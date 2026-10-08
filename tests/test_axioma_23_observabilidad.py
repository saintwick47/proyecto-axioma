import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
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
SECTION = "23. Observabilidad (degradación + feedback)"
_LOG = logging.getLogger("axioma.test.observabilidad")


def _deg():
    from src.utils import degradation as d
    return d


def _comp():
    from src.core import feedback_loop_components as c
    return c


def _rec(signal, feedback_type=None, task="general_chat", agent="WebDispatcher"):
    c = _comp()
    return c.FeedbackRecord(
        id="x", session_id="s", task_type=task, agent_role=agent,
        query="q", response="r", timestamp=datetime.now(timezone.utc),
        feedback_type=feedback_type or c.FeedbackType.IMPLICIT,
        signal_or_rating=signal)


class TestDegradationMetrics:
    def setup_method(self):
        _deg().reset_degradation_stats()

    def test_counts_contract_and_soft_separately(self):
        d = _deg()
        d.log_degraded(_LOG, TypeError("firma"), "a.b")
        d.log_degraded(_LOG, AttributeError("attr"), "a.c")
        d.log_degraded(_LOG, TimeoutError("red"), "a.d")
        s = d.degradation_stats()
        check("contrato=2, blando=1", (s["contract"], s["soft"]) == (2, 1),
              f"contract={s['contract']} soft={s['soft']}", SECTION)
        assert (s["contract"], s["soft"]) == (2, 1)
        assert s["total"] == 3

    def test_breakdown_by_exception_and_context(self):
        d = _deg()
        d.log_degraded(_LOG, TypeError("a"), "mod.uno")
        d.log_degraded(_LOG, TypeError("b"), "mod.dos")
        d.log_degraded(_LOG, KeyError("c"), "mod.uno")
        s = d.degradation_stats()
        check("by_exception", s["by_exception"] == {"TypeError": 2, "KeyError": 1},
              str(s["by_exception"]), SECTION)
        check("by_context", s["by_context"] == {"mod.uno": 2, "mod.dos": 1},
              str(s["by_context"]), SECTION)
        assert s["by_exception"]["TypeError"] == 2

    def test_contract_error_in_critical_route_is_alert(self):
        d = _deg()
        d.log_degraded(_LOG, TypeError("x"), "QualityMonitor._qm_record")
        s = d.degradation_stats()
        check("alerta por ruta crítica (contrato)",
              s["hay_alerta"] and s["criticos_en_rutas_clave"] == 1,
              f"alertas={s['alertas']}", SECTION)
        check("la alerta nombra la ruta",
              any("quality" in a for a in s["alertas"]), str(s["alertas"]), SECTION)
        assert s["hay_alerta"] is True

    def test_soft_error_in_critical_route_is_not_alert(self):
        """Un timeout en memory es degradación esperada, no bug de integración."""
        d = _deg()
        d.log_degraded(_LOG, TimeoutError("tarde"), "MemoryGateway.fetch")
        s = d.degradation_stats()
        check("sin alerta por degradación blanda",
              s["hay_alerta"] is False and s["criticos_en_rutas_clave"] == 0,
              str(s["alertas"]), SECTION)
        assert s["hay_alerta"] is False

    def test_all_critical_routes_are_detected(self):
        d = _deg()
        for ctx in ("dispatcher.process", "MemoryGateway.store",
                    "ValidatorAgent.check", "feedback_loop.record",
                    "quality_monitor.score"):
            d.reset_degradation_stats()
            d.log_degraded(_LOG, NameError("x"), ctx)
            s = d.degradation_stats()
            check(f"ruta crítica detectada: {ctx}", s["hay_alerta"] is True,
                  f"by_route={s['by_route']}", SECTION)
            assert s["hay_alerta"] is True

    def test_unknown_context_goes_to_otras(self):
        d = _deg()
        d.log_degraded(_LOG, ValueError("x"), "modulo.cualquiera")
        s = d.degradation_stats()
        check("contexto no crítico → 'otras'", s["by_route"] == {"otras": 1},
              str(s["by_route"]), SECTION)
        assert s["by_route"] == {"otras": 1}

    def test_reset_clears_everything(self):
        d = _deg()
        d.log_degraded(_LOG, TypeError("x"), "dispatcher.process")
        d.reset_degradation_stats()
        s = d.degradation_stats()
        check("reset deja todo en cero",
              s["total"] == 0 and not s["by_route"] and s["hay_alerta"] is False,
              str(s["total"]), SECTION)
        assert s["total"] == 0

    def test_stats_are_json_serializable(self):
        d = _deg()
        d.log_degraded(_LOG, TypeError("x"), "dispatcher.process")
        try:
            json.dumps(d.degradation_stats())
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            logging.getLogger(__name__).debug(f"[test] {e}")
        check("stats serializables a JSON", ok, "json.dumps OK", SECTION)
        assert ok

    def test_context_key_cap_is_bounded(self):
        """No debe crecer sin límite: contextos únicos se agrupan en '<otros>'."""
        d = _deg()
        for i in range(d._STATS_MAX_KEYS + 50):
            d.log_degraded(_LOG, ValueError("x"), f"modulo_{i}.f")
        s = d.degradation_stats()
        check(f"≤ {d._STATS_MAX_KEYS} claves de contexto",
              len(s["by_context"]) <= d._STATS_MAX_KEYS,
              f"{len(s['by_context'])} claves", SECTION)
        assert len(s["by_context"]) <= d._STATS_MAX_KEYS

    def test_log_degraded_still_returns_contract_flag(self):
        d = _deg()
        check("log_degraded retorna True para contrato",
              d.log_degraded(_LOG, TypeError("x"), "a.b") is True, "", SECTION)
        check("log_degraded retorna False para blando",
              d.log_degraded(_LOG, OSError("x"), "a.b") is False, "", SECTION)
        assert d.is_contract_error(TypeError()) and not d.is_contract_error(OSError())


