#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# rafael_daemon.py - Orquestador standalone Rafael (voz + proactivo + widget)
# Ruta: src/core/rafael_daemon.py
# Autor: SaintWick
# Versión: 1.1.1
#
# Orquesta el modo Jarvis con voz full-duplex, detección de wake word,
# motor proactivo y widget de diálogo por texto. Todo en un único event loop
# asyncio sin threads adicionales. Integra VoicePipeline, ProactiveEngine,
# Dispatcher y comunicación IPC con el widget.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from config.settings import settings
from config.multimodal_loader import build_stt_config, build_vad_config, build_tts_config, get_input_device
from config.paths import Paths
from src.core.dispatcher import Dispatcher
from src.core.proactive_engine import ProactiveEngine
from src.core.state_emitter import emit_state
from src.multimodal.pipeline_core import _classify_jarvis_intent
from src.multimodal.stt_engine import STTEngine
from src.multimodal.tts_engine import TTSEngine
from src.multimodal.vad_filter import VADFilter
from src.multimodal.voice_pipeline import VoicePipeline
from src.multimodal.wake_word import WakeWordDetector

try:
    from src.core.context_sensor import get_sensor as _get_context_sensor
    _CONTEXT_SENSOR_AVAILABLE = True
except ImportError:
    _get_context_sensor = None  # type: ignore[assignment]
    _CONTEXT_SENSOR_AVAILABLE = False

try:
    from tools.detailed_logger import initialize_logger as _initialize_detailed_logger
    from tools.detailed_logger import close_logger as _close_detailed_logger
    _DETAILED_LOGGER_AVAILABLE = True
except ImportError:
    _initialize_detailed_logger = None  # type: ignore[assignment]
    _close_detailed_logger = None  # type: ignore[assignment]
    _DETAILED_LOGGER_AVAILABLE = False

logger = logging.getLogger("rafael")

_VALID_MODES = frozenset({"jarvis", "voice_only", "silent"})

_WIDGET_SCRIPT = Paths.SRC / "interfaces" / "components" / "rafael_widget.py"

_IPC_FILES = (
    Path("/tmp/.axioma_widget_state"),
    Path("/tmp/.axioma_rafael_dialog_in"),
    Path("/tmp/.axioma_rafael_dialog_out"),
)
_DIALOG_IN_FILE = _IPC_FILES[1]
_DIALOG_OUT_FILE = _IPC_FILES[2]
_DIALOG_POLL_INTERVAL = 0.25
_DIALOG_DISPATCH_TIMEOUT = 220.0

_WIDGET_RETRY_DELAYS = (1.0, 2.0, 4.0)
_WIDGET_MAX_RETRIES = 3


def setup_logging(level: int = logging.INFO) -> None:
    """Logging del daemon a archivo — Paths.LOGS ya es el path centralizado del proyecto."""
    Paths.ensure_exists(Paths.LOGS)
    log_file = Paths.LOGS / "rafael.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    if _DETAILED_LOGGER_AVAILABLE:
        ok = _initialize_detailed_logger()
        if ok:
            logger.info("[RafaelDaemon] detailed_logger inicializado (logs/reg_error.txt)")
        else:
            logger.warning("[RafaelDaemon] detailed_logger no pudo inicializarse")
    else:
        logger.warning("[RafaelDaemon] tools.detailed_logger no disponible (ImportError)")


def _cleanup_ipc_files() -> None:
    for f in _IPC_FILES:
        try:
            f.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[RafaelDaemon] No se pudo limpiar IPC {f}: {e}")


