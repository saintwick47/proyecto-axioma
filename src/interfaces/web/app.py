#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# app.py - Interfaz Web Principal (NiceGUI)
# Ruta: /ruta/a/axioma/src/interfaces/web/app.py
# Autor: SaintWick
# Versión: 0.1.8
#
# Interfaz web principal de AXIOMA usando NiceGUI. Proporciona chat,
# configuración, modo Jarvis y gestión de sesiones. Incluye
# detección de tareas de código (telemetría), persistencia de
# configuración y logging estructurado con detailed_logger. Todo mensaje
# (código o no) pasa por Dispatcher — sin bypass de generación directa.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Union
from nicegui import ui, app as nicegui_app
from src.utils.degradation import log_degraded

# ============================================================================
# CONFIGURACIÓN DE RUTAS E IMPORTS
# ============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.paths import Paths
from .sesion_segura import secreto_de_sesion   # ✅ secreto de sesión por equipo
from src.domain.entities import Message
# ✅ v0.1.6b: Import no utilizado eliminado
from src.interfaces.components.chat import ChatComponent
from ..components.modals import (
    create_status_dialog,
    create_settings_dialog,
    create_exit_dialog,
    create_file_dialog,
    create_mi_hard_dialog,   # ✅ v0.6.9v (F1): vista read-only hardware/modelos
)
from ..components.export_dialog import create_export_dialog  # ✅ F3 (v0.6.9v)
from ..components.health_badge import HealthBadge  # ✅ ISSUE-053: badge de salud
from ..components.sessions_dialog import create_sessions_dialog  # ✅ ISSUE-054
from .states import SessionState, get_current_session, get_session_manager

