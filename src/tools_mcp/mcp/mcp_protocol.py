#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.0 — MCP Protocol (Tipos y Mensajes)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/mcp/mcp_protocol.py
# Autor: SaintWick
# Fecha: 2026-04-02
#
# Propósito: Tipos, dataclasses y helpers para el protocolo
#            Model Context Protocol (MCP) sobre JSON-RPC 2.0.
#
# El protocolo MCP define cómo los modelos de lenguaje se comunican
# con servers externos para acceder a tools, recursos y prompts.
# AXIOMA lo usa para CONSUMIR tools de servers MCP externos
# (filesystem, browser, base de datos, etc.) sin modificar el core.
#
# Especificación: https://modelcontextprotocol.io/specification
#
# Mensajes implementados:
#   → initialize         Handshake inicial con el server
#   → tools/list         Listar tools disponibles en el server
#   → tools/call         Invocar una tool específica
#   ← notifications/*    Mensajes asíncronos del server (ignorados en v0.4)
#
# Filosofía JARVIS:
#   Conectarse a un server MCP es como JARVIS accediendo a un nuevo
#   subsistema — browser, filesystem, base de datos. Una vez conectado,
#   esas capacidades son nativas. El protocolo es el lenguaje común.
# ═══════════════════════════════════════════════════════════════
# ✅ v0.4.0 — Sin dependencias externas. Solo stdlib.
#             Compatible con MCP spec 2024-11-05.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


# ============================================================================
# CONSTANTES DEL PROTOCOLO
# ============================================================================

MCP_PROTOCOL_VERSION = "2024-11-05"   # Versión del spec MCP implementada
JSON_RPC_VERSION = "2.0"
AXIOMA_CLIENT_NAME = "axioma"
AXIOMA_CLIENT_VERSION = "0.4.0"


# ============================================================================
# TIPOS BASE JSON-RPC 2.0
# ============================================================================

def _new_id() -> str:
    """Genera un ID único para cada request JSON-RPC."""
    return str(uuid.uuid4())[:8]


@dataclass
class JsonRpcRequest:
    """
    Request JSON-RPC 2.0 genérico.
    Base para todos los mensajes que AXIOMA envía al server MCP.
    """
    method: str
    params: Optional[Dict[str, Any]] = None
    id: str = field(default_factory=_new_id)
    jsonrpc: str = JSON_RPC_VERSION

    def to_dict(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
        }
        if self.params is not None:
            msg["params"] = self.params
        return msg

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class JsonRpcResponse:
    """
    Response JSON-RPC 2.0 genérico.
    Parseado desde las respuestas del server MCP.
    """
    id: Optional[str]
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    jsonrpc: str = JSON_RPC_VERSION

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def error_message(self) -> str:
        if self.error:
            return self.error.get("message", "Error desconocido del server MCP")
        return ""

    @property
    def error_code(self) -> Optional[int]:
        if self.error:
            return self.error.get("code")
        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JsonRpcResponse":
        return cls(
            id=data.get("id"),
            result=data.get("result"),
            error=data.get("error"),
            jsonrpc=data.get("jsonrpc", JSON_RPC_VERSION),
        )

    @classmethod
    def from_json(cls, raw: str) -> "JsonRpcResponse":
        try:
            return cls.from_dict(json.loads(raw))
        except json.JSONDecodeError as e:
            return cls(
                id=None,
                error={"code": -32700, "message": f"Parse error: {e}"},
            )


# ============================================================================
# MENSAJES MCP ESPECÍFICOS
# ============================================================================

class McpMethod(str, Enum):
    """Métodos del protocolo MCP que AXIOMA utiliza."""
    INITIALIZE      = "initialize"
    TOOLS_LIST      = "tools/list"
    TOOLS_CALL      = "tools/call"
    RESOURCES_LIST  = "resources/list"   # Reservado para Fase 2
    PROMPTS_LIST    = "prompts/list"     # Reservado para Fase 2
    PING            = "ping"


def build_initialize_request(
    client_name: str = AXIOMA_CLIENT_NAME,
    client_version: str = AXIOMA_CLIENT_VERSION,
) -> JsonRpcRequest:
    """
    Construye el request de inicialización (handshake).
    Primer mensaje enviado al conectar con cualquier server MCP.
    """
    return JsonRpcRequest(
        method=McpMethod.INITIALIZE,
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},          # AXIOMA soporta invocación de tools
                "sampling": {},       # Reservado
            },
            "clientInfo": {
                "name": client_name,
                "version": client_version,
            },
        },
    )


def build_tools_list_request() -> JsonRpcRequest:
    """Solicita la lista de tools disponibles en el server."""
    return JsonRpcRequest(
        method=McpMethod.TOOLS_LIST,
        params={},
    )


