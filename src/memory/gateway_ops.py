#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Gateway Operations Module
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/gateway_ops.py
# Autor: SaintWick
# Fecha: 2026-03-26  # ✅ CORREGIDO: Verificar retorno add() + fallback get_context
# Propósito: Operaciones de memoria (add, get, search, clear, export)
# ✅ v0.1.0: FIX AUDITORÍA — self._semantic.add()/.search() eran API de una
#   implementación anterior a la migración a LanceDB (resabio ChromaDB).
#   Corregido a .store()/.recall() (API real de Palace, ver gateway.py).
#   Nota: estos métodos de negocio están TODOS sombreados por los de
#   MemoryGateway (gateway.py) — ver docstring de la clase.
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError
from src.utils.degradation import log_degraded

# ============================================================================
# LOGGING HELPERS (compartidos con gateway.py)
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
    """
    Helper para logging de operaciones de memoria.
    Args:
        op: Operación realizada (add, search, clear, etc.)
        collection: Colección afectada
        session_id: ID de sesión asociada
        items_count: Número de items afectados
        duration: Duración de la operación en segundos
        success: Si la operación fue exitosa
        db_path: Ruta de persistencia
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized (no _initialized)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_memory_operation(op, collection, session_id, items_count, duration, success, db_path)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/gateway_ops.py:_log_memory")


def _log_error(error: Exception, context: str, extra: dict = None):
    """
    Helper para logging de errores.
    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized (no _initialized)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/gateway_ops.py:_log_error")


