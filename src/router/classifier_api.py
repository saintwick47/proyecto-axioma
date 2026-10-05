#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# classifier_api.py - Detector de APIs: Patrones + Integración Externa
# Ruta: /ruta/a/axioma/src/router/classifier_api.py
# Autor: SaintWick
# Versión: 0.1.8
#
# Detecta consultas especiales (clima, finanzas, noticias, búsqueda web, hora)
# mediante patrones regex y las enriquece con integración de APIs externas
# (Tavily, Serper, News API). Incluye extracción de ubicación y normalización
# de texto para robustez ante acentos y mayúsculas.
# ═══════════════════════════════════════════════════════════════

import logging
import time
import re
import hashlib
import unicodedata
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from config.settings import settings
from src.domain.types import TaskType
from src.router.classifier_core import _log_error, _log_router

logger = logging.getLogger(__name__)


# ============================================================================
# ✅ FIX AUDITORÍA (AUD-CAS-001) — Circuit breaker liviano por proveedor
# Mismo patrón ya usado en searcher.py y external_apis.py: sin esto, un
# proveedor CONFIGURADO (con API key) pero caído a nivel red hace que cada
# consulta pague su timeout completo (10s) antes de caer al siguiente, sin
# memoria de fallos recientes. "Sin API key" no cuenta como fallo (retorna
# instantáneo, no toca la red).
# ============================================================================
from src.utils.provider_circuit import ProviderCircuit as _ProviderCircuit


def _log_classifier_api(event: str, detected: bool, method: str, text: str, extra: dict = None):
    """Log detección de consulta especial en DetailedLogger."""
    try:
        from tools.detailed_logger import get_logger
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event(
                "CLASSIFIER_API",
                f"{method}: {'HIT' if detected else 'MISS'} — {event}",
                level="INFO" if detected else "DEBUG",
                extra={
                    "detected": detected,
                    "method": method,
                    "text_preview": text[:50],
                    **(extra or {})
                }
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 classifier_api.py:56] excepción degradada (intencional): {_d2e}")


