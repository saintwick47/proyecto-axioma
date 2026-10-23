#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — app_ui.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/interfaces/web/app_ui.py
#
# ✅ 2026-10-22 (refactorización 50/50): la MITAD DE PRESENTACIÓN se extrae de
#   `src/interfaces/web/app.py` a este hermano. Es HOJA: **no** importa el
#   original a nivel de módulo; lo importa DIFERIDO dentro de las funciones que
#   escriben o leen el estado compartido que sigue siendo dueño de app.py:
#     · `create_header()` escribe `app._health_badge` (lo refrescan
#       `send_message` y `_on_estado_click`, que se quedan en app.py).
#     · `_crear_barra_de_acciones()` lee `app._chat_component_global` y cablea
#       los handlers de diálogo que se quedan en app.py (`_on_estado_click`,
#       `_on_mi_hard_click`, `_on_export_click`, `_on_sessions_click`).
#     · Los wrappers `_open_*_dialog()` abren diálogos cuyo estado
#       (`_status_dialog`, `_settings_dialog`, …) vive en app.py.
#   Ese import diferido evita el ciclo de imports (AUD-CIR-001): app.py importa
#   app_ui a nivel de módulo y app_ui solo toca app en tiempo de llamada.
#
#   Qué se movió (verbatim, salvo los accesos a estado compartido):
#     · Helpers compartidos: `_dlog` (+ bootstrap de detailed_logger).
#     · Identidad del navegador: `_browser_user_id`, `_set_browser_user_id`,
#       `_stable_session_id` (las usan header, Jarvis, clear_chat y main_page).
#     · Encabezado y navegación: `create_header`, `_crear_barra_de_acciones`,
#       `_nav_btn`, `_open_dialog` y los wrappers `_open_*_dialog`,
#       `_on_configurar_click`, el toggle de Modo Jarvis y sus etiquetas.
#     · UI de usuarios: `_select_user`, `_create_user_and_back`,
#       `user_select_page`, `user_new_page`.
#     · Acción de sesión: `clear_chat`.
#
#   Qué se QUEDA en app.py (este hermano lo consume): el motor de chat
#   (`send_message`, `create_chat_container`, `_safe_scroll`), `main_page`, el
#   ENTRY POINT (`start_server`, junto al guard `__main__`), el ESTADO de los
#   diálogos y sus handlers (`_on_estado_click`, `_on_export_click`,
#   `_on_mi_hard_click`, `_on_sessions_click`), `_is_code_task` y
#   `_health_badge`.
#   La API pública no cambia: app.py RE-IMPORTA todos los nombres de este
#   archivo con los mismos nombres, así que `app.create_header`,
#   `app.clear_chat`, `app._dlog`, … siguen existiendo y apuntando a estos
#   mismos objetos.
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, Dict, Any, Callable, Union

from nicegui import ui, app as nicegui_app

from src.utils.degradation import log_degraded

from config.paths import Paths
from ..components.health_badge import HealthBadge
from .states import get_current_session
from .app_logic import _log_web_event, _log_error, _init_browser_storage

logger = logging.getLogger(__name__)


try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _DETAILED_LOGGER_AVAILABLE = True
except ImportError:
    _DETAILED_LOGGER_AVAILABLE = False
    _get_detailed_logger = None  # type: ignore[assignment]


def _dlog() -> Optional[Any]:
    """Retorna instancia del detailed_logger o None si no disponible."""
    if not _DETAILED_LOGGER_AVAILABLE:
        return None
    try:
        lgr = _get_detailed_logger()
        if lgr.is_initialized:
            return lgr
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 app.py:74] excepción degradada (intencional): {_d2e}")
    return None


# ═══════════════════════════════════════════════════════════════
# IDENTIDAD DEL NAVEGADOR — usuario activo y sesión estable por día
# ═══════════════════════════════════════════════════════════════

