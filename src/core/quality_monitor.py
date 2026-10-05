#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# quality_monitor.py - Monitor de Calidad en Tiempo Real
# Ruta: /ruta/a/axioma/src/core/quality_monitor.py
# Autor: SaintWick
# Versión: 0.7.1
#
# Monitorea la calidad de respuestas, detecta degradación,
# genera alertas tempranas y recomienda ajustes de parámetros.
# Orquesta el DegradationDetector y ParameterRecommender con
# métricas continuas y manejo de alertas thread-safe.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import threading
import time
import statistics
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
from collections import OrderedDict, deque

# ✅ v0.6.9v (REFACTOR 50/50): capa de DETECCIÓN (helpers de logging,
# enums, dataclasses y detectores) movida a src/core/quality_detectors.py.
# Se re-exporta para no romper la API pública de este módulo
# (p. ej. `from src.core.quality_monitor import QualityScore, AlertLevel`).
from .quality_detectors import (  # noqa: E402,F401
    AlertLevel,
    QualityDimension,
    QualityScore,
    QualityAlert,
    ParameterRecommendation,
    DegradationDetector,
    ParameterRecommender,
    _log_quality,
    _log_error,
)

logger = logging.getLogger(__name__)


# ============================================================================
# CLASE PRINCIPAL: QualityMonitor
# ============================================================================
class QualityMonitor:
    """
    Monitor central de calidad en tiempo real.

    Orquesta:
    - DegradationDetector: Detecta caídas de calidad
    - ParameterRecommender: Sugiere ajustes
    - Alert system: Notifica degradaciones
    - Metrics tracking: Métricas continuas

    Integración con otros componentes:
    - FeedbackLoop: Para cerrar ciclo de optimización
    - AgentLoop: Para evaluación continua
    - Dispatcher: Para monitoreo global

    Example:
    >>> monitor = QualityMonitor()
    >>> await monitor.record_score(task_type, agent_role, score)
    >>> alert = await monitor.check_degradation()
    >>> recommendation = await monitor.get_recommendation("temperature", 0.7, 6.5)
    """

    def __init__(
        self,
        degradation_threshold_percent: float = 20.0,
        alert_cooldown_seconds: int = 300,
        metrics_max_entries: int = 1000,
    ):
        """
        Inicializa monitor de calidad.

        Args:
            degradation_threshold_percent: % de degradación para alerta
            alert_cooldown_seconds: Tiempo mínimo entre alertas iguales
            metrics_max_entries: Máximo de entradas en métricas
        """
        start = time.perf_counter()

        self.degradation_threshold = degradation_threshold_percent
        self.alert_cooldown = timedelta(seconds=alert_cooldown_seconds)
        self.metrics_max_entries = metrics_max_entries

        # Componentes
        self.detector = DegradationDetector(
            degradation_threshold_percent=degradation_threshold_percent,
        )
        self.recommender = ParameterRecommender()

        # Almacenamiento de alertas
        self._alerts: OrderedDict[str, QualityAlert] = OrderedDict()
        self._last_alert_time: Dict[str, datetime] = {}  # key -> last alert time

        # Métricas
        self._scores: deque = deque(maxlen=metrics_max_entries)
        self._total_evaluations = 0
        self._total_alerts = 0

        self._lock = asyncio.Lock()

        duration = time.perf_counter() - start
        logger.info(f"QualityMonitor initialized in {duration:.4f}s")
        _log_quality("init", success=True, extra={"duration": round(duration, 4)})

    async def record_score(
        self,
        task_type: str,
        agent_role: str,
        score: QualityScore,
        parameters: Optional[Dict[str, float]] = None,
    ) -> Optional[QualityAlert]:
        """
        Registra score de calidad y detecta degradación.

        Args:
            task_type: Tipo de tarea
            agent_role: Rol del agente
            score: Score de calidad
            parameters: Parámetros usados (para recomendaciones)

        Returns:
            QualityAlert si se detectó degradación, None en caso contrario
        """
        async with self._lock:
            self._total_evaluations += 1
            self._scores.append({
                "task_type": task_type,
                "agent_role": agent_role,
                "score": score.overall,
                "timestamp": score.timestamp,
            })

        # Detectar degradación
        alert = await self.detector.record_score(task_type, agent_role, score.overall)

        # ✅ FIX FASE 4: Registrar parámetros SIEMPRE para aprender de éxitos y fracasos.
        # Antes: `if parameters and alert:` solo aprendía de fracasos, sesgando el dataset.
        if parameters:
            for param, value in parameters.items():
                await self.recommender.record_parameter(param, value, score.overall)

        # Guardar alerta si existe
        if alert:
            async with self._lock:
                # Check cooldown
                key = f"{alert.task_type}:{alert.agent_role}:{alert.dimension.value}"
                last_alert = self._last_alert_time.get(key)

                if last_alert and (datetime.now(timezone.utc) - last_alert) < self.alert_cooldown:
                    logger.debug(f"Alert cooldown active for {key}")
                    return None

                self._alerts[alert.id] = alert
                self._last_alert_time[key] = datetime.now(timezone.utc)
                self._total_alerts += 1

                # Limpiar alertas antiguas
                while len(self._alerts) > 100:
                    self._alerts.popitem(last=False)

            _log_quality(
                "alert_generated",
                task_type=task_type,
                agent_role=agent_role,
                score=score.overall,
                alert_level=alert.level.value,
                success=True,
            )

        return alert

    async def get_recommendation(
        self,
        parameter: str,
        current_value: float,
        current_score: float,
    ) -> Optional[ParameterRecommendation]:
        """
        Obtiene recomendación de ajuste de parámetro.

        Args:
            parameter: Nombre del parámetro
            current_value: Valor actual
            current_score: Score actual

        Returns:
            ParameterRecommendation o None
        """
        return await self.recommender.get_recommendation(
            parameter=parameter,
            current_value=current_value,
            current_score=current_score,
        )

    # ✅ FIX FASE 4: Async y lock para prevenir RuntimeError en concurrencia
    async def get_active_alerts(
        self,
        level: Optional[AlertLevel] = None,
        task_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[QualityAlert]:
        """
        Obtiene alertas activas.

        Args:
            level: Filtrar por nivel (None = todos)
            task_type: Filtrar por tipo de tarea
            limit: Máximo de alertas a retornar

        Returns:
            Lista de QualityAlert ordenadas por timestamp (más reciente primero)
        """
        async with self._lock:
            alerts = list(self._alerts.values())

        if level:
            alerts = [a for a in alerts if a.level == level]

        if task_type:
            alerts = [a for a in alerts if a.task_type == task_type]

        # Ordenar por timestamp descendente
        alerts.sort(key=lambda a: a.timestamp, reverse=True)

        return alerts[:limit]

    # ✅ FIX FASE 4: Async y lock para prevenir RuntimeError en concurrencia
    async def acknowledge_alert(self, alert_id: str) -> bool:
        """
        Reconoce una alerta.

        Args:
            alert_id: ID de la alerta

        Returns:
            True si se reconoció, False si no existe
        """
        async with self._lock:
            if alert_id in self._alerts:
                self._alerts[alert_id].acknowledged = True
                logger.info(f"Alert acknowledged: {alert_id}")
                return True
            return False

    def get_quality_metrics(
        self,
        task_type: Optional[str] = None,
        agent_role: Optional[str] = None,
        window_minutes: int = 60,
    ) -> Dict[str, Any]:
        """
        Obtiene métricas de calidad.

        Args:
            task_type: Filtrar por tipo de tarea
            agent_role: Filtrar por rol de agente
            window_minutes: Ventana de tiempo en minutos

        Returns:
            Dict con métricas de calidad
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

        # Filtrar scores recientes
        recent = [
            s for s in self._scores
            if s["timestamp"] > cutoff
        ]

        if task_type:
            recent = [s for s in recent if s["task_type"] == task_type]

        if agent_role:
            recent = [s for s in recent if s["agent_role"] == agent_role]

        if not recent:
            return {
                "samples": 0,
                "avg_score": 0.0,
                "min_score": 0.0,
                "max_score": 0.0,
                "acceptance_rate": 0.0,
                "alert_count": 0,
            }

        scores = [s["score"] for s in recent]
        acceptable = sum(1 for s in scores if s >= 6.0)

        return {
            "samples": len(recent),
            "avg_score": round(statistics.mean(scores), 2),
            "min_score": round(min(scores), 2),
            "max_score": round(max(scores), 2),
            "std_dev": round(statistics.stdev(scores), 2) if len(scores) > 1 else 0,
            "acceptance_rate": round(acceptable / len(scores), 4),
            "alert_count": self._total_alerts,
            "total_evaluations": self._total_evaluations,
            "window_minutes": window_minutes,
        }

    def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas completas del monitor.

        Returns:
            Dict con estadísticas de todos los componentes
        """
        # Single pass sobre _alerts en lugar de dos iteraciones separadas
        _acknowledged = sum(1 for a in self._alerts.values() if a.acknowledged)
        _active = len(self._alerts) - _acknowledged
        return {
            "total_evaluations": self._total_evaluations,
            "total_alerts": self._total_alerts,
            "active_alerts": _active,
            "acknowledged_alerts": _acknowledged,
            "quality_metrics": self.get_quality_metrics(),
            "detector_stats": self.detector.get_stats(),
            "recommender_stats": self.recommender.get_stats(),
        }

    async def cleanup_old_alerts(self, max_age_hours: int = 24) -> int:
        """
        Limpia alertas antiguas.

        Args:
            max_age_hours: Edad máxima en horas

        Returns:
            Número de alertas eliminadas
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        removed = 0

        async with self._lock:
            to_remove = [
                aid for aid, alert in self._alerts.items()
                if alert.timestamp < cutoff and alert.acknowledged
            ]

            for aid in to_remove:
                del self._alerts[aid]
                removed += 1

        if removed > 0:
            logger.info(f"Cleaned up {removed} old alerts")

        return removed


# ============================================================================
# SINGLETON GLOBAL
# ============================================================================
_monitor_instance: Optional[QualityMonitor] = None
# ✅ FIX: asyncio.Lock() a nivel módulo → RuntimeError en Python 3.10+ al importar
# threading.Lock() es seguro en cualquier contexto; __init__ de QualityMonitor no es async.
_monitor_lock = threading.Lock()


async def get_quality_monitor(
    degradation_threshold_percent: float = 20.0,
    alert_cooldown_seconds: int = 300,
    metrics_max_entries: int = 1000,
) -> QualityMonitor:
    """
    Obtiene instancia singleton del QualityMonitor.

    Args:
        degradation_threshold_percent: % de degradación para alerta
        alert_cooldown_seconds: Tiempo mínimo entre alertas
        metrics_max_entries: Máximo de entradas en métricas

    Returns:
        Instancia de QualityMonitor
    """
    global _monitor_instance

    if _monitor_instance is None:
        with _monitor_lock:  # threading.Lock — no necesita await
            if _monitor_instance is None:
                _monitor_instance = QualityMonitor(
                    degradation_threshold_percent=degradation_threshold_percent,
                    alert_cooldown_seconds=alert_cooldown_seconds,
                    metrics_max_entries=metrics_max_entries,
                )

    return _monitor_instance


async def reset_quality_monitor() -> None:
    """Reinicia el singleton del QualityMonitor."""
    global _monitor_instance

    if _monitor_instance:
        _monitor_instance = None