class TestSignalNormalization:
    def test_normalized_signal_accepts_three_forms(self):
        c = _comp()
        for raw in (c.ImplicitSignal.ROUTED, "ImplicitSignal.ROUTED", "routed",
                    "ROUTED"):
            got = _rec(raw).normalized_signal
            check(f"normaliza {raw!r}", got == "routed", got, SECTION)
            assert got == "routed"

    def test_explicit_rating_normalization(self):
        c = _comp()
        r = _rec(c.ExplicitRating.GOOD, feedback_type=c.FeedbackType.EXPLICIT)
        check("rating explícito normalizado a su valor (GOOD → '4')",
              r.normalized_signal == "4", r.normalized_signal, SECTION)
        check("GOOD sigue siendo positivo", r.is_positive is True,
              str(r.is_positive), SECTION)
        assert r.normalized_signal == "4" and r.is_positive is True

    def test_to_dict_emits_canonical_value_not_enum_repr(self):
        c = _comp()
        got = _rec(c.ImplicitSignal.ROUTED).to_dict()["signal_or_rating"]
        check("to_dict emite 'routed' (no 'ImplicitSignal.ROUTED')",
              got == "routed", got, SECTION)
        assert got == "routed"

    def test_classification_survives_db_roundtrip(self):
        """Regresión: al recargar de la DB la señal llega como STRING y la
        clasificación comparaba contra el ENUM → devolvía None en silencio."""
        c = _comp()
        copy_str = _rec("ImplicitSignal.COPY_ACTION")
        requery_str = _rec("ImplicitSignal.RE_QUERY")
        check("COPY_ACTION string sigue siendo positivo",
              copy_str.is_positive is True, str(copy_str.is_positive), SECTION)
        check("RE_QUERY string sigue siendo negativo",
              requery_str.is_negative is True, str(requery_str.is_negative), SECTION)
        assert copy_str.is_positive is True and requery_str.is_negative is True

    def test_routed_is_neutral(self):
        """ROUTED mide ruteo: no es positivo ni negativo."""
        c = _comp()
        r = _rec(c.ImplicitSignal.ROUTED)
        check("ROUTED neutro", r.is_positive is None and r.is_negative is None,
              f"pos={r.is_positive} neg={r.is_negative}", SECTION)
        assert r.is_positive is None and r.is_negative is None


class TestFeedbackBreakdown:
    _N = {"i": 0}

    def _loop(self, tmp_path):
        """Loop HERMÉTICO: sesión única + JSONL en tmp (no carga datos reales).

        ⚠️ `FeedbackLoop(persist_path=None)` NO es "sin persistencia": cae al
        path por defecto (`data/feedback/<sesión>.jsonl`) y **carga** los
        registros que ya existan. Con eso el desglose mediría datos de corridas
        anteriores (pasó en la primera versión de este test).
        """
        from src.core.feedback_loop import FeedbackLoop
        TestFeedbackBreakdown._N["i"] += 1
        sid = f"obs_test_{TestFeedbackBreakdown._N['i']}"
        return FeedbackLoop(session_id=sid, persist_path=tmp_path / f"{sid}.jsonl",
                            db_path=str(tmp_path / f"{sid}.db"))

    def test_breakdown_counts_routed_by_task_and_agent(self, tmp_path):
        c = _comp()
        loop = self._loop(tmp_path)
        try:
            asyncio.run(loop.record_implicit("obs_test", "general_chat",
                                             "WebDispatcher", "q", "r",
                                             c.ImplicitSignal.ROUTED))
            asyncio.run(loop.record_implicit("obs_test", "code_generation",
                                             "CoderAgent", "q2", "r2",
                                             c.ImplicitSignal.ROUTED))
            got = asyncio.run(loop.get_signal_breakdown())
        finally:
            loop2 = loop
            try:
                asyncio.run(loop2.close()) if hasattr(loop2, "close") else None
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[test] close: {_d2e}")
        check("routed contado", got["routed"] == 2, str(got["por_senal"]), SECTION)
        check("routed por tarea", got["routed_por_tarea"].get("general_chat") == 1,
              str(got["routed_por_tarea"]), SECTION)
        check("desglose por agente", got["por_agente"].get("CoderAgent") == 1,
              str(got["por_agente"]), SECTION)
        assert got["routed"] == 2
        assert "ImplicitSignal." not in json.dumps(got["por_senal"])

    def test_breakdown_empty_loop_is_safe(self, tmp_path):
        loop = self._loop(tmp_path)
        got = asyncio.run(loop.get_signal_breakdown())
        check("loop vacío no rompe", got["total_implicitos"] == 0
              and got["tasa_exito_implicita"] == 0.0, str(got), SECTION)
        assert got["routed"] == 0

    def test_breakdown_is_json_serializable(self, tmp_path):
        c = _comp()
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_implicit("obs_test", "general_chat",
                                         "WebDispatcher", "q", "r",
                                         c.ImplicitSignal.ROUTED))
        got = asyncio.run(loop.get_signal_breakdown())
        try:
            json.dumps(got)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            logging.getLogger(__name__).debug(f"[test] {e}")
        check("breakdown serializable", ok, "json.dumps OK", SECTION)
        assert ok


