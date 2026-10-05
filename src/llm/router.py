#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/llm/router.py
# AXIOMA v0.0.9 — Router de Modelos LLM
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Selección inteligente de modelo por tarea
# ✅ v0.0.9: FIX AUDITORÍA — _decision_cache sin límite. cache_key ya no
#   incluye hash(str(context)) (route() no usa context para la decisión,
#   solo fragmentaba el cache en entradas idénticas que nunca se
#   limpiaban) + OrderedDict con LRU acotado (256) como defensa adicional.
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from collections import OrderedDict
import logging
import time

from config.settings import settings
from src.domain.types import TaskType
from src.domain.exceptions import LLMError, RouterError
from .client import OllamaClient
from .config import LLMConfig, ModelConfig

logger = logging.getLogger(__name__)

# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

def _log_router(query: str, task_type: str, model: str, confidence: float,
                duration: float = None, cache_hit: bool = False):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_router_decision(query=query, task_type=task_type,
                                    model_selected=model, confidence=confidence,
                                    cache_hit=cache_hit, duration=duration)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 router.py:48] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str):
    if not LOGGER_AVAILABLE:
        logger.error(f"llm_router - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context,
                         function_name=f"llm_router.{context}")
    except Exception:
        logger.error(f"llm_router - {context}: {error}")


@dataclass
class RoutingDecision:
    """Decisión de ruteo con metadatos."""
    model: str
    task_type: str
    temperature: float
    confidence: float
    fallback_chain: List[str]
    reasoning: str
    timestamp: float


