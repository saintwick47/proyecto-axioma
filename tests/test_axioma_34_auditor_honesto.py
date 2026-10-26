#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# test_axioma_34_auditor_honesto.py
# ═══════════════════════════════════════════════════════════════
# Tests de la pasada de HONESTIDAD del auditor (A1–A9). El bug de partida:
# el SCORE 100 no medía calidad (el snapshot c4cd41f^, con path traversal +
# rmtree fuera del base + 49 conexiones SQLite sin cerrar, sacaba 100/100).
#
# Autorizado por el tope R13: `test_axioma_18_auditor_regresiones.py` está en
# 946/950 líneas y los 24 archivos previos están en 826–950; no había lugar.
#
# Cada detector nuevo tiene al menos un caso POSITIVO (detecta) y uno
# NEGATIVO (no detecta el patrón seguro). `assert` real.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import pytest
import subprocess
import sys
from pathlib import Path

from tools.auditor.aud_core import (
    AudFinding,
    AudSeverity,
    AudVerdict,
    CodeIndex,
)
from tools.auditor.aud_detectors_arquitectura import (
    CsrfSimpleRequestDetector,
    MutatingRoutesInventoryDetector,
)
from tools.auditor.aud_detectors_infra import TempfileMktempDetector
from tools.auditor.aud_detectors_regresiones import (
    FileReadSinkDetector,
    NativeImportOSErrorDetector,
    PathContainmentDetector,
    UnsafeExecDetector,
    UnsafePathDeleteDetector,
)

try:
    from .axioma_report import check
except ImportError:  # pragma: no cover
    from axioma_report import check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION = "34. Auditor — SCORE honesto, deuda y detectores de seguridad"


def _index(root: Path) -> CodeIndex:
    idx = CodeIndex(root)
    idx.build()
    return idx


def _run(detector_cls, tmp_path: Path, fname: str, src: str):
    p = tmp_path / "src" / fname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return detector_cls(_index(tmp_path)).run().findings


def _finding(sev=AudSeverity.LOW, det="silent_exceptions") -> AudFinding:
    return AudFinding(
        finding_id="AUD-TEST-001", title="t", severity=sev,
        verdict=AudVerdict.NEW, detector=det, file="src/x.py", line_start=1,
    )


