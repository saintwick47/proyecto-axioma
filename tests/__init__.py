# AXIOMA Package
#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v3.2 — Módulo de Tests
# ✅ v3.2.0 (2026-08-25): Suite integral (tests/test_axioma_*.py).
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/__init__.py
# Autor: SaintWick
# Propósito: exports para suite de tests pytest
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any, Dict, List

__version__ = "3.2.0"
__author__ = "SaintWick"
__all__ = ["conftest", "axioma_report", "list_test_suites", "health_check", "run_tests"]

# Suites de la suite integral (cada archivo ≤ 750 líneas)
SUITES = {
    "test_axioma_01_config_domain": "Estructura del proyecto, config/ y dominio",
    "test_axioma_02_core": "Núcleo (core): dispatcher, colas, planner, hardware, calidad",
    "test_axioma_03_llm_router_memory": "LLM, router de intenciones y memoria",
    "test_axioma_04_agents": "Agentes: base, coder, analyst, loop, parallel executor",
    "test_axioma_04_agents": "Agentes (incluye herramientas MCP y multimodal, fusionado)",
    "test_axioma_06_web_handlers_analisis": "Web, handlers, prompts, utils + análisis global",
    "test_axioma_26_rac_estatico": "RAC: validación estática, chunking AST y filtro anti-alucinación",
}


def __getattr__(name: str) -> Any:
    """Lazy loading de componentes del módulo Tests."""
    from importlib import import_module
    if name == "conftest":
        return import_module(".conftest", __name__)
    if name == "axioma_report":
        return import_module(".axioma_report", __name__)
    if name in SUITES:
        return import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def list_test_suites() -> List[Dict[str, Any]]:
    """
    Lista todas las suites de tests disponibles.

    Returns:
        Lista de dicts con info de cada suite
    """
    return [
        {"name": name, "description": desc}
        for name, desc in SUITES.items()
    ]


def health_check() -> Dict[str, Any]:
    """
    Verifica estado de la suite de tests (importabilidad).

    Returns:
        Dict con estado de cada suite
    """
    results: Dict[str, Any] = {"conftest": False}
    try:
        from . import conftest  # noqa: F401
        results["conftest"] = True
    except Exception:
        results["conftest"] = False

    for name in SUITES:
        results[name] = False
        try:
            __getattr__(name)
            results[name] = True
        except Exception:
            results[name] = False
    return results


def run_tests(suite: Optional[str] = None, verbose: bool = False) -> int:
    """
    Ejecuta tests de manera programática.

    Args:
        suite: Nombre de la suite a ejecutar (None = todas)
        verbose: Mostrar output detallado

    Returns:
        Código de éxito (0 = todos pasan, 1+ = fallos)
    """
    import pytest

    args = ["tests/"]

    if suite:
        if suite in SUITES:
            args = [f"tests/{suite}.py"]
        else:
            raise ValueError(f"Suite desconocida: {suite}")

    if verbose:
        args.append("-v")
    else:
        args.append("-q")

    return pytest.main(args)
