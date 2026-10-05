#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.0 — Módulo de Interfaz Web (NiceGUI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-12
# Propósito: Exports para servidor web NiceGUI + API FastAPI
# ═══════════════════════════════════════════════════════════════
from typing import Any, Dict

# ═══════════════════════════════════════════════════════════════
# METADATA DEL MÓDULO
# ═══════════════════════════════════════════════════════════════
__version__ = "1.0.0"
__author__ = "SaintWick"

__all__ = [
    "app",
    "server",
    "routes",
    "states",
    "components",
    "get_app",
    "health_check",
    "start_server",
    "get_router",
    "create_api_app",
]

# ═══════════════════════════════════════════════════════════════
# LAZY LOADING DE SUBMÓDULOS
# ═══════════════════════════════════════════════════════════════
# Evita importar submódulos pesados al hacer `import src.interfaces.web`.
# Python cachea módulos en sys.modules — no necesitamos caché manual.
# _importing previene recursión si un submódulo importa del paquete padre.
_importing: set = set()


def __getattr__(name: str) -> Any:
    """
    Lazy loading de submódulos del paquete web.

    Args:
        name: Nombre del submódulo a cargar

    Returns:
        Módulo solicitado

    Raises:
        AttributeError: Si el nombre no existe o se detecta recursión

    Example:
        >>> from src.interfaces.web import server
        >>> server_instance = server.WebServer()
    """
    if name not in ("app", "server", "routes", "states", "components"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    if name in _importing:
        raise AttributeError(f"Recursion detected importing {name}")

    _importing.add(name)
    try:
        import importlib
        return importlib.import_module(f".{name}", __name__)
    finally:
        _importing.discard(name)


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════
def get_app() -> Any:
    """
    Obtiene instancia de la aplicación NiceGUI.

    Returns:
        Módulo app de NiceGUI (objeto ui)

    Example:
        >>> ui = get_app()
        >>> ui.run(host='0.0.0.0', port=8000)
    """
    from .app import ui
    return ui


def health_check() -> Dict[str, Any]:
    """
    Verifica estado del servidor web.

    Returns:
        Dict con estado del servidor

    Example:
        >>> status = health_check()
        >>> print(status['status'])
        'ok'
    """
    from config.settings import settings

    return {
        "status": "ok",
        "host": settings.host if hasattr(settings, 'host') else "127.0.0.1",
        "port": settings.port if hasattr(settings, 'port') else 8000,
        "version": __version__,
        "frontend": "nicegui",
        "backend": "fastapi",
        "components": {
            "chat": True,
            "sidebar": True,
            "modals": True,
        },
    }


def start_server(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """
    Inicia servidor NiceGUI. Wrapper para uso directo desde main.py.

    Args:
        host: Host del servidor
        port: Puerto del servidor
        reload: Recargar automáticamente (dev only)

    Example:
        >>> start_server(host='0.0.0.0', port=8000)
    """
    from .app import start_server as _start_server
    _start_server(host=host, port=port, reload=reload)


def get_router() -> Any:
    """
    Obtiene el router FastAPI para tests de API.
    Permite testear endpoints sin iniciar NiceGUI.

    Returns:
        FastAPI Router instance

    Example:
        >>> router = get_router()
        >>> from fastapi import FastAPI
        >>> app = FastAPI()
        >>> app.include_router(router)
    """
    from .routes import router
    return router


def create_api_app() -> Any:
    """
    Crea FastAPI app standalone para tests de API.
    Separa tests de API de tests de UI NiceGUI.

    Returns:
        FastAPI app instance

    Example:
        >>> api_app = create_api_app()
        >>> from fastapi.testclient import TestClient
        >>> client = TestClient(api_app)
    """
    from fastapi import FastAPI
    from .routes import router

    app = FastAPI(
        title="AXIOMA API",
        version=__version__,
        description="API REST para AXIOMA - Backend separado de NiceGUI UI"
    )
    app.include_router(router)
    return app
