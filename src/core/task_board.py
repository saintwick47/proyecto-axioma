#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — TaskBoard: Tablero de tareas de coordinación
# Ruta: src/core/task_board.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 4 plan_harness — Coordinación):
#   Registro de tareas de agentes con dependencias, modelo requerido y
#   estados explícitos de espera de memoria/modelo:
#     queued → running → done | failed
#                    ↘ waiting_model (slot de memoria ocupado)
#                    ↘ waiting_result (dependencia cruzada de modelo)
#                    ↘ refused_oom   (MemoryGuard rechazó la carga)
#   Permite al AgentCoordinator saber qué tareas están listas, cuáles
#   esperan un resultado y cuáles fueron rechazadas por memoria.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ⚠️ NO unificar con el `QueueTaskStatus` de `task_queue.py` (ni con el de netron): son máquinas de
# estado de cosas DISTINTAS. Ésta es del TABLERO DE COORDINACIÓN y tiene estados que las otras no tienen
# (`WAITING_MODEL`, `WAITING_RESULT`, `REFUSED_OOM`). Medido el 2026-10-09: unificarlas obligaría a la
# unión de todos los estados y cada consumidor tendría que defenderse de estados imposibles.
class BoardTaskStatus:
    """Estados canónicos de una tarea de coordinación."""
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_MODEL = "waiting_model"     # esperando slot de memoria/modelo
    WAITING_RESULT = "waiting_result"   # esperando resultado de otra tarea
    DONE = "done"
    FAILED = "failed"
    REFUSED_OOM = "refused_oom"         # MemoryGuard rechazó (fail-safe)


@dataclass
class Task:
    """Tarea unitaria del tablero."""
    task_id: str
    name: str
    model: str
    status: str = BoardTaskStatus.QUEUED
    depends_on: List[str] = field(default_factory=list)
    result: Any = None
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "model": self.model,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "error": self.error,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskBoard:
    """
    Tablero de tareas thread-safe del AgentCoordinator.
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = threading.RLock()

    # ── Escritura ───────────────────────────────────────────────────────────

    def add(
        self,
        name: str,
        model: str,
        depends_on: Optional[List[str]] = None,
        task_id: Optional[str] = None,
    ) -> str:
        tid = task_id or f"task_{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._tasks[tid] = Task(
                task_id=tid, name=name, model=model,
                depends_on=list(depends_on or []),
            )
        return tid

    def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        result: Any = None,
        error: str = "",
        **metadata: Any,
    ) -> Optional[Task]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if status is not None:
                task.status = status
            if result is not None:
                task.result = result
            if error:
                task.error = error
            if metadata:
                task.metadata.update(metadata)
            task.updated_at = time.time()
            return task

    def store_result(self, task_id: str, result: Any) -> Optional[Task]:
        return self.update(task_id, status=BoardTaskStatus.DONE, result=result)

    # ── Lectura ─────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task

    def list_by_status(self, status: str) -> List[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.status == status]

    def ready_tasks(self) -> List[Task]:
        """
        Tareas QUEUED cuyas dependencias ya están resueltas (DONE).
        """
        with self._lock:
            tasks = list(self._tasks.values())
        ready = []
        for t in tasks:
            if t.status != BoardTaskStatus.QUEUED:
                continue
            deps = t.depends_on or []
            if all(
                self._tasks.get(d) is not None
                and self._tasks.get(d).status == BoardTaskStatus.DONE
                for d in deps
            ):
                ready.append(t)
        return ready

    def waiting_on(self, task_id: str) -> Optional[str]:
        """Primera dependencia NO resuelta de la tarea, o None."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            for d in task.depends_on or []:
                dep = self._tasks.get(d)
                if dep is None or dep.status != BoardTaskStatus.DONE:
                    return d
            return None

    def get_result(self, task_id: str) -> Any:
        task = self.get(task_id)
        return task.result if task is not None else None

    def all_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    # ── Stats ───────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            by_status: Dict[str, int] = {}
            for t in self._tasks.values():
                by_status[t.status] = by_status.get(t.status, 0) + 1
            return {
                "total": len(self._tasks),
                "by_status": by_status,
                "tasks": [t.to_dict() for t in self._tasks.values()],
            }
