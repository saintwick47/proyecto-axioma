import json
import subprocess
import sys
from pathlib import Path
import pytest
try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SECTION = "22. Quality gate"


def _gate():
    import tools.quality_gate as g
    return g
TESTS_OK = (
    "# Informe de Verificación Integral — AXIOMA\n\n"
    "- **Tanda (run-id):** `batch_20260101_000000` — los resultados de todos los\n"
    "  archivos de la tanda se acumulan en ESTE archivo.\n\n"
    "- 🧪 pytest exitstatus: **0** (OK)\n"
    "**Ejecución pytest:** exitstatus=0 — tests recolectados=300\n"
    "- **Veredicto: ✅ TODO CORRECTO**\n"
)
HEALTH_OK = ("- **Vigencia:** ✅ reciente — último evento hace 0.3 h\n"
             "## 🩺 Anomalías / diagnóstico\n- ✅ sin anomalías detectadas\n")


def _audit(score=100, high=0, critical=0, detectors=None):
    dets = detectors if detectors is not None else [
        {"name": n, "duration_ms": 1.0, "findings_count": 0}
        for n in _gate().GATED_DETECTORS
    ]
    return {"summary": {"score": score, "total": 0, "critical": critical,
                        "high": high, "medium": 13, "low": 175,
                        "score_breakdown": []},
            "findings": [], "detectors": dets}


