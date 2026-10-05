#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.3 — CoderAgent Core (helpers, constantes, chunked)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/coder_core.py
# Autor: SaintWick
# Fecha: 2026-05-06
# Propósito: Módulo auxiliar de CoderAgent.
#            Contiene: constantes, regex, dataclasses, helpers de
#            extracción, chunked generation, quality analysis y blacklist.
#            Importado exclusivamente por coder.py — no requiere
#            cambios en ningún otro módulo del sistema.
# ✅ v0.4.3 — BUGFIXES:
#    - is_code_blacklisted(): Desempaquetado seguro de tupla (value, expires_at)
#      retornada por CacheL2.get() para evitar AttributeError en coder.py.
#    - update_open_structures(): Corregida lógica destructiva que reemplazaba
#      la última estructura en vez de apilar cuando compartían el mismo tipo.
# ═══════════════════════════════════════════════════════════════
import ast
import logging
import re
import time
import hashlib
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# IMPORTS OPCIONALES CON FALLBACK SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

try:
    from src.router.cache_l2 import get_cache_l2
    _CODER_CACHE_L2_AVAILABLE = True
except ImportError:
    try:
        from src.router.cache_l2 import CacheL2
        _coder_cache_l2_instance = None

        def get_cache_l2():
            global _coder_cache_l2_instance
            if _coder_cache_l2_instance is None:
                _coder_cache_l2_instance = CacheL2()
            return _coder_cache_l2_instance
        _CODER_CACHE_L2_AVAILABLE = True
    except ImportError:
        _CODER_CACHE_L2_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════
# CONSTANTES Y UMBRALES
# ═══════════════════════════════════════════════════════════════
QUALITY_THRESHOLDS = {
    "level_1": {"min_quality": 0.70, "min_syntax": 0.90, "max_lines": 30,  "action": "accept"},
    "level_2": {"min_quality": 0.60, "min_syntax": 0.80, "max_lines": 100, "action": "accept"},
    "level_3": {"min_quality": 0.50, "min_syntax": 0.70, "max_lines": 200, "action": "review"},
    "level_4": {"min_quality": 0.40, "min_syntax": 0.50, "max_lines": 500, "action": "fallback"},
}

QUALITY_WEIGHTS = {
    # ✅ v0.6.9a (calidad de código): recalibrados — type_hints medido por AST,
    # tests con más peso (son la señal de comportamiento más fuerte). Suma 1.0.
    "syntax_valid": 0.40, "docstrings": 0.20, "type_hints": 0.05,
    "tests": 0.15, "logical_structure": 0.10, "error_handling": 0.10,
}

BLACKLIST_TTL = 600  # 10 minutos

_EXTENSIONS = {
    "python": ".py", "javascript": ".js", "java": ".java",
    "cpp": ".cpp", "c": ".c", "go": ".go", "rust": ".rs",
    "typescript": ".ts", "html": ".html", "css": ".css",
}

_CHUNKED_KEYWORDS = [
    "archivo completo", "módulo completo", "clase completa", "sistema completo",
    "todo el código", "código completo", "implementación completa",
    "todas las funciones", "full implementation", "complete file",
    "genera todo", "escribe todo", "1000 líneas", "500 líneas", "300 líneas",
]

_COMPLEX_KEYWORDS = [
    "programa completo", "aplicación", "sistema", "similar a", "pero mejor",
    "con todas las funciones", "full", "completo", "framework", "librería", "paquete",
]

_LARGE_TOOLS = [
    "nmap", "wireshark", "metasploit", "burp suite",
    "ansible", "terraform", "kubernetes",
    "django", "flask", "react", "vue", "tensorflow", "pytorch",
]

# ✅ v0.4.2: Pre-compilación de patrones para evitar re.compile en cada llamada
_SINGLE_FUNCTION_PATTERNS_STR = [
    r"escribe\s+una\s+función", r"crea\s+una\s+función",
    r"genera\s+una\s+función", r"implementa\s+una\s+función",
    r"def\s+\w+\s*\(", r"función\s+python", r"función\s+con\s+esta\s+firma",
]
_SINGLE_FUNCTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _SINGLE_FUNCTION_PATTERNS_STR]

