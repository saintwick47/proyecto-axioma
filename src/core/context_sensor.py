#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — ContextSensor: Snapshot del entorno en tiempo real
# Ruta: src/core/context_sensor.py
# Autor: SaintWick
# Versión: 1.0.0 (Semana 2 — D)
# Dependencias opcionales: psutil, wmctrl (subprocess)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


class ContextSensor:
    """
    Snapshot del estado del entorno sin dependencias pesadas.
    Usa psutil (CPU/RAM/batería) + subprocess wmctrl (ventanas activas).
    Inyectado en Dispatcher.process() como hint ambiental.
    """

    def get_snapshot(self) -> Dict[str, Any]:
        """Retorna estado completo del sistema en este momento."""
        return {
            "time":          datetime.now().strftime("%H:%M"),
            "day_of_week":   datetime.now().strftime("%A"),
            "hour":          datetime.now().hour,
            "cpu_percent":   self._get_cpu(),
            "ram_used_pct":  self._get_ram(),
            "battery":       self._get_battery(),
            "active_apps":   self._get_active_windows(),
            "ollama_running": self._check_process("ollama"),
        }

    def get_context_hint(self) -> str:
        """
        String compacto para inyectar en system prompt.
        Solo incluye alertas relevantes; vacío si todo normal.
        """
        if not _PSUTIL_AVAILABLE:
            return ""
        try:
            s = self.get_snapshot()
            hints = []
            battery = s.get("battery")
            if battery and battery["percent"] < 20 and not battery["charging"]:
                hints.append(f"batería crítica {battery['percent']:.0f}%")
            if s.get("ram_used_pct", 0) > 85:
                hints.append(f"RAM alta {s['ram_used_pct']:.0f}%")
            if s.get("cpu_percent", 0) > 80:
                hints.append(f"CPU alta {s['cpu_percent']:.0f}%")
            return ", ".join(hints)
        except Exception:
            return ""

    # ── Lecturas del sistema ──────────────────────────────────────────────────

    def _get_cpu(self) -> float:
        if not _PSUTIL_AVAILABLE:
            return 0.0
        try:
            return psutil.cpu_percent(interval=0.1)
        except Exception:
            return 0.0

    def _get_ram(self) -> float:
        if not _PSUTIL_AVAILABLE:
            return 0.0
        try:
            return psutil.virtual_memory().percent
        except Exception:
            return 0.0

    def _get_battery(self) -> Optional[Dict[str, Any]]:
        if not _PSUTIL_AVAILABLE:
            return None
        try:
            b = psutil.sensors_battery()
            if b:
                return {"percent": b.percent, "charging": b.power_plugged}
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 context_sensor.py:90] excepción degradada (intencional): {_d2e}")
        return None

    def _get_active_windows(self) -> List[str]:
        """Lectura pasiva via wmctrl — solo nombres de ventanas, sin efectos."""
        try:
            out = subprocess.check_output(
                ["wmctrl", "-l"], text=True, timeout=2.0,
                stderr=subprocess.DEVNULL,
            )
            lines = [ln.split(None, 3) for ln in out.strip().split("\n") if ln]
            return [parts[3] for parts in lines if len(parts) >= 4][:5]
        except Exception:
            return []

    def _check_process(self, name: str) -> bool:
        if not _PSUTIL_AVAILABLE:
            return False
        try:
            return any(
                name in p.name().lower()
                for p in psutil.process_iter(["name"])
            )
        except Exception:
            return False


# ── Singleton ─────────────────────────────────────────────────────────────────

_sensor_instance: Optional[ContextSensor] = None


def get_sensor() -> ContextSensor:
    global _sensor_instance
    if _sensor_instance is None:
        _sensor_instance = ContextSensor()
    return _sensor_instance
