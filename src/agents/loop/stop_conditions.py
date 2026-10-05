#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Stop Conditions para Agent Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/stop_conditions.py
# Autor: SaintWick
# Fecha: 2026-04-01
# Propósito: Definir y verificar condiciones de parada del loop
# Versión: 0.3.0 (Fase 1, Semana 2)
# ═══════════════════════════════════════════════════════════════

import logging
import time
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field

# ✅ FIX: Importar desde types.py (NO definir StopReason localmente)
from .types import StopReason

logger = logging.getLogger(__name__)


@dataclass
class StopCondition:
    """Define una condición de parada custom."""
    name: str
    check_fn: Callable[[], bool]
    reason: StopReason
    priority: int = 0
    enabled: bool = True
    description: str = ""

    def evaluate(self) -> bool:
        """Evalúa si la condición se activó."""
        if not self.enabled:
            return False
        try:
            return self.check_fn()
        except Exception as e:
            logger.error(f"StopCondition {self.name} evaluation error: {e}")
            return False


class StopConditions:
    """
    Gestiona condiciones de parada del Agent Loop.

    Responsabilidades:
    - Verificar máximo de iteraciones
    - Verificar timeout global
    - Verificar condiciones custom
    - Determinar razón de parada
    - Prevenir loops infinitos
    """

    DEFAULT_MAX_ITERATIONS = 10
    DEFAULT_GLOBAL_TIMEOUT = 180
    DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

    def __init__(
        self,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        global_timeout: int = DEFAULT_GLOBAL_TIMEOUT,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ):
        self.max_iterations = max_iterations
        self.global_timeout = global_timeout
        self.max_consecutive_failures = max_consecutive_failures

        self._start_time: Optional[float] = None
        self._custom_conditions: List[StopCondition] = []
        self._stop_reason: Optional[StopReason] = None
        self._enabled = True

        logger.debug(
            f"StopConditions initialized: max_iter={max_iterations}, "
            f"timeout={global_timeout}s, max_failures={max_consecutive_failures}"
        )

    @property
    def stop_reason(self) -> Optional[StopReason]:
        """Retorna razón de parada si está activada."""
        return self._stop_reason

    @property
    def is_stopped(self) -> bool:
        """Verifica si el loop debe detenerse."""
        return self._stop_reason is not None

    # ═══════════════════════════════════════════════════════════
    # CONTROL DEL LOOP
    # ═══════════════════════════════════════════════════════════

    def start(self):
        """Inicia el tracking de condiciones."""
        self._start_time = time.time()
        self._stop_reason = None
        self._enabled = True
        logger.debug("StopConditions started")

    def stop(self, reason: StopReason):
        """
        Detiene el loop con razón específica.

        Args:
            reason: Razón de parada
        """
        self._stop_reason = reason
        logger.info(f"StopConditions: loop stopped - reason={reason.value}")

    def reset(self):
        """Reinicia las condiciones."""
        self._start_time = None
        self._stop_reason = None
        self._enabled = True
        logger.debug("StopConditions reset")

    # ═══════════════════════════════════════════════════════════
    # VERIFICACIÓN DE CONDICIONES
    # ═══════════════════════════════════════════════════════════

    def check(
        self,
        iteration: int,
        start_time: Optional[float] = None,
        state: Any = None,
        consecutive_failures: int = 0,
        quality_score: Optional[float] = None,
        quality_threshold: float = 0.80,
    ) -> bool:
        """
        Verifica todas las condiciones de parada.

        Args:
            iteration: Iteración actual
            start_time: Timestamp de inicio del loop
            state: Estado actual del loop
            consecutive_failures: Fallos consecutivos
            quality_score: Score de calidad actual
            quality_threshold: Umbral de calidad para parar

        Returns:
            bool: True si debe parar
        """
        if not self._enabled:
            return False

        if self._stop_reason:
            return True

        if iteration >= self.max_iterations:
            self._stop_reason = StopReason.MAX_ITERATIONS
            logger.info(f"Stop: max iterations reached ({iteration})")
            return True

        if start_time:
            elapsed = time.time() - start_time
            if elapsed >= self.global_timeout:
                self._stop_reason = StopReason.TIMEOUT
                logger.info(f"Stop: timeout reached ({elapsed:.2f}s)")
                return True

        if consecutive_failures >= self.max_consecutive_failures:
            self._stop_reason = StopReason.CONSECUTIVE_FAILURES
            logger.info(f"Stop: consecutive failures ({consecutive_failures})")
            return True

        if quality_score is not None and quality_score >= quality_threshold:
            self._stop_reason = StopReason.QUALITY_THRESHOLD
            logger.info(f"Stop: quality threshold reached ({quality_score:.2f})")
            return True

        for condition in sorted(self._custom_conditions, key=lambda c: -c.priority):
            if condition.evaluate():
                self._stop_reason = condition.reason
                logger.info(f"Stop: custom condition '{condition.name}' activated")
                return True

        return False

    # ═══════════════════════════════════════════════════════════
    # CONDICIONES CUSTOM
    # ═══════════════════════════════════════════════════════════

    def add_condition(
        self,
        name: str,
        check_fn: Callable[[], bool],
        reason: StopReason,
        priority: int = 0,
        description: str = "",
    ):
        """
        Agrega condición de parada custom.

        Args:
            name: Nombre identificador
            check_fn: Función que retorna True si debe parar
            reason: Razón de parada asociada
            priority: Prioridad (mayor = verifica primero)
            description: Descripción de la condición
        """
        condition = StopCondition(
            name=name,
            check_fn=check_fn,
            reason=reason,
            priority=priority,
            description=description,
        )
        self._custom_conditions.append(condition)
        logger.debug(f"StopCondition added: {name} (priority={priority})")

    def remove_condition(self, name: str) -> bool:
        """
        Remueve condición custom por nombre.

        Args:
            name: Nombre de la condición

        Returns:
            bool: True si fue removida
        """
        initial_count = len(self._custom_conditions)
        self._custom_conditions = [c for c in self._custom_conditions if c.name != name]
        removed = len(self._custom_conditions) < initial_count
        if removed:
            logger.debug(f"StopCondition removed: {name}")
        return removed

    def enable_condition(self, name: str) -> bool:
        """Activa una condición custom."""
        for condition in self._custom_conditions:
            if condition.name == name:
                condition.enabled = True
                logger.debug(f"StopCondition enabled: {name}")
                return True
        return False

    def disable_condition(self, name: str) -> bool:
        """Desactiva una condición custom."""
        for condition in self._custom_conditions:
            if condition.name == name:
                condition.enabled = False
                logger.debug(f"StopCondition disabled: {name}")
                return True
        return False

    # ═══════════════════════════════════════════════════════════
    # ESTADO Y MÉTRICAS
    # ═══════════════════════════════════════════════════════════

    def get_time_remaining(self) -> Optional[float]:
        """
        Calcula tiempo restante antes de timeout.

        Returns:
            float: Segundos restantes o None si no started
        """
        if not self._start_time:
            return None
        elapsed = time.time() - self._start_time
        return max(0.0, self.global_timeout - elapsed)

    def get_iterations_remaining(self, current: int) -> int:
        """
        Calcula iteraciones restantes.

        Args:
            current: Iteración actual

        Returns:
            int: Iteraciones restantes
        """
        return max(0, self.max_iterations - current)

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna métricas de condiciones."""
        return {
            "max_iterations": self.max_iterations,
            "global_timeout": self.global_timeout,
            "max_consecutive_failures": self.max_consecutive_failures,
            "stop_reason": self._stop_reason.value if self._stop_reason else None,
            "is_stopped": self.is_stopped,
            "time_remaining": self.get_time_remaining(),
            "custom_conditions_count": len(self._custom_conditions),
            "custom_conditions": [
                {"name": c.name, "enabled": c.enabled, "priority": c.priority}
                for c in self._custom_conditions
            ],
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serializa estado completo."""
        return {
            "max_iterations": self.max_iterations,
            "global_timeout": self.global_timeout,
            "max_consecutive_failures": self.max_consecutive_failures,
            "start_time": self._start_time,
            "stop_reason": self._stop_reason.value if self._stop_reason else None,
            "enabled": self._enabled,
            "custom_conditions": len(self._custom_conditions),
        }
