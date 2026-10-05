#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — P0: panel de transparencia + feedback enriquecido cableado
# ═══════════════════════════════════════════════════════════════
# Cubre los dos agujeros del P0:
#   (a) `confidence` / `confidence_label` / `validation_passed` se descartaban
#       en `_build_response_metadata` → la UI no podía mostrar nada.
#   (b) `FeedbackLoop.record_explicit()` tenía **0 llamadas**: el 👍/👎 del
#       usuario solo escribía un trace de log. Ahora hay rating 1-5 + comentario
#       + tags y llegan al loop de aprendizaje.
# ═══════════════════════════════════════════════════════════════
import asyncio
import ast
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

SECTION = "25. Transparencia + feedback (P0)"

META = {
    "request": "¿Qué es AXIOMA?", "task_type": "research",
    "model_used": "qwen3:8b", "agent_role": "WebDispatcher",
    "confidence": 0.62, "confidence_label": "Moderado",
    "validation_state": "omitida", "latency_ms": 4200.0,
    "sources": ["Doc A - https://a.com", "Doc B - https://b.com"],
    "classification_confidence": 0.91,
}


def _tr():
    from src.interfaces.components import transparency
    return transparency


def _logic():
    from src.interfaces.web import app_logic
    return app_logic


def _chat():
    from src.interfaces.components import chat
    return chat


# ═══════════════════════════════════════════════════════════════
# (a) PANEL DE TRANSPARENCIA
# ═══════════════════════════════════════════════════════════════


class TestTransparencyRows:
    def test_full_metadata_rows(self):
        rows = _tr().transparency_rows(META)
        flat = " | ".join(f"{a}: {b}" for a, b, _ in rows)
        check("fila de modelo", "qwen3:8b" in flat and "research" in flat, flat[:80], SECTION)
        check("fila de confianza", "Moderado (62%)" in flat, flat[:80], SECTION)
        check("fila de validación", "omitida" in flat, flat[:80], SECTION)
        check("fila de fuentes", "2 cita(s)" in flat, flat[:80], SECTION)
        check("fila de tiempo", "4.2 s" in flat, flat[:80], SECTION)
        check("fila de clasificación", "91%" in flat, flat[:80], SECTION)
        assert "Moderado (62%)" in flat and "4.2 s" in flat

    def test_rows_have_no_invented_data(self):
        """Sin datos, se dice '—' / 'sin citas': nunca se inventa un valor."""
        rows = _tr().transparency_rows({})
        flat = " | ".join(f"{a}: {b}" for a, b, _ in rows)
        check("sin citas se declara", "sin citas" in flat, flat[:90], SECTION)
        check("confianza desconocida se declara", "—" in flat, flat[:90], SECTION)
        check("no se inventa latencia", "Tiempo" not in flat, flat[:90], SECTION)
        assert "sin citas" in flat and "Tiempo" not in flat

    def test_confidence_label_thresholds(self):
        f = _tr().confidence_label
        check("≥0.8 Alto", f(0.9) == "Alto", f(0.9), SECTION)
        check("0.5-0.8 Moderado", f(0.62) == "Moderado", f(0.62), SECTION)
        check("<0.5 Bajo", f(0.2) == "Bajo", f(0.2), SECTION)
        check("None → —", f(None) == "—", f(None), SECTION)
        assert f(0.9) == "Alto" and f(0.2) == "Bajo"

    def test_low_confidence_is_flagged_visually(self):
        rows = _tr().transparency_rows({"confidence": 0.2, "model_used": "m",
                                        "task_type": "t"})
        conf = [r for r in rows if "Confianza" in r[0]][0]
        check("confianza baja en rojo", "red" in conf[2], conf[2], SECTION)
        check("dice Bajo (20%)", "Bajo (20%)" in conf[1], conf[1], SECTION)
        assert "red" in conf[2]

    def test_sources_cap_is_disclosed(self):
        """`sources` se capea a 8 en la metadata: el panel lo dice, no lo oculta."""
        rows = _tr().transparency_rows({"sources": [f"u{i}" for i in range(8)]})
        src = [r for r in rows if "Fuentes" in r[0]][0]
        check("avisa que hay más fuentes", "primeras" in src[1], src[1], SECTION)
        assert "primeras" in src[1]

    def test_markdown_like_source_counts(self):
        rows = _tr().transparency_rows({"sources": ["a"]})
        src = [r for r in rows if "Fuentes" in r[0]][0]
        check("una cita se informa como 1", "1 cita(s)" in src[1], src[1], SECTION)
        assert "1 cita(s)" in src[1]


