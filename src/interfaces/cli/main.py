#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.1 — Interfaz CLI (Command Line Interface)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/cli/main.py
# Autor: SaintWick
# Fecha: 2026-04-13
# Propósito: CLI headless para operaciones sin interfaz web
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: Orchestrator → Dispatcher (módulo eliminado)
# ✅ CORREGIDO: Integración con DetailedLogger
# ✅ v0.5.0 (Fase 2): Integración CommandParser en loop interactivo.
# ✅ v1.0.0 (Fase 4): --voice flag + cmd_voice() + --model arg + check mejorado.
# ✅ v1.0.1 (2026-06-24): Eliminadas referencias a modelos obsoletos
#   (vicgalle/prometheus-7b-v2.0:latest → qwen3:8b) en fallbacks.
import argparse
import asyncio
import sys
import time
import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

# Asegurar ruta para imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import AxiomaError, ConfigurationError
from src.utils.ui import fuentes_de_respuesta

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_cli_event(category: str, message: str, extra: dict = None,
                   level: str = "INFO"):
    """
    Helper para logging de eventos CLI.

    ✅ ISSUE-144 (MEDIDO 2026-10-04): faltaba el parámetro `level` y **12 llamadas**
    de este mismo módulo se lo pasaban (`level="ERROR"`/`"WARNING"`, justo en los
    caminos de error y aviso). El error de argumentos salta ANTES de entrar a la
    función, así que el `try/except` de acá no podía atraparlo: medido, con Ollama
    caído `main.py --check` imprimía `TypeError: _log_cli_event() got an unexpected
    keyword argument 'level'` en lugar de decir qué faltaba instalar. Ninguna prueba
    cubría esa función.

    Args:
        category: Categoría del evento (CLI, CHECK, CHAT, BACKUP, INFO)
        message: Mensaje descriptivo
        extra: Información adicional para el log
        level: Nivel del evento (INFO por defecto; ERROR/WARNING en los avisos)
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_event(category, message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:69] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra: dict = None):
    """
    Helper para logging de errores.

    Args:
        error: Excepción capturada
        context: Contexto donde ocurrió el error
        extra: Información adicional para el log
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, '_initialized') and log._initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:88] excepción degradada (intencional): {_d2e}")


# ═══════════════════════════════════════════════════════════════
# CLASE: CLIConsole
# ═══════════════════════════════════════════════════════════════
class CLIConsole:
    """
    Consola CLI con soporte rich opcional.

    Features:
    - ✅ Logging detallado de todas las operaciones
    - ✅ Timing de cada comando
    - ✅ Error tracking completo
    - ✅ Soporte rich opcional para output formateado

    Example:
        >>> console = CLIConsole(use_rich=True)
        >>> console.print_success("Operación completada")
    """

    def __init__(self, use_rich: bool = True):
        """
        Inicializa consola CLI.

        Args:
            use_rich: Usar rich para output formateado

        Logging:
            - Registra inicialización de consola
            - Rich availability status
        """
        start = time.time()
        self.use_rich = use_rich and RICH_AVAILABLE
        self.console = Console() if self.use_rich else None
        duration = time.time() - start
        _log_cli_event("CLI", f"Console initialized: rich={self.use_rich}",
                      extra={"duration": round(duration, 4), "rich_available": RICH_AVAILABLE})

    def print(self, message: str, style: str = "") -> None:
        """
        Imprime mensaje con estilo opcional.

        Args:
            message: Mensaje a imprimir
            style: Estilo de rich (ej: "bold red")

        Logging:
            - Registra mensaje impreso (solo errores/success)
        """
        if self.use_rich and self.console:
            self.console.print(message, style=style)
        else:
            print(message)

    def print_panel(self, title: str, content: str, style: str = "blue") -> None:
        """
        Imprime panel con título.

        Args:
            title: Título del panel
            content: Contenido del panel
            style: Estilo del borde

        Logging:
            - Registra panel mostrado
        """
        _log_cli_event("CLI", f"Panel: {title}", extra={"content_preview": content[:50]})
        if self.use_rich and self.console:
            self.console.print(Panel(content, title=title, border_style=style))
        else:
            print(f"\n=== {title} ===\n{content}\n")

    def print_error(self, message: str) -> None:
        """
        Imprime error.

        Args:
            message: Mensaje de error

        Logging:
            - Registra error mostrado en consola
        """
        _log_cli_event("CLI", f"Error displayed: {message[:100]}", level="ERROR")
        self.print(f"❌ ERROR: {message}", style="bold red")

    def print_success(self, message: str) -> None:
        """
        Imprime éxito.

        Args:
            message: Mensaje de éxito

        Logging:
            - Registra éxito mostrado en consola
        """
        _log_cli_event("CLI", f"Success: {message[:100]}")
        self.print(f"✅ {message}", style="bold green")

    def print_warning(self, message: str) -> None:
        """
        Imprime advertencia.

        Args:
            message: Mensaje de advertencia

        Logging:
            - Registra advertencia mostrada en consola
        """
        _log_cli_event("CLI", f"Warning: {message[:100]}", level="WARNING")
        self.print(f"⚠️ {message}", style="bold yellow")

    def print_info(self, message: str) -> None:
        """
        Imprime información.

        Args:
            message: Mensaje informativo

        Logging:
            - Registra información mostrada en consola
        """
        _log_cli_event("CLI", f"Info: {message[:100]}")
        self.print(f"ℹ️ {message}", style="bold blue")


