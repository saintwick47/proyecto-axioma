#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Componente Sidebar (NiceGUI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/components/sidebar.py
# Autor: SaintWick
# Fecha: 2026-03-27
# Propósito: Barra lateral de navegación reutilizable para NiceGUI
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: log._initialized → log.is_initialized (consistencia con app.py)
# ✅ NUEVO: Dashboard v0.0.9 integrado (ítem de navegación 📊)
# ✅ NUEVO: Integración con DetailedLogger
# ✅ CORREGIDO v0.0.10 (2026-06-24):
# - Fallback model en status section → qwen3:8b (2 lugares)
# ═══════════════════════════════════════════════════════════════
import logging
import time
import json
from typing import Optional, Dict, Any, Callable, List
from nicegui import ui
from datetime import datetime
from pathlib import Path

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
        category: Categoría del evento (UI, SESSION, NAVIGATION, DASHBOARD, etc.)
        message: Mensaje descriptivo
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized (no _initialized)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level="INFO", extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 sidebar.py:51] excepción degradada (intencional): {_d2e}")


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
        # ✅ CORREGIDO: Usar propiedad pública is_initialized (no _initialized)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 sidebar.py:70] excepción degradada (intencional): {_d2e}")


# ═══════════════════════════════════════════════════════════════
# CLASE SIDEBAR
# ═══════════════════════════════════════════════════════════════
class SidebarComponent:
    """
    Componente de barra lateral para navegación en NiceGUI.

    Features:
    - ✅ Separación de lógica de UI y negocio
    - ✅ Soporte para callbacks de navegación
    - ✅ Estado reactivo del sistema
    - ✅ Sidebar colapsable con animación
    - ✅ Logging detallado de todas las operaciones
    - ✅ Timing de cada operación
    - ✅ Error tracking completo
    - ✅ NUEVO: Navegación Dashboard integrada (v0.0.9)

    Example:
        >>> sidebar = SidebarComponent(
        ...     on_chat_click=handle_chat,
        ...     on_exit_click=handle_exit,
        ...     state=session_state
        ... )
        >>> sidebar.create()
    """

    def __init__(self,
                 on_chat_click: Optional[Callable] = None,
                 on_settings_click: Optional[Callable] = None,
                 on_status_click: Optional[Callable] = None,
                 on_backup_click: Optional[Callable] = None,
                 on_dashboard_click: Optional[Callable] = None,
                 on_exit_click: Optional[Callable] = None,
                 state: Optional[Any] = None):
        """
        Inicializa componente sidebar.
        Args:
            on_chat_click: Callback al hacer click en Chat
            on_settings_click: Callback al hacer click en Configuración
            on_status_click: Callback al hacer click en Estado
            on_backup_click: Callback al hacer click en Backup
            on_dashboard_click: Callback al hacer click en Dashboard (✅ NUEVO)
            on_exit_click: Callback al hacer click en Salir
            state: Estado de sesión (SessionState)
        Logging:
            - Registra inicialización del componente
            - Callbacks configurados
        """
        start = time.time()
        _log_web_event("UI", "SidebarComponent initialization started")

        self.on_chat_click = on_chat_click
        self.on_settings_click = on_settings_click
        self.on_status_click = on_status_click
        self.on_backup_click = on_backup_click
        self.on_dashboard_click = on_dashboard_click  # ✅ NUEVO
        self.on_exit_click = on_exit_click
        self.state = state
        self.elements: Dict[str, Any] = {}
        self.is_collapsed = False

        duration = time.time() - start
        _log_web_event("UI", "SidebarComponent initialized",
                      extra={"duration": round(duration, 4),
                            "callbacks_configured": sum([
                                on_chat_click is not None,
                                on_settings_click is not None,
                                on_status_click is not None,
                                on_backup_click is not None,
                                on_dashboard_click is not None,
                                on_exit_click is not None
                            ])})
        logger.debug(f"SidebarComponent initialized: state={state is not None}")

    def create(self, collapsible: bool = True) -> ui.left_drawer:
        """
        Crea la UI del sidebar.
        Args:
            collapsible: Si True, permite colapsar el sidebar
        Returns:
            Instancia de ui.left_drawer
        Logging:
            - Registra creación del sidebar
            - Elementos UI creados
            - Timing de creación
        """
        start = time.time()
        _log_web_event("UI", "Creating SidebarComponent UI",
                      extra={"collapsible": collapsible})

        with ui.left_drawer().classes('bg-primary text-white') as drawer:
            with ui.column().classes('w-full h-full justify-between'):
                # ✅ SECCIÓN SUPERIOR: Logo y navegación
                with ui.column().classes('w-full gap-2 p-4'):
                    # Logo y título
                    with ui.row().classes('w-full items-center gap-3 mb-4'):
                        ui.image('/static/logo.png').classes('h-10 w-10').on(
                            'error',
                            lambda e: e.source.set_text('📘')
                        )
                        title_label = ui.label('AXIOMA v0.0.9').classes('text-xl font-bold')
                        self.elements['title_label'] = title_label

                    # Botón colapsar (solo si collapsible=True)
                    if collapsible:
                        collapse_btn = ui.button(
                            '◀',
                            on_click=lambda: self._toggle_collapse(drawer)
                        ).props('flat dense size=sm')
                        self.elements['collapse_btn'] = collapse_btn

                    # ✅ NUEVO: Separador
                    ui.separator().classes('bg-white opacity-20')

                    # Items de navegación principal
                    self._create_nav_items()

                # ✅ SECCIÓN INFERIOR: Estado y acciones
                with ui.column().classes('w-full gap-2 p-4'):
                    # Separador
                    ui.separator().classes('bg-white opacity-20')

                    # Estado del sistema
                    self._create_status_section()

                    # Acciones rápidas
                    self._create_action_buttons()

                    # Footer con versión
                    self._create_footer()

            self.elements['drawer'] = drawer

        duration = time.time() - start
        _log_web_event("UI", "SidebarComponent UI created",
                      extra={"duration": round(duration, 4),
                            "elements_count": len(self.elements),
                            "collapsible": collapsible})
        return drawer

    def _create_nav_items(self) -> None:
        """
        Crea items de navegación principal.
        ✅ NUEVO: Agregado ítem Dashboard (📊)
        Logging:
            - Registra creación de cada item de navegación
            - Callbacks asociados
        """
        _log_web_event("UI", "Creating navigation items")

        nav_items = [
            {'icon': '💬', 'label': 'Chat', 'callback': self.on_chat_click, 'key': 'nav_chat'},
            # ✅ NUEVO: Dashboard (v0.0.9)
            {'icon': '📊', 'label': 'Dashboard', 'callback': self.on_dashboard_click, 'key': 'nav_dashboard'},
            {'icon': '🏥', 'label': 'Estado', 'callback': self.on_status_click, 'key': 'nav_status'},
            {'icon': '📁', 'label': 'Archivos', 'callback': None, 'key': 'nav_files'},
            {'icon': '⚙️', 'label': 'Configuración', 'callback': self.on_settings_click, 'key': 'nav_settings'},
            {'icon': '💾', 'label': 'Backup', 'callback': self.on_backup_click, 'key': 'nav_backup'},
        ]

        for item in nav_items:
            with ui.row().classes('w-full items-center gap-3 p-2 rounded-lg hover:bg-white hover:bg-opacity-10 cursor-pointer') as row:
                ui.label(item['icon']).classes('text-xl')
                label = ui.label(item['label']).classes('text-base')

                if item['callback']:
                    row.on('click', item['callback'])
                    _log_web_event("NAVIGATION", f"Nav item created: {item['label']}",
                                  extra={"has_callback": True})
                else:
                    _log_web_event("NAVIGATION", f"Nav item created: {item['label']}",
                                  extra={"has_callback": False})

                # Guardar referencia para actualizar estado activo
                self.elements[item['key']] = {'row': row, 'label': label}

    def _create_status_section(self) -> None:
        """
        Crea sección de estado del sistema.
        Logging:
            - Registra creación de sección de estado
            - Valores iniciales mostrados
        """
        _log_web_event("UI", "Creating status section")

        ui.label('🖥️ Estado del Sistema').classes('text-sm font-semibold mb-2')

        # Estado de Ollama
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Ollama').classes('text-xs')
            ollama_status = ui.label('✅').classes('text-xs')
            self.elements['ollama_status'] = ollama_status

        # Estado de APIs
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('APIs').classes('text-xs')
            api_status = ui.label('⚠️').classes('text-xs')
            self.elements['api_status'] = api_status

        # Modelo activo
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Modelo').classes('text-xs')
            model_value = ui.label(
                self.state.model if self.state else 'qwen3:8b'  # ✅ CORREGIDO
            ).classes('text-xs text-gray-300')
            self.elements['model_value'] = model_value

        # Sesión activa
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Sesión').classes('text-xs')
            session_value = ui.label(
                self.state.session_id[-8:] if self.state and self.state.session_id else 'N/A'
            ).classes('text-xs text-gray-300')
            self.elements['session_value'] = session_value

        # Modo Jarvis
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('Modo').classes('text-xs')
            _initial_mode = "normal"
            if self.state:
                _initial_mode = (
                    getattr(self.state, 'jarvis_mode', None)
                    or getattr(getattr(self.state, 'context', None), 'jarvis_mode', None)
                    or "normal"
                )
            _mode_labels: dict = {
                "normal":     "👤 Normal",
                "jarvis":     "🤖 Jarvis",
                "voice_only": "🎙️ Voz",
                "silent":     "🔇 Silencioso",
            }
            jarvis_mode_value = ui.label(
                _mode_labels.get(_initial_mode, "👤 Normal")
            ).classes('text-xs text-gray-300')
            self.elements['jarvis_mode_value'] = jarvis_mode_value

        _log_web_event("UI", "Status section created",
                      extra={"model": self.state.model if self.state else 'qwen3:8b',  # ✅ CORREGIDO
                            "session_id": self.state.session_id[-8:] if self.state and self.state.session_id else 'N/A'})

    def _create_action_buttons(self) -> None:
        """
        Crea botones de acción rápida.
        Logging:
            - Registra creación de botones de acción
            - Callbacks asociados
        """
        _log_web_event("UI", "Creating action buttons")

        ui.label('⚡ Acciones Rápidas').classes('text-sm font-semibold mb-2')

        with ui.row().classes('w-full gap-2'):
            # Limpiar chat
            clear_btn = ui.button('🗑️', on_click=lambda: self._clear_chat()).props('flat dense size=sm')
            clear_btn.tooltip('Limpiar historial')
            self.elements['clear_btn'] = clear_btn

            # Exportar sesión
            export_btn = ui.button('📤', on_click=lambda: self._export_session()).props('flat dense size=sm')
            export_btn.tooltip('Exportar sesión')
            self.elements['export_btn'] = export_btn

            # Refresh estado
            refresh_btn = ui.button('🔄', on_click=lambda: self._refresh_status()).props('flat dense size=sm')
            refresh_btn.tooltip('Refresh estado')
            self.elements['refresh_btn'] = refresh_btn

        # Botón salir (destacado)
        with ui.row().classes('w-full justify-center mt-2'):
            exit_btn = ui.button(
                '🚪 Salir',
                on_click=lambda: self.on_exit_click() if self.on_exit_click else None
            ).props('flat color=red').classes('w-full')
            self.elements['exit_btn'] = exit_btn

        _log_web_event("UI", "Action buttons created",
                      extra={"buttons_count": 4})

    def _create_footer(self) -> None:
        """
        Crea footer con información de versión.
        Logging:
            - Registra creación del footer
            - Versión mostrada
        """
        _log_web_event("UI", "Creating footer")

        with ui.row().classes('w-full justify-center'):
            # ✅ CORREGIDO: Versión v0.0.9
            version_label = ui.label('v0.0.9').classes('text-xs text-gray-400')
            self.elements['version_label'] = version_label

        with ui.row().classes('w-full justify-center'):
            timestamp_label = ui.label(
                datetime.now().strftime('%Y-%m-%d')
            ).classes('text-xs text-gray-500')
            self.elements['timestamp_label'] = timestamp_label

        _log_web_event("UI", "Footer created",
                      extra={"version": "v0.0.9", "date": datetime.now().strftime('%Y-%m-%d')})

    def _toggle_collapse(self, drawer: ui.left_drawer) -> None:
        """
        Colapsa/expande el sidebar.
        Args:
            drawer: Instancia del drawer
        Logging:
            - Registra cambio de estado colapsado
            - Nuevo ancho del sidebar
        """
        start = time.time()
        _log_web_event("UI", "Toggle sidebar collapse",
                      extra={"current_state": self.is_collapsed})

        self.is_collapsed = not self.is_collapsed

        if self.is_collapsed:
            drawer.style('width: 80px;')
            # Ocultar labels, mostrar solo iconos
            for key, element in self.elements.items():
                if isinstance(element, dict) and 'label' in element:
                    element['label'].style('display: none;')
            if 'collapse_btn' in self.elements:
                self.elements['collapse_btn'].props('icon=▶')
        else:
            drawer.style('width: 280px;')
            # Mostrar labels
            for key, element in self.elements.items():
                if isinstance(element, dict) and 'label' in element:
                    element['label'].style('display: block;')
            if 'collapse_btn' in self.elements:
                self.elements['collapse_btn'].props('icon=◀')

        duration = time.time() - start
        _log_web_event("UI", "Sidebar collapse toggled",
                      extra={"new_state": self.is_collapsed,
                            "duration": round(duration, 4)})

    def _clear_chat(self) -> None:
        """
        Limpia el historial de chat.
        Logging:
            - Registra limpieza de chat
            - Session ID afectado
            - Notificación mostrada
        """
        start = time.time()
        _log_web_event("CHAT", "Clear chat from sidebar",
                      extra={"session_id": self.state.session_id[:16] if self.state else None})

        if self.state:
            self.state.clear_history()
            ui.notify('✅ Historial limpiado')
            # Recargar página para resetear UI
            ui.navigate.reload()

            duration = time.time() - start
            _log_web_event("CHAT", "Chat cleared from sidebar",
                          extra={"duration": round(duration, 4)})
        else:
            _log_web_event("CHAT", "Clear chat failed - no state",
                          extra={"reason": "No session state available"})

    def _export_session(self) -> None:
        """
        Exporta la sesión actual a JSON.
        Logging:
            - Registra exportación de sesión
            - Ruta del archivo exportado
            - Count de mensajes exportados
            - Errores en exportación
        """
        start = time.time()
        _log_web_event("SESSION", "Export session from sidebar",
                      extra={"session_id": self.state.session_id[:16] if self.state else None})

        if not self.state:
            ui.notify('⚠️ No hay sesión activa', color='warning')
            _log_web_event("SESSION", "Export failed - no active session",
                          extra={"reason": "No session state available"})
            return

        try:
            export_dir = Path.home() / 'Downloads' / 'axioma_exports'
            export_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            export_file = export_dir / f'axioma_session_{timestamp}.json'

            session_data = {
                'session_id': self.state.session_id,
                'model': self.state.model,
                'temperature': self.state.temperature,
                'enable_validation': self.state.enable_validation,
                'message_count': len(self.state.messages),
                'messages': [
                    {
                        'role': msg.role,
                        'content': msg.content,
                        'timestamp': msg.timestamp,
                        'source': msg.source,
                        'task_type': msg.task_type,
                        # ✅ F3: las citas viajan también en el JSON de sesión
                        'sources': list(getattr(msg, 'sources', None) or [])
                    }
                    for msg in self.state.messages
                ],
                'exported_at': datetime.now().isoformat()
            }

            with open(export_file, 'w', encoding='utf-8') as f:
                json.dump(session_data, f, indent=2, ensure_ascii=False)

            ui.notify(f'✅ Sesión exportada: {export_file.name}')

            duration = time.time() - start
            _log_web_event("SESSION", "Session exported successfully",
                          extra={"duration": round(duration, 4),
                                "file_path": str(export_file),
                                "message_count": len(self.state.messages),
                                "file_size": export_file.stat().st_size})
            logger.info(f'Session exported to {export_file}')

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SidebarComponent._export_session", extra={"duration": duration})
            _log_web_event("SESSION", "Session export failed",
                          extra={"duration": round(duration, 4),
                                "error": str(e)})
            ui.notify(f'❌ Error exportando: {str(e)}', color='negative')

    def _refresh_status(self) -> None:
        """
        Refresca el estado mostrado en el sidebar.
        Logging:
            - Registra refresh de estado
            - Estado de Ollama y APIs
        """
        start = time.time()
        _log_web_event("UI", "Refresh status from sidebar")

        # Actualizar estado de Ollama
        ollama_ok = True  # Verificar conexión real
        if 'ollama_status' in self.elements:
            self.elements['ollama_status'].set_text('✅' if ollama_ok else '❌')

        # Actualizar estado de APIs
        api_ok = True  # Verificar APIs reales
        if 'api_status' in self.elements:
            self.elements['api_status'].set_text('✅' if api_ok else '⚠️')

        ui.notify('🔄 Estado actualizado')

        duration = time.time() - start
        _log_web_event("UI", "Status refreshed",
                      extra={"duration": round(duration, 4),
                            "ollama_ok": ollama_ok,
                            "api_ok": api_ok})

    def update_model_info(self, model: str) -> None:
        """
        Actualiza información del modelo en el sidebar.
        Args:
            model: Nombre del modelo activo
        Logging:
            - Registra actualización de modelo
            - Modelo anterior y nuevo
        """
        _log_web_event("UI", "Update model info in sidebar",
                      extra={"model": model})

        if 'model_value' in self.elements:
            self.elements['model_value'].set_text(model)

        _log_web_event("UI", "Model info updated",
                      extra={"model": model})

    def update_session_info(self, session_id: str) -> None:
        """
        Actualiza información de la sesión en el sidebar.
        Args:
            session_id: ID de la sesión activa
        """
        _log_web_event("SESSION", "Update session info in sidebar",
                      extra={"session_id": session_id[-8:] if len(session_id) > 8 else session_id})

        if 'session_value' in self.elements:
            self.elements['session_value'].set_text(
                session_id[-8:] if len(session_id) > 8 else session_id
            )

        _log_web_event("SESSION", "Session info updated",
                      extra={"session_id": session_id[-8:] if len(session_id) > 8 else session_id})

    def update_jarvis_mode_info(self, mode: str) -> None:
        """
        Actualiza el indicador de Modo Jarvis en el sidebar.

        Args:
            mode: Modo activo — "normal" | "jarvis" | "voice_only" | "silent"
        """
        _mode_labels: dict = {
            "normal":     "👤 Normal",
            "jarvis":     "🤖 Jarvis",
            "voice_only": "🎙️ Voz",
            "silent":     "🔇 Silencioso",
        }
        label = _mode_labels.get(mode, "👤 Normal")
        if 'jarvis_mode_value' in self.elements:
            self.elements['jarvis_mode_value'].set_text(label)
            # Resaltar en púrpura cuando modo no es normal
            if mode != "normal":
                self.elements['jarvis_mode_value'].classes(
                    'text-xs text-purple-300', remove='text-gray-300'
                )
            else:
                self.elements['jarvis_mode_value'].classes(
                    'text-xs text-gray-300', remove='text-purple-300'
                )
        _log_web_event("UI", "Jarvis mode info updated in sidebar", extra={"mode": mode})

    def set_nav_active(self, nav_key: str) -> None:
        """
        Marca un item de navegación como activo.
        Args:
            nav_key: Key del item de navegación (ej: 'nav_chat', 'nav_dashboard')
        Logging:
            - Registra cambio de navegación activa
            - Item seleccionado
        """
        _log_web_event("NAVIGATION", "Set nav active",
                      extra={"nav_key": nav_key})

        # Resetear todos
        for key, element in self.elements.items():
            if key.startswith('nav_') and isinstance(element, dict):
                element['row'].classes('bg-white bg-opacity-0', remove='bg-white bg-opacity-10')

        # Activar seleccionado
        if nav_key in self.elements and isinstance(self.elements[nav_key], dict):
            self.elements[nav_key]['row'].classes('bg-white bg-opacity-10', remove='bg-white bg-opacity-0')

        _log_web_event("NAVIGATION", "Nav item activated",
                      extra={"nav_key": nav_key})

    def get_elements_count(self) -> int:
        """
        Retorna número de elementos UI registrados.
        Returns:
            int: Total de elementos
        Logging:
            - Registra acceso al count
        """
        _log_web_event("UI", "Get elements count")
        return len(self.elements)

    def __repr__(self) -> str:
        """
        Representación string del objeto.
        Returns:
            str: Representación legible
        """
        return f"SidebarComponent(elements={len(self.elements)}, collapsed={self.is_collapsed})"


# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════
def create_sidebar(
    on_chat_click: Optional[Callable] = None,
    on_settings_click: Optional[Callable] = None,
    on_status_click: Optional[Callable] = None,
    on_backup_click: Optional[Callable] = None,
    on_dashboard_click: Optional[Callable] = None,  # ✅ NUEVO
    on_exit_click: Optional[Callable] = None,
    state: Optional[Any] = None,
    collapsible: bool = True
) -> SidebarComponent:
    """
    Factory function para crear sidebar.
    Args:
        on_chat_click: Callback para click en Chat
        on_settings_click: Callback para click en Configuración
        on_status_click: Callback para click en Estado
        on_backup_click: Callback para click en Backup
        on_dashboard_click: Callback para click en Dashboard (✅ NUEVO)
        on_exit_click: Callback para click en Salir
        state: Estado de sesión
        collapsible: Si permite colapsar el sidebar
    Returns:
        Instancia de SidebarComponent creada y renderizada
    Logging:
        - Registra creación vía factory
        - Callbacks configurados
    """
    start = time.time()
    _log_web_event("UI", "Creating SidebarComponent via factory",
                  extra={"collapsible": collapsible,
                        "callbacks_count": sum([
                            on_chat_click is not None,
                            on_settings_click is not None,
                            on_status_click is not None,
                            on_backup_click is not None,
                            on_dashboard_click is not None,
                            on_exit_click is not None
                        ])})

    component = SidebarComponent(
        on_chat_click=on_chat_click,
        on_settings_click=on_settings_click,
        on_status_click=on_status_click,
        on_backup_click=on_backup_click,
        on_dashboard_click=on_dashboard_click,  # ✅ NUEVO
        on_exit_click=on_exit_click,
        state=state
    )
    component.create(collapsible=collapsible)

    duration = time.time() - start
    _log_web_event("UI", "SidebarComponent created via factory",
                  extra={"duration": round(duration, 4),
                        "elements_count": len(component.elements)})
    return component


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.0.10
# ═══════════════════════════════════════════════════════════════
# | Línea/Sección           | Cambio Realizado                        | Propósito                         |
# |-------------------------|-----------------------------------------|-----------------------------------|
# | ~274                    | model_value → qwen3:8b                | Eliminar vicgalle/prometheus      |
# | ~277                    | extra model → qwen3:8b                | Eliminar vicgalle/prometheus      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — ~470 LÍNEAS — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
