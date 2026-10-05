#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — MemoryGuard: Guard de memoria (VRAM/RAM) para modelos
# Ruta: src/core/memory_guard.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 4 plan_harness — Coordinación con seguridad de memoria):
#   Evitar OOM al trabajar con modelos en paralelo o en conjunto:
#     - can_load(model): ¿la carga del modelo dejaría la RAM sobre el
#       límite duro (HARD)? Si sí → NO ejecutar (fail-safe). Zona WARN
#       → permitir con advertencia (sistema de aviso).
#     - Estimación post-swap: como ModelSwap descarga el modelo previo
#       ANTES de cargar el nuevo (unload → load secuencial), can_load
#       suma solo el requerimiento del modelo nuevo — el swap en sí no
#       crea solapamiento de RAM (el modelo actual, mmap'd, no está en
#       ram_used_gb() y no se resta).
#     - can_coexist(a, b): ¿los modelos caben juntos en la VRAM/RAM
#       disponible? Pensado para el FUTURO con más VRAM donde se puedan
#       ejecutar modelos en paralelo (ver settings.allow_parallel_models).
#     - reserve()/release(): reservas nominales para que dos requests
#       concurrentes no doble-apunten la misma memoria.
#
# Sin dependencias duras: psutil es opcional; en Linux se lee
# /proc/meminfo; el tamaño de modelo se estima desde el tag (p. ej.
# "qwen3:8b" → ~8B params → ~5.2 GB Q4) con cache, y se afina con
# HardwareFitManager si Ollama está disponible.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── psutil opcional ─────────────────────────────────────────────────────────
try:
    import psutil  # type: ignore[import-not-found]
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


def _default_ram_ratios() -> Tuple[float, float]:
    """(warn_ratio, hard_ratio) desde settings si están disponibles."""
    try:
        from config.settings import settings
        return (
            float(getattr(settings, "memory_ram_warn_ratio", 0.85)),
            float(getattr(settings, "memory_ram_hard_ratio", 0.95)),
        )
    except Exception as e:
        # AUD-SIL: settings ausente → defaults conservadores (degradación).
        log_degraded(logger, e, "src/core/memory_guard.py:_default_ram_ratios")
        return 0.85, 0.95


def _default_headroom_gb() -> float:
    try:
        from config.settings import settings
        return float(getattr(settings, "memory_headroom_gb", 1.5))
    except Exception as e:
        # AUD-SIL: settings ausente → default (degradación).
        log_degraded(logger, e, "src/core/memory_guard.py:_default_headroom_gb")
        return 1.5


def _default_allow_parallel() -> bool:
    try:
        from config.settings import settings
        return bool(getattr(settings, "allow_parallel_models", False))
    except Exception as e:
        # AUD-SIL: settings ausente → False (seguro por defecto).
        log_degraded(logger, e, "src/core/memory_guard.py:_default_allow_parallel")
        return False


def _default_overhead_ratio() -> float:
    """Factor de overhead del footprint (sobre required_gb de hardware_fit)."""
    try:
        from config.settings import settings
        return float(getattr(settings, "memory_guard_overhead_ratio", 1.0))
    except Exception as e:
        log_degraded(logger, e, "src/core/memory_guard.py:_default_overhead_ratio")
        return 1.0


# ============================================================================
# MEMORY GUARD
# ============================================================================

