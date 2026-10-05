#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Gestor de sesiones visible (ISSUE-054)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/sessions_dialog.py
# Propósito: listar, buscar, exportar y borrar sesiones desde la UI, sin
#            depender de comandos. El backend ya existía (`SessionManager`
#            singleton + `GET/DELETE /sessions`); faltaba la superficie.
#
# DISEÑO
#   · `filter_sessions()` / `session_rows()` / `relative_time()` /
#     `build_session_json()` / `build_session_markdown()` son **PURAS** →
#     testeables sin UI ni sesiones reales.
#   · Borrar es DESTRUCTIVO: se pide confirmación en dos pasos y **no se permite
#     borrar la sesión en uso** (rompería la página abierta).
#   · Borrar quita la sesión de memoria; los archivos YA exportados y el feedback
#     persistido no se tocan (se dice en la UI, no se asume).
#   · Refresco MANUAL (`dialog._refresh`) y filtrado en memoria: sin `ui.timer`
#     (regla R7).
# ═══════════════════════════════════════════════════════════════
import json
import logging
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass


# ═══════════════════════════════════════════════════════════════
# LÓGICA PURA
# ═══════════════════════════════════════════════════════════════
def _norm(text: Any) -> str:
    """Minúsculas sin acentos (buscar 'sesion' debe encontrar 'sesión')."""
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def parse_ts(value: Any) -> Optional[datetime]:
    """Parsea un timestamp ISO del estado de sesión (None si no se puede)."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def relative_time(value: Any, now: Optional[datetime] = None) -> str:
    """'hace 3 min' / 'hace 2 h' / 'hace 4 d' (o '—' si no se puede)."""
    ts = parse_ts(value)
    if ts is None:
        return "—"
    ref = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (ref - ts).total_seconds()
    if delta < 0:
        return "ahora"
    if delta < 60:
        return "hace segundos"
    if delta < 3600:
        return f"hace {int(delta // 60)} min"
    if delta < 86400:
        return f"hace {int(delta // 3600)} h"
    return f"hace {int(delta // 86400)} d"


def filter_sessions(sessions: Dict[str, Dict[str, Any]],
                    query: str = "") -> List[str]:
    """IDs que matchean la búsqueda (por id, modelo o usuario), más nuevos primero.

    Función PURA: recibe el dict de `SessionManager.get_all_sessions()`.
    """
    q = _norm(query).strip()
    items: List[Tuple[float, str]] = []
    for sid, info in (sessions or {}).items():
        if not isinstance(info, dict):
            continue
        if q:
            heno = _norm(" ".join(str(info.get(k, "")) for k in
                                  ("session_id", "model", "user_id", "user_role")))
            heno += " " + _norm(sid)
            if q not in heno:
                continue
        ts = parse_ts(info.get("last_activity"))
        items.append((ts.timestamp() if ts else 0.0, sid))
    items.sort(key=lambda kv: kv[0], reverse=True)
    return [sid for _, sid in items]


def can_delete(session_id: str, current_session_id: Optional[str]) -> Tuple[bool, str]:
    """¿Se puede borrar? Devuelve (permitido, motivo si no).

    Protección real: borrar la sesión **en uso** dejaría la página abierta
    apuntando a un estado inexistente.
    """
    if not session_id:
        return False, "sesión inválida"
    if current_session_id and session_id == current_session_id:
        return False, "es tu sesión actual (cambiala antes de borrarla)"
    return True, ""


def session_rows(info: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Filas (etiqueta, valor, css) para mostrar una sesión. PURA."""
    info = info or {}
    sid = str(info.get("session_id") or "?")
    rows: List[Tuple[str, str, str]] = [
        ("🆔", sid[:16] + ("…" if len(sid) > 16 else ""), "text-xs font-mono"),
        ("💬", f"{int(info.get('message_count') or 0)} mensaje(s)",
         "text-xs text-gray-600"),
        ("🤖", f"{info.get('model') or '—'}  ·  temp {info.get('temperature', '—')}",
         "text-xs text-gray-600"),
        ("🕒", f"activa {relative_time(info.get('last_activity'))}  ·  "
               f"creada {relative_time(info.get('created_at'))}",
         "text-xs text-gray-500"),
    ]
    if info.get("uploaded_files_count"):
        rows.append(("📎", f"{info['uploaded_files_count']} archivo(s)",
                     "text-xs text-blue-700"))
    if info.get("is_processing"):
        rows.append(("⏳", "procesando", "text-xs text-orange-700"))
    if info.get("jarvis_mode") and info.get("jarvis_mode") != "normal":
        rows.append(("🤖", f"modo {info['jarvis_mode']}", "text-xs text-purple-700"))
    return rows