class TestValidationStateHonesty:
    """`validation_passed=False` NO siempre significa que falló."""

    def _state(self, passed, rmeta):
        return _logic()._validation_state(passed, rmeta)

    def test_approved(self):
        check("aprobada", self._state(True, {}) == "aprobada", "", SECTION)
        assert self._state(True, {}) == "aprobada"

    def test_failed(self):
        got = self._state(False, {"validation_failed": True})
        check("fallida cuando el dispatcher la marcó", got == "fallida", got, SECTION)
        assert got == "fallida"

    def test_skipped(self):
        got = self._state(False, {"validation_skipped_grounding": True})
        check("omitida cuando se salteó", got == "omitida", got, SECTION)
        assert got == "omitida"

    def test_not_applicable_is_not_a_failure(self):
        got = self._state(False, {})
        check("sin marcas → no aplica (no se afirma un fallo)",
              got == "no_aplicada", got, SECTION)
        assert got == "no_aplicada"

    def test_labels_do_not_claim_failure_for_not_applicable(self):
        rows = _tr().transparency_rows({"validation_state": "no_aplicada"})
        val = [r for r in rows if "Validación" in r[1]][0]
        check("texto honesto", "no aplica" in val[1] and "falló" not in val[1],
              val[1], SECTION)
        assert "no aplica" in val[1]

    def test_failed_state_message_is_explicit(self):
        rows = _tr().transparency_rows({"validation_state": "fallida"})
        val = [r for r in rows if "Validación" in r[1]][0]
        check("se informa que falló", "falló" in val[1], val[1], SECTION)
        assert "falló" in val[1]


class TestMetadataPassthrough:
    """(a) El borde UI ya no descarta confidence/validation."""

    @staticmethod
    def _resp(**kw):
        from src.domain.entities import Response
        base = dict(content="hola", task_type="general_chat", model_used="qwen3:8b",
                    latency_ms=100.0)
        base.update(kw)
        return Response(**base)

    def test_confidence_and_label_are_forwarded(self):
        meta = _logic()._build_response_metadata(self._resp(confidence=0.83), "q", 0.0)
        check("confidence presente", meta.get("confidence") == 0.83,
              str(meta.get("confidence")), SECTION)
        check("label derivada cuando falta", meta.get("confidence_label") == "Alto",
              str(meta.get("confidence_label")), SECTION)
        assert meta["confidence"] == 0.83 and meta["confidence_label"] == "Alto"

    def test_valid_label_from_response_is_forwarded(self):
        """Si `Response` trae una etiqueta VÁLIDA, se usa tal cual (no se re-deriva)."""
        meta = _logic()._build_response_metadata(
            self._resp(confidence=0.83, confidence_label="Moderado"), "q", 0.0)
        check("label válida del Response se respeta",
              meta.get("confidence_label") == "Moderado",
              str(meta.get("confidence_label")), SECTION)
        assert meta["confidence_label"] == "Moderado"

    def test_invalid_label_falls_back_to_derived(self):
        """`Response` normaliza labels fuera de Alto|Moderado|Bajo a None
        (ver `validate_confidence_label`); ahí entra la derivación local."""
        meta = _logic()._build_response_metadata(
            self._resp(confidence=0.83, confidence_label="Muy alto"), "q", 0.0)
        check("label inválida → se deriva de la confianza",
              meta.get("confidence_label") == "Alto",
              str(meta.get("confidence_label")), SECTION)
        assert meta["confidence_label"] == "Alto"

    def test_validation_state_forwarded(self):
        meta = _logic()._build_response_metadata(
            self._resp(validation_passed=True), "q", 0.0)
        check("validation_passed", meta.get("validation_passed") is True, "", SECTION)
        check("validation_state", meta.get("validation_state") == "aprobada",
              str(meta.get("validation_state")), SECTION)
        assert meta["validation_state"] == "aprobada"

    def test_agent_role_and_classification_forwarded(self):
        meta = _logic()._build_response_metadata(
            self._resp(metadata={"agent_role": "CoderAgent",
                                 "classification_confidence": 0.77}), "q", 0.0)
        check("agent_role", meta.get("agent_role") == "CoderAgent",
              str(meta.get("agent_role")), SECTION)
        check("classification_confidence", meta.get("classification_confidence") == 0.77,
              str(meta.get("classification_confidence")), SECTION)
        assert meta["agent_role"] == "CoderAgent"

    def test_broken_values_do_not_raise(self):
        """Valores no numéricos → None, sin excepción.

        Se usa un doble en vez de `Response` a propósito: pydantic **rechaza**
        `confidence="no-numero"` en la construcción, así que para probar la
        defensa del borde UI hay que simular un objeto que ya viene raro (p. ej.
        de un agente o de un cache con datos viejos).
        """
        from types import SimpleNamespace
        raro = SimpleNamespace(content="x", task_type="t", model_used="m",
                               latency_ms=1.0, confidence="no-numero", sources=[],
                               validation_passed=False,
                               metadata={"classification_confidence": "x"})
        meta = _logic()._build_response_metadata(raro, "q", 0.0)
        check("confidence no numérica → None (no rompe)",
              meta.get("confidence") is None, str(meta.get("confidence")), SECTION)
        check("classification_confidence no numérica → None",
              meta.get("classification_confidence") is None,
              str(meta.get("classification_confidence")), SECTION)
        assert meta["confidence"] is None and meta["classification_confidence"] is None


# ═══════════════════════════════════════════════════════════════
# (b) FEEDBACK ENRIQUECIDO CABLEADO AL LOOP
# ═══════════════════════════════════════════════════════════════


