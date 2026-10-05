from __future__ import annotations
import importlib
import sys
from pathlib import Path
try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check
try:
    from .suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import
except ImportError:  # pragma: no cover
    from suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import
import asyncio
import unittest.mock as _mock


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION_WEB = "6. Web, handlers, prompts, utils"
SECTION_GLOBAL = "7. Análisis estático global (todos los módulos)"
WEB_API = {
    "src.interfaces.web.routes": ["router"],
    "src.interfaces.web.routes_extended": ["list_models", "status_endpoint"],
    "src.interfaces.web.routes_hwfit": ["hwfit_hardware", "hwfit_rank", "hwfit_check_model",
                                        "hwfit_compatible"],
    "src.interfaces.web.server": ["WebServer", "create_web_server", "_mount_fastapi_router"],
    "src.interfaces.web.app": ["create_header"],
    "src.interfaces.web.app_logic": [],
    "src.interfaces.web.states": [],
    "src.interfaces.components.chat": ["ChatComponent", "ChatMessage", "create_chat_component"],
    "src.interfaces.components.sidebar": ["SidebarComponent", "create_sidebar"],
    "src.interfaces.components.modals": ["create_ambiguity_warning_dialog", "AmbiguityWarningDialog"],
    "src.interfaces.components.rafael_widget": [],
    "src.interfaces.cli.main": [],
    "src.interfaces.cli.command_parser": ["CommandParser"],
}
HANDLERS_API = {
    "src.handlers.data_handler": ["DataHandler"],
    "src.handlers.data_fetchers": ["DataFetchers"],
}
PROMPTS_API = {
    "src.prompts.base_prompts": [],
    "src.prompts.task_prompts": ["TaskLayer"],
    "src.prompts.tool_prompts": ["ToolLayer"],
    "src.prompts.context_injection": ["ContextLayer"],
    "src.prompts.output_formats": ["OutputLayer"],
    "src.prompts.reflection_triggers": ["ReflectionLayer"],
    "src.prompts.prompt_manager": ["PromptManager", "get_prompt_manager"],
    "src.prompts.prompt_optimizer": ["PromptOptimizer"],
}
UTILS_API = {
    "src.utils.ambiguity_detector": ["AmbiguityDetector", "detect_ambiguity", "rewrite_query",
                                     "get_clarification_questions"],
    "src.utils.db_pool": ["SQLiteConnectionPool"],
    "src.utils.geolocator": ["Geolocator", "GeoLocation", "get_geolocator"],
    "src.utils.ui": ["print_header", "print_success", "print_error", "ok", "fail", "warn", "info"],
}
EXTRA_API = {
    "src.learning.trace_store": ["TraceStore", "Trace", "get_trace_store"],
    "src.monitoring.alert_manager": ["AlertManager", "Alert", "AlertRule", "AlertSeverity",
                                     "get_alert_manager", "collect_system_metrics"],
    "src.integrations.external_apis": ["ExternalAPIManager"],
}


class TestWeb:
    def test_web_apis(self):
        section(SECTION_WEB)
        for mod, syms in WEB_API.items():
            _api_check(mod, syms, SECTION_WEB)

    def test_fastapi_router(self):
        m = _import("src.interfaces.web.routes")
        if isinstance(m, str):
            check("router FastAPI", False, m, SECTION_WEB)
            return
        router = getattr(m, "router", None)
        check("router FastAPI definido", router is not None, type(router).__name__, SECTION_WEB)
        if router is not None:
            n = len(getattr(router, "routes", []))
            check("router con rutas", n > 0, f"{n} rutas", SECTION_WEB)

    def test_web_server_clase(self):
        from src.interfaces.web.server import WebServer
        check("WebServer clase", WebServer is not None, "OK", SECTION_WEB)
        check("WebServer.start método", hasattr(WebServer, "start"), "OK", SECTION_WEB)