class TestObservabilityEndpoints:
    def _routes(self):
        from src.interfaces.web import routes_extended as r
        return r

    def test_endpoints_registered(self):
        paths = [f"/api{getattr(r, 'path', '')}" for r in self._routes().router.routes]
        for ep in ("/api/v1/metrics/degradation", "/api/v1/feedback/stats"):
            check(f"endpoint registrado {ep}", ep in paths, str(len(paths)), SECTION)
            assert ep in paths

    def test_degradation_endpoint_payload(self):
        d = _deg()
        d.reset_degradation_stats()
        d.log_degraded(_LOG, TypeError("x"), "QualityMonitor._qm_record")
        got = asyncio.run(self._routes().get_degradation_metrics())
        check("payload con contadores y alerta",
              got["contract"] == 1 and got["hay_alerta"] is True
              and "duration_ms" in got, str(sorted(got)[:6]), SECTION)
        assert got["contract"] == 1 and got["hay_alerta"] is True

    def test_feedback_endpoint_payload(self):
        got = asyncio.run(self._routes().get_feedback_stats())
        check("payload con global + signals",
              got.get("available") is True and "global" in got and "signals" in got,
              str(sorted(got)), SECTION)
        assert "routed" in got["signals"]

    def test_feedback_endpoint_degrades_without_loop(self, monkeypatch):
        """Sin FeedbackLoop debe responder `available: False`, no un 500."""
        import src.core.feedback_loop as fl

        async def _none():
            return None
        monkeypatch.setattr(fl, "get_feedback_loop", _none)
        got = asyncio.run(self._routes().get_feedback_stats())
        check("degrada sin loop", got.get("available") is False, str(got), SECTION)
        assert got["available"] is False

    def test_endpoints_are_json_serializable(self):
        payloads = [asyncio.run(self._routes().get_degradation_metrics()),
                    asyncio.run(self._routes().get_feedback_stats())]
        try:
            json.dumps(payloads)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            logging.getLogger(__name__).debug(f"[test] {e}")
        check("respuestas JSON-serializables", ok, "json.dumps OK", SECTION)
        assert ok
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── Fusionado desde test_axioma_26_salud_badge.py ──
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SECTION_2 = "26. Badge de salud (ISSUE-053)"


def _badge():
    from src.interfaces.components import health_badge
    return health_badge


class TestHealthSnapshot:
    def test_healthy_when_nothing_wrong(self):
        snap = _badge().health_snapshot({"contract": 0, "soft": 12},
                                        {"total_requests": 30, "error_rate": 0.03})
        check("estado ok", snap["estado"] == "ok", snap["estado"], SECTION_2)
        check("emoji verde", snap["emoji"] == "🟢", snap["emoji"], SECTION_2)
        check("sin motivos", not snap["motivos"], str(snap["motivos"]), SECTION_2)
        assert snap["estado"] == "ok" and snap["emoji"] == "🟢"

    def test_contract_error_in_critical_route_is_red(self):
        snap = _badge().health_snapshot(
            {"contract": 3, "criticos_en_rutas_clave": 3}, {})
        check("estado error", snap["estado"] == "error", snap["estado"], SECTION_2)
        check("emoji rojo", snap["emoji"] == "🔴", snap["emoji"], SECTION_2)
        check("motivo menciona rutas críticas",
              "rutas críticas" in snap["motivos"][0], str(snap["motivos"]), SECTION_2)
        assert snap["estado"] == "error"

    def test_contract_error_outside_critical_is_yellow(self):
        snap = _badge().health_snapshot(
            {"contract": 2, "criticos_en_rutas_clave": 0}, {})
        check("estado warn", snap["estado"] == "warn", snap["estado"], SECTION_2)
        check("emoji amarillo", snap["emoji"] == "🟡", snap["emoji"], SECTION_2)
        assert snap["estado"] == "warn"

    def test_high_api_error_rate_warns(self):
        snap = _badge().health_snapshot(
            {}, {"total_requests": 20, "error_rate": 0.25, "error_requests": 5})
        check("tasa de error alta → warn", snap["estado"] == "warn",
              str(snap["motivos"]), SECTION_2)
        check("el motivo cita el porcentaje", "25%" in snap["motivos"][0],
              snap["motivos"][0], SECTION_2)
        assert snap["estado"] == "warn"

    def test_error_rate_ignored_with_few_requests(self):
        """Con 2 requests un 50% de error no es señal de nada."""
        snap = _badge().health_snapshot({}, {"total_requests": 2, "error_rate": 0.5})
        check("pocos requests → no alerta", snap["estado"] == "ok",
              str(snap["motivos"]), SECTION_2)
        assert snap["estado"] == "ok"

    def test_negative_feedback_warns(self):
        snap = _badge().health_snapshot({}, {"total_requests": 1},
                                        {"negative_recent": 4})
        check("feedback negativo repetido → warn", snap["estado"] == "warn",
              str(snap["motivos"]), SECTION_2)
        assert snap["estado"] == "warn"

    def test_soft_degradations_alone_do_not_alert(self):
        """Degradación esperada (red/timeout) no es una incidencia."""
        snap = _badge().health_snapshot({"contract": 0, "soft": 99}, {})
        check("solo degradaciones blandas → sano", snap["estado"] == "ok",
              str(snap["motivos"]), SECTION_2)
        assert snap["estado"] == "ok"

    def test_snapshot_is_json_serializable(self):
        import json
        snap = _badge().health_snapshot({"contract": 1, "criticos_en_rutas_clave": 1},
                                        {"total_requests": 10, "error_rate": 0.2})
        try:
            json.dumps(snap)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check("snapshot serializable", ok, "json.dumps OK", SECTION_2)
        assert ok

    def test_empty_inputs_do_not_break(self):
        snap = _badge().health_snapshot()
        check("sin datos → sano", snap["estado"] == "ok", snap["estado"], SECTION_2)
        check("detalle con ceros", snap["detalle"]["total_requests"] == 0,
              str(snap["detalle"]), SECTION_2)
        assert snap["estado"] == "ok"


