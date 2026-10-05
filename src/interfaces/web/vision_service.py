#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/interfaces/web/vision_service.py
# v0.6.9p — descripción/análisis de imágenes del chat con qwen3-vl.
#
# Modo REAL: usa vision_agent.describe_generic_image() (prompt NEUTRO,
# sin el sesgo de screenshot del VisionAgent) y descarga el VLM de RAM
# al terminar (best-effort) — hardware marginal.
# Modo TEST (agent inyectado): replica el contrato anterior para poder
# probar la lógica sin Ollama.
# describe_image() es síncrona (bloqueante): llamarla desde un worker
# thread (asyncio.to_thread / run_in_executor), nunca desde el loop UI.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
from typing import Optional

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# Indicios de que el usuario quiere LEER/transcribir texto → Tesseract
_READING_HINT = re.compile(
    r"\b(cop[ií]a|transcrib[ií]|le[eé](?:me|elo|é)?|dice|diga|texto|"
    r"escrib[ií]|lectura|ocr|cartel|etiqueta)\b", re.IGNORECASE)


def _instruction_pide_lectura(instruction: str) -> bool:
    """True si la instrucción pide transcribir/leer texto de la imagen."""
    return bool(instruction) and bool(_READING_HINT.search(instruction))


def _unload_best_effort(model: Optional[str], host: Optional[str]) -> None:
    """Pide a Ollama liberar el VLM de RAM (keep_alive=0) — best-effort.

    Si falla no importa: Ollama igual lo descarga al vencer el keep_alive
    (default 5 min).
    """
    if not model or not host:
        return
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            f"{host.rstrip('/')}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001 — best-effort: nunca romper el canal
        logger.debug(f"[vision_service] unload VLM omitido: {exc}")


def _unload_other_models(keep_model: Optional[str], host: Optional[str]) -> None:
    """Descarga de Ollama todo modelo distinto del VLM (qwen3:8b, coder…).

    v0.6.9q: el warm-up del web deja qwen3:8b residente (~7.4 GB); con RAM
    marginal el VLM no entra y el análisis de imagen hacía timeout silencioso.
    Liberamos los otros ANTES de cargar qwen3-vl (el chat recarga su modelo
    solo en el próximo mensaje).
    """
    if not host:
        return
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(
                f"{host.rstrip('/')}/api/ps", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("models", []):
            name = m.get("name")
            if name and name != keep_model:
                logger.info(f"[vision_service] liberando {name} para el VLM")
                _unload_best_effort(name, host)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug(f"[vision_service] liberar otros modelos omitido: {exc}")


def _log_outcome(via: str, ok: bool, error: Optional[str],
                 seconds: float, chars: int, model: Optional[str]) -> None:
    """Registra el resultado del análisis en detailed_logger (diagnóstico)."""
    msg = ("[✓] imagen analizada" if ok
           else f"[✗] análisis de imagen falló ({error})")
    try:
        from tools.detailed_logger import get_logger as _get
        dl = _get()
        if dl is not None and getattr(dl, "is_initialized", False):
            dl.log_event(
                "VISION_CHAT", msg, level="INFO" if ok else "ERROR",
                extra={"via": via, "success": ok, "error": error,
                       "duration_ms": round(seconds * 1000, 1),
                       "chars": chars, "model": model or ""})
    except Exception as e:
        # R3: el log detallado es accesorio; el resultado de visión ya está calculado.
        log_degraded(logger, e, "vision_service._log_outcome (detailed_logger)",
                     contract_level=logging.DEBUG)
    if ok:
        logger.info(f"[vision_service] {via} OK en {seconds:.0f}s "
                    f"({chars} chars, {model})")
    else:
        logger.warning(f"[vision_service] {via} FALLÓ en {seconds:.0f}s: {error}")


def describe_image(
    image_b64: str,
    instruction: str = "",
    context_hint: str = "generic",
    agent=None,
) -> str:
    """Analiza una imagen (base64) con el VLM local y devuelve texto.

    Args:
        image_b64: Imagen codificada en base64.
        instruction: Pregunta/instrucción del usuario (vacío → describir).
        context_hint: Solo usado en el modo test (agente inyectado). En el
            modo real se usa el prompt NEUTRO de describe_generic_image
            (los hints del VisionAgent son para screenshots).
        agent: VisionAgent inyectado (tests) — si es None se usa el canal
            real genérico con la config de settings (llm_vision_model).

    Returns:
        str — respuesta del VLM (recortada).

    Raises:
        RuntimeError: si no hay imagen, el VLM falla o responde vacío.
    """
    if not image_b64:
        raise RuntimeError("no hay imagen (image_b64 vacío)")

    if agent is not None:
        # ── Modo test: replica el contrato de VisionAgent.execute ──
        from types import SimpleNamespace
        user_query = (instruction or "").strip() or \
            "Describí esta imagen en detalle."
        msg = SimpleNamespace(content=user_query)
        result = agent.execute(
            msg, image_b64=image_b64, context_hint=context_hint,
            active_window="",
        )
        content = (getattr(result, "content", "") or "").strip()
        if not getattr(result, "success", False) or not content:
            error = getattr(result, "error", None) or "respuesta vacía del VLM"
            _unload_best_effort(getattr(agent, "model", None),
                                getattr(agent, "host", None))
            raise RuntimeError(f"visión falló: {error}")
        _unload_best_effort(getattr(agent, "model", None),
                            getattr(agent, "host", None))
        return content

    # ── Modo real: prompt neutro + preprocesado + gestión de RAM ──
    from config.settings import settings
    import time as _time
    model = getattr(settings, "llm_vision_model", None)
    host = getattr(settings, "ollama_host", "http://127.0.0.1:11434")
    t0 = _time.time()
    out: Optional[str] = None
    error: Optional[str] = None
    via = "vlm"
    try:
        # ✅ Fase 2: si pide leer/transcribir texto, Tesseract local es más
        # exacto (texto chico/mediano/impreso) y ~100x más rápido que el VLM.
        if getattr(settings, "vision_ocr_enabled", True) and \
                _instruction_pide_lectura(instruction):
            from src.interfaces.web.vision_ocr import ocr_image
            ocr = ocr_image(image_b64)
            if ocr is not None and ocr.confidence >= getattr(
                    settings, "vision_ocr_min_conf", 60.0):
                out = ocr.text
                via = "ocr"

        # ✅ v0.6.9q: liberar otros modelos (chat/coder) ANTES del VLM — el
        # warm-up del web deja qwen3:8b residente y el VLM hacía timeout.
        if out is None:
            _unload_other_models(model, host)

        # ✅ Fase 1: preprocesado adaptativo (autocontraste/upscale) y, si
        # la variante primaria falla, reintento con la original.
        if out is None:
            from src.interfaces.web.vision_preprocess import build_variants
            from src.agents.vision_agent import describe_generic_image
            variants = build_variants(
                image_b64,
                enabled=getattr(settings, "vision_preprocess_enabled", True),
            )
            last_error: Optional[Exception] = None
            for variant in variants:
                try:
                    out = describe_generic_image(variant, instruction)
                    break
                except Exception as exc:  # noqa: BLE001 — reportamos y seguimos
                    last_error = exc
            if out is None:
                error = (
                    f"visión falló tras {len(variants)} intentos: {last_error}")
    finally:
        _unload_best_effort(model, host)
        _log_outcome(via=via, ok=out is not None, error=error,
                     seconds=_time.time() - t0,
                     chars=len(out or ""), model=model)

    if out is not None:
        return out
    raise RuntimeError(error or "visión falló")