def _browser_user_id() -> str:
    """Usuario activo del navegador (vacío si no se eligió).

    ✅ FIX v0.6.8t: storage.browser → storage.user. En NiceGUI 3.11
    app.storage.browser es una cookie de SOLO LECTURA después de que la
    página respondió (ReadOnlyDict): escribir el usuario desde el clic se
    descartaba silenciosamente y /chat redirigía de vuelta a /. storage.user
    es server-side, modificable en cualquier momento y sobrevive la
    navegación (identificado por la cookie de sesión).
    """
    try:
        return str(nicegui_app.storage.user.get("user_id", "") or "")
    except Exception as e:
        log_degraded(logger, e, "src/interfaces/web/app.py:_browser_user_id")
        return ""


def _set_browser_user_id(username: str) -> None:
    try:
        nicegui_app.storage.user["user_id"] = username
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 app.py:663] excepción degradada (intencional): {_d2e}")


def _stable_session_id() -> Optional[str]:
    """ID de sesión ESTABLE por usuario+día (✅ v0.6.8oo — opción D).

    ANTES: SessionState generaba `nicegui_<timestamp_micro>` en cada carga de
    página → el historial persistido (LongTermMemory) quedaba bajo un id nuevo
    por reload y la hidratación (F5) nunca encontraba los mensajes viejos.
    AHORA: mismo id todo el día para el mismo usuario → la conversación
    sobrevive recargas dentro del día.
    """
    user = _browser_user_id()
    if not user:
        return None
    from datetime import datetime as _dt
    return f"{user}_{_dt.now().strftime('%Y%m%d')}"


# ═══════════════════════════════════════════════════════════════
# ENCABEZADO, BARRA DE ACCIONES Y APERTURA DE DIÁLOGOS
# ═══════════════════════════════════════════════════════════════

def _on_configurar_click() -> None:
    """Abre la pantalla "Configurar AXIOMA" (Fase 1 de contenedores).

    Muestra qué le falta al equipo (el preflight), permite descargar los modelos que faltan con un
    clic —con avance y DETENER— y cargar las claves de API del usuario.
    """
    from src.interfaces.components.configurar_axioma import abrir_configurar_axioma
    abrir_configurar_axioma()


def _nav_btn(icon: str, action: Union[str, Callable[[], None]], tooltip: str) -> None:
    """
    Helper para botones de navegación en header.
    Args:
        icon: Ícono del botón
        action: URL string o función callback
        tooltip: Texto de tooltip
    Returns:
        None
    """
    def handler() -> None:
        if isinstance(action, str):
            ui.navigate.to(action)
        else:
            action()

    ui.button(icon, on_click=handler).props('flat dense').tooltip(tooltip)


def _open_dialog(dialog: Optional[ui.dialog]) -> None:
    """
    Abre diálogo con verificación de null.
    Args:
        dialog: Objeto dialog de NiceGUI o None
    Returns:
        None
    """
    if dialog:
        _log_web_event("UI", f"Opening dialog: {dialog}")
        dialog.open()

        # ✅ v0.0.14: Log en detailed_logger
        dl = _dlog()
        if dl:
            dl.log_event("WEB_UI", f"Dialog abierto: {type(dialog).__name__}", level="INFO")
    else:
        _log_web_event("UI", "Dialog open attempted but dialog is None", level="WARNING")


def _open_status_dialog() -> None:
    """Wrapper para abrir status dialog."""
    from . import app as _app  # estado del diálogo: dueño app.py
    _open_dialog(_app._status_dialog)


def _open_settings_dialog() -> None:
    """Wrapper para abrir settings dialog."""
    from . import app as _app  # estado del diálogo: dueño app.py
    _open_dialog(_app._settings_dialog)


def _open_exit_dialog() -> None:
    """Wrapper para abrir exit dialog."""
    from . import app as _app  # estado del diálogo: dueño app.py
    _open_dialog(_app._exit_dialog)


def _open_file_dialog() -> None:
    """Wrapper para abrir el diálogo de archivos (📁)."""
    from . import app as _app  # estado del diálogo: dueño app.py
    _open_dialog(_app._file_dialog)