class TestHealthBadgeRuntime:
    def test_snapshot_reads_real_stats(self):
        from src.interfaces.web import request_stats as rs
        from src.utils.degradation import reset_degradation_stats
        rs.reset()
        reset_degradation_stats()
        rs.record_request("/api/v1/x", 200, 5.0)
        snap = _badge()._snapshot_from_runtime()
        check("ve el request real", snap["detalle"]["total_requests"] == 1,
              str(snap["detalle"]["total_requests"]), SECTION_2)
        check("estado sano", snap["estado"] == "ok", snap["estado"], SECTION_2)
        assert snap["detalle"]["total_requests"] == 1

    def test_refresh_without_element_does_not_raise(self):
        b = _badge().HealthBadge()
        snap = b.refresh()      # sin UI creada
        check("refresh sin elemento devuelve snapshot", "estado" in snap,
              str(snap.get("estado")), SECTION_2)
        assert "estado" in snap

    def test_tooltip_explains_state(self):
        b = _badge().HealthBadge()
        texto = b._tooltip_text(_badge().health_snapshot(
            {"contract": 2, "criticos_en_rutas_clave": 2, "soft": 1},
            {"total_requests": 9, "error_rate": 0.11, "uptime_seconds": 600}))
        check("incluye motivo", "rutas críticas" in texto, texto[:80], SECTION_2)
        check("incluye requests", "requests: 9" in texto, texto[:80], SECTION_2)
        check("incluye uptime", "min" in texto, texto[:80], SECTION_2)
        assert "rutas críticas" in texto and "requests: 9" in texto