class TestSessionEndpoints:
    def test_list_sessions_devuelve_sesiones_reales(self):
        import asyncio
        from src.interfaces.web.states import SessionManager
        from src.interfaces.web.routes_extended import list_sessions

        mgr = SessionManager()
        sid = "pytest_regresion_sessions"
        try:
            mgr.get_or_create_session(sid)
            r = asyncio.run(list_sessions())
            assert "sessions" in r and "total" in r
            ids = [s.get("session_id") for s in r["sessions"]]
            assert sid in ids, f"la sesión {sid!r} no apareció en el listado: {ids}"
            assert r["total"] >= 1
        finally:
            mgr.delete_session(sid)

    def test_get_session_existente_e_inexistente(self):
        import asyncio
        from src.interfaces.web.states import SessionManager
        from src.interfaces.web.routes_extended import get_session

        mgr = SessionManager()
        sid = "pytest_regresion_getsession"
        try:
            s = mgr.get_or_create_session(sid)
            s.add_message("user", "hola")
            found = asyncio.run(get_session(sid))
            assert found.get("found") is True, found
            assert len(found.get("messages", [])) == 1
        finally:
            mgr.delete_session(sid)
        missing = asyncio.run(get_session("no_existe_xyz_123"))
        assert missing.get("found") is False, missing

    def test_delete_session_existente_e_inexistente(self):
        import asyncio
        from src.interfaces.web.states import SessionManager
        from src.interfaces.web.routes_extended import delete_session

        mgr = SessionManager()
        sid = "pytest_regresion_delsession"
        mgr.get_or_create_session(sid)
        ok = asyncio.run(delete_session(sid))
        assert ok["success"] is True, ok
        # ya no existe
        assert mgr.get_session(sid) is None
        missing = asyncio.run(delete_session("no_existe_xyz_123"))
        assert missing["success"] is False, missing

    def test_endpoints_sessions_usan_el_manager_real(self):
        """✅ v0.6.9v: COMPORTAMIENTO en vez de grep del fuente.

        Antes se buscaba la cadena 'TODO: Implementar SessionManager' en el
        archivo. Un test de comportamiento es más fuerte: si los endpoints
        volvieran a ser stubs, estos asserts fallan igual — sin acoplarse al
        texto ni a la ubicación del código.
        """
        import asyncio
        from src.interfaces.web.states import SessionManager
        from src.interfaces.web.routes_extended import list_sessions, get_session

        mgr = SessionManager()
        sid = "pytest_uso_manager_real"
        try:
            mgr.get_or_create_session(sid)
            listed = asyncio.run(list_sessions())
            assert any(s.get("session_id") == sid for s in listed["sessions"]), \
                "list_sessions no devolvió una sesión real del SessionManager"
            detail = asyncio.run(get_session(sid))
            assert detail.get("found") is True, "get_session no encontró la sesión real"
        finally:
            mgr.delete_session(sid)


class TestHandlers:
    def test_handlers_apis(self):
        section(SECTION_WEB)
        for mod, syms in HANDLERS_API.items():
            _api_check(mod, syms, SECTION_WEB)

    def test_data_handler_comportamiento(self):
        import asyncio
        from src.domain.entities import Message, MessageRole, Response
        from src.handlers.data_handler import DataHandler
        dh = DataHandler(timeout=5)
        msg = Message(role=MessageRole.USER, content="hola")
        res = asyncio.run(dh.handle(msg, "chat"))
        check("DataHandler.handle('chat')", isinstance(res, Response),
              f"tipo={type(res).__name__}", SECTION_WEB)

    def test_data_fetchers(self):
        from src.handlers.data_fetchers import DataFetchers
        df = DataFetchers()
        check("DataFetchers() construible", df is not None, "OK", SECTION_WEB)


class TestWeatherRegression:
    """Regresión 2026-08-25: las consultas de clima fallaban con
    'No se encontró ubicación para: Hoy En Cordoba Vgb' porque las
    abreviaturas no se expandían y el nombre compuesto no se recortaba."""

    def test_extract_city_abreviaturas(self):
        from src.handlers.data_fetchers import DataFetchers
        df = DataFetchers()
        cases = {
            "clima hoy en cordoba vgb": "Villa General Belgrano",
            "clima en cordoba villa general belgrano": "Villa General Belgrano",
            "clima en cba": "Córdoba",
            "clima en villa general belgrano": "Villa General Belgrano",
            "como esta el clima en cordoba": "Córdoba",
        }
        for query, expected in cases.items():
            city = df.extract_city(query)
            check(f"extract_city({query!r})", city == expected,
                  f"→ {city!r}", SECTION_WEB)

    def test_weather_fallback_sin_ubicacion(self):
        """Si la ubicación no se encuentra, fetch_weather devuelve una
        respuesta amigable (success=False) en vez de lanzar ValueError."""
        import asyncio
        from unittest.mock import patch
        from src.handlers.data_fetchers import DataFetchers

        async def _run():
            df = DataFetchers()
            with patch("src.handlers.data_fetchers.get_coordinates",
                       return_value=None):
                return await df.fetch_weather("clima en lugarnonexistente123")

        try:
            res = asyncio.run(_run())
            ok = isinstance(res, dict) and res.get("success") is False \
                and "No encontré" in res.get("content", "")
            check("fetch_weather fallback amigable", ok,
                  f"success={res.get('success')}", SECTION_WEB)
        except Exception as e:  # noqa: BLE001
            check("fetch_weather fallback amigable", False,
                  f"{type(e).__name__}: {e}", SECTION_WEB)


