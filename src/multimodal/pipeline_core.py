#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Pipeline Core (Lógica compartida STT→Dispatch→TTS)
# Ruta: src/multimodal/pipeline_core.py
# Autor: SaintWick
# Versión: 1.3.0
#
# Cambios v1.1.0:
#   [FIX 1.1] ctx.update(task_type) tras dispatch — CAAC Fase 1 operativo
#   [FIX 1.2] UtteranceResult reemplaza Optional[str] opaco:
#             distingue ok / not_actionable / needs_repeat / dispatch_error / tts_error
#   [FIX 2.1] Retry con backoff exponencial para TimeoutError/ConnectionError en dispatch
# Cambios v1.2.0 (Rafael Fase 1):
#   [FIX 3.1] jarvis_mode != "normal" → bypassea IntentClassifier: clasifica
#             localmente con _classify_jarvis_intent() y llama
#             dispatcher._dispatch() directo (preserva VRAM lock + semáforos
#             de _dispatch(), a diferencia de llamar _handle_jarvis() a mano)
# Cambios v1.3.0 (streaming incremental real):
#   [FIX 4.1] response_override en process_utterance_core() — permite que
#             voice_pipeline.py, en modo streaming, obtenga la Response por
#             su cuenta vía dispatcher.process_stream(text, is_final, sid)
#             sobre parciales de VADFilter.listen_incremental(), y reuse acá
#             ctx.update + TTS + barge-in sin duplicar esa lógica ni pasar
#             de nuevo por el dispatch propio de este archivo.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:  # ✅ ISSUE-098: solo para anotaciones (evita ciclos en runtime)
    from src.domain.entities import Response
    from src.multimodal.stt_engine import TranscriptionResult

from src.multimodal.tts_engine import TTSEngine

logger = logging.getLogger(__name__)

_MAX_TTS_CHARS = 500
_TTS_TRUNCATION_SUFFIX = "... hay más información disponible."

# Retry config — solo para errores de transporte, no de lógica
_MAX_DISPATCH_RETRIES = 2
_RETRY_DELAYS = [1.0, 2.0]
_DISPATCH_TIMEOUT = 220.0

# ── Clasificación heurística de intent Jarvis ────────────────────────────────
# Mismo patrón que _is_analysis_intent() en dispatcher.py: any(kw in q for kw in KEYWORDS)
_SCREEN_DESCRIBE_KEYWORDS = frozenset({
    "pantalla", "screen", "qué ves", "que ves", "qué hay en pantalla",
    "que hay en pantalla", "describe lo que", "mirá la pantalla",
    "mira la pantalla", "qué estoy viendo", "que estoy viendo",
    "captura de pantalla", "qué tengo abierto", "que tengo abierto",
})
_AUTONOMOUS_TASK_KEYWORDS = frozenset({
    "abrí", "abri", "abre", "cerrá", "cerra", "cierra", "ejecutá", "ejecuta",
    "instalá", "instala", "creá", "crea", "borrá", "borra", "movete a",
    "andá a", "anda a", "hacé", "hace", "corré", "corre", "lanzá", "lanza",
    "reiniciá", "reinicia", "apagá", "apaga",
})
_CAMERA_DESCRIBE_KEYWORDS = frozenset({
    "cámara", "camara", "webcam",
})
_JARVIS_EXCLUSIVE_TASKS = frozenset({"screen_describe", "camera_describe", "autonomous_task"})


def _classify_jarvis_intent(text: str) -> str:
    """
    Clasificación local de intent para modo Jarvis — bypassea IntentClassifier.
    Retorna uno de: "camera_describe" | "screen_describe" | "autonomous_task" | "jarvis_query".
    Cámara se chequea antes que pantalla — "qué ves" es ambiguo entre ambas
    y "cámara"/"webcam" es la señal más específica.
    """
    if not text:
        return "jarvis_query"
    q = text.strip().lower()
    if any(kw in q for kw in _CAMERA_DESCRIBE_KEYWORDS):
        return "camera_describe"
    if any(kw in q for kw in _SCREEN_DESCRIBE_KEYWORDS):
        return "screen_describe"
    if any(kw in q for kw in _AUTONOMOUS_TASK_KEYWORDS):
        return "autonomous_task"
    return "jarvis_query"


