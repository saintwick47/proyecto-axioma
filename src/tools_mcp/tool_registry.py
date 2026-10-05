#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# tool_registry.py - Tool Registry (Sistema Nervioso Central)
# Ruta: /ruta/a/axioma/src/tools_mcp/tool_registry.py
# Autor: SaintWick
# Versión: 1.5.1
#
# Registro centralizado de todas las capacidades de AXIOMA:
# tools nativas (terminal, sistema, workspace), adapters de APIs
# externas (weather, finance, news, web_search) y filesystem.
# Proporciona registro, consulta, ejecución y schemas para MCP.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import os
import shutil
import subprocess
import shlex
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Type

if TYPE_CHECKING:  # ✅ ISSUE-098: solo anotación
    from src.domain.types import RuntimeMode

from .tool_base import (
    BaseTool,
    ToolNotFoundError,
    ToolPermission,
    ToolResult,
    ToolSchema,
    ToolParam,
)

# ── Import desde tool_implementations (refactorización 50/50) ────
from .tool_implementations import (
    # Adapters — APIs externas
    WeatherTool,
    FinanceTool,
    NewsTool,
    WebSearchTool,
    # Utilidades
    CalculatorTool,  # ✅ v0.6.9p: calculadora determinística sin eval()
    # Motores SerpAPI (v0.6.9q): respaldo si un proveedor se bloquea
    SearchImagesTool,
    SearchYoutubeTool,
    SearchMapsTool,
    SearchShoppingTool,
    SearchScholarTool,
    SearchPatentsTool,
    # Corpus (v0.6.9s): biblioteca documental
    CorpusSearchTool,
    CorpusIndexTool,
    # Filesystem — Archivos y directorios
    FsReadTool,
    FsWriteTool,
    FsListTool,
    FsDeleteTool,
    FsMoveTool,
    FsSearchTool,   # ✅ plan_rafael (2026-08-31): búsqueda por nombre
)

logger = logging.getLogger(__name__)

# ── Import seguro de SandboxExecutor ─────────────────────────────
try:
    from .sandbox import SandboxExecutor, SandboxConfig, SandboxResult
    SANDBOX_AVAILABLE = True
except ImportError:
    SANDBOX_AVAILABLE = False
    SandboxExecutor = None
    SandboxConfig = None
    SandboxResult = None
    logger.warning("SandboxExecutor no disponible — usando fallback seguro")

# ── Import seguro de WorkspaceManageTool (v1.5.0) ──────────────
try:
    from .tools.workspace_tool import WorkspaceManageTool
    _WORKSPACE_TOOL_AVAILABLE = True
except ImportError:
    _WORKSPACE_TOOL_AVAILABLE = False
    WorkspaceManageTool = None
    logger.debug("WorkspaceManageTool no disponible")


