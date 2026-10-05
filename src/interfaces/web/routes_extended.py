#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 → v0.3.0 — Rutas de la API Web (SYSTEM)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/routes_extended.py
# Autor: SaintWick
# Fecha: 2026-03-28
# Propósito: Endpoints System API únicos - Admin, Sessions, Models, Stats, Workspace
#
# v0.1.0 — System endpoints: status, sessions, models, stats
# v0.3.0 — GET /models: dinámico desde settings (elimina hardcoding)
# v0.2.0 — FASE 8: /workspace/download — descarga segura de archivos
#           FASE 9: /workspace/attempts — historial de intentos con código
# ═══════════════════════════════════════════════════════════════
# ✅ REFACTORIZADO: Eliminados 7 endpoints duplicados con routes.py
# ✅ CORREGIDO: Importación consistente desde routes.py (router singleton, helpers, modelos)
# ✅ CORREGIDO: Distribución 50/50 de responsabilidades entre archivos
# ✅ COMPLETO: ~520 líneas — SIN OMISIONES — PROTOCOLO CUMPLIDO
import logging
import asyncio
import os
import signal
import time
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime
import json
from pathlib import Path
from config.settings import settings
from config.paths import Paths
# ✅ v0.6.9w (FIX VERSIONES): /status devolvía `"version": "0.2.0"` hardcodeada
# mientras el proyecto va por 0.6.9. Fuente única: `src/__init__.py`.
from src import __version__ as AXIOMA_VERSION
from src.domain.entities import Message, Response, MessageRole
from src.domain.exceptions import AxiomaError, IntegrationError
from src.router.smart_router import SmartRouter
# ✅ CORREGIDO: Importa router, helpers y modelos desde routes.py para evitar duplicación
from src.interfaces.web.routes import (
    router, _log_web_event, _log_error, LOGGER_AVAILABLE, logger,
    ChatRequest, ChatResponse, FileUploadResponse, FileAnalysisRequest,
    FileAnalysisResponse, BackupRequest, BackupResponse, ModelCheckResponse,
    APIStatusResponse, get_router, cleanup_router
)
from src.agents.analyst import AnalystAgent
from src.llm.client import OllamaClient

# ✅ PLAN v2.0 (Fase 2): TraceStore — import seguro (mismo patrón que
# dispatcher_operations.py); el endpoint /traces delega 100% en él.
try:
    from src.learning.trace_store import get_trace_store
except Exception:  # noqa: BLE001 — degradación silenciosa
    get_trace_store = None  # type: ignore[assignment]
