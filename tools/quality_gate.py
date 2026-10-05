#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Quality Gate (P0 del informe, v0.6.9v)
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/quality_gate.py
# Propósito: bloquear regresiones de calidad con condiciones OBJETIVAS y
#            verificables, usando los artefactos que ya produce el proyecto
#            (no re-mide nada por su cuenta):
#
#   · SCORE AXIOMA >= 100           ← resumen JSON del auditor
#   · CRITICAL == 0 y HIGH == 0     ← idem
#   · detectores-selectos == 0      ← fragile_source_grep_tests,
#                                     silent_contract_swallow,
#                                     nondeterministic_hw_tests,
#                                     config_task_keys  (los 4 que la sesión
#                                     agregó: el gate los protege)
#   · suite completa PASS           ← reporte consolidado de run_all_tests.py
#                                     (usa `pytest_exitstatus` REAL, no la
#                                     palabra "PASS" del markdown)
#   · run_id presente               ← trazabilidad del reporte consolidado
#   · health report fresco          ← tools/log_health_report.py (`stale_hours`)
#                                     ⚠️ en CI no hay logs de runtime: se
#                                     saltea SOLO si no existe ningún log.
#
# Uso:
#   python tools/quality_gate.py --audit-json logs/audit_summary.json \
#                                --test-report logs/axioma_test_report_batch_*.md \
#                                --health-report logs/axioma_health_*.md
#   python tools/quality_gate.py --self-check      # corre todo y evalúa
#
# Salida: tabla legible + exit code 0 (PASA) / 1 (BLOQUEA). Con --json escribe
# además el resumen máquina-legible para CI.
# ═══════════════════════════════════════════════════════════════
import argparse
import glob
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Detectores que el proyecto considera "no negociables" (los agregó esta sesión
# para no repetir bugs reales). Si alguno pasa de 0, el gate bloquea.
GATED_DETECTORS = (
    "fragile_source_grep_tests",
    "silent_contract_swallow",
    "nondeterministic_hw_tests",
    "config_task_keys",
)

DEFAULT_SCORE_MIN = 100
DEFAULT_STALE_HOURS_MAX = 12.0
MIN_TESTS = 100   # una suite que recolecta menos que esto es sospechosa


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    skipped: bool = False
    blocking: bool = True

    @property
    def status(self) -> str:
        if self.skipped:
            return "⏭️  SKIP"
        return "✅ PASA" if self.passed else "❌ BLOQUEA"


@dataclass
class GateResult:
    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed or c.skipped or not c.blocking for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {"name": c.name, "status": c.status, "passed": c.passed,
                 "skipped": c.skipped, "blocking": c.blocking,
                 "detail": c.detail}
                for c in self.checks
            ],
        }


# ═══════════════════════════════════════════════════════════════
# Lectura de artefactos
# ═══════════════════════════════════════════════════════════════
def _newest(pattern: str) -> Optional[Path]:
    matches = sorted(glob.glob(str(PROJECT_ROOT / pattern)))
    return Path(matches[-1]) if matches else None


