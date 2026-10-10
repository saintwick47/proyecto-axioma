import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
import pytest


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _rx():
    import src.core.research_export as m
    return m


def _dlg():
    import src.interfaces.components.export_dialog as m
    return m


@dataclass
class _Msg:
    """Doble de MessageData (duck-typing: solo role/content + opcionales)."""
    role: str
    content: str
    timestamp: str = ""
    source: str = "llm"
    task_type: str = ""
    sources: list = field(default_factory=list)


def _pairs():
    return [
        _Msg("user", "¿Qué es AXIOMA?"),
        _Msg("assistant", "Es un agente local.", "2026-01-01T10:00",
             task_type="research", sources=["Doc A - https://a.com"]),
        _Msg("system", "ruido de sistema"),
        _Msg("user", "¿Y el clima?"),
        _Msg("assistant", "22 grados.", "2026-01-01T10:05",
             task_type="weather", sources=["Clima - https://b.com",
                                           "Doc A - https://a.com"]),
    ]


class TestCollectEntries:
    def test_pairs_assistant_with_previous_question(self):
        entries = _rx().collect_entries(_pairs())
        assert [e.question for e in entries] == ["¿Qué es AXIOMA?", "¿Y el clima?"]
        assert [e.answer for e in entries] == ["Es un agente local.", "22 grados."]

    def test_system_messages_are_ignored(self):
        entries = _rx().collect_entries(_pairs())
        assert all("ruido" not in e.question and "ruido" not in e.answer
                   for e in entries)

    def test_sources_deduplicated_preserving_order(self):
        entries = _rx().collect_entries(_pairs())
        assert entries[1].sources == ["Clima - https://b.com", "Doc A - https://a.com"]

    def test_filters_non_string_and_blank_sources(self):
        msgs = [_Msg("assistant", "ok", sources=["  ", None, 42, "valida"])]
        assert _rx().collect_entries(msgs)[0].sources == ["valida"]

    def test_missing_sources_attribute_is_tolerated(self):
        class _Pelado:
            role = "assistant"
            content = "sin atributos extra"
        entries = _rx().collect_entries([_Pelado()])
        assert entries[0].sources == [] and entries[0].answer == "sin atributos extra"

    def test_empty_and_none_inputs(self):
        assert _rx().collect_entries([]) == []
        assert _rx().collect_entries(None) == []

    def test_question_is_not_reused_for_second_answer(self):
        msgs = [_Msg("user", "p1"), _Msg("assistant", "a1"),
                _Msg("assistant", "a2 sin pregunta")]
        entries = _rx().collect_entries(msgs)
        assert entries[0].question == "p1"
        assert entries[1].question == ""   # no arrastra la pregunta anterior


class TestSummaryAndNames:
    def test_summary_counts(self):
        stats = _rx().export_summary(_rx().collect_entries(_pairs()))
        assert stats == {"answers": 2, "with_citations": 2, "sources": 2}

    def test_summary_counts_answers_without_citations(self):
        msgs = [_Msg("user", "q"), _Msg("assistant", "sin citas")]
        stats = _rx().export_summary(_rx().collect_entries(msgs))
        assert stats == {"answers": 1, "with_citations": 0, "sources": 0}

    def test_filename_sanitizes_session_id(self):
        name = _rx().suggested_filename("../../etc/passwd", "md", when="20260101_000000")
        assert "/" not in name and ".." not in name
        # 8 primeros chars = '../../et' → solo alfanuméricos quedan
        assert name == "investigacion_et_20260101_000000.md"

    def test_filename_extension_per_format(self):
        assert _rx().suggested_filename("s", "html").endswith(".html")
        assert _rx().suggested_filename("s", "md").endswith(".md")

    def test_filename_rejects_unknown_format(self):
        with pytest.raises(ValueError):
            _rx().suggested_filename("s", "pdf")

    def test_filename_without_session(self):
        assert _rx().suggested_filename("", "md").startswith("investigacion_sesion_")


