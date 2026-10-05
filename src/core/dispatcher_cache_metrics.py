#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Dispatcher: Cache y Métricas internas
# Ruta: src/core/dispatcher_cache_metrics.py
# Autor: SaintWick
# Extraído de dispatcher.py v0.9.0
# Contiene: RoutingCache, _DispatcherMetrics
# ═══════════════════════════════════════════════════════════════
import threading
import hashlib
from typing import Optional, Dict, Any
from collections import OrderedDict as OD


# ── RoutingCache ─────────────────────────────────────────────────────────────

class RoutingCache:
    """Cache LRU para decisiones de routing frecuentes (thread-safe)."""

    __slots__ = ['_cache', '_lock', '_hits', '_misses', '_max_size']

    def __init__(self, max_size: int = 500):
        self._cache: OD = OD()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._max_size = max_size

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.encode()).hexdigest()[:32]

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        k = self._key(query)
        with self._lock:
            if k in self._cache:
                self._cache.move_to_end(k)
                self._hits += 1
                return self._cache[k]
            self._misses += 1
            return None

    def set(self, query: str, decision: Dict[str, Any]) -> None:
        k = self._key(query)
        with self._lock:
            if k in self._cache:
                self._cache.move_to_end(k)
            self._cache[k] = decision
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
        }


# ── _DispatcherMetrics ───────────────────────────────────────────────────────

class _DispatcherMetrics:
    """
    Métricas internas del Dispatcher con límite de memoria.
    Privada — no confundir con BoundedMetrics de smart_router_core.py.
    """

    __slots__ = ['_metrics', '_lock', '_total_count', '_max_entries', '_planning_stats']

    def __init__(self, max_entries: int = 1000):
        self._metrics: OD = OD()
        self._lock = threading.Lock()
        self._total_count = 0
        self._max_entries = max_entries
        self._planning_stats = {
            "total_planned": 0,
            "avg_depth": 0.0,
            "step_success": 0,
            "step_failures": 0,
        }

    def record(
        self,
        task_type: str,
        handler: str,
        latency_ms: float,
        success: bool,
        plan_depth: int = 0,
        step_index: int = -1,
    ) -> None:
        key = f"{task_type}:{handler}"
        with self._lock:
            if key not in self._metrics:
                while len(self._metrics) >= self._max_entries:
                    self._metrics.popitem(last=False)
                self._metrics[key] = {
                    "count": 0,
                    "total_latency": 0.0,
                    "successes": 0,
                    "failures": 0,
                    "planning_depths": [],
                }
            m = self._metrics[key]
            m["count"] += 1
            m["total_latency"] += latency_ms
            if success:
                m["successes"] += 1
            else:
                m["failures"] += 1
            if plan_depth > 0:
                m["planning_depths"].append(plan_depth)
                self._planning_stats["total_planned"] += 1
                if step_index >= 0:
                    if success:
                        self._planning_stats["step_success"] += 1
                    else:
                        self._planning_stats["step_failures"] += 1
            self._total_count += 1

    def get_all(self) -> Dict[str, Any]:
        with self._lock:
            result = {}
            for key, data in self._metrics.items():
                if data["count"] > 0:
                    depths = data.get("planning_depths", [])
                    avg_depth = sum(depths) / max(len(depths), 1) if depths else 0.0
                    result[key] = {
                        "count": data["count"],
                        "avg_latency_ms": round(data["total_latency"] / data["count"], 2),
                        "success_rate": round(data["successes"] / max(data["count"], 1), 4),
                        "avg_plan_depth": round(avg_depth, 2),
                    }
            return result

    def get_planning_stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._planning_stats)

    def get_total_count(self) -> int:
        return self._total_count

    def clear(self) -> None:
        with self._lock:
            self._metrics.clear()
            self._total_count = 0
            self._planning_stats = {
                "total_planned": 0,
                "avg_depth": 0.0,
                "step_success": 0,
                "step_failures": 0,
            }
