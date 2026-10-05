#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# client.py - Cliente Ollama — Operaciones de consulta y negocio
# Ruta: /ruta/a/axioma/src/llm/client.py
# Autor: SaintWick
# Versión: 0.2.0
#
# Extiende OllamaClientBase para operaciones de alto nivel:
# generación con streaming, chat con historial y soporte para
# tool calling nativo. Incluye circuit breaker, retry exponencial,
# timeouts progresivos, cache LRU y pooling de sesiones HTTP.
# ═══════════════════════════════════════════════════════════════
import time
import json
import logging
import traceback
import httpx
from typing import Optional, Dict, Any, List, Generator, Callable
from config.settings import settings
from src.domain.exceptions import LLMError
# ✅ CORREGIDO: Importar clase base + todas las clases necesarias desde módulo hermano
from src.llm.client_base import (
    OllamaClientBase, _log_model_call, _log_error, _log_event, GeneracionCancelada,
    RequestCache, HTTPSessionPool, LOGGER_AVAILABLE, OllamaResponse,
    CircuitBreaker, CircuitState, ScenarioType,
    num_predict_coherente, DEFAULT_NUM_PREDICT, TG_TOK_S, PP_TOK_S, CHARS_PER_TOKEN,
)
logger = logging.getLogger(__name__)


def _evento_de_descarga(evento: Dict[str, Any], modelo: str) -> Dict[str, Any]:
    """Traduce un evento crudo de Ollama (`/api/pull`) a algo que la interfaz pueda mostrar.

    Ollama manda líneas como `{"status": "pulling manifest"}` y, mientras baja,
    `{"status": "downloading digest", "completed": N, "total": M}`. El porcentaje sólo se
    puede calcular cuando viene `total`; en los demás casos va `None` (mejor decir "no sé"
    que inventar un número).
    """
    total = evento.get("total") or 0
    completado = evento.get("completed") or 0
    porcentaje = round(100 * completado / total, 1) if total else None
    return {
        "modelo": modelo,
        "estado": str(evento.get("status", "")),
        "completado": completado,
        "total": total,
        "porcentaje": porcentaje,
    }

def _safe_divide(value: Any, divisor: float) -> Optional[float]:
    """División segura — retorna None si value es None o divisor es 0."""
    if value is None or divisor == 0:
        return None
    try:
        return float(value) / divisor
    except (TypeError, ValueError):
        return None

# ============================================================================
# ✅ RE-EXPORTS — Mantener compatibilidad con tests y código existente
# ============================================================================
__all__ = [
    'OllamaClient',
    'OllamaClientBase',
    'OllamaResponse',
    'CircuitBreaker',
    'CircuitState',
    'ScenarioType',
    'RequestCache',
    'HTTPSessionPool',
]