def build_tools_call_request(
    tool_name: str,
    arguments: Dict[str, Any],
) -> JsonRpcRequest:
    """
    Construye el request para invocar una tool en el server MCP.

    Args:
        tool_name : Nombre de la tool tal como la reportó el server.
        arguments : Parámetros de la tool (deben respetar su inputSchema).
    """
    return JsonRpcRequest(
        method=McpMethod.TOOLS_CALL,
        params={
            "name": tool_name,
            "arguments": arguments,
        },
    )


def build_ping_request() -> JsonRpcRequest:
    """Ping para verificar que el server sigue activo."""
    return JsonRpcRequest(method=McpMethod.PING, params={})


# ============================================================================
# TIPOS DE RESPUESTA MCP
# ============================================================================

@dataclass
class McpServerInfo:
    """
    Información del server MCP recibida en el handshake initialize.
    Usada por McpServerRegistry para identificar servers conectados.
    """
    name: str
    version: str
    protocol_version: str
    capabilities: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_initialize_result(cls, result: Dict[str, Any]) -> "McpServerInfo":
        server_info = result.get("serverInfo", {})
        return cls(
            name=server_info.get("name", "unknown"),
            version=server_info.get("version", "0.0.0"),
            protocol_version=result.get("protocolVersion", MCP_PROTOCOL_VERSION),
            capabilities=result.get("capabilities", {}),
        )

    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities,
            "supports_tools": self.supports_tools(),
        }


@dataclass
class McpToolDefinition:
    """
    Definición de una tool tal como la reporta un server MCP.
    El bridge la convierte en BaseTool para uso nativo en AXIOMA.
    """
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "McpToolDefinition":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            input_schema=data.get("inputSchema", {}),
        )

    def get_required_params(self) -> List[str]:
        return self.input_schema.get("required", [])

    def get_properties(self) -> Dict[str, Any]:
        return self.input_schema.get("properties", {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class McpToolCallResult:
    """
    Resultado de invocar una tool en un server MCP.
    Parseado desde la response tools/call.
    """
    success: bool
    content: List[Dict[str, Any]]   # Array de content blocks MCP
    is_error: bool = False
    raw_result: Optional[Dict[str, Any]] = None

    @classmethod
    def from_response(cls, response: JsonRpcResponse) -> "McpToolCallResult":
        if not response.success:
            return cls(
                success=False,
                content=[],
                is_error=True,
                raw_result={"error": response.error},
            )
        result = response.result or {}
        is_error = result.get("isError", False)
        content = result.get("content", [])
        return cls(
            success=not is_error,
            content=content,
            is_error=is_error,
            raw_result=result,
        )

    def extract_text(self) -> str:
        """
        Extrae el texto de todos los content blocks.
        MCP puede retornar texto, imágenes y otros tipos —
        AXIOMA extrae solo el texto para compatibilidad con el LLM.
        """
        parts = []
        for block in self.content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "is_error": self.is_error,
            "text": self.extract_text(),
            "content_blocks": len(self.content),
        }


# ============================================================================
# ERRORES DE PROTOCOLO MCP
# ============================================================================

class McpProtocolError(Exception):
    """Error en la comunicación con el protocolo MCP."""
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class McpConnectionError(McpProtocolError):
    """No se pudo establecer o mantener conexión con el server MCP."""


class McpHandshakeError(McpProtocolError):
    """El handshake initialize falló o el server no es compatible."""


class McpToolCallError(McpProtocolError):
    """La invocación de una tool en el server MCP falló."""


# ============================================================================
# HELPERS DE PARSING
# ============================================================================

def parse_tools_list(response: JsonRpcResponse) -> List[McpToolDefinition]:
    """
    Parsea la lista de tools desde una response tools/list.
    Retorna lista vacía si hay error o formato inesperado.
    """
    if not response.success or not response.result:
        return []
    tools_raw = response.result.get("tools", [])
    result = []
    for raw in tools_raw:
        try:
            result.append(McpToolDefinition.from_dict(raw))
        except Exception:
            continue   # Tool con formato inválido — ignorar y continuar
    return result


def is_notification(data: Dict[str, Any]) -> bool:
    """
    True si el mensaje es una notificación MCP (sin id).
    Las notificaciones no requieren response — se ignoran en v0.4.
    """
    return "method" in data and "id" not in data


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN v0.4.0
# ═══════════════════════════════════════════════════════════════
# | Componente            | Propósito                           |
# |-----------------------|-------------------------------------|
# | JsonRpcRequest        | Request genérico JSON-RPC 2.0       |
# | JsonRpcResponse       | Response genérica con parsing       |
# | McpMethod             | Enum de métodos MCP soportados      |
# | build_*_request()     | Factories para cada mensaje MCP     |
# | McpServerInfo         | Info del server desde handshake     |
# | McpToolDefinition     | Tool tal como la reporta el server  |
# | McpToolCallResult     | Resultado de tools/call parseado    |
# | parse_tools_list()    | Helper de parsing para tools/list   |
# | McpProtocolError      | Jerarquía de errores de protocolo   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
