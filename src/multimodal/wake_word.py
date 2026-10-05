#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# wake_word.py - Detección offline de activación (WakeWordDetector)
# Ruta: src/multimodal/wake_word.py
# Autor: SaintWick
# Versión: 1.2.0
#
# Detecta wake words en stream de audio continuo usando pvporcupine
# (si disponible) o fallback por umbral de energía. Incluye calibración
# adaptativa del ruido basal y soporte para device de entrada (micrófono).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import os
import time
from typing import Generator, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    import pvporcupine
    _PORCUPINE_AVAILABLE = True
except ImportError:
    _PORCUPINE_AVAILABLE = False

# openWakeWord: modelo "hey_jarvis" incluido en el paquete.
try:
    from openwakeword.model import Model as _OWWModel
    import openwakeword as _oww_pkg
    _OWW_MODEL_PATH = os.path.join(
        os.path.dirname(_oww_pkg.__file__),
        "resources", "models", "hey_jarvis_v0.1.onnx",
    )
    _OWW_AVAILABLE = os.path.exists(_OWW_MODEL_PATH)
except ImportError:
    _OWW_AVAILABLE = False

# ✅ plan_rafael (2026-08-31): el wake word REAL es "rafael" — se verifica por
# STT sobre la frase capturada (voice_pipeline._strip_wake_prefix). La puerta
# de audio es la energía calibrada (detecta "hay habla"). openWakeWord queda
# DISPONIBLE pero desactivado por default: su modelo preentrenado detecta
# "hey jarvis" (frase que ya no usamos) y consumiría capturas al pedo.
# Si algún día se entrena un modelo propio para "rafael", activarlo acá.
_USE_OPENWAKEWORD = False


def _load_oww_model():
    """Carga openWakeWord (hey_jarvis) solo si _USE_OPENWAKEWORD está activo."""
    if not _USE_OPENWAKEWORD or not _OWW_AVAILABLE:
        return None
    try:
        model = _OWWModel(wakeword_model_paths=[_OWW_MODEL_PATH])
        logger.info(f"[WakeWord] openWakeWord cargado ({_OWW_MODEL_PATH})")
        return model
    except Exception as e:
        logger.warning(f"[WakeWord] openWakeWord init error: {e}")
        return None

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except ImportError:
    _SD_AVAILABLE = False

WAKE_WORDS_ES = ["axioma", "hey axioma", "oye axioma"]
_SAMPLE_RATE  = 16000
_FRAME_MS     = 30
_CALIBRATION_SECONDS = 2.0   # duración de medición del ruido basal al inicio
_NOISE_FLOOR_MULTIPLIER = 3.0  # umbral = max(500, ruido_basal × 3)
# ✅ FIX v1.1.0: techo sano para el threshold calculado. El máximo RMS
# matemáticamente posible para PCM int16 es ~32767 (señal constante a
# máxima amplitud — audio real nunca lo alcanza). 8000 deja margen amplio
# para un ambiente genuinamente ruidoso (basal hasta ~2666 con el
# multiplicador ×3) mientras sigue muy por debajo del techo físico, incluso
# después de multiplicar por sensitivity (hasta ~1.5x típico → 12000, todavía
# con sobra respecto a 32767 para que la voz real pueda superarlo.
_MAX_SANE_NOISE_FLOOR = 8000.0


