#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.8.0 — Detectores y Handlers de Feedback Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/feedback_loop_detectors_handlers.py
# Autor: SaintWick
# Fecha: 2026-04-26
# Propósito: Detectores de señales implícitas, procesador de
#            feedback explícito y optimizador de parámetros.
# Versión: 0.8.0
# ═══════════════════════════════════════════════════════════════
# ✅ EXTRAÍDO DESDE feedback_loop_components.py v0.8.0
# Componentes:
#   - ImplicitFeedbackDetector  → Detecta señales implícitas del usuario
#   - ExplicitFeedbackHandler   → Procesa feedback directo (thumbs, rating)
#   - ParameterOptimizer        → Ajusta parámetros dinámicamente
# ═══════════════════════════════════════════════════════════════

import asyncio
import logging
import hashlib
import json
import os
from typing import Optional, Dict, Any, List, Union
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from config.settings import settings

from src.core.feedback_loop_components import (
    FeedbackType,
    ImplicitSignal,
    ExplicitRating,
    FeedbackRecord,
    _log_feedback,
)
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


# ============================================================================
# DETECTOR DE FEEDBACK IMPLÍCITO
# ============================================================================
class ImplicitFeedbackDetector:
    """
    Detecta señales implícitas de feedback del comportamiento del usuario.

    Estrategias de detección:
    - RE_QUERY: Misma consulta reformulada en < 5 min
    - EDIT_RESPONSE: Usuario edita respuesta generada
    - SHORT_SESSION: Sesión termina < 30s tras respuesta
    - LONG_WAIT: Usuario espera > 2 min antes de siguiente acción
    - COPY_ACTION: Usuario copia código/contenido generado
    - IGNORED: Usuario no interactúa con respuesta en > 5 min

    Thread-safe para uso concurrente.
    """

    def __init__(
        self,
        re_query_window_seconds: int = 300,
        short_session_threshold_seconds: int = 30,
        long_wait_threshold_seconds: int = 120,
        ignored_threshold_seconds: int = 300,
    ):
        """
        Inicializa detector de feedback implícito.

        Args:
            re_query_window_seconds: Ventana para detectar re-consultas (default: 300s)
            short_session_threshold_seconds: Umbral para sesión corta (default: 30s)
            long_wait_threshold_seconds: Umbral para espera larga (default: 120s)
            ignored_threshold_seconds: Umbral para respuesta ignorada (default: 300s)
        """
        self.re_query_window = timedelta(seconds=re_query_window_seconds)
        self.short_session_threshold = timedelta(seconds=short_session_threshold_seconds)
        self.long_wait_threshold = timedelta(seconds=long_wait_threshold_seconds)
        self.ignored_threshold = timedelta(seconds=ignored_threshold_seconds)

        # Historial reciente para detección de patrones
        self._recent_queries: Dict[str, List[tuple]] = defaultdict(list)  # session_id -> [(query_hash, timestamp)]
        self._recent_responses: Dict[str, List[tuple]] = defaultdict(list)  # session_id -> [(response_hash, timestamp)]
        self._lock = asyncio.Lock()

        logger.debug("ImplicitFeedbackDetector initialized")

    def _hash_text(self, text: str) -> str:
        """Genera hash corto para texto."""
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _cleanup_old_entries(self, session_id: str, max_age_seconds: int = 3600) -> None:
        """Limpia entradas antiguas del historial."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)

        # Limpiar queries
        self._recent_queries[session_id] = [
            (h, t) for h, t in self._recent_queries[session_id]
            if t > cutoff
        ]

        # Limpiar responses
        self._recent_responses[session_id] = [
            (h, t) for h, t in self._recent_responses[session_id]
            if t > cutoff
        ]

    async def detect_re_query(self, session_id: str, query: str) -> bool:
        """
        Detecta si el usuario está reformulando la misma consulta.

        Args:
            session_id: ID de sesión
            query: Consulta actual

        Returns:
            True si es una re-consulta, False en caso contrario
        """
        async with self._lock:
            self._cleanup_old_entries(session_id)

            query_hash = self._hash_text(query.lower().strip())
            now = datetime.now(timezone.utc)

            # Buscar consultas similares en la ventana
            for prev_hash, prev_time in self._recent_queries[session_id]:
                if prev_hash == query_hash and (now - prev_time) <= self.re_query_window:
                    logger.debug(f"RE_QUERY detected: session={session_id[:8]}...")
                    return True

            # Registrar consulta actual
            self._recent_queries[session_id].append((query_hash, now))
            return False

    async def detect_copy_action(self, session_id: str, response: str, action_timestamp: datetime) -> bool:
        """
        Detecta si el usuario copió la respuesta generada.

        Args:
            session_id: ID de sesión
            response: Respuesta generada
            action_timestamp: Timestamp de la acción de copiar

        Returns:
            True si se detectó acción de copiar
        """
        # En implementación real, esto se integraría con eventos de UI
        # Aquí simulamos detección basada en patrón
        response_hash = self._hash_text(response)

        async with self._lock:
            # Registrar que esta respuesta fue "copiada"
            self._recent_responses[session_id].append((response_hash, action_timestamp))
            logger.debug(f"COPY_ACTION detected: session={session_id[:8]}...")
            return True

    async def detect_ignored_response(
        self,
        session_id: str,
        response_timestamp: datetime,
        next_action_timestamp: Optional[datetime] = None,
    ) -> bool:
        """
        Detecta si el usuario ignoró la respuesta.

        Args:
            session_id: ID de sesión
            response_timestamp: Cuando se generó la respuesta
            next_action_timestamp: Timestamp de siguiente acción (None = no hubo)

        Returns:
            True si se detectó respuesta ignorada
        """
        now = next_action_timestamp or datetime.now(timezone.utc)
        elapsed = now - response_timestamp

        if elapsed >= self.ignored_threshold:
            logger.debug(f"IGNORED detected: session={session_id[:8]}..., elapsed={elapsed.total_seconds():.0f}s")
            return True
        return False

    async def detect_short_session(
        self,
        session_id: str,
        response_timestamp: datetime,
        session_end_timestamp: datetime,
    ) -> bool:
        """
        Detecta si la sesión terminó muy rápido tras la respuesta.

        Args:
            session_id: ID de sesión
            response_timestamp: Cuando se generó la respuesta
            session_end_timestamp: Cuando terminó la sesión

        Returns:
            True si es sesión corta
        """
        elapsed = session_end_timestamp - response_timestamp
        if elapsed <= self.short_session_threshold:
            logger.debug(f"SHORT_SESSION detected: session={session_id[:8]}..., elapsed={elapsed.total_seconds():.0f}s")
            return True
        return False

    async def detect_long_wait(
        self,
        session_id: str,
        response_timestamp: datetime,
        next_action_timestamp: datetime,
    ) -> bool:
        """
        Detecta si el usuario esperó mucho antes de siguiente acción.

        Args:
            session_id: ID de sesión
            response_timestamp: Cuando se generó la respuesta
            next_action_timestamp: Timestamp de siguiente acción

        Returns:
            True si es espera larga
        """
        elapsed = next_action_timestamp - response_timestamp
        if elapsed >= self.long_wait_threshold:
            logger.debug(f"LONG_WAIT detected: session={session_id[:8]}..., elapsed={elapsed.total_seconds():.0f}s")
            return True
        return False


# ============================================================================
# MANEJADOR DE FEEDBACK EXPLÍCITO
# ============================================================================
class ExplicitFeedbackHandler:
    """
    Procesa feedback explícito proporcionado por el usuario.

    Soporta:
    - Thumbs up/down (binario)
    - Calificaciones 1-5 estrellas
    - Comentarios textuales opcionales
    - Tags de categoría (útil, preciso, completo, etc.)

    Thread-safe para uso concurrente.
    """

    def __init__(self, require_comment_for_low_rating: bool = False):
        """
        Inicializa manejador de feedback explícito.

        Args:
            require_comment_for_low_rating: Si requiere comentario para ratings 1-2
        """
        self.require_comment_for_low = require_comment_for_low_rating
        self._lock = asyncio.Lock()

        logger.debug("ExplicitFeedbackHandler initialized")

    async def record_thumbs(
        self,
        session_id: str,
        task_type: str,
        agent_role: str,
        query: str,
        response: str,
        thumbs_up: bool,
        comment: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedbackRecord:
        """
        Registra feedback de thumbs up/down.

        Args:
            session_id: ID de sesión
            task_type: Tipo de tarea
            agent_role: Rol del agente
            query: Consulta original
            response: Respuesta generada
            thumbs_up: True para up, False para down
            comment: Comentario opcional del usuario
            metadata: Metadatos adicionales

        Returns:
            FeedbackRecord creado
        """
        import uuid
        rating = ExplicitRating.GOOD if thumbs_up else ExplicitRating.POOR
        return await self._create_record(
            session_id=session_id,
            task_type=task_type,
            agent_role=agent_role,
            query=query,
            response=response,
            feedback_type=FeedbackType.EXPLICIT,
            signal_or_rating=rating,
            comment=comment,
            metadata=metadata,
        )

    async def record_rating(
        self,
        session_id: str,
        task_type: str,
        agent_role: str,
        query: str,
        response: str,
        rating: Union[int, ExplicitRating],
        comment: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedbackRecord:
        """
        Registra calificación 1-5 estrellas.

        Args:
            session_id: ID de sesión
            task_type: Tipo de tarea
            agent_role: Rol del agente
            query: Consulta original
            response: Respuesta generada
            rating: Calificación (1-5 o ExplicitRating)
            comment: Comentario opcional
            tags: Tags de categoría opcionales
            metadata: Metadatos adicionales

        Returns:
            FeedbackRecord creado

        Raises:
            ValueError: Si rating inválido o comentario requerido faltante
        """
        import uuid
        # Validar rating
        if isinstance(rating, int):
            if rating < 1 or rating > 5:
                raise ValueError(f"Invalid rating: {rating}. Must be 1-5.")
            rating_enum = ExplicitRating(rating)
        else:
            rating_enum = rating

        # Validar comentario para ratings bajos si está configurado
        if self.require_comment_for_low and rating_enum.is_negative and not comment:
            raise ValueError("Comment required for negative ratings")

        # Agregar tags a metadata
        if tags:
            metadata = metadata or {}
            metadata["tags"] = tags

        return await self._create_record(
            session_id=session_id,
            task_type=task_type,
            agent_role=agent_role,
            query=query,
            response=response,
            feedback_type=FeedbackType.EXPLICIT,
            signal_or_rating=rating_enum,
            comment=comment,
            metadata=metadata,
        )

    async def _create_record(
        self,
        session_id: str,
        task_type: str,
        agent_role: str,
        query: str,
        response: str,
        feedback_type: FeedbackType,
        signal_or_rating: Union[ImplicitSignal, ExplicitRating, str],
        comment: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedbackRecord:
        """Crea y registra un FeedbackRecord."""
        import uuid

        now = datetime.now(timezone.utc)
        record_metadata = metadata or {}
        if comment:
            record_metadata["comment"] = comment

        record = FeedbackRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            task_type=task_type,
            agent_role=agent_role,
            query=query,
            response=response,
            timestamp=now,
            feedback_type=feedback_type,
            signal_or_rating=signal_or_rating,
            metadata=record_metadata,
        )

        _log_feedback(
            "record",
            session_id=session_id,
            task_type=task_type,
            feedback_type=feedback_type.value,
            success=True,
            extra={"record_id": record.id[:8], "is_positive": record.is_positive},
        )

        logger.debug(f"Explicit feedback recorded: {record}")
        return record


# ============================================================================
# OPTIMIZADOR DE PARÁMETROS
# ============================================================================
class ParameterOptimizer:
    """
    Ajusta dinámicamente parámetros del sistema basado en feedback.

    Parámetros optimizables:
    - temperature: Para generación de respuestas
    - timeout: Timeouts por tipo de tarea
    - similarity_threshold: Para búsqueda semántica
    - max_retries: Reintentos para agentes

    Estrategias:
    - Moving average de success rate por tarea
    - Ajuste gradual (no cambios bruscos)
    - Límites configurables para evitar overfitting
    """

    def __init__(
        self,
        adjustment_window_size: int = 50,
        min_adjustment_step: float = 0.05,
        max_adjustment_step: float = 0.20,
        cooldown_seconds: int = 300,
        persist_path: Optional[str] = None,
    ):
        """
        Inicializa optimizador de parámetros.

        Args:
            adjustment_window_size: Ventana de interacciones para decidir ajuste
            min_adjustment_step: Paso mínimo de ajuste (default: 0.05)
            max_adjustment_step: Paso máximo de ajuste (default: 0.20)
            cooldown_seconds: Tiempo mínimo entre ajustes del mismo parámetro
            persist_path: Ruta JSON para persistir el estado aprendido
                (`_feedback_history` + `_current_params`). `None` = en memoria
                (comportamiento previo). ✅ A1/ISSUE-075: los tests deben pasar
                un path en `tmp_path` (R17) para no escribir en los datos del
                usuario.
        """
        self.window_size = adjustment_window_size
        self.min_step = min_adjustment_step
        self.max_step = max_adjustment_step
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._persist_path = persist_path

        # Historial de feedback por tarea
        self._feedback_history: Dict[str, List[bool]] = defaultdict(list)  # task_type -> [is_positive]

        # Último ajuste por parámetro
        self._last_adjustment: Dict[str, datetime] = {}

        # Parámetros actuales (copias locales para optimización)
        self._current_params: Dict[str, Dict[str, float]] = {
            "temperature": {"default": settings.default_temperature},
            "similarity_threshold": {"default": 0.5},
            "timeout_multiplier": {"default": 1.0},
        }

        self._lock = asyncio.Lock()

        # ✅ A1 (ISSUE-075): cargar estado aprendido si existe persistencia.
        if self._persist_path:
            self._load()

        logger.debug("ParameterOptimizer initialized")

    def _get_key(self, param: str, task_type: Optional[str] = None) -> str:
        """Genera key única para parámetro + tarea."""
        return f"{param}:{task_type or 'default'}"

    def _can_adjust(self, param: str, task_type: Optional[str] = None) -> bool:
        """Verifica si se puede ajustar el parámetro (cooldown)."""
        key = self._get_key(param, task_type)
        last = self._last_adjustment.get(key)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last) >= self.cooldown

    def _record_feedback(self, task_type: str, is_positive: bool) -> None:
        """Registra feedback en historial para optimización."""
        history = self._feedback_history[task_type]
        history.append(is_positive)

        # Mantener ventana fija
        if len(history) > self.window_size:
            history.pop(0)

        # ✅ A1: persistir estado aprendido (historial de éxito/fallo por tarea)
        self._persist()

    # ── Persistencia (A1 / ISSUE-075) ─────────────────────────────────────
    # ANTES: `_feedback_history` y `_current_params` vivían SOLO en memoria →
    # el sistema "aprendía" y se olvidaba en cada reinicio. Ahora se persisten
    # a un JSON chico (estado aprendido por tarea). `_last_adjustment` (cooldown)
    # es transitorio y NO se persiste: tras reiniciar se permite un ajuste
    # inmediato, lo cual es inofensivo.

    def _load(self) -> None:
        """Carga estado aprendido desde `_persist_path` si existe."""
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            hist = (data or {}).get("feedback_history", {}) or {}
            self._feedback_history = defaultdict(list, {
                k: [bool(x) for x in v]
                for k, v in hist.items() if isinstance(v, list)
            })
            params = (data or {}).get("current_params", {}) or {}
            for param, values in params.items():
                if isinstance(values, dict) and param in self._current_params:
                    for k, v in values.items():
                        try:
                            self._current_params[param][k] = float(v)
                        except (TypeError, ValueError) as e:
                            # R3: valor persistido no numérico → se conserva el default.
                            log_degraded(
                                logger, e,
                                f"FeedbackLoop.ParameterOptimizer._load (parámetro inválido {param}.{k})",
                                contract_level=logging.DEBUG,
                            )
            logger.info(f"[ParameterOptimizer] estado cargado desde {self._persist_path}")
        except FileNotFoundError:
            # Primera corrida: no hay estado previo que cargar (situación esperada).
            logger.debug(f"[ParameterOptimizer] sin estado previo en {self._persist_path}")
        except Exception as e:
            logger.warning(f"[ParameterOptimizer] no se pudo cargar {self._persist_path}: {e}")

    def _persist(self) -> None:
        """Persiste estado aprendido a `_persist_path` (atómico: temp + rename)."""
        if not self._persist_path:
            return
        try:
            payload = {
                "feedback_history": {k: list(v) for k, v in self._feedback_history.items()},
                "current_params": {
                    p: dict(vals) for p, vals in self._current_params.items()
                },
            }
            tmp = f"{self._persist_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._persist_path)
        except Exception as e:
            logger.warning(f"[ParameterOptimizer] no se pudo persistir {self._persist_path}: {e}")

    def _calculate_adjustment(self, task_type: str) -> Optional[float]:
        """
        Calcula ajuste recomendado basado en historial.

        Returns:
            Float de ajuste (-max_step a +max_step) o None si no hay suficiente data
        """
        history = self._feedback_history.get(task_type, [])
        if len(history) < self.window_size // 2:
            return None  # No hay suficiente data

        # Calcular success rate reciente
        recent = history[-self.window_size:]
        success_rate = sum(1 for x in recent if x) / len(recent)

        # Target: 70-80% success rate es ideal
        target_low, target_high = 0.70, 0.80

        if success_rate < target_low:
            # Baja calidad → ajustar para ser más conservador
            return -min(self.max_step, max(self.min_step, (target_low - success_rate)))
        elif success_rate > target_high:
            # Alta calidad → permitir más creatividad
            return min(self.max_step, max(self.min_step, (success_rate - target_high)))

        return None  # Dentro del rango ideal, no ajustar

    async def adjust_parameter(
        self,
        param: str,
        task_type: str,
        current_value: float,
        min_value: float,
        max_value: float,
    ) -> float:
        """
        Ajusta un parámetro basado en feedback histórico.

        Args:
            param: Nombre del parámetro a ajustar
            task_type: Tipo de tarea específico
            current_value: Valor actual del parámetro
            min_value: Valor mínimo permitido
            max_value: Valor máximo permitido

        Returns:
            Nuevo valor ajustado (o current_value si no se ajusta)
        """
        async with self._lock:
            if not self._can_adjust(param, task_type):
                logger.debug(f"Parameter {param} for {task_type} in cooldown")
                return current_value

            adjustment = self._calculate_adjustment(task_type)
            if adjustment is None:
                return current_value

            # Aplicar ajuste con límites
            new_value = current_value + adjustment
            new_value = max(min_value, min(max_value, new_value))

            # Registrar ajuste
            self._last_adjustment[self._get_key(param, task_type)] = datetime.now(timezone.utc)
            self._current_params.setdefault(param, {})[task_type] = new_value

            # ✅ A1: persistir el valor aprendido (sobrevive reinicios)
            self._persist()

            _log_feedback(
                "adjust",
                task_type=task_type,
                feedback_type="parameter",
                success=True,
                extra={
                    "param": param,
                    "old_value": round(current_value, 4),
                    "new_value": round(new_value, 4),
                    "adjustment": round(adjustment, 4),
                },
            )

            logger.info(
                f"Parameter adjusted: {param} for {task_type}: "
                f"{current_value:.4f} → {new_value:.4f} (Δ{adjustment:+.4f})"
            )

            return new_value

    def record_interaction(self, task_type: str, is_positive: bool) -> None:
        """Registra interacción para futuras optimizaciones."""
        self._record_feedback(task_type, is_positive)

    def get_current_value(self, param: str, task_type: Optional[str] = None) -> float:
        """Obtiene valor actual de un parámetro.

        ✅ FIX (plan_fiabilidad P1.3, 2026-09-14): antes esta función buscaba
        la clave COMPUESTA de _get_key() ("param:task_type") dentro de
        _current_params, pero adjust_parameter() guarda el ajuste bajo la
        clave PLANA `task_type` (`self._current_params.setdefault(param,
        {})[task_type] = new_value`). El mismatch hacía que get_current_value()
        NUNCA devolviera un valor ajustado — siempre caía al "default"
        estático, aunque adjust_parameter() sí hubiera calculado y guardado
        un ajuste real. _get_key() sigue siendo correcto para
        _last_adjustment (cooldown), que es un espacio de claves distinto.
        """
        params_for_param = self._current_params.get(param, {})
        key = task_type or "default"
        return params_for_param.get(key, params_for_param.get("default", 0.0))

    def get_adjusted_value(self, param: str, task_type: str) -> Optional[float]:
        """Valor APRENDIDO para `task_type`, o None si nunca hubo ajuste.

        ✅ FIX (revisión P1.3, 2026-09-14): `get_current_value()` devuelve el
        DEFAULT cuando aún no hay ajuste para la tarea (su contrato es "valor
        efectivo actual"). El consumidor de temperatura necesita distinguir
        "hay un ajuste real" de "todavía no aprendí nada", porque en el segundo
        caso NO debe pisar la temperatura configurada por tarea en models.yaml.
        """
        return self._current_params.get(param, {}).get(task_type)

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del optimizador."""
        return {
            "window_size": self.window_size,
            "adjustments_count": len(self._last_adjustment),
            "tracked_tasks": len(self._feedback_history),
            "current_params": {
                param: {k: round(v, 4) for k, v in values.items()}
                for param, values in self._current_params.items()
            },
        }


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE COMPONENTES
# ═══════════════════════════════════════════════════════════════
# | Componente                  | Propósito                        |
# |-----------------------------|----------------------------------|
# | ImplicitFeedbackDetector    | Detecta señales implícitas       |
# | ExplicitFeedbackHandler     | Procesa feedback explícito       |
# | ParameterOptimizer          | Ajusta parámetros dinámicamente  |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════