class _FakeLoop:
    """Loop falso que captura la llamada (no toca DB ni JSONL reales)."""

    def __init__(self):
        self.calls = []

    async def record_explicit(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(**kw)


def _widget(meta=None, text="respuesta de prueba", session_id="p0_test"):
    chat = _chat()
    st = SimpleNamespace(session_id=session_id)
    return chat.FeedbackWidget(session_state=st,
                               response_metadata=dict(meta or META),
                               response_text=text)


class TestFeedbackWiring:
    def test_record_reaches_the_loop(self, monkeypatch):
        import src.core.feedback_loop as fl
        fake = _FakeLoop()

        async def _get():
            return fake
        monkeypatch.setattr(fl, "get_feedback_loop", _get)

        fw = _widget()
        ok = asyncio.run(fw._record_to_loop(5, "excelente", ["buena"]))
        check("el feedback llega al FeedbackLoop", ok is True, str(ok), SECTION)
        check("se llamó record_explicit", len(fake.calls) == 1, str(len(fake.calls)), SECTION)
        call = fake.calls[0]
        check("rating correcto", call["rating"] == 5, str(call["rating"]), SECTION)
        check("comentario", call["comment"] == "excelente", str(call["comment"]), SECTION)
        check("tags", call["tags"] == ["buena"], str(call["tags"]), SECTION)
        check("query del usuario", call["query"] == META["request"], str(call["query"]), SECTION)
        check("respuesta real (no vacía)", call["response"] == "respuesta de prueba",
              call["response"], SECTION)
        check("agent_role de la metadata", call["agent_role"] == "WebDispatcher",
              str(call["agent_role"]), SECTION)
        check("task_type", call["task_type"] == "research", str(call["task_type"]), SECTION)
        assert ok is True and call["rating"] == 5 and call["tags"] == ["buena"]

    def test_metadata_extra_travels(self, monkeypatch):
        import src.core.feedback_loop as fl
        fake = _FakeLoop()

        async def _get():
            return fake
        monkeypatch.setattr(fl, "get_feedback_loop", _get)
        asyncio.run(_widget()._record_to_loop(3, "", []))
        extra = fake.calls[0]["metadata"]
        check("metadata incluye modelo/confianza/fuentes",
              extra.get("model_used") == "qwen3:8b"
              and extra.get("confidence") == 0.62
              and extra.get("source_count") == 2, str(extra), SECTION)
        assert extra["source_count"] == 2

    def test_no_loop_degrades_without_raising(self, monkeypatch):
        import src.core.feedback_loop as fl

        async def _none():
            return None
        monkeypatch.setattr(fl, "get_feedback_loop", _none)
        ok = asyncio.run(_widget()._record_to_loop(4, "", []))
        check("sin loop devuelve False (no lanza)", ok is False, str(ok), SECTION)
        assert ok is False

    def test_loop_failure_is_swallowed(self, monkeypatch):
        import src.core.feedback_loop as fl

        async def _boom():
            raise RuntimeError("simulado")
        monkeypatch.setattr(fl, "get_feedback_loop", _boom)
        ok = asyncio.run(_widget()._record_to_loop(4, "", []))
        check("fallo del loop → False, sin excepción", ok is False, str(ok), SECTION)
        assert ok is False

    def test_agent_role_falls_back_when_absent(self, monkeypatch):
        import src.core.feedback_loop as fl
        fake = _FakeLoop()

        async def _get():
            return fake
        monkeypatch.setattr(fl, "get_feedback_loop", _get)
        meta = dict(META)
        meta.pop("agent_role")
        asyncio.run(_widget(meta)._record_to_loop(4, "", []))
        check("sin agent_role usa WebDispatcher",
              fake.calls[0]["agent_role"] == "WebDispatcher",
              str(fake.calls[0]["agent_role"]), SECTION)
        assert fake.calls[0]["agent_role"] == "WebDispatcher"

    def test_thumbs_shortcut_sets_rating(self):
        fw = _widget()
        fw.elements = {"rating": SimpleNamespace(value=None)}
        fw._set_rating(4)
        check("👍 fija rating 4", fw.elements["rating"].value == 4,
              str(fw.elements["rating"].value), SECTION)
        fw._set_rating(2)
        check("👎 fija rating 2", fw.elements["rating"].value == 2,
              str(fw.elements["rating"].value), SECTION)
        assert fw.elements["rating"].value == 2

    def test_tags_are_a_closed_set(self):
        tags = _chat().FeedbackWidget.TAGS
        check("tags definidos", len(tags) >= 5, str(tags), SECTION)
        check("tags sin duplicados", len(set(tags)) == len(tags), str(tags), SECTION)
        check("tags en snake_case (agregables)", all(t == t.lower() and " " not in t
                                                     for t in tags), str(tags), SECTION)
        assert len(tags) >= 5

    def test_submit_handler_is_async(self):
        """Debe ser async: así `await loop.record_explicit(...)` corre en el loop
        de NiceGUI, sin hilos ni `create_task` sueltos."""
        fn = _chat().FeedbackWidget._submit_feedback
        check("_submit_feedback es async", inspect.iscoroutinefunction(fn), "", SECTION)
        assert inspect.iscoroutinefunction(fn)


class TestRecordExplicitHasCallers:
    """Regresión del bug: `record_explicit` tenía 0 llamadas en todo el repo."""

    def test_record_explicit_is_called_from_the_ui(self):
        callers = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "record_explicit":
                    callers.append(str(path.relative_to(PROJECT_ROOT)))
        check("hay al menos un consumidor de record_explicit", bool(callers),
              str(sorted(set(callers))), SECTION)
        check("el consumidor es la UI del chat",
              any("components" in c for c in callers),
              str(sorted(set(callers))), SECTION)
        assert callers and any("components" in c for c in callers)

    def test_widget_no_longer_only_logs(self):
        """El widget debe llamar al loop, no solo al logger.

        Se verifica con **AST** (estructura del código): a diferencia de un grep
        sobre el texto, no se rompe si el método se reformatea o se mueve.
        (La prueba de COMPORTAMIENTO de que el loop recibe el rating está en el
        E2E de más abajo.)
        """
        import inspect as _inspect
        import textwrap
        # Se analiza la CLASE completa (dedent) y se busca el método por nombre:
        # así no depende de cómo esté formateado el `def`.
        tree = ast.parse(textwrap.dedent(
            _inspect.getsource(_chat().FeedbackWidget)))
        metodo = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "_submit_feedback"), None)
        if metodo is None:
            check("_submit_feedback existe", False, "no encontrado", SECTION)
            assert False, "_submit_feedback no encontrado en FeedbackWidget"
        llama_al_loop = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_record_to_loop"
            for n in ast.walk(metodo))
        check("_submit_feedback llama _record_to_loop", llama_al_loop,
              "llamada detectada por AST", SECTION)
        assert llama_al_loop