class TestEvaluateBlocks:
    @staticmethod
    def _fixtures(tmp_path, tests_text=TESTS_OK, health_text=HEALTH_OK):
        (tmp_path / "t.md").write_text(tests_text, encoding="utf-8")
        (tmp_path / "h.md").write_text(health_text, encoding="utf-8")
        return (_gate().parse_test_report(tmp_path / "t.md"),
                _gate().parse_health_report(tmp_path / "h.md"))

    def test_all_good_passes(self, tmp_path):
        tests, health = self._fixtures(tmp_path)
        r = _gate().evaluate(_audit(), tests, health)
        check("todo OK → gate PASA", r.ok,
              str([c.name for c in r.checks if not c.passed and not c.skipped]),
              SECTION)
        assert r.ok

    def test_low_score_blocks(self):
        r = _gate().evaluate(_audit(score=90), {"path": "t", "worst_exitstatus": 0,
                                                "resumen_ok": True, "archivos_total": 1,
                                                "archivos_ok": 1, "tests_collected": 300,
                                                "run_id": "r"}, {"path": "h"})
        check("SCORE 90 bloquea", not r.ok, str([c.name for c in r.checks
                                                 if not c.passed]), SECTION)
        assert not r.ok

    def test_high_and_critical_block(self):
        for field, val in (("high", 1), ("critical", 2)):
            audit = _audit(**{field: val})
            r = _gate().evaluate(audit, {"path": "t", "worst_exitstatus": 0,
                                         "resumen_ok": True, "archivos_total": 1,
                                         "archivos_ok": 1, "tests_collected": 300,
                                         "run_id": "r"}, {"path": "h"})
            check(f"{field.upper()}={val} bloquea", not r.ok, "", SECTION)
            assert not r.ok

    def test_missing_gated_detector_blocks(self):
        dets = [{"name": n, "findings_count": 0} for n in _gate().GATED_DETECTORS[:-1]]
        r = _gate().evaluate(_audit(detectors=dets),
                             {"path": "t", "worst_exitstatus": 0, "resumen_ok": True,
                              "archivos_total": 1, "archivos_ok": 1,
                              "tests_collected": 300, "run_id": "r"}, {"path": "h"})
        check("detector ausente bloquea", not r.ok,
              str([c.name for c in r.checks if not c.passed]), SECTION)
        assert not r.ok

    def test_detector_with_findings_blocks(self):
        for det in _gate().GATED_DETECTORS:
            dets = [{"name": n, "findings_count": 3 if n == det else 0}
                    for n in _gate().GATED_DETECTORS]
            r = _gate().evaluate(_audit(detectors=dets),
                                 {"path": "t", "worst_exitstatus": 0, "resumen_ok": True,
                                  "archivos_total": 1, "archivos_ok": 1,
                                  "tests_collected": 300, "run_id": "r"}, {"path": "h"})
            check(f"{det}=3 bloquea", not r.ok, "", SECTION)
            assert not r.ok

    def test_bad_exitstatus_blocks(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 1,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": "r"}, {"path": "h"})
        check("exitstatus=1 bloquea", not r.ok, "", SECTION)
        assert not r.ok

    def test_few_tests_blocks(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 5,
                                        "run_id": "r"}, {"path": "h"})
        check("suite sospechosamente chica bloquea", not r.ok, "", SECTION)
        assert not r.ok

    def test_missing_run_id_blocks(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": None}, {"path": "h"})
        check("sin run_id bloquea", not r.ok, "", SECTION)
        assert not r.ok

    def test_missing_artifacts_block(self):
        r = _gate().evaluate({}, {}, {}, require_health=True)
        check("sin artefactos bloquea", not r.ok,
              str(len([c for c in r.checks if not c.passed])), SECTION)
        assert not r.ok

    def test_stale_health_blocks(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": "r"},
                             {"path": "h", "stale_hours": 99.0, "anomalias": False})
        check("health viejo bloquea", not r.ok, "", SECTION)
        assert not r.ok

    def test_health_anomalies_block(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": "r"},
                             {"path": "h", "stale_hours": 0.2, "anomalias": True})
        check("anomalías de health bloquean", not r.ok, "", SECTION)
        assert not r.ok

    def test_ci_mode_skips_health_when_absent(self):
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": "r"}, {}, require_health=False)
        skipped = [c for c in r.checks if c.skipped]
        check("modo CI saltea health (no lo finge)", bool(skipped),
              f"{len(skipped)} check(s) SKIP", SECTION)
        check("modo CI pasa sin health", r.ok, "", SECTION)
        assert r.ok and skipped

    def test_ci_mode_still_requires_health_if_it_exists(self):
        """Si hay health report, se evalúa igual en CI (no se ignora)."""
        r = _gate().evaluate(_audit(), {"path": "t", "worst_exitstatus": 0,
                                        "resumen_ok": True, "archivos_total": 1,
                                        "archivos_ok": 1, "tests_collected": 300,
                                        "run_id": "r"},
                             {"path": "h", "stale_hours": 500.0}, require_health=False)
        check("health presente se evalúa aunque sea CI", not r.ok, "", SECTION)
        assert not r.ok


class TestParsers:
    def test_parse_test_report_real_format(self, tmp_path):
        f = tmp_path / "batch.md"
        f.write_text(TESTS_OK, encoding="utf-8")
        got = _gate().parse_test_report(f)
        check("run_id parseado", got["run_id"] == "batch_20260101_000000",
              str(got["run_id"]), SECTION)
        check("exitstatus real parseado", got["worst_exitstatus"] == 0,
              str(got["worst_exitstatus"]), SECTION)
        check("archivos OK/total", (got["archivos_ok"], got["archivos_total"]) == (1, 1),
              f"{got['archivos_ok']}/{got['archivos_total']}", SECTION)
        check("tests recolectados", got["tests_collected"] == 300,
              str(got["tests_collected"]), SECTION)
        assert got["resumen_ok"]

    def test_parse_test_report_takes_worst_exitstatus(self, tmp_path):
        f = tmp_path / "batch.md"
        f.write_text(TESTS_OK + TESTS_OK.replace("**0**", "**1**"), encoding="utf-8")
        got = _gate().parse_test_report(f)
        check("el peor exitstatus manda", got["worst_exitstatus"] == 1,
              str(got["exitstatuses"]), SECTION)
        assert got["worst_exitstatus"] == 1

    def test_parse_missing_file_is_empty(self, tmp_path):
        got = _gate().parse_test_report(tmp_path / "no_existe.md")
        check("archivo inexistente → dict vacío", got.get("worst_exitstatus") is None,
              str(got), SECTION)
        assert got.get("worst_exitstatus") is None and got["path"]

    def test_parse_health_report_real_format(self, tmp_path):
        f = tmp_path / "h.md"
        f.write_text(HEALTH_OK, encoding="utf-8")
        got = _gate().parse_health_report(f)
        check("stale_hours parseado", got["stale_hours"] == 0.3,
              str(got["stale_hours"]), SECTION)
        check("sin anomalías detectado", got["anomalias"] is False,
              str(got["anomalias"]), SECTION)
        assert got["stale_hours"] == 0.3 and got["anomalias"] is False

    def test_parse_health_with_anomalies(self, tmp_path):
        f = tmp_path / "h.md"
        f.write_text("- **Vigencia:** ✅ reciente — último evento hace 1.0 h\n"
                     "- ❌ anomalía detectada: x\n", encoding="utf-8")
        got = _gate().parse_health_report(f)
        check("anomalías detectadas", got["anomalias"] is True, str(got), SECTION)
        assert got["anomalias"] is True

    def test_parse_audit_summary_missing(self, tmp_path):
        got = _gate().load_audit_summary(tmp_path / "nada.json")
        check("audit JSON inexistente → vacío", got == {}, str(got), SECTION)
        assert got == {}


class TestCli:
    def _run(self, audit, tests_text, health_text, tmp_path, extra=()):
        (tmp_path / "a.json").write_text(json.dumps(audit), encoding="utf-8")
        (tmp_path / "t.md").write_text(tests_text, encoding="utf-8")
        (tmp_path / "h.md").write_text(health_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "quality_gate.py"),
             "--audit-json", str(tmp_path / "a.json"),
             "--test-report", str(tmp_path / "t.md"),
             "--health-report", str(tmp_path / "h.md"), *extra],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    def test_cli_passes_when_all_good(self, tmp_path):
        p = self._run(_audit(), TESTS_OK, HEALTH_OK, tmp_path)
        check("CLI exit 0 con todo OK", p.returncode == 0,
              p.stdout.strip().splitlines()[-2] if p.stdout else p.stderr[:200],
              SECTION)
        assert p.returncode == 0, p.stdout + p.stderr

    def test_cli_blocks_and_reports_which_check(self, tmp_path):
        p = self._run(_audit(score=88), TESTS_OK, HEALTH_OK, tmp_path)
        check("CLI exit 1 con SCORE bajo", p.returncode == 1, str(p.returncode), SECTION)
        check("CLI dice qué bloqueó", "SCORE" in p.stdout and "BLOQUEA" in p.stdout,
              "BLOQUEA + SCORE en salida", SECTION)
        assert p.returncode == 1 and "SCORE" in p.stdout

    def test_cli_writes_json_summary(self, tmp_path):
        out = tmp_path / "gate.json"
        self._run(_audit(), TESTS_OK, HEALTH_OK, tmp_path, extra=("--json", str(out)))
        data = json.loads(out.read_text(encoding="utf-8"))
        check("--json escribe resumen", data.get("ok") is True,
              f"{len(data.get('checks', []))} checks", SECTION)
        assert data["ok"] is True and data["checks"]


