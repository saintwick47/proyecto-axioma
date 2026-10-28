#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — command_parser_commands.py  (REFACTORIZACIÓN 50/50)
# Ruta: /ruta/a/axioma/src/interfaces/cli/command_parser_commands.py
#
# ✅ REFACTOR: mitad de COMANDOS OPERATIVOS extraída de
#   src/interfaces/cli/command_parser.py (1109 líneas) para dejarlo por debajo
#   de las 1000 (regla del proyecto: cada archivo >1000 líneas se parte en DOS:
#   el original conserva una mitad y este hermano recibe la otra).
#
#   Qué se movió, SIN cambios de comportamiento:
#     · El CONTRATO de resultado: `CommandResult` y `CommandDef`. Los usan las
#       DOS mitades, así que viven acá y el original los re-importa: la
#       dependencia va en UN SOLO SENTIDO (command_parser → este módulo), sin
#       ciclo de import (AUD-CIR-001).
#     · Los handlers de los comandos OPERATIVOS de la CLI →
#       `CommandParserCommandsMixin`: `_cmd_help`, `_cmd_clear`, `_cmd_memory`,
#       `_cmd_permission`, `_cmd_sandbox`, `_cmd_metrics`, `_cmd_rac`,
#       `_cmd_rollback`, `_cmd_model`, `_cmd_history`, `_cmd_exit` y
#       `_cmd_modo` (los comandos documentados en la cabecera/tabla del
#       original, más /modo).
#
#   Qué QUEDA en el original (no se movió):
#     · El motor del parser: `__init__`, `_register_commands`, `handle`,
#       `is_command` y las utilidades de introspección (`_suggest_similar`,
#       `get_command_names`, `get_help_text`).
#     · Los comandos de CONTROL/OBSERVABILIDAD del harness: `/mode`,
#       `/session`, `/audit`, `/guard`, `/traces` y `_format_traces`.
#
#   Por qué la API pública NO cambia:
#     `CommandParser` (en el original) hereda de `CommandParserCommandsMixin`,
#     así que los 12 comandos siguen respondiendo en cualquier instancia; y el
#     original RE-IMPORTA `CommandResult`, `CommandDef` y el mixin con los
#     MISMOS nombres, de modo que
#     `from src.interfaces.cli.command_parser import CommandResult` devuelve
#     EL MISMO objeto que antes.
#
#   Los guards de importación opcional definen `None` cuando falta la
#   dependencia para que el re-export del original sea total (los usos están
#   protegidos por los flags `_PERMISSIONS_AVAILABLE` / `_EXECUTOR_AVAILABLE`,
#   así que el comportamiento no cambia).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ── Imports opcionales de subsistemas (mismos guards que el original) ────────
try:
    from src.tools_mcp.permissions import get_gateway, PermissionRule, PermissionMode
    _PERMISSIONS_AVAILABLE = True
except ImportError:
    _PERMISSIONS_AVAILABLE = False
    get_gateway = PermissionRule = PermissionMode = None

try:
    from src.tools_mcp.tool_executor import get_executor
    _EXECUTOR_AVAILABLE = True
except ImportError:
    _EXECUTOR_AVAILABLE = False
    get_executor = None


# ============================================================================
# CONTRATO DE COMANDO — tipos compartidos por las dos mitades
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
# COMMAND PARSER COMMANDS MIXIN — handlers de los comandos operativos
# ============================================================================

class CommandParserCommandsMixin:
    """Handlers de los comandos de la CLI (mitad extraída de command_parser).

    Se mezcla en `CommandParser` (command_parser.py). No define estado propio:
    usa `self._state` y `self._commands`, que inicializa el `__init__` del
    parser concreto. Los docstrings y el cuerpo de cada handler son los
    originales, sin cambios de comportamiento.
    """

    # ── Handlers de comandos ──────────────────────────────────────────────

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

    # ── /rac — Revisión Axioma Completa ────────────────────────────────────
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
                       f"   Uso: /model <nombre>  (ej: /model qwen3.5:9b)",
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


# ═══════════════════════════════════════════════════════════════
# ✅ MITAD EXTRAÍDA (2026-10-22) — los 12 handlers conservan su cuerpo
#    original (mismos docstrings, mismos CommandResult). El módulo NO importa
#    nada de command_parser.py: la dependencia es unidireccional.
# ═══════════════════════════════════════════════════════════════
