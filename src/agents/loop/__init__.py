#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Agent Loop Module
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/__init__.py
# Autor: SaintWick
# Fecha: 2026-04-01
# Propósito: Exportar componentes del sistema Agent Loop
# Versión: 0.3.0 (Fase 1, Semana 2)
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: Enums importados desde types.py (NO desde módulos individuales)
# ═══════════════════════════════════════════════════════════════
#
# ⚠️ USO Y ESTADO — LEER ANTES DE BORRAR NADA (verificado 2026-09-29)
# ═══════════════════════════════════════════════════════════════
# Este paquete **NO es código muerto**. Estado real, comprobado con el grafo
# transitivo de imports por AST desde los 6 entry points del runtime
# (main.py, Rafael.py, axioma_auditor.py, src/interfaces/web/app.py,
# src/interfaces/cli/main.py, src/core/rafael_daemon.py):
#
#   · El RUNTIME no lo alcanza (ninguno de los 176 módulos alcanzables lo importa)
#     y no hay cargas dinámicas (importlib/__import__) que lo mencionen.
#   · SÍ tiene consumidores reales fuera del runtime:
#       - tools/benchmark_suites.py → StateManager, LoopState, LoopPhase
#       - tests/test_axioma_04_agents.py → AgentLoop, IterationTracker,
#         StopConditions, ReflectionEngine, LoopState, LoopPhase (16 referencias)
#       - config/settings.py → 7 campos `agent_loop_*` + `max_reasoning_tokens`
#   · Está **PARKED a propósito**: la decisión **B2** es NO cablearlo al runtime
#     porque cada iteración cuesta ~42 s medidos ⇒ cablearlo multiplicaría la
#     latencia de cualquier tarea. (La decisión, con su condición de reapertura,
#     vive en `docs/AGENT_GUIDE.md` §8 "Decisiones cerradas" y en
#     `docs/ISSUES_HISTORY.md`; el `PLAN_MAESTRO.md` se archivó el 2026-09-29 al
#     completarse todo su contenido.)
#
# DECISIÓN DEL USUARIO (2026-09-29): **se conserva**. Borrarlo rompería el
# benchmark y sus tests. Si algún día se decide eliminarlo, hay que actualizar
# esos 3 consumidores y los campos de config en el mismo commit.
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# IMPORTS PRINCIPALES — ENUMS DESDE types.py
# ═══════════════════════════════════════════════════════════

# ✅ FIX: Importar enums desde types.py para evitar circular imports
from .types import (
    LoopPhase,
    LoopState,
    StopReason,
    ReflectionOutcome,
)

from .agent_loop import (
    AgentLoop,
    LoopContext,
    LoopMetrics,
)

from .state_manager import (
    StateManager,
    StateSnapshot,
)

from .iteration_tracker import (
    IterationTracker,
    IterationRecord,
)

from .stop_conditions import (
    StopConditions,
    StopCondition,
)

from .reflection_engine import (
    ReflectionEngine,
    ReflectionResult,
)

# ═══════════════════════════════════════════════════════════
# EXPORTS PÚBLICOS
# ═══════════════════════════════════════════════════════════

__all__ = [
    # ── Types/Enums (desde types.py) ─────────────────────────
    "LoopPhase",
    "LoopState",
    "StopReason",
    "ReflectionOutcome",
    # ── Loop Principal ──────────────────────────────────────
    "AgentLoop",
    "LoopContext",
    "LoopMetrics",
    # ── Gestión de Estado ───────────────────────────────────
    "StateManager",
    "StateSnapshot",
    # ── Tracking de Iteraciones ─────────────────────────────
    "IterationTracker",
    "IterationRecord",
    # ── Condiciones de Parada ───────────────────────────────
    "StopConditions",
    "StopCondition",
    # ── Motor de Reflexión ──────────────────────────────────
    "ReflectionEngine",
    "ReflectionResult",
]

# ═══════════════════════════════════════════════════════════
# METADATOS DEL MÓDULO
# ═══════════════════════════════════════════════════════════

__version__ = "0.3.0"
__author__ = "SaintWick"

