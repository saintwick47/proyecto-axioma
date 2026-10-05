#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.7 — SmartRouter Principal (Facade)
# Ruta: /ruta/a/axioma/src/router/smart_router.py
# Autor: SaintWick
# Versión: 1.0.7 (v4.1.0 — FASE 1 Resiliencia)
# Propósito: Orquestador de ruteo. Delega lógica pura a smart_router_core
#            y llamadas de red a smart_router_api.
# Cambios v1.0.7 (FASE 1):
#   - ✅ FIX: route_and_execute() usa copia de contexto (evita State Pollution).
#   - ✅ FIX: route_and_execute() captura Exception genérica (evita caída descontrolada).
# ═══════════════════════════════════════════════════════════════
import logging
import time
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from src.domain.exceptions import LLMError
from src.domain.types import TaskType
from src.llm.router import ModelRouter
from .cache import RouterCache, get_router_cache, reset_router_cache
from .classifier import IntentClassifier
from .smart_router_core import (
    CODE_MODEL,
    BoundedMetrics,
    FallbackStrategy,
    RoutingDecisionCache,
    RoutingResult,
    CodeQualityAnalyzer,
    QueryNormalizer,
    APIRequirementEvaluator,
    QUALITY_WEIGHTS,
    QUALITY_THRESHOLDS,
)
from .smart_router_api import RouterAPIExecutor, API_FALLBACK_PROMPT

# ── PromptOptimizer — import seguro (Fase 5) ────────────────────────────────
try:
    from src.prompts.prompt_optimizer import get_prompt_optimizer
    _OPTIMIZER_AVAILABLE = True
except ImportError:
    get_prompt_optimizer = None
    _OPTIMIZER_AVAILABLE = False

# ── FeedbackLoop — import seguro (Fase 3) ───────────────────────────────────
try:
    from src.core.feedback_loop import FeedbackLoop, get_feedback_loop_sync
    _FEEDBACK_AVAILABLE = True
except ImportError:
    FeedbackLoop = None
    _FEEDBACK_AVAILABLE = False

# ─── Logging opcional via DetailedLogger ────────────────────────────────────
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_router(
    query: str, task_type: str, model: str, confidence: float,
    cache_hit: bool = False, duration: float = None, api_required: bool = False,
) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_router_decision(query, task_type, model, confidence,
                                    cache_hit, duration, api_required)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 smart_router.py:76] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 smart_router.py:87] excepción degradada (intencional): {_d2e}")