# ═══════════════════════════════════════════════════════════════
# COMANDO: --check
# ═══════════════════════════════════════════════════════════════
def cmd_check(console: CLIConsole) -> int:
    """
    Valida entorno AXIOMA desde CLI.

    Returns:
        int: Código de salida (0=éxito · 2=error de config · 3=error inesperado ·
             4=el entorno está bien pero **falta algo para poder responder**, por ejemplo
             Ollama caído o el modelo de chat sin descargar — `ISSUE-146`; antes decía
             "listo" con código 0 en ese caso)

    Logging:
        - Registra inicio/fin del check
        - Cada validación realizada
        - Errores encontrados con detalle
    """
    start = time.time()
    _log_cli_event("CHECK", "Environment validation started")
    console.print_panel("CHECK", "Validando entorno AXIOMA...", style="cyan")

    try:
        # Validar settings
        console.print_success(f"Settings: timeout={settings.ollama_timeout}s")
        _log_cli_event("CHECK", "Settings validated",
                      extra={"timeout": settings.ollama_timeout})

        console.print_success(f"Model: {settings.llm_default_model if hasattr(settings, 'llm_default_model') else 'qwen3:8b'}")
        _log_cli_event("CHECK", "Default model validated",
                      extra={"model": settings.llm_default_model if hasattr(settings, 'llm_default_model') else 'qwen3:8b'})

        # Validar directorios
        for attr in ["ROOT", "DATA", "LOGS", "MEMORY"]:
            path = getattr(Paths, attr, None)
            if path:
                if path.exists():
                    console.print_success(f"Existe: {path}")
                    _log_cli_event("CHECK", f"Directory exists: {attr}",
                                  extra={"path": str(path)})
                else:
                    path.mkdir(parents=True, exist_ok=True)
                    console.print_info(f"Creado: {path}")
                    _log_cli_event("CHECK", f"Directory created: {attr}",
                                  extra={"path": str(path)})

        # Validar Ollama
        # ✅ ISSUE-146 (MEDIDO 2026-10-04): si Ollama no responde o falta el modelo de chat, el
        # veredicto NO puede decir "listo" (antes lo decía y salía con código 0). Es lo que el
        # usuario pidió para el instalable: "que diga qué necesita antes de que funcione".
        _bloqueantes: list = []
        try:
            from src.llm.client import OllamaClient
            client = OllamaClient()
            if client.health_check():
                console.print_success("Ollama: conectado")
                _log_cli_event("CHECK", "Ollama connection successful")
                _chat = getattr(settings, "llm_default_model", "qwen3:8b")
                _instalados = [m.get("name", "") for m in (client.list_models() or [])]
                if _instalados and _chat not in _instalados and f"{_chat}:latest" not in _instalados:
                    _bloqueantes.append(
                        f"falta el modelo de chat `{_chat}`: instalalo con `ollama pull {_chat}`")
            else:
                console.print_warning("Ollama: sin respuesta")
                _log_cli_event("CHECK", "Ollama connection failed", level="WARNING")
                _bloqueantes.append(
                    f"Ollama no responde en {getattr(settings, 'ollama_host', '?')}: sin él "
                    "AXIOMA no puede responder nada. Instalalo con "
                    "`curl -fsSL https://ollama.com/install.sh | sh` y arrancalo con "
                    "`ollama serve`; o apuntá `OLLAMA_HOST` al equipo donde ya lo tengas")
        except ImportError:
            console.print_warning("LLM Client: no disponible")
            _log_cli_event("CHECK", "LLM Client not available", level="WARNING")
            _bloqueantes.append("no se pudo importar el cliente de modelos (src/llm/client.py)")

        # ✅ v1.0.0: Validar módulos Fase 1-4
        _checks = [
            ("Fase 1 — PermissionGateway", "src.tools_mcp.permissions", "get_gateway"),
            ("Fase 1 — validate_input",    "src.tools_mcp.tool_base",   "BaseTool"),
            ("Fase 2 — CommandParser",     "src.interfaces.cli.command_parser", "CommandParser"),
            ("Fase 2 — AudioPipeline",     "src.multimodal.audio_pipeline", "AudioPipeline"),
            ("Fase 3 — ContextWindow",     "src.memory.context_window", "ContextWindowManager"),
            ("Fase 3 — TeamCoordinator",   "src.core.task_decomposer",  "TeamCoordinator"),
            ("Fase 3 — EWMAOptimizer",     "src.core.feedback_loop",    "EWMAThresholdOptimizer"),
            ("Fase 4 — StagingFS",         "src.tools_mcp.sandbox",     "StagingFS"),
            ("Fase 4 — vLLMClient",        "src.llm.vllm_client",       "VLLMClient"),
        ]
        for label, module, cls_name in _checks:
            try:
                mod = __import__(module, fromlist=[cls_name])
                getattr(mod, cls_name)
                console.print_success(f"{label}: ✅")
            except ImportError as ie:
                console.print_warning(f"{label}: no disponible ({ie})")
            except Exception as ex:
                console.print_warning(f"{label}: error ({ex})")

        duration = time.time() - start
        _log_cli_event("CHECK", f"Environment validation completed in {duration:.4f}s",
                      extra={"duration": round(duration, 4)})
        if _bloqueantes:
            console.print_error("\n❌ AXIOMA todavía NO puede responder: falta algo de lo de arriba.")
            for _falta in _bloqueantes:
                console.print_info(f"   • {_falta}")
            console.print_info("   Volvé a correr `python main.py --check` cuando lo tengas.")
            return 4
        console.print_success("\nEntorno válido. AXIOMA listo.")
        return 0

    except ConfigurationError as e:
        duration = time.time() - start
        _log_error(e, "CLI.cmd_check", extra={"duration": duration})
        _log_cli_event("CHECK", f"Configuration error: {str(e)}", level="ERROR")
        console.print_error(f"Configuración: {e}")
        return 2

    except AxiomaError as e:
        duration = time.time() - start
        _log_error(e, "CLI.cmd_check", extra={"duration": duration})
        _log_cli_event("CHECK", f"Axioma error: {str(e)}", level="ERROR")
        console.print_error(f"AXIOMA: {e}")
        return 2

    except Exception as e:
        duration = time.time() - start
        _log_error(e, "CLI.cmd_check", extra={"duration": duration})
        _log_cli_event("CHECK", f"Unexpected error: {type(e).__name__}: {str(e)}", level="ERROR")
        console.print_error(f"Inesperado: {type(e).__name__}: {e}")
        return 3


