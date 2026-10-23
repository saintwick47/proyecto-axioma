#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.1 — Componente de Chat (NiceGUI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/components/chat.py
# Autor: SaintWick
# ✅ FIX v0.1.0: Subida de archivos → se almacena internamente, NO en textarea.
#    El usuario escribe su instrucción libremente y se envían juntos.
# ✅ FIX v0.1.0: await e.file.read() (NiceGUI 2.x StarletteUploadFile)
# ✅ FIX v0.1.0: RuntimeError "slot stack is empty" — cola UI + ui.timer
# ✅ FIX: Separación correcta código/explicación usando split por bloques
# ✅ FIX: e.args.get('shiftKey') en lugar de e.shift_key
# ✅ FIX: props(remove=...) en set_processing
# ✅ FIX v0.1.1: Eliminado truncamiento a 8000 chars → 50000 chars.
#    num_ctx dinámico en client_base.py maneja la ventana de contexto.
# ✅ v0.2.0 (Modo Jarvis): Comando /modo — activa/desactiva Jarvis desde chat.
#    Modos válidos: normal | jarvis | voice_only | silent
#    Propaga el cambio a SessionContext si state lo expone.
# ✅ CORREGIDO v0.1.2 (2026-06-24):
#    - Model text fallback en create(): vicgalle/prometheus → qwen3:8b
# ═══════════════════════════════════════════════════════════════
import asyncio
import base64
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Tuple

from nicegui import ui, events, context as _ng_context

# ✅ Fase 5: reusa el writer de archivos ya probado de app_logic.py — no
# duplica la lógica de guardado, solo el botón de descarga es nuevo acá.
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass

logger = logging.getLogger(__name__)

# ✅ v0.6.9w (REFACTOR 50/50): adjuntos + helpers de logging compartidos.
# Viven en el módulo hermano `chat_attachments.py`; la dependencia va en un
# solo sentido (chat → chat_attachments). Se re-importan con los MISMOS
# nombres para conservar la API pública: consumidores y tests que hacen
# `from src.interfaces.components.chat import _is_image_attachment` (o usan
# MAX_FILE_CHARS / _detect_image_ext) siguen funcionando igual.
from .chat_attachments import (  # noqa: E402,F401 — re-export de compatibilidad
    ChatAttachmentsMixin,
    MAX_FILE_CHARS,
    _IMAGE_EXTS,
    _IMAGE_MAGIC,
    _detect_image_ext,
    _is_image_attachment,
    _log_error,
    _log_event,
    _looks_binary,
    _ocr_scanned_pdf,
    _pdf_parece_escaneado,
    _safe_log,
)


# ── Modos Jarvis válidos (espejo de context.py VALID_JARVIS_MODES) ────────────
_VALID_JARVIS_MODES = frozenset({"normal", "jarvis", "voice_only", "silent"})

_JARVIS_MODE_LABELS: Dict[str, str] = {
    "normal":     "👤 Normal",
    "jarvis":     "🤖 Jarvis",
    "voice_only": "🎙️ Sólo voz",
    "silent":     "🔇 Silencioso",
}


# ═══════════════════════════════════════════════════════════════
# CLASE: ChatMessage
# ═══════════════════════════════════════════════════════════════
class ChatMessage:
    """Representa un mensaje en el chat."""

    def __init__(self, role: str, content: str, timestamp: Optional[str] = None):
        self.role = role
        self.content = content
        self.timestamp = timestamp or datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}


# ═══════════════════════════════════════════════════════════════
# CLASE: FeedbackWidget
from .chat_render import ChatRenderMixin  # noqa: E402


