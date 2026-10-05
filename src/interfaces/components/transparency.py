#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Panel de transparencia por respuesta (P0 del plan nuevo)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/transparency.py
# Propósito: mostrar, debajo de cada respuesta, POR QUÉ se puede confiar en ella:
#            modelo usado, confianza, si pasó validación, fuentes/citas, tiempo,
#            tarea y quién la atendió — y ahí mismo permitir calificarla.
#
# POR QUÉ EXISTE: `Response` ya traía `confidence`, `confidence_label`,
# `validation_passed` y `sources`, y los agentes/validador los setean, pero
# `_build_response_metadata` los DESCARTABA: la UI nunca mostraba nada de esto
# (el usuario solo veía el bloque 📎 Fuentes). No hay backend nuevo: es
# visibilidad de datos que ya existían (ver `docs/AGENT_GUIDE.md`).
#
# DISEÑO:
#   · `transparency_rows(meta)` es PURA (sin NiceGUI) → testeable sin UI.
#   · El colapsable arranca CERRADO y no refresca solo: sin cargas permanentes
#     (regla R7 del proyecto: CPU-only).
#   · `validation_state` es un tri-estado HONESTO: `validation_passed=False` no
#     siempre significa "falló" — puede ser que la validación no aplique a esa
#     tarea. Nunca se afirma un fallo que no ocurrió.
# ═══════════════════════════════════════════════════════════════
import logging
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass


_FUENTES_MOSTRADAS = 8   # `_build_response_metadata` capea `sources` a 8

# Estado de validación → (texto, clase CSS)
_VALIDATION_LABELS: Dict[str, Tuple[str, str]] = {
    "aprobada": ("✅ Validación: aprobada", "text-green-700"),
    "fallida": ("⚠️ Validación: falló (se conservó la respuesta original)",
                "text-orange-700"),
    "omitida": ("⚠️ Validación: omitida", "text-orange-600"),
    "no_aplicada": ("— Validación: no aplica a esta tarea", "text-gray-500"),
}


def confidence_label(value: Optional[float]) -> str:
    """Etiqueta de confianza coherente con el resto del sistema."""
    if value is None:
        return "—"
    if value >= 0.8:
        return "Alto"
    if value >= 0.5:
        return "Moderado"
    return "Bajo"


def transparency_rows(meta: Optional[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """Filas del panel: (etiqueta, valor, clase CSS). Función PURA.

    Devuelve solo lo que REALMENTE existe en la metadata; nada inventado.
    """
    meta = meta or {}
    rows: List[Tuple[str, str, str]] = []

    model = meta.get("model_used") or "—"
    task = meta.get("task_type") or "—"
    agent = meta.get("agent_role") or ""
    rows.append(("🤖 Modelo", f"{model}  ·  tarea: {task}" +
                 (f"  ·  atendió: {agent}" if agent else ""), "text-xs"))

    conf = meta.get("confidence")
    label = meta.get("confidence_label") or confidence_label(conf)
    if conf is None:
        rows.append(("🎯 Confianza", f"— ({label})", "text-xs text-gray-500"))
    else:
        pct = f"{float(conf) * 100:.0f}%"
        css = ("text-xs text-green-700" if float(conf) >= 0.8 else
               "text-xs text-orange-700" if float(conf) >= 0.5 else
               "text-xs text-red-700")
        rows.append(("🎯 Confianza", f"{label} ({pct})", css))

    state = meta.get("validation_state") or (
        "aprobada" if meta.get("validation_passed") else "no_aplicada")
    v_text, v_css = _VALIDATION_LABELS.get(state, _VALIDATION_LABELS["no_aplicada"])
    report = meta.get("validation_report") or {}
    if isinstance(report, dict) and report.get("score") is not None:
        v_text += f"  ·  score {report['score']}"
    rows.append(("🧪", v_text, v_css))

    sources = [s for s in (meta.get("sources") or []) if isinstance(s, str) and s.strip()]
    if sources:
        extra = (" (mostrando las primeras "
                 f"{_FUENTES_MOSTRADAS})" if len(sources) >= _FUENTES_MOSTRADAS else "")
        rows.append(("📎 Fuentes", f"{len(sources)} cita(s){extra}",
                     "text-xs text-blue-700"))
    else:
        rows.append(("📎 Fuentes", "sin citas", "text-xs text-gray-500"))

    lat = meta.get("latency_ms")
    if isinstance(lat, (int, float)) and lat > 0:
        rows.append(("⏱️ Tiempo", f"{lat / 1000:.1f} s", "text-xs text-gray-600"))

    cls_conf = meta.get("classification_confidence")
    if isinstance(cls_conf, (int, float)):
        rows.append(("🧭 Clasificación",
                     f"{float(cls_conf) * 100:.0f}% de seguridad en la tarea",
                     "text-xs text-gray-500"))

    return rows


def render_transparency_block(meta: Optional[Dict[str, Any]],
                              host: Optional[Any] = None,
                              feedback_factory: Optional[Any] = None) -> Optional[Any]:
    """Colapsable '¿Por qué esta respuesta?' (+ feedback dentro, si se pasa).

    Args:
        meta: metadata de la respuesta (`_build_response_metadata`).
        host: contenedor NiceGUI donde insertarlo (por defecto el slot actual).
        feedback_factory: callable sin argumentos que construye el widget de
            feedback dentro del colapsable (fusiona transparencia + calificación
            en una sola superficie, como pide el plan).

    Returns:
        El `ui.expansion` creado, o None si no hay nada que mostrar.
    """
    rows = transparency_rows(meta)
    if not rows:
        return None
    try:
        ctx = host if host is not None else _null_ctx()
        with ctx:
            with ui.expansion('🔍 ¿Por qué esta respuesta?').classes(
                    'w-full text-xs text-gray-600') as exp:
                for label, value, css in rows:
                    ui.label(f"{label}: {value}").classes(css)
                if feedback_factory is not None:
                    try:
                        feedback_factory()
                    except Exception as _d2e:  # noqa: BLE001
                        log_degraded(logger, _d2e,
                                     "transparency: widget de feedback")
            return exp
    except Exception as _d2e:  # noqa: BLE001 — la UI nunca debe romper
        log_degraded(logger, _d2e, "transparency.render_transparency_block")
        return None


class _null_ctx:
    """Contexto nulo (usa el slot actual de NiceGUI)."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


__all__ = ["transparency_rows", "render_transparency_block",
           "confidence_label"]
