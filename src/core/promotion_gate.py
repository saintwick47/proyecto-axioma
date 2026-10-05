#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# promotion_gate.py - Sandbox → análisis → veredicto → promoción
# Ruta: src/core/promotion_gate.py
# Autor: SaintWick
# Versión: 0.2.0
#
# Gate obligatorio para write_file/move_file/delete_file de SystemAgent.
# Ningún archivo real se toca hasta que el usuario dé el veredicto
# ("aprobar"). El contenido propuesto y el diff completo persisten en disco
# (report.txt, diff.txt, proposed_content.txt) para revisión antes de decidir.
# Soporta análisis con sandbox (sintaxis + tests en copia sombra para código
# fuente de AXIOMA, o ejecución directa para archivos externos).
# ═══════════════════════════════════════════════════════════════
#
# ✅ v0.2.0: FIX AUDITORÍA (S4) — _analyze_external() ejecutaba el archivo
#    propuesto con subprocess.run() crudo (sin resource limits, sin
#    sanitización de output) ANTES del veredicto del usuario. Ahora corre
#    vía SandboxExecutor.execute_script(..., interpreter=[venv_python]) —
#    mismos límites de CPU/memoria/nproc/nofile que cualquier otra ejecución
#    sandboxeada, y el stdout/stderr que termina en el informe pasa por
#    _sanitize_output() (redacción de secretos). Nota honesta: el
#    aislamiento de RED de SandboxExecutor (enable_network=False) es solo
#    de configuración hoy — no hay namespace/firewall real detrás. No se
#    tocó esa parte acá; queda como gap conocido, no nuevo.
#
# Dos rutas de análisis según el destino:
#   - Archivo dentro del árbol de AXIOMA (src/**, Rafael.py, config/**):
#     se copia TODO el proyecto a /tmp/axioma_shadow/<key>/ (excluyendo
#     venv/.git/__pycache__/.pytest_cache), se aplica el cambio ahí, y
#     se corre pytest contra la copia con BASE_DIR/DATA_DIR/LOGS_DIR/
#     CONFIG_DIR/CACHE_DIR redirigidos a datos propios de la copia
#     (Settings los toma de env var si llegan absolutos — confirmado
#     contra settings.py). write → test específico si se encuentra
#     (convención de nombre + grep de imports en tests/). move/delete
#     → suite completa, porque mover/borrar puede romper imports en
#     archivos que no se tocaron directamente.
#   - Archivo fuera del árbol de AXIOMA: se usa WorkspaceManager (venv
#     aislado) y se ejecuta directo — no hay proyecto ni tests que
#     correr, solo sintaxis + ejecución real.
#
# El archivo real solo se toca en resolve(verdict="aprobar"), vía
# StagingFS (write/move, con backup+revert automático) o backup+unlink
# propio (delete). "corregir" descarta el veredicto pero REUSA la copia
# sombra / workspace de la misma (sesión, path) — no repite el copytree
# completo — y espera un nuevo propose_*() con contenido nuevo. Tope de
# 3 intentos por (sesión, path); al superarlo, propose_*() rechaza hasta
# que se descarte explícitamente.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import ast
import difflib
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from src.core.workspace_manager import get_workspace_manager
from src.tools_mcp.sandbox_staging import get_staging
from src.tools_mcp.sandbox import get_sandbox_executor
from src.tools_mcp.tool_implementations import _classify_kind
from src.utils.degradation import log_degraded

# ── FASE 2 (plan_harness): FileAuditTrail — auditoría de operaciones ────────
# Import seguro: si la infraestructura de auditoría no está disponible,
# el PromotionGate sigue funcionando sin registrar (degradación).
try:
    from src.learning.file_audit import get_file_audit, FileOpStatus
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False

# ── FASE 3 (plan_harness): EventBus — evento file.operation ─────────────────
# El bus se importa DIFERIDO en _audit_record (misma razón que en
# tool_executor/sandbox: src/core/__init__ importa el Dispatcher con
# impaciencia; un import a nivel de módulo crearía un ciclo).
try:
    from src.domain.events import EventTypes as _PromotionEventTypes
    _PROMOTION_EVENT_TYPES_AVAILABLE = True
except ImportError:
    _PROMOTION_EVENT_TYPES_AVAILABLE = False

logger = logging.getLogger(__name__)

