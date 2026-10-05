#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# client_base.py - Cliente Base Ollama (Infraestructura)
# Ruta: /ruta/a/axioma/src/llm/client_base.py
# Autor: SaintWick
# Versión: 1.0.8
#
# Proporciona la infraestructura base para el cliente Ollama:
# gestión de sesiones HTTP, cache LRU, circuit breaker, backoff
# exponencial, timeouts adaptativos, pre-carga de modelos y
# soporte para embeddings. Incluye integración con ModelSwap
# para control de VRAM y fallbacks seguros con circuit breaker.
# ═══════════════════════════════════════════════════════════════
import asyncio
import contextvars
import time
import random
import json
import logging
import traceback
import hashlib
import threading
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections import OrderedDict
from config.settings import settings
from src.domain.exceptions import LLMError
from src.utils.degradation import log_degraded

try:
    from src.core.self_healing import get_monitor as _get_healing_monitor
    _HEALING_AVAILABLE = True
except ImportError:
    _HEALING_AVAILABLE = False
    _get_healing_monitor = None

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

import httpx

logger = logging.getLogger(__name__)


class RequestCache:
    """Cache LRU para respuestas de Ollama."""
    def __init__(self, max_size: int = 500):
        self.max_size = max_size
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _generate_key(self, model: str, prompt: str, temperature: Optional[float],
                       system: Optional[str] = None, max_tokens: Optional[int] = None) -> str:
        # ✅ FIX: la key no incluía max_tokens ni system — dos llamadas con el
        # mismo model/prompt/temperature pero distinto max_tokens (ej. un
        # reintento con más margen) o distinto system prompt colisionaban y
        # compartían la MISMA respuesta cacheada, aunque hubieran pedido cosas
        # distintas. Confirmado con un caso real: una respuesta truncada por
        # max_tokens chico quedaba pegada aunque el siguiente pedido tuviera
        # mucho más margen.
        cache_key = f"{model}:{prompt}:{temperature}:{system}:{max_tokens}"
        return hashlib.sha256(cache_key.encode()).hexdigest()

    def get(self, model: str, prompt: str, temperature: Optional[float],
            system: Optional[str] = None, max_tokens: Optional[int] = None) -> Optional[Dict[str, Any]]:
        cache_key = self._generate_key(model, prompt, temperature, system, max_tokens)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                self._hits += 1
                return self._cache[cache_key]
            self._misses += 1
            return None

    def set(self, model: str, prompt: str, temperature: Optional[float], response: Dict[str, Any],
            system: Optional[str] = None, max_tokens: Optional[int] = None) -> None:
        cache_key = self._generate_key(model, prompt, temperature, system, max_tokens)
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
            self._cache[cache_key] = response
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0
        }


class HTTPSessionPool:
    """Pool de sesiones HTTP para reutilización de conexiones."""
    def __init__(self, max_size: int = 10):
        self.max_size = max_size
        self._pool: List[httpx.Client] = []
        self._in_use: set = set()
        self._lock = threading.Lock()
        self._base_url: Optional[str] = None
        self._timeout: Optional[int] = None

    def initialize(self, base_url: str, timeout: int) -> None:
        self._base_url = base_url
        self._timeout = timeout

    def acquire(self, timeout: Optional[int] = None) -> httpx.Client:
        effective_timeout = timeout or self._timeout or 30
        with self._lock:
            for i, session in enumerate(self._pool):
                try:
                    current = float(session.timeout.read)
                    if abs(current - effective_timeout) < 1:
                        self._pool.pop(i)
                        # ✅ FIX: guardar la sesión misma (referencia fuerte), no
                        # id(session) — mismo bug ya identificado y corregido en
                        # db_pool.py: si un caller no llama release() y la sesión
                        # es garbage-collected, un objeto nuevo puede reciclar esa
                        # misma dirección de memoria y aparecer como "en uso" por
                        # error, corrompiendo el tracking del pool.
                        self._in_use.add(session)
                        return session
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 client_base.py:137] excepción degradada (intencional): {_d2e}")
            session = httpx.Client(
                base_url=self._base_url or "http://127.0.0.1:11434",
                timeout=effective_timeout,
                headers={"Content-Type": "application/json"},
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=300.0),
                trust_env=False
            )
            self._in_use.add(session)
            return session

    def release(self, session: httpx.Client) -> None:
        with self._lock:
            if session in self._in_use:
                self._in_use.discard(session)
            if len(self._pool) < self.max_size:
                self._pool.append(session)
            else:
                session.close()

    def close_all(self) -> None:
        with self._lock:
            for session in self._pool:
                try:
                    session.close()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 client_base.py:163] excepción degradada (intencional): {_d2e}")
            for session in list(self._in_use):
                try:
                    session.close()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 client_base.py:168] excepción degradada (intencional): {_d2e}")
                self._in_use.discard(session)
            self._pool.clear()


def _log_model_call(model: str, action: str, task_type: str = "", input_size: int = None,
                    output_size: int = None, duration: float = None, success: bool = True,
                    temperature: float = None, tokens_used: int = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        lg = get_logger()
        if hasattr(lg, 'is_initialized') and lg.is_initialized:
            lg.log_model_call(model, action, task_type, input_size, output_size, duration, success, temperature, tokens_used)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 client_base.py:183] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: Dict = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        lg = get_logger()
        if hasattr(lg, 'is_initialized') and lg.is_initialized:
            lg.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 client_base.py:194] excepción degradada (intencional): {_d2e}")


def _log_event(category: str, message: str, level: str = "INFO", extra: Dict = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        lg = get_logger()
        if hasattr(lg, 'is_initialized') and lg.is_initialized:
            lg.log_event(category, message, level, extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 client_base.py:205] excepción degradada (intencional): {_d2e}")


def _log_file(path: str, mode: str = "read", operation: str = ""):
    if not LOGGER_AVAILABLE:
        return
    try:
        lg = get_logger()
        if hasattr(lg, 'is_initialized') and lg.is_initialized:
            size = None
            exists = Path(path).exists()
            if exists:
                size = Path(path).stat().st_size
            lg.log_file_access(path, mode, operation, size, exists)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 client_base.py:220] excepción degradada (intencional): {_d2e}")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ScenarioType(str, Enum):
    STANDARD = "standard"
    FIRST_LOAD = "first_load"
    VISION = "vision"
    EMBED = "embed"
    CODE = "code"


