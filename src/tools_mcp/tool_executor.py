#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# tool_executor.py - Capa de ejecución segura para tools (ToolExecutor)
# Ruta: /ruta/a/axioma/src/tools_mcp/tool_executor.py
# Autor: SaintWick
# Versión: 0.6.0
#
# Orquesta la ejecución segura de tools: resolución, validación de
# permisos, PermissionGateway, CircuitBreaker, timeout, retry,
# métricas y logging. Siempre retorna ToolResult sin excepciones.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tool_base import (
    ToolPermission,
    ToolPermissionError,
    ToolResult,
    ToolTimeoutError,
)
from .tool_registry import ToolRegistry, get_registry

# ── Import desde executor_infrastructure (refactorización 50/50) ──
from .executor_infrastructure import (
    ToolMetrics,
    CircuitBreaker,
    CircuitBreakerRegistry,
)

logger = logging.getLogger(__name__)

# ✅ FASE 1 (plan_harness): RuntimeMode para filtrado por modo de ejecución.
# types.py importa tool_base (sin dependencias internas de AXIOMA), así
# que este import no crea ciclos.
try:
    from src.domain.types import RuntimeMode
    _RUNTIME_MODE_AVAILABLE = True
except ImportError as _rie:  # pragma: no cover — contexto aislado
    logger.warning(f"[Executor] RuntimeMode no disponible: {_rie}")
    RuntimeMode = None  # type: ignore[assignment,misc]
    _RUNTIME_MODE_AVAILABLE = False

# ✅ FASE 3 (plan_harness): eventos de tools (pre/post/bloqueo).
# - EventTypes (src.domain.events) es importable a nivel de módulo: el
#   paquete domain ya no importa tools_mcp, así que no hay ciclo.
# - El bus (src.core.event_bus) se importa DIFERIDO dentro de
#   _publish_event: src/core/__init__ importa el Dispatcher con
#   impaciencia y este importa tools_mcp — un import a nivel de módulo
#   crearía un ciclo (tool_executor → src.core → dispatcher →
#   tool_executor parcial) cuando tool_executor se importa temprano.
try:
    from src.domain.events import EventTypes as _EventTypes
except ImportError:  # pragma: no cover — contexto aislado
    _EventTypes = None  # type: ignore[assignment,misc]


def _publish_event(event_type: str, data: Dict[str, Any]) -> None:
    """Publica un evento al bus si está disponible. Silencioso si no."""
    try:
        from src.core.event_bus import get_event_bus
        get_event_bus().publish_simple(event_type, data, source="tool_executor")
    except Exception as e:  # noqa: BLE001 — el bus nunca debe romper al executor
        logger.debug(f"[Executor] publish event error (non-fatal): {e}")


# ── Import seguro DetailedLogger ────────────────────────────────
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


# ── Import seguro PermissionGateway ─────────────────────────────
try:
    from .permissions import get_gateway as _get_permission_gateway, Decision as _Decision
    _PERMISSIONS_AVAILABLE = True
except ImportError:
    _PERMISSIONS_AVAILABLE = False
    _get_permission_gateway = None  # type: ignore[assignment]
    _Decision = None                # type: ignore[assignment]


