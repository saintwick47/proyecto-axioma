#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.2.0 — Entidades del Dominio
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/domain/entities.py
# Autor: SaintWick
# Fecha: 2026-03-20
# Propósito: Modelos de datos puros (sin lógica de negocio) + FIX metadata para UI
#            ✅ v4.2.0: ValidationScore movido desde validator.py al dominio
# ═══════════════════════════════════════════════════════════════
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict

# ✅ 2026-09-29 — hallado con USO REAL (no con tests): el marcado INTERNO del
# grounding se filtraba a la respuesta visible. Medido:
#   "…Este dato fue proporcionado por Instagram
#    (<fuente_externa>Instagram</fuente_externa>) y también confirmado por…"
# Los modelos chicos copian las etiquetas del prompt. En vez de limpiar en cada
# agente (searcher, researcher, chat…), se limpia en el ÚNICO punto por el que
# pasan TODAS las respuestas: `Response` y `AgentResult`.
_MARCADO_INTERNO = re.compile(r"</?fuente_externa[^>]*>", re.IGNORECASE)


def limpiar_marcado_interno(texto: str) -> str:
    """Quita el marcado interno del grounding de un texto visible al usuario.

    Solo actúa si el texto menciona `fuente_externa` (camino rápido): el 99,9 %
    de las respuestas no lo hace y no paga ningún costo.
    """
    if not texto or "fuente_externa" not in texto.lower():
        return texto
    limpio = _MARCADO_INTERNO.sub("", texto)
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return limpio.strip()


class MessageRole(str, Enum):
    """Roles válidos para mensajes en el sistema."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(BaseModel):
    """
    Mensaje intercambiado en el sistema.
    Atributos:
        content: Contenido textual del mensaje (requerido)
        role: Rol del emisor (user/assistant/system/tool)
        timestamp: Marca de tiempo UTC automática
        metadata: Datos adicionales opcionales (ej: source, confidence)
        tool_calls: Llamadas a herramientas si aplica
        tool_call_id: ID para correlacionar respuesta de herramienta
    """
    model_config = ConfigDict(frozen=False, extra="allow")

    content: str = Field(..., min_length=1, description="Contenido del mensaje")
    role: MessageRole = Field(default=MessageRole.USER, description="Rol del emisor")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Marca de tiempo UTC"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Metadatos opcionales (source, confidence, etc.)"
    )
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Llamadas a herramientas externas"
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="ID para correlacionar con tool_calls"
    )

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        """Validar que el contenido no esté vacío tras limpiar whitespace."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("content no puede estar vacío o ser solo whitespace")
        return cleaned

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Limitar tamaño de metadata para evitar abuso de memoria."""
        if len(v) > 50:
            raise ValueError("metadata no puede tener más de 50 claves")
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Convertir a diccionario para serialización."""
        return self.model_dump(mode="json")

    def __str__(self) -> str:
        return f"{self.role.value}: {self.content[:100]}{'...' if len(self.content) > 100 else ''}"