# ============================================================================
# CLASE PÚBLICA: OllamaClient (hereda de OllamaClientBase)
# ============================================================================
class OllamaClient(OllamaClientBase):
    """
    Cliente para Ollama — Operaciones de consulta y negocio.
    Interfaz pública idéntica al original para compatibilidad con tests existentes.
    Features heredados:
    - Circuit breaker para fallos en cascada
    - Retry exponencial con backoff
    - Timeouts progresivos
    - HTTP session pooling (heredado)
    - Request cache LRU (heredado)
    """

    # ✅ 2026-09-29: `_think_flag()` se movió a `OllamaClientBase` (client_base.py).
    # Vivía SOLO acá (subclase) y por eso no llegaba a `OllamaClientBase.generate()`,
    # que es el método que usan los AGENTES: medido en un A/B real, `qwen3:8b` por
    # el camino del agente gastó los 300 tokens de `num_predict` en "thinking" y
    # devolvió **56 chars** (respuesta vacía) en 126 s. Ahora la lógica es única y
    # la aplican los 4 caminos (`generate`, `generate_stream`, `chat`, `chat_stream`).

    def _opciones_coherentes(self, texto_len: int, timeout: float,
                             temperature: Optional[float],
                             max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """`options` de un request, con los dos invariantes medidos (v0.7.5).

        1. **ISSUE-086** — `num_ctx` en potencias de dos: Ollama descarta TODO el
           KV cacheado si `num_ctx` cambia (medido: un delta de **+1** fuerza el
           re-prefill completo; con el mismo valor, 1 tok / 0,38 s). Valor estable
           ⇒ el prefijo de la conversación se reutiliza entre turnos (medido:
           9570 → 3228 tokens re-evaluados en 3 turnos; turnos 2 y 3 en 1,8 s).
        2. **ISSUE-074/A3a** — `num_predict` acotado al timeout de la petición:
           pedir 4096 tokens con 180 s (650 s necesarios a 6,3 tok/s) garantizaba
           timeout + reintento. Se acota y se registra el recorte.

        Antes de esto, `chat()`/`chat_stream()`/`generate_stream()` **no enviaban
        `num_ctx` ni `num_predict`**: el servidor usaba su default (2048/4096) y
        truncaba el historial en silencio, y la generación no tenía tope.
        """
        preset = max_tokens if max_tokens is not None else DEFAULT_NUM_PREDICT
        num_predict = num_predict_coherente(preset, timeout, texto_len)
        if num_predict < preset:
            previsto = texto_len // CHARS_PER_TOKEN / PP_TOK_S + num_predict / TG_TOK_S
            logger.warning(
                f"[A3a] num_predict acotado {preset} → {num_predict} para "
                f"timeout={timeout}s (prompt≈{texto_len // CHARS_PER_TOKEN} tok: "
                f"prefill≈{texto_len // CHARS_PER_TOKEN / PP_TOK_S:.0f}s + "
                f"generación≈{num_predict / TG_TOK_S:.0f}s = {previsto:.0f}s)"
            )
            # El aviso de "prompt demasiado grande" mira SOLO el prefill (antes
            # miraba el total y decía "el prompt casi agota el timeout" cuando en
            # realidad lo agotaba la generación: aviso engañoso en los logs).
            _t_prefill = (texto_len // CHARS_PER_TOKEN) / PP_TOK_S
            if _t_prefill > timeout * 0.5:
                logger.warning(
                    f"[A3a] el PREFILL del prompt ({_t_prefill:.0f}s) usa más de la "
                    f"mitad del timeout ({timeout}s): esta ruta necesita más "
                    f"timeout o un prompt más chico"
                )
        return {
            "temperature": temperature if temperature is not None else settings.default_temperature,
            "num_predict": num_predict,
            "num_ctx": self._calculate_num_ctx(texto_len, num_predict),
        }

    def generate_stream(self, prompt: str, system: Optional[str] = None,
                        temperature: Optional[float] = None, timeout: Optional[int] = None,
                        task_type: str = "UNKNOWN", **kwargs) -> Generator[str, None, None]:
        """
        Genera respuesta en streaming.

        ✅ FIX v0.1.2: GeneratorExit manejado explícitamente dentro del
        context manager session.stream(). Cuando el consumer del generator
        deja de iterar (cancelación de UI, timeout del caller, break),
        Python lanza GeneratorExit en el punto de yield activo. Sin este
        handler, response.__exit__ no se ejecutaba y la conexión HTTP
        quedaba abierta en el pool de httpx indefinidamente.

        El flujo correcto es:
          GeneratorExit recibido → response.close() → limpieza normal del
          context manager → finally libera session al pool → return.
        """
        start_time = time.time()
        chunks_count = 0
        _log_event("OLLAMA_CLIENT", f"Generate stream started: {task_type}", extra={
            "model": self.model, "prompt_length": len(prompt),
            "temperature": temperature, "task_type": task_type
        })
        # ✅ v0.6.8kk: sin caracteres etiquetados como tokens en el start
        # (len(prompt) NO son input_tokens). El modelo solo reporta tokens
        # reales al completar.
        _log_model_call(self.model, "generate_stream", task_type,
                        temperature=temperature)

        if not self.circuit_breaker.can_execute():
            _log_event("OLLAMA_CLIENT", "Circuit breaker OPEN - stream rejected", level="ERROR",
                       extra={"model": self.model, "state": self.circuit_breaker.state.value})
            _log_model_call(self.model, "generate_stream", task_type, success=False)
            raise LLMError(f"Circuit breaker OPEN para {self.model}", model=self.model)

        effective_timeout = self._get_timeout(timeout)
        session = self._get_client(timeout=effective_timeout)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
            "options": self._opciones_coherentes(
                len(prompt) + (len(system) if system else 0),
                effective_timeout, temperature, kwargs.get("max_tokens")),
            **self._think_flag(self.model),
            # ✅ FIX: ver mismo fix en client_base.py generate() — un "30m" fijo
            # pisaba la distinción default/no-default de ModelSwap en cada
            # llamada real, dejando modelos no-default cargados más de lo pensado.
            "keep_alive": "30m" if self.model == settings.llm_default_model else "5m",
        }
        if system:
            payload["system"] = system

        try:
            with session.stream("POST", "/api/generate", json=payload,
                                timeout=effective_timeout) as response:
                response.raise_for_status()
                self.circuit_breaker.record_success()

                try:
                    for line in response.iter_lines():
                        if line:
                            data = json.loads(line)
                            if "response" in data:
                                chunk = data["response"]
                                chunks_count += 1
                                yield chunk
                            if data.get("done", False):
                                break

                except GeneratorExit:
                    # ✅ FIX v0.1.2: consumer abandonó la iteración antes de done=True.
                    # Cerramos la response explícitamente para que httpx libere la
                    # conexión al pool en lugar de dejarla abierta hasta el GC.
                    response.close()
                    _log_event("OLLAMA_CLIENT", "Stream cancelled by consumer (GeneratorExit)", extra={
                        "model": self.model, "task_type": task_type,
                        "chunks_received": chunks_count,
                    })
                    return  # Salida limpia — el finally libera la session

            elapsed = time.time() - start_time
            _log_event("OLLAMA_CLIENT", "Stream completed", extra={
                "model": self.model, "task_type": task_type,
                "chunks_count": chunks_count,
                "duration_seconds": round(elapsed, 4),
                "duration_ms": round(elapsed * 1000,2),
            })
            _log_model_call(self.model, "generate_stream", task_type,
                            output_size=chunks_count, duration=elapsed, success=True)
            if self.is_first_request:
                self.is_first_request = False

        except GeneratorExit:
            # GeneratorExit puede ocurrir también fuera del with session.stream()
            # (antes de entrar al bloque). Manejo defensivo idéntico.
            _log_event("OLLAMA_CLIENT", "Stream cancelled before connection (GeneratorExit)", extra={
                "model": self.model, "task_type": task_type,
            })
            return

        except Exception as e:
            elapsed = time.time() - start_time
            self.circuit_breaker.record_failure()
            is_timeout = isinstance(e, (
                httpx.TimeoutException, httpx.ReadTimeout,
                httpx.ConnectTimeout, httpx.WriteTimeout
            ))
            _log_error(e, "OllamaClient.generate_stream", extra={
                "model": self.model, "task_type": task_type,
                "chunks_received": chunks_count, "is_timeout": is_timeout
            })
            _log_model_call(self.model, "generate_stream", task_type,
                            output_size=chunks_count, duration=elapsed, success=False)
            if is_timeout:
                raise LLMError(
                    f"Stream timeout after {elapsed:.1f}s ({chunks_count} chunks received)",
                    model=self.model
                )
            raise LLMError(f"Streaming failed: {e}", model=self.model)

        finally:
            if session:
                self._release_client(session)

    def chat(self, messages: List[Dict[str, str]], temperature: Optional[float] = None,
             timeout: Optional[int] = None, task_type: str = "CHAT",
             use_cache: bool = False, tools: Optional[List[Dict]] = None, **kwargs) -> OllamaResponse:
        """
        Chat con historial de mensajes y soporte para Tool Calling.

        ✅ v0.2.0: Añadido parámetro tools para inyectar definiciones de herramientas
        y parseo de tool_calls en la respuesta de Ollama.
        """
        start_time = time.time()
        input_size = sum(len(m.get("content", "")) for m in messages)
        # ✅ CORREGIDO: Logging condicional a DEBUG para eliminar I/O síncrono en ruta crítica
        if logger.isEnabledFor(logging.DEBUG):
            _log_event("OLLAMA_CLIENT", f"Chat started: {task_type}", extra={
                "model": self.model, "messages_count": len(messages),
                "input_size": input_size, "temperature": temperature,
                "has_tools": tools is not None
            })
        # ✅ v0.6.8kk: `input_size` acá son CARACTERES (suma de contenidos), no
        # tokens — no etiquetarlos como "input_tokens" en el evento MODEL.
        _log_model_call(self.model, "chat", task_type, temperature=temperature)

        if not self.circuit_breaker.can_execute():
            _log_event("OLLAMA_CLIENT", "Circuit breaker OPEN - chat rejected", level="ERROR",
                       extra={"model": self.model, "state": self.circuit_breaker.state.value})
            _log_model_call(self.model, "chat", task_type, success=False)
            raise LLMError(f"Circuit breaker OPEN para {self.model}", model=self.model)

        effective_timeout = self._get_timeout(timeout)
        session = None
        try:
            session = self._get_client(timeout=effective_timeout)
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": self._opciones_coherentes(
                    input_size, effective_timeout, temperature, kwargs.get("max_tokens")),
                **self._think_flag(self.model),
                # ✅ FIX: ver mismo fix en client_base.py generate() — un "30m" fijo
                # pisaba la distinción default/no-default de ModelSwap en cada
                # llamada real, dejando modelos no-default cargados más de lo pensado.
                "keep_alive": "30m" if self.model == settings.llm_default_model else "5m",
            }

            # ✅ NUEVO v0.2.0: Inyectar tools si se proporcionan
            if tools:
                payload["tools"] = tools

            if logger.isEnabledFor(logging.DEBUG):
                _log_event("OLLAMA_CLIENT", "Sending chat request", extra={
                    "model": self.model, "messages_count": len(messages),
                    "timeout": effective_timeout
                })
            response = session.post("/api/chat", json=payload, timeout=effective_timeout)
            response.raise_for_status()
            data = response.json()
            self.circuit_breaker.record_success()

            # ✅ NUEVO v0.2.0: Extraer tool_calls y message crudo
            message = data.get("message", {})
            tool_calls = message.get("tool_calls", None)

            ollama_response = OllamaResponse(
                content=message.get("content", "") or "",
                model=data.get("model", self.model) or self.model,
                done=bool(data.get("done", False)),
                total_duration=_safe_divide(data.get("total_duration"), 1e9),
                load_duration=_safe_divide(data.get("load_duration"), 1e9),
                prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
                eval_count=int(data.get("eval_count", 0) or 0),
                eval_duration=_safe_divide(data.get("eval_duration"), 1e9),
                raw=data,
                tool_calls=tool_calls,
                message=message
            )
            # ✅ v0.7.5 (ISSUE-099): una respuesta VACÍA con `done_reason=length`
            # significa que el presupuesto se consumió sin llegar a escribir texto
            # (típico de los modelos con "thinking"). Antes salía como respuesta
            # vacía sin ninguna traza; ahora queda registrado.
            _dr = (data.get("done_reason") or "").lower()
            if not (message.get("content") or "").strip():
                if _dr == "length" or (ollama_response.eval_count or 0) > 0:
                    logger.warning(
                        f"[ISSUE-099] respuesta VACÍA del modelo {self.model} "
                        f"(done_reason={_dr or '?'}, eval_count="
                        f"{ollama_response.eval_count}, "
                        f"num_predict={payload['options'].get('num_predict')}): "
                        f"revisar presupuesto/thinking"
                    )
            elapsed = time.time() - start_time
            # ✅ v0.2.1: load_duration ya venía en la respuesta de Ollama
            # (línea de arriba, ollama_response.load_duration) pero nunca se
            # logueaba — es la medición directa de cuánto tardó Ollama en
            # cargar/cambiar el modelo en memoria, separado de la inferencia
            # real. >0 de forma consistente entre llamadas a modelos
            # distintos = confirma costo de swap; ~0 = el modelo ya estaba
            # cargado. Sin esto no había forma de distinguir "carga lenta"
            # de "inferencia lenta" en los logs.
            if ollama_response.load_duration and ollama_response.load_duration > 0.05:
                logger.info(
                    f"[OLLAMA_CLIENT] {self.model}: load_duration="
                    f"{ollama_response.load_duration:.2f}s de {elapsed:.2f}s totales "
                    f"(task_type={task_type})"
                )
                _log_event("OLLAMA_CLIENT", "Model load detected", extra={
                    "model": self.model, "task_type": task_type,
                    "load_duration_seconds": round(ollama_response.load_duration, 3),
                    "total_duration_seconds": round(elapsed, 3),
                })
            if logger.isEnabledFor(logging.DEBUG):
                _log_event("OLLAMA_CLIENT", "Chat completed successfully", extra={
                    "model": self.model, "task_type": task_type,
                    "duration_seconds": round(elapsed, 4),
                    "tokens_generated": ollama_response.eval_count,
                    "tokens_prompt": ollama_response.prompt_eval_count,
                    "tool_calls_made": len(tool_calls) if tool_calls else 0
                })
                _log_model_call(self.model, "chat", task_type,
                                input_size=ollama_response.prompt_eval_count,
                                output_size=ollama_response.eval_count,
                                duration=elapsed, success=True,
                                temperature=temperature,
                                tokens_used=ollama_response.eval_count)
            if self.is_first_request:
                self.is_first_request = False
            return ollama_response

        except Exception as e:
            elapsed = time.time() - start_time
            self.circuit_breaker.record_failure()
            _log_error(e, "OllamaClient.chat", extra={
                "model": self.model, "task_type": task_type,
                "messages_count": len(messages)
            })
            _log_model_call(self.model, "chat", task_type,
                            input_size=input_size, duration=elapsed, success=False)
            raise LLMError(f"Chat failed: {e}", model=self.model)

        finally:
            if session:
                self._release_client(session)

    def chat_stream(self, messages: List[Dict[str, str]],
                    temperature: Optional[float] = None,
                    timeout: Optional[int] = None,
                    task_type: str = "CHAT",
                    usage_sink: Optional[Dict[str, Any]] = None,
                    **kwargs) -> Generator[str, None, None]:
        """
        Chat con historial en STREAMING (v0.6.8ss, A3): igual payload que
        `chat()` pero stream=True; cede cada chunk de texto del asistente.
        SIN tools (el path de tool-calling sigue por chat() síncrono).

        `usage_sink`: diccionario opcional del llamador donde se deja el uso REAL
        del stream (`prompt_eval_count`, `eval_count`, `done_reason`) que Ollama
        envía en el último chunk. Sin esto, la telemetría de tokens se perdía en
        el camino con flujo (Fase 1, 2026-10-02).

        El consumer debe iterar en un hilo (asyncio.to_thread por chunk si
        quiere no bloquear el event loop) o interrumpir con GeneratorExit —
        la limpieza de httpx está garantizada igual que generate_stream().
        """
        start_time = time.time()
        input_size = sum(len(m.get("content", "")) for m in messages)
        _log_event("OLLAMA_CLIENT", f"Chat stream started: {task_type}", extra={
            "model": self.model, "messages_count": len(messages),
            "input_size": input_size, "temperature": temperature,
        })
        _log_model_call(self.model, "chat_stream", task_type,
                        temperature=temperature)

        if not self.circuit_breaker.can_execute():
            _log_event("OLLAMA_CLIENT", "Circuit breaker OPEN - chat stream rejected",
                       level="ERROR", extra={"model": self.model,
                                             "state": self.circuit_breaker.state.value})
            _log_model_call(self.model, "chat_stream", task_type, success=False)
            raise LLMError(f"Circuit breaker OPEN para {self.model}", model=self.model)

        effective_timeout = self._get_timeout(timeout)
        session = None
        chunks_count = 0
        full_text: List[str] = []
        max_tokens_chat_stream = kwargs.get("max_tokens")
        try:
            session = self._get_client(timeout=effective_timeout)
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": self._opciones_coherentes(
                    sum(len(m.get("content", "")) for m in messages),
                    effective_timeout, temperature, max_tokens_chat_stream),
                **self._think_flag(self.model),
                "keep_alive": "30m" if self.model == settings.llm_default_model else "5m",
            }
            with session.stream("POST", "/api/chat", json=payload,
                                timeout=effective_timeout) as response:
                response.raise_for_status()
                self.circuit_breaker.record_success()
                try:
                    for line in response.iter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        msg = data.get("message", {}) or {}
                        chunk = msg.get("content", "") or ""
                        if chunk:
                            chunks_count += 1
                            full_text.append(chunk)
                            yield chunk
                        if data.get("done", False):
                            # ✅ Fase 1 (2026-10-02): uso REAL del stream. Ollama
                            # manda `prompt_eval_count`/`eval_count` en el ÚLTIMO
                            # chunk y antes se descartaban: al encender el flujo de
                            # tokens, el chat perdía los tokens de las trazas
                            # (`tokens_prompt`/`tokens_generated` volvían a 0 — justo
                            # lo que arregló ISSUE-082). Se acumula en el diccionario
                            # del llamador: sin estado compartido en el cliente, que
                            # puede tener 2 requests en paralelo.
                            if usage_sink is not None:
                                usage_sink["prompt_eval_count"] = int(
                                    data.get("prompt_eval_count") or 0)
                                usage_sink["eval_count"] = int(
                                    data.get("eval_count") or 0)
                                usage_sink["done_reason"] = data.get("done_reason")
                            break
                except GeneratorExit:
                    logger.debug(
                        "[OLLAMA_CLIENT] chat_stream consumer abandonó — "
                        "cerrando response httpx"
                    )
                    raise
        except Exception as e:
            self.circuit_breaker.record_failure()
            _log_error(e, "OllamaClient.chat_stream", extra={
                "model": self.model, "task_type": task_type,
                "messages_count": len(messages),
            })
            _log_model_call(self.model, "chat_stream", task_type,
                            input_size=input_size, duration=time.time() - start_time,
                            success=False)
            raise LLMError(f"Chat stream failed: {e}", model=self.model)
        finally:
            elapsed = time.time() - start_time
            if logger.isEnabledFor(logging.DEBUG):
                _log_event("OLLAMA_CLIENT", "Chat stream completed", extra={
                    "model": self.model, "task_type": task_type,
                    "duration_seconds": round(elapsed, 4),
                    "chunks": chunks_count,
                    "chars": sum(len(c) for c in full_text),
                })
                _log_model_call(self.model, "chat_stream", task_type,
                                duration=elapsed, success=True,
                                temperature=temperature,
                                output_size=chunks_count)
            if session:
                self._release_client(session)

    def list_models(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Lista modelos disponibles en Ollama."""
        start_time = time.time()
        _log_event("OLLAMA_CLIENT", "List models started", extra={
            "timeout": timeout, "url": f"{self.base_url}/api/tags"
        })
        session = None
        try:
            session = self._get_client(timeout=timeout)
            response = session.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = data.get("models", [])
            elapsed = time.time() - start_time
            _log_event("OLLAMA_CLIENT", f"List models completed: {len(models)} models", extra={
                "models_count": len(models), "duration_ms": round(elapsed * 1000, 2)
            })
            return models
        except Exception as e:
            elapsed = time.time() - start_time
            _log_error(e, "OllamaClient.list_models", extra={
                "timeout": timeout, "elapsed_ms": round(elapsed * 1000, 2)
            })
            raise LLMError(f"Failed to list models: {e}", model=self.model)
        finally:
            if session:
                self._release_client(session)

    def pull_model(self, model: str, timeout: float = 600.0,
                   on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                   token: Any = None) -> None:
        """Descarga un modelo de Ollama, informando progreso y pudiendo cancelarse.

        ✅ ISSUE-148 (MEDIDO 2026-10-04, Fase 1 de contenedores): antes pedía
        `stream: false`, así que **no había progreso ni forma de cancelar** — la misma raíz
        que `ISSUE-137` en `generate()`: con `stream: false` Ollama no manda las cabeceras
        hasta que terminó, y un modelo de varios GB tarda minutos con la interfaz muda.
        Ahora el pedido va en **modo flujo**: se leen los eventos reales de Ollama, se avisa
        con `on_progress` y el token de cancelación puede cortar la descarga. El flujo se
        drena siempre (mismo tráfico), con o sin `on_progress`, para no tener dos caminos.

        Args:
            model: nombre del modelo, por ejemplo `qwen3:8b`.
            timeout: segundos máximos de la descarga.
            on_progress: se llama con cada evento ya traducido (`_evento_de_descarga`).
            token: `TokenCancelacion`; si se cancela, la descarga se corta de verdad.

        Raises:
            GeneracionCancelada: si se canceló durante la descarga (no es un error).
            LLMError: si la descarga falla de verdad.
        """
        start_time = time.time()
        _log_event("OLLAMA_CLIENT", f"Pull model started: {model}", extra={
            "model_to_pull": model, "timeout": timeout,
            "con_progreso": on_progress is not None, "cancelable": token is not None,
        })
        session = None
        try:
            session = self._get_client(timeout=timeout)
            payload = {"name": model, "stream": True}
            _log_event("OLLAMA_CLIENT", "Sending pull request (modo flujo)",
                       extra={"model": model})
            with session.stream("POST", f"{self.base_url}/api/pull",
                                json=payload, timeout=timeout) as response:
                response.raise_for_status()
                if token is not None:
                    token.registrar(response)
                try:
                    for linea in response.iter_lines():
                        if token is not None and getattr(token, "cancelado", False):
                            raise GeneracionCancelada("Descarga cancelada por el usuario")
                        if not linea:
                            continue
                        evento = json.loads(linea)
                        if evento.get("error"):
                            raise LLMError(f"Failed to pull model: {evento['error']}",
                                           model=model)
                        if on_progress is not None:
                            on_progress(_evento_de_descarga(evento, model))
                        if evento.get("status") == "success":
                            break
                finally:
                    if token is not None:
                        token.liberar(response)
            elapsed = time.time() - start_time
            _log_event("OLLAMA_CLIENT", f"Model pulled successfully: {model}", extra={
                "model": model, "duration_seconds": round(elapsed, 4)
            })
            logger.info(f"Model pulled: {model}")
        except GeneracionCancelada:
            logger.info(f"[{model}] descarga cancelada por el usuario")
            raise
        except LLMError:
            raise
        except Exception as e:
            # Una cancelación cierra la conexión: eso llega como error de red y NO es una falla.
            if token is not None and getattr(token, "cancelado", False):
                logger.info(f"[{model}] descarga cancelada por el usuario")
                raise GeneracionCancelada("Descarga cancelada por el usuario") from e
            elapsed = time.time() - start_time
            _log_error(e, "OllamaClient.pull_model", extra={
                "model": model, "timeout": timeout,
                "elapsed_seconds": round(elapsed, 2)
            })
            raise LLMError(f"Failed to pull model: {e}", model=model)
        finally:
            if session:
                self._release_client(session)

    def close(self) -> None:
        """Cierra la sesión HTTP y libera recursos del pool."""
        _log_event("OLLAMA_CLIENT", "Closing client session", extra={
            "model": self.model, "session_active": self._session is not None
        })
        if self._session:
            self._release_client(self._session)
            self._session = None
            _log_event("OLLAMA_CLIENT", "HTTP session released to pool")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del cache."""
        cache = self._get_cache()
        return cache.get_stats()

    def clear_cache(self) -> None:
        """Limpia el cache de requests."""
        cache = self._get_cache()
        cache.clear()
        _log_event("OLLAMA_CLIENT", "Request cache cleared", extra={"model": self.model})

    def __enter__(self):
        _log_event("OLLAMA_CLIENT", "Context manager enter", extra={"model": self.model})
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            _log_error(exc_val, "OllamaClient.__exit__", extra={
                "model": self.model, "exception_type": exc_type.__name__
            })
        self.close()
        _log_event("OLLAMA_CLIENT", "Context manager exit", extra={
            "model": self.model, "had_exception": exc_type is not None
        })
        return False
