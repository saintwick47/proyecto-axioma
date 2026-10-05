#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — MemoryGateway: Gateway Unificado de Memoria
# Ruta: src/memory/gateway.py
# Autor: SaintWick
# Versión: 0.3.1 (v4.2.0 — Paralelización y Optimización de Latencia)
# Cambios:
#   v0.1.0 — KnowledgeGraph como 4ta capa
#   v0.2.0 — ✅ v4.1.0: Integración de ContextStrategies y Caché de Estrategia
#   v0.3.0 — ✅ v4.2.0:
#            - ThreadPoolExecutor para I/O paralelo y Fire-and-Forget
#            - Palace.recall acepta query_embedding (evita doble cálculo)
#            - add_message() no bloquea por escrituras en Semantic
#            - search_semantic() y get_session_summary() paralelos
#            - Fix bug: r.get("score") → r.get("similarity")
#   v0.3.1 — ✅ FIX AUDITORÍA (resabio ChromaDB→LanceDB):
#            - model_fn en search_semantic()/_bg_semantic_store() chequeaba
#              hasattr(self._semantic, '_get_embedding') — método que Palace
#              (LanceDB) nunca tuvo. model_fn devolvía None SIEMPRE en cache
#              miss; el optimizer nunca cacheaba nada real (Palace recalculaba
#              el embedding internamente igual — el "evitar doble cómputo"
#              del comentario original nunca se cumplía). Ahora llama
#              get_embedding_model().embed(t) directo, como ya hace Palace.
#            - Mensaje de error "pip install mempalace>=3.1.0" corregido a
#              la dependencia real (lancedb pyarrow).
#            - Comentarios/docstrings "ChromaDB" actualizados a "LanceDB".
# ═══════════════════════════════════════════════════════════════
import logging
import time
import uuid
from typing import Optional, List, Dict, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError
from src.domain.entities import Message, Session
from src.domain.types import TaskType
from src.utils.degradation import log_degraded
from .gateway_ops import MemoryGatewayOps, _log_memory, _log_error
from .context_strategies import (
    TokenEstimator, SelectorConfig,
    RecencySelector, RelevanceSelector, HybridSelector, MythosInspiredSelector,
    BaseSelector
)
import json

# ── FASE 2 (plan_harness): SessionEventLog — evento por mensaje agregado ────
# Import seguro: si la infraestructura de eventos no está disponible, la
# memoria sigue funcionando sin log de sesión (degradación silenciosa).
try:
    from src.learning.session_event_log import get_session_event_log, SessionEventType
    _SESSION_EVENTS_AVAILABLE = True
except ImportError:
    _SESSION_EVENTS_AVAILABLE = False

# ✅ v0.6.9v (REFACTOR 50/50): capa de almacenamiento LanceDB (Palace) movida
# a src/memory/palace_storage.py. gateway.py queda como orquestador de capas.
# Re-exporta Palace/MEMPALACE_AVAILABLE/_PALACE_LANCE_AVAILABLE/get_embedding_model
# para compatibilidad interna (gateway los usa en _init_semantic/search_semantic).
from .palace_storage import (  # noqa: E402
    Palace,
    MEMPALACE_AVAILABLE,
    _PALACE_LANCE_AVAILABLE,
    get_embedding_model,
    EMBED_DIMENSION,
)

try:
    from .embedding_optimizer import EmbeddingOptimizer, get_embedding_optimizer
    OPTIMIZER_AVAILABLE = True
except ImportError:
    OPTIMIZER_AVAILABLE = False
    EmbeddingOptimizer = None
    get_embedding_optimizer = None

# ✅ Semana 1 A1: KnowledgeGraph — import seguro
try:
    from .knowledge_graph import KnowledgeGraph, KGNode, KGEdge
    KG_AVAILABLE = True
except ImportError:
    KG_AVAILABLE = False
    KnowledgeGraph = None
    KGNode = None
    KGEdge = None

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# plan_memory (Fase 1, 2026-08-29): mapeo TaskType → source_type.
# Se usa en el write (add_message) y en el read (get_context →
# expected_source_type) para el boost de relevancia. Agrupado por valor
# resultante; default "chat" para todo TaskType no listado.
# ═══════════════════════════════════════════════════════════════
TASK_TO_SOURCE: Dict[str, str] = {
    # code (11 CODE_* + CODE_SEARCH)
    **{t.value: "code" for t in TaskType if t.name.startswith("CODE_")},
    TaskType.CODE_SEARCH.value: "code",
    # search
    TaskType.WEB_SEARCH.value: "search",
    TaskType.NEWS_SEARCH.value: "search",
    TaskType.ACADEMIC_SEARCH.value: "search",
    TaskType.FILE_SEARCH.value: "search",  # ✅ plan_rafael (2026-08-31)
    # api / consultas
    TaskType.WEATHER_QUERY.value: "api",
    TaskType.FINANCE_QUERY.value: "api",
    TaskType.NEWS_QUERY.value: "api",
    TaskType.LOCATION_QUERY.value: "api",
    TaskType.TIME_QUERY.value: "api",
    # analysis
    TaskType.DATA_ANALYSIS.value: "analysis",
    TaskType.DOCUMENT_ANALYSIS.value: "analysis",
    TaskType.SENTIMENT_ANALYSIS.value: "analysis",
    TaskType.TREND_ANALYSIS.value: "analysis",
    TaskType.RISK_ANALYSIS.value: "analysis",
    # validation
    TaskType.FACT_CHECKING.value: "validation",
    TaskType.QUALITY_VALIDATION.value: "validation",
    TaskType.SECURITY_AUDIT.value: "validation",
    # vision
    TaskType.SCREENSHOT_ANALYSIS.value: "vision",
    TaskType.SCREEN_DESCRIBE.value: "vision",
    TaskType.CAMERA_DESCRIBE.value: "vision",
    # system
    TaskType.SYSTEM_CONTROL.value: "system",
    TaskType.APP_LAUNCH.value: "system",
    TaskType.VOLUME_CONTROL.value: "system",
    TaskType.SCREENSHOT.value: "system",
    # chat (default)
}

