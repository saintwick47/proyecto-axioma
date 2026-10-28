#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — sandbox_execution.py  (REFACTORIZACIÓN 50/50)
# Ruta: /ruta/a/axioma/src/tools_mcp/sandbox_execution.py
#
# ✅ 2026-10-22 (refactorización 50/50): la mitad de MOTOR DE EJECUCIÓN
#    se extrae de `src/tools_mcp/sandbox.py` (1057 líneas) a este hermano,
#    para dejarlo por debajo de las 1000 (regla del proyecto: cada archivo
#    >1000 líneas se parte en DOS: el original conserva una mitad y este
#    hermano recibe la otra; nunca varios archivos chicos).
#
#    Qué se movió, SIN cambios de comportamiento:
#      · El MODELO del resultado de ejecución: `SandboxResult` y
#        `SandboxMode`. El original los re-importa con los MISMOS nombres
#        ⇒ mismo objeto, misma API pública (`from src.tools_mcp.sandbox
#        import SandboxResult, SandboxMode` sigue funcionando igual).
#      · Los helpers de logging/eventos compartidos por las dos mitades:
#        `_publish_sandbox_unavailable`, `_log_sandbox_event`, `logger` y
#        los imports seguros de `SandboxUnavailableError`, `EventTypes` y
#        DetailedLogger. El original re-importa los que usa (los tres
#        primeros símbolos) ⇒ un solo objeto, sin duplicar estado.
#      · El MOTOR DE EJECUCIÓN de comandos, como `SandboxExecutionMixin`:
#        `execute_command` (validaciones de seguridad, staging del cwd en
#        STRICT y cleanup), `_prepare_env` (entorno limpio),
#        `_sanitize_output` (redacción de secretos) y
#        `_execute_with_limits` (Popen, rlimits, timeout, métricas).
#        `SandboxExecutor` (en el original) hereda de este mixin, así que
#        los métodos siguen disponibles en cualquier instancia: la API
#        pública NO cambia.
#
#    Qué QUEDA en el original: `SandboxConfig` + las políticas
#    `_default_blocked_commands`/`_default_blocked_env_vars`, el
#    CONFINAMIENTO (`_can_confine`, `_raise_if_cannot_confine` — que además
#    son marcadores por FUENTE del auditor: `_SECURITY_CHECKS` en
#    tools/auditor/aud_detectors_arquitectura.py exige `def _can_confine`
#    y `def _raise_if_cannot_confine` dentro de sandbox.py), la fachada
#    pública (`__init__`, `execute_python`, `execute_script`), las
#    estadísticas (`get_stats`, `reset_stats`) y las factorías
#    (`sandbox_context`, `get_sandbox_executor`).
#
#    Corte elegido: "ejecutar" (construir/lanzar/acotar el proceso y
#    sanear su salida) vs "configurar/policy/confinar y exponer la API".
#    `execute_python`/`execute_script` quedan en el original porque son
#    fachadas finas de validación que sólo delegan en `execute_command`;
#    los dos predicados de confinamiento quedan porque el auditor los
#    busca en el fuente del original.
#
#    Estado mutable compartido: NINGUNO de módulo (ni caches, ni registros,
#    ni locks, ni singletons). Los únicos globales son `logger` (mismo
#    objeto que el original: `logging.getLogger("src.tools_mcp.sandbox")`)
#    y las banderas de disponibilidad de imports, que son inmutables.
#    Dependencia en UN SOLO SENTIDO (sandbox → este módulo): este hermano
#    es HOJA y NO importa el original a nivel de módulo (sin AUD-CIR-001).
#    Barrido: ningún test parchea globals de sandbox a nivel de módulo
#    (`setattr(sb, "_can_confine", ...)` en test_axioma_07_seguridad_modos
#    parchea la INSTANCIA, que sigue funcionando porque el mixin llama
#    `self._can_confine()`); test_axioma_04 exige SandboxExecutor,
#    SandboxConfig, SandboxMode, SandboxResult y get_sandbox_executor en el
#    original ⇒ los dos movidos se re-exportan.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import resource
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

