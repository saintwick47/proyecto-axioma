#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_tools.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_tools.py
#
# 2026-10-13: dominio **AGENT LOOP ... RUNTIME MODE** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


class SettingsTools(BaseModel):
    """Dominio AGENT LOOP ... RUNTIME MODE extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === AGENT LOOP — v0.3.0 ===
    # ═══════════════════════════════════════════════════════════
    agent_loop_max_iterations: int = Field(default=10, ge=1, le=50)
    agent_loop_timeout_per_step: int = Field(default=30, ge=10, le=120)
    agent_loop_global_timeout: int = Field(default=180, ge=60, le=600)
    agent_loop_enable_reflection: bool = Field(default=True)
    agent_loop_quality_threshold: float = Field(default=0.80, ge=0.50, le=1.0)
    agent_loop_max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    max_reasoning_tokens: int = Field(
        default=2048, ge=512, le=4096,
        description="Máximo de tokens de razonamiento por iteración del AgentLoop (Ryzen 5 8500G)"
    )
    dynamic_context_threshold: float = Field(
        default=0.95, ge=0.85, le=0.99,
        description="Umbral dinámico de similitud para ContextWindowManager (floor=0.85)"
    )
    barge_in_timeout_ms: int = Field(
        default=50, ge=20, le=200,
        description="Timeout en ms para detección de barge-in en VoicePipeline"
    )

    # ═══════════════════════════════════════════════════════════
    # === MCP + TOOL ARCHITECTURE — v0.4.0 ===
    # ═══════════════════════════════════════════════════════════
    tool_permission_coder: int = Field(default=2, ge=0, le=4)
    tool_permission_researcher: int = Field(default=3, ge=0, le=4)
    tool_permission_analyst: int = Field(default=0, ge=0, le=4)
    tool_permission_validator: int = Field(default=0, ge=0, le=4)
    tool_permission_searcher: int = Field(default=3, ge=0, le=4)
    tool_permission_default: int = Field(default=2, ge=0, le=4)
    mcp_timeout: float = Field(default=30.0, ge=5.0, le=120.0)
    mcp_connect_timeout: float = Field(default=10.0, ge=3.0, le=30.0)
    mcp_max_reconnect_attempts: int = Field(default=3, ge=1, le=10)
    mcp_servers: List[str] = Field(default_factory=list)
    mcp_auto_connect: bool = Field(default=False)
    tool_executor_max_retries: int = Field(default=0, ge=0, le=3)
    tool_executor_retry_delay: float = Field(default=1.0, ge=0.5, le=5.0)

    # ═══════════════════════════════════════════════════════════
    # === SANDBOX EXECUTION — v0.4.1 ===
    # ═══════════════════════════════════════════════════════════
    sandbox_max_cpu_seconds: int = Field(default=60, ge=10, le=300)
    sandbox_max_memory_mb: int = Field(default=256, ge=64, le=2048)
    sandbox_max_output_bytes: int = Field(default=100000, ge=10000, le=1000000)
    sandbox_max_file_size_bytes: int = Field(default=10000000, ge=1000000, le=100000000)
    sandbox_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    sandbox_work_dir: Optional[str] = Field(default=None)
    sandbox_allowed_commands: List[str] = Field(default_factory=list)
    sandbox_blocked_commands: List[str] = Field(
        default_factory=lambda: [
            "rm -rf", "mkfs", "dd if=", ":(){ :|:& };:", "chmod -R 777",
            "> /dev/sda", "shutdown", "reboot", "halt", "poweroff",
            "sudo", "su ", "pkexec", "doas",
            "curl", "wget", "nc ", "netcat", "ssh", "scp", "sftp",
            "python", "python3", "pip", "pip3",
            "node", "npm", "ruby", "gem",
            "docker", "kubectl", "podman", "containerd",
        ],
    )
    sandbox_blocked_env_vars: List[str] = Field(
        default_factory=lambda: [
            "AWS_SECRET", "AWS_ACCESS_KEY", "DATABASE_URL",
            "PRIVATE_KEY", "SECRET_KEY", "API_KEY",
            "PASSWORD", "TOKEN", "CREDENTIAL",
        ],
    )
    sandbox_mode: Literal["strict", "relaxed", "disabled"] = Field(default="strict")
    sandbox_enable_network: bool = Field(default=False)
    sandbox_enable_file_write: bool = Field(default=False)

    # ═══════════════════════════════════════════════════════════
    # === VRAM LOCK — timeout de espera (AUD-FT-001) ===
    # Piso para esperar la liberación del candado de VRAM (RAC) antes
    # de un swap. Config-driven (antes era un literal 300.0 hardcodeado
    # en model_swap.py::_check_vram_lock).
    # ═══════════════════════════════════════════════════════════
    vram_lock_wait_timeout: float = Field(
        default=300.0, ge=60.0, le=1800.0,
        description="Piso (segundos) de espera por el candado de VRAM antes de un swap de modelo",
    )

    # ═══════════════════════════════════════════════════════════
    # === RUNTIME MODE — FASE 1 (plan_harness) ===
    # Modo de ejecución del sistema: acota el toolset disponible por
    # sesión vía RuntimeMode (src/domain/types.py). STANDARD = toolset
    # completo (default). Ver ToolExecutor paso 2.5 y
    # ToolRegistry.list_for_mode().
    # ═══════════════════════════════════════════════════════════
    runtime_mode: Literal["standard", "minimal", "safe_mode", "code_mode"] = Field(
        default="standard",
        description="Modo de ejecución del sistema (standard|minimal|safe_mode|code_mode)",
    )

    @field_validator("sandbox_blocked_commands", mode="before")
    @classmethod
    def validate_blocked_commands(cls, v: Union[List[str], str, None]) -> List[str]:
        """Valida que la lista de comandos bloqueados sea una lista de strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [cmd.strip() for cmd in v.split(",") if cmd.strip()]
        return [str(cmd).strip() for cmd in v if str(cmd).strip()]
