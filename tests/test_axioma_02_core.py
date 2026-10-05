from __future__ import annotations
import importlib
import inspect
import tempfile
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
SECTION = "2. Núcleo (core)"
CORE_API = {
    "src.core.dependency_resolver": ["DependencyResolver", "get_dependency_resolver"],
    "src.core.task_queue": ["TaskPriority", "TaskStatus", "Task", "TaskQueue", "TaskMetrics", "TaskError"],
    "src.core.load_balancer": ["Worker", "LoadBalancer", "get_load_balancer", "reset_load_balancer"],
    "src.core.model_swap": ["ModelSwap"],
    "src.core.vram_lock": ["acquire_vram_lock", "release_vram_lock", "is_vram_locked",
                           "get_vram_lock_reason", "wait_for_vram_unlock"],
    "src.core.state_emitter": ["emit_state", "get_current_state"],
    "src.core.context_sensor": ["ContextSensor", "get_sensor"],
    "src.core.quality_monitor": ["AlertLevel", "QualityDimension", "QualityScore", "QualityAlert"],
    "src.core.workspace_manager": ["WorkspaceManager", "get_workspace_manager"],
    "src.core.self_healing": ["HealthStatus", "HealingAction", "HandlerHealth", "SelfHealingMonitor"],
    "src.core.distributed_queue": ["DistributedQueue", "get_distributed_queue", "reset_distributed_queue"],
    "src.core.task_decomposer": ["SubTask", "TaskDecomposer", "SwarmStatus", "SubTaskStatus"],
    "src.core.feedback_loop": ["FeedbackLoop", "get_feedback_loop_sync"],
    "src.core.planner": ["PlanResult", "MultiStepPlanner"],
    "src.core.promotion_gate": ["ProposalState", "Verdict", "Proposal", "PromotionGate"],
    "src.core.dispatcher": ["Dispatcher"],
    "src.core.dispatcher_cache_metrics": [],
    "src.core.dispatcher_distributed": [],
    "src.core.dispatcher_handlers": [],
    "src.core.dispatcher_operations": [],
    "src.core.dispatcher_process": [],
    "src.core.feedback_loop_components": [],
    "src.core.feedback_loop_detectors_handlers": [],
    "src.core.interpreter_loop": [],
    "src.core.jarvis_engine": [],
    "src.core.proactive_engine": [],
    "src.core.rafael_daemon": [],
    "src.core.self_healing": [],
}


class TestCoreImportsYApi:
    def test_todos_los_modulos_core(self):
        section(SECTION)
        for mod, symbols in CORE_API.items():
            _api_check(mod, symbols)


class TestTaskQueue:
    def test_prioridades_y_estados(self):
        from src.core.task_queue import TaskPriority, TaskStatus
        check("TaskPriority contiene HIGH", TaskPriority.HIGH.value > TaskPriority.LOW.value, "HIGH>LOW", SECTION)
        check("TaskStatus.COMPLETED.is_terminal", TaskStatus.COMPLETED.is_terminal is True, "OK", SECTION)
        check("TaskStatus.PENDING.is_active", TaskStatus.PENDING.is_active is True, "OK", SECTION)

    def test_task_queue_operaciones(self):
        from src.core.task_queue import TaskQueue, TaskPriority, Task
        q = TaskQueue()
        check("TaskQueue construible", q is not None, type(q).__name__, SECTION)
        t = Task(id="t1", name="tarea", payload={}, priority=TaskPriority.HIGH)
        check("Task construible", t.id == "t1", f"prioridad={t.priority}", SECTION)
        d = t.to_dict()
        check("Task.to_dict()", isinstance(d, dict) and d.get("id") == "t1", "OK", SECTION)


class TestLoadBalancer:
    def test_stats(self):
        from src.core.load_balancer import LoadBalancer
        lb = LoadBalancer()
        stats = lb.get_stats()
        needed = {"workers", "total_capacity", "total_active", "saturated", "global_load_ratio"}
        check("LoadBalancer.get_stats()", needed <= set(stats), str(sorted(stats)), SECTION)