class TestVoiceRuntimeRegression:
    """Regresión 2026-08-25: el audio se activaba solo al iniciar la web.
    Ahora VoicePipeline se registra pero solo arranca si se habilita
    (config jarvis_mode_default o toggle de la UI)."""

    def test_set_voice_idempotente(self):
        from unittest.mock import MagicMock, patch
        import src.interfaces.web.voice_runtime as vr

        pipeline = MagicMock()
        with patch.object(vr, "_pipeline", pipeline), \
             patch.object(vr, "_running", False):
            vr.set_voice(True)
            vr.set_voice(True)   # segunda llamada no debe re-arrancar
            vr.set_voice(False)
            vr.set_voice(False)  # segunda llamada no debe re-detener
            check("set_voice(True) arranca una vez", pipeline.start.call_count == 1,
                  f"start x{pipeline.start.call_count}", SECTION_WEB)
            check("set_voice(False) detiene una vez", pipeline.stop.call_count == 1,
                  f"stop x{pipeline.stop.call_count}", SECTION_WEB)

    def test_set_voice_sin_pipeline(self):
        import src.interfaces.web.voice_runtime as vr
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                vr, "_pipeline", None):
            ok = vr.set_voice(True)
            check("set_voice sin pipeline → False", ok is False, "no pipeline", SECTION_WEB)

    def test_jarvis_default_controla_audio(self):
        """jarvis_mode_default=normal (default) → la voz NO debe estar
        activa por defecto. La UI lo enciende con set_voice(True)."""
        from config.settings import settings
        from src.interfaces.web.voice_runtime import set_voice, is_voice_running
        from unittest.mock import MagicMock, patch
        import src.interfaces.web.voice_runtime as vr
        default = settings.jarvis_mode_default
        check("jarvis_mode_default es 'normal'", default in ("normal", "silent"),
              f"default={default!r}", SECTION_WEB)
        pipeline = MagicMock()
        with patch.object(vr, "_pipeline", pipeline), \
             patch.object(vr, "_running", False):
            active = is_voice_running()
            check("voz inactiva por defecto", active is False, "no auto-start", SECTION_WEB)


class TestPrompts:
    def test_prompts_apis(self):
        section(SECTION_WEB)
        for mod, syms in PROMPTS_API.items():
            _api_check(mod, syms, SECTION_WEB)

    def test_prompt_manager_build(self):
        from src.prompts.prompt_manager import get_prompt_manager
        pm = get_prompt_manager("coder")
        out = pm.build(task_type="CODE_GENERATION")
        check("PromptManager.build()", isinstance(out, str) and len(out) > 0,
              f"{len(out)} chars", SECTION_WEB)
        check("PromptManager.get_template_value()",
              pm.get_template_value("identity") is not None, "OK", SECTION_WEB)
        pm.reload_template()
        check("PromptManager.reload_template()", True, "OK", SECTION_WEB)

    def test_capas_prompt(self):
        from src.prompts.context_injection import ContextLayer
        cl = ContextLayer()
        check("ContextLayer() construible", cl is not None, "OK", SECTION_WEB)


class TestUtils:
    def test_utils_apis(self):
        section(SECTION_WEB)
        for mod, syms in UTILS_API.items():
            _api_check(mod, syms, SECTION_WEB)

    def test_ambiguity_detector_comportamiento(self):
        from src.utils.ambiguity_detector import detect_ambiguity, rewrite_query
        level, score, patterns = detect_ambiguity("reemplazar el modulo viejo por uno nuevo")
        check("detect_ambiguity() detecta high", level == "high",
              f"nivel={level} score={score} patrones={patterns}", SECTION_WEB)
        rw = rewrite_query("optimizar el sistema", "medium")
        check("rewrite_query()", isinstance(rw, str) and len(rw) > 0, rw[:40], SECTION_WEB)

    def test_geolocator_mockeado(self):
        from src.utils.geolocator import Geolocator
        geo = Geolocator(use_ip_lookup=False)
        check("Geolocator(use_ip_lookup=False) construible", geo is not None, "OK", SECTION_WEB)
        try:
            loc = geo.get_location()
            check("Geolocator.get_location()", loc is not None, str(loc), SECTION_WEB)
        except Exception as e:  # noqa: BLE001
            check("Geolocator.get_location()", False, f"{type(e).__name__}: {e}", SECTION_WEB)

    def test_db_pool(self, tmp_path):
        from src.utils.db_pool import SQLiteConnectionPool
        pool = SQLiteConnectionPool(db_path=str(tmp_path / "pool.db"))
        check("SQLiteConnectionPool() construible", pool is not None, "OK", SECTION_WEB)

    def test_ui_funciones(self):
        from src.utils.ui import print_header, print_success, ok, fail
        for fn, name in [(print_header, "print_header"), (print_success, "print_success"),
                         (ok, "ok"), (fail, "fail")]:
            check(f"ui.{name} callable", callable(fn), "OK", SECTION_WEB)


class TestLearningMonitoring:
    def test_extra_apis(self):
        section(SECTION_WEB)
        for mod, syms in EXTRA_API.items():
            _api_check(mod, syms, SECTION_WEB)

    def test_trace_store(self, tmp_path):
        from src.learning.trace_store import TraceStore
        ts = TraceStore(db_path=tmp_path / "traces.db")
        check("TraceStore() construible", ts is not None, "OK", SECTION_WEB)

    def test_alert_manager(self):
        from src.monitoring.alert_manager import get_alert_manager
        am = get_alert_manager()
        check("get_alert_manager()", am is not None, "OK", SECTION_WEB)


