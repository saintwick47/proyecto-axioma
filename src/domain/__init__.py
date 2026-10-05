#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/src/domain/__init__.py
# Autor: SaintWick
"""
Dominio puro de AXIOMA — cero imports externos.
✅ CORREGIDO: MessageRole se importa desde entities.py (no desde types.py)
"""

# Tipos y enums
from src.domain.types import TaskType, AgentRole

# Entidades (incluye MessageRole)
from src.domain.entities import MessageRole, Message, Session, Response, AgentState, AgentExecution

# Excepciones
from src.domain.exceptions import (
    AxiomaError,
    ValidationError,
    LLMError,
    MemoryError,
    RouterError,
    AgentError,
    ConfigurationError
)

# Contratos/Protocolos
from src.domain.contracts import (
    LLMClientProtocol,
    MemoryGatewayProtocol,
    RouterProtocol
)

# Export público
__all__ = [
    # Tipos
    "TaskType",
    "AgentRole",

    # Entidades
    "MessageRole",
    "Message",
    "Session",
    "Response",
    "AgentState",
    "AgentExecution",

    # Excepciones
    "AxiomaError",
    "ValidationError",
    "LLMError",
    "MemoryError",
    "RouterError",
    "AgentError",
    "ConfigurationError",

    # Protocolos
    "LLMClientProtocol",
    "MemoryGatewayProtocol",
    "RouterProtocol",
]
