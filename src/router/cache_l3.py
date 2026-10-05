#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.3 — Caché Semántico L3 (LanceDB + BAAI/bge-m3)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/cache_l3.py
# Autor: SaintWick
# v0.6.3: Migración ChromaDB → LanceDB (lancedb==0.33.0)
#         API pública idéntica: get/set/get_stats/clear/__repr__
#         Upsert por doc_id: delete(where) + add([record])
#         Evicción por set_at: to_pandas()[["doc_id","set_at"]]
#         Métrica cosine: similarity = 1.0 - _distance
#         Path separado: semantic_cache_l3_lance (evita conflicto con ChromaDB existente)
# ✅ v0.6.4 (2026-07-02): FIX _evict_oldest() — log WARNING en lugar de DEBUG
#                         para detectar fallos de evicción silenciosos
# ═══════════════════════════════════════════════════════════════
import logging
import time
import json
import hashlib
import threading
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import RouterError
from src.memory.embedding_model_singleton import (
    get_embedding_model,
    EMBED_MODEL_NAME,
    EMBED_DIMENSION,
    _ST_AVAILABLE,
)

logger = logging.getLogger(__name__)

try:
    import lancedb
    from lancedb.pydantic import LanceModel, Vector
    _LANCE_AVAILABLE = True
except ImportError:
    _LANCE_AVAILABLE = False
    lancedb = None  # type: ignore[assignment]
    LanceModel = object  # type: ignore[misc,assignment]
    Vector = None  # type: ignore[assignment]

if _LANCE_AVAILABLE:
    class CacheRecord(LanceModel):
        """Schema LanceDB para caché semántico L3."""
        vector: Vector(EMBED_DIMENSION)
        doc_id: str = ""
        query_preview: str = ""
        expires_at: int = 0
        set_at: int = 0
        task_type: str = ""
        response_cache_key: str = ""
        value_json: str = "{}"
else:
    CacheRecord = None  # type: ignore[assignment,misc]


class SemanticCacheL3:
    """
    Caché semántico L3 basado en LanceDB + BAAI/bge-m3.
    v0.6.3: Migración desde ChromaDB. API pública idéntica.
    """

    COLLECTION_NAME = "router_decisions_l3"
    EMBED_MODEL = EMBED_MODEL_NAME
    EMBED_DIM = EMBED_DIMENSION

    # ✅ FIX: bajo esta longitud de query (tras strip), NO se consulta el
    # índice semántico — frases cortas ("hola como estas", "donde
    # estamos?") generan embeddings poco discriminativos y producen
    # falsos positivos entre intenciones totalmente distintas. Mismo
    # patrón que _is_greeting_or_basic_chat() (len(text) < 20) en
    # src/router/classifier_core.py, aplicado acá donde realmente
    # importa: el índice que decide qué respuesta se sirve.
    # Confirmado en producción: "donde estamos?" (16 chars) matcheó
    # "quien soy?" con sim=0.6259 (thresh=0.60) y devolvió esa respuesta
    # ajena, verbatim, vía el camino "L3 Hybrid FULL HIT" de dispatcher.py.
    MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH = 30

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        similarity_threshold: float = 0.60,
        ttl_seconds: int = 3600,
        max_entries: int = 500,
        ollama_base_url: str = None,
    ) -> None:
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._available = False
        self._table = None
        self._db = None
        self._stats: Dict[str, int] = {
            "hits": 0, "misses": 0, "sets": 0,
            "embed_errors": 0, "ttl_expirations": 0,
            "stale_pointers": 0,
        }

        if not _LANCE_AVAILABLE:
            logger.warning("[L3] lancedb no disponible — SemanticCacheL3 desactivado")
            return

        if not _ST_AVAILABLE:
            logger.warning(
                "[L3] sentence-transformers no disponible — SemanticCacheL3 desactivado. "
                "Ejecuta: pip install sentence-transformers"
            )
            return

        try:
            _path = persist_path or (Paths.DATA / "semantic_cache_l3_lance")
            _path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(_path))

            # ✅ 2026-08-25: race-safe (inits concurrentes → "Table already exists")
            try:
                self._table = self._db.open_table(self.COLLECTION_NAME)
            except Exception:
                try:
                    self._table = self._db.create_table(
                        self.COLLECTION_NAME, schema=CacheRecord
                    )
                except Exception:
                    self._table = self._db.open_table(self.COLLECTION_NAME)

            self._available = True
            count = self._table.count_rows()
            logger.info(
                f"[L3] SemanticCacheL3 inicializado (v0.6.3 LanceDB): "
                f"path={_path}, threshold={similarity_threshold}, "
                f"entries={count}/{max_entries}"
            )
        except Exception as e:
            logger.warning(f"[L3] LanceDB init error — L3 desactivado: {e}")

    def _load_model(self) -> bool:
        """Usa singleton centralizado para cargar BGE-M3."""
        model = get_embedding_model()
        if model is None:
            self._available = False
            return False
        if hasattr(model, 'load'):
            return model.load()
        return True

    def _embed(self, text: str) -> Optional[List[float]]:
        """Genera embedding local via singleton BGE-M3."""
        model = get_embedding_model()
        vec = model.embed(text)
        if vec is None:
            self._stats["embed_errors"] += 1
        return vec

    def get(self, query: str) -> Optional[Tuple[Dict[str, Any], float]]:
        """Busca la decisión de routing más semánticamente similar."""
        if not self._available:
            return None

        # ✅ FIX: ver MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH arriba.
        if len(query.strip()) < self.MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH:
            logger.debug(
                f"[L3] Query corta ({len(query.strip())} chars < "
                f"{self.MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH}) — se salta el "
                f"índice semántico, evita falsos positivos: '{query[:40]}'"
            )
            self._stats["misses"] += 1
            return None

        start = time.perf_counter()
        vec = self._embed(query)
        if vec is None:
            return None

        vec_list: List[float] = list(vec) if not isinstance(vec, list) else vec

        with self._lock:
            try:
                results: List[Dict[str, Any]] = (
                    self._table.search(vec_list)
                    .distance_type("cosine")
                    .limit(1)
                    .to_list()
                )
            except Exception as e:
                logger.debug(f"[L3] query error: {e}")
                return None

        if not results:
            self._stats["misses"] += 1
            return None

        r = results[0]
        distance = float(r.get("_distance", 1.0))
        similarity = max(0.0, min(1.0, 1.0 - distance))
        doc_id = r.get("doc_id", "")
        query_preview = r.get("query_preview", "N/A")
        expires_at = r.get("expires_at", 0)

        logger.info(
            f"[L3 DIAG] query='{query[:50]}' | "
            f"match='{query_preview[:40]}' | "
            f"sim={similarity:.4f} | "
            f"thresh={self._threshold} | "
            f"{'✅ HIT' if similarity >= self._threshold else '❌ MISS'}"
        )

        if expires_at and int(time.time()) > expires_at:
            if doc_id:
                try:
                    safe_id = doc_id.replace("'", "''")
                    self._table.delete(f"doc_id = '{safe_id}'")
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 cache_l3.py:216] excepción degradada (intencional): {_d2e}")
            self._stats["ttl_expirations"] += 1
            self._stats["misses"] += 1
            return None

        if similarity < self._threshold:
            self._stats["misses"] += 1
            return None

        try:
            value = json.loads(r.get("value_json", "{}"))
        except Exception:
            value = {
                "task_type": r.get("task_type", ""),
                "response_cache_key": r.get("response_cache_key", ""),
            }
        value["response_cache_key"] = r.get("response_cache_key", "")

        self._stats["hits"] += 1
        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(
            f"[L3] HIT (sim={similarity:.3f}, {elapsed:.1f}ms, "
            f"ram_ptr={'✅' if value.get('response_cache_key') else '❌'}): "
            f"{query[:40]}"
        )
        return value, round(similarity, 4)

    def set(self, query: str, value: Dict[str, Any], **kwargs) -> bool:
        """Almacena una decisión de routing con su embedding semántico."""
        if not self._available:
            return False

        vec = self._embed(query)
        if vec is None:
            return False

        vec_list: List[float] = list(vec) if not isinstance(vec, list) else vec
        doc_id = hashlib.sha256(query.encode()).hexdigest()[:32]
        expires_at = int(time.time()) + self._ttl
        set_at = int(time.time())

        try:
            value_json = json.dumps(value, ensure_ascii=False)
        except Exception:
            value_json = "{}"

        record = {
            "vector": vec_list,
            "doc_id": doc_id,
            "query_preview": query[:80],
            "expires_at": expires_at,
            "set_at": set_at,
            "task_type": str(value.get("task_type", "")),
            "response_cache_key": str(value.get("response_cache_key", "")),
            "value_json": value_json,
        }

        with self._lock:
            try:
                current_count = self._table.count_rows()
                if current_count >= self._max_entries:
                    self._evict_oldest()

                safe_id = doc_id.replace("'", "''")
                self._table.delete(f"doc_id = '{safe_id}'")
                self._table.add([record])

                # ✅ v0.6.8 MULTI-QUERY LIGERO: guarda las variantes expandidas
                # del query en el mismo set(). Costo amortizado en escritura —
                # futuras lecturas semánticamente similares a UNA variante
                # aciertan este HIT aunque no se parezcan al query original.
                # Ver src/memory/multi_query.py (expansión 100% determinística).
                self._store_l3_variants(query, value, doc_id, expires_at)

                new_count = self._table.count_rows()
                self._stats["sets"] += 1
                logger.info(
                    f"[L3] SET OK: '{query[:40]}' → "
                    f"task={record['task_type']} | "
                    f"ram_ptr={'✅' if record['response_cache_key'] else '❌'} | "
                    f"collection={new_count}"
                )
                return True
            except Exception as e:
                err_name = type(e).__name__
                logger.warning(f"[L3] SET FAIL: '{query[:40]}' → {err_name}: {e}")
                return False

    def _store_l3_variants(
        self,
        query: str,
        value: Dict[str, Any],
        original_doc_id: str,
        expires_at: int,
    ) -> None:
        """
        ✅ v0.6.8: Almacena variantes expandidas del query como entradas
        independientes en L3 (misma decisión de routing, distinto vector).

        Silencioso si está deshabilitado, si la query es corta o si falla —
        nunca rompe el set() principal.
        """
        try:
            if not getattr(settings, "multi_query_store_l3_variants", True):
                return
            if len(query.strip()) < getattr(
                settings, "multi_query_min_length", self.MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH
            ):
                return

            from src.memory.multi_query import expand_query
            max_variants = getattr(settings, "multi_query_max_variants", 4)
            variants = expand_query(query, max_variants=max_variants)
            if len(variants) <= 1:
                return  # Sin variantes extra — nada que guardar

            # UN solo batch BGE-M3 para todas las variantes
            model = get_embedding_model()
            if model is None:
                return
            vectors = model.embed_batch(variants)
            if vectors is None:
                return

            set_at = int(time.time())
            value_json = json.dumps(value, ensure_ascii=False)
            added = 0
            for variant, vec in zip(variants, vectors):
                if vec is None:
                    continue
                variant_doc_id = hashlib.sha256(variant.encode()).hexdigest()[:32]
                if variant_doc_id == original_doc_id:
                    continue
                vvec = list(vec) if not isinstance(vec, list) else vec
                variant_record = {
                    "vector": vvec,
                    "doc_id": variant_doc_id,
                    "query_preview": variant[:80],
                    "expires_at": expires_at,
                    "set_at": set_at,
                    "task_type": str(value.get("task_type", "")),
                    "response_cache_key": str(value.get("response_cache_key", "")),
                    "value_json": value_json,
                }
                safe_vid = variant_doc_id.replace("'", "''")
                self._table.delete(f"doc_id = '{safe_vid}'")
                self._table.add([variant_record])
                added += 1

            if added:
                logger.debug(
                    f"[L3] Variantes almacenadas: {added} extra para '{query[:40]}'"
                )
        except Exception as e:
            logger.debug(f"[L3] Variant storage skipped (non-fatal): {type(e).__name__}: {e}")

    def _evict_oldest(self) -> None:
        """Evicta las entries más antiguas por set_at."""
        try:
            batch_size = max(1, self._max_entries // 10)
            # ✅ FIX v0.6.5: to_pandas() requiere pylance (extra no instalado).
            # search() sin vector hace full scan nativo — mismo mecanismo que
            # get() ya usa vía to_list(), sin dependencia de pyarrow/pylance.
            rows: List[Dict[str, Any]] = (
                self._table.search()
                .select(["doc_id", "set_at"])
                .limit(max(self._max_entries * 2, 100))
                .to_list()
            )
            if not rows:
                return
            rows.sort(key=lambda r: r.get("set_at", 0))
            oldest_ids: List[str] = [r["doc_id"] for r in rows[:batch_size]]
            if oldest_ids:
                where_clause = " OR ".join(
                    f"doc_id = '{oid.replace(chr(39), chr(39)*2)}'"
                    for oid in oldest_ids
                )
                self._table.delete(where_clause)
        except Exception as e:
            # ✅ FIX v0.6.4: Log WARNING en lugar de DEBUG para detectar fallos
            logger.warning(f"[L3] Eviction failed: {type(e).__name__}: {e}")

    def get_stats(self) -> Dict[str, Any]:
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / max(total, 1)
        count = 0
        if self._available and self._table is not None:
            try:
                count = self._table.count_rows()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 cache_l3.py:407] excepción degradada (intencional): {_d2e}")
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate": round(hit_rate, 4),
            "collection_size": count,
            "max_entries": self._max_entries,
            "threshold": self._threshold,
            "ttl_seconds": self._ttl,
            "available": self._available,
            "embed_model": self.EMBED_MODEL,
        }

    def clear(self) -> None:
        if not self._available or self._db is None:
            return
        with self._lock:
            try:
                self._db.drop_table(self.COLLECTION_NAME)
                self._table = self._db.create_table(
                    self.COLLECTION_NAME, schema=CacheRecord
                )
                logger.info("[L3] Colección L3 limpiada")
            except Exception as e:
                logger.warning(f"[L3] clear error: {e}")

    def __repr__(self) -> str:
        stats = self.get_stats()
        return (
            f"SemanticCacheL3(model={self.EMBED_MODEL}, "
            f"available={self._available}, "
            f"entries={stats['collection_size']}/{self._max_entries}, "
            f"hit_rate={stats['hit_rate']:.2%})"
        )


RouterCache = SemanticCacheL3
