#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — SessionEventLog: Bitácora de eventos de sesión
# Ruta: src/learning/session_event_log.py
# Autor: SaintWick
# Fecha: 2026-08-26
# Propósito (FASE 2 plan_harness — Trazabilidad):
#   Persiste eventos estructurados de cada sesión (mensajes, tools,
#   cambios de modo, forks) en SQLite. Es la base para:
#     - derive_messages(): reconstruir el historial de una sesión
#       sin depender de la memoria viva (replay, como el session-log
#       del harness de referencia).
#     - verify_invariant(): invariantes de integridad del log
#       (orden temporal monotónico, conteo de eventos).
#     - fork(): clonar el log de una sesión a otra (sesiones hijas).
#
# Diseño (referencia: deepseek-harness session-mode — el log ES el
# estado; nada se muta fuera de banda):
#   - append() es la ÚNICA vía de escritura del log.
#   - Los eventos se leen por orden de id (orden de append).
#   - Nunca lanza excepciones al caller (mismo patrón TraceStore):
#     si la BD falla, el sistema sigue funcionando sin trazabilidad.
#
# Esquema: tabla `session_events` en data/axioma.db
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import hashlib
import json
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


# ============================================================================
# MODELO DE DATOS
# ============================================================================

class SessionEventType:
    """Tipos de evento canónicos del log de sesión."""
    MESSAGE_ADDED = "message_added"
    TOOL_CALLED = "tool_called"
    MODE_SWITCH = "mode_switch"
    SESSION_FORK = "session_fork"
    FILE_PROPOSED = "file_proposed"
    FILE_PROMOTED = "file_promoted"
    FILE_REVERTED = "file_reverted"
    SYSTEM = "system"


# ============================================================================
# SESSION EVENT LOG
# ============================================================================