class TestGateConsistency:
    def test_gated_detectors_exist_in_auditor_registry(self):
        """El gate no debe vigilar detectores que ya no existen."""
        from tools.auditor import DETECTORS
        registered = set(DETECTORS) if isinstance(DETECTORS, dict) else {
            getattr(d, "name", None) or getattr(type(d), "name", None)
            for d in DETECTORS}
        missing = [d for d in _gate().GATED_DETECTORS if d not in registered]
        check("detectores del gate registrados", not missing,
              f"ausentes: {missing}" if missing else
              f"{len(_gate().GATED_DETECTORS)}/{len(registered)} OK", SECTION)
        assert not missing

    def test_auditor_json_has_summary_block(self):
        """El gate depende de `summary` en el JSON del auditor."""
        from tools.auditor.aud_main import AxiomaAuditor
        src = inspect.getsource(AxiomaAuditor.generate_report)
        check("generate_report(json) incluye summary", "'summary'" in src,
              "clave 'summary' presente", SECTION)
        assert "'summary'" in src
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


class TestReportPruning:
    """El podador de reportes NO debe borrar la evidencia del gate.

    Bug real (v0.6.9v, encontrado por el propio gate): el glob
    `axioma_test_report_*` matcheaba también los consolidados de la tanda
    (`..._batch_<run_id>.md`) y los borraba, así que correr UN archivo de test
    después de la suite completa eliminaba el reporte con `run_id` y
    `pytest exitstatus` que el gate necesita.
    """

    def _mk(self, tmp_path, monkeypatch, names):
        from tests import axioma_report as ar
        monkeypatch.setattr(ar, "LOGS_DIR", tmp_path)
        files = []
        for i, n in enumerate(names):
            f = tmp_path / n
            f.write_text("x", encoding="utf-8")
            # mtime creciente: el último de la lista es el más nuevo
            import os
            os.utime(f, (1000 + i, 1000 + i))
            files.append(f)
        return ar, files

    def test_batch_report_survives_single_report_prune(self, tmp_path, monkeypatch):
        ar, files = self._mk(tmp_path, monkeypatch, [
            "axioma_test_report_batch_20260101_000000.md",
            "axioma_test_report_20260101_000001.md",
        ])
        ar._prune_old_reports()
        batch = tmp_path / "axioma_test_report_batch_20260101_000000.md"
        check("el reporte consolidado sobrevive", batch.exists(),
              str([p.name for p in tmp_path.iterdir()]), SECTION)
        assert batch.exists()

    def test_single_reports_still_pruned_to_one(self, tmp_path, monkeypatch):
        ar, files = self._mk(tmp_path, monkeypatch, [
            "axioma_test_report_20260101_000000.md",
            "axioma_test_report_20260101_000001.md",
            "axioma_test_report_20260101_000002.md",
        ])
        ar._prune_old_reports()
        left = sorted(p.name for p in tmp_path.iterdir())
        check("solo queda el reporte más nuevo", left == ["axioma_test_report_20260101_000002.md"],
              str(left), SECTION)
        assert len(left) == 1

    def test_batches_pruned_with_their_own_limit(self, tmp_path, monkeypatch):
        ar, files = self._mk(tmp_path, monkeypatch, [
            f"axioma_test_report_batch_2026010{i}_000000.md" for i in range(1, 6)
        ])
        ar._prune_old_reports()
        left = sorted(p.name for p in tmp_path.iterdir())
        tope = ar._prune_old_reports.__defaults__[1]   # keep_batches (hoy 2)
        check(f"consolidados podados a su propio tope ({tope})",
              len(left) == tope, str(left), SECTION)
        assert len(left) == tope


# ── Fusionado desde test_axioma_24_stats_frescura.py ──
import asyncio
import inspect
import time

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SECTION_2 = "24. Stats reales y frescura del health"


def _rs():
    from src.interfaces.web import request_stats
    return request_stats


def _routes():
    from src.interfaces.web import routes_extended
    return routes_extended


