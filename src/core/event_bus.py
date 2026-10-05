#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — EventBus: bus de eventos síncrono con aislamiento
# Ruta: src/core/event_bus.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 3 plan_harness — Extensibilidad):
#   Desacoplar productores de eventos (ToolExecutor, PromotionGate,
#   Dispatcher) de los consumidores (SecurityListener, auditoría, UI).
#   Un listener NUNCA puede romper al publicador: los errores de un
#   listener se aíslan y se registran.
#
# Diseño (referencia harness event-producer-consumer):
#   - publish() es síncrono y NO bloqueante por diseño (los listeners
#     deben ser ligeros; trabajo pesado → thread propio del listener).
#   - subscribe() retorna un disposer para desuscribirse.
#   - Los listeners se invocan en orden de suscripción.
#
# Uso:
#   bus = get_event_bus()
#   disposer = bus.subscribe(EventTypes.TOOL_DENIED, mi_listener)
#   bus.publish(AxiomaEvent(event_type=EventTypes.TOOL_DENIED, data={...}))
#   disposer()  # desuscribir
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.domain.events import AxiomaEvent, EventTypes

logger = logging.getLogger(__name__)

# Listener: Callable[[AxiomaEvent], None]
EventListener = Callable[[AxiomaEvent], None]


class EventBus:
    """
    Bus de eventos síncrono con aislamiento de errores.

    Thread-safe: publish() y subscribe() usan el mismo RLock, y los
    listeners se invocan FUERA del lock (un listener que publique
    eventos no se deadlockea).
    """

    def __init__(self) -> None:
        self._listeners: Dict[str, List[EventListener]] = {}
        self._lock = threading.RLock()
        self._published = 0
        self._listener_errors = 0
        self._created_at = time.time()

    # ── Suscripción ──────────────────────────────────────────────────────────

    def subscribe(self, event_type: str, listener: EventListener) -> Callable[[], None]:
        """
        Registra un listener para un tipo de evento.

        Returns:
            Disposer: función que desuscribe al listener.
        """
        with self._lock:
            self._listeners.setdefault(event_type, []).append(listener)
        return lambda: self.unsubscribe(event_type, listener)

    def unsubscribe(self, event_type: str, listener: EventListener) -> None:
        with self._lock:
            bucket = self._listeners.get(event_type)
            if not bucket:
                return
            try:
                bucket.remove(listener)
            except ValueError as e:
                # AUD-SIL-029: el listener ya no estaba (race de
                # desuscripción) — esperado e inofensivo.
                logger.debug(f"[EventBus] unsubscribe race: {e}")
            if not bucket:
                self._listeners.pop(event_type, None)

    # ── Publicación ──────────────────────────────────────────────────────────

    def publish(self, event: AxiomaEvent) -> None:
        """
        Distribuye el evento a los listeners suscritos.

        Aislamiento: un listener que lanza excepción NO corta la
        distribución a los demás, ni propaga al publicador. Se cuenta
        como listener_error para stats.
        """
        if not event.event_id:
            event.event_id = uuid.uuid4().hex[:12]
        with self._lock:
            listeners = list(self._listeners.get(event.event_type, ()))
        if not listeners:
            return
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:  # noqa: BLE001 — aislamiento deliberado
                self._listener_errors += 1
                logger.warning(
                    f"[EventBus] Listener error en '{event.event_type}': {e}"
                )
        self._published += 1

    def publish_simple(self, event_type: str, data: Dict[str, Any],
                       source: str = "system") -> None:
        """Atajo: construye y publica un AxiomaEvent."""
        self.publish(AxiomaEvent(event_type=event_type, data=data, source=source))

    # ── Stats ────────────────────────────────────────────────────────────────

    def listener_count(self, event_type: Optional[str] = None) -> int:
        with self._lock:
            if event_type is not None:
                return len(self._listeners.get(event_type, ()))
            return sum(len(v) for v in self._listeners.values())

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "listeners": {k: len(v) for k, v in self._listeners.items()},
                "total_listeners": sum(len(v) for v in self._listeners.values()),
                "published": self._published,
                "listener_errors": self._listener_errors,
                "uptime_s": round(time.time() - self._created_at, 1),
            }


# ============================================================================
# SINGLETON — get_event_bus()
# ============================================================================

_instance: Optional[EventBus] = None
_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Singleton del bus de eventos (thread-safe, lazy)."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = EventBus()
        return _instance
