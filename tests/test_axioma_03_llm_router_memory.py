#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral (archivo 3/6): LLM, router y memoria
from __future__ import annotations

import importlib
import time
from unittest.mock import MagicMock, patch

try:
    from .axioma_report import section, check
except ImportError:  # pragma: no cover
    from axioma_report import section, check

SECTION = "3. LLM, router y memoria"

try:
    from .suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import
except ImportError:  # pragma: no cover
    from suite_utils import import_safe as _import, has_symbols as _has_symbols,\
        api_check as _api_check, check_import as _check_import

LLM_API = {
    "src.llm.client_base": ["OllamaClientBase", "ScenarioType"],
    "src.llm.client": ["OllamaClient", "CircuitBreaker"],
    "src.llm.config": ["LLMConfig", "ModelConfig", "TaskModelMapping", "CODE_MODEL", "DEFAULT_MODEL"],
    "src.llm.judge": ["JudgeMode", "JudgeResult", "LLMJudge", "_parse_output"],
    "src.llm.router": ["ModelRouter", "RoutingDecision", "LLMConfig"],
    "src.llm.vllm_client": ["VLLMClient"],
}
ROUTER_API = {
    "src.router.cache": ["RouterCache", "CacheEntry", "SemanticCacheL3", "CacheL2Class"],
    "src.router.cache_l2": ["CacheL2", "get_cache_l2", "reset_cache_l2"],
    "src.router.cache_l3": ["SemanticCacheL3", "CacheRecord"],
    "src.router.classifier": ["IntentClassifier"],
    "src.router.classifier_api": ["APIDetector"],
    "src.router.classifier_core": ["IntentClassifierCore"],
    "src.router.smart_router": ["SmartRouter"],
    "src.router.smart_router_api": [],
    "src.router.smart_router_core": [],
}
MEMORY_API = {
    "src.memory.gateway": ["MemoryGateway", "Palace"],
    "src.memory.gateway_ops": ["MemoryGatewayOps"],
    "src.memory.semantic": ["SemanticMemory", "MemoryRecord", "EMBED_DIMENSION"],
    "src.memory.semantic_base": ["EmbeddingCache", "MemoryRecord"],
    "src.memory.short_term": ["ShortTermMemory"],
    "src.memory.long_term": ["LongTermMemory", "SQLiteConnectionPool"],
    "src.memory.long_term_base": ["SQLiteConnectionPool"],
    "src.memory.context_window": ["ContextWindowManager", "get_context_window_manager"],
    "src.memory.context_strategies": ["BaseSelector"],
    "src.memory.embedding_model_singleton": ["get_embedding_model"],
    "src.memory.embedding_optimizer": ["EmbeddingOptimizer", "EmbeddingCache"],
    "src.memory.knowledge_graph": ["KnowledgeGraph"],
    "src.memory.user_profile": ["UserProfile"],
}


class TestImportsYApi:
    def test_llm(self):
        section(SECTION)
        for mod, syms in LLM_API.items():
            _api_check(mod, syms)

    def test_router(self):
        for mod, syms in ROUTER_API.items():
            _api_check(mod, syms)

    def test_memory(self):
        for mod, syms in MEMORY_API.items():
            _api_check(mod, syms)


# ═══════════════════════════════════════════════════════════════
# 3B. Comportamiento LLM


class TestOllamaClient:
    def test_init_con_mocks(self):
        with patch("src.llm.client.httpx") as mock_httpx:
            from src.llm.client import OllamaClient
            mock_httpx.Client.return_value = MagicMock()
            client = OllamaClient(model="test-model", timeout=5, base_url="http://localhost:11434")
            check("OllamaClient() construible", client is not None, f"model={client.model}", SECTION)
            check("OllamaClient.get_cache_stats()", isinstance(client.get_cache_stats(), dict), "OK", SECTION)
            check("OllamaClient.clear_cache()", client.clear_cache() is None, "OK", SECTION)
            client.close()
            check("OllamaClient.close()", True, "OK", SECTION)

    def test_context_manager(self):
        with patch("src.llm.client.httpx"):
            from src.llm.client import OllamaClient
            with OllamaClient(model="m", timeout=1, base_url="http://localhost:11434") as client:
                check("OllamaClient.__enter__", client is not None, "OK", SECTION)
            check("OllamaClient.__exit__", True, "sin error", SECTION)

    def test_circuit_breaker(self):
        from src.llm.client import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
        check("CircuitBreaker.can_execute() inicial", cb.can_execute() is True, "OK", SECTION)
        cb.record_failure()
        cb.record_failure()
        check("CircuitBreaker se abre tras 2 fallos", cb.can_execute() is False, "OPEN", SECTION)
        cb.record_success()
        check("CircuitBreaker.record_success()", cb.failure_count == 0, "OK", SECTION)