@dataclass
class CircuitBreaker:
    """Circuit Breaker para proteger contra fallos en cascada."""
    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 1
    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    last_failure_time: Optional[float] = field(default=None)
    half_open_calls: int = field(default=0)

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.half_open_calls = 0
        _log_event("CIRCUIT_BREAKER", f"SUCCESS → CLOSED", extra={"failure_count": 0, "state": self.state.value})

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 1: Evitar CB stagnado en HALF_OPEN
        # Si la llamada de prueba en HALF_OPEN falla, debemos volver a OPEN
        # y reiniciar el timer, no quedarnos atascados rechazando tráfico.
        # ═══════════════════════════════════════════════════════════════
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
            _log_event("CIRCUIT_BREAKER", "HALF_OPEN → OPEN (recovery test failed)", level="WARNING",
                       extra={"failure_threshold": self.failure_threshold})
        elif self.failure_count >= self.failure_threshold:
            old_state = self.state
            self.state = CircuitState.OPEN
            _log_event("CIRCUIT_BREAKER", f"OPENED after {self.failure_count} failures", level="WARNING",
                       extra={"previous_state": old_state.value, "failure_threshold": self.failure_threshold})
        else:
            _log_event("CIRCUIT_BREAKER", f"Failure #{self.failure_count}", level="WARNING",
                       extra={"state": self.state.value, "threshold": self.failure_threshold})

    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.last_failure_time is None:
                return False
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                _log_event("CIRCUIT_BREAKER", "OPEN → HALF_OPEN (testing recovery)",
                           extra={"elapsed": round(elapsed, 2), "timeout": self.recovery_timeout})
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls < self.half_open_max_calls:
                self.half_open_calls += 1
                _log_event("CIRCUIT_BREAKER", f"HALF_OPEN call #{self.half_open_calls}",
                           extra={"max_calls": self.half_open_max_calls})
                return True
            return False
        return False

    def reset(self) -> None:
        old_state = self.state
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = None
        self.half_open_calls = 0
        _log_event("CIRCUIT_BREAKER", f"RESET: {old_state.value} → CLOSED")


def tokens_de_respuesta(resp: Any) -> Dict[str, int]:
    """Uso REAL de tokens de una respuesta de Ollama (`ISSUE-082`).

    Tanto `OllamaClientBase.generate()` como `OllamaClient.chat()` devuelven un
    `OllamaResponse` con `prompt_eval_count` (tokens de entrada) y `eval_count`
    (tokens generados, `num_predict` real). Sin propagarlos, las columnas
    `tokens_prompt`/`tokens_generated` de `data/traces.db` quedaban **siempre en 0**
    (verificado: 0 de 741 filas) y la latencia no se podía atribuir a nada
    (modelo vs prompt vs reintentos vs búsqueda).

    Devuelve un dict listo para mezclar en `Response.metadata`.
    """
    return {
        "tokens_prompt": int(getattr(resp, "prompt_eval_count", 0) or 0),
        "tokens_generated": int(getattr(resp, "eval_count", 0) or 0),
    }


@dataclass
class OllamaResponse:
    """Respuesta estructurada de Ollama."""
    content: str
    model: str
    done: bool
    total_duration: float
    load_duration: float
    prompt_eval_count: int
    eval_count: int
    eval_duration: float
    raw: Dict[str, Any] = field(default_factory=dict)
    # ✅ NUEVO v1.0.8: Soporte para Tool Calling nativo de Ollama
    tool_calls: Optional[List[Dict[str, Any]]] = None
    # El mensaje completo crudo para reinyectar en el historial si hay tool_calls
    message: Optional[Dict[str, Any]] = None


