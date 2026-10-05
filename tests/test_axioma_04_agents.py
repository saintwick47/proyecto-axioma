from __future__ import annotations
import asyncio
import importlib
from unittest.mock import MagicMock, patch
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


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
SECTION = "4. Agentes (src/agents)"
AGENTS_API = {
    "src.agents.base": ["BaseAgent", "AgentConfig", "AgentResult", "AgentPriority"],
    "src.agents.coder": ["CoderAgent", "CriticalSecurityFlawDetected"],
    "src.agents.coder_core": ["extract_code", "analyze_code_quality", "is_complex_request"],
    "src.agents.analyst": ["AnalystAgent"],
    "src.agents.researcher": ["ResearcherAgent"],
    "src.agents.researcher_sources": [],
    "src.agents.searcher": ["SearcherAgent"],
    "src.agents.validator": ["ValidatorAgent"],
    "src.agents.vision_agent": ["VisionAgent"],
    "src.agents.system_agent": ["SystemAgent"],
    "src.agents.code_contract": ["parse_code_contract", "build_contract_critique"],
    "src.agents.code_validator": ["CodeValidator"],
    "src.agents.parallel_executor": ["ParallelExecutor", "ExecutionResult", "RetryConfig"],
    "src.agents.loop.agent_loop": ["AgentLoop"],
    "src.agents.loop.iteration_tracker": ["IterationTracker"],
    "src.agents.loop.reflection_engine": ["ReflectionEngine", "ReflectionResult", "ReflectionOutcome"],
    "src.agents.loop.state_manager": ["LoopState", "LoopPhase"],
    "src.agents.loop.stop_conditions": ["StopConditions", "StopReason"],
    "src.agents.loop.types": [],
}


class TestImportsYApi:
    def test_agentes(self):
        section(SECTION)
        for mod, syms in AGENTS_API.items():
            _api_check(mod, syms)


class TestBaseAgent:
    def test_agent_result(self):
        from src.agents.base import AgentResult
        r = AgentResult(success=True, content="ok", agent_type="coder",
                        model_used="m", task_type="code_generation", latency_ms=1.0)
        check("AgentResult() construible", r.success is True and r.content == "ok",
              f"task={r.task_type}", SECTION)
        check("AgentResult.to_response()", hasattr(r, "to_response"), "método", SECTION)

    def test_base_execute_no_implementado(self):
        from src.agents.base import BaseAgent

        class _Concrete(BaseAgent):
            async def execute(self, msg, **kwargs):  # pragma: no cover
                raise NotImplementedError

        agent = _Concrete(model="test-model")
        check("BaseAgent subclase construible", agent is not None, "OK", SECTION)
        check("BaseAgent.config.model", agent.config.model == "test-model", "OK", SECTION)


class TestCoderCore:
    def test_extract_code(self):
        from src.agents.coder_core import extract_code
        blocks = extract_code("```python\nprint(1)\n```\n```javascript\nconsole.log(1)\n```")
        check("extract_code() multi-lenguaje", len(blocks) == 2,
              f"{len(blocks)} bloques: {[b['language'] for b in blocks]}", SECTION)

    def test_is_complex_request(self):
        from src.agents.coder_core import is_complex_request
        complex_ok, detail = is_complex_request(
            "construye una aplicacion completa similar a un framework "
            "pero mejor con todas las funciones de django y react")
        check("is_complex_request() detecta complejidad", complex_ok is True,
              f"detail={detail[:40]}", SECTION)

    def test_analyze_code_quality(self):
        from src.agents.coder_core import analyze_code_quality
        from config.settings import settings
        res = analyze_code_quality("def f():\n    return 42\n", settings)
        check("analyze_code_quality()", isinstance(res, dict) and "quality_score" in res,
              f"score={res.get('quality_score')}", SECTION)


class TestCoderAgent:
    def test_execute_mockeado(self):
        from src.agents.coder import CoderAgent
        from src.domain.entities import Message, MessageRole
        try:
            with patch("src.agents.base.OllamaClient") as MockCls:
                inst = MockCls.return_value
                resp = MagicMock()
                resp.content = "```python\nprint('hola')\n```"
                resp.model = "m"
                resp.done = True
                inst.generate.return_value = resp
                inst.chat.return_value = resp
                agent = CoderAgent(model="m", timeout=1, enable_memory=False)
                msg = Message(role=MessageRole.USER, content="escribe hola mundo")
                result = agent.execute(msg, task_type="code_generation")
                check("CoderAgent.execute() con mock", result.success is True,
                      f"latency={result.latency_ms:.0f}ms", SECTION)
        except Exception as e:  # noqa: BLE001
            check("CoderAgent.execute() con mock", False, f"{type(e).__name__}: {e}", SECTION)

    def test_metodos_publicos(self):
        from src.agents.coder import CoderAgent
        for meth in ["generate_code", "debug_code", "explain_code", "get_code_stats"]:
            check(f"CoderAgent.{meth} existe", hasattr(CoderAgent, meth), "OK", SECTION)

    # ✅ REGRESIÓN v0.6.8u: _build_prompt usaba session_id/user_id sin
    # declararlos → NameError rompía TODA generación de código.
    def test_build_prompt_con_session_y_user(self):
        from src.agents.coder import CoderAgent
        from src.domain.entities import Message, MessageRole
        try:
            agent = CoderAgent(model="m", timeout=1, enable_memory=False)
            msg = Message(role=MessageRole.USER, content="crea un programa en python")
            prompt = agent._build_prompt(
                msg, "code_generation", session_id="s1", user_id="u1",
            )
            check("CoderAgent._build_prompt(session_id, user_id) sin NameError",
                  isinstance(prompt, str) and len(prompt) > 0, f"len={len(prompt)}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("CoderAgent._build_prompt(session_id, user_id) sin NameError",
                  False, f"{type(e).__name__}: {e}", SECTION)


