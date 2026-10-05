#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# detailed_logger.py - Sistema de Logging Detallado para AXIOMA
# Ruta: /ruta/a/axioma/tools/detailed_logger.py
# Autor: SaintWick
# Versión: 0.1.1
#
# Proporciona un logger no bloqueante, thread-safe con trazabilidad
# de funciones, modelos, memoria, router, agentes, circuit breakers
# y feedback. Incluye captura de errores con cadena de causa,
# variables locales redactadas y rotación de logs.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging

import sys
import os
import re
import atexit
import traceback
import functools
import time
import datetime
import json
import inspect
import hashlib
import threading
from pathlib import Path
from typing import Optional, Callable, Any, Dict, List
from contextlib import contextmanager

__version__ = "0.2.0"

# ============================================================================
# ✅ FIX AUDITORÍA (S6) — Redacción de secretos en variables locales
# ============================================================================
# _get_frame_locals() captura f_locals del frame del error y los escribe en
# logs/reg_error.txt en texto plano. Si una variable local se llama algo como
# "api_key" o "token" (ej. `key = settings.tavily_api_key`), su valor real
# quedaba expuesto en el log. No se desactiva la captura (sigue siendo la
# info que necesitamos para diagnosticar errores de AXIOMA/Rafael) — se
# redacta el valor solo cuando el nombre de la variable o su contenido
# matchea un patrón de secreto conocido.
_SENSITIVE_NAME_HINTS = (
    "key", "token", "secret", "password", "passwd", "credential", "auth",
)
_SECRET_VALUE_PATTERNS = [
    re.compile(r'sk-[A-Za-z0-9]{20,}'),                     # OpenAI-style
    re.compile(r'AKIA[A-Z0-9]{16}'),                        # AWS access key
    re.compile(r'ghp_[A-Za-z0-9]{30,}'),                    # GitHub token
    re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}'),            # Slack token
    re.compile(r'Bearer\s+[A-Za-z0-9._-]{15,}', re.IGNORECASE),
]


def _redact_value(name: str, value_repr: str) -> str:
    """Redacta value_repr si `name` sugiere un secreto o si el contenido
    matchea un patrón de secreto conocido. No toca nada más — el resto
    de la variable (u otras variables) se loguea igual que antes."""
    if any(hint in name.lower() for hint in _SENSITIVE_NAME_HINTS):
        return "[REDACTED]"
    redacted = value_repr
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted

# ============================================================================
# CONFIGURACIÓN — RUTAS DERIVADAS DEL PROYECTO (portátiles)
# ============================================================================
# ✅ v0.2.0 (2026-09-01): las rutas se derivan de __file__ en vez de estar
# hardcodeadas a /ruta/a/axioma — el logger funciona en
# cualquier checkout/clon sin editar nada.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LoggerConfig:
    """Configuración central del logger — rutas derivadas del proyecto."""
    PROJECT_ROOT:             Path = _PROJECT_ROOT
    OUTPUT_FILE:              Path = _PROJECT_ROOT / "logs" / "reg_error.txt"
    BACKUP_FILE:              Path = _PROJECT_ROOT / "logs" / "reg_error_old.txt"
    FEEDBACK_FILE:            Path = _PROJECT_ROOT / "logs" / "feedback_log.jsonl"
    LOGS_DIR:                 Path = _PROJECT_ROOT / "logs"
    DATA_DIR:                 Path = _PROJECT_ROOT / "data"
    MAX_LOG_FILES:            int  = 2
    MAX_TRACEBACK_LINES:      int  = 150     # ampliado de 100 — cadenas de causa son largas
    MAX_LOCAL_VARS:           int  = 10      # max variables locales en error frame
    ENABLE_FUNCTION_TRACE:    bool = True
    ENABLE_FILE_ACCESS:       bool = True
    ENABLE_ERROR_DETAILS:     bool = True
    ENABLE_TIMING:            bool = True
    ENABLE_MODEL_TRACE:       bool = True
    ENABLE_MEMORY_TRACE:      bool = True
    ENABLE_ROUTER_TRACE:      bool = True
    ENABLE_AGENT_TRACE:       bool = True
    ENABLE_FEEDBACK_TRACE:    bool = True
    ENABLE_CB_TRACE:          bool = True    # circuit breaker
    ENABLE_LOCAL_VARS:        bool = True    # captura vars locales en error
    ENCODING:                 str  = "utf-8"


