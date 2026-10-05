#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# Ruta: /ruta/a/axioma/src/core/dispatcher.py
# Autor: SaintWick
# dispatcher.py - Orquestador central de requests multi-agente de AXIOMA.
#
# El Dispatcher es el punto de entrada principal del sistema. Define la clase
# Dispatcher: recursos inyectables (agentes lazy-loaded), configuración de
# caché/métricas y los handlers inline que no ameritan su propio mixin
# (_handle_jarvis, _handle_location). El pipeline de procesamiento en sí
# (process(), _dispatch(), overrides de ruteo, caché L1/L2/L3) vive en
# dispatcher_process.py (DispatcherProcessMixin) — separado en refactor de
# mantenibilidad, el archivo original había crecido a 1401 líneas.
#
# Responsabilidades de ESTE archivo:
#   - Definición de la clase Dispatcher y sus recursos (agentes, caché, semáforos).
#   - Lazy-loading de agentes especializados (CoderAgent, SearcherAgent, etc.).
#   - Handler de Modo Jarvis y de consultas de ubicación.
#   - Mapeo task_type → nombre de handler para métricas.
#
# Ver dispatcher_process.py para: clasificación de intención, overrides de
# ruteo, caché multicapa, circuit breaker, validación post-dispatch,
# aprendizaje continuo (FeedbackLoop) y el router interno (_dispatch).
# ──────────────────────────────────────────────────────────────────────────────
import logging
import asyncio
from typing import Optional, Dict, Any, Callable
import time
import traceback
import threading
from datetime import datetime, timezone
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from src.domain.entities import Message, Response, MessageRole
from src.router.classifier import IntentClassifier
from src.handlers.data_handler import DataHandler
from src.agents.coder import CoderAgent
from src.agents.searcher import SearcherAgent
from src.agents.researcher import ResearcherAgent
from src.agents.validator import ValidatorAgent
from src.llm.client import OllamaClient

# ✅ v0.9.4: vllm_client conectado — import seguro
try:
    from src.llm.vllm_client import get_active_client as _get_active_llm_client
    _VLLM_FACTORY_AVAILABLE = True
except ImportError:
    _VLLM_FACTORY_AVAILABLE = False
    _get_active_llm_client = None  # type: ignore[assignment]

from config.settings import settings
from src.core.dispatcher_cache_metrics import RoutingCache, _DispatcherMetrics
from src.core.dispatcher_handlers import (
    DispatcherHandlersMixin,
    _log_event, _log_error,
)
# ✅ v0.6.8mm: refactor 50/50 — 2ª mitad de handlers (código/análisis/chat/
# investigación/screenshot/autónomo/pipeline) en dispatcher_handlers_extra.py
from src.core.dispatcher_handlers_extra import DispatcherHandlersExtraMixin
from src.core.dispatcher_operations import DispatcherOperationsMixin
# ✅ Refactor de mantenibilidad: pipeline de process()/_dispatch() extraído
from src.core.dispatcher_process import DispatcherProcessMixin
from src.utils.degradation import log_degraded

# ✅ Fase 6 — FeedbackLoop — import seguro (usado en _ensure_feedback_loop)
try:
    from src.core.feedback_loop import get_feedback_loop as _get_feedback_loop
    _FEEDBACK_AVAILABLE = True
except ImportError:
    _FEEDBACK_AVAILABLE = False
    _get_feedback_loop = None  # type: ignore[assignment]

# ✅ Fase 7 — Integración distribuida (import seguro)
try:
    from src.core.dispatcher_distributed import DispatcherDistributedMixin
    _DISTRIBUTED_AVAILABLE = True
except ImportError:
    _DISTRIBUTED_AVAILABLE = False
    class DispatcherDistributedMixin:  # type: ignore[no-redef]
        """Stub vacío si dispatcher_distributed.py no está disponible."""
        def get_distributed_stats(self):
            return {"distributed_queue": None, "load_balancer": None}

