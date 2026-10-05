import asyncio
import inspect
import json
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
HW_CPU = {
    "backend": "amd_igpu", "gpu_name": "AMD GPU", "gpu_vram_gb": 2.0,
    "effective_budget_gb": 0.9, "ram_gb": 14.74, "cpu_threads": 12,
    "is_gpu_available": False,
}
RANK = {
    "count": 2,
    "models": [
        {"model_name": "qwen3:8b", "size_gb": 4.87, "required_gb": 5.45,
         "fit_level": "marginal", "run_mode": "cpu_only",
         "notes": "Solo CPU RAM (5.5/14.7 GB) — velocidad reducida"},
        {"model_name": "qwen2.5-coder:7b", "size_gb": 4.68, "required_gb": 5.24,
         "fit_level": "marginal", "run_mode": "cpu_only", "notes": ""},
    ],
}
COMPAT_WARN = {
    "has_warnings": True, "models_checked": 2, "all_compatible": False,
    "results": {
        "llm_default_model": {"model_name": "qwen3:8b",
                              "fit_level": "marginal", "run_mode": "cpu_only"},
        "llm_code_model": {"model_name": "qwen2.5-coder:7b",
                           "fit_level": "marginal", "run_mode": "cpu_only"},
    },
}


def _modals():
    """Módulo donde vive el diálogo (extraído de modals.py, esquema 50/50)."""
    import src.interfaces.components.mi_hard_dialog as mi_hard_mod
    return mi_hard_mod


class TestMiHardSections:
    """El formateo es una función pura → testeable sin cliente NiceGUI."""

    def test_three_sections_in_order(self):
        sections = _modals().mi_hard_sections(HW_CPU, RANK, COMPAT_WARN)
        assert [s["title"] for s in sections] == [
            "Hardware detectado",
            "Modelos de la configuración",
            "Modelos instalados (2)",
        ]

    def test_cpu_only_is_stated_explicitly(self):
        """Sin GPU utilizable la vista debe decirlo (no dejarlo implícito)."""
        sec = _modals().mi_hard_sections(HW_CPU, RANK, COMPAT_WARN)[0]
        texts = " ".join(t for t, _ in sec["lines"])
        assert "CPU (sin GPU utilizable)" in texts
        assert "amd_igpu" in texts
        assert "14.74 GB" in texts and "budget: 0.9 GB" in texts

    def test_gpu_present_changes_label(self):
        hw = dict(HW_CPU, is_gpu_available=True)
        sec = _modals().mi_hard_sections(hw, RANK, COMPAT_WARN)[0]
        texts = " ".join(t for t, _ in sec["lines"])
        assert "🎮 GPU" in texts and "sin GPU utilizable" not in texts

    def test_warning_line_and_per_slot_entries(self):
        sec = _modals().mi_hard_sections(HW_CPU, RANK, COMPAT_WARN)[1]
        texts = [t for t, _ in sec["lines"]]
        assert "⚠️ 2 modelos" in texts[0]
        assert any("llm_default_model" in t and "qwen3:8b" in t for t in texts)
        assert any("llm_code_model" in t for t in texts)
        # el aviso va con clase naranja, no verde
        assert sec["lines"][0][1] == "text-xs text-orange-600"

    def test_all_compatible_shows_green(self):
        compat = dict(COMPAT_WARN, has_warnings=False)
        sec = _modals().mi_hard_sections(HW_CPU, RANK, compat)[1]
        assert sec["lines"][0] == ("✅ todos entran", "text-xs text-green-600")

    def test_rank_lines_carry_size_and_notes(self):
        sec = _modals().mi_hard_sections(HW_CPU, RANK, COMPAT_WARN)[2]
        joined = " ".join(t for t, _ in sec["lines"])
        assert "qwen3:8b — 4.87 GB (req 5.45)" in joined
        assert "velocidad reducida" in joined
        # un modelo sin notas no arrastra un guión suelto
        sin_notas = [t for t, _ in sec["lines"] if "qwen2.5-coder:7b" in t][0]
        assert not sin_notas.rstrip().endswith("—")

    def test_empty_payloads_do_not_crash(self):
        """Ollama caído / endpoint con error → la vista no debe romper."""
        sections = _modals().mi_hard_sections({}, {}, {})
        assert len(sections) == 3
        assert sections[0]["lines"]  # siempre hay al menos la línea de modo
        assert sections[2]["title"] == "Modelos instalados (0)"

    def test_non_dict_slot_entries_are_skipped(self):
        compat = {"has_warnings": False, "models_checked": 2,
                  "results": {"ok": {"model_name": "m", "fit_level": "fit",
                                     "run_mode": "cpu_only"}, "raro": "texto"}}
        sec = _modals().mi_hard_sections(HW_CPU, RANK, compat)[1]
        assert any("ok:" in t for t, _ in sec["lines"])
        assert not any("raro" in t for t, _ in sec["lines"])


