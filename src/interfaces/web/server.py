#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Servidor Web NiceGUI (REFACTORIZADO)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/interfaces/web/server.py
# Autor: SaintWick
# Fecha: 2026-03-28
# ✅ CORREGIDO: Signal handlers para Ctrl+C
# ✅ CORREGIDO: Fix favicon path absoluto
# ✅ CORREGIDO: Signal handlers a nivel de módulo (antes de WebServer)
# ✅ CORREGIDO: SIGINT y SIGTERM registrados explícitamente
# ✅ CORREGIDO: Shutdown graceful con os._exit() después de cleanup
# ✅ ELIMINADO (v0.6.8s): Endpoints Dashboard /metrics, /health/detailed, /metrics/agents retirados
# ✅ CORREGIDO: Logging consistente con is_initialized (no _initialized)
# ✅ CORREGIDO: routes_extended.py importado para registrar todos los endpoints
# ✅ CORREGIDO: Verificación de endpoints críticos post-mount
# ✅ CORREGIDO v0.1.1: _log_web_event() con parámetro level
# ✅ CORREGIDO v0.1.2 (2026-06-24): Eliminadas referencias a aletheia-3b:latest → qwen3:8b
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, Any, Optional
# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_web_event(category: str, message: str, extra: dict = None, level: str = "INFO"):
    """
    Helper para logging de eventos web.
    Args:
        category: Categoría del evento (SERVER, SESSION, UI, API, etc.)
        message: Mensaje descriptivo
        extra: Información adicional para el log
        level: Nivel de log (INFO, WARNING, ERROR, DEBUG)
    """
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        # ✅ CORREGIDO: Usar propiedad pública is_initialized en lugar de _initialized
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 server.py:55] excepción degradada (intencional): {_d2e}")

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
        # ✅ CORREGIDO: Usar propiedad pública is_initialized en lugar de _initialized
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 server.py:73] excepción degradada (intencional): {_d2e}")

from nicegui import ui, app as nicegui_app

# Asegurar ruta absoluta para imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from config.paths import Paths

# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: MOUNT FASTAPI ROUTER
# ═══════════════════════════════════════════════════════════════
# ✅ NUEVO: Importar y montar router de FastAPI
def build_api_app():
    """Construye la app FastAPI (CORS + conteo de requests + routers).

    ✅ v0.6.9v (paso 5 del plan): extraído de `_mount_fastapi_router()` para poder
    TESTEAR el cableado de verdad — el test monta/inspecciona ESTA app (objetos
    reales) en vez de grepear el fuente, que es frágil ante refactors.
    Devuelve None si falta algún endpoint crítico.
    """
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from .request_stats import track_request_stats

        app = FastAPI(title="AXIOMA API", version="0.1.0")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Contador REAL de requests: `/api/v1/stats` devolvía total_requests,
        # avg_latency_ms y uptime_seconds en 0 (stub) mientras `router_metrics`
        # era real. Coste: unos ints por request.
        app.middleware("http")(track_request_stats)

        # Importar los módulos de rutas (registran endpoints en el router común)
        from .routes import router
        from . import routes_extended    # noqa: F401 — registra /status, /stats, etc.
        from . import routes_hwfit       # noqa: F401 — registra /hwfit/*
        from . import routes_preflight   # noqa: F401 — registra /preflight/* (Fase 1 de contenedores)

        app.include_router(router)

        critical_paths = ["/api/v1/health", "/api/v1/chat"]
        registered = [r.path for r in router.routes if hasattr(r, 'path')]
        missing = [p for p in critical_paths if p not in registered]
        if missing:
            logger.error(f"❌ Endpoints críticos faltantes: {missing}")
            _log_web_event("SERVER", f"Critical endpoints missing: {missing}",
                           level="ERROR")
            return None
        return app
    except Exception as exc:  # noqa: BLE001
        _log_error(exc, "server.build_api_app")
        return None


_fastapi_app = None

