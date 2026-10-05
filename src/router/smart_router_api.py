#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Smart Router API: Integraciones y Llamadas de Red
# Ruta: /ruta/a/axioma/src/router/smart_router_api.py
# Autor: SaintWick
# Versión: 0.1.0 (v4.1.0 — SRP Refactor)
# Propósito: Aísla la fragilidad de las llamadas HTTP (Tavily, Serper, etc.)
#            del código de negocio estable del SmartRouter.
# ═══════════════════════════════════════════════════════════════
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from config.settings import settings
from src.domain.exceptions import AxiomaError
from src.domain.types import TaskType
from src.integrations.external_apis import ExternalAPIManager
from src.router.smart_router_core import QueryNormalizer

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
        logging.getLogger(__name__).debug(f"[D2 smart_router_api.py:42] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 smart_router_api.py:53] excepción degradada (intencional): {_d2e}")


# ═══════════════════════════════════════════════════════════════
# PROMPT DE FALLBACK PARA API
# ═══════════════════════════════════════════════════════════════
API_FALLBACK_PROMPT = (
    "\n\n⚠️ INSTRUCCIÓN CRÍTICA: La API externa no está disponible. "
    "Responde DIRECTAMENTE con tu conocimiento general sobre el tema. "
    "NO generes código. NO escribas scripts. NO propongas usar APIs. "
    "NO crees funciones ni clases. Da la información que tengas, "
    "indicando si es aproximada o de tu conocimiento general."
)


# ═══════════════════════════════════════════════════════════════
# ROUTER API EXECUTOR
# ═══════════════════════════════════════════════════════════════
class RouterAPIExecutor:
    """
    Gestiona las llamadas a APIs externas para el SmartRouter.
    Aísla la lógica de red, reintentos y mapeo de intents del orquestador principal.
    """

    _API_INTENT_MAP: Dict[str, str] = {
        TaskType.WEATHER_QUERY.value: "weather_query",
        TaskType.NEWS_QUERY.value:    "news_query",
        TaskType.FINANCE_QUERY.value: "finance_query",
        TaskType.WEB_SEARCH.value:    "web_search",
    }

    def __init__(self, timeout: int = None) -> None:
        timeout = timeout or getattr(settings, 'ollama_timeout', 45)
        self._api_manager: Optional[ExternalAPIManager] = None
        self._timeout = timeout
        self._normalizer = QueryNormalizer()

    def _get_api_manager(self) -> ExternalAPIManager:
        """Lazy initialization del ExternalAPIManager."""
        if self._api_manager is None:
            self._api_manager = ExternalAPIManager(timeout=self._timeout)
        return self._api_manager

    async def execute_api_call(self, intent: str, query: str, **kwargs) -> Dict[str, Any]:
        """
        Llama a la API externa correspondiente al intent.

        Retorna siempre un dict con 'success' bool.
        En caso de fallo, el caller hace fallback al LLM.
        """
        t0 = time.time()
        api_manager = self._get_api_manager()
        api_intent  = self._API_INTENT_MAP.get(intent, "web_search")

        # Normalizar queries específicas antes de enviar a la API
        norm_query = self._normalizer.normalize_weather_query(query) if intent == TaskType.WEATHER_QUERY.value else query

        logger.info(f"[API] intent={intent} query={query[:80]!r}")

        try:
            result = await api_manager.execute(api_intent, norm_query, **kwargs)
            duration = time.time() - t0

            if result.get("success"):
                _log_router(query, intent, "API", 1.0, api_required=True, duration=duration)
                return {
                    "success":   True,
                    "source":    "api",
                    "content":   result.get("summary", result.get("answer", str(result))),
                    "data":      result,
                    "timestamp": datetime.now().isoformat(),
                }

            logger.warning(f"[API] fallo en {intent}: {result.get('error')}")
            _log_router(query, intent, "LLM_FALLBACK", 0.5, api_required=True, duration=duration)
            return {"success": False, "source": "api",
                    "error": result.get("error", "API call failed"), "fallback": "llm"}

        except AxiomaError as e:
            _log_error(e, "RouterAPIExecutor.execute_api_call", extra={"intent": intent})
            return {"success": False, "source": "api", "error": str(e), "fallback": "llm"}
        except Exception as e:
            logger.exception(f"[API] error inesperado: {type(e).__name__}: {e}")
            _log_error(e, "RouterAPIExecutor.execute_api_call", extra={"intent": intent})
            return {"success": False, "source": "api",
                    "error": f"{type(e).__name__}: {e}", "fallback": "llm"}

    async def cleanup(self) -> None:
        """Libera recursos asincrónicos (API manager)."""
        if self._api_manager:
            await self._api_manager.cleanup()
            self._api_manager = None
        logger.info("RouterAPIExecutor: API manager liberado")