class TestNoPollingPolicy:
    """R7: ningún componente de UI debe refrescar solo (CPU-only)."""

    def test_no_permanent_ui_timer_without_activation_management(self):
        """Todo `ui.timer` debe ser `once=True` o gestionado (activate/deactivate).

        Así un timer que late SIEMPRE (el caso real: la cola de UI de `chat.py`
        latía 10 veces/s con la cola vacía) queda prohibido, pero se permite un
        timer que duerme cuando no hay trabajo. Se analiza con AST, no con grep.
        """
        import ast
        ofensores = []
        for path in (PROJECT_ROOT / "src" / "interfaces").rglob("*.py"):
            if "test" in path.name:
                continue
            src = path.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            usos = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "timer"]
            if not usos:
                continue
            una_vez = all(any(k.arg == "once" and getattr(k.value, "value", False) is True
                              for k in n.keywords) for n in usos)
            gestionado = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("activate", "deactivate")
                for n in ast.walk(tree))
            if not (una_vez or gestionado):
                ofensores.append(f"{path.relative_to(PROJECT_ROOT)} ({len(usos)} timer/s)")
        check("sin timers permanentes sin gestión de activación", not ofensores,
              str(ofensores), SECTION_2)
        assert not ofensores, (
            f"timers que laten siempre en: {ofensores} — R7 pide refresco manual "
            "o un timer que duerma cuando no hay trabajo"
        )

    def test_badge_refreshes_on_events_not_timers(self):
        """El badge se refresca por evento: la app lo llama en 2 momentos."""
        import ast
        src = (PROJECT_ROOT / "src" / "interfaces" / "web" / "app.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        llamadas = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "refresh" and \
                    isinstance(node.func.value, ast.Name) and \
                    node.func.value.id == "_health_badge":
                llamadas += 1
        check("la app refresca el badge tras eventos (>=2)", llamadas >= 2,
              f"{llamadas} llamadas", SECTION_2)
        assert llamadas >= 2


class TestWiring:
    def test_app_uses_the_component(self):
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        mod = _badge()
        check("app importa HealthBadge", getattr(app, "HealthBadge", None) is mod.HealthBadge,
              "identidad de clase", SECTION_2)
        check("app expone el global del badge", hasattr(app, "_health_badge"),
              "global presente", SECTION_2)
        assert getattr(app, "HealthBadge", None) is mod.HealthBadge

    def test_badge_renders_and_updates_in_real_ui(self, tmp_path):
        """E2E: el badge dice 'sano' y pasa a 'con errores' al degradarse."""
        from nicegui.testing.user_simulation import user_simulation
        main = tmp_path / "badge_main.py"
        main.write_text(f'''\
import logging
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from nicegui import ui
from src.interfaces.components.health_badge import HealthBadge
from src.utils import degradation


@ui.page('/')
async def home():
    b = HealthBadge()
    b.create()

    def degradar():
        degradation.reset_degradation_stats()
        degradation.log_degraded(logging.getLogger('x'), TypeError('t'),
                                 'dispatcher.process')
        b.refresh()

    ui.button('DEGRADAR', on_click=degradar)


ui.run(port=8190, show=False, reload=False, storage_secret='test-secret')
''', encoding="utf-8")

        async def scenario():
            async with user_simulation(main_file=str(main)) as user:
                await user.open('/')
                await user.should_see('sano')
                user.find('DEGRADAR').click()
                await asyncio.sleep(0.6)
                await user.should_see('con errores')

        try:
            asyncio.run(scenario())
            ok, detalle = True, "badge cambió de 🟢 sano a 🔴 con errores"
        except Exception as e:  # noqa: BLE001
            ok, detalle = False, f"{type(e).__name__}: {e}"
        check("el badge refleja el estado en la UI real", ok, detalle, SECTION_2)
        assert ok


class TestIdleUpdateTimer:
    """El timer de la cola de UI duerme cuando no hay trabajo (R7).

    Antes latía 10 veces por segundo SIEMPRE (cola vacía incluida). Lo detectó
    la política `ui.timer` de esta suite.
    """

    class _FakeTimer:
        def __init__(self):
            self.active = True
            self.activaciones = 0
            self.desactivaciones = 0

        def activate(self):
            self.active = True
            self.activaciones += 1

        def deactivate(self):
            self.active = False
            self.desactivaciones += 1

    def _component(self):
        from src.interfaces.components.chat import ChatComponent
        c = ChatComponent.__new__(ChatComponent)     # sin UI real
        c._pending_ui_updates = []
        c._update_timer = self._FakeTimer()
        return c

    def test_timer_activates_only_when_work_arrives(self):
        c = self._component()
        c._update_timer.active = False
        check("empieza dormido", c._update_timer.active is False,
              str(c._update_timer.active), SECTION_2)
        c._schedule_ui(lambda: None)
        check("al encolar despierta", c._update_timer.active is True,
              str(c._update_timer.active), SECTION_2)
        assert c._update_timer.active is True

    def test_timer_sleeps_when_queue_is_empty(self):
        c = self._component()
        ejecutados = []
        c._schedule_ui(lambda: ejecutados.append(1))
        c._process_pending_updates()
        check("procesó lo encolado", ejecutados == [1], str(ejecutados), SECTION_2)
        check("y volvió a dormir", c._update_timer.active is False,
              str(c._update_timer.active), SECTION_2)
        assert ejecutados == [1] and c._update_timer.active is False

    def test_empty_queue_does_not_wake_up(self):
        c = self._component()
        c._update_timer.active = False
        c._process_pending_updates()          # cola vacía
        check("sin trabajo no se activa", c._update_timer.activaciones == 0,
              str(c._update_timer.activaciones), SECTION_2)
        assert c._update_timer.activaciones == 0

    def test_no_update_is_lost_if_enqueued_during_drain(self):
        """Re-chequeo tras drenar: lo que llega mientras se procesa no se pierde."""
        c = self._component()
        llegada_tardia = []
        c._schedule_ui(lambda: c._schedule_ui(lambda: llegada_tardia.append(1)))
        c._process_pending_updates()     # procesa el 1º, que encola otro
        check("el encolado durante el drenaje mantiene el timer despierto",
              c._update_timer.active is True, str(c._update_timer.active), SECTION_2)
        c._process_pending_updates()     # ahora procesa el tardío
        check("y se ejecuta", llegada_tardia == [1], str(llegada_tardia), SECTION_2)
        assert llegada_tardia == [1]

    def test_broken_timer_does_not_raise(self):
        c = self._component()
        c._update_timer = object()       # sin .active/.activate
        try:
            c._schedule_ui(lambda: None)
            c._process_pending_updates()
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        check("timer roto no rompe la UI", ok, "sin excepción", SECTION_2)
        assert ok


# ═══════════════════════════════════════════════════════════════
# EWMA vivo + telemetría por turno + métrica honesta (ISSUE-067/068)
# ═══════════════════════════════════════════════════════════════
# `EWMAThresholdOptimizer` estaba definido, importado en `feedback_loop.py:36` y
# **nunca instanciado**; por eso la tabla `interaction_metrics` no existía. Con
# él vivo hay telemetría por tarea persistida y umbrales adaptativos.
class TestEWMAVivoYTelemetria:
    @staticmethod
    def _loop(tmp_path):
        from src.core.feedback_loop import FeedbackLoop
        return FeedbackLoop(session_id="ewma_test",
                            persist_path=tmp_path / "f.jsonl",
                            db_path=str(tmp_path / "axioma.db"))

    def test_el_optimizer_esta_instanciado(self, tmp_path):
        loop = self._loop(tmp_path)
        check("EWMAThresholdOptimizer instanciado",
              getattr(loop, "_threshold_optimizer", None) is not None,
              str(type(getattr(loop, "_threshold_optimizer", None)).__name__), SECTION)
        assert getattr(loop, "_threshold_optimizer", None) is not None

    def test_la_telemetria_persiste_en_su_db(self, tmp_path):
        """La tabla `interaction_metrics` (que no existía) se crea y se alimenta."""
        import asyncio, sqlite3
        from src.core.feedback_loop_components import ImplicitSignal
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_implicit("s", "general_chat", "LLM_Chat",
                                         "q", "r", ImplicitSignal.ROUTED))
        con = sqlite3.connect(tmp_path / "axioma.db")
        n = con.execute("select count(*) from interaction_metrics").fetchone()[0]
        filas = con.execute("select task_type, success from interaction_metrics").fetchall()
        check("tabla interaction_metrics creada", n >= 1, f"{n} filas", SECTION)
        check("registró la tarea y el resultado",
              filas and filas[0][0] == "general_chat", str(filas[:2]), SECTION)
        assert n >= 1

    def test_senal_neutra_no_penaliza_la_calidad(self, tmp_path):
        """`ROUTED` es neutra: antes diluía la tasa de éxito y bloqueaba umbrales."""
        import asyncio
        from src.core.feedback_loop_components import ImplicitSignal
        loop = self._loop(tmp_path)
        for _ in range(5):
            asyncio.run(loop.record_implicit("s", "general_chat", "LLM_Chat",
                                             "q", "r", ImplicitSignal.ROUTED))
        st = asyncio.run(loop.get_global_stats())
        check("las neutras se cuentan como neutras", st["neutral_count"] == 5,
              str(st["neutral_count"]), SECTION)
        check("no hay señales decididas", st["decided_count"] == 0,
              str(st["decided_count"]), SECTION)
        check("la métrica declara su base",
              "decididas" in str(st.get("success_rate_basis")), 
              str(st.get("success_rate_basis")), SECTION)
        assert st["decided_count"] == 0 and st["neutral_count"] == 5

    def test_tasa_de_exito_sobre_decididas(self, tmp_path):
        import asyncio
        from src.core.feedback_loop_components import (ExplicitRating,
                                                       FeedbackType)
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_explicit("s", "general_chat", "LLM_Chat", "q", "r",
                                         ExplicitRating.GOOD))
        asyncio.run(loop.record_explicit("s", "general_chat", "LLM_Chat", "q", "r",
                                         ExplicitRating.POOR))
        st = asyncio.run(loop.get_global_stats())
        check("1 buena / 1 mala → 0.5 (no diluido por neutras)",
              st["global_success_rate"] == 0.5, str(st["global_success_rate"]), SECTION)
        assert st["global_success_rate"] == 0.5

    def test_gate_proactivo_no_bloquea_sin_evidencia(self, tmp_path):
        """Sin señales decididas el gate devuelve 1.0 (no bloquear por falta de datos)."""
        import asyncio
        from src.core.proactive_engine import ProactiveEngine
        from src.core.feedback_loop_components import ImplicitSignal
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_implicit("s", "general_chat", "LLM_Chat",
                                         "q", "r", ImplicitSignal.ROUTED))
        # ⚠️ Hay que parchear el símbolo donde se USA (`proactive_engine` lo
        # importa a nivel de módulo), no el del módulo de origen.
        import src.core.proactive_engine as pe_mod
        original = pe_mod.get_feedback_loop

        async def _fake():
            return loop
        pe_mod.get_feedback_loop = _fake
        try:
            engine = ProactiveEngine()
            ewma = asyncio.run(engine._get_ewma())
        finally:
            pe_mod.get_feedback_loop = original
        check("sin decididas → 1.0", ewma == 1.0, str(ewma), SECTION)
        assert ewma == 1.0

    def test_el_endpoint_expone_umbrales(self, tmp_path):
        """`/feedback/stats` muestra los umbrales EWMA por tarea."""
        import asyncio
        from src.interfaces.web import routes_extended as R
        got = asyncio.run(R.get_feedback_stats())
        check("payload con thresholds", "thresholds" in got,
              str(sorted(got)[:6]), SECTION)
        assert "thresholds" in got


