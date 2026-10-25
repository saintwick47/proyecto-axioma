#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# client_base_infra.py - Infraestructura del cliente base Ollama
# Ruta: /ruta/a/axioma/src/llm/client_base_infra.py
# Autor: SaintWick
# Versión: 1.0.8
#
# Mitad EXTRAÍDA de `client_base.py` (split 50/50, 2026-10-22).
# Contiene la infraestructura que comparten los clientes:
#   · RequestCache (LRU) y HTTPSessionPool (pool de conexiones)
#   · CircuitState, ScenarioType y CircuitBreaker
#   · Logging de modelo/errores/eventos/archivos (_log_model_call,
#     _log_error, _log_event, _log_file) y LOGGER_AVAILABLE/get_logger
#   · OllamaResponse, tokens_de_respuesta y _safe_divide
#   · Constantes de contexto/presupuesto (NUM_CTX_*, TG_TOK_S, PP_TOK_S,
#     MARGEN_PRESUPUESTO, DEFAULT_NUM_PREDICT) y num_predict_coherente
#   · Cancelación real de pedidos (TokenCancelacion, GeneracionCancelada,
#     token_cancelacion_actual, usar_token_cancelacion) y helpers de flujo
#     (_generate_json_por_flujo, _datos_de_generacion)
#
# NO importa nada de `client_base.py`: la dependencia es en UN SOLO SENTIDO
# (client_base → client_base_infra), así no hay ciclo (AUD-CIR-001).
# El original re-importa estos nombres, así que la API pública no cambia.
# ═══════════════════════════════════════════════════════════════
import contextvars
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.domain.exceptions import LLMError
from src.utils.degradation import log_degraded

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

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
        _log_event("CIRCUIT_BREAKER", "SUCCESS → CLOSED", extra={"failure_count": 0, "state": self.state.value})

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
