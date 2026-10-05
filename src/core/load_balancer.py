#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.0 — Load Balancer: Distribución entre Workers Locales
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/load_balancer.py
# Autor: SaintWick
# Fecha: 2026-04-09
# Propósito: Distribuye trabajo entre N workers asyncio locales.
#            Selección por least-connections. 4 workers por defecto
#            (deja 2 cores para Ollama y sistema).
#            Sin dependencias externas.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Awaitable

logger = logging.getLogger(__name__)

# ============================================================================
# LOGGING
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_event(message: str, level: str = "INFO", extra: dict = None) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(category="LOAD_BALANCER", message=message,
                          level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 load_balancer.py:40] excepción degradada (intencional): {_d2e}")


# ============================================================================
# WORKER
# ============================================================================
@dataclass
class Worker:
    """Worker individual con tracking de carga."""
    worker_id: str
    max_concurrent: int = 4
    _active: int = field(default=0, repr=False)
    _completed: int = field(default=0, repr=False)
    _failed: int = field(default=0, repr=False)
    _total_latency_ms: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def active(self) -> int:
        return self._active

    @property
    def is_available(self) -> bool:
        return self._active < self.max_concurrent

    @property
    def load_ratio(self) -> float:
        """0.0 = idle, 1.0 = saturado."""
        return self._active / self.max_concurrent

    @property
    def avg_latency_ms(self) -> float:
        total = self._completed + self._failed
        return round(self._total_latency_ms / total, 2) if total > 0 else 0.0

    async def run(
        self,
        coro: Callable[..., Awaitable[Any]],
        *args,
        **kwargs,
    ) -> Any:
        """
        Ejecuta una corutina contando carga activa.
        Actualiza métricas al finalizar.
        """
        async with self._lock:
            self._active += 1

        start = time.perf_counter()
        success = False
        try:
            result = await coro(*args, **kwargs)
            success = True
            return result
        except Exception:
            raise
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            async with self._lock:
                self._active -= 1
                self._total_latency_ms += elapsed
                if success:
                    self._completed += 1
                else:
                    self._failed += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "active": self._active,
            "max_concurrent": self.max_concurrent,
            "load_ratio": round(self.load_ratio, 3),
            "completed": self._completed,
            "failed": self._failed,
            "avg_latency_ms": self.avg_latency_ms,
        }


