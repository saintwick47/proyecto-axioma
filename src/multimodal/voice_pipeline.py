#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# voice_pipeline.py - Loop continuo de voz (VoicePipeline)
# Ruta: src/multimodal/voice_pipeline.py
# Autor: SaintWick
# Versión: 1.5.0
#
# Implementa el pipeline completo de voz: WakeWord → VAD → STT → Dispatcher → TTS.
# Soporta streaming incremental real con procesamiento parcial y final,
# barge-in, modos Jarvis (normal/jarvis/voice_only/silent), y métricas
# end-to-end. Ejecuta en thread daemon separado.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import collections
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False

from src.domain.entities import Message, MessageRole
from src.multimodal.tts_engine import TTSEngine
from src.multimodal.pipeline_core import (
    process_utterance_core,
    UtteranceResult,
    _classify_jarvis_intent,
    _JARVIS_EXCLUSIVE_TASKS,
    _predict_handler_weights,
)

logger = logging.getLogger(__name__)

# ✅ CAAC Fase 1: SessionContext — import seguro
try:
    from src.domain.context import SessionContext as _SessionContext
    _SESSION_CONTEXT_AVAILABLE = True
except ImportError:
    _SessionContext = None  # type: ignore[assignment,misc]
    _SESSION_CONTEXT_AVAILABLE = False

# ✅ v0.4.0: settings para Jarvis Mode config
try:
    from config.settings import settings as _settings
    _SETTINGS_AVAILABLE = True
except ImportError:
    _settings = None  # type: ignore[assignment]
    _SETTINGS_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════
# ✅ v1.4.0: detailed_logger — import seguro (doble capa)
# ═══════════════════════════════════════════════════════════════
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _DETAILED_LOGGER_AVAILABLE = True
except ImportError:
    _DETAILED_LOGGER_AVAILABLE = False
    _get_detailed_logger = None  # type: ignore[assignment]


def _dlog() -> Optional[Any]:
    """Retorna instancia del detailed_logger o None si no disponible."""
    if not _DETAILED_LOGGER_AVAILABLE:
        return None
    try:
        lgr = _get_detailed_logger()
        if lgr.is_initialized:
            return lgr
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:79] excepción degradada (intencional): {_d2e}")
    return None


# ── VoicePipelineStats ────────────────────────────────────────────────────────
@dataclass
class VoicePipelineStats:
    """
    [Fase 3.1] Métricas end-to-end del VoicePipeline.
    Todas las propiedades son thread-safe via _lock.
    """
    utterances_total: int = 0
    utterances_successful: int = 0
    confidence_gates_triggered: int = 0   # needs_repeat
    not_actionable_total: int = 0         # silencio válido
    dispatch_errors: int = 0
    tts_errors: int = 0
    barge_ins_total: int = 0
    retries_total: int = 0

    # Últimas 100 latencias end-to-end (ms) para p95
    _latencies: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=100)
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, result: "UtteranceResult", latency_ms: float) -> None:
        """Registra el resultado de un ciclo completo."""
        with self._lock:
            self.utterances_total += 1
            if result.success:
                self.utterances_successful += 1
            if result.reason == "needs_repeat":
                self.confidence_gates_triggered += 1
            elif result.reason == "not_actionable":
                self.not_actionable_total += 1
            elif result.reason == "dispatch_error":
                self.dispatch_errors += 1
            elif result.reason == "tts_error":
                self.tts_errors += 1
            if result.retries > 0:
                self.retries_total += result.retries
            self._latencies.append(latency_ms)

    def record_barge_in(self) -> None:
        with self._lock:
            self.barge_ins_total += 1

    def get_stats(self) -> dict:
        """Snapshot thread-safe de todas las métricas."""
        with self._lock:
            latencies = sorted(self._latencies)
            n = len(latencies)
            avg = sum(latencies) / n if n else 0.0
            p95 = latencies[int(n * 0.95)] if n >= 20 else None

            return {
                "utterances_total": self.utterances_total,
                "utterances_successful": self.utterances_successful,
                "success_rate": (
                    self.utterances_successful / self.utterances_total
                    if self.utterances_total else 0.0
                ),
                "confidence_gates_triggered": self.confidence_gates_triggered,
                "not_actionable_total": self.not_actionable_total,
                "dispatch_errors": self.dispatch_errors,
                "tts_errors": self.tts_errors,
                "barge_ins_total": self.barge_ins_total,
                "retries_total": self.retries_total,
                "avg_latency_ms": round(avg, 1),
                "p95_latency_ms": round(p95, 1) if p95 is not None else None,
                "latency_samples": n,
            }


