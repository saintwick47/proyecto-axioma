#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# researcher_sources.py - Capa de acceso a datos para ResearcherAgent
# Ruta: /ruta/a/axioma/src/agents/researcher_sources.py
# Autor: SaintWick
# Versión: 0.0.9
#
# Proporciona detección de consultas (weather, news, finance, time),
# geolocalización, acceso a APIs externas (Tavily, Serper, NewsAPI,
# Open-Meteo) y datos estáticos de fallback. Esta capa no conoce el
# LLM ni AgentResult; su única responsabilidad es obtener datos
# del mundo exterior y clasificar el tipo de consulta.
# ═══════════════════════════════════════════════════════════════
#
# ╔═══════════════════════════════════════════════════════════════╗
# ║  ⚠️  DUPLICACIÓN CONSCIENTE — NO REFACTORIZAR              ║
# ╠═══════════════════════════════════════════════════════════════╣
# ║  Este archivo contiene métodos de búsqueda web (search_     ║
# ║  tavily, search_serper, search_news) con nombres similares  ║
# ║  a:                                                         ║
# ║                                                             ║
# ║    • src/integrations/external_apis.py (ASYNC, DetailedLog) ║
# ║    • src/agents/searcher.py          (SYNC + DuckDuckGo)    ║
# ║                                                             ║
# ║  Las 3 implementaciones NO son duplicación real.            ║
# ║  Body hash SHA256 del AST normalizado confirma que los      ║
# ║  cuerpos son DISTINTOS (hash únicos por archivo).           ║
# ║                                                             ║
# ║  Este archivo es ÚNICO porque:                              ║
# ║    1. Es capa de DATOS PURA — NO conoce LLM, AgentResult,   ║
# ║       BaseAgent, prompts ni síntesis. Solo obtiene datos.   ║
# ║    2. Es SYNC (httpx.post) — ResearcherAgent es sync y      ║
# ║       no puede depender de async sin romper su contrato.    ║
# ║    3. Tiene Open-Meteo (clima gratuito, sin API key) que    ║
# ║       es ÚNICO en todo el sistema (no está en external_apis ║
# ║       ni en searcher).                                      ║
# ║    4. Tiene detección de queries por regex (weather, news,  ║
# ║       finance, time) — lógica de clasificación que NO       ║
# ║       pertenece a external_apis.py ni searcher.py.          ║
# ║    5. Tiene datos estáticos de fallback (get_weather_info,  ║
# ║       get_time_info) — sin I/O de red.                      ║
# ║    6. Consolidarlo con external_apis.py acoplaría           ║
# ║       ResearcherAgent a ExternalAPIManager (violación SRP). ║
# ║                                                             ║
# ║  Cada implementación tiene un caller distinto en runtime:   ║
# ║    • researcher_sources.py → ResearcherAgent (self.sources) ║
# ║    • external_apis.py → SmartRouter + MCP Tools             ║
# ║    • searcher.py → Dispatcher._handle_search()              ║
# ║                                                             ║
# ║  Consolidar rompería contratos reales:                      ║
# ║    - Forzar sync→async rompería ResearcherAgent             ║
# ║    - Mover Open-Meteo a external_apis.py mezclaría dominios ║
# ║    - Mover detección regex a external_apis.py acoplaría     ║
# ║      lógica de clasificación a un gestor de APIs            ║
# ║                                                             ║
# ║  NO unificar. Ver: logs/search_duplication_analysis_v2.txt  ║
# ╚═══════════════════════════════════════════════════════════════╝
#
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from config.settings import settings

logger = logging.getLogger(__name__)

# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

