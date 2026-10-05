#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral (archivo 1/6)
# Sección 1: Estructura del proyecto, config/ y dominio (domain/)
# Cada check registra su resultado en logs/axioma_test_report_*.md
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION = "1. Estructura, configuración y dominio"

try:
    from .suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import
except ImportError:  # pragma: no cover
    from suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import


# ── Helpers ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════


class TestProjectStructure:
    def test_directorios_clave(self):
        section(SECTION)
        for d in ["config", "src", "tests", "logs", "data", "tools", "docs"]:
            check(f"directorio {d}/", (PROJECT_ROOT / d).is_dir(), str(PROJECT_ROOT / d), SECTION)

    def test_archivos_clave(self):
        for f in ["main.py", "README.md", "CHANGELOG.md", "requirements.txt",
                  ".env", ".env.example", "LICENSE"]:
            p = PROJECT_ROOT / f
            check(f"archivo {f}", p.is_file(), f"{p} ({p.stat().st_size if p.exists() else 0} B)", SECTION)

    def test_env_tiene_claves_esenciales(self):
        env_path = PROJECT_ROOT / ".env"
        if not env_path.is_file():
            check(".env presente", False, "no existe .env", SECTION)
            return
        required = ["OLLAMA_HOST", "LLM_DEFAULT_MODEL", "LLM_CODE_MODEL",
                    "LLM_EMBED_MODEL", "LLM_JUDGE_MODEL"]
        content = env_path.read_text(encoding="utf-8", errors="ignore")
        missing = [k for k in required if f"{k}=" not in content]
        check("claves esenciales en .env", not missing,
              "faltan: " + ", ".join(missing) if missing else f"{len(required)} claves presentes", SECTION)

    def test_requirements_cargables(self):
        req = PROJECT_ROOT / "requirements.txt"
        if not req.is_file():
            check("requirements.txt", False, "no existe", SECTION)
            return
        lines = [l.strip() for l in req.read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        check("requirements.txt parseable", len(lines) > 0, f"{len(lines)} dependencias listadas", SECTION)


# ═══════════════════════════════════════════════════════════════
# 1B. config/ — settings
# ═══════════════════════════════════════════════════════════════


class TestConfigSettings:
    def test_settings_import(self):
        section(SECTION)
        self._mod = _check_import("config.settings importable", "config.settings")

    def test_campos_esenciales(self):
        m = _check_import("config.settings (re-import)", "config.settings")
        s = getattr(m, "settings", None)
        if s is None:
            check("settings singleton", False, "no existe settings", SECTION)
            return
        required = ["llm_default_model", "llm_code_model", "llm_vision_model",
                    "llm_embed_model", "llm_judge_model", "llm_fallback_1",
                    "ollama_host", "default_temperature", "validation_threshold"]
        missing = [k for k in required if not hasattr(s, k)]
        check("campos esenciales de settings", not missing,
              "faltan: " + ", ".join(missing) if missing else f"{len(required)} campos OK", SECTION)

    def test_valores_modelos_no_vacios(self):
        from config.settings import settings as s
        bad = [k for k in ["llm_default_model", "llm_code_model"]
               if not getattr(s, k, "")]
        check("modelos principales configurados no vacíos", not bad,
              "faltan: " + ", ".join(bad) if bad else
              f"default={s.llm_default_model}, code={s.llm_code_model}", SECTION)
        check("llm_judge_model (vacío → judge usa fallback)",
              not getattr(s, "llm_judge_model", None) is None or bool(s.llm_fallback_1),
              f"judge={s.llm_judge_model!r} fallback={s.llm_fallback_1}", SECTION)

    def test_host_ollama_con_esquema(self):
        from config.settings import settings as s
        host = s.ollama_host
        check("ollama_host con esquema http(s)", host.startswith(("http://", "https://")),
              str(host), SECTION)

    def test_ensure_directories(self):
        from config.settings import settings as s
        try:
            s.ensure_directories()
            check("settings.ensure_directories()", True, "sin error", SECTION)
        except Exception as e:  # noqa: BLE001
            check("settings.ensure_directories()", False, f"{type(e).__name__}: {e}", SECTION)

    def test_validar_helpers(self):
        from config.settings import get_setting_value, validate_setting
        v = get_setting_value("llm_default_model", "none")
        check("get_setting_value()", isinstance(v, str) and v != "none",
              f"valor={v}", SECTION)
        check("validate_setting('ollama_host')", isinstance(validate_setting("ollama_host", "http://x"), bool),
              "retorna bool", SECTION)


class TestSettingsComputations:
    def test_propiedades_computadas(self):
        section(SECTION)
        from config.settings import settings as s
        checks = {
            "llm_fallback_chain": lambda: len(s.llm_fallback_chain) >= 1,
            "all_models": lambda: len(s.all_models) >= 1,
            "critical_models": lambda: isinstance(s.critical_models, list),
            "optional_models": lambda: isinstance(s.optional_models, list),
            "jarvis_config": lambda: isinstance(s.jarvis_config, dict),
            "mcp_config": lambda: isinstance(s.mcp_config, dict),
            "sandbox_config": lambda: isinstance(s.sandbox_config, dict),
            "embedding_optimizer_config": lambda: isinstance(s.embedding_optimizer_config, dict),
            "vision_config": lambda: isinstance(s.vision_config, dict),
            "agent_loop_config": lambda: isinstance(s.agent_loop_config, dict),
            "should_verify_models_on_startup": lambda: isinstance(s.should_verify_models_on_startup, bool),
        }
        for name, fn in checks.items():
            try:
                ok = bool(fn())
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"
            else:
                detail = "OK"
            check(f"settings.{name}", ok, detail, SECTION)

    def test_timeouts_computados(self):
        from config.settings import settings as s
        try:
            t = s.get_timeout_for_scenario("chat")
            check("get_timeout_for_scenario('chat')", isinstance(t, int) and t > 0, f"{t}s", SECTION)
            t2 = s.get_timeout_for_model(s.llm_default_model, is_first=True)
            check("get_timeout_for_model(default)", isinstance(t2, int) and t2 > 0, f"{t2}s", SECTION)
        except Exception as e:  # noqa: BLE001
            check("timeouts computados", False, f"{type(e).__name__}: {e}", SECTION)

    def test_timeout_pisos_minimos(self):
        """Regresión 2026-08-25: la UI fallaba con ReadTimeout porque el
        timeout estándar (45s) era menor que el tiempo real de generación
        en hardware CPU (~30-37s por 200 tokens). Se aplicaron pisos en
        settings_computations sin tocar .env."""
        from config.settings import settings as s
        try:
            std = s.get_timeout_for_scenario("standard")
            first = s.get_timeout_for_scenario("first_load")
            code = s.get_timeout_for_scenario("code")
            check("timeout estándar >= 90s (piso)", std >= 90, f"{std}s", SECTION)
            check("timeout first_load >= 120s (piso)", first >= 120, f"{first}s", SECTION)
            check("timeout code >= 180s", code >= 180, f"{code}s", SECTION)
        except Exception as e:  # noqa: BLE001
            check("pisos de timeout", False, f"{type(e).__name__}: {e}", SECTION)

    def test_umbrales_y_niveles(self):
        from config.settings import settings as s
        try:
            q = s.get_quality_threshold("code_generation")
            check("get_quality_threshold('code_generation')", 0 <= q <= 10, f"{q}", SECTION)
            lvl = s.get_code_level(80)
            check("get_code_level(80)", isinstance(lvl, int), f"nivel={lvl}", SECTION)
            ma = s.get_max_attempts("code_generation")
            check("get_max_attempts('code_generation')", isinstance(ma, int) and ma > 0, f"{ma}", SECTION)
            perm = s.get_tool_permission_for_agent("coder")
            check("get_tool_permission_for_agent('coder')", isinstance(perm, int), f"{perm}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("umbrales y niveles", False, f"{type(e).__name__}: {e}", SECTION)


# ═══════════════════════════════════════════════════════════════
# 1C. config/ — paths, multimodal_loader y archivos YAML
# ═══════════════════════════════════════════════════════════════


class TestConfigPaths:
    def test_paths_api(self):
        section(SECTION)
        m = _check_import("config.paths importable", "config.paths")
        if isinstance(m, str):
            return
        cls = getattr(m, "Paths", None)
        if cls is None:
            check("config.paths.Paths", False, "no existe clase Paths", SECTION)
            return
        missing = [s for s in ["ensure_exists", "resolve", "validate_writable",
                               "verify_paths", "get_all_paths"] if not hasattr(cls, s)]
        check("config.paths.Paths métodos", not missing,
              "faltan: " + ", ".join(missing) if missing else "5 métodos OK", SECTION)

    def test_paths_get_all(self):
        try:
            from config.paths import Paths
            paths = Paths.get_all_paths()
            check("Paths.get_all_paths()", isinstance(paths, dict) and len(paths) > 0,
                  f"{len(paths)} rutas", SECTION)
        except Exception as e:  # noqa: BLE001
            check("Paths.get_all_paths()", False, f"{type(e).__name__}: {e}", SECTION)


class TestConfigMultimodalLoader:
    def test_loader_api(self):
        section(SECTION)
        m = _check_import("config.multimodal_loader importable", "config.multimodal_loader")
        if isinstance(m, str):
            return
        ok, detail = _has_symbols("config.multimodal_loader",
                                  ["build_vad_config", "build_stt_config", "get_input_device"])
        check("multimodal_loader símbolos", ok, detail, SECTION)

    def test_build_configs(self):
        try:
            from config.multimodal_loader import build_vad_config, build_stt_config
            vad = build_vad_config()
            stt = build_stt_config()
            check("build_vad_config()", hasattr(vad, "__dataclass_fields__") or vad is not None,
                  str(type(vad).__name__), SECTION)
            check("build_stt_config()", hasattr(stt, "__dataclass_fields__") or stt is not None,
                  str(type(stt).__name__), SECTION)
        except Exception as e:  # noqa: BLE001
            check("build_vad/stt_config()", False, f"{type(e).__name__}: {e}", SECTION)


class TestConfigYaml:
    def test_models_yaml(self):
        section(SECTION)
        try:
            import yaml
            data = yaml.safe_load((PROJECT_ROOT / "config" / "models.yaml").read_text())
            check("models.yaml parseable", isinstance(data, dict), f"{len(data)} claves raíz", SECTION)
            models = data.get("models") or data
            check("models.yaml contiene modelos", len(models) > 0, f"{len(models)} entradas", SECTION)
        except Exception as e:  # noqa: BLE001
            check("models.yaml", False, f"{type(e).__name__}: {e}", SECTION)

    def test_ambiguity_patterns_yaml(self):
        try:
            import yaml
            data = yaml.safe_load((PROJECT_ROOT / "config" / "ambiguity_patterns.yaml").read_text())
            needed = {"detector", "rewriter", "clarification"}
            missing = needed - set(data or {})
            check("ambiguity_patterns.yaml secciones", not missing,
                  "faltan: " + ", ".join(missing) if missing else "detector/rewriter/clarification OK", SECTION)
            pats = (data.get("detector") or {}).get("patterns") or {}
            check("ambiguity_patterns patrones", {"high", "medium", "low"} <= set(pats),
                  str(sorted(pats)), SECTION)
        except Exception as e:  # noqa: BLE001
            check("ambiguity_patterns.yaml", False, f"{type(e).__name__}: {e}", SECTION)

    def test_multimodal_yaml(self):
        try:
            import yaml
            data = yaml.safe_load((PROJECT_ROOT / "config" / "multimodal.yaml").read_text())
            check("multimodal.yaml parseable", isinstance(data, dict), f"{len(data)} claves raíz", SECTION)
        except Exception as e:  # noqa: BLE001
            check("multimodal.yaml", False, f"{type(e).__name__}: {e}", SECTION)

    def test_permissions_json(self):
        try:
            data = json.loads((PROJECT_ROOT / "config" / "permissions.json").read_text())
            check("permissions.json parseable", isinstance(data, (dict, list)), f"{len(data)} entradas", SECTION)
        except Exception as e:  # noqa: BLE001
            check("permissions.json", False, f"{type(e).__name__}: {e}", SECTION)


# ═══════════════════════════════════════════════════════════════
# 1D. domain/ — entities, types, exceptions, contracts
# ═══════════════════════════════════════════════════════════════


class TestDomainEntities:
    def test_entities_api(self):
        section(SECTION)
        ok, detail = _has_symbols("src.domain.entities",
                                  ["Message", "MessageRole", "Session", "Response", "AgentState"])
        check("src.domain.entities símbolos", ok, detail, SECTION)

    def test_message(self):
        from src.domain.entities import Message, MessageRole
        m = Message(role=MessageRole.USER, content="hola")
        check("Message construcción", m.role == MessageRole.USER and m.content == "hola",
              f"role={m.role.value}", SECTION)
        d = m.to_dict()
        check("Message.to_dict()", isinstance(d, dict) and d.get("content") == "hola",
              str(list(d)), SECTION)
        with pytest.raises(Exception):
            Message(role=MessageRole.USER, content="")
        check("Message valida contenido vacío", True, "raise en content=''", SECTION)

    def test_session(self):
        from src.domain.entities import Session, Message, MessageRole
        s = Session(session_id="s1")
        s.add_message(Message(role=MessageRole.USER, content="q1"))
        s.add_message(Message(role=MessageRole.ASSISTANT, content="a1"))
        recent = s.get_recent_messages(1)
        check("Session.get_recent_messages(1)", len(recent) == 1 and recent[0].content == "a1",
              f"total={len(s.messages)}", SECTION)
        check("Session.to_dict()", isinstance(s.to_dict(), dict), "OK", SECTION)

    def test_response(self):
        from src.domain.entities import Response
        r = Response(content="resp", task_type="chat", model_used="qwen3:8b", latency_ms=10.0)
        check("Response construcción", r.content == "resp", f"confianza={r.confidence}", SECTION)
        check("Response.is_success (propiedad)", r.is_success is True, "error=None → success", SECTION)
        r2 = Response(content="x", task_type="chat", model_used="m", latency_ms=1.0, error="boom")
        check("Response con error", r2.is_success is False, "error='boom' → no success", SECTION)
        msg = r.to_message()
        check("Response.to_message()", msg.role.value == "assistant", f"metadata={len(msg.metadata or {})}", SECTION)
        check("Response.to_dict()", isinstance(r.to_dict(), dict), "OK", SECTION)

    def test_agent_state(self):
        from src.domain.entities import AgentState
        vals = {v.value for v in AgentState}
        check("AgentState incluye FAILED", "failed" in vals, f"estados={sorted(vals)}", SECTION)


class TestDomainTypes:
    def test_types_api(self):
        section(SECTION)
        ok, detail = _has_symbols("src.domain.types",
                                  ["TaskType", "AgentRole", "AgentPriority", "StatsProvider", "ExecutableAgent"])
        check("src.domain.types símbolos", ok, detail, SECTION)

    def test_tasktype(self):
        from src.domain.types import TaskType
        vals = {t.value for t in TaskType}
        check("TaskType tiene code_generation", "code_generation" in vals, f"{len(vals)} tareas", SECTION)
        check("TaskType.is_code_related", TaskType.is_code_related("code_generation") is True, "OK", SECTION)
        check("TaskType.is_jarvis_task('screen_describe')",
              TaskType.is_jarvis_task("screen_describe") is True, "OK", SECTION)
        check("TaskType.is_conversation", TaskType.is_conversation("general_chat") is True, "OK", SECTION)

    def test_agentrole(self):
        from src.domain.types import AgentRole
        role = AgentRole.get_agent_for_task("code_generation")
        check("AgentRole.get_agent_for_task('code_generation')", role is not None, str(role), SECTION)
        caps = AgentRole.get_capabilities(AgentRole.CODER)
        check("AgentRole.get_capabilities(CODER)", isinstance(caps, dict) and len(caps) > 0,
              str(list(caps)), SECTION)


class TestDomainExceptions:
    def test_exceptions_api(self):
        section(SECTION)
        ok, detail = _has_symbols("src.domain.exceptions",
                                  ["AxiomaError", "ConfigurationError", "LLMError",
                                   "MemoryError", "RouterError", "AgentError",
                                   "IntegrationError", "ValidationError"])
        check("src.domain.exceptions símbolos", ok, detail, SECTION)

    def test_jerarquia(self):
        from src.domain.exceptions import AxiomaError, ConfigurationError
        check("ConfigurationError hereda de AxiomaError", issubclass(ConfigurationError, AxiomaError),
              "OK", SECTION)
        with pytest.raises(AxiomaError):
            raise ConfigurationError("test")
        check("raise ConfigurationError capturado por AxiomaError", True, "OK", SECTION)


class TestDomainContracts:
    def test_contracts_api(self):
        section(SECTION)
        ok, detail = _has_symbols("src.domain.contracts",
                                  ["LLMClientProtocol", "MemoryGatewayProtocol", "RouterProtocol"])
        check("src.domain.contracts símbolos", ok, detail, SECTION)

    def test_protocols_definidos(self):
        try:
            from src.domain.contracts import LLMClientProtocol, MemoryGatewayProtocol, RouterProtocol
            check("Protocols de domain/contracts", all(p is not None for p in
                  [LLMClientProtocol, MemoryGatewayProtocol, RouterProtocol]), "OK", SECTION)
        except Exception as e:  # noqa: BLE001
            check("Protocols de domain/contracts", False, f"{type(e).__name__}: {e}", SECTION)


# ═══════════════════════════════════════════════════════════════
# 1E. config/__init__ y src/__init__
# ═══════════════════════════════════════════════════════════════


class TestPackageInits:
    def test_config_init(self):
        section(SECTION)
        m = _check_import("config importable", "config")
        if not isinstance(m, str):
            check("config/__init__ ok", True, f"attrs={len(dir(m))}", SECTION)

    def test_src_init(self):
        m = _check_import("src importable", "src")
        if not isinstance(m, str):
            check("src/__init__ ok", True, f"attrs={len(dir(m))}", SECTION)

    def test_modulos_config_importables(self):
        for mod in ["config.settings", "config.settings_computations", "config.paths",
                    "config.multimodal_loader"]:
            _check_import(f"import {mod}", mod)


# ═══════════════════════════════════════════════════════════════
# 1F. models.yaml — TEMPERATURA POR TAREA (P0 del informe, v0.6.9v)
# ═══════════════════════════════════════════════════════════════
# La sección raíz `task_temperatures` NO tenía lector y TODAS las tareas corrían a 0.7.
# Se movió a `task_mappings.<task>.temperature` y el loader acepta la forma "SOLO
# temperatura" (sin claves de modelo), imposible antes porque `from_dict` default-eaba
# `primary_model` a CODE_MODEL.


class TestTaskTemperatureWiring:
    @staticmethod
    def _cfg():
        from src.llm.config import LLMConfig
        return LLMConfig()

    def test_mapped_task_temperature_is_applied(self):
        """Una temperatura declarada en el mapeo llega a get_model_for_task."""
        check("code_correction temp 0.10 (no 0.7 default)",
              self._cfg().get_temperature_for_task("code_correction") == 0.10,
              str(self._cfg().get_temperature_for_task("code_correction")), SECTION)

    def test_temperature_only_entry_does_not_change_model(self):
        """⚠️ Clave del diseño: sin claves de modelo NO se rutea a CODE_MODEL."""
        cfg = self._cfg()
        from config.settings import settings
        for task in ("web_search", "time_query", "quality_validation"):
            model = cfg.get_model_for_task(task)
            check(f"{task} mantiene el modelo default",
                  model.name == settings.llm_default_model,
                  f"{task} → {model.name}", SECTION)

    def test_temperature_only_entry_applies_its_temperature(self):
        cfg = self._cfg()
        got = {t: cfg.get_temperature_for_task(t)
               for t in ("web_search", "time_query", "finance_query",
                         "quality_validation", "data_analysis", "news_query",
                         "weather_query")}
        expected = {"web_search": 0.30, "time_query": 0.10,
                    "finance_query": 0.30, "quality_validation": 0.10,
                    "data_analysis": 0.30, "news_query": 0.30,
                    "weather_query": 0.30}
        check("temperaturas solo-temperatura aplicadas", got == expected,
              str(got), SECTION)

    def test_task_without_declared_temperature_keeps_default(self):
        """`code_review` no declara temperatura: no se inventa un valor."""
        cfg = self._cfg()
        check("code_review conserva el default (0.7)",
              cfg.get_temperature_for_task("code_review") == 0.7,
              str(cfg.get_temperature_for_task("code_review")), SECTION)

    def test_unknown_task_still_resolves(self):
        cfg = self._cfg()
        m = cfg.get_model_for_task("tarea_que_no_existe")
        check("tarea desconocida resuelve a un modelo real",
              m is not None and bool(m.name), m.name, SECTION)

    def test_no_temperature_only_entry_collides_with_a_mapping(self):
        """Una tarea no puede estar en task_mappings y en task_temperatures."""
        cfg = self._cfg()
        collision = set(cfg.task_mappings) & set(cfg.task_temperatures)
        check("sin colisión mapeo/temperatura", not collision,
              str(sorted(collision)) if collision else "0 colisiones", SECTION)

    def test_mapped_models_unchanged_by_temperature_edit(self):
        """El saneo cambió temperaturas, NO modelos (regresión de ruteo)."""
        cfg = self._cfg()
        got = {t: cfg.get_model_for_task(t).name for t in cfg.task_mappings}
        expected = {
            "code_generation": "qwen2.5-coder:7b",
            "code_review": "qwen2.5-coder:7b",
            "code_correction": "qwen2.5-coder:7b",
            "code_explanation": "qwen2.5-coder:7b",
            "general_chat": "qwen3:8b",
            "complex_reasoning": "qwen3:8b",
        }
        check("mapeos de modelo intactos", got == expected, str(got), SECTION)

    def test_loader_accepts_empty_mapping_entry(self):
        """Una entrada `{}` no debe romper la carga ni crear un mapeo."""
        import tempfile, yaml as _yaml
        from src.llm.config import LLMConfig
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            _yaml.safe_dump({
                "models": {"qwen3:8b": {"enabled": True}},
                "task_mappings": {"general_chat": {}, "web_search": {"temperature": 0.42}},
            }, fh)
            path = fh.name
        try:
            cfg = LLMConfig(config_path=Path(path))
            check("entrada vacía ignorada", "general_chat" not in cfg.task_mappings,
                  str(list(cfg.task_mappings)), SECTION)
            check("entrada solo-temperature leída",
                  cfg.task_temperatures.get("web_search") == 0.42,
                  str(cfg.task_temperatures), SECTION)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_invalid_temperature_type_is_ignored(self):
        """Un valor no numérico no debe romper: se ignora."""
        import tempfile, yaml as _yaml
        from src.llm.config import LLMConfig
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            _yaml.safe_dump({
                "models": {"qwen3:8b": {"enabled": True}},
                "task_mappings": {"time_query": {"temperature": "alta"}},
            }, fh)
            path = fh.name
        try:
            cfg = LLMConfig(config_path=Path(path))
            check("temperatura inválida ignorada",
                  "time_query" not in cfg.task_temperatures, "OK", SECTION)
        finally:
            Path(path).unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
# 1G. REGLA DE PRECEDENCIA DE TEMPERATURA (v0.6.9v)
# ═══════════════════════════════════════════════════════════════
# Documentada y enforceada: (1) `task_mappings.<tarea>.temperature`, (2) forma
# solo-temperature, (3) `models.<modelo>.temperature`, (4) 0.7 del código. Antes, un mapeo
# SIN `temperature:` forzaba 0.7 y **pisaba en silencio** la del modelo.


class TestTemperaturePrecedenceRule:
    @staticmethod
    def _cfg(tmp_path, yaml_text):
        import yaml as _yaml
        from src.llm.config import LLMConfig
        f = tmp_path / "m.yaml"
        f.write_text(yaml_text, encoding="utf-8")
        return LLMConfig(config_path=f)

    BASE = """
models:
  qwen3:8b:
    enabled: true
    temperature: 0.25
  qwen2.5-coder:7b:
    enabled: true
    temperature: 0.10
"""

    def test_model_temperature_used_when_mapping_has_none(self, tmp_path):
        cfg = self._cfg(tmp_path, self.BASE + """
task_mappings:
  general_chat:
    preferred_model: "qwen3:8b"
""")
        got = cfg.get_temperature_for_task("general_chat")
        check("regla 3: mapeo sin temperature hereda la del MODELO (0.25)",
              got == 0.25, str(got), SECTION)
        assert got == 0.25

    def test_explicit_mapping_temperature_wins_over_model(self, tmp_path):
        cfg = self._cfg(tmp_path, self.BASE + """
task_mappings:
  general_chat:
    preferred_model: "qwen3:8b"
    temperature: 0.80
""")
        got = cfg.get_temperature_for_task("general_chat")
        check("regla 1: temperature del MAPEO gana sobre la del modelo",
              got == 0.80, str(got), SECTION)
        assert got == 0.80

    def test_temperature_only_entry_wins_over_model(self, tmp_path):
        cfg = self._cfg(tmp_path, self.BASE + """
task_mappings:
  web_search:
    temperature: 0.45
""")
        got = cfg.get_temperature_for_task("web_search")
        check("regla 2: entrada solo-temperature gana sobre la del modelo",
              got == 0.45, str(got), SECTION)
        assert got == 0.45

    def test_code_default_when_nothing_declared(self, tmp_path):
        cfg = self._cfg(tmp_path, """
models:
  qwen2.5-coder:7b:
    enabled: true
""")
        got = cfg.get_temperature_for_task("code_generation")
        check("regla 4: sin nada declarado → 0.7 del código", got == 0.7,
              str(got), SECTION)
        assert got == 0.7

    def test_mapping_temperature_is_none_when_absent(self, tmp_path):
        cfg = self._cfg(tmp_path, self.BASE + """
task_mappings:
  general_chat:
    preferred_model: "qwen3:8b"
""")
        check("un mapeo sin temperature queda en None (no en 0.7 hardcodeado)",
              cfg.task_mappings["general_chat"].temperature is None,
              str(cfg.task_mappings["general_chat"].temperature), SECTION)
        assert cfg.task_mappings["general_chat"].temperature is None


# ═══════════════════════════════════════════════════════════════
# POLÍTICA DE TAMAÑO DE LOS TESTS (ISSUE-057 / R13)
# ═══════════════════════════════════════════════════════════════
# Los archivos de test se FUSIONAN por tema en vez de multiplicarse, y ninguno
# debe superar las 950 líneas. Este test es la regla escrita: si un archivo
# crece más, hay que fusionarlo (o dividir por tema) antes de seguir agregando.
class TestTamanoDeArchivosDeTest:
    LIMITE = 950

    def test_ningun_archivo_supera_el_limite(self):
        excedidos = {}
        for path in sorted((PROJECT_ROOT / "tests").glob("test_axioma_*.py")):
            n = len(path.read_text(encoding="utf-8").splitlines())
            if n > self.LIMITE:
                excedidos[path.name] = n
        check(f"ningún test_axioma_* supera {self.LIMITE} líneas", not excedidos,
              str(excedidos) if excedidos else
              f"{len(list((PROJECT_ROOT / 'tests').glob('test_axioma_*.py')))} archivos OK",
              SECTION)
        assert not excedidos, (
            f"archivos por encima de {self.LIMITE} líneas: {excedidos}. "
            "Fusionar por tema (R13, ver docs/AGENT_GUIDE.md) en vez de agregar "
            "archivos nuevos."
        )

    def test_la_suite_no_multiplica_archivos(self):
        """Tope de archivos: si se supera, corresponde fusionar (no agregar)."""
        archivos = sorted((PROJECT_ROOT / "tests").glob("test_axioma_*.py"))
        check("cantidad de archivos de suite razonable (≤ 24)", len(archivos) <= 24,
              f"{len(archivos)} archivos", SECTION)
        assert len(archivos) <= 24


class TestLosTestsNoEscribenEnLosDatosDelUsuario:
    """Política (ISSUE-059): la suite no debe ensuciar `data/` del usuario.

    Caso real: `test_axioma_02_core` creaba `FeedbackLoop()` sin `persist_path`,
    que cae al path por defecto (`data/feedback/<hash>.jsonl`) → **una corrida de
    la suite = un archivo nuevo** en los datos del usuario (se acumularon 37).
    Se detecta con AST: cualquier `FeedbackLoop(...)` en tests debe pasar
    `persist_path`.
    """

    def test_feedback_loop_en_tests_siempre_usa_persist_path(self):
        import ast
        ofensores = []
        for path in sorted((PROJECT_ROOT / "tests").glob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                nombre = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if nombre != "FeedbackLoop":
                    continue
                kwargs = {k.arg for k in node.keywords}
                if "persist_path" not in kwargs:
                    ofensores.append(f"{path.name}:{node.lineno}")
        check("ningún test crea FeedbackLoop sin persist_path", not ofensores,
              str(ofensores) if ofensores else "OK", SECTION)
        assert not ofensores, (
            f"estos tests escribirían en data/feedback/ del usuario: {ofensores} "
            "— pasar `persist_path=tmp_path / 'x.jsonl'`"
        )

    def test_datos_del_usuario_no_tienen_residuo_de_suite(self):
        """Guardia barata: el residuo conocido de tests no debe reaparecer."""
        feedback = PROJECT_ROOT / "data" / "feedback"
        if not feedback.is_dir():
            check("sin carpeta data/feedback (nada que revisar)", True, "—", SECTION)
            return
        import json
        residuo = []
        for f in feedback.glob("*.jsonl"):
            try:
                recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
                        if l.strip()]
            except Exception:
                continue
            if recs and all(str(r.get("session_id") or "") == "s_regresion" for r in recs):
                residuo.append(f.name)
        check("sin archivos de feedback dejados por la suite", not residuo,
              str(residuo) if residuo else "0 residuos", SECTION)
        assert not residuo


class TestFeedbackLoopHermeticoEnTests:
    """Política (ISSUE-067): los tests no deben escribir la SQLite del usuario.

    `FeedbackLoop(...)` sin `db_path` apunta a `data/axioma.db`, donde ahora vive
    la telemetría (`interaction_metrics`, `quality_metrics`). Igual que ISSUE-059
    con `data/feedback/`: en tests se pasa `db_path=tmp_path/…`.
    """

    def test_feedback_loop_en_tests_pasa_db_path(self):
        import ast
        ofensores = []
        for path in sorted((PROJECT_ROOT / "tests").glob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                nombre = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if nombre != "FeedbackLoop":
                    continue
                kwargs = {k.arg for k in node.keywords}
                if "db_path" not in kwargs:
                    ofensores.append(f"{path.name}:{node.lineno}")
        check("ningún test crea FeedbackLoop sin db_path", not ofensores,
              str(ofensores) if ofensores else "OK", SECTION)
        assert not ofensores, (
            f"estos tests escribirían en data/axioma.db del usuario: {ofensores}"
        )


# ═══════════════════════════════════════════════════════════════
# POLÍTICA: `log_degraded` debe estar LIGADO donde se usa (ISSUE-077)
# ═══════════════════════════════════════════════════════════════
# Caso real: `src/memory/gateway.py` y `src/core/dispatcher_process_guard.py` llamaban a
# `log_degraded(...)` sin import que lo ligara (el import existía, pero local dentro de OTRA
# función del archivo) ⇒ `NameError`: ROMPE en vez de degradar. Sin linter general a
# propósito (daría falsos positivos): el chequeo es acotado al helper de R3.
class TestPoliticaDegradacionLigada:
    @staticmethod
    def _ligaduras(fn) -> set:
        import ast
        nombres = set()
        for n in ast.walk(fn):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                nombres.update((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                nombres.add(n.id)
            elif isinstance(n, ast.arg):
                nombres.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                nombres.add(n.name)
        return nombres

    @staticmethod
    def _ligaduras_modulo(tree) -> set:
        """Nombres que liga el MÓDULO. No entra en funciones: un `import` local
        liga esa función, no el módulo — y ese fue exactamente el bug real."""
        import ast
        nombres = set()

        def recorrer(nodos):
            for n in nodos:
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    nombres.update((a.asname or a.name).split(".")[0] for a in n.names)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    nombres.add(n.name)          # liga el nombre, no sus internos
                elif isinstance(n, ast.Assign):
                    nombres.update(t.id for t in n.targets if isinstance(t, ast.Name))
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    nombres.add(n.target.id)
                elif isinstance(n, (ast.Try, ast.If, ast.With, ast.While)):
                    recorrer(n.body)
                    for h in getattr(n, "handlers", []):
                        recorrer(h.body)
                    recorrer(getattr(n, "orelse", []))
                    recorrer(getattr(n, "finalbody", []))

        recorrer(tree.body)
        return nombres

    def test_log_degraded_ligado_en_cada_modulo_que_lo_usa(self):
        import ast
        ofensores = []
        for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            globales = self._ligaduras_modulo(tree)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                locales = self._ligaduras(fn)
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Name) and node.id == "log_degraded"
                            and isinstance(node.ctx, ast.Load)
                            and "log_degraded" not in locales
                            and "log_degraded" not in globales):
                        rel = path.relative_to(PROJECT_ROOT).as_posix()
                        ofensores.append(f"{rel}:{node.lineno} en {fn.name}()")
        check("todo uso de log_degraded está ligado", not ofensores,
              str(ofensores) if ofensores else "", SECTION)
        assert not ofensores, (
            f"estos usos lanzarían NameError al degradar: {ofensores}. "
            "Agregar `from src.utils.degradation import log_degraded` en el módulo."
        )


class TestRunnerDeTestsNoReportaFalsoVerde:
    # ── Movido desde test_axioma_22 (R13: ese archivo quedaba en 957 líneas) ──
    """`ISSUE-087`: un archivo con tests fallidos NO puede contarse como OK.

    Caso real (CI del 2026-09-28): `test_axioma_22` terminó con
    `7 failed, 60 passed` (exit 1) y el runner lo reportó **`PASS*`**, así que su
    resumen dijo "18/18 archivos OK" mientras 7 tests fallaban. La causa era
    `elif "passed" in out`, que da True con cualquier resumen de pytest.
    El gate sí lo detectó (lee el exitstatus por archivo) — por eso la CI marcó
    13/18 —, pero el runner mentía y ocultaba el fallo en su propia salida.
    """

    @staticmethod
    def _status(rc, salida):
        from tools.run_all_tests import _status_de
        return _status_de(rc, salida)

    def test_archivo_con_fallos_no_es_pass(self):
        out = "7 failed, 60 passed in 3.79s"
        got = self._status(1, out)
        check("7 failed, 60 passed + exit 1 → FAIL", got == "FAIL", got, SECTION)
        assert got == "FAIL", f"falso verde: {out!r} se reportó como {got}"

    def test_archivo_con_errores_de_coleccion_no_es_pass(self):
        got = self._status(2, "2 errors in 0.5s")
        check("errores sin 'passed' → FAIL", got == "FAIL", got, SECTION)
        assert got == "FAIL"

    def test_exit_cero_es_pass(self):
        assert self._status(0, "60 passed in 3.0s") == "PASS"

    def test_timeout_y_sin_tests_se_distinguen(self):
        check("exit 124 → TIMEOUT", self._status(124, "") == "TIMEOUT", "", SECTION)
        check("exit 5 → NO-TESTS", self._status(5, "no tests ran") == "NO-TESTS", "", SECTION)
        assert self._status(124, "") == "TIMEOUT"
        assert self._status(5, "no tests ran") == "NO-TESTS"

    def test_pass_estrella_solo_sin_fallos_ni_errores(self):
        got = self._status(1, "60 passed, 2 warnings in 3.0s")
        check("exit 1 con 'passed' y sin fallos → PASS*", got == "PASS*", got, SECTION)
        assert got == "PASS*"

    def test_todos_los_estados_son_considerados_ok_solo_si_pasaron(self):
        from tools.run_all_tests import _status_de
        for estado in ("PASS", "PASS*"):
            assert estado.startswith("PASS"), estado
        for rc, out in ((1, "3 failed, 1 passed"), (124, ""), (2, "1 error")):
            assert not _status_de(rc, out).startswith("PASS"), (rc, out)


class TestGateNombraLosArchivosQueFallan:
    """El gate debe decir CUÁL archivo falla y QUÉ check (ISSUE-091/ISSUE-095).

    Caso real (CI del 2026-09-28): el gate bloqueaba con `17/18 archivos` sin
    decir cuál (un `check()` fallido sin `assert` marca el archivo ❌ pero pytest
    sale 0), y después nombraba el archivo pero no el check.
    """

    # ✅ ISSUE-095: fixture con la ESTRUCTURA REAL del reporte: `> Corrida:` CIERRA
    # el bloque de totales y la tabla del archivo siguiente abre con `# ▶ Corrida:`.
    # El fixture anterior ponía `> Corrida:` ANTES de las filas ⇒ pasaba con un
    # parser que en el reporte real nombraba al archivo equivocado.
    TEXTO = (
        "| directorio docs/ | ❌ FAIL | falta |\n"
        "| otro check | ✅ PASS | ok |\n\n"
        "> Corrida: `test_uno.py`\n\n"
        "- ❌ Fallaron (checks): **1**\n"
        "- **Veredicto: ❌ HAY FALLOS**\n\n"
        "# ▶ Corrida: `test_dos.py`\n\n"
        "| check sano | ✅ PASS | ok |\n\n"
        "> Corrida: `test_dos.py`\n\n"
        "- **Veredicto: ✅ TODO CORRECTO**\n"
    )

    def test_detecta_el_archivo_y_el_check(self):
        from tools.quality_gate import _archivos_con_fallos, _resumen_fallos
        esperado = {"test_uno.py": ["directorio docs/ → falta"]}
        fallos = _archivos_con_fallos(self.TEXTO)
        check("nombra el archivo con ❌ y su check", fallos == esperado, str(fallos), SECTION)
        check("el resumen es legible", _resumen_fallos(fallos) == "test_uno.py (1)",
              _resumen_fallos(fallos), SECTION)
        assert fallos == esperado

    def test_detecta_cuando_falla_el_ultimo_archivo(self):
        """Caso CI real: el archivo que falla puede ser el ÚLTIMO."""
        from tools.quality_gate import _archivos_con_fallos
        texto = (
            "| check sano | ✅ PASS | ok |\n\n"
            "> Corrida: `test_uno.py`\n\n"
            "- **Veredicto: ✅ TODO CORRECTO**\n\n"
            "# ▶ Corrida: `test_dos.py`\n\n"
            "| drenado | ❌ FAIL | refused_ram |\n\n"
            "> Corrida: `test_dos.py`\n\n"
            "- ❌ Fallaron (checks): **1**\n"
            "- **Veredicto: ❌ HAY FALLOS**\n"
        )
        esperado = {"test_dos.py": ["drenado → refused_ram"]}
        fallos = _archivos_con_fallos(texto)
        check("el último archivo se nombra con su check", fallos == esperado,
              str(fallos), SECTION)
        assert fallos == esperado

    def test_sin_fallos_no_reporta_nada(self):
        from tools.quality_gate import _archivos_con_fallos
        solo_ok = (
            "| check | ✅ PASS | ok |\n\n"
            "> Corrida: `a.py`\n\n- **Veredicto: ✅ TODO CORRECTO**\n"
        )
        check("todo OK → sin archivos con fallos", _archivos_con_fallos(solo_ok) == {},
              str(_archivos_con_fallos(solo_ok)), SECTION)
        assert _archivos_con_fallos(solo_ok) == {}

