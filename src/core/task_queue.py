#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.3 — Task Queue: Cola Asíncrona con Prioridades
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/task_queue.py
# Autor: SaintWick
# Fecha: 2026-04-05
# Propósito: Cola de tareas asíncrona thread-safe con prioridades,
#            timeout configurable, cancelación segura y métricas
# Versión: 0.6.3
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO v0.6.0:
# - TaskError definida localmente (no existe en src.domain.exceptions)
# - Type hints corregidos: metadata en lugar de meta (reserved word)
# - Eliminados imports incorrectos de excepciones no existentes
# ✅ NUEVO v0.6.0:
# - Task, QueueTaskStatus, TaskPriority enums para tipado fuerte
# - TaskQueue con asyncio.Queue + asyncio.Lock para concurrencia
# - Prioridades: HIGH > NORMAL > LOW (LRU dentro de misma prioridad)
# - Timeout configurable por tarea con cancelación automática
# - Cancelación segura de tareas pendientes (no en ejecución)
# - Métricas en tiempo real: throughput, latency p95, error rate
# - Integración con DetailedLogger para trazabilidad completa
# - Thread-safe y async-safe operations
# ✅ FIX v0.6.1:
# - _queue_lock: asyncio.Lock() a nivel módulo → threading.Lock() (crash Python 3.10+)
# ✅ FIX v0.6.2 — CRÍTICO:
# - execute(): eliminado deadlock por asyncio.Lock anidado.
#   _metrics.record() y el bloque de cleanup se ejecutaban ambos dentro de
#   "async with self._lock", pero _metrics.record() también adquiere self._lock
#   internamente → deadlock garantizado en cualquier path de error/cancelación.
#   SOLUCIÓN: separar cleanup (con lock) de metrics.record() (sin lock externo).
# ✅ v0.6.3 — FIX AUDITORÍA (M10): aging de prioridad. TaskPriority era
#   estático al encolar — una tarea LOW podía esperar indefinidamente bajo
#   carga sostenida de HIGH/NORMAL, porque asyncio.PriorityQueue nunca
#   reevalúa la prioridad de un item ya insertado. dequeue() ahora chequea
#   primero _pop_aged_task(): si hay una tarea LOW/NORMAL pendiente hace
#   más de aging_threshold_seconds (default 30s, configurable en __init__),
#   se sirve fuera de orden. La entrada vieja que queda en la PriorityQueue
#   se descarta cuando le toca salir — mismo mecanismo ya usado para
#   tareas canceladas.
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# ✅ 2026-10-22 (refactorización 50/50): el MODELO DE DATOS y las operaciones
#    de PRIORIZACIÓN (`TaskError`, `TaskPriority`, `QueueTaskStatus`, `Task`,
#    `enqueue`, `dequeue`, `_pop_aged_task`, `peek`, `cancel`) viven ahora en
#    el hermano `task_queue_priority.py`, como
#    `TaskQueuePriorityMixin`. Acá QUEDAN la ejecución, el worker, la
#    observabilidad/ciclo de vida y el SINGLETON de la cola.
#    `TaskQueue` hereda del mixin y este módulo RE-IMPORTA los nombres
#    movidos con los MISMOS nombres ⇒ la API pública NO cambia. TaskMetrics
#    (métricas bounded) se queda acá, junto a `__init__`/`get_stats`.
#    Dependencia en UN SOLO SENTIDO (este módulo → hermano): sin AUD-CIR-001.
# ═══════════════════════════════════════════════════════════════
import asyncio
import inspect
import logging
import threading
import time
from typing import Optional, Dict, Any, List, Callable, Awaitable
from datetime import datetime, timezone
from collections import OrderedDict

# ── Re-exports de la mitad movida (mismos objetos ⇒ API pública intacta) ──
from .task_queue_priority import (  # noqa: F401 — re-export: API pública intacta
    TaskError,
    TaskPriority,
    QueueTaskStatus,
    Task,
    TaskQueuePriorityMixin,
    _log_task,
    _log_error,
)

logger = logging.getLogger(__name__)


