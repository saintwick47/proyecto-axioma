#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — quality_detectors.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/core/quality_detectors.py
#
# ✅ v0.6.9v (REFACTOR): capa de DETECCIÓN de calidad extraída de
#   src/core/quality_monitor.py (el original queda intacto como fachada).
#
#   Qué se movió desde quality_monitor.py (sin cambios de comportamiento):
#     - Helpers de logging `_log_quality` / `_log_error`
#     - Enums `AlertLevel` / `QualityDimension`
#     - Dataclasses `QualityScore` / `QualityAlert` / `ParameterRecommendation`
#     - `DegradationDetector`  (moving average, caída brusca, fallos consecutivos)
#     - `ParameterRecommender` (correlación parámetro↔score)
#
#   `QualityMonitor` (la fachada pública + `get_quality_monitor()`) permanece en
#   quality_monitor.py, que re-exporta estos símbolos para no romper la API
#   (p. ej. `from src.core.quality_monitor import QualityScore, AlertLevel`).
#
#   Este módulo NO importa nada de quality_monitor.py → sin ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import statistics
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_quality(op: str, task_type: str = "", agent_role: str = "",
                 score: float = None, alert_level: str = "",
                 success: bool = True, extra: Dict[str, Any] = None):
    """
    Helper para logging de operaciones de quality monitor.

    Args:
        op: Operación realizada (monitor, alert, recommend, etc.)
        task_type: Tipo de tarea monitoreada
        agent_role: Rol del agente evaluado
        score: Score de calidad (0-10)
        alert_level: Nivel de alerta (info, warning, critical)
        success: Si la operación fue exitosa
        extra: Información adicional para el log
    """
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(
                category="QUALITY_MONITOR",
                message=f"{op} {alert_level or 'ok'}",
                level="DEBUG" if success else "WARNING",
                extra={
                    "task_type": task_type,
                    "agent_role": agent_role,
                    "quality_score": score,
                    "alert_level": alert_level,
                    **(extra or {}),
                }
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/quality_detectors.py:_log_quality")


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/quality_detectors.py:_log_error")


# ============================================================================
# ENUMS — Tipado fuerte para calidad
# ============================================================================
class AlertLevel(str, Enum):
    """
    Niveles de alerta de calidad.

    INFO      → Cambios menores, sin acción requerida
    WARNING   → Degradación detectada, monitorear de cerca
    CRITICAL  → Degradación severa, acción inmediata requerida
    """
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class QualityDimension(str, Enum):
    """
    Dimensiones de calidad evaluables.

    ACCURACY       → Precisión de la respuesta
    COMPLETENESS   → Completitud de la información
    RELEVANCE      → Relevancia para la query
    COHERENCE      → Coherencia lógica
    SAFETY         → Seguridad (no contenido dañino)
    FORMAT         → Formato correcto (JSON, código, etc.)
    LATENCY        → Tiempo de respuesta
    """
    ACCURACY = "accuracy"
    COMPLETENESS = "completeness"
    RELEVANCE = "relevance"
    COHERENCE = "coherence"
    SAFETY = "safety"
    FORMAT = "format"
    LATENCY = "latency"


# ============================================================================
# DATACLASSES — Estructuras de datos para calidad
# ============================================================================
@dataclass
class QualityScore:
    """
    Score de calidad con múltiples dimensiones.

    Attributes:
        overall: Score general (0-10)
        accuracy: Precisión (0-10)
        completeness: Completitud (0-10)
        relevance: Relevancia (0-10)
        coherence: Coherencia (0-10)
        safety: Seguridad (0-10)
        format: Formato correcto (0-10)
        latency_score: Score de latencia (0-10, 10 = rápido)
        timestamp: Timestamp de evaluación (UTC)
        metadata: Metadatos adicionales
    """
    overall: float
    accuracy: float = 0.0
    completeness: float = 0.0
    relevance: float = 0.0
    coherence: float = 0.0
    safety: float = 0.0
    format: float = 0.0
    latency_score: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Valida que todos los scores estén en rango 0-10."""
        for attr in ['overall', 'accuracy', 'completeness', 'relevance',
                     'coherence', 'safety', 'format', 'latency_score']:
            value = getattr(self, attr)
            if not (0.0 <= value <= 10.0):
                setattr(self, attr, max(0.0, min(10.0, value)))

    @property
    def is_acceptable(self) -> bool:
        """True si el score overall es >= 6.0 (threshold configurable)."""
        return self.overall >= 6.0

    @property
    def is_excellent(self) -> bool:
        """True si el score overall es >= 8.0."""
        return self.overall >= 8.0

    @property
    def is_poor(self) -> bool:
        """True si el score overall es < 5.0."""
        return self.overall < 5.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "overall": round(self.overall, 2),
            "accuracy": round(self.accuracy, 2),
            "completeness": round(self.completeness, 2),
            "relevance": round(self.relevance, 2),
            "coherence": round(self.coherence, 2),
            "safety": round(self.safety, 2),
            "format": round(self.format, 2),
            "latency_score": round(self.latency_score, 2),
            "timestamp": self.timestamp.isoformat(),
            "is_acceptable": self.is_acceptable,
            "is_excellent": self.is_excellent,
            "is_poor": self.is_poor,
            **self.metadata,
        }

    def __repr__(self) -> str:
        return f"QualityScore(overall={self.overall:.2f}, acceptable={self.is_acceptable})"


@dataclass
class QualityAlert:
    """
    Alerta de degradación de calidad.

    Attributes:
        id: UUID único de la alerta
        level: Nivel de alerta (INFO/WARNING/CRITICAL)
        task_type: Tipo de tarea afectada
        agent_role: Rol del agente afectado
        previous_score: Score anterior a la degradación
        current_score: Score actual (degradado)
        degradation_percent: Porcentaje de degradación
        dimension: Dimensión de calidad degradada
        timestamp: Timestamp de la alerta (UTC)
        message: Mensaje descriptivo de la alerta
        recommended_action: Acción recomendada
        acknowledged: Si la alerta fue reconocida
    """
    id: str
    level: AlertLevel
    task_type: str
    agent_role: str
    previous_score: float
    current_score: float
    degradation_percent: float
    dimension: QualityDimension
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message: str = ""
    recommended_action: str = ""
    acknowledged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "id": self.id,
            "level": self.level.value,
            "task_type": self.task_type,
            "agent_role": self.agent_role,
            "previous_score": round(self.previous_score, 2),
            "current_score": round(self.current_score, 2),
            "degradation_percent": round(self.degradation_percent, 2),
            "dimension": self.dimension.value,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "recommended_action": self.recommended_action,
            "acknowledged": self.acknowledged,
        }

    def __repr__(self) -> str:
        return f"QualityAlert({self.level.value} {self.task_type} {self.degradation_percent:.1f}%↓)"


@dataclass
class ParameterRecommendation:
    """
    Recomendación de ajuste de parámetros.

    Attributes:
        parameter: Nombre del parámetro a ajustar
        current_value: Valor actual
        recommended_value: Valor recomendado
        reason: Razón de la recomendación
        confidence: Confianza en la recomendación (0-1)
        expected_improvement: Mejora esperada en calidad
    """
    parameter: str
    current_value: float
    recommended_value: float
    reason: str
    confidence: float = 0.0
    expected_improvement: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "parameter": self.parameter,
            "current_value": round(self.current_value, 4),
            "recommended_value": round(self.recommended_value, 4),
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "expected_improvement": round(self.expected_improvement, 2),
        }


# ============================================================================
# DETECTOR DE DEGRADACIÓN
# ============================================================================
class DegradationDetector:
    """
    Detecta degradación de calidad en tiempo real.

    Estrategias de detección:
    - Moving average comparison (ventana deslizante)
    - Percentil-based threshold (p5, p10)
    - Consecutive failures count
    - Sudden drop detection (> X% en una iteración)

    Thread-safe para uso concurrente.
    """

    def __init__(
        self,
        window_size: int = 20,
        degradation_threshold_percent: float = 20.0,
        consecutive_failures_threshold: int = 3,
        sudden_drop_threshold_percent: float = 30.0,
    ):
        """
        Inicializa detector de degradación.

        Args:
            window_size: Tamaño de ventana para moving average
            degradation_threshold_percent: % de degradación para alerta
            consecutive_failures_threshold: Fallos consecutivos para alerta
            sudden_drop_threshold_percent: % de caída brusca para alerta
        """
        self.window_size = window_size
        self.degradation_threshold = degradation_threshold_percent
        self.consecutive_failures_threshold = consecutive_failures_threshold
        self.sudden_drop_threshold = sudden_drop_threshold_percent

        # Historial de scores por tarea/agente
        self._score_history: Dict[str, deque] = {}  # key -> deque de scores
        self._consecutive_failures: Dict[str, int] = {}  # key -> count
        self._last_score: Dict[str, float] = {}  # key -> last score

        self._lock = asyncio.Lock()

        logger.debug(f"DegradationDetector initialized: window={window_size}")

    def _get_key(self, task_type: str, agent_role: str) -> str:
        """Genera key única para tarea/agente."""
        return f"{task_type}:{agent_role}"

    async def record_score(self, task_type: str, agent_role: str, score: float) -> Optional[QualityAlert]:
        """
        Registra score y detecta degradación.

        Args:
            task_type: Tipo de tarea
            agent_role: Rol del agente
            score: Score de calidad (0-10)

        Returns:
            QualityAlert si se detectó degradación, None en caso contrario
        """
        key = self._get_key(task_type, agent_role)
        # ✅ FIX FASE 4: Acumular alertas en lista para no sobrescribir CRITICAL con WARNING
        alerts_detected = []

        async with self._lock:
            # Inicializar historial si no existe
            if key not in self._score_history:
                self._score_history[key] = deque(maxlen=self.window_size)
                self._consecutive_failures[key] = 0
                self._last_score[key] = score
                self._score_history[key].append(score)
                return None

            # Agregar score al historial
            self._score_history[key].append(score)
            history = list(self._score_history[key])

            # Calcular moving average
            if len(history) >= 3:  # Mínimo 3 scores para comparar
                avg = statistics.mean(history[:-1])  # Excluir score actual
                current = score

                # Detectar degradación por moving average
                if avg > 0:
                    degradation = ((avg - current) / avg) * 100

                    if degradation >= self.degradation_threshold:
                        alerts_detected.append(QualityAlert(
                            id=str(uuid.uuid4()),
                            level=AlertLevel.WARNING if degradation < 40 else AlertLevel.CRITICAL,
                            task_type=task_type,
                            agent_role=agent_role,
                            previous_score=avg,
                            current_score=current,
                            degradation_percent=degradation,
                            dimension=QualityDimension.ACCURACY,
                            message=f"Degradación detectada: {degradation:.1f}% below average",
                            recommended_action=self._get_recommendation(degradation, task_type),
                        ))

                # Detectar caída brusca
                last = self._last_score.get(key, avg)
                # ✅ FIX FASE 4: Épsilon para prevenir división inestable/Overflow
                if last > 1e-6:
                    sudden_drop = ((last - current) / last) * 100
                    if sudden_drop >= self.sudden_drop_threshold:
                        alerts_detected.append(QualityAlert(
                            id=str(uuid.uuid4()),
                            level=AlertLevel.CRITICAL,
                            task_type=task_type,
                            agent_role=agent_role,
                            previous_score=last,
                            current_score=current,
                            degradation_percent=sudden_drop,
                            dimension=QualityDimension.ACCURACY,
                            message=f"Caída brusca: {sudden_drop:.1f}% en una iteración",
                            recommended_action="Revisar cambios recientes en prompts/parámetros",
                        ))

            # Trackear fallos consecutivos
            if score < 5.0:  # Score pobre
                self._consecutive_failures[key] += 1
                if self._consecutive_failures[key] >= self.consecutive_failures_threshold:
                    alerts_detected.append(QualityAlert(
                        id=str(uuid.uuid4()),
                        level=AlertLevel.WARNING,
                        task_type=task_type,
                        agent_role=agent_role,
                        previous_score=self._last_score.get(key, 0),
                        current_score=score,
                        degradation_percent=0,
                        dimension=QualityDimension.ACCURACY,
                        message=f"{self._consecutive_failures[key]} fallos consecutivos",
                        recommended_action="Considerar retry con diferentes parámetros",
                    ))
            else:
                self._consecutive_failures[key] = 0

            # Guardar último score
            self._last_score[key] = score

        # ✅ FIX FASE 4: Si hay múltiples alertas, retornar la de mayor severidad
        if alerts_detected:
            severity_order = [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.CRITICAL]
            alerts_detected.sort(key=lambda a: severity_order.index(a.level), reverse=True)
            top_alert = alerts_detected[0]

            _log_quality(
                "degradation_detected",
                task_type=task_type,
                agent_role=agent_role,
                score=score,
                alert_level=top_alert.level.value,
                success=True,
                extra={"degradation": top_alert.degradation_percent},
            )
            return top_alert

        return None

    def _get_recommendation(self, degradation: float, task_type: str) -> str:
        """Genera recomendación basada en degradación."""
        if degradation >= 50:
            return "URGENTE: Revisar prompts, modelos y parámetros inmediatamente"
        elif degradation >= 30:
            return "Ajustar temperature, timeout o cambiar modelo"
        else:
            return "Monitorear de cerca, considerar ajustes menores"

    def get_stats(self, task_type: Optional[str] = None, agent_role: Optional[str] = None) -> Dict[str, Any]:
        """
        Obtiene estadísticas del detector.

        Args:
            task_type: Filtrar por tipo de tarea (None = todos)
            agent_role: Filtrar por rol de agente (None = todos)

        Returns:
            Dict con estadísticas
        """
        if task_type and agent_role:
            key = self._get_key(task_type, agent_role)
            if key in self._score_history:
                history = list(self._score_history[key])
                return {
                    "task_type": task_type,
                    "agent_role": agent_role,
                    "samples": len(history),
                    "avg_score": round(statistics.mean(history), 2) if history else 0,
                    "min_score": round(min(history), 2) if history else 0,
                    "max_score": round(max(history), 2) if history else 0,
                    "std_dev": round(statistics.stdev(history), 2) if len(history) > 1 else 0,
                    "consecutive_failures": self._consecutive_failures.get(key, 0),
                }

        # Estadísticas globales
        all_scores = []
        for history in self._score_history.values():
            all_scores.extend(history)

        return {
            "total_tracked": len(self._score_history),
            "total_samples": len(all_scores),
            "avg_score": round(statistics.mean(all_scores), 2) if all_scores else 0,
            "consecutive_failures_total": sum(self._consecutive_failures.values()),
        }


# ============================================================================
# RECOMENDADOR DE PARÁMETROS
# ============================================================================
class ParameterRecommender:
    """
    Recomienda ajustes de parámetros basados en patrones de calidad.

    Parámetros recomendables:
    - temperature: Creatividad vs consistencia
    - timeout: Tiempo de respuesta
    - max_tokens: Longitud de respuesta
    - similarity_threshold: Threshold de búsqueda semántica

    Estrategias:
    - Pattern matching con historial
    - Correlación score-parámetro
    - A/B testing simulation
    """

    def __init__(self, min_confidence: float = 0.6):
        """
        Inicializa recomendador.

        Args:
            min_confidence: Confianza mínima para recomendación
        """
        self.min_confidence = min_confidence

        # ✅ FIX FASE 4: Usar deque para evicción O(1) en vez de List con pop(0)
        # Historial de parámetros y scores: param -> deque of (value, score)
        self._param_history: Dict[str, deque] = {}

        self._lock = asyncio.Lock()

        logger.debug(f"ParameterRecommender initialized: min_confidence={min_confidence}")

    async def record_parameter(self, parameter: str, value: float, score: float) -> None:
        """
        Registra parámetro usado y score resultante.

        Args:
            parameter: Nombre del parámetro
            value: Valor usado
            score: Score de calidad resultante
        """
        async with self._lock:
            if parameter not in self._param_history:
                # maxlen garantiza que la memoria no crezca infinitamente y elimina pop(0) O(N)
                self._param_history[parameter] = deque(maxlen=100)

            self._param_history[parameter].append((value, score))

    async def get_recommendation(
        self,
        parameter: str,
        current_value: float,
        current_score: float,
    ) -> Optional[ParameterRecommendation]:
        """
        Obtiene recomendación para un parámetro.

        Args:
            parameter: Nombre del parámetro
            current_value: Valor actual
            current_score: Score actual

        Returns:
            ParameterRecommendation o None si no hay recomendación
        """
        async with self._lock:
            history = self._param_history.get(parameter, [])

            if len(history) < 5:  # Mínimo data para recomendar
                return None

            # Analizar correlación
            values = [v for v, s in history]
            scores = [s for v, s in history]

            # Encontrar valor óptimo histórico
            best_idx = scores.index(max(scores))
            best_value = values[best_idx]
            best_score = scores[best_idx]

            # Calcular confianza basada en consistencia
            if len(scores) > 1:
                score_std = statistics.stdev(scores)
                confidence = max(0, 1 - (score_std / 10))  # Normalizado 0-1
            else:
                confidence = 0.5

            if confidence < self.min_confidence:
                return None

            # Calcular mejora esperada
            expected_improvement = max(0, best_score - current_score)

            if expected_improvement < 0.5:  # Mejora mínima para recomendar
                return None

            return ParameterRecommendation(
                parameter=parameter,
                current_value=current_value,
                recommended_value=best_value,
                reason=f"Históricamente {best_value:.3f} produjo score {best_score:.2f}",
                confidence=confidence,
                expected_improvement=expected_improvement,
            )

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del recomendador."""
        return {
            "tracked_parameters": len(self._param_history),
            "total_records": sum(len(h) for h in self._param_history.values()),
            "parameters": list(self._param_history.keys()),
        }


__all__ = [
    "AlertLevel",
    "QualityDimension",
    "QualityScore",
    "QualityAlert",
    "ParameterRecommendation",
    "DegradationDetector",
    "ParameterRecommender",
]
