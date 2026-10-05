#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Rutas Hardware Fit
# Ruta: src/interfaces/web/routes_hwfit.py
# Autor: SaintWick
# Versión: 0.1.0
# Propósito: Endpoints /api/v1/hwfit/* — detección de hardware y
#            evaluación de compatibilidad de modelos Ollama.
#            Todos los endpoints son non-fatal: errores devuelven
#            JSON estructurado en lugar de propagar excepciones.
# ═══════════════════════════════════════════════════════════════
import asyncio
import time
from typing import Any, Dict

from fastapi.responses import JSONResponse

from src.interfaces.web.routes import (
    router,
    _log_web_event,
    _log_error,
    logger,
)


# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /hwfit/hardware — Perfil de hardware detectado
# ═══════════════════════════════════════════════════════════════
@router.get("/hwfit/hardware")
async def hwfit_hardware(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Retorna el perfil de hardware detectado (backend, VRAM, RAM, budget, threads).
    El resultado se cachea 24 h; force_refresh=true fuerza re-detección.
    """
    start = time.time()
    _log_web_event("API", "GET /api/v1/hwfit/hardware")
    try:
        from src.core.hardware_fit import HardwareFitManager
        hw = await asyncio.to_thread(
            HardwareFitManager.instance().get_hardware,
            force_refresh,
        )
        duration = time.time() - start
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/hardware - SUCCESS - {duration * 1000:.2f}ms",
            extra={"backend": hw.backend, "budget_gb": round(hw.effective_budget_gb, 2)},
        )
        return {
            "backend": hw.backend,
            "gpu_name": hw.gpu_name,
            "gpu_vram_gb": round(hw.gpu_vram_gb, 2),
            "effective_budget_gb": round(hw.effective_budget_gb, 2),
            "ram_gb": round(hw.ram_gb, 2),
            "cpu_threads": hw.cpu_threads,
            "is_gpu_available": hw.is_gpu_available,
            "detected_at": hw.detected_at,
            "duration_ms": round(duration * 1000, 2),
        }
    except Exception as exc:
        duration = time.time() - start
        _log_error(exc, "routes_hwfit.hwfit_hardware")
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/hardware - ERROR - {duration * 1000:.2f}ms",
            extra={"error": str(exc)},
        )
        logger.error(f"hwfit_hardware error: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /hwfit/rank — Modelos Ollama rankeados por fit
# ═══════════════════════════════════════════════════════════════
@router.get("/hwfit/rank")
async def hwfit_rank() -> Dict[str, Any]:
    """
    Evalúa todos los modelos Ollama instalados contra el hardware detectado.
    Orden retornado: perfect → good → marginal → no_fit.
    """
    start = time.time()
    _log_web_event("API", "GET /api/v1/hwfit/rank")
    try:
        from src.core.hardware_fit import HardwareFitManager
        results = await asyncio.to_thread(HardwareFitManager.instance().rank_models)
        by_fit: Dict[str, int] = {}
        serialized = []
        for r in results:
            by_fit[r.fit_level] = by_fit.get(r.fit_level, 0) + 1
            serialized.append({
                "model_name": r.model_name,
                "size_gb": round(r.size_gb, 2),
                "required_gb": round(r.required_gb, 2),
                "fit_level": r.fit_level,
                "run_mode": r.run_mode,
                "notes": r.notes,
            })
        duration = time.time() - start
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/rank - SUCCESS - {duration * 1000:.2f}ms",
            extra={"count": len(serialized), "by_fit_level": by_fit},
        )
        return {
            "count": len(serialized),
            "by_fit_level": by_fit,
            "models": serialized,
            "duration_ms": round(duration * 1000, 2),
        }
    except Exception as exc:
        duration = time.time() - start
        _log_error(exc, "routes_hwfit.hwfit_rank")
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/rank - ERROR - {duration * 1000:.2f}ms",
            extra={"error": str(exc)},
        )
        logger.error(f"hwfit_rank error: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /hwfit/check/{model_name} — Modelo específico
# ═══════════════════════════════════════════════════════════════
@router.get("/hwfit/check/{model_name:path}")
async def hwfit_check_model(model_name: str) -> Dict[str, Any]:
    """
    Verifica compatibilidad de un modelo específico instalado en Ollama.
    Soporta nombres con '/' (ej: qwen2.5-coder:7b).
    Retorna 404 si el modelo no está instalado en Ollama.
    """
    start = time.time()
    _log_web_event("API", f"GET /api/v1/hwfit/check/{model_name}")
    try:
        from src.core.hardware_fit import HardwareFitManager
        result = await asyncio.to_thread(
            HardwareFitManager.instance().check_model,
            model_name,
        )
        duration = time.time() - start
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/check/{model_name} - SUCCESS - {duration * 1000:.2f}ms",
            extra={"fit_level": result.fit_level},
        )
        return {
            "model_name": result.model_name,
            "size_gb": round(result.size_gb, 2),
            "required_gb": round(result.required_gb, 2),
            "fit_level": result.fit_level,
            "run_mode": result.run_mode,
            "notes": result.notes,
            "duration_ms": round(duration * 1000, 2),
        }
    except ValueError as exc:
        duration = time.time() - start
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/check/{model_name} - NOT FOUND - {duration * 1000:.2f}ms",
        )
        return JSONResponse(
            status_code=404,
            content={"error": str(exc), "model_name": model_name},
        )
    except Exception as exc:
        duration = time.time() - start
        _log_error(exc, f"routes_hwfit.hwfit_check_model/{model_name}")
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/check/{model_name} - ERROR - {duration * 1000:.2f}ms",
            extra={"error": str(exc)},
        )
        logger.error(f"hwfit_check_model error: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# ENDPOINT: /hwfit/compatible — Modelos de settings vs hardware
# ═══════════════════════════════════════════════════════════════
@router.get("/hwfit/compatible")
async def hwfit_compatible() -> Dict[str, Any]:
    """
    Verifica si los modelos configurados en settings caben en el hardware.
    Solo incluye modelos que están instalados en Ollama.
    Campo all_compatible=false indica que al menos uno es no_fit o marginal.
    """
    start = time.time()
    _log_web_event("API", "GET /api/v1/hwfit/compatible")
    try:
        from src.core.hardware_fit import HardwareFitManager
        report = await asyncio.to_thread(
            HardwareFitManager.instance().is_current_config_compatible
        )
        has_warnings = any(
            r.fit_level in ("marginal", "no_fit") for r in report.values()
        )
        serialized = {
            key: {
                "model_name": r.model_name,
                "size_gb": round(r.size_gb, 2),
                "required_gb": round(r.required_gb, 2),
                "fit_level": r.fit_level,
                "run_mode": r.run_mode,
                "notes": r.notes,
            }
            for key, r in report.items()
        }
        duration = time.time() - start
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/compatible - SUCCESS - {duration * 1000:.2f}ms",
            extra={"models_checked": len(serialized), "has_warnings": has_warnings},
        )
        return {
            "all_compatible": not has_warnings,
            "has_warnings": has_warnings,
            "models_checked": len(serialized),
            "results": serialized,
            "duration_ms": round(duration * 1000, 2),
        }
    except Exception as exc:
        duration = time.time() - start
        _log_error(exc, "routes_hwfit.hwfit_compatible")
        _log_web_event(
            "API",
            f"GET /api/v1/hwfit/compatible - ERROR - {duration * 1000:.2f}ms",
            extra={"error": str(exc)},
        )
        logger.error(f"hwfit_compatible error: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE ENDPOINTS — HWFIT v0.1.0
# ═══════════════════════════════════════════════════════════════
# | Endpoint                          | Método | Propósito                        |
# |-----------------------------------|--------|----------------------------------|
# | /api/v1/hwfit/hardware            | GET    | Perfil hardware (VRAM/RAM/budget)|
# | /api/v1/hwfit/rank                | GET    | Todos los modelos Ollama rankeados|
# | /api/v1/hwfit/check/{model_name}  | GET    | Modelo específico (soporta '/')  |
# | /api/v1/hwfit/compatible          | GET    | Modelos de settings vs hardware  |
# ═══════════════════════════════════════════════════════════════
# ✅ Todos los endpoints usan asyncio.to_thread (httpx sync → no bloquea event loop)
# ✅ model_name usa {param:path} para soportar '/' en nombres (HuggingFace style)
# ✅ check/{model_name} → 404 si modelo no instalado en Ollama (ValueError)
# ✅ Non-fatal: todos los errores → JSONResponse estructurado, nunca propagan
# ✅ Logging consistente — helpers importados desde routes.py
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
