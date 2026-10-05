#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Componente de Modales (NiceGUI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/components/modals.py
# Autor: SaintWick
# Fecha: 2026-03-21  # ACTUALIZADO: FASE 5 - ClarificationDialog
# Propósito: Componentes modales reutilizables (status, settings, exit, ambiguity, clarification)
# Versión: 0.0.8+ (v0.6.9p)
# ✅ CORREGIDO v0.0.9 (2026-06-24):
# - Fallback model en SettingsDialog: vicgalle/prometheus → qwen3:8b
# - Opciones de modelo: reemplazado vicgalle/prometheus por qwen3:8b
# ═══════════════════════════════════════════════════════════════
import logging
import asyncio
import os
import signal
import time
from typing import Optional, Dict, Any, Callable, List
from nicegui import ui
from datetime import datetime
from pathlib import Path
from config.settings import settings

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_web_event(category: str, message: str, extra: dict = None):
    """
    Helper para logging de eventos web.
    Args:
        category: Categoría del evento (UI, MODAL, SESSION, etc.)
        message: Mensaje descriptivo
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level="INFO", extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 modals.py:49] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra: dict = None):
    """
    Helper para logging de errores.
    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 modals.py:66] excepción degradada (intencional): {_d2e}")

# ═══════════════════════════════════════════════════════════════
# ✅ FASE 3: DIÁLOGO DE ADVERTENCIA DE AMBIGÜEDAD
# ═══════════════════════════════════════════════════════════════
class AmbiguityWarningDialog:
    """
    Diálogo de advertencia por ambigüedad en requests.
    Features:
    - ✅ Muestra advertencia antes de generar código
    - ✅ Explica asunciones tomadas
    - ✅ Ofrece opción de clarificar o continuar
    - ✅ Logging detallado de interacciones
    - ✅ Diseño consistente con otros modales

    Example:
    >>> dialog = AmbiguityWarningDialog()
    >>> dialog.create()
    >>> dialog.open(level="high", patterns=["mejor que", "nmap"])
    """

    def __init__(self, continue_callback: Optional[Callable] = None,
                 clarify_callback: Optional[Callable] = None):
        """
        Inicializa diálogo de advertencia.
        Args:
            continue_callback: Función a llamar si usuario continúa
            clarify_callback: Función a llamar si usuario quiere clarificar
        Logging:
            - Registra inicialización del diálogo
            - Callbacks configurados
        """
        start = time.time()
        _log_web_event("MODAL", "AmbiguityWarningDialog initialization started")

        self.continue_callback = continue_callback
        self.clarify_callback = clarify_callback
        self.dialog: Optional[ui.dialog] = None
        self.warning_content: Optional[ui.column] = None

        duration = time.time() - start
        _log_web_event("MODAL", "AmbiguityWarningDialog initialized",
                      extra={"duration": round(duration, 4),
                            "continue_callback": continue_callback is not None,
                            "clarify_callback": clarify_callback is not None})
        logger.debug(f"AmbiguityWarningDialog initialized")

    def create(self) -> ui.dialog:
        """
        Crea y retorna el diálogo.
        Returns:
            ui.dialog: Instancia del diálogo creado
        Logging:
            - Registra creación del diálogo
            - Elementos UI creados
        """
        start = time.time()
        _log_web_event("MODAL", "Creating AmbiguityWarningDialog UI")

        with ui.dialog() as self.dialog, ui.card().classes('w-[500px]'):
            ui.label('⚠️ Petición Ambigua Detectada').classes('text-xl font-bold text-orange-800')

            self.warning_content = ui.column().classes('w-full gap-3 mt-4')

            # Contenido dinámico se llena en open()
            with self.warning_content:
                ui.label('Cargando advertencia...').classes('text-gray-500')

            # Botones
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('❌ Clarificar',
                         on_click=lambda: self._on_clarify(),
                         color='orange').props('flat')
                ui.button('✅ Continuar',
                         on_click=lambda: self._on_continue(),
                         color='primary').props('flat')

        duration = time.time() - start
        _log_web_event("MODAL", "AmbiguityWarningDialog UI created",
                      extra={"duration": round(duration, 4)})

        return self.dialog

    def open(self, level: str = "high", patterns: Optional[list] = None,
             message: Optional[str] = None) -> None:
        """
        Abre el diálogo con información específica.
        Args:
            level: Nivel de ambigüedad ("high", "medium")
            patterns: Patrones detectados en el request
            message: Mensaje personalizado de advertencia
        Logging:
            - Registra apertura del diálogo
            - Información mostrada
        """
        _log_web_event("MODAL", "AmbiguityWarningDialog opened",
                      extra={"level": level, "patterns": patterns})

        if self.warning_content:
            self.warning_content.clear()

            with self.warning_content:
                # Mensaje principal según nivel
                if level == "high":
                    ui.icon('warning', size='xl').classes('text-orange-600')
                    ui.label('Tu petición es amplia y puede interpretarse de varias formas.').classes('text-base')
                elif level == "medium":
                    ui.icon('info', size='xl').classes('text-blue-600')
                    ui.label('Tu petición tiene algunos términos que pueden optimizarse.').classes('text-base')

                # Mensaje personalizado si existe
                if message:
                    ui.separator()
                    ui.label(message).classes('text-sm text-gray-700')

                # Patrones detectados
                if patterns:
                    ui.separator()
                    ui.label('Patrones detectados:').classes('text-sm font-semibold text-gray-600')
                    with ui.row().classes('gap-2 flex-wrap'):
                        for pattern in patterns[:5]:
                            ui.badge(pattern, color='orange-1').classes('text-xs')

                # Explicación de asunciones
                ui.separator()
                ui.label('¿Qué voy a hacer?').classes('text-sm font-semibold text-gray-600')

                if level == "high":
                    ui.label('• Generaré una solución PRÁCTICA con limitaciones documentadas').classes('text-xs text-gray-600')
                    ui.label('• No asumiré requisitos no especificados').classes('text-xs text-gray-600')
                    ui.label('• Incluiré advertencias sobre herramientas enterprise').classes('text-xs text-gray-600')
                elif level == "medium":
                    ui.label('• Optimizaré según criterios estándar').classes('text-xs text-gray-600')
                    ui.label('• Documentaré trade-offs en el código').classes('text-xs text-gray-600')
                    ui.label('• Podés pedir ajustes específicos después').classes('text-xs text-gray-600')

                # Feedback prompt
                ui.separator()
                ui.label('¿El resultado no es lo que buscabas?').classes('text-xs text-gray-500 italic')
                ui.label('Usá el feedback para mejorar la próxima respuesta.').classes('text-xs text-gray-500 italic')

        if self.dialog:
            self.dialog.open()

    def close(self) -> None:
        """
        Cierra el diálogo.
        Logging:
            - Registra cierre del diálogo
        """
        _log_web_event("MODAL", "AmbiguityWarningDialog closed")
        if self.dialog:
            self.dialog.close()

    def _on_continue(self) -> None:
        """
        Handler para botón Continuar.
        Logging:
            - Registra decisión del usuario
        """
        _log_web_event("AMBIGUITY", "User chose to continue with ambiguous request")

        if self.continue_callback:
            self.continue_callback()

        self.close()
        ui.notify('✅ Generando código con asunciones documentadas...', color='primary')

    def _on_clarify(self) -> None:
        """
        Handler para botón Clarificar.
        Logging:
            - Registra decisión del usuario
        """
        _log_web_event("AMBIGUITY", "User chose to clarify ambiguous request")

        if self.clarify_callback:
            self.clarify_callback()

        self.close()
        ui.notify('📝 Por favor, especificá qué necesitás exactamente.', color='info')


