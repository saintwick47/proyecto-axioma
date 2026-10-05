#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — FileAuditTrail: Auditoría de operaciones de archivo
# Ruta: src/learning/file_audit.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 2 plan_harness — Trazabilidad):
#   Registra cada operación de archivo del sistema (write/move/delete)
#   con checksum SHA256, sesión, estado y propuesta asociada. Permite:
#     - get_session_operations(): qué tocó una sesión y en qué orden.
#     - verify_checksum(): ¿el archivo actual coincide con el checksum
#       del último commit registrado? (detección de modificaciones
#       posteriores no auditadas).
#
# Integración (FASE 2):
#   - PromotionGate._propose()  → record(status="proposed")
#   - PromotionGate._promote()  → record(status="committed") tras commit
#   - PromotionGate._discard()  → record(status="discarded")
#   - Fallos de commit          → record(status="failed")
#
# Diseño: nunca lanza excepciones al caller (mismo patrón TraceStore).
# Esquema: tabla `file_operations` en data/axioma.db
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports del sistema AXIOMA (safe) ────────────────────────────────────────

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


class FileOpStatus:
    """Estados de una operación de archivo auditada."""
    PROPOSED = "proposed"     # Propuesta pendiente de veredicto
    COMMITTED = "committed"   # Aplicada al filesystem (checksum del resultado)
    DISCARDED = "discarded"   # Descartada — archivo no tocado
    REVERTED = "reverted"     # Commit revertido
    FAILED = "failed"         # Falló al aplicar


# ============================================================================
# FILE AUDIT TRAIL
# ============================================================================