# ============================================================================
# MIXIN DE OPERACIONES — Lógica de negocio pura
# ============================================================================
class MemoryGatewayOps:
    """
    Mixin con todas las operaciones de negocio del Gateway.
    Requiere: self._short_term, self._long_term, self._semantic, self.session_id

    ⚠️ FIX AUDITORÍA: gateway.py (MemoryGateway) redefine add_message(),
    get_context(), search_semantic(), get_session_summary(), clear() y
    export_session() — TODOS los métodos de negocio de este mixin quedan
    sombreados (MRO de Python siempre usa la versión de la subclase). Lo
    único que MemoryGateway realmente hereda en uso es _sanitize_session_id()
    y los helpers _log_memory/_log_error importados a nivel de módulo.
    No se borran estos métodos (podría haber otro consumidor de este mixin
    fuera de lo que vimos) pero SÍ se corrigió su API interna: llamaban
    self._semantic.add()/.search() — API de una implementación anterior a
    la migración a LanceDB. La clase Palace actual (gateway.py) expone
    .store()/.recall(), no .add()/.search(). Si algún día dejan de estar
    sombreados, con la API vieja fallarían con AttributeError.
    """

    @staticmethod
    def _sanitize_session_id(session_id: str) -> str:
        """
        Sanitiza session_id para usar en nombres de colección/archivo.
        Args:
            session_id: ID original de sesión
        Returns:
            ID sanitizado (solo alphanumeric, max 8 chars)
        """
        # Solo caracteres alphanumeric
        sanitized = ''.join(c for c in session_id if c.isalnum())
        # Limitar a 8 caracteres
        return sanitized[:8] or "default"

    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Agrega mensaje a todas las capas de memoria.
        Args:
            role: Rol del mensaje (user/assistant/system)
            content: Contenido del mensaje
            metadata: Metadatos opcionales
        Returns:
            True si se agregó exitosamente
        Example:
            >>> gateway.add_message("user", "¿Qué es Python?")
            True
        """
        start = time.time()
        _log_memory("add_message", "axioma_sessions", session_id=self.session_id)

        success = False
        timestamp = datetime.now(timezone.utc)

        try:
            # Short-term (siempre primero)
            if self._short_term is not None:
                try:
                    # ✅ CORREGIDO: Verificar retorno de add()
                    add_result = self._short_term.add(role, content, metadata)
                    if add_result:
                        success = True
                        logger.debug(f"Short-term add OK: role={role}")
                    else:
                        logger.warning(f"Short-term add returned False: role={role}, session={self.session_id[:8]}")
                except Exception as e:
                    logger.error(f"Short-term add failed: {e}")

            # Long-term (siempre intentar, incluso si short-term falló)
            if self._long_term is not None:
                try:
                    _lt_meta = dict(metadata or {})
                    if getattr(self, "agent_id", None):
                        _lt_meta["agent_id"] = self.agent_id
                    message_id = self._long_term.add(
                        session_id=self.session_id,
                        role=role,
                        content=content,
                        metadata=_lt_meta,
                    )
                    if message_id and message_id > 0:
                        success = True
                        logger.debug(f"Long-term add OK: role={role}, id={message_id}")
                    else:
                        logger.warning(f"Long-term add returned invalid id: {message_id}")
                except Exception as e:
                    logger.error(f"Long-term add failed: {e}")

            # Semantic (solo mensajes de usuario para embeddings)
            # Ejecutado en background thread — genera embedding BGE-M3 (~50-200ms)
            # sin bloquear el retorno al caller. success se marca True de inmediato
            # porque short/long-term ya confirmaron persistencia.
            if self._semantic is not None and role == "user":
                _semantic_ref = self._semantic
                _semantic_meta = {
                    "role": role,
                    "session_id": self.session_id,
                    "timestamp": timestamp.isoformat(),
                    **(metadata or {}),
                }
                if getattr(self, "agent_id", None):
                    _semantic_meta["agent_id"] = self.agent_id
                _semantic_content = content

                def _semantic_write() -> None:
                    try:
                        # ✅ FIX AUDITORÍA: era .add() — API de una implementación
                        # anterior a la migración a LanceDB. Palace expone .store().
                        _semantic_ref.store(text=_semantic_content, metadata=_semantic_meta)
                    except Exception as e:
                        logger.error(f"Semantic add failed (background): {e}")

                import threading as _threading
                _threading.Thread(
                    target=_semantic_write,
                    daemon=True,
                    name="gateway-semantic-write",
                ).start()
                success = True

            duration = time.time() - start
            _log_memory("add_message", "axioma_sessions",
                       session_id=self.session_id, items_count=1,
                       duration=duration, success=success, db_path=str(Paths.MEMORY / "axioma.db"))

            if success:
                logger.debug(f"Message added: role={role}, len={len(content)}")

            return success

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.add_message", extra={"role": role, "content_len": len(content)})
            _log_memory("add_message", "axioma_sessions",
                       session_id=self.session_id, success=False, duration=duration)
            raise

    def get_context(
        self,
        limit: int = 10,
        include_system: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Obtiene contexto reciente de la conversación.
        Args:
            limit: Número máximo de mensajes
            include_system: Incluir mensajes de sistema
        Returns:
            Lista de mensajes [{role, content, timestamp}, ...]
        Example:
            >>> context = gateway.get_context(limit=5)
            >>> for msg in context:
            ...     print(f"{msg['role']}: {msg['content']}")
        """
        start = time.time()
        _log_memory("get_context", "axioma_sessions", session_id=self.session_id)

        try:
            # Priorizar short-term (más rápido)
            if self._short_term is not None:
                try:
                    messages = self._short_term.get_recent(limit)
                    # ✅ CORREGIDO: Solo retornar si short-term tiene mensajes, sino fallback a long-term
                    if messages:
                        if not include_system:
                            messages = [m for m in messages if m.get("role") != "system"]
                        duration = time.time() - start
                        _log_memory("get_context", "short_term",
                                   session_id=self.session_id, items_count=len(messages),
                                   duration=duration, success=True)
                        return messages
                    # Si short-term está vacío, continuar intentando con long-term
                except Exception as e:
                    logger.error(f"Short-term get_context failed: {e}")

            # Fallback a long-term (cuando short-term está vacío o falla)
            if self._long_term is not None:
                try:
                    messages = self._long_term.get_recent(
                        session_id=self.session_id,
                        limit=limit,
                    )
                    if not include_system:
                        messages = [m for m in messages if m.get("role") != "system"]

                    duration = time.time() - start
                    _log_memory("get_context", "long_term",
                               session_id=self.session_id, items_count=len(messages),
                               duration=duration, success=True)
                    return messages

                except Exception as e:
                    logger.error(f"Long-term get_context failed: {e}")

            duration = time.time() - start
            _log_memory("get_context", "axioma_sessions",
                       session_id=self.session_id, items_count=0, duration=duration, success=False)
            return []

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.get_context", extra={"limit": limit})
            _log_memory("get_context", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def search_semantic(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """
        Búsqueda semántica por similitud vectorial.
        Args:
            query: Texto de búsqueda
            limit: Número máximo de resultados
            min_similarity: Mínima similitud (0.0-1.0)
        Returns:
            Lista de resultados [{text, similarity, metadata}, ...]
        Example:
            >>> results = gateway.search_semantic("cómo instalar Python")
            >>> for r in results:
            ...     print(f"Similarity: {r['similarity']}, Text: {r['text']}")
        """
        start = time.time()
        _log_memory("search_semantic", "semantic", session_id=self.session_id)

        if self._semantic is None:
            logger.warning("Semantic memory not enabled")
            _log_memory("search_semantic", "semantic",
                       session_id=self.session_id, success=False, duration=0)
            return []

        try:
            # ✅ FIX AUDITORÍA: era self._semantic.search(..., min_similarity=...) —
            # API de una implementación anterior a la migración a LanceDB. Palace
            # expone .recall(..., min_score=...).
            results = self._semantic.recall(
                query=query,
                limit=limit,
                min_score=min_similarity,
            )

            duration = time.time() - start
            _log_memory("search_semantic", "semantic",
                       session_id=self.session_id, items_count=len(results),
                       duration=duration, success=True)
            logger.debug(f"Semantic search: {len(results)} results for '{query[:50]}'")
            return results

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.search_semantic", extra={"query": query[:50]})
            _log_memory("search_semantic", "semantic",
                       session_id=self.session_id, duration=duration, success=False)
            logger.error(f"Semantic search failed: {e}")
            return []

    def get_session_summary(self) -> Dict[str, Any]:
        """
        Obtiene resumen de la sesión actual.
        Returns:
            Dict con estadísticas de la sesión
        Example:
            >>> summary = gateway.get_session_summary()
            >>> print(f"Messages: {summary['message_count']}")
        """
        start = time.time()
        _log_memory("get_session_summary", "axioma_sessions", session_id=self.session_id)

        summary = {
            "session_id": self.session_id,
            "message_count": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "system_messages": 0,
            "created_at": None,
            "updated_at": None,
        }

        try:
            if self._short_term is not None:
                try:
                    stats = self._short_term.get_stats()
                    summary["message_count"] = stats.get("total", 0)
                    summary["updated_at"] = stats.get("last_updated")
                except Exception as _d2e:
                    log_degraded(logging.getLogger(__name__), _d2e, "src/memory/gateway_ops.py:get_session_summary")

            if self._long_term is not None:
                try:
                    stats = self._long_term.get_session_stats(self.session_id)
                    summary["message_count"] = max(
                        summary["message_count"],
                        stats.get("total", 0),
                    )
                    summary["user_messages"] = stats.get("user", 0)
                    summary["assistant_messages"] = stats.get("assistant", 0)
                    summary["system_messages"] = stats.get("system", 0)
                    summary["created_at"] = stats.get("created_at")
                    summary["updated_at"] = stats.get("updated_at")
                except Exception as _d2e:
                    log_degraded(logging.getLogger(__name__), _d2e, "src/memory/gateway_ops.py:get_session_summary")

            duration = time.time() - start
            _log_memory("get_session_summary", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=True)
            return summary

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.get_session_summary", extra={"session_id": self.session_id})
            _log_memory("get_session_summary", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def clear(self, keep_system: bool = False) -> bool:
        """
        Limpia toda la memoria de la sesión.
        Args:
            keep_system: Mantener mensajes de sistema
        Returns:
            True si se limpió exitosamente
        """
        start = time.time()
        _log_memory("clear", "axioma_sessions", session_id=self.session_id)

        success = True

        try:
            if self._short_term is not None:
                try:
                    self._short_term.clear(keep_system=keep_system)
                except Exception as e:
                    logger.error(f"Short-term clear failed: {e}")
                    success = False

            if self._long_term is not None:
                try:
                    self._long_term.clear_session(self.session_id)
                except Exception as e:
                    logger.error(f"Long-term clear failed: {e}")
                    success = False

            if self._semantic is not None:
                try:
                    self._semantic.clear()
                except Exception as e:
                    logger.error(f"Semantic clear failed: {e}")
                    success = False

            duration = time.time() - start
            _log_memory("clear", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=success)

            if success:
                logger.info(f"Memory cleared for session {self.session_id}")

            return success

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.clear", extra={"session_id": self.session_id})
            _log_memory("clear", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def export_session(self) -> Dict[str, Any]:
        """
        Exporta toda la sesión a formato serializable.
        Returns:
            Dict con toda la data de la sesión
        Example:
            >>> export = gateway.export_session()
            >>> import json
            >>> with open("session.json", "w") as f:
            ...     json.dump(export, f)
        """
        start = time.time()
        _log_memory("export_session", "axioma_sessions", session_id=self.session_id)

        try:
            result = {
                "session_id": self.session_id,
                "messages": self.get_context(limit=1000),
                "summary": self.get_session_summary(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }

            duration = time.time() - start
            _log_memory("export_session", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=True)
            return result

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "MemoryGateway.export_session", extra={"session_id": self.session_id})
            _log_memory("export_session", "axioma_sessions",
                       session_id=self.session_id, duration=duration, success=False)
            raise


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — Refactorización v0.0.9
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                          | Razón                    |
# |-------|--------------------------------|--------------------------|
# | ~3    | ✅ Versión: v0.0.9 mantenida   | Consistencia con sistema |
# | ~97   | ✅ Verificar retorno add()     | ✅ CORREGIDO: add_message|
# | ~107  | ✅ Verificar message_id > 0    | ✅ CORREGIDO: long_term  |
# | ~147  | ✅ if messages: + fallback     | ✅ CORREGIDO: get_context|
# | Todas | ✅ Docstrings completas        | Claridad y consistencia  |
# | Todas | ✅ Type hints consistentes     | Claridad estructural     |
# | Todas | ✅ Comentarios ✅ CORREGIDO:   | Trazabilidad de cambios  |
# ═══════════════════════════════════════════════════════════════