def load_audit_summary(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_test_report(path: Optional[Path]) -> Dict[str, Any]:
    """Extrae del reporte consolidado: veredicto, exitstatus real y run_id.

    Formatos reales que se parsean (ver `tests/axioma_report.py`):
      · `**Tanda (run-id):** \\`batch_YYYYmmdd_HHMMSS\\``
      · `- 🧪 pytest exitstatus: **0** (OK)`   (una por archivo)
      · `**Ejecución pytest:** exitstatus=0 — tests recolectados=46`
      · `- **Veredicto: ✅ TODO CORRECTO**`
    """
    out: Dict[str, Any] = {"path": str(path) if path else None}
    if path is None or not Path(path).exists():
        return out
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"run[_-]?id[^0-9A-Za-z]*`?([A-Za-z0-9_\-]+)", text)
    out["run_id"] = m.group(1) if m else None
    # `pytest exitstatus: **N**` aparece una vez por archivo → el peor manda
    statuses = [int(x) for x in
                re.findall(r"pytest exitstatus:\s*\*\*(\d+)\*\*", text)]
    out["exitstatuses"] = statuses
    out["worst_exitstatus"] = max(statuses) if statuses else None
    # `Ejecución pytest: exitstatus=N` = una por archivo ejecutado (más estable
    # que contar cabeceras de sección, que varían en el primer archivo)
    out["archivos_total"] = len(re.findall(r"Ejecución pytest:\*\* exitstatus=\d+", text))
    out["archivos_ok"] = len(re.findall(r"Veredicto: ✅ TODO CORRECTO", text))
    recol = [int(x) for x in re.findall(r"tests recolectados=(\d+)", text)]
    out["tests_collected"] = sum(recol)
    out["resumen_ok"] = (out["archivos_total"] > 0
                         and out["archivos_ok"] == out["archivos_total"])
    # ✅ v0.7.4: NOMBRAR los archivos que no están OK y qué check falló en cada uno.
    # Antes el gate solo decía "17/18 archivos" y había que adivinar cuál fallaba
    # (pasó en CI: 17/18 con exitstatus 0 — un `check()` fallido sin `assert`).
    # En una tanda compartida cada archivo escribe su bloque precedido por
    # `> Corrida: \`<archivo>\`` (lo pone `run_all_tests.py` vía AXIOMA_TEST_RUN_LABEL).
    out["archivos_con_fallos"] = _archivos_con_fallos(text)
    return out


def _archivos_con_fallos(text: str) -> Dict[str, List[str]]:
    r"""{archivo: [checks en ❌]} a partir del reporte consolidado.

    ✅ v0.7.4 (`ISSUE-095`) — BUG REAL corregido: la primera versión partía el
    reporte por `> Corrida: \`<archivo>\``, pero ese marcador lo escribe
    `axioma_report.finalize()` al **CERRAR** el bloque de totales de un archivo.
    Resultado: las filas ❌ que quedaban en el segmento eran las del archivo
    **siguiente**, así que el gate nombraba al archivo anterior (o, cuando el
    siguiente estaba OK, imprimía `(sin check ❌: revisar el reporte)` — medido en
    la CI del 2026-09-28).

    Estructura real del archivo (verificado sobre `logs/axioma_test_report_batch_*.md`):

        [filas del 1er archivo]          ← sin cabecera propia
        > Corrida: `uno.py`              ← totales de uno.py
        ### Resultado de esta corrida
        # ▶ Corrida: `dos.py`            ← cabecera de la tabla de dos.py
        [filas de dos.py]
        > Corrida: `dos.py`
        ### Resultado de esta corrida

    O sea: el segmento anterior a cada marcador contiene los totales del archivo
    ANTERIOR y, después de su cabecera `# ▶ Corrida:`, las filas del archivo que
    el marcador nombra. Se resuelve con las dos marcas.
    """
    fallos: Dict[str, List[str]] = {}
    bloques = re.split(r"^> Corrida: `([^`]+)`\s*$", text, flags=re.M)
    # bloques = [filas_1, nombre_1, (totales_1 + filas_2), nombre_2, ...]
    nombres = bloques[1::2]
    cuerpos = bloques[2::2]
    if not nombres:
        return fallos
    filas_por_archivo: Dict[str, str] = {nombres[0]: bloques[0]}
    for i, nombre in enumerate(nombres):
        cuerpo = cuerpos[i] if i < len(cuerpos) else ""
        partes = re.split(r"^# ▶ Corrida: `([^`]+)`\s*$", cuerpo, flags=re.M)
        totales = partes[0]                     # totales del archivo `nombre`
        if len(partes) >= 3:
            filas_por_archivo[partes[1]] = partes[2]   # tabla del archivo siguiente
        if "Veredicto: ✅ TODO CORRECTO" in totales:
            continue
        filas = filas_por_archivo.get(nombre, "")
        malos = re.findall(r"^\| ([^|]+?) \| ❌ FAIL \| ([^|]*)\|", filas, flags=re.M)
        fallos[nombre] = [
            (f"{n.strip()} → {d.strip()[:80]}" if d.strip() else n.strip())
            for n, d in malos
        ] or ["(sin check ❌: revisar el reporte)"]
    return fallos


