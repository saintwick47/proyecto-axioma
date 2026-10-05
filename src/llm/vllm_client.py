#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.0 — Cliente vLLM (Backend Opcional)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/llm/vllm_client.py
# Autor: SaintWick
# Fecha: 2026-04-11
#
# Propósito: Backend vLLM opcional con el mismo contrato público que OllamaClient.
#            Permite switch Ollama → vLLM < 500ms sin pérdida de contexto.
#            Import seguro — si vLLM no está disponible, el sistema usa Ollama.
#
# Contrato respetado (igual a OllamaClient):
#   generate(prompt, system, temperature, stream, timeout, task_type) → OllamaResponse
#   embed(text, model, timeout, use_cache) → List[float]
#   health_check(timeout) → bool
#   chat(messages, temperature, timeout, task_type) → OllamaResponse
#   list_models(timeout) → List[Dict]
#   close() → None
#
# Activación:
#   from src.llm.vllm_client import get_vllm_client, is_vllm_available
#   if is_vllm_available():
#       client = get_vllm_client()
#   else:
#       client = OllamaClient()
#
# Switch en dispatcher (< 500ms):
#   from src.llm.vllm_client import get_active_client
#   client = get_active_client()  # retorna vLLM si disponible, Ollama si no
# ═══════════════════════════════════════════════════════════════
# ✅ v0.5.0 (Fase 4) — Archivo nuevo. Import seguro total.
#             No rompe nada si vLLM no está instalado.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# ── Imports desde client_base (contrato compartido) ──────────────────────────
from src.llm.client_base import (
    OllamaResponse,
    CircuitBreaker,
    CircuitState,
    RequestCache,
    _log_event,
    _log_error,
    _log_model_call,
    _safe_divide,
)
from src.domain.exceptions import LLMError

# ── Import seguro httpx ───────────────────────────────────────────────────────
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False

# ── Detección de vLLM server ──────────────────────────────────────────────────
# vLLM expone una API REST compatible con OpenAI — no requiere el paquete vllm
# instalado localmente. Solo necesita que el servidor esté corriendo.
_VLLM_AVAILABLE: Optional[bool] = None
_VLLM_LOCK = threading.Lock()


def is_vllm_available(host: str = "http://localhost:8000", timeout: float = 2.0) -> bool:
    """
    Verifica si hay un servidor vLLM activo en el host dado.
    Cachea el resultado por sesión — no hace ping en cada llamada.
    """
    global _VLLM_AVAILABLE
    if _VLLM_AVAILABLE is not None:
        return _VLLM_AVAILABLE

    with _VLLM_LOCK:
        if _VLLM_AVAILABLE is not None:
            return _VLLM_AVAILABLE

        if not _HTTPX_AVAILABLE:
            _VLLM_AVAILABLE = False
            return False

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{host}/health")
                _VLLM_AVAILABLE = response.status_code == 200
        except Exception:
            _VLLM_AVAILABLE = False

        logger.info(f"[vLLM] Disponible: {_VLLM_AVAILABLE} — {host}")
        return _VLLM_AVAILABLE


def reset_vllm_availability_cache() -> None:
    """Fuerza re-detección en el próximo is_vllm_available(). Para tests."""
    global _VLLM_AVAILABLE
    _VLLM_AVAILABLE = None


# ============================================================================
# VLLM CONFIG
# ============================================================================

@dataclass
class VLLMConfig:
    """Configuración del cliente vLLM."""
    host: str = "http://localhost:8000"
    model: str = "mistralai/Mistral-7B-Instruct-v0.2"
    timeout: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.7
    top_p: float = 0.9
    cache_size: int = 500
    max_retries: int = 2
    # Para embeddings: vLLM no soporta /embeddings en todas las versiones
    # fallback a Ollama si falla
    embed_fallback_to_ollama: bool = True


# ============================================================================
# VLLM CLIENT — Mismo contrato que OllamaClient
# ============================================================================