class TestDoDLearningLoopCloses:
    """DoD medible: después de calificar, el loop tiene el registro."""

    def test_rating_lands_in_the_real_loop(self, tmp_path, monkeypatch):
        from src.core.feedback_loop import FeedbackLoop
        import src.core.feedback_loop as fl

        loop = FeedbackLoop(session_id="p0_dod", persist_path=tmp_path / "f.jsonl",
                            db_path=str(tmp_path / "axioma.db"))

        async def _get():
            return loop
        monkeypatch.setattr(fl, "get_feedback_loop", _get)

        fw = _widget(session_id="p0_dod")
        # ⚠️ El loop carga métricas agregadas de la DB real al construirse
        # (`_load_metrics_from_db`): se mide el DELTA, no el absoluto.
        before = asyncio.run(loop.get_global_stats())
        ok = asyncio.run(fw._record_to_loop(4, "muy útil", ["buena"]))
        check("el loop acepta el feedback", ok is True, str(ok), SECTION)

        stats = asyncio.run(loop.get_global_stats())
        records = asyncio.run(loop.get_recent_records(limit=5))
        delta_records = stats.get("total_records", 0) - before.get("total_records", 0)
        delta_pos = stats.get("positive_count", 0) - before.get("positive_count", 0)
        check("total_records subió en 1", delta_records == 1,
              f"delta={delta_records}", SECTION)
        check("1 positivo (rating 4)", delta_pos == 1, f"delta={delta_pos}", SECTION)
        rec = records[0]
        check("el registro es del tipo EXPLICIT",
              getattr(rec.feedback_type, "value", "") == "explicit",
              str(rec.feedback_type), SECTION)
        check("guardó el rating", str(getattr(rec, "signal_or_rating", "")) == "4",
              str(rec.signal_or_rating), SECTION)
        check("guardó el comentario", "muy útil" in json.dumps(rec.to_dict(), ensure_ascii=False),
              "comentario en el registro", SECTION)
        assert ok is True and delta_records == 1 and delta_pos == 1
        assert rec.metadata.get("tags") == ["buena"] or "buena" in json.dumps(rec.to_dict())


class TestParameterOptimizerPersistencia:
    """A1/ISSUE-075: el estado aprendido del `ParameterOptimizer` sobrevive reinicios.

    ANTES: `_feedback_history` y `_current_params` vivían solo en memoria → el
    sistema "aprendía" y se olvidaba en cada reinicio. Ahora persisten a un JSON
    chico vía `persist_path` (None = solo memoria, comportamiento previo).
    """

    def test_estado_aprendido_sobrevive_reinstanciacion(self, tmp_path):
        from src.core.feedback_loop_detectors_handlers import ParameterOptimizer

        path = tmp_path / "param_optimizer.json"
        # ventana chica + cooldown 0 para que el ajuste dispare con pocas muestras
        opt = ParameterOptimizer(persist_path=str(path),
                                 adjustment_window_size=4, cooldown_seconds=0)
        for _ in range(10):
            opt.record_interaction("general_chat", True)
        nuevo = asyncio.run(opt.adjust_parameter(
            "temperature", "general_chat", 0.7, 0.0, 1.0))
        check("el ajuste disparó (éxito alto → sube temperature)",
              nuevo > 0.7, f"nuevo={nuevo}", SECTION)
        assert path.exists()

        # Reinstanciar: debe cargar el estado aprendido desde el JSON
        opt2 = ParameterOptimizer(persist_path=str(path),
                                  adjustment_window_size=4, cooldown_seconds=0)
        check("historial de feedback recargado (con ventana)",
              list(opt2._feedback_history.get("general_chat", [])) == [True] * 4,
              str(list(opt2._feedback_history.get("general_chat", []))), SECTION)
        check("valor ajustado recargado",
              abs(opt2._current_params["temperature"].get("general_chat", 0.0) - nuevo) < 1e-9,
              str(opt2._current_params["temperature"]), SECTION)
        assert opt2._current_params["temperature"]["general_chat"] == nuevo

    def test_sin_persist_path_es_solo_memoria(self, tmp_path):
        from src.core.feedback_loop_detectors_handlers import ParameterOptimizer

        opt = ParameterOptimizer(persist_path=None)
        assert opt._persist_path is None
        opt.record_interaction("general_chat", True)  # no debe lanzar ni escribir
        # sin persist_path no se crea archivo alguno
        assert not (tmp_path / "param_optimizer.json").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ═══════════════════════════════════════════════════════════════