class TestOtrosAgentes:
    def _try_init(self, cls, name, **kw):
        try:
            agent = cls(**kw)
            check(f"{name}() construible", agent is not None, type(agent).__name__, SECTION)
        except Exception as e:  # noqa: BLE001
            check(f"{name}() construible", False, f"{type(e).__name__}: {e}", SECTION)

    def test_analyst(self):
        from src.agents.analyst import AnalystAgent
        self._try_init(AnalystAgent, "AnalystAgent", model="m", timeout=1)

    def test_researcher(self):
        from src.agents.researcher import ResearcherAgent
        self._try_init(ResearcherAgent, "ResearcherAgent", model="m", timeout=1)

    def test_searcher(self):
        from src.agents.searcher import SearcherAgent
        self._try_init(SearcherAgent, "SearcherAgent", model="m", timeout=1)

    def test_validator(self):
        from src.agents.validator import ValidatorAgent
        self._try_init(ValidatorAgent, "ValidatorAgent", model="m", timeout=1)

    def test_vision_agent(self):
        from src.agents.vision_agent import VisionAgent
        self._try_init(VisionAgent, "VisionAgent", model="m", timeout=1)

    def test_system_agent(self):
        from src.agents.system_agent import SystemAgent
        self._try_init(SystemAgent, "SystemAgent")

    def test_researcher_sources(self):
        m = _import("src.agents.researcher_sources")
        check("researcher_sources importable", not isinstance(m, str),
              "" if not isinstance(m, str) else m, SECTION)

    # ✅ REGRESIÓN v0.6.8u: _build_search_prompt usaba session_id/user_id
    # sin declararlos → NameError rompía la búsqueda web general.
    def test_researcher_build_search_prompt_con_session(self):
        from src.agents.researcher import ResearcherAgent
        from src.domain.entities import Message, MessageRole
        try:
            agent = ResearcherAgent(model="m", timeout=1, enable_memory=False)
            msg = Message(role=MessageRole.USER, content="noticias de tecnologia")
            prompt = agent._build_search_prompt(
                msg, [], task_type="web_search", session_id="s2", user_id="u2",
            )
            check("ResearcherAgent._build_search_prompt(session_id, user_id) sin NameError",
                  isinstance(prompt, str) and len(prompt) > 0, f"len={len(prompt)}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("ResearcherAgent._build_search_prompt(session_id, user_id) sin NameError",
                  False, f"{type(e).__name__}: {e}", SECTION)


class TestAgentLoop:
    def test_iteration_tracker(self):
        from src.agents.loop.iteration_tracker import IterationTracker
        it = IterationTracker(max_iterations=3)
        it.start()
        check("IterationTracker.current_iteration", it.current_iteration == 0, "OK", SECTION)
        it.next_iteration()
        check("IterationTracker.next_iteration", it.current_iteration == 1, "OK", SECTION)
        check("IterationTracker.has_reached_max", it.has_reached_max() is False, "1/3", SECTION)
        check("IterationTracker.to_dict()", isinstance(it.to_dict(), dict), "OK", SECTION)

    def test_stop_conditions(self):
        from src.agents.loop.stop_conditions import StopConditions, StopReason
        sc = StopConditions(max_iterations=2, global_timeout=180, max_consecutive_failures=2)
        check("StopConditions() construible", sc.is_stopped is False, "OK", SECTION)
        sc.start()
        must_stop = sc.check(iteration=1, consecutive_failures=0)
        check("StopConditions.check(iteration=1)", isinstance(must_stop, bool) and must_stop is False,
              "no debe parar en iter 1", SECTION)
        sc.stop(StopReason.MAX_ITERATIONS)
        check("StopConditions.stop()", sc.is_stopped is True, str(sc.stop_reason), SECTION)

    def test_reflection_engine(self):
        from src.agents.loop.reflection_engine import ReflectionEngine
        re = ReflectionEngine()
        res = re.evaluate("respuesta de prueba", 0.9)
        check("ReflectionEngine.evaluate()", res is not None, f"outcome={res.outcome}", SECTION)

    def test_agent_loop_mockeado(self):
        async def _run():
            from src.agents.loop.agent_loop import AgentLoop
            loop = AgentLoop(agent_type="coder", max_iterations=2)
            return loop

        try:
            loop = asyncio.run(_run())
            check("AgentLoop() construible", loop is not None, "OK", SECTION)
        except Exception as e:  # noqa: BLE001
            check("AgentLoop() construible", False, f"{type(e).__name__}: {e}", SECTION)


