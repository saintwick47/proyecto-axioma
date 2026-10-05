#!/usr/bin/env python3
"""
tools/regression_eval.py — Evaluación continua / comparación entre versiones.

Genera un snapshot estructurado del proyecto (v0.6.9u) y lo compara con el
anterior (sidecar logs/axioma_eval_last.json) para detectar REGRESIONES:

  - git (commit) + fecha
  - estructura (archivos src/*.py, tests/test_*.py)
  - pytest (passed/skipped/failed)     [--no-pytest para saltar]
  - auditor AXIOMA (SCORE /100)        [--no-auditor para saltar]
  - métricas agénticas desde logs (health report v2: errors, validator,
    retry, sandbox/reparaciones, calidad) si existe logs/axioma_health_last.json

Salida: logs/axioma_eval_<ts>.md + sidecar logs/axioma_eval_last.json.

Uso:
  python tools/regression_eval.py              # completo (~2-3 min)
  python tools/regression_eval.py --no-pytest --no-auditor   # rápido
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SIDECAR = _ROOT / "logs" / "axioma_eval_last.json"

# (clave, etiqueta, subir-es-bueno)
_COMPARE = [
    ("src_py_files", "Archivos src/*.py", True),
    ("tests_files", "Archivos de test", True),
    ("pytest_passed", "Tests passed", True),
    ("pytest_skipped", "Tests skipped", False),
    ("pytest_failed", "Tests failed", False),
    ("auditor_score", "Auditor SCORE", True),
    ("logs_errors", "Errores en logs", False),
    ("logs_validator_fail_pct", "Validador FAIL %", False),
    ("logs_retry_pct", "Reintentos %", False),
    ("logs_repairs", "Reparaciones Fase4", True),
    ("logs_quality_score", "Calidad código", True),
]


def _src_py() -> int:
    return sum(1 for p in _ROOT.rglob("*.py")
               if "venv" not in p.parts and ".git" not in p.parts)


def _tests() -> int:
    return sum(1 for p in (_ROOT / "tests").glob("test_*.py"))


def _pytest() -> Dict[str, int]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=_ROOT, capture_output=True, text=True, timeout=300)
    tail = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)?", tail)
    if m:
        return {"pytest_passed": int(m.group(1)),
                "pytest_skipped": int(m.group(2) or 0),
                "pytest_failed": int(m.group(3) or 0)}
    return {"pytest_passed": 0, "pytest_skipped": 0, "pytest_failed": -1}


def _auditor() -> Optional[int]:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "tools.auditor.aud_main", "--mode", "full"],
            cwd=_ROOT, capture_output=True, text=True, timeout=240)
    except Exception as exc:  # noqa: BLE001 — timeout u otro: no romper el eval
        print(f"  [!] auditor omitido: {exc}", file=sys.stderr)
        return None
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"SCORE AXIOMA:\s*([\d.]+)\s*/\s*100", text)
    return int(float(m.group(1))) if m else None


def _logs_metrics() -> Dict[str, Any]:
    last = _ROOT / "logs" / "axioma_health_last.json"
    if not last.exists():
        return {}
    try:
        d = json.loads(last.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    mapping = {
        "errors_total": "logs_errors",
        "validator_fail_pct": "logs_validator_fail_pct",
        "retry_pct": "logs_retry_pct",
        "repairs": "logs_repairs",
        "quality_score": "logs_quality_score",
    }
    for src, dst in mapping.items():
        if src in d:
            out[dst] = d[src]
    return out


def _fmt_num(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.1f}"
    return str(x)


def _delta(prev: Dict[str, Any], cur: Dict[str, Any]) -> list:
    lines = []
    for key, label, up_good in _COMPARE:
        if key not in prev or key not in cur:
            continue
        a, b = prev[key], cur[key]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        delta = b - a
        eps = 1e-9
        if abs(delta) <= eps:
            arrow, icon = "→", "🟢"
        else:
            good = (delta > 0) == up_good
            arrow = "⬆" if delta > 0 else "⬇"
            icon = "🟢" if good else "🔴"
        lines.append(f"- {label}: {_fmt_num(b)} {arrow} "
                     f"(prev {_fmt_num(a)}) {icon}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Eval continua / regresión")
    ap.add_argument("--no-pytest", action="store_true")
    ap.add_argument("--no-auditor", action="store_true")
    args = ap.parse_args()

    git_rev = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
        capture_output=True, text=True).stdout.strip() or "?"
    cur: Dict[str, Any] = {
        "git_rev": git_rev,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "src_py_files": _src_py(),
        "tests_files": _tests(),
    }
    if not args.no_pytest:
        cur.update(_pytest())
    if not args.no_auditor:
        score = _auditor()
        if score is not None:
            cur["auditor_score"] = score
    cur.update(_logs_metrics())

    prev: Dict[str, Any] = {}
    if _SIDECAR.exists():
        try:
            prev = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    out = _ROOT / f"logs/axioma_eval_{datetime.now():%Y%m%d_%H%M%S}.md"
    lines = ["# 📊 AXIOMA — Eval continua (regresión)", "",
             f"- Commit: `{git_rev}` · {cur['date']}", ""]
    lines.append("## Snapshot")
    lines.append("")
    for key, label, _ in _COMPARE:
        if key in cur:
            lines.append(f"- {label}: {_fmt_num(cur[key])}")
    lines.append("")
    if prev:
        lines.append("## Delta vs anterior")
        lines.append("")
        lines.extend(_delta(prev, cur) or ["- sin cambios"])
        lines.append("")
    else:
        lines.append("_✓ primer snapshot — no hay anterior para comparar_")
    out.write_text("\n".join(lines), encoding="utf-8")
    # ✅ ISSUE-058: conservar solo los últimos reportes de eval (antes se
    # acumulaban uno por corrida).
    try:
        from tools.detailed_logger import prune_reports
        prune_reports("axioma_eval_*.md", keep=2, protect=out)
    except Exception:
        pass
    _SIDECAR.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[+] Reporte: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
