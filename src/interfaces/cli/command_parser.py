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
# ✅ REFACTOR 50/50 (2026-10-22): los handlers de los comandos OPERATIVOS
#    (help, clear, memory, permission, sandbox, metrics, rac, rollback, model,
#    history, exit, modo) y el CONTRATO CommandResult/CommandDef se movieron a
#    `command_parser_commands.py` (CommandParserCommandsMixin). Este archivo
#    conserva el MOTOR del parser (registro, parseo, despacho, utilidades de
#    introspección) y los comandos de CONTROL/OBSERVABILIDAD del harness
#    (/mode, /session, /audit, /guard, /traces, _format_traces). Se RE-IMPORTAN
#    los nombres movidos ⇒ la API pública no cambia.
from __future__ import annotations

import logging
import shlex
# `dataclass`/`field`/`Callable`/`Tuple` eran atributos de este módulo antes del
# split; se conservan para no cambiar el namespace (los tipos viven en el hermano).
from dataclasses import dataclass, field  # noqa: F401 — namespace histórico
from typing import Any, Callable, Dict, List, Optional, Tuple  # noqa: F401 — idem

logger = logging.getLogger(__name__)

# ── Imports opcionales de subsistemas (los usa esta mitad) ──────────────────
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

# ── Re-exports de la mitad movida (API pública intacta) ─────────────────────
from .command_parser_commands import (  # noqa: E402, F401 — re-export de compatibilidad
    CommandResult,
    CommandDef,
    CommandParserCommandsMixin,
    _PERMISSIONS_AVAILABLE,
    get_gateway,
    PermissionRule,
    PermissionMode,
    _EXECUTOR_AVAILABLE,
    get_executor,
    log_degraded,
)


class CommandParser(CommandParserCommandsMixin):
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

    # ── Utilidades ────────────────────────────────────────────────────────────

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
# ✅ 2026-10-22 — SPLIT 50/50: el contrato (CommandResult/CommandDef) y los
#    handlers de los comandos operativos viven en command_parser_commands.py
#    (CommandParserCommandsMixin), re-importados desde acá.
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
