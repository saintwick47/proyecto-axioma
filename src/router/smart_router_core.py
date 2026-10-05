#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# smart_router_core.py - Router Core: constantes, caché, entidades y lógica pura.
# Ruta: /ruta/a/axioma/src/router/smart_router_core.py
# Autor: SaintWick
# Versión: 0.3.1
#
# Proporciona componentes reutilizables para el enrutamiento inteligente:
#   - CodeQualityAnalyzer: valida y califica código generado.
#   - QueryNormalizer: normaliza consultas para optimizar ruteo.
#   - APIRequirementEvaluator: determina necesidad y disponibilidad de APIs.
#   - RoutingDecisionCache: caché LRU para decisiones.
#   - BoundedMetrics: métricas con límite de memoria.
#   - FallbackStrategy: estrategia de fallback para modelos.
# ═══════════════════════════════════════════════════════════════
import hashlib
import logging
import re
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.domain.types import TaskType

try:
    from config.settings import settings
    SETTINGS_AVAILABLE = True
except ImportError:
    SETTINGS_AVAILABLE = False

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# CONSTANTES — un solo lugar para cambiar modelo y umbrales
# ═══════════════════════════════════════════════════════════════

# ✅ v0.3.1: Modelo principal de código — settings.llm_code_model (qwen2.5-coder:7b).
# Cambiar aquí afecta todo el sistema de routing de código.
CODE_MODEL = getattr(settings, "llm_code_model", "qwen2.5-coder:7b")

# Umbrales de calidad por nivel de código generado.
QUALITY_THRESHOLDS: Dict[str, Dict[str, Any]] = {
    "level_1": {"min_quality": 0.70, "min_syntax": 0.90, "max_lines": 30,  "action": "accept"},
    "level_2": {"min_quality": 0.60, "min_syntax": 0.80, "max_lines": 100, "action": "accept"},
    "level_3": {"min_quality": 0.50, "min_syntax": 0.70, "max_lines": 200, "action": "review"},
    "level_4": {"min_quality": 0.40, "min_syntax": 0.50, "max_lines": 500, "action": "review"},
}

# Pesos del score de calidad. Deben sumar 1.0.
QUALITY_WEIGHTS: Dict[str, float] = {
    "syntax_valid":      0.40,
    "has_docstrings":    0.25,
    "logical_structure": 0.15,
    "error_handling":    0.10,
    "has_tests":         0.10,
}

# Timeouts en segundos por nivel de código
LEVEL_TIMEOUTS: Dict[int, int] = {
    1: 120,
    2: 300,
    3: 600,
    4: 900,
}


# ═══════════════════════════════════════════════════════════════
# FALLBACK STRATEGY
# ═══════════════════════════════════════════════════════════════
class FallbackStrategy:
    """Estrategia de fallback para el modelo de código."""

    CRITICAL_QUALITY_THRESHOLD = 0.35

    def should_fallback(
        self,
        estimated_lines: int,
        estimated_complexity: str,
        previous_quality: Optional[float] = None,
    ) -> bool:
        if previous_quality is not None and previous_quality < self.CRITICAL_QUALITY_THRESHOLD:
            return True
        return False

    def get_fallback_model(self, task_type: str) -> str:
        # ✅ v0.3.1: Solo modelos instalados en Ollama.
        #   Eliminado aletheia-3b:latest (no instalado).
        #   complex_reasoning → modelo general instalado, no código.
        if task_type == "code_generation":
            return CODE_MODEL                          # settings.llm_code_model
        if task_type == "complex_reasoning":
            return getattr(settings, "llm_default_model", "qwen3:8b")  # modelo general instalado
        return getattr(settings, "llm_fallback_1", "qwen2.5-coder:7b")  # fallback ligero instalado

    def get_timeout_for_level(self, level: int) -> int:
        return LEVEL_TIMEOUTS.get(level, 300)


