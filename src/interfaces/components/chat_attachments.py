#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — chat_attachments.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/interfaces/components/chat_attachments.py
#
# ✅ v0.6.9w (REFACTOR): capa de ADJUNTOS (📎/📁, imagen→visión) extraída de
#   src/interfaces/components/chat.py (1172 líneas) para dejarlo por debajo de
#   las 1000 (regla del proyecto: cada archivo >1000 líneas se parte en DOS).
#
#   Qué se movió desde chat.py, SIN cambios de comportamiento:
#     · Constantes: `_IMAGE_EXTS`, `_IMAGE_MAGIC`, `MAX_FILE_CHARS`.
#     · Funciones de módulo: `_is_image_attachment`, `_detect_image_ext`,
#       `_looks_binary`, `_pdf_parece_escaneado`, `_ocr_scanned_pdf`.
#     · Helpers de logging compartidos por las dos mitades: `_safe_log`,
#       `_log_event`, `_log_error`. Viven acá para que la dependencia sea
#       UNIDIRECCIONAL (chat → chat_attachments): si quedaran en chat.py, este
#       módulo tendría que importarlos de vuelta y se crearía un ciclo.
#     · Métodos de `ChatComponent` → `ChatAttachmentsMixin`: `_handle_file_upload`,
#       `_process_attachment_bytes`, `attach_local_path`, `_clear_attachment`,
#       `_process_vision_message`.
#
#   Por qué la API pública NO cambia:
#     `ChatComponent` (en chat.py) hereda de `ChatAttachmentsMixin`, y chat.py
#     re-importa TODOS los nombres movidos con los MISMOS nombres. Así los
#     consumidores (app.py, tests) siguen pudiendo hacer
#     `from src.interfaces.components.chat import _is_image_attachment` o
#     `chat.MAX_FILE_CHARS`, e `isinstance(ChatComponent(...), ...)` no cambia.
#     Los 5 métodos siguen respondiendo igual en la instancia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Optional

from nicegui import events, ui

# ✅ Mismo helper de logging de chat.py (dueño original: tools.detailed_logger).
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def log_degraded(logger_, exc, context, **kwargs):
    """Espejo de `chat.log_degraded` (R3): el dueño único sigue siendo chat.py.

    El import es DIFERIDO a propósito: chat.py importa este módulo en su
    cabecera, así que un import module-level sería circular. Además los tests
    monkeypatchean `chat.log_degraded` para espiar esta ruta (test_axioma_18),
    o sea que el símbolo se LEE del dueño; no se duplica ni se redefine acá.
    """
    from . import chat as _chat_mod
    _chat_mod.log_degraded(logger_, exc, context, **kwargs)


# ── Adjuntos de imagen (v0.6.9p) — se envían a qwen3-vl (bajo demanda) ─────
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


def _is_image_attachment(filename: Optional[str]) -> bool:
    """True si la extensión corresponde a una imagen soportada (v0.6.9p)."""
    try:
        return Path(filename or "").suffix.lower() in _IMAGE_EXTS
    except Exception as e:
        log_degraded(logger, e, "src/interfaces/components/chat.py:_is_image_attachment")
        return False


# ✅ v0.6.9q: detección por CONTENIDO (magic bytes) — un upload sin nombre de
# archivo llegaba como "unknown.txt" y el PNG se volcaba como texto basura.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _detect_image_ext(data: bytes) -> Optional[str]:
    """Devuelve la extensión de imagen si `data` empieza con su firma."""
    for sig, ext in _IMAGE_MAGIC:
        if data.startswith(sig):
            return ext
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _looks_binary(text: str) -> bool:
    """True si el texto decodificado parece binario (muchos controles)."""
    sample = text[:16384]
    if not sample:
        return False
    ctrl = sum(1 for ch in sample
               if ord(ch) < 32 and ch not in "\n\r\t")
    return ctrl / len(sample) > 0.25


def _pdf_parece_escaneado(text: str, threshold: int = 60) -> bool:
    """Un PDF escaneado no tiene texto extraíble (solo imágenes)."""
    return len((text or "").strip()) < threshold


