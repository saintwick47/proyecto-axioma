#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# sandbox.py - Sandbox Executor (Ejecución Segura de Código)
# Ruta: /ruta/a/axioma/src/tools_mcp/sandbox.py
# Autor: SaintWick
# Versión: 0.5.5
#
# Ejecuta código y comandos en entorno aislado con límites de recursos
# (CPU, memoria, tiempo, output), sin shell=True y con sanitización
# de salida. Soporta staging de archivos, ejecución de scripts con
# intérprete configurable y auditoría integrada. Modos STRICT/RELAXED.
# ═══════════════════════════════════════════════════════════════
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de MOTOR DE EJECUCIÓN
#    (SandboxResult, SandboxMode, execute_command, _prepare_env,
#    _sanitize_output, _execute_with_limits y los helpers de eventos/log)
#    vive ahora en el hermano `sandbox_execution.py`. Acá QUEDAN la
#    configuración/política (SandboxConfig + listas por defecto), el
#    CONFINAMIENTO (_can_confine/_raise_if_cannot_confine, también
#    marcadores del auditor por FUENTE), la fachada pública
#    (execute_python/execute_script), las estadísticas y las factorías.
#    Los nombres movidos se re-importan con los MISMOS nombres (mismo
#    objeto ⇒ API pública intacta) y SandboxExecutor hereda del mixin.

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import contextmanager

# ── Import desde sandbox_staging (refactorización 50/50) ────────
# Mantenemos la importación para compatibilidad con código externo
# que importe StagingFS/gets_staging desde sandbox.py
from .sandbox_staging import StagingEntry, StagingFS, get_staging  # noqa: F401 — re-export

# ── Import desde sandbox_execution (refactorización 50/50) ──────
# Nombres movidos al hermano, re-importados con los MISMOS nombres:
# un solo objeto ⇒ la API pública (y `from ...sandbox import X`) no cambia.
# `_publish_sandbox_unavailable` y `_SANDBOX_UNAVAILABLE_AVAILABLE` los usa
# _raise_if_cannot_confine (fail-closed), que se queda acá.
from .sandbox_execution import (
    SandboxExecutionMixin,
    SandboxMode,
    SandboxResult,
    SandboxUnavailableError,
    _publish_sandbox_unavailable,
    _SANDBOX_UNAVAILABLE_AVAILABLE,
)

logger = logging.getLogger(__name__)

# ── Import seguro de settings (graceful fallback) ────────────────
# ✅ NUEVO (hallazgo agente filesystem v1.6.0): mismo patrón try/except
# que _PERMISSIONS_AVAILABLE en tool_executor.py — sandbox.py no
# dependía de settings.py antes, y no queremos romper contextos donde
# settings no está disponible (tests unitarios aislados, scripts sueltos).
try:
    from config.settings import settings as _settings
    _SETTINGS_AVAILABLE = True
except ImportError:
    _settings = None
    _SETTINGS_AVAILABLE = False


def _default_blocked_commands() -> List[str]:
    """
    ✅ FIX (hallazgo agente filesystem v1.6.0): settings.py define
    sandbox_blocked_commands (con su propio validator y warning de
    startup si está vacío — ver config/settings.py) pero
    get_sandbox_executor() nunca la leía: SandboxConfig.blocked_commands
    era una lista hardcodeada acá, completamente desconectada. El
    warning de startup era efectivamente un placebo — cambiar la config
    no cambiaba nada en runtime.

    Unión (no reemplazo) de ambas listas: settings.sandbox_blocked_commands
    tiene entradas que esta lista no tenía (fork bomb ':(){ :|:& };:',
    '> /dev/sda', 'pkexec', 'doas', 'sftp', 'containerd'). Es unión y no
    reemplazo a propósito — solo puede volver el chequeo MÁS estricto
    nunca menos, así que no hay riesgo de regresión funcional sobre lo
    que ya se bloqueaba. is_command_allowed() hace substring match
    (`blocked.lower() in cmd_lower`), así que entradas de un solo token
    ("rm") y de frase ("rm -rf") conviven sin conflicto.
    """
    base = [
        "rm", "dd", "mkfs", "chmod", "chown", "sudo", "su",
        "shutdown", "reboot", "halt", "poweroff",
        "curl", "wget", "nc", "netcat", "ssh", "scp",
        "python", "python3", "pip", "pip3",
        "node", "npm", "ruby", "gem",
        "docker", "kubectl", "podman",
        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Shells interactivos bloqueados.
        # Previene ejecución arbitraria vía `bash -c "rm -rf /"`.
        # ═══════════════════════════════════════════════════════════════
        "sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh",
    ]
    if _SETTINGS_AVAILABLE and _settings and _settings.sandbox_blocked_commands:
        extra = [c for c in _settings.sandbox_blocked_commands if c not in base]
        return base + extra
    return base


