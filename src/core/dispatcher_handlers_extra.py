#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# dispatcher_handlers_extra.py — AXIOMA: 2ª mitad de los handlers
# ═══════════════════════════════════════════════════════════════
# Ruta: src/core/dispatcher_handlers_extra.py
# Versión: 0.6.8mm
# Propósito: Refactor 50/50 — dispatcher_handlers.py quedó en 1666
#            líneas. Este archivo recibe la mitad (handlers de
#            código/análisis/validación/chat/investigación/screenshot/
#            autónomo/pipeline) como un segundo mixin:
#            `DispatcherHandlersExtraMixin`. Dispatcher compone AMBOS
#            mixins, así que la API pública no cambia (self._handle_*
#            disponibles en Dispatcher).
#
# Módulo original conserva: helpers de logging, constantes de prompts,
# detección de carpeta/archivo, file_search/folder_analysis, system/data/
# search + _llm_generate_with_retry.
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
from .dispatcher_handlers_extra_code import DispatcherHandlersExtraCodeMixin

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

# ✅ v0.6.9r (1): HITL — aprobaciones de tools pendientes por sesión.
# El chat guarda aquí una acción riesgosa (gateway_ask) y espera la
# respuesta del usuario ("aprobá"/"cancelá") para ejecutarla.
_PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}


