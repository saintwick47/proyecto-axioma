#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — AgentLoop: Ciclo observe-think-act-reflect
# ═══════════════════════════════════════════════════════════════
# Ruta: src/agents/loop/agent_loop.py
# Autor: SaintWick
# Versión: 0.5.1
# Propósito: Implementa el ciclo observe-think-act-reflect para
#            agentes (OBSERVE → THINK → ACT → REFLECT), con
#            condiciones de parada configurables (iteraciones,
#            timeout global, fallos consecutivos), motor de
#            reflexión opcional, tracking de métricas por fase,
#            estimación de reasoning_budget y callback hooks por
#            fase. En outcome=IMPROVE, inyecta critique y
#            _last_score en LoopContext para la siguiente
#            iteración — sin acoplamiento directo con Planner.
# ═══════════════════════════════════════════════════════════════
import logging
import time
import asyncio
from typing import Optional, Dict, Any, List, Callable, Generator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .types import LoopPhase, ReflectionOutcome
from .state_manager import StateManager, LoopState
from .iteration_tracker import IterationTracker, IterationRecord
from .stop_conditions import StopConditions, StopReason
from .reflection_engine import ReflectionEngine, ReflectionResult

logger = logging.getLogger(__name__)


@dataclass
class LoopMetrics:
    """Métricas de ejecución del loop."""
    total_iterations: int = 0
    successful_iterations: int = 0
    failed_iterations: int = 0
    total_duration_ms: float = 0.0
    phase_durations: Dict[str, float] = field(default_factory=dict)
    stop_reason: Optional[StopReason] = None

    def avg_iteration_duration(self) -> float:
        if self.total_iterations == 0:
            return 0.0
        return self.total_duration_ms / self.total_iterations

    def success_rate(self) -> float:
        if self.total_iterations == 0:
            return 0.0
        return self.successful_iterations / self.total_iterations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_iterations": self.total_iterations,
            "successful_iterations": self.successful_iterations,
            "failed_iterations": self.failed_iterations,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "avg_iteration_duration_ms": round(self.avg_iteration_duration(), 2),
            "success_rate": round(self.success_rate(), 4),
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "phase_durations": {k: round(v, 2) for k, v in self.phase_durations.items()},
        }


@dataclass
class LoopContext:
    """Contexto compartido entre fases del loop."""
    query: str
    session_id: Optional[str] = None
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    memory_context: List[Dict[str, Any]] = field(default_factory=list)
    search_context: Optional[str] = None
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    previous_output: Optional[str] = None
    previous_critique: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # ✅ Semana 1 A2: tracking de score para detectar degradación en siguiente iteración
    _last_score: Optional[float] = None
    # ✅ [C1]: hint de presupuesto de razonamiento — metadata para callbacks, no max_tokens del LLM
    reasoning_budget: int = 1024

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "session_id": self.session_id,
            "conversation_history": self.conversation_history[-5:],
            "memory_context": self.memory_context[:3],
            "search_context": self.search_context[:500] if self.search_context else None,
            "tool_results": self.tool_results,
            "previous_output": self.previous_output[:200] if self.previous_output else None,
            "previous_critique": self.previous_critique,
            "metadata": self.metadata,
        }