def _default_blocked_env_vars() -> List[str]:
    """✅ FIX (hallazgo agente filesystem v1.6.0): mismo caso que
    _default_blocked_commands() pero para blocked_env_vars — unión con
    settings.sandbox_blocked_env_vars."""
    base = [
        "AWS_SECRET", "AWS_ACCESS_KEY", "DATABASE_URL",
        "PRIVATE_KEY", "SECRET_KEY", "API_KEY",
        "PASSWORD", "TOKEN", "CREDENTIAL",
    ]
    if _SETTINGS_AVAILABLE and _settings and _settings.sandbox_blocked_env_vars:
        extra = [v for v in _settings.sandbox_blocked_env_vars if v not in base]
        return base + extra
    return base


@dataclass
class SandboxConfig:
    """
    Configuración de límites y restricciones del sandbox.

    Atributos:
        timeout_seconds       : Timeout máximo de ejecución
        max_cpu_seconds       : Límite de tiempo de CPU
        max_memory_mb         : Límite de memoria en MB
        max_output_bytes      : Máximo de bytes en stdout/stderr
        max_file_size_bytes   : Máximo tamaño de archivo creado
        allowed_commands      : Lista blanca de comandos permitidos
        blocked_commands      : Lista negra de comandos bloqueados
        blocked_env_vars      : Variables de entorno bloqueadas
        work_dir              : Directorio de trabajo (None = tmp auto)
        mode                  : Modo de ejecución (STRICT/RELAXED/DISABLED)
        enable_network        : Si permite acceso a red (default: False)
        enable_file_write     : Si permite escritura en filesystem (default: False)
    """
    timeout_seconds: float = 30.0
    max_cpu_seconds: float = 25.0
    max_memory_mb: int = 256
    max_output_bytes: int = 100000
    max_file_size_bytes: int = 10000000
    allowed_commands: List[str] = field(default_factory=list)
    blocked_commands: List[str] = field(default_factory=_default_blocked_commands)
    blocked_env_vars: List[str] = field(default_factory=_default_blocked_env_vars)
    work_dir: Optional[Path] = None
    mode: SandboxMode = SandboxMode.STRICT
    enable_network: bool = False
    enable_file_write: bool = False

    def __post_init__(self):
        """Valida configuración después de inicialización."""
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds debe ser >= 1")
        if self.max_cpu_seconds < 1:
            raise ValueError("max_cpu_seconds debe ser >= 1")
        if self.max_memory_mb < 64:
            raise ValueError("max_memory_mb debe ser >= 64")
        if self.mode == SandboxMode.STRICT:
            self.enable_network = False
            self.enable_file_write = False

    def is_command_allowed(self, command: str) -> Tuple[bool, str]:
        """
        Verifica si un comando está permitido.

        Args:
            command: Nombre del comando a verificar

        Returns:
            Tuple[bool, str]: (permitido, razón)
        """
        cmd_lower = command.lower().strip()

        # Verificar lista negra primero
        for blocked in self.blocked_commands:
            if blocked.lower() in cmd_lower:
                return False, f"Comando bloqueado: {blocked}"

        # Si hay lista blanca, verificar que esté incluida
        if self.allowed_commands:
            for allowed in self.allowed_commands:
                if allowed.lower() in cmd_lower:
                    return True, "Comando en lista blanca"
            return False, "Comando no está en lista blanca"

        return True, "Comando permitido"


