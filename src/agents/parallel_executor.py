#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# parallel_executor.py - Ejecutor Concurrente de Agentes
# Ruta: /ruta/a/axioma/src/agents/parallel_executor.py
# Autor: SaintWick
# Versión: 0.6.2
#
# 📌 ARCHIVO RESERVADO PARA USO FUTURO — MODELOS EN PARALELO (GPU)
# ───────────────────────────────────────────────────────────────
# ⚠️ NO ELIMINAR. Está deliberadamente SIN CABLEAR hoy y eso es por diseño,
#    no código muerto (decisión documentada en docs/ISSUES_HISTORY.md,
#    ISSUE-036):
#
#    · QUÉ APORTA: ejecución concurrente de agentes con `asyncio.Semaphore`,
#      timeouts, reintentos con backoff exponencial + jitter, integración con
#      `TaskQueue` por prioridad, métricas (throughput, p95/p99, error rate),
#      callbacks de eventos y `shutdown` graceful.
#
#    · POR QUÉ NO ESTÁ CABLEADO HOY: el hardware actual es **CPU-only con
#      ~14 GB marginales** (~4-6 tok/s). Correr varios agentes LLM a la vez NO
#      acelera: duplica modelos en RAM y degrada ambos caminos. Por eso el
#      sistema serializa a propósito vía `MemoryGuard`, `vram_lock`,
#      `settings.llm_concurrency` (max_concurrent=2) y
#      `AgentCoordinator(allow_parallel=False)`.
#
#    · CUÁNDO CABLEARLO: al incorporar **GPU** (VRAM suficiente para más de un
#      modelo residente). En ese momento este módulo ya está listo:
#        `ParallelExecutor(max_concurrent=N)` + `execute_batch()`.
#      Al hacerlo: medir antes/después y respetar el gating por memoria
#      (`MemoryGuard`), igual que el resto del sistema.
#
#    · QUÉ NO DUPLICA: sus reintentos/backoff son equivalentes a
#      `BaseAgent.execute_with_retry_async()` y sus métricas a las de
#      `task_queue`/`self_healing`; se conserva porque el camino paralelo
#      necesita su propio control de concurrencia y telemetría agregada.
# ───────────────────────────────────────────────────────────────
# Ejecuta agentes en paralelo con control de concurrencia (Semaphore),
# timeouts, reintentos con backoff exponencial, integración con
# TaskQueue por prioridad y métricas en tiempo real (throughput,
# latencia p95/p99, tasa de error). Incluye callbacks para eventos
# y graceful shutdown.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import threading
import time
from typing import Optional, Dict, Any, List, Callable, Awaitable
from src.core.task_queue import TaskQueue, Task, TaskPriority, get_task_queue

# ✅ v0.6.9v (REFACTOR 50/50): modelos y métricas (RetryConfig,
# ExecutionResult, ExecutionMetrics) y helpers de logging movidos a
# src/agents/parallel_executor_models.py. Se re-exportan para no romper
# la API pública del módulo.
from .parallel_executor_models import (  # noqa: E402,F401
    RetryConfig,
    ExecutionResult,
    ExecutionMetrics,
    _log_executor,
    _log_error,
)

logger = logging.getLogger(__name__)


