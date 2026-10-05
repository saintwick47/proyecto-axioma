#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.0 — Distributed Queue: Cola Particionada por Tipo
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/distributed_queue.py
# Autor: SaintWick
# Fecha: 2026-04-09
# Propósito: Particionado de cola en memoria por grupo de tarea.
#            Aísla tareas lentas (código) de rápidas (chat/datos).
#            Sin dependencias externas. Backend: asyncio puro.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import threading
from typing import Dict, List, Optional, Any

from src.core.task_queue import TaskQueue, Task, TaskPriority, TaskError

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_event(message: str, level: str = "INFO", extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(category="DISTRIBUTED_QUEUE", message=message,
                          level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 distributed_queue.py:39] excepción degradada (intencional): {_d2e}")


# ============================================================================
# PARTICIONES — Grupos de tareas por velocidad de ejecución
# ============================================================================
# FAST   → chat, datos, búsqueda       (< 5s  típico)
# MEDIUM → análisis, validación        (5-30s típico)
# SLOW   → código, research            (30s+  típico)
# DEFAULT → fallback para tipos no mapeados

PARTITION_MAP: Dict[str, str] = {
    # FAST
    "general_chat": "fast",
    "weather_query": "fast",
    "finance_query": "fast",
    "time_query": "fast",
    "news_query": "fast",
    "web_search": "fast",
    "news_search": "fast",
    # MEDIUM
    "academic_search": "medium",
    "data_analysis": "medium",
    "document_analysis": "medium",
    "sentiment_analysis": "medium",
    "trend_analysis": "medium",
    "risk_analysis": "medium",
    "fact_checking": "medium",
    "quality_validation": "medium",
    "security_audit": "medium",
    # SLOW
    "code_generation": "slow",
    "code_correction": "slow",
    "code_explanation": "slow",
    "code_review": "slow",
    "code_optimization": "slow",
    "code_testing": "slow",
    "code_documentation": "slow",
    "code_migration": "slow",
    "code_deployment": "slow",
    "code_debugging": "slow",
    "research": "slow",
}

PARTITION_NAMES = ("fast", "medium", "slow", "default")


def get_partition(task_type: str) -> str:
    """Retorna el nombre de partición para un task_type dado."""
    return PARTITION_MAP.get(task_type.lower(), "default")


# ============================================================================
# DISTRIBUTED QUEUE
# ============================================================================
class DistributedQueue:
    """
    Cola particionada en memoria por grupo de velocidad de tarea.

    Cada partición es un TaskQueue independiente con workers propios.
    Esto aísla tareas lentas (código) para que no bloqueen tareas rápidas (chat).

    Particiones: fast | medium | slow | default
    Thread-safe. Sin dependencias externas.
    """

    def __init__(
        self,
        max_size_per_partition: int = 500,
        deduplicate: bool = False,
    ):
        self._partitions: Dict[str, TaskQueue] = {
            name: TaskQueue(
                max_size=max_size_per_partition,
                deduplicate=deduplicate,
            )
            for name in PARTITION_NAMES
        }
        self._lock = asyncio.Lock()
        logger.info(
            f"[DISTRIBUTED_QUEUE] Inicializado: {len(self._partitions)} particiones, "
            f"max_size={max_size_per_partition}"
        )
        _log_event("DistributedQueue inicializado", extra={
            "partitions": list(self._partitions.keys()),
            "max_size_per_partition": max_size_per_partition,
        })

    def get_partition_queue(self, partition: str) -> TaskQueue:
        """Retorna la TaskQueue de una partición específica."""
        return self._partitions.get(partition, self._partitions["default"])

    async def enqueue(
        self,
        name: str,
        payload: Dict[str, Any],
        task_type: str = "general_chat",
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout_seconds: Optional[float] = None,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        """
        Encola una tarea en la partición correspondiente a su task_type.

        Returns:
            Task encolada con partition_key en metadata
        """
        partition = get_partition(task_type)
        queue = self._partitions[partition]

        meta = dict(metadata or {})
        meta["partition"] = partition
        meta["task_type"] = task_type

        task = await queue.enqueue(
            name=name,
            payload=payload,
            priority=priority,
            timeout_seconds=timeout_seconds,
            task_id=task_id,
            metadata=meta,
        )

        _log_event(f"Enqueued → partition={partition}", extra={
            "task_id": task.id,
            "task_type": task_type,
            "priority": priority.name,
        })
        return task

    async def dequeue(
        self,
        partition: str,
        timeout: Optional[float] = 0.1,
    ) -> Optional[Task]:
        """Extrae la siguiente tarea de una partición específica."""
        queue = self._partitions.get(partition, self._partitions["default"])
        return await queue.dequeue(timeout=timeout)

    async def cancel(self, task_id: str, partition: Optional[str] = None,
                     reason: str = "User requested") -> bool:
        """
        Cancela una tarea. Si se conoce la partición, busca ahí primero.
        De lo contrario busca en todas.
        """
        if partition:
            return await self._partitions[partition].cancel(task_id, reason=reason)
        for q in self._partitions.values():
            if await q.cancel(task_id, reason=reason):
                return True
        return False

    async def get_stats(self) -> Dict[str, Any]:
        """Estadísticas de todas las particiones."""
        stats: Dict[str, Any] = {}
        total_pending = 0
        total_running = 0
        for name, queue in self._partitions.items():
            s = await queue.get_stats()
            stats[name] = s
            total_pending += s["queue"]["pending"]
            total_running += s["queue"]["running"]
        return {
            "partitions": stats,
            "totals": {
                "pending": total_pending,
                "running": total_running,
            },
        }

    def pending_count(self, partition: str) -> int:
        """Número de tareas pendientes en una partición (no async, para el balanceador)."""
        q = self._partitions.get(partition, self._partitions["default"])
        return len(q._pending)

    def running_count(self, partition: str) -> int:
        """Número de tareas en ejecución en una partición."""
        q = self._partitions.get(partition, self._partitions["default"])
        return len(q._running)

    async def shutdown(self) -> None:
        """Apaga todas las particiones."""
        for name, queue in self._partitions.items():
            await queue.shutdown(wait_for_pending=False)
        logger.info("[DISTRIBUTED_QUEUE] Shutdown completo")


# ============================================================================
# SINGLETON
# ============================================================================
_dq_instance: Optional[DistributedQueue] = None
_dq_lock = threading.Lock()


def get_distributed_queue(
    max_size_per_partition: int = 500,
    deduplicate: bool = False,
) -> DistributedQueue:
    """Singleton thread-safe de DistributedQueue."""
    global _dq_instance
    if _dq_instance is None:
        with _dq_lock:
            if _dq_instance is None:
                _dq_instance = DistributedQueue(
                    max_size_per_partition=max_size_per_partition,
                    deduplicate=deduplicate,
                )
    return _dq_instance


def reset_distributed_queue() -> None:
    """Solo para tests."""
    global _dq_instance
    with _dq_lock:
        _dq_instance = None
