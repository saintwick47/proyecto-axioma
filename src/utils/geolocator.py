#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Geolocalizador (Geolocation Utility)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/utils/geolocator.py
# Autor: SaintWick
# Fecha: 2026-03-18
# Propósito: Detectar ubicación, fecha y hora del usuario para consultas contextuales
# Logging: tools/detailed_logger.py → logs/reg_error.txt
# ═══════════════════════════════════════════════════════════════
import logging
import socket
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path
from config.settings import settings

logger = logging.getLogger(__name__)

# ============================================================================
# ✅ INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

# ============================================================================
# ✅ HELPERS DE LOGGING ESPECÍFICOS PARA GEOLOCALIZACIÓN
# ============================================================================
def _log_event(category: str, message: str, level: str = "INFO", extra: Dict[str, Any] = None):
    """Registra evento genérico de geolocalización."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 geolocator.py:43] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra_info: Dict[str, Any] = None):
    """Registra error con traceback completo."""
    if not LOGGER_AVAILABLE:
        logger.error(f"geolocator - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, extra_info=extra_info,
                         function_name=f"geolocator.{context}")
    except Exception:
        logger.error(f"geolocator - {context}: {error}")

def _log_location(method: str, location: str, success: bool, duration: float = None,
                  source: str = "", cache_hit: bool = False):
    """Registra detección de ubicación con métricas."""
    if not LOGGER_AVAILABLE:
        return
    extra = {
        "method": method,
        "location": location,
        "success": success,
        "source": source,
        "cache_hit": cache_hit,
    }
    if duration is not None:
        extra["duration_ms"] = round(duration * 1000, 2)
    _log_event("GEOLOCATION", f"[{'✓' if success else '✗'}] {method} → {location}",
               level="INFO" if success else "WARNING", extra=extra)

# Fallback location configurable (default: user's known location)
DEFAULT_LOCATION = getattr(settings, 'default_location', 'Córdoba, Argentina')
DEFAULT_TIMEZONE = getattr(settings, 'default_timezone', 'America/Argentina/Cordoba')

# IP Geolocation API (free, no key required)
IP_API_URL = "http://ip-api.com/json/"
IP_API_TIMEOUT = 5  # seconds


class GeoLocation:
    """Información de geolocalización del usuario."""

    def __init__(
        self,
        city: str,
        region: str,
        country: str,
        country_code: str,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        timezone: Optional[str] = None,
        ip: Optional[str] = None,
    ):
        self.city = city
        self.region = region
        self.country = country
        self.country_code = country_code
        self.lat = lat
        self.lon = lon
        self.timezone = timezone
        self.ip = ip

    @property
    def full_location(self) -> str:
        """Retorna ubicación completa formateada."""
        if self.city and self.country:
            return f"{self.city}, {self.country}"
        elif self.city:
            return self.city
        elif self.country:
            return self.country
        return DEFAULT_LOCATION

    @property
    def local_datetime(self) -> Dict[str, Any]:
        """Retorna fecha y hora local."""
        try:
            import pytz
            tz = pytz.timezone(self.timezone or DEFAULT_TIMEZONE)
            local_now = datetime.now(tz)
            return {
                "datetime": local_now.isoformat(),
                "date": local_now.strftime("%Y-%m-%d"),
                "time": local_now.strftime("%H:%M:%S"),
                "timezone": self.timezone or DEFAULT_TIMEZONE,
                "utc_offset": local_now.strftime("%z"),
            }
        except Exception:
            now = datetime.now(timezone.utc)
            return {
                "datetime": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "timezone": "UTC",
                "utc_offset": "+0000",
            }

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "city": self.city,
            "region": self.region,
            "country": self.country,
            "country_code": self.country_code,
            "lat": self.lat,
            "lon": self.lon,
            "timezone": self.timezone,
            "ip": self.ip,
            "full_location": self.full_location,
            "local_datetime": self.local_datetime,
        }

    def __str__(self) -> str:
        return f"{self.full_location} ({self.timezone or DEFAULT_TIMEZONE})"


class Geolocator:
    """
    Detecta ubicación del usuario mediante múltiples métodos.
    Orden de prioridad:
    1. Ubicación explícita en la query
    2. Timezone del sistema (/etc/timezone)
    3. Cache válido
    4. Geolocalización por IP
    5. settings.default_location
    6. Hardcoded fallback
    """

    def __init__(self, use_ip_lookup: bool = True, timeout: int = IP_API_TIMEOUT):
        self.use_ip_lookup = use_ip_lookup
        self.timeout = timeout
        self._cached_location: Optional[GeoLocation] = None
        self._cache_ttl_seconds = 3600
        self._cache_timestamp: Optional[float] = None
        _log_event("GEOLOCATION", f"Geolocator initialized: use_ip_lookup={use_ip_lookup}, timeout={timeout}s")

    def _is_cache_valid(self) -> bool:
        """Verifica si la ubicación cacheada está vigente."""
        if not self._cached_location or not self._cache_timestamp:
            return False
        is_valid = (datetime.now().timestamp() - self._cache_timestamp) < self._cache_ttl_seconds
        if is_valid:
            _log_event("GEOLOCATION", "Cache hit - using cached location", extra={
                "cached_location": self._cached_location.full_location if self._cached_location else None,
                "cache_age_seconds": round(datetime.now().timestamp() - self._cache_timestamp, 2)
            })
        return is_valid

    def _detect_by_ip(self) -> Optional[GeoLocation]:
        """Detecta ubicación mediante IP-API.com (sin API key)."""
        if not self.use_ip_lookup:
            _log_event("GEOLOCATION", "IP lookup disabled by config", level="DEBUG")
            return None
        start = datetime.now()
        _log_event("GEOLOCATION", f"Attempting IP geolocation via {IP_API_URL}", extra={"timeout": self.timeout})
        try:
            with urllib.request.urlopen(IP_API_URL, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
                duration = (datetime.now() - start).total_seconds()
                if data.get("status") == "success":
                    location = GeoLocation(
                        city=data.get("city", ""),
                        region=data.get("regionName", ""),
                        country=data.get("country", ""),
                        country_code=data.get("countryCode", ""),
                        lat=data.get("lat"),
                        lon=data.get("lon"),
                        timezone=data.get("timezone"),
                        ip=data.get("query"),
                    )
                    _log_location("IP-API", location.full_location, success=True, duration=duration, source="ip-api.com")
                    return location
                else:
                    _log_location("IP-API", "failed", success=False, duration=duration, source="ip-api.com")
                    return None
        except urllib.error.URLError as e:
            duration = (datetime.now() - start).total_seconds()
            _log_error(e, "_detect_by_ip", extra_info={"duration_ms": round(duration * 1000, 2)})
            _log_location("IP-API", "failed", success=False, duration=duration, source="ip-api.com")
            return None
        except json.JSONDecodeError as e:
            duration = (datetime.now() - start).total_seconds()
            _log_error(e, "_detect_by_ip_json", extra_info={"duration_ms": round(duration * 1000, 2)})
            return None
        except Exception as e:
            duration = (datetime.now() - start).total_seconds()
            _log_error(e, "_detect_by_ip_unexpected", extra_info={"duration_ms": round(duration * 1000, 2)})
            return None

    def _detect_by_system_timezone(self) -> Optional[GeoLocation]:
        """Infiere ubicación desde /etc/timezone del sistema."""
        start = datetime.now()
        TZ_MAP = {
            "America/Argentina/Buenos_Aires": ("Buenos Aires", "Buenos Aires", "Argentina", "AR"),
            "America/Argentina/Cordoba":      ("Córdoba",      "Córdoba",      "Argentina", "AR"),
            "America/Argentina/Mendoza":      ("Mendoza",      "Mendoza",      "Argentina", "AR"),
            "America/Argentina/Rosario":      ("Rosario",      "Santa Fe",     "Argentina", "AR"),
            "America/Argentina/Salta":        ("Salta",        "Salta",        "Argentina", "AR"),
            "America/Argentina/Tucuman":      ("Tucumán",      "Tucumán",      "Argentina", "AR"),
            "America/Santiago":   ("Santiago",   "Metropolitana", "Chile",    "CL"),
            "America/Montevideo": ("Montevideo", "Montevideo",    "Uruguay",   "UY"),
            "America/Mexico_City":("Ciudad de México", "CDMX",     "México",    "MX"),
            "Europe/Madrid":      ("Madrid",      "Madrid",        "España",    "ES"),
        }
        try:
            tz_file = Path("/etc/timezone")
            if tz_file.exists():
                sys_tz = tz_file.read_text().strip()
            else:
                import subprocess
                r = subprocess.run(["timedatectl", "show", "--property=Timezone", "--value"],
                                   capture_output=True, text=True, timeout=2)
                sys_tz = r.stdout.strip() if r.returncode == 0 else ""
            if sys_tz and sys_tz in TZ_MAP:
                city, region, country, cc = TZ_MAP[sys_tz]
                duration = (datetime.now() - start).total_seconds()
                location = GeoLocation(city=city, region=region, country=country,
                                      country_code=cc, timezone=sys_tz)
                _log_location("system_timezone", location.full_location, success=True,
                             duration=duration, source=f"tz:{sys_tz}")
                return location
        except Exception as e:
            duration = (datetime.now() - start).total_seconds()
            _log_error(e, "_detect_by_system_timezone", extra_info={"duration_ms": round(duration * 1000, 2)})
        return None

    def get_location(self, explicit_location: Optional[str] = None) -> GeoLocation:
        """Obtiene ubicación del usuario."""
        _log_event("GEOLOCATION", f"get_location called: explicit_location={explicit_location}")

        # 1. Ubicación explícita
        if explicit_location and explicit_location.strip():
            _log_event("GEOLOCATION", f"Using explicit location: {explicit_location}", level="DEBUG")
            parts = [p.strip() for p in explicit_location.split(",") if p.strip()]
            city = parts[0] if len(parts) >= 1 else ""
            country = parts[1] if len(parts) >= 2 else ""
            location = GeoLocation(city=city, region="", country=country, country_code="",
                                  timezone=DEFAULT_TIMEZONE if country.lower() == "argentina" else None)
            _log_location("explicit_query", location.full_location, success=True, source="user_query")
            return location

        # 2. Timezone del sistema
        sys_location = self._detect_by_system_timezone()
        if sys_location:
            return sys_location

        # 3. Cache válido
        if self._is_cache_valid():
            return self._cached_location

        # 4. IP geolocation
        if self.use_ip_lookup:
            ip_location = self._detect_by_ip()
            if ip_location:
                self._cached_location = ip_location
                self._cache_timestamp = datetime.now().timestamp()
                return ip_location

        # 5. settings.default_location
        if hasattr(settings, "default_location") and settings.default_location:
            parts = [p.strip() for p in settings.default_location.split(",") if p.strip()]
            city = parts[0] if len(parts) >= 1 else ""
            country = parts[1] if len(parts) >= 2 else ""
            location = GeoLocation(city=city, region="", country=country, country_code="",
                                  timezone=getattr(settings, "default_timezone", DEFAULT_TIMEZONE))
            _log_location("settings_default", location.full_location, success=True, source="settings.py")
            return location

        # 6. Fallback hardcoded
        _log_event("GEOLOCATION", f"Geolocation failed, using fallback: {DEFAULT_LOCATION}", level="WARNING")
        fallback = GeoLocation(city="Córdoba", region="Córdoba", country="Argentina",
                              country_code="AR", timezone=DEFAULT_TIMEZONE)
        self._cached_location = fallback
        self._cache_timestamp = datetime.now().timestamp()
        _log_location("fallback_hardcoded", fallback.full_location, success=True, source="hardcoded")
        return fallback

    def clear_cache(self) -> None:
        """Limpia cache de geolocalización."""
        was_cached = self._cached_location is not None
        self._cached_location = None
        self._cache_timestamp = None
        _log_event("GEOLOCATION", f"Cache cleared (was_cached={was_cached})", level="DEBUG")

    def get_context_for_weather(self, query: str) -> Dict[str, Any]:
        """Genera contexto completo para consultas de clima."""
        start = datetime.now()
        _log_event("GEOLOCATION", f"get_context_for_weather called: query={query[:50]}...")

        explicit_location: Optional[str] = None
        import re
        location_match = re.search(
            r'\b(?:en|para|de)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ\s,]+?)(?:\s+hoy|\s+mañana|\s+esta|\s*$)',
            query, re.IGNORECASE
        )
        if location_match:
            candidate = location_match.group(1).strip()
            if candidate.lower() not in {'hoy', 'mañana', 'el', 'la', 'los', 'las', 'un', 'una'}:
                explicit_location = candidate

        location = self.get_location(explicit_location=explicit_location)
        local_dt = location.local_datetime
        duration = (datetime.now() - start).total_seconds()

        context = {
            "location": location.full_location,
            "city": location.city,
            "country": location.country,
            "timezone": location.timezone or DEFAULT_TIMEZONE,
            "current_date": local_dt["date"],
            "current_time": local_dt["time"],
            "current_datetime": local_dt["datetime"],
            "utc_offset": local_dt["utc_offset"],
            "query": query,
            "prompt_context": f"Ubicación: {location.full_location}\nFecha local: {local_dt['date']}\nHora local: {local_dt['time']} ({location.timezone or DEFAULT_TIMEZONE})\nConsulta: {query}",
        }
        _log_event("GEOLOCATION", f"Context generated", extra={
            "location": location.full_location,
            "duration_ms": round(duration * 1000, 2),
        })
        return context


# Instancia global para uso compartido (lazy init)
_geolocator_instance: Optional[Geolocator] = None

def get_geolocator(use_ip_lookup: bool = True) -> Geolocator:
    """Obtiene instancia singleton de Geolocator."""
    global _geolocator_instance
    if _geolocator_instance is None:
        _log_event("GEOLOCATION", f"Creating singleton Geolocator: use_ip_lookup={use_ip_lookup}")
        _geolocator_instance = Geolocator(use_ip_lookup=use_ip_lookup)
    return _geolocator_instance


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS (FIX INDENTATION)
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                              | Razón                    |
# |-------|-------------------------------------|--------------------------|
# | ~24   | ✅ try: con import indentado        | Fix IndentationError     |
# | ~25   | ✅ from tools.detailed_logger...    | Import dentro del try    |
# | ~35   | ✅ _log_event() helper nuevo        | Eventos genéricos        |
# | ~47   | ✅ _log_error() con traceback       | Diagnóstico de fallos    |
# | ~59   | ✅ _log_location() métricas         | Auditoría de detección   |
# | ~120  | ✅ Logging en __init__              | Trazabilidad de arranque |
# | ~135  | ✅ Logging en _is_cache_valid()     | Cache hits/misses        |
# | ~145  | ✅ Logging en _detect_by_ip()       | Llamadas IP-API + timing |
# | ~220  | ✅ Logging en get_location()        | Flujo completo           |
# | ~260  | ✅ Logging en get_context_for_weather| Contexto para clima     |
# ═══════════════════════════════════════════════════════════════
