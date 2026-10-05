#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# interpreter_loop.py - Bucle autónomo de ejecución de código (InterpreterLoop)
# Ruta: /ruta/a/axioma/src/core/interpreter_loop.py
# Autor: SaintWick
# Versión: 1.8.0
#
# Implementa el bucle Code → Execute → Observe → Fix para ejecución autónoma
# de código generado por Rafael Engine. Soporta workspace persistente,
# auto-resolución de dependencias, validación factual y trazabilidad con
# TraceStore. Reemplaza el antiguo paso por terminal_exec (SecureTerminalTool)
# por escritura directa y SandboxExecutor para evitar problemas de shell.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.domain.entities import Message, MessageRole
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

try:
    from src.core.state_emitter import emit_state as _emit_state
except ImportError:
    def _emit_state(state: str) -> None:  # type: ignore[misc]
        pass

# ✅ FIX AUDITORÍA v1.8.0 (S2/S8): ExecutionContext/ToolPermission ya no se
# usan — _sandbox_exec() dejó de pasar por ToolExecutor/terminal_exec (ver
# el método más abajo). Solo queda ToolResult, como tipo de retorno.
try:
    from src.tools_mcp.tool_executor import ToolResult
except ImportError:
    ToolResult = None  # type: ignore[misc, assignment]

# ✅ v1.7.0: TraceStore — import seguro
try:
    from src.learning.trace_store import get_trace_store, Trace as _Trace
    _TRACE_STORE_AVAILABLE = True
except ImportError:
    _TRACE_STORE_AVAILABLE = False
    get_trace_store = None  # type: ignore[assignment]
    _Trace = None           # type: ignore[assignment]

try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_loop(event: str, **kwargs: Any) -> None:
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = _get_detailed_logger()
        if getattr(log, "is_initialized", False):
            log.log_event("INTERPRETER_LOOP", event, extra=kwargs)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 interpreter_loop.py:67] excepción degradada (intencional): {_d2e}")


# ============================================================================
# CONFIGURACIÓN
# ============================================================================

@dataclass
class LoopConfig:
    """Parámetros del bucle autónomo."""
    max_fix_attempts: int = 5
    sandbox_tool_name: str = "terminal_exec"
    sandbox_timeout: float = 45.0
    # ✅ FIX v0.6.8 (hallazgo 3.2): era 120s. En CPU, UNA generación del
    # CoderAgent tarda ~110s (qwen2.5-coder:7b, sin GPU) — con 120s el
    # global_timeout cortaba después de 1-2 intentos (log real: 2 intentos
    # en 223.8s) antes de que el ciclo code→execute→fix pudiera completar
    # nada. 300s permite los 5 intentos de fix completos.
    global_timeout: float = 300.0


# ============================================================================
# TRAZAS
# ============================================================================

@dataclass
class StepTrace:
    """Registro de un intento code→execute→(fix)."""
    attempt: int
    code: str
    code_lines: int
    tool_result: Optional[ToolResult] = None
    success: bool = False
    error_observed: Optional[str] = None
    duration_ms: float = 0.0
    generation_ms: float = 0.0
    execution_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "code_lines": self.code_lines,
            "success": self.success,
            "error": self.error_observed[:200] if self.error_observed else None,
            "duration_ms": round(self.duration_ms, 2),
            "generation_ms": round(self.generation_ms, 2),
            "execution_ms": round(self.execution_ms, 2),
        }


# ============================================================================
# RESULTADO FINAL
# ============================================================================

