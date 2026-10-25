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
from typing import Optional, Any, List
from nicegui import ui

# ============================================================================
# CONFIGURACIÓN DE RUTAS E IMPORTS
# ============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.paths import Paths
from config.acceso_web import host_expuesto, token_de_web   # ✅ token automático (0600)
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
from .states import SessionState, get_current_session

# ── Importar lógica extraída desde app_logic.py ─────────────────────────────
from .app_logic import (
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
# ✅ REFACTOR 50/50 (2026-10-22): la mitad de PRESENTACIÓN (encabezado,
# navegación y UI de usuarios) vive en `app_ui.py`. Este módulo conserva el
# motor de chat, la página principal, el entry point y el ESTADO de los
# diálogos con sus handlers; re-importa los nombres movidos para que la API
# pública (`app.create_header`, `app.clear_chat`, `app._dlog`, …) NO cambie.
# ═══════════════════════════════════════════════════════════════
from .app_ui import (  # noqa: F401 — re-export: API pública histórica
    _dlog,
    _browser_user_id,
    _set_browser_user_id,
    _stable_session_id,
    _on_configurar_click,
    _nav_btn,
    _open_dialog,
    _open_status_dialog,
    _open_settings_dialog,
    _open_exit_dialog,
    _open_file_dialog,
    _crear_barra_de_acciones,
    create_header,
    _VALID_JARVIS_MODES,
    _JARVIS_MODE_LABELS,
    _toggle_jarvis_from_header,
    _select_user,
    _create_user_and_back,
    user_select_page,
    user_new_page,
    clear_chat,
)


# ═══════════════════════════════════════════════════════════════
# ✅ v0.1.8: _CODE_PATTERNS — solo términos inequívocamente de código.
#   Sin \bescribe\b, \bclase\b, \bcódigo\b, \bprograma\b,
#   \bmake\b, \bbuild\b, \bwrite\b, \bcrea\b, \bgenera\b (falsos
#   positivos). Guard de negación: si el fragmento previo al término
#   contiene una negación, la query NO se clasifica como código.
#   Hoy solo se usa para TELEMETRÍA en send_message.
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# ESTADO DE LOS DIÁLOGOS — dueño: app.py (test-mockeable)
# main_page los crea; los handlers de abajo los leen directo.
# ═══════════════════════════════════════════════════════════════

_status_dialog: Optional[ui.dialog] = None
_settings_dialog: Optional[ui.dialog] = None
_exit_dialog: Optional[ui.dialog] = None
_file_dialog: Optional[ui.dialog] = None   # ✅ plan_rafael → AXIOMA (2026-08-31)
_mi_hard_dialog: Optional[ui.dialog] = None   # ✅ F1 "Mi Hard" (2026)
_export_dialog: Optional[ui.dialog] = None    # ✅ F3 exportar investigación
_health_badge: Optional[HealthBadge] = None    # ✅ ISSUE-053: salud en el header
_sessions_dialog: Optional[ui.dialog] = None   # ✅ ISSUE-054: gestor de sesiones
# ✅ Fase 2 (ISSUE-135): el encabezado (en app_ui.py) necesita al chat para
# mostrar/ocultar lo avanzado (RAC / Modo Jarvis). El header se crea ANTES del
# chat, así que se guarda la referencia acá (estado compartido: dueño app.py)
# y el botón la lee al hacer clic (nunca al construir).
_chat_component_global: Optional['ChatComponent'] = None


# ═══════════════════════════════════════════════════════════════
# HANDLERS DE DIÁLOGOS — estado y apertura viven acá
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# LÓGICA DE CHAT — envío, contenedor y scroll
# ═══════════════════════════════════════════════════════════════

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

    _log_web_event("CHAT", "Message sent", extra={
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


# ═══════════════════════════════════════════════════════════════
# PÁGINA PRINCIPAL — ensambla header + chat + diálogos
# ═══════════════════════════════════════════════════════════════

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

    # ✅ Parte B.3: si AXIOMA está expuesto a la red, el banner con el código de acceso
    # queda visible mientras siga expuesto (y explica cómo volver a sólo esta computadora).
    try:
        from .app_ui import create_aviso_de_acceso
        create_aviso_de_acceso()
    except Exception as _aviso_err:  # noqa: BLE001 — el banner no puede romper la página
        logging.getLogger(__name__).debug(f"[aviso de acceso] no se pudo mostrar: {_aviso_err}")

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


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT — arranque del servidor NiceGUI
# ═══════════════════════════════════════════════════════════════

def start_server(
    host: str = '127.0.0.1',
    port: int = 8000,
    reload: bool = False
) -> None:
    """
    Inicia servidor NiceGUI con configuración optimizada.

    El default es 127.0.0.1 (sólo este equipo), igual que `main.py --web` y
    `server.py`: el middleware de Host protege `/api/*`, pero la UI NiceGUI y
    su websocket quedaban accesibles desde la LAN con el default anterior
    (`0.0.0.0`). Para exponerlo a la red hay que pedirlo explícitamente:
    `start_server(host='0.0.0.0')`.

    Args:
        host: Host del servidor
        port: Puerto del servidor
        reload: Si habilitar auto-reload para desarrollo
    Returns:
        None
    """
    _log_web_event("SERVER", f"Starting on {host}:{port}")
    logger.info(f'Starting NiceGUI server on http://{host}:{port}')

    # ✅ 2026-10-25: la misma fuente de verdad que `server.WebServer` para saber si
    # AXIOMA quedó expuesto, y generación automática del código de acceso (0600).
    # Antes este entry point ni siquiera dejaba `AXIOMA_WEB_HOST` puesto.
    import os as _os
    _os.environ["AXIOMA_WEB_HOST"] = str(host)
    if host_expuesto(host):
        token = token_de_web()
        if token:
            print(f"\n🔐 AXIOMA está abierto a otros equipos. Código de acceso: {token}")
            print("   Para volver a sólo esta computadora: AXIOMA_WEB_HOST=127.0.0.1 en el .env\n")
        else:
            print("\n⚠️  AXIOMA está abierto a otros equipos y no tiene código de acceso.")
            print("   Cerrá AXIOMA o poné AXIOMA_WEB_HOST=127.0.0.1 en el .env\n")

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


# ═══════════════════════════════════════════════════════════════
# EJECUCIÓN DIRECTA
# ═══════════════════════════════════════════════════════════════

if __name__ in {'__main__', 'mp_main'}:
    start_server()