PROMO_ROOT = Path.home() / ".axioma" / "promotions"
SHADOW_ROOT = Path("/tmp/axioma_shadow")
MAX_ATTEMPTS = 3
SHADOW_IGNORE = shutil.ignore_patterns(
    "venv", ".git", "__pycache__", ".pytest_cache", "*.pyc", "*.pyo",
    # ✅ AUDITORÍA 2026-08-25: la sombra es para validar promoción de CÓDIGO
    # — excluir runtime/datos pesados (data/ 22GB rompía el copytree en tmpfs).
    "data", "logs", "cache", "deepseek-harness", "inforchat", "node_modules",
    ".optimizer_backups", "benchmarks", "reports", ".coverage",
)
PYTEST_TIMEOUT_SECONDS = 120.0
RUN_TIMEOUT_SECONDS = 30.0


class ProposalState(str, Enum):
    STAGED = "staged"
    ANALYZED = "analyzed"        # informe listo, esperando veredicto
    PROMOTED = "promoted"
    DISCARDED = "discarded"
    CORRECTING = "correcting"    # veredicto "corregir" ya aplicado


class Verdict(str, Enum):
    APROBAR = "aprobar"
    CORREGIR = "corregir"
    DESCARTAR = "descartar"


@dataclass
class Proposal:
    """Una propuesta de escritura/movimiento/borrado pendiente de veredicto."""
    proposal_id: str
    operation: str                      # "write" | "move" | "delete"
    target_path: Path                   # ruta real absoluta (destino final)
    session_id: str = "default"
    stable_key: str = ""                # hash(sesión, path) — vive entre intentos
    source_path: Optional[Path] = None  # solo para "move": ruta origen
    content: Optional[str] = None       # solo para "write": contenido nuevo
    is_axioma_source: bool = False
    attempt: int = 1
    state: ProposalState = ProposalState.STAGED
    shadow_root: Optional[Path] = None
    workspace_id: Optional[str] = None
    report_dir: Optional[Path] = None
    report_path: Optional[Path] = None
    diff_path: Optional[Path] = None
    content_path: Optional[Path] = None
    tests_found: List[str] = field(default_factory=list)
    tests_passed: Optional[bool] = None
    syntax_ok: Optional[bool] = None
    summary: str = ""
    created_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "operation": self.operation,
            "target_path": str(self.target_path),
            "attempt": self.attempt,
            "state": self.state.value,
            "syntax_ok": self.syntax_ok,
            "tests_passed": self.tests_passed,
            "tests_found": self.tests_found,
            "report_path": str(self.report_path) if self.report_path else None,
            "diff_path": str(self.diff_path) if self.diff_path else None,
            "content_path": str(self.content_path) if self.content_path else None,
            "summary": self.summary,
        }


