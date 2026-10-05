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
import asyncio
import logging
import re
import time
import inspect
import base64
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from src.domain.entities import Message, Response, MessageRole
from config.settings import settings

# ✅ NUEVO v1.3.0: Imports para Tool Calling en Agent Loop
from src.tools_mcp.tool_executor import get_executor, ExecutionContext, ToolPermission
from src.tools_mcp.tool_registry import get_registry
# ✅ Fase 3: validación multicapa determinista sobre el contrato ya
# parseado en Fase 2 (CoderAgent), no reparsea la respuesta cruda.
# code_validator.py vive junto a code_contract.py en src/agents/.
from src.agents.code_contract import CodeContract
from src.agents.code_validator import CodeValidator, build_validation_critique
from src.tools_mcp.sandbox import get_sandbox_executor
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


# ── Helpers de logging ───────────────────────────────────────────────────────

def _get_caller_info() -> dict:
    frame = inspect.currentframe()
    try:
        if frame and frame.f_back:
            return {
                "file": frame.f_back.f_code.co_filename,
                "line": frame.f_back.f_lineno,
                "function": frame.f_back.f_code.co_name,
            }
    finally:
        del frame
    return {}


def _log_event(category: str, message: str, level: str = "INFO", extra: dict = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level, extra=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_event")


def _log_error(error: Exception, context: str, extra_info: dict = None):
    caller = _get_caller_info()
    if not LOGGER_AVAILABLE:
        logger.error(f"dispatcher - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error,
                context=context,
                extra_info=extra_info,
                file_path=caller.get("file", ""),
                line_number=caller.get("line"),
                function_name=f"dispatcher.{context}",
            )
    except Exception:
        logger.error(f"dispatcher - {context}: {error}")


def _log_router_decision(
    query: str, task_type: str, handler: str,
    confidence: float, duration: float = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_router_decision(
                query=query, task_type=task_type, model_selected=handler,
                confidence=confidence, duration=duration,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_router_decision")


def _log_agent_execution(
    agent: str, task: str, state: str,
    duration: float = None, success: bool = True, error: str = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(
                agent_name=agent, task_type=task, state=state,
                duration=duration, success=success, error=error,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_agent_execution")


def _log_model_call(
    model: str, action: str, task_type: str,
    duration: float = None, success: bool = True,
    input_size: int = None, output_size: int = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_model_call(
                model_name=model, action=action, task_type=task_type,
                duration=duration, success=success,
                input_size=input_size, output_size=output_size,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/dispatcher_handlers.py:_log_model_call")


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
# ✅ v0.6.8ii: análisis de carpeta/directorio con grounding
# (fix de logs/reg_error.txt: "analiza la carpeta X" no leía el FS
#  → general_chat/code_explanation divagaban sin leer archivos)
# ═══════════════════════════════════════════════════════════════
_FOLDER_HINTS = ("carpeta", "directorio", "folder")
_FOLDER_VERBS = (
    "analiz", "explor", "revis", "examin", "estudi", "inspeccion",
    "qué hace", "que hace", "qué hacen", "que hacen",
    "para qué sirve", "para que sirve", "cómo funciona", "como funciona",
    "qué es", "que es", "resum", "explica",
)


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


def _is_folder_analysis_query(query: str) -> bool:
    """True si el usuario pide analizar/entender UNA CARPETA (no un archivo).

    Requiere un verbo de análisis + (hint de carpeta | ruta tools/src/... |
    "sus archivos/módulos"). Así no pisa búsquedas tipo "buscá la carpeta X"
    (ubicación) ni "leé el archivo X".
    """
    if not query:
        return False
    q = query.lower()
    has_verb = any(v in q for v in _FOLDER_VERBS)
    if not has_verb:
        return False
    if any(h in q for h in _FOLDER_HINTS):
        return True
    if re.search(r"(?:^|\s)(?:tools|src|config|tests|docs|data)/[a-z0-9_./\-]+", q):
        return True
    if "sus archivos" in q or "sus módulos" in q or "sus modulos" in q:
        return True
    return False


def _extract_folder_candidates(query: str, root: "Path"):
    """
    Resuelve carpetas candidatas (Path existentes) para la consulta.

    Entiende: "la carpeta netron", "netron que se encuentra dentro de la
    carpeta tools" (padre explícito), rutas tipo "tools/netron" o
    "src/core". Devuelve lista de directorios existentes, ordenada por
    preferencia (padre explícito > raíz > tools/ > src/).
    """
    from pathlib import Path

    q = query.lower()
    names: list = []

    # 1) Nombre tras "carpeta/directorio/folder"
    m = re.search(
        r"(?:carpeta|directorio|folder)\s+(?:de\s+|llamada\s+|llamado\s+"
        r"|del\s+|el\s+|la\s+|con\s+nombre\s+)?([a-z0-9_./\-]+)",
        q,
    )
    if m:
        name = m.group(1).strip(" .,:;¿?")
        if name:
            names.append(name.split()[0] if name.split() else name)

    # 2) Ruta explícita (tools/..., src/...) sin palabra "carpeta"
    m2 = re.search(
        r"(?:^|\s)((?:tools|src|config|tests|docs|data|axioma)/[a-z0-9_./\-]+)",
        q,
    )
    if m2:
        names.append(m2.group(1).strip(" .,:;¿?"))

    # 3) Carpeta padre explícita: "... X que se encuentra dentro de la carpeta Y"
    parent = None
    mp = re.search(
        r"dentro\s+de\s+(?:la\s+|el\s+)?(?:carpeta|directorio|folder\s+)?"
        r"([a-z0-9_./\-]+)",
        q,
    )
    if mp:
        parent = mp.group(1).strip(" .,:;¿?") or None

    # 4) Fallback: nombre desnudo al final ("... sus archivos dentro de netron")
    if not names and parent:
        names.append(parent)
    if not names:
        m4 = re.search(r"(?:de|sobre)\s+([a-z0-9_./\-]{2,})$", q)
        if m4:
            names.append(m4.group(1).strip(" .,:;¿?"))

    root = Path(root)
    candidates: list = []
    for name in dict.fromkeys(names):
        if not name:
            continue
        p = Path(name)
        if p.is_absolute():
            if p.is_dir():
                candidates.append(p)
            continue
        tries = []
        if parent and parent not in name:
            tries.append(root / parent / name)
        tries += [root / name, root / "tools" / name, root / "src" / name]
        for t in tries:
            if t.is_dir():
                candidates.append(t)
                break
    return candidates


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.8pp (C): rutear intenciones fs_* / "archivos que hablan
# de X" a file_search con grounding (no a web_search/general_chat).
# ═══════════════════════════════════════════════════════════════
_FS_TOOL_TOKENS = ("fs_search", "fs_read", "fs_list", "fs_write", "fs_")
_FS_CONTENT_VERBS = (
    "hablan de", "habla de", "mencionan", "menciona", "mencionar",
    "contienen", "contiene", "que traten de", "que tratan de",
    "referencias a", "referencia a",
)


def _is_fs_tool_intent(query: str) -> bool:
    """True si el usuario nombra herramientas fs_* o pide archivos cuyo
    CONTENIDO mencione un tema (grep local, no web)."""
    if not query:
        return False
    q = query.lower()
    if any(tok in q for tok in _FS_TOOL_TOKENS):
        return True
    # "...qué archivos hablan/mencionan/contienen X..." (+ archivo/archivos)
    if any(v in q for v in _FS_CONTENT_VERBS) and ("archivo" in q or "files" in q):
        return True
    return False


def _extract_content_keyword(query: str) -> Optional[str]:
    """Extrae el tema entre comillas o tras 'hablan de|mencionan|...'.

    Devuelve None si no hay tema identificable (ahí file_search usa su
    flujo normal por nombre)."""
    if not query:
        return None
    m = re.search(r"['\"]([^'\"]{2,60})['\"]", query)
    if m:
        return m.group(1).strip()
    m = re.search(
        r"(?:hablan de|habla de|mencionan|menciona|contienen|contiene|"
        r"traten de|tratan de)\s+(?:el\s+|la\s+|los\s+|las\s+|un\s+|una\s+)?"
        r"([a-z0-9áéíóúñü][a-z0-9áéíóúñü_.\-]{1,39})",
        query.lower(),
    )
    if m:
        kw = m.group(1).strip(" .,:;¿?")
        # descartar si el resto es pura ruta de carpeta o palabra vacía
        if kw and not kw.startswith(("tools/", "src/", "config/")):
            return kw
    return None


# ── DispatcherHandlersMixin ──────────────────────────────────────────────────

class DispatcherHandlersMixin:
    """
    Mixin con todos los handlers de agentes y helpers de respuesta.
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
        internal_retries = getattr(settings, 'max_retries', 2)
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
            ) if not asyncio.iscoroutinefunction(self.data_handler.handle) else \
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

    # ── Handler: listado de carpeta vía fs_list (grounded, sin LLM) ──
    async def _handle_file_list_folder(
        self, query: str, session_id: Optional[str], task_type: str,
        start: float,
    ) -> Optional[Response]:
        """
        v0.6.8pp (C): "usá fs_list para listar la carpeta tools/netron" →
        lista las entradas REALES de la carpeta (fs_list). Sin LLM.

        Devuelve None si no hay carpeta identificable (flujo por nombre).
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not registry.has("fs_list"):
                return None
            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                return None
            folder = candidates[0]
            lst = await asyncio.to_thread(
                registry.get("fs_list").run, path=str(folder),
            )
            if not lst.success:
                return None
            entries = (lst.data or {}).get("entries", []) or []
            if not entries:
                return Response(
                    content=f"📂 {folder}: la carpeta está vacía.",
                    session_id=session_id, model_used="fs_list",
                    task_type=task_type,
                    latency_ms=(time.time() - start) * 1000,
                    confidence=0.9, validation_passed=True, error=None,
                    sources=[str(folder)],
                )
            shown = entries[:40]
            extra = f"\n… y {len(entries) - len(shown)} entradas más." \
                if len(entries) > len(shown) else ""
            lines = [f"📂 {folder} ({len(entries)} entradas):"]
            for e in shown:
                kind = e.get("kind", "")
                icon = "📁" if kind == "dir" else "📄"
                pth = str(e.get("path", ""))
                rel = str(Path(pth).relative_to(repo_root)) \
                    if str(pth).startswith(str(repo_root)) else pth
                lines.append(f"  {icon} {rel}")
            lines.append(extra or "")
            content = "\n".join(lines)
            _log_event("DISPATCHER", "FileListFolder completed", extra={
                "folder": str(folder), "entries": len(entries),
            })
            return Response(
                content=content, session_id=session_id, model_used="fs_list",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.95, validation_passed=True, error=None,
                sources=[str(folder)],
            )
        except Exception as e:
            _log_error(e, "_handle_file_list_folder", extra_info={"query": query[:60]})
            return None

    # ── Handler: búsqueda de CONTENIDO local (grep grounded, sin LLM) ──
    async def _handle_file_content_search(
        self, query: str, session_id: Optional[str], task_type: str,
        start: float,
    ) -> Optional[Response]:
        """
        v0.6.8pp (C): responde "qué archivos de X hablan/mencionan Y" o
        "usá fs_search para buscar archivos que mencionen Y" listando los
        archivos cuyo CONTENIDO contiene el tema. Cero generación LLM.

        Devuelve None si no hay tema ni carpeta identificables (el llamador
        continúa con el flujo de búsqueda por nombre).
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        keyword = _extract_content_keyword(query)
        if not keyword:
            return None
        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not (registry.has("fs_list") and registry.has("fs_read")):
                return None

            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                # Sin carpeta acotada: al menos no divagar → búsqueda por nombre
                return None

            folders = [c for c in candidates if c.is_dir()][:3]
            needle = keyword.lower()
            hits: list = []          # (relpath, snippet)
            scanned = 0
            for folder in folders:
                lst = await asyncio.to_thread(
                    registry.get("fs_list").run, path=str(folder),
                )
                entries = (lst.data or {}).get("entries", []) if lst.success else []
                _READABLE = {".py", ".md", ".txt", ".json", ".yaml", ".yml",
                             ".toml", ".cfg", ".ini", ".sh", ".csv", ".log"}
                files = []
                for e in entries:
                    pth = str(e.get("path", ""))
                    if e.get("kind") == "dir" or not pth:
                        continue
                    if "__pycache__" in pth or "/." in pth:
                        continue
                    if Path(pth).suffix.lower() in _READABLE:
                        files.append(pth)
                files.sort(key=lambda p2: len(p2))
                for pth in files[:60]:
                    scanned += 1
                    read = await asyncio.to_thread(
                        registry.get("fs_read").run, path=pth, max_chars=6_000,
                    )
                    if not (read.success and read.output):
                        continue
                    low = read.output.lower()
                    if needle not in low:
                        continue
                    rel = str(Path(pth).relative_to(repo_root)) \
                        if str(pth).startswith(str(repo_root)) else pth
                    snippet = ""
                    for line in read.output.splitlines():
                        if needle in line.lower():
                            snippet = line.strip()[:160]
                            break
                    hits.append((rel, snippet))
                    if len(hits) >= 12:
                        break
                if len(hits) >= 12:
                    break

            if not hits:
                return Response(
                    content=f"🔎 En {folders[0]} no encontré archivos cuyo "
                            f"contenido mencione «{keyword}» (revisé {scanned} "
                            f"archivos legibles).",
                    session_id=session_id, model_used="fs_read",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.9, validation_passed=True, error=None,
                    sources=[str(f) for f in folders],
                )

            lines = [f"🔎 Archivos que mencionan «{keyword}» "
                     f"({len(hits)}):\n"]
            for rel, snippet in hits:
                if snippet:
                    lines.append(f"• {rel}\n    …{snippet}")
                else:
                    lines.append(f"• {rel}")
            content = "\n".join(lines)
            _log_event("DISPATCHER", "FileContentSearch completed", extra={
                "keyword": keyword[:40], "hits": len(hits),
            })
            return Response(
                content=content, session_id=session_id, model_used="fs_read",
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.95, validation_passed=True, error=None,
                sources=[h[0] for h in hits][:12],
            )
        except Exception as e:
            _log_error(e, "_handle_file_content_search", extra_info={"query": query[:60]})
            return None

    # ── Handler: Folder analysis (grounding real: lista + lee la carpeta) ──
    async def _handle_folder_analysis(
        self, message: Message, task_type: str,
        session_id: Optional[str], context: Optional[str],
    ) -> Response:
        """
        Analiza UNA CARPETA leyendo sus archivos de verdad (fs_list + fs_read)
        y resumiendo con el modelo de código. La respuesta queda anclada al
        contenido (sources) — el modelo no puede inventar módulos que no leyó.

        Límites de seguridad: cap de archivos (12) y de contexto (~18k chars).
        """
        from pathlib import Path

        start = time.time()
        query = message.content or ""
        repo_root = Path(__file__).resolve().parents[2]
        _log_event("DISPATCHER", "FolderAnalysis started", extra={
            "query_preview": query[:60],
        })

        try:
            from src.tools_mcp.tool_registry import get_registry
            registry = get_registry()
            if not (registry.has("fs_list") and registry.has("fs_read")):
                return Response(
                    content="El análisis de carpetas no está disponible (faltan fs_list/fs_read).",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )

            candidates = _extract_folder_candidates(query, repo_root)
            if not candidates:
                return Response(
                    content="No encontré esa carpeta. Probá con el nombre exacto, "
                            "ej: 'analiza la carpeta tools/netron' o 'qué hace src/core'.",
                    session_id=session_id, model_used=settings.llm_default_model,
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.5, validation_passed=True, error=None,
                )

            folder = candidates[0]
            lst = await asyncio.to_thread(
                registry.get("fs_list").run, path=str(folder),
            )
            entries = (lst.data or {}).get("entries", []) if lst.success else []

            _READABLE = {".py", ".md", ".txt", ".json", ".yaml", ".yml",
                         ".toml", ".cfg", ".ini", ".sh"}
            files = []
            for e in entries:
                pth = str(e.get("path", ""))
                if e.get("kind") == "dir" or not pth:
                    continue
                if "__pycache__" in pth or "/." in pth or pth.endswith("__init__.pyc"):
                    continue
                if Path(pth).suffix.lower() in _READABLE:
                    files.append(pth)
            # Preferir .py/.md y nombres cortos; cap de archivos
            files.sort(key=lambda p2: (Path(p2).suffix not in (".py", ".md"), len(p2)))
            files = files[:12]

            if not files:
                return Response(
                    content=f"📂 {folder}: no encontré archivos legibles "
                            f"(.py/.md/.json/…) para analizar.",
                    session_id=session_id, model_used="fs_list",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.8, validation_passed=True, error=None,
                    sources=[str(folder)],
                )

            blob_parts = []
            total = 0
            budget = 18_000
            read_sources = []
            truncated = False
            for pth in files:
                if total >= budget:
                    truncated = True
                    break
                read = await asyncio.to_thread(
                    registry.get("fs_read").run, path=pth, max_chars=4_000,
                )
                if not (read.success and read.output):
                    continue
                rel = str(Path(pth).relative_to(repo_root)) if str(pth).startswith(str(repo_root)) else pth
                chunk = read.output[:budget - total]
                blob_parts.append(f"===== {rel} =====\n{chunk}")
                total += len(chunk)
                read_sources.append(str(pth))

            if not blob_parts:
                return Response(
                    content=f"📂 {folder}: no pude leer el contenido de los archivos.",
                    session_id=session_id, model_used="fs_read",
                    task_type=task_type, latency_ms=(time.time() - start) * 1000,
                    confidence=0.6, validation_passed=True, error=None,
                    sources=[str(folder)],
                )

            note = ("\n(se truncó la lectura por límite de contexto)" if truncated else "")
            prompt = (
                "Analizá la carpeta de código que sigue. Respondé EN ESPAÑOL, "
                "estructurado:\n"
                "1) qué es/para qué sirve la carpeta en conjunto;\n"
                "2) qué hace cada archivo (una línea por archivo, basándote "
                "SOLO en su contenido);\n"
                "3) cómo se relacionan entre sí.\n"
                "REGLA: basate ÚNICAMENTE en el contenido provisto. Si un "
                "archivo no aporta, decilo. NO inventes módulos, funciones ni "
                "archivos que no estén en el contenido.\n\n"
                f"CARPETA: {folder}\n"
                f"ARCHIVOS LEÍDOS ({len(read_sources)}):\n"
                + "\n".join(f"- {p3}" for p3 in read_sources)
                + f"\n\nCONTENIDO:\n" + "\n".join(blob_parts) + note
            )

            gen = await self._llm_generate_with_retry(
                prompt=prompt,
                model=settings.llm_code_model,
                max_tokens=900,
            )
            text = getattr(gen, "content", "") or ""
            if not text.strip():
                text = "El modelo no devolvió un análisis (vacío)."

            content = f"📂 Análisis de {folder}:\n\n{text.strip()}"
            if truncated:
                content += f"\n\n_⚠️ Se analizaron {len(read_sources)} archivos (límite de contexto)._"
            return Response(
                content=content,
                session_id=session_id,
                model_used=settings.llm_code_model,
                task_type=task_type,
                latency_ms=(time.time() - start) * 1000,
                confidence=0.9,
                validation_passed=True,
                error=None,
                sources=read_sources[:10],
            )
        except Exception as e:
            _log_error(e, "_handle_folder_analysis", extra_info={"task_type": task_type})
            return Response(
                content=f"Error analizando la carpeta: {e}",
                session_id=session_id, model_used=settings.llm_default_model,
                task_type=task_type, latency_ms=(time.time() - start) * 1000,
                confidence=0.0, validation_passed=False, error=str(e),
            )

    # ── Handler: Analysis ────────────────────────────────────────────────────