# E2E con el harness de UI real del proyecto (nicegui.testing)
# ═══════════════════════════════════════════════════════════════
# No se grepea el fuente: se levanta una app mínima que usa el MISMO código de
# producción (`transparency.render_transparency_block` + `FeedbackWidget`), se
# hace clic como un usuario y se verifica el efecto REAL (el loop recibe el
# rating). El loop se reemplaza por uno falso DENTRO del subproceso para no
# ensuciar la DB/JSONL del usuario.
_UI_MAIN_SRC = '''\
import json, sys
from pathlib import Path
sys.path.insert(0, {root!r})
from nicegui import ui

OUT = Path({out!r})

META = {{"request": "¿Qué es AXIOMA?", "task_type": "research",
        "model_used": "qwen3:8b", "agent_role": "WebDispatcher",
        "confidence": 0.62, "confidence_label": "Moderado",
        "validation_state": "omitida", "latency_ms": 4200.0,
        "sources": ["Doc A - https://a.com"], "classification_confidence": 0.91}}

import src.core.feedback_loop as fl


class FakeLoop:
    async def record_explicit(self, **kw):
        OUT.write_text(json.dumps(kw, default=str))
        return None


async def _get():
    return FakeLoop()


fl.get_feedback_loop = _get


@ui.page('/')
async def home():
    from src.interfaces.components.chat import FeedbackWidget
    from src.interfaces.components.transparency import render_transparency_block

    class St:
        session_id = 'ui_p0'

    fw = FeedbackWidget(session_state=St(), response_metadata=META,
                        response_text="AXIOMA es local.")
    render_transparency_block(META, feedback_factory=fw.create)
    fw.show()


ui.run(port=8192, show=False, reload=False, storage_secret='test-secret')
'''


class TestTransparencyUiE2E:
    def _main_file(self, tmp_path):
        out = tmp_path / "feedback.json"
        f = tmp_path / "p0_ui_main.py"
        f.write_text(_UI_MAIN_SRC.format(root=str(PROJECT_ROOT), out=str(out)),
                     encoding="utf-8")
        return str(f), out

    def test_panel_renders_real_data_and_feedback_reaches_loop(self, tmp_path):
        from nicegui.testing.user_simulation import user_simulation
        main_file, out = self._main_file(tmp_path)

        async def scenario():
            async with user_simulation(main_file=main_file) as user:
                await user.open('/')
                # 1) el colapsable existe y, al abrirlo, muestra datos REALES
                await user.should_see('¿Por qué esta respuesta?')
                user.find('¿Por qué esta respuesta?').click()
                await user.should_see('qwen3:8b')
                await user.should_see('Moderado (62%)')
                await user.should_see('omitida')
                await user.should_see('Enviar')
                # 2) calificar y enviar → el feedback llega al loop
                user.find('👍').click()
                await asyncio.sleep(0.3)
                user.find('Enviar').click()
                await asyncio.sleep(1.2)

        asyncio.run(scenario())

        check("el archivo de feedback se escribió", out.exists(),
              str(out), SECTION)
        assert out.exists(), "el feedback no llegó al loop"
        payload = json.loads(out.read_text(encoding="utf-8"))
        check("rating recibido (👍 → 4)", payload.get("rating") == 4,
              str(payload.get("rating")), SECTION)
        check("query real del usuario", payload.get("query") == "¿Qué es AXIOMA?",
              str(payload.get("query")), SECTION)
        check("respuesta real (no vacía)", payload.get("response") == "AXIOMA es local.",
              str(payload.get("response")), SECTION)
        check("agent_role de la metadata",
              payload.get("agent_role") == "WebDispatcher",
              str(payload.get("agent_role")), SECTION)
        check("task_type de la metadata", payload.get("task_type") == "research",
              str(payload.get("task_type")), SECTION)
        assert payload["rating"] == 4 and payload["response"] == "AXIOMA es local."


# ═══════════════════════════════════════════════════════════════
# Calibración de la confianza mostrada (continuación del P0)
# ═══════════════════════════════════════════════════════════════
# Antes: cada agente fijaba `Response.confidence` (el coder 1.0 fijo) y el panel
# mostraba "Alto (100%)" incluso con un score mediocre del validador. Ahora el
# score del validador pesa más que la autopercepción del agente, y sin evidencia
# la confianza del agente queda intacta.