class TestAnalisisGlobal:
    def test_todos_los_modulos_src_importan(self):
        """Importa CADA módulo .py de src/ y registra el resultado."""
        section(SECTION_GLOBAL)
        src_dir = PROJECT_ROOT / "src"
        modules = []
        for py in sorted(src_dir.rglob("*.py")):
            if "__pycache__" in str(py):
                continue
            rel = py.relative_to(PROJECT_ROOT)
            if rel.name == "__init__.py":
                mod = ".".join(rel.parts[:-1])
            else:
                mod = ".".join(rel.with_suffix("").parts)
            modules.append(mod)

        ok_count = 0
        failed = []
        for mod in modules:
            m = _import(mod)
            if isinstance(m, str):
                failed.append((mod, m))
            else:
                ok_count += 1
        check(f"import de {len(modules)} módulos src/", not failed,
              f"{ok_count} OK" + (f" | fallan: {len(failed)}" if failed else ""), SECTION_GLOBAL)
        for mod, err in failed[:10]:
            check(f"  {mod}", False, err, SECTION_GLOBAL)

    def test_modulos_config_importan(self):
        for mod in ["config.settings", "config.settings_computations", "config.paths",
                    "config.multimodal_loader", "config"]:
            m = _import(mod)
            check(f"import {mod}", not isinstance(m, str), "" if not isinstance(m, str) else m, SECTION_GLOBAL)

    def test_estructura_src(self):
        expected = ["agents", "core", "domain", "handlers", "interfaces", "integrations",
                    "learning", "llm", "memory", "monitoring", "multimodal", "prompts",
                    "router", "tools_mcp", "utils"]
        src_dir = PROJECT_ROOT / "src"
        missing = [d for d in expected if not (src_dir / d).is_dir()]
        check("subdirectorios de src/", not missing,
              "faltan: " + ", ".join(missing) if missing else f"{len(expected)} subdirs OK", SECTION_GLOBAL)

    def test_main_importable(self):
        m = _import("main")
        check("main importable", not isinstance(m, str), "" if not isinstance(m, str) else m, SECTION_GLOBAL)

    def test_cli_parser(self):
        m = _import("src.interfaces.cli.command_parser")
        if isinstance(m, str):
            check("command_parser importable", False, m, SECTION_GLOBAL)
            return
        parser_cls = getattr(m, "CommandParser", None)
        check("CommandParser disponible", parser_cls is not None, "OK", SECTION_GLOBAL)

    def test_sin_modelos_baneados(self):
        """Verifica que los modelos baneados no aparezcan en configuración ACTIVA
        (ignora líneas de comentario/documentación)."""
        banned = ["llama3.2:3b", "llama3.1:8b", "qwen2.5-coder:14b", "llava:7b",
                  "selene-mini", "moondream:v2"]
        found = []
        for path in [PROJECT_ROOT / "config" / "models.yaml",
                     PROJECT_ROOT / "config" / "settings.py"]:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                low = stripped.lower()
                for b in banned:
                    if b.lower() in low:
                        found.append(f"{path.name}:{b}")
        check("sin modelos baneados en config activa", not found,
              "encontrados: " + ", ".join(found) if found else "limpio", SECTION_GLOBAL)

    def test_directorios_salida(self):
        for d in ["data", "logs", "cache", "docs"]:
            check(f"directorio {d}/ existe", (PROJECT_ROOT / d).is_dir(), "OK", SECTION_GLOBAL)

    # ── Regresiones 2026-08-25 ──────────────────────────────────
    _OBSOLETE_MODELS = (
        "qwen2.5:3b", "axioma-code:latest", "huihui_ai/qwen2.5-abliterate:7b",
        "huihui_ai/qwen2.5-vl-abliterated:7b", "huihui_ai/qwen2.5-coder-abliterate:14b",
        "moondream:v2", "gemma4:e4b", "aletheia-3b:latest", "llava:7b",
    )

    def test_sin_modelos_obsoletos_en_src(self):
        """Regresión: modelos no instalados hardcodeados en código ACTIVO de src/
        (causaban HTTP 404 en runtime). Usa AST — ignora docstrings/comentarios."""
        import ast as _ast
        found = []
        for py in sorted((PROJECT_ROOT / "src").rglob("*.py")):
            if "__pycache__" in str(py):
                continue
            tree = _ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
            doc_ids = set()
            for n in _ast.walk(tree):
                if not isinstance(n, (_ast.Module, _ast.ClassDef,
                                      _ast.FunctionDef, _ast.AsyncFunctionDef)):
                    continue
                if n.body and isinstance(n.body[0], _ast.Expr) \
                        and isinstance(n.body[0].value, _ast.Constant) \
                        and isinstance(n.body[0].value.value, str):
                    doc_ids.add(id(n.body[0].value))
            for n in _ast.walk(tree):
                if not isinstance(n, _ast.Constant) or not isinstance(n.value, str):
                    continue
                if id(n) in doc_ids:
                    continue
                low = n.value.lower()
                for m in self._OBSOLETE_MODELS:
                    if m.lower() in low:
                        found.append(f"{py.name}:{m}")
                        break
        check("sin modelos obsoletos en código activo de src/", not found,
              "encontrados: " + ", ".join(found[:8]) if found else "limpio", SECTION_GLOBAL)

    def test_sin_get_event_loop(self):
        """Regresión: asyncio.get_event_loop() deprecado en Python 3.14
        (feedback_loop.py y speedtest.py fueron corregidos).

        ✅ v0.6.9v: chequeo reescrito con AST (antes era line-scan). El scan por
        línea daba FALSOS POSITIVOS con el propio auditor, que contiene el
        patrón como string de detección/mensaje
        (`code_snippet="asyncio.get_event_loop()"` en
        tools/auditor/aud_detectors_arquitectura.py). Con AST solo cuentan las
        LLAMADAS reales, no las cadenas.
        """
        import ast as _ast
        found = []
        for base in ["src", "tools", "config"]:
            for py in sorted((PROJECT_ROOT / base).rglob("*.py")):
                if "__pycache__" in str(py):
                    continue
                try:
                    tree = _ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
                except SyntaxError:
                    continue
                for node in _ast.walk(tree):
                    if not isinstance(node, _ast.Call):
                        continue
                    fn = node.func
                    # asyncio.get_event_loop(...)  o  <alias>.get_event_loop(...)
                    if isinstance(fn, _ast.Attribute) and fn.attr == "get_event_loop":
                        found.append(f"{py.relative_to(PROJECT_ROOT)}:{node.lineno}")
        check("sin asyncio.get_event_loop() en código", not found,
              ", ".join(found[:8]) if found else "limpio", SECTION_GLOBAL)

    def test_sin_except_desnudo(self):
        """Regresión: except: desnudo captura KeyboardInterrupt/SystemExit
        (7 corregidos en tools/)."""
        found = []
        for base in ["src", "tools", "config"]:
            for py in sorted((PROJECT_ROOT / base).rglob("*.py")):
                if "__pycache__" in str(py):
                    continue
                in_doc = False
                for i, line in enumerate(py.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith(('"""', "'''")):
                        # docstring de una línea ("""texto""") no cambia estado
                        if stripped.endswith(('"""', "'''")) and len(stripped) > 6:
                            continue
                        in_doc = not in_doc
                        continue
                    if in_doc or stripped.startswith("#"):
                        continue
                    if stripped == "except:":
                        found.append(f"{py.relative_to(PROJECT_ROOT)}:{i}")
        check("sin 'except:' desnudo", not found,
              ", ".join(found[:8]) if found else "limpio", SECTION_GLOBAL)

    def test_provider_circuit_compartido(self):
        """Regresión: _ProviderCircuit estaba duplicado en 4 archivos.

        ✅ v0.6.9v: COMPORTAMIENTO — se importan los 4 módulos y se verifica que
        la clase sea LITERALMENTE LA MISMA (identidad), no que el texto del
        import aparezca en el archivo. Es más fuerte: detecta una redefinición
        local aunque el import siga escrito.
        """
        from src.utils.provider_circuit import ProviderCircuit

        mods = ["src.agents.researcher_sources", "src.agents.searcher",
                "src.integrations.external_apis", "src.router.classifier_api"]
        bad = []
        for name in mods:
            mod = importlib.import_module(name)
            got = getattr(mod, "_ProviderCircuit", None) or getattr(mod, "ProviderCircuit", None)
            if got is not ProviderCircuit:
                bad.append(name)
        check("ProviderCircuit COMPARTIDO (misma clase) en los 4 módulos", not bad,
              "distintos/copiados: " + ", ".join(bad) if bad else "4/4 identidad OK",
              SECTION_GLOBAL)


class _FakeResp:
    status_code = 200


class _FakeAsyncClient:
    """Context manager httpx.AsyncClient falso: get() → 200 (o excepción)."""
    def __init__(self, timeout=None, raise_error: bool = False):
        self.timeout = timeout
        self.raise_error = raise_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self.raise_error:
            raise OSError("sin red")
        return _FakeResp()


class _FakeHTTP:
    """Módulo httpx falso (la API usa `client.AsyncClient(...)`)."""
    raise_error = False

    @classmethod
    def AsyncClient(cls, timeout=None):
        c = _FakeAsyncClient(timeout=timeout)
        c.raise_error = cls.raise_error
        return c


class TestSystemStatusDialog:
    """Estado del sistema: recolección pura (testeable) del diálogo 'Estado'."""

    def test_collect_system_status_ok(self):
        """Con Ollama arriba y API keys seteadas → todo True."""
        from src.interfaces.web import app_logic
        from config.settings import settings as _settings

        with _mock.patch.object(_settings, "ollama_host", "http://ollama:11434"), \
             _mock.patch.object(_settings, "tavily_api_key", "t-key"), \
             _mock.patch.object(_settings, "news_api_key", "n-key"), \
             _mock.patch.object(_settings, "serper_api_key", "s-key"):
            status = asyncio.run(app_logic.collect_system_status(
                http_client=_FakeHTTP,
            ))
        assert status["ollama"] is True
        assert status["tavily"] is True
        assert status["news"] is True
        assert status["serper"] is True

    def test_collect_system_status_ollama_down(self):
        """Ollama caído (excepción HTTP) → ollama False, keys siguen."""
        from src.interfaces.web import app_logic
        from config.settings import settings as _settings

        with _mock.patch.object(_settings, "ollama_host", "http://ollama:11434"):
            _FakeHTTP.raise_error = True
            try:
                status = asyncio.run(app_logic.collect_system_status(
                    http_client=_FakeHTTP,
                ))
            finally:
                _FakeHTTP.raise_error = False
        assert status["ollama"] is False
        assert set(status.keys()) == {"ollama", "tavily", "news", "serper",
                                      "serpapi", "dolar"}

    def test_estado_button_exposes_async_refresh(self):
        """v0.6.8ll: los diálogos viven en modals.py (fuente única) y app.py
        los consume vía factories. El refresh es async incremental con
        dialog._refresh; no hay dialog.on('open') (evento inexistente)."""
        app = _import("src.interfaces.web.app")
        assert app is not None
        for sym in ("create_status_dialog", "create_settings_dialog",
                    "create_exit_dialog", "create_file_dialog",
                    "_on_estado_click"):
            assert callable(getattr(app, sym, None)), f"app.{sym} no disponible"

        # ✅ v0.6.9v: COMPORTAMIENTO/IDENTIDAD en vez de grep del fuente.
        # app.py CONSUME modals sin redefinir los builders: se verifica por
        # IDENTIDAD de objetos (si app.py definiera su propio builder, la
        # identidad fallaría aunque el import siguiera escrito).
        import inspect
        from src.interfaces.components import modals as modals_mod

        for name in ("create_status_dialog", "create_settings_dialog",
                     "create_exit_dialog", "create_file_dialog"):
            assert getattr(app, name) is getattr(modals_mod, name), \
                f"app.{name} no es la misma función que modals.{name} (¿redefinida?)"

        # modals: el refresh async está expuesto y NO se usa el evento mal formado.
        status_src = inspect.getsource(modals_mod.create_status_dialog)
        assert "_refresh" in status_src, "create_status_dialog no expone _refresh"
        assert "dialog.on('open', lambda" not in status_src, \
            "reapareció el handler 'open' como lambda (evento mal formado)"
        assert "dialog.on('open', asyncio.create_task" not in status_src, \
            "reapareció el handler 'open' con create_task"
        # header con handler async, resuelto en el módulo (no por texto)
        assert callable(getattr(app, "_on_estado_click", None)), \
            "app._on_estado_click no disponible"


# ── Fusionado desde test_axioma_12_explorador_archivos.py ──


class TestExploreDir:
    def _explore(self, path):
        from src.interfaces.web.app_logic import explore_dir
        return explore_dir(str(path))

    def test_lista_carpetas_y_archivos(self, tmp_path):
        (tmp_path / "subcarpeta").mkdir()
        (tmp_path / "nota.txt").write_text("hola", encoding="utf-8")
        (tmp_path / "datos.csv").write_text("a,b", encoding="utf-8")
        data = self._explore(tmp_path)
        assert data is not None
        assert [d["name"] for d in data["dirs"]] == ["subcarpeta"]
        assert {f["name"] for f in data["files"]} == {"nota.txt", "datos.csv"}
        assert all(f["path"].startswith(str(tmp_path)) for f in data["files"])
        assert data["parent"] == str(tmp_path.parent)

    def test_oculta_dotfiles_y_ruido(self, tmp_path):
        (tmp_path / ".env").write_text("SECRETO", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / "venv").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "ok.py").write_text("x", encoding="utf-8")
        data = self._explore(tmp_path)
        nombres = [d["name"] for d in data["dirs"]] + \
                  [f["name"] for f in data["files"]]
        assert nombres == ["ok.py"]  # solo lo visible

    def test_orden_alfabetico(self, tmp_path):
        for n in ("zeta.txt", "alfa.txt", "Beta.txt"):
            (tmp_path / n).write_text("x", encoding="utf-8")
        data = self._explore(tmp_path)
        assert [f["name"] for f in data["files"]] == \
            ["alfa.txt", "Beta.txt", "zeta.txt"]

    def test_tamano_presente(self, tmp_path):
        (tmp_path / "bin.dat").write_bytes(b"\x00" * 2048)
        data = self._explore(tmp_path)
        assert data["files"][0]["size"] == 2048

    def test_no_existe_devuelve_none(self, tmp_path):
        assert self._explore(tmp_path / "no_existe") is None

    def test_archivo_no_directorio_devuelve_none(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("x", encoding="utf-8")
        assert self._explore(f) is None


class TestAdjuntoSeguro:
    """v0.6.9q — detección de imagen por contenido y rechazo de binarios."""

    def test_magic_png(self):
        from src.interfaces.components.chat import _detect_image_ext
        assert _detect_image_ext(b"\x89PNG\r\n\x1a\nresto") == ".png"

    def test_magic_jpeg_gif_webp(self):
        from src.interfaces.components.chat import _detect_image_ext
        assert _detect_image_ext(b"\xff\xd8\xff\xe0") == ".jpg"
        assert _detect_image_ext(b"GIF89a...") == ".gif"
        assert _detect_image_ext(b"RIFF\x00\x00\x00\x00WEBP") == ".webp"

    def test_texto_no_es_imagen(self):
        from src.interfaces.components.chat import _detect_image_ext
        assert _detect_image_ext(b"def hola(): pass") is None

    def test_binario_parece_binario(self):
        from src.interfaces.components.chat import _looks_binary
        # PNG bytes decodificados como utf-8 = muchos controles
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        assert _looks_binary(png.decode("utf-8", errors="ignore")) is True

    def test_texto_normal_no_es_binario(self):
        from src.interfaces.components.chat import _looks_binary
        assert _looks_binary("Hola, ¿cómo estás?\nSegunda línea.") is False

    def test_imagen_como_unknown_txt_se_detecta(self):
        # Reproducción del bug: PNG que llegaba como "unknown.txt"
        from src.interfaces.components.chat import _detect_image_ext
        import struct
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13)
        assert _detect_image_ext(png) == ".png"


