#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — ProactiveEngine: Acciones autónomas sin input usuario
# Ruta: src/core/proactive_engine.py
# Autor: SaintWick
# Versión: 1.3.1 (FIX: await get_global_stats coroutine)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from src.domain.entities import Message, MessageRole
from src.utils.degradation import log_degraded

# ✅ CAAC Fase 1: TaskQueue — import seguro (solo para triggers proactivos)
try:
    from src.core.task_queue import TaskQueue, TaskPriority
    _TASKQUEUE_AVAILABLE = True
except ImportError:
    TaskQueue = None  # type: ignore[assignment,misc]
    TaskPriority = None  # type: ignore[assignment]
    _TASKQUEUE_AVAILABLE = False

logger = logging.getLogger(__name__)

try:
    from src.core.feedback_loop import get_feedback_loop
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False


# ── ProactiveTrigger ──────────────────────────────────────────────────────────

@dataclass
class ProactiveTrigger:
    """Definición de un trigger proactivo periódico."""
    name: str
    query: str
    interval_minutes: int
    enabled: bool = True
    run_at_hours: List[int] = field(default_factory=list)  # [] = cualquier hora
    last_run: Optional[datetime] = None
    min_ewma_threshold: float = 0.0   # 0.0 = siempre ejecutar

    def should_fire(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        # ✅ FIX v1.3.0: Verificar run_at_hours ANTES de last_run is None
        # Antes: last_run is None → return True (saltaba run_at_hours)
        # Esto causaba que morning_brief (horas [8,9]) disparara a las 15:44
        if self.run_at_hours and now.hour not in self.run_at_hours:
            return False
        if self.last_run is None:
            return True
        elapsed_min = (now - self.last_run).total_seconds() / 60
        if elapsed_min < self.interval_minutes:
            return False
        return True


# ── Triggers predefinidos ─────────────────────────────────────────────────────

DEFAULT_TRIGGERS: List[ProactiveTrigger] = [
    ProactiveTrigger(
        name="morning_brief",
        query=(
            "Dame un resumen rápido: clima actual, "
            "noticias importantes de hoy y una frase motivadora."
        ),
        interval_minutes=60 * 24,   # 1 vez por día
        run_at_hours=[8, 9],
        enabled=True,
    ),
    ProactiveTrigger(
        name="system_health_check",
        query="__SYSTEM_CHECK__",   # trigger interno — no va al LLM
        interval_minutes=30,
        enabled=True,
    ),
]


# ── ProactiveEngine ───────────────────────────────────────────────────────────

class ProactiveEngine:
    """
    Scheduler que dispara acciones sin input del usuario.

    - Lee EWMAThresholdOptimizer del FeedbackLoop para triggers adaptativos.
    - Triggers con min_ewma_threshold se saltan si calidad del sistema < threshold.
    - Triggers dinámicos generados desde patrones frecuentes del UserProfile.
    - Loop cada 60s evaluando todos los triggers registrados.
    - ✅ v1.3.0: startup_grace_seconds evita triggers en los primeros N segundos.
    """

    def __init__(
        self,
        dispatcher=None,
        tts=None,
        user_profile=None,
        context_sensor=None,
        voice_active_event=None,   # ✅ CAAC Fase 1: threading.Event compartido con VoicePipeline
        task_queue=None,           # ✅ CAAC Fase 1: TaskQueue opt-in para triggers proactivos
        startup_grace_seconds: float = 120.0,  # ✅ FIX v1.3.0: No disparar en los primeros 2 min
    ):
        self._dispatcher = dispatcher
        self._tts = tts
        self._profile = user_profile
        self._sensor = context_sensor
        self._triggers: List[ProactiveTrigger] = list(DEFAULT_TRIGGERS)
        self._running = False
        self._last_fire_time: Optional[datetime] = None  # [B2]: para check_voice_idle
        # ✅ CAAC Fase 1: voice_active_event — threading.Event read-only
        # set() / clear() lo hace VoicePipeline; ProactiveEngine solo lo lee
        self._voice_active_event = voice_active_event
        # ✅ CAAC Fase 1: TaskQueue opt-in — si None, _fire() despacha directo
        self._task_queue = task_queue
        # ✅ FIX v1.3.0: Startup grace period
        self._startup_grace_seconds = startup_grace_seconds
        self._start_time: Optional[datetime] = None
        logger.info(f"[ProactiveEngine] Inicializado (grace={startup_grace_seconds}s)")

    # ── Registro de triggers ──────────────────────────────────────────────────

    def register_trigger(self, trigger: ProactiveTrigger) -> None:
        self._triggers.append(trigger)
        logger.info(f"[ProactiveEngine] Trigger registrado: {trigger.name}")

    def register_dynamic_triggers(self) -> None:
        """
        Genera triggers desde patrones frecuentes del UserProfile.
        Solo activa si ewma > threshold del FeedbackLoop.
        """
        if not self._profile:
            return
        try:
            patterns = self._profile.get_frequent_patterns()
            for p in patterns:
                # ✅ ISSUE-066/067: la condición usa `hits` (conteo crudo) y no
                # `weight`: desde la delta rule el peso es una **frecuencia
                # relativa** (0-1) y el umbral ≥5 de antes nunca se cumpliría,
                # desactivando en silencio los triggers dinámicos. Para aristas
                # viejas sin `hits` se cae al peso anterior (≥5) como respaldo.
                hits = p.get("hits")
                if not (isinstance(hits, (int, float)) and hits >= 5
                        or (hits is None and p.get("weight", 0) >= 5)):
                    continue
                category = p.get("category", "")
                label = p.get("label", "")
                if category == "topic" and label in {"weather_query", "news_query"}:
                    trigger = ProactiveTrigger(
                        name=f"auto_{label}",
                        query=f"Dame información actualizada sobre: {label.replace('_', ' ')}",
                        interval_minutes=120,
                        run_at_hours=[7, 8, 9, 18, 19],
                        min_ewma_threshold=0.75,
                    )
                    if not any(t.name == trigger.name for t in self._triggers):
                        self.register_trigger(trigger)
        except Exception as e:
            logger.warning(f"[ProactiveEngine] Dynamic triggers error: {e}")

    # ── EWMA ─────────────────────────────────────────────────────────────────

    async def _get_ewma(self) -> float:
        """
        Lee EWMA del FeedbackLoop. Retorna 1.0 si no disponible.
        ✅ v1.3.1: get_global_stats() es coroutine, requiere await.
        """
        if not _FEEDBACK_AVAILABLE:
            return 1.0
        try:
            fl = await get_feedback_loop()
            # ✅ FIX v1.3.1: Añadido await — get_global_stats es async
            stats = await fl.get_global_stats() if hasattr(fl, "get_global_stats") else {}
            # ✅ ISSUE-067: sin señales DECIDIDAS no hay evidencia de mala calidad
            # → 1.0 (no bloquear). Antes la tasa dividía por el total e incluía
            # las señales neutras, así que quedaba artificialmente baja (0.12) y
            # bloqueaba cualquier umbral de calidad.
            if int(stats.get("decided_count") or 0) <= 0:
                return 1.0
            return float(stats.get("global_success_rate", 1.0))
        except Exception as e:
            log_degraded(logger, e, "src/core/proactive_engine.py:_get_ewma")
            return 1.0

    # ── Loop principal ────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Loop cada 60s evaluando triggers."""
        self._running = True
        self._start_time = datetime.now()  # ✅ FIX v1.3.0: Track startup time
        self.register_dynamic_triggers()
        logger.info(f"[ProactiveEngine] Loop iniciado (grace={self._startup_grace_seconds}s)")

        while self._running:
            now = datetime.now()

            # ✅ FIX v1.3.0: Startup grace period — no disparar nada en los primeros N segundos
            if self._start_time is not None:
                elapsed_since_start = (now - self._start_time).total_seconds()
                if elapsed_since_start < self._startup_grace_seconds:
                    remaining = self._startup_grace_seconds - elapsed_since_start
                    logger.debug(
                        f"[ProactiveEngine] Grace period activo — {remaining:.0f}s restantes"
                    )
                    await asyncio.sleep(60)
                    continue

            ewma = await self._get_ewma()

            for trigger in self._triggers:
                if not trigger.should_fire(now):
                    continue
                if ewma < trigger.min_ewma_threshold:
                    logger.debug(
                        f"[ProactiveEngine] {trigger.name} skip — "
                        f"ewma={ewma:.2f} < {trigger.min_ewma_threshold}"
                    )
                    continue
                # ✅ CAAC Fase 1: verificar voice_active_event antes de disparar
                if self._voice_active_event is not None and self._voice_active_event.is_set():
                    logger.debug(
                        f"[ProactiveEngine] {trigger.name} postponed — voz activa"
                    )
                    continue
                asyncio.create_task(self._fire(trigger))
                trigger.last_run = now

            await asyncio.sleep(60)

            # [B2]: Predicción proactiva si voice idle >= 60s
            if self.check_voice_idle(idle_seconds=60):
                # ✅ FIX: antes el resultado de _predict_next_intent() se
                # descartaba por completo (create_task sin usar el retorno) —
                # calculaba una query candidata y no hacía nada con ella.
                # _fire_predicted_intent() reusa el mismo pipeline
                # dispatch+TTS que los triggers programados (_fire()), con
                # un prefijo distinto para dejar claro que es una sugerencia
                # basada en patrones, no una alerta programada.
                asyncio.create_task(self._fire_predicted_intent())

    async def _fire(self, trigger: ProactiveTrigger) -> None:
        """
        Ejecuta trigger: procesa query y habla resultado.
        ✅ CAAC Fase 1:
          - Re-verifica voice_active_event al inicio (puede haber cambiado
            desde que se encoló el create_task).
          - Si TaskQueue disponible: encola con priority LOW para no bloquear
            el path de voz/usuario.
          - Si no hay TaskQueue: despacha directo (comportamiento legacy).
        """
        # Guard final: si voz activa, posponer 15s y reintentar una vez
        if self._voice_active_event is not None and self._voice_active_event.is_set():
            logger.debug(f"[ProactiveEngine] {trigger.name} — voz activa, retry en 15s")
            await asyncio.sleep(15)
            if self._voice_active_event.is_set():
                logger.debug(f"[ProactiveEngine] {trigger.name} — sigue activa, cancelado")
                return

        logger.info(f"[ProactiveEngine] Firing: {trigger.name}")
        self._last_fire_time = datetime.now()  # [B2]: track for voice idle
        try:
            if trigger.query == "__SYSTEM_CHECK__":
                await self._handle_system_check()
                return

            msg = Message(content=trigger.query, role=MessageRole.SYSTEM)

            # ✅ CAAC Fase 1: TaskQueue opt-in — solo triggers proactivos van a la cola
            if _TASKQUEUE_AVAILABLE and self._task_queue is not None and TaskPriority is not None:
                try:
                    task = await self._task_queue.enqueue(
                        name=f"proactive_{trigger.name}",
                        payload={"query": trigger.query, "trigger": trigger.name},
                        priority=TaskPriority.LOW,
                        timeout_seconds=30.0,
                        metadata={"source": "proactive_engine"},
                    )
                    # Ejecutar la tarea directamente en este contexto
                    # (TaskQueue gestiona prioridad; el worker somos nosotros aquí)
                    response = await self._dispatcher.process(
                        msg, session_id="proactive"
                    )
                    task.status = __import__(
                        'src.core.task_queue', fromlist=['TaskStatus']
                    ).TaskStatus.COMPLETED
                except Exception as tq_err:
                    logger.warning(
                        f"[ProactiveEngine] TaskQueue error, fallback directo: {tq_err}"
                    )
                    response = await self._dispatcher.process(
                        msg, session_id="proactive"
                    )
            else:
                response = await self._dispatcher.process(msg, session_id="proactive")

            if response.error:
                logger.warning(f"[ProactiveEngine] {trigger.name} error: {response.error}")
                return

            if self._tts:
                prefix = f"Aviso de {trigger.name.replace('_', ' ')}: "
                text = prefix + (response.content or "")[:300]
                await self._tts.speak(text)
            else:
                logger.info(
                    f"[ProactiveEngine] {trigger.name}: "
                    f"{(response.content or '')[:100]}"
                )

        except Exception as e:
            logger.error(f"[ProactiveEngine] Fire error {trigger.name}: {e}")

    async def _handle_system_check(self) -> None:
        """Chequeo de salud sin LLM — usa ContextSensor."""
        if not self._sensor:
            return
        try:
            hint = self._sensor.get_context_hint()
            if hint and self._tts:
                await self._tts.speak(f"Alerta del sistema: {hint}")
        except Exception as e:
            logger.warning(f"[ProactiveEngine] system check error: {e}")

    # ── [B2]: Voice idle + Intent prediction ────────────────────────────────

    def check_voice_idle(self, idle_seconds: float) -> bool:
        """
        [B2] Retorna True si han pasado idle_seconds desde el último _fire().
        Threshold: idle_seconds > 30 (configurable vía parámetro).
        Retorna False si _running=False o si nunca se disparó un trigger.
        """
        if not self._running:
            return False
        if self._last_fire_time is None:
            return False
        elapsed = (datetime.now() - self._last_fire_time).total_seconds()
        return elapsed > idle_seconds

    async def _predict_next_intent(self) -> Optional[str]:
        """
        [B2] Lee patrones frecuentes del UserProfile y predice el próximo intent.
        Aplica peso temporal: hour_weight = 1.0 + 0.5 si now.hour en run_at_hours del trigger.
        Retorna query del top-1 patrón ponderado, o None si no hay patrones.
        No debe bloquear — siempre llamar via create_task().
        """
        try:
            if not self._profile:
                return None
            patterns = self._profile.get_frequent_patterns()
            if not patterns:
                return None
            now = datetime.now()
            # Buscar trigger con run_at_hours que coincida con hora actual
            matching_hours = {
                t.name for t in self._triggers
                if t.run_at_hours and now.hour in t.run_at_hours
            }
            best_query: Optional[str] = None
            best_score = -1.0
            for p in patterns:
                base_weight = float(p.get("weight", 1.0))
                # hour_weight: +0.5 si el label del patrón coincide con trigger activo en esta hora
                label = p.get("label", "")
                hour_weight = 1.5 if any(label in name for name in matching_hours) else 1.0
                score = base_weight * hour_weight
                if score > best_score:
                    best_score = score
                    best_query = f"Dame información actualizada sobre: {label.replace('_', ' ')}"
            return best_query
        except Exception as e:
            logger.warning(f"[ProactiveEngine] _predict_next_intent error: {e}")
            return None

    async def _fire_predicted_intent(self) -> None:
        """
        ✅ NUEVO: consume el resultado de _predict_next_intent() — antes se
        calculaba y se descartaba. Reusa el mismo guard de voz activa y el
        mismo pipeline dispatch+TTS que _fire(), pero con prefijo distinto
        ("Basado en tus patrones...") para dejar claro que es una sugerencia
        inferida de patrones de uso, no una alerta programada explícitamente.
        No debe bloquear — se llama vía create_task() desde run_forever().
        """
        query = await self._predict_next_intent()
        if not query:
            return

        if self._voice_active_event is not None and self._voice_active_event.is_set():
            logger.debug("[ProactiveEngine] predicted intent — voz activa, descartado")
            return

        logger.info(f"[ProactiveEngine] Predicted intent firing: {query!r}")
        try:
            msg = Message(content=query, role=MessageRole.SYSTEM)
            response = await self._dispatcher.process(msg, session_id="proactive_predicted")

            if response.error:
                logger.warning(f"[ProactiveEngine] predicted intent error: {response.error}")
                return

            if self._tts:
                text = "Basado en tus patrones, tal vez te interese: " + (response.content or "")[:300]
                await self._tts.speak(text)
            else:
                logger.info(f"[ProactiveEngine] predicted intent: {(response.content or '')[:100]}")
        except Exception as e:
            logger.error(f"[ProactiveEngine] predicted intent fire error: {e}")

    def stop(self) -> None:
        self._running = False
        logger.info("[ProactiveEngine] Detenido")

    # ── Utilidades ────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "startup_grace_seconds": self._startup_grace_seconds,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "triggers": [
                {
                    "name": t.name,
                    "enabled": t.enabled,
                    "interval_minutes": t.interval_minutes,
                    "last_run": t.last_run.isoformat() if t.last_run else None,
                    "min_ewma_threshold": t.min_ewma_threshold,
                }
                for t in self._triggers
            ],
        }