class TestTelemetriaP11Completa:
    """P1.1: la telemetría guarda agente, latencia, fuentes y confianza.

    Verifica la cadena completa que se corrigió: `AgentResult.to_response()`
    propaga `sources` → el dispatcher manda `latency_ms` + `num_sources` +
    `confidence` → `FeedbackLoop` los propaga → columnas en `interaction_metrics`.
    """

    @staticmethod
    def _loop(tmp_path):
        from src.core.feedback_loop import FeedbackLoop
        return FeedbackLoop(session_id="p11_test",
                            persist_path=tmp_path / "f.jsonl",
                            db_path=str(tmp_path / "axioma.db"))

    def test_la_fila_tiene_todos_los_campos(self, tmp_path):
        import asyncio, sqlite3
        from src.core.feedback_loop_components import ImplicitSignal
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_implicit(
            "s1", "general_chat", "LLM_Chat", "q", "r", ImplicitSignal.ROUTED,
            latency_ms=208116.74,
            metadata={"num_sources": 3, "confidence": 0.88}))
        con = sqlite3.connect(tmp_path / "axioma.db")
        fila = con.execute(
            "select task_type, agent_role, latency_ms, num_sources, "
            "confidence_input, success from interaction_metrics").fetchone()
        check("task_type y agent_role", fila[0] == "general_chat" and fila[1] == "LLM_Chat",
              str(fila[:2]), SECTION)
        check("latencia real (no 0.0)", fila[2] == 208116.74, str(fila[2]), SECTION)
        check("nº de fuentes", fila[3] == 3, str(fila[3]), SECTION)
        check("confianza calibrada", fila[4] == 0.88, str(fila[4]), SECTION)
        check("señal neutra registrada como éxito (no penaliza)", fila[5] == 1,
              str(fila[5]), SECTION)
        assert fila[2] == 208116.74 and fila[3] == 3 and fila[4] == 0.88

    def test_las_fuentes_del_agente_llegan_a_la_telemetria(self, tmp_path):
        """Cadena completa: `to_response()` → `num_sources`."""
        import asyncio, sqlite3
        from src.agents.base import AgentResult
        from src.core.feedback_loop_components import ImplicitSignal
        resp = AgentResult(success=True, content="x", agent_type="researcher",
                           model_used="qwen3:8b", task_type="research",
                           latency_ms=1234.0, confidence=0.9,
                           metadata={"sources": ["A - u1", "B - u2"]}).to_response("s")
        loop = self._loop(tmp_path)
        asyncio.run(loop.record_implicit(
            "s1", "research", "ResearcherAgent", "q", resp.content,
            ImplicitSignal.ROUTED, latency_ms=resp.latency_ms,
            metadata={"num_sources": len(resp.sources or []),
                      "confidence": resp.confidence}))
        con = sqlite3.connect(tmp_path / "axioma.db")
        n = con.execute("select num_sources from interaction_metrics").fetchone()[0]
        check("num_sources = 2 (viene de Response.sources)", n == 2, str(n), SECTION)
        assert n == 2