# ============================================================================
# MÉTRICAS — Bounded para evitar memory leaks
# ============================================================================
class TaskMetrics:
    """
    Métricas de ejecución con límite de memoria.

    Features:
    - Thread-safe con asyncio.Lock PROPIO (no comparte lock con TaskQueue)
    - Bounded: elimina entradas antiguas cuando excede max_entries
    - Cálculo de percentiles (p50, p95, p99) para latency
    - Throughput y error rate en tiempo real
    """

    __slots__ = ['_data', '_lock', '_max_entries']

    def __init__(self, max_entries: int = 1000):
        """
        Inicializa métricas.

        Args:
            max_entries: Máximo de entradas a mantener (default: 1000)
        """
        self._data: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        # ✅ FIX v0.6.2: Lock PROPIO — nunca comparte lock con TaskQueue.
        # Esto previene el deadlock donde execute() adquiría self._lock (de TaskQueue)
        # y luego llamaba a _metrics.record() que internamente también adquiría
        # self._lock (mismo objeto) → asyncio.Lock no es reentrant → deadlock.
        self._lock = asyncio.Lock()
        self._max_entries = max_entries

    async def record(self, task: Task, success: bool, latency_ms: float) -> None:
        """
        Registra métricas de una tarea completada.

        Args:
            task: Tarea completada
            success: Si la ejecución fue exitosa
            latency_ms: Latencia en milisegundos

        Note:
            Usa su propio lock (_metrics._lock), independiente del lock
            de TaskQueue. Puede llamarse con o sin el lock de TaskQueue
            adquirido sin riesgo de deadlock.
        """
        async with self._lock:
            # Evict si excede límite
            while len(self._data) >= self._max_entries:
                self._data.popitem(last=False)

            self._data[task.id] = {
                "name": task.name,
                "priority": task.priority.name,
                "success": success,
                "latency_ms": latency_ms,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timestamp_ts": time.time(),  # float epoch — evita parsing en get_stats()
            }

    async def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas agregadas.

        Returns:
            Dict con throughput, latency percentiles, error rate, etc.
        """
        async with self._lock:
            if not self._data:
                return {
                    "total_tasks": 0,
                    "success_rate": 0.0,
                    "avg_latency_ms": 0.0,
                    "p50_latency_ms": 0.0,
                    "p95_latency_ms": 0.0,
                    "p99_latency_ms": 0.0,
                    "throughput_per_minute": 0.0,
                }

            latencies = [e["latency_ms"] for e in self._data.values()]
            successes = sum(1 for e in self._data.values() if e["success"])
            total = len(self._data)

            latencies_sorted = sorted(latencies)

            def percentile(data: List[float], p: float) -> float:
                if not data:
                    return 0.0
                k = (len(data) - 1) * p / 100
                f = int(k)
                c = f + 1 if f + 1 < len(data) else f
                return data[f] + (k - f) * (data[c] - data[f]) if c != f else data[f]

            # Throughput: tareas por minuto (últimos 5 min aprox)
            _now_ts = time.time()
            recent = [e for e in self._data.values()
                     if (_now_ts - e.get("timestamp_ts", 0)) < 300]
            throughput = len(recent) / 5.0 if recent else 0.0

            # by_priority: single pass O(n) en lugar de O(n × prioridades)
            _by_priority: Dict[str, Dict[str, Any]] = {
                p.name: {"count": 0, "successes": 0} for p in TaskPriority
            }
            for e in self._data.values():
                p_name = e["priority"]
                if p_name in _by_priority:
                    _by_priority[p_name]["count"] += 1
                    if e["success"]:
                        _by_priority[p_name]["successes"] += 1
            by_priority_stats = {
                p_name: {
                    "count": d["count"],
                    "success_rate": round(d["successes"] / max(d["count"], 1), 4),
                }
                for p_name, d in _by_priority.items()
            }

            return {
                "total_tasks": total,
                "success_rate": round(successes / max(total, 1), 4),
                "avg_latency_ms": round(sum(latencies) / max(total, 1), 2),
                "p50_latency_ms": round(percentile(latencies_sorted, 50), 2),
                "p95_latency_ms": round(percentile(latencies_sorted, 95), 2),
                "p99_latency_ms": round(percentile(latencies_sorted, 99), 2),
                "throughput_per_minute": round(throughput, 2),
                "by_priority": by_priority_stats,
            }

    async def clear(self) -> None:
        """Limpia todas las métricas."""
        async with self._lock:
            self._data.clear()


# ============================================================================
# TASK QUEUE — Cola asíncrona con prioridades
# ============================================================================
class TaskQueue(TaskQueuePriorityMixin):
    """
    Cola de tareas asíncrona thread-safe con soporte para prioridades.

    Features:
    - asyncio.Queue + asyncio.Lock para concurrencia segura
    - Prioridades: HIGH > NORMAL > LOW (LRU dentro de misma prioridad)
    - Timeout configurable por tarea con cancelación automática
    - Cancelación segura de tareas pendientes (no en ejecución)
    - Deduplicación opcional por hash de payload
    - Métricas en tiempo real integradas
    - Callbacks para eventos: on_enqueue, on_start, on_complete, on_error
    - Integración con DetailedLogger para trazabilidad

    Example:
    >>> queue = TaskQueue(max_size=1000, deduplicate=True)
    >>> task = await queue.enqueue("process_data", {"file": "x.csv"}, priority=TaskPriority.HIGH)
    >>> result = await queue.execute(task, handler_fn)
    >>> stats = await queue.get_stats()
    """

    __slots__ = [
        '_queue', '_lock', '_tasks', '_pending', '_running', '_completed',
        '_max_size', '_deduplicate', '_metrics', '_callbacks', '_shutdown',
        '_aging_threshold_seconds',
    ]

    def __init__(
        self,
        max_size: int = 1000,
        deduplicate: bool = False,
        metrics_max_entries: int = 1000,
        aging_threshold_seconds: Optional[float] = 30.0,
    ):
        """
        Inicializa cola de tareas.

        Args:
            max_size: Máximo de tareas en cola (default: 1000)
            deduplicate: Si True, evita tareas duplicadas por hash (default: False)
            metrics_max_entries: Máximo de entradas en métricas (default: 1000)
            aging_threshold_seconds: ✅ FIX AUDITORÍA (M10) — si una tarea
                LOW/NORMAL lleva pendiente más de este tiempo, se sirve fuera
                de orden en el próximo dequeue() en vez de esperar a que la
                PriorityQueue la saque por su cuenta (lo que nunca pasa bajo
                carga sostenida de HIGH, porque la prioridad queda fija al
                encolar). None desactiva el aging. Default: 30s.
        """
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._lock = asyncio.Lock()
        self._tasks: Dict[str, Task] = {}
        self._pending: Dict[str, Task] = {}
        self._running: Dict[str, Task] = {}
        self._completed: OrderedDict[str, Task] = OrderedDict()
        self._max_size = max_size
        self._deduplicate = deduplicate
        # ✅ FIX v0.6.2: TaskMetrics tiene su PROPIO lock interno.
        # No comparte self._lock con TaskQueue → sin deadlock posible.
        self._metrics = TaskMetrics(max_entries=metrics_max_entries)
        self._callbacks: Dict[str, List[Callable]] = {
            "enqueue": [], "start": [], "complete": [], "error": [], "cancel": [],
        }
        self._shutdown = False
        self._aging_threshold_seconds = aging_threshold_seconds

        logger.info(f"TaskQueue initialized: max_size={max_size}, deduplicate={deduplicate}")
        _log_task("INIT", status="ready", success=True)

    # ── Callbacks ─────────────────────────────────────────────────────────
    def on_enqueue(self, callback: Callable[[Task], Any]) -> None:
        """Registra callback para cuando una tarea es encolada."""
        self._callbacks["enqueue"].append(callback)

    def on_start(self, callback: Callable[[Task], Any]) -> None:
        """Registra callback para cuando una tarea inicia ejecución."""
        self._callbacks["start"].append(callback)

    def on_complete(self, callback: Callable[[Task, Any], Any]) -> None:
        """Registra callback para cuando una tarea completa exitosamente."""
        self._callbacks["complete"].append(callback)

    def on_error(self, callback: Callable[[Task, Exception], Any]) -> None:
        """Registra callback para cuando una tarea falla."""
        self._callbacks["error"].append(callback)

    def on_cancel(self, callback: Callable[[Task], Any]) -> None:
        """Registra callback para cuando una tarea es cancelada."""
        self._callbacks["cancel"].append(callback)

    def _emit(self, event: str, *args, **kwargs) -> None:
        """Emite evento a todos los callbacks registrados."""
        for cb in self._callbacks.get(event, []):
            try:
                if inspect.iscoroutinefunction(cb):
                    asyncio.create_task(cb(*args, **kwargs))
                else:
                    cb(*args, **kwargs)
            except Exception as e:
                logger.warning(f"Callback {event} failed: {e}")

    # ── Execute ───────────────────────────────────────────────────────────
    async def execute(
        self,
        task: Task,
        handler: Callable[[Task], Awaitable[Any]],
    ) -> Task:
        """
        Ejecuta una tarea con el handler proporcionado.

        Args:
            task: Tarea a ejecutar
            handler: Función async que procesa la tarea y retorna resultado

        Returns:
            Task actualizada con resultado o error

        Note (v0.6.2):
            El cleanup de estructuras internas (self._lock) y el registro de
            métricas (self._metrics._lock) usan locks DISTINTOS y se ejecutan
            en bloques separados dentro del finally. Esto elimina el deadlock
            que ocurría cuando ambas operaciones compartían el mismo lock.
        """
        start = time.perf_counter()
        # Variables locales para el finally — evitan acceder a task.status
        # después de que el lock fue liberado y otro coroutine pudo modificarla.
        _final_status: Optional[QueueTaskStatus] = None
        _final_latency: float = 0.0
        _cancelled_error: Optional[asyncio.CancelledError] = None

        try:
            # Verificar timeout antes de empezar
            if task.is_expired:
                task.status = QueueTaskStatus.TIMEOUT
                task.error = f"Task expired before execution (timeout={task.timeout_seconds}s)"
                task.completed_at = datetime.now(timezone.utc)
                _final_status = task.status
                _final_latency = (time.perf_counter() - start) * 1000
                return task

            # Ejecutar handler con timeout si corresponde
            if task.timeout_seconds:
                result = await asyncio.wait_for(
                    handler(task),
                    timeout=task.timeout_seconds
                )
            else:
                result = await handler(task)

            # Éxito
            task.status = QueueTaskStatus.COMPLETED
            task.result = result
            _final_latency = (time.perf_counter() - start) * 1000

        except asyncio.TimeoutError:
            task.status = QueueTaskStatus.TIMEOUT
            task.error = f"Execution timeout ({task.timeout_seconds}s)"
            _final_latency = (time.perf_counter() - start) * 1000

        except asyncio.CancelledError as e:
            task.status = QueueTaskStatus.CANCELLED
            task.error = "Execution cancelled"
            _final_latency = (time.perf_counter() - start) * 1000
            # Guardamos para re-raise después del finally completo
            _cancelled_error = e

        except Exception as e:
            task.status = QueueTaskStatus.FAILED
            task.error = f"{type(e).__name__}: {e}"
            _final_latency = (time.perf_counter() - start) * 1000
            _log_error(e, f"Task.execute[{task.name}]", extra={"task_id": task.id})
            self._emit("error", task, e)

        finally:
            task.completed_at = datetime.now(timezone.utc)
            _final_status = task.status
            _success = _final_status == QueueTaskStatus.COMPLETED

            # ── Bloque 1: cleanup de estructuras (con self._lock) ──────────
            # NUNCA llamar a _metrics.record() aquí — usa su propio lock
            # y hacerlo dentro de self._lock causaba deadlock (v0.6.1 bug).
            async with self._lock:
                if task.id in self._running:
                    del self._running[task.id]
                self._completed[task.id] = task
                while len(self._completed) > self._metrics._max_entries:
                    self._completed.popitem(last=False)

            # ── Bloque 2: métricas (con self._metrics._lock, SIN self._lock) ──
            # Separado del bloque anterior para evitar lock anidado.
            await self._metrics.record(task, _success, _final_latency)

            # ── Bloque 3: logging y callbacks (sin locks) ──────────────────
            duration_for_log = time.perf_counter() - start
            _log_task("COMPLETE", task_id=task.id, priority=task.priority.name,
                     status=_final_status.value if _final_status else "unknown",
                     duration=duration_for_log, success=_success,
                     extra={"latency_ms": round(_final_latency, 2)})
            self._emit("complete", task, task.result if _success else None)

        logger.debug(f"Task completed: {task} (latency={_final_latency:.2f}ms)")

        # Re-raise CancelledError DESPUÉS de que el finally completó limpiamente
        if _cancelled_error is not None:
            raise _cancelled_error

        return task

    # ── Worker Loop ───────────────────────────────────────────────────────
    async def worker_loop(
        self,
        handler: Callable[[Task], Awaitable[Any]],
        poll_interval: float = 0.1,
        max_concurrent: int = 10,
    ) -> None:
        """
        Loop de worker que procesa tareas de la cola.

        Args:
            handler: Función async que procesa cada tarea
            poll_interval: Segundos entre polls si cola vacía
            max_concurrent: Máximo de tareas concurrentes (default: 10)
        """
        sem = asyncio.Semaphore(max_concurrent)

        async def process_task(task: Task):
            async with sem:
                await self.execute(task, handler)

        while not self._shutdown:
            task = await self.dequeue(timeout=poll_interval)
            if task:
                asyncio.create_task(process_task(task))
            elif not self._pending and not self._running:
                # Cola vacía y nada en ejecución — esperar un poco
                await asyncio.sleep(poll_interval)

    # ── Stats & Info ──────────────────────────────────────────────────────
    async def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas completas de la cola.

        Returns:
            Dict con métricas de cola y ejecución
        """
        async with self._lock:
            pending_count = len(self._pending)
            running_count = len(self._running)
            completed_count = len(self._completed)
            tasks_snapshot = dict(self._tasks)

        # _metrics.get_stats() usa su propio lock — seguro llamar fuera de self._lock
        metrics = await self._metrics.get_stats()

        return {
            "queue": {
                "pending": pending_count,
                "running": running_count,
                "completed_total": completed_count,
                "max_size": self._max_size,
                "utilization": round((pending_count + running_count) / max(self._max_size, 1), 4),
            },
            "tasks": {
                "by_status": {
                    status.value: sum(1 for t in tasks_snapshot.values() if t.status == status)
                    for status in QueueTaskStatus
                },
                "by_priority": {
                    p.name: sum(1 for t in tasks_snapshot.values() if t.priority == p)
                    for p in TaskPriority
                },
            },
            "metrics": metrics,
            "shutdown": self._shutdown,
        }

    def get_task(self, task_id: str) -> Optional[Task]:
        """Obtiene tarea por ID (thread-safe para lectura)."""
        return self._tasks.get(task_id)

    def list_pending(self, limit: int = 50) -> List[Task]:
        """Lista tareas pendientes ordenadas por prioridad."""
        return sorted(
            list(self._pending.values()),
            key=lambda t: (-t.priority.value, t.created_at),
        )[:limit]

    def list_running(self) -> List[Task]:
        """Lista tareas en ejecución."""
        return list(self._running.values())

    # ── Cleanup & Shutdown ────────────────────────────────────────────────
    async def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """
        Limpia tareas completadas antiguas para liberar memoria.

        Args:
            max_age_seconds: Edad máxima para mantener en historial

        Returns:
            Número de tareas eliminadas
        """
        async with self._lock:
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
            to_remove = [
                tid for tid, task in self._completed.items()
                if task.completed_at and task.completed_at.timestamp() < cutoff
            ]
            for tid in to_remove:
                del self._completed[tid]
                if tid in self._tasks:
                    del self._tasks[tid]

            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} old completed tasks")

            return len(to_remove)

    async def shutdown(self, wait_for_pending: bool = True, timeout: float = 30.0) -> None:
        """
        Apaga la cola de tareas gracefulmente.

        Args:
            wait_for_pending: Si True, espera a que tareas pendientes completen
            timeout: Timeout máximo para esperar (segundos)
        """
        logger.info(f"TaskQueue shutdown initiated: wait={wait_for_pending}, timeout={timeout}s")
        self._shutdown = True

        if wait_for_pending:
            start = time.time()
            while (self._pending or self._running) and (time.time() - start) < timeout:
                await asyncio.sleep(0.1)

            # Cancelar lo que quede después del timeout
            for task_id in list(self._pending.keys()) + list(self._running.keys()):
                await self.cancel(task_id, reason="Queue shutdown")

        # Limpiar estructuras
        async with self._lock:
            self._pending.clear()
            self._running.clear()
            self._tasks.clear()

        logger.info("TaskQueue shutdown complete")
        _log_task("SHUTDOWN", status="complete", success=True)

    # ── Context Manager ───────────────────────────────────────────────────
    async def __aenter__(self) -> "TaskQueue":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.shutdown(wait_for_pending=True)


