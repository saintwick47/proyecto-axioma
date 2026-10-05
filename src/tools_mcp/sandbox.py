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
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import signal
import resource
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from contextlib import contextmanager

# ── Import desde sandbox_staging (refactorización 50/50) ────────
# Mantenemos la importación para compatibilidad con código externo
# que importe StagingFS/gets_staging desde sandbox.py
from .sandbox_staging import StagingEntry, StagingFS, get_staging

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

# ── Import seguro de SandboxUnavailableError (fail-closed) ───────
# ✅ FASE 1 plan_harness: excepción del dominio para rechazar ejecución
# cuando el sandbox no puede confinar. Import opcional para no romper
# contextos donde src.domain no está en el path (tests unitarios aislados).
try:
    from src.domain.exceptions import SandboxUnavailableError
    _SANDBOX_UNAVAILABLE_AVAILABLE = True
except ImportError:
    SandboxUnavailableError = None  # type: ignore[assignment,misc]
    _SANDBOX_UNAVAILABLE_AVAILABLE = False

# ── Import seguro del EventBus (FASE 3 plan_harness) ─────────────
# Para publicar sandbox.unavailable cuando el fail-closed se dispara.
# El bus se importa DIFERIDO en _publish_sandbox_unavailable: src/core/
# __init__ importa el Dispatcher con impaciencia, y el Dispatcher llega
# hasta sandbox — un import a nivel de módulo crearía un ciclo.
try:
    from src.domain.events import EventTypes as _SandboxEventTypes
    _SANDBOX_EVENT_TYPES_AVAILABLE = True
except ImportError:
    _SANDBOX_EVENT_TYPES_AVAILABLE = False


def _publish_sandbox_unavailable(backend: str, message: str) -> None:
    """Publica el evento de fail-closed si el bus está disponible."""
    if not _SANDBOX_EVENT_TYPES_AVAILABLE:
        return
    try:
        from src.core.event_bus import get_event_bus
        get_event_bus().publish_simple(
            _SandboxEventTypes.SANDBOX_UNAVAILABLE,
            {"backend": backend, "message": message},
            source="sandbox",
        )
    except Exception as e:
        # AUD-SIL-034: el bus nunca debe romper el fail-closed del sandbox
        # (la excepción de confinamiento ya se está lanzando aparte).
        logger.debug(f"[Sandbox] publish sandbox.unavailable error: {e}")


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