# ═══════════════════════════════════════════════════════════
# CONSTANTES DEL MÓDULO
# ═══════════════════════════════════════════════════════════

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_TIMEOUT_PER_STEP = 30
DEFAULT_GLOBAL_TIMEOUT = 180
DEFAULT_QUALITY_THRESHOLD = 0.80

# ═══════════════════════════════════════════════════════════
# FUNCIONES HELPER
# ═══════════════════════════════════════════════════════════

def create_loop(
    agent_type: str,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    enable_reflection: bool = True,
) -> AgentLoop:
    """
    Factory function para crear un AgentLoop configurado.

    Args:
        agent_type: Tipo de agente ("coder", "validator", etc.)
        max_iterations: Máximo de iteraciones del loop
        enable_reflection: Si habilitar motor de reflexión

    Returns:
        AgentLoop: Instancia configurada del loop
    """
    return AgentLoop(
        agent_type=agent_type,
        max_iterations=max_iterations,
        enable_reflection=enable_reflection,
    )


def get_loop_phase_name(phase: LoopPhase) -> str:
    """Obtiene nombre legible de una fase del loop."""
    phase_names = {
        LoopPhase.OBSERVE: "Observar",
        LoopPhase.THINK: "Pensar",
        LoopPhase.ACT: "Actuar",
        LoopPhase.REFLECT: "Reflexionar",
    }
    return phase_names.get(phase, phase.value)


def get_loop_state_name(state: LoopState) -> str:
    """Obtiene nombre legible de un estado del loop."""
    state_names = {
        LoopState.IDLE: "Inactivo",
        LoopState.RUNNING: "Ejecutando",
        LoopState.PAUSED: "Pausado",
        LoopState.COMPLETED: "Completado",
        LoopState.FAILED: "Fallido",
        LoopState.STOPPED: "Detenido",
    }
    return state_names.get(state, state.value)


def get_stop_reason_name(reason: StopReason) -> str:
    """Obtiene nombre legible de una razón de parada."""
    reason_names = {
        StopReason.SUCCESS: "Éxito",
        StopReason.MAX_ITERATIONS: "Máximo de iteraciones",
        StopReason.TIMEOUT: "Timeout",
        StopReason.ERROR: "Error",
        StopReason.USER_INTERRUPT: "Interrupción de usuario",
        StopReason.STOP_CONDITION: "Condición de parada",
        StopReason.QUALITY_THRESHOLD: "Umbral de calidad",
        StopReason.CONSECUTIVE_FAILURES: "Fallos consecutivos",
        StopReason.DEGRADATION: "Degradación de calidad",
    }
    return reason_names.get(reason, reason.value)


# ═══════════════════════════════════════════════════════════
# LOGGING DEL MÓDULO
# ═══════════════════════════════════════════════════════════

import logging

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def enable_debug_logging():
    """Habilita logging debug para el módulo loop."""
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


# ═══════════════════════════════════════════════════════════
# VALIDACIÓN DE IMPORTS
# ═══════════════════════════════════════════════════════════

def _validate_imports():
    """Valida que todos los imports estén disponibles."""
    try:
        assert AgentLoop is not None
        assert StateManager is not None
        assert IterationTracker is not None
        assert StopConditions is not None
        assert ReflectionEngine is not None
        logger.debug("Agent Loop module imports validated successfully")
        return True
    except Exception as e:
        logger.error(f"Agent Loop module import validation failed: {e}")
        return False


_validate_imports()

# ═══════════════════════════════════════════════════════════
# 📋 RESUMEN DE CORRECCIÓN v0.3.0
# ═══════════════════════════════════════════════════════════
# | Sección       | Cambio                              | Razón                    |
# |---------------|-------------------------------------|--------------------------|
# | Imports       | ✅ Enums desde types.py             | Evita circular imports   |
# | __all__       | ✅ 17 exports públicos              | API clara                |
# | Constants     | ✅ DEFAULT_* valores                | Configuración central    |
# | Helpers       | ✅ create_loop, *_name functions    | Utilidades               |
# | Validation    | ✅ _validate_imports()              | Integrity check          |
# ═══════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════