# ============================================================================
# SINGLETON GLOBAL
# ============================================================================
_queue_instance: Optional[TaskQueue] = None
# ✅ FIX v0.6.1: asyncio.Lock() a nivel módulo → RuntimeError en Python 3.10+ al importar
# threading.Lock() es seguro en cualquier contexto; __init__ de TaskQueue no es async.
_queue_lock = threading.Lock()


async def get_task_queue(
    max_size: int = 1000,
    deduplicate: bool = False,
    metrics_max_entries: int = 1000,
) -> TaskQueue:
    """
    Obtiene instancia singleton del TaskQueue.

    Args:
        max_size: Máximo de tareas en cola
        deduplicate: Si evitar tareas duplicadas
        metrics_max_entries: Máximo de entradas en métricas

    Returns:
        Instancia de TaskQueue
    """
    global _queue_instance

    if _queue_instance is None:
        with _queue_lock:  # threading.Lock — no necesita await
            if _queue_instance is None:
                _queue_instance = TaskQueue(
                    max_size=max_size,
                    deduplicate=deduplicate,
                    metrics_max_entries=metrics_max_entries,
                )

    return _queue_instance


async def reset_task_queue() -> None:
    """Reinicia el singleton del TaskQueue."""
    global _queue_instance

    if _queue_instance:
        await _queue_instance.shutdown(wait_for_pending=False)
        _queue_instance = None


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CORRECCIONES
# ═══════════════════════════════════════════════════════════════
# | Versión | Línea  | Cambio                                      | Razón                              |
# |---------|--------|---------------------------------------------|------------------------------------|
# | v0.6.0  | ~35    | ✅ TaskError definida localmente            | No existe en exceptions.py         |
# | v0.6.0  | ~36    | ✅ Eliminado import de TaskError            | Evita ImportError                  |
# | v0.6.0  | ~220   | ✅ metadata en lugar de meta               | meta es reserved word              |
# | v0.6.1  | ~861   | ✅ _queue_lock: asyncio.Lock() → threading  | Crash Python 3.10+ en import       |
# | v0.6.1  | ~27    | ✅ import threading agregado               | Requerido por threading.Lock       |
# | v0.6.2  | TaskMetrics | ✅ Lock PROPIO en TaskMetrics         | Previene deadlock con TaskQueue    |
# | v0.6.2  | execute()   | ✅ Cleanup y metrics en bloques separados | asyncio.Lock no es reentrant       |
# | v0.6.2  | execute()   | ✅ CancelledError re-raise post-finally   | Limpieza completa antes de propagar|
# | v0.6.2  | get_stats() | ✅ metrics.get_stats() fuera de self._lock| Evita lock anidado en lectura      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
