#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# system_agent.py - Control del sistema operativo Linux
# Ruta: src/agents/system_agent.py
# Autor: SaintWick
# Versión: 1.5.3
#
# Agente para operaciones del sistema: abrir/cerrar apps, control de
# volumen, capturas de pantalla, portapapeles, listado de archivos,
# y operaciones de archivo con sandbox + PromotionGate (write/move/delete/copy).
# Toda ejecución pasa por ToolExecutor + Sandbox.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
import shlex
import time
from pathlib import Path
from typing import Optional, Tuple
from src.agents.base import BaseAgent, AgentResult
from src.core.promotion_gate import get_promotion_gate
from src.domain.entities import Message
from src.domain.types import AgentRole

logger = logging.getLogger(__name__)

# ✅ detailed_logger — import seguro (doble capa)
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _DETAILED_LOGGER_AVAILABLE = True
except ImportError:
    _DETAILED_LOGGER_AVAILABLE = False
    _get_detailed_logger = None  # type: ignore[assignment]


def _dlog() -> Optional[any]:
    """Retorna instancia del detailed_logger o None si no disponible."""
    if not _DETAILED_LOGGER_AVAILABLE:
        return None
    try:
        lgr = _get_detailed_logger()
        if lgr.is_initialized:
            return lgr
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 system_agent.py:43] excepción degradada (intencional): {_d2e}")
    return None


# Mapeo keyword → operación (infinitivo + voseo imperativo)
_INTENT_MAP = {
    "abrir":       "open_app",
    "abrí":        "open_app",
    "abri":        "open_app",
    "abre":        "open_app",
    "lanzar":      "open_app",
    "lanzá":       "open_app",
    "lanza":       "open_app",
    "cerrar":      "close_app",
    "cerrá":       "close_app",
    "cerra":       "close_app",
    "cierra":      "close_app",
    "volumen":     "set_volume",
    "captura":     "take_screenshot",
    "screenshot":  "take_screenshot",
    "portapapeles": "read_clipboard",
    "clipboard":   "read_clipboard",
    "archivos":    "list_files",
    "listar":      "list_files",
    "listá":       "list_files",
    "lista":       "list_files",
    "leer":        "read_file",
    "leé":         "read_file",
    "lee":         "read_file",
    "escribir":    "write_file",
    "escribí":     "write_file",
    "escribi":     "write_file",
    "escribe":     "write_file",
    "copiar":      "copy_file",
    "copiá":       "copy_file",
    "copia":       "copy_file",
    "mover":       "move_file",
    "moví":        "move_file",
    "movi":        "move_file",
    "mueve":       "move_file",
    "eliminar":    "delete_file",
    "eliminá":     "delete_file",
    "elimina":     "delete_file",
    "borrar":      "delete_file",
    "borrá":       "delete_file",
    "borra":       "delete_file",
    "aprobar":     "verdict_aprobar",
    "apruebo":     "verdict_aprobar",
    "aprobá":      "verdict_aprobar",
    "corregir":    "verdict_corregir",
    "corregí":     "verdict_corregir",
    "descartar":   "verdict_descartar",
    "descartá":    "verdict_descartar",
}