def _log_tool_event(
    tool_name: str,
    event: str,
    success: bool = True,
    duration: float = None,
    error: str = None,
    extra: Dict[str, Any] = None,
) -> None:
    """Delega al DetailedLogger si está disponible. Silencioso si no."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = _get_detailed_logger()
        if getattr(log, "is_initialized", False):
            log.log_agent_execution(
                agent_name=f"tool:{tool_name}",
                task_type="TOOL_EXECUTION",
                state=event,
                duration=duration,
                success=success,
                error=error,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 tool_executor.py:112] excepción degradada (intencional): {_d2e}")


# ============================================================================
# EXECUTION CONTEXT — Configuración por llamada
# ============================================================================

@dataclass
class ExecutionContext:
    """
    Contexto de ejecución para una llamada a tool.

    Permite configurar permisos, timeout y comportamiento de retry
    por llamada — sin modificar la tool ni el registry.

    Atributos:
        session_permission : Nivel máximo permitido en esta sesión.
                             Default EXECUTE — seguro para uso general.
        timeout_seconds    : Timeout real para la ejecución. None = sin límite.
        max_retries        : Reintentos ante fallo. 0 = sin retry.
        retry_delay        : Segundos entre reintentos (backoff fijo).
        caller             : Identificador del llamador (agente, módulo).
                             Usado en logging y métricas.
        metadata           : Datos adicionales de contexto para trazabilidad.

    ✅ v0.5.0 (Fase 1) — Nuevos campos para PermissionGateway:
        resource_path      : Path del recurso afectado por la tool.
                             Usado por PermissionGateway para matching de reglas.
                             Default "" → gateway usa el nombre de la tool como path.
        risk_score         : Score de riesgo estimado de la operación (0.0–1.0).
                             El agente o el planner lo calculan y lo propagan aquí.
                             Default 0.2 — conservador para operaciones no clasificadas.
        skip_permission_check : Si True, omite el gateway (solo para tests internos).
                             Default False.
    """
    session_permission: ToolPermission = ToolPermission.EXECUTE
    timeout_seconds: Optional[float] = 30.0
    max_retries: int = 0
    retry_delay: float = 1.0
    caller: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)
    # ✅ CORREGIDO v0.5.0: Campos para PermissionGateway
    resource_path: str = ""
    risk_score: float = 0.2
    skip_permission_check: bool = False
    # ✅ FASE 1 (plan_harness): RuntimeMode de la sesión/llamada.
    # Default STANDARD (toolset completo) — retrocompatible con todos los
    # callers existentes. El paso 2.5 de run() filtra por modo.
    runtime_mode: Optional["RuntimeMode"] = None  # None → STANDARD


# ============================================================================
# TOOL EXECUTOR — Ejecución segura centralizada
# ============================================================================

class ToolExecutor:
    """
    Capa de ejecución segura para tools de AXIOMA.

    Responsabilidades (en orden de ejecución):
        1. Resolver la tool en el registry
        2. Validar permiso de la sesión vs permiso requerido
        3. Verificar que la tool esté disponible (is_available)
        3.5 ✅ v0.5.0: Evaluar PermissionGateway (path + confidence + risk)
        3.6 ✅ v0.6.0: Evaluar CircuitBreaker — bloquear si tool en estado OPEN
        4. Ejecutar con timeout real via asyncio
        5. Aplicar retry con backoff si corresponde
        6. Registrar métricas y delegar al DetailedLogger
        7. Retornar ToolResult siempre — nunca lanza excepciones al caller

    Thread-safe. Singleton accesible via get_executor().

    Comportamiento del PermissionGateway (paso 3.5):
        - Solo activo si ctx.resource_path != "" — sin path no hay recurso
          real que proteger.
        - Decision.ALLOW  → continúa normalmente
        - Decision.DENY   → retorna ToolResult.fail() con
                            metadata["gateway_denied"]=True
        - Decision.ASK    → retorna ToolResult.fail() con
                            metadata["gateway_ask"]=True
                            La UI/CLI puede detectar este flag y pedir
                            confirmación al usuario, luego reintentar con
                            ExecutionContext(skip_permission_check=True).
        - Gateway no disponible → continúa sin evaluación (degradación)
        - resource_path vacío   → continúa sin evaluación (invocaciones
          internas/agentes)
    """

    _instance: Optional["ToolExecutor"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, registry: Optional[ToolRegistry] = None) -> "ToolExecutor":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._executor_initialized = False
            return cls._instance

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        if self._executor_initialized:
            return
        self._registry = registry or get_registry()
        self._metrics = ToolMetrics(max_entries=500)
        # ✅ v0.6.0: CircuitBreaker registry — un breaker por tool
        # failure_threshold=5: 5 fallos consecutivos abren el circuito
        # cooldown_seconds=60: 60s en OPEN antes de intentar HALF_OPEN
        self._cb_registry = CircuitBreakerRegistry(
            default_failure_threshold=5,
            default_cooldown_seconds=60.0,
        )
        self._executor_initialized = True
        logger.info("ToolExecutor inicializado")

    # ── Ejecución principal ───────────────────────────────────────────────────

    async def run(
        self,
        tool_name: str,
        context: Optional[ExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Ejecuta una tool de forma segura y asíncrona.

        Punto de entrada principal desde agentes y el dispatcher.
        Siempre retorna ToolResult — nunca lanza excepciones.

        Args:
            tool_name : Nombre de la tool registrada en el registry.
            context   : ExecutionContext con permisos y configuración.
                        Si None, usa defaults (EXECUTE, timeout=30s).
            **kwargs  : Parámetros de la tool.

        Returns:
            ToolResult con success=True o success=False + error explícito.
        """
        ctx = context or ExecutionContext()
        t0 = time.perf_counter()

        # ── 1. Resolver tool ─────────────────────────────────────────────
        try:
            tool = self._registry.get(tool_name)
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            logger.warning(f"[Executor] Tool no encontrada: '{tool_name}'")
            self._metrics.record(tool_name, latency, success=False)
            return ToolResult.fail(
                error=str(e),
                tool_name=tool_name,
                latency_ms=latency,
            )

        # ── 2. Validar permisos de sesión ────────────────────────────────
        if tool.permission_level > ctx.session_permission:
            latency = (time.perf_counter() - t0) * 1000
            err = ToolPermissionError(
                tool_name, tool.permission_level, ctx.session_permission
            )
            logger.warning(f"[Executor] Permiso denegado: {err}")
            _log_tool_event(
                tool_name, "PERMISSION_DENIED", success=False,
                duration=latency / 1000, error=str(err),
            )
            _publish_event(_EventTypes.TOOL_DENIED, {
                "tool_name": tool_name, "caller": ctx.caller,
                "reason": "session_permission",
                "required": tool.permission_level,
                "allowed": ctx.session_permission,
            })
            self._metrics.record(tool_name, latency, success=False)
            return ToolResult.fail(
                error=str(err),
                tool_name=tool_name,
                latency_ms=latency,
                metadata={
                    "permission_denied": True,
                    "required": tool.permission_level,
                    "allowed": ctx.session_permission,
                },
            )

        # ── 2.5 ✅ FASE 1 (plan_harness) + gap.txt: RuntimeMode check ────
        # Filtra la tool por el modo de ejecución de la sesión/llamada
        # (SAFE_MODE solo lectura, MINIMAL mínimo, etc.). Se evalúa después
        # del permiso de sesión porque ambos son restricciones de nivel.
        # Acepta RuntimeMode (enum) o str ("safe_mode", ...) — callers
        # externos pueden pasar el valor de settings tal cual.
        if _RUNTIME_MODE_AVAILABLE and ctx.runtime_mode is not None:
            try:
                mode = (
                    ctx.runtime_mode
                    if isinstance(ctx.runtime_mode, RuntimeMode)
                    else RuntimeMode(str(ctx.runtime_mode))
                )
            except (ValueError, TypeError):
                # RuntimeMode inválido — degradación silenciosa (gap.txt §3)
                logger.debug(
                    f"[Executor] RuntimeMode inválido '{ctx.runtime_mode}' — check omitido"
                )
                mode = None
            if mode is not None:
                max_perm = mode.allowed_permission_level()
                if tool.permission_level > max_perm:
                    latency = (time.perf_counter() - t0) * 1000
                    msg = (
                        f"Tool '{tool_name}' bloqueada por RuntimeMode."
                        f"{mode.value} (permiso {tool.permission_level.name} > "
                        f"máximo {max_perm.name})"
                    )
                    logger.warning(f"[Executor] [Mode] {msg}")
                    _log_tool_event(
                        tool_name, "RUNTIME_MODE_BLOCKED", success=False,
                        duration=latency / 1000, error=msg,
                    )
                    _publish_event(_EventTypes.TOOL_DENIED, {
                        "tool_name": tool_name, "caller": ctx.caller,
                        "reason": "runtime_mode",
                        "mode": mode.value,
                        "required": tool.permission_level,
                        "allowed": max_perm,
                    })
                    self._metrics.record(tool_name, latency, success=False)
                    return ToolResult.fail(
                        error=msg,
                        tool_name=tool_name,
                        latency_ms=latency,
                        metadata={
                            "runtime_mode_blocked": True,
                            "mode": mode.value,
                            "required": tool.permission_level,
                            "allowed": max_perm,
                        },
                    )

        # ── 3. Verificar disponibilidad ──────────────────────────────────
        if not tool.is_available():
            latency = (time.perf_counter() - t0) * 1000
            msg = (
                f"Tool '{tool_name}' no disponible "
                f"(API key ausente o servicio inactivo)."
            )
            logger.warning(f"[Executor] {msg}")
            self._metrics.record(tool_name, latency, success=False)
            return ToolResult.fail(
                error=msg,
                tool_name=tool_name,
                latency_ms=latency,
                metadata={"unavailable": True},
            )

        # ── 3.5 ✅ v0.5.0: PermissionGateway ─────────────────────────────
        if (
            _PERMISSIONS_AVAILABLE
            and not ctx.skip_permission_check
            and ctx.resource_path
        ):
            gateway_result = self._evaluate_permission_gateway(
                tool_name=tool_name,
                tool_permission=tool.permission_level,
                ctx=ctx,
                t0=t0,
            )
            if gateway_result is not None:
                return gateway_result

        # ── 3.6 ✅ v0.6.0: CircuitBreaker ────────────────────────────────
        cb = self._cb_registry.get(tool_name)
        if not cb.allow_request():
            latency = (time.perf_counter() - t0) * 1000
            status = cb.get_status()
            msg = (
                f"Tool '{tool_name}' circuit OPEN — "
                f"{status['failures_consecutive']} fallos consecutivos. "
                f"Reintento en ~{status['cooldown_seconds']}s."
            )
            logger.warning(f"[Executor] [CB] {msg}")
            _log_tool_event(
                tool_name, "CIRCUIT_OPEN", success=False,
                duration=latency / 1000, error=msg,
            )
            _publish_event(_EventTypes.CIRCUIT_OPEN, {
                "tool_name": tool_name, "caller": ctx.caller,
                "failures": status["failures_consecutive"],
                "cooldown_seconds": status["cooldown_seconds"],
            })
            _publish_event(_EventTypes.TOOL_DENIED, {
                "tool_name": tool_name, "caller": ctx.caller,
                "reason": "circuit_open",
            })
            self._metrics.record(tool_name, latency, success=False)
            return ToolResult.fail(
                error=msg,
                tool_name=tool_name,
                latency_ms=latency,
                metadata={"circuit_open": True, "circuit_status": status},
            )

        # ── 4 + 5. Ejecutar con timeout y retry ─────────────────────────
        _publish_event(_EventTypes.TOOL_CALLED, {
            "tool_name": tool_name, "caller": ctx.caller,
            "mode": ctx.runtime_mode.value if ctx.runtime_mode is not None else "standard",
            "resource_path": ctx.resource_path,
        })
        result = await self._execute_with_retry(
            tool_name, tool, ctx, **kwargs
        )

        # ── 3.6 cont.: registrar resultado en CircuitBreaker ─────────────
        cb.record_result(result.success)

        # ── 6. Métricas y logging ────────────────────────────────────────
        self._metrics.record(tool_name, result.latency_ms, result.success)
        _log_tool_event(
            tool_name=tool_name,
            event="COMPLETED" if result.success else "FAILED",
            success=result.success,
            duration=result.latency_ms / 1000,
            error=result.error,
            extra={"caller": ctx.caller, "source": result.source},
        )
        _publish_event(_EventTypes.TOOL_RESULT, {
            "tool_name": tool_name, "caller": ctx.caller,
            "success": result.success,
            "latency_ms": result.latency_ms,
            "error": (result.error or "")[:200],
        })

        status = "✅" if result.success else "❌"
        logger.debug(
            f"[Executor] {status} '{tool_name}' "
            f"{result.latency_ms:.1f}ms caller={ctx.caller!r}"
        )

        return result

    # ── PermissionGateway helper ──────────────────────────────────────────

    def _evaluate_permission_gateway(
        self,
        tool_name: str,
        tool_permission: ToolPermission,
        ctx: ExecutionContext,
        t0: float,
    ) -> Optional[ToolResult]:
        """
        ✅ v0.5.0: Evalúa el PermissionGateway para la tool actual.

        Retorna:
            None              → Decision.ALLOW, continuar ejecución normal
            ToolResult.fail() → Decision.DENY o ASK, detener ejecución

        Degradación silenciosa: si el gateway falla por cualquier razón,
        loguea el error y retorna None (permite la ejecución).
        Esto garantiza que un gateway roto no bloquee el sistema.
        """
        try:
            gateway = _get_permission_gateway()
            # resource_path vacío → usar nombre de la tool como path
            path = ctx.resource_path or tool_name
            # confidence del clasificador propagada via metadata
            confidence = float(
                ctx.metadata.get("classification_confidence", 0.92)
            )

            eval_result = gateway.evaluate(
                path=path,
                tool_name=tool_name,
                confidence=confidence,
                risk_score=ctx.risk_score,
                tool_permission=tool_permission,
            )

            latency = (time.perf_counter() - t0) * 1000

            if eval_result.decision == _Decision.DENY:
                logger.warning(
                    f"[Executor] Gateway DENY '{tool_name}' "
                    f"path={path!r}: {eval_result.reason}"
                )
                _log_tool_event(
                    tool_name, "GATEWAY_DENIED", success=False,
                    duration=latency / 1000, error=eval_result.reason,
                )
                self._metrics.record(tool_name, latency, success=False)
                return ToolResult.fail(
                    error=(
                        f"Permiso denegado por gateway: "
                        f"{eval_result.reason}"
                    ),
                    tool_name=tool_name,
                    latency_ms=latency,
                    metadata={
                        "gateway_denied": True,
                        "gateway_reason": eval_result.reason,
                        "gateway_rule": (
                            eval_result.rule.pattern
                            if eval_result.rule
                            else None
                        ),
                    },
                )

            if eval_result.decision == _Decision.ASK:
                logger.info(
                    f"[Executor] Gateway ASK '{tool_name}' "
                    f"path={path!r}: {eval_result.reason}"
                )
                _log_tool_event(
                    tool_name, "GATEWAY_ASK", success=False,
                    duration=latency / 1000, error=eval_result.reason,
                )
                self._metrics.record(tool_name, latency, success=False)
                return ToolResult.fail(
                    error=(
                        f"Se requiere confirmación del usuario: "
                        f"{eval_result.reason}"
                    ),
                    tool_name=tool_name,
                    latency_ms=latency,
                    metadata={
                        "gateway_ask": True,
                        "gateway_reason": eval_result.reason,
                        "gateway_rule": (
                            eval_result.rule.pattern
                            if eval_result.rule
                            else None
                        ),
                        # Flag para que CLI/UI detecte y pida confirmación
                        "requires_user_confirmation": True,
                    },
                )

            # Decision.ALLOW → None significa "continuar"
            logger.debug(
                f"[Executor] Gateway ALLOW '{tool_name}' path={path!r} "
                f"conf={confidence:.2f} risk={ctx.risk_score:.2f}"
            )
            return None

        except Exception as e:
            # Degradación silenciosa: gateway roto no bloquea el sistema
            logger.error(
                f"[Executor] PermissionGateway error (degradado): {e}"
            )
            return None

    # ── Versión síncrona ──────────────────────────────────────────────────

    def run_sync(
        self,
        tool_name: str,
        context: Optional[ExecutionContext] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Versión síncrona de run().

        Para usar desde contextos no-async (scripts, tests, CLI).
        Crea un event loop temporal si no hay uno activo.
        """
        try:
            try:
                asyncio.get_running_loop()
                loop_is_running = True
            except RuntimeError as e:
                # AUD-SIL: idiom estándar — sin loop activo en este hilo.
                logger.debug(f"[Executor] sin event loop activo: {e}")
                loop_is_running = False

            if loop_is_running:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=1
                ) as pool:
                    future = pool.submit(
                        asyncio.run,
                        self.run(tool_name, context, **kwargs),
                    )
                    return future.result()
            else:
                return asyncio.run(
                    self.run(tool_name, context, **kwargs)
                )
        except Exception as e:
            return ToolResult.fail(
                error=f"Error en run_sync: {e}",
                tool_name=tool_name,
            )

    # ── Ejecución con retry ───────────────────────────────────────────────

    async def _execute_with_retry(
        self,
        tool_name: str,
        tool: Any,
        ctx: ExecutionContext,
        **kwargs: Any,
    ) -> ToolResult:
        """
        Ejecuta la tool con timeout y reintentos.

        Cada intento tiene su propio timeout.
        Si todos los intentos fallan, retorna el último error.
        """
        max_attempts = max(1, ctx.max_retries + 1)
        last_result: Optional[ToolResult] = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    f"[Executor] Retry {attempt}/{ctx.max_retries} "
                    f"para '{tool_name}' en {ctx.retry_delay}s"
                )
                await asyncio.sleep(ctx.retry_delay)

            last_result = await self._execute_with_timeout(
                tool, ctx.timeout_seconds, **kwargs
            )

            if last_result.success:
                if attempt > 0:
                    last_result.metadata["retry_attempt"] = attempt
                return last_result

            # No reintentar errores de permiso o tool no encontrada
            if last_result.metadata.get("permission_denied"):
                return last_result

        return last_result  # type: ignore

    async def _execute_with_timeout(
        self,
        tool: Any,
        timeout_seconds: Optional[float],
        **kwargs: Any,
    ) -> ToolResult:
        """
        Ejecuta la tool con timeout real via asyncio.wait_for.

        Si la tool es sync, la ejecuta en un thread pool para no
        bloquear el event loop — mismo comportamiento que arun() en BaseTool.
        """
        t0 = time.perf_counter()
        tool_name = tool.name

        try:
            if timeout_seconds is not None:
                result = await asyncio.wait_for(
                    tool.arun(**kwargs),
                    timeout=timeout_seconds,
                )
            else:
                result = await tool.arun(**kwargs)
            return result

        except asyncio.TimeoutError:
            latency = (time.perf_counter() - t0) * 1000
            err = ToolTimeoutError(tool_name, timeout_seconds)
            logger.warning(f"[Executor] Timeout: {err}")
            return ToolResult.fail(
                error=str(err),
                tool_name=tool_name,
                latency_ms=latency,
                metadata={
                    "timeout": True,
                    "timeout_seconds": timeout_seconds,
                },
            )
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            logger.error(
                f"[Executor] Excepción ejecutando '{tool_name}': {e}"
            )
            return ToolResult.fail(
                error=f"{type(e).__name__}: {e}",
                tool_name=tool_name,
                latency_ms=latency,
            )

    # ── Ejecución en batch ────────────────────────────────────────────────

    async def run_batch(
        self,
        calls: List[Dict[str, Any]],
        context: Optional[ExecutionContext] = None,
        parallel: bool = False,
    ) -> List[ToolResult]:
        """
        Ejecuta múltiples tools.

        Args:
            calls    : Lista de dicts con 'tool' y kwargs de la tool.
                       Ej: [{"tool": "fs_read", "path": "main.py"}, ...]
            context  : ExecutionContext compartido para todas las calls.
            parallel : Si True, ejecuta en paralelo con asyncio.gather.
                       Si False, ejecuta en secuencia (más seguro).

        Returns:
            Lista de ToolResult en el mismo orden que calls.
        """
        if not calls:
            return []

        ctx = context or ExecutionContext()

        if parallel:
            tasks = [
                self.run(
                    call.pop("tool"),
                    context=ctx,
                    **call,
                )
                for call in [dict(c) for c in calls]
            ]
            return list(await asyncio.gather(*tasks))
        else:
            results = []
            for call in calls:
                call_copy = dict(call)
                tool_name = call_copy.pop("tool")
                result = await self.run(
                    tool_name, context=ctx, **call_copy
                )
                results.append(result)
            return results

    # ── Introspección ─────────────────────────────────────────────────────

    def can_execute(
        self, tool_name: str, session_permission: ToolPermission
    ) -> bool:
        """
        True si la tool existe, está disponible y el permiso es suficiente.
        Útil para que los agentes verifiquen antes de intentar ejecutar.
        Nota: no evalúa el PermissionGateway — es solo un pre-check básico.
        """
        if not self._registry.has(tool_name):
            return False
        tool = self._registry.get(tool_name)
        return (
            tool.permission_level <= session_permission
            and tool.is_available()
        )

    def available_for(
        self, session_permission: ToolPermission
    ) -> List[str]:
        """
        Lista de tools ejecutables con el nivel de permiso dado.
        Permite que los agentes sepan qué pueden hacer en su contexto.
        """
        return [
            t["name"]
            for t in self._registry.list_by_permission(session_permission)
            if t["available"]
        ]

    def get_metrics(self) -> Dict[str, Any]:
        """Métricas de ejecución por tool."""
        return self._metrics.get_all()

    def get_stats(self) -> Dict[str, Any]:
        """Resumen completo del executor."""
        stats = {
            "registry": self._registry.get_stats(),
            "execution_metrics": self.get_metrics(),
            "permissions_available": _PERMISSIONS_AVAILABLE,
            "circuit_breakers": self._cb_registry.get_all_statuses(),
        }
        # Incluir stats del gateway si está disponible
        if _PERMISSIONS_AVAILABLE:
            try:
                stats["gateway"] = _get_permission_gateway().get_stats()
            except Exception as e:
                # AUD-SIL-035: stats del gateway son best-effort — un fallo
                # del gateway no debe romper get_stats() del executor.
                logger.debug(f"[Executor] gateway stats error: {e}")
        return stats

    def get_circuit_breaker_status(
        self, tool_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        ✅ v0.6.0: Estado de circuit breakers.
        Si tool_name es None retorna todos. Si se especifica, retorna solo ese.
        """
        if tool_name:
            return self._cb_registry.get(tool_name).get_status()
        return self._cb_registry.get_all_statuses()

    def reset_circuit_breaker(
        self, tool_name: Optional[str] = None
    ) -> bool:
        """
        ✅ v0.6.0: Reset manual de circuit breaker(s).
        Si tool_name es None resetea todos. Retorna True si existía.
        """
        if tool_name:
            result = self._cb_registry.reset(tool_name)
            if result:
                logger.info(
                    f"[Executor] CircuitBreaker reset manual: '{tool_name}'"
                )
            return result
        self._cb_registry.reset_all()
        logger.info("[Executor] Todos los CircuitBreakers reseteados")
        return True

    def clear_metrics(self) -> None:
        """Limpia métricas de ejecución."""
        self._metrics.clear()
        logger.debug("[Executor] Métricas limpiadas")


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================

_executor: Optional[ToolExecutor] = None
_executor_lock = threading.Lock()


def get_executor() -> ToolExecutor:
    """
    Retorna la instancia singleton del ToolExecutor.

    Thread-safe. Usa el ToolRegistry singleton por defecto.

    Uso desde agentes:
        from src.tools_mcp.tool_executor import get_executor, ExecutionContext
        from src.tools_mcp.tool_base import ToolPermission

        executor = get_executor()
        ctx = ExecutionContext(
            session_permission=ToolPermission.EXECUTE,
            timeout_seconds=30.0,
            caller="CoderAgent",
            resource_path="src/core/dispatcher.py",  # ✅ v0.5.0
            risk_score=0.15,                          # ✅ v0.5.0
        )
        result = await executor.run(
            "terminal_exec", ctx, command="pytest tests/"
        )
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ToolExecutor()
    return _executor
