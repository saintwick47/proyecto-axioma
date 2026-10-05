#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.1 — Tool Base (Contrato Unificado)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/tools_mcp/tool_base.py
# Autor: SaintWick
# Fecha: 2026-04-11
#
# Propósito: Contrato base para toda herramienta en AXIOMA.
#            Define BaseTool, ToolResult, ToolSchema y ToolPermission.
#            Cualquier capacidad real del sistema (API, filesystem,
#            terminal, MCP externa) implementa esta interfaz.
#
# Filosofía JARVIS:
#   Una tool no es solo una función — es una capacidad con nombre,
#   descripción, permisos y trazabilidad. El sistema nervioso central
#   (ToolRegistry) las orquesta. Los agentes las invocan sin conocer
#   la implementación subyacente.
#
# Uso:
#   class WeatherTool(BaseTool):
#       name = "weather"
#       description = "Obtiene el clima actual para una ubicación"
#       permission_level = ToolPermission.NETWORK
#
#       def _run(self, query: str, **kwargs) -> ToolResult:
#           ...
# ═══════════════════════════════════════════════════════════════
# ✅ v0.4.0 — Archivo nuevo, sin dependencias internas de AXIOMA.
#             Import seguro total — puede cargarse en cualquier contexto.
# ✅ v0.4.1 — __init_subclass__ verifica _is_dynamic_tool flag
#               para permitir tools que setean attributes en __init__
# ✅ v0.5.0 (Fase 1) — ToolSchema.input_schema: campo JSON Schema opcional.
#             BaseTool.validate_input(): validación pre-ejecución contra JSON Schema.
#             Retrocompatible: tools sin input_schema solo validan required desde params.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

# ✅ CORREGIDO v0.5.0: Import seguro jsonschema — no rompe si no está instalado
try:
    import jsonschema as _jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _jsonschema = None  # type: ignore[assignment]
    _JSONSCHEMA_AVAILABLE = False

logger = logging.getLogger(__name__)

# ============================================================================
# PERMISSION LEVELS — Sistema de permisos de 5 niveles (0-4)
# ============================================================================
class ToolPermission(IntEnum):
    """
    Niveles de permiso para ejecución de tools.
    Jerarquía inspirada en JARVIS — cada nivel habilita el anterior:
    READ_ONLY   → Solo lectura, sin efectos secundarios
    LOCAL_WRITE → Escritura local controlada
    EXECUTE     → Ejecutar procesos en el sistema
    NETWORK     → Llamadas a APIs y servicios externos
    PRIVILEGED  → Operaciones destructivas o de alto impacto

    El ToolExecutor valida el nivel requerido contra el nivel
    permitido por la sesión antes de cada ejecución.
    """
    READ_ONLY   = 0   # read_file, list_dir, git_log, sys_env, health
    LOCAL_WRITE = 1   # write_file, fs_delete, fs_move, git_commit (wrapper)
    EXECUTE     = 2   # terminal_exec, dev_lint, dev_run_tests
    NETWORK     = 3   # weather, finance, news, web_search, http_get/post
    PRIVILEGED  = 4   # operaciones destructivas, deploy, process kill