# ═══════════════════════════════════════════════════════════════
# COMANDO: --chat
# ═══════════════════════════════════════════════════════════════
def _print_sources(console, response) -> None:
    """Imprime fuentes/citas cuando la respuesta está grounded (v0.6.9p).

    ✅ 2026-09-29 (unificación de CLIs): la EXTRACCIÓN de fuentes es la compartida
    (`src/utils/ui.py::fuentes_de_respuesta`), la misma que usa el CLI de la raíz
    (`main.py`). Antes cada CLI tenía su propia versión inline y podían divergir
    (de hecho divergieron: la raíz no mostraba fuentes hasta `ISSUE-111`).
    """
    try:
        srcs = fuentes_de_respuesta(response)
        if srcs:
            console.print_info(f"📎 Fuentes ({len(srcs)}):")
            for s in srcs:
                console.print(f"  - {s}")
    except Exception as exc:  # noqa: BLE001 — decorativo, nunca romper el chat
        logging.getLogger(__name__).debug(
            f"[cli] fuentes no imprimibles: {exc}")


def cmd_chat(console: CLIConsole, query: Optional[str] = None) -> int:
    """
    Ejecuta chat interactivo o consulta única.

    Args:
        console: Instancia de CLIConsole
        query: Consulta única (None para modo interactivo)

    Returns:
        int: Código de salida (0=éxito, 2=error AXIOMA, 3=error inesperado)

    Logging:
        - Registra inicio/fin del chat
        - Cada mensaje enviado/recibido
        - Errores en procesamiento
    """
    start = time.time()
    _log_cli_event("CHAT", "Chat mode started", extra={"query": query[:50] if query else "interactive"})
    console.print_panel("CHAT", "Iniciando interfaz de chat...", style="cyan")

    # ✅ CORREGIDO v0.5.0 (Fase 2): Import seguro CommandParser
    try:
        from src.interfaces.cli.command_parser import CommandParser
        _COMMANDS_AVAILABLE = True
    except ImportError:
        CommandParser = None  # type: ignore[assignment,misc]
        _COMMANDS_AVAILABLE = False

    try:
        # ✅ CORREGIDO: Orchestrator eliminado → Dispatcher como nuevo núcleo
        try:
            from src.core.dispatcher import Dispatcher
            from src.domain.entities import Message
            dispatcher = Dispatcher()
            _log_cli_event("CHAT", "Dispatcher initialized successfully")

            # ✅ v0.5.0: Estado de sesión compartido con CommandParser
            # ✅ ISSUE-130: el identificador se genera UNA vez por chat
            # interactivo y se pasa en cada consulta. Antes era None, así que
            # cada renglón abría una sesión nueva y el modelo no veía el
            # historial (el write-path guardaba, nadie releía). Con esto, un
            # chat de consola es una conversación, no consultas sueltas.
            session_state: dict = {
                "model": getattr(dispatcher, "_active_model", None),
                "history": [],
                "session_id": f"cli_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
            }

            # ✅ v0.5.0: Inicializar CommandParser si está disponible
            cmd_parser = None
            if _COMMANDS_AVAILABLE and CommandParser is not None:
                cmd_parser = CommandParser(console=console, session_state=session_state)
                _log_cli_event("CHAT", "CommandParser initialized")

            if query:
                # Consulta única — no interceptar slash commands en modo no-interactivo
                msg = Message(content=query, role="user")
                process_start = time.time()
                import asyncio as _asyncio
                response = _asyncio.run(dispatcher.process(msg))
                process_duration = time.time() - process_start
                _log_cli_event("CHAT", f"Query processed in {process_duration:.4f}s",
                              extra={"duration": round(process_duration, 4),
                                    "response_length": len(response.content)})
                console.print_panel("AXIOMA", response.content, style="green")
                _print_sources(console, response)
                duration = time.time() - start
                _log_cli_event("CHAT", f"Single query completed in {duration:.4f}s",
                              extra={"duration": round(duration, 4)})
                return 0
            else:
                # Interactivo
                hint = " | /help para comandos" if _COMMANDS_AVAILABLE else ""
                console.print_info(f"Modo interactivo. Escribe 'salir' para terminar{hint}.\n")
                _log_cli_event("CHAT", "Interactive mode started")
                message_count = 0
                while True:
                    try:
                        user_input = input("👤 Tú: ").strip()
                    except EOFError:
                        break

                    if user_input.lower() in ["salir", "exit", "quit"]:
                        console.print_info("Hasta luego.")
                        _log_cli_event("CHAT", f"Interactive session ended: {message_count} messages")
                        return 0

                    if not user_input:
                        continue

                    # ✅ v0.6.8ww: `/plan <consulta>` NO se intercepta como
                    # comando de UI — es el pipeline F3 del dispatcher
                    # (v0.6.8rr). Antes: CommandParser → "Comando desconocido".
                    _plan_ui = user_input.lower()
                    _is_plan_cmd = _plan_ui == "/plan" or _plan_ui.startswith("/plan ")
                    if (cmd_parser is not None and cmd_parser.is_command(user_input)
                            and not _is_plan_cmd):
                        cmd_result = cmd_parser.handle(user_input)
                        if cmd_result is not None:
                            if cmd_result.output:
                                console.print(cmd_result.output)
                            _log_cli_event("CHAT", f"Command executed: {user_input.split()[0]}",
                                          extra={"success": cmd_result.success})
                            if cmd_result.exit_session:
                                _log_cli_event("CHAT", f"Session exit via command: {message_count} messages")
                                return 0
                            continue

                    message_count += 1
                    # ✅ v0.5.0: Registrar en historial de sesión
                    session_state["history"].append({"role": "user", "content": user_input})

                    msg = Message(content=user_input, role="user")
                    process_start = time.time()
                    import asyncio as _asyncio
                    # ✅ ISSUE-130: la sesión del chat viaja en cada consulta
                    # (antes no se pasaba ninguna y cada renglón era aislado).
                    _sid = session_state["session_id"]
                    # 2026-08-25: get_event_loop() deprecado → get_running_loop()
                    try:
                        _asyncio.get_running_loop()  # RuntimeError si no hay loop
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            response = pool.submit(
                                _asyncio.run,
                                dispatcher.process(msg, session_id=_sid),
                            ).result()
                    except RuntimeError:
                        response = _asyncio.run(dispatcher.process(msg, session_id=_sid))
                    process_duration = time.time() - process_start
                    _log_cli_event("CHAT", f"Message {message_count} processed in {process_duration:.4f}s",
                                  extra={"duration": round(process_duration, 4),
                                        "input_length": len(user_input),
                                        "output_length": len(response.content)})
                    console.print_panel("AXIOMA", response.content, style="green")
                    _print_sources(console, response)
                    # ✅ v0.5.0: Guardar respuesta en historial
                    session_state["history"].append({"role": "assistant", "content": response.content})

        except ImportError:
            console.print_warning("Núcleo no disponible. Usando modo diagnóstico...")
            _log_cli_event("CHAT", "Core not available, diagnostic mode", level="WARNING")
            if query:
                console.print_info(f"Consulta recibida: '{query}'")
                _log_cli_event("CHAT", f"Diagnostic query: {query[:50]}")
            else:
                console.print_info("Comandos: --check, --chat 'consulta', --backup")
                _log_cli_event("CHAT", "Diagnostic mode help displayed")
            return 0

    except AxiomaError as e:
        duration = time.time() - start
        _log_error(e, "CLI.cmd_chat", extra={"duration": duration, "query": query[:50] if query else None})
        _log_cli_event("CHAT", f"Axioma error: {str(e)}", level="ERROR")
        console.print_error(f"AXIOMA: {e}")
        return 2

    except KeyboardInterrupt:
        duration = time.time() - start
        _log_cli_event("CHAT", f"Interrupted by user after {duration:.4f}s", level="WARNING")
        console.print_info("\nInterrumpido por usuario.")
        return 0

    except Exception as e:
        duration = time.time() - start
        _log_error(e, "CLI.cmd_chat", extra={"duration": duration, "query": query[:50] if query else None})
        _log_cli_event("CHAT", f"Unexpected error: {type(e).__name__}: {str(e)}", level="ERROR")
        console.print_error(f"{type(e).__name__}: {e}")
        return 3


