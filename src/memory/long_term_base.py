#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Memoria a Largo Plazo (SQLite) — BASE
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/long_term_base.py
# Autor: SaintWick
# Fecha: 2026-03-27
# Propósito: Infraestructura base: pool, conexiones, validaciones, write
# Optimizaciones: Connection pooling, LRU cache, thread-safe
# Fix: logging._initialized (no is_initialized)
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import threading
import re
from collections import OrderedDict
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_memory(op: str, collection: str = "", session_id: str = "",
                items_count: int = None, duration: float = None,
                success: bool = True, db_path: str = ""):
    """Helper para logging de operaciones de memoria."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar _initialized (no is_initialized)
        if hasattr(log, '_initialized') and log._initialized:
            log.log_memory_operation(op, collection, session_id, items_count, duration, success, db_path)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/long_term_base.py:_log_memory")

def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar _initialized (no is_initialized)
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/long_term_base.py:_log_error")

from src.utils.db_pool import SQLiteConnectionPool
from src.utils.degradation import log_degraded

# CLASE BASE: LongTermMemoryBase
# ============================================================================
class LongTermMemoryBase:
    """Base para memoria a largo plazo — Infraestructura y operaciones write."""
    MAX_SESSION_ID_LENGTH = 64
    MAX_CONTENT_LENGTH = 100000
    MAX_METADATA_SIZE = 10000
    _query_cache: OrderedDict = OrderedDict()
    _query_cache_max_size = 500

    def __init__(self, db_path: Optional[Path] = None, pool_size: int = 5):
        start = time.time()
        _log_memory("long_term_init", "long_term", db_path=str(db_path or Paths.MEMORY / "axioma.db"))
        self.db_path = db_path or (Paths.MEMORY / "axioma.db")
        self._pool: Optional[SQLiteConnectionPool] = None
        self._lock = threading.RLock()
        self._initialized = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemoryBase.__init__", extra={"db_path": str(self.db_path)})
            _log_memory("long_term_init", "long_term", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"No se puede crear directorio: {e}", operation="init")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemoryBase.__init__", extra={"db_path": str(self.db_path)})
            _log_memory("long_term_init", "long_term", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error inicializando ruta: {e}", operation="init")
        try:
            self._pool = SQLiteConnectionPool(self.db_path, pool_size=pool_size)
            self._pool.initialize()
            self._init_db()
            self._initialized = True
            duration = time.time() - start
            _log_memory("long_term_init", "long_term", duration=duration, success=True, db_path=str(self.db_path))
            logger.info(f"LongTermMemoryBase initialized: {self.db_path}")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemoryBase.__init__", extra={"db_path": str(self.db_path)})
            _log_memory("long_term_init", "long_term", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error inicializando DB: {e}", operation="init")

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if not self._pool:
            return None
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            raise MemoryError("No hay conexiones disponibles en el pool", operation="connect")
        return conn

    def _release_connection(self, conn: sqlite3.Connection) -> None:
        if self._pool and conn:
            self._pool.release(conn)

    def _init_db(self) -> None:
        start = time.time()
        conn = self._get_connection()
        if not conn:
            raise MemoryError("No se pudo obtener conexión para init_db", operation="init_db")
        try:
            with self._lock:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, message_count INTEGER DEFAULT 0, metadata TEXT);
                CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT NOT NULL, metadata TEXT, tokens INTEGER DEFAULT 0, FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, category TEXT, key TEXT NOT NULL, value TEXT NOT NULL, confidence REAL DEFAULT 1.0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE SET NULL);
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
                CREATE INDEX IF NOT EXISTS idx_knowledge_session ON knowledge(session_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
                """)
                conn.commit()
                duration = time.time() - start
                _log_memory("init_db", "long_term", duration=duration, success=True, db_path=str(self.db_path))
        except sqlite3.Error as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemoryBase._init_db", extra={"db_path": str(self.db_path)})
            _log_memory("init_db", "long_term", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error creando esquema: {e}", operation="init_db")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemoryBase._init_db", extra={"db_path": str(self.db_path)})
            _log_memory("init_db", "long_term", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error inesperado: {e}", operation="init_db")
        finally:
            self._release_connection(conn)

    @staticmethod
    def _validate_session_id(session_id: str) -> bool:
        if not session_id or not isinstance(session_id, str):
            return False
        if not re.match(r'^[a-zA-Z0-9_-]+$', session_id):
            return False
        if len(session_id) > LongTermMemoryBase.MAX_SESSION_ID_LENGTH:
            return False
        return True

    @staticmethod
    def _validate_content(content: str) -> bool:
        if not content or not isinstance(content, str):
            return False
        if len(content) > LongTermMemoryBase.MAX_CONTENT_LENGTH:
            return False
        return True

    @staticmethod
    def _validate_metadata(metadata: Optional[Dict[str, Any]]) -> bool:
        if metadata is None:
            return True
        if not isinstance(metadata, dict):
            return False
        try:
            if len(json.dumps(metadata)) > LongTermMemoryBase.MAX_METADATA_SIZE:
                return False
        except (TypeError, ValueError):
            return False
        return True

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

    def _update_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("SELECT message_count FROM sessions WHERE session_id = ?", (session_id,))
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE sessions SET updated_at = ?, message_count = message_count + 1 WHERE session_id = ?", (timestamp, session_id))
        else:
            conn.execute("INSERT INTO sessions (session_id, created_at, updated_at, message_count) VALUES (?, ?, ?, 1)", (session_id, timestamp, timestamp))

    def add(self, session_id: str, role: str, content: str, metadata: Optional[Dict[str, Any]] = None, tokens: int = 0) -> int:
        start = time.time()
        _log_memory("add", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            _log_memory("add", "long_term", session_id=session_id, success=False, duration=0)
            raise MemoryError(f"session_id inválido: {session_id[:20]}...", operation="add")
        if not role or role not in ("user", "assistant", "system"):
            _log_memory("add", "long_term", session_id=session_id, success=False, duration=0)
            raise MemoryError(f"role inválido: {role}", operation="add")
        if not self._validate_content(content):
            _log_memory("add", "long_term", session_id=session_id, success=False, duration=0)
            raise MemoryError(f"content inválido (longitud: {len(content)})", operation="add")
        if not self._validate_metadata(metadata):
            _log_memory("add", "long_term", session_id=session_id, success=False, duration=0)
            raise MemoryError("metadata inválido o demasiado grande", operation="add")
        conn = self._get_connection()
        if not conn:
            raise MemoryError("No se pudo obtener conexión", operation="add")
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            logger.error(f"Metadata serialization failed: {e}")
            metadata_json = "{}"
        with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._update_session(conn, session_id)
                cursor = conn.execute("INSERT INTO messages (session_id, role, content, timestamp, metadata, tokens) VALUES (?, ?, ?, ?, ?, ?)", (session_id, role, content, timestamp, metadata_json, tokens))
                message_id = cursor.lastrowid
                conn.commit()
                self._cache_clear()
                duration = time.time() - start
                _log_memory("add", "long_term", session_id=session_id, items_count=1, duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemoryBase add: session={session_id[:20]}, role={role}, id={message_id}, tokens={tokens}")
                return message_id
            except sqlite3.Error as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemoryBase.add", extra={"session_id": session_id})
                _log_memory("add", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="add")
            except Exception as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemoryBase.add", extra={"session_id": session_id})
                _log_memory("add", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="add")
            finally:
                self._release_connection(conn)

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — long_term_base.py v0.0.9 BASE
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                          | Razón                    |
# |-------|--------------------------------|--------------------------|
# | ~28   | ✅ log._initialized            | Fix logging (test #4)    |
# | ~36   | ✅ log._initialized            | Fix logging (test #4)    |
# | ~45   | ✅ SQLiteConnectionPool        | Módulo independiente     |
# | ~135  | ✅ LRU cache (_query_cache)    | Optimización CPU         |
# | ~330  | ✅ Método add()                | Operación write crítica  |
# | Todas | ✅ finally + release           | Manejo seguro recursos   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
