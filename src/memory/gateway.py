#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — MemoryGateway: Gateway Unificado de Memoria
# Ruta: src/memory/gateway.py
# Autor: SaintWick
# Versión: 0.4.0 (REFACTORIZACIÓN 50/50 — 2026-10-22)
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
#   v0.4.0 — ✅ REFACTORIZACIÓN 50/50 (2026-10-22): la mitad de INICIALIZACIÓN
#            DE CAPAS + LECTURA (init short/long/semantic, estrategias de
#            contexto, get_context, inyección cross-sesión, search_semantic,
#            enrich_with_kg, poda de retención y TASK_TO_SOURCE/_session_date)
#            vive en el hermano `src/memory/gateway_retrieval.py`
#            (MemoryGatewayRetrievalMixin). Acá quedan __init__ y el ciclo de
#            escritura/vida (add_message, resumen, clear/export, stats,
#            _submit, close). El original RE-IMPORTA los helpers movidos y la
#            clase hereda del mixin ⇒ API pública intacta.
# ═══════════════════════════════════════════════════════════════
import logging
import time
import uuid
from typing import Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from config.settings import settings
from config.paths import Paths
from src.utils.degradation import log_degraded
from .gateway_ops import MemoryGatewayOps, _log_memory, _log_error

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
from .palace_storage import (  # noqa: E402, F401 — re-export: API pública intacta
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

# ✅ Refactor 50/50 (2026-10-22): helpers de módulo movidos al hermano. Se
# re-importan con los mismos nombres ⇒ API pública intacta (gateway.TASK_TO_SOURCE,
# gateway._session_date) y `MemoryGateway` hereda del mixin de lectura.
from .gateway_retrieval import (  # noqa: E402, F401 — re-export: API pública intacta
    MemoryGatewayRetrievalMixin,
    TASK_TO_SOURCE,
    _SESSION_DATE_RE,
    _session_date,
)


class MemoryGateway(MemoryGatewayRetrievalMixin, MemoryGatewayOps):
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
        _retention = int(settings.memory_retention_days or 0)
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
