from __future__ import annotations
import threading
import time
from pathlib import Path
import pytest
try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECTION = "8. Coordinación FASE 4: MemoryGuard + AgentCoordinator + TaskBoard"


class FakeSwap:
    """Swap simulado — registra ensure() sin tocar Ollama."""
    def __init__(self):
        self.current_model = None
        self.log = []

    def ensure(self, model):
        self.log.append(f"ensure:{model}")
        self.current_model = model
        return True


class FakeGuard:
    """MemoryGuard determinista para tests de COORDINACIÓN.

    ✅ v0.6.9v: los tests de AgentCoordinator/TaskBoard verifican la LÓGICA de
    coordinación (swap, drenado, dependencias), no la política de RAM. Usaban
    `MemoryGuard()` real, que lee la RAM del equipo en vivo → en esta máquina
    (14 GB, RAM ajustada) el guard rechazaba `qwen3:8b` con `refused_ram` y los
    checks fallaban de forma ambiental/fluctuante (10 falsos fallos).
    La política de RAM tiene sus PROPIOS tests (TestMemoryGuard con el guard
    real), así que acá se inyecta este fake permisivo y los tests quedan
    deterministas.

    Interfaz espejo de MemoryGuard: required_gb / can_load / can_coexist.
    """

    def __init__(self, allow: bool = True):
        self.allow = allow

    def required_gb(self, model_name: str) -> float:
        return 4.0

    def can_load(self, model_name: str):
        if self.allow:
            return True, "ok", {"model": model_name, "required_gb": 4.0}
        return False, "refused_ram", {"model": model_name, "refused": "fake"}

    def can_coexist(self, model_name: str, already_loaded: dict):
        if self.allow:
            return True, "ok", {"model": model_name}
        return False, "refused_ram", {"model": model_name, "refused": "fake"}


class TestMemoryGuard:
    def test_required_gb_estimacion(self):
        section(SECTION)
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard()
        check("qwen3:8b estima tamaño", g.required_gb("qwen3:8b") >= 3.0,
              f"{g.required_gb('qwen3:8b'):.1f} GB", SECTION)
        check("13b estima mayor que 8b",
              g.required_gb("foo:13b") > g.required_gb("qwen3:8b"),
              f"{g.required_gb('foo:13b'):.1f} vs {g.required_gb('qwen3:8b'):.1f}", SECTION)
        # ✅ FIX 2026-08-29: el default se escala con `memory_guard_overhead_ratio`
        # (required = DEFAULT_MODEL_SIZE_GB * ratio); `== DEFAULT` quedó obsoleto al
        # calibrar el ratio (1.17 → 1.10) y fallaba en cada corrida (4.68 vs 4.4).
        check("sin tag usa default escalado",
              g.required_gb("modelo-raro") == g.DEFAULT_MODEL_SIZE_GB * g._overhead_ratio,
              str(g.required_gb("modelo-raro")), SECTION)

    def test_ram_refused_fail_safe(self, monkeypatch):
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard(warn_ratio=0.85, hard_ratio=0.95, headroom_gb=1.0)
        total = g.ram_total_gb()
        # RAM casi llena → cargar otro modelo debe ser REFUSED
        monkeypatch.setattr(g, "ram_used_gb", lambda: total - 0.5)
        ok, reason, details = g.can_load("qwen3:8b")
        check("RAM llena → refused_ram", ok is False and reason == "refused_ram",
              f"{reason} {details}", SECTION)

    def test_ram_normal_ok(self, monkeypatch):
        """RAM normal (fijada por el test) → cargar el modelo se permite.

        ✅ v0.7.4 (`ISSUE-093`): el check medía la RAM **real** del equipo, porque
        `can_load()` consulta `ram_total_gb()`/`ram_used_gb()`. Con 7.8 de 14.7 GB
        usados, `qwen3:8b` (6.0 GB) daba 14.82 GB > límite duro 14.0 GB ⇒
        `refused_ram` y el check fallaba **por el estado de la máquina, no por el
        código** (medido: mismo archivo y revisión, ✅ a las 17:09 y ❌ a las 17:26;
        en el runner de CI, con la RAM ya ocupada, fallaba siempre ⇒ el "17/18" de
        la CI). Es la trampa 28 (un test no puede depender del entorno) y R5.
        Igual que su hermano `test_ram_refused_fail_safe`, se fija la RAM a un
        valor normal para medir la **lógica** del guard.
        """
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard(warn_ratio=0.85, hard_ratio=0.95, headroom_gb=1.0)
        monkeypatch.setattr(g, "ram_total_gb", lambda: 16.0)
        monkeypatch.setattr(g, "ram_used_gb", lambda: 2.0)
        ok, reason, details = g.can_load("qwen3:8b")
        check("RAM normal → ok", ok is True and reason in ("ok", "warn_ram", "no_fit_warn"),
              f"{reason} {details}", SECTION)

    def test_reserve_release(self):
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard()
        check("reserve doble", g.reserve("m1") is True and g.reserve("m1") is False,
              str(g.reserved_models()), SECTION)
        g.release("m1")
        check("release libera", "m1" not in g.reserved_models(),
              str(g.reserved_models()), SECTION)

    def test_can_coexist(self, monkeypatch):
        """Coexistencia: con RAM holgada caben dos modelos; con RAM llena no.

        ✅ v0.7.4 (`ISSUE-094`): la RAM TOTAL también se fija (16 GB). Antes se
        leía del equipo (`total = g.ram_total_gb()`) y el check "coexist con RAM
        libre" exige `0,2·T + 4 + required + 0,5 ≤ 0,95·T`, o sea **T ≥ 13,7 GB**:
        pasaba en este equipo (14,74 GB, con solo 0,5 GB de margen) y fallaba en
        un runner más chico —medido: con T=13 GB falla exactamente este check, y
        es el `1` check ❌ que reportaba la CI—. Igual que `test_ram_normal_ok`:
        el test verifica la POLÍTICA, no la máquina.
        """
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard(warn_ratio=0.85, hard_ratio=0.95, headroom_gb=0.5)
        total = 16.0
        monkeypatch.setattr(g, "ram_total_gb", lambda: total)
        # RAM con espacio → coexisten
        monkeypatch.setattr(g, "ram_used_gb", lambda: total * 0.2)
        ok, _, _ = g.can_coexist("qwen3:8b", {"qwen2.5-coder:7b": 4.0})
        check("coexist con RAM libre", ok is True, "ok", SECTION)
        # RAM llena → NO coexisten (futuro paralelo restringido)
        monkeypatch.setattr(g, "ram_used_gb", lambda: total - 0.5)
        ok2, reason, _ = g.can_coexist("qwen3:8b", {"qwen2.5-coder:7b": 4.0})
        check("coexist con RAM llena → refused", ok2 is False and reason == "refused_ram",
              reason, SECTION)


