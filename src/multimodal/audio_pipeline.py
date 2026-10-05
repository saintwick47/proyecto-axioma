#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# audio_pipeline.py - Orquestador del Pipeline de Voz (VAD → STT → Dispatcher → TTS)
# Ruta: /ruta/a/axioma/src/multimodal/audio_pipeline.py
# Autor: SaintWick
# Versión: 0.6.0
#
# Orquesta el pipeline completo de voz: captura de micrófono,
# detección de voz (VAD), transcripción (STT), confidence gate,
# despacho al Dispatcher y síntesis de voz (TTS). Soporta modo
# continuo y push-to-talk. Delega el core a pipeline_core.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from .stt_engine import STTEngine, STTConfig, TranscriptionResult
from .tts_engine import TTSEngine, TTSConfig
from .vad_filter import VADFilter, VADConfig, AudioChunk
from .pipeline_core import process_utterance_core, UtteranceResult

logger = logging.getLogger(__name__)


# ============================================================================
# PIPELINE RESULT
# ============================================================================

@dataclass
class PipelineResult:
    """Resultado completo de un ciclo del pipeline de voz."""
    transcription: Optional[TranscriptionResult]
    dispatcher_response: Optional[str]
    success: bool
    error: Optional[str] = None
    total_latency_ms: float = 0.0
    # Desglose de latencia
    vad_latency_ms: float = 0.0
    stt_latency_ms: float = 0.0
    dispatch_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0
    # Flags de control
    was_repeated: bool = False          # True → confidence gate activado
    metadata: dict = field(default_factory=dict)


# ============================================================================
# AUDIO PIPELINE
# ============================================================================

