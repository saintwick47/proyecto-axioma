#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/utils/__init__.py — Exports públicos del paquete de utilidades
# Ruta: /ruta/a/axioma/src/utils/__init__.py
# ═══════════════════════════════════════════════════════════════
"""
Módulo de utilidades para AXIOMA.

Submódulos:
- ui: Utilidades de interfaz de usuario (colores, helpers de print)
- geolocator: Detección de ubicación y timezone
- ambiguity_detector: Detección de ambigüedad en queries
- db_pool: Connection pool de SQLite compartido
- provider_circuit: Circuit breaker por proveedor externo
- file_text: Extracción de texto de archivos (PDF/DOCX/plano)

Ejemplo de uso:
    from src.utils import Colors, print_success
    from src.utils.geolocator import get_geolocator
    from src.utils.ambiguity_detector import AmbiguityDetector
"""

# Imports de UI consolidada
from .ui import (
    Colors,
    C,
    print_header,
    print_success,
    print_error,
    print_warning,
    print_info,
    print_header_extended,
    print_success_extended,
    print_error_extended,
    print_warning_extended,
    print_info_extended,
    ok,
    fail,
    warn,
    info,
    head,
    step,
)

# Fase 3: Connection pool consolidado
from .db_pool import SQLiteConnectionPool

__all__ = [
    # DB
    'SQLiteConnectionPool',
    # UI
    'Colors',
    'C',
    'print_header',
    'print_success',
    'print_error',
    'print_warning',
    'print_info',
    'print_header_extended',
    'print_success_extended',
    'print_error_extended',
    'print_warning_extended',
    'print_info_extended',
    'ok',
    'fail',
    'warn',
    'info',
    'head',
    'step',
]