class TestParallelExecutor:
    def test_init(self):
        from src.agents.parallel_executor import ParallelExecutor
        pe = ParallelExecutor(max_concurrent=2)
        check("ParallelExecutor() construible", pe is not None, "OK", SECTION)

    def test_execute_batch_mockeado(self):
        async def _run():
            from src.agents.base import AgentResult
            from src.agents.parallel_executor import ParallelExecutor
            from src.core.task_queue import TaskPriority

            async def _handler(payload, **kw):
                return AgentResult(success=True, content="ok", agent_type="coder",
                                   model_used="m", task_type="code_generation", latency_ms=1.0)

            pe = ParallelExecutor(max_concurrent=2)
            tasks = [
                {"agent_type": "coder", "handler": _handler, "payload": {"q": "q1"},
                 "priority": TaskPriority.HIGH, "task_id": "t1"},
                {"agent_type": "coder", "handler": _handler, "payload": {"q": "q2"},
                 "priority": TaskPriority.NORMAL, "task_id": "t2"},
            ]
            results = await pe.execute_batch(tasks, parallel=True)
            return results

        try:
            results = asyncio.run(_run())
            check("ParallelExecutor.execute_batch()", isinstance(results, list) and len(results) == 2,
                  f"{len(results)} resultados", SECTION)
        except Exception as e:  # noqa: BLE001
            check("ParallelExecutor.execute_batch()", False, f"{type(e).__name__}: {e}", SECTION)


class TestCodeContract:
    def test_parse(self):
        from src.agents.code_contract import parse_code_contract
        from config.settings import settings
        contract = ("```python\n# main.py\nprint('hola')\n```\n"
                    "Test: python -m pytest\n")
        cc = parse_code_contract(contract, settings, query="genera un programa")
        check("parse_code_contract()", cc is not None,
              f"files={len(getattr(cc, 'files', []) or [])}", SECTION)

    def test_validator_estructura(self):
        from src.agents.code_validator import CodeValidator
        check("CodeValidator clase", CodeValidator is not None, "OK", SECTION)
        check("CodeValidator métodos estáticos", hasattr(CodeValidator, "validate_static") or
              any(m.startswith("validate") for m in dir(CodeValidator)), "OK", SECTION)


class TestLoopTypes:
    def test_loop_types(self):
        m = _import("src.agents.loop.types")
        check("loop/types importable", not isinstance(m, str), "" if not isinstance(m, str) else m, SECTION)

    def test_state_manager(self):
        from src.agents.loop.state_manager import LoopState, LoopPhase
        check("LoopState enum", len(list(LoopState)) >= 3, str([s.value for s in LoopState]), SECTION)
        check("LoopPhase enum", len(list(LoopPhase)) >= 3, str([p.value for p in LoopPhase]), SECTION)


class TestValidatorScoreExtraction:
    def _make(self):
        from src.agents.validator import ValidatorAgent
        return ValidatorAgent.__new__(ValidatorAgent)

    def test_score_markdown_bold(self):
        v = self._make()
        s = v._extract_score_from_response("**SCORE:** 10.0\n**VEREDICTO:** APROBADO")
        check("B1 score markdown **SCORE:** 10.0", s == 10.0, f"score={s}", SECTION)

    def test_score_plano(self):
        v = self._make()
        s = v._extract_score_from_response("SCORE: 8.5\nVEREDICTO: APROBADO")
        check("B1 score plano 8.5", s == 8.5, f"score={s}", SECTION)

    def test_fallback_simetrico_aprobado(self):
        v = self._make()
        s = v._extract_score_from_response(
            "Resultado correcto y completo\nVEREDICTO: APROBADO")
        check("B1 fallback aprobado → 8.0", s == 8.0, f"score={s}", SECTION)

    def test_fallback_simetrico_rechazado(self):
        v = self._make()
        s = v._extract_score_from_response(
            "**VEREDICTO:** RECHAZADO\nCRITIQUE: faltan fuentes")
        check("B1 rechazado → 4.0", s == 4.0, f"score={s}", SECTION)

    def test_verdict_negacion(self):
        v = self._make()
        ver = v._extract_verdict_from_response("no fue APROBADO el resultado")
        check("B1 negación 'no ... APROBADO' → False", ver is False, f"ver={ver}", SECTION)

    def test_critique_markdown(self):
        v = self._make()
        crit = v._extract_critique_from_response(
            "**CRITIQUE:** faltan fuentes\n**VEREDICTO:** RECHAZADO")
        check("B1 critique markdown limpia", "faltan fuentes" in crit, f"crit={crit!r}", SECTION)


class TestCodeQualityV09a:
    def test_scoring_type_hints_y_asserts(self):
        from config.settings import settings
        from src.agents.coder_core import analyze_code_quality
        sample = '''CÓDIGO:
ARCHIVO: suma.py
```python
def sumar(a: int, b: int) -> int:
    """Suma."""
    return a + b
```
TESTS:
ARCHIVO: test_suma.py
```python
from suma import sumar

def test_sumar():
    assert sumar(2, 3) == 5
    assert sumar(-1, 1) == 0
```
'''
        a = analyze_code_quality(sample, settings)
        check("v0.6.9a type_hints por AST", a["has_type_hints"] is True,
              f"type_hints={a['has_type_hints']}", SECTION)
        check("v0.6.9a asserts contados", a["tests_count"] >= 2,
              f"tests_count={a['tests_count']}", SECTION)
        check("v0.6.9a score alto", a["quality_score"] > 0.7,
              f"score={a['quality_score']:.2f}", SECTION)

    def test_strip_outer_fence(self):
        from src.agents.code_contract import _strip_outer_fence
        t = _strip_outer_fence("```plaintext\n1 passed\n```")
        check("v0.6.9a fence plaintext limpio", t == "1 passed", repr(t), SECTION)
        check("v0.6.9a sin fence intacto",
              _strip_outer_fence("texto plano") == "texto plano", "OK", SECTION)


# ── Fusionado desde test_axioma_05_tools_multimodal.py ──
import json
from unittest.mock import AsyncMock