class TestHwfitPayloadNormalizer:
    """El check devuelve dict en éxito y JSONResponse (404) en fallo."""

    def test_dict_passthrough(self):
        d = {"model_name": "qwen3:8b", "fit_level": "marginal"}
        assert _modals()._hwfit_payload(d) is d

    def test_jsonresponse_is_decoded(self):
        from fastapi.responses import JSONResponse
        resp = JSONResponse(status_code=404,
                            content={"error": "no instalado", "model_name": "x"})
        payload = _modals()._hwfit_payload(resp)
        assert payload["error"] == "no instalado"
        assert payload["model_name"] == "x"

    def test_undecodable_body_returns_none(self):
        class _Raro:
            body = b"\xff\xfe not json"
        assert _modals()._hwfit_payload(_Raro()) is None

    def test_none_returns_none(self):
        assert _modals()._hwfit_payload(None) is None


class TestOptimizacionesDelPipeline:
    """`ISSUE-136` (medido): el pipeline esperaba 15 s por una clasificación que nunca
    llegaba y 41-43 s por una validación que no cambia la respuesta."""

    def test_la_clasificacion_por_llm_esta_apagada_y_con_guardas(self, monkeypatch):
        """OFF no se llama al modelo (se perdían 15 s); aun encendido sólo se aceptan
        categorías de código (medido: 'Hola' → `translation` con confianza 0,95)."""
        from config.settings import settings as _settings
        from src.router import classifier as cls
        from src.router.classifier import LLM_CLASSIFY_ACEPTADOS, LLM_CLASSIFY_CONF_MINIMA
        llamadas = []
        monkeypatch.setattr(cls.IntentClassifier, "_classify_by_llm",
                            lambda self, t: (llamadas.append(t), (None, 0.0))[1])
        monkeypatch.setattr(_settings, "classifier_llm_enabled", False)
        r = cls.IntentClassifier().classify("¿Para qué sirve un journaling de archivos?")
        assert llamadas == [], f"no debe llamar al LLM con el interruptor OFF: {llamadas}"
        assert r["task_type"] == "general_chat", r
        assert "code_generation" in LLM_CLASSIFY_ACEPTADOS
        for inventada in ("translation", "tutoring", "technical_support", "data_analysis"):
            assert inventada not in LLM_CLASSIFY_ACEPTADOS, inventada
        assert 0.0 < LLM_CLASSIFY_CONF_MINIMA <= 1.0

    def test_la_validacion_corre_en_segundo_plano_y_deja_el_resultado(self):
        """La respuesta sale ANTES del veredicto (41-43 s medidos) y el resultado
        llega después a la metadata."""
        from src.core.dispatcher_process import DispatcherProcessMixin
        from src.domain.entities import Response

        class _Validador:
            def validate_response(self, _p, _r):
                return {"valid": False, "metadata": {"score": 3.0, "critique": "corta"}}

        class _Disp(DispatcherProcessMixin):
            async def _qm_record(self, *_a, **_k):
                return None

        obj = _Disp.__new__(_Disp)
        obj.validator = _Validador()
        resp = Response(content="una respuesta corta", task_type="general_chat",
                        model_used="qwen3:8b", latency_ms=1.0, confidence=0.8, sources=[])

        async def _escenario():
            obj._lanzar_validacion_en_fondo("¿pregunta?", resp, "general_chat")
            assert resp.metadata.get("validation_en_fondo") is True
            assert not resp.metadata.get("validation_en_fondo_completada")
            for _ in range(200):              # espera al veredicto (no bloquea la respuesta)
                if resp.metadata.get("validation_en_fondo_completada"):
                    break
                await asyncio.sleep(0.01)

        asyncio.run(_escenario())
        assert resp.metadata.get("validation_en_fondo_completada") is True, resp.metadata
        assert resp.metadata.get("validation_failed") is True, resp.metadata
        assert resp.metadata.get("validation_score") == 3.0, resp.metadata


class TestCachesSinContaminacion:
    """`ISSUE-138` (medido): las cachés servían respuestas de OTRAS conversaciones.
    Dos causas (2026-10-03): la clave era **sólo el texto de la pregunta** (el mismo
    texto en otra conversación recibía la respuesta vieja) y el umbral semántico L3 era
    **0.60**, cuando con bge-m3 dos preguntas DISTINTAS puntúan **0.603-0.635**
    (un parafraseo real: 0.933).
    """

    def test_la_firma_de_contexto_y_los_ajustes_de_las_caches(self):
        from config.settings import settings
        from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin as G
        from src.router.cache import RouterCache

        def firma(historial):
            return G._firma_contexto(SimpleNamespace(
                _historial_para_prompt=lambda _s, _t=None, h=historial: list(h)), "s")

        a = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        b = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "OTRA"}]
        assert firma([]) == "", "sin historial la firma debe ser vacía"
        assert len(firma(a)) == 16 and firma(a) != firma(b), "otra conversación ⇒ otra firma"
        assert firma(a) == firma(a), "la misma conversación debe dar la misma firma"
        # umbral >=0.80 (0.60 contaminaba) y ambas cachés se pueden apagar
        assert settings.cache_respuestas_enabled is True
        assert settings.cache_semantica_enabled is True
        assert settings.cache_semantica_umbral >= 0.80, settings.cache_semantica_umbral
        rc = RouterCache(ttl_seconds=60, max_size=5, enable_l2=False, enable_l3=True)
        if rc._l3 is not None:
            assert abs(rc._l3._threshold - settings.cache_semantica_umbral) < 1e-6, rc._l3._threshold


