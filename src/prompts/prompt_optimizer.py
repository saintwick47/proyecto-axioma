#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Prompt Optimizer (Fase 5)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/prompts/prompt_optimizer.py
# Autor: SaintWick
# Fecha: 2026-04-09
# Propósito: Versionado de prompts, scoring histórico, fallback automático
#            y A/B testing ponderado por agente+tarea.
# ═══════════════════════════════════════════════════════════════
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Umbral mínimo de score para mantener una versión activa
_DEFAULT_QUALITY_THRESHOLD = 0.55
# Ejecuciones consecutivas bajo umbral antes de downgrade
_DOWNGRADE_WINDOW = 3
# Máximo de versiones por (agent, task)
_MAX_VERSIONS = 3


@dataclass
class PromptVersion:
    """Registro de una versión de prompt para un agente+tarea."""
    version_id: str                         # ej: "v1", "v2", "fallback"
    agent_type: str
    task_type: str
    score_history: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    retry_count: int = 0
    usage_count: int = 0
    active: bool = True

    @property
    def avg_score(self) -> float:
        if not self.score_history:
            return 1.0  # sin historial → asumir buena calidad
        return round(sum(self.score_history) / len(self.score_history), 4)

    @property
    def retry_rate(self) -> float:
        if self.usage_count == 0:
            return 0.0
        return round(self.retry_count / self.usage_count, 4)

    @property
    def success_rate(self) -> float:
        if not self.score_history:
            return 1.0
        return round(sum(1 for s in self.score_history if s >= _DEFAULT_QUALITY_THRESHOLD)
                     / len(self.score_history), 4)

    def record(self, score: float, was_retry: bool) -> None:
        self.score_history.append(score)
        self.usage_count += 1
        if was_retry:
            self.retry_count += 1

    def last_n_scores(self, n: int) -> List[float]:
        scores = list(self.score_history)
        return scores[-n:] if len(scores) >= n else scores

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id":   self.version_id,
            "agent_type":   self.agent_type,
            "task_type":    self.task_type,
            "avg_score":    self.avg_score,
            "retry_rate":   self.retry_rate,
            "success_rate": self.success_rate,
            "usage_count":  self.usage_count,
            "active":       self.active,
        }


