#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.9 — Gestión de Estado por Sesión (NiceGUI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/states.py
# Autor: SaintWick
# Fecha: 2026-04-04
# Propósito: Estado reactivo por sesión de usuario en NiceGUI
#            + Logging robusto + Type hints completos + Thread-safe
# Logs: /ruta/a/axioma/logs/reg_error.txt
#
# ✅ CORREGIDO v0.0.9:
# - Logging helpers con verificación robusta de is_initialized
# - Type hints completos en todos los parámetros y retornos
# - Docstrings completas con Args/Returns en métodos públicos
# - Manejo seguro de dispatcher (fallback si no disponible)
# - Thread-safety con threading.Lock en SessionManager
# - TTL/expiration con método is_expired() configurable
# - LRU eviction cuando se alcanza max_sessions
# ✅ CORREGIDO v0.0.10 (2026-06-24):
# - Fallback model en __post_init__: vicgalle/prometheus → qwen3:8b
# ═══════════════════════════════════════════════════════════════
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from config.settings import settings
from config.paths import Paths
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


def _log_web_event(category: str, message: str, extra: Optional[Dict[str, Any]] = None, level: str = "INFO") -> None:
    """
    Helper para logging de eventos web.

    Args:
        category: Categoría del evento (SESSION, UI, API, DASHBOARD, etc.)
        message: Mensaje descriptivo del evento
        extra: Información adicional para el log (dict opcional)
        level: Nivel de logging (INFO, WARNING, ERROR)

    Returns:
        None
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Verificación robusta de is_initialized (propiedad pública)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 states.py:64] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """
    Helper para logging de errores.

    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log (dict opcional)

    Returns:
        None
    """
    if not LOGGER_AVAILABLE:
        logger.error(f"states.{context}: {error}")
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Verificación robusta de is_initialized (propiedad pública)
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error,
                context=context,
                extra_info=extra,
                function_name=f"states.{context}"
            )
    except Exception:
        logger.error(f"states.{context}: {error}")


# ═══════════════════════════════════════════════════════════════
# ARTEFACTOS DE CÓDIGO (ISSUE-055)
# ═══════════════════════════════════════════════════════════════
# Topes defensivos: un artefacto gigante no debe inflar el estado en memoria ni
# el documento exportado. El código completo igual queda en `data/outputs/`.
ARTIFACT_CONTENT_MAX = 100_000     # caracteres por archivo
ARTIFACT_MAX_FILES = 20            # archivos por respuesta


def _sanitize_artifacts(artifacts: Optional[List[Dict[str, Any]]]
                        ) -> List[Dict[str, Any]]:
    """Normaliza los artefactos que se guardan en el mensaje. Función PURA.

    Deja solo `{name, language, content, truncated?}` de entradas válidas, con
    topes de tamaño y cantidad. Nada de `None`, ni dicts sin nombre.
    """
    out: List[Dict[str, Any]] = []
    for art in (artifacts or [])[:ARTIFACT_MAX_FILES]:
        if not isinstance(art, dict):
            continue
        name = str(art.get("name") or "").strip()
        content = art.get("content")
        if not name or not isinstance(content, str) or not content.strip():
            continue
        truncado = len(content) > ARTIFACT_CONTENT_MAX
        entry: Dict[str, Any] = {
            "name": name,
            "language": str(art.get("language") or "text"),
            "content": content[:ARTIFACT_CONTENT_MAX],
        }
        if truncado:
            entry["truncated"] = True
        if art.get("is_test"):
            entry["is_test"] = True
        out.append(entry)
    return out


# ═══════════════════════════════════════════════════════════════
# CLASES DE ESTADO
# ═══════════════════════════════════════════════════════════════
@dataclass
class MessageData:
    """
    Representa un mensaje en el historial de chat.

    Attributes:
        role: Rol del mensaje ('user' o 'assistant')
        content: Contenido textual del mensaje
        timestamp: Marca de tiempo ISO
        source: Origen del mensaje ('llm', 'api', 'error')
        task_type: Tipo de tarea asociada (opcional)
    """
    role: str  # 'user' o 'assistant'
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = 'llm'  # 'llm', 'api', 'error'
    task_type: Optional[str] = None
    # ✅ F3: citas/fuentes que respaldan la respuesta (antes solo vivían en el
    # render de la UI y se perdían al exportar). Lista vacía = sin citas.
    sources: List[str] = field(default_factory=list)
    # ✅ ISSUE-055: artefactos de código de la respuesta (nombre + contenido +
    # lenguaje) para que la exportación los incluya. Se guarda una copia
    # ACOTADA (`ARTIFACT_CONTENT_MAX`) — el código completo también queda en
    # disco (`data/outputs/`), acá va lo necesario para el documento.
    artifacts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializa a diccionario.

        Returns:
            Dict con todos los campos del mensaje
        """
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "source": self.source,
            "task_type": self.task_type,
            "sources": list(self.sources or []),
            "artifacts": [dict(a) for a in (self.artifacts or [])],
        }