def _resumen_fallos(fallos: Dict[str, List[str]]) -> str:
    """Texto corto: `archivo.py (3 checks)`, `otro.py (1)`…"""
    return ", ".join(f"{k} ({len(v)})" for k, v in fallos.items()) or "?"


def volcar_fallos(tests: Dict[str, Any]) -> None:
    """Imprime los checks ❌ por archivo (para que la CI diga QUÉ falló)."""
    fallos = tests.get("archivos_con_fallos") or {}
    if not fallos:
        return
    print("\n" + "=" * 72)
    print("ARCHIVOS CON CHECKS FALLIDOS (el gate los cuenta por archivo)")
    print("=" * 72)
    for archivo, checks in fallos.items():
        print(f"  ❌ {archivo}")
        for c in checks:
            print(f"       - {c}")
    print("=" * 72 + "\n")


def parse_health_report(path: Optional[Path]) -> Dict[str, Any]:
    """Lee `logs/axioma_health_*.md` (vigencia + anomalías)."""
    out: Dict[str, Any] = {"path": str(path) if path else None}
    if path is None or not Path(path).exists():
        return out
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"hace\s+([0-9.]+)\s*h", text)
    if m:
        out["stale_hours"] = float(m.group(1))
    out["anomalias"] = "sin anomalías" not in text.lower()
    out["vigente"] = "✅ reciente" in text
    return out


# ═══════════════════════════════════════════════════════════════
# Evaluación
# ═══════════════════════════════════════════════════════════════
def evaluate(audit: Dict[str, Any], tests: Dict[str, Any],
             health: Dict[str, Any], score_min: int = DEFAULT_SCORE_MIN,
             stale_max: float = DEFAULT_STALE_HOURS_MAX,
             require_health: bool = True) -> GateResult:
    """Evalúa las condiciones. Función PURA (testeable sin correr nada)."""
    res = GateResult()
    summary = (audit or {}).get("summary") or {}

    # 1) Auditoría: SCORE
    if not summary:
        res.checks.append(Check("SCORE AXIOMA", False,
                                "sin resumen de auditoría (¿corriste el auditor?)"))
    else:
        score = summary.get("score")
        res.checks.append(Check(
            f"SCORE >= {score_min}", isinstance(score, (int, float)) and score >= score_min,
            f"SCORE={score}"))

        # 2) Severidades altas
        crit, high = summary.get("critical", 0), summary.get("high", 0)
        res.checks.append(Check("CRITICAL == 0", crit == 0, f"CRITICAL={crit}"))
        res.checks.append(Check("HIGH == 0", high == 0, f"HIGH={high}"))

        # 3) Detectores protegidos
        counts = {d.get("name"): d.get("findings_count", 0)
                  for d in (audit.get("detectors") or [])}
        for det in GATED_DETECTORS:
            n = counts.get(det)
            if n is None:
                res.checks.append(Check(f"detector {det}", False,
                                        "detector ausente del reporte"))
            else:
                res.checks.append(Check(f"detector {det} == 0", n == 0,
                                        f"{det}={n}"))

    # 4) Suite de tests: veredicto + exitstatus REAL
    if not tests.get("path"):
        res.checks.append(Check("suite completa PASS", False,
                                "sin reporte consolidado de tests"))
    else:
        worst = tests.get("worst_exitstatus")
        res.checks.append(Check(
            "pytest exitstatus real == 0", worst == 0,
            f"peor exitstatus={worst} de {len(tests.get('exitstatuses') or [])} archivos"))
        res.checks.append(Check(
            "todos los archivos OK", bool(tests.get("resumen_ok")),
            (f"{tests.get('archivos_ok')}/{tests.get('archivos_total')} archivos"
             + (f" · fallan: {_resumen_fallos(tests.get('archivos_con_fallos') or {})}"
                if not tests.get("resumen_ok") else ""))))
        n = tests.get("tests_collected") or 0
        res.checks.append(Check(
            f"tests recolectados >= {MIN_TESTS}", n >= MIN_TESTS, f"{n} tests"))
        res.checks.append(Check(
            "run_id presente (trazabilidad)", bool(tests.get("run_id")),
            f"run_id={tests.get('run_id')}"))

    # 5) Health report fresco
    if not health.get("path"):
        res.checks.append(Check(
            "health report fresco", not require_health,
            "sin health report (CI no genera logs de runtime)",
            skipped=not require_health, blocking=require_health))
    else:
        stale = health.get("stale_hours")
        res.checks.append(Check(
            f"health fresco (<= {stale_max} h)",
            isinstance(stale, (int, float)) and stale <= stale_max,
            f"stale_hours={stale}"))
        res.checks.append(Check(
            "health sin anomalías", not health.get("anomalias"),
            "anomalías detectadas" if health.get("anomalias") else "sin anomalías"))
    return res