# ═══════════════════════════════════════════════════════════════
# ✅ FASE 5: DIÁLOGO DE CLARIFICACIÓN (FALLBACK A PREGUNTAS)
# ═══════════════════════════════════════════════════════════════
class ClarificationDialog:
    """
    Diálogo de clarificación para fallback tras fallos consecutivos.
    ✅ FASE 5: Se activa tras 2+ fallos en generación de código

    Features:
    - Muestra preguntas específicas según patrones detectados
    - Permite al usuario responder antes de reintentar
    - Logging detallado de interacciones
    - Diseño consistente con otros modales

    Example:
    >>> dialog = ClarificationDialog(submit_callback=on_submit)
    >>> dialog.create()
    >>> dialog.open(questions=["¿Qué aspecto específico?", "¿Uso personal o empresarial?"])
    """

    def __init__(self, submit_callback: Optional[Callable] = None,
                 cancel_callback: Optional[Callable] = None):
        """
        Inicializa diálogo de clarificación.
        Args:
            submit_callback: Función a llamar al enviar respuestas
            cancel_callback: Función a llamar al cancelar
        """
        start = time.time()
        _log_web_event("MODAL", "ClarificationDialog initialization started")

        self.submit_callback = submit_callback
        self.cancel_callback = cancel_callback
        self.dialog: Optional[ui.dialog] = None
        self.questions_content: Optional[ui.column] = None
        self.question_inputs: Dict[int, ui.input] = {}

        duration = time.time() - start
        _log_web_event("MODAL", "ClarificationDialog initialized",
                      extra={"duration": round(duration, 4)})

    def create(self) -> ui.dialog:
        """Crea y retorna el diálogo."""
        start = time.time()
        _log_web_event("MODAL", "Creating ClarificationDialog UI")

        with ui.dialog() as self.dialog, ui.card().classes('w-[500px]'):
            ui.label('❓ Necesitamos más información').classes('text-xl font-bold text-blue-800')
            ui.label('Tu petición anterior no pudo ser completada. Por favor, respondé estas preguntas:').classes('text-sm text-gray-600 mt-2')

            self.questions_content = ui.column().classes('w-full gap-4 mt-4')
            with self.questions_content:
                ui.label('Cargando preguntas...').classes('text-gray-500')

            # Botones
            with ui.row().classes('w-full justify-end gap-2 mt-4'):
                ui.button('❌ Cancelar', on_click=lambda: self._on_cancel(), color='negative').props('flat')
                ui.button('✅ Enviar Respuestas', on_click=lambda: self._on_submit(), color='primary').props('flat')

        duration = time.time() - start
        _log_web_event("MODAL", "ClarificationDialog UI created", extra={"duration": round(duration, 4)})
        return self.dialog

    def open(self, questions: List[str], original_request: str = "") -> None:
        """
        Abre el diálogo con preguntas específicas.
        Args:
            questions: Lista de preguntas para el usuario
            original_request: Petición original para contexto
        """
        _log_web_event("MODAL", "ClarificationDialog opened", extra={"questions_count": len(questions)})

        if self.questions_content:
            self.questions_content.clear()
            self.question_inputs.clear()

            with self.questions_content:
                # Mostrar petición original si existe
                if original_request:
                    ui.label('Petición original:').classes('text-xs font-semibold text-gray-500')
                    ui.label(f'"{original_request[:100]}{"..." if len(original_request) > 100 else ""}"').classes('text-xs text-gray-600 italic mb-2')

                # Generar inputs para cada pregunta
                for i, question in enumerate(questions):
                    ui.label(f'{i+1}. {question}').classes('text-sm font-medium text-gray-700')
                    input_field = ui.input(placeholder='Tu respuesta...').classes('w-full').props('outlined')
                    self.question_inputs[i] = input_field

        if self.dialog:
            self.dialog.open()

    def close(self) -> None:
        """Cierra el diálogo."""
        _log_web_event("MODAL", "ClarificationDialog closed")
        if self.dialog:
            self.dialog.close()

    def get_answers(self) -> Dict[int, str]:
        """Obtiene las respuestas del usuario."""
        answers = {}
        for idx, input_field in self.question_inputs.items():
            if input_field and input_field.value:
                answers[idx] = input_field.value.strip()
        return answers

    def _on_submit(self) -> None:
        """Handler para botón Enviar."""
        _log_web_event("CLARIFICATION", "User submitted clarification answers")
        answers = self.get_answers()

        if not answers:
            ui.notify('⚠️ Por favor respondé al menos una pregunta', color='warning', timeout=3)
            return

        if self.submit_callback:
            try:
                self.submit_callback(answers)
            except Exception as e:
                _log_error(e, "ClarificationDialog._on_submit", extra={"answers_count": len(answers)})
                logger.warning(f"Clarification callback error: {e}")

        self.close()
        ui.notify('✅ Respuestas enviadas. Generando nueva respuesta...', color='positive', timeout=2)

    def _on_cancel(self) -> None:
        """Handler para botón Cancelar."""
        _log_web_event("CLARIFICATION", "User cancelled clarification")
        if self.cancel_callback:
            try:
                self.cancel_callback()
            except Exception as e:
                logger.warning(f"Clarification cancel callback error: {e}")
        self.close()
        ui.notify('❌ Clarificación cancelada', color='negative', timeout=2)


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.8ll — DIÁLOGOS PRINCIPALES (fuente única; los consume app.py)
# ═══════════════════════════════════════════════════════════════
# Los diálogos de la UI (Estado / Configuración / Archivos / Salida) viven
# ACÁ y app.py los importa — antes estaban duplicados en app.py + clases
# legacy (StatusDialog/SettingsDialog/ExitDialog) con refresh roto.
#
# Eficiencia:
#   - Refresh del estado INCREMENTAL: las filas se crean UNA vez y solo se
#     actualiza texto/clase en cada refresh (nada de clear()+rebuild).
#   - Guard anti-reentrada en el refresh (sin dobles llamadas HTTP).
#   - Construcción única por página (dialog reutilizado por open/close).
#   - Handlers async nativos de NiceGUI (nada de dialog.on('open') que nunca
#     dispara, ni asyncio.create_task suelto sin contexto).
# ═══════════════════════════════════════════════════════════════