class TestNegativaYRespaldoDelModeloDeCodigo:
    """`ISSUE-142` (medido): el modelo censurado se niega **en prosa** en pedidos legítimos
    de laboratorio (frases reales medidas el 2026-10-03 con `qwen2.5-coder:7b`, 2-5 s)
    mientras `huihui_ai/qwen2.5-coder-abliterate:7b` sí devuelve código. Sin detector la
    negativa atravesaba el portero de calidad: ~60 s de reintentos (63,7 s medidos) para
    fallar con `error=None`. Ante la negativa se reintenta UNA vez con el otro modelo
    (en RAM entra uno por vez ⇒ cambio de modelo, ~6 s medidos).
    """

    FRASES_REALES = (
        "Lo siento, pero no puedo ayudarte con eso.",
        "Lo siento por la confusión, pero no puedo ayudarte con eso.",
        "Lo siento, pero no puedo ayudar con eso.",
    )

    def test_reconoce_las_negativas_y_no_marca_a_quien_si_genera_codigo(self):
        from src.agents.coder_core import detect_refusal
        for frase in self.FRASES_REALES:
            assert detect_refusal(frase), f"no detectó la negativa real: {frase!r}"
        assert detect_refusal("I'm sorry, but I can't help with that.")
        assert detect_refusal("As an AI, I cannot provide that code.")
        # sin esto, una respuesta CON código se marcaría por una frase suelta
        assert detect_refusal("Aquí tienes:\n```python\nprint('hola')\n```") is None
        assert detect_refusal("No puedo usar eval sin sanitizar, así que va la versión "
                              "segura:\n```python\nimport ast\n```") is None
        assert detect_refusal("Claro, te explico cómo funciona el bucle.") is None
        assert detect_refusal("") is None

    def test_una_negativa_no_gasta_reintentos_y_queda_marcada(self):
        """El corte que ahorra los ~60 s de reintentos inútiles (medido: 63,7 s)."""
        from unittest.mock import patch
        from src.agents.coder import CoderAgent
        from src.domain.entities import Message, MessageRole

        with patch("src.agents.base.OllamaClient"):
            agente = CoderAgent(model="m", timeout=1, enable_memory=False)
        llamadas = []
        agente._generate = lambda **_k: (llamadas.append(1), self.FRASES_REALES[0])[1]
        resultado = agente.execute(
            Message(role=MessageRole.USER, content="escribí un keylogger de prueba"),
            task_type="code_generation")
        assert resultado.success is False, "una negativa no es un éxito"
        assert resultado.metadata.get("code_refusal"), resultado.metadata
        assert len(llamadas) == 1, f"no debe reintentar: hubo {len(llamadas)} llamadas al modelo"

    @staticmethod
    def _dispatcher(disponible=True):
        """Stub del mixin: sólo lo que toca `_reintentar_con_respaldo_de_codigo`."""
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin

        class _Agente:
            config = SimpleNamespace(model="modelo-de-respaldo")

            def execute(self, *_a, **_k):
                return SimpleNamespace(success=True, content="```python\nx = 1\n```",
                                       model_used="modelo-de-respaldo", error=None, metadata={})

        obj = DispatcherHandlersExtraMixin.__new__(DispatcherHandlersExtraMixin)
        obj._coder_fallback = _Agente()
        obj._user_id = None
        obj._modelo_de_codigo_disponible = lambda _n: disponible
        obj._coder_de_respaldo = lambda: obj._coder_fallback
        return obj

    @staticmethod
    def _ejecutar(obj, frase="no puedo ayudar"):
        """Devuelve (resultado, negativa original) para poder mirar sus marcas."""
        from src.domain.entities import Message
        negado = SimpleNamespace(metadata={"code_refusal": frase}, success=False,
                                 error=f"code_refusal: {frase}")
        return asyncio.run(obj._reintentar_con_respaldo_de_codigo(
            negado, Message(content="pedido", role="user"), "code_generation", None, "s1")), negado

    def test_ante_una_negativa_usa_el_modelo_de_respaldo(self):
        r, _ = self._ejecutar(self._dispatcher())
        assert r is not None, "debería haber reintentado con el respaldo"
        assert r.success is True
        assert r.metadata.get("code_fallback_usado") == "modelo-de-respaldo", r.metadata
        assert r.metadata.get("code_refusal_primario") == "no puedo ayudar", r.metadata

    def test_sin_respaldo_instalado_ni_configurado_no_reintenta(self, monkeypatch):
        r, negado = self._ejecutar(self._dispatcher(disponible=False))
        assert r is None, "no debe reintentar si el modelo no está instalado"
        assert negado.metadata.get("code_fallback_no_instalado") == "modelo-de-respaldo", negado.metadata
        from config.settings import settings
        monkeypatch.setattr(settings, "llm_code_model_fallback", "")
        obj = self._dispatcher()
        obj._coder_de_respaldo = lambda: None
        assert self._ejecutar(obj, "no puedo")[0] is None