logger = logging.getLogger(__name__)

# ── Imports opcionales ───────────────────────────────────────────────────────
try:
    from src.tools_mcp import get_registry
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False
    get_registry = None

try:
    from src.core.planner import MultiStepPlanner
    _PLANNER_AVAILABLE = True
except ImportError:
    MultiStepPlanner = None
    _PLANNER_AVAILABLE = False

# ✅ Semana 2 B: UserProfile
try:
    from src.memory.user_profile import UserProfile
    _PROFILE_AVAILABLE = True
except ImportError:
    _PROFILE_AVAILABLE = False
    UserProfile = None

# ✅ Semana 2 D: ContextSensor
try:
    from src.core.context_sensor import get_sensor as _get_sensor
    _SENSOR_AVAILABLE = True
except ImportError:
    _SENSOR_AVAILABLE = False
    _get_sensor = None

# ✅ v0.4.0: JarvisEngine — import seguro
try:
    from src.core.jarvis_engine import JarvisEngine as _JarvisEngine
    _JARVIS_AVAILABLE = True
except ImportError:
    _JarvisEngine = None  # type: ignore[assignment,misc]
    _JARVIS_AVAILABLE = False


class Dispatcher(DispatcherDistributedMixin, DispatcherHandlersMixin, DispatcherHandlersExtraMixin, DispatcherOperationsMixin, DispatcherProcessMixin):
    """Orquestador central de requests con inyección de dependencias, concurrencia controlada por semáforos segmentados y sistema de caché multicapa (L1/L2/L3). Delegación determinista a handlers sin LLM y agentes con LLM.

    Herencia:
    DispatcherDistributedMixin    — Fase 7: DistributedQueue + LoadBalancer
    DispatcherHandlersMixin       — 1ª mitad: file_search/folder_analysis/system/data/search + retry
    DispatcherHandlersExtraMixin  — 2ª mitad (v0.6.8mm): code/analysis/validation/chat/research/screenshot/autonomous/pipeline
    DispatcherOperationsMixin     — stats, streaming, predictive, warmup, cleanup
    DispatcherProcessMixin       — process(), _dispatch() (dispatcher_process.py)
    """
    # ✅ v1.3.0: location_query agregado a DATA_TASKS
    DATA_TASKS = {"weather_query", "finance_query", "time_query", "news_query", "location_query"}

    CODE_TASKS = {
        "code_generation", "code_correction", "code_explanation",
        "code_review", "code_optimization", "code_testing",
        "code_documentation", "code_migration", "code_deployment",
        "code_debugging", "code_autonomous",
    }
    SEARCH_TASKS = {"web_search", "news_search", "academic_search"}
    # ✅ plan_rafael (2026-08-31): búsqueda de archivos/carpetas en disco.
    FILE_TASKS = {"file_search"}
    ANALYSIS_TASKS = {
        "data_analysis", "document_analysis", "sentiment_analysis",
        "trend_analysis", "risk_analysis",
    }
    VALIDATION_TASKS = {
        "fact_checking",
        "quality_validation",
        "security_audit",
    }
    # ✅ Semana 5 F1
    SYSTEM_TASKS = {
        "system_control",
        "app_launch",
        "volume_control",
        "screenshot",
    }
    # ✅ v0.3.6: Vision tasks — análisis visual de pantalla
    VISION_TASKS = {
        "screenshot_analysis",
    }
    # ✅ v0.4.0: Jarvis tasks — asistente autónomo
    # ✅ Rafael Fase 2: camera_describe agregado (routing + VRAM lock heredado)
    JARVIS_TASKS = {
        "jarvis_query",
        "screen_describe",
        "autonomous_task",
        "camera_describe",
    }

    _routing_cache: Optional[RoutingCache] = None
    _metrics: Optional[_DispatcherMetrics] = None
    _cache_lock = threading.Lock()

    def __init__(
        self,
        enable_cache: bool = True,
        cache_size: int = 500,
        metrics_max_entries: int = 1000,
        agent_factory: Optional[Callable] = None,   # ✅ Semana 0: DI para tests y extensibilidad
        user_id: Optional[str] = None,              # ✅ UI de usuarios (2026-08-31)
    ):
        start_init = time.time()

        # ✅ UI de usuarios (2026-08-31): identidad de la sesión → memoria
        # (Capa 2) aislada por usuario. Respaldo: la identidad configurada.
        self._user_id = user_id or getattr(settings, "axioma_user_id", "")

        # Agentes lazy
        self._classifier: Optional[IntentClassifier] = None
        self._data_handler: Optional[DataHandler] = None
        self._coder: Optional[CoderAgent] = None
        self._searcher: Optional[SearcherAgent] = None
        self._researcher: Optional[ResearcherAgent] = None
        self._validator: Optional[ValidatorAgent] = None
        self._llm_client: Optional[OllamaClient] = None

        # ✅ Semana 0: factory inyectable para override en tests
        self._agent_factory = agent_factory

        # Cache y métricas
        self._enable_cache = enable_cache
        self._routing_cache_size = cache_size
        self._routing_cache: Optional[RoutingCache] = None
        if enable_cache:
            self._routing_cache = self._get_routing_cache(cache_size)
        self._bounded_metrics = self._get_metrics(metrics_max_entries)

        # ✅ FIX AUDITORÍA: antes el write a L3 semántico (más abajo, tras un
        # response exitoso de alta confianza) lanzaba threading.Thread(daemon=True)
        # nuevo en CADA request que calificaba — sin pool, sin límite de threads
        # concurrentes. RouterCache.set() hace el write de L3 de forma síncrona
        # (embedding BGE-M3, ~100ms) — bajo carga sostenida esto acumulaba threads
        # sin límite. Mismo patrón ya usado en trace_store.py (L7).
        self._l3_write_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="dispatcher-l3-write",
        )

        # ✅ plan_memory (B, 2026-08-29): gateways de memoria POR SESIÓN para el
        # write-path centralizado (_record_exchange). Cache acotado — las
        # sesiones nicegui_* son efímeras y no deben acumularse en un daemon de
        # larga vida.
        self._memory_gateways: Dict[str, Any] = {}
        self._memory_gateways_lock = threading.Lock()
        self._memory_gateways_max = 64

        # ToolRegistry
        self._tool_registry = None
        if TOOLS_AVAILABLE:
            try:
                self._tool_registry = get_registry()
                tool_count = len(self._tool_registry.names())
                logger.info(f"ToolRegistry inicializado — {tool_count} tools disponibles")
                _log_event("DISPATCHER", "ToolRegistry inicializado", extra={
                    "tool_count": tool_count,
                    "tools": self._tool_registry.names(),
                })
            except Exception as e:
                logger.warning(f"ToolRegistry no disponible: {e}")

        # ✅ Semana 2 B: UserProfile — sobre KG del gateway de memoria
        self._user_profile: Optional[Any] = None
        if _PROFILE_AVAILABLE:
            try:
                from src.memory.gateway import MemoryGateway
                from src.memory.knowledge_graph import KnowledgeGraph
                from config.paths import Paths
                _kg = KnowledgeGraph(db_path=Paths.DATA / "knowledge_graph.db")
                # ✅ Capa 2 (2026-08-31): identidad persistente del usuario
                # (el usuario del equipo suele ser admin; los nuevos son normales). Antes quedaba
                # siempre "default".
                self._user_profile = UserProfile(
                    kg=_kg, user_id=self._user_id,
                )
                logger.info(f"UserProfile inicializado en Dispatcher (user={self._user_profile._user_id})")
            except Exception as e:
                logger.warning(f"UserProfile no disponible: {e}")

        # ✅ Semana 2 D: ContextSensor — singleton lazy
        self._sensor_enabled: bool = (
            _SENSOR_AVAILABLE and getattr(settings, "enable_context_sensor", True)
        )

        # Planner
        self._planner: Optional[Any] = None
        if _PLANNER_AVAILABLE and MultiStepPlanner:
            try:
                self._planner = MultiStepPlanner()
                logger.info("MultiStepPlanner inicializado en Dispatcher")
            except Exception as e:
                logger.warning(f"MultiStepPlanner no disponible: {e}")

        # ✅ v0.6.9d: SecurityListener — observador PASIVO de eventos de
        # seguridad (tool denies, circuit open, sandbox fail-closed, file ops,
        # mode switch). Los emisores ya existen en runtime (tool_executor,
        # sandbox, promotion_gate, dispatcher_process); esto activa el
        # consumidor en TODOS los entrypoints (web/chat) al construir el
        # Dispatcher. NUNCA bloquea peticiones: solo loguea/audita/cuenta
        # (handlers ligeros, cada uno protegido).
        self._security_listener: Optional[Any] = None
        try:
            from src.core.security_listener import get_security_listener
            self._security_listener = get_security_listener(enabled=True)
            logger.info(
                "SecurityListener activo (observador pasivo de eventos de seguridad)"
            )
        except Exception as e:
            log_degraded(logger, e, "src/core/dispatcher.py:__init__")

        # ✅ Fase 6 — FeedbackLoop lazy: se inicializa en primer uso (es async)
        self._feedback_loop = None
        self._feedback_loop_initialized = False

        # ✅ v0.9.0: Contador para trigger periódico del learning loop
        self._process_call_count: int = 0
        self._learning_loop_interval: int = 100  # cada N requests

        # ✅ Response cache en RAM — evita inferencia LLM para prompts repetidos
        # ✅ FIX AUD-FIFO-001: OrderedDict + move_to_end en cada hit → LRU real
        # (antes era dict plano con next(iter(...)) en la evicción → FIFO puro)
        self._response_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._response_cache_ttl: Dict[str, int] = {
            "general_chat":        600,   # 10 min
            "web_search":          300,   #  5 min
            "news_search":         300,   #  5 min
            "data_analysis":       120,   #  2 min
            "document_analysis":   120,   #  2 min
            "weather_query":        60,   #  1 min
            "finance_query":        60,   #  1 min
            "news_query":          120,   #  2 min
            "academic_search":     600,   # 10 min
            "research":           1800,   # 30 min
            "code_review":         300,   #  5 min — v1.0.6
            "screenshot_analysis":  30,   # 30s — v0.3.6: frames cambian frecuentemente
            # ✅ v1.3.0: location_query — ubicación puede cambiar si usuario viaja
            "location_query":      300,   #  5 min
        }
        self._response_cache_default_ttl = 300  # 5 min fallback
        self._response_cache_max = 200
        self._response_cache_lock = threading.Lock()

        # ✅ [B1]: cache de partials por session_id
        self._partial_cache: Dict[str, str] = {}

        # ✅ CAAC Fase 0: store de SessionContext por session_id (en memoria, thread-safe)
        self._session_store: Dict[str, Any] = {}
        self._session_store_lock = threading.Lock()

        # ✅ v0.9.2: Semáforos de concurrencia por partición de velocidad
        self._sem_slow   = asyncio.Semaphore(1)   # code_*
        self._sem_medium = asyncio.Semaphore(2)   # search_*, analysis_*
        self._sem_fast   = asyncio.Semaphore(8)   # chat, data

        duration = time.time() - start_init
        logger.info(f"Dispatcher initialized in {duration * 1000:.2f}ms")
        _log_event("DISPATCHER", "Dispatcher initialized", extra={
            "init_duration_ms": round(duration * 1000, 2),
            "cache_enabled": enable_cache,
            "cache_size": cache_size,
            "metrics_max_entries": metrics_max_entries,
            "tools_available": TOOLS_AVAILABLE,
            "agent_factory_injected": agent_factory is not None,
            "user_profile_active": self._user_profile is not None,
            "sensor_enabled": self._sensor_enabled,
            "data_tasks": list(self.DATA_TASKS),
            "code_tasks": list(self.CODE_TASKS),
            "search_tasks": list(self.SEARCH_TASKS),
            "validation_tasks": list(self.VALIDATION_TASKS),
            "jarvis_tasks": list(self.JARVIS_TASKS),
            "jarvis_available": _JARVIS_AVAILABLE,
        })

    async def _ensure_feedback_loop(self):
        """Lazy init del FeedbackLoop singleton (es async — no puede ir en __init__)."""
        if not self._feedback_loop_initialized and _FEEDBACK_AVAILABLE:
            try:
                self._feedback_loop = await _get_feedback_loop()
                self._feedback_loop_initialized = True
                logger.debug("FeedbackLoop inicializado en Dispatcher")
            except Exception as e:
                logger.warning(f"FeedbackLoop no disponible: {e}")
            self._feedback_loop_initialized = True  # no reintentar

    # ── Cache / Metrics classmethods ─────────────────────────────────────────
    @classmethod
    def _get_routing_cache(cls, size: int = 500) -> RoutingCache:
        with cls._cache_lock:
            if cls._routing_cache is None:
                cls._routing_cache = RoutingCache(max_size=size)
        return cls._routing_cache

    @classmethod
    def _get_metrics(cls, max_entries: int = 1000) -> _DispatcherMetrics:
        with cls._cache_lock:
            if cls._metrics is None:
                cls._metrics = _DispatcherMetrics(max_entries=max_entries)
        return cls._metrics

    # ── Lazy properties ──────────────────────────────────────────────────────
    @property
    def classifier(self) -> IntentClassifier:
        if self._classifier is None:
            self._classifier = IntentClassifier(threshold=settings.validation_threshold)
            logger.debug(f"IntentClassifier lazy initialized (threshold={settings.validation_threshold})")
            _log_event("DISPATCHER", "IntentClassifier lazy initialized", extra={
                "threshold": settings.validation_threshold,
            })
        return self._classifier

    @property
    def data_handler(self) -> DataHandler:
        if self._data_handler is None:
            self._data_handler = DataHandler(timeout=10)
            logger.debug("DataHandler lazy initialized")
            _log_event("DISPATCHER", "DataHandler lazy initialized")
        return self._data_handler

    @property
    def coder(self) -> CoderAgent:
        if self._coder is None:
            self._coder = (
                self._agent_factory("coder") if self._agent_factory else CoderAgent()
            )
            logger.debug("CoderAgent lazy initialized")
            _log_event("DISPATCHER", "CoderAgent lazy initialized")
        return self._coder

    @property
    def searcher(self) -> SearcherAgent:
        if self._searcher is None:
            self._searcher = (
                self._agent_factory("searcher") if self._agent_factory else SearcherAgent()
            )
            logger.debug("SearcherAgent lazy initialized")
            _log_event("DISPATCHER", "SearcherAgent lazy initialized")
        return self._searcher

    @property
    def researcher(self) -> ResearcherAgent:
        if self._researcher is None:
            self._researcher = (
                self._agent_factory("researcher") if self._agent_factory else ResearcherAgent()
            )
            logger.debug("ResearcherAgent lazy initialized")
            _log_event("DISPATCHER", "ResearcherAgent lazy initialized")
        return self._researcher

    @property
    def validator(self) -> ValidatorAgent:
        if self._validator is None:
            self._validator = (
                self._agent_factory("validator") if self._agent_factory
                else ValidatorAgent(model=getattr(settings, 'validator_model', None) or settings.llm_fallback_1 or settings.llm_default_model)  # v0.6.9h
            )
            logger.debug(f"ValidatorAgent lazy initialized (model={settings.llm_fallback_1})")
            _log_event("DISPATCHER", "ValidatorAgent lazy initialized", extra={
                "model": settings.llm_fallback_1,
            })
        return self._validator

    @property
    def system_agent(self):
        """✅ Semana 5 F2: SystemAgent lazy init."""
        if not hasattr(self, '_system_agent') or self._system_agent is None:
            try:
                from src.agents.system_agent import SystemAgent
                self._system_agent = SystemAgent()
                logger.debug("SystemAgent lazy initialized")
            except Exception as e:
                logger.warning(f"SystemAgent no disponible: {e}")
                self._system_agent = None
        return self._system_agent

    @property
    def vision_agent(self):
        """✅ v0.3.6: VisionAgent lazy init — qwen2.5-vl:7b."""
        if not hasattr(self, '_vision_agent') or self._vision_agent is None:
            try:
                from src.agents.vision_agent import VisionAgent
                self._vision_agent = VisionAgent()
                logger.debug("VisionAgent lazy initialized")
                _log_event("DISPATCHER", "VisionAgent lazy initialized", extra={
                    "model": getattr(settings, "llm_vision_model", "qwen2.5-vl:7b"),
                })
            except Exception as e:
                logger.warning(f"VisionAgent no disponible: {e}")
                self._vision_agent = None
        return self._vision_agent

    @property
    def screen_capture(self):
        """v0.4.0 [D1]: ScreenCaptureEngine lazy init — reusado por JarvisEngine y screenshot_analysis."""
        if not hasattr(self, '_screen_capture') or self._screen_capture is None:
            try:
                from src.multimodal.screen_capture_engine import ScreenCaptureEngine
                self._screen_capture = ScreenCaptureEngine()
                logger.debug("ScreenCaptureEngine lazy initialized")
            except Exception as e:
                logger.warning(f"ScreenCaptureEngine no disponible: {e}")
                self._screen_capture = None
        return self._screen_capture

    @property
    def jarvis_engine(self):
        """v0.4.0: JarvisEngine lazy init."""
        if not hasattr(self, '_jarvis_engine') or self._jarvis_engine is None:
            if _JARVIS_AVAILABLE and _JarvisEngine is not None:
                try:
                    self._jarvis_engine = _JarvisEngine(dispatcher=self)
                    logger.debug("JarvisEngine lazy initialized")
                    _log_event("DISPATCHER", "JarvisEngine lazy initialized")
                except Exception as e:
                    logger.warning(f"JarvisEngine no disponible: {e}")
                    self._jarvis_engine = None
            else:
                self._jarvis_engine = None
        return self._jarvis_engine

    @property
    def llm_client(self) -> OllamaClient:
        if self._llm_client is None:
            # ✅ v0.9.4: get_active_client() detecta vLLM en localhost:8000.
            # Si no está disponible retorna OllamaClient — comportamiento idéntico al anterior.
            if _VLLM_FACTORY_AVAILABLE and _get_active_llm_client is not None:
                self._llm_client = _get_active_llm_client()
            else:
                self._llm_client = OllamaClient()
            backend = type(self._llm_client).__name__
            logger.debug(f"{backend} lazy initialized")
            _log_event("DISPATCHER", f"{backend} lazy initialized", extra={
                "backend": backend,
            })
        return self._llm_client

    async def _handle_jarvis(
        self,
        message: "Message",
        task_type: str,
        session_id: Optional[str],
        context: Optional[str],
    ) -> "Response":
        """
        Handler para tareas del Modo Jarvis.
        Delega a JarvisEngine si disponible, fallback a _handle_chat.
        Tasks: jarvis_query | screen_describe | autonomous_task
        """
        engine = self.jarvis_engine
        if engine is not None:
            try:
                _log_event("DISPATCHER", f"JarvisEngine handling: {task_type}", extra={
                    "session_id": session_id,
                    "task_type": task_type,
                })
                return await engine.handle(message, task_type, session_id, context)
            except Exception as e:
                logger.warning(f"JarvisEngine fallo, fallback a chat: {e}")

        # Fallback: tratar como general_chat
        return await self._handle_chat(message, "general_chat", session_id, context)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v1.3.0: HANDLER PARA LOCATION_QUERY
    # ═══════════════════════════════════════════════════════════════
    async def _handle_location(
        self,
        message: "Message",
        task_type: str,
        session_id: Optional[str],
        context: Optional[str],
    ) -> "Response":
        """
        Handler para consultas de ubicación general.
        Usa Geolocator para detectar ubicación del usuario y responde
        con información contextual (país, ciudad, zona horaria, hora local).

        Args:
            message: Mensaje del usuario
            task_type: Tipo de tarea (location_query)
            session_id: ID de sesión
            context: Contexto adicional

        Returns:
            Response con información de ubicación
        """
        start_time = datetime.now(timezone.utc)
        query = message.content or ""

        try:
            # Importar Geolocator
            from src.utils.geolocator import get_geolocator

            _log_event("DISPATCHER", f"Handling location query: {query[:50]}", extra={
                "session_id": session_id,
                "task_type": task_type,
            })

            # Obtener instancia de Geolocator
            geolocator = get_geolocator(use_ip_lookup=True)

            # Detectar ubicación
            location = geolocator.get_location()
            local_dt = location.local_datetime

            # Construir respuesta
            response_text = (
                f"📍 **Tu ubicación actual:**\n\n"
                f"**País:** {location.country} ({location.country_code})\n"
                f"**Ciudad:** {location.city}\n"
                f"**Región:** {location.region}\n"
                f"**Zona horaria:** {location.timezone or 'UTC'}\n"
                f"**Fecha y hora local:** {local_dt['date']} {local_dt['time']}\n\n"
                f"Esta información se obtuvo mediante geolocalización por IP y "
                f"configuración del sistema."
            )

            latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

            _log_event("DISPATCHER", "Location query completed", extra={
                "session_id": session_id,
                "location": location.full_location,
                "latency_ms": round(latency, 2),
            })

            return Response(
                content=response_text,
                session_id=session_id,
                model_used="geolocator",
                task_type=task_type,
                latency_ms=latency,
                confidence=0.95,
                sources=["ip-api.com", "system_timezone"],
                metadata={
                    "location_data": location.to_dict(),
                    "detection_method": "geolocator",
                }
            )

        except Exception as e:
            tb = traceback.format_exc()
            _log_error(e, "_handle_location", extra_info={
                "query": query[:100],
                "session_id": session_id,
                "traceback": tb,
            })
            logger.error(f"Location handler error: {e}", exc_info=True)

            return Response(
                content=f"❌ Error al obtener ubicación: {str(e)}",
                session_id=session_id,
                model_used="geolocator",
                task_type=task_type,
                latency_ms=0.0,
                confidence=0.0,
                error=str(e)
            )

    def _handler_name(self, task_type: str) -> str:
        """Mapea task_type al nombre del handler para métricas."""
        if task_type in self.DATA_TASKS:
            return "DataHandler"
        if task_type in self.CODE_TASKS:
            return "CoderAgent"
        if task_type in self.SEARCH_TASKS:
            return "SearcherAgent"
        if task_type in self.FILE_TASKS:
            return "FileSearchAgent"  # ✅ plan_rafael (2026-08-31)
        if task_type in self.ANALYSIS_TASKS:
            return "LLM_Analysis"
        if task_type in self.VALIDATION_TASKS:
            return "ValidatorAgent"
        if task_type in self.SYSTEM_TASKS:
            return "SystemAgent"
        if task_type in self.VISION_TASKS:
            return "VisionAgent"
        # ✅ v1.0.6: code_review va por analysis path
        if task_type == "code_review":
            return "LLM_Analysis"
        if task_type in self.JARVIS_TASKS:
            return "JarvisEngine"
        # ✅ v1.3.0: location_query usa Geolocator
        if task_type == "location_query":
            return "Geolocator"
        return "LLM_Chat"
