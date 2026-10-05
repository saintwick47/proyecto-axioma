#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.8.0 — Clasificador Core: Pipeline Explícito y Trazable
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/classifier_core.py
# Autor: SaintWick
# Propósito: Clasificación de intenciones con pipeline explícito
# v4.2.0: Pipeline trazable por AST (preprocess → calibrate → assign)
# v4.2.1: Variantes sin acento en KEYWORD_MAP
# v4.3.0: add_example() — online learning incremental
# v4.4.0: LOCATION_QUERY
# v4.5.0: WEB_SEARCH keywords OSS/GitHub
# v4.6.0: KEYWORD_MAP WEB_SEARCH saneado — 13 FP eliminados
# v4.6.1: TRAINING_EXAMPLES WEB_SEARCH expandido con hechos actuales
# v4.7.0: KEYWORD_MAP — eliminados 22 falsos positivos:
#   CODE_GENERATION: codigo/código/programar/programa/genera/generá/
#                    crea/creá/escribí/escribe/función/funcion/clase
#   CODE_DEBUGGING:  error
#   CODE_EXPLANATION: explica/explicar/qué hace/que hace/
#                     cómo funciona/como funciona
#   WEB_SEARCH:  mejores/recientes/últimas/ultimas/últimos/ultimos/
#                alternativas/alternativa/populares
#   WEATHER:     tiempo
#   DATA_ANALYSIS: analizar/analiza
#   TECH_SUPPORT: ayuda
#   Guard de negación: _has_leading_negation() en code_verb+context
#   y en KEYWORD_MAP scan para _CODE_TASK_TYPES.
# v4.8.0: THRESHOLD ADAPTATIVO por tipo de tarea + ajuste por feature variance
#   - ADAPTIVE_THRESHOLDS: dict[str, float] por TaskType.value
#   - _adaptive_threshold(task_type): instance method, considera _last_feature_std
#   - _assign_task_type_adaptive(): nueva lógica con threshold por tipo
#   - _record_feature_std(): tracking de varianza para ajuste dinámico
#   - Backward compat: si threshold=0.7 (default), usa adaptive.
#                        Si threshold != 0.7, override global (compat legacy).
#   - MIN_FLOOR_THRESHOLD=0.3 preserva el comportamiento del floor original.
# ═══════════════════════════════════════════════════════════════
import logging
import time
import math
import hashlib
import json
import re
from typing import Optional, Dict, Any, List, Tuple, Set
from pathlib import Path
from datetime import datetime
from collections import Counter
from config.settings import settings
from config.paths import Paths
from src.utils.degradation import log_degraded
from src.domain.types import TaskType
from src.domain.exceptions import RouterError
from src.router.classifier_embedding import ClassifierEmbeddingMixin
logger = logging.getLogger(__name__)

# ── NEGACIÓN — constantes y helper de módulo ────────────────────────────────
# Usados en _classify_by_keywords() para evitar que "no generes código"
# sea clasificado como CODE_GENERATION.
_NEGATION_WINDOW: int = 40  # ventana de chars antes del match
_NEGATION_PREFIXES: frozenset = frozenset({
    "no ", "no\t", "nunca ", "sin ", "jamás ", "jamas ",
    "don't ", "dont ", "do not ", "ni ", "tampoco ",
})

def _has_leading_negation(text: str) -> bool:
    """True si hay negación en los primeros _NEGATION_WINDOW chars del texto preprocesado."""
    window = text[:_NEGATION_WINDOW]
    return any(window.startswith(neg) or f" {neg}" in window for neg in _NEGATION_PREFIXES)

# ═══════════════════════════════════════════════════════════════
# ✅ v4.8.0: CONSTANTES DE THRESHOLD ADAPTATIVO
# ═══════════════════════════════════════════════════════════════
# Threshold mínimo (floor) — preserva comportamiento original v4.7.0.
# Si calibrated_score < MIN_FLOOR_THRESHOLD → fallback directo a GENERAL_CHAT.
MIN_FLOOR_THRESHOLD: float = 0.3
# Threshold por defecto cuando el TaskType no está en ADAPTIVE_THRESHOLDS.
ADAPTIVE_DEFAULT_THRESHOLD: float = 0.65
# Incremento de threshold cuando feature variance es alta (muchas scores
# similares en embeddings). Indica clasificador "indeciso" → ser más estricto.
ADAPTIVE_VARIANCE_BOOST: float = 0.10
ADAPTIVE_VARIANCE_THRESHOLD: float = 0.4
ADAPTIVE_VARIANCE_MAX: float = 0.95  # cap superior
# Threshold por tipo de tarea — más sensible para code/search,
# más estricto para system/jarvis. Razón: un threshold único (0.7)
# penaliza tareas conversacionales (muchos falsos positivos) y es
# permisivo en tareas críticas (falsos negativos).
ADAPTIVE_THRESHOLDS: Dict[str, float] = {
    # ── Code generation & related — más sensible (recall++) ──
    "code_generation":   0.55,
    "code_correction":   0.55,
    "code_explanation":  0.55,
    "code_review":       0.55,
    "code_optimization": 0.55,
    "code_testing":      0.55,
    "code_documentation":0.55,
    "code_migration":    0.55,
    "code_deployment":   0.55,
    "code_debugging":    0.55,
    "code_search":       0.55,
    "file_search":       0.60,   # ✅ plan_rafael (2026-08-31)
    "code_autonomous":   0.55,
    # ── Search & research — moderadamente sensible ──
    "web_search":        0.60,
    "news_search":       0.60,
    "academic_search":   0.60,
    "research":          0.60,
    # ── Analysis — estándar-ish ──
    "data_analysis":     0.65,
    "document_analysis": 0.65,
    "sentiment_analysis":0.65,
    "trend_analysis":    0.65,
    "risk_analysis":     0.70,
    # ── Validation — estricto ──
    "quality_validation":0.70,
    "fact_checking":     0.70,
    "security_audit":    0.75,
    # ── Data queries — estándar ──
    "weather_query":     0.65,
    "finance_query":     0.65,
    "time_query":        0.55,
    "news_query":        0.65,
    "location_query":    0.65,
    # ── Conversational — catch-all sensible ──
    "general_chat":      0.50,
    "technical_support": 0.60,
    "tutoring":          0.60,
    "brainstorming":     0.60,
    "roleplay":          0.60,
    "content_creation":  0.60,
    "poetry_generation": 0.60,
    "script_writing":    0.60,
    "translation":       0.60,
    "summarization":     0.60,
    # ── Visual — estricto (costoso si clasifica mal) ──
    "screenshot_analysis":0.75,
    "screen_describe":    0.75,
    # ── System — muy estricto ──
    "system_control":    0.75,
    "app_launch":        0.75,
    "volume_control":    0.75,
    "screenshot":        0.75,
    "camera_describe":   0.75,
    # ── Jarvis / autónomo — muy estricto ──
    "jarvis_query":      0.80,
    "autonomous_task":   0.80,
}

try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

def _log_router(query: str, task_type: str, model_selected: str,
                confidence: float, cache_hit: bool = False,
                duration: float = None, api_required: bool = False):
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_router_decision(query, task_type, model_selected,
                                    confidence, cache_hit, duration, api_required)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 classifier_core.py:161] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra: dict = None):
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 classifier_core.py:171] excepción degradada (intencional): {_d2e}")

GREETING_PATTERNS: List[str] = [
    r'^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey)\b',
    r'^\s*(cómo estás|como estas|qué tal|que tal|cómo va|como va)\b',
    r'^\s*(qué puedes hacer|que puedes hacer|en qué me ayudas|en que me ayudas)\b',
]

