#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# jarvis_engine.py - Motor del Modo Jarvis
# Ruta: src/core/jarvis_engine.py
# Autor: SaintWick
# Versión: 1.1.0
#
# Núcleo de procesamiento para el Modo Jarvis. Maneja los
# task_types exclusivos: jarvis_query (respuesta corta),
# screen_describe (captura + visión), camera_describe y
# autonomous_task (planificación y ejecución encadenada).
# Integra con detailed_logger para trazabilidad completa.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.domain.entities import Message, Response
from config.settings import settings

if TYPE_CHECKING:
    from src.core.dispatcher import Dispatcher

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# ✅ v1.1.0: detailed_logger — import seguro (doble capa)
# ═══════════════════════════════════════════════════════════════
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _DETAILED_LOGGER_AVAILABLE = True
except ImportError:
    _DETAILED_LOGGER_AVAILABLE = False
    _get_detailed_logger = None  # type: ignore[assignment]


def _dlog() -> Optional[Any]:
    """Retorna instancia del detailed_logger o None si no disponible."""
    if not _DETAILED_LOGGER_AVAILABLE:
        return None
    try:
        lgr = _get_detailed_logger()
        if lgr.is_initialized:
            return lgr
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:49] excepción degradada (intencional): {_d2e}")
    return None


def _emit_state(state: str) -> None:
    """Wrapper seguro para state_emitter."""
    try:
        from src.core.state_emitter import emit_state
        emit_state(state)
    except (ImportError, Exception) as _d2e:
        logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:59] excepción degradada (intencional): {_d2e}")


# ── Imports opcionales ────────────────────────────────────────────────────────
try:
    from src.llm.vllm_client import get_active_client as _get_active_llm
    _VLLM_AVAILABLE = True
except ImportError:
    _VLLM_AVAILABLE = False
    _get_active_llm = None  # type: ignore[assignment]

try:
    from src.llm.client import OllamaClient
    _OLLAMA_AVAILABLE = True
except ImportError:
    _OLLAMA_AVAILABLE = False
    OllamaClient = None  # type: ignore[assignment]

try:
    from src.agents.vision_agent import VisionAgent
    _VISION_AVAILABLE = True
except ImportError:
    _VISION_AVAILABLE = False
    VisionAgent = None  # type: ignore[assignment]

try:
    from src.agents.system_agent import SystemAgent
    _SYSTEM_AGENT_AVAILABLE = True
except ImportError:
    _SYSTEM_AGENT_AVAILABLE = False
    SystemAgent = None  # type: ignore[assignment]

try:
    from src.core.planner import MultiStepPlanner
    _PLANNER_AVAILABLE = True
except ImportError:
    _PLANNER_AVAILABLE = False
    MultiStepPlanner = None  # type: ignore[assignment]


# ── Prompts de sistema ────────────────────────────────────────────────────────
_JARVIS_SYSTEM_PROMPT = (
    "Eres AXIOMA en Modo Jarvis. Respondé de forma directa, concisa y en primera persona. "
    "Sin preámbulos, sin despedidas, sin explicaciones innecesarias. "
    "Si la pregunta es ambigua, respondé con la interpretación más probable. "
    "Máximo 2-3 oraciones por respuesta."
)

_SCREEN_DESCRIBE_SYSTEM_PROMPT = (
    "Describí brevemente lo que ves en esta captura de pantalla. "
    "Identificá: aplicación activa, contenido principal, cualquier elemento relevante. "
    "Sé conciso — máximo 3 oraciones."
)

_CAMERA_DESCRIBE_SYSTEM_PROMPT = (
    "Describí brevemente lo que ves en esta imagen capturada por cámara. "
    "Identificá: personas presentes, objetos relevantes, escena general. "
    "Sé conciso — máximo 3 oraciones."
)

_AUTONOMOUS_PLANNER_PROMPT = (
    "Sos un planificador de tareas autónomo. "
    "Dado un objetivo, generá una lista de pasos concretos y ejecutables. "
    "Respondé SOLO con JSON: {{\"steps\": [\"paso 1\", \"paso 2\", ...]}}. "
    "Máximo {max_steps} pasos. Sin explicaciones fuera del JSON."
)