SECTION_2 = "5. Herramientas MCP y multimodal"
TOOLS_API = {
    "src.tools_mcp.tool_base": ["BaseTool", "ToolPermission", "ToolResult", "ToolSchema",
                                "ToolParam", "ToolError", "ToolPermissionError",
                                "ToolNotFoundError", "ToolTimeoutError"],
    "src.tools_mcp.tool_registry": ["ToolRegistry", "get_registry"],
    "src.tools_mcp.tool_executor": ["ToolExecutor", "ExecutionContext", "get_executor"],
    "src.tools_mcp.executor_infrastructure": ["ToolMetrics", "CircuitBreaker", "CircuitBreakerRegistry"],
    "src.tools_mcp.permissions": ["PermissionGateway", "PermissionMode", "Decision", "get_gateway"],
    "src.tools_mcp.sandbox": ["SandboxExecutor", "SandboxConfig", "SandboxMode", "SandboxResult",
                              "get_sandbox_executor"],
    "src.tools_mcp.sandbox_staging": ["StagingFS", "StagingEntry", "get_staging"],
    "src.tools_mcp.tool_implementations": ["WeatherTool", "FinanceTool", "NewsTool",
                                           "WebSearchTool", "FsReadTool", "FsWriteTool"],
    "src.tools_mcp.mcp.mcp_protocol": ["JsonRpcRequest", "JsonRpcResponse", "McpMethod",
                                       "McpServerInfo", "McpToolDefinition", "McpToolCallResult",
                                       "build_initialize_request", "build_tools_list_request",
                                       "build_tools_call_request", "build_ping_request",
                                       "parse_tools_list", "MCP_PROTOCOL_VERSION"],
    "src.tools_mcp.mcp.mcp_client": ["McpClient", "McpTransport"],
    "src.tools_mcp.mcp.mcp_tool_bridge": ["McpToolBridge", "McpDynamicTool", "McpServerEntry",
                                          "get_bridge"],
    "src.tools_mcp.tools.workspace_tool": ["WorkspaceManageTool"],
}
MULTIMODAL_API = {
    "src.multimodal.pipeline_core": ["UtteranceResult", "process_utterance_core",
                                     "_classify_jarvis_intent", "_predict_handler_weights"],
    "src.multimodal.audio_pipeline": ["AudioPipeline", "PipelineResult", "get_pipeline"],
    "src.multimodal.voice_pipeline": ["VoicePipeline", "VoicePipelineStats"],
    "src.multimodal.stt_engine": ["STTEngine", "STTConfig", "TranscriptionResult"],
    "src.multimodal.tts_engine": ["TTSEngine", "TTSConfig"],
    "src.multimodal.vad_filter": ["VADFilter", "VADConfig", "AudioChunk"],
    "src.multimodal.wake_word": ["WakeWordDetector"],
    "src.multimodal.camera_engine": ["CameraCaptureEngine"],
    "src.multimodal.screen_capture_engine": ["MonitorDetector", "ContextHintDetector",
                                             "CaptureFrame", "MonitorInfo"],
    "src.multimodal.ptt_axioma": ["process_audio", "main"],
}


class TestImportsYApiTools:
    def test_tools_mcp(self):
        section(SECTION_2)
        for mod, syms in TOOLS_API.items():
            _api_check(mod, syms)

    def test_multimodal(self):
        for mod, syms in MULTIMODAL_API.items():
            _api_check(mod, syms)


class TestToolBase:
    def test_tool_permission(self):
        from src.tools_mcp.tool_base import ToolPermission
        levels = {t.value for t in ToolPermission}
        check("ToolPermission niveles", levels == {0, 1, 2, 3, 4}, str(sorted(levels)), SECTION_2)

    def test_tool_result(self):
        from src.tools_mcp.tool_base import ToolResult
        r = ToolResult(success=True, output="salida", tool_name="test_tool", metadata={"k": "v"})
        check("ToolResult(success) construible", r.success is True and r.output == "salida",
              f"tool={r.tool_name}", SECTION_2)
        check("ToolResult.to_dict()", isinstance(r.to_dict(), dict), "OK", SECTION_2)


class TestToolRegistry:
    def test_registro_default(self):
        from src.tools_mcp.tool_registry import get_registry
        reg = get_registry()
        names = reg.names()
        needed = {"fs_read", "fs_write", "fs_list", "terminal_exec", "web_search",
                  "weather", "sys_env", "workspace_manage"}
        check("ToolRegistry herramientas base", needed <= set(names),
              f"faltan: {sorted(needed - set(names))}" if needed - set(names) else f"{len(names)} tools", SECTION_2)
        check("get_registry() singleton", reg is get_registry(), "misma instancia", SECTION_2)

    def test_registro_manual(self):
        from src.tools_mcp.tool_base import BaseTool, ToolResult, ToolPermission

        class _EchoTool(BaseTool):
            name = "test_echo"
            description = "echo"
            permission = ToolPermission.EXECUTE

            def _run(self, text: str = "", **kwargs) -> ToolResult:
                return ToolResult(success=True, output=text, tool_name=self.name)

        from src.tools_mcp.tool_registry import get_registry
        reg = get_registry()
        reg.register(_EchoTool())
        check("ToolRegistry.register()", reg.has("test_echo") is True, "OK", SECTION_2)
        res = reg.execute("test_echo", text="hola")
        check("ToolRegistry.execute()", res.success is True and res.output == "hola",
              f"output={res.output}", SECTION_2)
        reg.unregister("test_echo")
        check("ToolRegistry.unregister()", reg.has("test_echo") is False, "OK", SECTION_2)

    def test_stats(self):
        from src.tools_mcp.tool_registry import get_registry
        stats = get_registry().get_stats()
        check("ToolRegistry.get_stats()", "total_registered" in stats and "tool_names" in stats,
              f"{stats.get('total_registered')} tools", SECTION_2)


