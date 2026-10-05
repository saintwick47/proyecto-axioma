#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — coder_prompts.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/agents/coder_prompts.py
#
# ✅ v0.6.9v (REFACTOR): construcción de prompts (usuario + system) extraída
#   de src/agents/coder.py (el original queda intacto).
#
#   Qué se movió desde coder.py (sin cambios de comportamiento):
#     - `_build_prompt`               → prompt de usuario (contexto + instrucciones)
#     - `_build_coder_system_prompt`  → system prompt vía PromptManager/ContextLayer
#     - `_build_system_prompt`        → system prompt con fallback seguro
#
#   `CoderAgent` hereda ahora `CoderPromptMixin`, así la API pública
#   (self._build_prompt, self._build_system_prompt, …) no cambia.
#
#   Los helpers de módulo (_build_repo_context, CONTEXT_LAYER_AVAILABLE,
#   ContextLayer) viven en coder.py y se importan DENTRO de los métodos
#   (deferred) para evitar el ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.domain.entities import Message
from src.domain.types import TaskType

logger = logging.getLogger(__name__)


class CoderPromptMixin:
    """Construcción de prompts (usuario y system) para el CoderAgent.

    Extraído de CoderAgent (50/50): este mixin se ocupa de armar el prompt de
    usuario (con contexto de memoria, instrucciones por task_type y contexto de
    búsqueda web) y el system prompt (vía PromptManager con fallback seguro).
    El estado del agente (self.language, self._prompt_manager, self.config) lo
    define CoderAgent.
    """

    def _build_prompt(
        self,
        input_msg: Message,
        task_type: str,
        previous_critique: Optional[str] = None,
        attempt: int = 1,
        ambiguity_level: str = "none",
        rewritten_query: Optional[str] = None,
        search_context: Optional[str] = None,
        session_id: Optional[str] = None,  # ✅ plan_memory (B): memoria por sesión
        user_id: Optional[str] = None,    # ✅ UI de usuarios (2026-08-31): identidad
    ) -> str:
        """Construye el prompt de usuario."""
        context = self._get_context(
            limit=8,
            task_type=task_type,
            query=input_msg.content,
            max_tokens=self._get_max_tokens(task_type),
            session_id=session_id,  # ✅ plan_memory (B)
            user_id=user_id,  # ✅ UI de usuarios
        )
        context_text = (
            "\n".join([f"{m['role']}: {m['content'][:200]}" for m in context])
            if context else ""
        )
        instructions = {
            TaskType.CODE_GENERATION.value:   "Genera código completo y funcional. Sé conciso.",
            TaskType.CODE_DEBUGGING.value:    "Identifica y corrige el error. Explicación breve.",
            TaskType.CODE_EXPLANATION.value:  "Explica el código claramente. Máximo 3 párrafos.",
            TaskType.CODE_REVIEW.value:       "Revisa y sugiere mejoras. Prioriza lo crítico.",
            TaskType.CODE_OPTIMIZATION.value: "Optimiza el rendimiento. Muestra solo cambios clave.",
        }
        base_query = rewritten_query or input_msg.content
        prompt = (
            f"{instructions.get(task_type, 'Genera código. Sé conciso.')}\n"
            + (f"Contexto:\n{context_text}\n" if context_text else "")
            + f"Entrada: {base_query}\n"
            + (f"Lenguaje: {self.language}\n" if self.language else "")
        )
        if search_context:
            ctx_limit = min(len(search_context), 2000)
            prompt += (
                f"\n---\nCONTEXTO DE BÚSQUEDA WEB:\n{search_context[:ctx_limit]}\n"
                "Usa este contexto como referencia."
            )
        # ✅ v0.6.9a-F (corrección): la crítica se aplica SIEMPRE que venga
        # (no solo attempt > 1). Un critique EXTERNO sembrado por el caller
        # (refinamiento /plan F, o el repair de Fase 4 de _handle_code) llega
        # con attempt=1 — antes se descartaba y la regeneración salía
        # idéntica a la original (por eso el repair no progresaba).
        if previous_critique:
            prompt += f"\n---\n{previous_critique}\nAplica TODAS las mejoras anteriores."

        # ✅ PLAN v2.0 (Fase 4): repo-map — CONTEXTO DEL REPO antes del return.
        # Solo si settings.repo_map_enabled (default False); fallback silencioso.
        if settings.repo_map_enabled:
            from .coder import _build_repo_context
            repo_context = _build_repo_context(base_query)
            if repo_context:
                prompt += f"\n---\n{repo_context}"

        return prompt + "\nRespuesta:"

    def _build_coder_system_prompt(
        self,
        task_type: str,
        history: Optional[List[Dict[str, Any]]] = None,
        search_context: Optional[str] = None,
        is_retry: bool = False,
        quality_score: float = 0.0,
        quality_critique: Optional[str] = None,
    ) -> str:
        """Construye system prompt usando PromptManager con fallback seguro."""
        if not self._prompt_manager:
            return self.config.system_prompt or self.DEFAULT_SYSTEM_PROMPT

        context = None
        from .coder import CONTEXT_LAYER_AVAILABLE, ContextLayer
        if CONTEXT_LAYER_AVAILABLE and ContextLayer is not None:
            try:
                context = ContextLayer(
                    conversation_history=history or [],
                    search_context=search_context,
                )
            except Exception as e:
                logger.warning(f"CoderAgent: ContextLayer init failed: {e}")

        return self._build_system_prompt(
            task_type=task_type, context=context, is_retry=is_retry,
            previous_score=quality_score, critique=quality_critique or "",
        )

    def _build_system_prompt(
        self,
        task_type: str,
        context: Any = None,
        is_retry: bool = False,
        previous_score: float = 0.0,
        critique: str = "",
    ) -> str:
        """
        Construye system prompt vía PromptManager (6 layers: identidad,
        tarea, herramientas, contexto, formato, reflexión), con fallback
        seguro al prompt estático si el manager no está disponible o falla.

        ✅ FIX AUDITORÍA: antes este método ignoraba TODOS sus parámetros
        (task_type, context, is_retry, previous_score, critique) y devolvía
        siempre self.config.system_prompt/DEFAULT_SYSTEM_PROMPT sin importar
        qué le pasaran. _build_coder_system_prompt() armaba un ContextLayer
        real y consultaba self._prompt_manager para nada — el resultado se
        descartaba acá. El PromptManager nunca llegaba a construir un
        prompt real; CoderAgent siempre usaba el mismo prompt estático,
        sin contexto de conversación/búsqueda ni crítica de reintento.
        """
        if self._prompt_manager is None:
            return self.config.system_prompt or self.DEFAULT_SYSTEM_PROMPT

        try:
            if is_retry and critique:
                return self._prompt_manager.build_for_retry(
                    task_type=task_type,
                    previous_score=previous_score,
                    critique=critique,
                    context=context,
                )
            return self._prompt_manager.build(
                task_type=task_type,
                context=context,
            )
        except Exception as e:
            logger.warning(
                f"CoderAgent: PromptManager.build() falló, fallback a prompt estático: {e}"
            )
            return self.config.system_prompt or self.DEFAULT_SYSTEM_PROMPT
