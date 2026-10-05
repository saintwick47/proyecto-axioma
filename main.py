#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# main.py - Entry Point Principal de AXIOMA
# Ruta: /ruta/a/axioma/main.py
# Versión: 1.0.8
#
# Punto de entrada para CLI y servidor web (NiceGUI + API).
# Orquesta el arranque del sistema: warm-up de modelos, verificación
# de hardware, limpieza de workspaces, inicialización de VoicePipeline
# y ProactiveEngine, y gestión de ciclo de vida con shutdown graceful.
# ═══════════════════════════════════════════════════════════════
import argparse
import sys
import os
import signal
import traceback
import time
import inspect
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from importlib.metadata import version, PackageNotFoundError

# Configuración de rutas — ✅ ISSUE-147 (2026-10-04, Fase 0 de contenedores):
# antes era la carpeta del autor (`Path("/ruta/a/axioma")`), así que
# en otra PC (o dentro de un contenedor) `sys.path` apuntaba a una carpeta inexistente y
# los `mkdir` de los registros fallaban con "permiso denegado". Medido: corriendo una
# copia en /tmp, el programa usaba y ESCRIBÍA en la carpeta original. Ahora la raíz es la
# carpeta donde vive este archivo (misma convención que `config/settings.py` y
# `config/paths.py`, que ya lo hacían así).
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ═══════════════════════════════════════════════════════════════
# PRE-FLIGHT: Verificación de dependencias críticas
# ═══════════════════════════════════════════════════════════════
# ✅ FIX (2026-08-26): "webrtcvad" → "webrtcvad-wheels" — el fork
# mantenido (misma API, sin pkg_resources deprecado) reemplazó al
# original en requirements.txt; importlib.metadata.version("webrtcvad")
# lanzaba PackageNotFoundError y el pre-flight intentaba instalar un
# paquete que ya no se usa. Los floors de httpx/nicegui son pisos
# mínimos (las versiones instaladas son muy superiores).
REQUIRED_DEPS = {
    "webrtcvad-wheels": "2.0.14",
    "httpx": "0.24.0",
    "nicegui": "1.4.0",
}

def _check_and_update_deps(auto_update: bool = False) -> bool:
    """Verifica dependencias. Retorna True si todo está OK."""
    needs_action = False
    for pkg, min_ver in REQUIRED_DEPS.items():
        try:
            installed_ver = version(pkg)
            inst_parts = list(map(int, installed_ver.split('.')[:2]))
            req_parts = list(map(int, min_ver.split('.')[:2]))
            if inst_parts < req_parts:
                print(f"⏳ {pkg} desactualizado: {installed_ver} < {min_ver}")
                needs_action = True
                action = "upgrade"
            else:
                continue
        except PackageNotFoundError:
            print(f"❌ {pkg} NO está instalado.")
            needs_action = True
            action = "install"

        if needs_action:
            do_it = auto_update
            if not auto_update:
                respuesta = input(f"   ¿Instalar/Actualizar {pkg} ahora? [Y/n]: ").strip().lower()
                do_it = respuesta in ("", "y", "yes")

            if do_it:
                print(f"🔄 Ejecutando: pip install {action} {pkg}...")
                try:
                    # ✅ FIX: "action" ("upgrade"/"install") se pasaba como
                    # argumento posicional de pip install, junto al paquete —
                    # pip lo interpretaba como OTRO paquete a instalar (que no
                    # existe en PyPI), así que el comando fallaba siempre,
                    # nunca instalaba ni actualizaba el paquete real. La forma
                    # correcta de pedir upgrade es la flag --upgrade.
                    cmd = [sys.executable, "-m", "pip", "install", "-q"]
                    if action == "upgrade":
                        cmd.append("--upgrade")
                    cmd.append(pkg)
                    subprocess.check_call(cmd)
                    print(f"✅ {pkg} resuelto.")
                except subprocess.CalledProcessError:
                    print(f"❌ Error instalando {pkg}. Abortando.")
                    return False
            else:
                print(f"⏭️ Omitido. El sistema puede fallar por falta de {pkg}.")
            needs_action = False
    print("✅ Dependencias verificadas.\n")
    return True

# ✅ 2026-10-01 (ISSUE-125): si se corre con el Python del SISTEMA en vez del venv,
# el error nativo es un `ModuleNotFoundError` entre decenas de imports (parece un
# bug del código). La implementación ÚNICA vive en `src/utils/degradation.py`
# (ahí y no duplicada: el auditor marca MEDIUM el código duplicado).
from src.utils.degradation import verificar_interprete as _verificar_interprete
_verificar_interprete("main.py")

