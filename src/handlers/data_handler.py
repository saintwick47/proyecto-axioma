#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Data Handler con Caché L2 + Ambigüedad Híbrido
# Ruta: src/handlers/data_handler.py
# Autor: SaintWick
# Versión: 0.1.1 (v4.1.1 — Fix CacheL2 tuple return)
# Cambios:
#   v0.0.8 — Detección de ambigüedad en requests de usuario
#   v0.0.9 — Caché L2 SQLite para respuestas de APIs
#   v0.0.9 — REFACT — 50/50: APIs+Parsers → data_fetchers.py
#   v0.1.0 — ✅ v4.1.0: Despacho dinámico (dict) reemplazado por
#            match/case estático. FlowMapper traza 100% las llamadas.
#   v0.1.1 — ✅ v4.1.1: FIX CacheL2.get() retorna (value, expires_at).
#            _get_cached_response() desempaqueta tupla correctamente.
# ═══════════════════════════════════════════════════════════════
"""
Resolvedor determinista para consultas de datos en tiempo real (clima, finanzas, hora, noticias)
que omite la inferencia LLM. Implementa caché persistente L2 (SQLite) con TTL dinámico por
dominio y validación de ambigüedad pre-ejecución.

── REFACTORIZACIÓN 50/50 ──────────────────────────────────────
  La capa de acceso a datos fue extraída a:
    → data_fetchers.py (DataFetchers, get_coordinates)
  Este archivo conserva:
    → Caché L2 (SQLite) con TTL dinámico
    → Detección de ambigüedad pre-ejecución
    → Orquestación de handlers (DataHandler)
    → Construcción de Response

Soporta:
- weather_query → Open-Meteo API (via DataFetchers)
- finance_query → Tavily API (via DataFetchers)
- time_query → datetime del sistema (via DataFetchers)
- news_query → NewsAPI (via DataFetchers)
"""
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone
import logging
import time
import traceback

from src.domain.entities import Response, Message
from config.settings import settings

# ── Import desde data_fetchers (refactorización 50/50) ──────────
from .data_fetchers import DataFetchers, get_coordinates

# ✅ CORREGIDO: Logger definido en scope global
logger = logging.getLogger(__name__)


# ============================================================================
# ✅ INTEGRACIÓN AMBIGÜEDAD - FASE 1
# ============================================================================
try:
    from src.utils.ambiguity_detector import AmbiguityDetector, detect_ambiguity
    AMBIGUITY_DETECTOR_AVAILABLE = True
except ImportError:
    AMBIGUITY_DETECTOR_AVAILABLE = False
    logger.warning("AmbiguityDetector no disponible - usando fallback")


# ============================================================================
# ✅ v0.0.9: INTEGRACIÓN CACHÉ L2 PARA APIs
# ============================================================================
try:
    from src.router.cache_l2 import get_cache_l2
    _CACHE_L2_AVAILABLE = True
except ImportError:
    try:
        from src.router.cache_l2 import CacheL2
        _cache_l2_instance = None
        def get_cache_l2():
            global _cache_l2_instance
            if _cache_l2_instance is None:
                _cache_l2_instance = CacheL2()
            return _cache_l2_instance
        _CACHE_L2_AVAILABLE = True
    except ImportError:
        _CACHE_L2_AVAILABLE = False
        logger.debug("CacheL2 no disponible - APIs sin caché persistente")


# ============================================================================
# ✅ INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


# ============================================================================
# ✅ HELPERS DE LOGGING ESPECÍFICOS PARA DATA HANDLER
# ============================================================================

def _log_event(category: str, message: str, level: str = "INFO", extra: Dict[str, Any] = None):
    """Registra evento genérico en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 data_handler.py:106] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra_info: Dict[str, Any] = None):
    """Registra error con traceback completo."""
    if not LOGGER_AVAILABLE:
        logger.error(f"data_handler - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error,
                context=context,
                extra_info=extra_info,
                function_name=f"data_handler.{context}"
            )
    except Exception:
        logger.error(f"data_handler - {context}: {error}")


def _log_handler_operation(task_type: str, operation: str, success: bool,
                           duration: float = None, confidence: float = None):
    """Registra operación del handler con métricas."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            extra = {
                "task_type": task_type,
                "operation": operation,
                "success": success,
            }
            if duration is not None:
                # Los callers pasan `latency` YA en ms (diff*1000); antes se
                # multiplicaba otra vez por 1000 → duration_ms salía ×1000
                # (ej. 1991509.0 ms para ~1991 ms reales). Fix v0.6.8ii.
                extra["duration_ms"] = round(duration, 2)
            if confidence is not None:
                extra["confidence"] = round(confidence, 2)
            log.log_event(
                "DATA_HANDLER",
                f"[{'✓' if success else '✗'}] {task_type}.{operation}",
                level="INFO" if success else "ERROR",
                extra=extra,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 data_handler.py:154] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ✅ HELPERS DE AMBIGÜEDAD
