#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — SecurityListener: observador de eventos de seguridad
# Ruta: src/core/security_listener.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 3 plan_harness — Extensibilidad):
#   Consume los eventos de seguridad del EventBus (denegaciones de
#   tools, circuit open, operaciones de archivo, sandbox no disponible,
#   cambios de modo) y:
#     1. Registra cada uno en el logger estándar + DetailedLogger.
#     2. Reenvía las operaciones de archivo al FileAuditTrail (fusión
#        con la auditoría de FASE 2 — un solo lugar de registro).
#     3. Mantiene contadores para stats y alertas.
#
# Diseño: los handlers son LIGEROS por contrato (el EventBus es
# síncrono); cualquier trabajo pesado debe delegarse a un thread.
# Nunca lanza — el bus ya aísla errores, pero aquí además se
# protege cada handler individualmente.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from src.domain.events import AxiomaEvent, EventTypes
from src.core.event_bus import EventBus, get_event_bus

logger = logging.getLogger(__name__)

# ── Imports opcionales (degradación silenciosa) ─────────────────────────────

try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

try:
    from src.learning.file_audit import get_file_audit
    _FILE_AUDIT_AVAILABLE = True
except ImportError:
    _FILE_AUDIT_AVAILABLE = False


def _detailed_event(event: str, level: str = "WARNING", **extra: Any) -> None:
    """Delega al DetailedLogger si está disponible. Silencioso si no."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = _get_detailed_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event(event, str(extra.get("message", "")),
                          level=level, extra=extra)
    except Exception as e:
        # AUD-SIL-029: DetailedLogger opcional — fallo de logging no debe
        # romper el listener de seguridad.
        logger.debug(f"[SecurityListener] detailed_logger error: {e}")


class SecurityListener:
    """
    Listener de seguridad del EventBus (singleton vía get_security_listener).

    Suscribe handlers a EventTypes.SECURITY_RELEVANT y registra cada
    evento en logger + DetailedLogger + FileAuditTrail (file ops).
    """

    _EVENT_LEVELS = {
        EventTypes.TOOL_DENIED: "WARNING",
        EventTypes.CIRCUIT_OPEN: "WARNING",
        EventTypes.FILE_OPERATION: "INFO",
        EventTypes.SANDBOX_UNAVAILABLE: "CRITICAL",
        EventTypes.MODE_SWITCH: "INFO",
    }

    def __init__(self, event_bus: Optional[EventBus] = None, enabled: bool = True) -> None:
        self._bus = event_bus or get_event_bus()
        self._enabled = enabled
        self._counters: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._disposers = []
        if enabled:
            self._subscribe_all()
        logger.info("[SecurityListener] inicializado (listening)")

    # ── Suscripción ──────────────────────────────────────────────────────────

    def _subscribe_all(self) -> None:
        self._disposers = [
            self._bus.subscribe(etype, self._dispatch)
            for etype in EventTypes.SECURITY_RELEVANT
        ]

    def dispose(self) -> None:
        """Desuscribe todos los handlers (para tests / shutdown)."""
        for disposer in self._disposers:
            try:
                disposer()
            except Exception as e:
                # AUD-SIL-029: disposer roto no debe impedir el resto del
                # cleanup (shutdown/test).
                logger.debug(f"[SecurityListener] disposer error: {e}")
        self._disposers = []
        self._enabled = False

    # ── Dispatch central ─────────────────────────────────────────────────────

    def _dispatch(self, event: AxiomaEvent) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._counters[event.event_type] = (
                self._counters.get(event.event_type, 0) + 1
            )
        handler = getattr(self, f"_on_{event.event_type.replace('.', '_')}", None)
        if handler is not None:
            try:
                handler(event)
            except Exception as e:  # noqa: BLE001 — el bus nunca debe romperse
                logger.warning(f"[SecurityListener] handler error {event.event_type}: {e}")

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_tool_denied(self, event: AxiomaEvent) -> None:
        msg = (
            f"Tool denegada: {event.data.get('tool_name')} "
            f"motivo={event.data.get('reason')} mode={event.data.get('mode')}"
        )
        logger.warning(f"[SecurityListener] {msg}")
        _detailed_event(
            "SECURITY_TOOL_DENIED", level="WARNING",
            message=msg, **event.data,
        )

    def _on_circuit_open(self, event: AxiomaEvent) -> None:
        msg = f"CircuitBreaker abierto: {event.data.get('tool_name')}"
        logger.warning(f"[SecurityListener] {msg}")
        _detailed_event("SECURITY_CIRCUIT_OPEN", level="WARNING",
                        message=msg, **event.data)

    def _on_file_operation(self, event: AxiomaEvent) -> None:
        # Reenvío a la auditoría persistente (FASE 2) — un solo registro.
        if not _FILE_AUDIT_AVAILABLE:
            return
        try:
            audit = get_file_audit()
            if audit is None:
                return
            audit.record(
                session_id=event.data.get("session_id", "default"),
                operation=event.data.get("operation", "unknown"),
                path=event.data.get("path", ""),
                status=event.data.get("status", "proposed"),
                checksum_sha256=event.data.get("checksum_sha256"),
                proposal_id=event.data.get("proposal_id"),
            )
        except Exception as e:
            logger.debug(f"[SecurityListener] file_audit error (non-fatal): {e}")

    def _on_sandbox_unavailable(self, event: AxiomaEvent) -> None:
        msg = (
            f"Sandbox no disponible (fail-closed): "
            f"backend={event.data.get('backend')} msg={event.data.get('message')}"
        )
        logger.critical(f"[SecurityListener] {msg}")
        _detailed_event("SECURITY_SANDBOX_UNAVAILABLE", level="CRITICAL",
                        message=msg, **event.data)

    def _on_mode_switch(self, event: AxiomaEvent) -> None:
        msg = f"RuntimeMode: {event.data.get('from')} → {event.data.get('to')}"
        logger.info(f"[SecurityListener] {msg}")
        _detailed_event("SECURITY_MODE_SWITCH", level="INFO", message=msg, **event.data)

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "events_by_type": dict(self._counters),
                "total_events": sum(self._counters.values()),
            }


# ============================================================================
# SINGLETON — get_security_listener()
# ============================================================================

_instance: Optional[SecurityListener] = None
_lock = threading.Lock()


def get_security_listener(enabled: bool = True) -> SecurityListener:
    """Singleton del SecurityListener (thread-safe, lazy)."""
    global _instance
    with _lock:
        if _instance is None or _instance._enabled != enabled:
            if _instance is not None:
                _instance.dispose()
            _instance = SecurityListener(enabled=enabled)
        return _instance