class Session(BaseModel):
    """
    Sesión de conversación con un usuario.
    Atributos:
        session_id: Identificador único (UUID recomendado)
        user_id: Identificador del usuario (anonimizado si es público)
        messages: Historial de mensajes en orden cronológico
        created_at: Marca de creación UTC
        updated_at: Última actualización UTC
        context: Contexto acumulado para retrieval (resumido)
        metadata: Datos de sesión (idioma, timezone, preferencias)
    """
    model_config = ConfigDict(frozen=False, extra="allow")

    session_id: str = Field(..., min_length=1, description="ID único de sesión")
    user_id: Optional[str] = Field(default=None, description="ID de usuario (opcional)")
    messages: List[Message] = Field(
        default_factory=list,
        description="Historial de mensajes",
        max_length=1000
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    context: str = Field(
        default="",
        max_length=4000,
        description="Contexto resumido para retrieval"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Preferencias de sesión (idioma, timezone, etc.)"
    )

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        """Validar formato de session_id."""
        if not v.strip():
            raise ValueError("session_id no puede estar vacío")
        return v.strip()

    def add_message(self, message: Message) -> "Session":
        """Agregar mensaje y actualizar timestamp."""
        self.messages.append(message)
        self.updated_at = datetime.now(timezone.utc)
        if len(self.messages) > 1000:
            self.messages = self.messages[-1000:]
        return self

    def get_recent_messages(self, n: int = 10) -> List[Message]:
        """Obtener últimos N mensajes."""
        return self.messages[-n:] if self.messages else []

    def to_dict(self) -> Dict[str, Any]:
        """Convertir a diccionario para persistencia."""
        return self.model_dump(mode="json")


# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: Response CON campo metadata para UI de código
# ═══════════════════════════════════════════════════════════════
class Response(BaseModel):
    """
    Respuesta generada por el sistema AXIOMA.
    Atributos:
        content: Contenido de la respuesta
        session_id: Sesión asociada (para trazabilidad)
        model_used: Modelo que generó la respuesta
        task_type: Tipo de tarea procesada (de TaskType enum)
        latency_ms: Tiempo de generación en milisegundos
        confidence: Confianza del sistema en la respuesta (0.0-1.0)
        sources: Fuentes utilizadas (para research/validation)
        validation_passed: Si pasó validación de CoT
        error: Mensaje de error si falló (None si éxito)
        presentation_style: Estilo de presentación (direct|explanatory)
        include_follow_up: Si incluir oferta de ayuda al final
        confidence_label: Etiqueta de confianza (Alto|Moderado|Bajo)
        metadata: Datos adicionales para UI (has_code, extracted_code, etc.)
    """
    model_config = ConfigDict(frozen=False, extra="allow")

    content: str = Field(..., description="Contenido de la respuesta")
    session_id: Optional[str] = Field(default=None, description="Sesión asociada")
    model_used: str = Field(..., description="Modelo utilizado para generar")
    task_type: str = Field(..., description="Tipo de tarea procesada")
    latency_ms: float = Field(ge=0, description="Latencia en milisegundos")
    confidence: float = Field(
        ge=0.0, le=1.0, default=0.5, description="Confianza (0.0-1.0)"
    )
    sources: List[str] = Field(
        default_factory=list, description="Fuentes consultadas"
    )
    validation_passed: bool = Field(
        default=False, description="Si pasó validación de CoT"
    )
    error: Optional[str] = Field(default=None, description="Error si falló")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    presentation_style: str = Field(
        default="direct",
        description="Estilo de presentación: direct|explanatory"
    )
    include_follow_up: bool = Field(
        default=True,
        description="Si incluir oferta de ayuda al final"
    )
    confidence_label: Optional[str] = Field(
        default=None,
        description="Etiqueta de confianza: Alto|Moderado|Bajo"
    )
    # ✅ CRÍTICO: Campo metadata para pasar información de agentes a UI
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Metadata adicional (has_code, extracted_code, suggested_filename, etc.)"
    )

    @field_validator("content")
    @classmethod
    def _content_sin_marcado_interno(cls, v: str) -> str:
        """✅ 2026-09-29: ninguna respuesta visible puede llevar `<fuente_externa>`.

        Es el choke point: todo lo que llega al usuario (CLI, web, AgentResult →
        Response) pasa por acá.
        """
        return limpiar_marcado_interno(v)

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        """Redondear confianza a 2 decimales para consistencia."""
        return round(v, 2)

    @field_validator("presentation_style")
    @classmethod
    def validate_presentation_style(cls, v: str) -> str:
        """Validar estilo de presentación."""
        if v not in ["direct", "explanatory"]:
            return "direct"
        return v

    @field_validator("confidence_label")
    @classmethod
    def validate_confidence_label(cls, v: Optional[str]) -> Optional[str]:
        """Validar etiqueta de confianza."""
        if v is not None and v not in ["Alto", "Moderado", "Bajo"]:
            return None
        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Limitar tamaño de metadata para evitar abuso de memoria."""
        if v is not None and len(v) > 50:
            raise ValueError("metadata no puede tener más de 50 claves")
        return v

    @property
    def is_success(self) -> bool:
        """Verificar si la respuesta fue exitosa."""
        return self.error is None

    def to_message(self, role: MessageRole = MessageRole.ASSISTANT) -> Message:
        """Convertir respuesta a mensaje para historial."""
        return Message(
            content=self.content,
            role=role,
            metadata={
                "model": self.model_used,
                "task_type": self.task_type,
                "confidence": self.confidence,
                "latency_ms": self.latency_ms,
                "presentation_style": self.presentation_style,
                "include_follow_up": self.include_follow_up,
                "confidence_label": self.confidence_label,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convertir a diccionario para logging/respuesta API."""
        return {
            **self.model_dump(mode="json"),
            "metadata": self.metadata,
        }


# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: AgentState AHORA INCLUYE FAILED
# ═══════════════════════════════════════════════════════════════
class AgentState(str, Enum):
    """Estados posibles de un agente en ejecución."""
    IDLE = "idle"
    PROCESSING = "processing"
    WAITING_TOOL = "waiting_tool"
    VALIDATING = "validating"
    ERROR = "error"
    COMPLETED = "completed"
    # ✅ CORREGIDO: Agregar FAILED para compatibilidad con base.py:_end_execution()
    FAILED = "failed"