# ============================================================================
# LOGGING — Integración con DetailedLogger (reexportado desde routes.py)
# ============================================================================
# Nota: Los helpers _log_web_event y _log_error ya están definidos en routes.py
# y se importan arriba para mantener consistencia en logging estructurado.
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /status (System Status) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.get("/status")
async def status_endpoint() -> Dict[str, Any]:
    """
    Retorna estado completo del sistema.

    Logging:
    - Registra verificación de estado
    - Stats del router
    - Timing de la operación

    Returns:
        Dict con estado, versión, modelo, timeout y router_stats
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/status")
    try:
        router_instance = get_router()
        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/status - SUCCESS - {duration:.2f}ms")
        return {
            "status": "running",
            "version": AXIOMA_VERSION,
            "model": getattr(settings, 'llm_default_model', ""),
            "timeout": settings.ollama_timeout if hasattr(settings, 'ollama_timeout') else 25,
            "router_stats": router_instance.get_stats(),
        }
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.status_endpoint", extra={"duration": duration})
        _log_web_event("API", f"GET /api/v1/status - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Status endpoint error: {e}", exc_info=True)
        return {
            "status": "error",
            "version": AXIOMA_VERSION,
            "error": str(e),
        }
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /sessions (List) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    """
    Lista sesiones activas del sistema.

    Logging:
    - Registra listado de sesiones

    Returns:
        Dict con lista de sesiones y total
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/sessions")
    try:
        # ✅ v0.6.9v (ISSUE-015): cableado al SessionManager real (singleton en
        # states.py) — antes devolvía siempre {"sessions": [], "total": 0}.
        # Import diferido para evitar ciclo routes_extended ↔ states.
        from src.interfaces.web.states import SessionManager
        manager = SessionManager()
        all_sessions = manager.get_all_sessions()
        sessions = list(all_sessions.values())
        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/sessions - SUCCESS - {duration:.2f}ms",
                       extra={"total": len(sessions)})
        return {
            "sessions": sessions,
            "total": len(sessions),
        }
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.list_sessions", extra={"duration": duration})
        _log_web_event("API", f"GET /api/v1/sessions - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"List sessions error: {e}", exc_info=True)
        return {
            "sessions": [],
            "total": 0,
            "error": str(e),
        }
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /sessions/{session_id} (GET) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> Dict[str, Any]:
    """
    Obtiene detalles de una sesión específica.

    Args:
        session_id: ID único de la sesión

    Logging:
    - Registra acceso a sesión
    - Session ID tracking

    Returns:
        Dict con session_id, messages y created_at
    """
    start_time = time.time()
    _log_web_event("API", f"GET /api/v1/sessions/{session_id}")
    try:
        # ✅ v0.6.9v (ISSUE-015): cableado al SessionManager real.
        from src.interfaces.web.states import SessionManager
        manager = SessionManager()
        session = manager.get_session(session_id)
        duration = time.time() - start_time
        if session is None:
            _log_web_event("API", f"GET /api/v1/sessions/{session_id} - NOT_FOUND - {duration:.2f}ms")
            return {
                "session_id": session_id,
                "messages": [],
                "created_at": None,
                "found": False,
                "error": "session not found",
            }
        info = session.to_dict()
        # Detalle de mensajes (el to_dict del estado solo trae el count).
        info["messages"] = [m.to_dict() for m in session.messages]
        info["found"] = True
        _log_web_event("API", f"GET /api/v1/sessions/{session_id} - SUCCESS - {duration:.2f}ms",
                       extra={"message_count": len(session.messages)})
        return info
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.get_session", extra={"session_id": session_id, "duration": duration})
        _log_web_event("API", f"GET /api/v1/sessions/{session_id} - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Get session error: {e}", exc_info=True)
        return {
            "session_id": session_id,
            "error": str(e),
        }
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /sessions/{session_id} (DELETE) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> Dict[str, Any]:
    """
    Elimina una sesión específica.

    Args:
        session_id: ID único de la sesión a eliminar

    Logging:
    - Registra eliminación de sesión
    - Session ID tracking

    Returns:
        Dict con success y message
    """
    start_time = time.time()
    _log_web_event("API", f"DELETE /api/v1/sessions/{session_id}")
    try:
        # ✅ v0.6.9v (ISSUE-015): cableado al SessionManager real.
        from src.interfaces.web.states import SessionManager
        manager = SessionManager()
        deleted = manager.delete_session(session_id)
        duration = time.time() - start_time
        if deleted:
            _log_web_event("API", f"DELETE /api/v1/sessions/{session_id} - SUCCESS - {duration:.2f}ms")
            return {
                "success": True,
                "message": f"Session {session_id} deleted",
                "session_id": session_id,
            }
        _log_web_event("API", f"DELETE /api/v1/sessions/{session_id} - NOT_FOUND - {duration:.2f}ms")
        return {
            "success": False,
            "message": f"Session {session_id} not found",
            "session_id": session_id,
        }
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.delete_session", extra={"session_id": session_id, "duration": duration})
        _log_web_event("API", f"DELETE /api/v1/sessions/{session_id} - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Delete session error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /models (List) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.get("/models")
async def list_models() -> Dict[str, Any]:
    """
    Lista modelos configurados en el sistema.

    Logging:
    - Registra listado de modelos
    - Modelos disponibles

    Returns:
        Dict con lista de modelos y fallback_chain
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/models")
    try:
        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/models - SUCCESS - {duration:.2f}ms")
        return {
            "models": [
                entry
                for attr, model_type in [
                    ("llm_default_model", "default"),
                    ("llm_code_model",    "code"),
                    ("llm_vision_model",  "vision"),
                    ("llm_embedding_model", "embedding"),
                    ("llm_fallback_1",    "fallback"),
                ]
                for name in [getattr(settings, attr, None)]
                if name
                for entry in [{"name": name, "type": model_type, "status": "configured"}]
            ],
            "fallback_chain": getattr(settings, "llm_fallback_chain", []),
        }
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.list_models", extra={"duration": duration})
        _log_web_event("API", f"GET /api/v1/models - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"List models error: {e}", exc_info=True)
        return {
            "models": [],
            "fallback_chain": [],
            "error": str(e),
        }
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /stats (System Stats) — ÚNICO
# ═══════════════════════════════════════════════════════════════
@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """
    Retorna estadísticas del sistema.

    ✅ v0.6.9v (paso 5 del plan): antes era un STUB —`total_requests`,
    `avg_latency_ms` y `uptime_seconds` salían **hardcodeados en 0** mientras
    `router_metrics` era real (un stub con apariencia de dato, la misma clase de
    problema que ISSUE-015). Ahora:
      · requests/latencia/uptime  → middleware real (`request_stats`)
      · `storage`                 → tamaño REAL de SQLite/feedback/workspaces
                                    (el informe pedía observabilidad de
                                    crecimiento de SQLite/LanceDB/sesiones)
      · `degradation`             → errores de contrato vs degradación esperada
      · `feedback`                → registros y señal ROUTED (P1 del informe)
    Se conservan las 4 claves históricas para no romper consumidores.

    Logging:
    - Registra acceso a estadísticas
    - Router metrics
    - Timing de la operación

    Returns:
        Dict con total_requests, avg_latency_ms, uptime_seconds, router_metrics
        (+ error_rate, by_status, top_paths, storage, degradation, feedback)
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/stats")
    try:
        router_instance = get_router()
        from .request_stats import stats as _req_stats
        req = _req_stats()

        payload: Dict[str, Any] = {
            # ── claves históricas (ahora con datos reales) ──
            "total_requests": req["total_requests"],
            "avg_latency_ms": req["avg_latency_ms"],
            "uptime_seconds": req["uptime_seconds"],
            "router_metrics": router_instance.get_metrics(),
            # ── detalle nuevo ──
            "error_requests": req["error_requests"],
            "error_rate": req["error_rate"],
            "max_latency_ms": req["max_latency_ms"],
            "slowest_path": req["slowest_path"],
            "by_status": req["by_status"],
            "top_paths": req["top_paths"],
            "first_request": req["first_request"],
            "last_request": req["last_request"],
            "storage": _storage_stats(),
            "degradation": _degradation_stats_safe(),
            "feedback": await _feedback_stats_safe(),
            "generated_at": req["generated_at"],
        }
        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/stats - SUCCESS - {duration:.2f}ms",
                       extra={"total_requests": req["total_requests"]})
        payload["duration_ms"] = round(duration * 1000, 2)
        return payload
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.get_stats", extra={"duration": duration})
        _log_web_event("API", f"GET /api/v1/stats - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        logger.error(f"Get stats error: {e}", exc_info=True)
        return {
            "total_requests": 0,
            "avg_latency_ms": 0,
            "uptime_seconds": 0,
            "router_metrics": {},
            "error": str(e),
        }


def _storage_stats() -> Dict[str, Any]:
    """Tamaño real de los almacenes locales (SQLite, feedback, workspaces)."""
    def _mb(path: Path) -> Optional[float]:
        try:
            return round(path.stat().st_size / (1024 * 1024), 3) if path.is_file() else None
        except OSError:
            return None

    def _dir_mb(path: Path) -> Optional[float]:
        try:
            if not path.is_dir():
                return None
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return round(total / (1024 * 1024), 3)
        except OSError:
            return None

    data_dir = Paths.DATA
    out: Dict[str, Any] = {
        "sqlite_db_mb": _mb(data_dir / "axioma.db"),
        "feedback_dir_mb": _dir_mb(data_dir / "feedback"),
        "workspaces_mb": _dir_mb(Path.home() / ".axioma" / "workspaces"),
        "logs_dir_mb": _dir_mb(Paths.LOGS),
        "data_dir": str(data_dir),
    }
    return out


def _degradation_stats_safe() -> Dict[str, Any]:
    """Métricas de degradación (nunca propaga error al endpoint)."""
    try:
        from src.utils.degradation import degradation_stats
        return degradation_stats()
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes._degradation_stats_safe")
        return {"available": False, "error": str(exc)}


async def _feedback_stats_safe() -> Dict[str, Any]:
    """Resumen de feedback (ROUTED incluido); degrada si el loop no está."""
    try:
        from src.core.feedback_loop import get_feedback_loop
        loop = await get_feedback_loop()
        if loop is None:
            return {"available": False}
        signals = await loop.get_signal_breakdown()
        return {
            "available": True,
            "total_records": signals.get("registros_totales", 0),
            "implicit": signals.get("total_implicitos", 0),
            "routed": signals.get("routed", 0),
        }
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes._feedback_stats_safe")
        return {"available": False, "error": str(exc)}
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /traces (PLAN v2.0 Fase 2) — Traces del dispatcher
# Mismo patrón que /stats: sin auth (coherente con el resto del archivo),
# error como dict inline, _log_web_event + _log_error. Delega 100% al
# TraceStore singleton que usa el CLI /traces.
# ═══════════════════════════════════════════════════════════════
@router.get("/traces")
async def get_traces(
    n: int = 50,
    task_type: Optional[str] = None,
    handler: Optional[str] = None,
    mode: Optional[str] = None,  # "stats" → latency_stats + success_rate
) -> Dict[str, Any]:
    """
    Traces del dispatcher: recent / by_task_type / by_handler / stats.

    Args:
        n: Cantidad de trazas (default 50)
        task_type: Filtrar por tipo de tarea
        handler: Filtrar por nombre de handler
        mode: "stats" → latency_stats + success_rate

    Returns:
        Dict con available, traces o latency/success_rate.
    """
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/traces")
    try:
        store = get_trace_store()
        if store is None:
            return {"available": False, "traces": []}
        if mode == "stats":
            result = {
                "available": True,
                "latency": store.latency_stats(task_type),
                "success_rate": store.success_rate(task_type),
            }
        elif task_type:
            result = {"available": True, "traces": store.by_task_type(task_type, n)}
        elif handler:
            result = {"available": True, "traces": store.by_handler(handler, n)}
        else:
            result = {"available": True, "traces": store.recent(n)}
        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/traces - SUCCESS - {duration:.2f}ms")
        return result
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.get_traces", extra={"duration": duration})
        _log_web_event("API", f"GET /api/v1/traces - ERROR - {duration:.2f}ms",
                       extra={"error": str(e)})
        return {"available": False, "traces": [], "error": str(e)}
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /workspace/download (FASE 8) — Descarga segura de archivos
# ═══════════════════════════════════════════════════════════════
@router.get("/workspace/download")
async def download_workspace_file(session_id: str, filename: str):
    """
    Descarga un archivo del workspace de una sesión.
    Solo permite archivos generados por el usuario (no internos del sistema).

    Seguridad:
    - Path traversal: archivo debe estar DENTRO del workspace
    - Archivos internos bloqueados: script.py, install_log.jsonl, attempt_*
    - Verificación de existencia y tipo archivo

    Args:
        session_id: ID de la sesión
        filename: Nombre del archivo a descargar

    Returns:
        FileResponse con el archivo, o JSONResponse con error
    """
    start_time = time.time()
    _log_web_event("API", f"GET /api/v1/workspace/download session={session_id} file={filename}")
    try:
        from src.core.workspace_manager import get_workspace_manager
        wm = get_workspace_manager()
        workspace = wm.get_workspace(session_id)
        file_path = os.path.join(workspace, filename)

        # Seguridad 1: path traversal — el archivo debe estar DENTRO del workspace
        real_path = os.path.realpath(file_path)
        real_workspace = os.path.realpath(workspace)
        if not real_path.startswith(real_workspace + os.sep):
            duration = time.time() - start_time
            _log_web_event("API", f"GET /api/v1/workspace/download - BLOCKED traversal - {duration:.2f}ms",
                           extra={"session_id": session_id, "filename": filename})
            return JSONResponse(
                status_code=403,
                content={"error": "Acceso fuera del workspace"}
            )

        # Seguridad 2: bloquear archivos internos (no son output del usuario)
        fn_lower = filename.lower()
        blocked_exact = {"script.py", "install_log.jsonl"}
        blocked_prefixes = ("attempt_", "venv")
        if fn_lower in blocked_exact:
            duration = time.time() - start_time
            _log_web_event("API", f"GET /api/v1/workspace/download - BLOCKED internal - {duration:.2f}ms",
                           extra={"session_id": session_id, "filename": filename})
            return JSONResponse(
                status_code=403,
                content={"error": "Archivo interno del sistema, no descargable"}
            )
        for prefix in blocked_prefixes:
            if fn_lower.startswith(prefix):
                duration = time.time() - start_time
                _log_web_event("API", f"GET /api/v1/workspace/download - BLOCKED prefix - {duration:.2f}ms",
                               extra={"session_id": session_id, "filename": filename})
                return JSONResponse(
                    status_code=403,
                    content={"error": "Archivo interno del sistema, no descargable"}
                )

        # Seguridad 3: el archivo debe existir y ser un archivo (no directorio)
        if not os.path.isfile(real_path):
            duration = time.time() - start_time
            _log_web_event("API", f"GET /api/v1/workspace/download - NOT FOUND - {duration:.2f}ms",
                           extra={"session_id": session_id, "filename": filename})
            return JSONResponse(
                status_code=404,
                content={"error": f"Archivo no encontrado: {filename}"}
            )

        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/workspace/download - SUCCESS - {duration:.2f}ms",
                       extra={"session_id": session_id, "filename": filename, "size": os.path.getsize(real_path)})
        return FileResponse(
            real_path,
            filename=filename,
            media_type="application/octet-stream",
        )
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.download_workspace_file", extra={"duration": duration})
        return JSONResponse(status_code=500, content={"error": str(e)})
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /workspace/attempts (FASE 9) — Historial de intentos
# ═══════════════════════════════════════════════════════════════
@router.get("/workspace/attempts")
async def get_attempt_history(session_id: str):
    """
    Retorna el historial de intentos con contenido de código fuente.
    Incluye el script.py actual como attempt 'final'.

    Args:
        session_id: ID de la sesión

    Returns:
        Dict con session_id, total, y lista de attempts (con campo "content")
    """
    start_time = time.time()
    _log_web_event("API", f"GET /api/v1/workspace/attempts session={session_id}")
    try:
        from src.core.workspace_manager import get_workspace_manager
        wm = get_workspace_manager()
        history = wm.get_attempt_history(session_id)
        workspace = wm.get_workspace(session_id)

        attempts = []
        for entry in history:
            filepath = entry["file"]
            if os.path.isfile(filepath):
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                attempts.append({
                    "attempt": entry["attempt"],
                    "lines": content.count("\n") + 1,
                    "size_bytes": entry["size_bytes"],
                    "content": content,
                })

        # Incluir script.py actual como "final"
        script_path = os.path.join(workspace, "script.py")
        if os.path.isfile(script_path):
            with open(script_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            attempts.append({
                "attempt": "final",
                "lines": content.count("\n") + 1,
                "size_bytes": os.path.getsize(script_path),
                "content": content,
            })

        duration = time.time() - start_time
        _log_web_event("API", f"GET /api/v1/workspace/attempts - SUCCESS - {duration:.2f}ms",
                       extra={"session_id": session_id, "total": len(attempts)})
        return {
            "session_id": session_id,
            "total": len(attempts),
            "attempts": attempts,
        }
    except Exception as e:
        duration = time.time() - start_time
        _log_error(e, "routes.get_attempt_history", extra={"duration": duration})
        return JSONResponse(status_code=500, content={"error": str(e)})
# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /metrics/degradation (v0.6.9v, P1 del informe)
# ═══════════════════════════════════════════════════════════════
# Hace CONSULTABLE lo que antes solo existía como línea de log:
# cuántos `[degradado/contrato]` hubo, en qué módulo y con qué excepción.
# Si aparece un error de contrato en una ruta crítica (dispatcher, memory,
# validator, feedback, quality) el sistema perdió una feature en silencio
# → se devuelve como `alertas` con `hay_alerta: true`.
@router.get("/metrics/degradation")
async def get_degradation_metrics() -> Dict[str, Any]:
    """Métricas de degradación (errores de contrato vs degradación esperada)."""
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/metrics/degradation")
    try:
        from src.utils.degradation import degradation_stats
        stats = degradation_stats()
        duration = time.time() - start_time
        _log_web_event(
            "API",
            f"GET /api/v1/metrics/degradation - SUCCESS - {duration * 1000:.2f}ms",
            extra={"contract": stats.get("contract"), "soft": stats.get("soft"),
                   "hay_alerta": stats.get("hay_alerta")},
        )
        stats["duration_ms"] = round(duration * 1000, 2)
        return stats
    except Exception as exc:
        duration = time.time() - start_time
        _log_error(exc, "routes.get_degradation_metrics",
                   extra={"duration": duration})
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /feedback/stats (v0.6.9v, P1 del informe)
# ═══════════════════════════════════════════════════════════════
# "Explotar el feedback ROUTED": la señal se registraba desde ISSUE-033 pero
# nadie la leía. Acá se expone el desglose por señal/tarea/agente para poder
# medir ruteo, handlers y calidad por flujo.
@router.get("/feedback/stats")
async def get_feedback_stats() -> Dict[str, Any]:
    """Estadísticas del FeedbackLoop + desglose de señales implícitas."""
    start_time = time.time()
    _log_web_event("API", "GET /api/v1/feedback/stats")
    try:
        from src.core.feedback_loop import get_feedback_loop
        loop = await get_feedback_loop()
        if loop is None:
            return {"available": False, "reason": "feedback loop no disponible"}
        payload: Dict[str, Any] = {
            "available": True,
            "global": await loop.get_global_stats(),
            "signals": await loop.get_signal_breakdown(),
            # ✅ ISSUE-067: umbrales EWMA por tarea (telemetría real, antes muerta)
            "thresholds": _thresholds_safe(loop),
        }
        duration = time.time() - start_time
        _log_web_event(
            "API",
            f"GET /api/v1/feedback/stats - SUCCESS - {duration * 1000:.2f}ms",
            extra={"routed": (payload["signals"] or {}).get("routed")},
        )
        payload["duration_ms"] = round(duration * 1000, 2)
        return payload
    except Exception as exc:
        duration = time.time() - start_time
        _log_error(exc, "routes.get_feedback_stats", extra={"duration": duration})
        return JSONResponse(status_code=500, content={"error": str(exc)})


def _thresholds_safe(loop: Any) -> Dict[str, Any]:
    """Umbrales EWMA por tarea del FeedbackLoop (nunca rompe el endpoint)."""
    try:
        opt = getattr(loop, "_threshold_optimizer", None)
        if opt is None:
            return {}
        task_types = ["general_chat", "code_generation", "code_correction",
                      "complex_reasoning", "web_search", "research"]
        return {t: opt.get_threshold(t) for t in task_types}
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "routes._thresholds_safe")
        return {}


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE ENDPOINTS — SYSTEM v0.2.0 (COMPLETO)
# ═══════════════════════════════════════════════════════════════
# | Endpoint                       | Método | Propósito                    | Estado  |
# |--------------------------------|--------|------------------------------|---------|
# | /api/v1/status                 | GET    | Estado del sistema           | ✅ Único|
# | /api/v1/sessions               | GET    | Listar sesiones              | ✅ Único|
# | /api/v1/sessions/{id}          | GET    | Detalles de sesión           | ✅ Único|
# | /api/v1/sessions/{id}          | DELETE | Eliminar sesión              | ✅ Único|
# | /api/v1/models                 | GET    | Listar modelos               | ✅ Único|
# | /api/v1/stats                  | GET    | Estadísticas del sistema     | ✅ Único|
# | /api/v1/workspace/download     | GET    | Descargar archivo workspace  | ✅ FASE 8|
# | /api/v1/workspace/attempts     | GET    | Historial de intentos        | ✅ FASE 9|
# | /api/v1/metrics/degradation    | GET    | Degradaciones (contrato/soft)| ✅ v0.6.9v|
# | /api/v1/feedback/stats         | GET    | Feedback + señales (ROUTED)  | ✅ v0.6.9v|
# ═══════════════════════════════════════════════════════════════
# 🗑️ ENDPOINTS ELIMINADOS (DUPLICADOS CON routes.py):
# | Endpoint              | Razón de eliminación                      |
# |-----------------------|-------------------------------------------|
# | /analyze_file         | Duplicado → routes.py (Core)              |
# | /api/status           | Duplicado → routes.py (System)            |
# | /health               | Duplicado → routes.py (Core)              |
# | /models/check         | Duplicado → routes.py (System)            |
# | /backup               | Duplicado → routes.py (System)            |
# | /shutdown             | Duplicado → routes.py (System)            |
# | /metrics              | ELIMINADO v0.6.8s (Dashboard retirado)    |
# | /health/detailed      | ELIMINADO v0.6.8s (Dashboard retirado)    |
# | /metrics/agents       | ELIMINADO v0.6.8s (Dashboard retirado)    |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ✅ Balance: routes.py (Core) + routes_extended.py (System + Workspace)
# ✅ Sin importaciones circulares — router singleton desde routes.py
# ✅ Logging consistente — helpers importados desde routes.py
# ✅ FASE 8: Path traversal, bloqueo de archivos internos, FileResponse streaming
# ✅ FASE 9: Historial con contenido, errors="replace", attempt "final"
# ═══════════════════════════════════════════════════════════════