# ═══════════════════════════════════════════════════════════════
# COMANDO: --voice  (Fase 2.2)
# ═══════════════════════════════════════════════════════════════
def _tts_config_desde_settings(play_audio: bool = True):
    """`TTSConfig` con la VOZ y la prosodia de `settings` (`.env`) — ✅ ISSUE-122.

    Antes la voz estaba hardcodeada (`es-ES-AlvaroNeural`, español de España) y sonaba
    "de afuera" para un usuario argentino. Ahora se elige con `AXIOMA_TTS_VOICE`
    (default `es-AR-TomasNeural`, rioplatense).
    """
    from src.multimodal.tts_engine import TTSConfig
    return TTSConfig(
        voice=getattr(settings, "tts_voice", "es-AR-TomasNeural"),
        fallback_voice=getattr(settings, "tts_fallback_voice", "es-AR-ElenaNeural"),
        rate=getattr(settings, "tts_rate", "+0%"),
        pitch=getattr(settings, "tts_pitch", "+0Hz"),
        volume=getattr(settings, "tts_volume", "+0%"),
        play_audio=play_audio,
    )


def _leer_audio_16k(path: Path, console: CLIConsole):
    """Lee un audio (WAV/MP3/OGG/FLAC) y lo deja mono a 16 kHz float32.

    ✅ Separado de `cmd_voice_file` para que cada función quede chica (el auditor marca
    como LOW los métodos >80 líneas). Devuelve `None` si no se puede leer (ya avisado).
    """
    import numpy as np
    try:
        import soundfile as sf
        audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as e:
        console.print_error(f"No se pudo leer el audio: {type(e).__name__}: {e}")
        return None
    audio = audio.mean(axis=1)                    # mono
    if sr != 16000:                               # el STT espera 16 kHz
        n = int(len(audio) * 16000 / sr)
        idx = np.linspace(0, len(audio) - 1, n)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype("float32")
    console.print_info(f"Audio: {path.name} · {len(audio)/16000:.1f} s · 16 kHz mono")
    return audio