# ── Importar lógica extraída desde app_logic.py ─────────────────────────────
from .app_logic import (
    LOGGER_AVAILABLE,
    _log_web_event,
    _log_error,
    _build_response_metadata,
    compact_code_artifact,   # ✅ ISSUE-055: artefactos de código al estado
    _init_browser_storage,
    load_settings,
    _log_extra,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ✅ v0.0.14: detailed_logger — import seguro (doble capa)
# ═══════════════════════════════════════════════════════════════
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


# ============================================================================
# ✅ v0.1.8: _CODE_PATTERNS — solo términos inequívocamente de código.
#   Eliminados: \bescribe\b, \bclase\b, \bcódigo\b, \bprograma\b,
#               \bmake\b, \bbuild\b, \bwrite\b, \bcrea\b, \bgenera\b
#   (eran fuente de falsos positivos: "escríbeme un poema", "código postal",
#    "clases de yoga", "no me crees nada", etc.)
#   Agregado: guard de negación — si el fragmento previo al término contiene
#   una negación, la query NO se clasifica como código.
# ============================================================================
_CODE_PATTERNS: List[str] = [
    r"\bscript\b",
    r"\bfunción\b",
    r"\bfuncion\b",
    r"\bimplementa\b",
    r"\bimplementar\b",
    r"\bdevelop\b",
    r"\bgenerate\s+code\b",
    r"\bcreate\s+(?:a\s+)?(?:function|class|script|module|api|endpoint)\b",
    r"\bwrite\s+(?:a\s+)?(?:function|class|script|module|test|code)\b",
    r"\bcode\s+(?:for|to|that|which)\b",
    r"\b(?:def|class|import|async\s+def)\s+\w",
    r"```(?:python|javascript|typescript|bash|sql|rust|go|java|cpp|c\+\+)",
]

_NEGATION_WINDOW = 40

_NEGATION_TERMS: List[str] = [
    "no ", "no\t", "nunca", "sin ", "sin\t",
    "don't", "dont", "do not", "without",
    "ni ", "jamás", "jamas",
]


def _is_code_task(content: str) -> bool:
    """
    Detecta si el mensaje del usuario es una tarea de generación de código.

    Solo activa el path de código cuando el match es inequívoco:
    - Patrones que no tienen uso común fuera de contexto técnico.
    - Guard de negación: si los _NEGATION_WINDOW caracteres anteriores
      al match contienen un término de negación, retorna False.
    """
    content_lower = content.lower()
    for pattern in _CODE_PATTERNS:
        m = re.search(pattern, content_lower)
        if not m:
            continue
        window_start = max(0, m.start() - _NEGATION_WINDOW)
        preceding = content_lower[window_start:m.start()]
        if any(neg in preceding for neg in _NEGATION_TERMS):
            continue
        return True
    return False


# ============================================================================
# VARIABLES GLOBALES PARA DIÁLOGOS (Test-mockeables)
# ============================================================================
_status_dialog: Optional[ui.dialog] = None
_settings_dialog: Optional[ui.dialog] = None
_exit_dialog: Optional[ui.dialog] = None
_file_dialog: Optional[ui.dialog] = None   # ✅ plan_rafael → AXIOMA (2026-08-31)
_mi_hard_dialog: Optional[ui.dialog] = None   # ✅ F1 "Mi Hard" (2026)
_export_dialog: Optional[ui.dialog] = None    # ✅ F3 exportar investigación
_health_badge: Optional[HealthBadge] = None    # ✅ ISSUE-053: salud en el header
_sessions_dialog: Optional[ui.dialog] = None   # ✅ ISSUE-054: gestor de sesiones
# ✅ Fase 2 (ISSUE-135): el encabezado necesita al chat para mostrar/ocultar lo
# avanzado (RAC / Modo Jarvis). El header se crea ANTES del chat, así que se
# guarda la referencia y el botón la lee al hacer clic (nunca al construir).
_chat_component_global: Optional['ChatComponent'] = None


# ============================================================================
# COMPONENTES DE UI — PATRONES UNIFICADOS (Con logging para coverage)
# ============================================================================
def _on_configurar_click() -> None:
    """Abre la pantalla "Configurar AXIOMA" (Fase 1 de contenedores).

    Muestra qué le falta al equipo (el preflight), permite descargar los modelos que faltan con un
    clic —con avance y DETENER— y cargar las claves de API del usuario.
    """
    from src.interfaces.components.configurar_axioma import abrir_configurar_axioma
    abrir_configurar_axioma()


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
        if _chat_component_global is not None:
            try:
                _chat_component_global.set_advanced(_avanzado_visible['v'])
            except Exception as _av_err:
                logging.getLogger(__name__).debug(
                    f"[Fase 2] avanzado en el chat: {_av_err}")

    with avanzado:
        _nav_btn('📁', _open_file_dialog, 'Archivos')
        # ✅ v0.6.8jj: "Estado" con on_click ASYNC — abre y refresca
        # (dialog.on('open') nunca disparaba en NiceGUI → diálogo vacío).
        ui.button('🏥', on_click=_on_estado_click).props(
            'flat dense').tooltip('Estado')
        # ✅ F1 "Mi Hard": hardware + qué modelos entran (read-only)
        ui.button('🖥️', on_click=_on_mi_hard_click).props(
            'flat dense').tooltip('Mi Hard')
        # ✅ Fase 1 de contenedores: pantalla "Configurar AXIOMA" (estado + descarga de modelos + claves)
        ui.button('🧩', on_click=_on_configurar_click).props(
            'flat dense').tooltip('Configurar AXIOMA (qué falta y qué instalar)')
        # ✅ F3: exportar la investigación de la sesión (MD/HTML)
        ui.button('📄', on_click=_on_export_click).props(
            'flat dense').tooltip('Exportar investigación')
        # ✅ ISSUE-054: listar / buscar / exportar / borrar sesiones
        ui.button('🗂️', on_click=_on_sessions_click).props(
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
                global _health_badge
                _health_badge = HealthBadge()
                _health_badge.create()
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
    _open_dialog(_status_dialog)


async def _on_estado_click() -> None:
    """
    Handler ASYNC del botón '🏥 Estado': abre el diálogo y dispara el refresh
    en el contexto del cliente (fix v0.6.8jj — ver create_status_dialog).
    """
    dialog = _status_dialog
    if dialog is None:
        _log_web_event("UI", "Status dialog is None", level="WARNING")
        return
    dialog.open()
    # ✅ ISSUE-053: al abrir "Estado" se refresca también el badge del header.
    if _health_badge is not None:
        try:
            _health_badge.refresh()
        except Exception as e:
            _log_error(e, "_on_estado_click.health_badge")
    refresh = getattr(dialog, "_refresh", None)
    if refresh is not None:
        try:
            await refresh()
        except Exception as e:
            _log_error(e, "_on_estado_click")


async def _on_mi_hard_click() -> None:
    """
    Handler ASYNC del botón '🖥️ Mi Hard': abre el diálogo y refresca
    hardware/modelos. Mismo patrón que _on_estado_click (v0.6.8jj).
    """
    dialog = _mi_hard_dialog
    if dialog is None:
        _log_web_event("UI", "Mi Hard dialog is None", level="WARNING")
        return
    dialog.open()
    refresh = getattr(dialog, "_refresh", None)
    if refresh is not None:
        try:
            await refresh()
        except Exception as e:
            _log_error(e, "_on_mi_hard_click")


def _on_export_click() -> None:
    """Handler del botón '📄 Exportar investigación'.

    SÍNCRONO a propósito: la exportación es IO local rápido (leer la lista de
    mensajes + escribir el archivo). Sin `await` no se bloquea nada del loop.
    """
    dialog = _export_dialog
    if dialog is None:
        _log_web_event("UI", "Export dialog is None", level="WARNING")
        return
    dialog.open()
    refresh = getattr(dialog, "_refresh", None)
    if refresh is not None:
        try:
            refresh()
        except Exception as e:
            _log_error(e, "_on_export_click")


def _on_sessions_click() -> None:
    """Handler del botón '🗂️ Sesiones': abre el gestor y refresca la lista.

    SÍNCRONO: releer el `SessionManager` es en memoria (sin I/O de red).
    """
    dialog = _sessions_dialog
    if dialog is None:
        _log_web_event("UI", "Sessions dialog is None", level="WARNING")
        return
    dialog.open()
    refresh = getattr(dialog, "_refresh", None)
    if refresh is not None:
        try:
            refresh()
        except Exception as e:
            _log_error(e, "_on_sessions_click")


def _open_settings_dialog() -> None:
    """Wrapper para abrir settings dialog."""
    _open_dialog(_settings_dialog)


def _open_exit_dialog() -> None:
    """Wrapper para abrir exit dialog."""
    _open_dialog(_exit_dialog)


def _open_file_dialog() -> None:
    """Wrapper para abrir el diálogo de archivos (📁)."""
    _open_dialog(_file_dialog)


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


def create_chat_container(state: SessionState) -> ChatComponent:
    """
    Crea contenedor principal de chat con ChatComponent.
    Args:
        state: Estado de sesión actual
    Returns:
        ChatComponent: Instancia creada y configurada
    """
    start = time.time()
    _log_web_event("UI", "Creating chat container", extra={
        "session_id": state.session_id[:16] if state.session_id else "new"
    })

    async def send_callback(
        textarea: Any,
        messages_container: Any,
        loading: Any,
        welcome: Any
    ) -> None:
        await send_message(textarea, state, chat_component)

    chat_component = ChatComponent(
        placeholder='Escribí tu consulta...',
        max_chars=10000,
        send_callback=send_callback,
        state=state,
    )
    chat_component.create()

    # ✅ Fase 2: el encabezado (creado antes) consulta esta referencia al usar
    # el botón "Avanzado" para mostrar/ocultar RAC y Modo Jarvis.
    global _chat_component_global
    _chat_component_global = chat_component

    duration = time.time() - start
    _log_web_event("UI", "Chat container created", extra={
        "duration": round(duration, 4),
        "session_id": state.session_id[:16] if state.session_id else "new"
    })

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_UI", "Chat container creado", level="INFO", extra={
            "session_id": state.session_id[:16] if state.session_id else "new",
            "duration_ms": round(duration * 1000, 2),
        })

    return chat_component


