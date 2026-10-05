#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.2 — Connection Pool SQLite Compartido
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/utils/db_pool.py
# Autor: SaintWick
# Fecha: 2026-04-06
# Propósito: Pool de conexiones SQLite reutilizable.
#            Consolidado desde cache_l2.py y long_term_base.py (Fase 3).
# ✅ v0.0.2: FIX — _in_use guardaba id(conn) en vez de la conexión misma;
#   si un caller no llamaba release() y la conexión era garbage-collected,
#   un objeto nuevo podía reciclar esa dirección y aparecer como "en uso"
#   por error. Ahora guarda las conexiones (referencia fuerte).
# ═══════════════════════════════════════════════════════════════
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class SQLiteConnectionPool:
    """
    Pool de conexiones SQLite para reutilización.

    Features:
    - Thread-safe con threading.Lock
    - Connection reuse para reducir overhead
    - Timeout para adquisición de conexiones
    - WAL mode para mejor concurrencia

    Example:
        >>> pool = SQLiteConnectionPool(Path("axioma.db"))
        >>> pool.initialize()
        >>> conn = pool.acquire()
        >>> # usar conexión
        >>> pool.release(conn)
        >>> pool.close_all()
    """

    def __init__(self, db_path: Path, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: List[sqlite3.Connection] = []
        # ✅ FIX AUDITORÍA: antes guardaba id(conn) (un int) en vez de la
        # conexión misma. Si un caller no llamaba release() y la conexión
        # era garbage-collected, un objeto nuevo podía reciclar esa misma
        # dirección de memoria y aparecer como "ya en uso" por error —
        # corrompiendo el tracking. Ahora se guardan las conexiones mismas
        # (referencia fuerte, sin ambigüedad de identidad).
        self._in_use: set = set()
        self._lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Inicializa el pool creando conexiones."""
        if self._initialized:
            return
        with self._lock:
            for _ in range(self.pool_size):
                conn = self._create_connection()
                if conn:
                    self._pool.append(conn)
            self._initialized = True
            logger.debug(f"SQLiteConnectionPool initialized: {len(self._pool)} conexiones")

    def _create_connection(self) -> Optional[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        except Exception as e:
            logger.error(f"Failed to create SQLite connection: {e}")
            return None

    def acquire(self, timeout: float = 5.0) -> Optional[sqlite3.Connection]:
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if self._pool:
                    conn = self._pool.pop()
                    self._in_use.add(conn)
                    return conn
                if len(self._in_use) < self.pool_size * 2:
                    conn = self._create_connection()
                    if conn:
                        self._in_use.add(conn)
                        return conn
            time.sleep(0.01)
        logger.warning(f"SQLiteConnectionPool: timeout acquiring connection after {timeout}s")
        return None

    def release(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if conn in self._in_use:
                self._in_use.discard(conn)
                try:
                    conn.execute("SELECT 1")
                    if len(self._pool) < self.pool_size:
                        self._pool.append(conn)
                    else:
                        conn.close()
                except Exception:
                    conn.close()

    def close_all(self) -> None:
        """Cierra todas las conexiones del pool."""
        with self._lock:
            for conn in self._pool:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    conn.close()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 db_pool.py:121] excepción degradada (intencional): {_d2e}")
            self._pool.clear()
            self._in_use.clear()
            self._initialized = False
            logger.debug("SQLiteConnectionPool closed")
