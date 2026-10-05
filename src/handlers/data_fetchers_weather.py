#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — data_fetchers_weather.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/handlers/data_fetchers_weather.py
#
# ✅ v0.6.9v (REFACTOR): capa de CLIMA + GEOCODING extraída de
#   src/handlers/data_fetchers.py (el original queda intacto).
#
#   Qué se movió desde data_fetchers.py (sin cambios de comportamiento):
#     - Helper compartido `_log_api_call` (+ LOGGER_AVAILABLE / get_logger)
#     - Geocoding: `_COORDS_MAP`, caches, `_GEOCODING_NEGATIVE_TTL`,
#       `get_coordinates()`
#     - `WeatherFetcherMixin`: `fetch_weather`, `extract_city`,
#       `weather_code_to_emoji` + sus constantes `_CITY_PATTERNS` /
#       `_CITY_STOPWORDS` / `_CITY_NORMALIZE`
#
#   `DataFetchers` (la clase pública) permanece en data_fetchers.py y hereda
#   `WeatherFetcherMixin`; el módulo re-exporta `get_coordinates` y
#   `_log_api_call` para no romper la API pública
#   (`from .data_fetchers import DataFetchers, get_coordinates`).
#
#   Este módulo NO importa nada de data_fetchers.py → sin ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ============================================================================
# IMPORT SEGURO DE DETAILEDLOGGER
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


def _log_api_call(
    api_name: str,
    operation: str,
    success: bool,
    duration: float = None,
    response_size: int = None,
    error: str = None,
    extra_info: Dict[str, Any] = None,
):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            extra = {
                "api": api_name,
                "operation": operation,
                "success": success,
            }
            if duration is not None:
                extra["duration_ms"] = round(duration * 1000, 2)
            if response_size is not None:
                extra["response_bytes"] = response_size
            if error:
                extra["error"] = error
            if extra_info:
                extra.update(extra_info)
            level = "INFO" if success else "WARNING"
            log.log_event(
                "API_CALL",
                f"[{'✓' if success else '✗'}] {api_name}.{operation}",
                level=level,
                extra=extra,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 data_fetchers_weather.py] excepción degradada (intencional): {_d2e}")


# ============================================================================
# HELPER PARA OBTENER COORDENADAS
# ============================================================================

# ✅ v0.1.0: Coordenadas hardcodeadas ampliadas con ciudades turísticas comunes
_COORDS_MAP: Dict[str, Tuple[float, float]] = {
    "buenos aires": (-34.6037, -58.3816),
    "córdoba": (-31.4201, -64.1888),
    "cordoba": (-31.4201, -64.1888),
    "rosario": (-32.9442, -60.6505),
    "mendoza": (-32.8895, -68.8458),
    "la plata": (-34.9215, -57.9545),
    "san miguel de tucumán": (-26.8083, -65.2176),
    "tucumán": (-26.8083, -65.2176),
    "tucuman": (-26.8083, -65.2176),
    "salta": (-24.7859, -65.4117),
    "santa fe": (-31.6333, -60.7000),
    "mar del plata": (-38.0055, -57.5426),
    # ✅ v0.1.0: Ciudades turísticas / consultadas frecuentemente
    "villa general belgrano": (-31.9792, -64.5536),
    "bariloche": (-41.1335, -71.3103),
    "san carlos de bariloche": (-41.1335, -71.3103),
    "ushuaia": (-54.8019, -68.3030),
    "el calafate": (-50.3370, -72.2649),
    "mendoza city": (-32.8895, -68.8458),
    "iguazú": (-25.5956, -54.5854),
    "puerto iguazú": (-25.5956, -54.5854),
    "carlos paz": (-31.4201, -64.5000),
    "villa carlos paz": (-31.4201, -64.5000),
    "rio grande": (-53.7877, -67.7049),
    "rio gallegos": (-51.6230, -69.2168),
    "neuquén": (-38.9516, -68.0591),
    "neuquen": (-38.9516, -68.0591),
}