class TestRequestStats:
    def setup_method(self):
        _rs().reset()

    def test_counts_requests_and_latency(self):
        rs = _rs()
        rs.record_request("/api/v1/a", 200, 10.0)
        rs.record_request("/api/v1/a", 200, 30.0)
        rs.record_request("/api/v1/b", 500, 20.0)
        s = rs.stats()
        check("total real", s["total_requests"] == 3, str(s["total_requests"]), SECTION_2)
        check("latencia promedio real", s["avg_latency_ms"] == 20.0,
              str(s["avg_latency_ms"]), SECTION_2)
        check("conteo de errores", s["error_requests"] == 1, str(s["errors"] if "errors" in s else s["error_requests"]), SECTION_2)
        check("by_status", s["by_status"] == {"200": 2, "500": 1},
              str(s["by_status"]), SECTION_2)
        check("top_paths", s["top_paths"]["/api/v1/a"] == 2,
              str(s["top_paths"]), SECTION_2)
        assert s["total_requests"] == 3 and s["avg_latency_ms"] == 20.0

    def test_error_rate(self):
        rs = _rs()
        for _ in range(3):
            rs.record_request("/x", 200, 1.0)
        rs.record_request("/x", 404, 1.0)
        check("error_rate = 1/4", rs.stats()["error_rate"] == 0.25,
              str(rs.stats()["error_rate"]), SECTION_2)
        assert rs.stats()["error_rate"] == 0.25

    def test_empty_stats_are_zero_not_error(self):
        s = _rs().stats()
        check("sin requests → ceros coherentes",
              s["total_requests"] == 0 and s["avg_latency_ms"] == 0.0
              and s["error_rate"] == 0.0, str(s["total_requests"]), SECTION_2)
        assert s["total_requests"] == 0

    def test_uptime_is_real(self):
        up = _rs().uptime_seconds()
        check("uptime > 0 y plausible", 0 < up < 60 * 60 * 24 * 30, f"{up:.1f}s", SECTION_2)
        assert up > 0

    def test_slowest_path_tracked(self):
        rs = _rs()
        rs.record_request("/rapido", 200, 5.0)
        rs.record_request("/lento", 200, 900.0)
        s = rs.stats()
        check("path más lento registrado", s["slowest_path"] == "/lento"
              and s["max_latency_ms"] == 900.0, str(s["slowest_path"]), SECTION_2)
        assert s["slowest_path"] == "/lento"

    def test_path_tracking_is_bounded(self):
        rs = _rs()
        for i in range(rs._MAX_PATHS + 40):
            rs.record_request(f"/p/{i}", 200, 1.0)
        s = rs.stats(top_paths=1000)
        check(f"≤ {rs._MAX_PATHS} paths distintos",
              len(s["top_paths"]) <= rs._MAX_PATHS,
              f"{len(s['top_paths'])} paths", SECTION_2)
        assert len(s["top_paths"]) <= rs._MAX_PATHS

    def test_reset_clears(self):
        rs = _rs()
        rs.record_request("/x", 200, 1.0)
        rs.reset()
        check("reset deja todo en cero", rs.stats()["total_requests"] == 0,
              str(rs.stats()["total_requests"]), SECTION_2)
        assert rs.stats()["total_requests"] == 0

    def test_bad_input_does_not_raise(self):
        rs = _rs()
        for bad in ((None, None, None), ("/x", "no-int", "no-float")):
            try:
                rs.record_request(*bad)
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
            check(f"entrada inválida tolerada {bad[1]!r}", ok, "", SECTION_2)
            assert ok

    def test_stats_json_serializable(self):
        rs = _rs()
        rs.record_request("/x", 200, 1.0)
        try:
            json.dumps(rs.stats())
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check("stats serializables", ok, "json.dumps OK", SECTION_2)
        assert ok


