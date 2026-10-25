#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — task_queue_priority.py  (REFACTORIZACIÓN 50/50)
# Ruta: /ruta/a/axioma/src/core/task_queue_priority.py
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de MODELO DE DATOS y
#    PRIORIZACIÓN se extrae de `src/core/task_queue.py` (1077 líneas) a este
#    hermano, para dejarlo por debajo de las 1000 (regla del proyecto: cada
#    archivo >1000 líneas se parte en DOS: el original conserva una mitad y
#    este hermano recibe la otra).
#
#    Qué se movió, SIN cambios de comportamiento:
#      · El MODELO de tareas: `TaskError`, `TaskPriority`, `QueueTaskStatus`
#        y `Task`. Las MÉTRICAS (`TaskMetrics`) quedan en el original, junto a
#        `get_stats`/`__init__` que son sus consumidores naturales.
#      · Los helpers de logging compartidos por las dos mitades: `_log_task`,
#        `_log_error`, `_LOGGER_AVAILABLE` y `get_logger` (el original los
#        re-importa con los MISMOS nombres ⇒ un solo objeto, sin duplicar).
#      · Las operaciones de ADMISIÓN/PRIORIZACIÓN de la cola, como
#        `TaskQueuePriorityMixin`: `enqueue`, `dequeue`, `_pop_aged_task`
#        (aging M10), `peek` y `cancel`. `TaskQueue` (en el original) hereda
#        de este mixin, así que los métodos siguen disponibles en cualquier
#        instancia: la API pública NO cambia.
#
#    Qué QUEDA en el original: `TaskMetrics`, `__init__`/`__slots__`, los
#    callbacks (`on_*`, `_emit`), la EJECUCIÓN (`execute`, `worker_loop`), la
#    OBSERVABILIDAD/ciclo de vida (`get_stats`, `get_task`, `list_*`,
#    `cleanup_completed`, `shutdown`, context manager) y el SINGLETON
#    (`_queue_instance`, `_queue_lock`, `get_task_queue`,
#    `reset_task_queue`).
#
#    Estado mutable compartido: NINGUNO. El singleton de la cola vive SOLO en
#    el original y este hermano no lo toca ni lo duplica. Dependencia en UN
#    SOLO SENTIDO (task_queue → este módulo): el hermano es HOJA y NO importa
#    el original a nivel de módulo (sin AUD-CIR-001). Los tests importan y
#    parchean únicamente nombres del original; no hay globals de este módulo
#    parcheados por tests (barrido `setattr(` sobre tests/ incluido).
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import time
import uuid
import hashlib
from enum import Enum, IntEnum
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from dataclasses import dataclass, field

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

logger = logging.getLogger("src.core.task_queue")  # nombre histórico (mismo objeto que el original)


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


# ⚠️ NO unificar con el `BoardTaskStatus` de `task_board.py`: ésta es la cola de trabajo GENÉRICA y sus
# estados son otros (`PENDING`, `COMPLETED`, `CANCELLED`, `TIMEOUT`). Ver el comentario del tablero
# (medido el 2026-10-09: son máquinas de estado distintas, no una duplicada).
class QueueTaskStatus(Enum):
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
    status: QueueTaskStatus = QueueTaskStatus.PENDING
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


class TaskQueuePriorityMixin:
    """Mitad de MODELO y PRIORIZACIÓN de TaskQueue (refactor 50/50).

    `TaskQueue` (en `task_queue.py`) hereda de este mixin: agrupa las
    operaciones de admisión y servicio por prioridad (`enqueue`, `dequeue`,
    `_pop_aged_task`, `peek`, `cancel`), con el aging M10 y los locks
    internos que ya tenía la clase. Los contratos `Task`/`TaskPriority`/
    `QueueTaskStatus`/`TaskError` se definen acá y el original los re-importa
    (dependencia en un solo sentido, sin ciclo). `TaskMetrics` se queda en el
    original junto a `__init__`/`get_stats`.

    Hoja del grafo: no importa `src.core.task_queue` a nivel de módulo.
    """

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
                    if existing._hash == task._hash and existing.status == QueueTaskStatus.PENDING:
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
                    task.status = QueueTaskStatus.RUNNING
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
            task.status = QueueTaskStatus.RUNNING
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
            task.status = QueueTaskStatus.CANCELLED
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
