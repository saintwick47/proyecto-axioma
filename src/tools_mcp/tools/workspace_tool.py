#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.5.0 — WorkspaceManageTool (Tool MCP)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/tools/workspace_tool.py
# Autor: SaintWick
# Fecha: 2026-05-04
#
# v1.5.0 — FASE 5: Tool MCP para gestionar workspaces persistentes.
#           Lista, inspecciona y limpia workspaces desde el chat.
#           Usa WorkspaceManager internamente.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Dict

from ..tool_base import (
    BaseTool,
    ToolPermission,
    ToolResult,
    ToolSchema,
    ToolParam,
)

logger = logging.getLogger(__name__)

# ✅ FIX CICLO DE IMPORTS (2026-08-26, gap.txt): el import a nivel de
# módulo de src.core.workspace_manager arrastraba src/core/__init__ →
# dispatcher → dispatcher_handlers → src.agents.coder → src.agents.base
# → src.tools_mcp (parcial) — rompía TOOLS_AVAILABLE en base.py cuando
# src.tools_mcp se importaba primero (flujo CLI). Import diferido dentro
# del método que lo usa (línea ~99).


def _get_workspace_manager():
    """Import diferido de get_workspace_manager (rompe el ciclo tools_mcp→core→agents→tools_mcp)."""
    from src.core.workspace_manager import get_workspace_manager
    return get_workspace_manager()


class WorkspaceManageTool(BaseTool):
    """
    Gestiona workspaces persistentes de AXIOMA.

    Acciones:
      - list: Lista todos los workspaces existentes
      - stats: Estadísticas generales del sistema de workspaces
      - inspect: Detalle de un workspace específico (archivos, venv, intentos)
      - cleanup: Elimina un workspace específico
      - cleanup_all: Elimina todos los workspaces
    """
    name = "workspace_manage"
    description = (
        "Gestiona workspaces persistentes de AXIOMA: listar, "
        "inspeccionar archivos generados, ver historial de intentos "
        "y limpiar workspaces antiguos."
    )
    permission_level = ToolPermission.LOCAL_WRITE
    tags = ["workspace", "system", "management"]

    schema = ToolSchema(
        name="workspace_manage",
        description=description,
        params=[
            ToolParam(
                "action", "str",
                "Acción a ejecutar",
            ),
            ToolParam(
                "session_id", "str",
                "ID de sesión (requerido para inspect y cleanup)",
                required=False,
            ),
        ],
        returns="dict — resultado de la acción ejecutada",
        examples=[
            "workspace_manage(action='list')",
            "workspace_manage(action='inspect', session_id='mi-sesion')",
            "workspace_manage(action='cleanup', session_id='mi-sesion')",
        ],
        tags=["workspace", "system"],
    )

    def is_available(self) -> bool:
        try:
            _get_workspace_manager()
            return True
        except Exception:
            return False

    def _run(self, action: str = "", session_id: str = None, **kwargs: Any) -> ToolResult:
        import time
        t0 = time.perf_counter()

        try:
            wm = _get_workspace_manager()
        except Exception as e:
            return ToolResult.fail(
                error=f"WorkspaceManager no disponible: {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        if not action:
            return ToolResult.fail(
                error="Acción requerida: list, stats, inspect, cleanup, cleanup_all",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        latency = (time.perf_counter() - t0) * 1000

        try:
            if action == "list":
                workspaces = wm.list_workspaces()
                return ToolResult.ok(
                    output=f"{len(workspaces)} workspace(s): {', '.join(workspaces) if workspaces else 'ninguno'}",
                    tool_name=self.name,
                    latency_ms=latency,
                    data={"workspaces": workspaces, "count": len(workspaces)},
                )

            elif action == "stats":
                stats = wm.get_stats()
                return ToolResult.ok(
                    output=f"Base: {stats['base_path']} | Workspaces: {stats['workspaces_count']}",
                    tool_name=self.name,
                    latency_ms=latency,
                    data=stats,
                )

            elif action == "inspect":
                if not session_id:
                    return ToolResult.fail(
                        error="session_id requerido para inspect",
                        tool_name=self.name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                    )
                return ToolResult.ok(
                    output=f"Workspace: {session_id}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    data={
                        "session_id": session_id,
                        "workspace_path": wm.get_workspace(session_id),
                        "venv_exists": wm.venv_exists(session_id),
                        "files": wm.scan_workspace(session_id),
                        "attempts": wm.get_attempt_history(session_id),
                    },
                )

            elif action == "cleanup":
                if not session_id:
                    return ToolResult.fail(
                        error="session_id requerido para cleanup",
                        tool_name=self.name,
                        latency_ms=(time.perf_counter() - t0) * 1000,
                    )
                deleted = wm.cleanup(session_id)
                msg = f"Workspace '{session_id}' eliminado" if deleted else f"Workspace '{session_id}' no existía"
                return ToolResult.ok(
                    output=msg,
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    data={"deleted": deleted, "session_id": session_id},
                )

            elif action == "cleanup_all":
                workspaces = wm.list_workspaces()
                results = []
                for sid in workspaces:
                    results.append({"session_id": sid, "deleted": wm.cleanup(sid)})
                total_deleted = sum(1 for r in results if r["deleted"])
                return ToolResult.ok(
                    output=f"{total_deleted}/{len(results)} workspaces eliminados",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                    data={"cleaned": results, "total": len(results), "deleted_count": total_deleted},
                )

            else:
                return ToolResult.fail(
                    error=f"Acción desconocida: {action}. Usar: list, stats, inspect, cleanup, cleanup_all",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

        except Exception as e:
            return ToolResult.fail(
                error=f"Error ejecutando '{action}': {type(e).__name__}: {e}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