# ═══════════════════════════════════════════════════════════════
# ISSUE-082 — el uso REAL de tokens llega a `data/traces.db`
# ═══════════════════════════════════════════════════════════════
# Antes: `tokens_generated`/`tokens_prompt` estaban en **0 en las 741 filas** de
# `traces` (el cliente los calculaba pero no se propagaban), así que la latencia
# no se podía atribuir a nada (modelo vs prompt vs reintentos vs búsqueda).
class TestUsoDeTokensEnLaTelemetria:
    def test_tokens_de_respuesta_lee_los_contadores_de_ollama(self):
        import types
        from src.llm.client_base import tokens_de_respuesta
        uso = tokens_de_respuesta(types.SimpleNamespace(prompt_eval_count=461, eval_count=137))
        check("lee prompt_eval_count y eval_count", uso == {"tokens_prompt": 461, "tokens_generated": 137},
              str(uso), SECTION)
        assert uso == {"tokens_prompt": 461, "tokens_generated": 137}

    def test_tokens_de_respuesta_sin_datos_no_inventa(self):
        from src.llm.client_base import tokens_de_respuesta
        uso = tokens_de_respuesta(object())
        check("sin contadores → 0 (no inventa)", uso == {"tokens_prompt": 0, "tokens_generated": 0},
              str(uso), SECTION)
        assert uso == {"tokens_prompt": 0, "tokens_generated": 0}

    def test_generate_propaga_el_uso_al_agent_result(self):
        """Cadena completa: `_generate` → thread-local → `AgentResult.metadata`."""
        import types
        from src.agents.base import AgentResult, BaseAgent

        class ClienteFalso:
            def generate(self, **_kw):
                return types.SimpleNamespace(content="hola", prompt_eval_count=7, eval_count=3)

        class AgenteConcreto(BaseAgent):          # BaseAgent es ABC: hace falta execute()
            def execute(self, *_a, **_k):         # pragma: no cover
                raise NotImplementedError

        ag = AgenteConcreto.__new__(AgenteConcreto)
        ag._llm_client = ClienteFalso()   # la property es de solo lectura
        ag.config = types.SimpleNamespace(system_prompt="s", temperature=0.1, max_tokens=10)
        ag._skip_cache_next = False
        ag._last_llm_response = None

        contenido = ag._generate("prompt de prueba", task_type="general_chat")
        res = AgentResult(success=True, content=contenido, agent_type="coder",
                          model_used="qwen3:8b", task_type="general_chat", latency_ms=1.0)
        check("el AgentResult lleva los tokens de la llamada",
              (res.metadata or {}).get("tokens_generated") == 3
              and (res.metadata or {}).get("tokens_prompt") == 7, str(res.metadata), SECTION)
        assert (res.metadata or {}).get("tokens_generated") == 3

    def test_el_uso_se_consume_una_sola_vez(self):
        """No puede quedar pegado a un resultado posterior del mismo hilo."""
        import types
        from src.agents.base import AgentResult, BaseAgent, _USO_LLM

        class ClienteFalso:
            def generate(self, **_kw):
                return types.SimpleNamespace(content="x", prompt_eval_count=5, eval_count=2)

        class AgenteConcreto(BaseAgent):
            def execute(self, *_a, **_k):         # pragma: no cover
                raise NotImplementedError

        ag = AgenteConcreto.__new__(AgenteConcreto)
        ag._llm_client = ClienteFalso()   # la property es de solo lectura
        ag.config = types.SimpleNamespace(system_prompt="s", temperature=0.1, max_tokens=10)
        ag._skip_cache_next = False
        ag._last_llm_response = None
        try:
            ag._generate("p", task_type="t")
            con_uso = AgentResult(success=True, content="a", agent_type="x", model_used="m",
                                  task_type="t", latency_ms=1.0)
            sin_uso = AgentResult(success=True, content="b", agent_type="x", model_used="m",
                                  task_type="t", latency_ms=1.0)
        finally:
            _USO_LLM.tokens = None
        check("el primero lo lleva", (con_uso.metadata or {}).get("tokens_generated") == 2,
              str(con_uso.metadata), SECTION)
        check("el segundo NO lo hereda", not (sin_uso.metadata or {}).get("tokens_generated"),
              str(sin_uso.metadata), SECTION)
        assert (con_uso.metadata or {}).get("tokens_generated") == 2
        assert not (sin_uso.metadata or {}).get("tokens_generated")

    def test_el_trace_toma_los_tokens_del_metadata(self):
        import types
        from src.core.dispatcher_process import _code_trace_fields
        f = _code_trace_fields(types.SimpleNamespace(
            metadata={"tokens_prompt": 300, "tokens_generated": 42}, validation_passed=True))
        check("kwargs del trace con tokens reales",
              (f["tokens_prompt"], f["tokens_generated"]) == (300, 42), str(f), SECTION)
        assert (f["tokens_prompt"], f["tokens_generated"]) == (300, 42)

    def test_el_trace_sin_metadata_no_rompe(self):
        import types
        from src.core.dispatcher_process import _code_trace_fields
        f = _code_trace_fields(types.SimpleNamespace(metadata=None))
        check("sin metadata → 0 (sin excepción)",
              (f["tokens_prompt"], f["tokens_generated"]) == (0, 0), str(f), SECTION)
        assert (f["tokens_prompt"], f["tokens_generated"]) == (0, 0)