# ✅ v0.1.0: Cache de coordenadas obtenidas vía geocoding API
_geocoding_cache: Dict[str, Optional[Tuple[float, float]]] = {}
# ✅ Fase 8: el cache positivo (coordenadas encontradas) no necesita TTL
# — no cambian. El NEGATIVO (city_key -> None) sí lo necesita: sin esto,
# una falla transitoria (rate limit de Open-Meteo, blip de red, timeout
# de 5s) marca esa ciudad como "no encontrada" para siempre durante todo
# el uptime del daemon (systemd, proceso de larga duración) — ningún
# reintento futuro, aunque la API vuelva a estar disponible en segundos.
_geocoding_cache_negative_ts: Dict[str, float] = {}
_GEOCODING_NEGATIVE_TTL = 3600  # 1h


async def get_coordinates(city: str) -> Optional[Tuple[float, float]]:
    """
    Obtiene coordenadas (lat, lon) de una ciudad.

    Orden de resolución:
    1. Cache en memoria (ya consultado antes)
    2. Diccionario hardcodeado (_COORDS_MAP)
    3. Open-Meteo Geocoding API (fallback para cualquier ciudad del mundo)

    ✅ FIX: eliminado el paso intermedio de Geolocator.get_location(
    explicit_location=city) — SIEMPRE fallaba para este propósito.
    GeoLocation.lat/lon quedan en None en el path de ubicación explícita
    (geolocator.py solo parsea el string en city/country ahí, no geocodifica —
    diseño correcto para get_context_for_weather(), que solo necesita
    city/country/timezone, pero inútil acá). El chequeo `if location.lat and
    location.lon` nunca era verdadero, así que este paso era 100% muerto:
    TODA ciudad fuera de _COORDS_MAP caía siempre al paso 4.
    ✅ FIX: paso 4 (ahora paso 3) usaba urllib.request.urlopen() SÍNCRONO
    dentro de una función llamada sin await desde fetch_weather() (async) —
    bloqueaba el event loop ENTERO hasta 5s en cada consulta de clima para
    cualquier ciudad no hardcodeada (que es la enorme mayoría, incluida
    cualquier localidad chica). Ahora es async + httpx.AsyncClient, igual
    que el resto de este archivo.
    """
    city_key = city.strip().lower()

    # 1. Cache
    if city_key in _geocoding_cache:
        cached = _geocoding_cache[city_key]
        if cached is not None:
            return cached
        # ✅ Fase 8: negativo — solo válido si no expiró el TTL
        cached_at = _geocoding_cache_negative_ts.get(city_key, 0.0)
        if (time.time() - cached_at) < _GEOCODING_NEGATIVE_TTL:
            return None
        # Expiró — se trata como miss, se limpia y sigue a re-consultar
        del _geocoding_cache[city_key]
        _geocoding_cache_negative_ts.pop(city_key, None)

    # 2. Diccionario hardcodeado
    if city_key in _COORDS_MAP:
        coords = _COORDS_MAP[city_key]
        _geocoding_cache[city_key] = coords
        return coords

    # 3. Open-Meteo Geocoding API — soporta cualquier ciudad del mundo
    try:
        encoded_city = urllib.parse.quote(city)
        geocoding_url = (
            f"https://geocoding-api.open-meteo.com/v1/search?"
            f"name={encoded_city}&count=1&language=es&format=json"
        )
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(geocoding_url)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        if results:
            first = results[0]
            lat = first.get("latitude")
            lon = first.get("longitude")
            resolved_name = first.get("name", city)
            if lat is not None and lon is not None:
                coords = (float(lat), float(lon))
                _geocoding_cache[city_key] = coords
                # También cachear por el nombre resuelto
                resolved_key = resolved_name.strip().lower()
                _geocoding_cache[resolved_key] = coords
                logger.info(
                    f"Coordinates from Geocoding API: {city} → "
                    f"{resolved_name} ({lat}, {lon})"
                )
                return coords

        logger.warning(f"Geocoding API: no results for '{city}'")
        _geocoding_cache[city_key] = None
        _geocoding_cache_negative_ts[city_key] = time.time()
    except Exception as e:
        logger.warning(f"Geocoding API failed for {city}: {e}")
        _geocoding_cache[city_key] = None
        _geocoding_cache_negative_ts[city_key] = time.time()

    return None


