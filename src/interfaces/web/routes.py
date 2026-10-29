#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Rutas de la API Web (CORE - REFACTORIZADO)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/routes.py
# Autor: SaintWick
# Fecha: 2026-03-28
# Propósito: Endpoints Core API para NiceGUI - Chat, Upload, Analyze, Health
# ═══════════════════════════════════════════════════════════════
# ✅ REFACTORIZADO: Separación de responsabilidades 50/50 con routes_extended.py
# ✅ ELIMINADO (v0.6.8s): Endpoints Dashboard (/metrics, /health/detailed, /metrics/agents) retirados
# ✅ CORREGIDO: Eliminando duplicados de endpoints administrativos
# ✅ CORREGIDO: log._initialized → log.is_initialized (consistencia)
# ✅ COMPLETO: ~750 líneas — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ✅ NUEVO: Endpoint /rac para ejecutar Revisión Axioma Completa desde la web
# ✅ NUEVO: Extensión .py y .md permitidas en /upload_file
# ✅ MEJORA: Validación de resultado en /rac y limpieza de agente proxy
# ═══════════════════════════════════════════════════════════════
import logging
import asyncio
import os
import time
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
from config.paths import Paths
# ✅ 2026-10-25 (fuente única de la versión): /health devolvía "0.1.0" fijo
# mientras el proyecto iba por otra versión. Se lee de `src/__init__.py`, igual
# que `/status` (routes_extended) y `FastAPI(version=...)` (server).
from src import __version__ as AXIOMA_VERSION
from src.domain.entities import Message
from src.domain.exceptions import AxiomaError, IntegrationError
from src.router.smart_router import SmartRouter
# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_web_event(category: str, message: str, extra: dict = None):
    """
    Helper para logging de eventos web.
    Args:
        category: Categoría del evento (API, SESSION, UI, etc.)
        message: Mensaje descriptivo
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level="INFO", extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 routes.py:62] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra: dict = None):
    """
    Helper para logging de errores.
    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 routes.py:80] excepción degradada (intencional): {_d2e}")

# ═══════════════════════════════════════════════════════════════
# ROUTER PRINCIPAL
# ═══════════════════════════════════════════════════════════════
# ✅ 2026-10-08: el prefijo era `/api/v1` y este router se MONTA bajo `/api` (server.py:
# `nicegui_app.mount('/api', _fastapi_app)`), así que la dirección pública quedaba DUPLICADA:
# `/api/api/v1/...`. MEDIDO contra el servidor andando: `/api/v1/chat` daba 404 y `/api/api/v1/chat` 200
# —o sea que la ruta que documenta el README y la que usa todo el mundo estaba rota—. Con `/v1` acá, la
# dirección pública es la que se quería: **`/api/v1/...`**.
router = APIRouter(prefix="/v1", tags=["AXIOMA"])

# ═══════════════════════════════════════════════════════════════
# MODELOS DE REQUEST/RESPONSE (Compartidos con routes_extended)
# ═══════════════════════════════════════════════════════════════
class ChatRequest(BaseModel):
    """Request para endpoint de chat."""
    message: str = Field(..., min_length=1, max_length=10000)
    session_id: Optional[str] = None
    model: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    files: Optional[List[str]] = None
    intent: Optional[str] = None

class ChatResponse(BaseModel):
    """Response para endpoint de chat."""
    success: bool
    content: str
    response: str  # Alias para compatibilidad frontend
    model_used: str
    model: str  # Alias para compatibilidad frontend
    latency_ms: float
    session_id: Optional[str] = None
    error: Optional[str] = None
    source: Optional[str] = None  # "api" o "llm"
    task_type: Optional[str] = None  # Tipo de tarea
    api_required: Optional[bool] = None  # Si usó API externa

class FileUploadResponse(BaseModel):
    """Response para upload de archivos."""
    success: bool
    file_id: str
    file_name: str
    file_type: str
    file_size: int
    file_path: str
    message: str
    error: Optional[str] = None