def _status_data():
    """Estado del sistema (Ollama + API keys) — helper inyectable/testeable."""
    from ..web.app_logic import collect_system_status
    return collect_system_status()


def _build_status_dialog() -> "ui.dialog":
    """
    Diálogo 'Estado': refresh incremental con health reales.
    Expone `dialog._refresh` (async) para el header (fix v0.6.8jj).
    """
    with ui.dialog() as dialog, ui.card().classes('w-96'):
        ui.label('🏥 Estado del Sistema').classes('text-xl font-bold')

        rows_content = ui.column().classes('w-full gap-1')
        _value_labels: Dict[str, Any] = {}
        _value_styles: Dict[str, str] = {}
        _lock = {"active": False}
        _APIS = ["Ollama", "Tavily API", "News API", "Serper API",
                 "SerpAPI", "Dolar API"]

        def _apply_status(api_name: str, text: str, color: str) -> None:
            """Actualiza texto/clase de una fila sin acumular colores viejos.

            ✅ v0.6.8nn fix: `.classes(color, clear=False)` NO existe en
            NiceGUI (TypeError) → el diálogo pintaba todo "❌ error".
            Ahora se remueve la clase anterior antes de aplicar la nueva.
            """
            lbl = _value_labels.get(api_name)
            if lbl is None:
                return
            prev = _value_styles.get(api_name, "")
            try:
                lbl.set_text(text)
                if prev and prev != color:
                    lbl.classes(remove=prev)
                if color and color != prev:
                    lbl.classes(color)
            except Exception as e:
                _log_error(e, f"refresh_status.apply_status:{api_name}")
            _value_styles[api_name] = color

        async def refresh_status() -> None:
            if _lock["active"]:
                return
            _lock["active"] = True
            try:
                status = await _status_data()
                values = {
                    "Ollama": bool(status.get("ollama")),
                    "Tavily API": bool(status.get("tavily")),
                    "News API": bool(status.get("news")),
                    "Serper API": bool(status.get("serper")),
                    "SerpAPI": bool(status.get("serpapi")),
                    "Dolar API": bool(status.get("dolar")),
                }
                if not _value_labels:
                    # Primera vez: crear filas + botones (una sola vez)
                    with rows_content:
                        for api_name in _APIS:
                            with ui.row().classes(
                                    'w-full justify-between items-center').props('align-center'):
                                ui.label(api_name)
                                _value_labels[api_name] = ui.label("")
                        with ui.row().classes('w-full justify-end gap-2'):
                            ui.button('Actualizar', on_click=refresh_status).props('flat')
                            ui.button('Cerrar', on_click=dialog.close).props('flat')
                # ✅ v0.6.9v (ISSUE-038): la etiqueta decía "❌ No" para TODO,
                # así que no se distinguía "falta la API key en .env" de "la API
                # no responde". Ahora el motivo es explícito por tipo.
                _KEY_BASED = {"Tavily API", "News API", "Serper API", "SerpAPI"}
                for api_name, ok in values.items():
                    if ok:
                        text, color = "✅ OK", "text-green"
                    elif api_name in _KEY_BASED:
                        text, color = "❌ Sin key (.env)", "text-red"
                    elif api_name == "Dolar API":
                        text, color = "❌ No responde", "text-red"
                    else:
                        text, color = "❌ No responde", "text-red"
                    _apply_status(api_name, text, color)
            except Exception as e:
                # ✅ Un solo ítem que falle no debe tumbar el resto
                _log_error(e, "refresh_status")
                for api_name in _APIS:
                    _apply_status(api_name, "❌ error", "text-red")
            finally:
                _lock["active"] = False

        dialog._refresh = refresh_status  # type: ignore[attr-defined]
    return dialog


