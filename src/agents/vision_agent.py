#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# vision_agent.py - Agente de Visión (VisionAgent)
# Ruta: src/agents/vision_agent.py
# Autor: SaintWick
# Versión: 1.6.0
#
# Agente especializado en análisis visual de capturas de pantalla
# utilizando el VLM configurado en settings.llm_vision_model. Proporciona una
# interfaz síncrona y asíncrona para describir imágenes, responder
# preguntas sobre el contenido visual y contextualizar la interfaz
# de usuario. Incluye redimensionamiento automático de imágenes,
# manejo de errores y logging detallado.
# ═══════════════════════════════════════════════════════════════
import asyncio
import base64
import io
import logging
import time
from typing import Any, Dict, Optional, Tuple
from config.settings import settings
from src.domain.entities import Message
# ✅ FIX v1.1.0: AgentResult está en src.agents.base, NO en src.domain.entities
from src.agents.base import AgentResult

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
        logging.getLogger(__name__).debug(f"[D2 vision_agent.py:46] excepción degradada (intencional): {_d2e}")
    return None

# ═══════════════════════════════════════════════════════════════
# ✅ v1.4.0: PROMPTS — FORZADO DE IDIOMA ESPAÑOL
# ═══════════════════════════════════════════════════════════════
_SYSTEM_PROMPT_BASE = (
    "Eres AXIOMA Visual, un asistente experto en análisis de pantalla. "
    "Analizas imágenes de la pantalla del usuario y respondes SIEMPRE en español. "
    "Sé conciso, específico y útil. "
    "Si el usuario hace una pregunta sobre lo que ves, respóndela directamente basándote en la imagen. "
    "Describe elementos visibles con detalle: colores, textos, botones, ventanas, íconos."
)

_CONTEXT_PROMPTS: Dict[str, str] = {
    "web": (
        "Estás viendo una página web. Describe el contenido principal con detalle:\n"
        "1. Título de la página o pestaña visible\n"
        "2. Texto principal y artículos visibles\n"
        "3. Botones, enlaces y elementos interactivos\n"
        "4. Menús de navegación y estructura general\n"
        "5. Cualquier elemento relevante o inusual"
    ),
    "document": (
        "Estás viendo un documento. Describe su contenido con detalle:\n"
        "1. Título y subtítulos del documento\n"
        "2. Secciones principales visibles\n"
        "3. Texto y párrafos importantes\n"
        "4. Tablas, gráficos o imágenes presentes\n"
        "5. Formato general y tipo de documento"
    ),
    "code": (
        "Estás viendo código fuente en un editor. Describe con detalle:\n"
        "1. Lenguaje de programación (Python, JavaScript, etc.)\n"
        "2. Nombre del archivo si es visible en la pestaña\n"
        "3. Función, clase o bloque de código activo\n"
        "4. Líneas de código visibles y su propósito\n"
        "5. Errores, advertencias o mensajes del editor"
    ),
    "image": (
        "Estás viendo una imagen o editor gráfico. Describe con detalle:\n"
        "1. Qué muestra la imagen (escena, personas, objetos)\n"
        "2. Colores dominantes y estilo visual\n"
        "3. Elementos principales y composición\n"
        "4. Herramientas activas si es un editor\n"
        "5. Dimensiones o resolución si son visibles"
    ),
    "generic": (
        "Describe lo que ves en la pantalla con detalle:\n"
        "1. Aplicación o ventana activa\n"
        "2. Contenido principal visible\n"
        "3. Elementos de interfaz (botones, menús, íconos)\n"
        "4. Texto visible y su significado\n"
        "5. Cualquier elemento relevante o inusual"
    ),
}

# ✅ v1.5.1: Modelos que requieren workaround de CPU (num_gpu: 0).
# gemma4:e4b desinstalado (nunca funcionó bien) — set vacío por ahora.
# Si un VLM nuevo muestra el mismo tipo de bug (ej. GGML_ASSERT), agregarlo acá.
_MODELS_REQUIRING_CPU_WORKAROUND: set = set()