# ═══════════════════════════════════════════════════════════════
# CODE QUALITY ANALYZER — Lógica pura de validación de código
# ═══════════════════════════════════════════════════════════════
class CodeQualityAnalyzer:
    """
    Analiza la calidad del código generado por el LLM.
    Extraído de SmartRouter para permitir testeo unitario sin mockear APIs.
    """

    def __init__(self, weights: Dict[str, float], thresholds: Dict[str, Dict[str, Any]]) -> None:
        self.weights = weights
        self.thresholds = thresholds

    def extract_code(self, content: str) -> str:
        """Extrae código limpio desde bloques markdown."""
        if not content or not content.strip():
            return ""

        blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", content, re.DOTALL)
        if blocks:
            return "\n".join(b.strip() for b in blocks)

        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.split("\n")
            if len(lines) > 2:
                return "\n".join(lines[1:-1]).strip()

        return stripped

    def compute_quality_score(self, analysis: Dict[str, Any]) -> float:
        """Calcula el score ponderado a partir de los flags del análisis."""
        score = 0.0
        if analysis["has_syntax"]:
            score += self.weights["syntax_valid"]
        if analysis["has_docstrings"]:
            score += self.weights["has_docstrings"]
        if analysis["has_error_handling"]:
            score += self.weights["error_handling"]
        if analysis["has_tests"]:
            score += self.weights["has_tests"]

        lines = analysis["line_count"]
        if 10 <= lines <= 200:
            score += self.weights["logical_structure"]
        elif lines > 200:
            score += self.weights["logical_structure"] * 0.7

        return min(score, 1.0)

    def analyze_code_quality(self, content: str, max_tokens: int = 512) -> Dict[str, Any]:
        """Ejecuta el análisis completo de calidad sobre el contenido generado."""
        clean = self.extract_code(content)
        lines = clean.split("\n") if clean else []

        analysis: Dict[str, Any] = {
            "has_syntax":           False,
            "has_docstrings":       False,
            "has_tests":            False,
            "has_error_handling":   False,
            "line_count":           len(lines),
            "syntax_error":         None,
            "quality_score":        0.0,
            "logical_structure_score": 0.0,
            "recommended_action":   "review",
            "code_level":           2,
        }

        try:
            compile(clean, "<generated>", "exec")
            analysis["has_syntax"] = True
        except SyntaxError as e:
            analysis["syntax_error"] = str(e)
            logger.warning(f"❌ SyntaxError en código generado: {e}")

        analysis["has_docstrings"]     = '"""' in clean or "'''" in clean
        analysis["has_tests"]          = "test" in clean.lower() or "assert" in clean
        analysis["has_error_handling"] = "try" in clean and "except" in clean

        score = self.compute_quality_score(analysis)
        analysis["logical_structure_score"] = score
        analysis["quality_score"]           = score

        line_count = analysis["line_count"]
        if SETTINGS_AVAILABLE and hasattr(settings, "get_code_level"):
            analysis["code_level"] = settings.get_code_level(line_count)
        else:
            analysis["code_level"] = 2

        level_key = f"level_{analysis['code_level']}"
        threshold = self.thresholds.get(level_key)
        if threshold and score >= threshold["min_quality"]:
            analysis["recommended_action"] = threshold["action"]

        return analysis

    def log_quality_alerts(self, analysis: Dict[str, Any]) -> None:
        """Emite warnings/errors si la calidad del código es baja."""
        if analysis["quality_score"] < 0.50:
            logger.warning(
                f"⚠️ CALIDAD BAJA: {analysis['quality_score']:.2f} — requiere revisión"
            )
        if not analysis["has_syntax"]:
            logger.error(
                f"❌ SYNTAX ERROR: {analysis.get('syntax_error', 'desconocido')}"
            )


# ═══════════════════════════════════════════════════════════════
# QUERY NORMALIZER — Lógica pura de normalización de consultas
# ═══════════════════════════════════════════════════════════════
class QueryNormalizer:
    """
    Evalúa y normaliza queries para optimizar el ruteo y la generación.
    Extraído de SmartRouter para permitir testeo unitario sin dependencias.
    """

    DIRECT_FORMAT_PATTERNS: List[str] = [
        r"cotizac[ióo]n", r"cu[aá]nto", r"precio", r"pron[oó]stico",
        r"temperatura",   r"\bhora\b",  r"dólar",  r"euro",
        r"\bblue\b",      r"\bmep\b",   r"\bccl\b",
        r"compra",        r"venta",     r"máx",     r"mín",    r"precip",
    ]

    LOCATION_PATTERN: str = r"(?:en|de|para)\s+([A-Za-zÁ-ú\s]+?)(?:\s*$|[,.!?])"

    def requires_direct_format(self, query: str) -> bool:
        """True si la query pide dato puntual (precio, hora, cotización…)."""
        q = query.lower()
        return any(re.search(p, q) for p in self.DIRECT_FORMAT_PATTERNS)

    def normalize_weather_query(self, query: str) -> str:
        """Enriquece la query de clima con términos que mejoran las APIs."""
        match = re.search(self.LOCATION_PATTERN, query.lower())
        if match:
            location = match.group(1).strip().title()
            return f"current weather forecast {location} temperature humidity precipitation today"
        return f"{query} current weather temperature forecast"


