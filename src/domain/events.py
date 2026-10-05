#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Eventos del dominio (FASE 3 plan_harness — Extensibilidad)
# Ruta: src/domain/events.py
# Autor: SaintWick
# Fecha: 2026-08-26
#
# Tipos de evento canónicos + dataclass AxiomaEvent que transporta el
# EventBus (src/core/event_bus.py). Inspirado en el event-producer-
# consumer del harness de referencia: eventos tipados, log-only cuando
# aplica, y listeners que nunca pueden romper al publicador.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict


class EventTypes:
    """Vocabulario cerrado de eventos del sistema (strings estables)."""
    TOOL_CALLED = "tool.called"              # Tool invocada (pre-ejecución)
    TOOL_RESULT = "tool.result"              # Tool completada (éxito/fallo)
    TOOL_DENIED = "tool.denied"              # Tool bloqueada (permisos/modo/circuit)
    CIRCUIT_OPEN = "circuit.open"            # CircuitBreaker abierto
    FILE_OPERATION = "file.operation"        # Operación de archivo auditada
    SANDBOX_UNAVAILABLE = "sandbox.unavailable"  # Fail-closed del sandbox
    MODE_SWITCH = "mode.switch"              # Cambio de RuntimeMode
    DISPATCH_STARTED = "dispatch.started"    # Request entrante al Dispatcher
    SYSTEM = "system"                        # Evento general de sistema

    # Eventos que el SecurityListener observa por defecto
    SECURITY_RELEVANT = frozenset({
        TOOL_DENIED,
        CIRCUIT_OPEN,
        FILE_OPERATION,
        SANDBOX_UNAVAILABLE,
        MODE_SWITCH,
    })


@dataclass
class AxiomaEvent:
    """
    Evento tipado del bus. Los listeners reciben SIEMPRE esta dataclass.

    Atributos:
        event_type : EventTypes (string estable, serializable).
        data       : payload libre del productor.
        source     : módulo/componente que emitió el evento.
        created_at : epoch seconds (monotónico por evento).
        event_id   : uuid corto para trazabilidad.
    """
    event_type: str
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    created_at: float = field(default_factory=time.time)
    event_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "data": self.data,
            "source": self.source,
            "created_at": self.created_at,
            "event_id": self.event_id,
        }
