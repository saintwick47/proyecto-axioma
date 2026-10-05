#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — dispatcher_plan.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/core/dispatcher_plan.py
#
# ✅ v0.6.9v (REFACTOR): capa de PLAN-PIPELINE (F3 /plan) extraída de
#   src/core/dispatcher_handlers_extra.py (el original queda intacto).
#
#   Qué se movió desde dispatcher_handlers_extra.py (sin cambios de comportamiento):
#     - `execute_pipeline`          → ejecuta una lista de agentes (coder/researcher/validator)
#     - `_stream_chat_response`     → streaming con token_cb (v0.6.8ss A3)
#     - `_plan_pipeline_for`        → heurístico GRATIS de pipeline (sin LLM)
#     - `_run_plan_pipeline`        → orquesta /plan + refinamiento dirigido (opt-in)
#     - `_check_code_complexity`    → detecta proyectos muy complejos
#
#   `DispatcherHandlersExtraMixin` hereda `DispatcherPlanMixin`, así la API
#   pública (self.execute_pipeline, self._plan_pipeline_for, …) no cambia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from src.domain.entities import Message, Response, MessageRole
from config.settings import settings
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


class DispatcherPlanMixin:
    """Pipeline de agentes y plan F3 (/plan).

    Extraído de dispatcher_handlers_extra.py (50/50): este mixin se ocupa de
    armar y ejecutar cadenas de agentes (coder → researcher → validator) y del
    refinamiento dirigido opt-in. Los métodos llaman a otros handlers vía
    self._handle_* (definidos en el Dispatcher compuesto) y a _record_metric /
    _error_response — sin importar dispatcher_handlers_extra (evita ciclos).
    """

    async def execute_pipeline(
        self,
        query: str,
        pipeline: List[str],
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Response:
        start_time = datetime.now(timezone.utc)

        if not pipeline:
            return self._error_response("pipeline vacío", session_id, start_time)

        _VALID = {"coder", "researcher", "validator"}
        invalid = [s for s in pipeline if s not in _VALID]
        if invalid:
            return self._error_response(f"agentes inválidos: {invalid}", session_id, start_time)

        from .dispatcher_handlers import _log_event
        _log_event("DISPATCHER", "execute_pipeline start", extra={
            "pipeline": pipeline, "session_id": session_id,
            "query_preview": query[:80],
        })

        current_query = query
        last_response: Optional[Response] = None
        _validation_note: Optional[Dict[str, Any]] = None

        for step in pipeline:
            step_message = Message(
                content=current_query,
                role=MessageRole.USER,
                metadata={"pipeline_step": step, **(context or {})},
            )

            try:
                if step == "coder":
                    response = await self._handle_code(
                        step_message, "code_generation", session_id, None)
                elif step == "researcher":
                    response = await self._handle_research(
                        step_message, "research", session_id, None)
                elif step == "validator":
                    # ✅ v0.6.8xx (fix en vivo /plan 2026-09-05): el paso
                    # "validator" es una VALIDACIÓN FINAL del artefacto, no un
                    # productor de contenido. ANTES reemplazaba la respuesta
                    # del paso anterior por el análisis del validador
                    # ("1. ANÁLISIS: ..."), por lo que /plan respondía la
                    # crítica y no el programa. AHORA: se conserva el
                    # artefacto y el veredicto viaja en metadata.
                    response = await self._handle_validation(
                        step_message, "quality_validation", session_id, None)
                    if response.error:
                        logger.warning(
                            f"execute_pipeline validator error: {response.error}")
                        return response
                    _validation_note = {
                        "passed": bool(
                            getattr(response, "validation_passed", False)
                            or (response.metadata or {}).get("passed", False)),
                        "score": (response.metadata or {}).get("score"),
                        "note": (response.content or "")[:400],
                    }
                    if last_response is not None:
                        continue  # conservar el artefacto del paso productor
                    # sin artefacto previo → devolver el veredicto
                    response.metadata["pipeline_validator"] = _validation_note
                    last_response = response
                    current_query = response.content
                    continue
                else:
                    return self._error_response(
                        f"agente desconocido: {step}", session_id, start_time)

            except Exception as e:
                logger.error(f"execute_pipeline step={step} failed: {e}")
                return self._error_response(str(e), session_id, start_time)

            if response.error:
                logger.warning(f"execute_pipeline step={step} error: {response.error}")
                return response

            last_response = response
            current_query = response.content

        if _validation_note is not None and last_response is not None:
            try:
                if not last_response.metadata:
                    last_response.metadata = {}
                last_response.metadata["pipeline_validator"] = _validation_note
            except Exception as e:
                # R3: la nota del validador es metadata informativa.
                log_degraded(logger, e, "Dispatcher.execute_pipeline (metadata pipeline_validator)",
                             contract_level=logging.DEBUG)

        latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        from .dispatcher_handlers import _log_event
        _log_event("DISPATCHER", "execute_pipeline complete", extra={
            "pipeline": pipeline, "session_id": session_id,
            "latency_ms": round(latency, 2),
            "validation_note": bool(_validation_note),
        })

        self._record_metric("pipeline", "+".join(pipeline), latency, True,
                            plan_depth=len(pipeline))

        return last_response

    # ── F3 opt-in (PLAN_ADAPTADO.md FASE 3): /plan + pipeline automático ──

    async def _stream_chat_response(
        self,
        messages: List[Dict[str, Any]],
        token_cb: Any,
        task_type: str,
        max_tokens: Optional[int] = None,
        usage_sink: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        v0.6.8ss (A3): itera llm_client.chat_stream() cediendo cada chunk a
        token_cb (síncrono, actualización de UI) sin bloquear el event loop
        (cada lectura va por asyncio.to_thread). Acumula y devuelve el texto
        completo. Un fallo de UI (token_cb) NUNCA corta la generación.

        ✅ Fase 1 (2026-10-02, medido): `max_tokens` — el camino con flujo NO
        respetaba el tope de salida del chat y por eso generaba de más (medición
        base: 1162 caracteres con flujo vs 589 sin flujo para la misma pregunta,
        135 s vs 90 s). `usage_sink` — sin esto el camino con flujo devolvía la
        respuesta SIN `tokens_prompt`/`tokens_generated` (los contadores de las
        trazas quedaban en cero, que es exactamente lo que arregló ISSUE-082).

        DETENER (ISSUE-134): cancelar esta corrutina **ya detiene la generación**
        —medido con el runner de Ollama: 545 % de CPU generando → 21-30 % en los
        8 s posteriores al corte, y 0 tokens más—. El `finally: gen.close()`
        cierra la conexión cuando el generador queda suspendido, que es lo que
        hace que Ollama aborte. Se probó además un cierre explícito de la
        conexión (`cancel_sink`) y **no aportó diferencia medible**: no se agregó.
        """
        gen = self.llm_client.chat_stream(
            messages=messages,
            temperature=settings.default_temperature,
            timeout=settings.ollama_timeout_chat,
            task_type=task_type,
            max_tokens=max_tokens,
            usage_sink=usage_sink,
        )
        parts: List[str] = []
        try:
            while True:
                chunk = await asyncio.to_thread(next, gen, None)
                if chunk is None:
                    break
                parts.append(chunk)
                try:
                    token_cb(chunk)
                except Exception as _cb_err:
                    # ✅ ISSUE-019: un fallo de contrato acá (token_cb con firma
                    # equivocada) rompería el streaming en silencio.
                    log_degraded(logger, _cb_err, "dispatcher_plan:_stream_chat_response")
        finally:
            try:
                gen.close()
            except Exception as _gc_err:
                log_degraded(logger, _gc_err, "src/core/dispatcher_plan.py:_stream_chat_response")
        return "".join(parts)

    def _plan_pipeline_for(
        self, query: str, task_type_value: Optional[str] = None,
    ) -> Optional[List[str]]:
        """
        Arma el pipeline F3 para `query` (heurístico, GRATIS — sin LLM).

        Descompone la consulta (TaskDecomposer, llm_refine=False) y mapea
        roles → pipeline ejecutable {"coder","researcher","validator"}.

        Reglas (PLAN_ADAPTADO.md F3, adaptadas al infra real):
          - Cap: settings.plan_max_steps (default 2). "1 paso = ruta actual":
            con max_steps=1 el pipeline es el rol principal a secas.
          - Validador SOLO como paso FINAL ("validar solo el resultado
            final", nunca por paso intermedio): pipeline típico
            [rol_principal, "validator"] (≤2 generaciones CPU — por eso es
            opt-in, default OFF).
          - Roles sin handler de pipeline (orchestrator/analyst/system) se
            descartan; si NO queda ningún rol ejecutable (ej. chat puro) →
            None y el llamador procesa por la ruta normal.
        """
        try:
            from src.domain.types import TaskType, AgentRole
            from src.core.planner import MultiStepPlanner
            import uuid as _uuid
        except Exception as e:
            log_degraded(logger, e, "src/core/dispatcher_plan.py:_plan_pipeline_for")
            return None

        _ROLE_TO_TOKEN = {
            AgentRole.CODER: "coder",
            AgentRole.RESEARCHER: "researcher",
            AgentRole.VALIDATOR: "validator",
        }

        def _map_tokens(subtasks: List[Any]) -> List[str]:
            """Mapea roles de subtareas → tokens de pipeline, dedupe seguidos."""
            out: List[str] = []
            for st in subtasks:
                tok = _ROLE_TO_TOKEN.get(st.agent_role)
                if tok and (not out or out[-1] != tok):
                    out.append(tok)
            return out

        try:
            planner = getattr(self, "_planner", None) or MultiStepPlanner()
            tt = TaskType.AUTONOMOUS_TASK
            if task_type_value:
                try:
                    tt = TaskType(task_type_value)
                except Exception as _d2e:
                    log_degraded(logger, _d2e, "src/core/dispatcher_plan.py:_plan_pipeline_for")
            pid = f"plan_{_uuid.uuid4().hex[:8]}"
            subtasks = planner.decomposer.decompose(pid, query, tt)
            tokens = _map_tokens(subtasks)

            if not tokens:
                # ✅ v0.6.8xx (fix en vivo /plan): CODE_AUTONOMOUS no está en
                # _TASK_TO_AGENT → decompose devuelve ORCHESTRATOR (sin rol de
                # pipeline) y /plan caía al fallback atómico (generaba normal,
                # sin pipeline — log 19:14). Reintento heurístico barato:
                # código → coder, búsqueda/investigación → researcher, resto
                # (chat puro) → None (ruta normal).
                q_low = (query or "").lower()
                _code_hints = (
                    "código", "codigo", "programa", "script", "función", "funcion",
                    "python", "clase", "html", "css", "test", "archivo", ".py",
                    "función", "aplicación", "aplicacion", "api", "algoritmo",
                )
                _search_hints = (
                    "investig", "busc", "research", "noticia", "información",
                    "informacion", "compar", "arquitectura", "framework",
                    "analizá", "analiza", "resum", "fuente", "mejor",
                )
                if any(h in q_low for h in _code_hints):
                    tt = TaskType.CODE_GENERATION
                elif any(h in q_low for h in _search_hints):
                    tt = TaskType.WEB_SEARCH
                else:
                    return None  # chat puro → ruta normal
                subtasks = planner.decomposer.decompose(
                    f"plan_{_uuid.uuid4().hex[:8]}", query, tt,
                )
                tokens = _map_tokens(subtasks)
        except Exception as e:
            log_degraded(logger, e, "src/core/dispatcher_plan.py:_plan_pipeline_for")
            return None

        if not subtasks or not tokens:
            return None

        max_steps = int(getattr(settings, "plan_max_steps", 2) or 2)
        if max_steps <= 1:
            return tokens[:1]

        # rol(es) principal(es) + validador FINAL (1 solo, al final)
        non_validator = [t for t in tokens if t != "validator"]
        if not non_validator:
            return None
        principal = non_validator[: max_steps - 1]
        pipeline = principal + ["validator"]
        return pipeline

    async def _run_plan_pipeline(
        self,
        query: str,
        session_id: Optional[str] = None,
        task_type_value: Optional[str] = None,
    ) -> Optional[Response]:
        """
        Ejecuta el pipeline F3 para `query` si hay roles ejecutables.
        Devuelve None si la consulta no mapea a pipeline (chat puro) → el
        llamador sigue por dispatch normal (sin costo extra).
        """
        start = time.time()
        pipeline = self._plan_pipeline_for(query, task_type_value)
        if not pipeline:
            return None

        from .dispatcher_handlers import _log_event
        _log_event("DISPATCHER", "F3 /plan pipeline start", extra={
            "pipeline": pipeline,
            "session_id": session_id,
            "query_preview": query[:80],
        })
        logger.info(f"[PLAN] pipeline {pipeline} para: {query[:80]}")

        response = await self.execute_pipeline(
            query=query,
            pipeline=pipeline,
            session_id=session_id,
            context=None,
        )
        if response is None:
            return None
        if response.error:
            logger.warning(f"[PLAN] pipeline falló: {response.error}")
            return response

        # ✅ v0.6.9a (F, flag plan_refine_enabled — OFF default): refinamiento
        # dirigido por el validador. Si el pipeline terminó en validator y el
        # score final es < plan_refine_min_score con crítica accionable, se
        # hace UNA segunda pasada coder con esa crítica (el artefacto se
        # re-genera con los casos sugeridos) y se revalida. Coste +1-2
        # generaciones CPU — por eso es opt-in.
        _note = (response.metadata or {}).get("pipeline_validator") or {}
        if (
            getattr(settings, "plan_refine_enabled", False)
            and pipeline
            and pipeline[-1] == "validator"
            and _note.get("score") is not None
            and float(_note["score"]) < float(
                getattr(settings, "plan_refine_min_score", 9.0))
            and (_note.get("note") or "").strip()
        ):
            _critique = _note["note"][:1500]
            logger.info(
                f"[PLAN] refinamiento dirigido (F): score {_note['score']} < "
                f"{settings.plan_refine_min_score} → 2ª pasada con crítica"
            )
            refined = await self.execute_pipeline(
                query=query,
                pipeline=pipeline,
                session_id=session_id,
                context={"previous_critique": _critique},
            )
            if refined is not None and refined.error is None:
                response = refined
                try:
                    if not response.metadata:
                        response.metadata = {}
                    response.metadata["plan_refined"] = True
                except Exception as e:
                    # R3: la marca `plan_refined` es metadata informativa.
                    log_degraded(logger, e, "Dispatcher._run_plan_pipeline (metadata plan_refined)",
                                 contract_level=logging.DEBUG)

        response.metadata["plan_pipeline"] = list(pipeline)
        response.metadata["plan_latency_ms"] = round((time.time() - start) * 1000, 2)
        return response

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _check_code_complexity(self, query: str) -> tuple[bool, str]:
        q = query.lower()
        complex_kw = [
            "programa completo", "aplicación", "sistema", "similar a",
            "pero mejor", "con todas las funciones", "full", "completo",
            "framework", "librería", "paquete",
        ]
        large_tools = [
            "metasploit", "burp suite", "ansible", "terraform", "kubernetes",
            "django", "flask", "react", "vue", "tensorflow", "pytorch", "selenium",
        ]
        defensive_kw = [
            "escanear", "escaneo", "red propia", "mi red", "auditoría",
            "monitoreo", "detectar ataques", "detectar intrusos", "seguridad defensiva",
            "network scan", "port scan", "vulnerability scan", "blue team",
            "administración de red", "admin de red",
        ]

        if any(kw in q for kw in defensive_kw):
            return False, ""

        has_complex = any(kw in q for kw in complex_kw)
        has_tool = any(t in q for t in large_tools)

        logger.debug(f"[DISPATCHER] complexity: complex={has_complex}, tool={has_tool}")

        if has_complex and has_tool:
            msg = (
                "Este proyecto es muy complejo y requiere múltiples archivos y módulos.\n\n"
                "**¿Qué te gustaría que haga primero?**\n\n"
                "1. **Esqueleto básico** — Estructura del proyecto con funciones principales\n"
                "2. **Módulo específico** — Decime qué funcionalidad concreta necesitás\n"
                "3. **Prototipo simple** — Versión reducida con 1-2 funciones principales\n"
                "4. **Guía de implementación** — Explicación paso a paso de cómo construirlo\n\n"
                "Especificá qué opción preferís o contame qué parte necesitás primero."
            )
            return True, msg

        return False, ""