class TestTaskBoard:
    def test_ciclo_estados(self):
        from src.core.task_board import TaskBoard, TaskStatus
        board = TaskBoard()
        a = board.add("paso A", "qwen3:8b")
        b = board.add("paso B", "qwen2.5-coder:7b", depends_on=[a])
        check("ready_tasks solo A", [t.name for t in board.ready_tasks()] == ["paso A"],
              str([t.name for t in board.ready_tasks()]), SECTION)
        board.store_result(a, "resultado A")
        check("waiting_on resuelto", board.waiting_on(b) is None, str(board.waiting_on(b)), SECTION)
        check("B ahora ready", [t.name for t in board.ready_tasks()] == ["paso B"],
              str([t.name for t in board.ready_tasks()]), SECTION)
        board.update(b, status=TaskStatus.REFUSED_OOM, error="ram")
        stats = board.get_stats()
        check("refused_oom registrado",
              stats["by_status"].get("refused_oom") == 1,
              str(stats["by_status"]), SECTION)

    def test_waiting_on_dependencia(self):
        from src.core.task_board import TaskBoard
        board = TaskBoard()
        a = board.add("A", "m1")
        b = board.add("B", "m2", depends_on=[a])
        check("waiting_on devuelve dep", board.waiting_on(b) == a, str(board.waiting_on(b)), SECTION)


class TestCoordinatorSwap:
    def test_run_on_model_basico(self):
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=FakeGuard(), board=None, allow_parallel=False)
        out = coord.run_on_model("qwen3:8b", lambda m, x: f"{m} {x}", caller="t", x=1)
        check("run_on_model ejecuta", out["success"] is True and out["result"] == "qwen3:8b 1",
              str(out), SECTION)
        check("swap registrado", fs.log == ["ensure:qwen3:8b"], str(fs.log), SECTION)
        check("busy liberado", coord.get_status()["busy"] == {}, str(coord.get_status()["busy"]), SECTION)

    def test_drenado_espera_modelo_ocupado(self):
        """El modelo cargado SIEMPRE termina antes del swap (drenado).

        ✅ v0.6.8v: determinista — antes usaba time.sleep(0.05) fijo para
        asumir que tA ya arrancó; bajo carga de la suite completa el hilo
        no alcanzaba a correr en 50ms y el test fallaba intermitente
        (ValueError de .index() o orden invertido). Ahora espera el marcador
        real de arranque y usa lookup seguro.
        """
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=FakeGuard(), board=None, allow_parallel=False)
        timeline = []

        def trabajo_largo(m, delay):
            timeline.append(f"inicio:{m}")
            time.sleep(delay)
            timeline.append(f"fin:{m}")
            return "ok"

        tA = threading.Thread(
            target=lambda: coord.run_on_model("qwen3:8b", trabajo_largo, caller="A", delay=0.3))
        tA.start()
        # Esperar a que tA REALMENTE arrancó (no sleep fijo).
        deadline = time.time() + 5.0
        while "inicio:qwen3:8b" not in timeline and time.time() < deadline:
            time.sleep(0.01)
        tB = threading.Thread(
            target=lambda: coord.run_on_model("qwen2.5-coder:7b", lambda m: "B", caller="B"))
        tB.start()
        tA.join()
        tB.join()
        orden = timeline + fs.log

        def _idx(marker: str) -> int:
            return orden.index(marker) if marker in orden else len(orden) + 1

        check("drenado: fin qwen3 antes de ensure coder",
              _idx("fin:qwen3:8b") < _idx("ensure:qwen2.5-coder:7b"),
              str(orden), SECTION)

    def test_oom_rechaza_sin_ejecutar(self):
        from src.core.agent_coordinator import AgentCoordinator
        class GuardOOM:
            def required_gb(self, m):
                return 4.0
            def can_load(self, m):
                return (False, "refused_ram", {"ram_after_gb": 99})
            def can_coexist(self, m, loaded):
                return (False, "refused_ram", {})
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=GuardOOM(), board=None, allow_parallel=False)
        ejecutado = []
        out = coord.run_on_model("qwen3:8b", lambda m: ejecutado.append(m), caller="oom")
        check("OOM → no ejecuta", out["success"] is False and out["reason"] == "refused_ram",
              str(out), SECTION)
        check("OOM → sin swap", fs.log == [], str(fs.log), SECTION)
        check("OOM → fn nunca corre", ejecutado == [], str(ejecutado), SECTION)

    def test_taskboard_refused_oom(self):
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.task_board import TaskBoard, TaskStatus
        class GuardOOM:
            def required_gb(self, m):
                return 4.0
            def can_load(self, m):
                return (False, "refused_ram", {})
            def can_coexist(self, m, loaded):
                return (False, "refused_ram", {})
        board = TaskBoard()
        coord = AgentCoordinator(swap=FakeSwap(), guard=GuardOOM(), board=board,
                                 allow_parallel=False)
        out = coord.run_on_model("qwen3:8b", lambda m: 1, caller="oom_task")
        tasks = board.list_by_status(TaskStatus.REFUSED_OOM)
        check("tarea marcada refused_oom en tablero", len(tasks) == 1,
              str([t.name for t in tasks]), SECTION)


