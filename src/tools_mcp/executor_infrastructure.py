#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# executor_infrastructure.py - Infraestructura de Resiliencia y Métricas
# Ruta: /ruta/a/axioma/src/tools_mcp/executor_infrastructure.py
# Autor: SaintWick
# Versión: 0.6.0
#
# Patrones de resiliencia y métricas para el ToolExecutor:
# ToolMetrics (métricas bounded), CircuitBreaker (protección contra
# fallos en cascada) y CircuitBreakerRegistry (registro singleton).
# Extraído de tool_executor.py — refactorización 50/50.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import enum
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# TOOL METRICS — Métricas bounded por tool
# ============================================================================

class ToolMetrics:
    """
    Métricas de ejecución por tool con límite de memoria.

    Registro thread-safe de calls, éxitos, fallos y latencia.
    Las métricas se mantienen en un OrderedDict con límite de entradas
    para evitar crecimiento infinito en sesiones largas.

    Historial:
        v0.4.1 — Renombrada de ExecutionMetrics a _ToolMetrics (privada).
        REFACT  — Promovida a ToolMetrics (pública) al mover a su propio módulo.
                  Elimina ambigüedad con parallel_executor.ExecutionMetrics.
                  Esta clase es sync, keyed por tool_name.
    """

    __slots__ = ["_data", "_lock", "_max_entries"]

    def __init__(self, max_entries: int = 500) -> None:
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def record(self, tool_name: str, latency_ms: float, success: bool) -> None:
        """
        Registra una ejecución de tool.

        Args:
            tool_name  : Nombre de la tool.
            latency_ms : Latencia en milisegundos.
            success    : True si la ejecución fue exitosa.
        """
        with self._lock:
            if tool_name not in self._data:
                # Evict oldest entries si se alcanza el límite
                while len(self._data) >= self._max_entries:
                    self._data.popitem(last=False)
                self._data[tool_name] = {
                    "calls": 0,
                    "successes": 0,
                    "failures": 0,
                    "total_latency_ms": 0.0,
                    "last_latency_ms": 0.0,
                }
            entry = self._data[tool_name]
            entry["calls"] += 1
            entry["total_latency_ms"] += latency_ms
            entry["last_latency_ms"] = latency_ms
            if success:
                entry["successes"] += 1
            else:
                entry["failures"] += 1

    def get_all(self) -> Dict[str, Any]:
        """
        Retorna métricas agregadas de todas las tools.

        Returns:
            Dict keyed por tool_name con calls, success_rate,
            avg_latency_ms, last_latency_ms, failures.
        """
        with self._lock:
            result = {}
            for name, data in self._data.items():
                calls = data["calls"]
                result[name] = {
                    "calls": calls,
                    "success_rate": round(
                        data["successes"] / max(calls, 1), 4
                    ),
                    "avg_latency_ms": round(
                        data["total_latency_ms"] / max(calls, 1), 2
                    ),
                    "last_latency_ms": round(data["last_latency_ms"], 2),
                    "failures": data["failures"],
                }
            return result

    def get_tool(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Retorna métricas de una tool específica o None."""
        with self._lock:
            return dict(self._data.get(tool_name, {}))

    def clear(self) -> None:
        """Limpia todas las métricas."""
        with self._lock:
            self._data.clear()


# ============================================================================
# CIRCUIT BREAKER — Protección por tool contra fallos en cascada
# ============================================================================

class CBState(enum.Enum):
    """Estados del CircuitBreaker."""
    CLOSED    = "closed"      # Normal — permite ejecución
    OPEN      = "open"        # Disparado — bloquea ejecución sin tocar LLM
    HALF_OPEN = "half_open"   # Probando recuperación — deja pasar 1 request


class CircuitBreaker:
    """
    Circuit Breaker por tool. Thread-safe. Sin dependencias externas.

    Estados:
        CLOSED    → ejecución normal. Cuenta fallos consecutivos.
        OPEN      → bloquea inmediatamente. Tras `cooldown_seconds`
                     pasa a HALF_OPEN.
        HALF_OPEN → deja pasar exactamente 1 request de prueba.
                    Si tiene éxito → CLOSED (reset).
                    Si falla → OPEN (reinicia cooldown).

    Parámetros configurables por instancia:
        failure_threshold : fallos consecutivos para abrir el circuito
                            (default 5)
        cooldown_seconds  : tiempo en OPEN antes de intentar recuperación
                            (default 60s)

    La ventana de fallos es estrictamente consecutiva — un éxito resetea
    el contador. Esto evita disparos espurios por errores transitorios.
    """

    __slots__ = [
        "_state", "_failures", "_last_failure_time", "_lock",
        "_failure_threshold", "_cooldown_seconds", "_tool_name",
        "_total_opens", "_total_half_open_successes",
    ]

    def __init__(
        self,
        tool_name: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._tool_name = tool_name
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = CBState.CLOSED
        self._failures = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()
        # Contadores para observabilidad
        self._total_opens = 0
        self._total_half_open_successes = 0

    def is_open(self) -> bool:
        """
        Retorna True si el circuito bloquea la ejecución.

        Note: Este método fuerza la resolución del estado (cooldown check).
        """
        with self._lock:
            return self._resolve_state() == CBState.OPEN

    def allow_request(self) -> bool:
        """
        Verifica si se permite ejecutar el request.

        CLOSED    → True siempre.
        OPEN      → False hasta cooldown. Tras cooldown transiciona
                    a HALF_OPEN y retorna True.
        HALF_OPEN → True solo para el primer request de prueba.
        """
        with self._lock:
            state = self._resolve_state()
            if state == CBState.CLOSED:
                return True
            if state == CBState.OPEN:
                return False
            # HALF_OPEN: permitir exactamente 1 request de prueba.
            # Transicionamos a OPEN temporalmente para bloquear los
            # siguientes hasta que el resultado llegue vía record_result().
            return True

    def record_result(self, success: bool) -> None:
        """
        Registra el resultado de una ejecución y actualiza el estado.

        Debe llamarse siempre después de allow_request() == True.

        Args:
            success : True si la ejecución fue exitosa.
        """
        with self._lock:
            state = self._resolve_state()

            if success:
                if state in (CBState.CLOSED, CBState.HALF_OPEN):
                    # Reset completo
                    if state == CBState.HALF_OPEN:
                        self._total_half_open_successes += 1
                        logger.info(
                            f"[CB] '{self._tool_name}' HALF_OPEN → CLOSED "
                            f"(recuperado tras {self._total_opens} aperturas)"
                        )
                    self._failures = 0
                    self._last_failure_time = None
                    self._state = CBState.CLOSED
            else:
                self._failures += 1
                self._last_failure_time = time.monotonic()

                if state == CBState.HALF_OPEN:
                    # Prueba fallida → volver a OPEN
                    self._state = CBState.OPEN
                    logger.warning(
                        f"[CB] '{self._tool_name}' HALF_OPEN → OPEN "
                        f"(prueba fallida, cooldown={self._cooldown_seconds}s)"
                    )
                elif self._failures >= self._failure_threshold:
                    self._state = CBState.OPEN
                    self._total_opens += 1
                    logger.warning(
                        f"[CB] '{self._tool_name}' CLOSED → OPEN "
                        f"({self._failures} fallos consecutivos)"
                    )

    def _resolve_state(self) -> CBState:
        """
        Resuelve el estado real considerando el cooldown.

        Llamar solo bajo lock.
        OPEN + tiempo transcurrido >= cooldown → transiciona a HALF_OPEN.
        """
        if (
            self._state == CBState.OPEN
            and self._last_failure_time is not None
        ):
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._state = CBState.HALF_OPEN
                logger.info(
                    f"[CB] '{self._tool_name}' OPEN → HALF_OPEN "
                    f"(cooldown {elapsed:.1f}s / {self._cooldown_seconds}s)"
                )
        return self._state

    def reset(self) -> None:
        """Reset manual — para tests o intervención operativa."""
        with self._lock:
            self._state = CBState.CLOSED
            self._failures = 0
            self._last_failure_time = None

    def get_status(self) -> Dict[str, Any]:
        """Estado completo para observabilidad."""
        with self._lock:
            state = self._resolve_state()
            elapsed = (
                time.monotonic() - self._last_failure_time
                if self._last_failure_time
                else None
            )
            return {
                "tool": self._tool_name,
                "state": state.value,
                "failures_consecutive": self._failures,
                "failure_threshold": self._failure_threshold,
                "cooldown_seconds": self._cooldown_seconds,
                "elapsed_since_last_failure": (
                    round(elapsed, 1) if elapsed else None
                ),
                "total_opens": self._total_opens,
                "total_half_open_successes": self._total_half_open_successes,
            }


# ============================================================================
# CIRCUIT BREAKER REGISTRY — Registro singleton por tool
# ============================================================================

class CircuitBreakerRegistry:
    """
    Registro singleton de CircuitBreakers por tool.

    Permite configuración por tool y acceso centralizado para stats.
    Crea breakers lazily al primer acceso vía get().
    """

    __slots__ = [
        "_breakers", "_lock",
        "_default_threshold", "_default_cooldown",
    ]

    def __init__(
        self,
        default_failure_threshold: int = 5,
        default_cooldown_seconds: float = 60.0,
    ) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()
        self._default_threshold = default_failure_threshold
        self._default_cooldown = default_cooldown_seconds

    def get(self, tool_name: str) -> CircuitBreaker:
        """
        Retorna el breaker para una tool, creándolo si no existe.

        Args:
            tool_name : Nombre de la tool.

        Returns:
            CircuitBreaker para la tool especificada.
        """
        with self._lock:
            if tool_name not in self._breakers:
                self._breakers[tool_name] = CircuitBreaker(
                    tool_name=tool_name,
                    failure_threshold=self._default_threshold,
                    cooldown_seconds=self._default_cooldown,
                )
            return self._breakers[tool_name]

    def get_all_statuses(self) -> Dict[str, Any]:
        """
        Snapshot de estado de todos los breakers.

        Returns:
            Dict keyed por tool_name con el status de cada breaker.
            Útil para /health o dashboards de métricas.
        """
        with self._lock:
            return {
                name: cb.get_status()
                for name, cb in self._breakers.items()
            }

    def reset(self, tool_name: str) -> bool:
        """
        Reset manual de un breaker específico.

        Args:
            tool_name : Nombre de la tool.

        Returns:
            True si el breaker existía y fue reseteado.
        """
        with self._lock:
            if tool_name in self._breakers:
                self._breakers[tool_name].reset()
                return True
            return False

    def reset_all(self) -> None:
        """Reset de todos los breakers."""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "ToolMetrics",
    "CBState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
]
