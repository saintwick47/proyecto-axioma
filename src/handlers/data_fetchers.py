#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Data Fetchers (Capa de acceso a datos externos)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/handlers/data_fetchers.py
# Autor: SaintWick
# ✅ CORREGIDO v0.1.0:
# - extract_city(): Regex genérico "en/para/de + nombre" antes del diccionario
#   FIX: "clima para villa general belgrano" → extrae "villa general belgrano"
# - get_coordinates(): Fallback a Open-Meteo Geocoding API para ciudades
#   no presentes en el diccionario hardcodeado
#   FIX: Villa General Belgrano ahora obtiene coordenadas reales (-31.98, -64.55)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
import pytz

from config.settings import settings

logger = logging.getLogger(__name__)

# ✅ v0.6.9v (ISSUE-037): limpieza del tema para NewsAPI.
_NEWS_FILLER = re.compile(
    r"\b(dame|deme|decime|mostrame|mostr[aá]|traeme|quiero|necesito|"
    r"las|los|el|la|un|una|unos|unas|del|de|en|sobre|para|"
    r"hoy|ahora|actuales|recientes|"
    r"[uú]ltimas?|noticias?|news)\b",
    re.IGNORECASE,
)


def _news_topic(query: str) -> str:
    """Tema de búsqueda limpio para NewsAPI.

    ✅ ISSUE-037: la limpieza anterior era
    `query.replace("noticias","").replace("últimas","")` — usaba el acento
    literal (con "ultimas" sin tilde NO limpiaba) y dejaba muletillas
    ("dame las ultimas   de argentina hoy") → NewsAPI devolvía 0 artículos y
    el request FALLABA con ValueError ante el usuario. Ahora se quitan
    muletillas por regex, sin depender de acentos. Devuelve "" si no queda un
    tema útil (el llamador usa un default amplio).
    """
    if not query:
        return ""
    cleaned = _NEWS_FILLER.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:¿?¡!-")
    return cleaned if len(cleaned) >= 3 else ""


# ✅ v0.6.9v (REFACTOR 50/50): la capa de clima + geocoding (y el helper
# compartido _log_api_call) se movió a data_fetchers_weather.py.
# Se re-exporta para no romper la API pública del módulo
# (`from .data_fetchers import DataFetchers, get_coordinates`).
from .data_fetchers_weather import (  # noqa: E402,F401
    WeatherFetcherMixin,
    get_coordinates,
    _log_api_call,
)


# ============================================================================
# CLASE PRINCIPAL — CAPA DE ACCESO A DATOS
# ============================================================================