def cmd_voice_file(console: CLIConsole, ruta: str, hablar: bool = True) -> int:
    """Procesa un ARCHIVO de audio con el pipeline completo — ✅ ISSUE-123.

    Motivo (medido): `--voice` necesita micrófono, así que no se podía verificar sin
    hardware. `AudioPipeline` ya tenía `process_audio(np.ndarray)`, que recibe muestras:
    esto lo expone desde el CLI leyendo un WAV/MP3 ⇒ la cadena STT → Dispatcher → TTS
    queda **verificable sin micrófono** y además sirve para transcribir/consultar audios
    grabados (notas de voz, reuniones).

    Args:
        ruta: archivo de audio (WAV/MP3/OGG/FLAC; se convierte a 16 kHz mono).
        hablar: si True, el TTS lee la respuesta (usar `--no-tts` para no reproducir).
    """
    try:
        from src.multimodal.audio_pipeline import AudioPipeline
        from src.multimodal.stt_engine import STTConfig
    except ImportError as e:
        console.print_error(f"Módulo de voz no disponible: {e}")
        return 3

    path = Path(ruta).expanduser()
    audio = _leer_audio_16k(path, console)
    if audio is None:
        return 3

    try:
        from src.core.dispatcher import Dispatcher
        dispatcher = Dispatcher()
        pipeline = AudioPipeline(
            dispatcher=dispatcher,
            stt_config=STTConfig(model_size="small"),
            tts_config=_tts_config_desde_settings(play_audio=hablar),
        )

        async def _run():
            return await pipeline.process_audio(audio)

        resultado = asyncio.run(_run())
    except Exception as e:
        console.print_error(f"Error procesando el audio: {type(e).__name__}: {e}")
        return 3

    transcripcion = getattr(resultado, "transcription", None)
    texto = getattr(transcripcion, "text", "") if transcripcion else ""
    conf = getattr(transcripcion, "confidence", 0.0) if transcripcion else 0.0
    console.print_success(f"Transcripción (confianza {conf:.2f}): {texto}")

    respuesta = getattr(resultado, "dispatcher_response", None)
    error = getattr(resultado, "error", None)
    # ⚠️ `dispatcher_response` es el TEXTO (str), no un `Response`: `PipelineResult`
    # guarda `result.response_content`. Se aceptan los dos casos por robustez.
    contenido = (respuesta if isinstance(respuesta, str)
                 else (getattr(respuesta, "content", "") or "")) if respuesta else ""
    if contenido and not error:
        console.print_panel("AXIOMA", contenido[:1200])
        if not isinstance(respuesta, str):
            _print_sources(console, respuesta)
        else:
            console.print_info("(el pipeline de voz devuelve solo el texto: sin lista de fuentes)")
        _log_cli_event("VOICE_FILE", "Audio procesado",
                       extra={"archivo": str(path), "chars_respuesta": len(contenido)})
        return 0
        _log_cli_event("VOICE_FILE", "Audio procesado",
                       extra={"archivo": str(path), "chars_respuesta": len(contenido)})
        return 0

    console.print_error(f"Sin respuesta: {error or 'error desconocido'}")
    return 1


