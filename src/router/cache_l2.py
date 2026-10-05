#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.2 — Cache L2 (Persistente) para Router
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/cache_l2.py
# Autor: SaintWick
# Fecha: 2026-04-05
# Propósito: Cache de nivel 2 persistente (SQLite) para decisiones de routing
# Versión: 0.5.2 (FASE 3)
# ✅ FIX v0.5.1: Añadidos _log_error y _log_cache que faltaban.
# ✅ FIX v0.5.1: MAX_KEY_LENGTH aumentado a 100000 para archivos adjuntos.
# ✅ FIX v0.5.2 (FASE 3):
#    - get() retorna expires_at para evitar Stale Routing en L1.
#    - set() UPDATE incrementa access_count en lugar de resetear a 0.
#    - _enforce_max_entries() ejecuta de forma probabilística (10%).
# ═══════════════════════════════════════════════════════════════
import logging
import time
import json
import random
import sqlite3
import threading
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from collections import OrderedDict
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError

from src.utils.db_pool import SQLiteConnectionPool

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 cache_l2.py:54] excepción degradada (intencional): {_d2e}")


def _log_cache(op: str, level: str = "L2", key: str = "",
               items_count: int = None, duration: float = None,
               success: bool = True, hit: bool = False, **kwargs):
    """
    Helper para logging de operaciones de caché.
    ✅ v0.5.1: Añadido **kwargs para aceptar db_path, size, etc.
    sin causar TypeError: unexpected keyword argument.
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event("CACHE", f"[{level}] {op}: {key[:30] if key else ''} "
                          f"{'HIT' if hit else 'MISS'}",
                          extra={"operation": op, "level": level, "hit": hit,
                                 "items_count": items_count, "duration": duration,
                                 **kwargs})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 cache_l2.py:76] excepción degradada (intencional): {_d2e}")


# ============================================================================
# CLASE PRINCIPAL: CacheL2
# ============================================================================
class CacheL2:
    """
    Cache de nivel 2 persistente para decisiones de routing.

    Responsabilidades:
    - Persistir decisiones de routing en SQLite
    - TTL-based expiration con cleanup automático
    - Thread-safe operations
    - Métricas de hit/miss y tamaño
    - Integración con RouterCache (L1)

    Arquitectura:
    ┌─────────────────────────────────────────────────────────┐
    │                    Router Query                        │
    └───────────────────────┬─────────────────────────────────┘
                            │
                            ▼
    ┌─────────────────────────────────────────────────────────┐
    │              RouterCache (L1 - Memoria)                 │
    │         • Max 1000 entries • TTL 300s • Rápido         │
    └───────────────────────┬─────────────────────────────────┘
                            │ Miss
                            ▼
    ┌─────────────────────────────────────────────────────────┐
    │              CacheL2 (L2 - SQLite)                      │
    │    • Max 10000 entries • TTL 3600s • Persistente       │
    └───────────────────────┬─────────────────────────────────┘
                            │ Miss
                            ▼
    ┌─────────────────────────────────────────────────────────┐
    │              Router Decision (LLM/API)                  │
    └─────────────────────────────────────────────────────────┘

    Example:
        >>> from config.paths import Paths
        >>> cache = CacheL2(db_path=Paths.CACHE / "router_l2.db")
        >>> cache.initialize()
        >>> cache.set("query_hash", {"model": "llama3.2", "task": "CODE"})
        >>> result, expires_at = cache.get("query_hash")
        >>> cache.close()
    """

    # Límites de seguridad
    MAX_KEY_LENGTH = 100000  # ✅ v0.5.1: Aumentado de 256 a 100000 para queries con archivos adjuntos
    MAX_VALUE_SIZE = 50000   # 50KB máximo por entrada
    DEFAULT_TTL = 3600       # 1 hora por defecto
    CLEANUP_INTERVAL = 300   # Cleanup cada 5 minutos

    def __init__(
        self,
        db_path: Optional[Path] = None,
        pool_size: int = 5,
        max_entries: int = 10000,
        default_ttl: int = DEFAULT_TTL,
    ):
        """
        Inicializa cache L2.

        Args:
            db_path: Ruta al archivo SQLite (default: Paths.CACHE/router_l2.db)
            pool_size: Tamaño del pool de conexiones
            max_entries: Máximo de entradas en la cache
            default_ttl: TTL por defecto en segundos
        """
        start = time.time()

        # ✅ CORREGIDO: Usar Paths.CACHE si existe, sino crear
        self.db_path = db_path or (Paths.CACHE / "router_l2.db")
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self.default_ttl_seconds = default_ttl

        # Asegurar directorio existe
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            _log_error(e, "CacheL2.__init__", extra={"db_path": str(self.db_path)})
            raise MemoryError(f"No se puede crear directorio: {e}", operation="init")

        # Connection pool
        self._pool: Optional[SQLiteConnectionPool] = None
        self._pool_size = pool_size

        # Thread safety
        self._lock = threading.RLock()
        self._initialized = False

        # Métricas
        self._stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "cleanups": 0,
            "errors": 0,
        }

        # Cleanup timer
        self._cleanup_timer: Optional[threading.Timer] = None
        self._last_cleanup: float = 0

        try:
            self._pool = SQLiteConnectionPool(self.db_path, pool_size=pool_size)
            self._pool.initialize()
            self._init_db()
            self._initialized = True

            # Iniciar cleanup timer
            self._schedule_cleanup()

            duration = time.time() - start
            _log_cache("init", "L2", duration=duration, success=True,
                      db_path=str(self.db_path), size=0)
            logger.info(f"CacheL2 initialized: {self.db_path} (max={max_entries}, ttl={default_ttl}s)")

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2.__init__", extra={"db_path": str(self.db_path)})
            _log_cache("init", "L2", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error inicializando cache L2: {e}", operation="init")

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        """Obtiene una conexión del pool."""
        if not self._pool:
            return None
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            raise MemoryError("No hay conexiones disponibles en el pool", operation="connect")
        return conn

    def _release_connection(self, conn: sqlite3.Connection) -> None:
        """Libera una conexión al pool."""
        if self._pool and conn:
            self._pool.release(conn)

    def _init_db(self) -> None:
        """Inicializa esquema de la base de datos."""
        start = time.time()
        conn = self._get_connection()

        if not conn:
            raise MemoryError("No se pudo obtener conexión para init_db", operation="init_db")

        try:
            with self._lock:
                conn.executescript("""
                    -- Tabla principal de cache L2
                    CREATE TABLE IF NOT EXISTS cache_entries (
                        key_hash TEXT PRIMARY KEY NOT NULL,
                        key_original TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        access_count INTEGER DEFAULT 0,
                        last_accessed TEXT,
                        size_bytes INTEGER DEFAULT 0
                    );

                    -- Índices para búsqueda eficiente
                    CREATE INDEX IF NOT EXISTS idx_expires ON cache_entries(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_accessed ON cache_entries(last_accessed);
                    CREATE INDEX IF NOT EXISTS idx_created ON cache_entries(created_at);

                    -- Tabla de métricas
                    CREATE TABLE IF NOT EXISTS cache_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        hits INTEGER DEFAULT 0,
                        misses INTEGER DEFAULT 0,
                        sets INTEGER DEFAULT 0,
                        deletes INTEGER DEFAULT 0,
                        size INTEGER DEFAULT 0,
                        cleanups INTEGER DEFAULT 0
                    );

                    -- Tabla de configuración
                    CREATE TABLE IF NOT EXISTS cache_config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                """)
                conn.commit()

                # Guardar configuración inicial
                timestamp = datetime.now(timezone.utc).isoformat()
                conn.execute("""
                    INSERT OR REPLACE INTO cache_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, ("max_entries", str(self.max_entries), timestamp))
                conn.execute("""
                    INSERT OR REPLACE INTO cache_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, ("default_ttl", str(self.default_ttl), timestamp))
                conn.commit()

                duration = time.time() - start
                _log_cache("init_db", "L2", duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"CacheL2 schema initialized")

        except sqlite3.Error as e:
            duration = time.time() - start
            _log_error(e, "CacheL2._init_db", extra={"db_path": str(self.db_path)})
            _log_cache("init_db", "L2", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error creando esquema: {e}", operation="init_db")

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2._init_db", extra={"db_path": str(self.db_path)})
            _log_cache("init_db", "L2", duration=duration, success=False, db_path=str(self.db_path))
            raise MemoryError(f"Error inesperado: {e}", operation="init_db")

        finally:
            self._release_connection(conn)

    def _generate_key_hash(self, key: str) -> str:
        """
        Genera hash SHA256 para una clave.

        Args:
            key: Clave original

        Returns:
            Hash de 64 caracteres hex
        """
        return hashlib.sha256(key.encode()).hexdigest()

    def _validate_key(self, key: str) -> bool:
        """Valida que la clave sea válida."""
        if not key or not isinstance(key, str):
            return False
        if len(key) > self.MAX_KEY_LENGTH:
            return False
        return True

    def _validate_value(self, value: Any) -> bool:
        """Valida que el valor no exceda tamaño máximo."""
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
            if len(serialized) > self.MAX_VALUE_SIZE:
                return False
            return True
        except (TypeError, ValueError):
            return False

    def _schedule_cleanup(self):
        """Programa cleanup automático."""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()

        self._cleanup_timer = threading.Timer(
            self.CLEANUP_INTERVAL,
            self._cleanup_expired
        )
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _cleanup_expired(self) -> int:
        """
        Limpia entradas expiradas.

        Returns:
            Número de entradas eliminadas
        """
        if not self._initialized:
            return 0

        start = time.time()
        conn = self._get_connection()

        if not conn:
            return 0

        try:
            with self._lock:
                now = datetime.now(timezone.utc).isoformat()

                cursor = conn.execute("""
                    DELETE FROM cache_entries
                    WHERE expires_at < ?
                """, (now,))

                deleted = cursor.rowcount
                conn.commit()

                # Registrar métrica de cleanup
                self._stats["cleanups"] += 1
                conn.execute("""
                    INSERT INTO cache_metrics (timestamp, cleanups)
                    VALUES (?, ?)
                """, (now, deleted))
                conn.commit()

                self._last_cleanup = time.time()

                duration = time.time() - start
                _log_cache("cleanup", "L2", duration=duration, success=True,
                          db_path=str(self.db_path), size=deleted)
                logger.debug(f"CacheL2 cleanup: {deleted} entradas eliminadas")

                # Re-programar cleanup
                self._schedule_cleanup()

                return deleted

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2._cleanup_expired", extra={"db_path": str(self.db_path)})
            self._stats["errors"] += 1
            return 0

        finally:
            self._release_connection(conn)

    def get(self, key: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Obtiene valor y fecha de expiración de la cache L2.

        Args:
            key: Clave original (se hash internally)

        Returns:
            Tuple (valor, expires_at_iso). Si no existe/expiró: (None, None).
            ✅ v0.5.2: Retorna expires_at para que L1 calcule el TTL restante.
        """
        if not self._initialized:
            return None, None

        if not key or not isinstance(key, str):
            logger.warning(f"CacheL2: clave inválida (len={len(key) if key else 0})")
            return None, None
        # ✅ v0.6.9q: adjuntos grandes (~150K chars) excedían el límite y se
        # descartaban con warning — ahora se cachean hasheados.
        if len(key) > self.MAX_KEY_LENGTH:
            key = "h:" + self._generate_key_hash(key)

        start = time.time()
        key_hash = self._generate_key_hash(key)
        conn = self._get_connection()

        if not conn:
            self._stats["misses"] += 1
            return None, None

        try:
            with self._lock:
                now = datetime.now(timezone.utc).isoformat()

                cursor = conn.execute("""
                    SELECT value_json, access_count, expires_at
                    FROM cache_entries
                    WHERE key_hash = ? AND expires_at > ?
                """, (key_hash, now))

                row = cursor.fetchone()

                if row:
                    # Hit - actualizar acceso
                    value = json.loads(row["value_json"])
                    expires_at = row["expires_at"]
                    new_access_count = row["access_count"] + 1

                    conn.execute("""
                        UPDATE cache_entries
                        SET access_count = ?, last_accessed = ?
                        WHERE key_hash = ?
                    """, (new_access_count, now, key_hash))
                    conn.commit()

                    self._stats["hits"] += 1

                    duration = time.time() - start
                    _log_cache("get", "L2", key=key_hash, hit=True,
                              duration=duration, success=True, db_path=str(self.db_path))
                    logger.debug(f"CacheL2 HIT: {key[:32]}... (access #{new_access_count})")

                    return value, expires_at
                else:
                    # Miss
                    self._stats["misses"] += 1

                    duration = time.time() - start
                    _log_cache("get", "L2", key=key_hash, hit=False,
                              duration=duration, success=True, db_path=str(self.db_path))
                    logger.debug(f"CacheL2 MISS: {key[:32]}...")

                    return None, None

        except json.JSONDecodeError as e:
            logger.error(f"CacheL2: JSON decode error for key {key[:32]}: {e}")
            self._stats["errors"] += 1
            return None, None

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2.get", extra={"key": key[:32]})
            self._stats["errors"] += 1
            return None, None

        finally:
            self._release_connection(conn)

    def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl: Optional[int] = None,
    ) -> bool:
        """
        Guarda valor en la cache L2.

        Args:
            key: Clave original
            value: Valor a almacenar (debe ser serializable a JSON)
            ttl: TTL en segundos (default: default_ttl)

        Returns:
            True si se guardó exitosamente
        """
        if not self._initialized:
            return False

        if not key or not isinstance(key, str):
            logger.warning(f"CacheL2: clave inválida para set (len={len(key) if key else 0})")
            return False
        # ✅ v0.6.9q: claves largas (adjuntos ~150K) → hash en vez de rechazo
        if len(key) > self.MAX_KEY_LENGTH:
            key = "h:" + self._generate_key_hash(key)

        if not self._validate_value(value):
            logger.warning(f"CacheL2: valor demasiado grande para clave {key[:32]}")
            return False

        start = time.time()
        key_hash = self._generate_key_hash(key)
        ttl = ttl or self.default_ttl
        conn = self._get_connection()

        if not conn:
            return False

        try:
            with self._lock:
                now = datetime.now(timezone.utc)
                created_at = now.isoformat()
                expires_at = datetime.fromtimestamp(
                    now.timestamp() + ttl, tz=timezone.utc
                ).isoformat()

                value_json = json.dumps(value, ensure_ascii=False, default=str)
                size_bytes = len(value_json.encode('utf-8'))

                # Verificar si existe para actualizar o insertar
                cursor = conn.execute("""
                    SELECT key_hash FROM cache_entries WHERE key_hash = ?
                """, (key_hash,))

                exists = cursor.fetchone() is not None

                if exists:
                    # ═══════════════════════════════════════════════════════════════
                    # ✅ FIX FASE 3: Preservar access_count en UPDATE.
                    # Antes se reseteaba a 0, destruyendo la métrica de popularidad.
                    # ═══════════════════════════════════════════════════════════════
                    conn.execute("""
                        UPDATE cache_entries
                        SET value_json = ?, created_at = ?, expires_at = ?,
                            size_bytes = ?, last_accessed = ?, access_count = access_count + 1
                        WHERE key_hash = ?
                    """, (value_json, created_at, expires_at, size_bytes, created_at, key_hash))
                else:
                    # INSERT
                    conn.execute("""
                        INSERT INTO cache_entries
                        (key_hash, key_original, value_json, created_at, expires_at, size_bytes, last_accessed, access_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """, (key_hash, key, value_json, created_at, expires_at, size_bytes, created_at))

                conn.commit()

                # ═══════════════════════════════════════════════════════════════
                # ✅ FIX FASE 3: Degradación O(N) mitigada.
                # Ejecutar _enforce_max_entries solo el 10% de las veces
                # (probabilístico) para evitar escaneo completo en cada SET.
                # ═══════════════════════════════════════════════════════════════
                if random.random() < 0.1:
                    self._enforce_max_entries(conn)

                self._stats["sets"] += 1

                duration = time.time() - start
                _log_cache("set", "L2", key=key_hash, duration=duration,
                          success=True, db_path=str(self.db_path))
                logger.debug(f"CacheL2 SET: {key[:32]}... (ttl={ttl}s, size={size_bytes}B)")

                return True

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2.set", extra={"key": key[:32]})
            self._stats["errors"] += 1
            return False

        finally:
            self._release_connection(conn)

    def _enforce_max_entries(self, conn: sqlite3.Connection):
        """
        Elimina entradas más antiguas si se excede max_entries.

        Args:
            conn: Conexión SQLite activa
        """
        try:
            cursor = conn.execute("""
                SELECT COUNT(*) as count FROM cache_entries
            """)
            count = cursor.fetchone()["count"]

            if count > self.max_entries:
                # Eliminar las más antiguas por last_accessed
                to_delete = count - self.max_entries
                conn.execute("""
                    DELETE FROM cache_entries
                    WHERE key_hash IN (
                        SELECT key_hash FROM cache_entries
                        ORDER BY last_accessed ASC
                        LIMIT ?
                    )
                """, (to_delete,))
                conn.commit()
                logger.debug(f"CacheL2: {to_delete} entradas eliminadas por límite de tamaño")

        except Exception as e:
            logger.error(f"CacheL2: error enforcing max_entries: {e}")
            self._stats["errors"] += 1

    def delete(self, key: str) -> bool:
        """
        Elimina entrada de la cache.

        Args:
            key: Clave original

        Returns:
            True si se eliminó
        """
        if not self._initialized:
            return False

        start = time.time()
        key_hash = self._generate_key_hash(key)
        conn = self._get_connection()

        if not conn:
            return False

        try:
            with self._lock:
                cursor = conn.execute("""
                    DELETE FROM cache_entries WHERE key_hash = ?
                """, (key_hash,))

                deleted = cursor.rowcount > 0
                conn.commit()

                if deleted:
                    self._stats["deletes"] += 1

                duration = time.time() - start
                _log_cache("delete", "L2", key=key_hash, duration=duration,
                          success=deleted, db_path=str(self.db_path))
                logger.debug(f"CacheL2 DELETE: {key[:32]}... {'OK' if deleted else 'NOT FOUND'}")

                return deleted

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2.delete", extra={"key": key[:32]})
            self._stats["errors"] += 1
            return False

        finally:
            self._release_connection(conn)

    def clear(self) -> bool:
        """
        Limpia toda la cache.

        Returns:
            True si se limpió exitosamente
        """
        if not self._initialized:
            return False

        start = time.time()
        conn = self._get_connection()

        if not conn:
            return False

        try:
            with self._lock:
                conn.execute("DELETE FROM cache_entries")
                conn.commit()

                duration = time.time() - start
                _log_cache("clear", "L2", duration=duration, success=True,
                          db_path=str(self.db_path), size=0)
                logger.info("CacheL2 cleared")

                return True

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "CacheL2.clear", extra={"db_path": str(self.db_path)})
            self._stats["errors"] += 1
            return False

        finally:
            self._release_connection(conn)

    def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas de la cache.

        Returns:
            Dict con estadísticas completas
        """
        if not self._initialized:
            return {"error": "Cache not initialized"}

        conn = self._get_connection()

        if not conn:
            return self._stats.copy()

        try:
            with self._lock:
                # Tamaño actual
                cursor = conn.execute("SELECT COUNT(*) as count FROM cache_entries")
                size = cursor.fetchone()["count"]

                # Tamaño total en bytes
                cursor = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) as total FROM cache_entries")
                total_bytes = cursor.fetchone()["total"]

                # Hit rate
                total_requests = self._stats["hits"] + self._stats["misses"]
                hit_rate = round(self._stats["hits"] / max(total_requests, 1), 4)

                return {
                    **self._stats.copy(),
                    "size": size,
                    "total_bytes": total_bytes,
                    "max_entries": self.max_entries,
                    "hit_rate": hit_rate,
                    "db_path": str(self.db_path),
                    "initialized": self._initialized,
                    "last_cleanup": self._last_cleanup,
                }

        except Exception as e:
            logger.error(f"CacheL2: error getting stats: {e}")
            self._stats["errors"] += 1
            return {**self._stats.copy(), "error": str(e)}

        finally:
            self._release_connection(conn)

    def get_size(self) -> int:
        """Retorna número de entradas en la cache."""
        stats = self.get_stats()
        return stats.get("size", 0)

    def get_hit_rate(self) -> float:
        """Retorna tasa de hit de la cache."""
        stats = self.get_stats()
        return stats.get("hit_rate", 0.0)

    def close(self) -> None:
        """Cierra la cache y libera recursos."""
        if not self._initialized:
            return

        # Cancelar cleanup timer
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None

        # Cerrar pool de conexiones
        if self._pool:
            self._pool.close_all()
            self._pool = None

        self._initialized = False
        logger.info("CacheL2 closed")


# ============================================================================
# SINGLETON GLOBAL
# ============================================================================
_cache_l2_instance: Optional[CacheL2] = None
_cache_l2_lock = threading.Lock()


def get_cache_l2(
    db_path: Optional[Path] = None,
    pool_size: int = 5,
    max_entries: int = 10000,
    default_ttl: int = CacheL2.DEFAULT_TTL,
) -> CacheL2:
    """
    Obtiene instancia singleton de CacheL2.

    Args:
        db_path: Ruta al archivo SQLite
        pool_size: Tamaño del pool de conexiones
        max_entries: Máximo de entradas
        default_ttl: TTL por defecto en segundos

    Returns:
        Instancia de CacheL2
    """
    global _cache_l2_instance

    if _cache_l2_instance is None:
        with _cache_l2_lock:
            if _cache_l2_instance is None:
                _cache_l2_instance = CacheL2(
                    db_path=db_path,
                    pool_size=pool_size,
                    max_entries=max_entries,
                    default_ttl=default_ttl,
                )

    return _cache_l2_instance


def reset_cache_l2() -> None:
    """Reinicia el singleton de CacheL2."""
    global _cache_l2_instance

    if _cache_l2_instance:
        _cache_l2_instance.close()
        _cache_l2_instance = None

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.5.2 (FASE 3)
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Header               | ✅ v0.5.2 changelog (FASE 3)              | Trazabilidad              |
# | import random        | ✅ Añadido para lógica probabilística     | _enforce_max_entries      |
# | get()                | ✅ Retorna Tuple[value, expires_at]        | Evitar Stale Routing en L1|
# | set() UPDATE         | ✅ access_count = access_count + 1         | Preservar métricas LFU    |
# | set() enforce        | ✅ Ejecución 10% probabilística           | Evitar O(N) en escritura  |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