if not _check_and_update_deps(auto_update=False):
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# IMPORTS DE AXIOMA (Después del pre-flight check)
# ═══════════════════════════════════════════════════════════════
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import AxiomaError, ConfigurationError
from src.utils.ui import print_fuentes

try:
    from tools.detailed_logger import (
        initialize_logger, close_logger, get_logger, DetailedLogger
    )
    LOGGER_AVAILABLE = True
except ImportError as e:
    print(f"⚠️  Warning: detailed_logger no disponible: {e}", file=sys.stderr)
    LOGGER_AVAILABLE = False

app = None

# ═══════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES DE LOGGING
# ═══════════════════════════════════════════════════════════════
def _get_caller_info() -> Dict[str, Any]:
    frame = inspect.currentframe()
    try:
        if frame and frame.f_back:
            return {
                "file": frame.f_back.f_code.co_filename,
                "line": frame.f_back.f_lineno,
                "function": frame.f_back.f_code.co_name,
            }
    finally:
        del frame
    return {}

def _ensure_logger(context: str = "main", create_dirs: bool = True) -> bool:
    if not LOGGER_AVAILABLE:
        return False
    try:
        if create_dirs:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
        elif not LOGS_DIR.exists():
            return True
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            return True
        log_file = LOGS_DIR / "reg_error.txt"
        if log_file.parent.exists() or create_dirs:
            result = initialize_logger(log_file, rotate=True)
            if result:
                get_logger().log_event("SETUP", f"Logger inicializado: {context}")
            return result
        return True
    except Exception as e:
        print(f"⚠️  Warning: Error logger {context}: {e}", file=sys.stderr)
        return False

def _log(category: str, message: str, level: str = "INFO", extra: Optional[Dict[str, Any]] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category, message, level, extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:165] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra: Optional[Dict[str, Any]] = None):
    caller = _get_caller_info()
    print(f"❌ [{context}] {type(error).__name__}: {error}", file=sys.stderr)
    if LOGGER_AVAILABLE:
        try:
            log = get_logger()
            if hasattr(log, 'is_initialized') and log.is_initialized:
                log.log_error(error, context=context, extra_info=extra,
                            file_path=caller.get("file", ""), line_number=caller.get("line"))
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 main.py:177] excepción degradada (intencional): {_d2e}")

def _log_func_call(name: str, args: Optional[Dict[str, Any]] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        caller = _get_caller_info()
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_function_call(name, "__main__", caller.get("file", ""),
                                caller.get("line", 0), kwargs=args)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:189] excepción degradada (intencional): {_d2e}")

def _log_func_return(name: str, ok: bool, dur: float, result_info: Optional[Dict[str, Any]] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_function_return(name, "__main__", dur, ok, result_info)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:199] excepción degradada (intencional): {_d2e}")

def _log_file(path: str, mode: str = "read", operation: str = ""):
    if not LOGGER_AVAILABLE:
        return
    try:
        size = None
        exists = Path(path).exists()
        if exists:
            size = Path(path).stat().st_size
        caller = _get_caller_info()
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_file_access(path, mode, operation, size, exists, caller.get("function", ""))
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:214] excepción degradada (intencional): {_d2e}")

def _log_model(model: str, action: str, task: str, duration: Optional[float] = None,
               success: bool = True, input_size: Optional[int] = None,
               output_size: Optional[int] = None, temperature: Optional[float] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_model_call(model, action, task, input_size, output_size,
                             duration, success, temperature)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:227] excepción degradada (intencional): {_d2e}")

def _log_memory(op: str, collection: str = "", session_id: str = "",
                items: Optional[int] = None, duration: Optional[float] = None,
                success: bool = True, db_path: str = ""):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_memory_operation(op, collection, session_id, items,
                                   duration, success, db_path)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:240] excepción degradada (intencional): {_d2e}")

def _log_router(query: str, task_type: str, model: str, confidence: float,
                cache_hit: bool = False, duration: Optional[float] = None,
                api_required: bool = False):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_router_decision(query, task_type, model, confidence,
                                  cache_hit, duration, api_required)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:253] excepción degradada (intencional): {_d2e}")

def _log_agent(agent: str, task: str, state: str = "started",
               duration: Optional[float] = None, success: bool = True,
               error: Optional[str] = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(agent, task, state, duration, success, error)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:265] excepción degradada (intencional): {_d2e}")