def cmd_voice(console: CLIConsole, model: Optional[str] = None) -> int:
    """
    Activa el pipeline de voz: VAD → STT → Dispatcher → TTS.
    Escucha continuamente hasta Ctrl+C o /exit.
    """
    import asyncio as _asyncio

    try:
        from src.multimodal.audio_pipeline import AudioPipeline
        from src.multimodal.stt_engine import STTConfig
        from src.multimodal.tts_engine import TTSConfig
    except ImportError as e:
        console.print_error(f"Módulo de voz no disponible: {e}")
        console.print_info("Instalar: pip install faster-whisper sounddevice edge-tts webrtcvad-wheels soundfile")
        return 3

    console.print_panel("VOICE", "Iniciando pipeline de voz AXIOMA...", style="cyan")

    try:
        from src.core.dispatcher import Dispatcher
        dispatcher = Dispatcher()

        stt_config = STTConfig(model_size=model or "small")
        tts_config = _tts_config_desde_settings(play_audio=True)

        pipeline = AudioPipeline(
            dispatcher=dispatcher,
            stt_config=stt_config,
            tts_config=tts_config,
            on_transcription=lambda r: console.print_info(f"Escuché: {r.text}"),
            on_response=lambda r: None,  # TTS lo habla
            on_error=lambda e: console.print_error(f"Error: {e}"),
        )

        console.print_success("Pipeline listo. Habla para interactuar. Ctrl+C para salir.")
        _log_cli_event("VOICE", "Voice pipeline started")

        async def _run():
            await pipeline.start()

        _asyncio.run(_run())
        return 0

    except KeyboardInterrupt:
        console.print_info("\nPipeline de voz detenido.")
        _log_cli_event("VOICE", "Voice pipeline stopped by user")
        return 0
    except Exception as e:
        _log_error(e, "CLI.cmd_voice")
        console.print_error(f"{type(e).__name__}: {e}")
        return 3


