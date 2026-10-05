#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Layer 1: Task Classification & Routing Logic
# Ruta: src/prompts/task_prompts.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 1 del sistema de prompts.
Inyecta el contexto de clasificación de tarea para que el agente
sepa exactamente qué se espera de él en esta ejecución.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# Descripciones de task types para inyectar en prompts
TASK_DESCRIPTIONS: Dict[str, str] = {
    "CODE_GENERATION":   "Generar código nuevo funcional y documentado",
    "CODE_DEBUGGING":    "Analizar y corregir errores en código existente",
    "CODE_EXPLANATION":  "Explicar código de forma clara y didáctica",
    "CODE_REVIEW":       "Revisar calidad, estilo y posibles mejoras del código",
    "WEB_SEARCH":        "Buscar información actualizada en fuentes externas",
    "NEWS_SEARCH":       "Encontrar noticias recientes sobre un tema",
    "RESEARCH":          "Investigación profunda y síntesis de información",
    "ANALYSIS":          "Analizar datos, patrones o situaciones con métricas",
    "VALIDATION":        "Evaluar la calidad y precisión de una respuesta",
    "GENERAL_CHAT":      "Conversación general, preguntas directas, asistencia",
    "UNKNOWN":           "Tarea no clasificada — inferir del contexto",
}


@dataclass
class TaskLayer:
    """
    Layer 1: Clasificación y contexto de tarea.

    Informa al agente sobre el tipo de tarea detectada,
    el objetivo específico y las expectativas de la respuesta.
    """
    task_type: str
    description: Optional[str] = None
    user_intent: Optional[str] = None
    complexity_hint: Optional[str] = None  # "simple" | "medium" | "complex"
    context_clues: List[str] = field(default_factory=list)

    def build(self) -> str:
        """
        Construye el bloque de Layer 1.

        Returns:
            String del layer 1 con contexto de tarea
        """
        # ✅ FIX: task_type llega en minúscula desde el clasificador (ej.
        # "code_generation", igual a TaskType.CODE_GENERATION.value), pero
        # TASK_DESCRIPTIONS usa keys en mayúscula ("CODE_GENERATION") — sin
        # normalizar, este lookup fallaba SIEMPRE y caía a "UNKNOWN" en cada
        # request real, aunque no rompía nada (TAREA ACTUAL igual mostraba
        # el task_type crudo, solo faltaba la descripción legible).
        task_desc = self.description or TASK_DESCRIPTIONS.get(
            self.task_type.upper(), TASK_DESCRIPTIONS["UNKNOWN"]
        )
        lines = [
            f"TAREA ACTUAL: {self.task_type}",
            f"Objetivo: {task_desc}",
        ]
        if self.user_intent:
            lines.append(f"Intención del usuario: {self.user_intent}")
        if self.complexity_hint:
            lines.append(f"Complejidad estimada: {self.complexity_hint}")
        if self.context_clues:
            clues = ", ".join(self.context_clues)
            lines.append(f"Indicadores detectados: {clues}")
        return "\n".join(lines)

    @classmethod
    def from_task_type(cls, task_type: str, **kwargs: Any) -> "TaskLayer":
        """
        Crea un TaskLayer desde un task_type string.

        Args:
            task_type: Tipo de tarea (ej: 'CODE_GENERATION')
            **kwargs: Parámetros opcionales adicionales

        Returns:
            Instancia de TaskLayer configurada
        """
        return cls(task_type=task_type, **kwargs)

    @classmethod
    def infer_complexity(cls, line_count: Optional[int] = None,
                         word_count: Optional[int] = None) -> str:
        """
        Infiere la complejidad de la tarea según métricas.

        Args:
            line_count: Líneas de código estimadas
            word_count: Palabras en el prompt del usuario

        Returns:
            String de complejidad: 'simple' | 'medium' | 'complex'
        """
        if line_count is not None:
            if line_count <= 30:
                return "simple"
            elif line_count <= 100:
                return "medium"
            return "complex"
        if word_count is not None:
            if word_count <= 20:
                return "simple"
            elif word_count <= 80:
                return "medium"
            return "complex"
        return "medium"