def _ocr_scanned_pdf(path: Any, max_pages: int = 3) -> Optional[str]:
    """Rasteriza páginas de un PDF escaneado (pdftoppm) y las lee con Tesseract.

    v0.6.9r (2): requiere poppler-utils (pdftoppm). Si no está, devuelve None
    (el adjunto queda con la extracción original). Mejor que volcar el PDF
    binario como texto basura.
    """
    import shutil
    import subprocess
    import tempfile
    if shutil.which("pdftoppm") is None:
        logger.info("[pdf_ocr] pdftoppm no disponible — OCR omitido")
        return None
    try:
        import base64 as _b64
        from src.interfaces.web.vision_ocr import ocr_image
        tmpdir = Path(tempfile.mkdtemp(prefix="axioma_pdf_ocr_"))
        try:
            # Rasterizar páginas a PNG
            subprocess.run(
                ["pdftoppm", "-png", "-r", "150",
                 "-f", "1", "-l", str(max_pages), str(path),
                 str(tmpdir / "page")],
                capture_output=True, timeout=60,
            )
            pages = sorted(tmpdir.glob("page-*.png"))
            if not pages:
                return None
            texts = []
            for p in pages:
                b64 = _b64.b64encode(p.read_bytes()).decode("ascii")
                ocr = ocr_image(b64)
                if ocr and ocr.text.strip():
                    texts.append(ocr.text.strip())
            return "\n\n".join(texts) if texts else None
        finally:
            try:
                import shutil as _sh
                _sh.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                # R3: limpieza de temporales — el OCR ya devolvió su resultado.
                log_degraded(logger, e, "chat._ocr_scanned_pdf (rmtree tmpdir)",
                             contract_level=logging.DEBUG)
    except Exception as exc:
        logger.warning(f"[pdf_ocr] falló: {exc}")
        return None


# ═══════════════════════════════════════════════════════════════
# Helpers de logging compartidos (movidos desde chat.py)
# ═══════════════════════════════════════════════════════════════


