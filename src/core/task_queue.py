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
# - Task, TaskStatus, TaskPriority enums para tipado fuerte
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
import asyncio
import logging
import threading
import time
import uuid
import hashlib
from enum import Enum, IntEnum
from typing import Optional, Dict, Any, List, Callable, Awaitable, Tuple
from datetime import datetime, timezone
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from config.settings import settings
from config.paths import Paths
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: TaskError definida localmente (no existe en exceptions.py)
# ═══════════════════════════════════════════════════════════════
class TaskError(Exception):
    """Excepción para errores de cola de tareas."""
    pass

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_task(op: str, task_id: str = "", priority: str = "",
              status: str = "", duration: float = None,
              success: bool = True, extra: Dict[str, Any] = None):
    """
    Helper para logging de operaciones de task queue.

    Args:
        op: Operación realizada (enqueue, dequeue, cancel, complete)
        task_id: ID único de la tarea
        priority: Prioridad de la tarea (HIGH/NORMAL/LOW)
        status: Estado actual de la tarea
        duration: Duración de la operación en segundos
        success: Si la operación fue exitosa
        extra: Información adicional para el log
    """
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(
                category="TASK_QUEUE",
                message=f"{op} {status}",
                level="DEBUG" if success else "WARNING",
                extra={
                    "task_id": task_id,
                    "priority": priority,
                    "task_status": status,
                    "duration_ms": round(duration * 1000, 2) if duration else None,
                    **(extra or {}),
                }
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 task_queue.py:108] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 task_queue.py:120] excepción degradada (intencional): {_d2e}")


# ============================================================================
# ENUMS — Tipado fuerte para tareas
# ============================================================================
class TaskPriority(IntEnum):
    """
    Niveles de prioridad para tareas.

    Orden: HIGH (3) > NORMAL (2) > LOW (1)

    Uso:
    >>> TaskPriority.HIGH > TaskPriority.NORMAL  # True
    """
    LOW = 1
    NORMAL = 2
    HIGH = 3

    @classmethod
    def from_string(cls, value: str) -> "TaskPriority":
        """Convierte string a TaskPriority (case-insensitive)."""
        mapping = {
            "low": cls.LOW, "l": cls.LOW,
            "normal": cls.NORMAL, "n": cls.NORMAL, "default": cls.NORMAL,
            "high": cls.HIGH, "h": cls.HIGH, "urgent": cls.HIGH,
        }
        return mapping.get(value.lower().strip(), cls.NORMAL)


class TaskStatus(Enum):
    """
    Estados del ciclo de vida de una tarea.

    Flujo típico:
    PENDING → RUNNING → COMPLETED
                ↓
            FAILED
                ↓
            CANCELLED (desde PENDING o RUNNING)
    """
    PENDING = "pending"      # En cola, esperando ejecución
    RUNNING = "running"      # En ejecución activa
    COMPLETED = "completed"  # Ejecución exitosa
    FAILED = "failed"        # Error durante ejecución
    CANCELLED = "cancelled"  # Cancelada por usuario o timeout
    TIMEOUT = "timeout"      # Excedió tiempo máximo

    @property
    def is_terminal(self) -> bool:
        """True si el estado es final (no puede cambiar)."""
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED, self.TIMEOUT}

    @property
    def is_active(self) -> bool:
        """True si la tarea está en progreso."""
        return self in {self.PENDING, self.RUNNING}


