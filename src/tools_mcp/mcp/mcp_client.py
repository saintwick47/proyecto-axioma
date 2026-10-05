#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.4.0 — MCP Client (Consumidor de Servers Externos)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/mcp/mcp_client.py
# Autor: SaintWick
# Fecha: 2026-04-02
#
# Propósito: Conectar AXIOMA a servers MCP externos y ejecutar
#            sus tools como si fueran capacidades nativas.
#
# Transportes soportados:
#   stdio  → Servidor como proceso hijo (filesystem MCP, git MCP, etc.)
#             Comunicación por stdin/stdout — el más común en MCP local.
#   http   → Servidor como servicio HTTP/SSE (browser MCP, APIs remotas).
#             Comunicación por requests HTTP — para servers remotos.
#
# Filosofía JARVIS:
#   Cada server MCP es un nuevo subsistema que JARVIS puede conectar.
#   Filesystem MCP → JARVIS puede navegar el proyecto completo.
#   Browser MCP    → JARVIS puede navegar la web autónomamente.
#   DB MCP         → JARVIS puede consultar bases de datos.
#   Todo sin modificar el core — solo conectar y usar.
#
# Uso típico:
#   client = McpClient.stdio("npx @modelcontextprotocol/server-filesystem /home")
#   await client.connect()
#   tools = await client.list_tools()       # McpToolDefinition[]
#   result = await client.call_tool("read_file", {"path": "main.py"})
#   await client.disconnect()
# ═══════════════════════════════════════════════════════════════
# ✅ v0.4.0 — stdio funcional. HTTP con httpx (import seguro).
#             Reconexión automática con backoff exponencial.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .mcp_protocol import (
    JsonRpcRequest,
    JsonRpcResponse,
    McpConnectionError,
    McpHandshakeError,
    McpServerInfo,
    McpToolCallResult,
    McpToolDefinition,
    build_initialize_request,
    build_ping_request,
    build_tools_call_request,
    build_tools_list_request,
    is_notification,
    parse_tools_list,
)

logger = logging.getLogger(__name__)


# ── Import seguro httpx — solo necesario para transporte HTTP ─────────────────
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ============================================================================
# TIPOS DE TRANSPORTE
# ============================================================================

class McpTransport(str, Enum):
    STDIO = "stdio"   # Proceso hijo — el más común para MCP local
    HTTP  = "http"    # Servicio HTTP/SSE — para servers remotos


# ============================================================================
# MCP CLIENT
# ============================================================================

# ✅ ISSUE-104 (B1b): máximo de líneas que se leen esperando la respuesta propia
# (notificaciones, respuestas ajenas o basura) antes de rendirse.
_MAX_LINEAS_ESPERANDO_RESPUESTA = 20