def _notify_progress(message: Any, phase: str) -> None:
    """Notifica a la UI la fase actual ("ver el agente trabajando" v0.6.9v)."""
    md = getattr(message, "metadata", None) or {}
    cb = md.get("_on_progress")
    if callable(cb):
        try:
            cb(phase)
        except Exception as e:
            # R3: la UI pierde una notificación de progreso, no el resultado.
            log_degraded(logger, e, "dispatcher._notify_progress (callback de UI)", contract_level=logging.DEBUG)

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════
# DispatcherHandlersExtraMixin — 2ª mitad (≈50%) de dispatcher_handlers
# ═══════════════════════════════════════════════════════════════
class DispatcherHandlersExtraMixin(DispatcherHandlersExtraCodeMixin, DispatcherPlanMixin):
    """Handlers movidos en el refactor 50/50 (v0.6.8mm).

    Se compone en Dispatcher junto a DispatcherHandlersMixin; los métodos
    pueden usar self._* de cualquiera de las dos mitades.
    """


    # ── Handler: Search ──────────────────────────────────────────────────────


    async def _handle_analysis(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start_time = datetime.now(timezone.utc)
        start = time.time()
        logger.debug(f"→ LLM Analysis: {task_type}")

        content = message.content or ""
        has_code = _detect_code_content(content)
        system_prompt = CODE_REVIEW_SYSTEM_PROMPT if has_code else GENERAL_ANALYSIS_SYSTEM_PROMPT

        _log_event("DISPATCHER", f"LLM analysis started: {task_type}", extra={
            "query": content[:50],
            "has_context": context is not None,
            "has_code": has_code,
            "system_prompt_type": "code_review" if has_code else "general",
        })

        prompt = f"Contexto:\n{context}\n{content}" if context else content

        try:
            result = await self._llm_generate_with_retry(
                prompt=prompt,
                model=settings.llm_default_model,
                max_retries=2,
                # ✅ v0.7.5 (ISSUE-074/A3a, decisión del usuario): 420 s no
                # alcanzaba para archivos completos — el prefill de ~15k tokens
                # cuesta ~330 s por sí solo (medido) y el invariante recortaba la
                # respuesta a ~80 tokens. Ahora usa el nivel 3 del coder (900 s),
                # que ya estaba configurado en `settings`.
                base_timeout=float(settings.get_timeout_for_code_level(3)),
                max_tokens=MAX_TOKENS_ANALYSIS,
                system=system_prompt,
            )

            duration = time.time() - start
            _log_model_call(
                model=settings.llm_default_model, action="generate",
                task_type=task_type, duration=duration, success=True,
                input_size=len(prompt),
                output_size=len(result.content) if hasattr(result, 'content') else 0,
            )

            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            result_content = result.content if hasattr(result, 'content') else str(result)

            return Response(
                content=result_content, session_id=session_id,
                model_used=settings.llm_default_model, task_type=task_type,
                latency_ms=latency, confidence=0.75, sources=[],
                validation_passed=True, error=None,
                # ✅ ISSUE-082: uso real de tokens (antes quedaba en 0 en traces.db).
                metadata=tokens_de_respuesta(result),
            )

        except Exception as e:
            _log_error(e, "_handle_analysis", extra_info={"task_type": task_type})
            logger.error(f"Analysis error: {e}")
            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            return Response(
                content=f"Error en análisis: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=latency,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: Validation ──────────────────────────────────────────────────



    async def _handle_chat(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        """
        Chat con historial de mensajes y soporte para Tool Calling nativo.
        El LLM decide si necesita usar herramientas (web_search, news, etc.)
        basándose en el contexto de la conversación.
        """
        start_time = datetime.now(timezone.utc)
        start = time.time()
        logger.debug(f"→ LLM Chat (Agent Loop): {task_type}")

        # ✅ v0.6.9r (1): HITL — si hay una aprobación pendiente de esta sesión,
        # la respuesta del usuario la aprueba o la cancela antes de todo.
        _user_text = (message.content or "").strip()
        _pending = _PENDING_APPROVALS.get(session_id) if session_id else None
        if _pending:
            _low = _user_text.lower()
            if _low in {"aprobá", "aprobado", "si", "sí", "confirmá",
                        "confirmar", "ok", "dale", "ejecutar", "aceptá"}:
                return await self._run_pending_approval(
                    _pending, session_id, task_type, start)
            if _low in {"cancelá", "cancelar", "no", "rechazá", "rechazar"}:
                _PENDING_APPROVALS.pop(session_id, None)
                return Response(
                    content="Acción cancelada.", session_id=session_id,
                    model_used=settings.llm_default_model, task_type=task_type,
                    latency_ms=(time.time() - start) * 1000, confidence=0.8,
                    sources=[], validation_passed=True, error=None,
                    metadata={"approval_cancelled": True})

        # ✅ v0.6.8nn (plan adaptado F1): tools SEGURAS del registry en el chat.
        # Antes: lista hardcodeada ["web_search","news","weather","finance"].
        # Ahora: adapters web + lectura de archivos (fs_read/fs_list/fs_search
        # son READ_ONLY, permiso 0 ≤ NETWORK del contexto). Las tools de
        # escritura/terminal quedan FUERA del chat por ahora (PLAN_ADAPTADO.md).
        _notify_progress(message, "🔍 Entendiendo la consulta...")

        _CHAT_SAFE_TOOLS = [
            "web_search", "news", "weather", "finance",
            "fs_read", "fs_list", "fs_search",
            "corpus_search",  # ✅ v0.6.9s: búsqueda en biblioteca documental
        ]
        registry = get_registry()
        available_tool_names = [n for n in _CHAT_SAFE_TOOLS if registry.has(n)]
        ollama_tools = self._format_tools_for_ollama(available_tool_names)

        # ✅ v0.6.8ss (B2): gate por verbo + ejemplos de tool-call.
        # Problema medido: qwen3:8b casi nunca llama tools ("Chat completed
        # without tools" en logs). Dos palancas baratas:
        #  1) Exponer tools SOLO si la consulta sugiere necesidad (reduce
        #     tokens de schema en chat puro y convierte la disponibilidad en
        #     señal fuerte para llamarlas cuando importa).
        #  2) System prompt con ejemplos concretos cuándo/cómo usar cada tool.
        _TOOL_TRIGGERS = (
            "busc", "busca", "investig", "noticia", "noticias", "clima",
            "cuánto", "cuanto", "precio", "cotización", "cotizacion", "dólar",
            "dolar", "actual", "última", "ultima", "último", "ultimo",
            "archivo", "carpeta", "directorio", "leé", "lee", "leer",
            "mostrame", "mostrá", "fs_", "hablan de", "mencion", "contiene",
            "contenido de", "fuente", "fuentes",
        )
        _q = (message.content or "").lower()
        # ✅ v0.7.5 (ISSUE-073): una consulta de DATO verificable activa las tools
        # aunque no traiga un "trigger" textual. Caso real del log (12-09):
        # "¿Dónde se ubica la falla geológica…?" no tenía ningún trigger ⇒
        # `_tools_active = False` ⇒ 0 de 8 tools, 0 fuentes, 208 s.
        _es_factual = expects_factual_grounding(message.content or "")
        _tools_active = bool(ollama_tools) and (
            any(t in _q for t in _TOOL_TRIGGERS) or _es_factual)

        system_prompt = (
            "Eres AXIOMA, un asistente local útil. "
            "No menciones que eres un modelo de IA; respondé directamente."
        )
        # ✅ Fase 1 (ISSUE-134): brevedad por defecto. En CPU la espera es
        # proporcional a lo que el modelo escribe (medido: ~6-8 tokens/s ⇒ 300
        # tokens son ~40-50 s más de espera). Respuestas cortas por defecto y
        # ampliar sólo si el usuario lo pide es la palanca de velocidad más
        # barata que existe; el interruptor permite medirlo y revertirlo.
        if settings.chat_respuestas_breves:
            system_prompt += (
                "\n\nSé BREVE: respondé en 1-3 oraciones (o pocos ítems si son "
                "pasos) y no repitas la pregunta. Si el usuario pide más detalle, "
                "ampliá lo necesario."
            )
        if _tools_active:
            _examples = []
            if "web_search" in available_tool_names:
                _examples.append(
                    "- Datos actuales o que no sabés (noticias, precios, clima): "
                    "llamá web_search con el parámetro query.")
            if "news" in available_tool_names:
                _examples.append(
                    "- Noticias recientes: llamá news con el parámetro query.")
            if "fs_search" in available_tool_names:
                _examples.append(
                    "- Archivos del proyecto: llamá fs_search (name) y si hay "
                    "coincidencias, fs_read (path) para leer el contenido.")
            system_prompt += (
                "\n\nPara responder, usá las HERRAMIENTAS disponibles cuando "
                "corresponda. Ejemplos:\n" + "\n".join(_examples)
                + "\n\nIMPORTANTE: las herramientas están disponibles EN ESTE "
                "turno. Si la consulta pide algo actual o un archivo del "
                "proyecto, llamá la función en vez de inventar la respuesta."
            )
        # ✅ ISSUE-130: los turnos previos de ESTA sesión van entre la
        # instrucción de sistema y el mensaje nuevo. Antes el chat era aislado
        # (cada consulta se respondía sin saber de qué se venía hablando) aunque
        # la memoria ya guardaba los intercambios vía `_record_exchange`. Sin
        # historial (sesión nueva, `session_id` vacío, memoria caída) la lista
        # queda igual que antes: el chat no depende de la memoria para andar.
        _historial = self._historial_para_prompt(session_id, message.content)
        if _historial:
            logger.debug(
                f"Chat con {len(_historial)} mensajes de historial "
                f"(sesión {session_id})"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            *_historial,
            {"role": "user", "content": message.content}
        ]

        MAX_TOOL_ITERATIONS = 3
        tools_executed = 0
        approval_asked = False  # ✅ v0.6.9r (1): HITL en el loop
        # ✅ v0.7.5 (ISSUE-073): fuentes del grounding factual. Se declara ACÁ
        # arriba porque la ruta de streaming (más abajo) también las usa: antes
        # estaba declarada después y esa salida habría dado NameError (lo cazó
        # `ruff --select F821`, no la suite).
        _fuentes_grounding: List[str] = []

        # ✅ v0.6.8ss (A3): streaming del chat SIN tools (1 generación),
        # detrás de settings.stream_chat_tokens (default OFF) y SOLO si el
        # caller (UI web) dejó un callback de tokens en
        # message.metadata["_on_token"]. Cualquier error → fallback síncrono.
        _stream_cb = (getattr(message, "metadata", None) or {}).get("_on_token")
        if (
            not _tools_active
            and settings.stream_chat_tokens
            and callable(_stream_cb)
        ):
            try:
                # ✅ Fase 1: `usage_sink` recoge el uso real del stream (antes el
                # camino con flujo devolvía la respuesta SIN tokens: las trazas
                # quedaban en 0), `max_tokens` le da el mismo tope de salida que
                # el camino síncrono (antes generaba de más: 1162 vs 589 chars
                # medidos). DETENER (ISSUE-134) ya funciona cancelando la tarea:
                # medido con el runner de Ollama, 545 % → 21-30 % de CPU.
                _uso_stream: Dict[str, Any] = {}
                _streamed_full = await self._stream_chat_response(
                    messages, _stream_cb, task_type,
                    max_tokens=MAX_TOKENS_CHAT, usage_sink=_uso_stream,
                )
                latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                _log_event("DISPATCHER", "Chat streamed (A3)", extra={
                    "task_type": task_type,
                    "latency_ms": round(latency, 2),
                    "chars": len(_streamed_full),
                    "tokens_generated": int(_uso_stream.get("eval_count") or 0),
                })
                # ✅ tokens_de_respuesta() es la implementación ÚNICA del mapeo
                # (ISSUE-082): se le pasa el uso del stream como objeto.
                _meta_stream = {
                    "streamed": True, "chat_tool_calls": 0,
                    **tokens_de_respuesta(SimpleNamespace(**_uso_stream)),
                }
                if _uso_stream.get("done_reason") == "length":
                    _meta_stream["was_truncated"] = True
                return Response(
                    content=_streamed_full, session_id=session_id,
                    model_used=settings.llm_default_model, task_type=task_type,
                    latency_ms=latency, confidence=0.85,
                    sources=_fuentes_grounding,
                    validation_passed=True, error=None,
                    metadata=_meta_stream,
                )
            except Exception as _se:
                logger.warning(f"[A3] streaming falló, fallback síncrono: {_se}")

        # ✅ v0.6.6: executor dentro del try para capturar fallos de inicialización.
        #   Antes estaba fuera y cualquier excepción en get_executor() subía
        #   sin capturar hasta process(), produciendo error sin respuesta al usuario.
        try:
            executor = get_executor()
            # ✅ v0.7.5 (ISSUE-073): grounding DETERMINISTA para consultas de dato.
            # No se depende de que el modelo llame la tool (qwen3:8b casi nunca lo
            # hace: "Chat completed without tools" en los logs): si la consulta pide
            # un dato verificable y hay web_search, se ejecuta acá y las URLs
            # quedan en `sources` (el panel de transparencia las muestra).
            if _es_factual and "web_search" in available_tool_names:
                try:
                    _ctx_g = ExecutionContext(
                        session_permission=ToolPermission.NETWORK,
                        timeout_seconds=30.0, caller="ChatGrounding",
                        resource_path="web_search")
                    _res_g = await executor.run(
                        "web_search", context=_ctx_g, query=message.content)
                    if _res_g.success and _res_g.output:
                        _txt_g = str(_res_g.output)
                        _fuentes_grounding = list(dict.fromkeys(
                            re.findall(r"https?://[^\s)\]\"'>]+", _txt_g)))[:8]
                        _bloque = (
                            "\n\nDATOS DE BÚSQUEDA WEB ya obtenidos para esta "
                            "consulta (no hace falta llamar web_search): citá la "
                            "fuente con su URL al responder. NO repitas las "
                            "etiquetas <fuente_externa> en tu respuesta.\n"
                            f"{_txt_g}")
                        messages[0]["content"] += (
                            _bloque if not settings.content_grounding_enabled
                            else f"\n\n<fuente_externa>{_bloque}</fuente_externa>")
                        tools_executed += 1
                        _log_event("DISPATCHER", "Grounding web previo (ISSUE-073)",
                                   extra={"fuentes": len(_fuentes_grounding),
                                          "chars": len(_txt_g)})
                    else:
                        logger.warning(
                            f"[ISSUE-073] web_search sin resultados para consulta "
                            f"factual: {getattr(_res_g, 'error', '?')}")
                except Exception as _ge:
                    log_degraded(logger, _ge,
                                 "dispatcher_handlers_extra._handle_chat.grounding")
            for iteration in range(MAX_TOOL_ITERATIONS):
                _log_event("DISPATCHER", f"Chat Agent Loop - Iteration {iteration + 1}", extra={
                    "session_id": session_id, "tools_available": len(ollama_tools)
                })

                # Si es la última iteración, forzar síntesis textual removiendo las herramientas
                current_tools = (
                    ollama_tools
                    if (_tools_active and iteration < MAX_TOOL_ITERATIONS - 1)
                    else None
                )

                _notify_progress(message, f"🤖 Generando (paso {iteration + 1})...")
                ollama_res = await asyncio.to_thread(
                    self.llm_client.chat,
                    messages=messages,
                    temperature=settings.default_temperature,
                    timeout=settings.ollama_timeout_chat,  # ✅ v0.6.8t: 60s hardcodeado era insuficiente en CPU
                    task_type=task_type,
                    # ✅ v0.7.5 (ISSUE-074/A3a): el tope de salida del chat ahora
                    # SE PASA (antes `MAX_TOKENS_CHAT` se importaba y no se usaba:
                    # el chat generaba sin tope hasta llenar el contexto ⇒ 650 s a
                    # 6,3 tok/s contra un timeout de 180 s).
                    max_tokens=MAX_TOKENS_CHAT,
                    tools=current_tools
                )

                # Si el LLM no pide herramientas, devolvemos la respuesta final
                if not ollama_res.tool_calls:
                    latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                    _log_event("DISPATCHER", "Chat completed without tools or final synthesis done", extra={
                        "iterations": iteration + 1,
                        "tools_executed": tools_executed,
                    })
                    response_meta = tokens_de_respuesta(ollama_res)  # ✅ ISSUE-082
                    if tools_executed:
                        response_meta["chat_tool_calls"] = tools_executed
                    return Response(
                        content=ollama_res.content, session_id=session_id,
                        model_used=settings.llm_default_model, task_type=task_type,
                        latency_ms=latency, confidence=0.85,
                        sources=_fuentes_grounding,
                        validation_passed=True, error=None,
                        metadata=response_meta or None,
                    )

                # El LLM pidió usar una herramienta. Procesar tool_calls
                _log_event("DISPATCHER", "LLM requested tool calls", extra={
                    "tools_requested": [tc.get("function", {}).get("name") for tc in ollama_res.tool_calls]
                })

                # Añadir el mensaje del asistente pidiendo tools al historial
                messages.append(ollama_res.message)

                # Ejecutar cada tool pedida
                for tool_call in ollama_res.tool_calls:
                    tools_executed += 1
                    func_info = tool_call.get("function", {})
                    tool_name = func_info.get("name")
                    _notify_progress(message, f"🔧 Ejecutando {tool_name}...")

                    # ✅ v0.6.8nn fix: Ollama devuelve `arguments` como DICT
                    # (no como string JSON). Antes json.loads(dict) reventaba
                    # (#4 en logs/reg_error.txt) y mataba toda la respuesta
                    # tras ~106 s de generación.
                    _raw_args = func_info.get("arguments", {})
                    if isinstance(_raw_args, dict):
                        tool_args = _raw_args
                    elif isinstance(_raw_args, str):
                        try:
                            tool_args = json.loads(_raw_args or "{}")
                        except json.JSONDecodeError:
                            tool_args = {}
                    else:
                        tool_args = {}

                    logger.info(f"[Agent Loop] Executing tool: {tool_name} with args: {tool_args}")

                    ctx = ExecutionContext(
                        session_permission=ToolPermission.NETWORK,
                        timeout_seconds=30.0,
                        caller="AgentLoop_Chat",
                        resource_path=tool_name
                    )

                    # ✅ v0.6.8nn: una tool que falle no mata la respuesta —
                    # se inyecta el error como resultado y el LLM decide.
                    try:
                        tool_result = await executor.run(tool_name, context=ctx, **tool_args)
                        # ✅ v0.6.9r (1): HITL — tool que requiere aprobación
                        # (gateway_ask): guardar la acción y devolver la
                        # pregunta al usuario (que aprueba/cancela en su
                        # próximo mensaje).
                        if not tool_result.success and \
                                (tool_result.metadata or {}).get("gateway_ask"):
                            _PENDING_APPROVALS[session_id] = {
                                "tool": tool_name, "args": tool_args,
                                "reason": tool_result.error,
                            }
                            _lat = (datetime.now(timezone.utc) -
                                    start_time).total_seconds() * 1000
                            return Response(
                                content=(
                                    "⚠️ **Se requiere aprobación para ejecutar "
                                    f"la herramienta `{tool_name}`**.\n\n"
                                    f"Motivo: {tool_result.error}\n\n"
                                    "Respondé **`aprobá`** para ejecutarla o "
                                    "**`cancelá`** para descartar."
                                ),
                                session_id=session_id,
                                model_used=settings.llm_default_model,
                                task_type=task_type,
                                latency_ms=_lat, confidence=0.85,
                                sources=[], validation_passed=True,
                                error=None,
                                metadata={"approval_pending": True},
                            )
                        raw_output = (tool_result.output if tool_result.success
                                      else f"Error: {tool_result.error}")
                    except Exception as _tool_err:
                        _log_error(_tool_err, "_handle_chat.tool_exec",
                                   extra_info={"tool": tool_name, "task_type": task_type})
                        raw_output = f"Error ejecutando {tool_name}: {_tool_err}"

                    # ✅ PLAN v2.0 (Fase 6): el output de una tool es contenido
                    # EXTERNO (resultados web, news, etc.) — envolverlo con el
                    # delimitador antes de inyectarlo al historial del LLM para
                    # que no se ejecuten instrucciones incrustadas en el resultado.
                    # Es el path de chat por defecto y el más expuesto.
                    if settings.content_grounding_enabled:
                        tool_content = (
                            f"<fuente_externa>{raw_output}</fuente_externa>"
                        )
                    else:
                        tool_content = raw_output

                    # Añadir el resultado de la tool al historial para la siguiente iteración
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "content": tool_content
                    })

            # Si agota iteraciones, el loop ya pidió síntesis en la última iteración
            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            _meta = tokens_de_respuesta(ollama_res) if ollama_res else {}  # ✅ ISSUE-082
            if tools_executed:
                _meta["chat_tool_calls"] = tools_executed

            # ✅ v0.6.9r (1): HITL — devolver la pregunta de aprobación.
            if approval_asked:
                _p = _PENDING_APPROVALS.get(session_id, {})
                return Response(
                    content=(
                        "⚠️ **Se requiere aprobación para ejecutar la "
                        f"herramienta `{_p.get('tool')}`**.\n\n"
                        f"Motivo: {_p.get('reason')}\n\n"
                        "Respondé **`aprobá`** para ejecutarla o "
                        "**`cancelá`** para descartar."
                    ),
                    session_id=session_id,
                    model_used=settings.llm_default_model,
                    task_type=task_type,
                    latency_ms=latency,
                    confidence=0.85,
                    sources=[],
                    validation_passed=True,
                    error=None,
                    metadata={"approval_pending": True},
                )

            return Response(
                content=ollama_res.content if ollama_res else "He alcanzado el límite de iteraciones de herramientas. Por favor, reformula tu consulta.",
                session_id=session_id,
                model_used=settings.llm_default_model,
                task_type=task_type,
                latency_ms=latency,
                confidence=0.7,
                sources=[],
                validation_passed=True,
                error=None,
                metadata=_meta or None,
            )

        except Exception as e:
            _log_error(e, "_handle_chat_agent_loop", extra_info={"task_type": task_type})
            logger.error(f"LLM chat (agent loop) error: {e}")
            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            return Response(
                content=f"Error en respuesta: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=latency,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: Research ────────────────────────────────────────────────────


    async def _handle_research(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ ResearcherAgent: {task_type}")

        try:
            result = await asyncio.to_thread(
                self.researcher.execute, message, task_type=task_type,
                session_id=session_id,  # ✅ plan_memory (B)
                user_id=self._user_id,  # ✅ UI de usuarios (2026-08-31)
            )
            _log_agent_execution("ResearcherAgent", task_type, "completed",
                                 duration=time.time() - start,
                                 success=result.success, error=result.error)
            return result.to_response(session_id)

        except Exception as e:
            _log_error(e, "_handle_research", extra_info={"task_type": task_type})
            logger.error(f"ResearcherAgent error: {e}")
            return Response(
                content=f"Error en investigación: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.2.2: _handle_screenshot_analysis — FIX FALLBACK
    # ✅ v1.2.2: PASO 3 reemplazado: elimina LLM texto con path. Retry VisionAgent
    #            con timeout+60s (Sub-caso A) o SystemAgent→bytes→VisionAgent (Sub-caso B).
    # ═══════════════════════════════════════════════════════════════


    async def _handle_screenshot_analysis(
        self,
        message: Message,
        task_type: str,
        session_id: Optional[str],
        context: Optional[str],
    ) -> Response:
        """
        Captura la pantalla activa y la analiza con VisionAgent (qwen3-vl:4b).

        Flujo:
        1. ScreenCaptureEngine.capture_sync() → captura bytes PNG
        2. Convertir a base64
        3. VisionAgent.execute(image_b64=...) → análisis (qwen3-vl:4b)
        4. Fallback: SystemAgent con command= (string) para terminal_exec
        """
        start = time.time()
        logger.debug(f"-> ScreenshotAnalysis: {task_type}")
        _log_event("DISPATCHER", f"ScreenshotAnalysis started: {task_type}", extra={
            "query": message.content[:50] if message.content else "",
            "session_id": session_id,
        })

        # ── PASO 1: Capturar pantalla con ScreenCaptureEngine ─────────────
        image_b64 = None
        capture_frame = None

        try:
            engine = self.screen_capture
            if engine is None:
                raise RuntimeError("ScreenCaptureEngine no disponible")
            _log_event("SCREEN_CAPTURE", "Capturing screen...", extra={
                "backend": "auto",
            })

            # Captura síncrona en thread
            capture_frame = await asyncio.to_thread(engine.capture_sync)

            if capture_frame and capture_frame.image_b64:
                image_b64 = capture_frame.image_b64
                _log_event("SCREEN_CAPTURE", "Screen captured successfully", extra={
                    "width": capture_frame.width,
                    "height": capture_frame.height,
                    "byte_size": capture_frame.byte_size,
                    "backend_used": capture_frame.backend_used,
                    "context_hint": capture_frame.context_hint,
                })
            else:
                _log_event("SCREEN_CAPTURE", "Screen capture returned None or empty",
                           level="WARNING")

        except ImportError as e:
            logger.warning(f"ScreenCaptureEngine not available: {e}")
            _log_event("SCREEN_CAPTURE", "ScreenCaptureEngine import failed",
                       level="ERROR", extra={"error": str(e)})
        except Exception as e:
            logger.warning(f"Screen capture error: {e}")
            _log_error(e, "_handle_screenshot_analysis.capture")

        # ── PASO 2: VisionAgent con image_b64 (gemma4:e4b) ────────────────
        vision = getattr(self, "vision_agent", None)

        if vision is not None and image_b64:
            try:
                # Detectar context_hint desde capture_frame
                context_hint = "generic"
                active_window = ""
                if capture_frame:
                    context_hint = capture_frame.context_hint or "generic"
                    active_window = capture_frame.active_window or ""

                _log_event("VISION_AGENT", "Calling VisionAgent (qwen3-vl:4b) with image_b64", extra={
                    "image_b64_len": len(image_b64),
                    "context_hint": context_hint,
                    "active_window": active_window,
                })

                result = await asyncio.to_thread(
                    vision.execute,
                    message,
                    task_type=task_type,
                    image_b64=image_b64,
                    context_hint=context_hint,
                    active_window=active_window,
                )

                _log_agent_execution(
                    "VisionAgent", task_type, "completed",
                    duration=time.time() - start,
                    success=result.success, error=result.error,
                )

                if result.success and result.content:
                    return result.to_response(session_id)

                logger.warning(f"VisionAgent result empty/failed: {result.error}")

            except Exception as e:
                logger.warning(f"VisionAgent error, trying fallback: {e}")
                _log_error(e, "_handle_screenshot_analysis.vision_agent")

        # ── PASO 3: Fallback — v1.2.2 FIX ────────────────────────────────
        # ELIMINADO: path "LLM texto + path string" (chat LLM no ve imágenes).
        # Sub-caso A: image_b64 de PASO 1 disponible → retry VisionAgent, timeout+60s.
        # Sub-caso B: sin image_b64 → SystemAgent captura disco → leer bytes → VisionAgent.
        vision    = getattr(self, "vision_agent", None)
        system_ag = getattr(self, "system_agent", None)

        if vision is not None and image_b64:
            try:
                # ✅ FIX: antes armaba un threading.Thread manual y le hacía
                # t.join(timeout=...) DESDE una coroutine — eso bloquea el
                # event loop ENTERO (no solo esta tarea) hasta extended_timeout+5
                # (hasta 245s), mismo problema que ya se corrigió en L1 classify()
                # y en el validation-fallback de dispatcher.py. asyncio.to_thread
                # + asyncio.wait_for logra lo mismo (override de timeout incluido)
                # sin congelar nada más mientras espera.
                base_timeout    = settings.ollama_timeout_vision
                extended_timeout = base_timeout + 90
                logger.info(f"VisionAgent retry: override timeout {base_timeout}s → {extended_timeout}s")
                _log_event("VISION_AGENT", "Retry con timeout extendido", extra={
                    "base_timeout": base_timeout,
                    "extended_timeout": extended_timeout,
                })

                # ✅ FIX: vision es una instancia COMPARTIDA (un solo
                # VisionAgent para todo el Dispatcher) — mutar vision.timeout/
                # vision._client.timeout sin lock es una race condition real
                # si dos análisis de pantalla caen en este retry al mismo
                # tiempo (una sesión le pisa el timeout a la otra a mitad de
                # camino). Lock de instancia, creado perezosamente porque este
                # mixin no tiene __init__ propio.
                if not hasattr(self, "_vision_retry_lock"):
                    self._vision_retry_lock = asyncio.Lock()

                async with self._vision_retry_lock:
                    # v1.2.3: override timeout en VisionAgent._client para que httpx
                    # respete el tiempo extendido (sin override, corta igualmente a base_timeout)
                    old_va_timeout  = vision.timeout
                    old_cli_timeout = vision._client.timeout
                    vision.timeout        = extended_timeout
                    vision._client.timeout = extended_timeout

                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                vision.execute,
                                message,
                                task_type=task_type,
                                image_b64=image_b64,
                                context_hint=capture_frame.context_hint if capture_frame else "generic",
                                active_window=capture_frame.active_window if capture_frame else "",
                            ),
                            timeout=extended_timeout + 5,
                        )
                        if result.success and result.content:
                            return result.to_response(session_id)
                        logger.warning(f"VisionAgent retry fallido: {result.error}")
                    except asyncio.TimeoutError:
                        logger.warning(f"VisionAgent retry timeout tras {extended_timeout}s")
                    finally:
                        # ✅ dentro del lock Y con try/finally: se restaura
                        # siempre (incluso si se cancela desde afuera) y
                        # ningún otro retry concurrente puede pisar el valor
                        # mientras este todavía no terminó de restaurar.
                        vision.timeout         = old_va_timeout
                        vision._client.timeout = old_cli_timeout
            except Exception as e:
                logger.warning(f"VisionAgent retry error: {e}")
                _log_error(e, "_handle_screenshot_analysis.vision_retry")

        elif vision is not None and system_ag is not None:
            try:
                shot_msg = Message(content="captura screenshot", role=message.role)
                shot = await asyncio.to_thread(
                    system_ag.execute, shot_msg, task_type="screenshot"
                )
                if shot.success:
                    _SHOT_PATH = "/tmp/axioma_screenshot.png"
                    try:
                        fb64 = base64.b64encode(open(_SHOT_PATH, "rb").read()).decode("utf-8")
                        result = await asyncio.to_thread(
                            vision.execute,
                            message,
                            task_type=task_type,
                            image_b64=fb64,
                            context_hint="generic",
                            active_window="",
                        )
                        if result.success and result.content:
                            return result.to_response(session_id)
                        logger.warning(f"VisionAgent (SystemAgent path) fallido: {result.error}")
                    except Exception as img_err:
                        logger.warning(f"No se pudo leer screenshot de disco: {img_err}")
                else:
                    logger.warning(f"SystemAgent screenshot fallido: {shot.error}")
            except Exception as e:
                logger.warning(f"Screenshot fallback error: {e}")
                _log_error(e, "_handle_screenshot_analysis.fallback")

        # ── Último recurso ────────────────────────────────────────────────
        logger.error("_handle_screenshot_analysis: VisionAgent y SystemAgent no disponibles")

        return Response(
            content=(
                "❌ No pude analizar la pantalla.\n\n"
                "Posibles causas:\n"
                "• ScreenCaptureEngine no disponible (¿estás en Wayland sin grim?)\n"
                "• VisionAgent no inicializado\n"
                "• Permisos insuficientes para captura\n\n"
                "Verifica que `scrot` o `grim` estén instalados."
            ),
            session_id=session_id, model_used="vision",
            task_type=task_type,
            latency_ms=(time.time() - start) * 1000,
            confidence=0.0, validation_passed=False,
            error="Screen capture unavailable",
        )

    # ── Autonomous Code (Open Interpreter) ────────────────────────────────────



    def _error_response(
        self, error_msg: str, session_id: Optional[str], start_time: datetime,
    ) -> Response:
        latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        _log_event("DISPATCHER", "Error response generated", extra={
            "error": error_msg[:100],
            "session_id": session_id,
            "latency_ms": round(latency, 2),
        })
        return Response(
            content=f"No pude procesar tu solicitud: {error_msg}",
            session_id=session_id, model_used="dispatcher",
            task_type="error", latency_ms=latency,
            confidence=0.0, sources=[], validation_passed=False,
            error=error_msg,
        )
