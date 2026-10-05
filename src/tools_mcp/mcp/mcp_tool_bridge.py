#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# mcp_tool_bridge.py - Puente entre MCP externo y ToolRegistry de AXIOMA
# Ruta: /ruta/al/proyecto
# Autor: SaintWick
# Versión: 0.4.0
#
# Conecta automáticamente servidores MCP y convierte sus tools en
# BaseTool nativas registradas en el ToolRegistry. Proporciona
# transparencia para los agentes, que pueden usar las tools MCP
# a través del ToolExecutor sin conocer su origen.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from ..tool_base import (
    BaseTool,
    ToolParam,
    ToolPermission,
    ToolResult,
    ToolSchema,
)
from ..tool_registry import ToolRegistry, get_registry
from .mcp_client import McpClient
from .mcp_protocol import McpToolDefinition

logger = logging.getLogger(__name__)


# ============================================================================
# MCP DYNAMIC TOOL — BaseTool generada dinámicamente desde definición MCP
# ============================================================================

class McpDynamicTool(BaseTool):
    """
    BaseTool generada automáticamente desde una McpToolDefinition.

    No se instancia directamente — el McpToolBridge la crea
    via _make_tool() con los datos del server.

    El permission_level se asigna según la categoría del server:
        filesystem server → READ_ONLY o LOCAL_WRITE según la tool
        browser server    → NETWORK
        db server         → EXECUTE
    El bridge puede configurar esto por server.
    """

    # ✅ CORREGIDO: Flag para indicar que es clase dinámica
    # Esto permite saltar la validación de description en __init_subclass__
    _is_dynamic_tool = True

    # ✅ CORREGIDO: Placeholder NO VACÍO para pasar validación inicial
    # Estos atributos se sobreescriben en __init__ a nivel de instancia
    name = "_mcp_dynamic"
    description = "Tool MCP dinámica — se configura en runtime"
    permission_level = ToolPermission.NETWORK
    tags: List[str] = ["mcp", "external"]

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        client: McpClient,
        permission: ToolPermission,
        server_label: str,
        input_schema: Dict[str, Any],
    ) -> None:
        # Setear atributos de instancia ANTES de cualquier validación
        # __init_subclass__ valida atributos de CLASE — en instancias
        # necesitamos sobreescribir así:
        object.__setattr__(self, "name", tool_name)
        object.__setattr__(self, "description", tool_description)
        object.__setattr__(self, "permission_level", permission)
        object.__setattr__(self, "tags", ["mcp", "external", server_label])

        self._client = client
        self._server_label = server_label
        self._input_schema = input_schema
        self._calls = 0
        self._failures = 0

        # Construir schema desde input_schema del server MCP
        params = []
        properties = input_schema.get("properties", {})
        required = input_schema.get("required", [])
        for param_name, param_info in properties.items():
            params.append(ToolParam(
                name=param_name,
                type=param_info.get("type", "str"),
                description=param_info.get("description", ""),
                required=param_name in required,
                default=param_info.get("default"),
            ))

        object.__setattr__(self, "schema", ToolSchema(
            name=tool_name,
            description=tool_description,
            params=params,
            returns="str — resultado de la tool MCP",
            tags=["mcp", "external", server_label],
        ))

    def _run(self, **kwargs: Any) -> ToolResult:
        """Sync fallback — delega a arun() via asyncio."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.arun(**kwargs))
        finally:
            loop.close()

    async def arun(self, **kwargs: Any) -> ToolResult:
        """
        Invoca la tool en el server MCP y convierte el resultado a ToolResult.
        Reconecta automáticamente si la conexión cayó.

        ✅ ISSUE-104 (B1a): VALIDA la entrada antes de llamar al server, igual que
        `BaseTool.run()`. Antes esta clase sobreescribía `arun()` y saltaba
        `validate_input()` (y su `_run()` delega acá), así que **las tools MCP eran
        las únicas que nunca validaban**: los argumentos que arma el LLM llegaban
        crudos al server. `run()` no se ejecuta en este camino, así que la validación
        se hace acá explícitamente.
        """
        t0 = time.perf_counter()
        self._calls += 1

        valid, validation_error = self.validate_input(kwargs)
        if not valid:
            latency = (time.perf_counter() - t0) * 1000
            logger.warning(
                f"[{self.name}] Validación de input fallida (MCP): {validation_error}"
            )
            return ToolResult.fail(
                error=f"Validación de input: {validation_error}",
                tool_name=self.name,
                latency_ms=latency,
                metadata={"validation_failed": True, "mcp": True,
                          "server": self._server_label},
            )

        # Reconexión lazy si el client no está conectado
        if not self._client.is_connected:
            try:
                logger.info(
                    f"[Bridge] Reconectando a '{self._server_label}' "
                    f"para tool '{self.name}'"
                )
                await self._client.connect()
            except Exception as e:
                self._failures += 1
                return ToolResult.fail(
                    error=f"No se pudo reconectar al server MCP '{self._server_label}': {e}",
                    tool_name=self.name,
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )

        # Invocar la tool en el server
        # El nombre de la tool que el server conoce puede ser sin prefijo
        # Si el nombre tiene prefijo "server__", extraemos el original
        mcp_tool_name = self.name
        if "__" in self.name:
            mcp_tool_name = self.name.split("__", 1)[1]

        mcp_result = await self._client.call_tool(mcp_tool_name, kwargs)
        latency = (time.perf_counter() - t0) * 1000

        if mcp_result.success:
            return ToolResult.ok(
                output=mcp_result.extract_text(),
                tool_name=self.name,
                latency_ms=latency,
                data=mcp_result.to_dict(),
                source="mcp",
                metadata={
                    "server": self._server_label,
                    "content_blocks": len(mcp_result.content),
                },
            )

        self._failures += 1
        return ToolResult.fail(
            error=mcp_result.extract_text() or "Error desconocido en server MCP",
            tool_name=self.name,
            latency_ms=latency,
            metadata={"server": self._server_label, "is_error": mcp_result.is_error},
        )

    def is_available(self) -> bool:
        """La tool MCP está disponible si el client está conectado."""
        return self._client.is_connected

    def get_tool_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "server": self._server_label,
            "calls": self._calls,
            "failures": self._failures,
            "success_rate": round((self._calls - self._failures) / max(self._calls, 1), 4),
        }


# ============================================================================
# MCP SERVER REGISTRY — Registro de servers conectados
# ============================================================================

class McpServerEntry:
    """Entrada en el registro de servers MCP conectados."""
    __slots__ = ["label", "client", "tool_names", "connected_at", "permission"]

    def __init__(
        self,
        label: str,
        client: McpClient,
        tool_names: List[str],
        permission: ToolPermission,
    ) -> None:
        self.label = label
        self.client = client
        self.tool_names = tool_names
        self.connected_at = time.time()
        self.permission = permission

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "tool_names": self.tool_names,
            "tool_count": len(self.tool_names),
            "permission": ToolPermission(self.permission).name,
            "uptime_seconds": round(time.time() - self.connected_at, 1),
            "client_stats": self.client.get_stats(),
        }


# ============================================================================
# MCP TOOL BRIDGE — Orquestador principal
# ============================================================================

class McpToolBridge:
    """
    Puente entre servers MCP externos y el ToolRegistry de AXIOMA.

    Responsabilidades:
        1. Conectar un McpClient a un server MCP
        2. Listar las tools que el server ofrece
        3. Crear McpDynamicTool para cada una
        4. Registrarlas en el ToolRegistry con prefijo único
        5. Mantener el registro de servers conectados
        6. Desconectar limpiamente y limpiar el registry

    El prefijo evita colisiones: si dos servers tienen una tool
    llamada "read_file", se registran como "fs__read_file" y
    "db__read_file" respectivamente.

    Si el server solo tiene una tool o el nombre ya es único,
    se puede registrar sin prefijo via use_prefix=False.
    """

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry or get_registry()
        self._servers: Dict[str, McpServerEntry] = {}
        self._lock = threading.Lock()

    async def connect_server(
        self,
        label: str,
        client: McpClient,
        permission: ToolPermission = ToolPermission.NETWORK,
        use_prefix: bool = True,
        tool_name_filter: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Conecta un server MCP y registra sus tools en el ToolRegistry.

        Args:
            label       : Nombre descriptivo del server (ej: "filesystem", "browser").
                          Se usa como prefijo en el registry: "filesystem__read_file".
            client      : McpClient ya configurado (stdio o http).
            permission  : Nivel de permiso asignado a TODAS las tools de este server.
                          Default NETWORK — conservador para servers externos.
            use_prefix  : Si True, prefija los nombres con label + "__".
                          Recomendado para evitar colisiones.
            tool_name_filter : Si se especifica, solo registra las tools en esta lista.

        Returns:
            Lista de nombres registrados en el ToolRegistry.

        Raises:
            Exception si la conexión o el listado de tools falla.
        """
        # Conectar
        await client.connect()
        server_info = client.server_info

        if server_info and not server_info.supports_tools():
            logger.warning(
                f"[Bridge] Server '{label}' no declara soporte de tools. "
                "Intentando listar de todas formas."
            )

        # Listar tools del server
        mcp_tools: List[McpToolDefinition] = await client.list_tools()

        if not mcp_tools:
            logger.warning(f"[Bridge] Server '{label}' no reportó tools.")
            return []

        # Filtrar si corresponde
        if tool_name_filter:
            mcp_tools = [t for t in mcp_tools if t.name in tool_name_filter]

        # Crear y registrar BaseTool por cada tool MCP
        registered_names: List[str] = []
        for mcp_tool in mcp_tools:
            tool_name = f"{label}__{mcp_tool.name}" if use_prefix else mcp_tool.name

            dynamic_tool = McpDynamicTool(
                tool_name=tool_name,
                tool_description=mcp_tool.description or f"Tool MCP '{mcp_tool.name}' del server '{label}'",
                client=client,
                permission=permission,
                server_label=label,
                input_schema=mcp_tool.input_schema,
            )

            self._registry.register(dynamic_tool)
            registered_names.append(tool_name)

        # Registrar el server
        with self._lock:
            self._servers[label] = McpServerEntry(
                label=label,
                client=client,
                tool_names=registered_names,
                permission=permission,
            )

        logger.info(
            f"[Bridge] Server '{label}' conectado — "
            f"{len(registered_names)} tools registradas: {registered_names}"
        )
        return registered_names

    async def disconnect_server(self, label: str) -> bool:
        """
        Desconecta un server MCP y elimina sus tools del registry.

        Returns:
            True si el server existía y fue desconectado.
        """
        with self._lock:
            entry = self._servers.pop(label, None)

        if not entry:
            logger.warning(f"[Bridge] Server '{label}' no encontrado para desconectar.")
            return False

        # Limpiar tools del registry
        for tool_name in entry.tool_names:
            self._registry.unregister(tool_name)

        # Desconectar el client
        try:
            await entry.client.disconnect()
        except Exception as e:
            logger.warning(f"[Bridge] Error desconectando '{label}': {e}")

        logger.info(
            f"[Bridge] Server '{label}' desconectado — "
            f"{len(entry.tool_names)} tools eliminadas del registry"
        )
        return True

    async def disconnect_all(self) -> None:
        """Desconecta todos los servers MCP registrados."""
        labels = list(self._servers.keys())
        for label in labels:
            await self.disconnect_server(label)
        logger.info("[Bridge] Todos los servers MCP desconectados")

    # ── Introspección ─────────────────────────────────────────────────────────

    def get_connected_servers(self) -> List[Dict[str, Any]]:
        """Lista todos los servers MCP conectados con sus stats."""
        with self._lock:
            return [entry.to_dict() for entry in self._servers.values()]

    def is_server_connected(self, label: str) -> bool:
        """True si el server con ese label está conectado."""
        with self._lock:
            entry = self._servers.get(label)
        return entry is not None and entry.client.is_connected

    def get_server_tools(self, label: str) -> List[str]:
        """Lista de nombres de tools registradas para un server."""
        with self._lock:
            entry = self._servers.get(label)
        return entry.tool_names if entry else []

    def get_stats(self) -> Dict[str, Any]:
        """Resumen del estado del bridge."""
        with self._lock:
            servers = list(self._servers.values())

        total_tools = sum(len(s.tool_names) for s in servers)
        return {
            "connected_servers": len(servers),
            "total_mcp_tools_registered": total_tools,
            "servers": [s.to_dict() for s in servers],
        }


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================

_bridge: Optional[McpToolBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> McpToolBridge:
    """
    Retorna la instancia singleton del McpToolBridge.

    Usa el ToolRegistry singleton por defecto.
    Uso desde código de configuración o agentes:
        from src.tools_mcp.mcp.mcp_tool_bridge import get_bridge
        from src.tools_mcp.mcp.mcp_client import McpClient
        from src.tools_mcp.tool_base import ToolPermission

        bridge = get_bridge()
        client = McpClient.stdio("npx @modelcontextprotocol/server-filesystem /ruta/al/proyecto
        await bridge.connect_server("filesystem", client, permission=ToolPermission.LOCAL_WRITE)

        # ✅ CORREGIDO: desde ahora en cualquier agente, vía ToolExecutor
        # (NO registry.execute() directo — ver ToolRegistry.execute():
        # "sin validación completa de permisos", saltearía PermissionGateway):
        #   result = await executor.run("filesystem__read_file", path="main.py")
    """
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = McpToolBridge()
    return _bridge
