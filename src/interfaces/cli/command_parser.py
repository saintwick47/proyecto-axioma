#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.0 — Command Parser (Slash Commands CLI)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/cli/command_parser.py
# Autor: SaintWick
# Fecha: 2026-04-11
#
# Propósito: Parser robusto de slash commands para el loop interactivo de CLI.
#            Intercepta entradas que empiezan con '/' antes de pasarlas al Dispatcher.
#            Expone 17 comandos con help dinámico y validación de argumentos.
#            (PLAN v2.0 Fase 2: /traces agregado — antes eran 16).
#
# Filosofía JARVIS:
#   El operador necesita control total desde la línea de comandos.
#   Cada slash command es una acción directa al sistema — sin pasar por el LLM.
#
# Uso:
#   parser = CommandParser(console, dispatcher, gateway)
#   result = parser.handle("/memory query axioma --limit 5")
#   if result is not None:
#       # fue un comando, result es el output
#   else:
#       # no era comando, pasar al dispatcher normalmente
#
# Comandos disponibles:
#   /help [command]           → Ayuda general o de comando específico
#   /clear [scope]            → Limpia contexto (session/memory/all)
#   /memory [query] [--limit] → Consulta memoria vectorial
#   /permission [action] ...  → Gestión de reglas de permiso
#   /sandbox                  → Estado del sandbox activo
#   /metrics [component]      → Métricas de rendimiento
#   /rac [completa|parcial]   → Revisión Axioma Completa / Parcial
#   /rollback [session]       → Revierte cambios de sesión
#   /model [name]             → Cambia modelo activo
#   /history [--limit]        → Historial de la sesión
#   /exit [--save]            → Cierra sesión
# ═══════════════════════════════════════════════════════════════
# ✅ v0.5.0 (Fase 2) — Archivo nuevo. Sin dependencias circulares.
#             Import seguro de todos los subsistemas opcionales.
# ✅ NUEVO: Comando /rac para ejecutar Revisión Axioma Completa desde CLI
# ✅ MEJORA: Validación de resultado en _cmd_rac y tipado seguro para UI cross-usage
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import shlex
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports opcionales de subsistemas ────────────────────────────────────────
try:
    from src.tools_mcp.permissions import get_gateway, PermissionRule, PermissionMode
    _PERMISSIONS_AVAILABLE = True
except ImportError:
    _PERMISSIONS_AVAILABLE = False

try:
    from src.tools_mcp.tool_executor import get_executor
    _EXECUTOR_AVAILABLE = True
except ImportError:
    _EXECUTOR_AVAILABLE = False

# ── FASE 1+2 (plan_harness): RuntimeMode, SessionEventLog, FileAuditTrail ──
try:
    from src.domain.types import RuntimeMode
    _RUNTIME_MODE_AVAILABLE = True
except ImportError:
    _RUNTIME_MODE_AVAILABLE = False

try:
    from src.learning.session_event_log import get_session_event_log, SessionEventType
    _SESSION_EVENTS_AVAILABLE = True
except ImportError:
    _SESSION_EVENTS_AVAILABLE = False

try:
    from src.learning.file_audit import get_file_audit
    _FILE_AUDIT_AVAILABLE = True
except ImportError:
    _FILE_AUDIT_AVAILABLE = False


# ============================================================================
# COMMAND RESULT — Resultado de ejecución de un comando
# ============================================================================

@dataclass
class CommandResult:
    """Resultado de la ejecución de un slash command."""
    success: bool
    output: str                          # Texto a mostrar en consola
    exit_session: bool = False           # True → el loop debe terminar
    save_on_exit: bool = False           # True → guardar sesión al salir
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# COMMAND DEFINITION
# ============================================================================

@dataclass
class CommandDef:
    """Definición de un slash command."""
    name: str                            # Sin slash: "help", "clear", etc.
    description: str
    usage: str                           # Ejemplo: "/help [command]"
    args: List[str]                      # Nombres de args: ["command?"] — ? = opcional
    handler: Callable[..., CommandResult]


# ============================================================================
# COMMAND PARSER
# ============================================================================