class APIDetector:
    """
    Detector de consultas especiales con integración de APIs externas.
    """

    # ═══════════════════════════════════════════════════════════
    # PATRONES PARA DETECCIÓN DE CONSULTAS ESPECIALES
    # ═══════════════════════════════════════════════════════════

    # ✅ v0.1.6: Patrones de clima corregidos
    # ELIMINADO: r'sol' standalone (matcheaba dentro de "solución", "solo", etc.)
    # REEMPLAZADO: patrones contextuales que requieren verbo o cuantificador
    # ANTES: "tu debes buscar la informacion" → \bsol\b matcheaba "solución"
    # AHORA: solo matchea "hace sol", "hay sol", "mucho sol", etc.
    # ✅ v0.1.7: FIX — r'\\s+' → r'\s+', r'\\b' → r'\b' en todos los patrones
    WEATHER_PATTERNS = [
        r'pronostico\s+(del\s+)?clima',
        r'pronostico\s+(para|de|del|en)\s',
        r'pronostico\s+(para\s+)?(hoy|manana|pasado|esta\s+semana)',
        r'\bpronostico\b',
        r'clima\s+(en|para|de|hoy|manana|ahora|hace)',
        r'que\s+clima',
        r'hace\s+hoy',
        r'temperatura\s+(en|de|actual|para)',
        r'va\s+llover', r'humedad', r'viento',
        r'lluvia', r'llover', r'nieve', r'nublado',
        r'weather', r'forecast', r'rain',
        r'sunny', r'cloudy', r'temperature',
        r'\btiempo\b',
        r'tiempo\s+(en|para|de|hoy|manana|ahora)',
        r'\bclima\b',
        r'\b(hace|hay|mucho|poco|tiene)\s+sol\b',
        r'\bsol\s+(fuerte|radiante|brillante|caliente)',
        r'esta\s+soleado', r'está\s+soleado',
    ]

    # ═══════════════════════════════════════════════════════════
    # ✅ FIX FASE 2: Entity Extraction Poisoning mitigado.
    # Se eliminaron los patrones genéricos basados solo en preposiciones
    # (en, de, para) que extraían ubicaciones incorrectas como "política"
    # en "noticias de política". Ahora se exige palabra clave de dominio.
    #
    # ✅ v0.1.5: Agregados patrones para pronostico de/del/en y
    # tiempo en/para/de. Antes solo existia pronostico para.
    # ✅ v0.1.7: FIX — r'\\s+' → r'\s+', r'\\?' → r'\?', r'\\.' → r'\.'
    # ═══════════════════════════════════════════════════════════
    LOCATION_PATTERNS = [
        r'pronostico\s+para\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'pronostico\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'pronostico\s+del\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'pronostico\s+en\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'temperatura\s+en\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'clima\s+en\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'clima\s+para\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'clima\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'tiempo\s+en\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'tiempo\s+para\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
        r'tiempo\s+de\s+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\?|\.|,|$)',
    ]

    # ✅ v0.1.7: FIX — r'\\s+' → r'\s+'
    NEWS_PATTERNS = [
        r'noticias\s+(de|sobre|hoy|ultimas|actuales)',
        r'ultimas\s+noticias', r'breaking\s+news', r'headline',
        r'news\s+(about|today|latest)', r'actualidad', r'acontecimientos',
    ]

    # ✅ v0.1.7: FIX — r'\\s+' → r'\s+'
    FINANCE_PATTERNS = [
        r'cotizaciones?\s+del\s+(dolar|euro|peso|libra|yen)',
        r'cotizaciones?\s+(hoy|actual|en\s+vivo)',
        r'(dolar|euro|peso|libra|yen)\s+(cotizacion|precio|cuanto\s+vale|valor)',
        r'dolar\s+blue',
        r'blue\s+(hoy|actual|cotizacion|precio|valor)',
        r'dolar\s+(mep|ccl|oficial|tarjeta|mayorista|solidario)',
        r'(mep|ccl|argenpesos)\s+(cotizacion|precio|valor)',
        r'precio\s+del\s+bitcoin', r'valor\s+del\s+euro',
        r'como\s+esta\s+la\s+bolsa', r'tasa\s+de\s+cambio',
        r'cuanto\s+vale\s+el\s+dolar', r'compra\s+venta\s+dolar',
        r'stock\s+price', r'bitcoin\s+(precio|valor|cuanto)',
        r'crypto', r'\bcripto', r'\bcriptomoneda', r'\bbolsa', r'\bmercado', r'\binversion',
    ]

    FINANCE_KEYWORDS = [
        'dolar', 'dólar', 'euro', 'peso', 'libra', 'yen',
        'blue', 'mep', 'ccl', 'argenpesos',
        'cotizacion', 'cotización',
        'bitcoin', 'crypto', 'cripto', 'usd', 'ars',
        'bolsa', 'tasa de cambio', 'divisa',
    ]

    CODE_INDICATORS = [
        'escribe codigo', 'escribe código',
        'genera codigo', 'genera código',
        'implementa', 'implementar',
        'def ', 'class ', 'import ',
        'fastapi', 'pydantic', 'basemodel',
        'unittest', 'pytest',
        'async def', 'await ',
        'dataclass', 'decorator',
        '-> ', ':  ', '    def',
    ]

    # ✅ Fase 8: "tiempo" es ambiguo en español (clima o duración/
    # rendimiento) — \btiempo\b y tiempo\s+de (WEATHER_PATTERNS) solo se
    # frenan con CODE_INDICATORS si hay sintaxis LITERAL (def/class/
    # import). Frases como "el tiempo de ejecución de esta función" o
    # "cuánto tiempo tarda este algoritmo" no tienen sintaxis literal y
    # son cortas — un umbral de longitud no las agarra. Estas keywords
    # cubren la prosa técnica sin sintaxis, en normalized text (sin
    # tildes, como se compara WEATHER_PATTERNS).
    TECH_CONTEXT_KEYWORDS = frozenset({
        'codigo', 'funcion', 'algoritmo', 'programa', 'script',
        'ejecucion', 'compilacion', 'servidor', 'api', 'endpoint',
        'consulta sql', 'query', 'proceso', 'sistema', 'respuesta',
        'latencia', 'rendimiento', 'optimizar', 'optimizacion',
    })

    # ✅ v0.1.7: FIX — r'\\s+' → r'\s+'
    TIME_PATTERNS = [
        r'que\s+hora\s+es', r'hora\s+(en|de|actual|ahora)',
        r'what\s+time\s+is\s+it', r'current\s+time',
        r'zona\s+horaria', r'huso\s+horario',
    ]

    # ✅ v0.1.6: WEB_SEARCH_PATTERNS expandido con 12 patrones nuevos
    # ✅ v0.1.7: BUG CRÍTICO CORREGIDO — r'\\s+' → r'\s+', r'\\b' → r'\b'
    #   CAUSA: r'busca(r)?\\s+' en raw string = buscar \s literal en texto
    #   → NUNCA matcheaba queries reales → PASO 1 siempre fallaba → WEB_SEARCH
    #   solo se detectaba vía keywords de PASO 2 con confianza 0.6
    #   FIX: Single backslash en raw strings = metachar regex correcto
    # ✅ v0.1.7: +13 patrones para consultas de info actual en lenguaje natural
    # ✅ v0.1.8: +18 patrones para hechos actuales sin verbo explícito de búsqueda.
    #   CASO REAL DETECTADO: "cuantos goles marco messi en este mundial 2026"
    #   clasificaba como general_chat porque ningún patrón cubría preguntas
    #   de hechos reales actuales (deportes, eventos, personas, resultados).
    # NOTA: todos los patrones se evalúan sobre texto NORMALIZADO (sin acentos,
    # minúsculas) via _normalize_text(). Se usan siempre formas sin acento.
    WEB_SEARCH_PATTERNS = [
        # Búsqueda explícita con verbo
        r'busca(r)?\s+(informacion\s+)?(sobre|de|acerca\s+de)',
        r'investiga(r)?\s+(sobre|de|acerca\s+de)',
        r'busca(r)?\s+en\s+(la\s+)?(web|internet|google|bing)',
        r'busca(r)?\s+datos?\s+(de|sobre)',
        r'busca(r)?\s+por\s+(mi|me)',
        r'quiero\s+saber\s+(sobre|de|acerca\s+de)',
        r'buscame\s+(informacion|datos|info)',
        r'buscame\s+sobre',
        r'investiga\s+(sobre|de)',
        r'buscar\s+informacion',
        r'buscar\s+datos',
        r'busca\s+informacion',
        r'search\s+(for|about)',
        r'find\s+information\s+(about|on)',
        # GitHub, repositorios, proyectos open source
        r'\b(github|gitlab|bitbucket)\b',
        r'\brepositorios?\s+(de|en|sobre|nuevos|populares)',
        r'\bproyectos?\s+(nuevos|populares|recientes|open\s+source|de\s+codigo\s+abierto)',
        r'\bdocumentacion\s+(de|sobre|en|similar)',
        r'\btrending\s+(en|de|sobre)',
        r'\bopen\s+source\b',
        r'\bcodigo\s+abierto\b',
        r'\balternativas?\s+(a|de|para|en)',
        r'\bmejores?\s+(proyectos|repositorios|librerias|frameworks|herramientas)',
        r'\btop\s+\d+\s+(proyectos|repositorios|librerias)',
        r'\brecientes?\s+(proyectos|repositorios|avances|desarrollos)',
        r'\bultimos?\s+(proyectos|repositorios|lanzamientos)',
        r'\b(similar|parecido|equivalente)\s+(a|al|como)\b',
        r'\b(como|tipo|estilo)\s+(el\s+)?(proyecto|repo|libreria|framework)',
        # ✅ v0.1.7: Consultas de información actual con indicador de tiempo
        r'que\s+paso\s+(con|en)\b',
        r'situacion\s+actual',
        r'\bultima\s+version\s+de\b',
        r'que\s+hay\s+de\s+nuevo',
        r'novedades\s+(de|sobre|en)',
        r'actualizacion\s+(de|sobre)',
        r'cual\s+es\s+(el|la)\s+ultim[oa]',
        r'que\s+se\s+sabe\s+(sobre|de)',
        r'que\s+esta\s+pasando\s+(con|en)',
        # ✅ v0.1.8: Hechos actuales sin verbo explícito de búsqueda
        # Deportes / eventos / resultados
        r'\ben\s+este\s+(mundial|torneo|copa|campeonato|evento|partido)',
        r'\b(mundial|copa\s+del\s+mundo)\s+\d{4}\b',
        r'cuantos?\s+(goles?|puntos?|asistencias?|partidos?|juegos?)\s+\w',
        r'\b(goles?|puntos?|asistencias?)\s+(de|del|que\s+(marco|hizo|metio|anoto))',
        r'\b(resultado|resultados|marcador)\s+(de|del|en\s+el)',
        r'\b(quien|quienes)\s+(gano|marco|metio|anoto|jugo|gana|juega)',
        r'\b(campeon|ganador|ganadora)\s+(del|de\s+la)\s+(mundial|copa|torneo|liga)',
        r'\bclasificacion\s+(del|de\s+la|en\s+el)',
        r'\bpartido\s+(de|entre|hoy|ayer|manana)',
        # Personas / cargos actuales
        r'\b(quien\s+es|quienes\s+son)\s+(el|la|los|las)\s+(actual|nuevo|nueva|presidente|ceo|director)',
        r'\b(actual|nuevo|nueva)\s+(presidente|primer\s+ministro|ceo|director|secretario)',
        r'\b(presidente|primer\s+ministro|ceo)\s+(de|del|actual)',
        # Precios / valores actuales
        r'\bprecio\s+(actual|de\s+hoy|en\s+este\s+momento)',
        r'\bvalor\s+(actual|de\s+hoy|en\s+este\s+momento)',
        r'\bcuanto\s+(cuesta|vale|esta)\s+(hoy|ahora|actualmente)',
        # Noticias y eventos recientes
        r'\b(hoy|ayer|esta\s+semana|este\s+mes|este\s+año)\s+(quien|que|como|cuanto)',
        r'\bultimas?\s+(noticias?|novedades?)\s+(de|sobre|del)',
        r'\bnoticias?\s+(recientes?|de\s+hoy|actuales?)\s+(de|sobre)',
    ]

    # ═══════════════════════════════════════════════════════════
    # INICIALIZACIÓN
    # ═══════════════════════════════════════════════════════════
    def __init__(self, enable_api_integration: bool = True):
        """Inicializa detector de APIs externas."""
        self.enable_api_integration = enable_api_integration
        self._location_cache: Dict[str, str] = {}
        # ✅ FIX AUD-CAS-001: circuit breaker por proveedor (weather: tavily/serper)
        self._circuits: Dict[str, _ProviderCircuit] = {
            "tavily": _ProviderCircuit(),
            "serper": _ProviderCircuit(),
        }
        self._circuit_fail_threshold = 3
        self._circuit_cooldown_seconds = 60.0
        logger.info(f"APIDetector initialized: api_integration={enable_api_integration}")

    # ═══════════════════════════════════════════════════════════
    # MÉTODOS DE DETECCIÓN DE PATRONES
    # ═══════════════════════════════════════════════════════════
    def _normalize_text(self, text: str) -> str:
        """Normaliza texto: quita acentos y convierte a minúsculas."""
        text_normalized = unicodedata.normalize('NFKD', text.lower())
        text_normalized = ''.join(c for c in text_normalized if not unicodedata.combining(c))
        return text_normalized

    def _is_code_prompt(self, text: str) -> bool:
        """
        Guard rápido — detecta si el texto ES un prompt de código.
        Se evalúa ANTES de cualquier detección especial.
        """
        text_lower = text.lower()
        return any(indicator in text_lower for indicator in self.CODE_INDICATORS)

    def _is_technical_context(self, text_normalized: str) -> bool:
        """
        ✅ Fase 8: guard complementario a _is_code_prompt() — ese exige
        sintaxis literal (def/class/import), esto detecta prosa técnica
        sin sintaxis ("el tiempo de ejecución de esta función"). Existe
        específicamente por la ambigüedad de "tiempo" en WEATHER_PATTERNS
        (clima vs. duración/rendimiento) — ver TECH_CONTEXT_KEYWORDS.
        """
        return any(kw in text_normalized for kw in self.TECH_CONTEXT_KEYWORDS)

    def _is_weather_query(self, text: str) -> bool:
        """
        Detecta si es consulta sobre clima.
        ✅ v0.1.6: Guard anti-web-search. Si detecta intent de búsqueda web,
        retorna False ANTES de evaluar patrones de clima.
        Esto resuelve el orden incorrecto de checks en el facade
        (weather se ejecuta antes que web_search).
        """
        text_normalized = self._normalize_text(text)

        if self._is_web_search_intent(text_normalized):
            _log_classifier_api(
                "weather_query", False, "web_search_guard", text,
                {"reason": "web_search intent detectado, weather descartado"}
            )
            return False

        if self._is_code_prompt(text):
            _log_classifier_api(
                "weather_query", False, "code_guard", text,
                {"reason": "código detectado, weather descartado"}
            )
            return False

        # ✅ Fase 8: prosa técnica sin sintaxis literal — ver
        # _is_technical_context(). Chequeo separado del de arriba porque
        # usa un criterio distinto (vocabulario, no sintaxis).
        if self._is_technical_context(text_normalized):
            _log_classifier_api(
                "weather_query", False, "tech_context_guard", text,
                {"reason": "contexto técnico detectado, weather descartado"}
            )
            return False

        result = any(re.search(p, text_normalized, re.I) for p in self.WEATHER_PATTERNS)
        if result:
            matched = next((p for p in self.WEATHER_PATTERNS if re.search(p, text_normalized, re.I)), None)
            _log_classifier_api("weather_query", True, "regex", text, {"matched_pattern": matched})
        return result

    def _is_web_search_intent(self, text_normalized: str) -> bool:
        """
        ✅ v0.1.6: Helper para detectar intent de búsqueda web.
        Usado como guard en _is_weather_query() para evitar falsos positivos.
        ✅ v0.1.7: Ahora funciona correctamente tras el fix de backslashes.
        """
        return any(re.search(p, text_normalized, re.I) for p in self.WEB_SEARCH_PATTERNS)

    def _is_news_query(self, text: str) -> bool:
        """Detecta si es consulta sobre noticias."""
        text_normalized = self._normalize_text(text)
        result = any(re.search(p, text_normalized, re.I) for p in self.NEWS_PATTERNS)
        if result:
            matched = next((p for p in self.NEWS_PATTERNS if re.search(p, text_normalized, re.I)), None)
            _log_classifier_api("news_query", True, "regex", text, {"matched_pattern": matched})
        return result

    def _is_finance_query(self, text: str) -> bool:
        """
        Detecta si es consulta financiera.
        Guard: si es prompt de código → False inmediatamente.
        """
        if self._is_code_prompt(text):
            _log_classifier_api(
                "finance_query", False, "code_guard", text,
                {"reason": "código detectado, finance descartado"}
            )
            return False
        text_normalized = self._normalize_text(text)
        matched_pattern = next((p for p in self.FINANCE_PATTERNS if re.search(p, text_normalized, re.I)), None)
        if matched_pattern:
            _log_classifier_api("finance_query", True, "regex", text, {"matched_pattern": matched_pattern})
            return True
        matched_kw = next(
            (
                kw for kw in self.FINANCE_KEYWORDS
                if re.search(rf"\b{re.escape(kw)}\b", text_normalized, re.I)
            ),
            None,
        )
        if matched_kw:
            _log_classifier_api("finance_query", True, "keyword_fallback", text, {"matched_keyword": matched_kw})
            return True
        return False

    def _is_time_query(self, text: str) -> bool:
        """Detecta si es consulta sobre la hora."""
        text_normalized = self._normalize_text(text)
        result = any(re.search(p, text_normalized, re.I) for p in self.TIME_PATTERNS)
        if result:
            matched = next((p for p in self.TIME_PATTERNS if re.search(p, text_normalized, re.I)), None)
            _log_classifier_api("time_query", True, "regex", text, {"matched_pattern": matched})
        return result

    def _is_web_search_query(self, text: str) -> bool:
        """
        Detecta si es consulta de búsqueda web.
        ✅ v0.1.6: Expandido con patrones de GitHub, repositorios, proyectos, etc.
        ✅ v0.1.7: Patrones ahora funcionales (fix de doble backslash en raw strings).
        """
        text_normalized = self._normalize_text(text)
        result = any(re.search(p, text_normalized, re.I) for p in self.WEB_SEARCH_PATTERNS)
        if result:
            matched = next((p for p in self.WEB_SEARCH_PATTERNS if re.search(p, text_normalized, re.I)), None)
            _log_classifier_api("web_search_query", True, "regex", text, {"matched_pattern": matched})
        return result

    # ═══════════════════════════════════════════════════════════
    # EXTRACCIÓN DE UBICACIÓN
    # ═══════════════════════════════════════════════════════════
    def _extract_location(self, text: str) -> Optional[str]:
        """
        Extrae ubicación de una consulta de clima.
        v0.1.5: Normaliza el texto ANTES de buscar patrones,
        igual que _is_weather_query(). Antes: "pronóstico para Córdoba"
        no matcheaba r'pronostico\\s+para\\s+...' porque tenía acentos.
        Ahora normaliza primero y luego busca, consistente con la
        detección de patrones.
        """
        cache_key = hashlib.md5(text.encode()).hexdigest()[:16]
        if cache_key in self._location_cache:
            logger.debug(f"Location cache hit: {self._location_cache[cache_key]}")
            return self._location_cache[cache_key]

        text_normalized = self._normalize_text(text)

        for pattern in self.LOCATION_PATTERNS:
            match = re.search(pattern, text_normalized, re.IGNORECASE)
            if match:
                location = match.group(1).strip()
                location = location[:50]
                if location.lower() not in ['el', 'la', 'los', 'las', 'un', 'una', 'hoy', 'manana', 'ahora']:
                    self._location_cache[cache_key] = location
                    logger.debug(f"Location extracted: {location}")
                    return location

        if hasattr(settings, 'default_location') and settings.default_location:
            self._location_cache[cache_key] = settings.default_location
            logger.debug(f"Location fallback to default: {settings.default_location}")
            return settings.default_location
        logger.debug("No location found in query")
        return None

    def _get_country_code(self, location: str) -> str:
        """Mapea nombre de país a código ISO."""
        country_map = {
            "argentina": "ar", "espana": "es", "mexico": "mx",
            "colombia": "co", "chile": "cl", "peru": "pe",
            "usa": "us", "united states": "us", "uk": "gb",
        }
        return country_map.get(location.lower().strip(), "us")

    # ═══════════════════════════════════════════════════════════
    # INTEGRACIÓN CON APIs EXTERNAS
    # ═══════════════════════════════════════════════════════════
    def _get_weather_from_api(self, location: str) -> Optional[Dict[str, Any]]:
        """Obtiene datos climáticos desde API externa (fallback)."""
        start = time.time()
        if not self.enable_api_integration:
            logger.debug("API integration disabled")
            return None
        # ✅ FIX AUD-CAS-001: saltear proveedores con circuito abierto (fallos
        # recientes) en vez de pagar su timeout completo en cada consulta.
        tavily_circuit = self._circuits["tavily"]
        if settings.tavily_api_key and not tavily_circuit.is_open(self._circuit_cooldown_seconds):
            try:
                import httpx
                url = "https://api.tavily.com/search"
                payload = {
                    "api_key": settings.tavily_api_key,
                    "query": f"weather forecast {location}",
                    "search_depth": "basic",
                    "max_results": 3,
                }
                response = httpx.post(url, json=payload, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data.get("results"):
                    duration = time.time() - start
                    tavily_circuit.record_success()
                    logger.info(f"Weather API (Tavily) success in {duration:.4f}s")
                    return {"source": "tavily", "results": data["results"][:3], "location": location}
                tavily_circuit.record_success()  # respondió bien, solo sin resultados útiles
            except Exception as e:
                duration = time.time() - start
                tavily_circuit.record_failure(self._circuit_fail_threshold)
                _log_error(e, "APIDetector._get_weather_from_api", extra={"location": location, "api": "tavily"})
                logger.warning(f"Tavily weather API failed in {duration:.4f}s: {e}")
        elif settings.tavily_api_key:
            logger.debug("Weather API (Tavily): circuito abierto, salteando")
        serper_circuit = self._circuits["serper"]
        if settings.serper_api_key and not serper_circuit.is_open(self._circuit_cooldown_seconds):
            try:
                import httpx
                url = "https://google.serper.dev/search"
                headers = {"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"}
                payload = {"q": f"weather {location}"}
                response = httpx.post(url, headers=headers, json=payload, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data.get("organic"):
                    duration = time.time() - start
                    serper_circuit.record_success()
                    logger.info(f"Weather API (Serper) success in {duration:.4f}s")
                    return {"source": "serper", "results": data["organic"][:3], "location": location}
                serper_circuit.record_success()
            except Exception as e:
                duration = time.time() - start
                serper_circuit.record_failure(self._circuit_fail_threshold)
                _log_error(e, "APIDetector._get_weather_from_api", extra={"location": location, "api": "serper"})
                logger.warning(f"Serper weather API failed in {duration:.4f}s: {e}")
        elif settings.serper_api_key:
            logger.debug("Weather API (Serper): circuito abierto, salteando")
        return None

    def _get_news_from_api(self, query: str, location: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Obtiene noticias desde News API."""
        start = time.time()
        if not self.enable_api_integration or not settings.news_api_key:
            logger.debug("News API integration disabled or no key")
            return None
        try:
            import httpx
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "apiKey": settings.news_api_key,
                "pageSize": 5,
                "sortBy": "publishedAt",
                "language": "es" if not location or location.lower() in ["argentina", "espana", "mexico"] else "en",
            }
            if location:
                params["country"] = self._get_country_code(location)
            response = httpx.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("articles"):
                duration = time.time() - start
                logger.info(f"News API success in {duration:.4f}s: {len(data['articles'])} articles")
                return {"source": "newsapi", "articles": data["articles"][:5], "query": query}
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "APIDetector._get_news_from_api", extra={"query": query, "location": location})
            logger.warning(f"News API failed in {duration:.4f}s: {e}")
        return None

    def _get_finance_from_api(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Obtiene datos financieros desde API externa."""
        start = time.time()
        if not self.enable_api_integration:
            logger.debug("API integration disabled")
            return None
        if settings.tavily_api_key:
            try:
                import httpx
                url = "https://api.tavily.com/search"
                payload = {
                    "api_key": settings.tavily_api_key,
                    "query": f"{symbol} price quote today",
                    "search_depth": "basic",
                    "max_results": 3,
                }
                response = httpx.post(url, json=payload, timeout=10)
                response.raise_for_status()
                data = response.json()
                if data.get("results"):
                    duration = time.time() - start
                    logger.info(f"Finance API (Tavily) success in {duration:.4f}s")
                    return {"source": "tavily", "results": data["results"][:3], "symbol": symbol}
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "APIDetector._get_finance_from_api", extra={"symbol": symbol})
                logger.warning(f"Tavily finance API failed in {duration:.4f}s: {e}")
        return None

    def _get_time_info(self, location: Optional[str] = None) -> Dict[str, Any]:
        """Obtiene información de hora/zona horaria."""
        now = datetime.now(timezone.utc)
        if not location:
            return {
                "utc_time": now.strftime("%H:%M:%S"),
                "utc_date": now.strftime("%Y-%m-%d"),
                "timezone": "UTC"
            }
        tz_map = {
            "argentina": "UTC-3", "buenos aires": "UTC-3", "cordoba": "UTC-3",
            "espana": "UTC+1", "madrid": "UTC+1",
            "mexico": "UTC-6", "mexico df": "UTC-6",
            "usa": "UTC-5 a UTC-8", "new york": "UTC-5",
            "londres": "UTC+0", "tokio": "UTC+9",
        }
        location_lower = location.lower().strip()
        tz = tz_map.get(location_lower, "desconocida")
        return {
            "utc_time": now.strftime("%H:%M:%S"),
            "local_timezone": tz,
            "note": f"Para hora exacta en {location}, consultar servicio de zona horaria",
        }

    # ═══════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════
    def set_api_integration(self, enabled: bool) -> None:
        """Habilita/deshabilita integración con APIs externas."""
        self.enable_api_integration = enabled
        logger.info(f"APIDetector API integration: {'enabled' if enabled else 'disabled'}")

    def clear_location_cache(self) -> None:
        """Limpia cache de ubicaciones."""
        count = len(self._location_cache)
        self._location_cache.clear()
        logger.info(f"APIDetector location cache cleared: {count} entries")