class TestConfidenceCalibration:
    def _c(self):
        from src.utils import confidence
        return confidence

    def test_no_evidence_keeps_agent_confidence(self):
        c = self._c()
        check("sin validación → intacta (0.8)", c.calibrate_confidence(0.8) == 0.8,
              str(c.calibrate_confidence(0.8)), SECTION)
        check("sin validación y sin agente → None (no se inventa)",
              c.calibrate_confidence(None) is None, "", SECTION)
        assert c.calibrate_confidence(0.8) == 0.8 and c.calibrate_confidence(None) is None

    def test_high_score_approaches_agent_value(self):
        c = self._c()
        got = c.calibrate_confidence(1.0, validator_score=9.5)
        check("score 9.5 + agente 1.0 → ~0.95 (tope)", got == 0.95, str(got), SECTION)
        assert got == 0.95

    def test_mediocre_score_lowers_the_claim(self):
        """El caso que motivó el cambio: agente dice 1.0, validador 5.0."""
        c = self._c()
        got = c.calibrate_confidence(1.0, validator_score=5.0)
        check("1.0 con score 5.0 ya no se muestra como máximo", got == 0.7,
              str(got), SECTION)
        check("y la etiqueta baja a Moderado", c.label_for(got) == "Moderado",
              str(c.label_for(got)), SECTION)
        assert got == 0.7 and c.label_for(got) == "Moderado"

    def test_failed_validation_caps_confidence(self):
        c = self._c()
        got = c.calibrate_confidence(1.0, validator_score=8.0,
                                     validation_failed=True)
        check("si la validación falló, no se muestra como confiable",
              got <= c.FAILED_CAP, str(got), SECTION)
        check("y la etiqueta es Bajo", c.label_for(got) == "Bajo",
              str(c.label_for(got)), SECTION)
        assert got <= c.FAILED_CAP

    def test_sources_raise_confidence(self):
        c = self._c()
        sin = c.calibrate_confidence(0.7, validator_score=7.0, source_count=0)
        una = c.calibrate_confidence(0.7, validator_score=7.0, source_count=1)
        tres = c.calibrate_confidence(0.7, validator_score=7.0, source_count=3)
        check("grounding sube la confianza", sin < una < tres,
              f"{sin} < {una} < {tres}", SECTION)
        assert sin < una < tres

    def test_weights_are_documented_and_sum_one(self):
        c = self._c()
        check("pesos suman 1", round(c.W_VALIDATOR + c.W_AGENT, 6) == 1.0,
              f"{c.W_VALIDATOR}+{c.W_AGENT}", SECTION)
        check("el validador pesa más que el agente", c.W_VALIDATOR > c.W_AGENT,
              f"{c.W_VALIDATOR} > {c.W_AGENT}", SECTION)
        assert round(c.W_VALIDATOR + c.W_AGENT, 6) == 1.0

    def test_out_of_range_score_is_clamped(self):
        c = self._c()
        check("score 99 no rompe el rango", c.calibrate_confidence(0.5, 99) <= 0.95,
              str(c.calibrate_confidence(0.5, 99)), SECTION)
        check("score negativo no rompe el rango",
              c.calibrate_confidence(0.5, -5) >= 0.05,
              str(c.calibrate_confidence(0.5, -5)), SECTION)
        assert c.calibrate_confidence(0.5, 99) <= 0.95

    def test_agent_confidence_out_of_range_is_clamped(self):
        c = self._c()
        check("agente 5.0 → rango válido", 0.0 <= c.calibrate_confidence(5.0, 8) <= 1.0,
              str(c.calibrate_confidence(5.0, 8)), SECTION)
        assert 0.0 <= c.calibrate_confidence(5.0, 8) <= 1.0

    def test_apply_calibration_sets_value_and_label(self):
        from src.domain.entities import Response
        c = self._c()
        r = Response(content="x", task_type="general_chat", model_used="m",
                     latency_ms=1.0, confidence=1.0)
        got = c.apply_calibration(r, validator_score=5.0)
        check("confidence calibrada en el Response", r.confidence == 0.7,
              str(r.confidence), SECTION)
        check("label coherente con el número", r.confidence_label == "Moderado",
              str(r.confidence_label), SECTION)
        check("no toca otros campos", r.content == "x" and r.task_type == "general_chat",
              "campos intactos", SECTION)
        assert got == 0.7 and r.confidence_label == "Moderado"

    def test_apply_calibration_reads_sources_from_response(self):
        from src.domain.entities import Response
        c = self._c()
        r = Response(content="x", task_type="research", model_used="m",
                     latency_ms=1.0, confidence=0.6,
                     sources=["a", "b", "c"])
        c.apply_calibration(r, validator_score=7.0)
        esperado = c.calibrate_confidence(0.6, 7.0, source_count=3)
        check("usa las fuentes del Response", r.confidence == esperado,
              f"{r.confidence} == {esperado}", SECTION)
        assert r.confidence == esperado

    def test_apply_calibration_survives_broken_object(self):
        c = self._c()
        got = c.apply_calibration(SimpleNamespace(), validator_score=7.0)
        check("objeto raro no rompe", got is None or isinstance(got, float),
              str(got), SECTION)

    def test_calibration_separates_values_instead_of_flattening(self):
        """Métrica del cambio: antes TODAS las tareas simples daban 1.0."""
        c = self._c()
        agentes = {"coder": 1.0, "analista": 0.8, "chat": 1.0, "research": 0.75}
        scores = {"coder": 9.0, "analista": 6.0, "chat": 4.0, "research": 8.5}
        antes = c.calibration_summary(agentes)
        despues = c.calibration_summary({
            k: c.calibrate_confidence(agentes[k], scores[k]) for k in agentes})
        check("antes había valores repetidos (1.0, 1.0)",
              len(set(antes["valores"].values())) < len(agentes),
              str(antes["valores"]), SECTION)
        check("después hay más valores distintos (más informativo)",
              len(set(despues["valores"].values())) == len(agentes),
              str(despues["valores"]), SECTION)
        assert len(set(despues["valores"].values())) == len(agentes)

    def test_dispatcher_extracts_validator_score(self):
        """El score sale de `metadata.score` (0-10) y solo si es numérico."""
        from src.core.dispatcher_process import _extract_val_score as E
        check("metadata.score", E({"metadata": {"score": 7.5}}) == 7.5, "", SECTION)
        check("score en raíz", E({"score": 9.0}) == 9.0, "", SECTION)
        check("sin score → None", E({"metadata": {}}) is None, "", SECTION)
        check("score no numérico → None",
              E({"metadata": {"score": "alto"}}) is None, "", SECTION)
        assert E({"metadata": {"score": 7.5}}) == 7.5 and E(None) is None

    def test_nothing_branches_on_response_confidence(self):
        """La calibración es segura: nadie decide por `response.confidence`.

        Escaneo de POLÍTICA sobre todo el árbol, con AST: busca comparaciones
        (`<`, `>`, `<=`, `>=`, `==`, `!=`) cuyo operando sea
        `<algo con "response">.confidence`. Ningún resultado = calibrar la
        confianza solo cambia lo que se muestra, no el comportamiento.
        """
        sospechosos = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                for operando in (node.left, *node.comparators):
                    if isinstance(operando, ast.Attribute) and operando.attr == "confidence":
                        base = operando.value
                        nombre = (base.id if isinstance(base, ast.Name)
                                  else getattr(base, "attr", ""))
                        if "response" in nombre:
                            sospechosos.append(
                                f"{path.relative_to(PROJECT_ROOT)}:{nombre}.confidence")
        check("sin ramificaciones por response.confidence (AST)", not sospechosos,
              str(sospechosos), SECTION)
        assert not sospechosos


