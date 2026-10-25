#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# client_base.py - Cliente Base Ollama (Infraestructura)
# Ruta: /ruta/a/axioma/src/llm/client_base.py
# Autor: SaintWick
# Versión: 1.0.8
#
# Proporciona la clase `OllamaClientBase`: sesiones HTTP, cache,
# circuit breaker, backoff exponencial, timeouts adaptativos,
# pre-carga de modelos, generación con cancelación y embeddings.
#
# ✅ Split 50/50 (2026-10-22): la INFRAESTRUCTURA que vivía acá
# (RequestCache, HTTPSessionPool, CircuitBreaker, helpers de logging,
# OllamaResponse, constantes de contexto/presupuesto,
# num_predict_coherente y el sistema de cancelación/flujo) se movió a
# `client_base_infra.py`. En este archivo queda `OllamaClientBase` y se
# RE-IMPORTAN los nombres movidos: la API pública no cambia
# (`from src.llm.client_base import X` sigue funcionando igual).
# ═══════════════════════════════════════════════════════════════
import logging
import random
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

import httpx

from config.settings import settings
from src.domain.exceptions import LLMError

try:
    from src.core.self_healing import get_monitor as _get_healing_monitor
    _HEALING_AVAILABLE = True
except ImportError:
    _HEALING_AVAILABLE = False
    _get_healing_monitor = None

logger = logging.getLogger(__name__)

# ── Re-exports de la mitad movida (API pública intacta) ──────────────────────
from src.llm.client_base_infra import (  # noqa: E402, F401 — re-export: API pública intacta
    RequestCache, HTTPSessionPool,
    LOGGER_AVAILABLE, get_logger,
    _log_model_call, _log_error, _log_event, _log_file,
    CircuitState, ScenarioType, CircuitBreaker,
    tokens_de_respuesta, OllamaResponse, _safe_divide,
    NUM_CTX_MIN, NUM_CTX_MAX, CHARS_PER_TOKEN, CTX_BUFFER,
    TG_TOK_S, PP_TOK_S, MARGEN_PRESUPUESTO, DEFAULT_NUM_PREDICT,
    num_predict_coherente,
    _token_cancelacion, GeneracionCancelada, TokenCancelacion,
    token_cancelacion_actual, usar_token_cancelacion,
    _generate_json_por_flujo, _datos_de_generacion,
    log_degraded,
)


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
        _log_event("OLLAMA_CLIENT", "Session acquired from pool", extra={"base_url": self.base_url, "timeout": effective_timeout})
        return session

    def _release_client(self, session: httpx.Client) -> None:
        if self._session_pool:
            self._session_pool.release(session)
        _log_event("OLLAMA_CLIENT", "Session released to pool")

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
            _log_event("OLLAMA_CLIENT", "Model pre-loaded successfully", extra={
                "model": self.model, "duration_seconds": round(duration, 2)
            })
            _log_model_call(self.model, "preload", "SYSTEM", duration=duration, success=True)
            logger.info(f"[PRELOAD] ✅ Modelo {self.model} cargado en {duration:.1f}s")
            return True

        except Exception as e:
            self.circuit_breaker.record_failure()
            duration = time.time() - start
            _log_event("OLLAMA_CLIENT", "Model pre-load failed", level="WARNING", extra={
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
        if not settings.chat_disable_thinking:
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
                _log_event("OLLAMA_CLIENT", "Cache HIT", extra={"model": self.model, "task_type": task_type, "duration_ms": round(elapsed * 1000, 2)})
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

            fallback = settings.llm_fallback_1
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

            _log_event("OLLAMA_CLIENT", "No fallback available", level="ERROR",
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
                    "keep_alive": settings.ollama_keep_alive,
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
                _log_event("OLLAMA_CLIENT", "Generate completed successfully", extra={
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
                    _log_event("OLLAMA_CLIENT", "Embed via LOCAL singleton (skipped HTTP)",
                               extra={"model": embed_model, "embedding_dimension": len(embedding), "duration_ms": round(elapsed * 1000, 2)})
                    _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text),
                                   output_size=len(embedding), duration=elapsed, success=True)
                    return embedding
            except Exception as e:
                _log_event("OLLAMA_CLIENT", "Singleton failed, falling back to HTTP", level="WARNING",
                           extra={"model": embed_model, "error": str(e)})

        # Para otros modelos o si singleton falla: ruta HTTP normal
        _log_event("OLLAMA_CLIENT", "Embed started (HTTP)", extra={"model": embed_model, "text_length": len(text)})
        _log_model_call(embed_model, "embed", "EMBEDDING", input_size=len(text))

        if use_cache:
            cache = self._get_cache()
            cached = cache.get(embed_model, text, None)
            if cached and "embedding" in cached:
                elapsed = time.time() - start_time
                _log_event("OLLAMA_CLIENT", "Embed cache HIT", extra={"model": embed_model, "duration_ms": round(elapsed * 1000, 2)})
                return cached["embedding"]

        effective_timeout = timeout or settings.ollama_timeout_embed
        session = None
        try:
            session = self._get_client(timeout=effective_timeout)
            payload = {
                "model": embed_model,
                "prompt": text,
                "keep_alive": settings.ollama_keep_alive,
            }
            _log_event("OLLAMA_CLIENT", "Sending embed request", extra={"model": embed_model})
            response = session.post(f"{self.base_url}/api/embeddings", json=payload, timeout=effective_timeout)
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding", [])
            elapsed = time.time() - start_time
            _log_event("OLLAMA_CLIENT", "Embed completed", extra={"model": embed_model, "embedding_dimension": len(embedding)})
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
