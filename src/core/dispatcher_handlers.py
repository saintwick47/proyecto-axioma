#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Dispatcher: Logging helpers y Handlers de agentes
# Ruta: src/core/dispatcher_handlers.py
# Autor: SaintWick
# ✅ v1.2.1: Actualización de comentarios — Eliminada referencia a fallback de visión.
#    Ahora VisionAgent usa exclusivamente qwen3-vl:4b (settings.llm_vision_model).
# ✅ v1.2.0: FIX SCREENSHOT — ScreenCaptureEngine antes de VisionAgent
# ✅ v1.1.0: _handle_screenshot_analysis() — VisionAgent + ScreenCaptureEngine hook
# ✅ v1.3.0 (TOOL CALLING): _handle_chat reescrito como Agent Loop nativo.
#    El LLM decide si usar herramientas (web_search, news, etc.) sin depender
#    de clasificadores regex/keywords frágiles.
# ═══════════════════════════════════════════════════════════════
# ✅ REFACTOR 50/50: la mitad de ARCHIVOS/CARPETAS de este módulo vive en
#    src/core/dispatcher_handlers_files.py (DispatcherHandlersFilesMixin):
#    helpers de logging compartidos, detección carpeta/fs_* y los handlers
#    _handle_file_list_folder / _handle_file_content_search /
#    _handle_folder_analysis. Este módulo los re-importa con los MISMOS
#    nombres y DispatcherHandlersMixin hereda del mixin ⇒ API intacta.
#    Dependencia en un solo sentido: dispatcher_handlers → *_files.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import re
import time
import inspect
from typing import Optional, Any
from src.domain.entities import Message, Response
from config.settings import settings

# ✅ NUEVO v1.3.0: Imports para Tool Calling en Agent Loop
# ✅ Fase 3: validación multicapa determinista sobre el contrato ya
# parseado en Fase 2 (CoderAgent), no reparsea la respuesta cruda.
# code_validator.py vive junto a code_contract.py en src/agents/.

logger = logging.getLogger(__name__)

try:
    from tools.detailed_logger import get_logger  # noqa: F401 — disponibilidad
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False
# ✅ REFACTOR 50/50: nombres movidos al hermano, re-importados con los
# MISMOS nombres para conservar la API pública (dispatcher.py, dispatcher_
# process*.py, dispatcher_handlers_extra*.py y los tests siguen importándolos
# desde acá). La dependencia va en un solo sentido: este módulo → hermano.
from .dispatcher_handlers_files import (  # noqa: E402,F401 — re-export de compatibilidad
    DispatcherHandlersFilesMixin,
    _FOLDER_HINTS,
    _FOLDER_VERBS,
    _FS_CONTENT_VERBS,
    _FS_TOOL_TOKENS,
    _extract_content_keyword,
    _extract_folder_candidates,
    _get_caller_info,
    _is_folder_analysis_query,
    _is_fs_tool_intent,
    _log_agent_execution,
    _log_error,
    _log_event,
    _log_model_call,
    _log_router_decision,
)


# ═══════════════════════════════════════════════════════════════
# Defaults de max_tokens por tipo de tarea
# ═══════════════════════════════════════════════════════════════
# ✅ v0.7.5 (ISSUE-074/A3a): los presupuestos se ELIGEN para que entren en el
# timeout de su ruta, con la velocidad medida en CPU (6,3 tok/s para qwen3:8b):
#   · chat:     512 tok ⇒ 512/3,5 = 146 s + prefill ✅ dentro de 180 s
#               (antes 4096 ⇒ 1170 s al peor TG medido: timeout + reintento seguros)
#   · análisis: 1536 tok ⇒ 439 s + prefill ✅ dentro de 900 s (nivel 3, el
#               timeout que usa esa ruta desde v0.7.5)
#               (antes 8192 con timeout de 420 s ⇒ 2340 s, 5,6× el timeout)
# El cliente además acota cualquier presupuesto al timeout REAL de la petición
# (`num_predict_coherente`, client_base.py), así que el desajuste no puede volver.
MAX_TOKENS_ANALYSIS = 1536
MAX_TOKENS_CHAT = 512
# El default genérico del cliente es `DEFAULT_NUM_PREDICT = 512` (client_base):
# con el timeout STANDARD (45 s) el invariante lo acota a 255 y lo registra.
MAX_TOKENS_DEFAULT = 512


