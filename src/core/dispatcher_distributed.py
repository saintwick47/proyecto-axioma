#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.7.0 — Dispatcher: Integración Distribuida
# Ruta: src/core/dispatcher_distributed.py
# Autor: SaintWick
# Fase 7 — Escalado Distribuido
# Contiene: DispatcherDistributedMixin
#   - enqueue_to_distributed(): rutea al DistributedQueue por task_type
#   - dispatch_via_lb(): ejecuta handler vía LoadBalancer
#   - get_distributed_stats(): métricas combinadas DQ + LB
# NOTA: Mixin puro — sin __init__ propio. Requiere que la clase
#       base tenga self._metrics (_DispatcherMetrics) y self._handler_name()
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from src.domain.entities import Message, Response
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports opcionales ────────────────────────────────────────────────────────
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_event(message: str, level: str = "INFO", extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(category="DISPATCHER_DISTRIBUTED",
                          message=message, level=level, extra=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_distributed.py:_log_event")


# ── Imports de infraestructura distribuida ────────────────────────────────────
try:
    from src.core.distributed_queue import get_distributed_queue, DistributedQueue
    from src.core.task_queue import TaskPriority
    _DQ_AVAILABLE = True
except ImportError:
    _DQ_AVAILABLE = False
    logger.warning("[DISPATCHER_DISTRIBUTED] DistributedQueue no disponible")

try:
    from src.core.load_balancer import get_load_balancer, LoadBalancer
    _LB_AVAILABLE = True
except ImportError:
    _LB_AVAILABLE = False
    logger.warning("[DISPATCHER_DISTRIBUTED] LoadBalancer no disponible")


# ── Mapeo task_type → TaskPriority ───────────────────────────────────────────
_PRIORITY_MAP: Dict[str, "TaskPriority"] = {}

if _DQ_AVAILABLE:
    _PRIORITY_MAP = {
        "code_generation":    TaskPriority.HIGH,
        "code_debugging":     TaskPriority.HIGH,
        "code_review":        TaskPriority.NORMAL,
        "code_correction":    TaskPriority.HIGH,
        "research":           TaskPriority.NORMAL,
        "data_analysis":      TaskPriority.NORMAL,
        "fact_checking":      TaskPriority.NORMAL,
        "quality_validation": TaskPriority.LOW,
        "general_chat":       TaskPriority.NORMAL,
        "weather_query":      TaskPriority.LOW,
        "finance_query":      TaskPriority.LOW,
        "news_query":         TaskPriority.LOW,
        "time_query":         TaskPriority.LOW,
        "web_search":         TaskPriority.NORMAL,
    }


def _get_priority(task_type: str) -> "TaskPriority":
    if not _DQ_AVAILABLE:
        return None  # type: ignore
    return _PRIORITY_MAP.get(task_type, TaskPriority.NORMAL)


# ============================================================================
# MIXIN
# ============================================================================
class DispatcherDistributedMixin:
    """
    Mixin que agrega capacidad de ruteo distribuido al Dispatcher.

    Métodos públicos:
        enqueue_to_distributed()   — encola en DistributedQueue por task_type
        dispatch_via_lb()          — ejecuta handler vía LoadBalancer
        get_distributed_stats()    — métricas DQ + LB combinadas
        process_distributed()      — process() con ruteo distribuido completo

    Degradación elegante: si DQ o LB no están disponibles, el método
    correspondiente levanta RuntimeError con mensaje claro.
    """

    # ── DistributedQueue ──────────────────────────────────────────────────────

    def _get_dq(self) -> "DistributedQueue":
        if not _DQ_AVAILABLE:
            raise RuntimeError(
                "[DISPATCHER_DISTRIBUTED] DistributedQueue no disponible — "
                "verificar src/core/distributed_queue.py"
            )
        return get_distributed_queue()

    def _get_lb(self) -> "LoadBalancer":
        if not _LB_AVAILABLE:
            raise RuntimeError(
                "[DISPATCHER_DISTRIBUTED] LoadBalancer no disponible — "
                "verificar src/core/load_balancer.py"
            )
        return get_load_balancer()

    async def enqueue_to_distributed(
        self,
        msg: Message,
        task_type: str,
        session_id: Optional[str] = None,
        priority: Optional["TaskPriority"] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Encola un mensaje en la partición correcta del DistributedQueue.

        Returns:
            task_id de la tarea encolada

        Raises:
            RuntimeError: Si DistributedQueue no está disponible
        """
        dq = self._get_dq()
        effective_priority = priority or _get_priority(task_type)
        meta = dict(metadata or {})
        if session_id:
            meta["session_id"] = session_id

        task = await dq.enqueue(
            name=f"dispatch:{task_type}",
            payload={"content": msg.content, "role": msg.role},
            task_type=task_type,
            priority=effective_priority,
            metadata=meta,
        )

        _log_event(f"Enqueued task_type={task_type}", extra={
            "task_id": task.id,
            "task_type": task_type,
            "priority": effective_priority.name if effective_priority else "NORMAL",
            "session_id": session_id,
        })
        logger.debug(
            f"[DISPATCHER_DISTRIBUTED] Enqueued {task_type} → "
            f"partition={task.metadata.get('partition', '?')} "
            f"task_id={task.id}"
        )
        return task.id

    async def dispatch_via_lb(
        self,
        handler_fn: Callable[..., Any],
        *args: Any,
        task_type: str = "general_chat",
        **kwargs: Any,
    ) -> Any:
        """
        Ejecuta un handler async a través del LoadBalancer (least-connections).

        Args:
            handler_fn: Función async del handler a ejecutar
            *args:      Argumentos posicionales para handler_fn
            task_type:  Para logging/métricas
            **kwargs:   Argumentos keyword para handler_fn

        Returns:
            Resultado del handler

        Raises:
            TimeoutError: Si no hay worker disponible
            RuntimeError: Si LoadBalancer no está disponible
        """
        lb = self._get_lb()
        handler_name = self._handler_name(task_type)

        t0 = time.perf_counter()
        success = False
        try:
            result = await lb.dispatch(handler_fn, *args, **kwargs)
            success = True
            return result
        except Exception:
            raise
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            # Registrar en métricas del dispatcher si están disponibles
            if hasattr(self, "_metrics"):
                self._metrics.record(
                    task_type=task_type,
                    handler=f"LB:{handler_name}",
                    latency_ms=elapsed_ms,
                    success=success,
                )
            _log_event(
                f"LB dispatch: {handler_name}",
                level="INFO" if success else "ERROR",
                extra={
                    "task_type": task_type,
                    "handler": handler_name,
                    "latency_ms": round(elapsed_ms, 2),
                    "success": success,
                },
            )

    async def process_distributed(
        self,
        msg: Message,
        session_id: Optional[str] = None,
        use_lb: bool = True,
    ) -> Response:
        """
        Versión distribuida de process() que usa DQ + LB.

        Flujo:
            1. Clasifica el mensaje (usa self._classify() del dispatcher base)
            2. Encola en DistributedQueue por task_type
            3. Despacha el handler vía LoadBalancer
            4. Retorna Response

        Si DQ/LB no están disponibles, hace fallback a process() normal.

        Args:
            msg:        Mensaje de entrada
            session_id: ID de sesión
            use_lb:     Si False, ejecuta handler directamente sin LB

        Returns:
            Response del handler
        """
        # Fallback si infraestructura no disponible
        if not _DQ_AVAILABLE or not _LB_AVAILABLE:
            logger.warning(
                "[DISPATCHER_DISTRIBUTED] DQ/LB no disponibles — "
                "fallback a process() estándar"
            )
            return await self.process(msg, session_id=session_id)

        t0 = time.perf_counter()

        try:
            # ✅ FIX AUDITORÍA: antes usaba self._smart_router.route(msg.content) —
            # Dispatcher no tiene ese atributo (usa self.classifier directo, no
            # SmartRouter — ver hallazgo L11/X2), y encima asumía un objeto con
            # .task_type.value cuando self.classifier.classify() devuelve un
            # dict (classification.get("task_type", ...)). Si esto se llegaba a
            # invocar, tiraba AttributeError garantizado. Ahora usa el mismo
            # contrato real que process() (incluyendo asyncio.to_thread — ver
            # fix L1 de dispatcher.py).
            classification = await asyncio.to_thread(self.classifier.classify, msg.content)
            task_type = classification.get("task_type", "general_chat")
            handler_name = self._handler_name(task_type)

            # 2. Encolar en DQ (registro asíncrono — no bloquea)
            try:
                await self.enqueue_to_distributed(
                    msg=msg,
                    task_type=task_type,
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning(f"[DISPATCHER_DISTRIBUTED] Enqueue falló (no crítico): {e}")

            # 3. Resolver handler
            handler_fn = self._resolve_handler(task_type)

            # 4. Ejecutar vía LB o directo
            if use_lb:
                response = await self.dispatch_via_lb(
                    handler_fn,
                    msg,
                    session_id=session_id,
                    task_type=task_type,
                )
            else:
                response = await handler_fn(msg, session_id=session_id)

            elapsed_ms = (time.perf_counter() - t0) * 1000
            _log_event(f"process_distributed OK: {task_type}", extra={
                "handler": handler_name,
                "latency_ms": round(elapsed_ms, 2),
                "session_id": session_id,
                "use_lb": use_lb,
            })
            return response

        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.error(
                f"[DISPATCHER_DISTRIBUTED] process_distributed falló: {e} "
                f"({elapsed_ms:.0f}ms) — fallback a process()"
            )
            # Fallback garantizado
            return await self.process(msg, session_id=session_id)

    def _resolve_handler(self, task_type: str) -> Callable:
        """
        Resuelve el método handler para un task_type dado.
        Mapeo consistente con _handler_name() del dispatcher base.
        """
        handler_map = {
            "CoderAgent":     self._handle_code,
            "DataHandler":    self._handle_data,
            "SearcherAgent":  self._handle_search,
            "FileSearchAgent": self._handle_file_search,  # ✅ plan_rafael (2026-08-31)
            "ResearcherAgent":self._handle_research,
            "ValidatorAgent": self._handle_validation,
            "SystemAgent":    self._handle_system,
            "AnalystAgent":   self._handle_analysis,  # ✅ CORREGIDO: Antes "LLM_Analysis"
            "LLM_Chat":       self._handle_chat,
        }
        handler_name = self._handler_name(task_type)
        handler = handler_map.get(handler_name)
        if handler is None:
            raise ValueError(
                f"[DISPATCHER_DISTRIBUTED] Handler desconocido para "
                f"task_type='{task_type}' (handler_name='{handler_name}')"
            )
        return handler

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_distributed_stats(self) -> Dict[str, Any]:
        """
        Retorna métricas combinadas de DistributedQueue + LoadBalancer.
        Seguro si alguno no está disponible.
        """
        stats: Dict[str, Any] = {
            "distributed_queue": None,
            "load_balancer": None,
        }

        if _DQ_AVAILABLE:
            try:
                dq = get_distributed_queue()
                stats["distributed_queue"] = {
                    "partitions": {
                        name: {
                            "pending": dq.pending_count(name),
                            "running": dq.running_count(name),
                        }
                        for name in ("fast", "medium", "slow", "default")
                    }
                }
            except Exception as e:
                stats["distributed_queue"] = {"error": str(e)}

        if _LB_AVAILABLE:
            try:
                lb = get_load_balancer()
                stats["load_balancer"] = lb.get_stats()
            except Exception as e:
                stats["load_balancer"] = {"error": str(e)}

        return stats
