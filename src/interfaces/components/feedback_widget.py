#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — FeedbackWidget: feedback explícito enriquecido (P0 del plan)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/components/feedback_widget.py
# Origen: extraído de `chat.py` (ya superaba las 1000 líneas → AUD-PERF) con el
#         esquema 50/50: `chat.py` RE-EXPORTA `FeedbackWidget`, así que los
#         consumidores (`from .chat import FeedbackWidget`) no cambian.
#
# ✅ v0.6.9v (P0 del plan nuevo): antes eran dos botones 👍/👎 que **solo**
# escribían un trace en `detailed_logger` (más un `feedback_callback` opcional
# que nadie pasaba): el feedback del usuario **nunca llegaba** a
# `FeedbackLoop.record_explicit()` — que ya existía con rating 1-5, comentario y
# tags y tenía **0 llamadas** en todo el proyecto. El ciclo
# feedback → QualityMonitor → PromptOptimizer estaba cortado en la UI.
#
# El handler es ASYNC (NiceGUI lo soporta): así `await record_explicit(...)` corre
# en el event loop de la UI, sin hilos ni `create_task` sueltos. Si falla, se
# degrada con `log_degraded` y **nunca** rompe la respuesta.
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from nicegui import ui

logger = logging.getLogger(__name__)

try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:  # pragma: no cover
    LOGGER_AVAILABLE = False

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass


def _safe_log(fn: Callable) -> None:
    """Ejecuta fn(log) solo si el logger está disponible e inicializado."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            fn(log)
    except Exception as _d2e:  # noqa: BLE001
        # ✅ R3 (AGENT_GUIDE): degradar NO es esconder — un error de CONTRATO
        # (TypeError/AttributeError/…) debe verse como WARNING. Con
        # `logger.debug` acá fallaban el detector `silent_contract_swallow` y el
        # test de regresión `test_no_migrado_reaparece` (los atrapó el gate).
        log_degraded(logger, _d2e, "feedback_widget._safe_log")


def _log_event(category: str, message: str, level: str = "INFO",
               extra: dict = None) -> None:
    _safe_log(lambda log: log.log_event(category, message, level=level, extra=extra))


# ═══════════════════════════════════════════════════════════════
class FeedbackWidget:
    """Feedback explícito enriquecido: rating 1-5 + comentario + tags.

    ✅ v0.6.9v (P0 del plan nuevo): antes eran dos botones 👍/👎 que **solo**
    escribían un trace en `detailed_logger` (más un `feedback_callback` opcional
    que **nadie pasaba**): el feedback del usuario **nunca llegaba** a
    `FeedbackLoop.record_explicit()` — que ya existía con rating 1-5, comentario
    y tags, y tenía **0 llamadas** en todo el proyecto. O sea: el ciclo
    feedback → QualityMonitor → PromptOptimizer estaba cortado en la UI.
    Ahora el handler es ASYNC (NiceGUI lo soporta) y llama al loop de verdad;
    si falla, se degrada con `log_degraded` y **nunca** rompe la respuesta.
    """

    # Tags de conjunto CERRADO: un vocabulario libre no se puede agregar.
    TAGS: Tuple[str, ...] = (
        "sin_fuentes", "datos_incorrectos", "codigo_roto", "muy_corto",
        "muy_largo", "lento", "buena",
    )

    def __init__(self, session_state: Optional[Any] = None,
                 response_metadata: Optional[Dict] = None,
                 feedback_callback: Optional[Callable] = None,
                 response_text: str = ""):
        self.session_state = session_state
        self.response_metadata = response_metadata or {}
        # El texto de la respuesta NO viaja en la metadata: se pasa aparte para
        # que `record_explicit` guarde la respuesta real y no una vacía.
        self.response_text = response_text or ""
        self.feedback_callback = feedback_callback
        self.elements: Dict[str, Any] = {}
        self.is_visible = False
        self.last_rating: Optional[int] = None

    def create(self) -> Dict[str, Any]:
        elements: Dict[str, Any] = {}
        with ui.card().classes('w-full mt-2 p-3 bg-gray-50') as card:
            ui.label('Calificá esta respuesta (1 = mala, 5 = excelente)'
                     ).classes('text-xs font-semibold mb-1')
            with ui.row().classes('items-center gap-1'):
                rating_btn = ui.toggle({1: '1', 2: '2', 3: '3', 4: '4', 5: '5'})
                rating_btn.props('dense unelevated')
                send = ui.button('Enviar', on_click=self._submit_feedback).props(
                    'flat dense color=primary')
            comment = ui.textarea(
                placeholder='Comentario opcional (qué falló, qué mejorar...)'
            ).classes('w-full').props('outlined rows=2 dense')
            tags = ui.select(list(self.TAGS), multiple=True,
                             label='Etiquetas (opcional)').classes(
                'w-full').props('outlined dense use-chips')
            elements.update({"card": card, "rating": rating_btn, "comment": comment,
                             "tags": tags, "send": send})
            # Compatibilidad con el flujo viejo 👍/👎 (algunos tests/callers lo
            # usan): siguen existiendo pero ya NO son la UI principal.
            with ui.row().classes('items-center gap-2'):
                up = ui.button('👍', on_click=lambda: self._set_rating(4)).props(
                    'flat dense size=sm')
                down = ui.button('👎', on_click=lambda: self._set_rating(2)).props(
                    'flat dense size=sm')
                elements.update({"thumbs_up": up, "thumbs_down": down})

        self.elements = elements
        card.style('display: none;')
        return elements

    def _set_rating(self, value: int) -> None:
        """Fija el rating desde los botones 👍/👎 (atajo del toggle 1-5)."""
        if rating := self.elements.get('rating'):
            rating.value = value

    def show(self) -> None:
        if card := self.elements.get('card'):
            card.style('display: block;')
        self.is_visible = True

    def hide(self) -> None:
        if card := self.elements.get('card'):
            card.style('display: none;')
        self.is_visible = False

    async def _submit_feedback(self) -> None:
        """Envía el rating al FeedbackLoop real (y conserva el trace viejo)."""
        rating = self.elements.get('rating')
        comment = self.elements.get('comment')
        tags = self.elements.get('tags')
        value = getattr(rating, 'value', None)
        if value is None:
            ui.notify('⚠️ Elegí una calificación de 1 a 5', color='warning',
                      timeout=2)
            return
        try:
            value = int(value)
        except (TypeError, ValueError):
            ui.notify('⚠️ Calificación inválida', color='warning', timeout=2)
            return

        comment_text = (comment.value or "") if comment else ""
        tag_list = list(tags.value or []) if tags else []
        self.last_rating = value
        sent = await self._record_to_loop(value, comment_text, tag_list)
        self._log_trace(value >= 4, comment_text)

        if self.feedback_callback:
            try:
                self.feedback_callback(value >= 4, comment_text)
            except Exception as e:
                logger.warning(f"Feedback callback error: {e}")

        if sent:
            ui.notify('✅ ¡Gracias! Tu calificación entrena al sistema',
                      color='positive', timeout=3)
        else:
            ui.notify('⚠️ No se pudo registrar la calificación (queda en el log)',
                      color='warning', timeout=3)
        self.hide()
        _log_event("FEEDBACK", f"Submitted rating={value} tags={len(tag_list)}")

    async def _record_to_loop(self, rating: int, comment: str,
                              tags: List[str]) -> bool:
        """Llama a `FeedbackLoop.record_explicit()`. True si se registró.

        Nunca propaga: el feedback es un extra, no puede romper la UI.
        """
        meta = self.response_metadata
        session_id = (self.session_state.session_id if self.session_state
                      else "anonymous")
        try:
            from src.core.feedback_loop import get_feedback_loop
            loop = await get_feedback_loop()
            if loop is None:
                return False
            await loop.record_explicit(
                session_id=session_id,
                task_type=meta.get('task_type', 'UNKNOWN'),
                agent_role=meta.get('agent_role') or 'WebDispatcher',
                query=meta.get('request', ''),
                response=self.response_text,
                rating=rating,
                comment=comment or None,
                tags=tags or None,
                latency_ms=float(meta.get('latency_ms') or 0.0),
                metadata={
                    'model_used': meta.get('model_used', 'unknown'),
                    'confidence': meta.get('confidence'),
                    'validation_state': meta.get('validation_state'),
                    'source_count': len(meta.get('sources') or []),
                },
            )
            return True
        except Exception as e:  # noqa: BLE001 — el feedback nunca rompe la UI
            log_degraded(logger, e, "FeedbackWidget.record_explicit")
            return False

    def _log_trace(self, was_helpful: bool, feedback_text: str) -> None:
        """Trace histórico en detailed_logger (comportamiento previo, se conserva)."""
        meta = self.response_metadata
        session_id = (self.session_state.session_id if self.session_state
                      else "anonymous")
        _safe_log(lambda log: log.log_feedback(
            user_id=f"user_{session_id[:8]}",
            request=meta.get('request', ''),
            response_id=f"resp_{int(time.time() * 1000)}",
            was_helpful=was_helpful,
            feedback_text=feedback_text,
            ambiguity_level=meta.get('ambiguity_level', 'none'),
            task_type=meta.get('task_type', 'UNKNOWN'),
            model_used=meta.get('model_used', 'unknown'),
            latency_ms=meta.get('latency_ms', 0.0),
            session_id=session_id,
        ))


# ═══════════════════════════════════════════════════════════════
# CLASE: ChatComponent
# ═══════════════════════════════════════════════════════════════