def _build_prompt(user_query: str, context_hint: str, active_window: str) -> Tuple[str, str]:
    """Construye el prompt y system para el VLM configurado (llm_vision_model)."""
    ctx_instruction = _CONTEXT_PROMPTS.get(context_hint, _CONTEXT_PROMPTS["generic"])
    parts = [ctx_instruction]
    if active_window:
        parts.append(f"\nVentana activa: {active_window}")
    if user_query and user_query.strip():
        parts.append(f"\nPregunta del usuario: {user_query}")
    return "\n".join(parts), _SYSTEM_PROMPT_BASE


def _resize_image_b64(image_b64: str, max_dim: int = 800, quality: int = 75) -> str:
    """
    ✅ v1.6.0: Redimensiona imagen para el VLM configurado.
    Usa max_dim desde settings por defecto (800px).
    ✅ v0.6.9q: normaliza el modo de color ANTES de guardar JPEG — las
    imágenes RGBA (capturas con canal alfa) fallaban con "cannot write mode
    RGBA as JPEG" y se enviaban SIN re-escalar (respuesta vacía del VLM).
    """
    if not image_b64:
        return image_b64

    try:
        from PIL import Image

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes))

        w, h = img.size
        if w <= max_dim and h <= max_dim:
            return image_b64

        ratio = min(max_dim / w, max_dim / h)
        new_w = max(1, int(w * ratio))
        new_h = max(1, int(h * ratio))

        img_resized = img.resize((new_w, new_h), Image.LANCZOS)

        # ✅ v0.6.9q: RGBA/LA/P/CMYK → RGB (fondo blanco) para poder
        # guardar JPEG. Sin esto el resize fallaba en modo RGBA.
        if img_resized.mode == "RGBA":
            bg = Image.new("RGB", img_resized.size, (255, 255, 255))
            bg.paste(img_resized, mask=img_resized.split()[3])
            img_resized = bg
        elif img_resized.mode != "RGB":
            img_resized = img_resized.convert("RGB")

        buffer = io.BytesIO()
        img_resized.save(buffer, format='JPEG', quality=quality, optimize=True)
        resized_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        logger.info(
            f"[VISION] Image resized: {w}x{h} → {new_w}x{new_h} "
            f"({len(image_b64)} → {len(resized_b64)} chars)"
        )
        return resized_b64

    except ImportError:
        logger.warning("[VISION] PIL not available — skipping resize")
        return image_b64
    except Exception as e:
        logger.warning(f"[VISION] Image resize failed: {e} — using original")
        return image_b64