def _api_status_text(ok: bool) -> tuple:
    """('✅ Conectado'/'❌ No' + clase css) para el estado."""
    if ok:
        return ("✅ OK", "text-green")
    return ("❌ No", "text-red")


def _build_settings_dialog(state: Optional[Any] = None) -> "ui.dialog":
    """Diálogo de Configuración (portado de app.py — fuente única en modals)."""
    from ..web.app_logic import _setting_select, save_settings as _save_settings

    with ui.dialog() as dialog, ui.card().classes('w-96'):
        ui.label('⚙️ Configuración').classes('text-xl font-bold')

        with ui.column().classes('w-full gap-4'):
            _available_models = [
                m for m in {
                    settings.llm_default_model,
                    settings.llm_code_model,
                    settings.llm_vision_model,
                } if m
            ]
            _model_value = getattr(state, "model", settings.llm_default_model)
            if _model_value not in _available_models:
                _available_models.insert(0, _model_value)

            _setting_select('Modelo', _available_models, _model_value,
                            lambda v: setattr(state, 'model', v))

            _setting_select('Temperatura', ['0.1', '0.5', '0.7', '0.9'],
                            str(getattr(state, "temperature", 0.5)),
                            lambda v: setattr(state, 'temperature', float(v)))

            toggle = ui.switch('Validar respuestas',
                               value=bool(getattr(state, "enable_validation", True)))
            toggle.on('change',
                      lambda e: setattr(state, 'enable_validation', e.value)
                      if hasattr(e, 'value') else None)

            _jarvis_current = getattr(state, 'jarvis_mode', 'normal') or 'normal'
            _setting_select('Modo Jarvis', ['normal', 'jarvis', 'voice_only', 'silent'],
                            _jarvis_current,
                            lambda v: setattr(state, 'jarvis_mode', v))

            async def _save_and_close() -> None:
                try:
                    _save_settings(state, sidebar=None)
                    await asyncio.sleep(0.05)
                    dialog.close()
                except Exception as e:
                    _log_error(e, "_save_and_close — settings")
                    try:
                        dialog.close()
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 modals.py:536] excepción degradada (intencional): {_d2e}")

            with ui.row().classes('w-full justify-end'):
                ui.button('Guardar', on_click=_save_and_close).props('flat color=primary')
                ui.button('Cancelar', on_click=dialog.close).props('flat')

    return dialog