class TestLLMConfig:
    def test_model_config(self):
        from src.llm.config import ModelConfig, CODE_MODEL, DEFAULT_MODEL
        mc = ModelConfig(name="qwen3:8b", task_types=["code_generation"])
        check("ModelConfig() construible", mc.name == "qwen3:8b", f"temp={mc.temperature}", SECTION)
        check("CODE_MODEL desde settings", isinstance(CODE_MODEL, str) and CODE_MODEL, CODE_MODEL, SECTION)
        check("DEFAULT_MODEL desde settings", isinstance(DEFAULT_MODEL, str) and DEFAULT_MODEL, DEFAULT_MODEL, SECTION)

    def test_get_model_for_task(self):
        from src.llm.config import LLMConfig, ModelConfig
        try:
            cfg = LLMConfig()
            mc = cfg.get_model_for_task("code_generation")
            check("LLMConfig.get_model_for_task()", isinstance(mc, ModelConfig) and bool(mc.name),
                  f"modelo={mc.name}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("LLMConfig.get_model_for_task()", False, f"{type(e).__name__}: {e}", SECTION)


class TestJudge:
    def test_parse_output(self):
        from src.llm.judge import JudgeMode, _parse_output
        score, label, reason = _parse_output("The response is correct.\n**Result:** 4", JudgeMode.ABSOLUTE)
        check("_parse_output(absolute)", score == 4.0, f"score={score} label={label}", SECTION)
        check("JudgeMode miembros", {m.value for m in JudgeMode} == {"absolute", "binary", "pairwise"},
              str([m.value for m in JudgeMode]), SECTION)

    def test_judge_mockeado(self):
        from src.llm.judge import LLMJudge
        with patch("src.llm.judge.OllamaClient") as MockClient:
            inst = MockClient.return_value
            inst.generate.return_value = MagicMock(content="The response is correct.\n**Result:** 4")
            judge = LLMJudge(model="m", timeout=1)
            res = judge.evaluate_absolute("instruccion", "respuesta")
            check("LLMJudge.evaluate_absolute()", res is not None, f"passed={res.passed}", SECTION)
            judge.close()
            check("LLMJudge.close()", True, "OK", SECTION)


# ═══════════════════════════════════════════════════════════════
# 3C. Comportamiento router


class TestClassifierCore:
    def test_clasificacion_sin_embeddings(self):
        from src.router.classifier_core import IntentClassifierCore
        clf = IntentClassifierCore(use_embeddings=False)
        cases = {
            "escribe un codigo fibonacci": "code_generation",
            "hola como estas": "general_chat",
            "busca noticias de tecnologia": "web_search",
        }
        for text, expected in cases.items():
            task_type, conf = clf.classify(text)
            check(f"classify({text!r})", task_type.value == expected,
                  f"→ {task_type.value} (conf={conf:.2f})", SECTION)

    def test_preprocess(self):
        """✅ ISSUE-064: ahora COLAPSA los espacios internos.

        Antes devolvía `"hola   mundo"` (solo `lower().strip()`), lo que hacía que
        los keywords multi-palabra fallaran en silencio con espaciado irregular
        (`'que hora es' in 'que  hora es'` es False).
        """
        from src.router.classifier_core import IntentClassifierCore
        out = IntentClassifierCore._preprocess_text("  HOLA   MUNDO  ")
        check("_preprocess_text() colapsa espacios", out == "hola mundo",
              f"out={out!r}", SECTION)
        assert out == "hola mundo"


class TestSmartRouter:
    def test_init(self):
        from src.router.smart_router import SmartRouter
        r = SmartRouter()
        check("SmartRouter() construible", r is not None, "OK", SECTION)
        check("SmartRouter atributos", hasattr(r, "classifier_threshold"), "OK", SECTION)


# ═══════════════════════════════════════════════════════════════
# 3D. Comportamiento memoria


class TestShortTerm:
    def test_ciclo(self):
        from src.memory.short_term import ShortTermMemory
        stm = ShortTermMemory()
        stm.add("user", "hola")
        stm.add("assistant", "mundo")
        history = stm.get_recent(2)
        check("ShortTermMemory.add/get_recent", len(history) == 2, f"{len(history)} msgs", SECTION)
        stm.clear()
        check("ShortTermMemory.clear()", len(stm.get_recent(10)) == 0, "OK", SECTION)


class TestContextWindow:
    def test_manager(self):
        from src.memory.context_window import ContextWindowManager
        cwm = ContextWindowManager()
        check("ContextWindowManager() construible", cwm is not None, "OK", SECTION)
        check("ContextWindowManager métodos", all(hasattr(cwm, m) for m in
              ["estimate_tokens", "fits", "optimize", "get_stats"]), "estimate/fits/optimize", SECTION)


class TestEmbeddingOptimizer:
    def test_optimizer_con_mocks(self):
        from src.memory.embedding_optimizer import EmbeddingOptimizer
        try:
            opt = EmbeddingOptimizer.__new__(EmbeddingOptimizer)
            check("EmbeddingOptimizer clase", opt is not None, "OK", SECTION)
        except Exception as e:  # noqa: BLE001
            check("EmbeddingOptimizer clase", False, f"{type(e).__name__}: {e}", SECTION)


class TestKnowledgeGraph:
    def test_kg(self, tmp_path):
        from src.memory.knowledge_graph import KnowledgeGraph, KGNode, KGEdge
        kg = KnowledgeGraph(db_path=str(tmp_path / "kg_test.db"))
        node = KGNode(node_id="python", label="lenguaje")
        kg.add_node(node)
        node2 = KGNode(node_id="programacion", label="concepto")
        kg.add_node(node2)
        kg.add_edge(KGEdge(source_id="python", target_id="programacion", relation="es_parte_de"))
        found = kg.get_node("python")
        check("KnowledgeGraph.add_node/get_node", found is not None and found.node_id == "python",
              str(found), SECTION)
        stats = kg.get_stats()
        check("KnowledgeGraph.get_stats()", isinstance(stats, dict), "OK", SECTION)


class TestUserProfile:
    def test_perfil(self, tmp_path):
        from src.memory.knowledge_graph import KnowledgeGraph
        from src.memory.user_profile import UserProfile
        kg = KnowledgeGraph(db_path=str(tmp_path / "kg_up.db"))
        up = UserProfile(kg=kg)
        check("UserProfile(kg) construible", up._user_id == "default", f"user={up._user_id}", SECTION)
        kg.get_stats()
        check("KG operativo", True, "OK", SECTION)


class TestMemoryGateway:
    def test_gateway_ligero(self, tmp_path):
        from src.memory.gateway import MemoryGateway
        gw = MemoryGateway(session_id="suite_test", enable_short=True, enable_long=False,
                           enable_semantic=False, enable_optimizer=False, enable_kg=False)
        check("MemoryGateway(solo short) construible", gw is not None, "OK", SECTION)
        try:
            gw.add_message("user", "hola memoria")
            ctx = gw.get_context()
            check("MemoryGateway.add_message/get_context", isinstance(ctx, list), f"{len(ctx)} msgs", SECTION)
        except Exception as e:  # noqa: BLE001
            check("MemoryGateway.add_message/get_context", False, f"{type(e).__name__}: {e}", SECTION)
        gw.close()
        check("MemoryGateway.close()", True, "OK", SECTION)


class TestCacheL3:
    def test_cache_con_embedder_mock(self):
        import threading
        from src.router.cache_l3 import SemanticCacheL3
        cache = SemanticCacheL3.__new__(SemanticCacheL3)
        cache._available = True
        cache._threshold = 0.6
        cache._ttl = 60
        cache._max_entries = 10
        cache._lock = threading.RLock()
        cache._stats = {"hits": 0, "misses": 0, "sets": 0,
                        "embed_errors": 0, "ttl_expirations": 0, "stale_pointers": 0}
        cache._table = MagicMock()
        cache._table.count_rows.return_value = 0
        cache._table.add.return_value = None
        cache._table.delete.return_value = None
        cache._table.search.return_value.to_list.return_value = []

        def _fake_embed(text):
            return [1.0, 2.0, 3.0]

        cache._embed = _fake_embed
        ok = cache.set("pregunta de prueba", {"answer": "42", "task_type": "general_chat"})
        check("SemanticCacheL3.set()", ok is True, "OK", SECTION)
        check("SemanticCacheL3._stats['sets']", cache._stats["sets"] == 1, "OK", SECTION)
        cache._available = False
        miss = cache.get("otra pregunta")
        check("SemanticCacheL3.get() desactivado → miss", miss is None, "OK", SECTION)


class TestCacheL2:
    def test_api(self):
        from src.router.cache_l2 import get_cache_l2
        c2 = get_cache_l2()
        check("get_cache_l2()", c2 is not None, type(c2).__name__, SECTION)


class TestLanceDBRace:
    """Regresión 2026-08-25: 'Table already exists' por inits concurrentes
    de LanceDB (VoicePipeline + chat). Ahora el init es race-safe:
    open → create → open (si otro proceso ganó la carrera)."""

    def test_reinit_misma_tabla(self, tmp_path):
        """Inicializar dos veces la misma colección no debe explotar."""
        from src.memory.semantic import SemanticMemory
        d = str(tmp_path / "lance_race")
        try:
            m1 = SemanticMemory(collection_name="test_race", persist_directory=d)
            m2 = SemanticMemory(collection_name="test_race", persist_directory=d)
            check("SemanticMemory reinit misma colección", m2 is not None,
                  f"tabla={m2.collection_name}", SECTION)
        except Exception as e:  # noqa: BLE001
            check("SemanticMemory reinit misma colección", False,
                  f"{type(e).__name__}: {e}", SECTION)

    def test_create_table_carrera_fallback(self, tmp_path):
        """Si open_table falla (NotFound) y create_table falla con
        'already exists' (otro proceso ganó), se abre la tabla existente."""
        from unittest.mock import MagicMock, patch
        fake_table = MagicMock()
        fake_db = MagicMock()
        # open falla (NotFound) → create falla (race) → open OK
        fake_db.open_table.side_effect = [Exception("table not found"),
                                          fake_table]
        fake_db.create_table.side_effect = ValueError("Table 'x' already exists")

        from src.memory.semantic import SemanticMemory
        try:
            with patch("src.memory.semantic.lancedb.connect", return_value=fake_db):
                m = SemanticMemory(collection_name="race_x",
                                   persist_directory=str(tmp_path / "lr"))
            check("init sobrevive a la carrera (open→create→open)", m._table is fake_table,
                  "fallback a open_table OK", SECTION)
        except Exception as e:  # noqa: BLE001
            check("init sobrevive a la carrera (open→create→open)", False,
                  f"{type(e).__name__}: {e}", SECTION)


# ══════════════════════════════════════════════════════════════════════════
# plan_memory v2 (2026-08-29) — Fase 0 (bug MD5), B (write-path) y
# Fase 1 (source_type + boost). Ver inforchat/plan_memory.txt.
# ══════════════════════════════════════════════════════════════════════════


class TestPlanMemory:
    def test_fase0_get_model_fn_usa_modelo_real(self, monkeypatch):
        """Fase 0: _get_model_fn() debe usar BGE-M3 real, no MD5."""
        import src.memory.context_strategies as cs
        from src.memory.context_strategies import RelevanceSelector

        class FakeModel:
            def embed(self, text):  # noqa: ARG002
                return [1.0, 0.0]

        class FakeOptimizer:
            def _hash_embedding(self, text):  # noqa: ARG002
                return [0.0, 1.0]

        rs = RelevanceSelector.__new__(RelevanceSelector)
        rs._optimizer = FakeOptimizer()

        monkeypatch.setattr(cs, "get_embedding_model", lambda: FakeModel())
        emb = rs._get_model_fn()("hola")
        check("Fase0: modelo real (no MD5)", emb == [1.0, 0.0], str(emb), SECTION)

        def _boom():  # modelo caído
            raise RuntimeError("modelo no disponible")

        monkeypatch.setattr(cs, "get_embedding_model", _boom)
        emb2 = rs._get_model_fn()("x")
        check("Fase0: fallback a hash solo si el modelo falla",
              emb2 == [0.0, 1.0], str(emb2), SECTION)

    def test_fase0_cache_namespace_bge_m3(self, monkeypatch):
        """Fase 0: el scoring usa el namespace 'BAAI/bge-m3' (comparte cache con Palace)."""
        import src.memory.context_strategies as cs
        from src.memory.context_strategies import (
            RelevanceSelector, SelectorConfig, TokenEstimator, RecencySelector,
        )

        class FakeModel:
            def embed(self, text):  # noqa: ARG002
                return [0.1] * 8

        class FakeOptimizer:
            def __init__(self):
                self.names = []

            def get_embedding(self, text, model_fn, model_name="default", use_cache=True):  # noqa: ARG002
                self.names.append(model_name)
                return model_fn(text)

            def _hash_embedding(self, text):  # noqa: ARG002
                return [0.5] * 8

        monkeypatch.setattr(cs, "get_embedding_model", lambda: FakeModel())
        opt = FakeOptimizer()
        est = TokenEstimator()
        config = SelectorConfig()
        rs = RelevanceSelector(est, opt, config, RecencySelector(est))
        rs._score_messages_by_relevance(
            [{"content": "hola", "metadata": {}}], "query", expected_source_type=None,
        )
        check("Fase0: namespace unificado BAAI/bge-m3",
              len(opt.names) >= 2 and all(n == "BAAI/bge-m3" for n in opt.names),
              str(opt.names), SECTION)

    def test_fase1_source_type_propaga_capas(self):
        """Fase 1: source_type se propaga a short_term y long_term (y Palace vía merge)."""
        from src.memory.gateway import MemoryGateway
        gw = MemoryGateway(
            session_id="suite_planmem", enable_long=False,
            enable_semantic=False, enable_optimizer=False, enable_kg=False,
        )
        try:
            gw.add_message("user", "hola", source_type="code")
            recent = gw._short_term.get_recent(5)
            md = next((m.get("metadata", {}) for m in recent if m.get("role") == "user"), {})
            check("source_type en short_term", md.get("source_type") == "code", str(md), SECTION)

            captured = {}

            class FakeLong:
                def add(self, session_id, role, content, metadata):  # noqa: ARG002
                    captured.update(metadata)
                    return 1

            gw._long_term = FakeLong()
            gw.add_message("assistant", "respuesta")  # default source_type
            check("source_type default 'chat' en long_term",
                  captured.get("source_type") == "chat", str(captured), SECTION)
        finally:
            gw.close()

    def test_fase1_boost_source_type(self, monkeypatch):
        """Fase 1: el boost sube el score cuando metadata.source_type == esperado."""
        import src.memory.context_strategies as cs
        from src.memory.context_strategies import (
            RelevanceSelector, SelectorConfig, TokenEstimator, RecencySelector,
        )

        class FakeModel:
            def embed(self, text):  # noqa: ARG002
                return [float(len(text)) / 100.0, 0.1]

        class FakeOptimizer:
            _cache = {}

            def get_embedding(self, text, model_fn, model_name="default", use_cache=True):  # noqa: ARG002
                if text not in self._cache:
                    self._cache[text] = model_fn(text)
                return self._cache[text]

            def _hash_embedding(self, text):  # noqa: ARG002
                return [0.5] * 2

        monkeypatch.setattr(cs, "get_embedding_model", lambda: FakeModel())
        opt = FakeOptimizer()
        est = TokenEstimator()
        config = SelectorConfig()
        rs = RelevanceSelector(est, opt, config, RecencySelector(est))

        msgs = [
            {"content": "mismo texto", "metadata": {"source_type": "code"}},
            {"content": "mismo texto", "metadata": {"source_type": "search"}},
            {"content": "mismo texto", "metadata": {}},  # viejo sin source_type
        ]
        scored = rs._score_messages_by_relevance(
            msgs, "misma query", expected_source_type="code",
        )
        score = {m.get("metadata", {}).get("source_type", "none"): s for m, s in scored}
        check("boost: code > search (mismo contenido)",
              score["code"] > score["search"], f"{score['code']:.3f} vs {score['search']:.3f}", SECTION)
        check("sin source_type → sin boost (== search)",
              score["none"] == score["search"], f"{score['none']:.3f} vs {score['search']:.3f}", SECTION)

    def test_fase1_task_to_source_cubre_tasktypes(self):
        """Fase 1: TASK_TO_SOURCE cubre los 47 TaskType con fuentes válidas."""
        from src.domain.types import TaskType
        from src.memory.gateway import TASK_TO_SOURCE
        sources = {"code", "search", "api", "analysis", "validation", "vision", "system", "chat"}
        bad = [
            t.value for t in TaskType
            if TASK_TO_SOURCE.get(t.value, "chat") not in sources
        ]
        check("TASK_TO_SOURCE: 47 TaskType → fuente válida", len(bad) == 0, str(bad[:5]), SECTION)
        check("code_generation → code",
              TASK_TO_SOURCE.get("code_generation") == "code", "code", SECTION)
        check("data_analysis → analysis",
              TASK_TO_SOURCE.get("data_analysis") == "analysis", "analysis", SECTION)
        check("general_chat → chat (default)",
              TASK_TO_SOURCE.get("general_chat", "chat") == "chat", "chat", SECTION)

    def test_fase1_estrategia_analysis_relevance(self):
        """Punto 5: los analysis completos van a RelevanceSelector (antes recency)."""
        from src.memory.gateway import MemoryGateway
        gw = MemoryGateway(
            session_id="suite_planmem2", enable_short=True, enable_long=False,
            enable_semantic=False, enable_optimizer=False, enable_kg=False,
        )
        try:
            for task in ("data_analysis", "document_analysis", "sentiment_analysis",
                         "trend_analysis", "risk_analysis"):
                s = gw._get_strategy_for_task(task)
                check(f"{task} → RelevanceSelector",
                      s.__class__.__name__ == "RelevanceSelector",
                      s.__class__.__name__, SECTION)
        finally:
            gw.close()

    def test_punto5_analyst_mapea_tasktype_real(self):
        """Punto 5: analyst.py mapea analysis_type a TaskType real (no inventado)."""
        from src.agents.analyst import _ANALYSIS_TASK_TYPE
        from src.domain.types import TaskType
        valid = {t.value for t in TaskType}
        bad = {k: v for k, v in _ANALYSIS_TASK_TYPE.items() if v not in valid}
        check("analyst: analysis_type → TaskType real", len(bad) == 0, str(bad), SECTION)
        check("document → document_analysis",
              _ANALYSIS_TASK_TYPE.get("document") == "document_analysis",
              str(_ANALYSIS_TASK_TYPE.get("document")), SECTION)
        check("summary → summarization",
              _ANALYSIS_TASK_TYPE.get("summary") == "summarization",
              str(_ANALYSIS_TASK_TYPE.get("summary")), SECTION)

    def test_b_agent_memoria_por_sesion(self, tmp_path):
        """B: _get_context(session_id=...) lee lo escrito por el write-path (long_term)."""
        from src.agents.coder import CoderAgent
        from src.memory.gateway import MemoryGateway
        # Write side (como dispatcher): long_term real pero sesión de prueba.
        gw = MemoryGateway(session_id="suite_planmem_sess")
        try:
            gw.add_message("user", "mensaje de prueba para la sesión", source_type="chat")
            # Read side (como agente): misma sesión.
            agent = CoderAgent()
            ctx = agent._get_context(
                limit=8, task_type="general_chat", query="prueba",
                max_tokens=1024, session_id="suite_planmem_sess",
            )
            check("B: agente lee la sesión del write-path", len(ctx) >= 1,
                  f"{len(ctx)} mensajes", SECTION)
            # Sin session_id → comportamiento previo (0).
            ctx2 = agent._get_context(limit=8, task_type="general_chat",
                                      query="prueba", max_tokens=1024)
            check("B: sin session_id no ve la sesión ajena", len(ctx2) == 0,
                  f"{len(ctx2)} mensajes", SECTION)
        finally:
            gw.close()
            # Limpiar la sesión de prueba del long_term real.
            import sqlite3
            from config.paths import Paths
            db = Paths.MEMORY / "axioma.db"
            try:
                con = sqlite3.connect(db)
                con.execute("DELETE FROM messages WHERE session_id='suite_planmem_sess'")
                con.commit()
                con.close()
            except Exception:  # noqa: BLE001
                pass

    def test_capa2_user_id_en_metadata(self):
        """Capa 2: el user_id se propaga a short_term y long_term."""
        from src.memory.gateway import MemoryGateway
        # ✅ Usuario PROPIO de la prueba: antes dependía de la configuración del equipo (ISSUE-151).
        usuario = "usuario-de-prueba"
        gw = MemoryGateway(
            session_id="suite_c2", user_id=usuario,
            enable_long=False, enable_semantic=False,
            enable_optimizer=False, enable_kg=False,
        )
        try:
            gw.add_message("user", "hola capa2")
            recent = gw._short_term.get_recent(5)
            md = next((m.get("metadata", {}) for m in recent if m.get("role") == "user"), {})
            check("Capa2: user_id en short_term", md.get("user_id") == usuario, str(md), SECTION)

            captured = {}

            class FakeLong:
                def add(self, session_id, role, content, metadata):  # noqa: ARG002
                    captured.update(metadata)
                    return 1

            gw._long_term = FakeLong()
            gw.add_message("assistant", "respuesta")
            check("Capa2: user_id en long_term", captured.get("user_id") == usuario,
                  str(captured), SECTION)
        finally:
            gw.close()

    def test_capa2_cross_sesion_aislamiento(self, tmp_path, monkeypatch):
        """Capa 2: sesión nueva del MISMO user inyecta cross-sesión; otro user NO."""
        import config.paths as cp
        import src.memory.gateway as gw_mod
        orig_memory = cp.Paths.MEMORY
        cp.Paths.MEMORY = tmp_path
        try:
            class FakeEmbed:
                def embed(self, text):  # noqa: ARG002  (constante → similitud 1.0)
                    return [0.5] * 1024

            monkeypatch.setattr(gw_mod, "get_embedding_model", lambda: FakeEmbed())

            gwA = gw_mod.MemoryGateway(session_id="c2_sess_A", user_id="usuario")
            gwA.add_message("user", "El proyecto Axioma usa Ollama local", source_type="chat")
            gwA.add_message("assistant", "Correcto, Local-First", source_type="chat")

            # ✅ ISSUE-092: el write a Palace es fire-and-forget en `self._executor`.
            # Antes se esperaba con un POLL y sin `assert`: en un runner lento el check
            # fallaba y **pytest salía 0** (el gate lo contaba como ❌ en el CI: 17/18).
            # Ahora se DRENA el executor (determinista, sin reloj → R5) y con `assert`.
            gwA._executor.shutdown(wait=True)   # espera a los bg writes encolados
            got = len(gwA._semantic.collection.to_arrow())
            check("Capa2: Palace recibió el user message", got >= 1, f"{got} filas", SECTION)
            assert got >= 1, f"el write a Palace no llegó tras drenar el executor ({got} filas)"

            gwB = gw_mod.MemoryGateway(session_id="c2_sess_B", user_id="usuario")
            ctx = gwB.get_context(
                task_type="general_chat", query="qué modelo usa axioma", max_tokens=1024,
            )
            cross = [m for m in ctx if (m.get("metadata") or {}).get("source") == "cross_session"]
            check("Capa2: sesión nueva del MISMO user inyecta cross-sesión",
                  len(cross) >= 1, f"{len(cross)} mensajes", SECTION)
            assert len(cross) >= 1, f"la sesión nueva del mismo user no vio cross-sesión ({len(cross)})" 

            # ✅ ISSUE-143 (MEDIDO): en tareas de CÓDIGO no se inyecta nada ajeno.
            ctx_code = gwB.get_context(task_type="code_generation",
                                       query="qué modelo usa axioma", max_tokens=1024)
            cross_code = [m for m in ctx_code
                          if (m.get("metadata") or {}).get("source") == "cross_session"]
            check("ISSUE-143: código NO recibe contexto ajeno", cross_code == [],
                  f"{len(cross_code)} mensajes", SECTION)
            assert cross_code == [], f"el código vio {len(cross_code)} mensajes ajenos"

            gwC = gw_mod.MemoryGateway(session_id="c2_sess_C", user_id="otro_user")
            ctxC = gwC.get_context(
                task_type="general_chat", query="qué modelo usa axioma", max_tokens=1024,
            )
            crossC = [m for m in ctxC if (m.get("metadata") or {}).get("source") == "cross_session"]
            check("Capa2: OTRO user NO ve los mensajes", len(crossC) == 0,
                  f"{len(crossC)} mensajes", SECTION)
            assert len(crossC) == 0, f"otro user vio mensajes ajenos ({len(crossC)})" 

            gwA.close()
            gwB.close()
            gwC.close()
        finally:
            cp.Paths.MEMORY = orig_memory


# ══════════════════════════════════════════════════════════════════════════
# UI de usuarios (2026-08-31) — UserRegistry + plumbing de identidad
# ══════════════════════════════════════════════════════════════════════════


class TestUserRegistry:
    def test_registro_seed_y_validaciones(self, tmp_path, monkeypatch):
        """Seed admin(root) + crear/duplicado/vacío."""
        import src.memory.user_registry as ur
        monkeypatch.setattr(ur, "_users_path", lambda: tmp_path / "users.json")

        # ✅ ISSUE-151: sin identidad no hay usuarios; con identidad se siembra ESA.
        from config.settings import settings
        monkeypatch.setattr(settings, "axioma_user_id", "")
        monkeypatch.setattr(settings, "axioma_user_role", "user")
        check("UserRegistry: sin identidad no hay usuarios", ur.list_users() == [],
              str(ur.list_users()), SECTION)
        monkeypatch.setattr(settings, "axioma_user_id", "del-equipo")
        sembrados = ur.list_users()
        check("UserRegistry: con identidad configurada se siembra esa",
              len(sembrados) == 1 and sembrados[0]["username"] == "del-equipo", str(sembrados), SECTION)

        u = ur.create_user("maria")
        check("UserRegistry: crea usuario normal",
              u["username"] == "maria" and u["role"] == "user"
              and u["email"] == "" and u["place"] == "" and u["country"] == "",
              str(u), SECTION)
        check("UserRegistry: listado incluye al nuevo",
              len(ur.list_users()) == 2, str(ur.list_users()), SECTION)

        try:
            ur.create_user("")
            check("UserRegistry: vacío rechazado", False, "no lanzó", SECTION)
        except ValueError:
            check("UserRegistry: vacío rechazado", True, "ValueError", SECTION)
        try:
            ur.create_user("maria")          # duplicado del recién creado (no del autor)
            check("UserRegistry: duplicado rechazado", False, "no lanzó", SECTION)
        except ValueError:
            check("UserRegistry: duplicado rechazado", True, "ValueError", SECTION)

        check("UserRegistry: get_user",
              (ur.get_user("maria") or {}).get("username") == "maria", "maria", SECTION)
        check("UserRegistry: get_user inexistente",
              ur.get_user("nadie") is None, "None", SECTION)

    def test_user_plumbing_agente(self, tmp_path, monkeypatch):
        """El gateway del agente usa el user_id de la sesión (UI de usuarios)."""
        import config.paths as cp
        orig = cp.Paths.MEMORY
        cp.Paths.MEMORY = tmp_path
        try:
            from src.agents.coder import CoderAgent
            a = CoderAgent()
            gw = a._memory_for("sess_u", user_id="maria")
            check("UI usuarios: gateway con user de sesión",
                  gw.user_id == "maria", str(gw.user_id), SECTION)
            from config.settings import settings
            gw2 = a._memory_for("sess_u2")
            check("UI usuarios: respaldo = la identidad configurada",
                  gw2.user_id == settings.axioma_user_id, str(gw2.user_id), SECTION)
        finally:
            cp.Paths.MEMORY = orig


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9e — Ruteo de la batería de calidad de código (prueba codigo.txt)
# ═══════════════════════════════════════════════════════════════


class TestCodeBatteryRouting:
    def test_prompts_de_codigo_por_descripcion(self):
        from src.router.classifier_core import IntentClassifierCore
        clf = IntentClassifierCore(use_embeddings=False)
        cases = {
            "Implementá una clase LRUCache en Python con métodos get(key), put(key, value) y un límite de capacidad.": "code_generation",
            "Implementá un sistema de cola de tareas con prioridades, workers asíncronos y retry con backoff exponencial.": "code_generation",
            "Creá un cliente HTTP con circuit breaker, pool de conexiones y timeout configurable. Usá httpx.": "code_generation",
            "Este código tiene una race condition. Encontrala y corregila:": "code_debugging",
            "Optimizá esta función que procesa 1M de registros.": "code_optimization",
            "Generá tests pytest para una función de transferencia bancaria que deba validar saldo suficiente.": "code_generation",
        }
        for text, expected in cases.items():
            tt, conf = clf.classify(text)
            check(f"batería → {expected}", tt.value == expected,
                  f"→ {tt.value} (conf={conf:.2f})", SECTION)

    def test_eventos_pubsub_no_es_finance(self):
        from src.router.classifier_core import IntentClassifierCore
        clf = IntentClassifierCore(use_embeddings=False)
        q = "Diseñá un sistema de eventos con bus pub/sub que soporte handlers síncronos y asíncronos."
        tt, conf = clf.classify(q)
        check("eventos pub/sub NO finance", tt.value == "code_generation",
              f"→ {tt.value} (conf={conf:.2f})", SECTION)


# ═══════════════════════════════════════════════════════════════
# Normalización de espacios en el matching de keywords (ISSUE-064)
# ═══════════════════════════════════════════════════════════════
# `lower().strip()` no matcheaba keywords multi-palabra con espaciado irregular.
class TestNormalizacionDeEspaciosEnKeywords:
    def _k(self):
        from src.router.classifier_core import IntentClassifierCore
        return IntentClassifierCore

    def test_preprocess_colapsa_espacios_internos(self):
        K = self._k()
        check("colapsa dobles espacios",
              K._preprocess_text("  Que   HORA   es  ") == "que hora es",
              repr(K._preprocess_text("  Que   HORA   es  ")), SECTION)
        check("tolera tabulaciones y saltos",
              K._preprocess_text("que\t hora\n es") == "que hora es",
              repr(K._preprocess_text("que\t hora\n es")), SECTION)
        check("None/vacío no rompe", K._preprocess_text(None) == ""
              and K._preprocess_text("") == "", "", SECTION)
        assert K._preprocess_text("  Que   HORA   es  ") == "que hora es"

    def test_keyword_multipalabra_matchea_con_espaciado_irregular(self):
        K = self._k()
        casos = [("que hora es", "que  hora es"),
                 ("cuanto cuesta", "cuanto   cuesta el dolar"),
                 ("dame las noticias", "dame\tlas noticias")]
        for kw, texto in casos:
            norm = K._preprocess_text(texto)
            check(f"matchea {kw!r} en {texto!r}",
                  K._keyword_match(kw, norm) is True, repr(norm), SECTION)
            assert K._keyword_match(kw, norm) is True

    def test_deteccion_de_saludo_tolera_espaciado(self):
        K = self._k()
        c = K()
        check("saludo con espacios raros se detecta",
              c._is_greeting_or_basic_chat("hola   que   tal") is True,
              "True", SECTION)
        assert c._is_greeting_or_basic_chat("hola   que   tal") is True


# ═══════════════════════════════════════════════════════════════
# Delta rule en las asociaciones del perfil (ISSUE-066)
# ═══════════════════════════════════════════════════════════════
# Antes `w ← 0.95·w + 1.0`: todo convergía al mismo valor. Ahora persigue la frecuencia.
class TestDeltaRulePerfil:
    @staticmethod
    def _rule():
        from src.memory.user_profile import UserProfile
        up = UserProfile.__new__(UserProfile)     # sin KG: solo la regla
        return up._compute_weight

    def test_es_acotado_y_no_satura_en_20(self):
        f = self._rule()
        w = 0.0
        for n in range(1, 200):
            w = f(w, hits=n, total_hits=n)
        check("nunca supera 1.0", w <= 1.0, str(w), SECTION)
        check("converge a 1.0 si es la única entidad", abs(w - 1.0) < 0.01, str(w), SECTION)
        assert w <= 1.0 and abs(w - 1.0) < 0.01

    def test_distingue_frecuencias(self):
        """El caso que la regla vieja no podía: 30 vs 100 sobre el mismo total."""
        f = self._rule()
        pocas, muchas = 0.0, 0.0
        for i in range(30):
            pocas = f(pocas, hits=i + 1, total_hits=100)
        for i in range(100):
            muchas = f(muchas, hits=i + 1, total_hits=100)
        check("30 interacciones < 100 interacciones", pocas < muchas,
              f"{pocas} < {muchas}", SECTION)
        check("y son claramente distintas (no ambas 20)",
              (muchas - pocas) > 0.3, f"dif={muchas - pocas:.3f}", SECTION)
        assert pocas < muchas and (muchas - pocas) > 0.3

    def test_corrige_cuando_el_interes_decae(self):
        """Si la frecuencia relativa cae, el peso BAJA (la vieja solo subía)."""
        f = self._rule()
        w = 0.9
        for _ in range(20):
            w = f(w, hits=50, total_hits=500)     # 10% de las interacciones
        check("el peso baja hacia la frecuencia real", w < 0.2, str(w), SECTION)
        assert w < 0.2

    def test_entradas_raras_no_rompen(self):
        f = self._rule()
        check("total 0 no divide por cero", isinstance(f(0.5, hits=0, total_hits=0), float),
              str(f(0.5, hits=0, total_hits=0)), SECTION)
        check("current None → 0.0", f(None) == 0.0, str(f(None)), SECTION)
        assert isinstance(f(0.5, hits=0, total_hits=0), float)

    def test_frequent_patterns_expone_hits(self):
        import tempfile, pathlib
        from src.memory.knowledge_graph import KnowledgeGraph
        from src.memory.user_profile import UserProfile
        tmp = pathlib.Path(tempfile.mkdtemp()) / "kg.db"
        try:
            kg = KnowledgeGraph(db_path=str(tmp))
        except TypeError:      # firma sin db_path: no ensuciar la DB real
            check("KnowledgeGraph acepta db_path (test hermético)", False,
                  "sin db_path no se puede aislar", SECTION)
            return
        up = UserProfile(kg=kg)
        for _ in range(4):
            up.learn_from_interaction("Genera una función Python para ordenar",
                                      "ok", "code_generation")
        pat = up.get_frequent_patterns()
        check("los patrones traen hits", bool(pat) and all("hits" in p for p in pat),
              str([(p.get("label"), p.get("hits")) for p in pat][:3]), SECTION)
        check("hits refleja las interacciones", any(p.get("hits") == 4 for p in pat),
              str([p.get("hits") for p in pat][:5]), SECTION)
        assert pat and all("hits" in p for p in pat)


class TestNumCtxEstable:
    """`num_ctx` en buckets y presupuesto coherente con el timeout (v0.7.5).

    Medido (Ollama 0.30.0, prompts reales; ISSUE-086/ISSUE-074): mismo `num_ctx`
    ⇒ prefill **1 tok / 0,38 s**; un delta de **+1** descarta el KV y re-evalúa todo
    (15.444 tok / 318 s); con buckets, 3 turnos bajan de 9570 a 3228 tokens
    re-evaluados. TG medida (decae con el contexto): 6,3 → 3,6 tok/s.
    """

    def test_buckets_y_estabilidad(self):
        from src.llm.client_base import OllamaClientBase as C
        ctxs = [C._calculate_num_ctx(n, p) for n, p in
                ((0, 16), (5000, 300), (20000, 900), (60000, 4096), (9 ** 5 * 3, 8192))]
        no_potencia = [v for v in ctxs if v < 32768 and v & (v - 1)]
        turnos = {C._calculate_num_ctx(n, 64) for n in (12097, 12165, 12233)}
        check("num_ctx es potencia de dos", not no_potencia, str(ctxs), SECTION)
        check("3 turnos comparten num_ctx (caché reutilizable)", len(turnos) == 1,
              str(turnos), SECTION)
        assert not no_potencia and len(turnos) == 1

    def test_sin_truncado_y_dentro_de_limites(self):
        from src.llm.client_base import (CHARS_PER_TOKEN, CTX_BUFFER, NUM_CTX_MAX,
                                         NUM_CTX_MIN, OllamaClientBase)
        malos = []  # buckets que truncarían el prompt
        for n in (0, 100, 3000, 8038, 12097, 69803, 150000):
            for p in (16, 300, 1800, 4096, 8192):
                ctx = OllamaClientBase._calculate_num_ctx(n, p)
                req = max(n // CHARS_PER_TOKEN, 512) + p + CTX_BUFFER
                if req <= NUM_CTX_MAX and ctx < req:
                    malos.append((n, p, ctx, req))
        extremos = [OllamaClientBase._calculate_num_ctx(0, 0),
                    OllamaClientBase._calculate_num_ctx(10 ** 7, 32768)]
        check("num_ctx >= requerido (no trunca)", not malos, str(malos), SECTION)
        check("num_ctx en [MIN, MAX]", extremos == [NUM_CTX_MIN, NUM_CTX_MAX],
              str(extremos), SECTION)
        assert not malos and extremos == [NUM_CTX_MIN, NUM_CTX_MAX]

    def test_presupuesto_no_excede_el_timeout(self):
        from config.settings import settings
        from src.core.dispatcher_handlers import MAX_TOKENS_ANALYSIS, MAX_TOKENS_CHAT
        from src.llm.client_base import (CHARS_PER_TOKEN, MARGEN_PRESUPUESTO, PP_TOK_S,
                                         TG_TOK_S, num_predict_coherente)
        malos, intactos = [], []
        for pedido, to, largo in ((4096, 45, 200), (4096, 180, 120), (8192, 420, 12000),
                                  (1800, 1200, 20000), (2048, 420, 3000)):
            ef = num_predict_coherente(pedido, to, largo)
            previsto = (largo // CHARS_PER_TOKEN) / PP_TOK_S + ef / TG_TOK_S
            if ef > pedido or previsto > to:
                malos.append((pedido, to, largo, ef, round(previsto)))
        for pedido, to, largo in ((100, 45, 120), (1800, 1200, 20000), (300, 180, 400)):
            intactos.append(num_predict_coherente(pedido, to, largo) == pedido)
        # Presupuesto real de cada ruta (prompt chico) contra el timeout de ESA ruta.
        rutas = {"chat": (MAX_TOKENS_CHAT, settings.ollama_timeout_chat),
                 "análisis": (MAX_TOKENS_ANALYSIS, settings.get_timeout_for_code_level(3))}
        fuera = {k: v for k, v in rutas.items()
                 if v[0] / TG_TOK_S > v[1]}
        check("presupuesto acotado al timeout", not malos, str(malos), SECTION)
        check("no se recorta lo que sí entra", all(intactos), str(intactos), SECTION)
        check("MAX_TOKENS_* entran en su timeout", not fuera, str(fuera), SECTION)
        assert not malos and all(intactos) and not fuera

    def test_el_chat_manda_num_predict_y_num_ctx(self):
        """Antes `chat()` no enviaba ninguno: contexto default y sin tope."""
        from unittest.mock import patch
        from src.llm.client import OllamaClient
        capturado = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self):
                return {"message": {"content": "ok"}, "done": True}

        with patch("src.llm.client.httpx"):
            cli = OllamaClient(model="m", timeout=180, base_url="http://localhost:11434")
            cli._get_client = lambda timeout=None: type("S", (), {
                "post": staticmethod(lambda url, json=None, timeout=None:
                                     (capturado.update(json), _Resp())[1])})()
            cli.chat([{"role": "user", "content": "hola"}], max_tokens=1024, timeout=180)
        opts = capturado.get("options", {})
        check("chat envía num_predict y num_ctx acotados",
              opts.get("num_predict", 0) <= 560 and opts.get("num_ctx") == 2048,
              str(opts), SECTION)
        assert "num_predict" in opts and "num_ctx" in opts and opts["num_predict"] <= 560
