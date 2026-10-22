#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# Ruta: /ruta/a/axioma/src/core/dispatcher_process.py
# Autor: SaintWick
# dispatcher_process.py - Motor de procesamiento y ruteo del Dispatcher de AXIOMA.
#
# Extraído de dispatcher.py (refactor de mantenibilidad — el archivo original
# había crecido a 1401 líneas). Contiene el pipeline completo de una request:
# caché de respuesta (RAM/L3 semántico), clasificación de intención, overrides
# de ruteo, dispatch al handler/agente correspondiente, validación post-dispatch
# de chat, aprendizaje (UserProfile/FeedbackLoop), y el router interno de bajo
# nivel (_dispatch) con circuit breaker y VRAM lock.
#
# DispatcherProcessMixin se combina con Dispatcher (dispatcher.py) — los
# métodos de acá acceden a recursos definidos en dispatcher.py (self.classifier,
# self.coder, self._routing_cache, self._sem_fast, etc.) vía self., sin
# necesidad de importar nada de dispatcher.py — evita import circular
# (dispatcher.py sí importa DispatcherProcessMixin desde este archivo).
# ──────────────────────────────────────────────────────────────────────────────
import logging
import asyncio
import time
import traceback
import hashlib
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from src.domain.entities import Message, Response
from src.llm.client import OllamaClient
from config.settings import settings
from src.core.dispatcher_handlers import _log_event, _log_error, _log_router_decision

# ✅ v0.6.9v (REFACTOR 50/50): guard + write-path de memoria extraídos a
# src/core/dispatcher_process_guard.py. DispatcherProcessMixin hereda el mixin.
from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin
from src.core.dispatcher_process_steps import DispatcherProcessStepsMixin
from src.utils.degradation import log_degraded
from src.core.dispatcher_process_guard import calibrate_response_confidence

# ── FASE 3 (plan_harness): evento dispatch.started. Import diferido del
# bus dentro de process() (src/core/__init__ importa el Dispatcher con
# impaciencia — un import a nivel de módulo del bus sería un ciclo).

# ✅ Fase 6 — FeedbackLoop — import seguro
try:
    from src.core.feedback_loop import ImplicitSignal
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False
    ImplicitSignal = None  # type: ignore[assignment]

# ✅ FIX L3: RouterCache singleton — propagación de decisiones al caché semántico
try:
    from src.router.cache import get_router_cache as _get_router_cache
    _ROUTER_CACHE_AVAILABLE = True
except ImportError:
    _ROUTER_CACHE_AVAILABLE = False
    _get_router_cache = None  # type: ignore[assignment]

# ✅ CAAC Fase 0: SessionContext — import seguro
try:
    from src.domain.context import SessionContext as _SessionContext
    _SESSION_CONTEXT_AVAILABLE = True
except ImportError:
    _SessionContext = None  # type: ignore[assignment,misc]
    _SESSION_CONTEXT_AVAILABLE = False

# ✅ Semana 2 D: ContextSensor
try:
    from src.core.context_sensor import get_sensor as _get_sensor
except ImportError:
    _get_sensor = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ✅ v1.0.6: Detección de intent de análisis para routing override
# ═══════════════════════════════════════════════════════════════
_ANALYSIS_INTENT_KEYWORDS = {
    "analiz", "revis", "audit", "bug", "error", "vulnerabil",
    "mejor", "correg", "fix", "issue", "problema", "falla",
    "seguridad", "security", "review", "check", "inspect",
    "evalua", "diagnost", "critic", "robust", "hallazgo",
    "debilidad", "riesgo", "parche", "patch", "hardening",
}


def _is_analysis_intent(query: str) -> bool:
    """
    Detecta si el usuario quiere análisis/revisión de código,
    no generación de código nuevo.
    Esto es necesario porque el classifier retorna code_generation
    cuando detecta código en el input, pero "analizalo" debería
    ir a _handle_analysis con CODE_REVIEW_SYSTEM_PROMPT.
    """
    if not query:
        return False
    q = query.lower()
    return any(kw in q for kw in _ANALYSIS_INTENT_KEYWORDS)


# ═══════════════════════════════════════════════════════════════
# ✅ v1.6.0: Detección de intent de comando de sistema para routing
# override — classifier_core.py no tiene NINGÚN patrón en KEYWORD_MAP
# para system_control/app_launch/volume_control (confirmado por grep
# exhaustivo — ADAPTIVE_THRESHOLDS los espera como output posible, pero
# nada los produce). Sin este override, "listá los archivos en .",
# "abrí nautilus", etc. caen a general_chat/web_search y nunca llegan a
# SystemAgent.
# ═══════════════════════════════════════════════════════════════
def _is_system_command_intent(query: str) -> bool:
    """
    True si la primera palabra de la query es un verbo de SystemAgent
    sin colisión conocida con el classifier — ver DISPATCHER_ROUTING_VERBS
    en system_agent.py (el comentario ahí lista las 3 categorías excluidas
    por colisión con KEYWORD_MAP o por depender de contexto de sesión:
    write_file/take_screenshot/verdict_*).

    Mismo criterio que SystemAgent._parse_intent(): match EXACTO de
    palabra, no substring — "listá" matchea, "listado" no. A diferencia
    de _parse_intent (que tolera relleno antes del verbo, "por favor
    eliminá X"), acá solo se mira la PRIMERA palabra — más estricto a
    propósito, para minimizar falsos positivos a nivel de ruteo, donde
    una clasificación equivocada es más cara que un parámetro mal
    extraído dentro de un agente ya elegido.
    """
    if not query:
        return False
    words = query.strip().split()
    if not words:
        return False
    first = words[0].strip(".,!?:;¿¡").lower()
    try:
        from src.agents.system_agent import DISPATCHER_ROUTING_VERBS
    except ImportError:
        return False
    return first in DISPATCHER_ROUTING_VERBS