class WeatherFetcherMixin:
    """Fetch de clima (Open-Meteo) + extracción de ciudad.

    Extraído de DataFetchers (50/50): este mixin se ocupa del sub-dominio de
    clima — geocoding, extracción de ciudad del query y mapeo de códigos WMO.
    `DataFetchers` lo hereda, así `self.fetch_weather()` / `self.extract_city()`
    siguen disponibles sin cambios.
    """

    # ✅ v0.1.0: Patrones regex para extraer ciudad del query
    _CITY_PATTERNS: List[str] = [
        # "clima en/para/de Villa General Belgrano"
        r'(?:en|para|de)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ][a-záéíóúñ]+)*)',
        # "clima Villa General Belgrano" (sin preposición)
        r'clima\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ][a-záéíóúñ]+)*)',
        # "pronóstico para Villa General Belgrano"
        r'pron[oó]stico\s+(?:para\s+|en\s+|de\s+)?([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ][a-záéíóúñ]+)*)',
        # "temperatura en Villa General Belgrano"
        r'temperatura\s+(?:en|de|para)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ][a-záéíóúñ]+)*)',
    ]

    # Stopwords que NO son ciudades
    _CITY_STOPWORDS: set = {
        'hoy', 'mañana', 'pasado', 'esta', 'este', 'semana',
        'actual', 'ahora', 'lunes', 'martes', 'miércoles',
        'jueves', 'viernes', 'sábado', 'domingo',
    }

    # Diccionario de normalización: variante → nombre canónico
    _CITY_NORMALIZE: Dict[str, str] = {
        "buenos aires": "Buenos Aires",
        "córdoba": "Córdoba",
        "cordoba": "Córdoba",
        "rosario": "Rosario",
        "mendoza": "Mendoza",
        "la plata": "La Plata",
        "tucumán": "San Miguel de Tucumán",
        "tucuman": "San Miguel de Tucumán",
        "salta": "Salta",
        "santa fe": "Santa Fe",
        "mar del plata": "Mar del Plata",
        "villa general belgrano": "Villa General Belgrano",
        # ✅ 2026-08-25: abreviaturas comunes (fallaban con "no se encontró ubicación")
        "vgb": "Villa General Belgrano",
        "cordoba vgb": "Villa General Belgrano",
        "cba": "Córdoba",
        "mdp": "Mar del Plata",
        "v.g.b.": "Villa General Belgrano",
        "bariloche": "Bariloche",
        "san carlos de bariloche": "Bariloche",
        "ushuaia": "Ushuaia",
        "el calafate": "El Calafate",
        "iguazú": "Puerto Iguazú",
        "iguazu": "Puerto Iguazú",
        "puerto iguazú": "Puerto Iguazú",
        "carlos paz": "Villa Carlos Paz",
        "villa carlos paz": "Villa Carlos Paz",
        "neuquén": "Neuquén",
        "neuquen": "Neuquén",
    }

    async def fetch_weather(self, query: str) -> Dict[str, Any]:
        """
        Open-Meteo: clima en tiempo real.

        Args:
            query: Consulta del usuario.

        Returns:
            Dict con content, confidence, sources.
        """
        start = time.time()
        logger.debug(f"DataFetchers: fetch_weather started: {query[:50]}...")

        city = self.extract_city(query) or "Buenos Aires"
        coords = await get_coordinates(city)

        # ✅ 2026-08-25: fallback progresivo — si el nombre completo no resuelve,
        # probar recortando palabras de adelante ("cordoba villa general belgrano"
        # → "villa general belgrano" → "general belgrano") y variantes del diccionario.
        if not coords:
            parts = city.split()
            candidates = []
            for i in range(1, len(parts)):
                candidates.append(" ".join(parts[i:]))
            for key, value in self._CITY_NORMALIZE.items():
                if key in city.lower() and value not in candidates:
                    candidates.append(value)
            for cand in candidates:
                coords = await get_coordinates(cand)
                if coords:
                    city = cand
                    break

        if not coords:
            logger.error(f"Weather failed: location not found for {city}")
            return {
                "content": (
                    f"🌍 No encontré la ubicación **'{city}'**.\n"
                    f"Probá con el nombre completo (ej: *Villa General Belgrano*, "
                    f"*Córdoba*, *Mar del Plata*) o sin abreviaturas."
                ),
                "confidence": 0.3,
                "sources": [],
                "success": False,
            }

        lat, lon = coords
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,"
            f"weather_code,wind_speed_10m&timezone=auto"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                _log_api_call(
                    "open_meteo", "request", success=True,
                    extra_info={"city": city, "lat": lat, "lon": lon},
                )
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                current = data["current"]
                temp = current["temperature_2m"]
                humidity = current["relative_humidity_2m"]
                wind = current["wind_speed_10m"]
                weather_code = current["weather_code"]
                weather_emoji = self.weather_code_to_emoji(weather_code)

                content = (
                    f"{weather_emoji} Clima en {city}:\n"
                    f"• Temperatura: {temp}°C\n"
                    f"• Humedad: {humidity}%\n"
                    f"• Viento: {wind} km/h"
                )

                duration = time.time() - start
                _log_api_call(
                    "open_meteo", "response", success=True,
                    duration=duration, response_size=len(content),
                )
                logger.debug(
                    f"DataFetchers: Weather completed: {city} "
                    f"({duration*1000:.1f}ms)"
                )

                return {
                    "content": content,
                    "confidence": 1.0,
                    "sources": ["api.open-meteo.com"],
                }
        except Exception as e:
            duration = time.time() - start
            _log_api_call(
                "open_meteo", "response", success=False,
                duration=duration, error=str(e),
            )
            raise

    @staticmethod
    def extract_city(query: str) -> Optional[str]:
        """
        Extrae ciudad del query.

        ✅ v0.1.0: Flujo mejorado
        1. Regex: buscar nombre después de "en/para/de"
        2. Diccionario de normalización (ciudades con alias)
        3. Fallback a None (DataFetchers usará "Buenos Aires" como default)

        FIX: "dame el clima para villa general belgrano" → "Villa General Belgrano"
        ANTES: No matcheaba → None → "Buenos Aires"
        """
        if not query:
            return None

        # 1. Intentar regex — captura cualquier nombre de ciudad
        for pattern in WeatherFetcherMixin._CITY_PATTERNS:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                # Filtrar stopwords
                if candidate.lower() not in WeatherFetcherMixin._CITY_STOPWORDS:
                    # ✅ 2026-08-25: si el candidato contiene un nombre canónico
                    # (ej. "cordoba villa general belgrano" → "villa general
                    # belgrano"), preferir el más específico del diccionario.
                    best = None
                    for key, value in WeatherFetcherMixin._CITY_NORMALIZE.items():
                        if key in candidate.lower() and (best is None or len(key) > len(best)):
                            best, best_value = key, value
                    if best is not None:
                        logger.debug(f"extract_city: regex+dict → '{candidate}' → '{best_value}'")
                        return best_value
                    # Normalizar si está en el diccionario
                    normalized = WeatherFetcherMixin._CITY_NORMALIZE.get(
                        candidate.lower(), candidate.title()
                    )
                    logger.debug(f"extract_city: regex → '{candidate}' → '{normalized}'")
                    return normalized

        # 2. Buscar en diccionario de normalización (substring match)
        query_lower = query.lower()
        for key, value in WeatherFetcherMixin._CITY_NORMALIZE.items():
            if key in query_lower:
                logger.debug(f"extract_city: dict → '{key}' → '{value}'")
                return value

        # 3. No encontrado
        logger.debug(f"extract_city: no match for '{query[:50]}'")
        return None

    @staticmethod
    def weather_code_to_emoji(code: int) -> str:
        """Convierte código WMO a emoji."""
        if code == 0:
            return "☀️"
        elif code in [1, 2, 3]:
            return "🌤️"
        elif code in [45, 48]:
            return "🌫️"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
            return "🌧️"
        elif code in [71, 73, 75, 85, 86]:
            return "❄️"
        elif code in [95, 96, 99]:
            return "⛈️"
        else:
            return "🌤️"


__all__ = [
    "WeatherFetcherMixin",
    "get_coordinates",
    "_log_api_call",
    "_COORDS_MAP",
    "_geocoding_cache",
]
