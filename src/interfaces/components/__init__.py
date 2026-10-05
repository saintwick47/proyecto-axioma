#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Componentes NiceGUI (Exports)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/components/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-27
# Propósito: Exports centralizados para componentes reutilizables de NiceGUI
# ═══════════════════════════════════════════════════════════════
import logging
from typing import Optional, Any, Dict, List

# ═══════════════════════════════════════════════════════════════
# METADATA DEL MÓDULO
# ═══════════════════════════════════════════════════════════════
__version__ = "0.0.9"
__author__ = "SaintWick"
__all__ = [
    # Componentes de chat
    "ChatComponent",
    "ChatMessage",
    # Componentes de navegación
    "SidebarComponent",
    # Componentes de modales
    "StatusDialog",
    "SettingsDialog",
    "ExitDialog",
    # Factory functions — chat y navegación
    "create_chat_component",
    "create_sidebar",
    # Factory functions — modales
    "create_status_dialog",
    "create_settings_dialog",
    "create_exit_dialog",
    # Utilidades
    "list_components",
    "get_component_info",
    "health_check",
]

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# IMPORTS DE COMPONENTES (con fallback graceful)
# ═══════════════════════════════════════════════════════════════
# Si nicegui no está instalado, las clases quedan en None y las
# funciones de utilidad reportan disponibilidad parcial.

try:
    from .chat import (
        ChatComponent,
        ChatMessage,
        create_chat_component,
    )
except ImportError as e:
    ChatComponent = None
    ChatMessage = None
    create_chat_component = None
    logger.warning(f"Chat component no disponible: {e}")

try:
    from .modals import (
        StatusDialog,
        SettingsDialog,
        ExitDialog,
        create_status_dialog,
        create_settings_dialog,
        create_exit_dialog,
    )
except ImportError as e:
    StatusDialog = None
    SettingsDialog = None
    ExitDialog = None
    create_status_dialog = None
    create_settings_dialog = None
    create_exit_dialog = None
    logger.warning(f"Modals component no disponible: {e}")

try:
    from .sidebar import (
        SidebarComponent,
        create_sidebar,
    )
except ImportError as e:
    SidebarComponent = None
    create_sidebar = None
    logger.warning(f"Sidebar component no disponible: {e}")


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════
_COMPONENT_REGISTRY = [
    ("ChatComponent", "chat", "Componente de chat reutilizable", "chat.py", lambda: ChatComponent),
    ("SidebarComponent", "navigation", "Barra lateral de navegación", "sidebar.py", lambda: SidebarComponent),
    ("StatusDialog", "modal", "Diálogo de estado del sistema", "modals.py", lambda: StatusDialog),
    ("SettingsDialog", "modal", "Diálogo de configuración", "modals.py", lambda: SettingsDialog),
    ("ExitDialog", "modal", "Diálogo de confirmación de salida", "modals.py", lambda: ExitDialog),
]


def list_components() -> List[Dict[str, Any]]:
    """
    Lista todos los componentes disponibles.

    Returns:
        Lista de dicts con información de cada componente
    """
    components = []
    for name, ctype, description, file, getter in _COMPONENT_REGISTRY:
        obj = getter()
        if obj is not None:
            components.append({
                "name": name,
                "type": ctype,
                "description": description,
                "file": file,
                "available": True,
            })
    return components


def get_component_info(component_name: str) -> Optional[Dict[str, Any]]:
    """
    Obtiene información de un componente específico.

    Args:
        component_name: Nombre del componente (ej: 'StatusDialog')

    Returns:
        Dict con información del componente o None si no existe
    """
    for name, ctype, description, file, getter in _COMPONENT_REGISTRY:
        if name == component_name:
            obj = getter()
            if obj is not None:
                return {
                    "name": name,
                    "type": ctype,
                    "description": description,
                    "file": file,
                    "available": True,
                }
    return None


def health_check() -> Dict[str, Any]:
    """
    Verifica estado de los componentes.

    Returns:
        Dict con estado de cada grupo de componentes
    """
    return {
        "version": __version__,
        "components": {
            "chat": {
                "available": ChatComponent is not None,
                "factory": create_chat_component is not None,
            },
            "sidebar": {
                "available": SidebarComponent is not None,
                "factory": create_sidebar is not None,
            },
            "modals": {
                "status_dialog": StatusDialog is not None,
                "settings_dialog": SettingsDialog is not None,
                "exit_dialog": ExitDialog is not None,
                "factories": all([
                    create_status_dialog is not None,
                    create_settings_dialog is not None,
                    create_exit_dialog is not None,
                ]),
            },
        },
        "all_available": all(getter() is not None for _, _, _, _, getter in _COMPONENT_REGISTRY),
    }
