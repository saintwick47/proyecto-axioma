#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.1 — TraceStore: Persistencia de trazas de ejecución
# ═══════════════════════════════════════════════════════════════
# Ruta: src/learning/trace_store.py
# Autor: SaintWick
# Fecha: 2026-05-24
# Propósito: Persiste cada ejecución del Dispatcher como traza
#            estructurada en SQLite. Base para HeuristicRouter y
#            análisis de rendimiento histórico.
#
# ✅ v0.1.1: FIX AUDITORÍA (L7) — record_async() creaba un
#   threading.Thread(daemon=True) nuevo por cada llamada (se invoca en
#   cada request desde _record_metric()) — sin pool ni límite de threads
#   concurrentes, y al ser daemon=True las escrituras en vuelo se podían
#   perder en un shutdown. Reemplazado por un ThreadPoolExecutor acotado
#   (writer_workers, default 2); close() ahora espera (wait=True) a que
#   las escrituras pendientes terminen antes de cerrar el pool SQLite.
#
# Integración:
#   - Hook en DispatcherOperationsMixin._record_metric()
#   - Hook en InterpreterLoop.run() al finalizar (éxito y agotamiento)
#   - NO modifica interfaces públicas existentes
#   - Import seguro con fallback: si falla, el sistema sigue normal
#
# Esquema: tabla `traces` en Paths.DATA / "traces.db"
#   (separado de axioma.db para no contaminar esquema de memoria)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports del sistema AXIOMA ───────────────────────────────────────────────

try:
    from config.paths import Paths
    _PATHS_AVAILABLE = True
except ImportError:
    _PATHS_AVAILABLE = False

try:
    from src.utils.db_pool import SQLiteConnectionPool
    _POOL_AVAILABLE = True
except ImportError:
    _POOL_AVAILABLE = False


# ============================================================================
# MODELO DE DATOS
# ============================================================================

@dataclass
class Trace:
    """
    Traza de una ejecución del Dispatcher.

    Campos obligatorios: task_type, handler, latency_ms, success.
    Todos los demás son opcionales para no bloquear el registro
    si algún contexto no está disponible.
    """
    task_type: str
    handler: str
    latency_ms: float
    success: bool

    # Contexto de la request
    session_id: Optional[str] = None
    query_len: int = 0                   # len(message.content) — no guardamos el texto
    confidence: float = 0.0

    # Modelo y tokens
    model: str = ""
    tokens_prompt: int = 0
    tokens_generated: int = 0

    # Herramientas usadas
    tool_calls: List[str] = field(default_factory=list)

    # Loop autónomo (InterpreterLoop)
    loop_attempts: int = 0               # 0 = no fue loop autónomo
    loop_stop_reason: str = ""

    # Reward calculado post-hoc (None = aún sin evaluar)
    reward: Optional[float] = None

    # ✅ Fase 7: contrato de código verificable (Fase 1-4) — 0/False en
    # cualquier trace que no sea de una tarea de código, no solo default
    # técnico. code_retries cuenta reintentos del repair loop EXTERNO
    # (dispatcher_handlers.py, Fase 4), no los internos de CoderAgent.
    code_contract_complete: bool = False
    code_validation_passed: bool = False
    code_retries: int = 0
    code_truncated: bool = False

    # Timestamp automático
    ts: float = field(default_factory=time.time)

    def to_row(self) -> tuple:
        """Convierte a tupla para INSERT SQLite."""
        return (
            self.ts,
            self.task_type,
            self.handler,
            self.latency_ms,
            int(self.success),
            self.session_id,
            self.query_len,
            self.confidence,
            self.model,
            self.tokens_prompt,
            self.tokens_generated,
            json.dumps(self.tool_calls),
            self.loop_attempts,
            self.loop_stop_reason,
            self.reward,
            int(self.code_contract_complete),
            int(self.code_validation_passed),
            self.code_retries,
            int(self.code_truncated),
        )


# ============================================================================
# TRACESTORE
# ============================================================================

