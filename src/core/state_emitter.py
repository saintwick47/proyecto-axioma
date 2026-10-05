#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.4 — State Emitter (IPC Rafael Widget)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/state_emitter.py
# Autor: SaintWick
# Uso:  from src.core.state_emitter import emit_state
#       emit_state("COMPUTING") | emit_state("IDLE") | emit_state("HEALING") | emit_state("WATCHING")
# IPC:  archivo /tmp/.axioma_widget_state — JSON, fire-and-forget.
#       El widget lee este archivo cada 200ms. Sin dependencias externas.
# Cambios v0.6.4 (Rafael Fase 2): + WATCHING — indicador de privacidad
#   obligatorio mientras la cámara está activa.
# ═══════════════════════════════════════════════════════════════
import json
import logging
import time
from pathlib import Path
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

_STATE_FILE = Path("/tmp/.axioma_widget_state")
_VALID_STATES = frozenset({"IDLE", "COMPUTING", "HEALING", "WATCHING"})


def emit_state(state: str) -> None:
    """
    Emite un estado al widget Rafael. Fire-and-forget, nunca lanza excepciones.
    state: "IDLE" | "COMPUTING" | "HEALING" | "WATCHING"
    """
    if state not in _VALID_STATES:
        logger.warning(f"[StateEmitter] Estado inválido ignorado: '{state}'")
        return
    try:
        _STATE_FILE.write_text(
            json.dumps({"state": state, "ts": time.time()}),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug(f"[StateEmitter] No se pudo escribir estado: {e}")


def get_current_state() -> str:
    """
    Lee el estado actual emitido. Retorna 'IDLE' si el archivo no existe o está corrupto.
    """
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        state = data.get("state", "IDLE")
        return state if state in _VALID_STATES else "IDLE"
    except Exception as e:
        log_degraded(logger, e, "src/core/state_emitter.py:get_current_state")
        return "IDLE"
