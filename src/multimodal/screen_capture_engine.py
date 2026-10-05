#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# screen_capture_engine.py - Screen Capture Engine multi-backend
# Ruta: src/multimodal/screen_capture_engine.py
# Autor: SaintWick
# Versión: 1.0.4
#
# Captura de pantalla con soporte para Wayland/PipeWire/X11,
# detección de monitores, ROI, compresión y contexto (ventana activa).
# Backends: grim (wlroots), spectacle (KDE Plasma Wayland),
# scrot, import (ImageMagick), PIL. Publica frames en asyncio.Queue
# para consumo por Dispatcher/VisionAgent.
# ═══════════════════════════════════════════════════════════════
import asyncio
import base64
import io
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ✅ v1.0.1: Importar settings para configuración dinámica
from config.settings import settings

logger = logging.getLogger(__name__)

# ── Imports opcionales ───────────────────────────────────────────────────────
try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    logger.warning("PIL no disponible — compresión limitada")

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════
# CONSTANTES (Fallbacks si settings falla)
# ═══════════════════════════════════════════════════════════════

# Timeout para cada captura subprocess (segundos)
CAPTURE_SUBPROCESS_TIMEOUT = 8

# Máximo de frames en la queue interna (evita backpressure)
CAPTURE_QUEUE_MAXSIZE = 4

# Directorio temporal para capturas intermedias
_TEMP_DIR = Path("/tmp/axioma_vision")


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class ROI:
    """Región de interés para recorte de pantalla (coordenadas absolutas)."""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def as_grim_geometry(self) -> str:
        """Formato '-g X,Y WxH' para grim."""
        return f"{self.x},{self.y} {self.width}x{self.height}"


@dataclass
class MonitorInfo:
    """Información de un monitor detectado."""
    name: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool = False
    scale: float = 1.0


@dataclass
class CaptureFrame:
    """Frame capturado listo para envío al VisionAgent."""
    image_b64: str                          # base64 JPEG
    width: int
    height: int
    timestamp: float = field(default_factory=time.time)
    monitor: str = "primary"
    roi: Optional[ROI] = None
    context_hint: str = "generic"          # web | document | code | image | generic
    active_window: str = ""
    backend_used: str = "unknown"
    byte_size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image_b64":    self.image_b64,
            "width":        self.width,
            "height":       self.height,
            "timestamp":    self.timestamp,
            "monitor":      self.monitor,
            "context_hint": self.context_hint,
            "active_window": self.active_window,
            "backend_used": self.backend_used,
            "byte_size":    self.byte_size,
        }


# ═══════════════════════════════════════════════════════════════
# MONITOR DETECTOR
# ═══════════════════════════════════════════════════════════════