def _log_sources_event(event: str, level: str = "INFO", extra: dict = None):
    """Log evento de researcher_sources en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event("RESEARCHER_SOURCES", event, level=level, extra=extra or {})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 researcher_sources.py:88] excepción degradada (intencional): {_d2e}")

def _log_sources_error(error: Exception, context: str, extra_info: Dict[str, Any] = None):
    """Registra error con traceback completo."""
    if not LOGGER_AVAILABLE:
        logger.error(f"researcher_sources - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, extra_info=extra_info,
                          function_name=f"researcher_sources.{context}")
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 researcher_sources.py:101] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ✅ FIX AUDITORÍA — Circuit breaker liviano por proveedor de búsqueda
# ============================================================================
# Mismo problema (y mismo fix) que searcher.py: sin esto, un proveedor
# CONFIGURADO pero caído a nivel red (no solo sin API key) hace que cada
# búsqueda pague su timeout completo (settings.search_timeout) antes de caer
# al siguiente. Se duplica acá en vez de importar de searcher.py a propósito
# — este archivo es explícitamente independiente de las otras 2
# implementaciones de búsqueda (ver el aviso de "DUPLICACIÓN CONSCIENTE"
# arriba); una clase utilitaria de 20 líneas sin I/O propio no es el tipo de
# acoplamiento que ese aviso busca evitar.
from src.utils.provider_circuit import ProviderCircuit as _ProviderCircuit


# ============================================================================
# CLASE PRINCIPAL — CAPA DE ACCESO A DATOS
# ============================================================================
class ResearcherSources:
    """
    Capa de acceso a datos para ResearcherAgent.

    Responsabilidades:
    - Detectar tipo de consulta (clima, noticias, finanzas, hora)
    - Buscar en APIs externas (Tavily, Serper, NewsAPI)
    - Obtener clima vía Open-Meteo (gratuito, sin API key)
    - Proveer datos estáticos de fallback (clima, zonas horarias)
    - Extraer ubicación desde texto

    NO conoce: LLM, AgentResult, BaseAgent, prompts, síntesis.
    """

    # ═══════════════════════════════════════════════════════════
    # PATRONES DE DETECCIÓN (SIN ACENTOS - se normaliza el texto)
    # ═══════════════════════════════════════════════════════════
    # ✅ CORREGIDO: Patrones ampliados para clima
    WEATHER_PATTERNS = [
        r'pronostico\s+(del\s+)?clima',
        r'pronostico\s+(para\s+)?(hoy|manana|pasado|esta\s+semana)',
        r'clima\s+(en|hoy|manana|ahora)',
        r'temperatura\s+(en|de|actual)',
        r'va\s+llover', r'humedad', r'viento',
        r'lluvia', r'llover', r'nieve', r'sol', r'nublado',
        r'weather', r'forecast', r'rain',
        r'sunny', r'cloudy', r'temperature',
    ]

    NEWS_PATTERNS = [
        r'noticias\s+(de|sobre|hoy|ultimas|actuales)',
        r'ultimas\s+noticias', r'breaking\s+news', r'headline',
        r'news\s+(about|today|latest)', r'actualidad', r'acontecimientos',
    ]

    # ✅ CORREGIDO: Patrones financieros ampliados (igual que classifier_api.py)
    FINANCE_PATTERNS = [
        # Orden flexible: "cotizacion del dolar" O "dolar cotizacion"
        r'cotizaciones?\s+del\s+(dolar|euro|peso|libra|yen)',
        r'cotizaciones?\s+(hoy|actual|en\s+vivo)',
        r'(dolar|euro|peso|libra|yen)\s+(cotizacion|precio|cuanto\s+vale|valor)',
        # ✅ NUEVOS: Términos específicos Argentina/Latam
        r'dolar\s+blue',
        r'blue\s+(hoy|actual|cotizacion|precio|valor)',
        r'dolar\s+(mep|ccl|oficial|tarjeta|mayorista|solidario)',
        r'(mep|ccl|argenpesos)\s+(cotizacion|precio|valor)',
        # ✅ NUEVOS: Variantes semánticas
        r'precio\s+del\s+bitcoin', r'valor\s+del\s+euro',
        r'como\s+esta\s+la\s+bolsa', r'tasa\s+de\s+cambio',
        r'cuanto\s+vale\s+el\s+dolar', r'compra\s+venta\s+dolar',
        # Patrones generales
        r'stock\s+price', r'bitcoin\s+(precio|valor|cuanto)',
        r'crypto', r'cripto', r'bolsa', r'mercado', r'inversion',
    ]

    # ✅ NUEVO: Keywords de fallback para finanzas (cuando regex no matchea)
    FINANCE_KEYWORDS = [
        'dolar', 'dólar', 'euro', 'peso', 'libra', 'yen',
        'blue', 'mep', 'ccl', 'argenpesos',
        'cotizacion', 'cotización', 'precio', 'valor', 'compra', 'venta',
        'bolsa', 'mercado', 'inversion', 'inversión',
        'bitcoin', 'crypto', 'cripto', 'usd', 'ars',
    ]

    TIME_PATTERNS = [
        r'que\s+hora\s+es', r'hora\s+(en|de|actual|ahora)',
        r'what\s+time\s+is\s+it', r'current\s+time',
        r'zona\s+horaria', r'huso\s+horario',
    ]

    # ═══════════════════════════════════════════════════════════
    # INICIALIZACIÓN
    # ═══════════════════════════════════════════════════════════
    def __init__(self):
        """Inicializa la capa de fuentes con contador de estadísticas."""
        self._api_stats = {
            "tavily_calls": 0,
            "serper_calls": 0,
            "news_api_calls": 0,
            "open_meteo_calls": 0,
        }
        # ✅ FIX AUDITORÍA: circuit breaker por proveedor (ver clase arriba)
        self._circuits: Dict[str, _ProviderCircuit] = {
            "tavily": _ProviderCircuit(),
            "serper": _ProviderCircuit(),
        }
        self._circuit_fail_threshold = 3
        self._circuit_cooldown_seconds = 60.0

    # ═══════════════════════════════════════════════════════════
    # NORMALIZACIÓN Y DETECCIÓN DE PATRONES
    # ═══════════════════════════════════════════════════════════
    @staticmethod
    def normalize_text(text: str) -> str:
        """
        Normaliza texto: quita acentos y convierte a minúsculas.

        Args:
            text: Texto original

        Returns:
            Texto normalizado sin acentos
        """
        text_normalized = unicodedata.normalize('NFKD', text.lower())
        text_normalized = ''.join(
            c for c in text_normalized if not unicodedata.combining(c)
        )
        return text_normalized

    def is_weather_query(self, text: str) -> bool:
        """✅ CORREGIDO: Detección robusta de consultas de clima."""
        text_normalized = self.normalize_text(text)
        return any(
            re.search(p, text_normalized, re.I)
            for p in self.WEATHER_PATTERNS
        )

    def is_news_query(self, text: str) -> bool:
        """Detecta consultas de noticias."""
        text_normalized = self.normalize_text(text)
        return any(
            re.search(p, text_normalized, re.I)
            for p in self.NEWS_PATTERNS
        )

    # ✅ CORREGIDO: Detección financiera con fallback por keywords
    def is_finance_query(self, text: str) -> bool:
        """
        Detecta si es consulta financiera.

        Flujo:
        1. Intenta con patrones regex primero
        2. Si no matchea, usa keywords como fallback
        """
        text_normalized = self.normalize_text(text)
        # 1. Patrones regex primero (más preciso)
        if any(re.search(p, text_normalized, re.I) for p in self.FINANCE_PATTERNS):
            return True
        # 2. ✅ FALLBACK: Keywords simples para cobertura amplia
        return any(kw in text_normalized for kw in self.FINANCE_KEYWORDS)

    def is_time_query(self, text: str) -> bool:
        """Detecta consultas de hora."""
        text_normalized = self.normalize_text(text)
        return any(
            re.search(p, text_normalized, re.I)
            for p in self.TIME_PATTERNS
        )

    def detect_query_type(self, text: str) -> Optional[str]:
        """
        Detecta el tipo de consulta y retorna el string descriptivo.

        Args:
            text: Texto de la consulta

        Returns:
            'weather', 'news', 'finance', 'time' o None
        """
        if self.is_weather_query(text):
            return "weather"
        if self.is_news_query(text):
            return "news"
        if self.is_finance_query(text):
            return "finance"
        if self.is_time_query(text):
            return "time"
        return None

    # ═══════════════════════════════════════════════════════════
    # GEOLOCALIZACIÓN
    # ═══════════════════════════════════════════════════════════
    @staticmethod
    def extract_location(text: str) -> Optional[str]:
        """
        Extrae ubicación explícita de la query.

        Args:
            text: Texto de la consulta

        Returns:
            Ubicación extraída o None
        """
        patterns = [
            r'en\s+([A-Z][a-záéíóúñ\s]+?)(?:\?|\.|,|$)',
            r'de\s+([A-Z][a-záéíóúñ\s]+?)(?:\?|\.|,|$)',
            r'para\s+([A-Z][a-záéíóúñ\s]+?)(?:\?|\.|,|$)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                location = match.group(1).strip()
                if location.lower() not in [
                    'el', 'la', 'los', 'las', 'un', 'una',
                    'hoy', 'mañana',
                ]:
                    return location
        # ✅ CORREGIDO: Fallback a settings.default_location
        if hasattr(settings, 'default_location') and settings.default_location:
            logger.debug(
                f"Location fallback to default: {settings.default_location}"
            )
            return settings.default_location
        return None

    @staticmethod
    def get_country_code(location: str) -> str:
        """
        Mapea nombre de país a código ISO.

        Args:
            location: Nombre del país

        Returns:
            Código ISO de 2 letras
        """
        country_map = {
            "argentina": "ar", "españa": "es", "méxico": "mx",
            "colombia": "co", "chile": "cl", "perú": "pe",
            "usa": "us", "united states": "us", "uk": "gb",
        }
        return country_map.get(location.lower().strip(), "us")

    # ═══════════════════════════════════════════════════════════
    # BÚSQUEDA WEB — APIs EXTERNAS
    # ═══════════════════════════════════════════════════════════
    def search_tavily(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """
        Búsqueda web con Tavily API.

        Args:
            query: Consulta de búsqueda
            max_results: Máximo de resultados

        Returns:
            Lista de resultados con título, URL, contenido
        """
        if not settings.tavily_api_key:
            return []
        try:
            import httpx
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": settings.tavily_api_key,
                "query": query,
                "search_depth": "basic",
                "include_answer": False,
                "max_results": max_results,
            }
            response = httpx.post(url, json=payload, timeout=settings.search_timeout)
            response.raise_for_status()
            data = response.json()
            results = [
                {
                    "title": i.get("title", ""),
                    "url": i.get("url", ""),
                    "content": i.get("content", ""),
                    "score": i.get("score", 0),
                }
                for i in data.get("results", [])
            ]
            self._api_stats["tavily_calls"] += 1
            self._circuits["tavily"].record_success()
            return results[:max_results]
        except Exception as e:
            self._circuits["tavily"].record_failure(self._circuit_fail_threshold)
            _log_sources_error(
                e, "search_tavily", extra_info={"query": query[:50]}
            )
            logger.warning(f"Tavily search failed: {e}")
            return []

    def search_serper(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """
        Búsqueda web con Serper API.

        Args:
            query: Consulta de búsqueda
            max_results: Máximo de resultados

        Returns:
            Lista de resultados con título, URL, snippet
        """
        if not settings.serper_api_key:
            return []
        try:
            import httpx
            url = "https://google.serper.dev/search"
            headers = {
                "X-API-KEY": settings.serper_api_key,
                "Content-Type": "application/json",
            }
            payload = {"q": query, "num": max_results}
            response = httpx.post(
                url, headers=headers, json=payload, timeout=settings.search_timeout
            )
            response.raise_for_status()
            data = response.json()
            results = [
                {
                    "title": i.get("title", ""),
                    "url": i.get("link", ""),
                    "content": i.get("snippet", ""),
                }
                for i in data.get("organic", [])[:max_results]
            ]
            self._api_stats["serper_calls"] += 1
            self._circuits["serper"].record_success()
            return results
        except Exception as e:
            self._circuits["serper"].record_failure(self._circuit_fail_threshold)
            _log_sources_error(
                e, "search_serper", extra_info={"query": query[:50]}
            )
            logger.warning(f"Serper search failed: {e}")
            return []

    def search_news(
        self, query: str, location: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Búsqueda de noticias con NewsAPI.

        Args:
            query: Tema de búsqueda
            location: Ubicación para filtrar noticias

        Returns:
            Lista de noticias con título, fuente, fecha
        """
        if not settings.news_api_key:
            return []
        try:
            import httpx
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "apiKey": settings.news_api_key,
                "pageSize": 5,
                "sortBy": "publishedAt",
                "language": (
                    "es"
                    if not location
                    or location.lower() in ["argentina", "españa", "méxico"]
                    else "en"
                ),
            }
            if location:
                params["country"] = self.get_country_code(location)
            response = httpx.get(url, params=params, timeout=settings.search_timeout)
            response.raise_for_status()
            data = response.json()
            results = [
                {
                    "title": i.get("title", ""),
                    "description": i.get("description", ""),
                    "url": i.get("url", ""),
                    "source": i.get("source", {}).get("name", ""),
                    "published_at": i.get("publishedAt", ""),
                }
                for i in data.get("articles", [])[:5]
            ]
            self._api_stats["news_api_calls"] += 1
            return results
        except Exception as e:
            _log_sources_error(
                e, "search_news",
                extra_info={"query": query[:50], "location": location},
            )
            logger.warning(f"News API search failed: {e}")
            return []

    def search_web(
        self, query: str, max_results: int = 5
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Fallback de búsqueda web (Tavily → Serper).

        ✅ FIX AUDITORÍA: cada proveedor tiene un _ProviderCircuit — si viene
        fallando (>= _circuit_fail_threshold seguidos), se saltea sin tocar
        la red durante _circuit_cooldown_seconds en vez de pagar su timeout
        completo en cada query nueva. Mismo fix aplicado en searcher.py.

        ✅ PLAN v2.0 (Fase 6): devuelve (results, provider_name) para que el
        caller reporte el proveedor REAL que respondió (api_used) en vez de
        adivinar por keys configuradas.

        Args:
            query: Consulta de búsqueda
            max_results: Máximo de resultados

        Returns:
            Tupla (lista de resultados o lista vacía, nombre del proveedor o "")
        """
        for provider, search_fn in (
            ("tavily", self.search_tavily),
            ("serper", self.search_serper),
        ):
            circuit = self._circuits[provider]
            if circuit.is_open(self._circuit_cooldown_seconds):
                logger.debug(f"[ResearcherSources] {provider}: circuito abierto, salteando")
                continue
            results = search_fn(query, max_results)
            if results:
                return results, provider

        logger.info(f"Web search fallback exhausted for: {query[:50]}")
        return [], ""

    # ═══════════════════════════════════════════════════════════
    # CLIMA — OPEN-METEO (GRATUITO, SIN API KEY)
    # ═══════════════════════════════════════════════════════════
    def get_weather_openmeteo(
        self, city: str, location_name: str, date_str: str
    ) -> str:
        """
        Obtiene pronóstico del tiempo desde Open-Meteo (gratuito, sin API key).
        Geocodifica la ciudad con nominatim y llama a open-meteo.com.

        Args:
            city: Nombre de la ciudad
            location_name: Nombre completo de la ubicación
            date_str: Fecha actual como string

        Returns:
            Pronóstico del tiempo formateado
        """
        import urllib.request
        import urllib.parse
        import json
        try:
            # 1. Geocodificar ciudad → lat/lon
            geo_query = urllib.parse.quote(city)
            geo_url = (
                f"https://geocoding-api.open-meteo.com/v1/search"
                f"?name={geo_query}&count=1&language=es&format=json"
            )
            with urllib.request.urlopen(geo_url, timeout=5) as resp:
                geo_data = json.loads(resp.read().decode())
                results = geo_data.get("results", [])
                if not results:
                    return (
                        f"{location_name} — {date_str}\n"
                        f"No se pudo geocodificar la ubicación."
                    )
                r = results[0]
                lat, lon = r["latitude"], r["longitude"]
                tz = r.get("timezone", "America/Argentina/Cordoba")
                resolved_name = r.get("name", city) + ", " + r.get("country", "")
                # 2. Obtener pronóstico actual + diario
                wx_url = (
                    f"https://api.open-meteo.com/v1/forecast"
                    f"?latitude={lat}&longitude={lon}"
                    f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                    f"precipitation,weather_code,wind_speed_10m,wind_gusts_10m"
                    f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
                    f"weather_code,precipitation_probability_max"
                    f"&timezone={urllib.parse.quote(tz)}&forecast_days=3"
                )
                with urllib.request.urlopen(wx_url, timeout=5) as resp:
                    wx = json.loads(resp.read().decode())
                curr, daily = wx.get("current", {}), wx.get("daily", {})
                WMO = {
                    0: "Despejado", 1: "Mayormente despejado",
                    2: "Parcialmente nublado", 3: "Nublado",
                    45: "Niebla", 48: "Niebla con escarcha",
                    51: "Llovizna leve", 53: "Llovizna moderada",
                    55: "Llovizna intensa", 61: "Lluvia leve",
                    63: "Lluvia moderada", 65: "Lluvia intensa",
                    71: "Nieve leve", 73: "Nieve moderada",
                    75: "Nieve intensa", 80: "Chubascos leves",
                    81: "Chubascos moderados", 82: "Chubascos intensos",
                    95: "Tormenta", 96: "Tormenta con granizo",
                    99: "Tormenta fuerte",
                }
                code = curr.get("weather_code", 0)
                condition = WMO.get(code, f"Código {code}")
                temp = curr.get("temperature_2m", "?")
                feels = curr.get("apparent_temperature", "?")
                humidity = curr.get("relative_humidity_2m", "?")
                wind = curr.get("wind_speed_10m", "?")
                gusts = curr.get("wind_gusts_10m", "?")
                precip = curr.get("precipitation", "?")
                tmax = daily.get("temperature_2m_max", [None])[0]
                tmin = daily.get("temperature_2m_min", [None])[0]
                precip_sum = daily.get("precipitation_sum", [None])[0]
                precip_prob = daily.get("precipitation_probability_max", [None])[0]
                lines_out = [
                    f"📍 {resolved_name} — {date_str}",
                    f"🌡 Ahora: {temp}°C (sensación {feels}°C) — {condition}",
                    f"📊 Máx/Mín: {tmax}°C / {tmin}°C",
                    f"💧 Humedad: {humidity}% | 💨 Viento: {wind} km/h (ráfagas {gusts} km/h)",
                    f"🌧 Precipitación: {precip} mm hoy | Prob: {precip_prob}%",
                ]
                self._api_stats["open_meteo_calls"] += 1
                return "\n".join(lines_out)
        except Exception as e:
            logger.warning(f"Open-Meteo failed: {e}")
            return (
                f"{location_name} — {date_str}\n"
                f"No se pudo obtener el pronóstico. Consultá smn.gob.ar"
            )

    # ═══════════════════════════════════════════════════════════
    # DATOS ESTÁTICOS (FALLBACK)
    # ═══════════════════════════════════════════════════════════
    @staticmethod
    def get_weather_info(
        location: str, local_datetime: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Información climática estática (fallback).

        Args:
            location: Ubicación para el clima
            local_datetime: Fecha/hora local si disponible

        Returns:
            Descripción del clima
        """
        climate_data = {
            "villa general belgrano": (
                "Villa General Belgrano, Córdoba, Argentina tiene clima templado "
                "con estaciones definidas. Verano (dic-feb): 25-35°C con lluvias "
                "esporádicas. Invierno (jun-ago): 5-18°C, noches frías. "
                "Otoño/primavera: templado, ideal para turismo. Para pronóstico "
                "en tiempo real, consulta el Servicio Meteorológico Nacional (SMN) "
                "de Argentina."
            ),
            "buenos aires": (
                "Buenos Aires tiene clima subtropical húmedo. Verano: 25-35°C, "
                "húmedo. Invierno: 8-18°C, suave. Lluvias distribuidas todo el "
                "año. Pronóstico actual: consultar SMN Argentina."
            ),
            "cordoba": (
                "Córdoba tiene clima templado con veranos cálidos (28-35°C) e "
                "inviernos frescos (5-18°C). Lluvias concentradas en verano. "
                "Para pronóstico actualizado, visita smn.gob.ar."
            ),
            "madrid": (
                "Madrid tiene clima mediterráneo continental. Veranos calurosos "
                "(30-40°C), inviernos fríos (0-10°C). Lluvias en primavera/otoño. "
                "Consulta AEMET para pronóstico actual."
            ),
            "méxico df": (
                "Ciudad de México tiene clima templado subhúmedo. Temperaturas "
                "12-25°C todo el año. Lluvias en verano (jun-sep). Consulta "
                "CONAGUA para pronóstico actual."
            ),
        }
        location_lower = location.lower().strip()
        for key, info in climate_data.items():
            if key in location_lower or location_lower in key:
                return info
        date_context = (
            f" (fecha local: {local_datetime['date']})"
            if local_datetime and local_datetime.get('date')
            else ""
        )
        return (
            f"Para {location}{date_context}, el clima varía según la estación. "
            f"Para información en tiempo real, te recomiendo consultar: "
            f"1) Servicio Meteorológico local, 2) AccuWeather, o 3) Weather.com. "
            f"¿En qué otra cosa puedo ayudarte?"
        )

    @staticmethod
    def get_time_info(location: Optional[str] = None) -> str:
        """
        Información de hora actual.

        Args:
            location: Ubicación opcional para zona horaria

        Returns:
            Información de hora actual
        """
        now = datetime.now(timezone.utc)
        if not location:
            return (
                f"La hora UTC actual es {now.strftime('%H:%M')}. "
                f"Para hora local, especifica una ubicación."
            )
        tz_map = {
            "argentina": "UTC-3", "buenos aires": "UTC-3",
            "cordoba": "UTC-3",
            "españa": "UTC+1", "madrid": "UTC+1",
            "méxico": "UTC-6", "méxico df": "UTC-6",
            "usa": "UTC-5 a UTC-8", "new york": "UTC-5",
            "londres": "UTC+0", "tokio": "UTC+9",
        }
        location_lower = location.lower().strip()
        tz = tz_map.get(location_lower, "desconocida")
        return (
            f"En {location}, la zona horaria es {tz}. "
            f"La hora UTC actual es {now.strftime('%H:%M')}. "
            f"Para hora local exacta, consulta time.is/"
            f"{location.replace(' ', '-')}"
        )

    # ═══════════════════════════════════════════════════════════
    # ESTADÍSTICAS
    # ═══════════════════════════════════════════════════════════
    def get_api_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas de llamadas a APIs."""
        return self._api_stats.copy()

# ============================================================================
# EXPORTS
# ============================================================================
__all__ = ["ResearcherSources"]