class TestMarkdown:
    def test_header_has_session_model_and_counts(self):
        md = _rx().build_markdown(_rx().collect_entries(_pairs()), "abc123",
                                  {"model": "qwen3:8b"},
                                  generated_at="2026-01-01 00:00:00")
        assert "# 🔎 Investigación — AXIOMA" in md
        assert "`abc123`" in md and "`qwen3:8b`" in md
        assert "**Respuestas:** 2 (2 con citas)" in md
        assert "**Fuentes únicas:** 2" in md

    def test_each_answer_numbered_with_sources(self):
        md = _rx().build_markdown(_rx().collect_entries(_pairs()), "s")
        assert "## 1. ¿Qué es AXIOMA?" in md
        assert "## 2. ¿Y el clima?" in md
        assert "### Fuentes (1)" in md and "### Fuentes (2)" in md

    def test_source_index_lists_unique_urls_once(self):
        md = _rx().build_markdown(_rx().collect_entries(_pairs()), "s")
        idx = md.split("## 📚 Índice de fuentes")[1]
        assert idx.count("https://a.com") == 1
        assert idx.count("https://b.com") == 1

    def test_empty_session_is_explicit(self):
        md = _rx().build_markdown([], "s")
        assert "No hay respuestas en esta sesión todavía." in md

    def test_missing_question_is_labelled(self):
        msgs = [_Msg("assistant", "respuesta huérfana")]
        md = _rx().build_markdown(_rx().collect_entries(msgs), "s")
        assert "(sin pregunta registrada)" in md

    def test_answers_without_sources_omit_sources_block(self):
        msgs = [_Msg("user", "q"), _Msg("assistant", "sin citas")]
        md = _rx().build_markdown(_rx().collect_entries(msgs), "s")
        assert "### Fuentes" not in md
        assert "## 📚 Índice de fuentes" not in md


class TestHtmlSecurity:
    def test_document_is_standalone_and_printable(self):
        html = _rx().build_html(_rx().collect_entries(_pairs()), "s")
        assert html.startswith("<!DOCTYPE html>")
        assert "<meta charset=\"utf-8\">" in html
        assert "@media print" in html            # PDF por navegador
        assert "Guardar como PDF" in html

    def test_script_in_answer_is_escaped(self):
        msgs = [_Msg("user", "q"), _Msg("assistant", "<script>alert(1)</script>")]
        html = _rx().build_html(_rx().collect_entries(msgs), "s")
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_inline_html_in_answer_is_neutralised(self):
        msgs = [_Msg("user", "q"), _Msg("assistant", "texto <b>crudo</b> y <img src=x>")]
        html = _rx().build_html(_rx().collect_entries(msgs), "s")
        assert "<b>crudo</b>" not in html
        assert "<img src=x>" not in html
        assert "&lt;b&gt;crudo&lt;/b&gt;" in html

    def test_question_and_source_titles_are_escaped(self):
        msgs = [_Msg("user", "<script>q</script>"),
                _Msg("assistant", "a", sources=["<img src=x onerror=alert(1)>"])]
        html = _rx().build_html(_rx().collect_entries(msgs), "s")
        assert "<script>q" not in html
        assert "<img src=x" not in html

    def test_markdown_subset_renders(self):
        answer = ("Parrafo con **negrita** y `codigo`.\n\n"
                  "- item uno\n- item dos\n\n"
                  "```python\nprint(1)\n```\n\n"
                  "Ver [doc](https://x.com/a)")
        msgs = [_Msg("user", "q"), _Msg("assistant", answer)]
        html = _rx().build_html(_rx().collect_entries(msgs), "s")
        assert "<strong>negrita</strong>" in html
        assert "<code>codigo</code>" in html
        assert "<li>item uno</li>" in html
        assert "<pre><code>" in html
        assert 'href="https://x.com/a"' in html

    def test_ampersands_are_escaped(self):
        msgs = [_Msg("user", "q"), _Msg("assistant", "a & b")]
        html = _rx().build_html(_rx().collect_entries(msgs), "s")
        assert "a &amp; b" in html

    def test_empty_session_html(self):
        html = _rx().build_html([], "s")
        assert "No hay respuestas en esta sesión todavía." in html


class TestSecretScrubbing:
    def test_api_key_in_answer_is_not_written(self, monkeypatch):
        from config.settings import settings
        monkeypatch.setattr(settings, "serpapi_api_key", "SUPERSECRET123",
                            raising=False)
        msgs = [_Msg("user", "q"),
                _Msg("assistant", "la key es SUPERSECRET123 ok")]
        entries = _rx().collect_entries(msgs)
        assert "SUPERSECRET123" not in _rx().build_markdown(entries, "s")
        assert "SUPERSECRET123" not in _rx().build_html(entries, "s")
        assert "***" in _rx().build_markdown(entries, "s")


