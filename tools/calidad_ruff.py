#!/usr/bin/env python3
"""Ratchet de calidad de ruff: la deuda sólo baja (plan de calidad v2.0, 2026-11-04).

Mide las reglas OBJETIVO sobre el árbol y compara con la sección `calidad` de
`audit_baseline.json` (el MISMO baseline del auditor: un solo lugar que versionar).

- Falla (exit 1) sólo si algún contador SUBE respecto de la base.
- No obliga a arreglar nada de golpe: hay ~6.500 infracciones medidas; esto impide que crezcan.
- Cuando una regla llega a cero, se agrega a `select` de `pyproject.toml` y queda bloqueada.

Uso:
  python tools/calidad_ruff.py              # compara (0 = sin regresión, 1 = empeoró, 2 = sin base)
  python tools/calidad_ruff.py --medir      # crea/reescribe la base (acción explícita)
  python tools/calidad_ruff.py --actualizar # baja la base si hubo mejoras; no escribe si empeoró
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BASELINE = RAIZ / "audit_baseline.json"
REGLAS_OBJETIVO = ["I", "UP", "C4", "E", "W", "SIM", "B", "F", "ASYNC", "TC", "RUF"]
IGNORAR = ["E501"]
RUTAS = ["src", "config", "tools", "tests", "main.py", "Rafael.py", "axioma_auditor.py"]


def medir() -> dict:
    """Corre ruff y devuelve el conteo por regla. Falla con mensaje claro si no está."""
    orden = [sys.executable, "-m", "ruff", "check", "--output-format", "json",
             "--select", ",".join(REGLAS_OBJETIVO), "--ignore", ",".join(IGNORAR), *RUTAS]
    try:
        proceso = subprocess.run(orden, cwd=RAIZ, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[X] no se pudo ejecutar ruff: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if proceso.returncode not in (0, 1):  # 1 = hay infracciones (normal)
        print(f"[X] ruff falló ({proceso.returncode}): {proceso.stderr[:300]}", file=sys.stderr)
        raise SystemExit(2)
    try:
        hallazgos = json.loads(proceso.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"[X] ruff no devolvió JSON válido: {exc}", file=sys.stderr)
        raise SystemExit(2)
    por_regla: dict = {}
    for h in hallazgos:
        codigo = str(h.get("code") or "?")
        por_regla[codigo] = por_regla.get(codigo, 0) + 1
    return {"total": len(hallazgos), "por_regla": dict(sorted(por_regla.items())),
            "reglas": REGLAS_OBJETIVO, "ignorar": IGNORAR}


def comparar(actual: dict, base: dict) -> dict:
    """Regresiones: reglas cuyo contador subió (o nuevas con más de 0)."""
    antes = (base or {}).get("por_regla") or {}
    ahora = actual.get("por_regla") or {}
    return {regla: (antes.get(regla, 0), n) for regla, n in ahora.items()
            if n > antes.get(regla, 0)}


def _leer_baseline() -> dict:
    if not BASELINE.is_file():
        return {}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[X] baseline ilegible ({BASELINE}): {exc}", file=sys.stderr)
        raise SystemExit(2)


def _escribir(baseline: dict, medicion: dict) -> None:
    baseline["calidad"] = {**medicion, "generado": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")}
    BASELINE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[+] base de calidad escrita en {BASELINE}: {medicion['total']} infracciones")


def main(argv: list) -> int:
    medicion = medir()
    baseline = _leer_baseline()
    print(f"[i] ruff: {medicion['total']} infracciones en {len(medicion['por_regla'])} reglas")
    if "--medir" in argv:
        _escribir(baseline, medicion)
        return 0
    if "calidad" not in baseline:
        print("[X] falta la sección 'calidad' en audit_baseline.json: corré --medir", file=sys.stderr)
        return 2
    regresiones = comparar(medicion, baseline["calidad"])
    if "--actualizar" in argv:
        if regresiones:
            print(f"[X] hay regresiones, no se actualiza: {regresiones}", file=sys.stderr)
            return 1
        _escribir(baseline, medicion)
        return 0
    if regresiones:
        for regla, (antes, ahora) in sorted(regresiones.items()):
            print(f"[X] {regla}: {antes} → {ahora}", file=sys.stderr)
        print(f"[X] {len(regresiones)} regla(s) empeoraron (ratchet de calidad)", file=sys.stderr)
        return 1
    print("[+] sin regresión de calidad (ruff)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
