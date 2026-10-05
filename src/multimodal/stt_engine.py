#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v2026 — STT Engine (Speech-to-Text) [A1] — transcribe_partial v1.1.0
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/multimodal/stt_engine.py
# Autor: SaintWick
# Fecha: 2026-04-18
# ✅ [A1]: TranscriptionResult.is_final + STTEngine.transcribe_partial()
# ✅ transcribe_partial() v1.1.0: re-transcribe el buffer creciente en cada
#   yield de audio_gen (VADFilter.listen_incremental) en vez de esperar a
#   que el generador termine para hacer una sola pasada — streaming real,
#   no pseudo-streaming de una grabación ya completa. Ver docstring del
#   método para el detalle del cambio y el tradeoff de costo de CPU.
#
# Propósito: Transcripción de audio a texto usando faster-whisper en CPU.
#            Modelo configurable (base/small/medium). Default: small (~244MB).
#            Confidence gate: transcripciones < 0.6 → piden repetición, nunca ejecutan.
#
# Criterio de aceptación:
#   - 0% ejecuciones sobre transcripciones ambiguas (confidence < threshold)
#   - Normalización de rutas y símbolos antes de retornar
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, List, Optional

import numpy as np
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None  # type: ignore[assignment,misc]
    _WHISPER_AVAILABLE = False


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

@dataclass
class STTConfig:
    """Configuración del motor STT."""
    model_size: str = "small"          # base|small|medium — large muy lento en CPU
    language: str = "es"               # Español por defecto
    beam_size: int = 1                 # ✅ plan_rafael: 1 (greedy) — 5 saturaba CPU en
                                       # transcribe_partial (streaming re-transcribe todo)
    temperature: float = 0.0           # Determinista
    confidence_threshold: float = 0.5  # ✅ plan_rafael: 0.5 — ÚNICA fuente:
                                       # settings.jarvis_voice_confidence_threshold
    sample_rate: int = 16000
    # CPU: int8 cuantizado — 2-3x más rápido sin pérdida significativa
    compute_type: str = "int8"
    device: str = "cpu"
    # Número de threads para CPU — None = auto (todos los cores)
    cpu_threads: int = 4
    # Suprimir tokens especiales del output
    suppress_blank: bool = True
    no_speech_threshold: float = 0.5   # ✅ plan_rafael: alineado con confidence_threshold


# ============================================================================
# TRANSCRIPTION RESULT
# ============================================================================

@dataclass
class TranscriptionResult:
    """Resultado de transcripción STT."""
    text: str                          # Texto transcripto normalizado
    confidence: float                  # Score promedio de los segmentos
    language: str = "es"
    duration_seconds: float = 0.0
    latency_ms: float = 0.0
    below_threshold: bool = False      # True → no ejecutar, pedir repetición
    is_final: bool = True              # [A1]: False = resultado parcial del stream
    raw_segments: List[dict] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        """True si la transcripción tiene confianza suficiente para ejecutar."""
        return not self.below_threshold and len(self.text.strip()) > 0

    @property
    def needs_repeat(self) -> bool:
        """True si el sistema debe pedir al usuario que repita."""
        return self.below_threshold or len(self.text.strip()) == 0


# ============================================================================
# POST-NORMALIZER
# ============================================================================

class _TextNormalizer:
    """
    Normaliza el texto transcripto antes de enviarlo al Dispatcher.
    Convierte rutas, símbolos y terminología técnica a texto legible.
    Evita que el TTS pronuncie símbolos raros.
    """

    # Patrones de normalización para pronunciación correcta en TTS
    _PATH_PATTERN = re.compile(r'(/[\w/\.\-_]+)')
    _URL_PATTERN = re.compile(r'https?://\S+')
    _CODE_PATTERN = re.compile(r'`[^`]+`')

    @classmethod
    def normalize_for_dispatch(cls, text: str) -> str:
        """
        Normaliza texto para enviarlo al Dispatcher.
        Preserva rutas y código — el Dispatcher los necesita tal cual.
        Solo limpia artefactos de transcripción.
        """
        # Remover tokens especiales de whisper
        text = re.sub(r'\[.*?\]', '', text)
        text = re.sub(r'\(.*?\)', '', text)
        # Limpiar espacios múltiples
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @classmethod
    def normalize_for_tts(cls, text: str) -> str:
        """
        Normaliza texto para pronunciación TTS.
        Convierte rutas y código a formas pronunciables.
        """
        # Rutas → descripción pronunciable
        text = cls._PATH_PATTERN.sub(
            lambda m: m.group(1).replace('/', ' barra ').replace('.', ' punto '),
            text
        )
        # URLs → omitir
        text = cls._URL_PATTERN.sub('la URL', text)
        # Código inline → omitir backticks
        text = cls._CODE_PATTERN.sub(lambda m: m.group(0)[1:-1], text)
        # Limpiar espacios
        text = re.sub(r'\s+', ' ', text).strip()
        return text