class RafaelDaemon:
    """Orquestador standalone de Rafael — instancia único Dispatcher, VoicePipeline y ProactiveEngine."""

    def __init__(self, mode: str = "jarvis") -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"Modo inválido: '{mode}'. Válidos: {sorted(_VALID_MODES)}")

        self._mode = mode
        self._shutdown_event = asyncio.Event()
        self._widget_proc: Optional[asyncio.subprocess.Process] = None

        self._dispatcher = Dispatcher()
        self._voice_active_event = threading.Event()

        # ✅ plan_rafael (2026-08-31): TTS con config real desde
        # multimodal.yaml (piper_model offline activado). Antes TTSEngine() sin
        # argumentos → piper_model="" → piper nunca se cargaba.
        self._tts = TTSEngine(build_tts_config())
        # ✅ FIX v1.1.1: config real desde multimodal.yaml (antes: sin
        # argumentos, ignoraba el yaml por completo — ver
        # config/multimodal_loader.py)
        self._stt = STTEngine(build_stt_config())
        self._vad = VADFilter(build_vad_config())
        # ✅ ISSUE-126: la sensibilidad es ajustable desde `.env` (la detección es por
        # energía: sin modelo ML, 0.5 dispara con cualquier ruido fuerte).
        from config.settings import settings as _settings
        self._wake = WakeWordDetector(device=get_input_device(),
                                      sensitivity=getattr(_settings, "wake_sensitivity", 0.5))

        self._voice_pipeline = VoicePipeline(
            dispatcher=self._dispatcher,
            tts=self._tts,
            stt=self._stt,
            vad=self._vad,
            wake_detector=self._wake,
            voice_active_event=self._voice_active_event,
        )
        self._voice_pipeline.set_jarvis_mode(mode)

        self._proactive_engine = ProactiveEngine(
            dispatcher=self._dispatcher,
            tts=self._tts,
            user_profile=getattr(self._dispatcher, "_user_profile", None),
            context_sensor=(_get_context_sensor() if _CONTEXT_SENSOR_AVAILABLE else None),
            voice_active_event=self._voice_active_event,
        )

        logger.info(f"[RafaelDaemon] Inicializado — modo={mode}")

    # ── Widget supervisor ────────────────────────────────────────────────────

    async def _widget_supervisor(self) -> None:
        """
        Lanza rafael_widget.py como subprocess vía asyncio.create_subprocess_exec.
        Cierre limpio (code=0, ventana cerrada por el usuario) → no se reintenta.
        Crash (code!=0) → máx. 3 reintentos con backoff exponencial (1s, 2s, 4s).
        """
        if not _WIDGET_SCRIPT.exists():
            logger.warning(
                f"[RafaelDaemon] Widget script no encontrado en {_WIDGET_SCRIPT} — "
                "supervisor desactivado"
            )
            return

        retries = 0
        while not self._shutdown_event.is_set():
            try:
                self._widget_proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(_WIDGET_SCRIPT),
                    cwd=str(Paths.ROOT),
                )
                logger.info(f"[RafaelDaemon] Widget lanzado — pid={self._widget_proc.pid}")
                returncode = await self._widget_proc.wait()
            except Exception as e:
                logger.error(f"[RafaelDaemon] Error lanzando widget: {e}", exc_info=True)
                returncode = -1

            if self._shutdown_event.is_set():
                return

            if returncode == 0:
                # ✅ plan_rafael (2026-08-31): el widget es la interfaz de
                # Rafael. Si el usuario la CERRÓ (exit 0 — por el ✕, Alt+F4 o
                # el WM), Rafael se apaga COMPLETO (antes solo se cerraba el
                # widget y el daemon de voz seguía activo).
                logger.info("[RafaelDaemon] Widget cerrado por el usuario — apagando daemon")
                import os
                import signal
                os.kill(os.getpid(), signal.SIGTERM)
                return

            retries += 1
            logger.warning(f"[RafaelDaemon] Widget terminó (code={returncode})")
            if retries > _WIDGET_MAX_RETRIES:
                logger.error("[RafaelDaemon] Widget superó reintentos máximos — supervisión abandonada")
                return
            delay = _WIDGET_RETRY_DELAYS[retries - 1]
            logger.warning(f"[RafaelDaemon] Reintento widget {retries}/{_WIDGET_MAX_RETRIES} en {delay}s")
            await asyncio.sleep(delay)

    async def _stop_widget(self) -> None:
        if self._widget_proc is None or self._widget_proc.returncode is not None:
            return
        self._widget_proc.terminate()
        try:
            await asyncio.wait_for(self._widget_proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._widget_proc.kill()
            await self._widget_proc.wait()

    # ── Diálogo por texto (widget) ───────────────────────────────────────────

    async def _dialog_poll_loop(self) -> None:
        """
        Polling async de /tmp/.axioma_rafael_dialog_in — mismo event loop que
        voz/proactive, sin threading.Thread. Dedupe por id.
        """
        last_id: Optional[str] = None
        while not self._shutdown_event.is_set():
            try:
                if _DIALOG_IN_FILE.exists():
                    data = json.loads(_DIALOG_IN_FILE.read_text(encoding="utf-8"))
                    msg_id = data.get("id")
                    text = data.get("text", "")
                    if msg_id and msg_id != last_id and text:
                        last_id = msg_id
                        await self._process_dialog_message(msg_id, text)
            except Exception as e:
                logger.warning(f"[RafaelDaemon] dialog_poll_loop error: {e}", exc_info=True)
            await asyncio.sleep(_DIALOG_POLL_INTERVAL)

    async def _process_dialog_message(self, msg_id: str, text: str) -> None:
        """
        Misma rama de clasificación que la voz: bypass directo solo para
        screen_describe/camera_describe/autonomous_task. jarvis_query va al
        pipeline completo (dispatcher.process(), IntentClassifier real) —
        si no, cualquier búsqueda/código/clima caía en LLM directo sin tools.
        Sin TTS — responde solo en el panel del widget.
        """
        # ✅ plan_rafael (2026-08-31): comando de cierre desde el widget
        # (botón derecho → "Cerrar"). Se envía SIGTERM a sí mismo → el handler
        # de Rafael.py cancela main_task → shutdown() limpio (daemon + widget).
        if text.strip().lower() in ("/cerrar", "/shutdown", "cerrar"):
            logger.info("[RafaelDaemon] Comando de cierre recibido del widget — apagando")
            import os
            import signal
            os.kill(os.getpid(), signal.SIGTERM)
            return

        from src.domain.entities import Message, MessageRole
        from src.multimodal.pipeline_core import _JARVIS_EXCLUSIVE_TASKS

        task_type = _classify_jarvis_intent(text)
        use_bypass = task_type in _JARVIS_EXCLUSIVE_TASKS
        logger.info(
            f"[RafaelDaemon] dialog_in id={msg_id} task_type={task_type} "
            f"bypass={use_bypass} text='{text[:80]}'"
        )
        msg = Message(content=text, role=MessageRole.USER, metadata={"source": "dialog"})

        try:
            if use_bypass:
                response = await asyncio.wait_for(
                    self._dispatcher._dispatch(msg, task_type, None, None),
                    timeout=_DIALOG_DISPATCH_TIMEOUT,
                )
            else:
                response = await asyncio.wait_for(
                    self._dispatcher.process(msg, session_id=msg_id),
                    timeout=_DIALOG_DISPATCH_TIMEOUT,
                )
            reply = response.content if response and not response.error else "❌ Error procesando la consulta."
        except Exception as e:
            logger.error(f"[RafaelDaemon] dialog dispatch error: {e}", exc_info=True)
            reply = "❌ Hubo un problema procesando tu mensaje."

        logger.info(f"[RafaelDaemon] dialog_out id={msg_id} reply='{reply[:80]}'")
        try:
            _DIALOG_OUT_FILE.write_text(
                json.dumps({"id": msg_id, "text": reply, "ts": time.time()}),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[RafaelDaemon] No se pudo escribir dialog_out: {e}")

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        _cleanup_ipc_files()
        emit_state("IDLE")
        logger.info(f"[RafaelDaemon] Arrancando — modo={self._mode}")

        tasks = [
            asyncio.ensure_future(self._voice_pipeline.run_full_duplex()),
            asyncio.ensure_future(self._widget_supervisor()),
            asyncio.ensure_future(self._dialog_poll_loop()),
        ]
        # ✅ plan_rafael (2026-08-31): ProactiveEngine OFF por default — Rafael
        # SOLO responde cuando el usuario le habla. Antes hablaba solo (clima,
        # noticias, preguntas de ubicación sin que se le pida nada). Se puede
        # reactivar con jarvis_proactive_enabled=True.
        try:
            _proactive_enabled = bool(
                getattr(settings, "jarvis_proactive_enabled", False)
            )
        except Exception:
            _proactive_enabled = False
        if _proactive_enabled:
            tasks.append(
                asyncio.ensure_future(self._proactive_engine.run_forever())
            )
            logger.info("[RafaelDaemon] ProactiveEngine habilitado")
        else:
            logger.info("[RafaelDaemon] ProactiveEngine DESHABILITADO — solo responde a input del usuario")
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks: Optional[list] = None) -> None:
        logger.info("[RafaelDaemon] Apagando...")
        self._shutdown_event.set()
        self._proactive_engine.stop()
        self._voice_pipeline.stop()
        await self._stop_widget()

        if tasks:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        _cleanup_ipc_files()
        emit_state("IDLE")
        if _DETAILED_LOGGER_AVAILABLE:
            try:
                _close_detailed_logger()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 rafael_daemon.py:341] excepción degradada (intencional): {_d2e}")
        logger.info("[RafaelDaemon] Apagado completo")
        # ✅ plan_rafael (2026-08-31): los pools/threads no-daemon (dispatcher,
        # voice pipeline, embeddings) mantienen el proceso vivo tras el
        # shutdown limpio → systemd lo mataba con SIGKILL ("failed: timeout").
        # Forzamos la salida del proceso después del cleanup.
        try:
            import os
            os._exit(0)
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 rafael_daemon.py:351] excepción degradada (intencional): {_d2e}")