class MonitorDetector:
    """Detecta monitores disponibles en Wayland (wlr-randr) o X11 (xrandr)."""

    def get_monitors(self) -> List[MonitorInfo]:
        monitors = self._try_wlr_randr()
        if not monitors:
            monitors = self._try_xrandr()
        if not monitors:
            # Fallback genérico — resolución común
            monitors = [MonitorInfo("primary", 0, 0, 1920, 1080, is_primary=True)]
        return monitors

    def get_primary(self) -> MonitorInfo:
        monitors = self.get_monitors()
        for m in monitors:
            if m.is_primary:
                return m
        return monitors[0] if monitors else MonitorInfo("primary", 0, 0, 1920, 1080, True)

    def _try_wlr_randr(self) -> List[MonitorInfo]:
        try:
            r = subprocess.run(
                ["wlr-randr"], capture_output=True, text=True,
                timeout=3, env={**os.environ},
            )
            if r.returncode != 0:
                return []
            return self._parse_wlr_randr(r.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    def _parse_wlr_randr(self, output: str) -> List[MonitorInfo]:
        monitors: List[MonitorInfo] = []
        current_name = ""
        x = y = w = h = 0
        scale = 1.0
        is_primary = False

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Nombre del monitor (primera palabra sin espacios = nombre)
            if not line.startswith(" ") and not line.startswith("\t"):
                if current_name and w > 0:
                    monitors.append(MonitorInfo(current_name, x, y, w, h, is_primary, scale))
                current_name = stripped.split()[0]
                is_primary = "primary" in stripped.lower()
                x = y = w = h = 0
                scale = 1.0
            elif "Position:" in stripped:
                parts = stripped.replace("Position:", "").strip().split(",")
                if len(parts) == 2:
                    x, y = int(parts[0]), int(parts[1])
            elif "Mode:" in stripped or "Current mode:" in stripped:
                part = stripped.split(":")[-1].strip().split()[0]
                if "x" in part:
                    dims = part.split("x")
                    w, h = int(dims[0]), int(dims[1])
            elif "Scale:" in stripped:
                try:
                    scale = float(stripped.split(":")[-1].strip())
                except ValueError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 screen_capture_engine.py:187] excepción degradada (intencional): {_d2e}")

        if current_name and w > 0:
            monitors.append(MonitorInfo(current_name, x, y, w, h, is_primary, scale))

        if monitors and not any(m.is_primary for m in monitors):
            monitors[0].is_primary = True

        return monitors

    def _try_xrandr(self) -> List[MonitorInfo]:
        try:
            r = subprocess.run(
                ["xrandr", "--query"], capture_output=True, text=True, timeout=3,
            )
            if r.returncode != 0:
                return []
            return self._parse_xrandr(r.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    def _parse_xrandr(self, output: str) -> List[MonitorInfo]:
        monitors: List[MonitorInfo] = []
        import re
        pattern = re.compile(
            r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)",
            re.MULTILINE,
        )
        for m in pattern.finditer(output):
            name = m.group(1)
            is_primary = m.group(2) is not None
            w, h = int(m.group(3)), int(m.group(4))
            x, y = int(m.group(5)), int(m.group(6))
            monitors.append(MonitorInfo(name, x, y, w, h, is_primary))
        return monitors


# ═══════════════════════════════════════════════════════════════
# CONTEXT HINT DETECTOR
# ═══════════════════════════════════════════════════════════════

