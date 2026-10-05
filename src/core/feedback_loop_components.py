#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.8.2 — Componentes de Feedback Loop (Refactorizado)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/feedback_loop_components.py
# Autor: SaintWick
# Fecha: 2026-04-26
# Propósito: Clases auxiliares para feedback_loop.py (enums, dataclasses,
#            logging helpers, EWMA threshold optimizer)
# Versión: 0.8.2
# ═══════════════════════════════════════════════════════════════
# ✅ REFACTORIZADO DESDE feedback_loop.py v0.7.1
# ✅ v0.8.1: ImplicitFeedbackDetector, ExplicitFeedbackHandler,
#            ParameterOptimizer → feedback_loop_detectors_handlers.py
# ✅ v0.8.1: datetime.utcnow() → datetime.now(timezone.utc)
# ✅ v0.8.2: QualityMetrics persiste en SQLite (data/axioma.db)
# ═══════════════════════════════════════════════════════════════

import logging
import threading
import hashlib
import sqlite3 as _sqlite3
import threading as _threading
from collections import deque
from enum import Enum, IntEnum
from typing import Optional, Dict, Any, List, Union
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

from config.settings import settings
from config.paths import Paths
from src.domain.types import TaskType, AgentRole
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_feedback(op: str, session_id: str = "", task_type: str = "",
                  feedback_type: str = "", success: bool = True,
                  extra: Dict[str, Any] = None):
    """
    Helper para logging de operaciones de feedback.

    Args:
        op: Operación realizada (record, optimize, adjust, etc.)
        session_id: ID de sesión asociada
        task_type: Tipo de tarea afectada
        feedback_type: Tipo de feedback (implicit/explicit)
        success: Si la operación fue exitosa
        extra: Información adicional para el log
    """
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_event(
                category="FEEDBACK_LOOP",
                message=f"{op} {feedback_type}",
                level="DEBUG" if success else "WARNING",
                extra={
                    "session_id": session_id,
                    "task_type": task_type,
                    "feedback_type": feedback_type,
                    **(extra or {}),
                }
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/feedback_loop_components.py:_log_feedback")


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/core/feedback_loop_components.py:_log_error")


# ============================================================================
# ENUMS — Tipado fuerte para feedback
# ============================================================================
class FeedbackType(str, Enum):
    """
    Tipos de feedback soportados.

    IMPLICIT  → Señales inferidas del comportamiento del usuario
    EXPLICIT  → Feedback directo proporcionado por el usuario
    """
    IMPLICIT = "implicit"
    EXPLICIT = "explicit"


class ImplicitSignal(str, Enum):
    """
    Señales implícitas detectables.

    RE_QUERY      → Usuario reformula la misma consulta
    EDIT_RESPONSE → Usuario edita/corrige la respuesta generada
    SHORT_SESSION → Sesión termina muy rápido tras respuesta
    LONG_WAIT     → Usuario espera mucho antes de siguiente acción
    COPY_ACTION   → Usuario copia código/contenido generado
    IGNORED       → Usuario ignora completamente la respuesta
    ROUTED        → ✅ v0.6.9v: el router/dispatcher completó una decisión de
                    ruteo para esta request (evento NEUTRO, no una reacción del
                    usuario). Usado por `smart_router.route()` y por
                    `Dispatcher.process()` al registrar el intercambio.
                    ⚠️ Faltaba: AMBOS call sites usaban `ImplicitSignal.ROUTED` y
                    fallaban con AttributeError silenciado en DEBUG → la señal
                    NUNCA se registraba (bug ISSUE-033).
    """
    RE_QUERY = "re_query"
    EDIT_RESPONSE = "edit_response"
    SHORT_SESSION = "short_session"
    LONG_WAIT = "long_wait"
    COPY_ACTION = "copy_action"
    IGNORED = "ignored"
    ROUTED = "routed"


class ExplicitRating(IntEnum):
    """
    Calificaciones explícitas (1-5 estrellas).

    1 = Muy malo, 2 = Malo, 3 = Neutral, 4 = Bueno, 5 = Excelente
    """
    VERY_POOR = 1
    POOR = 2
    NEUTRAL = 3
    GOOD = 4
    EXCELLENT = 5

    @property
    def is_positive(self) -> bool:
        """True si la calificación es positiva (4-5)."""
        return self.value >= 4

    @property
    def is_negative(self) -> bool:
        """True si la calificación es negativa (1-2)."""
        return self.value <= 2


# ============================================================================
# DATACLASSES — Estructuras de datos para feedback
# ============================================================================
@dataclass
class FeedbackRecord:
    """
    Registro estructurado de una interacción con feedback.

    Attributes:
        id: UUID único del registro (auto-generado)
        session_id: ID de sesión del usuario
        task_type: Tipo de tarea ejecutada
        agent_role: Rol del agente que respondió
        query: Consulta original del usuario
        response: Respuesta generada por el sistema
        timestamp: Timestamp de la interacción (UTC)
        feedback_type: IMPLICIT o EXPLICIT
        signal_or_rating: Señal implícita o calificación explícita
        metadata: Metadatos adicionales (latencia, tokens, etc.)
        _hash: Hash para deduplicación (auto-calculado)
    """
    id: str
    session_id: str
    task_type: str
    agent_role: str
    query: str
    response: str
    timestamp: datetime
    feedback_type: FeedbackType
    signal_or_rating: Union[ImplicitSignal, ExplicitRating, str]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Calcula hash único para deduplicación."""
        content = f"{self.session_id}:{self.query}:{self.response}:{self.timestamp.isoformat()}"
        self._hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    @property
    def normalized_signal(self) -> str:
        """Señal/rating normalizado a su valor canónico en minúsculas.

        ✅ v0.6.9v (P1 del informe): antes la señal viajaba de TRES formas
        distintas — enum (`ImplicitSignal.ROUTED`), nombre serializado
        (`"ImplicitSignal.ROUTED"`, lo que producía `str(enum)` en `to_dict`) y
        valor (`"routed"`). Un consumidor que comparara contra `.value` no veía
        las señales persistidas, y al revés: al recargar de la DB,
        `is_positive`/`is_negative` comparaban contra miembros del enum y
        **fallaban en silencio** (dejaban de clasificar COPY_ACTION/RE_QUERY).
        Esta normalización hace que las tres formas se lean igual.
        """
        raw = self.signal_or_rating
        if isinstance(raw, (ImplicitSignal, ExplicitRating)):
            # ⚠️ `ExplicitRating` es un IntEnum: `raw.value` es int (4), así que
            # se fuerza str para que el contrato sea SIEMPRE texto.
            return str(raw.value)
        text = str(raw or "").strip()
        if "." in text:                      # "ImplicitSignal.ROUTED" → "routed"
            text = text.rsplit(".", 1)[-1]
        return text.lower()

    @property
    def is_positive(self) -> bool:
        """True si el feedback es positivo."""
        if self.feedback_type == FeedbackType.EXPLICIT:
            if isinstance(self.signal_or_rating, ExplicitRating):
                return self.signal_or_rating.is_positive
            return self.normalized_signal in ["up", "positive", "5", "4"]
        # Implícito: COPY_ACTION es positivo
        if self.normalized_signal == ImplicitSignal.COPY_ACTION.value:
            return True
        if self.normalized_signal in (ImplicitSignal.RE_QUERY.value,
                                      ImplicitSignal.EDIT_RESPONSE.value):
            return False
        return None  # Neutral/indeterminado

    @property
    def is_negative(self) -> bool:
        """True si el feedback es negativo."""
        if self.feedback_type == FeedbackType.EXPLICIT:
            if isinstance(self.signal_or_rating, ExplicitRating):
                return self.signal_or_rating.is_negative
            return self.normalized_signal in ["down", "negative", "1", "2"]
        # Implícito: RE_QUERY, EDIT_RESPONSE, IGNORED son negativos
        if self.normalized_signal in (ImplicitSignal.RE_QUERY.value,
                                      ImplicitSignal.EDIT_RESPONSE.value,
                                      ImplicitSignal.IGNORED.value):
            return True
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para persistencia/APIs."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "task_type": self.task_type,
            "agent_role": self.agent_role,
            "query": self.query,
            "response": self.response,
            "timestamp": self.timestamp.isoformat(),
            "feedback_type": self.feedback_type.value,
            # ✅ v0.6.9v: el VALOR canónico ("routed"), no `str(enum)`
            # ("ImplicitSignal.ROUTED") — ver `normalized_signal`.
            "signal_or_rating": self.normalized_signal,
            "is_positive": self.is_positive,
            "is_negative": self.is_negative,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        sign = "✅" if self.is_positive else "❌" if self.is_negative else "➖"
        return (
            f"FeedbackRecord({sign} {self.task_type} "
            f"session={self.session_id[:8]}... {self.feedback_type.value})"
        )


def _percentile(sorted_data: List[float], pct: float) -> float:
    """Percentil por interpolación lineal sobre una lista YA ordenada."""
    if not sorted_data:
        return 0.0
    if len(sorted_data) == 1:
        return sorted_data[0]
    k = (len(sorted_data) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


@dataclass
class QualityMetrics:
    """
    Métricas de calidad agregadas por tarea/agente.
    ✅ v0.8.2: Persistencia automática en SQLite (data/axioma.db)

    Attributes:
        task_type: Tipo de tarea
        agent_role: Rol del agente
        total_interactions: Total de interacciones registradas
        positive_count: Feedback positivo
        negative_count: Feedback negativo
        neutral_count: Feedback neutral/indeterminado
        avg_latency_ms: Latencia promedio en milisegundos
        p50_latency_ms: Percentil 50 de latencia
        p95_latency_ms: Percentil 95 de latencia
        p99_latency_ms: Percentil 99 de latencia
        success_rate: Tasa de éxito (positivos / totales)
        last_updated: Timestamp de última actualización
        db_path: Ruta a la base de datos SQLite para persistencia (inyectado)
    """
    task_type: str
    agent_role: str
    total_interactions: int = 0
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    success_rate: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    db_path: Optional[str] = field(default=None, repr=False)
    # ✅ FIX: ventana acotada de latencias recientes para calcular p50/p95/p99
    # reales — antes esos 3 campos se declaraban pero ningún método los
    # calculaba, quedaban en 0.0 para siempre pese a persistirse en SQLite.
    _latencies: "deque[float]" = field(
        default_factory=lambda: deque(maxlen=100), repr=False, compare=False
    )

    def update(self, record: FeedbackRecord, latency_ms: float) -> None:
        """Actualiza métricas con un nuevo registro y persiste en SQLite."""
        self.total_interactions += 1
        self.last_updated = datetime.now(timezone.utc)

        if record.is_positive:
            self.positive_count += 1
        elif record.is_negative:
            self.negative_count += 1
        else:
            self.neutral_count += 1

        # Actualizar latencias (simple moving average para demo)
        alpha = 0.1  # Factor de suavizado
        self.avg_latency_ms = alpha * latency_ms + (1 - alpha) * self.avg_latency_ms

        # ✅ FIX: p50/p95/p99 reales sobre la ventana acotada (antes: 0.0 fijo,
        # ningún método los tocaba pese a persistirse en SQLite)
        self._latencies.append(latency_ms)
        _sorted = sorted(self._latencies)
        self.p50_latency_ms = _percentile(_sorted, 50)
        self.p95_latency_ms = _percentile(_sorted, 95)
        self.p99_latency_ms = _percentile(_sorted, 99)

        # Actualizar success rate
        if self.total_interactions > 0:
            self.success_rate = round(self.positive_count / self.total_interactions, 4)

        # ✅ v0.8.2: Persistir estado agregado
        self._persist_metrics()

    def _persist_metrics(self) -> None:
        """✅ v0.8.2: Upsert a SQLite. O(1) en arranque al cargar desde aquí."""
        if not self.db_path:
            return

        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

            with _sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS quality_metrics (
                        task_type         TEXT NOT NULL,
                        agent_role        TEXT NOT NULL,
                        total_interactions INTEGER NOT NULL DEFAULT 0,
                        positive_count   INTEGER NOT NULL DEFAULT 0,
                        negative_count   INTEGER NOT NULL DEFAULT 0,
                        neutral_count    INTEGER NOT NULL DEFAULT 0,
                        avg_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p50_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p95_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p99_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        success_rate     REAL NOT NULL DEFAULT 0.0,
                        last_updated     DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (task_type, agent_role)
                    )
                """)
                conn.execute("""
                    INSERT OR REPLACE INTO quality_metrics
                    (task_type, agent_role, total_interactions, positive_count, negative_count,
                     neutral_count, avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms,
                     success_rate, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.task_type, self.agent_role, self.total_interactions, self.positive_count,
                    self.negative_count, self.neutral_count, self.avg_latency_ms, self.p50_latency_ms,
                    self.p95_latency_ms, self.p99_latency_ms, self.success_rate,
                    datetime.now(timezone.utc).isoformat()
                ))
                conn.commit()
        except Exception as e:
            logger.warning(f"[QualityMetrics] Error persistiendo en SQLite: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "task_type": self.task_type,
            "agent_role": self.agent_role,
            "total_interactions": self.total_interactions,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "neutral_count": self.neutral_count,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p50_latency_ms": round(self.p50_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "p99_latency_ms": round(self.p99_latency_ms, 2),
            "success_rate": self.success_rate,
            "last_updated": self.last_updated.isoformat(),
        }


# ============================================================================
# ✅ v0.8.0 (Fase 3): EWMA THRESHOLD OPTIMIZER + SQLite interaction_metrics
# ============================================================================

class EWMAThresholdOptimizer:
    """
    Ajusta umbrales de confianza dinámicamente por task_type usando EWMA.

    EWMA (Exponentially Weighted Moving Average) da más peso a interacciones
    recientes — permite adaptación rápida sin necesidad de re-entrenar modelos.

    Fórmula: threshold_new = α × outcome + (1 − α) × threshold_old
    donde outcome = 1.0 si éxito, 0.0 si fallo.

    Integra con SQLite para persistencia entre sesiones.
    Usa la misma DB que el resto del sistema (data/axioma.db).

    Uso:
        optimizer = EWMAThresholdOptimizer()
        optimizer.record_interaction("CODE_GENERATION", confidence=0.88, success=True)
        threshold = optimizer.get_threshold("CODE_GENERATION")
        # threshold ajustado según historial reciente
    """

    _DEFAULT_THRESHOLD = 0.92   # Umbral base del Master Plan
    _ALPHA = 0.15               # Factor de suavizado EWMA — 0.15 = responde en ~7 interacciones
    _MIN_THRESHOLD = 0.65       # Piso de seguridad — nunca bajar de esto
    _MAX_THRESHOLD = 0.98       # Techo — nunca subir de esto
    _MIN_SAMPLES = 5            # Mínimo de muestras antes de ajustar umbral

    def __init__(self, db_path: Optional[str] = None) -> None:
        # ✅ FIX: "data/axioma.db" era relativo al CWD del proceso — Paths ya
        # estaba importado en este archivo pero nunca se usaba. Paths.DATA
        # está resuelto de forma absoluta contra Paths.ROOT.
        self._db_path = db_path or str(Paths.DATA / "axioma.db")
        self._lock = _threading.RLock()
        # Cache en memoria: task_type → {"threshold": float, "samples": int, "ewma": float}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._init_db()
        self._load_from_db()
        logger.info(f"[EWMAThresholdOptimizer] Inicializado — db={self._db_path}")

    def _init_db(self) -> None:
        """Crea tablas SQLite si no existen."""
        import os
        os.makedirs(os.path.dirname(self._db_path) if os.path.dirname(self._db_path) else ".", exist_ok=True)
        try:
            with _sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS interaction_metrics (
                        session_id        TEXT NOT NULL,
                        task_type         TEXT NOT NULL,
                        confidence_input  REAL NOT NULL,
                        confidence_output REAL NOT NULL DEFAULT 0.0,
                        success           INTEGER NOT NULL DEFAULT 0,
                        intervention_req  INTEGER NOT NULL DEFAULT 0,
                        agent_role        TEXT NOT NULL DEFAULT 'unknown',
                        latency_ms        REAL NOT NULL DEFAULT 0.0,
                        num_sources       INTEGER NOT NULL DEFAULT 0,
                        signal            TEXT NOT NULL DEFAULT '',
                        feedback_type     TEXT NOT NULL DEFAULT '',
                        validation_passed INTEGER NOT NULL DEFAULT 0,
                        validation_score  REAL NOT NULL DEFAULT 0.0,
                        timestamp         DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (session_id, task_type, timestamp)
                    )
                """)
                # ✅ P1.1 (plan_fiabilidad, 2026-09-12): agent_role/latency_ms/
                # num_sources son columnas nuevas — el CREATE TABLE IF NOT
                # EXISTS de arriba no las agrega a una DB que ya tenía la
                # tabla vieja (sin ellas). ALTER TABLE ADD COLUMN una por una,
                # ignorando "duplicate column" si ya existen (idempotente).
                for _col_def in (
                    "agent_role TEXT NOT NULL DEFAULT 'unknown'",
                    "latency_ms REAL NOT NULL DEFAULT 0.0",
                    "num_sources INTEGER NOT NULL DEFAULT 0",
                    # ✅ ISSUE-068 (2026-09-14): señal (implícita/explícita) y
                    # estado de validación por turno — faltaban en la telemetría.
                    "signal TEXT NOT NULL DEFAULT ''",
                    "feedback_type TEXT NOT NULL DEFAULT ''",
                    "validation_passed INTEGER NOT NULL DEFAULT 0",
                    "validation_score REAL NOT NULL DEFAULT 0.0",
                ):
                    try:
                        conn.execute(
                            f"ALTER TABLE interaction_metrics ADD COLUMN {_col_def}"
                        )
                    except _sqlite3.OperationalError as _alter_err:
                        if "duplicate column" not in str(_alter_err).lower():
                            raise
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_pattern
                    ON interaction_metrics(task_type, success)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ewma_thresholds (
                        task_type   TEXT PRIMARY KEY,
                        threshold   REAL NOT NULL DEFAULT 0.92,
                        ewma_value  REAL NOT NULL DEFAULT 0.92,
                        samples     INTEGER NOT NULL DEFAULT 0,
                        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # ✅ v0.8.2: Crear tabla quality_metrics si no existe (doble safety)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS quality_metrics (
                        task_type         TEXT NOT NULL,
                        agent_role        TEXT NOT NULL,
                        total_interactions INTEGER NOT NULL DEFAULT 0,
                        positive_count   INTEGER NOT NULL DEFAULT 0,
                        negative_count   INTEGER NOT NULL DEFAULT 0,
                        neutral_count    INTEGER NOT NULL DEFAULT 0,
                        avg_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p50_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p95_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        p99_latency_ms   REAL NOT NULL DEFAULT 0.0,
                        success_rate     REAL NOT NULL DEFAULT 0.0,
                        last_updated     DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (task_type, agent_role)
                    )
                """)
                conn.commit()
            logger.debug("[EWMAThresholdOptimizer] Tablas SQLite inicializadas")
        except Exception as e:
            logger.error(f"[EWMAThresholdOptimizer] Error inicializando DB: {e}")

    def _load_from_db(self) -> None:
        """Carga umbrales actuales desde SQLite al cache en memoria."""
        try:
            with _sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT task_type, threshold, ewma_value, samples FROM ewma_thresholds"
                ).fetchall()
                for task_type, threshold, ewma_value, samples in rows:
                    self._cache[task_type] = {
                        "threshold": threshold,
                        "ewma": ewma_value,
                        "samples": samples,
                    }
        except Exception as e:
            logger.warning(f"[EWMAThresholdOptimizer] No se pudo cargar DB: {e}")

    def record_interaction(
        self,
        task_type: str,
        confidence_input: float,
        success: bool,
        session_id: str = "global",
        confidence_output: float = 0.0,
        intervention_required: bool = False,
        agent_role: str = "unknown",
        latency_ms: float = 0.0,
        num_sources: int = 0,
        signal: str = "",
        feedback_type: str = "",
        validation_passed: bool = False,
        validation_score: float = 0.0,
    ) -> float:
        """
        Registra una interacción y actualiza el umbral EWMA.

        Args:
            task_type          : Tipo de tarea (ej: "CODE_GENERATION")
            confidence_input   : Confianza del clasificador al inicio
            success            : True si la interacción fue exitosa
            session_id         : ID de sesión
            confidence_output  : Confianza de la respuesta generada
            intervention_required: True si requirió intervención humana
            agent_role         : Rol del agente que respondió (P1.1)
            latency_ms         : Latencia real de la respuesta en ms (P1.1)
            num_sources        : Nº de fuentes citadas en la respuesta (P1.1)
            signal             : Señal concreta normalizada ("routed", "4", …) (ISSUE-068)
            feedback_type      : "implicit" / "explicit" (ISSUE-068)
            validation_passed  : True si el validador aprobó (ISSUE-068)
            validation_score   : Score del validador (0.0 si no validó) (ISSUE-068)

        Returns:
            Nuevo umbral calculado para el task_type.
        """
        with self._lock:
            # Actualizar cache EWMA
            if task_type not in self._cache:
                self._cache[task_type] = {
                    "threshold": self._DEFAULT_THRESHOLD,
                    "ewma": self._DEFAULT_THRESHOLD,
                    "samples": 0,
                }

            entry = self._cache[task_type]
            entry["samples"] += 1

            # EWMA sobre outcome (1=éxito, 0=fallo)
            outcome = 1.0 if success else 0.0
            entry["ewma"] = self._ALPHA * outcome + (1 - self._ALPHA) * entry["ewma"]

            # Ajustar threshold solo si hay suficientes muestras
            if entry["samples"] >= self._MIN_SAMPLES:
                # Threshold inversamente proporcional al EWMA:
                # alta tasa de éxito → podemos bajar threshold (más permisivo)
                # baja tasa de éxito → subir threshold (más estricto)
                raw = self._DEFAULT_THRESHOLD + (0.5 - entry["ewma"]) * 0.2
                entry["threshold"] = max(self._MIN_THRESHOLD, min(self._MAX_THRESHOLD, raw))

            new_threshold = entry["threshold"]

            # Persistir en SQLite
            self._persist_interaction(
                session_id, task_type, confidence_input,
                confidence_output, success, intervention_required, entry,
                agent_role=agent_role, latency_ms=latency_ms, num_sources=num_sources,
                signal=signal, feedback_type=feedback_type,
                validation_passed=validation_passed, validation_score=validation_score,
            )

            logger.debug(
                f"[EWMA] {task_type}: ewma={entry['ewma']:.3f} "
                f"threshold={new_threshold:.3f} samples={entry['samples']}"
            )
            return new_threshold

    def _persist_interaction(
        self,
        session_id: str,
        task_type: str,
        confidence_input: float,
        confidence_output: float,
        success: bool,
        intervention_req: bool,
        entry: Dict[str, Any],
        agent_role: str = "unknown",
        latency_ms: float = 0.0,
        num_sources: int = 0,
        signal: str = "",
        feedback_type: str = "",
        validation_passed: bool = False,
        validation_score: float = 0.0,
    ) -> None:
        """Persiste interacción y umbral actualizado en SQLite."""
        try:
            with _sqlite3.connect(self._db_path) as conn:
                import datetime as _dt
                ts_micro = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                conn.execute(
                    """INSERT INTO interaction_metrics
                       (session_id, task_type, confidence_input, confidence_output,
                        success, intervention_req, agent_role, latency_ms, num_sources,
                        signal, feedback_type, validation_passed, validation_score,
                        timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, task_type, confidence_input, confidence_output,
                     int(success), int(intervention_req), str(agent_role or "unknown"),
                     float(latency_ms or 0.0), int(num_sources or 0),
                     str(signal or ""), str(feedback_type or ""),
                     int(validation_passed), float(validation_score or 0.0), ts_micro)
                )
                conn.execute(
                    """INSERT OR REPLACE INTO ewma_thresholds
                       (task_type, threshold, ewma_value, samples, last_updated)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (task_type, entry["threshold"], entry["ewma"], entry["samples"])
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[EWMA] Error persistiendo en SQLite: {e}")

    def get_threshold(self, task_type: str) -> float:
        """
        Retorna el umbral de confianza ajustado para el task_type dado.
        Si no hay historial, retorna el default (0.92).
        """
        with self._lock:
            entry = self._cache.get(task_type)
            if entry and entry["samples"] >= self._MIN_SAMPLES:
                return entry["threshold"]
            return self._DEFAULT_THRESHOLD

    def get_stats(self) -> Dict[str, Any]:
        """Estadísticas de todos los task_types trackeados."""
        with self._lock:
            return {
                task_type: {
                    "threshold": e["threshold"],
                    "ewma": round(e["ewma"], 4),
                    "samples": e["samples"],
                }
                for task_type, e in self._cache.items()
            }


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE COMPONENTES EN ESTE ARCHIVO
# ═══════════════════════════════════════════════════════════════
# | Componente                  | Propósito                        |
# |-----------------------------|----------------------------------|
# | FeedbackType                | Enum: IMPLICIT/EXPLICIT          |
# | ImplicitSignal              | Enum: re_query, edit, copy, etc. |
# | ExplicitRating              | Enum: 1-5 estrellas              |
# | FeedbackRecord              | Registro estructurado            |
# | QualityMetrics              | Métricas por tarea/agente ✅ v0.8.2|
# | EWMAThresholdOptimizer      | Optimizador de umbrales EWMA     |
# | _log_feedback / _log_error  | Helpers de logging               |
# ═══════════════════════════════════════════════════════════════
# 📋 COMPONENTES EN feedback_loop_detectors_handlers.py
# ═══════════════════════════════════════════════════════════════
# | ImplicitFeedbackDetector    | Detecta señales implícitas       |
# | ExplicitFeedbackHandler     | Procesa feedback explícito       |
# | ParameterOptimizer          | Ajusta parámetros dinámicamente  |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════