@dataclass
class InterpreterResult:
    """Resultado del bucle autónomo completo."""
    success: bool
    final_output: str
    final_code: str
    traces: List[StepTrace]
    total_attempts: int
    duration_ms: float
    stop_reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "final_output": self.final_output[:1000] if self.final_output else "",
            "final_code_lines": len(self.final_code.splitlines()) if self.final_code else 0,
            "total_attempts": self.total_attempts,
            "duration_ms": round(self.duration_ms, 2),
            "stop_reason": self.stop_reason,
            "traces": [t.to_dict() for t in self.traces],
        }

    def to_response(self, session_id: Optional[str] = None) -> Any:
        """Convierte a Response de AXIOMA (import seguro)."""
        try:
            from src.domain.entities import Response
            if self.success:
                content = (
                    f"✨ [Rafael Engine — "
                    f"{self.total_attempts} intento"
                    f"{'s' if self.total_attempts != 1 else ''}]\n\n"
                    f"{self.final_output}"
                )
            else:
                content = (
                    f"❌ [Rafael Engine — intentos agotados]\n\n"
                    f"{self.final_output}"
                )
            # v1.3.0: Mencionar archivos generados en el workspace
            files = self.metadata.get("generated_files", [])
            if files:
                file_lines = "\n\n📁 Archivos generados en el workspace:\n"
                for f in files:
                    size_kb = f["size_bytes"] / 1024
                    file_lines += f"  - {f['name']} ({size_kb:.1f} KB)\n"
                content = content + file_lines
            return Response(
                content=content,
                session_id=session_id,
                model_used="interpreter_loop",
                task_type="code_autonomous",
                latency_ms=self.duration_ms,
                confidence=0.9 if self.success else 0.3,
                validation_passed=self.success,
                error=None if self.success else f"Autonomous mode: {self.stop_reason}",
                metadata={"interpreter_result": self.to_dict()},
            )
        except Exception as e:
            log_degraded(logger, e, "src/core/interpreter_loop.py:to_response")
            return self


# ============================================================================
# INTERPRETER LOOP
# ============================================================================