# ═══════════════════════════════════════════════════════════════
# v1.5.0: Cleanup automático de workspaces stale
# ═══════════════════════════════════════════════════════════════
def _cleanup_stale_workspaces() -> None:
    try:
        from src.core.workspace_manager import get_workspace_manager
        wm = get_workspace_manager()
        stale = wm.cleanup_stale(max_age_hours=24.0)
        if stale:
            print(f"🧹 [WORKSPACE] Cleanup automático: {len(stale)} workspaces viejos eliminados")
            _log("MAIN", f"Cleanup automático: {len(stale)} workspaces eliminados",
                 extra={"deleted": stale})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:280] excepción degradada (intencional): {_d2e}")

# ═══════════════════════════════════════════════════════════════
# v1.5.1: Warm-up de modelos — INDEPENDIENTE de VoicePipeline
# ═══════════════════════════════════════════════════════════════
def _warmup_models() -> bool:
    """
    Pre-carga modelos en VRAM antes de que el servidor acepte requests.
    Retorna True si al menos el modelo default se cargó.

    ✅ v1.5.1: Extraído del bloque VoicePipeline para que siempre corra,
    incluso si VoicePipeline/ProactiveEngine no están disponibles.
    ✅ v1.0.6: Registra modelo cargado en ModelSwap después del warmup.
    """
    import asyncio
    try:
        from tools.warmup_models import warm_up_all
        print("🔥 [WARMUP] Pre-cargando modelos en VRAM...")
        _log("MAIN", "Warm-up de modelos iniciado")

        results = asyncio.run(warm_up_all())

        if "error" in results:
            print(f"⚠️  [WARMUP] Ollama no disponible — los modelos se cargarán bajo demanda")
            _log("MAIN", "Warm-up falló: Ollama no disponible", level="WARNING")
            return False

        loaded = sum(1 for v in results.values() if v)
        total = len(results)
        if loaded > 0:
            print(f"✅ [WARMUP] {loaded}/{total} modelos pre-cargados — sin cold start")
            _log("MAIN", f"Warm-up OK: {loaded}/{total} modelos", extra=results)

            # ═══════════════════════════════════════════════════════════════
            # ✅ v1.0.6: Registrar modelo cargado en ModelSwap
            # warm_up_all() solo calienta el default (MODELS_TO_WARM
            # fue reducido a 1 modelo en warmup_models.py v1.0.6).
            # ModelSwap.register_loaded() marca qué modelo está en VRAM
            # sin hacer API call — es declaración de estado, no acción.
            # ═══════════════════════════════════════════════════════════════
            try:
                from src.core.model_swap import ModelSwap
                ModelSwap.instance().register_loaded(settings.llm_default_model)
                _log("MAIN", f"ModelSwap registered: {settings.llm_default_model}")
            except ImportError as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:325] excepción degradada (intencional): {_d2e}")

            return True
        else:
            print(f"⚠️  [WARMUP] Ningún modelo pudo cargarse — se cargarán bajo demanda")
            _log("MAIN", "Warm-up: 0 modelos cargados", level="WARNING")
            return False
    except Exception as e:
        print(f"⚠️  [WARMUP] Omitido: {e}")
        _log("MAIN", f"Warm-up omitido: {e}", level="WARNING")
        return False

# ═══════════════════════════════════════════════════════════════
# v1.0.8: Hardware fit check — evaluación temprana de compatibilidad
# ═══════════════════════════════════════════════════════════════
def _check_hardware_fit() -> None:
    """
    Evalúa compatibilidad de los modelos configurados con el hardware detectado.
    Emite WARNING en consola y log para no_fit/marginal. Completamente non-fatal.
    Llamar después de _warmup_models() — garantiza que Ollama está disponible.
    """
    try:
        from src.core.hardware_fit import HardwareFitManager
        mgr = HardwareFitManager.instance()
        hw = mgr.get_hardware()
        print(
            f"🔍 [HW-FIT] {hw.backend} | "
            f"vram={hw.gpu_vram_gb:.1f}GB (budget={hw.effective_budget_gb:.1f}GB) | "
            f"ram={hw.ram_gb:.1f}GB | threads={hw.cpu_threads}"
        )
        _log("MAIN", "HardwareFit detectado", extra={
            "backend": hw.backend,
            "gpu_name": hw.gpu_name,
            "vram_gb": round(hw.gpu_vram_gb, 2),
            "budget_gb": round(hw.effective_budget_gb, 2),
            "ram_gb": round(hw.ram_gb, 2),
            "threads": hw.cpu_threads,
        })
        report = mgr.is_current_config_compatible()
        if not report:
            _log("MAIN", "HardwareFit: ningún modelo configurado encontrado en Ollama", level="WARNING")
            return
        icons = {"perfect": "✅", "good": "✅", "marginal": "⚠️ ", "no_fit": "❌"}
        for key, result in report.items():
            icon = icons.get(result.fit_level, "❓")
            level = "WARNING" if result.fit_level in ("marginal", "no_fit") else "INFO"
            msg = f"{key} ({result.model_name}): {result.fit_level} — {result.notes}"
            print(f"{icon} [HW-FIT] {msg}")
            _log("MAIN", f"HardwareFit: {msg}", level=level, extra={
                "key": key,
                "model": result.model_name,
                "fit_level": result.fit_level,
                "required_gb": round(result.required_gb, 2),
                "notes": result.notes,
            })
    except ImportError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 main.py:381] excepción degradada (intencional): {_d2e}")
    except Exception as exc:
        _log("MAIN", f"HardwareFit check skipped: {exc}", level="WARNING")


