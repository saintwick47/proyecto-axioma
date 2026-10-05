#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# Ruta: /ruta/a/axioma/src/core/dispatcher_operations.py
# Autor: SaintWick
# dispatcher_operations.py - Mixin de operaciones auxiliares para el Dispatcher.
#
# Proporciona métodos de utilidad para:
#   - Registro de métricas con integración a self-healing y TraceStore.
#   - Procesamiento de streaming y dispatch predictivo (pre-warming).
#   - Gestión de contexto de sesión (SessionContext).
#   - Estadísticas y métricas públicas del dispatcher.
#   - Pre-calentamiento de caché y modelos en RAM.
#   - Limpieza de recursos (cleanup).
#
# Este mixin se combina con Dispatcher para separar la lógica de operaciones
# del ruteo core.
# ──────────────────────────────────────────────────────────────────────────────
import logging
import asyncio
import threading
import time
from typing import Optional, Dict, Any

from config.settings import settings
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports opcionales ───────────────────────────────────────────────────────

try:
    from src.tools_mcp import get_executor
    _EXECUTOR_AVAILABLE = True
except ImportError:
    _EXECUTOR_AVAILABLE = False
    get_executor = None

try:
    from src.core.self_healing import get_monitor as _get_healing_monitor
    _HEALING_AVAILABLE = True
except ImportError:
    _HEALING_AVAILABLE = False
    _get_healing_monitor = None

try:
    from src.core.feedback_loop import ImplicitSignal
    _IMPLICIT_SIGNAL_AVAILABLE = True
except ImportError:
    _IMPLICIT_SIGNAL_AVAILABLE = False
    ImplicitSignal = None  # type: ignore[assignment,misc]

try:
    from src.core.dispatcher_distributed import DispatcherDistributedMixin
    _DISTRIBUTED_AVAILABLE = True
except ImportError:
    _DISTRIBUTED_AVAILABLE = False

# ── TraceStore — import seguro ───────────────────────────────────────────────
try:
    from src.learning.trace_store import get_trace_store, Trace as _Trace
    _TRACE_STORE_AVAILABLE = True
except ImportError:
    _TRACE_STORE_AVAILABLE = False
    get_trace_store = None  # type: ignore[assignment]
    _Trace = None           # type: ignore[assignment]