# ============================================================================
# DIÁLOGOS DE UI — CON LÓGICA SEPARADA PARA TESTING
# ============================================================================


# ============================================================================
# LÓGICA DE CHAT — FLUJO OPTIMIZADO Y TESTEABLE
# ============================================================================
async def send_message(
    textarea: Any,
    state: SessionState,
    chat_component: ChatComponent
) -> None:
    """
    Envía mensaje del usuario y procesa respuesta con Dispatcher.
    ✅ v0.1.6a: status_task manejada correctamente incluso si ocurre excepción antes de crearla.
    ✅ v0.1.6a: ThreadPoolExecutor cerrado adecuadamente con 'with'.
    ✅ Fase 0: eliminado el bypass de streaming directo para código (H1) —
       toda tarea pasa por Dispatcher, sin excepción.
    Args:
        textarea: Elemento textarea de NiceGUI
        state: Estado de sesión actual
        chat_component: Instancia de ChatComponent
    Returns:
        None
    """
    start_time = time.time()
    content = (textarea.value or '').strip() if hasattr(textarea, 'value') else ''

    if not content or state.is_processing:
        return

    _log_web_event("CHAT", f"Message sent", extra={
        "session_id": state.session_id[:16] if state.session_id else "new",
        "length": len(content)
    })

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_CHAT", "Mensaje recibido", level="INFO", extra={
            "session_id": state.session_id[:16] if state.session_id else "new",
            "length": len(content),
            "is_code_hint": _is_code_task(content),
        })

    # ✅ v0.1.6a: Inicializar status_task antes del try para evitar NameError en finally
    status_task = None
    _stop = [False]

    try:
        chat_component.hide_welcome()
        chat_component.add_message('user', content)

        if hasattr(textarea, 'value'):
            textarea.value = ''
            textarea.update()

        state.is_processing = True
        chat_component.set_processing(True, '📨 Mensaje recibido...')

        state.init_dispatcher()

        if not state.dispatcher:
            chat_component.add_message('assistant', '❌ Error: Dispatcher no inicializado')
            if dl:
                dl.log_event("WEB_CHAT", "Dispatcher no inicializado", level="ERROR")
            return

        message = Message(content=content, role='user')

        # ✅ v0.6.8ss (A3): streaming opt-in — settings.stream_chat_tokens
        # (default OFF). Si está activo, se crea el placeholder de streaming
        # y se registra un callback de tokens en message.metadata; el
        # dispatcher (path chat sin tools) lo consume con llm_client.chat_stream.
        # Cualquier fallo del streaming → respuesta completa normal.
        _stream_label = None
        # ✅ Fase 1: el acumulador del texto vive en el alcance del handler para
        # que DETENER pueda conservar lo ya escrito aunque no haya streaming.
        _acc: dict = {"text": ""}
        try:
            from config.settings import settings as _axioma_settings
            if getattr(_axioma_settings, "stream_chat_tokens", False):
                _stream_label = chat_component.add_streaming_placeholder()
                if _stream_label is not None:
                    def _on_token(chunk: str) -> None:
                        _acc["text"] += chunk
                        chat_component.update_streaming_content(_stream_label, _acc["text"])

                    message.metadata = {"_on_token": _on_token}
        except Exception as _stream_setup_err:
            logger.debug(f"[A3] setup de streaming omitido: {_stream_setup_err}")
            _stream_label = None

        # ✅ v0.6.9v: hook de PROGRESO para "ver el agente trabajando".
        # El dispatcher invoca message.metadata["_on_progress"]("fase") y acá
        # se muestra la fase + segundos transcurridos en el label de estado.
        _prog = {"phase": "Procesando…"}
        try:
            message.metadata = dict(message.metadata or {})
            message.metadata["_on_progress"] = lambda ph: _prog.__setitem__("phase", ph)
        except Exception as _pe:
            logging.getLogger(__name__).debug(f"[v0.6.9v] hook progreso: {_pe}")

        # Status messages rotation
        _status_msgs = ['🔍 Analizando...', '🌐 Buscando contexto...', '🤖 Generando...',
                       '⚙️ Procesando...', '📝 Escribiendo...', '🔄 Trabajando...', '⏳ Esperando...']

        async def _rotate_status() -> None:
            t0 = time.monotonic()
            idx = 0
            while not _stop[0]:
                try:
                    await asyncio.wait_for(asyncio.sleep(1.0), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if _stop[0]:
                    break
                elapsed = int(time.monotonic() - t0)
                # Si el dispatcher ya informó una fase, mostrarla; si no, rotar
                phase = _prog["phase"]
                if phase == "Procesando…":
                    idx = (idx + 1) % len(_status_msgs)
                    phase = _status_msgs[idx]
                chat_component.set_status(f"{phase} · {elapsed}s")

        status_task = asyncio.create_task(_rotate_status())

        # ── ✅ Fase 0: bypass de streaming directo eliminado (H1) ─────────────
        # Antes: si _is_code_task(content) daba true, este bloque generaba
        # código directo con OllamaClient(settings.llm_code_model), sin pasar
        # por Dispatcher/CoderAgent/validación. Tenía además un NameError real
        # (`code_model` nunca definido, ver _StreamResponse.model_used más
        # abajo en el historial) que, al ocurrir DESPUÉS de
        # finalize_streaming_message()/state.add_message() ya haber mostrado
        # el código sin validar, caía al except y volvía a llamar
        # dispatcher.process() — dos respuestas por cada consulta de código,
        # doble cómputo LLM en hardware marginal. Toda tarea (código o no)
        # va ahora por el mismo path: Dispatcher → agente correspondiente →
        # validación. _is_code_task() se conserva solo para telemetría
        # (is_code_hint/is_code_task en los logs de arriba/abajo).

        try:
            if dl:
                dl.log_agent_execution(
                    agent_name="WebDispatcher",
                    task_type="web_query",
                    state="started",
                    success=True,
                )

            # ✅ Fase 1 (ISSUE-134): la generación corre como TAREA cancelable y el
            # botón DETENER la corta. En el camino con flujo (ahora ON por
            # defecto) el cierre del generador cierra la conexión HTTP con Ollama
            # ⇒ el modelo deja de generar de verdad. En el camino sin flujo sólo
            # se libera la espera (el pedido síncrono sigue hasta terminar o
            # llegar al timeout): por eso el flujo importa también para esto.
            # ✅ ISSUE-137: token de cancelación REAL para el camino de los agentes
            # (código, validador, research). Medido: sin esto, tras cancelar la
            # tarea el runner seguía al ~590 % de CPU hasta terminar (plazo de hasta
            # 900 s en código). El cliente, al ver el token, pide en MODO FLUJO y
            # registra su respuesta; DETENER la cierra y Ollama aborta la
            # generación. Se setea en el contexto ANTES de crear la tarea y
            # `asyncio.to_thread` lo hereda.
            from src.llm.client_base import TokenCancelacion, usar_token_cancelacion
            _token_cancel = TokenCancelacion()
            usar_token_cancelacion(_token_cancel)
            _gen_task = asyncio.create_task(state.dispatcher.process(
                message,
                session_id=state.session_id,
                context=getattr(state, "context", None),
            ))
            _stop_state = {"pedido": False, "_acc": _acc, "token": _token_cancel}

            def _pedir_detencion() -> None:
                _stop_state["pedido"] = True
                _token_cancel.cancelar()      # corta el pedido HTTP en curso
                _gen_task.cancel()            # y libera la espera de la UI

            chat_component.on_stop(_pedir_detencion)
            try:
                response = await _gen_task
            except asyncio.CancelledError:
                _parcial = (_stop_state.get("_acc") or {}).get("text", "")
                if _stream_label is not None and _parcial:
                    # Se conserva lo que ya se había escrito (mejor que borrarlo).
                    chat_component.finalize_streaming_message(
                        _stream_label, _parcial + "\n\n_⏹ Detenido por el usuario._")
                elif _stream_label is not None:
                    chat_component.remove_streaming_placeholder(_stream_label)
                else:
                    chat_component.add_message('assistant', '⏹ Generación detenida.')
                if _parcial:
                    state.add_message('assistant', _parcial)
                _log_web_event("CHAT", "Generación DETENIDA por el usuario", extra={
                    "session_id": state.session_id[:16] if state.session_id else "new",
                    "chars_parciales": len(_parcial),
                    "streamed": _stream_label is not None,
                })
                return

            if response and response.is_success:
                metadata = _build_response_metadata(response, content, start_time)
                _was_streamed = bool(
                    _stream_label is not None
                    and (getattr(response, "metadata", None) or {}).get("streamed")
                )
                if _was_streamed:
                    # El mensaje ya se renderizó token a token: finalizar el
                    # placeholder como mensaje definitivo (sin duplicar).
                    chat_component.finalize_streaming_message(
                        _stream_label, response.content, metadata)
                else:
                    if _stream_label is not None:
                        chat_component.remove_streaming_placeholder(_stream_label)
                    chat_component.add_message('assistant', response.content,
                                              show_feedback=True, response_metadata=metadata)
                state.add_message('assistant', response.content,
                                  sources=metadata.get('sources'),
                                  # ✅ ISSUE-055: el código de la respuesta se
                                  # guarda para que la exportación lo incluya.
                                  artifacts=compact_code_artifact(
                                      metadata.get('code_artifact')))
                # ✅ ISSUE-053: el badge refleja lo que pasó en esta respuesta
                # (chequeo barato sobre contadores en memoria, sin I/O).
                if _health_badge is not None:
                    try:
                        _health_badge.refresh()
                    except Exception as _hb_err:
                        _log_error(_hb_err, "send_message.health_badge")

                _log_web_event("CHAT", "Message processed SUCCESS",
                              extra=_log_extra(state, start_time, response))

                if dl:
                    dl.log_agent_execution(
                        agent_name="WebDispatcher",
                        task_type=getattr(response, 'task_type', 'web_query'),
                        state="completed",
                        duration=(time.time() - start_time),
                        success=True,
                    )
            else:
                if _stream_label is not None:
                    chat_component.remove_streaming_placeholder(_stream_label)
                error = response.error if response and response.error else 'Error en procesamiento'
                chat_component.add_message('assistant', f'❌ Error: {error}')

                _log_web_event("CHAT", "Message processed FAILED", extra={
                    **_log_extra(state, start_time),
                    "error": error
                })

                if dl:
                    dl.log_agent_execution(
                        agent_name="WebDispatcher",
                        task_type="web_query",
                        state="failed",
                        duration=(time.time() - start_time),
                        success=False,
                        error=error,
                    )

        finally:
            _stop[0] = True
            if status_task is not None:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 app.py:569] excepción degradada (intencional): {_d2e}")

    except Exception as e:
        _log_error(e, "send_message", extra={"session_id": state.session_id[:16] if state.session_id else "new"})
        if _stream_label is not None:
            chat_component.remove_streaming_placeholder(_stream_label)
        chat_component.add_message('assistant', f'❌ Error: {e}')

        dl = _dlog()
        if dl:
            dl.log_error(
                error=e,
                context=f"send_message — session={state.session_id[:16] if state.session_id else 'new'}",
                function_name="send_message",
                extra_info={
                    "content_length": len(content),
                    "is_code_task": _is_code_task(content) if content else False,
                },
            )

    finally:
        state.is_processing = False
        chat_component.set_processing(False)
        asyncio.create_task(_safe_scroll())