class TestWritingFiles:
    def test_export_research_writes_markdown_and_html(self, tmp_path):
        entries = _rx().collect_entries(_pairs())
        md = _rx().export_research(entries, "ses1", "md", out_dir=tmp_path)
        html = _rx().export_research(entries, "ses1", "html", out_dir=tmp_path)
        assert md.exists() and md.suffix == ".md" and "ses1" in md.name
        assert html.exists() and html.suffix == ".html"
        # el archivo CONTIENE lo que generan los builders (misma fuente)
        text = md.read_text(encoding="utf-8")
        assert "## 1. ¿Qué es AXIOMA?" in text
        assert "### Fuentes (1)" in text
        assert html.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_default_destination_is_session_workspace(self):
        """Sin out_dir el destino es el workspace de la sesión (no el CWD)."""
        from src.core.workspace_manager import get_workspace_manager
        entries = _rx().collect_entries(_pairs())
        path = _rx().export_research(entries, "f3_workspace_probe", "md")
        try:
            assert path.parent == Path(
                get_workspace_manager().get_workspace("f3_workspace_probe"))
            assert Path.cwd() not in path.parents or ".axioma" in str(path)
        finally:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()   # no dejar workspaces de prueba colgando
            except OSError as _d2e:
                import logging
                logging.getLogger(__name__).debug(f"[F3 test] {_d2e}")

    def test_export_all_returns_both_formats(self, tmp_path):
        out = _rx().export_all(_rx().collect_entries(_pairs()), "ses1",
                               out_dir=tmp_path)
        assert set(out) == {"md", "html"}
        assert all(Path(p).exists() for p in out.values())

    def test_export_rejects_unknown_format(self, tmp_path):
        with pytest.raises(ValueError):
            _rx().export_research([], "s", "pdf", out_dir=tmp_path)

    def test_export_creates_missing_directory(self, tmp_path):
        target = tmp_path / "nuevo" / "sub"
        path = _rx().export_research(_rx().collect_entries(_pairs()), "s",
                                     "md", out_dir=target)
        assert path.parent == target and path.exists()


class TestStateRoundTrip:
    def _state(self):
        from src.interfaces.web.states import SessionState
        st = SessionState()
        st.session_id = "roundtrip1"
        st.model = "qwen3:8b"
        return st

    def test_add_message_persists_sources(self, tmp_path):
        st = self._state()
        st.add_message("user", "¿Qué es AXIOMA?")
        st.add_message("assistant", "Un agente local.", task_type="research",
                       sources=["Doc - https://a.com"])
        # siempre con out_dir=tmp_path: nunca escribir en el CWD del repo
        exported = _dlg().export_session_research(st, "md", out_dir=tmp_path)
        assert exported is not None and exported.exists()
        assert "Doc - https://a.com" in exported.read_text(encoding="utf-8")
        md = _rx().build_markdown(_rx().collect_entries(st.messages), st.session_id)
        assert "Doc - https://a.com" in md
        assert "### Fuentes (1)" in md

    def test_add_message_filters_blank_sources(self):
        st = self._state()
        msg = st.add_message("assistant", "x", sources=["  ", "", None])
        assert msg.sources == []

    def test_message_to_dict_includes_sources(self):
        st = self._state()
        msg = st.add_message("assistant", "x", sources=["u - https://u.com"])
        assert msg.to_dict()["sources"] == ["u - https://u.com"]


class TestExportDialogLogic:
    def test_no_state_returns_none(self, tmp_path):
        assert _dlg().export_session_research(None, "md", out_dir=tmp_path) is None

    def test_state_without_answers_returns_none(self, tmp_path):
        from src.interfaces.web.states import SessionState
        st = SessionState()
        st.add_message("user", "solo pregunta")
        assert _dlg().export_session_research(st, "md", out_dir=tmp_path) is None

    def test_invalid_format_returns_none(self, tmp_path):
        from src.interfaces.web.states import SessionState
        st = SessionState()
        st.add_message("user", "q")
        st.add_message("assistant", "a")
        assert _dlg().export_session_research(st, "pdf", out_dir=tmp_path) is None

    def test_summary_text_before_and_after(self):
        from src.interfaces.web.states import SessionState
        st = SessionState()
        assert "no hay respuestas" in _dlg()._summary_text(st).lower()
        st.add_message("user", "q")
        st.add_message("assistant", "a", sources=["D - https://d.com"])
        txt = _dlg()._summary_text(st)
        assert "1 respuesta(s)" in txt and "1 con citas" in txt

    def test_summary_text_without_state(self):
        assert "no hay respuestas" in _dlg()._summary_text(None).lower()


class TestWiring:
    def test_app_consumes_factory_by_identity(self):
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        mod = _dlg()
        assert getattr(app, "create_export_dialog") is mod.create_export_dialog
        assert callable(getattr(app, "_on_export_click", None))

    def test_click_handler_is_sync_and_survives_missing_dialog(self):
        """Sync a propósito (IO local) y tolerante a diálogo no montado."""
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        assert not inspect.iscoroutinefunction(app._on_export_click)
        original = app._export_dialog
        try:
            app._export_dialog = None
            app._on_export_click()      # no debe lanzar
        finally:
            app._export_dialog = original

    def test_dialog_exposes_refresh_and_has_no_timer(self):
        src = inspect.getsource(_dlg()._build_export_dialog)
        assert "_refresh" in src
        assert "ui.timer" not in src, "no debe refrescar solo (CPU-only)"
        assert "dialog.on('open'" not in src
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── Fusionado desde test_axioma_27_sesiones.py ──
import asyncio
import json
from datetime import datetime, timedelta, timezone
try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SECTION_2 = "27. Gestor de sesiones (ISSUE-054)"