class VLLMClient:
    """
    Cliente para servidor vLLM con API compatible OpenAI.

    Implementa el mismo contrato público que OllamaClient:
        generate(), embed(), health_check(), chat(), list_models(), close()

    El Dispatcher puede usar VLLMClient en lugar de OllamaClient sin modificaciones.
    Switch < 500ms: solo reemplazar la instancia del cliente.

    Endpoints usados:
        POST /v1/completions     → generate()
        POST /v1/chat/completions → chat()
        POST /v1/embeddings      → embed()
        GET  /v1/models          → list_models()
        GET  /health             → health_check()
    """

    def __init__(self, config: Optional[VLLMConfig] = None) -> None:
        self.config = config or VLLMConfig()
        self.model = self.config.model
        self.base_url = self.config.host
        self.timeout = self.config.timeout
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=30.0,
        )
        self.max_retries = self.config.max_retries
        self._cache = RequestCache(max_size=self.config.cache_size)
        self._session: Optional[Any] = None
        self.is_first_request = True

        logger.info(
            f"[vLLM] Client inicializado — host={self.base_url} model={self.model}"
        )

    # ── Contrato público (igual a OllamaClient) ───────────────────────────────

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        task_type: str = "UNKNOWN",
        use_cache: bool = True,
        **kwargs,
    ) -> OllamaResponse:
        """
        Genera respuesta usando vLLM /v1/completions.
        Retorna OllamaResponse — mismo tipo que OllamaClient.generate().
        """
        t0 = time.perf_counter()
        _log_model_call(self.model, "generate", task_type, input_size=len(prompt))

        # Cache check
        if use_cache and not stream:
            cached = self._cache.get(self.model, prompt, temperature)
            if cached:
                return OllamaResponse(
                    content=cached["content"], model=cached["model"],
                    done=True, total_duration=0.0, load_duration=0.0,
                    prompt_eval_count=cached.get("prompt_eval_count", 0),
                    eval_count=cached.get("eval_count", 0),
                    eval_duration=0.0, raw=cached,
                )

        if not self.circuit_breaker.can_execute():
            raise LLMError(f"[vLLM] Circuit breaker OPEN para {self.model}", model=self.model)

        effective_temp = temperature if temperature is not None else self.config.temperature
        effective_timeout = timeout or self.timeout

        # Construir prompt completo si hay system
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "max_tokens": self.config.max_tokens,
            "temperature": effective_temp,
            "top_p": self.config.top_p,
            "stream": False,  # streaming manejado en generate_stream
        }

        last_exc = None
        for attempt in range(max(1, self.max_retries)):
            try:
                with httpx.Client(timeout=effective_timeout) as client:
                    response = client.post(
                        f"{self.base_url}/v1/completions",
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()

                self.circuit_breaker.record_success()
                self.is_first_request = False

                choice = data.get("choices", [{}])[0]
                content = choice.get("text", "").strip()
                usage = data.get("usage", {})

                ollama_resp = OllamaResponse(
                    content=content,
                    model=data.get("model", self.model),
                    done=True,
                    total_duration=(time.perf_counter() - t0),
                    load_duration=0.0,
                    prompt_eval_count=usage.get("prompt_tokens", 0),
                    eval_count=usage.get("completion_tokens", 0),
                    eval_duration=(time.perf_counter() - t0),
                    raw=data,
                )

                if use_cache:
                    self._cache.set(self.model, prompt, temperature, {
                        "content": content, "model": ollama_resp.model,
                        "prompt_eval_count": ollama_resp.prompt_eval_count,
                        "eval_count": ollama_resp.eval_count,
                    })

                latency_ms = (time.perf_counter() - t0) * 1000
                _log_model_call(
                    self.model, "generate", task_type,
                    input_size=ollama_resp.prompt_eval_count,
                    output_size=ollama_resp.eval_count,
                    duration=latency_ms / 1000, success=True,
                )
                return ollama_resp

            except Exception as e:
                last_exc = e
                self.circuit_breaker.record_failure()
                logger.warning(f"[vLLM] generate attempt {attempt+1} falló: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))

        _log_model_call(self.model, "generate", task_type, success=False)
        raise LLMError(f"[vLLM] generate falló tras {self.max_retries} intentos: {last_exc}", model=self.model)

    def generate_stream(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        task_type: str = "UNKNOWN",
        **kwargs,
    ) -> Generator[str, None, None]:
        """Genera respuesta en streaming via vLLM SSE."""
        if not self.circuit_breaker.can_execute():
            raise LLMError(f"[vLLM] Circuit breaker OPEN", model=self.model)

        effective_temp = temperature if temperature is not None else self.config.temperature
        effective_timeout = timeout or self.timeout
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "max_tokens": self.config.max_tokens,
            "temperature": effective_temp,
            "stream": True,
        }

        try:
            with httpx.Client(timeout=effective_timeout) as client:
                with client.stream("POST", f"{self.base_url}/v1/completions", json=payload) as response:
                    response.raise_for_status()
                    self.circuit_breaker.record_success()
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                import json
                                data = json.loads(data_str)
                                chunk = data.get("choices", [{}])[0].get("text", "")
                                if chunk:
                                    yield chunk
                            except Exception:
                                continue
        except Exception as e:
            self.circuit_breaker.record_failure()
            raise LLMError(f"[vLLM] stream falló: {e}", model=self.model)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        task_type: str = "CHAT",
        use_cache: bool = False,
        **kwargs,
    ) -> OllamaResponse:
        """Chat con historial usando vLLM /v1/chat/completions."""
        t0 = time.perf_counter()

        if not self.circuit_breaker.can_execute():
            raise LLMError(f"[vLLM] Circuit breaker OPEN", model=self.model)

        effective_temp = temperature if temperature is not None else self.config.temperature
        effective_timeout = timeout or self.timeout

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": effective_temp,
            "top_p": self.config.top_p,
            "stream": False,
        }

        try:
            with httpx.Client(timeout=effective_timeout) as client:
                response = client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

            self.circuit_breaker.record_success()
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "").strip()
            usage = data.get("usage", {})

            return OllamaResponse(
                content=content,
                model=data.get("model", self.model),
                done=True,
                total_duration=(time.perf_counter() - t0),
                load_duration=0.0,
                prompt_eval_count=usage.get("prompt_tokens", 0),
                eval_count=usage.get("completion_tokens", 0),
                eval_duration=(time.perf_counter() - t0),
                raw=data,
            )

        except Exception as e:
            self.circuit_breaker.record_failure()
            raise LLMError(f"[vLLM] chat falló: {e}", model=self.model)

    def embed(
        self,
        text: str,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        use_cache: bool = True,
    ) -> List[float]:
        """
        Genera embeddings via vLLM /v1/embeddings.
        Fallback a OllamaClient.embed() si vLLM no soporta embeddings.
        """
        embed_model = model or self.model
        effective_timeout = timeout or self.timeout

        # Cache check
        if use_cache:
            cached = self._cache.get(embed_model, text, None)
            if cached and "embedding" in cached:
                return cached["embedding"]

        try:
            with httpx.Client(timeout=effective_timeout) as client:
                response = client.post(
                    f"{self.base_url}/v1/embeddings",
                    json={"model": embed_model, "input": text},
                )
                response.raise_for_status()
                data = response.json()
                embedding = data["data"][0]["embedding"]

            if use_cache:
                self._cache.set(embed_model, text, None, {"embedding": embedding})

            return embedding

        except Exception as e:
            if self.config.embed_fallback_to_ollama:
                logger.warning(f"[vLLM] embed falló ({e}) — fallback a Ollama")
                try:
                    from src.llm.client import OllamaClient
                    ollama = OllamaClient()
                    return ollama.embed(text=text, model=embed_model, timeout=int(effective_timeout))
                except Exception as e2:
                    raise LLMError(f"[vLLM+Ollama] embed falló: {e2}", model=embed_model)
            raise LLMError(f"[vLLM] embed falló: {e}", model=embed_model)

    def health_check(self, timeout: float = 5.0) -> bool:
        """Verifica si el servidor vLLM está activo."""
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{self.base_url}/health")
                ok = response.status_code == 200
                if ok:
                    self.circuit_breaker.record_success()
                return ok
        except Exception:
            return False

    def list_models(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Lista modelos disponibles en el servidor vLLM."""
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(f"{self.base_url}/v1/models")
                response.raise_for_status()
                data = response.json()
                return [{"name": m.get("id", ""), **m} for m in data.get("data", [])]
        except Exception as e:
            raise LLMError(f"[vLLM] list_models falló: {e}", model=self.model)

    def close(self) -> None:
        """Libera recursos. Compatible con OllamaClient.close()."""
        self._session = None
        logger.debug("[vLLM] Client cerrado")

    def get_cache_stats(self) -> Dict[str, Any]:
        return self._cache.get_stats()

    def clear_cache(self) -> None:
        self._cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __repr__(self) -> str:
        return f"<VLLMClient model={self.model!r} host={self.base_url!r}>"


# ============================================================================
# SINGLETON + FACTORY
# ============================================================================

_vllm_instance: Optional[VLLMClient] = None
_vllm_instance_lock = threading.Lock()


def get_vllm_client(config: Optional[VLLMConfig] = None) -> VLLMClient:
    """
    Retorna singleton de VLLMClient.
    Thread-safe. Crea instancia en el primer llamado.
    """
    global _vllm_instance
    if _vllm_instance is None:
        with _vllm_instance_lock:
            if _vllm_instance is None:
                _vllm_instance = VLLMClient(config=config)
    return _vllm_instance


def get_active_client(
    vllm_host: str = "http://localhost:8000",
    prefer_vllm: bool = True,
) -> Any:
    """
    Retorna el cliente LLM activo.

    Si prefer_vllm=True y vLLM está disponible → VLLMClient.
    En caso contrario → OllamaClient.

    Switch < 500ms: ambos clientes respetan el mismo contrato público.
    Uso desde Dispatcher:
        from src.llm.vllm_client import get_active_client
        self.llm_client = get_active_client()
    """
    if prefer_vllm and is_vllm_available(host=vllm_host):
        logger.info("[LLM] Usando backend vLLM")
        return get_vllm_client()

    logger.info("[LLM] Usando backend Ollama")
    from src.llm.client import OllamaClient
    return OllamaClient()


# ═══════════════════════════════════════════════════════════════
# 📋 CAMBIOS REALIZADOS v0.5.0 (Fase 4) — ARCHIVO NUEVO
# ═══════════════════════════════════════════════════════════════
# | Componente          | Propósito                                          |
# |---------------------|----------------------------------------------------|
# | VLLMConfig          | host, model, timeout, max_tokens, temperature      |
# | VLLMClient          | Contrato idéntico a OllamaClient                   |
# | generate()          | /v1/completions + cache + circuit breaker          |
# | generate_stream()   | /v1/completions streaming SSE                      |
# | chat()              | /v1/chat/completions                               |
# | embed()             | /v1/embeddings + fallback a Ollama                 |
# | health_check()      | GET /health                                        |
# | list_models()       | GET /v1/models                                     |
# | is_vllm_available() | Detección lazy del servidor vLLM                   |
# | get_vllm_client()   | Singleton accessor                                 |
# | get_active_client() | Auto-selecciona vLLM o Ollama — switch < 500ms    |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
