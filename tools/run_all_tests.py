#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — tools/run_all_tests.py
# Ruta: /ruta/a/axioma/tools/run_all_tests.py
#
# ✅ v0.6.9v (ISSUE-005): corre TODOS los tests/test_axioma_*.py en una
#    tanda con RUN-ID COMPARTIDO, de modo que se genere UN SOLO reporte
#    consolidado en logs/axioma_test_report_<run-id>.md con los resultados
#    de todos los archivos (antes cada proceso pisaba al anterior y el
#    reporte final solo mostraba el último archivo).
#
#    Se corre archivo por archivo con timeout individual: así un test que
#    cuelga (p.ej. uno que requiere LLM/Ollama real) no bloquea la tanda y
#    se reporta como TIMEOUT en lugar de perder todo.
#
# Uso:
#   python tools/run_all_tests.py                 # timeout 130s por archivo
#   python tools/run_all_tests.py --timeout 300   # timeout custom
#   python tools/run_all_tests.py --only test_axioma_06
#   python tools/run_all_tests.py -k "sessions"   # pasa -k a pytest
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TESTS_DIR = _ROOT / "tests"
LOGS_DIR = _ROOT / "logs"


def _status_de(rc: int, salida: str) -> str:
    """Clasifica una corrida de pytest a partir del exit code y la salida.

    ✅ v0.7.3 (2026-09-28) — BUG REAL corregido: antes la rama `PASS*` era
    `elif "passed" in out` y eso incluye CUALQUIER salida con fallos, porque
    pytest resume "7 failed, 60 passed in 3.79s". Un archivo con 7 tests
    fallidos quedaba como `PASS*` → `ok` en el resumen ("18/18 archivos OK")
    mientras 7 tests fallaban. Ahora `PASS*` exige **cero** fallos/errores: se
    reserva para exit != 0 por ruido de colección/warnings.
    """
    if rc == 0:
        return "PASS"
    if rc == 124:
        return "TIMEOUT"
    if rc == 5:
        return "NO-TESTS"
    if "check(s) FALLIDOS" in salida:
        # ✅ ISSUE-092: el conftest fuerza exitstatus=1 cuando hubo checks fallidos
        # aunque pytest diga "N passed" (muchos tests usan `check()` sin `assert`).
        return "FAIL"
    if "passed" in salida and "failed" not in salida and "error" not in salida:
        return "PASS*"
    return "FAIL"


def _pytest_cmd(python: str, test_file: Path, extra: list[str]) -> list[str]:
    return [python, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider", *extra]


def main() -> int:
    ap = argparse.ArgumentParser(description="Corre la suite completa en una tanda con reporte consolidado.")
    ap.add_argument("--timeout", type=int, default=130, help="Timeout por archivo en segundos (default 130)")
    ap.add_argument("--only", default="", help="Substring para filtrar archivos de test (ej: test_axioma_06)")
    ap.add_argument("-k", dest="kexpr", default="", help="Expresión -k de pytest")
    ap.add_argument("--run-id", default="", help="Run-id explícito (default: timestamp)")
    args = ap.parse_args()

    python = str(_ROOT / "venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable

    run_id = args.run_id.strip() or datetime.now(timezone.utc).strftime("batch_%Y%m%d_%H%M%S")
    files = sorted(TESTS_DIR.glob("test_axioma_*.py"))
    if args.only:
        files = [f for f in files if args.only in f.name]
    if not files:
        print("⚠️  No hay archivos de test que coincidan.")
        return 1

    extra = ["-k", args.kexpr] if args.kexpr else []

    env = os.environ.copy()
    env["AXIOMA_TEST_RUN_ID"] = run_id

    print(f"🧪 Tanda: {run_id} — {len(files)} archivos (timeout {args.timeout}s c/u)")
    print(f"   Reporte consolidado: logs/axioma_test_report_{run_id}.md\n")

    import shutil  # noqa: F401  (reservado: copia de reportes si se necesita)
    results: list[tuple[str, str, float]] = []
    t0 = time.time()

    for f in files:
        run_env = env.copy()
        run_env["AXIOMA_TEST_RUN_LABEL"] = f.name
        start = time.time()
        try:
            proc = subprocess.run(
                _pytest_cmd(python, f, extra),
                cwd=str(_ROOT), env=run_env,
                capture_output=True, text=True, timeout=args.timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            out, rc = "", 124
        dur = time.time() - start

        if rc == 0:
            status = "PASS"
        else:
            status = _status_de(rc, out)

        results.append((f.name, status, dur))
        # Mostrar el resumen de pytest de esa corrida (última línea con "passed")
        summary_line = ""
        for line in reversed(out.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary_line = line.strip()
                break
        print(f"  {status:9s} {f.name:52s} {dur:6.1f}s  {summary_line[:60]}")

    total_dur = time.time() - t0
    ok = sum(1 for _, s, _ in results if s.startswith("PASS"))
    bad = [r for r in results if not r[1].startswith("PASS")]
    report = LOGS_DIR / f"axioma_test_report_{run_id}.md"

    print(f"\n{'=' * 72}")
    print(f"RESUMEN: {ok}/{len(results)} archivos OK · {len(bad)} con problemas · {total_dur:.1f}s")
    if bad:
        for name, status, _ in bad:
            print(f"  - {status}: {name}")
    print(f"📄 Reporte consolidado: {report}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