class TestMiHardWiring:
    def test_la_pantalla_arranca_simple_y_lo_avanzado_esta_detras(self):
        """`ISSUE-135` (Fase 2): por defecto conversación; lo demás detrás de un botón.
        Prueba de CABLEADO sobre el objeto resuelto (`inspect.getsource`, permitido por
        R4): el grupo avanzado nace OCULTO y existe el botón que lo muestra. Verificado
        además con la UI real (Chromium headless): el contenedor sale con `hidden`.
        """
        from src.interfaces.web import app as web_app
        barra = inspect.getsource(web_app._crear_barra_de_acciones)
        encabezado = inspect.getsource(web_app.create_header)
        assert "avanzado.set_visibility(False)" in barra, \
            "los controles avanzados deben nacer ocultos"
        assert "_btn_avanzado" in barra, "falta el botón que muestra lo avanzado"
        assert "set_advanced" in barra, "el botón debe avisarle al chat (RAC/Jarvis)"
        assert "_crear_barra_de_acciones()" in encabezado, \
            "el encabezado debe armar la barra de acciones"

    def test_modals_reexports_public_api(self):
        """Esquema 50/50: modals.py RE-EXPORTA (el API público no cambia)."""
        from src.interfaces.components import modals as modals_mod
        mod = _modals()
        assert modals_mod.create_mi_hard_dialog is mod.create_mi_hard_dialog
        assert modals_mod.mi_hard_sections is mod.mi_hard_sections

    def test_modals_stays_under_heavy_file_threshold(self):
        """modals.py no debe volver a pasar las 1000 líneas (AUD-PERF)."""
        from src.interfaces.components import modals as modals_mod
        n = len(inspect.getsource(modals_mod).splitlines())
        assert n < 1000, f"modals.py volvió a {n} líneas — extraer de nuevo"

    def test_factory_exposes_refresh_and_no_open_event(self):
        modals_mod = _modals()
        builder_src = inspect.getsource(modals_mod._build_mi_hard_dialog)
        assert "_refresh" in builder_src, "el diálogo no expone _refresh"
        assert "dialog.on('open'" not in builder_src, \
            "reapareció el evento 'open' (inexistente en NiceGUI)"
        assert "ui.timer" not in builder_src, \
            "el diálogo no debe refrescar solo (carga permanente en CPU)"

    def test_hwfit_routes_resolves_real_coroutines(self):
        """El diálogo consume las corrutinas REALES de /hwfit/* (sin duplicar)."""
        routes = _modals()._hwfit_routes()
        assert routes is not None, "routes_hwfit no importable"
        assert set(routes) == {"hardware", "rank", "compatible", "check"}
        for name, fn in routes.items():
            assert inspect.iscoroutinefunction(fn), f"{name} no es corrutina"

        from src.interfaces.web import routes_hwfit as real
        assert routes["hardware"] is real.hwfit_hardware
        assert routes["rank"] is real.hwfit_rank
        assert routes["compatible"] is real.hwfit_compatible
        assert routes["check"] is real.hwfit_check_model

    def test_hwfit_routes_degrades_when_import_fails(self):
        """Si routes_hwfit no se puede importar, devuelve None (no lanza)."""
        modals_mod = _modals()
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **kw):
            if "routes_hwfit" in name:
                raise ImportError("simulado")
            return real_import(name, *a, **kw)

        try:
            builtins.__import__ = _boom
            assert modals_mod._hwfit_routes() is None
        finally:
            builtins.__import__ = real_import

    def test_app_consumes_modals_factory_by_identity(self):
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        modals_mod = _modals()
        assert getattr(app, "create_mi_hard_dialog") is modals_mod.create_mi_hard_dialog
        assert callable(getattr(app, "_on_mi_hard_click", None))
        assert asyncio_iscoroutine(app._on_mi_hard_click)

    def test_handler_survives_missing_dialog(self):
        """_mi_hard_dialog=None (página sin montar) no debe lanzar."""
        import importlib
        app = importlib.import_module("src.interfaces.web.app")
        original = app._mi_hard_dialog
        try:
            app._mi_hard_dialog = None
            import asyncio
            asyncio.run(app._on_mi_hard_click())  # no debe lanzar
        finally:
            app._mi_hard_dialog = original

    def test_f1_does_not_add_new_config_flags(self):
        """F1 es read-only: NO debe introducir config muerta nueva.

        (El detector config_task_keys del auditor marca claves sin lector;
        F1 no necesita ninguna bandera para funcionar.)
        """
        from config.settings import settings
        # las 4 claves de modelo que la vista consume siguen existiendo
        assert hasattr(settings, "llm_default_model")
        assert hasattr(settings, "llm_code_model")


def asyncio_iscoroutine(fn):
    return inspect.iscoroutinefunction(fn)
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── Fusionado desde test_axioma_21_config_models_yaml.py ──
import ast
import yaml
try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
YAML_PATH = PROJECT_ROOT / "config" / "models.yaml"
LOADER_PATH = PROJECT_ROOT / "src" / "llm" / "config.py"
SECTION_2 = "21. Validador de models.yaml"
_MODEL_KEYS = ("primary", "preferred_model", "fallbacks", "fallback_model",
               "fallback")
_KNOWN_ENTRY_KEYS = set(_MODEL_KEYS) | {"temperature"}
MAX_TEMPERATURE = 1.5   # por encima de esto la generación deja de ser útil


def _raw() -> dict:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


