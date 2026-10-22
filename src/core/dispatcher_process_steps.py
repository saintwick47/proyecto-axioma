#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — dispatcher_process_steps.py  (REFACTORIZACIÓN POR PARTES)
# Ruta: src/core/dispatcher_process_steps.py
#
# ✅ 2026-10-13 (PIEZA 7, R14 exceptuado el 2026-10-12 / decisión C4 reabierta):
#   Pasos de `DispatcherProcessMixin.process()` extraídos a este mixin hermano, sin cambios de
#   comportamiento. Mismo patrón que el resto del dispatcher (`dispatcher_process_guard.py`,
#   `dispatcher_cache_metrics.py`, …) y que la refactorización del clasificador.
#
#   Qué se movió desde dispatcher_process.py:
#     - `_preclasificar`  → la pre-clasificación (intents de FS/papelera y de comando de sistema
#                           resueltos SIN LLM; si ninguno aplica, se llama al clasificador con o sin
#                           contexto de sesión). Devuelve (task_type, confidence).
#     - `_construir_contexto_interno` → el contexto interno enriquecido (perfil, pista del sensor,
#                           knowledge graph). Devuelve (internal_context, enriched_context).
#     - `_aplicar_guards_de_tipo` + 3 overrides (`_aplicar_override_codigo`,
#                           `_aplicar_override_file_search`, `_aplicar_override_sistema`): los routing
#                           overrides por tipo de tarea. Son tres métodos separados a propósito: juntos
#                           pasarían de 80 líneas y el auditor los marca «método largo» (LOW).
#
#   `DispatcherProcessMixin` hereda ahora `DispatcherProcessStepsMixin`, así la API pública no cambia.
#
#   Imports: `_log_event` viene DIRECTO de `src.core.dispatcher_handlers` (de donde ya lo toma
#   dispatcher_process.py). Los helpers definidos en dispatcher_process.py (`_is_system_command_intent`,
#   etc.) se importan DIFERIDO dentro del método, para no crear un ciclo.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio

try:  # igual que en dispatcher_process.py: el módulo de contexto puede no estar
    from src.domain.context import SessionContext as _SessionContext
    _SESSION_CONTEXT_AVAILABLE = True
except ImportError:
    _SessionContext = None  # type: ignore[assignment,misc]
    _SESSION_CONTEXT_AVAILABLE = False
import logging
import time
from typing import Any, Dict, Optional, Set, Tuple

from src.core.dispatcher_handlers import _log_event
from src.core.dispatcher_process_guard import calibrate_response_confidence
from src.utils.degradation import log_degraded
from datetime import datetime, timezone
from config.settings import settings
from src.core.dispatcher_handlers import _log_router_decision
from src.domain.entities import Message

# ✅ Fase 6 — FeedbackLoop — import seguro (mismo patrón que dispatcher_process.py)
try:
    from src.core.feedback_loop import ImplicitSignal
except ImportError:  # pragma: no cover
    ImplicitSignal = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ✅ v0.6.8ss/ISSUE-061: tareas de chat que pasan por el validador factual (movida de `process()`).
_CHAT_VALIDATION_TASKS: Set[str] = {"general_chat", "technical_support", "tutoring", "brainstorming"}