# ═══════════════════════════════════════════════════════════════
# ✅ NUEVO (hallazgo agente filesystem v1.6.0, pendiente 2): igual que
# _is_system_command_intent() pero para aprobar/descartar — estos NO
# están en DISPATCHER_ROUTING_VERBS (ver comentario ahí) porque forzar
# task_type=system_control por la sola palabra es semánticamente
# incorrecto sin propuesta pendiente ("aprobar el balance" no es un
# veredicto). Acá se agrega la condición que falta: solo pisa si
# PromotionGate.has_pending(session_id) es True para ESA sesión.
# "corregir"/"corregí" quedan afuera incluso con propuesta pendiente —
# siguen colisionando con CODE_CORRECTION en KEYWORD_MAP y no hay forma
# de distinguir "corregí esto" (código) de "corregí" (veredicto) solo
# con la primera palabra + pendiente, sin mirar si el resto del mensaje
# tiene contenido de código. Sigue sin resolver, a propósito.
# ═══════════════════════════════════════════════════════════════
_VERDICT_ROUTING_VERBS: frozenset = frozenset({
    "aprobar", "apruebo", "aprobá",
    "descartar", "descartá",
})


def _is_verdict_intent(query: str, session_id: Optional[str]) -> bool:
    """
    True si la primera palabra es aprobar/descartar (match exacto, mismo
    criterio que _is_system_command_intent) Y la sesión tiene una
    propuesta de PromotionGate esperando veredicto.
    """
    if not query or not session_id:
        return False
    words = query.strip().split()
    if not words:
        return False
    first = words[0].strip(".,!?:;¿¡").lower()
    if first not in _VERDICT_ROUTING_VERBS:
        return False
    try:
        from src.core.promotion_gate import get_promotion_gate
    except ImportError:
        return False
    return get_promotion_gate().has_pending(session_id)


def _code_trace_fields(response: Any) -> Dict[str, Any]:
    """
    ✅ Fase 7: extrae los 4 campos de contrato de código desde un
    Response para pasarlos a _record_metric() → TraceStore. Centralizado
    acá porque hay 3 call sites de _record_metric() en process() (dos de
    ellos en paths de cache de clasificación que igual llaman a
    _dispatch() fresco — sí pueden traer code_artifact/validation_report
    reales). Para respuestas sin contrato (CODE_EXPLANATION/CODE_REVIEW/
    weather/chat/etc.) devuelve todo en su default (False/0) — mismo
    comportamiento que no pasar estos kwargs.
    """
    meta = getattr(response, "metadata", None) or {}
    code_artifact = meta.get("code_artifact")
    return {
        "code_contract_complete": bool(code_artifact and code_artifact.get("complete")),
        "code_validation_passed": bool(getattr(response, "validation_passed", False)),
        "code_retries":           int(meta.get("validation_retries", 0) or 0),
        "code_truncated":         bool(meta.get("was_truncated", False)),
        # ✅ ISSUE-082: uso real de tokens. Lo llenan los agentes
        # (`AgentResult.__post_init__` ← `BaseAgent._generate`) y los handlers del
        # dispatcher (`tokens_de_respuesta(ollama_res)`); sin esto las columnas
        # `tokens_prompt`/`tokens_generated` de `traces.db` quedaban en 0 en las
        # 741 filas y la latencia no se podía atribuir.
        "tokens_prompt":          int(meta.get("tokens_prompt", 0) or 0),
        "tokens_generated":       int(meta.get("tokens_generated", 0) or 0),
    }



def _extract_val_score(val_result: Any) -> Optional[float]:
    """Score del validador (0-10) desde su resultado, si viene numérico.

    El validador devuelve el score en `metadata["score"]` (ver
    `ValidatorAgent.validate_response`); cualquier otra forma se ignora para no
    calibrar con datos inventados.
    """
    try:
        for contenedor in (val_result, (val_result or {}).get("metadata") or {}):
            score = contenedor.get("score") if isinstance(contenedor, dict) else None
            if isinstance(score, (int, float)):
                return float(score)
        return None
    except Exception as e:
        log_degraded(logger, e, "src/core/dispatcher_process.py:_extract_val_score")
        return None

