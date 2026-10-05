#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.1 — State Manager para Agent Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/state_manager.py
# Autor: SaintWick
# Fecha: 2026-05-02
# Propósito: Gestionar estado del ciclo del agente: transiciones
#            válidas entre LoopState, fase actual (LoopPhase),
#            snapshots en memoria y checkpointing persistente en
#            SQLite (retención configurable, eviction por sesión,
#            WAL mode, thread-safe) para recuperación entre
#            reinicios. El store cae a fallback silencioso si
#            SQLite no está disponible — nunca rompe el loop.
# Versión: 0.4.1
# ═══════════════════════════════════════════════════════════════
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List

# ✅ FIX: Importar desde types.py (NO desde agent_loop.py)
from .types import LoopPhase, LoopState

# ✅ v0.4.0: Import seguro de Paths — fallback a ~/.axioma si no disponible
try:
    from config.paths import Paths
    _DEFAULT_DB_PATH = Paths.DATA / "checkpoints.db"
except ImportError:
    _DEFAULT_DB_PATH = Path.home() / ".axioma" / "checkpoints.db"

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CHECKPOINT STORE — Persistencia SQLite de snapshots
# ═══════════════════════════════════════════════════════════════
class _CheckpointStore:
    """
    Persistencia SQLite para snapshots del StateManager.
    Diseño:
    - sqlite3 stdlib — cero dependencias externas.
    - Una tabla: `checkpoints` con índice compuesto (session_id, timestamp).
    - Cada snapshot se serializa como JSON en la columna `data`.
    - WAL mode para concurrencia sin bloqueos de escritura.
    - Retención configurable: purge automático tras N días.
    - Thread-safe: check_same_thread=False + threading.Lock propio.
    - Fallback silencioso ante cualquier error de I/O — no rompe el loop.
    """
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS checkpoints (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT    NOT NULL,
        state       TEXT    NOT NULL,
        phase       TEXT,
        iteration   INTEGER NOT NULL DEFAULT 0,
        timestamp   TEXT    NOT NULL,
        data        TEXT    NOT NULL,
        created_at  TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints (session_id, timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_checkpoints_created
    ON checkpoints (created_at);
    """
    def __init__(
        self,
        db_path: Path = _DEFAULT_DB_PATH,
        retention_days: int = 7,
        max_snapshots_per_session: int = 100,
    ) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._max_per_session = max_snapshots_per_session
        self._lock = threading.Lock()
        self._available = False
        self._conn: Optional[sqlite3.Connection] = None
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(db_path),
                check_same_thread=False,
                timeout=5.0,
            )
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()
            self._available = True
            logger.info(f"[Checkpoint] SQLite inicializado: {db_path}")
            self._purge_old()
        except Exception as e:
            logger.warning(f"[Checkpoint] SQLite no disponible — checkpointing desactivado: {e}")

    # ── Escritura ─────────────────────────────────────────────────────────────
    def save(self, session_id: str, snapshot_dict: Dict[str, Any]) -> bool:
        if not self._available or self._conn is None:
            return False
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._conn.execute(
                    """INSERT INTO checkpoints
                    (session_id, state, phase, iteration, timestamp, data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        snapshot_dict.get("state", ""),
                        snapshot_dict.get("phase"),
                        snapshot_dict.get("iteration", 0),
                        snapshot_dict.get("timestamp", now_iso),
                        json.dumps(snapshot_dict, ensure_ascii=False),
                        now_iso,
                    ),
                )
                self._conn.commit()
                self._evict_session(session_id)
                return True
        except Exception as e:
            logger.debug(f"[Checkpoint] save error: {e}")
            return False

    # ── Lectura ───────────────────────────────────────────────────────────────
    def load_last(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not self._available or self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(
                    """SELECT data FROM checkpoints
                    WHERE session_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1""",
                    (session_id,),
                )
                row = cur.fetchone()
                if row:
                    return json.loads(row[0])
                return None
        except Exception as e:
            logger.debug(f"[Checkpoint] load_last error: {e}")
            return None

    def load_history(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not self._available or self._conn is None:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(
                    """SELECT data FROM checkpoints
                    WHERE session_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                    (session_id, limit),
                )
                rows = cur.fetchall()
                return [json.loads(r[0]) for r in rows]
        except Exception as e:
            logger.debug(f"[Checkpoint] load_history error: {e}")
            return []

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._available or self._conn is None:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(
                    """SELECT session_id, state, MAX(timestamp) as last_seen,
                    COUNT(*) as snapshot_count
                    FROM checkpoints
                    GROUP BY session_id
                    ORDER BY last_seen DESC
                    LIMIT ?""",
                    (limit,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "session_id": r[0],
                        "last_state": r[1],
                        "last_seen": r[2],
                        "snapshot_count": r[3],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.debug(f"[Checkpoint] list_sessions error: {e}")
            return []

    # ── Mantenimiento ─────────────────────────────────────────────────────────
    def _evict_session(self, session_id: str) -> None:
        try:
            self._conn.execute(
                """DELETE FROM checkpoints
                WHERE session_id = ?
                AND id NOT IN (
                    SELECT id FROM checkpoints
                    WHERE session_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                )""",
                (session_id, session_id, self._max_per_session),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug(f"[Checkpoint] evict error: {e}")

    def _purge_old(self) -> int:
        if not self._available or self._conn is None:
            return 0
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            ).isoformat()
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM checkpoints WHERE created_at < ?", (cutoff,)
                )
                self._conn.commit()
                deleted = cur.rowcount
                if deleted:
                    logger.info(f"[Checkpoint] Purge: {deleted} entries > {self._retention_days}d eliminadas")
                return deleted
        except Exception as e:
            logger.debug(f"[Checkpoint] purge error: {e}")
            return 0

    def delete_session(self, session_id: str) -> bool:
        if not self._available or self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM checkpoints WHERE session_id = ?", (session_id,)
                )
                self._conn.commit()
                return True
        except Exception as e:
            logger.debug(f"[Checkpoint] delete_session error: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        if not self._available or self._conn is None:
            return {"available": False, "db_path": str(self._db_path)}
        try:
            with self._lock:
                total = self._conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
                sessions = self._conn.execute("SELECT COUNT(DISTINCT session_id) FROM checkpoints").fetchone()[0]
                size_bytes = self._db_path.stat().st_size if self._db_path.exists() else 0
                return {
                    "available": True,
                    "db_path": str(self._db_path),
                    "total_snapshots": total,
                    "total_sessions": sessions,
                    "db_size_kb": round(size_bytes / 1024, 1),
                    "retention_days": self._retention_days,
                    "max_per_session": self._max_per_session,
                }
        except Exception as e:
            return {"available": False, "error": str(e)}

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 state_manager.py:265] excepción degradada (intencional): {_d2e}")
        self._conn = None
        self._available = False

# ═══════════════════════════════════════════════════════════════
# ✅ v0.4.1: FIX CRÍTICO — Agregado decorador @dataclass
# ═══════════════════════════════════════════════════════════════
@dataclass
class StateSnapshot:
    """Snapshot del estado en un momento dado."""
    state: LoopState
    phase: Optional[LoopPhase] = None
    iteration: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "state": self.state.value,
            "phase": self.phase.value if self.phase else None,
            "iteration": self.iteration,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

class StateManager:
    """
    Gestiona el estado del Agent Loop.
    """
    VALID_TRANSITIONS = {
        LoopState.IDLE: [LoopState.RUNNING],
        LoopState.RUNNING: [LoopState.PAUSED, LoopState.COMPLETED, LoopState.FAILED, LoopState.STOPPED],
        LoopState.PAUSED: [LoopState.RUNNING, LoopState.STOPPED],
        LoopState.COMPLETED: [LoopState.IDLE],
        LoopState.FAILED: [LoopState.IDLE],
        LoopState.STOPPED: [LoopState.IDLE],
    }

    def __init__(
        self,
        session_id: Optional[str] = None,
        enable_checkpointing: bool = True,
        checkpoint_db_path: Optional[Path] = None,
        checkpoint_retention_days: int = 7,
    ):
        self._state = LoopState.IDLE
        self._phase: Optional[LoopPhase] = None
        self._iteration = 0
        self._started_at: Optional[datetime] = None
        self._completed_at: Optional[datetime] = None
        self._error: Optional[str] = None
        self._snapshots: list = []
        self._metadata: Dict[str, Any] = {}

        self._session_id: Optional[str] = session_id
        self._checkpoint: Optional[_CheckpointStore] = None
        if enable_checkpointing and session_id is not None:
            _path = checkpoint_db_path or _DEFAULT_DB_PATH
            self._checkpoint = _CheckpointStore(
                db_path=_path,
                retention_days=checkpoint_retention_days,
            )
            logger.debug(f"StateManager initialized — session={session_id}, checkpointing={'on' if self._checkpoint else 'off'}")

    @property
    def state(self) -> LoopState:
        return self._state
    @property
    def current_phase(self) -> Optional[LoopPhase]:
        return self._phase
    @property
    def iteration(self) -> int:
        return self._iteration
    @property
    def is_running(self) -> bool:
        return self._state == LoopState.RUNNING
    @property
    def is_terminal(self) -> bool:
        return self._state in [LoopState.COMPLETED, LoopState.FAILED, LoopState.STOPPED]

    # ═══════════════════════════════════════════════════════════
    # TRANSICIONES DE ESTADO
    # ═══════════════════════════════════════════════════════════
    def start(self) -> bool:
        if not self._can_transition(LoopState.RUNNING):
            logger.warning(f"Invalid state transition: {self._state} → RUNNING")
            return False
        self._state = LoopState.RUNNING
        self._started_at = datetime.now(timezone.utc)
        self._completed_at = None
        self._error = None
        self._iteration = 0
        self._phase = None
        self._take_snapshot()
        logger.info(f"StateManager: started at {self._started_at}")
        return True

    def complete(self) -> bool:
        if not self._can_transition(LoopState.COMPLETED):
            logger.warning(f"Invalid state transition: {self._state} → COMPLETED")
            return False
        self._state = LoopState.COMPLETED
        self._completed_at = datetime.now(timezone.utc)
        self._take_snapshot()
        logger.info(f"StateManager: completed at {self._completed_at}")
        return True

    def fail(self, error: str) -> bool:
        if not self._can_transition(LoopState.FAILED):
            logger.warning(f"Invalid state transition: {self._state} → FAILED")
            return False
        self._state = LoopState.FAILED
        self._error = error
        self._completed_at = datetime.now(timezone.utc)
        self._take_snapshot()
        logger.error(f"StateManager: failed with error: {error}")
        return True

    def stop(self) -> bool:
        if not self._can_transition(LoopState.STOPPED):
            logger.warning(f"Invalid state transition: {self._state} → STOPPED")
            return False
        self._state = LoopState.STOPPED
        self._completed_at = datetime.now(timezone.utc)
        self._take_snapshot()
        logger.info(f"StateManager: stopped at {self._completed_at}")
        return True

    def pause(self) -> bool:
        if not self._can_transition(LoopState.PAUSED):
            logger.warning(f"Invalid state transition: {self._state} → PAUSED")
            return False
        self._state = LoopState.PAUSED
        self._take_snapshot()
        logger.info(f"StateManager: paused")
        return True

    def resume(self) -> bool:
        if not self._can_transition(LoopState.RUNNING):
            logger.warning(f"Invalid state transition: {self._state} → RUNNING")
            return False
        self._state = LoopState.RUNNING
        self._take_snapshot()
        logger.info(f"StateManager: resumed")
        return True

    def set_phase(self, phase: LoopPhase) -> bool:
        if self._state != LoopState.RUNNING:
            logger.warning(f"Cannot set phase when state is {self._state}")
            return False
        old_phase = self._phase
        self._phase = phase
        self._take_snapshot()
        logger.debug(f"StateManager: phase changed {old_phase} → {phase}")
        return True

    def set_iteration(self, iteration: int) -> bool:
        if iteration < 0:
            logger.warning(f"Invalid iteration number: {iteration}")
            return False
        self._iteration = iteration
        return True

    def set_metadata(self, key: str, value: Any):
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self._metadata.get(key, default)

    # ═══════════════════════════════════════════════════════════
    # SNAPSHOTS Y HISTORIAL
    # ═══════════════════════════════════════════════════════════
    def _take_snapshot(self):
        snapshot = StateSnapshot(
            state=self._state,
            phase=self._phase,
            iteration=self._iteration,
            timestamp=datetime.now(timezone.utc),
            metadata=self._metadata.copy(),
        )
        self._snapshots.append(snapshot)
        if self._checkpoint is not None and self._session_id is not None:
            snap_dict = snapshot.to_dict()
            snap_dict["session_id"] = self._session_id
            if self._started_at:
                snap_dict["started_at"] = self._started_at.isoformat()
            if self._error:
                snap_dict["error"] = self._error
            self._checkpoint.save(self._session_id, snap_dict)

    def get_snapshots(self, limit: int = 10) -> list:
        return self._snapshots[-limit:]

    def get_last_snapshot(self) -> Optional[StateSnapshot]:
        return self._snapshots[-1] if self._snapshots else None

    # ═══════════════════════════════════════════════════════════
    # UTILIDADES
    # ═══════════════════════════════════════════════════════════
    def _can_transition(self, target: LoopState) -> bool:
        return target in self.VALID_TRANSITIONS.get(self._state, [])

    def get_duration_seconds(self) -> Optional[float]:
        if not self._started_at:
            return None
        end_time = self._completed_at or datetime.now(timezone.utc)
        return (end_time - self._started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "phase": self._phase.value if self._phase else None,
            "iteration": self._iteration,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "completed_at": self._completed_at.isoformat() if self._completed_at else None,
            "duration_seconds": self.get_duration_seconds(),
            "error": self._error,
            "is_running": self.is_running,
            "is_terminal": self.is_terminal,
            "snapshot_count": len(self._snapshots),
            "session_id": self._session_id,
            "checkpointing_enabled": self._checkpoint is not None,
        }

    def reset(self):
        self._state = LoopState.IDLE
        self._phase = None
        self._iteration = 0
        self._started_at = None
        self._completed_at = None
        self._error = None
        self._snapshots.clear()
        self._metadata.clear()
        logger.debug("StateManager reset")

    # ═══════════════════════════════════════════════════════════
    # ✅ v0.4.0: CHECKPOINTING — Recuperación entre reinicios
    # ═══════════════════════════════════════════════════════════
    def restore_from_checkpoint(self, session_id: Optional[str] = None) -> bool:
        sid = session_id or self._session_id
        if sid is None or self._checkpoint is None:
            logger.debug("[Checkpoint] restore_from_checkpoint: sin session_id o store")
            return False
        data = self._checkpoint.load_last(sid)
        if data is None:
            logger.info(f"[Checkpoint] Sin checkpoint previo para session={sid}")
            return False
        try:
            self._state = LoopState(data["state"])
            self._phase = LoopPhase(data["phase"]) if data.get("phase") else None
            self._iteration = data.get("iteration", 0)
            self._error = data.get("error")
            self._metadata = data.get("metadata", {})
            started_at_str = data.get("started_at")
            self._started_at = datetime.fromisoformat(started_at_str) if started_at_str else None
            self._completed_at = None
            logger.info(f"[Checkpoint] Restaurado: session={sid}, state={self._state.value}, iteration={self._iteration}")
            return True
        except Exception as e:
            logger.warning(f"[Checkpoint] restore_from_checkpoint error: {e}")
            return False

    def get_checkpoint_history(self, session_id: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        sid = session_id or self._session_id
        if sid is None or self._checkpoint is None:
            return []
        return self._checkpoint.load_history(sid, limit=limit)

    def list_checkpoint_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self._checkpoint is None:
            return []
        return self._checkpoint.list_sessions(limit=limit)

    def get_checkpoint_stats(self) -> Dict[str, Any]:
        if self._checkpoint is None:
            return {"available": False, "reason": "checkpointing disabled or no session_id"}
        return self._checkpoint.get_stats()

    def purge_checkpoints(self, session_id: Optional[str] = None) -> bool:
        if self._checkpoint is None:
            return False
        sid = session_id or self._session_id
        if sid:
            return self._checkpoint.delete_session(sid)
        self._checkpoint._purge_old()
        return True

    def close(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint.close()
            self._checkpoint = None