# ============================================================================

def _log_ambiguity_detection(request: str, level: str, score: int, patterns: List[str], duration_ms: float):
    """Registra detección de ambigüedad con métricas."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            extra = {
                "ambiguity_level": level,
                "ambiguity_score": score,
                "patterns_found": patterns,
                "detection_duration_ms": round(duration_ms, 2),
                "request_preview": request[:100] if request else ""
            }
            log.log_event(
                "AMBIGUITY_DETECTION",
                f"Detectado nivel: {level}",
                level="INFO",
                extra=extra,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 data_handler.py:182] excepción degradada (intencional): {_d2e}")


def _build_warning_message(ambiguity_level: str, patterns: List[str]) -> str:
    """Construye mensaje de advertencia para el usuario."""
    warnings = {
        "high": (
            "⚠️ Tu petición es amplia. Generaré una solución práctica "
            "con limitaciones documentadas."
        ),
        "medium": (
            "⚠️ Optimizaré según criterios estándar. Revisá los "
            "trade-offs en el código."
        ),
    }
    base_warning = warnings.get(ambiguity_level, "")
    if patterns:
        base_warning += f"\n\nPatrones detectados: {', '.join(patterns)}"
    base_warning += (
        "\n\n¿El resultado no es lo que buscabas? Usá el feedback para mejorar."
    )
    return base_warning


# ============================================================================
# CLASE PRINCIPAL — DataHandler
# ============================================================================

class DataHandler:
    """
    Resolvedor determinista para consultas de datos en tiempo real.

    Implementa caché persistente L2 (SQLite) con TTL dinámico por dominio
    y validación de ambigüedad pre-ejecución.

    Delega acceso a datos a DataFetchers (data_fetchers.py).

    Latencia esperada:
        <5ms (cache hit) vs 2-5s (API call)
    """

    # ✅ v0.0.9: TTL por tipo de API
    _CACHE_TTL_MAP = {
        "weather_query": 1800,   # 30 minutos (clima cambia lento)
        "finance_query": 900,    # 15 minutos (mercados en horario)
        "time_query": 60,        # 1 minuto (solo para evitar spam)
        "news_query": 1800,      # 30 minutos (noticias se actualizan cada hora)
    }

    def __init__(self, timeout: int = 10):
        """Inicializa el handler."""
        start_init = time.time()
        self.timeout = timeout

        # ✅ REFACT: Capa de datos delegada a DataFetchers
        self.fetchers = DataFetchers(timeout=timeout)

        # ✅ NUEVO: Inicializar detector de ambigüedad
        self.ambiguity_detector = None
        if AMBIGUITY_DETECTOR_AVAILABLE:
            try:
                self.ambiguity_detector = AmbiguityDetector()
            except Exception as e:
                logger.warning(f"Failed to initialize AmbiguityDetector: {e}")

        # ✅ v0.0.9: Inicializar caché L2 para APIs
        self._cache = None
        if _CACHE_L2_AVAILABLE:
            try:
                self._cache = get_cache_l2()
                logger.debug("DataHandler: Caché L2 para APIs activado")
            except Exception as e:
                logger.warning(f"DataHandler: Caché L2 no disponible: {e}")
                self._cache = None

        duration = time.time() - start_init
        _log_event("DATA_HANDLER", "DataHandler initialized", extra={
            "timeout": self.timeout,
            "init_duration_ms": round(duration * 1000, 2),
            "ambiguity_detector_available": AMBIGUITY_DETECTOR_AVAILABLE,
            "cache_l2_available": self._cache is not None
        })
        logger.info(
            f"DataHandler initialized: timeout={self.timeout}s, "
            f"cache_l2={'✅' if self._cache else '❌'}"
        )

    # ═══════════════════════════════════════════════════════════
    # COMPATIBILIDAD — Proxies a DataFetchers
    # ═══════════════════════════════════════════════════════════

    async def _weather(self, query: str) -> Dict[str, Any]:
        """Proxy → DataFetchers.fetch_weather()"""
        # ✅ FIX: faltaba await — fetch_weather() es async, sin esto esto
        # devolvía la coroutine sin ejecutar, no el dict de resultado.
        return await self.fetchers.fetch_weather(query)

    async def _finance(self, query: str) -> Dict[str, Any]:
        """Proxy → DataFetchers.fetch_finance()"""
        return await self.fetchers.fetch_finance(query)

    async def _time(self, query: str) -> Dict[str, Any]:
        """Proxy → DataFetchers.fetch_time()"""
        return await self.fetchers.fetch_time(query)

    async def _news(self, query: str) -> Dict[str, Any]:
        """Proxy → DataFetchers.fetch_news()"""
        return await self.fetchers.fetch_news(query)

    def _extract_city(self, query: str) -> Optional[str]:
        """Proxy → DataFetchers.extract_city()"""
        return self.fetchers.extract_city(query)

    def _weather_code_to_emoji(self, code: int) -> str:
        """Proxy → DataFetchers.weather_code_to_emoji()"""
        return self.fetchers.weather_code_to_emoji(code)

    # ═══════════════════════════════════════════════════════════
    # ✅ v0.0.9: MÉTODOS DE CACHÉ L2
    # ═══════════════════════════════════════════════════════════

    def _get_cached_response(self, task_type: str, query: str) -> Optional[Dict[str, Any]]:
        """Busca respuesta cacheada para una query de API."""
        if not self._cache:
            return None
        try:
            # Normalizar: minúsculas, sin espacios extra, sin acentos básicos
            normalized = query.strip().lower()
            cache_key = f"api_{task_type}_{normalized}"
            result = self._cache.get(cache_key)
            # ✅ v0.1.1 FIX: CacheL2.get() retorna (value, expires_at)
            # Desempaquetar tupla para obtener solo el diccionario de valor
            if isinstance(result, tuple):
                return result[0]  # value
            return result
        except Exception as e:
            logger.debug(f"DataHandler: Error leyendo caché L2: {e}")
            return None

    def _set_cached_response(self, task_type: str, query: str, data: Dict[str, Any]) -> None:
        """Guarda respuesta de API en caché con TTL según tipo."""
        if not self._cache:
            return
        try:
            normalized = query.strip().lower()
            cache_key = f"api_{task_type}_{normalized}"
            effective_ttl = self._CACHE_TTL_MAP.get(task_type, 1800)
            self._cache.set(cache_key, data, ttl=effective_ttl)
            logger.debug(
                f"DataHandler: Respuesta cacheada (ttl={effective_ttl}s): "
                f"{task_type}"
            )
        except Exception as e:
            logger.debug(f"DataHandler: Error escribiendo caché L2: {e}")

    # ═══════════════════════════════════════════════════════════
    # MÉTODO PRINCIPAL
    # ═══════════════════════════════════════════════════════════

    async def handle(
        self,
        message: Message,
        task_type: str,
        session_id: Optional[str] = None,
    ) -> Response:
        """
        Procesa request de datos y retorna Response.

        Flujo:
        1. Verificar caché L2 (si hay hit, retornar inmediato)
        2. Detectar ambigüedad en el request
        3. Despachar al fetcher correspondiente (ESTÁTICO)
        4. Guardar en caché si fue exitoso
        5. Construir Response con metadata de ambigüedad
        """
        start_time = datetime.now(timezone.utc)
        _log_event(
            "DATA_HANDLER", f"handle() started: {task_type}",
            extra={
                "session_id": session_id,
                "query_preview": (
                    message.content[:50] if message.content else ""
                ),
            },
        )

        # ── 1. VERIFICAR CACHÉ L2 ────────────────────────────────
        cached = self._get_cached_response(task_type, message.content)
        if cached is not None:
            latency = (
                (datetime.now(timezone.utc) - start_time).total_seconds()
                * 1000
            )
            _log_handler_operation(
                task_type, "handle_cache_hit", success=True,
                duration=latency, confidence=cached.get("confidence", 1.0),
            )
            _log_event(
                "DATA_HANDLER", f"CACHE HIT: {task_type}",
                extra={
                    "latency_ms": round(latency, 2),
                    "query_preview": (
                        message.content[:50] if message.content else ""
                    ),
                },
            )
            logger.info(
                f"DataHandler CACHE HIT: {task_type} ({latency:.1f}ms)"
            )
            return Response(
                content=cached.get("content", cached.get("raw", "")),
                session_id=session_id,
                model_used="data_handler_cache",
                task_type=task_type,
                latency_ms=latency,
                confidence=cached.get("confidence", 1.0),
                sources=cached.get("sources", []),
                validation_passed=True,
                error=None,
                presentation_style="direct",
                metadata={
                    "cache_hit": True,
                    "cache_source": "l2_sqlite",
                    "cached_at": cached.get("cached_at", ""),
                },
            )

        # ── 2. DETECCIÓN DE AMBIGÜEDAD ───────────────────────────
        ambiguity_level = "none"
        ambiguity_score = 0
        ambiguity_patterns = []
        detection_duration_ms = 0.0
        show_warning = False
        warning_message = None

        if (
            AMBIGUITY_DETECTOR_AVAILABLE
            and self.ambiguity_detector
            and message.content
        ):
            try:
                detect_start = time.time()
                (
                    ambiguity_level,
                    ambiguity_score,
                    ambiguity_patterns,
                ) = self.ambiguity_detector.detect_ambiguity(
                    message.content
                )
                detection_duration_ms = (
                    (time.time() - detect_start) * 1000
                )
                _log_ambiguity_detection(
                    message.content, ambiguity_level,
                    ambiguity_score, ambiguity_patterns,
                    detection_duration_ms,
                )
                if ambiguity_level in ["high", "medium"]:
                    show_warning = True
                    warning_message = _build_warning_message(
                        ambiguity_level, ambiguity_patterns
                    )
            except Exception as e:
                logger.warning(f"Ambiguity detection failed: {e}")
                ambiguity_level = "none"

        # ── 3. DESPACHAR AL FETCHER (ESTÁTICO) ───────────────────
        # ✅ v4.1.0: Despacho estático vía match/case.
        # El FlowMapper y el AST pueden trazar la llamada directa
        # a DataFetchers.fetch_*(), eliminando el estado AISLADO/PARCIAL.
        try:
            match task_type:
                case "weather_query":
                    result = await self.fetchers.fetch_weather(message.content)
                case "finance_query":
                    result = await self.fetchers.fetch_finance(message.content)
                case "time_query":
                    result = await self.fetchers.fetch_time(message.content)
                case "news_query":
                    result = await self.fetchers.fetch_news(message.content)
                case _:
                    _log_event(
                        "DATA_HANDLER",
                        f"Unsupported task_type: {task_type}",
                        level="WARNING",
                    )
                    return self._error_response(
                        f"Task type no soportado: {task_type}",
                        task_type, session_id, start_time,
                    )

            latency = (
                (datetime.now(timezone.utc) - start_time).total_seconds()
                * 1000
            )
            _log_handler_operation(
                task_type, "handle", success=True,
                duration=latency, confidence=result.get("confidence", 1.0),
            )

            # ── 4. GUARDAR EN CACHÉ ──────────────────────────────
            if result and not result.get("error"):
                result_with_timestamp = {
                    **result,
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                }
                self._set_cached_response(
                    task_type, message.content, result_with_timestamp
                )

            # ── 5. CONSTRUIR RESPONSE ─────────────────────────────
            response = Response(
                content=result["content"],
                session_id=session_id,
                model_used="data_handler",
                task_type=task_type,
                latency_ms=latency,
                confidence=result.get("confidence", 1.0),
                sources=result.get("sources", []),
                validation_passed=True,
                error=None,
                presentation_style="direct",
            )

            # Metadata de ambigüedad
            response.metadata = {
                "ambiguity_level": ambiguity_level,
                "ambiguity_score": ambiguity_score,
                "ambiguity_patterns": ambiguity_patterns,
                "detection_duration_ms": detection_duration_ms,
                "show_warning": show_warning,
                "warning_message": warning_message,
                "cache_hit": False,
            }

            return response

        except Exception as e:
            _log_error(e, "handle", extra_info={
                "task_type": task_type,
                "session_id": session_id,
                "query_preview": (
                    message.content[:50] if message.content else ""
                ),
            })
            _log_handler_operation(task_type, "handle", success=False)
            logger.error(f"DataHandler error en {task_type}: {e}")
            return self._error_response(
                str(e), task_type, session_id, start_time
            )

    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════

    def _error_response(
        self,
        error_msg: str,
        task_type: str,
        session_id: Optional[str],
        start_time: datetime,
    ) -> Response:
        """Crea Response de error."""
        latency = (
            (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        )
        _log_event(
            "DATA_HANDLER", "Error response generated",
            extra={
                "task_type": task_type,
                "error": error_msg[:100],
                "latency_ms": round(latency, 2),
            },
        )
        return Response(
            content=f"No pude obtener los datos solicitados: {error_msg}",
            session_id=session_id,
            model_used="data_handler",
            task_type=task_type,
            latency_ms=latency,
            confidence=0.0,
            sources=[],
            validation_passed=False,
            error=error_msg,
        )