class DispatcherProcessMixin(DispatcherProcessStepsMixin, DispatcherProcessGuardMixin):
    """
    Mixin con el pipeline de procesamiento de requests y el router interno.
    Se combina con Dispatcher (dispatcher.py), que aporta los recursos:
    self.classifier, self.coder, self._routing_cache, self._sem_*, etc.
    """

    def _lanzar_validacion_en_fondo(
        self, pregunta: str, response: Response, task_type: str,
    ) -> None:
        """Valida la respuesta SIN bloquear (medido: 41-43 s en CPU).

        El veredicto del validador **no cambia la respuesta** (si falla, se
        conserva la original: es single-pass), así que hacer esperar al usuario
        por él sólo agrega latencia. Acá se lanza en segundo plano y cuando
        termina deja el resultado en `response.metadata` (para el panel de
        transparencia) y en el QualityMonitor (telemetría).

        Marcas honestas para el panel: `validation_en_fondo` mientras corre y
        `validation_en_fondo_completada` cuando termina; si el veredicto es FAIL
        se agrega `validation_failed` (que es lo que ya leía la UI).
        """
        try:
            if not isinstance(getattr(response, "metadata", None), dict):
                response.metadata = {}
            response.metadata["validation_en_fondo"] = True
        except Exception as _me:
            log_degraded(logger, _me, "src/core/dispatcher_process.py:validation_en_fondo(marca)")

        async def _validar() -> None:
            try:
                _res = await asyncio.to_thread(
                    self.validator.validate_response, pregunta, response.content
                )
            except Exception as _ve:
                log_degraded(logger, _ve,
                             "src/core/dispatcher_process.py:validation_en_fondo(validar)")
                return
            _score = _extract_val_score(_res)
            try:
                md = response.metadata if isinstance(response.metadata, dict) else {}
                md["validation_en_fondo_completada"] = True
                if _score is not None:
                    md["validation_score"] = _score
                if not (_res or {}).get("valid", True):
                    md["validation_failed"] = True
                    md["validation_single_pass"] = True
                    logger.warning(
                        "[VALIDATOR] single-pass (en segundo plano): FAIL con score "
                        f"{(_res or {}).get('metadata', {}).get('score', 0)} — "
                        "se conserva la respuesta original (sin re-generar)"
                    )
                    _log_event("DISPATCHER", "Chat validation FAILED — keeping original (single-pass)", extra={
                        "task_type": task_type,
                        "reason": "no_regeneration",
                        "critique": str((_res or {}).get("metadata", {}).get("critique", ""))[:200],
                        "score": (_res or {}).get("metadata", {}).get("score", 0),
                        "en_segundo_plano": True,
                    })
                response.metadata = md
            except Exception as _d2e:
                log_degraded(logger, _d2e,
                             "src/core/dispatcher_process.py:validation_en_fondo(metadata)")

            # ✅ v0.6.9d: QualityMonitor con el score real, ahora sin bloquear.
            if isinstance(_score, (int, float)):
                try:
                    await self._qm_record(task_type, "validator", float(_score))
                except Exception as _qm_e:
                    log_degraded(logger, _qm_e,
                                 "src/core/dispatcher_process.py:validation_en_fondo(qm)")

        try:
            _tarea = asyncio.create_task(_validar())
            # Referencia viva: una tarea sin referencia puede recolectarse antes
            # de terminar (y perderíamos el veredicto en silencio).
            tareas = getattr(self, "_tareas_validacion", None)
            if tareas is None:
                tareas = set()
                self._tareas_validacion = tareas
            tareas.add(_tarea)
            _tarea.add_done_callback(tareas.discard)
        except Exception as _te:
            log_degraded(logger, _te,
                         "src/core/dispatcher_process.py:validation_en_fondo(lanzar)")

    def _guardar_en_cache_respuesta(self, clave: Optional[str], response: Any,
                                    task_type: str) -> None:
        """Guarda la respuesta en la caché de RAM (responsabilidad única).

        Se usa en los DOS caminos que producen una respuesta: el normal (al final
        del pipeline) y el del acierto semántico parcial de la L3. Sin esto, una
        pregunta que ya estaba en el índice semántico **nunca** llenaba la caché de
        RAM (medido el 2026-10-03: 0 entradas tras responder) y los repetidos
        volvían a pasar por el modelo.
        """
        if not clave:
            return
        try:
            _ttl = self._response_cache_ttl.get(task_type, self._response_cache_default_ttl)
            with self._response_cache_lock:
                if len(self._response_cache) >= self._response_cache_max:
                    self._response_cache.popitem(last=False)   # evict LRU real
                self._response_cache[clave] = {
                    "response": response,
                    "task_type": task_type,
                    "ts": time.time(),
                    "ttl": _ttl,
                }
                self._response_cache.move_to_end(clave)        # refresco de recencia
        except Exception as e:
            log_degraded(logger, e, "src/core/dispatcher_process.py:_guardar_en_cache_respuesta")

    async def process(
        self,
        message: Message,
        session_id: Optional[str] = None,
        context=None,  # Optional[Union[str, SessionContext]] — backward compatible
    ) -> Response:
        start_time = datetime.now(timezone.utc)
        query_preview = message.content[:100] if message.content else ""

        # ✅ PLAN v2.0 (Fase 3): gate anti-injection sobre el input DIRECTO,
        # al inicio REAL del pipeline — ANTES de _response_cache, _routing_cache
        # y el hit L3 semántico (no "antes de classify()", que queda después
        # de los tres caches). Si bloquea, se devuelve un Response con
        # input_blocked y no se toca ningún cache ni LLM.
        if settings.input_screening_enabled:
            blocked_reason = self._screen_input(message.content)
            if blocked_reason:
                _log_event("DISPATCHER", "Input blocked by anti-injection gate", extra={
                    "session_id": session_id,
                    "reason": blocked_reason,
                    "query_preview": query_preview,
                })
                logger.warning(
                    f"[Dispatcher] Input bloqueado (anti-injection): {blocked_reason}"
                )
                return Response(
                    content=(
                        "⛔ No puedo procesar ese pedido: contiene un intento de "
                        "manipulación del prompt del sistema. Reformulá la consulta."
                    ),
                    session_id=session_id,
                    model_used="none",
                    task_type="input_blocked",
                    latency_ms=0.0,
                    confidence=0.0,
                    sources=[],
                    validation_passed=False,
                    error=f"input_blocked: {blocked_reason}",
                    metadata={"input_blocked": True, "reason": blocked_reason},
                )

        # ✅ FASE 3 (plan_harness): publicar evento de request entrante.
        try:
            from src.core.event_bus import get_event_bus as _bus
            from src.domain.events import EventTypes as _ET
            _bus().publish_simple(
                _ET.DISPATCH_STARTED,
                {
                    "session_id": session_id,
                    "chars": len(message.content or ""),
                    "query_preview": query_preview,
                },
                source="dispatcher",
            )
        except Exception as e:  # noqa: BLE001
            log_degraded(logger, e, "src/core/dispatcher_process.py:process")

        # ── Response cache: hit → devolver respuesta sin tocar LLM ────────────
        # ✅ MEDIDO (2026-10-03): la clave era SÓLO el texto de la pregunta, así que
        # el mismo texto en otra conversación devolvía la respuesta vieja (con el
        # historial inyectado desde ISSUE-130 eso es contaminación: el usuario la
        # reportó). Ahora la clave incluye **el historial** y **el modelo**: cambia
        # cualquiera de los dos y la entrada ya no aplica.
        _ctx_firma = self._firma_contexto(session_id, message.content)
        _rcache_key = hashlib.sha256(
            f"{message.content}\x00{_ctx_firma}\x00{settings.llm_default_model}".encode()
        ).hexdigest()
        if not getattr(settings, "cache_respuestas_enabled", True):
            _rcache_key = None      # caché de respuesta desactivada por configuración
        with self._response_cache_lock:
            _rcache_entry = self._response_cache.get(_rcache_key) if _rcache_key else None
            if _rcache_entry and (time.time() - _rcache_entry["ts"]) < _rcache_entry["ttl"]:
                self._response_cache.move_to_end(_rcache_key)  # ✅ FIX: LRU real
                _log_event("DISPATCHER", "Response cache HIT", extra={
                    "session_id": session_id,
                    "task_type": _rcache_entry["task_type"],
                    "cache": "response_ram",
                })
                cached_resp = _rcache_entry["response"]
                return Response(
                    content=cached_resp.content,
                    session_id=session_id,
                    model_used=cached_resp.model_used,
                    task_type=cached_resp.task_type,
                    latency_ms=0.0,
                    confidence=cached_resp.confidence,
                    sources=getattr(cached_resp, 'sources', []),
                    metadata={**(getattr(cached_resp, 'metadata', {}) or {}), "from_response_cache": True},
                )

        # LRU cache hit (RoutingCache de dispatcher_cache_metrics — clasificación exacta)
        if self._routing_cache:
            cached = self._routing_cache.get(message.content)
            if cached:
                task_type = cached["task_type"]
                handler = cached["handler"]
                confidence = cached["confidence"]
                _log_event("DISPATCHER", f"Routing cache HIT: {task_type}", extra={
                    "session_id": session_id, "cache_hit": True,
                })
                response = await self._dispatch(message, task_type, session_id, context)
                latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                self._record_metric(
                    task_type, handler, latency, response.error is None,
                    **_code_trace_fields(response),
                )
                return response

        # ── ✅ v0.9.3 FIX L3 B-HYBRID: L3 como índice semántico de RAM ─────
        # ✅ MEDIDO (2026-10-03): la L3 servía respuestas de preguntas DISTINTAS por
        # umbral bajo (0.60; dos preguntas distintas puntúan 0.603-0.635 medidos con
        # bge-m3) y tampoco miraba la conversación. Ahora: (a) umbral configurable con
        # default 0.85, (b) **sólo sin historial** — una respuesta contextual nunca
        # sale de un atajo semántico — y (c) se puede apagar por configuración.
        if (_ROUTER_CACHE_AVAILABLE and not _ctx_firma
                and getattr(settings, "cache_semantica_enabled", True)):
            try:
                _rc = _get_router_cache()
                _rc_result = _rc.get(message.content)
                if _rc_result is not None:
                    ram_ptr = _rc_result.get("response_cache_key", "")
                    if ram_ptr:
                        with self._response_cache_lock:
                            cached_entry = self._response_cache.get(ram_ptr)
                            if cached_entry and (time.time() - cached_entry["ts"]) < cached_entry["ttl"]:
                                self._response_cache.move_to_end(ram_ptr)  # ✅ FIX: LRU real
                                cached_resp = cached_entry["response"]
                                _log_event("DISPATCHER", f"L3 Hybrid FULL HIT: {cached_resp.task_type}", extra={
                                    "session_id": session_id,
                                    "cache_hit": True,
                                    "cache_tier": "l3_semantic_ram",
                                })
                                logger.info(f"L3 Hybrid FULL HIT (RAM lookup): task={cached_resp.task_type} query='{query_preview}'")
                                return Response(
                                    content=cached_resp.content,
                                    session_id=session_id,
                                    model_used=cached_resp.model_used,
                                    task_type=cached_resp.task_type,
                                    latency_ms=0.0,
                                    confidence=cached_resp.confidence,
                                    sources=getattr(cached_resp, 'sources', []),
                                    metadata={**(getattr(cached_resp, 'metadata', {}) or {}), "from_l3_cache": True},
                                )

                    task_type = _rc_result.get("task_type", "general_chat")
                    confidence = _rc_result.get("confidence", 0.8)
                    _log_event("DISPATCHER", f"L3 Semantic PARTIAL HIT (fallback to handler): {task_type}", extra={
                        "session_id": session_id,
                        "cache_hit": True,
                        "cache_tier": "l3_semantic_fallback",
                    })
                    response = await self._dispatch(message, task_type, session_id, context)
                    # ✅ 2026-10-03: este camino también GUARDA en la caché de RAM.
                    # Antes volvía sin guardar, así que una pregunta que estaba en el
                    # índice semántico no llenaba nunca la caché (medido: 0 entradas)
                    # y los repetidos volvían a pasar por el modelo.
                    self._guardar_en_cache_respuesta(_rcache_key, response, task_type)
                    # ✅ 2026-10-03 (hallazgo): y también GUARDA EL INTERCAMBIO EN LA
                    # MEMORIA. Antes se volvía sin `_record_exchange`, así que una
                    # sesión cuyas preguntas estaban en el índice semántico quedaba
                    # **sin historial** (medido: 0 mensajes tras un intercambio real)
                    # ⇒ el chat "no recordaba" y la clave de caché perdía el contexto.
                    self._record_exchange(message, response, session_id, task_type)
                    latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                    self._record_metric(
                        task_type, self._handler_name(task_type),
                        latency, response.error is None,
                        **_code_trace_fields(response),
                    )
                    return response
            except Exception as e:
                log_degraded(logger, e, "src/core/dispatcher_process.py:process")

        _log_event("DISPATCHER", f"Processing: {query_preview}...", extra={
            "session_id": session_id,
            "has_context": context is not None,
            "content_length": len(message.content) if message.content else 0,
        })

        # ═══════════════════════════════════════════════════════════════
        # ✅ v0.6.8rr (F3 opt-in, PLAN_ADAPTADO.md FASE 3): comando
        # "/plan <consulta>" — pipeline multi-paso explícito.
        #
        # Se resuelve ANTES del clasificador LLM (un /plan no es una
        # pregunta, es una orden de pipeline; no paga 15 s de clasificación).
        # La descomposición es heurística y gratis (TaskDecomposer
        # llm_refine=False); el coste real son las generaciones del
        # pipeline (≤ settings.plan_max_steps, default 2) — por eso es
        # OPT-IN: solo corre cuando el usuario lo pide con /plan o cuando
        # settings.plan_pipeline_enabled=True (ver abajo).
        # ═══════════════════════════════════════════════════════════════
        if message.content:
            _stripped_q = message.content.strip()
            _plan_lower = _stripped_q.lower()
            if _plan_lower == "/plan" or _plan_lower.startswith("/plan "):
                _plan_query = _stripped_q[5:].strip() or None
                if not _plan_query:
                    return Response(
                        content="Uso: `/plan <consulta compleja>` — descompongo "
                                "la tarea en pasos (≤2 en CPU) y ejecuto el "
                                "pipeline. Ej: `/plan analizá tools/netron y "
                                "generá un reporte de mejoras`.",
                        session_id=session_id,
                        model_used="planner",
                        task_type="code_autonomous",
                        latency_ms=0.0,
                        confidence=0.9, validation_passed=True, error=None,
                    )
                try:
                    _plan_resp = await self._run_plan_pipeline(
                        _plan_query, session_id, task_type_value="code_autonomous",
                    )
                    if _plan_resp is not None:
                        _log_event("DISPATCHER", "Request completed: /plan (pipeline)", extra={
                            "handler": "PlannerPipeline",
                            "latency_ms": round(_plan_resp.metadata.get("plan_latency_ms", 0.0), 2),
                            "content_length": len(_plan_resp.content or ""),
                            "success": _plan_resp.error is None,
                        })
                        self._record_metric(
                            "code_autonomous", "PlannerPipeline",
                            _plan_resp.metadata.get("plan_latency_ms", 0.0),
                            _plan_resp.error is None,
                        )
                        self._record_exchange(
                            message, _plan_resp, session_id, "code_autonomous",
                        )
                        return _plan_resp
                    # consulta atómica (1 paso) → procesar la consulta real
                    # por la ruta normal, sin el prefijo /plan
                    logger.info(
                        f"[PLAN] consulta atómica, sin pipeline: {_plan_query[:60]}"
                    )
                    message.content = _plan_query
                except Exception as _plan_e:
                    _log_error(_plan_e, "process._plan", extra_info={
                        "query": _plan_query[:80], "session_id": session_id,
                    })
                    return self._error_response(
                        f"Error en /plan: {_plan_e}", session_id, start_time,
                    )

        try:
            classify_start = time.time()

            _session_ctx, context = self._contexto_de_sesion(context, session_id)

            # ═══════════════════════════════════════════════════════════════
            # ✅ FIX AUDITORÍA v1.3.3 — L1: classify()/classify_with_context()
            # ejecutan _classify_by_llm() con httpx.Client SÍNCRONO (timeout 8s)
            # directo a Ollama. Sin este to_thread, cada request no-cacheado/
            # no-saludo bloqueaba el event loop COMPLETO del daemon durante
            # hasta 8s — ninguna otra sesión concurrente avanzaba mientras tanto.
            # Mismo patrón de asyncio.to_thread ya usado en el resto del archivo.
            # ═══════════════════════════════════════════════════════════════
            # ═══════════════════════════════════════════════════════════════
            # ✅ v0.6.8qq: PRE-ROUTING determinístico — ANTES del clasificador
            # LLM (~15 s en CPU con qwen3:8b)
            #
            # PROBLEMA (logs 15:26): "usá fs_search para…" pagaba ~15 s de
            # clasificación LLM (→ web_search) para que el override
            # post-clasificación lo corrigiera a file_search. "analizá la
            # carpeta X" y los comandos de sistema (1ª palabra en
            # DISPATCHER_ROUTING_VERBS) idem.
            #
            # FIX: las intenciones DETERMINÍSTICAS (fs_* / contenido de
            # archivos, análisis de carpeta, comando de sistema) no necesitan
            # LLM para clasificar: se resuelven en µs y se salta la generación
            # del clasificador. Los overrides post-clasificación quedan
            # intactos para todo lo demás.
            # ═══════════════════════════════════════════════════════════════
            task_type, confidence = await self._preclasificar(
                message, _session_ctx, classify_start
            )

            # ═══════════════════════════════════════════════════════════════
            # ✅ v1.0.6: Routing override — código + intent de análisis
            #
            # PROBLEMA: El classifier retorna code_generation cuando detecta
            # código en el input, pero "analizalo" debería ir a _handle_analysis
            # con CODE_REVIEW_SYSTEM_PROMPT.
            #
            # FIX: Si el mensaje contiene código Y el usuario expresa intent
            # de análisis, sobreescribir task_type a code_review antes del
            # dispatch. Esto hace que vaya a _handle_analysis en vez de
            # _handle_code, usando:
            #   - CODE_REVIEW_SYSTEM_PROMPT (review riguroso)
            #   - num_ctx dinámico (archivo completo)
            #   - max_tokens=8192 (respuesta larga)
            #   - llama3.1-8b (rápido, 60s vs 547s)
            # ═══════════════════════════════════════════════════════════════
            task_type = self._aplicar_guards_de_tipo(message, task_type, session_id)

            # ✅ Semana 2: construir contexto enriquecido antes del routing
            _internal_context, enriched_context = self._construir_contexto_interno(
                message, context
            )

            # ═══════════════════════════════════════════════════════════════
            # ✅ v0.6.8rr (F3 opt-in): pipeline automático — SOLO si
            # settings.plan_pipeline_enabled=True (default False).
            #
            # Tras la clasificación ya sabemos el task_type: se intenta el
            # pipeline solo para consultas DESCOMPONIBLES (≥2 pasos reales) y
            # no-triviales (≥40 chars); si la descomposición es atómica,
            # _run_plan_pipeline devuelve None y sigue la ruta normal (sin
            # costo extra). Nunca aplica a /plan (ya resuelto arriba).
            # ═══════════════════════════════════════════════════════════════
            if (
                getattr(settings, "plan_pipeline_enabled", False)
                and len((message.content or "")) >= 40
            ):
                try:
                    _auto_plan = await self._run_plan_pipeline(
                        message.content, session_id, task_type_value=task_type,
                    )
                except Exception as _ap_e:
                    _auto_plan = None
                    log_degraded(logger, _ap_e, "src/core/dispatcher_process.py:process")
                if _auto_plan is not None:
                    _auto_latency = _auto_plan.metadata.get("plan_latency_ms", 0.0)
                    _log_event("DISPATCHER", "Request completed: auto-plan (F3)", extra={
                        "task_type": task_type,
                        "handler": "PlannerPipeline",
                        "latency_ms": round(_auto_latency, 2),
                        "content_length": len(_auto_plan.content or ""),
                        "success": _auto_plan.error is None,
                    })
                    logger.info(
                        f"[PLAN] auto-pipeline activo para {task_type} "
                        f"({_auto_plan.metadata.get('plan_pipeline')})"
                    )
                    self._record_metric(
                        task_type, "PlannerPipeline", _auto_latency,
                        _auto_plan.error is None,
                    )
                    self._record_exchange(
                        message, _auto_plan, session_id, task_type,
                    )
                    return _auto_plan

            response, handler = await self._despachar(message, task_type, session_id, enriched_context)

            # ── v0.6.2: Validación factual post-dispatch para GENERAL_CHAT ──────
            # El ValidatorAgent usa llm_fallback_1 (qwen2.5-coder:7b) — sin sesgo de
            # auto-confirmación respecto al modelo chat principal (qwen3:8b).
            # ✅ v0.6.8pp: SINGLE-PASS — una sola validación; si falla se conserva
            # la respuesta original (sin re-generar ni re-validar, ver abajo).
            # ✅ ISSUE-061 (bug real, detectado en vivo): estas dos variables las
            # usa la CALIBRACIÓN de confianza de más abajo, así que deben existir
            # SIEMPRE — no solo en la rama donde corre el validador. Estaban
            # declaradas dentro del `else` de la validación, y en los caminos que
            # la omiten (respuesta corta, grounding, tarea sin validación) la
            # calibración levantaba UnboundLocalError (quedaba degradado a
            # WARNING y la confianza no se calibraba).
            _val_score: Optional[float] = None
            _val_failed = False
            self._validar_chat_si_corresponde(message, response, task_type)

            self._calibrar_confianza(
                response,
                validator_score=_val_score,
                validation_failed=_val_failed,
                task_type=task_type,
                message=message,
            )

            # ✅ Semana 2 B: aprender de la interacción — background thread
            if self._user_profile and response.error is None:
                try:
                    asyncio.create_task(
                        asyncio.to_thread(
                            self._user_profile.learn_from_interaction,
                            message.content,
                            response.content or "",
                            task_type,
                        )
                    )
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

            # ✅ Fase 6 — FeedbackLoop: registrar señal implícita ROUTED
            await self._registrar_senal_feedback(
                message,
                response,
                task_type,
                session_id,
                validator_score=_val_score,
                validation_failed=_val_failed,
            )

            # ✅ v0.9.0: Learning loop periódico — cada N requests cierra el loop
            # de aprendizaje: registros positivos del FeedbackLoop → add_example()
            # del clasificador. Fire-and-forget via create_task, no bloquea process().
            self._registrar_metricas_y_cierre(
                start_time=start_time,
                query_preview=query_preview,
                task_type=task_type,
                handler=handler,
                confidence=confidence,
            )

            # ✅ Fase 0.5 (H5): para CODE_TASKS, exige validation_passed real
            # antes de cachear (ahora seteado por CoderAgent, ver H4). No se
            # aplica a otros task_types: validate() de BaseAgent (el único
            # lugar que setea validation_passed genéricamente) solo se
            # invoca desde execute_with_retry_async(), que nada llama en el
            # path real — gatear el cache global con esto rompería weather/
            # chat/search, que nunca pasan por ahí.
            _needs_validation = task_type in self.CODE_TASKS
            if _rcache_key and response.error is None and response.content and (
                not _needs_validation or response.validation_passed
            ):
                # ✅ Clave = pregunta + historial + modelo (ver arriba), y el guardado
                # vive en un helper compartido con el camino de la L3.
                self._guardar_en_cache_respuesta(_rcache_key, response, task_type)

            # ✅ CAAC Fase 0: cache contextual (clave compuesta) + stateless fallback
            if self._routing_cache and confidence >= 0.7:
                if _session_ctx and _session_ctx.last_intent and session_id:
                    ctx_key = (
                        f"ctx:{session_id[:8]}:"
                        f"{_session_ctx.last_intent}:"
                        f"{hashlib.sha256(message.content.encode()).hexdigest()[:16]}"
                    )
                    self._routing_cache.set(
                        ctx_key,
                        {"task_type": task_type, "handler": handler, "confidence": confidence},
                    )
                else:
                    self._routing_cache.set(
                        message.content,
                        {"task_type": task_type, "handler": handler, "confidence": confidence},
                    )

            # ✅ FIX L3 B-HYBRID: propagar puntero al RouterCache/L3 semántico.
            # ✅ MEDIDO (2026-10-03): sólo se indexa cuando la conversación NO tiene
            # historial — si no, una respuesta contextual quedaría disponible para
            # preguntas parecidas de otras conversaciones (contaminación).
            if (response.error is None and confidence >= 0.7 and _ROUTER_CACHE_AVAILABLE
                    and _rcache_key and not _ctx_firma
                    and getattr(settings, "cache_semantica_enabled", True)):
                try:
                    _rc = _get_router_cache()
                    _rc_value = {
                        "task_type": task_type,
                        "handler": handler,
                        "confidence": confidence,
                        "response_cache_key": _rcache_key,
                    }
                    # ✅ FIX AUDITORÍA: ThreadPoolExecutor acotado en vez de
                    # threading.Thread(daemon=True) nuevo por request — ver
                    # comentario en __init__.
                    self._l3_write_executor.submit(
                        _rc.set, message.content, _rc_value, level=1,
                    )
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

            # ✅ CAAC Fase 0: actualizar SessionContext en store
            if _SESSION_CONTEXT_AVAILABLE and session_id and response.error is None:
                with self._session_store_lock:
                    existing = self._session_store.get(session_id)
                    if existing is not None:
                        existing.update(task_type)
                    elif _SessionContext is not None:
                        new_ctx = _SessionContext(session_id=session_id)
                        new_ctx.update(task_type)
                        self._session_store[session_id] = new_ctx

            if hasattr(response, 'metadata') and response.metadata is not None:
                response.metadata["classification_confidence"] = confidence
                # ✅ v0.6.9v (P0 del plan nuevo): quién atendió la consulta.
                # Mismo `_handler_name(task_type)` que ya se usa para la métrica
                # ROUTED → el panel de transparencia y el feedback EXPLÍCITO
                # usan la misma identidad de agente.
                response.metadata["agent_role"] = handler

            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            logger.info(f"Request completed in {latency:.2f}ms → {handler}")
            _log_event("DISPATCHER", f"Request completed: {task_type}", extra={
                "handler": handler,
                "latency_ms": round(latency, 2),
                "content_length": len(response.content) if response.content else 0,
                "success": response.error is None,
            })
            self._record_metric(
                task_type, handler, latency, response.error is None,
                **_code_trace_fields(response),
            )

            # ✅ plan_memory (B): write-path centralizado — persiste el
            # intercambio (user + assistant) en la memoria de la sesión.
            # Punto único de escritura: los agentes leen esta misma sesión
            # vía _get_context(session_id=...).
            self._record_exchange(message, response, session_id, task_type)

            return response

        except Exception as e:
            tb = traceback.format_exc()
            _log_error(e, "process", extra_info={
                "query": query_preview,
                "session_id": session_id,
                "traceback": tb,
            })
            logger.error(f"Dispatcher error: {e}", exc_info=True)
            return self._error_response(str(e), session_id, start_time)

    # ── plan_memory (B): write-path de memoria por sesión ─────────────────────

    async def _dispatch(
        self,
        message: Message,
        task_type: str,
        session_id: Optional[str],
        context: Optional[str],
    ) -> Response:
        """Router interno — delega al handler correcto con control de concurrencia."""

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX AUDITORÍA v0.3.6 — CIRCUIT_BREAK real
        #
        # Antes: _record_metric() (dispatcher_operations.py) solo escribía
        # una key __fallback_{handler} en routing_cache cuando self_healing
        # sugería CIRCUIT_BREAK. Ningún archivo leía esa key — el "corte"
        # era cosmético, el handler seguía recibiendo tráfico normal.
        #
        # Ahora: se consulta el estado en vivo del mismo SelfHealingMonitor
        # (misma key `handler` que usa _record_metric() vía _handler_name())
        # ANTES de tomar semáforo o invocar al handler. Si sugiere
        # CIRCUIT_BREAK, se rechaza rápido con degradación explícita —
        # esto sí reduce la carga real sobre un handler en estado CRITICAL
        # y le da margen para recuperarse (ventana deslizante de self_healing).
        # ═══════════════════════════════════════════════════════════════
        try:
            from src.core.self_healing import get_monitor as _get_healing_monitor_live
            _handler_check = self._handler_name(task_type)
            _action = _get_healing_monitor_live().suggest_action(_handler_check)
            _action_value = _action.value if hasattr(_action, 'value') else str(_action)
            if _action_value == "circuit_break":
                logger.warning(f"[HEALING] CIRCUIT_BREAK activo — rechazando dispatch a {_handler_check}")
                _log_event("DISPATCHER", f"CIRCUIT_BREAK: dispatch rechazado ({_handler_check})", level="WARNING", extra={
                    "handler": _handler_check,
                    "task_type": task_type,
                    "session_id": session_id,
                })
                return Response(
                    content=(
                        f"⚠️ El componente **{_handler_check}** está temporalmente fuera de "
                        f"servicio por fallos recientes. Probá de nuevo en unos segundos."
                    ),
                    session_id=session_id,
                    model_used="self_healing",
                    task_type=task_type,
                    latency_ms=0.0,
                    confidence=0.0,
                    error="circuit_break_open",
                )
        except ImportError as _d2e:
            logger.debug(f"[D2 dispatcher_process.py:1162] excepción degradada (intencional/silenciosa): {_d2e}")
        except Exception as _healing_exc:
            # ✅ FIX: el except original solo cubría ImportError. _record_metric()
            # (dispatcher_operations.py) usa deliberadamente except Exception para
            # estas mismas llamadas — "self-healing nunca bloquea el sistema". Acá,
            # con solo ImportError, un bug interno de suggest_action()/get_monitor()
            # (no un import faltante) se propagaba sin control y podía romper CADA
            # dispatch — exactamente lo opuesto de lo que self-healing debe garantizar.
            log_degraded(logger, _healing_exc, "src/core/dispatcher_process.py:_dispatch")

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX VRAM LOCK: Rechazar tareas LLM si RAC está corriendo
        # ═══════════════════════════════════════════════════════════════
        try:
            from src.core.vram_lock import is_vram_locked, get_vram_lock_reason
            _LOCK_AVAILABLE = True
        except ImportError:
            _LOCK_AVAILABLE = False

        if _LOCK_AVAILABLE:
            # Tareas que requieren LLM y por tanto VRAM
            # ✅ v1.3.0: location_query NO requiere LLM (usa Geolocator directamente)
            llm_tasks = self.CODE_TASKS | self.SEARCH_TASKS | self.ANALYSIS_TASKS | self.VALIDATION_TASKS | self.VISION_TASKS | self.JARVIS_TASKS | {"general_chat", "code_review"}
            if is_vram_locked() and task_type in llm_tasks:
                reason = get_vram_lock_reason()
                logger.warning(f"Dispatch bloqueado — VRAM bloqueada por: {reason}")
                return Response(
                    content=f"🚧 El sistema está ejecutando **{reason}**. Las tareas de IA están temporalmente pausadas para evitar saturación de memoria. Intenta de nuevo en unos minutos.",
                    session_id=session_id,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=0.0,
                    confidence=1.0,
                    error=None
                )

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.0.6: code_review → _handle_analysis
        #
        # Cuando el routing override en process() cambia task_type a
        # code_review, este debe ir a _handle_analysis (que usa
        # CODE_REVIEW_SYSTEM_PROMPT), NO a _handle_code (que usa
        # CoderAgent con streaming y modelo de 14B).
        # ═══════════════════════════════════════════════════════════════
        if task_type == "code_review":
            async with self._sem_medium:
                return await self._handle_analysis(message, task_type, session_id, context)

        if task_type in self.DATA_TASKS:
            # ✅ v1.3.0: location_query usa su propio handler, no DataHandler
            if task_type == "location_query":
                async with self._sem_fast:
                    return await self._handle_location(message, task_type, session_id, context)
            async with self._sem_fast:
                return await self._handle_data(message, task_type, session_id)

        if task_type in self.CODE_TASKS:
            async with self._sem_slow:
                return await self._handle_code(message, task_type, session_id, context)

        if task_type in self.SEARCH_TASKS:
            async with self._sem_medium:
                return await self._handle_search(message, task_type, session_id, context)

        # ✅ plan_rafael (2026-08-31): búsqueda de archivos/carpetas en disco.
        if task_type in self.FILE_TASKS:
            async with self._sem_fast:
                return await self._handle_file_search(message, task_type, session_id, context)

        if task_type in self.ANALYSIS_TASKS:
            async with self._sem_medium:
                return await self._handle_analysis(message, task_type, session_id, context)

        if task_type in self.VALIDATION_TASKS:
            async with self._sem_medium:
                return await self._handle_validation(message, task_type, session_id, context)

        if task_type in self.SYSTEM_TASKS:
            async with self._sem_fast:
                return await self._handle_system(message, task_type, session_id)

        if task_type in self.VISION_TASKS:
            async with self._sem_medium:
                return await self._handle_screenshot_analysis(message, task_type, session_id, context)

        if task_type in self.JARVIS_TASKS:
            async with self._sem_fast:
                return await self._handle_jarvis(message, task_type, session_id, context)

        async with self._sem_fast:
            return await self._handle_chat(message, task_type, session_id, context)
