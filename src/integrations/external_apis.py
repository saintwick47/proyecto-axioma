#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# external_apis.py - Gestor de APIs Externas con Despacho Estático
# Ruta: /ruta/a/axioma/src/integrations/external_apis.py
# Autor: SaintWick
# Versión: 4.2.1
#
# Gestión unificada y asíncrona de APIs externas (Tavily, Serper, NewsAPI)
# con fallback en cascada, circuit breaker por proveedor, logging detallado
# y despacho estático match/case. Proporciona facade execute() para
# RouterAPIExecutor y MCP Tools.
# ═══════════════════════════════════════════════════════════════
#
# ╔═══════════════════════════════════════════════════════════════╗
# ║  ⚠️  DUPLICACIÓN CONSCIENTE — NO REFACTORIZAR              ║
# ╠═══════════════════════════════════════════════════════════════╣
# ║  Este archivo contiene métodos de búsqueda web (_search_    ║
# ║  tavily, _search_serper, _search_newsapi) con nombres       ║
# ║  similares a:                                               ║
# ║                                                             ║
# ║    • src/agents/searcher.py          (SYNC + DuckDuckGo)    ║
# ║    • src/agents/researcher_sources.py (SYNC, capa datos)    ║
# ║                                                             ║
# ║  Las 3 implementaciones NO son duplicación real.            ║
# ║  Body hash SHA256 del AST normalizado confirma que los      ║
# ║  cuerpos son DISTINTOS (hash únicos por archivo).           ║
# ║                                                             ║
# ║  Este archivo es ÚNICO porque:                              ║
# ║    1. Es ASYNC (httpx.AsyncClient) — requerido por          ║
# ║       SmartRouter/RouterAPIExecutor que corren en event loop║
# ║    2. Usa helper genérico _http_search_impl() reutilizable  ║
# ║    3. Logging DetailedLogger completo (API_CALL + métricas) ║
# ║    4. Facade execute(intent, query) para RouterAPIExecutor  ║
# ║    5. Fallback en cascada Tavily → Serper → SerpAPI (+ dolarapi para dólar)             ║
# ║    6. Es consumido por 4 MCP Tools (WeatherTool,            ║
# ║       FinanceTool, NewsTool, WebSearchTool) vía sync→async  ║
# ║                                                             ║
# ║  Cada implementación tiene un caller distinto en runtime:   ║
# ║    • external_apis.py → SmartRouter + MCP Tools             ║
# ║    • searcher.py → Dispatcher._handle_search()              ║
# ║    • researcher_sources.py → ResearcherAgent                ║
# ║                                                             ║
# ║  Consolidar rompería contratos reales:                      ║
# ║    - Forzar async→sync requeriría asyncio.to_thread()       ║
# ║    - MCP Tools perderían su adapter sync→async              ║
# ║    - RouterAPIExecutor no puede ser sync (event loop)       ║
# ║                                                             ║
# ║  NO unificar. Ver: logs/search_duplication_analysis_v2.txt  ║
# ╚═══════════════════════════════════════════════════════════════╝
#
# ═══════════════════════════════════════════════════════════════
import logging
import asyncio
import time
import sys
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from src.domain.exceptions import IntegrationError

logger = logging.getLogger(__name__)


# ============================================================================
# ✅ FIX AUDITORÍA — Circuit breaker liviano por proveedor de búsqueda
# ============================================================================
# El health check de initialize() (_generic_health_check) solo valida que la
# API key exista — no que el servicio esté realmente arriba. Sin esto, un
# proveedor configurado pero caído a nivel red hace que cada búsqueda pague
# su timeout completo (self.timeout) antes de caer al siguiente. Se duplica
# acá en vez de importar de searcher.py/researcher_sources.py a propósito —
# ver el aviso de "DUPLICACIÓN CONSCIENTE" arriba, este archivo es async y
# los otros dos son sync.
from src.utils.provider_circuit import ProviderCircuit as _ProviderCircuit