def _sd():
    from src.interfaces.components import sessions_dialog
    return sessions_dialog


class _MsgSes:
    """Doble de `MessageData` (to_dict como el real)."""

    def __init__(self, role, content, sources=None, ts="2026-01-01T10:00:00+00:00"):
        # OJO: sin coerción — el doble debe poder representar datos RAROS
        # (p. ej. `sources` como string) para probar la defensa del exportador.
        self._d = {"role": role, "content": content, "timestamp": ts,
                   "source": "llm", "task_type": None,
                   "sources": sources if sources is not None else []}

    def to_dict(self):
        return dict(self._d)


def _sessions(now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "aaa111": {"session_id": "aaa111", "message_count": 5,
                   "model": "qwen3:8b", "temperature": 0.7,
                   "created_at": (now - timedelta(hours=3)).isoformat(),
                   "last_activity": (now - timedelta(minutes=2)).isoformat(),
                   "user_id": "usuario", "jarvis_mode": "normal"},
        "bbb222": {"session_id": "bbb222", "message_count": 0,
                   "model": "qwen2.5-coder:7b", "temperature": 0.2,
                   "created_at": (now - timedelta(days=2)).isoformat(),
                   "last_activity": (now - timedelta(days=1)).isoformat(),
                   "is_processing": True},
        "ccc333": {"session_id": "ccc333", "message_count": 1,
                   "model": "qwen3:8b", "temperature": 0.5,
                   "created_at": (now - timedelta(hours=1)).isoformat(),
                   "last_activity": (now - timedelta(minutes=30)).isoformat(),
                   "user_id": "José"},
    }


class TestFilterAndSort:
    def test_sorted_by_last_activity_desc(self):
        ids = _sd().filter_sessions(_sessions())
        check("más reciente primero", ids == ["aaa111", "ccc333", "bbb222"],
              str(ids), SECTION_2)
        assert ids == ["aaa111", "ccc333", "bbb222"]

    def test_search_by_model(self):
        ids = _sd().filter_sessions(_sessions(), "qwen2")
        check("busca por modelo", ids == ["bbb222"], str(ids), SECTION_2)
        assert ids == ["bbb222"]

    def test_search_by_user(self):
        ids = _sd().filter_sessions(_sessions(), "usuario")
        check("busca por usuario", ids == ["aaa111"], str(ids), SECTION_2)
        assert ids == ["aaa111"]

    def test_search_is_accent_insensitive(self):
        ids = _sd().filter_sessions(_sessions(), "jose")
        check("'jose' encuentra a 'José'", ids == ["ccc333"], str(ids), SECTION_2)
        assert ids == ["ccc333"]

    def test_search_by_id_prefix(self):
        ids = _sd().filter_sessions(_sessions(), "ccc")
        check("busca por id", ids == ["ccc333"], str(ids), SECTION_2)
        assert ids == ["ccc333"]

    def test_empty_query_returns_all(self):
        check("sin búsqueda devuelve todo",
              len(_sd().filter_sessions(_sessions(), "")) == 3, "", SECTION_2)
        assert len(_sd().filter_sessions(_sessions(), "")) == 3

    def test_no_match_returns_empty(self):
        check("sin coincidencias → lista vacía",
              _sd().filter_sessions(_sessions(), "zzzz") == [], "", SECTION_2)
        assert _sd().filter_sessions(_sessions(), "zzzz") == []

    def test_tolerates_broken_input(self):
        check("None no rompe", _sd().filter_sessions(None, "x") == [], "", SECTION_2)
        check("entradas no-dict se ignoran",
              _sd().filter_sessions({"x": "no-dict"}, "") == [], "", SECTION_2)
        assert _sd().filter_sessions(None) == []