# ═══════════════════════════════════════════════════════════════
# COMANDO: --backup
# ═══════════════════════════════════════════════════════════════
def crear_backup_json() -> Path:
    """Crea el backup estructural y devuelve su ruta — **implementación ÚNICA**.

    ✅ 2026-09-29 (unificación de CLIs): antes había DOS implementaciones del mismo
    backup (`main.py` escribía `axioma_state_<ts>.json` en `DATA_DIR/backups` y este
    módulo `axioma_backup_<ts>.json` en `Paths.BACKUPS`), con contenidos y resoluciones
    de ruta distintas. Ahora la raíz delega acá (`main.py::cmd_backup`).

    Nombre unificado: `axioma_backup_<timestamp>.json`.
    """
    backup_dir = Paths.BACKUPS if hasattr(Paths, "BACKUPS") else Paths.DATA / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"axioma_backup_{timestamp}.json"
    state = {
        "version": "1.0.0",
        "timestamp": timestamp,
        "backup_created_at": datetime.now().isoformat(),
        "paths": {
            "root": str(Paths.ROOT),
            "data": str(Paths.DATA),
            "logs": str(Paths.LOGS),
            "memory": str(getattr(Paths, "MEMORY", Paths.DATA / "memory")),
        },
        "settings": {
            "default_model": getattr(settings, "llm_default_model", "qwen3:8b"),
            "code_model": getattr(settings, "llm_code_model", "qwen2.5-coder:7b"),
            "ollama_timeout": getattr(settings, "ollama_timeout", 45),
            "max_retries": getattr(settings, "max_retries", 2),
        },
        "note": "Backup estructural — datos de memoria no incluidos",
    }
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return backup_file


def cmd_backup(console: CLIConsole) -> int:
    """
    Crea backup del sistema.

    Args:
        console: Instancia de CLIConsole

    Returns:
        int: Código de salida (0=éxito, 3=error inesperado)

    Logging:
        - Registra inicio/fin del backup
        - Ruta del archivo creado
        - Tamaño del backup
        - Errores en creación
    """
    start = time.time()
    _log_cli_event("BACKUP", "Backup process started")
    console.print_panel("BACKUP", "Iniciando respaldo...", style="cyan")
    try:
        backup_file = crear_backup_json()
        file_size = backup_file.stat().st_size
        duration = time.time() - start
        _log_cli_event("BACKUP", f"Backup completed in {duration:.4f}s",
                       extra={"duration": round(duration, 4),
                              "file_path": str(backup_file), "file_size": file_size})
        console.print_success(f"Guardado: {backup_file}")
        console.print_info(f"Tamaño: {file_size} bytes")
        return 0
    except Exception as e:
        _log_error(e, "CLI.cmd_backup", extra={"duration": time.time() - start})
        _log_cli_event("BACKUP", f"Backup failed: {type(e).__name__}: {str(e)}", level="ERROR")
        console.print_error(f"{type(e).__name__}: {e}")
        return 3


# ═══════════════════════════════════════════════════════════════
# COMANDO: --info
# ═══════════════════════════════════════════════════════════════
def cmd_info(console: CLIConsole) -> int:
    """
    Muestra información del sistema.

    Args:
        console: Instancia de CLIConsole

    Returns:
        int: Código de salida (0=éxito)

    Logging:
        - Registra acceso a información del sistema
        - Datos mostrados en panel
    """
    start = time.time()
    _log_cli_event("INFO", "System info requested")
    console.print_panel("INFO", "Información del Sistema AXIOMA", style="magenta")

    info = {
        "Versión": "1.0.1",
        "Ruta Raíz": str(Paths.ROOT),
        "Modelo Default": settings.llm_default_model if hasattr(settings, 'llm_default_model') else "qwen3:8b",
        "Timeout Ollama": f"{settings.ollama_timeout}s",
        "Max Reintentos": settings.max_retries,
        "Rich Disponible": "Sí" if RICH_AVAILABLE else "No",
        "Logger Disponible": "Sí" if LOGGER_AVAILABLE else "No",
    }

    for key, value in info.items():
        console.print(f"  {key}: {value}")
        _log_cli_event("INFO", f"Info displayed: {key}={value}")

    duration = time.time() - start
    _log_cli_event("INFO", f"System info completed in {duration:.4f}s",
                  extra={"duration": round(duration, 4)})
    return 0


# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def _ejecutar_comando(args, console: CLIConsole) -> int:
    """Despacha el comando pedido (extraído de `main()`: el auditor marca métodos >80)."""
    # Ejecutar comando
    if args.check:
        _log_cli_event("CLI", "Command: --check")
        result = cmd_check(console)
    elif args.chat is not None:
        _log_cli_event("CLI", f"Command: --chat {'(interactive)' if args.chat is None else '(query)'}")
        result = cmd_chat(console, args.chat)
    elif getattr(args, "voice_file", None):
        _log_cli_event("CLI", f"Command: --voice-file {args.voice_file}")
        result = cmd_voice_file(console, args.voice_file, hablar=not args.no_tts)
    elif getattr(args, "voice", False):
        _log_cli_event("CLI", "Command: --voice")
        result = cmd_voice(console, model=getattr(args, "model", None))
    elif args.backup:
        _log_cli_event("CLI", "Command: --backup")
        result = cmd_backup(console)
    elif args.info:
        _log_cli_event("CLI", "Command: --info")
        result = cmd_info(console)
    else:
        result = 0


