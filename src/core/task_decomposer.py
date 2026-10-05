#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — TaskDecomposer: Descomposición Jerárquica de Tareas
# Ruta: /ruta/a/axioma/src/core/task_decomposer.py
# Autor: SaintWick
# Fecha: 2026-04-09
# Propósito: Descomponer tareas complejas en sub-tareas ejecutables con dependencias
# ✅ v0.6.0 (Fase 3): TeamCoordinator — swarm coordinado con assign_agent() y monitor_progress()
# ═══════════════════════════════════════════════════════════════
"""
Descompone tareas complejas en sub-tareas ejecutables, asignando tipos,
prioridades y dependencias lógicas. Integrado con `PromptManager` para
generar instrucciones por sub-paso.
"""
from __future__ import annotations
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from src.domain.types import TaskType, AgentRole
from src.prompts.prompt_manager import get_prompt_manager

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """
    Representa una sub-tarea atómica dentro de un plan multi-step.
    """
    task_id: str
    description: str
    task_type: TaskType
    agent_role: AgentRole
    depends_on: List[str] = field(default_factory=list)
    priority: int = 2  # 1=high, 2=medium, 3=low
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para logging/transporte."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "task_type": self.task_type.value,
            "agent_role": self.agent_role.value,
            "depends_on": self.depends_on,
            "priority": self.priority,
            "metadata": self.metadata
        }

    def __repr__(self) -> str:
        return f"SubTask({self.task_id}, type={self.task_type.value}, priority={self.priority})"