# ═══════════════════════════════════════════════════════════════
# COMANDO: CHECK
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# ADAPTADOR DE CONSOLA (✅ unificación de CLIs, 2026-09-29)
# La implementación ÚNICA de los comandos vive en `src/interfaces/cli/main.py`.
# El módulo espera un `CLIConsole` (rich); este adaptador lo mapea a la salida de
# siempre de la raíz (emojis + prints) para NO cambiar la experiencia del usuario.
# ═══════════════════════════════════════════════════════════════
class _ConsolaRaiz:
    """Interfaz mínima de `CLIConsole` con la salida clásica de la raíz."""

    def print(self, *args, **kwargs):
        print(*args)

    def print_info(self, msg, *a, **k):
        print(f"ℹ️  {msg}")

    def print_success(self, msg, *a, **k):
        print(f"✅ {msg}")

    def print_warning(self, msg, *a, **k):
        print(f"⚠️  {msg}")

    def print_error(self, msg, *a, **k):
        print(f"❌ {msg}")

    def print_panel(self, titulo, msg, *a, **k):
        print(f"🔹 [{titulo}] {msg}")


def _consola_raiz() -> "_ConsolaRaiz":
    """Instancia del adaptador (evita repetir `_ConsolaRaiz()` en cada comando)."""
    return _ConsolaRaiz()


def cmd_check() -> int:
    """Validación del entorno — pre-flight propio + validación UNIFICADA del módulo.

    ✅ Unificación de CLIs: la validación de settings/directorios/Ollama es la del
    módulo (`src/interfaces/cli/main.py::cmd_check`), que ya era más completa. La raíz
    conserva lo suyo: el chequeo de dependencias.
    """
    start = time.time()
    _log_func_call("cmd_check")
    try:
        _ensure_logger("cmd_check", create_dirs=False)
        _log("CHECK", "Iniciando validación de entorno",
             extra={"timestamp": datetime.now().isoformat()})
        print("🔹 [CHECK] Validando entorno AXIOMA...")

        _check_and_update_deps(auto_update=False)

        from src.interfaces.cli.main import cmd_check as _check_modulo
        rc = _check_modulo(_consola_raiz())
        _log("CHECK", f"Validación delegada al módulo CLI (rc={rc})")
        print(f"⏱️  Duración total: {time.time() - start:.2f}s")
        _log_func_return("cmd_check", rc == 0, time.time() - start)
        return rc
    except Exception as e:
        _log_error(e, "cmd_check")
        print(f"❌ [CHECK] Error: {e}")
        _log_func_return("cmd_check", False, time.time() - start)
        return 1


