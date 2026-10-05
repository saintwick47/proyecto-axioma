#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Memoria a Largo Plazo (SQLite) — QUERIES
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/long_term.py
# Autor: SaintWick
# Fecha: 2026-03-27
# Propósito: Operaciones de consulta y negocio (hereda infraestructura)
# Dependencia: Extiende LongTermMemoryBase desde long_term_base.py
# Optimizaciones: LRU cache, batch reads, thread-safe
# Compatibilidad: Interfaz pública idéntica al original
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import threading
from config.paths import Paths
from src.domain.exceptions import MemoryError
# ✅ CORREGIDO: Importar clase base desde módulo hermano
from src.memory.long_term_base import LongTermMemoryBase, _log_memory, _log_error, LOGGER_AVAILABLE, logger, SQLiteConnectionPool
from src.utils.degradation import log_degraded

# ============================================================================
# CLASE PÚBLICA: LongTermMemory (hereda de LongTermMemoryBase)
# ============================================================================
class LongTermMemory(LongTermMemoryBase):
    """Memoria a largo plazo usando SQLite — Operaciones de consulta y negocio.

    Interfaz pública idéntica al original para compatibilidad con tests existentes.
    """

    def get_recent(self, session_id: str, limit: int = 50, role: Optional[str] = None) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("get_recent", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            logger.warning(f"Invalid session_id in get_recent: {session_id[:20]}...")
            _log_memory("get_recent", "long_term", session_id=session_id, success=False, duration=0)
            return []
        limit = min(limit, 1000)
        cache_key = f"recent:{session_id}:{limit}:{role}"
        cached = self._cache_get(cache_key)
        if cached:
            duration = time.time() - start
            _log_memory("get_recent", "long_term", session_id=session_id, items_count=len(cached), duration=duration, success=True, db_path=str(self.db_path))
            return cached
        conn = self._get_connection()
        if not conn:
            return []
        with self._lock:
            try:
                if role:
                    cursor = conn.execute("SELECT id, session_id, role, content, timestamp, metadata, tokens FROM messages WHERE session_id = ? AND role = ? ORDER BY timestamp DESC LIMIT ?", (session_id, role, limit))
                else:
                    cursor = conn.execute("SELECT id, session_id, role, content, timestamp, metadata, tokens FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?", (session_id, limit))
                rows = cursor.fetchall()
                messages = []
                for row in rows:
                    msg = dict(row)
                    if msg.get("metadata"):
                        try:
                            msg["metadata"] = json.loads(msg["metadata"])
                        except json.JSONDecodeError as e:
                            log_degraded(logger, e, "src/memory/long_term.py:get_recent")
                            msg["metadata"] = {}
                    else:
                        msg["metadata"] = {}
                    messages.append(msg)
                result = list(reversed(messages))
                self._cache_set(cache_key, result)
                duration = time.time() - start
                _log_memory("get_recent", "long_term", session_id=session_id, items_count=len(result), duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory get_recent: session={session_id[:20]}, count={len(result)}, role={role}")
                return result
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_recent", extra={"session_id": session_id})
                _log_memory("get_recent", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="get_recent")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_recent", extra={"session_id": session_id})
                _log_memory("get_recent", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="get_recent")
            finally:
                self._release_connection(conn)

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        start = time.time()
        _log_memory("get_session_summary", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            logger.warning(f"Invalid session_id in get_session_summary: {session_id[:20]}...")
            _log_memory("get_session_summary", "long_term", session_id=session_id, success=False, duration=0)
            return {"session_id": session_id, "exists": False, "message_count": 0}
        cache_key = f"summary:{session_id}"
        cached = self._cache_get(cache_key)
        if cached:
            duration = time.time() - start
            _log_memory("get_session_summary", "long_term", session_id=session_id, duration=duration, success=True)
            return cached
        conn = self._get_connection()
        if not conn:
            return {"session_id": session_id, "exists": False, "message_count": 0}
        with self._lock:
            try:
                cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
                session = cursor.fetchone()
                if not session:
                    duration = time.time() - start
                    _log_memory("get_session_summary", "long_term", session_id=session_id, duration=duration, success=True)
                    return {"session_id": session_id, "exists": False, "message_count": 0}
                cursor = conn.execute("SELECT role, COUNT(*) as count FROM messages WHERE session_id = ? GROUP BY role", (session_id,))
                role_counts = {row["role"]: row["count"] for row in cursor.fetchall()}
                cursor = conn.execute("SELECT COUNT(*) as count FROM messages WHERE session_id = ?", (session_id,))
                message_count = cursor.fetchone()["count"]
                result = {"session_id": session_id, "exists": True, "created_at": session["created_at"], "updated_at": session["updated_at"], "message_count": message_count, "user_messages": role_counts.get("user", 0), "assistant_messages": role_counts.get("assistant", 0), "system_messages": role_counts.get("system", 0), "metadata": json.loads(session["metadata"]) if session.get("metadata") else {}}
                self._cache_set(cache_key, result)
                duration = time.time() - start
                _log_memory("get_session_summary", "long_term", session_id=session_id, duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory get_session_summary: session={session_id[:20]}")
                return result
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_session_summary", extra={"session_id": session_id})
                _log_memory("get_session_summary", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="get_session_summary")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_session_summary", extra={"session_id": session_id})
                _log_memory("get_session_summary", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="get_session_summary")
            finally:
                self._release_connection(conn)

    def get_session_stats(self, session_id: str) -> Dict[str, Any]:
        start = time.time()
        _log_memory("get_session_stats", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            logger.warning(f"Invalid session_id in get_session_stats: {session_id[:20]}...")
            _log_memory("get_session_stats", "long_term", session_id=session_id, success=False, duration=0)
            return {"total": 0, "user": 0, "assistant": 0, "system": 0, "created_at": None, "updated_at": None}
        conn = self._get_connection()
        if not conn:
            return {"total": 0, "user": 0, "assistant": 0, "system": 0, "created_at": None, "updated_at": None}
        with self._lock:
            try:
                cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
                session = cursor.fetchone()
                if not session:
                    duration = time.time() - start
                    _log_memory("get_session_stats", "long_term", session_id=session_id, duration=duration, success=True)
                    return {"total": 0, "user": 0, "assistant": 0, "system": 0, "created_at": None, "updated_at": None}
                cursor = conn.execute("SELECT role, COUNT(*) as count FROM messages WHERE session_id = ? GROUP BY role", (session_id,))
                counts = {row["role"]: row["count"] for row in cursor.fetchall()}
                result = {"total": counts.get("user", 0) + counts.get("assistant", 0) + counts.get("system", 0), "user": counts.get("user", 0), "assistant": counts.get("assistant", 0), "system": counts.get("system", 0), "created_at": session["created_at"], "updated_at": session["updated_at"]}
                duration = time.time() - start
                _log_memory("get_session_stats", "long_term", session_id=session_id, duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory get_session_stats: session={session_id[:20]}")
                return result
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_session_stats", extra={"session_id": session_id})
                _log_memory("get_session_stats", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="get_session_stats")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_session_stats", extra={"session_id": session_id})
                _log_memory("get_session_stats", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="get_session_stats")
            finally:
                self._release_connection(conn)

    def clear_session(self, session_id: str) -> bool:
        start = time.time()
        _log_memory("clear_session", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            logger.warning(f"Invalid session_id in clear_session: {session_id[:20]}...")
            _log_memory("clear_session", "long_term", session_id=session_id, success=False, duration=0)
            return False
        conn = self._get_connection()
        if not conn:
            return False
        with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                conn.execute("UPDATE sessions SET message_count = 0, updated_at = ? WHERE session_id = ?", (datetime.now(timezone.utc).isoformat(), session_id))
                conn.commit()
                deleted = cursor.rowcount > 0
                self._cache_clear()
                duration = time.time() - start
                _log_memory("clear_session", "long_term", session_id=session_id, duration=duration, success=True, db_path=str(self.db_path))
                logger.info(f"LongTermMemory cleared: session={session_id[:20]}, deleted={deleted}")
                return deleted
            except sqlite3.Error as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemory.clear_session", extra={"session_id": session_id})
                _log_memory("clear_session", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="clear_session")
            except Exception as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemory.clear_session", extra={"session_id": session_id})
                _log_memory("clear_session", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="clear_session")
            finally:
                self._release_connection(conn)

    def search(self, query: str, session_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("search", "long_term", session_id=session_id or "global")
        if session_id and not self._validate_session_id(session_id):
            logger.warning(f"Invalid session_id in search: {session_id[:20]}...")
            session_id = None
        limit = min(limit, 100)
        query = query.replace("%", "\\%").replace("_", "\\_")
        search_pattern = f"%{query}%"
        conn = self._get_connection()
        if not conn:
            return []
        with self._lock:
            try:
                if session_id:
                    cursor = conn.execute("SELECT id, session_id, role, content, timestamp, metadata, tokens FROM messages WHERE session_id = ? AND content LIKE ? ESCAPE '\\' ORDER BY timestamp DESC LIMIT ?", (session_id, search_pattern, limit))
                else:
                    cursor = conn.execute("SELECT id, session_id, role, content, timestamp, metadata, tokens FROM messages WHERE content LIKE ? ESCAPE '\\' ORDER BY timestamp DESC LIMIT ?", (search_pattern, limit))
                rows = cursor.fetchall()
                messages = []
                for row in rows:
                    msg = dict(row)
                    if msg.get("metadata"):
                        try:
                            msg["metadata"] = json.loads(msg["metadata"])
                        except json.JSONDecodeError as e:
                            log_degraded(logger, e, "src/memory/long_term.py:search")
                            msg["metadata"] = {}
                    else:
                        msg["metadata"] = {}
                    messages.append(msg)
                duration = time.time() - start
                _log_memory("search", "long_term", session_id=session_id or "global", items_count=len(messages), duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory search: query='{query[:30]}', results={len(messages)}")
                return messages
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.search", extra={"query": query[:30]})
                _log_memory("search", "long_term", session_id=session_id or "global", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="search")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.search", extra={"query": query[:30]})
                _log_memory("search", "long_term", session_id=session_id or "global", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="search")
            finally:
                self._release_connection(conn)

    def add_knowledge(self, key: str, value: str, category: Optional[str] = None, session_id: Optional[str] = None, confidence: float = 1.0) -> int:
        start = time.time()
        _log_memory("add_knowledge", "knowledge", session_id=session_id or "global")
        if not key or not isinstance(key, str):
            _log_memory("add_knowledge", "knowledge", success=False, duration=0)
            raise MemoryError("key es requerido y debe ser string", operation="add_knowledge")
        if not value or not isinstance(value, str):
            _log_memory("add_knowledge", "knowledge", success=False, duration=0)
            raise MemoryError("value es requerido y debe ser string", operation="add_knowledge")
        if session_id and not self._validate_session_id(session_id):
            _log_memory("add_knowledge", "knowledge", success=False, duration=0)
            raise MemoryError(f"session_id inválido: {session_id[:20]}...", operation="add_knowledge")
        confidence = max(0.0, min(1.0, confidence))
        conn = self._get_connection()
        if not conn:
            raise MemoryError("No se pudo obtener conexión", operation="add_knowledge")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("INSERT INTO knowledge (session_id, category, key, value, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (session_id, category, key, value, confidence, timestamp, timestamp))
                knowledge_id = cursor.lastrowid
                conn.commit()
                self._cache_clear()
                duration = time.time() - start
                _log_memory("add_knowledge", "knowledge", items_count=1, duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory knowledge added: key={key[:30]}, category={category}")
                return knowledge_id
            except sqlite3.Error as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemory.add_knowledge", extra={"key": key[:30]})
                _log_memory("add_knowledge", "knowledge", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="add_knowledge")
            except Exception as e:
                conn.rollback()
                duration = time.time() - start
                _log_error(e, "LongTermMemory.add_knowledge", extra={"key": key[:30]})
                _log_memory("add_knowledge", "knowledge", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="add_knowledge")
            finally:
                self._release_connection(conn)

    def get_knowledge(self, category: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        start = time.time()
        _log_memory("get_knowledge", "knowledge", session_id=category or "global")
        limit = min(limit, 1000)
        conn = self._get_connection()
        if not conn:
            return []
        with self._lock:
            try:
                if category:
                    cursor = conn.execute("SELECT * FROM knowledge WHERE category = ? ORDER BY confidence DESC, updated_at DESC LIMIT ?", (category, limit))
                else:
                    cursor = conn.execute("SELECT * FROM knowledge ORDER BY confidence DESC, updated_at DESC LIMIT ?", (limit,))
                rows = cursor.fetchall()
                result = [dict(row) for row in rows]
                duration = time.time() - start
                _log_memory("get_knowledge", "knowledge", items_count=len(result), duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory get_knowledge: category={category}, count={len(result)}")
                return result
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_knowledge", extra={"category": category})
                _log_memory("get_knowledge", "knowledge", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="get_knowledge")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_knowledge", extra={"category": category})
                _log_memory("get_knowledge", "knowledge", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="get_knowledge")
            finally:
                self._release_connection(conn)

    def export_session(self, session_id: str) -> Dict[str, Any]:
        start = time.time()
        _log_memory("export_session", "long_term", session_id=session_id)
        if not self._validate_session_id(session_id):
            _log_memory("export_session", "long_term", session_id=session_id, success=False, duration=0)
            raise MemoryError(f"session_id inválido: {session_id[:20]}...", operation="export_session")
        try:
            summary = self.get_session_summary(session_id)
            messages = self.get_recent(session_id, limit=10000)
            result = {"session": summary, "messages": messages, "exported_at": datetime.now(timezone.utc).isoformat()}
            duration = time.time() - start
            _log_memory("export_session", "long_term", session_id=session_id, duration=duration, success=True, db_path=str(self.db_path))
            logger.debug(f"LongTermMemory export_session: session={session_id[:20]}")
            return result
        except MemoryError:
            raise
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "LongTermMemory.export_session", extra={"session_id": session_id})
            _log_memory("export_session", "long_term", session_id=session_id, duration=duration, success=False, db_path=str(self.db_path))
            logger.error(f"Unexpected error exporting session: {e}")
            raise MemoryError(f"Error exportando: {e}", operation="export_session")

    def get_stats(self) -> Dict[str, Any]:
        start = time.time()
        _log_memory("get_stats", "long_term")
        conn = self._get_connection()
        if not conn:
            return {"total_sessions": 0, "total_messages": 0, "total_knowledge": 0, "total_tokens": 0, "db_path": str(self.db_path), "db_size_bytes": 0}
        with self._lock:
            try:
                cursor = conn.execute("SELECT COUNT(*) as count FROM sessions")
                total_sessions = cursor.fetchone()["count"]
                cursor = conn.execute("SELECT COUNT(*) as count FROM messages")
                total_messages = cursor.fetchone()["count"]
                cursor = conn.execute("SELECT COUNT(*) as count FROM knowledge")
                total_knowledge = cursor.fetchone()["count"]
                cursor = conn.execute("SELECT SUM(tokens) as total FROM messages")
                total_tokens = cursor.fetchone()["total"] or 0
                result = {"total_sessions": total_sessions, "total_messages": total_messages, "total_knowledge": total_knowledge, "total_tokens": total_tokens, "db_path": str(self.db_path), "db_size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0}
                duration = time.time() - start
                _log_memory("get_stats", "long_term", items_count=total_messages, duration=duration, success=True, db_path=str(self.db_path))
                logger.debug(f"LongTermMemory get_stats: sessions={total_sessions}, messages={total_messages}")
                return result
            except sqlite3.Error as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_stats", extra={})
                _log_memory("get_stats", "long_term", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error DB: {e}", operation="get_stats")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "LongTermMemory.get_stats", extra={})
                _log_memory("get_stats", "long_term", duration=duration, success=False, db_path=str(self.db_path))
                raise MemoryError(f"Error inesperado: {e}", operation="get_stats")
            finally:
                self._release_connection(conn)

    def close(self) -> None:
        start = time.time()
        _log_memory("close", "long_term", db_path=str(self.db_path))
        with self._lock:
            if self._pool:
                try:
                    self._pool.close_all()
                    self._pool = None
                    duration = time.time() - start
                    _log_memory("close", "long_term", duration=duration, success=True, db_path=str(self.db_path))
                    logger.debug("LongTermMemory connection pool closed")
                except Exception as e:
                    duration = time.time() - start
                    _log_error(e, "LongTermMemory.close", extra={})
                    _log_memory("close", "long_term", duration=duration, success=False, db_path=str(self.db_path))
                    logger.error(f"Error closing connection pool: {e}")
                finally:
                    self._initialized = False

    def __len__(self) -> int:
        try:
            conn = self._get_connection()
            if not conn:
                return 0
            cursor = conn.execute("SELECT COUNT(*) as count FROM messages")
            count = cursor.fetchone()["count"]
            self._release_connection(conn)
            return count
        except Exception as e:
            logger.error(f"Error getting length: {e}")
            return 0

    def __repr__(self) -> str:
        try:
            stats = self.get_stats()
            return f"LongTermMemory(db={self.db_path.name}, sessions={stats['total_sessions']}, messages={stats['total_messages']})"
        except Exception:
            return f"LongTermMemory(db={self.db_path.name})"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception as _d2e:
            log_degraded(logging.getLogger(__name__), _d2e, "src/memory/long_term.py:__del__")

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — long_term.py v0.0.9 QUERIES
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                          | Razón                    |
# |-------|--------------------------------|--------------------------|
# | ~20   | ✅ Import desde long_term_base | Heredar infraestructura  |
# | ~27   | ✅ Clase LongTermMemory        | Extensión con queries    |
# | ~33   | ✅ get_recent()                | Consulta con cache LRU   |
# | ~95   | ✅ get_session_summary()       | Resumen con aggregations |
# | ~155  | ✅ clear_session()             | Delete con transacción   |
# | ~190  | ✅ search()                    | LIKE con ESCAPE          |
# | ~240  | ✅ add_knowledge()/get_knowledge() | Vault de conocimiento |
# | ~310  | ✅ export_session()/get_stats() | Export y métricas globales|
# | ~360  | ✅ close()/__magic__/__del__   | Lifecycle completo       |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