# ═══════════════════════════════════════════════════════════════
# API REQUIREMENT EVALUATOR — Lógica pura de evaluación de APIs
# ═══════════════════════════════════════════════════════════════
class APIRequirementEvaluator:
    """
    Evalúa si un TaskType requiere APIs externas y verifica disponibilidad.
    Extraído de SmartRouter para eliminar acoplamiento con settings en el router.
    """

    API_REQUIRED_TASKS: Dict[str, Dict[str, Any]] = {
        TaskType.WEATHER_QUERY.value:  {"apis": ["tavily", "serper"], "fallback": "llm"},
        TaskType.NEWS_QUERY.value:     {"apis": ["newsapi", "tavily"], "fallback": "llm"},
        TaskType.FINANCE_QUERY.value:  {"apis": ["tavily", "serper"], "fallback": "llm"},
        TaskType.TIME_QUERY.value:     {"apis": [],                   "fallback": "static"},
        TaskType.WEB_SEARCH.value:     {"apis": ["tavily", "serper"], "fallback": "llm"},
    }

    def requires_api(self, task_type: str) -> Tuple[bool, List[str], str]:
        """
        Determina si la tarea necesita API externa y cuáles están disponibles.
        Retorna: (api_required, available_apis, fallback_strategy)
        """
        if task_type not in self.API_REQUIRED_TASKS:
            return False, [], "none"

        config = self.API_REQUIRED_TASKS[task_type]

        api_keys = {
            "tavily":  getattr(settings, "tavily_api_key", None) if SETTINGS_AVAILABLE else None,
            "serper":  getattr(settings, "serper_api_key", None) if SETTINGS_AVAILABLE else None,
            "newsapi": getattr(settings, "news_api_key", None) if SETTINGS_AVAILABLE else None,
        }

        available = [api for api in config["apis"] if api_keys.get(api)]
        return True, available, config["fallback"]

    def get_api_status(self) -> Dict[str, Any]:
        """Estado de configuración de APIs externas."""
        has_tavily = bool(getattr(settings, "tavily_api_key", None)) if SETTINGS_AVAILABLE else False
        has_serper = bool(getattr(settings, "serper_api_key", None)) if SETTINGS_AVAILABLE else False
        has_newsapi = bool(getattr(settings, "news_api_key", None)) if SETTINGS_AVAILABLE else False
        has_google = (bool(getattr(settings, "google_api_key", None)) and
                      bool(getattr(settings, "search_engine_id", None))) if SETTINGS_AVAILABLE else False

        return {
            "tavily":        has_tavily,
            "serper":        has_serper,
            "newsapi":       has_newsapi,
            "google_cse":    has_google,
            "all_configured": all([has_tavily, has_serper, has_newsapi]),
        }


# ═══════════════════════════════════════════════════════════════
# ROUTING DECISION CACHE — LRU thread-safe
# ═══════════════════════════════════════════════════════════════
class RoutingDecisionCache:
    """Caché LRU para decisiones de ruteo, compartida entre instancias."""

    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max_size
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.encode()).hexdigest()[:32]

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        key = self._key(query)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                return self._cache[key]
            self._misses += 1
            return None

    def set(self, query: str, decision: Dict[str, Any]) -> None:
        key = self._key(query)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = decision
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size":     len(self._cache),
            "max_size": self.max_size,
            "hits":     self._hits,
            "misses":   self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
        }