# ═══════════════════════════════════════════════════════════════
# Calibración extraída y testeable (ISSUE-061, bug en vivo)
# ═══════════════════════════════════════════════════════════════
# El log del usuario (12-09) mostró:
#   [degradado/contrato] ...:calibrar_confianza: UnboundLocalError:
#   cannot access local variable '_val_score' where it is not associated with a value
# `_val_score` se declaraba dentro de la rama del validador y se usaba después
# en TODOS los caminos → cuando la validación se omite (respuesta corta,
# grounding, tarea sin validación) la calibración explotaba. Al extraerla a una
# función con contrato, se testea.
class TestCalibracionExtraida:
    @staticmethod
    def _fn():
        from src.core.dispatcher_process_guard import (
            calibrate_response_confidence)
        return calibrate_response_confidence

    @staticmethod
    def _resp(conf=0.85, meta=None, sources=None):
        from src.domain.entities import Response
        return Response(content="x", task_type="general_chat",
                        model_used="qwen3:8b", latency_ms=10.0,
                        confidence=conf, sources=list(sources or []),
                        metadata=dict(meta or {}))

    def test_no_explota_sin_score_de_validador(self):
        """El caso exacto del bug: validación omitida → score None."""
        r = self._resp(conf=0.85)
        got = self._fn()(r, validator_score=None, validation_failed=False,
                         task_type="general_chat")
        check("sin score no lanza y no cambia la confianza",
              got == 0.85 and r.confidence == 0.85, f"{got}", SECTION)
        assert got == 0.85

    def test_calibra_con_score(self):
        r = self._resp(conf=1.0)
        got = self._fn()(r, validator_score=5.0, task_type="general_chat")
        check("score 5.0 baja la confianza mostrada", got == 0.7, str(got), SECTION)
        check("y queda en el Response", r.confidence == 0.7, str(r.confidence), SECTION)
        assert got == 0.7

    def test_usa_el_score_del_validation_report_como_evidencia(self):
        r = self._resp(conf=1.0, meta={"validation_report": {"score": 9.0}})
        got = self._fn()(r, validator_score=None, task_type="code_generation")
        check("toma el score del validation_report",
              got is not None and got > 0.9, str(got), SECTION)
        assert got is not None and got > 0.9

    def test_validacion_fallida_topa_la_confianza(self):
        r = self._resp(conf=1.0)
        got = self._fn()(r, validator_score=8.0, validation_failed=True,
                         task_type="general_chat")
        check("si falló la validación no se muestra confiable",
              got is not None and got <= 0.45, str(got), SECTION)
        assert got is not None and got <= 0.45

    def test_tolera_objetos_raros_sin_lanzar(self):
        got = self._fn()(SimpleNamespace(), validator_score=7.0)
        check("objeto sin confianza no rompe", got is None or isinstance(got, float),
              str(got), SECTION)
        check("None tampoco rompe", self._fn()(None, validator_score=7.0) is None,
              "", SECTION)
        assert self._fn()(None, validator_score=7.0) is None

    def test_todos_los_caminos_del_pipeline_estan_cubiertos(self):
        """Regresión del bug: la calibración debe sobrevivir a CUALQUIER
        combinación de (score, failed), incluidas las que antes no inicializaban
        las variables (`None` = validación omitida)."""
        fallos = []
        for score in (None, 0.0, 5.0, 10.0):
            for failed in (False, True):
                r = self._resp(conf=0.9)
                try:
                    self._fn()(r, validator_score=score,
                               validation_failed=failed, task_type="tutoring")
                except Exception as exc:  # noqa: BLE001
                    fallos.append(f"score={score} failed={failed}: {type(exc).__name__}")
        check("ninguna combinación lanza (incluido score=None)",
              not fallos, str(fallos), SECTION)
        assert not fallos