# ═══════════════════════════════════════════════════════════════
# OLLAMA VISION CLIENT
# ═══════════════════════════════════════════════════════════════
class _OllamaVisionClient:
    """
    Cliente mínimo para Ollama multimodal API.
    ✅ v1.6.0: Simplificado — model-agnostic, no atado a un VLM específico.
    """

    def __init__(self, host: str, model: str, timeout: int) -> None:
        self.host    = host.rstrip("/")
        self.model   = model
        self.timeout = timeout

    def generate(
        self,
        prompt: str,
        image_b64: str,
        system: str = "",
        num_predict: Optional[int] = None,
    ) -> str:
        """Síncrono — llamar desde asyncio.to_thread."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx no disponible — instala: pip install httpx")

        payload: Dict[str, Any] = {
            "model":   self.model,
            "prompt":  prompt,
            "images":  [image_b64],
            "stream":  False,
        }

        # ✅ v0.6.9p: tope de tokens de salida — evita generaciones
        # desbocadas que colgaban hasta el timeout (caso contraste bajo).
        if num_predict:
            payload["options"] = {"num_predict": int(num_predict)}

        # ✅ v1.3.0: Solo incluir "system" si NO está vacío
        if system and system.strip():
            payload["system"] = system

        # ✅ v1.5.1: FIX GGML_ASSERT para gemma4 (non-causal attention)
        model_lower = self.model.lower()
        if any(m in model_lower for m in _MODELS_REQUIRING_CPU_WORKAROUND):
            payload["options"] = {"num_gpu": 0}
            logger.info(f"[VISION_CLIENT] Forcing CPU mode (num_gpu: 0) for {self.model}")

        url = f"{self.host}/api/generate"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
                response_text = data.get("response", "").strip()

                logger.debug(
                    f"[VISION_CLIENT] model={self.model} "
                    f"prompt_len={len(prompt)} "
                    f"image_len={len(image_b64)} "
                    f"has_system={bool(system)} "
                    f"response_len={len(response_text)}"
                )

                return response_text

        except httpx.TimeoutException:
            raise TimeoutError(
                f"VisionModel timeout después de {self.timeout}s — "
                f"Ejecuta: ollama pull {self.model}"
            )
        except httpx.HTTPStatusError as e:
            err_detail = e.response.text[:200]
            raise RuntimeError(f"Ollama API error {e.response.status_code}: {err_detail}")
        except Exception as e:
            raise RuntimeError(f"VisionClient error: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════
# VISION AGENT
# ═══════════════════════════════════════════════════════════════
class VisionAgent:
    """
    Agente de visión — VLM configurado en settings.llm_vision_model.

    ✅ v1.6.0: Sin fallbacks, sin lógica condicional compleja.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        # Resolución de configuración simple
        # ✅ FIX: el fallback ya no apunta a gemma4:e4b (desinstalado) —
        # settings.llm_vision_model es un campo real, este default solo
        # aplica si esa lectura fallara por completo.
        self.model   = model   or getattr(settings, "llm_vision_model", settings.llm_default_model)
        self.host    = host    or getattr(settings, "ollama_host", "http://127.0.0.1:11434")
        self.timeout = timeout or getattr(settings, "ollama_timeout_vision", 120)
        self._client = _OllamaVisionClient(self.host, self.model, self.timeout)

        logger.info(
            f"VisionAgent inicializado: model={self.model} timeout={self.timeout}s"
        )

        dl = _dlog()
        if dl:
            dl.log_event("VISION_AGENT", "VisionAgent inicializado", level="INFO", extra={
                "model": self.model,
                "host": self.host,
                "timeout": self.timeout,
            })

    def execute(self, input_msg: Message, **kwargs: Any) -> AgentResult:
        """
        Punto de entrada síncrono.
        """
        t0 = time.time()
        dl = _dlog()

        image_b64     = kwargs.get("image_b64", "")
        context_hint  = kwargs.get("context_hint", "generic")
        active_window = kwargs.get("active_window", "")

        if not image_b64:
            if dl:
                dl.log_agent_execution(
                    agent_name="VisionAgent",
                    task_type="screenshot_analysis",
                    state="failed",
                    success=False,
                    error="image_b64 vacío",
                )
            return AgentResult(
                content="No se proporcionó imagen para analizar.",
                success=False,
                error="image_b64 vacío",
                confidence=0.0,
                agent_type="vision",
                model_used=self.model,
                task_type="screenshot_analysis",
                latency_ms=round((time.time() - t0) * 1000, 2),
                metadata={"agent": "vision_agent"},
            )

        user_query = input_msg.content or ""

        if dl:
            dl.log_agent_execution(
                agent_name="VisionAgent",
                task_type="screenshot_analysis",
                state="started",
                success=True,
            )

        # ── Configuración de Resize ─────────────────────────────
        # Usamos settings para max width, fallback a 800
        max_w = getattr(settings, 'vision_capture_max_width', 800)
        processed_image = _resize_image_b64(image_b64, max_dim=max_w, quality=82)

        prompt, system = _build_prompt(user_query, context_hint, active_window)

        try:
            text = self._client.generate(
                prompt=prompt,
                image_b64=processed_image,
                system=system,
            )
            duration = time.time() - t0

            if not text or len(text.strip()) == 0:
                empty_error = RuntimeError(
                    f"Modelo {self.model} retornó respuesta vacía. "
                    f"Prompt len={len(prompt)}, Image len={len(processed_image)}."
                )
                logger.error(f"[VISION] Empty response from {self.model}")
                raise empty_error

            # ✅ Respuesta válida
            if dl:
                dl.log_model_call(
                    model_name=self.model,
                    action="generate",
                    task_type="screenshot_analysis",
                    input_size=len(prompt),
                    output_size=len(text),
                    duration=duration,
                    success=True,
                )
                dl.log_agent_execution(
                    agent_name="VisionAgent",
                    task_type="screenshot_analysis",
                    state="completed",
                    duration=duration,
                    success=True,
                )

            return AgentResult(
                content=text,
                success=True,
                error=None,
                confidence=0.85,
                agent_type="vision",
                model_used=self.model,
                task_type="screenshot_analysis",
                latency_ms=round(duration * 1000, 2),
                metadata={
                    "agent":          "vision_agent",
                    "model":          self.model,
                    "context_hint":   context_hint,
                    "active_window":  active_window,
                    "duration_s":     round(duration, 2),
                    "resize_dim":     max_w,
                },
            )

        except Exception as e:
            duration = time.time() - t0
            logger.error(f"VisionAgent ERROR: {e}")

            if dl:
                dl.log_error(
                    error=e,
                    context=f"VisionAgent.execute",
                    function_name="VisionAgent.execute",
                    extra_info={
                        "model": self.model,
                        "context_hint": context_hint,
                        "image_b64_len": len(image_b64),
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
                dl.log_agent_execution(
                    agent_name="VisionAgent",
                    task_type="screenshot_analysis",
                    state="failed",
                    duration=duration,
                    success=False,
                    error=str(e),
                )

            return AgentResult(
                content=f"Error al analizar la imagen: {e}",
                success=False,
                error=str(e),
                confidence=0.0,
                agent_type="vision",
                model_used=self.model,
                task_type="screenshot_analysis",
                latency_ms=round(duration * 1000, 2),
                metadata={
                    "agent":     "vision_agent",
                    "model":     self.model,
                    "duration_s": round(duration, 2),
                },
            )

    async def execute_async(self, input_msg: Message, **kwargs: Any) -> AgentResult:
        return await asyncio.to_thread(self.execute, input_msg, **kwargs)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "model":       self.model,
            "host":        self.host,
            "timeout":     self.timeout,
        }


# ═══════════════════════════════════════════════════════════════
# Canal genérico (v0.6.9p): imágenes sueltas del chat — prompt NEUTRO,
# sin el sesgo de screenshot del VisionAgent (ventanas/botones/íconos)
# que hacía que ante un texto el VLM respondiera "Aplicación o ventana
# activa..." en vez de transcribirlo.
# ═══════════════════════════════════════════════════════════════
_GENERIC_SYSTEM = (
    "Sos un asistente de visión local que analiza imágenes, fotos, "
    "documentos o capturas sueltas que el usuario adjunta en el chat. "
    "La imagen NO es una captura de una aplicación que haya que auditar. "
    "Respondés en español, conciso y SOLO sobre lo que se ve. "
    "Si te piden copiar texto, transcribilo EXACTO y sin comentarios "
    "ni listas numeradas, salvo que se pida otro formato."
)


def describe_generic_image(
    image_b64: str,
    instruction: str = "",
    model: Optional[str] = None,
    host: Optional[str] = None,
    timeout: Optional[int] = None,
    max_dim: int = 800,
    num_predict: Optional[int] = None,
) -> str:
    """Describe/analiza una imagen suelta (adjunto del chat) con prompt neutro.

    Args:
        image_b64: Imagen en base64.
        instruction: Pregunta del usuario (vacío → descripción general).
        model/host/timeout: override de config (defaults desde settings).
        max_dim: Tamaño máximo de re-escala (default 800 px).
        num_predict: Tope de tokens de salida (default settings.vision_num_predict).

    Returns:
        str — texto del VLM (recortado).

    Raises:
        RuntimeError: si no hay imagen o el VLM responde vacío.
    """
    if not image_b64:
        raise RuntimeError("no hay imagen (image_b64 vacío)")
    model = model or getattr(settings, "llm_vision_model",
                             settings.llm_default_model)
    host = host or getattr(settings, "ollama_host", "http://127.0.0.1:11434")
    timeout = timeout or getattr(settings, "ollama_timeout_vision", 120)
    if num_predict is None:
        num_predict = getattr(settings, "vision_num_predict", 512)

    client = _OllamaVisionClient(host, model, timeout)
    processed = _resize_image_b64(image_b64, max_dim=max_dim, quality=82)
    prompt = (instruction or "").strip() or "Describí la imagen con detalle."
    text = client.generate(prompt=prompt, image_b64=processed,
                           system=_GENERIC_SYSTEM, num_predict=num_predict)
    if not text or not text.strip():
        raise RuntimeError(f"el VLM {model} respondió vacío")
    return text.strip()


def _log(msg: str, level: str = "info") -> None:
    dl = _dlog()
    if dl:
        dl.log_event("VISION_AGENT", msg, level.upper())
    else:
        getattr(logger, level, logger.info)(msg)