class ContextHintDetector:
    """
    Infiere el context_hint desde la ventana activa del compositor.
    Orden: web > document > code > image > generic
    """

    _WEB_KEYWORDS    = {"firefox", "chromium", "chrome", "brave", "opera", "epiphany", "midori"}
    _CODE_KEYWORDS   = {"code", "neovim", "nvim", "vim", "emacs", "jetbrains", "pycharm",
                        "intellij", "sublime", "gedit", "kate", "helix"}
    _DOC_KEYWORDS    = {"evince", "okular", "zathura", "libreoffice", "soffice",
                        "document", "writer", "impress"}
    _IMAGE_KEYWORDS  = {"gimp", "inkscape", "eog", "geeqie", "shotwell", "krita",
                        "feh", "sxiv", "image"}

    def detect(self, active_window: str) -> str:
        if not active_window:
            return "generic"
        lower = active_window.lower()
        if any(k in lower for k in self._WEB_KEYWORDS):
            return "web"
        if any(k in lower for k in self._DOC_KEYWORDS):
            return "document"
        if any(k in lower for k in self._CODE_KEYWORDS):
            return "code"
        if any(k in lower for k in self._IMAGE_KEYWORDS):
            return "image"
        return "generic"

    def get_active_window_name(self) -> str:
        """Obtiene el nombre de la ventana activa (Wayland → xdotool fallback)."""
        # Intento 1: swaymsg (Sway/i3)
        try:
            r = subprocess.run(
                ["swaymsg", "-t", "get_tree"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0:
                import json
                tree = json.loads(r.stdout)
                focused = self._find_focused(tree)
                if focused:
                    return focused.get("name", "") or focused.get("app_id", "")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as _d2e:
            logging.getLogger(__name__).debug(f"[D2 screen_capture_engine.py:271] excepción degradada (intencional): {_d2e}")

        # Intento 2: xdotool (X11)
        try:
            r = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as _d2e:
            logging.getLogger(__name__).debug(f"[D2 screen_capture_engine.py:282] excepción degradada (intencional): {_d2e}")

        return ""

    def _find_focused(self, node: dict) -> Optional[dict]:
        if node.get("focused"):
            return node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = self._find_focused(child)
            if result:
                return result
        return None


# ═══════════════════════════════════════════════════════════════
# CAPTURE BACKENDS
# ═══════════════════════════════════════════════════════════════

class _CaptureBackend:
    """Base para backends de captura."""
    name: str = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        """Retorna bytes PNG/JPEG o None si falla."""
        raise NotImplementedError


class GrimBackend(_CaptureBackend):
    """
    Captura Wayland nativa usando grim.
    Solo disponible en compositors wlroots (Sway, Hyprland).
    NO funciona en KDE KWin ni GNOME Mutter.
    """
    name = "grim"

    def is_available(self) -> bool:
        try:
            r = subprocess.run(["which", "grim"], capture_output=True, timeout=2)
            if r.returncode != 0:
                return False
            # Test-capture real: detecta si el compositor soporta wlr-screencopy.
            # grim falla inmediatamente con rc!=0 si el protocolo no está disponible.
            tmp = Path("/tmp/_axioma_grim_probe.png")
            probe = subprocess.run(
                ["grim", str(tmp)],
                capture_output=True,
                timeout=4,
                env={**os.environ},
            )
            tmp.unlink(missing_ok=True)
            return probe.returncode == 0
        except Exception:
            return False

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _TEMP_DIR / f"capture_{int(time.time()*1000)}.png"
        cmd = ["grim"]

        if monitor and monitor.name and monitor.name != "primary":
            cmd += ["-o", monitor.name]

        if roi and roi.is_valid:
            cmd += ["-g", roi.as_grim_geometry()]
        elif monitor:
            cmd += ["-g", f"{monitor.x},{monitor.y} {monitor.width}x{monitor.height}"]

        cmd.append(str(out_path))

        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT,
                env={**os.environ},
            )
            if r.returncode != 0:
                logger.debug(f"grim failed: {r.stderr.decode()[:200]}")
                return None
            if out_path.exists():
                data = out_path.read_bytes()
                out_path.unlink(missing_ok=True)
                return data
        except subprocess.TimeoutExpired:
            logger.warning("grim timeout")
        except Exception as e:
            logger.debug(f"grim error: {e}")
        finally:
            out_path.unlink(missing_ok=True)
        return None


class SpectacleBackend(_CaptureBackend):
    """
    Captura KDE Plasma Wayland usando spectacle.
    Backend nativo para KWin — único método confiable en KDE Wayland.
    Usa modo --background --nonotify para captura silenciosa sin GUI.
    """
    name = "spectacle"

    def is_available(self) -> bool:
        try:
            r = subprocess.run(["which", "spectacle"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _TEMP_DIR / f"capture_{int(time.time()*1000)}.png"
        cmd = [
            "spectacle",
            "--background",
            "--nonotify",
            "--fullscreen",
            "--output", str(out_path),
        ]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                timeout=CAPTURE_SUBPROCESS_TIMEOUT,
                env={**os.environ},
            )
            if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                data = out_path.read_bytes()
                out_path.unlink(missing_ok=True)
                return data
            logger.debug(f"spectacle failed rc={r.returncode}: {r.stderr.decode()[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("spectacle timeout")
        except Exception as e:
            logger.debug(f"spectacle error: {e}")
        finally:
            out_path.unlink(missing_ok=True)
        return None


class ScrotBackend(_CaptureBackend):
    """Captura X11 usando scrot."""
    name = "scrot"

    def is_available(self) -> bool:
        try:
            r = subprocess.run(["which", "scrot"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _TEMP_DIR / f"capture_{int(time.time()*1000)}.png"
        cmd = ["scrot"]

        if roi and roi.is_valid:
            cmd += ["-a", f"{roi.x},{roi.y},{roi.width},{roi.height}"]

        cmd.append(str(out_path))

        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT,
            )
            if r.returncode != 0:
                return None
            if out_path.exists():
                data = out_path.read_bytes()
                out_path.unlink(missing_ok=True)
                return data
        except subprocess.TimeoutExpired:
            logger.warning("scrot timeout")
        except Exception as e:
            logger.debug(f"scrot error: {e}")
        finally:
            out_path.unlink(missing_ok=True)
        return None


class ImportBackend(_CaptureBackend):
    """Captura X11 usando ImageMagick import."""
    name = "import_magick"

    def is_available(self) -> bool:
        try:
            r = subprocess.run(["which", "import"], capture_output=True, timeout=2)
            return r.returncode == 0 and os.environ.get("DISPLAY")
        except Exception:
            return False

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        _TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _TEMP_DIR / f"capture_{int(time.time()*1000)}.png"
        cmd = ["import", "-window", "root"]

        if roi and roi.is_valid:
            cmd += ["-crop", f"{roi.width}x{roi.height}+{roi.x}+{roi.y}"]

        cmd.append(str(out_path))

        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT,
            )
            if r.returncode != 0:
                return None
            if out_path.exists():
                data = out_path.read_bytes()
                out_path.unlink(missing_ok=True)
                return data
        except Exception as e:
            logger.debug(f"import error: {e}")
        finally:
            out_path.unlink(missing_ok=True)
        return None


class PILBackend(_CaptureBackend):
    """
    Captura usando PIL ImageGrab (solo X11 / XWayland).
    Valida que la imagen no sea negra — XWayland en Wayland puede
    capturar correctamente pero devuelve negro si no hay permisos.
    """
    name = "pil_imagegrab"

    def is_available(self) -> bool:
        if not _PIL_AVAILABLE:
            return False
        try:
            from PIL import ImageGrab  # noqa
            return bool(os.environ.get("DISPLAY"))
        except Exception:
            return False

    @staticmethod
    def _is_black(data: bytes, threshold: float = 5.0) -> bool:
        if not _NUMPY_AVAILABLE:
            return False
        try:
            import numpy as np
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(data))
            return float(np.array(img).mean()) < threshold
        except Exception:
            return False

    def capture(
        self,
        monitor: Optional[MonitorInfo] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[bytes]:
        if not _PIL_AVAILABLE:
            return None
        try:
            from PIL import ImageGrab
            bbox = None
            if roi and roi.is_valid:
                bbox = (roi.x, roi.y, roi.x + roi.width, roi.y + roi.height)
            img = ImageGrab.grab(bbox=bbox)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
            if self._is_black(data):
                logger.debug("PILBackend: imagen negra (Wayland sin soporte), descartando")
                return None
            return data
        except Exception as e:
            logger.debug(f"PIL ImageGrab error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════
# IMAGE PROCESSOR
# ═══════════════════════════════════════════════════════════════

class ImageProcessor:
    """Comprime y escala imágenes para el VisionAgent (qwen3-vl:4b)."""

    def process(
        self,
        raw_bytes: bytes,
        native_w: int = 0,
        native_h: int = 0,
    ) -> Tuple[str, int, int, int]:
        """
        Recibe bytes PNG/JPEG, retorna (base64_jpeg, width, height, byte_size).

        v1.0.4: Auto-detect resolución nativa del monitor.
          - native_w/h: resolución real del monitor detectada por capture_sync().
          - settings.vision_capture_max_width/height actúan como override
            explícito solo si son MENORES a la resolución nativa.
          - Con native=0 y settings default=3840: nunca resize en monitores ≤4K.
        """
        if not _PIL_AVAILABLE:
            b64 = base64.b64encode(raw_bytes).decode()
            return b64, 0, 0, len(raw_bytes)

        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size

        # Resolución efectiva máxima:
        # - native_w/h del monitor detectado es el techo real
        # - settings solo puede bajar (override explícito), nunca subir
        # - fallback 3840x2160 → sin resize en monitores ≤4K
        cfg_w = getattr(settings, 'vision_capture_max_width',  3840)
        cfg_h = getattr(settings, 'vision_capture_max_height', 2160)

        if native_w > 0 and native_h > 0:
            max_w = native_w if cfg_w >= native_w else cfg_w
            max_h = native_h if cfg_h >= native_h else cfg_h
        else:
            max_w, max_h = cfg_w, cfg_h

        if w > max_w or h > max_h:
            ratio = min(max_w / w, max_h / h)
            new_w, new_h = int(w * ratio), int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            w, h = new_w, new_h
            logger.debug(f"[ImageProcessor] resize → {w}x{h} (native={native_w}x{native_h} cfg={cfg_w}x{cfg_h})")

        quality   = getattr(settings, 'vision_jpeg_quality', 82)
        max_bytes = 512 * 1024

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)

        while buf.tell() > max_bytes and quality > 50:
            quality -= 10
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)

        raw = buf.getvalue()
        b64 = base64.b64encode(raw).decode()
        logger.debug(f"[ImageProcessor] {w}x{h} q={quality} size={len(raw)}B")
        return b64, w, h, len(raw)


# ═══════════════════════════════════════════════════════════════
# SCREEN CAPTURE ENGINE
# ═══════════════════════════════════════════════════════════════

class ScreenCaptureEngine:
    """
    Motor principal de captura de pantalla.

    Uso básico:
        engine = ScreenCaptureEngine()
        frame = await engine.capture_async()
        # frame es CaptureFrame con image_b64 listo para VisionAgent
    """

    def __init__(self) -> None:
        self._monitor_detector = MonitorDetector()
        self._context_detector = ContextHintDetector()
        self._processor = ImageProcessor()
        self._backends: List[_CaptureBackend] = [
            GrimBackend(),       # Wayland wlroots (Sway, Hyprland)
            SpectacleBackend(),  # KDE Plasma Wayland (KWin nativo)
            ScrotBackend(),      # X11
            ImportBackend(),     # X11 ImageMagick
            PILBackend(),        # X11 / XWayland último recurso
        ]
        self._active_backend: Optional[_CaptureBackend] = None
        self._queue_active = False
        self._queue_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        logger.info("ScreenCaptureEngine inicializado (v1.0.4)")

    # ── Backend selection ────────────────────────────────────────────────────

    def _get_backend(self) -> Optional[_CaptureBackend]:
        if self._active_backend and self._active_backend.is_available():
            return self._active_backend
        for backend in self._backends:
            if backend.is_available():
                self._active_backend = backend
                logger.info(f"ScreenCapture backend seleccionado: {backend.name}")
                return backend
        logger.error("ScreenCaptureEngine: ningún backend disponible")
        return None

    def get_available_backends(self) -> List[str]:
        return [b.name for b in self._backends if b.is_available()]

    # ── Monitor detection ────────────────────────────────────────────────────

    def get_monitors(self) -> List[MonitorInfo]:
        return self._monitor_detector.get_monitors()

    def get_monitor_by_name(self, name: str) -> Optional[MonitorInfo]:
        for m in self.get_monitors():
            if m.name == name:
                return m
        return None

    # ── Capture (sync) ──────────────────────────────────────────────────────

    def capture_sync(
        self,
        monitor_name: Optional[str] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[CaptureFrame]:
        """
        Captura sincrónica. Usar desde threads (asyncio.to_thread).
        """
        backend = self._get_backend()
        if not backend:
            return None

        # Resolver monitor
        monitor: Optional[MonitorInfo] = None
        if monitor_name:
            monitor = self.get_monitor_by_name(monitor_name)
        if monitor is None:
            monitor = self._monitor_detector.get_primary()

        # Detectar ventana activa + context_hint
        active_window = self._context_detector.get_active_window_name()
        context_hint  = self._context_detector.detect(active_window)

        t0 = time.time()
        raw = backend.capture(monitor=monitor, roi=roi)
        if raw is None:
            # Intentar el siguiente backend disponible en cascada
            for fallback in self._backends:
                if fallback is backend or not fallback.is_available():
                    continue
                logger.debug(f"Fallback a backend: {fallback.name}")
                raw = fallback.capture(monitor=monitor, roi=roi)
                if raw is not None:
                    backend = fallback
                    break

        if raw is None:
            logger.error("ScreenCapture: todos los backends fallaron")
            return None

        capture_ms = (time.time() - t0) * 1000
        logger.debug(f"Captura OK [{backend.name}] {capture_ms:.0f}ms raw={len(raw)}B")

        native_w = monitor.width  if monitor else 0
        native_h = monitor.height if monitor else 0
        b64, w, h, byte_size = self._processor.process(raw, native_w=native_w, native_h=native_h)

        return CaptureFrame(
            image_b64=b64,
            width=w,
            height=h,
            timestamp=time.time(),
            monitor=monitor.name if monitor else "primary",
            roi=roi,
            context_hint=context_hint,
            active_window=active_window,
            backend_used=backend.name,
            byte_size=byte_size,
        )

    # ── Capture (async) ─────────────────────────────────────────────────────

    async def capture_async(
        self,
        monitor_name: Optional[str] = None,
        roi: Optional[ROI] = None,
    ) -> Optional[CaptureFrame]:
        """
        Captura asíncrona. Ejecuta en asyncio.to_thread para no bloquear el event loop.
        Usar desde dispatcher handlers.
        """
        return await asyncio.to_thread(self.capture_sync, monitor_name, roi)

    # ── Queue mode (para triggers futuros: voz, hotkey) ─────────────────────

    def start_queue_mode(
        self,
        queue: asyncio.Queue,
        interval_seconds: float = 0.0,  # 0 = one-shot por trigger, >0 = polling
        monitor_name: Optional[str] = None,
    ) -> None:
        """
        Publica frames en `queue` cada `interval_seconds`.
        Si interval_seconds == 0, captura una vez y se detiene.
        """
        if self._queue_active:
            logger.warning("ScreenCaptureEngine: queue_mode ya activo")
            return

        self._stop_event.clear()
        self._queue_active = True

        def _worker():
            loop = asyncio.new_event_loop()
            try:
                while not self._stop_event.is_set():
                    frame = self.capture_sync(monitor_name=monitor_name)
                    if frame:
                        try:
                            loop.run_until_complete(
                                asyncio.wait_for(queue.put(frame), timeout=2.0)
                            )
                        except asyncio.QueueFull:
                            logger.debug("ScreenCapture queue llena — frame descartado")
                        except Exception as e:
                            logger.debug(f"Queue put error: {e}")
                    if interval_seconds <= 0:
                        break
                    self._stop_event.wait(timeout=interval_seconds)
            finally:
                loop.close()
                self._queue_active = False

        self._queue_thread = threading.Thread(
            target=_worker,
            name="axioma-screen-capture",
            daemon=True,
        )
        self._queue_thread.start()
        logger.info(f"ScreenCaptureEngine queue_mode iniciado (interval={interval_seconds}s)")

    def stop_queue_mode(self) -> None:
        self._stop_event.set()
        if self._queue_thread and self._queue_thread.is_alive():
            self._queue_thread.join(timeout=3)
        self._queue_active = False
        logger.info("ScreenCaptureEngine queue_mode detenido")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "active_backend":     self._active_backend.name if self._active_backend else None,
            "available_backends": self.get_available_backends(),
            "queue_active":       self._queue_active,
            "monitors":           [
                {"name": m.name, "res": f"{m.width}x{m.height}", "primary": m.is_primary}
                for m in self.get_monitors()
            ],
        }


# ═══════════════════════════════════════════════════════════════
# SINGLETON FACTORY
# ═══════════════════════════════════════════════════════════════

_engine_instance: Optional[ScreenCaptureEngine] = None
_engine_lock = threading.Lock()


def get_screen_capture_engine() -> ScreenCaptureEngine:
    """Singleton thread-safe del ScreenCaptureEngine."""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                _engine_instance = ScreenCaptureEngine()
    return _engine_instance