def _build_file_dialog(state: Optional[Any] = None,
                       chat: Optional[Any] = None) -> "ui.dialog":
    """📁 Búsqueda/lectura/EXPLORACIÓN de archivos (fuente única).

    v0.6.9p: explorá carpetas por navegación y adjuntá un archivo al chat
    (📎 Adjuntar para analizar) igual que el upload del chat.
    """
    from src.tools_mcp.tool_registry import get_registry
    _registry = get_registry()

    with ui.dialog() as dialog, ui.card().classes('w-[720px] max-w-[95vw]'):
        ui.label('📁 Archivos de AXIOMA').classes('text-xl font-bold')
        ui.label('Buscá por nombre o explorá carpetas. Después: 📄 Leer | '
                 '📝 Resumen (LLM) | 💬 Cita | 📎 Adjuntar al chat.'
                 ).classes('text-sm text-gray-500')

        results = ui.column().classes('w-full gap-1 max-h-56 overflow-auto')
        output_box = ui.column().classes('w-full')

        def _show(content: str, path: str = "") -> None:
            output_box.clear()
            with output_box:
                if path:
                    ui.label(f'📄 {path}').classes('text-sm font-bold text-blue-400')
                ui.markdown("```\n" + content + "\n```").classes('w-full')

        def _search(_e=None) -> None:
            q = (search_input.value or "").strip()
            if not q:
                ui.notify('Escribí un nombre para buscar', type='warning')
                return
            if not _registry.has("fs_search"):
                ui.notify('Tool fs_search no disponible', type='negative')
                return
            r = _registry.get("fs_search").run(name=q, max_results=12)
            results.clear()
            matches = (r.data or {}).get("matches", [])
            if not matches:
                with results:
                    ui.label('Sin coincidencias.').classes('text-gray-500 text-sm')
                return
            with results:
                for m in matches:
                    path = m["path"]
                    icon = '📁' if m["type"] == 'dir' else '📄'
                    with ui.row().classes('items-center gap-2 w-full py-1 border-b border-gray-700'):
                        ui.label(f'{icon} {m["name"]}').classes('text-sm font-medium w-40 truncate')
                        ui.label(path).classes('text-xs text-gray-400 truncate').style('flex:1')
                        ui.button('Leer', on_click=lambda p=path: _read(p)).props('size=sm dense')
                        ui.button('Resumen', on_click=lambda p=path: _summary(p)).props('size=sm dense color=primary')
                        ui.button('Cita', on_click=lambda p=path: _quote(p)).props('size=sm dense')

        def _read(path: str) -> None:
            if _registry.has("fs_read"):
                r = _registry.get("fs_read").run(path=path, max_chars=8000)
                _show(r.output or r.error or "(vacío)", path)

        def _quote(path: str) -> None:
            if _registry.has("fs_read"):
                r = _registry.get("fs_read").run(path=path, max_chars=1200)
                _show(r.output or r.error or "(vacío)", path)

        async def _summary(path: str) -> None:
            _show("⏳ Generando resumen (CPU, puede tardar ~1 min)...", path)
            try:
                if state is not None and state.dispatcher is None:
                    state.init_dispatcher()
                if state is None or state.dispatcher is None:
                    _show("Dispatcher no disponible.", path)
                    return
                from src.domain.entities import Message
                resp = await state.dispatcher.process(
                    Message(content=f"leé el archivo {path} y resumilo", role="user"),
                )
                _show(resp.content or "Sin resumen.", path)
            except Exception as e:
                _show(f"Error generando resumen: {e}", path)

        with ui.row().classes('w-full items-center gap-2'):
            search_input = ui.input('Buscar archivo o carpeta...',
                                    on_change=_search).props('outlined').classes('flex-1')
            ui.button('🔍 Buscar', on_click=_search).props('color=primary')

        with ui.column().classes('w-full'):
            ui.separator()
            ui.label('Resultados').classes('text-sm font-semibold text-gray-400')
            results
            ui.separator()
            ui.label('Contenido').classes('text-sm font-semibold text-gray-400')
            output_box

        # ── v0.6.9p: explorador de carpetas (navegación → adjuntar) ──
        ui.separator()
        ui.label('📂 Explorá carpetas (📄 archivo = seleccionar)'
                 ).classes('text-sm font-semibold text-gray-400')
        try:
            _root = str(getattr(settings, "_REPO_ROOT", Path.cwd()))
        except Exception:
            _root = str(Path.cwd())
        _cur = {"path": _root}
        path_label = ui.label('').classes(
            'text-xs font-mono text-blue-300 truncate w-full')
        explorer = ui.column().classes('w-full gap-1 max-h-56 overflow-auto')
        _actions = ui.row().classes('w-full items-center gap-2')

        def _fmt_size(n: int) -> str:
            if n < 1024:
                return f"{n} B"
            if n < 1048576:
                return f"{n // 1024} KB"
            return f"{n / 1048576:.1f} MB"

        def _go(path: str) -> None:
            _cur["path"] = path
            _render_explorer()

        def _select(path: str) -> None:
            _actions.clear()
            with _actions:
                ui.label(f'Seleccionado: {path}').classes(
                    'text-xs text-gray-300 truncate')
                ui.button('📄 Leer', on_click=lambda: _read(path)
                          ).props('size=sm dense')
                ui.button('📝 Resumen', on_click=lambda: asyncio.create_task(
                    _summary(path))).props('size=sm dense color=primary')
                ui.button('💬 Cita', on_click=lambda: _quote(path)
                          ).props('size=sm dense')
                if chat is not None:
                    ui.button('📎 Adjuntar al chat',
                              on_click=lambda: _attach(path)
                              ).props('size=sm dense color=positive')

        def _attach(path: str) -> None:
            ok = chat.attach_local_path(path)
            if ok:
                dialog.close()
                ui.notify('📎 Adjuntado. Escribí tu instrucción en el chat y '
                          'enviá.', type='positive')

        def _render_explorer() -> None:
            from src.interfaces.web.app_logic import explore_dir
            data = explore_dir(_cur["path"])
            if data is None:
                ui.notify(f'No se pudo leer {_cur["path"]}', type='negative')
                return
            path_label.set_text(f'📍 {data["path"]}')
            explorer.clear()
            _actions.clear()
            with explorer:
                if data.get("parent"):
                    ui.button('🔼 Subir',
                              on_click=lambda: _go(data["parent"])
                              ).props('flat dense').classes('w-full')
                for d in data["dirs"]:
                    ui.button(f'📁 {d["name"]}',
                              on_click=lambda p=d["path"]: _go(p)
                              ).props('flat dense align="left"'
                                      ).classes('w-full justify-start')
                for f in data["files"]:
                    ui.button(f'📄 {f["name"]}  ({_fmt_size(f["size"])})',
                              on_click=lambda p=f["path"]: _select(p)
                              ).props('flat dense align="left"'
                                      ).classes('w-full justify-start')
            if not data["dirs"] and not data["files"]:
                with explorer:
                    ui.label('(carpeta vacía)').classes('text-gray-500 text-sm')

        _render_explorer()

        with ui.row().classes('w-full justify-end pt-2'):
            ui.button('Cerrar', on_click=dialog.close).props('flat')

    return dialog


