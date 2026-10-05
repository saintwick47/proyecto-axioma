#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — parallel_executor_models.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/agents/parallel_executor_models.py
#
# 📌 PARTE DE UN ARCHIVO RESERVADO PARA USO FUTURO (MODELOS EN PARALELO, GPU).
#    ⚠️ NO ELIMINAR. Ver el encabezado de `parallel_executor.py` y el análisis
#    completo en docs/ISSUES_HISTORY.md (ISSUE-036). Hoy no está cableado porque
#    el hardware es CPU-only (~14 GB marginales) y el sistema serializa los
#    agentes a propósito (MemoryGuard / vram_lock / llm_concurrency);
#    quedará listo para cuando haya GPU con VRAM para varios modelos.
#
# ✅ v0.6.9v (REFACTOR): capa de MODELOS + MÉTRICAS extraída de
#   src/agents/parallel_executor.py (el original queda intacto).
#
#   Qué se movió desde parallel_executor.py (sin cambios de comportamiento):
#     - Helpers de logging `_log_executor` / `_log_error`
#     - `RetryConfig`      → backoff exponencial con jitter
#     - `ExecutionResult`  → resultado unificado de ejecución (ok/fail/to_dict)
#     - `ExecutionMetrics` → métricas bounded (percentiles p50/p95/p99,
#                            throughput, error rate por agente y prioridad)
#
#   `ParallelExecutor` permanece en parallel_executor.py, que re-exporta estos
#   símbolos para no romper la API pública del módulo.
#
#   Este módulo NO importa nada de parallel_executor.py → sin ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.core.task_queue import TaskPriority

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_executor(op: str, agent_type: str = "", task_id: str = "",
                  status: str = "", duration: float = None,
                  success: bool = True, extra: Dict[str, Any] = None):
    """
    Helper para logging de operaciones del ParallelExecutor.

    Args:
        op: Operación realizada (submit, execute, cancel, etc.)
        agent_type: Tipo de agente (coder, researcher, etc.)
        task_id: ID único de la tarea
        status: Estado actual de la tarea
        duration: Duración de la operación en segundos
        success: Si la operación fue exitosa
        extra: Información adicional para el log
    """
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(
                category="PARALLEL_EXECUTOR",
                message=f"{op} {status}",
                level="DEBUG" if success else "WARNING",
                extra={
                    "agent_type": agent_type,
                    "task_id": task_id,
                    "task_status": status,
                    "duration_ms": round(duration * 1000, 2) if duration else None,
                    **(extra or {}),
                }
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 parallel_executor_models.py] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 parallel_executor_models.py] excepción degradada (intencional): {_d2e}")


# ============================================================================
# CONFIGURACIÓN — Retry con backoff exponencial
# ============================================================================
@dataclass
class RetryConfig:
    """
    Configuración para reintentos con backoff exponencial.

    Fórmula: delay = min(base * (multiplier ^ attempt), max_delay)

    Example:
    >>> config = RetryConfig(base=1.0, multiplier=2.0, max_delay=30.0, max_attempts=3)
    >>> # Intento 1: 1s, Intento 2: 2s, Intento 3: 4s (máx 30s)
    """
    base: float = 1.0          # Delay base en segundos
    multiplier: float = 2.0    # Factor de multiplicación por intento
    max_delay: float = 30.0    # Delay máximo en segundos
    max_attempts: int = 3      # Máximo de intentos (1 = sin retry)
    jitter: bool = True        # Si agregar jitter aleatorio para evitar thundering herd

    def get_delay(self, attempt: int) -> float:
        """
        Calcula delay para un intento específico.

        Args:
            attempt: Número de intento (0-based)

        Returns:
            Delay en segundos
        """
        delay = min(self.base * (self.multiplier ** attempt), self.max_delay)
        if self.jitter:
            # Jitter aleatorio ±25% para evitar sincronización
            jitter_range = delay * 0.25
            delay += random.uniform(-jitter_range, jitter_range)
        return max(0.0, delay)