class ModelRouter:
    """
    Router inteligente para selección de modelos LLM.

    Features:
    - Selección por tipo de tarea
    - Fallback automático en cascada
    - Cache de decisiones recientes
    - Métricas de rendimiento por modelo

    Example:
        >>> router = ModelRouter()
        >>> decision = router.route("code_generation")
        >>> client = OllamaClient(decision.model)
        >>> response = client.generate(prompt, temperature=decision.temperature)
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        """
        Inicializa router de modelos.

        Args:
            config: Configuración LLM (crea nueva si None)
        """
        self.config = config or LLMConfig()
        self._client_cache: Dict[str, OllamaClient] = {}
        self._metrics: Dict[str, Dict[str, Any]] = {}
        # ✅ FIX AUDITORÍA: antes dict plano sin max_size ni evicción, solo
        # TTL chequeado en lectura (las entradas vencidas nunca se
        # eliminaban). El cache_key incluía hash(str(context)) pese a que
        # route() no usa `context` para nada en la decisión — con cualquier
        # caller que pase un context que varíe por request (ej.
        # smart_router.py pasa `previous_quality`, que cambia todo el
        # tiempo), esto generaba una entrada nueva por request, sin límite,
        # en un proceso de vida larga (systemd). Ahora: cache_key por
        # task_type solamente (lo único que de verdad determina la
        # decisión) + OrderedDict con LRU acotado como defensa adicional.
        self._decision_cache: "OrderedDict[str, RoutingDecision]" = OrderedDict()
        self._decision_cache_max_size: int = 256
        self._cache_ttl: int = 300  # 5 minutos

        logger.info("ModelRouter initialized")

    def route(self, task_type: str, context: Optional[Dict[str, Any]] = None) -> RoutingDecision:
        """
        Determina el mejor modelo para una tarea.

        Args:
            task_type: Tipo de tarea (string o TaskType)
            context: Contexto adicional para decisión

        Returns:
            RoutingDecision con modelo y configuración

        Example:
            >>> decision = router.route("code_generation")
            >>> print(f"Model: {decision.model}, Temp: {decision.temperature}")
        """
        start_time = time.time()

        # Normalize task_type
        if isinstance(task_type, TaskType):
            task_type = task_type.value

        # ✅ FIX AUDITORÍA: cache_key ya no incluye context — get_model_for_task()
        # (abajo) depende solo de task_type, así que hashear context solo
        # fragmentaba el cache en entradas funcionalmente idénticas que
        # nunca se limpiaban. `context` se mantiene en la firma por
        # compatibilidad con callers existentes (ej. smart_router.py).
        cache_key = task_type
        if cache_key in self._decision_cache:
            cached = self._decision_cache[cache_key]
            if time.time() - cached.timestamp < self._cache_ttl:
                self._decision_cache.move_to_end(cache_key)
                logger.debug(f"Routing cache hit: {task_type}")
                return cached
            # Vencida — sacarla en vez de dejarla acumulada sin usar
            del self._decision_cache[cache_key]

        # Get model config for task
        model_config = self.config.get_model_for_task(task_type)

        # Get fallback chain
        fallback_chain = self.config.get_fallback_chain(model_config.name)

        # Build decision
        decision = RoutingDecision(
            model=model_config.name,
            task_type=task_type,
            temperature=model_config.temperature,
            confidence=0.9,
            fallback_chain=fallback_chain,
            reasoning=f"Selected {model_config.name} for {task_type} (priority={model_config.priority})",
            timestamp=time.time(),
        )

        # Cache decision
        self._decision_cache[cache_key] = decision
        self._decision_cache.move_to_end(cache_key)
        while len(self._decision_cache) > self._decision_cache_max_size:
            self._decision_cache.popitem(last=False)

        # Record metrics
        elapsed = time.time() - start_time
        self._record_routing_metric(task_type, model_config.name, elapsed)
        _log_router(query=task_type, task_type=task_type, model=decision.model,
                    confidence=decision.confidence, duration=elapsed,
                    cache_hit=False)

        logger.info(f"Routed {task_type} → {decision.model} (temp={decision.temperature})")

        return decision

    def route_with_fallback(
        self,
        task_type: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[RoutingDecision, List[str]]:
        """
        Ruta con cadena de fallback completa.

        Args:
            task_type: Tipo de tarea
            context: Contexto adicional

        Returns:
            Tuple (decisión principal, lista de fallbacks)
        """
        decision = self.route(task_type, context)
        return decision, decision.fallback_chain[1:] if len(decision.fallback_chain) > 1 else []

    def get_client(self, model: Optional[str] = None) -> OllamaClient:
        """
        Obtiene cliente LLM (con cache).

        Args:
            model: Nombre del modelo (usa default si None)

        Returns:
            OllamaClient configurado
        """
        model_name = model or settings.llm_default_model

        if model_name not in self._client_cache:
            self._client_cache[model_name] = OllamaClient(model=model_name)

        return self._client_cache[model_name]

    def execute_with_fallback(
        self,
        task_type: str,
        prompt: str,
        system: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Ejecuta generación con fallback automático.

        Args:
            task_type: Tipo de tarea
            prompt: Prompt para el modelo
            system: System prompt opcional
            context: Contexto para ruteo

        Returns:
            Respuesta del modelo

        Raises:
            LLMError: Si todos los fallbacks fallan
        """
        decision, fallbacks = self.route_with_fallback(task_type, context)

        # Try primary model
        try:
            client = self.get_client(decision.model)
            response = client.generate(
                prompt=prompt,
                system=system,
                temperature=decision.temperature,
            )
            self._record_success(decision.model)
            return response
        except LLMError as e:
            logger.warning(f"Primary model failed: {decision.model} → {e}")
            self._record_failure(decision.model)

        # Try fallbacks
        for fallback_model in fallbacks:
            logger.info(f"Trying fallback: {fallback_model}")
            try:
                client = self.get_client(fallback_model)
                response = client.generate(
                    prompt=prompt,
                    system=system,
                    temperature=decision.temperature,
                )
                self._record_success(fallback_model)
                return response
            except LLMError as e:
                logger.warning(f"Fallback failed: {fallback_model} → {e}")
                self._record_failure(fallback_model)

        # All failed
        err = LLMError(
            f"All models failed for {task_type}. Tried: {[decision.model] + fallbacks}",
            model=decision.model,
        )
        _log_error(err, "execute_with_fallback")
        raise err

    def _record_routing_metric(self, task_type: str, model: str, latency: float) -> None:
        """Registra métrica de ruteo."""
        key = f"{task_type}:{model}"
        if key not in self._metrics:
            self._metrics[key] = {
                "count": 0,
                "total_latency": 0.0,
                "successes": 0,
                "failures": 0,
            }

        self._metrics[key]["count"] += 1
        self._metrics[key]["total_latency"] += latency

    def _record_success(self, model: str) -> None:
        """Registra éxito de modelo."""
        if model not in self._metrics:
            self._metrics[model] = {"count": 0, "successes": 0, "failures": 0}
        self._metrics[model]["successes"] += 1

    def _record_failure(self, model: str) -> None:
        """Registra fallo de modelo."""
        if model not in self._metrics:
            self._metrics[model] = {"count": 0, "successes": 0, "failures": 0}
        self._metrics[model]["failures"] += 1

    def get_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Obtiene métricas de ruteo."""
        metrics = {}
        for key, data in self._metrics.items():
            if "total_latency" in data and data["count"] > 0:
                metrics[key] = {
                    "count": data["count"],
                    "avg_latency": data["total_latency"] / data["count"],
                    "success_rate": data["successes"] / max(data["count"], 1),
                    "failures": data["failures"],
                }
            else:
                metrics[key] = {
                    "successes": data.get("successes", 0),
                    "failures": data.get("failures", 0),
                    "success_rate": data.get("successes", 0) / max(data.get("successes", 0) + data.get("failures", 1), 1),
                }
        return metrics

    def clear_cache(self) -> None:
        """Limpia cache de decisiones."""
        self._decision_cache.clear()
        logger.info("Routing cache cleared")

    def close(self) -> None:
        """Cierra todos los clientes cacheados."""
        for client in self._client_cache.values():
            client.close()
        self._client_cache.clear()
        logger.info("ModelRouter closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
