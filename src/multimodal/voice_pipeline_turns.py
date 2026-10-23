#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — voice_pipeline_turns.py  (REFACTORIZACIÓN 50/50)
# Ruta: /ruta/a/axioma/src/multimodal/voice_pipeline_turns.py
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de STREAMING DEL TURNO DE
#    VOZ se extrae de `src/multimodal/voice_pipeline.py` (1054 líneas) a este
#    hermano, para dejarlo por debajo de las 1000 (regla del proyecto: cada
#    archivo >1000 líneas se parte en DOS: el original conserva una mitad y
#    este hermano recibe la otra; nunca varios archivos chicos).
#
#    Qué se movió, SIN cambios de comportamiento:
#      · El LOOP DE STREAMING y sus piezas, como
#        `VoicePipelineTurnsMixin`: `_listen_loop` (captura incremental real
#        con `vad.listen_incremental` + `stt.transcribe_partial`),
#        `_fire_partial_side_effects` (cacheo/predicción fire-and-forget de
#        los partials), `_process_utterance_stream` (gate de confianza,
#        wake word por STT, dispatch y TTS del final),
#        `_strip_wake_prefix` (matching del wake word con variantes de
#        transcripción), `_get_streaming_response` (bypass Jarvis /
#        `dispatcher.process_stream`) y `_barge_in_monitor` (barge-in).
#        `VoicePipeline` (en el original) hereda de este mixin, así que los
#        métodos siguen disponibles en cualquier instancia: la API pública
#        NO cambia (los tests usan `VoicePipeline.__new__(VoicePipeline)` y
#        `p._strip_wake_prefix(...)`, que sigue funcionando igual).
#      · Los helpers compartidos por las dos mitades: `_dlog` (y su import
#        seguro de DetailedLogger) y la decisión pura `accion_turno_voz`
#        (ISSUE-126: turno sin voz ≠ pedir repetición). El original los
#        re-importa con los MISMOS nombres ⇒ un solo objeto, sin duplicar
#        estado; `from src.multimodal.voice_pipeline import accion_turno_voz`
#        (tests de RAC estático) sigue funcionando.
#
#    Qué QUEDA en el original: `VoicePipelineStats`, `__init__` (deps +
#    config Jarvis desde settings), el CONTEXTO de sesión
#    (`_get_or_create_context`), el turno de CAPTURA COMPLETA
#    (`_process_utterance`, `_process_utterance_from_chunk`), el CICLO DE
#    VIDA (`run_full_duplex`, `start`, `run_forever`, `stop`,
#    `_cancel_main_task`, `is_running`/`is_speaking`/`is_listening`), el
#    MODO JARVIS (`set_jarvis_mode`, `jarvis_mode`) y `get_stats`.
#
#    Corte elegido: "turno por streaming" (partials, wake word por texto,
#    barge-in) vs "init/contexto/turno completo/ciclo de vida/modo". Las dos
#    mitades son las dos rutas reales del pipeline: `_listen_loop` →
#    `_process_utterance_stream` (movida) y el one-shot
#    `_process_utterance` → `_process_utterance_from_chunk` (queda).
#
#    Estado mutable compartido: NINGUNO de módulo (sin caches, registros,
#    locks ni singletons globales; `VoicePipelineStats` y los locks/eventos
#    son de INSTANCIA y se acceden por `self.`, así que este mixin los usa
#    sin duplicarlos). Los únicos globales son `logger` (mismo objeto que el
#    original: `logging.getLogger("src.multimodal.voice_pipeline")`) y las
#    banderas de disponibilidad de imports, inmutables.
#    Dependencia en UN SOLO SENTIDO (voice_pipeline → este módulo): este
#    hermano es HOJA y NO importa el original a nivel de módulo (sin
#    AUD-CIR-001); `pipeline_core`/`domain.entities` se importan de su
#    origen, no del original.
#    Barrido: ningún test parchea globals de voice_pipeline (sólo importa
#    VoicePipeline, VoicePipelineStats y accion_turno_voz); no hay tests de
#    `inspect.getsource`/`read_text()` sobre este archivo; ningún detector
#    del auditor busca cadenas por FUENTE acá.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from src.domain.entities import Message, MessageRole
from src.multimodal.pipeline_core import (
    process_utterance_core,
    UtteranceResult,
    _classify_jarvis_intent,
    _JARVIS_EXCLUSIVE_TASKS,
    _predict_handler_weights,
)

# Mismo objeto Logger que el original (`logging.getLogger(__name__)` allí):
# así los mensajes del streaming conservan el nombre histórico del módulo.
logger = logging.getLogger("src.multimodal.voice_pipeline")


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


class VoicePipelineTurnsMixin:
    """
    Turno de voz por STREAMING (mitad de streaming, 50/50).

    Cubre la ruta incremental real del pipeline: `_listen_loop` captura y
    transcribe partials, `_fire_partial_side_effects` cachea/predice sin
    bloquear, `_process_utterance_stream` decide el turno final (silencio,
    confianza, wake word) y despacha, `_get_streaming_response` resuelve la
    Response, `_strip_wake_prefix` matchea el wake word transcrito y
    `_barge_in_monitor` detecta interrupciones durante el TTS.

    Requiere que la clase que lo mezcle defina el estado de instancia que
    crea `VoicePipeline.__init__` (`_vad`, `_stt`, `_tts`, `_dispatcher`,
    `_stats`, `_barge_in_event`, `_voice_active_event`, `_jarvis_mode`,
    `_wake_word`, `_turnos_vacios`, `_running`, `_voice_session_id`, ...) y
    el helper de contexto `_get_or_create_context`.
    """

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