class TestModelSwap:
    def test_status(self):
        from src.core.model_swap import ModelSwap
        ms = ModelSwap()
        status = ms.get_status()
        check("ModelSwap.get_status()", "default_model" in status and "current_model" in status,
              f"default={status.get('default_model')}", SECTION)

    def test_restore_default(self):
        """`restore_default()` habla con Ollama: sin servidor, se SALTA.

        ✅ v0.7.3 (2026-09-28): antes llamaba a la operación real y su `check()`
        fallaba en CI (`OLLAMA_HOST` apunta a un puerto muerto) → el archivo
        quedaba ❌ y la CI roja por un test unitario que dependía de que hubiera
        un servidor de modelos (R5: los tests no dependen de red/hardware).
        """
        import os as _os
        import socket
        from urllib.parse import urlparse

        host = urlparse(_os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
        try:
            with socket.create_connection(
                    (host.hostname or "127.0.0.1", host.port or 11434), timeout=1.5):
                pass
        except OSError:
            pytest.skip("Ollama no disponible: restore_default() necesita el servidor")

        from src.core.model_swap import ModelSwap
        ms = ModelSwap()
        ok = ms.restore_default()
        check("ModelSwap.restore_default()", isinstance(ok, bool), f"retorna {ok}", SECTION)
        assert isinstance(ok, bool)


class TestVramLock:
    def test_lock_ciclo(self):
        from src.core.vram_lock import acquire_vram_lock, is_vram_locked, release_vram_lock, get_vram_lock_reason
        acquire_vram_lock("test_unit")
        check("is_vram_locked() tras acquire", is_vram_locked() is True, get_vram_lock_reason(), SECTION)
        release_vram_lock()
        check("is_vram_locked() tras release", is_vram_locked() is False, "liberado", SECTION)


class TestStateEmitter:
    def test_estados_validos(self):
        from src.core.state_emitter import emit_state, get_current_state
        emit_state("IDLE")
        check("emit_state('IDLE') + get_current_state()", get_current_state() == "IDLE",
              f"estado={get_current_state()}", SECTION)
        emit_state("COMPUTING")
        check("emit_state('COMPUTING')", get_current_state() == "COMPUTING",
              f"estado={get_current_state()}", SECTION)


class TestContextSensor:
    def test_snapshot(self):
        from src.core.context_sensor import get_sensor
        snap = get_sensor().get_snapshot()
        needed = {"cpu_percent", "ram_used_pct", "time", "day_of_week"}
        check("ContextSensor.get_snapshot()", needed <= set(snap), str(sorted(snap)), SECTION)


class TestQualityMonitor:
    def test_quality_score(self):
        from src.core.quality_monitor import QualityScore, QualityDimension, AlertLevel
        qs = QualityScore(overall=9.0)
        check("QualityScore(9.0).is_acceptable", qs.is_acceptable is True, ">=6.0", SECTION)
        check("QualityScore(9.0).is_excellent", qs.is_excellent is True, ">=8.0", SECTION)
        qs2 = QualityScore(overall=3.0)
        check("QualityScore(3.0).is_poor", qs2.is_poor is True, "<5.0", SECTION)
        check("QualityDimension members", len(list(QualityDimension)) >= 5,
              str([d.value for d in QualityDimension]), SECTION)
        check("AlertLevel members", len(list(AlertLevel)) >= 3,
              str([a.value for a in AlertLevel]), SECTION)


class TestWorkspaceManager:
    def test_ciclo_workspace(self, tmp_path):
        from src.core.workspace_manager import WorkspaceManager
        base = tmp_path / "ws"
        base.mkdir(parents=True, exist_ok=True)
        wm = WorkspaceManager(base_path=str(base))
        ws = wm.get_workspace("s_ws")
        check("WorkspaceManager.get_workspace()", "s_ws" in str(ws), str(ws), SECTION)
        check("WorkspaceManager.workspace_exists()", wm.workspace_exists("s_ws") is True, "OK", SECTION)
        check("WorkspaceManager.cleanup()", wm.cleanup("s_ws") is True, "OK", SECTION)
        stats = wm.get_stats()
        check("WorkspaceManager.get_stats()", "workspaces_count" in stats, "OK", SECTION)


class TestSelfHealing:
    def test_monitor(self):
        from src.core.self_healing import SelfHealingMonitor, HealthStatus
        shm = SelfHealingMonitor()
        st = shm.check_handler("h_test", 50.0, True)
        check("SelfHealingMonitor.check_handler(ok)", st == HealthStatus.HEALTHY, str(st), SECTION)
        st2 = shm.check_handler("h_test", 5000.0, False)
        check("SelfHealingMonitor.check_handler(error)", isinstance(st2, HealthStatus), str(st2), SECTION)


class TestDistributedQueue:
    def test_instancia(self):
        from src.core.distributed_queue import get_distributed_queue
        dq = get_distributed_queue()
        check("get_distributed_queue()", dq is not None, type(dq).__name__, SECTION)


class TestTaskDecomposer:
    def test_decompose(self):
        from src.core.task_decomposer import TaskDecomposer
        from src.domain.types import TaskType
        td = TaskDecomposer()
        tasks = td.decompose("td_1", "escribe un codigo fibonacci en python y explicalo", TaskType.CODE_GENERATION)
        check("TaskDecomposer.decompose()", isinstance(tasks, list) and len(tasks) >= 1,
              f"{len(tasks)} subtareas", SECTION)
        stats = td.get_decomposition_stats()
        check("TaskDecomposer.get_decomposition_stats()", isinstance(stats, dict), "OK", SECTION)


class TestFeedbackLoop:
    def test_sync(self):
        from src.core.feedback_loop import get_feedback_loop_sync
        fl = get_feedback_loop_sync()
        check("get_feedback_loop_sync()", fl is not None, type(fl).__name__, SECTION)
        try:
            v = fl.get_current_parameter("temperature", "code_generation")
            check("FeedbackLoop.get_current_parameter()", isinstance(v, float), f"{v}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("FeedbackLoop.get_current_parameter()", False, f"{type(e).__name__}: {e}", SECTION)


class TestPlanner:
    def test_plan(self):
        from src.core.planner import MultiStepPlanner
        p = MultiStepPlanner()
        steps = p.plan("resolver una ecuacion de segundo grado", max_steps=5)
        check("MultiStepPlanner.plan()", isinstance(steps, list), f"{len(steps)} pasos", SECTION)
        check("MultiStepPlanner.get_stats()", isinstance(p.get_stats(), dict), "OK", SECTION)


class TestPromotionGate:
    def test_propuesta(self, tmp_path):
        from src.core.promotion_gate import PromotionGate
        pg = PromotionGate()
        target = tmp_path / "propuesta.txt"
        try:
            prop = pg.propose_write(str(target), "contenido de prueba")
            check("PromotionGate.propose_write()", prop is not None, f"estado={prop.state}", SECTION)
        except OSError as e:  # /tmp lleno → sombra del proyecto no copiable
            check("PromotionGate.propose_write()", False, f"entorno: {e}", SECTION)
            return
        if prop is None:
            return
        try:
            # ✅ 2026-08-25: veredictos en español (Verdict.APROBAR = "aprobar")
            res = pg.resolve(prop.proposal_id, "aprobar", "ok")
            check("PromotionGate.resolve(aprobar)", res.get("success") is True, str(res), SECTION)
        except OSError as e:  # /tmp lleno → sombra del proyecto no copiable
            check("PromotionGate.resolve(aprobar)", False,
                  f"entorno: {e} (shadow en /tmp lleno)", SECTION)


class TestDependencyResolver:
    def test_extract_missing(self):
        from src.core.dependency_resolver import get_dependency_resolver
        dr = get_dependency_resolver()
        pkg = dr.extract_missing_package("ModuleNotFoundError: No module named 'requests'")
        check("extract_missing_package()", pkg == "requests", f"paquete={pkg}", SECTION)
        check("is_installable_error()", dr.is_installable_error(
            "ModuleNotFoundError: No module named 'numpy'") is True, "OK", SECTION)
        check("is_package_allowed('numpy')", dr.is_package_allowed("numpy") is True, "OK", SECTION)
        imports = dr.extract_all_imports("import os\nfrom pathlib import Path\nimport json")
        check("extract_all_imports()", "os" in imports and "pathlib" in imports, str(imports), SECTION)


class TestHardwareFit:
    def test_evaluate_model_fit(self):
        from src.core.hardware_fit import HardwareProfile, evaluate_model_fit
        hp = HardwareProfile(gpu_vram_gb=8.0, ram_gb=32.0, cpu_threads=16,
                             gpu_name="AMD Radeon (ROCm)", backend="amd")
        r = evaluate_model_fit(model_name="qwen3:8b", size_gb=5.0, hw=hp)
        check("evaluate_model_fit()", r is not None, f"fit={getattr(r, 'fit_level', '?')}", SECTION)

    def test_pure_scoring(self):
        from src.core.hardware_fit import _fit_score, _composite
        s = _fit_score(required=6.0, budget=8.0)
        check("_fit_score(6.0, 8.0)", 0 <= s <= 100, f"score={s:.2f}", SECTION)
        c = _composite(quality=0.8, speed=0.7, fit=0.9)
        check("_composite()", 0 <= c <= 1, f"compuesto={c:.2f}", SECTION)

    def test_manager_mockeado(self):
        from src.core.hardware_fit import HardwareFitManager
        mgr = HardwareFitManager.__new__(HardwareFitManager)
        check("HardwareFitManager clase instanciable (new)", mgr is not None, "OK", SECTION)


class TestDispatcher:
    def test_init_y_estructura(self):
        from src.core.dispatcher import Dispatcher
        d = Dispatcher(agent_factory=lambda role: None)
        check("Dispatcher() construible", d is not None, "OK", SECTION)
        check("Dispatcher.classifier accesible", hasattr(d, "classifier"), "propiedad", SECTION)
        check("Dispatcher.process método", callable(getattr(d, "process", None)), "OK", SECTION)
        check("Dispatcher._routing_cache", hasattr(d, "_routing_cache"), "OK", SECTION)

    def test_handler_name(self):
        from src.core.dispatcher import Dispatcher
        d = Dispatcher(agent_factory=lambda role: None)
        name = d._handler_name("code_generation")
        check("Dispatcher._handler_name('code_generation')", isinstance(name, str) and name,
              f"handler={name}", SECTION)


class TestModulosExtendidos:
    def test_dispatcher_mixins(self):
        from src.core.dispatcher import Dispatcher
        mro = [c.__name__ for c in Dispatcher.__mro__]
        check("Dispatcher MRO incluye mixins", any("Mixin" in m for m in mro), str(mro), SECTION)
        for meth in ["process", "get_metrics", "get_stats"]:
            check(f"Dispatcher.{meth} existe", hasattr(Dispatcher, meth), "OK", SECTION)

    def test_feedback_components(self):
        m = _import("src.core.feedback_loop_components")
        if not isinstance(m, str):
            names = [n for n in dir(m) if not n.startswith("_")]
            check("feedback_loop_components importable", len(names) > 0, f"{len(names)} símbolos", SECTION)
        else:
            check("feedback_loop_components importable", False, m, SECTION)

    def test_rafael_daemon(self):
        m = _import("src.core.rafael_daemon")
        check("rafael_daemon importable", not isinstance(m, str), "" if not isinstance(m, str) else m, SECTION)
        if not isinstance(m, str):
            names = [n for n in dir(m) if not n.startswith("_")]
            check("rafael_daemon símbolos", len(names) > 0, f"{len(names)} símbolos", SECTION)


class TestF3PlanOptIn:
    def test_plan_pipeline_for_code(self):
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        m = DispatcherHandlersExtraMixin()
        pipe = m._plan_pipeline_for(
            "generá un programa completo en python que procese CSV", "code_generation")
        check("F3 code → [coder, validator]", pipe == ["coder", "validator"],
              f"pipeline={pipe}", SECTION)

    def test_plan_pipeline_for_research(self):
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        m = DispatcherHandlersExtraMixin()
        pipe = m._plan_pipeline_for(
            "investigá arquitecturas RAG y compará frameworks", "web_search")
        check("F3 research → [researcher, validator]",
              pipe == ["researcher", "validator"], f"pipeline={pipe}", SECTION)

    def test_plan_pipeline_none_para_chat(self):
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        m = DispatcherHandlersExtraMixin()
        pipe = m._plan_pipeline_for("hola", "general_chat")
        check("F3 chat puro → None (ruta normal)", pipe is None,
              f"pipeline={pipe}", SECTION)
        pipe2 = m._plan_pipeline_for(
            "escribí en una línea: 'prueba de historial netron-123'", "general_chat")
        check("F3 eco citado → None", pipe2 is None, f"pipeline={pipe2}", SECTION)

    def test_plan_pipeline_respeta_max_steps(self):
        from config.settings import settings
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        m = DispatcherHandlersExtraMixin()
        old = settings.plan_max_steps
        try:
            settings.plan_max_steps = 1
            pipe = m._plan_pipeline_for(
                "generá un programa completo en python", "code_generation")
            check("F3 max_steps=1 → solo rol principal",
                  pipe == ["coder"], f"pipeline={pipe}", SECTION)
        finally:
            settings.plan_max_steps = old

    def test_plan_gate_code_autonomous_hint(self):
        # v0.6.8xx: el gate /plan pasa task_type="code_autonomous"; el decomposer
        # NO mapea CODE_AUTONOMOUS (→ ORCHESTRATOR sin rol). El reintento
        # heurístico debe armar [coder, validator] para código y None para chat.
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
        m = DispatcherHandlersExtraMixin()
        pipe = m._plan_pipeline_for(
            "generá un programa en python que sume dos números y explicalo",
            "code_autonomous")
        check("F3 /plan code_autonomous → [coder, validator]",
              pipe == ["coder", "validator"], f"pipeline={pipe}", SECTION)
        pipe2 = m._plan_pipeline_for(
            "escribí un haiku sobre el mar", "code_autonomous")
        check("F3 /plan chat puro → None", pipe2 is None, f"pipeline={pipe2}", SECTION)

    def test_pipeline_validador_no_reemplaza_artefacto(self):
        # v0.6.8xx: /plan respondía la crítica del validador y no el código.
        # El paso "validator" debe conservar el artefacto y dejar el veredicto
        # en metadata.pipeline_validator.
        import asyncio
        from unittest.mock import patch
        from src.domain.entities import Response
        from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin

        m = DispatcherHandlersExtraMixin()

        async def fake_code(msg, tt, sid, ctx):
            return Response(content="def sumar(a, b): return a + b",
                            session_id=sid, model_used="x", task_type=tt,
                            latency_ms=1.0, confidence=0.9, validation_passed=True)

        async def fake_val(msg, tt, sid, ctx):
            return Response(content="ANÁLISIS: correcto...",
                            session_id=sid, model_used="v", task_type=tt,
                            latency_ms=1.0, confidence=0.9,
                            validation_passed=True,
                            metadata={"score": 9.0, "passed": True})

        async def _run():
            with patch.object(m, "_handle_code", fake_code), \
                 patch.object(m, "_handle_validation", fake_val), \
                 patch.object(m, "_record_metric", lambda *a, **k: None, create=True):
                resp = await m.execute_pipeline(
                    "generá un programa", ["coder", "validator"], "s1")
            return resp

        resp = asyncio.run(_run())
        check("F3 /plan conserva el artefacto (código)",
              resp.content.startswith("def sumar"), f"content={resp.content[:40]!r}", SECTION)
        pv = (resp.metadata or {}).get("pipeline_validator", {})
        check("F3 /plan veredicto en metadata",
              pv.get("score") == 9.0 and pv.get("passed") is True,
              f"pv={pv}", SECTION)


# ── Fusionado desde test_axioma_09_cobertura_fallos.py ──
import pytest


def test_finance_libra_no_falso_positivo():
    from src.router.classifier_api import APIDetector
    det = APIDetector()

    # El FP original del log: 'libra' dentro de "benchmark_calibration"
    assert det._is_finance_query("benchmark_calibration") is False
    # 'libra' dentro de otra palabra tampoco debe disparar
    assert det._is_finance_query("equilibrio de fuerzas") is False
    assert det._is_finance_query("calibración del sistema") is False

    # Consultas financieras reales SÍ deben disparar
    assert det._is_finance_query("a cuanto esta el dolar hoy") is True
    assert det._is_finance_query("cuanto vale la libra esterlina") is True
    assert det._is_finance_query("cotizacion del euro") is True


def test_extract_search_name_no_corrompe():
    from src.core.dispatcher_handlers import DispatcherHandlersMixin
    f = DispatcherHandlersMixin._extract_search_name  # método sin estado (self no usado)

    # Antes: 'en' dentro de "benchmark" → "b chmark"
    assert f(None, "buscá el archivo benchmark") == "benchmark"
    # Antes: 'rafael' se quitaba → ".py"
    assert f(None, "leé el archivo Rafael.py") == "Rafael.py"
    assert f(None, "buscá el archivo settings") == "settings"
    assert f(None, "resumí el archivo README.md") == "README.md"
    assert f(None, "dónde está la carpeta data") == "data"
    assert f(None, "citá un bloque del archivo main.py") == "main.py"


def test_extract_search_name_vacio_sin_crash():
    from src.core.dispatcher_handlers import DispatcherHandlersMixin
    f = DispatcherHandlersMixin._extract_search_name
    assert f(None, "") == ""
    assert f(None, None) == ""


def test_file_search_routing_a_file_search_agent():
    from src.core.dispatcher import Dispatcher
    from src.core.dispatcher_process import DispatcherProcessMixin

    assert "file_search" in Dispatcher.FILE_TASKS
    d = Dispatcher(enable_cache=False)
    assert d._handler_name("file_search") == "FileSearchAgent"
    # _handle_file_search viene de DispatcherHandlersMixin (combinado en Dispatcher)
    assert hasattr(Dispatcher, "_handle_file_search"), (
        "Dispatcher no expone _handle_file_search"
    )
    # El router interno _dispatch debe tener el branch FILE_TASKS (antes caía a chat)
    src_dispatch = inspect.getsource(DispatcherProcessMixin._dispatch)
    assert "FILE_TASKS" in src_dispatch and "file_search" in src_dispatch, (
        "el _dispatch no enruta file_search por FILE_TASKS"
    )


def test_fs_search_registrada_en_registry():
    from src.tools_mcp.tool_registry import ToolRegistry
    reg = ToolRegistry()
    assert reg.has("fs_search"), "FsSearchTool no está registrada"
    tool = reg.get("fs_search")
    assert tool.name == "fs_search"


def test_fs_search_busca_archivo_y_salta_dirs_pesados(tmp_path):
    from src.tools_mcp.tool_registry import ToolRegistry
    reg = ToolRegistry()
    tool = reg.get("fs_search")

    (tmp_path / "target.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "otro.txt").write_text("y", encoding="utf-8")
    # "logs" está en _SKIP_DIRS — no debe aparecer en resultados
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "secreto.txt").write_text("z", encoding="utf-8")

    res = tool.run(name="target.txt", base_dir=str(tmp_path))
    assert res.success, res.error
    assert res.data["count"] == 1
    assert str(tmp_path / "target.txt") in res.data["matches"][0]["path"]

    res2 = tool.run(name="secreto.txt", base_dir=str(tmp_path))
    assert res2.success
    assert res2.data["count"] == 0, "no debe buscar dentro de _SKIP_DIRS (logs)"

    res3 = tool.run(name="otro.txt", base_dir=str(tmp_path))
    assert res3.data["count"] == 1
    assert str(tmp_path / "sub" / "otro.txt") in res3.data["matches"][0]["path"]


def test_memoria_primer_mensaje_llega_a_short_term(tmp_path):
    """Gateway FRESCO: add_message debe persistir en short_term aunque la
    memoria esté vacía (antes `if self._short_term:` era False por
    __bool__ = len > 0 → la capa se saltaba silenciosamente)."""
    import config.paths as cp
    mp = pytest.MonkeyPatch()
    mp.setattr(cp.Paths, "MEMORY", tmp_path)
    from src.memory.gateway import MemoryGateway
    gw = MemoryGateway(
        session_id="cob_fallos_1",
        enable_semantic=False, enable_optimizer=False, enable_kg=False,
    )
    try:
        r = gw.add_message("user", "hola", metadata={}, source_type="chat")
        assert r is True, "add_message debe tener éxito en gateway fresco"
        recent = gw._short_term.get_recent(5)
        assert len(recent) == 1, (
            f"el 1er mensaje no llegó a short_term (truthiness): {recent}"
        )
        assert recent[0]["content"] == "hola"
    finally:
        gw.close()
        mp.undo()


def test_coder_retries_conservan_identidad():
    """La recursión de retry (calidad/blacklist/CWE) no debe perder la
    identidad en intentos 2..N (bug detectado por test_axioma_08)."""
    from unittest.mock import patch
    from src.agents.coder import CoderAgent
    from src.domain.entities import Message, MessageRole

    with patch("src.agents.base.OllamaClient"):
        agent = CoderAgent(model="m", timeout=1, enable_memory=False)

    msg = Message(role=MessageRole.USER, content="crea una funcion en python")
    llamadas: list = []
    agent._build_prompt = lambda *a, **k: (llamadas.append(k) or "PROMPT")
    # Contenido sin bloques markdown → calidad baja → dispara retries
    agent._generate = lambda **k: "codigo sin docstrings ni bloques"

    result = agent.execute(
        msg, task_type="code_generation", session_id="s1", user_id="u1",
    )
    assert len(llamadas) >= 2, f"esperaba retries, hubo {len(llamadas)} llamadas a _build_prompt"
    for i, c in enumerate(llamadas):
        assert c.get("session_id") == "s1", f"intento {i + 1} perdió session_id: {c}"
        assert c.get("user_id") == "u1", f"intento {i + 1} perdió user_id: {c}"


class TestDegradacionNoSilenciosa:
    def _logger(self, caplog):
        import logging
        return logging.getLogger("axioma.test.degradation")

    def test_error_de_contrato_va_a_warning(self, caplog):
        import logging
        from src.utils.degradation import log_degraded, is_contract_error
        log = self._logger(caplog)
        exc = AttributeError("'float' object has no attribute 'overall'")
        assert is_contract_error(exc) is True
        with caplog.at_level(logging.DEBUG, logger="axioma.test.degradation"):
            contract = log_degraded(log, exc, "QualityMonitor.record_score")
        assert contract is True
        assert any(r.levelno == logging.WARNING for r in caplog.records), \
            f"un error de contrato debe loguearse a WARNING: {caplog.records}"
        assert any("degradado/contrato" in r.getMessage() for r in caplog.records)

    def test_error_esperado_queda_en_debug(self, caplog):
        import logging
        from src.utils.degradation import log_degraded, is_contract_error
        log = self._logger(caplog)
        exc = TimeoutError("connection timed out")
        assert is_contract_error(exc) is False
        with caplog.at_level(logging.DEBUG, logger="axioma.test.degradation"):
            contract = log_degraded(log, exc, "ExternalAPI.search")
        assert contract is False
        assert not any(r.levelno >= logging.WARNING for r in caplog.records), \
            "una degradación esperada (timeout) no debe generar WARNING"

    @pytest.mark.parametrize("exc", [
        TypeError("bad args"), AttributeError("x"), NameError("y"),
        ImportError("z"), NotImplementedError("w"),
    ])
    def test_clasificacion_de_contratos(self, exc):
        from src.utils.degradation import is_contract_error
        assert is_contract_error(exc) is True

    def test_qm_record_registra_el_score(self):
        """ISSUE-018: el score float del validador debe llegar al monitor."""
        import asyncio
        from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin
        from src.core.quality_monitor import get_quality_monitor

        class _D(DispatcherProcessGuardMixin):
            pass

        async def go():
            await _D()._qm_record("code_generation", "validator", 7.5)
            m = await get_quality_monitor()
            return m

        m = asyncio.run(go())
        assert any(s.get("score") == 7.5 for s in m._scores), \
            "el score del validador no quedó registrado en QualityMonitor"


class TestImplicitSignalRouted:
    def test_routed_existe_en_el_enum(self):
        from src.core.feedback_loop import ImplicitSignal
        assert hasattr(ImplicitSignal, "ROUTED"), \
            "ImplicitSignal.ROUTED desapareció (los call sites volverían a fallar)"
        assert ImplicitSignal.ROUTED.value == "routed"

    def test_call_sites_pasan_agent_role(self):
        """Ambos emisores deben pasar agent_role (requerido por record_implicit)."""
        from src.core import dispatcher_process as DP
        from src.router import smart_router as SR

        for mod, nombre in ((DP, "dispatcher_process"), (SR, "smart_router")):
            src = inspect.getsource(mod)
            usa_routed = "ImplicitSignal.ROUTED" in src
            pasa_rol = "agent_role=" in src
            assert usa_routed, f"{nombre}: ya no emite la señal ROUTED"
            assert pasa_rol, (
                f"{nombre}: emite ROUTED pero no pasa agent_role → "
                f"TypeError silenciado (bug ISSUE-033)")

    def test_record_implicit_crea_el_registro(self, tmp_path):
        """Llamada real con la firma completa → el FeedbackRecord se crea.

        ⚠️ ISSUE-059: `FeedbackLoop()` SIN `persist_path` cae al path por defecto
        (`data/feedback/<hash>.jsonl`) → **cada corrida de la suite dejaba un
        archivo en los datos del usuario** (se acumularon 37). Con `tmp_path` el
        test queda hermético.
        """
        import asyncio
        from src.core.feedback_loop import FeedbackLoop, ImplicitSignal

        async def go():
            fl = FeedbackLoop(persist_path=tmp_path / "feedback.jsonl",
                              # ✅ ISSUE-067: telemetría EWMA a DB temporal
                              db_path=str(tmp_path / "axioma.db"))
            return await fl.record_implicit(
                session_id="s_regresion", task_type="general_chat",
                agent_role="LLM_Chat", query="hola", response="ok",
                signal=ImplicitSignal.ROUTED,
            )

        rec = asyncio.run(go())
        assert rec is not None, "record_implicit devolvió None con la firma completa"
        assert rec.signal_or_rating == ImplicitSignal.ROUTED
        assert rec.agent_role == "LLM_Chat"

    def test_firma_de_record_implicit_exige_agent_role(self):
        """Si `agent_role` dejara de ser requerido, este test avisa."""
        from src.core.feedback_loop import FeedbackLoop
        params = inspect.signature(FeedbackLoop.record_implicit).parameters
        assert "agent_role" in params, "record_implicit perdió el parámetro agent_role"


class TestStressConcurrenciaAsync:
    """C5: stress de concurrencia — la clase de bug que los unit tests no ven.

    `TaskQueue` y `vram_lock` se usan desde varios hilos/tareas (workers, warmup,
    RAC) y sólo se cubría el camino feliz de un hilo. Todo se acota con
    `asyncio.wait_for`/deadlines: un deadlock **falla** en segundos en vez de
    colgar la suite. Encontró `ISSUE-100` y `ISSUE-101` (ver docs/ISSUES_HISTORY).
    """

    def test_productores_concurrentes_sin_perdidas(self):
        """5 productores × 10 tareas: ninguna se pierde ni se duplica."""
        import asyncio
        from src.core.task_queue import TaskPriority, TaskQueue

        async def escenario():
            q = TaskQueue()

            async def productor(k):
                for i in range(10):
                    await q.enqueue(f"t{k}-{i}", {"k": k, "i": i}, TaskPriority.NORMAL)

            await asyncio.gather(*(productor(k) for k in range(5)))
            ids = {t.id for t in q.list_pending()}
            vistos, orden = set(), []
            while True:
                t = await q.dequeue(timeout=0.2)
                if t is None:
                    break
                vistos.add(t.id)
                orden.append(t.id)
            return ids, vistos, orden

        ids, vistos, orden = asyncio.run(asyncio.wait_for(escenario(), timeout=20))
        check("los 50 encolados están pendientes", len(ids) == 50, str(len(ids)), SECTION)
        check("se desencolaron los 50, sin repetidos",
              vistos == ids and len(orden) == 50, f"{len(vistos)}/{len(ids)}", SECTION)
        assert vistos == ids and len(orden) == 50

    def test_prioridad_se_respeta_bajo_concurrencia(self):
        """Con encolado concurrente, los HIGH salen antes que los LOW."""
        import asyncio
        from src.core.task_queue import TaskPriority, TaskQueue

        async def escenario():
            q = TaskQueue()

            async def productor(p, n):
                for i in range(n):
                    await q.enqueue(f"{p.name}-{i}", {}, p)

            await asyncio.gather(
                productor(TaskPriority.LOW, 10),
                productor(TaskPriority.NORMAL, 10),
                productor(TaskPriority.HIGH, 10),
            )
            salida = []
            while True:
                t = await q.dequeue(timeout=0.2)
                if t is None:
                    break
                salida.append(t.priority)
            return salida

        salida = asyncio.run(asyncio.wait_for(escenario(), timeout=20))
        pos = {p: [i for i, x in enumerate(salida) if x == p] for p in
               (TaskPriority.HIGH, TaskPriority.NORMAL, TaskPriority.LOW)}
        check("todo HIGH antes que todo LOW",
              max(pos[TaskPriority.HIGH]) < min(pos[TaskPriority.LOW]),
              f"HIGH hasta {max(pos[TaskPriority.HIGH])}, LOW desde "
              f"{min(pos[TaskPriority.LOW])}", SECTION)
        check("30 desencoladas y NORMAL antes que LOW",
              len(salida) == 30
              and max(pos[TaskPriority.NORMAL]) < min(pos[TaskPriority.LOW]),
              f"{len(salida)} tareas · NORMAL hasta "
              f"{max(pos[TaskPriority.NORMAL])}", SECTION)
        assert len(salida) == 30
        assert max(pos[TaskPriority.HIGH]) < min(pos[TaskPriority.LOW])

    def test_dequeue_vacio_devuelve_none_sin_colgar(self):
        import asyncio
        import time
        from src.core.task_queue import TaskQueue

        async def escenario():
            q = TaskQueue()
            t0 = time.perf_counter()
            tarea = await q.dequeue(timeout=0.2)
            return tarea, time.perf_counter() - t0

        tarea, transcurrido = asyncio.run(asyncio.wait_for(escenario(), timeout=10))
        check("dequeue vacío → None", tarea is None, str(tarea), SECTION)
        check("y respeta el timeout (no cuelga)", transcurrido < 5.0,
              f"{transcurrido:.2f}s", SECTION)
        assert tarea is None and transcurrido < 5.0

    def test_worker_loop_procesa_todo_y_no_cuelga(self):
        """El camino real (worker + shutdown) con 20 tareas."""
        import asyncio
        from src.core.task_queue import TaskQueue

        async def escenario():
            q = TaskQueue()
            hechas = []
            listo = asyncio.Event()

            async def handler(task):
                hechas.append(task.id)
                if len(hechas) >= 20:
                    listo.set()
                return {"ok": True}

            for i in range(20):
                await q.enqueue(f"job-{i}", {"i": i})
            worker = asyncio.create_task(q.worker_loop(handler, poll_interval=0.05))
            await asyncio.wait_for(listo.wait(), timeout=15)
            await asyncio.wait_for(q.shutdown(wait_for_pending=True, timeout=5), timeout=15)
            worker.cancel()
            return hechas

        hechas = asyncio.run(asyncio.wait_for(escenario(), timeout=40))
        check("el worker procesó las 20 tareas, sin duplicados",
              len(hechas) == 20 and len(set(hechas)) == 20,
              f"{len(hechas)}/{len(set(hechas))}", SECTION)
        assert len(hechas) == 20 and len(set(hechas)) == 20

    def test_shutdown_con_pendientes_no_cuelga(self):
        """Sin worker, `shutdown` debe respetar su timeout (no esperar para siempre)."""
        import asyncio
        import time
        from src.core.task_queue import TaskQueue

        async def escenario():
            q = TaskQueue()
            for i in range(5):
                await q.enqueue(f"pend-{i}", {})
            t0 = time.perf_counter()
            await q.shutdown(wait_for_pending=True, timeout=1.0)
            return time.perf_counter() - t0

        transcurrido = asyncio.run(asyncio.wait_for(escenario(), timeout=20))
        check("shutdown respeta el timeout (~1 s, no 30)", transcurrido < 6.0,
              f"{transcurrido:.2f}s", SECTION)
        assert transcurrido < 6.0

    def test_dequeue_salta_las_entradas_canceladas(self):
        """ISSUE-100: cancelar no puede hacer que `dequeue` devuelva None.

        Se cancelan 3 de prioridad HIGH (van primero) y se verifica que la viva
        (LOW) se sirva igual, sin gastar el timeout en las entradas muertas.
        """
        import asyncio
        import time
        from src.core.task_queue import TaskPriority, TaskQueue

        async def escenario():
            q = TaskQueue()
            muertas = [await q.enqueue(f"muerta-{i}", {}, TaskPriority.HIGH)
                       for i in range(3)]
            viva = await q.enqueue("viva", {}, TaskPriority.LOW)
            for m in muertas:
                await q.cancel(m.id, reason="test")
            t0 = time.perf_counter()
            primera = await q.dequeue(timeout=2.0)
            segunda = await q.dequeue(timeout=0.3)
            return primera, segunda, time.perf_counter() - t0, viva.id

        primera, segunda, transcurrido, viva_id = asyncio.run(
            asyncio.wait_for(escenario(), timeout=20))
        check("la tarea viva se sirve detrás de 3 canceladas",
              primera is not None and primera.id == viva_id,
              f"{getattr(primera, 'name', None)}", SECTION)
        check("y sin gastar el timeout en las muertas", transcurrido < 1.5,
              f"{transcurrido:.3f}s", SECTION)
        check("cola ya vacía → None (contrato intacto)", segunda is None,
              str(segunda), SECTION)
        assert primera is not None and primera.id == viva_id
        assert transcurrido < 1.5 and segunda is None

    def test_vram_lock_despierta_a_todos_los_que_esperan(self):
        """ISSUE-101: liberar el candado despierta a TODOS los que esperan."""
        import threading
        import time
        from src.core.vram_lock import (acquire_vram_lock, is_vram_locked,
                                        release_vram_lock, wait_for_vram_unlock)

        resultados = {}
        acquire_vram_lock("test_stress")
        try:
            def esperar(k):
                t0 = time.perf_counter()
                resultados[k] = (wait_for_vram_unlock(timeout=10.0),
                                 time.perf_counter() - t0)
            hilos = [threading.Thread(target=esperar, args=(i,)) for i in range(3)]
            for h in hilos:
                h.start()
            time.sleep(0.2)
            release_vram_lock()
            for h in hilos:
                h.join(timeout=8)
        finally:
            release_vram_lock()
        check("los 3 esperadores despertaron con True",
              all(resultados.get(i, (False,))[0] is True for i in range(3)),
              str({k: v[0] for k, v in resultados.items()}), SECTION)
        check("despertaron rápido (<2 s)",
              all(v[1] < 2.0 for v in resultados.values()),
              str({k: round(v[1], 2) for k, v in resultados.items()}), SECTION)
        check("el candado queda libre para el resto de la suite",
              is_vram_locked() is False, "libre", SECTION)
        assert all(resultados.get(i, (False,))[0] is True for i in range(3))
        assert all(v[1] < 2.0 for v in resultados.values())
        assert is_vram_locked() is False

    def test_vram_lock_respeta_el_timeout(self):
        """Con el candado tomado, `wait_for_vram_unlock` debe esperar y dar False."""
        import time
        from src.core.vram_lock import acquire_vram_lock, release_vram_lock, wait_for_vram_unlock

        acquire_vram_lock("test_timeout")
        try:
            t0 = time.perf_counter()
            libre = wait_for_vram_unlock(timeout=0.3)
            transcurrido = time.perf_counter() - t0
        finally:
            release_vram_lock()
        check("con el candado tomado devuelve False (ISSUE-101)", libre is False,
              f"{libre} en {transcurrido:.3f}s", SECTION)
        check("y no espera más que el timeout", transcurrido < 5.0,
              f"{transcurrido:.2f}s", SECTION)
        assert libre is False and transcurrido < 5.0

    def test_model_swap_espera_al_candado_de_rac(self, monkeypatch):
        """ISSUE-101, efecto REAL: el swap espera y rehúsa al expirar (antes seguía)."""
        import time
        from config.settings import settings
        from src.core.model_swap import ModelSwap
        from src.core.vram_lock import (acquire_vram_lock, is_vram_locked,
                                        release_vram_lock)

        monkeypatch.setattr(settings, "vram_lock_wait_timeout", 1.0, raising=False)
        swap = ModelSwap.instance()
        monkeypatch.setattr(swap, "_swap_timeout", 0.5)
        acquire_vram_lock("test_stress")
        try:
            t0 = time.perf_counter()
            with pytest.raises(RuntimeError):
                swap._check_vram_lock()
            transcurrido = time.perf_counter() - t0
        finally:
            release_vram_lock()
        check("ModelSwap ESPERA el candado (no sigue de largo)",
              transcurrido >= 0.9, f"{transcurrido:.2f}s", SECTION)
        check("y rehúsa el swap al expirar el timeout", True, "RuntimeError", SECTION)
        check("el candado queda libre", is_vram_locked() is False, "libre", SECTION)
        assert transcurrido >= 0.9 and is_vram_locked() is False


def test_contratos_del_chat_detener_y_avanzado():
    """`ISSUE-134/135`: contratos del chat sin NiceGUI — DETENER y "Avanzado"."""
    from src.interfaces.components.chat import ChatComponent
    comp = ChatComponent.__new__(ChatComponent)
    comp._on_stop_cb = None
    comp._on_stop_click()                 # sin callback: no debe lanzar
    llamadas = []
    comp.on_stop(lambda: llamadas.append("corte"))
    comp._on_stop_click()
    assert llamadas == ["corte"], llamadas
    comp.on_stop(lambda: 1 / 0)           # callback roto: no propaga
    comp._on_stop_click()
    comp.elements = {}
    comp.set_advanced(True)               # sin grupo: tampoco debe lanzar
    visto = []
    comp.elements = {"advanced_group": type("G", (), {
        "set_visibility": lambda self, v: visto.append(v)})()}
    comp.set_advanced(True)
    comp.set_advanced(False)
    assert visto == [True, False], visto