class TestDeleteProtection:
    def test_cannot_delete_current_session(self):
        permitido, motivo = _sd().can_delete("aaa111", "aaa111")
        check("no se puede borrar la sesión en uso", permitido is False,
              str(permitido), SECTION_2)
        check("explica por qué", "actual" in motivo, motivo, SECTION_2)
        assert permitido is False and "actual" in motivo

    def test_can_delete_other_session(self):
        permitido, motivo = _sd().can_delete("bbb222", "aaa111")
        check("se puede borrar otra sesión", permitido is True, str(permitido), SECTION_2)
        check("sin motivo", motivo == "", motivo, SECTION_2)
        assert permitido is True and motivo == ""

    def test_can_delete_without_current(self):
        check("sin sesión actual se permite",
              _sd().can_delete("bbb222", None)[0] is True, "", SECTION_2)
        assert _sd().can_delete("bbb222", None)[0] is True

    def test_invalid_session_id_rejected(self):
        check("id vacío → no", _sd().can_delete("", "aaa111")[0] is False, "", SECTION_2)
        check("id None → no", _sd().can_delete(None, "aaa")[0] is False, "", SECTION_2)
        assert _sd().can_delete("", None)[0] is False


class TestRowsAndTime:
    def test_rows_include_core_fields(self):
        filas = _sd().session_rows(_sessions()["aaa111"])
        flat = " | ".join(f"{a}: {b}" for a, b, _ in filas)
        check("id", "aaa111" in flat, flat[:80], SECTION_2)
        check("mensajes", "5 mensaje(s)" in flat, flat[:80], SECTION_2)
        check("modelo y temperatura", "qwen3:8b" in flat and "0.7" in flat,
              flat[:80], SECTION_2)
        check("actividad relativa", "hace 2 min" in flat, flat[:80], SECTION_2)
        assert "5 mensaje(s)" in flat

    def test_rows_flag_processing_and_files(self):
        info = dict(_sessions()["bbb222"], uploaded_files_count=2,
                    jarvis_mode="jarvis")
        flat = " | ".join(b for _, b, _ in _sd().session_rows(info))
        check("marca procesando", "procesando" in flat, flat[:90], SECTION_2)
        check("cuenta archivos", "2 archivo(s)" in flat, flat[:90], SECTION_2)
        check("muestra modo jarvis", "jarvis" in flat, flat[:90], SECTION_2)
        assert "procesando" in flat

    def test_rows_with_empty_info_does_not_break(self):
        filas = _sd().session_rows({})
        check("info vacía produce filas", len(filas) >= 3, str(len(filas)), SECTION_2)
        assert len(filas) >= 3

    def test_relative_time_buckets(self):
        now = datetime.now(timezone.utc)
        rt = _sd().relative_time
        check("segundos", rt((now - timedelta(seconds=5)).isoformat(), now) == "hace segundos",
              rt((now - timedelta(seconds=5)).isoformat(), now), SECTION_2)
        check("minutos", rt((now - timedelta(minutes=5)).isoformat(), now) == "hace 5 min",
              rt((now - timedelta(minutes=5)).isoformat(), now), SECTION_2)
        check("horas", rt((now - timedelta(hours=5)).isoformat(), now) == "hace 5 h",
              rt((now - timedelta(hours=5)).isoformat(), now), SECTION_2)
        check("días", rt((now - timedelta(days=3)).isoformat(), now) == "hace 3 d",
              rt((now - timedelta(days=3)).isoformat(), now), SECTION_2)
        check("inválido → —", rt("no-es-fecha", now) == "—", rt("x", now), SECTION_2)
        check("None → —", rt(None, now) == "—", "", SECTION_2)
        assert rt((now - timedelta(days=3)).isoformat(), now) == "hace 3 d"