class TestSandbox:
    def test_config(self):
        from src.tools_mcp.sandbox import SandboxConfig
        cfg = SandboxConfig()
        check("SandboxConfig() construible", cfg is not None, "OK", SECTION_2)
        check("SandboxConfig tiene workdir", hasattr(cfg, "workdir") or hasattr(cfg, "work_dir"), "OK", SECTION_2)


class TestMcpProtocol:
    def test_requests(self):
        from src.tools_mcp.mcp.mcp_protocol import (build_initialize_request, build_ping_request,
                                                    build_tools_list_request, McpMethod)
        req = build_initialize_request(client_name="suite", client_version="1.0")
        check("build_initialize_request()", req.method == McpMethod.INITIALIZE.value,
              f"id={req.id}", SECTION_2)
        ping = build_ping_request()
        check("build_ping_request()", ping.method == McpMethod.PING.value, "OK", SECTION_2)
        tools = build_tools_list_request()
        check("build_tools_list_request()", tools.method == McpMethod.TOOLS_LIST.value, "OK", SECTION_2)
        check("MCP_PROTOCOL_VERSION definido",
              __import__("src.tools_mcp.mcp.mcp_protocol", fromlist=["MCP_PROTOCOL_VERSION"]).MCP_PROTOCOL_VERSION,
              "OK", SECTION_2)

    def test_parse_tools_list(self):
        from src.tools_mcp.mcp.mcp_protocol import JsonRpcResponse, parse_tools_list
        resp = JsonRpcResponse(id=1, result={"tools": [
            {"name": "t1", "description": "d1", "inputSchema": {"type": "object"}}
        ]})
        tools = parse_tools_list(resp)
        check("parse_tools_list()", len(tools) == 1 and tools[0].name == "t1", f"{len(tools)} tools", SECTION_2)


class TestPermissions:
    def test_gateway(self, tmp_path):
        from src.tools_mcp.permissions import PermissionGateway, get_gateway
        cfg = tmp_path / "perm_test.json"
        cfg.write_text(json.dumps({"rules": []}))
        gw = get_gateway(config_path=cfg)
        check("get_gateway()", gw is not None, type(gw).__name__, SECTION_2)


class TestStagingFS:
    def test_staging(self, tmp_path):
        from src.tools_mcp.sandbox_staging import StagingFS
        fs = StagingFS.__new__(StagingFS)
        check("StagingFS clase", fs is not None, "OK", SECTION_2)


class TestPipelineCore:
    def test_jarvis_intent(self):
        from src.multimodal.pipeline_core import _classify_jarvis_intent
        r = _classify_jarvis_intent("analiza la pantalla")
        check("_classify_jarvis_intent('analiza la pantalla')", isinstance(r, str) and r,
              f"intent={r}", SECTION_2)

    def test_predict_weights(self):
        from src.multimodal.pipeline_core import _predict_handler_weights
        w = _predict_handler_weights("analiza la pantalla y toma captura")
        check("_predict_handler_weights()", isinstance(w, dict), str(sorted(w)), SECTION_2)

    def test_utterance_result(self):
        from src.multimodal.pipeline_core import UtteranceResult
        r = UtteranceResult(success=True, response_content="hola")
        check("UtteranceResult(success)", r.success is True and r.reason == "ok", "OK", SECTION_2)
        r2 = UtteranceResult(success=False, reason="not_actionable")
        check("UtteranceResult(not_actionable)", r2.success is False, r2.reason, SECTION_2)

    def test_process_utterance_no_accionable(self):
        async def _run():
            from src.multimodal.pipeline_core import process_utterance_core
            stt = MagicMock()
            stt.is_actionable = False
            stt.needs_repeat = False
            tts = AsyncMock()
            dispatcher = AsyncMock()
            res = await process_utterance_core(stt, tts, dispatcher)
            return res

        try:
            res = asyncio.run(_run())
            check("process_utterance_core(no accionable)", res.success is False and
                  res.reason == "not_actionable", res.reason, SECTION_2)
        except Exception as e:  # noqa: BLE001
            check("process_utterance_core(no accionable)", False, f"{type(e).__name__}: {e}", SECTION_2)


class TestAudioPipeline:
    def test_clase(self):
        from src.multimodal.audio_pipeline import AudioPipeline, PipelineResult
        check("AudioPipeline clase", AudioPipeline is not None, "OK", SECTION_2)
        r = PipelineResult(transcription=None, dispatcher_response=None, success=True)
        check("PipelineResult(success)", r.success is True, "OK", SECTION_2)


class TestVoicePipeline:
    def test_stats(self):
        from src.multimodal.voice_pipeline import VoicePipelineStats
        s = VoicePipelineStats()
        check("VoicePipelineStats() construible", s is not None, "OK", SECTION_2)


class TestSTT:
    def test_transcription_result(self):
        from src.multimodal.stt_engine import TranscriptionResult
        r = TranscriptionResult(text="hola", confidence=0.9)
        check("TranscriptionResult() construible", r.text == "hola", f"conf={r.confidence}", SECTION_2)


class TestTTS:
    def test_config(self):
        from src.multimodal.tts_engine import TTSConfig
        cfg = TTSConfig()
        check("TTSConfig() construible", cfg is not None, "OK", SECTION_2)


