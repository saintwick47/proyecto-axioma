#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Utilidades de UI Consolidadas
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/utils/ui.py
# Autor: SaintWick
# Fecha: 2026-03-25
# Propósito: Consolidar clases Colors y C de múltiples archivos
#            Eliminación de duplicación en 6 archivos de tools/tests
# ═══════════════════════════════════════════════════════════════
"""
Utilidades de UI para AXIOMA.

Clases consolidadas:
- Colors: Colores ANSI para output en terminal (reemplaza duplicados en tests/tools)
- C: Alias de Colors con métodos helper (reemplaza duplicados en diagnósticos)

Uso:
    from src.utils.ui import Colors, C, print_header, print_success

    # Método 1: Usar clase Colors
    print(f"{Colors.GREEN}Éxito{Colors.RESET}")

    # Método 2: Usar alias C (más corto)
    print(f"{C.OK}Éxito{C.END}")

    # Método 3: Usar funciones helper
    print_success("Operación completada")
"""


class Colors:
    """
    Colores ANSI para output en terminal.

    Consolidado de:
    - test_general.py (líneas 18-35)
    - verify_fixes.py (clase Colors)
    - test_full_flow.py (clase Colors)
    - commit.py (clase Colors)

    Atributos:
        GREEN: Color verde para éxitos
        RED: Color rojo para errores
        YELLOW: Color amarillo para advertencias
        BLUE: Color azul para información
        RESET: Resetear colores
        BOLD: Texto en negrita
        HEADER: Color magenta para headers (de verify_fixes)
        OKBLUE: Azul claro (de verify_fixes)
        OKCYAN: Cyan (de verify_fixes)
        OKGREEN: Verde (de verify_fixes)
        WARNING: Amarillo (de verify_fixes)
        FAIL: Rojo (de verify_fixes)
        ENDC: Reset (de verify_fixes)
        UNDERLINE: Subrayado (de verify_fixes)
        CYAN: Alias de OKCYAN para compatibilidad con test_suite.py
    """
    # Colores básicos (de test_general.py, test_full_flow.py, commit.py)
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

    # Colores extendidos (de verify_fixes.py)
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    # ✅ CORREGIDO: Agregado alias CYAN para compatibilidad con test_suite.py
    CYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    UNDERLINE = '\033[4m'


class C(Colors):
    """
    Alias de Colors con nombre corto para diagnósticos.

    Consolidado de:
    - axioma_diagnostico.py (clase C)
    - diagnostico_finanzas.py (clase C)

    Hereda todos los atributos de Colors y agrega:
        OK: Alias de GREEN
        FAIL: Alias de RED
        WARN: Alias de YELLOW
        INFO: Alias de BLUE
        HEAD: Alias de HEADER
        END: Alias de RESET
    """
    # Alias cortos (de axioma_diagnostico.py, diagnostico_finanzas.py)
    OK = Colors.GREEN
    FAIL = Colors.RED
    WARN = Colors.YELLOW
    INFO = Colors.BLUE
    HEAD = Colors.HEADER
    END = Colors.RESET


# ═══════════════════════════════════════════════════════════════
# FUNCIONES HELPER — CONSOLIDADAS
# ═══════════════════════════════════════════════════════════════

def print_header(text: str) -> None:
    """
    Imprime header con colores y separadores.

    Args:
        text: Texto del header
    """
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}\n")


def print_success(text: str) -> None:
    """
    Imprime mensaje de éxito con check verde.

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")


def print_error(text: str) -> None:
    """
    Imprime mensaje de error con X roja.

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")


def print_warning(text: str) -> None:
    """
    Imprime mensaje de advertencia con triángulo amarillo.

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.YELLOW}⚠️  {text}{Colors.RESET}")


def print_info(text: str) -> None:
    """
    Imprime mensaje informativo con i azul.

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.BLUE}ℹ️  {text}{Colors.RESET}")


# ═══════════════════════════════════════════════════════════════
# FUNCIONES HELPER ADICIONALES — DE VERIFY_FIXES.PY
# ═══════════════════════════════════════════════════════════════

def print_header_extended(text: str) -> None:
    """
    Imprime header extendido con colores magenta (de verify_fixes.py).

    Args:
        text: Texto del header
    """
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'═' * 70}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'═' * 70}{Colors.ENDC}\n")


def print_success_extended(text: str) -> None:
    """
    Imprime mensaje de éxito con formato extendido (de verify_fixes.py).

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.OKGREEN}✅ {text}{Colors.ENDC}")


def print_error_extended(text: str) -> None:
    """
    Imprime mensaje de error con formato extendido (de verify_fixes.py).

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.FAIL}❌ {text}{Colors.ENDC}")


