#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Tipos y Enums compartidos para Agent Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/types.py
# Autor: SaintWick
# Fecha: 2026-04-01
# Propósito: Centralizar enums para evitar imports circulares
# Versión: 0.3.0 (Fase 1, Semana 2)
# ═══════════════════════════════════════════════════════════════

from enum import Enum


class LoopPhase(str, Enum):
    """Fases del ciclo Agent Loop."""
    OBSERVE = "observe"      # Recopilar contexto y memoria
    THINK = "think"          # Planificar y razonar
    ACT = "act"              # Ejecutar herramienta o acción
    REFLECT = "reflect"      # Evaluar resultado y auto-corregir


class LoopState(str, Enum):
    """Estados posibles del Agent Loop."""
    IDLE = "idle"              # Sin ejecución activa
    RUNNING = "running"        # Loop en ejecución
    PAUSED = "paused"          # Loop pausado (esperando input)
    COMPLETED = "completed"    # Loop completado exitosamente
    FAILED = "failed"          # Loop falló con error
    STOPPED = "stopped"        # Loop detenido por condición


class StopReason(str, Enum):
    """Razones posibles para detener el loop."""
    SUCCESS = "success"                    # Tarea completada exitosamente
    MAX_ITERATIONS = "max_iterations"      # Alcanzó máximo de iteraciones
    TIMEOUT = "timeout"                    # Timeout global excedido
    ERROR = "error"                        # Error crítico
    USER_INTERRUPT = "user_interrupt"      # Usuario detuvo ejecución
    STOP_CONDITION = "stop_condition"      # Condición custom activada
    QUALITY_THRESHOLD = "quality_threshold" # Score de calidad alcanzado
    CONSECUTIVE_FAILURES = "consecutive_failures"  # Múltiples fallos seguidos
    DEGRADATION = "degradation"            # Degradación de calidad detectada


class ReflectionOutcome(str, Enum):
    """Resultado posible de la reflexión."""
    CONTINUE = "continue"        # Continuar con siguiente iteración
    IMPROVE = "improve"          # Mejorar y reintentar
    ACCEPT = "accept"            # Aceptar resultado actual
    ABORT = "abort"              # Abortar ejecución


# ═══════════════════════════════════════════════════════════
# EXPORTS
# ═══════════════════════════════════════════════════════════

__all__ = [
    "LoopPhase",
    "LoopState",
    "StopReason",
    "ReflectionOutcome",
]