# ═══════════════════════════════════════════════════════════════
# ✅ v1.0.5: System prompts especializados por tipo de contenido
# ═══════════════════════════════════════════════════════════════
CODE_REVIEW_SYSTEM_PROMPT = """Eres un ingeniero de software senior realizando una revisión de código rigurosa. Tu trabajo es encontrar bugs REALES, no dar consejos genéricos.

REGLAS ESTRICTAS:
1. LEE el código línea por línea antes de responder.
2. NO sugieras librerías que YA ESTÁN importadas en el código.
3. NO des consejos genéricos (añadir comentarios, usar clases, etc.).
4. SOLO reporta bugs, errores silenciosos, edge cases y vulnerabilidades de seguridad.
5. Para cada bug, especifica: variable/function exacta, condición que causa el fallo, y tipo de error (TypeError, NameError, etc.).
6. Verifica SIEMPRE:
   - Variables que pueden ser None (especialmente retornos de psutil, requests, subprocess)
   - Variables usadas pero nunca asignadas (NameError)
   - Variables asignadas pero nunca usadas (código muerto)
   - Faltas de try/except en llamadas al sistema (FileNotFoundError, TimeoutError)
   - Inyección de comandos o path traversal
   - Timeout en subprocess/run_cmd
7. Si el código no tiene errores reales, di "No se encontraron bugs críticos" y explica brevemente por qué es robusto.
8. Formato de salida:
   ## 🔴 Errores Críticos (causan crash)
   ## 🟡 Errores Silenciosos (datos incorrectos o pérdida de información)
   ## 🟢 Mejoras de Robustez (no crashean pero deberían corregirse)"""

GENERAL_ANALYSIS_SYSTEM_PROMPT = """Eres un analista técnico experto. Proporciona análisis profundos y específicos basados en el contenido proporcionado. No repitas lo obvio. Enfócate en hallazgos accionables y errores que un revisor novato pasaría por alto. Si encuentras problemas, sé específico sobre dónde están y por qué importan."""


def _detect_code_content(content: str) -> bool:
    """
    Detecta si el contenido incluye código fuente que requiere
    revisión especializada en vez de análisis genérico.
    """
    if not content:
        return False
    # Bloques de código markdown
    if "```" in content:
        return True
    # Indicadores de código Python
    python_indicators = ["def ", "class ", "import ", "from ", "async def ", "if __name__"]
    python_count = sum(1 for kw in python_indicators if kw in content)
    if python_count >= 2:
        return True
    # Indicadores de código en otros lenguajes
    other_indicators = ["function ", "const ", "public class ", "void ", "fn ", "func "]
    other_count = sum(1 for kw in other_indicators if kw in content)
    if other_count >= 2:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# ¿La consulta pide un DATO VERIFICABLE? (ISSUE-063)