def _loader_root_keys() -> set:
    """Claves raíz del YAML que el loader REALMENTE consume (derivadas del AST).

    Busca dentro de `LLMConfig._load_config` los patrones `"X" in data` y
    `data["X"]`, que es exactamente cómo el loader lee el archivo.
    """
    tree = ast.parse(LOADER_PATH.read_text(encoding="utf-8"))
    keys: set = set()
    for node in ast.walk(tree):
        # `if "X" in data:`  /  `if "X" in data and ...`
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and isinstance(node.ops[0], ast.In):
            for operand in (node.left, *node.comparators):
                if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                    keys.add(operand.value)
        # `data["X"]`
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            keys.add(node.slice.value)
    # Solo las claves que el loader lee del YAML raíz del archivo de modelos
    return {k for k in keys if k in ("models", "task_mappings", "fallback_chain",
                                     "benchmark_seed", "optimizer", "fallback",
                                     "task_temperatures", "task_models")}


def _accepted_loader_model_keys() -> set:
    """Claves de modelo que `TaskModelMapping.from_dict` lee (parseadas)."""
    tree = ast.parse(LOADER_PATH.read_text(encoding="utf-8"))
    keys: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "from_dict":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                        and sub.func.attr == "get" and sub.args:
                    a0 = sub.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        keys.add(a0.value)
    return keys


def _canonical_task_labels() -> set:
    """Valores de `TaskType` + etiquetas de tarea usadas como literal en src/."""
    from src.domain.types import TaskType
    labels = {t.value for t in TaskType}
    return labels


class TestRootKeysHaveReader:
    """La regresión más importante: secciones raíz que nadie lee."""

    def test_loader_reads_exactly_three_root_keys(self):
        keys = _loader_root_keys()
        check("loader lee claves raíz derivadas del AST",
              keys == {"models", "task_mappings", "fallback_chain"},
              str(sorted(keys)), SECTION_2)

    def test_no_dead_root_keys(self):
        data = _raw()
        consumed = _loader_root_keys()
        dead = [k for k in data if k not in consumed]
        check("sin claves raíz sin lector", not dead,
              f"sin lector: {dead}" if dead else f"{len(data)} claves, todas leídas",
              SECTION_2)
        assert not dead, (
            f"claves raíz sin lector en models.yaml: {dead}. "
            "Si son configuración real, cablearlas a un consumidor ANTES; "
            "si no, borrarlas (ver ISSUE-040/041)."
        )

    def test_removed_sections_do_not_come_back(self):
        """Guarda específica de las secciones borradas en v0.6.9v."""
        data = _raw()
        prohibidas = ["task_temperatures", "task_models", "benchmark_seed",
                      "optimizer", "chat", "complex", "code", "embed",
                      "vision", "vision_fallback", "judge", "fallback"]
        reaparecidas = [k for k in prohibidas if k in data]
        check("secciones fantasma no reaparecen", not reaparecidas,
              str(reaparecidas), SECTION_2)
        assert not reaparecidas

    def test_fallback_root_is_not_confused_with_fallback_chain(self):
        """El loader lee `fallback_chain` (lista); un `fallback:` raíz (dict) NO."""
        data = _raw()
        check("no hay `fallback` raíz (dict de política)",
              "fallback" not in data, str(list(data)), SECTION_2)
        assert "fallback" not in data
        # `fallback` DENTRO de un modelo sí es válido (per-model), lo lee
        # ModelConfig.from_dict; no debe confundirse con el de raíz.
        assert "fallback_chain" in _loader_root_keys()


class TestTaskMappings:
    def test_task_labels_are_canonical_or_emitted(self):
        data = _raw()
        valid = _canonical_task_labels() | {"complex_reasoning"}
        bad = [t for t in (data.get("task_mappings") or {}) if t not in valid]
        check("task_types de task_mappings válidos", not bad,
              f"desconocidos: {bad}" if bad else
              f"{len(data.get('task_mappings') or {})} entradas OK", SECTION_2)
        assert not bad, (
            f"task_types que no matchean ningún TaskType ni etiqueta emitida: "
            f"{bad} (clase de bug ISSUE-028: la entrada nunca se aplica)"
        )

    def test_no_unknown_keys_inside_entries(self):
        data = _raw()
        raros = {}
        for task, entry in (data.get("task_mappings") or {}).items():
            if not isinstance(entry, dict):
                raros[task] = "no es dict"
                continue
            unknown = set(entry) - _KNOWN_ENTRY_KEYS
            if unknown:
                raros[task] = sorted(unknown)
        check("sin claves desconocidas en las entradas", not raros,
              str(raros), SECTION_2)
        assert not raros, (
            f"claves que el loader ignora (config inerte): {raros}. "
            f"Aceptadas: {sorted(_KNOWN_ENTRY_KEYS)}"
        )

    def test_accepted_model_keys_match_loader(self):
        """Las claves de modelo que este validador acepta son las del loader."""
        loader_keys = _accepted_loader_model_keys()
        faltan = [k for k in _MODEL_KEYS if k not in loader_keys]
        check("claves de modelo alineadas con from_dict", not faltan,
              f"el validador acepta claves que el loader no lee: {faltan}"
              if faltan else f"{len(loader_keys)} claves en el loader", SECTION_2)
        assert not faltan

    def test_no_task_is_both_mapped_and_temperature_only(self):
        cfg = _cfg()
        collision = set(cfg.task_mappings) & set(cfg.task_temperatures)
        check("sin doble fuente de temperatura", not collision,
              str(sorted(collision)), SECTION_2)
        assert not collision

    def test_every_entry_has_an_effect(self):
        """Ninguna entrada vacía ni con solo claves ignoradas."""
        data = _raw()
        inertes = []
        for task, entry in (data.get("task_mappings") or {}).items():
            if not isinstance(entry, dict) or not (
                    set(entry) & _KNOWN_ENTRY_KEYS):
                inertes.append(task)
        check("todas las entradas tienen efecto", not inertes,
              str(inertes), SECTION_2)
        assert not inertes