# ═══════════════════════════════════════════════════════════════
# ✅ NUEVO v1.6.0: subset de _INTENT_MAP seguro para que dispatcher.py
# fuerce task_type="system_control" ANTES de que el resto del pipeline
# corra (ver _is_system_command_intent() en dispatcher.py). classifier_core.py
# no tiene NINGÚN patrón en KEYWORD_MAP para system_control/app_launch/
# volume_control — confirmado por grep exhaustivo — así que sin este
# override "listá X"/"abrí X" caían siempre a general_chat/web_search y
# nunca llegaban acá.
#
# NO es el _INTENT_MAP completo — quedan afuera 4 categorías, confirmado
# contra KEYWORD_MAP/CODE_CONTEXT_KEYWORDS/VISION_SCREEN_KEYWORDS de
# classifier_core.py:
#   - escribir/escribí/escribi/escribe (write_file): "escribir" ya mapea
#     a CONTENT_CREATION en KEYWORD_MAP; escribí/escribe alimentan
#     CODE_CONTEXT_KEYWORDS. Forzar acá rompería esas dos rutas.
#   - captura/screenshot (take_screenshot): están en VISION_SCREEN_KEYWORDS,
#     rutean a screenshot_analysis (VisionAgent describe la pantalla) —
#     operación distinta a esta (guardar el screenshot a archivo).
#   - aprobar/apruebo/aprobá/descartar/descartá (verdict_aprobar/
#     verdict_descartar): ✅ RESUELTO (pendiente 2) — no están en este
#     frozenset porque forzar por keyword sola es semánticamente
#     incorrecto sin propuesta pendiente (ej. "aprobar el balance"), NO
#     por colisión con el classifier. Alcanzables igual desde lenguaje
#     natural vía un chequeo SEPARADO en dispatcher.py
#     (_is_verdict_intent()) que solo pisa si
#     PromotionGate.has_pending(session_id) es True para esa sesión.
#   - corregir/corregí (verdict_corregir): sigue sin resolver — colisiona
#     con CODE_CORRECTION en KEYWORD_MAP incluso con propuesta pendiente
#     (no hay forma de distinguir "corregí esto" código de "corregí"
#     veredicto solo con la primera palabra). verdict_corregir sigue
#     alcanzable solo si task_type ya es system_control por otra vía
#     (ej. UI/CLI explícita), no por lenguaje natural.
#   - leer/leé/lee (read_file): "leer" está en CODE_CONTEXT_KEYWORDS
#     (confirmado, classifier_core.py línea ~211), y "lee la pantalla"/
#     "leer pantalla" ya mapean directo a SCREENSHOT_ANALYSIS en
#     KEYWORD_MAP — mismo tipo de colisión que escribir/captura. La
#     operación read_file existe (ver execute()) y es alcanzable si
#     task_type ya llegó como system_control por otra vía; no por este
#     override.
#
# Si se agrega un verbo nuevo a _INTENT_MAP arriba, sumarlo acá requiere
# el mismo chequeo contra classifier_core.py antes de hacerlo.
DISPATCHER_ROUTING_VERBS: frozenset[str] = frozenset({
    "abrir", "abrí", "abri", "abre", "lanzar", "lanzá", "lanza",
    "cerrar", "cerrá", "cerra", "cierra",
    "volumen",
    "portapapeles", "clipboard",
    "archivos", "listar", "listá", "lista",
    "copiar", "copiá", "copia",
    "mover", "moví", "movi", "mueve",
    "eliminar", "eliminá", "elimina", "borrar", "borrá", "borra",
})


