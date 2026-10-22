#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — settings_vision.py  (REFACTORIZACIÓN POR PARTES, pieza 6a)
# Ruta: config/settings_vision.py
#
# ✅ 2026-10-13: dominio **SCREEN CAPTURE / VISION — v0.3.6** extraído de `config/settings.py`, sin
#    cambios de comportamiento. Son CAMPOS, así que el mixin hereda de `pydantic.BaseModel` (una
#    clase común no recolecta anotaciones como campos); el mixin de MÉTODOS que ya existía
#    (`settings_computations.py`) es una clase común a propósito.
#
#   Qué se movió desde settings.py (líneas 764-832 del original): los 12 campos de captura de
#   pantalla y visión (`enable_screen_capture`, `vision_capture_*`, `vision_jpeg_quality`,
#   `vision_context_hint_enabled`, `vision_response_cache_ttl`, `vision_num_predict`,
#   `vision_preprocess_enabled`, `vision_ocr_*`, `vision_model_selector`). No tiene validadores.
#
#   `Settings(BaseSettings, SettingsVision, SettingsComputations)` hereda este mixin, así la API
#   pública NO cambia: mismos nombres, anotaciones y defaults.
# ═══════════════════════════════════════════════════════════════
from typing import Literal

from pydantic import BaseModel, Field


class SettingsVision(BaseModel):
    """Dominio de captura de pantalla y visión (v0.3.6).

    Extraído de `Settings` (pieza 6a). Sólo CAMPOS: no tiene validadores propios.
    """

    # ═══════════════════════════════════════════════════════════
    # === SCREEN CAPTURE / VISION — v0.3.6 ===
    # ═══════════════════════════════════════════════════════════
    enable_screen_capture: bool = Field(
        default=True,
        description="Habilita ScreenCaptureEngine para análisis visual"
    )
    vision_capture_backend: str = Field(
        default="auto",
        description="Backend de captura: auto | grim | scrot | import_magick | pil"
    )
    # ✅ v0.1.6b: RESTAURADO a resolución ORIGINAL (3840×2160 para soportar 4K/HiDPI)
    # El resize a 800px destruía legibilidad de texto en IDEs, múltiples monitores, HiDPI
    # gemma4:e4b probado exitosamente en 1920×1080 (111s, 1004 tokens) — resolución nativa funciona
    vision_capture_max_width: int = Field(
        default=3840, ge=640, le=7680,
        description="Ancho máximo del frame enviado al VisionModel (3840 soporta 4K nativo, resize deshabilitado)"
    )
    vision_capture_max_height: int = Field(
        default=2160, ge=480, le=4320,
        description="Alto máximo del frame enviado al VisionModel (2160 soporta 4K nativo, resize deshabilitado)"
    )
    vision_jpeg_quality: int = Field(
        default=82, ge=50, le=95,
        description="Calidad JPEG de compresión del frame (balance calidad/tokens)"
    )
    vision_context_hint_enabled: bool = Field(
        default=True,
        description="Infiere contexto desde la ventana activa (web/code/document/image)"
    )
    vision_response_cache_ttl: int = Field(
        default=30, ge=10, le=300,
        description="TTL del cache de respuestas del VisionAgent (segundos)"
    )
    # ✅ v0.6.9p (Fase 1): robustez del canal de imágenes del chat
    vision_num_predict: int = Field(
        default=512, ge=64, le=2048,
        description="Tope de tokens de salida por generación VLM (evita "
                    "generaciones desbocadas que colgaban hasta el timeout)"
    )
    vision_preprocess_enabled: bool = Field(
        default=True,
        description="Preprocesado adaptativo antes del VLM: autocontraste si "
                    "contraste bajo, upscale 2x si la imagen es chica"
    )
    # ✅ v0.6.9p (Fase 2): OCR híbrido con Tesseract local
    vision_ocr_enabled: bool = Field(
        default=True,
        description="Usa Tesseract local como segunda opinión cuando la "
                    "instrucción pide leer/transcribir texto"
    )
    vision_ocr_min_conf: float = Field(
        default=60.0, ge=0.0, le=100.0,
        description="Confianza mínima de Tesseract (0-100) para aceptar su "
                    "transcripción sin pasar por el VLM"
    )
    # ✅ 2026-10-10: era `Literal["auto", "qwen3-vl:4b"]` —el nombre de UN modelo— y su descripción
    # prometía «forzar uso explícito», pero MEDIDO: `get_vision_config()` devolvía `llm_vision_model` en
    # las dos ramas, así que no tenía ningún efecto. Ahora es texto libre: «auto» = usá el VLM que
    # configuraste (`llm_vision_model`), que es lo que hace siempre, y cualquier otro nombre se acepta
    # por compatibilidad (la interfaz puede guardarlo al recargar ajustes).
    vision_model_selector: str = Field(
        default="auto",
        description=(
            "Selector de modelo de visión para la interfaz. 'auto' = usá `llm_vision_model`, que es el "
            "VLM que configuraste y lo que se usa siempre. Se aceptan otros nombres por compatibilidad."
        )
    )