def _safe_log(fn):
    """Ejecuta fn(log) solo si el logger está disponible e inicializado."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            fn(log)
    except Exception as _d2e:
        logger.debug(f"[D2 chat.py:63] excepción degradada (intencional/silenciosa): {_d2e}")


def _log_event(category: str, message: str, level: str = "INFO", extra: dict = None):
    _safe_log(lambda log: log.log_event(category, message, level=level, extra=extra))


def _log_error(error: Exception, context: str, extra: dict = None):
    _safe_log(lambda log: log.log_error(error, context=context, extra_info=extra))


# ═══════════════════════════════════════════════════════════════
# LÍMITE DE ARCHIVOS ADJUNTOS
# ═══════════════════════════════════════════════════════════════
# ✅ 2026-09-01: 50K → 150K — archivos grandes (código, docs) se analizaban
# truncados a la mitad; con el nuevo extractor de texto (PDF/DOCX reales)
# 150K da margen cómodo sin saturar el contexto del LLM.
MAX_FILE_CHARS = 150000


# ═══════════════════════════════════════════════════════════════
# MIXIN: ChatAttachmentsMixin
# ═══════════════════════════════════════════════════════════════
class ChatAttachmentsMixin:
    """Adjuntos (📎/📁) e imagen→visión, extraídos de ChatComponent.

    `ChatComponent` hereda de este mixin, así que `self` es el componente
    completo: usa `self.elements`, `self.add_message`, `self.hide_welcome`,
    `self.set_processing` y el estado `_attached_*` tal como antes.
    """

    # ── Manejo de subida de archivos ────────────────────────────────
    async def _handle_file_upload(self, e: events.UploadEventArguments):
        """
        Maneja archivos subidos. Almacena el contenido internamente y
        muestra un indicador visual. El textarea queda libre para que
        el usuario escriba su instrucción. Al enviar, se combinan.
        """
        try:
            file_bytes = None
            filename = 'unknown.txt'
            original_size = 0

            if hasattr(e, 'name') and e.name:
                filename = e.name
            elif hasattr(e, 'file') and hasattr(e.file, 'filename') and e.file.filename:
                filename = e.file.filename

            # Estrategia 1: e.file (NiceGUI >= 1.4.36 / 2.x)
            if file_bytes is None and hasattr(e, 'file') and e.file is not None:
                try:
                    raw = e.file
                    if hasattr(raw, 'read'):
                        data = raw.read()
                        if asyncio.iscoroutine(data):
                            data = await data
                        if isinstance(data, str):
                            file_bytes = data.encode('utf-8')
                        elif isinstance(data, bytes):
                            file_bytes = data
                    elif hasattr(raw, 'file') and hasattr(raw.file, 'read'):
                        data = raw.file.read()
                        if asyncio.iscoroutine(data):
                            data = await data
                        if isinstance(data, str):
                            file_bytes = data.encode('utf-8')
                        elif isinstance(data, bytes):
                            file_bytes = data
                except Exception as read_err:
                    logger.warning(f"Estrategia 1 fallida: {read_err}")

            # Estrategia 2: e.content (NiceGUI < 1.4.36)
            if file_bytes is None and hasattr(e, 'content') and e.content is not None:
                content = e.content
                if isinstance(content, bytes):
                    file_bytes = content
                elif isinstance(content, str):
                    file_bytes = content.encode('utf-8')
                elif hasattr(content, 'read'):
                    try:
                        if hasattr(content, 'seek'):
                            content.seek(0)
                    except Exception as _d2e:
                        logger.debug(f"[D2 chat.py:436] excepción degradada (intencional/silenciosa): {_d2e}")
                    data = content.read()
                    if isinstance(data, str):
                        file_bytes = data.encode('utf-8')
                    elif isinstance(data, bytes):
                        file_bytes = data

            # Estrategia 3: e.contents
            if file_bytes is None and hasattr(e, 'contents') and e.contents is not None:
                contents = e.contents
                if isinstance(contents, bytes):
                    file_bytes = contents
                elif isinstance(contents, str):
                    file_bytes = contents.encode('utf-8')

            # Estrategia 4: e.path
            if file_bytes is None and hasattr(e, 'path') and e.path:
                try:
                    file_bytes = Path(e.path).read_bytes()
                except (OSError, IOError, TypeError) as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 chat.py:456] excepción degradada (intencional): {_d2e}")

            if not file_bytes:
                diag = {}
                if hasattr(e, 'file'):
                    diag['e.file'] = type(e.file).__name__
                    if hasattr(e.file, 'file'):
                        diag['e.file.file'] = type(e.file.file).__name__
                if hasattr(e, 'content'):
                    diag['e.content'] = type(e.content).__name__
                if hasattr(e, 'name'):
                    diag['e.name'] = type(e.name).__name__
                logger.error(f"UPLOAD FALLIDO: file_bytes vacío. Diagnóstico: {diag}")
                ui.notify(f"❌ Archivo vacío o no leído. Diagnóstico: {diag}", color='negative', timeout=10)
                return

            self._process_attachment_bytes(filename, file_bytes)

        except Exception as ex:
            logger.exception("Error procesando archivo subido")
            ui.notify(f"❌ Error leyendo archivo: {ex}", color='negative')

    def _process_attachment_bytes(self, filename: str, file_bytes: bytes) -> None:
        """Procesa bytes adjuntos (upload 📎 o explorador 📁) → estado + barra.

        Imágenes → base64 para qwen3-vl. PDF/DOCX → extracción de texto real
        (pypdf/python-docx). Resto → texto utf-8 con tope MAX_FILE_CHARS.
        """
        original_size = len(file_bytes)

        # ✅ v0.6.9q: si el nombre no tiene extensión de imagen pero el
        # CONTENIDO es una imagen (upload sin e.name → "unknown.txt"), se
        # corrige el nombre para que entre al canal de visión, no al texto.
        if Path(filename).suffix.lower() not in _IMAGE_EXTS:
            _det = _detect_image_ext(file_bytes)
            if _det:
                filename = f"{Path(filename).stem}{_det}"

        # ✅ v0.6.9q: rechazar binarios que no son imagen (evita volcar bytes
        # basura al clasificador/LLM — causó misrouting a finance_query).
        if Path(filename).suffix.lower() not in _IMAGE_EXTS:
            _probe = file_bytes.decode('utf-8', errors='ignore')
            if _looks_binary(_probe):
                ui.notify(
                    f"⚠️ {filename} no es texto ni imagen soportada — no se "
                    f"adjuntó.", color='warning', timeout=8,
                )
                return

        # ✅ v0.6.9p: IMAGEN adjunta → se guarda en base64 para el VLM
        # (qwen3-vl bajo demanda). No se decodifica como texto.
        if Path(filename).suffix.lower() in _IMAGE_EXTS:
            self._attached_image_b64 = base64.b64encode(
                file_bytes).decode('ascii')
            self._attached_image_name = filename
            self._attached_file_name = None
            self._attached_file_content = None
            bar = self.elements.get('attachment_bar')
            label = self.elements.get('attachment_label')
            if bar and label:
                kb = max(1, original_size // 1024)
                label.set_text(f'🖼️ {filename} ({kb} KB)')
                bar.style('display: flex;')
            ui.notify(
                f"🖼️ {filename} adjuntado. Escribí tu instrucción "
                f"(o dejalo vacío para que la describa) y enviá.",
                color='info', timeout=6,
            )
            return

        # ✅ 2026-09-01: extracción de TEXTO REAL — PDF/DOCX se parsean
        # con pypdf/python-docx en vez de decodificar bytes crudos.
        if filename.lower().endswith(('.pdf', '.docx', '.doc')):
            try:
                from src.utils.file_text import extract_file_text
                import tempfile
                with tempfile.NamedTemporaryFile(
                    suffix=Path(filename).suffix.lower(), delete=False,
                ) as tf:
                    tf.write(file_bytes)
                    tmp_path = tf.name
                try:
                    content = extract_file_text(Path(tmp_path), max_chars=MAX_FILE_CHARS)
                    # ✅ v0.6.9r (2): PDF escaneado (sin texto extraíble) → OCR
                    # rasterizando ppáginas con pdftoppm + Tesseract.
                    if _pdf_parece_escaneado(content) and \
                            Path(tmp_path).suffix.lower() == ".pdf":
                        from config.settings import settings as _st
                        if getattr(_st, "pdf_scan_ocr_enabled", True):
                            ocr_text = _ocr_scanned_pdf(
                                Path(tmp_path),
                                max_pages=getattr(_st, "pdf_scan_max_pages", 3))
                            if ocr_text:
                                content = ocr_text
                finally:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception as _d2e:
                        logger.debug(f"[D2 chat.py:493] excepción degradada (intencional/silenciosa): {_d2e}")
                if not content:
                    content = file_bytes.decode('utf-8', errors='ignore')
            except Exception as exc:
                logger.warning(f"extract_file_text fallback a crudo: {exc}")
                content = file_bytes.decode('utf-8', errors='ignore')
        else:
            content = file_bytes.decode('utf-8', errors='ignore')

        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + (
                f"\n\n⚠️ [ARCHIVO TRUNCADO: se muestran {MAX_FILE_CHARS} de "
                f"~{original_size} caracteres. Para archivos más grandes, "
                f"especificá qué sección querés analizar.]"
            )

        self._attached_file_name = filename
        self._attached_file_content = content

        bar = self.elements.get('attachment_bar')
        label = self.elements.get('attachment_label')
        if bar and label:
            display_chars = len(content)
            if len(content) == MAX_FILE_CHARS and original_size > MAX_FILE_CHARS:
                label.set_text(f'📎 {filename} ({display_chars}/{original_size} chars — truncado)')
            else:
                label.set_text(f'📎 {filename} ({display_chars} chars)')
            bar.style('display: flex;')

        if original_size > MAX_FILE_CHARS:
            ui.notify(
                f"⚠️ {filename} truncado a {MAX_FILE_CHARS} chars de {original_size}. "
                f"Especificá qué sección analizar.",
                color='warning', timeout=8,
            )
        else:
            ui.notify(f"📎 {filename} adjuntado ({original_size} chars). Escribí tu instrucción y enviá.", color='info', timeout=5)

    def attach_local_path(self, path: str) -> bool:
        """Adjunta un archivo del servidor (explorador 📁) como si se hubiera
        subido con 📎. Devuelve True si quedó adjuntado.
        """
        try:
            p = Path(path)
            if not p.is_file():
                ui.notify(f"❌ No es un archivo: {path}", color='negative')
                return False
            file_bytes = p.read_bytes()
        except Exception as ex:
            logger.exception(f"attach_local_path falló: {path}")
            ui.notify(f"❌ No pude leer {path}: {ex}", color='negative')
            return False
        try:
            self._process_attachment_bytes(p.name, file_bytes)
            return True
        except Exception as ex:
            logger.exception(f"attach_local_path process falló: {path}")
            ui.notify(f"❌ No pude adjuntar {p.name}: {ex}", color='negative')
            return False

    def _clear_attachment(self) -> None:
        """Quita el archivo adjunto."""
        self._attached_file_name = None
        self._attached_file_content = None
        self._attached_image_b64 = None
        self._attached_image_name = None

        bar = self.elements.get('attachment_bar')
        if bar:
            bar.style('display: none;')

    # ── Imagen → visión (v0.6.9p) ──────────────────────────────────────────
    async def _process_vision_message(self, instruction: str) -> None:
        """Responde a una imagen adjunta con qwen3-vl (bajo demanda).

        Se ejecuta en un worker thread para no bloquear el loop de NiceGUI
        (la generación VLM en CPU tarda decenas de segundos).
        """
        b64 = self._attached_image_b64 or ''
        name = self._attached_image_name or 'imagen'
        self._clear_attachment()

        user_text = instruction.strip()
        bubble = f"{user_text}\n\n🖼️ `{name}`" if user_text else f"🖼️ `{name}`"
        self.add_message('user', bubble)
        self.hide_welcome()

        self.set_processing(True, '🖼️ Analizando imagen...')
        reply = ''
        try:
            from src.interfaces.web.vision_service import describe_image
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                None, describe_image, b64, user_text)
        except Exception as e:
            logger.exception("VISION: fallo analizando imagen")
            reply = (f"❌ No pude analizar la imagen "
                     f"({type(e).__name__}). Detalle en logs (VISION_CHAT).")
        finally:
            self.set_processing(False)

        if reply.strip():
            self.add_message('assistant', reply)
            if self.state is not None:
                try:
                    self.state.add_message('assistant', reply)
                except Exception as _state_err:
                    logger.debug(
                        f"[vision] estado no actualizado: {_state_err}")