class InterpreterLoop:
    """
    Bucle autónomo estilo Open Interpreter.

    Flujo por iteración:
        1. CoderAgent.execute(input_msg=..., task_type=...) → código
        2. Extraer bloques ```python``` del resultado
        3. Pre-scan de imports → batch install (v1.6.0)
        4. SandboxExecutor.execute_script() directo (✅ v1.8.0 — antes
           pasaba por ToolExecutor.run("terminal_exec", command=...), ver
           _sandbox_exec() para el porqué del cambio) → ToolResult
        5. ToolResult.success → retornar output
        6. ToolResult.error → construir fix_message con stderr → reintentar
    """

    _CODE_RE = re.compile(r'```(?:python)?\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)

    def __init__(
        self,
        coder: Any,
        executor: Any,
        config: Optional[LoopConfig] = None,
        workspace_manager: Any = None,
        validator: Any = None,
    ):
        self.coder = coder
        # ✅ v1.8.0: ya no lo usa _sandbox_exec() (ver fix S2/S8) — se
        # mantiene como parámetro para no romper la firma del constructor.
        self.executor = executor
        self.config = config or LoopConfig()
        self._workspace = workspace_manager
        self._validator = validator
        if self._workspace:
            _log_loop("WORKSPACE_ATTACHED")
        _log_loop("INIT", sandbox_tool=self.config.sandbox_tool_name,
                  max_fix=self.config.max_fix_attempts)
        logger.info(
            f"InterpreterLoop init: max_fix={self.config.max_fix_attempts}, "
            f"tool={self.config.sandbox_tool_name}"
        )

    # ── Entrada principal ────────────────────────────────────────────────────

    async def run(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> InterpreterResult:
        t0 = time.perf_counter()
        traces: List[StepTrace] = []
        last_code = ""
        last_error: Optional[str] = None
        stop_reason = "max_fix_attempts"
        _emit_state("COMPUTING")

        for attempt in range(1, self.config.max_fix_attempts + 1):
            ta = time.perf_counter()

            if (ta - t0) >= self.config.global_timeout:
                stop_reason = "global_timeout"
                _log_loop("GLOBAL_TIMEOUT", elapsed_s=round(ta - t0, 1))
                break

            # ── 1. Generar código ────────────────────────────────────────────
            tg = time.perf_counter()
            try:
                msg = Message(
                    content=query if attempt == 1 else self._fix_prompt(query, last_code, last_error),
                    role=MessageRole.USER,
                    metadata={"autonomous_mode": True, "attempt": attempt},
                )
                kwargs = {"input_msg": msg, "task_type": "code_generation"}
                if attempt > 1 and last_error:
                    kwargs["previous_critique"] = last_error
                    kwargs["attempt"] = attempt

                result = await asyncio.to_thread(self.coder.execute, **kwargs)

                if not result.success or not result.content:
                    traces.append(StepTrace(
                        attempt=attempt, code="", code_lines=0,
                        error_observed=result.error or "CoderAgent sin contenido",
                        duration_ms=(time.perf_counter() - ta) * 1000,
                        generation_ms=(time.perf_counter() - tg) * 1000,
                    ))
                    stop_reason = "generation_error"
                    continue

                gen_ms = (time.perf_counter() - tg) * 1000

            except Exception as e:
                traces.append(StepTrace(
                    attempt=attempt, code="", code_lines=0,
                    error_observed=str(e),
                    duration_ms=(time.perf_counter() - ta) * 1000,
                ))
                stop_reason = "generation_error"
                logger.error(f"[InterpreterLoop] Gen error attempt {attempt}: {e}")
                continue

            # ── 2. Extraer código ────────────────────────────────────────────
            code = self._extract_code(result)
            if not code:
                traces.append(StepTrace(
                    attempt=attempt, code="", code_lines=0,
                    error_observed="No se extrajo código de la respuesta",
                    duration_ms=(time.perf_counter() - ta) * 1000,
                    generation_ms=gen_ms,
                ))
                stop_reason = "extraction_error"
                continue

            last_code = code
            n_lines = len(code.splitlines())

            # ── ✅ v0.6.8 (hallazgo 3.2): validación estática temprana ─────
            # Antes, un SyntaxError en el código generado se descubría recién
            # al ejecutar en sandbox — consumiendo un intento completo de
            # sandbox (y su latencia) para un error que `compile()` detecta
            # en microsegundos. Mismo criterio que CodeValidator._validate_static()
            # (Fase 3) y analyze_code_quality() (coder_core.py): compilar por
            # bloque, no concatenado. Si no compila → error de fix directo,
            # sin tocar el sandbox.
            _syntax_error = self._static_syntax_error(code)
            if _syntax_error:
                last_error = f"SyntaxError: {_syntax_error}"
                traces.append(StepTrace(
                    attempt=attempt, code=code, code_lines=n_lines,
                    error_observed=last_error,
                    duration_ms=(time.perf_counter() - ta) * 1000,
                    generation_ms=gen_ms, execution_ms=0.0,
                ))
                stop_reason = "syntax_error"
                _emit_state("HEALING")
                _log_loop("STATIC_SYNTAX_ERROR", attempt=attempt,
                          error=_syntax_error[:100])
                logger.info(
                    f"[InterpreterLoop] ⚠️ SyntaxError estático attempt "
                    f"{attempt} (sin sandbox): {_syntax_error[:80]}"
                )
                # ✅ v0.6.8pp: mismo código + mismo SyntaxError que el intento
                # anterior → el próximo fix es byte-idéntico (caché) → cortar.
                if (
                    len(traces) >= 2
                    and traces[-2].error_observed == last_error
                    and traces[-2].code == code
                ):
                    stop_reason = "no_progress"
                    _log_loop("NO_PROGRESS_STALL", attempt=attempt,
                              error=last_error[:120])
                    logger.warning(
                        f"[InterpreterLoop] ⛔ Sin progreso (syntax) attempt "
                        f"{attempt}: error y código idénticos — cortando"
                    )
                    break
                continue

            # ── v1.6.0: Pre-install de dependencias detectadas ──────────────
            if self._workspace and session_id:
                try:
                    from src.core.dependency_resolver import get_dependency_resolver
                    resolver = get_dependency_resolver()
                    modules = resolver.extract_all_imports(code)
                    packages = resolver.resolve_to_packages(modules)
                    if packages:
                        _log_loop("PRE_INSTALL", packages=packages)
                        logger.info(f"[InterpreterLoop] Pre-install: {packages}")
                        install_result = self._workspace.install_packages(
                            session_id or "default", packages
                        )
                        if install_result["success"]:
                            logger.info(f"[InterpreterLoop] Pre-install exitoso: {packages}")
                        elif install_result["packages"]:
                            logger.warning(
                                f"[InterpreterLoop] Pre-install falló para: {install_result['stderr'][:100]}"
                            )
                except Exception as e:
                    logger.debug(f"[InterpreterLoop] Pre-install falló: {e}")

            # ── 3. Ejecutar en sandbox ───────────────────────────────────────
            te = time.perf_counter()
            try:
                tr = await self._sandbox_exec(code, session_id=session_id, attempt=attempt)
                exec_ms = (time.perf_counter() - te) * 1000
            except Exception as e:
                exec_ms = (time.perf_counter() - te) * 1000
                last_error = f"Sandbox exception: {e}"
                traces.append(StepTrace(
                    attempt=attempt, code=code, code_lines=n_lines,
                    error_observed=last_error,
                    duration_ms=(time.perf_counter() - ta) * 1000,
                    generation_ms=gen_ms, execution_ms=exec_ms,
                ))
                stop_reason = "sandbox_unavailable"
                logger.error(f"[InterpreterLoop] Sandbox error: {e}")
                continue

            # ── 4. Observar resultado ────────────────────────────────────────
            dur_ms = (time.perf_counter() - ta) * 1000

            if tr.success:
                output = self._get_output(tr)

                # v1.8.0: Validación factual post-ejecución (Rafael Engine)
                if self._validator is not None:
                    try:
                        val_msg = Message(
                            content=f"Valida la salida de este código para la tarea: {query}",
                            role=MessageRole.USER,
                        )
                        val_result = await asyncio.to_thread(
                            self._validator.execute,
                            input_msg=val_msg,
                            content_to_validate=output[:2000],
                        )
                        if not val_result.success:
                            last_error = (
                                f"Validación factual fallida: "
                                f"{str(val_result.metadata.get('critique', 'Datos inconsistentes'))[:200]}"
                            )
                            traces.append(StepTrace(
                                attempt=attempt, code=code, code_lines=n_lines,
                                tool_result=tr, success=False,
                                error_observed=last_error,
                                duration_ms=dur_ms, generation_ms=gen_ms,
                                execution_ms=exec_ms,
                            ))
                            _emit_state("HEALING")
                            _log_loop("VALIDATOR_REJECT", attempt=attempt,
                                      critique=last_error[:80])
                            logger.info(
                                f"[InterpreterLoop] ⚠️ Validator rechazó attempt "
                                f"{attempt}: {last_error[:80]}"
                            )
                            continue
                    except Exception as _val_exc:
                        logger.warning(
                            f"[InterpreterLoop] Validator error (saltando): {_val_exc}"
                        )

                # v1.3.0: Detectar archivos generados en workspace
                generated_files = []
                # v1.4.0: Historial de intentos
                attempt_history = []
                if self._workspace and session_id:
                    try:
                        generated_files = self._workspace.scan_workspace(session_id)
                        attempt_history = self._workspace.get_attempt_history(session_id)
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 interpreter_loop.py:436] excepción degradada (intencional): {_d2e}")

                traces.append(StepTrace(
                    attempt=attempt, code=code, code_lines=n_lines,
                    tool_result=tr, success=True,
                    duration_ms=dur_ms, generation_ms=gen_ms, execution_ms=exec_ms,
                ))
                total_ms = (time.perf_counter() - t0) * 1000
                _log_loop("SUCCESS", attempt=attempt, lines=n_lines,
                          total_ms=round(total_ms, 0))
                logger.info(f"[InterpreterLoop] ✅ attempt {attempt} ({total_ms:.0f}ms, {n_lines} lines)")
                # ✅ v1.7.0: TraceStore — persiste traza de éxito (fire-and-forget)
                if _TRACE_STORE_AVAILABLE:
                    try:
                        _ts = get_trace_store()
                        if _ts is not None and _Trace is not None:
                            _ts.record_async(_Trace(
                                task_type="code_autonomous",
                                handler="InterpreterLoop",
                                latency_ms=round(total_ms, 2),
                                success=True,
                                session_id=session_id,
                                loop_attempts=attempt,
                                loop_stop_reason="success",
                                model=getattr(getattr(self.coder, 'config', None), 'model', ''),
                            ))
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 interpreter_loop.py:463] excepción degradada (intencional): {_d2e}")
                _emit_state("IDLE")
                return InterpreterResult(
                    success=True, final_output=output, final_code=code,
                    traces=traces, total_attempts=attempt,
                    duration_ms=total_ms, stop_reason="success",
                    metadata={
                        "session_id": session_id,
                        "coder_model": getattr(self.coder.config, 'model', 'unknown'),
                        "generated_files": generated_files,
                        "attempt_history": attempt_history,
                    },
                )

            # Fracaso — extraer error
            error_msg = tr.error or "Error desconocido en sandbox"
            stderr = ""
            if hasattr(tr, 'metadata') and tr.metadata:
                if tr.metadata.get("gateway_denied"):
                    stop_reason = "permission_denied"
                    _log_loop("PERMISSION_DENIED", attempt=attempt)
                    logger.warning("[InterpreterLoop] Gateway DENY — abort")
                    break
                if tr.metadata.get("circuit_open"):
                    stop_reason = "circuit_open"
                    _log_loop("CIRCUIT_OPEN", attempt=attempt)
                    logger.warning("[InterpreterLoop] CircuitBreaker OPEN — abort")
                    break
                stderr = tr.metadata.get("stderr", "")
                if stderr:
                    error_msg += f"\nStderr:\n{stderr}"

            last_error = error_msg
            traces.append(StepTrace(
                attempt=attempt, code=code, code_lines=n_lines,
                tool_result=tr, success=False, error_observed=error_msg,
                duration_ms=dur_ms, generation_ms=gen_ms, execution_ms=exec_ms,
            ))
            _emit_state("HEALING")
            _log_loop("FIX_NEEDED", attempt=attempt,
                      error=error_msg[:100], lines=n_lines)
            logger.info(f"[InterpreterLoop] ❌ attempt {attempt}: {error_msg[:100]}")

            # ✅ v0.6.8pp (logs/reg_error.txt 15:03): guard de estancamiento.
            # Caso: el fix del intento anterior produjo código que falla con
            # el MISMO error (traceback idéntico). El próximo fix_message es
            # byte-idéntico al anterior → la caché de generación devuelve el
            # mismo código roto → falla igual → bucle sin progreso (intentos
            # 3-5 del caso code_testing fueron 3 copias idénticas).
            #
            # Si el error recién observado es IDÉNTICO al del intento previo,
            # cortar acá: otra generación no puede cambiar nada.
            if (
                len(traces) >= 2
                and traces[-2].error_observed == error_msg
                and traces[-2].code == code
            ):
                stop_reason = "no_progress"
                _log_loop("NO_PROGRESS_STALL", attempt=attempt,
                          error=error_msg[:120])
                logger.warning(
                    f"[InterpreterLoop] ⛔ Sin progreso en attempt {attempt}: "
                    f"error idéntico al intento anterior — cortando "
                    f"(evita generar el mismo código roto)"
                )
                break

        total_ms = (time.perf_counter() - t0) * 1000
        _log_loop("EXHAUSTED", attempts=len(traces), reason=stop_reason,
                  total_ms=round(total_ms, 0))
        logger.warning(f"[InterpreterLoop] Exhausted {self.config.max_fix_attempts} attempts — {stop_reason}")

        # v1.4.0: Incluir historial de intentos en resultado de agotamiento
        attempt_history = []
        if self._workspace and session_id:
            try:
                attempt_history = self._workspace.get_attempt_history(session_id)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 interpreter_loop.py:541] excepción degradada (intencional): {_d2e}")

        # ✅ v1.7.0: TraceStore — persiste traza de agotamiento (fire-and-forget)
        if _TRACE_STORE_AVAILABLE:
            try:
                _ts = get_trace_store()
                if _ts is not None and _Trace is not None:
                    _ts.record_async(_Trace(
                        task_type="code_autonomous",
                        handler="InterpreterLoop",
                        latency_ms=round(total_ms, 2),
                        success=False,
                        session_id=session_id,
                        loop_attempts=len(traces),
                        loop_stop_reason=stop_reason,
                        model=getattr(getattr(self.coder, 'config', None), 'model', ''),
                    ))
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 interpreter_loop.py:559] excepción degradada (intencional): {_d2e}")

        _emit_state("IDLE")
        return InterpreterResult(
            success=False,
            final_output=self._error_summary(traces, last_code),
            final_code=last_code,
            traces=traces,
            total_attempts=len(traces),
            duration_ms=total_ms,
            stop_reason=stop_reason,
            metadata={
                "session_id": session_id,
                "attempt_history": attempt_history,
            },
        )

    # ── Sandbox ──────────────────────────────────────────────────────────────
    async def _sandbox_exec(
        self, code: str, session_id: Optional[str] = None, attempt: int = 1
    ) -> ToolResult:
        """
        Escribe el código a un archivo real y lo ejecuta vía SandboxExecutor.

        ✅ FIX AUDITORÍA v1.8.0 (S2/S8): antes esto armaba un string con
        sintaxis de shell real (|, >, &&, $(...)) y lo mandaba a terminal_exec
        (SecureTerminalTool), que hace shlex.split() + subprocess shell=False.
        Con shell=False los operadores de shell NUNCA se interpretan — quedan
        como argumentos literales de `echo` — así que el script NUNCA se
        escribía ni se ejecutaba de verdad. `echo` corría "exitosamente" con
        basura de argumentos, y todo lo que este loop interpretaba como
        "error a corregir" era en realidad la infraestructura rota, no el
        código generado. El base64 del path del intérprete (S8) evadía de
        paso la blacklist de tool_registry.py sin resolver nada de fondo.

        Ahora: escritura directa con Path.write_text() (sin shell, sin
        base64) + SandboxExecutor.execute_script(interpreter=[python_bin]) —
        mismo mecanismo ya usado en promotion_gate.py (S4). De paso, se saca
        una capa de indirección (ToolExecutor → PermissionGateway →
        CircuitBreaker → SecureTerminalTool → shlex.split()) para esta
        ejecución puntual, que ya estaba sandboxeada igual — menos latencia
        por intento.

        v1.1.0: Si hay WorkspaceManager, usa workspace persistente + venv.
                Si no hay workspace, fallback en /tmp con sys.executable.
        v1.2.0: Auto-resuelve ModuleNotFoundError instalando el paquete faltante
                en el venv y reintentando sin consumir un intento de fix.
        v1.4.0: Guarda script.py anterior como attempt_N.py para historial.
        v1.5.0: Verifica whitelist/blocklist antes de auto-instalar paquetes.
        """
        from src.tools_mcp.sandbox import get_sandbox_executor

        # ── Resolver paths según disponibilidad de workspace ───────────────
        workspace: Optional[str] = None
        python_bin: Optional[str] = None
        if self._workspace:
            try:
                workspace = self._workspace.get_workspace(session_id or "default")
                python_bin = self._workspace.get_venv_python(session_id or "default")
                script_path = f"{workspace}/script.py"
            except Exception as e:
                _log_loop("WORKSPACE_FALLBACK", error=str(e))
                logger.warning(f"[InterpreterLoop] Workspace error, fallback a /tmp: {e}")
                workspace = None
                python_bin = None
                script_path = "/tmp/_ax_exec.py"
        else:
            script_path = "/tmp/_ax_exec.py"

        # ── v1.4.0: Guardar intento anterior como historial antes de sobrescribir ──
        if python_bin and attempt > 1:
            prev_script = f"{workspace}/attempt_{attempt - 1}.py"
            try:
                if Path(script_path).exists():
                    shutil.copy2(script_path, prev_script)
            except Exception as e:
                # R3: historial de intentos best-effort — no debe romper la ejecución.
                log_degraded(logger, e, f"InterpreterLoop._sandbox_exec (historial intento {attempt})")

        # ── Escribir el código directo — sin shell, sin base64 ──────────────
        code_with_shebang = f"#!/usr/bin/env python3\n{code}"
        try:
            Path(script_path).write_text(code_with_shebang, encoding="utf-8")
        except Exception as e:
            return ToolResult.fail(
                error=f"No se pudo escribir el script: {e}",
                tool_name=self.config.sandbox_tool_name,
            )

        # ── Ejecutar vía SandboxExecutor (mismo mecanismo que promotion_gate.py) ──
        sandbox = get_sandbox_executor(timeout=self.config.sandbox_timeout)

        def _run_once() -> "ToolResult":
            sbx = sandbox.execute_script(
                script_path,
                cwd=workspace,
                timeout=self.config.sandbox_timeout,
                interpreter=[python_bin] if python_bin else None,
            )
            result = ToolResult(
                success=(sbx.returncode == 0 and not sbx.timeout),
                output=sbx.stdout or "",
                tool_name=self.config.sandbox_tool_name,
                latency_ms=sbx.duration_ms,
                error=None if sbx.returncode == 0 and not sbx.timeout
                      else (sbx.error or sbx.stderr or "Error desconocido en sandbox"),
                source="sandbox",
                metadata={
                    "stderr": sbx.stderr or "",
                    "killed": sbx.killed,
                    "timeout": sbx.timeout,
                    "returncode": sbx.returncode,
                },
            )
            # Extracción agresiva de error cuando tr.error está vacío
            if not result.success and not (result.error or "").strip():
                raw = (result.metadata.get("stderr", "") or result.output or "").strip()
                if raw:
                    result.error = raw[:500]
            return result

        # execute_script() es síncrono (subprocess.Popen bloqueante) — a thread
        # para no bloquear el event loop, mismo criterio que el fix de L1.
        tr = await asyncio.to_thread(_run_once)

        # ── v1.2.0: Auto-resolver dependencias ──────────────────────────────
        if (
            not tr.success
            and self._workspace
            and python_bin
            and session_id
        ):
            try:
                from src.core.dependency_resolver import get_dependency_resolver
                resolver = get_dependency_resolver()
                error_text = (tr.error or "") + tr.metadata.get("stderr", "")
                pkg = resolver.extract_missing_package(error_text)
                if pkg:
                    # ── v1.5.0: Verificar whitelist ANTES de instalar ───────
                    if not resolver.is_package_allowed(pkg):
                        logger.warning(f"[InterpreterLoop] Paquete bloqueado: {pkg}")
                        return ToolResult.fail(
                            error=f"Paquete '{pkg}' no permitido para auto-instalación",
                            tool_name=self.config.sandbox_tool_name,
                            metadata={"package_blocked": True, "package": pkg},
                        )
                    # ── Fin verificación whitelist ───────────────────────────
                    _log_loop("AUTO_INSTALL", package=pkg)
                    logger.info(f"[InterpreterLoop] Auto-instalando {pkg} en venv")
                    install_result = self._workspace.install_package(
                        session_id or "default", pkg
                    )
                    if install_result["success"]:
                        tr2 = await asyncio.to_thread(_run_once)
                        if tr2.success:
                            return tr2
                        tr = tr2
            except Exception as e:
                logger.debug(f"[InterpreterLoop] Auto-install falló: {e}")

        return tr

    # ── Extracción de código ─────────────────────────────────────────────────

    @staticmethod
    def _static_syntax_error(code: str) -> Optional[str]:
        """
        ✅ v0.6.8 (hallazgo 3.2): valida sintaxis del código SIN ejecutar.

        Compila por bloque ```python``` (mismo criterio que CodeValidator
        Fase 3 y analyze_code_quality): si hay bloques etiquetados, cada
        uno compila por separado; si no, compila el string completo.
        Retorna el primer error de sintaxis o None si compila.
        """
        if not code or not code.strip():
            return None
        blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", code, re.DOTALL | re.IGNORECASE)
        targets = blocks if blocks else [code]
        for target in targets:
            try:
                compile(target, "<generated>", "exec")
            except SyntaxError as e:
                line = e.lineno or 0
                return f"{e.msg} (línea {line})"
        return None

    def _extract_code(self, coder_result: Any) -> str:
        if hasattr(coder_result, 'metadata') and coder_result.metadata:
            blocks = coder_result.metadata.get("extracted_code", [])
            if isinstance(blocks, list) and blocks:
                parts = [b.get("code", "") for b in blocks if b.get("code")]
                if parts:
                    return "\n\n".join(parts)

        content = getattr(coder_result, 'content', str(coder_result))
        matches = self._CODE_RE.findall(content)
        if matches:
            return max(matches, key=len).strip()

        return ""

    # ── Output del sandbox ───────────────────────────────────────────────────

    @staticmethod
    def _get_output(tr: ToolResult) -> str:
        if hasattr(tr, 'output') and tr.output:
            return str(tr.output)
        if hasattr(tr, 'data') and tr.data:
            return str(tr.data)
        return "(ejecutado sin output)"

    # ── Prompts de fix ───────────────────────────────────────────────────────

    @staticmethod
    def _fix_prompt(original: str, code: str, error: str) -> str:
        return (
            f"Tarea original: {original}\n\n"
            f"Código que FALLÓ al ejecutar:\n```python\n{code}\n```\n\n"
            f"ERROR DEL SANDBOX:\n{error}\n\n"
            f"Corregí el error. Respondé SOLO con ```python ... ``` sin explicaciones."
        )

    @staticmethod
    def _error_summary(traces: List[StepTrace], last_code: str) -> str:
        lines = [f"⚠️ No pude ejecutar la tarea tras {len(traces)} intento(s):", ""]
        for t in traces:
            icon = "✅" if t.success else "❌"
            err = t.error_observed[:150] if t.error_observed else "OK"
            lines.append(f"{icon} Intento {t.attempt}: {t.code_lines} líneas, {t.duration_ms:.0f}ms — {err}")
        if last_code:
            lines += ["", "Último código generado:", f"```python\n{last_code}\n```"]
        return "\n".join(lines)

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "available": True,
            "max_fix_attempts": self.config.max_fix_attempts,
            "sandbox_tool": self.config.sandbox_tool_name,
            "sandbox_timeout": self.config.sandbox_timeout,
            "global_timeout": self.config.global_timeout,
        }


# ============================================================================
# SINGLETON lazy
# ============================================================================

_loop: Optional[InterpreterLoop] = None


def get_interpreter_loop(
    coder: Any = None,
    executor: Any = None,
    config: Optional[LoopConfig] = None,
    workspace_manager: Any = None,
) -> InterpreterLoop:
    global _loop
    if _loop is None:
        if coder is None or executor is None:
            raise ValueError("Primera llamada requiere coder y executor")
        _loop = InterpreterLoop(
            coder=coder, executor=executor, config=config,
            workspace_manager=workspace_manager,
        )
        logger.info("InterpreterLoop singleton creado")
    return _loop


def reset_interpreter_loop() -> None:
    global _loop
    _loop = None
