#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.2.0 → v1.3.0 → v1.4.0 — DependencyResolver (Resolución Inteligente de Dependencias)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/dependency_resolver.py
# Autor: SaintWick
# Fecha: 2026-05-04
#
# v1.2.0 — FASE 2: Detecta ModuleNotFoundError y extrae el
#           paquete faltante para auto-instalación en el venv.
# v1.3.0 — FASE 6: Whitelist + blocklist de paquetes para
#           auto-instalación segura. Método is_package_allowed().
# v1.4.0 — FASE 7: Pre-scan de imports + resolve a paquetes pip.
#           extract_all_imports() y resolve_to_packages().
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MODULE_NOT_FOUND_RE = re.compile(
    r"ModuleNotFoundError[:]?\s+No module named\s+['\"]?([^'\"\s]+)['\"]?",
    re.IGNORECASE,
)


class DependencyResolver:
    """Extrae nombres de paquetes faltantes desde errores de Python."""

    # Paquetes seguros para auto-instalación (comunes, sin riesgo)
    _DEFAULT_WHITELIST: frozenset = frozenset({
        "pandas", "numpy", "requests", "matplotlib", "seaborn",
        "scipy", "scikit-learn", "openpyxl", "xlrd",
        "beautifulsoup4", "bs4", "lxml",
        "Pillow", "plotly", "bokeh", "altair",
        "flask", "django", "fastapi", "uvicorn", "httpx",
        "sqlalchemy", "psycopg2", "pymongo", "redis",
        "pyyaml", "toml", "python-dotenv",
        "pytest", "black", "flake8", "mypy", "ruff",
        "reportlab", "fpdf", "pdfplumber",
        "feedparser", "sympy", "networkx", "rich", "typer", "click",
        "pydantic", "attrs",
        # NOTA: csv, json, os, sys, etc. son stdlib — NO van aquí
    })

    # Patrones explícitamente bloqueados (se verifican como substring)
    _BLOCKLIST: frozenset = frozenset({
        "malicious", "exploit", "backdoor", "rootkit",
        "keylogger", "stealer", "cryptominer",
    })

    # Regex para detectar líneas import en el código
    _IMPORT_RE: re.Pattern = re.compile(
        r'^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)',
        re.MULTILINE,
    )

    # Mapeo de módulos a paquetes pip (solo los que difieren del nombre de módulo)
    _MODULE_TO_PACKAGE: dict = {
        "cv2": "opencv-python",
        "bs4": "beautifulsoup4",
        "sklearn": "scikit-learn",
        "PIL": "Pillow",
        "dotenv": "python-dotenv",
        "yaml": "pyyaml",
        "plotly_express": "plotly",
    }

    # Módulos de la stdlib que NUNCA necesitan pip install
    _STDLIB: frozenset = frozenset({
        "os", "sys", "json", "re", "time", "datetime", "math",
        "collections", "itertools", "functools", "pathlib",
        "io", "string", "typing", "dataclasses", "abc",
        "logging", "argparse", "subprocess", "threading",
        "asyncio", "hashlib", "random", "copy", "pprint",
        "traceback", "inspect", "textwrap", "unicodedata",
        "enum", "decimal", "fractions", "statistics",
        "tempfile", "shutil", "glob", "fnmatch",
        "struct", "array", "queue", "heapq", "bisect",
        "contextlib", "warnings", "contextvars", "types",
        "weakref", "operator", "pickle", "csv", "json",
    })

    def extract_missing_package(self, error_text: str) -> Optional[str]:
        if not error_text or "ModuleNotFoundError" not in error_text:
            return None
        match = _MODULE_NOT_FOUND_RE.search(error_text)
        if match:
            pkg = match.group(1)
            if "." in pkg:
                pkg = pkg.split(".")[0]
            logger.info(f"[DependencyResolver] Paquete faltante detectado: {pkg}")
            return pkg
        return None

    def is_installable_error(self, error_text: str) -> bool:
        return self.extract_missing_package(error_text) is not None

    def is_package_allowed(self, package: str) -> bool:
        """
        Verifica si un paquete está permitido para auto-instalación.
        Primero verifica blocklist (substring match), luego whitelist (exact match).
        """
        pkg_lower = package.lower().strip()
        # Blocklist primero: si el nombre contiene algún patrón prohibido, rechazar
        for blocked in self._BLOCKLIST:
            if blocked in pkg_lower:
                logger.warning(f"[DependencyResolver] Paquete bloqueado por patrón: {package}")
                return False
        # Whitelist: debe estar explícitamente permitido
        return pkg_lower in self._DEFAULT_WHITELIST

    def extract_all_imports(self, code: str) -> list:
        """
        Extrae todos los nombres de módulos importados en el código fuente.
        Retorna lista de nombres únicos, sin repetidos.
        No distingue entre 'import foo' y 'from foo import bar' — ambos retornan 'foo'.
        """
        if not code:
            return []
        matches = self._IMPORT_RE.findall(code)
        return list(set(matches))

    def resolve_to_packages(self, modules: list) -> list:
        """
        Convierte nombres de módulos a nombres de paquetes pip instalables.
        - Aplica mapeo _MODULE_TO_PACKAGE (ej: PIL → Pillow)
        - Filtra módulos de stdlib
        - Filtra módulos no permitidos por whitelist
        - Retorna lista única de paquetes
        """
        packages = set()
        for mod in modules:
            # Filtrar stdlib
            if mod.lower() in self._STDLIB:
                continue
            # Mapear a nombre de paquete pip
            pkg = self._MODULE_TO_PACKAGE.get(mod, mod)
            # Doble check: el paquete mapeado tampoco debe ser stdlib
            if pkg.lower() in self._STDLIB:
                continue
            # Verificar whitelist
            if self.is_package_allowed(pkg):
                packages.add(pkg)
        return list(packages)


_resolver: Optional[DependencyResolver] = None


def get_dependency_resolver() -> DependencyResolver:
    global _resolver
    if _resolver is None:
        _resolver = DependencyResolver()
    return _resolver