class DispatcherOperationsMixin:
    """Mixin con operaciones de utilidad, estadísticas y mantenimiento del Dispatcher.

    Este mixin se combina con Dispatcher para separar la lógica de operaciones
    (stats, streaming, cache warming, cleanup) del ruteo core.
    """

    # ── Métricas + Self-Healing ──────────────────────────────────────────────

    def _record_metric(
        self,
        task_type: str,
        handler: str,
        latency_ms: float,
        success: bool,
        plan_depth: int = 0,
        step_index: int = -1,
        # ✅ v0.9.4: extras opcionales para TraceStore — callers existentes no cambian
        session_id: Optional[str] = None,
        query_len: int = 0,
        confidence: float = 0.0,
        model: str = "",
        tokens_prompt: int = 0,
        tokens_generated: int = 0,
        tool_calls: list = None,
        # ✅ Fase 7: contrato de código (Fase 1-4) — opcionales, default
        # False/0 preserva el comportamiento exacto para callers
        # existentes (weather/chat/search/etc, que nunca los pasan).
        code_contract_complete: bool = False,
        code_validation_passed: bool = False,
        code_retries: int = 0,
        code_truncated: bool = False,
    ) -> None:
        """Registra métrica, self-healing y traza persistente en SQLite."""
        if not hasattr(self, '_bounded_metrics') or self._bounded_metrics is None:
            return
        self._bounded_metrics.record(
            task_type, handler, latency_ms, success, plan_depth, step_index,
        )

        # ✅ v0.9.4: TraceStore — persiste en SQLite en daemon thread (fire-and-forget)
        # Import seguro: si _TRACE_STORE_AVAILABLE es False el bloque no se ejecuta.
        # record_async() usa daemon thread → no añade latencia al Dispatcher.
        if _TRACE_STORE_AVAILABLE:
            try:
                _ts = get_trace_store()
                if _ts is not None and _Trace is not None:
                    _ts.record_async(_Trace(
                        task_type=task_type,
                        handler=handler,
                        latency_ms=latency_ms,
                        success=success,
                        session_id=session_id,
                        query_len=query_len,
                        confidence=confidence,
                        model=model,
                        tokens_prompt=tokens_prompt,
                        tokens_generated=tokens_generated,
                        tool_calls=tool_calls or [],
                        code_contract_complete=code_contract_complete,
                        code_validation_passed=code_validation_passed,
                        code_retries=code_retries,
                        code_truncated=code_truncated,
                    ))
            except Exception as e:
                # R3: el tracing es observabilidad — nunca bloquea el pipeline.
                log_degraded(logger, e, "Dispatcher._record_metric (tracing)",
                             contract_level=logging.DEBUG)

        # ✅ Semana 0: self-healing activo — consume suggest_action()
        # Estrategia sin set_handler_priority (no existe en TaskQueue):
        #   circuit_break  → corte real en dispatcher._dispatch() (consulta en vivo
        #                    al monitor antes de invocar al handler — ver v0.9.6).
        #                    Acá solo se loguea la transición.
        #   fallback_model → actualizar routing_cache con modelo alternativo
        #   reduce_load    → log warning (sin acción estructural — TaskQueue no lo soporta)
        if not _HEALING_AVAILABLE:
            return
        try:
            monitor = _get_healing_monitor()
            monitor.check_handler(handler, latency_ms, success)
            action = monitor.suggest_action(handler)
            action_value = action.value if hasattr(action, 'value') else str(action)

            if action_value == "circuit_break":
                # ✅ FIX AUDITORÍA v0.9.6: antes esto escribía __fallback_{handler}
                # en routing_cache, key que ningún archivo leía — el corte era
                # cosmético. El corte real ahora ocurre en dispatcher._dispatch(),
                # que consulta suggest_action(handler) en vivo antes de dispatch.
                logger.warning(f"[HEALING] CIRCUIT_BREAK: {handler} — rechazo activo en _dispatch()")

            elif action_value == "fallback_model":
                logger.warning(f"[HEALING] FALLBACK_MODEL: {handler} → {getattr(settings, 'llm_fallback_1', 'N/A')}")
                if self._routing_cache:
                    fallback_model = getattr(settings, 'llm_fallback_1', settings.llm_default_model)
                    cached = self._routing_cache.get(f"__model_{task_type}")
                    if cached:
                        cached["model"] = fallback_model
                        self._routing_cache.set(f"__model_{task_type}", cached)

            elif action_value == "reduce_load":
                # TaskQueue.set_handler_priority() no existe — solo log
                logger.info(f"[HEALING] REDUCE_LOAD: {handler} (sin acción en TaskQueue)")

        except Exception as _d2e:
            log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_operations.py:_record_metric")

    # ── [B1]: Streaming + Predictive dispatch ───────────────────────────────

    async def process_stream(
        self,
        text: str,
        is_final: bool,
        session_id: str,
    ) -> "Optional[Any]":
        """
        [B1] Procesa texto parcial o final del STT stream.
        Si not is_final: cachea en _partial_cache y retorna None.
        Si is_final: construye Message y llama process() normal.
        Limpia _partial_cache[session_id] después de is_final=True.
        """
        if not is_final:
            self._partial_cache[session_id] = text
            return None
        # Final: usar texto acumulado si existe, sino el texto recibido
        full_text = self._partial_cache.pop(session_id, text) or text
        from src.domain.entities import Message, MessageRole
        message = Message(content=full_text, role=MessageRole.USER)
        return await self.process(message, session_id=session_id)

    async def process_predictive(
        self,
        query: str,
        weights: Dict[str, float],
    ) -> None:
        """
        [B1] Pre-calienta el handler más probable según weights.
        Ejecuta en background via create_task — no retorna Response.
        Cualquier error → log WARNING y continuar.
        """
        try:
            if not weights:
                return
            top_handler = max(weights, key=weights.get)
            asyncio.create_task(
                asyncio.to_thread(self._pre_warm_handler, top_handler, weights)
            )
        except Exception as e:
            logger.warning(f"[B1] process_predictive error: {e}")

    def _pre_warm_handler(
        self,
        handler: str,
        weights: Dict[str, float],
    ) -> None:
        """
        [B1] Lazy init del handler indicado y entrada sintética en routing_cache.
        Solo actúa si el handler no está ya cacheado (baja confianza = 0.5).
        """
        try:
            # Lazy init según handler
            if handler == "CoderAgent":
                _ = self.coder
            elif handler == "SearcherAgent":
                _ = self.searcher
            elif handler == "ResearcherAgent":
                _ = self.researcher
            elif handler == "ValidatorAgent":
                _ = self.validator
            # Poblar routing_cache con entrada sintética de baja confianza
            if self._routing_cache:
                synthetic_key = f"__prewarm_{handler}"
                if not self._routing_cache.get(synthetic_key):
                    self._routing_cache.set(
                        synthetic_key,
                        {"handler": handler, "confidence": 0.5, "prewarm": True},
                    )
            logger.debug(f"[B1] _pre_warm_handler: {handler} calentado")
        except Exception as e:
            logger.warning(f"[B1] _pre_warm_handler error ({handler}): {e}")

    # ── CAAC: SessionContext helpers ─────────────────────────────────────────

    def get_session_context(self, session_id: str) -> Optional[Any]:
        """Retorna el SessionContext almacenado para el session_id dado."""
        with self._session_store_lock:
            return self._session_store.get(session_id)

    def clear_session_context(self, session_id: str) -> None:
        """Elimina el SessionContext del store para el session_id dado."""
        with self._session_store_lock:
            self._session_store.pop(session_id, None)

    def get_session_store_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas del store de sesiones."""
        with self._session_store_lock:
            return {
                "active_sessions": len(self._session_store),
                "session_ids": list(self._session_store.keys()),
            }

    # ── Stats / Métricas públicas ────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna todas las métricas del dispatcher incluyendo cache y planning."""
        if not hasattr(self, '_bounded_metrics') or self._bounded_metrics is None:
            return {}
        metrics = self._bounded_metrics.get_all()
        if self._routing_cache:
            metrics["routing_cache"] = self._routing_cache.get_stats()
        metrics["planning"] = self._bounded_metrics.get_planning_stats()
        return metrics

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas completas del dispatcher."""
        stats = {
            "metrics": self.get_metrics(),
            "cache_enabled": self._enable_cache,
            "cache_size": self._routing_cache_size,
            "total_requests": (
                self._bounded_metrics.get_total_count()
                if self._bounded_metrics else 0
            ),
            "agent_factory_active": self._agent_factory is not None,
            "user_profile_active": self._user_profile is not None,
            "sensor_enabled": self._sensor_enabled,
            "data_tasks": list(self.DATA_TASKS),
            "code_tasks": list(self.CODE_TASKS),
            "search_tasks": list(self.SEARCH_TASKS),
            "analysis_tasks": list(self.ANALYSIS_TASKS),
            "validation_tasks": list(self.VALIDATION_TASKS),
            "system_tasks": list(self.SYSTEM_TASKS),
        }
        if self._tool_registry is not None:
            stats["tool_registry"] = self._tool_registry.get_stats()
        if _HEALING_AVAILABLE:
            try:
                stats["health"] = _get_healing_monitor().get_all_statuses()
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_operations.py:get_stats")
        if _DISTRIBUTED_AVAILABLE:
            try:
                stats["distributed"] = self.get_distributed_stats()
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_operations.py:get_stats")
        # ✅ v0.9.4: TraceStore stats
        if _TRACE_STORE_AVAILABLE:
            try:
                _ts = get_trace_store()
                if _ts is not None:
                    stats["trace_store"] = _ts.get_stats()
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_operations.py:get_stats")
        return stats

    def get_feedback_stats(self) -> Dict[str, Any]:
        """✅ Fase 6 — Estadísticas del FeedbackLoop."""
        if not _IMPLICIT_SIGNAL_AVAILABLE or self._feedback_loop is None:
            return {"available": False, "initialized": self._feedback_loop_initialized}
        try:
            return {"available": True, **self._feedback_loop.get_global_stats()}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def get_tool_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas del ToolRegistry y Executor."""
        if not _EXECUTOR_AVAILABLE or self._tool_registry is None:
            return {"available": False}
        try:
            executor = get_executor()
            return {
                "available": True,
                "registry": self._tool_registry.get_stats(),
                "executor": executor.get_stats(),
            }
        except Exception as e:
            return {"available": False, "error": str(e)}

    # ── Cache Warming & Cleanup ──────────────────────────────────────────────

    async def pre_warm_cache(self) -> Dict[str, Any]:
        """Precalienta modelos críticos en RAM y popula routing cache."""
        warmup_stats = {
            "models_loaded": 0, "cache_entries": 0,
            "errors": [], "duration_ms": 0.0,
        }
        start_warm = time.perf_counter()
        logger.info("🔥 Iniciando pre-warm cache...")

        critical_models = [
            getattr(settings, "llm_default_model", "qwen3:8b"),
            getattr(settings, "llm_code_model", "qwen2.5-coder:7b"),
            getattr(settings, "llm_embed_model", "BAAI/bge-m3"),
        ]
        for model in critical_models:
            try:
                self.llm_client.generate(
                    prompt="init", model=model, max_tokens=1,
                    stream=False, task_type="WARMUP",
                )
                warmup_stats["models_loaded"] += 1
                logger.debug(f"✅ Modelo cargado en RAM: {model}")
            except Exception as e:
                warmup_stats["errors"].append(f"Modelo {model}: {e}")
                logger.warning(f"⚠️ {e}")

        warmup_queries = [
            "hola", "buenos días", "ayuda",
            "clima en Buenos Aires", "qué hora es",
            "cotización del dólar", "escribe código",
        ]
        for query in warmup_queries:
            try:
                classification = self.classifier.classify(query)
                task_type = classification.get("task_type", "general_chat")
                confidence = classification.get("confidence", 0.5)
                handler = self._handler_name(task_type)
                if self._routing_cache and confidence >= 0.5:
                    self._routing_cache.set(
                        query,
                        {"task_type": task_type, "handler": handler, "confidence": confidence},
                    )
                warmup_stats["cache_entries"] += 1
            except Exception as e:
                warmup_stats["errors"].append(f"Query '{query}': {e}")

        warmup_stats["duration_ms"] = (time.perf_counter() - start_warm) * 1000
        logger.info(
            f"🔥 Pre-warm: {warmup_stats['duration_ms']:.2f}ms | "
            f"Modelos: {warmup_stats['models_loaded']} | "
            f"Caché: {warmup_stats['cache_entries']} | "
            f"Errores: {len(warmup_stats['errors'])}"
        )
        return warmup_stats

    def clear_cache(self) -> None:
        """Limpia el routing cache."""
        if self._routing_cache:
            self._routing_cache.clear()
            logger.info("Dispatcher routing cache cleared")

    async def cleanup(self) -> None:
        """Limpia recursos del dispatcher."""
        if self._routing_cache:
            self._routing_cache.clear()
        if self._llm_client:
            self._llm_client.close()
        # ✅ FIX AUDITORÍA: esperar a que los writes de L3 en vuelo terminen
        # antes de cerrar — antes, con threads daemon sueltos, un write en
        # curso al momento del shutdown se podía perder silenciosamente.
        if getattr(self, "_l3_write_executor", None) is not None:
            self._l3_write_executor.shutdown(wait=True, cancel_futures=False)
        # ✅ v0.9.4: cerrar TraceStore
        if _TRACE_STORE_AVAILABLE:
            try:
                _ts = get_trace_store()
                if _ts is not None:
                    _ts.close()
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_operations.py:cleanup")
        logger.info("Dispatcher cleaned up")