# ═══════════════════════════════════════════════════════════════
# Caso real del log (12-09): "¿Dónde se ubica la falla geológica en el valle de
# Calamuchita…?" se respondió desde la memoria del modelo (0 fuentes, 208 s) y el
# panel la mostró como "Alto (88%)" porque el validador le dio 9.0 — el validador
# juzga el TEXTO, no si el dato está respaldado. Estos marcadores detectan
# consultas de DATO (ubicación, fecha, cifra, autoría…) donde una respuesta SIN
# fuentes no debería presentarse como altamente confiable.
_FACTUAL_MARKERS = (
    "donde se ubica", "dónde se ubica", "donde queda", "dónde queda",
    "donde esta", "dónde está", "donde estan", "dónde están",
    "ubicacion de", "ubicación de", "se encuentra en",
    "quien es", "quién es", "quienes son", "quiénes son",
    "cuando ocurrio", "cuándo ocurrió", "cuando sucedio", "cuándo sucedió",
    "en que año", "en qué año", "en que fecha", "en qué fecha",
    "cuantos habitantes", "cuántos habitantes", "poblacion de", "población de",
    "cual es la capital", "cuál es la capital", "capital de",
    "cuanto mide", "cuánto mide", "cuanto cuesta", "cuánto cuesta",
    "precio de", "cotizacion de", "cotización de",
    "historia de", "origen de", "definicion de", "definición de",
    # ✅ v0.7.5 (ISSUE-073, medido en la corrida real del 2026-09-28 17:06):
    # "quien sera el proximo gobernador de cordoba? hay candidatos para
    # intendente en villa general belgrano" NO matcheaba ningún marcador ⇒
    # `general_chat` con **0 tools y 0 fuentes** ⇒ el modelo improvisó sobre una
    # elección FUTURA. Candidaturas, elecciones y encuestas son datos
    # verificables: deben venir con fuentes.
    "quien sera", "quién será", "quienes seran", "quiénes serán",
    "quien va a ganar", "quién va a ganar", "quien ganara", "quién ganará",
    "quien gano", "quién ganó", "proximo gobernador", "próximo gobernador",
    "proximo presidente", "próximo presidente", "proximo intendente",
    "próximo intendente", "proximas elecciones", "próximas elecciones",
    "elecciones", "candidato", "candidatos", "candidatura", "candidaturas",
    "encuesta", "encuestas", "resultado electoral", "padron electoral",
)
# Excluidos: consultas conversacionales o creativas donde NO se espera cita.
_NOT_FACTUAL_HINTS = (
    "chiste", "poema", "cuento", "historia inventada", "roleplay",
    "traducí", "traduce", "resumí este texto", "reescribí",
)
# Excluidos: pedidos de CÓDIGO. Medido (2026-09-28): "Implementá una clase
# Candidato en Python con get y put" matcheaba el marcador "candidato" y el chat
# habría disparado una búsqueda web inútil. Una orden de programar no espera
# citas, aunque mencione palabras que en otro contexto serían un dato.
_CODE_HINTS = (
    "clase", "funcion", "función", "metodo", "método", "implementa", "implementá",
    "codigo", "código", "script", "def ", "class ", "import ", "python",
    "javascript", "pytest", "tests", "refactor", "depurar",
)


def expects_factual_grounding(query: str) -> bool:
    """True si la consulta pide un dato verificable (y por lo tanto una respuesta
    sin fuentes no debería mostrarse como muy confiable). PURA y barata."""
    if not query:
        return False
    # ⚠️ Normalizar espacios: la consulta real del log traía doble espacio
    # ("Donde  se ubica") y con `lower()` a secas no matcheaba ningún marcador.
    q = re.sub(r"\s+", " ", str(query)).strip().lower()
    if any(h in q for h in _NOT_FACTUAL_HINTS):
        return False
    if any(h in q for h in _CODE_HINTS) and not any(
            m in q for m in ("quien es", "quién es", "donde se ubica",
                             "dónde se ubica", "cuando ocurrio", "en que año")):
        return False
    return any(m in q for m in _FACTUAL_MARKERS)


# ── DispatcherHandlersMixin ──────────────────────────────────────────────────