class PromptOptimizer:
    """
    Gestiona versiones de prompts con scoring histórico, fallback automático
    y A/B testing ponderado.

    Integración:
        optimizer = PromptOptimizer()
        version = optimizer.get_best_version("coder", "code_generation")
        # ... ejecutar con esa versión ...
        optimizer.record_execution("coder", "code_generation", score=0.82, was_retry=False)

    Auto-downgrade:
        Si score < threshold durante _DOWNGRADE_WINDOW ejecuciones consecutivas,
        la versión activa se desactiva y se activa la siguiente disponible.
    """

    def __init__(self, quality_threshold: float = _DEFAULT_QUALITY_THRESHOLD) -> None:
        self.quality_threshold = quality_threshold
        # key: (agent_type, task_type) → List[PromptVersion]
        self._versions: Dict[Tuple[str, str], List[PromptVersion]] = defaultdict(list)
        self._lock = threading.Lock()

    # ── Registro de versiones ────────────────────────────────────────────────

    def register_version(
        self,
        agent_type: str,
        task_type: str,
        version_id: str = "v1",
    ) -> PromptVersion:
        """Registra una nueva versión de prompt para agent+task."""
        key = (agent_type, task_type)
        with self._lock:
            existing = [v.version_id for v in self._versions[key]]
            if version_id in existing:
                return next(v for v in self._versions[key] if v.version_id == version_id)
            if len(self._versions[key]) >= _MAX_VERSIONS:
                logger.warning(
                    f"PromptOptimizer: máximo de versiones alcanzado "
                    f"para {agent_type}/{task_type}"
                )
                return self._versions[key][-1]
            version = PromptVersion(
                version_id=version_id,
                agent_type=agent_type,
                task_type=task_type,
            )
            self._versions[key].append(version)
            logger.debug(f"PromptOptimizer: registrada {agent_type}/{task_type}/{version_id}")
            return version

    def _ensure_default(self, agent_type: str, task_type: str) -> PromptVersion:
        """Garantiza que existe al menos la versión v1."""
        key = (agent_type, task_type)
        if not self._versions[key]:
            return self.register_version(agent_type, task_type, "v1")
        return self._versions[key][0]

    # ── Registro de ejecuciones ──────────────────────────────────────────────

    def record_execution(
        self,
        agent_type: str,
        task_type: str,
        score: float,
        was_retry: bool = False,
        version_id: Optional[str] = None,
    ) -> None:
        """
        Registra el resultado de una ejecución.

        Args:
            agent_type: Tipo de agente
            task_type:  Tipo de tarea
            score:      Score de calidad (0.0-1.0)
            was_retry:  Si fue un reintento
            version_id: Versión usada (usa la activa si None)
        """
        self._ensure_default(agent_type, task_type)
        key = (agent_type, task_type)
        with self._lock:
            versions = self._versions[key]
            if version_id:
                target = next((v for v in versions if v.version_id == version_id), None)
            else:
                target = next((v for v in versions if v.active), None) or versions[0]

            if target is None:
                return

            target.record(score, was_retry)

            # Evaluar si debe downgradearse
            if self.should_downgrade(agent_type, task_type, _check_lock=False):
                self._rotate_version(key)

    def should_downgrade(
        self,
        agent_type: str,
        task_type: str,
        _check_lock: bool = True,
    ) -> bool:
        """
        True si la versión activa tuvo score < threshold en las últimas
        _DOWNGRADE_WINDOW ejecuciones consecutivas.
        """
        key = (agent_type, task_type)

        def _check() -> bool:
            versions = self._versions.get(key, [])
            active = next((v for v in versions if v.active), None)
            if active is None:
                return False
            last = active.last_n_scores(_DOWNGRADE_WINDOW)
            if len(last) < _DOWNGRADE_WINDOW:
                return False
            return all(s < self.quality_threshold for s in last)

        if _check_lock:
            with self._lock:
                return _check()
        return _check()

    def _rotate_version(self, key: Tuple[str, str]) -> None:
        """Desactiva la versión activa y activa la siguiente disponible."""
        versions = self._versions[key]
        active_idx = next((i for i, v in enumerate(versions) if v.active), None)
        if active_idx is None:
            return
        versions[active_idx].active = False
        next_idx = active_idx + 1
        if next_idx < len(versions):
            versions[next_idx].active = True
            logger.warning(
                f"PromptOptimizer: downgrade {key[0]}/{key[1]} → "
                f"{versions[next_idx].version_id} "
                f"(score bajo umbral {self.quality_threshold} por {_DOWNGRADE_WINDOW} ejecuciones)"
            )
        else:
            # No hay siguiente — reactivar la última como fallback
            versions[active_idx].active = True
            logger.warning(
                f"PromptOptimizer: sin versión siguiente para {key[0]}/{key[1]}, "
                "manteniendo versión actual"
            )

    # ── Selección de versión ─────────────────────────────────────────────────

    def get_best_version(self, agent_type: str, task_type: str) -> str:
        """
        Retorna el version_id de la versión activa con mejor avg_score.

        Returns:
            version_id (ej: "v1", "v2", "fallback")
        """
        self._ensure_default(agent_type, task_type)
        key = (agent_type, task_type)
        with self._lock:
            active = [v for v in self._versions[key] if v.active]
            if not active:
                return "v1"
            return max(active, key=lambda v: v.avg_score).version_id

    def get_ab_weight(
        self,
        agent_type: str,
        task_type: str,
        version_id: str,
    ) -> float:
        """
        Peso normalizado de una versión para A/B routing (0.0-1.0).
        Versiones con mejor avg_score reciben más tráfico.

        Returns:
            Peso entre 0.0 y 1.0
        """
        key = (agent_type, task_type)
        with self._lock:
            active = [v for v in self._versions.get(key, []) if v.active]
            if not active:
                return 1.0
            total_score = sum(v.avg_score for v in active) or 1.0
            target = next((v for v in active if v.version_id == version_id), None)
            if target is None:
                return 0.0
            return round(target.avg_score / total_score, 4)

    def select_ab_version(self, agent_type: str, task_type: str) -> str:
        """
        Selecciona una versión para A/B testing usando distribución ponderada.

        Returns:
            version_id seleccionado
        """
        import random
        key = (agent_type, task_type)
        with self._lock:
            active = [v for v in self._versions.get(key, []) if v.active]
            if not active:
                return "v1"
            if len(active) == 1:
                return active[0].version_id
            # Distribución ponderada por avg_score
            weights = [v.avg_score for v in active]
            total = sum(weights) or 1.0
            r = random.random() * total
            cumulative = 0.0
            for v, w in zip(active, weights):
                cumulative += w
                if r <= cumulative:
                    return v.version_id
            return active[-1].version_id

    # ── Estadísticas ─────────────────────────────────────────────────────────

    def get_stats(
        self,
        agent_type: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Estadísticas de versiones. Filtra por agent/task si se especifican.

        Returns:
            Dict con métricas por versión
        """
        with self._lock:
            result: Dict[str, Any] = {}
            for (agent, task), versions in self._versions.items():
                if agent_type and agent != agent_type:
                    continue
                if task_type and task != task_type:
                    continue
                key_str = f"{agent}/{task}"
                result[key_str] = {
                    "versions":      [v.to_dict() for v in versions],
                    "active_version": next(
                        (v.version_id for v in versions if v.active), None
                    ),
                    "needs_downgrade": self.should_downgrade(agent, task, _check_lock=False),
                }
            return result

    def get_active_version_info(
        self, agent_type: str, task_type: str
    ) -> Optional[Dict[str, Any]]:
        """Retorna info de la versión activa o None si no existe."""
        key = (agent_type, task_type)
        with self._lock:
            active = next((v for v in self._versions.get(key, []) if v.active), None)
            return active.to_dict() if active else None


# ── Singleton global ──────────────────────────────────────────────────────────
_optimizer: Optional[PromptOptimizer] = None
_optimizer_lock = threading.Lock()


def get_prompt_optimizer() -> PromptOptimizer:
    """Retorna o crea la instancia singleton de PromptOptimizer."""
    global _optimizer
    with _optimizer_lock:
        if _optimizer is None:
            _optimizer = PromptOptimizer()
        return _optimizer
