#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Tipos, Enums y Protocols del Dominio
# Ruta: src/domain/types.py
# Autor: SaintWick
# Versión: 1.8.0 (CAMERA_DESCRIBE support — Rafael Fase 2)
# Cambios v1.8.0:
#   + CAMERA_DESCRIBE TaskType — descripción verbal de captura de cámara (grupo JARVIS)
#   + is_jarvis_task() actualizado para incluir camera_describe
# Cambios v1.7.0:
#   + LOCATION_QUERY TaskType — consulta de ubicación geográfica
#   + is_search_related() actualizado para incluir location_query
#   + AgentRole.get_agent_for_task() actualizado para location_query
#   + AgentRole.RESEARCHER capabilities: ipapi añadido (Geolocator)
# Cambios v1.6.0:
#   + JARVIS_QUERY TaskType — consulta al asistente en modo Jarvis
#   + SCREEN_DESCRIBE TaskType — descripción verbal de pantalla
#   + AUTONOMOUS_TASK TaskType — ejecución autónoma de tarea encadenada
#   + TaskType.is_jarvis_task() helper
#   + AgentRole.get_agent_for_task() actualizado para nuevos tipos
#   + AgentRole.SYSTEM capabilities: jarvis entries
# ═══════════════════════════════════════════════════════════════

from enum import Enum
from typing import Dict, Any, List, Protocol, runtime_checkable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.tools_mcp.tool_base import ToolPermission  # noqa: F401
    from src.domain.entities import Message, AgentResult

# ═══════════════════════════════════════════════════════════════
# PROTOCOLOS FORMALES (Contratos del Dominio)
# ═══════════════════════════════════════════════════════════════

@runtime_checkable
class StatsProvider(Protocol):
    """
    Contrato formal para cualquier componente que provea métricas de estado.
    Elimina la dependencia de Duck Typing para el método get_stats().
    """
    def get_stats(self) -> Dict[str, Any]: ...

@runtime_checkable
class ExecutableAgent(Protocol):
    """
    Contrato formal para agentes capaces de procesar un Message y retornar un AgentResult.
    Unifica la firma de ejecución a través de todos los agentes (Coder, Searcher, Analyst).
    """
    def execute(self, input_msg: "Message", **kwargs: Any) -> "AgentResult": ...

@runtime_checkable
class Searchable(Protocol):
    """
    Contrato formal para módulos con capacidad de búsqueda (ej. MemoryGateway, ExternalAPIs).
    Estandariza la interfaz de búsqueda semántica y de datos.
    """
    def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]: ...


# ═══════════════════════════════════════════════════════════════
# ENUMS DEL DOMINIO
# ═══════════════════════════════════════════════════════════════

