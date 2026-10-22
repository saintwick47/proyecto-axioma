#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_jarvis.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_jarvis.py
#
# 2026-10-13: dominio **JARVIS MODE ... JARVIS MODE** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
from typing import Literal

from pydantic import BaseModel, AliasChoices, Field


class SettingsJarvis(BaseModel):
    """Dominio JARVIS MODE ... JARVIS MODE extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === JARVIS MODE — v0.4.0 ===
    # ═══════════════════════════════════════════════════════════
    jarvis_mode_default: Literal["normal", "jarvis", "voice_only", "silent"] = Field(
        default="normal",
        description=(
            "Modo por defecto al iniciar el sistema. "
            "'normal' = GUI + voz opcional. "
            "'jarvis' = voz full-duplex + respuestas cortas. "
            "'voice_only' = igual que jarvis, sin salida en GUI. "
            "'silent' = procesa sin TTS (automatizaciones)."
        )
    )
    jarvis_wake_word: str = Field(
        default="hey rafael",
        validation_alias=AliasChoices("AXIOMA_JARVIS_WAKE_WORD", "JARVIS_WAKE_WORD"),
        description="Frase de activación del modo Jarvis. ✅ 2026-10-01 (ISSUE-127): "
                    "default 'hey rafael'. Se verifica por STT sobre la frase; "
                    "`_strip_wake_prefix` acepta TAMBIÉN el nombre solo ('rafael …'), "
                    "porque Whisper puede transcribir 'hey' de varias formas.",
    )
    jarvis_response_max_tokens: int = Field(
        default=150, ge=50, le=512,
        description="Máximo de tokens por respuesta en modo Jarvis (respuestas cortas y directas)"
    )
    jarvis_tts_auto_speak: bool = Field(
        default=True,
        description="Si True, toda respuesta en modo Jarvis se vocaliza automáticamente vía TTS"
    )
    jarvis_voice_confidence_threshold: float = Field(
        default=0.5, ge=0.3, le=0.95,
        description="Umbral mínimo de confidence STT para procesar un comando en modo Jarvis. "
                    "✅ plan_rafael (2026-08-31): ÚNICA fuente del umbral (antes yaml=0.5, "
                    "voice_pipeline hardcode 0.35, settings 0.6 — tres valores divergentes). "
                    "STTConfig y VoicePipeline lo leen de acá.",
    )
    jarvis_proactive_enabled: bool = Field(
        default=False,
        description="✅ plan_rafael (2026-08-31): ProactiveEngine OFF por default — "
                    "Rafael SOLO responde cuando el usuario le habla (chat o voz). "
                    "Si se activa, vuelve a hablar solo (clima/noticias en horarios).",
    )
    jarvis_screen_describe_on_query: bool = Field(
        default=False,
        description=(
            "Si True, al recibir cualquier consulta en modo Jarvis se captura "
            "y describe la pantalla como contexto adicional"
        )
    )
    jarvis_autonomous_task_max_steps: int = Field(
        default=5, ge=1, le=20,
        description="Máximo de pasos en una tarea autónoma encadenada (AUTONOMOUS_TASK)"
    )
    jarvis_autonomous_task_timeout: int = Field(
        default=120, ge=30, le=600,
        description="Timeout global en segundos para tareas autónomas encadenadas"
    )
    jarvis_gui_indicator: bool = Field(
        default=True,
        description="Si True, muestra indicador visual del modo Jarvis en la sidebar"
    )