# ============================================================================
# TERMINAL TOOL — ✅ v1.5.1: shlex.split() ANTES del sandbox
# ============================================================================
class SecureTerminalTool(BaseTool):
    """
    Ejecuta un comando bash en el sistema CON SANDBOX SEGURO.

    ✅ v0.4.1: Usa SandboxExecutor en lugar de subprocess con shell=True
    ✅ v1.5.1: Convierte command:str → args:List[str] via shlex.split()
               antes de pasar al sandbox. Resuelve SECURITY_VIOLATION.

    Contrato de entrada:
    - command: str — comando bash como string (compatible con schema ToolParam)
    - Internamente convertido a List[str] via shlex.split() antes del sandbox

    Fallback: Si sandbox.py no está disponible, usa modo seguro con:
    - shlex.split() para parsear comando
    - Blocklist estricta de comandos peligrosos
    - Timeout configurable
    """
    name = "terminal_exec"
    description = (
        "Ejecuta un comando bash con sandbox seguro. "
        "Retorna stdout + stderr con límites de recursos, "
        "timeout configurable y protección contra shell injection."
    )
    permission_level = ToolPermission.EXECUTE
    tags = ["terminal", "system", "sandbox"]

    # Blocklist de seguridad (defense-in-depth)
    _BLOCKED_PATTERNS = [
        "rm -rf /", "rm -rf ~", "mkfs", "dd if=",
        ":(){ :|:& };:", "chmod -R 777 /",
        "> /dev/sda", "shutdown", "reboot", "halt", "poweroff",
        "sudo", "su ", "pkexec", "doas",
        "curl", "wget", "nc ", "netcat", "ssh", "scp", "sftp",
        "python", "python3", "pip", "pip3",
        "node", "npm", "ruby", "gem",
        "docker", "kubectl", "podman", "containerd",
    ]

    schema = ToolSchema(
        name="terminal_exec",
        description=description,
        params=[
            ToolParam("command", "str", "Comando bash a ejecutar"),
            ToolParam("cwd", "str", "Directorio de trabajo", required=False, default="."),
            ToolParam("timeout", "int", "Timeout en segundos", required=False, default=30),
        ],
        returns="str — stdout y stderr del comando con metadatos de sandbox",
        tags=["terminal", "sandbox"],
    )

    def _run(self, command: str = "", cwd: str = ".", timeout: int = 30, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()

        # Validación básica de entrada
        if not command or not command.strip():
            return ToolResult.fail(
                error="Comando vacío",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # ═══════════════════════════════════════════════════════════
        # ✅ v1.5.1: Convertir string → List[str] ANTES de cualquier ejecución
        # SandboxExecutor.execute_command() requiere args: List[str]
        # shlex.split() parsea correctamente comandos con pipes, &&, quotes
        # ═══════════════════════════════════════════════════════════
        try:
            args_list: List[str] = shlex.split(command)
        except ValueError as e:
            return ToolResult.fail(
                error=f"Comando mal formado (shlex error): {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"command": command, "parse_error": str(e)},
            )

        if not args_list:
            return ToolResult.fail(
                error="Comando vacío tras parsear",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # Verificar blocklist (defense-in-depth)
        # ✅ FIX: chequeo movido a DESPUÉS de shlex.split(), sobre args_list
        # ya parseado. shlex resuelve comillas (cu"r"l → curl) antes de este
        # punto — chequear el string crudo permitía bypassear la blocklist
        # insertando comillas dentro de una palabra bloqueada. Se normaliza
        # reuniendo los tokens ya parseados, que es la forma real que se
        # ejecuta.
        normalized_cmd = " ".join(args_list).lower()
        for blocked in self._BLOCKED_PATTERNS:
            if blocked in normalized_cmd:
                return ToolResult.fail(
                    error=f"Comando bloqueado por seguridad: '{blocked}'",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    metadata={"blocked": True, "pattern": blocked},
                )

        # ── Modo 1: SandboxExecutor disponible (RECOMENDADO) ─────────────
        if SANDBOX_AVAILABLE and SandboxExecutor is not None:
            try:
                config = SandboxConfig(
                    timeout_seconds=float(timeout),
                    max_cpu_seconds=float(timeout) * 0.9,
                    max_memory_mb=256,
                    max_output_bytes=100000,
                )
                executor = SandboxExecutor(config)

                # ✅ v1.5.1: Pasar args_list (List[str]) en vez de command (str)
                result = executor.execute_command(
                    args=args_list,  # ← FIX CRÍTICO: List[str] en vez de str
                    cwd=Path(cwd) if cwd else None,
                    timeout=float(timeout),
                )

                # Construir output con metadatos de sandbox
                output_parts = []
                if result.stdout:
                    output_parts.append(result.stdout.strip())
                if result.stderr:
                    output_parts.append(f"[stderr]\n{result.stderr.strip()}")
                if result.killed:
                    output_parts.append("\n[⚠️ Proceso terminado por límite de recursos]")
                if result.timeout:
                    output_parts.append("\n[⏱️ Timeout excedido]")

                output = "\n".join(output_parts) or "(sin output)"
                latency = (time.perf_counter() - t0) * 1000

                return ToolResult(
                    success=result.success,
                    output=output,
                    tool_name=self.name,
                    latency_ms=latency,
                    data={
                        "returncode": result.returncode,
                        "command": command,
                        "args": args_list,
                        "sandbox": True,
                        "cpu_time": result.cpu_time,
                        "memory_kb": result.memory_kb,
                        "killed": result.killed,
                        "timeout": result.timeout,
                    },
                    error=None if result.success else result.error,
                    source="sandbox",
                    metadata=result.metadata,
                )

            except Exception as e:
                logger.warning(f"SandboxExecutor failed, falling back to safe mode: {e}")
                # Fallback al modo seguro si sandbox falla

        # ── Modo 2: Fallback seguro (sin sandbox.py) ─────────────────────
        try:
            # args_list ya fue parseado arriba — reutilizar
            # Validar que el comando base no esté bloqueado
            cmd_base = Path(args_list[0]).name if args_list else ""
            for blocked in self._BLOCKED_PATTERNS:
                if blocked.split()[0] == cmd_base:
                    return ToolResult.fail(
                        error=f"Comando base bloqueado: '{cmd_base}'",
                        tool_name=self.name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                    )

            # Ejecutar con subprocess seguro
            result = subprocess.run(
                args_list,        # ✅ Ya es List[str]
                shell=False,      # ✅ CRÍTICO: Nunca shell=True
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd if cwd != "." else None,
                env={
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                },
            )

            latency = (time.perf_counter() - t0) * 1000

            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout.strip())
            if result.stderr:
                output_parts.append(f"[stderr]\n{result.stderr.strip()}")

            output = "\n".join(output_parts) or "(sin output)"
            success = result.returncode == 0

            return ToolResult(
                success=success,
                output=output,
                tool_name=self.name,
                latency_ms=latency,
                data={
                    "returncode": result.returncode,
                    "command": command,
                    "args": args_list,
                    "sandbox": False,
                },
                error=None if success else f"Exit code {result.returncode}",
                source="local",
                metadata={"fallback_mode": True},
            )

        except subprocess.TimeoutExpired as e:
            return ToolResult.fail(
                error=f"Timeout ({timeout}s) ejecutando: {command}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"timeout": True, "partial_output": e.output[:500] if e.output else None},
            )
        except FileNotFoundError as e:
            return ToolResult.fail(
                error=f"Comando no encontrado: {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except PermissionError as e:
            return ToolResult.fail(
                error=f"Permiso denegado: {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.fail(
                error=f"Error ejecutando comando: {type(e).__name__}: {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )


# ============================================================================
# SISTEMA TOOL
# ============================================================================
class SysEnvTool(BaseTool):
    """Lee variables de entorno del sistema (solo lectura)."""
    name = "sys_env"
    description = (
        "Lee el valor de una variable de entorno. "
        "Solo lectura — nunca modifica el entorno."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["system", "read"]

    schema = ToolSchema(
        name="sys_env",
        description=description,
        params=[
            ToolParam("var_name", "str", "Nombre de la variable de entorno"),
            ToolParam("default", "str", "Valor si la variable no existe", required=False, default=""),
        ],
        returns="str — valor de la variable o default",
        tags=["system"],
    )

    def _run(self, var_name: str = "", default: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()

        # Bloquear variables sensibles
        blocked_vars = {"AXIOMA_SECRET", "DATABASE_URL", "AWS_SECRET", "PRIVATE_KEY"}
        if var_name.upper() in blocked_vars:
            return ToolResult.fail(
                error=f"Variable bloqueada por seguridad: {var_name}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        value = os.environ.get(var_name, default)
        latency = (time.perf_counter() - t0) * 1000
        found = var_name in os.environ

        return ToolResult.ok(
            output=value,
            tool_name=self.name,
            latency_ms=latency,
            data={"var_name": var_name, "found": found},
        )


# ============================================================================
# SYSTEM OPEN APP TOOL
# ============================================================================
# ✅ NUEVO (agente filesystem): extraída de SystemAgent._open_app_detached()
# (system_agent.py) para que pase por ToolExecutor → PermissionGateway →
# CircuitBreaker como cualquier otra tool, en vez de bypassear ese pipeline
# con una llamada directa a subprocess.Popen() desde el agente. Comportamiento
# idéntico al método original: mismo Popen detached (start_new_session=True,
# necesario para apps GUI persistentes — SandboxExecutor mata el proceso al
# vencer su timeout, ver sandbox.py), misma blocklist por basename, misma
# resolución de alias de navegador vía shutil.which() cacheado. La auditoría
# de cada lanzamiento (bloqueado o exitoso) que antes hacía el método a mano
# vía detailed_logger queda cubierta por _log_tool_event() en tool_executor.py
# — no se duplica acá.

_INSTALLED_APPS_CACHE: dict = {}

_BROWSER_CANDIDATES = [
    "brave", "brave-browser", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "firefox",
]
_BROWSER_ALIASES = {"navegador", "browser", "internet", "web"}

_OPEN_APP_BLOCKLIST = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "sudo", "su", "pkexec", "doas",
    "rm", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "kill", "pkill", "killall",
    "apt", "apt-get", "dpkg", "yum", "dnf", "pacman",
    "pip", "pip3", "npm", "snap", "flatpak",
    "python", "python3", "perl", "ruby", "node", "php",
    "nc", "netcat", "ncat", "curl", "wget", "ssh", "telnet",
    "gcc", "cc", "make",
})


def _detect_installed_apps() -> dict:
    """
    shutil.which() sobre candidatos conocidos. Cacheado en memoria —
    se escanea una sola vez por proceso, no en cada sys_open_app.
    """
    global _INSTALLED_APPS_CACHE
    if _INSTALLED_APPS_CACHE:
        return _INSTALLED_APPS_CACHE
    for name in _BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            _INSTALLED_APPS_CACHE[name] = path
    logger.info(f"[SysOpenAppTool] Apps detectadas: {list(_INSTALLED_APPS_CACHE.keys())}")
    return _INSTALLED_APPS_CACHE


class SysOpenAppTool(BaseTool):
    """Lanza una aplicación GUI del sistema como proceso independiente."""
    name = "sys_open_app"
    description = (
        "Abre una aplicación GUI del sistema (navegador, gestor de archivos, "
        "editor, etc.) como proceso independiente y persistente. No sirve "
        "para comandos de shell ni utilidades de terminal — usar terminal_exec "
        "para eso. 'navegador'/'browser' resuelve contra el navegador instalado."
    )
    permission_level = ToolPermission.EXECUTE
    tags = ["system", "execute"]

    schema = ToolSchema(
        name="sys_open_app",
        description=description,
        params=[
            ToolParam("target", "str", "Nombre o alias de la app a abrir, ej: 'nautilus', 'navegador', 'firefox'"),
        ],
        returns="str — confirmación con el PID del proceso lanzado",
        examples=["sys_open_app(target='nautilus')", "sys_open_app(target='navegador')"],
        tags=["system", "execute"],
    )

    def _run(self, target: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        if not target:
            return ToolResult.fail(
                error="target requerido — nombre de la aplicación a abrir",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        target_lower = target.lower().strip()
        # Mismo criterio que el método original: comparar por basename, no
        # por el string completo — un path ("/bin/bash") esquivaba una
        # blocklist que solo comparaba nombres pelados.
        target_basename = Path(target_lower).name

        if target_basename in _OPEN_APP_BLOCKLIST:
            return ToolResult.fail(
                error=f"'{target}' no se puede abrir como aplicación (utilidad de sistema, no una app GUI)",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
                metadata={"blocked": True},
            )

        resolved = target

        if target_lower in _BROWSER_ALIASES:
            apps = _detect_installed_apps()
            if not apps:
                return ToolResult.fail(
                    error="No se detectó ningún navegador instalado",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            resolved = next(iter(apps.values()))
        elif shutil.which(target) is None:
            apps = _detect_installed_apps()
            if target_lower in (c.split("-")[0] for c in _BROWSER_CANDIDATES) and apps:
                disponibles = ", ".join(apps.keys())
                return ToolResult.fail(
                    error=f"'{target}' no está instalado. Navegadores disponibles: {disponibles}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

        try:
            proc = subprocess.Popen(
                [resolved],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return ToolResult.fail(
                error=f"Comando no encontrado: '{resolved}'",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.fail(
                error=f"Error lanzando '{resolved}': {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        time.sleep(0.3)
        if proc.poll() is not None and proc.returncode != 0:
            return ToolResult.fail(
                error=f"'{resolved}' salió inmediatamente (code={proc.returncode})",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        latency = (time.perf_counter() - t0) * 1000
        return ToolResult.ok(
            output=f"'{resolved}' lanzado (pid={proc.pid})",
            tool_name=self.name,
            latency_ms=latency,
            data={"resolved": resolved, "pid": proc.pid},
        )


# ============================================================================
# TOOL REGISTRY — Sistema nervioso central
# ============================================================================
class ToolRegistry:
    """
    Registro centralizado de todas las capacidades de AXIOMA.

    Singleton thread-safe. Una sola instancia vive durante toda
    la ejecución. Los agentes acceden via get_registry().

    Responsabilidades:
    - Mantener el catálogo de tools disponibles
    - Registrar tools nativas, adapters y tools MCP externas
    - Ejecutar tools por nombre con validación de permisos básica
    - Exponer catálogo a agentes y al bridge MCP

    La validación completa de permisos vive en ToolExecutor.
    El registry solo valida que la tool exista.
    """
    _instance: Optional["ToolRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ToolRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._tools: Dict[str, BaseTool] = {}
        self._initialized = True
        self._register_defaults()
        logger.info(
            f"ToolRegistry inicializado — "
            f"{len(self._tools)} tools registradas"
        )

    # ── Registro ─────────────────────────────────────────────────────────────

    def register(self, tool: BaseTool) -> None:
        """Registra una tool. Sobreescribe si ya existe con el mismo nombre."""
        if not isinstance(tool, BaseTool):
            raise TypeError(f"Solo se pueden registrar instancias de BaseTool, recibido: {type(tool)}")
        self._tools[tool.name] = tool
        logger.debug(f"Tool registrada: '{tool.name}' (perm={ToolPermission(tool.permission_level).name})")

    def register_class(self, tool_class: Type[BaseTool]) -> None:
        """Instancia y registra una clase de tool."""
        self.register(tool_class())

    def unregister(self, name: str) -> bool:
        """Elimina una tool del registry. Retorna True si existía."""
        existed = name in self._tools
        self._tools.pop(name, None)
        if existed:
            logger.debug(f"Tool eliminada del registry: '{name}'")
        return existed

    # ── Consulta ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> BaseTool:
        """Retorna la tool por nombre. Lanza ToolNotFoundError si no existe."""
        if name not in self._tools:
            raise ToolNotFoundError(name)
        return self._tools[name]

    def has(self, name: str) -> bool:
        """True si la tool está registrada."""
        return name in self._tools

    def list_all(self) -> List[Dict[str, Any]]:
        """Lista todas las tools registradas con su descripción completa."""
        return [tool.describe() for tool in self._tools.values()]

    def list_available(self) -> List[Dict[str, Any]]:
        """Lista solo las tools operativas (is_available() == True)."""
        return [
            tool.describe()
            for tool in self._tools.values()
            if tool.is_available()
        ]

    def list_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        """Filtra tools por tag."""
        return [
            tool.describe()
            for tool in self._tools.values()
            if tag in tool.tags
        ]

    def list_by_permission(self, max_level: ToolPermission) -> List[Dict[str, Any]]:
        """Lista tools con permission_level <= max_level."""
        return [
            tool.describe()
            for tool in self._tools.values()
            if tool.permission_level <= max_level
        ]

    def list_for_mode(self, mode: "RuntimeMode") -> List[Dict[str, Any]]:
        """
        ✅ FASE 1 (plan_harness): lista tools permitidas para un RuntimeMode.

        Delega en list_by_permission() con el nivel máximo que permite el
        modo (ver RuntimeMode.allowed_permission_level()). Import diferido
        para evitar dependencias circulares en tiempo de import.
        """
        from src.domain.types import RuntimeMode  # import diferido (evita ciclos)
        max_perm = mode.allowed_permission_level()
        return self.list_by_permission(max_perm)

    def names(self) -> List[str]:
        """Lista de nombres de todas las tools registradas."""
        return list(self._tools.keys())

    # ── Ejecución directa (sin validación de permisos completa) ──────────────

    def execute(self, name: str, **kwargs: Any) -> ToolResult:
        """
        Ejecuta una tool por nombre directamente.
        Sin validación completa de permisos — usar ToolExecutor
        cuando se necesite control de acceso estricto.
        Útil para agentes que ya tienen permisos implícitos.
        """
        tool = self.get(name)  # lanza ToolNotFoundError si no existe
        return tool.run(**kwargs)

    async def aexecute(self, name: str, **kwargs: Any) -> ToolResult:
        """Versión async de execute()."""
        tool = self.get(name)
        return await tool.arun(**kwargs)

    # ── Schemas para MCP ─────────────────────────────────────────────────────

    def get_schemas_for_mcp(self) -> List[Dict[str, Any]]:
        """
        Retorna todos los schemas en formato MCP JSON-RPC.
        Usado por mcp_tool_bridge para registrar las tools
        nativas de AXIOMA en un server MCP externo.
        """
        return [
            tool.get_schema().to_mcp_dict()
            for tool in self._tools.values()
            if tool.is_available()
        ]

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Resumen del estado actual del registry."""
        all_tools = list(self._tools.values())
        available = [t for t in all_tools if t.is_available()]
        by_permission: Dict[str, int] = {}
        for t in all_tools:
            pname = ToolPermission(t.permission_level).name
            by_permission[pname] = by_permission.get(pname, 0) + 1

        return {
            "total_registered": len(all_tools),
            "total_available": len(available),
            "by_permission_level": by_permission,
            "tool_names": self.names(),
        }

    # ── Registro por defecto de todas las tools ───────────────────────────────

    def _register_defaults(self) -> None:
        """
        Registra todas las tools nativas y adapters al inicializar.
        Orden: adapters externos → filesystem → terminal → sistema → workspace.
        Tools de datos (adapters + filesystem) importadas desde
        tool_implementations.py — refactorización 50/50.
        """
        defaults: List[Type[BaseTool]] = [
            # Adapters — APIs existentes (desde tool_implementations.py)
            WeatherTool,
            FinanceTool,
            NewsTool,
            WebSearchTool,
            # Utilidades — ✅ v0.6.9p: calculadora determinística
            CalculatorTool,
            # Motores SerpAPI — ✅ v0.6.9q (respaldo ante bloqueos)
            SearchImagesTool,
            SearchYoutubeTool,
            SearchMapsTool,
            SearchShoppingTool,
            SearchScholarTool,
            SearchPatentsTool,
            # Corpus (v0.6.9s)
            CorpusSearchTool,
            CorpusIndexTool,
            # Filesystem (desde tool_implementations.py)
            FsReadTool,
            FsWriteTool,
            FsListTool,
            FsDeleteTool,
            FsMoveTool,
            FsSearchTool,  # ✅ plan_rafael (2026-08-31)
            # Terminal — ✅ v1.5.1: string→List[str] bridge (local)
            SecureTerminalTool,
            # Sistema (local)
            SysEnvTool,
            SysOpenAppTool,  # ✅ NUEVO (agente filesystem)
        ]

        for cls in defaults:
            try:
                self.register(cls())
            except Exception as e:
                logger.warning(f"No se pudo registrar {cls.__name__}: {e}")

        # ✅ v1.5.0 — FASE 5: Registrar WorkspaceManageTool (import seguro)
        if _WORKSPACE_TOOL_AVAILABLE and WorkspaceManageTool is not None:
            try:
                self.register(WorkspaceManageTool())
                logger.info("Tool registrada: workspace_manage (v1.5.0)")
            except Exception as e:
                logger.warning(f"No se pudo registrar WorkspaceManageTool: {e}")


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================
_registry: Optional[ToolRegistry] = None
_registry_lock = threading.Lock()

def get_registry() -> ToolRegistry:
    """
    Retorna la instancia singleton del ToolRegistry.
    Thread-safe. Crea la instancia en el primer llamado.

    Uso en agentes:
        from src.tools_mcp.tool_registry import get_registry
        registry = get_registry()
        result = registry.execute("weather", query="Buenos Aires")
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ToolRegistry()
    return _registry
