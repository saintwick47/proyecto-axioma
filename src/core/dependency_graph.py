#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# dependency_graph.py - Grafo de Dependencias (DAG) para tareas
# Ruta: /ruta/a/axioma/src/core/dependency_graph.py
# Autor: SaintWick
# Versión: 0.1.0
#
# Implementa un grafo acíclico dirigido para modelar dependencias entre tareas,
# con detección de ciclos, orden topológico, y detección de cadenas riesgosas.
# Se utiliza en el planificador MultiStepPlanner para orquestar ejecuciones.
# ═══════════════════════════════════════════════════════════════
"""
Grafo acíclico dirigido (DAG) para modelar dependencias entre tareas.
Incluye detección de ciclos, orden topológico y resolución dinámica de ejecutables.
"""
from __future__ import annotations
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class DependencyNode:
    """Nodo del grafo de dependencias."""
    task_id: str
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)
    status: str = "pending"  # pending, ready, running, completed, failed
    metadata: Dict[str, Any] = field(default_factory=dict)  # ✅ CORREGIDO: typo + missing ':'

class DependencyGraph:
    """
    Grafo acíclico dirigido para planificación de tareas multi-paso.
    Garantiza ejecución ordenada y detección temprana de dependencias circulares.
    """
    def __init__(self) -> None:
        self._nodes: Dict[str, DependencyNode] = {}
        self._adj: Dict[str, List[str]] = defaultdict(list)
        self._rev_adj: Dict[str, List[str]] = defaultdict(list)
        self._in_degree: Dict[str, int] = defaultdict(int)
        logger.debug("DependencyGraph inicializado")

    def add_task(
        self,
        task_id: str,
        depends_on: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None  # ✅ CORREGIDO: typo + missing ':'
    ) -> None:
        """
        Registra una tarea y sus dependencias.
        Lanza ValueError si se detecta ciclo.
        """
        if task_id in self._nodes:
            logger.warning(f"Task {task_id} ya existe en el grafo. Actualizando dependencias.")

        depends = depends_on or []
        self._nodes[task_id] = DependencyNode(
            task_id=task_id,
            dependencies=depends,
            metadata=metadata or {}
        )
        self._in_degree[task_id] = len(depends)

        for dep in depends:
            self._adj[dep].append(task_id)
            self._rev_adj[task_id].append(dep)
            if dep not in self._nodes:
                self._nodes[dep] = DependencyNode(task_id=dep)

        # ✅ CORREGIDO: _has_cycle() ahora lanza ValueError directamente si detecta ciclo
        try:
            self._has_cycle()
        except ValueError:
            # Rollback ante ciclo detectado
            del self._nodes[task_id]
            for dep in depends:
                if task_id in self._adj[dep]:
                    self._adj[dep].remove(task_id)
                if dep in self._rev_adj[task_id]:
                    self._rev_adj[task_id].remove(dep)
            raise ValueError(f"Ciclo detectado al añadir tarea {task_id}")

        # [C2] Hook: detectar cadenas riesgosas (skip en grafos pequeños)
        risky = self._detect_risky_chains()
        if risky:
            logger.warning(f"[DAG] Cadenas riesgosas detectadas: {risky}")

    def mark_completed(self, task_id: str) -> List[str]:
        """
        Marca tarea como completada y retorna lista de tareas listas para ejecutar.
        """
        if task_id not in self._nodes:
            return []
        self._nodes[task_id].status = "completed"
        ready = []
        for dependent in self._adj[task_id]:
            self._in_degree[dependent] -= 1
            if self._in_degree[dependent] == 0:
                self._nodes[dependent].status = "ready"
                ready.append(dependent)
        return ready

    def get_ready_tasks(self) -> List[str]:
        """Retorna IDs de tareas sin dependencias pendientes (listas para ejecutar)."""
        return [
            tid for tid, node in self._nodes.items()
            if node.status in ("pending", "ready") and self._in_degree[tid] == 0
        ]

    def get_execution_order(self) -> List[str]:
        """Orden topológico de todas las tareas."""
        order = []
        queue = deque([tid for tid in self._nodes if self._in_degree[tid] == 0])
        temp_in_degree = dict(self._in_degree)
        while queue:
            current = queue.popleft()
            order.append(current)
            for neighbor in self._adj[current]:
                temp_in_degree[neighbor] -= 1
                if temp_in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if len(order) != len(self._nodes):
            raise RuntimeError("Orden topológico imposible: ciclo detectado")
        return order

    def _has_cycle(self) -> bool:
        """
        Kahn's algorithm simplificado para detección de ciclos.
        ✅ CORREGIDO: Lanza ValueError si se detecta un ciclo.
        """
        in_deg = dict(self._in_degree)
        queue = deque([n for n in in_deg if in_deg[n] == 0])
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for neighbor in self._adj[node]:
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)
        if visited != len(self._nodes):
            raise ValueError("Ciclo detectado en el grafo de dependencias")
        return False  # ✅ CORREGIDO: retorno explícito para coincidir con type hint

    def get_task_status(self, task_id: str) -> Optional[str]:
        """Retorna el estado de una tarea específica."""
        node = self._nodes.get(task_id)
        return node.status if node else None

    def get_pending_tasks(self) -> List[str]:
        """Retorna IDs de tareas pendientes (no completadas)."""
        return [tid for tid, node in self._nodes.items() if node.status != "completed"]

    def _max_depth_from(self, node_id: str, visited: set) -> int:
        """Profundidad máxima de la cadena de dependientes desde node_id (DFS)."""
        if node_id in visited:
            return 0
        visited.add(node_id)
        children = self._adj.get(node_id, [])
        if not children:
            return 1
        return 1 + max(self._max_depth_from(c, visited) for c in children)

    def get_max_depth(self) -> int:
        """
        ✅ FIX AUDITORÍA: profundidad máxima real del grafo (cadena más larga
        de dependientes), reusando la misma lógica de _detect_risky_chains().
        Antes, planner.py calculaba su propio "max_depth" contando la
        CANTIDAD de dependencias directas de un nodo (len(deps)) — un número
        distinto y mal nombrado, no la profundidad real de la cadena.
        """
        if not self._nodes:
            return 0
        return max(self._max_depth_from(tid, set()) for tid in self._nodes)

    def _detect_risky_chains(self) -> List[str]:
        """
        [C2] Detecta cadenas riesgosas en el grafo.
        Criterios: profundidad >= 5 nodos O costo > 100 en metadata.
        Marca metadata["security_risk"] = True en nodos afectados.
        Solo actúa si len(nodes) > 10 (skip en grafos pequeños).
        """
        if len(self._nodes) <= 10:
            return []
        try:
            risky: List[str] = []
            DEPTH_THRESHOLD = 5
            COST_THRESHOLD = 100

            for task_id, node in self._nodes.items():
                depth = self._max_depth_from(task_id, set())
                cost = node.metadata.get("cost", 0)
                if depth >= DEPTH_THRESHOLD or cost > COST_THRESHOLD:
                    node.metadata["security_risk"] = True
                    risky.append(task_id)

            return risky
        except Exception as e:
            logger.warning(f"[DependencyGraph] _detect_risky_chains error: {e}")
            return []

    def reset(self) -> None:
        """Reinicia el grafo a estado inicial."""
        self._nodes.clear()
        self._adj.clear()
        self._rev_adj.clear()
        self._in_degree.clear()
        logger.debug("DependencyGraph reseteado")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el grafo a diccionario para logging/debug."""
        return {
            "nodes": {
                tid: {
                    "status": n.status,
                    "deps": n.dependencies,
                    "dependents": n.dependents
                }
                for tid, n in self._nodes.items()
            },
            "ready": self.get_ready_tasks(),
            "pending": self.get_pending_tasks(),
            "total_tasks": len(self._nodes),
            "completed": sum(1 for n in self._nodes.values() if n.status == "completed")
        }

    def __len__(self) -> int:
        """Número de tareas en el grafo."""
        return len(self._nodes)

    def __repr__(self) -> str:
        return f"DependencyGraph(nodes={len(self._nodes)}, ready={len(self.get_ready_tasks())})"