class CommandParser:
    """
    Parser de slash commands para el loop interactivo de AXIOMA CLI.

    Intercepta entradas que comienzan con '/' y las despacha al handler
    correspondiente. Retorna None para entradas que no son comandos,
    permitiendo que el caller las pase al Dispatcher normalmente.

    Thread-safe para uso en el loop principal de cmd_chat().

    Uso típico en el loop interactivo:
        parser = CommandParser(console=console, session_state=state)
        ...
        user_input = input("👤 Tú: ").strip()
        cmd_result = parser.handle(user_input)
        if cmd_result is not None:
            if cmd_result.exit_session:
                break
            continue  # ya fue procesado
        # else: pasar al dispatcher normalmente
    """

    def __init__(
        self,
        console: Optional[Any] = None,   # CLIConsole — sin import circular. Seguro para UI.
        session_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Args:
            console      : Instancia de CLIConsole para output (Opcional si se usa desde UI).
            session_state: Dict mutable con estado de la sesión actual.
                           Claves usadas: "model", "history", "session_id".
        """
        self._console = console
        self._state: Dict[str, Any] = session_state or {
            "model": None,
            "history": [],
            "session_id": None,
        }
        self._commands: Dict[str, CommandDef] = {}
        self._register_commands()

    # ── Registro de comandos ──────────────────────────────────────────────────

    def _register_commands(self) -> None:
        """Registra los 17 comandos disponibles."""
        defs = [
            CommandDef(
                name="help",
                description="Muestra ayuda general o de un comando específico",
                usage="/help [command]",
                args=["command?"],
                handler=self._cmd_help,
            ),
            CommandDef(
                name="clear",
                description="Limpia el contexto de la sesión",
                usage="/clear [session|memory|all]",
                args=["scope?"],
                handler=self._cmd_clear,
            ),
            CommandDef(
                name="memory",
                description="Consulta la memoria vectorial de AXIOMA",
                usage="/memory [query] [--limit N]",
                args=["query?", "--limit?"],
                handler=self._cmd_memory,
            ),
            CommandDef(
                name="permission",
                description="Gestiona reglas del PermissionGateway",
                usage="/permission list | add <pattern> <mode> | remove <pattern>",
                args=["action", "pattern?", "mode?"],
                handler=self._cmd_permission,
            ),
            CommandDef(
                name="sandbox",
                description="Muestra el estado del sandbox activo",
                usage="/sandbox",
                args=[],
                handler=self._cmd_sandbox,
            ),
            CommandDef(
                name="metrics",
                description="Muestra métricas de rendimiento del sistema",
                usage="/metrics [executor|dispatcher|gateway|all]",
                args=["component?"],
                handler=self._cmd_metrics,
            ),
            # ✅ NUEVO: Comando /rac para Revisión Axioma Completa
            CommandDef(
                name="rac",
                description="Revisión Axioma Completa: analiza archivos y genera MD",
                usage="/rac [completa|parcial]",
                args=["scope?"],
                handler=self._cmd_rac,
            ),
            CommandDef(
                name="rollback",
                description="Revierte cambios de la sesión actual o una específica",
                usage="/rollback [session_id]",
                args=["session?"],
                handler=self._cmd_rollback,
            ),
            CommandDef(
                name="model",
                description="Cambia el modelo LLM activo para esta sesión",
                usage="/model <name>",
                args=["name"],
                handler=self._cmd_model,
            ),
            CommandDef(
                name="history",
                description="Muestra el historial de mensajes de la sesión",
                usage="/history [--limit N]",
                args=["--limit?"],
                handler=self._cmd_history,
            ),
            CommandDef(
                name="exit",
                description="Cierra la sesión interactiva",
                usage="/exit [--save]",
                args=["--save?"],
                handler=self._cmd_exit,
            ),
            # ✅ v0.4.0: Modo Jarvis
            CommandDef(
                name="modo",
                description="Cambia el modo del asistente (normal/jarvis/voice_only/silent)",
                usage="/modo [normal|jarvis|voice_only|silent]",
                args=["mode?"],
                handler=self._cmd_modo,
            ),
            # ✅ FASE 1+2 (plan_harness): /mode, /session, /audit
            CommandDef(
                name="mode",
                description="Muestra o cambia el RuntimeMode (standard|minimal|safe_mode|code_mode)",
                usage="/mode [standard|minimal|safe_mode|code_mode]",
                args=["mode?"],
                handler=self._cmd_mode,
            ),
            CommandDef(
                name="session",
                description="Log de eventos de sesión: /session events [id] | /session fork <nuevo_id>",
                usage="/session events [session_id] | /session fork <nuevo_id>",
                args=["action", "session?"],
                handler=self._cmd_session,
            ),
            CommandDef(
                name="audit",
                description="Auditoría de operaciones de archivo: /audit [session_id]",
                usage="/audit [session_id]",
                args=["session?"],
                handler=self._cmd_audit,
            ),
            # ✅ FASE 4 (plan_harness): /guard — estado de memoria y coordinador
            CommandDef(
                name="guard",
                description="Estado del MemoryGuard y AgentCoordinator (RAM/VRAM, modelos)",
                usage="/guard [memory|coordinator|all]",
                args=["component?"],
                handler=self._cmd_guard,
            ),
            # ✅ PLAN v2.0 (Fase 2): /traces — observabilidad de dispatcher
            CommandDef(
                name="traces",
                description="Traces del dispatcher: /traces [N] | task <type> | handler <name> | stats",
                usage="/traces [N|task <type>|handler <name>|stats]",
                args=["sub?"],
                handler=self._cmd_traces,
            ),
        ]
        for d in defs:
            self._commands[d.name] = d

    # ── Entrada principal ─────────────────────────────────────────────────────

    def handle(self, raw: str) -> Optional[CommandResult]:
        """
        Parsea y ejecuta un slash command.

        Args:
            raw: Entrada cruda del usuario (ej: "/memory axioma --limit 5")

        Returns:
            CommandResult si era un comando válido.
            None si la entrada no empieza con '/' — el caller debe procesarla normalmente.
        """
        raw = raw.strip()
        if not raw.startswith("/"):
            return None

        # Parsear tokens con shlex para manejar strings con espacios
        try:
            tokens = shlex.split(raw)
        except ValueError as e:
            return CommandResult(
                success=False,
                output=f"❌ Error parseando comando: {e}",
            )

        command_name = tokens[0][1:].lower()  # remover '/'
        args = tokens[1:]

        if command_name not in self._commands:
            similar = self._suggest_similar(command_name)
            suggestion = f"\n¿Quisiste decir /{similar}?" if similar else ""
            return CommandResult(
                success=False,
                output=(
                    f"❌ Comando desconocido: /{command_name}{suggestion}\n"
                    f"Usá /help para ver los comandos disponibles."
                ),
            )

        cmd_def = self._commands[command_name]
        logger.debug(f"[CommandParser] Ejecutando /{command_name} args={args}")

        try:
            return cmd_def.handler(args)
        except Exception as e:
            logger.error(f"[CommandParser] Error en /{command_name}: {e}")
            return CommandResult(
                success=False,
                output=f"❌ Error ejecutando /{command_name}: {e}",
            )

    def is_command(self, raw: str) -> bool:
        """True si la entrada es un slash command (empieza con '/')."""
        return raw.strip().startswith("/")

    # ── Handlers de comandos ──────────────────────────────────────────────────

    def _cmd_help(self, args: List[str]) -> CommandResult:
        """/help [command] — Ayuda general o específica."""
        if args:
            target = args[0].lstrip("/").lower()
            if target in self._commands:
                cmd = self._commands[target]
                lines = [
                    f"📖 /{cmd.name}",
                    f"   {cmd.description}",
                    f"   Uso: {cmd.usage}",
                ]
                return CommandResult(success=True, output="\n".join(lines))
            return CommandResult(
                success=False,
                output=f"❌ Comando desconocido: /{target}",
            )

        # Help general
        lines = ["📖 Comandos disponibles:\n"]
        for name, cmd in self._commands.items():
            lines.append(f"  {cmd.usage:<40} {cmd.description}")
        lines.append("\nUsá /help <command> para más detalle.")
        return CommandResult(success=True, output="\n".join(lines))

    def _cmd_clear(self, args: List[str]) -> CommandResult:
        """/clear [scope] — Limpia contexto."""
        scope = args[0].lower() if args else "session"
        valid_scopes = {"session", "memory", "all"}

        if scope not in valid_scopes:
            return CommandResult(
                success=False,
                output=f"❌ Scope inválido: '{scope}'. Opciones: {', '.join(valid_scopes)}",
            )

        cleared = []

        if scope in ("session", "all"):
            self._state["history"] = []
            cleared.append("historial de sesión")

        if scope in ("memory", "all"):
            try:
                from src.memory.gateway import MemoryGateway
                gw = MemoryGateway()
                # Limpiar solo short-term (no vectorial persistente)
                if hasattr(gw, "clear_short_term"):
                    gw.clear_short_term()
                    cleared.append("memoria de corto plazo")
            except Exception as e:
                logger.warning(f"[CommandParser] No se pudo limpiar memoria: {e}")
                cleared.append("memoria (parcial — error en gateway)")

        output = "✅ Limpiado: " + ", ".join(cleared) if cleared else "ℹ️ Nada que limpiar."
        return CommandResult(success=True, output=output)

    def _cmd_memory(self, args: List[str]) -> CommandResult:
        """/memory [query] [--limit N] — Consulta memoria vectorial."""
        # Parsear --limit
        limit = 5
        query_parts = []
        i = 0
        while i < len(args):
            if args[i] == "--limit" and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                    i += 2
                    continue
                except ValueError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 command_parser.py:419] excepción degradada (intencional): {_d2e}")
            query_parts.append(args[i])
            i += 1

        query = " ".join(query_parts).strip()

        try:
            from src.memory.gateway import MemoryGateway
            gw = MemoryGateway()

            if query:
                results = gw.search(query, limit=limit)
                if not results:
                    return CommandResult(success=True, output=f"ℹ️ Sin resultados para: '{query}'")
                lines = [f"🧠 Memoria — resultados para '{query}' (limit={limit}):\n"]
                for i, r in enumerate(results, 1):
                    content = getattr(r, "content", str(r))[:120]
                    score = getattr(r, "score", "?")
                    lines.append(f"  {i}. [{score}] {content}")
                return CommandResult(success=True, output="\n".join(lines))
            else:
                # Sin query: mostrar resumen de la memoria
                stats = gw.get_stats() if hasattr(gw, "get_stats") else {}
                lines = ["🧠 Estado de memoria:\n"]
                for k, v in stats.items():
                    lines.append(f"  {k}: {v}")
                return CommandResult(success=True, output="\n".join(lines) or "ℹ️ Sin estadísticas disponibles.")

        except ImportError:
            return CommandResult(
                success=False,
                output="❌ MemoryGateway no disponible.",
            )
        except Exception as e:
            return CommandResult(success=False, output=f"❌ Error consultando memoria: {e}")

    def _cmd_permission(self, args: List[str]) -> CommandResult:
        """/permission list | add <pattern> <mode> | remove <pattern>"""
        if not _PERMISSIONS_AVAILABLE:
            return CommandResult(
                success=False,
                output="❌ PermissionGateway no disponible.",
            )

        action = args[0].lower() if args else "list"

        if action == "list":
            gateway = get_gateway()
            rules = gateway.list_rules()
            if not rules:
                return CommandResult(success=True, output="ℹ️ Sin reglas configuradas.")
            lines = [f"🔐 Reglas activas ({len(rules)}):\n"]
            for i, r in enumerate(rules, 1):
                tools = r.get("allowed_tools") or "todas"
                lines.append(
                    f"  {i}. [{r['mode']}] {r['pattern']!r} "
                    f"(conf≥{r['min_confidence']} risk≤{r['max_risk_score']} tools={tools})"
                )
                if r.get("description"):
                    lines.append(f"     → {r['description']}")
            return CommandResult(success=True, output="\n".join(lines))

        elif action == "add":
            if len(args) < 3:
                return CommandResult(
                    success=False,
                    output="❌ Uso: /permission add <pattern> <mode>\n"
                           f"   Modos válidos: {', '.join(m.value for m in PermissionMode)}",
                )
            pattern = args[1]
            mode_str = args[2].lower()
            try:
                mode = PermissionMode(mode_str)
            except ValueError:
                return CommandResult(
                    success=False,
                    output=f"❌ Modo inválido: '{mode_str}'. "
                           f"Opciones: {', '.join(m.value for m in PermissionMode)}",
                )
            gateway = get_gateway()
            rule = PermissionRule(pattern=pattern, mode=mode)
            gateway.add_rule(rule, persist=True)
            return CommandResult(
                success=True,
                output=f"✅ Regla agregada: [{mode.value}] {pattern!r}",
            )

        elif action == "remove":
            if len(args) < 2:
                return CommandResult(
                    success=False,
                    output="❌ Uso: /permission remove <pattern>",
                )
            pattern = args[1]
            gateway = get_gateway()
            removed = gateway.remove_rule(pattern, persist=True)
            if removed:
                return CommandResult(success=True, output=f"✅ Regla eliminada: {pattern!r}")
            return CommandResult(success=False, output=f"❌ Regla no encontrada: {pattern!r}")

        return CommandResult(
            success=False,
            output=f"❌ Acción desconocida: '{action}'. Opciones: list, add, remove",
        )

    def _cmd_sandbox(self, args: List[str]) -> CommandResult:
        """/sandbox — Estado del sandbox."""
        try:
            from src.tools_mcp.sandbox import SandboxExecutor
            sb = SandboxExecutor()
            status = sb.get_status() if hasattr(sb, "get_status") else {"active": False}
            lines = ["📦 Estado del Sandbox:\n"]
            for k, v in status.items():
                lines.append(f"  {k}: {v}")
            return CommandResult(success=True, output="\n".join(lines))
        except ImportError:
            return CommandResult(
                success=True,
                output="📦 Sandbox: no inicializado (SandboxExecutor no disponible).",
            )
        except Exception as e:
            return CommandResult(success=False, output=f"❌ Error consultando sandbox: {e}")

    def _cmd_metrics(self, args: List[str]) -> CommandResult:
        """/metrics [component] — Métricas del sistema."""
        component = args[0].lower() if args else "all"
        lines = []

        if component in ("executor", "all") and _EXECUTOR_AVAILABLE:
            try:
                executor = get_executor()
                stats = executor.get_stats()
                lines.append("⚡ ToolExecutor:")
                metrics = stats.get("execution_metrics", {})
                if metrics:
                    for tool, data in list(metrics.items())[:10]:
                        lines.append(
                            f"  {tool}: {data['calls']} calls "
                            f"success={data['success_rate']:.0%} "
                            f"avg={data['avg_latency_ms']:.1f}ms"
                        )
                else:
                    lines.append("  Sin métricas aún.")
            except Exception as e:
                lines.append(f"  ❌ Error: {e}")

        if component in ("gateway", "all") and _PERMISSIONS_AVAILABLE:
            try:
                gateway = get_gateway()
                stats = gateway.get_stats()
                lines.append("\n🔐 PermissionGateway:")
                for k, v in stats.items():
                    lines.append(f"  {k}: {v}")
            except Exception as e:
                lines.append(f"  ❌ Error: {e}")

        if component in ("dispatcher", "all"):
            lines.append("\n📡 Dispatcher: usar /metrics dispatcher requiere instancia activa.")

        if not lines:
            return CommandResult(
                success=False,
                output=f"❌ Componente desconocido: '{component}'. Opciones: executor, gateway, dispatcher, all",
            )

        return CommandResult(success=True, output="\n".join(lines))

    # ── FASE 1+2 (plan_harness): /mode, /session, /audit ────────────────────
    def _cmd_mode(self, args: List[str]) -> CommandResult:
        """/mode [standard|minimal|safe_mode|code_mode] — RuntimeMode de la sesión."""
        if not _RUNTIME_MODE_AVAILABLE:
            return CommandResult(success=False, output="❌ RuntimeMode no disponible.")
        current = self._state.get("runtime_mode")
        if current is None:
            try:
                from config.settings import settings
                current = RuntimeMode(settings.runtime_mode or "standard")
            except Exception:
                current = RuntimeMode.STANDARD

        if not args:
            return CommandResult(
                success=True,
                output=(
                    f"🛡️ RuntimeMode actual: {current.value}\n"
                    f"   write_allowed={current.allows_file_write()} "
                    f"max_permission={current.allowed_permission_level().name}\n"
                    "   Uso: /mode standard|minimal|safe_mode|code_mode"
                ),
            )

        raw = args[0].lower()
        try:
            new_mode = RuntimeMode(raw)
        except ValueError:
            return CommandResult(
                success=False,
                output=(
                    f"❌ Modo inválido: '{raw}'. Opciones: "
                    "standard, minimal, safe_mode, code_mode"
                ),
            )

        self._state["runtime_mode"] = new_mode
        # ✅ gap.txt (2026-08-26): persistir en settings para que
        # BaseAgent._resolve_runtime_mode() lo lea al construir el
        # ExecutionContext — sin esto el filtro del paso 2.5 del
        # ToolExecutor nunca se activa desde los agentes.
        try:
            from config.settings import settings
            settings.runtime_mode = new_mode.value
            logger.info(f"[CommandParser] settings.runtime_mode = {new_mode.value}")
        except Exception as e:
            logger.warning(
                f"[CommandParser] No se pudo persistir runtime_mode en settings: {e}"
            )
        # Registrar el cambio en el log de eventos de la sesión
        if _SESSION_EVENTS_AVAILABLE:
            try:
                log = get_session_event_log()
                if log is not None:
                    session_id = self._state.get("session_id") or "cli"
                    log.append(
                        session_id=session_id,
                        event_type=SessionEventType.MODE_SWITCH,
                        payload={"from": current.value, "to": new_mode.value},
                    )
            except Exception as e:
                logger.debug(f"[CommandParser] mode event error: {e}")

        return CommandResult(
            success=True,
            output=(
                f"🛡️ RuntimeMode cambiado: {current.value} → {new_mode.value}\n"
                f"   write_allowed={new_mode.allows_file_write()} "
                f"max_permission={new_mode.allowed_permission_level().name}"
            ),
        )

    def _cmd_session(self, args: List[str]) -> CommandResult:
        """/session events [session_id] | /session fork <nuevo_id> — log de sesión."""
        if not _SESSION_EVENTS_AVAILABLE:
            return CommandResult(success=False, output="❌ SessionEventLog no disponible.")
        action = args[0].lower() if args else "events"
        session_id = args[1] if len(args) > 1 else (self._state.get("session_id") or "default")

        try:
            log = get_session_event_log()
            if log is None:
                return CommandResult(success=False, output="❌ SessionEventLog no inicializado.")

            if action == "events":
                evs = log.events(session_id, limit=50)
                if not evs:
                    return CommandResult(
                        success=True,
                        output=f"📭 Sin eventos para la sesión '{session_id}'.",
                    )
                lines = [f"📜 Log de sesión '{session_id}' ({len(evs)} eventos):"]
                for ev in evs:
                    lines.append(
                        f"  #{ev['id']} [{ev['event_type']}] "
                        f"{str(ev['payload'])[:80]}"
                    )
                return CommandResult(success=True, output="\n".join(lines))

            if action == "fork":
                if len(args) < 2:
                    return CommandResult(
                        success=False, output="❌ Uso: /session fork <nuevo_id>"
                    )
                # Fuente = sesión actual; destino = el id pedido
                source_id = self._state.get("session_id") or "default"
                new_id = args[1]
                copied = log.fork(source_id, new_id)
                return CommandResult(
                    success=True,
                    output=f"🔀 Sesión '{source_id}' clonada a '{new_id}' ({copied} eventos).",
                )

            if action == "invariant":
                inv = log.verify_invariant(session_id)
                return CommandResult(
                    success=inv["ok"],
                    output=(
                        f"✅ Invariantes OK ({inv['events']} eventos)" if inv["ok"]
                        else f"❌ Invariantes violadas: {inv['checks']}"
                    ),
                )

            return CommandResult(
                success=False,
                output="❌ Acción inválida. Uso: /session events [id] | /session fork <id> | /session invariant [id]",
            )
        except Exception as e:
            return CommandResult(success=False, output=f"❌ Error en /session: {e}")

    def _cmd_audit(self, args: List[str]) -> CommandResult:
        """/audit [session_id] — operaciones de archivo auditadas."""
        if not _FILE_AUDIT_AVAILABLE:
            return CommandResult(success=False, output="❌ FileAuditTrail no disponible.")
        session_id = args[0] if args else (self._state.get("session_id") or "default")

        try:
            audit = get_file_audit()
            if audit is None:
                return CommandResult(success=False, output="❌ FileAuditTrail no inicializado.")
            ops = audit.get_session_operations(session_id, limit=50)
            if not ops:
                return CommandResult(
                    success=True,
                    output=f"📭 Sin operaciones de archivo para la sesión '{session_id}'.",
                )
            lines = [f"🛡️ Operaciones de archivo de '{session_id}' ({len(ops)}):"]
            for op in ops:
                csum = (op.get("checksum_sha256") or "")[:12]
                lines.append(
                    f"  #{op['id']} [{op['status']}] {op['operation']} "
                    f"{op['path']} sha={csum}"
                )
            return CommandResult(success=True, output="\n".join(lines))
        except Exception as e:
            return CommandResult(success=False, output=f"❌ Error en /audit: {e}")

    # ── FASE 4 (plan_harness): /guard ───────────────────────────────────────
    def _cmd_guard(self, args: List[str]) -> CommandResult:
        """/guard [memory|coordinator|all] — estado de memoria y coordinación."""
        component = args[0].lower() if args else "all"
        lines = []

        if component in ("memory", "all"):
            try:
                from src.core.memory_guard import get_memory_guard
                guard = get_memory_guard()
                st = guard.get_status()
                lines.append("🧠 MemoryGuard:")
                lines.append(
                    f"  RAM: {st['ram_used_gb']:.1f} / {st['ram_total_gb']:.1f} GB "
                    f"(warn>{st['warn_ratio']:.0%} hard>{st['hard_ratio']:.0%} "
                    f"headroom={st['headroom_gb']}GB)"
                )
                if st.get("reservations"):
                    lines.append(f"  Reservas: {st['reservations']}")
                else:
                    lines.append("  Reservas: ninguna")
            except Exception as e:
                lines.append(f"  ❌ Error: {e}")

        if component in ("coordinator", "all"):
            try:
                from src.core.agent_coordinator import get_coordinator
                coord = get_coordinator()
                st = coord.get_status()
                lines.append("🤝 AgentCoordinator:")
                lines.append(
                    f"  parallel_models={st['allow_parallel_models']} "
                    f"loaded={st['loaded_models'] or '∅'} busy={st['busy'] or '∅'}"
                )
                conts = st.get("continuations") or {}
                cont_summary = ", ".join(
                    f"{c}:{v['status']}" for c, v in conts.items()
                )
                lines.append(
                    f"  continuaciones: {len(conts)} ({cont_summary})"
                )
            except Exception as e:
                lines.append(f"  ❌ Error: {e}")

        if not lines:
            return CommandResult(
                success=False,
                output=f"❌ Componente desconocido: '{component}'. Opciones: memory, coordinator, all",
            )
        return CommandResult(success=True, output="\n".join(lines))

    def _cmd_traces(self, args: List[str]) -> CommandResult:
        """/traces [N|task <type>|handler <name>|stats] — traces del dispatcher.

        PLAN v2.0 (Fase 2): observabilidad — delega 100% al TraceStore
        singleton (el mismo que usa el endpoint web /api/v1/traces).
        """
        try:
            from src.learning.trace_store import get_trace_store
            store = get_trace_store()
        except Exception as e:
            return CommandResult(
                success=False,
                output=f"❌ TraceStore no disponible: {e}",
            )
        if store is None:
            return CommandResult(
                success=False,
                output="❌ TraceStore no inicializado (traces deshabilitados).",
            )

        sub = args[0].lower() if args else ""
        try:
            if sub == "stats":
                stats = store.latency_stats()
                rate = store.success_rate()
                lines = [
                    "📊 TraceStore — stats",
                    f"  success_rate: {rate:.1%}",
                ]
                if isinstance(stats, dict):
                    for k, v in stats.items():
                        lines.append(f"  {k}: {v}")
                return CommandResult(success=True, output="\n".join(lines))

            if sub == "task":
                task_type = args[1] if len(args) > 1 else ""
                if not task_type:
                    return CommandResult(
                        success=False,
                        output="❌ Uso: /traces task <type>",
                    )
                traces = store.by_task_type(task_type, 20)
                return self._format_traces(traces, f"task={task_type}")

            if sub == "handler":
                handler = args[1] if len(args) > 1 else ""
                if not handler:
                    return CommandResult(
                        success=False,
                        output="❌ Uso: /traces handler <name>",
                    )
                traces = store.by_handler(handler, 20)
                return self._format_traces(traces, f"handler={handler}")

            # Por defecto: /traces [N] — últimas N trazas (default 10)
            try:
                n = int(sub) if sub else 10
            except ValueError:
                return CommandResult(
                    success=False,
                    output=f"❌ Subcomando inválido: '{sub}'. Uso: /traces [N|task <type>|handler <name>|stats]",
                )
            traces = store.recent(min(max(n, 1), 50))
            return self._format_traces(traces, f"últimas {min(max(n, 1), 50)}")
        except Exception as e:
            return CommandResult(
                success=False,
                output=f"❌ Error al consultar traces: {e}",
            )

    def _format_traces(self, traces: List[Dict[str, Any]], label: str) -> CommandResult:
        """Formatea una lista de trazas para consola."""
        if not traces:
            return CommandResult(
                success=True,
                output=f"📊 TraceStore — {label}: sin trazas registradas.",
            )
        lines = [f"📊 TraceStore — {label} ({len(traces)}):", ""]
        for t in traces[:20]:
            ts = t.get("timestamp", "")
            task = t.get("task_type", "?")
            handler = t.get("handler", "?")
            ok = "✅" if t.get("success") else "❌"
            latency = t.get("latency_ms")
            lat_str = f" {latency:.0f}ms" if isinstance(latency, (int, float)) else ""
            lines.append(f"  {ok} [{ts}] {task} → {handler}{lat_str}")
        return CommandResult(success=True, output="\n".join(lines))

    # ── NUEVO: Handler para /rac ────────────────────────────────────────────
    def _cmd_rac(self, args: List[str]) -> CommandResult:
        """/rac [completa|parcial] — Revisión Axioma Completa.
        Delega a AnalystAgent.review_system(), el cual internamente maneja
        la configuración de agente específica (validation=False, max_tokens=800).
        """
        scope = args[0].lower() if args else "completa"
        valid_scopes = {"completa", "parcial"}

        if scope not in valid_scopes:
            return CommandResult(
                success=False,
                output=f"❌ Scope inválido: '{scope}'. Opciones: {', '.join(valid_scopes)}",
            )

        try:
            from src.agents.analyst import AnalystAgent
            logger.info(f"[CommandParser] Ejecutando /rac scope={scope}")

            # El agente instanciado aquí es solo un shell para acceder a review_system.
            # review_system() internamente instanciará un agente dedicado con
            # enable_validation=False y max_tokens=800 para evitar vacíos.
            agent = AnalystAgent(temperature=0.2, enable_memory=False)
            result = agent.review_system(scope=scope)

            # ✅ MEJORA: Verificar el flag success del resultado de review_system
            if not result.get("success", False):
                return CommandResult(
                    success=False,
                    output=f"⚠️ Revisión RAC fallida: {result.get('message', 'Error desconocido')}"
                )

            output = (
                f"✅ Revisión finalizada.\n"
                f"📄 Archivos analizados: {result.get('files_analyzed', 0)}\n"
                f"📝 Reporte generado en: {result.get('report_path', 'N/A')}"
            )
            return CommandResult(success=True, output=output)

        except Exception as e:
            logger.error(f"[CommandParser] Error en /rac: {e}")
            return CommandResult(success=False, output=f"❌ Error ejecutando RAC: {e}")

    def _cmd_rollback(self, args: List[str]) -> CommandResult:
        """/rollback [session_id] — Revierte cambios de sesión."""
        session_id = args[0] if args else self._state.get("session_id")
        if not session_id:
            return CommandResult(
                success=False,
                output="❌ No hay sesión activa para revertir. Especificá un session_id.",
            )
        # Limpiar historial de la sesión como rollback básico de estado
        self._state["history"] = []
        return CommandResult(
            success=True,
            output=f"✅ Rollback aplicado para sesión: {session_id}\n"
                   f"   Historial de sesión limpiado. Los cambios en filesystem requieren git stash.",
            metadata={"rolled_back_session": session_id},
        )

    def _cmd_model(self, args: List[str]) -> CommandResult:
        """/model <name> — Cambia modelo activo."""
        if not args:
            current = self._state.get("model") or "default (settings)"
            return CommandResult(
                success=True,
                output=f"ℹ️ Modelo activo: {current}\n"
                       f"   Uso: /model <nombre>  (ej: /model qwen3:8b)",
            )

        model_name = args[0]
        # Validar disponibilidad en Ollama si es posible
        try:
            from src.llm.client import OllamaClient
            client = OllamaClient()
            available = client.list_models() if hasattr(client, "list_models") else []
            model_names = [m.get("name", "") for m in available] if available else []
            if model_names and model_name not in model_names:
                return CommandResult(
                    success=False,
                    output=f"❌ Modelo no disponible: '{model_name}'\n"
                           f"   Modelos disponibles: {', '.join(model_names[:8])}",
                )
        except Exception as e:
            # R3: no se pudo validar contra Ollama → se acepta el modelo igual.
            log_degraded(logger, e, f"CommandParser._cmd_model (validar '{model_name}')")

        old_model = self._state.get("model") or "default"
        self._state["model"] = model_name
        return CommandResult(
            success=True,
            output=f"✅ Modelo cambiado: {old_model} → {model_name}",
            metadata={"model_changed": True, "new_model": model_name},
        )

    def _cmd_history(self, args: List[str]) -> CommandResult:
        """/history [--limit N] — Historial de la sesión."""
        limit = 10
        if "--limit" in args:
            idx = args.index("--limit")
            if idx + 1 < len(args):
                try:
                    limit = int(args[idx + 1])
                except ValueError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 command_parser.py:985] excepción degradada (intencional): {_d2e}")

        history = self._state.get("history", [])
        if not history:
            return CommandResult(success=True, output="ℹ️ Sin historial en esta sesión.")

        recent = history[-limit:]
        lines = [f"📜 Historial (últimos {len(recent)} de {len(history)}):\n"]
        for i, entry in enumerate(recent, 1):
            role = entry.get("role", "?")
            content = entry.get("content", "")[:100]
            lines.append(f"  {i}. [{role}] {content}")
        return CommandResult(success=True, output="\n".join(lines))

    def _cmd_exit(self, args: List[str]) -> CommandResult:
        """/exit [--save] — Cierra la sesión."""
        save = "--save" in args
        return CommandResult(
            success=True,
            output="✅ Sesión cerrada." + (" Estado guardado." if save else ""),
            exit_session=True,
            save_on_exit=save,
        )

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _cmd_modo(self, args: list) -> "CommandResult":
        """/modo [normal|jarvis|voice_only|silent] — Cambia el modo del asistente."""
        _valid = {"normal", "jarvis", "voice_only", "silent"}
        _labels = {
            "normal":     "👤 Normal",
            "jarvis":     "🤖 Jarvis",
            "voice_only": "🎙️ Sólo voz",
            "silent":     "🔇 Silencioso",
        }
        if not args:
            current = self._state.get("jarvis_mode", "normal") or "normal"
            label = _labels.get(current, current)
            return CommandResult(
                success=True,
                output=(
                    f"Modo actual: {label}\n"
                    f"Modos disponibles: normal | jarvis | voice_only | silent\n"
                    f"Uso: /modo jarvis"
                ),
            )
        mode = args[0].lower()
        if mode not in _valid:
            return CommandResult(
                success=False,
                output=f"❌ Modo inválido: '{mode}'. Válidos: normal | jarvis | voice_only | silent",
            )
        self._state["jarvis_mode"] = mode
        ctx = self._state.get("context")
        if ctx is not None:
            if hasattr(ctx, "update_mode"):
                try:
                    ctx.update_mode(mode)
                except Exception as e:
                    logger.warning(f"[CommandParser] SessionContext.update_mode: {e}")
            elif hasattr(ctx, "jarvis_mode"):
                try:
                    ctx.jarvis_mode = mode
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 command_parser.py:1049] excepción degradada (intencional): {_d2e}")
        vp = self._state.get("voice_pipeline")
        if vp is not None and hasattr(vp, "set_jarvis_mode"):
            try:
                vp.set_jarvis_mode(mode)
            except Exception as e:
                logger.warning(f"[CommandParser] voice_pipeline.set_jarvis_mode: {e}")
        label = _labels.get(mode, mode)
        logger.info(f"[CommandParser] /modo → {mode}")
        return CommandResult(
            success=True,
            output=f"✅ Modo cambiado a: {label}",
            metadata={"jarvis_mode": mode},
        )

    def _suggest_similar(self, name: str) -> Optional[str]:
        """Sugiere el comando más similar por prefijo."""
        for cmd_name in self._commands:
            if cmd_name.startswith(name[:3]) or name.startswith(cmd_name[:3]):
                return cmd_name
        return None

    def get_command_names(self) -> List[str]:
        """Retorna lista de nombres de comandos disponibles."""
        return list(self._commands.keys())

    def get_help_text(self) -> str:
        """Retorna el texto de ayuda completo como string."""
        result = self._cmd_help([])
        return result.output


# ═══════════════════════════════════════════════════════════════
# 📋 CAMBIOS REALIZADOS v0.5.0 (Fase 2) — ARCHIVO NUEVO
# ═══════════════════════════════════════════════════════════════
# | Componente      | Propósito                                              |
# |-----------------|--------------------------------------------------------|
# | CommandResult   | Resultado tipado de ejecución de comando               |
# | CommandDef      | Definición de comando: nombre, uso, args, handler      |
# | CommandParser   | Parser principal — handle() retorna None si no es cmd  |
# | /help           | Help general + específico por comando                  |
# | /clear          | Limpia session/memory/all con import seguro gateway    |
# | /memory         | Búsqueda vectorial + stats con --limit                 |
# | /permission     | list/add/remove sobre PermissionGateway                |
# | /sandbox        | Estado del SandboxExecutor                             |
# | /metrics         | Stats de executor + gateway + dispatcher               |
# | /rac            | Revisión Axioma Completa con validación de resultado   |
# | /rollback       | Rollback de sesión + instrucción git stash             |
# | /model          | Cambio de modelo con validación Ollama opcional        |
# | /history        | Historial de sesión con --limit                        |
# | /exit           | Cierre de sesión con flag --save                       |
# | /mode           | FASE 1: RuntimeMode de la sesión (standard/minimal/safe_mode/code_mode) |
# | /session        | FASE 2: log de sesión (events/fork/invariant)          |
# | /audit          | FASE 2: operaciones de archivo auditadas               |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