def main() -> int:
    """
    Función principal CLI.

    Returns:
        int: Código de salida del comando ejecutado

    Logging:
        - Registra inicio de CLI
        - Comando ejecutado
        - Argumentos recibidos
    """
    start = time.time()
    _log_cli_event("CLI", "CLI started", extra={"args": sys.argv[1:]})

    parser = argparse.ArgumentParser(
        prog="axioma-cli",
        description="AXIOMA v1.0.1 — Interfaz de Línea de Comandos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python -m src.interfaces.cli.main --check
  python -m src.interfaces.cli.main --chat "¿Qué es Python?"
  python -m src.interfaces.cli.main --chat
  python -m src.interfaces.cli.main --backup
  python -m src.interfaces.cli.main --info
  python -m src.interfaces.cli.main --voice
  python -m src.interfaces.cli.main --voice --model small
  python -m src.interfaces.cli.main --chat "hola" --model qwen3:8b
"""
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check",  action="store_true", help="Validar entorno + módulos Fase 1-4")
    group.add_argument("--chat",   nargs="?", const=None, metavar="QUERY", help="Modo chat (interactivo o consulta única)")
    group.add_argument("--voice",  action="store_true", help="Modo voz: VAD→STT→Dispatcher→TTS")
    group.add_argument("--voice-file", metavar="RUTA", default=None,
                       help="Procesa un audio grabado (WAV/MP3): STT→Dispatcher→TTS, sin micrófono")
    parser.add_argument("--no-tts", action="store_true",
                        help="No reproducir la respuesta con TTS (útil con --voice-file)")
    group.add_argument("--backup", action="store_true", help="Crear backup del estado")
    group.add_argument("--info",   action="store_true", help="Información del sistema")
    group.add_argument("--no-rich",    action="store_true", help="Desactivar rich")
    group.add_argument("--no-logging", action="store_true", help="Desactivar logging")
    # ✅ v1.0.0: Argumentos opcionales transversales
    parser.add_argument("--model", metavar="MODEL", default=None,
                        help=f"Modelo LLM a usar (ej: {settings.llm_code_model}, small para voz)")

    args = parser.parse_args()

    # Inicializar consola
    console = CLIConsole(use_rich=not args.no_rich)

    # Sin argumentos: mostrar ayuda
    if not any([args.check, args.chat is not None, getattr(args, "voice", False),
                getattr(args, "voice_file", None), args.backup, args.info]):
        parser.print_help()
        _log_cli_event("CLI", "Help displayed (no arguments)")
        return 0

    # Ejecutar comando
    result = _ejecutar_comando(args, console)

    duration = time.time() - start
    _log_cli_event("CLI", f"CLI completed in {duration:.4f}s",
                  extra={"duration": round(duration, 4), "exit_code": result})
    return result


if __name__ == "__main__":
    sys.exit(main())


# ═══════════════════════════════════════════════════════════════
# 📋 CAMBIOS REALIZADOS
# ═══════════════════════════════════════════════════════════════
# | Versión | Sección       | Cambio                                        |
# |---------|---------------|-----------------------------------------------|
# | v0.0.8  | cmd_chat      | Orchestrator → Dispatcher                     |
# | v0.0.8  | logging       | Integración DetailedLogger                    |
# | v0.5.0  | cmd_chat      | Import seguro CommandParser                   |
# | v0.5.0  | cmd_chat      | session_state compartido con CommandParser     |
# | v0.5.0  | loop interac. | Intercepción slash commands antes de dispatch |
# | v0.5.0  | loop interac. | Historial de sesión en session_state          |
# | v0.5.0  | hint UI       | Mensaje "/help para comandos" en modo interac.|
# | v1.0.0  | cmd_check     | Valida módulos Fase 1-4 en --check            |
# | v1.0.0  | cmd_voice     | Pipeline voz VAD→STT→Dispatcher→TTS           |
# | v1.0.0  | --voice/--model| Nuevos flags CLI                             |
# | v1.0.0  | asyncio fix   | dispatcher.process() es async — usa run()     |
# | v1.0.0  | fallbacks     | Modelos viejos → nuevos (T02/T26)             |
# | v1.0.1  | cmd_check     | Fallback model → qwen3:8b (antes prometheus)|
# | v1.0.1  | cmd_backup    | Fallback model → qwen3:8b                   |
# | v1.0.1  | cmd_info      | Fallback model → qwen3:8b                   |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