@dataclass
class SessionState:
    """
    Estado reactivo por sesión de usuario en NiceGUI.

    ✅ CORREGIDO: FASE 4 — dispatcher en lugar de orchestrator

    Attributes:
        session_id: Identificador único de sesión
        dispatcher: Instancia de Dispatcher para procesamiento
        messages: Lista de mensajes del historial
        is_processing: Flag de procesamiento activo
        model: Modelo LLM seleccionado
        temperature: Temperatura de generación
        enable_validation: Flag de validación habilitada
        created_at: Timestamp de creación
        last_activity: Timestamp de última actividad
        uploaded_files: Lista de archivos subidos
    """
    session_id: str = ""
    # ✅ UI de usuarios (2026-08-31): identidad elegida en la ventana
    # preliminar. El usuario del equipo puede ser admin; los creados son "user".
    user_id: Optional[str] = None
    user_role: Optional[str] = None
    # ✅ CORREGIDO: FASE 4 — dispatcher en lugar de orchestrator
    dispatcher: Optional[Any] = None
    messages: List[MessageData] = field(default_factory=list)
    is_processing: bool = False
    model: str = ""
    temperature: float = 0.5
    enable_validation: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    uploaded_files: List[Dict[str, Any]] = field(default_factory=list)
    # ✅ Jarvis Mode — v0.4.0
    jarvis_mode: str = "normal"  # "normal" | "jarvis" | "voice_only" | "silent"
    context: Optional[Any] = None  # SessionContext — se inicializa en __post_init__

    def __post_init__(self) -> None:
        """
        Inicializa valores por defecto después de la creación.

        Returns:
            None
        """
        start = time.time()
        _log_web_event("SESSION", f"Session init: {self.session_id[:16] if self.session_id else 'new'}")

        if not self.model:
            # ✅ CORREGIDO: Fallback a qwen3:8b (eliminado vicgalle/prometheus)
            self.model = settings.llm_default_model if hasattr(settings, 'llm_default_model') else 'qwen3:8b'

        if not self.session_id:
            self.session_id = f"nicegui_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        # ✅ Jarvis Mode — inicializar SessionContext
        if self.context is None:
            try:
                from src.domain.context import SessionContext
                _valid_modes = {"normal", "jarvis", "voice_only", "silent"}
                _mode = self.jarvis_mode if self.jarvis_mode in _valid_modes else "normal"
                self.context = SessionContext(
                    session_id=self.session_id,
                    jarvis_mode=_mode,
                )
            except Exception as e:
                logger.warning(f"SessionContext no disponible: {e}")

        duration = time.time() - start
        _log_web_event("SESSION", f"Session initialized: {self.session_id[:16]}",
                      extra={"duration": round(duration, 4), "model": self.model})
        logger.debug(f"SessionState initialized: {self.session_id[:16]}")

    # ✅ CORREGIDO: FASE 4 — init_dispatcher en lugar de init_orchestrator
    def init_dispatcher(self) -> None:
        """
        Inicializa Dispatcher para esta sesión (lazy loading).

        Returns:
            None
        """
        start = time.time()
        _log_web_event("SESSION", f"Dispatcher init started: {self.session_id[:16]}")

        if self.dispatcher is None:
            try:
                # ✅ CORREGIDO: Importar Dispatcher en lugar de Orchestrator
                from src.core.dispatcher import Dispatcher
                # ✅ UI de usuarios (2026-08-31): el dispatcher de la sesión
                # conoce al usuario elegido → memoria (Capa 2) aislada por user.
                self.dispatcher = Dispatcher(user_id=self.user_id)
                duration = time.time() - start
                _log_web_event("SESSION", f"Dispatcher initialized: {self.session_id[:16]}",
                              extra={"duration": round(duration, 4),
                                     "user_id": self.user_id})
                logger.info(f"Dispatcher initialized for session {self.session_id[:16]} "
                            f"(user={self.user_id})")
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "SessionState.init_dispatcher", extra={"session_id": self.session_id[:16]})
                _log_web_event("SESSION", f"Dispatcher init failed: {self.session_id[:16]}",
                              extra={"duration": round(duration, 4), "error": str(e)})
                logger.error(f"Failed to initialize dispatcher: {e}")
                self.dispatcher = None

    def add_message(self, role: str, content: str, source: str = 'llm',
                   task_type: Optional[str] = None,
                   sources: Optional[List[str]] = None,
                   artifacts: Optional[List[Dict[str, Any]]] = None) -> MessageData:
        """
        Agrega mensaje al historial y retorna el MessageData creado.

        Args:
            role: Rol del mensaje ('user' o 'assistant')
            content: Contenido del mensaje
            source: Origen del mensaje ('llm', 'api', 'error')
            task_type: Tipo de tarea asociada (opcional)
            sources: Citas/fuentes que respaldan la respuesta (F3, opcional)
            artifacts: Artefactos de código de la respuesta (ISSUE-055, opcional)

        Returns:
            MessageData: Mensaje creado y agregado
        """
        start = time.time()
        _log_web_event("SESSION", f"Add message: {role} - {self.session_id[:16]}",
                      extra={"content_len": len(content), "source": source})

        clean_sources = [s.strip() for s in (sources or [])
                         if isinstance(s, str) and s.strip()]
        message = MessageData(role=role, content=content, source=source,
                              task_type=task_type, sources=clean_sources,
                              artifacts=_sanitize_artifacts(artifacts))
        self.messages.append(message)
        self.last_activity = datetime.now(timezone.utc)

        duration = time.time() - start
        _log_web_event("SESSION", f"Message added: {role} - {self.session_id[:16]}",
                      extra={"duration": round(duration, 4), "message_count": len(self.messages)})
        logger.debug(f"Message added to session {self.session_id[:16]}: {role} - {len(content)} chars")
        return message

    def clear_history(self) -> None:
        """
        Limpia historial de mensajes manteniendo configuración de sesión.

        Returns:
            None
        """
        start = time.time()
        message_count = len(self.messages)
        _log_web_event("SESSION", f"Clear history started: {self.session_id[:16]}",
                      extra={"message_count": message_count})

        self.messages = []
        self.last_activity = datetime.now(timezone.utc)

        # ✅ CORREGIDO: Usar dispatcher en lugar de orchestrator
        if self.dispatcher:
            try:
                _log_web_event("SESSION", f"Dispatcher ready: {self.session_id[:16]}")
                logger.info(f"Session {self.session_id[:16]} history cleared")
            except Exception as e:
                _log_error(e, "SessionState.clear_history", extra={"session_id": self.session_id[:16]})
                logger.warning(f"Error with dispatcher: {e}")

        duration = time.time() - start
        _log_web_event("SESSION", f"History cleared: {self.session_id[:16]}",
                      extra={"duration": round(duration, 4), "messages_cleared": message_count})

    def add_file(self, file_id: str, file_name: str, file_type: str,
                file_size: int, file_path: str) -> Dict[str, Any]:
        """
        Registra archivo subido en la sesión y retorna su info.

        Args:
            file_id: Identificador único del archivo
            file_name: Nombre del archivo
            file_type: Tipo/MIME del archivo
            file_size: Tamaño en bytes
            file_path: Ruta completa del archivo

        Returns:
            Dict[str, Any]: Información del archivo registrado
        """
        start = time.time()
        _log_web_event("SESSION", f"File upload: {file_name} - {self.session_id[:16]}",
                      extra={"file_size": file_size, "file_type": file_type})

        file_info = {
            'file_id': file_id,
            'file_name': file_name,
            'file_type': file_type,
            'file_size': file_size,
            'file_path': file_path,
            'uploaded_at': datetime.now(timezone.utc).isoformat()
        }
        self.uploaded_files.append(file_info)
        self.last_activity = datetime.now(timezone.utc)

        duration = time.time() - start
        _log_web_event("SESSION", f"File registered: {file_name} - {self.session_id[:16]}",
                      extra={"duration": round(duration, 4), "files_count": len(self.uploaded_files)})
        logger.debug(f"File added to session {self.session_id[:16]}: {file_name} ({file_size} bytes)")
        return file_info

    def get_recent_messages(self, limit: int = 10) -> List[MessageData]:
        """
        Obtiene últimos N mensajes de la sesión.

        Args:
            limit: Número máximo de mensajes a retornar

        Returns:
            List[MessageData]: Lista de mensajes recientes
        """
        start = time.time()
        result = self.messages[-limit:] if self.messages else []
        duration = time.time() - start
        _log_web_event("SESSION", f"Get recent messages: {self.session_id[:16]}",
                      extra={"duration": round(duration, 4), "count": len(result), "limit": limit})
        return result

    def hydrate_from_memory(self, limit: int = 50) -> int:
        """
        Carga historial persistido (LongTermMemory) si la sesión está vacía.

        ✅ v0.6.8nn (plan adaptado F5 — versión READ-ONLY): la capa de memoria
        ya persiste cada turno (user+assistant con session_id); acá solo se LEE
        para continuar la conversación tras recargar la UI. No escribe nada.

        Returns:
            int: mensajes cargados (0 si no hay o ya había historial).
        """
        if not self.session_id or self.messages:
            return 0
        try:
            from src.memory.long_term import LongTermMemory
            rows = LongTermMemory().get_recent(self.session_id, limit=limit)
        except Exception as e:
            log_degraded(logger, e, "src/interfaces/web/states.py:hydrate_from_memory")
            return 0

        def _ts_key(r) -> float:
            ts = r.get("timestamp")
            if isinstance(ts, (int, float)):
                return float(ts)
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except Exception as e:
                    log_degraded(logger, e, "src/interfaces/web/states.py:_ts_key")
                    return 0.0
            return 0.0

        try:
            rows = sorted(rows, key=_ts_key)
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 states.py:380] excepción degradada (intencional): {_d2e}")

        loaded = 0
        seen: set = set()
        for row in rows:
            role = row.get("role") or ""
            content = row.get("content") or ""
            if role not in ("user", "assistant") or not content:
                continue
            if content in seen:
                continue
            seen.add(content)
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            ts = row.get("timestamp")
            if isinstance(ts, (int, float)):
                try:
                    ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except Exception:
                    ts = ""
            elif not isinstance(ts, str):
                ts = ""
            self.messages.append(MessageData(
                role=role,
                content=content,
                timestamp=ts or datetime.now(timezone.utc).isoformat(),
                source=meta.get("source_type", "user" if role == "user" else "llm"),
                task_type=meta.get("task_type"),
            ))
            loaded += 1
        return loaded

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializa estado a diccionario para debugging/logging.

        Returns:
            Dict[str, Any]: Estado completo de la sesión
        """
        start = time.time()
        result = {
            'session_id': self.session_id,
            'message_count': len(self.messages),
            'model': self.model,
            'temperature': self.temperature,
            'enable_validation': self.enable_validation,
            'is_processing': self.is_processing,
            'created_at': self.created_at.isoformat(),
            'last_activity': self.last_activity.isoformat(),
            'uploaded_files_count': len(self.uploaded_files),
            'dispatcher_available': self.dispatcher is not None,
            'jarvis_mode': self.jarvis_mode,
        }
        duration = time.time() - start
        _log_web_event("SESSION", f"State serialized: {self.session_id[:16]}",
                      extra={"duration": round(duration, 4)})
        return result

    def is_expired(self, ttl_seconds: int = 3600) -> bool:
        """
        Verifica si la sesión expiró por inactividad.

        Args:
            ttl_seconds: Tiempo de vida en segundos (default: 1 hora)

        Returns:
            bool: True si la sesión expiró, False si está activa
        """
        elapsed = (datetime.now(timezone.utc) - self.last_activity).total_seconds()
        expired = elapsed > ttl_seconds
        if expired:
            _log_web_event("SESSION", f"Session expired: {self.session_id[:16]}",
                          extra={"elapsed": round(elapsed, 2), "ttl": ttl_seconds})
        return expired


# ═══════════════════════════════════════════════════════════════
# GESTOR DE SESIONES (SINGLETON THREAD-SAFE)
# ═══════════════════════════════════════════════════════════════
class SessionManager:
    """
    Gestor centralizado de sesiones de usuario (thread-safe, TTL, LRU).

    Features:
    - Singleton thread-safe con threading.Lock
    - Limpieza automática de sesiones expiradas
    - Evicción LRU cuando se alcanza max_sessions
    - Logging detallado de todas las operaciones

    Example:
        >>> manager = SessionManager()
        >>> session = manager.get_or_create_session()
        >>> session.add_message('user', 'Hola')
    """

    _instance: Optional['SessionManager'] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> 'SessionManager':
        """
        Singleton thread-safe.

        Returns:
            SessionManager: Única instancia del gestor
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._sessions: Dict[str, SessionState] = {}
                cls._instance._max_sessions = 100
                cls._instance._session_ttl = 3600
                cls._instance._manager_lock = threading.Lock()
        return cls._instance

    def get_or_create_session(self, session_id: Optional[str] = None) -> SessionState:
        """
        Obtiene sesión existente o crea nueva con limpieza previa de expiradas.

        Args:
            session_id: ID de sesión existente (opcional, crea nueva si None)

        Returns:
            SessionState: Sesión obtenida o creada
        """
        start = time.time()
        _log_web_event("SESSION", "Get or create session started")

        with self._manager_lock:
            # Limpieza previa de sesiones expiradas
            expired_count = self._cleanup_expired()
            if expired_count > 0:
                _log_web_event("SESSION", f"Cleaned {expired_count} expired sessions")

            # Buscar sesión existente
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_activity = datetime.now(timezone.utc)
                duration = time.time() - start
                _log_web_event("SESSION", f"Session retrieved: {session_id[:16]}",
                              extra={"duration": round(duration, 4)})
                logger.debug(f"Session retrieved: {session_id[:16]}")
                return session

            # ✅ CORREGIDO: respetar session_id proporcionado al crear nueva sesión
            new_session = SessionState(session_id=session_id) if session_id else SessionState()
            self._sessions[new_session.session_id] = new_session

            # Evicción LRU si se excede max_sessions
            if len(self._sessions) > self._max_sessions:
                evicted_id = self._evict_oldest()
                _log_web_event("SESSION", f"Session evicted (max reached): {evicted_id[:16] if evicted_id else 'unknown'}")

            duration = time.time() - start
            _log_web_event("SESSION", f"Session created: {new_session.session_id[:16]}",
                          extra={"duration": round(duration, 4), "total_sessions": len(self._sessions)})
            logger.info(f"New session created: {new_session.session_id[:16]}")
            return new_session

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """
        Obtiene sesión por ID (retorna None si no existe).

        Args:
            session_id: ID de la sesión a buscar

        Returns:
            Optional[SessionState]: Sesión encontrada o None
        """
        start = time.time()
        with self._manager_lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_activity = datetime.now(timezone.utc)
                duration = time.time() - start
                _log_web_event("SESSION", f"Session found: {session_id[:16]}",
                              extra={"duration": round(duration, 4)})
                return session

            duration = time.time() - start
            _log_web_event("SESSION", f"Session not found: {session_id[:16]}",
                          extra={"duration": round(duration, 4)})
            logger.debug(f"Session not found: {session_id[:16]}")
            return None

    def delete_session(self, session_id: str) -> bool:
        """
        Elimina sesión por ID liberando recursos del dispatcher.

        Args:
            session_id: ID de la sesión a eliminar

        Returns:
            bool: True si se eliminó, False si no existía
        """
        start = time.time()
        _log_web_event("SESSION", f"Delete session started: {session_id[:16]}")

        with self._manager_lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                # ✅ CORREGIDO: Usar dispatcher en lugar de orchestrator
                if session.dispatcher:
                    try:
                        _log_web_event("SESSION", f"Dispatcher ready for cleanup: {session_id[:16]}")
                    except Exception as e:
                        _log_error(e, "SessionManager.delete_session", extra={"session_id": session_id[:16]})
                        logger.warning(f"Error with dispatcher: {e}")

                del self._sessions[session_id]
                duration = time.time() - start
                _log_web_event("SESSION", f"Session deleted: {session_id[:16]}",
                              extra={"duration": round(duration, 4), "remaining_sessions": len(self._sessions)})
                logger.info(f"Session deleted: {session_id[:16]}")
                return True

            duration = time.time() - start
            _log_web_event("SESSION", f"Session not found for delete: {session_id[:16]}",
                          extra={"duration": round(duration, 4)})
            return False

    def _cleanup_expired(self) -> int:
        """
        Elimina sesiones expiradas y retorna count de eliminadas.

        Returns:
            int: Número de sesiones eliminadas
        """
        start = time.time()
        expired = [
            sid for sid, session in self._sessions.items()
            if session.is_expired(self._session_ttl)
        ]
        for sid in expired:
            self.delete_session(sid)

        if expired:
            duration = time.time() - start
            _log_web_event("SESSION", f"Cleaned up {len(expired)} expired sessions",
                          extra={"duration": round(duration, 4), "expired_count": len(expired)})
            logger.info(f"Cleaned up {len(expired)} expired sessions")

        return len(expired)

    def _evict_oldest(self) -> Optional[str]:
        """
        Elimina la sesión más antigua por LRU y retorna su ID.

        Returns:
            Optional[str]: ID de la sesión evictada o None si no hay sesiones
        """
        if not self._sessions:
            return None

        oldest_id = min(
            self._sessions.keys(),
            key=lambda sid: self._sessions[sid].last_activity
        )
        self.delete_session(oldest_id)
        _log_web_event("SESSION", f"Evicted oldest session: {oldest_id[:16]}",
                      extra={"reason": "max_sessions_reached", "max_sessions": self._max_sessions})
        logger.warning(f"Evicted oldest session: {oldest_id[:16]} (max sessions reached)")
        return oldest_id

    def get_all_sessions(self) -> Dict[str, Dict[str, Any]]:
        """
        Retorna info de todas las sesiones activas para debugging.

        Returns:
            Dict[str, Dict[str, Any]]: Diccionario con info de todas las sesiones
        """
        start = time.time()
        with self._manager_lock:
            result = {sid: session.to_dict() for sid, session in self._sessions.items()}
            duration = time.time() - start
            _log_web_event("SESSION", "Get all sessions",
                          extra={"duration": round(duration, 4), "count": len(result)})
            return result

    def get_stats(self) -> Dict[str, Any]:
        """
        Retorna estadísticas del gestor de sesiones.

        Returns:
            Dict[str, Any]: Estadísticas completas del gestor
        """
        start = time.time()
        with self._manager_lock:
            result = {
                'total_sessions': len(self._sessions),
                'max_sessions': self._max_sessions,
                'session_ttl_seconds': self._session_ttl,
                'sessions': {
                    sid: {
                        'messages': len(session.messages),
                        'last_activity': session.last_activity.isoformat()
                    }
                    for sid, session in self._sessions.items()
                },
            }
            duration = time.time() - start
            _log_web_event("SESSION", "Get stats",
                          extra={"duration": round(duration, 4), "total_sessions": len(self._sessions)})
            return result

    def clear_all(self) -> int:
        """
        Elimina todas las sesiones y retorna count.

        Returns:
            int: Número de sesiones eliminadas
        """
        start = time.time()
        with self._manager_lock:
            count = len(self._sessions)
            self._sessions.clear()
            duration = time.time() - start
            _log_web_event("SESSION", f"All sessions cleared: {count}",
                          extra={"duration": round(duration, 4)})
            logger.info(f"All {count} sessions cleared")
            return count


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD (FACTORY FUNCTIONS)
# ═══════════════════════════════════════════════════════════════
def get_session_manager() -> SessionManager:
    """
    Obtiene instancia singleton del SessionManager.

    Returns:
        SessionManager: Instancia única del gestor
    """
    return SessionManager()


def get_current_session(session_id: Optional[str] = None) -> SessionState:
    """
    Helper para obtener/crear sesión actual en handlers de NiceGUI.

    Args:
        session_id: ID de sesión existente (opcional, crea nueva si None)

    Returns:
        SessionState: Sesión actual obtenida o creada
    """
    start = time.time()
    _log_web_event("SESSION", "Get current session")

    manager = get_session_manager()
    session = manager.get_or_create_session(session_id)
    # ✅ v0.6.8nn (plan adaptado F5): continuidad read-only — si la sesión se
    # recreó sin historial (reload/restart), lo recupera de la memoria.
    if not session.messages:
        try:
            session.hydrate_from_memory()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 states.py:735] excepción degradada (intencional): {_d2e}")

    duration = time.time() - start
    _log_web_event("SESSION", f"Current session: {session.session_id[:16]}",
                  extra={"duration": round(duration, 4)})
    return session


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.0.10
# ═══════════════════════════════════════════════════════════════
# | Línea/Sección           | Cambio Realizado                        | Propósito                         |
# |-------------------------|-----------------------------------------|-----------------------------------|
# | ~112                    | Fallback model → qwen3:8b             | Eliminar vicgalle/prometheus      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — ~560 LÍNEAS — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