class MemoryGuard:
    """
    Guard de memoria para carga de modelos — fail-safe contra OOM.

    Decisiones (política):
      - RAM después del swap > total * HARD_RATIO  → REFUSED (no ejecutar).
      - RAM después del swap > total * WARN_RATIO  → permitido con WARNING.
      - Modelo que no cabe en VRAM (no_fit)        → permitido con WARNING
        (corre en cpu_only/cpu_offload — lento pero seguro; el arbitraje
        de un modelo por vez ya lo hace ModelSwap).
      - can_coexist(): para el futuro con más VRAM (parallel mode).
    """

    # Constantes por defecto (override vía settings si existen)
    DEFAULT_MODEL_SIZE_GB = 4.0
    _Q4_GB_PER_B = 0.6   # densidad Q4_K_M: ~0.6 GB por billón de params
    _KV_OVERHEAD_GB = 0.4

    def __init__(
        self,
        warn_ratio: Optional[float] = None,
        hard_ratio: Optional[float] = None,
        headroom_gb: Optional[float] = None,
        overhead_ratio: Optional[float] = None,
    ) -> None:
        d_warn, d_hard = _default_ram_ratios()
        self.warn_ratio = float(warn_ratio if warn_ratio is not None else d_warn)
        self.hard_ratio = float(hard_ratio if hard_ratio is not None else d_hard)
        self.headroom_gb = float(headroom_gb if headroom_gb is not None else _default_headroom_gb())
        # ✅ CALIBRACIÓN (2026-08-27, re-calibrada 2026-08-29): factor sobre
        # required_gb (KV incluido) para ajustar al footprint real del modelo
        # cargado. Re-calibrada contra el RSS real de Ollama (serve + runner
        # llama-server): ratio 1.10 en este hardware (default 1.0 = usar
        # required_gb de hardware_fit, size * 1.12 KV). El ratio previo 1.17
        # se había calibrado contra /api/ps size (disco), que sobreestima la
        # RAM real ~5-13%.
        if overhead_ratio is None:
            overhead_ratio = _default_overhead_ratio()
        self._overhead_ratio = max(1.0, float(overhead_ratio))
        self._reservations: Dict[str, float] = {}
        self._size_cache: Dict[str, float] = {}
        self._warned: set = set()
        self._lock = threading.RLock()
        self._created_at = time.time()

    # ── Tamaño estimado de modelo ───────────────────────────────────────────

    def required_gb(self, model_name: str) -> float:
        """
        RAM/VRAM requerida estimada para el modelo (GB).

        Fuentes en orden:
          1. Caché interna.
          2. HardwareFitManager (si Ollama responde /api/tags).
          3. Estimación por tag: "qwen3:8b" → 8B params → ~5.2 GB Q4.
        """
        if not model_name:
            return 0.0
        with self._lock:
            if model_name in self._size_cache:
                return self._size_cache[model_name]

        size = self._estimate_from_hardware_fit(model_name)
        if size is None:
            size = self._estimate_from_tag(model_name)
        size = max(0.5, size)

        with self._lock:
            self._size_cache[model_name] = size
        return size

    def _estimate_from_hardware_fit(self, model_name: str) -> Optional[float]:
        try:
            from src.core.hardware_fit import HardwareFitManager
            result = HardwareFitManager.instance().check_model(model_name)
            # ✅ FIX CALIBRACIÓN (2026-08-27): antes devolvía result.size_gb
            # (tamaño del ARCHIVO en disco). El footprint real del modelo
            # cargado incluye KV cache + buffers de Ollama — hardware_fit ya
            # lo estima en result.required_gb (= size * (1 + 0.12 KV)).
            # ✅ RECALIBRADO (2026-08-29): el benchmark mide ahora el RSS real
            # de Ollama (serve + runner llama-server) y el ratio resultante en
            # este hardware es ~1.10 sobre required_gb (antes 1.17 contra
            # /api/ps size, que sobreestima la RAM real) — ver
            # calibration_data.json (memory_guard_tag.global_overhead_ratio) y
            # el setting memory_guard_overhead_ratio para ajustar.
            return float(result.required_gb) * self._overhead_ratio
        except Exception as e:
            # AUD-SIL: Ollama/hardware_fit no disponible → estimación por tag.
            log_degraded(logger, e, "src/core/memory_guard.py:_estimate_from_hardware_fit")
            return None

    def _estimate_from_tag(self, model_name: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_name, re.IGNORECASE)
        if m:
            params_b = float(m.group(1))
            # ✅ CALIBRACIÓN: aplicar el factor de overhead del footprint.
            return (params_b * self._Q4_GB_PER_B + self._KV_OVERHEAD_GB) * self._overhead_ratio
        return self.DEFAULT_MODEL_SIZE_GB * self._overhead_ratio

    # ── Memoria del sistema ─────────────────────────────────────────────────

    def ram_total_gb(self) -> float:
        """RAM total del sistema en GB (0.0 si no se puede medir)."""
        if _PSUTIL_AVAILABLE:
            try:
                return psutil.virtual_memory().total / (1024 ** 3)
            except Exception as e:
                # AUD-SIL-031: degradación — cae al fallback /proc/meminfo.
                log_degraded(logger, e, "src/core/memory_guard.py:ram_total_gb")
        try:
            total_kb = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                        break
            return total_kb / (1024 * 1024) if total_kb else 0.0
        except Exception as e:
            # AUD-SIL: /proc/meminfo ilegible → 0 (check permisivo).
            log_degraded(logger, e, "src/core/memory_guard.py:ram_total_gb")
            return 0.0

    def ram_used_gb(self) -> float:
        """RAM usada del sistema en GB (0.0 = desconocido → check permisivo)."""
        if _PSUTIL_AVAILABLE:
            try:
                return psutil.virtual_memory().used / (1024 ** 3)
            except Exception as e:
                # AUD-SIL-031: degradación — cae al fallback /proc/meminfo.
                log_degraded(logger, e, "src/core/memory_guard.py:ram_used_gb")
        try:
            total_kb = avail_kb = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb and avail_kb:
                        break
            if total_kb and avail_kb:
                return (total_kb - avail_kb) / (1024 * 1024)
            return 0.0
        except Exception as e:
            # AUD-SIL: /proc/meminfo ilegible → 0 (check permisivo).
            log_degraded(logger, e, "src/core/memory_guard.py:ram_used_gb")
            return 0.0

    # ── Modelo actual (para estimación post-swap) ──────────────────────────

    def current_model(self) -> Optional[str]:
        try:
            from src.core.model_swap import ModelSwap
            return ModelSwap.instance().current_model
        except Exception as e:
            # AUD-SIL: ModelSwap no disponible → sin modelo actual (None).
            log_degraded(logger, e, "src/core/memory_guard.py:current_model")
            return None

    # ── Chequeo de carga ────────────────────────────────────────────────────

    def can_load(self, model_name: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        ¿Se puede cargar *model_name* sin riesgo de OOM?

        Returns:
            (ok, reason, details)
            ok=False → NO ejecutar (RAM sobrecargada — fail-safe).
            reason  : "ok" | "warn_ram" | "refused_ram" | "no_fit_warn"
        """
        required = self.required_gb(model_name)
        total = self.ram_total_gb()
        used = self.ram_used_gb()
        current = self.current_model()
        current_req = self.required_gb(current) if current else 0.0

        # Estimación post-swap: ModelSwap descarga el modelo previo antes
        # de cargar el nuevo (unload → load secuencial).
        # ✅ FIX (2026-08-29): se ELIMINÓ el "- current_req". Con el mmap por
        # defecto de Ollama, los pesos del modelo cargado son page cache y NO
        # están en ram_used_gb() (psutil .used los excluye) — restar
        # current_req restaba un valor que nunca estuvo sumado y subestimaba
        # la RAM post-swap en ~current_req GB (can_load más permisivo de lo
        # diseñado). Sin la resta es conservador y correcto en ambos modos
        # (mmap on: modelo no contado → solo se suma el nuevo; mmap off:
        # modelo en used → se sobrestima en current_req, dirección segura).
        ram_after = used + required + self.headroom_gb

        hard_limit = total * self.hard_ratio if total > 0 else 0.0
        warn_limit = total * self.warn_ratio if total > 0 else 0.0

        details: Dict[str, Any] = {
            "model": model_name,
            "required_gb": round(required, 2),
            "ram_total_gb": round(total, 2),
            "ram_used_gb": round(used, 2),
            "ram_after_swap_gb": round(ram_after, 2),
            "ram_hard_limit_gb": round(hard_limit, 2),
            "ram_warn_limit_gb": round(warn_limit, 2),
            "current_model": current,
            "current_required_gb": round(current_req, 2),
        }

        # RAM desconocida (0.0) → no podemos afirmar OOM; chequeamos fit.
        if total <= 0:
            fit = self._fit_level(model_name)
            if fit == "no_fit":
                details["fit_level"] = "no_fit"
                return True, "no_fit_warn", details
            return True, "ok", details

        if ram_after > hard_limit:
            details["refused"] = "ram_hard_exceeded"
            return False, "refused_ram", details

        if ram_after > warn_limit:
            details["warn"] = "ram_near_limit"
            self._warn_once(model_name, ram_after, hard_limit)
            return True, "warn_ram", details

        fit = self._fit_level(model_name)
        if fit == "no_fit":
            details["fit_level"] = "no_fit"
            return True, "no_fit_warn", details
        return True, "ok", details

    def can_coexist(self, model_name: str, already_loaded: Dict[str, float]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        ¿*model_name* puede convivir en RAM/VRAM con los modelos ya cargados?
        Pensado para el futuro con más VRAM (allow_parallel_models=True).
        Suma los requerimientos de TODOS los modelos residentes.
        """
        required = self.required_gb(model_name)
        others = sum(v for k, v in already_loaded.items() if k != model_name)
        total = self.ram_total_gb()
        used = self.ram_used_gb()
        ram_after = used + others + required + self.headroom_gb
        hard_limit = total * self.hard_ratio if total > 0 else 0.0
        details = {
            "model": model_name,
            "required_gb": round(required, 2),
            "others_gb": round(others, 2),
            "ram_after_gb": round(ram_after, 2),
            "ram_hard_limit_gb": round(hard_limit, 2),
        }
        if total > 0 and ram_after > hard_limit:
            details["refused"] = "ram_hard_exceeded"
            return False, "refused_ram", details
        return True, "ok", details

    # ── Reservas nominales ──────────────────────────────────────────────────

    def reserve(self, model_name: str) -> bool:
        """Marca el modelo como reservado (en carga). True si no estaba reservado."""
        with self._lock:
            if model_name in self._reservations:
                return False
            self._reservations[model_name] = self.required_gb(model_name)
            return True

    def release(self, model_name: str) -> None:
        with self._lock:
            self._reservations.pop(model_name, None)

    def reserved_models(self) -> List[str]:
        with self._lock:
            return list(self._reservations.keys())

    # ── Utilidades ──────────────────────────────────────────────────────────

    def _fit_level(self, model_name: str) -> str:
        try:
            from src.core.hardware_fit import HardwareFitManager
            return HardwareFitManager.instance().check_model(model_name).fit_level
        except Exception as e:
            # AUD-SIL: hardware_fit no disponible → "unknown" (sin bloqueo).
            log_degraded(logger, e, "src/core/memory_guard.py:_fit_level")
            return "unknown"

    def _warn_once(self, model_name: str, ram_after: float, hard_limit: float) -> None:
        if model_name in self._warned:
            return
        self._warned.add(model_name)
        logger.warning(
            f"[MemoryGuard] ⚠️ RAM cerca del límite cargando {model_name!r}: "
            f"estimado {ram_after:.1f}GB / límite duro {hard_limit:.1f}GB. "
            "Si el sistema se vuelve inestable, descargue modelos (keep_alive=0)."
        )

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ram_total_gb": round(self.ram_total_gb(), 2),
                "ram_used_gb": round(self.ram_used_gb(), 2),
                "warn_ratio": self.warn_ratio,
                "hard_ratio": self.hard_ratio,
                "headroom_gb": self.headroom_gb,
                "reservations": dict(self._reservations),
                "size_cache": dict(self._size_cache),
                "warnings_issued": sorted(self._warned),
            }


# ============================================================================
# SINGLETON — get_memory_guard()
# ============================================================================

_instance: Optional[MemoryGuard] = None
_lock = threading.Lock()


def get_memory_guard() -> MemoryGuard:
    """Singleton del MemoryGuard (thread-safe, lazy)."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = MemoryGuard()
        return _instance