class TestExports:
    def test_markdown_has_both_roles_and_sources(self):
        msgs = [_MsgSes("user", "¿Qué es AXIOMA?"),
                _MsgSes("assistant", "Un agente local.",
                     sources=["Doc - https://a.com"])]
        md = _sd().build_session_markdown(_sessions()["aaa111"], msgs)
        check("incluye la pregunta del usuario", "¿Qué es AXIOMA?" in md,
              md[:80], SECTION_2)
        check("incluye la respuesta", "Un agente local." in md, md[:80], SECTION_2)
        check("incluye fuentes", "### Fuentes (1)" in md, md[:80], SECTION_2)
        check("cabecera con la sesión", "`aaa111`" in md, md[:80], SECTION_2)
        assert "¿Qué es AXIOMA?" in md and "### Fuentes (1)" in md

    def test_markdown_without_messages_says_so(self):
        md = _sd().build_session_markdown({}, [])
        check("avisa que no hay mensajes", "no tiene mensajes" in md,
              md[-60:], SECTION_2)
        assert "no tiene mensajes" in md

    def test_markdown_normalizes_string_sources(self):
        """Datos viejos con `sources` string no deben partirse por carácter."""
        md = _sd().build_session_markdown({}, [_MsgSes("assistant", "x",
                                                    sources="Doc - https://a.com")])
        check("una fuente, no 17", "### Fuentes (1)" in md,
              str([l for l in md.splitlines() if l.startswith("###")][:2]), SECTION_2)
        assert "### Fuentes (1)" in md

    def test_json_export_has_messages_and_metadata(self):
        msgs = [_MsgSes("user", "hola"), _MsgSes("assistant", "buenas")]
        data = json.loads(_sd().build_session_json(_sessions()["aaa111"], msgs))
        check("metadata de la sesión", data["session"]["model"] == "qwen3:8b",
              str(data["session"].get("model")), SECTION_2)
        check("mensajes completos", len(data["messages"]) == 2,
              str(len(data["messages"])), SECTION_2)
        check("cuenta coherente", data["message_count"] == 2,
              str(data["message_count"]), SECTION_2)
        assert data["message_count"] == 2 and data["messages"][0]["role"] == "user"

    def test_export_writes_both_formats(self, tmp_path):
        from src.interfaces.web.states import SessionManager
        mgr = SessionManager()
        sid = "test_export_tmp"
        st = mgr.get_or_create_session(sid)
        try:
            st.add_message("user", "hola")
            st.add_message("assistant", "buenas", sources=["Doc - https://a.com"])
            md = _sd().export_session(sid, "md", out_dir=tmp_path)
            js = _sd().export_session(sid, "json", out_dir=tmp_path)
            check("escribe MD", md is not None and md.exists(), str(md), SECTION_2)
            check("escribe JSON", js is not None and js.exists(), str(js), SECTION_2)
            check("el MD contiene la conversación",
                  "hola" in md.read_text(encoding="utf-8"),
                  md.name, SECTION_2)
            assert md.exists() and js.exists()
        finally:
            mgr.delete_session(sid)      # limpieza: no dejar sesiones de prueba

    def test_export_invalid_format_returns_none(self, tmp_path):
        check("formato desconocido → None",
              _sd().export_session("x", "pdf", out_dir=tmp_path) is None,
              "", SECTION_2)
        assert _sd().export_session("x", "pdf", out_dir=tmp_path) is None

    def test_export_never_touches_real_workspace(self, tmp_path):
        """Regresión: `out_dir` debe respetarse (los tests no ensucian ~/.axioma)."""
        from src.interfaces.web.states import SessionManager
        mgr = SessionManager()
        sid = "test_nows_tmp"
        mgr.get_or_create_session(sid).add_message("user", "x")
        try:
            p = _sd().export_session(sid, "md", out_dir=tmp_path)
            check("escribe en out_dir", p is not None and p.parent == tmp_path,
                  str(p.parent if p else None), SECTION_2)
            assert p is not None and p.parent == tmp_path
        finally:
            mgr.delete_session(sid)


class TestWiringSesiones:
    def test_app_uses_the_factory(self):
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        mod = _sd()
        check("app importa la factory",
              getattr(app, "create_sessions_dialog", None) is mod.create_sessions_dialog,
              "identidad", SECTION_2)
        check("app expone el global", hasattr(app, "_sessions_dialog"), "", SECTION_2)
        check("handler del botón", callable(getattr(app, "_on_sessions_click", None)),
              "", SECTION_2)
        assert getattr(app, "create_sessions_dialog", None) is mod.create_sessions_dialog

    def test_dialog_has_no_timer(self):
        src = inspect.getsource(_sd()._build_sessions_dialog)
        check("sin ui.timer (R7)", "ui.timer" not in src, "presente", SECTION_2)
        check("expone _refresh", "_refresh" in src, "presente", SECTION_2)
        assert "ui.timer" not in src and "_refresh" in src

    def test_dialog_renders_and_lists_sessions(self, tmp_path):
        """E2E: el diálogo lista sesiones reales y protege la sesión en uso."""
        from nicegui.testing.user_simulation import user_simulation
        main = tmp_path / "sessions_main.py"
        main.write_text(f'''\
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from nicegui import ui
from src.interfaces.web.states import SessionManager
from src.interfaces.components.sessions_dialog import create_sessions_dialog


@ui.page('/')
async def home():
    mgr = SessionManager()
    actual = mgr.get_or_create_session('e2e_actual')
    actual.add_message('user', 'consulta de prueba')
    otra = mgr.get_or_create_session('e2e_otra')
    otra.add_message('user', 'otra consulta')
    d = create_sessions_dialog(actual)
    d.open()
    d._refresh()


ui.run(port=8188, show=False, reload=False, storage_secret='test-secret')
''', encoding="utf-8")

        async def scenario():
            async with user_simulation(main_file=str(main)) as user:
                await user.open('/')
                await user.should_see('🗂️ Sesiones')
                await user.should_see('tu sesión')          # la actual marcada
                await user.should_see('e2e_otra')           # la otra listada
                await user.should_see('2 sesión(es) activa(s)')

        try:
            asyncio.run(scenario())
            ok, detalle = True, "diálogo lista y marca la sesión actual"
        except Exception as e:  # noqa: BLE001
            ok, detalle = False, f"{type(e).__name__}: {e}"
        check("el diálogo renderiza sesiones en la UI real", ok, detalle, SECTION_2)
        assert ok, detalle