class AgentLoop:
    """
    Implementa el ciclo observe-think-act-reflect para agentes.

    Flujo:
    1. OBSERVE: Recopilar contexto, memoria, historial
    2. THINK:   Planificar pasos, seleccionar herramientas
    3. ACT:     Ejecutar acción o herramienta
    4. REFLECT: Evaluar resultado, decidir continuar o parar

    ✅ Semana 1 A2: cuando outcome=IMPROVE, critique y score se inyectan
    en LoopContext para que la próxima iteración los use en evaluate().
    Sin acoplamiento directo Planner↔AgentLoop.
    """

    DEFAULT_MAX_ITERATIONS = 10
    DEFAULT_TIMEOUT_PER_STEP = 30
    DEFAULT_GLOBAL_TIMEOUT = 180

    def __init__(
        self,
        agent_type: str,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout_per_step: int = DEFAULT_TIMEOUT_PER_STEP,
        global_timeout: int = DEFAULT_GLOBAL_TIMEOUT,
        enable_reflection: bool = True,
    ):
        self.agent_type = agent_type
        self.max_iterations = max_iterations
        self.timeout_per_step = timeout_per_step
        self.global_timeout = global_timeout
        self.enable_reflection = enable_reflection

        self.state_manager = StateManager()
        self.iteration_tracker = IterationTracker(max_iterations=max_iterations)
        self.stop_conditions = StopConditions(
            max_iterations=max_iterations,
            global_timeout=global_timeout,
        )
        self.reflection_engine = ReflectionEngine() if enable_reflection else None

        self._observe_callback: Optional[Callable] = None
        self._think_callback: Optional[Callable] = None
        self._act_callback: Optional[Callable] = None
        self._reflect_callback: Optional[Callable] = None

        self._metrics = LoopMetrics()
        self._current_context: Optional[LoopContext] = None

        logger.info(f"AgentLoop initialized: agent={agent_type}, max_iter={max_iterations}")

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def set_observe_callback(self, callback: Callable[[LoopContext], LoopContext]):
        self._observe_callback = callback
        logger.debug(f"OBSERVE callback set for {self.agent_type}")

    def set_think_callback(self, callback: Callable[[LoopContext], Dict[str, Any]]):
        self._think_callback = callback
        logger.debug(f"THINK callback set for {self.agent_type}")

    def set_act_callback(self, callback: Callable[[Dict[str, Any]], Any]):
        self._act_callback = callback
        logger.debug(f"ACT callback set for {self.agent_type}")

    def set_reflect_callback(self, callback: Callable[[Any, LoopContext], ReflectionResult]):
        self._reflect_callback = callback
        logger.debug(f"REFLECT callback set for {self.agent_type}")

    # ── Ejecución principal ───────────────────────────────────────────────────

    def execute(self, query: str, **kwargs) -> Dict[str, Any]:
        """Ejecuta ciclo completo observe-think-act-reflect."""
        start_time = time.time()

        self._current_context = LoopContext(
            query=query,
            session_id=kwargs.get("session_id"),
            conversation_history=kwargs.get("conversation_history", []),
            memory_context=kwargs.get("memory_context", []),
            search_context=kwargs.get("search_context"),
            metadata=kwargs.get("metadata", {}),
        )

        self.state_manager.start()
        self.iteration_tracker.start()
        # ✅ FIX v0.5.1: inicializar StopConditions para que _start_time interno
        # quede seteado. Sin esto, get_time_remaining() y get_metrics()["time_remaining"]
        # retornaban siempre None aunque el loop llevara segundos ejecutando.
        self.stop_conditions.start()
        logger.info(f"AgentLoop started: agent={self.agent_type}, query={query[:50]}...")

        try:
            # [C1] PREPARE: estimar reasoning_budget antes del loop
            self._current_context.reasoning_budget = self._estimate_reasoning_budget(query)
            logger.debug(f"[LOOP] reasoning_budget={self._current_context.reasoning_budget}")

            while not self._should_stop():
                iteration_start = time.time()

                self.iteration_tracker.next_iteration()
                self.state_manager.set_iteration(self.iteration_tracker.current_iteration)

                # OBSERVE
                observe_start = time.time()
                self.state_manager.set_phase(LoopPhase.OBSERVE)
                self._current_context = self._execute_observe()
                self._record_phase_duration(LoopPhase.OBSERVE, time.time() - observe_start)

                # THINK
                think_start = time.time()
                self.state_manager.set_phase(LoopPhase.THINK)
                plan = self._execute_think()
                self._record_phase_duration(LoopPhase.THINK, time.time() - think_start)

                # ACT
                act_start = time.time()
                self.state_manager.set_phase(LoopPhase.ACT)
                result = self._execute_act(plan)
                self._record_phase_duration(LoopPhase.ACT, time.time() - act_start)

                # REFLECT
                reflect_start = time.time()
                self.state_manager.set_phase(LoopPhase.REFLECT)
                reflection = self._execute_reflect(
                    result, self.iteration_tracker.current_iteration
                )
                self._record_phase_duration(LoopPhase.REFLECT, time.time() - reflect_start)

                # Actualizar contexto con resultado
                self._current_context.previous_output = str(result)[:2000]
                self._current_context.tool_results.append({
                    "iteration": self.iteration_tracker.current_iteration,
                    "result": str(result)[:500],
                    "success": reflection.success,
                })

                iteration_duration = (time.time() - iteration_start) * 1000
                self.iteration_tracker.record_iteration(
                    success=reflection.success,
                    duration_ms=iteration_duration,
                    reflection_score=reflection.score,
                    reflection_critique=reflection.critique,
                )

                self._metrics.total_iterations += 1
                if reflection.success:
                    self._metrics.successful_iterations += 1
                else:
                    self._metrics.failed_iterations += 1

                if reflection.success and reflection.should_stop:
                    self.state_manager.complete()
                    self._metrics.stop_reason = StopReason.SUCCESS
                    break

            # Finalizar
            if self.state_manager.state != LoopState.COMPLETED:
                if self._should_stop_timeout():
                    self._metrics.stop_reason = StopReason.TIMEOUT
                elif self._should_stop_max_iterations():
                    self._metrics.stop_reason = StopReason.MAX_ITERATIONS
                else:
                    self._metrics.stop_reason = StopReason.STOP_CONDITION
                self.state_manager.complete()

            self._metrics.total_duration_ms = (time.time() - start_time) * 1000
            logger.info(
                f"AgentLoop completed: agent={self.agent_type}, "
                f"iterations={self._metrics.total_iterations}, "
                f"stop_reason={self._metrics.stop_reason}"
            )
            return self._build_result()

        except Exception as e:
            logger.error(f"AgentLoop error: {e}")
            self.state_manager.fail(error=str(e))
            self._metrics.stop_reason = StopReason.ERROR
            self._metrics.total_duration_ms = (time.time() - start_time) * 1000
            return self._build_result(error=str(e))

    def execute_stream(self, query: str, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """Ejecuta el loop en modo streaming (yield por fase)."""
        start_time = time.time()

        self._current_context = LoopContext(
            query=query,
            session_id=kwargs.get("session_id"),
            conversation_history=kwargs.get("conversation_history", []),
        )

        self.state_manager.start()
        self.iteration_tracker.start()
        # ✅ FIX v0.5.1: inicializar también en execute_stream para consistencia
        self.stop_conditions.start()

        try:
            while not self._should_stop():
                self.iteration_tracker.next_iteration()

                yield {
                    "type": "iteration_start",
                    "iteration": self.iteration_tracker.current_iteration,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                self.state_manager.set_phase(LoopPhase.OBSERVE)
                self._current_context = self._execute_observe()
                yield {
                    "type": "phase_complete",
                    "phase": LoopPhase.OBSERVE.value,
                    "context": self._current_context.to_dict(),
                }

                self.state_manager.set_phase(LoopPhase.THINK)
                plan = self._execute_think()
                yield {
                    "type": "phase_complete",
                    "phase": LoopPhase.THINK.value,
                    "plan": plan,
                }

                self.state_manager.set_phase(LoopPhase.ACT)
                result = self._execute_act(plan)
                yield {
                    "type": "phase_complete",
                    "phase": LoopPhase.ACT.value,
                    "result": str(result)[:500],
                }

                self.state_manager.set_phase(LoopPhase.REFLECT)
                reflection = self._execute_reflect(
                    result, self.iteration_tracker.current_iteration
                )
                yield {
                    "type": "phase_complete",
                    "phase": LoopPhase.REFLECT.value,
                    "reflection": {
                        "success": reflection.success,
                        "should_stop": reflection.should_stop,
                        "outcome": reflection.outcome.value,
                        "critique": reflection.critique,
                    },
                }

                if reflection.success and reflection.should_stop:
                    self.state_manager.complete()
                    break

            self._metrics.total_duration_ms = (time.time() - start_time) * 1000
            yield {"type": "loop_complete", "result": self._build_result()}

        except Exception as e:
            logger.error(f"AgentLoop stream error: {e}")
            yield {"type": "error", "error": str(e)}

    # ── Fases ─────────────────────────────────────────────────────────────────

    def _estimate_reasoning_budget(self, query: str) -> int:
        """
        [C1] Heurística sin LLM para estimar tokens de razonamiento.
        < 10 palabras → 512 | 10-30 → 1024 | > 30 → 2048
        Clampea entre 512 y el máximo de settings si existe.
        """
        try:
            word_count = len(query.split())
            if word_count < 10:
                budget = 512
            elif word_count <= 30:
                budget = 1024
            else:
                budget = 2048
            # Respetar settings.max_reasoning_tokens si disponible
            try:
                from config.settings import settings
                max_budget = getattr(settings, "max_reasoning_tokens", 2048)
            except Exception:
                max_budget = 2048
            return max(512, min(budget, max_budget))
        except Exception:
            return 1024

    def _execute_observe(self) -> LoopContext:
        if self._observe_callback:
            return self._observe_callback(self._current_context)
        if self._current_context.memory_context:
            logger.debug(f"OBSERVE: {len(self._current_context.memory_context)} memorias cargadas")
        return self._current_context

    def _execute_think(self) -> Dict[str, Any]:
        if self._think_callback:
            return self._think_callback(self._current_context)
        return {
            "action": "generate_response",
            "query": self._current_context.query,
            "context_available": bool(self._current_context.memory_context),
        }

    def _execute_act(self, plan: Dict[str, Any]) -> Any:
        """
        Ejecuta la fase ACT con el callback configurado.

        ✅ FIX v0.5.1: guard para callbacks async en contexto síncrono.
        Si el callback devuelve una coroutine, se lanza RuntimeError explicativo
        en lugar de retornar silenciosamente un objeto coroutine no ejecutado,
        lo que producía resultados vacíos o incorrectos sin ningún traceback.

        Para callbacks async, usar execute_stream() o convertir execute() a async.
        """
        if self._act_callback:
            result = self._act_callback(plan)
            if asyncio.iscoroutine(result):
                # Coroutine detectada en contexto síncrono — fallar explícitamente
                # en lugar de retornar un objeto coroutine sin ejecutar.
                result.close()  # Prevenir ResourceWarning de coroutine no awaitada
                raise RuntimeError(
                    f"[AgentLoop] El callback ACT de '{self.agent_type}' es async "
                    f"pero execute() es síncrono. "
                    f"Use execute_stream() o registre un callback síncrono."
                )
            return result
        return plan

    def _execute_reflect(self, result: Any, iteration: int) -> ReflectionResult:
        """
        ✅ Semana 1 A2: cuando outcome=IMPROVE, inyecta critique y _last_score
        en LoopContext para que la siguiente iteración los propague a evaluate().
        Sin acoplamiento con Planner.
        """
        if self._reflect_callback:
            return self._reflect_callback(result, self._current_context)

        if self.reflection_engine:
            reflection = self.reflection_engine.evaluate(
                result=result,
                context=self._current_context,
                iteration=iteration,
                previous_score=self._current_context._last_score,
            )

            # ✅ A2: IMPROVE → persistir critique y score en context
            if reflection.outcome == ReflectionOutcome.IMPROVE:
                self._current_context.previous_critique = reflection.critique
                self._current_context._last_score = reflection.score
                logger.debug(
                    f"[LOOP] iter={iteration} IMPROVE — "
                    f"score={reflection.score:.2f} "
                    f"critique='{reflection.critique[:80]}'"
                )
            elif reflection.outcome == ReflectionOutcome.CONTINUE:
                # CONTINUE: actualizar score pero no sobreescribir critique
                self._current_context._last_score = reflection.score
            else:
                # ACCEPT / ABORT: limpiar tracking
                self._current_context._last_score = None

            return reflection

        return ReflectionResult(
            success=True,
            should_stop=True,
            outcome=ReflectionOutcome.ACCEPT,
        )

    # ── Condiciones de parada ─────────────────────────────────────────────────

    def _should_stop(self) -> bool:
        """
        Verifica si el loop debe detenerse.

        ✅ FIX v0.5.1: pasa consecutive_failures (self._metrics.failed_iterations)
        a StopConditions.check(). Antes se omitía, lo que dejaba el cap de
        MAX_CONSECUTIVE_FAILURES completamente desconectado del loop real:
        la condición existía en StopConditions pero nunca se evaluaba.
        """
        return self.stop_conditions.check(
            iteration=self.iteration_tracker.current_iteration,
            start_time=self.iteration_tracker.start_time,
            state=self.state_manager.state,
            consecutive_failures=self._metrics.failed_iterations,
        )

    def _should_stop_timeout(self) -> bool:
        if not self.iteration_tracker.start_time:
            return False
        return time.time() - self.iteration_tracker.start_time >= self.global_timeout

    def _should_stop_max_iterations(self) -> bool:
        return self.iteration_tracker.current_iteration >= self.max_iterations

    # ── Métricas y registro ───────────────────────────────────────────────────

    def _record_phase_duration(self, phase: LoopPhase, duration_seconds: float):
        phase_key = phase.value
        if phase_key not in self._metrics.phase_durations:
            self._metrics.phase_durations[phase_key] = 0.0
        self._metrics.phase_durations[phase_key] += duration_seconds * 1000

    def _build_result(self, error: Optional[str] = None) -> Dict[str, Any]:
        return {
            "success": self.state_manager.state == LoopState.COMPLETED and not error,
            "output": self._current_context.previous_output if self._current_context else None,
            "iterations": self._metrics.total_iterations,
            "metrics": self._metrics.to_dict(),
            "context": self._current_context.to_dict() if self._current_context else None,
            "error": error,
        }

    # ── Utilidades ────────────────────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        return self._metrics.to_dict()

    def get_state(self) -> Dict[str, Any]:
        return {
            "state": self.state_manager.state.value,
            "phase": (
                self.state_manager.current_phase.value
                if self.state_manager.current_phase else None
            ),
            "iteration": self.iteration_tracker.current_iteration,
            "max_iterations": self.max_iterations,
        }

    def reset(self):
        self.state_manager.reset()
        self.iteration_tracker.reset()
        self.stop_conditions.reset()
        self._metrics = LoopMetrics()
        self._current_context = None
        logger.debug(f"AgentLoop reset: agent={self.agent_type}")
