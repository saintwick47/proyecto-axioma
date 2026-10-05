#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# model_swap.py - VRAM Arbitrator (ModelSwap)
# Ruta: src/core/model_swap.py
# Autor: SaintWick
# Versión: 1.0.9
#
# Garantiza que solo un modelo Ollama resida en VRAM a la vez,
# previniendo thrashing y OOM en hardware con iGPU compartida.
# Controla keep_alive, realiza swap (unload + load) con timeouts
# adaptativos según hardware, e integra con vram_lock y
# HardwareFitManager para checks de compatibilidad y recuperación.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

import httpx

from config.settings import settings

logger = logging.getLogger("axiom.model_swap")


class ModelSwap:
    """
    Singleton VRAM arbitrator — un modelo en VRAM, uno fuera.

    Two-lock strategy:
      _state_lock — rápido, protege _current_model reads/writes
      _swap_lock  — lento, serializa secuencias unload→load
    """

    _instance: Optional["ModelSwap"] = None
    _init_lock = threading.Lock()

    # ── Keep-alive configuración ─────────────────────────────
    KEEP_ALIVE_DEFAULT = "30m"   # Modelo default: permanece cargado
    KEEP_ALIVE_TEMPORARY = "5m"  # Modelo no-default: expira rápido

    def __init__(self):
        self._ollama_url: str = settings.ollama_host
        self._default_model: str = settings.llm_default_model
        self._swap_timeout: float = float(
            settings.ollama_timeout_first_load * 3
        )  # 195s — suficiente para carga en frío

        self._current_model: Optional[str] = None
        self._last_swap_time: float = 0.0

        # Fast lock for _current_model reads/writes
        self._state_lock = threading.Lock()
        # Slow lock for swap sequences (unload + load = 30-90s)
        self._swap_lock = threading.Lock()

    # ── Singleton ────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "ModelSwap":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Reset singleton (tests only)."""
        with cls._init_lock:
            cls._instance = None

    # ── Public read-only ─────────────────────────────────────

    @property
    def current_model(self) -> Optional[str]:
        """Model currently believed to be in VRAM (None = unknown)."""
        with self._state_lock:
            return self._current_model

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def last_swap_time(self) -> float:
        with self._state_lock:
            return self._last_swap_time

    # ── Public state registration (sin API calls) ────────────

    def register_loaded(self, model_name: str):
        """
        Declarar que *model_name* está actualmente en VRAM.
        Llamar después de warmup o cuando se SABE que un modelo
        fue cargado externamente. No hace API call.
        """
        with self._state_lock:
            old = self._current_model
            self._current_model = model_name
            self._last_swap_time = time.time()
        if old != model_name:
            logger.info(f"[ModelSwap] registered: {old or '∅'} → {model_name}")

    def register_empty(self):
        """Declarar que VRAM está vacía (ej: después de restart Ollama)."""
        with self._state_lock:
            old = self._current_model
            self._current_model = None
        if old is not None:
            logger.info(f"[ModelSwap] registered empty (was {old})")

    # ── Core: ensure / restore ───────────────────────────────

    def ensure(self, model_name: str) -> bool:
        """
        Garantizar que *model_name* está cargado en VRAM.

        Returns
        -------
        True  — se realizó un swap (modelo viejo evicteado, nuevo cargado)
        False — el modelo ya estaba residente, no se hizo trabajo

        Raises
        ------
        RuntimeError — VRAM locked por RAC, o swap falló
        """
        # ── Fast path: ya está cargado ───────────────────────
        with self._state_lock:
            if self._current_model == model_name:
                return False

        # ── VRAM lock guard ──────────────────────────────────
        self._check_vram_lock()

        # ── Slow path: acquire swap lock ─────────────────────
        logger.info(f"[ModelSwap] ensure({model_name}) — swap required")
        with self._swap_lock:
            # Double-check (otro thread pudo haber swappeado)
            with self._state_lock:
                if self._current_model == model_name:
                    logger.debug("[ModelSwap] double-check: already loaded")
                    return False
                old_model = self._current_model

            # ── Hardware fit check (non-fatal) ───────────────
            self._check_model_fit(model_name)

            # ── Ejecutar swap ─────────────────────────────────
            t0 = time.time()
            try:
                # ✅ Modo PARALELO (pedido del usuario): si la placa puede tener el modelo más
                # pesado y el otro entra en memoria, NO se descarga: conviven (uno en la placa y
                # otro en memoria) y no se pierde el tiempo de recarga. En modo simple, igual que
                # siempre: un modelo a la vez.
                if old_model is not None and not self._trabaja_en_paralelo(old_model, model_name):
                    self._unload_model(old_model)
                self._load_model(model_name)
            except Exception as exc:
                logger.error(
                    f"[ModelSwap] SWAP FAILED {old_model} → {model_name}: {exc}"
                )
                # ══════════════════════════════════════════════════════════
                # ✅ FIX FASE 1: _emergency_recover() ya no toma argumento.
                # El modelo previo ya fue descargado o falló, la recuperación
                # siempre debe intentar cargar el default por seguridad.
                # ══════════════════════════════════════════════════════════
                self._emergency_recover()
                raise RuntimeError(
                    f"Model swap failed ({old_model} → {model_name}): {exc}"
                ) from exc

            # ── Actualizar estado ─────────────────────────────
            with self._state_lock:
                self._current_model = model_name
                self._last_swap_time = time.time()

            elapsed = time.time() - t0
            logger.info(
                f"[ModelSwap] swap OK {elapsed:.1f}s: "
                f"{old_model or '∅'} → {model_name}"
            )
            return True

    def restore_default(self) -> bool:
        """Volver al modelo default. Mismos returns que ensure()."""
        return self.ensure(self._default_model)

    # ── Emergency / recovery ─────────────────────────────────

    def _trabaja_en_paralelo(self, viejo: Optional[str], nuevo: str) -> bool:
        """¿Los dos modelos pueden convivir? (uno en la placa, otro en memoria).

        Se calcula con el hardware real (la detección está cacheada 24 h en `HardwareFitManager`).
        Ante cualquier duda se responde **NO** (modo simple): descargar de más nunca rompe, tener dos
        modelos que no entran sí.
        """
        try:
            from src.core.hardware_fit import HardwareFitManager, decidir_modo_de_trabajo
            hw = HardwareFitManager.instance().get_hardware()
            modo = decidir_modo_de_trabajo(hw)
            if modo == "paralelo":
                logger.info(f"[ModelSwap] modo PARALELO: '{viejo}' y '{nuevo}' pueden convivir "
                            "(uno en la placa, otro en memoria)")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ModelSwap] no se pudo decidir el modo de trabajo: {exc}")
        return False

    def force_unload_all(self):
        """Nuke everything from VRAM. Usar después de Ollama hang."""
        with self._swap_lock:
            with self._state_lock:
                old = self._current_model
                self._current_model = None
            if old:
                self._unload_model(old)
            logger.warning(f"[ModelSwap] force-unloaded: {old}")

    def reconcile(self):
        """
        Re-sincronizar estado interno con /api/ps de Ollama.
        Llamar después de Ollama restart, crash, o largo idle.
        """
        with self._swap_lock:
            loaded = self._query_ollama_ps()
            with self._state_lock:
                if loaded:
                    self._current_model = loaded[0]
                else:
                    self._current_model = None
                cur = self._current_model
            logger.info(f"[ModelSwap] reconciled → {cur} (Ollama ps: {loaded})")

    # ── Diagnostics ──────────────────────────────────────────

    def get_status(self) -> dict:
        with self._state_lock:
            cur = self._current_model
            last = self._last_swap_time
        return {
            "current_model": cur,
            "default_model": self._default_model,
            "swap_in_progress": self._swap_lock.locked(),
            "ollama_url": self._ollama_url,
            "last_swap_ago_s": round(time.time() - last, 1) if last else None,
        }

    def __repr__(self) -> str:
        return (
            f"ModelSwap(current={self.current_model}, "
            f"default={self._default_model})"
        )

    # ══════════════════════════════════════════════════════════
    # Internal
    # ══════════════════════════════════════════════════════════

    def _check_vram_lock(self):
        """Rehusar swap si RAC tiene el candado de VRAM."""
        try:
            from src.core.vram_lock import is_vram_locked, wait_for_vram_unlock
            if is_vram_locked():
                # ✅ AUD-FT-001 (2026-08-26): el piso de espera ya no es un
                # literal 300.0 — es settings.vram_lock_wait_timeout
                # (config-driven). Se combina con _swap_timeout (carga del
                # modelo) como máximo de ambos.
                wait_timeout = max(
                    float(self._swap_timeout),
                    float(getattr(settings, "vram_lock_wait_timeout", 300.0)),
                )
                logger.info("[ModelSwap] VRAM locked — esperando liberación...")
                unlocked = wait_for_vram_unlock(timeout=wait_timeout)
                if not unlocked:
                    raise RuntimeError(
                        "[ModelSwap] VRAM locked por RAC — timeout esperando liberación"
                    )
                logger.info("[ModelSwap] VRAM liberada, continuando swap")
        except ImportError:
            pass  # vram_lock no disponible, continuar

    def _unload_model(self, model_name: str):
        """
        Evictear modelo de VRAM enviando keep_alive=0.
        Non-fatal si falla — el modelo puede ya estar fuera.
        """
        if not model_name:
            return
        try:
            payload = {
                "model": model_name,
                "prompt": "",
                "keep_alive": 0,          # ← key: evictear inmediatamente
                "stream": False,
            }
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self._ollama_url}/api/generate", json=payload
                )
                resp.raise_for_status()
            logger.info(f"[ModelSwap] unloaded: {model_name}")
        except Exception as exc:
            # Non-fatal — el modelo puede haber sido evicteado ya
            logger.warning(
                f"[ModelSwap] unload {model_name} failed (non-fatal): {exc}"
            )

    def _load_model(self, model_name: str):
        """
        Pre-cargar modelo en VRAM vía generación de 1 token.
        Bloquea hasta que el modelo esté residente y responsivo.

        Default: keep_alive=30m (permanece cargado)
        Non-default: keep_alive=5m (expira rápido → auto-restore)

        ✅ FIX AUDITORÍA: timeout adaptado al modelo/hardware en vez de un
        valor fijo (self._swap_timeout, calculado una sola vez en __init__
        como ollama_timeout_first_load * 3). Un modelo grande corriendo en
        cpu_offload/cpu_only puede tardar bastante más que eso — ver
        _compute_load_timeout().
        """
        is_default = model_name == self._default_model
        keep_alive = self.KEEP_ALIVE_DEFAULT if is_default else self.KEEP_ALIVE_TEMPORARY

        payload = {
            "model": model_name,
            "prompt": " ",
            "stream": False,
            "keep_alive": keep_alive,
            "options": {
                "num_predict": 1,
                "temperature": 0.0,
            },
        }

        timeout = self._compute_load_timeout(model_name)
        logger.info(
            f"[ModelSwap] loading {model_name} (keep_alive={keep_alive}, "
            f"timeout={timeout:.0f}s) ..."
        )
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{self._ollama_url}/api/generate", json=payload
            )
            resp.raise_for_status()

        logger.info(f"[ModelSwap] loaded: {model_name}")

    def _compute_load_timeout(self, model_name: str) -> float:
        """
        ✅ FIX AUDITORÍA: timeout de carga adaptado al tamaño del modelo y
        al run_mode real (gpu vs cpu_offload/cpu_only), reutilizando
        HardwareFitManager (ya se consulta en _check_model_fit() para el
        warning de fit — mismo mecanismo, no uno nuevo). cpu_offload/
        cpu_only implican I/O de disco + carga mucho más lenta que GPU
        directo; un modelo grande en ese modo puede superar largamente
        el fijo original (195s). self._swap_timeout se mantiene como PISO
        mínimo — nunca baja de ahí, solo sube cuando hardware_fit indica
        que hace falta. Non-fatal: si hardware_fit no está disponible,
        cae al valor fijo original.
        """
        try:
            from src.core.hardware_fit import HardwareFitManager
            mgr = HardwareFitManager.instance()
            result = mgr.check_model(model_name)
            # Cifras conservadoras de estimación (no medición real de
            # throughput): ~2.5s/GB en GPU directo, ~8s/GB con offload/CPU
            # (lectura de disco + volcado a RAM en vez de VRAM directa).
            seconds_per_gb = 8.0 if result.run_mode in ("cpu_offload", "cpu_only") else 2.5
            estimated = result.size_gb * seconds_per_gb + 15.0  # +15s de margen fijo
            return max(self._swap_timeout, estimated)
        except ImportError:
            return self._swap_timeout
        except Exception as exc:
            logger.debug(f"[ModelSwap] timeout adaptativo no disponible, uso fijo: {exc}")
            return self._swap_timeout

    # ══════════════════════════════════════════════════════════
    # ✅ FIX FASE 1: Removido parámetro `previous_model` inútil.
    # Antes: _emergency_recover(self, previous_model) y nunca se usaba,
    # causando confusión en logs y firmas. Siempre carga el default.
    # ══════════════════════════════════════════════════════════
    def _emergency_recover(self):
        """
        Después de un swap fallido, intentar cargar el modelo default.
        Si el default también falla, marcar VRAM como vacía.
        """
        logger.warning("[ModelSwap] emergency recovery — loading default model")
        try:
            self._load_model(self._default_model)
            with self._state_lock:
                self._current_model = self._default_model
                self._last_swap_time = time.time()
            logger.info("[ModelSwap] emergency recovery OK — default loaded")
        except Exception as recover_exc:
            logger.error(
                f"[ModelSwap] emergency recovery FAILED: {recover_exc}"
            )
            with self._state_lock:
                self._current_model = None

    def _check_model_fit(self, model_name: str) -> None:
        """
        Consultar HardwareFitManager y emitir WARNING si el modelo no es
        compatible con el hardware detectado. Completamente non-fatal:
        ImportError y cualquier excepción se absorben silenciosamente.
        """
        try:
            from src.core.hardware_fit import HardwareFitManager
            mgr = HardwareFitManager.instance()
            result = mgr.check_model(model_name)
            if result.fit_level == "no_fit":
                budget: float = mgr.get_hardware().effective_budget_gb
                logger.warning(
                    f"[ModelSwap] hardware_fit=NO_FIT for {model_name!r} — "
                    f"{result.notes} "
                    f"(required={result.required_gb:.1f}GB, available={budget:.1f}GB)"
                )
            else:
                logger.debug(
                    f"[ModelSwap] hardware_fit={result.fit_level} for {model_name!r}"
                )
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 model_swap.py:402] excepción degradada (intencional): {_d2e}")
        except Exception as exc:
            logger.debug(f"[ModelSwap] hardware_fit check skipped: {exc}")

    def _query_ollama_ps(self) -> List[str]:
        """GET /api/ps — listar modelos que Ollama considera 'running'."""
        try:
            # ✅ AUD-FT-002 (2026-08-26): el getattr apuntaba a
            # "ollama_timeout" que NO existe en settings — el fallback 45.0
            # era efectivamente fijo. Ahora usa el setting real
            # ollama_timeout_standard (45s, ge=30 le=90) config-driven.
            _ps_timeout = float(settings.ollama_timeout_standard)
            with httpx.Client(timeout=_ps_timeout) as client:
                resp = client.get(f"{self._ollama_url}/api/ps")
                resp.raise_for_status()
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception as exc:
            logger.warning(f"[ModelSwap] /api/ps failed: {exc}")
            return []
