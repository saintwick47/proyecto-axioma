#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — request_stats.py (v0.6.9v, paso 5 del plan)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/web/request_stats.py
# Propósito: contadores REALES de requests de la API para `/api/v1/stats`.
#
# POR QUÉ: `/api/v1/stats` devolvía **ceros hardcodeados**
# (`total_requests: 0, avg_latency_ms: 0, uptime_seconds: 0`) mientras
# `router_metrics` era real: un stub con apariencia de dato. Este módulo
# aporta la parte que faltaba, con memoria ACOTADA (nunca crece sin límite) y
# coste despreciable (unos pocos ints por request).
#
# Diseño: contadores en memoria + `threading.Lock` (los requests pueden llegar
# de varios hilos). Nada de persistencia ni de I/O por request.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

_MAX_PATHS = 50          # tope de paths distintos trackeados
_TOP_PATHS = 10          # cuántos devolver en el resumen

_LOCK = threading.Lock()
_PROCESS_START = time.time()
_STATS: Dict[str, Any] = {}


def _reset_locked() -> None:
    _STATS.clear()
    _STATS.update({
        "total": 0,
        "errors": 0,             # status >= 400
        "latency_ms_total": 0.0,
        "latency_ms_max": 0.0,
        "by_status": {},
        "by_path": {},
        "slowest_path": None,
        "first_request": None,
        "last_request": None,
    })


_reset_locked()


def record_request(path: str, status: int, duration_ms: float) -> None:
    """Registra un request (thread-safe, sin I/O, memoria acotada)."""
    try:
        dur = float(duration_ms) if duration_ms and duration_ms > 0 else 0.0
        with _LOCK:
            _STATS["total"] += 1
            if int(status) >= 400:
                _STATS["errors"] += 1
            _STATS["latency_ms_total"] += dur
            if dur > _STATS["latency_ms_max"]:
                _STATS["latency_ms_max"] = dur
                _STATS["slowest_path"] = path
            st = str(status)
            _STATS["by_status"][st] = _STATS["by_status"].get(st, 0) + 1
            key = path or "/"
            if key not in _STATS["by_path"]:
                # Se reserva 1 slot para "<otros>": si no, el dict llegaba a
                # MAX+1 claves (las MAX reales + el bucket de desborde).
                if len(_STATS["by_path"]) >= _MAX_PATHS - 1:
                    key = "<otros>"
            _STATS["by_path"][key] = _STATS["by_path"].get(key, 0) + 1
            now = time.time()
            if _STATS["first_request"] is None:
                _STATS["first_request"] = now
            _STATS["last_request"] = now
    except Exception as e:  # pragma: no cover — las métricas nunca rompen el request
        # R3: contar métricas es accesorio; el request se atiende igual.
        log_degraded(logger, e, "request_stats.record_request (_STATS)", contract_level=logging.DEBUG)


def uptime_seconds() -> float:
    """Segundos desde el arranque del proceso (1 fuente: psutil si está)."""
    try:
        import psutil
        return max(0.0, time.time() - psutil.Process().create_time())
    except Exception:
        # ✅ REVERTIDO (2026-09-29): acá NO va log_degraded. El `try` envuelve el
        # import OPCIONAL de `psutil` (no instalado a propósito; ver
        # requirements.txt) y `log_degraded` clasifica ModuleNotFoundError como
        # error de CONTRATO ⇒ WARNING en cada llamada, que además marcaba el badge
        # de salud como "con errores" con el sistema sano (lo cazó
        # test_axioma_23::TestWiring::test_badge_renders_and_updates_in_real_ui).
        # La sonda ya degrada sola: cae a `_PROCESS_START`.
        return max(0.0, time.time() - _PROCESS_START)


def stats(top_paths: int = _TOP_PATHS) -> Dict[str, Any]:
    """Snapshot JSON-serializable de los contadores de requests."""
    with _LOCK:
        total = _STATS["total"]
        lat_total = _STATS["latency_ms_total"]
        snapshot: Dict[str, Any] = {
            "total_requests": total,
            "error_requests": _STATS["errors"],
            "error_rate": round(_STATS["errors"] / total, 4) if total else 0.0,
            "avg_latency_ms": round(lat_total / total, 2) if total else 0.0,
            "max_latency_ms": round(_STATS["latency_ms_max"], 2),
            "slowest_path": _STATS["slowest_path"],
            "by_status": dict(_STATS["by_status"]),
            "top_paths": dict(sorted(_STATS["by_path"].items(),
                                     key=lambda kv: -kv[1])[:top_paths]),
            "first_request": _STATS["first_request"],
            "last_request": _STATS["last_request"],
        }
    snapshot["uptime_seconds"] = round(uptime_seconds(), 1)
    snapshot["generated_at"] = time.time()
    return snapshot


def reset() -> None:
    """Reinicia los contadores (tests)."""
    with _LOCK:
        _reset_locked()


async def track_request_stats(request, call_next):
    """Middleware HTTP que alimenta estos contadores.

    ✅ v0.6.9v: vive acá (y no como closure dentro de `server.py`) para poder
    testearlo de verdad — se monta en la app y se ejercita con un cliente real
    en vez de inspeccionar el fuente. Es agnóstico de FastAPI (duck typing).
    """
    t0 = time.time()
    response = await call_next(request)
    try:
        record_request(request.url.path, response.status_code,
                       (time.time() - t0) * 1000.0)
    except Exception as _d2e:  # noqa: BLE001 — nunca romper el request
        import logging
        logging.getLogger(__name__).debug(
            f"[D2 request_stats.track_request_stats] {_d2e}")
    return response


__all__ = ["record_request", "stats", "reset", "uptime_seconds",
           "track_request_stats"]
