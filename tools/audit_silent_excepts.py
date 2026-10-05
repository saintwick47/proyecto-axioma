#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — tools/audit_silent_excepts.py
# Ruta: /ruta/a/axioma/tools/audit_silent_excepts.py
#
# ✅ v0.6.9v (ISSUE-019): audita los bloques `except` que degradan a DEBUG.
#
#   Problema: el proyecto usa "degrada en silencio para no romper el request"
#   (correcto), pero un ERROR DE CONTRATO (TypeError/AttributeError/NameError/
#   ImportError) logueado a DEBUG esconde un bug de integración — la feature
#   deja de funcionar sin que nadie se entere. Caso real: ISSUE-018.
#
#   Esta herramienta NO cambia código: lista los candidatos priorizados para
#   migrarlos gradualmente a `src.utils.degradation.log_degraded`, que sube a
#   WARNING solo los errores de contrato.
#
# Uso:
#   python tools/audit_silent_excepts.py              # resumen por archivo
#   python tools/audit_silent_excepts.py --top 20     # top N archivos
#   python tools/audit_silent_excepts.py --detail src/core/dispatcher_process.py
#   python tools/audit_silent_excepts.py --json       # salida JSON
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))   # ✅ permite importar tools.auditor.*
SRC = _ROOT / "src"

# Rutas donde el fallo silencioso implica pérdida de datos/features → prioridad alta
HIGH_VALUE_HINTS = (
    "memory", "gateway", "corpus", "dispatcher", "quality",
    "validator", "learning", "trace", "feedback", "session",
)


def _logs_at_debug(handler: ast.ExceptHandler) -> tuple[bool, str]:
    """True si el handler degrada sin log visible, con el detalle de sus llamadas.

    ✅ v0.6.9v: criterio UNIFICADO con el detector `silent_contract_swallow`
    (una sola fuente de verdad: `tools/auditor/aud_detectors_regresiones.py`).
    Antes esta herramienta y el detector usaban criterios distintos y daban
    números diferentes (p. ej. acá se bajaba a handlers ANIDADOS y se contaban
    mensajes de debug informativos como si fueran errores tragados).
    """
    from tools.auditor.aud_detectors_regresiones import (
        _handler_calls, _handler_reraises, _own_nodes,
    )
    calls = _handler_calls(handler)          # solo lo propio (sin anidados)
    if _handler_reraises(handler):
        return False, "raise"
    if calls & {"warning", "error", "critical", "exception", "log_degraded"}:
        return False, ",".join(sorted(calls))
    orden = [c for c in ("debug", "print") if c in calls]
    return True, ",".join(orden) or "(sin log)"


def audit_file(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    found: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            silent, calls = _logs_at_debug(node)
            if not silent:
                continue
            # ¿Captura amplia? (Exception / BaseException / sin tipo)
            broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
            )
            found.append({
                "file": str(path.relative_to(_ROOT)),
                "line": node.lineno,
                "broad": broad,
                "calls": calls,
            })
    return found


def _priority(filepath: str, broad: bool) -> int:
    """Prioridad 0 = más urgente."""
    p = 1 if broad else 2
    if any(h in filepath.lower() for h in HIGH_VALUE_HINTS):
        p -= 1
    return max(0, p)


def main() -> int:
    ap = argparse.ArgumentParser(description="Audita except→DEBUG (ISSUE-019).")
    ap.add_argument("--top", type=int, default=15, help="Cantidad de archivos a mostrar")
    ap.add_argument("--detail", default="", help="Detalle de un archivo puntual (substring)")
    ap.add_argument("--json", action="store_true", help="Salida JSON")
    args = ap.parse_args()

    files = sorted(SRC.rglob("*.py"))
    results: list[dict] = []
    for f in files:
        if "__pycache__" in str(f):
            continue
        results.extend(audit_file(f))

    for r in results:
        r["priority"] = _priority(r["file"], r["broad"])

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if args.detail:
        sel = [r for r in results if args.detail in r["file"]]
        print(f"🔍 {len(sel)} bloques except→DEBUG en archivos que contienen {args.detail!r}:\n")
        for r in sorted(sel, key=lambda x: x["line"]):
            print(f"  {r['file']}:{r['line']}  broad={r['broad']}  log={r['calls']}")
        return 0

    by_file: dict[str, int] = {}
    prio_of_file: dict[str, int] = {}
    for r in results:
        by_file[r["file"]] = by_file.get(r["file"], 0) + 1
        prev = prio_of_file.get(r["file"], 99)
        prio_of_file[r["file"]] = min(prev, r["priority"])

    print("🔎 Auditoría ISSUE-019 — bloques `except` que degradan a DEBUG (o no loguean)\n")
    print(f"Total: {len(results)} bloques en {len(by_file)} archivos\n")
    print(f"{'bloques':>8}  {'prioridad':>9}  archivo")
    print("-" * 72)
    ordered = sorted(by_file.items(), key=lambda kv: (prio_of_file[kv[0]], -kv[1]))
    for f, n in ordered[:args.top]:
        p = prio_of_file[f]
        etiqueta = "ALTA" if p == 0 else ("media" if p == 1 else "baja")
        print(f"{n:>8}  {etiqueta:>9}  {f}")

    hi = [r for r in results if r["priority"] == 0]
    print(f"\n🎯 Prioridad ALTA (pérdida silenciosa de datos/features): {len(hi)} bloques "
          f"en {len({r['file'] for r in hi})} archivos")
    for f in sorted({r["file"] for r in hi})[:args.top]:
        n = sum(1 for r in hi if r["file"] == f)
        print(f"     {n:>3}  {f}")
    print("\n   Migrar a:  from src.utils.degradation import log_degraded")
    print("              log_degraded(logger, exc, \"<contexto>\")")
    print("\n   ℹ️  Inventario AMPLIO: incluye handlers sin log y con debug informativo.")
    print("       El GATE automático es más estricto — solo marca lo que REPORTA un")
    print("       error por DEBUG en rutas de alto valor (detector")
    print("       `silent_contract_swallow`). Usá este listado para revisar por tandas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