# ═══════════════════════════════════════════════════════════════
# Confianza honesta en consultas de DATO sin fuentes (ISSUE-063)
# ═══════════════════════════════════════════════════════════════
# Caso real del log del usuario (12-09): "¿Dónde se ubica la falla geológica en
# el valle de Calamuchita…?" → respondida SIN fuentes, validada con 9.0 (el
# validador juzga el texto) y mostrada como "Alto (88%)".
class TestConfianzaHonestaSinFuentes:
    @staticmethod
    def _fn():
        from src.core.dispatcher_process_guard import (
            calibrate_response_confidence, NO_SOURCES_FACTUAL_CAP)
        return calibrate_response_confidence, NO_SOURCES_FACTUAL_CAP

    @staticmethod
    def _detect():
        from src.core.dispatcher_handlers import expects_factual_grounding
        return expects_factual_grounding

    def test_detecta_consulta_de_dato_real(self):
        q = ("Donde  se ubica la falla geologica  en el valle de Calamuchita  "
             "en provincia de Cordoba Argentina?")
        check("detecta la consulta real del log (con doble espacio)",
              self._detect()(q) is True, str(self._detect()(q)), SECTION)
        check("detecta 'quién es'", self._detect()("Quien es el autor de Rayuela?"),
              "", SECTION)
        check("detecta 'cuál es la capital'",
              self._detect()("Cual es la capital de Francia?"), "", SECTION)
        assert self._detect()(q) is True

    def test_no_marca_conversacion_ni_creatividad(self):
        d = self._detect()
        for q in ("Que es Python?", "Explicame como funciona el dispatcher",
                  "Contame un chiste", "Escribime un poema de amor",
                  "Que pronostico tenemos para hoy?"):
            check(f"no marca: {q[:38]}", d(q) is False, str(d(q)), SECTION)
        assert d("Que es Python?") is False and d("Contame un chiste") is False

    def test_topa_la_confianza_si_no_hay_fuentes(self):
        fn, cap = self._fn()
        from src.domain.entities import Response
        r = Response(content="x", task_type="general_chat", model_used="qwen3:8b",
                     latency_ms=10.0, confidence=0.85, sources=[])
        got = fn(r, validator_score=9.0, expects_grounding=True,
                 task_type="general_chat")
        check(f"con score 9.0 pero 0 fuentes queda en {cap}", got == cap,
              str(got), SECTION)
        check("y la etiqueta baja de Alto", r.confidence_label != "Alto",
              str(r.confidence_label), SECTION)
        assert got == cap

    def test_con_fuentes_no_topa(self):
        fn, cap = self._fn()
        from src.domain.entities import Response
        r = Response(content="x", task_type="research", model_used="qwen3:8b",
                     latency_ms=10.0, confidence=0.85,
                     sources=["Doc - https://a.com"])
        got = fn(r, validator_score=9.0, expects_grounding=True, task_type="research")
        check("con una fuente real no se topa", got is not None and got > cap,
              str(got), SECTION)
        assert got is not None and got > cap

    def test_no_topa_si_no_era_consulta_de_dato(self):
        fn, cap = self._fn()
        from src.domain.entities import Response
        r = Response(content="x", task_type="general_chat", model_used="qwen3:8b",
                     latency_ms=10.0, confidence=0.85, sources=[])
        got = fn(r, validator_score=9.0, expects_grounding=False)
        check("charla normal sin fuentes mantiene su confianza",
              got is not None and got > cap, str(got), SECTION)
        assert got is not None and got > cap

    def test_validacion_fallida_sigue_mandando(self):
        fn, cap = self._fn()
        from src.domain.entities import Response
        r = Response(content="x", task_type="general_chat", model_used="qwen3:8b",
                     latency_ms=10.0, confidence=0.9, sources=[])
        got = fn(r, validator_score=8.0, validation_failed=True,
                 expects_grounding=True)
        check("si falló la validación el techo es el de fallo (0.45)",
              got is not None and got <= 0.45, str(got), SECTION)
        assert got is not None and got <= 0.45


def test_estado_de_validacion_en_curso_no_miente():
    """`ISSUE-136`: con la validación en segundo plano, el estado debe decir "en curso".

    Antes, una respuesta dibujada antes del veredicto se mostraba como
    "no_aplicada" (mentira por omisión); ahora se distinguen las dos cosas.
    """
    from src.interfaces.web.app_logic import _validation_state
    assert _validation_state(False, {"validation_en_fondo": True}) == "en curso"
    completa = {"validation_en_fondo": True, "validation_en_fondo_completada": True,
                "validation_score": 10.0}
    assert _validation_state(False, completa) == "aprobada"
    assert _validation_state(False, {**completa, "validation_score": 2.0,
                                     "validation_failed": True}) == "fallida"
    assert _validation_state(False, {"validation_skipped_short": True}) == "omitida"