# ============================================================================
# TOOL SCHEMA — Descripción de parámetros (para MCP y auto-documentación)
# ============================================================================
@dataclass
class ToolParam:
    """Descriptor de un parámetro de tool."""
    name: str
    type: str                          # "str", "int", "bool", "dict", "list"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class ToolSchema:
    """
    Schema completo de una tool — usado por el protocolo MCP para
    exponer capacidades a clientes externos y para auto-documentación
    interna del sistema.

    El bridge MCP convierte este schema al formato JSON-RPC esperado
    por el protocolo sin modificar la tool en sí.

    ✅ v0.5.0 (Fase 1):
        input_schema: JSON Schema estándar (Draft 7) opcional.
        Si se define, BaseTool.validate_input() lo usa para validación
        estricta con jsonschema. Si no, validate_input() cae back a
        validar solo campos required desde params. 100% retrocompatible.

    Ejemplo de input_schema:
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "encoding": {"type": "string", "enum": ["utf-8", "latin-1"]}
            },
            "required": ["path"],
            "additionalProperties": False
        }
    """
    name: str
    description: str
    params: List[ToolParam] = field(default_factory=list)
    returns: str = "str"               # Tipo de retorno como string descriptivo
    examples: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    # ✅ CORREGIDO v0.5.0: JSON Schema estándar opcional para validación estricta
    input_schema: Optional[Dict[str, Any]] = None

    def to_mcp_dict(self) -> Dict[str, Any]:
        """
        Serializa al formato de tool description del protocolo MCP.
        Usado por mcp_tool_bridge.py para registrar tools en servers externos.

        ✅ v0.5.0: Si input_schema está definido, lo usa directamente como
        inputSchema (más preciso). Si no, genera desde params como antes.
        """
        # ✅ CORREGIDO v0.5.0: Priorizar input_schema explícito si existe
        if self.input_schema is not None:
            return {
                "name": self.name,
                "description": self.description,
                "inputSchema": self.input_schema,
            }

        properties: Dict[str, Any] = {}
        required_params: List[str] = []

        for p in self.params:
            properties[p.name] = {
                "type": p.type,
                "description": p.description,
            }
            if p.default is not None:
                properties[p.name]["default"] = p.default
            if p.required:
                required_params.append(p.name)

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required_params,
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialización interna para logging y métricas."""
        return {
            "name": self.name,
            "description": self.description,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "required": p.required,
                    "default": p.default,
                }
                for p in self.params
            ],
            "returns": self.returns,
            "tags": self.tags,
            "has_input_schema": self.input_schema is not None,  # ✅ CORREGIDO v0.5.0
        }


# ============================================================================
# TOOL RESULT — Resultado unificado de cualquier tool
# ============================================================================
@dataclass
class ToolResult:
    """
    Resultado unificado de ejecución de cualquier tool en AXIOMA.
    Siempre tiene éxito o error explícito — nunca lanza excepciones
    al llamador. El ToolExecutor captura todo y encapsula aquí.

    Atributos:
        success     : True si la tool ejecutó sin error
        output      : Contenido principal (string para compatibilidad LLM)
        data        : Datos estructurados opcionales (dict/list)
        error       : Mensaje de error si success=False
        tool_name   : Nombre de la tool que generó este resultado
        latency_ms  : Tiempo de ejecución en milisegundos
        source      : Origen ("local", "api", "mcp", "cache")
        metadata    : Metadatos adicionales de trazabilidad
    """
    success: bool
    output: str
    tool_name: str
    latency_ms: float = 0.0
    data: Optional[Any] = None
    error: Optional[str] = None
    source: str = "local"              # "local" | "api" | "mcp" | "cache"
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        output: str,
        tool_name: str,
        latency_ms: float = 0.0,
        data: Optional[Any] = None,
        source: str = "local",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolResult":
        """Factory para resultado exitoso — el camino feliz."""
        return cls(
            success=True,
            output=output,
            tool_name=tool_name,
            latency_ms=latency_ms,
            data=data,
            source=source,
            metadata=metadata or {},
        )

    @classmethod
    def fail(
        cls,
        error: str,
        tool_name: str,
        latency_ms: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ToolResult":
        """Factory para resultado fallido — error explícito y trazable."""
        return cls(
            success=False,
            output="",
            tool_name=tool_name,
            latency_ms=latency_ms,
            error=error,
            metadata=metadata or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialización para logging, métricas y respuestas API."""
        return {
            "success": self.success,
            "output": self.output,
            "tool_name": self.tool_name,
            "latency_ms": round(self.latency_ms, 2),
            "data": self.data,
            "error": self.error,
            "source": self.source,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        status = "✅" if self.success else "❌"
        return (
            f"ToolResult({status} {self.tool_name!r} "
            f"{self.latency_ms:.1f}ms source={self.source!r})"
        )


# ============================================================================
# BASE TOOL — Contrato abstracto para todas las tools
# ============================================================================
class BaseTool(ABC):
    """
    Contrato base para toda herramienta en AXIOMA.

    Implementar una tool nueva es simple:
    1. Heredar de BaseTool
    2. Definir name, description, permission_level
    3. Implementar _run() — la lógica real
    4. Registrar en ToolRegistry (automático si usás @registry.register)

    El ToolExecutor se encarga de:
    - Validar permisos antes de llamar
    - Validar input contra JSON Schema (validate_input) antes de ejecutar
    - Medir latencia
    - Capturar excepciones y convertirlas en ToolResult.fail()
    - Logging y trazabilidad

    La tool NO necesita manejar errores internamente salvo los
    específicos de su dominio — los genéricos los maneja el executor.

    Atributos de clase (OBLIGATORIOS en subclases):
        name             : Identificador único, snake_case
        description      : Descripción legible para el LLM y el sistema MCP
        permission_level : ToolPermission requerido para ejecutar

    Atributos de clase (OPCIONALES):
        tags             : Categorías para filtrado en el registry
        schema           : ToolSchema completo (auto-generado si no se define)

    ✅ CORREGIDO v0.4.1:
        _is_dynamic_tool : Flag para tools que setean atributos en __init__
                           (ej: McpDynamicTool del MCP Bridge)

    ✅ CORREGIDO v0.5.0 (Fase 1):
        validate_input() : Valida kwargs antes de _run(). Si el schema tiene
                           input_schema, usa jsonschema. Si no, valida required
                           desde params. El executor lo llama automáticamente.
    """
    # ── Definir en cada subclase ──────────────────────────────────────────────
    name: str = ""
    description: str = ""
    permission_level: ToolPermission = ToolPermission.READ_ONLY
    tags: List[str] = []

    # ── Schema opcional — si no se define, se genera desde name+description ──
    schema: Optional[ToolSchema] = None

    # ✅ CORREGIDO v0.4.1: Flag para tools dinámicas (MCP Bridge)
    _is_dynamic_tool: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """
        Validación en tiempo de definición de clase.
        Asegura que todas las tools tengan name y description.
        Falla rápido — mejor en import que en runtime.

        ✅ CORREGIDO v0.4.1:
            - Verifica _is_dynamic_tool flag
            - Si es True, salta validación de description (se setea en __init__)
            - McpDynamicTool usa este flag para evitar error en import
        """
        super().__init_subclass__(**kwargs)

        # Solo validar clases concretas (no ABCs intermedias)
        if not getattr(cls, "__abstractmethods__", None):
            # Validar name — siempre requerido
            if not cls.name:
                raise TypeError(
                    f"Tool '{cls.__name__}' debe definir el atributo 'name'."
                )

            # ✅ CORREGIDO v0.4.1: Validar description solo si NO es dinámica
            is_dynamic = getattr(cls, "_is_dynamic_tool", False)

            if not is_dynamic and not cls.description:
                raise TypeError(
                    f"Tool '{cls.__name__}' (name={cls.name!r}) debe definir 'description'."
                )

    # ── Validación de input — llamar antes de _run() ─────────────────────────

    def validate_input(self, params: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        ✅ CORREGIDO v0.5.0 (Fase 1): Valida los parámetros de entrada.

        Estrategia en dos niveles:
          1. Si el schema define input_schema (JSON Schema estándar) y jsonschema
             está disponible → validación estricta con tipos, enum, minLength, etc.
          2. Fallback: validar solo que los campos required de params estén presentes.
             Retrocompatible con todas las tools existentes que no definen input_schema.

        Args:
            params: kwargs que se pasarán a _run().

        Returns:
            (True, None)          → validación OK
            (False, "mensaje")    → validación fallida con motivo

        Ejemplo de uso en ToolExecutor (antes de llamar _run):
            ok, err = tool.validate_input(kwargs)
            if not ok:
                return ToolResult.fail(error=f"Validación fallida: {err}", tool_name=tool.name)
        """
        schema = self.get_schema()

        # ── Nivel 1: JSON Schema estricto ───────────────────────────────────
        if schema.input_schema is not None:
            if not _JSONSCHEMA_AVAILABLE:
                # jsonschema no instalado: advertir y caer a nivel 2
                logger.warning(
                    f"[{self.name}] jsonschema no disponible — "
                    "saltando validación estricta. Instalar: pip install jsonschema"
                )
            else:
                try:
                    _jsonschema.validate(
                        instance=params,
                        schema=schema.input_schema,
                        # Draft 7 es suficiente y el más compatible
                        cls=_jsonschema.Draft7Validator,
                    )
                    return True, None
                except _jsonschema.ValidationError as e:
                    # e.message da el error puntual; e.path da la ruta al campo
                    field_path = " → ".join(str(p) for p in e.absolute_path) or "root"
                    return False, f"[{field_path}] {e.message}"
                except _jsonschema.SchemaError as e:
                    # El schema mismo está mal definido — error del desarrollador
                    logger.error(f"[{self.name}] input_schema inválido: {e.message}")
                    return False, f"Schema de tool inválido: {e.message}"

        # ── Nivel 2: Validación de required desde params (fallback) ─────────
        if schema.params:
            required_fields = [p.name for p in schema.params if p.required]
            missing = [f for f in required_fields if f not in params]
            if missing:
                return False, f"Parámetros requeridos ausentes: {missing}"

        return True, None

    # ── Interfaz pública — NO sobreescribir en subclases ─────────────────────
    def run(self, **kwargs: Any) -> ToolResult:
        """
        Punto de entrada principal — sync.
        ✅ v0.5.0: Valida input antes de ejecutar. Si falla, retorna ToolResult.fail()
        sin llamar a _run(). Mide latencia total (validación + ejecución).
        El ToolExecutor llama este método (no _run directamente).
        """
        t0 = time.perf_counter()

        # ✅ CORREGIDO v0.5.0: Validar input antes de ejecutar
        valid, validation_error = self.validate_input(kwargs)
        if not valid:
            latency = (time.perf_counter() - t0) * 1000
            logger.warning(f"[{self.name}] Validación de input fallida: {validation_error}")
            return ToolResult.fail(
                error=f"Validación de input: {validation_error}",
                tool_name=self.name,
                latency_ms=latency,
                metadata={"validation_failed": True},
            )

        try:
            result = self._run(**kwargs)
            # Actualizar latencia si _run no la calculó
            if result.latency_ms == 0.0:
                result.latency_ms = (time.perf_counter() - t0) * 1000
            return result
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            tb = traceback.format_exc()
            logger.error(
                f"[{self.name}] Excepción no capturada: {e}\n{tb}"
            )
            return ToolResult.fail(
                error=f"{type(e).__name__}: {e}",
                tool_name=self.name,
                latency_ms=latency,
                metadata={"traceback": tb[:500]},
            )

    async def arun(self, **kwargs: Any) -> ToolResult:
        """
        Punto de entrada async.
        Por defecto, delega a run() en un thread para no bloquear el event loop.
        Las tools verdaderamente async (MCP, HTTP) sobreescriben este método.
        ✅ v0.5.0: La validación de input ocurre en run(), incluida aquí por delegación.
        """
        import asyncio
        # ✅ FIX: get_running_loop() — arun() es async, siempre hay loop activo
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.run(**kwargs))

    # ── Interfaz a implementar ────────────────────────────────────────────────
    @abstractmethod
    def _run(self, **kwargs: Any) -> ToolResult:
        """
        Lógica real de la tool.
        Siempre retorna ToolResult — usar ToolResult.ok() o ToolResult.fail().
        No lanzar excepciones salvo las muy específicas del dominio.
        El método run() captura el resto.
        PRE-CONDICIÓN: validate_input() ya fue llamado y retornó (True, None).
        """
        pass

    # ── Metadata y utilidades ─────────────────────────────────────────────────
    def get_schema(self) -> ToolSchema:
        """
        Retorna el schema de la tool.
        Si la subclase no definió uno, genera uno mínimo desde name+description.
        """
        if self.schema is not None:
            return self.schema
        return ToolSchema(
            name=self.name,
            description=self.description,
            tags=list(self.tags),
        )

    def is_available(self) -> bool:
        """
        Verifica si la tool está operativa.
        Sobreescribir para tools que requieren verificación externa
        (API key presente, servicio activo, etc.)
        """
        return True

    def describe(self) -> Dict[str, Any]:
        """Descripción completa para el registry y logging."""
        schema = self.get_schema()
        return {
            "name": self.name,
            "description": self.description,
            "permission_level": self.permission_level,
            "permission_name": ToolPermission(self.permission_level).name,
            "tags": list(self.tags),
            "available": self.is_available(),
            "schema": schema.to_dict(),
            "has_strict_validation": schema.input_schema is not None,  # ✅ v0.5.0
        }

    def __repr__(self) -> str:
        perm = ToolPermission(self.permission_level).name
        return f"<Tool name={self.name!r} permission={perm}>"


# ============================================================================
# TOOL ERROR — Excepciones específicas del subsistema de tools
# ============================================================================
class ToolError(Exception):
    """Error base del subsistema de tools."""
    def __init__(self, message: str, tool_name: str = "", **kwargs: Any):
        super().__init__(message)
        self.tool_name = tool_name
        self.extra = kwargs


class ToolPermissionError(ToolError):
    """La tool requiere un nivel de permiso mayor al permitido en la sesión."""
    def __init__(
        self,
        tool_name: str,
        required: ToolPermission,
        allowed: ToolPermission,
    ):
        super().__init__(
            f"Tool '{tool_name}' requiere permiso {required.name} "
            f"(nivel {required}), pero el máximo permitido es "
            f"{allowed.name} (nivel {allowed}).",
            tool_name=tool_name,
        )
        self.required = required
        self.allowed = allowed


class ToolNotFoundError(ToolError):
    """La tool solicitada no está registrada en el ToolRegistry."""
    def __init__(self, tool_name: str):
        super().__init__(
            f"Tool '{tool_name}' no encontrada en el registry. "
            "Verificá que esté registrada antes de usarla.",
            tool_name=tool_name,
        )


class ToolTimeoutError(ToolError):
    """La tool excedió el tiempo máximo de ejecución."""
    def __init__(self, tool_name: str, timeout_seconds: float):
        super().__init__(
            f"Tool '{tool_name}' excedió el timeout de {timeout_seconds}s.",
            tool_name=tool_name,
        )
        self.timeout_seconds = timeout_seconds


class ToolValidationError(ToolError):
    """✅ NUEVO v0.5.0: Input de la tool no superó validación de schema."""
    def __init__(self, tool_name: str, reason: str):
        super().__init__(
            f"Tool '{tool_name}' rechazó el input: {reason}",
            tool_name=tool_name,
        )
        self.reason = reason


# ═══════════════════════════════════════════════════════════════
# 📋 CAMBIOS REALIZADOS v0.5.0 (Fase 1)
# ═══════════════════════════════════════════════════════════════
# | Línea  | Tipo      | Descripción                                          | Impacto                        |
# |--------|-----------|------------------------------------------------------|--------------------------------|
# | 43-48  | NUEVO     | Import seguro jsonschema con flag _JSONSCHEMA_AVAILABLE | Sin breaking change si ausente |
# | 41     | NUEVO     | Tuple agregado a imports typing                      | Requerido por validate_input   |
# | ~112   | NUEVO     | ToolSchema.input_schema: Optional[Dict] = None       | JSON Schema estándar opcional  |
# | ~118   | CORREGIDO | to_mcp_dict(): prioriza input_schema si existe       | MCP más preciso                |
# | ~153   | CORREGIDO | to_dict(): agrega has_input_schema                   | Introspección y logging        |
# | ~265   | NUEVO     | BaseTool.validate_input(): 2 niveles de validación   | Core de Fase 1                 |
# | ~312   | CORREGIDO | run(): llama validate_input() antes de _run()         | Validación automática          |
# | ~340   | CORREGIDO | arun(): nota que validación ocurre vía run()          | Consistencia async/sync        |
# | ~370   | CORREGIDO | describe(): agrega has_strict_validation              | Observabilidad                 |
# | ~410   | NUEVO     | ToolValidationError: excepción específica de schema  | Jerarquía de errores completa  |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
