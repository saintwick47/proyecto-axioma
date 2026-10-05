#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5 — Memoria a Corto Plazo
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/short_term.py
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Ventana conversacional con deque + TTL
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, List, Dict, Any, Deque
from collections import deque
from datetime import datetime, timezone, timedelta
import threading
from src.utils.degradation import log_degraded

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
    """
    Helper para logging de operaciones de memoria.

    Args:
        op: Operación realizada (add, get, clear, etc.)
        collection: Colección/memoria afectada
        session_id: ID de sesión asociada
        items_count: Número de items afectados
        duration: Duración de la operación en segundos
        success: Si la operación fue exitosa
        db_path: Ruta de base de datos (si aplica)
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_memory_operation(op, collection, session_id, items_count, duration, success, db_path)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/short_term.py:_log_memory")

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
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/short_term.py:_log_error")

# ============================================================================
# CLASE PRINCIPAL
# ============================================================================
class ShortTermMemory:
    """
    Memoria a corto plazo para conversaciones recientes.

    Implementa:
    - Deque con límite máximo de mensajes
    - TTL (Time To Live) para expiración automática
    - Thread-safe para acceso concurrente
    - Estadísticas de uso
    - Logging detallado de todas las operaciones

    Features:
    - Ventana deslizante de mensajes (max_messages)
    - Expiración automática por tiempo (ttl_seconds)
    - Conteo por rol (user/assistant/system)
    - Métricas de uso (added, expired, cleared)
    - Thread-safe con RLock

    Example:
    >>> memory = ShortTermMemory(max_messages=50, ttl_seconds=3600)
    >>> memory.add("user", "Hola")
    >>> memory.add("assistant", "¡Hola! ¿En qué ayudo?")
    >>> recent = memory.get_recent(limit=10)
    >>> stats = memory.get_stats()
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        max_messages: int = 50,
        ttl_seconds: int = 3600,
    ):
        """
        Inicializa memoria a corto plazo.

        Args:
            session_id: ID de sesión para tracking
            max_messages: Máximo de mensajes en ventana
            ttl_seconds: Tiempo de vida en segundos

        Example:
        >>> memory = ShortTermMemory(
        ...     session_id="abc123",
        ...     max_messages=100,
        ...     ttl_seconds=7200
        ... )
        """
        start = time.time()
        self.session_id = session_id
        self.max_messages = max_messages
        self.ttl_seconds = ttl_seconds

        _log_memory("short_term_init", "short_term", session_id=session_id)

        # Deque para mensajes (thread-safe con lock)
        self._messages: Deque[Dict[str, Any]] = deque(maxlen=max_messages)
        self._lock = threading.RLock()

        # Timestamps para TTL
        self._created_at = datetime.now(timezone.utc)
        self._last_accessed = self._created_at
        self._last_updated = self._created_at

        # Estadísticas
        self._stats = {
            "total_added": 0,
            "total_expired": 0,
            "total_cleared": 0,
        }

        duration = time.time() - start
        _log_memory("short_term_init", "short_term",
                   session_id=session_id, duration=duration, success=True)
        logger.debug(f"ShortTermMemory initialized: max={max_messages}, ttl={ttl_seconds}s")

    def _is_expired(self) -> bool:
        """
        Verifica si la memoria expiró por TTL.

        Returns:
            bool: True si el TTL ha expirado

        Example:
        >>> memory._is_expired()
        False
        """
        elapsed = (datetime.now(timezone.utc) - self._created_at).total_seconds()
        return elapsed > self.ttl_seconds

    def _cleanup_expired(self) -> int:
        """
        Limpia mensajes expirados.

        Returns:
            int: Número de mensajes eliminados

        Example:
        >>> count = memory._cleanup_expired()
        >>> print(f"Expired messages: {count}")
        """
        start = time.time()
        if self._is_expired():
            with self._lock:
                count = len(self._messages)
                self._messages.clear()
                self._stats["total_expired"] += count
                # v0.6.9g: renovar ventana — el TTL es por CREACION; sin esto
                # quedaba expirada para siempre (add short_term ✗ en sesiones >1 h)
                self._created_at = datetime.now(timezone.utc)
                duration = time.time() - start
                _log_memory("cleanup_expired", "short_term",
                           session_id=self.session_id, items_count=count, duration=duration)
                logger.info(f"ShortTermMemory expired: {count} messages cleared (ventana renovada)")
                return count
        return 0

    def add(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Agrega mensaje a la memoria.

        Args:
            role: Rol (user/assistant/system)
            content: Contenido del mensaje
            metadata: Metadatos opcionales

        Returns:
            bool: True si se agregó exitosamente

        Raises:
            Exception: Si hay error crítico (logged)

        Example:
        >>> memory.add("user", "¿Qué es Python?")
        True
        >>> memory.add("assistant", "Python es un lenguaje...", metadata={"model": "llama3"})
        True
        """
        start = time.time()
        _log_memory("add", "short_term", session_id=self.session_id)

        # Check TTL primero
        self._cleanup_expired()

        if self._is_expired():
            logger.warning("Cannot add: memory expired")
            _log_memory("add", "short_term", session_id=self.session_id, success=False, duration=0)
            return False

        try:
            with self._lock:
                message = {
                    "role": role,
                    "content": content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": metadata or {},
                }
                self._messages.append(message)
                self._last_updated = datetime.now(timezone.utc)
                self._stats["total_added"] += 1

            duration = time.time() - start
            _log_memory("add", "short_term",
                       session_id=self.session_id, items_count=1, duration=duration, success=True)
            logger.debug(f"ShortTermMemory add: role={role}, len={len(content)}")
            return True
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.add", extra={"role": role, "content_len": len(content)})
            _log_memory("add", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Obtiene últimos N mensajes.

        Args:
            limit: Número máximo de mensajes

        Returns:
            List[Dict]: Lista de mensajes recientes

        Example:
        >>> recent = memory.get_recent(limit=5)
        >>> for msg in recent:
        ...     print(f"{msg['role']}: {msg['content'][:50]}")
        """
        start = time.time()
        _log_memory("get_recent", "short_term", session_id=self.session_id)

        self._cleanup_expired()

        try:
            with self._lock:
                self._last_accessed = datetime.now(timezone.utc)
                # Convert deque to list and get last N
                messages = list(self._messages)
                result = messages[-limit:] if len(messages) > limit else messages

            duration = time.time() - start
            _log_memory("get_recent", "short_term",
                       session_id=self.session_id, items_count=len(result), duration=duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.get_recent", extra={"limit": limit})
            _log_memory("get_recent", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def get_all(self) -> List[Dict[str, Any]]:
        """
        Obtiene todos los mensajes no expirados.

        Returns:
            List[Dict]: Lista completa de mensajes

        Example:
        >>> all_msgs = memory.get_all()
        >>> print(f"Total messages: {len(all_msgs)}")
        """
        start = time.time()
        _log_memory("get_all", "short_term", session_id=self.session_id)

        self._cleanup_expired()

        try:
            with self._lock:
                self._last_accessed = datetime.now(timezone.utc)
                result = list(self._messages)

            duration = time.time() - start
            _log_memory("get_all", "short_term",
                       session_id=self.session_id, items_count=len(result), duration=duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.get_all", extra={})
            _log_memory("get_all", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def get_by_role(self, role: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Obtiene mensajes filtrados por rol.

        Args:
            role: Rol a filtrar (user/assistant/system)
            limit: Máximo de resultados

        Returns:
            List[Dict]: Lista de mensajes del rol especificado

        Example:
        >>> user_msgs = memory.get_by_role("user", limit=5)
        >>> assistant_msgs = memory.get_by_role("assistant", limit=5)
        """
        start = time.time()
        _log_memory("get_by_role", "short_term", session_id=self.session_id)

        self._cleanup_expired()

        try:
            with self._lock:
                self._last_accessed = datetime.now(timezone.utc)
                filtered = [m for m in self._messages if m["role"] == role]
                result = filtered[-limit:]

            duration = time.time() - start
            _log_memory("get_by_role", "short_term",
                       session_id=self.session_id, items_count=len(result), duration=duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.get_by_role", extra={"role": role})
            _log_memory("get_by_role", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def get_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas de la memoria.

        Returns:
            Dict: Estadísticas completas

        Example:
        >>> stats = memory.get_stats()
        >>> print(f"Total: {stats['total']}, Added: {stats['total_added']}")
        >>> print(f"By role: {stats['by_role']}")
        """
        start = time.time()
        _log_memory("get_stats", "short_term", session_id=self.session_id)

        try:
            with self._lock:
                # Count by role
                role_counts = {"user": 0, "assistant": 0, "system": 0}
                for msg in self._messages:
                    role = msg.get("role", "unknown")
                    if role in role_counts:
                        role_counts[role] += 1

                result = {
                    "session_id": self.session_id,
                    "total": len(self._messages),
                    "max_messages": self.max_messages,
                    "ttl_seconds": self.ttl_seconds,
                    "created_at": self._created_at.isoformat(),
                    "last_updated": self._last_updated.isoformat(),
                    "last_accessed": self._last_accessed.isoformat(),
                    "is_expired": self._is_expired(),
                    "time_remaining": max(
                        0,
                        self.ttl_seconds - (datetime.now(timezone.utc) - self._created_at).total_seconds(),
                    ),
                    **self._stats,
                    "by_role": role_counts,
                }

            duration = time.time() - start
            _log_memory("get_stats", "short_term",
                       session_id=self.session_id, duration=duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.get_stats", extra={})
            _log_memory("get_stats", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def clear(self, keep_system: bool = False) -> bool:
        """
        Limpia toda la memoria.

        Args:
            keep_system: Mantener mensajes de sistema

        Returns:
            bool: True si se limpió exitosamente

        Example:
        >>> memory.clear()  # Limpia todo
        True
        >>> memory.clear(keep_system=True)  # Mantiene system messages
        True
        """
        start = time.time()
        _log_memory("clear", "short_term", session_id=self.session_id)

        try:
            with self._lock:
                if keep_system:
                    # Keep only system messages
                    system_msgs = [m for m in self._messages if m["role"] == "system"]
                    self._messages.clear()
                    for msg in system_msgs:
                        self._messages.append(msg)
                else:
                    self._messages.clear()
                self._stats["total_cleared"] += 1
                self._last_updated = datetime.now(timezone.utc)

            duration = time.time() - start
            _log_memory("clear", "short_term",
                       session_id=self.session_id, duration=duration, success=True)
            logger.info(f"ShortTermMemory cleared: keep_system={keep_system}")
            return True
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.clear", extra={"keep_system": keep_system})
            _log_memory("clear", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def remove_old(self, max_age_seconds: int) -> int:
        """
        Remueve mensajes más antiguos que un tiempo dado.

        Args:
            max_age_seconds: Edad máxima en segundos

        Returns:
            int: Número de mensajes removidos

        Example:
        >>> removed = memory.remove_old(300)  # Remueve mensajes > 5 minutos
        >>> print(f"Removed {removed} old messages")
        """
        start = time.time()
        _log_memory("remove_old", "short_term", session_id=self.session_id)

        removed = 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)

        try:
            with self._lock:
                new_messages = deque(maxlen=self.max_messages)
                for msg in self._messages:
                    try:
                        msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                        if msg_time > cutoff:
                            new_messages.append(msg)
                        else:
                            removed += 1
                    except Exception as e:
                        log_degraded(logger, e, "src/memory/short_term.py:remove_old")
                        new_messages.append(msg)
                self._messages = new_messages
                self._last_updated = datetime.now(timezone.utc)

            if removed > 0:
                _log_memory("remove_old", "short_term",
                           session_id=self.session_id, items_count=removed)
                logger.info(f"ShortTermMemory: removed {removed} old messages")

            duration = time.time() - start
            _log_memory("remove_old", "short_term",
                       session_id=self.session_id, duration=duration, success=True)
            return removed
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.remove_old", extra={"max_age_seconds": max_age_seconds})
            _log_memory("remove_old", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def refresh_ttl(self) -> bool:
        """
        Refresca el TTL (reinicia contador de expiración).

        Returns:
            bool: True si se refrescó exitosamente

        Example:
        >>> memory.refresh_ttl()  # Reinicia el timer de expiración
        True
        """
        start = time.time()
        _log_memory("refresh_ttl", "short_term", session_id=self.session_id)

        try:
            with self._lock:
                self._created_at = datetime.now(timezone.utc)
                self._last_accessed = self._created_at

            duration = time.time() - start
            _log_memory("refresh_ttl", "short_term",
                       session_id=self.session_id, duration=duration, success=True)
            logger.debug("ShortTermMemory TTL refreshed")
            return True
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "ShortTermMemory.refresh_ttl", extra={})
            _log_memory("refresh_ttl", "short_term",
                       session_id=self.session_id, duration=duration, success=False)
            raise

    def __len__(self) -> int:
        """
        Retorna número de mensajes actuales.

        Returns:
            int: Total de mensajes en memoria

        Example:
        >>> len(memory)
        15
        """
        self._cleanup_expired()
        return len(self._messages)

    def __bool__(self) -> bool:
        """
        Retorna True si hay mensajes no expirados.

        Returns:
            bool: True si hay mensajes

        Example:
        >>> if memory:
        ...     print("Hay mensajes en memoria")
        """
        self._cleanup_expired()
        return len(self._messages) > 0

    def __iter__(self):
        """
        Itera sobre mensajes no expirados.

        Yields:
            Dict: Cada mensaje en la memoria

        Example:
        >>> for msg in memory:
        ...     print(f"{msg['role']}: {msg['content']}")
        """
        self._cleanup_expired()
        with self._lock:
            for msg in self._messages:
                yield msg

    def __repr__(self) -> str:
        """
        Representación string del objeto.

        Returns:
            str: Representación legible

        Example:
        >>> print(memory)
        ShortTermMemory(session=abc123, messages=15, max=50)
        """
        try:
            return (
                f"ShortTermMemory(session={self.session_id[:8] if self.session_id else 'None'}, "
                f"messages={len(self._messages)}, max={self.max_messages})"
            )
        except Exception:
            return f"ShortTermMemory(session={self.session_id})"

    def __enter__(self):
        """
        Context manager entry.

        Example:
        >>> with ShortTermMemory() as memory:
        ...     memory.add("user", "test")
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit.
        Limpia recursos al salir del contexto.
        """
        # No hay recursos externos que limpiar en short-term
        return False

    def __del__(self):
        """
        Destructor.

        No hay recursos externos que limpiar (sin ficheros abiertos ni
        conexiones propias): no se necesita ningún `try/except` silencioso.
        """