def cmd_chat(query: Optional[str] = None) -> int:
    """Chat por CLI — pre-flight propio de la raíz + bucle UNIFICADO del módulo.

    ✅ Unificación de CLIs: antes la raíz tenía su propio bucle de chat y el módulo
    otro, y derivaron (la raíz no mostraba fuentes hasta `ISSUE-111`). Ahora el bucle
    vive en `src/interfaces/cli/main.py::cmd_chat`; acá quedan las cosas propias del
    entry point: warm-up de modelos y chequeo de hardware.
    """
    import asyncio
    start = time.time()
    _log_func_call("cmd_chat", {"query": query[:50] if query else None})
    try:
        _ensure_logger("cmd_chat")
        _log("CHAT", f"Iniciando chat - query: {query[:50] if query else 'None'}",
             extra={"mode": "single" if query else "interactive"})

        _warmup_models()
        _check_hardware_fit()

        from src.interfaces.cli.main import cmd_chat as _chat_modulo
        rc = _chat_modulo(_consola_raiz(), query)
        _log("CHAT", f"Chat finalizado con rc={rc}")
        _log_func_return("cmd_chat", rc == 0, time.time() - start)
        return rc
    except ImportError as e:
        print("⚠ [CHAT] Núcleo no disponible. Modo diagnóstico...")
        _log("CHAT", f"Dispatcher ImportError: {e}", level="WARNING")
        if query:
            print(f"📝 Consulta: '{query}'")
            print("💡 Ejecuta 'python main.py --check'")
        else:
            print("💡 AXIOMA v0.1.0 — Sistema en construcción.")
        _log_func_return("cmd_chat", False, time.time() - start)
        return 1


def cmd_backup() -> int:
    """Backup estructural — DELEGA en `crear_backup_json()` (✅ unificación de CLIs).

    Antes este comando tenía su propia implementación (nombre `axioma_state_*.json`,
    contenido distinto al del módulo CLI) y **estaba ROTO**: pasaba `size=` a
    `_log_file()`, que no acepta ese argumento ⇒ `TypeError` siempre (medido: el
    `python main.py --backup` de HEAD no funcionaba). Ahora la lógica vive en un solo
    lugar (`src/interfaces/cli/main.py::crear_backup_json`) y acá queda la telemetría
    propia del entry point.
    """
    start = time.time()
    _log_func_call("cmd_backup")
    try:
        _ensure_logger("cmd_backup")
        _log("BACKUP", "Iniciando proceso de backup", extra={"timestamp": datetime.now().isoformat()})
        print("🔹 [BACKUP] Iniciando respaldo...")

        from src.interfaces.cli.main import crear_backup_json
        backup_file = crear_backup_json()

        size = backup_file.stat().st_size if backup_file.exists() else 0
        _log_file(str(backup_file.parent), "create", "backup")
        _log_file(str(backup_file), "write", "backup")
        _log("BACKUP", f"Backup guardado: {backup_file}", extra={"size_bytes": size})
        print(f"✅ [BACKUP] Guardado: {backup_file}")
        print(f"📦 Tamaño: {size} bytes")
        _log_func_return("cmd_backup", True, time.time() - start)
        return 0
    except Exception as e:
        _log_error(e, "cmd_backup")
        print(f"❌ [BACKUP] Error: {e}")
        _log_func_return("cmd_backup", False, time.time() - start)
        return 1


