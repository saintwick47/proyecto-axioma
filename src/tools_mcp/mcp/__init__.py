#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.0 — src/tools_mcp/mcp/__init__.py
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/mcp/__init__.py
# Autor: SaintWick
# Propósito: Exports públicos del submódulo MCP.
#            Expone solo lo necesario para uso externo al submódulo.
#            Los internos de protocolo quedan en mcp_protocol.py.
# ═══════════════════════════════════════════════════════════════

from .mcp_client import McpClient, McpTransport
from .mcp_protocol import (
    McpConnectionError,
    McpHandshakeError,
    McpProtocolError,
    McpServerInfo,
    McpToolCallResult,
    McpToolDefinition,
    McpToolCallError,
)
from .mcp_tool_bridge import McpToolBridge, get_bridge

__all__ = [
    # Client
    "McpClient",
    "McpTransport",
    # Protocol types
    "McpServerInfo",
    "McpToolDefinition",
    "McpToolCallResult",
    # Errors
    "McpProtocolError",
    "McpConnectionError",
    "McpHandshakeError",
    "McpToolCallError",
    # Bridge
    "McpToolBridge",
    "get_bridge",
]