# ============================================================================
# CLASE PRINCIPAL — DetailedLogger
# ============================================================================
class DetailedLogger:
    """
    Logger detallado para AXIOMA v0.1.0
    Registra: funciones, archivos, modelos LLM, memoria, router, agentes,
              circuit breakers, feedback, errores con cadena de causa completa.
    NO BLOQUEANTE: si el disco falla o hay error de I/O, cae a stderr silenciosamente.
    THREAD-SAFE: lock interno para escrituras concurrentes (asyncio + threads).
    """
    _instance: Optional[DetailedLogger] = None
    _instance_initialized: bool = False

    def __new__(cls) -> DetailedLogger:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if DetailedLogger._instance_initialized:
            return
        if self._initialized:
            return
        self.config              = LoggerConfig()
        self.log_file: Optional[Any] = None
        self._lock               = threading.Lock()   # thread-safety
        self._feedback_lock      = threading.Lock()   # ✅ v0.2.0: lock propio del feedback
        self.start_time: float   = 0.0
        self.execution_stack: List[Dict[str, Any]] = []
        # contadores
        self.error_count         = 0
        self.function_count      = 0
        self.file_access_count   = 0
        self.model_calls         = 0
        self.memory_ops          = 0
        self.router_decisions    = 0
        self.agent_executions    = 0
        self.feedback_count      = 0
        self.cb_state_changes    = 0
        self._initialized        = True
        DetailedLogger._instance_initialized = True

    # ✅ v0.2.0: reset completo del singleton — permite reinicializar en
    # tests/entornos con múltiples ciclos (antes _instance_initialized
    # quedaba True para siempre y un segundo initialize() fallaba).
    @classmethod
    def _reset_singleton(cls) -> None:
        cls._instance = None
        cls._instance_initialized = False

    # ─────────────────────────────────────────────────────────────────────────
    # ESCRITURA SEGURA — nunca levanta excepciones hacia AXIOMA
    # ─────────────────────────────────────────────────────────────────────────
    def _safe_write(self, text: str, flush: bool = False) -> None:
        """Escritura no-bloqueante: si falla el disco, cae a stderr y sigue."""
        if not self.log_file:
            return
        try:
            with self._lock:
                self.log_file.write(text)
                if flush:
                    self.log_file.flush()
        except Exception as exc:
            try:
                print(f"[DetailedLogger._safe_write] I/O error: {exc}", file=sys.stderr)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:168] excepción degradada (intencional): {_d2e}")

    def _safe_flush(self) -> None:
        try:
            if self.log_file:
                with self._lock:
                    self.log_file.flush()
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:176] excepción degradada (intencional): {_d2e}")

    # ─────────────────────────────────────────────────────────────────────────
    # INICIALIZACIÓN
    # ─────────────────────────────────────────────────────────────────────────
    def _validate_logs_directory(self) -> bool:
        # ✅ v0.2.0: auto-crear el directorio de logs si no existe (antes
        # exigía `mkdir -p` manual y fallaba en checkouts nuevos).
        try:
            self.config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"❌ ERROR: No se pudo crear {self.config.LOGS_DIR}: {e}", file=sys.stderr)
            return False
        if not self.config.LOGS_DIR.is_dir():
            print(f"❌ ERROR: {self.config.LOGS_DIR} no es un directorio", file=sys.stderr)
            return False
        if not os.access(str(self.config.LOGS_DIR), os.W_OK):
            print(f"❌ ERROR: Sin permisos de escritura en {self.config.LOGS_DIR}", file=sys.stderr)
            return False
        return True

    def _rotate_logs(self) -> None:
        try:
            if self.config.BACKUP_FILE.exists():
                self.config.BACKUP_FILE.unlink()
            if self.config.OUTPUT_FILE.exists():
                self.config.OUTPUT_FILE.rename(self.config.BACKUP_FILE)
        except Exception as e:
            print(f"⚠️  Warning: Error en rotación de logs: {e}", file=sys.stderr)

    def initialize(self, output_file: Optional[Path] = None, rotate: bool = True) -> bool:
        try:
            if output_file:
                self.config.OUTPUT_FILE = Path(output_file).resolve()
                self.config.BACKUP_FILE = self.config.OUTPUT_FILE.parent / "reg_error_old.txt"
                self.config.LOGS_DIR    = self.config.OUTPUT_FILE.parent
            if not self._validate_logs_directory():
                return False
            if rotate:
                self._rotate_logs()
            self.log_file   = open(self.config.OUTPUT_FILE, 'a', encoding=self.config.ENCODING, buffering=1)
            self.start_time = time.time()
            self._write_header()
            self.log_event("SYSTEM", f"Logger v{__version__} inicializado")
            self.log_event("CONFIG", f"Archivo: {self.config.OUTPUT_FILE.absolute()}")
            self.log_event("CONFIG", f"Feedback: {self.config.FEEDBACK_FILE.absolute()}")
            self._safe_flush()
            return True
        except PermissionError as e:
            print(f"❌ ERROR permisos logger: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"❌ ERROR logger: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return False

    def _write_header(self) -> None:
        header = (
            "\n╔══════════════════════════════════════════════════════════════════════════════╗\n"
            f"║              AXIOMA — REGISTRO DETALLADO DE EJECUCIÓN v{__version__:<22}║\n"
            "╠══════════════════════════════════════════════════════════════════════════════╣\n"
        )
        self._safe_write(header)
        now = datetime.datetime.now().isoformat()
        self._safe_write(f"║ Fecha Inicio: {now:<58}║\n")
        self._safe_write(f"║ PID:          {os.getpid():<58}║\n")
        self._safe_write(f"║ Python:       {sys.version.split()[0]:<58}║\n")
        self._safe_write(
            "╠══════════════════════════════════════════════════════════════════════════════╣\n",
            flush=True,
        )

    def _write_footer(self) -> None:
        duration = time.time() - self.start_time
        lines = [
            "\n╠══════════════════════════════════════════════════════════════════════════════╣\n",
            "║                           RESUMEN DE EJECUCIÓN                                ║\n",
            "╠══════════════════════════════════════════════════════════════════════════════╣\n",
            f"║ Duración Total:        {duration:.2f}s{' ' * 50}║\n",
            f"║ Funciones ejecutadas:  {self.function_count:<50}║\n",
            f"║ Llamadas a modelos:    {self.model_calls:<50}║\n",
            f"║ Operaciones memoria:   {self.memory_ops:<50}║\n",
            f"║ Decisiones router:     {self.router_decisions:<50}║\n",
            f"║ Ejecuciones agente:    {self.agent_executions:<50}║\n",
            f"║ Circuit breakers:      {self.cb_state_changes:<50}║\n",
            f"║ Feedback registrado:   {self.feedback_count:<50}║\n",
            f"║ Errores registrados:   {self.error_count:<50}║\n",
            f"║ Fecha Fin:             {datetime.datetime.now().isoformat():<50}║\n",
            "╚══════════════════════════════════════════════════════════════════════════════╝\n",
        ]
        for line in lines:
            self._safe_write(line)
        self._safe_flush()

    def close(self) -> None:
        if self.log_file:
            try:
                self.log_event("SYSTEM", "Cerrando logger...")
                self._write_footer()
            finally:
                try:
                    self._safe_flush()
                    self.log_file.close()
                except Exception as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:280] excepción degradada (intencional): {_d2e}")
                self.log_file = None
                print(f"✅ Registro guardado: {self.config.OUTPUT_FILE.absolute()}")
                if self.config.BACKUP_FILE.exists():
                    print(f"📁 Backup: {self.config.BACKUP_FILE.absolute()}")
                if self.config.FEEDBACK_FILE.exists():
                    print(f"📊 Feedback: {self.config.FEEDBACK_FILE} ({self.feedback_count} registros)")

    @property
    def is_initialized(self) -> bool:
        return self.log_file is not None

    def reset(self) -> None:
        """✅ v0.2.0: cierra el logger actual y reinicia el singleton —
        útil en tests con múltiples ciclos de initialize()."""
        try:
            if self.log_file:
                self.close()
        finally:
            DetailedLogger._reset_singleton()

    # ─────────────────────────────────────────────────────────────────────────
    # EVENTOS — escritura principal
    # ─────────────────────────────────────────────────────────────────────────
    def log_event(self, category: str, message: str, level: str = "INFO",
                  extra: Optional[Dict] = None) -> None:
        if not self.log_file:
            return
        ts       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        extra_s  = f" | {json.dumps(extra, ensure_ascii=False, default=str)}" if extra else ""
        line     = f"[{ts}] [{level:8}] [{category:20}] {message}{extra_s}\n"
        flush    = level in ("ERROR", "CRITICAL")
        self._safe_write(line, flush=flush)

    def log_warning(self, category: str, message: str, extra: Optional[Dict] = None) -> None:
        """Conveniencia para nivel WARNING."""
        self.log_event(category, message, level="WARNING", extra=extra)

    # ─────────────────────────────────────────────────────────────────────────
    # ERRORES — captura efectiva y no-bloqueante
    # ─────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _error_id(error: BaseException) -> str:
        """Hash corto para identificar/deduplicar errores repetidos."""
        tb  = error.__traceback__
        loc = ""
        while tb and tb.tb_next:
            tb = tb.tb_next
        if tb:
            loc = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"
        raw = f"{type(error).__name__}:{str(error)[:80]}:{loc}"
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:8]

    @staticmethod
    def _get_frame_locals(error: BaseException, max_vars: int = 10) -> Dict[str, str]:
        """
        Captura variables locales del frame donde ocurrió el error.

        ✅ FIX AUDITORÍA (S6): cada valor pasa por _redact_value() antes de
        devolverse — redacta si el nombre de variable sugiere un secreto
        (key/token/secret/password/credential/auth) o si el contenido
        matchea un patrón de secreto conocido. La captura en sí NO se
        desactiva: sigue siendo el detalle que necesitamos para diagnosticar
        errores reales de AXIOMA/Rafael.
        """
        tb = error.__traceback__
        if not tb:
            return {}
        while tb.tb_next:
            tb = tb.tb_next
        try:
            raw   = tb.tb_frame.f_locals
            items = list(raw.items())[-max_vars:]
            result = {}
            for k, v in items:
                if k.startswith("__"):
                    continue
                try:
                    s = repr(v)
                    s = s[:120] + "..." if len(s) > 120 else s
                    result[k] = _redact_value(k, s)
                except Exception:
                    result[k] = "<repr error>"
            return result
        except Exception:
            return {}

    def log_error(self, error: BaseException, context: str = "",
                  extra_info: Optional[Dict] = None,
                  file_path: str = "", line_number: Optional[int] = None,
                  function_name: str = "") -> None:
        """
        Registra error con:
        - Traceback completo (cadena __cause__ / __context__)
        - Dump del execution_stack activo (qué bloque se estaba ejecutando)
        - Variables locales del frame del error
        - error_id (hash) para deduplicar errores repetidos en los logs
        Siempre hace flush. Nunca lanza excepción hacia AXIOMA.
        """
        self.error_count += 1

        # Siempre intentar flush aunque ENABLE_ERROR_DETAILS=False
        if not self.log_file:
            # Fallback a stderr si no hay archivo
            print(f"[AXIOMA ERROR] {type(error).__name__}: {error} | ctx={context}", file=sys.stderr)
            return

        if not self.config.ENABLE_ERROR_DETAILS:
            self._safe_write(
                f"[ERROR] {type(error).__name__}: {error} | ctx={context}\n",
                flush=True,
            )
            return

        error_id = self._error_id(error)

        sep  = "═" * 78
        body = [
            f"\n{sep}\n",
            f"[ERROR #{self.error_count}] id={error_id}\n",
            f"  Tipo:     {type(error).__name__}\n",
            f"  Mensaje:  {error}\n",
            f"  Contexto: {context}\n",
        ]
        if file_path:
            body.append(f"  Archivo:  {file_path}\n")
        if line_number:
            body.append(f"  Línea:    {line_number}\n")
        if function_name:
            body.append(f"  Función:  {function_name}\n")

        # Execution stack activo
        if self.execution_stack:
            body.append("  Stack de ejecución activo:\n")
            for frame in self.execution_stack:
                elapsed = time.time() - frame.get("start", time.time())
                body.append(f"    → {frame['name']} ({elapsed:.2f}s)\n")

        # Extra info
        if extra_info:
            body.append("  Info adicional:\n")
            for k, v in extra_info.items():
                # ✅ FIX AUDITORÍA (S6): mismo criterio de redacción que
                # _get_frame_locals — extra_info es un dict arbitrario que
                # cualquier caller puede pasar (ej. {"api_key": "..."}).
                body.append(f"    {k}: {_redact_value(k, str(v))}\n")

        # Variables locales del frame del error
        if self.config.ENABLE_LOCAL_VARS:
            local_vars = self._get_frame_locals(error, self.config.MAX_LOCAL_VARS)
            if local_vars:
                body.append("  Variables locales (frame del error):\n")
                for k, v in local_vars.items():
                    body.append(f"    {k} = {v}\n")

        # Traceback principal
        body.append("\n--- TRACEBACK ---\n")
        try:
            tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
            for line in tb_lines[:self.config.MAX_TRACEBACK_LINES]:
                body.append(f"  {line}")
            if len(tb_lines) > self.config.MAX_TRACEBACK_LINES:
                body.append(f"  ... ({len(tb_lines) - self.config.MAX_TRACEBACK_LINES} líneas omitidas)\n")
        except Exception as tb_err:
            body.append(f"  <error al formatear traceback: {tb_err}>\n")

        # Cadena de causa explícita (__cause__ / __context__)
        cause   = getattr(error, "__cause__", None)
        context_ = getattr(error, "__context__", None)
        if cause and cause is not error:
            body.append("\n--- CAUSA DIRECTA (raise X from Y) ---\n")
            try:
                for line in traceback.format_exception(type(cause), cause, cause.__traceback__):
                    body.append(f"  {line}")
            except Exception:
                body.append(f"  {type(cause).__name__}: {cause}\n")
        elif context_ and context_ is not error:
            body.append("\n--- EXCEPCIÓN DE CONTEXTO (durante el manejo de otra) ---\n")
            try:
                for line in traceback.format_exception(type(context_), context_, context_.__traceback__):
                    body.append(f"  {line}")
            except Exception:
                body.append(f"  {type(context_).__name__}: {context_}\n")

        body.append(f"--- FIN ERROR #{self.error_count} ---\n{sep}\n")

        for chunk in body:
            self._safe_write(chunk)
        self._safe_flush()

    # ─────────────────────────────────────────────────────────────────────────
    # MÉTRICAS ESPECÍFICAS
    # ─────────────────────────────────────────────────────────────────────────
    def log_function_call(self, func_name: str, module: str, file_path: str = "",
                          line_number: int = 0, args: tuple = None,
                          kwargs: dict = None, start_time: float = None) -> None:
        if not self.config.ENABLE_FUNCTION_TRACE or not self.log_file:
            return
        self.function_count += 1
        args_s = ""
        if args:
            args_s = f"args={len(args)}"
        if kwargs:
            args_s += f", kwargs={len(kwargs)}" if args_s else f"kwargs={len(kwargs)}"
        info: Dict[str, Any] = {"module": module, "file": file_path, "line": line_number}
        if start_time:
            info["start_time"] = datetime.datetime.fromtimestamp(start_time).isoformat()
        self.log_event("FUNCTION", f"CALL: {module}.{func_name}({args_s})", extra=info)

    def log_function_return(self, func_name: str, module: str, duration: float,
                            success: bool = True, result_info: Optional[Dict] = None) -> None:
        if not self.config.ENABLE_FUNCTION_TRACE or not self.log_file:
            return
        info: Dict[str, Any] = {"duration_ms": round(duration * 1000, 2), "success": success}
        if result_info:
            info.update(result_info)
        level = "INFO" if success else "WARNING"
        self.log_event("FUNCTION",
                       f"RETURN: {module}.{func_name} [{'✓' if success else '✗'}] {duration:.4f}s",
                       level=level, extra=info)

    def log_file_access(self, file_path: str, mode: str = "read", operation: str = "",
                        size: Optional[int] = None, exists: bool = True, caller: str = "") -> None:
        if not self.config.ENABLE_FILE_ACCESS or not self.log_file:
            return
        self.file_access_count += 1
        info: Dict[str, Any] = {"path": str(file_path), "mode": mode, "exists": exists}
        if operation:
            info["operation"] = operation
        if size is not None:
            info["size_bytes"] = size
        if caller:
            info["caller"] = caller
        self.log_event("FILE_ACCESS", f"[{mode.upper():5}] {file_path}", extra=info)

    def log_model_call(self, model_name: str, action: str, task_type: str = "",
                       input_size: Optional[int] = None, output_size: Optional[int] = None,
                       duration: Optional[float] = None, success: bool = True,
                       temperature: Optional[float] = None, tokens_used: Optional[int] = None,
                       error_type: Optional[str] = None) -> None:
        if not self.config.ENABLE_MODEL_TRACE or not self.log_file:
            return
        self.model_calls += 1
        info: Dict[str, Any] = {"model": model_name, "action": action,
                                 "task_type": task_type, "success": success}
        if input_size  is not None: info["input_tokens"]    = input_size
        if output_size is not None: info["output_tokens"]   = output_size
        if duration    is not None: info["duration_ms"]     = round(duration * 1000, 2)
        if temperature is not None: info["temperature"]     = temperature
        if tokens_used is not None: info["tokens_used"]     = tokens_used
        if not success and error_type:
            info["error_type"] = error_type
        level = "INFO" if success else "ERROR"
        self.log_event("MODEL",
                       f"[{'✓' if success else '✗'}] {model_name} — {action} — {task_type}",
                       level=level, extra=info)

    def log_memory_operation(self, operation: str, collection: str = "",
                             session_id: str = "", items_count: Optional[int] = None,
                             duration: Optional[float] = None, success: bool = True,
                             db_path: str = "") -> None:
        if not self.config.ENABLE_MEMORY_TRACE or not self.log_file:
            return
        self.memory_ops += 1
        info: Dict[str, Any] = {"operation": operation, "collection": collection,
                                 "session_id": session_id, "success": success}
        if items_count is not None: info["items_count"]    = items_count
        if duration    is not None: info["duration_ms"]    = round(duration * 1000, 2)
        if db_path:                 info["db_path"]        = db_path
        level = "INFO" if success else "ERROR"
        self.log_event("MEMORY", f"[{'✓' if success else '✗'}] {operation} — {collection}",
                       level=level, extra=info)

    def log_router_decision(self, query: str, task_type: str, model_selected: str,
                            confidence: float, cache_hit: bool = False,
                            duration: Optional[float] = None, api_required: bool = False) -> None:
        if not self.config.ENABLE_ROUTER_TRACE or not self.log_file:
            return
        self.router_decisions += 1
        info: Dict[str, Any] = {
            "query_preview":  query[:50] if query else "",
            "task_type":      task_type,
            "model_selected": model_selected,
            "confidence":     round(confidence, 3),
            "cache_hit":      cache_hit,
            "api_required":   api_required,
        }
        if duration is not None:
            info["duration_ms"] = round(duration * 1000, 2)
        self.log_event("ROUTER",
                       f"DECISION: {task_type} → {model_selected} (conf={confidence:.2f})",
                       extra=info)

    def log_agent_execution(self, agent_name: str, task_type: str,
                            state: str = "started", duration: Optional[float] = None,
                            success: bool = True, error: Optional[str] = None) -> None:
        if not self.config.ENABLE_AGENT_TRACE or not self.log_file:
            return
        self.agent_executions += 1
        info: Dict[str, Any] = {"agent": agent_name, "task_type": task_type,
                                  "state": state, "success": success}
        if duration is not None: info["duration_ms"] = round(duration * 1000, 2)
        if error:                info["error"]       = error
        level = "INFO" if success else "ERROR"
        self.log_event("AGENT",
                       f"[{'✓' if success else '✗'}] {agent_name} — {task_type} — {state}",
                       level=level, extra=info)

    def log_circuit_breaker(self, breaker_name: str, new_state: str,
                            failure_count: int = 0, last_error: str = "") -> None:
        """
        Registra cambio de estado del CircuitBreaker.
        new_state: 'CLOSED' | 'OPEN' | 'HALF_OPEN'
        """
        if not self.config.ENABLE_CB_TRACE or not self.log_file:
            return
        self.cb_state_changes += 1
        icon  = {"OPEN": "🔴", "CLOSED": "🟢", "HALF_OPEN": "🟡"}.get(new_state, "⚪")
        info: Dict[str, Any] = {"breaker": breaker_name, "state": new_state,
                                  "failure_count": failure_count}
        if last_error:
            info["last_error"] = last_error[:200]
        level = "WARNING" if new_state == "OPEN" else "INFO"
        self.log_event("CIRCUIT_BREAKER",
                       f"{icon} {breaker_name} → {new_state} (failures={failure_count})",
                       level=level, extra=info)

    def log_feedback(self, user_id: str, request: str, response_id: str,
                     was_helpful: bool, feedback_text: str = "",
                     ambiguity_level: str = "none", task_type: str = "",
                     model_used: str = "", latency_ms: float = 0.0,
                     session_id: str = "") -> None:
        if not self.config.ENABLE_FEEDBACK_TRACE:
            return
        self.feedback_count += 1
        record = {
            "timestamp":       datetime.datetime.now().isoformat(),
            "user_id":         user_id,
            "session_id":      session_id,
            "request":         request[:500],
            "response_id":     response_id,
            "was_helpful":     was_helpful,
            "feedback_text":   feedback_text[:1000],
            "ambiguity_level": ambiguity_level,
            "task_type":       task_type,
            "model_used":      model_used,
            "latency_ms":      round(latency_ms, 2),
        }
        try:
            # ✅ v0.2.0: lock propio del feedback — antes se abría el archivo
            # sin lock y dos threads escribiendo feedback a la vez podían
            # intercalar líneas JSON (corrompiendo feedback_log.jsonl).
            with self._feedback_lock:
                with open(self.config.FEEDBACK_FILE, 'a', encoding=self.config.ENCODING) as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
        except Exception as e:
            try:
                print(f"[DetailedLogger.log_feedback] Error escribiendo feedback: {e}", file=sys.stderr)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:640] excepción degradada (intencional): {_d2e}")
        self.log_event("FEEDBACK",
                       f"[{'✓' if was_helpful else '✗'}] {task_type} | model={model_used}",
                       extra={"user_id": user_id, "was_helpful": was_helpful,
                              "ambiguity_level": ambiguity_level, "latency_ms": round(latency_ms, 2)})

    # ─────────────────────────────────────────────────────────────────────────
    # CONTEXT MANAGER
    # ─────────────────────────────────────────────────────────────────────────
    @contextmanager
    def track_execution(self, name: str, context: str = "", extra: Optional[Dict] = None):
        start = time.time()
        depth = len(self.execution_stack)
        self.log_event("EXECUTION", f"{'  ' * depth}INICIO: {name}",
                       extra={"context": context, "depth": depth, **(extra or {})})
        self.execution_stack.append({"name": name, "start": start})
        try:
            yield
            duration = time.time() - start
            self.log_event("EXECUTION", f"{'  ' * depth}FIN ✓: {name}",
                           extra={"duration_ms": round(duration * 1000, 2)})
        except Exception as e:
            duration = time.time() - start
            self.log_event("EXECUTION", f"{'  ' * depth}FIN ✗: {name}",
                           level="ERROR", extra={"duration_ms": round(duration * 1000, 2)})
            self.log_error(e, context=f"En ejecución: {name}")
            raise
        finally:
            if self.execution_stack:
                self.execution_stack.pop()