class SystemAgent(BaseAgent):
    """
    Agente de control del sistema operativo Linux.
    Toda ejecución pasa por ToolExecutor + Sandbox — nunca subprocess directo.
    Operaciones soportadas:
    open_app, close_app, set_volume, take_screenshot,
    read_clipboard, write_clipboard, list_files
    """
    AGENT_TYPE = "system"

    # ✅ FIX T25: Constante movida a clase para que el test SystemAgent.ALLOWED_OPERATIONS funcione
    ALLOWED_OPERATIONS = {
        "open_app",
        "close_app",
        "set_volume",
        "take_screenshot",
        "read_clipboard",
        "write_clipboard",
        "list_files",
        "read_file",
        "write_file",
        "copy_file",
        "move_file",
        "delete_file",
        "verdict_aprobar",
        "verdict_corregir",
        "verdict_descartar",
    }

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=(
                "Eres un agente de control del sistema operativo Linux. "
                "Ejecutas acciones concretas: abrir apps, controlar volumen, "
                "tomar capturas. Confirma la acción realizada en cada respuesta."
            ),
            **kwargs,
        )

    # ── FASE 4 (plan_harness): coordinación multi-modelo con memoria segura ──
    def run_model_chain(self, steps: list, timeout: float = 600.0) -> dict:
        """
        Ejecuta una cadena de pasos que pueden requerir modelos distintos,
        con guard de memoria (no OOM) y swap con drenado.

        steps: [{"name": str, "model": str, "fn": callable(model, deps),
                 "depends_on": [str]}]

        Cada paso recibe (model_name, deps) donde deps = {nombre: resultado}.
        Si un modelo no puede cargarse por memoria, el paso se registra
        como refused_oom y la cadena devuelve el error sin ejecutar.
        """
        from src.core.agent_coordinator import get_coordinator
        coord = get_coordinator()
        return coord.run_chain(steps, timeout=timeout)

    def execute(self, input_msg: Message, **kwargs) -> AgentResult:
        """Parsea intención y delega a execute_tool_sync()."""
        import time
        t0 = time.perf_counter()
        task_type = kwargs.get("task_type", "system_control")
        # ✅ FIX (hallazgo test_fs_agent_temp.py — "Documentos"→"documentos"
        # en el error de list_files): antes se lowercaseaba TODO el query
        # acá, antes de _parse_intent(). Case-sensitive filesystem (Linux):
        # cualquier target/contenido con mayúsculas ("Documentos", nombres
        # propios en contenido dictado para write_file) quedaba
        # silenciosamente roto — ni siquiera fallaba con un error claro,
        # simplemente buscaba/escribía el nombre equivocado. El
        # lowercase original solo hacía falta para que el matching de
        # VERBOS en _INTENT_MAP fuera case-insensitive ("Listá" al
        # empezar una oración) — eso ahora se resuelve adentro de
        # _parse_intent(), por palabra, sin tocar el case de
        # targets/contenido/reason.
        query = input_msg.content

        # ✅ Log inicio en detailed_logger
        dl = _dlog()
        if dl:
            dl.log_agent_execution(
                agent_name="SystemAgent",
                task_type=task_type,
                state="started",
                success=True,
            )

        operation, params = self._parse_intent(query)

        if operation not in self.ALLOWED_OPERATIONS:
            error_msg = f"Operación no permitida: {operation}"
            if dl:
                dl.log_agent_execution(
                    agent_name="SystemAgent",
                    task_type=task_type,
                    state="failed",
                    duration=(time.perf_counter() - t0),
                    success=False,
                    error=error_msg,
                )
            return AgentResult(
                success=False,
                content=f"Operación no permitida: {operation}",
                agent_type=self.AGENT_TYPE,
                model_used="system",
                task_type=task_type,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=f"{operation} no está en ALLOWED_OPERATIONS",
            )

        try:
            # [D1] Grounding check pre-ejecución
            ok, reason = self._grounding_check(operation, params)
            if not ok:
                logger.warning(f"[SystemAgent] grounding_check failed: {reason}")
                if dl:
                    dl.log_event("SYSTEM_AGENT", f"grounding_check failed: {reason}", level="WARNING")
                return AgentResult(
                    success=False,
                    content=reason,
                    agent_type=self.AGENT_TYPE,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=reason,
                )

            # [D1] DependencyGraph hook — solo si hay múltiples targets
            if len(params) > 1 or (isinstance(params.get("target"), list)):
                try:
                    from src.core.dependency_graph import DependencyGraph
                    _dg = DependencyGraph()
                    for i, tgt in enumerate(params.get("targets", [params.get("target", "")])):
                        _dg.add_task(f"sys_{i}", metadata={"target": tgt})
                    risky = _dg._detect_risky_chains()
                    if risky:
                        logger.warning(f"[SystemAgent] DependencyGraph: cadenas riesgosas {risky}")
                except ImportError as _d2e:
                    logging.getLogger(__name__).debug(f"[D2 system_agent.py:289] excepción degradada (intencional): {_d2e}")
                except Exception as _dg_err:
                    logger.warning(f"[SystemAgent] DependencyGraph hook error: {_dg_err}")

            # ✅ FIX Fase 0: take_screenshot delega en ScreenCaptureEngine.
            # scrot es X11 puro — no captura en sesión Wayland nativa (KDE/Sway/Hyprland).
            if operation == "take_screenshot":
                content = self._take_screenshot_via_engine()
                success = content is not None
                if dl:
                    dl.log_agent_execution(
                        agent_name="SystemAgent",
                        task_type=task_type,
                        state="completed" if success else "failed",
                        duration=(time.perf_counter() - t0),
                        success=success,
                        error=None if success else "ScreenCaptureEngine no disponible",
                    )
                return AgentResult(
                    success=success,
                    content=content or "❌ take_screenshot falló",
                    agent_type=self.AGENT_TYPE,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=None if success else "ScreenCaptureEngine no disponible",
                )

            # ✅ v1.6.0: open_app ya no llama subprocess directo — pasa por la
            # tool sys_open_app (tool_registry.py) vía ToolExecutor, igual que
            # list_files pasa por fs_list. Gana PermissionGateway+CircuitBreaker;
            # el comportamiento de lanzamiento (Popen detached, blocklist, alias
            # de navegador) es idéntico al que tenía este método antes.
            if operation == "open_app":
                target = params.get("target", "")
                result = self.execute_tool_sync(
                    tool_name="sys_open_app",
                    target=target,
                    resource_path=target,
                    risk_score=0.1,
                )
                success, output = self._unpack_tool_result(result)
                if dl:
                    dl.log_agent_execution(
                        agent_name="SystemAgent",
                        task_type=task_type,
                        state="completed" if success else "failed",
                        duration=(time.perf_counter() - t0),
                        success=success,
                        error=None if success else output,
                    )
                return AgentResult(
                    success=success,
                    content=f"✅ {operation}: {output}" if success else f"❌ {operation} falló: {output}",
                    agent_type=self.AGENT_TYPE,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=None if success else output,
                )

            # ✅ v1.3.0: list_files usa la tool real fs_list (BaseTool) —
            # antes construía "ls -la {target}" y lo mandaba a terminal_exec
            # como shell. fs_list ya está registrada en tool_registry (ver
            # tool_implementations.py) — misma ruta que fs_read/fs_write.
            if operation == "list_files":
                target = params.get("target", "") or "~"
                result = self.execute_tool_sync(
                    tool_name="fs_list",
                    path=target,
                    resource_path=target,
                    risk_score=0.1,
                )
                success, output = self._unpack_tool_result(result)
                if success:
                    output = "\n".join(output.splitlines()[:20])

                if dl:
                    dl.log_agent_execution(
                        agent_name="SystemAgent",
                        task_type=task_type,
                        state="completed" if success else "failed",
                        duration=(time.perf_counter() - t0),
                        success=success,
                        error=None if success else output,
                    )

                return AgentResult(
                    success=success,
                    content=f"✅ {operation}: {output}" if success else f"❌ {operation} falló: {output}",
                    agent_type=self.AGENT_TYPE,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=None if success else output,
                )

            # ✅ NUEVO (agente filesystem) v1.6.0: read_file usa fs_read
            # directo — mismo criterio que list_files (solo lectura, no
            # pasa por PromotionGate). No se agrega "leer"/"leé"/"lee" a
            # DISPATCHER_ROUTING_VERBS por colisión con CODE_CONTEXT_KEYWORDS/
            # SCREENSHOT_ANALYSIS en classifier_core.py — ver comentario de
            # DISPATCHER_ROUTING_VERBS más arriba.
            if operation == "read_file":
                target = params.get("target", "")
                if not target:
                    msg = "read_file requiere un path"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )
                result = self.execute_tool_sync(
                    tool_name="fs_read",
                    path=target,
                    resource_path=target,
                    risk_score=0.1,
                )
                success, output = self._unpack_tool_result(result)

                if dl:
                    dl.log_agent_execution(
                        agent_name="SystemAgent", task_type=task_type,
                        state="completed" if success else "failed",
                        duration=(time.perf_counter() - t0), success=success,
                        error=None if success else output,
                    )

                return AgentResult(
                    success=success,
                    content=f"✅ {operation}: {output}" if success else f"❌ {operation} falló: {output}",
                    agent_type=self.AGENT_TYPE,
                    model_used="system",
                    task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=None if success else output,
                )

            # ✅ v1.5.0: write_file/move_file/delete_file ya no tocan fs_write/
            # fs_move/fs_delete directo — pasan por PromotionGate. session_id
            # identifica la propuesta pendiente para el veredicto posterior.
            session_id = kwargs.get("session_id", "default")

            if operation == "write_file":
                target = params.get("target", "")
                content_file = params.get("content_file", "")
                content = params.get("content", "")

                if not target:
                    msg = "write_file requiere un path (ej: 'escribí notas.txt hola mundo')"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                if content_file:
                    try:
                        content = Path(content_file).expanduser().read_text(encoding="utf-8")
                    except Exception as e:
                        msg = f"No pude leer '{content_file}': {e}"
                        return AgentResult(
                            success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                            model_used="system", task_type=task_type,
                            latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                        )

                try:
                    proposal = get_promotion_gate().propose_write(target, content, session_id=session_id)
                except RuntimeError as e:
                    return AgentResult(
                        success=False, content=f"❌ {e}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(e),
                    )
                return self._proposal_result(proposal, task_type, t0, dl)

            # ✅ NUEVO (agente filesystem) v1.6.0: copy_file compone fs_read
            # (ToolExecutor, solo lectura — no toca PromotionGate) + propose_write
            # (mismo camino que write_file, reusado tal cual, no una escritura
            # paralela sin veredicto). Deliberadamente solo texto: fs_read
            # decodifica con errors="replace", y escribir esa decodificación de
            # vuelta corrompería un binario en silencio — se corta antes con el
            # "kind" que ahora devuelve fs_read. Copiar directorios tampoco entra
            # acá (mismo criterio que fs_delete: "no elimina directorios" — esto
            # tampoco los copia); fs_read ya rechaza el intento con un error claro.
            if operation == "copy_file":
                src = params.get("target", "")
                dst = params.get("target2", "")
                if not src or not dst:
                    msg = "copy_file requiere origen y destino (ej: 'copiá a.txt a backup/a.txt')"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                read_result = self.execute_tool_sync(
                    tool_name="fs_read",
                    path=src,
                    max_chars=10_000_000,  # copia sin recortar — no es para mostrar en chat
                    resource_path=src,
                    risk_score=0.1,
                )
                read_ok, read_output = self._unpack_tool_result(read_result)
                if not read_ok:
                    msg = f"No pude leer el origen '{src}': {read_output}"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                read_kind = (getattr(read_result, "data", None) or {}).get("kind")
                if read_kind not in (None, "text"):
                    msg = (
                        f"copy_file solo soporta archivos de texto — '{src}' se "
                        f"detectó como '{read_kind}' (binario/programa), la copia "
                        f"lo corrompería. Usá un gestor de archivos para binarios."
                    )
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                try:
                    proposal = get_promotion_gate().propose_write(dst, read_output, session_id=session_id)
                except RuntimeError as e:
                    return AgentResult(
                        success=False, content=f"❌ {e}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(e),
                    )
                return self._proposal_result(proposal, task_type, t0, dl)

            if operation == "move_file":
                src = params.get("target", "")
                dst = params.get("target2", "")
                if not src or not dst:
                    msg = "move_file requiere origen y destino (ej: 'moví a.txt a backup/a.txt')"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                try:
                    proposal = get_promotion_gate().propose_move(src, dst, session_id=session_id)
                except RuntimeError as e:
                    return AgentResult(
                        success=False, content=f"❌ {e}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(e),
                    )
                return self._proposal_result(proposal, task_type, t0, dl)

            if operation == "delete_file":
                target = params.get("target", "")
                if not target:
                    msg = "delete_file requiere un path"
                    return AgentResult(
                        success=False, content=f"❌ {msg}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=msg,
                    )

                try:
                    proposal = get_promotion_gate().propose_delete(target, session_id=session_id)
                except RuntimeError as e:
                    return AgentResult(
                        success=False, content=f"❌ {e}", agent_type=self.AGENT_TYPE,
                        model_used="system", task_type=task_type,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2), error=str(e),
                    )
                return self._proposal_result(proposal, task_type, t0, dl)

            # ✅ v1.5.0: "aprobar"/"corregir"/"descartar" resuelven la propuesta
            # pendiente de la sesión — no requieren el proposal_id.
            if operation in ("verdict_aprobar", "verdict_corregir", "verdict_descartar"):
                verdict = operation.split("_", 1)[1]
                reason = params.get("reason", "")
                result = get_promotion_gate().resolve_pending(session_id, verdict, reason)

                if dl:
                    dl.log_agent_execution(
                        agent_name="SystemAgent", task_type=task_type,
                        state="completed" if result.get("success") else "failed",
                        duration=(time.perf_counter() - t0), success=bool(result.get("success")),
                        error=None if result.get("success") else result.get("error", result.get("message")),
                    )

                success = bool(result.get("success"))
                text = result.get("message") or result.get("error", "")
                return AgentResult(
                    success=success,
                    content=(f"✅ {text}" if success else f"❌ {text}"),
                    agent_type=self.AGENT_TYPE, model_used="system", task_type=task_type,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error=None if success else text,
                )

            # ✅ v1.2.1: _build_command() retorna str (contrato con terminal_exec schema)
            command = self._build_command(operation, params)

            # ✅ Log comando construido
            if dl:
                dl.log_event("SYSTEM_AGENT", f"Executing: {operation}", level="INFO", extra={
                    "command": command,
                    "target": params.get("target", ""),
                })

            # ✅ v1.2.1: enviar command= (str) — el schema de terminal_exec requiere command:str
            # SecureTerminalTool._run() hace shlex.split() internamente antes del sandbox
            result = self.execute_tool_sync(
                tool_name="terminal_exec",
                command=command,  # ← str, como el schema espera
                timeout=10.0,
            )

            # ✅ v1.3.0 FIX: ver _unpack_tool_result — antes bool(result)/str(result)
            # para un ToolResult siempre daban True/repr, nunca el fallo real.
            success, output = self._unpack_tool_result(result)

            # ✅ Log resultado
            if dl:
                dl.log_agent_execution(
                    agent_name="SystemAgent",
                    task_type=task_type,
                    state="completed",
                    duration=(time.perf_counter() - t0),
                    success=success,
                    error=None if success else output,
                )

            return AgentResult(
                success=success,
                content=f"✅ {operation}: {output}" if success else f"❌ {operation} falló",
                agent_type=self.AGENT_TYPE,
                model_used="system",
                task_type=task_type,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=None if success else output,
            )

        except Exception as e:
            logger.error(f"[SystemAgent] Error en {operation}: {e}")

            # ✅ Log error detallado
            if dl:
                dl.log_error(
                    error=e,
                    context=f"SystemAgent.execute — operation={operation}",
                    function_name="SystemAgent.execute",
                    extra_info={
                        "operation": operation,
                        "params": params,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                    },
                )
                dl.log_agent_execution(
                    agent_name="SystemAgent",
                    task_type=task_type,
                    state="failed",
                    duration=(time.perf_counter() - t0),
                    success=False,
                    error=str(e),
                )

            return AgentResult(
                success=False,
                content=f"Error ejecutando {operation}",
                agent_type=self.AGENT_TYPE,
                model_used="system",
                task_type=task_type,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                error=str(e),
            )

    def _take_screenshot_via_engine(self) -> Optional[str]:
        """
        ✅ FIX Fase 0: captura vía ScreenCaptureEngine (Grim/Spectacle/Scrot/PIL
        con fallback en cascada) en vez de scrot fijo — necesario en sesión
        Wayland nativa donde scrot no captura.
        Guarda JPEG en /tmp/axioma_screenshot.png (mismo path que antes).
        """
        try:
            import base64
            from src.multimodal.screen_capture_engine import ScreenCaptureEngine

            if not hasattr(self, "_screen_capture_engine") or self._screen_capture_engine is None:
                self._screen_capture_engine = ScreenCaptureEngine()

            frame = self._screen_capture_engine.capture_sync()
            if frame is None or not frame.image_b64:
                return None

            out_path = "/tmp/axioma_screenshot.png"
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(frame.image_b64))

            return f"✅ take_screenshot: Screenshot guardado en {out_path} (backend={frame.backend_used})"
        except Exception as e:
            logger.error(f"[SystemAgent] _take_screenshot_via_engine error: {e}")
            return None

    def _grounding_check(self, operation: str, params: dict) -> Tuple[bool, str]:
        """
        [D1] Verifica que params no contengan path traversal ni pipes peligrosos.
        Retorna (True, "") si ok, (False, motivo) si denegado.
        """
        import re
        target = str(params.get("target", ""))

        # Path traversal: ../ o ~ al inicio de target
        if re.search(r'(\.\.[/\\]|^~)', target):
            return False, f"Operación denegada: path traversal detectado en target='{target}'"

        # Shell injection: pipes, punto y coma, doble ampersand
        if re.search(r'[|;&]', target):
            return False, f"Operación denegada: caracteres peligrosos en target='{target}'"

        return True, ""

    _STOPWORDS = {"el", "la", "los", "las", "un", "una", "mi", "de", "por", "favor"}

    def _parse_intent(self, query: str):
        """
        Extrae operación y parámetros del query.

        ✅ FIX confirmado por test_rafael_bridge.py: antes se buscaba cada
        keyword como SUBSTRING de la query completa (`if keyword in query`).
        Un path como "diag_movido.txt" contiene "movi" (keyword de
        move_file) como substring — "eliminá diag_movido.txt" terminaba
        clasificado como move_file, nunca llegaba a mirar "eliminar"/
        "eliminá". Ahora se recorren las PALABRAS de la query en orden y
        se busca la primera que coincide EXACTO con una clave de
        _INTENT_MAP (lookup de diccionario, no substring) — sigue
        tolerando relleno antes del verbo ("por favor eliminá X"
        encuentra "eliminá" en la posición 2 igual), pero un argumento
        (path/contenido) ya no puede secuestrar la operación.

        write_file/move_file/copy_file necesitan más de un parámetro simple
        ("target"), así que se parsean aparte:
          - write_file: primer token NO-stopword = path; el CONTENIDO
            se toma de la lista SIN filtrar (rest filtra "el"/"la"/"de"/
            etc., pensado para extraer un target de una frase de
            comando — pero el contenido que el usuario dicta es texto
            literal, no una frase de comando, y filtrarlo le borraba
            palabras reales como "de"). Si el token siguiente al path
            es "desde", el resto es un path a leer (permite corregir
            editando proposed_content.txt en vez de re-dictar todo).
          - move_file/copy_file: separador "a"/"hacia" entre origen y
            destino — misma forma para ambas, comparten la rama.
          - verdict_*: el resto de la frase queda como "reason" (motivo
            opcional para el informe/log, no afecta el veredicto).

        ✅ FIX confirmado con test_fs_agent_temp.py: la rama genérica
        (open_app/close_app/set_volume/list_files/read_file) usaba
        rest[0] — en frases naturales el target va al FINAL ("listá los
        archivos en ." → rest=['archivos','en','.'] tras filtrar
        stopwords, rest[0]='archivos' es el filler de list_files, no el
        target real). rest[-1] extrae '.'. Mismo caso con "volumen al
        50": rest[0]='al' (stopword no filtrada, no está en _STOPWORDS),
        rest[-1]='50' correcto.
        """
        words = query.split()
        operation = None
        idx = -1
        for i, w in enumerate(words):
            # ✅ FIX: .lower() acá (no upstream en execute()) — matching de
            # verbo case-insensitive sin tocar el case de `words`, del que
            # salen raw_rest/rest (targets/contenido preservan lo tipeado).
            w_clean = w.strip(".,!?:;").lower()
            if w_clean in _INTENT_MAP:
                operation = _INTENT_MAP[w_clean]
                idx = i
                break

        if operation is None:
            return "unknown", {}

        raw_rest = words[idx + 1:]
        # ✅ FIX: comparación case-insensitive contra _STOPWORDS (todo en
        # minúscula), preservando el case original de cada palabra en `rest`.
        rest = [w for w in raw_rest if w.lower() not in self._STOPWORDS]

        if operation == "write_file":
            if not raw_rest:
                return operation, {"target": "", "content": ""}
            # target sale de `rest` (salta fillers tipo "el"/"la" antes
            # del path), pero el contenido sale de `raw_rest` SIN filtrar
            # — es texto literal, no debe perder palabras.
            target = rest[0] if rest else raw_rest[0]
            try:
                target_pos = raw_rest.index(target)
            except ValueError:
                target_pos = 0
            remainder = raw_rest[target_pos + 1:]
            # ✅ FIX: .lower() en la comparación, remainder conserva case.
            if remainder and remainder[0].lower() == "desde":
                return operation, {"target": target, "content_file": " ".join(remainder[1:])}
            return operation, {"target": target, "content": " ".join(remainder)}

        if operation in ("move_file", "copy_file"):
            # ✅ FIX: .lower() en la comparación del separador, rest conserva case.
            sep_idx = next((i for i, w in enumerate(rest) if w.lower() in ("a", "hacia")), -1)
            if sep_idx < 0 or sep_idx == len(rest) - 1 or sep_idx == 0:
                return operation, {"target": rest[0] if rest else "", "target2": ""}
            return operation, {"target": rest[0], "target2": rest[sep_idx + 1]}

        if operation in ("verdict_aprobar", "verdict_corregir", "verdict_descartar"):
            return operation, {"reason": " ".join(rest)}

        param = rest[-1] if rest else ""
        return operation, {"target": param}

    def _proposal_result(self, proposal, task_type: str, t0: float, dl) -> "AgentResult":
        """
        ✅ v1.5.0: Convierte una Proposal de PromotionGate en AgentResult.
        Nunca es el resultado final — es el informe (con rutas a
        report.txt/diff.txt/proposed_content.txt) esperando veredicto.
        """
        info = proposal.to_public_dict()
        headline = (
            f"Propuesta {info['proposal_id']} lista para revisar "
            f"(intento {info['attempt']}/3). "
        )
        if info["tests_passed"] is True:
            headline += "Tests: PASÓ. "
        elif info["tests_passed"] is False:
            headline += "Tests: FALLÓ. "
        else:
            headline += "Sin ejecución de test (ver diff). "
        headline += f"Informe completo: {info['report_path']}"
        if info["content_path"]:
            headline += f" | Contenido propuesto: {info['content_path']}"
        headline += ". Decidí: 'aprobar', 'corregir' o 'descartar'."

        if dl:
            dl.log_agent_execution(
                agent_name="SystemAgent", task_type=task_type,
                state="completed", duration=(time.perf_counter() - t0),
                success=True,
            )

        return AgentResult(
            success=True,
            content=headline,
            agent_type=self.AGENT_TYPE,
            model_used="system",
            task_type=task_type,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            error=None,
            metadata=info,
        )

    @staticmethod
    def _unpack_tool_result(result) -> Tuple[bool, str]:
        """
        ✅ v1.3.0: Normaliza el retorno de execute_tool_sync().

        Camino normal: BaseAgent.execute_tool_sync() retorna el ToolResult
        (dataclass de tool_base.py) devuelto por ToolExecutor.run_sync().
        Camino de excepción: base.py atrapa la excepción y retorna un dict
        plano {"success": False, "error": ..., "output": ""}.

        ❌ Bug anterior: se chequeaba isinstance(result, dict) primero.
           Un ToolResult NO es un dict → caía a bool(result)/str(result).
           Un dataclass sin __bool__ es SIEMPRE truthy y su str() es el
           __repr__ ("ToolResult(✅ 'tool' 12.3ms ...)"), no el output real.
           Resultado: success=True siempre, output=repr en vez de contenido.

        ✅ Fix: distinguir primero por atributo 'success' (ToolResult),
           dict como fallback explícito, y solo caer a bool()/str() si
           no es ninguno de los dos (no debería ocurrir en la práctica).
        """
        if hasattr(result, "success"):
            success = bool(result.success)
            text = result.output if success else (getattr(result, "error", None) or result.output or "")
            return success, text
        if isinstance(result, dict):
            success = bool(result.get("success", False))
            text = result.get("output", "") if success else (result.get("error") or result.get("output", ""))
            return success, text
        return bool(result), str(result)

    def _build_command(self, operation: str, params: dict) -> str:
        """
        ✅ v1.2.1: Construye comando como STRING (contrato con terminal_exec schema).
        SecureTerminalTool._run() hace shlex.split() internamente antes del sandbox.

        Esto resuelve el error:
        "Validación de input: Parámetros requeridos ausentes: ['command']"

        El schema de terminal_exec es:
        - command: str (requerido)
        - cwd: str (opcional)
        - timeout: int (opcional)

        NO acepta args: List[str] — eso fue un error en v1.2.0.
        """
        target = params.get("target", "")

        # ✅ FIX Fase 0: sin operadores compuestos (||, |, &&) — SandboxExecutor
        # corre siempre con shell=False, esos operadores llegan como argumentos
        # literales al comando, no se interpretan. take_screenshot ya no pasa
        # por acá (ver _take_screenshot_via_engine). read/write_clipboard usan
        # wl-clipboard, nativo de la sesión Wayland (KDE/Sway/Hyprland) en vez
        # de xclip (X11, no lee/escribe el clipboard de Wayland de forma confiable).
        # ✅ v1.3.0: list_files tampoco pasa por acá — ver fs_list en execute().
        # ✅ v1.6.0: open_app tampoco pasa por acá — ver sys_open_app en execute().
        commands = {
            "close_app":       f"pkill -f {target}",
            "set_volume":      f"pactl set-sink-volume @DEFAULT_SINK@ {target}%",
            "read_clipboard":  "wl-paste",
            "write_clipboard": f"wl-copy {shlex.quote(target)}",
        }
        return commands.get(operation, f"echo 'Operación: {operation}'")