class TestCrossModelCheckpoint:
    def test_wait_for_result_cruzado(self):
        """Dependencia cruzada: checkpoint → productor → resume sin perder hilo.

        ✅ v0.6.8v: lookups defensivos — un fallo se registra en el reporte
        en vez de explotar con KeyError/IndexError (que hacía flakear el
        test bajo carga de la suite completa).
        """
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=FakeGuard(), board=None, allow_parallel=False)

        def productor(m):
            return {"dato": "producido por " + m}

        def continuacion(state, result):
            return f"consumidor {state['nombre']} recibió {result['dato']}"

        out = coord.wait_for_result(
            producer=productor, producer_model="qwen2.5-coder:7b",
            consumer_state={"nombre": "A"},
            consumer_model="qwen3:8b",
            continuation_fn=continuacion, caller="cruzado",
        )
        check("wait_for_result exitoso", out["success"] is True, str(out), SECTION)
        check("resultado inyectado",
              out.get("result") == "consumidor A recibió producido por qwen2.5-coder:7b",
              str(out.get("result")), SECTION)
        check("swap ida y vuelta",
              fs.log == ["ensure:qwen2.5-coder:7b", "ensure:qwen3:8b"],
              str(fs.log), SECTION)
        conts = coord.get_status()["continuations"]
        primer_estado = next(iter(conts.values()), {}).get("status")
        check("continuación resuelta",
              len(conts) == 1 and primer_estado == "resumed",
              str(conts), SECTION)

    def test_continuacion_persistida_no_se_pierde(self):
        """El checkpoint se guarda ANTES del swap del productor (no se pierde el hilo)."""
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=FakeGuard(), board=None, allow_parallel=False)

        def productor(m):
            return "r"

        def continuacion(state, result):
            return "ok"

        cid = coord.checkpoint(
            model_name="qwen3:8b", state={"x": 1}, waiting_on="prod",
            continuation_fn=continuacion,
        )
        check("checkpoint devuelve id", bool(cid), cid, SECTION)
        conts = coord.get_status()["continuations"]
        check("checkpoint registrado (waiting)",
              conts[cid]["status"] == "waiting", str(conts), SECTION)
        out = coord.resume_continuation(cid, "resultado")
        check("resume con resultado", out["success"] is True, str(out), SECTION)


class TestRunChain:
    def test_chain_dependencias(self):
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=FakeGuard(), board=None, allow_parallel=False)
        steps = [
            {"name": "buscar", "model": "qwen3:8b",
             "fn": lambda m, deps: {"docs": "documentos"}},
            {"name": "analizar", "model": "qwen2.5-coder:7b",
             "fn": lambda m, deps: f"código de {deps['buscar']['docs']}",
             "depends_on": ["buscar"]},
            {"name": "resumir", "model": "qwen3:8b",
             "fn": lambda m, deps: f"resumen de {deps['analizar']}",
             "depends_on": ["analizar"]},
        ]
        out = coord.run_chain(steps)
        check("chain exitosa", out["success"] is True, str(out), SECTION)
        check("resultado final correcto",
              out["results"]["resumir"] == "resumen de código de documentos",
              str(out["results"]), SECTION)
        check("swaps ordenados",
              fs.log == ["ensure:qwen3:8b", "ensure:qwen2.5-coder:7b"],
              str(fs.log), SECTION)

    def test_chain_circular_bloqueada(self):
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        coord = AgentCoordinator(swap=FakeSwap(), guard=FakeGuard(), board=None,
                                 allow_parallel=False)
        steps = [
            {"name": "a", "model": "m1", "fn": lambda m, deps: 1, "depends_on": ["b"]},
            {"name": "b", "model": "m2", "fn": lambda m, deps: 2, "depends_on": ["a"]},
        ]
        out = coord.run_chain(steps)
        check("cadena circular → blocked", out["success"] is False and "blocked" in out,
              str(out.get("blocked")), SECTION)