class TraceStore:
    """
    Persistencia de trazas en SQLite via SQLiteConnectionPool.

    Diseño:
    - WAL mode (heredado del pool) → escrituras no bloquean lecturas
    - Escrituras en background thread → _record_metric() no añade latencia
    - record() nunca lanza excepciones hacia el caller
    - Singleton lazy vía get_trace_store()
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS traces (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              REAL    NOT NULL,
        task_type       TEXT    NOT NULL,
        handler         TEXT    NOT NULL,
        latency_ms      REAL    NOT NULL,
        success         INTEGER NOT NULL,
        session_id      TEXT,
        query_len       INTEGER DEFAULT 0,
        confidence      REAL    DEFAULT 0.0,
        model           TEXT    DEFAULT '',
        tokens_prompt   INTEGER DEFAULT 0,
        tokens_generated INTEGER DEFAULT 0,
        tool_calls      TEXT    DEFAULT '[]',
        loop_attempts   INTEGER DEFAULT 0,
        loop_stop_reason TEXT   DEFAULT '',
        reward          REAL,
        code_contract_complete INTEGER DEFAULT 0,
        code_validation_passed INTEGER DEFAULT 0,
        code_retries    INTEGER DEFAULT 0,
        code_truncated  INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_traces_ts        ON traces(ts);
    CREATE INDEX IF NOT EXISTS idx_traces_task_type ON traces(task_type);
    CREATE INDEX IF NOT EXISTS idx_traces_handler   ON traces(handler);
    CREATE INDEX IF NOT EXISTS idx_traces_success   ON traces(success);
    """

    # ✅ Fase 7: columnas para instalaciones EXISTENTES — CREATE TABLE IF
    # NOT EXISTS de _SCHEMA no altera una tabla que ya existe, así que
    # una base de datos de traces.db previa a este cambio se queda sin
    # estas 4 columnas a menos que corra este ALTER TABLE (ver
    # _migrate_schema más abajo). Nombre y tipo deben coincidir 1:1 con
    # los agregados en _SCHEMA arriba.
    _MIGRATIONS = [
        "ALTER TABLE traces ADD COLUMN code_contract_complete INTEGER DEFAULT 0",
        "ALTER TABLE traces ADD COLUMN code_validation_passed INTEGER DEFAULT 0",
        "ALTER TABLE traces ADD COLUMN code_retries INTEGER DEFAULT 0",
        "ALTER TABLE traces ADD COLUMN code_truncated INTEGER DEFAULT 0",
    ]

    _INSERT = """
    INSERT INTO traces (
        ts, task_type, handler, latency_ms, success,
        session_id, query_len, confidence,
        model, tokens_prompt, tokens_generated,
        tool_calls, loop_attempts, loop_stop_reason, reward,
        code_contract_complete, code_validation_passed, code_retries, code_truncated
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def __init__(self, db_path: Optional[Path] = None, pool_size: int = 3, writer_workers: int = 2):
        if not _POOL_AVAILABLE:
            raise ImportError("SQLiteConnectionPool no disponible")

        self.db_path = db_path or (
            Paths.DATA / "traces.db" if _PATHS_AVAILABLE
            else Path("data/traces.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._pool = SQLiteConnectionPool(self.db_path, pool_size=pool_size)
        self._pool.initialize()
        self._lock = threading.Lock()
        self._initialized = False
        self._write_errors = 0  # contador para backoff si hay errores repetidos

        # ✅ FIX AUDITORÍA (L7): record_async() creaba un threading.Thread
        # (daemon=True) NUEVO por cada llamada — sin pool, sin límite de
        # threads concurrentes, y al ser daemon=True las escrituras en
        # vuelo se podían perder en un shutdown (no se esperaba a que
        # terminaran). Se llama en CADA request desde _record_metric() —
        # bajo carga sostenida esto podía acumular threads sin límite.
        # Ahora usa un ThreadPoolExecutor acotado (writer_workers, default
        # 2) — mismo trabajo, sin threads sin límite, y close() espera a
        # que las escrituras pendientes terminen antes de cerrar el pool
        # de conexiones SQLite.
        self._write_executor = ThreadPoolExecutor(
            max_workers=writer_workers, thread_name_prefix="trace-store-write",
        )

        self._init_schema()
        self._migrate_schema()
        self._initialized = True
        logger.info(f"TraceStore initialized: {self.db_path}")

    # ── Esquema ──────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            raise RuntimeError("TraceStore: no se pudo obtener conexión para init_schema")
        try:
            with self._lock:
                conn.executescript(self._SCHEMA)
                conn.commit()
        finally:
            self._pool.release(conn)

    def _migrate_schema(self) -> None:
        """
        ✅ Fase 7: aplica ALTER TABLE para columnas nuevas contra una
        traces.db que ya existía antes de este cambio. CREATE TABLE IF
        NOT EXISTS (arriba) no toca una tabla ya creada, así que sin
        esto las 4 columnas nuevas faltarían en cualquier instalación
        previa y el INSERT fallaría con "table traces has no column
        named code_contract_complete" (o el que sea). Cada ALTER va en
        su propio try/except — SQLite no soporta "ADD COLUMN IF NOT
        EXISTS" nativamente, así que en una base ya migrada (o una
        instalación nueva, donde _SCHEMA ya creó las columnas) el error
        de "duplicate column name" es esperado y se ignora.
        """
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            logger.warning("TraceStore: no se pudo obtener conexión para _migrate_schema")
            return
        try:
            with self._lock:
                for statement in self._MIGRATIONS:
                    try:
                        conn.execute(statement)
                        conn.commit()
                    except Exception as e:
                        if "duplicate column" not in str(e).lower():
                            logger.warning(f"TraceStore: migración falló ({statement}): {e}")
        finally:
            self._pool.release(conn)

    # ── Escritura ────────────────────────────────────────────────────────────

    def record(self, trace: Trace) -> None:
        """
        Persiste una traza. Nunca lanza excepciones — el Dispatcher
        no debe fallar por un error de tracing.

        Ejecuta la escritura en el thread actual (ya es llamado desde
        _record_metric que puede estar en background). Pool size=3
        garantiza que no bloquea el Dispatcher principal.
        """
        if not self._initialized:
            return
        # Backoff si hay muchos errores consecutivos (BD corrupta, disco lleno)
        if self._write_errors > 10:
            return
        try:
            conn = self._pool.acquire(timeout=1.0)  # timeout corto — no bloquear
            if conn is None:
                self._write_errors += 1
                return
            try:
                conn.execute(self._INSERT, trace.to_row())
                conn.commit()
                self._write_errors = 0  # reset en éxito
            finally:
                self._pool.release(conn)
        except Exception as e:
            self._write_errors += 1
            log_degraded(logger, e, "src/learning/trace_store.py:record")

    def record_async(self, trace: Trace) -> None:
        """
        Versión fire-and-forget para llamar desde corrutinas sin await.

        ✅ FIX AUDITORÍA (L7): antes lanzaba un threading.Thread(daemon=True)
        nuevo por llamada. Ahora usa el ThreadPoolExecutor acotado del
        __init__ — mismo comportamiento fire-and-forget para el caller,
        pero sin threads sin límite y con drenado ordenado en close().
        """
        if not self._initialized:
            return
        try:
            self._write_executor.submit(self.record, trace)
        except RuntimeError:
            # Executor ya en shutdown (cierre en curso) — no bloquear al caller
            logger.debug("TraceStore.record_async: executor en shutdown, traza descartada")

    # ── Lectura ──────────────────────────────────────────────────────────────

    def recent(self, n: int = 100) -> List[Dict[str, Any]]:
        """Últimas N trazas ordenadas por timestamp desc."""
        return self._query(
            "SELECT * FROM traces ORDER BY ts DESC LIMIT ?", (n,)
        )

    def by_task_type(self, task_type: str, n: int = 50) -> List[Dict[str, Any]]:
        """Trazas filtradas por task_type."""
        return self._query(
            "SELECT * FROM traces WHERE task_type = ? ORDER BY ts DESC LIMIT ?",
            (task_type, n),
        )

    def by_handler(self, handler: str, n: int = 50) -> List[Dict[str, Any]]:
        """Trazas filtradas por handler."""
        return self._query(
            "SELECT * FROM traces WHERE handler = ? ORDER BY ts DESC LIMIT ?",
            (handler, n),
        )

    def latency_stats(self, task_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Estadísticas de latencia (mean, p50, p95) por task_type o global.
        Útil para HeuristicRouter y alertas de regresión.
        """
        where = "WHERE task_type = ?" if task_type else ""
        params = (task_type,) if task_type else ()
        rows = self._query(
            f"SELECT latency_ms FROM traces {where} ORDER BY latency_ms",
            params,
        )
        if not rows:
            return {"count": 0}
        vals = [r["latency_ms"] for r in rows]
        n = len(vals)
        return {
            "count": n,
            "mean_ms": round(sum(vals) / n, 2),
            "p50_ms": round(vals[n // 2], 2),
            "p95_ms": round(vals[int(n * 0.95)], 2),
            "min_ms": round(vals[0], 2),
            "max_ms": round(vals[-1], 2),
        }

    def success_rate(self, task_type: Optional[str] = None) -> float:
        """Tasa de éxito [0.0-1.0] global o por task_type."""
        where = "WHERE task_type = ?" if task_type else ""
        params = (task_type,) if task_type else ()
        rows = self._query(
            f"SELECT success FROM traces {where}", params
        )
        if not rows:
            return 1.0
        return sum(r["success"] for r in rows) / len(rows)

    def get_stats(self) -> Dict[str, Any]:
        """Resumen general del store para get_stats() del Dispatcher."""
        rows = self._query(
            "SELECT COUNT(*) as total, "
            "SUM(success) as ok, "
            "AVG(latency_ms) as avg_lat "
            "FROM traces"
        )
        if not rows:
            return {"available": True, "total": 0}
        r = rows[0]
        return {
            "available": True,
            "db_path": str(self.db_path),
            "total_traces": r.get("total", 0),
            "success_count": r.get("ok", 0),
            "avg_latency_ms": round(r.get("avg_lat") or 0, 2),
            "write_errors": self._write_errors,
        }

    # ── Interno ──────────────────────────────────────────────────────────────

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Ejecuta SELECT y retorna lista de dicts. Nunca lanza al caller."""
        conn = self._pool.acquire(timeout=2.0)
        if conn is None:
            return []
        try:
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            log_degraded(logger, e, "src/learning/trace_store.py:_query")
            return []
        finally:
            self._pool.release(conn)

    def close(self) -> None:
        """Cierra el pool. Llamar desde Dispatcher.cleanup()."""
        # ✅ FIX AUDITORÍA (L7): esperar (wait=True) a que las escrituras
        # en vuelo en el ThreadPoolExecutor terminen ANTES de cerrar el
        # pool de conexiones SQLite — antes, con threads daemon sueltos,
        # una escritura en curso al momento del shutdown se podía perder
        # silenciosamente.
        self._write_executor.shutdown(wait=True, cancel_futures=False)
        self._pool.close_all()
        logger.info("TraceStore closed")


# ============================================================================
# SINGLETON
# ============================================================================

_store: Optional[TraceStore] = None
_store_lock = threading.Lock()


def get_trace_store() -> Optional[TraceStore]:
    """
    Singleton lazy. Retorna None si no se puede inicializar
    (entorno sin SQLite, paths no disponibles, etc.).
    El caller nunca debe fallar si retorna None.
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        try:
            _store = TraceStore()
            return _store
        except Exception as e:
            logger.warning(f"TraceStore no disponible (non-fatal): {e}")
            return None