def _safe_divide(value: Optional[float], divisor: float, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return value / divisor
    except (TypeError, ZeroDivisionError) as e:
        # ✅ Política R3: un TypeError acá es error de CONTRATO (una duración que
        # llega como str, p. ej.) y un divisor 0 es un dato imposible de Ollama.
        # Antes se devolvía el default en silencio (el auditor lo marcaba como
        # handler silencioso en este archivo); ahora queda como degradación.
        log_degraded(logger, e, "src/llm/client_base.py:_safe_divide")
        return default


# ═══════════════════════════════════════════════════════════════
# ✅ v0.5.3: Límites de contexto para Ollama
# ═══════════════════════════════════════════════════════════════
# num_ctx mínimo y máximo. Sin esto, Ollama default es 2048
# que trunca input silenciosamente en archivos grandes.
NUM_CTX_MIN = 2048
NUM_CTX_MAX = 32768
# Estimación conservadora: ~3 chars por token para español/código
CHARS_PER_TOKEN = 3
# Buffer extra para system prompt, instrucciones, overhead
CTX_BUFFER = 512

# ═══════════════════════════════════════════════════════════════
# ✅ v0.7.5 (ISSUE-074 / A3a): PRESUPUESTO ↔ TIMEOUT (medición propia)
# ═══════════════════════════════════════════════════════════════
# Velocidades MEDIDAS en este equipo (CPU, 6 núcleos físicos / 12 hilos,
# Ollama 0.30.0, 2026-09-28) — y lo importante: **decaen con el contexto**:
#
#   qwen3:8b, contexto ~0-33 tok → TG 6,28-6,30 tok/s   (3 corridas, ±1%)
#   qwen3:8b, contexto 2.477 tok → TG 4,84 tok/s · PP 50,8 tok/s
#   qwen3:8b, contexto 3.231 tok → TG 3,59 tok/s · PP 43,6 tok/s
#   qwen2.5-coder:7b             → TG 7,92 tok/s
#
# Por eso NO se usa una sola constante optimista: `TG_TOK_S` toma el **peor caso
# medido** (contexto de ~3k) y el prefill se descuenta aparte, porque compite por
# el MISMO timeout de la petición.
TG_TOK_S = 3.5          # generación, peor caso medido (contexto ~3k)
PP_TOK_S = 45.0         # prefill (43,6-50,8 medidos)
MARGEN_PRESUPUESTO = 0.85   # 15% para carga de la máquina y jitter

# Presupuesto de salida cuando el caller no pide ninguno: chico a propósito
# (un default grande + timeout corto = timeout garantizado y respuesta perdida).
DEFAULT_NUM_PREDICT = 512


def num_predict_coherente(solicitado: int, timeout: float,
                          prompt_len: int = 0) -> int:
    """Acota `num_predict` a lo que REALMENTE entra en `timeout` (A3a).

    Motivo (medido): `MAX_TOKENS_CHAT = 4096` con timeout de 180 s pedía **650 s**
    de generación a 6,3 tok/s ⇒ la petición moría por timeout, se reintentaba
    (`max_retries`) y la respuesta larga nunca llegaba: de ahí la cola de 391 s en
    `data/traces.db` y el p95 de 121 s en `general_chat`.

    Además del tiempo de generación se descuenta el **prefill** del prompt
    (`PP_TOK_S`): con un archivo entero (~15k tokens) el prefill solo cuesta
    ~330 s, más que el timeout de varias rutas. El recorte se registra en el log
    (`num_predict_solicitado` vs `num_predict_efectivo`), nunca es silencioso.
    """
    tope = max(float(timeout), 1.0) * MARGEN_PRESUPUESTO
    tokens_prompt = max(int(prompt_len) // max(CHARS_PER_TOKEN, 1), 1)
    t_disponible = max(tope - (tokens_prompt / PP_TOK_S), 2.0)
    maximo = int(t_disponible * TG_TOK_S)
    return max(64, min(int(solicitado), maximo))


_token_cancelacion: Any = contextvars.ContextVar("axioma_token_cancelacion", default=None)


class GeneracionCancelada(LLMError):
    """El usuario apretó DETENER: el pedido se cortó a propósito (no reintentar)."""


class TokenCancelacion:
    """Permite DETENER de verdad los pedidos de los agentes (código incluido).

    Estado de la investigación (`ISSUE-137`, medido y probado de tres formas):

    * El chat (con flujo de tokens) **sí** se corta: cerrar el generador cierra la
      conexión y Ollama aborta (medido 545 % → 21-30 % de CPU).
    * El camino de los agentes (código, validador, research) usa `generate()` con
      `stream: false`, y en ese modo **no hay nada que cerrar**: medido, Ollama
      **no manda las cabeceras** hasta que terminó de generar (el consumidor
      quedaba bloqueado 60 s sin respuesta, con el runner al 548 %), y cerrar el
      pool de sesiones tampoco interrumpe el pedido.
    * **Solución**: cuando hay un token activo, `generate()` pide en **modo flujo**
      y acumula el texto internamente. Así las cabeceras llegan enseguida, los
      trozos van llegando y cerrar la respuesta corta la generación.

    Se propaga por `contextvars` para no enhebrar un parámetro nuevo por todo el
    pipeline: la UI lo pone antes de lanzar la tarea y `asyncio.to_thread` (que
    copia el contexto) lo hace visible en el hilo del agente.
    """

    def __init__(self) -> None:
        self._respuestas: set = set()
        self._lock = threading.Lock()
        self.cancelado = False

    def registrar(self, respuesta: Any) -> None:
        with self._lock:
            if self.cancelado:      # ya se pidió cortar: no dejar pasar el pedido
                raise GeneracionCancelada("Generación cancelada por el usuario")
            self._respuestas.add(respuesta)

    def liberar(self, respuesta: Any) -> None:
        with self._lock:
            self._respuestas.discard(respuesta)

    def cancelar(self) -> int:
        """Cierra las conexiones en curso. Devuelve cuántas cerró."""
        with self._lock:
            self.cancelado = True
            pendientes = list(self._respuestas)
            self._respuestas.clear()
        for resp in pendientes:
            try:
                resp.close()
            except Exception as e:  # noqa: BLE001 — cerrar de más nunca debe romper
                log_degraded(logger, e, "TokenCancelacion.cancelar")
        if pendientes:
            logger.info(f"[DETENER] {len(pendientes)} pedido(s) en curso cerrados")
        return len(pendientes)


def token_cancelacion_actual() -> Any:
    """Token activo (o None). Lo lee el cliente antes de cada pedido."""
    return _token_cancelacion.get()


def usar_token_cancelacion(token: Any) -> Any:
    """Setea el token en el contexto actual (lo heredan tareas e hilos hijos)."""
    return _token_cancelacion.set(token)


def _generate_json_por_flujo(session: Any, payload: Dict[str, Any], timeout: Any,
                             token: Any) -> Dict[str, Any]:
    """Hace el `generate` en MODO FLUJO y devuelve lo mismo que sin flujo.

    Necesario para que DETENER pueda cortarlo (ver `TokenCancelacion`). El
    diccionario devuelto tiene las mismas claves que la respuesta sin flujo
    (`response`, `done`, `prompt_eval_count`, `eval_count`, `done_reason` y las
    duraciones), así el resto de `generate()` no cambia.
    """
    payload = {**payload, "stream": True}
    trozos: List[str] = []
    datos: Dict[str, Any] = {"response": "", "done": False}
    with session.stream("POST", "/api/generate", json=payload, timeout=timeout) as response:
        response.raise_for_status()
        token.registrar(response)
        try:
            for linea in response.iter_lines():
                if not linea:
                    continue
                d = json.loads(linea)
                trozo = d.get("response") or ""
                if trozo:
                    trozos.append(trozo)
                if d.get("done"):
                    for clave in ("prompt_eval_count", "eval_count", "done_reason",
                                  "total_duration", "load_duration", "eval_duration",
                                  "model"):
                        if clave in d:
                            datos[clave] = d[clave]
                    datos["done"] = True
                    break
        finally:
            token.liberar(response)
    datos["response"] = "".join(trozos)
    return datos


def _datos_de_generacion(session: Any, payload: Dict[str, Any], timeout: Any,
                         token: Any = None) -> Dict[str, Any]:
    """Elige el camino del pedido: flujo si hay token, histórico si no.

    Sin token, `session.post(...)` exactamente como siempre (cero riesgo). Con
    token, modo flujo para que DETENER pueda cerrar la conexión (`ISSUE-137`).
    Se extrajo para poder probar la elección sin levantar el cliente entero.
    """
    if token is None:
        response = session.post("/api/generate", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    return _generate_json_por_flujo(session, payload, timeout, token)


class OllamaClientBase:
    """Base para cliente Ollama — Infraestructura y operaciones write."""
    _request_cache: Dict[str, RequestCache] = {}
    _cache_lock = threading.Lock()
    _session_pool: Optional[HTTPSessionPool] = None

    def __init__(self, model: Optional[str] = None, timeout: Optional[int] = None,
                 base_url: Optional[str] = None, scenario: ScenarioType = ScenarioType.STANDARD,
                 is_first_request: bool = False, cache_size: int = 500, disable_fallback: bool = False):
        start_init = time.time()
        self.model = model or settings.llm_default_model
        self.base_url = base_url or settings.ollama_host
        # ✅ FIX: el scenario ya no se infiere del nombre del modelo (substring
        # "coder"/"code"). Cada caller (agente) debe declarar su scenario
        # explícito — ver BaseAgent.LLM_SCENARIO. Cambiar el nombre de un
        # modelo ya no puede alterar su timeout/comportamiento.
        self.scenario = scenario
        self.is_first_request = is_first_request or scenario == ScenarioType.FIRST_LOAD
        if timeout is not None:
            self.timeout = timeout
        else:
            self.timeout = settings.get_timeout_for_scenario(self.scenario.value, is_first_request=self.is_first_request)

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX CRÍTICO: Circuit Breaker tolerante para LLMs locales.
        # ═══════════════════════════════════════════════════════════════
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=max(settings.max_retries + 3, 5),
            recovery_timeout=120.0
        )
        self.max_retries = settings.max_retries
        self.base_delay = 1.0
        self._session: Optional[httpx.Client] = None
        self._pool_size = 5
        self._cache_size = cache_size
        # ✅ FIX RAC: Prevenir cambio de modelo (VRAM thrashing)
        self.disable_fallback = disable_fallback
        self._request_cache: Optional[RequestCache] = None
        duration = time.time() - start_init
        _log_event("OLLAMA_CLIENT", f"Initialized: {self.model}", extra={
            "model": self.model, "timeout": self.timeout, "scenario": self.scenario.value,
            "base_url": self.base_url, "is_first_request": self.is_first_request,
            "init_duration_ms": round(duration * 1000, 2), "cache_size": cache_size,
            "cb_failure_threshold": self.circuit_breaker.failure_threshold,
            "cb_recovery_timeout": self.circuit_breaker.recovery_timeout,
            "disable_fallback": self.disable_fallback,
        })
        _log_model_call(self.model, "init", "SYSTEM", duration=duration, success=True)
        logger.info(f"OllamaClientBase initialized: model={self.model}, timeout={self.timeout}s, "
                     f"scenario={self.scenario.value}, cb_threshold={self.circuit_breaker.failure_threshold}, "
                     f"cb_recovery={self.circuit_breaker.recovery_timeout}s, disable_fallback={self.disable_fallback}")

    @classmethod
    def _get_shared_cache(cls, model: str, cache_size: int = 500) -> RequestCache:
        with cls._cache_lock:
            if model not in cls._request_cache:
                cls._request_cache[model] = RequestCache(max_size=cache_size)
            return cls._request_cache[model]

    @classmethod
    def _get_session_pool(cls, base_url: str, timeout: int) -> HTTPSessionPool:
        if cls._session_pool is None:
            cls._session_pool = HTTPSessionPool(max_size=10)
        cls._session_pool.initialize(base_url, timeout)
        return cls._session_pool

    def _get_cache(self) -> RequestCache:
        if self._request_cache is None:
            self._request_cache = self._get_shared_cache(self.model, self._cache_size)
        return self._request_cache

    def _get_timeout(self, override: Optional[int] = None) -> int:
        if override is not None:
            _log_event("OLLAMA_CLIENT", f"Using override timeout: {override}s")
            return override
        if self.is_first_request:
            timeout = settings.get_timeout_for_scenario(self.scenario.value, is_first_request=True)
            _log_event("OLLAMA_CLIENT", f"Using first-load timeout: {timeout}s", extra={"model": self.model, "scenario": "first_load"})
            return timeout
        _log_event("OLLAMA_CLIENT", f"Using standard timeout: {self.timeout}s", extra={"model": self.model, "scenario": self.scenario.value})
        return self.timeout

    def _get_client(self, timeout: Optional[int] = None) -> httpx.Client:
        effective_timeout = self._get_timeout(timeout)
        pool = self._get_session_pool(self.base_url, effective_timeout)
        session = pool.acquire(timeout=effective_timeout)
        self._session = session
        _log_event("OLLAMA_CLIENT", f"Session acquired from pool", extra={"base_url": self.base_url, "timeout": effective_timeout})
        return session

    def _release_client(self, session: httpx.Client) -> None:
        if self._session_pool:
            self._session_pool.release(session)
        _log_event("OLLAMA_CLIENT", f"Session released to pool")

    def _calculate_backoff(self, attempt: int) -> float:
        delay = self.base_delay * (2 ** attempt)
        jitter = random.uniform(0, 0.1 * delay)
        result = min(delay + jitter, 30.0)
        _log_event("OLLAMA_CLIENT", f"Backoff calculated: {result:.2f}s", extra={
            "attempt": attempt, "base_delay": self.base_delay, "jitter": round(jitter, 3), "capped": result >= 30.0
        })
        return result

    # ═══════════════════════════════════════════════════════════════
    # ✅ v0.5.3: Cálculo dinámico de num_ctx
    # ✅ v0.7.5 (ISSUE-086): cuantizado a POTENCIA DE DOS (buckets)
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _calculate_num_ctx(prompt_len: int, num_predict: int) -> int:
        """
        Calcula num_ctx dinámico basado en el tamaño del prompt y
        tokens de salida solicitados.

        Sin esto, Ollama usa default 2048 y trunca el input
        silenciosamente cuando se envían archivos grandes.

        ✅ v0.7.5 (ISSUE-086) — el valor se CUANTIZA a la potencia de dos
        siguiente. Medido con Ollama 0.30.0 (prompt real de 69,8 KB):

          · num_ctx igual + prompt igual  → prefill **1 tok / 0,38 s** (cache
            de prefijo reutilizada; 15.444 tok / 318 s en frío ≈ 830×).
          · num_ctx distinto → **descarta TODO el KV cacheado**: con un delta de
            **+1** ya se re-evalúa el prompt completo (2249 tok / 35 s).
            `num_ctx` es parámetro de *carga* del modelo, no de request.

        La fórmula exacta (`prompt_len//3 + num_predict + 512`) devolvía un valor
        **distinto en cada turno** (el prompt crece ~24 car/turno ⇒ num_ctx +8):
        medido con el cliente real, una conversación de 3 turnos con prefijo
        compartido re-evaluaba 9570 tok / 177 s. Con buckets, los tres turnos
        comparten num_ctx y solo se evalúa la cola nueva. Cuantizar hacia arriba
        **no** trunca: el bucket siempre es ≥ lo requerido.

        Args:
            prompt_len: Longitud del prompt en caracteres
            num_predict: Tokens de salida solicitados

        Returns:
            num_ctx en potencias de dos, entre NUM_CTX_MIN y NUM_CTX_MAX
        """
        estimated_input_tokens = max(prompt_len // CHARS_PER_TOKEN, 512)
        required_ctx = estimated_input_tokens + num_predict + CTX_BUFFER
        num_ctx = NUM_CTX_MIN
        while num_ctx < required_ctx and num_ctx < NUM_CTX_MAX:
            num_ctx *= 2
        return min(num_ctx, NUM_CTX_MAX)

    def health_check(self, timeout: float = 5.0) -> bool:
        start = time.time()
        _log_event("OLLAMA_CLIENT", "Health check started", extra={"timeout": timeout, "url": f"{self.base_url}/api/tags"})
        try:
            session = self._get_client(timeout=timeout)
            try:
                response = session.get("/api/tags")
                success = response.status_code == 200
            finally:
                self._release_client(session)
            duration = time.time() - start
            _log_event("OLLAMA_CLIENT", f"Health check {'passed' if success else 'failed'}",
                       level="INFO" if success else "WARNING", extra={
                           "status_code": response.status_code, "duration_ms": round(duration * 1000, 2)
                       })
            _log_model_call(self.model, "health_check", "SYSTEM", duration=duration, success=success)
            return success
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "OllamaClientBase.health_check", extra={"url": f"{self.base_url}/api/tags", "timeout": timeout})
            _log_model_call(self.model, "health_check", "SYSTEM", duration=duration, success=False)
            logger.warning(f"Ollama health check failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # ✅ NUEVO: Pre-carga de modelo en VRAM
    # ═══════════════════════════════════════════════════════════════
    def preload_model(self, timeout: int = 120) -> bool:
        """
        Pre-carga el modelo en VRAM haciendo una petición mínima.
        Útil para evitar timeouts en la primera llamada real.

        Args:
            timeout: Timeout en segundos (default 120, suficiente para carga en frío)

        Returns:
            bool: True si el modelo se cargó correctamente
        """
        start = time.time()
        _log_event("OLLAMA_CLIENT", f"Pre-loading model: {self.model}", extra={"timeout": timeout})
        logger.info(f"[PRELOAD] Cargando modelo {self.model} en VRAM...")

        session = None
        try:
            session = self._get_client(timeout=timeout)
            payload = {
                "model": self.model,
                "prompt": "ready",
                "stream": False,
                "options": {"num_predict": 1, "temperature": 0.1, "num_ctx": NUM_CTX_MIN}
            }
            response = session.post("/api/generate", json=payload, timeout=timeout)
            response.raise_for_status()

            self.circuit_breaker.record_success()
            duration = time.time() - start
            _log_event("OLLAMA_CLIENT", f"Model pre-loaded successfully", extra={
                "model": self.model, "duration_seconds": round(duration, 2)
            })
            _log_model_call(self.model, "preload", "SYSTEM", duration=duration, success=True)
            logger.info(f"[PRELOAD] ✅ Modelo {self.model} cargado en {duration:.1f}s")
            return True

        except Exception as e:
            self.circuit_breaker.record_failure()
            duration = time.time() - start
            _log_event("OLLAMA_CLIENT", f"Model pre-load failed", level="WARNING", extra={
                "model": self.model, "error": str(e), "duration_seconds": round(duration, 2)
            })
            _log_model_call(self.model, "preload", "SYSTEM", duration=duration, success=False)
            logger.warning(f"[PRELOAD] ❌ Modelo {self.model} falló: {e}")
            return False

        finally:
            if session:
                self._release_client(session)

    # ═══════════════════════════════════════════════════════════════
    # ✅ 2026-09-29: `_think_flag` vive en la BASE (antes solo en `OllamaClient`,
    # subclase) porque `generate()` — el método que usan los AGENTES — está acá y
    # no lo aplicaba: medido en un A/B real, `qwen3:8b` por el camino del agente
    # gastó los 300 tokens de `num_predict` en "thinking" y devolvió **56 chars**
    # (vacío) en 126 s, contra una respuesta coherente de 800+ chars con el modelo
    # sin razonamiento. El fix de `ISSUE-099` había cubierto solo el chat.
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _think_flag(model: str) -> Dict[str, Any]:
        """`{"think": False}` para modelos con razonamiento, si está configurado.

        ✅ v0.7.5 (ISSUE-099): medido con qwen3:8b y un prompt real con fuentes
        (num_predict=431): **~400 tokens se fueron en "thinking"** ⇒ respuesta de
        192 chars con `done_reason=length` y, en la corrida real del usuario,
        respuesta **vacía**. Con `think: false`: 1275 chars, `done_reason=stop` y
        54 s contra 87 s. Se controla con `CHAT_DISABLE_THINKING`.

        ✅ 2026-09-29: aplicado también en `generate()` (camino de los agentes) —
        ver el bloque de comentario de arriba.
        """
        if not getattr(settings, "chat_disable_thinking", True):
            return {}
        familia = (model or "").lower()
        if any(f in familia for f in ("qwen3", "deepseek-r1", "reasoning")):
            return {"think": False}
        return {}

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.0.7: Añadido skip_model_swap para prevenir VRAM Thrashing
    # en llamadas de fallback internas.
    # ═══════════════════════════════════════════════════════════════
    def generate(self, prompt: str, system: Optional[str] = None, temperature: Optional[float] = None,
                 stream: bool = False, timeout: Optional[int] = None, task_type: str = "UNKNOWN",
                 use_cache: bool = True, skip_model_swap: bool = False, **kwargs) -> OllamaResponse:
        start_time = time.time()
        _log_event("OLLAMA_CLIENT", f"Generate started: {task_type}", extra={
            "model": self.model, "prompt_length": len(prompt), "temperature": temperature, "stream": stream, "task_type": task_type
        })
        # ✅ v0.6.8kk: evento MODEL ÚNICO y canónico por generate. El start NO
        # registraba "input_tokens" con len(prompt) (caracteres etiquetados como
        # tokens — 1816 en vez de 637 reales) y duplicaba el MODEL del completion.
        # El start/end ya está cubierto por OLLAMA_CLIENT ("Generate started"/
        # "Generate completed"); el MODEL de abajo (con tokens reales de Ollama:
        # prompt_eval_count/eval_count) es el único evento por request.

        # ── Cache check ──
        if use_cache and not stream:
            # ✅ FIX: max_tokens acá, mismo criterio que la línea ~656 más abajo
            # (kwargs.get("max_tokens", 4096)) — se necesita ANTES en el flujo
            # para el cache key, así que se computa temprano acá también.
            _cache_max_tokens = kwargs.get("max_tokens", 4096)
            cache = self._get_cache()
            cached = cache.get(self.model, prompt, temperature, system=system, max_tokens=_cache_max_tokens)
            if cached:
                elapsed = time.time() - start_time
                _log_event("OLLAMA_CLIENT", f"Cache HIT", extra={"model": self.model, "task_type": task_type, "duration_ms": round(elapsed * 1000, 2)})
                return OllamaResponse(content=cached["content"], model=cached["model"], done=cached["done"],
                                      total_duration=cached["total_duration"], load_duration=cached["load_duration"],
                                      prompt_eval_count=cached["prompt_eval_count"], eval_count=cached["eval_count"],
                                      eval_duration=cached["eval_duration"], raw=cached["raw"])

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX CRÍTICO: Fallback con cliente SEPARADO y disable_fallback
        # ═══════════════════════════════════════════════════════════════
        if not self.circuit_breaker.can_execute():
            _log_event("OLLAMA_CLIENT", "Circuit breaker OPEN", level="WARNING",
                       extra={"model": self.model, "state": self.circuit_breaker.state.value})
            _log_model_call(self.model, "generate", task_type, success=False, duration=0)

            # ✅ FIX RAC: Si disable_fallback=True, lanzar LLMError directo
            # Nunca cambia de modelo. Previene VRAM thrashing.
            if self.disable_fallback:
                _log_event("OLLAMA_CLIENT", "Fallback DISABLED — raising LLMError directly", level="WARNING",
                           extra={"model": self.model})
                raise LLMError(f"Circuit breaker OPEN para {self.model} (fallback deshabilitado)", model=self.model)

            fallback = getattr(settings, "llm_fallback_1", None)
            if fallback and fallback != self.model:
                _log_event("OLLAMA_CLIENT", f"Creating SEPARATE client for fallback: {fallback}", level="WARNING",
                           extra={"original_model": self.model, "fallback_model": fallback})
                logger.warning(f"[SELF_HEALING] Circuit breaker OPEN en {self.model}, "
                               f"usando fallback: {fallback} (cliente separado)")

                # ✅ FIX: Cliente separado con su PROPIO Circuit Breaker (CLOSED)
                try:
                    # ═══════════════════════════════════════════════════════════════
                    # ✅ FIX FASE 1: VRAM Thrashing prevention.
                    # disable_fallback=True evita recursión.
                    # skip_model_swap=True en llamada evita descargar el modelo
                    # principal que aún reside en VRAM y que reintará cargar luego.
                    # ═══════════════════════════════════════════════════════════════
                    fallback_client = self.__class__(
                        model=fallback,
                        timeout=timeout or self.timeout,
                        scenario=self.scenario,
                        disable_fallback=True
                    )
                    fallback_result = fallback_client.generate(
                        prompt=prompt, system=system, temperature=temperature,
                        stream=stream, timeout=timeout, task_type=task_type,
                        use_cache=False, skip_model_swap=True, **kwargs
                    )

                    # ═══════════════════════════════════════════════════════════════
                    # ✅ FIX FASE 1: Falso Positivo eliminado.
                    # El CB del modelo principal NO debe cerrarse si el fallback
                    # funciona. Solo debe cerrarse si el modelo principal responde.
                    # ═══════════════════════════════════════════════════════════════
                    _log_event("OLLAMA_CLIENT", f"Fallback succeeded, primary CB remains {self.circuit_breaker.state.value}",
                               extra={"original_model": self.model, "fallback_model": fallback})
                    return fallback_result
                except Exception as fallback_err:
                    _log_event("OLLAMA_CLIENT", f"Fallback {fallback} also failed", level="ERROR",
                               extra={"fallback_error": str(fallback_err)})
                    _log_error(fallback_err, "OllamaClientBase.generate.fallback",
                              extra={"original_model": self.model, "fallback_model": fallback})
                    raise LLMError(
                        f"Circuit breaker OPEN para {self.model} y fallback {fallback} también falló: {fallback_err}",
                        model=self.model
                    )

            _log_event("OLLAMA_CLIENT", f"No fallback available", level="ERROR",
                       extra={"model": self.model, "fallback": fallback})
            raise LLMError(f"Circuit breaker OPEN para {self.model}", model=self.model)

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.0.7: ModelSwap — asegurar modelo correcto en VRAM
        # Salteado si es llamada interna de fallback (skip_model_swap=True)
        # ═══════════════════════════════════════════════════════════════
        if not skip_model_swap:
            try:
                from src.core.model_swap import ModelSwap
                ModelSwap.instance().ensure(self.model)
            except ImportError:
                pass  # ModelSwap no disponible, continuar sin VRAM management
            except RuntimeError as _ms_err:
                _log_event("OLLAMA_CLIENT", f"ModelSwap ensure failed: {_ms_err}", level="ERROR",
                           extra={"model": self.model, "error": str(_ms_err)})
                raise LLMError(str(_ms_err), model=self.model)

        # ── Bucle de reintentos normal (CB CLOSED o HALF_OPEN) ──
        last_exception = None
        effective_timeout = self._get_timeout(timeout)
        session = None
        for attempt in range(self.max_retries):
            try:
                session = self._get_client(timeout=effective_timeout)

                effective_max_tokens = kwargs.get("max_tokens", 4096)

                # ✅ v0.7.5 (ISSUE-074/A3a): el presupuesto no puede exceder lo que
                # entra en el timeout de ESTA petición (TG y PP medidos).
                prompt_len = len(prompt) + (len(system) if system else 0)
                max_tokens_solicitado = effective_max_tokens
                effective_max_tokens = num_predict_coherente(
                    effective_max_tokens, effective_timeout, prompt_len)
                if effective_max_tokens < max_tokens_solicitado:
                    # ⚠️ Cuidado con los nombres: acá se usaba
                    # `TOKENS_POR_SEGUNDO_CPU`, que dejó de existir al reescribir
                    # las constantes (ahora `TG_TOK_S`) ⇒ NameError en runtime cada
                    # vez que se acotaba el presupuesto (medido: 2026-09-28 17:08,
                    # `OllamaClientBase.generate`, timeout=90 ⇒ el validator quedó
                    # sin LLM y la respuesta salió sin validar).
                    _t_prefill = (prompt_len // CHARS_PER_TOKEN) / PP_TOK_S
                    logger.warning(
                        f"[A3a] num_predict acotado {max_tokens_solicitado} → "
                        f"{effective_max_tokens} para timeout={effective_timeout}s "
                        f"(prompt≈{prompt_len // CHARS_PER_TOKEN} tok: prefill≈"
                        f"{_t_prefill:.0f}s + generación≈"
                        f"{effective_max_tokens / TG_TOK_S:.0f}s; a TG={TG_TOK_S} "
                        f"tok/s sin acotar la petición moriría por timeout)"
                    )

                # ✅ FIX v0.5.3: num_ctx dinámico basado en tamaño del prompt
                # ✅ v0.7.5 (ISSUE-086): cuantizado a potencias de dos (caché de prefijo)
                # ✅ 2026-09-29 (ISSUE-114): el caller puede FIJARLO con `num_ctx=`.
                # Medido en el RAC: sus 577 llamadas repiten el mismo system prompt
                # (~370 tokens) pero el `num_ctx` bailaba entre 2048/4096/8192/16384 y
                # **cada cambio descarta TODO el KV cacheado** (ISSUE-086) ⇒ se perdía
                # la reutilización del prefijo común y la RAM del KV se movía en cada
                # llamada.
                num_ctx = int(kwargs.get("num_ctx") or 0) or self._calculate_num_ctx(
                    prompt_len, effective_max_tokens)

                payload = {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": stream,
                    "options": {
                        "temperature": temperature if temperature is not None else settings.default_temperature,
                        "num_predict": effective_max_tokens,
                        "num_ctx": num_ctx,
                    },
                    # ✅ FIX histórico: sin esto Ollama usa su default de 5 minutos,
                    # forzando recargas frecuentes (confirmado en rafael.log real:
                    # load_duration=4.058s de 14.48s en una respuesta de 74 caracteres).
                    # ✅ FIX NUEVO: un "30m" fijo para TODO modelo pisaba, en cada
                    # llamada real, la distinción default/no-default de ModelSwap
                    # (KEEP_ALIVE_DEFAULT/TEMPORARY) — un modelo no-default (código,
                    # visión) quedaba cargado hasta 30min en vez de los 5min pensados
                    # para liberar RAM. Ahora replica el mismo criterio que ModelSwap.
                    "keep_alive": "30m" if self.model == settings.llm_default_model else "5m",
                    # ✅ 2026-09-29: sin esto, un modelo con razonamiento (qwen3)
                    # por el camino de los AGENTES gastaba TODO `num_predict` en
                    # "thinking" y devolvía vacío (medido: 300 tokens ⇒ 56 chars).
                    **self._think_flag(self.model),
                }
                if system:
                    payload["system"] = system

                _log_event("OLLAMA_CLIENT", f"Sending request (attempt {attempt + 1}/{self.max_retries})",
                           extra={
                               "model": self.model,
                               "prompt_length": len(prompt),
                               "timeout": effective_timeout,
                               "num_ctx": num_ctx,
                               "num_predict": effective_max_tokens,
                           })

                # ✅ ISSUE-137: con token (DETENER) el pedido va en modo flujo para
                # poder cortarlo; sin token es el `session.post()` de siempre.
                data = _datos_de_generacion(session, payload, effective_timeout,
                                            token_cancelacion_actual())
                self.circuit_breaker.record_success()
                ollama_response = OllamaResponse(
                    content=data.get("response", "") or "", model=data.get("model", self.model) or self.model,
                    done=bool(data.get("done", False)), total_duration=_safe_divide(data.get("total_duration"), 1e9),
                    load_duration=_safe_divide(data.get("load_duration"), 1e9),
                    prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
                    eval_count=int(data.get("eval_count", 0) or 0),
                    eval_duration=_safe_divide(data.get("eval_duration"), 1e9), raw=data)
                elapsed = time.time() - start_time
                _log_event("OLLAMA_CLIENT", f"Generate completed successfully", extra={
                    "model": self.model, "task_type": task_type, "duration_seconds": round(elapsed, 4),
                    "tokens_generated": ollama_response.eval_count, "tokens_prompt": ollama_response.prompt_eval_count,
                    "num_ctx_used": num_ctx, "num_predict_used": effective_max_tokens,
                    "num_predict_solicitado": max_tokens_solicitado,
                })
                _log_model_call(self.model, "generate", task_type, input_size=ollama_response.prompt_eval_count,
                                output_size=ollama_response.eval_count, duration=elapsed, success=True,
                                temperature=temperature, tokens_used=ollama_response.eval_count)
                if use_cache and not stream:
                    cache = self._get_cache()
                    cache.set(self.model, prompt, temperature, {"content": ollama_response.content, "model": ollama_response.model,
                                                                "done": ollama_response.done, "total_duration": ollama_response.total_duration,
                                                                "load_duration": ollama_response.load_duration, "prompt_eval_count": ollama_response.prompt_eval_count,
                                                                "eval_count": ollama_response.eval_count, "eval_duration": ollama_response.eval_duration, "raw": ollama_response.raw},
                              system=system, max_tokens=effective_max_tokens)
                if self.is_first_request:
                    self.is_first_request = False
                    _log_event("OLLAMA_CLIENT", "First request completed - switching to standard timeout")
                return ollama_response
            except Exception as e:
                last_exception = e
                self.circuit_breaker.record_failure()
                elapsed = time.time() - start_time
                # ✅ ISSUE-137: si el corte lo pidió el usuario, NO se reintenta
                # (reintentar volvería a lanzar la generación que quiso detener).
                if isinstance(e, GeneracionCancelada):
                    _log_event("OLLAMA_CLIENT", "Generación cancelada por el usuario",
                               level="INFO", extra={"model": self.model,
                                                    "task_type": task_type})
                    raise
                is_timeout = isinstance(e, (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout))
                _log_event("OLLAMA_CLIENT", f"Attempt {attempt + 1}/{self.max_retries} failed", level="WARNING",
                           extra={"model": self.model, "task_type": task_type, "error_type": type(e).__name__, "is_timeout": is_timeout})
                if is_timeout:
                    _log_event("OLLAMA_CLIENT", "Timeout — skipping retries", level="WARNING")
                    break
                if attempt < self.max_retries - 1:
                    delay = self._calculate_backoff(attempt)
                    _log_event("OLLAMA_CLIENT", f"Retrying in {delay:.2f}s...", extra={"next_attempt": attempt + 2})
                    time.sleep(delay)
            finally:
                if session:
                    self._release_client(session)
                    session = None

        total_duration = time.time() - start_time
        _log_error(last_exception, "OllamaClientBase.generate", extra={
            "model": self.model, "task_type": task_type, "total_attempts": self.max_retries,
            "total_duration_seconds": round(total_duration, 4), "traceback": traceback.format_exc()
        })
        _log_model_call(self.model, "generate", task_type, input_size=len(prompt), duration=total_duration, success=False)
        raise LLMError(f"Failed after {self.max_retries} attempts: {last_exception}", model=self.model)

    def embed(self, text: str, model: Optional[str] = None, timeout: Optional[int] = None, use_cache: bool = True) -> List[float]:
        """
        Genera embeddings para texto.

        v0.5.2: Si el modelo es BAAI/bge-m3, usa el singleton local
        (sin HTTP, sin dependencia de Ollama). Para otros modelos,
        usa la ruta HTTP normal.
        """
        start_time = time.time()
        embed_model = model or settings.llm_embed_model

        # ✅ v0.5.2: Redirigir BGE-M3 al singleton local
        if "bge-m3" in embed_model.lower():
            try:
                from src.memory.embedding_model_singleton import get_embedding_model
                singleton = get_embedding_model()
                embedding = singleton.embed(text)
                if embedding is not None:
                    elapsed = time.time() - start_time
                    _log_event("OLLAMA_CLIENT", f"Embed via LOCAL singleton (skipped HTTP)",
                               extra={"model": embed_model, "embedding_dimension": len(embedding), "duration_ms": round(elapsed * 1000, 2)})
                    _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text),
                                   output_size=len(embedding), duration=elapsed, success=True)
                    return embedding
            except Exception as e:
                _log_event("OLLAMA_CLIENT", f"Singleton failed, falling back to HTTP", level="WARNING",
                           extra={"model": embed_model, "error": str(e)})

        # Para otros modelos o si singleton falla: ruta HTTP normal
        _log_event("OLLAMA_CLIENT", f"Embed started (HTTP)", extra={"model": embed_model, "text_length": len(text)})
        _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text))

        if use_cache:
            cache = self._get_cache()
            cached = cache.get(embed_model, text, None)
            if cached and "embedding" in cached:
                elapsed = time.time() - start_time
                _log_event("OLLAMA_CLIENT", f"Embed cache HIT", extra={"model": embed_model, "duration_ms": round(elapsed * 1000, 2)})
                return cached["embedding"]

        effective_timeout = timeout or settings.ollama_timeout_embed
        session = None
        try:
            session = self._get_client(timeout=effective_timeout)
            payload = {
                "model": embed_model,
                "prompt": text,
                "keep_alive": getattr(settings, "ollama_keep_alive", "30m"),
            }
            _log_event("OLLAMA_CLIENT", f"Sending embed request", extra={"model": embed_model})
            response = session.post(f"{self.base_url}/api/embeddings", json=payload, timeout=effective_timeout)
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding", [])
            elapsed = time.time() - start_time
            _log_event("OLLAMA_CLIENT", f"Embed completed", extra={"model": embed_model, "embedding_dimension": len(embedding)})
            _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text), output_size=len(embedding), duration=elapsed, success=True)
            if use_cache:
                cache = self._get_cache()
                cache.set(embed_model, text, None, {"embedding": embedding})
            return embedding
        except Exception as e:
            elapsed = time.time() - start_time
            _log_error(e, "OllamaClientBase.embed", extra={"model": embed_model, "text_length": len(text)})
            _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text), duration=elapsed, success=False)
            raise LLMError(f"Embeddings failed: {e}", model=embed_model)
        finally:
            if session:
                self._release_client(session)
