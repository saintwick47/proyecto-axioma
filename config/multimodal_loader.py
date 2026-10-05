#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Loader de config/multimodal.yaml → dataclasses reales
# Ruta: config/multimodal_loader.py
# Autor: SaintWick
# Versión: 1.0.0
#
# Propósito: rafael_daemon.py instanciaba STTEngine()/VADFilter()/
# WakeWordDetector() sin argumentos — multimodal.yaml no se leía en
# ningún punto del sistema (ni vad_filter.py, ni stt_engine.py, ni
# settings.py, que no tiene campos vad_*/stt_* pese a que
# test_audio_diagnostic.py los busca vía getattr con default,
# ocultando el problema en vez de fallar). Este loader parsea el
# yaml real y arma VADConfig/STTConfig; audio.input_device se expone
# aparte porque WakeWordDetector no tiene un config propio.
# Si el yaml falta o no parsea: log de warning/error y fallback a los
# defaults hardcodeados de cada dataclass — mismo comportamiento que
# había hasta ahora, ahora con visibilidad real en el log.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import yaml

from config.paths import Paths
from src.multimodal.stt_engine import STTConfig
from src.multimodal.vad_filter import VADConfig
from src.multimodal.tts_engine import TTSConfig

logger = logging.getLogger(__name__)

_MULTIMODAL_YAML_PATH = Paths.CONFIG / "multimodal.yaml"

# Whitelist explícita: solo estos campos del yaml se pasan a cada
# dataclass. Evita que una clave nueva/mal escrita en el yaml rompa
# el constructor con un TypeError por kwarg inesperado.
_VAD_FIELDS = {
    "sample_rate", "frame_ms", "aggressiveness", "silence_frames_end",
    "voice_frames_min", "ring_buffer_frames", "max_utterance_seconds", "backend",
    "min_yield_interval",  # ✅ plan_rafael (2026-08-31)
}
_STT_FIELDS = {
    "model_size", "language", "beam_size", "temperature", "confidence_threshold",
    "sample_rate", "compute_type", "device", "cpu_threads", "suppress_blank",
    "no_speech_threshold",
}
_TTS_FIELDS = {
    "voice", "fallback_voice", "rate", "volume", "pitch",
    "play_audio", "timeout_seconds", "piper_model",
}


def _load_yaml() -> Dict[str, Any]:
    """Lee y parsea multimodal.yaml. Devuelve {} (con log) si falta o no parsea."""
    if not _MULTIMODAL_YAML_PATH.exists():
        logger.warning(
            f"[MultimodalLoader] {_MULTIMODAL_YAML_PATH} no existe — "
            "usando defaults hardcodeados de VADConfig/STTConfig"
        )
        return {}
    try:
        with open(_MULTIMODAL_YAML_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}
    except Exception as e:
        logger.error(
            f"[MultimodalLoader] Error parseando {_MULTIMODAL_YAML_PATH}: {e} — "
            "usando defaults hardcodeados de VADConfig/STTConfig"
        )
        return {}


def build_vad_config() -> VADConfig:
    """Arma VADConfig real desde multimodal.yaml (sección vad: + audio.input_device)."""
    raw = _load_yaml()
    vad_raw = dict(raw.get("vad") or {})
    audio_raw = dict(raw.get("audio") or {})
    kwargs = {k: v for k, v in vad_raw.items() if k in _VAD_FIELDS}
    cfg = VADConfig(**kwargs)
    # Un solo lugar decide el dispositivo: `get_input_device()` (y así vale el atajo por variable)
    cfg.device = get_input_device()
    del audio_raw
    return cfg


def build_stt_config() -> STTConfig:
    """
    Arma STTConfig real desde multimodal.yaml (sección stt:).
    No toca audio.input_device — STTEngine transcribe arrays numpy ya
    capturados, no abre su propio sd.InputStream.

    ✅ plan_rafael FASE 1 (2026-08-31): el umbral de confianza tiene UNA
    sola fuente — settings.jarvis_voice_confidence_threshold (0.5) — y pisa
    el valor del yaml (que quedaba documental). Antes divergían: yaml 0.5,
    dataclass 0.6, voice_pipeline hardcode 0.35.
    """
    raw = _load_yaml()
    stt_raw = dict(raw.get("stt") or {})
    kwargs = {k: v for k, v in stt_raw.items() if k in _STT_FIELDS}
    try:
        from config.settings import settings
        kwargs["confidence_threshold"] = float(
            getattr(settings, "jarvis_voice_confidence_threshold", 0.5)
        )
    except Exception:
        kwargs.setdefault("confidence_threshold", 0.5)
    return STTConfig(**kwargs)