# ═══════════════════════════════════════════════════════════════
# REGEX
# ═══════════════════════════════════════════════════════════════
# ✅ v0.4.2: Patrón unificado. \s* ya absorbe \n, haciendo innecesario el doble patrón.
CODE_PATTERN     = re.compile(r'```(\w+)?\s*(.*?)```', re.DOTALL | re.IGNORECASE)
FILE_PATTERN     = re.compile(r'^ARCHIVO:\s*(\S+\.\w+)', re.MULTILINE | re.IGNORECASE)

# ═══════════════════════════════════════════════════════════════
# DATACLASS: GenerationCursor
# ═══════════════════════════════════════════════════════════════
@dataclass
class GenerationCursor:
    """
    Trackea el estado de una generación chunked en progreso.

    Attributes:
        accumulated_code: Código acumulado de todos los chunks anteriores.
        current_line: Número de línea actual (para prompt de continuación).
        open_structures: Stack de clases/funciones abiertas en nivel 0.
        chunk_count: Cantidad de chunks generados hasta el momento.
        is_complete: True cuando se detectó EOF o marcador de fin.
    """
    accumulated_code: str = ""
    current_line: int = 0
    open_structures: List[str] = dc_field(default_factory=list)
    chunk_count: int = 0
    is_complete: bool = False