# ── Fusionado desde test_axioma_07_user_selection_e2e.py ──
from nicegui.testing.user_simulation import user_simulation

_MAIN_SRC = '''\
from nicegui import app, ui

@ui.page('/')
def home():
    def select(username: str) -> None:
        app.storage.user['user_id'] = username
        ui.navigate.to('/chat')
    ui.button('saintwick', on_click=lambda: select('saintwick'))

@ui.page('/chat')
def chat():
    uid = app.storage.user.get('user_id', '')
    if not uid:
        ui.navigate.to('/')
        return
    ui.label(f'CHAT-OK-{uid}')

ui.run(storage_secret='test-secret')
'''


def _main_file(tmp_path):
    main = tmp_path / 'user_select_main.py'
    main.write_text(_MAIN_SRC, encoding='utf-8')
    return str(main)


def test_click_user_reaches_chat(tmp_path) -> None:
    """Clic en el usuario → /chat renderiza el chat (el user_id sobrevive)."""
    async def scenario() -> None:
        async with user_simulation(main_file=_main_file(tmp_path)) as user:
            await user.open('/')
            await user.should_see('saintwick')
            user.find('saintwick').click()
            await user.should_see('CHAT-OK-saintwick')
    asyncio.run(scenario())


def test_chat_without_user_redirects_home(tmp_path) -> None:
    """Sin usuario elegido, /chat redirige a la ventana de usuarios."""
    async def scenario() -> None:
        async with user_simulation(main_file=_main_file(tmp_path)) as user:
            await user.open('/chat')
            await user.should_see('saintwick')
    asyncio.run(scenario())