class WakeWordDetector:
    """
    Detecta wake words en stream de audio continuo.
    Estrategia:
      - pvporcupine si disponible (preciso, offline)
      - energy threshold como fallback (heurístico)
    set_speaking(True) durante reproducción TTS evita auto-activación.

    [Fase 4.3] Umbral adaptativo: calibra el ruido basal durante 2s al inicio
    y setea threshold = max(500, ruido_basal × 3). Re-calibra si el entorno cambia.
    """

    # ✅ ISSUE-126: frames CONSECUTIVOS por encima del umbral para declarar wake word.
    # 5 × 30 ms = 150 ms: una palabra los sostiene, un clic de teclado o una tos no.
    # Es constante de CLASE para que el test pueda verificar el default (no solo la lógica).
    FRAMES_SOSTENIDOS = 5

    def __init__(
        self,
        sensitivity: float = 0.5,
        wake_words: Optional[List[str]] = None,
        device: Optional[str] = None,
    ):
        self._sensitivity = sensitivity
        self._wake_words = wake_words or WAKE_WORDS_ES
        # ✅ FIX v1.2.0: dispositivo de entrada (audio.input_device en
        # multimodal.yaml, ej. "pulse"). None = default del sistema.
        self._device = device
        self._frame_samples = int(_SAMPLE_RATE * _FRAME_MS / 1000)
        self._is_speaking = False
        self._noise_floor: float = 500.0        # actualizado por _calibrate()
        self._calibrated: bool = False

        # ✅ plan_rafael (2026-08-31): wake word neuronal offline (openWakeWord).
        # Detección por nombre real ("hey jarvis") en vez del umbral de energía
        # (que con el mic saturado nunca disparaba). Fallback: porcupine si se
        # instala con key, y el umbral de energía solo si nada más hay.
        self._oww = None
        self._oww_threshold = 0.5
        # ✅ ISSUE-126: energía sostenida (un pico de 30 ms ya no alcanza).
        self._frames_sostenidos = self.FRAMES_SOSTENIDOS
        self._frames_sobre_umbral = 0
        # ✅ plan_rafael (2026-08-31): openWakeWord desactivado por default
        # (detecta "hey jarvis", no "rafael") — la puerta real es energía
        # calibrada + verificación del nombre por STT en el pipeline.
        self._oww = _load_oww_model()

        logger.info(
            f"[WakeWord] Init — porcupine={_PORCUPINE_AVAILABLE} "
            f"openwakeword={_OWW_AVAILABLE and self._oww is not None} "
            f"sd={_SD_AVAILABLE} sensitivity={sensitivity} device={device!r}"
        )

    @property
    def is_available(self) -> bool:
        return _SD_AVAILABLE

    def set_speaking(self, speaking: bool) -> None:
        """Marcar si TTS está reproduciendo — evita auto-activación."""
        self._is_speaking = speaking

    def _calibrate(self, capture_buffer_ref: list, frame_bytes: int) -> None:
        """
        [Fase 4.3] Mide energía basal durante _CALIBRATION_SECONDS.
        Setea self._noise_floor = max(500, energia_basal * 3).

        ✅ FIX v1.1.0: clamp a _MAX_SANE_NOISE_FLOOR. El máximo RMS
        matemáticamente posible para PCM int16 es ~32767 (señal constante a
        máxima amplitud, algo que audio real nunca produce). Sin clamp, una
        lectura de calibración anómala (ej. ruido_basal=27008.6 medido en
        producción — 82% del máximo posible, sostenido 2 segundos completos)
        produce threshold=81025.8 y, con sensitivity=0.5, un umbral efectivo
        de 40512.9 — MATEMÁTICAMENTE IMPOSIBLE de superar por cualquier audio
        real. Resultado: wake word nunca se dispara, sin ningún error ni
        log que lo explique — parece que "no escucha" cuando en realidad
        el umbral quedó por encima del techo físico del formato de audio.
        Causa más probable de una lectura así de alta: ganancia de
        micrófono muy alta en el SO (revisar alsamixer/pavucontrol) o
        dispositivo de entrada incorrecto — el clamp evita que el síntoma
        sea "no responde nunca, sin pista alguna", pero NO corrige eso: si
        el input está saturado, la voz real capturada para VAD/Whisper
        también puede llegar distorsionada.
        Usa el capture_buffer que ya está siendo llenado por el callback.
        """
        logger.info("[WakeWord] Calibrando ruido basal...")
        deadline = time.perf_counter() + _CALIBRATION_SECONDS
        energies = []
        while time.perf_counter() < deadline:
            while len(capture_buffer_ref[0]) < frame_bytes:
                if time.perf_counter() > deadline:
                    break
                time.sleep(0.005)
            if len(capture_buffer_ref[0]) >= frame_bytes:
                frame = capture_buffer_ref[0][:frame_bytes]
                capture_buffer_ref[0] = capture_buffer_ref[0][frame_bytes:]
                audio = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                energies.append(float(np.sqrt(np.mean(audio ** 2))))
        if energies:
            basal = sum(energies) / len(energies)
            computed_floor = max(500.0, basal * _NOISE_FLOOR_MULTIPLIER)
            if computed_floor > _MAX_SANE_NOISE_FLOOR:
                logger.warning(
                    f"[WakeWord] Ruido basal={basal:.1f} da threshold="
                    f"{computed_floor:.1f}, por encima del máximo sano "
                    f"({_MAX_SANE_NOISE_FLOOR:.0f}) — el máximo RMS posible "
                    f"para audio real es ~32767. Esto normalmente significa "
                    f"ganancia de micrófono demasiado alta en el SO o "
                    f"dispositivo de entrada incorrecto — revisar "
                    f"alsamixer/pavucontrol. Clampeando a "
                    f"{_MAX_SANE_NOISE_FLOOR:.0f} para que el wake word "
                    f"pueda dispararse, pero la causa de fondo sigue sin "
                    f"corregir y puede afectar también la calidad del audio "
                    f"que recibe VAD/Whisper."
                )
                computed_floor = _MAX_SANE_NOISE_FLOOR
            self._noise_floor = computed_floor
            logger.info(f"[WakeWord] Ruido basal={basal:.1f} → threshold={self._noise_floor:.1f}")
        self._calibrated = True

    def listen_for_wake(self, deadline: Optional[float] = None) -> Generator[bool, None, None]:
        """
        Stream continuo de frames de audio.
        yield True cuando detecta wake word, yield False en frames normales.
        [Fase 4.3] Verifica deadline en loop interno — no bloquea si no hay audio.
        NOTA: engine interno de listen_once() — no es dead code (el detector
        AUD-DC ignora callers intra-archivo; ver axioma_auditor.py v8.1).
        """
        if not _SD_AVAILABLE:
            logger.error("[WakeWord] sounddevice no disponible")
            return

        # Usar lista mutable para que _calibrate pueda modificar el buffer
        capture_buffer_ref = [b""]
        frame_bytes = self._frame_samples * 2  # int16

        def _callback(indata, frames, time_info, status):
            pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            capture_buffer_ref[0] += pcm

        with sd.InputStream(
            samplerate=_SAMPLE_RATE, channels=1,
            dtype="float32", blocksize=self._frame_samples,
            device=self._device,
            callback=_callback,
        ):
            # Calibración al primer uso
            if not self._calibrated:
                self._calibrate(capture_buffer_ref, frame_bytes)

            logger.info(f"[WakeWord] Escuchando wake word (threshold={self._noise_floor:.1f})...")
            while True:
                # [Fase 4.3] Timeout real — verificar deadline en loop de espera
                while len(capture_buffer_ref[0]) < frame_bytes:
                    if deadline is not None and time.perf_counter() > deadline:
                        logger.debug("[WakeWord] Deadline alcanzado — abortando")
                        return
                    time.sleep(0.005)

                frame = capture_buffer_ref[0][:frame_bytes]
                capture_buffer_ref[0] = capture_buffer_ref[0][frame_bytes:]

                if self._is_speaking:
                    yield False
                    continue

                audio = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                energy = float(np.sqrt(np.mean(audio ** 2)))

                detected = False
                if self._oww is not None:
                    # ✅ plan_rafael: wake word neuronal ("hey jarvis").
                    # openWakeWord bufferiza internamente y devuelve {model: score}.
                    try:
                        preds = self._oww.predict(audio)
                        score = float(max(preds.values())) if preds else 0.0
                        detected = score >= self._oww_threshold
                    except Exception:
                        detected = False
                if not detected and self._oww is None:
                    # Fallback: umbral de energía (solo si no hay wake word real).
                    # [Fase 4.3] Umbral adaptativo en lugar de 500.0 fijo
                    # ✅ 2026-10-01 (ISSUE-126): antes bastaba UN frame (30 ms) por encima
                    # del umbral ⇒ un clic de teclado, una tos o un golpe disparaban el
                    # turno. Medido en `logs/rafael.log`: la wake word se disparaba cada
                    # 5-10 s y el pipeline pedía "¿podés repetir?" en bucle. Ahora la
                    # energía debe SOSTENERSE `_frames_sostenidos` frames seguidos
                    # (~150 ms): una palabra dicha los sostiene de sobra, el ruido
                    # impulsivo no.
                    if self._evaluar_frame(energy):
                        detected = True
                        self._frames_sobre_umbral = 0     # rearmar para la próxima
                yield detected

    def _evaluar_frame(self, energy: float) -> bool:
        """¿La energía SOSTENIDA alcanza para declarar wake word? (✅ ISSUE-126).

        Cuenta frames consecutivos por encima del umbral y devuelve True al llegar a
        `_frames_sostenidos`. Se extrajo del bucle de captura para poder testearlo sin
        micrófono (el generador lee de un stream real).
        """
        if energy > (self._noise_floor * self._sensitivity):
            self._frames_sobre_umbral += 1
        else:
            self._frames_sobre_umbral = 0
        return self._frames_sobre_umbral >= self._frames_sostenidos

    def listen_once(self, timeout: float = 5.0) -> bool:
        """
        Espera una sola detección. Útil para tests y pipeline.
        [Fase 4.3] Timeout real via deadline pasado a listen_for_wake.
        """
        deadline = time.perf_counter() + timeout
        for detected in self.listen_for_wake(deadline=deadline):
            if detected:
                return True
        return False
