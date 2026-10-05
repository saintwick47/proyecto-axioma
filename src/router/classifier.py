#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.1 — Clasificador de Intenciones (Facade Orquestador)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/classifier.py
# Autor: SaintWick
# Fecha: 2026-03-18
# Propósito: Orquestar clasificación: cache → API detection → keywords → embeddings
# Nota: Archivo original backup en classifier.py.bak (1225 líneas)
#
# ✅ FIX v0.1.1: Normalización defensiva de task_type en PASO 2 y PASO 3
# ✅ v0.1.2: PASO 4 LLM fallback — qwen3:8b interpreta intención semántica
#            cuando PASO 1-3 no alcanzan confianza > 0.5 sobre GENERAL_CHAT.
# ✅ v0.1.3: LLM promovido a PASO 2 (clasificador primario).
#            Keywords → PASO 3 (fallback si LLM falla/timeout).
#            Embeddings → PASO 4 (fallback final).
#   _classify_by_keywords() y _classify_by_embeddings() pueden retornar
#   string plano O TaskType enum. Ahora se usa _ensure_str_task_type()
#   para garantizar consistencia y evitar AttributeError: 'str' has no 'value'
# ═══════════════════════════════════════════════════════════════
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Tuple

if TYPE_CHECKING:  # ✅ ISSUE-098: solo anotación
    from src.domain.context import SessionContext
from pathlib import Path
from datetime import datetime
from config.settings import settings
from src.domain.types import TaskType
from src.domain.exceptions import RouterError

# ✅ CORREGIDO: Imports de módulos refactorizados
from src.router.classifier_core import IntentClassifierCore, _log_router, _log_error
from src.router.classifier_api import APIDetector
from src.router.classifier_external_data import ClassifierExternalDataMixin
import json as _json
import math as _math
import threading as _threading

# ✅ MEDIDO (2026-10-03): el paso de clasificación por LLM se puede apagar y por
# defecto está APAGADO (`settings.classifier_llm_enabled`). Dos mediciones:
#   1. Sin `think: False` la llamada tardaba **68,5 s** contra un límite de 15 s ⇒
#      se pasaba del límite SIEMPRE: 15 s perdidos por consulta y, encima, el
#      clasificador por LLM **nunca clasificaba** (todo caía a palabras clave).
#      Con `think: False` responde en 3,3-5,7 s (medido).
#   2. Pero al dejarlo decidir, el ruteo EMPEORA la experiencia en preguntas
#      conceptuales: "¿Qué es un índice en una base de datos?" → `data_analysis`
#      ⇒ handler de análisis con `MAX_TOKENS_ANALYSIS` ⇒ **203,9 s y 829 tokens de
#      informe** (contra 79,2 s del camino de chat). Y en charla alucina
#      categorías con confianza 0,95 ("Hola" → `translation`, "Gracias" →
#      `translation`, "¿Me ayudás con algo?" → `technical_support`).
# Por eso: apagado por defecto (equivale al comportamiento real de hoy, sin los
# 15 s perdidos) y, si se enciende, sólo se aceptan las categorías de CÓDIGO, que
# son las que el modelo acierta de forma consistente y tienen su propio camino.
LLM_CLASSIFY_ACEPTADOS = frozenset({
    "code_generation", "code_correction", "code_explanation", "code_review",
    "code_optimization", "code_testing", "code_documentation", "code_migration",
    "code_deployment", "code_debugging",
})
LLM_CLASSIFY_CONF_MINIMA = 0.7

_KNN_LOCK = _threading.Lock()
_KNN_LOADED: Optional[Dict[str, Any]] = None


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# HELPER DE NORMALIZACIÓN (FIX v0.1.1)
# ═══════════════════════════════════════════════════════════
def _ensure_str_task_type(value: Any, fallback: str = "general_chat") -> str:
    """
    Normaliza cualquier valor de task_type a string plano.

    Resuelve la inconsistencia donde _classify_by_keywords() y
    _classify_by_embeddings() pueden retornar:
      - TaskType enum → se extrae .value
      - str plano → se retorna tal cual
      - None/otro → se usa fallback

    Args:
        value: Valor a normalizar (Enum, str, None, etc.)
        fallback: Valor por defecto si no se puede extraer string

    Returns:
        str: task_type siempre como string plano
    """
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if isinstance(value, Enum):
        return value.value
    # Último recurso: convertir a string
    try:
        return str(value)
    except Exception:
        return fallback