def _build_exit_dialog() -> "ui.dialog":
    """🚪 Diálogo de salida con confirmación (SIGINT)."""
    with ui.dialog() as dialog, ui.card().classes('w-96'):
        ui.label('🚪 Cerrar AXIOMA').classes('text-xl font-bold')
        ui.label('¿Estás seguro de que deseas cerrar AXIOMA?').classes('mt-2')
        ui.label('El servidor se detendrá y la interfaz se cerrará.'
                 ).classes('text-sm text-gray-500')

        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Cancelar', on_click=dialog.close).props('flat')

            def shutdown() -> None:
                try:
                    dialog.close()
                    _log_web_event("SYSTEM", "Shutdown initiated")
                    os.kill(os.getpid(), signal.SIGINT)
                except Exception as e:
                    _log_error(e, "shutdown")
                    os._exit(1)

            ui.button('✅ Cerrar Sistema', on_click=shutdown).props('flat color=red')

    return dialog


# ═══════════════════════════════════════════════════════════════
# LEGACY SHIMS (compatibilidad API antigua basada en clases).
# Ya NO construyen UI propia: delegan en los builders eficientes.
# Deprecados — usar las factories create_*_dialog().
# ═══════════════════════════════════════════════════════════════
class StatusDialog:
    """Legacy: wrapper de create_status_dialog() (deprecado)."""
    def __init__(self, api_check_callback: Optional[Callable] = None):
        self.api_check_callback = api_check_callback
        self.dialog: Optional[ui.dialog] = None

    def create(self) -> ui.dialog:
        self.dialog = create_status_dialog()
        return self.dialog

    def open(self) -> None:
        if self.dialog:
            self.dialog.open()

    def close(self) -> None:
        if self.dialog:
            self.dialog.close()