# ─────────────────────────────────────────────────────────────────────────
# A1 — índice de deuda, baseline y ratchet
# ─────────────────────────────────────────────────────────────────────────
class TestDeudaYBaseline:
    def test_indice_pondera_medium_x5_y_low_x1(self):
        from tools.auditor.aud_main import debt_summary
        debt = debt_summary([_finding(AudSeverity.MEDIUM, "d1"),
                             _finding(AudSeverity.MEDIUM, "d1"),
                             _finding(AudSeverity.LOW, "d1"),
                             _finding(AudSeverity.LOW, "d2"),
                             _finding(AudSeverity.INFO, "d2")])
        check("deuda = 2*5 + 2 = 12", debt["index"] == 12, str(debt["index"]), SECTION)
        check("conteo por detector", debt["by_detector"]["d1"]["medium"] == 2
              and debt["by_detector"]["d2"]["low"] == 1, str(debt["by_detector"]), SECTION)
        check("INFO no pesa", debt["info"] == 1 and debt["index"] == 12, "", SECTION)
        assert debt["index"] == 12 and debt["info"] == 1
        assert debt["by_detector"]["d1"]["medium"] == 2

    def test_ratchet_solo_bloquea_si_empeora(self):
        from tools.auditor.aud_main import compare_to_baseline
        base = {"counts": {"critical": 0, "high": 0},
                "by_detector": {"d1": {"medium": 1, "low": 2}},
                "score": 100, "debt_index": 7}
        mejor = {"by_detector": {"d1": {"medium": 1, "low": 1}}, "index": 6}
        peor = {"by_detector": {"d1": {"medium": 2, "low": 2}}, "index": 12}
        ok = compare_to_baseline(mejor, {"critical": 0, "high": 0}, base, Path("b.json"))
        mal = compare_to_baseline(peor, {"critical": 0, "high": 0}, base, Path("b.json"))
        check("mejorar no bloquea", ok["ok"] and not ok["regressions"], str(ok), SECTION)
        check("empeorar bloquea", not mal["ok"] and mal["blocking"]
              and mal["regressions"][0]["delta"] == 1, str(mal["regressions"]), SECTION)
        assert ok["ok"] and not ok["regressions"]
        assert not mal["ok"] and mal["regressions"]

    def test_sin_baseline_no_inventa_regresion(self):
        from tools.auditor.aud_main import compare_to_baseline
        out = compare_to_baseline({"by_detector": {}, "index": 0},
                                  {"critical": 0, "high": 0}, None, None)
        check("sin baseline: no bloquea", out["ok"] and not out["baseline_present"],
              str(out), SECTION)
        assert out["ok"] and out["regressions"] == []

    def test_json_expone_deuda_scope_y_aviso(self, tmp_path):
        from tools.auditor.aud_main import AxiomaAuditor
        a = AxiomaAuditor(tmp_path)
        a.findings = [_finding(AudSeverity.MEDIUM), _finding()]
        data = json.loads(a.generate_report("json"))
        summary = data["summary"]
        for key in ("score_meaning", "quality_notice", "debt", "scope",
                    "regression", "score_breakdown"):
            check(f"summary.{key} presente", key in summary, "", SECTION)
            assert key in summary
        check("deuda MEDIUM/LOW visible", summary["debt"]["index"] == 6,
              str(summary["debt"]["index"]), SECTION)
        check("el aviso dice que 100 no es calidad",
              "NO significa" in summary["score_meaning"], summary["score_meaning"], SECTION)
        check("scope reporta indexados y fuera", "files_indexed" in summary["scope"]
              and "files_excluded" in summary["scope"], str(summary["scope"]), SECTION)
        assert summary["debt"]["index"] == 6
        assert "NO significa" in summary["score_meaning"]

    def test_gate_bloquea_por_regresion_de_deuda(self):
        import tools.quality_gate as gate
        dets = [{"name": n, "findings_count": 0} for n in gate.GATED_DETECTORS]
        tests = {"path": "t", "worst_exitstatus": 0, "resumen_ok": True,
                 "archivos_total": 1, "archivos_ok": 1, "tests_collected": 300,
                 "run_id": "r"}
        health = {"path": "h", "stale_hours": 0.1, "anomalias": False}
        base = {"summary": {"score": 100, "critical": 0, "high": 0,
                            "medium": 1, "low": 0,
                            "debt": {"index": 5, "medium": 1, "low": 0},
                            "regression": {"baseline_present": True,
                                           "regressions": []}},
                "findings": [], "detectors": dets}
        malo = json.loads(json.dumps(base))
        malo["summary"]["regression"]["regressions"] = [
            {"detector": "x", "severity": "MEDIUM",
             "baseline": 0, "current": 1, "delta": 1}]
        ok = gate.evaluate(base, tests, health)
        bad = gate.evaluate(malo, tests, health)
        check("sin regresión → gate PASA", ok.ok,
              str([c.name for c in ok.checks if not c.passed]), SECTION)
        check("con regresión → gate BLOQUEA", not bad.ok,
              str([c.name for c in bad.checks if not c.passed]), SECTION)
        assert ok.ok and not bad.ok


# ─────────────────────────────────────────────────────────────────────────
# A2 — contención de paths y borrado sobre paths derivados
# ─────────────────────────────────────────────────────────────────────────
class TestPathContainment:
    BAD = '''
import shutil
class WM:
    def __init__(self):
        self._base = __import__("pathlib").Path("/tmp/base")
    def get_workspace(self, session_id):
        ws = self._base / session_id
        ws.mkdir(parents=True, exist_ok=True)
        return str(ws)
'''
    GOOD = '''
from pathlib import Path
class WM:
    def __init__(self):
        self._base = Path("/tmp/base")
    def get_workspace(self, name):
        p = (self._base / name).resolve()
        if not p.is_relative_to(self._base.resolve()):
            raise ValueError("fuera del base")
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
'''
    GOOD_REGEX = '''
from pathlib import Path
import re
_SID = re.compile(r"^[A-Za-z0-9_-]+$")
class WM:
    def get_workspace(self, sid):
        if not _SID.fullmatch(sid):
            raise ValueError("sid inválido")
        p = Path("/tmp/base") / sid
        p.write_text("x", encoding="utf-8")
        return str(p)
'''

    def test_detecta_path_derivado_sin_contencion(self, tmp_path):
        f = _run(PathContainmentDetector, tmp_path, "wm.py", self.BAD)
        check("path traversal detectado", bool(f), "0 hallazgos", SECTION)
        check("severidad MEDIUM", f and f[0].severity == AudSeverity.MEDIUM,
              str(f[0].severity) if f else "sin findings", SECTION)
        assert f and f[0].severity == AudSeverity.MEDIUM

    def test_acepta_is_relative_to(self, tmp_path):
        assert _run(PathContainmentDetector, tmp_path, "wm_ok.py", self.GOOD) == []

    def test_acepta_allowlist_regex(self, tmp_path):
        assert _run(PathContainmentDetector, tmp_path, "wm_re.py", self.GOOD_REGEX) == []