# ═══════════════════════════════════════════════════════════
# CLASE: IntentClassifier (Facade)
# ═══════════════════════════════════════════════════════════
class IntentClassifier(ClassifierExternalDataMixin):
    """
    Facade orquestador para clasificación de intenciones con APIs externas.
    Mantiene interfaz pública compatible con versión original.

    Usa combinación de:
    1. Palabras clave (fast, fallback)
    2. Embeddings + similitud coseno (preciso)
    3. Modelo ML entrenado (sklearn, óptimo)
    4. Integración con APIs externas (clima, noticias, finanzas, hora)

    Features:
    - 36 tipos de tarea soportados (según types.py)
    - Umbral de confianza configurable
    - Cache de clasificaciones recientes
    - Fallback a keywords si ML falla
    - Detección automática de consultas de clima/noticias/finanzas/hora
    - Integración con Tavily, Serper, News API para datos en tiempo real
    - Logging detallado de todas las operaciones
    - Timing de cada clasificación
    - Error tracking completo

    Flujo de clasificación:
    1. Verificar cache
    2. Detectar consultas especiales (clima/noticias/finanzas/hora/web_search)
    3. Clasificación por keywords (rápido)
    4. Clasificación por embeddings (preciso)
    5. Fallback a GENERAL_CHAT

    Example:
        >>> classifier = IntentClassifier(threshold=0.7)
        >>> result = classifier.classify("¿Qué clima hace hoy?")
        >>> print(f"Task: {result['task_type']}, Confidence: {result['confidence']}")
    """

    # ═══════════════════════════════════════════════════════════
    # INICIALIZACIÓN
    # ═══════════════════════════════════════════════════════════
    def __init__(
        self,
        model_path: Optional[Path] = None,
        threshold: float = 0.7,
        use_embeddings: bool = True,
        enable_api_integration: bool = True,
        clear_cache_on_init: bool = False,
    ):
        """
        Inicializa clasificador de intenciones con soporte para APIs externas.

        Args:
            model_path: Ruta a modelo entrenado (opcional)
            threshold: Umbral de confianza mínimo
            use_embeddings: Usar embeddings para clasificación
            enable_api_integration: Habilitar integración con APIs externas
            clear_cache_on_init: Limpiar cache al inicializar. False por defecto
                para preservar cache entre instancias (ej: tests, SmartRouter).
                Pasar True solo cuando se requiere reset explícito del estado.

        Example:
            >>> classifier = IntentClassifier(
            ...     threshold=0.7,
            ...     use_embeddings=True,
            ...     enable_api_integration=True
            ... )
        """
        start = time.time()
        _log_router("INIT", "FACADE", "initializing", 0.0)

        # ✅ CORREGIDO: Composición en lugar de lógica monolítica
        self._core = IntentClassifierCore(
            model_path=model_path,
            threshold=threshold,
            use_embeddings=use_embeddings
        )
        self._api = APIDetector(enable_api_integration=enable_api_integration)
        self._cache_ttl = 300

        # v0.0.9: clear_cache_on_init=False por defecto — preserva cache entre instancias.
        # Solo limpiar cuando se pase True explícitamente (reset de estado controlado).
        if clear_cache_on_init:
            core_entries = len(self._core._cache)
            location_entries = len(self._api._location_cache)
            self._core.clear_cache()
            self._api.clear_location_cache()
            _log_router("INIT", "CACHE_CLEARED", "clear_cache_on_init", 1.0)
            logger.info(
                f"[CLASSIFIER] Cache cleared on initialization | "
                f'{{"core_cache_entries": {core_entries}, '
                f'"location_cache_entries": {location_entries}, '
                f'"total_entries_cleared": {core_entries + location_entries}}}'
            )

        try:
            duration = time.time() - start
            _log_router("INIT", "FACADE", "initialized", 1.0, duration=duration)
            logger.info(f"IntentClassifier facade initialized: threshold={threshold}, api_integration={enable_api_integration}")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "IntentClassifier.__init__", extra={"threshold": threshold})
            _log_router("INIT", "FACADE", "init_failed", 0.0, duration=duration)
            raise

    # ═══════════════════════════════════════════════════════════
    # MÉTODO PRINCIPAL DE CLASIFICACIÓN
    # ═══════════════════════════════════════════════════════════
    def classify(self, text: str, force_keywords: bool = False) -> Dict[str, Any]:
        """
        Clasifica texto por tipo de tarea con integración de APIs externas.

        Flujo de clasificación:
        1. Verificar cache
        2. Detectar consultas especiales (clima/noticias/finanzas/hora/web_search)
        3. Clasificación por keywords (rápido)
        4. Clasificación por embeddings (preciso)
        5. Fallback a GENERAL_CHAT

        Args:
            text: Texto a clasificar
            force_keywords: Forzar clasificación por keywords

        Returns:
            Dict con:
            - task_type: Tipo de tarea clasificado
            - confidence: Confianza de la clasificación (0.0-1.0)
            - method: Método usado (cache/keywords/embeddings/special)
            - alternatives: Tipos alternativos considerados
            - metadata: Información adicional (ubicación, API usada, etc.)
            - duration: Tiempo de procesamiento en segundos

        Example:
            >>> classifier = IntentClassifier()
            >>> result = classifier.classify("¿Qué clima hace hoy?")
            >>> print(f"Task: {result['task_type']}, Method: {result['method']}")
        """
        start = time.time()
        _log_router(text[:50], "PENDING", "classifying", 0.0)
        self._core._stats["total_classifications"] += 1

        # ═══════════════════════════════════════════════════════
        # PASO 0: Verificar cache primero
        # ═══════════════════════════════════════════════════════
        cached = self._core._get_from_cache(text)
        if cached:
            task_type, confidence = cached
            duration = time.time() - start
            _log_router(text[:50], task_type.value if isinstance(task_type, Enum) else str(task_type), "CACHE", confidence, cache_hit=True, duration=duration)
            logger.info(f"Classification (cache): '{text[:30]}' → {_ensure_str_task_type(task_type)} in {duration:.4f}s")
            return {
                "task_type": _ensure_str_task_type(task_type),
                "confidence": round(confidence, 4),
                "method": "cache",
                "alternatives": [],
                "metadata": {},
                "duration": duration,
            }

        # Resultado por defecto
        result = {
            "task_type": TaskType.GENERAL_CHAT.value,
            "confidence": 0.5,
            "method": "default",
            "alternatives": [],
            "text_preview": text[:50] + "..." if len(text) > 50 else text,
            "metadata": {},
        }

        # ═══════════════════════════════════════════════════════
        # PASO 1: Detectar consultas especiales con APIs
        # ✅ CORREGIDO v0.1.0: Guard de código — si el texto contiene
        # indicadores de Python (def, class, import, FastAPI, etc.)
        # se salta toda la detección especial y va directo a keywords.
        # Evita que prompts de código largos sean clasificados como
        # finance_query por keywords genéricas como 'valor' o 'compra'.
        # ═══════════════════════════════════════════════════════
        if self._api.enable_api_integration and not force_keywords:
            # ✅ GUARD v0.1.0: si es prompt de código, saltar detección especial
            if self._api._is_code_prompt(text):
                logger.debug(f"PASO 1 skipped: código detectado en '{text[:50]}'")
            else:
                # Consulta de clima
                if self._api._is_weather_query(text):
                    location = self._api._extract_location(text)
                    result["task_type"] = TaskType.WEATHER_QUERY.value
                    result["confidence"] = 0.95
                    result["method"] = "special_weather"
                    result["metadata"]["location"] = location
                    result["metadata"]["api_available"] = bool(
                        settings.tavily_api_key or settings.serper_api_key
                    )
                    self._core._stats["special_classifications"] = self._core._stats.get("special_classifications", 0) + 1
                    self._core._save_to_cache(text, TaskType.WEATHER_QUERY, 0.95)
                    duration = time.time() - start
                    _log_router(text[:50], TaskType.WEATHER_QUERY.value, "SPECIAL", 0.95, duration=duration, api_required=True)
                    logger.info(f"Classification (weather): '{text[:30]}' → WEATHER_QUERY in {duration:.4f}s")
                    result["duration"] = duration
                    return result

                # Consulta de noticias
                if self._api._is_news_query(text):
                    result["task_type"] = TaskType.NEWS_QUERY.value
                    result["confidence"] = 0.85
                    result["method"] = "special_news"
                    result["metadata"]["api_available"] = bool(settings.news_api_key)
                    self._core._stats["special_classifications"] = self._core._stats.get("special_classifications", 0) + 1
                    self._core._save_to_cache(text, TaskType.NEWS_QUERY, 0.85)
                    duration = time.time() - start
                    _log_router(text[:50], TaskType.NEWS_QUERY.value, "SPECIAL", 0.85, duration=duration, api_required=True)
                    logger.info(f"Classification (news): '{text[:30]}' → NEWS_QUERY in {duration:.4f}s")
                    result["duration"] = duration
                    return result

                # Consulta financiera
                if self._api._is_finance_query(text):
                    result["task_type"] = TaskType.FINANCE_QUERY.value
                    result["confidence"] = 0.85
                    result["method"] = "special_finance"
                    result["metadata"]["api_available"] = bool(settings.tavily_api_key)
                    self._core._stats["special_classifications"] = self._core._stats.get("special_classifications", 0) + 1
                    self._core._save_to_cache(text, TaskType.FINANCE_QUERY, 0.85)
                    duration = time.time() - start
                    _log_router(text[:50], TaskType.FINANCE_QUERY.value, "SPECIAL", 0.85, duration=duration, api_required=True)
                    logger.info(f"Classification (finance): '{text[:30]}' → FINANCE_QUERY in {duration:.4f}s")
                    result["duration"] = duration
                    return result

                # Consulta de hora
                if self._api._is_time_query(text):
                    location = self._api._extract_location(text)
                    result["task_type"] = TaskType.TIME_QUERY.value
                    result["confidence"] = 0.9
                    result["method"] = "special_time"
                    result["metadata"]["location"] = location
                    self._core._stats["special_classifications"] = self._core._stats.get("special_classifications", 0) + 1
                    self._core._save_to_cache(text, TaskType.TIME_QUERY, 0.9)
                    duration = time.time() - start
                    _log_router(text[:50], TaskType.TIME_QUERY.value, "SPECIAL", 0.9, duration=duration, api_required=False)
                    logger.info(f"Classification (time): '{text[:30]}' → TIME_QUERY in {duration:.4f}s")
                    result["duration"] = duration
                    return result

                # Consulta de búsqueda web
                if self._api._is_web_search_query(text):
                    result["task_type"] = TaskType.WEB_SEARCH.value
                    result["confidence"] = 0.85
                    result["method"] = "special_web_search"
                    result["metadata"]["api_available"] = bool(settings.tavily_api_key or settings.serper_api_key)
                    self._core._stats["special_classifications"] = self._core._stats.get("special_classifications", 0) + 1
                    self._core._save_to_cache(text, TaskType.WEB_SEARCH, 0.85)
                    duration = time.time() - start
                    _log_router(text[:50], TaskType.WEB_SEARCH.value, "SPECIAL", 0.85, duration=duration, api_required=True)
                    logger.info(f"Classification (web_search): '{text[:30]}' → WEB_SEARCH in {duration:.4f}s")
                    result["duration"] = duration
                    return result

        # ═══════════════════════════════════════════════════════
        # ✅ NUEVO: saludo / small-talk — ANTES del LLM.
        # Confirmado con test_classifier_diagnosis.py: el LLM (PASO 2)
        # clasifica "Hola, ¿cómo estás?" como "translation" (confianza
        # 0.95) — un error real del modelo, no de cache. El chequeo de
        # keywords (_is_greeting_or_basic_chat) SÍ acierta (general_chat,
        # 0.9) pero antes solo corría en PASO 3, demotado, y nunca se
        # alcanzaba porque el LLM "tenía éxito" (devolvía *algo*, aunque
        # estuviera mal). Rápido (sin red) y ya demostrado confiable —
        # no tiene sentido dejar que el LLM decida acá.
        # ═══════════════════════════════════════════════════════
        if result["task_type"] == TaskType.GENERAL_CHAT.value and not force_keywords:
            if self._core._is_greeting_or_basic_chat(text):
                result["task_type"] = TaskType.GENERAL_CHAT.value
                result["confidence"] = 0.9
                result["method"] = "greeting_fast_path"
                _log_router(text[:50], result["task_type"], "greeting_fast_path", result["confidence"])
                return result

        # ═══════════════════════════════════════════════════════
        # PASO 2: LLM — clasificador semántico (qwen3:8b), APAGADO por defecto
        # ⚠️ MEDIDO (2026-10-03): este paso se pasaba del límite de 15 s SIEMPRE
        # (el modelo gastaba 68,5 s pensando) ⇒ 15 s perdidos por consulta y, de
        # hecho, nunca clasificaba. Ahora está detrás de
        # `settings.classifier_llm_enabled` (default OFF ⇒ se comporta como hoy:
        # palabras clave, sin los 15 s). Con el interruptor encendido responde en
        # 3-6 s y sólo se aceptan categorías de CÓDIGO (ver `LLM_CLASSIFY_ACEPTADOS`).
        # ═══════════════════════════════════════════════════════
        if (result["task_type"] == TaskType.GENERAL_CHAT.value and not force_keywords
                and getattr(settings, "classifier_llm_enabled", False)):
            # ✅ v0.6.9i: kNN embeddings primero (solo con flag ON; default OFF
            # porque la medición dio 70% con errores de tareas código cercanas)
            knn_task, knn_conf = (self._knn_classify(text)
                                  if getattr(settings, "classifier_knn_enabled", False)
                                  else (None, 0.0))
            if knn_task:
                result["task_type"] = _ensure_str_task_type(knn_task)
                result["confidence"] = knn_conf
                result["method"] = "embeddings_knn"
                self._core._stats["knn_classifications"] = (
                    self._core._stats.get("knn_classifications", 0) + 1
                )
                _log_router(text[:50], result["task_type"], "embeddings_knn",
                            knn_conf, duration=0)
            else:
                # Fallback: LLM cuando el kNN no está seguro (sin pérdida de calidad)
                llm_task, llm_conf = self._classify_by_llm(text)
                if llm_task:
                    result["task_type"] = llm_task
                    result["confidence"] = llm_conf
                    result["method"] = "llm"
                    self._core._stats["llm_classifications"] = (
                        self._core._stats.get("llm_classifications", 0) + 1
                    )

        # ═══════════════════════════════════════════════════════
        # PASO 3: Keywords — fallback degradado si LLM falló o tardó
        # ✅ v0.1.3: Demotado a fallback. Solo activa si PASO 2 no clasificó.
        # ═══════════════════════════════════════════════════════
        if result["task_type"] == TaskType.GENERAL_CHAT.value:
            keyword_task, keyword_conf = self._core._classify_by_keywords(text)
            if keyword_task:
                result["task_type"] = _ensure_str_task_type(keyword_task)
                result["confidence"] = keyword_conf
                result["method"] = "keywords_fallback"
                self._core._stats["keyword_classifications"] += 1

        # ═══════════════════════════════════════════════════════
        # PASO 4: Embeddings — fallback final
        # ✅ v0.1.3: Demotado a último recurso antes de GENERAL_CHAT.
        # ═══════════════════════════════════════════════════════
        if (
            result["task_type"] == TaskType.GENERAL_CHAT.value
            and not force_keywords
            and self._core.use_embeddings
        ):
            embed_task, embed_conf = self._core._classify_by_embeddings(text)
            if embed_task and embed_conf > result["confidence"]:
                result["task_type"] = _ensure_str_task_type(embed_task)
                result["confidence"] = embed_conf
                result["method"] = "embeddings_fallback"
                self._core._stats["embedding_classifications"] += 1

        # Guardar en cache
        try:
            # ✅ FIX v0.1.1: Reconstruir TaskType desde string normalizado
            # Si result["task_type"] ya es string, lo convertimos a enum para el cache
            task_type_str = result["task_type"]
            try:
                task_type_enum = TaskType(task_type_str)
            except ValueError:
                # Si el string no es un TaskType válido, usar GENERAL_CHAT
                task_type_enum = TaskType.GENERAL_CHAT
                result["task_type"] = task_type_enum.value
            self._core._save_to_cache(text, task_type_enum, result["confidence"])
        except Exception as e:
            _log_error(e, "IntentClassifier.classify", extra={"text": text[:50]})
            logger.warning(f"Failed to save classification to cache: {e}")

        duration = time.time() - start
        _log_router(text[:50], result["task_type"], "CLASSIFIER", result["confidence"], duration=duration)
        logger.debug(f"Classification: '{text[:30]}' → {result['task_type']} ({result['method']}, conf={result['confidence']:.2f}) in {duration:.4f}s")
        result["duration"] = duration
        return result

    # ═══════════════════════════════════════════════════════════════
    # ✅ v0.6.9i: KNN embeddings (bge-m3) ANTES del LLM — quita los ~15 s
    # del clasificador LLM en los keyword-miss. El LLM queda como FALLBACK
    # cuando el kNN no está seguro (best < 0.40 o sin margen) → cero pérdida
    # de calidad. Vectores de ejemplo precomputados en data/cache/
    # classifier_knn.json (IntentClassifierCore.TRAINING_EXAMPLES + chat).
    # ═══════════════════════════════════════════════════════════════
    def _knn_load_vectors(self) -> Optional[Dict[str, Any]]:
        global _KNN_LOADED
        if _KNN_LOADED is not None:
            return _KNN_LOADED
        with _KNN_LOCK:
            if _KNN_LOADED is not None:
                return _KNN_LOADED
            try:
                cache = Path("data/cache/classifier_knn.json")
                if cache.exists():
                    _KNN_LOADED = _json.loads(cache.read_text(encoding="utf-8"))
            except Exception as e:
                logging.getLogger(__name__).debug(f"[KNN] load cache error: {e}")
                _KNN_LOADED = {}
        return _KNN_LOADED

    @staticmethod
    def _knn_cos(a: List[float], b: List[float]) -> float:
        try:
            dot = sum(x * y for x, y in zip(a, b))
            na = _math.sqrt(sum(x * x for x in a))
            nb = _math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na and nb else 0.0
        except Exception:
            return 0.0

    def _knn_classify(self, text: str) -> Tuple[Optional[str], float]:
        """kNN sobre ejemplos por tarea → (task, conf) o (None, 0)."""
        data = self._knn_load_vectors()
        if not data:
            return None, 0.0
        try:
            from src.memory.embedding_model_singleton import get_embedding_model
            vec = get_embedding_model().embed((text or "").lower())
        except Exception:
            return None, 0.0
        if not vec:
            return None, 0.0
        best_sim, best_task, second = -1.0, None, -1.0
        for task, items in data.items():
            for _q, v in items:
                sim = self._knn_cos(vec, v)
                if sim > best_sim:
                    second = best_sim
                    best_sim, best_task = sim, task
                elif sim > second:
                    second = sim
        if best_task and best_sim >= 0.40 and (best_sim - second >= 0.02 or best_sim >= 0.50):
            return best_task, round(min(0.9, 0.45 + best_sim * 0.5), 2)
        return None, 0.0

    def _classify_by_llm(self, text: str) -> Tuple[Optional[str], float]:
        """
        PASO 4: clasificación semántica vía el modelo LLM por defecto (qwen3:8b),
        detrás de `settings.classifier_llm_enabled` (default OFF).

        Usa httpx síncrono directo a la API de Ollama para no introducir
        dependencias asíncronas en el pipeline. Timeout 15 s — si falla (modelo
        frío o caído) degrada a palabras clave sin bloquear el pipeline.
        Los dos defectos medidos y sus porqués están en las constantes
        `LLM_CLASSIFY_ACEPTADOS` / `LLM_CLASSIFY_CONF_MINIMA` de arriba.
        """
        try:
            import httpx
            import json as _json
            import re as _re

            supported = ", ".join(
                t.value for t in TaskType if t != TaskType.GENERAL_CHAT
            )
            prompt = (
                "You are a concise intent classifier. Classify the query below.\n"
                f"Available categories: {supported}\n"
                f'Query: "{text}"\n'
                "Reply with ONLY this JSON — no markdown, no explanation:\n"
                '{"task_type": "<category>", "confidence": <0.0-1.0>}'
            )
            base_url = getattr(settings, "ollama_base_url", "http://localhost:11434")
            payload = {
                "model": getattr(settings, "llm_default_model", "qwen3:8b"),
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                # ✅ Defecto 1: sin esto el modelo "piensa" y se pasa del límite.
                "think": False,
            }
            # ✅ 2026-08-25: 8s era insuficiente con modelo frío (load >20s) → 15s
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(f"{base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data.get("message", {}).get("content", "").strip()
            content = _re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=_re.MULTILINE).strip()
            parsed = _json.loads(content)
            task_type_str = str(parsed.get("task_type", "")).strip()
            confidence = float(parsed.get("confidence", 0.6))
            TaskType(task_type_str)  # ValueError si no es un valor válido del enum
            # ✅ Defecto 2 (medido): sólo se acepta para las categorías donde el
            # modelo acierta de forma consistente; el resto (charla, saludos,
            # "traducción" inventada) se deja al camino normal.
            if task_type_str not in LLM_CLASSIFY_ACEPTADOS:
                logger.debug(
                    f"[classifier] categoría del LLM descartada ({task_type_str}, "
                    f"conf={confidence:.2f}) — no está entre las aceptadas"
                )
                return None, 0.0
            if confidence < LLM_CLASSIFY_CONF_MINIMA:
                logger.debug(
                    f"[classifier] categoría del LLM con confianza baja "
                    f"({task_type_str}, conf={confidence:.2f} < {LLM_CLASSIFY_CONF_MINIMA})"
                )
                return None, 0.0
            return task_type_str, min(max(confidence, 0.0), 1.0)

        except Exception as e:
            # ✅ AUDITORÍA 2026-08-25: un fallo de modelo (p. ej. 404 model not found)
            # es un problema de configuración que debe verse en WARNING, no ocultarse
            # en DEBUG — degrada la clasificación en silencio.
            err_str = str(e)
            if "404" in err_str or "not found" in err_str.lower() or "model" in err_str.lower():
                _log_error(e, "classifier.llm_fallback", extra={"query_preview": text[:80]})
                logger.warning(f"LLM classify fallback: modelo no disponible ({err_str[:120]}) — usando keywords")
            else:
                logger.debug(f"LLM classify fallback (non-fatal): {e}")
            return None, 0.0

    def classify_batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        """
        Clasifica múltiples textos en batch.

        Args:
            texts: Lista de textos a clasificar

        Returns:
            Lista de resultados de clasificación
        """
        start = time.time()
        results = [self.classify(text) for text in texts]
        duration = time.time() - start
        logger.info(f"Batch classification: {len(texts)} texts in {duration:.4f}s")
        return results

    # ═══════════════════════════════════════════════════════════
    # ENTRENAMIENTO
    # ═══════════════════════════════════════════════════════════
    def train(self, examples: Dict[str, List[str]], save_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Entrena clasificador con ejemplos.

        Args:
            examples: Dict {task_type: [ejemplos]}
            save_path: Ruta para guardar modelo

        Returns:
            Stats de entrenamiento

        Example:
            >>> examples = {
            ...     "code_generation": ["crea una función", "escribe código"],
            ...     "general_chat": ["hola", "buenos días"]
            ... }
            >>> stats = classifier.train(examples)
        """
        return self._core.train(examples, save_path)

    # ═══════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════
    def get_supported_types(self) -> List[str]:
        """
        Retorna lista de tipos de tarea soportados.

        Returns:
            Lista de valores TaskType
        """
        return self._core.get_supported_types()

    def clear_cache(self) -> None:
        """
        Limpia cache de clasificaciones.

        Logging:
        - Registra limpieza de cache
        - Count de entradas eliminadas
        """
        self._core.clear_cache()
        self._api.clear_location_cache()
        logger.info("IntentClassifier all caches cleared")

    def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas del clasificador.

        Returns:
            Dict con estadísticas completas
        """
        core_stats = self._core.get_stats()
        return {
            **core_stats,
            "api_integration_enabled": self._api.enable_api_integration,
            "location_cache_size": len(self._api._location_cache),
        }

    def set_api_integration(self, enabled: bool) -> None:
        """
        Habilita/deshabilita integración con APIs externas.

        Args:
            enabled: True para habilitar, False para deshabilitar
        """
        self._api.set_api_integration(enabled)
        logger.info(f"IntentClassifier API integration: {'enabled' if enabled else 'disabled'}")

    # ═══════════════════════════════════════════════════════════
    # MÉTODOS DE ACCESO A APIs (expuestos para testing/uso externo)
    # ═══════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════
    # CLASIFICACIÓN CONTEXTUAL (CAAC Fase 0)
    # ═══════════════════════════════════════════════════════════
    def classify_with_context(
        self,
        text: str,
        context: "SessionContext",  # type: ignore[name-defined]
        force_keywords: bool = False,
    ) -> Dict[str, Any]:
        """
        Wrapper contextual sobre classify().

        Aplica boost de last_intent cuando la confianza base es baja
        y el texto parece una continuación del turno anterior.

        Reglas de boost (aplicadas DESPUÉS del classify() base):
        1. Si confidence < 0.65 y context.is_continuation(text):
           → task_type = last_intent, confidence = max(original, 0.72)
           → method = "context_continuation"
        2. Si confidence < 0.50 y turns > 2 (baja confianza multi-turno):
           → task_type = context.dominant_intent() o last_intent
           → confidence = 0.60
           → method = "context_inferred"
           → metadata["inferred_from_context"] = True

        NO modifica IntentClassifierCore. NO altera cache del core.
        Guarda en cache contextual propio (sin colisión con cache stateless).

        Args:
            text:          Texto a clasificar
            context:       SessionContext del turno actual
            force_keywords: Pasar a classify() base

        Returns:
            Dict idéntico al de classify() con campos adicionales en metadata:
            - context_boost_applied: bool
            - original_task_type: str (si hubo boost)
            - original_confidence: float (si hubo boost)
        """
        import time as _time
        # Importar aquí para evitar circular import en tiempo de módulo
        try:
            from src.domain.context import SessionContext as _SC
        except ImportError:
            # Fallback si se llama fuera del proyecto (tests unitarios)
            pass

        start = _time.time()

        # ── Paso base: clasificación stateless normal ────────────────────────
        result = self.classify(text, force_keywords=force_keywords)
        base_task = result["task_type"]
        base_conf = result["confidence"]

        # Sin last_intent no hay contexto que aplicar
        if not context.last_intent:
            result["metadata"]["context_boost_applied"] = False
            return result

        boosted = False

        # ── Regla 1: continuación clara + confianza baja ─────────────────────
        if base_conf < 0.65 and context.is_continuation(text):
            result["metadata"]["original_task_type"] = base_task
            result["metadata"]["original_confidence"] = base_conf
            # ✅ FIX v0.1.1: Normalizar last_intent por si viene como Enum
            result["task_type"] = _ensure_str_task_type(context.last_intent)
            result["confidence"] = round(max(base_conf, 0.72), 4)
            result["method"] = "context_continuation"
            boosted = True
            logger.info(
                f"[CTX] Continuation boost: '{text[:40]}' "
                f"{base_task}({base_conf:.2f}) → "
                f"{result['task_type']}(0.72) "
                f"[turns={context.turns}]"
            )

        # ── Regla 2: baja confianza en sesión larga ──────────────────────────
        elif base_conf < 0.50 and context.turns > 2:
            dominant = context.dominant_intent() or context.last_intent
            result["metadata"]["original_task_type"] = base_task
            result["metadata"]["original_confidence"] = base_conf
            # ✅ FIX v0.1.1: Normalizar dominant por si viene como Enum
            result["task_type"] = _ensure_str_task_type(dominant)
            result["confidence"] = 0.60
            result["method"] = "context_inferred"
            result["metadata"]["inferred_from_context"] = True
            boosted = True
            logger.info(
                f"[CTX] Multi-turn infer: '{text[:40]}' "
                f"{base_task}({base_conf:.2f}) → "
                f"{result['task_type']}(0.60) "
                f"[turns={context.turns}]"
            )

        result["metadata"]["context_boost_applied"] = boosted
        result["duration"] = _time.time() - start
        return result


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS
# ═══════════════════════════════════════════════════════════════
# | Versión | Cambio                                      | Razón                                     |
# |---------|---------------------------------------------|-------------------------------------------|
# | v0.1.0  | Guard de código en PASO 1                   | Evita código→finance_query                |
# | v0.1.0  | Detección de WEB_SEARCH                     | Nueva categoría especial                  |
# | v0.1.1  | Helper _ensure_str_task_type()              | Normaliza Enum/str/None a string          |
# | v0.1.1  | PASO 2: _ensure_str_task_type(keyword_task) | Fix AttributeError 'str' has no 'value'   |
# | v0.1.1  | PASO 3: _ensure_str_task_type(embed_task)   | Fix mismo error en embeddings             |
# | v0.1.1  | classify_with_context: normalizar intents   | Fix mismo error en boost contextual       |
# | v0.1.1  | Cache: reconstruir TaskType desde string    | Garantiza enum válido para _save_to_cache |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
