#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/interfaces/web/vision_preprocess.py
# v0.6.9p (Fase 1) — preprocesado adaptativo del canal de imágenes.
#
# Decide y prepara la imagen ANTES del VLM con análisis CV barato (PIL):
#   - contraste bajo  → autocontraste (ImageOps.autocontrast)
#   - imagen chica    → upscale 2x (LANCZOS, mantiene aspecto)
#   - normal          → se usa la original tal cual
#
# build_variants() devuelve variantes ORDENADAS: [primaria, original...]
# para que el canal reintente con la original si la primaria falla.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import base64
import io
import math
from typing import Dict, List

from PIL import Image, ImageOps

_LOW_CONTRAST_STD = 30.0   # desviación estándar de grises por debajo de la cual → autocontraste
_SMALL_SIDE = 400          # imágenes con lado máximo menor a esto se upscalean 2x
_UPSCALE_FACTOR = 2.0
_UPSCALE_MAX_SIDE = 1600   # techo del upscale (no disparar tokens)


def _decode(image_b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")


def _encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _gray_stddev(img: Image.Image) -> float:
    """Desviación estándar de la luminancia (0-255).

    Robusta al grosor del texto: negro sobre blanco → alto; gris claro
    sobre blanco → bajo; imagen sólida → 0 (no dispara autocontraste).
    """
    gray = img.convert("L")
    hist = gray.histogram()
    total = max(1, sum(hist))
    mean = sum(v * c for v, c in enumerate(hist)) / total
    var = sum(((v - mean) ** 2) * c for v, c in enumerate(hist)) / total
    return math.sqrt(var)


def analyze(image_b64: str) -> Dict[str, float]:
    """Métricas CV baratas: lado máximo y desviación estándar de grises."""
    img = _decode(image_b64)
    return {
        "max_side": float(max(img.size)),
        "contrast_std": _gray_stddev(img),
    }


def build_variants(image_b64: str, enabled: bool = True) -> List[str]:
    """Variantes ordenadas [primaria, original] para el intento con VLM.

    Si el análisis es normal devuelve solo la original (cero costo).
    """
    variants = [image_b64]
    if not enabled or not image_b64:
        return variants
    try:
        img = _decode(image_b64)
        max_side = max(img.size)
        stddev = _gray_stddev(img)
        primary = None

        if 0.0 < stddev < _LOW_CONTRAST_STD:
            # Contraste bajo (texto gris sobre blanco, etc.)
            primary = ImageOps.autocontrast(img, cutoff=2)
        elif max_side <= _SMALL_SIDE:
            # Imagen chica → upscale para darle detalle al VLM
            scale = min(_UPSCALE_FACTOR, _UPSCALE_MAX_SIDE / max_side)
            if scale > 1.0:
                w, h = img.size
                primary = img.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.LANCZOS,
                )

        if primary is not None:
            return [_encode(primary), image_b64]
        return variants
    except Exception:
        return variants