class ChatComponent(ChatAttachmentsMixin, ChatRenderMixin):
    """Componente de chat reutilizable para NiceGUI."""

    # Captura: (lang_opcional, código)
    CODE_PATTERN = re.compile(r'```(\w+)?\n(.*?)```', re.DOTALL)
    # Captura: "ARCHIVO: nombre.py" con newline opcional
    FILE_PATTERN = re.compile(r'^ARCHIVO:\s*(\S+\.\w+)\s*\n?', re.MULTILINE | re.IGNORECASE)
    # Delimitador completo para split: ```lang\n...código...```
    BLOCK_SPLIT_PATTERN = re.compile(r'(```(?:\w+)?\n.*?```)', re.DOTALL)

    def __init__(self, placeholder: str = 'Escribí tu consulta...',
                 max_chars: int = 10000, send_callback: Optional[Callable] = None,
                 state: Optional[Any] = None):
        self.placeholder = placeholder
        self.max_chars = max_chars
        self.send_callback = send_callback
        self.state = state
        self.elements: Dict[str, Any] = {}
        self.messages: List[ChatMessage] = []
        self._message_count = 0
        self.feedback_widgets: List[FeedbackWidget] = []
        self._ng_client: Optional[Any] = None

        # Cola de actualizaciones UI para background tasks
        self._pending_ui_updates: List[Callable] = []

        # ✅ Fase 1: callback del botón DETENER (lo registra app.py)
        self._on_stop_cb: Optional[Callable] = None

        # Archivo adjunto almacenado internamente
        self._attached_file_name: Optional[str] = None
        self._attached_file_content: Optional[str] = None

        # Imagen adjunta (v0.6.9p) — base64 para qwen3-vl
        self._attached_image_b64: Optional[str] = None
        self._attached_image_name: Optional[str] = None

        # Instanciar CommandParser para reutilización en UI
        try:
            from src.interfaces.cli.command_parser import CommandParser
            self._command_parser = CommandParser(console=None, session_state={})
        except Exception:
            self._command_parser = None

    def create(self) -> Dict[str, Any]:
        """Construye la UI del componente."""

        try:
            self._ng_client = _ng_context.client
            logger.info("ChatComponent: NiceGUI client capturado correctamente")
        except RuntimeError:
            self._ng_client = None
            logger.warning("ChatComponent: No se pudo capturar contexto NiceGUI en create()")

        elements = {}
        with ui.column().classes('w-full flex-1 p-4'):
            # Pantalla de bienvenida
            with ui.column().classes('w-full items-center justify-center flex-1').style('display: flex;') as welcome:
                ui.image('/static/logo.png').classes('h-24').on(
                    'error', lambda e: e.source.set_text('📘') if hasattr(e, 'source') else None
                )
                ui.label('Bienvenido a AXIOMA').classes('text-2xl font-bold')
                ui.label('Sistema Multi-Agente de IA').classes('text-lg text-gray-600')
                with ui.row().classes('gap-4 mt-4'):
                    for chip in ['💻 Código', '🔍 Investigación', '📊 Análisis', '✅ Validación']:
                        ui.chip(chip).props('outline')
                # ✅ CORREGIDO: Fallback a qwen3:8b
                model_text = f'🤖 {self.state.model}' if self.state else '🤖 qwen3:8b'
                elements['model_indicator'] = ui.label(model_text).classes('mt-4 text-sm text-gray-500')
                elements['welcome'] = welcome

            # Contenedor de mensajes
            with ui.column().classes('w-full flex-1 overflow-y-auto').style('display: none;') as messages_container:
                pass
            elements['messages'] = messages_container

            # Indicador de archivo adjunto
            with ui.row().classes('w-full items-center gap-2 px-4 py-1 bg-blue-50 rounded').style('display: none;') as attachment_bar:
                ui.icon('attach_file').classes('text-blue-500 text-sm')
                attachment_label = ui.label('').classes('text-xs text-blue-700 font-medium flex-1')
                attachment_clear = ui.button('✕', on_click=lambda: self._clear_attachment())
                attachment_clear.props('flat dense round size=xs color=negative')
            elements['attachment_bar'] = attachment_bar
            elements['attachment_label'] = attachment_label

            # Área de input
            with ui.row().classes('w-full items-end gap-2 p-4 border-t'):
                textarea = ui.textarea(placeholder=self.placeholder).classes('flex-1').props('outlined rows=1 autogrow')

                async def _dispatch_input():
                    text = textarea.value.strip()
                    if not text and not self._attached_file_content \
                            and not self._attached_image_b64:
                        return

                    # ✅ v0.6.9p: imagen adjunta → path de visión (qwen3-vl).
                    # No se mezcla como texto: el LLM de chat no ve píxeles.
                    if self._attached_image_b64:
                        textarea.value = ''
                        await self._process_vision_message(text)
                        return

                    # Si hay archivo adjunto, combinar con la instrucción
                    if self._attached_file_content:
                        ext = self._attached_file_name.split('.')[-1] if self._attached_file_name else 'txt'
                        file_block = (
                            f"Archivo adjunto: {self._attached_file_name}\n"
                            f"```{ext}\n{self._attached_file_content}\n```\n"
                        )
                        if text:
                            combined = f"{file_block}\n{text}"
                        else:
                            combined = file_block
                        textarea.value = combined
                        self._clear_attachment()

                    text = textarea.value.strip()

                    # ✅ v0.6.8ww: `/plan <consulta>` NO es comando de UI — es
                    # el pipeline F3 del dispatcher. Se deja pasar como mensaje
                    # normal (el dispatcher lo detecta por el prefijo y arma el
                    # pipeline). Antes caía en CommandParser → "Comando
                    # desconocido: /plan" y nunca llegaba al dispatcher.
                    if text.startswith('/') and not (text.lower() == '/plan' or text.lower().startswith('/plan ')):
                        textarea.value = ''
                        await self._dispatch_ui_command(text)
                        return

                    if self.send_callback:
                        await self.send_callback(
                            textarea,
                            elements.get('messages'),
                            elements.get('loading'),
                            elements.get('welcome'),
                        )

                textarea.on('keydown.enter.prevent', lambda e: asyncio.create_task(_dispatch_input()) if not e.args.get('shiftKey') else None)

                # Botón de subida de archivos
                ui.upload(label='📎', on_upload=self._handle_file_upload, auto_upload=True).props('flat dense color=grey').classes('self-center')

                # ✅ Fase 2 (ISSUE-135): lo AVANZADO vive detrás del botón
                # "Avanzado" del encabezado: RAC y Modo Jarvis son funciones de
                # power-user y ocupaban lugar permanente en la fila de escritura.
                # Oculto por defecto ⇒ la primera pantalla es sólo conversación.
                advanced_group = ui.row().classes('items-center gap-1 self-center')
                advanced_group.set_visibility(False)
                with advanced_group:
                    # Botón y selector para /rac
                    with ui.row().classes('items-center gap-1 self-center'):
                        rac_select = ui.select(['completa', 'parcial'], value='completa').props('dense flat').classes('w-28')
                        ui.button('🛡️ RAC', on_click=lambda: asyncio.create_task(self._execute_rac(rac_select.value))).props('flat dense color=warning')

                    # Botón de Modo Jarvis
                    jarvis_btn = ui.button(
                        '🤖',
                        on_click=lambda: asyncio.create_task(self._toggle_jarvis_mode()),
                    ).props('flat dense color=purple').classes('self-center')
                    jarvis_btn.tooltip('Activar/Desactivar Modo Jarvis')
                    elements['jarvis_btn'] = jarvis_btn
                elements['advanced_group'] = advanced_group

                send_btn = ui.button('🚀', on_click=lambda: asyncio.create_task(_dispatch_input())).props('round')

                # ✅ Fase 1 (ISSUE-134): botón de DETENER. Antes, una vez lanzada
                # la generación había que esperar los 60-135 s completos (medido:
                # 83-135 s por respuesta en este equipo). Visible sólo mientras
                # se procesa; el app.py registra qué hacer con `on_stop(...)`.
                stop_btn = ui.button('⏹', on_click=self._on_stop_click).props(
                    'round color=negative').classes('self-center')
                stop_btn.tooltip('Detener la generación en curso')
                stop_btn.style('display: none;')
                elements['stop_btn'] = stop_btn

                char_count = ui.label(f'0/{self.max_chars}').classes('text-xs text-gray-500')

                def _update_char_count(e):
                    count = len(e.value) if hasattr(e, 'value') and e.value else 0
                    char_count.set_text(f'{count}/{self.max_chars}')
                    char_count.style(
                        'color: var(--error-color)' if count > self.max_chars * 0.9
                        else 'color: var(--text-secondary)'
                    )

                textarea.on('input', _update_char_count)
                elements.update({'textarea': textarea, 'send_btn': send_btn, 'char_count': char_count})

            # Indicador de carga con estado descriptivo
            with ui.column().classes('w-full items-center gap-1').style('display: none;') as loading:
                with ui.row().classes('items-center gap-3'):
                    ui.spinner('dots', size='md', color='primary')
                    status_label = ui.label('Procesando...').classes('text-sm text-gray-500 italic')
                elements['status_label'] = status_label
            elements['loading'] = loading

            # Indicador de Modo Jarvis (badge visible cuando está activo)
            with ui.row().classes('w-full items-center justify-center gap-2 py-1').style('display: none;') as jarvis_indicator:
                ui.icon('smart_toy').classes('text-purple-500 text-sm')
                jarvis_mode_label = ui.label('').classes('text-xs text-purple-700 font-semibold')
            elements['jarvis_indicator'] = jarvis_indicator
            elements['jarvis_mode_label'] = jarvis_mode_label

            # Timer que procesa la cola de actualizaciones UI.
            # ✅ ISSUE-053/R7: arranca **inactivo** y sólo se activa cuando hay
            # algo encolado (`_schedule_ui`). Antes latía 10 veces por segundo
            # SIEMPRE, aunque la cola estuviera vacía (carga permanente en un
            # equipo CPU-only). Lo detectó el test de política `ui.timer` de
            # `test_axioma_23_observabilidad`.
            self._update_timer = ui.timer(0.1, self._process_pending_updates)
            self._update_timer.active = False

        self.elements = elements
        return elements

    # ── Helper: Ejecutar operación UI de forma segura ──────────────
    def _ui_safe(self, fn: Callable) -> None:
        if self._ng_client is not None:
            try:
                with self._ng_client:
                    fn()
            except RuntimeError as e:
                if "slot stack" in str(e).lower():
                    self._schedule_ui(fn)
                else:
                    raise
        else:
            try:
                fn()
            except RuntimeError:
                self._schedule_ui(fn)

    # ── Cola de actualizaciones UI ─────────────────────────────────
    def _schedule_ui(self, fn: Callable) -> None:
        self._pending_ui_updates.append(fn)
        # Solo late mientras hay trabajo pendiente (ver _process_pending_updates).
        self._wake_update_timer()

    def _wake_update_timer(self) -> None:
        """Activa el timer de la cola si está inactivo (nunca lanza)."""
        try:
            timer = getattr(self, '_update_timer', None)
            if timer is not None and not timer.active:
                timer.activate()
        except Exception as _d2e:  # noqa: BLE001
            log_degraded(logger, _d2e, "ChatComponent._wake_update_timer")

    def _process_pending_updates(self) -> None:
        if not self._pending_ui_updates:
            # Se apaga solo cuando no queda nada: 0 wakeups en reposo.
            self._sleep_update_timer()
            return
        updates = self._pending_ui_updates[:]
        self._pending_ui_updates.clear()
        for fn in updates:
            try:
                fn()
            except Exception as e:
                logger.error(f"Error procesando actualización UI encolada: {e}", exc_info=True)
        # Re-chequeo: si llegó algo mientras drenábamos, sigue activo. No hay
        # ventana de pérdida: `_schedule_ui` activa el timer *después* de
        # encolar, así que un encolado posterior al chequeo vuelve a activarlo.
        if not self._pending_ui_updates:
            self._sleep_update_timer()

    def _sleep_update_timer(self) -> None:
        """Desactiva el timer de la cola (nunca lanza)."""
        try:
            timer = getattr(self, '_update_timer', None)
            if timer is not None and timer.active:
                timer.deactivate()
        except Exception as _d2e:  # noqa: BLE001
            log_degraded(logger, _d2e, "ChatComponent._sleep_update_timer")

    # ── Toggle rápido de Modo Jarvis desde botón ─────────────────────────────
    async def _toggle_jarvis_mode(self) -> None:
        """
        Alterna entre 'normal' y 'jarvis'.
        Si el modo actual ya es jarvis/voice_only/silent → vuelve a normal.
        Si es normal → activa jarvis.
        """
        current = "normal"
        if self.state and hasattr(self.state, 'jarvis_mode'):
            current = getattr(self.state, 'jarvis_mode', "normal")
        elif self.state and hasattr(self.state, 'context') and self.state.context:
            current = getattr(self.state.context, 'jarvis_mode', "normal")

        new_mode = "normal" if current != "normal" else "jarvis"
        await self._set_jarvis_mode(new_mode)

    # ── Command Dispatcher UI ─────────────────────────────────────────
    async def _dispatch_ui_command(self, text: str):
        parts = text.split()
        command = parts[0].lower()
        args = parts[1:]

        if command == '/rac':
            scope = args[0] if args and args[0] in ['completa', 'parcial'] else 'completa'
            await self._execute_rac(scope)
            return

        # ── /modo [normal|jarvis|voice_only|silent] ───────────────────
        if command == '/modo':
            if not args:
                current = "normal"
                if self.state and hasattr(self.state, 'jarvis_mode'):
                    current = getattr(self.state, 'jarvis_mode', "normal")
                elif self.state and hasattr(self.state, 'context') and self.state.context:
                    current = getattr(self.state.context, 'jarvis_mode', "normal")
                label = _JARVIS_MODE_LABELS.get(current, current)
                self._ui_safe(lambda: self.hide_welcome())
                self._ui_safe(lambda: self.add_message(
                    'system',
                    f"Modo actual: **{label}**\n\n"
                    f"Modos disponibles: `normal` · `jarvis` · `voice_only` · `silent`\n"
                    f"Uso: `/modo jarvis`"
                ))
                return

            requested_mode = args[0].lower()
            if requested_mode not in _VALID_JARVIS_MODES:
                self._ui_safe(lambda: self.hide_welcome())
                self._ui_safe(lambda: self.add_message(
                    'system',
                    f"❌ Modo inválido: `{requested_mode}`\n\n"
                    f"Modos válidos: `normal` · `jarvis` · `voice_only` · `silent`"
                ))
                return

            await self._set_jarvis_mode(requested_mode)
            return

        # ── Resto de comandos → CommandParser ────────────────────────
        self._ui_safe(lambda: self.hide_welcome())
        self._ui_safe(lambda: self.add_message('user', text))

        if self._command_parser:
            try:
                result = await asyncio.to_thread(self._command_parser.handle, text)
                if result:
                    output = getattr(result, 'output', str(result))
                    success = getattr(result, 'success', True)
                    role = 'assistant' if success else 'system'
                    if output:
                        self._schedule_ui(lambda: self.add_message(role, output))
                    else:
                        self._schedule_ui(lambda: self.add_message('system', f"❓ Comando ejecutado sin salida: {command}"))
                else:
                    self._schedule_ui(lambda: self.add_message('system', f"❓ Comando no reconocido o sin salida: {command}"))
            except Exception as e:
                logger.error(f"Error en CommandParser: {e}")
                # ✅ ISSUE-085: capturar el texto AHORA. PEP 3110 borra `e` al
                # salir del `except`, y este callback se ejecuta DESPUÉS (cola
                # `_pending_ui_updates` + `ui.timer`) → el lambda lanzaba
                # `NameError: cannot access free variable 'e'` y **enmascaraba el
                # error original** justo en el manejador de errores.
                _error_txt = str(e)
                self._schedule_ui(lambda: self.add_message('system', f"❌ Error ejecutando comando: {_error_txt}"))
        else:
            self._schedule_ui(lambda: self.add_message('system', "❌ CommandParser no disponible en esta sesión."))

    # ── Aplicar cambio de modo Jarvis ─────────────────────────────────────────
    async def _set_jarvis_mode(self, mode: str) -> None:
        """
        Propaga el cambio de modo a state y/o SessionContext, y actualiza la UI.

        Orden de propagación:
          1. state.jarvis_mode (si el state lo expone directamente)
          2. state.context.jarvis_mode (si state tiene SessionContext)
          3. Sólo UI (fallback si state no existe)
        """
        propagated = False

        if self.state:
            # Intentar propagación directa en state
            if hasattr(self.state, 'jarvis_mode'):
                try:
                    self.state.jarvis_mode = mode
                    propagated = True
                except Exception as e:
                    logger.warning(f"/modo: no se pudo setear state.jarvis_mode: {e}")

            # Intentar propagación en SessionContext anidado
            if hasattr(self.state, 'context') and self.state.context is not None:
                ctx = self.state.context
                if hasattr(ctx, 'update_mode'):
                    try:
                        ctx.update_mode(mode)
                        propagated = True
                    except Exception as e:
                        logger.warning(f"/modo: SessionContext.update_mode falló: {e}")
                elif hasattr(ctx, 'jarvis_mode'):
                    try:
                        ctx.jarvis_mode = mode
                        propagated = True
                    except Exception as e:
                        logger.warning(f"/modo: no se pudo setear ctx.jarvis_mode: {e}")

        label = _JARVIS_MODE_LABELS.get(mode, mode)

        # ✅ 2026-08-25: sincronizar el pipeline de voz con el modo elegido.
        # jarvis/voice_only → activa audio; normal/silent → lo desactiva.
        try:
            from src.interfaces.web.voice_runtime import set_voice
            set_voice(mode in ("jarvis", "voice_only"))
        except Exception as e:
            logger.warning(f"/modo: no se pudo sincronizar VoicePipeline: {e}")

        # Actualizar indicador visual en la barra
        self._update_jarvis_indicator(mode)

        # Notificación de cambio
        color = 'purple' if mode != 'normal' else 'grey'
        self._ui_safe(lambda: ui.notify(
            f"Modo cambiado a {label}",
            color=color,
            timeout=3,
            position='top-right',
        ))

        # Mensaje en chat confirmando el cambio
        propagation_note = "" if propagated else "\n⚠️ El estado no pudo propagarse al contexto de sesión."
        self._ui_safe(lambda: self.hide_welcome())
        self._ui_safe(lambda: self.add_message(
            'system',
            f"✅ {label} activado.{propagation_note}"
        ))

        _log_event("JARVIS_MODE", f"Modo cambiado a: {mode}", extra={"propagated": propagated})

    def _update_jarvis_indicator(self, mode: str) -> None:
        """Actualiza el badge de modo Jarvis en la UI."""
        indicator = self.elements.get('jarvis_indicator')
        mode_label = self.elements.get('jarvis_mode_label')
        jarvis_btn = self.elements.get('jarvis_btn')

        if mode == 'normal':
            if indicator:
                indicator.style('display: none;')
            if jarvis_btn:
                jarvis_btn.props('flat dense color=grey')
        else:
            label = _JARVIS_MODE_LABELS.get(mode, mode)
            if mode_label:
                mode_label.set_text(label)
            if indicator:
                indicator.style('display: flex;')
            if jarvis_btn:
                jarvis_btn.props('flat dense color=purple')

    # ── Ejecución de /rac desde UI ──────────────────────────────────
    async def _execute_rac(self, scope: str):
        self._schedule_ui(lambda: self.hide_welcome())
        self._schedule_ui(lambda: self.add_message('user', f'/rac {scope}'))
        self._schedule_ui(lambda: self.set_processing(True, status=f'Revisión Axioma {scope.title()} en progreso...'))

        try:
            from src.agents.analyst import AnalystAgent
            agent = AnalystAgent(temperature=0.2, enable_memory=False)

            result = await asyncio.to_thread(agent.review_system, scope)

            success = result.get("success", False) if isinstance(result, dict) else False

            if success:
                report_path = result.get('report_path', 'N/A')
                files_analyzed = result.get('files_analyzed', 0)

                self._schedule_ui(lambda: ui.notify(
                    "✅ RAC finalizado!",
                    color='positive',
                    timeout=15,
                    position='top',
                    closeBtn=True,
                ))
                self._schedule_ui(lambda: self.add_message('assistant',
                    f"✅ **RAC {scope.title()} finalizado con éxito.**\n\n"
                    f"📄 Archivos analizados: {files_analyzed}\n"
                    f"📝 Reporte generado en: `{report_path}`\n\n"
                    f"Puedes abrir el archivo Markdown para ver todas las observaciones."
                ))
            else:
                error_msg = result.get('error', 'Error desconocido') if isinstance(result, dict) else 'Resultado inválido'
                self._schedule_ui(lambda: ui.notify(
                    "❌ RAC falló",
                    color='negative',
                    timeout=10,
                    position='top',
                ))
                self._schedule_ui(lambda: self.add_message('assistant',
                    f"❌ **RAC {scope.title()} falló.**\n\n"
                    f"Error: `{error_msg}`\n\n"
                    f"Revisa los logs en la terminal para más detalles."
                ))

        except Exception as e:
            logger.exception("Error inesperado ejecutando RAC desde UI")
            # ✅ ISSUE-085: mismo caso que arriba — el callback corre después de
            # que el `except` terminó, así que `e` ya no existe. Se captura ahora.
            _error_txt = str(e)
            self._schedule_ui(lambda: ui.notify(
                f"❌ Error crítico en RAC",
                color='negative',
                timeout=10,
            ))
            self._schedule_ui(lambda: self.add_message('assistant', f"❌ Error crítico ejecutando RAC:\n`{_error_txt}`"))

        finally:
            self._schedule_ui(lambda: self.set_processing(False))

    def add_message(self, role: str, content: str,
                    show_feedback: bool = False,
                    response_metadata: Optional[Dict] = None) -> None:
        messages_container = self.elements.get('messages')
        if not messages_container:
            return

        self.messages.append(ChatMessage(role=role, content=content))
        self._message_count += 1

        try:
            with messages_container:
                with ui.row().classes('w-full ' + ('justify-end' if role == 'user' else 'justify-start')):
                    bg = 'bg-blue-100' if role == 'user' else 'bg-gray-100'
                    avatar = '👤' if role == 'user' else '📘'
                    name = 'Vos' if role == 'user' else 'AXIOMA'

                    if role in ('assistant', 'system'):
                        self._render_assistant_message(content, bg, response_metadata)
                    else:
                        ui.chat_message(content, name=name, stamp=avatar).classes(bg)

            # ✅ v0.6.9v (P0 del plan nuevo): fuentes + panel de transparencia +
            # feedback, en un solo helper compartido por las DOS rutas de render
            # (mensaje completo y streaming). Antes el streaming no mostraba ni
            # fuentes ni widget de feedback.
            self._render_message_extras(messages_container, content,
                                        response_metadata,
                                        show_feedback=show_feedback)

        except Exception as e:
            _log_error(e, "ChatComponent.add_message", extra={"role": role})

    def _render_message_extras(self, host: Any, content: str,
                               response_metadata: Optional[Dict],
                               show_feedback: bool = True) -> None:
        """Bloque 📎 Fuentes + colapsable 🔍 transparencia + feedback.

        Compartido por `add_message` (respuesta completa) y por
        `finalize_streaming_message` (respuesta streameada) — así el usuario ve
        lo mismo en ambos caminos. Nunca propaga errores: la UI no se rompe por
        un problema de metadata.
        """
        if not response_metadata:
            return
        try:
            from .transparency import render_transparency_block

            _srcs = [s for s in (response_metadata.get('sources') or [])
                     if isinstance(s, str) and s.strip()]
            if _srcs:
                with host:
                    self._render_sources_block(_srcs)

            fw = None
            if show_feedback:
                fw = FeedbackWidget(session_state=self.state,
                                    response_metadata=response_metadata,
                                    response_text=content)
            with host:
                exp = render_transparency_block(
                    response_metadata,
                    feedback_factory=(fw.create if fw is not None else None))
                if fw is not None:
                    if exp is None:      # sin metadata útil: widget suelto
                        fw.create()
                    fw.show()
            if fw is not None:
                self.feedback_widgets.append(fw)
        except Exception as e:  # noqa: BLE001 — la UI nunca debe romper
            log_degraded(logger, e, "ChatComponent._render_message_extras")

    def hide_welcome(self) -> None:
        if w := self.elements.get('welcome'):
            w.style('display: none;')
        if m := self.elements.get('messages'):
            m.style('display: flex;')

    def clear(self) -> None:
        self.messages.clear()
        self._message_count = 0
        self.feedback_widgets.clear()
        self._clear_attachment()
        if m := self.elements.get('messages'):
            m.clear()
            m.style('display: none;')
        if w := self.elements.get('welcome'):
            w.style('display: flex;')
        if t := self.elements.get('textarea'):
            t.value = ''

    def set_status(self, message: str) -> None:
        if sl := self.elements.get('status_label'):
            sl.set_text(message)

    def set_processing(self, processing: bool, status: str = 'Procesando...') -> None:
        if l := self.elements.get('loading'):
            l.style('display: flex;' if processing else 'display: none;')
        if processing:
            self.set_status(status)
        if t := self.elements.get('textarea'):
            if processing:
                t.props('readonly')
            else:
                t.props(remove='readonly')
        if b := self.elements.get('send_btn'):
            if processing:
                b.props('disable')
            else:
                b.props(remove='disable')
        # ✅ Fase 1: el botón de detener sólo existe mientras hay algo en curso.
        if s := self.elements.get('stop_btn'):
            s.style('display: inline-flex;' if processing else 'display: none;')

    def on_stop(self, callback) -> None:
        """Registra qué hacer cuando el usuario aprieta DETENER (lo cablea app.py)."""
        self._on_stop_cb = callback

    def set_advanced(self, visible: bool) -> None:
        """✅ Fase 2: muestra/oculta lo avanzado (RAC, Modo Jarvis).

        La pantalla por defecto es sólo conversación; el encabezado tiene el
        botón "Avanzado" que llama acá. No rompe nada si el grupo no existe
        (por ejemplo en pruebas que construyen el componente a medias).
        """
        if g := self.elements.get('advanced_group'):
            g.set_visibility(bool(visible))

    def _on_stop_click(self) -> None:
        """Avisa al llamador; el botón NUNCA debe romper la conversación."""
        cb = getattr(self, '_on_stop_cb', None)
        if callable(cb):
            try:
                cb()
            except Exception as e:
                log_degraded(logger, e, "ChatComponent._on_stop_click")
        else:
            logger.debug("[Fase 1] DETENER apretado sin callback registrado")

    def update_model_indicator(self, model: str) -> None:
        if m := self.elements.get('model_indicator'):
            m.set_text(f'🤖 {model}')

    def get_message_count(self) -> int:
        return self._message_count

    def get_messages(self) -> List[ChatMessage]:
        return self.messages

    def __len__(self) -> int:
        return len(self.messages)

    def __repr__(self) -> str:
        return f"ChatComponent(messages={len(self.messages)}, max_chars={self.max_chars})"


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════
def create_chat_component(placeholder: str = 'Escribí tu consulta...',
                          max_chars: int = 10000,
                          send_callback: Optional[Callable] = None,
                          state: Optional[Any] = None) -> ChatComponent:
    component = ChatComponent(
        placeholder=placeholder,
        max_chars=max_chars,
        send_callback=send_callback,
        state=state,
    )
    component.create()
    return component


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9v (P0) — RE-EXPORT (esquema 50/50)
# ═══════════════════════════════════════════════════════════════
# El widget de feedback vive en su propio módulo (chat.py supera las 1000
# líneas). Se re-exporta para no cambiar a los consumidores.
from .feedback_widget import FeedbackWidget  # noqa: E402,F401
