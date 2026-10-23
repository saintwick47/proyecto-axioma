#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — gateway_retrieval.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/memory/gateway_retrieval.py
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de INICIALIZACIÓN DE CAPAS +
#   LECTURA se extrae de `src/memory/gateway.py` a este hermano, como
#   `MemoryGatewayRetrievalMixin`. MemoryGateway hereda de este mixin y
#   re-importa los helpers de módulo ⇒ la API pública NO cambia.
#
#   Qué se movió: `_prune_old_sessions` (+ `_session_date`/`_SESSION_DATE_RE`),
#   `_init_short_term`, `_init_long_term`, `_init_semantic`,
#   `_init_context_strategies`, `_get_strategy_for_task`, `get_context`,
#   `_inject_cross_session`, `_multi_query_embed_batch`, `enrich_with_kg`,
#   `search_semantic` y el mapeo `TASK_TO_SOURCE`. Quedan en el original
#   `__init__`, `add_message`, `_log_session_event`, `get_session_summary`,
#   `clear`, `export_session`, los stats/`_submit`/`close` y las capas ops.
#
#   Es HOJA: **no** importa el original a nivel de módulo (sin AUD-CIR-001).
#   Solo `_multi_query_embed_batch` y `search_semantic` lo leen DIFERIDO
#   (`import src.memory.gateway as _orig`) para resolver `get_embedding_model`
#   por el módulo ORIGINAL: un test lo parchea allí y el monkeypatch debe
#   llegar al mismo objeto (no se construye un modelo real por accidente).
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, List, Dict, Any

from config.settings import settings
from config.paths import Paths
from src.domain.types import TaskType
from src.utils.degradation import log_degraded

from .gateway_ops import _log_memory, _log_error
from .context_strategies import (
    TokenEstimator, SelectorConfig,
    RecencySelector, RelevanceSelector, HybridSelector, MythosInspiredSelector,
    BaseSelector,
)
from .palace_storage import Palace, MEMPALACE_AVAILABLE

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


class MemoryGatewayRetrievalMixin:
    """Mitad de INICIALIZACIÓN y LECTURA de MemoryGateway (refactor 50/50).

    Inicializa las capas short/long/semantic, las estrategias de contexto y
    expone `get_context` (con inyección cross-sesión), `search_semantic`,
    `enrich_with_kg`, `_multi_query_embed_batch` y la poda de retención.
    Movido tal cual desde `src/memory/gateway.py`; `MemoryGateway` hereda de
    este mixin, así que la API pública queda intacta.

    Hoja del grafo: no importa `src.memory.gateway` a nivel de módulo.
    """

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
                max_messages=settings.max_context_messages,
                ttl_seconds=settings.session_timeout,
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
                and not settings.cross_session_en_codigo):
            _log_memory("get_context", "sin_cross_session_por_codigo",
                        session_id=self.session_id, items_count=len(selected),
                        success=True)
            return selected

        if not self.user_id or not query or self._semantic is None:
            return selected
        estimator = getattr(self, "_estimator", None)
        try:
            limit = int(settings.cross_session_limit)
            min_sim = float(settings.cross_session_min_similarity)
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
        import src.memory.gateway as _orig
        try:
            model = _orig.get_embedding_model()
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
        import src.memory.gateway as _orig
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
                            _orig.get_embedding_model().embed(t)
                            if _orig.get_embedding_model() is not None else None
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
                settings.multi_query_enabled
                and len(query.strip()) >= settings.multi_query_min_length
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
                            max_variants=settings.multi_query_max_variants,
                            limit=limit,
                            min_similarity=min_similarity,
                            rrf_k=settings.multi_query_rrf_k,
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
            if settings.hybrid_bm25_enabled and results:
                try:
                    from src.utils.bm25 import bm25_scores
                    texts = [r.get("text", "") for r in results]
                    bm = bm25_scores(query, texts)
                    mx = max(bm) if bm else 0.0
                    w = float(settings.hybrid_bm25_weight)
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