class SessionEventLog:
    """
    Bitácora persistente de eventos de sesión (tabla `session_events`).

    Uso:
        log = get_session_event_log()
        event_id = log.append("sess-1", SessionEventType.MESSAGE_ADDED,
                              {"role": "user", "chars": 42})
        messages = log.derive_messages("sess-1")
        log.verify_invariant("sess-1")
        log.fork("sess-1", "sess-1-child")
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS session_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_session_events_session
        ON session_events (session_id, id);
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
        logger.info(f"SessionEventLog initialized: {self.db_path}")

    # ── Esquema ──────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._pool.acquire(timeout=5.0)
        if conn is None:
            raise RuntimeError("SessionEventLog: no se pudo obtener conexión para init_schema")
        try:
            with self._lock:
                conn.executescript(self._SCHEMA)
                conn.commit()
        finally:
            self._pool.release(conn)

    # ── Escritura (única vía: append) ────────────────────────────────────────

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """
        Agrega un evento al log de la sesión. Única vía de escritura.

        Returns:
            id del evento insertado, o None si falló (no bloquea al caller).
        """
        if not self._initialized:
            return None
        if self._write_errors > 10:
            return None
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        created_at = time.time()
        try:
            conn = self._pool.acquire(timeout=1.0)
            if conn is None:
                self._write_errors += 1
                return None
            try:
                cur = conn.execute(
                    "INSERT INTO session_events (session_id, event_type, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, event_type, payload_json, created_at),
                )
                conn.commit()
                self._write_errors = 0
                return int(cur.lastrowid)
            finally:
                self._pool.release(conn)
        except Exception as e:
            self._write_errors += 1
            log_degraded(logger, e, "src/learning/session_event_log.py:append")
            return None

    # ── Lectura / derivación ────────────────────────────────────────────────

    def events(self, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Eventos de la sesión en orden de append (id ascendente)."""
        return self._query(
            "SELECT id, session_id, event_type, payload, created_at "
            "FROM session_events WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, int(limit)),
        )

    def derive_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Reconstruye el historial de mensajes de la sesión a partir de los
        eventos MESSAGE_ADDED — replay del log (misma filosofía que el
        session-log del harness: el log ES el estado).

        Returns:
            Lista de {"role": ..., "content_preview": ..., "chars": ...,
            "created_at": ...} en orden cronológico.
        """
        msgs: List[Dict[str, Any]] = []
        for ev in self.events(session_id, limit=100000):
            if ev["event_type"] != SessionEventType.MESSAGE_ADDED:
                continue
            try:
                payload = json.loads(ev["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            msgs.append({
                "role": payload.get("role", "unknown"),
                "content_preview": payload.get("content_preview", "")[:200],
                "chars": payload.get("chars", 0),
                "created_at": ev["created_at"],
            })
        return msgs

    def verify_invariant(self, session_id: str) -> Dict[str, Any]:
        """
        Verifica invariantes de integridad del log de la sesión:
          1. ids estrictamente crecientes en orden temporal (monotonía).
          2. created_at no decreciente entre eventos consecutivos.
          3. payloads JSON parseables.
        Returns:
            {"ok": bool, "checks": {...}, "events": int}
        """
        evs = self.events(session_id, limit=100000)
        monotonic_ids = all(
            evs[i]["id"] < evs[i + 1]["id"] for i in range(len(evs) - 1)
        )
        monotonic_time = all(
            evs[i]["created_at"] <= evs[i + 1]["created_at"]
            for i in range(len(evs) - 1)
        )
        parseable = 0
        for ev in evs:
            try:
                json.loads(ev["payload"] or "{}")
                parseable += 1
            except (json.JSONDecodeError, TypeError) as e:
                # AUD-SIL-043: payload corrupto cuenta como no-parseable
                # (el invariant lo reporta) — sin log por evento.
                logger.debug(f"[SessionEventLog] payload no parseable: {e}")
        ok = monotonic_ids and monotonic_time and parseable == len(evs)
        return {
            "ok": ok,
            "events": len(evs),
            "checks": {
                "monotonic_ids": monotonic_ids,
                "monotonic_time": monotonic_time,
                "payloads_parseable": parseable == len(evs),
            },
        }

    def fork(self, source_session_id: str, new_session_id: str) -> int:
        """
        Clona el log de una sesión a otra (sesión hija). Registra un evento
        SESSION_FORK en la hija con el hash del estado fuente.

        Returns:
            Cantidad de eventos copiados.
        """
        evs = self.events(source_session_id, limit=100000)
        copied = 0
        for ev in evs:
            payload = {}
            try:
                payload = json.loads(ev["payload"] or "{}")
            except (json.JSONDecodeError, TypeError) as e:
                # AUD-SIL-043: payload corrupto → estado vacío (degradación),
                # el fork sigue copiando el evento.
                logger.debug(f"[SessionEventLog] fork payload error: {e}")
            payload["forked_from"] = ev["id"]
            if self.append(new_session_id, ev["event_type"], payload):
                copied += 1
        # Evento de marca del fork
        source_hash = hashlib.sha256(
            f"{source_session_id}:{len(evs)}".encode()
        ).hexdigest()[:16]
        self.append(
            new_session_id,
            SessionEventType.SESSION_FORK,
            {"source_session": source_session_id, "source_events": len(evs),
             "source_hash": source_hash},
        )
        return copied

    # ── Stats / utilidades ───────────────────────────────────────────────────

    def session_count(self) -> Dict[str, Any]:
        rows = self._query(
            "SELECT session_id, COUNT(*) AS n FROM session_events GROUP BY session_id"
        )
        return {r["session_id"]: r["n"] for r in rows}

    def get_stats(self) -> Dict[str, Any]:
        total = self._query("SELECT COUNT(*) AS n FROM session_events")
        return {
            "total_events": total[0]["n"] if total else 0,
            "sessions": len(self.session_count()),
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
            log_degraded(logger, e, "src/learning/session_event_log.py:_query")
            return []

    def close(self) -> None:
        if self._initialized:
            self._pool.close_all()
            self._initialized = False


# ============================================================================
# SINGLETON — get_session_event_log()
# ============================================================================

_instance: Optional[SessionEventLog] = None
_lock = threading.Lock()


def get_session_event_log(db_path: Optional[Path] = None) -> Optional[SessionEventLog]:
    """Singleton con inicialización lazy. None si la infraestructura falta."""
    global _instance
    with _lock:
        if _instance is None:
            try:
                _instance = SessionEventLog(db_path=db_path)
            except Exception as e:
                logger.warning(f"[SessionEventLog] No disponible: {e}")
                _instance = None
        return _instance
