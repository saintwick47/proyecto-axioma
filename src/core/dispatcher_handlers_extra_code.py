#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — dispatcher_handlers_extra_code.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/core/dispatcher_handlers_extra_code.py
#
# Extraído de: src/core/dispatcher_handlers_extra.py (2026-10-22)
#
# Propósito: la FAMILIA DE CÓDIGO de los handlers, que estaba mezclada con
#   chat/análisis/investigación/screenshot en el mismo archivo:
#     · _modelo_de_codigo_disponible / _coder_de_respaldo
#     · _reintentar_con_respaldo_de_codigo
#     · _handle_code (con su validación: _handle_validation,
#       _format_tools_for_ollama, _run_pending_approval)
#     · _handle_autonomous_code / _should_use_autonomous_mode
#
# Relación con el original:
#   `DispatcherHandlersExtraMixin(DispatcherHandlersExtraCodeMixin, DispatcherPlanMixin)`
#   hereda este mixin, así que la API pública (self._handle_code, …) NO cambia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import asyncio
import logging
import re
import time
import inspect
import base64
import json
from types import SimpleNamespace
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from src.domain.entities import Message, Response, MessageRole
from config.settings import settings

from src.tools_mcp.tool_executor import get_executor, ExecutionContext, ToolPermission
from src.tools_mcp.tool_registry import get_registry
from src.agents.code_contract import CodeContract
from src.agents.code_validator import CodeValidator, build_validation_critique
from src.tools_mcp.sandbox import get_sandbox_executor
from src.utils.degradation import log_degraded

# ✅ v0.6.9v (REFACTOR 50/50): plan-pipeline (F3 /plan) extraído a
# src/core/dispatcher_plan.py. DispatcherHandlersExtraMixin hereda el mixin.
from .dispatcher_plan import DispatcherPlanMixin

# Helpers/constantes compartidas que viven en dispatcher_handlers.py
from .dispatcher_handlers import (
    _log_event,
    _log_error,
    _log_agent_execution,
    _log_model_call,
    _log_router_decision,
    _get_caller_info,
    _detect_code_content,
    MAX_TOKENS_ANALYSIS,
    MAX_TOKENS_CHAT,
    MAX_TOKENS_DEFAULT,
    expects_factual_grounding,
    CODE_REVIEW_SYSTEM_PROMPT,
    GENERAL_ANALYSIS_SYSTEM_PROMPT,
)
from src.llm.client_base import tokens_de_respuesta

logger = logging.getLogger(__name__)


