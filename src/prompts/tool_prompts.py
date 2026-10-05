#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Layer 2: Tool Selection & Permission Checks
# Ruta: src/prompts/tool_prompts.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 2 del sistema de prompts.
Informa al agente qué herramientas tiene disponibles,
cuáles requieren confirmación y cuáles están bloqueadas.
"""
from dataclasses import dataclass, field
from typing import List, Optional


# Descripciones de herramientas disponibles en AXIOMA
TOOL_DESCRIPTIONS: dict = {
    "web_search":      "Búsqueda web en tiempo real",
    "news_api":        "Noticias recientes vía NewsAPI",
    "doc_search":      "Búsqueda en documentación técnica",
    "cache_lookup":    "Consulta caché de respuestas previas",
    "memory_semantic": "Búsqueda semántica en memoria de largo plazo",
    "syntax_checker":  "Validación de sintaxis de código",
    "code_executor":   "Ejecución de código en entorno controlado",
    "data_processor":  "Procesamiento y análisis de datos",
    "chart_generator": "Generación de gráficos y visualizaciones",
    "calculator":      "Cálculos matemáticos precisos",
    "file_read":       "Lectura de archivos del sistema",
    "file_write":      "Escritura de archivos (requiere confirmación)",
    "file_delete":     "Eliminación de archivos (BLOQUEADO por defecto)",
}


@dataclass
class ToolLayer:
    """
    Layer 2: Herramientas disponibles y permisos.

    Define claramente al agente qué puede usar, qué necesita
    confirmación y qué está completamente bloqueado.
    """
    allowed: List[str] = field(default_factory=list)
    requires_confirmation: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    active_tools: List[str] = field(default_factory=list)  # herramientas realmente disponibles

    def build(self) -> str:
        """
        Construye el bloque de Layer 2.

        Returns:
            String del layer 2 con contexto de herramientas
        """
        lines = ["HERRAMIENTAS DISPONIBLES:"]

        if self.allowed:
            tools_desc = []
            for t in self.allowed:
                desc = TOOL_DESCRIPTIONS.get(t, t)
                available = "✅" if (not self.active_tools or t in self.active_tools) else "⚠️ (no inicializada)"
                tools_desc.append(f"  {available} {t}: {desc}")
            lines.append("Permitidas:\n" + "\n".join(tools_desc))

        if self.requires_confirmation:
            conf_list = ", ".join(self.requires_confirmation)
            lines.append(f"Requieren confirmación del usuario: {conf_list}")

        if self.blocked:
            block_list = ", ".join(self.blocked)
            lines.append(f"❌ BLOQUEADAS (no usar): {block_list}")

        if not self.allowed and not self.active_tools:
            lines.append("  (Sin herramientas externas — responde con conocimiento propio)")

        return "\n".join(lines)

    @classmethod
    def minimal(cls) -> "ToolLayer":
        """Layer de herramientas mínimo — solo conocimiento del modelo."""
        return cls()

    @classmethod
    def for_coder(cls) -> "ToolLayer":
        """Configuración de herramientas para CoderAgent."""
        return cls(
            allowed=["syntax_checker", "code_executor"],
            requires_confirmation=["file_write"],
            blocked=["file_delete", "system_commands"],
        )

    @classmethod
    def for_searcher(cls) -> "ToolLayer":
        """Configuración de herramientas para SearcherAgent."""
        return cls(
            allowed=["web_search", "news_api", "cache_lookup"],
            requires_confirmation=[],
            blocked=["file_delete", "code_executor"],
        )

    @classmethod
    def for_researcher(cls) -> "ToolLayer":
        """Configuración de herramientas para ResearcherAgent."""
        return cls(
            allowed=["web_search", "news_api", "doc_search", "memory_semantic", "cache_lookup"],
            requires_confirmation=[],
            blocked=["file_delete", "code_executor"],
        )

    @classmethod
    def for_analyst(cls) -> "ToolLayer":
        """Configuración de herramientas para AnalystAgent."""
        return cls(
            allowed=["data_processor", "chart_generator", "memory_semantic", "calculator"],
            requires_confirmation=[],
            blocked=["file_delete", "system_commands"],
        )

    @classmethod
    def for_validator(cls) -> "ToolLayer":
        """Configuración de herramientas para ValidatorAgent."""
        return cls(
            allowed=["syntax_checker"],
            requires_confirmation=[],
            blocked=["file_write", "file_delete", "code_executor"],
        )
