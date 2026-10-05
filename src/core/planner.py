#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# planner.py - Orquestador de Planificación MultiPaso (MultiStepPlanner)
# Ruta: src/core/planner.py
# Autor: SaintWick
# Versión: 0.1.0
#
# Descompone consultas complejas en sub-tareas, construye un grafo de
# dependencias (DAG) y ejecuta los pasos de forma secuencial o en paralelo.
# Mantiene un historial acotado de planes ejecutados y expone métricas para
# monitoreo. Integra con TaskDecomposer para la descomposición semántica y
# con DependencyGraph para la resolución de dependencias.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from src.domain.types import TaskType
from src.core.dependency_graph import DependencyGraph
from src.core.task_decomposer import TaskDecomposer, SubTask

logger = logging.getLogger(__name__)

_PLAN_HISTORY_MAXLEN = 200


@dataclass
class PlanResult:
    """Resultado de la ejecución de un plan multi-step."""
    plan_id: str
    status: str  # pending | running | completed | partial_failure | failed
    steps_completed: int
    total_steps: int
    outputs: Dict[str, Any]
    errors: List[str]
    duration_ms: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "steps_completed": self.steps_completed,
            "total_steps": self.total_steps,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }


class MultiStepPlanner:
    """
    Crea, valida y ejecuta planes multi-paso.
    Mantiene estado en memoria y expone métricas para BoundedMetrics.
    """

    def __init__(
        self,
        decomposer: Optional[TaskDecomposer] = None,
        max_plans: int = 50,
        enable_parallel: bool = False,
    ):
        self.decomposer = decomposer or TaskDecomposer()
        self._active_plans: Dict[str, DependencyGraph] = {}
        self._executors: Dict[str, Callable[..., Any]] = {}
        self._max_plans = max_plans
        self._enable_parallel = enable_parallel
        # ✅ Semana 0: bounded — evita crecimiento infinito en RAM
        self._plan_history: deque = deque(maxlen=_PLAN_HISTORY_MAXLEN)
        logger.debug(
            f"MultiStepPlanner inicializado: max_plans={max_plans}, "
            f"parallel={enable_parallel}, history_maxlen={_PLAN_HISTORY_MAXLEN}"
        )

    def plan(
        self,
        objective: str,
        task_type: Optional[TaskType] = None,
        max_steps: int = 10,
    ) -> List[str]:
        """
        Descompone `objective` en una lista plana de descripciones de pasos.
        Sync — pensado para invocarse vía asyncio.to_thread() (ej. JarvisEngine).
        No arma DAG ni ejecuta — solo delega en TaskDecomposer.decompose().
        Para ejecución real con dependencias usar create_plan()/execute_plan().
        """
        tt = task_type or TaskType.AUTONOMOUS_TASK
        pid = f"plan_{uuid.uuid4().hex[:8]}"
        subtasks = self.decomposer.decompose(pid, objective, tt)
        return [st.description for st in subtasks[:max_steps]]

    async def create_plan(
        self,
        plan_id: str,
        query: str,
        task_type: TaskType,
        executor_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> Dict[str, Any]:
        """Descompone query, construye DAG y registra plan activo."""
        pid = plan_id or f"plan_{uuid.uuid4().hex[:8]}"

        if len(self._active_plans) >= self._max_plans:
            oldest = next(iter(self._active_plans))
            del self._active_plans[oldest]
            logger.warning(f"Límite de planes alcanzado. Eliminando {oldest}")

        start = time.perf_counter()
        task_type_value = task_type.value if hasattr(task_type, "value") else str(task_type)
        logger.info(f"Creando plan {pid} para tipo {task_type_value}")

        subtasks = self.decomposer.decompose(pid, query, task_type)
        if not subtasks:
            raise ValueError("No se pudieron generar sub-tareas para la consulta")

        dag = DependencyGraph()
        for st in subtasks:
            dag.add_task(
                task_id=st.task_id,
                depends_on=st.depends_on,
                metadata={
                    "type": st.task_type.value,
                    "agent": st.agent_role.value,
                    "priority": st.priority,
                    "description": st.description,
                },
            )
            self._executors[st.task_id] = executor_fn

        self._active_plans[pid] = dag
        duration = (time.perf_counter() - start) * 1000

        return {
            "plan_id": pid,
            "status": "ready",
            "steps": len(subtasks),
            "dependency_graph": dag.to_dict(),
            "planning_latency_ms": round(duration, 2),
        }

    async def execute_plan(
        self,
        plan_id: str,
        initial_context: Optional[Dict[str, Any]] = None,
    ) -> PlanResult:
        """
        Ejecuta plan respetando topología del DAG.
        Si enable_parallel=True ejecuta en paralelo los pasos sin dependencias entre sí.
        Pasa outputs de padres a hijos vía contexto.
        """
        if plan_id not in self._active_plans:
            raise ValueError(f"Plan {plan_id} no existe o fue eliminado")

        dag = self._active_plans[plan_id]
        outputs: Dict[str, Any] = {}
        errors: List[str] = []
        start = time.perf_counter()
        completed = 0
        total_steps = len(dag)
        ready = dag.get_ready_tasks()

        while ready and not errors:
            if self._enable_parallel and len(ready) > 1:

                async def _run_task(task_id: str) -> tuple[str, Any]:
                    exec_fn = self._executors.get(task_id)
                    if not exec_fn:
                        raise RuntimeError(f"No hay ejecutor registrado para {task_id}")
                    step_context = dict(initial_context or {})
                    node = dag._nodes.get(task_id)
                    for dep_id in (node.dependencies if node else []):
                        if dep_id in outputs:
                            step_context[f"prev_{dep_id}"] = outputs[dep_id]
                    result = exec_fn(task_id, step_context)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return task_id, result

                results = await asyncio.gather(
                    *[_run_task(tid) for tid in ready],
                    return_exceptions=True,
                )
                for item in results:
                    if isinstance(item, Exception):
                        errors.append(f"Error paralelo: {item}")
                        logger.error(f"Plan {plan_id} error en paso paralelo: {item}")
                        break
                    task_id, result = item
                    outputs[task_id] = result
                    dag.mark_completed(task_id)
                    completed += 1
                if errors:
                    break
                ready = dag.get_ready_tasks()

            else:
                task_id = ready[0]
                try:
                    exec_fn = self._executors.get(task_id)
                    if not exec_fn:
                        raise RuntimeError(f"No hay ejecutor registrado para {task_id}")

                    step_context = dict(initial_context or {})
                    node = dag._nodes.get(task_id)
                    for dep_id in (node.dependencies if node else []):
                        if dep_id in outputs:
                            step_context[f"prev_{dep_id}"] = outputs[dep_id]

                    result = exec_fn(task_id, step_context)
                    if asyncio.iscoroutine(result):
                        result = await result

                    outputs[task_id] = result
                    dag.mark_completed(task_id)
                    completed += 1
                    ready = dag.get_ready_tasks()

                except Exception as e:
                    err_msg = f"Error en {task_id}: {e}"
                    logger.error(f"Plan {plan_id} falló en paso {task_id}: {e}")
                    errors.append(err_msg)
                    dag._nodes[task_id].status = "failed"
                    break

        duration = (time.perf_counter() - start) * 1000
        status = (
            "completed" if not errors
            else ("partial_failure" if completed > 0 else "failed")
        )
        # ✅ FIX AUDITORÍA: antes esto contaba la cantidad de dependencias
        # DIRECTAS del nodo con más deps (len(data.get("deps", []))) — un
        # número distinto y mal nombrado como "max_depth". dependency_graph.py
        # ya tenía la lógica correcta de profundidad de cadena (usada en
        # _detect_risky_chains()) pero no la exponía — ahora sí, vía
        # dag.get_max_depth().
        max_depth = dag.get_max_depth()
        plan_result = PlanResult(
            plan_id=plan_id,
            status=status,
            steps_completed=completed,
            total_steps=total_steps,
            outputs=outputs,
            errors=errors,
            duration_ms=round(duration, 2),
            metadata={"max_depth": max_depth, "parallel_enabled": self._enable_parallel},
        )
        self._plan_history.append(plan_result)
        return plan_result

    def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retorna estado actual del DAG de un plan activo."""
        if plan_id not in self._active_plans:
            return None
        return self._active_plans[plan_id].to_dict()

    def get_plan_result(self, plan_id: str) -> Optional[PlanResult]:
        """Busca en el historial el resultado final de un plan."""
        for pr in reversed(self._plan_history):
            if pr.plan_id == plan_id:
                return pr
        return None

    def cleanup_plan(self, plan_id: str) -> bool:
        """Elimina plan y ejecutores asociados para liberar memoria."""
        if plan_id in self._active_plans:
            dag = self._active_plans.pop(plan_id)
            for task_id in dag._nodes:
                self._executors.pop(task_id, None)
            logger.debug(f"Plan {plan_id} limpiado de memoria")
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Métricas agregadas del planificador para monitoreo."""
        history = list(self._plan_history)
        total = len(history)
        completed = sum(1 for p in history if p.status == "completed")
        failed = sum(1 for p in history if p.status in ("failed", "partial_failure"))
        avg_duration = sum(p.duration_ms for p in history) / max(total, 1)
        return {
            "active_plans": len(self._active_plans),
            "total_executed": total,
            "completed": completed,
            "failed": failed,
            "success_rate": round(completed / max(total, 1), 4),
            "avg_duration_ms": round(avg_duration, 2),
            "max_plans_limit": self._max_plans,
            "history_maxlen": _PLAN_HISTORY_MAXLEN,
        }

    def clear_history(self) -> None:
        """Reinicia historial de planes (útil para testing)."""
        self._plan_history.clear()
        logger.debug("Historial de planes limpiado")