# ═══════════════════════════════════════════════════════════════
# BOUNDED METRICS — métricas con límite de memoria
# ═══════════════════════════════════════════════════════════════
class BoundedMetrics:
    """Métricas de ruteo con límite de entradas para evitar crecimiento infinito."""

    _EMPTY_ENTRY: Dict[str, Any] = {
        "count": 0, "total_latency": 0.0,
        "total_confidence": 0.0, "successes": 0, "failures": 0,
    }

    def __init__(self, max_entries: int = 1000) -> None:
        self.max_entries = max_entries
        self._metrics: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._total_count = 0

    def record(self, task_type: str, model: str, latency_ms: float, confidence: float) -> None:
        key = f"{task_type}:{model}"
        with self._lock:
            if key not in self._metrics:
                while len(self._metrics) >= self.max_entries:
                    self._metrics.popitem(last=False)
                self._metrics[key] = dict(self._EMPTY_ENTRY)
            entry = self._metrics[key]
            entry["count"] += 1
            entry["total_latency"] += latency_ms
            entry["total_confidence"] += confidence
            self._total_count += 1

    def get_all(self) -> Dict[str, Any]:
        with self._lock:
            return {
                key: {
                    "count":          data["count"],
                    "avg_latency_ms": round(data["total_latency"] / data["count"], 2),
                    "avg_confidence": round(data["total_confidence"] / data["count"], 4),
                    "success_rate":   round(data["successes"] / max(data["count"], 1), 4),
                }
                for key, data in self._metrics.items()
                if data["count"] > 0
            }

    def clear(self) -> None:
        with self._lock:
            self._metrics.clear()
            self._total_count = 0

    def get_total_count(self) -> int:
        return self._total_count


# ═══════════════════════════════════════════════════════════════
# ROUTING RESULT — dataclass-like con __slots__
# ═══════════════════════════════════════════════════════════════
class RoutingResult:
    """Resultado de una decisión de ruteo."""

    __slots__ = [
        "task_type", "model", "temperature", "confidence",
        "cache_hit", "method", "fallback_chain", "latency_ms",
        "api_required", "max_tokens", "presentation_style",
        "code_level", "quality_score", "timestamp",
    ]

    def __init__(
        self,
        task_type: TaskType,
        model: str,
        temperature: float,
        confidence: float,
        cache_hit: bool = False,
        method: str = "direct",
        fallback_chain: Optional[List[str]] = None,
        latency_ms: float = 0.0,
        api_required: bool = False,
        max_tokens: int = 512,
        presentation_style: str = "direct",
        code_level: int = 2,
        quality_score: float = 0.0,
    ) -> None:
        self.task_type         = task_type
        self.model             = model
        self.temperature       = temperature
        self.confidence        = confidence
        self.cache_hit         = cache_hit
        self.method            = method
        self.fallback_chain    = fallback_chain or []
        self.latency_ms        = latency_ms
        self.api_required      = api_required
        self.max_tokens        = max_tokens
        self.presentation_style = presentation_style
        self.code_level        = code_level
        self.quality_score     = quality_score
        self.timestamp         = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_type":          self.task_type.value,
            "model":              self.model,
            "temperature":        self.temperature,
            "confidence":         round(self.confidence, 4),
            "cache_hit":          self.cache_hit,
            "method":             self.method,
            "fallback_chain":     self.fallback_chain,
            "latency_ms":         round(self.latency_ms, 2),
            "api_required":       self.api_required,
            "max_tokens":         self.max_tokens,
            "presentation_style": self.presentation_style,
            "code_level":         self.code_level,
            "quality_score":      round(self.quality_score, 4),
            "timestamp":          self.timestamp.isoformat(),
        }

    @classmethod
    def from_cache(cls, cached: Dict[str, Any], latency_ms: float) -> "RoutingResult":
        return cls(
            task_type=TaskType(cached["task_type"]),
            model=cached["model"],
            temperature=cached["temperature"],
            confidence=cached["confidence"],
            cache_hit=True,
            method="cache",
            latency_ms=latency_ms,
            api_required=cached.get("api_required", False),
            max_tokens=cached.get("max_tokens", 512),
            presentation_style=cached.get("presentation_style", "direct"),
            code_level=cached.get("code_level", 2),
            quality_score=cached.get("quality_score", 0.0),
        )