class AgentExecution(BaseModel):
    """
    Registro de ejecución de un agente.
    Para auditoría, debugging y métricas de rendimiento.
    """
    model_config = ConfigDict(frozen=False, extra="allow")

    agent_name: str = Field(..., description="Nombre del agente")
    task_type: str = Field(..., description="Tipo de tarea asignada")
    state: AgentState = Field(default=AgentState.IDLE)
    input_summary: str = Field(..., max_length=500, description="Resumen del input")
    output_summary: Optional[str] = Field(
        default=None, max_length=500, description="Resumen del output"
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completed_at: Optional[datetime] = Field(default=None)
    latency_ms: Optional[float] = Field(default=None, ge=0)
    error: Optional[str] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        """Calcular duración si está completado."""
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None

    def complete(self, output_summary: str = None, error: str = None) -> "AgentExecution":
        """Marcar como completado con resultado."""
        self.state = AgentState.ERROR if error else AgentState.COMPLETED
        self.completed_at = datetime.now(timezone.utc)
        self.latency_ms = self.duration_ms
        if output_summary:
            self.output_summary = output_summary[:500]
        if error:
            self.error = error[:500]
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convertir a diccionario para persistencia."""
        return self.model_dump(mode="json")


# ═══════════════════════════════════════════════════════════════
# ✅ v4.2.0: ValidationScore — Movido desde validator.py al dominio
# ═══════════════════════════════════════════════════════════════
class ValidationScore(BaseModel):
    """
    Estructura de datos para scores del ciclo Evaluator-Optimizer.

    Clamp de score entre 0.0 y 10.0 vía field_validator.
    Límite de critique a 500 chars para evitar context drift
    en iteraciones sucesivas del CoderAgent.

    ✅ v4.2.0: Movido desde src/agents/validator.py a este módulo de dominio.
    Pasa de [⚠️ AISLADO] a [✅ CONECTADO] — el AST puede trazar su
    contrato de dominio y su uso en el ciclo Evaluator-Optimizer.

    Uso:
        score = ValidationScore(score=8.5, critique="...", passed=True, details={})
        agent_result.metadata["validation_score"] = score.to_dict()
    """
    model_config = ConfigDict(frozen=False, extra="allow")

    score: float = Field(
        ge=0.0, le=10.0,
        description="Score de validación entre 0.0 y 10.0"
    )
    critique: str = Field(
        default="",
        max_length=500,
        description="Feedback accionable (máx 500 chars para evitar context drift)"
    )
    passed: bool = Field(
        default=False,
        description="Si la validación pasó el threshold"
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Checks detallados (cot, safety, completeness, etc.)"
    )

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: float) -> float:
        """Clamp score entre 0.0 y 10.0 (doble validación)."""
        return max(0.0, min(10.0, round(v, 2)))

    @field_validator("critique")
    @classmethod
    def truncate_critique(cls, v: str) -> str:
        """Limitar critique a 500 chars para evitar context drift."""
        if v and len(v) > 500:
            return v[:500]
        return v or ""

    @field_validator("details")
    @classmethod
    def validate_details(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """Limitar tamaño de details para evitar abuso de memoria."""
        if len(v) > 30:
            raise ValueError("details no puede tener más de 30 claves")
        return v

    def to_dict(self) -> Dict[str, Any]:
        """Convertir a diccionario para metadata de AgentResult."""
        return {
            "score": self.score,
            "critique": self.critique,
            "passed": self.passed,
            "details": self.details,
        }

    def __repr__(self) -> str:
        return f"ValidationScore(score={self.score:.1f}, passed={self.passed})"


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS v4.2.0
# ═══════════════════════════════════════════════════════════════
# | Sección          | Cambio Realizado                        | Impacto AST                |
# |------------------|-----------------------------------------|----------------------------|
# | ValidationScore  | ✅ NUEVO: Movido desde validator.py     | [⚠️ AISLADO] → [✅ CONECTADO] |
# | Contrato dominio | ✅ Pydantic BaseModel (consistente)     | Validación automática      |
# | Clamp score      | ✅ field_validator + ge/le              | Robustez mejorada          |
# | Truncate critique | ✅ field_validator max_length=500       | Anti context drift         |
# | validator.py     | Pendiente: importar desde entities.py   | Elimina definición local   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — FUNCIONALIDAD PRESERVADA — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════