class TestArtifactPersistence:
    def test_sanitize_keeps_valid_entries(self):
        from src.interfaces.web.states import _sanitize_artifacts
        got = _sanitize_artifacts([
            {"name": "a.py", "content": "print(1)", "language": "python"},
            {"content": "sin nombre"},
            "basura", None,
            {"name": "b.py", "content": "   "},
        ])
        check("solo entradas válidas", len(got) == 1, str(got), SECTION_2)
        check("conserva lenguaje", got[0]["language"] == "python", str(got[0]), SECTION_2)
        assert len(got) == 1 and got[0]["name"] == "a.py"

    def test_sanitize_truncates_big_files(self):
        from src.interfaces.web.states import (_sanitize_artifacts,
                                               ARTIFACT_CONTENT_MAX)
        got = _sanitize_artifacts([{"name": "big.py",
                                    "content": "a" * (ARTIFACT_CONTENT_MAX + 50)}])
        check(f"trunca a {ARTIFACT_CONTENT_MAX}",
              len(got[0]["content"]) == ARTIFACT_CONTENT_MAX, str(len(got[0]["content"])),
              SECTION_2)
        check("marca el truncado", got[0].get("truncated") is True, str(got[0].get("truncated")),
              SECTION_2)
        assert got[0]["truncated"] is True

    def test_sanitize_limits_file_count(self):
        from src.interfaces.web.states import (_sanitize_artifacts,
                                               ARTIFACT_MAX_FILES)
        got = _sanitize_artifacts([{"name": f"f{i}.py", "content": "x"}
                                   for i in range(ARTIFACT_MAX_FILES + 10)])
        check("respeta el tope de archivos", len(got) == ARTIFACT_MAX_FILES,
              str(len(got)), SECTION_2)
        assert len(got) == ARTIFACT_MAX_FILES

    def test_message_stores_artifacts_and_serializes_them(self):
        from src.interfaces.web.states import SessionState
        st = SessionState()
        msg = st.add_message("assistant", "ok",
                             artifacts=[{"name": "a.py", "content": "x=1",
                                         "language": "python"}])
        check("guardado en el mensaje", len(msg.artifacts) == 1,
              str(msg.artifacts), SECTION_2)
        check("to_dict lo incluye", msg.to_dict()["artifacts"][0]["name"] == "a.py",
              str(msg.to_dict().get("artifacts")), SECTION_2)
        assert msg.to_dict()["artifacts"][0]["name"] == "a.py"

    def test_compact_code_artifact_maps_contract(self):
        from src.interfaces.web.app_logic import compact_code_artifact
        art = {"files": [{"name": "app.py", "content": "x", "language": "python"}],
               "test_files": [{"name": "test_app.py", "content": "y",
                               "language": "python"}]}
        got = compact_code_artifact(art)
        check("código + tests", len(got) == 2, str(got), SECTION_2)
        check("marca el test", got[1]["is_test"] is True, str(got[1]), SECTION_2)
        check("basura → []", compact_code_artifact(None) == []
              and compact_code_artifact("x") == [], "", SECTION_2)
        assert len(got) == 2 and got[1]["is_test"] is True