class DataFetchers(WeatherFetcherMixin):
    """
    Capa de acceso a datos para DataHandler.

    Responsabilidades:
        - Obtener datos de APIs externas (Open-Meteo, Tavily, NewsAPI)
        - Obtener datos del sistema (hora/fecha)
        - Parsear y formatear respuestas
        - Extraer entidades del texto (ciudades)

    NO conoce: Caché L2, Ambigüedad, Response, Dispatcher.
    """


    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    # ═══════════════════════════════════════════════════════════
    # FETCHERS DE API
    # ═══════════════════════════════════════════════════════════

    async def fetch_finance(self, query: str) -> Dict[str, Any]:
        """
        Datos financieros: dolarapi.com (gratis, sin key) para dólar argentino;
        Tavily como general (requiere TAVILY_API_KEY).
        """
        start = time.time()
        logger.debug(f"DataFetchers: fetch_finance started: {query[:50]}...")

        # ✅ plan_rafael (2026-08-31): cotización del dólar SIN API key vía
        # dolarapi.com (Argentina, gratis). Antes, sin TAVILY_API_KEY, la
        # consulta fallaba con "TAVILY_API_KEY no configurado" y no daba valor.
        if re.search(r"\bd[oó]lar(es)?\b", query.lower()):
            try:
                return await self._fetch_dolar_arg()
            except Exception as e:
                logger.warning(f"DataFetchers: dolarapi falló ({e}) — probando Tavily")

        if not settings.tavily_api_key:
            logger.error("Finance failed: TAVILY_API_KEY not configured")
            raise ValueError(
                "TAVILY_API_KEY no configurado y no es consulta de dólar "
                "(dolarapi.com solo cubre dólar argentino)"
            )

        search_query = f"{query} price today latest"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                _log_api_call(
                    "tavily", "request", success=True,
                    extra_info={"query": search_query[:50]},
                )
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": settings.tavily_api_key,
                        "query": search_query,
                        "max_results": 3,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("results"):
                    _log_api_call(
                        "tavily", "response", success=False,
                        error="No results",
                    )
                    raise ValueError("No se encontraron datos financieros")

                first = data["results"][0]
                snippet = first.get("content", "")[:300]
                url = first.get("url", "")
                content = f"💰 {query}:\n{snippet}\nFuente: {url}"

                duration = time.time() - start
                _log_api_call(
                    "tavily", "response", success=True,
                    duration=duration, response_size=len(content),
                )
                logger.debug(
                    f"DataFetchers: Finance completed: {query[:30]} "
                    f"({duration*1000:.1f}ms)"
                )

                return {
                    "content": content,
                    "confidence": 0.9,
                    "sources": [url],
                }
        except Exception as e:
            duration = time.time() - start
            _log_api_call(
                "tavily", "response", success=False,
                duration=duration, error=str(e),
            )
            raise

    async def _fetch_dolar_arg(self) -> Dict[str, Any]:
        """
        Cotizaciones del dólar argentino vía dolarapi.com (gratis, sin API key).
        Devuelve el mismo formato que fetch_finance: {content, confidence, sources}.
        """
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://dolarapi.com/v1/dolares")
                resp.raise_for_status()
                data = resp.json()

            lines: List[str] = []
            seen: set = set()
            for d in data:
                casa = d.get("casa") or d.get("nombre", "")
                if casa in seen:
                    continue
                seen.add(casa)
                compra, venta = d.get("compra"), d.get("venta")
                if compra is None or venta is None:
                    continue
                lines.append(
                    f"• {d.get('nombre', casa)}: "
                    f"compra ${compra:,.0f} — venta ${venta:,.0f}"
                )
            if not lines:
                raise ValueError("dolarapi no devolvió cotizaciones")

            content = "💰 Cotización del dólar hoy:\n" + "\n".join(lines[:8])
            content += "\nFuente: dolarapi.com"

            duration = time.time() - start
            _log_api_call(
                "dolarapi", "response", success=True,
                duration=duration, response_size=len(content),
            )
            logger.debug(f"DataFetchers: dólar OK ({duration*1000:.0f}ms, {len(lines)} casas)")
            return {
                "content": content,
                "confidence": 0.95,
                "sources": ["https://dolarapi.com/v1/dolares"],
            }
        except Exception as e:
            duration = time.time() - start
            _log_api_call(
                "dolarapi", "response", success=False,
                duration=duration, error=str(e),
            )
            raise

    async def fetch_time(self, query: str) -> Dict[str, Any]:
        """
        Hora/fecha del sistema.
        """
        start = time.time()
        logger.debug(f"DataFetchers: fetch_time started: {query[:50]}...")

        tz = pytz.timezone("America/Argentina/Buenos_Aires")
        now = datetime.now(tz)
        query_lower = query.lower()

        if any(word in query_lower for word in ["fecha", "día", "hoy"]):
            content = (
                f"📅 Fecha actual:\n"
                f"• {now.strftime('%A, %d de %B de %Y')}\n"
                f"• Zona: {tz}"
            )
        elif any(word in query_lower for word in ["hora", "tiempo"]):
            content = (
                f"🕐 Hora actual:\n"
                f"• {now.strftime('%H:%M:%S')}\n"
                f"• Zona: {tz}"
            )
        else:
            content = (
                f"🕐 Fecha y hora actual:\n"
                f"• Fecha: {now.strftime('%Y-%m-%d')}\n"
                f"• Hora: {now.strftime('%H:%M:%S')}\n"
                f"• Zona: {tz}"
            )

        duration = time.time() - start
        logger.debug(f"DataFetchers: Time completed ({duration*1000:.1f}ms)")

        return {
            "content": content,
            "confidence": 1.0,
            "sources": ["sistema"],
        }

    async def fetch_news(self, query: str) -> Dict[str, Any]:
        """
        NewsAPI: noticias recientes.
        """
        start = time.time()
        logger.debug(f"DataFetchers: fetch_news started: {query[:50]}...")

        if not settings.news_api_key:
            logger.error("News failed: NEWS_API_KEY not configured")
            raise ValueError("NEWS_API_KEY no configurado")

        # ✅ v0.6.9v (ISSUE-037): limpieza robusta (ver _news_topic).
        topic = _news_topic(query)

        params = {
            "apiKey": settings.news_api_key,
            "q": topic or "Argentina",
            "language": "es",
            "pageSize": 5,
            "sortBy": "publishedAt",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                _log_api_call(
                    "newsapi", "request", success=True,
                    extra_info={
                        "topic": topic or "Argentina",
                        "endpoint": "/everything",
                    },
                )
                resp = await client.get(
                    "https://newsapi.org/v2/everything", params=params
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") == "error":
                    error_msg = data.get(
                        "message", "Error desconocido de NewsAPI"
                    )
                    _log_api_call(
                        "newsapi", "response", success=False,
                        error=error_msg,
                    )
                    logger.error(f"NewsAPI error: {error_msg}")
                    raise ValueError(f"NewsAPI: {error_msg}")

                articles = data.get("articles", [])
                if not articles and params.get("q") != "Argentina":
                    # ✅ ISSUE-037: reintento ÚNICO con consulta amplia.
                    logger.info(
                        f"NewsAPI sin resultados para {params['q']!r} → "
                        f"reintento con 'Argentina'")
                    params["q"] = "Argentina"
                    resp = await client.get(
                        "https://newsapi.org/v2/everything", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    articles = data.get("articles", [])
                if not articles:
                    _log_api_call(
                        "newsapi", "response", success=False,
                        error="No articles",
                    )
                    logger.warning(
                        f"NewsAPI returned no articles. Response: {data}"
                    )
                    # ✅ ISSUE-037: DEGRADAR, no lanzar. Antes el ValueError
                    # llegaba al dispatcher y el usuario veía un ERROR en vez de
                    # una respuesta con el motivo.
                    return {
                        "content": (
                            f"📰 No encontré noticias para "
                            f"**'{query[:60]}'**.\n"
                            f"Probá con un tema o país más amplio "
                            f"(ej: *Argentina*, *economía*, *tecnología*)."
                        ),
                        "confidence": 0.3,
                        "sources": [],
                        "success": False,
                    }

                lines = ["📰 Últimas noticias:\n"]
                sources = []
                for i, article in enumerate(articles[:5], 1):
                    title = article.get("title", "Sin título")
                    source = article.get("source", {}).get("name", "Desconocido")
                    url = article.get("url", "")
                    lines.append(f"{i}. {title} ({source})")
                    if url:
                        sources.append(url)

                content = "\n".join(lines)
                duration = time.time() - start
                _log_api_call(
                    "newsapi", "response", success=True,
                    duration=duration, response_size=len(content),
                )
                logger.debug(
                    f"DataFetchers: News completed: {len(articles)} articles "
                    f"({duration*1000:.1f}ms)"
                )

                return {
                    "content": content,
                    "confidence": 0.95,
                    "sources": sources,
                }
        except Exception as e:
            duration = time.time() - start
            _log_api_call(
                "newsapi", "response", success=False,
                duration=duration, error=str(e),
            )
            raise


__all__ = ["DataFetchers", "get_coordinates"]