def _crear_barra_de_acciones() -> None:
    """✅ Fase 2 (ISSUE-135): "la conversación primero" en el encabezado.

    Visibles quedan el badge de salud (en `create_header`), *Limpiar chat* y el
    interruptor **Avanzado**; detrás de ese interruptor van las funciones de
    power-user: Archivos, Estado, Mi Hard, Exportar, Sesiones, Configuración,
    Jarvis, *Cambiar de usuario* y Salir. Antes eran **10 controles permanentes**.

    El estado del interruptor **no se persiste a propósito**: cada carga arranca
    simple. Se extrajo de `create_header` para no cruzar el umbral de método
    largo del auditor (AUD-PERF, 80 líneas).
    """
    # Estado y handlers de diálogo que siguen siendo dueños de app.py:
    # se leen a través del módulo, DIFERIDO (sin ciclo de import).
    from . import app as _app

    _avanzado_visible = {'v': False}
    avanzado = ui.row().classes('gap-2 items-center')
    avanzado.set_visibility(False)
    _btn_avanzado: Any = None

    def _toggle_avanzado() -> None:
        _avanzado_visible['v'] = not _avanzado_visible['v']
        avanzado.set_visibility(_avanzado_visible['v'])
        if _btn_avanzado is not None:
            _btn_avanzado.props(
                'unelevated dense color=primary' if _avanzado_visible['v']
                else 'flat dense')
        if _app._chat_component_global is not None:
            try:
                _app._chat_component_global.set_advanced(_avanzado_visible['v'])
            except Exception as _av_err:
                logging.getLogger(__name__).debug(
                    f"[Fase 2] avanzado en el chat: {_av_err}")

    with avanzado:
        _nav_btn('📁', _open_file_dialog, 'Archivos')
        # ✅ v0.6.8jj: "Estado" con on_click ASYNC — abre y refresca
        # (dialog.on('open') nunca disparaba en NiceGUI → diálogo vacío).
        ui.button('🏥', on_click=_app._on_estado_click).props(
            'flat dense').tooltip('Estado')
        # ✅ F1 "Mi Hard": hardware + qué modelos entran (read-only)
        ui.button('🖥️', on_click=_app._on_mi_hard_click).props(
            'flat dense').tooltip('Mi Hard')
        # ✅ Fase 1 de contenedores: pantalla "Configurar AXIOMA" (estado + descarga de modelos + claves)
        ui.button('🧩', on_click=_on_configurar_click).props(
            'flat dense').tooltip('Configurar AXIOMA (qué falta y qué instalar)')
        # ✅ F3: exportar la investigación de la sesión (MD/HTML)
        ui.button('📄', on_click=_app._on_export_click).props(
            'flat dense').tooltip('Exportar investigación')
        # ✅ ISSUE-054: listar / buscar / exportar / borrar sesiones
        ui.button('🗂️', on_click=_app._on_sessions_click).props(
            'flat dense').tooltip('Sesiones')
        _nav_btn('⚙️', _open_settings_dialog, 'Configuración')

        # ✅ FIX v0.0.13: Botón Modo Jarvis — on_click directo
        ui.button('🤖', on_click=_toggle_jarvis_from_header).props(
            'flat dense').tooltip('Modo Jarvis')

        # ✅ Fase 2: la pantalla de usuarios sigue accesible (se elige sola
        # cuando hay un único usuario).
        _nav_btn('👤', '/', 'Cambiar de usuario')
        _nav_btn('🚪', _open_exit_dialog, 'Salir')

    _nav_btn('🗑️', clear_chat, 'Limpiar chat')
    _btn_avanzado = ui.button('⚙️ Avanzado', on_click=_toggle_avanzado).props(
        'flat dense').tooltip('Mostrar/ocultar las funciones avanzadas')


