#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.0 — Módulo de Interfaz CLI
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/cli/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Exports para interfaz de línea de comandos
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any, Dict

__version__ = "0.4.0"
__author__ = "SaintWick"
__all__ = ["main", "run_cli", "health_check"]


# ═══════════════════════════════════════════════════════════════
# LAZY LOADING DE COMPONENTES
# ═══════════════════════════════════════════════════════════════

def __getattr__(name: str) -> Any:
    """
    Lazy loading de componentes del módulo CLI.

    ✅ ISSUE-145 (MEDIDO 2026-10-04): antes hacía `from . import main` acá adentro, y esa
    forma de importar vuelve a preguntar por el atributo `main` del paquete ⇒ se llama a
    sí misma sin fin (`RecursionError`). Medido: la forma `from src.interfaces.cli import
    main` **recursaba bajo pytest** (bajo Python normal andaba, por eso pasó desapercibido).
    Se importa por nombre completo con `importlib` y se cachea en `globals()` para que el
    próximo acceso ni siquiera pase por acá.
    """
    if name == "main":
        import importlib
        modulo = importlib.import_module(f"{__name__}.main")
        globals()["main"] = modulo
        return modulo
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════

def run_cli(args: Optional[list] = None) -> int:
    """
    Ejecuta la interfaz CLI.

    Args:
        args: Argumentos de línea de comandos

    Returns:
        Código de exito (0 = éxito, 1+ = error)
    """
    from .main import main
    import sys

    if args:
        old_argv = sys.argv
        sys.argv = [sys.argv[0]] + args
        try:
            return main()
        finally:
            sys.argv = old_argv
    else:
        return main()


def health_check() -> Dict[str, Any]:
    """
    Verifica estado de la CLI.

    Returns:
        Dict con estado de la CLI
    """
    return {
        "status": "ok",
        "version": __version__,
        "available": True,
    }


# ═══════════════════════════════════════════════════════════════
# RESUMEN
# ═══════════════════════════════════════════════════════════════
# ✅ SIN CAMBIOS: CLI se mantiene intacto (interfaz alternativa)
# ✅ SIN CAMBIOS: Mismo funcionamiento que versión anterior
# ✅ SIN CAMBIOS: Compatible con main.py root
# ═══════════════════════════════════════════════════════════════
