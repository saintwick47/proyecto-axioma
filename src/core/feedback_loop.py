#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# feedback_loop.py - Sistema de Retroalimentación y Auto-Optimización
# Ruta: /ruta/a/axioma/src/core/feedback_loop.py
# Autor: SaintWick
# Versión: 0.9.0
#
# Sistema de aprendizaje continuo que registra feedback implícito y explícito,
# ajusta parámetros del sistema y cierra el loop de aprendizaje integrando
# ejemplos positivos al clasificador de intenciones. Orquesta detectores,
# manejadores y optimizadores para mejorar la calidad de respuestas.
# ═══════════════════════════════════════════════════════════════
import asyncio
import logging
import threading
import time
import json
import hashlib
import sqlite3 as _sqlite3
from typing import Optional, Dict, Any, List, Callable, Awaitable, Union
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import OrderedDict, defaultdict

from config.settings import settings
from config.paths import Paths
from src.domain.types import TaskType, AgentRole

# Importar componentes base (enums, dataclasses, EWMA, logging helpers)
from .feedback_loop_components import (
    FeedbackType,
    ImplicitSignal,
    ExplicitRating,
    FeedbackRecord,
    QualityMetrics,
    EWMAThresholdOptimizer,
    _log_feedback,
    _log_error,
)

# Importar detectores y handlers (movidos en v0.8.1)
from .feedback_loop_detectors_handlers import (
    ImplicitFeedbackDetector,
    ExplicitFeedbackHandler,
    ParameterOptimizer,
)
from src.utils.degradation import log_degraded

# 🆕 FASE 6: Auto-Healing Monitor — import seguro
try:
    from src.core.self_healing import get_monitor as _get_healing_monitor
    _HEALING_AVAILABLE = True
except ImportError:
    _HEALING_AVAILABLE = False
    _get_healing_monitor = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ============================================================================