class TestVAD:
    def test_chunk(self):
        import numpy as np
        from src.multimodal.vad_filter import AudioChunk, VADConfig
        cfg = VADConfig()
        check("VADConfig() construible", cfg is not None, "OK", SECTION_2)
        c = AudioChunk(audio=np.zeros(160, dtype=np.int16), duration_seconds=0.01, voice_ratio=0.0)
        check("AudioChunk() construible", c.duration_seconds == 0.01, "OK", SECTION_2)


class TestScreenCapture:
    def test_monitor_detector(self):
        from src.multimodal.screen_capture_engine import MonitorDetector
        md = MonitorDetector()
        monitors = md.get_monitors()
        check("MonitorDetector.get_monitors()", isinstance(monitors, list), f"{len(monitors)} monitores", SECTION_2)
        primary = md.get_primary()
        check("MonitorDetector.get_primary()", primary is not None, str(primary), SECTION_2)

    def test_context_hint(self):
        from src.multimodal.screen_capture_engine import ContextHintDetector
        chd = ContextHintDetector()
        hint = chd.detect("")
        check("ContextHintDetector.detect('')", hint == "generic", f"hint={hint!r}", SECTION_2)
        hint2 = chd.detect("firefox")
        check("ContextHintDetector.detect('firefox')", hint2 == "web", f"hint={hint2!r}", SECTION_2)


class TestMcpClient:
    def test_init(self):
        from src.tools_mcp.mcp.mcp_client import McpClient, McpTransport
        with patch("src.tools_mcp.mcp.mcp_client.httpx") as mock_httpx:
            mock_httpx.Client.return_value = MagicMock()
            client = McpClient(url="http://localhost:9999", transport=McpTransport.HTTP)
            check("McpClient() construible", client is not None, "OK", SECTION_2)
            check("McpClient transport", client.transport == McpTransport.HTTP, str(client.transport), SECTION_2)


class TestWorkspaceTool:
    def test_instancia(self):
        from src.tools_mcp.tools.workspace_tool import WorkspaceManageTool
        tool = WorkspaceManageTool()
        check("WorkspaceManageTool() construible", tool is not None, f"name={tool.name}", SECTION_2)


# ═══════════════════════════════════════════════════════════════
# P1.1: `AgentResult.to_response()` propaga las fuentes (y las sanea)
# ═══════════════════════════════════════════════════════════════
# Antes, `metadata["sources"]` (lo que llenan researcher/searcher) **no** se
# copiaba a `Response.sources` → quedaba en [] aunque hubiera citas reales. Eso
# rompía la telemetría por turno (num_sources=0) y el respaldo de confianza.
class TestToResponseFuentes:
    @staticmethod
    def _mk(sources):
        from src.agents.base import AgentResult
        return AgentResult(success=True, content="x", agent_type="researcher",
                           model_used="qwen3:8b", task_type="research",
                           latency_ms=10.0, confidence=0.85,
                           metadata={"sources": sources})

    def test_copia_las_fuentes_al_campo_dedicado(self):
        r = self._mk(["Doc A - https://a.com", "Doc B - https://b.com"]).to_response("s")
        check("Response.sources poblado (antes quedaba en [])",
              r.sources == ["Doc A - https://a.com", "Doc B - https://b.com"],
              str(r.sources), SECTION)
        assert len(r.sources) == 2

    def test_sin_fuentes_queda_vacio(self):
        for vacio in ([], None):
            r = self._mk(vacio).to_response("s")
            check(f"sources={vacio!r} → []", r.sources == [], str(r.sources), SECTION)
        assert self._mk(None).to_response("s").sources == []

    def test_sanea_valores_raros_sin_romper(self):
        """`metadata` es libre: un valor raro no debe romper el Response.

        Casos medidos: int → TypeError; lista con no-str → ValidationError;
        dict → copiaba las CLAVES; string → lo partía por carácter.
        """
        casos = {
            "int (len)": (3, []),
            "dict": ({"a": 1}, []),
            "lista con no-str": (["ok", 3], ["ok"]),
            "string suelto": ("https://a.com", ["https://a.com"]),
        }
        for nombre, (entrada, esperado) in casos.items():
            try:
                r = self._mk(entrada).to_response("s")
                check(f"{nombre} → {esperado}", r.sources == esperado,
                      str(r.sources), SECTION)
            except Exception as exc:  # noqa: BLE001
                check(f"{nombre} no debe lanzar", False,
                      f"{type(exc).__name__}: {exc}", SECTION)

    def test_habilita_el_bonus_de_grounding_en_la_confianza(self):
        """Integración con ISSUE-063: con fuentes reales NO se topa la confianza."""
        from src.core.dispatcher_process_guard import calibrate_response_confidence
        con = self._mk(["Doc A - https://a.com", "Doc B - https://b.com"]).to_response("s")
        sin = self._mk([]).to_response("s")
        c_con = calibrate_response_confidence(con, validator_score=9.0, expects_grounding=True)
        c_sin = calibrate_response_confidence(sin, validator_score=9.0, expects_grounding=True)
        check("con fuentes: sin tope y con bonus", c_con is not None and c_con > 0.6,
              str(c_con), SECTION)
        check("sin fuentes: tope 0.6", c_sin == 0.6, str(c_sin), SECTION)
        assert c_con > 0.6 and c_sin == 0.6


