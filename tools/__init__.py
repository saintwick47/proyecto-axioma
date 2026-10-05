#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v3.1 — Módulo de Herramientas
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tools/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-06 (actualizado 2026-09-01)
# Propósito: exports para herramientas de utilidad
# ✅ 2026-09-01: health_checker + setup_validator + system_diagnostic
#    fusionados en tools/system_check.py (ver system_check.py).
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any, Dict, List

__version__ = "0.5.1"
__author__ = "SaintWick"
__all__ = ["system_check", "model_tester", "structure_detector"]


def __getattr__(name: str) -> Any:
    """Lazy loading de componentes del módulo Tools.

    ✅ v0.5.1 (2026-09-01): usa importlib.import_module — el patrón
    `from . import X` dentro de __getattr__ del paquete causa RecursionError
    al resolver el submódulo como atributo.
    """
    import importlib
    if name in ("system_check", "health_checker", "setup_validator",
                "system_diagnostic"):
        # health_checker/setup_validator/system_diagnostic ahora viven
        # dentro de system_check (fusión 2026-09-01) — retro-compat.
        return importlib.import_module(".system_check", __name__)
    elif name == "model_tester":
        return importlib.import_module(".model_tester", __name__)
    elif name == "structure_detector":
        return importlib.import_module(".structure_detector", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def list_tools() -> List[Dict[str, Any]]:
    """
    Lista todas las herramientas disponibles.

    Returns:
        Lista de dicts con info de cada herramienta
    """
    return [
        {"name": "system_check", "description": "Diagnóstico unificado (health+setup+diagnostic)", "critical": True},
        {"name": "model_tester", "description": "Testea modelos LLM", "critical": False},
        {"name": "structure_detector", "description": "Genera reporte de estructura", "critical": False},
    ]


def health_check() -> Dict[str, Any]:
    """
    Verifica estado de las herramientas.

    Returns:
        Dict con estado de cada herramienta
    """
    results = {
        "system_check": False,
        "model_tester": False,
        "structure_detector": False,
    }

    try:
        from . import system_check
        results["system_check"] = True
    except Exception:
        results["system_check"] = False

    try:
        from . import model_tester
        results["model_tester"] = True
    except Exception:
        results["model_tester"] = False

    try:
        from . import structure_detector
        results["structure_detector"] = True
    except Exception:
        results["structure_detector"] = False

    return results