def build_session_json(info: Dict[str, Any],
                       messages: Optional[List[Any]] = None) -> str:
    """Snapshot JSON completo de una sesión (metadata + mensajes con fuentes)."""
    payload: Dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "session": dict(info or {}),
        "messages": [],
    }
    for msg in (messages or []):
        if hasattr(msg, "to_dict"):
            payload["messages"].append(msg.to_dict())
        elif isinstance(msg, dict):
            payload["messages"].append(msg)
    payload["message_count"] = len(payload["messages"])
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def build_session_markdown(info: Dict[str, Any],
                           messages: Optional[List[Any]] = None) -> str:
    """Markdown de la conversación completa (usuario + asistente + citas)."""
    info = info or {}
    sid = info.get("session_id") or "sin-sesión"
    out: List[str] = [
        "# 💬 Conversación — AXIOMA",
        "",
        f"- **Sesión:** `{sid}`",
        f"- **Modelo:** `{info.get('model') or '—'}`  ·  "
        f"temperatura `{info.get('temperature', '—')}`",
        f"- **Mensajes:** {int(info.get('message_count') or 0)}",
        f"- **Creada:** {info.get('created_at') or '—'}",
        f"- **Última actividad:** {info.get('last_activity') or '—'}",
        "",
        "---",
        "",
    ]
    for msg in (messages or []):
        d = msg.to_dict() if hasattr(msg, "to_dict") else (msg or {})
        role = "🧑 Vos" if (d.get("role") == "user") else "📘 AXIOMA"
        out.append(f"## {role}")
        if d.get("timestamp"):
            out.append(f"<sub>{d['timestamp']}</sub>")
            out.append("")
        out.append(str(d.get("content") or ""))
        out.append("")
        # Normalización defensiva: `sources` debe ser lista. Si por datos viejos
        # llegara un string, iterarlo daría una "fuente" por carácter (bug real
        # detectado al probar el export).
        _raw_src = d.get("sources") or []
        if isinstance(_raw_src, str):
            _raw_src = [_raw_src] if _raw_src.strip() else []
        fuentes = [s for s in _raw_src if isinstance(s, str) and s.strip()]
        if fuentes:
            out.append(f"### Fuentes ({len(fuentes)})")
            out.append("")
            for s in fuentes:
                out.append(f"- {s}")
            out.append("")
        for art in _artifacts_of(d):
            titulo = art.get("name") or "archivo"
            if art.get("is_test"):
                titulo += "  (test)"
            out.append(f"### 🧩 {titulo}")
            out.append("")
            out.append(f"```{art.get('language') or 'text'}")
            out.append(str(art.get("content") or ""))
            out.append("```")
            if art.get("truncated"):
                out.append("")
                out.append("> ⚠️ Contenido truncado en la exportación "
                           "(el archivo completo está en `data/outputs/`).")
            out.append("")
        out.append("---")
        out.append("")
    if not messages:
        out += ["> Esta sesión no tiene mensajes.", ""]
    return "\n".join(out)


