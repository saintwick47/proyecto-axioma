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
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de STREAMING DEL TURNO
#    DE VOZ (loop de escucha, partials, wake word por texto, dispatch por
#    stream y barge-in), el helper `_dlog` y `accion_turno_voz` viven ahora
#    en el hermano `voice_pipeline_turns.py`. Acá QUEDAN el init/contexto,
#    el turno de captura completa, el ciclo de vida, el modo Jarvis y las
#    métricas. Los nombres movidos se re-importan con los MISMOS nombres
#    (mismo objeto ⇒ API pública intacta) y VoicePipeline hereda del mixin.

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
import uuid

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False

from src.multimodal.pipeline_core import (
    process_utterance_core,
    UtteranceResult,
)

# ── Import desde voice_pipeline_turns (refactorización 50/50) ───
# Nombres movidos al hermano, re-importados con los MISMOS nombres: un solo
# objeto ⇒ `from ...voice_pipeline import accion_turno_voz` sigue igual y el
# pipeline conserva `_dlog` para sus propios logs.
from .voice_pipeline_turns import (  # noqa: F401 — re-export: API pública intacta
    VoicePipelineTurnsMixin,
    _dlog,
    accion_turno_voz,
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


class VoicePipeline(VoicePipelineTurnsMixin):
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
