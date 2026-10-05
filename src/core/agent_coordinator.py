#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — AgentCoordinator: coordinación de agentes con memoria segura
# Ruta: src/core/agent_coordinator.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 4 plan_harness — Coordinación):
#   Orquesta agentes que requieren modelos distintos garantizando:
#     1. NO OOM: cada carga de modelo pasa por MemoryGuard.can_load().
#        Si la RAM quedaría sobrecargada → la tarea se rechaza
#        (refused_oom) o espera a que se libere memoria — nunca se
#        ejecuta un modelo que sobrecargue el sistema.
#     2. El modelo cargado SIEMPRE termina su trabajo: antes de
#        swappear a otro modelo se espera (drenado) a que el modelo
#        actual tenga 0 trabajos en curso.
#     3. Dependencias cruzadas entre modelos SIN perder el hilo:
#        si un agente (modelo X) necesita el resultado de otro agente
#        (modelo Y ≠ X), se guarda la continuación (checkpoint: estado
#        + función de reanudación), se descarga X, se carga Y, se
#        ejecuta el productor, se guarda el resultado, se vuelve a
#        cargar X y se reanuda la continuación con el resultado —
#        sin perder el punto de espera (persistido en SessionEventLog).
#     4. Futuro paralelo: settings.allow_parallel_models + 
#        MemoryGuard.can_coexist() permiten que varios modelos residan
#        juntos cuando el hardware lo aguante (hoy: False → un modelo
#        por vez vía ModelSwap).
#
# Modelos de referencia: ModelSwap (arbitraje Ollama un modelo por vez),
# MemoryGuard (guard de RAM/VRAM), TaskBoard (estados de tarea).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Imports opcionales (degradación silenciosa) ─────────────────────────────

try:
    from src.core.model_swap import ModelSwap
    _MODEL_SWAP_AVAILABLE = True
except ImportError:
    _MODEL_SWAP_AVAILABLE = False

try:
    from src.core.memory_guard import get_memory_guard, MemoryGuard
    _MEMORY_GUARD_AVAILABLE = True
except ImportError:
    _MEMORY_GUARD_AVAILABLE = False

try:
    from src.core.task_board import TaskBoard, TaskStatus
    _TASK_BOARD_AVAILABLE = True
except ImportError:
    _TASK_BOARD_AVAILABLE = False

try:
    from src.learning.session_event_log import get_session_event_log, SessionEventType
    _SESSION_EVENTS_AVAILABLE = True
except ImportError:
    _SESSION_EVENTS_AVAILABLE = False

try:
    from config.settings import settings as _settings
    _SETTINGS_AVAILABLE = True
except ImportError:
    _SETTINGS_AVAILABLE = False


# ============================================================================
# MODELOS DE DATOS
# ============================================================================

@dataclass
class AcquireResult:
    """Resultado de adquirir un slot de modelo."""
    ok: bool
    model: Optional[str] = None
    swapped: bool = False
    reason: str = "ok"            # ok | drained_timeout | refused_ram | swap_failed | unknown_model
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "model": self.model, "swapped": self.swapped,
                "reason": self.reason, "details": self.details}


@dataclass
class Continuation:
    """
    Punto de espera guardado de una ejecución (checkpoint).

    Se crea cuando un agente (corriendo en `model_name`) necesita el
    resultado de una tarea que corre en OTRO modelo. El estado del
    agente se serializa en `state` y la función de reanudación se
    registra aparte (no serializable) — al reanudar se llama
    `fn(state, result)` con el modelo original de vuelta en memoria.
    """
    continuation_id: str
    model_name: str            # modelo donde debe reanudarse
    state: Dict[str, Any]      # estado serializable del punto de espera
    waiting_on: str            # task_id cuyo resultado espera
    status: str = "waiting"    # waiting | resumed | failed
    created_at: float = field(default_factory=time.time)
    resumed_at: Optional[float] = None
    result: Any = None


# ============================================================================
# AGENT COORDINATOR
# ============================================================================

