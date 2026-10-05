#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.8.0 — Alert Manager: Evaluador de Umbrales
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/monitoring/alert_manager.py
# Autor: SaintWick
# Fecha: 2026-04-09
# Propósito: Evalúa métricas del sistema contra umbrales configurables,
#            emite alertas y las persiste via DetailedLogger.
#            Integrado con SelfHealingMonitor y DistributedQueue.
#            Sin dependencias externas. Thread-safe.
# ═══════════════════════════════════════════════════════════════
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_alert(message: str, level: str = "WARNING", extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(category="ALERT_MANAGER", message=message,
                          level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 alert_manager.py:41] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ENUMS Y DATACLASSES
# ============================================================================
class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class AlertState(str, Enum):
    ACTIVE   = "active"
    RESOLVED = "resolved"


@dataclass
class AlertRule:
    """Regla de evaluación: nombre, condición, severidad y cooldown."""
    name: str
    condition: Callable[[Dict[str, Any]], bool]
    severity: AlertSeverity
    message_template: str
    cooldown_seconds: float = 60.0


@dataclass
class Alert:
    """Instancia de alerta disparada."""
    rule_name: str
    severity: AlertSeverity
    message: str
    triggered_at: float = field(default_factory=time.time)
    state: AlertState = AlertState.ACTIVE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def resolve(self) -> None:
        self.state = AlertState.RESOLVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "message": self.message,
            "triggered_at": self.triggered_at,
            "state": self.state.value,
            "metadata": self.metadata,
        }