class TestCliMuestraFuentes:
    """El `--chat` del CLI mostraba solo el contenido (la UI web sí las muestra)."""

    def test_imprime_las_fuentes(self, capsys):
        from src.utils.ui import print_fuentes as _mostrar_fuentes

        class _Resp:
            sources = ["Censo 2022 — https://www.cba.gov.ar/censo", "INDEC — https://www.indec.gob.ar"]

        _mostrar_fuentes(_Resp())
        salida = capsys.readouterr().out
        check("el CLI nombra las fuentes", "Fuentes (2)" in salida, salida, SECTION_WEB)
        check("el CLI muestra las URLs", "cba.gov.ar" in salida and "indec.gob.ar" in salida,
              salida, SECTION_WEB)
        assert "Fuentes (2)" in salida and "cba.gov.ar" in salida

    def test_sin_fuentes_no_imprime_nada(self, capsys):
        from src.utils.ui import print_fuentes as _mostrar_fuentes

        class _Resp:
            sources = []

        _mostrar_fuentes(_Resp())
        check("sin fuentes no imprime ruido", capsys.readouterr().out.strip() == "",
              "imprimió algo", SECTION_WEB)
        assert capsys.readouterr().out.strip() == ""


class TestClisUnificados:
    """✅ Unificación de CLIs (2026-09-29): una sola implementación por comando.

    Medido antes de unificar: `main.py` y `src/interfaces/cli/main.py` tenían cada uno
    su bucle de chat, su `cmd_backup` y su forma de imprimir fuentes, y **derivaron**
    (la raíz no mostraba fuentes hasta `ISSUE-111`; su `--backup` estaba roto con
    `TypeError`). Ahora la raíz **delega** en el módulo con un adaptador de consola.
    """

    def test_la_raiz_delega_el_check_en_el_modulo(self, monkeypatch):
        import main as main_mod
        llamadas = []

        def _falso(console, *a, **k):
            llamadas.append(type(console).__name__)
            return 0

        import src.interfaces.cli.main as cli_mod   # import explícito: el paquete
        monkeypatch.setattr(cli_mod, "cmd_check", _falso)   # usa __getattr__ perezoso
        monkeypatch.setattr(main_mod, "_check_and_update_deps", lambda **k: True)
        rc = main_mod.cmd_check()
        assert rc == 0
        assert llamadas == ["_ConsolaRaiz"], f"debe delegar con el adaptador: {llamadas}"

    def test_la_raiz_delega_el_chat_en_el_modulo(self, monkeypatch):
        import main as main_mod
        visto = {}

        def _falso(console, query=None):
            visto["consola"] = type(console).__name__
            visto["query"] = query
            return 0

        import src.interfaces.cli.main as cli_mod
        monkeypatch.setattr(cli_mod, "cmd_chat", _falso)
        monkeypatch.setattr(main_mod, "_warmup_models", lambda: True)
        monkeypatch.setattr(main_mod, "_check_hardware_fit", lambda: None)
        rc = main_mod.cmd_chat("hola")
        assert rc == 0
        assert visto.get("consola") == "_ConsolaRaiz", visto
        assert visto.get("query") == "hola", visto

    def test_el_adaptador_imprime_al_estilo_de_la_raiz(self, capsys):
        import main as main_mod
        c = main_mod._ConsolaRaiz()
        c.print_info("info"); c.print_success("ok")
        c.print_warning("ojo"); c.print_error("mal"); c.print_panel("CHAT", "iniciando")
        salida = capsys.readouterr().out
        for esperado in ("ℹ️  info", "✅ ok", "⚠️  ojo", "❌ mal", "🔹 [CHAT] iniciando"):
            assert esperado in salida, f"falta {esperado!r} en {salida!r}"

    def test_backup_unico_lo_usan_los_dos(self):
        """Una sola implementación: la raíz importa la del módulo."""
        import inspect
        import main as main_mod
        src_raiz = inspect.getsource(main_mod.cmd_backup)
        assert "crear_backup_json" in src_raiz, "la raíz debe delegar el backup"
        from src.interfaces.cli.main import crear_backup_json
        assert callable(crear_backup_json)

    def test_chat_sin_consulta_es_modo_interactivo_no_ayuda(self):
        """`--chat` sin consulta debe llegar como "" (interactivo), no como None.

        Con `const=None`, "sin consulta" era indistinguible de "no lo pediste" ⇒
        `main()` imprimía la ayuda aunque su propio texto promete modo interactivo
        (había que escribir `--chat ""`). El contrato se fija acá: "" dispara el
        modo interactivo y una consulta explícita sigue viajando tal cual.
        """
        import main as main_mod
        sin_consulta = main_mod._construir_parser().parse_args(["--chat"])
        assert sin_consulta.chat == "", repr(sin_consulta.chat)
        assert sin_consulta.chat is not None
        con_consulta = main_mod._construir_parser().parse_args(["--chat", "hola"])
        assert con_consulta.chat == "hola"
        # La ayuda sigue mostrando los tres modos (no se rompió el contrato visible).
        ayuda = main_mod._construir_parser().format_help()
        for esperado in ("--chat [QUERY]", "Modo interactivo", "Consulta única"):
            assert esperado in ayuda, f"falta {esperado!r} en la ayuda"