def accion_turno_voz(texto: str, confianza: float, umbral: float,
                     turnos_vacios: int = 0) -> tuple:
    """Decide qué hacer con un turno de voz: (acción, turnos_vacios_nuevos).

    Acciones:
        - `"ignorar"`: no se transcribió NADA (silencio/ruido) ⇒ se descarta en silencio.
          **No** corresponde pedir repetición: eso es para habla mal entendida.
        - `"avisar_silencio"`: 3 turnos seguidos sin voz ⇒ un aviso único y se reinicia.
        - `"repetir"`: se transcribió algo pero con confianza baja.
        - `"seguir"`: hay texto y confianza suficiente.

    ✅ 2026-10-01 (ISSUE-126): el caso vacío es el que producía el bucle infinito de
    "¿Podés repetir? No entendí bien." con la wake word por energía.
    """
    if not (texto or "").strip():
        vacios = turnos_vacios + 1
        if vacios >= 3:
            return "avisar_silencio", 0
        return "ignorar", vacios
    if confianza < umbral:
        return "repetir", 0
    return "seguir", 0


class VoicePipeline:
    """
    Loop continuo: WakeWord → VAD → STT → Dispatcher → TTS.

    Modos:
    - Con wake word: espera activación antes de escuchar utterance.
    - Sin wake word: escucha continua con VAD.

    Ejecutar en thread daemon via start(). Detener via stop().
    """

    def __init__(
        self,
        dispatcher,
        tts,
        stt,
        vad,
        wake_detector=None,
        voice_active_event: Optional[threading.Event] = None,  # ✅ CAAC Fase 1
    ):
        # ✅ CORREGIDO: Validación de dependencias críticas
        if vad is None:
            raise ValueError("VoicePipeline: 'vad' es requerido (VAD filter para captura de audio)")
        if stt is None:
            raise ValueError("VoicePipeline: 'stt' es requerido (motor de transcripción)")
        if tts is None:
            raise ValueError("VoicePipeline: 'tts' es requerido (motor de síntesis de voz)")
        if dispatcher is None:
            raise ValueError("VoicePipeline: 'dispatcher' es requerido (procesamiento de consultas)")

        self._dispatcher = dispatcher
        self._tts = tts
        self._stt = stt
        self._vad = vad
        self._wake = wake_detector
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # ✅ FIX #1: asyncio.Event() creado dentro del loop activo (run_full_duplex)
        # No inicializar aquí — evita cross-binding entre main thread loop y daemon loop
        self._barge_in_event: Optional[asyncio.Event] = None
        self._full_duplex_task: Optional[asyncio.Task] = None

        # [FIX 1.3] Referencias para cancelación limpia desde stop()
        self._main_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # [Fase 3.1] Métricas end-to-end
        self._stats = VoicePipelineStats()

        # ✅ CAAC Fase 1: shared threading.Event para señalizar actividad de voz
        # ProactiveEngine lo consulta antes de disparar triggers
        self._voice_active_event: Optional[threading.Event] = voice_active_event

        # ✅ CAAC Fase 1: SessionContext por session_id — protegido con Lock
        # threading.Lock (no asyncio) porque el pipeline corre en thread separado
        self._session_contexts: Dict[str, object] = {}
        self._session_contexts_lock = threading.Lock()

        # session_id de voz: fijo por instancia (una sesión de voz continua)
        self._voice_session_id: str = f"voice-{uuid.uuid4().hex[:8]}"

        # ✅ v0.4.0: Modo Jarvis — se actualiza desde set_jarvis_mode()
        self._jarvis_mode: str = "normal"
        # ✅ ISSUE-126: turnos seguidos sin voz (corta el bucle de 'repetí')
        self._turnos_vacios = 0
        _jcfg = {}
        if _SETTINGS_AVAILABLE and _settings is not None and hasattr(_settings, "jarvis_config"):
            _jcfg = _settings.jarvis_config
        self._jarvis_mode = _jcfg.get("default_mode", "normal")
        self._wake_word: str = _jcfg.get("wake_word", "axioma")
        self._jarvis_tts_auto: bool = _jcfg.get("tts_auto_speak", True)
        # ✅ plan_rafael FASE 1 (2026-08-31): UN solo umbral de confianza —
        # settings.jarvis_voice_confidence_threshold (0.5). Antes hardcode 0.35
        # que divergía de yaml (0.5) y settings (0.6).
        self._jarvis_conf_threshold: float = (
            float(getattr(_settings, "jarvis_voice_confidence_threshold", 0.5))
            if _SETTINGS_AVAILABLE and _settings is not None else 0.5
        )

        # ✅ FIX AUD-GT-001: techo global para dispatch+TTS de un turno completo
        # (antes solo había timeouts locales: wake=30s, VAD=8s — nada acotaba
        # el ciclo completo si dispatcher/TTS colgaban sin lanzar excepción)
        # ✅ plan_rafael #11 (2026-08-31): 60s → 180s — screen_describe en CPU
        # tarda 30-150s y el timeout de 60s abortaba el turno a mitad.
        self._turn_timeout_seconds: float = 180.0

        logger.info(
            f"[VoicePipeline] Inicializado — session={self._voice_session_id} "
            f"jarvis_mode={self._jarvis_mode} wake_word={self._wake_word}"
        )

        # ✅ v1.4.0: Log inicial en detailed_logger
        dl = _dlog()
        if dl:
            dl.log_event("VOICE_PIPELINE", "VoicePipeline inicializado", level="INFO", extra={
                "session_id": self._voice_session_id,
                "jarvis_mode": self._jarvis_mode,
                "wake_word": self._wake_word,
                "vad_available": vad is not None,
                "stt_available": stt is not None,
                "tts_available": tts is not None,
            })

    # ── Procesamiento de utterances ───────────────────────────────────────────

    def _get_or_create_context(self):
        """Recupera o crea SessionContext para self._voice_session_id."""
        if not _SESSION_CONTEXT_AVAILABLE or _SessionContext is None:
            return None

        # ✅ FIX v1.3.1: TODO el bloque check+create DENTRO del lock
        # Antes: el `if ctx is None` estaba FUERA del lock → race condition
        # donde 2 threads podían crear 2 contextos distintos para la misma sesión
        with self._session_contexts_lock:
            ctx = self._session_contexts.get(self._voice_session_id)
            if ctx is None:
                ctx = _SessionContext(
                    session_id=self._voice_session_id,
                    jarvis_mode=self._jarvis_mode,
                )
                self._session_contexts[self._voice_session_id] = ctx
        return ctx

    async def _process_utterance(self) -> None:
        """Captura utterance completa via VAD y la procesa."""
        loop = asyncio.get_running_loop()

        # ✅ CORREGIDO: Validación antes de usar vad
        if self._vad is None:
            logger.warning("[VoicePipeline] VAD no disponible — omitiendo captura")
            return

        chunk = await loop.run_in_executor(None, self._vad.listen_once, 8.0)
        if chunk and chunk.is_valid:
            await self._process_utterance_from_chunk(chunk)

    async def _process_utterance_from_chunk(self, chunk) -> Optional["UtteranceResult"]:
        """STT → confidence gate → Dispatcher → TTS (delegado a pipeline_core).

        ✅ plan_rafael (2026-08-31): retorna el UtteranceResult (antes None —
        fire-and-forget) para que el diagnóstico de audio pueda reportar el
        éxito real del turno. Los callers que ignoraban el retorno no cambian.
        """
        dl = _dlog()
        try:
            if self._stt is None:
                logger.error("[VoicePipeline] STT no disponible — no se puede transcribir")
                if dl:
                    dl.log_event("VOICE_PIPELINE", "STT no disponible", level="ERROR")
                return

            transcription = self._stt.transcribe(chunk.to_float32())
            _ctx = self._get_or_create_context()

            t0 = time.perf_counter()

            if dl:
                dl.log_agent_execution(
                    agent_name="VoicePipeline",
                    task_type="utterance_chunk",
                    state="started",
                    success=True,
                    extra={"confidence": getattr(transcription, 'confidence', 0)},
                )

            result: UtteranceResult = await process_utterance_core(
                transcription,
                self._tts,
                self._dispatcher,
                session_id=self._voice_session_id,
                context=_ctx,
                voice_active_event=self._voice_active_event,
                wake_detector=self._wake,
                jarvis_mode=self._jarvis_mode,
            )

            latency_ms = (time.perf_counter() - t0) * 1000
            self._stats.record(result, latency_ms)

            # ✅ plan_rafael FASE 2 (2026-08-31): métricas de voz por turno en
            # rafael.log (setup_logging agrega el handler al root logger).
            _stats = self._stats.get_stats()
            logger.info(
                f"[VoicePipeline] turno ok={result.success} reason={result.reason} "
                f"lat={latency_ms:.0f}ms | total={_stats.get('utterances_total', 0)} "
                f"ok_rate={_stats.get('success_rate', 0):.2f} "
                f"barge_ins={_stats.get('barge_ins_total', 0)}"
            )

            # ✅ v1.4.0: Log resultado del utterance
            if dl:
                dl.log_agent_execution(
                    agent_name="VoicePipeline",
                    task_type=f"utterance_{result.reason}",
                    state="completed" if result.success else "failed",
                    duration=latency_ms / 1000,
                    success=result.success,
                    error=result.reason if not result.success else None,
                )

            # ✅ plan_rafael (2026-08-31): retornar el resultado del turno.
            return result

        except Exception as e:
            logger.error(f"[VoicePipeline] Error procesando utterance: {e}")

            # ✅ v1.4.0: Log error detallado
            if dl:
                dl.log_error(
                    error=e,
                    context="VoicePipeline._process_utterance_from_chunk",
                    function_name="_process_utterance_from_chunk",
                    extra_info={"session_id": self._voice_session_id},
                )

            try:
                await self._tts.speak_error("Error interno")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:371] excepción degradada (intencional): {_d2e}")

    # ── Control de ciclo de vida ──────────────────────────────────────────────

    async def run_full_duplex(self) -> None:
        """
        [A3] Reemplaza run_forever() como método principal.
        Lanza _listen_loop() + _barge_in_monitor() en paralelo con asyncio.gather().
        Mismo event loop — sin threads extra.

        [FIX 1.3] El Task de gather se almacena en self._main_task para que
        stop() pueda cancelarlo desde el thread principal via
        asyncio.run_coroutine_threadsafe, evitando el bloqueo de 30s en
        run_in_executor(wake.listen_once, 30.0).

        ✅ v1.3.2: FIX — usa asyncio.ensure_future() en lugar de
        loop.create_task(asyncio.gather(...)) que causaba TypeError.
        asyncio.ensure_future() acepta Futures Y coroutines en TODAS las
        versiones de Python, eliminando el error definitivamente.
        """
        self._barge_in_event = asyncio.Event()
        self._running = True
        loop = asyncio.get_running_loop()
        self._loop = loop

        logger.info("[VoicePipeline] Full-duplex loop iniciado")

        # ✅ v1.4.0: Log inicio del loop
        dl = _dlog()
        if dl:
            dl.log_event("VOICE_PIPELINE", "Full-duplex loop iniciado", level="INFO",
                         extra={"session_id": self._voice_session_id})

        try:
            # ✅ FIX v1.3.2: asyncio.ensure_future() es la forma oficial y robusta
            # Acepta coroutines, Futures y Tasks en todas las versiones de Python
            self._main_task = asyncio.ensure_future(
                asyncio.gather(
                    self._listen_loop(),
                    self._barge_in_monitor(),
                    return_exceptions=True,
                )
            )
            await self._main_task

        except asyncio.CancelledError:
            logger.info("[VoicePipeline] run_full_duplex cancelado (stop solicitado)")
            if dl:
                dl.log_event("VOICE_PIPELINE", "Full-duplex cancelado", level="INFO")

        except Exception as e:
            logger.error(f"[VoicePipeline] run_full_duplex error: {e}")
            if dl:
                dl.log_error(
                    error=e,
                    context="VoicePipeline.run_full_duplex",
                    function_name="run_full_duplex",
                )

    async def _listen_loop(self) -> None:
        """
        [A3] Versión refactorizada de _run_with_wake_word/_run_vad_only.
        Detecta utterances via STT stream (transcribe_partial).
        Al recibir is_final=True → llama _process_utterance_stream().

        ✅ v1.5.0: captura incremental real. Antes: vad.listen_once(8.0)
        esperaba la utterance COMPLETA (bloqueante en executor), y recién
        ahí se armaba un generador de un solo yield para transcribe_partial()
        — la transcripción arrancaba después de que el usuario terminaba de
        hablar, no durante. Ahora: vad.listen_incremental() es un async
        generator nativo que va yieldeando el buffer creciente MIENTRAS se
        habla, y transcribe_partial() re-transcribe en cada yield — la
        primera respuesta parcial puede llegar antes de que el usuario
        termine la frase. Ya no hace falta run_in_executor acá (la captura
        misma es async, no bloqueante) ni el método _audio_gen (era el
        wrapper de un solo yield que este cambio hace innecesario).
        """
        dl = _dlog()

        while self._running:
            try:
                if self._wake and self._wake.is_available:
                    loop = asyncio.get_running_loop()
                    detected = await loop.run_in_executor(
                        None, self._wake.listen_once, 30.0,
                    )
                    if not detected:
                        continue
                    # ✅ plan_rafael (2026-08-31): el "Dime" ya NO se dice acá —
                    # la puerta de audio detecta "hay habla" (energía calibrada);
                    # el wake word real ("rafael") se verifica por STT en
                    # _process_utterance_stream y el ack se emite solo si matchea.

                # ✅ CORREGIDO: Guard clause para vad
                # ✅ FIX: antes solo chequeaba `is None` — si self._vad existe
                # pero is_available es False (sounddevice no instalado, sin
                # hardware de mic), listen_incremental() retorna instantáneo
                # sin yieldear nada (ver vad_filter.py línea ~495-496), y este
                # guard no lo atrapaba — el while spineaba sin freno.
                if self._vad is None or not self._vad.is_available:
                    logger.warning("[VoicePipeline] VAD no disponible — saltando captura en full-duplex")
                    await asyncio.sleep(1.0)
                    continue

                # ✅ CORREGIDO: Validación de stt antes de transcribe_partial
                # ✅ FIX: faltaba el sleep antes de continue — sin él, si stt
                # queda None de forma persistente (ej. faster-whisper no
                # cargó), este branch spinea sin freno (mismo problema que el
                # de arriba, pero sin siquiera el sleep(1.0)).
                if self._stt is None:
                    logger.error("[VoicePipeline] STT no disponible — no se puede procesar stream")
                    await asyncio.sleep(1.0)
                    continue


                partial_results = []

                async for result in self._stt.transcribe_partial(
                    self._vad.listen_incremental(
                        timeout=8.0,
                        min_yield_interval=self._vad._config.min_yield_interval,
                    )
                ):
                    partial_results.append(result)
                    if not result.is_final:
                        self._fire_partial_side_effects(result)
                        continue

                    if dl:
                        dl.log_event("VOICE_PIPELINE", "STT stream final recibido",
                                     level="INFO", extra={
                                         "text_preview": result.text[:60],
                                         "confidence": result.confidence,
                                     })
                    await asyncio.wait_for(
                        self._process_utterance_stream(partial_results),
                        timeout=self._turn_timeout_seconds,
                    )
                    partial_results = []

            except asyncio.TimeoutError:
                logger.error(
                    f"[VoicePipeline] _process_utterance_stream excedió "
                    f"turn_timeout={self._turn_timeout_seconds:.0f}s — turno abortado"
                )
                if dl:
                    dl.log_event("VOICE_PIPELINE", "Turno abortado por timeout global",
                                  level="ERROR", extra={"timeout_s": self._turn_timeout_seconds})
                partial_results = []
            except Exception as e:
                logger.error(f"[VoicePipeline] _listen_loop error: {e}")
                if dl:
                    dl.log_error(
                        error=e,
                        context="VoicePipeline._listen_loop",
                        function_name="_listen_loop",
                    )
                await asyncio.sleep(1.0)

    def _fire_partial_side_effects(self, partial) -> None:
        """
        ✅ v1.5.0: Efectos secundarios de un partial no-final — cachear en
        dispatcher.process_stream() y pre-calentar el handler probable vía
        process_predictive(). Fire-and-forget: cada tarea se envuelve en su
        propio try/except interno, nunca deben interrumpir _listen_loop ni
        retrasar el próximo partial/final.
        """
        if not partial.text:
            return

        async def _safe_process_stream() -> None:
            try:
                if hasattr(self._dispatcher, "process_stream"):
                    await self._dispatcher.process_stream(
                        partial.text, False, self._voice_session_id,
                    )
            except Exception as e:
                logger.debug(f"[VoicePipeline] process_stream (partial) falló, no crítico: {e}")

        async def _safe_process_predictive() -> None:
            try:
                if not hasattr(self._dispatcher, "process_predictive"):
                    return
                weights = _predict_handler_weights(partial.text)
                if weights:
                    await self._dispatcher.process_predictive(partial.text, weights)
            except Exception as e:
                logger.debug(f"[VoicePipeline] process_predictive falló, no crítico: {e}")

        asyncio.create_task(_safe_process_stream())
        asyncio.create_task(_safe_process_predictive())

    async def _process_utterance_stream(self, partial_results: list) -> None:
        """
        [A3] Acumula partials → al is_final: dispatch + speak_chunked.
        Delega gate→dispatch→TTS a pipeline_core con barge_in_event.
        """
        if not partial_results:
            return

        final = partial_results[-1]
        if not final.is_final:
            return

        # Guard: barge_in_event puede ser None si run_full_duplex no inició aún
        if self._barge_in_event is None:
            return

        dl = _dlog()
        logger.info(f"[VoicePipeline] Stream STT final: '{final.text[:80]}'")

        # ✅ 2026-10-01 (ISSUE-126): DECISIÓN DEL TURNO en una función testeable.
        # El bug medido: con wake word por ENERGÍA (sin modelo ML: el log dice
        # `openwakeword=False porcupine=False`), cualquier ruido dispara el turno; si el
        # audio no tiene voz, la transcripción sale VACÍA y `confidence=0.00` ⇒ se pedía
        # "¿Podés repetir? No entendí bien." una y otra vez. Un turno SIN VOZ no es una
        # frase mal entendida: se ignora (y tras 3 seguidos, un aviso único).
        _accion, self._turnos_vacios = accion_turno_voz(
            final.text, final.confidence, self._jarvis_conf_threshold,
            getattr(self, "_turnos_vacios", 0))
        if _accion == "ignorar":
            logger.info(f"[VoicePipeline] Turno sin voz ({self._turnos_vacios}) — se ignora")
            return
        if _accion == "avisar_silencio":
            logger.info("[VoicePipeline] 3 turnos sin voz seguidos — aviso único")
            await self._tts.speak("No escucho nada. Avisame cuando me hables.")
            return

        # ✅ v0.4.0: En modo Jarvis aplicar confidence threshold propio
        if self._jarvis_mode != "normal":
            if final.confidence < self._jarvis_conf_threshold:
                logger.info(
                    f"[VoicePipeline] Jarvis: conf {final.confidence:.2f} < "
                    f"{self._jarvis_conf_threshold} — pidiendo repetición"
                )
                if dl:
                    dl.log_event("VOICE_PIPELINE", "Jarvis: confidence insuficiente",
                                 level="WARNING", extra={
                                     "confidence": final.confidence,
                                     "threshold": self._jarvis_conf_threshold,
                                 })
                await self._tts.speak("¿Podés repetir? No entendí bien.")
                return

        # ✅ plan_rafael (2026-08-31): wake word real por STT. La puerta de
        # audio (energía calibrada) solo detecta "hay habla"; acá se confirma
        # que la frase va dirigida a Rafael ("rafael [dime] <comando>"). Si no
        # matchea → se ignora sin despachar (Rafael no responde a charla
        # ajena). El ack "Dime" se emite solo cuando matchea.
        cleaned = self._strip_wake_prefix(final.text)
        if cleaned is None:
            logger.info(
                f"[VoicePipeline] Frase sin wake word '{self._wake_word}', "
                f"ignorada: '{final.text[:60]}'"
            )
            return
        if cleaned != final.text:
            final.text = cleaned
            try:
                await self._tts.speak("Dime")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:615] excepción degradada (intencional): {_d2e}")
            logger.info(f"[VoicePipeline] Wake '{self._wake_word}' detectado → comando: '{cleaned[:60]}'")

        # Resetear barge-in antes de hablar
        self._barge_in_event.clear()
        _ctx = self._get_or_create_context()
        t0 = time.perf_counter()

        try:
            if dl:
                dl.log_agent_execution(
                    agent_name="VoicePipeline",
                    task_type="utterance_stream",
                    state="started",
                    success=True,
                )

            # ✅ v1.5.0: la Response final se resuelve acá (streaming real vía
            # process_stream(), con bypass jarvis si corresponde) y se pasa
            # como response_override — process_utterance_core ya no hace su
            # propio dispatch para el caso streaming, solo ctx.update+TTS.
            response = await self._get_streaming_response(final)

            result: UtteranceResult = await process_utterance_core(
                final,
                self._tts,
                self._dispatcher,
                session_id=self._voice_session_id,
                context=_ctx,
                voice_active_event=self._voice_active_event,
                wake_detector=self._wake,
                barge_in_event=self._barge_in_event,
                jarvis_mode=self._jarvis_mode,
                response_override=response,
            )

            latency_ms = (time.perf_counter() - t0) * 1000
            self._stats.record(result, latency_ms)

            # ✅ v1.4.0: Log resultado
            if dl:
                dl.log_agent_execution(
                    agent_name="VoicePipeline",
                    task_type=f"stream_{result.reason}",
                    state="completed" if result.success else "failed",
                    duration=latency_ms / 1000,
                    success=result.success,
                    error=result.reason if not result.success else None,
                )

        except Exception as e:
            logger.error(f"[VoicePipeline] _process_utterance_stream error: {e}")
            if dl:
                dl.log_error(
                    error=e,
                    context="VoicePipeline._process_utterance_stream",
                    function_name="_process_utterance_stream",
                )
            if self._voice_active_event is not None:
                self._voice_active_event.clear()

        finally:
            # Siempre limpiar barge-in tras TTS
            if self._barge_in_event is not None:
                self._barge_in_event.clear()

    # ── Wake word por texto (plan_rafael 2026-08-31) ─────────────────────────

    def _strip_wake_prefix(self, text: str) -> Optional[str]:
        """
        Verifica el wake word ("rafael") en la frase transcrita y devuelve el
        texto SIN el prefijo, o None si la frase NO va dirigida a Rafael.

        Acepta variantes naturales: "rafael dime X", "rafael decime X",
        "rafael X", "rafael, dime X"... El wake word se configura en
        settings.jarvis_wake_word (default "rafael"); si el setting es una
        frase ("rafael dime"), se matchea la frase completa.
        """
        if not text or not text.strip():
            return None
        wake = (self._wake_word or "rafael").strip().lower()
        low = text.lower().strip()
        # ✅ 2026-10-01 (ISSUE-127): se acepta la frase configurada ("hey rafael") y
        # TAMBIÉN el nombre solo ("rafael …"). Motivo medido: la frase pasa por STT y
        # Whisper transcribe "hey" de formas variables ("ey", "hei", "hey"), así que
        # exigir la frase exacta haría fallar invocaciones legítimas. El nombre es la
        # parte discriminante (y es lo que ya funcionaba).
        variantes = [wake]
        if " " in wake:
            variantes.append(wake.split()[-1])
        match = next((v for v in variantes if v in low), None)
        if match is None:
            return None
        idx = low.find(match)
        rest = text[idx + len(match):].lstrip(" ,.:;¿¡")
        # Verbo de llamada opcional tras el nombre ("dime", "decime", ...)
        cleaned = re.sub(
            r"^(dime|decime|decí|decidme|diga|di)\b\s*", "", rest,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    async def _get_streaming_response(self, final) -> Optional[object]:
        """
        ✅ v1.5.0: Resuelve la Response final del streaming.

        En modo Jarvis con task exclusivo (screen_describe/camera_describe/
        autonomous_task) usa el bypass directo a dispatcher._dispatch() —
        mismo criterio que process_utterance_core() aplicaba antes internamente,
        preservando VRAM lock + semáforos de _dispatch() (ver docstring de
        _classify_jarvis_intent en pipeline_core.py). En cualquier otro caso
        pasa por dispatcher.process_stream(text, is_final=True, session_id) —
        el pipeline completo con IntentClassifier real.

        Si algo falla acá, o dispatcher no tiene process_stream, retorna None
        — process_utterance_core con response_override=None hace su propio
        dispatch de respaldo, mismo comportamiento que antes de v1.5.0.
        """
        try:
            if self._jarvis_mode != "normal":
                task_type = _classify_jarvis_intent(final.text)
                if task_type in _JARVIS_EXCLUSIVE_TASKS:
                    msg = Message(
                        content=final.text,
                        role=MessageRole.USER,
                        metadata={"source": "voice", "confidence": final.confidence},
                    )
                    logger.info(f"[VoicePipeline] Jarvis bypass (streaming) — task_type={task_type}")
                    # ✅ plan_rafael #11 (2026-08-31): avisar por TTS que va a
                    # tardar (screen_describe en CPU toma 30-150s).
                    if self._tts is not None and task_type in ("screen_describe", "camera_describe"):
                        try:
                            await self._tts.speak(
                                "Un momento, estoy mirando la pantalla"
                            )
                        except Exception as _d2e:
                            logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:742] excepción degradada (intencional): {_d2e}")
                    return await self._dispatcher._dispatch(
                        msg, task_type, self._voice_session_id, None,
                    )

            if hasattr(self._dispatcher, "process_stream"):
                return await self._dispatcher.process_stream(
                    final.text, True, self._voice_session_id,
                )
        except Exception as e:
            logger.warning(
                f"[VoicePipeline] _get_streaming_response falló, "
                f"fallback a dispatch propio de process_utterance_core: {e}"
            )
        return None

    async def _barge_in_monitor(self) -> None:
        """
        [A3] Monitorea VAD cada 20ms para detectar voz durante TTS.
        Si detecta voz → self._barge_in_event.set().
        Resetea el event cuando TTS termina (lo hace _process_utterance_stream).

        [FIX barge-in] Versión anterior llamaba a self._vad.is_speech_detected()
        protegido con hasattr() — VADFilter nunca tuvo ese método, hasattr()
        daba False siempre y esta corrutina no hacía nada en 20+ versiones.
        VADFilter ahora expone start_background_monitor()/stop_background_monitor()/
        is_speech_detected() (ver vad_filter.py). Esta versión además gatea todo
        por self._voice_active_event: el stream de monitoreo de VADFilter solo
        corre mientras el TTS está hablando — nunca en paralelo con el
        sd.InputStream que _listen_loop() abre vía wake.listen_once()/vad.listen_once(),
        evitando dos InputStream concurrentes sobre el mismo device.
        """
        was_speaking = False
        while self._running:
            try:
                await asyncio.sleep(0.02)  # poll cada 20ms
                if self._barge_in_event is None:
                    continue

                is_speaking_now = bool(
                    self._voice_active_event is not None
                    and self._voice_active_event.is_set()
                )

                if is_speaking_now and not was_speaking:
                    if self._vad and hasattr(self._vad, "start_background_monitor"):
                        await asyncio.to_thread(self._vad.start_background_monitor)
                elif was_speaking and not is_speaking_now:
                    if self._vad and hasattr(self._vad, "stop_background_monitor"):
                        await asyncio.to_thread(self._vad.stop_background_monitor)
                was_speaking = is_speaking_now

                if is_speaking_now and self._vad and hasattr(self._vad, 'is_speech_detected'):
                    if self._vad.is_speech_detected():
                        if not self._barge_in_event.is_set():
                            logger.debug("[VoicePipeline] Barge-in detectado")
                            self._barge_in_event.set()
                            self._stats.record_barge_in()

                            # ✅ v1.4.0: Log barge-in
                            dl = _dlog()
                            if dl:
                                dl.log_event("VOICE_PIPELINE", "Barge-in detectado",
                                             level="INFO", extra={
                                                 "total_barge_ins": self._stats.barge_ins_total,
                                             })

            except Exception as e:
                logger.warning(f"[VoicePipeline] _barge_in_monitor error: {e}")
                if self._vad and hasattr(self._vad, "stop_background_monitor"):
                    try:
                        await asyncio.to_thread(self._vad.stop_background_monitor)
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:815] excepción degradada (intencional): {_d2e}")
                was_speaking = False
                await asyncio.sleep(0.1)

    # ✅ v0.4.0: Jarvis Mode API pública — llamada desde UI o Dispatcher
    def set_jarvis_mode(self, mode: str) -> None:
        """
        Cambia el modo Jarvis del pipeline en caliente.
        Propaga al SessionContext activo si existe.

        Args:
            mode: "normal" | "jarvis" | "voice_only" | "silent"
        """
        _valid = {"normal", "jarvis", "voice_only", "silent"}
        if mode not in _valid:
            logger.warning(f"[VoicePipeline] set_jarvis_mode: modo inválido '{mode}'")
            return

        old_mode = self._jarvis_mode
        self._jarvis_mode = mode

        # ✅ FIX v1.3.1: Propagación DENTRO del lock — evita race condition
        # donde el contexto podría eliminarse entre el get y el update
        with self._session_contexts_lock:
            ctx = self._session_contexts.get(self._voice_session_id)
            if ctx is not None:
                if hasattr(ctx, "update_mode"):
                    try:
                        ctx.update_mode(mode)
                    except Exception as e:
                        logger.warning(f"[VoicePipeline] SessionContext.update_mode error: {e}")
                elif hasattr(ctx, "jarvis_mode"):
                    try:
                        ctx.jarvis_mode = mode
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:850] excepción degradada (intencional): {_d2e}")

        # En modo silent: TTS no habla
        if mode == "silent" and hasattr(self._tts, "_config"):
            try:
                self._tts._config.play_audio = False
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:857] excepción degradada (intencional): {_d2e}")
        elif mode != "silent" and hasattr(self._tts, "_config"):
            try:
                self._tts._config.play_audio = True
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:862] excepción degradada (intencional): {_d2e}")

        logger.info(f"[VoicePipeline] Modo Jarvis cambiado a: {mode}")

        # ✅ v1.4.0: Log cambio de modo
        dl = _dlog()
        if dl:
            dl.log_event("VOICE_PIPELINE", "Modo Jarvis cambiado", level="INFO", extra={
                "old_mode": old_mode,
                "new_mode": mode,
                "session_id": self._voice_session_id,
            })

    @property
    def jarvis_mode(self) -> str:
        """Modo Jarvis activo del pipeline."""
        return self._jarvis_mode

    def start(self) -> "VoicePipeline":
        """
        Inicia pipeline en thread daemon separado.
        ✅ v1.3.2: Manejo robusto de excepciones para evitar muerte silenciosa del thread.
        ✅ 2026-08-25: idempotente — si el thread ya está vivo, no crea otro
        (el toggle de la UI puede llamar start() repetidamente).
        """
        if getattr(self, "_thread", None) is not None and self._thread.is_alive():
            logger.info("[VoicePipeline] Ya corriendo — start() ignorado (idempotente)")
            return self
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.run_full_duplex())
            except Exception as e:
                logger.error(f"[VoicePipeline] Thread daemon murió con error: {e}", exc_info=True)
                # ✅ v1.4.0: Log muerte del thread
                dl = _dlog()
                if dl:
                    dl.log_error(
                        error=e,
                        context="VoicePipeline thread daemon murió",
                        function_name="start._run",
                    )
            finally:
                try:
                    # Limpiar tareas pendientes antes de cerrar el loop
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:914] excepción degradada (intencional): {_d2e}")
                loop.close()

        self._thread = threading.Thread(
            target=_run, daemon=True, name="VoicePipeline",
        )
        self._thread.start()
        logger.info("[VoicePipeline] Thread iniciado")

        # ✅ v1.4.0: Log start
        dl = _dlog()
        if dl:
            dl.log_event("VOICE_PIPELINE", "Thread daemon iniciado", level="INFO", extra={
                "thread_name": "VoicePipeline",
                "session_id": self._voice_session_id,
            })

        return self

    async def run_forever(self) -> None:
        """Alias de run_full_duplex() para compatibilidad con callers externos."""
        await self.run_full_duplex()

    def stop(self) -> None:
        """
        Detiene el pipeline.

        [FIX 1.3] Cancela self._main_task desde el thread correcto via
        run_coroutine_threadsafe — evita el bloqueo de hasta 30s que ocurría
        cuando _listen_loop estaba bloqueado en run_in_executor(wake.listen_once, 30.0).
        """
        self._running = False

        if self._voice_active_event is not None:
            self._voice_active_event.clear()

        if self._main_task is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._cancel_main_task(), self._loop
                )
            except Exception as e:
                logger.warning(f"[VoicePipeline] Error cancelando main task: {e}")

        logger.info("[VoicePipeline] Detenido")

        # ✅ v1.4.0: Log stop + métricas finales
        dl = _dlog()
        if dl:
            stats = self._stats.get_stats()
            dl.log_event("VOICE_PIPELINE", "Pipeline detenido", level="INFO", extra={
                "session_id": self._voice_session_id,
                "utterances_total": stats.get("utterances_total", 0),
                "success_rate": round(stats.get("success_rate", 0) * 100, 1),
                "barge_ins_total": stats.get("barge_ins_total", 0),
                "dispatch_errors": stats.get("dispatch_errors", 0),
            })

    async def _cancel_main_task(self) -> None:
        """Cancela el main task desde dentro del event loop del daemon."""
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception) as _d2e:
                logging.getLogger(__name__).debug(f"[D2 voice_pipeline.py:979] excepción degradada (intencional): {_d2e}")

    @property
    def is_running(self) -> bool:
        return self._running and (
            self._thread is not None and self._thread.is_alive()
        )

    @property
    def is_speaking(self) -> bool:
        """True si el TTS está reproduciendo audio ahora mismo."""
        if self._voice_active_event is not None:
            return self._voice_active_event.is_set()
        return False

    @property
    def is_listening(self) -> bool:
        """True si el pipeline está activo y no está hablando."""
        return self._running and not self.is_speaking

    def get_stats(self) -> dict:
        """[Fase 3.1] Retorna métricas end-to-end thread-safe."""
        return self._stats.get_stats()
