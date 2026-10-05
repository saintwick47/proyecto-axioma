#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/suite_utils.py
# Autor: SaintWick
# AXIOMA — tests/suite_utils.py
# Helpers compartidos de la suite integral (evita duplicar `_import`
# en los 6 archivos test_axioma_*.py — detectado por axioma_inspector).
# No es un archivo de test (pytest no lo recolecta).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import importlib

try:
    from .axioma_report import check
except ImportError:  # pragma: no cover
    from axioma_report import check


def import_safe(mod: str):
    """Importa un módulo; devuelve el módulo o un string de error."""
    try:
        return importlib.import_module(mod)
    except Exception as e:  # noqa: BLE001
        return f"IMPORT_ERROR: {type(e).__name__}: {e}"


def has_symbols(mod: str, symbols: list[str]) -> tuple[bool, str]:
    """Verifica que el módulo exista e importe y tenga los símbolos."""
    m = import_safe(mod)
    if isinstance(m, str):
        return False, m
    missing = [s for s in symbols if not hasattr(m, s)]
    if missing:
        return False, f"faltan símbolos: {', '.join(missing)}"
    return True, f"{len(symbols)} símbolos OK"


def check_import(name: str, mod: str, section: str | None = None):
    """Registra en el reporte si un módulo importa correctamente."""
    m = import_safe(mod)
    check(name, not isinstance(m, str), "" if not isinstance(m, str) else m, section)
    return m


def api_check(mod: str, symbols: list[str], section: str | None = None) -> None:
    """Registra en el reporte importación + API pública de un módulo."""
    m = import_safe(mod)
    if isinstance(m, str):
        check(f"{mod} importable", False, m, section)
        return
    check(f"{mod} importable", True, "OK", section)
    if not symbols:
        return
    missing = [s for s in symbols if not hasattr(m, s)]
    check(f"{mod} API pública", not missing,
          "faltan: " + ", ".join(missing) if missing else f"{len(symbols)} símbolos OK", section)