# ── Pre-warm heurístico para streaming (process_predictive) ─────────────────────
# ✅ v1.3.0: NO es el clasificador real — eso corre una sola vez, al final, con
# el texto completo (IntentClassifier vía dispatcher.process()/process_stream()).
# Esto es solo una señal barata para pre-calentar el handler más probable
# MIENTRAS el usuario todavía está hablando — un falso negativo (no matchea
# nada) es el caso común y no rompe nada, solo pierde la optimización de
# latencia para esa utterance puntual.
_PREDICTIVE_KEYWORDS: Dict[str, frozenset] = {
    "CoderAgent": frozenset({
        "código", "codigo", "función", "funcion", "bug", "error de", "python",
        "programa", "script", "clase", "variable", "compilá", "compila",
    }),
    "SearcherAgent": frozenset({"buscá", "busca", "googlea", "buscar"}),
    "ResearcherAgent": frozenset({"investigá", "investiga"}),
    "SystemAgent": frozenset({
        "abrí", "abri", "abre", "cerrá", "cerra", "cierra", "ejecutá", "ejecuta",
        "instalá", "instala", "creá", "crea", "borrá", "borra",
    }),
}


def _predict_handler_weights(text: str) -> Dict[str, float]:
    """
    Heurística liviana de keyword-matching sobre un parcial en progreso —
    retorna {handler_name: weight} para process_predictive(). Diccionario
    vacío si no matchea nada (caso común, no es un error).
    """
    if not text:
        return {}
    q = text.strip().lower()
    weights: Dict[str, float] = {}
    for handler_name, keywords in _PREDICTIVE_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in q)
        if hits:
            weights[handler_name] = min(1.0, 0.3 + 0.2 * hits)
    return weights


# ── UtteranceResult ────────────────────────────────────────────────────────────

@dataclass
class UtteranceResult:
    """
    Resultado tipado de process_utterance_core.
    Reemplaza Optional[str] — distingue los tres estados posibles:
      - success=True, reason="ok"             → dispatch + TTS completados
      - success=False, reason="needs_repeat"  → confidence gate activado
      - success=False, reason="not_actionable"→ texto vacío, silencio válido
      - success=False, reason="dispatch_error"→ error en Dispatcher
      - success=False, reason="tts_error"     → error en TTS (dispatch OK)
    """
    success: bool
    response_content: Optional[str] = None
    reason: str = "ok"          # "ok" | "needs_repeat" | "not_actionable" | "dispatch_error" | "tts_error"
    dispatch_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0
    retries: int = 0


# ── process_utterance_core ─────────────────────────────────────────────────────