# CLASE PRINCIPAL: FeedbackLoop
# ============================================================================
class FeedbackLoop:
    """
    Sistema central de retroalimentación y auto-optimización.

    Orquesta:
    - ImplicitFeedbackDetector: Detecta señales implícitas
    - ExplicitFeedbackHandler: Procesa feedback explícito
    - ParameterOptimizer: Ajusta parámetros dinámicamente
    - QualityMetrics: Trackea métricas por tarea/agente

    Integración con otros componentes:
    - MemoryGateway: Para almacenar feedback persistente
    - EmbeddingOptimizer: Para mejorar búsqueda semántica
    - SmartRouter: Para ajustar decisiones de ruteo

    Example:
    >>> loop = FeedbackLoop(session_id="abc123")
    >>> await loop.record_implicit(session_id, task_type, query, response, signal)
    >>> await loop.record_explicit(session_id, task_type, query, response, rating=4)
    >>> optimized_temp = await loop.get_optimized_parameter("temperature", task_type)
    >>> metrics = await loop.get_quality_metrics(task_type, agent_role)
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        enable_implicit_detection: bool = True,
        enable_explicit_handling: bool = True,
        enable_parameter_optimization: bool = True,
        persist_path: Optional[Path] = None,
        db_path: Optional[str] = None,
        param_persist_path: Optional[Path] = None,
    ):
        """
        Inicializa bucle de feedback.

        Args:
            session_id: ID de sesión (genera UUID si None)
            enable_implicit_detection: Habilitar detección implícita
            enable_explicit_handling: Habilitar manejo explícito
            enable_parameter_optimization: Habilitar optimización de parámetros
            persist_path: Ruta para persistencia de feedback (None = memoria)
            db_path: Ruta de la SQLite de métricas. **Los tests deben pasarla**
                (p. ej. `tmp_path/"axioma.db"`): por defecto apunta a
                `data/axioma.db` del usuario y la telemetría la ensuciaría
                (misma lección que ISSUE-059 con `data/feedback/`).
            param_persist_path: Ruta JSON para persistir el estado aprendido del
                `ParameterOptimizer` (A1/ISSUE-075). `None` = en memoria (los
                tests NO persisten y no ensucian `data/`). El singleton de
                producción (`get_feedback_loop`) lo fija a
                `data/feedback/param_optimizer.json`.
        """
        start = time.perf_counter()

        self.session_id = session_id or hashlib.sha256(
            f"{time.time()}".encode()
        ).hexdigest()[:16]

        self.enable_implicit = enable_implicit_detection
        self.enable_explicit = enable_explicit_handling
        self.enable_optimization = enable_parameter_optimization
        self.persist_path = persist_path or (Paths.DATA / "feedback" / f"{self.session_id}.jsonl")

        # Componentes
        self.implicit_detector = ImplicitFeedbackDetector() if enable_implicit_detection else None
        self.explicit_handler = ExplicitFeedbackHandler() if enable_explicit_handling else None
        # ✅ A1 (ISSUE-075): persistir el estado aprendido del optimizador de
        # parámetros. `None` = en memoria (los tests no escriben en `data/`);
        # el singleton de producción (`get_feedback_loop`) pasa el path real.
        self.param_optimizer = (
            ParameterOptimizer(
                persist_path=str(param_persist_path) if param_persist_path else None
            )
            if enable_parameter_optimization else None
        )
        # ⚠️ `_db_path` se resuelve ANTES de crear el optimizer (que lo necesita).
        # ✅ ISSUE-067: inyectable para que los tests no escriban en los datos del
        # usuario (la telemetría EWMA crea `interaction_metrics`).
        self._db_path = str(db_path or (Paths.DATA / "axioma.db"))
        # ✅ ISSUE-067: EWMAThresholdOptimizer estaba definido, importado y **nunca
        # instanciado** (código muerto): por eso la tabla `interaction_metrics` no
        # existía en `data/axioma.db`. Se instancia acá para tener telemetría por
        # tarea persistida y umbrales adaptativos disponibles.
        self._threshold_optimizer = None
        try:
            self._threshold_optimizer = EWMAThresholdOptimizer(db_path=self._db_path)
        except Exception as _ewma_err:  # noqa: BLE001 — sin telemetría se sigue
            log_degraded(logger, _ewma_err,
                         "FeedbackLoop: EWMAThresholdOptimizer no disponible")

        # Almacenamiento de registros
        self._records: OrderedDict[str, FeedbackRecord] = OrderedDict()
        self._metrics: Dict[str, QualityMetrics] = {}  # key: "{task_type}:{agent_role}"
        self._lock = asyncio.Lock()

        # ✅ FIX FASE 4: Límite máximo para evitar fuga de memoria (DoS)
        self._max_records = 1000

        # Métricas globales
        self._total_records = 0
        self._total_positive = 0
        self._total_negative = 0

        # ✅ v0.8.2: DB Path unificada y carga O(1) de métricas
        # ✅ FIX: "data/axioma.db" era relativo al CWD del proceso, no al
        # root del proyecto — bajo systemd (o cualquier CWD distinto) podía
        # escribir/leer en un lugar equivocado. Paths.DATA ya está resuelto
        # de forma absoluta contra Paths.ROOT (ver config/paths.py).
        self._load_metrics_from_db()

        # Cargar persistencia si existe (JSONL como log crudo)
        if self.persist_path and self.persist_path.exists():
            self._load_persistence()

        duration = time.perf_counter() - start
        logger.info(f"FeedbackLoop initialized: session={self.session_id}, persist={bool(self.persist_path)}")
        _log_feedback("init", session_id=self.session_id, success=True, extra={"duration": round(duration, 4)})

    def _get_metrics_key(self, task_type: str, agent_role: str) -> str:
        """Genera key única para métricas."""
        return f"{task_type}:{agent_role}"

    async def get_signal_breakdown(self) -> Dict[str, Any]:
        """Desglose de señales implícitas por señal, tarea y agente.

        ✅ v0.6.9v (P1 del informe): "explotar el feedback ROUTED". La señal se
        registraba desde ISSUE-033 pero **nadie la leía**, así que no había
        forma de medir ruteo/handlers. Esto la hace consultable desde
        `GET /api/v1/feedback/stats`, sin agregar estado nuevo: se deriva de los
        registros que ya se guardan.
        """
        async with self._lock:
            records = list(self._records.values())

        por_senal: Dict[str, int] = {}
        por_tarea: Dict[str, int] = {}
        por_agente: Dict[str, int] = {}
        routed_por_tarea: Dict[str, int] = {}
        implicit = [r for r in records
                    if getattr(r, "feedback_type", None) == FeedbackType.IMPLICIT]
        positivos = 0
        for r in implicit:
            nombre = getattr(r, "normalized_signal", None) or \
                str(getattr(r, "signal_or_rating", ""))
            por_senal[nombre] = por_senal.get(nombre, 0) + 1
            tarea = getattr(r, "task_type", "") or "desconocida"
            agente = getattr(r, "agent_role", "") or "desconocido"
            por_tarea[tarea] = por_tarea.get(tarea, 0) + 1
            por_agente[agente] = por_agente.get(agente, 0) + 1
            if nombre == "routed":
                routed_por_tarea[tarea] = routed_por_tarea.get(tarea, 0) + 1
            if getattr(r, "is_positive", False):
                positivos += 1
        total = len(implicit)
        return {
            "total_implicitos": total,
            "por_senal": por_senal,
            "routed": por_senal.get("routed", 0),
            "routed_por_tarea": routed_por_tarea,
            "por_tarea": por_tarea,
            "por_agente": por_agente,
            "tasa_exito_implicita": round(positivos / max(total, 1), 4),
            "registros_totales": len(records),
        }

    def _ensure_metrics(self, task_type: str, agent_role: str) -> QualityMetrics:
        """
        Asegura que existen métricas para tarea/agente.
        ✅ v0.8.2: Inyecta db_path para auto-persistencia en SQLite.
        """
        key = self._get_metrics_key(task_type, agent_role)
        if key not in self._metrics:
            self._metrics[key] = QualityMetrics(
                task_type=task_type,
                agent_role=agent_role,
                db_path=self._db_path
            )
        return self._metrics[key]

    def _load_metrics_from_db(self) -> None:
        """
        Carga métricas agregadas desde SQLite (axioma.db) en vez de recalcular desde JSONL.
        Esto convierte el arranque de O(N) a O(1), resolviendo el gap de rendimiento.
        """
        import os
        os.makedirs(os.path.dirname(self._db_path) if os.path.dirname(self._db_path) else ".", exist_ok=True)

        try:
            with _sqlite3.connect(self._db_path) as conn:
                # Asegurar que la tabla existe
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

                rows = conn.execute(
                    "SELECT task_type, agent_role, total_interactions, positive_count, negative_count, "
                    "neutral_count, avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms, "
                    "success_rate, last_updated FROM quality_metrics"
                ).fetchall()

                for row in rows:
                    (task_type, agent_role, total, pos, neg, neu, avg_lat, p50, p95, p99, s_rate, last_upd) = row
                    key = self._get_metrics_key(task_type, agent_role)
                    self._metrics[key] = QualityMetrics(
                        task_type=task_type,
                        agent_role=agent_role,
                        total_interactions=total,
                        positive_count=pos,
                        negative_count=neg,
                        neutral_count=neu,
                        avg_latency_ms=avg_lat,
                        p50_latency_ms=p50,
                        p95_latency_ms=p95,
                        p99_latency_ms=p99,
                        success_rate=s_rate,
                        last_updated=datetime.fromisoformat(last_upd) if isinstance(last_upd, str) else datetime.now(timezone.utc),
                        db_path=self._db_path
                    )

            # Actualizar globales desde la fuente de verdad (SQLite)
            self._total_records = sum(m.total_interactions for m in self._metrics.values())
            self._total_positive = sum(m.positive_count for m in self._metrics.values())
            self._total_negative = sum(m.negative_count for m in self._metrics.values())

            logger.info(f"Loaded {len(self._metrics)} metrics from SQLite ({self._db_path})")
        except Exception as e:
            _log_error(e, "FeedbackLoop._load_metrics_from_db", extra={"db_path": self._db_path})
            logger.warning(f"Failed to load metrics from DB: {e}")

    def _load_persistence(self) -> None:
        """
        Carga registros persistentes desde archivo JSONL.
        ✅ v0.8.2: Ya NO recalcula métricas aquí para evitar O(N) en arranque.
                    Las métricas se cargan directamente desde SQLite.
        ✅ v0.8.3 (FASE 4): Solo carga los últimos N registros a RAM para evitar
                    O(N) en memoria si el JSONL crece indefinidamente.
        """
        if not self.persist_path or not self.persist_path.exists():
            return

        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                # ✅ FIX FASE 4: Leer solo las últimas N líneas
                lines = f.readlines()
                recent_lines = lines[-self._max_records:]

                for line in recent_lines:
                    if line.strip():
                        data = json.loads(line)
                        # Reconstruir record (simplificado)
                        record = FeedbackRecord(
                            id=data["id"],
                            session_id=data["session_id"],
                            task_type=data["task_type"],
                            agent_role=data["agent_role"],
                            query=data["query"],
                            response=data["response"],
                            timestamp=datetime.fromisoformat(data["timestamp"]),
                            feedback_type=FeedbackType(data["feedback_type"]),
                            signal_or_rating=data["signal_or_rating"],
                            metadata=data.get("metadata", {}),
                        )
                        self._records[record.id] = record
                        # ✅ v0.8.2: Se elimina self._update_metrics(record)
                        # Las métricas ya se cargaron desde SQLite y no deben duplicarse.

            logger.info(f"Loaded {len(self._records)} feedback records from {self.persist_path}")
        except Exception as e:
            _log_error(e, "FeedbackLoop._load_persistence", extra={"path": str(self.persist_path)})
            logger.warning(f"Failed to load persistence: {e}")

    def _persist_record(self, record: FeedbackRecord) -> None:
        """Persiste un registro a archivo JSONL (log crudo)."""
        if not self.persist_path:
            return

        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.persist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            _log_error(e, "FeedbackLoop._persist_record", extra={"path": str(self.persist_path)})

    def _feed_threshold_optimizer(self, record: FeedbackRecord,
                                  confidence: float = 0.0,
                                  latency_ms: float = 0.0) -> None:
        """Alimenta el EWMA por tarea (telemetría + umbral adaptativo).

        ✅ ISSUE-067: `success` se define con la señal del registro y **las
        señales neutras** (p. ej. `ROUTED`, que solo confirma que hubo ruteo) se
        registran como éxito=True con la confianza dada: no son un fallo.

        ✅ P1.1 (plan_fiabilidad, 2026-09-12): agent_role/latency_ms/num_sources
        se propagan a `interaction_metrics` — antes la tabla tenía las columnas
        base nomás y `latency_ms` nunca viajaba desde `record_implicit()`.
        `num_sources` sale de `record.metadata["num_sources"]`, que
        `dispatcher_process.py` completa con `len(response.sources)` (a su vez
        arreglado en `AgentResult.to_response()`, que antes lo dejaba siempre
        en `[]`).
        """
        if self._threshold_optimizer is None:
            return
        try:
            es_positivo = record.is_positive
            es_negativo = record.is_negative
            if es_negativo:
                exito = False
            elif es_positivo:
                exito = True
            else:
                exito = True    # neutro (ROUTED, etc.): no penaliza calidad
            try:
                num_sources = int((record.metadata or {}).get("num_sources", 0) or 0)
            except (TypeError, ValueError):
                num_sources = 0
            # ✅ ISSUE-068 (2026-09-14): señal + estado de validación por turno.
            # La señal sale del propio registro (normalized_signal) y el tipo
            # (implicit/explicit) del feedback_type; la validación viaja en la
            # metadata que completa dispatcher_process.py (validation_score /
            # validation_passed). Nunca deben romper la telemetría base.
            _meta = record.metadata or {}
            try:
                signal = str(getattr(record, "normalized_signal", "") or "")
            except Exception as e:
                log_degraded(logger, e, "src/core/feedback_loop.py:_feed_threshold_optimizer")
                signal = ""
            try:
                feedback_type = str(getattr(getattr(record, "feedback_type", None), "value", "") or "")
            except Exception as e:
                log_degraded(logger, e, "src/core/feedback_loop.py:_feed_threshold_optimizer")
                feedback_type = ""
            try:
                validation_score = float(_meta.get("validation_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                validation_score = 0.0
            try:
                validation_passed = bool(_meta.get("validation_passed", False))
            except Exception as e:
                log_degraded(logger, e, "src/core/feedback_loop.py:_feed_threshold_optimizer")
                validation_passed = False
            self._threshold_optimizer.record_interaction(
                task_type=str(getattr(record, "task_type", "") or "unknown"),
                confidence_input=float(confidence or 0.0),
                success=exito,
                session_id=str(getattr(record, "session_id", "") or "global"),
                confidence_output=float(confidence or 0.0),
                agent_role=str(getattr(record, "agent_role", "") or "unknown"),
                latency_ms=float(latency_ms or 0.0),
                num_sources=num_sources,
                signal=signal,
                feedback_type=feedback_type,
                validation_passed=validation_passed,
                validation_score=validation_score,
            )
        except Exception as _d2e:  # noqa: BLE001 — la telemetría nunca rompe
            log_degraded(logger, _d2e, "FeedbackLoop._feed_threshold_optimizer")

    def _update_metrics(self, record: FeedbackRecord, latency_ms: float = 0.0) -> None:
        """
        Actualiza métricas con un nuevo registro en tiempo real.
        ✅ v0.8.2: QualityMetrics.update() ahora persiste cambios en SQLite internamente.
        """
        metrics = self._ensure_metrics(record.task_type, record.agent_role)
        metrics.update(record, latency_ms)

        # ✅ ISSUE-067: telemetría EWMA por tarea (tabla `interaction_metrics`).
        # La confianza sale de la metadata del registro cuando existe (el feedback
        # explícito del panel la guarda desde ISSUE-052).
        try:
            _conf = float((record.metadata or {}).get("confidence") or 0.0)
        except (TypeError, ValueError):
            _conf = 0.0
        self._feed_threshold_optimizer(record, confidence=_conf, latency_ms=latency_ms)

        # Actualizar globales
        self._total_records += 1
        if record.is_positive:
            self._total_positive += 1
        elif record.is_negative:
            self._total_negative += 1

    # ── API PÚBLICA: Registro de Feedback ─────────────────────────────────

    async def record_implicit(
        self,
        session_id: str,
        task_type: str,
        agent_role: str,
        query: str,
        response: str,
        signal: ImplicitSignal,
        latency_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedbackRecord:
        """
        Registra feedback implícito detectado.

        Args:
            session_id: ID de sesión
            task_type: Tipo de tarea
            agent_role: Rol del agente
            query: Consulta original
            response: Respuesta generada
            signal: Señal implícita detectada
            latency_ms: Latencia de la respuesta
            metadata: Metadatos adicionales

        Returns:
            FeedbackRecord creado
        """
        if not self.enable_implicit or not self.implicit_detector:
            return None

        import uuid

        now = datetime.now(timezone.utc)
        record = FeedbackRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            task_type=task_type,
            agent_role=agent_role,
            query=query,
            response=response,
            timestamp=now,
            feedback_type=FeedbackType.IMPLICIT,
            signal_or_rating=signal,
            metadata=metadata or {},
        )

        # ✅ FIX FASE 4: Desacoplar I/O y locks bloqueantes del async lock principal
        notify_healing = False

        async with self._lock:
            self._records[record.id] = record
            self._update_metrics(record, latency_ms)

            # ✅ FIX FASE 4: Evicción segura para evitar fuga de memoria
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

            if self.enable_optimization and self.param_optimizer:
                self.param_optimizer.record_interaction(task_type, record.is_positive)

            if _HEALING_AVAILABLE and self._ensure_metrics(task_type, agent_role).total_interactions >= 10:
                notify_healing = True

        # Fuera del lock: I/O y bloqueos de threading (evita congelar el event loop)
        self._persist_record(record)

        if notify_healing:
            try:
                _get_healing_monitor().check_handler(
                    task_type,
                    latency_ms,
                    bool(record.is_positive),
                )
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/feedback_loop.py:record_implicit")

        _log_feedback(
            "record",
            session_id=session_id,
            task_type=task_type,
            feedback_type=FeedbackType.IMPLICIT.value,
            success=True,
            extra={"signal": signal.value, "is_positive": record.is_positive},
        )

        logger.debug(f"Implicit feedback recorded: {record}")
        return record

    async def record_explicit(
        self,
        session_id: str,
        task_type: str,
        agent_role: str,
        query: str,
        response: str,
        rating: Union[int, ExplicitRating],
        comment: Optional[str] = None,
        tags: Optional[List[str]] = None,
        latency_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> FeedbackRecord:
        """
        Registra feedback explícito proporcionado por usuario.

        Args:
            session_id: ID de sesión
            task_type: Tipo de tarea
            agent_role: Rol del agente
            query: Consulta original
            response: Respuesta generada
            rating: Calificación (1-5 o ExplicitRating)
            comment: Comentario opcional
            tags: Tags de categoría opcionales
            latency_ms: Latencia de la respuesta
            metadata: Metadatos adicionales

        Returns:
            FeedbackRecord creado
        """
        if not self.enable_explicit or not self.explicit_handler:
            return None

        record = await self.explicit_handler.record_rating(
            session_id=session_id,
            task_type=task_type,
            agent_role=agent_role,
            query=query,
            response=response,
            rating=rating,
            comment=comment,
            tags=tags,
            metadata=metadata,
        )

        # ✅ FIX FASE 4: Desacoplar I/O y locks bloqueantes del async lock principal
        notify_healing = False

        async with self._lock:
            self._records[record.id] = record
            self._update_metrics(record, latency_ms)

            # ✅ FIX FASE 4: Evicción segura para evitar fuga de memoria
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)

            if self.enable_optimization and self.param_optimizer:
                self.param_optimizer.record_interaction(task_type, record.is_positive)

            if _HEALING_AVAILABLE and self._ensure_metrics(task_type, agent_role).total_interactions >= 10:
                notify_healing = True

        # Fuera del lock: I/O y bloqueos de threading (evita congelar el event loop)
        self._persist_record(record)

        if notify_healing:
            try:
                _get_healing_monitor().check_handler(
                    task_type,
                    latency_ms,
                    bool(record.is_positive),
                )
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/core/feedback_loop.py:record_explicit")

        logger.debug(f"Explicit feedback recorded: {record}")
        return record

    # ── API PÚBLICA: Optimización de Parámetros ─────────────────────────

    async def get_optimized_parameter(
        self,
        param: str,
        task_type: str,
        current_value: float,
        min_value: float,
        max_value: float,
    ) -> float:
        """
        Obtiene valor optimizado de un parámetro.

        Args:
            param: Nombre del parámetro
            task_type: Tipo de tarea
            current_value: Valor actual
            min_value: Mínimo permitido
            max_value: Máximo permitido

        Returns:
            Valor optimizado (o current_value si no hay optimización)
        """
        if not self.enable_optimization or not self.param_optimizer:
            return current_value

        return await self.param_optimizer.adjust_parameter(
            param=param,
            task_type=task_type,
            current_value=current_value,
            min_value=min_value,
            max_value=max_value,
        )

    def get_current_parameter(self, param: str, task_type: Optional[str] = None) -> Optional[float]:
        """Obtiene valor actual de un parámetro optimizado (None si no hay optimización)."""
        if not self.enable_optimization or not self.param_optimizer:
            return None
        return self.param_optimizer.get_current_value(param, task_type)

    # ── API PÚBLICA: Métricas y Estadísticas ────────────────────────────

    # ✅ FIX FASE 4: Async y copia segura para evitar race conditions
    async def get_quality_metrics(
        self,
        task_type: str,
        agent_role: str,
    ) -> Optional[QualityMetrics]:
        """
        Obtiene métricas de calidad para tarea/agente.

        Args:
            task_type: Tipo de tarea
            agent_role: Rol del agente

        Returns:
            QualityMetrics o None si no hay datos
        """
        async with self._lock:
            key = self._get_metrics_key(task_type, agent_role)
            return self._metrics.get(key)

    # ✅ FIX FASE 4: Async y copia segura para evitar race conditions
    async def get_global_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas globales del feedback loop.

        Returns:
            Dict con estadísticas agregadas
        """
        async with self._lock:
            total = self._total_records
            stats = {
                "session_id": self.session_id,
                "total_records": total,
                "positive_count": self._total_positive,
                "negative_count": self._total_negative,
                "neutral_count": total - self._total_positive - self._total_negative,
                # ✅ ISSUE-067: el ratio se calcula sobre las señales **decididas**
                # (positivas + negativas). Antes dividía por el total, así que las
                # señales NEUTRAS — p. ej. `ROUTED`, que solo confirma que hubo
                # ruteo — diluían la métrica: con 10 positivas, 1 negativa y 71
                # neutras daba 0.12, y cualquier umbral de calidad (0.75 en los
                # triggers proactivos) quedaba inalcanzable.
                "global_success_rate": round(
                    self._total_positive
                    / max(1, self._total_positive + self._total_negative), 4),
                "success_rate_basis": "decididas (positivas+negativas)",
                "decided_count": self._total_positive + self._total_negative,
                "tasks_tracked": len(self._metrics),
                "optimization_enabled": self.enable_optimization,
                "param_optimizer": self.param_optimizer.get_stats() if self.param_optimizer else None,
                "implicit_detector": {
                    "enabled": self.enable_implicit,
                    "recent_queries": sum(len(v) for v in self.implicit_detector._recent_queries.values()) if self.implicit_detector else 0,
                } if self.implicit_detector else None,
            }
        return stats

    # ✅ FIX FASE 4: Async y copia segura para evitar race conditions
    async def get_recent_records(self, limit: int = 50, feedback_type: Optional[FeedbackType] = None) -> List[FeedbackRecord]:
        """
        Obtiene registros recientes de feedback.

        Args:
            limit: Máximo de registros a retornar
            feedback_type: Filtrar por tipo de feedback (None = todos)

        Returns:
            Lista de FeedbackRecord ordenados por timestamp (más reciente primero)
        """
        async with self._lock:
            records = list(self._records.values())

        if feedback_type:
            records = [r for r in records if r.feedback_type == feedback_type]

        # Ordenar por timestamp descendente
        records.sort(key=lambda r: r.timestamp, reverse=True)

        return records[:limit]

    # ── API PÚBLICA: Limpieza y Mantenimiento ───────────────────────────

    # ── ✅ v0.9.0: Cierre del loop de aprendizaje ────────────────────────────

    async def consume_feedback_for_classifier(
        self,
        classifier: Any,
        batch_size: int = 50,
        min_confidence: float = 0.6,
    ) -> Dict[str, Any]:
        """
        Cierra el loop de aprendizaje: drena registros positivos acumulados
        y los empuja al IntentClassifierCore vía add_example().

        Diseño:
        - Solo procesa registros is_positive=True con confidence >= min_confidence
        - Procesa en batch de batch_size para no bloquear el event loop
        - La última escritura al clasificador (persist=True) se hace una sola vez
          al final del batch, no en cada ejemplo (evita N escrituras a disco)
        - Nunca propaga excepciones — si el clasificador falla, el loop continúa
        - Registra estadísticas del proceso en _stats

        Args:
            classifier: Instancia de IntentClassifierCore con add_example()
            batch_size: Máximo de registros a procesar en esta llamada
            min_confidence: Umbral mínimo de confidence del registro para usarlo

        Returns:
            Dict con total_processed, total_added, skipped, errors
        """
        if not hasattr(classifier, "add_example"):
            logger.warning("consume_feedback_for_classifier: classifier sin add_example()")
            return {"total_processed": 0, "total_added": 0, "skipped": 0, "errors": 0}

        stats = {"total_processed": 0, "total_added": 0, "skipped": 0, "errors": 0}

        # Snapshot seguro de registros positivos bajo lock
        async with self._lock:
            candidates = [
                r for r in self._records.values()
                if r.is_positive
                and getattr(r, "confidence", 1.0) >= min_confidence
                and r.task_type
                and r.query
            ]

        # Limitar al batch_size más recientes por timestamp
        candidates = sorted(candidates, key=lambda r: r.timestamp, reverse=True)[:batch_size]

        if not candidates:
            return stats

        # Procesar en thread para no bloquear el event loop (add_example puede
        # llamar _get_embedding que es CPU-bound)
        def _process_batch() -> Dict[str, Any]:
            _stats = {"total_processed": 0, "total_added": 0, "skipped": 0, "errors": 0}
            for i, record in enumerate(candidates):
                _stats["total_processed"] += 1
                try:
                    # persist=False en todos excepto el último para una sola escritura
                    is_last = (i == len(candidates) - 1)
                    added = classifier.add_example(
                        task_type=record.task_type,
                        text=record.query,
                        persist=is_last,
                    )
                    if added:
                        _stats["total_added"] += 1
                    else:
                        _stats["skipped"] += 1
                except Exception as e:
                    _stats["errors"] += 1
                    log_degraded(logger, e, "src/core/feedback_loop.py:_process_batch")
            return _stats

        try:
            stats = await asyncio.get_running_loop().run_in_executor(None, _process_batch)
        except Exception as e:
            logger.warning(f"consume_feedback_for_classifier batch failed (non-fatal): {e}")
            stats["errors"] += 1

        _log_feedback(
            "consume_feedback",
            session_id=self.session_id,
            success=stats["errors"] == 0,
            extra=stats,
        )
        logger.info(
            f"consume_feedback_for_classifier: processed={stats['total_processed']}, "
            f"added={stats['total_added']}, skipped={stats['skipped']}, errors={stats['errors']}"
        )
        return stats

    async def cleanup_old_records(self, max_age_days: int = 30) -> int:
        """
        Limpia registros antiguos para liberar memoria.

        Args:
            max_age_days: Edad máxima en días para mantener registros

        Returns:
            Número de registros eliminados
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        removed = 0

        async with self._lock:
            to_remove = [
                rid for rid, record in self._records.items()
                if record.timestamp < cutoff
            ]

            for rid in to_remove:
                del self._records[rid]
                removed += 1

            if removed > 0:
                logger.info(f"Cleaned up {removed} old feedback records")

        return removed

    async def export_feedback(self, output_path: Path, format: str = "jsonl") -> int:
        """
        Exporta feedback a archivo para análisis externo.

        Args:
            output_path: Ruta de salida
            format: Formato de exportación ("jsonl" o "json")

        Returns:
            Número de registros exportados
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        async with self._lock:
            records = list(self._records.values())

        if format == "json":
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in records], f, indent=2, ensure_ascii=False)
        else:  # jsonl
            with open(output_path, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

        logger.info(f"Exported {len(records)} feedback records to {output_path}")
        return len(records)

    # ── Context Manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "FeedbackLoop":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Persistir al salir si está configurado
        if self.persist_path:
            await self.export_feedback(self.persist_path, format="jsonl")


# ============================================================================
# SINGLETON GLOBAL
# ============================================================================
_loop_instance: Optional[FeedbackLoop] = None
_loop_lock = threading.Lock()


async def get_feedback_loop(
    session_id: Optional[str] = None,
    enable_implicit_detection: bool = True,
    enable_explicit_handling: bool = True,
    enable_parameter_optimization: bool = True,
    persist_path: Optional[Path] = None,
    param_persist_path: Optional[Path] = None,
) -> FeedbackLoop:
    """
    Obtiene instancia singleton del FeedbackLoop.

    Args:
        session_id: ID de sesión (genera UUID si None)
        enable_implicit_detection: Habilitar detección implícita
        enable_explicit_handling: Habilitar manejo explícito
        enable_parameter_optimization: Habilitar optimización de parámetros
        persist_path: Ruta para persistencia de feedback
        param_persist_path: Ruta JSON para el estado del `ParameterOptimizer`
            (A1). Por defecto `data/feedback/param_optimizer.json` (producción).

    Returns:
        Instancia de FeedbackLoop
    """
    global _loop_instance

    if _loop_instance is None:
        with _loop_lock:
            if _loop_instance is None:
                _loop_instance = FeedbackLoop(
                    session_id=session_id,
                    enable_implicit_detection=enable_implicit_detection,
                    enable_explicit_handling=enable_explicit_handling,
                    enable_parameter_optimization=enable_parameter_optimization,
                    persist_path=persist_path,
                    # ✅ A1: estado aprendido global (no por sesión) en data/feedback
                    param_persist_path=param_persist_path or (
                        Paths.DATA / "feedback" / "param_optimizer.json"
                    ),
                )

    return _loop_instance


async def reset_feedback_loop() -> None:
    """Reinicia el singleton del FeedbackLoop."""
    global _loop_instance

    with _loop_lock:
        _loop_instance = None


def get_feedback_loop_sync() -> FeedbackLoop:
    """
    Obtiene instancia singleton del FeedbackLoop de forma síncrona.

    Usar en contextos síncronos (__init__, propiedades) donde no se puede awaitar.
    Para contextos async usar get_feedback_loop().
    """
    global _loop_instance

    if _loop_instance is None:
        with _loop_lock:
            if _loop_instance is None:
                _loop_instance = FeedbackLoop()

    return _loop_instance