class TestStatsEndpoint:
    def setup_method(self):
        _rs().reset()

    def test_endpoint_returns_real_data_not_zeros(self):
        rs = _rs()
        rs.record_request("/api/v1/chat", 200, 250.0)
        rs.record_request("/api/v1/health", 200, 50.0)
        got = asyncio.run(_routes().get_stats())
        check("total_requests real (no 0 hardcodeado)",
              got["total_requests"] == 2, str(got["total_requests"]), SECTION_2)
        check("avg_latency_ms real", got["avg_latency_ms"] == 150.0,
              str(got["avg_latency_ms"]), SECTION_2)
        check("uptime_seconds real", got["uptime_seconds"] > 0,
              str(got["uptime_seconds"]), SECTION_2)
        assert got["total_requests"] == 2 and got["uptime_seconds"] > 0

    def test_historical_keys_preserved(self):
        got = asyncio.run(_routes().get_stats())
        for key in ("total_requests", "avg_latency_ms", "uptime_seconds",
                    "router_metrics"):
            check(f"clave histórica presente: {key}", key in got, "", SECTION_2)
            assert key in got

    def test_storage_block_reports_real_sizes(self):
        got = asyncio.run(_routes().get_stats())
        st = got["storage"]
        check("storage con tamaños reales",
              isinstance(st.get("sqlite_db_mb"), (int, float))
              and st["sqlite_db_mb"] > 0, str(st), SECTION_2)
        check("storage incluye data_dir y logs",
              "data_dir" in st and "logs_dir_mb" in st, str(sorted(st)), SECTION_2)
        assert st["sqlite_db_mb"] > 0

    def test_degradation_and_feedback_blocks(self):
        got = asyncio.run(_routes().get_stats())
        check("bloque degradation", "contract" in got["degradation"],
              str(sorted(got["degradation"])[:5]), SECTION_2)
        check("bloque feedback con routed", "routed" in got["feedback"],
              str(got["feedback"]), SECTION_2)
        assert "contract" in got["degradation"] and "routed" in got["feedback"]

    def test_payload_json_serializable(self):
        got = asyncio.run(_routes().get_stats())
        try:
            json.dumps(got)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check("payload /stats serializable", ok, "json.dumps OK", SECTION_2)
        assert ok

    def test_degrades_if_degradation_module_fails(self, monkeypatch):
        import src.utils.degradation as deg

        def _boom():
            raise RuntimeError("simulado")
        monkeypatch.setattr(deg, "degradation_stats", _boom)
        got = asyncio.run(_routes().get_stats())
        check("degradación de subsistema no rompe /stats",
              got["degradation"].get("available") is False,
              str(got["degradation"]), SECTION_2)
        assert got["degradation"]["available"] is False

    def test_degrades_if_feedback_loop_fails(self, monkeypatch):
        import src.core.feedback_loop as fl

        async def _boom():
            raise RuntimeError("simulado")
        monkeypatch.setattr(fl, "get_feedback_loop", _boom)
        got = asyncio.run(_routes().get_stats())
        check("feedback caído no rompe /stats",
              got["feedback"].get("available") is False,
              str(got["feedback"]), SECTION_2)
        assert got["feedback"]["available"] is False


class TestMiddlewareWiring:
    """Cableado verificado sobre OBJETOS REALES (no grep del fuente).

    `build_api_app()` construye la app del server; el test la inspecciona y la
    ejercita con un cliente HTTP. Si alguien mueve el middleware de archivo, el
    test sigue pasando mientras siga cableado (que es lo que importa).
    """

    def test_server_builds_app_with_tracking_middleware(self):
        from src.interfaces.web.server import build_api_app
        from src.interfaces.web.request_stats import track_request_stats
        app = build_api_app()
        check("build_api_app devuelve una app", app is not None, str(app), SECTION_2)
        assert app is not None
        dispatch = [getattr(m, "kwargs", {}).get("dispatch")
                    for m in app.user_middleware]
        check("el conteo de requests está cableado (identidad de objeto)",
              track_request_stats in dispatch, str(dispatch), SECTION_2)
        assert track_request_stats in dispatch

    def test_critical_endpoints_registered(self):
        from src.interfaces.web.server import build_api_app
        app = build_api_app()
        # ✅ 2026-10-08: dirección PÚBLICA (`/api` del montaje + `/v1` del router); antes miraba sólo el
        # router y pasaba aunque la pública estuviera duplicada.
        paths = [f"/api{getattr(r, 'path', '')}" if str(getattr(r, "path", "")).startswith("/v1")
                 else getattr(r, "path", "") for r in app.routes]
        for ep in ("/api/v1/health", "/api/v1/chat", "/api/v1/stats",
                   "/api/v1/metrics/degradation", "/api/v1/feedback/stats"):
            check(f"endpoint crítico presente {ep}", ep in paths, str(len(paths)), SECTION_2)
            assert ep in paths

    def test_middleware_counts_through_real_http_client(self):
        """Ejercita el middleware REAL sobre la app REAL (sin threadpool del server)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.interfaces.web.request_stats import (
            track_request_stats, stats, reset)

        app = FastAPI()
        app.middleware("http")(track_request_stats)

        @app.get("/ping")
        async def _ping():
            return {"ok": True}

        reset()
        client = TestClient(app)
        client.get("/ping")
        client.get("/ping")
        s = stats()
        check("el middleware cuenta 2 requests reales", s["total_requests"] == 2,
              str(s["total_requests"]), SECTION_2)
        check("cuenta por path", s["top_paths"].get("/ping") == 2,
              str(s["top_paths"]), SECTION_2)
        assert s["total_requests"] == 2

    def test_middleware_survives_record_failure(self, monkeypatch):
        """Si el registro falla, el request NO debe romperse."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.interfaces.web import request_stats as rs

        def _boom(*_a, **_k):
            raise RuntimeError("simulado")
        monkeypatch.setattr(rs, "record_request", _boom)

        app = FastAPI()
        app.middleware("http")(rs.track_request_stats)

        @app.get("/x")
        async def _x():
            return {"ok": True}
        resp = TestClient(app).get("/x")
        check("request OK aunque falle el registro", resp.status_code == 200,
              str(resp.status_code), SECTION_2)
        assert resp.status_code == 200