async def process_utterance_core(
    transcription: "TranscriptionResult",
    tts: "TTSEngine",
    dispatcher,
    *,
    session_id: Optional[str] = None,
    context=None,                                    # SessionContext | None
    voice_active_event: Optional[threading.Event] = None,
    wake_detector=None,
    barge_in_event: Optional[asyncio.Event] = None,
    max_tts_chars: int = _MAX_TTS_CHARS,
    jarvis_mode: str = "normal",
    response_override: Optional["Response"] = None,
) -> UtteranceResult:
    """
    Ciclo completo: confidence_gate → dispatch (con retry) → ctx.update → TTS.

    Retorna UtteranceResult en todos los casos — nunca None.

    ✅ v1.3.0: response_override — si el caller ya obtuvo una Response por su
    cuenta (ej. voice_pipeline.py en modo streaming, vía
    dispatcher.process_stream(text, is_final=True, session_id) sobre
    parciales de VADFilter.listen_incremental()), la pasa acá y se saltea
    todo el paso 2 (construcción de Message, clasificación jarvis local,
    retry loop) — reusa ctx.update + TTS + barge-in tal cual, sin duplicar
    esa lógica. Sin response_override, comportamiento idéntico a antes.
    Nota: el retry con backoff de este método es SOLO para el dispatch
    propio del paso 2 — con response_override, cualquier retry queda a
    cargo del caller (process_stream() ya pasa por el Dispatcher completo).
    """
    # ── 1. Confidence gate ────────────────────────────────────────────────────
    if transcription.needs_repeat:
        await tts.speak_confirmation_request()
        return UtteranceResult(success=False, reason="needs_repeat")

    if not transcription.is_actionable:
        return UtteranceResult(success=False, reason="not_actionable")

    logger.info(f"[PipelineCore] STT OK: '{transcription.text[:80]}'")

        # ✅ v0.4.0: En modo Jarvis reducir max_tts_chars
    if jarvis_mode != "normal":
        max_tts_chars = min(max_tts_chars, 300)