class TaskType(Enum):
    """
    Catálogo de 48 tipos de tarea soportados por AXIOMA.
    Categorías:
    - CÓDIGO (11):          Generación, debugging, review, etc.
    - ANÁLISIS (5):         Datos, documentos, sentimiento, etc.
    - BÚSQUEDA (5):         Web, noticias, académico, código, archivos
    - CONVERSACIÓN (5):     Chat, soporte, tutoría, etc.
    - CONSULTAS (7):        Clima, hora, finanzas, ubicación, etc.
    - VALIDACIÓN (3):       Fact-checking, calidad, seguridad
    - CREATIVIDAD (3):      Contenido, guiones, poesía
    - SISTEMA OS (5):       Control de OS, apps, volumen, captura
    - JARVIS (4):           Consultas Jarvis, descripción de pantalla/cámara, tareas autónomas
    """

    # === CÓDIGO (11 tipos) ===
    CODE_GENERATION    = "code_generation"
    CODE_CORRECTION    = "code_correction"
    CODE_EXPLANATION   = "code_explanation"
    CODE_REVIEW        = "code_review"
    CODE_OPTIMIZATION  = "code_optimization"
    CODE_TESTING       = "code_testing"
    CODE_DOCUMENTATION = "code_documentation"
    CODE_MIGRATION     = "code_migration"
    CODE_DEPLOYMENT    = "code_deployment"
    CODE_DEBUGGING     = "code_debugging"
    CODE_AUTONOMOUS    = "code_autonomous"

    # === ANÁLISIS (5 tipos) ===
    DATA_ANALYSIS      = "data_analysis"
    DOCUMENT_ANALYSIS  = "document_analysis"
    SENTIMENT_ANALYSIS = "sentiment_analysis"
    TREND_ANALYSIS     = "trend_analysis"
    RISK_ANALYSIS      = "risk_analysis"

    # === BÚSQUEDA (5 tipos) ===
    WEB_SEARCH      = "web_search"
    NEWS_SEARCH     = "news_search"
    ACADEMIC_SEARCH = "academic_search"
    CODE_SEARCH     = "code_search"
    FILE_SEARCH     = "file_search"   # ✅ plan_rafael (2026-08-31): buscar archivos/carpetas en disco

    # === CONVERSACIÓN (5 tipos) ===
    GENERAL_CHAT      = "general_chat"
    TECHNICAL_SUPPORT = "technical_support"
    TUTORING          = "tutoring"
    BRAINSTORMING     = "brainstorming"
    ROLEPLAY          = "roleplay"

    # === CONSULTAS ESPECÍFICAS (7 tipos) ===
    WEATHER_QUERY  = "weather_query"
    TIME_QUERY     = "time_query"
    FINANCE_QUERY  = "finance_query"
    NEWS_QUERY     = "news_query"
    LOCATION_QUERY = "location_query"
    TRANSLATION    = "translation"
    SUMMARIZATION  = "summarization"

    # === VALIDACIÓN (3 tipos) ===
    FACT_CHECKING      = "fact_checking"
    QUALITY_VALIDATION = "quality_validation"
    SECURITY_AUDIT     = "security_audit"

    # === CREATIVIDAD (3 tipos) ===
    CONTENT_CREATION  = "content_creation"
    SCRIPT_WRITING    = "script_writing"
    POETRY_GENERATION = "poetry_generation"

    # === SISTEMA OS (5 tipos) ===
    SYSTEM_CONTROL      = "system_control"
    APP_LAUNCH          = "app_launch"
    VOLUME_CONTROL      = "volume_control"
    SCREENSHOT          = "screenshot"
    SCREENSHOT_ANALYSIS = "screenshot_analysis"

    # === JARVIS (4 tipos) ===
    JARVIS_QUERY    = "jarvis_query"     # Consulta al asistente en modo Jarvis (voz → respuesta corta)
    SCREEN_DESCRIBE = "screen_describe"  # Descripción verbal de la pantalla activa
    AUTONOMOUS_TASK = "autonomous_task"  # Tarea encadenada autónoma (planificar → ejecutar → reportar)
    CAMERA_DESCRIBE = "camera_describe"  # Descripción verbal de lo capturado por cámara

    # ── Helpers ──────────────────────────────────────────────────────────────

    @classmethod
    def is_code_related(cls, task_type: str) -> bool:
        return task_type.startswith("code_")

    @classmethod
    def is_search_related(cls, task_type: str) -> bool:
        return task_type.endswith("_search") or task_type in [
            cls.WEATHER_QUERY.value,
            cls.NEWS_QUERY.value,
            cls.FINANCE_QUERY.value,
            cls.LOCATION_QUERY.value,
        ]

    @classmethod
    def is_conversation(cls, task_type: str) -> bool:
        return task_type in [
            cls.GENERAL_CHAT.value,
            cls.TECHNICAL_SUPPORT.value,
            cls.TUTORING.value,
            cls.BRAINSTORMING.value,
            cls.ROLEPLAY.value,
        ]

    @classmethod
    def requires_validation(cls, task_type: str) -> bool:
        return task_type in [
            cls.CODE_GENERATION.value,
            cls.CODE_CORRECTION.value,
            cls.FACT_CHECKING.value,
            cls.QUALITY_VALIDATION.value,
            cls.SECURITY_AUDIT.value,
        ]

    @classmethod
    def supports_streaming(cls, task_type: str) -> bool:
        return task_type not in [
            cls.QUALITY_VALIDATION.value,
            cls.SECURITY_AUDIT.value,
        ]

    @classmethod
    def is_system_task(cls, task_type: str) -> bool:
        """True si es tarea de control del OS."""
        return task_type in {
            cls.SYSTEM_CONTROL.value,
            cls.APP_LAUNCH.value,
            cls.VOLUME_CONTROL.value,
            cls.SCREENSHOT.value,
            cls.SCREENSHOT_ANALYSIS.value,
        }

    @classmethod
    def is_jarvis_task(cls, task_type: str) -> bool:
        """True si es tarea exclusiva del Modo Jarvis."""
        return task_type in {
            cls.JARVIS_QUERY.value,
            cls.SCREEN_DESCRIBE.value,
            cls.AUTONOMOUS_TASK.value,
            cls.CAMERA_DESCRIBE.value,
        }