# ============================================================================
# DECORADORES
# ============================================================================
def track_function(logger: DetailedLogger):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            module    = func.__module__
            func_name = func.__name__
            file_path = ""
            line_num  = 0
            try:
                file_path = inspect.getfile(func)
                line_num  = func.__code__.co_firstlineno
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:687] excepción degradada (intencional): {_d2e}")
            start = time.time()
            # ✅ v0.2.0: los kwargs se redactan por nombre (key/token/secret/
            # password/credential/auth) — antes se pasaban crudos y un
            # kwargs={"api_key": "sk-..."} quedaba en el log.
            safe_kwargs = None
            if kwargs:
                safe_kwargs = {
                    k: _redact_value(k, repr(v)) for k, v in kwargs.items()
                }
            logger.log_function_call(func_name, module, file_path, line_num, args, safe_kwargs, start)
            try:
                result = func(*args, **kwargs)
                logger.log_function_return(func_name, module, time.time() - start, success=True)
                return result
            except Exception as e:
                logger.log_function_return(func_name, module, time.time() - start, success=False)
                logger.log_error(e, context=f"{module}.{func_name}",
                                 file_path=file_path, line_number=line_num,
                                 function_name=func_name)
                raise
        return wrapper
    return decorator


# ============================================================================
# API PÚBLICA
# ============================================================================
def get_logger() -> DetailedLogger:
    return DetailedLogger()

