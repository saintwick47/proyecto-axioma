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
from src.router.classifier_learning import ClassifierLearningMixin
from src.router.classifier_keyword_rules import ClassifierKeywordRulesMixin
from src.router.classifier_scoring import ClassifierScoringMixin
from src.router.classifier_cache import ClassifierCacheMixin
from src.router.classifier_keywords import (
    _WORD_BOUNDARY_KEYWORDS as _TABLA_WORD_BOUNDARY,
    FINANCE_KEYWORDS as _TABLA_FINANCE,
    KEYWORD_MAP as _TABLA_KEYWORD_MAP,
    TRAINING_EXAMPLES as _TABLA_TRAINING,
)
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

# ✅ 2026-10-13 (refactorización, pieza 4): las tablas de palabras viven ahora en
# `classifier_keywords.py`, junto a las otras cuatro. Se importan acá con los MISMOS nombres,
# así el resto del archivo —y quien las importe de este módulo— sigue funcionando igual.
from src.router.classifier_keywords import (
    CODE_CONTEXT_KEYWORDS, CODE_EXPLANATION_KEYWORDS, CODE_VERBS, DOCUMENT_NOUNS,
    GREETING_PATTERNS, LOCATION_PATTERNS, QUALITY_VALIDATION_KEYWORDS,
    TASK_TYPE_NORMALIZATION, VISION_SCREEN_KEYWORDS,
)


class IntentClassifierCore(
    ClassifierLearningMixin, ClassifierCacheMixin, ClassifierScoringMixin,
    ClassifierKeywordRulesMixin, ClassifierEmbeddingMixin
):
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


    # ✅ 2026-10-13: las tablas de palabras se movieron a `classifier_keywords.py`
    # (refactorización por partes, R14 exceptuado el 2026-10-12). Se re-exponen como
    # ATRIBUTOS DE CLASE para no cambiar nada: el código las usa como `self.KEYWORD_MAP`
    # y como `IntentClassifierCore._WORD_BOUNDARY_KEYWORDS`.
    KEYWORD_MAP = _TABLA_KEYWORD_MAP
    FINANCE_KEYWORDS = _TABLA_FINANCE
    _WORD_BOUNDARY_KEYWORDS = _TABLA_WORD_BOUNDARY
    TRAINING_EXAMPLES = _TABLA_TRAINING

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

    def get_supported_types(self) -> List[str]:
        return [t.value for t in TaskType]

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