# ═══════════════════════════════════════════════════════════════
# Modo --self-check: corre los 3 artefactos y después evalúa
# ═══════════════════════════════════════════════════════════════
def _run(cmd: List[str], timeout: int = 900) -> int:
    print(f"  $ {' '.join(cmd)}", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    print(f"    → exit={proc.returncode} ({time.time() - t0:.1f}s)", flush=True)
    return proc.returncode


def self_check(timeout: int = 900) -> int:
    """Genera los artefactos y devuelve la ruta de cada uno."""
    py = sys.executable
    audit_json = PROJECT_ROOT / "logs" / "audit_summary.json"
    _run([py, "axioma_auditor.py", "--format", "json", "-o", str(audit_json)])
    _run([py, "tools/run_all_tests.py", "--timeout", "150"], timeout=timeout)
    _run([py, "tools/log_health_report.py"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AXIOMA quality gate")
    ap.add_argument("--audit-json", default=None,
                    help="JSON con --format json del auditor")
    ap.add_argument("--test-report", default=None,
                    help="Reporte consolidado de tools/run_all_tests.py")
    ap.add_argument("--health-report", default=None,
                    help="Reporte de tools/log_health_report.py")
    ap.add_argument("--score-min", type=int, default=DEFAULT_SCORE_MIN)
    ap.add_argument("--stale-hours-max", type=float, default=DEFAULT_STALE_HOURS_MAX)
    ap.add_argument("--no-require-health", action="store_true",
                    help="No exigir health report (modo CI sin logs)")
    ap.add_argument("--self-check", action="store_true",
                    help="Correr auditor + tests + health antes de evaluar")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Escribir el resultado máquina-legible en esta ruta")
    args = ap.parse_args()

    if args.self_check:
        self_check()

    audit_path = Path(args.audit_json) if args.audit_json else \
        (PROJECT_ROOT / "logs" / "audit_summary.json")
    test_path = Path(args.test_report) if args.test_report else \
        _newest("logs/axioma_test_report_batch_*.md")
    health_path = Path(args.health_report) if args.health_report else \
        _newest("logs/axioma_health_*.md")

    audit = load_audit_summary(audit_path)
    tests = parse_test_report(test_path)
    # ✅ v0.7.4: si algún archivo tiene checks fallidos, decir CUÁL y QUÉ (antes el
    # gate solo mostraba "17/18 archivos" y había que adivinar).
    volcar_fallos(tests)
    health = parse_health_report(health_path)

    res = evaluate(audit, tests, health, score_min=args.score_min,
                   stale_max=args.stale_hours_max,
                   require_health=not args.no_require_health)

    print("=" * 72)
    print("AXIOMA — QUALITY GATE")
    print("=" * 72)
    for c in res.checks:
        print(f"  {c.status:12s} {c.name:34s} {c.detail}")
    print("-" * 72)
    print(f"  VEREDICTO: {'✅ PASA' if res.ok else '❌ BLOQUEA'}")
    print("=" * 72)

    if args.json_out:
        # ✅ ISSUE-149: en el CI la carpeta `logs/` puede no existir (si un paso anterior falló),
        # y el portero moría con FileNotFoundError en vez de decir su veredicto.
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(res.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
