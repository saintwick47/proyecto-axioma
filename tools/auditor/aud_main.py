#!/usr/bin/env python3
"""
tools/auditor/auditor.py — registro de detectores, AxiomaAuditor y CLI main()

v0.6.8uu: extraído de axioma_auditor.py (monolítico v8.3, 3112 líneas) para
facilitar el mantenimiento. Este paquete divide el auditor en 5 módulos
(≤900 líneas c/u); axioma_auditor.py en la raíz quedó como shim de
compatibilidad (CLI y exports idénticos).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)
from tools.auditor.aud_core import (
    BaseDetector, CodeIndex, DetectorResult, AudFinding, AudSeverity, AudVerdict,
)
from tools.auditor.aud_detectors_infra import (
    CascadeWithoutProviderCBDetector, ConnectionPerMethodDetector,
    CosmeticCircuitBreakDetector, DeadInvalidationDetector,
    FireAndForgetThreadDetector, FixedTimeoutDetector,
    LruWithoutMoveToEndDetector, MissingGlobalTimeoutDetector,
    NameCollisionDetector, RiskyDeadCodeDetector, StaticPriorityDetector,
    UnboundedCacheDetector,
)
from tools.auditor.aud_detectors_salud import (
    EnvModelsDetector, FifoCacheDetector, MissingCompoundIndexDetector,
    SilentExceptionsDetector, SyncBlockingInAsyncDetector,
    SyntaxImportDetector,
)
from tools.auditor.aud_detectors_arquitectura import (
    AsyncModuleLevelDetector, CircularImportDetector, ConfigRefsDetector,
    DuplicateCodeDetector, IntegrationDetector, PlanV2ProtectionsDetector,
    SecurityHarnessDetector, SingletonViolationDetector,
)
# ✅ v0.6.9v: detectores de las clases de bug halladas en la sesión de
# refactorización + observabilidad (ver docs/ISSUES_HISTORY.md).
from tools.auditor.aud_detectors_regresiones import (
    ConfigTaskKeysDetector,
    FragileSourceGrepTestDetector,
    NativeImportOSErrorDetector,
    NondeterministicHardwareTestDetector,
    PathContainmentDetector,
    SilentContractSwallowDetector,
    UnsafeExecDetector,
    UnsafePathDeleteDetector,
)


# ============================================================================
# REGISTRO DE DETECTORES
# ============================================================================

DETECTORS: Dict[str, type[BaseDetector]] = {
    'lru': LruWithoutMoveToEndDetector,
    'circuit_break': CosmeticCircuitBreakDetector,
    'unbounded_cache': UnboundedCacheDetector,
    'fire_forget': FireAndForgetThreadDetector,
    'dead_code': RiskyDeadCodeDetector,
    'cascade': CascadeWithoutProviderCBDetector,
    'fixed_timeout': FixedTimeoutDetector,
    'dead_invalidation': DeadInvalidationDetector,
    'static_priority': StaticPriorityDetector,
    'connection_per_method': ConnectionPerMethodDetector,
    'global_timeout': MissingGlobalTimeoutDetector,
    'name_collision': NameCollisionDetector,
    'sync_blocking': SyncBlockingInAsyncDetector,
    'compound_index': MissingCompoundIndexDetector,
    'fifo_cache': FifoCacheDetector,
    # ✅ v8.0 (2026-08-25): auditoría completa
    'syntax_import': SyntaxImportDetector,
    'silent_exceptions': SilentExceptionsDetector,
    'env_models': EnvModelsDetector,
    # ✅ v8.1 (2026-08-26): protecciones FASE 1-3 plan_harness
    'security_harness': SecurityHarnessDetector,
    # ✅ v8.2 (2026-09-01): protecciones PLAN v2.0 (Fases 2-6)
    'plan_v2_protections': PlanV2ProtectionsDetector,
    # ✅ v8.1 (2026-08-27): fusión de tools/verify_all.py (eliminado)
    'config_refs': ConfigRefsDetector,
    # ✅ v8.3 (2026-09-01): fusión de axioma_inspector.py (eliminado)
    'singleton_violation': SingletonViolationDetector,
    'async_module_level': AsyncModuleLevelDetector,
    'circular_import': CircularImportDetector,
    'duplicate_code': DuplicateCodeDetector,
    'integration': IntegrationDetector,
    # ✅ v0.6.9v: clases de bug de la sesión de refactor/observabilidad
    # (ISSUE-018/019 contrato tragado en DEBUG, ISSUE-020 tests que grepean
    # el fuente, ISSUE-022 tests dependientes de la RAM del equipo).
    'silent_contract_swallow': SilentContractSwallowDetector,
    'fragile_source_grep_tests': FragileSourceGrepTestDetector,
    'nondeterministic_hw_tests': NondeterministicHardwareTestDetector,
    # ✅ v0.6.9v (ISSUE-028): config de tareas escrita pero no leída por el loader
    # (o con nombres que no matchean ningún task_type real).
    'config_task_keys': ConfigTaskKeysDetector,
    # ✅ v0.7.5 (A2): clases de bug REALES de seguridad que el auditor no veía.
    'path_containment': PathContainmentDetector,
    'unsafe_path_delete': UnsafePathDeleteDetector,
    'native_import_oserror': NativeImportOSErrorDetector,
    'unsafe_exec': UnsafeExecDetector,
}


# ============================================================================
# HONESTIDAD DEL SCORE — deuda, baseline y ratchet (A1)
# ============================================================================
# ✅ v0.7.5 (A1): el SCORE /100 sólo resta CRITICAL/HIGH, así que un árbol con
# bugs graves de seguridad podía sacar 100 si el detector no los veía (medido:
# snapshot c4cd41f^ → SCORE 100 con path traversal + rmtree fuera del base +
# 49 conexiones sin cerrar). Se mantiene el SCORE como métrica de GATE y se
# agrega, al lado, un **índice de deuda** explícito (MEDIUM/LOW/INFO por
# detector) + baseline versionado + modo ratchet. SCORE 100 ≠ calidad.
# ✅ A9: una sola fuente de versión. Se LEE `src/__init__.py` en vez de
# importarlo: un `import src` en tiempo de import del auditor dejaba
# `sys.modules["src"]` apuntando al repo y, al auditar OTRO árbol con --root,
# `syntax_import` mezclaba el `src` del repo con el `config` del árbol auditado
# (medido: 3 CRITICAL falsos en el snapshot). Antes: CLI v8.3, paquete v0.6.8uu,
# proyecto 0.7.5.
def _project_version() -> str:
    src_init = Path(__file__).resolve().parent.parent.parent / "src" / "__init__.py"
    try:
        texto = src_init.read_text(encoding="utf-8")
    except OSError:
        return "0.7.5"
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', texto, re.M)
    return m.group(1) if m else "0.7.5"


AUDITOR_VERSION = _project_version()
AUDIT_BASELINE_SCHEMA = 1
AUDIT_BASELINE_FILENAME = "audit_baseline.json"
AUDIT_HISTORY_FILENAME = "audit_history.jsonl"

# Pesos del índice de deuda: MEDIUM pesa 5×LOW; INFO es cobertura, no deuda.
DEBT_WEIGHTS: Dict[str, int] = {"medium": 5, "low": 1}

SCORE_MEANING = (
    "SCORE /100 mide SÓLO CRITICAL/HIGH (y las categorías históricas del "
    "score_breakdown). MEDIUM y LOW NO restan: un SCORE 100 NO significa "
    "'sin deuda' ni 'sin bugs'. Mirar `debt` (índice de deuda por detector) "
    "y `regression` (ratchet contra audit_baseline.json)."
)


def _confirmed(findings: List[AudFinding]) -> List[AudFinding]:
    return [f for f in findings
            if f.verdict in (AudVerdict.CONFIRMED, AudVerdict.NEW)]


def _severity_label(finding: AudFinding) -> str:
    return str(getattr(finding.severity, "value", finding.severity)).lower()


def severity_counts(findings: List[AudFinding]) -> Dict[str, int]:
    """Conteo por severidad de los hallazgos CONFIRMED/NEW (incluye INFO)."""
    out: Dict[str, int] = {"total": 0, "critical": 0, "high": 0,
                           "medium": 0, "low": 0, "info": 0}
    for f in _confirmed(findings):
        sev = _severity_label(f)
        out["total"] += 1
        out[sev if sev in out else "info"] += 1
    return out


def debt_summary(findings: List[AudFinding]) -> Dict[str, Any]:
    """Índice de deuda explícito (A1): MEDIUM/LOW/INFO por detector.

    `index` = MEDIUM×5 + LOW×1. No se capa a 100 a propósito: es un conteo
    ponderado comparable contra el baseline, no una nota.
    """
    by_det: Dict[str, Dict[str, int]] = {}
    for f in _confirmed(findings):
        sev = _severity_label(f)
        if sev not in ("medium", "low", "info"):
            continue
        row = by_det.setdefault(f.detector,
                                {"medium": 0, "low": 0, "info": 0, "total": 0})
        row[sev] += 1
        row["total"] += 1
    medium = sum(r["medium"] for r in by_det.values())
    low = sum(r["low"] for r in by_det.values())
    info = sum(r["info"] for r in by_det.values())
    index = medium * DEBT_WEIGHTS["medium"] + low * DEBT_WEIGHTS["low"]
    if index == 0:
        rating = "sin deuda registrada"
    elif index <= 50:
        rating = "baja"
    elif index <= 200:
        rating = "media"
    else:
        rating = "alta"
    return {
        "index": index,
        "rating": rating,
        "weights": dict(DEBT_WEIGHTS),
        "medium": medium,
        "low": low,
        "info": info,
        "by_detector": dict(sorted(by_det.items(),
                                   key=lambda kv: (-kv[1]["total"], kv[0]))),
        "note": ("SCORE 100 ≠ calidad: este índice cuenta lo que el SCORE NO "
                 "resta (MEDIUM/LOW). Comparar contra audit_baseline.json."),
    }


def _git_commit(root: Path) -> Optional[str]:
    """Commit corto del árbol auditado (`None` si no es un repo git)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def load_baseline(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Lee `audit_baseline.json`; `None` si no existe o está corrupto."""
    if path is None or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def compare_to_baseline(current_debt: Dict[str, Any],
                        current_counts: Dict[str, int],
                        baseline: Optional[Dict[str, Any]],
                        baseline_path: Optional[Path] = None) -> Dict[str, Any]:
    """Ratchet (A1): regresiones = MÁS MEDIUM/LOW por detector que el baseline.

    También bloquea si suben CRITICAL/HIGH (el SCORE ya lo cubre, pero el
    reporte debe decir QUÉ empeoró). No penaliza mejoras ni cambios de INFO.
    """
    if not baseline:
        return {
            "baseline_present": False,
            "baseline_path": str(baseline_path) if baseline_path else None,
            "ok": True,
            "blocking": False,
            "regressions": [],
            "note": ("sin baseline versionado: no se puede detectar regresión "
                     "de deuda. Generarlo con --write-baseline."),
        }
    regressions: List[Dict[str, Any]] = []
    base_counts = baseline.get("counts") or {}
    for sev in ("critical", "high"):
        before = int(base_counts.get(sev, 0) or 0)
        now = int(current_counts.get(sev, 0) or 0)
        if now > before:
            regressions.append({
                "detector": "(severidad)", "severity": sev.upper(),
                "baseline": before, "current": now, "delta": now - before,
            })
    base_by = baseline.get("by_detector") or {}
    for det, row in (current_debt.get("by_detector") or {}).items():
        before_row = base_by.get(det) or {}
        for sev in ("medium", "low"):
            before = int(before_row.get(sev, 0) or 0)
            now = int(row.get(sev, 0) or 0)
            if now > before:
                regressions.append({
                    "detector": det, "severity": sev.upper(),
                    "baseline": before, "current": now, "delta": now - before,
                })
    return {
        "baseline_present": True,
        "baseline_path": str(baseline_path) if baseline_path else None,
        "baseline_generated_at": baseline.get("generated_at"),
        "baseline_git_commit": baseline.get("git_commit"),
        "baseline_score": baseline.get("score"),
        "baseline_debt_index": baseline.get("debt_index"),
        "current_debt_index": current_debt.get("index"),
        "regressions": regressions,
        "ok": not regressions,
        "blocking": bool(regressions),
        "note": ("ratchet: bloquea si hay más MEDIUM/LOW que el baseline "
                 "(por detector) o más CRITICAL/HIGH."),
    }


def append_history(root: Path, record: Dict[str, Any]) -> Path:
    """Append a `logs/audit_history.jsonl` (A7: tendencia, sin borrar todo)."""
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / AUDIT_HISTORY_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


# ============================================================================
# AUDITOR PRINCIPAL
# ============================================================================

class AxiomaAuditor:
    """Auditor principal para AXIOMA."""

    def __init__(self, root: Path, verbose: bool = False):
        self.root = root
        self.verbose = verbose
        self.index = CodeIndex(root)
        self.findings: List[AudFinding] = []
        self.detector_results: List[DetectorResult] = []
        self.test_results: Optional[Dict[str, Any]] = None  # ✅ v8.3 (--run-tests)
        # ✅ v0.7.5 (A1): baseline opcional para el ratchet de deuda.
        self.baseline: Optional[Dict[str, Any]] = None
        self.baseline_path: Optional[Path] = None

    def build_index(self, include_tools_tests: bool = False) -> None:
        if self.verbose:
            print(f"[*] Indexando {self.root}...", file=sys.stderr)
        self.index.build(include_tools_tests=include_tools_tests)
        if self.verbose:
            print(f"    → {len(self.index.files)} archivos Python indexados",
                  file=sys.stderr)
            print(f"    → {sum(len(v) for v in self.index.classes.values())} clases",
                  file=sys.stderr)
            print(f"    → {sum(len(v) for v in self.index.functions.values())} funciones",
                  file=sys.stderr)

    def run_detectors(self,
                      detector_names: Optional[List[str]] = None) -> None:
        names = detector_names or list(DETECTORS.keys())
        for name in names:
            cls = DETECTORS.get(name)
            if not cls:
                print(f"[!] Detector desconocido: {name}", file=sys.stderr)
                continue
            if self.verbose:
                print(f"[*] Ejecutando detector: {name}", file=sys.stderr)
            detector = cls(self.index)
            result = detector.run()
            self.detector_results.append(result)
            self.findings.extend(result.findings)
            if self.verbose:
                print(f"    → {len(result.findings)} hallazgos "
                      f"({result.duration_ms:.1f}ms)", file=sys.stderr)

    def run_tests(self) -> Dict[str, Any]:
        """Ejecuta la suite pytest (test_axioma_*.py) y resume (--run-tests).

        ✅ v8.3 (2026-09-01): fusionado de axioma_inspector.py (sección 7).
        """
        result: Dict[str, Any] = {
            "available": False, "total": 0, "passed": 0, "failed": 0,
            "errors": [], "duration_ms": 0,
        }
        import subprocess as _sp
        suite_files = sorted((self.root / "tests").glob("test_axioma_*.py"))
        if not suite_files:
            result["errors"].append("Suite integral no encontrada (tests/test_axioma_*.py)")
            return result
        result["available"] = True
        t0 = time.perf_counter()
        try:
            proc = _sp.run(
                [sys.executable, "-m", "pytest", *[str(f) for f in suite_files],
                 "-q", "--tb=no", "-p", "no:cacheprovider"],
                capture_output=True, text=True, timeout=300, cwd=str(self.root),
            )
            result["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            out = proc.stdout + proc.stderr
            m_pass = re.search(r"(\d+) passed", out)
            m_fail = re.search(r"(\d+) failed", out)
            m_err = re.search(r"(\d+) error", out)
            passed = int(m_pass.group(1)) if m_pass else 0
            failed = int(m_fail.group(1)) if m_fail else 0
            errors = int(m_err.group(1)) if m_err else 0
            if passed + failed + errors > 0:
                result["total"] = passed + failed + errors
                result["passed"] = passed
                result["failed"] = failed
                if errors:
                    result["errors"].append(f"{errors} errores de pytest")
            elif proc.returncode != 0:
                err = proc.stderr.strip()[:400] or proc.stdout.strip()[:200]
                result["errors"].append(
                    f"returncode={proc.returncode}" + (f" — {err}" if err else ""))
        except subprocess.TimeoutExpired:
            result["errors"].append("Timeout al ejecutar tests (>300s)")
        except Exception as e:
            result["errors"].append(str(e))
        return result

    def score_breakdown(self) -> List[Tuple[str, int, int]]:
        """Desglose del SCORE: lista de (categoría, hallazgos, puntos restados).

        ✅ v0.6.9v (ISSUE-026): fuente ÚNICA de verdad de la fórmula — la usan
        `compute_score()` y el reporte, así el número siempre es auditable
        (antes el informe solo mostraba "SCORE: 90/100" sin explicar de dónde
        salía el descuento).
        """
        confirmed = [f for f in self.findings
                     if f.verdict in (AudVerdict.CONFIRMED, AudVerdict.NEW)]
        rows: List[Tuple[str, int, int]] = []

        def _count(pred) -> int:
            return sum(1 for f in confirmed if pred(f))

        syntax = _count(lambda f: f.detector == 'syntax_import'
                        and f.severity in (AudSeverity.CRITICAL, AudSeverity.HIGH))
        rows.append(("syntax_import (CRITICAL/HIGH) ×15", syntax, syntax * 15))

        async_v = _count(lambda f: f.detector in ('async_module_level', 'sync_blocking'))
        rows.append(("async/sync bloqueante ×5", async_v, async_v * 5))

        singleton = _count(lambda f: f.detector == 'singleton_violation')
        rows.append(("singleton_violation ×3", singleton, singleton * 3))

        dups = _count(lambda f: f.detector == 'duplicate_code')
        rows.append(("duplicate_code ×2", dups, dups * 2))

        circular = _count(lambda f: f.detector == 'circular_import')
        rows.append(("circular_import ×8", circular, circular * 8))

        isolated = _count(lambda f: f.detector == 'integration' and 'isolated' in f.tags)
        rows.append(("integration [isolated] //5", isolated, isolated // 5))

        residual = _count(lambda f: f.detector == 'integration' and 'refactor' in f.tags)
        rows.append(("integration [refactor] //10", residual, residual // 10))

        # ✅ v0.6.9v (ISSUE-026): WIRING de los detectores de la sesión de
        # observabilidad/refactor (ISSUE-018…025). Se penaliza por CATEGORÍA y
        # SUBLINEALMENTE — mismo criterio que `isolated`/`residual` — porque son
        # deuda de OBSERVABILIDAD/FIABILIDAD, no fallos funcionales: castigar
        # por hallazgo colapsaría el SCORE por volumen (un solo detector puede
        # reportar decenas de bloques legítimos). Cada regla lleva TOPE para que
        # la métrica no caiga a 0 por acumulación, y el `// N` marca el ritmo al
        # que conviene arreglar por tandas.
        # Esos detectores emiten MEDIUM/LOW, así que NO se solapan con la regla
        # `other_high` de abajo (que solo mira CRITICAL/HIGH).
        for det, div, cap, label in (
            ('silent_contract_swallow', 5, 6,
             "silent_contract_swallow //5 (tope 6)"),
            ('fragile_source_grep_tests', 4, 4,
             "fragile_source_grep_tests //4 (tope 4)"),
            ('nondeterministic_hw_tests', 3, 4,
             "nondeterministic_hw_tests //3 (tope 4)"),
            ('config_task_keys', 2, 4,
             "config_task_keys //2 (tope 4)"),
        ):
            n = _count(lambda f, d=det: f.detector == d)
            rows.append((label, n, min(n // div, cap)))

        other_high = _count(
            lambda f: f.severity in (AudSeverity.CRITICAL, AudSeverity.HIGH)
            and f.detector not in (
                'syntax_import', 'async_module_level', 'sync_blocking',
                'singleton_violation', 'duplicate_code', 'circular_import',
            ))
        rows.append(("otros CRITICAL/HIGH ×10", other_high, other_high * 10))
        return rows

    def compute_score(self) -> int:
        """SCORE AXIOMA /100 (fusionado de axioma_inspector, sección 10).

        Replica la fórmula del inspector: penaliza por CATEGORÍA (no por cada
        hallazgo), para no colapsar a 0 por volumen de LOW informativos.
        El desglose vive en `score_breakdown()` (fuente única de verdad).
        """
        return max(0, min(100, 100 - sum(d for _, _, d in self.score_breakdown())))

    # ── A1: deuda, baseline y ratchet ───────────────────────────────────
    def severity_counts(self) -> Dict[str, int]:
        return severity_counts(self.findings)

    def debt_summary(self) -> Dict[str, Any]:
        return debt_summary(self.findings)

    def baseline_document(self) -> Dict[str, Any]:
        """Documento versionable que congela la deuda actual (A1)."""
        debt = self.debt_summary()
        return {
            "schema": AUDIT_BASELINE_SCHEMA,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_commit": _git_commit(self.root),
            "auditor_version": AUDITOR_VERSION,
            "root": str(self.root),
            "score": self.compute_score(),
            "counts": self.severity_counts(),
            "debt_index": debt["index"],
            "by_detector": debt["by_detector"],
            "detectors_run": [r.name for r in self.detector_results],
        }

    def regression_summary(self) -> Dict[str, Any]:
        return compare_to_baseline(
            self.debt_summary(), self.severity_counts(),
            self.baseline, self.baseline_path)

    def history_record(self, report_path: Optional[str] = None) -> Dict[str, Any]:
        """Línea de `audit_history.jsonl` (A7: permite ver tendencia)."""
        counts = self.severity_counts()
        debt = self.debt_summary()
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_commit": _git_commit(self.root),
            "auditor_version": AUDITOR_VERSION,
            "score": self.compute_score(),
            "debt_index": debt["index"],
            "counts": {k: counts.get(k, 0) for k in
                       ("total", "critical", "high", "medium", "low", "info")},
            "by_detector": {r.name: len(r.findings)
                            for r in self.detector_results},
            "report": report_path,
        }

    def verify_known_findings(self,
                              known: List[Dict[str, Any]]) -> List[AudFinding]:
        verified: List[AudFinding] = []
        for k in known:
            finding_id = k.get('id', 'KWN-???')
            title = k.get('title', '')
            file_rel = k.get('file', '')
            line = k.get('line', 0)
            patterns = k.get('patterns', [])
            path = self.root / file_rel
            if not path.exists():
                verified.append(AudFinding(
                    finding_id=finding_id, title=title, severity=AudSeverity.INFO,
                    verdict=AudVerdict.FIXED, detector='verify',
                    file=file_rel,
                    explanation="Archivo eliminado — bug no aplica."
                ))
                continue
            src = path.read_text(encoding='utf-8', errors='ignore')
            lines = src.splitlines()
            if line > len(lines) or line <= 0:
                verified.append(AudFinding(
                    finding_id=finding_id, title=title, severity=AudSeverity.INFO,
                    verdict=AudVerdict.FIXED, detector='verify',
                    file=file_rel,
                    explanation="Línea fuera de rango — archivo cambió."
                ))
                continue
            window = '\n'.join(lines[max(0, line - 20):line + 20])
            matched = all(p in window for p in patterns) if patterns else True
            if matched:
                verified.append(AudFinding(
                    finding_id=finding_id, title=title,
                    severity=AudSeverity(k.get('severity', 'MEDIUM')),
                    verdict=AudVerdict.CONFIRMED, detector='verify',
                    file=file_rel, line_start=line,
                    code_snippet=window[:300],
                    explanation=k.get('explanation',
                                      'Patrón sigue presente en el código.')
                ))
            else:
                verified.append(AudFinding(
                    finding_id=finding_id, title=title,
                    severity=AudSeverity.INFO,
                    verdict=AudVerdict.FIXED, detector='verify',
                    file=file_rel, line_start=line,
                    explanation=("Patrón no encontrado en ventana ±20 líneas — "
                                 "probablemente corregido.")
                ))
        return verified

    def generate_report(self, format: str = 'markdown') -> str:
        if format == 'markdown':
            return self._md_report()
        elif format == 'json':
            # ✅ v0.6.9v (P0 informe): el JSON ahora trae el RESUMEN que necesita
            # el quality gate (SCORE, severidades y desglose). Antes solo tenía
            # hallazgos + detectores, así que el gate habría tenido que parsear
            # el Markdown (frágil ante cambios de formato).
            confirmed = _confirmed(self.findings)
            counts = severity_counts(self.findings)
            return json.dumps({
                'summary': {
                    'score': self.compute_score(),
                    'total': len(confirmed),
                    'critical': counts.get('critical', 0),
                    'high': counts.get('high', 0),
                    'medium': counts.get('medium', 0),
                    'low': counts.get('low', 0),
                    'info': counts.get('info', 0),
                    'score_breakdown': self.score_breakdown(),
                    # ✅ A1: el SCORE no es una nota de calidad; se agrega el
                    # índice de deuda, el aviso y el ratchet contra baseline.
                    'score_meaning': SCORE_MEANING,
                    'quality_notice': (
                        'SCORE 100 NO significa calidad: sólo mide CRITICAL/HIGH. '
                        f'Deuda actual: {self.debt_summary()["index"]} puntos '
                        f'({self.debt_summary()["medium"]} MEDIUM + '
                        f'{self.debt_summary()["low"]} LOW ponderados).'),
                    'debt': self.debt_summary(),
                    'scope': self.index.scope_summary(),
                    'regression': self.regression_summary(),
                },
                'findings': [f.to_dict() for f in self.findings],
                'detectors': [
                    {'name': r.name, 'duration_ms': r.duration_ms,
                     'findings_count': len(r.findings)}
                    for r in self.detector_results
                ]
            }, indent=2, default=str)
        raise ValueError(f"Formato no soportado: {format}")

    def _md_report(self) -> str:
        confirmed = _confirmed(self.findings)
        refuted = [f for f in self.findings if f.verdict == AudVerdict.REFUTED]
        dubious = [f for f in self.findings if f.verdict == AudVerdict.DUBIOUS]
        fixed = [f for f in self.findings if f.verdict == AudVerdict.FIXED]
        scope = self.index.scope_summary()
        debt = self.debt_summary()
        regression = self.regression_summary()
        by_severity: Dict[AudSeverity, List[AudFinding]] = defaultdict(list)
        for f in confirmed:
            by_severity[f.severity].append(f)
        excluded = ", ".join(f"{k}={v}" for k, v in
                             list(scope["excluded_by_dir"].items())[:6]) or "—"
        lines = [
            "# Auditoría AXIOMA — Reporte Generado",
            "",
            f"- **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **Root:** `{self.root}`  ",
            f"- **Python:** {sys.version.split()[0]}  ",
            f"- **Auditor:** v{AUDITOR_VERSION}  ",
            f"- **Archivos indexados:** {scope['files_indexed']}  ",
            f"- **Fuera del índice:** {scope['files_excluded']} ({excluded})  ",
            f"- **Errores de parseo:** {scope['parse_errors']}  ",
            f"- **tools/ y tests/ incluidos:** "
            f"{'sí' if scope['include_tools_tests'] else 'no (opt-in con --include-tools-tests)'}  ",
            f"- **Detectores ejecutados:** {len(self.detector_results)}  ",
            f"- **Hallazgos totales:** {len(self.findings)}  ",
            "",
            "## Resumen",
            "",
            "| Veredicto | Cantidad |",
            "|-----------|----------|",
            f"| ✅ CONFIRMED/NEW | {len(confirmed)} |",
            f"| ❌ REFUTED | {len(refuted)} |",
            f"| ⚠️ DUBIOUS | {len(dubious)} |",
            f"| 🔧 FIXED | {len(fixed)} |",
            "",
            "## Qué mide (y qué NO) el SCORE",
            "",
            f"> {SCORE_MEANING}",
            "",
            f"**Índice de deuda actual: {debt['index']} puntos** "
            f"(calificación: {debt['rating']}; {debt['medium']} MEDIUM ×5 + "
            f"{debt['low']} LOW ×1; INFO={debt['info']} no pesa).",
            "",
            "| Detector | MEDIUM | LOW | INFO | Total |",
            "|---|---|---|---|---|",
        ]
        for det, row in debt["by_detector"].items():
            lines.append(f"| {det} | {row['medium']} | {row['low']} | "
                         f"{row['info']} | {row['total']} |")
        if not debt["by_detector"]:
            lines.append("| (sin deuda MEDIUM/LOW/INFO) | 0 | 0 | 0 | 0 |")
        if regression.get("baseline_present"):
            regs = regression.get("regressions") or []
            estado = ("✅ sin regresión" if regression.get("ok")
                      else f"❌ {len(regs)} regresión(es)")
            lines.extend([
                "",
                f"**Ratchet vs baseline:** {estado} "
                f"(baseline: score={regression.get('baseline_score')}, "
                f"deuda={regression.get('baseline_debt_index')}, "
                f"commit={regression.get('baseline_git_commit')}).",
            ])
            for reg in regs[:10]:
                lines.append(f"- `{reg['detector']}` {reg['severity']}: "
                             f"{reg['baseline']} → {reg['current']} "
                             f"(+{reg['delta']})")
        else:
            lines.extend([
                "",
                "**Ratchet vs baseline:** sin `audit_baseline.json` — no se "
                "puede detectar regresión de deuda (`--write-baseline`).",
            ])
        lines.extend([
            "",
            "## Hallazgos por severidad (CONFIRMED + NEW)",
            "",
        ])
        for sev in [AudSeverity.CRITICAL, AudSeverity.HIGH, AudSeverity.MEDIUM,
                    AudSeverity.LOW, AudSeverity.INFO]:
            findings = by_severity.get(sev, [])
            if not findings:
                continue
            lines.append(f"### {sev.value} ({len(findings)})")
            lines.append("")
            for f in findings:
                lines.extend(self._format_finding(f))
        lines.extend([
            "",
            "## Detectores ejecutados",
            "",
            "| Detector | Hallazgos | Tiempo (ms) | Archivos |",
            "|----------|-----------|-------------|----------|",
        ])
        for r in self.detector_results:
            lines.append(f"| {r.name} | {len(r.findings)} | "
                         f"{r.duration_ms:.1f} | {r.files_scanned} |")
        if self.test_results is not None:
            tr = self.test_results
            lines.extend([
                "",
                "## Tests (--run-tests)",
                "",
            ])
            if tr.get("available"):
                lines.append(
                    f"- **Pytest:** {tr.get('passed', 0)}/{tr.get('total', 0)} "
                    f"pasaron ({tr.get('duration_ms', 0):.0f}ms)"
                )
                for err in tr.get("errors", []):
                    lines.append(f"- ⚠️ {err}")
            else:
                lines.append(f"- **Tests:** no disponibles — {tr.get('errors')}")
        lines.extend([
            "",
            f"## SCORE AXIOMA (gate): **{self.compute_score()}/100**",
            "",
            f"> ⚠️ **SCORE 100 ≠ calidad.** {SCORE_MEANING}",
            f"> Índice de deuda: **{debt['index']} puntos** "
            f"({debt['medium']} MEDIUM, {debt['low']} LOW, {debt['info']} INFO).",
            "",
        ])
        # ✅ v0.6.9v (ISSUE-026): desglose auditable del SCORE. Antes el informe
        # mostraba solo el número final, sin explicar de dónde salía el descuento.
        _bd = self.score_breakdown()
        _total = sum(d for _, _, d in _bd)
        lines.extend([
            "| Categoría | Hallazgos | Puntos restados |",
            "|---|---|---|",
        ])
        for label, n, ded in _bd:
            marca = "" if ded == 0 else " ⬅️"
            lines.append(f"| {label} | {n} | -{ded}{marca} |")
        lines.append(f"| **Total restado** |  | **-{_total}** |")
        lines.append("")
        return '\n'.join(lines)

    def _format_finding(self, f: AudFinding) -> List[str]:
        out = [
            f"#### `{f.finding_id}` — {f.title}",
            "",
            f"- **Severidad:** {f.severity.value}",
            f"- **Veredicto:** {f.verdict.value}",
            f"- **Archivo:** `{f.file}:{f.line_start}`"
            + (f"-{f.line_end}" if f.line_end else ""),
            f"- **Confianza:** {f.confidence:.0%}",
            f"- **Detector:** `{f.detector}`",
        ]
        if f.code_snippet:
            out.extend(["", "```python",
                        f.code_snippet.strip()[:500], "```"])
        if f.explanation:
            out.extend(["", f"**Explicación:** {f.explanation}"])
        if f.recommendation:
            out.extend(["", f"**Recomendación:** {f.recommendation}"])
        if f.tags:
            out.extend(["", f"**Tags:** {', '.join(sorted(f.tags))}"])
        out.append("")
        return out


# ============================================================================
# CLI
# ============================================================================

# ✅ v0.6.9v (ISSUE-058): cuántos reportes de auditoría se conservan en logs/.
AUDIT_REPORTS_KEEP = 1   # el JSON (audit_summary.json) es el que lee el gate


def _prune_old_audit_reports(logs_dir: Path, keep: int = AUDIT_REPORTS_KEEP) -> int:
    """Borra los `axioma_audit_report_*.md` más viejos. Devuelve cuántos borró.

    Delega en `prune_reports()` (implementación única, en `tools/detailed_logger.py`)
    y no toca otras familias de `logs/`.
    """
    from tools.detailed_logger import prune_reports
    return prune_reports("axioma_audit_report_*.md", keep=keep, logs_dir=logs_dir)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(f"Auditor AXIOMA v{AUDITOR_VERSION} — auditoría completa "
                     f"({len(DETECTORS)} detectores + informe en logs/)"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Ejemplos:
              python axioma_auditor.py --full --root . -o reporte.md
              python axioma_auditor.py --full --run-tests     # incluye pytest + SCORE
              python axioma_auditor.py --discover --detectors lru,unbounded_cache
              python axioma_auditor.py --verify --findings hallazgos.json
              python axioma_auditor.py --write-baseline       # congela la deuda actual
              python axioma_auditor.py --ratchet              # falla si empeora vs baseline
              python axioma_auditor.py --fail-on HIGH         # exit 2 con HIGH/CRITICAL
              python axioma_auditor.py --include-tools-tests  # audita también tools/ y tests/

            SCORE /100 mide SÓLO CRITICAL/HIGH. La deuda MEDIUM/LOW se reporta
            aparte (summary.debt) y el ratchet vive en summary.regression.
        """)
    )
    parser.add_argument('--root', default='.',
                        help='Root del proyecto AXIOMA')
    parser.add_argument('--mode', choices=['discover', 'verify', 'full'],
                        default='full')
    parser.add_argument('--full', action='store_const', const='full', dest='mode',
                        help='Atajo para --mode full')
    parser.add_argument('--discover', action='store_const', const='discover', dest='mode',
                        help='Atajo para --mode discover')
    parser.add_argument('--verify', action='store_const', const='verify', dest='mode',
                        help='Atajo para --mode verify')
    parser.add_argument('--detectors',
                        help='Lista de detectores separados por coma')
    parser.add_argument('--findings',
                        help='Archivo JSON con hallazgos previos a verificar')
    parser.add_argument('--run-tests', action='store_true',
                        help='Ejecuta pytest (test_axioma_*.py) y lo incluye en el reporte')
    parser.add_argument('--output', '-o',
                        help='Archivo de salida (default: logs/axioma_audit_report_<ts>.md; "-" = stdout)')
    parser.add_argument('--format', choices=['markdown', 'json'],
                        default='markdown')
    parser.add_argument('--verbose', '-v', action='store_true')
    # ── A6/A7/A8/A1: scope, historial, exit code y ratchet ─────────────
    parser.add_argument('--include-tools-tests', action='store_true',
                        help='Incluye tools/ y tests/ en el índice (opt-in; por '
                             'defecto quedan fuera)')
    parser.add_argument('--baseline', default=None,
                        help='Ruta del baseline de deuda (default: '
                             '<root>/audit_baseline.json)')
    parser.add_argument('--write-baseline', action='store_true',
                        help='Escribe el baseline con la deuda actual y sale 0')
    parser.add_argument('--ratchet', action='store_true',
                        help='Sale con código 2 si hay regresión vs el baseline '
                             '(más MEDIUM/LOW que el baseline)')
    parser.add_argument('--fail-on', choices=['CRITICAL', 'HIGH', 'MEDIUM'],
                        type=str.upper, default=None,
                        help='Sale con código 2 si hay hallazgos de esa severidad '
                             'o superior (HIGH|MEDIUM; no cambia el JSON)')
    parser.add_argument('--no-history', action='store_true',
                        help='No escribe logs/audit_history.jsonl')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[!] Root no existe: {root}", file=sys.stderr)
        return 1

    auditor = AxiomaAuditor(root, verbose=args.verbose)
    auditor.build_index(include_tools_tests=args.include_tools_tests)

    if args.mode in ('discover', 'full'):
        dets = args.detectors.split(',') if args.detectors else None
        auditor.run_detectors(dets)

    if args.run_tests:
        if args.verbose:
            print("[*] Ejecutando pytest (--run-tests)...", file=sys.stderr)
        auditor.test_results = auditor.run_tests()
        if args.verbose:
            tr = auditor.test_results
            print(f"    → {tr.get('passed', 0)}/{tr.get('total', 0)} tests "
                  f"({tr.get('duration_ms', 0):.0f}ms)", file=sys.stderr)

    if args.mode in ('verify', 'full') and args.findings:
        fp = Path(args.findings)
        if fp.exists():
            known = json.loads(fp.read_text())
            verified = auditor.verify_known_findings(known)
            auditor.findings.extend(verified)
        else:
            print(f"[!] Archivo de hallazgos no encontrado: {fp}",
                 file=sys.stderr)

    # ── A1: baseline / ratchet ───────────────────────────────────
    baseline_path = (Path(args.baseline) if args.baseline
                     else root / AUDIT_BASELINE_FILENAME)
    baseline_doc = auditor.baseline_document()
    if args.write_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_doc, indent=2, ensure_ascii=False,
                       default=str),
            encoding="utf-8")
        auditor.baseline = baseline_doc
        print(f"[+] Baseline escrito en: {baseline_path}", file=sys.stderr)
    else:
        auditor.baseline = load_baseline(baseline_path)
    auditor.baseline_path = baseline_path

    report = auditor.generate_report(format=args.format)

    # ── Destino del informe ─────────────────────────────────────
    report_path: Optional[str] = None
    if args.output == '-':
        print(report)
    else:
        if args.output:
            out_path = Path(args.output)
        else:
            logs_dir = root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_path = logs_dir / f"axioma_audit_report_{ts}.md"

        out_path.write_text(report, encoding='utf-8')
        report_path = str(out_path)
        if not args.output:
            # ✅ v0.6.9v (ISSUE-058): poda de reportes viejos. El auditor escribía
            # `axioma_audit_report_<ts>.md` en cada corrida y **nunca** limpiaba:
            # habían llegado a 51 archivos (~5 MB) en `logs/`. Se conservan los
            # últimos AUDIT_REPORTS_KEEP (los otros pruners del proyecto ya hacen
            # esto con sus familias: test reports y health reports).
            _prune_old_audit_reports(logs_dir, keep=AUDIT_REPORTS_KEEP)

    # ── A7: historial append-only (no lo toca la poda de reportes) ──
    if not args.no_history:
        try:
            hist = append_history(root, auditor.history_record(report_path))
            if args.verbose:
                print(f"[*] Historial: {hist}", file=sys.stderr)
        except OSError as exc:
            print(f"[!] No se pudo escribir el historial: {exc}", file=sys.stderr)

    counts = auditor.severity_counts()
    debt = auditor.debt_summary()
    regression = auditor.regression_summary()
    print(f"[+] Auditoría completa: {counts['total']} hallazgos "
          f"(CRITICAL={counts['critical']}, HIGH={counts['high']}, "
          f"MEDIUM={counts['medium']}, LOW={counts['low']}, "
          f"INFO={counts['info']})", file=sys.stderr)
    print(f"[+] SCORE AXIOMA (gate): {auditor.compute_score()}/100 — mide SÓLO "
          f"CRITICAL/HIGH; NO mide calidad", file=sys.stderr)
    print(f"[+] Índice de deuda: {debt['index']} puntos "
          f"({debt['medium']} MEDIUM + {debt['low']} LOW; {debt['rating']})",
          file=sys.stderr)
    if regression.get("baseline_present"):
        regs = regression.get("regressions") or []
        if regs:
            print(f"[!] Ratchet: {len(regs)} regresión(es) de deuda vs "
                  f"{baseline_path}", file=sys.stderr)
        else:
            print(f"[+] Ratchet: sin regresión de deuda vs {baseline_path}",
                  file=sys.stderr)
    else:
        print(f"[!] Sin baseline ({baseline_path}): no se puede detectar "
              f"regresión de deuda; usar --write-baseline", file=sys.stderr)
    if report_path:
        print(f"[+] Informe escrito en: {report_path}", file=sys.stderr)

    # ── A8: exit code opt-in (no cambia el JSON) ────────────────
    rc = 0
    if args.fail_on:
        orden = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2}
        umbral = orden[args.fail_on]
        superados = []
        for sev, idx in (('critical', 0), ('high', 1), ('medium', 2)):
            if idx <= umbral and counts.get(sev, 0) > 0:
                superados.append(f"{sev.upper()}={counts[sev]}")
        if superados:
            print(f"[!] --fail-on {args.fail_on}: {', '.join(superados)}",
                  file=sys.stderr)
            rc = 2
    if args.ratchet and regression.get("blocking"):
        print(f"[!] --ratchet: {len(regression.get('regressions') or [])} "
              f"regresión(es) vs el baseline", file=sys.stderr)
        rc = 2
    return rc


if __name__ == '__main__':
    sys.exit(main())