class TestParallelFuture:
    def test_paralelo_restringido_por_guard(self, monkeypatch):
        """
        allow_parallel_models=True pero la RAM actual no aguanta dos modelos
        → el segundo se rechaza (la restricción manda sobre el flag).
        """
        from src.core.agent_coordinator import AgentCoordinator
        from src.core.memory_guard import MemoryGuard
        g = MemoryGuard(warn_ratio=0.85, hard_ratio=0.95, headroom_gb=0.5)
        # ✅ v0.7.4 (ISSUE-094): RAM TOTAL fija (16 GB), no la del equipo.
        total = 16.0
        monkeypatch.setattr(g, "ram_total_gb", lambda: total)
        # ✅ FIX 2026-08-29: "RAM casi llena" (total-0.5) rechazaba también el PRIMER
        # modelo (used + required + headroom ya excede el límite). Con used = 40% un
        # modelo cabe (≈12.9GB < 15.2) y dos no (≈18.9GB > 15.2): intención real.
        monkeypatch.setattr(g, "ram_used_gb", lambda: total * 0.4)  # un modelo cabe, dos no
        fs = FakeSwap()
        coord = AgentCoordinator(swap=fs, guard=g, board=None, allow_parallel=True)
        r1 = coord.run_on_model("qwen3:8b", lambda m: "A", caller="p1")
        r2 = coord.run_on_model("qwen2.5-coder:7b", lambda m: "B", caller="p2")
        check("primero carga", r1["success"] is True, str(r1), SECTION)
        check("segundo rechazado por RAM", r2["success"] is False and r2["reason"] == "refused_ram",
              str(r2), SECTION)


class TestIntegracionSystemAgent:
    def test_system_agent_run_model_chain(self):
        """SystemAgent expone run_model_chain() (FASE 4, plan 4.3)."""
        from src.agents.system_agent import SystemAgent
        check("SystemAgent.run_model_chain existe",
              hasattr(SystemAgent, "run_model_chain"),
              "método presente", SECTION)