# ============================================================================
# CLASE PRINCIPAL: ParallelExecutor
# ============================================================================
class ParallelExecutor:
    """
    Ejecutor paralelo de agentes con control de concurrencia.

    Features:
    - Ejecución concurrente con asyncio.Semaphore
    - Integración con TaskQueue para encolado por prioridad
    - Timeout configurable por tarea con cancelación automática
    - Retry con backoff exponencial configurable
    - Métricas en tiempo real: throughput, latency p95/p99, error rate
    - Callbacks para eventos: on_submit, on_start, on_complete, on_error
    - Integración con DetailedLogger para trazabilidad completa
    - Graceful shutdown con wait_for_pending + timeout

    Example:
    >>> executor = ParallelExecutor(max_concurrent=10)
    >>> result = await executor.execute(
    ...     agent_type="coder",
    ...     handler=my_coder_handler,
    ...     payload={"query": "crea una función"},
    ...     priority=TaskPriority.HIGH,
    ... )
    >>> stats = await executor.get_stats()
    """

    __slots__ = [
        '_task_queue', '_semaphore', '_metrics', '_retry_config',
        '_callbacks', '_running_tasks', '_shutdown', '_max_concurrent'
    ]

    def __init__(
        self,
        max_concurrent: int = 10,
        task_queue: Optional[TaskQueue] = None,
        retry_config: Optional[RetryConfig] = None,
        metrics_max_entries: int = 1000,
    ):
        """
        Inicializa ejecutor paralelo.

        Args:
            max_concurrent: Máximo de tareas concurrentes (default: 10)
            task_queue: TaskQueue para encolado (crea nueva si None)
            retry_config: Configuración de retry (default: RetryConfig())
            metrics_max_entries: Máximo de entradas en métricas (default: 1000)
        """
        start = time.perf_counter()

        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._task_queue = task_queue or TaskQueue(max_size=1000)
        self._metrics = ExecutionMetrics(max_entries=metrics_max_entries)
        self._retry_config = retry_config or RetryConfig()
        self._callbacks: Dict[str, List[Callable]] = {
            "submit": [], "start": [], "complete": [], "error": [], "cancel": [],
        }
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown = False

        duration = time.perf_counter() - start
        logger.info(
            f"ParallelExecutor initialized: max_concurrent={max_concurrent}, "
            f"retry={self._retry_config.max_attempts} attempts"
        )
        _log_executor("INIT", status="ready", duration=duration, success=True)

    # ── Callbacks ─────────────────────────────────────────────────────────
    def on_submit(self, callback: Callable[[str, str, Dict[str, Any]], Any]) -> None:
        """Registra callback para cuando una tarea es enviada."""
        self._callbacks["submit"].append(callback)

    def on_start(self, callback: Callable[[str, str], Any]) -> None:
        """Registra callback para cuando una tarea inicia ejecución."""
        self._callbacks["start"].append(callback)

    def on_complete(self, callback: Callable[[str, ExecutionResult], Any]) -> None:
        """Registra callback para cuando una tarea completa exitosamente."""
        self._callbacks["complete"].append(callback)

    def on_error(self, callback: Callable[[str, Exception], Any]) -> None:
        """Registra callback para cuando una tarea falla."""
        self._callbacks["error"].append(callback)

    def on_cancel(self, callback: Callable[[str], Any]) -> None:
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

    # ── Ejecución principal ───────────────────────────────────────────────
    async def execute(
        self,
        agent_type: str,
        handler: Callable[[Dict[str, Any]], Awaitable[Any]],
        payload: Dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout_seconds: Optional[float] = None,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """
        Ejecuta un agente con control de concurrencia y retry.

        Args:
            agent_type: Tipo de agente (coder, researcher, analyst, etc.)
            handler: Función async que ejecuta el agente (recibe payload)
            payload: Datos/parámetros para la ejecución del agente
            priority: Nivel de prioridad (default: NORMAL)
            timeout_seconds: Timeout máximo en segundos (None = sin límite)
            task_id: ID personalizado (auto-generado si None)
            metadata: Metadatos adicionales

        Returns:
            ExecutionResult con resultado o error

        Note (v0.6.2):
            _execute_with_retry se envuelve en asyncio.create_task() y se
            registra en self._running_tasks ANTES del await. Esto garantiza
            que cancel() y shutdown() puedan encontrar y cancelar la tarea,
            y que active_count refleje el estado real del executor.
        """
        if self._shutdown:
            return ExecutionResult.fail(
                agent_type=agent_type,
                task_id=task_id or "unknown",
                error="ParallelExecutor is shutting down",
            )

        start = time.perf_counter()
        actual_task_id = task_id or f"{agent_type}_{int(time.time() * 1000)}"

        # Emitir evento de submit
        self._emit("submit", actual_task_id, agent_type, payload)
        _log_executor("SUBMIT", agent_type=agent_type, task_id=actual_task_id,
                     status="pending", success=True, extra={"priority": priority.name})

        # Encolar en TaskQueue
        await self._task_queue.enqueue(
            name=agent_type,
            payload=payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            task_id=actual_task_id,
            metadata=metadata or {},
        )

        # Ejecutar con semaphore para control de concurrencia
        async with self._semaphore:
            # Emitir evento de start
            self._emit("start", actual_task_id, agent_type)
            _log_executor("START", agent_type=agent_type, task_id=actual_task_id,
                         status="running", success=True)

            # ✅ FIX v0.6.2: envolver en asyncio.Task y registrar en _running_tasks
            # ANTES del await para que cancel() y shutdown() puedan encontrarlo.
            # El try/finally garantiza limpieza aunque ocurra CancelledError.
            asyncio_task = asyncio.create_task(
                self._execute_with_retry(
                    agent_type=agent_type,
                    task_id=actual_task_id,
                    handler=handler,
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                )
            )
            self._running_tasks[actual_task_id] = asyncio_task

            try:
                result = await asyncio_task
            except asyncio.CancelledError:
                result = ExecutionResult.fail(
                    agent_type=agent_type,
                    task_id=actual_task_id,
                    error="Execution cancelled",
                )
            finally:
                # Limpiar siempre — independientemente del resultado
                self._running_tasks.pop(actual_task_id, None)

        # Registrar métricas
        duration = (time.perf_counter() - start) * 1000
        result.latency_ms = duration
        await self._metrics.record(result, priority)

        # Emitir evento de complete/error
        if result.success:
            self._emit("complete", actual_task_id, result)
            _log_executor("COMPLETE", agent_type=agent_type, task_id=actual_task_id,
                         status="completed", duration=duration / 1000, success=True,
                         extra={"attempts": result.attempts})
        else:
            self._emit("error", actual_task_id, Exception(result.error or "Unknown error"))
            _log_executor("ERROR", agent_type=agent_type, task_id=actual_task_id,
                         status="failed", duration=duration / 1000, success=False,
                         extra={"error": result.error, "attempts": result.attempts})

        logger.debug(f"Execution completed: {result}")
        return result

    async def _execute_with_retry(
        self,
        agent_type: str,
        task_id: str,
        handler: Callable[[Dict[str, Any]], Awaitable[Any]],
        payload: Dict[str, Any],
        timeout_seconds: Optional[float] = None,
    ) -> ExecutionResult:
        """
        Ejecuta handler con retry y timeout.

        Args:
            agent_type: Tipo de agente
            task_id: ID de la tarea
            handler: Función async del agente
            payload: Datos para el handler
            timeout_seconds: Timeout por intento

        Returns:
            ExecutionResult con resultado o error
        """
        last_error: Optional[Exception] = None

        for attempt in range(self._retry_config.max_attempts):
            try:
                # Ejecutar con timeout si corresponde
                if timeout_seconds:
                    result = await asyncio.wait_for(
                        handler(payload),
                        timeout=timeout_seconds
                    )
                else:
                    result = await handler(payload)

                # Éxito
                return ExecutionResult.ok(
                    agent_type=agent_type,
                    task_id=task_id,
                    output=str(result) if result is not None else "",
                    data=result if isinstance(result, dict) else None,
                    attempts=attempt + 1,
                )

            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning(
                    f"Task {task_id} timeout (attempt {attempt + 1}/{self._retry_config.max_attempts})"
                )

            except asyncio.CancelledError:
                # CancelledError no se reintenta — propagación limpia
                return ExecutionResult.fail(
                    agent_type=agent_type,
                    task_id=task_id,
                    error="Execution cancelled",
                    attempts=attempt + 1,
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    f"Task {task_id} failed (attempt {attempt + 1}): {type(e).__name__}: {e}"
                )

            # Si no es el último intento, esperar con backoff
            if attempt < self._retry_config.max_attempts - 1:
                delay = self._retry_config.get_delay(attempt)
                logger.debug(f"Retry {attempt + 1} for task {task_id} in {delay:.2f}s")
                await asyncio.sleep(delay)

        # Todos los intentos fallaron
        error_msg = f"{type(last_error).__name__}: {last_error}" if last_error else "Unknown error"
        return ExecutionResult.fail(
            agent_type=agent_type,
            task_id=task_id,
            error=error_msg,
            attempts=self._retry_config.max_attempts,
        )

    # ── Ejecución en batch ────────────────────────────────────────────────
    async def execute_batch(
        self,
        tasks: List[Dict[str, Any]],
        parallel: bool = True,
    ) -> List[ExecutionResult]:
        """
        Ejecuta múltiples agentes en batch.

        Args:
            tasks: Lista de dicts con configuración de cada tarea:
                {
                    "agent_type": "coder",
                    "handler": my_handler,
                    "payload": {...},
                    "priority": TaskPriority.HIGH,  # opcional
                    "timeout_seconds": 30.0,  # opcional
                    "task_id": "custom_id",  # opcional
                }
            parallel: Si True, ejecuta en paralelo; si False, en secuencia

        Returns:
            Lista de ExecutionResult en el mismo orden que tasks
        """
        if not tasks:
            return []

        if parallel:
            # Ejecutar en paralelo con asyncio.gather
            coroutines = [
                self.execute(
                    agent_type=t["agent_type"],
                    handler=t["handler"],
                    payload=t["payload"],
                    priority=t.get("priority", TaskPriority.NORMAL),
                    timeout_seconds=t.get("timeout_seconds"),
                    task_id=t.get("task_id"),
                    metadata=t.get("metadata"),
                )
                for t in tasks
            ]
            _gathered = await asyncio.gather(*coroutines, return_exceptions=True)
            return [
                r if isinstance(r, ExecutionResult)
                else ExecutionResult.fail(agent_type="batch", task_id="unknown", error=str(r))
                for r in _gathered
            ]
        else:
            # Ejecutar en secuencia
            results = []
            for t in tasks:
                result = await self.execute(
                    agent_type=t["agent_type"],
                    handler=t["handler"],
                    payload=t["payload"],
                    priority=t.get("priority", TaskPriority.NORMAL),
                    timeout_seconds=t.get("timeout_seconds"),
                    task_id=t.get("task_id"),
                    metadata=t.get("metadata"),
                )
                results.append(result)
            return results

    # ── Cancelación ───────────────────────────────────────────────────────
    async def cancel(self, task_id: str, reason: str = "User requested") -> bool:
        """
        Cancela una tarea en ejecución o pendiente.

        Args:
            task_id: ID de la tarea a cancelar
            reason: Motivo de la cancelación

        Returns:
            True si cancelada, False si no encontrada o ya terminal

        Note (v0.6.2):
            _running_tasks ahora se popula correctamente en execute(),
            por lo que este método puede encontrar y cancelar tareas activas.
        """
        # Intentar cancelar en TaskQueue (pendientes)
        queue_cancelled = await self._task_queue.cancel(task_id, reason=reason)

        # Si está en _running_tasks (en ejecución activa), cancelar el asyncio.Task
        if task_id in self._running_tasks:
            task = self._running_tasks[task_id]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 parallel_executor.py:735] excepción degradada (intencional): {_d2e}")
                # execute() limpia _running_tasks en su propio finally
                # pero por si acaso lo eliminamos también aquí
                self._running_tasks.pop(task_id, None)
                self._emit("cancel", task_id)
                _log_executor("CANCEL", task_id=task_id, status="cancelled", success=True)
                logger.info(f"Task cancelled: {task_id} (reason: {reason})")
                return True

        return queue_cancelled

    # ── Stats & Info ──────────────────────────────────────────────────────
    async def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas completas del ejecutor.

        Returns:
            Dict con métricas de ejecución y estado del executor
        """
        queue_stats = await self._task_queue.get_stats()
        metrics_stats = await self._metrics.get_stats()

        return {
            "executor": {
                "max_concurrent": self._max_concurrent,
                "active_tasks": len(self._running_tasks),
                "shutdown": self._shutdown,
            },
            "queue": queue_stats,
            "metrics": metrics_stats,
        }

    def get_task(self, task_id: str) -> Optional[Task]:
        """Obtiene tarea por ID desde la TaskQueue."""
        return self._task_queue.get_task(task_id)

    @property
    def active_count(self) -> int:
        """Número de tareas actualmente en ejecución (para LoadBalancer)."""
        return len(self._running_tasks)

    # ── Cleanup & Shutdown ────────────────────────────────────────────────
    async def shutdown(self, wait_for_pending: bool = True, timeout: float = 30.0) -> None:
        """
        Apaga el ejecutor gracefulmente.

        Args:
            wait_for_pending: Si True, espera a que tareas pendientes completen
            timeout: Timeout máximo para esperar (segundos)

        Note (v0.6.2):
            Con _running_tasks correctamente poblado, shutdown() puede
            cancelar todas las tareas activas al expirar el timeout.
        """
        logger.info(f"ParallelExecutor shutdown initiated: wait={wait_for_pending}, timeout={timeout}s")
        self._shutdown = True

        if wait_for_pending:
            start = time.time()
            # Esperar a que tasks en ejecución completen
            while self._running_tasks and (time.time() - start) < timeout:
                # Limpiar tareas ya terminadas
                done_ids = [tid for tid, t in self._running_tasks.items() if t.done()]
                for tid in done_ids:
                    del self._running_tasks[tid]
                if self._running_tasks:
                    await asyncio.sleep(0.1)

            # Cancelar lo que quede después del timeout global
            for task_id in list(self._running_tasks.keys()):
                await self.cancel(task_id, reason="Executor shutdown")

        # Apagar TaskQueue
        await self._task_queue.shutdown(wait_for_pending=wait_for_pending, timeout=timeout)

        logger.info("ParallelExecutor shutdown complete")
        _log_executor("SHUTDOWN", status="complete", success=True)

    # ── Context Manager ───────────────────────────────────────────────────
    async def __aenter__(self) -> "ParallelExecutor":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.shutdown(wait_for_pending=True)


# ============================================================================
# SINGLETON GLOBAL
# ============================================================================
_executor_instance: Optional[ParallelExecutor] = None
# ✅ FIX v0.6.1: asyncio.Lock() a nivel de módulo lanza DeprecationWarning/RuntimeError
# en Python 3.10+ porque no hay event loop activo en tiempo de import.
# threading.Lock() es seguro en cualquier contexto de importación.
# El __init__ de ParallelExecutor no es async, por lo que threading.Lock basta.
_executor_lock = threading.Lock()


async def get_parallel_executor(
    max_concurrent: int = 10,
    task_queue: Optional[TaskQueue] = None,
    retry_config: Optional[RetryConfig] = None,
    metrics_max_entries: int = 1000,
) -> ParallelExecutor:
    """
    Obtiene instancia singleton del ParallelExecutor.

    Args:
        max_concurrent: Máximo de tareas concurrentes
        task_queue: TaskQueue para encolado (crea nueva si None)
        retry_config: Configuración de retry
        metrics_max_entries: Máximo de entradas en métricas

    Returns:
        Instancia de ParallelExecutor
    """
    global _executor_instance

    if _executor_instance is None:
        # ✅ FIX v0.6.1: threading.Lock() — double-checked locking, sin event loop
        with _executor_lock:
            if _executor_instance is None:
                # Obtener singleton de TaskQueue para compartir instancia
                shared_queue = task_queue or await get_task_queue()
                _executor_instance = ParallelExecutor(
                    max_concurrent=max_concurrent,
                    task_queue=shared_queue,
                    retry_config=retry_config,
                    metrics_max_entries=metrics_max_entries,
                )

    return _executor_instance


async def reset_parallel_executor() -> None:
    """Reinicia el singleton del ParallelExecutor."""
    global _executor_instance

    if _executor_instance:
        await _executor_instance.shutdown(wait_for_pending=False)
        _executor_instance = None