# ============================================================================
# ALERT MANAGER
# ============================================================================
class AlertManager:
    """
    Evalúa métricas del sistema contra reglas configurables.

    Flujo: `evaluate(metrics)` → evalúa todas las reglas →
    dispara alertas si se cumplen condiciones → persiste via DetailedLogger.

    Reglas por defecto:
    - error_rate_high    → error_rate > 0.15 (WARNING)
    - error_rate_critical → error_rate > 0.30 (CRITICAL)
    - p95_latency_high   → p95_latency_ms > 8000 (WARNING)
    - p95_latency_critical → p95_latency_ms > 15000 (CRITICAL)
    - queue_saturation   → pending_total > 400 (WARNING)
    - handler_degraded   → cualquier handler en DEGRADED/CRITICAL (WARNING)

    Thread-safe. Cooldown por regla para evitar spam.
    """

    def __init__(
        self,
        error_rate_warning: float = 0.15,
        error_rate_critical: float = 0.30,
        p95_warning_ms: float = 8000.0,
        p95_critical_ms: float = 15000.0,
        queue_saturation_threshold: int = 400,
        max_alert_history: int = 200,
    ):
        self._lock = threading.Lock()
        self._active_alerts: Dict[str, Alert] = {}
        self._alert_history: deque = deque(maxlen=max_alert_history)
        self._last_triggered: Dict[str, float] = {}

        # Construir reglas por defecto con los umbrales recibidos
        self._rules: List[AlertRule] = self._build_default_rules(
            error_rate_warning, error_rate_critical,
            p95_warning_ms, p95_critical_ms,
            queue_saturation_threshold,
        )

        logger.info(
            f"[ALERT_MANAGER] Inicializado: {len(self._rules)} reglas, "
            f"err_warn={error_rate_warning:.0%}, p95_warn={p95_warning_ms:.0f}ms"
        )
        _log_alert("AlertManager inicializado", level="INFO", extra={
            "rules": [r.name for r in self._rules],
        })

    # ── Reglas por defecto ────────────────────────────────────────────────
    def _build_default_rules(
        self, err_warn: float, err_crit: float,
        p95_warn: float, p95_crit: float,
        queue_sat: int,
    ) -> List[AlertRule]:
        return [
            AlertRule(
                name="error_rate_warning",
                condition=lambda m: m.get("error_rate", 0.0) > err_warn,
                severity=AlertSeverity.WARNING,
                message_template="Error rate elevado: {error_rate:.1%} (umbral {threshold:.1%})",
                cooldown_seconds=120.0,
            ),
            AlertRule(
                name="error_rate_critical",
                condition=lambda m: m.get("error_rate", 0.0) > err_crit,
                severity=AlertSeverity.CRITICAL,
                message_template="Error rate CRÍTICO: {error_rate:.1%} (umbral {threshold:.1%})",
                cooldown_seconds=60.0,
            ),
            AlertRule(
                name="p95_latency_warning",
                condition=lambda m: m.get("p95_latency_ms", 0.0) > p95_warn,
                severity=AlertSeverity.WARNING,
                message_template="P95 latencia alta: {p95_latency_ms:.0f}ms (umbral {threshold:.0f}ms)",
                cooldown_seconds=120.0,
            ),
            AlertRule(
                name="p95_latency_critical",
                condition=lambda m: m.get("p95_latency_ms", 0.0) > p95_crit,
                severity=AlertSeverity.CRITICAL,
                message_template="P95 latencia CRÍTICA: {p95_latency_ms:.0f}ms (umbral {threshold:.0f}ms)",
                cooldown_seconds=60.0,
            ),
            AlertRule(
                name="queue_saturation",
                condition=lambda m: m.get("queue_pending_total", 0) > queue_sat,
                severity=AlertSeverity.WARNING,
                message_template="Cola saturada: {queue_pending_total} tareas pendientes",
                cooldown_seconds=180.0,
            ),
            AlertRule(
                name="handler_degraded",
                condition=lambda m: any(
                    h.get("status") in ("degraded", "critical")
                    for h in m.get("handler_statuses", {}).values()
                ),
                severity=AlertSeverity.WARNING,
                message_template="Handler(s) degradado(s) detectado(s)",
                cooldown_seconds=120.0,
            ),
        ]

    # ── API pública ───────────────────────────────────────────────────────
    def add_rule(self, rule: AlertRule) -> None:
        """Agrega una regla personalizada."""
        with self._lock:
            self._rules.append(rule)
            logger.debug(f"[ALERT_MANAGER] Regla agregada: {rule.name}")

    def evaluate(self, metrics: Dict[str, Any]) -> List[Alert]:
        """
        Evalúa todas las reglas contra el snapshot de métricas.

        Args:
            metrics: Dict con claves esperadas:
                - error_rate: float (0.0-1.0)
                - p95_latency_ms: float
                - queue_pending_total: int
                - handler_statuses: Dict[str, Dict] (de SelfHealingMonitor.get_all_statuses())

        Returns:
            Lista de alertas nuevas disparadas en esta evaluación
        """
        now = time.time()
        new_alerts: List[Alert] = []

        with self._lock:
            for rule in self._rules:
                # Cooldown check
                last = self._last_triggered.get(rule.name, 0.0)
                if now - last < rule.cooldown_seconds:
                    continue

                try:
                    triggered = rule.condition(metrics)
                except Exception as e:
                    log_degraded(logger, e, "src/monitoring/alert_manager.py:evaluate")
                    continue

                if triggered:
                    # Formatear mensaje con datos disponibles
                    try:
                        msg = rule.message_template.format(
                            threshold=0.0,  # default
                            **metrics,
                        )
                    except (KeyError, ValueError):
                        msg = rule.message_template

                    alert = Alert(
                        rule_name=rule.name,
                        severity=rule.severity,
                        message=msg,
                        metadata={"metrics_snapshot": {
                            k: v for k, v in metrics.items()
                            if isinstance(v, (int, float, str, bool))
                        }},
                    )
                    self._active_alerts[rule.name] = alert
                    self._alert_history.append(alert)
                    self._last_triggered[rule.name] = now
                    new_alerts.append(alert)

                    log_level = "CRITICAL" if alert.severity == AlertSeverity.CRITICAL else "WARNING"
                    _log_alert(f"ALERTA [{alert.severity.value.upper()}]: {msg}",
                               level=log_level, extra=alert.metadata)
                    logger.warning(f"[ALERT_MANAGER] {alert.severity.value.upper()}: {msg}")

                else:
                    # Resolver alerta activa si la condición ya no se cumple
                    if rule.name in self._active_alerts:
                        self._active_alerts[rule.name].resolve()
                        del self._active_alerts[rule.name]
                        _log_alert(f"Alerta resuelta: {rule.name}", level="INFO")

        return new_alerts

    def get_active_alerts(self) -> List[Alert]:
        """Retorna alertas actualmente activas."""
        with self._lock:
            return list(self._active_alerts.values())

    def get_alert_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retorna historial de alertas (más reciente primero)."""
        with self._lock:
            history = list(self._alert_history)
        history.reverse()
        return [a.to_dict() for a in history[:limit]]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            active = list(self._active_alerts.values())
            return {
                "active_alerts": len(active),
                "active_by_severity": {
                    s.value: sum(1 for a in active if a.severity == s)
                    for s in AlertSeverity
                },
                "total_triggered": len(self._alert_history),
                "rules_count": len(self._rules),
            }

    def clear_active(self) -> None:
        """Resuelve todas las alertas activas. Para tests."""
        with self._lock:
            for alert in self._active_alerts.values():
                alert.resolve()
            self._active_alerts.clear()


# ============================================================================
# HELPER: construir métricas desde el sistema real
# ============================================================================
def collect_system_metrics() -> Dict[str, Any]:
    """
    Recolecta métricas del sistema para pasar a AlertManager.evaluate().
    Import seguro de todos los componentes — nunca lanza excepción.
    """
    metrics: Dict[str, Any] = {
        "error_rate": 0.0,
        "p95_latency_ms": 0.0,
        "queue_pending_total": 0,
        "handler_statuses": {},
    }

    # SelfHealingMonitor
    try:
        from src.core.self_healing import get_monitor
        monitor = get_monitor()
        statuses = monitor.get_all_statuses()
        metrics["handler_statuses"] = statuses
        # Calcular error_rate y p95 globales como promedio de handlers
        if statuses:
            rates = [v.get("error_rate", 0.0) for v in statuses.values()]
            p95s  = [v.get("p95_latency_ms", 0.0) for v in statuses.values()]
            metrics["error_rate"] = round(sum(rates) / len(rates), 4)
            metrics["p95_latency_ms"] = round(max(p95s), 2)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 alert_manager.py:331] excepción degradada (intencional): {_d2e}")

    # DistributedQueue
    try:
        from src.core.distributed_queue import get_distributed_queue
        dq = get_distributed_queue()
        total_pending = sum(
            dq.pending_count(p) for p in ("fast", "medium", "slow", "default")
        )
        metrics["queue_pending_total"] = total_pending
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 alert_manager.py:342] excepción degradada (intencional): {_d2e}")

    return metrics


# ============================================================================
# SINGLETON
# ============================================================================
_am_instance: Optional[AlertManager] = None
_am_lock = threading.Lock()


def get_alert_manager() -> AlertManager:
    """Singleton thread-safe de AlertManager."""
    global _am_instance
    if _am_instance is None:
        with _am_lock:
            if _am_instance is None:
                try:
                    from config.settings import settings
                    _am_instance = AlertManager(
                        error_rate_warning=getattr(settings, "alert_error_rate_warning", 0.15),
                        error_rate_critical=getattr(settings, "alert_error_rate_critical", 0.30),
                        p95_warning_ms=getattr(settings, "alert_p95_warning_ms", 8000.0),
                        p95_critical_ms=getattr(settings, "alert_p95_critical_ms", 15000.0),
                    )
                except Exception:
                    _am_instance = AlertManager()
    return _am_instance


def reset_alert_manager() -> None:
    """Solo para tests."""
    global _am_instance
    with _am_lock:
        _am_instance = None