class TestUnsafePathDelete:
    BAD = '''
import shutil
class WM:
    def __init__(self):
        self._base = __import__("pathlib").Path("/tmp/base")
    def cleanup(self, session_id):
        ws = self._base / session_id
        shutil.rmtree(ws, ignore_errors=True)
'''
    GOOD = '''
import shutil
from pathlib import Path
class WM:
    def __init__(self):
        self._base = Path("/tmp/base")
    def cleanup(self, sid):
        ws = (self._base / sid).resolve()
        if not ws.is_relative_to(self._base.resolve()):
            raise ValueError("fuera")
        shutil.rmtree(ws, ignore_errors=True)
'''

    def test_detecta_rmtree_sobre_path_derivado(self, tmp_path):
        f = _run(UnsafePathDeleteDetector, tmp_path, "del.py", self.BAD)
        check("rmtree inseguro detectado", bool(f), "0 hallazgos", SECTION)
        check("severidad HIGH", f and f[0].severity == AudSeverity.HIGH,
              str(f[0].severity) if f else "", SECTION)
        assert f and f[0].severity == AudSeverity.HIGH

    def test_acepta_contencion_antes_de_borrar(self, tmp_path):
        assert _run(UnsafePathDeleteDetector, tmp_path, "del_ok.py", self.GOOD) == []


# ─────────────────────────────────────────────────────────────────────────
# A2 — imports nativos que sólo atrapan ImportError
# ─────────────────────────────────────────────────────────────────────────
class TestNativeImportOSError:
    BAD = '''
try:
    import sounddevice as sd
    _AUDIO = True
except ImportError:
    sd = None
    _AUDIO = False
'''
    GOOD_TUPLE = '''
try:
    import sounddevice as sd
    _AUDIO = True
except (ImportError, OSError):
    sd = None
    _AUDIO = False
'''
    GOOD_PLAIN = '''
try:
    import json
    _JSON = True
except ImportError:
    _JSON = False
'''

    def test_detecta_import_nativo_con_solo_importerror(self, tmp_path):
        f = _run(NativeImportOSErrorDetector, tmp_path, "audio.py", self.BAD)
        check("sounddevice + ImportError detectado", bool(f), "0 hallazgos", SECTION)
        assert f and f[0].severity == AudSeverity.MEDIUM

    def test_acepta_oserror_en_el_tuple(self, tmp_path):
        assert _run(NativeImportOSErrorDetector, tmp_path, "audio_ok.py",
                    self.GOOD_TUPLE) == []

    def test_no_marca_import_puro_python(self, tmp_path):
        assert _run(NativeImportOSErrorDetector, tmp_path, "json_ok.py",
                    self.GOOD_PLAIN) == []


# ─────────────────────────────────────────────────────────────────────────
# A2 — shell=True / eval / exec / pickle
# ─────────────────────────────────────────────────────────────────────────
class TestUnsafeExec:
    BAD = '''
import os, subprocess
def correr(cmd, expresion):
    subprocess.run(cmd, shell=True)
    return eval(expresion)
'''
    GOOD = '''
import subprocess
def correr():
    subprocess.run(["ls", "-la"], shell=False)
    return eval("1 + 1")
'''

    def test_detecta_shell_true_y_eval_dinamico(self, tmp_path):
        f = _run(UnsafeExecDetector, tmp_path, "exec.py", self.BAD)
        sev = {x.severity for x in f}
        check("shell=True y eval dinámico detectados", len(f) >= 2, str(len(f)), SECTION)
        check("eval dinámico es HIGH", AudSeverity.HIGH in sev, str(sev), SECTION)
        assert len(f) >= 2 and AudSeverity.HIGH in sev

    def test_ignora_constantes_y_shell_false(self, tmp_path):
        assert _run(UnsafeExecDetector, tmp_path, "exec_ok.py", self.GOOD) == []


class TestDetectoresA2Registrados:
    def test_registrados_en_el_auditor(self):
        from tools.auditor.aud_main import DETECTORS
        for name in ("path_containment", "unsafe_path_delete",
                     "native_import_oserror", "unsafe_exec"):
            check(f"{name} registrado", name in DETECTORS, "", SECTION)
            assert name in DETECTORS


class TestProyectoRealSinMEDIUMDeSeguridad:
    """Regresión de los 15 MEDIUM corregidos (AUD-PTH-001..007 / AUD-IEO-001..008).

    Sin este test, un `except ImportError` o un `base / parámetro` nuevos
    reaparecen como MEDIUM y sólo se ven al correr el auditor a mano. El árbol
    real tiene que quedar en 0 MEDIUM para estos dos detectores.
    """

    def test_path_containment_y_native_import_en_cero(self):
        idx = _index(PROJECT_ROOT)
        for detector_cls in (PathContainmentDetector, NativeImportOSErrorDetector):
            findings = detector_cls(idx).run().findings
            medium = [f for f in findings if f.severity is AudSeverity.MEDIUM]
            detalle = [f"{f.file}:{f.line_start}" for f in medium][:5]
            check(f"{detector_cls.name}: 0 MEDIUM en el proyecto real",
                  not medium, str(detalle), SECTION)
            assert not medium, detalle


