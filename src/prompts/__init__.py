#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/prompts/__init__.py
# Autor: SaintWick
# AXIOMA — src/prompts/__init__.py
# Sistema de Prompts v0.1.0 — 6 Layers
# ═══════════════════════════════════════════════════════════════
"""
Sistema de prompts centralizado de AXIOMA.

Layers:
    0. base_prompts        → Identidad y reglas constitucionales
    1. task_prompts        → Clasificación de tarea
    2. tool_prompts        → Herramientas y permisos
    3. context_injection   → Memoria y sesión
    4. output_formats      → Formato de salida
    5. reflection_triggers → Auto-corrección

Uso rápido:
    from src.prompts import get_prompt_manager

    pm = get_prompt_manager("coder")
    system_prompt = pm.build(task_type="CODE_GENERATION")
"""
from .prompt_manager import PromptManager, get_prompt_manager, invalidate_cache
from .base_prompts import IdentityLayer, identity_layer
from .task_prompts import TaskLayer
from .tool_prompts import ToolLayer
from .context_injection import ContextLayer
from .output_formats import OutputLayer, OutputStyle
from .reflection_triggers import ReflectionLayer

__all__ = [
    "PromptManager",
    "get_prompt_manager",
    "invalidate_cache",
    "IdentityLayer",
    "identity_layer",
    "TaskLayer",
    "ToolLayer",
    "ContextLayer",
    "OutputLayer",
    "OutputStyle",
    "ReflectionLayer",
]

PROMPT_SYSTEM_VERSION = "0.1.0"