# ── Import seguro de DetailedLogger ─────────────────────────────
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_sandbox_event(
    event: str,
    command: str = "",
    success: bool = True,
    duration: float = None,
    error: str = None,
    extra: Dict[str, Any] = None,
) -> None:
    """Delega al DetailedLogger si está disponible. Silencioso si no."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = _get_detailed_logger()
        if getattr(log, "is_initialized", False):
            log.log_agent_execution(
                agent_name="sandbox",
                task_type="SANDBOX_EXECUTION",
                state=event,
                duration=duration,
                success=success,
                error=error,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 sandbox.py:172] excepción degradada (intencional): {_d2e}")


# ============================================================================
# SANDBOX RESULT — Resultado estructurado de ejecución
# ============================================================================

@dataclass
class SandboxResult:
    """
    Resultado de ejecución en sandbox.

    Atributos:
        returncode    : Código de retorno del proceso (0 = éxito)
        stdout        : Salida estándar (truncada si excede límite)
        stderr        : Error estándar (truncada si excede límite)
        killed        : True si el proceso fue terminado por límite
        timeout       : True si excedió el timeout
        cpu_time      : Tiempo de CPU usado en segundos
        memory_kb     : Memoria máxima usada en KB
        duration_ms   : Duración total en milisegundos
        error         : Mensaje de error si hubo fallo
        metadata      : Metadatos adicionales de trazabilidad
    """
    returncode: int
    stdout: str
    stderr: str
    killed: bool = False
    timeout: bool = False
    cpu_time: float = 0.0
    memory_kb: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """True si la ejecución fue exitosa (returncode == 0 y sin error)."""
        return self.returncode == 0 and self.error is None

    def to_dict(self) -> Dict[str, Any]:
        """Serialización para logging, métricas y respuestas API."""
        return {
            "returncode": self.returncode,
            "stdout": self.stdout[:1000] if self.stdout else "",
            "stderr": self.stderr[:1000] if self.stderr else "",
            "killed": self.killed,
            "timeout": self.timeout,
            "cpu_time": round(self.cpu_time, 4),
            "memory_kb": self.memory_kb,
            "duration_ms": round(self.duration_ms, 2),
            "success": self.success,
            "error": self.error,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        status = "✅" if self.success else "❌"
        return (
            f"SandboxResult({status} returncode={self.returncode} "
            f"{self.duration_ms:.1f}ms memory={self.memory_kb}KB)"
        )


# ============================================================================
# SANDBOX CONFIG — Configuración de límites y restricciones
# ============================================================================

class SandboxMode(str, Enum):
    """Modos de ejecución del sandbox."""
    STRICT = "strict"       # Todos los límites activos (producción)
    RELAXED = "relaxed"     # Límites suaves (desarrollo)
    DISABLED = "disabled"   # Sin sandbox (solo testing local)


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


# ============================================================================
# SANDBOX EXECUTOR — Ejecución segura centralizada
# ============================================================================

class SandboxExecutor:
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

    # ── Ejecución de comandos ───────────────────────────────────────────────

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

    def execute_command(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        _skip_command_checks: bool = False,
    ) -> SandboxResult:
        """
        Ejecuta un comando en sandbox con límites de recursos.

        Args:
            args: Lista de argumentos (String NO permitido por seguridad)
            cwd: Directorio de trabajo (None = work_dir config)
            env: Variables de entorno (None = env limpio)
            timeout: Timeout específico (None = config timeout)
            _skip_command_checks: SOLO uso interno (execute_python/execute_script).
                Salta el chequeo de dangerous_flags y blocked_commands, que
                existe para bloquear un caller EXTERNO que intente pasar un
                comando arbitrario (ej. SecureTerminalTool). execute_python()
                y execute_script() construyen `args` ellos mismos a partir de
                valores confiables (sys.executable + dict fijo de intérpretes),
                así que aplicarles la misma blacklist es contraproducente: el
                aislamiento real (shell=False, resource limits, env limpio,
                tmpdir aislado) sigue vigente igual. No exponer a callers externos.
        """
        t0 = time.perf_counter()
        self._execution_count += 1

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Forzado estricto de List[str].
        # Eliminada la conversión automática con shlex.split(). Si el
        # llamador pasa un string, se rechaza por seguridad para evitar
        # inyecciones imprevistas en el parseo.
        # ═══════════════════════════════════════════════════════════════
        if isinstance(args, str):
            _log_sandbox_event(
                "SECURITY_VIOLATION", command=args[:50],
                success=False, error="String args rejected",
            )
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr="Security Error: String args are not allowed. Provide List[str].",
                error="String args rejected for security",
                duration_ms=(time.perf_counter() - t0) * 1000,
                metadata={"security_violation": True, "reason": "string_args_provided"}
            )

        if not args:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr="No se proporcionaron argumentos",
                error="Argumentos vacíos",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 2: Bloqueo de flags de ejecución de shells.
        # Previene `python -c`, `sh -c`, etc. desde un caller EXTERNO.
        # Los callers internos confiables (execute_python/execute_script)
        # pasan _skip_command_checks=True — ver docstring de arriba.
        # ═══════════════════════════════════════════════════════════════
        if not _skip_command_checks:
            dangerous_flags = ["-c", "--command", "-exec", "--exec"]
            for arg in args[1:]: # Skip the command name itself
                if arg in dangerous_flags:
                    _log_sandbox_event(
                        "BLOCKED_FLAG", command=str(args),
                        success=False, error=f"Dangerous flag blocked: {arg}",
                    )
                    return SandboxResult(
                        returncode=-1,
                        stdout="",
                        stderr=f"Security Error: Execution flag '{arg}' is not permitted.",
                        error=f"Dangerous execution flag blocked: {arg}",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        metadata={"blocked": True, "flag": arg},
                    )

            # Verificar comando permitido
            cmd_name = Path(args[0]).name
            allowed, reason = self.config.is_command_allowed(cmd_name)
            if not allowed:
                _log_sandbox_event(
                    "BLOCKED", command=str(args),
                    success=False, error=reason,
                )
                return SandboxResult(
                    returncode=-1,
                    stdout="",
                    stderr=reason,
                    error=f"Comando no permitido: {reason}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    metadata={"blocked": True, "command": cmd_name},
                )

        # Preparar entorno
        clean_env = self._prepare_env(env)

        # Preparar directorio de trabajo
        work_dir = cwd or self.config.work_dir
        temp_dir = None

        if self.config.mode == SandboxMode.STRICT:
            # Crear directorio temporal aislado
            temp_dir = tempfile.mkdtemp(prefix="axioma_sandbox_")
            # ✅ FIX (hallazgo agente filesystem v1.6.0): antes STRICT
            # descartaba el `cwd` recibido y dejaba temp_dir vacío — un
            # script que hace un import relativo a un módulo hermano (ej.
            # PromotionGate._analyze_external() corriendo el .py propuesto
            # junto al resto del workspace) fallaba con ModuleNotFoundError
            # por el cwd equivocado, no por un error real del código
            # (falso negativo). Copiar el CONTENIDO de cwd al temp_dir
            # (en vez de ejecutar directo sobre cwd) mantiene el
            # aislamiento real de STRICT intacto — el proceso solo ve y
            # puede tocar una COPIA efímera que se borra al final (ver
            # cleanup más abajo), nunca el cwd original — y no toca para
            # nada los límites de recursos (cpu/mem/nproc/nofile), que
            # siguen aplicándose exactamente igual más abajo. Fallback
            # silencioso a temp_dir vacío si la copia falla (permisos,
            # symlinks raros, etc.) — mismo comportamiento que había antes.
            if cwd is not None:
                try:
                    src = Path(cwd)
                    if src.is_dir():
                        shutil.copytree(src, temp_dir, dirs_exist_ok=True)
                except Exception as e:
                    logger.warning(
                        f"[Sandbox] No se pudo copiar cwd={cwd} a temp_dir "
                        f"aislado, sigue con temp_dir vacío: {e}"
                    )
            work_dir = Path(temp_dir)

        try:
            # Ejecutar con límites
            result = self._execute_with_limits(
                args=args,
                cwd=work_dir,
                env=clean_env,
                timeout=timeout or self.config.timeout_seconds,
            )

            # Actualizar métricas
            self._total_cpu_time += result.cpu_time
            self._total_memory_kb += result.memory_kb

            # Logging
            _log_sandbox_event(
                event="COMPLETED" if result.success else "FAILED",
                command=str(args),
                success=result.success,
                duration=result.duration_ms / 1000,
                error=result.error,
                extra={"returncode": result.returncode},
            )

            return result

        finally:
            # Limpiar directorio temporal
            if temp_dir and Path(temp_dir).exists():
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(
                        f"Failed to cleanup temp dir {temp_dir}: {e}"
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

    # ── Métodos internos ────────────────────────────────────────────────────

    def _prepare_env(
        self, custom_env: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """
        Prepara entorno limpio para ejecución.

        Args:
            custom_env: Variables adicionales a incluir

        Returns:
            Dict: Entorno limpio y seguro
        """
        # Entorno mínimo seguro
        safe_env = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/usr/bin:/bin"
            ),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "HOME": tempfile.gettempdir(),
            "TMPDIR": tempfile.gettempdir(),
        }

        # Agregar variables custom (filtrando sensibles)
        if custom_env:
            for key, value in custom_env.items():
                # Verificar si es variable sensible
                is_sensitive = any(
                    blocked.lower() in key.lower()
                    for blocked in self.config.blocked_env_vars
                )
                if not is_sensitive:
                    safe_env[key] = value
                else:
                    logger.warning(f"Blocked sensitive env var: {key}")

        return safe_env

    # ═══════════════════════════════════════════════════════════════
    # ✅ FIX FASE 2: Sanitización de Output.
    # Previene la exfiltración de datos sensibles (API keys, tokens)
    # a través de la salida estándar de comandos permitidos.
    # ═══════════════════════════════════════════════════════════════
    def _sanitize_output(self, output: str) -> str:
        """
        Redacta potenciales secretos del output antes de devolverlo
        al LLM. Usa patrones de blocked_env_vars y claves AWS.
        """
        sanitized = output
        for var in self.config.blocked_env_vars:
            # Match VAR_NAME=VALUE o VAR_NAME: VALUE
            pattern = rf'({var}\s*[:=]\s*)(\S+)'
            sanitized = re.sub(pattern, r'\1[REDACTED]', sanitized, flags=re.IGNORECASE)

        # Redactar AWS Access Keys específicas (AKIA...)
        sanitized = re.sub(r'(AKIA[A-Z0-9]{16})', '[REDACTED_AWS_KEY]', sanitized)
        return sanitized

    def _execute_with_limits(
        self,
        args: List[str],
        cwd: Optional[Path],
        env: Dict[str, str],
        timeout: float,
    ) -> SandboxResult:
        """
        Ejecuta comando con límites de recursos.

        Args:
            args: Lista de argumentos
            cwd: Directorio de trabajo
            env: Variables de entorno
            timeout: Timeout en segundos

        Returns:
            SandboxResult: Resultado de la ejecución
        """
        t0 = time.perf_counter()
        killed = False
        timeout_occurred = False
        cpu_time = 0.0
        memory_kb = 0

        # ═══════════════════════════════════════════════════════════════
        # ✅ FASE 1 (plan_harness): FAIL-CLOSED.
        # Antes de intentar cualquier ejecución, verificar que el sandbox
        # puede confinar de verdad. Si no puede → SandboxUnavailableError.
        # Nunca degradar a ejecución sin límites.
        # ═══════════════════════════════════════════════════════════════
        self._raise_if_cannot_confine()

        try:
            # Configurar límites de recursos (Unix only)
            preexec_fn = None
            if (
                sys.platform != "win32"
                and self.config.mode == SandboxMode.STRICT
            ):
                def set_limits():
                    # ═══════════════════════════════════════════════════
                    # ✅ FASE 1: si setrlimit falla (kernel sin soporte,
                    # permisos, etc.) → SandboxUnavailableError. El error
                    # se propaga desde preexec_fn al padre vía Popen y se
                    # re-lanza abajo (fail-closed).
                    # ═══════════════════════════════════════════════════
                    try:
                        # Límite de CPU
                        resource.setrlimit(
                            resource.RLIMIT_CPU,
                            (
                                int(self.config.max_cpu_seconds),
                                int(self.config.max_cpu_seconds) + 1,
                            ),
                        )
                        # Límite de memoria
                        memory_bytes = self.config.max_memory_mb * 1024 * 1024
                        resource.setrlimit(
                            resource.RLIMIT_AS,
                            (memory_bytes, memory_bytes),
                        )
                        # Límite de procesos hijos
                        resource.setrlimit(
                            resource.RLIMIT_NPROC, (50, 50)
                        )
                        # Límite de archivos abiertos
                        resource.setrlimit(
                            resource.RLIMIT_NOFILE, (100, 100)
                        )
                    except Exception as e:
                        _publish_sandbox_unavailable(
                            "setrlimit", f"No se pudieron aplicar límites: {e}"
                        )
                        if _SANDBOX_UNAVAILABLE_AVAILABLE:
                            raise SandboxUnavailableError(
                                f"No se pudieron aplicar límites de recursos "
                                f"(setrlimit): {e}",
                                backend="setrlimit",
                            )
                        raise RuntimeError(
                            f"Sandbox STRICT: no se pudieron aplicar límites "
                            f"de recursos (setrlimit): {e}. Ejecución "
                            f"rechazada (fail-closed)."
                        )

                preexec_fn = set_limits

            # Ejecutar proceso (shell=False siempre para seguridad)
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
                preexec_fn=preexec_fn,
                shell=False,  # ✅ CRÍTICO: Nunca usar shell=True
            )

            try:
                stdout, stderr = process.communicate(timeout=timeout)
                returncode = process.returncode

            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                returncode = -9
                timeout_occurred = True
                killed = True

            except Exception as e:
                process.kill()
                return SandboxResult(
                    returncode=-1,
                    stdout="",
                    stderr=str(e),
                    killed=killed,
                    timeout=timeout_occurred,
                    error=f"Error ejecutando proceso: {e}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

            # Decodificar output
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # ✅ FIX FASE 2: Sanitizar output para redactar secretos
            stdout_str = self._sanitize_output(stdout_str)
            stderr_str = self._sanitize_output(stderr_str)

            # ✅ CORREGIDO v0.5.2: String truncado en una sola línea
            if len(stdout_str) > self.config.max_output_bytes:
                stdout_str = (
                    stdout_str[:self.config.max_output_bytes]
                    + "[...truncado]"
                )
            if len(stderr_str) > self.config.max_output_bytes:
                stderr_str = (
                    stderr_str[:self.config.max_output_bytes]
                    + "[...truncado]"
                )

            # Obtener métricas de recursos (Unix only)
            if sys.platform != "win32":
                try:
                    cpu_time = (
                        process.cpu_times().user
                        + process.cpu_times().system
                    )
                except Exception as e:
                    # AUD-SIL: métricas de CPU best-effort — fallback al
                    # tiempo transcurrido.
                    logger.debug(f"[Sandbox] cpu_times fallback: {e}")
                    cpu_time = timeout - (time.perf_counter() - t0)

                try:
                    memory_kb = process.memory_info().rss // 1024
                except Exception as e:
                    # AUD-SIL: métricas de memoria best-effort.
                    logger.debug(f"[Sandbox] memory_info fallback: {e}")
                    memory_kb = 0

            duration_ms = (time.perf_counter() - t0) * 1000

            return SandboxResult(
                returncode=returncode,
                stdout=stdout_str,
                stderr=stderr_str,
                killed=killed,
                timeout=timeout_occurred,
                cpu_time=cpu_time,
                memory_kb=memory_kb,
                duration_ms=duration_ms,
                metadata={
                    "command": args[0] if args else "",
                    "args_count": len(args),
                    "cwd": str(cwd) if cwd else None,
                },
            )

        except PermissionError as e:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"Permiso denegado: {e}",
                error="Permiso denegado para ejecutar",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except FileNotFoundError as e:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"Comando no encontrado: {e}",
                error="Comando no existe en PATH",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except SandboxUnavailableError:
            # ═══════════════════════════════════════════════════════════
            # ✅ FASE 1 (plan_harness): FAIL-CLOSED — re-lanzar siempre.
            # Puede venir de _raise_if_cannot_confine() o del setrlimit
            # dentro de preexec_fn (Popen lo propaga al padre). El sandbox
            # NUNCA ejecuta sin confinamiento; el caller decide cómo
            # manejarlo (BaseTool.run lo convierte en ToolResult.fail()).
            # ═══════════════════════════════════════════════════════════
            raise
        except Exception as e:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"Error inesperado: {e}",
                error=str(e),
                duration_ms=(time.perf_counter() - t0) * 1000,
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
