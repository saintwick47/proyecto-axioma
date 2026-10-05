#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# self_healing.py - Monitor de Auto-Curación (SelfHealingMonitor)
# Ruta: /ruta/a/axioma/src/core/self_healing.py
# Autor: SaintWick
# Versión: 0.5.1
#
# Detecta degradación de handlers por error_rate y latencia p95,
# mantiene ventanas deslizantes de resultados y sugiere acciones
# correctivas automáticas (circuit break, fallback, reducción de carga).
# ═══════════════════════════════════════════════════════════════
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
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
            log.log_event(category="SELF_HEALING", message=message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 self_healing.py:38] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ENUMS
# ============================================================================
class HealthStatus(str, Enum):
    HEALTHY  = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class HealingAction(str, Enum):
    NONE           = "none"
    REDUCE_LOAD    = "reduce_load"
    FALLBACK_MODEL = "fallback_model"
    CIRCUIT_BREAK  = "circuit_break"


# ============================================================================
# DATACLASS POR HANDLER
# ============================================================================
@dataclass
class HandlerHealth:
    """Estado de salud de un handler individual."""
    handler: str
    # Ventana deslizante de latencias (ms)
    _latencies: deque = field(default_factory=lambda: deque(maxlen=100))
    # ✅ FIX FASE 4: Ventana deslizante de resultados (1=éxito, 0=fallo)
    # Reemplaza los enteros infinitos _successes y _failures para evitar
    # promedios históricos de por vida (Sticky Badges) y allow self-healing.
    _results: deque = field(default_factory=lambda: deque(maxlen=100))
    status: HealthStatus = HealthStatus.HEALTHY

    def record(self, latency_ms: float, success: bool) -> None:
        self._latencies.append(latency_ms)
        self._results.append(1 if success else 0)

    @property
    def total(self) -> int:
        return len(self._results)

    @property
    def error_rate(self) -> float:
        if not self._results:
            return 0.0
        failures = sum(1 for r in self._results if r == 0)
        return round(failures / len(self._results), 4)

    @property
    def p95_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        sorted_lats = sorted(self._latencies)
        idx = int(len(sorted_lats) * 0.95)
        idx = min(idx, len(sorted_lats) - 1)
        return round(sorted_lats[idx], 2)

    def to_dict(self) -> Dict[str, Any]:
        successes = sum(1 for r in self._results if r == 1)
        failures = sum(1 for r in self._results if r == 0)
        return {
            "handler": self.handler,
            "status": self.status.value,
            "total": self.total,
            "error_rate": self.error_rate,
            "p95_latency_ms": self.p95_latency_ms,
            "successes": successes,
            "failures": failures,
            "samples": len(self._latencies),
        }


# ============================================================================
# MONITOR PRINCIPAL
# ============================================================================
class SelfHealingMonitor:
    """
    Monitor de salud por handler con sugerencia de acciones correctivas.

    Evalúa: error_rate, p95_latency. Thread-safe.
    """

    def __init__(
        self,
        error_rate_threshold: float = 0.15,
        p95_threshold_ms: float = 8000.0,
        min_samples: int = 10,
    ):
        self.error_rate_threshold = error_rate_threshold
        self.p95_threshold_ms = p95_threshold_ms
        self.min_samples = min_samples
        self._handlers: Dict[str, HandlerHealth] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, handler: str) -> HandlerHealth:
        if handler not in self._handlers:
            self._handlers[handler] = HandlerHealth(handler=handler)
        return self._handlers[handler]

    def check_handler(self, handler: str, latency_ms: float, success: bool) -> HealthStatus:
        """
        Registra métrica y evalúa estado del handler.

        Returns:
            HealthStatus actualizado
        """
        with self._lock:
            h = self._get_or_create(handler)
            h.record(latency_ms, success)

            if h.total < self.min_samples:
                return h.status  # Sin suficientes muestras, no cambiar estado

            prev_status = h.status
            error_rate = h.error_rate
            p95 = h.p95_latency_ms

            # Evaluar estado
            if error_rate > self.error_rate_threshold or p95 > self.p95_threshold_ms:
                if error_rate > self.error_rate_threshold * 2 or p95 > self.p95_threshold_ms * 1.5:
                    h.status = HealthStatus.CRITICAL
                else:
                    h.status = HealthStatus.DEGRADED
            else:
                h.status = HealthStatus.HEALTHY

            if h.status != prev_status:
                _log_event(
                    f"Health transition: {handler} {prev_status.value} → {h.status.value}",
                    level="WARNING" if h.status != HealthStatus.HEALTHY else "INFO",
                    extra={
                        "handler": handler,
                        "error_rate": error_rate,
                        "p95_latency_ms": p95,
                        "total_samples": h.total,
                    },
                )
                logger.warning(
                    f"[SELF_HEALING] {handler}: {prev_status.value} → {h.status.value} "
                    f"(error_rate={error_rate:.2%}, p95={p95:.0f}ms)"
                )

            return h.status

    def get_status(self, handler: str) -> HealthStatus:
        with self._lock:
            h = self._handlers.get(handler)
            return h.status if h else HealthStatus.HEALTHY

    def suggest_action(self, handler: str) -> HealingAction:
        """Sugiere acción correctiva basada en el estado actual."""
        with self._lock:
            h = self._handlers.get(handler)
            if not h or h.total < self.min_samples:
                return HealingAction.NONE

            if h.status == HealthStatus.HEALTHY:
                return HealingAction.NONE

            error_rate = h.error_rate
            p95 = h.p95_latency_ms

            # ✅ FIX FASE 4: Alinear acción con severidad real.
            # CRITICAL siempre escala a acción de alto impacto, sin importar si es LLM o no.
            if h.status == HealthStatus.CRITICAL:
                if "LLM" in handler and p95 > self.p95_threshold_ms:
                    return HealingAction.FALLBACK_MODEL
                return HealingAction.CIRCUIT_BREAK

            # DEGRADED: Acciones intermedias según tipo de handler
            if "LLM" in handler and p95 > self.p95_threshold_ms:
                return HealingAction.FALLBACK_MODEL

            return HealingAction.REDUCE_LOAD

    def get_all_statuses(self) -> Dict[str, Any]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._handlers.items()}

    def reset_handler(self, handler: str) -> None:
        with self._lock:
            if handler in self._handlers:
                del self._handlers[handler]
                logger.info(f"[SELF_HEALING] Handler reset: {handler}")


# ============================================================================
# SINGLETON
# ============================================================================
_monitor_instance: Optional[SelfHealingMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor(
    error_rate_threshold: float = 0.15,
    p95_threshold_ms: float = 8000.0,
    min_samples: int = 10,
) -> SelfHealingMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        with _monitor_lock:
            if _monitor_instance is None:
                _monitor_instance = SelfHealingMonitor(
                    error_rate_threshold=error_rate_threshold,
                    p95_threshold_ms=p95_threshold_ms,
                    min_samples=min_samples,
                )
                logger.info("[SELF_HEALING] SelfHealingMonitor initialized")
    return _monitor_instance


def reset_monitor() -> None:
    """Solo para tests."""
    global _monitor_instance
    with _monitor_lock:
        _monitor_instance = None