class TestCierreDelPortalSinErroresFalsos:
    """`ISSUE-133`: el guardado semántico va en SEGUNDO PLANO, así que un hilo
    tardío puede llegar después de cerrar el portal. Antes eso producía
    `AttributeError: 'NoneType' object has no attribute 'add'` y un **ERROR en
    los registros al cerrar** (aparecía al apagar AXIOMA con el sistema sano).
    Memoria que no se guarda por estar cerrada = degradación, no fallo.
    """

    def test_guardar_y_buscar_despues_de_cerrar_no_explota_ni_grita(self, caplog, tmp_path):
        from src.memory.palace_storage import Palace
        portal = Palace(path=str(tmp_path / "palace"), wing_name="w",
                        room_name="r", hall_name="h")
        portal.close()
        with caplog.at_level(logging.DEBUG):
            portal.store(text="tarde", metadata={"role": "user"},
                         embedding=[0.0] * 1024)
            assert portal.recall(query_embedding=[0.0] * 1024) == []
        errores = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        check("el portal cerrado omite guardar/buscar sin excepción ni ERROR",
              not errores, str(errores), SECTION)
        assert not errores, f"cerrar el portal no debe registrar ERROR: {errores}"



# ── `ISSUE-144`: el registro del CLI moría en los caminos de error (2026-10-04) ──
# `_log_cli_event()` no aceptaba `level` y 12 llamadas se lo pasaban: sin Ollama,
# `main.py --check` imprimía un TypeError en vez de decir qué faltaba instalar.
def test_log_cli_event_respeta_el_nivel(monkeypatch):
    """El nivel de quien llama llega al registro (antes iba fijo en INFO). Se importa con
    `importlib` porque `from src.interfaces.cli import main` **recursiona** bajo pytest
    (`ISSUE-145`, reportado aparte)."""

    import importlib
    cli = importlib.import_module("src.interfaces.cli.main")
    visto = {}

    class _Registro:
        _initialized = True
        def log_event(self, category, message, level="INFO", extra=None):
            visto.update(category=category, message=message, level=level, extra=extra)

    monkeypatch.setattr(cli, "LOGGER_AVAILABLE", True)
    monkeypatch.setattr(cli, "get_logger", lambda: _Registro())
    cli._log_cli_event("CHECK", "Ollama no responde", level="WARNING")
    assert visto.get("level") == "WARNING", visto
    assert visto.get("category") == "CHECK" and visto.get("message") == "Ollama no responde", visto


def test_todas_las_llamadas_a_log_cli_event_aceptan_sus_argumentos():
    """El defecto era de CONTRATO: 12 llamadas a una función que no los aceptaba."""
    import ast
    import importlib
    import inspect
    from pathlib import Path
    cli = importlib.import_module("src.interfaces.cli.main")
    aceptados = set(inspect.signature(cli._log_cli_event).parameters)
    arbol = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    malas = [n.lineno for n in ast.walk(arbol) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_log_cli_event"
             and any(k.arg not in aceptados for k in n.keywords if k.arg)]
    assert malas == [], f"llamadas con argumentos que la función no acepta: {malas}"