class DispatcherProcessStepsMixin:
    """Pasos del pipeline de `process()` (extraídos de DispatcherProcessMixin, pieza 7)."""

    async def _preclasificar(
        self, message: Message, session_ctx: Optional[Any], classify_start: float
    ) -> Tuple[str, float]:
        """Pre-clasificación sin LLM cuando el intent es evidente.

        Devuelve (task_type, confidence). Extraído de `process()` (pieza 7).
        """
        from src.core.dispatcher_process import _is_system_command_intent  # noqa: F401 — diferido
        _pre_task = None
        _pre_reason = None
        try:
            from src.core.dispatcher_handlers import (
                _is_fs_tool_intent as _fsi,
                _is_folder_analysis_query as _fai,
            )
            _qtext = message.content or ""
            if _fsi is not None and _fsi(_qtext):
                _pre_task, _pre_reason = "file_search", "fs_tool_intent"
            elif _fai is not None and _fai(_qtext):
                _pre_task, _pre_reason = "file_search", "folder_analysis_intent"
            elif _is_system_command_intent(_qtext):
                _pre_task, _pre_reason = "system_control", "first_word_system_verb"
        except Exception:
            _pre_task, _pre_reason = None, None

        if _pre_task is not None:
            task_type = _pre_task
            confidence = 0.9
            classify_duration = time.time() - classify_start
            logger.info(
                f"Classified (pre-routing, sin LLM): {task_type} "
                f"(conf=0.90) in {classify_duration * 1000:.2f}ms "
                f"[{_pre_reason}]"
            )
            _log_event("DISPATCHER", f"Classification (pre-routing): {task_type}", extra={
                "task_type": task_type,
                "confidence": confidence,
                "classification_duration_ms": round(classify_duration * 1000, 2),
                "context_used": session_ctx is not None,
                "llm_skipped": True,
                "override_reason": _pre_reason,
            })
            return task_type, confidence
        else:
            if session_ctx is not None:
                classification = await asyncio.to_thread(
                    self.classifier.classify_with_context, message.content, session_ctx
                )
            else:
                classification = await asyncio.to_thread(
                    self.classifier.classify, message.content
                )

            classify_duration = time.time() - classify_start
            task_type = classification.get("task_type", "general_chat")
            confidence = classification.get("confidence", 0.5)

            logger.info(
                f"Classified: {task_type} (conf={confidence:.2f}) "
                f"ctx={'yes' if session_ctx else 'no'} "
                f"in {classify_duration * 1000:.2f}ms"
            )
            _log_event("DISPATCHER", f"Classification: {task_type}", extra={
                "task_type": task_type,
                "confidence": confidence,
                "classification_duration_ms": round(classify_duration * 1000, 2),
                "context_used": session_ctx is not None,
                "context_boost": classification.get("metadata", {}).get("context_boost_applied", False),
            })
            return task_type, confidence

    def _aplicar_override_codigo(self, message: Message, task_type: str) -> str:
        """Código + pedido de análisis → `code_review` (override del clasificador).

        Sólo aplica si `task_type` YA es una tarea de código y el mensaje trae un bloque de código
        multilínea con intención de análisis. `_is_analysis_intent` se importa DIFERIDO (vive en
        `dispatcher_process.py`) para no crear el ciclo.
        """
        from src.core.dispatcher_process import _is_analysis_intent  # noqa: F401 — diferido

        if task_type in self.CODE_TASKS:
            from src.core.dispatcher_handlers import _detect_code_content
            has_code = _detect_code_content(message.content or "")
            wants_analysis = _is_analysis_intent(message.content or "")
            if has_code and wants_analysis:
                original_task = task_type
                task_type = "code_review"
                _log_event("DISPATCHER", "Routing override: code→analysis", extra={
                    "original_task": original_task,
                    "overridden_task": "code_review",
                    "override_reason": "code_content + analysis_intent",
                    "has_code": has_code,
                    "wants_analysis": wants_analysis,
                })
                logger.info(
                    f"[ROUTING OVERRIDE] {original_task} → code_review "
                    f"(code_content={has_code}, analysis_intent={wants_analysis})"
                )

        # ═══════════════════════════════════════════════════════════════
        return task_type

    def _aplicar_override_file_search(self, message: Message, task_type: str) -> str:
        """Análisis de carpeta o tools `fs_*` → `file_search` (para leer el filesystem de verdad).

        Los dos predicados se importan DENTRO del método desde `dispatcher_handlers` (igual que
        antes) y toleran no estar disponibles.
        """
        # ✅ v0.6.8ii: Routing override — análisis de CARPETA con grounding
        #
        # PROBLEMA (logs/reg_error.txt): "analiza la carpeta netron y dime
        # que es" / "quiero saber qué hacen sus archivos" caían a
        # general_chat / code_explanation y el modelo respondía SIN leer el
        # filesystem → divagaba (83-95 s por nada).
        #
        # FIX: si el clasificador no dio file_search pero la consulta pide
        # analizar una carpeta → forzar file_search, cuyo handler lista la
        # carpeta, lee los archivos y resume con grounding (sources).
        # ═══════════════════════════════════════════════════════════════
        if task_type != "file_search":
            try:
                from src.core.dispatcher_handlers import _is_folder_analysis_query
            except Exception:
                _is_folder_analysis_query = None  # type: ignore[assignment]
            if _is_folder_analysis_query is not None and _is_folder_analysis_query(message.content or ""):
                original_task = task_type
                task_type = "file_search"
                _log_event("DISPATCHER", "Routing override: → file_search (folder analysis)", extra={
                    "original_task": original_task,
                    "overridden_task": "file_search",
                    "override_reason": "folder_analysis_intent",
                })
                logger.info(
                    f"[ROUTING OVERRIDE] {original_task} → file_search "
                    f"(análisis de carpeta con grounding)"
                )

        # ═══════════════════════════════════════════════════════════════
        # ✅ v0.6.8pp (C): Routing override — intención fs_* / contenido
        #
        # PROBLEMA (logs/reg_error.txt): "usá fs_search para buscar qué
        # archivos de tools/netron hablan de WAN y listámelos" caía a
        # web_search / general_chat → 43-47 s de síntesis LLM al pedo y el
        # modelo respondía SIN leer el filesystem.
        #
        # FIX: si la consulta nombra tools fs_* o pide archivos cuyo
        # CONTENIDO mencione un tema → forzar file_search. El handler hace
        # grep local del contenido (fs_list + fs_read) sin generar LLM.
        # ═══════════════════════════════════════════════════════════════
        if task_type != "file_search":
            try:
                from src.core.dispatcher_handlers import _is_fs_tool_intent
            except Exception:
                _is_fs_tool_intent = None  # type: ignore[assignment]
            if _is_fs_tool_intent is not None and _is_fs_tool_intent(message.content or ""):
                original_task = task_type
                task_type = "file_search"
                _log_event("DISPATCHER", "Routing override: → file_search (fs_* / content)", extra={
                    "original_task": original_task,
                    "overridden_task": "file_search",
                    "override_reason": "fs_tool_intent",
                })
                logger.info(
                    f"[ROUTING OVERRIDE] {original_task} → file_search "
                    f"(intención fs_* / búsqueda de contenido)"
                )

        # ═══════════════════════════════════════════════════════════════
        return task_type

    def _aplicar_override_sistema(
        self, message: Message, task_type: str, session_id: Optional[str]
    ) -> str:
        """Comando de sistema o veredicto con propuesta pendiente → `system_control`.

        No pisa lo que ya es de `SYSTEM_TASKS`. El veredicto depende de `session_id` (propuesta de
        PromotionGate pendiente), por eso va en un `elif` y no en el guard anterior.
        """
        from src.core.dispatcher_process import (  # noqa: F401 — diferido, evita el ciclo
            _is_system_command_intent, _is_verdict_intent,
        )

        # ✅ v1.6.0: Routing override — comandos de SystemAgent sin patrón
        # en el classifier. Ver _is_system_command_intent() arriba para
        # el criterio exacto y por qué está acotado a un subset seguro
        # de _INTENT_MAP (DISPATCHER_ROUTING_VERBS en system_agent.py).
        # Solo pisa si el classifier no dio ya algo de SYSTEM_TASKS —
        # no hay razón para tocarlo si ya está bien.
        # ═══════════════════════════════════════════════════════════════
        if task_type not in self.SYSTEM_TASKS and _is_system_command_intent(message.content or ""):
            original_task = task_type
            task_type = "system_control"
            _log_event("DISPATCHER", "Routing override: → system_control", extra={
                "original_task": original_task,
                "overridden_task": "system_control",
                "override_reason": "first_word_system_verb",
            })
            logger.info(
                f"[ROUTING OVERRIDE] {original_task} → system_control "
                f"(primera palabra matchea DISPATCHER_ROUTING_VERBS)"
            )

        # ═══════════════════════════════════════════════════════════════
        # ✅ NUEVO (hallazgo agente filesystem v1.6.0, pendiente 2):
        # aprobar/descartar → system_control, solo si hay propuesta de
        # PromotionGate pendiente en esta sesión. Ver _is_verdict_intent()
        # arriba. Chequeo separado del de arriba porque depende de
        # session_id, no solo de la palabra.
        # ═══════════════════════════════════════════════════════════════
        elif task_type not in self.SYSTEM_TASKS and _is_verdict_intent(message.content or "", session_id):
            original_task = task_type
            task_type = "system_control"
            _log_event("DISPATCHER", "Routing override: → system_control", extra={
                "original_task": original_task,
                "overridden_task": "system_control",
                "override_reason": "verdict_word_with_pending_proposal",
            })
            logger.info(
                f"[ROUTING OVERRIDE] {original_task} → system_control "
                f"(veredicto con propuesta pendiente en sesión)"
            )
        return task_type

    def _aplicar_guards_de_tipo(
        self, message: Message, task_type: str, session_id: Optional[str]
    ) -> str:
        """Los routing overrides por tipo de tarea, en orden (extraídos de `process()`, pieza 7).

        Se probó la forma de un solo método y el auditor marcó «método largo» (>80 líneas, LOW):
        por eso son tres overrides independientes y esto es sólo el orquestador.
        """
        task_type = self._aplicar_override_codigo(message, task_type)
        task_type = self._aplicar_override_file_search(message, task_type)
        return self._aplicar_override_sistema(message, task_type, session_id)

    def _construir_contexto_interno(
        self, message: Message, context: Any
    ) -> Tuple[Dict[str, Any], Any]:
        """Contexto interno enriquecido para el handler (extraído de `process()`, pieza 7).

        Suma el perfil del usuario, la pista ambiental del sensor y el contexto de knowledge graph,
        y arma `enriched_context`. Devuelve `(_internal_context, enriched_context)`.
        `_get_sensor` viene de `dispatcher_process.py` (try/except de módulo) ⇒ DIFERIDO.
        """
        from src.core.dispatcher_process import _get_sensor  # noqa: F401 — diferido, evita el ciclo

        _internal_context: Dict[str, Any] = {"user_context": context}

        # Semana 1 A1: KG enrichment
        if hasattr(self, '_user_profile') and self._user_profile:
            try:
                from src.memory.knowledge_graph import KnowledgeGraph
                from config.paths import Paths
                _kg_ref = getattr(self._user_profile, '_kg', None)
                if _kg_ref:
                    from src.memory.gateway import MemoryGateway
                    words = [w for w in message.content.lower().split() if len(w) > 3]
                    kg_context = []
                    seen = set()
                    for word in words[:3]:
                        nodes = _kg_ref.search_nodes(label_pattern=word, limit=5)
                        for node in nodes:
                            if node.node_id not in seen:
                                seen.add(node.node_id)
                                related = _kg_ref.query_related(node.node_id, limit=3)
                                kg_context.append({"node": node.to_dict(), "related": related})
                    _internal_context["kg_enrichment"] = kg_context[:5]
            except Exception as _d2e:
                log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

        # Semana 2 B: UserProfile context
        if self._user_profile:
            try:
                profile_ctx = self._user_profile.get_user_context(message.content)
                if profile_ctx:
                    _internal_context["profile"] = profile_ctx
            except Exception as _d2e:
                log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

        # Semana 2 D: ContextSensor hint
        if self._sensor_enabled:
            try:
                ambient_hint = _get_sensor().get_context_hint()
                if ambient_hint:
                    _internal_context["ambient"] = ambient_hint
            except Exception as _d2e:
                log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

        # Serializar contexto enriquecido como string para handlers
        enriched_context = context
        if len(_internal_context) > 1:
            import json as _json
            try:
                enriched_context = _json.dumps(_internal_context, ensure_ascii=False)
            except Exception:
                enriched_context = context

        return _internal_context, enriched_context

    def _calibrar_confianza(
        self,
        response: Any,
        *,
        validator_score: Optional[float],
        validation_failed: bool,
        task_type: str,
        message: Message,
    ) -> None:
        """Calibra la confianza de la respuesta (extraído de `process()`, pieza 7).

        Averigua si la consulta pedía un dato verificable (`expects_factual_grounding`) y llama a
        `calibrate_response_confidence`, que topa la confianza si no hay fuentes. Todo el estado
        (`_expects_grounding`) es interno a este método.
        """
        # ✅ v0.6.9v (P0, continuación): CALIBRAR la confianza mostrada.
        # ⚠️ ISSUE-061: la lógica vive ahora en
        # `calibrate_response_confidence()` (mixin de guardias) para poder
        # testearla: antes era inline acá y un `UnboundLocalError` la rompía
        # en silencio en todos los caminos donde no corre el validador.
        # ✅ ISSUE-063: si la consulta pide un DATO verificable y la respuesta
        # no trae fuentes, la confianza se topa (el validador aprueba el
        # TEXTO, no el respaldo) — caso real del log: falla geológica
        # respondida sin fuentes mostrada como "Alto (88%)".
        _expects_grounding = False
        try:
            from src.core.dispatcher_handlers import (
                expects_factual_grounding as _efg,
            )
            _expects_grounding = bool(_efg(message.content or ""))
        except Exception as _eg_err:
            log_degraded(logger, _eg_err,
                         "src/core/dispatcher_process.py:expects_grounding")

        calibrate_response_confidence(
            response,
            validator_score=validator_score,
            validation_failed=validation_failed,
            task_type=task_type,
            expects_grounding=_expects_grounding,
        )

    def _validar_chat_si_corresponde(self, message: Message, response: Any, task_type: str) -> None:
        """Puerta de la validación factual de chat + chequeo de grounding (pieza 7, bloque 4).

        Extraído de `process()`: si la tarea es de chat y la respuesta es válida, mira si la consulta
        pedía analizar una carpeta; cuando es así y la respuesta NO trae `sources`, la marca
        (`grounding_missing`) y **no** la manda al validador (el pipeline repetiría el mismo error).
        El resto del camino (skip A1 y single-pass) vive en `_validar_a1`.
        """
        if (
            task_type in _CHAT_VALIDATION_TASKS
            and response.error is None
            and response.content
        ):
            # ✅ v0.6.8ii: grounding — si la consulta pide analizar archivos/
            # carpeta y la respuesta NO trae sources (ni evidencia de lectura),
            # no dejar que el validador la selle con PASS: se marca y se
            # omite la auto-retry inútil (el pipeline ungrounded repetiría
            # el mismo error).
            _grounding_query = False
            try:
                from src.core.dispatcher_handlers import (
                    _is_folder_analysis_query as _fq,
                )
                _grounding_query = bool(_fq and _fq(message.content or ""))
            except Exception:
                _grounding_query = False
            _has_sources = bool(getattr(response, "sources", None))
            if _grounding_query and not _has_sources:
                _log_event("DISPATCHER",
                           "Chat validation SKIPPED — respuesta sin grounding",
                           extra={
                               "task_type": task_type,
                               "reason": "folder_analysis_sin_sources",
                               "content_len": len(response.content or ""),
                           })
                logger.warning(
                    "[GROUNDING] consulta de carpeta respondida sin sources "
                    "(no se aprueba con validador)"
                )
                try:
                    response.metadata["grounding_missing"] = True
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")
            else:
                self._validar_a1(message, response, task_type, _has_sources)

    def _validar_a1(self, message: Message, response: Any, task_type: str, has_sources: bool) -> None:
        """Skip A1 (respuesta corta o grounded) y lanzamiento SINGLE-PASS (pieza 7, bloque 4).

        Extraído de `process()`. `has_sources` lo calcula `_validar_chat_si_corresponde` (es la misma
        evidencia de grounding) y `resp_len` lo recalcula acá, como en el original.
        """
        # ── v0.6.8ss (A1): skip de validación innecesaria ──────
        # 1) Respuestas triviales/cortas (≤80 chars: ecos, saludos,
        #    fórmulas, 1 línea): validar cuesta ~43 s en CPU y el
        #    validador suele marcar FAIL a respuestas cortas
        #    correctas (log 15:24: eco de 32 chars, score 5.0).
        # 2) Respuestas con sources REALES (grounded): ya ancladas a
        #    evidencia leída del FS/API; el validador no ve las
        #    fuentes, solo re-juzga el texto y falla respuestas
        #    buenas. (El skip inverso —carpeta sin sources— sigue
        #    arriba con grounding_missing.)
        _resp_len = len(response.content or "")
        if _resp_len <= 80 or has_sources:
            _a1_reason = "short" if _resp_len <= 80 else "grounded_sources"
            _log_event("DISPATCHER", "Chat validation SKIPPED (A1)", extra={
                "task_type": task_type,
                "reason": _a1_reason,
                "content_len": _resp_len,
                "sources": len(getattr(response, "sources", []) or []),
            })
            logger.debug(
                f"[A1] validación omitida ({_a1_reason}, "
                f"len={_resp_len}) — respuesta trivial/grounded"
            )
            try:
                if not response.metadata:
                    response.metadata = {}
                response.metadata[f"validation_skipped_{_a1_reason}"] = True
            except Exception as _d2e:
                log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")
        else:
            # ── v0.6.8pp: Validación SINGLE-PASS ──────────────────
            # El validador (qwen2.5-coder:7b) suele marcar FAIL a
            # respuestas cortas y correctas (scores 2.0-7.0 en CPU).
            # Antes: FAIL → re-generar con fallback (+20-90 s) →
            # validar de nuevo (+20-90 s) → a veces FAIL otra vez.
            # Ese caso pico costó ~391 s (log 15:03, 234 s solo el
            # ciclo de validación).
            #
            # AHORA: UNA sola validación. Si falla, NO se re-genera:
            # la respuesta original ya es la mejor disponible (el
            # pipeline de fallback repetiría el mismo estilo) y el
            # costo extra no mejora precisión. Solo se marca para
            # observabilidad.
            # ✅ v0.6.9v: evidencia para CALIBRAR la confianza que
            # se muestra (panel de transparencia). Se captura el
            # score del validador pase lo que pase.
            # ✅ OPTIMIZACIÓN MEDIDA (2026-10-03): la validación **no
            # bloquea la respuesta**. Medido en este equipo: 41-43 s
            # por respuesta (segunda llamada al modelo, con cambio de
            # modelo incluido) y su resultado **no cambia la
            # respuesta** (si falla, se conserva la original: es
            # single-pass). Ahora sale en SEGUNDO PLANO: el usuario ve
            # la respuesta ~41 s antes y el veredicto llega después, a
            # la metadata y a la telemetría (QualityMonitor).
            self._lanzar_validacion_en_fondo(message.content, response, task_type)

    async def _registrar_senal_feedback(
        self,
        message: Message,
        response: Any,
        task_type: str,
        session_id: Optional[str],
        *,
        validator_score: Optional[float],
        validation_failed: bool,
    ) -> None:
        """Registra la señal implícita ROUTED del FeedbackLoop (pieza 7, bloque 6).

        Extraído de `process()`: la telemetría por turno viaja con la confianza YA calibrada y el
        estado de validación. El score puede llegar en 0.0 porque la validación corre en SEGUNDO
        PLANO (medido: 41-43 s), y eso significa «todavía no validado», no «sin validar».
        """
        if response.error is None:
            await self._ensure_feedback_loop()
            if self._feedback_loop is not None and ImplicitSignal is not None:
                try:
                    asyncio.create_task(
                        self._feedback_loop.record_implicit(
                            session_id=session_id or self._feedback_loop.session_id,
                            task_type=task_type,
                            # ✅ v0.6.9v (ISSUE-033): `agent_role` es REQUERIDO
                            # por record_implicit y no se pasaba → TypeError
                            # (además de ImplicitSignal.ROUTED, que no existía).
                            # Silenciado en DEBUG → la señal NUNCA se registraba.
                            agent_role=self._handler_name(task_type),
                            query=message.content,
                            response=response.content or "",
                            signal=ImplicitSignal.ROUTED,
                            # ✅ P1.1 (plan_fiabilidad, 2026-09-12): antes no se
                            # pasaban — la telemetría por turno (interaction_metrics)
                            # quedaba siempre en latencia 0.0 y sin nº de fuentes.
                            # response.latency_ms/.sources ya están resueltos acá
                            # (response viene de AgentResult.to_response(), fix
                            # aparte en src/agents/base.py para que .sources no
                            # quede vacío).
                            latency_ms=response.latency_ms,
                            metadata={
                                "num_sources": len(response.sources or []),
                                # ✅ P1.1 (corroboración): la confianza YA
                                # CALIBRADA (ISSUE-056/063) viaja a la
                                # telemetría — antes `confidence_input/output`
                                # quedaban en 0.0 y la columna no servía.
                                # La calibración corre antes de este bloque.
                                "confidence": float(
                                    getattr(response, "confidence", 0.0) or 0.0),
                                # ✅ ISSUE-068 (2026-09-14): estado de
                                # validación por turno. `validation_passed=1`
                                # solo si el validador SÍ corrió (score no
                                # nulo) y no falló; si no hay validación el
                                # score queda 0.0 y passed=0 (distinguible
                                # de "validó y falló" por el score).
                                "validation_score": float(validator_score)
                                    if validator_score is not None else 0.0,
                                "validation_passed": 1 if (
                                    validator_score is not None and not validation_failed
                                ) else 0,
                                # ✅ 2026-10-03: desde que la validación corre
                                # en SEGUNDO PLANO (medido: 41-43 s), el score
                                # NO está listo cuando se escribe esta fila ⇒
                                # `validation_score=0` acá significa "todavía
                                # no validado", no "sin validar". El score real
                                # llega al QualityMonitor y a la metadata de la
                                # respuesta cuando el validador termina.
                                "validation_en_fondo": bool(
                                    (getattr(response, "metadata", None) or {})
                                    .get("validation_en_fondo", False)
                                ) if isinstance(getattr(response, "metadata", None), dict) else False,
                            },
                        )
                    )
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

    def _registrar_metricas_y_cierre(
        self,
        *,
        start_time: Any,
        query_preview: str,
        task_type: str,
        handler: str,
        confidence: float,
    ) -> None:
        """Métricas por llamada y cierre del turno (pieza 7, bloque 7a).

        Extraído de `process()`: cuenta la llamada, cada `_learning_loop_interval` dispara el
        consumo de feedback para el clasificador y (detrás de su flag) el ajuste async de
        temperature; y cierra con la duración total y la decisión de ruteo en el log.
        """
        self._process_call_count += 1
        if (
            self._process_call_count % self._learning_loop_interval == 0
            and self._feedback_loop is not None
            and self._classifier is not None
        ):
            try:
                asyncio.create_task(
                    self._feedback_loop.consume_feedback_for_classifier(
                        self.classifier._core  # IntentClassifierCore con add_example()
                    )
                )
                logger.debug(
                    f"Learning loop triggered (call #{self._process_call_count})"
                )
            except Exception as _d2e:
                log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

            # ✅ P1.3 (plan_fiabilidad, ISSUE-069, 2026-09-14): mismo
            # intervalo periódico, dispara el CÁLCULO async del ajuste de
            # temperature (adjust_parameter(), vía get_optimized_parameter()).
            # LLMConfig._apply_feedback_temperature() es quien LEE el
            # resultado (síncrono) en la próxima llamada a
            # get_model_for_task() — acá solo se actualiza ese valor.
            # Detrás del mismo flag que el consumidor, para no calcular
            # en vano si nadie lo va a leer.
            if getattr(settings, "feedback_temperature_tuning_enabled", False):
                try:
                    _current_temp = self._feedback_loop.get_current_parameter(
                        "temperature", task_type
                    )
                    if _current_temp is None:
                        _current_temp = settings.default_temperature
                    asyncio.create_task(
                        self._feedback_loop.get_optimized_parameter(
                            "temperature", task_type,
                            current_value=_current_temp,
                            min_value=0.0, max_value=1.0,
                        )
                    )
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_process.py:process")

        total_duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        _log_router_decision(
            query=query_preview, task_type=task_type,
            handler=handler, confidence=confidence, duration=total_duration,
        )

    # ── Bloque 8 de process(): contexto de sesión ─────────────────────────────
    def _contexto_de_sesion(self, context: Any, session_id: Optional[str]) -> Tuple[Any, Any]:
        """Arma el SessionContext de la sesión (bloque 8 de `process()`).

        Devuelve `(_session_ctx, context)` porque el bloque además **consume** `context`
        (lo deja en None cuando ya viene como SessionContext).
        """
        # ✅ CAAC Fase 0: clasificación contextual si hay SessionContext
        _session_ctx = None
        if _SESSION_CONTEXT_AVAILABLE and session_id:
            if _SessionContext and isinstance(context, _SessionContext):
                _session_ctx = context
                context = None
            else:
                with self._session_store_lock:
                    _session_ctx = self._session_store.get(session_id)
        return _session_ctx, context

    # ── Bloque 9 de process(): el dispatch ────────────────────────────────────
    async def _despachar(
        self, message: Any, task_type: str, session_id: Optional[str], enriched_context: Any
    ) -> Tuple[Any, str]:
        """Ejecuta el handler del tipo de tarea y devuelve `(response, handler)` (bloque 9).

        Es el corazón de `process()`: se extrajo sin tocar el orden de llama al handler ni
        el cálculo del nombre, para que el `response` y el `handler` sean los mismos.
        """
        response = await self._dispatch(message, task_type, session_id, enriched_context)
        handler = self._handler_name(task_type)
        return response, handler