# ✅ v0.6.9v (REFACTOR 50/50): helpers de integración (scrub de secretos,
# logging, dólar y normalización de motores SerpAPI) movidos a
# src/integrations/external_apis_helpers.py. Se re-exportan para no romper
# la API pública del módulo (tests y otros módulos los importan de acá).
from .external_apis_helpers import (  # noqa: E402,F401
    _scrub_secrets,
    _log_event,
    _log_error,
    _log_api_call,
    _query_es_dolar,
    _format_dolarapi,
    _ENGINE_LIST_KEYS,
    _normalize_engine_items,
)

# ============================================================================
# CLASE PRINCIPAL: ExternalAPIManager
# ============================================================================
class ExternalAPIManager:
    """
    Gestor unificado para APIs externas con fallback en cascada.
    Features:
    - Tavily API: Búsqueda web inteligente con AI
    - Serper API: Google Search vía API oficial
    - SerpAPI (serpapi.com): web, news, imágenes, youtube, maps, shopping, scholar, patents
    - dolarapi: cotizaciones de dólar (sin API key)
    - Fallback automático entre proveedores (Tavily → Serper → SerpAPI)
    - Timeout configurable por API vía settings
    - Logging detallado de todas las operaciones vía DetailedLogger
    ✅ v4.2.0: Despacho estático match/case — el AST y FlowMapper pueden
       trazar la cadena completa DataHandler → ExternalAPIs → _search_tavily/_search_serper.
       El flujo Data Handler pasa de [⚠️ PARCIAL] a [✅ CONECTADO].
    ✅ v4.2.1: execute(intent, query) — facade requerido por RouterAPIExecutor.
       RouterAPIExecutor llama api_manager.execute(api_intent, query), pero
       ExternalAPIManager no tenía ese método → AttributeError en runtime.
    """
    _SUPPORTED_APIS = ("tavily", "serper", "serpapi", "newsapi")
    _API_ENDPOINTS = {"tavily": "/search", "serper": "/search",
                      "serpapi": "/search.json", "newsapi": "/everything"}

    def __init__(self, timeout: Optional[int] = None):
        """Inicializa gestor de APIs externas con timeout configurable."""
        start_init = time.time()
        self.timeout = timeout or getattr(settings, 'ollama_timeout', 25)
        self._initialized = False
        self._session = None
        self._api_status = {api: False for api in self._SUPPORTED_APIS}
        # ✅ FIX AUDITORÍA: circuit breaker por proveedor (ver clase arriba)
        self._circuits: Dict[str, _ProviderCircuit] = {
            "tavily": _ProviderCircuit(),
            "serper": _ProviderCircuit(),
            "serpapi": _ProviderCircuit(),  # ✅ v0.6.9q
            "google_cse": _ProviderCircuit(),  # ✅ v0.6.9q: Google Custom Search
        }
        self._circuit_fail_threshold = 3
        self._circuit_cooldown_seconds = 60.0
        duration = time.time() - start_init
        _log_event("INTEGRATION", "ExternalAPIManager initialized", extra={
            "timeout": self.timeout, "init_duration_ms": round(duration * 1000, 2),
            "available_apis": self._get_available_apis()
        })
        logger.info(f"ExternalAPIManager initialized: timeout={self.timeout}s")

    async def initialize(self) -> bool:
        """Inicializa conexiones y verifica disponibilidad de APIs.
        ✅ v4.2.0: Despacho estático match/case reemplaza getattr(self, f"_check_{api_name}").
           El AST puede resolver estáticamente cada rama del match.
        """
        start = time.time()
        _log_event("INTEGRATION", "initialize() started")
        if self._initialized:
            _log_event("INTEGRATION", "initialize() skipped - already initialized")
            return True
        try:
            for api_name in self._SUPPORTED_APIS:
                key_attr = f"{api_name}_api_key"
                if hasattr(settings, key_attr) and getattr(settings, key_attr):
                    match api_name:
                        case "tavily":
                            self._api_status[api_name] = await self._check_tavily()
                        case "serper":
                            self._api_status[api_name] = await self._check_serper()
                        case "serpapi":
                            self._api_status[api_name] = await self._check_serpapi()
                        case "newsapi":
                            self._api_status[api_name] = await self._check_newsapi()
                        case _:
                            logger.warning(f"Unknown API in health check: {api_name}")
                _log_event("INTEGRATION", f"{api_name.capitalize()} check: {'✓' if self._api_status[api_name] else '✗'}")
            self._initialized = True
            duration = time.time() - start
            _log_event("INTEGRATION", "initialize() completed", extra={
                "available_apis": self._get_available_apis(),
                "duration_ms": round(duration * 1000, 2),
                "all_configured": all(getattr(settings, f"{api}_api_key", None) for api in self._SUPPORTED_APIS)
            })
            logger.info(f"ExternalAPIManager initialized: {self._get_available_apis()}")
            return any(self._api_status.values())
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "initialize", extra_info={"duration_ms": round(duration * 1000, 2), "api_status": self._api_status.copy()})
            logger.error(f"Failed to initialize ExternalAPIManager: {e}")
            return False

    async def _check_tavily(self) -> bool:
        """Verifica conexión con Tavily API (health check básico)."""
        return await self._generic_health_check("tavily", "tavily_api_key")

    async def _check_serper(self) -> bool:
        """Verifica conexión con Serper API (health check básico)."""
        return await self._generic_health_check("serper", "serper_api_key")

    async def _check_serpapi(self) -> bool:
        """Verifica conexión con SerpAPI (health check básico, v0.6.9q)."""
        return await self._generic_health_check("serpapi", "serpapi_api_key")

    async def _check_newsapi(self) -> bool:
        """Verifica conexión con News API (health check básico)."""
        return await self._generic_health_check("newsapi", "news_api_key")

    async def _generic_health_check(self, api_name: str, key_attr: str) -> bool:
        """Health check genérico para APIs que solo requieren validación de key."""
        start = time.time()
        try:
            available = bool(getattr(settings, key_attr, None))
            duration = time.time() - start
            _log_api_call(api_name, "/health", "check", success=available, duration=duration)
            return available
        except Exception as e:
            duration = time.time() - start
            _log_error(e, f"_check_{api_name}", extra_info={"duration_ms": round(duration * 1000, 2)})
            _log_api_call(api_name, "/health", "check", success=False, duration=duration, error=str(e))
            logger.warning(f"{api_name.capitalize()} health check failed: {e}")
            return False

    def _get_available_apis(self) -> List[str]:
        """Retorna lista de APIs disponibles (health check passed)."""
        return [name for name, available in self._api_status.items() if available]

    async def get_status(self) -> Dict[str, Any]:
        """Retorna estado completo de las APIs externas."""
        _log_event("INTEGRATION", "get_status() called")
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "apis": self._api_status.copy(),
            "all_configured": all(getattr(settings, f"{api}_api_key", None) for api in self._SUPPORTED_APIS),
            "available_count": sum(self._api_status.values()),
            "timeout": self.timeout,
        }
        _log_event("INTEGRATION", f"get_status() returned: {status['available_count']} APIs available")
        return status

    # ✅ v0.6.9q: cotizaciones de dólar desde dolarapi.com (público, sin key)
    async def _fetch_dolarapi(self) -> Optional[str]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get("https://dolarapi.com/v1/dolares")
                resp.raise_for_status()
                return _format_dolarapi(resp.json())
        except Exception as exc:  # noqa: BLE001 — cae al fallback web
            logger.warning(f"[external_apis] dolarapi falló: {exc}")
            return None

    # ✅ v4.2.1: execute() — facade requerido por RouterAPIExecutor
    async def execute(self, intent: str, query: str, **kwargs: Any) -> Dict[str, Any]:
        """
        Facade unificado para RouterAPIExecutor.execute_api_call().
        RouterAPIExecutor llama: api_manager.execute(api_intent, norm_query)
        donde api_intent viene de _API_INTENT_MAP:
        - "weather_query"  → search_web con contexto de clima
        - "news_query"     → get_news
        - "finance_query"  → search_web con contexto financiero
        - "web_search"     → search_web
        Retorna dict con 'success' bool, 'summary' str, 'results' list.
        RouterAPIExecutor usa result.get("success") y result.get("summary").
        """
        start = time.time()
        _log_event("INTEGRATION", f"execute() called: intent={intent}, query='{query[:50]}...'")
        try:
            match intent:
                case "web_search":
                    results = await self.search_web(query, max_results=kwargs.get("max_results", 5))
                    if results:
                        return {
                            "success": True,
                            "summary": self._format_search_results(results),
                            "answer": self._format_search_results(results),
                            "results": results,
                            "intent": intent,
                        }
                    return {"success": False, "error": "No se encontraron resultados de búsqueda web"}
                case "news_query":
                    location = kwargs.get("location")
                    articles = await self.get_news(query, location=location)
                    if articles:
                        return {
                            "success": True,
                            "summary": self._format_news_results(articles),
                            "answer": self._format_news_results(articles),
                            "results": articles,
                            "intent": intent,
                        }
                    return {"success": False, "error": "No se encontraron noticias"}
                case "weather_query":
                    weather_query = f"clima tiempo atmosférico {query} hoy temperatura"
                    results = await self.search_web(weather_query, max_results=3)
                    if results:
                        return {
                            "success": True,
                            "summary": self._format_search_results(results),
                            "answer": self._format_search_results(results),
                            "results": results,
                            "intent": intent,
                        }
                    return {"success": False, "error": "No se encontraron datos de clima"}
                case "finance_query":
                    # ✅ v0.6.9q: dólar/cotización → dolarapi (público) primero;
                    # si no aplica o falla, cae a search_web (tavily/serper).
                    if _query_es_dolar(query):
                        dolar = await self._fetch_dolarapi()
                        if dolar:
                            return {
                                "success": True,
                                "summary": dolar,
                                "answer": dolar,
                                "results": [],
                                "intent": intent,
                                "source": "dolarapi",
                            }
                    finance_query = f"{query} precio cotización hoy"
                    results = await self.search_web(finance_query, max_results=3)
                    if results:
                        return {
                            "success": True,
                            "summary": self._format_search_results(results),
                            "answer": self._format_search_results(results),
                            "results": results,
                            "intent": intent,
                        }
                    return {"success": False, "error": "No se encontraron datos financieros"}
                case _:
                    logger.warning(f"execute(): intent desconocido '{intent}', usando web_search")
                    results = await self.search_web(query)
                    if results:
                        return {
                            "success": True,
                            "summary": self._format_search_results(results),
                            "answer": self._format_search_results(results),
                            "results": results,
                            "intent": intent,
                        }
                    return {"success": False, "error": f"Intent '{intent}' no soportado y búsqueda web fallida"}
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "execute", extra_info={"intent": intent, "query": query[:50]})
            logger.error(f"execute() failed: intent={intent} error={e}")
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    def _format_search_results(self, results: List[Dict[str, Any]]) -> str:
        """Formatea resultados de búsqueda web en texto legible."""
        parts = []
        for r in results[:3]:
            title = r.get("title", "")
            content = r.get("content", r.get("snippet", ""))[:300]
            url = r.get("url", r.get("link", ""))
            if title or content:
                parts.append(f"**{title}**\n{content}\nFuente: {url}")
        return "\n".join(parts) if parts else "Sin resultados disponibles."

    def _format_news_results(self, articles: List[Dict[str, Any]]) -> str:
        """Formatea artículos de noticias en texto legible."""
        parts = []
        for a in articles[:3]:
            title = a.get("title", "")
            desc = a.get("description", "")[:200]
            source = a.get("source", "")
            url = a.get("url", "")
            published = a.get("published_at", "")
            if title:
                parts.append(f"**{title}** ({source}{', ' + published[:10] if published else ''})\n{desc}\n{url}")
        return "\n".join(parts) if parts else "Sin noticias disponibles."

    async def search_engine(self, engine: str, query: str,
                            max_results: int = 5,
                            params_extra: Optional[Dict[str, Any]] = None,
                            ) -> List[Dict[str, Any]]:
        """Consulta un motor SerpAPI genérico (v0.6.9q).

        Motores: google_images, youtube, google_maps, google_shopping,
        google_scholar, google_patents. Devuelve {title, url, content}.
        """
        key = getattr(settings, "serpapi_api_key", None)
        if not key:
            logger.warning(f"[external_apis] search_engine({engine}): sin key")
            return []
        if engine not in _ENGINE_LIST_KEYS:
            logger.warning(f"[external_apis] motor SerpAPI desconocido: {engine}")
            return []
        params: Dict[str, Any] = {"engine": engine, "q": query,
                                  "api_key": key}
        if engine != "google_patents":
            # google_patents rechaza el parámetro num (400 Bad Request)
            params["num"] = max_results
        if engine == "youtube":
            params["search_query"] = params.pop("q")  # YouTube usa search_query
        if engine == "google_maps":
            params.setdefault("type", "search")
        if params_extra:
            params.update(params_extra)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://serpapi.com/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()
            results = _normalize_engine_items(engine, data, max_results)
            if results:
                _log_api_call("serpapi", f"/search.json?engine={engine}",
                              "search_engine", success=True, duration=0.0,
                              response_size=len(results))
            return results
        except Exception as exc:  # noqa: BLE001 — respaldo: otro motor/proveedor
            logger.warning(f"[external_apis] SerpAPI engine '{engine}' falló: "
                           f"{_scrub_secrets(exc)}")
            return []

    async def _search_google_cse(self, query: str,
                                 max_results: int) -> List[Dict[str, Any]]:
        """Google Custom Search (v0.6.9q): key + CX del Programmable Search.

        Devuelve {title, url, content} a partir de data['items'].
        Se activa automáticamente si .env tiene GOOGLE_API_KEY y
        SEARCH_ENGINE_ID (CX).
        """
        if not (getattr(settings, "google_api_key", None)
                and getattr(settings, "search_engine_id", None)):
            return []
        params = {"key": settings.google_api_key,
                  "cx": settings.search_engine_id,
                  "q": query, "num": max_results}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params=params)
                resp.raise_for_status()
                data = resp.json()
            out = []
            for item in data.get("items") or []:
                out.append({"title": item.get("title", ""),
                            "url": item.get("link", ""),
                            "content": (item.get("snippet") or "")[:200]})
            if out:
                _log_api_call("google_cse", "/customsearch/v1",
                              "search_google_cse", success=True,
                              duration=0.0, response_size=len(out))
            return out
        except Exception as exc:  # noqa: BLE001 — cae al siguiente proveedor
            logger.warning(f"[external_apis] Google CSE falló: "
                           f"{_scrub_secrets(exc)}")
            return []

    async def search_web(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Búsqueda web con fallback en cascada: Tavily → Serper → SerpAPI → Google CSE.
        ✅ v4.2.0: Despacho estático match/case reemplaza getattr(self, f"_search_{api_name}").
           El AST puede resolver estáticamente cada rama del match.
           El flujo Data Handler pasa de [⚠️ PARCIAL] a [✅ CONECTADO].
        """
        start = time.time()
        _log_event("INTEGRATION", f"search_web() started: query='{query[:50]}...', max_results={max_results}")
        available = self._get_available_apis()
        if not available:
            _log_event("INTEGRATION", "search_web() failed: no APIs available", level="WARNING")
            logger.warning("No external APIs available for web search")
            return []
        providers = ["tavily", "serper", "serpapi"]
        if getattr(settings, "google_api_key", None) and \
                getattr(settings, "search_engine_id", None):
            providers.append("google_cse")  # ✅ v0.6.9q: Google Custom Search
        for api_name in providers:
            if api_name in available or api_name == "google_cse":
                # ✅ FIX AUDITORÍA: saltear sin red si el circuito está abierto
                if self._circuits[api_name].is_open(self._circuit_cooldown_seconds):
                    _log_event("INTEGRATION", f"{api_name.capitalize()} circuito abierto — salteando", level="WARNING")
                    logger.debug(f"[ExternalAPIManager] {api_name}: circuito abierto, salteando")
                    continue
                try:
                    _log_event("INTEGRATION", f"Attempting {api_name.capitalize()} search" + (" (fallback)" if api_name != "tavily" else ""))
                    match api_name:
                        case "tavily":
                            results = await self._search_tavily(query, max_results)
                        case "serper":
                            results = await self._search_serper(query, max_results)
                        case "serpapi":
                            results = await self._search_serpapi(query, max_results)
                        case "google_cse":
                            results = await self._search_google_cse(query, max_results)
                        case _:
                            logger.warning(f"No search implementation for: {api_name}")
                            continue
                    if results:
                        duration = time.time() - start
                        _log_api_call(api_name, "/search", "search_web", success=True,
                                      duration=duration, response_size=len(results))
                        _log_event("INTEGRATION", f"{api_name.capitalize()} search succeeded: {len(results)} results")
                        return results
                except Exception as e:
                    _log_error(e, f"search_web_{api_name}", extra_info={"query": query[:50]})
                    logger.warning(f"{api_name.capitalize()} search failed, trying next fallback: {e}")
        duration = time.time() - start
        _log_event("INTEGRATION", "All web search APIs failed", level="WARNING", extra={
            "query": query[:50], "duration_ms": round(duration * 1000, 2), "attempted_apis": available
        })
        logger.info("All web search APIs failed, returning empty results")
        return []

    async def _search_tavily(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Implementación de búsqueda con Tavily API."""
        return await self._http_search_impl(
            api_name="tavily", url="https://api.tavily.com/search",
            payload={"api_key": settings.tavily_api_key, "query": query, "search_depth": "basic",
                     "include_answer": False, "max_results": max_results},
            parse_fn=lambda data: [
                {"title": i.get("title", ""), "url": i.get("url", ""),
                 "content": i.get("content", ""), "score": i.get("score", 0)}
                for i in data.get("results", [])
            ],
            query=query, max_results=max_results, key_attr="tavily_api_key"
        )

    async def _search_serper(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Implementación de búsqueda con Serper API."""
        return await self._http_search_impl(
            api_name="serper", url="https://google.serper.dev/search",
            headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
            payload={"q": query, "num": max_results},
            parse_fn=lambda data: [
                {"title": i.get("title", ""), "url": i.get("link", ""), "content": i.get("snippet", "")}
                for i in data.get("organic", [])[:max_results]
            ],
            query=query, max_results=max_results, key_attr="serper_api_key"
        )

    async def _search_serpapi(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Implementación de búsqueda con SerpAPI (serpapi.com, engine=google).

        ✅ v0.6.9q: proveedor nuevo — usa GET con api_key en query string y
        parsea organic_results. Fallback en cascada tras tavily/serper.
        """
        if not getattr(settings, "serpapi_api_key", None):
            return []
        try:
            import httpx
            params = {"engine": "google", "q": query, "num": max_results,
                      "api_key": settings.serpapi_api_key}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://serpapi.com/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()
            organic = data.get("organic_results") or []
            results = [
                {"title": i.get("title", ""), "url": i.get("link", "")
                 or i.get("url", ""), "content": i.get("snippet", "")}
                for i in organic[:max_results]
                if i.get("title") or i.get("link")
            ]
            if results:
                _log_api_call("serpapi", "/search.json", "_search_serpapi",
                              success=True, duration=0.0,
                              response_size=len(results))
            return results
        except Exception as exc:  # noqa: BLE001 — cae al siguiente proveedor
            logger.warning(f"[external_apis] SerpAPI search falló: "
                           f"{_scrub_secrets(exc)}")
            return []

    async def _news_serpapi(self, query: str,
                            location: Optional[str] = None) -> List[Dict[str, Any]]:
        """Noticias vía SerpAPI engine=google_news (sin NewsAPI key).

        ✅ v0.6.9q: mismo formato que NewsAPI para _format_news_results:
        title / description / source / url / published_at.
        """
        key = getattr(settings, "serpapi_api_key", None)
        if not key:
            return []
        gl = {"argentina": "ar", "españa": "es", "méxico": "mx"}.get(
            (location or "").lower(), "ar")
        try:
            import httpx
            params = {"engine": "google_news", "q": query, "gl": gl,
                      "hl": "es-419", "api_key": key}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://serpapi.com/search.json", params=params)
                resp.raise_for_status()
                data = resp.json()
            out: List[Dict[str, Any]] = []
            for item in (data.get("news_results") or [])[:5]:
                title = item.get("title") or ""
                if not title:
                    continue
                src = item.get("source")
                src_name = (src.get("name") if isinstance(src, dict)
                            else (src or ""))
                out.append({
                    "title": title,
                    "description": (item.get("snippet")
                                    or item.get("description") or "")[:200],
                    "source": src_name,
                    "url": item.get("link") or item.get("url") or "",
                    "published_at": (item.get("date") or "")[:10],
                })
            return out
        except Exception as exc:  # noqa: BLE001 — cae a "sin noticias"
            logger.warning(f"[external_apis] SerpAPI news falló: "
                           f"{_scrub_secrets(exc)}")
            return []

    async def _http_search_impl(self, api_name: str, url: str, payload: Dict,
                                parse_fn, query: str, max_results: int,
                                key_attr: str, headers: Optional[Dict] = None,
                                method: str = "post") -> List[Dict[str, Any]]:
        """Implementación genérica para búsquedas HTTP con logging y error handling."""
        start = time.time()
        _log_event("INTEGRATION", f"_{api_name}() called: query='{query[:50]}...'")
        if not getattr(settings, key_attr, None):
            _log_event("INTEGRATION", f"_{api_name}() skipped: no API key", level="WARNING")
            return []
        try:
            import httpx
            _log_event("INTEGRATION", f"Sending {api_name.capitalize()} request", extra={
                "url": url, "payload_size": len(str(payload)), "timeout": self.timeout
            })
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                request_kwargs = {"url": url, "json": payload, "headers": headers} if method == "post" else {"url": url, "params": payload, "headers": headers}
                response = await getattr(client, method)(**request_kwargs)
                response.raise_for_status()
                data = response.json()
                results = parse_fn(data)
                duration = time.time() - start
                # ✅ FIX AUDITORÍA: registrar éxito del circuito
                if api_name in self._circuits:
                    self._circuits[api_name].record_success()
                _log_api_call(api_name, self._API_ENDPOINTS.get(api_name, "/search"), f"_{api_name}",
                              success=True, duration=duration, status_code=response.status_code,
                              response_size=len(results))
                return results[:max_results]
        except Exception as e:
            duration = time.time() - start
            # ✅ FIX AUDITORÍA: registrar fallo del circuito
            if api_name in self._circuits:
                self._circuits[api_name].record_failure(self._circuit_fail_threshold)
            _log_api_call(api_name, self._API_ENDPOINTS.get(api_name, "/search"), f"_{api_name}",
                          success=False, duration=duration, error=str(e))
            _log_error(e, f"_{api_name}", extra_info={"query": query[:50]})
            logger.warning(f"{api_name.capitalize()} search failed: {e}")
            return []

    async def get_news(self, query: str, location: Optional[str] = None) -> List[Dict[str, Any]]:
        """Obtiene noticias relevantes con filtrado opcional por ubicación."""
        start = time.time()
        _log_event("INTEGRATION", f"get_news() started: query='{query[:50]}...', location={location}")
        if not self._api_status.get("newsapi"):
            # ✅ v0.6.9q: sin NewsAPI → SerpAPI google_news como respaldo
            if getattr(settings, "serpapi_api_key", None):
                logger.info("NewsAPI no disponible — usando SerpAPI google_news")
                return await self._news_serpapi(query, location)
            _log_event("INTEGRATION", "get_news() skipped: NewsAPI not available", level="WARNING")
            logger.warning("NewsAPI not available")
            return []
        try:
            results = await self._search_newsapi(query, location)
            duration = time.time() - start
            _log_api_call("newsapi", "/everything", "get_news", success=bool(results),
                          duration=duration, response_size=len(results) if results else 0)
            _log_event("INTEGRATION", f"get_news() completed: {len(results) if results else 0} results")
            return results
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "get_news", extra_info={"query": query[:50], "location": location,
                                                  "duration_ms": round(duration * 1000, 2)})
            _log_api_call("newsapi", "/everything", "get_news", success=False, duration=duration, error=str(e))
            logger.error(f"News search failed: {e}")
            return []

    async def _search_newsapi(self, query: str, location: Optional[str]) -> List[Dict[str, Any]]:
        """Implementación de búsqueda con News API."""
        start = time.time()
        _log_event("INTEGRATION", f"_search_newsapi() called: query='{query[:50]}...', location={location}")
        if not settings.news_api_key:
            _log_event("INTEGRATION", "_search_newsapi() skipped: no API key", level="WARNING")
            return []
        try:
            import httpx
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query, "apiKey": settings.news_api_key, "pageSize": 5,
                "sortBy": "publishedAt",
                "language": "es" if not location or location.lower() in ["argentina", "españa", "méxico"] else "en",
            }
            if location:
                params["country"] = self._get_country_code(location)
            _log_event("INTEGRATION", "Sending NewsAPI request", extra={
                "url": url, "params": {k: v for k, v in params.items() if k != "apiKey"}, "timeout": self.timeout
            })
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if data.get("status") == "error":
                    error_msg = data.get("message", "Unknown NewsAPI error")
                    _log_event("INTEGRATION", f"NewsAPI returned error: {error_msg}", level="ERROR")
                    raise IntegrationError(f"NewsAPI error: {error_msg}", api="newsapi")
                results = [
                    {"title": i.get("title", ""), "description": i.get("description", ""),
                     "url": i.get("url", ""), "source": i.get("source", {}).get("name", ""),
                     "published_at": i.get("publishedAt", "")}
                    for i in data.get("articles", [])[:5]
                ]
                duration = time.time() - start
                _log_api_call("newsapi", "/everything", "_search_newsapi", success=True,
                              duration=duration, status_code=response.status_code, response_size=len(results))
                return results
        except Exception as e:
            duration = time.time() - start
            _log_api_call("newsapi", "/everything", "_search_newsapi", success=False,
                          duration=duration, error=str(e))
            _log_error(e, "_search_newsapi", extra_info={"query": query[:50], "location": location})
            logger.warning(f"News API search failed: {e}")
            return []

    def _get_country_code(self, location: str) -> str:
        """Mapea nombre de país a código ISO de 2 letras para NewsAPI."""
        country_map = {
            "argentina": "ar", "españa": "es", "méxico": "mx", "colombia": "co",
            "chile": "cl", "perú": "pe", "usa": "us", "united states": "us", "uk": "gb"
        }
        return country_map.get(location.lower().strip(), "us")

    async def cleanup(self) -> None:
        """Libera recursos y cierra sesiones HTTP."""
        start = time.time()
        _log_event("INTEGRATION", "cleanup() started")
        if self._session:
            try:
                await self._session.aclose()
                self._session = None
                _log_event("INTEGRATION", "HTTP session closed")
            except Exception as e:
                _log_error(e, "cleanup_session", extra_info={"session_active": True})
        self._initialized = False
        duration = time.time() - start
        _log_event("INTEGRATION", "cleanup() completed", extra={"duration_ms": round(duration * 1000, 2)})
        logger.info("ExternalAPIManager cleaned up")

    def __repr__(self) -> str:
        """Representación string para debugging."""
        return f"ExternalAPIManager(available={self._get_available_apis()})"