class TestMcpHardening:
    """B1a/B1b (ISSUE-104): validación en las tools MCP y correlación por `id`.

    Verificado antes de tocar nada: `mcp_client`, `mcp_protocol` y `mcp_tool_bridge`
    **no tenían ningún test** (no figuran en la cobertura de API), y ahí estaban los
    dos huecos: `McpDynamicTool.arun()` sobreescribe el `arun()` base (que delega en
    `run()` → valida) y saltaba la validación; y el transporte stdio devolvía la
    primera línea no-notificación sin mirar el `id`.
    """

    # ── B1b: clasificación de líneas del server (helper puro) ────────────────
    def test_clasifica_las_lineas_del_server(self):
        import json as _json
        from src.tools_mcp.mcp.mcp_client import clasificar_linea
        casos = [
            (_json.dumps({"id": "mio", "result": 1}), "respuesta"),
            (_json.dumps({"id": "otro", "result": 1}), "ajena"),
            (_json.dumps({"method": "notif", "params": {}}), "notificacion"),
            ("no es json", "invalida"),
            (_json.dumps([1, 2, 3]), "invalida"),
            (_json.dumps({"result": 1}), "invalida"),
        ]
        fallos = [c for c, esperado in casos if clasificar_linea(c, "mio")[0] != esperado]
        check("clasifica respuesta/ajena/notificación/inválida", not fallos,
              str(fallos), SECTION)
        assert not fallos, f"mal clasificadas: {fallos}"

    def test_acepta_bytes_y_str(self):
        from src.tools_mcp.mcp.mcp_client import clasificar_linea
        assert clasificar_linea(b'{"id":"x","result":1}', "x")[0] == "respuesta"
        assert clasificar_linea('{"id":"x","result":1}', "x")[0] == "respuesta"

    def _cliente_con_lineas(self, lineas):
        """McpClient con un proceso stdio FALSO que devuelve esas líneas."""
        import asyncio
        from src.tools_mcp.mcp.mcp_client import McpClient, McpTransport
        from src.tools_mcp.mcp.mcp_protocol import JsonRpcRequest

        class _Stdin:
            def write(self, _): pass
            async def drain(self): pass

        class _Stdout:
            def __init__(self, cola): self._cola = list(cola)
            async def readline(self):
                return self._cola.pop(0) if self._cola else b""

        class _Proc:
            def __init__(self, cola):
                self.stdin = _Stdin()
                self.stdout = _Stdout(cola)

        cli = McpClient(command=["falso"], transport=McpTransport.STDIO, timeout=2.0)
        cli._process = _Proc(lineas)
        req = JsonRpcRequest(method="tools/call", params={})
        resp = asyncio.run(cli._send_stdio(req.to_json(), req.id))
        return req, resp

    def test_ignora_respuestas_ajenas_y_notificaciones(self):
        """El caso real: la respuesta tardía de un request expirado NO debe usarse."""
        lineas = [
            b'{"method":"notifications/progress","params":{}}\n',   # notificación
            b'{"id":"request-viejo","result":{"valor":"AJENO"}}\n',  # respuesta tardía
            b'{"id":"OTRO"}\n',                                      # basura con id
        ]
        # la respuesta propia va última y con el id correcto del request
        import json as _json
        from src.tools_mcp.mcp.mcp_client import McpClient, McpTransport
        from src.tools_mcp.mcp.mcp_protocol import JsonRpcRequest

        class _Stdin:
            def write(self, _): pass
            async def drain(self): pass

        req = JsonRpcRequest(method="tools/call", params={})
        cola = list(lineas) + [_json.dumps({"id": req.id, "result": {"ok": True}}).encode() + b"\n"]

        class _Stdout:
            async def readline(self): return cola.pop(0) if cola else b""

        class _Proc:
            stdin = _Stdin()
            stdout = _Stdout()

        cli = McpClient(command=["falso"], transport=McpTransport.STDIO, timeout=2.0)
        cli._process = _Proc()
        import asyncio
        resp = asyncio.run(cli._send_stdio(req.to_json(), req.id))
        check("devuelve la respuesta con el id propio, no la ajena",
              resp.id == req.id and resp.result == {"ok": True},
              f"id={resp.id} result={resp.result}", SECTION)
        assert resp.id == req.id and resp.result == {"ok": True}

    def test_sin_respuesta_propia_falla_claro(self):
        import asyncio
        import pytest as _pytest
        from src.tools_mcp.mcp.mcp_client import McpClient, McpTransport
        from src.tools_mcp.mcp.mcp_protocol import JsonRpcRequest

        class _Stdin:
            def write(self, _): pass
            async def drain(self): pass

        cola = [b'{"id":"ajeno","result":1}\n'] * 40

        class _Stdout:
            async def readline(self): return cola.pop(0) if cola else b""

        class _Proc:
            stdin = _Stdin()
            stdout = _Stdout()

        cli = McpClient(command=["falso"], transport=McpTransport.STDIO, timeout=2.0)
        cli._process = _Proc()
        req = JsonRpcRequest(method="tools/call", params={})
        with _pytest.raises(Exception) as exc:
            asyncio.run(cli._send_stdio(req.to_json(), req.id))
        check("si solo llegan respuestas ajenas, error explícito",
              "respuesta esperada" in str(exc.value), str(exc.value)[:80], SECTION)
        assert "respuesta esperada" in str(exc.value)

    # ── B1a: validación de entrada en las tools MCP ─────────────────────────
    def test_tool_mcp_valida_la_entrada(self):
        """`arun()` de una tool MCP debe validar como lo hace `run()`."""
        import asyncio
        from src.tools_mcp.mcp.mcp_tool_bridge import McpDynamicTool
        from src.tools_mcp.tool_base import ToolPermission

        class _Cliente:
            is_connected = True
            llamadas = []
            async def connect(self): return True
            async def call_tool(self, nombre, args):
                _Cliente.llamadas.append((nombre, args))
                raise AssertionError("¡no debería llamarse al server!")

        tool = McpDynamicTool(
            tool_name="sumar", tool_description="suma dos números",
            client=_Cliente(), permission=ToolPermission.NETWORK, server_label="fake",
            input_schema={"type": "object", "properties": {
                "a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"]},
        )
        # `b` es requerido y no se envía → debe fallar ANTES de llamar al server
        res = asyncio.run(tool.arun(a=1))
        check("la tool MCP rechaza input inválido sin llamar al server",
              res.success is False and _Cliente.llamadas == [],
              f"success={res.success} llamadas={_Cliente.llamadas}", SECTION)
        check("el resultado lo marca como fallo de validación",
              (res.metadata or {}).get("validation_failed") is True,
              str(res.metadata), SECTION)
        assert res.success is False and _Cliente.llamadas == []
        assert (res.metadata or {}).get("validation_failed") is True