class SettingsDialog:
    """Legacy: wrapper de create_settings_dialog(state) (deprecado)."""
    def __init__(self, state: Optional[Any] = None,
                 save_callback: Optional[Callable] = None):
        self.state = state
        self.save_callback = save_callback
        self.dialog: Optional[ui.dialog] = None

    def create(self) -> ui.dialog:
        self.dialog = create_settings_dialog(self.state)
        return self.dialog

    def open(self) -> None:
        if self.dialog:
            self.dialog.open()

    def close(self) -> None:
        if self.dialog:
            self.dialog.close()


class ExitDialog:
    """Legacy: wrapper de create_exit_dialog() (deprecado)."""
    def __init__(self, shutdown_callback: Optional[Callable] = None):
        self.shutdown_callback = shutdown_callback
        self.dialog: Optional[ui.dialog] = None

    def create(self) -> ui.dialog:
        self.dialog = create_exit_dialog()
        return self.dialog

    def open(self) -> None:
        if self.dialog:
            self.dialog.open()

    def close(self) -> None:
        if self.dialog:
            self.dialog.close()


# FACTORY FUNCTIONS — app.py consume estas factories (v0.6.8ll)
def create_status_dialog(api_check_callback: Optional[Callable] = None) -> "ui.dialog":
    """Diálogo de Estado (refresh incremental, expone ._refresh)."""
    del api_check_callback  # API legacy aceptada por compatibilidad
    return _build_status_dialog()