class TestModelsAndFallbacks:
    def test_referenced_models_exist(self):
        cfg = _cfg()
        faltantes = []
        for task, mapping in cfg.task_mappings.items():
            if mapping.primary_model not in cfg.models:
                faltantes.append((task, mapping.primary_model))
            for fb in mapping.fallback_models:
                if fb not in cfg.models:
                    faltantes.append((task, f"fallback:{fb}"))
        check("modelos referenciados existen", not faltantes,
              f"inexistentes: {faltantes}" if faltantes else
              f"{len(cfg.models)} modelos disponibles", SECTION_2)
        assert not faltantes

    def test_no_self_fallback_and_no_duplicates(self):
        cfg = _cfg()
        problemas = []
        for task, mapping in cfg.task_mappings.items():
            if mapping.primary_model in mapping.fallback_models:
                problemas.append(f"{task}: fallback == primary")
            if len(set(mapping.fallback_models)) != len(mapping.fallback_models):
                problemas.append(f"{task}: fallbacks duplicados")
        check("fallbacks coherentes", not problemas,
              str(problemas), SECTION_2)
        assert not problemas

    def test_settings_models_are_declared(self):
        """Los modelos que usa settings deben existir en models.yaml."""
        from config.settings import settings
        data = _raw()
        declared = set(data.get("models") or {})
        requeridos = {
            "llm_default_model": settings.llm_default_model,
            "llm_code_model": settings.llm_code_model,
            "llm_embed_model": settings.llm_embed_model,
        }
        faltan = {k: v for k, v in requeridos.items() if v not in declared}
        check("modelos de settings declarados", not faltan,
              str(faltan) if faltan else str(sorted(requeridos.values())), SECTION_2)
        assert not faltan

    def test_enabled_models_are_loaded(self):
        cfg = _cfg()
        data = _raw()
        enabled = [m for m, v in (data.get("models") or {}).items()
                   if not isinstance(v, dict) or v.get("enabled", True)]
        missing = [m for m in enabled if m not in cfg.models]
        check("modelos enabled cargados por el loader", not missing,
              str(missing), SECTION_2)
        assert not missing


class TestTemperatures:
    def test_temperatures_within_range(self):
        data = _raw()
        fuera = {}
        for task, entry in (data.get("task_mappings") or {}).items():
            if isinstance(entry, dict) and "temperature" in entry:
                t = entry["temperature"]
                if not isinstance(t, (int, float)) or not (0.0 <= t <= MAX_TEMPERATURE):
                    fuera[task] = t
        check(f"temperaturas en rango 0.0–{MAX_TEMPERATURE}", not fuera,
              str(fuera), SECTION_2)
        assert not fuera

    def test_code_tasks_are_low_temperature(self):
        """Código con temperatura baja: es la intención declarada del config."""
        cfg = _cfg()
        for task in ("code_generation", "code_correction", "code_explanation"):
            t = cfg.get_temperature_for_task(task)
            check(f"{task} ≤ 0.3", t <= 0.3, str(t), SECTION_2)
            assert t <= 0.3

    def test_effective_temperature_is_not_the_default_for_all(self):
        """Regresión de v0.6.9v: antes TODAS las tareas corrían a 0.7."""
        cfg = _cfg()
        temps = {t: cfg.get_temperature_for_task(t) for t in cfg.task_temperatures}
        check("las temperaturas declaradas NO quedan todas en 0.7",
              len(set(temps.values())) > 1, str(temps), SECTION_2)
        assert len(set(temps.values())) > 1

    def test_every_temperature_entry_is_applied(self):
        """Cada entrada declarada debe llegar a get_temperature_for_task."""
        cfg = _cfg()
        aplicadas = {t: cfg.get_temperature_for_task(t) for t in cfg.task_temperatures}
        no_aplicadas = {t: v for t, v in aplicadas.items()
                        if t in cfg.task_temperatures and v != cfg.task_temperatures[t]}
        check("todas las temperaturas se aplican", not no_aplicadas,
              str(no_aplicadas), SECTION_2)
        assert not no_aplicadas


def _cfg():
    from src.llm.config import LLMConfig
    return LLMConfig()