class TestConversationExport:
    def _msgs(self):
        return [_MsgSes("user", "generá un script"),
                _MsgSes("assistant", "Listo.", sources=["Doc - https://a.com"])]

    def test_html_document_is_standalone_and_printable(self):
        html = _sd().build_session_html(_sessions()["aaa111"], self._msgs())
        check("doctype", html.startswith("<!DOCTYPE html>"), html[:20], SECTION_2)
        check("CSS de impresión", "@media print" in html, "", SECTION_2)
        check("incluye la pregunta y la respuesta",
              "generá un script" in html and "Listo." in html, "", SECTION_2)
        # ⚠️ La URL se **linkifica** (`<a href=…>`), así que el texto exacto
        # "Doc - https://a.com" NO aparece literal: se verifica lo que sí debe
        # estar (el enlace y el encabezado de fuentes).
        check("incluye el enlace de la fuente", "https://a.com" in html,
              str([l for l in html.splitlines() if "a.com" in l][:1]), SECTION_2)
        check("incluye el encabezado de fuentes", "Fuentes (1)" in html, "", SECTION_2)
        assert html.startswith("<!DOCTYPE html>") and "@media print" in html
        assert "https://a.com" in html and "Fuentes (1)" in html

    def test_html_escapes_untrusted_content(self):
        """El HTML del LLM y del código NO debe inyectarse."""
        msgs = [_MsgSes("assistant", "<script>alert(1)</script>",
                     sources=["<img src=x onerror=alert(1)>"])]
        html = _sd().build_session_html({"session_id": "s"}, msgs)
        check("script escapado", "<script>alert" not in html
              and "&lt;script&gt;" in html, "escapado", SECTION_2)
        check("img escapada", "<img src=x" not in html, "escapado", SECTION_2)
        assert "<script>alert" not in html

    def test_markdown_includes_code_artifacts(self):
        msg = _MsgSes("assistant", "acá está")
        msg._d["artifacts"] = [
            {"name": "s.py", "language": "python", "content": "print(1)"},
            {"name": "test_s.py", "language": "python", "content": "def test(): pass",
             "is_test": True},
        ]
        md = _sd().build_session_markdown({"session_id": "s"}, [msg])
        check("artefacto listado", "### 🧩 s.py" in md, md[-200:], SECTION_2)
        check("fence con lenguaje", "```python" in md, "", SECTION_2)
        check("test marcado", "(test)" in md, "", SECTION_2)
        assert "### 🧩 s.py" in md and "print(1)" in md

    def test_markdown_flags_truncated_artifact(self):
        msg = _MsgSes("assistant", "x")
        msg._d["artifacts"] = [{"name": "big.py", "language": "python",
                                "content": "a", "truncated": True}]
        md = _sd().build_session_markdown({"session_id": "s"}, [msg])
        check("avisa el truncado", "truncado" in md, md[-120:], SECTION_2)
        assert "truncado" in md

    def test_json_includes_artifacts(self):
        msg = _MsgSes("assistant", "x")
        msg._d["artifacts"] = [{"name": "s.py", "language": "python", "content": "1"}]
        data = json.loads(_sd().build_session_json({"session_id": "s"}, [msg]))
        check("artefactos en el JSON",
              data["messages"][0]["artifacts"][0]["name"] == "s.py",
              str(data["messages"][0].get("artifacts")), SECTION_2)
        assert data["messages"][0]["artifacts"][0]["name"] == "s.py"

    def test_export_writes_html(self, tmp_path):
        from src.interfaces.web.states import SessionManager
        mgr = SessionManager()
        sid = "test_html_tmp"
        mgr.get_or_create_session(sid).add_message("user", "hola")
        try:
            p = _sd().export_session(sid, "html", out_dir=tmp_path)
            check("escribe HTML", p is not None and p.suffix == ".html"
                  and p.exists(), str(p), SECTION_2)
            assert p is not None and p.read_text(encoding="utf-8").startswith("<!DOCTYPE")
        finally:
            mgr.delete_session(sid)

    def test_export_current_conversation_uses_live_state(self, tmp_path):
        """Regresión: sin el estado vivo el documento salía VACÍO en silencio."""
        from src.interfaces.web.states import SessionState
        from src.interfaces.components.export_dialog import (
            export_current_conversation)
        st = SessionState()
        st.session_id = "conv_live_tmp"      # NO registrada en el manager
        st.add_message("user", "pregunta viva")
        st.add_message("assistant", "respuesta viva")
        p = export_current_conversation(st, "md", out_dir=tmp_path)
        check("exporta aunque el manager no tenga la sesión",
              p is not None and "pregunta viva" in p.read_text(encoding="utf-8"),
              str(p), SECTION_2)
        assert p is not None and "respuesta viva" in p.read_text(encoding="utf-8")

    def test_export_current_conversation_rejects_bad_input(self, tmp_path):
        from src.interfaces.components.export_dialog import (
            export_current_conversation)
        check("sin estado → None", export_current_conversation(None, "md") is None,
              "", SECTION_2)
        check("formato inválido → None",
              export_current_conversation(object(), "pdf") is None, "", SECTION_2)
        assert export_current_conversation(None, "md") is None

    def test_shared_html_converter_is_single_source(self):
        """research_export y sessions_dialog usan el MISMO conversor escapado."""
        from src.core import html_document
        import src.core.research_export as re_mod
        check("research_export usa el módulo compartido",
              re_mod._md_body_to_html is html_document.md_to_html_body,
              "identidad de función", SECTION_2)
        check("el conversor escapa antes de etiquetar",
              "&lt;" in html_document.md_to_html_body("<b>x</b>")
              or "&lt;b&gt;" in html_document.md_to_html_body("<b>x</b>"),
              html_document.md_to_html_body("<b>x</b>"), SECTION_2)
        assert re_mod._md_body_to_html is html_document.md_to_html_body