def _mount_fastapi_router():
    """
    Monta el router de FastAPI con todos los endpoints.
    ✅ CORREGIDO: Esta función es CRÍTICA para que los endpoints funcionen.
    ✅ CORREGIDO: Importa routes_extended.py para registrar endpoints adicionales.
    ✅ CORREGIDO: Verifica endpoints críticos post-mount.
    """
    global _fastapi_app
    try:
        # ✅ v0.6.9v (paso 5): la app (CORS + middleware de conteo + routers)
        # se construye en `build_api_app()`, que es testeable por sí sola.
        _fastapi_app = build_api_app()
        if _fastapi_app is None:
            return False

        # ✅ CORREGIDO: Importar router desde routes.py (incluye endpoints Core)
        from .routes import router
        registered_paths = [r.path for r in router.routes if hasattr(r, 'path')]

        # ✅ CORREGIDO: Montar FastAPI app en NiceGUI bajo /api
        # Esto hace que los endpoints /api/v1/metrics, /api/v1/health/detailed, etc. funcionen
        nicegui_app.mount('/api', _fastapi_app)

        _log_web_event("SERVER", "FastAPI router mounted successfully at /api")
        logger.info("✅ FastAPI router mounted at /api")
        logger.info("✅ System endpoints available:")
        logger.info("   - GET /api/v1/status")
        logger.info("   - GET /api/v1/sessions")
        logger.info("   - GET /api/v1/models")
        logger.info("   - GET /api/v1/stats")
        logger.info("✅ HardwareFit endpoints available:")
        logger.info("   - GET /api/v1/hwfit/hardware")
        logger.info("   - GET /api/v1/hwfit/rank")
        logger.info("   - GET /api/v1/hwfit/check/{model_name}")
        logger.info("   - GET /api/v1/hwfit/compatible")
        logger.info(f"✅ Total endpoints registered: {len(registered_paths)}")

        return True

    except ImportError as e:
        _log_error(e, "server.fastapi_mount")
        _log_web_event("SERVER", f"FastAPI router mount failed: {e}")
        logger.error(f"❌ Failed to mount FastAPI router: {e}")
        return False

    except Exception as e:
        _log_error(e, "server.fastapi_mount")
        _log_web_event("SERVER", f"FastAPI router mount error: {e}")
        logger.error(f"❌ FastAPI router mount error: {e}")
        return False

# ✅ CORREGIDO: Montar router al importar el módulo
_mount_fastapi_router()

# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: SIGNAL HANDLERS A NIVEL DE MÓDULO
# ═══════════════════════════════════════════════════════════════
# Variable global para controlar shutdown
_shutdown_requested = False

def _signal_handler(signum, frame):
    """
    ✅ CORREGIDO v0.1.0: Handler personalizado para SIGINT/SIGTERM.
    Permite shutdown graceful con un solo Ctrl+C.
    Registrado a nivel de módulo ANTES de ui.run().
    """
    global _shutdown_requested

    if signum == signal.SIGINT:
        signal_name = "SIGINT (Ctrl+C)"
    elif signum == signal.SIGTERM:
        signal_name = "SIGTERM"
    else:
        signal_name = f"Signal {signum}"

    if _shutdown_requested:
        # Segundo Ctrl+C → forzar salida inmediata
        _log_web_event("SERVER", f"Force shutdown requested ({signal_name})")
        print("\n⚡ Cierre forzado...")
        if LOGGER_AVAILABLE:
            try:
                from tools.detailed_logger import close_logger
                close_logger()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 server.py:204] excepción degradada (intencional): {_d2e}")
        os._exit(1)

    _shutdown_requested = True
    _log_web_event("SERVER", f"Graceful shutdown initiated ({signal_name})")
    print("\n👋 Cerrando AXIOMA... (Ctrl+C nuevamente para forzar)")

    # Cerrar logger
    if LOGGER_AVAILABLE:
        try:
            from tools.detailed_logger import close_logger
            close_logger()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 server.py:217] excepción degradada (intencional): {_d2e}")

    # Salir limpiamente
    os._exit(0)

# ✅ CORREGIDO: Registrar handlers ANTES de cualquier código de servidor
# Esto garantiza que Ctrl+C funcione correctamente
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# Variable global adicional para compatibilidad
_shutdown_event = threading.Event()