def print_warning_extended(text: str) -> None:
    """
    Imprime mensaje de advertencia con formato extendido (de verify_fixes.py).

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.WARNING}⚠️  {text}{Colors.ENDC}")


def print_info_extended(text: str) -> None:
    """
    Imprime mensaje informativo con formato extendido (de verify_fixes.py).

    Args:
        text: Mensaje a imprimir
    """
    print(f"{Colors.OKCYAN}ℹ️  {text}{Colors.ENDC}")


# ═══════════════════════════════════════════════════════════════
# FUNCIONES HELPER ADICIONALES — DE DIAGNÓSTICOS
# ═══════════════════════════════════════════════════════════════

def ok(msg: str) -> None:
    """
    Imprime mensaje OK corto (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        msg: Mensaje a imprimir
    """
    print(f"  {C.OK}✅ {msg}{C.END}")


def fail(msg: str) -> None:
    """
    Imprime mensaje FAIL corto (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        msg: Mensaje a imprimir
    """
    print(f"  {C.FAIL}❌ {msg}{C.END}")


def warn(msg: str) -> None:
    """
    Imprime mensaje WARN corto (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        msg: Mensaje a imprimir
    """
    print(f"  {C.WARN}⚠️  {msg}{C.END}")


def info(msg: str) -> None:
    """
    Imprime mensaje INFO corto (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        msg: Mensaje a imprimir
    """
    print(f"  {C.INFO}ℹ️  {msg}{C.END}")


def head(msg: str) -> None:
    """
    Imprime header corto (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        msg: Texto del header
    """
    print(f"\n{C.HEAD}{C.BOLD}{'═'*70}\n{msg}\n{'═'*70}{C.END}")


def step(n: int, msg: str) -> None:
    """
    Imprime número de paso (de axioma_diagnostico.py, diagnostico_finanzas.py).

    Args:
        n: Número de paso
        msg: Mensaje del paso
    """
    print(f"\n{C.BOLD}[{n}] {msg}{C.END}")


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — CONSOLIDACIÓN UI
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                                  | Razón                        |
# |-------|-----------------------------------------|--------------------------------|
# | ~65   | ✅ CYAN = '\033[96m' agregado           | Fix AttributeError en test_suite.py |
# | ~65   | ✅ Docstring actualizado con CYAN       | Documentación consistente    |
# | TODAS | ✅ Header con ruta absoluta/autor/fecha | Cumplimiento protocolo       |
# | TODAS | ✅ Docstrings completos en funciones    | Claridad y mantenibilidad    |
# ═══════════════════════════════════════════════════════════════
# Archivos consolidados (clases locales eliminadas):
# 1. test_general.py — Colors eliminada
# 2. verify_fixes.py — Colors eliminada
# 3. test_full_flow.py — Colors eliminada
# 4. commit.py — Colors eliminada (mantener archivo, solo actualizar import)
# 5. axioma_diagnostico.py — C eliminada
# 6. diagnostico_finanzas.py — C eliminada
#
# Imports a actualizar en cada archivo:
#   from src.utils.ui import Colors, C, print_header, print_success, ...
#
# Beneficios:
# ✅ Eliminación de 6 duplicaciones de código
# ✅ Un solo punto de mantenimiento para UI
# ✅ Consistencia en todos los tests y herramientas
# ✅ Facilita cambios futuros de colores/formato
# ═══════════════════════════════════════════════════════════════
def fuentes_de_respuesta(resp, maximo: int = 8) -> list:
    """Fuentes citables de una respuesta (✅ 2026-09-29, medido con uso REAL).

    El CLI de la raíz (`main.py --chat`) imprimía solo `resp.content`, así que la
    respuesta citaba fuentes que el usuario **no podía ver** (la UI web sí las
    muestra). Para un dato factual, no poder verificarlas es justo lo que hace
    dudar de la información. Lee `sources` y, si está vacío, `metadata["sources"]`
    (los agentes llenan una u otra según el camino).
    """
    fuentes = [s for s in (getattr(resp, "sources", None) or []) if isinstance(s, str) and s.strip()]
    if not fuentes:
        meta = getattr(resp, "metadata", None) or {}
        fuentes = [s for s in (meta.get("sources") or []) if isinstance(s, str) and s.strip()]
    return fuentes[:maximo]


def print_fuentes(resp, maximo: int = 8) -> None:
    """Imprime las fuentes de una respuesta, si las hay (no imprime nada si no)."""
    fuentes = fuentes_de_respuesta(resp, maximo)
    if not fuentes:
        return
    print(f"📎 Fuentes ({len(fuentes)}):")
    for s in fuentes:
        print(f"   - {s}")


# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