class _StdlibToFileHandler(logging.Handler):
    """Vuelca WARNING+ del `logging` estándar al archivo del DetailedLogger.

    ✅ ISSUE-062: `log_degraded()` (y cualquier `logger.warning/error`) escribe
    por el `logging` de la stdlib, que **no** llega a `logs/reg_error.txt` (el
    DetailedLogger solo guarda SUS eventos). Consecuencia real: un
    `UnboundLocalError` de contrato ocurrido en vivo quedó visible en la consola
    pero el log decía "Errores registrados: 0" → el health report no podía verlo.
    Este handler emite el MISMO formato de línea que el resto del archivo, así
    que `tools/log_health_report.py` lo parsea y lo cuenta.
    """

    def __init__(self, path: Path):
        super().__init__(level=logging.WARNING)
        self._path = Path(path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            # ⚠️ La categoría va en MAYÚSCULAS: el parser del health report
            # (`_LINE_RE`) acepta solo `[A-Z_0-9]+` — con minúsculas la línea se
            # ignoraba en silencio.
            categoria = (record.name or "STDLIB").split(".")[-1].upper()[:20]
            mensaje = record.getMessage()
            if record.exc_info:
                mensaje += " | " + "".join(
                    traceback.format_exception(*record.exc_info)).replace("\n", " ⏎ ")
            line = f"[{ts}] [{record.levelname:8}] [{categoria:20}] {mensaje}\n"
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass    # el logging nunca debe romper la app


_STDLIB_HANDLER: Optional[_StdlibToFileHandler] = None


def attach_stdlib_handler(output_file: Optional[Path] = None) -> bool:
    """Engancha WARNING+ del logging estándar al archivo de log (una sola vez)."""
    global _STDLIB_HANDLER
    try:
        if _STDLIB_HANDLER is not None:
            return True
        path = Path(output_file) if output_file else LoggerConfig.OUTPUT_FILE
        handler = _StdlibToFileHandler(path)
        logging.getLogger().addHandler(handler)
        _STDLIB_HANDLER = handler
        return True
    except Exception:
        return False


def initialize_logger(output_file: Optional[Path] = None, rotate: bool = True) -> bool:
    ok = get_logger().initialize(output_file, rotate=rotate)
    if ok:
        # ✅ ISSUE-062: que las degradaciones/errores de contrato queden EN EL LOG
        attach_stdlib_handler(output_file)
    if ok:
        # ✅ v0.2.0: registrar el cierre en atexit — si el proceso muere sin
        # close_logger() explícito (crash, sys.exit, signal), el footer y el
        # flush final igual se escriben.
        _register_atexit(get_logger())
    return ok

_ATEXIT_REGISTERED = False


def _register_atexit(logger: DetailedLogger) -> None:
    """Registra close() una sola vez en atexit (idempotente)."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    _ATEXIT_REGISTERED = True
    atexit.register(lambda: (logger.close() if logger.is_initialized else None))

def close_logger() -> None:
    get_logger().close()

def get_log_files() -> List[Path]:
    logs_dir = LoggerConfig.LOGS_DIR
    if not logs_dir.exists():
        return []
    return sorted(logs_dir.glob("reg_error*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)

def get_feedback_records(limit: int = 100) -> List[Dict]:
    path = LoggerConfig.FEEDBACK_FILE
    if not path.exists():
        return []
    records = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:759] excepción degradada (intencional): {_d2e}")
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 detailed_logger.py:761] excepción degradada (intencional): {_d2e}")
    return records[-limit:]

def cleanup_old_logs(max_files: int = 2) -> None:
    files = get_log_files()
    for old_file in files[max_files:]:
        try:
            old_file.unlink()
            print(f"🗑️  Eliminado: {old_file}")
        except Exception as e:
            print(f"⚠️  No se pudo eliminar {old_file}: {e}")


# ═══════════════════════════════════════════════════════════════
# PODA DE REPORTES GENERADOS (v0.6.9v / ISSUE-058)
# ═══════════════════════════════════════════════════════════════
# Cada herramienta que escribe un reporte en `logs/` lo acumulaba sin límite
# (el auditor llegó a 51 archivos). Esta es la implementación ÚNICA: vive acá
# (hub de logging, archivo existente) y la usan el auditor, la eval de
# regresión, las sondas de visión y los reportes de test/health.
REPORTS_KEEP_DEFAULT = 2


def prune_reports(pattern: str, keep: int = REPORTS_KEEP_DEFAULT,
                  logs_dir: Optional[Path] = None,
                  protect: Optional[Path] = None) -> int:
    """Borra los reportes que sobran (más viejos por mtime). Devuelve cuántos.

    Args:
        pattern: glob relativo a `logs/` (p. ej. `axioma_eval_*.md`).
        keep: cuántos conservar (los más recientes por mtime).
        logs_dir: directorio (por defecto `LoggerConfig.LOGS_DIR`).
        protect: archivo que NUNCA se borra (el recién escrito, por si relojes
            raros lo dejaran fuera de los `keep` más nuevos).

    Nunca lanza: la limpieza no puede romper la herramienta que la llama.
    """
    try:
        d = Path(logs_dir) if logs_dir else LoggerConfig.LOGS_DIR
        reports = sorted(d.glob(pattern),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        restantes = keep
        if protect is not None:
            protect = Path(protect)
            if protect in reports:
                reports.remove(protect)
                restantes = max(0, keep - 1)
        removed = 0
        for old in reports[restantes:]:
            try:
                old.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0


# ============================================================================
# MAIN — TEST DEL LOGGER
# ============================================================================
if __name__ == "__main__":
    print(f"🧪 Testing DetailedLogger AXIOMA v{__version__}...")

    if not LoggerConfig.LOGS_DIR.exists():
        print(f"❌ ERROR: {LoggerConfig.LOGS_DIR}")
        print(f"   Ejecuta: mkdir -p {LoggerConfig.LOGS_DIR}")
        sys.exit(1)

    cleanup_old_logs(max_files=2)
    logger = DetailedLogger()
    test_file = LoggerConfig.LOGS_DIR / "test_reg_error.txt"

    if not initialize_logger(test_file, rotate=True):
        print("❌ ERROR: No se pudo inicializar el logger")
        sys.exit(1)

    with logger.track_execution("Test Block", "Prueba completa del logger"):
        # Evento genérico
        logger.log_event("TEST", "Evento de prueba")

        # Función
        logger.log_function_call("test_func", "__main__", str(test_file), 1)
        logger.log_function_return("test_func", "__main__", 0.05, success=True)

        # Modelo — usa modelos reales instalados
        logger.log_model_call(
            model_name="qwen3:8b",
            action="generate", task_type="GENERAL_CHAT",
            input_size=50, output_size=120, duration=1.2,
        )
        logger.log_model_call(
            model_name="qwen2.5-coder:7b",
            action="generate", task_type="CODE_GENERATION",
            input_size=80, output_size=200, duration=2.5,
        )
        logger.log_model_call(
            model_name="gemma4:e4b",
            action="vision", task_type="IMAGE_ANALYSIS",
            duration=3.1, success=False,
            error_type="OllamaTimeoutError",
        )

        # Memoria
        logger.log_memory_operation("save_message", "axioma_sessions",
                                    session_id="test_001", items_count=1, duration=0.003)

        # Router
        logger.log_router_decision(
            query="escribe un script de python",
            task_type="code_generation",
            model_selected="qwen2.5-coder:7b",
            confidence=0.94, duration=0.008,
        )

        # Agente
        logger.log_agent_execution("CoderAgent", "code_generation", "completed",
                                   duration=2.5, success=True)

        # Circuit Breaker
        logger.log_circuit_breaker("ollama_client", "OPEN",
                                   failure_count=5, last_error="ConnectionRefusedError")
        logger.log_circuit_breaker("ollama_client", "HALF_OPEN", failure_count=5)
        logger.log_circuit_breaker("ollama_client", "CLOSED", failure_count=0)

        # Feedback
        logger.log_feedback(
            user_id="user_test_001",
            request="función para sumar dos números",
            response_id="resp_test_001",
            was_helpful=True,
            task_type="CODE_GENERATION",
            model_used="qwen2.5-coder:7b",
            latency_ms=1250.5,
            session_id="session_test_001",
        )

        # Error con cadena de causa
        try:
            try:
                raise ValueError("Error original: división inválida")
            except ValueError as original:
                raise RuntimeError("Error derivado al procesar respuesta") from original
        except RuntimeError as e:
            logger.log_error(e, context="test_error_chain",
                             extra_info={"fase": "test", "intento": 1})

        time.sleep(0.05)

    close_logger()

    print("\n📁 Archivos de log:")
    for f in get_log_files():
        print(f"   {f.name} ({f.stat().st_size:,} bytes)")
    print(f"\n✅ Test completado: {test_file.absolute()}")