# ─────────────────────────────────────────────────────────────────────────
# A3 — sqlite3.connect sin cierre asegurado
# ─────────────────────────────────────────────────────────────────────────
class TestSQLiteSinCierre:
    BAD_WITH = '''
import sqlite3
def guardar(path, valor):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO t VALUES (?)", (valor,))
'''
    GOOD_CLOSING = '''
import sqlite3
from contextlib import closing
def guardar(path, valor):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO t VALUES (?)", (valor,))
'''
    GOOD_FINALLY = '''
import sqlite3
def guardar(path, valor):
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO t VALUES (?)", (valor,))
        conn.commit()
    finally:
        conn.close()
'''
    GOOD_ATTR = '''
import sqlite3
class Repo:
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
    def close(self):
        self._conn.close()
'''

    def _findings(self, tmp_path, src, name):
        from tools.auditor.aud_detectors_infra import ConnectionPerMethodDetector
        return _run(ConnectionPerMethodDetector, tmp_path, name, src)

    def test_with_sqlite_connect_cuenta_como_sin_cierre(self, tmp_path):
        f = self._findings(tmp_path, self.BAD_WITH, "bad.py")
        check("`with sqlite3.connect` se marca", len(f) == 1, str(len(f)), SECTION)
        check("explica que __exit__ no cierra",
              f and "__exit__" in f[0].explanation, f[0].explanation[:80] if f else "", SECTION)
        assert len(f) == 1 and "__exit__" in f[0].explanation

    def test_acepta_contextlib_closing(self, tmp_path):
        assert self._findings(tmp_path, self.GOOD_CLOSING, "closing_ok.py") == []

    def test_acepta_finally_close(self, tmp_path):
        assert self._findings(tmp_path, self.GOOD_FINALLY, "finally_ok.py") == []

    def test_acepta_atributo_cerrado_por_la_clase(self, tmp_path):
        assert self._findings(tmp_path, self.GOOD_ATTR, "attr_ok.py") == []


# ─────────────────────────────────────────────────────────────────────────
# A4 — config_refs tokenize/AST-aware (sin FPs en strings/comentarios)
# ─────────────────────────────────────────────────────────────────────────
class TestConfigRefsAST:
    FIXTURE = '''
"""Docstring con settings.dashboard_title (no es referencia)."""
def mensaje():
    return "Editá settings.dashboard_title en la configuración"

def anotado(x: "settings.campo"):
    return x

X = 1  # settings.comentario

def real():
    return settings.esto_no_existe_xyz
'''
    VALIDO = '''
def f():
    return settings.data_dir
'''

    def _findings(self, tmp_path, src, fname="cfg.py"):
        from tools.auditor.aud_detectors_arquitectura import ConfigRefsDetector
        return _run(ConfigRefsDetector, tmp_path, fname, src)

    def test_solo_marca_la_referencia_real(self, tmp_path):
        f = self._findings(tmp_path, self.FIXTURE)
        check("1 hallazgo (sólo el acceso real)", len(f) == 1, str(len(f)), SECTION)
        check("marca el atributo inexistente real",
              f and "esto_no_existe_xyz" in f[0].title,
              f[0].title if f else "sin findings", SECTION)
        assert len(f) == 1 and "esto_no_existe_xyz" in f[0].title

    def test_no_marca_atributo_valido(self, tmp_path):
        assert self._findings(tmp_path, self.VALIDO, "cfg_ok.py") == []


# ─────────────────────────────────────────────────────────────────────────
# A5 — silent_exceptions sin ruido (sondas, raise, warn, print_exc)
# ─────────────────────────────────────────────────────────────────────────
class TestSilentExceptionsSinRuido:
    FIXTURE = '''
import warnings, traceback

def sonda_none():
    try:
        return open("x").read()
    except OSError:
        return None

def sonda_vacia():
    try:
        return {}
    except OSError:
        return {}

def relanza():
    try:
        open("x")
    except OSError:
        raise

def avisa():
    try:
        open("x")
    except OSError as e:
        warnings.warn(str(e))

def traza():
    try:
        open("x")
    except OSError:
        traceback.print_exc()

def real():
    try:
        open("x")
    except OSError:
        self_cache_clear()
'''

    def _findings(self, tmp_path, src):
        from tools.auditor.aud_detectors_salud import SilentExceptionsDetector
        return _run(SilentExceptionsDetector, tmp_path, "silent.py", src)

    def test_sondas_raise_warn_y_print_exc_no_generan_deuda(self, tmp_path):
        f = self._findings(tmp_path, self.FIXTURE)
        low = [x for x in f if x.severity in (AudSeverity.LOW, AudSeverity.MEDIUM,
                                              AudSeverity.HIGH)]
        check("sin LOW/MEDIUM por sondas/raise/warn", not low,
              str([(x.severity.value, x.title) for x in low]), SECTION)
        check("el caso real queda visible como INFO",
              len(f) == 1 and f[0].severity == AudSeverity.INFO,
              str([(x.severity.value, x.title) for x in f]), SECTION)
        assert not low and len(f) == 1 and f[0].severity == AudSeverity.INFO