class SandboxExecutor(SandboxExecutionMixin):
    """
    Ejecutor de código y comandos en entorno sandbox.

    Responsabilidades:
    - Ejecutar comandos con límites de recursos
    - Aislar filesystem en directorio temporal
    - Limpiar variables de entorno sensibles
    - Capturar y truncar output
    - Registrar métricas y auditoría
    - Prevenir shell injection (shell=False siempre)

    Uso básico:
        config = SandboxConfig(timeout_seconds=30, max_memory_mb=256)
        executor = SandboxExecutor(config)
        result = executor.execute_command(["ls", "-la"])
        print(result.stdout)

    Uso con Python:
        result = executor.execute_python("print('hello')")
        print(result.stdout)
    """

    def __init__(self, config: Optional[SandboxConfig] = None):
        """
        Inicializa el executor de sandbox.

        Args:
            config: Configuración de límites y restricciones
        """
        self.config = config or SandboxConfig()
        self._execution_count = 0
        self._total_cpu_time = 0.0
        self._total_memory_kb = 0
        logger.info(
            f"SandboxExecutor initialized: "
            f"mode={self.config.mode.value}"
        )

    # ── Confinamiento (fail-closed) ─────────────────────────────────────────

    def _can_confine(self) -> bool:
        """
        ✅ FASE 1 (plan_harness): ¿El entorno permite confinamiento real?

        Verifica las precondiciones para aplicar límites de recursos en modo
        STRICT: plataforma POSIX con el módulo `resource` y los 4 rlimits
        que usa _execute_with_limits (CPU, AS, NPROC, NOFILE). Si alguna
        falta (Windows, macOS sin RLIMIT_NPROC, etc.), el sandbox NO puede
        confinar y debe rechazar la ejecución (fail-closed) en vez de correr
        sin límites.
        """
        if sys.platform == "win32":
            return False
        try:
            import resource as _res
            for attr in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_NOFILE"):
                if not hasattr(_res, attr):
                    return False
            return True
        except ImportError:
            return False

    def _raise_if_cannot_confine(self) -> None:
        """
        ✅ FASE 1 (plan_harness): fail-closed en modo STRICT.

        Si el sandbox está en modo STRICT (producción) pero el entorno no
        permite aplicar límites de recursos, lanza SandboxUnavailableError
        — nunca se ejecuta sin confinamiento. En RELAXED/DISABLED el
        confinamiento no se exige, así que no se lanza.
        """
        if self.config.mode != SandboxMode.STRICT:
            return
        if self._can_confine():
            return
        if not _SANDBOX_UNAVAILABLE_AVAILABLE:
            # Sin la excepción del dominio importable, fallamos igual:
            # RuntimeError explícito (fail-closed) en vez de correr suelto.
            _publish_sandbox_unavailable(
                "platform",
                "Sandbox STRICT requiere confinamiento, plataforma no lo permite",
            )
            raise RuntimeError(
                "Sandbox STRICT requiere confinamiento de recursos, pero "
                "la plataforma no lo permite (win32 o resource no disponible). "
                "Ejecución rechazada (fail-closed)."
            )
        _publish_sandbox_unavailable(
            "platform",
            "Sandbox STRICT requiere confinamiento, plataforma no lo permite",
        )
        raise SandboxUnavailableError(
            "El sandbox STRICT no puede confinar la ejecución en esta "
            "plataforma (win32 o resource/setrlimit no disponible). "
            "Ejecución rechazada (fail-closed).",
            backend="platform",
        )

    def execute_python(
        self,
        code: str,
        args: Optional[List[str]] = None,
        timeout: Optional[float] = None,
    ) -> SandboxResult:
        """
        Ejecuta código Python en sandbox.

        Args:
            code: Código Python a ejecutar
            args: Argumentos adicionales para python
            timeout: Timeout específico

        Returns:
            SandboxResult: Resultado de la ejecución

        Ejemplo:
            >>> executor = SandboxExecutor()
            >>> result = executor.execute_python("print('hello')")
            >>> print(result.stdout)
            hello
        """
        # Validar código básico
        if not code or not code.strip():
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr="Código vacío",
                error="Código Python vacío",
            )

        # Construir comando Python
        python_cmd = [sys.executable, "-c", code]
        if args:
            python_cmd.extend(args)

        # ✅ FIX AUDITORÍA: antes esto pasaba por execute_command() sin
        # _skip_command_checks — el flag '-c' caía en dangerous_flags y
        # SIEMPRE se bloqueaba. execute_python() nunca funcionó. El comentario
        # de "excepción controlada" no tenía código real detrás.
        return self.execute_command(python_cmd, timeout=timeout, _skip_command_checks=True)

    def execute_script(
        self,
        script_path: Union[str, Path],
        args: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        cwd: Optional[Union[str, Path]] = None,
        interpreter: Optional[List[str]] = None,
    ) -> SandboxResult:
        """
        Ejecuta un script en sandbox.

        Args:
            script_path: Ruta al script a ejecutar
            args: Argumentos adicionales
            timeout: Timeout específico
            cwd: Directorio de trabajo del proceso (None = default del sandbox)
            interpreter: Override del intérprete de invocación (lista de
                argumentos, ej. ["/ruta/venv/bin/python"]). Útil para correr
                un venv específico en vez del sys.executable del host. Si no
                se pasa, se infiere por extensión (.py/.sh/.js/.rb) como antes.

        Returns:
            SandboxResult: Resultado de la ejecución
        """
        script = Path(script_path)

        if not script.exists():
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"Script no encontrado: {script_path}",
                error="Script no existe",
            )

        if not script.is_file():
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"No es un archivo: {script_path}",
                error="No es un archivo",
            )

        if interpreter is not None:
            cmd_interpreter = interpreter
        else:
            # Determinar intérprete según extensión
            ext = script.suffix.lower()
            interpreters = {
                ".py": [sys.executable],
                ".sh": ["/bin/bash"],
                ".js": ["node"],
                ".rb": ["ruby"],
            }
            cmd_interpreter = interpreters.get(ext)
            if not cmd_interpreter:
                return SandboxResult(
                    returncode=-1,
                    stdout="",
                    stderr=f"Extensión no soportada: {ext}",
                    error="Extensión no soportada",
                )

        cmd = cmd_interpreter + [str(script)]
        if args:
            cmd.extend(args)

        # ✅ FIX AUDITORÍA: los intérpretes por defecto (sys.executable,
        # /bin/bash, node, ruby) están TODOS en blocked_commands, y un
        # `interpreter` custom (ej. venv/bin/python) también tiene basename
        # "python" — sin este bypass, execute_script() bloqueaba cualquier
        # invocación.
        return self.execute_command(
            cmd,
            cwd=Path(cwd) if cwd else None,
            timeout=timeout,
            _skip_command_checks=True,
        )

    # ── Métricas y estadísticas ─────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas de ejecuciones."""
        return {
            "execution_count": self._execution_count,
            "total_cpu_time": round(self._total_cpu_time, 4),
            "total_memory_kb": self._total_memory_kb,
            "config": {
                "mode": self.config.mode.value,
                "timeout_seconds": self.config.timeout_seconds,
                "max_memory_mb": self.config.max_memory_mb,
                "max_cpu_seconds": self.config.max_cpu_seconds,
            },
        }

    def reset_stats(self) -> None:
        """Reinicia las estadísticas."""
        self._execution_count = 0
        self._total_cpu_time = 0.0
        self._total_memory_kb = 0
        logger.debug("SandboxExecutor stats reset")


# ============================================================================
# CONTEXT MANAGER — Ejecución temporal con cleanup automático
# ============================================================================

@contextmanager
def sandbox_context(config: Optional[SandboxConfig] = None):
    """
    Context manager para ejecución sandbox con cleanup automático.

    Uso:
        with sandbox_context() as executor:
            result = executor.execute_command(["ls", "-la"])
            print(result.stdout)
    """
    executor = SandboxExecutor(config)
    try:
        yield executor
    finally:
        executor.reset_stats()


# ============================================================================
# FACTORY FUNCTION
# ============================================================================

def get_sandbox_executor(
    mode: SandboxMode = SandboxMode.STRICT,
    timeout: float = 30.0,
    max_memory_mb: int = 256,
) -> SandboxExecutor:
    """
    Factory function para crear SandboxExecutor configurado.

    Args:
        mode: Modo de ejecución (STRICT/RELAXED/DISABLED)
        timeout: Timeout en segundos
        max_memory_mb: Límite de memoria en MB

    Returns:
        SandboxExecutor: Instancia configurada
    """
    config = SandboxConfig(
        mode=mode,
        timeout_seconds=timeout,
        max_memory_mb=max_memory_mb,
    )
    return SandboxExecutor(config)
