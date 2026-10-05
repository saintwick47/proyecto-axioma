#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# Ruta: /ruta/a/axioma/src/agents/base.py
# Autor: SaintWick
# base.py - Protocolo base para agentes en AXIOMA.
#
# Proporciona la clase BaseAgent con integración a:
#   - LLM (OllamaClient) con generación y streaming.
#   - Memoria multicapa (MemoryGateway) con contexto y almacenamiento.
#   - Herramientas (ToolExecutor) con permisos y sandbox.
#   - Reintentos con backoff exponencial (versiones síncrona y asíncrona).
#   - Métricas de ejecución y estado.
#
# La clase es abstracta y debe ser extendida por agentes concretos (CoderAgent,
# DataHandler, etc.) que implementen el método `execute()`.
# ──────────────────────────────────────────────────────────────────────────────
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Generator
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
import time
import uuid
import traceback
import threading

from config.settings import settings
from src.domain.types import TaskType, AgentRole, StatsProvider, ExecutableAgent, AgentPriority, RuntimeMode
from src.domain.entities import (Message, Response, AgentState, AgentExecution,
                                 limpiar_marcado_interno)
from src.domain.exceptions import AgentError, LLMError
from src.llm.client import OllamaClient, ScenarioType
from src.llm.client_base import tokens_de_respuesta
from src.memory.gateway import MemoryGateway
from src.utils.degradation import log_degraded

# ── Imports opcionales ───────────────────────────────────────────────────────

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

try:
    from src.prompts import get_prompt_manager, PromptManager, ContextLayer
    PROMPT_MANAGER_AVAILABLE = True
except ImportError:
    PROMPT_MANAGER_AVAILABLE = False
    PromptManager = None
    ContextLayer = None

try:
    from src.tools_mcp import get_executor, ExecutionContext, ToolPermission
    from src.tools_mcp.tool_base import ToolResult
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False
    get_executor = None
    ExecutionContext = None
    ToolPermission = None
    ToolResult = None

logger = logging.getLogger(__name__)


# ── Helpers de logging ───────────────────────────────────────────────────────

def _log_agent(agent_type: str, event: str, task_type: str = "",
               duration: float = None, success: bool = True, error: str = None,
               extra: Dict[str, Any] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(
                agent_name=agent_type, task_type=task_type, state=event,
                duration=duration, success=success, error=error,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 base.py:81] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, agent_type: str = "",
               extra_info: Dict[str, Any] = None):
    if not LOGGER_AVAILABLE:
        logger.error(f"{agent_type} - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(
                error=error, context=context, extra_info=extra_info,
                function_name=f"{agent_type}.{context}",
            )
    except Exception:
        logger.error(f"{agent_type} - {context}: {error}")


def _log_model(agent_type: str, model: str, action: str, task_type: str,
               duration: float = None, success: bool = True,
               input_size: int = None, output_size: int = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_model_call(
                model_name=model, action=action, task_type=task_type,
                duration=duration, success=success,
                input_size=input_size, output_size=output_size,
            )
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 base.py:114] excepción degradada (intencional): {_d2e}")


# ── Clases de configuración ──────────────────────────────────────────────────

@dataclass
class AgentConfig:
    """Configuración estricta para inicialización de agentes."""
    agent_type: str
    model: str = field(default_factory=lambda: settings.llm_default_model)
    temperature: float = 0.7
    max_tokens: int = 512
    # ✅ FIX: Optional — None significa "sin override", el timeout real lo
    # decide OllamaClientBase vía scenario (ver get_timeout_for_scenario).
    # Un valor concreto acá SIEMPRE ganaba y anulaba el timeout por scenario.
    timeout: Optional[int] = None
    # ✅ FIX: scenario explícito por agente (ver BaseAgent.LLM_SCENARIO),
    # reemplaza la detección por substring del nombre del modelo.
    scenario: ScenarioType = ScenarioType.STANDARD
    priority: AgentPriority = AgentPriority.NORMAL
    enable_memory: bool = True
    enable_validation: bool = True
    max_retries: int = 3
    system_prompt: Optional[str] = None