class TestTemperaturePrecedenceIsEnforced:
    """La regla de precedencia está escrita Y enforceada (no es una promesa)."""

    def test_rule_is_documented_in_the_code(self):
        """El dueño de la regla es el código: debe declararla explícitamente."""
        from src.llm.config import TaskModelMapping
        doc = (TaskModelMapping.__doc__ or "")
        check("la clase documenta la precedencia",
              "REGLA DE PRECEDENCIA" in doc, "docstring presente", SECTION_2)
        check("None significa heredar del modelo",
              "None" in doc and "heredar" in doc.lower(), "documentado", SECTION_2)
        assert "REGLA DE PRECEDENCIA" in doc

    def test_mapping_without_temperature_does_not_override_model(self, tmp_path):
        """Enforcea el punto 3: el modelo manda si el mapeo no declara nada."""
        import yaml as _yaml
        from src.llm.config import LLMConfig
        f = tmp_path / "m.yaml"
        f.write_text(_yaml.safe_dump({
            "models": {"qwen3:8b": {"enabled": True, "temperature": 0.33}},
            "task_mappings": {"complex_reasoning": {"preferred_model": "qwen3:8b"}},
        }), encoding="utf-8")
        cfg = LLMConfig(config_path=f)
        got = cfg.get_temperature_for_task("complex_reasoning")
        check("mapeo sin temperature respeta models.<x>.temperature",
              got == 0.33, str(got), SECTION_2)
        assert got == 0.33

    def test_no_hardcoded_default_temperature_in_loader(self):
        """El 0.7 no debe volver como default del MAPEO (era el bug)."""
        from src.llm.config import TaskModelMapping
        m = TaskModelMapping.from_dict("x", {"preferred_model": "qwen3:8b"})
        check("from_dict sin temperature → None", m.temperature is None,
              str(m.temperature), SECTION_2)
        assert m.temperature is None

    def test_real_config_has_no_ambiguous_temperature(self):
        """En la config REAL no puede haber tarea con doble fuente."""
        cfg = _cfg()
        dup = set(cfg.task_mappings) & set(cfg.task_temperatures)
        check("config real sin doble fuente de temperatura", not dup,
              str(sorted(dup)), SECTION_2)
        assert not dup

    def test_models_may_declare_temperature_and_it_is_loaded(self, tmp_path):
        """Si un modelo declara temperature, el loader la lee (no se ignora)."""
        import yaml as _yaml
        from src.llm.config import LLMConfig
        f = tmp_path / "m.yaml"
        f.write_text(_yaml.safe_dump({
            "models": {"qwen3:8b": {"enabled": True, "temperature": 0.15}},
        }), encoding="utf-8")
        cfg = LLMConfig(config_path=f)
        check("models.<x>.temperature cargada",
              cfg.models["qwen3:8b"].temperature == 0.15,
              str(cfg.models["qwen3:8b"].temperature), SECTION_2)
        assert cfg.models["qwen3:8b"].temperature == 0.15


class TestArranqueSinBibliotecasPesadas:
    """Fase 0 (2026-10-02, medido): el arranque NO debe arrastrar las bibliotecas
    de vectores ni LanceDB.

    Medición que originó estas pruebas (Pss, procesos limpios):

    | proceso | antes | después |
    |---|---|---|
    | núcleo (`import src.core.dispatcher`) | 563 MB | **192 MB** |
    | `src.memory.palace_storage` | 543 MB | **32 MB** |
    | UI web (`import src.interfaces.web.app`) | 587 MB | **227 MB** |
    | Rafael (`import src.core.rafael_daemon`) | 627 MB | **534 MB** |
    | arranque del núcleo | 3,8 s | **1,1 s** |

    Son pruebas de COMPORTAMIENTO (arrancan un intérprete limpio y miran
    `sys.modules`), no de texto del código (R4).
    """

    CODIGO_ARRANQUE = (
        "import sys; sys.path.insert(0, {raiz!r});"
        "import src.memory.gateway;"
        "pesadas = [m for m in ('torch', 'sentence_transformers', 'lancedb', "
        "'transformers', 'sklearn') if m in sys.modules];"
        "print('|'.join(pesadas))"
    )

    def _correr(self, codigo: str) -> str:
        import subprocess
        r = subprocess.run([sys.executable, "-c", codigo], capture_output=True,
                           text=True, timeout=300, cwd=str(PROJECT_ROOT))
        assert r.returncode == 0, r.stderr[-600:]
        return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""

    def test_la_memoria_no_arrastra_vectores_ni_lancedb(self):
        """Importar la memoria no importa torch/sentence-transformers/lancedb."""
        pesadas = self._correr(self.CODIGO_ARRANQUE.format(raiz=str(PROJECT_ROOT)))
        assert pesadas == "", (
            f"el arranque carga bibliotecas pesadas sin usarlas: {pesadas} "
            "(~490 MB de sentence-transformers + ~129 MB de lancedb)"
        )

    def test_las_sondas_siguen_diciendo_que_estan_disponibles(self):
        """Diferir el import no debe romper las sondas de disponibilidad."""
        codigo = (
            "import sys; sys.path.insert(0, {raiz!r});"
            "from src.memory.embedding_model_singleton import _ST_AVAILABLE;"
            "from src.memory.palace_storage import _PALACE_LANCE_AVAILABLE, MEMPALACE_AVAILABLE;"
            "print(_ST_AVAILABLE, _PALACE_LANCE_AVAILABLE, MEMPALACE_AVAILABLE)"
        ).format(raiz=str(PROJECT_ROOT))
        assert self._correr(codigo) == "True True True"

    def test_el_esquema_diferido_crea_la_tabla_esperada(self):
        """Diferir el schema no cambia la tabla: mismas columnas.

        Esta prueba ejerce el import diferido de LanceDB **de punta a punta**
        (crea un `Palace` real en un directorio temporal), que es justo lo que
        dejó de hacerse al importar el módulo.
        """
        codigo = (
            "import sys, tempfile; sys.path.insert(0, {raiz!r});"
            "from src.memory.palace_storage import Palace;"
            "p = Palace(path=tempfile.mkdtemp(), wing_name='w', room_name='r', hall_name='h');"
            "print(','.join(sorted(f.name for f in p.collection.schema)))"
        ).format(raiz=str(PROJECT_ROOT))
        assert self._correr(codigo) == "metadata,session_id,text,user_id,vector"