# ─────────────────────────────────────────────────────────────────────────
# A6 — métricas efectivas (no span) y alcance real del índice
# ─────────────────────────────────────────────────────────────────────────
class TestScopeYLineasEfectivas:
    COMMENTS_ONLY = (
        "def f():\n"
        + "".join(f"    # comentario {i}\n" for i in range(100))
        + "    return 1\n"
    )
    REAL_LONG = (
        "def g():\n"
        + "".join(f"    x{i} = {i}\n" for i in range(90))
        + "    return x0\n"
    )

    def _method_findings(self, tmp_path, src):
        from tools.auditor.aud_detectors_arquitectura import IntegrationDetector
        f = _run(IntegrationDetector, tmp_path, "largos.py", src)
        return [x for x in f if "Método largo" in x.title]

    def test_metodo_con_100_comentarios_no_es_largo(self, tmp_path):
        f = self._method_findings(tmp_path, self.COMMENTS_ONLY)
        check("100 comentarios no son 100 líneas de lógica", not f,
              str([x.title for x in f]), SECTION)
        assert not f

    def test_metodo_con_90_lineas_efectivas_si_es_largo(self, tmp_path):
        f = self._method_findings(tmp_path, self.REAL_LONG)
        check("90 líneas efectivas se marcan", len(f) == 1, str(len(f)), SECTION)
        check("el título muestra efectivas y físicas",
              f and "efectivas" in f[0].title and "físicas" in f[0].title,
              f[0].title if f else "", SECTION)
        assert len(f) == 1 and "efectivas" in f[0].title

    def test_scope_reporta_indexados_y_fuera(self, tmp_path):
        for rel in ("src/a.py", "tools/b.py", "tests/test_c.py"):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x = 1\n", encoding="utf-8")
        idx = _index(tmp_path)
        sc = idx.scope_summary()
        check("indexa sólo src (1)", sc["files_indexed"] == 1, str(sc), SECTION)
        check("reporta 2 fuera (tools/tests)", sc["files_excluded"] == 2,
              str(sc["excluded_by_dir"]), SECTION)
        check("tools/ y tests/ no incluidos", sc["include_tools_tests"] is False,
              str(sc), SECTION)
        assert sc["files_indexed"] == 1 and sc["files_excluded"] == 2
        idx2 = CodeIndex(tmp_path)
        idx2.build(include_tools_tests=True)
        sc2 = idx2.scope_summary()
        check("--include-tools-tests indexa los 3", sc2["files_indexed"] == 3,
              str(sc2), SECTION)
        assert sc2["files_indexed"] == 3 and sc2["include_tools_tests"] is True


# ─────────────────────────────────────────────────────────────────────────
# A7/A8/A9 — historial, exit code y metadatos
# ─────────────────────────────────────────────────────────────────────────
def _cli(root: Path, *args: str, history: bool = False):
    """Corre el CLI real del auditor contra un fixture (sin historial por defecto)."""
    cmd = [sys.executable, str(PROJECT_ROOT / "axioma_auditor.py"),
           "--root", str(root), "--format", "json"]
    if not history:
        cmd.append("--no-history")
    cmd.extend(args)
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True,
                          text=True, timeout=180)


HIGH_SRC = '''
import shutil
class WM:
    def __init__(self):
        self._base = __import__("pathlib").Path("/tmp/base")
    def cleanup(self, session_id):
        ws = self._base / session_id
        shutil.rmtree(ws, ignore_errors=True)
'''
MEDIUM_SRC = '''
import shutil
class WM:
    def __init__(self):
        self._base = __import__("pathlib").Path("/tmp/base")
    def crear(self, session_id):
        ws = self._base / session_id
        ws.mkdir(parents=True, exist_ok=True)
'''