def cmd_web(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True) -> int:
    global app
    start = time.time()
    _log_func_call("cmd_web", {"host": host, "port": port, "open_browser": open_browser})
    try:
        _ensure_logger("cmd_web")
        _log("WEB", f"Iniciando servidor: {host}:{port}", extra={"open_browser": open_browser})
        print(f"🔹 [WEB] Iniciando AXIOMA NiceGUI en http://{host}:{port}")

        from src.interfaces.web.server import WebServer
        _log("WEB", "WebServer class imported from server.py")

        web_server = WebServer(host=host, port=port, open_browser=open_browser)
        _log("WEB", "WebServer instance created", extra={"host": host, "port": port})

        print(f"📚 Documentación API: http://{host}:{port}/docs")
        _log("WEB", "Starting WebServer via server.py")

        _voice_active_event = threading.Event()
        _vp = None
        _pe = None
        _dispatcher = None

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.5.1: WARM-UP PRIMERO — ANTES de VoicePipeline/Dispatcher
        # ═══════════════════════════════════════════════════════════════
        _warmup_models()
        _check_hardware_fit()

        # ═══════════════════════════════════════════════════════════════
        # ✅ v1.0.6: Reconciliar ModelSwap con estado real de Ollama
        # Después del warmup, ModelSwap ya tiene el modelo registrado
        # vía _warmup_models(). reconcile() verifica contra /api/ps
        # que Ollama realmente tiene el modelo cargado. Si Ollama
        # descargó el modelo por timeout (5min default), reconcile
        # detecta la discrepancia y actualiza el estado interno.
        # ═══════════════════════════════════════════════════════════════
        try:
            from src.core.model_swap import ModelSwap
            ModelSwap.instance().reconcile()
            _log("MAIN", f"ModelSwap reconciled: {ModelSwap.instance().current_model}")
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 main.py:684] excepción degradada (intencional): {_d2e}")

        # ✅ [D4] + CAAC: VoicePipeline — import seguro
        try:
            import asyncio as _asyncio
            from src.multimodal.voice_pipeline import VoicePipeline
            from src.multimodal.stt_engine import STTEngine
            from src.multimodal.tts_engine import TTSEngine
            from src.core.dispatcher import Dispatcher as _Dispatcher

            _dispatcher = _Dispatcher()

            _vad, _wake = None, None
            try:
                from src.multimodal.vad_filter import VADFilter
                _vad = VADFilter()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:701] excepción degradada (intencional): {_d2e}")
            try:
                from src.multimodal.wake_word import WakeWordDetector
                _wake = WakeWordDetector()
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:706] excepción degradada (intencional): {_d2e}")

            _vp = VoicePipeline(
                dispatcher=_dispatcher,
                tts=TTSEngine(),
                stt=STTEngine(),
                vad=_vad,
                wake_detector=_wake,
                voice_active_event=_voice_active_event,
            )
            # ✅ 2026-08-25: el audio NO se auto-activa. Se inicia solo si la
            # configuración lo pide (jarvis_mode_default) o cuando el usuario
            # lo habilita en la UI (Modo Jarvis → voice_runtime.set_voice).
            from src.interfaces.web.voice_runtime import register, set_voice, is_voice_running
            register(_vp)
            if settings.jarvis_mode_default in ("jarvis", "voice_only"):
                set_voice(True)
                _log("MAIN", f"VoicePipeline iniciado (jarvis_mode_default={settings.jarvis_mode_default})")
            else:
                _log("MAIN", "VoicePipeline REGISTRADO pero en espera (jarvis_mode_default=normal)")
        except Exception as _vp_err:
            _log("MAIN", f"VoicePipeline no disponible: {_vp_err}", level="WARNING")

        # ✅ [D4] + CAAC: ProactiveEngine — import seguro
        try:
            import asyncio as _asyncio
            from src.core.proactive_engine import ProactiveEngine
            from src.multimodal.tts_engine import TTSEngine as _TTSEngine

            _task_queue = None
            try:
                from src.core.task_queue import get_task_queue
                _task_queue = _asyncio.run(get_task_queue(max_size=200, deduplicate=True))
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:740] excepción degradada (intencional): {_d2e}")

            # Dispatcher puede no haberse creado si VoicePipeline falló
            if _dispatcher is None:
                try:
                    from src.core.dispatcher import Dispatcher as _Disp2
                    _dispatcher = _Disp2()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 main.py:748] excepción degradada (intencional): {_d2e}")

            _pe = ProactiveEngine(
                dispatcher=_dispatcher,
                tts=_TTSEngine(),
                voice_active_event=_voice_active_event,
                task_queue=_task_queue,
            )

            # ✅ v1.5.1: Keep-alive daemon + ProactiveEngine via on_startup
            try:
                from tools.warmup_models import keep_alive_daemon
                from nicegui import app as _nicegui_app

                async def _start_background_tasks():
                    import asyncio as _aio
                    _aio.create_task(keep_alive_daemon())
                    if _pe is not None:
                        _aio.ensure_future(_pe.run_forever())
                    # ✅ v0.6.9v (paso 5 del plan): frescura AUTOMÁTICA del
                    # health report. Antes había que acordarse de correrlo a
                    # mano, así que un reporte viejo se leía como estado actual
                    # (ISSUE-001). Se hace en un thread para no bloquear el
                    # arranque (es análisis de logs, no toca la GPU/CPU de forma
                    # significativa) y cualquier fallo es solo un WARNING.
                    from tools.log_health_report import (
                        refresh_health_report_async,
                    )

                    async def _refresh_health_report():
                        path = await refresh_health_report_async()
                        if path is not None:
                            _log("MAIN", f"Health report refrescado: {path}")
                        else:
                            _log("MAIN", "Health report no generado (sin logs)",
                                 level="WARNING")
                    _aio.create_task(_refresh_health_report())

                _nicegui_app.on_startup(_start_background_tasks)
            except Exception as _ka_err:
                _log("MAIN", f"Keep-alive daemon omitido: {_ka_err}", level="WARNING")

            _log("MAIN", "ProactiveEngine iniciado")
        except Exception as _pe_err:
            _log("MAIN", f"ProactiveEngine no disponible: {_pe_err}", level="WARNING")

        # ✅ CAAC Fase 3: Graceful shutdown handlers
        def _shutdown_handler(sig, frame):
            _log("MAIN", f"Signal {sig} recibido — shutdown iniciado")
            print(f"\n⏹️  [MAIN] Shutdown signal {sig} — deteniendo componentes...")
            try:
                if _vp is not None:
                    _vp.stop()
            except Exception as _e:
                _log("MAIN", f"VoicePipeline stop error: {_e}", level="WARNING")
            try:
                if _pe is not None:
                    _pe.stop()
            except Exception as _e:
                _log("MAIN", f"ProactiveEngine stop error: {_e}", level="WARNING")

            # ═══════════════════════════════════════════════════════════════
            # ✅ v1.0.6: Liberar VRAM al cerrar
            # Descarga todos los modelos (keep_alive=0) para que Ollama
            # no mantenga VRAM ocupada después de que AXIOMA se detenga.
            # Esto es importante en hardware con iGPU compartida donde
            # la VRAM se comparte con la RAM del sistema.
            # ═══════════════════════════════════════════════════════════════
            try:
                from src.core.model_swap import ModelSwap
                ModelSwap.instance().force_unload_all()
                _log("MAIN", "ModelSwap: VRAM freed on shutdown")
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:803] excepción degradada (intencional): {_d2e}")

            _voice_active_event.clear()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)

        _log("MAIN", "Hardware limits", extra={
            "max_reasoning_tokens": getattr(settings, "max_reasoning_tokens", 2048),
            "sandbox_max_memory_mb": getattr(settings, "sandbox_max_memory_mb", 256),
            "barge_in_timeout_ms": getattr(settings, "barge_in_timeout_ms", 50),
            "cpu": "Ryzen 5 8500G (6c/12t)",
        })

        duration = time.time() - start
        _log_func_return("cmd_web", True, duration)
        try:
            web_server.start()
        finally:
            if _vp is not None:
                try:
                    _vp.stop()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 main.py:827] excepción degradada (intencional): {_d2e}")
            if _pe is not None:
                try:
                    _pe.stop()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 main.py:832] excepción degradada (intencional): {_d2e}")
            if _dispatcher is not None:
                try:
                    import asyncio as _asyncio_finally
                    _asyncio_finally.run(_dispatcher.cleanup())
                    _log("MAIN", "Dispatcher cleanup OK")
                except Exception as _ce:
                    _log("MAIN", f"Dispatcher cleanup error: {_ce}", level="WARNING")
            _voice_active_event.clear()
            _log("MAIN", "Componentes detenidos en finally")
        return 0

    except ImportError as e:
        msg = f"Módulo web no disponible: {e}"
        _log_error(ImportError(msg), "WEB", {"type": "ImportError", "module": str(e)})
        print(f"❌ [WEB] Error: {msg}")
        print("💡 Instala: pip install nicegui fastapi uvicorn")
        duration = time.time() - start
        _log_func_return("cmd_web", False, duration)
        return 2
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 Servidor detenido.")
        _log("WEB", "Shutdown limpio (KeyboardInterrupt/SystemExit)", level="INFO")
        duration = time.time() - start
        _log_func_return("cmd_web", True, duration)
        return 0
    except Exception as e:
        _log_error(e, "WEB", {"type": "Unexpected", "host": host, "port": port, "tb": traceback.format_exc()})
        duration = time.time() - start
        _log_func_return("cmd_web", False, duration)
        return 3
    finally:
        if LOGGER_AVAILABLE:
            close_logger()

# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def _construir_parser() -> argparse.ArgumentParser:
    """Contrato de la línea de comandos (extraído para poder probarlo).

    ⚠️ `--chat` sin consulta queda en **cadena vacía**, no en `None`: con
    `const=None`, "sin consulta" era indistinguible de "no lo pediste" y
    `main()` imprimía la ayuda aunque su propio texto promete modo interactivo
    (había que escribir `--chat ""`).
    """
    parser = argparse.ArgumentParser(
        prog="axioma", description="AXIOMA v1.0.6 — Sistema Multi-Agente de IA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Ejemplos:
  python main.py --check                    # Validar entorno
  python main.py --chat "¿Qué es Python?"   # Consulta única
  python main.py --chat                     # Modo interactivo
  python main.py --backup                   # Crear respaldo
  python main.py --web                      # Iniciar UI web (NiceGUI + API)
  python main.py --web --host 0.0.0.0 --port 8080  # Servidor público

Rutas absolutas:
  Proyecto: {PROJECT_ROOT} | Datos: {DATA_DIR} | Logs: {LOGS_DIR}
  Logging: {LOGS_DIR / 'reg_error.txt'}

API Endpoints (refactorizados):
  POST /api/v1/chat          # Chat principal
  POST /api/v1/upload_file   # Upload de archivos
  POST /api/v1/analyze_file  # Análisis de archivos
  GET  /api/v1/api/status    # Estado de APIs externas
  GET  /api/v1/models/check  # Verificación de modelos Ollama
  POST /api/v1/shutdown      # Cierre controlado
  ... y más en /docs

Modelos configurados (settings.py):
  Default: {settings.llm_default_model}
  Código:  {settings.llm_code_model} (carga on-demand vía ModelSwap)
  Juez:    {settings.llm_judge_model}

VRAM Management (v1.0.6):
  Solo UN modelo en VRAM en cualquier momento.
  ModelSwap gestiona carga/descarga automática.
"""
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Validar configuración")
    group.add_argument("--chat", nargs="?", const="", metavar="QUERY", help="Modo chat")
    group.add_argument("--backup", action="store_true", help="Crear backup JSON")
    group.add_argument("--info", action="store_true", help="Información del sistema")
    group.add_argument("--voice", action="store_true", help="Modo voz (VAD→STT→Dispatcher→TTS)")
    group.add_argument("--voice-file", metavar="RUTA", default=None,
                       help="Procesa un audio grabado (WAV/MP3): STT→Dispatcher→TTS")
    parser.add_argument("--no-tts", action="store_true", help="No hablar la respuesta")
    parser.add_argument("--rac", metavar="ACCION", default=None,
                        choices=["status", "stop", "pause", "resume"])   # ISSUE-113
    group.add_argument("--web", action="store_true", help="Iniciar UI web NiceGUI + API")

    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host servidor web")
    parser.add_argument("--port", type=int, default=8000, help="Puerto servidor web")
    parser.add_argument("--no-browser", action="store_true", help="No abrir navegador")
    return parser


def main() -> int:
    start = time.time()
    try:
        _log("MAIN", "AXIOMA v1.0.6 starting", extra={
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "pid": os.getpid(),
            "cwd": os.getcwd()
        })
        _log_file(str(PROJECT_ROOT), "read", "project_root")

        _cleanup_stale_workspaces()

        parser = _construir_parser()

        args = parser.parse_args()


        if getattr(args, "rac", None):    # ✅ ISSUE-113: estado/control del RAC
            from src.agents.analyst import rac_control
            print(rac_control(args.rac)); return 0

        if args.info:      # ✅ unificación: implementación del módulo CLI
            from src.interfaces.cli.main import cmd_info
            return cmd_info(_consola_raiz())
        if getattr(args, "voice_file", None):   # ✅ ISSUE-123: pipeline con audio grabado
            from src.interfaces.cli.main import cmd_voice_file
            return cmd_voice_file(_consola_raiz(), args.voice_file, hablar=not args.no_tts)
        if args.voice:     # ✅ unificación: implementación del módulo CLI
            from src.interfaces.cli.main import cmd_voice
            return cmd_voice(_consola_raiz(), args.model if hasattr(args, "model") else None)
        if not any([args.check, args.chat is not None, args.backup, args.web]):
            parser.print_help()
            return 0

        _log("MAIN", f"Arguments parsed: check={args.check}, chat={args.chat}, backup={args.backup}, web={args.web}")

        if args.check:
            return cmd_check()
        elif args.chat is not None:
            return cmd_chat(args.chat)
        elif args.backup:
            return cmd_backup()
        elif args.web:
            return cmd_web(host=args.host, port=args.port, open_browser=not args.no_browser)

        return 0

    except Exception as e:
        print(f"❌ [MAIN] Error crítico: {type(e).__name__}: {e}", file=sys.stderr)
        if LOGGER_AVAILABLE:
            try:
                lg = get_logger()
                if hasattr(lg, 'is_initialized') and lg.is_initialized:
                    lg.log_error(e, context="main()", extra_info={"type": "MainLevel", "tb": traceback.format_exc()})
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 main.py:956] excepción degradada (intencional): {_d2e}")
        return 99
    finally:
        if LOGGER_AVAILABLE:
            close_logger()
        dur = time.time() - start
        print(f"\n⏱️  Duración total: {dur:.2f}s")
        _log("MAIN", "AXIOMA shutdown", extra={"duration_s": round(dur, 2), "exit_code": sys.exc_info()[0] is None})

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    sys.exit(main())