class FileAnalysisRequest(BaseModel):
    """Request para análisis de archivos."""
    file_id: str
    analysis_type: Optional[str] = "auto"

class FileAnalysisResponse(BaseModel):
    """Response para análisis de archivos."""
    success: bool
    file_id: str
    analysis: str
    file_type: str
    latency_ms: float
    error: Optional[str] = None

class BackupRequest(BaseModel):
    """Request para backup."""
    encrypt: bool = False

class BackupResponse(BaseModel):
    """Response para backup."""
    success: bool
    file_path: str
    size_bytes: int

class ModelCheckResponse(BaseModel):
    """Respuesta del endpoint de verificación de modelos."""
    timestamp: str
    ollama_available: bool
    models: Dict[str, Dict[str, Any]]
    fallback_chain: List[str]
    warnings: List[str]
    checked_at: Optional[float] = None

class APIStatusResponse(BaseModel):
    """Response para estado de APIs externas."""
    timestamp: str
    apis: Dict[str, bool]
    all_configured: bool
    message: str

class RACRequest(BaseModel):
    """Request para Revisión Axioma Completa."""
    scope: str = "completa"

# ═══════════════════════════════════════════════════════════════
# SINGLETON: SmartRouter (reutilizable entre requests)
# ═══════════════════════════════════════════════════════════════
_router_instance: Optional[SmartRouter] = None

def get_router() -> SmartRouter:
    """
    Obtiene instancia singleton de SmartRouter.
    Logging:
    - Registra creación del router singleton
    - Timing de inicialización
    """
    global _router_instance
    if _router_instance is None:
        start = time.time()
        _log_web_event("ROUTER", "Creating SmartRouter singleton")
        _router_instance = SmartRouter(
            cache_ttl=300,
            cache_max_size=1000,
            classifier_threshold=0.7,
            enable_cache=True,
            max_tokens=512,
        )
        duration = time.time() - start
        _log_web_event("ROUTER", f"SmartRouter singleton created in {duration:.4f}s",
                       extra={"duration": round(duration, 4)})
        logger.info(f"SmartRouter singleton initialized in {duration:.4f}s")
    return _router_instance

async def cleanup_router():
    """
    Limpia recursos del router al shutdown.
    Logging:
    - Registra cleanup del router
    """
    global _router_instance
    if _router_instance and hasattr(_router_instance, 'cleanup'):
        _log_web_event("ROUTER", "Cleaning up SmartRouter singleton")
        await _router_instance.cleanup()
        _router_instance = None

# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /health (Health Check Básico)
# ═══════════════════════════════════════════════════════════════
@router.get("/health")
async def health_endpoint() -> Dict[str, Any]:
    """
    Verifica estado básico de la API.
    Logging:
    - Registra health check
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/health - Health check")
    duration = time.time() - start_time
    _log_web_event("API", f"GET /api/v1/health - SUCCESS - {duration:.2f}ms")
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": AXIOMA_VERSION,
    }

# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /chat (ACTUALIZADO para NiceGUI)
# ═══════════════════════════════════════════════════════════════
@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    """
    Procesa mensaje de chat y retorna respuesta.
    ✅ CORREGIDO: Compatible con NiceGUI frontend
    ✅ CORREGIDO: Soporte para contexto y archivos adjuntos
    Logging:
    - Registra inicio/fin del endpoint
    - Timing completo de la operación
    - Errores con traceback completo
    - Session ID tracking
    - Task type y source de respuesta
    """
    start_time = time.time()
    _log_web_event("API", f"POST /api/v1/chat - session={request.session_id}",
                   extra={"input_length": len(request.message), "model": request.model})
    try:
        # Obtener router singleton
        router_instance = get_router()
        # Construir contexto con archivos y metadata
        context = request.context or {}
        if request.files:
            context["attached_files"] = request.files
        if request.intent:
            context["detected_intent"] = request.intent
        context["session_id"] = request.session_id
        context["model_preference"] = request.model
        # ✅ NUEVO: Usar route_and_execute para soporte de APIs
        result = await router_instance.route_and_execute(
            query=request.message,
            system_prompt=None,
            context=context,
        )
        # Determinar fuente de respuesta
        source = result.get("source", "llm")
        task_type = result.get("task_type", "general")
        api_required = result.get("api_required", False)
        # Build response
        latency_ms = result.get("latency_ms", (time.time() - start_time) * 1000)
        _log_web_event("API", f"POST /api/v1/chat - SUCCESS - {latency_ms:.2f}ms",
                       extra={"output_length": len(result.get("content", "")),
                              "model": result.get("model", "unknown"),
                              "source": source,
                              "task_type": task_type,
                              "api_required": api_required})
        return ChatResponse(
            success=result.get("success", False),
            content=result.get("content", ""),
            response=result.get("content", ""),  # Compatibilidad frontend
            model_used=result.get("model", "unknown"),
            model=result.get("model", "unknown"),  # Compatibilidad frontend
            latency_ms=latency_ms,
            session_id=request.session_id,
            error=result.get("error"),
            source=source,  # ✅ NUEVO
            task_type=task_type,  # ✅ NUEVO
            api_required=api_required,  # ✅ NUEVO
        )
    except IntegrationError as e:
        duration = time.time() - start_time
        _log_error(e, "routes.chat_endpoint", extra={"session_id": request.session_id, "duration": duration})
        _log_web_event("API", f"POST /api/v1/chat - INTEGRATION_ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Integration error in chat: {e}")
        return ChatResponse(
            success=False,
            content="",
            response="",
            model_used="none",
            model="none",
            latency_ms=duration * 1000,
            error=f"Integration: {str(e)}",
            source="error",
        )
    except AxiomaError as e:
        duration = time.time() - start_time
        _log_error(e, "routes.chat_endpoint", extra={"session_id": request.session_id, "duration": duration})
        _log_web_event("API", f"POST /api/v1/chat - AXIOMA_ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Axioma error in chat: {e}")
        return ChatResponse(
            success=False,
            content="",
            response="",
            model_used="none",
            model="none",
            latency_ms=duration * 1000,
            error=str(e),
            source="error",
        )
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.chat_endpoint", extra={"session_id": request.session_id, "duration": duration})
        _log_web_event("API", f"POST /api/v1/chat - ERROR - {duration:.2f}ms",
                       extra={"error": str(e), "type": type(e).__name__})
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        return ChatResponse(
            success=False,
            content="",
            response="",
            model_used="none",
            model="none",
            latency_ms=duration * 1000,
            error=str(e),
            source="error",
        )

# ═══════════════════════════════════════════════════════════════
# ✅ 2026-10-28 (AUDITORÍA, MEDIDO): `/v1/upload_file` aceptó un archivo de **70 MB** sin
# autenticación en modo expuesto (200, success:true): se podía llenar disco y memoria. Ahora hay
# tope (25 MB por defecto, `AXIOMA_UPLOAD_MAX_MB`) y se escribe por trozos, sin cargar todo en RAM.
MAX_UPLOAD_BYTES = int(os.environ.get("AXIOMA_UPLOAD_MAX_MB", "25")) * 1024 * 1024


# ENDPOINT: /upload_file (File Upload Handler)
# ═══════════════════════════════════════════════════════════════
async def _guardar_con_tope(archivo: UploadFile, destino: Path) -> tuple[int, bool]:
    """Escribe el upload por trozos y corta si supera `MAX_UPLOAD_BYTES`.

    Returns:
        (bytes_escritos, excedido). Si excedió, borra el parcial y devuelve (0, True).
    ✅ 2026-10-28 (AUDITORÍA): antes `await file.read()` cargaba todo en RAM y aceptó 70 MB.
    """
    escrito = 0
    with open(destino, "wb") as f:
        while True:
            trozo = await archivo.read(1024 * 1024)
            if not trozo:
                break
            escrito += len(trozo)
            if escrito > MAX_UPLOAD_BYTES:
                f.close()
                destino.unlink(missing_ok=True)
                return 0, True
            f.write(trozo)
    return escrito, False


_EXTENSIONES_SUBIDA = {".jsonl", ".pdf", ".log", ".toon", ".txt", ".json", ".csv", ".py", ".md"}


def _error_subida(nombre: str, ext: str, mensaje: str) -> FileUploadResponse:
    """Respuesta de fallo uniforme para /upload_file."""
    return FileUploadResponse(success=False, file_id="", file_name=nombre or "unknown",
                              file_type=ext, file_size=0, file_path="", message="",
                              error=mensaje)


def _validar_subida(file) -> tuple[str, str, "FileUploadResponse | None"]:
    """Valida extensión y basename; devuelve (ext, nombre_seguro, error_o_None)."""
    nombre = (file.filename or "") if file else ""
    ext = Path(nombre).suffix.lower()
    if ext not in _EXTENSIONES_SUBIDA:
        permitidas = ", ".join(sorted(_EXTENSIONES_SUBIDA))
        return ext, "", _error_subida(nombre, ext, f"Extensión no soportada: {ext}. Permitidas: {permitidas}")
    seguro = Path(nombre.replace("\\", "/")).name.strip()
    if not seguro or seguro in {".", ".."}:
        return ext, "", _error_subida(nombre, ext, f"Nombre de archivo inválido: {nombre!r}")
    return ext, seguro, None


@router.post("/upload_file", response_model=FileUploadResponse)
async def upload_file_endpoint(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
) -> FileUploadResponse:
    """Sube un archivo para análisis (extensiones y tope controlados)."""
    start_time = time.time()
    _log_web_event("API", f"POST /api/v1/upload_file - session={session_id}",
                   extra={"filename": file.filename if file else "unknown"})
    try:
        file_ext, safe_name, error = _validar_subida(file)
        if error is not None:
            _log_web_event("API", "POST /api/v1/upload_file - INVALIDO",
                           extra={"filename": file.filename if file else "unknown"})
            return error
        upload_dir = Paths.DATA / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_id = f"{timestamp}_{safe_name.replace(' ', '_')}"
        file_path = upload_dir / file_id
        if not file_path.resolve().is_relative_to(upload_dir.resolve()):
            raise ValueError(f"file_id inválido: {file_id!r} — debe quedar dentro de {upload_dir}")
        file_size, excedido = await _guardar_con_tope(file, file_path)
        if excedido:
            _log_web_event("API", "POST /api/v1/upload_file - TOO_LARGE",
                           extra={"limite_bytes": MAX_UPLOAD_BYTES})
            return _error_subida(safe_name, file_ext,
                                 f"El archivo supera el máximo permitido de "
                                 f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        duration = time.time() - start_time
        _log_web_event("API", f"POST /api/v1/upload_file - SUCCESS - {duration:.2f}ms",
                       extra={"file_id": file_id, "file_size": file_size, "file_type": file_ext})
        logger.info(f"File uploaded: {file_id} ({file_size} bytes, {file_ext})")
        return FileUploadResponse(success=True, file_id=file_id, file_name=safe_name,
                                  file_type=file_ext, file_size=file_size,
                                  file_path=str(file_path),
                                  message=f"Archivo subido ({file_size} bytes)")
    except Exception as e:
        _log_error(e, "routes.upload_file_endpoint", extra={"session_id": session_id})
        logger.error(f"Upload error: {e}", exc_info=True)
        return _error_subida(file.filename if file else "", "", f"Upload failed: {e}")

# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /analyze_file (File Analysis Handler)
# ═══════════════════════════════════════════════════════════════
@router.post("/analyze_file", response_model=FileAnalysisResponse)
async def analyze_file_endpoint(request: FileAnalysisRequest) -> FileAnalysisResponse:
    """
    Analiza archivo previamente subido.
    ✅ CORREGIDO: Compatible con NiceGUI frontend
    Soporta: .jsonl, .pdf, .log, .toon, .txt, .json, .csv
    Logging:
    - Registra inicio/fin del análisis
    - Tipo de archivo y análisis
    - Errores con traceback
    """
    start_time = time.time()
    _log_web_event("API", f"POST /api/v1/analyze_file - file_id={request.file_id}",
                   extra={"analysis_type": request.analysis_type})
    # ✅ FIX AUD-PTH-008 / CWE-22 (2026-10-26): el `file_id` lo manda el
    # cliente y se concatenaba a `uploads/` sin validar. Con
    # `file_id="/etc/passwd"`, `Path(base) / "/etc/passwd"` DESCARTA la base,
    # `extract_file_text` leía el archivo y su contenido terminaba en el LLM
    # (reproducido). Mismo patrón que `/upload_file` y WorkspaceManager: sólo
    # un NOMBRE (sin separadores ni "..") + contención real tras resolver
    # symlinks. Se valida ANTES del try para que el 400 no lo trague el except.
    file_id = request.file_id or ""
    safe_id = Path(file_id.replace("\\", "/")).name
    upload_dir = Paths.DATA / "uploads"
    if not file_id or safe_id != file_id or safe_id in {".", ".."}:
        _log_web_event("API", "POST /api/v1/analyze_file - INVALID_FILE_ID",
                       extra={"file_id": file_id})
        raise HTTPException(
            status_code=400,
            detail=(f"file_id inválido: {request.file_id!r} — tiene que ser un "
                    f"nombre de archivo dentro de {upload_dir}"),
        )
    file_path = upload_dir / safe_id
    if not file_path.resolve().is_relative_to(upload_dir.resolve()):
        _log_web_event("API", "POST /api/v1/analyze_file - INVALID_FILE_ID",
                       extra={"file_id": file_id})
        raise HTTPException(
            status_code=400,
            detail=(f"file_id inválido: {request.file_id!r} — sale de {upload_dir}"),
        )
    try:
        # Validar que el archivo existe
        if not file_path.exists():
            _log_web_event("API", "POST /api/v1/analyze_file - FILE_NOT_FOUND",
                           extra={"file_id": request.file_id})
            return FileAnalysisResponse(
                success=False,
                file_id=request.file_id,
                analysis="",
                file_type="",
                latency_ms=0,
                error=f"Archivo no encontrado: {request.file_id}",
            )
        # ✅ 2026-09-01: extracción de TEXTO REAL (PDF/DOCX parseados) en vez
        # de leer bytes crudos — antes un PDF binario crasheaba o inyectaba
        # basura al LLM. Límite subido de 5000 a 150000 chars.
        # ✅ 2026-10-29 (Qwen/auditoría): la extracción (PDF/DOCX) y el LLM son SYNC y estaban
        # dentro de un `async def`: bloqueaban el event loop de NiceGUI/FastAPI (una subida grande
        # congelaba la web entera). Van a un hilo.
        from src.utils.file_text import extract_file_text
        content = await asyncio.to_thread(extract_file_text, file_path, 150_000)
        # Determinar tipo de análisis
        file_ext = Path(file_path).suffix.lower()
        analysis_type = request.analysis_type
        # ✅ NUEVO: Usar AnalystAgent para análisis
        from src.agents.analyst import AnalystAgent
        analyst = AnalystAgent()
        message = Message(content=f"Analiza este archivo {file_ext}:\n{content}", role="user")
        result = await asyncio.to_thread(analyst.execute, message, analysis_type=analysis_type)
        duration = time.time() - start_time
        _log_web_event("API", f"POST /api/v1/analyze_file - SUCCESS - {duration:.2f}ms",
                       extra={"file_id": request.file_id, "analysis_type": analysis_type})
        return FileAnalysisResponse(
            success=result.success,
            file_id=request.file_id,
            analysis=result.content,
            file_type=file_ext,
            latency_ms=duration * 1000,
            error=result.error,
        )
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.analyze_file_endpoint", extra={"file_id": request.file_id, "duration": duration})
        _log_web_event("API", f"POST /api/v1/analyze_file - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Analyze file error: {e}", exc_info=True)
        return FileAnalysisResponse(
            success=False,
            file_id=request.file_id,
            analysis="",
            file_type="",
            latency_ms=duration * 1000,
            error=f"Analysis failed: {str(e)}",
        )

# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /rac (Revisión Axioma Completa)
# ═══════════════════════════════════════════════════════════════
@router.post("/rac")
async def rac_endpoint(request: RACRequest) -> Dict[str, Any]:
    """
    Ejecuta Revisión Axioma Completa (RAC) o Parcial.
    Genera un documento Markdown en la carpeta docs/ con errores, mejoras y optimizaciones.
    ✅ MEJORA: Valida resultado interno y evita configuración redundante en agente proxy.
    """
    start_time = time.time()
    _log_web_event("API", f"POST /api/v1/rac - scope={request.scope}")
    try:
        from src.agents.analyst import AnalystAgent

        # El agente instanciado aquí es solo un proxy para acceder a review_system().
        # review_system() internamente crea un agente dedicado (validation=False, max_tokens=800).
        # Por lo tanto, no configuramos temperature ni max_tokens aquí para evitar confusión.
        analyst = AnalystAgent(enable_memory=False)

        # Ejecutar en thread aparte para no bloquear el event loop asíncrono
        result = await asyncio.to_thread(analyst.review_system, request.scope)

        duration = time.time() - start_time

        # ✅ MEJORA: Verificar el flag success interno devuelto por review_system()
        is_success = result.get("success", False)

        if not is_success:
            _log_web_event("API", f"POST /api/v1/rac - COMPLETED_WITH_ERRORS - {duration:.2f}ms", extra={"result": result})
            # Retornamos 200 con success=False para que el frontend pueda manejar la lógica
            return {"success": False, "error": result.get("message", "RAC analysis failed internally"), **result}

        _log_web_event("API", f"POST /api/v1/rac - SUCCESS - {duration:.2f}ms")
        return {"success": True, **result}

    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.rac_endpoint", extra={"duration": duration})
        _log_web_event("API", f"POST /api/v1/rac - ERROR - {duration:.2f}ms", extra={"error": str(e)})
        logger.error(f"RAC endpoint error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

# 📋 RESUMEN DE ENDPOINTS — CORE v0.1.0 (COMPLETO)
# ═══════════════════════════════════════════════════════════════
# | Endpoint                    | Método | Propósito                    | Estado  |
# |-----------------------------|--------|------------------------------|---------|
# | /api/v1/health              | GET    | Health check básico          | ✅ Único|
# | /api/v1/chat                | POST   | Chat principal               | ✅ Único|
# | /api/v1/upload_file         | POST   | Upload de archivos           | ✅ Único|
# | /api/v1/analyze_file        | POST   | Análisis de archivos         | ✅ Único|
# | /api/v1/rac                 | POST   | Revisión Axioma Completa     | ✅ Único|
# # | /api/v1/health/detailed     | GET    | Health check detallado       | ✅ Único|
# | /api/v1/metrics/agents      | GET    | Métricas por agente          | ✅ Único|
# ═══════════════════════════════════════════════════════════════
# 🗑️ ENDPOINTS ELIMINADOS (DUPLICADOS → routes_extended.py):
# | Endpoint              | Razón de eliminación                      |
# |-----------------------|-------------------------------------------|
# | /api/status           | Duplicado → routes_extended.py (System)   |
# | /models/check         | Duplicado → routes_extended.py (System)   |
# | /backup               | Duplicado → routes_extended.py (System)   |
# | /shutdown             | Duplicado → routes_extended.py (System)   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ✅ Balance 50/50: routes.py (Core) + routes_extended.py (System)
# ✅ Sin importaciones circulares — router singleton definido aquí
# ✅ Logging consistente — helpers definidos aquí
# ═══════════════════════════════════════════════════════════════