# ============================================================================
# ✅ v0.1.6a: _safe_scroll convertida a async y await para scroll efectivo
# ============================================================================
async def _safe_scroll() -> None:
    """
    Ejecuta scroll automático con manejo seguro de contexto UI.
    Versión async corregida que realmente ejecuta el scroll.
    Returns:
        None
    """
    try:
        await ui.run_javascript(
            'const p=document.querySelector(".q-layout-page"); if(p) p.scrollTo(0,p.scrollHeight);'
        )
    except Exception as e:
        logger.debug(f"Scroll automático falló (no crítico): {e}")


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


# ============================================================================
# PÁGINAS PRINCIPALES — CON HANDLERS DE ERROR PARA COBERTURA
# ============================================================================
# ═══════════════════════════════════════════════════════════════
# UI DE USUARIOS (2026-08-31) — ventana preliminar de elección
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


@ui.page('/chat')
def main_page() -> None:
    """
    Página principal de AXIOMA con chat.
    Requiere: usuario elegido en la ventana preliminar ('/').
    Returns:
        None
    """
    _init_browser_storage()

    # ✅ UI de usuarios (2026-08-31): sin usuario elegido → volver a la
    # ventana de selección (el chat no se habilita).
    user_id = _browser_user_id()
    if not user_id:
        ui.navigate.to('/')
        return

    state = get_current_session(_stable_session_id())
    state.user_id = user_id
    try:
        from src.memory.user_registry import get_user
        _u = get_user(user_id)
        state.user_role = (_u or {}).get("role")
    except Exception:
        state.user_role = None
    load_settings(state)

    create_header()

    # ✅ Primer arranque guiado: la primera vez, la pantalla de configuración se abre SOLA y explica
    # qué falta (y lo instala con un clic). Se marca en `data/`, así se muestra una única vez.
    try:
        from src.interfaces.components.configurar_axioma import (
            abrir_configurar_axioma, es_primer_arranque, marcar_primer_arranque)
        if es_primer_arranque():
            marcar_primer_arranque()
            # `once=True`: un único disparo cuando la página está armada (nada de sondeo — R7).
            ui.timer(0.6, lambda: abrir_configurar_axioma(primer_arranque=True), once=True)
    except Exception as _primer_err:  # noqa: BLE001 — la guía no puede romper la pantalla
        logging.getLogger(__name__).debug(f"[primer arranque] no se pudo abrir la guía: {_primer_err}")
    chat_component = create_chat_container(state)

    global _status_dialog, _settings_dialog, _exit_dialog, _file_dialog
    global _mi_hard_dialog, _export_dialog, _sessions_dialog
    _status_dialog = create_status_dialog()
    _settings_dialog = create_settings_dialog(state)
    _exit_dialog = create_exit_dialog()
    _file_dialog = create_file_dialog(state, chat_component)
    _mi_hard_dialog = create_mi_hard_dialog()
    _export_dialog = create_export_dialog(state)
    _sessions_dialog = create_sessions_dialog(state)

    _log_web_event("UI", "Main page loaded", extra={
        "session_id": state.session_id[:16] if state.session_id else "new"
    })

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_UI", "Main page cargada", level="INFO", extra={
            "session_id": state.session_id[:16] if state.session_id else "new",
        })