class TaskDecomposer:
    """
    Analiza complejidad y genera secuencia lógica de SubTasks.
    Usa patrones heurísticos + LLM fallback si se requiere refinamiento.

    Flujo típico:
        decomposer = TaskDecomposer()
        subtasks = decomposer.decompose("plan_001", query, TaskType.CODE_GENERATION)
        for st in subtasks:
            prompt = decomposer.generate_step_prompt(st)
            # ... ejecutar con agente correspondiente
    """

    # Patrones heurísticos para detección de complejidad
    COMPLEXITY_INDICATORS: Dict[str, float] = {
        r"(crear|construir|desarrollar|implementar).*aplicación": 2.0,
        r"(integrar|conectar|orquestar).*(múltiples|varias|diferentes)": 1.5,
        r"(refactorizar|migrar|actualizar).*(completo|entero|base)": 1.8,
        r"(pipeline|workflow|arquitectura|sistema)": 1.2,
        r"(APIs?|servicios?|microservicios?)": 1.0,
        r"(validar|testear|auditar|verificar).*completo": 0.8,
        r"(documentar|explicar|comentar).*módulo": 0.5,
    }

    # ✅ CORREGIDO: SEARCHER → RESEARCHER (Enum válido)
    _TASK_TO_AGENT: Dict[TaskType, AgentRole] = {
        TaskType.CODE_GENERATION: AgentRole.CODER,
        TaskType.CODE_CORRECTION: AgentRole.CODER,
        TaskType.CODE_EXPLANATION: AgentRole.CODER,
        TaskType.CODE_REVIEW: AgentRole.CODER,
        TaskType.CODE_OPTIMIZATION: AgentRole.CODER,
        TaskType.CODE_TESTING: AgentRole.CODER,
        TaskType.CODE_DOCUMENTATION: AgentRole.CODER,
        TaskType.CODE_MIGRATION: AgentRole.CODER,
        TaskType.CODE_DEPLOYMENT: AgentRole.CODER,
        TaskType.CODE_DEBUGGING: AgentRole.CODER,
        TaskType.DATA_ANALYSIS: AgentRole.ANALYST,
        TaskType.DOCUMENT_ANALYSIS: AgentRole.ANALYST,
        TaskType.SENTIMENT_ANALYSIS: AgentRole.ANALYST,
        TaskType.TREND_ANALYSIS: AgentRole.ANALYST,
        TaskType.RISK_ANALYSIS: AgentRole.ANALYST,
        TaskType.WEB_SEARCH: AgentRole.RESEARCHER,
        TaskType.NEWS_SEARCH: AgentRole.RESEARCHER,
        TaskType.ACADEMIC_SEARCH: AgentRole.RESEARCHER,
        TaskType.CODE_SEARCH: AgentRole.RESEARCHER,
        TaskType.GENERAL_CHAT: AgentRole.ORCHESTRATOR,
        TaskType.TECHNICAL_SUPPORT: AgentRole.ORCHESTRATOR,
        TaskType.TUTORING: AgentRole.ORCHESTRATOR,
        TaskType.BRAINSTORMING: AgentRole.ORCHESTRATOR,
        TaskType.ROLEPLAY: AgentRole.ORCHESTRATOR,
        TaskType.WEATHER_QUERY: AgentRole.ORCHESTRATOR,
        TaskType.TIME_QUERY: AgentRole.ORCHESTRATOR,
        TaskType.FINANCE_QUERY: AgentRole.ORCHESTRATOR,
        TaskType.NEWS_QUERY: AgentRole.RESEARCHER,
        TaskType.TRANSLATION: AgentRole.ORCHESTRATOR,
        TaskType.SUMMARIZATION: AgentRole.RESEARCHER,
        TaskType.FACT_CHECKING: AgentRole.VALIDATOR,
        TaskType.QUALITY_VALIDATION: AgentRole.VALIDATOR,
        TaskType.SECURITY_AUDIT: AgentRole.VALIDATOR,
        TaskType.CONTENT_CREATION: AgentRole.ORCHESTRATOR,
        TaskType.SCRIPT_WRITING: AgentRole.CODER,
        TaskType.POETRY_GENERATION: AgentRole.ORCHESTRATOR,
    }

    # ✅ CORREGIDO: PHASE_TEMPLATES ahora incluye AgentRole como tercer elemento
    PHASE_TEMPLATES: Dict[TaskType, List[Tuple[str, TaskType, AgentRole]]] = {
        TaskType.CODE_GENERATION: [
            ("Análisis de requisitos y diseño", TaskType.DATA_ANALYSIS, AgentRole.ANALYST),
            ("Implementación del núcleo", TaskType.CODE_GENERATION, AgentRole.CODER),
            ("Validación y testing", TaskType.QUALITY_VALIDATION, AgentRole.VALIDATOR),
            ("Documentación y entrega", TaskType.CODE_DOCUMENTATION, AgentRole.CODER),
        ],
        TaskType.CODE_REVIEW: [
            ("Revisión de estructura y estilo", TaskType.CODE_REVIEW, AgentRole.CODER),
            ("Detección de vulnerabilidades", TaskType.SECURITY_AUDIT, AgentRole.VALIDATOR),
            ("Sugerencias de optimización", TaskType.CODE_OPTIMIZATION, AgentRole.CODER),
            ("Reporte final", TaskType.GENERAL_CHAT, AgentRole.RESEARCHER),
        ],
        TaskType.WEB_SEARCH: [
            ("Definición de términos de búsqueda", TaskType.GENERAL_CHAT, AgentRole.ORCHESTRATOR),
            ("Ejecución de búsquedas paralelas", TaskType.WEB_SEARCH, AgentRole.RESEARCHER),
            ("Filtrado y validación de fuentes", TaskType.QUALITY_VALIDATION, AgentRole.VALIDATOR),
            ("Síntesis de resultados", TaskType.SUMMARIZATION, AgentRole.RESEARCHER),
        ],
        TaskType.DATA_ANALYSIS: [
            ("Carga y preprocesamiento de datos", TaskType.DATA_ANALYSIS, AgentRole.ANALYST),
            ("Análisis exploratorio", TaskType.DATA_ANALYSIS, AgentRole.ANALYST),
            ("Identificación de patrones", TaskType.TREND_ANALYSIS, AgentRole.ANALYST),
            ("Generación de insights", TaskType.GENERAL_CHAT, AgentRole.RESEARCHER),
        ],
    }

    def __init__(
        self,
        max_depth: int = 4,
        llm_refine: bool = False,
        min_complexity_threshold: float = 1.5
    ):
        """
        Inicializa el descomponedor de tareas.
        """
        self.max_depth = max_depth
        self.llm_refine = llm_refine
        self.min_complexity_threshold = min_complexity_threshold
        logger.debug(
            f"TaskDecomposer inicializado: max_depth={max_depth}, "
            f"llm_refine={llm_refine}, threshold={min_complexity_threshold}"
        )

    def decompose(
        self,
        task_id: str,
        description: str,
        inferred_type: TaskType
    ) -> List[SubTask]:
        """
        Genera lista de SubTasks a partir de una descripción compleja.
        """
        complexity = self._estimate_complexity(description, inferred_type)

        # Si es tarea simple, retornar como única sub-tarea
        if complexity < self.min_complexity_threshold:
            logger.debug(f"Tarea {task_id} es atómica (complejidad={complexity:.2f})")
            agent_role = self._TASK_TO_AGENT.get(inferred_type, AgentRole.ORCHESTRATOR)
            return [SubTask(
                task_id=task_id,
                description=description,
                task_type=inferred_type,
                agent_role=agent_role,
                priority=1
            )]

        logger.info(f"Descomponiendo {task_id}: complejidad={complexity:.2f}")

        subtasks = self._heuristic_split(task_id, description, inferred_type, complexity)

        if self.llm_refine and len(subtasks) > self.max_depth:
            subtasks = self._trim_and_link(subtasks)

        self._validate_dependencies(subtasks)

        logger.info(f"Descomposición de {task_id}: {len(subtasks)} sub-tareas generadas")
        return subtasks

    def _estimate_complexity(self, text: str, task_type: TaskType) -> float:
        """
        Calcula score de complejidad 0.1-5.0 basado en múltiples factores.
        """
        score = 0.5
        text_lower = text.lower()

        if len(text) > 200: score += 0.5
        if len(text) > 500: score += 1.0

        action_verbs = len(re.findall(
            r'(crea|analiza|compara|despliega|integra|refactoriza|valida|'
            r'orquesta|migra|audita|sintetiza)',
            text_lower, re.IGNORECASE
        ))
        score += min(action_verbs * 0.4, 2.0)

        for pattern, weight in self.COMPLEXITY_INDICATORS.items():
            if re.search(pattern, text_lower, re.IGNORECASE):
                score += weight

        entities = len(re.findall(r'(?:y|,|;|más|además)', text_lower))
        score += min(entities * 0.2, 1.0)

        task_weights = {
            TaskType.CODE_GENERATION: 0.3,
            TaskType.CODE_REVIEW: 0.2,
            TaskType.WEB_SEARCH: 0.1,
            TaskType.DATA_ANALYSIS: 0.4,
        }
        score += task_weights.get(task_type, 0.2)

        return min(max(score, 0.1), 5.0)

    def _heuristic_split(
        self,
        root_id: str,
        description: str,
        base_type: TaskType,
        complexity: float
    ) -> List[SubTask]:
        """
        Divide la tarea en fases estándar usando plantillas predefinidas.
        """
        phases = self.PHASE_TEMPLATES.get(
            base_type,
            [
                ("Preparación y contexto", TaskType.GENERAL_CHAT, AgentRole.ORCHESTRATOR),
                (f"Ejecución: {description[:100]}", base_type, self._TASK_TO_AGENT.get(base_type, AgentRole.ORCHESTRATOR)),
                ("Validación de resultados", TaskType.QUALITY_VALIDATION, AgentRole.VALIDATOR),
                ("Entrega final", TaskType.GENERAL_CHAT, AgentRole.ORCHESTRATOR),
            ]
        )

        tasks: List[SubTask] = []
        prev_id: Optional[str] = None

        for i, (phase_desc, phase_type, phase_agent) in enumerate(phases):
            if complexity < (i + 1) * 0.9:
                break
            if len(tasks) >= self.max_depth:
                break

            task_id = f"{root_id}_step_{i+1}"
            depends_on = [prev_id] if prev_id else []

            tasks.append(SubTask(
                task_id=task_id,
                description=phase_desc,
                task_type=phase_type,
                agent_role=phase_agent,
                depends_on=depends_on,
                priority=1 if i == 0 else (2 if i < len(phases) - 1 else 3),
                metadata={"phase_index": i, "root_task": root_id}
            ))
            prev_id = task_id

        if not tasks:
            agent_role = self._TASK_TO_AGENT.get(base_type, AgentRole.ORCHESTRATOR)
            return [SubTask(
                task_id=root_id,
                description=description,
                task_type=base_type,
                agent_role=agent_role,
                priority=1
            )]

        return tasks

    def _trim_and_link(self, tasks: List[SubTask]) -> List[SubTask]:
        """Asegura coherencia de IDs y dependencias tras poda de lista."""
        if len(tasks) <= self.max_depth:
            return tasks

        trimmed = tasks[:self.max_depth]

        for i, task in enumerate(trimmed):
            if i == 0:
                task.depends_on = []
            else:
                task.depends_on = [trimmed[i-1].task_id]
            task.priority = i + 1

        logger.debug(f"Lista recortada a {len(trimmed)} tareas con dependencias validadas")
        return trimmed

    def _validate_dependencies(self, tasks: List[SubTask]) -> None:
        """Valida que todas las dependencias referenciadas existan en la lista."""
        task_ids = {t.task_id for t in tasks}

        for task in tasks:
            for dep in task.depends_on:
                if dep not in task_ids:
                    logger.warning(
                        f"Dependencia inválida en {task.task_id}: {dep} no existe. "
                        f"Eliminando dependencia."
                    )
                    task.depends_on.remove(dep)

        def has_cycle(task_id: str, visited: set, rec_stack: set) -> bool:
            visited.add(task_id)
            rec_stack.add(task_id)

            task = next((t for t in tasks if t.task_id == task_id), None)
            if task:
                for dep in task.depends_on:
                    if dep not in visited:
                        if has_cycle(dep, visited, rec_stack):
                            return True
                    elif dep in rec_stack:
                        return True

            rec_stack.remove(task_id)
            return False

        visited: set = set()
        for task in tasks:
            if task.task_id not in visited:
                if has_cycle(task.task_id, visited, set()):
                    raise ValueError(f"Ciclo de dependencias detectado en tarea {task.task_id}")

    def generate_step_prompt(
        self,
        subtask: SubTask,
        context: Optional[str] = None,
        previous_outputs: Optional[List[str]] = None
    ) -> str:
        """Genera prompt optimizado para un sub-paso específico."""
        pm = get_prompt_manager(subtask.agent_role.value)
        complexity_hint = "complex" if len(subtask.description) > 150 else "medium"

        step_context = None
        if previous_outputs:
            ctx_parts = [f"[Paso {i+1}]: {out[:200]}..." for i, out in enumerate(previous_outputs)]
            step_context = "\n".join(ctx_parts)

        return pm.build(
            task_type=subtask.task_type.value,
            user_intent=subtask.description,
            complexity_hint=complexity_hint,
            context=ContextLayer.from_text(step_context) if step_context else None,
            include_reflection_precheck=True,
            active_tools=None
        )

    def get_decomposition_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas de uso del descomponedor."""
        return {
            "max_depth": self.max_depth,
            "llm_refine_enabled": self.llm_refine,
            "complexity_threshold": self.min_complexity_threshold,
            "phase_templates_count": len(self.PHASE_TEMPLATES),
            "complexity_patterns_count": len(self.COMPLEXITY_INDICATORS),
        }

    def reset(self) -> None:
        """Reinicia estado interno (útil para testing)."""
        logger.debug("TaskDecomposer reseteado")


# ============================================================================
# ✅ v0.6.0 (Fase 3): TEAM COORDINATOR — Swarm coordinado sobre TaskDecomposer
# ============================================================================

from dataclasses import dataclass as _dataclass
from enum import Enum as _Enum


class SwarmStatus(str, _Enum):
    """Estado global del swarm de subtareas."""
    PENDING   = "pending"
    RUNNING   = "running"
    BLOCKED   = "blocked"     # Subtarea bloqueada por dependencia no resuelta
    COMPLETED = "completed"
    FAILED    = "failed"
    ESCALATED = "escalated"   # Requiere intervención humana


@_dataclass
class SubTaskStatus:
    """Estado de ejecución de una subtarea individual."""
    subtask: "SubTask"
    status: SwarmStatus = SwarmStatus.PENDING
    assigned_agent: Optional[AgentRole] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round(self.completed_at - self.started_at, 3)
        return None

    @property
    def is_done(self) -> bool:
        return self.status in (SwarmStatus.COMPLETED, SwarmStatus.FAILED, SwarmStatus.ESCALATED)


class TeamCoordinator:
    """
    Coordinador de swarm de agentes sobre subtareas generadas por TaskDecomposer.

    Responsabilidades:
        1. assign_agent()     → Selecciona agente óptimo por capacidad + carga
        2. monitor_progress() → Trackea estado, detecta bloqueos, escala si necesario
        3. get_execution_plan()→ Retorna plan ordenado respetando dependencias

    Uso:
        decomposer = TaskDecomposer()
        coordinator = TeamCoordinator(decomposer=decomposer)

        subtasks = decomposer.decompose("t001", description, TaskType.CODE_GENERATION)
        plan = coordinator.get_execution_plan(subtasks)
        coordinator.register_session("session_001", subtasks)

        for subtask in plan:
            agent_role = coordinator.assign_agent(subtask)
            # ejecutar con agente correspondiente
            coordinator.mark_started("session_001", subtask.task_id)
            # ... ejecución ...
            coordinator.mark_completed("session_001", subtask.task_id, result="output")

        status = coordinator.monitor_progress("session_001")
    """

    # Timeout por defecto antes de escalar una subtarea bloqueada
    _BLOCK_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        decomposer: Optional["TaskDecomposer"] = None,
        block_timeout: float = _BLOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._decomposer = decomposer or TaskDecomposer()
        self._block_timeout = block_timeout
        # Estado por session_id → {task_id: SubTaskStatus}
        self._sessions: Dict[str, Dict[str, SubTaskStatus]] = {}
        logger.info(f"[TeamCoordinator] Inicializado — block_timeout={block_timeout}s")

    def get_execution_plan(self, subtasks: List[SubTask]) -> List[SubTask]:
        """
        Retorna subtareas ordenadas para ejecución respetando dependencias.
        Usa topological sort — subtareas sin dependencias van primero.
        """
        if not subtasks:
            return []

        id_to_task = {t.task_id: t for t in subtasks}
        in_degree = {t.task_id: len(t.depends_on) for t in subtasks}

        # Índice de adyacencia inversa — evita scan O(n) por nodo.
        # Construcción O(n), lookup O(1) por dependiente.
        dependents: Dict[str, List[str]] = {t.task_id: [] for t in subtasks}
        for t in subtasks:
            for dep_id in t.depends_on:
                if dep_id in dependents:
                    dependents[dep_id].append(t.task_id)

        # Heap por prioridad — mantiene orden sin re-sort en cada iteración.
        import heapq
        ready_heap: list = []
        for t in subtasks:
            if in_degree[t.task_id] == 0:
                heapq.heappush(ready_heap, (t.priority, t.task_id))

        ordered: List[SubTask] = []
        while ready_heap:
            _, current_id = heapq.heappop(ready_heap)
            current = id_to_task[current_id]
            ordered.append(current)
            for dep_task_id in dependents[current_id]:
                in_degree[dep_task_id] -= 1
                if in_degree[dep_task_id] == 0:
                    dep_task = id_to_task[dep_task_id]
                    heapq.heappush(ready_heap, (dep_task.priority, dep_task_id))

        # Si no se procesaron todas → ciclo (ya validado por TaskDecomposer)
        if len(ordered) < len(subtasks):
            logger.warning("[TeamCoordinator] No se pudo ordenar todas las subtareas — usando orden original")
            return subtasks

        return ordered

    def assign_agent(self, subtask: SubTask) -> AgentRole:
        """
        Selecciona agente óptimo para una subtarea.

        Criterios (en orden):
          1. Rol explícito en SubTask.agent_role
          2. Mapeo TaskType → AgentRole de TaskDecomposer._TASK_TO_AGENT
          3. Fallback: ORCHESTRATOR

        Extensión futura: considerar carga actual via ParallelExecutor.active_count.
        """
        # El rol ya viene determinado por TaskDecomposer
        if subtask.agent_role:
            return subtask.agent_role

        role = TaskDecomposer._TASK_TO_AGENT.get(subtask.task_type, AgentRole.ORCHESTRATOR)
        logger.debug(f"[TeamCoordinator] assign_agent: {subtask.task_id} → {role.value}")
        return role

    def register_session(self, session_id: str, subtasks: List[SubTask]) -> None:
        """Registra una sesión con sus subtareas para tracking."""
        self._sessions[session_id] = {
            t.task_id: SubTaskStatus(subtask=t)
            for t in subtasks
        }
        logger.info(
            f"[TeamCoordinator] Sesión registrada: {session_id} "
            f"({len(subtasks)} subtareas)"
        )

    def mark_started(self, session_id: str, task_id: str) -> None:
        """
        Marca una subtarea como iniciada.

        ✅ FIX AUDITORÍA: antes usaba _asyncio.get_event_loop().time() cuando
        había un loop corriendo, o time.time() si no — dos relojes con épocas
        distintas (loop.time() es monotónico desde un origen arbitrario, NO
        el epoch Unix). monitor_progress() y SubTaskStatus.duration_seconds
        siempre restan usando time.time() — si started_at quedaba seteado
        con loop.time(), la resta daba un número gigante sin sentido,
        marcando la subtarea como "bloqueada" casi de inmediato sin importar
        cuánto llevara corriendo en realidad. Ahora usa time.time() siempre,
        consistente con completed_at (que ya lo hacía).
        """
        if session_id in self._sessions and task_id in self._sessions[session_id]:
            st = self._sessions[session_id][task_id]
            st.status = SwarmStatus.RUNNING
            st.started_at = __import__('time').time()
            st.assigned_agent = st.subtask.agent_role

    def mark_completed(
        self, session_id: str, task_id: str, result: str = ""
    ) -> None:
        """Marca una subtarea como completada."""
        if session_id in self._sessions and task_id in self._sessions[session_id]:
            st = self._sessions[session_id][task_id]
            st.status = SwarmStatus.COMPLETED
            st.completed_at = __import__('time').time()
            st.result = result

    def mark_failed(
        self, session_id: str, task_id: str, error: str = ""
    ) -> None:
        """Marca una subtarea como fallida."""
        if session_id in self._sessions and task_id in self._sessions[session_id]:
            st = self._sessions[session_id][task_id]
            st.status = SwarmStatus.FAILED
            st.completed_at = __import__('time').time()
            st.error = error

    def monitor_progress(self, session_id: str) -> Dict[str, Any]:
        """
        Trackea progreso del swarm para una sesión.

        Detecta:
          - Subtareas bloqueadas (RUNNING > block_timeout sin completar)
          - Porcentaje de completitud
          - Necesidad de escalada humana

        Returns:
            Dict con status, completado%, bloqueadas, escaladas.
        """
        if session_id not in self._sessions:
            return {"error": f"Sesión no registrada: {session_id}"}

        statuses = self._sessions[session_id]
        now = __import__('time').time()

        total = len(statuses)
        completed = sum(1 for s in statuses.values() if s.status == SwarmStatus.COMPLETED)
        failed = sum(1 for s in statuses.values() if s.status == SwarmStatus.FAILED)
        running = [s for s in statuses.values() if s.status == SwarmStatus.RUNNING]
        escalated = []

        # Detectar bloqueos por timeout
        blocked = []
        for st in running:
            if st.started_at and (now - st.started_at) > self._block_timeout:
                st.status = SwarmStatus.BLOCKED
                blocked.append(st.subtask.task_id)
                logger.warning(
                    f"[TeamCoordinator] Subtarea bloqueada: {st.subtask.task_id} "
                    f"({now - st.started_at:.0f}s > timeout {self._block_timeout}s)"
                )

        # Escalar si hay fallas críticas (prioridad 1 fallida)
        critical_failed = [
            s for s in statuses.values()
            if s.status == SwarmStatus.FAILED and s.subtask.priority == 1
        ]
        if critical_failed:
            for st in critical_failed:
                if st.status != SwarmStatus.ESCALATED:
                    st.status = SwarmStatus.ESCALATED
                    escalated.append(st.subtask.task_id)
                    logger.error(
                        f"[TeamCoordinator] Escalando a humano: {st.subtask.task_id} "
                        f"(subtarea crítica fallida)"
                    )

        overall = SwarmStatus.COMPLETED if completed == total else (
            SwarmStatus.ESCALATED if escalated else (
                SwarmStatus.BLOCKED if blocked else (
                    SwarmStatus.FAILED if failed == total else SwarmStatus.RUNNING
                )
            )
        )

        return {
            "session_id": session_id,
            "overall_status": overall.value,
            "total": total,
            "completed": completed,
            "failed": failed,
            "blocked": blocked,
            "escalated": escalated,
            "completion_pct": round(completed / max(total, 1) * 100, 1),
            "requires_human": bool(escalated),
        }

    def get_session_summary(self, session_id: str) -> List[Dict[str, Any]]:
        """Retorna resumen detallado por subtarea para una sesión."""
        if session_id not in self._sessions:
            return []
        return [
            {
                "task_id": st.subtask.task_id,
                "description": st.subtask.description,
                "agent": st.subtask.agent_role.value,
                "status": st.status.value,
                "duration_s": st.duration_seconds,
                "error": st.error,
            }
            for st in self._sessions[session_id].values()
        ]


# Import diferido para evitar circular dependency
from src.prompts.context_injection import ContextLayer  # noqa: E402