class TestFuentesNoCitables:
    """Una red social como fuente de un dato oficial resta credibilidad.

    Medido con uso real: DuckDuckGo devolvió un post de Instagram como PRIMERA
    fuente de una consulta censal y el modelo se la citó al usuario.
    """

    def test_descarta_redes_sociales_si_hay_alternativas(self):
        from src.agents.searcher import _filtrar_fuentes_no_citables
        res = [{"url": "https://www.instagram.com/p/X"}, {"url": "https://www.cba.gov.ar/censo"},
               {"url": "https://www.indec.gob.ar/censo"}]
        out = _filtrar_fuentes_no_citables(res)
        check("se descarta la red social", len(out) == 2, str(out), SECTION)
        assert all("instagram" not in r["url"] for r in out)

    def test_conserva_todo_si_solo_hay_redes(self):
        """Mejor una fuente floja que ninguna."""
        from src.agents.searcher import _filtrar_fuentes_no_citables
        res = [{"url": "https://www.instagram.com/p/X"}, {"url": "https://www.facebook.com/p/Y"}]
        out = _filtrar_fuentes_no_citables(res)
        check("sin alternativas no se descarta nada", len(out) == 2, str(out), SECTION)
        assert len(out) == 2

    def test_sitios_oficiales_no_se_tocan(self):
        from src.agents.searcher import _filtrar_fuentes_no_citables
        res = [{"url": "https://www.cba.gov.ar/x"}, {"url": "https://es.wikipedia.org/wiki/Córdoba"}]
        check("gob.ar y wikipedia pasan", _filtrar_fuentes_no_citables(res) == res,
              "se filtró de más", SECTION)
        assert _filtrar_fuentes_no_citables(res) == res


class TestThinkFlagEnElCaminoDeAgentes:
    """`generate()` (lo que usan los AGENTES) también debe apagar el "thinking".

    ✅ 2026-09-29 — hallado con un **A/B real** (`ISSUE-099` estaba a medias): el fix
    del `think:false` se aplicó en `chat`, `chat_stream` y `generate_stream`, pero
    **no en `generate()`**, que vive en `OllamaClientBase` mientras `_think_flag`
    estaba definido en la subclase `OllamaClient`. Medido: forzando `qwen3:8b` en el
    searcher, la respuesta tardó **126 s**, gastó **300 tokens** de `num_predict` y
    devolvió **56 chars** (vacía). Con un modelo sin razonamiento: respuesta
    coherente de 800+ chars en 82 s.

    Por eso el test captura el payload REAL de `generate()` (no inspecciona el
    fuente: un test de grep sería frágil y el auditor lo prohíbe).
    """

    def _payload(self, model: str):
        import sys
        from unittest.mock import patch
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.llm.client import OllamaClient

        capturado: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self):
                return {"response": "ok", "model": model, "done": True,
                        "eval_count": 3, "prompt_eval_count": 2}

        # ✅ CI (2026-10-03): `generate()` llama a `ModelSwap.ensure()` antes del
        # pedido, y ese paso **necesita Ollama vivo** (`/api/ps`). Sin neutralizarlo,
        # estos dos tests pasaban en la máquina del desarrollador y **fallaban en
        # GitHub Actions** (`LLMError: Model swap failed ... Connection refused`) —
        # el gate quedaba en 16/18 archivos. El test mide el PAYLOAD, no la VRAM.
        with patch("src.llm.client.httpx"), \
                patch("src.core.model_swap.ModelSwap.ensure", lambda self, model: None):
            cli = OllamaClient(model=model, timeout=180,
                               base_url="http://127.0.0.1:11434")
            cli._get_client = lambda timeout=None: type("S", (), {
                "post": staticmethod(lambda url, json=None, timeout=None:
                                     (capturado.update(json), _Resp())[1])})()
            cli.generate(prompt="¿cuántos habitantes tiene Córdoba?", timeout=180,
                         max_tokens=256, task_type="WEB_SEARCH")
        return capturado

    def test_generate_apaga_el_thinking_en_modelos_con_razonamiento(self):
        payload = self._payload("qwen3:8b")
        check("generate() manda think:false con qwen3", payload.get("think") is False,
              str(payload.get("think")), SECTION)
        assert payload.get("think") is False

    def test_generate_no_manda_think_a_modelos_sin_razonamiento(self):
        payload = self._payload("qwen2.5-coder:7b")
        check("un modelo sin thinking no recibe el flag en generate()",
              "think" not in payload, str(sorted(payload)), SECTION)
        assert "think" not in payload