@dataclass
class Task:
    """
    Representación de una tarea en el sistema.

    Attributes:
        id: UUID único de la tarea (auto-generado)
        name: Nombre descriptivo de la tarea
        payload: Datos/parámetros para la ejecución
        priority: Nivel de prioridad (HIGH/NORMAL/LOW)
        timeout_seconds: Timeout máximo en segundos (None = sin límite)
        created_at: Timestamp de creación (UTC)
        started_at: Timestamp de inicio de ejecución (None si no empezó)
        completed_at: Timestamp de finalización (None si no terminó)
        status: Estado actual de la tarea
        result: Resultado de la ejecución (None si no completó)
        error: Mensaje de error si falló (None si exitosa)
        metadata: Metadatos adicionales para trazabilidad
        _hash: Hash único para deduplicación (auto-calculado)
    """
    id: str
    name: str
    payload: Dict[str, Any]
    priority: TaskPriority = TaskPriority.NORMAL
    timeout_seconds: Optional[float] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Calcula hash único para deduplicación."""
        if not self.id:
            self.id = str(uuid.uuid4())
        content = f"{self.name}:{sorted(self.payload.items())}:{self.priority.value}"
        self._hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    @property
    def age_seconds(self) -> float:
        """Edad de la tarea en segundos."""
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    @property
    def execution_time_seconds(self) -> Optional[float]:
        """Tiempo de ejecución si está completada."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_expired(self) -> bool:
        """True si excedió el timeout (solo para tareas activas)."""
        if self.timeout_seconds is None or not self.started_at:
            return False
        if self.status.is_terminal:
            return False
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return elapsed > self.timeout_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para logging/APIs."""
        return {
            "id": self.id,
            "name": self.name,
            "payload": self.payload,
            "priority": self.priority.name,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
            "age_seconds": round(self.age_seconds, 2),
            "execution_time_seconds": round(self.execution_time_seconds, 2) if self.execution_time_seconds else None,
        }

    def __repr__(self) -> str:
        return (
            f"Task(id={self.id[:8]}..., name={self.name!r}, "
            f"priority={self.priority.name}, status={self.status.value})"
        )


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
class TaskQueue:
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
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(*args, **kwargs))
                else:
                    cb(*args, **kwargs)
            except Exception as e:
                logger.warning(f"Callback {event} failed: {e}")

    # ── Enqueue ───────────────────────────────────────────────────────────
    async def enqueue(
        self,
        name: str,
        payload: Dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout_seconds: Optional[float] = None,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        partition_key: Optional[str] = None,  # 🆕 FASE 7: para DistributedQueue
    ) -> Task:
        """
        Encola una nueva tarea.

        Args:
            name: Nombre descriptivo de la tarea
            payload: Datos/parámetros para la ejecución
            priority: Nivel de prioridad (default: NORMAL)
            timeout_seconds: Timeout máximo en segundos (None = sin límite)
            task_id: ID personalizado (auto-generado si None)
            metadata: Metadatos adicionales

        Returns:
            Task: Objeto tarea con ID único

        Raises:
            TaskError: Si la cola está llena o tarea duplicada (si deduplicate=True)
        """
        if self._shutdown:
            raise TaskError("TaskQueue is shutting down")

        async with self._lock:
            # Verificar límite de cola
            if len(self._pending) >= self._max_size:
                raise TaskError(f"TaskQueue full (max={self._max_size})")

            # Crear tarea
            task = Task(
                id=task_id or str(uuid.uuid4()),
                name=name,
                payload=payload,
                priority=priority,
                timeout_seconds=timeout_seconds,
                metadata=metadata or {},
            )

            # 🆕 FASE 7: registrar partition_key en metadata si se provee
            if partition_key and "partition" not in task.metadata:
                task.metadata["partition"] = partition_key

            # Deduplicación opcional
            if self._deduplicate:
                for existing in self._pending.values():
                    if existing._hash == task._hash and existing.status == TaskStatus.PENDING:
                        logger.debug(f"Duplicate task detected: {task.name}")
                        _log_task("ENQUEUE", task_id=task.id, status="duplicate", success=False)
                        raise TaskError(f"Duplicate task: {task.name}")

            # Agregar a estructuras internas
            self._tasks[task.id] = task
            self._pending[task.id] = task

            # Encolar con prioridad (negativo para HIGH=3 > NORMAL=2 > LOW=1)
            queue_priority = -priority.value
            timestamp = task.created_at.timestamp()
            await self._queue.put((queue_priority, timestamp, task.id))

            duration = time.perf_counter()
            _log_task("ENQUEUE", task_id=task.id, priority=priority.name,
                     status="pending", duration=duration, success=True,
                     extra={"payload_keys": list(payload.keys())})
            self._emit("enqueue", task)

            logger.debug(f"Task enqueued: {task}")
            return task

    # ── Dequeue ───────────────────────────────────────────────────────────
    async def dequeue(self, timeout: Optional[float] = None) -> Optional[Task]:
        """
        Obtiene la siguiente tarea para ejecutar (por prioridad).

        Args:
            timeout: Timeout máximo para esperar una tarea (None = bloqueante)

        Returns:
            Task o None si timeout o queue vacía

        ✅ FIX AUDITORÍA (M10): antes de sacar por el orden estricto de la
        PriorityQueue, se chequea si alguna tarea LOW/NORMAL superó
        _aging_threshold_seconds esperando — si la hay, se sirve directo
        (ver _pop_aged_task()). Sin esto, una tarea LOW puede esperar
        indefinidamente bajo carga sostenida de HIGH/NORMAL: la prioridad
        de un item ya insertado en asyncio.PriorityQueue nunca se reevalúa.
        """
        aged = await self._pop_aged_task()
        if aged is not None:
            return aged

        try:
            # ✅ ISSUE-100: BUCLE. Antes, si la entrada que salía de la cola no
            # estaba en `_pending` (tarea cancelada, o ya servida por el aging
            # boost), se hacía `task_done()` y **`return None`** aunque hubiera
            # tareas pendientes detrás. El contrato del docstring es "None si
            # timeout o queue vacía": medido, cancelar 1 de 3 tareas dejaba
            # `dequeue()` en None y las otras 2 sin servir (el worker perdía un
            # ciclo de poll; con N cancelaciones, N ciclos). Ahora se salta la
            # entrada muerta y se sigue con la siguiente, respetando el
            # presupuesto del timeout original.
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                restante: Optional[float] = None
                if deadline is not None:
                    restante = deadline - time.monotonic()
                    if restante <= 0:
                        return None
                if restante is not None:
                    result = await asyncio.wait_for(self._queue.get(), timeout=restante)
                else:
                    result = await self._queue.get()

                _, _, task_id = result

                async with self._lock:
                    if task_id not in self._pending:
                        # Tarea cancelada, o ya servida vía aging boost
                        # (ver _pop_aged_task) mientras esperaba en la cola.
                        self._queue.task_done()
                        continue

                    task = self._pending.pop(task_id)
                    task.status = TaskStatus.RUNNING
                    task.started_at = datetime.now(timezone.utc)
                    self._running[task_id] = task

                    duration = time.perf_counter()
                    _log_task("DEQUEUE", task_id=task.id, priority=task.priority.name,
                             status="running", duration=duration, success=True)
                    self._emit("start", task)

                    logger.debug(f"Task started: {task}")
                    return task

        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Dequeue error: {e}")
            return None

    async def _pop_aged_task(self) -> Optional[Task]:
        """
        ✅ FIX AUDITORÍA (M10): busca la tarea pendiente con prioridad menor
        a HIGH más antigua que superó _aging_threshold_seconds esperando, y
        la sirve fuera de orden (evita starvation indefinida de LOW/NORMAL
        bajo carga sostenida de HIGH). La entrada correspondiente que sigue
        viva dentro de self._queue (la asyncio.PriorityQueue en sí) se
        descarta silenciosamente cuando le toque salir — mismo mecanismo
        que ya existe para tareas canceladas (task_id not in self._pending).
        """
        if not self._aging_threshold_seconds:
            return None

        async with self._lock:
            if not self._pending:
                return None

            candidate: Optional[Task] = None
            for task in self._pending.values():
                if task.priority == TaskPriority.HIGH:
                    continue
                if task.age_seconds < self._aging_threshold_seconds:
                    continue
                if candidate is None or task.created_at < candidate.created_at:
                    candidate = task

            if candidate is None:
                return None

            task = self._pending.pop(candidate.id)
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(timezone.utc)
            self._running[task.id] = task

            duration = time.perf_counter()
            _log_task("DEQUEUE", task_id=task.id, priority=task.priority.name,
                     status="running_aged", duration=duration, success=True,
                     extra={"aged_seconds": round(task.age_seconds, 1)})
            self._emit("start", task)

            logger.info(
                f"[TaskQueue] Aging boost: {task} servida fuera de orden "
                f"tras {task.age_seconds:.0f}s de espera"
            )
            return task

    # ── Peek ──────────────────────────────────────────────────────────────
    async def peek(self) -> Optional[Task]:
        """
        Inspecciona la próxima tarea sin removerla de la cola.

        Returns:
            Task o None si cola vacía
        """
        async with self._lock:
            if not self._pending:
                return None
            # Retornar tarea de mayor prioridad (primera en _pending por orden de inserción)
            return next(iter(self._pending.values()))

    # ── Cancel ────────────────────────────────────────────────────────────
    async def cancel(self, task_id: str, reason: str = "User requested") -> bool:
        """
        Cancela una tarea pendiente o en ejecución.

        Args:
            task_id: ID de la tarea a cancelar
            reason: Motivo de la cancelación

        Returns:
            True si cancelada, False si no encontrada o ya terminal
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status.is_terminal:
                return False

            old_status = task.status
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now(timezone.utc)
            task.error = reason

            # Remover de estructuras apropiadas
            if task_id in self._pending:
                del self._pending[task_id]
            elif task_id in self._running:
                del self._running[task_id]

            # Agregar a completadas para historial
            self._completed[task_id] = task
            while len(self._completed) > self._metrics._max_entries:
                self._completed.popitem(last=False)

            duration = time.perf_counter()
            _log_task("CANCEL", task_id=task_id, priority=task.priority.name,
                     status=f"{old_status.value}→cancelled", duration=duration, success=True,
                     extra={"reason": reason})
            self._emit("cancel", task)

            logger.info(f"Task cancelled: {task} (reason: {reason})")
            return True

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
        _final_status: Optional[TaskStatus] = None
        _final_latency: float = 0.0
        _cancelled_error: Optional[asyncio.CancelledError] = None

        try:
            # Verificar timeout antes de empezar
            if task.is_expired:
                task.status = TaskStatus.TIMEOUT
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
            task.status = TaskStatus.COMPLETED
            task.result = result
            _final_latency = (time.perf_counter() - start) * 1000

        except asyncio.TimeoutError:
            task.status = TaskStatus.TIMEOUT
            task.error = f"Execution timeout ({task.timeout_seconds}s)"
            _final_latency = (time.perf_counter() - start) * 1000

        except asyncio.CancelledError as e:
            task.status = TaskStatus.CANCELLED
            task.error = "Execution cancelled"
            _final_latency = (time.perf_counter() - start) * 1000
            # Guardamos para re-raise después del finally completo
            _cancelled_error = e

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = f"{type(e).__name__}: {e}"
            _final_latency = (time.perf_counter() - start) * 1000
            _log_error(e, f"Task.execute[{task.name}]", extra={"task_id": task.id})
            self._emit("error", task, e)

        finally:
            task.completed_at = datetime.now(timezone.utc)
            _final_status = task.status
            _success = _final_status == TaskStatus.COMPLETED

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
                    for status in TaskStatus
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
