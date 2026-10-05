#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — F1 "Mi Hard": vista read-only de hardware y modelos
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/mi_hard_dialog.py
# Propósito: diálogo "Mi Hard" (hardware detectado + qué modelos entran).
# Origen: extraído de modals.py (superaba 1000 líneas → AUD-PERF) siguiendo
#         el esquema 50/50: modals.py RE-EXPORTA create_mi_hard_dialog, así
#         que el API público y los consumidores (app.py) no cambian.
# 100% read-only: 0 LLM, 0 descargas, 0 flags nuevos, 0 dependencias nuevas.
# Fuente de datos: src/interfaces/web/routes_hwfit.py (endpoints existentes),
#                  consumidos IN-PROCESS (sin HTTP).
# ═══════════════════════════════════════════════════════════════
import json
import logging
from typing import Any, Dict, List, Optional

from nicegui import ui

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass


def _log_error(error: Exception, context: str, extra: dict = None):
    """Log de error — misma vía que modals._log_error (DetailedLogger)."""
    try:
        from tools.detailed_logger import get_logger
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
            return
    except Exception as _d2e:  # noqa: BLE001
        logger.debug(f"[D2 mi_hard_dialog._log_error] {_d2e}")
    logger.error(f"[{context}] {error}")




# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9v (F1 "Mi Hard"): vista READ-ONLY de hardware + modelos
# ═══════════════════════════════════════════════════════════════
# Basada en lo que YA existe (0 LLM, 0 dependencias nuevas):
#   · src/core/hardware_fit.py  → HardwareFitManager (singleton CON caché)
#   · src/interfaces/web/routes_hwfit.py → /hwfit/hardware|rank|check|compatible
# Reglas de diseño:
#   · NO recalcula hardware en cada refresh (el manager cachea; refresh manual).
#   · NO modifica ni descarga modelos. NO corre benchmarks.
#   · Si Ollama no responde o falta un modelo, muestra el motivo (no rompe).
#   · Muestra "modo CPU" explícito cuando no hay GPU utilizable.
def _hwfit_payload(res: Any) -> Optional[Dict[str, Any]]:
    """Normaliza la respuesta de un endpoint hwfit (dict o JSONResponse)."""
    if isinstance(res, dict):
        return res
    body = getattr(res, "body", None)
    if body:
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except Exception as _d2e:  # noqa: BLE001 — body no-JSON: degradar a None
            log_degraded(logger, _d2e, "mi_hard: body de endpoint no decodificable")
            return None
    return None


