#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.3.0 — Reflection Engine para Agent Loop
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/loop/reflection_engine.py
# Autor: SaintWick
# Fecha: 2026-04-01
# Propósito: Motor de reflexión y auto-corrección
# Versión: 0.3.0 (Fase 1, Semana 2)
# ═══════════════════════════════════════════════════════════════

import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

# ✅ FIX: Importar desde types.py (NO definir ReflectionOutcome localmente)
from .types import ReflectionOutcome

logger = logging.getLogger(__name__)


@dataclass
class ReflectionResult:
    """Resultado de la evaluación de reflexión."""
    success: bool
    should_stop: bool
    outcome: ReflectionOutcome
    score: float = 0.0
    critique: str = ""
    improvements: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "success": self.success,
            "should_stop": self.should_stop,
            "outcome": self.outcome.value,
            "score": round(self.score, 4),
            "critique": self.critique[:500] if self.critique else "",
            "improvements": self.improvements,
            "metadata": self.metadata,
        }


class ReflectionEngine:
    """
    Motor de reflexión y auto-corrección.

    Responsabilidades:
    - Evaluar calidad del resultado
    - Generar crítica constructiva
    - Sugerir mejoras específicas
    - Decidir si continuar o parar
    - Trackear patrones de error
    """

    DEFAULT_QUALITY_THRESHOLD = 0.80
    DEFAULT_MIN_SCORE_FOR_ACCEPT = 0.70
    DEFAULT_MAX_IMPROVEMENTS = 5

    def __init__(
        self,
        quality_threshold: float = DEFAULT_QUALITY_THRESHOLD,
        min_score_for_accept: float = DEFAULT_MIN_SCORE_FOR_ACCEPT,
        max_improvements: int = DEFAULT_MAX_IMPROVEMENTS,
    ):
        self.quality_threshold = quality_threshold
        self.min_score_for_accept = min_score_for_accept
        self.max_improvements = max_improvements

        self._evaluation_history: List[Dict[str, Any]] = []
        self._error_patterns: Dict[str, int] = {}

        logger.debug(
            f"ReflectionEngine initialized: threshold={quality_threshold}, "
            f"min_accept={min_score_for_accept}"
        )

    # ═══════════════════════════════════════════════════════════
    # EVALUACIÓN PRINCIPAL
    # ═══════════════════════════════════════════════════════════

    def evaluate(
        self,
        result: Any,
        context: Any = None,
        iteration: int = 1,
        previous_score: Optional[float] = None,
        quality_metrics: Optional[Dict[str, Any]] = None,
    ) -> ReflectionResult:
        """
        Evalúa resultado y genera reflexión.

        Args:
            result: Resultado a evaluar
            context: Contexto de ejecución
            iteration: Número de iteración actual
            previous_score: Score de iteración anterior
            quality_metrics: Métricas de calidad específicas

        Returns:
            ReflectionResult: Resultado de la evaluación
        """
        score = self._calculate_score(result, quality_metrics)
        problems = self._detect_problems(result, context, quality_metrics)
        critique = self._generate_critique(problems, score, iteration)
        improvements = self._suggest_improvements(problems, self.max_improvements)
        outcome = self._determine_outcome(score, previous_score, iteration, problems)

        reflection = ReflectionResult(
            success=score >= self.min_score_for_accept,
            should_stop=outcome in [ReflectionOutcome.ACCEPT, ReflectionOutcome.ABORT],
            outcome=outcome,
            score=score,
            critique=critique,
            improvements=improvements,
            metadata={
                "iteration": iteration,
                "problems_detected": len(problems),
                "previous_score": previous_score,
            },
        )

        self._evaluation_history.append(reflection.to_dict())

        for problem in problems:
            problem_type = problem.get("type", "unknown")
            self._error_patterns[problem_type] = self._error_patterns.get(problem_type, 0) + 1

        logger.info(
            f"Reflection: iteration={iteration}, score={score:.2f}, "
            f"outcome={outcome.value}, problems={len(problems)}"
        )

        return reflection

    # ═══════════════════════════════════════════════════════════
    # CÁLCULO DE SCORE
    # ═══════════════════════════════════════════════════════════

    def _calculate_score(
        self,
        result: Any,
        quality_metrics: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Calcula score de calidad del resultado.

        Args:
            result: Resultado a evaluar
            quality_metrics: Métricas específicas de calidad

        Returns:
            float: Score entre 0.0 y 1.0
        """
        score = 0.5

        if quality_metrics:
            if "has_syntax" in quality_metrics:
                score += 0.4 if quality_metrics["has_syntax"] else 0.0
            if "has_docstrings" in quality_metrics:
                score += 0.25 if quality_metrics["has_docstrings"] else 0.0
            if "has_error_handling" in quality_metrics:
                score += 0.15 if quality_metrics["has_error_handling"] else 0.0
            if "has_tests" in quality_metrics:
                score += 0.10 if quality_metrics["has_tests"] else 0.0
            if "logical_structure_score" in quality_metrics:
                score += quality_metrics["logical_structure_score"] * 0.10

        if isinstance(result, str):
            result = result.strip()
            if not result:
                return 0.0
            if len(result) < 10:
                score -= 0.3
            if "error" in result.lower() or "fail" in result.lower():
                score -= 0.2

        return max(0.0, min(1.0, score))

    # ═══════════════════════════════════════════════════════════
    # DETECCIÓN DE PROBLEMAS
    # ═══════════════════════════════════════════════════════════

    def _detect_problems(
        self,
        result: Any,
        context: Any = None,
        quality_metrics: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Detecta problemas en el resultado.

        Args:
            result: Resultado a analizar
            context: Contexto de ejecución
            quality_metrics: Métricas de calidad

        Returns:
            Lista de problemas detectados
        """
        problems = []

        if quality_metrics:
            if not quality_metrics.get("has_syntax", True):
                problems.append({
                    "type": "syntax_error",
                    "severity": "high",
                    "description": quality_metrics.get("syntax_error", "Error de sintaxis"),
                })

            if not quality_metrics.get("has_docstrings", True):
                problems.append({
                    "type": "missing_docs",
                    "severity": "medium",
                    "description": "Faltan docstrings",
                })

            if not quality_metrics.get("has_error_handling", True):
                problems.append({
                    "type": "missing_error_handling",
                    "severity": "medium",
                    "description": "Falta manejo de errores",
                })

            if quality_metrics.get("quality_score", 1.0) < 0.50:
                problems.append({
                    "type": "low_quality",
                    "severity": "high",
                    "description": f"Calidad muy baja: {quality_metrics.get('quality_score', 0):.2f}",
                })

        if isinstance(result, str):
            result_lower = result.lower()
            if "not implemented" in result_lower:
                problems.append({
                    "type": "not_implemented",
                    "severity": "high",
                    "description": "Código no implementado",
                })
            if "todo" in result_lower or "FIXME" in result_lower:
                problems.append({
                    "type": "incomplete",
                    "severity": "low",
                    "description": "Código incompleto (TODO/FIXME)",
                })

        return problems

    # ═══════════════════════════════════════════════════════════
    # GENERACIÓN DE CRÍTICA
    # ═══════════════════════════════════════════════════════════

    def _generate_critique(
        self,
        problems: List[Dict[str, Any]],
        score: float,
        iteration: int,
    ) -> str:
        """
        Genera crítica constructiva.

        Args:
            problems: Problemas detectados
            score: Score de calidad
            iteration: Iteración actual

        Returns:
            str: Crítica para mejorar
        """
        if not problems:
            return "Resultado aceptable. No se detectaron problemas críticos."

        lines = [f"Iteración {iteration}: Se detectaron {len(problems)} problema(s):"]

        for i, problem in enumerate(problems, 1):
            severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                problem.get("severity", "low"), "⚪"
            )
            lines.append(
                f"  {severity_icon} {i}. {problem.get('type', 'unknown')}: "
                f"{problem.get('description', 'Sin descripción')}"
            )

        if score < 0.50:
            lines.append("")
            lines.append("⚠️ CALIDAD CRÍTICA: Se requiere mejora significativa.")
        elif score < 0.70:
            lines.append("")
            lines.append("⚡ CALIDAD BAJA: Mejoras recomendadas antes de aceptar.")

        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # SUGERENCIAS DE MEJORA
    # ═══════════════════════════════════════════════════════════

    def _suggest_improvements(
        self,
        problems: List[Dict[str, Any]],
        max_count: int = 5,
    ) -> List[str]:
        """
        Sugiere mejoras específicas.

        Args:
            problems: Problemas detectados
            max_count: Máximo de sugerencias

        Returns:
            Lista de sugerencias
        """
        suggestions = []

        improvement_map = {
            "syntax_error": "Corregir errores de sintaxis antes de continuar",
            "missing_docs": "Agregar docstrings con Args, Returns, Raises",
            "missing_error_handling": "Implementar try/except para manejo de errores",
            "low_quality": "Revisar estructura lógica y completar implementación",
            "not_implemented": "Implementar funcionalidad pendiente",
            "incomplete": "Completar secciones marcadas con TODO/FIXME",
        }

        for problem in problems[:max_count]:
            problem_type = problem.get("type", "unknown")
            suggestion = improvement_map.get(
                problem_type,
                f"Resolver: {problem.get('description', 'problema no especificado')}",
            )
            suggestions.append(suggestion)

        return suggestions

    # ═══════════════════════════════════════════════════════════
    # DETERMINACIÓN DE OUTCOME
    # ═══════════════════════════════════════════════════════════

    def _determine_outcome(
        self,
        score: float,
        previous_score: Optional[float],
        iteration: int,
        problems: List[Dict[str, Any]],
    ) -> ReflectionOutcome:
        """
        Determina el outcome de la reflexión.

        Args:
            score: Score actual
            previous_score: Score anterior
            iteration: Iteración actual
            problems: Problemas detectados

        Returns:
            ReflectionOutcome: Decisión tomada
        """
        if score >= self.quality_threshold:
            return ReflectionOutcome.ACCEPT

        if score < 0.30 and iteration < 3:
            return ReflectionOutcome.IMPROVE

        if previous_score is not None and score < previous_score * 0.70:
            logger.warning(f"Quality degradation: {previous_score:.2f} → {score:.2f}")
            return ReflectionOutcome.ABORT

        critical_problems = [p for p in problems if p.get("severity") == "high"]
        if critical_problems:
            return ReflectionOutcome.IMPROVE

        if score >= self.min_score_for_accept:
            return ReflectionOutcome.CONTINUE

        return ReflectionOutcome.IMPROVE

    # ═══════════════════════════════════════════════════════════
    # HISTORIAL Y PATRONES
    # ═══════════════════════════════════════════════════════════

    def get_evaluation_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Retorna historial de evaluaciones.

        Args:
            limit: Máximo de registros

        Returns:
            Lista de evaluaciones
        """
        return self._evaluation_history[-limit:]

    def get_error_patterns(self) -> Dict[str, int]:
        """Retorna patrones de error detectados."""
        return self._error_patterns.copy()

    def get_most_common_error(self) -> Optional[str]:
        """Retorna el error más común."""
        if not self._error_patterns:
            return None
        return max(self._error_patterns, key=self._error_patterns.get)

    def get_metrics(self) -> Dict[str, Any]:
        """Retorna métricas del engine."""
        scores = [e.get("score", 0) for e in self._evaluation_history]
        return {
            "total_evaluations": len(self._evaluation_history),
            "average_score": sum(scores) / len(scores) if scores else 0.0,
            "quality_threshold": self.quality_threshold,
            "min_score_for_accept": self.min_score_for_accept,
            "error_patterns": self._error_patterns,
            "most_common_error": self.get_most_common_error(),
        }

    def reset(self):
        """Reinicia el engine."""
        self._evaluation_history.clear()
        self._error_patterns.clear()
        logger.debug("ReflectionEngine reset")

    def to_dict(self) -> Dict[str, Any]:
        """Serializa estado completo."""
        return {
            "quality_threshold": self.quality_threshold,
            "min_score_for_accept": self.min_score_for_accept,
            "max_improvements": self.max_improvements,
            "evaluation_count": len(self._evaluation_history),
            "error_patterns": self._error_patterns,
            "metrics": self.get_metrics(),
        }