# Mismo objeto Logger que el original (`logging.getLogger(__name__)` allí):
# así los mensajes del motor conservan el nombre histórico del módulo.
logger = logging.getLogger("src.tools_mcp.sandbox")


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


# ── Import seguro de DetailedLogger ─────────────────────────────
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


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


# ✅ 2026-10-28: qué NO se copia al aislado de STRICT (nunca hace falta para un comando y
# puede ser gigante: entornos virtuales, datos, cachés, .git, modelos).
_EXCLUIR_DE_LA_COPIA = (
    "venv", "venv_axioma", ".venv", "env", ".git", "data", "logs", "cache", "models",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
)
_SUFIJOS_EXCLUIDOS = (".safetensors", ".gguf", ".onnx", ".pyc")


def _tamano_del_arbol(raiz: Path, limite: int) -> int:
    """Tamaño del árbol, cortando apenas supera `limite` (no recorre gigantes enteros)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(raiz):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUIR_DE_LA_COPIA]
        for nombre in filenames:
            if nombre.endswith(_SUFIJOS_EXCLUIDOS):
                continue
            try:
                total += (Path(dirpath) / nombre).lstat().st_size
            except OSError:
                continue
            if total > limite:
                return total
    return total


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

class SandboxExecutionMixin:
    """
    Motor de ejecución de SandboxExecutor (mitad de ejecución, 50/50).

    Agrupa lo que realmente lanza y acota un proceso: validaciones de
    seguridad de `execute_command`, staging/cleanup del cwd en STRICT,
    entorno limpio (`_prepare_env`), redacción de secretos
    (`_sanitize_output`) y la ejecución con rlimits/timeout
    (`_execute_with_limits`).

    Requiere que la clase que lo mezcle defina `self.config` (SandboxConfig)
    y los predicados de confinamiento `_can_confine`/`_raise_if_cannot_confine`
    — ambos viven en `SandboxExecutor`, en el módulo original.
    """

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
            # ✅ FIX (hallazgo agente filesystem v1.6.0): antes STRICT descartaba el `cwd` y
            # dejaba temp_dir vacío — un script con import relativo a un módulo hermano (ej.
            # PromotionGate._analyze_external()) fallaba con ModuleNotFoundError. Copiar el
            # contenido de cwd al temp_dir mantiene el aislamiento: el proceso sólo ve una copia
            # efímera que se borra al final.
            # ✅ 2026-10-28 (MEDIDO): sin tope, eso copiaba el repo ENTERO (venv de 2,8 GB
            # incluido) a /tmp en CADA comando: 7,4 GB y el tmpfs de /tmp lleno con un solo
            # `terminal_exec` desde la raíz. Ahora se excluyen los árboles pesados y, si igual
            # supera `max_copy_bytes`, se ejecuta en el cwd REAL (como RELAXED) en vez de llenar
            # el disco; ahí `work_dir` NO se pisa con el temporal.
            trabajo = Path(temp_dir)
            if cwd is not None:
                try:
                    src = Path(cwd)
                    if src.is_dir():
                        tamano = _tamano_del_arbol(src, self.config.max_copy_bytes)
                        if tamano <= self.config.max_copy_bytes:
                            shutil.copytree(
                                src, temp_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*_EXCLUIR_DE_LA_COPIA),
                            )
                        else:
                            logger.warning(
                                f"[Sandbox] cwd={src} supera "
                                f"{self.config.max_copy_bytes // (1024 * 1024)} MB: NO se copia al "
                                f"aislado; se ejecuta ahí. Subí `max_copy_bytes` si hace falta."
                            )
                            trabajo = src.resolve()
                except Exception as e:
                    logger.warning(
                        f"[Sandbox] No se pudo copiar cwd={cwd} a temp_dir "
                        f"aislado, sigue con temp_dir vacío: {e}"
                    )
            work_dir = trabajo

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