class DispatcherHandlersExtraCodeMixin:
    """Familia de código de los handlers (extraída de DispatcherHandlersExtraMixin)."""

    def _modelo_de_codigo_disponible(self, nombre: str) -> bool:
        """¿El modelo está realmente instalado en Ollama? (no se confía en la config).

        ✅ ISSUE-142: el respaldo apunta a un modelo que puede no estar instalado
        (es el caso del usuario, que lo bajó para probar). Si no está, **no se
        reintenta** (sería un error de modelo inexistente y tiempo perdido).
        """
        try:
            nombres = {m.get("name") for m in (self.llm_client.list_models() or [])}
            if not nombres:
                return False
            return nombre in nombres or f"{nombre}:latest" in nombres
        except Exception as e:
            log_degraded(logger, e, "dispatcher_handlers_extra._modelo_de_codigo_disponible")
            return False

    def _coder_de_respaldo(self) -> Optional[Any]:
        """Agente de código con el modelo de respaldo (creado una sola vez)."""
        nombre = (settings.llm_code_model_fallback or "").strip()
        if not nombre or nombre == settings.llm_code_model:
            return None
        agente = getattr(self, "_coder_fallback", None)
        if agente is None:
            try:
                from src.agents.coder import CoderAgent
                agente = CoderAgent(model=nombre)
                self._coder_fallback = agente
                logger.info(f"[ISSUE-142] CoderAgent de respaldo listo: {nombre}")
            except Exception as e:
                log_degraded(logger, e, "dispatcher_handlers_extra._coder_de_respaldo")
                return None
        return agente

    async def _reintentar_con_respaldo_de_codigo(
        self, result: Any, message: Message, task_type: str,
        critique: Optional[str], session_id: Optional[str],
    ) -> Optional[Any]:
        """Ante una NEGATIVA, reintenta UNA vez con el otro modelo de código.

        Medido (`ISSUE-142`): el modelo censurado se niega en prosa (2-5 s) para
        pedidos legítimos, y el abliterado los genera. Como en RAM sólo entra un
        modelo, el reintento descarga el primario y carga el respaldo (~6 s,
        `ISSUE-141`). Si el respaldo no está instalado, no se reintenta: se deja la
        negativa marcada para que el usuario vea el aviso.

        Devuelve el resultado del respaldo (con las marcas de trazabilidad) o None si
        no se intentó.
        """
        agente = self._coder_de_respaldo()
        if agente is None:
            return None
        nombre = getattr(agente.config, "model", None) or settings.llm_code_model_fallback
        if not self._modelo_de_codigo_disponible(nombre):
            logger.warning(
                f"[ISSUE-142] respaldo '{nombre}' NO está instalado — no se reintenta "
                "(ver el mensaje de negativa para instalarlo o cambiarlo)"
            )
            try:
                result.metadata = {**(result.metadata or {}), "code_fallback_no_instalado": nombre}
            except Exception as e:
                log_degraded(logger, e, "dispatcher_handlers_extra._reintentar_con_respaldo(marca)")
            return None

        logger.info(
            f"[ISSUE-142] el modelo primario se negó — reintentando con '{nombre}' "
            "(implica cambio de modelo en RAM)"
        )
        _log_event("DISPATCHER", "Respaldo por negativa del modelo de código", extra={
            "primario": settings.llm_code_model, "respaldo": nombre,
            "frase": (result.metadata or {}).get("code_refusal", ""),
            "task_type": task_type,
        })
        _frase_primaria = (result.metadata or {}).get("code_refusal")
        t0 = time.time()
        try:
            respaldo = await asyncio.to_thread(
                agente.execute, message, task_type=task_type,
                previous_critique=critique,
                session_id=session_id, user_id=self._user_id,
            )
        except Exception as e:
            log_degraded(logger, e, "dispatcher_handlers_extra._reintentar_con_respaldo(ejecutar)")
            return None
        _log_agent_execution("CoderAgent(respaldo)", task_type, "completed",
                             duration=time.time() - t0,
                             success=respaldo.success, error=respaldo.error)
        try:
            respaldo.metadata = {
                **(respaldo.metadata or {}),
                "code_fallback_usado": nombre,
                "code_refusal_primario": _frase_primaria,
                "code_fallback_segundos": round(time.time() - t0, 1),
            }
        except Exception as e:
            log_degraded(logger, e, "dispatcher_handlers_extra._reintentar_con_respaldo(metadata)")
        return respaldo

    async def _handle_code(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ CoderAgent: {task_type}")
        _log_event("DISPATCHER", f"CoderAgent started: {task_type}", extra={
            "query": message.content[:50] if message.content else "",
        })

        is_complex, clarification = self._check_code_complexity(message.content)
        if is_complex:
            return Response(
                content=clarification, session_id=session_id,
                model_used="dispatcher", task_type=task_type,
                latency_ms=(time.time() - start) * 1000,
                confidence=0.9, validation_passed=True,
            )

        # ✅ v0.6.8rr (F3 opt-in): los pasos de un pipeline (/plan) NUNCA
        # derivan a modo autónomo — execute_pipeline es una cadena de
        # generaciones (coder/researcher/validator), no ejecución en sandbox.
        # Sin este guard, "/plan generá y probá X" haría que el paso coder
        # entrara a InterpreterLoop por la keyword "probá".
        _is_pipeline_step = bool((getattr(message, "metadata", None) or {}).get("pipeline_step"))
        if not _is_pipeline_step and (
            task_type == "code_autonomous"
            or self._should_use_autonomous_mode(message.content)
        ):
            return await self._handle_autonomous_code(message, task_type, session_id, context)

        try:
            # ✅ Fase 4: repair loop externo — cuando CodeValidator (Fase 3)
            # encuentra que el código no compila por archivo o los tests
            # fallan de verdad, se reintenta la generación completa con la
            # crítica real como contexto, hasta code_contract_max_retries
            # veces (settings, Fase 1, default 3). Distinto del retry
            # interno de CoderAgent (Fase 2, self.config.max_retries=3,
            # dispara por contrato incompleto o su propia heurística de
            # calidad) — acá el trigger es la validación real (compile
            # por-archivo + tests corridos en sandbox), que CoderAgent no
            # puede ver por sí solo. No se encadena el attempt de uno con
            # el del otro a propósito: cada vuelta de este loop es una
            # generación fresca (attempt=1 interno), no una extensión de
            # los intentos internos ya agotados de la vuelta anterior.
            max_validation_retries = settings.code_contract_max_retries
            # ✅ v0.6.9a (F): si el caller (pipeline /plan en modo refine)
            # dejó una crítica previa en el metadata del mensaje, se usa como
            # critique inicial de la primera generación.
            validation_critique: Optional[str] = (
                (getattr(message, "metadata", None) or {}).get("previous_critique")
            ) or None
            result   = None
            report   = None
            # ✅ v0.6.8zz (fix sandbox 2026-09-05): guard de "sin progreso".
            # Caso real: los tests generados no importaban el módulo
            # (NameError) y el retry con la MISMA crítica regeneraba el MISMO
            # código (caché de generación) → 4 vueltas idénticas sin avanzar.
            # Si el artefacto nuevo es idéntico al del intento anterior, otra
            # generación no puede cambiar nada: se corta.
            _last_artifact_fingerprint: Optional[str] = None
            # ✅ ISSUE-142: el respaldo por negativa se intenta UNA vez por request
            # (no en cada vuelta del repair loop), y una vez que el respaldo generó,
            # **el resto del bucle usa ESE agente**: si no, la vuelta siguiente
            # volvía a llamar al primario, que se negaba otra vez y pisaba el
            # resultado bueno (medido: 123 s y respuesta de negativa con el código
            # del respaldo ya generado).
            _respaldo_intentado = False
            _agente = self.coder

            for outer_attempt in range(max_validation_retries + 1):
                result = await asyncio.to_thread(
                    _agente.execute, message, task_type=task_type,
                    previous_critique=validation_critique,
                    session_id=session_id,  # ✅ plan_memory (B): memoria por sesión
                    user_id=self._user_id,  # ✅ UI de usuarios (2026-08-31)
                )

                # ── ✅ ISSUE-142: si el modelo se NEGÓ, reintentar con el respaldo ──
                if ((result.metadata or {}).get("code_refusal")
                        and not _respaldo_intentado):
                    _respaldo_intentado = True
                    _res = await self._reintentar_con_respaldo_de_codigo(
                        result, message, task_type, validation_critique,
                        session_id,
                    )
                    if _res is not None:
                        result = _res
                        # El respaldo pasa a ser el agente de este request: las
                        # vueltas de reparación siguientes lo usan a él.
                        if not (result.metadata or {}).get("code_refusal"):
                            _agente = self._coder_de_respaldo() or _agente

                _log_agent_execution("CoderAgent", task_type, "completed",
                                     duration=time.time() - start,
                                     success=result.success, error=result.error)

                # Fingerprint del artefacto (código + tests) para el guard
                _artifact = (result.metadata or {}).get("code_artifact") if result.success else None
                if _artifact:
                    import hashlib as _hl
                    _fp_src = "".join(
                        str(f.get("path", f.get("name", ""))) + str(f.get("content", ""))
                        for f in (
                            _artifact.get("files", []) + _artifact.get("test_files", [])
                        )
                    )
                    _fp = _hl.sha256(_fp_src.encode("utf-8", "ignore")).hexdigest()
                    if outer_attempt > 0 and _fp == _last_artifact_fingerprint:
                        logger.warning(
                            "[CodeValidator] sin progreso: el artefacto del "
                            f"reintento {outer_attempt} es idéntico al anterior — "
                            "cortando el repair loop (evita regenerar el mismo "
                            "código por caché)"
                        )
                        result.metadata["validation_stalled"] = True
                        break
                    _last_artifact_fingerprint = _fp

                # ✅ Fase 3: CoderAgent (Fase 2) ya valida que el contrato
                # esté completo y que el código compile como blob único
                # (analyze_code_quality). Acá se agrega lo que CoderAgent
                # no hace: compilar cada archivo del contrato por separado,
                # y correr los tests de verdad en sandbox — la capa
                # dinámica real. Si no hay code_artifact (CODE_EXPLANATION/
                # CODE_REVIEW/etc., contrato deshabilitado, o needs_chunked)
                # no hay nada que validar ni reintentar acá — se corta.
                code_artifact = (result.metadata or {}).get("code_artifact") if result.success else None
                if not code_artifact:
                    break

                try:
                    contract  = CodeContract.from_dict(code_artifact)
                    sandbox   = get_sandbox_executor(timeout=settings.code_validation_timeout)
                    validator = CodeValidator(sandbox=sandbox, settings=settings)
                    report    = validator.validate(contract)
                    result.metadata["validation_report"] = report.to_dict()
                except Exception as e:
                    _log_error(e, "_handle_code.validation", extra_info={"task_type": task_type})
                    logger.warning(f"CodeValidator: validación omitida por error interno: {e}")
                    break

                if report.passed:
                    # ✅ Fix (encontrado con harness): no alcanza con NO tocar
                    # el flag acá — hay que forzarlo True explícitamente. Si
                    # se llegó a este punto tras un reintento, el
                    # validation_passed que trae `result` es el heurístico
                    # interno de CoderAgent (Fase 2) para ESE intento
                    # puntual, que puede no coincidir con que la validación
                    # real (Fase 3) sí pasó. La fuente de verdad acá es
                    # `report`, no la heurística.
                    result.validation_passed = True
                    break

                result.validation_passed = False
                if outer_attempt < max_validation_retries:
                    validation_critique = build_validation_critique(report)
                    logger.info(
                        f"CodeValidator: reintento {outer_attempt + 1}/{max_validation_retries} — "
                        f"{report.error_summary}"
                    )
                else:
                    logger.warning(
                        f"CodeValidator: agotados los reintentos, entrega con validación "
                        f"fallida — {report.error_summary}"
                    )

            # ✅ Fase 7: para trazar en TraceStore (dispatcher.py lee esto).
            # outer_attempt conserva su último valor tras el for — 0 si
            # nunca hubo que reintentar (o si no hay code_artifact y el
            # loop cortó en la primera vuelta).
            if result.metadata is not None:
                result.metadata["validation_retries"] = outer_attempt if code_artifact else 0

            # ── ✅ ISSUE-142: el modelo se NEGÓ (censura) ────────────────────
            # El agente ya no gasta reintentos en una negativa (medido: 63,7 s de
            # reintentos para nada) y el despachador ya intentó UNA vez el respaldo
            # (`llm_code_model_fallback`, arriba). Se llega acá cuando el respaldo
            # **también** se negó, no está descargado o no está configurado: se
            # convierte en un mensaje CLARO para el usuario, nombrando el modelo.
            _refusal = (result.metadata or {}).get("code_refusal")
            if _refusal:
                _modelo = result.model_used or settings.llm_code_model
                _respaldo = (result.metadata or {}).get("code_fallback_usado")
                _no_instalado = (result.metadata or {}).get("code_fallback_no_instalado")
                if _respaldo:
                    _estado = (f"- Se reintentó con el respaldo `{_respaldo}` y "
                               f"**también** se negó.\n")
                elif _no_instalado:
                    _estado = (f"- El respaldo `{_no_instalado}` **no está descargado**: "
                               f"no se reintentó (instalalo con `ollama pull {_no_instalado}`).\n")
                else:
                    _estado = ("- No se reintentó: sin código no hay nada que corregir "
                               "(antes se gastaban ~60 s en reintentos inútiles) y no hay "
                               "modelo de respaldo configurado.\n")
                return Response(
                    content=(
                        f"🚫 El modelo de código (`{_modelo}`) **se negó a generar esto** "
                        f"(censura): no es un error del sistema.\n\n"
                        f"- Frase con la que se negó: «{_refusal}»\n"
                        f"{_estado}\n"
                        f"Podés cambiar el modelo de código en "
                        f"`config/models.yaml` → `task_mappings.code_generation.preferred_model` "
                        f"o con la variable `AXIOMA_LLM_CODE_MODEL`, o pedirlo de otra forma."
                    ),
                    session_id=session_id, model_used=_modelo, task_type=task_type,
                    latency_ms=(time.time() - start) * 1000, confidence=0.9,
                    sources=[], validation_passed=False,
                    error=f"code_refusal: {_refusal}",
                    metadata={"code_refusal": _refusal,
                              "refusal_raw": (result.metadata or {}).get("refusal_raw", "")},
                )

            return result.to_response(session_id)

        except Exception as e:
            _log_error(e, "_handle_code", extra_info={"task_type": task_type})
            logger.error(f"CoderAgent error: {e}")
            return Response(
                content=f"Error generando código: {e}",
                session_id=session_id, model_used=settings.llm_code_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )
    async def _handle_validation(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ ValidatorAgent: {task_type}")
        _log_event("DISPATCHER", f"ValidatorAgent started: {task_type}", extra={
            "query": message.content[:50] if message.content else "",
        })

        try:
            result = await asyncio.to_thread(self.validator.execute, message, task_type=task_type)
            _log_agent_execution("ValidatorAgent", task_type, "completed",
                                 duration=time.time() - start,
                                 success=result.success, error=result.error)
            return Response(
                content=result.content,
                session_id=session_id, model_used=settings.validator_model or settings.llm_fallback_1 or settings.llm_default_model,  # v0.6.9h
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=getattr(result, 'confidence', 0.0),
                sources=[], validation_passed=getattr(result, 'validation_passed', False),
                error=getattr(result, 'error', None),
                metadata=getattr(result, 'metadata', {}),
            )

        except Exception as e:
            _log_error(e, "_handle_validation", extra_info={"task_type": task_type})
            logger.error(f"ValidatorAgent error: {e}")
            return Response(
                content=f"Error en validación: {e}",
                session_id=session_id, model_used=settings.llm_fallback_1,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.3.0: Handler: Chat con Bucle de Herramientas (Agent Loop)
    # ═══════════════════════════════════════════════════════════════


    def _format_tools_for_ollama(self, tool_names: List[str]) -> List[Dict[str, Any]]:
        """Convierte las tools de AXIOMA al formato JSON esperado por Ollama."""
        registry = get_registry()
        ollama_tools = []
        for name in tool_names:
            if registry.has(name):
                tool = registry.get(name)
                params_schema = {"type": "object", "properties": {}, "required": []}
                for p in tool.schema.params:
                    params_schema["properties"][p.name] = {
                        "type": p.type,
                        "description": p.description
                    }
                    if p.required:
                        params_schema["required"].append(p.name)

                ollama_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": params_schema
                    }
                })
        return ollama_tools


    async def _run_pending_approval(
        self, pending: Dict[str, Any], session_id: Optional[str],
        task_type: str, start: float,
    ) -> Response:
        """Ejecuta la herramienta que el usuario aprobó (HITL, v0.6.9r)."""
        tool_name = pending.get("tool")
        args = pending.get("args") or {}
        import src.core.dispatcher_handlers_extra as _dhx  # diferido: estado compartido
        _dhx._PENDING_APPROVALS.pop(session_id, None)
        if not tool_name:
            return Response(
                content="No hay acción pendiente.", session_id=session_id,
                model_used=settings.llm_default_model, task_type=task_type,
                latency_ms=(time.time() - start) * 1000, confidence=0.8,
                sources=[], validation_passed=True, error=None)
        try:
            executor = get_executor()
            ctx = ExecutionContext(
                session_permission=ToolPermission.PRIVILEGED,
                timeout_seconds=60.0, caller="HITL_UserApproved",
                resource_path=tool_name)
            res = await executor.run(tool_name, context=ctx, **args)
            return Response(
                content=(res.output if res.success else f"Error: {res.error}"),
                session_id=session_id,
                model_used=settings.llm_default_model, task_type=task_type,
                latency_ms=(time.time() - start) * 1000,
                confidence=0.9, sources=[], validation_passed=res.success,
                error=None if res.success else res.error,
                metadata={"approval_executed": True})
        except Exception as exc:
            _log_error(exc, "_handle_chat.pending_approval",
                       extra_info={"tool": tool_name, "task_type": task_type})
            return Response(
                content=f"❌ Error ejecutando la acción aprobada: {exc}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type,
                latency_ms=(time.time() - start) * 1000, confidence=0.5,
                sources=[], validation_passed=False, error=str(exc))
    async def _handle_autonomous_code(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        _log_event("DISPATCHER", "InterpreterLoop started", extra={
            "query": message.content[:80] if message.content else "",
            "session_id": session_id,
        })

        try:
            from src.core.interpreter_loop import get_interpreter_loop
            from src.tools_mcp.tool_executor import get_executor
            from src.core.workspace_manager import get_workspace_manager

            loop = get_interpreter_loop(
                coder=self.coder,
                executor=get_executor(),
                workspace_manager=get_workspace_manager(),
            )

            result = await loop.run(
                query=message.content,
                session_id=session_id,
            )

            duration = time.time() - start
            _log_agent_execution(
                "InterpreterLoop", task_type, result.stop_reason,
                duration=duration, success=result.success,
                error=None if result.success else result.stop_reason,
            )
            _log_event("INTERPRETER_LOOP", f"Completed: {result.stop_reason}", extra={
                "attempts": result.total_attempts,
                "duration_ms": round(result.duration_ms, 2),
                "success": result.success,
            })

            return result.to_response(session_id)

        except Exception as e:
            _log_error(e, "_handle_autonomous_code")
            logger.error(f"InterpreterLoop error: {e}")
            return Response(
                content=f"Error en modo autónomo: {e}",
                session_id=session_id, model_used="interpreter_loop",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )


    def _should_use_autonomous_mode(self, query: str) -> bool:
        """
        Detecta si la query pide EJECUTAR código (modo autónomo → InterpreterLoop).

        ✅ FIX v0.6.8 (hallazgo 3.2 del resumen_estado_proyecto):
        antes era substring matching plano (`kw in q`), lo que producía
        falsos positivos masivos: "test correspondiente" matcheaba
        "corre" (dentro de "correspondiente"), "correo" matcheaba
        "corre", "aprueba" matcheaba "prueba", etc. Toda query de
        generación de código normal que contuviera una palabra con
        "corre"/"prueba"/"testea" como subcadena se ruteaba al modo
        autónomo (63-163s, sin pasar por el contrato Fases 1-4).
        Ahora usa word boundaries: solo la palabra COMPLETA dispara.

        ✅ FIX v0.6.8pp (logs/reg_error.txt 15:03, caso code_testing):
        "escribí en una línea: \"prueba de historial netron-123\"" entró
        al modo autónomo porque "prueba" aparecía como palabra COMPLETA…
        pero dentro del TEXTO ENTRE COMILLAS que el usuario pedía escribir
        (era contenido, no una orden de probar código). Resultado: 234 s de
        InterpreterLoop intentando "procesar historial". Ahora se eliminan
        los spans entre comillas antes de matchear: una keyword que solo
        aparece citada (string a escribir, nombre de archivo, cita) ya no
        dispara ejecución de código.
        """
        # texto sin los fragmentos citados ("…" / '…' / “…”)
        q_no_quotes = re.sub(
            r"[\"'“”‘’]([^\"'“”‘’]{0,120})[\"'“”‘’]", " ", (query or "").lower()
        )
        q = q_no_quotes
        keywords = [
            "ejecuta", "ejecutá", "ejecutar", "ejecútalo", "ejecutalo",
            "corre", "corré", "correr", "correlo", "corrélo",
            "run", "corre el", "corre el test",
            "prueba", "probá", "probar", "pruébalo", "pruebalo",
            "testea", "testeá", "testear",
            "y ejecútalo", "y ejecutalo", "y correlo", "y corrélo",
            "y pruébalo", "y pruebalo", "y ejecuta", "y ejecutá",
            "y corre", "y corré", "y prueba", "y probá",
        ]
        for kw in keywords:
            # word boundary: la keyword debe ser palabra completa dentro de la query
            pattern = r"(?<!\w)" + re.escape(kw) + r"(?!\w)"
            if re.search(pattern, q):
                return True
        return False