def build_tts_config() -> TTSConfig:
    """Arma TTSConfig real: multimodal.yaml + **override de `settings` (`.env`)**.

    ✅ 2026-10-01 (ISSUE-122): la VOZ de RAFAEL sale de acá (no del CLI), y estaba
    hardcodeada en el YAML como `es-ES-AlvaroNeural` + Piper `es_ES-davefx` ⇒ español
    de ESPAÑA. Ahora `settings` (que SÍ lee `.env`) manda sobre el YAML para voz,
    prosodia y modelo de Piper — mismo criterio que `confidence_threshold`
    (`jarvis_voice_confidence_threshold`), donde ya se decidió que haya UNA fuente.
    Defaults: Piper `es_AR-daniela-high` (argentina, offline) y edge-tts
    `es-AR-TomasNeural` como fallback.
    """
    raw = _load_yaml()
    tts_raw = dict(raw.get("tts") or {})
    kwargs = {k: v for k, v in tts_raw.items() if k in _TTS_FIELDS}

    from config.settings import settings
    overrides = {
        "voice": getattr(settings, "tts_voice", None),
        "fallback_voice": getattr(settings, "tts_fallback_voice", None),
        "rate": getattr(settings, "tts_rate", None),
        "pitch": getattr(settings, "tts_pitch", None),
        "volume": getattr(settings, "tts_volume", None),
        "piper_model": getattr(settings, "piper_model", None),
    }
    for clave, valor in overrides.items():
        if valor:
            kwargs[clave] = valor
    # ✅ ISSUE-147 (MEDIDO): `piper_model` es una ruta RELATIVA
    # ("data/piper/es_AR-daniela-high.onnx"), así que se resolvía contra la carpeta de
    # trabajo: arrancando desde otro lado, `PiperVoice.load()` fallaba y la voz caía al
    # respaldo **sin error visible** (el except sólo deja un aviso). Se resuelve contra la
    # raíz del proyecto; una ruta absoluta se respeta tal cual (compatibilidad).
    if kwargs.get("piper_model"):
        from config.paths import Paths
        kwargs["piper_model"] = str(Paths.resolve(kwargs["piper_model"]))
    return TTSConfig(**kwargs)


def get_input_device() -> Optional[str]:
    """Dispositivo de entrada de audio (VAD + detector de la frase de activación).

    ✅ `ISSUE-150` (MEDIDO 2026-10-05): el yaml dice `pulse` (el complemento de ALSA de
    PulseAudio/PipeWire que existe en el equipo) y ese nombre **no existe dentro de un contenedor**:
    medido, el demonio arrancaba pero el lazo de escucha fallaba en bucle con
    `No input device matching 'pulse'`. Se puede sobrescribir con `AXIOMA_INPUT_DEVICE` (por ejemplo
    `default`, o el nombre/índice que liste `python -c "import sounddevice;
    print(sounddevice.query_devices())"`) sin tocar el archivo del equipo.
    """
    import os

    configurado = (os.environ.get("AXIOMA_INPUT_DEVICE")
                   or dict(_load_yaml().get("audio") or {}).get("input_device"))
    if not configurado:
        return None
    # ¿Ese dispositivo existe ACÁ? Si no (caso medido: `pulse` en el equipo y el contenedor sin el
    # complemento de ALSA de PulseAudio), se devuelve None para que la librería use el del sistema
    # en vez de fallar en bucle. Así el mismo archivo sirve en el equipo y en el contenedor.
    try:
        import sounddevice as sd
        if configurado not in [str(d.get("name") or "") for d in sd.query_devices()]:
            logging.getLogger(__name__).warning(
                f"[audio] el dispositivo '{configurado}' no existe acá: se usa el del sistema")
            return None
    except Exception as _sd_err:      # sin la librería, se respeta lo configurado
        logging.getLogger(__name__).debug(f"[audio] no se pudo comprobar el dispositivo: {_sd_err}")
    return configurado


# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