class PromotionGate:
    """
    Orquesta el ciclo sandbox → análisis → informe → veredicto → promoción
    para write_file/move_file/delete_file. Ver cabecera del módulo.
    """

    def __init__(self) -> None:
        PROMO_ROOT.mkdir(parents=True, exist_ok=True)
        SHADOW_ROOT.mkdir(parents=True, exist_ok=True)
        self._proposals: Dict[str, Proposal] = {}
        self._pending_by_session: Dict[str, str] = {}   # session_id -> proposal_id
        self._attempts_by_key: Dict[str, int] = {}       # stable_key -> intentos usados
        # ✅ FIX (hallazgo agente filesystem v1.6.0): antes self._staging era
        # UNA sola StagingFS compartida con session_id="promotion_gate" fijo
        # — PromotionGate es singleton de proceso, así que TODAS las sesiones
        # de usuario reusaban el mismo StagingFS. Su _registry interno indexa
        # por path resuelto, no por sesión: si dos sesiones distintas tenían
        # una propuesta pendiente para el MISMO target_path, el segundo
        # stage_write() pisaba la entry del primero en _registry — el
        # commit() de la sesión A terminaba escribiendo el contenido de la
        # sesión B. get_staging() ya soporta instancias aisladas por
        # session_id real (ver sandbox_staging.py) — ver _promote(), que
        # ahora pide get_staging(session_id=proposal.session_id) en el
        # momento de usarlo en vez de una instancia fija en __init__.
        self._workspaces = get_workspace_manager()
        # ✅ FIX AUDITORÍA (S4): antes _analyze_external() corría el archivo
        # propuesto con subprocess.run() crudo, sin límites de recursos ni
        # sanitización de output. Ahora pasa por SandboxExecutor (resource
        # limits + env limpio + redacción de secretos en el resultado que
        # el usuario lee en el informe).
        self._sandbox = get_sandbox_executor(timeout=RUN_TIMEOUT_SECONDS)
        logger.info(f"[PromotionGate] Inicializado — reports en {PROMO_ROOT}")

    # ── API pública: proponer ────────────────────────────────────────────────

    def propose_write(self, path: str, content: str, session_id: str = "default") -> Proposal:
        target = self._resolve_target(path)
        return self._propose(operation="write", target=target, session_id=session_id, content=content)

    def propose_move(self, src: str, dst: str, session_id: str = "default") -> Proposal:
        source = self._resolve_target(src)
        target = self._resolve_target(dst)
        return self._propose(operation="move", target=target, session_id=session_id, source=source)

    def propose_delete(self, path: str, session_id: str = "default") -> Proposal:
        target = self._resolve_target(path)
        return self._propose(operation="delete", target=target, session_id=session_id)

    # ── API pública: veredicto ───────────────────────────────────────────────

    def resolve_pending(self, session_id: str, verdict: str, reason: str = "") -> Dict[str, Any]:
        """Resuelve la propuesta pendiente de la sesión (no requiere proposal_id)."""
        proposal_id = self._pending_by_session.get(session_id)
        if not proposal_id:
            return {"success": False, "error": "No hay ninguna propuesta pendiente en esta sesión"}
        return self.resolve(proposal_id, verdict, reason)

    def has_pending(self, session_id: str) -> bool:
        """
        ✅ NUEVO (hallazgo agente filesystem v1.6.0): True si la sesión
        tiene una propuesta esperando veredicto. Pensado para que
        dispatcher.py condicione el override de aprobar/descartar por
        lenguaje natural a que realmente haya algo pendiente — decirle
        "aprobar" al dispatcher sin propuesta pendiente no debería forzar
        task_type=system_control (ver DISPATCHER_ROUTING_VERBS en
        system_agent.py).
        """
        return session_id in self._pending_by_session

    def resolve(self, proposal_id: str, verdict: str, reason: str = "") -> Dict[str, Any]:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            return {"success": False, "error": f"Propuesta no encontrada: {proposal_id}"}
        if proposal.state != ProposalState.ANALYZED:
            return {
                "success": False,
                "error": f"Propuesta en estado '{proposal.state.value}', no está esperando veredicto",
            }

        try:
            v = Verdict(verdict.strip().lower())
        except ValueError:
            return {"success": False, "error": f"Veredicto no reconocido: '{verdict}'. Usá: aprobar, corregir, descartar"}

        if v == Verdict.APROBAR:
            return self._promote(proposal)
        if v == Verdict.DESCARTAR:
            return self._discard(proposal, reason)
        if proposal.operation == "delete":
            return {"success": False, "error": "delete_file no admite 'corregir' — usá aprobar o descartar"}
        return self._mark_for_correction(proposal, reason)

    # ── Internals: proponer ──────────────────────────────────────────────────

    def _resolve_target(self, raw_path: str) -> Path:
        return Path(raw_path).expanduser().resolve()

    def _is_axioma_source(self, path: Path) -> bool:
        try:
            path.relative_to(settings.base_dir)
            return True
        except ValueError as e:
            log_degraded(logger, e, "src/core/promotion_gate.py:_is_axioma_source")
            return False

    def _stable_key(self, session_id: str, target: Path) -> str:
        raw = f"{session_id}:{target}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _propose(
        self, operation: str, target: Path, session_id: str,
        content: Optional[str] = None, source: Optional[Path] = None,
    ) -> Proposal:
        stable_key = self._stable_key(session_id, target)
        used = self._attempts_by_key.get(stable_key, 0)
        if used >= MAX_ATTEMPTS:
            raise RuntimeError(
                f"Máximo de {MAX_ATTEMPTS} intentos alcanzado para '{target}'. "
                f"Descartá la propuesta pendiente antes de volver a intentar."
            )

        proposal_id = f"prop_{uuid.uuid4().hex[:10]}"
        proposal = Proposal(
            proposal_id=proposal_id,
            operation=operation,
            target_path=target,
            session_id=session_id,
            stable_key=stable_key,
            source_path=source,
            content=content,
            is_axioma_source=self._is_axioma_source(target),
            attempt=used + 1,
            shadow_root=SHADOW_ROOT / stable_key,
            workspace_id=stable_key,
        )
        self._attempts_by_key[stable_key] = used + 1
        self._proposals[proposal_id] = proposal

        self._stage(proposal)
        self._analyze(proposal)
        self._write_report(proposal)

        proposal.state = ProposalState.ANALYZED
        self._pending_by_session[session_id] = proposal_id
        # ✅ FASE 2 (plan_harness): auditar la propuesta pendiente.
        self._audit_record(
            session_id=session_id, operation=operation,
            path=str(target), status=FileOpStatus.PROPOSED,
            proposal_id=proposal_id,
        )
        return proposal

    # ── Internals: stage (copia sombra o workspace, archivo real intacto) ────

    def _stage(self, proposal: Proposal) -> None:
        report_dir = PROMO_ROOT / proposal.proposal_id
        report_dir.mkdir(parents=True, exist_ok=True)
        proposal.report_dir = report_dir

        if proposal.is_axioma_source:
            self._stage_axioma_source(proposal)
        else:
            self._stage_external(proposal)

    def _stage_axioma_source(self, proposal: Proposal) -> None:
        if not proposal.shadow_root.exists():
            logger.info(f"[PromotionGate] Copiando proyecto a {proposal.shadow_root}...")
            # ✅ FIX (hallazgo test_fs_agent_temp.py — move con permission
            # denied en .optimizer_backups/*.bak ajenos a la propuesta):
            # copytree() con ignore=SHADOW_IGNORE ya completa la copia de
            # TODO lo demás y recién al final levanta shutil.Error
            # agregando los que fallaron (confirmado: el .bak con Permission
            # denied no impidió copiar el resto del árbol). Sin este
            # try/except, un permiso roto en un archivo de OTRO subsistema
            # (optimizer, no relacionado a esta propuesta) tumbaba TODA la
            # propuesta con un error opaco, tapando el error real (ej.
            # "archivo.txt no existe"). Ahora: loggea cada archivo que
            # falló (visibilidad, no se traga el problema) y sigue —
            # shadow_root queda con todo lo que SÍ se pudo copiar, que es
            # lo que _analyze_axioma_source() necesita para el archivo
            # target real de esta propuesta.
            try:
                shutil.copytree(settings.base_dir, proposal.shadow_root, ignore=SHADOW_IGNORE)
            except shutil.Error as e:
                for src, dst, err in e.args[0]:
                    logger.warning(f"[PromotionGate] No se pudo copiar '{src}' a shadow: {err}")
                if not proposal.shadow_root.exists():
                    raise
        else:
            logger.debug(f"[PromotionGate] Reusando copia sombra existente: {proposal.shadow_root}")

        rel_target = proposal.target_path.relative_to(settings.base_dir)
        shadow_target = proposal.shadow_root / rel_target

        if proposal.operation == "write":
            shadow_target.parent.mkdir(parents=True, exist_ok=True)
            shadow_target.write_text(proposal.content or "", encoding="utf-8")

        elif proposal.operation == "move":
            rel_source = proposal.source_path.relative_to(settings.base_dir)
            shadow_source = proposal.shadow_root / rel_source
            shadow_target.parent.mkdir(parents=True, exist_ok=True)
            if shadow_source.exists():
                shutil.move(str(shadow_source), str(shadow_target))

        elif proposal.operation == "delete":
            if shadow_target.exists():
                shadow_target.unlink()

    def _stage_external(self, proposal: Proposal) -> None:
        ws = Path(self._workspaces.get_workspace(proposal.workspace_id))

        if proposal.operation == "write":
            (ws / proposal.target_path.name).write_text(proposal.content or "", encoding="utf-8")
        elif proposal.operation == "move":
            if proposal.source_path and proposal.source_path.exists():
                shutil.copy2(proposal.source_path, ws / proposal.source_path.name)
        # delete: nada que copiar — se analiza directo sobre target_path real (solo lectura)

    # ── Internals: analyze ───────────────────────────────────────────────────

    def _analyze(self, proposal: Proposal) -> None:
        if proposal.is_axioma_source:
            self._analyze_axioma_source(proposal)
        else:
            self._analyze_external(proposal)

    def _check_syntax(self, content: str, label: str) -> Tuple[bool, str]:
        if not content:
            return True, ""
        try:
            ast.parse(content, filename=label)
            return True, ""
        except SyntaxError as e:
            return False, f"{e.__class__.__name__}: {e}"

    def _write_diff(self, proposal: Proposal) -> None:
        # ✅ FIX (hallazgo agente filesystem v1.6.0): errors="replace" en
        # los 3 read_text de acá — antes crasheaban con UnicodeDecodeError
        # al generar el diff de un "move"/"delete" sobre un binario. Esto
        # es solo para el informe/diff.txt que lee el usuario (nunca se
        # reescribe a disco desde acá) — mismo criterio que fs_read, no el
        # 'surrogateescape' de _promote() (que sí necesita round-trip exacto).
        if proposal.operation == "write":
            original = proposal.target_path.read_text(encoding="utf-8", errors="replace") if proposal.target_path.exists() else ""
            new_content = proposal.content or ""
            from_label, to_label = str(proposal.target_path), str(proposal.target_path)
        elif proposal.operation == "move":
            original = (
                proposal.source_path.read_text(encoding="utf-8", errors="replace")
                if proposal.source_path and proposal.source_path.exists() else ""
            )
            new_content = original  # el contenido no cambia, solo la ubicación
            from_label, to_label = str(proposal.source_path), str(proposal.target_path)
        else:  # delete
            original = proposal.target_path.read_text(encoding="utf-8", errors="replace") if proposal.target_path.exists() else ""
            new_content = ""
            from_label, to_label = str(proposal.target_path), "/dev/null"

        diff_lines = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
        ))
        diff_path = proposal.report_dir / "diff.txt"
        diff_path.write_text("".join(diff_lines) or "(sin diferencias de contenido)\n", encoding="utf-8")
        proposal.diff_path = diff_path

        if proposal.operation in ("write", "move"):
            content_path = proposal.report_dir / "proposed_content.txt"
            content_path.write_text(new_content, encoding="utf-8")
            proposal.content_path = content_path

    def _discover_tests(self, module_path: Path, project_root: Path) -> List[Path]:
        """
        Heurística: convención de nombre (test_<stem>.py) + grep de
        imports en tests/ que referencien el módulo. Sin dependency graph
        real — si no encuentra nada, el informe lo dice explícito en vez
        de fingir que se verificó algo.
        """
        tests_dir = project_root / "tests"
        if not tests_dir.is_dir():
            return []

        stem = module_path.stem
        found: List[Path] = []

        for candidate in (f"test_{stem}.py", f"{stem}_test.py"):
            p = tests_dir / candidate
            if p.is_file():
                found.append(p)

        try:
            for test_file in tests_dir.glob("*.py"):
                if test_file in found:
                    continue
                text = test_file.read_text(encoding="utf-8", errors="ignore")
                if f"import {stem}" in text or f".{stem}" in text or f"{stem}." in text:
                    found.append(test_file)
        except Exception as e:
            logger.debug(f"[PromotionGate] Error grep de tests: {e}")

        return found

    def _shadow_env(self, proposal: Proposal) -> Dict[str, str]:
        """
        Env vars que redirigen BASE_DIR/DATA_DIR/LOGS_DIR/CONFIG_DIR/CACHE_DIR
        a la copia sombra. Settings (pydantic BaseSettings) los toma de env
        var con el mismo nombre de campo; el validador solo fuerza el join
        con el path real si el valor recibido es relativo — si ya es
        absoluto (como estos), lo respeta. Sin esto, cualquier test que
        toque memoria/DB escribiría en el proyecto real aunque el código
        bajo test viva en la copia.
        """
        shadow_data = proposal.shadow_root / "_shadow_data"
        env = os.environ.copy()
        env["BASE_DIR"] = str(proposal.shadow_root)
        env["DATA_DIR"] = str(shadow_data / "data")
        env["LOGS_DIR"] = str(shadow_data / "logs")
        env["CONFIG_DIR"] = str(proposal.shadow_root / "config")
        env["CACHE_DIR"] = str(shadow_data / "cache")
        for d in (env["DATA_DIR"], env["LOGS_DIR"], env["CACHE_DIR"]):
            Path(d).mkdir(parents=True, exist_ok=True)
        return env

    def _run_pytest(self, proposal: Proposal, targets: List[str]) -> Tuple[Optional[bool], str]:
        if not targets:
            return None, "Sin test conocido para este archivo — revisá el diff manualmente."
        env = self._shadow_env(proposal)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *targets, "-v", "--tb=short"],
                cwd=str(proposal.shadow_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=PYTEST_TIMEOUT_SECONDS,
            )
            return result.returncode == 0, (result.stdout or "")[-2000:]
        except subprocess.TimeoutExpired:
            return False, f"Timeout ({PYTEST_TIMEOUT_SECONDS}s) corriendo tests"
        except Exception as e:
            return False, f"Error corriendo pytest: {e}"

    def _analyze_axioma_source(self, proposal: Proposal) -> None:
        rel_target = proposal.target_path.relative_to(settings.base_dir)
        shadow_target = proposal.shadow_root / rel_target

        if proposal.operation == "write":
            content = shadow_target.read_text(encoding="utf-8") if shadow_target.exists() else ""
            if shadow_target.suffix == ".py":
                ok, err = self._check_syntax(content, str(shadow_target))
                proposal.syntax_ok = ok
                if not ok:
                    proposal.tests_passed = False
                    proposal.summary = f"Sintaxis inválida — no se corrieron tests.\n{err}"
                    self._write_diff(proposal)
                    return
            else:
                proposal.syntax_ok = True

            tests = self._discover_tests(proposal.target_path, proposal.shadow_root)
            proposal.tests_found = [str(t.relative_to(proposal.shadow_root)) for t in tests]
            passed, tail = self._run_pytest(proposal, [str(t) for t in tests])
            proposal.tests_passed = passed
            proposal.summary = tail

        elif proposal.operation in ("move", "delete"):
            # Mover o borrar puede romper imports en archivos no tocados —
            # sin impact analysis real, se corre la suite completa.
            tests_dir = proposal.shadow_root / "tests"
            targets = [str(tests_dir)] if tests_dir.is_dir() else []
            passed, tail = self._run_pytest(proposal, targets)
            proposal.tests_passed = passed
            proposal.tests_found = ["tests/ (suite completa)"] if targets else []
            proposal.summary = tail
            proposal.syntax_ok = True

        self._write_diff(proposal)

    def _analyze_external(self, proposal: Proposal) -> None:
        ws = Path(self._workspaces.get_workspace(proposal.workspace_id))

        if proposal.operation == "delete":
            proposal.syntax_ok = True
            proposal.tests_passed = None
            exists = proposal.target_path.exists()
            size = proposal.target_path.stat().st_size if exists else 0
            proposal.summary = (
                f"Archivo {'existe' if exists else 'NO existe'} — {size} bytes. "
                f"No aplica ejecución de prueba para un borrado."
            )
            self._write_diff(proposal)
            return

        filename = (proposal.target_path if proposal.operation == "write" else proposal.source_path).name
        target_in_ws = ws / filename
        # ✅ FIX (hallazgo agente filesystem v1.6.0): errors="replace" —
        # antes crasheaba (UnicodeDecodeError) analizando un "move" de
        # binario, incluso para extensiones no-.py (el check de suffix
        # está DESPUÉS de este read_text). El resultado solo se usa para
        # el check de sintaxis .py de abajo; para no-.py se descarta en
        # la siguiente línea sin usarse.
        content = target_in_ws.read_text(encoding="utf-8", errors="replace") if target_in_ws.exists() else (proposal.content or "")

        if target_in_ws.suffix != ".py":
            proposal.syntax_ok = True
            proposal.tests_passed = None
            proposal.summary = "Archivo no ejecutable (no .py) — solo se valida el diff."
            self._write_diff(proposal)
            return

        ok, err = self._check_syntax(content, str(target_in_ws))
        proposal.syntax_ok = ok
        if not ok:
            proposal.summary = f"Sintaxis inválida.\n{err}"
            proposal.tests_passed = False
            self._write_diff(proposal)
            return

        python_bin = self._workspaces.get_venv_python(proposal.workspace_id)
        # ✅ FIX AUDITORÍA (S4): antes esto era subprocess.run() directo, sin
        # límites de recursos ni sanitización — el código propuesto por el
        # LLM/usuario corría con los mismos permisos que el proceso de AXIOMA,
        # antes de que el usuario diera el veredicto. Se usa el venv de la
        # sesión (interpreter=[python_bin]) vía SandboxExecutor para no perder
        # los paquetes que install_package()/install_packages() ya instalaron
        # ahí — sys.executable del host no los tendría.
        result = self._sandbox.execute_script(
            target_in_ws,
            cwd=ws,
            timeout=RUN_TIMEOUT_SECONDS,
            interpreter=[python_bin],
        )
        proposal.tests_passed = (result.returncode == 0) and not result.timeout
        if result.timeout:
            proposal.summary = f"Timeout ({RUN_TIMEOUT_SECONDS}s) ejecutando el archivo"
        elif result.error and result.returncode == -1:
            proposal.summary = f"Error ejecutando: {result.error}"
        else:
            proposal.summary = ((result.stdout or "") + (result.stderr or ""))[-2000:]

        self._write_diff(proposal)

    # ── Internals: informe ───────────────────────────────────────────────────

    def _write_report(self, proposal: Proposal) -> None:
        lines = [
            f"Propuesta: {proposal.proposal_id} (intento {proposal.attempt}/{MAX_ATTEMPTS})",
            f"Operación: {proposal.operation}",
            f"Destino: {proposal.target_path}",
        ]
        if proposal.source_path:
            lines.append(f"Origen: {proposal.source_path}")
        lines.append(f"Ubicación: {'código fuente de AXIOMA' if proposal.is_axioma_source else 'archivo externo'}")
        lines.append(f"Sintaxis: {'OK' if proposal.syntax_ok else 'FALLA' if proposal.syntax_ok is False else 'N/A'}")
        if proposal.tests_found:
            lines.append(f"Tests encontrados: {', '.join(proposal.tests_found)}")
        lines.append(
            "Resultado: "
            + ("PASÓ" if proposal.tests_passed else "FALLÓ" if proposal.tests_passed is False else "sin ejecución (ver diff)")
        )
        lines.append("")
        lines.append("── Detalle ──")
        lines.append(proposal.summary or "(sin detalle adicional)")
        lines.append("")
        lines.append(f"Diff completo: {proposal.diff_path}")
        if proposal.content_path:
            lines.append(f"Contenido propuesto completo: {proposal.content_path}")
        lines.append("")
        lines.append("Decisión: 'aprobar' | 'corregir' | 'descartar'")

        report_path = proposal.report_dir / "report.txt"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        proposal.report_path = report_path

    # ── Internals: veredicto ─────────────────────────────────────────────────

    def _promote(self, proposal: Proposal) -> Dict[str, Any]:
        try:
            # ✅ FASE 2 (plan_harness): auditar fallos de commit como failed.
            def _audit_fail(msg: str) -> Dict[str, Any]:
                self._audit_record(
                    session_id=proposal.session_id,
                    operation=proposal.operation,
                    path=str(proposal.target_path),
                    status=FileOpStatus.FAILED,
                    proposal_id=proposal.proposal_id,
                )
                return {"success": False, "error": msg}

            # ✅ FIX (hallazgo agente filesystem v1.6.0): instancia de
            # staging aislada por sesión real — ver comentario en __init__.
            staging = get_staging(session_id=proposal.session_id)

            if proposal.operation == "write":
                ok, err = staging.stage_write(proposal.target_path, proposal.content or "")
                if not ok:
                    return _audit_fail(f"No se pudo preparar la escritura: {err}")
                ok, err = staging.commit(proposal.target_path)
                if not ok:
                    return _audit_fail(f"Commit falló (revert aplicado): {err}")

            elif proposal.operation == "move":
                # ✅ FIX (hallazgo agente filesystem v1.6.0): antes
                # read_text(encoding="utf-8") sin errors= crasheaba
                # (UnicodeDecodeError, atrapado por el except Exception de
                # abajo) al mover un archivo binario/programa — el "move"
                # pasa TODO por el mismo staging de texto que "write". Ahora
                # se clasifica con _classify_kind() (mismo criterio que
                # fs_read/copy_file): texto → 'strict' sin cambios; binario/
                # programa → 'surrogateescape', que hace round-trip byte a
                # byte exacto entre el read() de acá y el write_text() de
                # staging.commit() (ver stage_write()/commit() en
                # sandbox_staging.py) — a diferencia de 'replace' (fs_read),
                # que es lossy y serviría para MOSTRAR contenido pero
                # corrompería el archivo movido.
                src_exists = proposal.source_path.exists()
                kind = _classify_kind(proposal.source_path) if src_exists else "text"
                read_errors = "strict" if kind == "text" else "surrogateescape"
                content = proposal.source_path.read_text(encoding="utf-8", errors=read_errors) if src_exists else ""
                ok, err = staging.stage_write(proposal.target_path, content, errors=read_errors)
                if not ok:
                    return _audit_fail(f"No se pudo preparar el destino: {err}")
                ok, err = staging.commit(proposal.target_path)
                if not ok:
                    return _audit_fail(f"Commit falló (revert aplicado): {err}")
                if proposal.source_path.exists():
                    proposal.source_path.unlink()

            elif proposal.operation == "delete":
                backup_dir = proposal.report_dir / "deleted_backup"
                backup_dir.mkdir(parents=True, exist_ok=True)
                if proposal.target_path.exists():
                    shutil.copy2(proposal.target_path, backup_dir / proposal.target_path.name)
                    proposal.target_path.unlink()

            proposal.state = ProposalState.PROMOTED
            self._cleanup(proposal, final=True)
            self._pending_by_session.pop(proposal.session_id, None)
            self._attempts_by_key.pop(proposal.stable_key, None)
            # ✅ FASE 2 (plan_harness): auditar el commit exitoso con checksum.
            self._audit_record(
                session_id=proposal.session_id,
                operation=proposal.operation,
                path=str(proposal.target_path),
                status=FileOpStatus.COMMITTED,
                proposal_id=proposal.proposal_id,
            )
            return {
                "success": True,
                "proposal_id": proposal.proposal_id,
                "message": f"{proposal.operation} promovido: {proposal.target_path}",
            }
        except Exception as e:
            logger.error(f"[PromotionGate] Error promoviendo {proposal.proposal_id}: {e}")
            self._audit_record(
                session_id=proposal.session_id,
                operation=proposal.operation,
                path=str(proposal.target_path),
                status=FileOpStatus.FAILED,
                proposal_id=proposal.proposal_id,
            )
            return {"success": False, "error": str(e)}

    def _audit_record(
        self,
        session_id: str,
        operation: str,
        path: str,
        status: str,
        proposal_id: Optional[str] = None,
    ) -> None:
        """Registra una operación en el FileAuditTrail. Silencioso si no disponible."""
        if _AUDIT_AVAILABLE:
            try:
                audit = get_file_audit()
                if audit is not None:
                    audit.record(
                        session_id=session_id,
                        operation=operation,
                        path=path,
                        status=status,
                        proposal_id=proposal_id,
                    )
            except Exception as e:
                logger.debug(f"[PromotionGate] Audit record error (non-fatal): {e}")
        # ✅ FASE 3 (plan_harness): publicar el evento file.operation al bus
        # (SecurityListener lo consume para logger + DetailedLogger).
        if _PROMOTION_EVENT_TYPES_AVAILABLE:
            try:
                from src.core.event_bus import get_event_bus
                get_event_bus().publish_simple(
                    _PromotionEventTypes.FILE_OPERATION,
                    {
                        "session_id": session_id,
                        "operation": operation,
                        "path": path,
                        "status": status,
                        "proposal_id": proposal_id,
                    },
                    source="promotion_gate",
                )
            except Exception as e:
                logger.debug(f"[PromotionGate] Event publish error (non-fatal): {e}")

    def _discard(self, proposal: Proposal, reason: str) -> Dict[str, Any]:
        proposal.state = ProposalState.DISCARDED
        self._cleanup(proposal, final=True)
        self._pending_by_session.pop(proposal.session_id, None)
        self._attempts_by_key.pop(proposal.stable_key, None)
        # ✅ FASE 2 (plan_harness): auditar el descarte (archivo no tocado).
        self._audit_record(
            session_id=proposal.session_id,
            operation=proposal.operation,
            path=str(proposal.target_path),
            status=FileOpStatus.DISCARDED,
            proposal_id=proposal.proposal_id,
        )
        return {
            "success": True,
            "proposal_id": proposal.proposal_id,
            "message": f"Propuesta descartada — el archivo real no fue tocado. {reason}".strip(),
        }

    def _mark_for_correction(self, proposal: Proposal, reason: str) -> Dict[str, Any]:
        proposal.state = ProposalState.CORRECTING
        self._pending_by_session.pop(proposal.session_id, None)

        used = self._attempts_by_key.get(proposal.stable_key, proposal.attempt)
        remaining = MAX_ATTEMPTS - used

        if remaining <= 0:
            return {
                "success": False,
                "proposal_id": proposal.proposal_id,
                "message": (
                    f"Se alcanzó el máximo de {MAX_ATTEMPTS} intentos para {proposal.target_path}. "
                    f"El archivo real sigue intacto. Descartá explícitamente para reiniciar el contador "
                    f"(la copia de trabajo se mantiene hasta entonces)."
                ),
            }

        # No se limpia shadow/workspace: el próximo propose_*() con el mismo
        # (sesión, path) reusa la copia y solo pisa el archivo cambiado.
        return {
            "success": True,
            "proposal_id": proposal.proposal_id,
            "message": (
                f"Descartado el intento {proposal.attempt} — el archivo real no fue tocado. "
                f"Mandá el contenido corregido ({remaining} intento(s) restante(s))."
            ),
        }

    def _cleanup(self, proposal: Proposal, final: bool) -> None:
        """final=True (aprobado/descartado): limpia copia sombra y workspace.
        final=False (corregir): no-op — se reusan en el próximo intento."""
        if not final:
            return
        if proposal.shadow_root and proposal.shadow_root.exists():
            shutil.rmtree(proposal.shadow_root, ignore_errors=True)
        if proposal.workspace_id:
            self._workspaces.cleanup(proposal.workspace_id)
        # report_dir (informe + diff + contenido) se conserva para consulta
        # posterior — no se borra automáticamente.


# ============================================================================
# SINGLETON
# ============================================================================
_gate: Optional[PromotionGate] = None


def get_promotion_gate() -> PromotionGate:
    global _gate
    if _gate is None:
        _gate = PromotionGate()
    return _gate