def mi_hard_sections(hw: Dict[str, Any], rank: Dict[str, Any],
                     compat: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transforma los payloads hwfit en secciones renderizables.

    Función PURA (sin NiceGUI) para poder testear el formato sin cliente UI.
    Devuelve [{title, lines:[(texto, clase_css), ...]}, ...].
    """
    gpu_ok = bool(hw.get("is_gpu_available"))
    hw_lines = [
        (
            f"{'🎮 GPU' if gpu_ok else '🧠 CPU (sin GPU utilizable)'} · "
            f"backend={hw.get('backend', '?')}",
            "text-xs",
        ),
        (
            f"GPU: {hw.get('gpu_name') or '—'} ({hw.get('gpu_vram_gb', 0)} GB VRAM) · "
            f"RAM: {hw.get('ram_gb', '?')} GB · threads: {hw.get('cpu_threads', '?')} · "
            f"budget: {hw.get('effective_budget_gb', '?')} GB",
            "text-xs text-gray-600",
        ),
    ]

    compat_lines: List[Any] = []
    if compat.get("has_warnings"):
        compat_lines.append((
            f"⚠️ {compat.get('models_checked', 0)} modelos — alguno está al límite o no entra",
            "text-xs text-orange-600",
        ))
    else:
        compat_lines.append(("✅ todos entran", "text-xs text-green-600"))
    for slot, info in (compat.get("results") or {}).items():
        if not isinstance(info, dict):
            continue
        compat_lines.append((
            f"· {slot}: {info.get('model_name')} — "
            f"{info.get('fit_level')} ({info.get('run_mode')})",
            "text-xs",
        ))

    rank_lines: List[Any] = []
    for m in (rank.get("models") or []):
        notes = (m.get("notes") or "")[:60]
        rank_lines.append((
            f"· {m.get('model_name')} — {m.get('size_gb')} GB "
            f"(req {m.get('required_gb')}) · {m.get('fit_level')} · {m.get('run_mode')}"
            + (f" — {notes}" if notes else ""),
            "text-xs",
        ))

    return [
        {"title": "Hardware detectado", "lines": hw_lines},
        {"title": "Modelos de la configuración", "lines": compat_lines},
        {
            "title": f"Modelos instalados ({rank.get('count', 0)})",
            "lines": rank_lines,
        },
    ]


def _hwfit_routes() -> Optional[Dict[str, Any]]:
    """Importa perezosamente las corrutinas de /hwfit/* (una sola vez).

    Se llama en el MONTAJE del diálogo, no en el click: el primer import
    de routes_hwfit cuesta ~3 s y dentro del request NiceGUI avisa
    "Response not ready after 3.0 seconds".
    """
    try:
        from src.interfaces.web.routes_hwfit import (
            hwfit_hardware, hwfit_rank, hwfit_compatible, hwfit_check_model,
        )
        return {
            "hardware": hwfit_hardware, "rank": hwfit_rank,
            "compatible": hwfit_compatible, "check": hwfit_check_model,
        }
    except Exception as _d2e:  # noqa: BLE001 — sin backend la vista degrada
        log_degraded(logger, _d2e, "mi_hard: import de routes_hwfit")
        return None


def _build_mi_hard_dialog() -> "ui.dialog":
    """Diálogo "Mi Hard": hardware detectado, compatibilidad y ranking de modelos."""
    dialog = ui.dialog().props('maximized=false')
    _lock = {"active": False}
    routes = _hwfit_routes()   # coste de import FUERA del click (ver arriba)

    with dialog, ui.card().classes('w-[42rem] max-w-full max-h-[85vh] overflow-y-auto p-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('🖥️ Mi Hard').classes('text-lg font-bold')
            ui.button('✕', on_click=dialog.close).props('flat dense round size=sm')

        hw_box = ui.column().classes('w-full gap-0')
        compat_box = ui.column().classes('w-full gap-0')
        rank_box = ui.column().classes('w-full gap-0')
        if routes is None:
            with hw_box:
                ui.label('❌ módulo hwfit no disponible').classes('text-xs text-red')
        with ui.row().classes('w-full items-center gap-2 mt-2'):
            check_input = ui.input(placeholder='modelo a chequear (ej: qwen3:8b)').classes('flex-1')
            check_out = ui.label('').classes('text-xs')

        async def _run_check() -> None:
            name = (check_input.value or '').strip()
            if not name:
                check_out.set_text('indicá un modelo')
                return
            if routes is None:
                check_out.set_text('❌ hwfit no disponible')
                return
            try:
                data = _hwfit_payload(await routes["check"](name))
                if not data:
                    check_out.set_text(f'❌ sin respuesta para {name}')
                    return
                if data.get('error'):
                    check_out.set_text(f"❌ {name}: no está instalado en Ollama")
                    return
                check_out.set_text(
                    f"✅ {data.get('model_name')}: {data.get('fit_level')} · "
                    f"{data.get('run_mode')} · {data.get('required_gb')} GB requeridos")
            except Exception as e:  # noqa: BLE001 — la UI nunca debe romper
                check_out.set_text(f'❌ error: {e}')

        ui.button('Chequear', on_click=_run_check).props('flat dense')

        async def refresh() -> None:
            """Refresca la vista. Manual: NO se dispara sola en cada tick."""
            if _lock["active"] or routes is None:
                return
            _lock["active"] = True
            try:
                hw = _hwfit_payload(await routes["hardware"]()) or {}
                rank = _hwfit_payload(await routes["rank"]()) or {}
                compat = _hwfit_payload(await routes["compatible"]()) or {}

                sections = mi_hard_sections(hw, rank, compat)
                for box, section in zip((hw_box, compat_box, rank_box), sections):
                    box.clear()
                    with box:
                        ui.label(section["title"]).classes(
                            'text-sm font-semibold mt-3')
                        for text, css in section["lines"]:
                            ui.label(text).classes(css)
            except Exception as e:  # noqa: BLE001
                _log_error(e, "mi_hard.refresh")
                try:
                    hw_box.clear()
                    with hw_box:
                        ui.label(f'❌ no se pudo leer el hardware: {e}').classes('text-xs text-red')
                except Exception as _d2e:
                    logger.debug(f"[D2 modals mi_hard] {_d2e}")
            finally:
                _lock["active"] = False

        with ui.row().classes('w-full justify-end gap-2 mt-3'):
            ui.button('Actualizar', on_click=refresh).props('flat dense')
            ui.button('Cerrar', on_click=dialog.close).props('flat dense')

        dialog._refresh = refresh  # type: ignore[attr-defined]
    return dialog


def create_mi_hard_dialog() -> "ui.dialog":
    """🖥️ Diálogo "Mi Hard" (hardware + modelos, read-only)."""
    return _build_mi_hard_dialog()