# ============================================================================
# STT ENGINE
# ============================================================================

def normalizar_audio(audio, objetivo: float = 0.7, piso_rms: float = 0.0005):
    """Normaliza el nivel del audio antes de transcribir (ISSUE-129).

    Motivo MEDIDO: el NIVEL del microfono causo los DOS extremos del problema — al 31 %
    el VAD/STT no oian la voz (turnos vacios: "No escucho nada") y al 85 % el audio
    saturaba (RMS 0,73 · pico 2,04: el VAD veia voz en el 100 % y el turno nunca
    cerraba). Normalizar por pico —con un piso para NO amplificar el silencio— hace que
    el STT funcione igual con cualquier perilla de volumen del sistema.

    Devuelve el audio intacto si viene vacio, si es silencio (RMS < piso) o si ya esta
    en nivel (ganancia entre 0.9 y 1.1).
    """
    try:
        import numpy as _np
        a = _np.asarray(audio, dtype="float32")
        if a.size == 0:
            return audio
        rms = float(_np.sqrt(_np.mean(a ** 2)))
        if rms < piso_rms:
            return audio
        pico = float(_np.max(_np.abs(a)))
        if pico <= 0:
            return audio
        ganancia = objetivo / pico
        if 0.9 <= ganancia <= 1.1:
            return audio
        return _np.clip(a * ganancia, -1.0, 1.0).astype("float32")
    except (TypeError, ValueError) as e:
        # Con `log_degraded` (el auditor marca LOW los handlers silenciosos):
        # si falla, se transcribe SIN normalizar — degradación explícita, no muda.
        log_degraded(logger, e, "stt_engine.normalizar_audio")
        return audio