# ============================================================================
# LOAD BALANCER
# ============================================================================
class LoadBalancer:
    """
    Distribuye corutinas entre N workers locales por least-connections.

    Estrategia: selecciona el worker con menor `active` que aún tenga
    capacidad disponible. Si todos están saturados, espera con backoff.

    Thread-safe. Sin dependencias externas.
    """

    def __init__(
        self,
        num_workers: int = 4,
        max_concurrent_per_worker: int = 3,
        wait_timeout_seconds: float = 30.0,
    ):
        self._workers: List[Worker] = [
            Worker(
                worker_id=f"worker_{i}",
                max_concurrent=max_concurrent_per_worker,
            )
            for i in range(num_workers)
        ]
        self._wait_timeout = wait_timeout_seconds
        self._lock = asyncio.Lock()
        self._total_dispatched = 0
        self._total_rejected = 0

        logger.info(
            f"[LOAD_BALANCER] Inicializado: {num_workers} workers × "
            f"{max_concurrent_per_worker} concurrent = "
            f"{num_workers * max_concurrent_per_worker} capacity total"
        )
        _log_event("LoadBalancer inicializado", extra={
            "num_workers": num_workers,
            "max_concurrent_per_worker": max_concurrent_per_worker,
            "total_capacity": num_workers * max_concurrent_per_worker,
        })

    def _select_worker(self) -> Optional[Worker]:
        """
        Selecciona el worker menos cargado con capacidad disponible.
        Estrategia: least-connections.
        """
        available = [w for w in self._workers if w.is_available]
        if not available:
            return None
        return min(available, key=lambda w: w._active)

    async def dispatch(
        self,
        coro: Callable[..., Awaitable[Any]],
        *args,
        worker_id: Optional[str] = None,
        **kwargs,
    ) -> Any:
        """
        Despacha una corutina al worker menos cargado.

        Args:
            coro: Función async a ejecutar
            *args: Argumentos posicionales para coro
            worker_id: Si se especifica, fuerza ese worker (override)
            **kwargs: Argumentos keyword para coro

        Returns:
            Resultado de la corutina

        Raises:
            TimeoutError: Si no hay worker disponible en wait_timeout
            RuntimeError: Si todos los workers están saturados y no se puede esperar
        """
        # Override de worker específico
        if worker_id:
            w = next((w for w in self._workers if w.worker_id == worker_id), None)
            if w:
                async with self._lock:
                    self._total_dispatched += 1
                return await w.run(coro, *args, **kwargs)

        # Espera con backoff hasta encontrar worker disponible
        deadline = time.perf_counter() + self._wait_timeout
        delay = 0.01  # 10ms inicial

        while time.perf_counter() < deadline:
            async with self._lock:
                worker = self._select_worker()
                if worker:
                    self._total_dispatched += 1
                    _log_event(f"Dispatch → {worker.worker_id}", extra={
                        "active": worker.active,
                        "load_ratio": round(worker.load_ratio, 3),
                    })

            if worker:
                return await worker.run(coro, *args, **kwargs)

            # Todos saturados — esperar con backoff exponencial (máx 200ms)
            await asyncio.sleep(min(delay, 0.2))
            delay *= 1.5

        async with self._lock:
            self._total_rejected += 1
        raise TimeoutError(
            f"[LOAD_BALANCER] Sin worker disponible en {self._wait_timeout}s"
        )

    async def dispatch_batch(
        self,
        tasks: List[Dict[str, Any]],
        parallel: bool = True,
    ) -> List[Any]:
        """
        Despacha múltiples corutinas. Si parallel=True usa gather.

        Cada task: {"coro": fn, "args": [], "kwargs": {}}
        """
        if not tasks:
            return []

        if parallel:
            coroutines = [
                self.dispatch(t["coro"], *t.get("args", []), **t.get("kwargs", {}))
                for t in tasks
            ]
            return list(await asyncio.gather(*coroutines, return_exceptions=True))
        else:
            results = []
            for t in tasks:
                r = await self.dispatch(t["coro"], *t.get("args", []), **t.get("kwargs", {}))
                results.append(r)
            return results

    @property
    def total_capacity(self) -> int:
        return sum(w.max_concurrent for w in self._workers)

    @property
    def total_active(self) -> int:
        return sum(w.active for w in self._workers)

    @property
    def is_saturated(self) -> bool:
        return all(not w.is_available for w in self._workers)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "workers": [w.to_dict() for w in self._workers],
            "total_capacity": self.total_capacity,
            "total_active": self.total_active,
            "total_dispatched": self._total_dispatched,
            "total_rejected": self._total_rejected,
            "saturated": self.is_saturated,
            "global_load_ratio": round(
                self.total_active / max(self.total_capacity, 1), 3
            ),
        }


# ============================================================================
# SINGLETON
# ============================================================================
_lb_instance: Optional[LoadBalancer] = None
_lb_lock = threading.Lock()


def get_load_balancer(
    num_workers: int = 4,
    max_concurrent_per_worker: int = 3,
    wait_timeout_seconds: float = 30.0,
) -> LoadBalancer:
    """Singleton thread-safe de LoadBalancer."""
    global _lb_instance
    if _lb_instance is None:
        with _lb_lock:
            if _lb_instance is None:
                _lb_instance = LoadBalancer(
                    num_workers=num_workers,
                    max_concurrent_per_worker=max_concurrent_per_worker,
                    wait_timeout_seconds=wait_timeout_seconds,
                )
    return _lb_instance


def reset_load_balancer() -> None:
    """Solo para tests."""
    global _lb_instance
    with _lb_lock:
        _lb_instance = None
