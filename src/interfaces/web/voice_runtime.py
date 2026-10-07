#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/voice_runtime.py
# Autor: SaintWick
# AXIOMA — src/interfaces/web/voice_runtime.py
# Controlador runtime de VoicePipeline para la UI.
# ✅ 2026-08-25: el audio NO debe activarse solo — se activa:
#   1. Al iniciar con settings.jarvis_mode_default en jarvis/voice_only
#   2. Cuando el usuario lo habilita en la UI (Modo Jarvis)
#   3. Al ejecutar Rafael.py (daemon, ruta separada)
# Este módulo conecta el toggle de la UI con el pipeline del servidor.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("axioma.voice_runtime")

_pipeline: Optional[Any] = None
_running: bool = False


def voz_posible() -> bool:
    """¿Se puede usar la voz en este entorno? (medido: dentro del contenedor no hay audio)

    La bandera `AXIOMA_SIN_AUDIO=1` la declara el contenedor porque ahí no hay dispositivos de audio.
    Sin esto, la interfaz ofrecía el botón de voz y el fallo aparecía después, en el medio del intento.
    """
    from config.multimodal_loader import audio_deshabilitado
    return not audio_deshabilitado()


def register(pipeline: Any) -> None:
    """Registra la instancia de VoicePipeline creada por main.py cmd_web."""
    global _pipeline
    _pipeline = pipeline
    if not voz_posible():
        logger.warning("[VoiceRuntime] voz desactivada a propósito (AXIOMA_SIN_AUDIO=1): el pipeline "
                       "queda registrado, pero no se va a usar")
    logger.info("[VoiceRuntime] VoicePipeline registrado")


def set_voice(active: bool) -> bool:
    """
    Activa/desactiva el pipeline de voz de forma segura (idempotente).
    Retorna True si el estado final es 'activo'.
    """
    global _running
    if _pipeline is None:
        logger.warning("[VoiceRuntime] set_voice ignorado — sin pipeline registrado")
        return False
    try:
        if active and not _running:
            _pipeline.start()
            _running = True
            logger.info("[VoiceRuntime] VoicePipeline ACTIVADO (UI/config)")
        elif not active and _running:
            _pipeline.stop()
            _running = False
            logger.info("[VoiceRuntime] VoicePipeline DESACTIVADO (UI/config)")
        return _running
    except Exception as e:  # noqa: BLE001
        logger.error(f"[VoiceRuntime] set_voice falló: {e}")
        return _running


def is_voice_running() -> bool:
    return _running