# ============================================================================
# RESULTADO DE EJECUCIÓN — Unificado para todos los agentes
# ============================================================================
@dataclass
class ExecutionResult:
    """
    Resultado unificado de ejecución de un agente.

    Attributes:
        success: True si la ejecución fue exitosa
        agent_type: Tipo de agente que ejecutó (coder, researcher, etc.)
        task_id: ID único de la tarea ejecutada
        output: Resultado principal (string para compatibilidad LLM)
        data: Datos estructurados opcionales (dict/list)
        error: Mensaje de error si success=False
        latency_ms: Tiempo total de ejecución en milisegundos
        attempts: Número de intentos realizados (para retry)
        metadata: Metadatos adicionales para trazabilidad
    """
    success: bool
    agent_type: str
    task_id: str
    output: str = ""
    data: Optional[Any] = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    attempts: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, agent_type: str, task_id: str, output: str,
           data: Optional[Any] = None, latency_ms: float = 0.0,
           attempts: int = 1, metadata: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        """Factory para resultado exitoso."""
        return cls(
            success=True,
            agent_type=agent_type,
            task_id=task_id,
            output=output,
            data=data,
            latency_ms=latency_ms,
            attempts=attempts,
            metadata=metadata or {},
        )

    @classmethod
    def fail(cls, agent_type: str, task_id: str, error: str,
             latency_ms: float = 0.0, attempts: int = 1,
             metadata: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        """Factory para resultado fallido."""
        return cls(
            success=False,
            agent_type=agent_type,
            task_id=task_id,
            error=error,
            latency_ms=latency_ms,
            attempts=attempts,
            metadata=metadata or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para logging/APIs."""
        return {
            "success": self.success,
            "agent_type": self.agent_type,
            "task_id": self.task_id,
            "output": self.output,
            "data": self.data,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "attempts": self.attempts,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        status = "✅" if self.success else "❌"
        return (
            f"ExecutionResult({status} {self.agent_type!r} "
            f"task={self.task_id[:8]}... {self.latency_ms:.1f}ms)"
        )


# ============================================================================
# MÉTRICAS DE EJECUCIÓN — Bounded para evitar memory leaks
# ============================================================================
class ExecutionMetrics:
    """
    Métricas de ejecución con límite de memoria.

    Features:
    - Thread-safe con asyncio.Lock
    - Bounded: elimina entradas antiguas cuando excede max_entries
    - Cálculo de percentiles (p50, p95, p99) para latency
    - Throughput y error rate en tiempo real
    - Métricas por agente y por prioridad
    """

    __slots__ = ['_data', '_lock', '_max_entries']

    def __init__(self, max_entries: int = 1000):
        """
        Inicializa métricas.

        Args:
            max_entries: Máximo de entradas a mantener (default: 1000)
        """
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._max_entries = max_entries

    async def record(self, result: ExecutionResult, priority: TaskPriority) -> None:
        """
        Registra métricas de una ejecución completada.

        Args:
            result: Resultado de la ejecución
            priority: Prioridad de la tarea ejecutada
        """
        async with self._lock:
            # Evict si excede límite
            while len(self._data) >= self._max_entries:
                # Eliminar la entrada más antigua
                oldest_key = next(iter(self._data))
                del self._data[oldest_key]

            self._data[result.task_id] = {
                "agent_type": result.agent_type,
                "priority": priority.name,
                "success": result.success,
                "latency_ms": result.latency_ms,
                "attempts": result.attempts,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timestamp_ts": time.time(),  # float epoch — evita parsing en get_stats()
            }

    async def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas agregadas.

        Returns:
            Dict con throughput, latency percentiles, error rate, etc.
        """
        async with self._lock:
            if not self._data:
                return {
                    "total_executions": 0,
                    "success_rate": 0.0,
                    "avg_latency_ms": 0.0,
                    "p50_latency_ms": 0.0,
                    "p95_latency_ms": 0.0,
                    "p99_latency_ms": 0.0,
                    "throughput_per_minute": 0.0,
                    "by_agent": {},
                    "by_priority": {},
                }

            latencies = [e["latency_ms"] for e in self._data.values()]
            successes = sum(1 for e in self._data.values() if e["success"])
            total = len(self._data)

            latencies_sorted = sorted(latencies)

            def percentile(data: List[float], p: float) -> float:
                if not data:
                    return 0.0
                k = (len(data) - 1) * p / 100
                f = int(k)
                c = f + 1 if f + 1 < len(data) else f
                return data[f] + (k - f) * (data[c] - data[f]) if c != f else data[f]

            # Throughput: ejecuciones por minuto (últimos 5 min aprox)
            _now_ts = time.time()
            recent = [
                e for e in self._data.values()
                if (_now_ts - e.get("timestamp_ts", 0)) < 300
            ]
            throughput = len(recent) / 5.0 if recent else 0.0

            # Agrupar por agente
            by_agent: Dict[str, Dict[str, Any]] = {}
            for e in self._data.values():
                agent = e["agent_type"]
                if agent not in by_agent:
                    by_agent[agent] = {"count": 0, "successes": 0, "latencies": []}
                by_agent[agent]["count"] += 1
                if e["success"]:
                    by_agent[agent]["successes"] += 1
                by_agent[agent]["latencies"].append(e["latency_ms"])

            # Calcular stats por agente
            for agent in by_agent:
                data = by_agent[agent]
                data["success_rate"] = round(data["successes"] / max(data["count"], 1), 4)
                data["avg_latency_ms"] = round(sum(data["latencies"]) / max(len(data["latencies"]), 1), 2)
                del data["latencies"]  # Limpiar para respuesta

            # Agrupar por prioridad
            by_priority: Dict[str, Dict[str, Any]] = {}
            for e in self._data.values():
                priority = e["priority"]
                if priority not in by_priority:
                    by_priority[priority] = {"count": 0, "successes": 0}
                by_priority[priority]["count"] += 1
                if e["success"]:
                    by_priority[priority]["successes"] += 1

            for priority in by_priority:
                data = by_priority[priority]
                data["success_rate"] = round(data["successes"] / max(data["count"], 1), 4)

            return {
                "total_executions": total,
                "success_rate": round(successes / max(total, 1), 4),
                "avg_latency_ms": round(sum(latencies) / max(total, 1), 2),
                "p50_latency_ms": round(percentile(latencies_sorted, 50), 2),
                "p95_latency_ms": round(percentile(latencies_sorted, 95), 2),
                "p99_latency_ms": round(percentile(latencies_sorted, 99), 2),
                "throughput_per_minute": round(throughput, 2),
                "by_agent": by_agent,
                "by_priority": by_priority,
            }

    async def clear(self) -> None:
        """Limpia todas las métricas."""
        async with self._lock:
            self._data.clear()


__all__ = [
    "RetryConfig",
    "ExecutionResult",
    "ExecutionMetrics",
    "_log_executor",
    "_log_error",
]