class TestHistorialExitCodeYMetadatos:
    def test_append_history_es_jsonl_append_only(self, tmp_path):
        from tools.auditor.aud_main import append_history
        path = append_history(tmp_path, {"score": 100, "debt_index": 1})
        path = append_history(tmp_path, {"score": 90, "debt_index": 7})
        lines = path.read_text(encoding="utf-8").splitlines()
        check("historial en logs/audit_history.jsonl",
              path.name == "audit_history.jsonl", path.name, SECTION)
        check("append-only: 2 líneas", len(lines) == 2, str(len(lines)), SECTION)
        check("cada línea es JSON con score",
              [json.loads(x)["score"] for x in lines] == [100, 90],
              str(lines), SECTION)
        assert path.name == "audit_history.jsonl" and len(lines) == 2
        assert [json.loads(x)["score"] for x in lines] == [100, 90]

    def test_cli_escribe_historial_y_la_poda_no_lo_borra(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        run = _cli(tmp_path, "--detectors", "integration",
                   "-o", str(tmp_path / "o.json"), history=True)
        hist = tmp_path / "logs" / "audit_history.jsonl"
        check("el CLI escribe audit_history.jsonl", run.returncode == 0 and hist.exists(),
              run.stderr[-200:], SECTION)
        line = json.loads(hist.read_text(encoding="utf-8").splitlines()[-1])
        check("el registro trae timestamp/score/deuda",
              {"timestamp", "score", "debt_index", "counts"} <= set(line),
              str(sorted(line)), SECTION)
        logs = tmp_path / "logs"
        for i in range(4):
            (logs / f"axioma_audit_report_2026010{i}_000000.md").write_text(
                "x", encoding="utf-8")
        from tools.auditor.aud_main import _prune_old_audit_reports
        _prune_old_audit_reports(logs, keep=1)
        md = list(logs.glob("axioma_audit_report_*.md"))
        check("la poda deja 1 .md y conserva el historial",
              len(md) == 1 and hist.exists(), f"{len(md)} md", SECTION)
        assert run.returncode == 0 and hist.exists()
        assert {"timestamp", "score", "debt_index", "counts"} <= set(line)
        assert len(md) == 1 and hist.exists()

    def test_fail_on_high_y_medium(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "wm.py").write_text(HIGH_SRC, encoding="utf-8")
        base = ("--detectors", "unsafe_path_delete,path_containment")
        sin_flag = _cli(tmp_path, *base, "-o", str(tmp_path / "a.json"))
        fail_high = _cli(tmp_path, *base, "--fail-on", "HIGH",
                         "-o", str(tmp_path / "b.json"))
        check("sin --fail-on sale 0", sin_flag.returncode == 0,
              str(sin_flag.returncode), SECTION)
        check("--fail-on HIGH sale 2", fail_high.returncode == 2,
              str(fail_high.returncode), SECTION)
        a = json.loads((tmp_path / "a.json").read_text())
        b = json.loads((tmp_path / "b.json").read_text())
        check("--fail-on NO cambia el JSON",
              a["summary"]["score"] == b["summary"]["score"]
              and a["summary"]["high"] == b["summary"]["high"] == 1,
              f"{a['summary']['score']} vs {b['summary']['score']}", SECTION)
        assert sin_flag.returncode == 0 and fail_high.returncode == 2
        assert a["summary"]["score"] == b["summary"]["score"]
        assert a["summary"]["high"] == b["summary"]["high"] == 1

    def test_fail_on_high_no_dispara_con_medium(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "wm.py").write_text(MEDIUM_SRC, encoding="utf-8")
        base = ("--detectors", "path_containment")
        alto = _cli(tmp_path, *base, "--fail-on", "HIGH",
                    "-o", str(tmp_path / "h.json"))
        medio = _cli(tmp_path, *base, "--fail-on", "MEDIUM",
                     "-o", str(tmp_path / "m.json"))
        check("MEDIUM no dispara --fail-on HIGH", alto.returncode == 0,
              str(alto.returncode), SECTION)
        check("MEDIUM dispara --fail-on MEDIUM", medio.returncode == 2,
              str(medio.returncode), SECTION)
        assert alto.returncode == 0 and medio.returncode == 2

    def test_ratchet_cli_falla_al_empeorar(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ok.py").write_text(
            "def suma(a, b):\n    return a + b\n", encoding="utf-8")
        dets = ("--detectors", "integration,silent_exceptions")
        base = _cli(tmp_path, *dets, "--write-baseline",
                    "-o", str(tmp_path / "base.json"))
        check("baseline escrito", base.returncode == 0,
              base.stderr[-200:], SECTION)
        (tmp_path / "src" / "bad.py").write_text(
            "def guardar(x):\n    try:\n        return 1 / x\n"
            "    except Exception:\n        pass\n", encoding="utf-8")
        mal = _cli(tmp_path, *dets, "--ratchet", "-o", str(tmp_path / "bad.json"))
        check("--ratchet sale 2 al empeorar", mal.returncode == 2,
              f"rc={mal.returncode}", SECTION)
        data = json.loads((tmp_path / "bad.json").read_text())
        regs = data["summary"]["regression"]["regressions"]
        check("el JSON dice QUÉ empeoró", bool(regs), str(regs), SECTION)
        assert base.returncode == 0 and mal.returncode == 2 and regs

    def test_version_y_conteo_de_detectores_consistentes(self):
        from src import __version__ as project_version
        from tools.auditor.aud_main import AUDITOR_VERSION, DETECTORS
        help_out = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "axioma_auditor.py"), "--help"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        check("versión única (auditor == proyecto)",
              AUDITOR_VERSION == project_version,
              f"{AUDITOR_VERSION} vs {project_version}", SECTION)
        check("el CLI anuncia el conteo REAL", f"{len(DETECTORS)} detectores"
              in help_out.stdout, str(len(DETECTORS)), SECTION)
        check("el CLI muestra la versión", AUDITOR_VERSION in help_out.stdout,
              help_out.stdout.splitlines()[0] if help_out.stdout else "", SECTION)
        # La copia PÚBLICA regenera el README (el del privado se publica como README_TECNICO.md):
        # ahí el README.md es el público y no documenta el auditor. No es una regresión.
        if (PROJECT_ROOT / "README_TECNICO.md").exists():
            pytest.skip("copia pública: el README.md es el público, no documenta el auditor")
        for doc in ("README.md", "COMANDOS_AXIOMA.txt"):
            text = (PROJECT_ROOT / doc).read_text(encoding="utf-8")
            check(f"{doc} dice {len(DETECTORS)} detectores",
                  f"{len(DETECTORS)} detectores" in text, "", SECTION)
            check(f"{doc} sin '26 detectores'", "26 detectores" not in text,
                  "", SECTION)
            assert f"{len(DETECTORS)} detectores" in text
            assert "26 detectores" not in text
        assert AUDITOR_VERSION == project_version
        assert f"{len(DETECTORS)} detectores" in help_out.stdout