class DispatcherHandlersMixin(DispatcherHandlersFilesMixin):
    """
    Mixin con todos los handlers de agentes y helpers de respuesta.

    Los handlers de archivos/carpetas (listado, búsqueda por contenido y
    análisis con grounding) viven en `DispatcherHandlersFilesMixin`
    (dispatcher_handlers_files.py). Acá quedan system/data/search y la
    entrada `_handle_file_search`, que delega en ese mixin.
    """

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.0.3 FIX CRÍTICO: _llm_generate_with_retry
    # ═══════════════════════════════════════════════════════════════
    async def _llm_generate_with_retry(
        self,
        prompt: str,
        model: str = None,
        max_retries: int = 2,
        base_timeout: float = 420.0,
        max_tokens: int = MAX_TOKENS_DEFAULT,
        system: Optional[str] = None,
    ) -> Any:
        """Llama a llm_client.generate() en hilo aparte con reintentos."""
        last_error = None
        internal_retries = settings.max_retries
        outer_timeout = (base_timeout * internal_retries) + 60.0

        for attempt in range(1, max_retries + 1):
            try:
                _log_event("LLM_RETRY", f"Attempt {attempt}/{max_retries}",
                           extra={"inner_timeout": base_timeout,
                                  "outer_timeout": outer_timeout,
                                  "prompt_len": len(prompt),
                                  "max_tokens": max_tokens,
                                  "has_system": system is not None})

                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.llm_client.generate,
                        prompt=prompt,
                        stream=False,
                        timeout=int(base_timeout),
                        max_tokens=max_tokens,
                        system=system,
                    ),
                    timeout=outer_timeout,
                )
                return result

            except asyncio.TimeoutError:
                last_error = TimeoutError(
                    f"Outer timeout after {outer_timeout}s (attempt {attempt}/{max_retries})"
                )
                logger.warning(
                    f"LLM outer timeout attempt {attempt}/{max_retries}: "
                    f"outer={outer_timeout}s, inner={base_timeout}s"
                )
                _log_model_call(
                    model=self.llm_client.model, action="generate",
                    task_type="retry", duration=outer_timeout, success=False,
                )
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.info(f"LLM retry: esperando {wait}s antes de reintento...")
                    await asyncio.sleep(wait)

            except Exception as e:
                last_error = e
                logger.warning(f"LLM error attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    wait = 2 ** attempt
                    await asyncio.sleep(wait)

        raise last_error or TimeoutError("LLM generate failed after all retries")

    # ── Handler: System ──────────────────────────────────────────────────────

    async def _handle_system(
        self, message: Message, task_type: str, session_id: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ SystemAgent: {task_type}")

        agent = self.system_agent
        if agent is None:
            return Response(
                content="SystemAgent no disponible",
                session_id=session_id, model_used="system",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False,
                error="SystemAgent no inicializado",
            )

        try:
            # ✅ FIX: session_id ahora se propaga a SystemAgent.execute() —
            # antes solo se usaba para result.to_response(session_id), nunca
            # llegaba al agente. PromotionGate (write_file/move_file/
            # delete_file) usa session_id para trackear la propuesta
            # pendiente; sin esto, todas las sesiones compartían "default".
            result = await asyncio.to_thread(
                agent.execute, message, task_type=task_type, session_id=session_id,
            )
            return result.to_response(session_id)
        except Exception as e:
            logger.error(f"SystemAgent error: {e}")
            return Response(
                content=f"Error en operación de sistema: {e}",
                session_id=session_id, model_used="system",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: Data ────────────────────────────────────────────────────────

    async def _handle_data(
        self, message: Message, task_type: str, session_id: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ DataHandler: {task_type}")
        _log_event("DISPATCHER", f"DataHandler started: {task_type}", extra={
            "query": message.content[:50] if message.content else "",
        })

        try:
            result = await asyncio.to_thread(
                self.data_handler.handle, message, task_type, session_id
            ) if not inspect.iscoroutinefunction(self.data_handler.handle) else \
                await self.data_handler.handle(message, task_type, session_id)

            _log_agent_execution("DataHandler", task_type, "completed",
                                 duration=time.time() - start, success=True)
            return result

        except Exception as e:
            _log_error(e, "_handle_data", extra_info={"task_type": task_type})
            logger.error(f"DataHandler error: {e}")
            return Response(
                content=f"Error obteniendo datos: {e}",
                session_id=session_id, model_used="data_handler",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: Code ────────────────────────────────────────────────────────


    async def _handle_search(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        logger.debug(f"→ SearcherAgent: {task_type}")
        _log_event("DISPATCHER", f"SearcherAgent started: {task_type}", extra={
            "query": message.content[:50] if message.content else "",
        })

        try:
            result = await asyncio.to_thread(
                self.searcher.execute, message, task_type=task_type,
                session_id=session_id,  # ✅ plan_memory (B)
                user_id=self._user_id,  # ✅ UI de usuarios (2026-08-31)
            )
            _log_agent_execution("SearcherAgent", task_type, "completed",
                                 duration=time.time() - start,
                                 success=result.success, error=result.error)
            return result.to_response(session_id)

        except Exception as e:
            _log_error(e, "_handle_search", extra_info={"task_type": task_type})
            logger.error(f"SearcherAgent error: {e}")
            return Response(
                content=f"Error en búsqueda: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: File Search (✅ plan_rafael 2026-08-31) ─────────────────────

    def _extract_search_name(self, query: str) -> str:
        """Extrae el nombre a buscar de la consulta ('buscá el archivo X' → X).

        ✅ plan_rafael (2026-08-31): las frases compuestas se quitan enteras;
        las palabras sueltas SOLO como palabra completa (\\b) — si no, "en"
        dentro de "benchmark" rompía el nombre.
        """
        import re
        q = (query or "").strip()
        if not q:
            return ""
        phrases = [
            "buscá el archivo", "busca el archivo", "buscar el archivo",
            "buscá la carpeta", "busca la carpeta", "buscar la carpeta",
            "buscar archivos", "buscar archivo", "buscar carpeta",
            "dónde está el archivo", "donde esta el archivo",
            "dónde está la carpeta", "donde esta la carpeta",
            "dónde se encuentra", "dónde está", "donde esta",
            "ubicación del archivo", "ubicacion del archivo",
            "ubicación de la carpeta", "ubicacion de la carpeta",
            "ruta del archivo", "encontrá el archivo", "encuentra el archivo",
            "cuál es el", "cual es el", "dónde queda", "donde queda",
            # ✅ plan_rafael (2026-08-31): lectura/resumen/cita
            "citá un bloque del archivo", "cita un bloque del archivo",
            "leé el archivo", "lee el archivo", "leer el archivo",
            "léeme el archivo", "leeme el archivo",
            "resumí el archivo", "resumi el archivo", "resumir el archivo",
            "mostrame el archivo", "mostrame el contenido del archivo",
        ]
        name = q
        for ph in phrases:
            name = name.replace(ph, " ")
        words = [
            "buscá", "busca", "buscar", "encontrá", "encuentra", "archivo",
            "archivos", "carpeta", "fichero", "ubicación", "ubicacion",
            "ruta", "dónde", "donde", "decime", "dime", "por",
            "favor", "el", "la", "los", "las", "del", "de", "en", "mi",
            "pc", "computadora", "computador", "quiero", "necesito", "saber",
            "cuál", "cual", "es", "queda", "y", "que", "con", "sobre",
            # ✅ plan_rafael (2026-08-31): verbos de lectura/resumen/cita
            "leé", "lee", "leer", "léeme", "leeme", "citá", "cita", "citar",
            "bloque", "bloques", "resumí", "resumi", "resumilo", "resumime",
            "resumen", "resumir", "resume", "sintetizá", "sintetiza",
            "mostrame", "mostrar", "contenido", "contenido",
        ]
        for w in words:
            name = re.sub(rf"\b{re.escape(w)}\b", " ", name, flags=re.IGNORECASE)
        name = " ".join(name.split())
        return name.strip(" ,.:;¿¡?¿!")[:80]

    def _detect_file_action(self, query: str) -> str:
        """'summary' | 'quote' | 'location' según la consulta (plan_rafael 2026-08-31)."""
        q = (query or "").lower()
        if any(w in q for w in ("resum", "sintetiza", "sintetiz")):
            return "summary"
        if any(w in q for w in ("cit", "bloque", "cita")):
            return "quote"
        return "location"

    async def _handle_file_search(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        start = time.time()
        query = message.content or ""
        logger.debug(f"→ FileSearchAgent: {query[:60]}")
        _log_event("DISPATCHER", "FileSearchAgent started", extra={
            "query": query[:50],
        })
        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not registry.has("fs_search"):
                return Response(
                    content="La búsqueda de archivos no está disponible (tool fs_search).",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )

            # ✅ v0.6.8ii: análisis de CARPETA (listar + leer + resumir con
            # grounding). Antes esto caía a general_chat/code_explanation y el
            # modelo respondía sin leer nada → divagaba (logs/reg_error.txt).
            if _is_folder_analysis_query(query):
                return await self._handle_folder_analysis(
                    message, task_type, session_id, context,
                )

            # ✅ v0.6.8pp (C): intención fs_* o "archivos que hablan de X" →
            # grep local del contenido (grounded, sin LLM). Antes el clasificador
            # mandaba "usá fs_search para…" a web_search/general_chat (43-47 s
            # de síntesis LLM al pedo).
            if _is_fs_tool_intent(query):
                # "usá fs_list para listar la carpeta X" → listado real
                if "fs_list" in query.lower():
                    resp = await self._handle_file_list_folder(
                        query, session_id, task_type, start,
                    )
                    if resp is not None:
                        return resp
                resp = await self._handle_file_content_search(
                    query, session_id, task_type, start,
                )
                if resp is not None:
                    return resp
                # sin carpeta/tema identificable → cae al flujo por nombre

            name = self._extract_search_name(query)
            if not name:
                return Response(
                    content="Decime qué archivo o carpeta querés buscar "
                            "(ej: 'buscá el archivo settings').",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )
            tool = registry.get("fs_search")
            result = await asyncio.to_thread(tool.run, name=name, max_results=10)
            matches = (result.data or {}).get("matches", [])

            # ✅ plan_rafael (2026-08-31): si pidió LEER/ RESUMIR / CITAR y hay
            # coincidencias, leemos el archivo y resumimos o citamos un bloque.
            action = self._detect_file_action(query)
            if action != "location" and matches and registry.has("fs_read"):
                path = matches[0]["path"]
                read = await asyncio.to_thread(
                    registry.get("fs_read").run, path=path, max_chars=6000,
                )
                if read.success and read.output and len(read.output) > 200:
                    content = read.output
                    if action == "summary":
                        summary_resp = await self._llm_generate_with_retry(
                            prompt=(
                                "Resumí en 3-5 frases concretas el contenido del "
                                "archivo que sigue. No inventes detalles.\n\n"
                                f"ARCHIVO: {path}\n\n{content[:5000]}"
                            ),
                            max_tokens=300,
                        )
                        summary_text = getattr(summary_resp, "content", "") or ""
                        return Response(
                            content=f"📄 Resumen de {path}:\n\n{summary_text}",
                            session_id=session_id,
                            model_used=settings.llm_default_model,
                            task_type=task_type,
                            latency_ms=(time.time() - start) * 1000,
                            confidence=0.9, validation_passed=True, error=None,
                            sources=[path],
                        )
                    # quote: bloque representativo del inicio del archivo
                    block = content[:1200]
                    return Response(
                        content=f"📄 Bloque de {path}:\n```\n{block}\n```\n\n"
                                "¿Querés que te lo resuma?",
                        session_id=session_id, model_used="fs_read",
                        task_type=task_type,
                        latency_ms=(time.time() - start) * 1000,
                        confidence=0.9, validation_passed=True, error=None,
                        sources=[path],
                    )

            content = result.output or "No se encontró."
            _log_agent_execution("FileSearchAgent", task_type, "completed",
                                 duration=time.time() - start,
                                 success=result.success, error=result.error)
            return Response(
                content=content,
                session_id=session_id, model_used="fs_search",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.95 if result.success else 0.4,
                validation_passed=result.success,
                error=result.error if not result.success else None,
                sources=[m.get("path", "") for m in matches][:5],
            )
        except Exception as e:
            _log_error(e, "_handle_file_search", extra_info={"task_type": task_type})
            return Response(
                content=f"Error buscando archivos: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )
