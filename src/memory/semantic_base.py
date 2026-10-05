#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.3 — Memoria Semántica (LanceDB) — BASE
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/semantic_base.py
# Autor: SaintWick
# ✅ v0.5.2: Eliminado cascade Ollama → MiniLM → Hash.
#            Ahora usa BGE-M3 singleton (dim 1024 consistente).
#            Eliminada clase EmbeddingModelPool (reemplazada por singleton).
# ✅ v0.6.3: MIGRACIÓN COMPLETA ChromaDB → LanceDB.
#            - _init_chroma() → _init_lance()
#            - chromadb.PersistentClient → lancedb.connect()
#            - collection.upsert() → table.add() / merge_insert()
#            - Default path "chroma" → "lancedb"
#            - LanceModel schema con campo id para merge_insert
# ═══════════════════════════════════════════════════════════════
import logging
import time
import json
import hashlib
import uuid
import re
import threading
from typing import Optional, List, Dict, Any
from pathlib import Path
from collections import OrderedDict
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError
from src.utils.degradation import log_degraded

# ═══════════════════════════════════════════════════════════════
# LanceDB Imports
# ═══════════════════════════════════════════════════════════════
try:
    import lancedb
    from lancedb.pydantic import LanceModel, Vector
    _LANCE_AVAILABLE = True
except ImportError:
    _LANCE_AVAILABLE = False
    lancedb = None
    LanceModel = None
    Vector = None

try:
    from src.memory.embedding_model_singleton import EMBED_DIMENSION
except ImportError:
    EMBED_DIMENSION = 1024

# ✅ v0.6.3: Schema LanceDB para memoria semántica
if _LANCE_AVAILABLE and LanceModel is not None and Vector is not None:
    class MemoryRecord(LanceModel):
        """Schema LanceDB para registros de memoria semántica."""
        id: str = ""
        vector: Vector(EMBED_DIMENSION)
        text: str
        session_id: str = "default"
        timestamp: str = ""
        metadata: str = "{}"
else:
    MemoryRecord = None  # type: ignore[misc,assignment]

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_memory(op: str, collection: str = "", session_id: str = "",
                items_count: int = None, duration: float = None,
                success: bool = True, db_path: str = ""):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_memory_operation(op, collection, session_id, items_count, duration, success, db_path)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic_base.py:_log_memory")


def _log_error(error: Exception, context: str, extra: dict = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic_base.py:_log_error")


from src.memory.embedding_optimizer import EmbeddingCache


class SemanticMemoryBase:
    """
    Base para memoria semántica — Infraestructura y operaciones write.

    v0.6.3: Usa LanceDB como backend vectorial. BGE-M3 singleton para embeddings.
    """

    MAX_COLLECTION_NAME_LENGTH = 512
    MIN_COLLECTION_NAME_LENGTH = 3
    MAX_TEXT_LENGTH = 50000
    MAX_BATCH_SIZE = 100
    MAX_SEARCH_LIMIT = 100

    _query_cache: OrderedDict = OrderedDict()
    _query_cache_max_size = 500

    def __init__(
        self,
        collection_name: Optional[str] = None,
        persist_directory: Optional[Path] = None,
        embedding_model: Optional[str] = None,
        cache_size: int = 1000,
    ):
        start = time.time()

        raw_name = collection_name or "axioma_default"
        self.collection_name = self._sanitize_collection_name(raw_name)

        # ✅ v0.6.3: Default path ahora es lancedb, no chroma
        self.persist_directory = persist_directory or Paths.MEMORY / "lancedb"
        self.embedding_model = embedding_model or (
            settings.llm_embed_model if hasattr(settings, 'llm_embed_model')
            else "BAAI/bge-m3"
        )

        _log_memory("semantic_init", "semantic", db_path=str(self.persist_directory))

        persist_path = Path(self.persist_directory) if not isinstance(self.persist_directory, Path) else self.persist_directory
        try:
            persist_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase.__init__", extra={"persist_directory": str(persist_path)})
            _log_memory("semantic_init", "semantic", duration=duration, success=False, db_path=str(persist_path))
            raise MemoryError(f"Error creando directorio: {e}", operation="init")

        self._embedding_cache = EmbeddingCache(max_size=cache_size)
        self._lock = threading.RLock()

        self._db = None
        self._table = None
        self._initialized = False

        try:
            self._init_lance()
            self._initialized = True
            duration = time.time() - start
            _log_memory("semantic_init", "semantic", duration=duration, success=True, db_path=str(self.persist_directory))
            logger.info(f"SemanticMemoryBase initialized: collection={self.collection_name}")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase.__init__", extra={"collection_name": self.collection_name})
            _log_memory("semantic_init", "semantic", duration=duration, success=False, db_path=persist_path)
            raise

    @staticmethod
    def _sanitize_collection_name(name: str) -> str:
        if not name:
            return "axioma_default"
        sanitized = ''.join(c if c.isalnum() or c in '._-' else '_' for c in name)
        sanitized = sanitized.strip('._-')
        if len(sanitized) < SemanticMemoryBase.MIN_COLLECTION_NAME_LENGTH:
            sanitized = (sanitized + 'axioma')[:SemanticMemoryBase.MIN_COLLECTION_NAME_LENGTH]
        return sanitized[:SemanticMemoryBase.MAX_COLLECTION_NAME_LENGTH]

    @staticmethod
    def _is_valid_collection_name(name: str) -> bool:
        if not name or len(name) < 3 or len(name) > 512:
            return False
        if not re.match(r'^[a-zA-Z0-9._-]+$', name):
            return False
        if not (name[0].isalnum() and name[-1].isalnum()):
            return False
        return True

    def _init_lance(self) -> None:
        """✅ v0.6.3: Inicializa conexión LanceDB y tabla de colección."""
        start = time.time()
        if not _LANCE_AVAILABLE:
            raise ImportError(
                "LanceDB no disponible. Ejecuta: pip install lancedb pyarrow"
            )

        try:
            self._db = lancedb.connect(str(self.persist_directory))

            # ✅ 2026-08-25: race-safe — inits concurrentes pueden fallar con
            # "Table already exists" en create_table → abrir la existente.
            try:
                self._table = self._db.open_table(self.collection_name)
                logger.debug(f"LanceDB tabla existente abierta: {self.collection_name}")
            except Exception:
                if MemoryRecord is not None:
                    try:
                        self._table = self._db.create_table(
                            self.collection_name, schema=MemoryRecord
                        )
                    except Exception as e:
                        log_degraded(logger, e, "src/memory/semantic_base.py:_init_lance")
                        self._table = self._db.open_table(self.collection_name)
                    logger.debug(f"LanceDB tabla creada con MemoryRecord: {self.collection_name}")
                else:
                    # Fallback: crear con pyarrow schema
                    import pyarrow as pa
                    schema = pa.schema([
                        pa.field("id", pa.utf8()),
                        pa.field("vector", pa.list_(pa.float32(), list_size=EMBED_DIMENSION)),
                        pa.field("text", pa.utf8()),
                        pa.field("session_id", pa.utf8()),
                        pa.field("timestamp", pa.utf8()),
                        pa.field("metadata", pa.utf8()),
                    ])
                    self._table = self._db.create_table(
                        self.collection_name, schema=schema
                    )
                    logger.debug(f"LanceDB tabla creada con pyarrow schema: {self.collection_name}")

            duration = time.time() - start
            _log_memory("init_lance", "semantic", duration=duration, success=True, db_path=str(self.persist_directory))
            logger.debug(f"LanceDB initialized: collection={self.collection_name}")

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase._init_lance", extra={"collection_name": self.collection_name})
            _log_memory("init_lance", "semantic", duration=duration, success=False, db_path=str(self.persist_directory))
            logger.warning(f"LanceDB init failed: {e}")
            self._db = None
            self._table = None

    @classmethod
    def _cache_get(cls, key: str) -> Optional[Any]:
        if key in cls._query_cache:
            cls._query_cache.move_to_end(key)
            return cls._query_cache[key]
        return None

    @classmethod
    def _cache_set(cls, key: str, value: Any) -> None:
        if key in cls._query_cache:
            cls._query_cache.move_to_end(key)
        cls._query_cache[key] = value
        while len(cls._query_cache) > cls._query_cache_max_size:
            cls._query_cache.popitem(last=False)

    @classmethod
    def _cache_clear(cls) -> None:
        cls._query_cache.clear()

    def _get_embedding(self, text: str) -> List[float]:
        """
        Genera embedding usando BGE-M3 local (singleton compartido).

        v0.5.2: Eliminado cascade Ollama → MiniLM → Hash.
        Razón: Incompatibilidad de dimensiones + duplicación de RAM.

        Prioridad:
        1. Cache de embeddings (EmbeddingCache, dim 1024)
        2. BGE-M3 local via singleton (dim 1024, sin HTTP)
        3. Hash MD5 (dim 16, fallback no semántico)
        """
        start = time.time()

        cached = self._embedding_cache.get(text, model_name="BAAI/bge-m3")
        if cached is not None:
            duration = time.time() - start
            _log_memory("get_embedding", "semantic", duration=duration, success=True)
            return cached

        try:
            from src.memory.embedding_model_singleton import get_embedding_model

            model = get_embedding_model()
            embedding = model.embed(text)

            if embedding is not None:
                self._embedding_cache.set(text, embedding, model_name="BAAI/bge-m3")
                duration = time.time() - start
                _log_memory("get_embedding", "semantic", duration=duration, success=True)
                return embedding

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase._get_embedding", extra={"text_len": len(text)})
            logger.warning(f"BGE-M3 singleton failed: {e}")

        result = self._hash_embedding(text)
        self._embedding_cache.set(text, result, model_name="hash_fallback")
        duration = time.time() - start
        _log_memory("get_embedding", "semantic", duration=duration, success=False)
        logger.warning("Using hash fallback embedding (not semantic)")
        return result

    def _hash_embedding(self, text: str) -> List[float]:
        hash_bytes = hashlib.md5(text.encode()).digest()
        return [float(b) / 255.0 for b in hash_bytes]

    def _generate_id(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _validate_text(self, text: str) -> bool:
        if not text or not isinstance(text, str):
            return False
        if len(text) > SemanticMemoryBase.MAX_TEXT_LENGTH:
            return False
        return True

    def _build_record(self, document_id: str, text: str,
                      embedding: List[float], metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Construye un dict de registro listo para LanceDB."""
        return {
            "id": document_id,
            "vector": embedding,
            "text": text,
            "session_id": str(metadata.get("session_id", "default")),
            "timestamp": str(metadata.get("timestamp", "")),
            "metadata": json.dumps(metadata) if metadata else "{}",
        }

    def _upsert_records(self, records: List[Dict[str, Any]]) -> None:
        """
        Inserta o actualiza registros en la tabla LanceDB.
        Usa merge_insert si la tabla tiene columna 'id', sino add simple.
        """
        if self._table is None:
            raise MemoryError("LanceDB table not initialized", operation="upsert")

        try:
            # Intentar merge_insert (upsert por columna id)
            self._table.merge_insert("id") \
                .when_matched_update_all() \
                .when_not_matched_insert_all() \
                .execute(records)
        except Exception as e:
            log_degraded(logger, e, "src/memory/semantic_base.py:_upsert_records")
            # Fallback: add simple (puede crear duplicados si no hay columna id)
            try:
                self._table.add(records)
            except Exception as add_err:
                log_degraded(logger, add_err, "src/memory/semantic_base.py:_upsert_records")
                # Último fallback: sin campo id
                try:
                    records_no_id = [
                        {k: v for k, v in r.items() if k != "id"}
                        for r in records
                    ]
                    self._table.add(records_no_id)
                except Exception as final_err:
                    raise MemoryError(
                        f"Error insertando en LanceDB: {final_err}",
                        operation="upsert"
                    )

    def add(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> bool:
        start = time.time()
        _log_memory("add", "semantic")

        if not self._validate_text(text):
            _log_memory("add", "semantic", success=False, duration=0)
            logger.warning(f"Invalid text for add: length={len(text) if text else 0}")
            return False

        if self._table is None:
            logger.warning("SemanticMemoryBase: LanceDB not initialized")
            _log_memory("add", "semantic", success=False, duration=0)
            return False

        try:
            embedding = self._get_embedding(text)
            document_id = doc_id or self._generate_id(text)
            doc_metadata = metadata or {}
            doc_metadata["created_at"] = str(uuid.uuid4())

            record = self._build_record(document_id, text, embedding, doc_metadata)
            self._upsert_records([record])

            self._cache_clear()

            duration = time.time() - start
            _log_memory("add", "semantic", items_count=1, duration=duration, success=True)
            logger.debug(f"SemanticMemoryBase add: id={document_id}, len={len(text)}")
            return True
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase.add", extra={"text_len": len(text)})
            _log_memory("add", "semantic", duration=duration, success=False)
            logger.error(f"SemanticMemoryBase add failed: {e}")
            return False

    def add_batch(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        start = time.time()
        _log_memory("add_batch", "semantic")

        if self._table is None:
            _log_memory("add_batch", "semantic", success=False, duration=0)
            return 0

        texts = texts[:SemanticMemoryBase.MAX_BATCH_SIZE]
        metadatas = metadatas or [{} for _ in texts]

        valid_pairs = [(t, m) for t, m in zip(texts, metadatas) if self._validate_text(t)]
        if not valid_pairs:
            _log_memory("add_batch", "semantic", success=False, duration=0)
            return 0

        texts = [t for t, m in valid_pairs]
        metadatas = [m for t, m in valid_pairs]
        ids = [self._generate_id(t) for t in texts]

        try:
            embeddings = [self._get_embedding(t) for t in texts]

            records = []
            for i, (doc_id, text, embedding) in enumerate(zip(ids, texts, embeddings)):
                meta = metadatas[i] if i < len(metadatas) else {}
                meta["created_at"] = str(uuid.uuid4())
                records.append(self._build_record(doc_id, text, embedding, meta))

            self._upsert_records(records)

            self._cache_clear()

            duration = time.time() - start
            _log_memory("add_batch", "semantic", items_count=len(texts), duration=duration, success=True)
            logger.debug(f"SemanticMemoryBase batch add: {len(texts)} documents")
            return len(texts)
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "SemanticMemoryBase.add_batch", extra={"batch_size": len(texts)})
            _log_memory("add_batch", "semantic", duration=duration, success=False)
            logger.error(f"SemanticMemoryBase batch add failed: {e}")
            return 0

    def close(self) -> None:
        start = time.time()
        _log_memory("close", "semantic")

        self._embedding_cache.clear()
        self._cache_clear()

        self._db = None
        self._table = None

        duration = time.time() - start
        _log_memory("close", "semantic", duration=duration, success=True)
        logger.info("SemanticMemoryBase closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __repr__(self) -> str:
        try:
            return f"SemanticMemoryBase(collection={self.collection_name})"
        except Exception:
            return f"SemanticMemoryBase(collection=unknown)"

    def __del__(self):
        try:
            self.close()
        except Exception as _d2e:
            log_degraded(logging.getLogger(__name__), _d2e, "src/memory/semantic_base.py:__del__")

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.6.3
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                        | Razón                         |
# |----------------------|-----------------------------------------------|-------------------------------|
# | Backend              | ✅ ChromaDB → LanceDB                         | Fase 1 migración              |
# | _init_chroma()       | ✅ → _init_lance()                            | lancedb.connect + create_table|
# | self._client         | ✅ → self._db                                 | lancedb.connect()             |
# | self._collection     | ✅ → self._table                              | db.open_table / create_table  |
# | collection.upsert()  | ✅ → merge_insert("id") con fallback a add()  | LanceDB no tiene upsert nativo|
# | Default path         | ✅ "chroma" → "lancedb"                       | Nueva ruta de almacenamiento  |
# | Metadata storage     | ✅ json.dumps() para campos anidados          | LanceDB prefiere strings      |
# | MemoryRecord schema  | ✅ LanceModel con campo id                    | Para merge_insert upsert      |
# | _build_record()      | ✅ Nuevo helper para construir dicts          | DRY para add/add_batch        |
# | _upsert_records()    | ✅ Nuevo helper merge_insert → add fallback   | Robustez de escritura         |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