# ─────────────────────────────────────────────────────────────────────────
# v0.7.6 — detectores de seguridad (AUD-FSR / AUTH-002 / AUTH-003 / TMP)
# ─────────────────────────────────────────────────────────────────────────
# Bug real medido: `POST /analyze_file` aceptaba `file_id="/etc/passwd"`,
# `Path(upload_dir) / "/etc/passwd"` descartaba la base y
# `extract_file_text` leía el archivo. AUD-PTH no lo veía porque el sink era
# el helper. Además: 3 POST sin auth con body "simple" (CSRF), 7 rutas
# mutantes sin auth, 3 `tempfile.mktemp` en tools/ (fuera del scope histórico).

FSR_SRC = '''
from pathlib import Path
from src.utils.file_text import extract_file_text

def analizar(request, base):
    p = base / request.file_id
    contenido = extract_file_text(p)
    return Message(content=f"Analiza: {contenido}")
'''

FSR_SAFE_SRC = '''
from pathlib import Path

def analizar(request, base):
    fid = Path(request.file_id).name
    p = (base / fid).resolve()
    if not p.is_relative_to(base.resolve()):
        raise ValueError("fuera del base")
    return extract_file_text(p)
'''

AUTH_SRC = '''
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

router = APIRouter()
def exigir_token_de_escritura(request): ...

class Cuerpo(BaseModel):
    valor: str

@router.post("/simple")
async def simple(flag: bool = False): ...

@router.post("/upload")
async def subir(file: UploadFile = File(...)): ...

@router.post("/json")
async def json_ep(datos: Cuerpo): ...

@router.post("/con-token", dependencies=[Depends(exigir_token_de_escritura)])
async def con_token(datos: Cuerpo): ...

@router.delete("/borrar/{x}")
async def borrar(x: str): ...
'''

TMP_SRC = '''
import os
import tempfile

a = tempfile.mktemp(suffix=".png")
with tempfile.NamedTemporaryFile() as f:
    pass

def x(tmp_path_factory):
    return tmp_path_factory.mktemp("x")

b = os.tmpnam()
'''

EXEC_LITERAL_SRC = '''
import os

def limpiar():
    os.system('clear' if os.name != 'nt' else 'cls')
    os.system("echo " + "hola")

def ejecutar(cmd):
    os.system(cmd)
'''


