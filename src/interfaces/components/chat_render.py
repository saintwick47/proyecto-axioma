#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — chat_render.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/interfaces/components/chat_render.py
#
# ✅ v0.6.9v (REFACTOR): capa de RENDER + STREAMING de mensajes extraída de
#   src/interfaces/components/chat.py (el original queda intacto).
#
#   Qué se movió desde chat.py (sin cambios de comportamiento):
#     - `_render_sources_block`
#     - `_render_assistant_message`
#     - `_render_code_artifact`
#     - `add_streaming_placeholder` / `update_streaming_content`
#     - `finalize_streaming_message` / `remove_streaming_placeholder`
#
#   `ChatComponent` (en chat.py) hereda ahora de `ChatRenderMixin`, así la
#   API pública (add_message, save_code_to_file, etc.) no cambia.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

# ✅ Fase 5: reusa el writer de archivos ya probado de app_logic.py.
from src.interfaces.web.app_logic import save_code_to_file

logger = logging.getLogger(__name__)

# Helper de logging degradado (espejo del usado en chat.py).
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


def _safe_log(fn):
    """Ejecuta fn(log) solo si el logger está disponible e inicializado."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            fn(log)
    except Exception as _d2e:
        logger.debug(f"[D2 chat_render.py] excepción degradada (intencional/silenciosa): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None):
    _safe_log(lambda log: log.log_error(error, context=context, extra_info=extra))


class ChatRenderMixin:
    """Renderización y streaming de mensajes del chat (NiceGUI).

    Extraído de ChatComponent para dividir responsabilidades (50/50):
    el componente original se ocupa de estado/upload/UI, este mixin solo
    dibuja los mensajes y maneja el placeholder de streaming.
    """

    # ── Render de fuentes ────────────────────────────────────────────────────
    def _render_sources_block(self, sources: List[str]) -> None:
        """Muestra las fuentes (archivos/URLs) que respaldan la respuesta."""
        try:
            with ui.row().classes('w-full justify-start'):
                with ui.card().classes('bg-gray-50 w-full max-w-3xl p-2'):
                    ui.label(f'📎 Fuentes ({len(sources)})').classes(
                        'text-xs font-semibold text-gray-500')
                    for src in sources:
                        with ui.row().classes('items-center gap-2 w-full'):
                            ui.icon('description').classes(
                                'text-gray-400 text-sm')
                            ui.label(src).classes(
                                'text-xs font-mono text-gray-600'
                            ).tooltip(src)
        except Exception as e:
            _log_error(e, "ChatRenderMixin._render_sources_block",
                       extra={"n": len(sources)})

    # ── Render de mensajes del asistente ─────────────────────────────────────
    def _render_assistant_message(
        self,
        content: str,
        bg: str,
        response_metadata: Optional[Dict],
    ) -> None:
        # ✅ Fase 5: cuando la respuesta trae code_artifact (contrato
        # verificado, Fase 1-4) se usa ESO como fuente de verdad — antes
        # todo se re-derivaba de `content` vía FILE_PATTERN/CODE_PATTERN,
        # que solo captura el PRIMER "ARCHIVO:" (search(), no findall())
        # y perdía por completo los demás archivos en una respuesta
        # multi-archivo, además de no mostrar nunca si el código estaba
        # realmente validado.
        code_artifact = (response_metadata or {}).get('code_artifact')
        if code_artifact and code_artifact.get('files'):
            self._render_code_artifact(
                code_artifact,
                (response_metadata or {}).get('validation_report'),
                bg,
            )
            return

        filename = None
        file_match = self.FILE_PATTERN.search(content)
        if file_match:
            filename = file_match.group(1)
        elif response_metadata and response_metadata.get('suggested_filename'):
            filename = response_metadata['suggested_filename']

        content_clean = self.FILE_PATTERN.sub('', content).strip()

        if not self.CODE_PATTERN.search(content_clean):
            ui.chat_message(content_clean, name='AXIOMA', stamp='📘').classes(bg)
            return

        parts = self.BLOCK_SPLIT_PATTERN.split(content_clean)

        with ui.card().classes(f'{bg} w-full max-w-4xl p-4'):
            if filename:
                ui.label(f'📄 {filename}').classes('text-sm font-mono font-semibold mb-2')

            for part in parts:
                part = part.strip()
                if not part:
                    continue

                block_match = re.match(r'^```(\w+)?\n(.*?)```$', part, re.DOTALL)
                if block_match:
                    lang = block_match.group(1) or 'text'
                    code = block_match.group(2).strip()
                    ui.code(code, language=lang).classes('w-full').props('copy')
                else:
                    ui.markdown(part).classes('text-sm')

    def _render_code_artifact(
        self,
        code_artifact: Dict[str, Any],
        validation_report: Optional[Dict[str, Any]],
        bg: str,
    ) -> None:
        """
        ✅ Fase 5: un ui.code() con copy nativo por archivo (código y
        tests, no solo el primero) + botón de descarga real (chat.py no
        tenía ninguno — el ✅copy de ui.code() no baja el archivo a
        disco) + el resultado REAL de validación (nunca dice "validado"
        si validation_report.passed es False).
        """
        with ui.card().classes(f'{bg} w-full max-w-4xl p-4'):
            if validation_report is not None:
                if validation_report.get('passed'):
                    ui.label('✅ Validado — compila y los tests pasan').classes(
                        'text-sm text-positive font-medium mb-2'
                    )
                else:
                    failed = [l['layer'] for l in validation_report.get('layers', []) if not l.get('passed')]
                    ui.label(
                        f"⚠️ Sin validar del todo — falló: {', '.join(failed) or 'validación'}"
                    ).classes('text-sm text-negative font-medium mb-2')

            all_files = [
                *code_artifact.get('files', []),
                *code_artifact.get('test_files', []),
            ]
            for f in all_files:
                name = f.get('name', 'archivo.py')
                code = f.get('content', '')
                lang = f.get('language', 'python')

                with ui.row().classes('w-full items-center justify-between mt-2'):
                    ui.label(f'📄 {name}').classes('text-sm font-mono font-semibold')

                    def _make_download(filename: str, file_code: str) -> Callable[[], None]:
                        # Factory para evitar el late-binding clásico de
                        # closures en loop — sin esto, los N botones
                        # terminan todos descargando el último archivo.
                        def _on_download() -> None:
                            filepath = save_code_to_file(file_code, filename)
                            ui.notify(f'✅ Guardado: {filepath}', color='positive', timeout=3)
                            ui.download(filepath)
                        return _on_download

                    ui.button('💾', on_click=_make_download(name, code)).props('dense flat size=sm')

                ui.code(code, language=lang).classes('w-full').props('copy')

            if code_artifact.get('dependencies'):
                ui.label('Dependencias: ' + ', '.join(code_artifact['dependencies'])).classes(
                    'text-xs text-gray-500 mt-2'
                )
            if code_artifact.get('warnings'):
                for w in code_artifact['warnings']:
                    ui.label(f'⚠️ {w}').classes('text-xs text-gray-500')

    # ── Streaming progresivo ─────────────────────────────────────────────────
    def add_streaming_placeholder(self) -> Any:
        messages_container = self.elements.get('messages')
        if not messages_container:
            return None
        with messages_container:
            with ui.row().classes('w-full justify-start'):
                with ui.card().classes('bg-gray-100 w-full max-w-4xl p-4'):
                    stream_label = ui.label('▋').classes('text-sm whitespace-pre-wrap')
        return stream_label

    def update_streaming_content(self, stream_label: Any, text: str) -> None:
        if stream_label is None:
            return
        try:
            stream_label.set_text(text + '▋')
        except Exception as _d2e:
            logger.debug(f"[D2 chat_render.py] excepción degradada (intencional/silenciosa): {_d2e}")

    def finalize_streaming_message(
        self,
        stream_label: Any,
        full_content: str,
        response_metadata: Optional[Dict] = None,
    ) -> None:
        if stream_label is None:
            return
        try:
            parent = stream_label.parent_slot.parent
            parent.clear()
            with parent:
                self._render_assistant_message(full_content, 'bg-gray-100', response_metadata)
            # ✅ v0.6.9v (P0 del plan nuevo): la ruta de STREAMING también debe
            # mostrar fuentes + transparencia + feedback. Antes los tres se
            # perdían acá (solo aparecían con `stream_chat_tokens=False`).
            if hasattr(self, '_render_message_extras'):
                self._render_message_extras(parent, full_content,
                                            response_metadata)
        except Exception:
            try:
                stream_label.set_text(full_content)
            except Exception as _d2e:
                logger.debug(f"[D2 chat_render.py] excepción degradada (intencional/silenciosa): {_d2e}")

    def remove_streaming_placeholder(self, stream_label: Any) -> None:
        """Quita el placeholder de streaming sin renderizar nada (v0.6.8ss A3)."""
        if stream_label is None:
            return
        try:
            parent = stream_label.parent_slot.parent
            parent.clear()
        except Exception as _d2e:
            logger.debug(f"[D2 chat_render.py remove_streaming_placeholder] excepción degradada: {_d2e}")