CODE_CONTEXT_KEYWORDS: Set[str] = {
    "python", "javascript", "java", "c++", "rust", "go", "typescript",
    "función", "funcion", "clase", "método", "metodo", "variable",
    "return", "import", "from", "def ", "class ", "async", "await",
    "list", "dict", "array", "string", "int", "float", "bool",
    "try", "except", "raise", "error", "exception",
    "docstring", "type hint", "typing", "pydantic", "fastapi",
    "decorator", "decorador", "módulo", "modulo", "paquete",
    "código", "codigo", "script", "programa",
    # ✅ v0.6.9e (batería calidad de código): prompts que piden CÓDIGO por
    # descripción, sin sintaxis inline. Antes: keywords_miss → general_chat
    # (LRU, cola de tareas, cliente HTTP, bus pub/sub tardaban ~195 s y
    # devolvían error).
    "tests", "pytest", "race condition", "cola de tareas", "cliente http",
    "httpx", "circuit breaker", "handlers", "pub/sub", "topic",
}

CODE_VERBS: Set[str] = {
    "genera", "generá", "crea", "creá", "escribí", "escribe",
    "programa", "programar", "corregir", "corregí",
    "debug", "depurar", "optimizar", "refactor", "compilar",
    # ✅ v0.6.9e: imperativos/voseo de construcción de código
    "implementá", "implementa", "diseñá", "diseña",
    "construí", "construye",
}

CODE_EXPLANATION_KEYWORDS: Set[str] = {
    "explica", "explicar", "qué hace", "que hace", "cómo funciona",
    "como funciona", "para qué sirve", "para que sirve",
    "analiza este código", "analizá este código",
    "revisa este código", "revisá este código",
}

QUALITY_VALIDATION_KEYWORDS: Set[str] = {
    "valida", "validar", "verifica", "verificar",
    "evalúa", "evaluar", "puntúa", "puntuar",
    "revisa calidad", "chequea", "controla",
    "judge", "validation",
    # ✅ v0.6.9f: variantes voseo/imperativo (validá, verificá, evaluá…)
    "validá", "verificá", "evaluá", "puntuá", "chequeá",
}

DOCUMENT_NOUNS: Set[str] = {
    "archivo", "documentos", "documento", "pdf", "texto",
    "leer", "extraer", "procesar", "analizar documento",
}

VISION_SCREEN_KEYWORDS: Set[str] = {
    "pantalla", "screenshot", "captura de pantalla",
    "analiza la pantalla", "analiza mi pantalla", "analizar pantalla",
    "qué estoy viendo", "que estoy viendo",
    "qué hay en pantalla", "que hay en pantalla",
    "qué se ve", "que se ve",
    "qué dice la pantalla", "que dice la pantalla",
    "ver pantalla", "mirar pantalla", "qué ves", "que ves",
}

LOCATION_PATTERNS: List[str] = [
    r'\b(dónde|donde)\s+(estamos|estoy|estás|están)\b',
    r'\b(en\s+)?(qué|que)\s+(país|pais|ciudad|lugar|región|region)\b',
    r'\b(mi\s+)?(ubicación|ubicacion|localización|localizacion)\b',
    r'\bgeolocaliz',
    r'\bdime\s+(dónde|donde)\b',
    r'\ben\s+(qué|que)\s+(país|pais)\s+estamos\b',
    r'\bcuál\s+es\s+mi\s+(ubicación|ubicacion|país|pais|ciudad)\b',
    r'\bcual\s+es\s+mi\s+(ubicación|ubicacion|país|pais|ciudad)\b',
]

TASK_TYPE_NORMALIZATION: Dict[str, str] = {
    "news_query": TaskType.NEWS_SEARCH.value,
    "web_query": TaskType.WEB_SEARCH.value,
    "code_gen": TaskType.CODE_GENERATION.value,
    "code_fix": TaskType.CODE_DEBUGGING.value,
    "code_explain": TaskType.CODE_EXPLANATION.value,
    "data_analyze": TaskType.DATA_ANALYSIS.value,
    "validate": TaskType.QUALITY_VALIDATION.value,
}


