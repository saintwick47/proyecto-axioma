#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Camera Capture Engine
# Ruta: src/multimodal/camera_engine.py
# Autor: SaintWick
# Versión: 1.0.0 (Rafael Fase 2)
# Propósito: Captura de cámara vía OpenCV, mismo contrato de salida
#            que ScreenCaptureEngine (CaptureFrame) para reusar
#            VisionAgent sin cambios.
# Dependencia nueva: opencv-python (cv2.VideoCapture) — confirmar
#            instalación en el venv real.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from src.multimodal.screen_capture_engine import CaptureFrame, ImageProcessor

logger = logging.getLogger(__name__)

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    _CV2_AVAILABLE = False
    logger.warning("opencv-python no disponible — CameraCaptureEngine deshabilitado")


class CameraCaptureEngine:
    """
    Motor de captura de cámara. Mismo contrato que ScreenCaptureEngine.

    Uso básico:
        engine = CameraCaptureEngine()
        frame = await engine.capture_async()
        # frame es CaptureFrame con image_b64 listo para VisionAgent
    """

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._processor = ImageProcessor()
        self._lock = threading.Lock()
        logger.info(
            f"CameraCaptureEngine inicializado (device={device_index}, "
            f"cv2_available={_CV2_AVAILABLE})"
        )

    def is_available(self) -> bool:
        if not _CV2_AVAILABLE:
            return False
        cap = cv2.VideoCapture(self._device_index)
        try:
            return cap.isOpened()
        finally:
            cap.release()

    # ── Capture (sync) ──────────────────────────────────────────────────────

    def capture_sync(self) -> Optional[CaptureFrame]:
        """Captura sincrónica. Usar desde threads (asyncio.to_thread)."""
        if not _CV2_AVAILABLE:
            logger.error("CameraCaptureEngine: opencv-python no disponible")
            return None

        with self._lock:
            cap = cv2.VideoCapture(self._device_index)
            try:
                if not cap.isOpened():
                    logger.error(
                        f"CameraCaptureEngine: no se pudo abrir device={self._device_index}"
                    )
                    return None
                t0 = time.time()
                ok, frame = cap.read()
                if not ok or frame is None:
                    logger.error("CameraCaptureEngine: cap.read() falló")
                    return None
            finally:
                cap.release()

        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            logger.error("CameraCaptureEngine: cv2.imencode falló")
            return None

        capture_ms = (time.time() - t0) * 1000
        raw_bytes = buf.tobytes()
        logger.debug(f"CameraCaptureEngine: captura OK {capture_ms:.0f}ms raw={len(raw_bytes)}B")

        b64, w, h, byte_size = self._processor.process(raw_bytes)

        return CaptureFrame(
            image_b64=b64,
            width=w,
            height=h,
            timestamp=time.time(),
            monitor="camera",
            context_hint="generic",
            active_window="",
            backend_used="opencv",
            byte_size=byte_size,
        )

    # ── Capture (async) ─────────────────────────────────────────────────────

    async def capture_async(self) -> Optional[CaptureFrame]:
        """Captura asíncrona. Ejecuta en asyncio.to_thread para no bloquear el event loop."""
        return await asyncio.to_thread(self.capture_sync)

    def get_stats(self) -> dict:
        return {
            "device_index": self._device_index,
            "cv2_available": _CV2_AVAILABLE,
            "is_available": self.is_available() if _CV2_AVAILABLE else False,
        }
