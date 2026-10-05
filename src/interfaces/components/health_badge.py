#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Badge de salud del sistema (ISSUE-053)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/health_badge.py
# Propósito: que el usuario sepa de un vistazo si el sistema está sano o
#            degradado, sin abrir el diálogo "Estado".
#
# FUENTES (todas ya existentes, 0 backend nuevo):
#   · `degradation_stats()` → errores de CONTRATO en rutas críticas (ISSUE-044)
#   · `request_stats()`     → requests y tasa de error reales (ISSUE-045)
#   · `feedback` (opcional) → rechazo explícito reciente
#
# REGLAS DE DISEÑO (ver docs/AGENT_GUIDE.md R7):
#   · **SIN polling**: nada de `ui.timer`. El badge se refresca al cargar la
#     página, tras cada respuesta y al abrir "Estado". Un timer permanente sería
#     carga constante en un equipo CPU-only.
#   · Se muestra SIEMPRE (punto verde discreto cuando todo está bien) para que
#     la ausencia de señal no se confunda con "no lo miré".
#   · La lógica de decisión es una función PURA (`health_snapshot`) testeable
#     sin UI.
# ═══════════════════════════════════════════════════════════════
import logging
from typing import Any, Dict, Optional, Tuple

from nicegui import ui

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass

# Umbral de tasa de error de la API para marcar advertencia.
ERROR_RATE_WARN = 0.10
# Mínimo de requests para que la tasa de error sea significativa (con 1-2
# requests un 50% de error no dice nada).
ERROR_RATE_MIN_REQUESTS = 5

ESTADOS: Dict[str, Tuple[str, str, str]] = {
    # estado: (emoji, texto, clase CSS)
    "ok": ("🟢", "sano", "text-xs text-green-700"),
    "warn": ("🟡", "degradado", "text-xs text-orange-700"),
    "error": ("🔴", "con errores", "text-xs text-red-700"),
}


def health_snapshot(degradation: Optional[Dict[str, Any]] = None,
                    requests: Optional[Dict[str, Any]] = None,
                    feedback: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Calcula el estado de salud. Función PURA.

    Returns:
        {"estado": "ok|warn|error", "emoji":…, "texto":…, "css":…,
         "motivos": [...], "detalle": {…}}
    """
    degradation = degradation or {}
    requests = requests or {}
    motivos = []

    contract = int(degradation.get("contract") or 0)
    criticos = int(degradation.get("criticos_en_rutas_clave") or 0)

    if criticos > 0:
        motivos.append(
            f"{criticos} error(es) de contrato en rutas críticas")
    elif contract > 0:
        motivos.append(f"{contract} degradación(es) de contrato (fuera de rutas clave)")

    total_req = int(requests.get("total_requests") or 0)
    err_rate = float(requests.get("error_rate") or 0.0)
    if total_req >= ERROR_RATE_MIN_REQUESTS and err_rate >= ERROR_RATE_WARN:
        motivos.append(
            f"{err_rate * 100:.0f}% de error en la API "
            f"({requests.get('error_requests')}/{total_req})")

    if feedback:
        neg = int(feedback.get("negative_recent") or 0)
        if neg >= 3:
            motivos.append(f"{neg} calificaciones negativas recientes")

    if criticos > 0:
        estado = "error"
    elif motivos:
        estado = "warn"
    else:
        estado = "ok"

    emoji, texto, css = ESTADOS[estado]
    return {
        "estado": estado,
        "emoji": emoji,
        "texto": texto,
        "css": css,
        "motivos": motivos,
        "detalle": {
            "contract": contract,
            "soft": int(degradation.get("soft") or 0),
            "criticos": criticos,
            "total_requests": total_req,
            "error_rate": err_rate,
            "uptime_seconds": requests.get("uptime_seconds"),
        },
    }


def _snapshot_from_runtime() -> Dict[str, Any]:
    """Lee las métricas reales del proceso (nunca lanza)."""
    deg: Dict[str, Any] = {}
    req: Dict[str, Any] = {}
    try:
        from src.utils.degradation import degradation_stats
        deg = degradation_stats()
    except Exception as _d2e:  # noqa: BLE001
        log_degraded(logger, _d2e, "health_badge.degradation_stats")
    try:
        from src.interfaces.web.request_stats import stats as req_stats
        req = req_stats()
    except Exception as _d2e:  # noqa: BLE001
        log_degraded(logger, _d2e, "health_badge.request_stats")
    return health_snapshot(deg, req)


class HealthBadge:
    """Badge del header. Refresco MANUAL (sin timers)."""

    def __init__(self) -> None:
        self.element: Optional[Any] = None
        self.tooltip_holder: Optional[Any] = None
        self.last: Dict[str, Any] = {}

    def create(self) -> Optional[Any]:
        """Crea el badge y devuelve el elemento (o None si la UI falla)."""
        try:
            snap = _snapshot_from_runtime()
            self.last = snap
            with ui.row().classes('items-center gap-1') as row:
                self.element = ui.label(
                    f"{snap['emoji']} {snap['texto']}").classes(snap["css"])
                self.tooltip_holder = self.element
                self.element.tooltip(self._tooltip_text(snap))
            return row
        except Exception as _d2e:  # noqa: BLE001 — la UI nunca debe romper
            log_degraded(logger, _d2e, "health_badge.create")
            return None

    @staticmethod
    def _tooltip_text(snap: Dict[str, Any]) -> str:
        lineas = list(snap.get("motivos") or [])
        det = snap.get("detalle") or {}
        lineas.append(f"requests: {det.get('total_requests', 0)} "
                      f"({float(det.get('error_rate') or 0) * 100:.0f}% error)")
        lineas.append(f"degradaciones: {det.get('contract', 0)} de contrato / "
                      f"{det.get('soft', 0)} esperadas")
        up = det.get("uptime_seconds")
        if isinstance(up, (int, float)):
            lineas.append(f"activo hace {up / 60:.0f} min")
        if not snap.get("motivos"):
            lineas.append("sin incidencias")
        return " · ".join(lineas)

    def refresh(self) -> Dict[str, Any]:
        """Recalcula el estado y actualiza el elemento (barato, sin I/O)."""
        snap = _snapshot_from_runtime()
        self.last = snap
        try:
            if self.element is not None:
                self.element.set_text(f"{snap['emoji']} {snap['texto']}")
                self.element.classes(replace=snap["css"])
                self.element.tooltip(self._tooltip_text(snap))
        except Exception as _d2e:  # noqa: BLE001
            log_degraded(logger, _d2e, "health_badge.refresh")
        return snap


__all__ = ["HealthBadge", "health_snapshot", "ERROR_RATE_WARN",
           "ERROR_RATE_MIN_REQUESTS"]
