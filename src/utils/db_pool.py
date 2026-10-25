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
import atexit
import logging
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ✅ 2026-10-25: registro DÉBIL de pools vivos. `close_all()` existía pero
# nadie lo llamaba (medido: 49 conexiones SQLite abiertas al terminar
# test_axioma_03 → ResourceWarning al cerrar el intérprete). El cierre
# ordenado (dispatcher.cleanup) usa `close_all_pools()`; el `atexit` es la
# red de seguridad para procesos que no pasan por el dispatcher (CLI de una
# sola corrida, tests, scripts). WeakSet: no alarga la vida de ningún pool
# y cerrar dos veces el mismo pool es idempotente.
_live_pools: "weakref.WeakSet[SQLiteConnectionPool]" = weakref.WeakSet()
_live_pools_lock = threading.Lock()


def _cerrar_conexiones(pool: list, in_use: set) -> None:
    """Cierra las conexiones de un pool que muere sin `close_all()`.

    Lo usa `weakref.finalize` en el `__init__` del pool: un KnowledgeGraph
    temporal de un test, un cache que se descarta, etc. Sin esto las
    conexiones quedaban como "unclosed database" hasta que el intérprete
    las recolectaba (de ahí los ResourceWarning de SQLite).
    """
    for conn in list({id(c): c for c in (*pool, *in_use)}.values()):
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as _d2e:
            logger.debug(f"[D2 db_pool.py:finalize] checkpoint omitido: {_d2e}")
        try:
            conn.close()
        except Exception as _d2e:
            logger.debug(f"[D2 db_pool.py:finalize] close falló: {_d2e}")


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
        with _live_pools_lock:
            _live_pools.add(self)
        # ✅ 2026-10-25: si el pool muere sin close_all() (KnowledgeGraph
        # temporal de un test, cache descartado) el finalizador cierra sus
        # conexiones. Sin esto quedaban "unclosed database" hasta que el
        # intérprete las recolectaba → ResourceWarning.
        self._finalizer = weakref.finalize(
            self, _cerrar_conexiones, self._pool, self._in_use
        )

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
        """Cierra TODAS las conexiones del pool: libres y prestadas.

        ✅ 2026-10-25: antes solo cerraba `_pool` y vaciaba `_in_use` sin
        cerrar esas conexiones (una conexión prestada quedaba abierta para
        siempre). En el apagado no hay prestadas, pero cerrarlas es lo
        correcto y hace idempotente el cierre.
        """
        with self._lock:
            conns = list({id(c): c for c in (*self._pool, *self._in_use)}.values())
            self._pool.clear()
            self._in_use.clear()
            self._initialized = False
            for conn in conns:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception as _d2e:
                    logger.debug(f"[D2 db_pool.py:close_all] checkpoint omitido: {_d2e}")
                try:
                    conn.close()
                except Exception as _d2e:
                    logger.debug(f"[D2 db_pool.py:close_all] close falló: {_d2e}")
            logger.debug("SQLiteConnectionPool closed (%d conexiones)", len(conns))


def close_all_pools() -> int:
    """Cierra todos los pools SQLite vivos. Idempotente; devuelve cuántos tocó.

    Se usa en el cierre ordenado (Dispatcher.cleanup) y en atexit como red
    de seguridad para procesos sin dispatcher (tests, scripts, CLI one-shot).
    """
    with _live_pools_lock:
        pools = list(_live_pools)
    for pool in pools:
        try:
            pool.close_all()
        except Exception as _d2e:  # el apagado nunca debe lanzar
            logger.debug(f"[D2 db_pool.py:close_all_pools] pool no cerrado: {_d2e}")
    return len(pools)


atexit.register(close_all_pools)
