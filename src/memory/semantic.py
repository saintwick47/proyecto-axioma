#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.3 — Memoria Semántica (LanceDB)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/semantic.py
# Autor: SaintWick
# v0.6.3: Migración ChromaDB → LanceDB (lancedb==0.33.0)
#         API pública idéntica: add/search/search_by_session/get_stats
#         /clear/delete_by_metadata/export_collection/close/__len__/__bool__
#         Métrica cosine: similarity = 1.0 - _distance (consistente con ChromaDB)
#         to_list() para acceder a _distance correctamente
#         session_id como columna top-level para filtrado eficiente
# ═══════════════════════════════════════════════════════════════
import json
import logging
import time
from typing import Optional, List, Dict, Any
from pathlib import Path
import hashlib
import re
import threading
from collections import OrderedDict
from config.paths import Paths
from src.domain.exceptions import MemoryError

try:
    import lancedb
    from lancedb.pydantic import LanceModel, Vector
    LANCEDB_AVAILABLE = True
except ImportError as e:
    LANCEDB_AVAILABLE = False
    logging.warning(f"LanceDB import failed: {e}. Semantic memory disabled.")

logger = logging.getLogger(__name__)

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

from src.memory.embedding_model_singleton import get_embedding_model, EMBED_DIMENSION
from src.utils.degradation import log_degraded


def _log_memory(
    op: str,
    collection: str = "",
    session_id: str = "",
    items_count: int = None,
    duration: float = None,
    success: bool = True,
    db_path: str = "",
) -> None:
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_memory_operation(
                op, collection, session_id, items_count, duration, success, db_path
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic.py:_log_memory")


def _log_error(error: Exception, context: str, extra: dict = None) -> None:
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic.py:_log_error")


if LANCEDB_AVAILABLE:
    class MemoryRecord(LanceModel):
        """Schema LanceDB para memoria semántica (dim=EMBED_DIMENSION)."""
        vector: Vector(EMBED_DIMENSION)
        text: str
        session_id: str = "default"
        timestamp: str = ""
        metadata: str = "{}"
else:
    MemoryRecord = None  # type: ignore[assignment,misc]


class SemanticSearchCache:
    """Cache LRU simple para resultados de búsqueda semántica."""

    def __init__(self, max_size: int = 500) -> None:
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def set(self, key: str, value: List[Dict[str, Any]]) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def _make_key(
        self,
        query: str,
        limit: int,
        min_sim: float,
        filter_md: Optional[Dict],
    ) -> str:
        md_str = str(sorted(filter_md.items())) if filter_md else ""
        return f"{query}:{limit}:{min_sim}:{md_str}"


class SemanticMemory:
    """
    Memoria semántica usando LanceDB para búsqueda vectorial.
    v0.6.3: Migración desde ChromaDB. API pública idéntica.
    Métrica cosine: similarity = 1.0 - _distance.
    """

    MAX_SEARCH_LIMIT = 100
    MAX_CONTENT_LENGTH = 100000

    def __init__(
        self,
        collection_name: Optional[str] = None,
        persist_directory: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        start = time.time()
        _log_memory("semantic_init", "semantic", session_id=session_id)

        if not LANCEDB_AVAILABLE:
            raise ImportError(
                "LanceDB no disponible. Ejecuta: pip install lancedb pyarrow"
            )

        self.session_id = session_id or "default"
        self.persist_directory = (
            Path(persist_directory) if persist_directory else Paths.MEMORY / "lancedb"
        )
        self.persist_directory.mkdir(parents=True, exist_ok=True)

        raw_name = collection_name or f"axioma_{self._sanitize_session_id(self.session_id)}"
        self.collection_name = self._sanitize_collection_name(raw_name)

        self._db = lancedb.connect(str(self.persist_directory))

        # ✅ 2026-08-25: race-safe — dos inits concurrentes pueden llamar
        # create_table a la vez ("Table already exists"). Si falla el create,
        # otro proceso ganó la carrera → abrir la tabla existente.
        try:
            self._table = self._db.open_table(self.collection_name)
        except Exception as e:
            log_degraded(logger, e, "src/memory/semantic.py:__init__")
            try:
                self._table = self._db.create_table(
                    self.collection_name, schema=MemoryRecord
                )
            except Exception as e:
                log_degraded(logger, e, "src/memory/semantic.py:__init__")
                self._table = self._db.open_table(self.collection_name)

        self._query_cache = SemanticSearchCache(max_size=500)
        self._lock = threading.RLock()

        duration = time.time() - start
        _log_memory(
            "semantic_init", "semantic",
            session_id=self.session_id, duration=duration, success=True,
            db_path=str(self.persist_directory),
        )
        logger.debug(f"SemanticMemory initialized: lancedb table={self.collection_name}")

    @staticmethod
    def _sanitize_session_id(session_id: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]', '_', session_id)[:16] or "default"

    @staticmethod
    def _sanitize_collection_name(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9._-]', '_', name).strip('._-')
        if len(sanitized) < 3:
            sanitized = (sanitized + 'axioma')[:3]
        return sanitized[:512]

    def add(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
    ) -> str:
        """
        Agrega documento a memoria semántica.
        Retorna doc_id (str vacío si falla silenciosamente).
        """
        start = time.time()
        _log_memory("add", "semantic", session_id=self.session_id)

        if not text or len(text) > self.MAX_CONTENT_LENGTH:
            _log_memory("add", "semantic", success=False, duration=0)
            raise MemoryError("text inválido o demasiado largo", operation="add")

        with self._lock:
            try:
                doc_id = hashlib.sha256(
                    f"{text}:{time.time()}".encode()
                ).hexdigest()[:16]

                doc_meta: Dict[str, Any] = {
                    "session_id": self.session_id,
                    "role": metadata.get("role", "user") if metadata else "user",
                    "timestamp": str(
                        metadata.get("timestamp", time.time()) if metadata else time.time()
                    ),
                    **(metadata or {}),
                }

                final_embedding = embedding
                if final_embedding is None:
                    model = get_embedding_model()
                    final_embedding = model.embed(text)
                    if final_embedding is None:
                        logger.warning(
                            f"SemanticMemory add SKIPPED: no embedding for '{text[:30]}'"
                        )
                        return ""

                vec: List[float] = (
                    list(final_embedding)
                    if not isinstance(final_embedding, list)
                    else final_embedding
                )

                record = {
                    "vector": vec,
                    "text": text,
                    "session_id": str(doc_meta.get("session_id", self.session_id)),
                    "timestamp": str(doc_meta.get("timestamp", "")),
                    "metadata": json.dumps(doc_meta),
                }

                self._table.add([record])
                self._query_cache.clear()

                duration = time.time() - start
                _log_memory(
                    "add", "semantic",
                    session_id=self.session_id, items_count=1,
                    duration=duration, success=True,
                )
                logger.debug(f"SemanticMemory add OK: id={doc_id}, len={len(text)}")
                return doc_id

            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SemanticMemory.add", extra={"text_len": len(text)})
                _log_memory(
                    "add", "semantic",
                    session_id=self.session_id, duration=duration, success=False,
                )
                raise MemoryError(f"Error almacenando en LanceDB: {e}", operation="add")

    def search(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Búsqueda semántica por similitud coseno.
        similarity = 1.0 - _distance (consistente con ChromaDB cosine).
        Filtro por session_id soportado via columna top-level.
        """
        start = time.time()
        _log_memory("search", "semantic", session_id=self.session_id)

        if not query:
            _log_memory("search", "semantic", success=False, duration=0)
            return []

        cache_key = self._query_cache._make_key(
            query, limit, min_similarity, filter_metadata
        )
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            duration = time.time() - start
            _log_memory(
                "search", "semantic",
                items_count=len(cached), duration=duration, success=True,
            )
            return cached

        limit = min(limit, self.MAX_SEARCH_LIMIT)

        with self._lock:
            try:
                query_embedding = get_embedding_model().embed(query)
                if query_embedding is None:
                    logger.warning(
                        f"SemanticMemory search SKIPPED: no embedding for '{query[:30]}'"
                    )
                    return []

                vec: List[float] = (
                    list(query_embedding)
                    if not isinstance(query_embedding, list)
                    else query_embedding
                )

                query_builder = (
                    self._table.search(vec)
                    .distance_type("cosine")
                    .limit(limit)
                )

                if filter_metadata:
                    sid = filter_metadata.get("session_id")
                    if sid:
                        safe_sid = str(sid).replace("'", "''")
                        query_builder = query_builder.where(
                            f"session_id = '{safe_sid}'"
                        )
                    other_keys = [k for k in filter_metadata if k != "session_id"]
                    if other_keys:
                        logger.warning(
                            f"SemanticMemory: filtros sin soporte directo en LanceDB "
                            f"(ignorados): {other_keys}"
                        )

                results_list: List[Dict[str, Any]] = query_builder.to_list()

                output: List[Dict[str, Any]] = []
                for r in results_list:
                    distance = float(r.get("_distance", 1.0))
                    similarity = round(1.0 - distance, 4)
                    if similarity >= min_similarity:
                        meta_raw = r.get("metadata", "{}")
                        output.append({
                            "text": r.get("text", ""),
                            "similarity": similarity,
                            "metadata": json.loads(meta_raw) if meta_raw else {},
                        })

                self._query_cache.set(cache_key, output)
                duration = time.time() - start
                _log_memory(
                    "search", "semantic",
                    session_id=self.session_id, items_count=len(output),
                    duration=duration, success=True,
                )
                logger.debug(
                    f"SemanticMemory search: {len(output)} results for '{query[:30]}'"
                )
                return output

            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SemanticMemory.search", extra={"query": query[:30]})
                _log_memory(
                    "search", "semantic",
                    session_id=self.session_id, duration=duration, success=False,
                )
                logger.error(f"SemanticMemory search failed: {e}")
                return []

    def search_by_session(
        self,
        session_id: str,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("search_by_session", "semantic", session_id=session_id)
        try:
            result = self.search(
                query=query,
                limit=limit,
                filter_metadata={"session_id": session_id},
            )
            duration = time.time() - start
            _log_memory(
                "search_by_session", "semantic",
                session_id=session_id, items_count=len(result),
                duration=duration, success=True,
            )
            return result
        except Exception as e:
            duration = time.time() - start
            _log_error(
                e, "SemanticMemory.search_by_session",
                extra={"session_id": session_id},
            )
            _log_memory(
                "search_by_session", "semantic",
                session_id=session_id, duration=duration, success=False,
            )
            raise

    def get_stats(self) -> Dict[str, Any]:
        start = time.time()
        _log_memory("get_stats", "semantic")
        try:
            count = self._table.count_rows() if self._table else 0
            duration = time.time() - start
            _log_memory(
                "get_stats", "semantic",
                items_count=count, duration=duration, success=True,
            )
            return {
                "count": count,
                "collection": self.collection_name,
                "persist_directory": str(self.persist_directory),
                "cache_stats": {"query_cache_size": len(self._query_cache._cache)},
            }
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemory.get_stats", extra={})
            _log_memory("get_stats", "semantic", duration=duration, success=False)
            return {"count": 0, "error": str(e)}

    def clear(self) -> bool:
        start = time.time()
        _log_memory("clear", "semantic")
        with self._lock:
            try:
                self._db.drop_table(self.collection_name)
                try:
                    self._table = self._db.create_table(
                        self.collection_name, schema=MemoryRecord
                    )
                except Exception as e:
                    log_degraded(logger, e, "src/memory/semantic.py:clear")
                    self._table = self._db.open_table(self.collection_name)
                self._query_cache.clear()
                duration = time.time() - start
                _log_memory("clear", "semantic", duration=duration, success=True)
                logger.info(f"SemanticMemory cleared: collection={self.collection_name}")
                return True
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SemanticMemory.clear", extra={})
                _log_memory("clear", "semantic", duration=duration, success=False)
                return False

    def delete_by_metadata(self, filter_metadata: Dict[str, Any]) -> int:
        """
        Elimina registros por metadata.
        LanceDB: filtro directo por columna 'session_id'.
        Otros campos del metadata JSON no son filtrables directamente.
        """
        start = time.time()
        _log_memory("delete_by_metadata", "semantic")
        with self._lock:
            try:
                if not filter_metadata:
                    return 0
                sid = filter_metadata.get("session_id")
                if not sid:
                    logger.warning(
                        "delete_by_metadata: solo 'session_id' soportado como filtro "
                        "en LanceDB. Otros campos requieren migración de schema."
                    )
                    return 0
                before = self._table.count_rows()
                safe_sid = str(sid).replace("'", "''")
                self._table.delete(f"session_id = '{safe_sid}'")
                after = self._table.count_rows()
                deleted = max(0, before - after)
                self._query_cache.clear()
                duration = time.time() - start
                _log_memory(
                    "delete_by_metadata", "semantic",
                    items_count=deleted, duration=duration, success=True,
                )
                return deleted
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SemanticMemory.delete_by_metadata", extra={})
                _log_memory(
                    "delete_by_metadata", "semantic", duration=duration, success=False
                )
                return 0

    def export_collection(self) -> Dict[str, Any]:
        start = time.time()
        _log_memory("export_collection", "semantic")
        try:
            df = self._table.to_pandas()
            duration = time.time() - start
            _log_memory("export_collection", "semantic", duration=duration, success=True)
            if df.empty:
                return {
                    "collection": self.collection_name,
                    "count": 0,
                    "documents": [],
                    "metadatas": [],
                    "embeddings": [],
                }
            return {
                "collection": self.collection_name,
                "count": len(df),
                "documents": df["text"].tolist(),
                "metadatas": [
                    json.loads(m) if m else {} for m in df["metadata"].tolist()
                ],
                "embeddings": [
                    v.tolist() if hasattr(v, "tolist") else list(v)
                    for v in df["vector"].tolist()
                ],
            }
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemory.export_collection", extra={})
            _log_memory(
                "export_collection", "semantic", duration=duration, success=False
            )
            return {"documents": [], "error": str(e)}

    def close(self) -> None:
        start = time.time()
        _log_memory("close", "semantic")
        with self._lock:
            try:
                self._query_cache.clear()
                self._table = None
                self._db = None
                duration = time.time() - start
                _log_memory("close", "semantic", duration=duration, success=True)
                logger.info("SemanticMemory closed")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SemanticMemory.close", extra={})
                _log_memory("close", "semantic", duration=duration, success=False)

    def __enter__(self) -> "SemanticMemory":
        return self

    def __exit__(self, exc_type: type, exc_val: BaseException, exc_tb: Any) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        try:
            count = self._table.count_rows() if self._table else 0
            return f"SemanticMemory(collection={self.collection_name}, count={count})"
        except Exception:
            return f"SemanticMemory(collection={self.collection_name})"

    def __len__(self) -> int:
        try:
            return self._table.count_rows() if self._table else 0
        except Exception:
            return 0

    def __bool__(self) -> bool:
        return self._table is not None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception as _d2e:
            log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic.py:__del__")