def create_header() -> None:
    """
    Crea header con logo y botones de navegación.
    Returns:
        None
    """
    start = time.time()
    _log_web_event("UI", "Creating header component")

    with ui.header().classes('w-full bg-primary text-white'):
        with ui.row().classes('w-full items-center justify-between px-4'):
            logo_path = Paths.ROOT / 'src' / 'interfaces' / 'web' / 'logo.png'
            logo_src = str(logo_path) if logo_path.exists() else ''

            ui.image(logo_src).classes('h-10').on(
                'error',
                lambda e: e.source.set_text('📘') if hasattr(e, 'source') else None
            )
            ui.label('AXIOMA v0.1.8').classes('text-xl font-bold')
            # ✅ UI de usuarios (2026-08-31): muestra el usuario activo.
            _active_user = _browser_user_id()
            if _active_user:
                ui.label(f'👤 {_active_user}').classes('text-sm font-medium opacity-90')

            with ui.row().classes('gap-2'):
                # ✅ ISSUE-053: estado de salud visible sin abrir el diálogo
                # (refresco MANUAL: al cargar, tras cada respuesta y al abrir
                # "Estado" — sin timers, regla R7).
                # Estado compartido: su dueño es app.py (lo refrescan
                # send_message y _on_estado_click). Se escribe a través
                # del módulo, DIFERIDO (sin ciclo de import).
                from . import app as _app
                _app._health_badge = HealthBadge()
                _app._health_badge.create()
                _crear_barra_de_acciones()

    duration = time.time() - start
    _log_web_event("UI", "Header component created", extra={
        "duration": round(duration, 4)
    })

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_UI", "Header creado", level="INFO", extra={
            "duration_ms": round(duration * 1000, 2),
        })


# ═══════════════════════════════════════════════════════════════
# MODO JARVIS — toggle rápido desde el header
# ═══════════════════════════════════════════════════════════════

_VALID_JARVIS_MODES = ("normal", "jarvis", "voice_only", "silent")


_JARVIS_MODE_LABELS: Dict[str, str] = {
    "normal":     "\U0001f464 Normal",
    "jarvis":     "\U0001f916 Jarvis",
    "voice_only": "\U0001f399\ufe0f S\u00f3lo voz",
    "silent":     "\U0001f507 Silencioso",
}


async def _toggle_jarvis_from_header() -> None:
    """Toggle rápido Normal ↔ Jarvis desde el botón del header."""
    state = get_current_session(_stable_session_id())
    current = getattr(state, "jarvis_mode", "normal") or "normal"
    new_mode = "normal" if current != "normal" else "jarvis"

    try:
        state.jarvis_mode = new_mode
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 app.py:294] excepción degradada (intencional): {_d2e}")

    ctx = getattr(state, "context", None)
    if ctx is not None:
        if hasattr(ctx, "update_mode"):
            try:
                ctx.update_mode(new_mode)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 app.py:302] excepción degradada (intencional): {_d2e}")
        elif hasattr(ctx, "jarvis_mode"):
            try:
                ctx.jarvis_mode = new_mode
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 app.py:307] excepción degradada (intencional): {_d2e}")

    label = _JARVIS_MODE_LABELS.get(new_mode, new_mode)
    color = "purple" if new_mode != "normal" else "grey"

    # ✅ Ahora ui.notify funciona porque el contexto UI se preservó
    ui.notify(f"Modo: {label}", color=color, timeout=2, position="top-right")
    _log_web_event("JARVIS_MODE", f"Header toggle \u2192 {new_mode}")

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_UI", "Jarvis mode toggled desde header", level="INFO", extra={
            "old_mode": current,
            "new_mode": new_mode,
            "session_id": getattr(state, 'session_id', 'unknown'),
        })


# ═══════════════════════════════════════════════════════════════
# UI DE USUARIOS — selección y creación
# ═══════════════════════════════════════════════════════════════

def _select_user(username: str) -> None:
    """Elige usuario y habilita AXIOMA (abre el chat)."""
    _set_browser_user_id(username)
    _log_web_event("UI", f"Usuario seleccionado: {username}", extra={"user_id": username})
    ui.navigate.to('/chat')


def _create_user_and_back(username: str, error_label: Any) -> None:
    """Crea el usuario y vuelve a la lista para elegirlo (botón 'Crear')."""
    from src.memory.user_registry import create_user
    try:
        user = create_user(username=username)
        ui.notify(f'Usuario "{user["username"]}" creado — elegilo en la lista',
                  type='positive')
        ui.navigate.to('/')
    except ValueError as e:
        if error_label is not None:
            error_label.set_text(str(e))
        ui.notify(str(e), type='negative')


