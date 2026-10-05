#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tools/analyze_tests_imports.py
# Autor: SaintWick
# AXIOMA — Herramienta de análisis: tests vs src/config
# Compara los imports de cada archivo de test contra los módulos
# reales del sistema y verifica la existencia de los símbolos
# importados (análisis estático con ast).
# ═══════════════════════════════════════════════════════════════
import ast
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
SRC_DIR = ROOT / "src"
CONFIG_DIR = ROOT / "config"

sys.path.insert(0, str(ROOT))


def extract_imports(tree):
    """Devuelve (modulos, simbolos) donde modulos es un set de 'a.b.c'
    importados y simbolos es lista de (modulo, [nombres]) para 'from x import y'."""
    mods = set()
    symbols = []  # (modulo, [nombres])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue  # imports relativos o locales
            mods.add(node.module)
            names = [a.name for a in node.names if a.name != "*"]
            if names:
                symbols.append((node.module, names))
    return mods, symbols


def module_exists(mod):
    if mod == "config" or mod.startswith("config."):
        rel = mod[len("config."):] if mod != "config" else ""
        path = CONFIG_DIR / (rel.replace(".", "/") + ".py")
        pkg = CONFIG_DIR / rel.replace(".", "/") / "__init__.py"
        return path.exists() or pkg.exists()
    if not mod.startswith("src."):
        return False
    rel = mod[len("src."):]
    path = SRC_DIR / (rel.replace(".", "/") + ".py")
    pkg = SRC_DIR / rel.replace(".", "/") / "__init__.py"
    return path.exists() or pkg.exists()


def resolve_sys_module(mod):
    """Intenta importar el módulo del sistema (src/config) y devuelve el módulo o None."""
    try:
        return importlib.import_module(mod)
    except Exception:
        return None


def check_symbol(module_obj, name):
    if module_obj is None:
        return "module_import_error"
    if hasattr(module_obj, name):
        return "ok"
    return "missing"


def main():
    report = []
    total_missing_symbols = 0
    files_with_issues = []

    for test_file in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        mods, symbols = extract_imports(tree)

        sys_mods = sorted(m for m in mods if m.startswith(("src.", "config")))
        other_mods = sorted(m for m in mods if not m.startswith(("src.", "config")))

        entries = []
        missing = []
        for m in sys_mods:
            if not module_exists(m):
                missing.append(f"{m} [MÓDULO NO EXISTE]")
                continue
            mod_obj = resolve_sys_module(m)
            if mod_obj is None:
                missing.append(f"{m} [MÓDULO NO IMPORTA]")
            else:
                entries.append(f"{m} OK")

        for m, names in symbols:
            if not m.startswith(("src.", "config")):
                continue
            if not module_exists(m):
                missing.append(f"{m} [MÓDULO NO EXISTE] (from-import {names})")
                continue
            mod_obj = resolve_sys_module(m)
            if mod_obj is None:
                missing.append(f"{m} [MÓDULO NO IMPORTA] (from-import {names})")
                continue
            for n in names:
                status = check_symbol(mod_obj, n)
                if status == "ok":
                    entries.append(f"{m}.{n} OK")
                else:
                    missing.append(f"{m}.{n} [NO EXISTE]")

        n_missing = len(missing)
        total_missing_symbols += n_missing
        if n_missing:
            files_with_issues.append((test_file.name, n_missing))

        report.append((test_file.name, entries, missing, other_mods))

    # ── Salida ──────────────────────────────────────────────────
    print("═" * 78)
    print("ANÁLISIS DE IMPORTS: tests/ vs src/ + config/")
    print("═" * 78)
    for name, entries, missing, other in report:
        print(f"\n▶ {name}  ({len(entries)} símbolos OK, {len(missing)} problemas)")
        for m in missing:
            print(f"    ✗ {m}")
        for m in entries:
            print(f"    ✓ {m}")
        if other:
            print(f"    (otros imports: {', '.join(other[:6])})")

    print("\n" + "═" * 78)
    print(f"RESUMEN: {len(report)} archivos de test analizados")
    print(f"Archivos con problemas de imports: {len(files_with_issues)}")
    for name, n in files_with_issues:
        print(f"    - {name}: {n} símbolos/módulos con problemas")
    print(f"Total de símbolos/módulos con problemas: {total_missing_symbols}")
    print("═" * 78)


if __name__ == "__main__":
    main()
