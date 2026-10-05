#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Layer 4: Output Formatting & Safety Constraints
# Ruta: src/prompts/output_formats.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 4 del sistema de prompts.
Define el formato exacto de salida esperado para cada tipo de agente
y refuerza las restricciones de seguridad de output.
"""
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class OutputStyle(str, Enum):
    """Estilos de formato de salida disponibles."""
    DIRECT      = "direct"      # Respuesta directa, sin estructura
    MARKDOWN    = "markdown"    # Headers, listas, tablas
    CODE_ONLY   = "code_only"   # Solo bloque de código
    JSON        = "json"        # JSON estructurado
    REPORT      = "report"      # Reporte con secciones
    CONVERSATIONAL = "conversational"  # Tono natural, párrafos


# Instrucciones de formato por estilo
FORMAT_INSTRUCTIONS: dict = {
    OutputStyle.DIRECT: (
        "Responde de forma directa y concisa.\n"
        "• Dato/resultado primero\n"
        "• Explicación breve si es necesaria\n"
        "• Sin preámbulos ni meta-comentarios"
    ),
    OutputStyle.MARKDOWN: (
        "Usa formato Markdown:\n"
        "• ## para secciones principales\n"
        "• **negrita** para términos clave\n"
        "• Tablas para datos comparativos\n"
        "• Listas para items enumerables"
    ),
    OutputStyle.CODE_ONLY: (
        "Tu respuesta debe contener ÚNICAMENTE el bloque de código:\n"
        "```python\n# código aquí\n```\n"
        "Sin explicaciones adicionales salvo que se soliciten."
    ),
    OutputStyle.JSON: (
        "Responde ÚNICAMENTE con JSON válido.\n"
        "Sin texto antes ni después del JSON.\n"
        "Sin bloques de código markdown alrededor del JSON."
    ),
    OutputStyle.REPORT: (
        "Estructura tu respuesta como reporte:\n"
        "## Resumen ejecutivo (2-3 oraciones)\n"
        "## Análisis (desarrollo)\n"
        "## Conclusión (acción recomendada)"
    ),
    OutputStyle.CONVERSATIONAL: (
        "Responde de forma natural y conversacional.\n"
        "Párrafos cortos, tono directo.\n"
        "Sin exceso de formato ni listas innecesarias."
    ),
}


@dataclass
class OutputLayer:
    """
    Layer 4: Formato de salida y restricciones de seguridad.

    Garantiza que el agente produzca outputs en el formato
    correcto y no genere contenido prohibido.
    """
    style: OutputStyle = OutputStyle.DIRECT
    max_length_hint: Optional[str] = None   # ej: "máximo 200 palabras"
    language_hint: str = "mismo idioma que el usuario"
    include_safety_reminder: bool = False   # Solo en casos sensibles

    # Restricciones de output (refuerzo del Layer 0)
    FORBIDDEN_PATTERNS = [
        "preámbulos como 'Basado en', 'Según', 'Puedo ayudarte'",
        "mostrar pensamientos internos o razonamiento paso a paso salvo que se pida",
        "inventar datos, estadísticas o fuentes",
        "cambiar de idioma sin razón",
    ]

    def build(self) -> str:
        """
        Construye el bloque de Layer 4.

        Returns:
            String del layer 4 con instrucciones de formato
        """
        lines = [
            "INSTRUCCIONES DE FORMATO:",
            FORMAT_INSTRUCTIONS[self.style],
        ]

        if self.max_length_hint:
            lines.append(f"Longitud: {self.max_length_hint}")

        lines.append(f"Idioma: {self.language_hint}")

        if self.include_safety_reminder:
            forbidden = "\n".join(f"  ❌ {p}" for p in self.FORBIDDEN_PATTERNS)
            lines.append(f"NO incluyas:\n{forbidden}")

        return "\n".join(lines)

    @classmethod
    def for_code(cls) -> "OutputLayer":
        """Formato para generación de código."""
        return cls(style=OutputStyle.MARKDOWN, max_length_hint="código completo funcional")

    @classmethod
    def for_chat(cls) -> "OutputLayer":
        """Formato para conversación general."""
        return cls(style=OutputStyle.CONVERSATIONAL, max_length_hint="máximo 3 párrafos")

    @classmethod
    def for_validation(cls) -> "OutputLayer":
        """Formato para validación — JSON obligatorio."""
        return cls(style=OutputStyle.JSON)

    @classmethod
    def for_research(cls) -> "OutputLayer":
        """Formato para investigación — reporte estructurado."""
        return cls(style=OutputStyle.REPORT, max_length_hint="máximo 600 palabras")

    @classmethod
    def for_analysis(cls) -> "OutputLayer":
        """Formato para análisis de datos — Markdown con tablas."""
        return cls(style=OutputStyle.MARKDOWN, max_length_hint="máximo 400 palabras")
