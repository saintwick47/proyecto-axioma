#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.0 — src/tools_mcp/__init__.py
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/__init__.py
# Autor: SaintWick
# Propósito: API pública del módulo tools_mcp.
#
# Expone exactamente lo que los agentes y el dispatcher necesitan.
# Todo lo demás es detalle de implementación.
#
# Uso desde agentes (import mínimo):
#   from src.tools_mcp import get_executor, ExecutionContext
#   from src.tools_mcp import ToolPermission
#
#   result = await get_executor().run(
#       "weather",
#       context=ExecutionContext(
#           session_permission=ToolPermission.NETWORK,
#           caller="ResearcherAgent",
#       ),
#       query="clima en Buenos Aires",
#   )
#
# Uso para conectar un server MCP:
#   from src.tools_mcp import get_bridge
#   from src.tools_mcp.mcp import McpClient
#
#   bridge = get_bridge()
#   client = McpClient.stdio("npx @modelcontextprotocol/server-filesystem /ruta/a/axioma")
#   await bridge.connect_server("filesystem", client)
#
# Uso para registrar una tool nueva:
#   from src.tools_mcp import get_registry, BaseTool, ToolResult, ToolPermission
#
#   class MiTool(BaseTool):
#       name = "mi_tool"
#       description = "Hace algo útil"
#       permission_level = ToolPermission.READ_ONLY
#       def _run(self, **kwargs) -> ToolResult:
#           return ToolResult.ok("hecho", tool_name=self.name)
#
#   get_registry().register(MiTool())
# ═══════════════════════════════════════════════════════════════

# ── Contratos base ────────────────────────────────────────────
from .tool_base import (
    BaseTool,
    ToolPermission,
    ToolResult,
    ToolSchema,
    ToolParam,
    ToolError,
    ToolPermissionError,
    ToolNotFoundError,
    ToolTimeoutError,
)

# ── Registry ──────────────────────────────────────────────────
from .tool_registry import ToolRegistry, get_registry

# ── Executor ──────────────────────────────────────────────────
from .tool_executor import ToolExecutor, ExecutionContext, get_executor

# ── MCP (import lazy — no falla si mcp/ tiene algún problema) ─
try:
    from .mcp import McpClient, McpTransport, McpToolBridge, get_bridge
    _MCP_AVAILABLE = True
except ImportError as _e:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        f"tools_mcp.mcp no disponible: {_e}. "
        "Funcionalidad MCP deshabilitada — el resto del módulo funciona normal."
    )
    _MCP_AVAILABLE = False
    McpClient = None       # type: ignore[assignment]
    McpTransport = None    # type: ignore[assignment]
    McpToolBridge = None   # type: ignore[assignment]
    get_bridge = None      # type: ignore[assignment]


__all__ = [
    # Contratos base
    "BaseTool",
    "ToolPermission",
    "ToolResult",
    "ToolSchema",
    "ToolParam",
    # Errores
    "ToolError",
    "ToolPermissionError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    # Registry
    "ToolRegistry",
    "get_registry",
    # Executor
    "ToolExecutor",
    "ExecutionContext",
    "get_executor",
    # MCP (puede ser None si no está disponible)
    "McpClient",
    "McpTransport",
    "McpToolBridge",
    "get_bridge",
    # Flag de disponibilidad MCP
    "_MCP_AVAILABLE",
]