# ═══════════════════════════════════════════════════════════════
# CLASE: WebServer
# ═══════════════════════════════════════════════════════════════
class WebServer:
    """
    Servidor web para interfaz AXIOMA con NiceGUI.
    Importable y controlable desde main.py.

    Features:
    - ✅ API REST completa con FastAPI (rutas en routes.py + routes_extended.py)
    - ✅ NiceGUI para frontend Python puro
    - ✅ Endpoints System disponibles (/api/v1/status, /sessions, etc.)
    - ✅ Verificación de modelos al inicio
    - ✅ Timeouts progresivos (25s/35s/55s/65s/150s)
    - ✅ Shutdown graceful desde UI
    - ✅ Logging detallado de todas las operaciones
    - ✅ Timing de cada operación
    - ✅ Error tracking completo

    Example:
        >>> server = WebServer(host="127.0.0.1", port=8000)
        >>> server.start()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        open_browser: bool = True
    ):
        """
        Inicializa servidor web.
        Args:
            host: Host del servidor
            port: Puerto del servidor
            open_browser: Abrir navegador automáticamente
        Logging:
            - Registra inicialización del servidor
            - Host y puerto configurados
        """
        start = time.time()
        _log_web_event("SERVER", "WebServer initialization started",
                      extra={"host": host, "port": port, "open_browser": open_browser})

        self.host = host
        self.port = port
        self.open_browser = open_browser
        self.app = None
        self.static_path = Paths.ROOT / "src" / "interfaces" / "web"

        duration = time.time() - start
        _log_web_event("SERVER", "WebServer initialized",
                      extra={"duration": round(duration, 4), "host": host, "port": port})
        logger.info(f"WebServer initialized: {self.host}:{self.port}")

    def _check_models_on_startup(self) -> None:
        """
        Verifica modelos críticos al iniciar (log solo, no bloquea).
        Usa timeouts progresivos según el tipo de modelo.
        Logging:
            - Registra inicio de verificación de modelos
            - Modelos disponibles/no disponibles
            - Warnings de modelos críticos
            - Errores en verificación
        """
        start = time.time()
        _log_web_event("SERVER", "Model startup check initiated")

        def _check():
            check_start = time.time()
            try:
                from src.llm.client import OllamaClient

                print("\n" + "="*60)
                print("  VERIFICACIÓN DE MODELOS OLLAMA")
                print("="*60 + "\n")

                first_load_timeout = settings.get_timeout_for_model(
                    model_name=settings.llm_default_model if hasattr(settings, 'llm_default_model') else "qwen3:8b",
                    is_first=True
                ) if hasattr(settings, 'get_timeout_for_model') else 65

                client = OllamaClient(
                    model=settings.llm_default_model if hasattr(settings, 'llm_default_model') else "qwen3:8b",
                    timeout=first_load_timeout
                )

                if not client.health_check(timeout=5.0):
                    _log_web_event("SERVER", "Ollama not available",
                                  extra={"host": settings.ollama_host if hasattr(settings, 'ollama_host') else 'http://127.0.0.1:11434'})
                    logger.warning(f"⚠️  Ollama no disponible en {settings.ollama_host if hasattr(settings, 'ollama_host') else 'http://127.0.0.1:11434'}")
                    print(f"⚠️  Ollama no disponible - algunas funciones limitadas")
                    print(f"   💡 Ejecuta: ollama serve")
                    client.close()
                    return

                print(f"✅ Ollama conectado: {settings.ollama_host if hasattr(settings, 'ollama_host') else 'http://127.0.0.1:11434'}")

                available_models = {m["name"]: m for m in client.list_models()}

                critical_models = [
                    ("default", settings.llm_default_model, "standard"),
                    ("code", settings.llm_code_model, "standard"),
                ]

                print("\nModelos Críticos:")
                print("  " + "-"*50)

                all_critical_ok = True
                for role, model_name, scenario in critical_models:
                    timeout = settings.get_timeout_for_model(model_name, is_first=True) if hasattr(settings, 'get_timeout_for_model') else 65

                    if model_name in available_models:
                        model_info = available_models[model_name]
                        size_gb = model_info.get("size", 0) / (1024**3)
                        print(f"  ✅ {role:8} → {model_name:30} ({size_gb:.1f} GB)")
                    else:
                        print(f"  ⚠️  {role:8} NO disponible: {model_name}")
                        all_critical_ok = False

                        if role == "default":
                            _log_web_event("SERVER", f"Critical model missing: {model_name}", level="WARNING")
                            logger.warning(f"⚠️  Modelo crítico no disponible: {model_name}")

                print()

                if all_critical_ok:
                    print("✅ Todos los modelos críticos disponibles")
                    print("✅ Sistema listo para producción")
                    _log_web_event("SERVER", "All critical models available", extra={"status": "ready"})
                else:
                    print("⚠️  Algunos modelos no están disponibles")
                    _log_web_event("SERVER", "Some critical models missing", level="WARNING")

                print("=" * 60)
                print("\nConfiguración de Timeouts Progresivos:")
                print("  " + "-"*50)
                print(f"  • Standard (operaciones normales):  {settings.ollama_timeout_standard if hasattr(settings, 'ollama_timeout_standard') else 45}s")
                print(f"  • First Load (primera carga):       {settings.ollama_timeout_first_load if hasattr(settings, 'ollama_timeout_first_load') else 65}s")
                print(f"  • Vision (modelos de visión):       {settings.ollama_timeout_vision if hasattr(settings, 'ollama_timeout_vision') else 55}s")
                print(f"  • Embed (embeddings):               {settings.ollama_timeout_embed if hasattr(settings, 'ollama_timeout_embed') else 35}s")
                print(f"  • Code (generación código):         {settings.ollama_timeout_code if hasattr(settings, 'ollama_timeout_code') else 150}s")
                print("=" * 60)

                client.close()

                duration = time.time() - check_start
                _log_web_event("SERVER", "Model startup check completed",
                              extra={"duration": round(duration, 3), "all_critical_ok": all_critical_ok})

            except Exception as e:
                duration = time.time() - check_start
                _log_error(e, "WebServer._check_models_on_startup", extra={"duration": duration})
                _log_web_event("SERVER", "Model startup check failed",
                              extra={"duration": round(duration, 3), "error": str(e)})
                logger.error(f"Error en verificación de modelos: {e}")
                print(f"⚠️  Error verificando modelos: {e}")
                print(f"💡  Verifica que Ollama esté corriendo: ollama serve\n")

        threading.Thread(target=_check, daemon=True).start()

    def _open_browser(self) -> None:
        """
        Abrir navegador en un thread separado.
        Logging:
            - Registra apertura de navegador
            - URL abierta
            - Errores en apertura
        """
        start = time.time()
        time.sleep(2.0)  # ✅ CORREGIDO: Esperar 2s para que el servidor esté listo

        url = f"http://{self.host if self.host != '0.0.0.0' else '127.0.0.1'}:{self.port}"

        try:
            webbrowser.open(url)
            duration = time.time() - start
            _log_web_event("SERVER", "Browser opened",
                          extra={"duration": round(duration, 4), "url": url})
            logger.info(f"🌐 Navegador abierto: {url}")
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "WebServer._open_browser", extra={"duration": duration, "url": url})
            _log_web_event("SERVER", "Browser open failed",
                          extra={"duration": round(duration, 4), "url": url, "error": str(e)})
            logger.warning(f"⚠️ No se pudo abrir el navegador: {e}")

    def start(self) -> None:
        """
        Iniciar servidor web NiceGUI.
        Logging:
            - Registra inicio del servidor
            - Configuración del servidor
            - Verificación de modelos
            - Apertura de navegador
        """
        start = time.time()
        _log_web_event("SERVER", "WebServer start initiated",
                      extra={"host": self.host, "port": self.port, "open_browser": self.open_browser})

        print("\n" + "="*60)
        print("  AXIOMA v0.1.0 — Servidor Web NiceGUI")
        print("="*60)
        print(f"\n🌐 Servidor web iniciado en http://{self.host}:{self.port}")
        print(f"📁 Directorio web: {self.static_path}")
        print(f"⏱️  Timeout estándar: {settings.ollama_timeout_standard if hasattr(settings, 'ollama_timeout_standard') else 45}s")
        print(f"⏱️  Timeout primera carga: {settings.ollama_timeout_first_load if hasattr(settings, 'ollama_timeout_first_load') else 65}s")
        print(f"⏱️  Timeout código: {settings.ollama_timeout_code if hasattr(settings, 'ollama_timeout_code') else 150}s")
        print("\n🛑 Presiona Ctrl+C para detener\n")

        if hasattr(settings, 'should_verify_models_on_startup') and settings.should_verify_models_on_startup:
            _log_web_event("SERVER", "Model verification enabled on startup")
            self._check_models_on_startup()
        else:
            _log_web_event("SERVER", "Model verification disabled on startup")

        if self.open_browser:
            _log_web_event("SERVER", "Browser auto-open enabled")
            threading.Thread(target=self._open_browser, daemon=True).start()
        else:
            _log_web_event("SERVER", "Browser auto-open disabled")

        self._start_nicegui()

        duration = time.time() - start
        _log_web_event("SERVER", "WebServer start completed",
                      extra={"duration": round(duration, 4)})

    def _start_nicegui(self) -> None:
        """
        Inicia el servidor NiceGUI.
        Logging:
            - Registra inicio de NiceGUI
            - Configuración del servidor
            - Errores en inicio
        """
        start = time.time()
        _log_web_event("SERVER", "NiceGUI server starting")

        try:
            from .app import main_page, start_server

            # ✅ FIX: Montar src/interfaces/web/ como /static
            # Esto hace que logo.png sea accesible en /static/logo.png
            nicegui_app.add_static_files('/static', str(self.static_path))

            # ✅ CORREGIDO v0.1.0: Usar path absoluto para favicon, NO URL path
            # NiceGUI ui.run() favicon requiere path de archivo, NO ruta URL
            logo_path = Paths.ROOT / 'src' / 'interfaces' / 'web' / 'logo.png'
            favicon_option = str(logo_path) if logo_path.exists() else None

            _log_web_event("SERVER", "NiceGUI configuration loaded",
                          extra={"host": self.host, "port": self.port, "favicon": favicon_option})

            # ✅ CORREGIDO v0.1.0: ui.run() con reload=False para evitar conflictos de signals
            # Los signal handlers ya están registrados a nivel de módulo
            ui.run(
                host=self.host,
                port=self.port,
                reload=False,       # ✅ Evita conflictos de signal handlers
                show=False,         # ✅ Evita doble apertura (solo webbrowser.open controla)
                title='AXIOMA v0.1.0',
                favicon=favicon_option,  # ✅ Path absoluto o None, NO '/static/logo.png'
                storage_secret='axioma_secret_key_change_in_production',
                reconnect_timeout=300,   # ✅ FIX: 300s para aguantar LLM calls lentos sin desconectar
            )

            duration = time.time() - start
            _log_web_event("SERVER", "NiceGUI server started",
                          extra={"duration": round(duration, 4)})

        except ImportError as e:
            duration = time.time() - start
            _log_error(e, "WebServer._start_nicegui", extra={"duration": duration})
            _log_web_event("SERVER", "NiceGUI import failed",
                          extra={"duration": round(duration, 4), "error": str(e)})
            logger.error(f"NiceGUI no está instalado: {e}")
            print(f"\n❌ Error: NiceGUI no está instalado")
            print(f"💡 Ejecuta: pip install nicegui\n")
            sys.exit(1)

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "WebServer._start_nicegui", extra={"duration": duration})
            _log_web_event("SERVER", "NiceGUI server start failed",
                          extra={"duration": round(duration, 4), "error": str(e)})
            logger.error(f"Error iniciando NiceGUI: {e}", exc_info=True)
            print(f"\n❌ Error iniciando servidor: {e}\n")
            sys.exit(1)

    def stop(self) -> None:
        """
        Detener servidor gracefulmente.
        Logging:
            - Registra inicio de shutdown
            - Señal SIGINT enviada
            - Errores en shutdown
        """
        start = time.time()
        _log_web_event("SERVER", "WebServer stop initiated")

        try:
            logger.info("Stopping WebServer...")
            _shutdown_event.set()
            os.kill(os.getpid(), signal.SIGINT)

            duration = time.time() - start
            _log_web_event("SERVER", "WebServer stop completed",
                          extra={"duration": round(duration, 4)})

        except Exception as e:
            duration = time.time() - start
            _log_error(e, "WebServer.stop", extra={"duration": duration})
            _log_web_event("SERVER", "WebServer stop failed",
                          extra={"duration": round(duration, 4), "error": str(e)})
            logger.error(f"Error stopping server: {e}")
            raise

# ═══════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════
def create_web_server(host: str = '127.0.0.1', port: int = 8000,
                      open_browser: bool = True) -> WebServer:
    """
    Factory function para crear servidor web.
    Args:
        host: Host del servidor
        port: Puerto del servidor
        open_browser: Abrir navegador automáticamente
    Returns:
        WebServer: Instancia creada
    """
    return WebServer(host=host, port=port, open_browser=open_browser)

# ═══════════════════════════════════════════════════════════════
# Ejecución directa para pruebas (opcional)
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    start = time.time()
    _log_web_event("SERVER", "Direct execution detected")

    server = create_web_server(
        host=settings.host if hasattr(settings, 'host') else "127.0.0.1",
        port=settings.port if hasattr(settings, 'port') else 8000,
        open_browser=True,
    )
    server.start()

    duration = time.time() - start
    _log_web_event("SERVER", "Direct execution completed",
                  extra={"duration": round(duration, 4)})

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.1.2
# ═══════════════════════════════════════════════════════════════
# | Línea/Sección           | Cambio Realizado                      | Propósito                    |
# |-------------------------|---------------------------------------|------------------------------|
# | ~298, ~303, ~319        | aletheia-3b:latest → qwen3:8b      | Eliminar referencias obsoletas|
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — ~550 LÍNEAS — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