# ═══════════════════════════════════════════════════════════════
# LOGGING HELPERS
# ═══════════════════════════════════════════════════════════════
def _safe_log(fn):
    """Ejecuta fn(log) solo si el logger está disponible e inicializado."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if (hasattr(log, 'is_initialized') and log.is_initialized) or \
           (hasattr(log, '_initialized') and log._initialized):
            fn(log)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 coder_core.py:145] excepción degradada (intencional): {_d2e}")


def log_agent(event: str, task_type: str = "", duration: float = None,
              success: bool = True, error: str = None):
    """Registra evento de agente en DetailedLogger."""
    _safe_log(lambda log: log.log_agent_execution(
        agent_name="coder", task_type=task_type, state=event,
        duration=duration, success=success, error=error,
    ))


def log_error(error: Exception, context: str, extra_info: dict = None):
    """Registra error con traceback completo."""
    if not LOGGER_AVAILABLE:
        logger.error(f"coder - {context}: {error}")
        return
    _safe_log(lambda log: log.log_error(
        error=error, context=context, extra_info=extra_info,
        function_name=f"coder.{context}",
    ))


def log_event(category: str, message: str, level: str = "INFO", extra: dict = None):
    """Registra evento genérico en DetailedLogger."""
    _safe_log(lambda log: log.log_event(category, message, level, extra))


# ═══════════════════════════════════════════════════════════════
# HELPERS: EXTRACCIÓN DE CÓDIGO
# ═══════════════════════════════════════════════════════════════
def extract_code(text: str) -> List[Dict[str, str]]:
    """Extrae bloques de código de texto con markdown usando múltiples patrones."""
    if not text or not text.strip():
        return []

    # ✅ v0.4.2: Un único patrón compilado unificado
    matches = CODE_PATTERN.findall(text)
    if matches:
        blocks = [
            {"language": (lang or "text").lower(), "code": code.strip()}
            for lang, code in matches
            if code.strip() and len(code.strip()) > 5
        ]
        if blocks:
            logger.debug(f"✅ Pattern matched: {len(blocks)} bloques extraídos")
            return blocks

    logger.warning("⚠️ No se encontraron bloques markdown")
    return []


# ✅ Patrones de negativa (español + inglés). Anclados en frases, no en palabras
# sueltas: "no puedo" a secas aparece en prosa normal ("no puedo usar X sin Y").
_REFUSAL_PATTERNS = [
    re.compile(p) for p in (
        r"no puedo ayudarte con eso",
        r"no puedo ayudar(?:te)? con eso",
        r"no puedo ayudarte con (?:esta|esa|tal|lo que)",
        r"no puedo (?:proporcionar|generar|escribir|crear|darte|ofrecerte|asistirte|realizar|ayudar)",
        r"no me es posible (?:ayudarte|proporcionar|generar|asistirte)",
        r"no voy a (?:ayudarte|proporcionar|generar|escribir)",
        r"no (?:está|esta) permitido (?:ayudarte|proporcionar|generar)",
        r"(?:lo siento|lamento|disculpa)[^.]{0,40}pero no puedo",
        r"como (?:una )?(?:ia|asistente|modelo de lenguaje)[^.]{0,40}no puedo",
        r"i can(?:not|'t|t) (?:help|assist|provide|generate|write|create)",
        r"i(?:'m| am) (?:sorry|unable)[^.]{0,40}(?:can(?:not|'t|t)|unable)",
        r"i (?:won(?:not|'t)|will not) (?:help|assist|provide|generate|write)",
        r"as an ai[^.]{0,40}(?:can(?:not|'t|t)|unable)",
    )
]


def detect_refusal(content: str) -> Optional[str]:
    """Detecta una NEGATIVA del modelo (censura) y devuelve la frase encontrada.

    ✅ MEDIDO (2026-10-03, `ISSUE-142`): con `qwen2.5-coder:7b`, **3 de 4** pedidos
    legítimos del laboratorio del usuario (leer un pcap propio, un keylogger en su
    VM de pruebas, un exploit de su CTF local) devuelven una negativa en prosa en
    **2-5 s**, con estas frases reales:
      · "Lo siento, pero no puedo ayudarte con eso."
      · "Lo siento por la confusión, pero no puedo ayudarte con eso."
    y el modelo `huihui_ai/qwen2.5-coder-abliterate:7b` **sí** genera el código en
    esos casos (medido), por eso interesa distinguir "se negó" de "no generó código".

    Sin esto, una negativa caía en el camino de "código con SyntaxError": el agente
    gastaba un **retry completo (70-90 s)** pidiéndole al mismo modelo que corrija un
    error de sintaxis que en realidad es una negativa ⇒ se negaba otra vez.

    Criterio (evita falsos positivos): si la respuesta **trae código**, NO se
    considera negativa aunque mencione esas frases (p. ej. "no puedo usar X sin Y,
    acá está el código"). Sólo se evalúa cuando no hay bloques de código.

    Returns:
        La frase que delata la negativa, o None si no es una negativa.
    """
    if not content or not content.strip():
        return None
    try:
        if extract_code(content):        # produjo código ⇒ no es una negativa
            return None
    except Exception as e:
        logger.debug(f"[detect_refusal] extract_code falló: {e}")
    texto = " ".join(content.lower().split())
    for patron in _REFUSAL_PATTERNS:
        m = patron.search(texto)
        if m:
            return m.group(0).strip()
    return None


def extract_filename(text: str) -> Optional[str]:
    """Extrae nombre de archivo del patrón ARCHIVO:."""
    match = FILE_PATTERN.search(text)
    return match.group(1) if match else None


def suggest_filename(query: str, code_blocks: List[Dict[str, str]]) -> str:
    """Sugiere nombre de archivo basado en query y código extraído."""
    if not code_blocks:
        return "code.txt"
    ext = _EXTENSIONS.get(code_blocks[0].get("language", "").lower(), ".txt")
    q = query.lower()
    for kw, name in [("factorial", "factorial"), ("fibonacci", "fibonacci"),
                     ("sort", "sort_algorithm"), ("api", "api_client")]:
        if kw in q:
            return f"{name}{ext}"
    if "csv" in q or "read" in q:
        return f"read_csv{ext}"
    return f"code_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"


def extract_code_blocks(content: str) -> str:
    """Extrae código de bloques markdown antes de validar sintaxis."""
    if not content or not content.strip():
        return ""

    blocks = extract_code(content)
    if blocks:
        extracted = "\n".join(b["code"] for b in blocks)
        logger.debug(f"✅ extract_code_blocks: {len(blocks)} bloques, {len(extracted)} chars")
        return extracted

    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.split("\n")
        if len(lines) > 2:
            return "\n".join(lines[1:-1]).strip()

    logger.warning("⚠️ extract_code_blocks: retornando contenido crudo")
    return stripped


# ═══════════════════════════════════════════════════════════════
# HELPERS: DETECCIÓN DE COMPLEJIDAD
# ═══════════════════════════════════════════════════════════════
def is_complex_request(query: str) -> Tuple[bool, str]:
    """Determina si el request es complejo y requiere clarificación."""
    q = query.lower()

    # ✅ v0.4.2: Búsqueda directa sobre patrones pre-compilados (O(1) compile penalty)
    if any(p.search(q) for p in _SINGLE_FUNCTION_PATTERNS):
        return False, ""

    has_complex = any(kw in q for kw in _COMPLEX_KEYWORDS)
    has_tool    = any(t  in q for t  in _LARGE_TOOLS)

    if has_complex and has_tool:
        return True, (
            "Este es un proyecto complejo que requiere múltiples archivos y módulos.\n"
            "**¿Qué te gustaría que haga primero?**\n"
            "1. **Esqueleto básico** - Estructura con funciones principales\n"
            "2. **Módulo específico** - Dime qué funcionalidad concreta querés\n"
            "3. **Prototipo simple** - Versión reducida con 1-2 funciones\n"
            "4. **Guía de implementación** - Explicación paso a paso\n"
            "Especificá qué opción preferís o qué funcionalidad necesitás primero."
        )

    if len(query.split()) > 50 or any(w in q for w in ["varios", "múltiples", "diferentes"]):
        return True, (
            "Este request tiene varias partes. Para darte código de mejor calidad:\n"
            "**¿Podrías especificar cuál es la funcionalidad más importante primero?**\n"
            "Puedo generar una función/módulo a la vez (más detallado) o todo junto (más rápido).\n"
            "¿Qué preferís?"
        )

    return False, ""


# ═══════════════════════════════════════════════════════════════
# HELPERS: QUALITY ANALYSIS
# ═══════════════════════════════════════════════════════════════
def analyze_code_quality(content: str, settings) -> Dict[str, Any]:
    """Analiza calidad de código generado con pesos basados en testing real."""
    clean_code = extract_code_blocks(content)
    analysis = {
        "has_syntax": False, "has_docstrings": False, "has_tests": False,
        "has_type_hints": False, "tests_count": 0,
        "has_error_handling": False, "logical_structure_score": 0.0,
        "quality_score": 0.0, "line_count": len(clean_code.split("\n")) if clean_code else 0,
        "syntax_error": None, "extracted_code_length": len(clean_code),
    }

    if clean_code:
        # ✅ FIX (encontrado en producción, test_code_contract_e2e.py):
        # antes se compilaba clean_code (TODOS los bloques concatenados con
        # un \n) como una sola unidad. Con el contrato de Fase 2, una
        # respuesta de CODE_GENERATION típica trae DOS bloques ```python
        # separados (CÓDIGO + TESTS) — dos módulos independientes que
        # individualmente compilan bien, pero concatenados pueden dar un
        # SyntaxError espurio en el borde entre uno y otro. Eso disparaba
        # el retry de H2 con código que en realidad no tenía ningún
        # problema real — dos ciclos completos de generación (70-90s cada
        # uno en CPU) antes de que el InterpreterLoop llegara al
        # global_timeout. Mismo criterio que CodeValidator._validate_static()
        # (Fase 3, code_validator.py) ya usa: por bloque, no concatenado.
        blocks_for_syntax = [
            b for b in extract_code(content) if b.get("language") in ("python", "py")
        ]
        if blocks_for_syntax:
            syntax_errors = []
            for block in blocks_for_syntax:
                try:
                    compile(block["code"], "<generated>", "exec")
                except SyntaxError as e:
                    syntax_errors.append(str(e))
            analysis["has_syntax"] = not syntax_errors
            if syntax_errors:
                analysis["syntax_error"] = "; ".join(syntax_errors)
                logger.warning(f"❌ SyntaxError: {analysis['syntax_error']}")
        else:
            # Sin bloques ```python/```py detectables (ninguno, o todos sin
            # ese tag — ej. el modelo no etiquetó el fence) — cae al
            # comportamiento viejo, compilar clean_code tal cual.
            try:
                compile(clean_code, "<generated>", "exec")
                analysis["has_syntax"] = True
            except SyntaxError as e:
                analysis["syntax_error"] = str(e)
                logger.warning(f"❌ SyntaxError: {e}")
    else:
        logger.warning("⚠️ Código vacío después de extracción")

    if '"""' in clean_code or "'''" in clean_code:
        analysis["has_docstrings"] = True
    if "test" in clean_code.lower() or "assert" in clean_code:
        analysis["has_tests"] = True
    if "try" in clean_code and "except" in clean_code:
        analysis["has_error_handling"] = True

    # ✅ v0.6.9a (A): type hints y asserts reales medidos por AST — la regla
    # constitucional exige type hints pero nada los verificaba, y has_tests
    # solo miraba la presencia de la palabra "test/assert".
    _has_annotated_def = False
    _assert_count = 0
    for block in blocks_for_syntax:
        try:
            tree = ast.parse(block["code"])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotated = node.returns is not None or any(
                    a.annotation is not None for a in (
                        list(node.args.args) + list(node.args.kwonlyargs)
                        + ([node.args.vararg] if node.args.vararg else [])
                        + ([node.args.kwarg] if node.args.kwarg else [])
                        + list(getattr(node.args, "posonlyargs", []))
                    )
                )
                if annotated:
                    _has_annotated_def = True
            elif isinstance(node, ast.AnnAssign):
                _has_annotated_def = True
        _assert_count += sum(
            1 for ln in block["code"].splitlines() if re.search(r"\bassert\b", ln)
        )
    analysis["tests_count"] = _assert_count
    # type hints: alguna función/método anotado o asignación anotada.
    analysis["has_type_hints"] = _has_annotated_def

    score = 0.0
    if analysis["has_syntax"]:         score += QUALITY_WEIGHTS["syntax_valid"]
    if analysis["has_docstrings"]:     score += QUALITY_WEIGHTS["docstrings"]
    if analysis["has_type_hints"]:     score += QUALITY_WEIGHTS["type_hints"]
    if analysis["has_error_handling"]: score += QUALITY_WEIGHTS["error_handling"]
    if analysis["has_tests"]:          score += QUALITY_WEIGHTS["tests"]
    # asserts reales: +mitad del peso tests si hay ≥2 (tests sustanciales)
    if _assert_count >= 2:
        score += QUALITY_WEIGHTS["tests"] * 0.5

    lc = analysis["line_count"]
    if 10 <= lc <= 100:  score += QUALITY_WEIGHTS["logical_structure"]
    elif lc > 100:       score += QUALITY_WEIGHTS["logical_structure"] * 0.5

    analysis["logical_structure_score"] = min(score, 1.0)
    analysis["quality_score"] = analysis["logical_structure_score"]
    analysis["code_level"] = settings.get_code_level(lc) if hasattr(settings, 'get_code_level') else 2

    level_key = f"level_{analysis['code_level']}"
    if level_key in QUALITY_THRESHOLDS:
        threshold = QUALITY_THRESHOLDS[level_key]
        analysis["recommended_action"] = (
            threshold["action"] if analysis["quality_score"] >= threshold["min_quality"] else "retry"
        )
    else:
        analysis["recommended_action"] = "review"

    return analysis


def get_quality_threshold_for_level(level: int, settings) -> float:
    """Obtiene umbral de calidad para un nivel específico."""
    if hasattr(settings, 'get_code_quality_threshold'):
        return settings.get_code_quality_threshold(level)
    return {1: 0.70, 2: 0.60, 3: 0.50, 4: 0.40}.get(level, 0.60)


def should_retry_for_quality(quality_analysis: Dict[str, Any], settings) -> bool:
    """Determina si debe reintentar generación por baja calidad."""
    if quality_analysis["quality_score"] < 0.50:       return True
    if not quality_analysis["has_syntax"]:              return True
    if quality_analysis["extracted_code_length"] < 10: return True
    level = quality_analysis.get("code_level", 2)
    return quality_analysis["quality_score"] < get_quality_threshold_for_level(level, settings)


def build_quality_critique(quality_analysis: Dict[str, Any]) -> str:
    """Construye crítica de calidad para reintentar generación."""
    issues = []
    if not quality_analysis["has_syntax"]:
        issues.append(f"ERROR DE SINTAXIS: {quality_analysis.get('syntax_error', 'Unknown')}")
    if not quality_analysis["has_docstrings"]:
        issues.append("Faltan docstrings en las funciones")
    if not quality_analysis["has_error_handling"]:
        issues.append("Falta manejo de errores (try/except)")
    if quality_analysis["quality_score"] < 0.50:
        issues.append(f"Calidad muy baja: {quality_analysis['quality_score']:.2f}")
    if quality_analysis["extracted_code_length"] < 10:
        issues.append("Código extraído muy corto o vacío")

    return (
        "MEJORAS REQUERIDAS:\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + "\nGenera código CORREGIDO que cumpla con todos los requisitos."
    )


# ═══════════════════════════════════════════════════════════════
# HELPERS: BLACKLIST L2
# ═══════════════════════════════════════════════════════════════
def get_code_hash(code: str) -> str:
    """Genera hash normalizado del código para blacklist."""
    if not code:
        return ""
    normalized = "\n".join(line.strip() for line in code.strip().splitlines())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def is_code_blacklisted(blacklist, code: str) -> Optional[Dict[str, Any]]:
    """Verifica si este código ya fue rechazado anteriormente."""
    if not blacklist or not code:
        return None
    try:
        code_hash = get_code_hash(code)
        result = blacklist.get(f"blacklist_{code_hash}")

        if result:
            # ✅ v0.4.3: CacheL2.get() retorna una tupla (value, expires_at),
            # por lo que extraemos el primer elemento. Si otro backend retorna
            # el valor directo (ej. dict), lo manejamos de forma segura.
            entry = result[0] if isinstance(result, tuple) else result

            if entry:
                logger.debug(f"Código blacklisted (hash={code_hash[:8]}...)")
                return entry

        return None
    except Exception as e:
        logger.debug(f"Error verificando blacklist: {e}")
        return None


def blacklist_code(blacklist, code: str, critique: str, score: float) -> None:
    """Agrega código a la blacklist con motivo del rechazo."""
    if not blacklist or not code:
        return
    try:
        code_hash = get_code_hash(code)
        blacklist.set(
            f"blacklist_{code_hash}",
            {
                "code_preview": code[:200],
                "critique": critique[:500],
                "score": score,
                "blacklisted_at": time.time(),
                "hash": code_hash,
            },
            ttl=BLACKLIST_TTL,
        )
        logger.info(f"Código blacklisteado (score={score:.2f}, hash={code_hash[:8]}..., ttl={BLACKLIST_TTL}s)")
        log_event("CODE_BLACKLIST", "Código agregado a blacklist", extra={
            "score": round(score, 4),
            "critique_preview": critique[:100],
            "code_hash": code_hash[:8],
            "ttl_seconds": BLACKLIST_TTL,
        })
    except Exception as e:
        logger.debug(f"Error blacklisteando código: {e}")


def init_blacklist():
    """Inicializa instancia de blacklist L2. Retorna None si no disponible."""
    if not _CODER_CACHE_L2_AVAILABLE:
        return None
    try:
        instance = get_cache_l2()
        logger.debug("CoderAgent: Blacklist L2 activada")
        return instance
    except Exception as e:
        logger.warning(f"CoderAgent: Blacklist L2 no disponible: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# HELPERS: CHUNKED GENERATION
# ═══════════════════════════════════════════════════════════════
def estimate_needs_chunking(query: str, settings) -> bool:
    """Determina si el request probablemente requiere generación chunked."""
    if not getattr(settings, 'chunked_generation_enabled', True):
        return False
    q = query.lower()
    if any(kw in q for kw in _CHUNKED_KEYWORDS):
        return True
    estimated_level = min(4, max(1, len(query.split()) // 15))
    return estimated_level >= 3


def extract_first_code_block(text: str) -> str:
    """Extrae el primer bloque de código Python de una respuesta."""
    blocks = extract_code(text)
    if blocks:
        return blocks[0]["code"]
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return stripped


def remove_overlap(accumulated: str, new_chunk: str, overlap_lines: int) -> str:
    """Elimina líneas repetidas al inicio de new_chunk que ya están en accumulated."""
    if not accumulated or not new_chunk:
        return new_chunk

    acc_lines = accumulated.splitlines()
    new_lines = new_chunk.splitlines()
    tail = [l.strip() for l in acc_lines[-overlap_lines:] if l.strip()]
    if not tail:
        return new_chunk

    skip_until = 0
    for i, line in enumerate(new_lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in tail:
            skip_until = i + 1
        else:
            break

    return "\n".join(new_lines[skip_until:])


def detect_completion(chunk: str) -> bool:
    """Detecta si el chunk representa el fin del archivo."""
    if not chunk:
        return False

    eof_markers = ["# EOF", "# END_OF_FILE", "# CHUNK_BREAK", "# FIN", "# END"]
    if any(marker in chunk for marker in eof_markers):
        return True

    lines = [l for l in chunk.splitlines() if l.strip()]
    if not lines:
        return False

    last = lines[-1]
    is_unindented   = not last.startswith((" ", "\t"))
    is_not_opening  = not any(last.strip().startswith(kw)
                              for kw in ["def ", "class ", "if ", "for ",
                                         "while ", "try:", "with "])
    is_not_decorator = not last.strip().startswith("@")
    return is_unindented and is_not_opening and is_not_decorator and len(lines) > 3


def update_open_structures(cursor: GenerationCursor, chunk: str) -> None:
    """Actualiza el stack de estructuras abiertas (clases/funciones nivel 0)."""
    for line in chunk.splitlines():
        m = re.match(r'^(class|def)\s+(\w+)', line)
        if m:
            kind, name = m.group(1), m.group(2)
            entry = f"{kind} {name}"
            # ✅ v0.4.3: Se corrige lógica destructiva. Se debe apilar la estructura
            # en vez de reemplazar la última si es del mismo tipo (ej. dos 'class' seguidas).
            cursor.open_structures.append(entry)


def build_continuation_prompt(cursor: GenerationCursor, original_prompt: str, settings) -> str:
    """Construye el prompt de continuación para el siguiente chunk."""
    overlap      = getattr(settings, 'chunk_overlap_lines', 15)
    acc_lines    = cursor.accumulated_code.splitlines()
    context_lines = acc_lines[-overlap:] if len(acc_lines) > overlap else acc_lines
    context_snippet = "\n".join(context_lines)

    structures_info = (
        f"Estructuras abiertas: {', '.join(cursor.open_structures)}"
        if cursor.open_structures else "Nivel raíz del módulo"
    )

    return (
        f"CONTINUACIÓN DE GENERACIÓN — Chunk {cursor.chunk_count + 1}\n"
        f"─────────────────────────────────────────\n"
        f"Tarea original: {original_prompt[:200]}\n"
        f"─────────────────────────────────────────\n"
        f"ÚLTIMAS LÍNEAS YA ESCRITAS (NO repetir):\n"
        f"```python\n{context_snippet}\n```\n"
        f"─────────────────────────────────────────\n"
        f"INSTRUCCIONES ESTRICTAS:\n"
        f"- CONTINÚA exactamente desde la línea {cursor.current_line}\n"
        f"- {structures_info}\n"
        f"- NO repitas código ya escrito\n"
        f"- NO agregues explicaciones ni comentarios de cierre\n"
        f"- Solo código Python válido\n"
        f"- Mantén indentación consistente\n"
        f"- Si el archivo está completo, escribe # EOF al final\n"
        f"Continúa:\n"
    )