# ============================================================================
# ENTRY POINT — CON LOGGING PARA COBERTURA
# ============================================================================
def start_server(
    host: str = '0.0.0.0',
    port: int = 8000,
    reload: bool = False
) -> None:
    """
    Inicia servidor NiceGUI con configuración optimizada.
    Args:
        host: Host del servidor
        port: Puerto del servidor
        reload: Si habilitar auto-reload para desarrollo
    Returns:
        None
    """
    _log_web_event("SERVER", f"Starting on {host}:{port}")
    logger.info(f'Starting NiceGUI server on http://{host}:{port}')

    # ✅ v0.0.14: Log en detailed_logger
    dl = _dlog()
    if dl:
        dl.log_event("WEB_SERVER", f"Iniciando servidor en {host}:{port}", level="INFO", extra={
            "host": host,
            "port": port,
            "reload": reload,
        })

    favicon = str(Paths.ROOT / 'src' / 'interfaces' / 'web' / 'logo.png')
    if not Path(favicon).exists():
        favicon = None

    ui.run(
        host=host,
        port=port,
        reload=reload,
        show=False,
        title='AXIOMA v0.1.8',
        favicon=favicon,
        storage_secret=secreto_de_sesion(),      # ✅ 2026-10-07: secreto propio del equipo
        reconnect_timeout=300
    )

    _log_web_event("SERVER", f"Server started on {host}:{port}")


# ============================================================================
# EJECUCIÓN DIRECTA
# ============================================================================
if __name__ in {'__main__', 'mp_main'}:
    start_server()