# ═══════════════════════════════════════════════════════════════
# CLASE: JarvisEngine
# ═══════════════════════════════════════════════════════════════
class JarvisEngine:
    """
    Motor del Modo Jarvis — maneja los 3 TaskTypes exclusivos:
    - jarvis_query: consulta directa al asistente con respuesta corta
    - screen_describe: captura + descripción visual de la pantalla activa
    - autonomous_task: planificación y ejecución encadenada de pasos

    Se instancia una vez por Dispatcher (lazy) y reutiliza sus agentes.

    Args:
        dispatcher: Instancia del Dispatcher que lo contiene.
                    Permite acceder a agentes ya inicializados (vision_agent, system_agent, etc.)
    """

    def __init__(self, dispatcher: Optional["Dispatcher"] = None) -> None:
        self._dispatcher = dispatcher
        self._llm: Optional[Any] = None
        self._vision: Optional[Any] = None
        self._system: Optional[Any] = None
        self._planner: Optional[Any] = None
        self._screen_capture: Optional[Any] = None
        self._camera: Optional[Any] = None

        # Cargar config de Jarvis desde settings
        _cfg: Dict[str, Any] = {}
        if hasattr(settings, 'jarvis_config'):
            _cfg = settings.jarvis_config

        self._response_max_tokens: int = _cfg.get("response_max_tokens", 150)
        self._confidence_threshold: float = _cfg.get("voice_confidence_threshold", 0.6)
        self._autonomous_max_steps: int = _cfg.get("autonomous_task_max_steps", 5)
        self._autonomous_timeout: int = _cfg.get("autonomous_task_timeout", 120)
        self._screen_describe_on_query: bool = _cfg.get("screen_describe_on_query", False)

        logger.info(
            f"JarvisEngine inicializado — max_tokens={self._response_max_tokens}, "
            f"autonomous_steps={self._autonomous_max_steps}"
        )

        # ✅ v1.1.0: Log en detailed_logger
        dl = _dlog()
        if dl:
            dl.log_event("JARVIS", "JarvisEngine inicializado", level="INFO", extra={
                "max_tokens": self._response_max_tokens,
                "autonomous_max_steps": self._autonomous_max_steps,
                "autonomous_timeout": self._autonomous_timeout,
                "screen_describe_on_query": self._screen_describe_on_query,
            })

    # ── Lazy helpers ─────────────────────────────────────────────────────────

    def _get_llm(self) -> Any:
        """Retorna cliente LLM activo (vLLM o Ollama)."""
        if self._llm is None:
            if _VLLM_AVAILABLE and _get_active_llm is not None:
                try:
                    self._llm = _get_active_llm(prefer_vllm=False)
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:188] excepción degradada (intencional): {_d2e}")
            if self._llm is None and _OLLAMA_AVAILABLE and OllamaClient is not None:
                self._llm = OllamaClient(model=settings.llm_default_model)
        return self._llm

    def _get_vision(self) -> Optional[Any]:
        """Retorna VisionAgent desde dispatcher si disponible."""
        if self._vision is None:
            if self._dispatcher is not None and hasattr(self._dispatcher, 'vision_agent'):
                self._vision = self._dispatcher.vision_agent
            elif _VISION_AVAILABLE and VisionAgent is not None:
                try:
                    self._vision = VisionAgent()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:202] excepción degradada (intencional): {_d2e}")
        return self._vision

    def _get_system(self) -> Optional[Any]:
        """Retorna SystemAgent desde dispatcher si disponible."""
        if self._system is None:
            if self._dispatcher is not None and hasattr(self._dispatcher, 'system_agent'):
                self._system = self._dispatcher.system_agent
            elif _SYSTEM_AGENT_AVAILABLE and SystemAgent is not None:
                try:
                    self._system = SystemAgent()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:214] excepción degradada (intencional): {_d2e}")
        return self._system

    def _get_screen_capture(self) -> Optional[Any]:
        """Retorna ScreenCaptureEngine desde dispatcher si disponible."""
        if self._screen_capture is None:
            if self._dispatcher is not None and hasattr(self._dispatcher, 'screen_capture'):
                self._screen_capture = self._dispatcher.screen_capture
            else:
                try:
                    from src.multimodal.screen_capture_engine import ScreenCaptureEngine
                    self._screen_capture = ScreenCaptureEngine()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:227] excepción degradada (intencional): {_d2e}")
        return self._screen_capture

    def _get_camera(self) -> Optional[Any]:
        """Retorna CameraCaptureEngine desde dispatcher si disponible, o instancia una nueva."""
        if self._camera is None:
            if self._dispatcher is not None and hasattr(self._dispatcher, 'camera_capture'):
                self._camera = self._dispatcher.camera_capture
            else:
                try:
                    from src.multimodal.camera_engine import CameraCaptureEngine
                    self._camera = CameraCaptureEngine()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:240] excepción degradada (intencional): {_d2e}")
        return self._camera

    def _get_planner(self) -> Optional[Any]:
        """Retorna MultiStepPlanner si disponible."""
        if self._planner is None:
            if self._dispatcher is not None and hasattr(self._dispatcher, '_planner'):
                self._planner = self._dispatcher._planner
            elif _PLANNER_AVAILABLE and MultiStepPlanner is not None:
                try:
                    self._planner = MultiStepPlanner()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:252] excepción degradada (intencional): {_d2e}")
        return self._planner

    # ── Punto de entrada principal ────────────────────────────────────────────

    async def handle(
        self,
        message: Message,
        task_type: str,
        session_id: Optional[str],
        context: Optional[str],
    ) -> Response:
        """
        Despachador interno de tasks Jarvis.

        Args:
            message: Mensaje del usuario
            task_type: "jarvis_query" | "screen_describe" | "autonomous_task"
            session_id: ID de sesión
            context: Contexto serializado (puede ser None)

        Returns:
            Response con la respuesta del motor Jarvis
        """
        start = time.perf_counter()
        _emit_state("COMPUTING")

        # ✅ v1.1.0: Log inicio de ejecución en detailed_logger
        dl = _dlog()
        if dl:
            dl.log_agent_execution(
                agent_name="JarvisEngine",
                task_type=task_type,
                state="started",
                success=True,
            )

        try:
            if task_type == "jarvis_query":
                content = await self._handle_jarvis_query(message, session_id, context)
            elif task_type == "screen_describe":
                content = await self._handle_screen_describe(message, session_id)
            elif task_type == "autonomous_task":
                content = await self._handle_autonomous_task(message, session_id)
            elif task_type == "camera_describe":
                content = await self._handle_camera_describe(message, session_id)
            else:
                content = f"JarvisEngine: task_type desconocido '{task_type}'"

            latency = (time.perf_counter() - start) * 1000
            _emit_state("IDLE")

            # ✅ v1.1.0: Log éxito
            if dl:
                dl.log_agent_execution(
                    agent_name="JarvisEngine",
                    task_type=task_type,
                    state="completed",
                    duration=latency / 1000,
                    success=True,
                )

            return Response(
                content=content,
                session_id=session_id,
                model_used=getattr(settings, 'llm_default_model', 'jarvis'),
                task_type=task_type,
                latency_ms=round(latency, 2),
                confidence=0.9,
                metadata={"jarvis_mode": True, "task_type": task_type},
            )

        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            _emit_state("IDLE")

            logger.error(f"JarvisEngine.handle error ({task_type}): {e}", exc_info=True)

            # ✅ v1.1.0: Log error detallado
            if dl:
                dl.log_error(
                    error=e,
                    context=f"JarvisEngine.handle — task_type={task_type}",
                    extra_info={
                        "task_type": task_type,
                        "session_id": session_id or "none",
                        "latency_ms": round(latency, 2),
                    },
                    function_name="JarvisEngine.handle",
                )
                dl.log_agent_execution(
                    agent_name="JarvisEngine",
                    task_type=task_type,
                    state="failed",
                    duration=latency / 1000,
                    success=False,
                    error=str(e),
                )

            return Response(
                content=f"❌ Error en Modo Jarvis: {e}",
                session_id=session_id,
                model_used="jarvis",
                task_type=task_type,
                latency_ms=round(latency, 2),
                confidence=0.0,
                error=str(e),
                metadata={"jarvis_mode": True},
            )

    # ── Handlers por task type ────────────────────────────────────────────────

    async def _handle_jarvis_query(
        self,
        message: Message,
        session_id: Optional[str],
        context: Optional[str],
    ) -> str:
        """
        Consulta directa al LLM con respuesta corta.
        Si screen_describe_on_query está activo, captura pantalla como contexto adicional.
        """
        query = message.content
        dl = _dlog()

        # Contexto de pantalla opcional
        screen_ctx = ""
        if self._screen_describe_on_query:
            try:
                screen_ctx = await self._capture_screen_description()
                if screen_ctx:
                    screen_ctx = f"\n[Pantalla actual: {screen_ctx}]"
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:385] excepción degradada (intencional): {_d2e}")

        prompt = f"{query}{screen_ctx}"

        llm = self._get_llm()
        if llm is None:
            if dl:
                dl.log_event("JARVIS", "LLM no disponible en jarvis_query", level="WARNING")
            return "❌ Cliente LLM no disponible en Modo Jarvis."

        start_llm = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                llm.generate,
                prompt=prompt,
                system=_JARVIS_SYSTEM_PROMPT,
                temperature=0.4,
                max_tokens=self._response_max_tokens,
            )

            duration = time.perf_counter() - start_llm

            # ✅ v1.1.0: Log model call
            if dl:
                model_name = getattr(llm, 'model', 'jarvis')
                output_text = ""
                if hasattr(result, 'content'):
                    output_text = result.content or ""
                elif isinstance(result, dict):
                    output_text = result.get("response", result.get("content", str(result)))
                else:
                    output_text = str(result)
                dl.log_model_call(
                    model_name=model_name,
                    action="generate",
                    task_type="jarvis_query",
                    input_size=len(prompt),
                    output_size=len(output_text),
                    duration=duration,
                    success=True,
                    temperature=0.4,
                )

            if hasattr(result, 'content'):
                return (result.content or "").strip()
            if isinstance(result, dict):
                return result.get("response", result.get("content", str(result))).strip()
            return str(result).strip()

        except Exception as e:
            duration = time.perf_counter() - start_llm
            logger.error(f"jarvis_query LLM error: {e}")

            # ✅ v1.1.0: Log error de LLM
            if dl:
                model_name = getattr(llm, 'model', 'jarvis')
                dl.log_model_call(
                    model_name=model_name,
                    action="generate",
                    task_type="jarvis_query",
                    input_size=len(prompt),
                    duration=duration,
                    success=False,
                )
                dl.log_error(
                    error=e,
                    context="JarvisEngine._handle_jarvis_query — LLM call",
                    function_name="_handle_jarvis_query",
                )
            raise

    async def _handle_screen_describe(
        self,
        message: Message,
        session_id: Optional[str],
    ) -> str:
        """
        Captura la pantalla activa y la describe verbalmente usando VisionAgent.
        Fallback: describe la pantalla usando SystemAgent (scrot) + texto plano.
        """
        dl = _dlog()
        vision = self._get_vision()
        screen_capture = self._get_screen_capture()

        if vision is not None and screen_capture is not None:
            try:
                frame = await screen_capture.capture_async()
                if frame is None or not frame.image_b64:
                    raise RuntimeError("ScreenCaptureEngine no devolvió imagen")

                screen_msg = Message(
                    content=_SCREEN_DESCRIBE_SYSTEM_PROMPT,
                    role=message.role,
                )
                if dl:
                    dl.log_event("JARVIS", "screen_describe: VisionAgent invocado", level="INFO", extra={
                        "backend_used": frame.backend_used,
                        "context_hint": frame.context_hint,
                    })

                result = await asyncio.to_thread(
                    vision.execute,
                    screen_msg,
                    task_type="screen_describe",
                    image_b64=frame.image_b64,
                    context_hint=frame.context_hint,
                    active_window=frame.active_window,
                )

                if hasattr(result, 'content') and result.content:
                    if dl:
                        dl.log_event("JARVIS", "screen_describe: VisionAgent OK",
                                     level="INFO", extra={"content_length": len(result.content)})
                    return result.content

            except Exception as e:
                logger.warning(f"VisionAgent screen_describe falló: {e}")
                if dl:
                    dl.log_error(
                        error=e,
                        context="JarvisEngine._handle_screen_describe — VisionAgent",
                        function_name="_handle_screen_describe",
                        extra_info={"fallback_to": "scrot_text"},
                    )

        # Fallback: captura con scrot + descripción textual simple
        if dl:
            dl.log_event("JARVIS", "screen_describe: usando fallback scrot", level="WARNING")
        return await self._screen_describe_fallback()

    async def _screen_describe_fallback(self) -> str:
        """
        Fallback para screen_describe: captura con scrot y retorna mensaje básico.
        No usa LLM — solo confirma la captura o reporta error.
        """
        system = self._get_system()
        dl = _dlog()

        if system is not None:
            try:
                msg = Message(content="captura screenshot", role="user")
                result = await asyncio.to_thread(
                    system.execute,
                    msg,
                    task_type="screenshot",
                )
                if hasattr(result, 'success') and result.success:
                    if dl:
                        dl.log_event("JARVIS", "screen_describe fallback: captura OK", level="INFO")
                    return "📸 Captura tomada en /tmp/axioma_screenshot.png — VisionAgent no disponible para describir."
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:536] excepción degradada (intencional): {_d2e}")

        if dl:
            dl.log_event("JARVIS", "screen_describe fallback: agentes no disponibles", level="ERROR")
        return "❌ No fue posible capturar o describir la pantalla (VisionAgent y SystemAgent no disponibles)."

    async def _capture_screen_description(self) -> str:
        """Helper: captura y describe pantalla, retorna string corto o vacío."""
        vision = self._get_vision()
        screen_capture = self._get_screen_capture()
        if vision is None or screen_capture is None:
            return ""
        try:
            frame = await screen_capture.capture_async()
            if frame is None or not frame.image_b64:
                return ""
            msg = Message(content="Describí brevemente la pantalla.", role="user")
            result = await asyncio.to_thread(
                vision.execute,
                msg,
                task_type="screen_describe",
                image_b64=frame.image_b64,
                context_hint=frame.context_hint,
                active_window=frame.active_window,
            )
            if hasattr(result, 'content') and result.content:
                return result.content[:200]
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:564] excepción degradada (intencional): {_d2e}")
        return ""

    async def _handle_camera_describe(
        self,
        message: Message,
        session_id: Optional[str],
    ) -> str:
        """
        Captura la cámara y la describe verbalmente usando VisionAgent.
        Emite WATCHING mientras el frame existe — handle() restaura IDLE al final
        (éxito o excepción), por eso no hace falta un finally aquí.
        """
        dl = _dlog()
        vision = self._get_vision()
        camera = self._get_camera()

        if vision is None or camera is None:
            if dl:
                dl.log_event("JARVIS", "camera_describe: cámara o VisionAgent no disponibles", level="ERROR")
            return "❌ No fue posible capturar o describir la cámara (CameraCaptureEngine o VisionAgent no disponibles)."

        try:
            _emit_state("WATCHING")
            frame = await camera.capture_async()
            if frame is None or not frame.image_b64:
                raise RuntimeError("CameraCaptureEngine no devolvió imagen")

            camera_msg = Message(
                content=_CAMERA_DESCRIBE_SYSTEM_PROMPT,
                role=message.role,
            )
            if dl:
                dl.log_event("JARVIS", "camera_describe: VisionAgent invocado", level="INFO", extra={
                    "backend_used": frame.backend_used,
                })

            result = await asyncio.to_thread(
                vision.execute,
                camera_msg,
                task_type="camera_describe",
                image_b64=frame.image_b64,
                context_hint=frame.context_hint,
                active_window=frame.active_window,
            )

            if hasattr(result, 'content') and result.content:
                if dl:
                    dl.log_event("JARVIS", "camera_describe: VisionAgent OK",
                                 level="INFO", extra={"content_length": len(result.content)})
                return result.content

        except Exception as e:
            logger.warning(f"VisionAgent camera_describe falló: {e}")
            if dl:
                dl.log_error(
                    error=e,
                    context="JarvisEngine._handle_camera_describe — VisionAgent",
                    function_name="_handle_camera_describe",
                    extra_info={"fallback_to": "camera_unavailable"},
                )

        return "❌ No fue posible describir la imagen de la cámara (VisionAgent sin respuesta)."

    async def _handle_autonomous_task(
        self,
        message: Message,
        session_id: Optional[str],
    ) -> str:
        """
        Tarea autónoma encadenada: planifica pasos → ejecuta cada uno → reporta.

        Flujo:
        1. LLM genera lista de pasos en JSON
        2. Para cada paso: intenta ejecutar via SystemAgent o responder via LLM
        3. Agrega resultado al historial interno
        4. Retorna resumen de ejecución
        """
        objective = message.content
        max_steps = self._autonomous_max_steps
        timeout = self._autonomous_timeout
        dl = _dlog()

        if dl:
            dl.log_event("JARVIS", "autonomous_task: planificando", level="INFO",
                         extra={"objective": objective[:100], "max_steps": max_steps})

        steps = await self._plan_steps(objective, max_steps)
        if not steps:
            if dl:
                dl.log_event("JARVIS", "autonomous_task: planificación falló", level="ERROR")
            return f"❌ No pude planificar pasos para: {objective}"

        results: List[str] = []
        start_global = time.perf_counter()

        for i, step in enumerate(steps, 1):
            elapsed = time.perf_counter() - start_global
            if elapsed > timeout:
                results.append(f"⏱️ Timeout alcanzado después de {i - 1} pasos.")
                if dl:
                    dl.log_event("JARVIS", f"autonomous_task: TIMEOUT en step {i}",
                                 level="WARNING", extra={"elapsed": round(elapsed, 2)})
                break

            step_result = await self._execute_step(step, i, objective)
            results.append(f"**Paso {i}:** {step}\n→ {step_result}")
            logger.debug(f"[JarvisEngine] autonomous step {i}/{len(steps)}: {step_result[:80]}")

        total = round(time.perf_counter() - start_global, 2)
        summary = "\n".join(results)

        if dl:
            dl.log_event("JARVIS", "autonomous_task: completado", level="INFO", extra={
                "steps_completed": len(results),
                "total_seconds": total,
            })

        return f"✅ Tarea autónoma completada ({len(results)} pasos, {total}s):\n{summary}"

    async def _plan_steps(self, objective: str, max_steps: int) -> List[str]:
        """
        Usa LLM para generar una lista de pasos en JSON.
        Retorna lista vacía si falla.
        """
        dl = _dlog()

        # Intentar con MultiStepPlanner primero
        planner = self._get_planner()
        if planner is not None:
            try:
                plan = await asyncio.to_thread(planner.plan, objective)
                if plan and isinstance(plan, list):
                    if dl:
                        dl.log_event("JARVIS", "plan_steps: MultiStepPlanner OK",
                                     level="INFO", extra={"steps": len(plan)})
                    return plan[:max_steps]
            except Exception as e:
                logger.warning(f"MultiStepPlanner falló: {e}")
                if dl:
                    dl.log_error(e, context="JarvisEngine._plan_steps — MultiStepPlanner",
                                 function_name="_plan_steps")

        # Fallback: LLM directo con respuesta JSON
        llm = self._get_llm()
        if llm is None:
            return []

        system_prompt = _AUTONOMOUS_PLANNER_PROMPT.format(max_steps=max_steps)

        try:
            result = await asyncio.to_thread(
                llm.generate,
                prompt=objective,
                system=system_prompt,
                temperature=0.2,
                max_tokens=512,
            )
            if hasattr(result, 'content'):
                raw = result.content or ""
            elif isinstance(result, dict):
                raw = result.get("response", result.get("content", ""))
            else:
                raw = str(result)
            return self._parse_steps_json(raw, max_steps)

        except Exception as e:
            logger.error(f"_plan_steps LLM error: {e}")
            if dl:
                dl.log_error(e, context="JarvisEngine._plan_steps — LLM",
                             function_name="_plan_steps")
            return []

    def _parse_steps_json(self, raw: str, max_steps: int) -> List[str]:
        """Extrae lista de pasos desde respuesta JSON del LLM."""
        import json
        import re

        # Intentar extraer bloque JSON
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return []

        try:
            data = json.loads(match.group(0))
            steps = data.get("steps", [])
            if isinstance(steps, list):
                return [str(s).strip() for s in steps if str(s).strip()][:max_steps]
        except (json.JSONDecodeError, TypeError) as _d2e:
            logging.getLogger(__name__).debug(f"[D2 jarvis_engine.py:753] excepción degradada (intencional): {_d2e}")
        return []

    async def _execute_step(self, step: str, step_num: int, objective: str) -> str:
        """
        Ejecuta un paso individual.
        Intenta SystemAgent para acciones OS, fallback a LLM para pasos cognitivos.
        """
        step_lower = step.lower()
        dl = _dlog()

        # Detectar si es una acción OS — infinitivo Y voseo imperativo
        # (mismo set que _AUTONOMOUS_TASK_KEYWORDS en pipeline_core.py)
        os_keywords = {
            "abrir", "abrí", "abri", "abre",
            "cerrar", "cerrá", "cerra", "cierra",
            "ejecutar", "ejecutá", "ejecuta",
            "lanzar", "lanzá", "lanza",
            "instalar", "instalá", "instala",
            "crear", "creá", "crea",
            "borrar", "borrá", "borra",
            "reiniciar", "reiniciá", "reinicia",
            "apagar", "apagá", "apaga",
            "correr", "corré", "corre",
            "captura", "screenshot",
            "volumen", "archivo", "carpeta", "directorio", "lista",
        }
        is_os_action = any(kw in step_lower for kw in os_keywords)

        if is_os_action:
            system = self._get_system()
            if system is not None:
                try:
                    msg = Message(content=step, role="user")
                    if dl:
                        dl.log_event("JARVIS", f"step {step_num}: SystemAgent invocado",
                                     level="INFO", extra={"step": step[:80]})
                    result = await asyncio.to_thread(
                        system.execute,
                        msg,
                        task_type="system_control",
                    )
                    if hasattr(result, 'content') and result.content:
                        return result.content
                except Exception as e:
                    logger.warning(f"SystemAgent step {step_num} error: {e}")
                    if dl:
                        dl.log_error(e, context=f"JarvisEngine step {step_num} — SystemAgent",
                                     function_name="_execute_step")

        # Fallback: LLM para pasos cognitivos o cuando SystemAgent falla
        llm = self._get_llm()
        if llm is None:
            return f"(no ejecutado — LLM no disponible)"

        try:
            prompt = (
                f"Objetivo general: {objective}\n"
                f"Paso actual a completar: {step}\n"
                f"Completá este paso y reportá brevemente el resultado."
            )
            result = await asyncio.to_thread(
                llm.generate,
                prompt=prompt,
                system=_JARVIS_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=100,
            )
            if hasattr(result, 'content'):
                raw = result.content or ""
            elif isinstance(result, dict):
                raw = result.get("response", result.get("content", ""))
            else:
                raw = str(result)
            return raw.strip() or "(sin respuesta)"

        except Exception as e:
            if dl:
                dl.log_error(e, context=f"JarvisEngine step {step_num} — LLM fallback",
                             function_name="_execute_step")
            return f"❌ Error: {e}"

    # ── Estadísticas ──────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estado interno del engine para diagnóstico."""
        return {
            "llm_initialized": self._llm is not None,
            "vision_initialized": self._vision is not None,
            "system_initialized": self._system is not None,
            "screen_capture_initialized": self._screen_capture is not None,
            "planner_initialized": self._planner is not None,
            "response_max_tokens": self._response_max_tokens,
            "autonomous_max_steps": self._autonomous_max_steps,
            "autonomous_timeout": self._autonomous_timeout,
            "screen_describe_on_query": self._screen_describe_on_query,
        }

    def __repr__(self) -> str:
        return (
            f"JarvisEngine("
            f"llm={self._llm is not None}, "
            f"vision={self._vision is not None}, "
            f"max_tokens={self._response_max_tokens})"
        )