# ═══════════════════════════════════════════════════════════════
# SMART ROUTER — FACADE
# ═══════════════════════════════════════════════════════════════
class SmartRouter:
    """
    Orquestador de ruteo multicapa (Facade).

    Delega responsabilidades siguiendo SRP:
    - CodeQualityAnalyzer: Validación de calidad de código.
    - QueryNormalizer: Normalización de consultas y formato directo.
    - APIRequirementEvaluator: Evaluación de requisitos de API.
    - RouterAPIExecutor: Llamadas a APIs externas y manejo de red.
    """

    # ── Estado compartido entre instancias (class-level) ────────────────────
    _routing_cache: Optional[RoutingDecisionCache] = None
    _metrics: Optional[BoundedMetrics] = None
    _cache_lock = threading.Lock()

    # ────────────────────────────────────────────────────────────────────────
    # INIT
    # ────────────────────────────────────────────────────────────────────────
    def __init__(
        self,
        cache_ttl: int = 300,
        cache_max_size: int = 1000,
        classifier_threshold: float = settings.validation_threshold,
        enable_cache: bool = True,
        max_tokens: int = 512,
        routing_cache_size: int = 500,
    ) -> None:
        self.enable_cache         = enable_cache
        self.max_tokens           = max_tokens
        self.classifier_threshold = classifier_threshold

        # ── Componentes Delegados (SRP) ──────────────────────────────────────
        self._fallback_strategy  = FallbackStrategy()
        self._quality_analyzer   = CodeQualityAnalyzer(QUALITY_WEIGHTS, QUALITY_THRESHOLDS)
        self._query_normalizer   = QueryNormalizer()
        self._api_evaluator      = APIRequirementEvaluator()
        self._api_executor       = RouterAPIExecutor(timeout=settings.ollama_timeout)

        # Dependencias con lazy init (properties)
        self._classifier:   Optional[IntentClassifier]   = None
        self._model_router: Optional[ModelRouter]         = None

        # Caché de respuestas (opcional)
        self._cache: Optional[RouterCache] = None
        self._routing_cache: Optional[RoutingDecisionCache] = None
        self._bounded_metrics: Optional[BoundedMetrics] = None

        if enable_cache:
            self._cache           = get_router_cache(ttl_seconds=cache_ttl, max_size=cache_max_size)
            self._routing_cache   = self._get_routing_cache(routing_cache_size)
            self._bounded_metrics = self._get_metrics()

        # PromptOptimizer — lazy (Fase 5)
        self._prompt_optimizer: Optional[Any] = None
        if _OPTIMIZER_AVAILABLE:
            try:
                self._prompt_optimizer = get_prompt_optimizer()
            except Exception as e:
                logger.warning(f"PromptOptimizer no disponible: {e}")

        # FeedbackLoop — lazy, solo si está disponible
        self._feedback_loop: Optional[Any] = None
        if _FEEDBACK_AVAILABLE:
            try:
                self._feedback_loop = get_feedback_loop_sync()
            except Exception as e:
                logger.warning(f"FeedbackLoop no disponible: {e}")

        logger.info(
            f"SmartRouter v1.0.7 (Facade) — model={CODE_MODEL}, "
            f"cache={enable_cache}, threshold={classifier_threshold}"
        )

    # ── Singletons de clase ──────────────────────────────────────────────────
    @classmethod
    def _get_routing_cache(cls, size: int = 500) -> RoutingDecisionCache:
        with cls._cache_lock:
            if cls._routing_cache is None:
                cls._routing_cache = RoutingDecisionCache(max_size=size)
            return cls._routing_cache

    @classmethod
    def _get_metrics(cls) -> BoundedMetrics:
        with cls._cache_lock:
            if cls._metrics is None:
                cls._metrics = BoundedMetrics(max_entries=1000)
            return cls._metrics

    # ── Lazy properties ──────────────────────────────────────────────────────
    @property
    def classifier(self) -> IntentClassifier:
        if self._classifier is None:
            self._classifier = IntentClassifier(threshold=self.classifier_threshold)
        return self._classifier

    @property
    def model_router(self) -> ModelRouter:
        if self._model_router is None:
            self._model_router = ModelRouter()
        return self._model_router

    # ────────────────────────────────────────────────────────────────────────
    # CACHÉ HELPERS
    # ────────────────────────────────────────────────────────────────────────
    def _get_from_cache(self, query: str, start_time: float) -> Optional[RoutingResult]:
        """Busca en L1 (RoutingDecisionCache) y L2 (RouterCache)."""
        latency = lambda: (time.time() - start_time) * 1000  # noqa

        # L1
        if self._routing_cache:
            cached = self._routing_cache.get(query)
            if cached:
                ms = latency()
                logger.debug(f"L1 HIT: {query[:30]!r}")
                _log_router(query[:50], cached["task_type"], cached["model"],
                            cached["confidence"], cache_hit=True, duration=ms / 1000)
                return RoutingResult.from_cache(cached, ms)

        # L2
        if self._cache:
            cached = self._cache.get(query)
            if cached:
                ms = latency()
                logger.debug(f"L2 HIT: {query[:30]!r}")
                if self._routing_cache:
                    self._routing_cache.set(query, cached)
                _log_router(query[:50], cached["task_type"], cached["model"],
                            cached["confidence"], cache_hit=True, duration=ms / 1000)
                return RoutingResult.from_cache(cached, ms)

        return None

    def _store_in_cache(self, query: str, result: RoutingResult, confidence: float) -> None:
        """Persiste la decisión en ambas capas de caché si la confianza lo justifica."""
        if confidence < self.classifier_threshold:
            return
        result_dict = result.to_dict()
        if self._cache:
            self._cache.set(query, result_dict, ttl_seconds=self._cache.ttl_seconds)
        if self._routing_cache:
            self._routing_cache.set(query, result_dict)

    # ────────────────────────────────────────────────────────────────────────
    # ROUTE
    # ────────────────────────────────────────────────────────────────────────
    def route(self, query: str, context: Optional[Dict[str, Any]] = None) -> RoutingResult:
        """
        Clasifica la query y devuelve una decisión de ruteo.
        Consulta caché primero (L1 → L2). Si hay miss, clasifica con
        IntentClassifier, selecciona modelo y construye RoutingResult.
        """
        t0 = time.time()
        _log_router(query[:50], "PENDING", "pending", 0.0)

        # ── Caché ────────────────────────────────────────────────────────────
        cached = self._get_from_cache(query, t0)
        if cached:
            return cached

        # ── Clasificación ────────────────────────────────────────────────────
        clf           = self.classifier.classify(query)
        task_type     = TaskType(clf["task_type"])
        confidence    = clf["confidence"]
        method        = clf["method"]

        # ── A/B routing ponderado (Fase 5) ─────────────────────────────────────
        if self._prompt_optimizer is not None:
            try:
                ab_version = self._prompt_optimizer.select_ab_version(
                    "router", task_type.value
                )
                logger.debug(f"A/B version: {task_type.value} → {ab_version}")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 smart_router.py:267] excepción degradada (intencional): {_d2e}")

        # ── Delegación a Evaluadores (SRP) ────────────────────────────────────
        api_required, available_apis, api_fallback = self._api_evaluator.requires_api(task_type.value)
        requires_direct = self._query_normalizer.requires_direct_format(query)
        model_decision  = self.model_router.route(task_type.value, context)

        # ── Ajustes para generación de código ────────────────────────────────
        temperature  = model_decision.temperature
        code_level   = 2
        quality_score = 0.0

        if task_type == TaskType.CODE_GENERATION:
            code_level = (
                settings.get_code_level(self.max_tokens // 5)
                if hasattr(settings, "get_code_level") else 2
            )
            quality_score = (
                settings.get_code_quality_threshold(code_level)
                if hasattr(settings, "get_code_quality_threshold") else 0.60
            )
            temperature        = min(temperature, 0.3)
            model_decision.model = CODE_MODEL

            prev_quality = (context or {}).get("previous_quality")
            if self._fallback_strategy.should_fallback(
                estimated_lines=self.max_tokens // 5,
                estimated_complexity="medium",
                previous_quality=prev_quality,
            ):
                logger.warning(
                    f"⚠️ Calidad previa crítica ({prev_quality:.2f}) — "
                    "revisar output manualmente"
                )

        if api_required and task_type.value in (
            TaskType.FINANCE_QUERY.value, TaskType.TIME_QUERY.value
        ):
            temperature = min(temperature, 0.3)

        # ── Resultado ────────────────────────────────────────────────────────
        latency = (time.time() - t0) * 1000
        result = RoutingResult(
            task_type=task_type,
            model=model_decision.model,
            temperature=temperature,
            confidence=confidence,
            method=method,
            fallback_chain=model_decision.fallback_chain,
            latency_ms=latency,
            api_required=api_required,
            max_tokens=min(self.max_tokens, 300) if requires_direct else self.max_tokens,
            presentation_style="direct" if requires_direct else "explanatory",
            code_level=code_level,
            quality_score=quality_score,
        )

        self._store_in_cache(query, result, confidence)
        if self._bounded_metrics:
            self._bounded_metrics.record(task_type.value, model_decision.model, latency, confidence)

        _log_router(query[:50], task_type.value, model_decision.model,
                    confidence, duration=latency / 1000, api_required=api_required)

        label = (f"APIs: {available_apis or api_fallback}" if api_required
                 else f"{task_type.value}, conf={confidence:.2f}")
        logger.info(f"Routed: {query[:30]!r} → {result.model} ({label})")

        if self._feedback_loop is not None:
            try:
                import asyncio
                from src.core.feedback_loop import ImplicitSignal
                coro = self._feedback_loop.record_implicit(
                    session_id=self._feedback_loop.session_id,
                    task_type=task_type.value,
                    # ✅ v0.6.9v (ISSUE-033): `agent_role` es REQUERIDO por
                    # record_implicit y no se pasaba → TypeError silenciado
                    # (además de ImplicitSignal.ROUTED, que no existía): la señal
                    # de ruteo nunca se registraba. Acá el emisor es el router.
                    agent_role="ModelRouter",
                    query=query,
                    response=result.model,
                    signal=ImplicitSignal.ROUTED,
                )
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.ensure_future(coro, loop=loop)
                except RuntimeError:
                    asyncio.run(coro)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 smart_router.py:352] excepción degradada (intencional): {_d2e}")

        return result

    # ────────────────────────────────────────────────────────────────────────
    # ROUTE AND EXECUTE
    # ────────────────────────────────────────────────────────────────────────
    async def route_and_execute(
        self,
        query: str,
        system_prompt: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Clasifica la query y ejecuta la llamada correspondiente (API o LLM).
        """
        t0 = time.time()
        routing = self.route(query, context)

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 1: Aislamiento de Contexto (State Pollution).
        # Se clona el diccionario para evitar que mutaciones internas
        # (api_fallback, presentation_style) contaminen el contexto
        # del llamador en futuras interacciones.
        # ═══════════════════════════════════════════════════════════════
        context = (context or {}).copy()
        context["presentation_style"] = routing.presentation_style

        effective_prompt = system_prompt
        if routing.presentation_style == "direct":
            effective_prompt = (system_prompt or "") + (
                "\n⚠️ RESPONDE DE FORMA CONCISA: Máximo 3 párrafos. Sin preámbulos."
            )

        # ── Intento de API externa (Delegado a RouterAPIExecutor) ──────────────
        if routing.api_required:
            api_result = await self._api_executor.execute_api_call(routing.task_type.value, query, **context)
            if api_result.get("success"):
                _log_router(query[:50], routing.task_type.value, "API",
                            routing.confidence, duration=time.time() - t0, api_required=True)
                return {
                    "success":            True,
                    "content":            api_result["content"],
                    "source":             "api",
                    "task_type":          routing.task_type.value,
                    "api_data":           api_result.get("data"),
                    "timestamp":          api_result.get("timestamp"),
                    "cache_hit":          routing.cache_hit,
                    "api_required":       True,
                    "max_tokens":         routing.max_tokens,
                    "presentation_style": routing.presentation_style,
                }

            logger.warning(f"API falló ({api_result.get('error')}), usando LLM con fallback anti-código")
            context["api_fallback"] = True
            context["api_error"]    = api_result.get("error")
            effective_prompt = (effective_prompt or "") + API_FALLBACK_PROMPT

        # ── Llamada al LLM ───────────────────────────────────────────────────
        try:
            response = self.model_router.execute_with_fallback(
                task_type=routing.task_type.value,
                prompt=query,
                system=effective_prompt,
                context=context,
            )

            # Análisis de calidad (Delegado a CodeQualityAnalyzer)
            quality_analysis = None
            if routing.task_type == TaskType.CODE_GENERATION:
                quality_analysis = self._quality_analyzer.analyze_code_quality(response.content, self.max_tokens)
                self._quality_analyzer.log_quality_alerts(quality_analysis)
                logger.info(
                    f"Code quality: {quality_analysis['quality_score']:.2f} "
                    f"(level {quality_analysis['code_level']})"
                )

            duration = time.time() - t0
            _log_router(query[:50], routing.task_type.value, response.model,
                        routing.confidence, duration=duration, api_required=routing.api_required)

            result = {
                "success":            True,
                "content":            response.content,
                "model":              response.model,
                "source":             "llm",
                "task_type":          routing.task_type.value,
                "temperature":        routing.temperature,
                "confidence":         routing.confidence,
                "latency_ms":         routing.latency_ms + response.total_duration * 1000,
                "cache_hit":          routing.cache_hit,
                "api_required":       routing.api_required,
                "max_tokens":         routing.max_tokens,
                "presentation_style": routing.presentation_style,
            }
            if quality_analysis:
                result["quality_analysis"] = quality_analysis
            return result

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 1: Fallback asíncrono completo.
        # Antes solo capturaba LLMError. Cualquier otra excepción de red
        # o de orquestación causaba una caída descontrolada. Ahora captura
        # Exception para garantizar degradación graceful y retorno seguro.
        # ═══════════════════════════════════════════════════════════════
        except Exception as e:
            logger.error(f"LLM/Ejecución falló: {e}\n{traceback.format_exc()}")
            _log_error(e, "SmartRouter.route_and_execute", extra={"query": query[:50]})
            _log_router(query[:50], routing.task_type.value, routing.model,
                        routing.confidence, duration=time.time() - t0,
                        api_required=routing.api_required)
            return {
                "success":            False,
                "error":              str(e),
                "task_type":          routing.task_type.value,
                "model":              routing.model,
                "api_required":       routing.api_required,
                "presentation_style": routing.presentation_style,
            }

    # ────────────────────────────────────────────────────────────────────────
    # MÉTRICAS Y STATS
    # ────────────────────────────────────────────────────────────────────────
    def get_feedback_stats(self) -> Dict[str, Any]:
        if self._feedback_loop is None:
            return {"available": False}
        try:
            return {"available": True, **self._feedback_loop.get_global_stats()}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def get_metrics(self) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {}
        if self._bounded_metrics:
            metrics.update(self._bounded_metrics.get_all())
        if self._cache:
            metrics["cache"] = self._cache.get_stats()
        if self._routing_cache:
            metrics["routing_cache"] = self._routing_cache.get_stats()
        metrics["classifier"]    = self.classifier.get_stats()
        metrics["model_router"]  = self.model_router.get_metrics()
        return metrics

    def get_api_status(self) -> Dict[str, Any]:
        """Estado de configuración de APIs externas (Delegado a APIRequirementEvaluator)."""
        return self._api_evaluator.get_api_status()

    def get_stats(self) -> Dict[str, Any]:
        threshold_value = getattr(
            self.classifier, "threshold",
            getattr(
                self.classifier, "classifier_core",
                type("_", (), {"threshold": self.classifier_threshold})()
            ).threshold,
        )
        return {
            "metrics":              self.get_metrics(),
            "supported_tasks":      len(self.classifier.get_supported_types()),
            "cache_enabled":        self.enable_cache,
            "classifier_threshold": threshold_value,
            "max_tokens":           self.max_tokens,
            "api_status":           self.get_api_status(),
            "total_requests":       self._bounded_metrics.get_total_count() if self._bounded_metrics else 0,
            "fallback_strategy":    "enabled" if self._fallback_strategy else "disabled",
            "code_model":           CODE_MODEL,
        }

    # ────────────────────────────────────────────────────────────────────────
    # CACHE MANAGEMENT
    # ────────────────────────────────────────────────────────────────────────
    def warm_cache(self) -> int:
        _log_router("WARM_CACHE", "CACHE", "warming", 0.0)
        t0 = time.time()

        fixtures: List[Tuple[str, str]] = [
            ("hola",                  TaskType.GENERAL_CHAT.value),
            ("buenos días",           TaskType.GENERAL_CHAT.value),
            ("ayuda",                 TaskType.GENERAL_CHAT.value),
            ("escribe código",        TaskType.CODE_GENERATION.value),
            ("debug",                 TaskType.CODE_DEBUGGING.value),
            ("busca información",     TaskType.WEB_SEARCH.value),
            ("resume",                TaskType.SUMMARIZATION.value),
            ("analiza",               TaskType.DATA_ANALYSIS.value),
            ("clima en Buenos Aires", TaskType.WEATHER_QUERY.value),
            ("últimas noticias",      TaskType.NEWS_QUERY.value),
            ("cotización del dólar",  TaskType.FINANCE_QUERY.value),
            ("qué hora es",           TaskType.TIME_QUERY.value),
        ]

        count = sum(
            1 for query, expected in fixtures
            if self.route(query).task_type.value == expected
        )

        _log_router("WARM_CACHE", "CACHE", "warmed", 1.0, duration=time.time() - t0)
        logger.info(f"Cache warmed: {count}/{len(fixtures)} hits")
        return count

    def clear_cache(self) -> None:
        if self._cache:
            self._cache.clear()
        if self._routing_cache:
            self._routing_cache.clear()
        self.classifier.clear_cache()
        logger.info("SmartRouter: cache limpiada")

    def set_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens
        logger.info(f"SmartRouter max_tokens → {max_tokens}")

    async def cleanup(self) -> None:
        """Libera recursos asincrónicos (API executor)."""
        await self._api_executor.cleanup()
        logger.info("SmartRouter: Recursos liberados vía RouterAPIExecutor")

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v1.0.7 (FASE 1)
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Header               | ✅ v1.0.7 changelog (FASE 1)              | Trazabilidad              |
# | route_and_execute()  | ✅ context = context.copy()               | Evitar State Pollution    |
# | route_and_execute()  | ✅ except Exception as e (era LLMError)   | Evitar caída descontrolada|
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
