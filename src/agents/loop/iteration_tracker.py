#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Iteration Tracker para Agent Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/iteration_tracker.py
# Autor: SaintWick
# Fecha: 2026-04-01
# Propósito: Tracking de iteraciones y métricas del loop
# Versión: 0.3.0 (Fase 1, Semana 2)
# ═══════════════════════════════════════════════════════════════

import logging
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class IterationRecord:
    """Registro de una iteración individual del loop."""
    iteration_number: int
    success: bool
    duration_ms: float
    timestamp: datetime
    reflection_score: Optional[float] = None
    reflection_critique: Optional[str] = None
    phase_durations: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "iteration_number": self.iteration_number,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 2),
            "timestamp": self.timestamp.isoformat(),
            "reflection_score": self.reflection_score,
            "reflection_critique": self.reflection_critique[:200] if self.reflection_critique else None,
            "phase_durations": {k: round(v, 2) for k, v in self.phase_durations.items()},
            "metadata": self.metadata,
        }


class IterationTracker:
    """
    Trackea iteraciones del Agent Loop.

    Responsabilidades:
    - Contar iteraciones completadas
    - Registrar duración de cada iteración
    - Almacenar resultados de reflexión
    - Calcular métricas agregadas
    - Detectar patrones de fallo
    """

    def __init__(self, max_iterations: int = 10):
        self.max_iterations = max_iterations
        self._current_iteration = 0
        self._start_time: Optional[float] = None
        self._records: List[IterationRecord] = []
        self._consecutive_failures = 0
        self._consecutive_successes = 0

        logger.debug(f"IterationTracker initialized: max_iterations={max_iterations}")

    @property
    def current_iteration(self) -> int:
        """Retorna iteración actual (1-based)."""
        return self._current_iteration

    @property
    def start_time(self) -> Optional[float]:
        """Retorna timestamp de inicio."""
        return self._start_time

    @property
    def total_iterations(self) -> int:
        """Retorna total de iteraciones completadas."""
        return len(self._records)

    @property
    def consecutive_failures(self) -> int:
        """Retorna fallos consecutivos actuales."""
        return self._consecutive_failures

    @property
    def consecutive_successes(self) -> int:
        """Retorna éxitos consecutivos actuales."""
        return self._consecutive_successes

    # ═══════════════════════════════════════════════════════════
    # CONTROL DEL LOOP
    # ═══════════════════════════════════════════════════════════

    def start(self):
        """Inicia el tracking del loop."""
        self._start_time = time.time()
        self._current_iteration = 0
        self._records.clear()
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        logger.debug("IterationTracker started")

    def next_iteration(self) -> int:
        """
        Avanza a la siguiente iteración.

        Returns:
            int: Número de la nueva iteración (1-based)
        """
        self._current_iteration += 1
        logger.debug(f"Iteration {self._current_iteration}/{self.max_iterations}")
        return self._current_iteration

    def has_reached_max(self) -> bool:
        """Verifica si se alcanzó el máximo de iteraciones."""
        return self._current_iteration >= self.max_iterations

    # ═══════════════════════════════════════════════════════════
    # REGISTRO DE ITERACIONES
    # ═══════════════════════════════════════════════════════════

    def record_iteration(
        self,
        success: bool,
        duration_ms: float,
        reflection_score: Optional[float] = None,
        reflection_critique: Optional[str] = None,
        phase_durations: Optional[Dict[str, float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IterationRecord:
        """
        Registra una iteración completada.

        Args:
            success: Si la iteración fue exitosa
            duration_ms: Duración en milisegundos
            reflection_score: Score de reflexión (0-1)
            reflection_critique: Crítica de reflexión
            phase_durations: Duración por fase del loop
            metadata: Metadata adicional

        Returns:
            IterationRecord: Registro creado
        """
        record = IterationRecord(
            iteration_number=self._current_iteration,
            success=success,
            duration_ms=duration_ms,
            timestamp=datetime.now(timezone.utc),
            reflection_score=reflection_score,
            reflection_critique=reflection_critique,
            phase_durations=phase_durations or {},
            metadata=metadata or {},
        )

        self._records.append(record)

        # Actualizar contadores consecutivos
        if success:
            self._consecutive_successes += 1
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._consecutive_successes = 0

        logger.debug(
            f"Iteration {self._current_iteration} recorded: "
            f"success={success}, duration={duration_ms:.2f}ms"
        )

        return record

    # ═══════════════════════════════════════════════════════════
    # MÉTRICAS Y ANÁLISIS
    # ═══════════════════════════════════════════════════════════

    def get_average_duration(self) -> float:
        """Retorna duración promedio de iteraciones."""
        if not self._records:
            return 0.0
        total = sum(r.duration_ms for r in self._records)
        return total / len(self._records)

    def get_success_rate(self) -> float:
        """Retorna tasa de éxito."""
        if not self._records:
            return 0.0
        successes = sum(1 for r in self._records if r.success)
        return successes / len(self._records)

    def get_average_reflection_score(self) -> Optional[float]:
        """Retorna score promedio de reflexión."""
        scores = [r.reflection_score for r in self._records if r.reflection_score is not None]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def get_total_duration(self) -> float:
        """Retorna duración total del loop."""
        if not self._start_time:
            return 0.0
        return (time.time() - self._start_time) * 1000

    def get_phase_totals(self) -> Dict[str, float]:
        """Retorna duración total por fase."""
        totals: Dict[str, float] = {}
        for record in self._records:
            for phase, duration in record.phase_durations.items():
                totals[phase] = totals.get(phase, 0.0) + duration
        return totals

    def detect_degradation(self, window: int = 3) -> bool:
        """
        Detecta degradación de calidad en ventanas recientes.

        Args:
            window: Tamaño de ventana para análisis

        Returns:
            bool: True si hay degradación detectada
        """
        if len(self._records) < window:
            return False

        recent = self._records[-window:]
        scores = [r.reflection_score for r in recent if r.reflection_score is not None]

        if len(scores) < 2:
            return False

        # Verificar tendencia descendente
        for i in range(1, len(scores)):
            if scores[i] >= scores[i-1]:
                return False

        return True

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna métricas completas del tracker."""
        return {
            "current_iteration": self._current_iteration,
            "total_iterations": self.total_iterations,
            "max_iterations": self.max_iterations,
            "success_rate": round(self.get_success_rate(), 4),
            "average_duration_ms": round(self.get_average_duration(), 2),
            "total_duration_ms": round(self.get_total_duration(), 2),
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "average_reflection_score": self.get_average_reflection_score(),
            "phase_totals": {k: round(v, 2) for k, v in self.get_phase_totals().items()},
            "degradation_detected": self.detect_degradation(),
        }

    def get_iteration_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Retona historial de iteraciones recientes.

        Args:
            limit: Máximo de iteraciones a retornar

        Returns:
            Lista de registros serializados
        """
        return [r.to_dict() for r in self._records[-limit:]]

    # ═══════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════

    def reset(self):
        """Reinicia el tracker a estado inicial."""
        self._current_iteration = 0
        self._start_time = None
        self._records.clear()
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        logger.debug("IterationTracker reset")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa el estado completo."""
        return {
            "max_iterations": self.max_iterations,
            "current_iteration": self._current_iteration,
            "start_time": self._start_time,
            "records_count": len(self._records),
            "metrics": self.get_metrics(),
        }