# ✅ ISSUE-082: uso de tokens de la ÚLTIMA llamada al LLM en este hilo.
# `BaseAgent._generate()` corre en el mismo hilo que la construcción del
# `AgentResult` (los agentes se ejecutan vía `asyncio.to_thread`), así que un
# thread-local evita tener que tocar los ~50 sitios que construyen
# `AgentResult`. Se CONSUME una sola vez (ver `__post_init__`).
_USO_LLM = threading.local()


@dataclass
class AgentResult:
    """Estructura de datos PURA para resultados de ejecución de agentes.
    Nota: Cualquier lógica de CoT/CWE/Cache debe residir en los Agentes, no aquí."""
    success: bool
    content: str
    agent_type: str
    model_used: str
    task_type: str
    latency_ms: float
    confidence: float = 0.5
    validation_passed: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """✅ ISSUE-082: adjunta el uso real de tokens de la llamada al LLM.

        `BaseAgent._generate()` deja el uso en `_USO_LLM` (thread-local) y acá se
        **consume una sola vez**, así que no puede quedar pegado a un resultado
        posterior del mismo hilo. Si el agente no llamó al LLM, no hay nada que
        adjuntar y el resultado queda igual que antes.

        ✅ 2026-09-29: además se limpia el **marcado interno del grounding** del
        contenido (medido con uso real: `<fuente_externa>…</fuente_externa>` se
        filtraba a la respuesta visible porque el modelo copia las etiquetas).
        """
        # ✅ 2026-09-29: nunca exponer el marcado interno del prompt al usuario.
        self.content = limpiar_marcado_interno(self.content)

        uso = getattr(_USO_LLM, "tokens", None)
        if not uso:
            return
        _USO_LLM.tokens = None
        self.metadata = {**(self.metadata or {}), **uso}

    def to_response(self, session_id: Optional[str] = None) -> Response:
        # ✅ P1.1 (plan_fiabilidad, 2026-09-12): `metadata["sources"]` (lo que
        # llenan researcher.py/searcher.py) nunca se copiaba al campo dedicado
        # `Response.sources` — quedaba siempre en [] pese a haber fuentes
        # reales en metadata. Response.sources es lo que lee la telemetría
        # por turno (P1.1) y cualquier UI que muestre respaldo de una
        # respuesta de dato.
        # ⚠️ Normalización defensiva: `metadata` es libre y un valor raro podía
        # romper o corromper el Response (medido):
        #   · int (p. ej. `len(sources)`) → TypeError: not iterable
        #   · lista con no-str            → ValidationError (pydantic: List[str])
        #   · dict                        → copiaba las CLAVES como fuentes
        #   · string suelto               → lo partía carácter por carácter (12 "fuentes")
        # Solo se aceptan strings no vacíos; los iterables se filtran.
        _raw_sources = self.metadata.get("sources") if self.metadata else None
        if isinstance(_raw_sources, str):
            sources = [_raw_sources] if _raw_sources.strip() else []
        elif isinstance(_raw_sources, (list, tuple, set)):
            sources = [x for x in _raw_sources
                       if isinstance(x, str) and x.strip()]
        else:
            sources = []
        return Response(
            content=self.content,
            session_id=session_id,
            model_used=self.model_used,
            task_type=self.task_type,
            latency_ms=self.latency_ms,
            confidence=self.confidence,
            sources=sources,
            validation_passed=self.validation_passed,
            error=self.error,
            metadata=self.metadata,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "content": self.content,
            "agent_type": self.agent_type,
            "model_used": self.model_used,
            "task_type": self.task_type,
            "latency_ms": round(self.latency_ms, 2),
            "confidence": round(self.confidence, 4),
            "validation_passed": self.validation_passed,
            "error": self.error,
            "metadata": self.metadata,
        }


# ── BaseAgent ────────────────────────────────────────────────────────────────

# ✅ plan_memory (B, 2026-08-29): tope de gateways de memoria por sesión que
# un agente cachea (las sesiones nicegui_* son efímeras — sin tope, un daemon
# de larga vida acumularía gateways indefinidamente).
MEMORY_SESSION_CACHE_MAX = 64


