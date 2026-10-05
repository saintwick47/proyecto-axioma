# AXIOMA Package
#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v3.1 — Módulo de Interfaces
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: exports para módulos de interfaz (web + cli)
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any, Dict, List

__version__ = "0.4.0"
__author__ = "SaintWick"
__all__ = ["web", "cli"]


def __getattr__(name: str) -> Any:
    """Lazy loading de componentes del módulo Interfaces.

    ✅ 2026-09-29: `from . import cli` DENTRO de `__getattr__` **re-entra** en
    `__getattr__("cli")` y produce `RecursionError` (medido: al resolver
    `src.interfaces.cli.main` por atributo, p. ej. con `monkeypatch.setattr` por
    nombre). `importlib.import_module` no pasa por el acceso de atributo ⇒ no
    recursiona.
    """
    if name in ("web", "cli"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def list_interfaces() -> List[Dict[str, Any]]:
    """
    Lista todas las interfaces disponibles.

    Returns:
        Lista de dicts con info de cada interfaz
    """
    return [
        {"type": "web", "name": "Web UI", "description": "Interfaz web FastAPI + HTML/JS", "port": 8000},
        {"type": "cli", "name": "CLI", "description": "Interfaz de línea de comandos", "headless": True},
    ]


def health_check() -> Dict[str, bool]:
    """
    Verifica estado de las interfaces.

    Returns:
        Dict con estado de cada interfaz
    """
    results = {"web": False, "cli": False}

    try:
        from .web import server
        results["web"] = server is not None
    except Exception:
        results["web"] = False

    try:
        from .cli import main
        results["cli"] = main is not None
    except Exception:
        results["cli"] = False

    return results