class STTEngine:
    """
    Motor de transcripción Speech-to-Text usando faster-whisper en CPU.

    Responsabilidades:
        1. Cargar modelo Whisper en memoria (lazy — al primer uso)
        2. Transcribir AudioChunk → TranscriptionResult
        3. Aplicar confidence gate (threshold configurable)
        4. Normalizar texto transcripto

    Uso:
        stt = STTEngine()
        result = stt.transcribe(chunk)
        if result.needs_repeat:
            tts.speak("¿Podés repetir? No entendí bien.")
        elif result.is_actionable:
            dispatcher.process(result.text)
    """

    def __init__(self, config: Optional[STTConfig] = None) -> None:
        self._config = config or STTConfig()
        self._model: Optional[WhisperModel] = None
        self._model_lock = threading.Lock()  # [FIX 1.4] previene doble carga bajo concurrencia
        self._normalizer = _TextNormalizer()
        self._load_stats = {
            "total_transcriptions": 0,
            "below_threshold": 0,
            "avg_confidence": 0.0,
            "avg_latency_ms": 0.0,
        }

        if not _WHISPER_AVAILABLE:
            logger.warning("[STT] faster-whisper no disponible")

    @property
    def is_available(self) -> bool:
        return _WHISPER_AVAILABLE

    def _load_model(self) -> None:
        """Carga el modelo lazy al primer uso. Thread-safe con double-checked locking."""
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:  # re-check dentro del lock
                return
            if not _WHISPER_AVAILABLE:
                raise RuntimeError("[STT] faster-whisper no instalado")
            logger.info(
                f"[STT] Cargando modelo '{self._config.model_size}' "
                f"device={self._config.device} compute={self._config.compute_type}"
            )
            t0 = time.perf_counter()
            self._model = WhisperModel(
                self._config.model_size,
                device=self._config.device,
                compute_type=self._config.compute_type,
                cpu_threads=self._config.cpu_threads,
            )
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(f"[STT] Modelo cargado en {elapsed:.0f}ms")

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """
        Transcribe audio numpy array → TranscriptionResult.

        Args:
            audio: np.ndarray float32 normalizado (-1.0 a 1.0), 16kHz mono.
                   Aceptar también int16 — se convierte internamente.

        Returns:
            TranscriptionResult con texto, confianza y flags de control.
        """
        t0 = time.perf_counter()

        if not _WHISPER_AVAILABLE:
            return TranscriptionResult(
                text="",
                confidence=0.0,
                below_threshold=True,
                latency_ms=0.0,
            )

        # Asegurar float32 normalizado
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # ✅ ISSUE-129: normalizar el nivel (robustez ante el volumen del micrófono).
        audio = normalizar_audio(audio)

        try:
            self._load_model()
        except Exception as e:
            logger.error(f"[STT] Error cargando modelo: {e}")
            return TranscriptionResult(text="", confidence=0.0, below_threshold=True, latency_ms=0.0)

        try:
            segments_iter, info = self._model.transcribe(
                audio,
                language=self._config.language,
                beam_size=self._config.beam_size,
                temperature=self._config.temperature,
                suppress_blank=self._config.suppress_blank,
                no_speech_threshold=self._config.no_speech_threshold,
                vad_filter=False,  # VAD ya aplicado upstream
            )

            segments = list(segments_iter)
            latency_ms = (time.perf_counter() - t0) * 1000

            if not segments:
                logger.debug("[STT] Sin segmentos — audio sin habla")
                return TranscriptionResult(
                    text="",
                    confidence=0.0,
                    language=info.language,
                    below_threshold=True,
                    latency_ms=latency_ms,
                )

            # Calcular confianza promedio desde avg_logprob de segmentos
            # avg_logprob está en rango (-inf, 0) — convertir a probabilidad
            confidences = []
            texts = []
            raw_segs = []

            for seg in segments:
                # avg_logprob: 0=perfecto, -1=~37%, -2=~14%
                # Mapear a [0,1]: exp(avg_logprob) clampado
                prob = min(1.0, max(0.0, 2.718 ** seg.avg_logprob))
                confidences.append(prob)
                texts.append(seg.text)
                raw_segs.append({
                    "text": seg.text,
                    "start": seg.start,
                    "end": seg.end,
                    "confidence": prob,
                    "no_speech_prob": seg.no_speech_prob,
                })

            avg_confidence = sum(confidences) / len(confidences)
            raw_text = " ".join(t.strip() for t in texts)
            normalized_text = self._normalizer.normalize_for_dispatch(raw_text)

            below = avg_confidence < self._config.confidence_threshold

            if below:
                logger.info(
                    f"[STT] Confidence {avg_confidence:.2f} < threshold "
                    f"{self._config.confidence_threshold} — requiere repetición"
                )
            else:
                logger.debug(
                    f"[STT] OK — conf={avg_confidence:.2f} "
                    f"text='{normalized_text[:60]}' latency={latency_ms:.0f}ms"
                )

            # Actualizar stats
            n = self._load_stats["total_transcriptions"]
            self._load_stats["total_transcriptions"] += 1
            self._load_stats["avg_confidence"] = (
                self._load_stats["avg_confidence"] * n + avg_confidence
            ) / (n + 1)
            self._load_stats["avg_latency_ms"] = (
                self._load_stats["avg_latency_ms"] * n + latency_ms
            ) / (n + 1)
            if below:
                self._load_stats["below_threshold"] += 1

            return TranscriptionResult(
                text=normalized_text,
                confidence=avg_confidence,
                language=info.language,
                duration_seconds=segments[-1].end if segments else 0.0,
                latency_ms=latency_ms,
                below_threshold=below,
                raw_segments=raw_segs,
            )

        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            logger.error(f"[STT] Error transcribiendo: {e}")
            return TranscriptionResult(
                text="",
                confidence=0.0,
                below_threshold=True,
                latency_ms=latency_ms,
            )

    async def transcribe_partial(
        self,
        audio_gen,
    ) -> "AsyncGenerator[TranscriptionResult, None]":
        """
        ✅ v1.1.0: Transcripción incremental real. audio_gen es un generador
        async que yieldea (audio_so_far: np.ndarray float32, is_final: bool)
        — típicamente VADFilter.listen_incremental(). audio_so_far es el
        buffer COMPLETO acumulado hasta ese momento (no un delta): se
        re-transcribe entero en cada yield no-final, porque Whisper/
        faster-whisper no tiene modo de "continuar" una transcripción con
        audio nuevo — no hay streaming incremental nativo en el modelo. Esto
        es "redecode de ventana creciente", el approach estándar para
        pseudo-streaming sobre un modelo batch.

        Costo de CPU: cada partial es una pasada COMPLETA desde el inicio de
        la utterance — más frecuentes los yields (ver min_yield_interval en
        listen_incremental()) o más larga la utterance, más costo acumulado.
        Si esto satura CPU, subir min_yield_interval del lado del VAD.

        Buffers cortos (inicio de la utterance) pueden no tener suficiente
        señal para que Whisper devuelva segmentos — eso es esperado en los
        primeros partials, no se yieldea nada en ese caso (salvo que sea
        is_final=True, ahí sí hay que cerrar el ciclo con un resultado vacío).

        ANTES (v1.0.x): recolectaba TODOS los chunks del generador antes de
        transcribir nada, y hacía una sola pasada al final, yieldeando un
        partial por SEGMENTO ya calculado — no había transcripción durante
        la captura, solo pseudo-streaming de un resultado ya terminado
        (el generador que lo alimentaba, _audio_gen() en voice_pipeline.py,
        yieldeaba una sola vez con el audio ya completo de listen_once()).

        PRECONDICIÓN: _WHISPER_AVAILABLE — si no, log WARNING y return.
        Usa asyncio.to_thread() en cada yield (antes, una sola vez para todo
        el generador) para no bloquear el event loop.
        vad_filter=True solo en este método (False en transcribe() batch).
        """
        if not _WHISPER_AVAILABLE:
            logger.warning("[STT] transcribe_partial: faster-whisper no disponible")
            return

        import asyncio
        import time as _time

        try:
            self._load_model()
        except Exception as e:
            logger.error(f"[STT] transcribe_partial: error cargando modelo: {e}")
            return

        def _transcribe_sync(audio):
            segments_iter, info = self._model.transcribe(
                audio,
                language=self._config.language,
                beam_size=self._config.beam_size,
                temperature=self._config.temperature,
                suppress_blank=self._config.suppress_blank,
                no_speech_threshold=self._config.no_speech_threshold,
                vad_filter=True,  # VAD activo solo en streaming
            )
            return list(segments_iter), info

        async for audio_so_far, is_final in audio_gen:
            if audio_so_far is None or len(audio_so_far) == 0:
                if is_final:
                    yield TranscriptionResult(
                        text="", confidence=0.0, below_threshold=True,
                        latency_ms=0.0, is_final=True,
                    )
                    return
                continue

            t0 = _time.perf_counter()
            try:
                # Wrap síncrono → async para no bloquear event loop
                segments, info = await asyncio.to_thread(_transcribe_sync, audio_so_far)
            except Exception as e:
                logger.error(f"[STT] transcribe_partial error: {e}")
                if is_final:
                    yield TranscriptionResult(
                        text="", confidence=0.0, below_threshold=True,
                        latency_ms=0.0, is_final=True,
                    )
                    return
                # Error en un partial no aborta el stream — se reintenta en el próximo yield
                continue

            latency_ms = (_time.perf_counter() - t0) * 1000

            if not segments:
                if is_final:
                    yield TranscriptionResult(
                        text="", confidence=0.0, below_threshold=True,
                        latency_ms=latency_ms, is_final=True,
                    )
                    return
                continue  # sin señal suficiente todavía — normal al inicio de la utterance

            confidences = [min(1.0, max(0.0, 2.718 ** seg.avg_logprob)) for seg in segments]
            avg_conf = sum(confidences) / len(confidences)
            full_text = self._normalizer.normalize_for_dispatch(
                " ".join(seg.text.strip() for seg in segments)
            )

            if not is_final:
                yield TranscriptionResult(
                    text=full_text,
                    confidence=avg_conf,
                    language=info.language,
                    latency_ms=latency_ms,
                    is_final=False,
                )
                continue

            below = avg_conf < self._config.confidence_threshold
            yield TranscriptionResult(
                text=full_text,
                confidence=avg_conf,
                language=info.language,
                duration_seconds=segments[-1].end if segments else 0.0,
                latency_ms=latency_ms,
                below_threshold=below,
                is_final=True,
            )
            return

    def get_stats(self) -> dict:
        return dict(self._load_stats)

    def unload(self) -> None:
        """Libera el modelo de memoria."""
        self._model = None
        logger.info("[STT] Modelo descargado")


# ═══════════════════════════════════════════════════════════════
# 📋 COMPONENTES v0.5.0 (Fase 2.2)
# ═══════════════════════════════════════════════════════════════
# | Componente          | Propósito                                  |
# |---------------------|--------------------------------------------|
# | STTConfig           | model_size, language, confidence_threshold |
# | TranscriptionResult | text, confidence, is_actionable, needs_repeat |
# | _TextNormalizer     | Normaliza para dispatch y TTS              |
# | STTEngine           | Transcripción lazy + confidence gate       |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