class AudioPipeline:
    """
    Pipeline de voz completo para AXIOMA.

    Flujo:
        Micrófono
          → VADFilter (webrtcvad) — filtra ruido, solo pasa voz
          → STTEngine (faster-whisper) — transcripción
          → confidence gate — < threshold → TTS pide repetición
          → post-normalizer — rutas, símbolos → texto legible
          → Dispatcher.process() — procesamiento del comando
          → respuesta
          → TTSEngine.speak() — síntesis y reproducción
          → Micrófono (siguiente utterance)

    Uso:
        pipeline = AudioPipeline(dispatcher=dispatcher)
        await pipeline.start()   # bloquea hasta pipeline.stop()

    O en modo callback:
        pipeline = AudioPipeline(
            dispatcher=dispatcher,
            on_transcription=lambda r: print(r.text),
        )
        await pipeline.run_once()
    """

    def __init__(
        self,
        dispatcher: Optional[Any] = None,
        vad_config: Optional[VADConfig] = None,
        stt_config: Optional[STTConfig] = None,
        tts_config: Optional[TTSConfig] = None,
        on_transcription: Optional[Callable[[TranscriptionResult], None]] = None,
        on_response: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._vad = VADFilter(vad_config)
        self._stt = STTEngine(stt_config)
        self._tts = TTSEngine(tts_config)

        # Callbacks opcionales para integración con UI
        self._on_transcription = on_transcription
        self._on_response = on_response
        self._on_error = on_error

        self._running = False
        self._stop_event = threading.Event()

        self._stats = {
            "cycles": 0,
            "successful": 0,
            "repeated": 0,
            "errors": 0,
            "avg_total_latency_ms": 0.0,
        }

        logger.info(
            f"[Pipeline] Inicializado — "
            f"vad={self._vad.is_available} "
            f"stt={self._stt.is_available} "
            f"tts={self._tts.is_available} "
            f"dispatcher={'sí' if dispatcher else 'no'}"
        )

    @property
    def is_ready(self) -> bool:
        """True si el pipeline puede operar (al menos STT disponible)."""
        return self._stt.is_available

    async def process_audio(self, audio: np.ndarray) -> PipelineResult:
        """
        Procesa un array de audio y retorna PipelineResult.
        Punto de entrada principal para integración con UI o tests.

        Args:
            audio: np.ndarray float32 16kHz mono, o int16 (se convierte).
        """
        t_total = time.perf_counter()

        # ── STT ───────────────────────────────────────────────────────────────
        # ✅ FIX: self._stt.transcribe() era una llamada síncrona bloqueante
        # dentro de una coroutine — congelaba el event loop entero durante
        # toda la transcripción (mismo patrón ya corregido en dispatcher.py
        # para L1 classify). Además, sin esto, ningún asyncio.wait_for externo
        # puede preemptar un cuelgue de STT (bloquea el loop, no solo la tarea).
        loop = asyncio.get_running_loop()
        t_stt = time.perf_counter()
        transcription = await loop.run_in_executor(None, self._stt.transcribe, audio)
        stt_latency = (time.perf_counter() - t_stt) * 1000

        if self._on_transcription:
            self._on_transcription(transcription)

        # ── gate → dispatch → TTS (delegado a pipeline_core) ─────────────────
        # Sin dispatcher: fallback inline (AudioPipeline puede operar sin dispatcher)
        if self._dispatcher is None:
            if transcription.needs_repeat:
                total_latency = (time.perf_counter() - t_total) * 1000
                self._update_stats(total_latency, success=False, repeated=True)
                return PipelineResult(
                    transcription=transcription,
                    dispatcher_response=None,
                    success=False,
                    stt_latency_ms=stt_latency,
                    total_latency_ms=total_latency,
                    was_repeated=True,
                )
            dispatch_response = f"[Sin dispatcher] Transcripto: {transcription.text}"
            if self._on_response:
                self._on_response(dispatch_response)
            total_latency = (time.perf_counter() - t_total) * 1000
            self._update_stats(total_latency, success=True)
            return PipelineResult(
                transcription=transcription,
                dispatcher_response=dispatch_response,
                success=True,
                total_latency_ms=total_latency,
                stt_latency_ms=stt_latency,
            )

        t_core = time.perf_counter()
        result: UtteranceResult = await process_utterance_core(
            transcription,
            self._tts,
            self._dispatcher,
            # AudioPipeline no pasa session_id, context, voice_active_event,
            # wake_detector ni barge_in_event — comportamiento PTT puro
        )
        core_latency = (time.perf_counter() - t_core) * 1000
        total_latency = (time.perf_counter() - t_total) * 1000

        # [FIX 1.2] Distinguir razones de fallo — elimina falso positivo on_error
        if result.reason == "needs_repeat":
            self._update_stats(total_latency, success=False, repeated=True)
            return PipelineResult(
                transcription=transcription,
                dispatcher_response=None,
                success=False,
                stt_latency_ms=stt_latency,
                total_latency_ms=total_latency,
                was_repeated=True,
            )

        if result.reason == "not_actionable":
            # Silencio válido — no es error
            self._update_stats(total_latency, success=False)
            return PipelineResult(
                transcription=transcription,
                dispatcher_response=None,
                success=False,
                stt_latency_ms=stt_latency,
                total_latency_ms=total_latency,
            )

        if not result.success and self._on_error:
            self._on_error(f"Pipeline error: {result.reason}")

        if result.success and self._on_response and result.response_content:
            self._on_response(result.response_content)

        self._update_stats(total_latency, success=result.success)

        logger.info(
            f"[Pipeline] Ciclo completo — total={total_latency:.0f}ms "
            f"(stt={stt_latency:.0f} core={core_latency:.0f}) reason={result.reason}"
        )

        return PipelineResult(
            transcription=transcription,
            dispatcher_response=result.response_content,
            success=result.success,
            total_latency_ms=total_latency,
            stt_latency_ms=stt_latency,
        )

    async def run_once(
        self, timeout: float = 15.0, process_timeout: float = 60.0
    ) -> PipelineResult:
        """
        Captura una utterance del micrófono y la procesa.
        Retorna el resultado. Útil para PTT (push-to-talk) o tests.

        Args:
            timeout: segundos de espera de audio en el micrófono (VAD).
            process_timeout: ✅ FIX AUD-GT-002 — techo global para
                STT+dispatch+TTS (process_audio()). Antes no había ningún
                asyncio.wait_for envolvente sobre ese tramo: un cuelgue en
                STT o en el dispatcher congelaba run_once() para siempre,
                pese a que el parámetro `timeout` sugería un límite total.
        """
        if not self._vad.is_available:
            return PipelineResult(
                transcription=None,
                dispatcher_response=None,
                success=False,
                error="VAD no disponible — micrófono no accesible",
            )

        logger.info("[Pipeline] Esperando utterance...")
        # ✅ FIX: get_running_loop() — siempre hay loop activo en async def
        loop = asyncio.get_running_loop()

        # VAD es bloqueante → ejecutar en thread
        # ✅ FIX AUD-GT-002: wait_for como red de seguridad adicional al
        # timeout interno de listen_once() — si ese timeout interno fallara,
        # esto igual acota la espera en vez de colgar run_in_executor sin límite.
        try:
            chunk: Optional[AudioChunk] = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._vad.listen_once(timeout=timeout),
                ),
                timeout=timeout + 5.0,
            )
        except asyncio.TimeoutError:
            return PipelineResult(
                transcription=None,
                dispatcher_response=None,
                success=False,
                error="Sin audio válido en el timeout dado",
            )

        if chunk is None or not chunk.is_valid:
            return PipelineResult(
                transcription=None,
                dispatcher_response=None,
                success=False,
                error="Sin audio válido en el timeout dado",
            )

        try:
            return await asyncio.wait_for(
                self.process_audio(chunk.to_float32()), timeout=process_timeout
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[Pipeline] process_audio() excedió process_timeout={process_timeout:.1f}s"
            )
            return PipelineResult(
                transcription=None,
                dispatcher_response=None,
                success=False,
                error=f"Timeout procesando utterance (>{process_timeout:.1f}s)",
            )

    async def start(self) -> None:
        """
        Inicia el pipeline en modo continuo.
        Bloqueante — usar await pipeline.start() en task separada.
        Detener con pipeline.stop().
        """
        if not self._vad.is_available:
            logger.error("[Pipeline] VAD no disponible — no se puede iniciar en modo continuo")
            return

        self._running = True
        self._stop_event.clear()
        logger.info("[Pipeline] Iniciado en modo continuo")
        # ✅ FIX: loop = asyncio.get_event_loop() eliminado — no se usa en start()

        while self._running and not self._stop_event.is_set():
            try:
                result = await self.run_once(timeout=30.0)
                if not result.success and result.error:
                    logger.warning(f"[Pipeline] Ciclo fallido: {result.error}")
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Pipeline] Error en ciclo continuo: {e}")
                self._stats["errors"] += 1
                await asyncio.sleep(1.0)

        logger.info("[Pipeline] Detenido")

    def stop(self) -> None:
        """Detiene el pipeline continuo."""
        self._running = False
        self._stop_event.set()
        logger.info("[Pipeline] Stop solicitado")

    def _update_stats(
        self,
        latency_ms: float,
        success: bool,
        repeated: bool = False,
    ) -> None:
        n = self._stats["cycles"]
        self._stats["cycles"] += 1
        self._stats["avg_total_latency_ms"] = (
            self._stats["avg_total_latency_ms"] * n + latency_ms
        ) / (n + 1)
        if success:
            self._stats["successful"] += 1
        if repeated:
            self._stats["repeated"] += 1

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "stt": self._stt.get_stats(),
            "tts": self._tts.get_stats(),
            "vad_available": self._vad.is_available,
        }


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================

_pipeline: Optional[AudioPipeline] = None
_pipeline_lock = threading.Lock()


def get_pipeline(
    dispatcher: Optional[Any] = None,
    **kwargs,
) -> AudioPipeline:
    """
    Retorna el singleton del AudioPipeline.
    Pasa dispatcher y configs en el primer llamado.
    """
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = AudioPipeline(dispatcher=dispatcher, **kwargs)
    return _pipeline