class BaseAgent(ABC, StatsProvider, ExecutableAgent):
    """
    Clase base abstracta para todos los agentes de AXIOMA.

    Proporciona la infraestructura común:
        - Cliente LLM con generación síncrona y streaming.
        - Memoria persistente (MemoryGateway) con contexto y almacenamiento.
        - Ejecución de herramientas (ToolExecutor) con permisos y sandbox.
        - Reintentos con backoff exponencial (sync y async).
        - Métricas de ejecución (estado, latencia, tasa de éxito).
        - Inyección de contexto desde Knowledge Graph (KG) en system prompt.
        - Logging estructurado.

    Los agentes concretos deben implementar el método abstracto `execute()`.
    """
    AGENT_TYPE = "base"
    DEFAULT_TOOL_PERMISSION = None
    # ✅ FIX: scenario LLM por tipo de agente, declarado acá — no adivinado
    # por nombre de modelo. Agentes concretos lo sobreescriben (ver coder.py).
    LLM_SCENARIO: ScenarioType = ScenarioType.STANDARD

    DEFAULT_SYSTEM_PROMPT = """Eres un asistente de IA útil y preciso.
REGLAS DE FORMATO OBLIGATORIAS:
❌ NUNCA uses preámbulos como "Basado en...", "Puedo...", "Según..."
❌ NUNCA muestres procesos internos o "📘 Pensando..." al usuario final
✅ Respuesta directa PRIMERO (datos, valores, fechas, unidades)
✅ Máximo 3 párrafos concisos
✅ Ofrece ayuda adicional al final
Responde de forma clara y concisa."""

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        enable_memory: bool = True,
        enable_validation: bool = True,
        max_retries: int = 3,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ):
        start_time = time.time()
        self.config = AgentConfig(
            agent_type=self.AGENT_TYPE,
            model=model or settings.llm_default_model,
            temperature=temperature or 0.7,
            max_tokens=max_tokens or 512,
            # ✅ FIX: sin fallback a settings.ollama_timeout acá — None deja que
            # OllamaClientBase resuelva el timeout por scenario (LLM_SCENARIO).
            # Solo un timeout explícito del caller debe pisar ese default.
            timeout=timeout,
            scenario=self.LLM_SCENARIO,
            enable_memory=enable_memory,
            enable_validation=enable_validation,
            max_retries=max_retries,
            system_prompt=system_prompt or self.DEFAULT_SYSTEM_PROMPT,
        )
        self._state = AgentState.IDLE
        self._current_execution: Optional[AgentExecution] = None
        # ✅ FIX MEMORIA: deque con maxlen previene fuga de memoria en ejecuciones de larga duración
        self._execution_history: deque = deque(maxlen=50)
        self._llm_client: Optional[OllamaClient] = None
        self._memory: Optional[MemoryGateway] = None
        # ✅ plan_memory (B, 2026-08-29): gateways de memoria POR SESIÓN (el
        # agente es singleton y sirve muchas sesiones). Cache acotado para no
        # acumular sesiones efímeras (nicegui_* se regeneran en cada recarga).
        self._memory_sessions: Dict[str, MemoryGateway] = {}
        self._memory_sessions_lock = threading.Lock()
        self._stats = {
            "total_executions": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "total_latency_ms": 0.0,
            "last_execution": None,
        }

        # ✅ FIX FASE 1: Flag de control para evitar Cache Poisoning en reintentos.
        self._skip_cache_next = False

        # ✅ Fase 2 (H6): _generate() descartaba el OllamaResponse completo,
        # devolvía solo .content — CoderAgent no podía leer done_reason
        # (truncamiento, Fase 6) porque se perdía acá, un nivel antes de
        # llegar a coder.py. _generate() sigue devolviendo str (no rompe
        # a SearcherAgent/ResearcherAgent/AnalystAgent/ValidatorAgent/
        # SystemAgent, que solo usan el string) pero ahora también deja
        # el OllamaResponse crudo en este atributo para quien lo necesite.
        self._last_llm_response: Optional[Any] = None

        self._prompt_manager: Optional[Any] = None
        if PROMPT_MANAGER_AVAILABLE:
            try:
                self._prompt_manager = get_prompt_manager(self.AGENT_TYPE)
                logger.debug(f"{self.AGENT_TYPE}: PromptManager inicializado")
            except Exception as e:
                logger.warning(f"{self.AGENT_TYPE}: PromptManager no disponible: {e}")

        self._tool_executor: Optional[Any] = None
        if TOOLS_AVAILABLE:
            try:
                self._tool_executor = get_executor()
                logger.debug(f"{self.AGENT_TYPE}: ToolExecutor inicializado")
            except Exception as e:
                logger.warning(f"{self.AGENT_TYPE}: ToolExecutor no disponible: {e}")

        duration = time.time() - start_time
        logger.info(f"{self.AGENT_TYPE} initialized: model={self.config.model}")
        _log_agent(self.AGENT_TYPE, "initialized", task_type="INIT",
                   duration=duration, success=True)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def is_available(self) -> bool:
        return self._state in [AgentState.IDLE, AgentState.COMPLETED]

    @property
    def llm_client(self) -> OllamaClient:
        if self._llm_client is None:
            # ✅ FIX: antes solo se pasaba `model` — scenario/timeout de
            # AgentConfig se calculaban y nunca llegaban al cliente real.
            self._llm_client = OllamaClient(
                model=self.config.model,
                scenario=self.config.scenario,
                timeout=self.config.timeout,
            )
        return self._llm_client

    @property
    def memory(self) -> Optional[MemoryGateway]:
        if self._memory is None and self.config.enable_memory:
            session_id = f"{self.AGENT_TYPE}_{uuid.uuid4().hex[:8]}"
            self._memory = MemoryGateway(session_id=session_id, agent_id=self.AGENT_TYPE)
        return self._memory

    # ── KG Context ───────────────────────────────────────────────────────────

    def _inject_kg_context(self, system_prompt: str, kg_context: List[Dict]) -> str:
        if not kg_context:
            return system_prompt
        try:
            kg_lines = []
            for item in kg_context[:3]:
                node = item.get("node", {})
                label = node.get("label", "")
                category = node.get("category", "")
                if label:
                    kg_lines.append(f"- {label} ({category})")
            if not kg_lines:
                return system_prompt
            kg_summary = "\n".join(kg_lines)
            return system_prompt + f"\n\nContexto conocido:\n{kg_summary}"
        except Exception as e:
            log_degraded(logger, e, "src/agents/base.py:_inject_kg_context")
            return system_prompt

    # ── Tool Execution ───────────────────────────────────────────────────────

    def _resolve_runtime_mode(self) -> Optional[RuntimeMode]:
        """
        ✅ gap.txt (2026-08-26): resuelve el RuntimeMode activo del sistema.

        Fuente: `settings.runtime_mode`, persistido por el CLI (`/mode ...`).
        None → el executor no filtra por modo (comportamiento STANDARD).

        Degradación silenciosa: si settings no tiene el campo o el valor es
        inválido, devuelve None (nada se rompe, nada se filtra de más).
        """
        try:
            raw = getattr(settings, "runtime_mode", None)
            if not raw:
                return None
            return RuntimeMode(raw)
        except (ValueError, AttributeError, TypeError):
            logger.debug(f"[{self.AGENT_TYPE}] runtime_mode inválido o ausente → None")
            return None

    async def execute_tool(
        self,
        tool_name: str,
        permission_override: Optional[Any] = None,
        timeout: float = 30.0,
        resource_path: str = "",
        risk_score: float = 0.2,
        **kwargs: Any,
    ) -> Any:
        if not TOOLS_AVAILABLE or self._tool_executor is None:
            logger.warning(
                f"{self.AGENT_TYPE}: execute_tool('{tool_name}') — tools_mcp no disponible"
            )
            return {"success": False, "error": "tools_mcp no disponible", "output": ""}

        effective_permission = permission_override
        if effective_permission is None:
            effective_permission = (
                self.DEFAULT_TOOL_PERMISSION
                if self.DEFAULT_TOOL_PERMISSION is not None
                else ToolPermission.EXECUTE
            )

        # ✅ v1.3.0: resource_path explícito, o inferido de kwargs (path/src)
        # si el llamador no lo pasó. Sin esto, PermissionGateway.evaluate()
        # nunca corre (tool_executor.py solo evalúa si resource_path != "").
        effective_resource_path = resource_path or str(
            kwargs.get("path") or kwargs.get("src") or ""
        )

        # ✅ gap.txt: propagar el RuntimeMode del sistema al ExecutionContext
        # para que el paso 2.5 del ToolExecutor filtre tools por modo.
        ctx = ExecutionContext(
            session_permission=effective_permission,
            timeout_seconds=timeout,
            caller=self.AGENT_TYPE,
            resource_path=effective_resource_path,
            risk_score=risk_score,
            runtime_mode=self._resolve_runtime_mode(),
        )
        try:
            result = await self._tool_executor.run(tool_name, context=ctx, **kwargs)
            _log_agent(self.AGENT_TYPE, f"tool:{tool_name}", task_type="TOOL",
                       success=result.success, error=result.error)
            return result
        except Exception as e:
            _log_error(e, f"execute_tool:{tool_name}", self.AGENT_TYPE)
            logger.error(f"{self.AGENT_TYPE}: Error ejecutando tool '{tool_name}': {e}")
            return {"success": False, "error": str(e), "output": ""}

    def execute_tool_sync(
        self,
        tool_name: str,
        permission_override: Optional[Any] = None,
        timeout: float = 30.0,
        resource_path: str = "",
        risk_score: float = 0.2,
        **kwargs: Any,
    ) -> Any:
        if not TOOLS_AVAILABLE or self._tool_executor is None:
            return {"success": False, "error": "tools_mcp no disponible", "output": ""}

        effective_permission = permission_override
        if effective_permission is None:
            effective_permission = (
                self.DEFAULT_TOOL_PERMISSION
                if self.DEFAULT_TOOL_PERMISSION is not None
                else ToolPermission.EXECUTE
            )

        # ✅ v1.3.0: ver nota en execute_tool() — mismo fix de propagación.
        effective_resource_path = resource_path or str(
            kwargs.get("path") or kwargs.get("src") or ""
        )

        # ✅ gap.txt: propagar el RuntimeMode del sistema al ExecutionContext
        # (mismo fix que execute_tool()).
        ctx = ExecutionContext(
            session_permission=effective_permission,
            timeout_seconds=timeout,
            caller=self.AGENT_TYPE,
            resource_path=effective_resource_path,
            risk_score=risk_score,
            runtime_mode=self._resolve_runtime_mode(),
        )
        # ✅ FIX ROBUSTEZ: Añadido manejo de excepciones para consistencia con execute_tool
        try:
            result = self._tool_executor.run_sync(tool_name, context=ctx, **kwargs)
            _log_agent(self.AGENT_TYPE, f"tool_sync:{tool_name}", task_type="TOOL",
                       success=result.success, error=result.error)
            return result
        except Exception as e:
            _log_error(e, f"execute_tool_sync:{tool_name}", self.AGENT_TYPE)
            logger.error(f"{self.AGENT_TYPE}: Error ejecutando tool sync '{tool_name}': {e}")
            return {"success": False, "error": str(e), "output": ""}

    def get_available_tools(self) -> List[str]:
        if not TOOLS_AVAILABLE or self._tool_executor is None:
            return []
        effective_permission = (
            self.DEFAULT_TOOL_PERMISSION
            if self.DEFAULT_TOOL_PERMISSION is not None
            else ToolPermission.EXECUTE
        )
        try:
            return self._tool_executor.available_for(effective_permission)
        except Exception as e:
            log_degraded(logger, e, "src/agents/base.py:get_available_tools")
            return []

    def can_use_tool(self, tool_name: str) -> bool:
        if not TOOLS_AVAILABLE or self._tool_executor is None:
            return False
        effective_permission = (
            self.DEFAULT_TOOL_PERMISSION
            if self.DEFAULT_TOOL_PERMISSION is not None
            else ToolPermission.EXECUTE
        )
        try:
            return self._tool_executor.can_execute(tool_name, effective_permission)
        except Exception as e:
            log_degraded(logger, e, "src/agents/base.py:can_use_tool")
            return False

    # ── Estado y ejecución ───────────────────────────────────────────────────

    def _set_state(self, state: AgentState) -> None:
        old_state = self._state
        self._state = state
        logger.debug(f"{self.AGENT_TYPE} state: {old_state.value} → {state.value}")

    def _start_execution(self, task_type: str, input_summary: str) -> AgentExecution:
        start_time = time.time()
        execution = AgentExecution(
            agent_name=self.AGENT_TYPE,
            task_type=task_type,
            state=AgentState.PROCESSING,
            input_summary=input_summary[:500],
        )
        self._current_execution = execution
        self._set_state(AgentState.PROCESSING)
        _log_agent(self.AGENT_TYPE, "started", task_type=task_type,
                   duration=time.time() - start_time, success=True)
        return execution

    def _end_execution(self, success: bool, output_summary: str = None,
                       error: str = None, latency_ms: Optional[float] = None) -> AgentExecution:
        """
        ✅ FIX MÉTRICAS: Añadido parámetro latency_ms opcional.
        Permite que las llamadas directas a execute() también contribuyan al promedio de latencia.
        """
        start_time = time.time()
        if self._current_execution:
            self._current_execution.complete(output_summary=output_summary, error=error)
            self._execution_history.append(self._current_execution)

        new_state = AgentState.COMPLETED if success else AgentState.FAILED
        self._set_state(new_state)

        self._stats["total_executions"] += 1
        if success:
            self._stats["successful_executions"] += 1
        else:
            self._stats["failed_executions"] += 1

        # ✅ FIX MÉTRICAS: Acumular latencia si se proporciona
        if latency_ms is not None:
            self._stats["total_latency_ms"] += latency_ms

        self._stats["last_execution"] = datetime.now(timezone.utc).isoformat()

        _log_agent(self.AGENT_TYPE, "completed", task_type="EXEC",
                   duration=time.time() - start_time, success=success, error=error)
        return self._current_execution

    # ── Generación LLM (Refactorizado DRY) ───────────────────────────────────

    def _log_generation(self, action: str, task_type: str, start_time: float,
                        success: bool, input_size: int, output_size: int = 0,
                        error: Exception = None):
        """Helper centralizado para logging de llamadas a modelo."""
        duration = time.time() - start_time
        _log_model(self.AGENT_TYPE, self.config.model, action, task_type,
                   duration=duration, success=success,
                   input_size=input_size, output_size=output_size)
        if error:
            extra_info = {
                "prompt_length": input_size,
                "task_type": task_type,
                "error_type": type(error).__name__
            }
            _log_error(error, f"{action} failed", self.AGENT_TYPE, extra_info=extra_info)

    def _handle_generation_error(self, error: Exception, action: str, task_type: str,
                                 start_time: float, prompt_length: int) -> AgentError:
        """Helper centralizado para manejo de errores de generación."""
        self._log_generation(action, task_type, start_time, False, prompt_length, error=error)
        if isinstance(error, LLMError):
            return AgentError(f"{action} failed: {error}", agent=self.AGENT_TYPE)
        return AgentError(f"Unexpected {action} error: {error}", agent=self.AGENT_TYPE)

    def _generate(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        start_time = time.time()
        task_type = kwargs.pop("task_type", "UNKNOWN")
        system_prompt = system or self.config.system_prompt
        kwargs.setdefault("temperature", self.config.temperature)
        kwargs.setdefault("max_tokens", self.config.max_tokens)

        # ✅ FIX FASE 1: Anti-Cache en Retry.
        if getattr(self, '_skip_cache_next', False):
            kwargs.setdefault("use_cache", False)
            self._skip_cache_next = False

        try:
            result = self.llm_client.generate(prompt=prompt, system=system_prompt, **kwargs)
            content = result.content if hasattr(result, 'content') else str(result)
            # ✅ Fase 2 (H6): antes se tiraba `result` acá y solo se devolvía
            # `content` — ver nota en __init__ sobre _last_llm_response.
            self._last_llm_response = result
            # ✅ ISSUE-082: el uso real de tokens viaja al AgentResult (thread-local).
            _USO_LLM.tokens = tokens_de_respuesta(result)
            # ✅ v0.6.8kk: el evento MODEL canónico (con tokens REALES de Ollama)
            # lo emite OllamaClientBase.generate() al completar. Este log AGENT
            # duplicaba el mismo generate con len(prompt)/len(content) — es decir,
            # CARACTERES etiquetados como tokens (1816 en vez de 637 reales).
            # Los errores sí se loguean (agregan contexto AGENT del fallo).
            return content
        except Exception as e:
            raise self._handle_generation_error(e, "LLM generation", task_type, start_time, len(prompt))

    def _generate_stream(self, prompt: str, system: Optional[str] = None,
                         **kwargs) -> Generator[str, None, None]:
        start_time = time.time()
        task_type = kwargs.pop("task_type", "UNKNOWN")
        system_prompt = system or self.config.system_prompt
        kwargs.setdefault("temperature", self.config.temperature)
        kwargs.setdefault("max_tokens", self.config.max_tokens)
        try:
            for chunk in self.llm_client.generate_stream(
                prompt=prompt, system=system_prompt, **kwargs,
            ):
                yield chunk
            # ✅ v0.6.8kk: el éxito del stream lo reporta el cliente (MODEL
            # canónico); aquí ya no se duplica con caracteres como "tokens".
        except Exception as e:
            raise self._handle_generation_error(e, "Streaming", task_type, start_time, len(prompt))

    # ── Memoria ──────────────────────────────────────────────────────────────

    def _memory_for(
        self, session_id: Optional[str], user_id: Optional[str] = None,
    ) -> Optional[MemoryGateway]:
        """
        Gateway de memoria para la sesión indicada (thread-safe).

        - session_id dado → gateway POR SESIÓN (comparte long_term/Palace con
          el write-path del dispatcher, plan_memory B).
        - user_id dado → identidad del usuario de la sesión (UI de usuarios,
          Capa 2); fallback al setting (axioma_user_id).
        - session_id None → gateway privado del agente (comportamiento previo,
          session_id aleatorio — solo uso standalone/tests).
        Cache acotado a MEMORY_SESSION_CACHE_MAX para no filtrar sesiones.
        """
        if not session_id:
            return self.memory
        with self._memory_sessions_lock:
            gw = self._memory_sessions.get(session_id)
            if gw is None:
                if len(self._memory_sessions) >= MEMORY_SESSION_CACHE_MAX:
                    # Evictar la más vieja (dict mantiene orden de inserción).
                    self._memory_sessions.pop(next(iter(self._memory_sessions)))
                # ✅ Capa 2 (2026-08-31): identidad persistente del usuario →
                # retrieval cross-sesión (mismo user_id que el write-path).
                gw = MemoryGateway(
                    session_id=session_id, agent_id=self.AGENT_TYPE,
                    user_id=user_id or getattr(settings, "axioma_user_id", ""),
                )
                self._memory_sessions[session_id] = gw
            return gw

    def _add_to_memory(self, role: str, content: str,
                       metadata: Dict[str, Any] = None,
                       session_id: Optional[str] = None,
                       user_id: Optional[str] = None) -> bool:
        gw = self._memory_for(session_id, user_id)
        if gw:
            return gw.add_message(role, content, metadata)
        return False

    def _get_context(
        self,
        limit: int = 10,
        task_type: Optional[str] = None,
        query: Optional[str] = None,
        max_tokens: int = 2048,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        gw = self._memory_for(session_id, user_id)
        if gw:
            return gw.get_context(
                limit=limit,
                task_type=task_type,
                query=query,
                max_tokens=max_tokens,
            )
        return []

    # ── Interfaz abstracta ───────────────────────────────────────────────────

    @abstractmethod
    def execute(self, input_msg: Message, **kwargs: Any) -> AgentResult:
        pass

    def validate(self, result: AgentResult) -> bool:
        if not self.config.enable_validation:
            result.validation_passed = True
            return True

        if not result.content or not result.content.strip():
            logger.warning(
                f"{self.AGENT_TYPE}: Validation failed - empty or whitespace-only content. "
                f"Task: {result.task_type}"
            )
            result.validation_passed = False
            return False

        result.validation_passed = True
        return True

    # ✅ AUD-DC-002 ELIMINADO (2026-08-26): execute_with_retry() síncrono
    # con time.sleep() real — dead code sin callers en el proyecto y
    # peligroso (bloqueaba el event loop si se invocaba desde una
    # corrutina). Reemplazo canónico: execute_with_retry_async().

    async def execute_with_retry_async(self, input_msg: Message, **kwargs: Any) -> AgentResult:
        """
        ✅ FIX AUDITORÍA: variante async-safe de execute_with_retry() — misma
        lógica de reintentos y backoff exponencial, pero self.execute() corre
        vía asyncio.to_thread() (mismo patrón que dispatcher.py/interpreter_loop.py
        usan para agentes síncronos) y el backoff usa asyncio.sleep() en vez de
        time.sleep(). Segura para llamar desde una corrutina — no bloquea el
        event loop. Usar esta desde código async; execute_with_retry() (sync)
        queda para callers puramente síncronos.
        """
        start_time = time.time()
        task_type = kwargs.get("task_type", "unknown")
        last_error = None

        for attempt in range(self.config.max_retries):
            try:
                result = await asyncio.to_thread(self.execute, input_msg, **kwargs)

                if result.success and self.validate(result):
                    duration = time.time() - start_time

                    _log_agent(self.AGENT_TYPE, "execute_with_retry_async", task_type,
                               duration=duration, success=True,
                               extra={"attempt": attempt + 1})
                    return result

                elif result.success and not result.validation_passed:
                    last_error = result.error or "Validation failed (empty/invalid content)"
                    logger.warning(
                        f"{self.AGENT_TYPE} attempt {attempt + 1} validation failed: "
                        f"content_length={len(result.content)}, error={last_error}"
                    )
                    _log_error(
                        AgentError(last_error),
                        f"execute_with_retry_async validation attempt {attempt + 1}",
                        self.AGENT_TYPE,
                        extra_info={"attempt": attempt + 1, "task_type": task_type, "content_length": len(result.content)}
                    )
                    self._skip_cache_next = True

                else:
                    last_error = result.error or "Execution failed (success=False)"
                    logger.warning(f"{self.AGENT_TYPE} attempt {attempt + 1} failed: {last_error}")
                    _log_error(
                        AgentError(last_error),
                        f"execute_with_retry_async execution attempt {attempt + 1}",
                        self.AGENT_TYPE,
                        extra_info={"attempt": attempt + 1, "task_type": task_type}
                    )

            except Exception as e:
                last_error = str(e)
                logger.warning(f"{self.AGENT_TYPE} attempt {attempt + 1} exception: {e}")
                _log_error(e, f"execute_with_retry_async attempt {attempt + 1}", self.AGENT_TYPE,
                           extra_info={"attempt": attempt + 1, "task_type": task_type})

            if attempt < self.config.max_retries - 1:
                delay = 1.0 * (2 ** attempt)
                logger.info(f"Retrying in {delay}s...")
                await asyncio.sleep(delay)

        duration = time.time() - start_time
        latency_ms = round(duration * 1000, 2)

        _log_agent(self.AGENT_TYPE, "execute_with_retry_async", task_type,
                   duration=duration, success=False, error=last_error,
                   extra={"attempts": self.config.max_retries})
        return AgentResult(
            success=False, content="", agent_type=self.AGENT_TYPE,
            model_used=self.config.model, task_type=task_type,
            latency_ms=latency_ms,
            error=f"Failed after {self.config.max_retries} attempts: {last_error}",
        )

    # ── Stats y ciclo de vida ────────────────────────────────────────────────

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "agent_type": self.AGENT_TYPE,
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "supports_streaming": True,
            "supports_memory": self.config.enable_memory,
            "supports_validation": self.config.enable_validation,
            "max_retries": self.config.max_retries,
            "priority": self.config.priority.value,
            "prompt_manager_active": self._prompt_manager is not None,
            "tools_active": self._tool_executor is not None,
            "available_tools": self.get_available_tools(),
            "tool_permission": (
                self.DEFAULT_TOOL_PERMISSION.name
                if TOOLS_AVAILABLE and self.DEFAULT_TOOL_PERMISSION is not None
                else "EXECUTE"
            ),
        }

    def get_stats(self) -> Dict[str, Any]:
        avg_latency = self._stats["total_latency_ms"] / max(self._stats["total_executions"], 1)
        success_rate = self._stats["successful_executions"] / max(self._stats["total_executions"], 1)
        return {
            "agent_type": self.AGENT_TYPE,
            "state": self._state.value,
            "is_available": self.is_available,
            "total_executions": self._stats["total_executions"],
            "successful_executions": self._stats["successful_executions"],
            "failed_executions": self._stats["failed_executions"],
            "success_rate": round(success_rate, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "last_execution": self._stats["last_execution"],
        }

    def reset(self) -> None:
        self._state = AgentState.IDLE
        self._current_execution = None
        logger.info(f"{self.AGENT_TYPE} reset")
        _log_agent(self.AGENT_TYPE, "reset", task_type="SYSTEM", success=True)

    def close(self) -> None:
        start_time = time.time()
        if self._llm_client:
            self._llm_client.close()
            self._llm_client = None
        if self._memory:
            self._memory.close()
            self._memory = None
        logger.info(f"{self.AGENT_TYPE} closed")
        _log_agent(self.AGENT_TYPE, "closed", task_type="SYSTEM",
                   duration=time.time() - start_time, success=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