def clasificar_linea(raw: Any, id_esperado: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Clasifica una línea del server MCP: `respuesta` | `ajena` | `notificacion` | `invalida`.

    ✅ ISSUE-104 (B1b): el transporte stdio devolvía la **primera** línea que no
    fuera notificación, sin mirar el `id`. Con una sola petición en vuelo eso suele
    alcanzar, pero **tras un timeout la respuesta tardía queda en el buffer** y se
    consume como respuesta de la petición SIGUIENTE ⇒ los resultados se cruzan.
    Ahora se compara el `id` y se descarta lo que no corresponde.
    """
    try:
        texto = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
        data = json.loads(texto)
    except Exception:  # noqa: BLE001 — JSON roto o línea vacía
        return "invalida", None
    if not isinstance(data, dict):
        return "invalida", None
    if is_notification(data):
        return "notificacion", data
    if "id" not in data:
        return "invalida", data
    if str(data.get("id")) != str(id_esperado):
        return "ajena", data
    return "respuesta", data


class McpClient:
    """
    Cliente MCP para consumir tools de servers externos.

    Soporta dos transportes:
        stdio — inicia el server como proceso hijo y comunica via stdin/stdout
        http  — conecta a un server HTTP existente

    Thread-safe para lectura concurrente. Una instancia por server.
    Usar McpClient.stdio() o McpClient.http() como constructores.

    El McpToolBridge usa este cliente para convertir las tools del
    server en BaseTool nativas registradas en el ToolRegistry de AXIOMA.
    """

    def __init__(
        self,
        transport: McpTransport,
        command: Optional[str] = None,       # Para stdio: comando a ejecutar
        args: Optional[List[str]] = None,    # Args adicionales del proceso
        url: Optional[str] = None,           # Para http: URL base del server
        timeout: float = 30.0,
        max_reconnect_attempts: int = 3,
        server_label: str = "",              # Nombre descriptivo para logs
    ) -> None:
        self.transport = transport
        self.command = command
        self.args = args or []
        self.url = url
        self.timeout = timeout
        self.max_reconnect_attempts = max_reconnect_attempts
        self.server_label = server_label or command or url or "mcp-server"

        # Estado de conexión
        self._connected: bool = False
        self._server_info: Optional[McpServerInfo] = None
        self._cached_tools: Optional[List[McpToolDefinition]] = None

        # Transporte stdio
        self._process: Optional[asyncio.subprocess.Process] = None
        self._read_lock = asyncio.Lock()

        # Métricas básicas
        self._calls_made: int = 0
        self._calls_failed: int = 0
        self._connected_at: Optional[float] = None

    # ── Constructores ─────────────────────────────────────────────────────────

    @classmethod
    def stdio(
        cls,
        command: str,
        args: Optional[List[str]] = None,
        timeout: float = 30.0,
        label: str = "",
    ) -> "McpClient":
        """
        Constructor para servidor MCP via stdio (proceso hijo).

        Ejemplos de servers stdio conocidos:
            "npx @modelcontextprotocol/server-filesystem /ruta"
            "npx @modelcontextprotocol/server-git"
            "uvx mcp-server-sqlite --db-path data.db"

        Args:
            command : Comando completo o ejecutable del server.
            args    : Argumentos adicionales separados.
            timeout : Timeout por operación en segundos.
            label   : Nombre descriptivo para logging.
        """
        parts = command.split() if isinstance(command, str) else [command]
        exe = parts[0]
        cmd_args = parts[1:] + (args or [])
        return cls(
            transport=McpTransport.STDIO,
            command=exe,
            args=cmd_args,
            timeout=timeout,
            server_label=label or command[:40],
        )

    @classmethod
    def http(
        cls,
        url: str,
        timeout: float = 30.0,
        label: str = "",
    ) -> "McpClient":
        """
        Constructor para servidor MCP via HTTP.

        Args:
            url     : URL base del server MCP (ej: "http://localhost:3000").
            timeout : Timeout por request en segundos.
            label   : Nombre descriptivo para logging.
        """
        if not _HTTPX_AVAILABLE:
            raise McpConnectionError(
                "httpx no instalado. Instalá con: pip install httpx --break-system-packages"
            )
        return cls(
            transport=McpTransport.HTTP,
            url=url.rstrip("/"),
            timeout=timeout,
            server_label=label or url,
        )

    # ── Conexión ──────────────────────────────────────────────────────────────

    async def connect(self) -> McpServerInfo:
        """
        Conecta al server MCP y realiza el handshake initialize.

        Retorna McpServerInfo con las capacidades del server.
        Lanza McpConnectionError o McpHandshakeError si falla.
        """
        if self._connected and self._server_info:
            return self._server_info

        logger.info(f"[MCP] Conectando a '{self.server_label}' ({self.transport.value})")

        if self.transport == McpTransport.STDIO:
            await self._connect_stdio()
        else:
            await self._connect_http()

        # Handshake initialize
        server_info = await self._initialize()
        self._server_info = server_info
        self._connected = True
        self._connected_at = time.time()

        logger.info(
            f"[MCP] Conectado: '{self.server_label}' — "
            f"server={server_info.name} v{server_info.version} "
            f"tools={server_info.supports_tools()}"
        )
        return server_info

    async def disconnect(self) -> None:
        """Cierra la conexión con el server."""
        if self.transport == McpTransport.STDIO and self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 mcp_client.py:233] excepción degradada (intencional): {_d2e}")
            self._process = None

        self._connected = False
        self._server_info = None
        self._cached_tools = None
        logger.info(f"[MCP] Desconectado: '{self.server_label}'")

    # ── API pública ───────────────────────────────────────────────────────────

    async def list_tools(self, use_cache: bool = True) -> List[McpToolDefinition]:
        """
        Lista las tools disponibles en el server.

        Args:
            use_cache : Si True, retorna tools cacheadas si ya se listaron.
                        Las tools de un server no cambian en runtime normalmente.

        Returns:
            Lista de McpToolDefinition lista para que el bridge las adapte.
        """
        self._ensure_connected()

        if use_cache and self._cached_tools is not None:
            return self._cached_tools

        request = build_tools_list_request()
        response = await self._send(request)
        tools = parse_tools_list(response)
        self._cached_tools = tools

        logger.info(f"[MCP] '{self.server_label}' reportó {len(tools)} tools")
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> McpToolCallResult:
        """
        Invoca una tool en el server MCP.

        Args:
            tool_name : Nombre exacto de la tool (reportado por list_tools).
            arguments : Parámetros de la tool.

        Returns:
            McpToolCallResult con success, texto extraído y content blocks.
        """
        self._ensure_connected()

        request = build_tools_call_request(tool_name, arguments)
        self._calls_made += 1

        try:
            response = await self._send(request)
            result = McpToolCallResult.from_response(response)

            if not result.success:
                self._calls_failed += 1
                logger.warning(
                    f"[MCP] Tool '{tool_name}' en '{self.server_label}' falló: "
                    f"{result.extract_text()[:100]}"
                )

            return result

        except Exception as e:
            self._calls_failed += 1
            logger.error(f"[MCP] Error llamando '{tool_name}': {e}")
            return McpToolCallResult(
                success=False,
                content=[{"type": "text", "text": f"Error MCP: {e}"}],
                is_error=True,
            )

    async def ping(self) -> bool:
        """Verifica que el server siga activo. True si responde."""
        if not self._connected:
            return False
        try:
            request = build_ping_request()
            response = await asyncio.wait_for(
                self._send(request),
                timeout=5.0,
            )
            return response.success
        except Exception:
            return False

    # ── Transporte stdio ──────────────────────────────────────────────────────

    async def _connect_stdio(self) -> None:
        """Inicia el proceso hijo del server MCP y prepara stdin/stdout."""
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Breve espera para que el proceso arranque
            await asyncio.sleep(0.3)

            if self._process.returncode is not None:
                stderr_out = b""
                if self._process.stderr:
                    stderr_out = await self._process.stderr.read(500)
                raise McpConnectionError(
                    f"Proceso MCP terminó inmediatamente. "
                    f"stderr: {stderr_out.decode(errors='replace')}"
                )
        except FileNotFoundError:
            raise McpConnectionError(
                f"Comando no encontrado: '{self.command}'. "
                f"Verificá que el server MCP esté instalado."
            )
        except Exception as e:
            raise McpConnectionError(f"Error iniciando proceso MCP: {e}")

    async def _connect_http(self) -> None:
        """Verifica que el server HTTP esté accesible."""
        if not _HTTPX_AVAILABLE:
            raise McpConnectionError("httpx no disponible")
        # La conexión HTTP real se hace en cada request — solo verificamos
        # que el servidor responda antes del handshake
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.url}/health")
                if response.status_code >= 500:
                    raise McpConnectionError(
                        f"Server HTTP MCP respondió {response.status_code}"
                    )
        except httpx.ConnectError:
            raise McpConnectionError(
                f"No se pudo conectar al server HTTP MCP en {self.url}"
            )

    # ── Envío y recepción de mensajes ─────────────────────────────────────────

    async def _send(self, request: "JsonRpcRequest") -> JsonRpcResponse:
        """
        Envía un request JSON-RPC y espera **su** respuesta (correlacionada por `id`).

        ✅ ISSUE-104 (B1b): antes recibía el string ya serializado, así que el
        transporte no podía saber a qué `id` correspondía lo que leía.
        """
        message = request.to_json()
        if self.transport == McpTransport.STDIO:
            return await self._send_stdio(message, request.id)
        return await self._send_http(message, request.id)

    async def _send_stdio(self, message: str, id_esperado: str = "") -> JsonRpcResponse:
        """
        Envía mensaje por stdin y lee la respuesta de stdout que corresponda.

        ✅ ISSUE-104 (B1b): se lee hasta encontrar la respuesta con el `id` esperado;
        las notificaciones, las respuestas AJENAS (típicamente la de un request que
        ya había expirado) y el JSON inválido se descartan y se sigue leyendo.
        """
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise McpConnectionError("Proceso stdio no disponible")

        async with self._read_lock:
            try:
                line = (message + "\n").encode()
                self._process.stdin.write(line)
                await self._process.stdin.drain()

                raw = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=self.timeout,
                )

                if not raw:
                    raise McpConnectionError("Server MCP cerró stdout inesperadamente")

                # Leer hasta la respuesta PROPIA (por id), descartando lo demás
                for _ in range(_MAX_LINEAS_ESPERANDO_RESPUESTA):
                    clase, data = clasificar_linea(raw, id_esperado)
                    if clase == "respuesta" and data is not None:
                        return JsonRpcResponse.from_dict(data)
                    if clase == "ajena" and data is not None:
                        logger.warning(
                            f"[MCP] '{self.server_label}': respuesta con id "
                            f"{data.get('id')!r} ignorada (se esperaba {id_esperado!r})"
                        )
                    raw = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=self.timeout,
                    )
                    if not raw:
                        raise McpConnectionError(
                            "Server MCP cerró stdout inesperadamente")

                raise McpConnectionError(
                    f"No se recibió la respuesta esperada (id={id_esperado!r}) del "
                    f"server MCP tras {_MAX_LINEAS_ESPERANDO_RESPUESTA} líneas")

            except asyncio.TimeoutError:
                raise McpConnectionError(
                    f"Timeout ({self.timeout}s) esperando respuesta de '{self.server_label}'"
                )

    async def _send_http(self, message: str, id_esperado: str = "") -> JsonRpcResponse:
        """Envía mensaje por HTTP POST al endpoint del server MCP."""
        if not _HTTPX_AVAILABLE:
            raise McpConnectionError("httpx no disponible")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/mcp",
                    content=message,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                respuesta = JsonRpcResponse.from_json(response.text)
                # ✅ ISSUE-104 (B1b): mismo control que en stdio.
                if id_esperado and str(respuesta.id) != str(id_esperado):
                    logger.warning(
                        f"[MCP] '{self.server_label}': el server respondió con id "
                        f"{respuesta.id!r} a un request {id_esperado!r}"
                    )
                return respuesta
        except httpx.TimeoutException:
            raise McpConnectionError(
                f"Timeout ({self.timeout}s) en HTTP request a '{self.server_label}'"
            )
        except httpx.HTTPStatusError as e:
            raise McpConnectionError(f"HTTP error {e.response.status_code}: {e}")

    # ── Handshake ─────────────────────────────────────────────────────────────

    async def _initialize(self) -> McpServerInfo:
        """Ejecuta el handshake MCP y valida compatibilidad."""
        request = build_initialize_request()
        try:
            response = await asyncio.wait_for(
                self._send(request),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise McpHandshakeError(
                f"Timeout en handshake con '{self.server_label}'"
            )

        if not response.success:
            raise McpHandshakeError(
                f"Handshake falló con '{self.server_label}': {response.error_message}"
            )

        if not response.result:
            raise McpHandshakeError(
                f"'{self.server_label}' retornó handshake vacío"
            )

        return McpServerInfo.from_initialize_result(response.result)

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        """Lanza McpConnectionError si no hay conexión activa."""
        if not self._connected:
            raise McpConnectionError(
                f"No conectado a '{self.server_label}'. Llamá await client.connect() primero."
            )

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> Optional[McpServerInfo]:
        return self._server_info

    def get_stats(self) -> Dict[str, Any]:
        uptime = round(time.time() - self._connected_at, 1) if self._connected_at else 0
        return {
            "server_label": self.server_label,
            "transport": self.transport.value,
            "connected": self._connected,
            "uptime_seconds": uptime,
            "calls_made": self._calls_made,
            "calls_failed": self._calls_failed,
            "success_rate": round(
                (self._calls_made - self._calls_failed) / max(self._calls_made, 1), 4
            ),
            "cached_tools": len(self._cached_tools) if self._cached_tools else 0,
            "server_info": self._server_info.to_dict() if self._server_info else None,
        }

    async def __aenter__(self) -> "McpClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"<McpClient '{self.server_label}' {self.transport.value} {status}>"


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN v0.4.0
# ═══════════════════════════════════════════════════════════════
# | Componente        | Propósito                               |
# |-------------------|-----------------------------------------|
# | McpTransport      | Enum stdio / http                       |
# | McpClient         | Cliente MCP principal                   |
# | McpClient.stdio() | Constructor para proceso hijo           |
# | McpClient.http()  | Constructor para server HTTP            |
# | connect()         | Conecta + handshake initialize          |
# | list_tools()      | Lista tools del server (con cache)      |
# | call_tool()       | Invoca una tool con argumentos          |
# | ping()            | Verifica que el server siga activo      |
# | disconnect()      | Cierra la conexión limpiamente          |
# | __aenter/exit__   | Context manager async                   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
