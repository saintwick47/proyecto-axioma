#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/interfaces/web/vision_ocr.py
# v0.6.9p (Fase 2) — OCR local con Tesseract (segunda opinión).
#
# Tesseract es determinista y rapidísimo (~0.1-0.5 s) en texto impreso
# limpio — complementa al VLM donde flaquea (texto chico/mediano,
# contraste bajo, tablas). Se invoca por subprocess (sin dependencia
# Python nueva) y devuelve texto + confianza media (formato TSV).
#
# tesseract-ocr + tesseract-ocr-spa son dependencias del SISTEMA:
#   sudo apt install tesseract-ocr tesseract-ocr-spa
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    """Resultado de Tesseract: texto + confianza media (0-100)."""
    text: str
    confidence: float


def tesseract_available() -> bool:
    """True si el binario tesseract está en el PATH (dependencia de sistema)."""
    return shutil.which("tesseract") is not None


def _parse_tsv(tsv: str) -> tuple[str, float]:
    """Parsea el TSV de Tesseract: une palabras y promedia sus confianzas."""
    confs: list[float] = []
    words: list[str] = []
    for line in tsv.splitlines():
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        # level,page,block,par,line,word: cols[0]=='5' → word row
        try:
            if cols[0] != "5":
                continue
            conf = float(cols[10])
            word = cols[11].strip()
            if word and conf >= 0:
                words.append(word)
                confs.append(conf)
        except (ValueError, IndexError):
            continue
    text = " ".join(words).strip()
    confidence = (sum(confs) / len(confs)) if confs else 0.0
    return text, confidence


def ocr_image(
    image_b64: str,
    lang: str = "spa",
    psm: int = 6,
    timeout: float = 20.0,
    ocr_bin: Optional[str] = None,
) -> Optional[OCRResult]:
    """Corre Tesseract sobre la imagen (base64).

    Args:
        image_b64: Imagen en base64 (PNG/JPEG/WebP…).
        lang: Lenguaje de tessdata (default spa — español + latín).
        psm: Page segmentation mode (6 = bloque de texto uniforme).
        timeout: Timeout del subprocess en segundos.
        ocr_bin: Binario override (tests) — default "tesseract".

    Returns:
        OCRResult con texto y confianza, o None si no hay tesseract,
        falla, vence el timeout o no detecta texto.
    """
    if ocr_bin is None and not tesseract_available():
        return None
    binary = ocr_bin or "tesseract"
    try:
        raw = base64.b64decode(image_b64)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(raw)
            tmp_path = tf.name
        try:
            proc = subprocess.run(
                [binary, tmp_path, "stdout", "-l", lang,
                 "--psm", str(psm), "tsv"],
                capture_output=True, timeout=timeout,
            )
            if proc.returncode != 0:
                return None
            tsv = proc.stdout.decode("utf-8", errors="ignore")
            text, confidence = _parse_tsv(tsv)
            if not text:
                return None
            return OCRResult(text=text, confidence=round(confidence, 1))
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError as exc:  # limpieza best-effort del temp
                logger.debug(f"[vision_ocr] limpieza temp: {exc}")
    except subprocess.TimeoutExpired as exc:
        # Degradación intencional: sin OCR local se cae al VLM (no es fatal)
        logger.debug(f"[vision_ocr] timeout Tesseract: {exc}")
        return None
    except (OSError, ValueError) as exc:
        # b64 inválido o binario ausente → el canal usa el VLM como fallback
        logger.debug(f"[vision_ocr] no disponible: {exc}")
        return None