import re as _re  # v0.6.9c (M1): helper de fecha de sesión

_SESSION_DATE_RE = _re.compile(r"(20\d{6})")


def _session_date(session_id: Optional[str]) -> Optional[str]:
    """Extrae la fecha YYYYMMDD embebida en un session_id (20YYMMDD)."""
    if not session_id:
        return None
    m = _SESSION_DATE_RE.search(str(session_id))
    return m.group(1) if m else None


class MemoryGateway(MemoryGatewayOps):
    """
    Gateway de orquestación de 4 capas de memoria (Short-term/Deque, Long-term/SQLite, Semantic/LanceDB, KnowledgeGraph/SQLite).
    Implementa fallback automático entre capas para lectura y enriquecimiento híbrido (Vectorial + KG) para búsquedas semánticas.
    Optimiza generaciones de embeddings mediante caché L1/L2 vía EmbeddingOptimizer.

    ✅ v4.2.0: Paralelización de I/O y Fire-and-Forget para reducción de latencia.
    ✅ v4.3.0: Multi-scope tagging — agent_id propagado a metadata en long-term y semantic.
               Permite filtrar retrieval por agente sin cambios de schema SQL.
    """
    def __init__(
        self,
        session_id: Optional[str] = None,
        enable_short: bool = True,
        enable_long: bool = True,
        enable_semantic: bool = True,
        enable_optimizer: bool = True,
        enable_kg: bool = True,           # ✅ Semana 1: nueva capa
        agent_id: Optional[str] = None,   # ✅ v4.3.0: multi-scope tagging
        user_id: Optional[str] = None,    # ✅ Capa 2 (2026-08-31): identidad persistente
    ):
        start = time.time()
        self.session_id = session_id or str(uuid.uuid4())
        self.user_id = user_id  # ✅ Capa 2: aísla la memoria por usuario
        self.agent_id = agent_id  # propagado a metadata en cada write
        self.enable_short = enable_short
        self.enable_long = enable_long
        self.enable_semantic = enable_semantic
        self.enable_optimizer = enable_optimizer and OPTIMIZER_AVAILABLE
        self.enable_kg = enable_kg and KG_AVAILABLE
        _log_memory("gateway_init", "axioma_memory", session_id=self.session_id)

        # ✅ OPTIMIZACIÓN: Pool de hilos para I/O paralelo y Fire-and-Forget
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="axioma_mem")

        # ✅ v0.6.9c (M1): política de retención — poda de sesiones viejas en
        # background (settings.memory_retention_days, 0 = off). No bloquea el
        # init; se ejecuta una vez por gateway con la política activa.
        _retention = int(getattr(settings, "memory_retention_days", 0) or 0)
        if _retention > 0:
            self._submit(self._prune_old_sessions, _retention)

        self._short_term = None
        self._long_term = None
        self._semantic = None
        self._optimizer: Optional[Any] = None

        if self.enable_optimizer:
            try:
                self._optimizer = get_embedding_optimizer(
                    embedding_cache_size=1000,
                    embedding_cache_ttl=3600,
                    query_cache_size=500,
                    query_cache_ttl=300,
                    batch_size=32,
                    similarity_percentile=75,
                )
                logger.info("MemoryGateway: EmbeddingOptimizer habilitado")
            except Exception as e:
                logger.warning(f"MemoryGateway: EmbeddingOptimizer init failed: {e}")
                self.enable_optimizer = False

        # ✅ Semana 1 A1: KnowledgeGraph — 4ta capa
        self._kg: Optional[Any] = None
        if self.enable_kg:
            try:
                kg_path = Paths.DATA / "knowledge_graph.db"
                self._kg = KnowledgeGraph(db_path=kg_path)
                logger.info(f"MemoryGateway: KnowledgeGraph habilitado en {kg_path}")
            except Exception as e:
                logger.warning(f"MemoryGateway: KnowledgeGraph init failed: {e}")
                self._kg = None

        try:
            if enable_short:
                self._init_short_term()
            if enable_long:
                self._init_long_term()
            if enable_semantic:
                self._init_semantic()

            # ✅ v4.1.0: Inicializar componentes de ContextStrategies
            self._init_context_strategies()

            duration = time.time() - start
            _log_memory("gateway_init", "axioma_memory",
                        session_id=self.session_id, duration=duration, success=True)
            logger.info(f"MemoryGateway initialized: session={self.session_id}, kg={self._kg is not None}, strategies=enabled")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.__init__", extra={"session_id": self.session_id})
            _log_memory("gateway_init", "axioma_memory",
                        session_id=self.session_id, duration=duration, success=False)
            raise

    def _prune_old_sessions(self, days: int) -> None:
        """✅ v0.6.9c (M1): poda de mensajes/sesiones más viejos que `days`.

        Identifica la fecha por el session_id (prefijo `20YYMMDD`, ej.
        sesion_20260905 / nicegui_20260904_...) — no depende del formato
        del timestamp. Sesiones sin fecha en el id (coder_*, voice-*, tests)
        NO se tocan. Corre en background (executor), nunca bloquea init.
        """
        if days <= 0 or self._long_term is None:
            return
        try:
            db_path = getattr(self._long_term, "db_path", None)
            if not db_path:
                return
            import sqlite3 as _sqlite3
            from datetime import date as _date, timedelta as _timedelta

            cutoff = (_date.today() - _timedelta(days=days)).strftime("%Y%m%d")
            con = _sqlite3.connect(str(db_path), timeout=5)
            try:
                old_ids = [
                    r[0] for r in con.execute(
                        "SELECT DISTINCT session_id FROM messages"
                    ).fetchall()
                ]
                doomed = [
                    sid for sid in old_ids
                    if _session_date(sid) and _session_date(sid) < cutoff
                ]
                if not doomed:
                    return
                # borrar por lotes (evitar query gigante)
                for i in range(0, len(doomed), 200):
                    batch = doomed[i:i + 200]
                    marks = ",".join("?" * len(batch))
                    con.execute(
                        f"DELETE FROM messages WHERE session_id IN ({marks})",
                        batch,
                    )
                    con.execute(
                        f"DELETE FROM sessions WHERE session_id IN ({marks})",
                        batch,
                    )
                con.commit()
            finally:
                con.close()
            logger.info(
                f"[MEMORY] Retención: {len(doomed)} sesiones > {days} días "
                f"podadas (corte {cutoff})"
            )
            _log_memory("prune_retention", "long_term",
                        session_id=self.session_id,
                        items_count=len(doomed), success=True)
        except Exception as e:
            logger.warning(f"[MEMORY] Poda de retención falló (no crítico): {e}")

    def _init_short_term(self) -> None:
        start = time.time()
        try:
            from .short_term import ShortTermMemory
            self._short_term = ShortTermMemory(
                session_id=self.session_id,
                max_messages=settings.max_context_messages if hasattr(settings, 'max_context_messages') else 50,
                ttl_seconds=settings.session_timeout if hasattr(settings, 'session_timeout') else 3600,
            )
            _log_memory("init_short_term", "short_term",
                        session_id=self.session_id, duration=time.time() - start, success=True)
            logger.debug("Short-term memory initialized")
        except Exception as e:
            _log_error(e, "MemoryGateway._init_short_term", extra={"session_id": self.session_id})
            _log_memory("init_short_term", "short_term",
                        session_id=self.session_id, duration=time.time() - start, success=False)
            logger.warning(f"Failed to init short-term memory: {e}")
            self._short_term = None

    def _init_long_term(self) -> None:
        start = time.time()
        try:
            from .long_term import LongTermMemory
            db_path = Paths.MEMORY / "axioma.db"
            self._long_term = LongTermMemory(db_path=db_path)
            _log_memory("init_long_term", "long_term",
                        session_id=self.session_id, duration=time.time() - start,
                        success=True, db_path=str(db_path))
            logger.debug("Long-term memory initialized")
        except Exception as e:
            _log_error(e, "MemoryGateway._init_long_term", extra={"session_id": self.session_id})
            _log_memory("init_long_term", "long_term",
                        session_id=self.session_id, duration=time.time() - start, success=False)
            logger.warning(f"Failed to init long-term memory: {e}")
            self._long_term = None

    def _init_semantic(self) -> None:
        start = time.time()
        try:
            if not MEMPALACE_AVAILABLE or Palace is None:
                # ✅ FIX AUDITORÍA: decía "pip install mempalace>=3.1.0" — resabio de
                # una dependencia anterior a la migración a LanceDB. La dependencia
                # real es lancedb (mismo mensaje que usa Palace.__init__ más abajo).
                raise ImportError("LanceDB no disponible. Ejecuta: pip install lancedb pyarrow")

            safe_session_id = self._sanitize_session_id(self.session_id)
            palace_path = Paths.MEMORY / "mempalace"
            palace_path.mkdir(parents=True, exist_ok=True)

            self._semantic = Palace(
                path=str(palace_path),
                wing_name=f"axioma_{safe_session_id}",
                room_name="conversation",
                hall_name="semantic",
            )
            _log_memory("init_semantic", "semantic",
                        session_id=self.session_id, duration=time.time() - start,
                        success=True, db_path=str(palace_path))
            logger.debug(f"Semantic memory initialized: mempalace wing=axioma_{safe_session_id}")
        except Exception as e:
            _log_error(e, "MemoryGateway._init_semantic", extra={"session_id": self.session_id})
            _log_memory("init_semantic", "semantic",
                        session_id=self.session_id, duration=time.time() - start, success=False)
            logger.warning(f"Failed to init semantic memory: {e}")
            self._semantic = None

    # ── ✅ v4.1.0: Inicialización de Estrategias de Contexto ─────────────────
    def _init_context_strategies(self) -> None:
        self._estimator = TokenEstimator()
        self._sel_config = SelectorConfig()

        self._recency_sel = RecencySelector(self._estimator)
        self._relevance_sel = RelevanceSelector(
            estimator=self._estimator,
            optimizer=self._optimizer,
            config=self._sel_config,
            fallback=self._recency_sel
        )
        self._hybrid_sel = HybridSelector(
            estimator=self._estimator,
            relevance_selector=self._relevance_sel,
            recency_selector=self._recency_sel,
            config=self._sel_config
        )
        self._mythos_sel = MythosInspiredSelector(
            hybrid_selector=self._hybrid_sel
        )
        self._strategy_cache: Dict[str, BaseSelector] = {}

    def _get_strategy_for_task(self, task_type: str) -> BaseSelector:
        if task_type in self._strategy_cache:
            return self._strategy_cache[task_type]

        if task_type.startswith("code_"):
            strategy = self._hybrid_sel
        elif task_type in (
            TaskType.WEB_SEARCH.value, TaskType.NEWS_QUERY.value,
            TaskType.WEATHER_QUERY.value, TaskType.FINANCE_QUERY.value,
            TaskType.DATA_ANALYSIS.value, TaskType.DOCUMENT_ANALYSIS.value,
            # ✅ plan_memory (2026-08-29): los analysis faltantes caían al else
            # → RecencySelector, no RelevanceSelector (mismo bug que analyst.py).
            TaskType.SENTIMENT_ANALYSIS.value, TaskType.TREND_ANALYSIS.value,
            TaskType.RISK_ANALYSIS.value,
        ):
            strategy = self._relevance_sel
        elif task_type == TaskType.GENERAL_CHAT.value:
            strategy = self._mythos_sel
        else:
            strategy = self._recency_sel

        self._strategy_cache[task_type] = strategy
        logger.debug(f"Strategy resolved and cached for {task_type}: {strategy.__class__.__name__}")
        return strategy

    # ── Contexto ─────────────────────────────────────────────────────────────
    def get_context(
        self,
        limit: int = 10,
        include_system: bool = True,
        task_type: Optional[str] = None,
        query: Optional[str] = None,
        max_tokens: int = 2048
    ) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("get_context", "axioma_sessions", session_id=self.session_id)
        try:
            raw_messages = []
            if self._short_term is not None:
                try:
                    raw_messages = self._short_term.get_recent(limit * 3)
                except Exception as e:
                    logger.error(f"Short-term get_context failed: {e}")

            if not raw_messages and self._long_term is not None:
                try:
                    raw_messages = self._long_term.get_recent(
                        session_id=self.session_id, limit=limit * 3,
                    )
                except Exception as e:
                    logger.error(f"Long-term get_context failed: {e}")

            if not include_system:
                raw_messages = [m for m in raw_messages if m.get("role") != "system"]

            # ✅ Capa 2 (2026-08-31): el bloque de estrategias + inyección
            # cross-sesión va ANTES del early-return de ventana vacía — una
            # sesión nueva (sin historial) es justo el caso que cross-sesión
            # debe resolver (inyectar contexto relevante de otras sesiones).
            if task_type and query:
                strategy = self._get_strategy_for_task(task_type)
                # ✅ plan_memory (Fase 1): source_type esperado para el
                # task_type actual → boost de relevancia en los Selectors.
                expected_source_type = TASK_TO_SOURCE.get(task_type, "chat")
                selected_messages = strategy.select(
                    messages=raw_messages,
                    query=query,
                    max_tokens=max_tokens,
                    expected_source_type=expected_source_type,
                )
                # ✅ Capa 2 (2026-08-31): inyección cross-sesión — mensajes
                # relevantes de OTRAS sesiones del mismo usuario.
                selected_messages = self._inject_cross_session(
                    selected_messages, query, max_tokens, task_type=task_type,
                )
                _log_memory("get_context", f"strategy_{strategy.__class__.__name__}",
                            session_id=self.session_id, items_count=len(selected_messages),
                            duration=time.time() - start, success=True)
                return selected_messages

            if not raw_messages:
                return []

            # ✅ ISSUE-130: se piden `limit * 3` mensajes (margen para el filtro
            # de `system`) y acá hay que quedarse con los ÚLTIMOS `limit`. Antes
            # era `raw_messages[:limit]`: en una sesión de 30 mensajes con
            # limit=6 devolvía m13..m18 en vez de m25..m30 — o sea el tercio más
            # VIEJO de la ventana reciente. Medido con un almacén simulado antes
            # de tocar el código; afectaba a los agentes que piden contexto
            # (coder/researcher/analyst), que recibían lo viejo en lugar de lo
            # último.
            _log_memory("get_context", "recency_default",
                        session_id=self.session_id, items_count=len(raw_messages[-limit:]),
                        duration=time.time() - start, success=True)
            return raw_messages[-limit:] if limit > 0 else []

        except Exception as e:
            _log_error(e, "MemoryGateway.get_context", extra={"limit": limit})
            raise

    # ── Semántica + KG ───────────────────────────────────────────────────────
    def _inject_cross_session(
        self,
        selected: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        ✅ Capa 2 (2026-08-31): retrieval cross-sesión.

        Inyecta mensajes RELEVANTES de otras sesiones del MISMO usuario
        (vía search_semantic → Palace, con multi-query integrado). Solo actúa
        cuando el gateway tiene user_id y hay query; sin user_id, sin hits o
        ante cualquier error → devuelve la ventana tal cual (sin regresión).
        Cada hit se etiqueta metadata["source"]="cross_session" y se respeta
        el presupuesto de tokens (max_tokens) con TokenEstimator.
        """
        # ✅ ISSUE-143 (MEDIDO 2026-10-04): las tareas de CÓDIGO no llevan contexto de
        # otras conversaciones. Con el pedido real del usuario (leer su propio pcap),
        # el pedido del keylogger de otra sesión entraba en el **puesto 2** (parecido
        # 0,582) y con el umbral 0,5 + `cross_session_limit=3` se inyectaba ⇒ el modelo
        # continuaba el tema VIEJO y el artefacto entregado era `keylogger.py`
        # (la validación lo daba por bueno: validaba que funcionara, no qué se pidió).
        # El contexto del código se gana con el pedido, no con la conversación vecina.
        if (str(task_type or "").startswith("code")
                and not getattr(settings, "cross_session_en_codigo", False)):
            _log_memory("get_context", "sin_cross_session_por_codigo",
                        session_id=self.session_id, items_count=len(selected),
                        success=True)
            return selected

        if not self.user_id or not query or self._semantic is None:
            return selected
        estimator = getattr(self, "_estimator", None)
        try:
            limit = int(getattr(settings, "cross_session_limit", 3))
            min_sim = float(getattr(settings, "cross_session_min_similarity", 0.5))
            if limit <= 0:
                return selected
            hits = self.search_semantic(
                query, limit=limit, min_similarity=min_sim, user_id=self.user_id,
            )
        except Exception as e:
            log_degraded(logger, e, "src/memory/gateway.py:_inject_cross_session")
            return selected
        if not hits:
            return selected

        existing = {m.get("content", "") for m in selected}
        used_tokens = estimator.estimate_messages(selected) if estimator else 0
        added: List[Dict[str, Any]] = []
        for hit in hits:
            content = hit.get("text", "")
            if not content or content in existing:
                continue
            msg: Dict[str, Any] = {
                "role": "user",
                "content": content,
                "metadata": {
                    **(hit.get("metadata") or {}),
                    "source": "cross_session",
                },
            }
            if estimator is not None:
                msg_tokens = estimator.estimate_message(msg)
                if used_tokens + msg_tokens > max_tokens:
                    break
                used_tokens += msg_tokens
            existing.add(content)
            added.append(msg)
        if added:
            logger.debug(
                f"[Memory] cross-session: {len(added)} mensajes de otras "
                f"sesiones del usuario {self.user_id!r} inyectados"
            )
        return selected + added

    def _multi_query_embed_batch(self, texts: List[str]):
        """
        ✅ v0.6.8: Batch de embeddings para multi-query ligero.

        Reutiliza el EmbeddingOptimizer (caché L1 + batch generator) cuando
        está disponible; si no, cae al singleton BGE-M3 directo.
        """
        try:
            model = get_embedding_model()
            if model is None:
                return None
            if self._optimizer:
                return self._optimizer.get_embeddings_batch(
                    texts=texts,
                    model_fn=lambda ts: (
                        model.embed_batch(ts) if model is not None else None
                    ),
                    model_name="BAAI/bge-m3",
                    use_cache=True,
                )
            return model.embed_batch(texts)
        except Exception as e:
            logger.warning(f"[MultiQuery] embed batch fallback failed: {e}")
            return None

    def enrich_with_kg(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if not self._kg:
            return []
        try:
            words = [w for w in query.lower().split() if len(w) > 3]
            results = []
            seen_ids = set()
            for word in words[:3]:
                nodes = self._kg.search_nodes(label_pattern=word, limit=limit)
                for node in nodes:
                    if node.node_id in seen_ids:
                        continue
                    seen_ids.add(node.node_id)
                    related = self._kg.query_related(node.node_id, limit=3)
                    results.append({"node": node.to_dict(), "related": related})
            return results[:limit]
        except Exception as e:
            logger.warning(f"KG enrich error: {e}")
            return []

    def search_semantic(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.5,
        use_optimizer: bool = True,
        user_id: Optional[str] = None,  # ✅ Capa 2: filtrar por usuario
    ) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("search_semantic", "semantic", session_id=self.session_id)
        if self._semantic is None:
            logger.warning("Semantic memory not enabled")
            _log_memory("search_semantic", "semantic",
                        session_id=self.session_id, success=False, duration=0)
            return []
        # ✅ Capa 2: filtro por usuario (param explícito o el del gateway).
        user_filter = user_id if user_id is not None else self.user_id
        try:
            # ✅ OPTIMIZACIÓN: Pre-calcular embedding para inyectarlo y evitar doble cómputo en LanceDB
            query_emb = None
            if use_optimizer and self._optimizer:
                try:
                    # ✅ FIX AUDITORÍA: antes chequeaba hasattr(self._semantic,
                    # '_get_embedding') — un método que Palace (LanceDB) nunca tuvo,
                    # resabio de la clase anterior basada en ChromaDB. Esto hacía que
                    # model_fn devolviera None SIEMPRE en cache miss, y el optimizer
                    # nunca calculaba ni cacheaba un embedding real acá (Palace
                    # terminaba recalculándolo internamente en recall() — el "evitar
                    # doble cómputo" de este comentario nunca se cumplía).
                    query_emb = self._optimizer.get_embedding(
                        text=query,
                        model_fn=lambda t: (
                            get_embedding_model().embed(t)
                            if get_embedding_model() is not None else None
                        ),
                        model_name="BAAI/bge-m3",
                        use_cache=True,
                    )
                except Exception as _d2e:
                    log_degraded(logging.getLogger(__name__), _d2e, "src/memory/gateway.py:search_semantic")

            # ✅ v0.6.8 MULTI-QUERY LIGERO: si está habilitado y la query es
            # suficientemente larga (mismo criterio que L3), la búsqueda
            # semántica usa expansión determinística + batch BGE-M3 + RRF.
            # El costo extra es de +0-200ms (sin LLM) — ver src/memory/multi_query.py.
            _multi_enabled = (
                getattr(settings, "multi_query_enabled", True)
                and len(query.strip()) >= getattr(settings, "multi_query_min_length", 30)
                and self._semantic is not None
            )

            def search_fn():
                if _multi_enabled:
                    try:
                        from src.memory.multi_query import multi_query_search
                        return multi_query_search(
                            query=query,
                            search_fn=lambda variant_text, variant_emb: self._semantic.recall(
                                query=variant_text,
                                query_embedding=variant_emb,
                                limit=limit,
                                min_score=min_similarity,
                                user_id=user_filter,  # ✅ Capa 2
                            ),
                            embed_batch_fn=self._multi_query_embed_batch,
                            max_variants=getattr(settings, "multi_query_max_variants", 4),
                            limit=limit,
                            min_similarity=min_similarity,
                            rrf_k=getattr(settings, "multi_query_rrf_k", 60),
                        )
                    except Exception as e:
                        logger.warning(f"Multi-query search failed, falling back: {e}")
                raw = self._semantic.recall(
                    query=query, query_embedding=query_emb, limit=limit,
                    min_score=min_similarity, user_id=user_filter,  # ✅ Capa 2
                )
                return [
                    {
                        "text": r.get("text", ""),
                        # ✅ FIX BUG: Era r.get("score"), lo cual siempre daba 0.0
                        "similarity": r.get("similarity", 0.0),
                        "metadata": r.get("metadata", {}),
                    }
                    for r in raw
                ]

            # ✅ OPTIMIZACIÓN: Búsqueda paralela Semantic + KG
            if self._kg:
                future_semantic = self._submit(search_fn)
                future_kg = self._submit(self.enrich_with_kg, query, limit)

                results = []
                try:
                    results = future_semantic.result(timeout=10.0)
                except Exception as e:
                    logger.error(f"Parallel semantic search failed: {e}")

                if len(results) < limit:
                    try:
                        kg_results = future_kg.result(timeout=5.0)
                        results.extend(kg_results)
                    except Exception as e:
                        logger.error(f"Parallel KG search failed: {e}")
            else:
                results = search_fn()

            # ✅ v0.6.9r (3a): re-rank híbrido BM25 sobre el top-k semántico.
            # Sume similitud semántica + coincidencia léxica (sin modelos;
            # mejora grounding). OFF por default — activar con
            # settings.hybrid_bm25_enabled = true tras medir.
            if getattr(settings, "hybrid_bm25_enabled", False) and results:
                try:
                    from src.utils.bm25 import bm25_scores
                    texts = [r.get("text", "") for r in results]
                    bm = bm25_scores(query, texts)
                    mx = max(bm) if bm else 0.0
                    w = float(getattr(settings, "hybrid_bm25_weight", 0.5))
                    for r, score in zip(results, bm):
                        lex = score / mx if mx > 0 else 0.0
                        r["similarity"] = round(
                            (1.0 - w) * float(r.get("similarity", 0.0))
                            + w * lex, 4)
                    results.sort(key=lambda r: r["similarity"], reverse=True)
                except Exception as _bm25_err:
                    log_degraded(logging.getLogger(__name__), _bm25_err, "src/memory/gateway.py:search_semantic")

            duration = time.time() - start
            _log_memory("search_semantic", "semantic",
                        session_id=self.session_id, items_count=len(results),
                        duration=duration, success=True)
            logger.debug(f"Semantic search: {len(results)} results for '{query[:50]}'")
            return results
        except Exception as e:
            _log_error(e, "MemoryGateway.search_semantic", extra={"query": query[:50]})
            _log_memory("search_semantic", "semantic",
                        session_id=self.session_id, duration=time.time() - start, success=False)
            logger.error(f"Semantic search failed: {e}")
            return []

    # ── Mensajes ─────────────────────────────────────────────────────────────
    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        use_optimizer: bool = True,
        source_type: str = "chat",  # ✅ plan_memory (Fase 1): etiqueta de origen
    ) -> bool:
        start = time.time()
        _log_memory("add_message", "axioma_sessions", session_id=self.session_id)
        success = False
        timestamp = time.time()
        # ✅ plan_memory (Fase 1): un SOLO merge de metadata con source_type,
        # propagado a las 3 capas (antes había 3 merges separados).
        metadata_full = {**(metadata or {}), "source_type": source_type}
        if self.user_id:  # ✅ Capa 2: etiquetar con identidad persistente
            metadata_full["user_id"] = self.user_id
        try:
            # Operaciones críticas síncronas (necesarias para contexto inmediato)
            if self._short_term is not None:
                try:
                    add_result = self._short_term.add(role, content, metadata_full)
                    if add_result:
                        success = True
                        logger.debug(f"Short-term add OK: role={role}")
                    else:
                        logger.warning(f"Short-term add returned False: role={role}")
                except Exception as e:
                    # ✅ v0.6.8t: el error real iba solo al terminal (logger.error);
                    # _log_error lo registra en reg_error.txt con traceback.
                    _log_error(e, "MemoryGateway.add_message.short_term",
                               extra={"role": role, "content_len": len(content)})
                    logger.error(f"Short-term add failed: {e}")
            if self._long_term is not None:
                try:
                    _lt_meta = dict(metadata_full)
                    if self.agent_id:
                        _lt_meta["agent_id"] = self.agent_id
                    message_id = self._long_term.add(
                        session_id=self.session_id, role=role,
                        content=content, metadata=_lt_meta,
                    )
                    if message_id and message_id > 0:
                        success = True
                        logger.debug(f"Long-term add OK: role={role}, id={message_id}")
                    else:
                        logger.warning(f"Long-term add invalid id: {message_id}")
                except Exception as e:
                    # ✅ v0.6.8t: ídem — diagnosticable en reg_error.txt.
                    _log_error(e, "MemoryGateway.add_message.long_term",
                               extra={"role": role, "content_len": len(content)})
                    logger.error(f"Long-term add failed: {e}")

            # ✅ OPTIMIZACIÓN: Fire-and-Forget para escritura en Semantic
            # No bloquea al agente, el contexto ya está en Short-term.
            # ✅ v0.6.8nn (plan adaptado F2): también se indexan las respuestas
            # del assistant → la memoria semántica recuerda el diálogo completo.
            if self._semantic is not None and role in ("user", "assistant"):
                def _bg_semantic_store(text, meta, ts):
                    try:
                        embedding = None
                        if use_optimizer and self._optimizer:
                            # ✅ FIX AUDITORÍA: mismo resabio de ChromaDB que en
                            # search_semantic() — ver comentario ahí.
                            embedding = self._optimizer.get_embedding(
                                text=text,
                                model_fn=lambda t: (
                                    get_embedding_model().embed(t)
                                    if get_embedding_model() is not None else None
                                ),
                                model_name="BAAI/bge-m3",
                                use_cache=True,
                            )
                        _sem_meta = {
                            "role": role,
                            "session_id": self.session_id,
                            "timestamp": ts,
                            **(meta or {}),
                        }
                        if self.agent_id:
                            _sem_meta["agent_id"] = self.agent_id
                        self._semantic.store(
                            text=text,
                            metadata=_sem_meta,
                            embedding=embedding,
                        )
                    except Exception as e:
                        logger.error(f"Background Semantic add failed: {e}")

                self._submit(_bg_semantic_store, content, metadata_full, timestamp)
                success = True # Asumimos éxito para no bloquear

            duration = time.time() - start
            # ✅ v0.6.8t: diagnóstico del estado de capas cuando falla — el
            # ✗ salía sin contexto (solo success=False en el summary). Los
            # errores de capa ya se capturan con _log_error (reg_error.txt).
            if not success:
                logger.warning(
                    f"add_message no persistió: role={role}, content_len={len(content)}, "
                    f"short={self._short_term is not None}, long={self._long_term is not None}"
                )
            _log_memory("add_message", "axioma_sessions",
                        session_id=self.session_id, items_count=1,
                        duration=duration, success=success,
                        db_path=str(Paths.MEMORY / "axioma.db"))
            if success:
                logger.debug(f"Message added: role={role}, len={len(content)}")
                # ✅ FASE 2 (plan_harness): evento del log de sesión —
                # replay del historial sin depender de la memoria viva.
                self._log_session_event(role=role, content=content, metadata=metadata)
            return success
        except Exception as e:
            _log_error(e, "MemoryGateway.add_message",
                       extra={"role": role, "content_len": len(content)})
            _log_memory("add_message", "axioma_sessions",
                        session_id=self.session_id, success=False, duration=time.time() - start)
            raise

    # ── FASE 2 (plan_harness): SessionEventLog ─────────────────────────────
    def _log_session_event(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Registra el mensaje en el log de eventos de la sesión. Silencioso si no disponible."""
        if not _SESSION_EVENTS_AVAILABLE:
            return
        try:
            log = get_session_event_log()
            if log is None:
                return
            log.append(
                session_id=self.session_id,
                event_type=SessionEventType.MESSAGE_ADDED,
                payload={
                    "role": role,
                    "chars": len(content or ""),
                    "content_preview": (content or "")[:200],
                    "metadata": metadata or {},
                },
            )
        except Exception as e:
            log_degraded(logger, e, "src/memory/gateway.py:_log_session_event")

    # ── Resumen / Export ─────────────────────────────────────────────────────
    def get_session_summary(self) -> Dict[str, Any]:
        start = time.time()
        _log_memory("get_session_summary", "axioma_sessions", session_id=self.session_id)
        summary = {
            "session_id": self.session_id,
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "system_messages": 0,
            "created_at": None,
            "updated_at": None,
        }
        try:
            # ✅ OPTIMIZACIÓN: Consultas paralelas de estadísticas
            futures = {}
            if self._short_term is not None:
                futures['st'] = self._submit(self._short_term.get_stats)
            if self._long_term is not None:
                futures['lt'] = self._submit(self._long_term.get_session_stats, self.session_id)
            if self._optimizer:
                futures['opt'] = self._submit(self._optimizer.get_stats)
            if self._kg:
                futures['kg'] = self._submit(self._kg.get_stats)

            if 'st' in futures:
                try:
                    stats = futures['st'].result(timeout=5.0)
                    summary["message_count"] = stats.get("total", 0)
                    summary["updated_at"] = stats.get("last_updated")
                except Exception as e:
                    log_degraded(logger, e, "MemoryGateway.get_session_summary (short_term)",
                                 contract_level=logging.DEBUG)
            if 'lt' in futures:
                try:
                    stats = futures['lt'].result(timeout=5.0)
                    summary["message_count"] = max(summary["message_count"], stats.get("total", 0))
                    summary["user_messages"] = stats.get("user", 0)
                    summary["assistant_messages"] = stats.get("assistant", 0)
                    summary["system_messages"] = stats.get("system", 0)
                    summary["created_at"] = stats.get("created_at")
                    summary["updated_at"] = stats.get("updated_at")
                except Exception as e:
                    log_degraded(logger, e, "MemoryGateway.get_session_summary (long_term)",
                                 contract_level=logging.DEBUG)
            if 'opt' in futures:
                try:
                    opt_stats = futures['opt'].result(timeout=5.0)
                    summary["optimizer"] = {
                        "embedding_cache_hit_rate": opt_stats.get("embedding_cache", {}).get("hit_rate", 0),
                        "query_cache_hit_rate": opt_stats.get("query_cache", {}).get("hit_rate", 0),
                        "total_operations": opt_stats.get("total_operations", 0),
                    }
                except Exception as e:
                    log_degraded(logger, e, "MemoryGateway.get_session_summary (optimizer)",
                                 contract_level=logging.DEBUG)
            if 'kg' in futures:
                try:
                    summary["kg"] = futures['kg'].result(timeout=5.0)
                except Exception as e:
                    log_degraded(logger, e, "MemoryGateway.get_session_summary (knowledge_graph)",
                                 contract_level=logging.DEBUG)

            summary["strategy_cache_size"] = len(self._strategy_cache)

            duration = time.time() - start
            _log_memory("get_session_summary", "axioma_sessions",
                        session_id=self.session_id, duration=duration, success=True)
            return summary
        except Exception as e:
            _log_error(e, "MemoryGateway.get_session_summary",
                       extra={"session_id": self.session_id})
            raise

    # ── Clear / Export / Close ───────────────────────────────────────────────
    def clear(self, keep_system: bool = False) -> bool:
        start = time.time()
        _log_memory("clear", "axioma_sessions", session_id=self.session_id)
        success = True
        try:
            if self._short_term is not None:
                try: self._short_term.clear(keep_system=keep_system)
                except Exception as e: logger.error(f"Short-term clear failed: {e}"); success = False
            if self._long_term is not None:
                try: self._long_term.clear_session(self.session_id)
                except Exception as e: logger.error(f"Long-term clear failed: {e}"); success = False
            if self._semantic is not None:
                try: self._semantic.clear()
                except Exception as e: logger.error(f"Semantic clear failed: {e}"); success = False
            if self._optimizer:
                try: self._optimizer.reset()
                except Exception as e: logger.warning(f"Optimizer clear failed: {e}")
            self._strategy_cache.clear()

            duration = time.time() - start
            _log_memory("clear", "axioma_sessions",
                        session_id=self.session_id, duration=duration, success=success)
            if success:
                logger.info(f"Memory cleared for session {self.session_id}")
            return success
        except Exception as e:
            _log_error(e, "MemoryGateway.clear", extra={"session_id": self.session_id})
            raise

    def export_session(self) -> Dict[str, Any]:
        start = time.time()
        _log_memory("export_session", "axioma_sessions", session_id=self.session_id)
        try:
            result = {
                "session_id": self.session_id,
                "messages": self.get_context(limit=1000),
                "summary": self.get_session_summary(),
                "exported_at": time.time(),
            }
            _log_memory("export_session", "axioma_sessions",
                        session_id=self.session_id,
                        duration=time.time() - start, success=True)
            return result
        except Exception as e:
            _log_error(e, "MemoryGateway.export_session",
                       extra={"session_id": self.session_id})
            raise

    def get_optimizer_stats(self) -> Optional[Dict[str, Any]]:
        if not self._optimizer:
            return None
        return self._optimizer.get_stats()

    def clear_optimizer_caches(self) -> bool:
        if not self._optimizer:
            return False
        try:
            self._optimizer.reset()
            logger.debug("EmbeddingOptimizer caches cleared")
            return True
        except Exception as e:
            logger.warning(f"Optimizer cache clear failed: {e}")
            return False

    def _submit(self, fn, *args, **kwargs):
        """Envía una tarea al ThreadPoolExecutor de forma segura.

        ✅ v0.6.9v (ISSUE-002): guard contra un executor ya apagado. Durante
        el cierre del proceso, `self._executor.shutdown()` puede ejecutarse
        mientras quedan submits en cola → "cannot schedule new futures after
        interpreter shutdown" (ruido espurio en los logs). Si el executor está
        cerrado, degrada silenciosamente sin romper el request.
        """
        ex = getattr(self, "_executor", None)
        if ex is None:
            return None
        try:
            return ex.submit(fn, *args, **kwargs)
        except (RuntimeError, ValueError):
            # Executor apagado o en shutdown → degradar sin romper.
            logger.debug("[MemoryGateway] _submit degradado: executor apagado")
            return None
        except Exception as e:
            # ✅ ISSUE-019: un error de contrato acá (p. ej. `fn` mal llamada)
            # perdería un write/read de memoria en silencio → WARNING.
            from src.utils.degradation import log_degraded
            log_degraded(logger, e, "MemoryGateway._submit")
            return None

    def close(self) -> None:
        start = time.time()
        _log_memory("close", "axioma_sessions", session_id=self.session_id)
        try:
            if self._long_term is not None:
                try: self._long_term.close()
                except Exception as e: logger.error(f"Long-term close failed: {e}")
            if self._semantic is not None:
                try: self._semantic.close()
                except Exception as e: logger.error(f"Semantic close failed: {e}")
            if self._optimizer:
                try: self._optimizer.reset()
                except Exception as e: logger.warning(f"Optimizer close failed: {e}")

            # ✅ Apagar el ThreadPoolExecutor limpiamente
            if hasattr(self, '_executor'):
                self._executor.shutdown(wait=True)

            _log_memory("close", "axioma_sessions",
                        session_id=self.session_id,
                        duration=time.time() - start, success=True)
            logger.info("MemoryGateway closed")
        except Exception as e:
            _log_error(e, "MemoryGateway.close", extra={"session_id": self.session_id})
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