# ── 2. Dispatch con retry (saltea si hay response_override) ──────────────
    import time as _time
    response = None
    retries = 0
    t_dispatch = _time.perf_counter()
    is_jarvis_mode = jarvis_mode != "normal"
    jarvis_task_type: Optional[str] = None
    use_bypass = False

    if response_override is not None:
        response = response_override
        dispatch_ms = 0.0
        logger.info(
            f"[PipelineCore] response_override presente — dispatch propio salteado "
            f"(task_type={getattr(response, 'task_type', '?')})"
        )
    else:
        try:
            from src.domain.entities import Message, MessageRole
            msg = Message(
                content=transcription.text,
                role=MessageRole.USER,
                metadata={"source": "voice", "confidence": transcription.confidence},
            )

            kwargs: dict = {}
            if session_id is not None:
                kwargs["session_id"] = session_id
            if context is not None:
                kwargs["context"] = context
            # jarvis_mode ya está en context.jarvis_mode — no se pasa como kwarg al dispatcher

            if is_jarvis_mode:
                jarvis_task_type = _classify_jarvis_intent(transcription.text)
                use_bypass = jarvis_task_type in _JARVIS_EXCLUSIVE_TASKS
                if use_bypass:
                    logger.info(f"[PipelineCore] Jarvis bypass — task_type={jarvis_task_type}")
                else:
                    logger.info("[PipelineCore] jarvis_query → pipeline completo (IntentClassifier real)")

            for attempt in range(_MAX_DISPATCH_RETRIES + 1):
                try:
                    if use_bypass:
                        response = await asyncio.wait_for(
                            dispatcher._dispatch(msg, jarvis_task_type, session_id, None),
                            timeout=_DISPATCH_TIMEOUT,
                        )
                    else:
                        response = await asyncio.wait_for(
                            dispatcher.process(msg, **kwargs),
                            timeout=_DISPATCH_TIMEOUT,
                        )
                    break
                except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                    if attempt < _MAX_DISPATCH_RETRIES:
                        retries += 1
                        logger.warning(
                            f"[PipelineCore] Dispatch intento {attempt+1} fallido ({type(exc).__name__}) "
                            f"— reintentando en {_RETRY_DELAYS[attempt]}s"
                        )
                        try:
                            await tts.speak("Un momento...")
                        except Exception as _d2e:
                            logging.getLogger(__name__).debug(f"[D2 pipeline_core.py:252] excepción degradada (intencional): {_d2e}")
                        await asyncio.sleep(_RETRY_DELAYS[attempt])
                    else:
                        raise

        except Exception as e:
            dispatch_ms = (_time.perf_counter() - t_dispatch) * 1000
            logger.error(f"[PipelineCore] Dispatch error: {e}", exc_info=True)
            try:
                await tts.speak_error("Hubo un problema procesando tu solicitud")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 pipeline_core.py:263] excepción degradada (intencional): {_d2e}")
            return UtteranceResult(
                success=False,
                reason="dispatch_error",
                dispatch_latency_ms=dispatch_ms,
                retries=retries,
            )
        else:
            dispatch_ms = (_time.perf_counter() - t_dispatch) * 1000

    logger.info(
        f"[PipelineCore] Dispatch retornó — task_type={getattr(response, 'task_type', '?')} "
        f"error={getattr(response, 'error', None)} dispatch_ms={dispatch_ms:.0f}"
    )

    if response is None or response.error:
        logger.error(f"[PipelineCore] Response con error: {getattr(response, 'error', 'response=None')}")
        await tts.speak_error("Hubo un problema procesando tu solicitud")
        return UtteranceResult(
            success=False,
            reason="dispatch_error",
            dispatch_latency_ms=dispatch_ms,
            retries=retries,
        )

    # ── 3. [FIX 1.1] Actualizar SessionContext con task_type real ────────────
    if context is not None:
        try:
            context.update(task_type=response.task_type)
        except Exception as e:
            logger.warning(f"[PipelineCore] ctx.update falló (no crítico): {e}")

    # ── 4. Pre-procesamiento TTS ──────────────────────────────────────────────
    tts_text = TTSEngine.preprocess_text(response.content or "")
    if len(tts_text) > max_tts_chars:
        tts_text = tts_text[:max_tts_chars] + _TTS_TRUNCATION_SUFFIX

    # ── 5. Señalizar voz activa ───────────────────────────────────────────────
    if voice_active_event is not None:
        voice_active_event.set()
    if wake_detector is not None:
        wake_detector.set_speaking(True)

    # ── 6. TTS — bifurcado por jarvis_mode ────────────────────────────────────
    t_tts = _time.perf_counter()
    tts_ok = True
    try:
        if jarvis_mode == "silent":
            # Modo silent: no hablar, solo procesar
            logger.debug("[PipelineCore] silent mode — TTS omitido")
        else:
            if barge_in_event is not None:
                await tts.speak_chunked(tts_text, barge_in_event)
            else:
                await tts.speak(tts_text)
    except Exception as e:
        logger.error(f"[PipelineCore] TTS error: {e}", exc_info=True)
        tts_ok = False
    finally:
        if wake_detector is not None:
            wake_detector.set_speaking(False)
        if voice_active_event is not None:
            voice_active_event.clear()

    tts_ms = (_time.perf_counter() - t_tts) * 1000

    # [Fase 3.2] Log estructurado — parseable por herramientas de monitoreo
    logger.info(
        "[PipelineCore] cycle_complete",
        extra={
            "session_id": session_id,
            "confidence": transcription.confidence,
            "dispatch_ms": round(dispatch_ms, 1),
            "tts_ms": round(tts_ms, 1),
            "truncated": len(tts_text) > max_tts_chars,
            "barge_in": barge_in_event is not None and barge_in_event.is_set(),
            "retries": retries,
            "tts_ok": tts_ok,
        }
    )

    return UtteranceResult(
        success=tts_ok,
        response_content=response.content,
        reason="ok" if tts_ok else "tts_error",
        dispatch_latency_ms=dispatch_ms,
        tts_latency_ms=tts_ms,
        retries=retries,
    )


# ═══════════════════════════════════════════════════════════════
# 📋 COMPONENTES v1.1.0
# ═══════════════════════════════════════════════════════════════
# | Componente            | Propósito                                      |
# |-----------------------|------------------------------------------------|
# | UtteranceResult       | Resultado tipado: reemplaza Optional[str]      |
# | process_utterance_core| gate → dispatch (retry) → ctx.update → TTS    |
# | _MAX_DISPATCH_RETRIES | 2 reintentos para TimeoutError/ConnectionError  |
# | _RETRY_DELAYS         | Backoff [1s, 2s]                               |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