def _artifacts_of(msg_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Artefactos válidos de un mensaje (normaliza y descarta basura). PURA."""
    out: List[Dict[str, Any]] = []
    for art in (msg_dict.get("artifacts") or []):
        if not isinstance(art, dict):
            continue
        nombre = str(art.get("name") or "").strip()
        contenido = art.get("content")
        if not nombre or not isinstance(contenido, str):
            continue
        out.append({
            "name": nombre,
            "language": str(art.get("language") or "text"),
            "content": contenido,
            "is_test": bool(art.get("is_test")),
            "truncated": bool(art.get("truncated")),
        })
    return out


def build_session_html(info: Dict[str, Any],
                       messages: Optional[List[Any]] = None) -> str:
    """HTML autocontenido de la conversación (imprimible a PDF).

    ✅ ISSUE-055: reusa el conversor COMPARTIDO `src/core/html_document.py`
    (una sola implementación del escapado, la misma que usa la investigación).
    """
    from src.core.html_document import html_document, md_to_html_body
    md = build_session_markdown(info, messages)
    sid = (info or {}).get("session_id") or "sin-sesión"
    meta = [
        f"Sesión: {sid}",
        f"Modelo: {(info or {}).get('model') or '—'}",
        f"Mensajes: {int((info or {}).get('message_count') or 0)}",
        f"Exportado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    marcador = "---"
    cuerpo_md = md.split(marcador, 1)[1] if marcador in md else md
    return html_document(
        title="💬 Conversación — AXIOMA",
        body_html=md_to_html_body(cuerpo_md),
        meta_lines=meta,
        footer="Generado por AXIOMA · para PDF: Imprimir → Guardar como PDF",
    )


# ═══════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════
def export_session(session_id: str, fmt: str = "json",
                   manager: Any = None, out_dir: Any = None,
                   state: Any = None) -> Optional[Path]:
    """Escribe el export en el workspace de la sesión. Devuelve la ruta o None.

    `state`: si se pasa el `SessionState` VIVO (la sesión abierta), se usan sus
    mensajes directamente. Sin esto, el export dependía de que el
    `SessionManager` todavía tuviera la sesión: si la evictó por TTL mientras la
    página seguía abierta, el documento salía **vacío en silencio**.
    """
    if fmt not in ("json", "md", "html"):
        return None
    try:
        from src.interfaces.web.states import SessionManager
        from src.core.workspace_manager import get_workspace_manager

        mgr = manager or SessionManager()
        session = mgr.get_session(session_id)
        all_info = mgr.get_all_sessions() or {}
        info = all_info.get(session_id) or {"session_id": session_id}
        # Si llega el estado VIVO se usan SUS mensajes: así el export no depende
        # de que el manager todavía tenga la sesión (evita documentos vacíos en
        # silencio si la evictó por TTL y la página sigue abierta).
        if state is not None:
            messages = list(getattr(state, "messages", None) or [])
            if hasattr(state, "to_dict"):
                try:
                    info = {**(state.to_dict() or {}), **info,
                            "session_id": session_id}
                except Exception as _d2e:  # noqa: BLE001
                    log_degraded(logger, _d2e, "export_session.state.to_dict")
        else:
            messages = list(getattr(session, "messages", None) or [])
        if fmt == "json":
            contenido = build_session_json(info, messages)
        elif fmt == "html":
            contenido = build_session_html(info, messages)
        else:
            contenido = build_session_markdown(info, messages)

        # `out_dir` existe para los TESTS: por defecto se escribe en el
        # workspace real de la sesión, y ningún test debe ensuciar ese directorio.
        target = (Path(out_dir) if out_dir is not None
                  else Path(get_workspace_manager().get_workspace(session_id)))
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sid = "".join(c for c in session_id[:8] if c.isalnum()) or "sesion"
        path = target / f"sesion_{sid}_{stamp}.{fmt}"
        path.write_text(contenido, encoding="utf-8")
        return path
    except Exception as _d2e:  # noqa: BLE001 — export no puede romper la UI
        log_degraded(logger, _d2e, f"session_admin.export({fmt})")
        return None


def _build_sessions_dialog(state: Any) -> "ui.dialog":
    """Diálogo '🗂️ Sesiones': listar, buscar, exportar y borrar."""
    dialog = ui.dialog().props('maximized=false')
    _lock = {"active": False}
    _filtro = {"q": ""}
    _confirm = {"sid": None}

    with dialog, ui.card().classes(
            'w-[46rem] max-w-full max-h-[85vh] overflow-y-auto p-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('🗂️ Sesiones').classes('text-lg font-bold')
            ui.button('✕', on_click=dialog.close).props('flat dense round size=sm')

        resumen = ui.label('').classes('text-xs text-gray-600')
        with ui.row().classes('w-full items-center gap-2'):
            buscar = ui.input(placeholder='buscar por id, modelo o usuario…'
                              ).classes('flex-1').props('outlined dense clearable')
            ui.button('Actualizar', on_click=lambda: refresh()).props('flat dense')

        lista = ui.column().classes('w-full gap-1')
        detalle = ui.label('').classes('text-xs')

        def _q() -> str:
            return (buscar.value or "")

        def _export(sid: str, fmt: str) -> None:
            path = export_session(sid, fmt)
            if path is None:
                detalle.set_text(f'❌ no se pudo exportar {sid[:12]}')
                ui.notify('❌ No se pudo exportar', color='negative')
                return
            try:
                ui.download.file(str(path))
            except Exception as _d2e:  # noqa: BLE001
                log_degraded(logger, _d2e, "sessions_dialog.ui.download")
            detalle.set_text(f"✅ exportado: {path.name}")
            ui.notify(f"✅ Exportado {path.name}")

        def _delete(sid: str, nombre: str) -> None:
            allowed, motivo = can_delete(sid, getattr(state, "session_id", None))
            if not allowed:
                ui.notify(f'⚠️ No se puede borrar: {motivo}', color='warning')
                return
            if _confirm["sid"] != sid:
                # Confirmación en dos pasos: el mismo botón confirma.
                _confirm["sid"] = sid
                ui.notify('⚠️ Tocá "Confirmar" para borrar la sesión',
                          color='warning', timeout=3)
                refresh()
                return
            _confirm["sid"] = None
            try:
                from src.interfaces.web.states import SessionManager
                ok = SessionManager().delete_session(sid)
            except Exception as _d2e:  # noqa: BLE001
                log_degraded(logger, _d2e, "sessions_dialog.delete")
                ok = False
            detalle.set_text(f"{'✅ borrada' if ok else '❌ no estaba'} {sid[:12]}")
            ui.notify('✅ Sesión borrada' if ok else '⚠️ No se encontró la sesión',
                      color='positive' if ok else 'warning')
            refresh()

        def refresh() -> None:
            """Relee las sesiones y redibuja la lista (manual, sin timers)."""
            if _lock["active"]:
                return
            _lock["active"] = True
            try:
                from src.interfaces.web.states import SessionManager
                mgr = SessionManager()
                all_sessions = mgr.get_all_sessions() or {}
                stats = mgr.get_stats() or {}
                actual = getattr(state, "session_id", None)
                ids = filter_sessions(all_sessions, _q())
                resumen.set_text(
                    f"{len(all_sessions)} sesión(es) activa(s) · "
                    f"máx {stats.get('max_sessions', '?')} · "
                    f"TTL {int((stats.get('session_ttl_seconds') or 0) // 60)} min · "
                    f"mostrando {len(ids)}")

                lista.clear()
                with lista:
                    if not ids:
                        ui.label('No hay sesiones que coincidan.').classes(
                            'text-xs text-gray-500')
                    for sid in ids:
                        info = all_sessions.get(sid) or {}
                        es_actual = bool(actual and sid == actual)
                        with ui.card().classes(
                                'w-full p-2 gap-0 ' +
                                ('bg-blue-50' if es_actual else 'bg-gray-50')):
                            with ui.row().classes('w-full items-center gap-2'):
                                ui.label('⭐ tu sesión' if es_actual else 'sesión'
                                         ).classes('text-xs font-semibold')
                                ui.space()
                                ui.button('📄 MD', on_click=lambda s=sid: _export(s, "md")
                                          ).props('flat dense size=sm')
                                ui.button('🌐 HTML', on_click=lambda s=sid: _export(s, "html")
                                          ).props('flat dense size=sm')
                                ui.button('🧾 JSON', on_click=lambda s=sid: _export(s, "json")
                                          ).props('flat dense size=sm')
                                permitido, motivo = can_delete(sid, actual)
                                etiqueta = ('Confirmar' if _confirm["sid"] == sid
                                            else '🗑️')
                                btn = ui.button(etiqueta,
                                                on_click=lambda s=sid, n=sid[:12]: _delete(s, n)
                                                ).props('flat dense size=sm')
                                if not permitido:
                                    btn.props('disable')
                                    btn.tooltip(f'No se puede borrar: {motivo}')
                                else:
                                    btn.tooltip('Borrar esta sesión (pide confirmación)')
                            for etiqueta_f, valor, css in session_rows(info):
                                ui.label(f"{etiqueta_f} {valor}").classes(css)
            except Exception as e:  # noqa: BLE001 — la UI nunca debe romper
                log_degraded(logger, e, "sessions_dialog.refresh")
                try:
                    lista.clear()
                    with lista:
                        ui.label(f'❌ no se pudieron leer las sesiones: {e}').classes(
                            'text-xs text-red')
                except Exception as _d2e:  # noqa: BLE001
                    # R3: degradar a DEBUG oculta errores de CONTRATO (lo marca
                    # el detector `silent_contract_swallow`). Se usa el helper.
                    log_degraded(logger, _d2e, "sessions_dialog.refresh.rollback")
            finally:
                _lock["active"] = False

        buscar.on('update:model-value', lambda _e: refresh())

        with ui.row().classes('w-full justify-end gap-2 mt-2'):
            ui.button('Cerrar', on_click=dialog.close).props('flat dense')

        dialog._refresh = refresh          # type: ignore[attr-defined]
        dialog._detalle = detalle          # type: ignore[attr-defined]
    return dialog


def create_sessions_dialog(state: Any) -> "ui.dialog":
    """🗂️ Diálogo de gestión de sesiones (listar/buscar/exportar/borrar)."""
    return _build_sessions_dialog(state)