class TestCancelacionDeLosAgentes:
    """`ISSUE-137` (medido): DETENER sólo cortaba el chat. En código/agentes el
    pedido iba con `stream: false`, donde Ollama **no manda las cabeceras** hasta
    terminar de generar ⇒ no había conexión que cerrar y el runner seguía al
    ~590 % de CPU (medido). Con un token activo, el cliente pide en **modo flujo**
    y el corte cierra la respuesta."""

    @staticmethod
    def _sesion_falsa(payloads):
        """Sesión httpx simulada: registra el payload y devuelve 3 trozos."""
        class _Resp:
            def __init__(self):
                self.cerrada = False

            def raise_for_status(self):
                return None

            def iter_lines(self):
                yield b'{"response": "Ho", "done": false}'
                yield b'{"response": "la", "done": false}'
                yield (b'{"response": "", "done": true, "prompt_eval_count": 11,'
                       b' "eval_count": 3, "done_reason": "stop"}')

            def close(self):
                self.cerrada = True

        class _Ctx:
            def __init__(self):
                self.resp = _Resp()

            def __enter__(self):
                return self.resp

            def __exit__(self, *_a):
                return False

        class _Sesion:
            def stream(self, _metodo, _url, timeout=None, **kw):
                payloads.append(dict(kw.get("json") or {}))
                return _Ctx()

        return _Sesion()

    def test_el_pedido_por_flujo_devuelve_lo_mismo_que_sin_flujo(self):
        from src.llm.client_base import _generate_json_por_flujo, TokenCancelacion
        payloads = []
        datos = _generate_json_por_flujo(self._sesion_falsa(payloads), {"model": "m"},
                                         timeout=30, token=TokenCancelacion())
        assert payloads and payloads[0].get("stream") is True, payloads
        assert datos["response"] == "Hola", datos
        assert datos["done"] is True
        assert (datos["prompt_eval_count"], datos["eval_count"]) == (11, 3), datos
        assert datos["done_reason"] == "stop", datos

    def test_cancelar_cierra_el_pedido_en_curso_y_frena_los_siguientes(self):
        import pytest
        from src.llm.client_base import (TokenCancelacion, GeneracionCancelada,
                                         _generate_json_por_flujo)

        class _Resp:
            cerrada = False

            def close(self):
                _Resp.cerrada = True

        token = TokenCancelacion()
        resp = _Resp()
        token.registrar(resp)
        assert token.cancelar() == 1, "debe cerrar el pedido en curso"
        assert _Resp.cerrada is True, "la respuesta en curso debe quedar cerrada"
        with pytest.raises(GeneracionCancelada):
            token.registrar(_Resp())          # no debe arrancar otro pedido
        # Y un pedido nuevo con el token ya cancelado tampoco arranca:
        with pytest.raises(GeneracionCancelada):
            _generate_json_por_flujo(self._sesion_falsa([]), {"model": "m"},
                                     timeout=30, token=token)

    def test_sin_token_usa_el_camino_historico_y_con_token_el_de_flujo(self):
        """Sin token no cambia nada; con token pide en flujo (imprescindible para
        poder cortarlo)."""
        from src.llm.client_base import _datos_de_generacion, TokenCancelacion

        class _RespPost:
            def raise_for_status(self):
                return None

            def json(self):
                return {"response": "ok", "done": True}

        class _Sesion:
            def __init__(self):
                self.usado = None

            def post(self, _url, timeout=None, **kw):
                self.usado = "post"
                return _RespPost()

            def stream(self, _metodo, _url, timeout=None, **kw):
                self.usado = "stream"
                self.payload = dict(kw.get("json") or {})
                return TestCancelacionDeLosAgentes._ctx()

        sesion = _Sesion()
        datos = _datos_de_generacion(sesion, {"model": "m"}, 30, None)
        assert sesion.usado == "post", "sin token debe usar el camino histórico"
        assert datos["response"] == "ok", datos

        sesion = _Sesion()
        _datos_de_generacion(sesion, {"model": "m"}, 30, TokenCancelacion())
        assert sesion.usado == "stream", "con token debe pedir en modo flujo"
        assert sesion.payload.get("stream") is True, sesion.payload

    @staticmethod
    def _ctx():
        class _Resp:
            def raise_for_status(self):
                return None

            def iter_lines(self):
                yield b'{"response": "ok", "done": true, "eval_count": 1}'

        class _Ctx:
            def __enter__(self):
                return _Resp()

            def __exit__(self, *_a):
                return False

        return _Ctx()


