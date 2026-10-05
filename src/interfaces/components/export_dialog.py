#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — F3: diálogo "Exportar investigación"
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/export_dialog.py
# Propósito: botón 📄 del header → exporta las respuestas con citas de la
#            sesión a Markdown/HTML y las descarga.
# Diseño:
#   · `export_session_research()` es la lógica (testeable sin NiceGUI);
#     el diálogo solo la llama y muestra el resultado.
#   · Handlers SÍNCRONOS: escribir el archivo es IO de disco local y rápido;
#     no se bloquea el event loop con `await` innecesario (regla CPU-only).
#   · El destino es el workspace de la sesión (WorkspaceManager), el mismo
#     que ya usa el resto del sistema.
#   · HTML → PDF: el propio HTML trae hoja de impresión (Ctrl+P del navegador).
#     No se agrega ninguna dependencia de PDF.
# ═══════════════════════════════════════════════════════════════
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from nicegui import ui

from src.core.research_export import (
    EXPORT_FORMATS,
    ResearchEntry,
    collect_entries,
    export_research,
    export_summary,
)

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass


_FMT_LABEL = {"md": "Markdown (.md)", "html": "HTML (imprimible a PDF)"}

# ✅ ISSUE-055: export de la CONVERSACIÓN COMPLETA (usuario + asistente + citas +
# artefactos de código), que es distinto del export de investigación de arriba:
# la investigación documenta RESPUESTAS; la conversación documenta el diálogo.
_CONV_LABEL = {"md": "Conversación MD", "html": "Conversación HTML",
               "json": "Conversación JSON"}


def export_session_research(state: Any, fmt: str = "md",
                            out_dir: Optional[Any] = None) -> Optional[Path]:
    """Exporta la investigación de la sesión. `None` si no hay nada que exportar.

    Función de lógica pura respecto de la UI: se puede llamar desde tests o
    desde el CLI sin abrir un diálogo.
    """
    if state is None or fmt not in EXPORT_FORMATS:
        return None
    messages = getattr(state, "messages", None) or []
    entries: List[ResearchEntry] = collect_entries(messages)
    if not export_summary(entries)["answers"]:
        return None
    meta: Dict[str, Any] = {"model": getattr(state, "model", "") or ""}
    return export_research(entries, getattr(state, "session_id", "") or "",
                           fmt=fmt, meta=meta, out_dir=out_dir)


def export_current_conversation(state: Any, fmt: str = "md",
                                out_dir: Any = None) -> Optional[Path]:
    """Exporta la conversación de la sesión ABIERTA (sin pasar por Sesiones)."""
    if state is None or fmt not in _CONV_LABEL:
        return None
    session_id = getattr(state, "session_id", "") or ""
    if not session_id:
        return None
    try:
        from .sessions_dialog import export_session
        # Se pasa el estado VIVO: así el export no depende de que el manager
        # todavía tenga la sesión (evita documentos vacíos en silencio).
        return export_session(session_id, fmt, out_dir=out_dir, state=state)
    except Exception as _d2e:  # noqa: BLE001
        log_degraded(logger, _d2e, "export_dialog.export_current_conversation")
        return None


def _summary_text(state: Any) -> str:
    """Resumen para la UI: cuántas respuestas/citas hay para exportar."""
    messages = getattr(state, "messages", None) or [] if state else []
    stats = export_summary(collect_entries(messages))
    if not stats["answers"]:
        return "Todavía no hay respuestas para exportar."
    return (f"{stats['answers']} respuesta(s) · "
            f"{stats['with_citations']} con citas · "
            f"{stats['sources']} fuente(s) única(s)")


def _build_export_dialog(state: Any) -> "ui.dialog":
    """Diálogo "Exportar investigación" (Markdown / HTML)."""
    dialog = ui.dialog()
    _lock = {"active": False}

    with dialog, ui.card().classes('w-[34rem] max-w-full p-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('📄 Exportar investigación').classes('text-lg font-bold')
            ui.button('✕', on_click=dialog.close).props('flat dense round size=sm')

        summary = ui.label(_summary_text(state)).classes('text-xs text-gray-600')
        result = ui.label('').classes('text-xs')
        ui.label(
            'Se guarda en el workspace de la sesión. Para PDF: abrí el HTML '
            'e imprimí con "Guardar como PDF".'
        ).classes('text-xs text-gray-500 mt-1')

        def _do_export(fmt: str) -> None:
            if _lock["active"]:
                return
            _lock["active"] = True
            try:
                path = export_session_research(state, fmt)
                if path is None:
                    result.set_text('⚠️ no hay respuestas para exportar')
                    ui.notify('⚠️ No hay respuestas para exportar', color='warning')
                    return
                # Descarga directa desde el propio servidor NiceGUI
                try:
                    ui.download.file(str(path))
                except Exception as _d2e:  # noqa: BLE001 — sin descarga, queda el path
                    log_degraded(logger, _d2e, "export_dialog: ui.download.file")
                result.set_text(f"✅ {path.name} ({path.stat().st_size} bytes)")
                ui.notify(f"✅ Exportado: {path.name}")
            except Exception as e:  # noqa: BLE001 — la UI nunca debe romper
                logger.error(f"[export_dialog] fallo exportando {fmt}: {e}")
                result.set_text(f'❌ error: {e}')
                ui.notify(f'❌ Error exportando: {e}', color='negative')
            finally:
                _lock["active"] = False

        ui.label('Investigación (respuestas con citas)').classes(
            'text-xs font-semibold mt-2')
        with ui.row().classes('w-full gap-2'):
            for fmt in EXPORT_FORMATS:
                ui.button(_FMT_LABEL[fmt],
                          on_click=lambda f=fmt: _do_export(f)).props('flat dense')

        # ✅ ISSUE-055: la conversación completa, desde acá mismo.
        ui.label('Conversación completa (preguntas, respuestas, código)').classes(
            'text-xs font-semibold mt-2')

        def _do_conversation(fmt: str) -> None:
            if _lock["active"]:
                return
            _lock["active"] = True
            try:
                path = export_current_conversation(state, fmt)
                if path is None:
                    result.set_text('⚠️ no hay conversación para exportar')
                    ui.notify('⚠️ No hay conversación para exportar',
                              color='warning')
                    return
                try:
                    ui.download.file(str(path))
                except Exception as _d2e:  # noqa: BLE001
                    log_degraded(logger, _d2e, "export_dialog.ui.download")
                result.set_text(f"✅ {path.name} ({path.stat().st_size} bytes)")
                ui.notify(f"✅ Exportado: {path.name}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[export_dialog] fallo exportando conversación: {e}")
                result.set_text(f'❌ error: {e}')
            finally:
                _lock["active"] = False

        with ui.row().classes('w-full gap-2'):
            for fmt in ("md", "html", "json"):
                ui.button(_CONV_LABEL[fmt],
                          on_click=lambda f=fmt: _do_conversation(f)
                          ).props('flat dense')

        with ui.row().classes('w-full justify-end mt-2'):
            ui.button('Cerrar', on_click=dialog.close).props('flat dense')

        def refresh() -> None:
            """Recalcula el resumen (se llama al abrir, no por timer)."""
            summary.set_text(_summary_text(state))

        dialog._refresh = refresh        # type: ignore[attr-defined]
        dialog._result = result          # type: ignore[attr-defined]
    return dialog


def create_export_dialog(state: Any) -> "ui.dialog":
    """📄 Diálogo de exportación de investigación de la sesión."""
    return _build_export_dialog(state)