class AgentRole(Enum):
    """
    Roles de agentes especializados en AXIOMA.
    """
    ORCHESTRATOR = "orchestrator"
    CODER        = "coder"
    RESEARCHER   = "researcher"
    ANALYST      = "analyst"
    VALIDATOR    = "validator"
    SYSTEM       = "system"

    @classmethod
    def get_agent_for_task(cls, task_type: str) -> "AgentRole":
        if task_type.startswith("code_"):
            return cls.CODER
        if task_type in {
            "web_search", "news_search", "academic_search", "code_search",
            "weather_query", "news_query", "finance_query", "location_query",
        }:
            return cls.RESEARCHER
        if task_type in {
            "data_analysis", "document_analysis", "sentiment_analysis",
            "trend_analysis", "risk_analysis",
        }:
            return cls.ANALYST
        if task_type in {
            "fact_checking", "quality_validation", "security_audit",
        }:
            return cls.VALIDATOR
        if TaskType.is_system_task(task_type) or TaskType.is_jarvis_task(task_type):
            return cls.SYSTEM
        return cls.RESEARCHER

    @classmethod
    def get_capabilities(cls, role: "AgentRole") -> dict:
        capabilities = {
            cls.ORCHESTRATOR: {
                "coordinates": True,
                "routes_tasks": True,
                "manages_fallbacks": True,
            },
            cls.CODER: {
                "generates_code": True,
                "debugs_code": True,
                "explains_code": True,
                "autonomous_execution": True,
                "languages": ["python", "javascript", "java", "cpp", "go", "rust"],
            },
            cls.RESEARCHER: {
                "web_search": True,
                "news_search": True,
                "synthesizes_info": True,
                "cites_sources": True,
                "supports_apis": ["tavily", "serper", "newsapi", "ipapi"],
            },
            cls.ANALYST: {
                "analyzes_documents": True,
                "analyzes_data": True,
                "extracts_patterns": True,
                "formats": ["json", "csv", "text", "tables"],
            },
            cls.VALIDATOR: {
                "forced_cot": True,
                "safety_check": True,
                "completeness_check": True,
                "fact_verification": True,
            },
            cls.SYSTEM: {
                "open_app": True,
                "close_app": True,
                "set_volume": True,
                "take_screenshot": True,
                "analyze_screen": True,
                "read_clipboard": True,
                "list_files": True,
                "uses_sandbox": True,
                # Jarvis capabilities
                "jarvis_voice_query": True,
                "screen_describe": True,
                "autonomous_task_chain": True,
            },
        }
        return capabilities.get(role, {})


# ═══════════════════════════════════════════════════════════════
# ✅ v4.2.0: AgentPriority — Movido desde base.py al dominio
# ═══════════════════════════════════════════════════════════════

class AgentPriority(str, Enum):
    """
    Prioridad de ejecución de agentes.
    ✅ v4.2.0: Movido desde src/agents/base.py a este módulo de dominio
    para unificar todos los enums del sistema junto a TaskType y AgentRole.
    """
    CRITICAL = "critical"
    HIGH     = "high"
    NORMAL   = "normal"
    LOW      = "low"


# ═══════════════════════════════════════════════════════════════
# ✅ FASE 1 (plan_harness): RuntimeMode — Modos de ejecución del sistema
# ═══════════════════════════════════════════════════════════════
# Inspirado en el sandbox-policy del harness de referencia
# (deepseek-harness/packages/sandbox/sandbox-policy): un modo por sesión
# que acota qué tools puede invocar el agente y qué efectos secundarios
# se permiten. STANDARD es el default (toolset completo); SAFE_MODE solo
# lectura; MINIMAL un subconjunto mínimo; CODE_MODE permite escritura
# para el flujo de generación de código.

class RuntimeMode(str, Enum):
    """
    Modo de ejecución del sistema — acota el toolset disponible por sesión.

    STANDARD   → Toolset completo (default, retrocompatible).
    MINIMAL    → Solo herramientas mínimas (sandbox_exec, fs_read).
    SAFE_MODE  → Solo lectura: sin escrituras, sin ejecución, sin red.
    CODE_MODE  → El modelo genera código que orquesta tools (permite escritura).

    Referencia: SandboxMode del harness ('read-only' | 'workspace-write' |
    'danger-full-access'), con default fail-safe (SAFE_MODE es el modo
    más restrictivo, pero STANDARD sigue siendo el default para no romper
    comportamiento existente).
    """
    STANDARD  = "standard"    # Todo el toolset actual
    MINIMAL   = "minimal"     # Solo sandbox_exec + fs_read
    SAFE_MODE = "safe_mode"   # Solo lectura, sin writes
    CODE_MODE = "code_mode"   # Modelo genera código que orquesta tools

    def allows_file_write(self) -> bool:
        """True si el modo permite escritura en el filesystem."""
        return self in (RuntimeMode.STANDARD, RuntimeMode.CODE_MODE)

    def allowed_permission_level(self) -> "ToolPermission":
        """
        Nivel ToolPermission máximo permitido en este modo.

        SAFE_MODE → READ_ONLY (solo lectura)
        MINIMAL   → EXECUTE (lectura + ejecución de sandbox)
        Otros     → PRIVILEGED (sin restricción por modo)

        Import diferido de ToolPermission a propósito: src/domain es
        dominio puro y tools_mcp/__init__ importa tool_executor, así que
        un import a nivel de módulo crearía un ciclo
        (types → tools_mcp → tool_executor → types).
        """
        from src.tools_mcp.tool_base import ToolPermission  # import diferido (evita ciclo)
        if self == RuntimeMode.SAFE_MODE:
            return ToolPermission.READ_ONLY
        if self == RuntimeMode.MINIMAL:
            return ToolPermission.EXECUTE
        return ToolPermission.PRIVILEGED
