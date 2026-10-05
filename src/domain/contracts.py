#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/src/domain/contracts.py
# Autor: SaintWick
"""Protocolos para inyección de dependencias en AXIOMA."""

from typing import Protocol, Optional, List, Dict, Any

# ✅ CORREGIDO: MessageRole ahora está en entities.py, no en types.py
from src.domain.types import TaskType
from src.domain.entities import MessageRole


class LLMClientProtocol(Protocol):
    """Protocolo para clientes LLM."""

    def health_check(self) -> bool:
        """Verifica si el cliente LLM está disponible."""
        ...

    async def generate(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Genera respuesta del LLM."""
        ...

    async def generate_with_context(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
    ) -> str:
        """Genera respuesta con contexto conversacional."""
        ...


class MemoryGatewayProtocol(Protocol):
    """Protocolo para gateway de memoria."""

    def save_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Guarda un mensaje en memoria."""
        ...

    def get_conversation(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Obtiene conversación de una sesión."""
        ...

    def search_semantic(
        self, query: str, limit: int = 5, threshold: float = 0.7
    ) -> List[Dict[str, Any]]:
        """Búsqueda semántica en memoria vectorial."""
        ...

    def clear_session(self, session_id: str) -> None:
        """Limpia memoria de una sesión."""
        ...


class RouterProtocol(Protocol):
    """Protocolo para router de intenciones."""

    def classify(self, query: str) -> TaskType:
        """Clasifica una consulta en un TaskType."""
        ...

    def get_model_for_task(self, task_type: TaskType) -> str:
        """Obtiene el modelo recomendado para una tarea."""
        ...

    def get_temperature_for_task(self, task_type: TaskType) -> float:
        """Obtiene la temperatura recomendada para una tarea."""
        ...