class FileAuditTrail:
    """
    Bitácora persistente de operaciones de archivo (tabla `file_operations`).

    Uso:
        audit = get_file_audit()
        audit.record("sess-1", "write", "/ruta/archivo.py",
                     status=FileOpStatus.COMMITTED, checksum_sha256="abc...")
        ops = audit.get_session_operations("sess-1")
        verdict = audit.verify_checksum("sess-1", "/ruta/archivo.py")
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS file_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        path TEXT NOT NULL,
        checksum_sha256 TEXT,
        status TEXT NOT NULL,
        proposal_id TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_file_operations_session
        ON file_operations (session_id, id);
    CREATE INDEX IF NOT EXISTS idx_file_operations_path
        ON file_operations (path, id);
    -- ✅ AUD-CI-001 (2026-08-26): índice compuesto para verify_checksum()
    -- y get_path_history() (WHERE session_id = ? AND path = ?).
    CREATE INDEX IF NOT EXISTS idx_file_operations_session_id_path
        ON file_operations (session_id, path);
    """

    def __init__(self, db_path: Optional[Path] = None, pool_size: int = 3):
        if not _POOL_AVAILABLE:
            raise ImportError("SQLiteConnectionPool no disponible")
        self.db_path = db_path or (
            Paths.DATA / "axioma.db" if _PATHS_AVAILABLE
            else Path("data/axioma.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._pool = SQLiteConnectionPool(self.db_path, pool_size=pool_size)
        self._pool.initialize()
        self._lock = threading.Lock()
        self._initialized = False
        self._write_errors = 0

        self._init_schema()
        self._initialized = True
        logger.info(f"FileAuditTrail initialized: {self.db_path}")

    # ── Esquema ──────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            raise RuntimeError("FileAuditTrail: no se pudo obtener conexión para init_schema")
        try:
            with self._lock:
                conn.executescript(self._SCHEMA)
                conn.commit()
        finally:
            self._pool.release(conn)

    # ── Escritura ────────────────────────────────────────────────────────────

    def record(
        self,
        session_id: str,
        operation: str,
        path: str,
        status: str = FileOpStatus.PROPOSED,
        checksum_sha256: Optional[str] = None,
        proposal_id: Optional[str] = None,
    ) -> Optional[int]:
        """
        Registra una operación de archivo. Nunca lanza excepciones.
        Si no se pasa checksum, se calcula del archivo si existe.
        """
        if not self._initialized:
            return None
        if self._write_errors > 10:
            return None
        if not checksum_sha256 and status == FileOpStatus.COMMITTED:
            checksum_sha256 = self._sha256_of(path)
        try:
            conn = self._pool.acquire(timeout=1.0)
            if conn is None:
                self._write_errors += 1
                return None
            try:
                cur = conn.execute(
                    "INSERT INTO file_operations "
                    "(session_id, operation, path, checksum_sha256, status, proposal_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, operation, path, checksum_sha256, status,
                     proposal_id, time.time()),
                )
                conn.commit()
                self._write_errors = 0
                return int(cur.lastrowid)
            finally:
                self._pool.release(conn)
        except Exception as e:
            self._write_errors += 1
            log_degraded(logger, e, "src/learning/file_audit.py:record")
            return None

    # ── Lectura / verificación ──────────────────────────────────────────────

    def get_session_operations(
        self, session_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Operaciones de archivo de una sesión, en orden cronológico."""
        return self._query(
            "SELECT id, session_id, operation, path, checksum_sha256, status, "
            "proposal_id, created_at FROM file_operations "
            "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, int(limit)),
        )

    def get_path_history(self, path: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Historial completo de un path (todas las sesiones)."""
        return self._query(
            "SELECT id, session_id, operation, checksum_sha256, status, "
            "proposal_id, created_at FROM file_operations "
            "WHERE path = ? ORDER BY id DESC LIMIT ?",
            (path, int(limit)),
        )

    def verify_checksum(self, session_id: str, path: str) -> Dict[str, Any]:
        """
        Verifica que el archivo actual coincida con el checksum del último
        COMMITTED registrado para (session_id, path).

        Returns:
            {"ok": bool, "current_checksum": ..., "last_committed": ...,
             "matches": bool, "detail": str}
        """
        ops = self._query(
            "SELECT id, checksum_sha256, status, created_at FROM file_operations "
            "WHERE session_id = ? AND path = ? AND status = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id, path, FileOpStatus.COMMITTED),
        )
        if not ops:
            return {
                "ok": False, "matches": None,
                "detail": "Sin operación COMMITTED registrada para este path en la sesión",
                "current_checksum": None, "last_committed": None,
            }
        last = ops[0]
        current = self._sha256_of(path)
        matches = current is not None and current == last["checksum_sha256"]
        return {
            "ok": True,
            "matches": matches,
            "current_checksum": current,
            "last_committed": last["checksum_sha256"],
            "detail": (
                "Checksum coincide — sin modificaciones posteriores no auditadas"
                if matches else
                "El archivo actual difiere del último commit auditado"
            ),
        }

    # ── Utilidades ───────────────────────────────────────────────────────────

    @staticmethod
    def _sha256_of(path: str) -> Optional[str]:
        """SHA256 del contenido del archivo, o None si no existe/no legible."""
        try:
            p = Path(path)
            if not p.is_file():
                return None
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            # AUD-SIL: archivo no legible → checksum None (verify_checksum
            # lo reporta como no verificable, sin romper la auditoría).
            log_degraded(logger, e, "src/learning/file_audit.py:_sha256_of")
            return None

    def get_stats(self) -> Dict[str, Any]:
        total = self._query("SELECT COUNT(*) AS n FROM file_operations")
        by_status = self._query(
            "SELECT status, COUNT(*) AS n FROM file_operations GROUP BY status"
        )
        return {
            "total_operations": total[0]["n"] if total else 0,
            "by_status": {r["status"]: r["n"] for r in by_status},
            "db_path": str(self.db_path),
        }

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if not self._initialized:
            return []
        try:
            conn = self._pool.acquire(timeout=2.0)
            if conn is None:
                return []
            try:
                conn.row_factory = __import__("sqlite3").Row
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                self._pool.release(conn)
        except Exception as e:
            log_degraded(logger, e, "src/learning/file_audit.py:_query")
            return []

    def close(self) -> None:
        if self._initialized:
            self._pool.close_all()
            self._initialized = False


# ============================================================================
# SINGLETON — get_file_audit()
# ============================================================================

_instance: Optional[FileAuditTrail] = None
_lock = threading.Lock()


def get_file_audit(db_path: Optional[Path] = None) -> Optional[FileAuditTrail]:
    """Singleton con inicialización lazy. None si la infraestructura falta."""
    global _instance
    with _lock:
        if _instance is None:
            try:
                _instance = FileAuditTrail(db_path=db_path)
            except Exception as e:
                logger.warning(f"[FileAuditTrail] No disponible: {e}")
                _instance = None
        return _instance
