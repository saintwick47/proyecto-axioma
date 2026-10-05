#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v2026 — Settings Computations Mixin
# Ruta: /ruta/a/axioma/config/settings_computations.py
# Autor: SaintWick
# Fecha: 2026-06-13
# Extraído de: config/settings.py (refactorización 50/50)
#
# Propósito:
#   Mixin con toda la lógica computacional de Settings.
#   Métodos que leen campos y devuelven valores derivados.
#
# Relación con settings.py:
#   Settings(BaseSettings, SettingsComputations) hereda este mixin.
# ═══════════════════════════════════════════════════════════════
# ✅ v0.1.5: Agregado get_vision_model_with_fallback()
# ✅ v0.1.6: ELIMINADO soporte moondream:v2 — Solo gemma4:e4b
#            get_vision_model_with_fallback simplificado a modelo único.
# ✅ v0.1.6: FIX getattr() con sintaxis correcta (self, "attr", default)
# ✅ v0.1.6: get_vision_fallback_config() retorna string vacío (sin fallback)
# ✅ v0.1.6: get_timeout_for_model() detecta "gemma4" directamente
# ✅ v0.1.6b: Ajustado a timeout 150s (coincide con settings.py)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Tuple


class SettingsComputations:
    """
    Mixin con métodos computacionales para Settings.
    No define campos Pydantic — solo métodos y @property que
    leen self.field_name definidos en la clase concreta Settings.
    Compatible con herencia múltiple en Pydantic v2.
    """

    # ═══════════════════════════════════════════════════════════
    # === TIMEOUTS — Resolución por escenario/nivel/modelo ===
    # ═══════════════════════════════════════════════════════════

    @property
    def ollama_timeout(self) -> int:
        """Timeout por defecto para compatibilidad."""
        return self.ollama_timeout_standard  # type: ignore[attr-defined]

    # ✅ 2026-08-25: pisos mínimos medidos en hardware real (CPU 88%/GPU 12%).
    # qwen2.5-coder:7b ≈ 30s / qwen3:8b ≈ 37s por generación corta (200 tokens);
    # con un timeout estándar de 45s las llamadas de la UI fallaban con
    # ReadTimeout. Los pisos garantizan que la generación pueda completarse
    # aunque .env configure valores menores. No toca .env.
    MIN_STANDARD_TIMEOUT = 90
    MIN_FIRST_LOAD_TIMEOUT = 120

    def get_timeout_for_scenario(
        self,
        scenario: Literal["standard", "first_load", "vision", "vision_fallback", "embed", "code"],
        is_first_request: bool = False,
    ) -> int:
        """Obtiene timeout según escenario (con pisos mínimos para hardware lento)."""
        if is_first_request or scenario == "first_load":
            return max(self.ollama_timeout_first_load, self.MIN_FIRST_LOAD_TIMEOUT)  # type: ignore[attr-defined]
        elif scenario == "vision":
            return self.ollama_timeout_vision  # type: ignore[attr-defined]
        elif scenario == "vision_fallback":
            return self.ollama_timeout_vision_fallback  # type: ignore[attr-defined]
        elif scenario == "embed":
            return self.ollama_timeout_embed  # type: ignore[attr-defined]
        elif scenario == "code":
            return self.ollama_timeout_code  # type: ignore[attr-defined]
        return max(self.ollama_timeout_standard, self.MIN_STANDARD_TIMEOUT)  # type: ignore[attr-defined]

    def get_timeout_for_code_level(self, level: int) -> int:
        """
        Retorna timeout basado en nivel de código.
        """
        timeouts = {
            1: self.ollama_timeout_code_level_1,  # type: ignore[attr-defined]
            2: self.ollama_timeout_code_level_2,  # type: ignore[attr-defined]
            3: self.ollama_timeout_code_level_3,  # type: ignore[attr-defined]
            4: self.ollama_timeout_code_level_4,  # type: ignore[attr-defined]
        }
        return timeouts.get(level, self.ollama_timeout_code_level_2)  # type: ignore[attr-defined]

    def get_timeout_for_optimizer(self, attempt: int = 1) -> int:
        """Retorna timeout para optimizer loop."""
        if attempt > 1:
            return self.ollama_timeout_optimizer  # type: ignore[attr-defined]
        return self.ollama_timeout_standard  # type: ignore[attr-defined]

    def get_timeout_for_model(self, model_name: str, is_first: bool = False) -> int:
        """
        Obtiene timeout basado en el modelo.
        ✅ v0.1.6: Detecta "gemma4" directamente además de "vl"/"vision".
        """
        model_lower = model_name.lower()
        if "gemma4" in model_lower or "vl" in model_lower or "vision" in model_lower:
            return self.get_timeout_for_scenario("vision", is_first)
        if "embed" in model_lower or "bge" in model_lower:
            return self.get_timeout_for_scenario("embed", is_first)
        if "coder" in model_lower or "code" in model_lower:
            return self.get_timeout_for_scenario("code")
        if is_first:
            return self.get_timeout_for_scenario("first_load")
        return self.get_timeout_for_scenario("standard")

    # ═══════════════════════════════════════════════════════════
    # === MODELOS — Listas y cadenas ===
    # ═══════════════════════════════════════════════════════════

    @property
    def llm_fallback_chain(self) -> List[str]:
        """Cadena de fallback."""
        chain = [self.llm_default_model]  # type: ignore[attr-defined]
        if self.llm_fallback_1:  # type: ignore[attr-defined]
            chain.append(self.llm_fallback_1)  # type: ignore[attr-defined]
        if self.llm_fallback_2:  # type: ignore[attr-defined]
            chain.append(self.llm_fallback_2)  # type: ignore[attr-defined]
        return chain

    @property
    def all_models(self) -> List[str]:
        """Lista completa de modelos."""
        models = [
            self.llm_default_model,  # type: ignore[attr-defined]
            self.llm_code_model,     # type: ignore[attr-defined]
            self.llm_vision_model,   # type: ignore[attr-defined]
            self.llm_embed_model,    # type: ignore[attr-defined]
        ]
        # ✅ v0.1.6: Solo agregar fallback si no está vacío
        fallback = self.llm_vision_fallback_model  # type: ignore[attr-defined]
        if fallback:
            models.append(fallback)
        if self.llm_judge_model:  # type: ignore[attr-defined]
            models.append(self.llm_judge_model)  # type: ignore[attr-defined]
        if self.llm_fallback_1:  # type: ignore[attr-defined]
            models.append(self.llm_fallback_1)  # type: ignore[attr-defined]
        if self.llm_fallback_2:  # type: ignore[attr-defined]
            models.append(self.llm_fallback_2)  # type: ignore[attr-defined]
        return models

    @property
    def critical_models(self) -> List[str]:
        """Modelos esenciales."""
        return [
            self.llm_default_model,  # type: ignore[attr-defined]
            self.llm_code_model,     # type: ignore[attr-defined]
        ]

    @property
    def optional_models(self) -> List[str]:
        """Modelos opcionales."""
        models = [
            self.llm_vision_model,  # type: ignore[attr-defined]
            self.llm_embed_model,   # type: ignore[attr-defined]
        ]
        # ✅ v0.1.6: Solo agregar fallback si no está vacío
        fallback = self.llm_vision_fallback_model  # type: ignore[attr-defined]
        if fallback:
            models.append(fallback)
        if self.llm_judge_model:  # type: ignore[attr-defined]
            models.append(self.llm_judge_model)  # type: ignore[attr-defined]
        return models

    # ═══════════════════════════════════════════════════════════
    # === ✅ v0.1.6: VISION MODEL SYSTEM (GEMMA4 SOLO) ===
    # ═══════════════════════════════════════════════════════════

    def get_vision_model_with_fallback(self) -> Tuple[str, int, bool]:
        """
        Retorna el modelo de visión a usar.
        ✅ v0.1.6: Simplificado para arquitectura de modelo único (gemma4:e4b).
        ✅ v0.1.6: FIX getattr(self, "attr", default) — sintaxis corregida.
        ✅ v0.1.6b: Timeout default 150s (coincide con settings.py).

        Returns:
            Tuple[str, int, bool]: (model_name, timeout, is_fallback)
            - model_name: 'gemma4:e4b' (único modelo válido)
            - timeout: Timeout en segundos (leído de settings)
            - is_fallback: False (siempre principal, no hay segundo modelo)
        """
        selector = getattr(self, "vision_model_selector", "auto")
        principal = getattr(self, "llm_vision_model", "qwen3-vl:4b")
        # ✅ v0.1.6: FIX CRÍTICO — getattr(self, "nombre_attr", default)
        # ✅ v0.1.6b: Default actualizado a 150s (resolución original ~111s)
        timeout_principal = getattr(self, "ollama_timeout_vision", 150)

        # Cualquier selector válido apunta al VLM instalado
        if selector in ["auto", "qwen3-vl:4b"]:
            return principal, timeout_principal, False

        # Fallback por seguridad si el selector es inválido
        return principal, timeout_principal, False

    def get_vision_fallback_config(self) -> Tuple[str, int]:
        """
        Retorna configuración del modelo de fallback de visión.
        ✅ v0.1.6: Devuelve string vacío (sin fallback). Arquitectura de modelo único.
        ✅ v0.1.6: FIX getattr(self, "attr", default) — sintaxis corregida.
        ✅ v0.1.6b: Timeout default 150s.

        Returns:
            Tuple[str, int]: (fallback_model_name, fallback_timeout)
            - fallback_model_name: "" (vacío, sin fallback)
            - fallback_timeout: Timeout mantenido por compatibilidad
        """
        fallback = getattr(self, "llm_vision_fallback_model", "")
        # ✅ v0.1.6: FIX CRÍTICO — getattr(self, "nombre_attr", default)
        timeout = getattr(self, "ollama_timeout_vision_fallback", 150)
        return fallback, timeout

    def get_available_vision_models(self) -> List[str]:
        """
        Retorna lista de modelos de visión disponibles para selector de UI.
        ✅ v0.1.6: Filtra cadenas vacías. Solo gemma4:e4b en práctica.

        Returns:
            List[str]: Lista de nombres de modelos no vacíos
        """
        models = []
        principal = getattr(self, "llm_vision_model", "")
        fallback = getattr(self, "llm_vision_fallback_model", "")
        if principal:
            models.append(principal)
        if fallback:  # Solo agregar si no está vacío
            models.append(fallback)
        return models

    # ═══════════════════════════════════════════════════════════
    # === VERIFICACIÓN DE MODELOS AL INICIO ===
    # ═══════════════════════════════════════════════════════════

    @property
    def should_verify_models_on_startup(self) -> bool:
        return self.verify_models_on_startup  # type: ignore[attr-defined]

    @property
    def startup_model_check_config(self) -> dict:
        return {
            "timeout": self.model_check_timeout,           # type: ignore[attr-defined]
            "retries": self.model_check_retries,           # type: ignore[attr-defined]
            "retry_delay": self.model_check_retry_delay,   # type: ignore[attr-defined]
            "required_models": self.required_models_on_startup,  # type: ignore[attr-defined]
            "on_missing": self.startup_behavior_on_missing_model,  # type: ignore[attr-defined]
        }

    # ═══════════════════════════════════════════════════════════
    # === EVALUATOR-OPTIMIZER — Umbrales de calidad ===
    # ═══════════════════════════════════════════════════════════

    def get_quality_threshold(self, task_type: str) -> float:
        """Obtiene threshold DINÁMICO por tipo de tarea."""
        t = task_type.lower()
        if any(k in t for k in ["code", "debug", "generation"]):
            base_threshold = self.quality_threshold_code  # type: ignore[attr-defined]
        elif any(k in t for k in ["research", "web", "search", "news", "finance", "weather"]):
            base_threshold = self.quality_threshold_research  # type: ignore[attr-defined]
        elif any(k in t for k in ["chat", "time", "quick", "general"]):
            base_threshold = self.quality_threshold_quick  # type: ignore[attr-defined]
        elif any(k in t for k in ["validation", "valid"]):
            base_threshold = self.quality_threshold_validation  # type: ignore[attr-defined]
        else:
            base_threshold = self.quality_threshold_default  # type: ignore[attr-defined]

        # ✅ FIX: eliminado el "ajuste dinámico por modelo juez" — dead code
        # desde v0.6.2 (juez externo eliminado, llm_judge_model siempre "",
        # ninguna de las dos condiciones podía disparar nunca).
        return base_threshold

    def get_code_quality_threshold(self, level: int) -> float:
        """
        Retorna umbral de calidad basado en nivel de código.
        """
        thresholds = {
            1: self.code_quality_level_1,  # type: ignore[attr-defined]
            2: self.code_quality_level_2,  # type: ignore[attr-defined]
            3: self.code_quality_level_3,  # type: ignore[attr-defined]
            4: self.code_quality_level_4,  # type: ignore[attr-defined]
        }
        return thresholds.get(level, self.code_quality_level_2)  # type: ignore[attr-defined]

    def get_max_attempts(self, task_type: str) -> int:
        """Obtiene máximo de intentos."""
        t = task_type.lower()
        limits = {
            "code": 3, "debug": 3, "research": 2, "web": 2, "analysis": 2,
            "chat": 1, "time": 1, "weather": 1, "quick": 1, "validation": 2,
        }
        for key, limit in limits.items():
            if key in t:
                return min(limit, self.max_optimizer_attempts)  # type: ignore[attr-defined]
        return min(2, self.max_optimizer_attempts)  # type: ignore[attr-defined]

    # ═══════════════════════════════════════════════════════════
    # === LÍMITES DE CÓDIGO — Nivel por líneas ===
    # ═══════════════════════════════════════════════════════════

    def get_code_level(self, estimated_lines: int) -> int:
        """
        Determina nivel de código basado en líneas estimadas.
        """
        if estimated_lines <= 30:
            return 1
        elif estimated_lines <= 100:
            return 2
        elif estimated_lines <= 200:
            return 3
        return 4

    # ═══════════════════════════════════════════════════════════
    # === AGENT LOOP — Vista de configuración ===
    # ═══════════════════════════════════════════════════════════

    @property
    def agent_loop_config(self) -> Dict[str, Any]:
        """Retorna configuración completa para Agent Loop."""
        return {
            "max_iterations": self.agent_loop_max_iterations,       # type: ignore[attr-defined]
            "timeout_per_step": self.agent_loop_timeout_per_step,   # type: ignore[attr-defined]
            "global_timeout": self.agent_loop_global_timeout,       # type: ignore[attr-defined]
            "enable_reflection": self.agent_loop_enable_reflection, # type: ignore[attr-defined]
            "quality_threshold": self.agent_loop_quality_threshold, # type: ignore[attr-defined]
            "max_consecutive_failures": self.agent_loop_max_consecutive_failures,  # type: ignore[attr-defined]
            "max_reasoning_tokens": self.max_reasoning_tokens,      # type: ignore[attr-defined]
            "dynamic_context_threshold": self.dynamic_context_threshold,  # type: ignore[attr-defined]
            "barge_in_timeout_ms": self.barge_in_timeout_ms,        # type: ignore[attr-defined]
        }

    # ═══════════════════════════════════════════════════════════
    # === MCP + TOOL ARCHITECTURE — Permisos y parseo ===
    # ═══════════════════════════════════════════════════════════

    def get_tool_permission_for_agent(self, agent_type: str) -> int:
        """
        Retorna el nivel de ToolPermission configurado para un tipo de agente.
        """
        permission_map = {
            "coder":      self.tool_permission_coder,      # type: ignore[attr-defined]
            "researcher": self.tool_permission_researcher,  # type: ignore[attr-defined]
            "analyst":    self.tool_permission_analyst,     # type: ignore[attr-defined]
            "validator":  self.tool_permission_validator,   # type: ignore[attr-defined]
            "searcher":   self.tool_permission_searcher,    # type: ignore[attr-defined]
        }
        return permission_map.get(
            agent_type.lower(),
            self.tool_permission_default,  # type: ignore[attr-defined]
        )

    def parse_mcp_servers(self) -> List[Dict[str, str]]:
        """
        Parsea la lista mcp_servers al formato estructurado.
        """
        result: List[Dict[str, str]] = []
        for entry in self.mcp_servers:  # type: ignore[attr-defined]
            parts = entry.split(":", 2)
            if len(parts) < 3:
                logging.warning(
                    f"[Settings] Entrada MCP inválida: {entry!r}"
                )
                continue
            label, transport, command_or_url = parts
            if transport not in ("stdio", "http"):
                logging.warning(
                    f"[Settings] Transporte MCP inválido '{transport}' en entrada: {entry!r}. "
                    "Usar 'stdio' o 'http'."
                )
                continue
            result.append({
                "label": label.strip(),
                "transport": transport.strip(),
                "command_or_url": command_or_url.strip(),
            })
        return result

    @property
    def mcp_config(self) -> Dict[str, Any]:
        """Configuración completa del subsistema MCP."""
        return {
            "timeout": self.mcp_timeout,                 # type: ignore[attr-defined]
            "connect_timeout": self.mcp_connect_timeout, # type: ignore[attr-defined]
            "max_reconnect_attempts": self.mcp_max_reconnect_attempts,  # type: ignore[attr-defined]
            "auto_connect": self.mcp_auto_connect,       # type: ignore[attr-defined]
            "servers_configured": len(self.mcp_servers), # type: ignore[attr-defined]
            "executor_max_retries": self.tool_executor_max_retries,   # type: ignore[attr-defined]
            "executor_retry_delay": self.tool_executor_retry_delay,   # type: ignore[attr-defined]
            "permissions": {
                "coder":      self.tool_permission_coder,      # type: ignore[attr-defined]
                "researcher": self.tool_permission_researcher,  # type: ignore[attr-defined]
                "analyst":    self.tool_permission_analyst,     # type: ignore[attr-defined]
                "validator":  self.tool_permission_validator,   # type: ignore[attr-defined]
                "searcher":   self.tool_permission_searcher,    # type: ignore[attr-defined]
                "default":    self.tool_permission_default,     # type: ignore[attr-defined]
            },
        }

    # ═══════════════════════════════════════════════════════════
    # === SANDBOX — Vista de configuración ===
    # ═══════════════════════════════════════════════════════════

    @property
    def sandbox_config(self) -> Dict[str, Any]:
        """Configuración completa del subsistema Sandbox."""
        return {
            "timeout_seconds": self.sandbox_timeout_seconds,       # type: ignore[attr-defined]
            "max_cpu_seconds": self.sandbox_max_cpu_seconds,       # type: ignore[attr-defined]
            "max_memory_mb": self.sandbox_max_memory_mb,           # type: ignore[attr-defined]
            "max_output_bytes": self.sandbox_max_output_bytes,     # type: ignore[attr-defined]
            "max_file_size_bytes": self.sandbox_max_file_size_bytes,  # type: ignore[attr-defined]
            "work_dir": self.sandbox_work_dir,                     # type: ignore[attr-defined]
            "allowed_commands": self.sandbox_allowed_commands,     # type: ignore[attr-defined]
            "blocked_commands": self.sandbox_blocked_commands,     # type: ignore[attr-defined]
            "blocked_env_vars": self.sandbox_blocked_env_vars,     # type: ignore[attr-defined]
            "mode": self.sandbox_mode,                             # type: ignore[attr-defined]
            "enable_network": self.sandbox_enable_network,         # type: ignore[attr-defined]
            "enable_file_write": self.sandbox_enable_file_write,   # type: ignore[attr-defined]
        }

    # ═══════════════════════════════════════════════════════════
    # === EMBEDDING OPTIMIZER — Vista de configuración ===
    # ═══════════════════════════════════════════════════════════

    @property
    def embedding_optimizer_config(self) -> Dict[str, Any]:
        """Configuración completa del EmbeddingOptimizer."""
        return {
            "embedding_cache_size": self.embedding_cache_max_size,      # type: ignore[attr-defined]
            "embedding_cache_ttl": self.embedding_cache_ttl_seconds,    # type: ignore[attr-defined]
            "query_cache_size": self.query_cache_max_size,              # type: ignore[attr-defined]
            "query_cache_ttl": self.query_cache_ttl_seconds,            # type: ignore[attr-defined]
            "batch_size": self.embedding_batch_size,                    # type: ignore[attr-defined]
            "max_workers": self.embedding_max_workers,                  # type: ignore[attr-defined]
            "similarity_percentile": self.similarity_percentile,        # type: ignore[attr-defined]
            "similarity_min_threshold": self.similarity_min_threshold,  # type: ignore[attr-defined]
        }

    # ═══════════════════════════════════════════════════════════
    # === JARVIS MODE — Vista de configuración ===
    # ═══════════════════════════════════════════════════════════

    @property
    def jarvis_config(self) -> Dict[str, Any]:
        """
        Configuración completa del Modo Jarvis para consumo por
        VoicePipeline, Dispatcher y SystemAgent sin leer campos uno a uno.
        """
        return {
            "default_mode":                self.jarvis_mode_default,                  # type: ignore[attr-defined]
            "wake_word":                   self.jarvis_wake_word,                     # type: ignore[attr-defined]
            "response_max_tokens":         self.jarvis_response_max_tokens,           # type: ignore[attr-defined]
            "tts_auto_speak":              self.jarvis_tts_auto_speak,                # type: ignore[attr-defined]
            "voice_confidence_threshold":  self.jarvis_voice_confidence_threshold,    # type: ignore[attr-defined]
            "screen_describe_on_query":    self.jarvis_screen_describe_on_query,      # type: ignore[attr-defined]
            "autonomous_task_max_steps":   self.jarvis_autonomous_task_max_steps,     # type: ignore[attr-defined]
            "autonomous_task_timeout":     self.jarvis_autonomous_task_timeout,       # type: ignore[attr-defined]
            "gui_indicator":               self.jarvis_gui_indicator,                 # type: ignore[attr-defined]
        }

    def get_jarvis_response_config(self, mode: str) -> Dict[str, Any]:
        """
        Retorna parámetros de generación adaptados al modo Jarvis activo.

        Args:
            mode: "normal" | "jarvis" | "voice_only" | "silent"

        Returns:
            dict con max_tokens, tts_enabled, gui_output para ese modo.
        """
        base: Dict[str, Any] = {
            "max_tokens": self.jarvis_response_max_tokens,  # type: ignore[attr-defined]
            "tts_enabled": self.jarvis_tts_auto_speak,       # type: ignore[attr-defined]
            "gui_output":  True,
        }
        if mode == "normal":
            base["max_tokens"] = 4096   # sin restricción Jarvis
            base["tts_enabled"] = False
        elif mode == "jarvis":
            pass  # valores por defecto de settings
        elif mode == "voice_only":
            base["gui_output"] = False
        elif mode == "silent":
            base["tts_enabled"] = False
        return base

    # ═══════════════════════════════════════════════════════════
    # === VISION CONFIG — Vista de configuración ===
    # ═══════════════════════════════════════════════════════════

    @property
    def vision_config(self) -> Dict[str, Any]:
        """
        Configuración completa del subsistema de visión para consumo
        por VisionAgent y Dispatcher sin leer campos uno a uno.
        """
        return {
            "principal_model":           self.llm_vision_model,              # type: ignore[attr-defined]
            "fallback_model":            self.llm_vision_fallback_model,     # type: ignore[attr-defined]
            "selector":                  self.vision_model_selector,         # type: ignore[attr-defined]
            "timeout_principal":         self.ollama_timeout_vision,         # type: ignore[attr-defined]
            "timeout_fallback":          self.ollama_timeout_vision_fallback, # type: ignore[attr-defined]
            "capture_enabled":           self.enable_screen_capture,         # type: ignore[attr-defined]
            "capture_backend":           self.vision_capture_backend,        # type: ignore[attr-defined]
            "capture_max_width":         self.vision_capture_max_width,      # type: ignore[attr-defined]
            "capture_max_height":        self.vision_capture_max_height,     # type: ignore[attr-defined]
            "jpeg_quality":              self.vision_jpeg_quality,           # type: ignore[attr-defined]
            "context_hint_enabled":      self.vision_context_hint_enabled,   # type: ignore[attr-defined]
            "response_cache_ttl":        self.vision_response_cache_ttl,     # type: ignore[attr-defined]
        }