class TestMemoryRetention:
    def test_session_date(self):
        from src.memory.gateway import _session_date
        assert _session_date("saintwick_20260905") == "20260905"
        assert _session_date("nicegui_20260904_135338_322087") == "20260904"
        assert _session_date("coder_33172afc") is None
        assert _session_date(None) is None

    def test_prune_old_sessions(self, tmp_path):
        import sqlite3
        from datetime import datetime, timedelta
        from types import SimpleNamespace
        from src.memory.gateway import MemoryGateway
        # ✅ v0.6.9p: ids relativos a HOY — antes usaba fechas fijas y el
        # test se rompía solo al pasar el tiempo (saintwick_20260905 dejaba
        # de ser "reciente" y el prune con days=2 la borraba).
        hoy = datetime.now()
        vieja = f"saintwick_{(hoy - timedelta(days=10)):%Y%m%d}"
        reciente = f"saintwick_{(hoy - timedelta(days=1)):%Y%m%d}"
        db = tmp_path / "mem.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                                   content TEXT, timestamp TEXT, metadata TEXT, tokens INTEGER);
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT,
                                   updated_at TEXT, message_count INTEGER, metadata TEXT);
        """)
        for sid in [vieja, reciente, "coder_abc"]:
            con.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (sid, "user", "x", "t"))
        con.commit()
        con.close()

        obj = object.__new__(MemoryGateway)
        obj._long_term = SimpleNamespace(db_path=str(db))
        obj.session_id = "test"
        obj._prune_old_sessions(days=2)

        con = sqlite3.connect(str(db))
        rows = [r[0] for r in con.execute("SELECT DISTINCT session_id FROM messages")]
        con.close()
        assert rows == [reciente, "coder_abc"], rows


class TestShortTermRenewal:
    def test_ventana_se_renueva(self):
        import time as _t
        from src.memory.short_term import ShortTermMemory
        m = ShortTermMemory(ttl_seconds=1)
        m.add("user", "hola")
        _t.sleep(1.2)
        ok = m.add("assistant", "segundo")   # tras expirar: renueva la ventana (descarta lo viejo)
        assert ok is True                    # antes: add devolvía False (expirada para siempre)
        assert len(m.get_recent(10)) == 1    # solo el mensaje nuevo (el viejo expiró)
        assert m.add("user", "tercero") is True
        assert len(m.get_recent(10)) == 2


# ── Fusionado desde test_axioma_08_regresiones_log.py ──
import inspect
import logging
from unittest.mock import MagicMock, patch


def _coder_agent():
    from src.agents.coder import CoderAgent
    with patch("src.agents.base.OllamaClient"):
        return CoderAgent(model="m", timeout=1, enable_memory=False)


def test_coder_execute_propaga_session_y_user_a_build_prompt():
    """execute() debe pasar session_id/user_id a _build_prompt (bug: no los pasaba)."""
    from src.domain.entities import Message, MessageRole
    agent = _coder_agent()
    msg = Message(role=MessageRole.USER, content="crea una funcion en python")

    calls: dict = {}
    agent._build_prompt = lambda *a, **k: (calls.update(k) or "PROMPT")
    agent._generate = lambda **k: "print('hola')"

    result = agent.execute(msg, task_type="code_generation",
                           session_id="s1", user_id="u1")

    assert calls.get("session_id") == "s1", f"session_id no propagado a _build_prompt: {calls}"
    assert calls.get("user_id") == "u1", f"user_id no propagado a _build_prompt: {calls}"
    assert result.success is True, f"execute falló: {result.error}"


def test_coder_build_prompt_acepta_session_y_user():
    """_build_prompt no debe lanzar NameError con session_id/user_id."""
    from src.domain.entities import Message, MessageRole
    agent = _coder_agent()
    msg = Message(role=MessageRole.USER, content="explica este codigo")
    prompt = agent._build_prompt(
        msg, "code_explanation", session_id="s1", user_id="u1",
    )
    assert isinstance(prompt, str) and len(prompt) > 0


def test_researcher_propaga_session_a_build_search_prompt():
    """execute → _execute_general_search → _build_search_prompt con identidad."""
    from src.agents.researcher import ResearcherAgent
    from src.domain.entities import Message, MessageRole
    with patch("src.agents.base.OllamaClient"):
        agent = ResearcherAgent(model="m", timeout=1, enable_memory=False)
    msg = Message(role=MessageRole.USER, content="busca informacion sobre python")

    calls: dict = {}
    agent._build_search_prompt = lambda *a, **k: (calls.update(k) or "PROMPT")
    agent._generate = lambda **k: "resumen sintetizado"
    agent.sources.search_web = lambda *a, **k: []

    result = agent.execute(msg, task_type="web_search",
                           session_id="s2", user_id="u2")

    assert calls.get("session_id") == "s2", f"session_id no propagado: {calls}"
    assert calls.get("user_id") == "u2", f"user_id no propagado: {calls}"
    assert result.success is True, f"execute falló: {result.error}"


def test_researcher_build_search_prompt_acepta_session_y_user():
    """_build_search_prompt no debe lanzar NameError (bug del log)."""
    from src.agents.researcher import ResearcherAgent
    from src.domain.entities import Message, MessageRole
    with patch("src.agents.base.OllamaClient"):
        agent = ResearcherAgent(model="m", timeout=1, enable_memory=False)
    msg = Message(role=MessageRole.USER, content="noticias de ciencia")
    prompt = agent._build_search_prompt(
        msg, [], task_type="web_search", session_id="s2", user_id="u2",
    )
    assert isinstance(prompt, str) and len(prompt) > 0


def test_chat_usa_timeout_configurable_no_hardcode():
    """El agent loop del chat debe usar settings.ollama_timeout_chat, no 60.
    ✅ v0.6.8mm: _handle_chat vive en dispatcher_handlers_extra.py (refactor
    50/50) — se inspecciona la 2ª mitad además del archivo original."""
    import src.core.dispatcher_handlers as dh
    from config.settings import settings

    src = inspect.getsource(dh)
    try:
        import src.core.dispatcher_handlers_extra as dhx
        src += inspect.getsource(dhx)
    except Exception:
        pass
    assert "ollama_timeout_chat" in src, "el chat no referencia ollama_timeout_chat"
    assert "timeout=60" not in src, "resurgió el timeout=60 hardcodeado en chat"
    assert settings.ollama_timeout_chat >= 120, (
        f"ollama_timeout_chat={settings.ollama_timeout_chat} es muy bajo para CPU"
    )


def _mem_gateway(tmp_path):
    import config.paths as cp
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cp.Paths, "MEMORY", tmp_path)
    from src.memory.gateway import MemoryGateway
    gw = MemoryGateway(
        session_id="reg_log_1",
        enable_semantic=False, enable_optimizer=False, enable_kg=False,
    )
    return gw, monkeypatch


def test_add_message_content_no_str_no_crash_y_diagnostica(tmp_path):
    """Content no-str (rompía la validación de long_term) → no crash + _log_error."""
    gw, mp = _mem_gateway(tmp_path)
    import src.memory.gateway as G
    try:
        errores: list = []
        orig_log_error = G._log_error

        def spy_log_error(e, ctx, **kwargs):  # noqa: ARG001
            errores.append((ctx, str(e)))

        G._log_error = spy_log_error
        try:
            r = gw.add_message("assistant", ["no", "soy", "str"], metadata={})
            assert r is True, "add_message debe degradar con éxito (short-term acepta)"
            assert any("long_term" in c for c, _ in errores), (
                f"el fallo de long_term no se diagnosticó con _log_error: {errores}"
            )
        finally:
            G._log_error = orig_log_error
    finally:
        gw.close()
        mp.undo()


def test_add_message_fallo_total_diagnostica_estado(tmp_path, caplog):
    """Si TODAS las capas fallan → summary success=False + warning con estado."""
    gw, mp = _mem_gateway(tmp_path)
    try:
        gw._short_term.add = lambda *a, **k: False   # capa caída
        gw._long_term.add = lambda *a, **k: 0        # id inválido
        with caplog.at_level(logging.WARNING):
            r = gw.add_message("assistant", "hola")
        assert r is False, "con todas las capas caídas debe devolver False"
        assert any("no persistió" in m for m in caplog.messages), (
            f"no se logueó el diagnóstico de fallo: {caplog.messages}"
        )
    finally:
        gw.close()
        mp.undo()


class TestLogHealthReport:
    SAMPLE = [
        "[2026-09-04 15:00:00.100] [INFO    ] [DISPATCHER          ] Processing: hola | {\"session_id\": \"s1\", \"content_length\": 4}",
        "[2026-09-04 15:00:15.200] [INFO    ] [DISPATCHER          ] Classification: general_chat | {\"task_type\": \"general_chat\", \"classification_duration_ms\": 15000.0}",
        "[2026-09-04 15:00:16.000] [INFO    ] [DISPATCHER          ] Request completed: general_chat | {\"handler\": \"LLM_Chat\", \"latency_ms\": 16000.0, \"content_length\": 30, \"success\": true}",
        "[2026-09-04 15:00:17.000] [WARNING ] [VALIDATOR          ] Validation result: FAIL (score=5.0) | {\"score\": 5.0, \"passed\": false, \"attempt\": 1, \"threshold\": 6.5}",
        "[2026-09-04 15:00:18.000] [INFO    ] [DISPATCHER          ] Chat validation FAILED — keeping original (single-pass) | {\"task_type\": \"general_chat\", \"reason\": \"no_regeneration\", \"score\": 5.0}",
        "[2026-09-04 15:00:19.000] [INFO    ] [DISPATCHER          ] L3 Semantic PARTIAL HIT: file_search | {\"session_id\": \"s1\"}",
        "[2026-09-04 15:00:20.000] [INFO    ] [DISPATCHER          ] Classification (pre-routing): file_search | {\"task_type\": \"file_search\", \"llm_skipped\": true}",
        "[2026-09-04 15:00:21.000] [INFO    ] [DISPATCHER          ] Chat Agent Loop - Iteration 1 | {\"session_id\": \"s1\", \"tools_available\": 7}",
        "[2026-09-04 15:00:22.000] [INFO    ] [DISPATCHER          ] LLM requested tool calls | {\"tools_requested\": [\"web_search\"]}",
        "[2026-09-04 15:00:23.000] [INFO    ] [DISPATCHER          ] Chat validation SKIPPED (A1) | {\"reason\": \"short\", \"content_len\": 12}",
        "[2026-09-04 15:00:24.000] [ERROR   ] [AGENT               ] [✗] algo falló",
    ]

    def test_parse_line(self):
        from tools.log_health_report import parse_line
        ev = parse_line(self.SAMPLE[0])
        assert ev is not None and ev["category"] == "DISPATCHER"
        assert ev["extra"].get("content_length") == 4
        assert parse_line("no es una línea de log") is None

    def test_collector_metricas(self):
        from tools.log_health_report import HealthCollector, parse_line
        c = HealthCollector()
        for raw in self.SAMPLE:
            ev = parse_line(raw)
            if ev:
                c.feed(ev)
        assert len(c.requests) == 1
        assert len(c.llm_classifications) == 1
        assert c.pre_routed == 1
        assert c.l3_partial == 1
        assert c.single_pass_fails == 1
        assert len(c.validator_results) == 1 and not c.validator_results[0]["passed"]
        assert c.tool_requested_events == 1 and c.chat_loops == 1
        assert c.validation_skips.get("short", 0) == 1
        assert len(c.errors) == 1


class TestLogHealthReportV2:
    MEM_ERR = ("[2026-09-04 15:00:26.000] [ERROR   ] [MEMORY              ] "
               "[✗] add — short_term | {\"operation\": \"add\", "
               "\"collection\": \"short_term\", \"success\": false, "
               "\"duration_ms\": 0}")
    INFRA_ERR = ("[2026-09-04 15:00:27.000] [ERROR   ] [OLLAMA_CLIENT       ] "
                 "connection timed out | {\"model\": \"qwen3:8b\"}")
    SANDBOX_ERR = ("[2026-09-04 15:00:25.000] [ERROR   ] [AGENT               ] "
                   "[✗] sandbox — SANDBOX_EXECUTION — FAILED | "
                   "{\"agent\": \"sandbox\", \"duration_ms\": 500.0}")
    CODER_START = ("[2026-09-04 15:00:25.500] [INFO    ] [AGENT               ] "
                   "[✓] coder — code_generation — started | "
                   "{\"task_type\": \"code_generation\"}")
    RETRY = ("[2026-09-04 15:00:28.000] [INFO    ] [OLLAMA_CLIENT       ] "
             "Sending request (attempt 2/2) | {\"model\": \"qwen3:8b\"}")
    L3_FULL = ("[2026-09-04 15:00:29.000] [INFO    ] [DISPATCHER          ] "
               "L3 Hybrid FULL HIT: code | {\"session_id\": \"s1\"}")
    OTRO_ERR = ("[2026-09-04 15:00:30.000] [ERROR   ] [AGENT               ] "
                "[✗] algo raro pasó")

    LINES = [MEM_ERR, INFRA_ERR, SANDBOX_ERR, CODER_START, RETRY, L3_FULL]

    def _collect(self):
        from tools.log_health_report import HealthCollector, parse_line
        c = HealthCollector()
        for raw in self.LINES:
            ev = parse_line(raw)
            if ev:
                c.feed(ev)
        return c

    def test_reintentos(self):
        c = self._collect()
        assert c.attempts_total == 1
        assert c.attempts_retried == 1

    def test_sandbox_reparacion(self):
        c = self._collect()
        assert c.sandbox_fails == 1
        assert c.repairs == 1

    def test_full_hit(self):
        c = self._collect()
        assert c.l3_full == 1

    def test_clasificacion_errores(self):
        from tools.log_health_report import classify_error, parse_line
        assert classify_error(parse_line(self.MEM_ERR)) == "memoria"
        assert classify_error(parse_line(self.INFRA_ERR)) == "infra"
        assert classify_error(parse_line(self.SANDBOX_ERR)) == "sandbox_esperado"
        assert classify_error(parse_line(self.OTRO_ERR)) == "otros"

    def test_build_report_v2(self):
        from tools.log_health_report import build_report, summarize
        c = self._collect()
        out = build_report(c, ["test.log"])
        assert "VERDICTO:" in out
        assert "Errores clasificados" in out
        assert "Anomalías / diagnóstico" in out
        assert "Pipeline de código" in out
        assert "Validaciones dinámicas **negativas**" in out
        assert "reintentados" in out
        s = summarize(c)
        assert s["sandbox_fails"] == 1 and s["repairs"] == 1
        assert s["mem_errors"] == 1 and s["infra_errors"] == 1
        # los fallos de memoria NO inflan la telemetría de ops exitosas
        assert s["mem_writes"] == 0

    def test_verdicto(self):
        from tools.log_health_report import verdict_of
        assert verdict_of([("✅", "ok")]) == "✅ Sano"
        assert verdict_of([("ℹ️", "aviso"), ("⚠️", "fallo")]) == "⚠️ Atender avisos"
        assert verdict_of([("❌", "grave")]) == "❌ Revisar"

    def test_hint_memoria(self):
        from tools.log_health_report import hint_for
        assert hint_for("[✗] add — short_term") is not None
        assert hint_for("Request completed: ok") is None


class TestLogStaleness:
    def _summarize_with_ts(self, ts_iso: str):
        from tools.log_health_report import HealthCollector, parse_line, summarize
        c = HealthCollector()
        ev = parse_line(f"[{ts_iso}] [INFO    ] [DISPATCHER          ] "
                        f"Request completed: chat | {{\"success\": true}}")
        assert ev is not None
        c.feed(ev)
        return summarize(c)

    def test_log_viejo_marca_desactualizado(self):
        from tools.log_health_report import THRESHOLDS, compute_anomalies
        # Un evento del año 2020 → claramente stale.
        s = self._summarize_with_ts("2020-01-01 10:00:00.000")
        assert s["stale_hours"] is not None and s["stale_hours"] > 1000
        anoms = compute_anomalies(s, dict(THRESHOLDS))
        assert any("DESACTUALIZADO" in t for _, t in anoms), anoms

    def test_log_reciente_no_marca_stale(self):
        from datetime import datetime, timezone
        from tools.log_health_report import THRESHOLDS, compute_anomalies
        now = datetime.now(timezone.utc).astimezone()
        s = self._summarize_with_ts(now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        assert s["stale_hours"] is not None and s["stale_hours"] < 1.0
        anoms = compute_anomalies(s, dict(THRESHOLDS))
        assert not any("DESACTUALIZADO" in t for _, t in anoms), anoms

    def test_reporte_incluye_vigencia(self):
        from tools.log_health_report import build_report
        from tools.log_health_report import HealthCollector, parse_line
        c = HealthCollector()
        c.feed(parse_line("[2020-01-01 10:00:00.000] [INFO    ] "
                          "[DISPATCHER          ] Request completed: chat | {}"))
        out = build_report(c, ["test.log"])
        assert "Vigencia:" in out
        assert "DESACTUALIZADO" in out

    def test_sin_timestamps_avisa(self):
        from tools.log_health_report import _staleness_hours
        assert _staleness_hours(None) is None


# ════════════════════════════════════════════════════════════════
# ISSUE-130: el chat deja de ser aislado — ventana reciente + historial
# ════════════════════════════════════════════════════════════════

def _gateway_con_mensajes(tmp_path, n: int = 30):
    """Gateway real (memoria en `tmp_path`) con `n` mensajes cronológicos."""
    gw, mp = _mem_gateway(tmp_path)
    for i in range(1, n + 1):
        gw.add_message("user" if i % 2 == 0 else "assistant", f"m{i}")
    return gw, mp


def test_ventana_reciente_devuelve_los_ULTIMOS_no_los_viejos(tmp_path):
    """`get_context(limit)` sin `query` devuelve el FINAL de la ventana.

    Antes: pedía los últimos `limit*3` (cronológicos) y devolvía
    `raw_messages[:limit]` → en una sesión de 30 mensajes con limit=6 daba
    m13..m18 en vez de m25..m30, o sea el tercio más VIEJO: justo lo que leen
    coder/researcher/analyst cuando piden contexto.
    """
    gw, mp = _gateway_con_mensajes(tmp_path, 30)
    try:
        ctx = gw.get_context(limit=6, include_system=False)
        assert [m["content"] for m in ctx] == [f"m{i}" for i in range(25, 31)]
        # Con límite mayor que la sesión devuelve todo, en orden, sin inventar.
        todo = gw.get_context(limit=100, include_system=False)
        assert [m["content"] for m in todo] == [f"m{i}" for i in range(1, 31)]
    finally:
        gw.close()
        mp.undo()


class _GuardStub:
    """Stub mínimo: solo lo que `_historial_para_prompt` toca."""
    def __init__(self, gw=None, explota=False):
        self._gw, self._explota = gw, explota

    def _get_session_gateway(self, _session_id):
        if self._explota:
            raise RuntimeError("memoria caída")
        return self._gw


def _historial(gw=None, session_id="s1", texto_actual=None, explota=False):
    from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin
    return DispatcherProcessGuardMixin._historial_para_prompt(
        _GuardStub(gw, explota), session_id, texto_actual)


def test_historial_del_chat_en_orden_sin_system_y_recortado(tmp_path):
    from src.core.dispatcher_process_guard import HISTORIAL_CHARS_POR_MENSAJE
    gw, mp = _gateway_con_mensajes(tmp_path, 4)          # m1..m4
    gw.add_message("system", "instrucción interna")      # no debe viajar
    gw.add_message("user", "y" * 2000)                   # se recorta
    try:
        assert [(m["role"], m["content"]) for m in _historial(gw)] == [
            ("assistant", "m1"), ("user", "m2"),
            ("assistant", "m3"), ("user", "m4"),
            ("user", "y" * HISTORIAL_CHARS_POR_MENSAJE),
        ]
    finally:
        gw.close()
        mp.undo()


def test_historial_se_degrada_y_no_duplica_el_mensaje_actual(tmp_path):
    """Sin sesión, con la memoria caída o con el texto repetido: no se rompe."""
    gw, mp = _mem_gateway(tmp_path)
    try:
        gw.add_message("user", "¿cuál es el clima?")
        assert _historial(gw, session_id=None) == []
        assert _historial(explota=True) == []
        assert _historial(gw, texto_actual="¿cuál es el clima?") == []
        assert _historial(gw, texto_actual="otra cosa") != []
    finally:
        gw.close()
        mp.undo()


class _RegistroFalso:
    def has(self, _nombre):
        return False


class _LLMFalso:
    """Captura el payload REAL que el chat le manda al modelo."""
    def __init__(self):
        self.payloads: list = []

    def chat(self, messages=None, **_kw):
        from types import SimpleNamespace
        self.payloads.append([dict(m) for m in (messages or [])])
        return SimpleNamespace(content="respuesta", tool_calls=None,
                               was_truncated=False, prompt_eval_count=12,
                               eval_count=3)


class _StubChat:
    """Portal de memoria y modelo reemplazados; el resto (`_handle_chat`,
    `_historial_para_prompt`) son los REALES del mixin. Va PRIMERO en las bases
    para que estos reemplazos ganen en el orden de resolución."""
    def __init__(self, gws, llm):
        self.llm_client = llm
        self._gws = gws          # portal de memoria por sesión (como el real)
        self._user_id = None

    def _get_session_gateway(self, session_id):
        return self._gws.get(session_id)

    def _format_tools_for_ollama(self, _names):
        return []

    def _should_use_autonomous_mode(self, _texto):
        return False


def _despachador_de_chat(gws, monkeypatch):
    import src.core.dispatcher_handlers_extra as M
    from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
    from src.core.dispatcher_process_guard import DispatcherProcessGuardMixin

    monkeypatch.setattr(M, "get_registry", lambda: _RegistroFalso())

    class _Disp(_StubChat, DispatcherHandlersExtraMixin,
                DispatcherProcessGuardMixin):
        pass

    llm = _LLMFalso()
    return _Disp(gws, llm), llm


def test_chat_manda_el_turno_previo_y_nada_mas_sin_historial(tmp_path, monkeypatch):
    """La consulta 2 de la misma sesión viaja CON el turno anterior.

    Antes `messages` era `[system, user_actual]`: cada consulta se respondía
    aislada aunque la memoria ya guardaba el intercambio. La segunda mitad del
    test fija el otro extremo: sesión nueva ⇒ payload igual que antes.
    """
    import asyncio
    from src.domain.entities import Message
    from src.memory.gateway import MemoryGateway
    gw, mp = _mem_gateway(tmp_path)
    # Misma carpeta de memoria (ya apuntada a tmp_path), sesión distinta y vacía.
    vacio = MemoryGateway(session_id="sesion_vacia", enable_semantic=False,
                          enable_optimizer=False, enable_kg=False)
    try:
        gw.add_message("user", "me llamo Saintwick")
        gw.add_message("assistant", "Un gusto, Saintwick.")
        disp, llm = _despachador_de_chat(
            {"sesion_con_historial": gw, "sesion_vacia": vacio}, monkeypatch)

        resp = asyncio.run(disp._handle_chat(
            Message(content="¿cómo me llamo?", role="user"),
            "chat", "sesion_con_historial", None))
        assert resp.error is None, resp.error
        assert [(m["role"], m["content"]) for m in llm.payloads[0]] == [
            ("system", llm.payloads[0][0]["content"]),
            ("user", "me llamo Saintwick"),
            ("assistant", "Un gusto, Saintwick."),
            ("user", "¿cómo me llamo?"),
        ], llm.payloads[0]

        resp2 = asyncio.run(disp._handle_chat(
            Message(content="hola", role="user"), "chat", "sesion_vacia", None))
        assert resp2.error is None, resp2.error
        assert [m["role"] for m in llm.payloads[1]] == ["system", "user"]
    finally:
        vacio.close()
        gw.close()
        mp.undo()


def test_el_umbral_cross_sesion_se_respeta(monkeypatch):
    """`ISSUE-143`: mutar el umbral a 0,0 no rompía nada (hueco medido 2026-10-04)."""

    from config.settings import settings
    from src.memory.gateway import MemoryGateway as GW
    obj, visto = GW.__new__(GW), {}
    obj.user_id, obj._semantic, obj.session_id = "u", object(), "s"
    obj.search_semantic = lambda q, **k: (visto.update(k), [])[1]
    monkeypatch.setattr(settings, "cross_session_min_similarity", 0.83)
    obj._inject_cross_session([], "consulta", 1024, task_type="general_chat")
    assert visto.get("min_similarity") == 0.83, visto