@ui.page('/')
def user_select_page() -> None:
    """
    Ventana preliminar: elegir usuario existente (clic) o crear uno nuevo.
    Los usuarios creados son normales (sin privilegios); el rol lo elige quien los crea.
    """
    _init_browser_storage()
    try:
        from src.memory.user_registry import list_users
        users = list_users()
    except Exception as e:
        _log_error(e, "user_select_page", extra={})
        users = []

    # ✅ Fase 2 (ISSUE-135): con UN solo usuario no hay nada que elegir: se entra
    # directo al chat. Antes había que tocar el nombre en cada arranque salvo que
    # el navegador recordara la sesión (y con un único usuario en `users.json`
    # —el caso de este equipo— era fricción pura). La pantalla sigue existiendo
    # y es alcanzable desde "Avanzado → 👤" para cuando haya más de uno.
    if len(users) == 1:
        _unico = (users[0] or {}).get("username") or ""
        if _unico:
            _select_user(_unico)
            return

    with ui.column().classes('w-full items-center gap-2 p-8'):
        with ui.card().classes('w-96 max-w-full'):
            ui.label('👤 AXIOMA — Seleccioná tu usuario').classes('text-xl font-bold')
            ui.label(
                'Elegí con quién vas a usar AXIOMA tocando su nombre. '
                'cada usuario tiene su memoria aparte; los nuevos son normales '
                '(sin privilegios).'
            ).classes('text-sm text-gray-500')

            if not users:
                ui.label('No hay usuarios todavía. Creá el primero.').classes('text-gray-500')

            for u in users:
                username = u.get("username", "")
                role = u.get("role", "user")
                place = u.get("place") or ""
                country = u.get("country") or ""
                extra = " · ".join(x for x in (place, country) if x)
                with ui.card().classes(
                        'w-full cursor-pointer hover:shadow-lg transition-shadow'
                ).on('click', lambda n=username: _select_user(n)):
                    with ui.row().classes('items-center justify-between w-full'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('person').classes('text-2xl')
                            ui.label(username).classes('text-lg font-semibold')
                        ui.badge(role).props(
                            f'color={("red" if role == "root" else "blue")}'
                        )
                    if extra:
                        ui.label(extra).classes('text-xs text-gray-500')

            ui.separator()
            ui.button(
                '➕ Usuario nuevo', on_click=lambda: ui.navigate.to('/user-new'),
            ).props('flat color=primary').classes('w-full')


@ui.page('/user-new')
def user_new_page() -> None:
    """
    Pantalla de creación: 4 campos (solo 'nombre de usuario' habilitado por
    ahora). Botón 'Crear' → vuelve a la lista para elegir el nuevo usuario.
    """
    _init_browser_storage()
    error_label = ui.label('').classes('text-red-500 text-sm')
    with ui.column().classes('w-full items-center gap-2 p-8'):
        with ui.card().classes('w-96 max-w-full'):
            ui.label('➕ Crear usuario nuevo').classes('text-xl font-bold')
            ui.label(
                'Completá el nombre de usuario. Correo, lugar y país quedan '
                'deshabilitados por ahora.'
            ).classes('text-sm text-gray-500')

            name_input = ui.input(
                'Nombre de usuario', placeholder='ej. maria',
            ).props('outlined')
            ui.input('Correo electrónico').props('outlined disabled')
            ui.input('Lugar').props('outlined disabled')
            ui.input('País').props('outlined disabled')

            with ui.row().classes('w-full gap-2 justify-between pt-2'):
                ui.button('Cancelar', on_click=lambda: ui.navigate.to('/')
                          ).props('flat')
                ui.button('Crear', on_click=lambda: _create_user_and_back(
                    name_input.value, error_label,
                )).props('color=primary')


# ═══════════════════════════════════════════════════════════════
# ACCIÓN DE SESIÓN — limpiar el historial del chat
# ═══════════════════════════════════════════════════════════════

def clear_chat() -> None:
    """
    Limpia historial de chat y recarga página.
    Returns:
        None
    """
    state = get_current_session(_stable_session_id())
    state.clear_history()

    _log_web_event("CHAT", "Chat cleared", extra={
        "session_id": state.session_id[:16] if state.session_id else "new"
    })
    ui.notify('✅ Historial limpiado', color='positive')
    ui.navigate.reload()

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_CHAT", "Chat limpiado", level="INFO", extra={
            "session_id": state.session_id[:16] if state.session_id else "new",
        })