class TestDetectoresSeguridadV076:
    def test_fsr_detecta_helper_de_lectura_con_path_derivado(self, tmp_path):
        f = _run(FileReadSinkDetector, tmp_path, "traversal.py", FSR_SRC)
        check("AUD-FSR detecta extract_file_text(path derivado)", len(f) == 1,
              str([(x.finding_id, x.line_start) for x in f]), SECTION)
        check("AUD-FSR ancla el hallazgo en la derivación (línea 6)",
              f and f[0].line_start == 6, str(f[0].line_start) if f else None, SECTION)
        check("AUD-FSR escala a HIGH si el contenido sale al LLM",
              f and f[0].severity is AudSeverity.HIGH,
              f[0].severity if f else None, SECTION)
        assert len(f) == 1 and f[0].line_start == 6
        assert f[0].severity is AudSeverity.HIGH

    def test_fsr_no_marca_basename_ni_path_validado(self, tmp_path):
        f = _run(FileReadSinkDetector, tmp_path, "safe.py", FSR_SAFE_SRC)
        check("AUD-FSR no marca basename + is_relative_to", f == [], str(f), SECTION)
        # Y en el árbol real (upload/workspace_manager/download ya corregidos): 0.
        real = FileReadSinkDetector(_index(PROJECT_ROOT)).run().findings
        check("AUD-FSR = 0 en los lugares ya corregidos del repo", real == [],
              str([(x.file, x.line_start) for x in real]), SECTION)
        assert f == [] and real == []

    def test_auth_002_solo_post_simple_sin_token(self, tmp_path):
        f = CsrfSimpleRequestDetector(_index_src(tmp_path, AUTH_SRC)).run().findings
        rutas = sorted(x.title.split("simple request: ")[1].split(" (")[0] for x in f)
        check("AUD-AUTH-002 marca los 2 POST simples sin auth",
              rutas == ["POST /simple", "POST /upload"], str(rutas), SECTION)
        check("AUD-AUTH-002 NO marca el que tiene token",
              not any("con-token" in r for r in rutas), str(rutas), SECTION)
        check("AUD-AUTH-002 NO marca body Pydantic (json) ni DELETE",
              all("json" not in r and "borrar" not in r for r in rutas), str(rutas), SECTION)
        check("AUD-AUTH-002 severidad MEDIUM",
              all(x.severity is AudSeverity.MEDIUM for x in f), "", SECTION)
        assert rutas == ["POST /simple", "POST /upload"]

    def test_auth_003_inventario_agregado_info(self, tmp_path):
        f = MutatingRoutesInventoryDetector(
            _index_src(tmp_path, AUTH_SRC)).run().findings
        check("AUD-AUTH-003 emite UN hallazgo agregado", len(f) == 1, str(len(f)), SECTION)
        check("AUD-AUTH-003 es INFO", f and f[0].severity is AudSeverity.INFO, "", SECTION)
        # sin auth: simple, upload, json, borrar (con-token SÍ tiene auth)
        check("AUD-AUTH-003 lista las 4 rutas sin auth",
              f and "4 rutas mutantes sin auth" in f[0].title,
              f[0].title if f else None, SECTION)
        assert len(f) == 1 and f[0].severity is AudSeverity.INFO
        assert all(r in f[0].code_snippet for r in ("/simple", "/upload", "/json", "/borrar"))

    def test_tmp_mktemp_scoped_a_tools(self, tmp_path):
        # En `tools/`, el índice por defecto NO lo ve (scope histórico).
        p = tmp_path / "tools" / "probe.py"
        p.parent.mkdir(parents=True)
        p.write_text(TMP_SRC, encoding="utf-8")
        default = TempfileMktempDetector(_index(tmp_path)).run().findings
        check("AUD-TMP respeta el scope por defecto (tools/ fuera)", default == [],
              str(default), SECTION)
        check("AUD-TMP declara include_tools", TempfileMktempDetector.include_tools is True,
              "", SECTION)
        ampliado = CodeIndex(tmp_path)
        ampliado.build(include_tools_tests=True)
        f = TempfileMktempDetector(ampliado).run().findings
        lineas = sorted(x.line_start for x in f)
        check("AUD-TMP encuentra tempfile.mktemp y os.tmpnam", len(f) == 2,
              str([(x.line_start, x.title) for x in f]), SECTION)
        check("AUD-TMP NO marca NamedTemporaryFile ni tmp_path_factory.mktemp",
              lineas == [5, 12], str(lineas), SECTION)
        assert len(f) == 2 and lineas == [5, 12]

    def test_exec_no_marca_ternario_de_literales(self, tmp_path):
        f = _run(UnsafeExecDetector, tmp_path, "limpiar.py", EXEC_LITERAL_SRC)
        check("AUD-EXEC no marca el ternario de literales (FP de netron)",
              len(f) == 1, str([(x.line_start, x.title) for x in f]), SECTION)
        check("AUD-EXEC sigue marcando os.system(cmd) dinámico",
              f and f[0].line_start == 9, str(f[0].line_start) if f else None, SECTION)
        assert len(f) == 1 and f[0].line_start == 9


def _index_src(tmp_path: Path, src: str) -> CodeIndex:
    """Índice de un fixture con un único `src/app.py`."""
    p = tmp_path / "src" / "app.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return _index(tmp_path)