def create_settings_dialog(state: Optional[Any] = None,
                           save_callback: Optional[Callable] = None) -> "ui.dialog":
    """Diálogo de Configuración (fuente única para app.py)."""
    del save_callback  # el guardado usa app_logic.save_settings
    return _build_settings_dialog(state)


def create_exit_dialog(shutdown_callback: Optional[Callable] = None) -> "ui.dialog":
    """Diálogo de Salida (fuente única para app.py)."""
    del shutdown_callback
    return _build_exit_dialog()


def create_file_dialog(state: Optional[Any] = None,
                       chat: Optional[Any] = None) -> "ui.dialog":
    """📁 Diálogo de Archivos (fuente única para app.py)."""
    return _build_file_dialog(state, chat)


def create_ambiguity_warning_dialog(continue_callback: Optional[Callable] = None,
                                    clarify_callback: Optional[Callable] = None) -> AmbiguityWarningDialog:
    """Factory para crear diálogo de advertencia de ambigüedad."""
    dialog = AmbiguityWarningDialog(continue_callback=continue_callback,
                                    clarify_callback=clarify_callback)
    dialog.create()
    return dialog


def create_clarification_dialog(submit_callback: Optional[Callable] = None,
                                cancel_callback: Optional[Callable] = None) -> ClarificationDialog:
    """Factory para crear diálogo de clarificación."""
    dialog = ClarificationDialog(submit_callback=submit_callback, cancel_callback=cancel_callback)
    dialog.create()
    return dialog


# 📋 RESUMEN DE CAMBIOS — v0.0.9
# ═══════════════════════════════════════════════════════════════
# | Línea/Sección           | Cambio Realizado                        | Propósito                         |
# |-------------------------|-----------------------------------------|-----------------------------------|
# | ~619                    | initial_model → qwen3:8b              | Eliminar vicgalle/prometheus      |
# | ~622                    | opciones → qwen3:8b en lista          | Eliminar vicgalle/prometheus      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — ~850 LÍNEAS — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9v (F1 "Mi Hard") — RE-EXPORT (esquema 50/50)
# ═══════════════════════════════════════════════════════════════
# El diálogo "Mi Hard" vive en su propio módulo (modals.py superaba las
# 1000 líneas). Se re-exporta aquí para NO cambiar el API público ni a
# los consumidores (app.py importa create_mi_hard_dialog desde modals).
from .mi_hard_dialog import (  # noqa: E402,F401
    create_mi_hard_dialog,
    mi_hard_sections,
)