class AgentCoordinator:
    """
    Coordinador de agentes multi-modelo con guard de memoria.

    Uso:
        coord = get_coordinator()
        result = coord.run_on_model("qwen2.5-coder:7b", mi_funcion, caller="coder")
        # dependencia cruzada:
        result = coord.wait_for_result(
            producer=producir_resumen, producer_model="qwen3:8b",
            consumer_state={...}, consumer_model="qwen2.5-coder:7b",
            continuation_fn=reanudar_con_resumen,
        )
    """

    def __init__(
        self,
        swap: Optional[Any] = None,
        guard: Optional[Any] = None,
        board: Optional[Any] = None,
        allow_parallel: Optional[bool] = None,
    ) -> None:
        self._swap = swap if swap is not None else (
            ModelSwap.instance() if _MODEL_SWAP_AVAILABLE else None
        )
        self._guard = guard if guard is not None else (
            get_memory_guard() if _MEMORY_GUARD_AVAILABLE else None
        )
        self._board = board if board is not None else (
            TaskBoard() if _TASK_BOARD_AVAILABLE else None
        )
        if allow_parallel is None:
            allow_parallel = bool(
                getattr(_settings, "allow_parallel_models", False)
                if _SETTINGS_AVAILABLE else False
            )
        self._allow_parallel = bool(allow_parallel)

        # ── Estado de modelos ────────────────────────────────
        self._busy: Dict[str, int] = {}        # modelo -> trabajos en curso
        self._loaded: Dict[str, float] = {}    # modelo -> required_gb residente
        self._idle_events: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()

        # ── Continuaciones ──────────────────────────────────
        self._continuations: Dict[str, Continuation] = {}
        self._continuation_fns: Dict[str, Callable[[Dict[str, Any], Any], Any]] = {}
        self._created_at = time.time()

    # ══════════════════════════════════════════════════════════
    # Slots de modelo: acquire / release con drenado + guard
    # ══════════════════════════════════════════════════════════

    def acquire_model(self, model_name: str, timeout: float = 600.0) -> AcquireResult:
        """
        Adquiere un slot de ejecución para *model_name*.

        Garantías:
          - Si el modelo ya está residente → solo marca busy.
          - Si hay OTRO modelo cargado y trabajando → espera (drenado)
            a que termine ANTES de swappear (timeout configurable).
          - MemoryGuard.can_load() → si la RAM quedaría sobrecargada,
            NO se ejecuta: AcquireResult(ok=False, reason="refused_ram").
          - Futuro paralelo: con allow_parallel_models + can_coexist()
            no se descarga el modelo previo.
        """
        if not model_name:
            return AcquireResult(ok=False, reason="unknown_model",
                                 details={"error": "model_name vacío"})

        # ── Fast path: ya residente ─────────────────────────
        with self._lock:
            if model_name in self._loaded:
                self._mark_busy_locked(model_name)
                return AcquireResult(ok=True, model=model_name, swapped=False)

        current = self._swap.current_model if self._swap is not None else None

        if self._allow_parallel:
            # ── Modo paralelo (futuro con más VRAM): coexistir ──
            if self._guard is not None:
                ok, reason, details = self._guard.can_coexist(model_name, self._loaded)
                if not ok:
                    return AcquireResult(ok=False, reason=reason, details=details)
        else:
            # ── Drenado: el modelo actual debe terminar su trabajo ──
            if current is not None and current != model_name:
                if not self._wait_model_idle(current, timeout):
                    return AcquireResult(
                        ok=False, reason="drained_timeout",
                        details={"current_model": current,
                                 "error": f"el modelo {current} sigue ocupado tras {timeout:.0f}s"},
                    )
            # ── Guard de memoria ────────────────────────────
            if self._guard is not None:
                ok, reason, details = self._guard.can_load(model_name)
                if not ok:
                    return AcquireResult(ok=False, reason=reason, details=details)

        # ── Swap (un modelo por vez) ────────────────────────
        swapped = False
        if self._swap is not None:
            try:
                swapped = bool(self._swap.ensure(model_name))
            except Exception as exc:
                logger.error(f"[Coordinator] swap falló para {model_name}: {exc}")
                return AcquireResult(
                    ok=False, reason="swap_failed",
                    details={"error": str(exc)},
                )

        with self._lock:
            req = self._guard.required_gb(model_name) if self._guard is not None else 0.0
            self._loaded[model_name] = req
            self._mark_busy_locked(model_name)
        logger.info(f"[Coordinator] modelo adquirido: {model_name} "
                    f"(swapped={swapped}, busy={self._busy.get(model_name)})")
        return AcquireResult(ok=True, model=model_name, swapped=swapped)

    def release_model(self, model_name: str) -> None:
        """Libera el slot: decrementa busy; en 0 → evento idle (drenado listo)."""
        with self._lock:
            n = self._busy.get(model_name, 0)
            if n <= 1:
                self._busy.pop(model_name, None)
                ev = self._idle_events.pop(model_name, None)
                if ev is not None:
                    ev.set()
            else:
                self._busy[model_name] = n - 1

    # ══════════════════════════════════════════════════════════
    # Ejecución de agentes sobre un modelo
    # ══════════════════════════════════════════════════════════

    def run_on_model(
        self,
        model_name: str,
        fn: Callable[..., Any],
        caller: str = "coordinator",
        timeout: float = 600.0,
        task_id: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Ejecuta `fn(model_name, *args, **kwargs)` con el modelo cargado.

        - Adquiere el slot (drenado + guard). Si es rechazado por RAM,
          la tarea queda REFUSED_OOM en el TaskBoard.
        - fn recibe el nombre del modelo como primer argumento.
        - SIEMPRE libera el slot al final (finally).
        """
        if self._board is not None and task_id is None:
            task_id = self._board.add(caller, model_name)

        res = self.acquire_model(model_name, timeout=timeout)
        if not res.ok:
            if self._board is not None and task_id is not None:
                self._board.update(
                    task_id,
                    status=TaskStatus.REFUSED_OOM if res.reason == "refused_ram"
                    else TaskStatus.FAILED,
                    error=res.reason,
                    **{"acquire_details": res.details},
                )
            logger.warning(
                f"[Coordinator] {caller}: no se ejecutó {model_name} — {res.reason}"
            )
            return {"success": False, "reason": res.reason, "details": res.details}

        if self._board is not None and task_id is not None:
            self._board.update(task_id, status=TaskStatus.RUNNING)

        try:
            result = fn(model_name, *args, **kwargs)
            if self._board is not None and task_id is not None:
                self._board.store_result(task_id, result)
            return {"success": True, "model": model_name, "result": result,
                    "task_id": task_id}
        except Exception as exc:
            logger.error(f"[Coordinator] {caller} falló en {model_name}: {exc}")
            if self._board is not None and task_id is not None:
                self._board.update(task_id, status=TaskStatus.FAILED, error=str(exc))
            return {"success": False, "model": model_name, "error": str(exc),
                    "task_id": task_id}
        finally:
            self.release_model(model_name)

    # ══════════════════════════════════════════════════════════
    # Dependencias cruzadas: checkpoint / resume
    # ══════════════════════════════════════════════════════════

    def checkpoint(
        self,
        model_name: str,
        state: Dict[str, Any],
        waiting_on: str,
        continuation_fn: Callable[[Dict[str, Any], Any], Any],
        continuation_id: Optional[str] = None,
    ) -> str:
        """
        Guarda el punto de espera de una ejecución (checkpoint).

        El modelo actual termina su trabajo actual; su continuación
        (estado + función) queda registrada para reanudar cuando el
        resultado de `waiting_on` esté listo. Se persiste un evento
        CONTINUATION_SAVED en el SessionEventLog (no se pierde el hilo).
        """
        cid = continuation_id or f"cont_{uuid.uuid4().hex[:10]}"
        cont = Continuation(
            continuation_id=cid,
            model_name=model_name,
            state=dict(state or {}),
            waiting_on=waiting_on,
        )
        with self._lock:
            self._continuations[cid] = cont
            self._continuation_fns[cid] = continuation_fn
        self._log_session_event(
            SessionEventType.SYSTEM,
            {"event": "continuation_saved", "continuation_id": cid,
             "model": model_name, "waiting_on": waiting_on},
        )
        logger.info(
            f"[Coordinator] checkpoint guardado: {cid} "
            f"(model={model_name}, waiting_on={waiting_on})"
        )
        return cid

    def resume_continuation(
        self,
        continuation_id: str,
        result: Any,
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Reanuda una continuación: recarga el modelo original, inyecta el
        resultado y llama a la función de reanudación con (state, result).
        """
        with self._lock:
            cont = self._continuations.get(continuation_id)
            fn = self._continuation_fns.get(continuation_id)
        if cont is None or fn is None:
            return {"success": False, "error": f"continuación no encontrada: {continuation_id}"}
        if cont.status != "waiting":
            return {"success": False, "error": f"continuación ya {cont.status}"}

        # Recargar el modelo de la continuación (drenado + guard)
        res = self.acquire_model(cont.model_name, timeout=timeout)
        if not res.ok:
            cont.status = "failed"
            return {"success": False, "reason": res.reason, "details": res.details}

        try:
            output = fn(dict(cont.state), result)
            cont.status = "resumed"
            cont.result = result
            cont.resumed_at = time.time()
            self._log_session_event(
                SessionEventType.SYSTEM,
                {"event": "continuation_resumed", "continuation_id": continuation_id,
                 "model": cont.model_name},
            )
            return {"success": True, "continuation_id": continuation_id,
                    "result": output}
        except Exception as exc:
            cont.status = "failed"
            logger.error(f"[Coordinator] resume {continuation_id} falló: {exc}")
            return {"success": False, "error": str(exc)}
        finally:
            self.release_model(cont.model_name)

    # ══════════════════════════════════════════════════════════
    # Flujo de dependencia cruzada completo
    # ══════════════════════════════════════════════════════════

    def wait_for_result(
        self,
        producer: Callable[..., Any],
        producer_model: str,
        consumer_state: Dict[str, Any],
        consumer_model: str,
        continuation_fn: Callable[[Dict[str, Any], Any], Any],
        producer_args: Optional[tuple] = None,
        producer_kwargs: Optional[Dict[str, Any]] = None,
        caller: str = "cross_model",
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        El consumidor (corriendo en `consumer_model`) necesita el resultado
        de `producer` (que corre en `producer_model` ≠ consumer_model).

        Protocolo (sin perder el hilo):
          1. checkpoint: guardar consumer_state + continuation_fn.
          2. Ejecutar el productor en su modelo (drenado + guard + swap).
          3. Guardar el resultado en el TaskBoard.
          4. resume_continuation: recargar consumer_model y llamar
             continuation_fn(consumer_state, resultado).

        Nota: el consumidor debe haber liberado su slot ANTES de llamar
        a este método (el checkpoint ya capturó su estado).
        """
        if producer_model == consumer_model:
            # Mismo modelo: sin swap, ejecución directa del productor.
            result = producer(*(producer_args or ()), **(producer_kwargs or {}))
            return continuation_fn(dict(consumer_state), result)

        cid = self.checkpoint(
            model_name=consumer_model,
            state=consumer_state,
            waiting_on=f"producer:{caller}",
            continuation_fn=continuation_fn,
        )
        # Registrar la tarea productora en el tablero
        producer_task_id = None
        if self._board is not None:
            producer_task_id = self._board.add(
                f"producer:{caller}", producer_model,
            )

        prod = self.run_on_model(
            producer_model,
            lambda m, *a, **k: producer(m, *a, **k),
            caller=f"producer:{caller}",
            timeout=timeout,
            task_id=producer_task_id,
            * (producer_args or ()),
            **(producer_kwargs or {}),
        )
        if not prod.get("success"):
            logger.error(
                f"[Coordinator] productor falló ({producer_model}): {prod.get('error')}"
            )
            return prod

        return self.resume_continuation(cid, prod["result"], timeout=timeout)

    # ══════════════════════════════════════════════════════════
    # Cadena de tareas con dependencias
    # ══════════════════════════════════════════════════════════

    def run_chain(
        self,
        steps: List[Dict[str, Any]],
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Ejecuta pasos respetando dependencias y modelos.

        steps: [{"name": str, "model": str, "fn": callable,
                 "depends_on": [str]}]

        - Se ejecutan en orden topológico (los que tienen todas las
          dependencias resueltas primero).
        - Cada paso corre con run_on_model (drenado + guard por modelo).
        - El resultado de cada paso queda en el TaskBoard y se inyecta
          en los pasos dependientes como `deps` (dict name → result).
        """
        results: Dict[str, Any] = {}
        task_ids: Dict[str, str] = {}
        remaining = list(steps)

        # Registrar todas las tareas
        for step in steps:
            if self._board is not None:
                tid = self._board.add(
                    step["name"], step.get("model", ""),
                    depends_on=step.get("depends_on", []),
                )
                task_ids[step["name"]] = tid

        guard_count = 0
        while remaining and guard_count <= len(steps) * 2 + 2:
            guard_count += 1
            progressed = False
            for step in list(remaining):
                deps = step.get("depends_on", [])
                if any(d not in results for d in deps):
                    continue  # dependencias sin resolver
                remaining.remove(step)
                progressed = True

                def _run_with_deps(m, _fn=step["fn"], _dep_names=list(deps),
                                   _results=results):
                    return _fn(m, deps={d: _results.get(d) for d in _dep_names})

                out = self.run_on_model(
                    step.get("model", ""), _run_with_deps,
                    caller=step["name"], timeout=timeout,
                    task_id=task_ids.get(step["name"]),
                )
                if out.get("success"):
                    results[step["name"]] = out["result"]
                else:
                    results[step["name"]] = {"error": out.get("error") or out.get("reason")}

            if not progressed and remaining:
                blocked = [s["name"] for s in remaining]
                logger.warning(f"[Coordinator] cadena bloqueada en: {blocked}")
                return {"success": False, "results": results,
                        "blocked": blocked}

        return {"success": len(remaining) == 0, "results": results,
                "unfinished": [s["name"] for s in remaining]}

    # ══════════════════════════════════════════════════════════
    # Internos
    # ══════════════════════════════════════════════════════════

    def _mark_busy_locked(self, model_name: str) -> None:
        self._busy[model_name] = self._busy.get(model_name, 0) + 1

    def _wait_model_idle(self, model_name: str, timeout: float) -> bool:
        """Espera a que el modelo no tenga trabajos en curso (drenado)."""
        with self._lock:
            if self._busy.get(model_name, 0) == 0:
                return True
            ev = self._idle_events.get(model_name)
            if ev is None:
                ev = threading.Event()
                self._idle_events[model_name] = ev
        logger.info(
            f"[Coordinator] esperando a que {model_name} termine "
            f"({self._busy.get(model_name)} trabajos en curso)..."
        )
        return ev.wait(timeout=timeout)

    def _log_session_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not _SESSION_EVENTS_AVAILABLE:
            return
        try:
            log = get_session_event_log()
            if log is not None:
                log.append("coordinator", event_type, payload)
        except Exception as e:
            logger.debug(f"[Coordinator] session event error: {e}")

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "allow_parallel_models": self._allow_parallel,
                "loaded_models": dict(self._loaded),
                "busy": dict(self._busy),
                "continuations": {
                    cid: {"status": c.status, "model": c.model_name,
                          "waiting_on": c.waiting_on}
                    for cid, c in self._continuations.items()
                },
                "board": self._board.get_stats() if self._board is not None else None,
                "uptime_s": round(time.time() - self._created_at, 1),
            }


# ============================================================================
# SINGLETON — get_coordinator()
# ============================================================================

_instance: Optional[AgentCoordinator] = None
_lock = threading.Lock()


def get_coordinator() -> AgentCoordinator:
    """Singleton del AgentCoordinator (thread-safe, lazy)."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = AgentCoordinator()
        return _instance
