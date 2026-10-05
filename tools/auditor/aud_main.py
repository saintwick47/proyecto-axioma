#!/usr/bin/env python3
"""
tools/auditor/auditor.py — registro de detectores, AxiomaAuditor y CLI main()

v0.6.8uu: extraído de axioma_auditor.py (monolítico v8.3, 3112 líneas) para
facilitar el mantenimiento. Este paquete divide el auditor en 5 módulos
(≤900 líneas c/u); axioma_auditor.py en la raíz quedó como shim de
compatibilidad (CLI y exports idénticos).
"""
from __future__ import annotations

import ast
import argparse
import json
import logging
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
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
    NondeterministicHardwareTestDetector,
    SilentContractSwallowDetector,
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
}


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

    def build_index(self) -> None:
        if self.verbose:
            print(f"[*] Indexando {self.root}...", file=sys.stderr)
        self.index.build()
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
            confirmed = [f for f in self.findings
                         if f.verdict in (AudVerdict.CONFIRMED, AudVerdict.NEW)]
            by_sev: Dict[str, int] = defaultdict(int)
            for f in confirmed:
                by_sev[str(getattr(f.severity, "value", f.severity)).lower()] += 1
            return json.dumps({
                'summary': {
                    'score': self.compute_score(),
                    'total': len(confirmed),
                    'critical': by_sev.get('critical', 0),
                    'high': by_sev.get('high', 0),
                    'medium': by_sev.get('medium', 0),
                    'low': by_sev.get('low', 0),
                    'score_breakdown': self.score_breakdown(),
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
        confirmed = [f for f in self.findings
                     if f.verdict in (AudVerdict.CONFIRMED, AudVerdict.NEW)]
        refuted = [f for f in self.findings if f.verdict == AudVerdict.REFUTED]
        dubious = [f for f in self.findings if f.verdict == AudVerdict.DUBIOUS]
        fixed = [f for f in self.findings if f.verdict == AudVerdict.FIXED]
        by_severity: Dict[AudSeverity, List[AudFinding]] = defaultdict(list)
        for f in confirmed:
            by_severity[f.severity].append(f)
        lines = [
            "# Auditoría AXIOMA — Reporte Generado",
            "",
            f"- **Fecha:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **Root:** `{self.root}`  ",
            f"- **Python:** {sys.version.split()[0]}  ",
            f"- **Archivos indexados:** {len(self.index.files)}  ",
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
            "## Hallazgos por severidad (CONFIRMED + NEW)",
            "",
        ]
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
            f"## SCORE AXIOMA: **{self.compute_score()}/100**",
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
        description="Auditor AXIOMA v8.3 — auditoría completa (26 detectores + informe en logs/)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Ejemplos:
              python axioma_auditor.py --full --root . -o reporte.md
              python axioma_auditor.py --full --run-tests     # incluye pytest + SCORE
              python axioma_auditor.py --discover --detectors lru,unbounded_cache
              python axioma_auditor.py --verify --findings hallazgos.json
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
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[!] Root no existe: {root}", file=sys.stderr)
        return 1

    auditor = AxiomaAuditor(root, verbose=args.verbose)
    auditor.build_index()

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

    report = auditor.generate_report(format=args.format)

    # ── Destino del informe ─────────────────────────────────────
    if args.output == '-':
        print(report)
        return 0
    if args.output:
        out_path = Path(args.output)
    else:
        logs_dir = root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = logs_dir / f"axioma_audit_report_{ts}.md"

    out_path.write_text(report, encoding='utf-8')
    if not args.output:
        # ✅ v0.6.9v (ISSUE-058): poda de reportes viejos. El auditor escribía
        # `axioma_audit_report_<ts>.md` en cada corrida y **nunca** limpiaba:
        # habían llegado a 51 archivos (~5 MB) en `logs/`. Se conservan los
        # últimos AUDIT_REPORTS_KEEP (los otros pruners del proyecto ya hacen
        # esto con sus familias: test reports y health reports).
        _prune_old_audit_reports(logs_dir, keep=AUDIT_REPORTS_KEEP)

    confirmed = [f for f in auditor.findings
                 if f.verdict in (AudVerdict.CONFIRMED, AudVerdict.NEW)]
    crit = sum(1 for f in confirmed if f.severity == AudSeverity.CRITICAL)
    high = sum(1 for f in confirmed if f.severity == AudSeverity.HIGH)
    med = sum(1 for f in confirmed if f.severity == AudSeverity.MEDIUM)
    low = sum(1 for f in confirmed if f.severity == AudSeverity.LOW)
    print(f"[+] Auditoría completa: {len(confirmed)} hallazgos "
          f"(CRITICAL={crit}, HIGH={high}, MEDIUM={med}, LOW={low})",
          file=sys.stderr)
    print(f"[+] SCORE AXIOMA: {auditor.compute_score()}/100", file=sys.stderr)
    print(f"[+] Informe escrito en: {out_path}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