def _log_sintetico(tmp_path, nombre="reg_error.txt"):
    """Log sintético para los tests del health report (ISSUE-089).

    ✅ v0.7.3 (2026-09-28): estos tests llamaban al tool **sin logs**, así que
    dependían de que existiera `logs/reg_error.txt` — un artefacto de RUNTIME que
    solo existe en la máquina del desarrollador. En CI (checkout limpio) el tool
    devolvía `None` y **7 tests fallaban**: la CI quedaba roja por un test mal
    aislado (R5: determinismo; R17: nada de datos del usuario).
    """
    p = tmp_path / nombre
    p.write_text(
        "[2026-01-01 10:00:00.123456] [INFO    ] [DISPATCHER ] Inicio de prueba | {}\n"
        "[2026-01-01 10:00:01.234567] [ERROR   ] [DISPATCHER ] fallo simulado de prueba\n"
        "[2026-01-01 10:00:02.345678] [WARNING ] [ROUTER     ] aviso simulado\n",
        encoding="utf-8")
    return p


class TestHealthRefreshAutomation:
    def test_generate_function_is_reusable(self, tmp_path):
        from tools.log_health_report import generate_health_report
        out = tmp_path / "h.md"
        path = generate_health_report(logs=[str(_log_sintetico(tmp_path))],
                                      output=str(out), write_sidecar=False)
        check("generate_health_report escribe el reporte",
              path is not None and Path(path).exists(), str(path), SECTION_2)
        assert path is not None and Path(path).exists()

    def test_generate_does_not_use_argv(self, tmp_path, monkeypatch):
        """No debe parsear `sys.argv` (se llama desde la app)."""
        from tools import log_health_report as h
        monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", "9999"])
        out = tmp_path / "h2.md"
        try:
            path = h.generate_health_report(logs=[str(_log_sintetico(tmp_path))],
                                            output=str(out), write_sidecar=False)
            ok = path is not None
        except SystemExit:  # pragma: no cover — argparse habría salido
            ok = False
        check("no cae en argparse de main()", ok, str(out), SECTION_2)
        assert ok

    def test_missing_logs_returns_none(self, tmp_path):
        from tools.log_health_report import generate_health_report
        got = generate_health_report(logs=[str(tmp_path / "no_existe.txt")],
                                     write_sidecar=False)
        check("sin logs → None (no excepción)", got is None, str(got), SECTION_2)
        assert got is None

    def test_cli_still_works(self):
        """La CLI debe seguir funcionando después del refactor."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log = _log_sintetico(Path(td))
            p = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "tools" / "log_health_report.py"),
                 "--logs", str(log), "-o", str(Path(td) / "axioma_health_cli.md")],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=180)
        check("CLI exit 0", p.returncode == 0, p.stderr.strip()[-120:], SECTION_2)
        check("CLI escribe el reporte", "axioma_health_" in p.stderr,
              p.stderr.strip()[-80:], SECTION_2)
        assert p.returncode == 0

    def test_startup_hook_refreshes_report(self, tmp_path):
        """El hook de arranque debe EXISTIR como función reutilizable y generar
        el reporte sin bloquear (se ejercita de verdad, no se grepea)."""
        from tools.log_health_report import refresh_health_report_async
        path = asyncio.run(refresh_health_report_async(
            logs=[str(_log_sintetico(tmp_path))],
            output=str(tmp_path / "axioma_health_refresh.md")))
        check("refresh async genera un reporte", path is not None and path.exists(),
              str(path), SECTION_2)
        assert path is not None and path.exists()

    def test_main_calls_the_refresh_at_startup(self):
        """`main.py` dispara el refresh en su arranque.

        Se verifica con AST (estructura), no con grep de texto: si el import se
        mueve de línea o se reformatea, sigue detectándose.
        """
        import ast
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
        importados = set()
        llamados = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    importados.add(alias.asname or alias.name)
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    llamados.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    llamados.add(fn.attr)
        check("main.py importa el refresh del health report",
              "refresh_health_report_async" in importados, str(sorted(importados))[:120],
              SECTION_2)
        check("main.py lo llama en el arranque",
              "refresh_health_report_async" in llamados, str(sorted(llamados))[:120],
              SECTION_2)
        assert "refresh_health_report_async" in importados
        assert "refresh_health_report_async" in llamados

    def test_refresh_does_not_use_argv(self, tmp_path, monkeypatch):
        """El refresh se llama desde la app: no debe parsear `sys.argv`."""
        from tools import log_health_report as h
        monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", "9999"])
        try:
            path = asyncio.run(h.refresh_health_report_async(
                logs=[str(_log_sintetico(tmp_path))],
                output=str(tmp_path / "h.md")))
            ok = path is not None
        except SystemExit:  # pragma: no cover — argparse habría salido
            ok = False
        check("refresh async no cae en argparse", ok, str(tmp_path), SECTION_2)
        assert ok

    def test_freshness_is_measurable(self, tmp_path):
        """La frescura debe ser EVALUABLE en el reporte (no una promesa)."""
        from tools.log_health_report import generate_health_report
        path = generate_health_report(logs=[str(_log_sintetico(tmp_path))],
                                      output=str(tmp_path / "h.md"),
                                      write_sidecar=False)
        text = Path(path).read_text(encoding="utf-8")
        check("el reporte declara su vigencia",
              "Vigencia" in text and " h" in text, "línea de vigencia presente",
              SECTION_2)
        assert "Vigencia" in text


class TestHealthReportPruning:
    """La frescura automática no debe acumular archivos sin límite."""

    def test_prune_keeps_last_n(self, tmp_path):
        from tools.log_health_report import _prune_health_reports
        import os
        for i in range(9):
            f = tmp_path / f"axioma_health_2026010{i}_000000.md"
            f.write_text("x", encoding="utf-8")
            os.utime(f, (1000 + i, 1000 + i))
        removed = _prune_health_reports(keep=5, logs_dir=tmp_path)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("se conservan solo los 5 más nuevos", len(left) == 5, str(left), SECTION_2)
        check("informa cuántos borró", removed == 4, str(removed), SECTION_2)
        assert len(left) == 5 and removed == 4

    def test_prune_ignores_other_files(self, tmp_path):
        from tools.log_health_report import _prune_health_reports
        (tmp_path / "axioma_audit_report_20260101.md").write_text("x")
        (tmp_path / "sidebar_0001.log").write_text("x")
        removed = _prune_health_reports(keep=5, logs_dir=tmp_path)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("no toca otros archivos de logs", len(left) == 2 and removed == 0,
              str(left), SECTION_2)
        assert removed == 0 and len(left) == 2

    def test_generate_prunes_automatically(self, tmp_path):
        from tools.log_health_report import generate_health_report
        import os
        for i in range(8):
            f = tmp_path / f"axioma_health_2026010{i}_000000.md"
            f.write_text("x", encoding="utf-8")
            os.utime(f, (1000 + i, 1000 + i))
        path = generate_health_report(logs=[str(_log_sintetico(tmp_path))],
                                      output=str(tmp_path / "axioma_health_new.md"),
                                      write_sidecar=False)
        left = sorted(p.name for p in tmp_path.iterdir()
                      if p.name.startswith("axioma_health_"))
        check("generate_health_report poda solo", len(left) <= 5, str(len(left)), SECTION_2)
        assert path is not None and len(left) <= 5


class TestConversationExportButtons:
    """ISSUE-055: el diálogo de export ofrece también la CONVERSACIÓN."""

    def test_export_dialog_exposes_conversation_export(self):
        from src.interfaces.components import export_dialog as ed
        check("función de conversación exportable",
              callable(getattr(ed, "export_current_conversation", None)),
              "presente", SECTION_2)
        check("etiquetas para md/html/json",
              set(ed._CONV_LABEL) == {"md", "html", "json"},
              str(sorted(ed._CONV_LABEL)), SECTION_2)
        assert callable(getattr(ed, "export_current_conversation", None))
        assert set(ed._CONV_LABEL) == {"md", "html", "json"}

    def test_dialog_offers_both_exports_and_no_timer(self):
        from src.interfaces.components import export_dialog as ed
        src = inspect.getsource(ed._build_export_dialog)
        check("botones de investigación", "_do_export" in src, "", SECTION_2)
        check("botones de conversación", "_do_conversation" in src, "", SECTION_2)
        check("sin ui.timer (R7)", "ui.timer" not in src, "", SECTION_2)
        assert "_do_conversation" in src


# ═══════════════════════════════════════════════════════════════
# PODA DE REPORTES DE AUDITORÍA (ISSUE-058)
# ═══════════════════════════════════════════════════════════════
# El auditor escribía un `axioma_audit_report_<ts>.md` por corrida sin limpiar
# (51 archivos, ~5 MB en logs/).
class TestAuditReportPruning:
    def _mk(self, tmp_path, n):
        import os
        files = []
        for i in range(n):
            # ⚠️ índice con 2 dígitos: con `{i}` los nombres 202601010/…011
            # rompían el orden ALFABÉTICO y el test comparaba mal (el pruner usa
            # mtime, que es lo correcto).
            f = tmp_path / f"axioma_audit_report_20260101_{i:06d}.md"
            f.write_text("x", encoding="utf-8")
            os.utime(f, (1000 + i, 1000 + i))
            files.append(f)
        return files

    def test_keeps_only_the_last_n(self, tmp_path):
        from tools.auditor.aud_main import _prune_old_audit_reports
        self._mk(tmp_path, 12)
        removed = _prune_old_audit_reports(tmp_path, keep=5)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("conserva 5 reportes", len(left) == 5, str(len(left)), SECTION)
        check("informa cuántos borró", removed == 7, str(removed), SECTION)
        check("los que quedan son los más nuevos por mtime",
              left[-1].endswith("_000011.md"), left[-1], SECTION)
        assert len(left) == 5 and removed == 7

    def test_does_not_touch_other_log_files(self, tmp_path):
        from tools.auditor.aud_main import _prune_old_audit_reports
        (tmp_path / "axioma_health_20260101_000000.md").write_text("x")
        (tmp_path / "axioma_test_report_batch_20260101_000000.md").write_text("x")
        (tmp_path / "reg_error.txt").write_text("x")
        self._mk(tmp_path, 8)
        _prune_old_audit_reports(tmp_path, keep=5)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("no toca otros archivos de logs", len(left) == 8,
              str(left[:4]), SECTION)
        assert len(left) == 8

    def test_fewer_reports_than_limit_is_noop(self, tmp_path):
        from tools.auditor.aud_main import _prune_old_audit_reports
        self._mk(tmp_path, 2)
        removed = _prune_old_audit_reports(tmp_path, keep=5)
        check("con menos reportes que el tope no borra nada", removed == 0,
              str(removed), SECTION)
        assert removed == 0


# ═══════════════════════════════════════════════════════════════
# HELPER COMPARTIDO DE PODA (tools/detailed_logger.prune_reports)
# ═══════════════════════════════════════════════════════════════
# Limpieza ÚNICA de reportes en logs/: la usan el auditor, la eval de regresión,
# las sondas de visión y los reportes de test/health.
class TestPruneReportsHelper:
    def _mk(self, tmp_path, patron, n):
        import os
        for i in range(n):
            f = tmp_path / patron.replace("*", f"20260101_{i:06d}")
            f.write_text("x", encoding="utf-8")
            os.utime(f, (1000 + i, 1000 + i))
        return tmp_path

    def test_keeps_only_the_most_recent(self, tmp_path):
        from tools.detailed_logger import prune_reports
        self._mk(tmp_path, "familia_*.md", 6)
        removed = prune_reports("familia_*.md", keep=2, logs_dir=tmp_path)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("conserva 2", len(left) == 2, str(left), SECTION)
        check("borró 4", removed == 4, str(removed), SECTION)
        check("los que quedan son los más nuevos",
              left[-1].endswith("_000005.md"), left[-1], SECTION)
        assert len(left) == 2 and removed == 4

    def test_protect_file_is_never_deleted(self, tmp_path):
        """El reporte recién escrito no se borra aunque el reloj venga raro."""
        from tools.detailed_logger import prune_reports
        self._mk(tmp_path, "familia_*.md", 4)
        recien = tmp_path / "familia_recien.md"
        recien.write_text("x", encoding="utf-8")
        import os
        os.utime(recien, (1, 1))          # el más viejo por mtime
        prune_reports("familia_*.md", keep=2, logs_dir=tmp_path, protect=recien)
        check("el archivo protegido sobrevive", recien.exists(), "existe", SECTION)
        assert recien.exists()

    def test_does_not_touch_other_families(self, tmp_path):
        from tools.detailed_logger import prune_reports
        self._mk(tmp_path, "familia_*.md", 3)
        (tmp_path / "otra_familia_1.md").write_text("x")
        (tmp_path / "reg_error.txt").write_text("x")
        prune_reports("familia_*.md", keep=1, logs_dir=tmp_path)
        left = sorted(p.name for p in tmp_path.iterdir())
        check("no toca otras familias", "otra_familia_1.md" in left
              and "reg_error.txt" in left, str(left), SECTION)
        assert "otra_familia_1.md" in left and "reg_error.txt" in left

    def test_no_match_and_bad_dir_do_not_raise(self, tmp_path):
        from tools.detailed_logger import prune_reports
        check("sin coincidencias → 0", prune_reports("nada_*.md", logs_dir=tmp_path) == 0,
              "", SECTION)
        check("directorio inexistente → 0 (no lanza)",
              prune_reports("x_*.md", logs_dir=tmp_path / "no_existe") == 0, "", SECTION)
        assert prune_reports("nada_*.md", logs_dir=tmp_path) == 0


def test_el_chequeo_del_entorno_dice_la_verdad(monkeypatch):
    """`ISSUE-146`: con Ollama caído, `--check` decía "AXIOMA listo" y salía con 0.

    Medido el 2026-10-04 (Fase 0 del plan de contenedores): el usuario pidió que el sistema
    diga qué falta ANTES de que funcione, y el veredicto decía que todo estaba bien.
    """
    import importlib
    import src.llm.client as cliente
    cli = importlib.import_module("src.interfaces.cli.main")

    class _Consola:                      # no imprime: sólo junta lo que se dijo
        def __init__(self):
            self.dichos = []
        def __getattr__(self, _n):
            return lambda *a, **_k: self.dichos.append(str(a))

    monkeypatch.setattr(cliente.OllamaClient, "health_check", lambda self: False)
    caido = _Consola()
    assert cli.cmd_check(caido) != 0, "con Ollama caído no puede decir que está listo"
    assert any("Ollama no responde" in d for d in caido.dichos), caido.dichos

    monkeypatch.setattr(cliente.OllamaClient, "health_check", lambda self: True)
    monkeypatch.setattr(cliente.OllamaClient, "list_models",
                        lambda self: [{"name": "qwen3:8b"}])
    sano = _Consola()
    assert cli.cmd_check(sano) == 0, "con Ollama y el modelo de chat, el entorno está listo"