class TestFlujoDeTokens:
    """`ISSUE-134` (Fase 1): el camino con flujo —ahora **ON por defecto**— debe
    tener paridad con el síncrono en lo que importa. Medición que lo motivó: para
    la misma pregunta generaba 1162 caracteres (sin tope) contra 589, y la
    respuesta llegaba **sin** `tokens_prompt`/`tokens_generated`."""

    def test_el_flujo_reporta_tokens_y_respeta_el_tope(self, monkeypatch):
        import asyncio
        from unittest.mock import patch
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        from src.core.dispatcher_handlers_extra import MAX_TOKENS_CHAT
        from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin
        from src.domain.entities import Message

        visto = {}

        class _Cliente:
            def chat_stream(self, messages=None, usage_sink=None, **kw):
                visto.update(kw)
                yield "Ho"
                yield "la"
                if usage_sink is not None:
                    usage_sink.update({"prompt_eval_count": 12, "eval_count": 3,
                                       "done_reason": "stop"})

        class _Disp(DispatcherHandlersExtraMixin, DispatcherProcessGuardMixin):
            _get_session_gateway = lambda self, _sid: None      # noqa: E731
            _format_tools_for_ollama = lambda self, _names: []  # noqa: E731
            _should_use_autonomous_mode = lambda self, _texto: False  # noqa: E731

        obj = _Disp.__new__(_Disp)
        obj.llm_client = _Cliente()
        obj._user_id = None
        recibidos = []
        msg = Message(content="hola", role="user",
                      metadata={"_on_token": recibidos.append})
        with patch("src.core.dispatcher_handlers_extra.get_registry",
                   return_value=type("R", (), {"has": lambda self, n: False})()):
            resp = asyncio.run(obj._handle_chat(msg, "chat", "s_flujo", None))

        meta = resp.metadata or {}
        assert visto.get("max_tokens") == MAX_TOKENS_CHAT, f"sin tope de salida: {visto}"
        assert resp.content == "Hola", resp.content
        assert (meta.get("tokens_prompt"), meta.get("tokens_generated")) == (12, 3), meta
        assert meta.get("streamed") is True and recibidos == ["Ho", "la"], f"{meta} · {recibidos}"


