#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Layer 5: Self-Correction & Reflection Triggers
# Ruta: src/prompts/reflection_triggers.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 5 del sistema de prompts.
Activa la reflexión y auto-corrección cuando la calidad
no alcanza el umbral esperado o se detectan problemas.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict


# Preguntas de auto-revisión por tipo de agente
REFLECTION_QUESTIONS: Dict[str, List[str]] = {
    "coder": [
        "¿El código compila sin errores de sintaxis?",
        "¿Todas las funciones tienen docstring con Args/Returns/Raises?",
        "¿Hay type hints en todos los parámetros y valores de retorno?",
        "¿El manejo de errores es adecuado para el contexto?",
        "¿El código hace exactamente lo que se pidió?",
    ],
    "validator": [
        "¿Mi evaluación es objetiva y basada en criterios claros?",
        "¿El output es JSON válido y parseable?",
        "¿La justificación es específica y no genérica?",
        "¿El score refleja correctamente la calidad real?",
    ],
    "searcher": [
        "¿La información es verificable y no inventada?",
        "¿La respuesta incluye la fuente de los datos?",
        "¿Distinguí claramente hechos de especulación?",
        "¿La información es reciente y relevante?",
    ],
    "researcher": [
        "¿El análisis está respaldado por evidencia concreta?",
        "¿Consideré múltiples perspectivas sobre el tema?",
        "¿La conclusión es útil y accionable para el usuario?",
        "¿El resumen ejecutivo captura lo más importante?",
    ],
    "analyst": [
        "¿Todos los números tienen unidades especificadas?",
        "¿Las conclusiones se derivan directamente de los datos?",
        "¿El formato es legible y estructurado?",
        "¿Indiqué la incertidumbre cuando existe?",
    ],
    "base": [
        "¿La respuesta es directa y responde la pregunta?",
        "¿El tono es apropiado para el contexto?",
        "¿Evité preámbulos innecesarios?",
    ],
}

# Instrucciones de mejora por tipo de problema
IMPROVEMENT_TEMPLATES: Dict[str, str] = {
    "low_quality":     "La calidad anterior fue {score:.2f}. Mejora: {critique}",
    "syntax_error":    "Error de sintaxis detectado: {error}. Genera código corregido.",
    "missing_docs":    "Faltan docstrings en: {missing}. Agrégalos con formato Google.",
    "json_invalid":    "El JSON generado es inválido. Asegúrate de que sea parseable.",
    "too_short":       "Respuesta demasiado breve. Desarrolla más: {aspect}",
    "off_topic":       "La respuesta no aborda la pregunta principal. Enfócate en: {topic}",
    "no_sources":      "Falta indicar fuentes. Agrega al menos una referencia.",
    "generic_answer":  "Respuesta genérica detectada. Sé más específico sobre: {aspect}",
}


@dataclass
class ReflectionLayer:
    """
    Layer 5: Auto-corrección y reflexión.

    Se activa cuando hay un reintento o cuando se quiere
    que el agente revise su output antes de entregarlo.
    """
    agent_type: str = "base"
    previous_score: Optional[float] = None
    critique: Optional[str] = None
    problem_type: Optional[str] = None
    problem_detail: Optional[str] = None
    enable_pre_check: bool = True  # Pregunta de revisión antes de responder
    custom_questions: List[str] = field(default_factory=list)

    def build(self) -> str:
        """
        Construye el bloque de Layer 5.

        Returns:
            String del layer 5, vacío si no hay reflexión activa
        """
        parts = []

        # Crítica de intento anterior (para reintentos)
        if self.previous_score is not None and self.critique:
            improvement_key = self.problem_type or "low_quality"
            template = IMPROVEMENT_TEMPLATES.get(improvement_key, IMPROVEMENT_TEMPLATES["low_quality"])
            try:
                improvement_msg = template.format(
                    score=self.previous_score,
                    critique=self.critique,
                    error=self.problem_detail or "",
                    missing=self.problem_detail or "",
                    aspect=self.problem_detail or "el contenido principal",
                    topic=self.problem_detail or "la pregunta del usuario",
                )
            except (KeyError, ValueError):
                improvement_msg = f"Mejora necesaria: {self.critique}"
            parts.append(f"⚠️ REINTENTO — {improvement_msg}")

        # Pre-check: preguntas de auto-revisión
        if self.enable_pre_check:
            questions = (
                self.custom_questions
                or REFLECTION_QUESTIONS.get(self.agent_type, REFLECTION_QUESTIONS["base"])
            )
            if questions:
                q_list = "\n".join(f"  • {q}" for q in questions[:4])  # Máximo 4
                parts.append(
                    "ANTES DE RESPONDER, verifica internamente:\n"
                    f"{q_list}\n"
                    "(No muestres estas preguntas al usuario)"
                )

        return "\n\n".join(parts) if parts else ""

    @classmethod
    def on_retry(
        cls,
        agent_type: str,
        score: float,
        critique: str,
        problem_type: str = "low_quality",
        detail: Optional[str] = None,
    ) -> "ReflectionLayer":
        """
        Factory para activar reflexión en un reintento.

        Args:
            agent_type: Tipo de agente que reintenta
            score: Score del intento anterior
            critique: Descripción del problema
            problem_type: Clave del tipo de problema
            detail: Detalle adicional del problema

        Returns:
            ReflectionLayer configurado para reintento
        """
        return cls(
            agent_type=agent_type,
            previous_score=score,
            critique=critique,
            problem_type=problem_type,
            problem_detail=detail,
            enable_pre_check=True,
        )

    @classmethod
    def standard(cls, agent_type: str) -> "ReflectionLayer":
        """
        Reflexión estándar sin reintento — solo pre-check.

        Args:
            agent_type: Tipo de agente

        Returns:
            ReflectionLayer con solo preguntas de auto-revisión
        """
        return cls(agent_type=agent_type, enable_pre_check=True)
