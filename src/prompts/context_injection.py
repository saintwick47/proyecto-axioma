#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.1.0 — Layer 3: Context Injection (Memory + Session State)
# Ruta: src/prompts/context_injection.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 3 del sistema de prompts.
Inyecta contexto dinámico: historial de conversación relevante,
resultados de búsqueda semántica en memoria y estado de sesión.

✅ v1.1.0 (FASE 2):
   - Delimitadores XML herméticos (<axioma_context>) para aislar
     las instrucciones del sistema del contenido dinámico.
   - Sanitización de texto para neutralizar intentos de Prompt Injection
     (cierre de etiquetas, comandos de sistema falsos).
"""
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


# Límites de contexto para no exceder ventana del modelo
MAX_HISTORY_ITEMS = 6       # Últimos N mensajes del historial
MAX_MEMORY_ITEMS = 3        # Resultados semánticos más relevantes
MAX_CONTEXT_CHARS = 2000    # Caracteres máximos del bloque de contexto


@dataclass
class ContextLayer:
    """
    Layer 3: Inyección de contexto de memoria y sesión.

    Toma los resultados del MemoryGateway y los formatea
    para inyectarlos eficientemente en el system prompt.

    ✅ v1.1.0: Ahora sanitiza el contenido y lo envuelve en etiquetas
    XML estrictas para prevenir que el LLM confunda el contexto con
    instrucciones directas del sistema.
    """
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    semantic_memories: List[Dict[str, Any]] = field(default_factory=list)
    session_metadata: Dict[str, Any] = field(default_factory=dict)
    search_context: Optional[str] = None  # Resultados de búsqueda web si los hay
    feedback_score: Optional[float] = None       # Score histórico de calidad (0.0-1.0)
    historical_critique: Optional[str] = None    # Crítica del intento anterior si aplica

    # ═══════════════════════════════════════════════════════════════
    # ✅ FIX FASE 2: Sanitización de Inputs (AI Safety)
    # Neutraliza intentos de inyección de prompts dentro del
    # historial o resultados de búsqueda.
    # ═══════════════════════════════════════════════════════════════
    @staticmethod
    def _sanitize_text(text: str) -> str:
        """
        Escapa y neutraliza patrones peligrosos en el texto dinámico.

        Prevención:
        - Cierra etiquetas XML falsas que rompan el delimitador.
        - Reemplaza patrones de override comunes (SYSTEM:, IGNORE PREVIOUS).
        """
        if not text:
            return ""

        # Remover intentos de cierre de etiquetas XML axioma
        sanitized = re.sub(r'<\/?axioma_\w+>', '', text, flags=re.IGNORECASE)

        # Neutralizar intentos de override de instrucciones
        sanitized = re.sub(
            r'\b(SYSTEM|INSTRUCTION|IGNORE\s+PREVIOUS|OVERRIDE)\s*:',
            '[FILTERED]:',
            sanitized,
            flags=re.IGNORECASE
        )

        return sanitized

    def build(self) -> str:
        """
        Construye el bloque de Layer 3.

        Returns:
            String del layer 3 envuelto en delimitadores XML, vacío si no hay contexto.
        """
        parts = []

        # Historial de conversación (últimos N intercambios)
        if self.conversation_history:
            history_block = self._format_history()
            if history_block:
                parts.append(history_block)

        # Memorias semánticas relevantes
        if self.semantic_memories:
            memory_block = self._format_memories()
            if memory_block:
                parts.append(memory_block)

        # Contexto de búsqueda web
        if self.search_context:
            # ✅ FIX FASE 2: Sanitizar contexto de búsqueda externa
            truncated = self._sanitize_text(self.search_context[:600])
            parts.append(f"CONTEXTO DE BÚSQUEDA RECIENTE:\n{truncated}")

        # Metadatos de sesión útiles
        if self.session_metadata:
            meta_block = self._format_session_meta()
            if meta_block:
                parts.append(meta_block)

        # Feedback histórico — orientación al modelo sobre calidad esperada
        if self.feedback_score is not None or self.historical_critique:
            parts.append(self._format_feedback())

        if not parts:
            return ""

        full_context = "\n\n".join(parts)
        # Truncar si excede límite de caracteres
        if len(full_context) > MAX_CONTEXT_CHARS:
            full_context = full_context[:MAX_CONTEXT_CHARS] + "\n[...contexto truncado]"

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Delimitadores Herméticos XML.
        # El LLM tratará todo dentro de estas etiquetas como datos,
        # no como instrucciones de sistema, previniendo Prompt Injection.
        # ═══════════════════════════════════════════════════════════════
        return f"<axioma_context>\n{full_context}\n</axioma_context>"

    def _format_history(self) -> str:
        """
        Formatea el historial de conversación.

        Returns:
            String con historial reciente formateado y sanitizado
        """
        recent = self.conversation_history[-MAX_HISTORY_ITEMS:]
        if not recent:
            return ""
        lines = ["Conversación reciente:"]
        for msg in recent:
            role = msg.get("role", "unknown")
            # ✅ FIX FASE 2: Sanitizar contenido del historial
            content = self._sanitize_text(msg.get("content", ""))[:200]
            prefix = "👤" if role == "user" else "🤖"
            lines.append(f"  {prefix} {role}: {content}")
        return "\n".join(lines)

    def _format_memories(self) -> str:
        """
        Formatea memorias semánticas relevantes.

        Returns:
            String con memorias relevantes sanitizadas
        """
        relevant = self.semantic_memories[:MAX_MEMORY_ITEMS]
        if not relevant:
            return ""
        lines = ["Información relevante de memoria:"]
        for mem in relevant:
            # ✅ FIX FASE 2: Sanitizar contenido de memorias
            content = self._sanitize_text(mem.get("content", mem.get("text", "")))[:150]
            score = mem.get("score", mem.get("similarity", 0))
            if content:
                score_str = f" (relevancia: {score:.2f})" if score else ""
                lines.append(f"  • {content}{score_str}")
        return "\n".join(lines)

    def _format_session_meta(self) -> str:
        """
        Formatea metadatos de sesión útiles.

        Returns:
            String con metadatos de sesión relevantes
        """
        useful_keys = ["session_id", "user_name", "language", "timezone", "previous_task"]
        meta_lines = []
        for key in useful_keys:
            if key in self.session_metadata:
                # ✅ FIX FASE 2: Sanitizar metadatos de sesión
                meta_lines.append(f"  {key}: {self._sanitize_text(str(self.session_metadata[key]))}")
        if not meta_lines:
            return ""
        return "Estado de sesión:\n" + "\n".join(meta_lines)

    def _format_feedback(self) -> str:
        """Formatea el feedback histórico para orientar al modelo."""
        lines = ["Feedback de calidad histórico:"]
        if self.feedback_score is not None:
            label = (
                "Alta" if self.feedback_score >= 0.75
                else "Media" if self.feedback_score >= 0.55
                else "Baja — requiere mejora"
            )
            lines.append(f"  • Score previo: {self.feedback_score:.2f} ({label})")
        if self.historical_critique:
            # ✅ FIX FASE 2: Sanitizar críticas históricas
            truncated = self._sanitize_text(self.historical_critique[:300])
            lines.append(f"  • Crítica: {truncated}")
        return "\n".join(lines)

    @classmethod
    def empty(cls) -> "ContextLayer":
        """Layer de contexto vacío — para primera interacción."""
        return cls()

    @classmethod
    def from_memory_gateway(
        cls,
        history: List[Dict[str, Any]],
        semantic_results: List[Dict[str, Any]],
        session: Dict[str, Any],
        search_ctx: Optional[str] = None,
    ) -> "ContextLayer":
        """
        Factory desde resultados del MemoryGateway.

        Args:
            history: Historial de conversación del gateway
            semantic_results: Resultados de búsqueda semántica
            session: Metadatos de sesión actual
            search_ctx: Contexto de búsqueda web opcional

        Returns:
            ContextLayer configurado con datos del gateway
        """
        return cls(
            conversation_history=history,
            semantic_memories=semantic_results,
            session_metadata=session,
            search_context=search_ctx,
        )

    @classmethod
    def with_feedback(
        cls,
        history: list,
        semantic_results: list,
        session: dict,
        feedback_score: Optional[float] = None,
        historical_critique: Optional[str] = None,
        search_ctx: Optional[str] = None,
    ) -> "ContextLayer":
        """
        Factory con soporte para feedback histórico (Fase 5).

        Args:
            feedback_score:      Score de calidad del prompt anterior (0.0-1.0)
            historical_critique: Crítica del intento anterior
        """
        return cls(
            conversation_history=history,
            semantic_memories=semantic_results,
            session_metadata=session,
            search_context=search_ctx,
            feedback_score=feedback_score,
            historical_critique=historical_critique,
        )

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v1.1.0 (FASE 2)
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Header               | ✅ v1.1.0 changelog (FASE 2)              | Trazabilidad              |
# | build()              | ✅ Delimitadores <axioma_context>         | Prevenir Prompt Injection |
# | _sanitize_text()     | ✅ Nuevo método de sanitización           | Filtrar SYSTEM:, OVERRIDE |
# | _format_*()          | ✅ Aplicar _sanitize_text a inputs        | Prevenir inyecciones      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