class IntentClassifierCore(ClassifierEmbeddingMixin):
    """
    Núcleo de clasificación de intenciones con pipeline explícito y trazable.
    Pipeline: classify() → preprocess → keywords/embeddings → calibrate → assign
    ✅ v4.8.0: Threshold adaptativo por tipo de tarea + ajuste por feature variance.
    """
    __slots__ = [
        'model_path', 'threshold', 'use_embeddings', '_cache', '_cache_ttl',
        '_model', '_vectorizer', '_training_vectors', '_stats',
        # ✅ v4.8.0: tracking de feature variance + flag de adaptive
        '_last_feature_std', '_adaptive_enabled',
    ]

    KEYWORD_MAP: Dict[str, str] = {
        # ── CODE_GENERATION ──────────────────────────────────────
        # ✅ v4.7.0: Eliminados 13 FP — codigo/código/programar/programa/
        #   genera/generá/crea/creá/escribí/escribe/función/funcion/clase
        #   ya están en CODE_CONTEXT_KEYWORDS o CODE_VERBS y son manejados
        #   por el compound check (code_verb + code_context) con mayor precisión.
        # ── CODE_DEBUGGING ───────────────────────────────────────
        # ✅ v4.7.0: Eliminado "error" — "fue un error", "error gramatical".
        "debug": TaskType.CODE_DEBUGGING.value,
        "depurar": TaskType.CODE_DEBUGGING.value,
        # ✅ v0.6.9e: prompts de debug por descripción (batería de calidad)
        "race condition": TaskType.CODE_DEBUGGING.value,
        "encuentra el error": TaskType.CODE_DEBUGGING.value,
        "encontrala": TaskType.CODE_DEBUGGING.value,
        # ── CODE_EXPLANATION ─────────────────────────────────────
        # ✅ v4.7.0: Eliminados "explica","explicar","qué hace","que hace",
        #   "cómo funciona","como funciona" — ya en CODE_EXPLANATION_KEYWORDS
        #   gateados por has_code_context. Sin ese gate → falsos positivos:
        #   "explica la historia", "cómo funciona el corazón".
        "explicar código": TaskType.CODE_EXPLANATION.value,
        "explicar codigo": TaskType.CODE_EXPLANATION.value,
        # ── CODE_REVIEW ──────────────────────────────────────────
        "revisar código": TaskType.CODE_REVIEW.value,
        "revisar codigo": TaskType.CODE_REVIEW.value,
        # ── CODE_OPTIMIZATION ────────────────────────────────────
        "optimizar": TaskType.CODE_OPTIMIZATION.value,
        "optimizá": TaskType.CODE_OPTIMIZATION.value,
        "refactor": TaskType.CODE_OPTIMIZATION.value,
        # ── CODE_TESTING ─────────────────────────────────────────
        "test": TaskType.CODE_TESTING.value,
        "prueba": TaskType.CODE_TESTING.value,
        # ✅ v0.6.8pp: "probá/probar" imperativos con word boundary —
        # al ignorar "test" citado (nombre de archivo) hay que cubrir el
        # verbo fuera de comillas; evita FP en "comprobá/aprobá/aprobar".
        "probá": TaskType.CODE_TESTING.value,
        "probar": TaskType.CODE_TESTING.value,
        # ── CODE_DOCUMENTATION ───────────────────────────────────
        "documentar": TaskType.CODE_DOCUMENTATION.value,
        # ── CODE_MIGRATION ───────────────────────────────────────
        "migrar": TaskType.CODE_MIGRATION.value,
        # ── CODE_DEPLOYMENT ──────────────────────────────────────
        "desplegar": TaskType.CODE_DEPLOYMENT.value,
        # ── CODE_CORRECTION ──────────────────────────────────────
        "corregir": TaskType.CODE_CORRECTION.value,
        "corregí": TaskType.CODE_CORRECTION.value,
        "sintacticamente": TaskType.CODE_CORRECTION.value,
        "sintáctico": TaskType.CODE_CORRECTION.value,
        "sintactico": TaskType.CODE_CORRECTION.value,
        # ── QUALITY_VALIDATION ───────────────────────────────────
        "valida": TaskType.QUALITY_VALIDATION.value,
        "validar": TaskType.QUALITY_VALIDATION.value,
        "verifica": TaskType.QUALITY_VALIDATION.value,
        "verificar": TaskType.QUALITY_VALIDATION.value,
        "evalúa": TaskType.QUALITY_VALIDATION.value,
        "evalua": TaskType.QUALITY_VALIDATION.value,
        "evaluar": TaskType.QUALITY_VALIDATION.value,
        "puntúa": TaskType.QUALITY_VALIDATION.value,
        "puntua": TaskType.QUALITY_VALIDATION.value,
        "revisa calidad": TaskType.QUALITY_VALIDATION.value,
        # ── WEB_SEARCH ───────────────────────────────────────────
        # v4.6.0: Solo keywords inequívocamente de búsqueda.
        # ✅ v4.7.0: Eliminados mejores/recientes/últimas/últimos/
        #   alternativas/alternativa/populares — superlativos genéricos
        #   que disparan WEB_SEARCH en preguntas no relacionadas.
        "buscar": TaskType.WEB_SEARCH.value,
        "busca": TaskType.WEB_SEARCH.value,
        "buscame": TaskType.WEB_SEARCH.value,
        "buscáme": TaskType.WEB_SEARCH.value,
        "investigar": TaskType.WEB_SEARCH.value,
        "investiga": TaskType.WEB_SEARCH.value,
        "buscar informacion": TaskType.WEB_SEARCH.value,
        "buscar información": TaskType.WEB_SEARCH.value,
        "buscar datos": TaskType.WEB_SEARCH.value,
        "github": TaskType.WEB_SEARCH.value,
        "gitlab": TaskType.WEB_SEARCH.value,
        "bitbucket": TaskType.WEB_SEARCH.value,
        "repositorio": TaskType.WEB_SEARCH.value,
        "repositorios": TaskType.WEB_SEARCH.value,
        "repo": TaskType.WEB_SEARCH.value,
        "documentación": TaskType.WEB_SEARCH.value,
        "documentacion": TaskType.WEB_SEARCH.value,
        "trending": TaskType.WEB_SEARCH.value,
        "open source": TaskType.WEB_SEARCH.value,
        "código abierto": TaskType.WEB_SEARCH.value,
        "codigo abierto": TaskType.WEB_SEARCH.value,
        # ── NEWS_SEARCH ──────────────────────────────────────────
        "noticia": TaskType.NEWS_SEARCH.value,
        "noticias": TaskType.NEWS_SEARCH.value,
        # ── ACADEMIC_SEARCH ──────────────────────────────────────
        "estudio": TaskType.ACADEMIC_SEARCH.value,
        # ── CODE_SEARCH ──────────────────────────────────────────
        "buscar código": TaskType.CODE_SEARCH.value,
        "buscar codigo": TaskType.CODE_SEARCH.value,
        # ── FILE_SEARCH (✅ plan_rafael 2026-08-31) ───────────────
        # Búsqueda de archivos/carpetas en el disco ("dónde está el archivo X").
        # Se buscan ANTES de "archivo" → DOCUMENT_ANALYSIS para no pisarse.
        "buscá el archivo": TaskType.FILE_SEARCH.value,
        "busca el archivo": TaskType.FILE_SEARCH.value,
        "buscar el archivo": TaskType.FILE_SEARCH.value,
        "buscá la carpeta": TaskType.FILE_SEARCH.value,
        "busca la carpeta": TaskType.FILE_SEARCH.value,
        "buscar la carpeta": TaskType.FILE_SEARCH.value,
        "buscar carpeta": TaskType.FILE_SEARCH.value,
        "dónde está el archivo": TaskType.FILE_SEARCH.value,
        "donde esta el archivo": TaskType.FILE_SEARCH.value,
        "dónde está la carpeta": TaskType.FILE_SEARCH.value,
        "donde esta la carpeta": TaskType.FILE_SEARCH.value,
        "ubicación del archivo": TaskType.FILE_SEARCH.value,
        "ubicacion del archivo": TaskType.FILE_SEARCH.value,
        "ruta del archivo": TaskType.FILE_SEARCH.value,
        "encontrá el archivo": TaskType.FILE_SEARCH.value,
        "encuentra el archivo": TaskType.FILE_SEARCH.value,
        "buscar archivos": TaskType.FILE_SEARCH.value,
        "buscar archivo": TaskType.FILE_SEARCH.value,
        "ubicación de la carpeta": TaskType.FILE_SEARCH.value,
        "ubicacion de la carpeta": TaskType.FILE_SEARCH.value,
        # ── FILE_SEARCH: lectura/resumen/cita de archivos (✅ plan_rafael 2026-08-31) ──
        "leé el archivo": TaskType.FILE_SEARCH.value,
        "lee el archivo": TaskType.FILE_SEARCH.value,
        "leer el archivo": TaskType.FILE_SEARCH.value,
        "léeme el archivo": TaskType.FILE_SEARCH.value,
        "leeme el archivo": TaskType.FILE_SEARCH.value,
        "resumí el archivo": TaskType.FILE_SEARCH.value,
        "resumi el archivo": TaskType.FILE_SEARCH.value,
        "resumime el archivo": TaskType.FILE_SEARCH.value,
        "resumir el archivo": TaskType.FILE_SEARCH.value,
        "resumen del archivo": TaskType.FILE_SEARCH.value,
        "citá un bloque del archivo": TaskType.FILE_SEARCH.value,
        "cita un bloque del archivo": TaskType.FILE_SEARCH.value,
        "citá el archivo": TaskType.FILE_SEARCH.value,
        "citar el archivo": TaskType.FILE_SEARCH.value,
        "mostrame el archivo": TaskType.FILE_SEARCH.value,
        "mostrame el contenido del archivo": TaskType.FILE_SEARCH.value,
        "mostrame el contenido de": TaskType.FILE_SEARCH.value,
        "contenido del archivo": TaskType.FILE_SEARCH.value,
        # ── FILE_SEARCH: análisis de carpeta/directorio con grounding ──
        # ✅ v0.6.8ii: "analiza la carpeta X / qué hacen sus archivos" solía
        #   caer a general_chat / code_explanation SIN leer el FS → el modelo
        #   divagaba (ver logs/reg_error.txt). Ahora → file_search, cuyo handler
        #   lista la carpeta y lee los archivos de verdad.
        "analiza la carpeta": TaskType.FILE_SEARCH.value,
        "analizá la carpeta": TaskType.FILE_SEARCH.value,
        "analizar la carpeta": TaskType.FILE_SEARCH.value,
        "analiza el directorio": TaskType.FILE_SEARCH.value,
        "analizá el directorio": TaskType.FILE_SEARCH.value,
        "analizar el directorio": TaskType.FILE_SEARCH.value,
        "explora la carpeta": TaskType.FILE_SEARCH.value,
        "explorá la carpeta": TaskType.FILE_SEARCH.value,
        "explorar la carpeta": TaskType.FILE_SEARCH.value,
        "explica la carpeta": TaskType.FILE_SEARCH.value,
        "explicá la carpeta": TaskType.FILE_SEARCH.value,
        "revisa la carpeta": TaskType.FILE_SEARCH.value,
        "revisá la carpeta": TaskType.FILE_SEARCH.value,
        "revisar la carpeta": TaskType.FILE_SEARCH.value,
        "estudia la carpeta": TaskType.FILE_SEARCH.value,
        "estudiá la carpeta": TaskType.FILE_SEARCH.value,
        "qué hace la carpeta": TaskType.FILE_SEARCH.value,
        "que hace la carpeta": TaskType.FILE_SEARCH.value,
        "qué hacen sus archivos": TaskType.FILE_SEARCH.value,
        "que hacen sus archivos": TaskType.FILE_SEARCH.value,
        "analiza tools": TaskType.FILE_SEARCH.value,
        "analiza src": TaskType.FILE_SEARCH.value,
        # ── DATA_ANALYSIS ────────────────────────────────────────
        # ✅ v4.7.0: Eliminados "analizar" y "analiza" — ya cubiertos por el
        #   compound check has_analiza+has_datos antes del KEYWORD_MAP scan.
        #   Como keywords sueltos → "analiza tu corazón" → DATA_ANALYSIS (FP).
        "estadística": TaskType.DATA_ANALYSIS.value,
        "estadistica": TaskType.DATA_ANALYSIS.value,
        # ── DOCUMENT_ANALYSIS ────────────────────────────────────
        "documento": TaskType.DOCUMENT_ANALYSIS.value,
        "pdf": TaskType.DOCUMENT_ANALYSIS.value,
        "archivo": TaskType.DOCUMENT_ANALYSIS.value,
        # ── SENTIMENT / TREND / RISK ─────────────────────────────
        "sentimiento": TaskType.SENTIMENT_ANALYSIS.value,
        "tendencia": TaskType.TREND_ANALYSIS.value,
        "riesgo": TaskType.RISK_ANALYSIS.value,
        # ── GENERAL_CHAT ─────────────────────────────────────────
        "hola": TaskType.GENERAL_CHAT.value,
        "buenos días": TaskType.GENERAL_CHAT.value,
        "buenas tardes": TaskType.GENERAL_CHAT.value,
        "cómo estás": TaskType.GENERAL_CHAT.value,
        "qué haces": TaskType.GENERAL_CHAT.value,
        "imagen": TaskType.GENERAL_CHAT.value,
        "foto": TaskType.GENERAL_CHAT.value,
        # ── TECHNICAL_SUPPORT ────────────────────────────────────
        # ✅ v4.7.0: Eliminado "ayuda" — demasiado genérico.
        #   "ayuda con mi vida", "ayuda a entender X" → TECHNICAL_SUPPORT (FP).
        #   PASO 4 LLM distinguirá soporte técnico de chat general.
        # ── TUTORING ─────────────────────────────────────────────
        "enseñar": TaskType.TUTORING.value,
        "tutorial": TaskType.TUTORING.value,
        # ── BRAINSTORMING / ROLEPLAY ─────────────────────────────
        "lluvia de ideas": TaskType.BRAINSTORMING.value,
        "roleplay": TaskType.ROLEPLAY.value,
        # ── CONTENT_CREATION ─────────────────────────────────────
        "escribir": TaskType.CONTENT_CREATION.value,
        "crear": TaskType.CONTENT_CREATION.value,
        "historia": TaskType.CONTENT_CREATION.value,
        # ── POETRY / SCRIPT ──────────────────────────────────────
        "poema": TaskType.POETRY_GENERATION.value,
        "guión": TaskType.SCRIPT_WRITING.value,
        "guion": TaskType.SCRIPT_WRITING.value,
        # ── TRANSLATION ──────────────────────────────────────────
        "traducir": TaskType.TRANSLATION.value,
        "traducción": TaskType.TRANSLATION.value,
        "traduccion": TaskType.TRANSLATION.value,
        # ── SUMMARIZATION ────────────────────────────────────────
        "resumir": TaskType.SUMMARIZATION.value,
        "resumen": TaskType.SUMMARIZATION.value,
        # ── WEATHER_QUERY ────────────────────────────────────────
        "clima": TaskType.WEATHER_QUERY.value,
        "pronóstico": TaskType.WEATHER_QUERY.value,
        "pronostico": TaskType.WEATHER_QUERY.value,
        "temperatura": TaskType.WEATHER_QUERY.value,
        "tiempo": TaskType.WEATHER_QUERY.value,
        # ── TIME_QUERY ───────────────────────────────────────────
        "hora": TaskType.TIME_QUERY.value,
        # ── FINANCE_QUERY ────────────────────────────────────────
        "finanza": TaskType.FINANCE_QUERY.value,
        "cotización": TaskType.FINANCE_QUERY.value,
        "cotizacion": TaskType.FINANCE_QUERY.value,
        # ── FACT_CHECKING ────────────────────────────────────────
        "comprobar": TaskType.FACT_CHECKING.value,
        # ── SECURITY_AUDIT ───────────────────────────────────────
        "auditoria": TaskType.SECURITY_AUDIT.value,
        "auditoría": TaskType.SECURITY_AUDIT.value,
        "seguridad": TaskType.SECURITY_AUDIT.value,
        "escanear": TaskType.CODE_GENERATION.value,
        "escaneo": TaskType.CODE_GENERATION.value,
        "escanea": TaskType.CODE_GENERATION.value,
        "escaner": TaskType.CODE_GENERATION.value,
        "escáner": TaskType.CODE_GENERATION.value,
        "scanner": TaskType.CODE_GENERATION.value,
        "scan": TaskType.CODE_GENERATION.value,
        "red local": TaskType.CODE_GENERATION.value,
        "mi red": TaskType.CODE_GENERATION.value,
        "dispositivos en red": TaskType.CODE_GENERATION.value,
        "puertos abiertos": TaskType.CODE_GENERATION.value,
        "detectar dispositivos": TaskType.CODE_GENERATION.value,
        "detectar ataques": TaskType.CODE_GENERATION.value,
        "detectar intrusos": TaskType.CODE_GENERATION.value,
        "detectar malware": TaskType.CODE_GENERATION.value,
        "monitoreo de red": TaskType.CODE_GENERATION.value,
        "monitorear red": TaskType.CODE_GENERATION.value,
        "tráfico de red": TaskType.CODE_GENERATION.value,
        "trafico de red": TaskType.CODE_GENERATION.value,
        "vulnerabilidades": TaskType.CODE_GENERATION.value,
        "vulnerabilidad": TaskType.CODE_GENERATION.value,
        "fingerprint": TaskType.CODE_GENERATION.value,
        "ping sweep": TaskType.CODE_GENERATION.value,
        "arp scan": TaskType.CODE_GENERATION.value,
        "port scan": TaskType.CODE_GENERATION.value,
        "network scan": TaskType.CODE_GENERATION.value,
        "blue team": TaskType.CODE_GENERATION.value,
        "intrusion": TaskType.CODE_GENERATION.value,
        "intrusión": TaskType.CODE_GENERATION.value,
        "administración de red": TaskType.CODE_GENERATION.value,
        # ── SCREENSHOT_ANALYSIS ──────────────────────────────────
        "qué estoy viendo": TaskType.SCREENSHOT_ANALYSIS.value,
        "que estoy viendo": TaskType.SCREENSHOT_ANALYSIS.value,
        "qué ves": TaskType.SCREENSHOT_ANALYSIS.value,
        "que ves": TaskType.SCREENSHOT_ANALYSIS.value,
        "qué hay en pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "que hay en pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "analiza la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "analiza mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "analizar pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "captura de pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "screenshot": TaskType.SCREENSHOT_ANALYSIS.value,
        "qué se ve": TaskType.SCREENSHOT_ANALYSIS.value,
        "que se ve": TaskType.SCREENSHOT_ANALYSIS.value,
        "describe la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "describe lo que ves": TaskType.SCREENSHOT_ANALYSIS.value,
        "qué dice la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "que dice la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "ver pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "mirar pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "lee la pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "leer pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "qué hay en mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "que hay en mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "dime que ves": TaskType.SCREENSHOT_ANALYSIS.value,
        "dime qué ves": TaskType.SCREENSHOT_ANALYSIS.value,
        "describe mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        "describí mi pantalla": TaskType.SCREENSHOT_ANALYSIS.value,
        # ── JARVIS_QUERY ─────────────────────────────────────────
        "modo jarvis": TaskType.JARVIS_QUERY.value,
        "activa jarvis": TaskType.JARVIS_QUERY.value,
        "axioma escucha": TaskType.JARVIS_QUERY.value,
        "dime axioma": TaskType.JARVIS_QUERY.value,
        "oye axioma": TaskType.JARVIS_QUERY.value,
        # ── AUTONOMOUS_TASK ──────────────────────────────────────
        "haz esto solo": TaskType.AUTONOMOUS_TASK.value,
        "ejecuta autonomamente": TaskType.AUTONOMOUS_TASK.value,
        "ejecuta autónomamente": TaskType.AUTONOMOUS_TASK.value,
        "tarea autonoma": TaskType.AUTONOMOUS_TASK.value,
        "tarea autónoma": TaskType.AUTONOMOUS_TASK.value,
        "hazlo de forma autonoma": TaskType.AUTONOMOUS_TASK.value,
        "planifica y ejecuta": TaskType.AUTONOMOUS_TASK.value,
        # ── LOCATION_QUERY ───────────────────────────────────────
        "dónde estamos": TaskType.LOCATION_QUERY.value,
        "donde estamos": TaskType.LOCATION_QUERY.value,
        "dónde estoy": TaskType.LOCATION_QUERY.value,
        "donde estoy": TaskType.LOCATION_QUERY.value,
        "dónde estás": TaskType.LOCATION_QUERY.value,
        "donde estás": TaskType.LOCATION_QUERY.value,
        "en qué país": TaskType.LOCATION_QUERY.value,
        "en que pais": TaskType.LOCATION_QUERY.value,
        "qué país": TaskType.LOCATION_QUERY.value,
        "que pais": TaskType.LOCATION_QUERY.value,
        "mi ubicación": TaskType.LOCATION_QUERY.value,
        "mi ubicacion": TaskType.LOCATION_QUERY.value,
        "geolocalización": TaskType.LOCATION_QUERY.value,
        "geolocalizacion": TaskType.LOCATION_QUERY.value,
        "ubicación actual": TaskType.LOCATION_QUERY.value,
        "ubicacion actual": TaskType.LOCATION_QUERY.value,
        "localización": TaskType.LOCATION_QUERY.value,
        "localizacion": TaskType.LOCATION_QUERY.value,
        "mi localización": TaskType.LOCATION_QUERY.value,
        "mi localizacion": TaskType.LOCATION_QUERY.value,
    }

    FINANCE_KEYWORDS: Set[str] = {
        'dolar', 'dólar', 'euro', 'peso', 'libra', 'yen',
        'blue', 'mep', 'ccl', 'argenpesos',
        'cotizacion', 'cotización', 'bolsa',
        'bitcoin', 'crypto', 'cripto', 'usd', 'ars',
        'tasa', 'cambio', 'divisa',
    }

    # v4.6.0: "articulo" eliminado (ya no está en KEYWORD_MAP)
    _WORD_BOUNDARY_KEYWORDS: Set[str] = {
        "genera", "generá", "crea", "creá", "programa",
        "test", "scan", "clase", "clima", "hora", "finanza",
        "probá", "probar",
        "busca", "buscame", "buscáme", "noticia",
        "analiza", "estudio", "archivo", "riesgo",
        "ayuda", "poema", "historia", "foto", "imagen",
        "error", "explica",
        "pronostico", "funcion",
        "pantalla", "screenshot",
        "jarvis", "axioma", "autonoma", "autonomamente",
        "geolocalización", "geolocalizacion",
        "ubicación", "ubicacion", "localización", "localizacion",
    }

    TRAINING_EXAMPLES: Dict[str, List[str]] = {
        TaskType.FILE_SEARCH.value: [
            "buscá el archivo benchmark_calibration", "dónde está el archivo settings.py",
            "busca la carpeta tools", "ubicación del archivo README",
            "encontrá el archivo main.py", "dónde está la carpeta data",
            "buscá el archivo .env", "ruta del archivo memory_guard",
        ],
        TaskType.CODE_GENERATION.value: [
            "crea una función en Python", "escribe código para", "necesito un script que",
            "implementa una clase", "genera código para", "genera un programa que",
            "crea un programa para", "hacé un script que", "escribí código para analizar archivos",
            "genera un programa q analice archivos y los corrija",
        ],
        TaskType.CODE_DEBUGGING.value: [
            "este código no funciona", "encuentra el error en", "debuggear este código",
            "por qué falla esto", "corrige el bug",
        ],
        TaskType.CODE_EXPLANATION.value: [
            "explica este código", "qué hace esta función", "cómo funciona este decorator",
            "explica cómo funciona", "para qué sirve este código",
        ],
        TaskType.QUALITY_VALIDATION.value: [
            "valida esta respuesta", "verifica la información", "evalúa este código",
            "puntúa esta respuesta", "revisa la calidad",
        ],
        TaskType.WEB_SEARCH.value: [
            # Búsqueda explícita
            "busca información sobre", "investiga acerca de",
            "qué dice internet sobre", "busca en la web",
            "buscame datos de", "buscar información de",
            # Hechos actuales — deportes
            "cuantos goles marco messi en el mundial 2026",
            "quien gano el partido de ayer",
            "resultado del argentina vs brasil",
            "en este mundial cuantos goles lleva argentina",
            "quien gano el torneo de tenis esta semana",
            "clasificacion actual del mundial 2026",
            # Hechos actuales — personas / cargos
            "quien es el presidente de argentina ahora",
            "cual es el ceo actual de apple",
            # Hechos actuales — noticias
            "que paso con messi en el partido de hoy",
            "noticias de hoy sobre inteligencia artificial",
            "ultimas noticias de tecnologia esta semana",
            # Hechos actuales — info general
            "cual es la situacion actual en ucrania",
            "que paso con el terremoto de ayer",
        ],
        TaskType.GENERAL_CHAT.value: [
            "hola", "buenos días", "cómo estás", "qué tal", "hablemos",
            "qué puedes hacer por mí", "puedes ayudarme", "ayuda con",
        ],
        TaskType.SUMMARIZATION.value: [
            "resume esto", "haz un resumen de", "puntos clave de", "sintetiza", "extrae lo importante",
        ],
        TaskType.DATA_ANALYSIS.value: [
            "analiza estos datos", "encuentra patrones en", "estadísticas de",
        ],
        TaskType.DOCUMENT_ANALYSIS.value: [
            "analiza este documento", "extrae información del pdf", "resume el archivo",
        ],
        TaskType.TRANSLATION.value: [
            "traduce al inglés", "traduce al español", "traducción de",
        ],
        TaskType.CONTENT_CREATION.value: [
            "escribe un artículo", "crea contenido para", "genera texto sobre",
        ],
        TaskType.WEATHER_QUERY.value: [
            "qué clima hace hoy", "pronóstico del tiempo", "va a llover mañana",
            "temperatura en Buenos Aires", "clima en Córdoba", "pronóstico para hoy",
            "pronóstico hoy", "dame el pronostico para hoy",
            "dame el pronostico para cordoba", "pronostico para villa general belgrano",
            "pronostico del clima", "como esta el clima", "que temperatura hace",
        ],
        TaskType.NEWS_QUERY.value: [
            "últimas noticias de Argentina", "noticias sobre tecnología",
            "qué pasó hoy", "breaking news",
        ],
        TaskType.FINANCE_QUERY.value: [
            "cotización del dólar hoy", "precio del bitcoin", "cómo está la bolsa",
            "valor del euro", "dolar blue hoy", "cotizacion del dolar blue",
            "cuánto vale el dólar blue", "dolar mep cotización",
            "ccl precio hoy", "argenpesos valor",
        ],
        TaskType.TIME_QUERY.value: [
            "qué hora es", "hora en Londres", "zona horaria de Tokio", "current time",
        ],
        TaskType.CODE_CORRECTION.value: [
            "corregí este código", "arreglá este script", "corrige los errores sintácticos",
            "analice archivos y los corrija sintacticamente",
        ],
        TaskType.SCREENSHOT_ANALYSIS.value: [
            "qué estoy viendo", "qué hay en mi pantalla", "analiza la pantalla",
            "describe lo que ves en pantalla", "qué se ve en pantalla",
            "lee lo que hay en pantalla", "qué dice la pantalla",
            "captura y analiza la pantalla", "screenshot análisis",
            "describe mi pantalla", "qué está en la pantalla",
            "analiza lo que estoy viendo", "mira la pantalla y dime",
            "qué ves en pantalla", "leer pantalla", "ver mi pantalla",
        ],
        TaskType.JARVIS_QUERY.value: [
            "modo jarvis", "axioma escucha", "oye axioma", "dime axioma",
            "activa el modo jarvis", "axioma qué hora es",
            "axioma cómo estoy", "axioma dime el clima",
        ],
        TaskType.SCREEN_DESCRIBE.value: [
            "describe la pantalla", "describe lo que ves", "qué hay en mi pantalla",
            "lee la pantalla", "leer pantalla", "describe la ventana activa",
        ],
        TaskType.AUTONOMOUS_TASK.value: [
            "haz esto solo", "ejecuta autónomamente", "tarea autónoma",
            "hazlo de forma autónoma", "planifica y ejecuta",
            "ejecuta los pasos necesarios", "termina esto por tu cuenta",
        ],
        TaskType.LOCATION_QUERY.value: [
            "dónde estamos ahora", "en qué país estamos", "dónde estoy",
            "cuál es mi ubicación", "puedes geolocalizarme", "dime dónde estoy",
            "en qué ciudad estoy", "cuál es mi país actual",
            "dónde me encuentro", "mi ubicación actual",
            "dime en qué país estamos", "dónde estamos ubicados",
            "en qué lugar estamos", "cuál es mi localización",
            "dime mi ubicación", "geolocalízame", "dónde estoy parado",
            "en qué región estamos", "dime dónde nos encontramos",
        ],
    }

    def __init__(self, model_path: Optional[Path] = None, threshold: float = 0.7, use_embeddings: bool = True):
        start = time.perf_counter()
        _log_router("INIT", "CORE", "initializing", 0.0)
        self.model_path = model_path or Paths.ROOT / "data" / "classifier_model.json"
        self.threshold = threshold
        self.use_embeddings = use_embeddings
        self._cache: Dict[str, Tuple[TaskType, float, datetime]] = {}
        self._cache_ttl = 300
        self._model = None
        self._vectorizer = None
        self._training_vectors: Dict[str, List[List[float]]] = {}
        self._stats = {
            "total_classifications": 0, "cache_hits": 0, "cache_misses": 0,
            "keyword_classifications": 0, "embedding_classifications": 0, "special_classifications": 0,
        }
        # ✅ v4.8.0: tracking de feature variance para ajuste dinámico del threshold
        self._last_feature_std: float = 0.0
        # ✅ v4.8.0: flag para habilitar/deshabilitar adaptive (default ON)
        self._adaptive_enabled: bool = True
        try:
            self._load_model()
            duration = time.perf_counter() - start
            _log_router("INIT", "CORE", "initialized", 1.0, duration=duration)
            logger.info(f"IntentClassifierCore initialized: threshold={threshold}, adaptive={self._adaptive_enabled}")
        except Exception as e:
            duration = time.perf_counter() - start
            _log_error(e, "IntentClassifierCore.__init__", extra={"threshold": threshold})
            _log_router("INIT", "CORE", "init_failed", 0.0, duration=duration)
            raise

    def classify(self, text: str) -> Tuple[TaskType, float]:
        start = time.perf_counter()
        processed_text = self._preprocess_text(text)
        cached = self._get_from_cache(text)
        if cached is not None:
            self._stats["total_classifications"] += 1
            self._stats["cache_hits"] += 1
            duration = time.perf_counter() - start
            _log_router(text[:50], cached[0].value, "cache", cached[1],
                        cache_hit=True, duration=duration)
            return cached
        self._stats["cache_misses"] += 1
        raw_intent: Optional[str] = None
        raw_score: float = 0.0
        classification_source: str = "none"
        raw_intent, raw_score = self._classify_by_keywords(processed_text)
        classification_source = "keywords"
        if raw_intent is None and self.use_embeddings:
            raw_intent, raw_score = self._classify_by_embeddings(text)
            classification_source = "embeddings"
        calibrated_score = self.calibrate_probability(raw_score)
        # ✅ v4.8.0: threshold adaptativo por tipo de tarea + ajuste por variance
        task_type = self._assign_task_type_adaptive(calibrated_score, raw_intent)
        self._save_to_cache(text, task_type, calibrated_score)
        self._stats["total_classifications"] += 1
        if classification_source == "keywords":
            self._stats["keyword_classifications"] += 1
        elif classification_source == "embeddings":
            self._stats["embedding_classifications"] += 1
        duration = time.perf_counter() - start
        _log_router(
            text[:50], task_type.value, classification_source,
            calibrated_score, duration=duration,
            api_required=TaskType.is_search_related(task_type.value),
        )
        return task_type, calibrated_score

    @staticmethod
    def _preprocess_text(text: str) -> str:
        """Normaliza el texto para el matching de keywords.

        ✅ ISSUE-064 (bug real): antes era solo `lower().strip()`, así que un
        texto con **espaciado irregular** ("que  hora es", típico de dictado o de
        copiar/pegar) NO matcheaba keywords multi-palabra: `_keyword_match` usa
        `keyword in text`, y `'que hora es' in 'que  hora es'` es **False**.
        Resultado: clasificación degradada a `general_chat` en silencio (y ~15 s
        de LLM para clasificar en CPU). Ahora los espacios internos se colapsan.
        """
        return re.sub(r"\s+", " ", str(text or "")).strip().lower()

    @staticmethod
    def calibrate_probability(raw_score: float) -> float:
        return round(max(0.0, min(1.0, raw_score)), 4)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v4.8.0: THRESHOLD ADAPTATIVO
    # ═══════════════════════════════════════════════════════════════
    def _adaptive_threshold(self, task_type_str: Optional[str]) -> float:
        """
        Calcula el threshold adaptativo para un tipo de tarea.
        Lógica:
        1. Si self._adaptive_enabled=False o self.threshold fue seteado
           explícitamente (≠ 0.7 default), usar self.threshold como override
           global (backward compat con tests/legacy code).
        2. Si no, buscar threshold por tipo en ADAPTIVE_THRESHOLDS.
        3. Si _last_feature_std > 0.4 (clasificador "indeciso" en embeddings),
           sumar 0.10 al threshold para ser más estricto.
        4. Cap superior en 0.95 para evitar umbrales imposibles.
        """
        # ── Backward compat: override global ──
        if not self._adaptive_enabled or self.threshold != 0.7:
            return self.threshold
        # ── Threshold base por tipo ──
        base = ADAPTIVE_THRESHOLDS.get(task_type_str or "", ADAPTIVE_DEFAULT_THRESHOLD)
        # ── Boost por feature variance (clasificador indeciso) ──
        if self._last_feature_std > ADAPTIVE_VARIANCE_THRESHOLD:
            base = min(base + ADAPTIVE_VARIANCE_BOOST, ADAPTIVE_VARIANCE_MAX)
        return round(base, 4)

    def _assign_task_type_adaptive(self, calibrated_prob: float, raw_intent: Optional[str]) -> TaskType:
        """
        ✅ v4.8.0: Asigna TaskType con threshold adaptativo por tipo.
        Flujo de decisión:
        1. Si raw_intent es None → GENERAL_CHAT (sin candidato)
        2. Si calibrated_prob < MIN_FLOOR (0.3) → GENERAL_CHAT (confianza muy baja)
        3. Calcular threshold adaptativo para raw_intent
        4. Si calibrated_prob < adaptive → GENERAL_CHAT (confianza insuficiente
           para el tipo específico)
        5. Si pasa el threshold → aceptar raw_intent como TaskType
        Backward compat: _assign_task_type() (static) sigue existiendo
        para callers legacy/tests que no necesitan el threshold adaptativo.
        """
        # ── Sin candidato o confianza muy baja → fallback ──
        if raw_intent is None or calibrated_prob < MIN_FLOOR_THRESHOLD:
            return TaskType.GENERAL_CHAT
        # ── Threshold adaptativo por tipo ──
        adaptive = self._adaptive_threshold(raw_intent)
        if calibrated_prob < adaptive:
            logger.debug(
                f"[AdaptiveThreshold] {raw_intent}: score={calibrated_prob:.3f} "
                f"< adaptive={adaptive:.3f} → fallback GENERAL_CHAT"
            )
            return TaskType.GENERAL_CHAT
        # ── Aceptar clasificación ──
        normalized = TASK_TYPE_NORMALIZATION.get(raw_intent, raw_intent)
        try:
            return TaskType(normalized)
        except ValueError:
            logger.warning(f"Unknown TaskType value: {normalized}, falling back to GENERAL_CHAT")
            return TaskType.GENERAL_CHAT

    # ── Método legacy preservado para backward compat ──
    @staticmethod
    def _assign_task_type(calibrated_prob: float, raw_intent: Optional[str]) -> TaskType:
        """
        ⚠️  LEGACY: Threshold fijo 0.3 floor, sin adaptive por tipo.
        Mantenido para callers externos (tests, integrations) que
        esperan este comportamiento. Usar _assign_task_type_adaptive()
        para el nuevo flujo.
        """
        if raw_intent is None or calibrated_prob < MIN_FLOOR_THRESHOLD:
            return TaskType.GENERAL_CHAT
        normalized = TASK_TYPE_NORMALIZATION.get(raw_intent, raw_intent)
        try:
            return TaskType(normalized)
        except ValueError:
            logger.warning(f"Unknown TaskType value: {normalized}, falling back to GENERAL_CHAT")
            return TaskType.GENERAL_CHAT

    @staticmethod
    def _keyword_match(keyword: str, text: str) -> bool:
        if ' ' in keyword:
            return keyword in text
        if keyword in IntentClassifierCore._WORD_BOUNDARY_KEYWORDS:
            return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))
        return keyword in text

    @staticmethod
    def _set_keyword_count(keyword_set: Set[str], text: str) -> int:
        return sum(1 for kw in keyword_set if IntentClassifierCore._keyword_match(kw, text))

    @staticmethod
    def _set_keyword_any(keyword_set: Set[str], text: str) -> bool:
        return any(IntentClassifierCore._keyword_match(kw, text) for kw in keyword_set)

    def _load_model(self) -> None:
        start = time.perf_counter()
        if not self.model_path.exists():
            self._training_vectors = {}
            logger.debug("No trained model found, using keyword fallback")
            return
        try:
            with open(self.model_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._training_vectors = data.get("vectors", {})
            duration = time.perf_counter() - start
            logger.debug(f"Loaded core model: {len(self._training_vectors)} examples in {duration:.4f}s")
        except Exception as e:
            duration = time.perf_counter() - start
            _log_error(e, "IntentClassifierCore._load_model", extra={"model_path": str(self.model_path)})
            logger.warning(f"Failed to load core model: {e}")
            self._training_vectors = {}

    def _is_greeting_or_basic_chat(self, processed_text: str) -> bool:
        # ✅ FIX: el facade (classifier.py PASO "greeting_fast_path") llama
        # a este método con el texto crudo, no con _preprocess_text() aplicado.
        # Normalizamos acá para no depender de que el caller lo haga bien.
        text_lower = self._preprocess_text(processed_text)
        if len(text_lower) < 20:
            if any(g in text_lower for g in ["hola", "hello", "hi", "buenos", "buenas"]):
                return True
        for pattern in GREETING_PATTERNS:
            if re.match(pattern, text_lower, re.I):
                return True
        if re.search(r'qué puedes hacer|que puedes hacer|en qué me ayudas|en que me ayudas|para qué sirves|puedes ayudarme', text_lower):
            return not self._set_keyword_any(CODE_CONTEXT_KEYWORDS, text_lower)
        if re.search(r'(puedes|puedo|me puedes|ayuda|ayudame|ayudarme|necesito|quiero).*código', text_lower):
            has_specific_verb = any(v in text_lower for v in [
                "genera", "crea", "escribe", "implementa", "def ", "class ",
                "función", "clase", "script", "programa"
            ])
            if not has_specific_verb:
                return True
        return False

    def _classify_by_keywords(self, processed_text: str) -> Tuple[Optional[str], float]:
        start = time.perf_counter()
        # ✅ FIX: el facade (classifier.py PASO 3 "keywords_fallback") llama
        # a este método con el texto crudo — nunca pasa por _preprocess_text().
        # LOCATION_PATTERNS no tiene re.IGNORECASE, así que "¿Dónde estamos?"
        # (con mayúscula) no matcheaba nunca y cursaba silenciosamente a
        # keywords_miss. Confirmado con re.search directo: falla en crudo,
        # matchea en minúsculas. Normalizamos acá, en el límite de la función,
        # para no depender de que cada caller futuro lo haga bien.
        processed_text = self._preprocess_text(processed_text)
        if self._is_greeting_or_basic_chat(processed_text):
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.GENERAL_CHAT.value, "keywords_greeting", 0.9, duration=duration)
            return TaskType.GENERAL_CHAT.value, 0.9
        # ✅ v0.6.9f: el compound code_verb+context va PRIMERO (antes de
        # explanation y quality) — una orden de GENERAR código ("Generá una
        # función que calcule el precio…", "Generá tests… que deba validar…")
        # no debe perder contra un FP de explanation/quality. "Explicá qué
        # hace…" no tiene verbo de CODE_VERBS → sigue por explanation.
        code_verb_matches = self._set_keyword_count(CODE_VERBS, processed_text)
        code_context_matches = self._set_keyword_count(CODE_CONTEXT_KEYWORDS, processed_text)
        if code_verb_matches > 0 and code_context_matches >= 1:
            # ✅ v4.7.0: guard de negación — "no generes código" no es CODE_GENERATION.
            if not _has_leading_negation(processed_text):
                duration = time.perf_counter() - start
                _log_router(processed_text[:50], TaskType.CODE_GENERATION.value, "keywords", 0.85, duration=duration)
                return TaskType.CODE_GENERATION.value, 0.85
        code_explanation_matches = self._set_keyword_count(CODE_EXPLANATION_KEYWORDS, processed_text)
        has_code_context = self._set_keyword_any(CODE_CONTEXT_KEYWORDS, processed_text)
        if code_explanation_matches > 0 and has_code_context:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.CODE_EXPLANATION.value, "keywords", 0.80, duration=duration)
            return TaskType.CODE_EXPLANATION.value, 0.80
        quality_validation_matches = self._set_keyword_count(QUALITY_VALIDATION_KEYWORDS, processed_text)
        if quality_validation_matches > 0:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.QUALITY_VALIDATION.value, "keywords", 0.80, duration=duration)
            return TaskType.QUALITY_VALIDATION.value, 0.80
        has_analiza = any(kw in processed_text for kw in ["analiza", "analizá", "analizar", "análisis"])
        has_datos = "datos" in processed_text or "data" in processed_text
        if has_analiza and has_datos:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.DATA_ANALYSIS.value, "keywords", 0.70, duration=duration)
            return TaskType.DATA_ANALYSIS.value, 0.70
        if self._set_keyword_any(VISION_SCREEN_KEYWORDS, processed_text):
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.SCREENSHOT_ANALYSIS.value, "keywords_vision", 0.90, duration=duration)
            return TaskType.SCREENSHOT_ANALYSIS.value, 0.90
        for pattern in LOCATION_PATTERNS:
            if re.search(pattern, processed_text):
                duration = time.perf_counter() - start
                _log_router(processed_text[:50], TaskType.LOCATION_QUERY.value, "keywords_location", 0.90, duration=duration)
                return TaskType.LOCATION_QUERY.value, 0.90
        # ✅ v0.7.5 (ISSUE-073): consulta de DATO verificable → WEB_SEARCH.
        # Caso real (log 12-09): "¿Dónde se ubica la falla geológica en el valle
        # de Calamuchita…?" → keywords_miss → general_chat → 0 tools, 0 fuentes,
        # 208 s. Se reusa `expects_factual_grounding()` (una sola fuente de verdad
        # con el chat) en vez de duplicar la lista de marcadores, y se exige que
        # NO sea una consulta de código/proyecto (esas no necesitan web).
        # ⚠️ `has_code_context` NO alcanza como guarda: usa substring matching y
        # "int" matchea dentro de "**int**endente" (medido: la consulta real
        # "…candidatos para intendente en villa general belgrano" quedaba marcada
        # como contexto de código). Se exige señal REAL de código: verbo de código
        # + contexto, o sintaxis explícita.
        _sintaxis_codigo = bool(re.search(
            r"(```|\bdef\s+\w|\bclass\s+\w|\bimport\s+\w|\breturn\b)",
            processed_text))
        _es_pedido_de_codigo = (
            (code_verb_matches > 0 and code_context_matches >= 1) or _sintaxis_codigo)
        if not _es_pedido_de_codigo:
            try:
                from src.core.dispatcher_handlers import expects_factual_grounding
                if expects_factual_grounding(processed_text):
                    duration = time.perf_counter() - start
                    _log_router(processed_text[:50], TaskType.WEB_SEARCH.value,
                                "keywords_factual", 0.6, duration=duration)
                    return TaskType.WEB_SEARCH.value, 0.6
            except Exception as e:
                # AUD-SIL: si el hook no se puede importar, se sigue el ruteo
                # normal (no se inventa una ruta) pero queda registrado.
                log_degraded(logger, e, "src/router/classifier_core.py:factual_grounding")
        # ✅ v0.6.8pp (logs/reg_error.txt 15:03): "escribí en una línea:
        # \"prueba de historial netron-123\"" → code_testing porque "prueba"
        # aparecía en el TEXTO CITADO (contenido a escribir, no una orden de
        # testear). Los spans entre comillas suelen ser contenido del usuario
        # (strings, nombres de archivo, citas) — no los usamos para decidir
        # intención por keyword. Las órdenes fuera de comillas siguen matcheando.
        _text_no_quotes = re.sub(
            r"[\"'“”‘’]([^\"'“”‘’]{0,120})[\"'“”‘’]", " ", processed_text
        )
        matches = [
            self.KEYWORD_MAP[kw]
            for kw in self.KEYWORD_MAP
            if self._keyword_match(kw, _text_no_quotes)
        ]
        if matches:
            most_common = Counter(matches).most_common(1)[0][0]
            duration = time.perf_counter() - start
            # ✅ v4.7.0: guard de negación — bloquea tareas de código si hay negación.
            _CODE_TASK_TYPES = {
                TaskType.CODE_GENERATION.value, TaskType.CODE_DEBUGGING.value,
                TaskType.CODE_EXPLANATION.value, TaskType.CODE_OPTIMIZATION.value,
                TaskType.CODE_CORRECTION.value, TaskType.CODE_REVIEW.value,
            }
            if most_common in _CODE_TASK_TYPES and _has_leading_negation(processed_text):
                pass  # cae a embeddings → PASO 4 LLM
            else:
                _log_router(processed_text[:50], most_common, "keywords", 0.6, duration=duration)
                return most_common, 0.6
        if "noticias" in processed_text or "news" in processed_text:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.NEWS_SEARCH.value, "keywords", 0.6, duration=duration)
            return TaskType.NEWS_SEARCH.value, 0.6
        if not self._set_keyword_any(CODE_CONTEXT_KEYWORDS, processed_text):
            for kw in self.FINANCE_KEYWORDS:
                if self._keyword_match(kw, processed_text):
                    duration = time.perf_counter() - start
                    _log_router(processed_text[:50], TaskType.FINANCE_QUERY.value, "keywords", 0.6, duration=duration)
                    return TaskType.FINANCE_QUERY.value, 0.6
        duration = time.perf_counter() - start
        _log_router(processed_text[:50], "none", "keywords_miss", 0.0, duration=duration)
        return None, 0.0

    def _generate_cache_key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _get_from_cache(self, text: str) -> Optional[Tuple[TaskType, float]]:
        cache_key = self._generate_cache_key(text)
        if cache_key not in self._cache:
            self._stats["cache_misses"] += 1
            return None
        task_type, confidence, timestamp = self._cache[cache_key]
        if (datetime.now() - timestamp).total_seconds() >= self._cache_ttl:
            del self._cache[cache_key]
            self._stats["cache_misses"] += 1
            return None
        self._stats["cache_hits"] += 1
        return task_type, confidence

    def _save_to_cache(self, text: str, task_type: TaskType, confidence: float) -> None:
        cache_key = self._generate_cache_key(text)
        self._cache[cache_key] = (task_type, confidence, datetime.now())

    def train(self, examples: Dict[str, List[str]], save_path: Optional[Path] = None) -> Dict[str, Any]:
        start = time.perf_counter()
        logger.info(f"Training core classifier with {len(examples)} task types")
        training_vectors = {}
        total_examples = 0
        for task_type, texts in examples.items():
            training_vectors[task_type] = []
            for text in texts:
                try:
                    vector = self._get_embedding(text)
                    training_vectors[task_type].append(vector)
                    total_examples += 1
                except Exception as e:
                    _log_error(e, "IntentClassifierCore.train", extra={"task_type": task_type})
                    logger.warning(f"Failed to embed example: {e}")
        self._training_vectors = training_vectors
        save_path = save_path or self.model_path
        save_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump({
                    "vectors": training_vectors,
                    "trained_at": datetime.now().isoformat(),
                    "total_examples": total_examples
                }, f, indent=2)
            duration = time.perf_counter() - start
            logger.info(f"Core classifier trained: {total_examples} examples → {save_path} in {duration:.4f}s")
            return {
                "task_types": len(training_vectors),
                "total_examples": total_examples,
                "model_path": str(save_path),
                "duration": duration
            }
        except Exception as e:
            duration = time.perf_counter() - start
            _log_error(e, "IntentClassifierCore.train", extra={"save_path": str(save_path)})
            logger.error(f"Failed to save trained model: {e}")
            raise

    def add_example(self, task_type: str, text: str, persist: bool = True) -> bool:
        if not text or not task_type:
            return False
        try:
            vector = self._get_embedding(text)
            if not vector:
                return False
            if task_type not in self._training_vectors:
                self._training_vectors[task_type] = []
            _MAX_EXAMPLES_PER_CLASS = 200
            if len(self._training_vectors[task_type]) >= _MAX_EXAMPLES_PER_CLASS:
                self._training_vectors[task_type].pop(0)
            self._training_vectors[task_type].append(vector)
            self.clear_cache()
            if persist:
                self._save_model()
            return True
        except Exception as e:
            _log_error(e, "IntentClassifierCore.add_example",
                       extra={"task_type": task_type, "text_len": len(text)})
            logger.warning(f"add_example failed (non-fatal): {e}")
            return False

    def _save_model(self) -> None:
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            total = sum(len(v) for v in self._training_vectors.values())
            with open(self.model_path, "w", encoding="utf-8") as f:
                json.dump({
                    "vectors": self._training_vectors,
                    "trained_at": datetime.now().isoformat(),
                    "total_examples": total,
                }, f)
            logger.debug(f"Model persisted: {total} examples → {self.model_path}")
        except Exception as e:
            _log_error(e, "IntentClassifierCore._save_model",
                       extra={"model_path": str(self.model_path)})
            logger.warning(f"_save_model failed (non-fatal): {e}")

    def get_learning_stats(self) -> Dict[str, Any]:
        per_class = {k: len(v) for k, v in self._training_vectors.items()}
        return {
            "total_examples": sum(per_class.values()),
            "classes": len(per_class),
            "examples_per_class": per_class,
            "threshold": self.threshold,
            "cache_size": len(self._cache),
        }

    def get_supported_types(self) -> List[str]:
        return [t.value for t in TaskType]

    def clear_cache(self) -> None:
        cache_size = len(self._cache)
        self._cache.clear()
        logger.info(f"IntentClassifierCore cache cleared: {cache_size} entries")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "cache_size": len(self._cache),
            "training_examples": sum(len(examples) for examples in self._training_vectors.values()),
            "task_types_supported": len(self.get_supported_types()),
            "threshold": self.threshold,
            "use_embeddings": self.use_embeddings,
            "classification_stats": self._stats.copy(),
            # ✅ v4.8.0: exponer info de adaptive threshold
            "adaptive_enabled": self._adaptive_enabled,
            "last_feature_std": self._last_feature_std,
            "adaptive_thresholds_count": len(ADAPTIVE_THRESHOLDS),
            "adaptive_default_threshold": ADAPTIVE_DEFAULT_THRESHOLD,
            "min_floor_threshold": MIN_FLOOR_THRESHOLD,
        }
