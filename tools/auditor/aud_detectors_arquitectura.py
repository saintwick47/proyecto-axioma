#!/usr/bin/env python3
"""
tools/auditor/detectors_arquitectura.py — detectores de seguridad/arquitectura (harness, plan v2, config, singletons, ciclos, duplicados, integración)

v0.6.8uu: extraído de axioma_auditor.py (monolítico v8.3, 3112 líneas) para
facilitar el mantenimiento. Este paquete divide el auditor en 5 módulos
(≤900 líneas c/u); axioma_auditor.py en la raíz quedó como shim de
compatibilidad (CLI y exports idénticos).
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)
from tools.auditor.aud_core import (
    BaseDetector, AudSeverity, AudVerdict,
    _resolve_imports_for, _resolve_top_level_imports_for, _to_snake_case,
)


# ============================================================================
# DETECTOR: Seguridad FASE 1-3 (plan_harness) — protecciones implementadas
# ============================================================================
# ✅ v8.1 (2026-08-26): Verifica la presencia de las protecciones agregadas
# por el plan_harness (doc original ausente del repo; la verificación
# canónica son estos detectores + tests/test_axioma_07_seguridad_modos.py):
#   FASE 1: SandboxUnavailableError, fail-closed sandbox, PermissionMode.DENY,
#           RuntimeMode, reglas deny en permissions.json, list_for_mode,
#           filtro RuntimeMode en ToolExecutor, settings.runtime_mode.
#   FASE 2: SessionEventLog, FileAuditTrail, integración PromotionGate.
#   FASE 3: EventBus, domain/events, SecurityListener.
# Emite hallazgos INFO solo cuando una protección FALTA (regresión).
# ============================================================================

# (archivo, símbolo/búsqueda, nombre de la protección)
_SECURITY_CHECKS = [
    # FASE 1
    ("src/domain/exceptions.py", "class SandboxUnavailableError",
     "SandboxUnavailableError (fail-closed del sandbox)"),
    ("src/domain/types.py", "class RuntimeMode",
     "RuntimeMode enum (modos de ejecución)"),
    ("src/domain/types.py", "def allowed_permission_level",
     "RuntimeMode.allowed_permission_level()"),
    ("src/tools_mcp/permissions.py", "DENY         = \"deny\"",
     "PermissionMode.DENY"),
    ("src/tools_mcp/permissions.py", "mode == PermissionMode.DENY",
     "Aplicación de PermissionMode.DENY en _apply_mode"),
    ("src/tools_mcp/sandbox.py", "def _can_confine",
     "SandboxExecutor._can_confine()"),
    ("src/tools_mcp/sandbox.py", "def _raise_if_cannot_confine",
     "SandboxExecutor._raise_if_cannot_confine() (fail-closed)"),
    ("src/tools_mcp/tool_registry.py", "def list_for_mode",
     "ToolRegistry.list_for_mode()"),
    ("src/tools_mcp/tool_executor.py", "runtime_mode: Optional",
     "ExecutionContext.runtime_mode"),
    ("src/tools_mcp/tool_executor.py", "RUNTIME_MODE_BLOCKED",
     "Filtro RuntimeMode en ToolExecutor.run (paso 2.5)"),
    ("config/settings.py", "runtime_mode: Literal",
     "settings.runtime_mode"),
    # FASE 2
    ("src/learning/session_event_log.py", "class SessionEventLog",
     "SessionEventLog (bitácora de sesión)"),
    ("src/learning/file_audit.py", "class FileAuditTrail",
     "FileAuditTrail (auditoría de archivos)"),
    ("src/core/promotion_gate.py", "def _audit_record",
     "Integración FileAuditTrail en PromotionGate"),
    # FASE 3
    ("src/core/event_bus.py", "class EventBus",
     "EventBus (bus de eventos)"),
    ("src/domain/events.py", "class EventTypes",
     "EventTypes (vocabulario de eventos)"),
    ("src/core/security_listener.py", "class SecurityListener",
     "SecurityListener (observador de seguridad)"),
    ("src/tools_mcp/tool_executor.py", "def _publish_event",
     "Publicación de eventos desde ToolExecutor"),
    # gap.txt (2026-08-26) — propagación RuntimeMode a agentes
    ("src/agents/base.py", "def _resolve_runtime_mode",
     "BaseAgent._resolve_runtime_mode() (propagación a agentes)"),
    ("src/agents/base.py", "runtime_mode=self._resolve_runtime_mode()",
     "Propagación en execute_tool/execute_tool_sync"),
    ("src/interfaces/cli/command_parser.py", "settings.runtime_mode = new_mode.value",
     "Persistencia de /mode en settings (CLI → agentes)"),
    # FASE 4 — Coordinación con memoria segura
    ("src/core/memory_guard.py", "class MemoryGuard",
     "MemoryGuard (guard VRAM/RAM anti-OOM)"),
    ("src/core/memory_guard.py", "def can_load",
     "MemoryGuard.can_load() (fail-safe RAM)"),
    ("src/core/memory_guard.py", "def can_coexist",
     "MemoryGuard.can_coexist() (futuro paralelo)"),
    ("src/core/agent_coordinator.py", "class AgentCoordinator",
     "AgentCoordinator (coordinación multi-modelo)"),
    ("src/core/agent_coordinator.py", "def acquire_model",
     "AgentCoordinator.acquire_model() (drenado + guard)"),
    ("src/core/agent_coordinator.py", "def wait_for_result",
     "Dependencia cruzada con checkpoint/resume"),
    ("src/core/agent_coordinator.py", "def resume_continuation",
     "Resume de continuación (no perder hilo)"),
    ("src/core/task_board.py", "class TaskBoard",
     "TaskBoard (estados de tarea)"),
    ("src/core/task_board.py", "REFUSED_OOM",
     "Estado refused_oom (fail-safe de memoria)"),
    ("config/settings.py", "allow_parallel_models",
     "settings.allow_parallel_models (futuro paralelo)"),
    ("config/settings.py", "memory_ram_hard_ratio",
     "settings.memory_ram_hard_ratio (límite duro RAM)"),
]

# (patrón de regla, nombre de la regla) — en config/permissions.json
_SECURITY_JSON_RULES = [
    (".git/**", "regla DENY .git/** (repositorio intocable)"),
    (".env*", "regla DENY .env* (secretos intocables)"),
    ("config/**", "regla DENY config/** (solo lectura)"),
    ("data/**", "regla DENY data/** (solo lectura)"),
]


class SecurityHarnessDetector(BaseDetector):
    """Verifica la presencia de las protecciones de FASE 1-4 del plan_harness."""
    name = "security_harness"
    description = "Protecciones de seguridad FASE 1-4 (plan_harness): RuntimeMode, fail-closed, DENY, auditoría, eventos, coordinación con memoria"

    def _detect(self) -> None:
        root = self.index.root
        src_text: Dict[str, str] = {}
        for rel, text in self.index.files.items():
            key = str(rel).replace("\\", "/")
            src_text[key] = text

        def _src(rel: str) -> str:
            if rel == "config/settings.py":
                # Refactorizacion 2026-10-13: los campos de configuracion se repartieron en modulos
                # hermanos (settings_llm.py, settings_paths.py, ...). La proteccion SIGUE existiendo:
                # se busca el marcador en TODO el paquete `config/settings*.py`.
                return "\n".join(v for k, v in src_text.items() if k.startswith("config/settings"))
            return src_text.get(rel, "")

        # ── 1. Símbolos en código ────────────────────────────────
        missing_symbols = []
        for rel, symbol, label in _SECURITY_CHECKS:
            if symbol not in _src(rel):
                missing_symbols.append((rel, label))

        # ── 2. Reglas JSON ───────────────────────────────────────
        json_rules: List[str] = []
        perm_path = root / "config" / "permissions.json"
        if perm_path.exists():
            try:
                data = json.loads(perm_path.read_text(encoding="utf-8"))
                json_rules = [r.get("pattern", "") for r in data.get("rules", [])]
            except Exception:
                json_rules = []
        missing_rules = [
            (pattern, label)
            for pattern, label in _SECURITY_JSON_RULES
            if pattern not in json_rules
        ]
        src_rule_mode = next(
            (r.get("mode") for r in
             (json.loads(perm_path.read_text(encoding="utf-8")).get("rules", [])
              if perm_path.exists() else [])
             if r.get("pattern") == "src/**"),
            None,
        )

        # ── Hallazgos ────────────────────────────────────────────
        if missing_symbols:
            self._add(
                finding_id="AUD-SEC-001",
                title=f"{len(missing_symbols)} protecciones de seguridad ausentes en el código",
                severity=AudSeverity.HIGH, verdict=AudVerdict.NEW,
                file=str(root), line_start=0,
                code_snippet="; ".join(
                    f"{rel}: {label}" for rel, label in missing_symbols[:8]
                ),
                explanation=(
                    "El plan_harness (FASE 1-3) definió estas protecciones; "
                    "su ausencia indica regresión o implementación incompleta."
                ),
                recommendation="Restaurar las protecciones listadas.",
                confidence=0.9, tags={'security', 'harness', 'regression'},
            )
        if missing_rules:
            self._add(
                finding_id="AUD-SEC-002",
                title=f"{len(missing_rules)} reglas DENY ausentes en config/permissions.json",
                severity=AudSeverity.HIGH, verdict=AudVerdict.NEW,
                file="config/permissions.json", line_start=0,
                code_snippet="; ".join(f"{p} ({l})" for p, l in missing_rules),
                explanation="Las rutas críticas (.git, .env, config, data) deben estar protegidas con modo deny.",
                recommendation="Agregar las reglas DENY antes de las reglas catch-all.",
                confidence=0.95, tags={'security', 'permissions', 'regression'},
            )
        if src_rule_mode == "acceptEdits":
            self._add(
                finding_id="AUD-SEC-003",
                title="src/** sigue en acceptEdits (debe ser default → ASK)",
                severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                file="config/permissions.json", line_start=0,
                code_snippet=f"mode={src_rule_mode}",
                explanation=(
                    "El plan_harness exige que src/** SIEMPRE pida confirmación; "
                    "acceptEdits auto-aprueba ediciones con confianza alta."
                ),
                recommendation="Cambiar src/** a mode=default (min_confidence 0.99, max_risk_score 0.10).",
                confidence=0.9, tags={'security', 'permissions', 'regression'},
            )


# ============================================================================
# DETECTOR: Protecciones PLAN v2.0 (2026-09-01)
# ============================================================================
# ✅ v8.2 (2026-09-01): Verifica la presencia de las protecciones implementadas
# por el plan v2.0 (doc original ausente del repo; verificación = detectores):
#   FASE 6: content_grounding_enabled, <fuente_externa> en prompts,
#           citas [n] validadas, _last_provider, search_web() tupla,
#           wrapper de tool output, handoff sin content="".
#   FASE 3: _INJECTION_PATTERNS + match_injection, _screen_input al inicio
#           de process(), input_screening_enabled.
#   FASE 2: /traces CLI + endpoint web.
#   FASE 5: _parse_output capturado en judge.py + dataset de evals.
#   FASE 4: repo-map en coder.py (CodeIndex importado, sin archivo nuevo).
# Emite hallazgos HIGH solo cuando una protección FALTA (regresión).
# ============================================================================

# (archivo, símbolo/búsqueda, nombre de la protección, fase)
_PLANV2_CHECKS = [
    # FASE 6 — integridad de contenido externo
    ("config/settings.py", "content_grounding_enabled",
     "settings.content_grounding_enabled (Fase 6)"),
    ("src/agents/researcher.py", "<fuente_externa>",
     "Delimitador <fuente_externa> en researcher._build_search_prompt (Fase 6)"),
    ("src/agents/researcher.py", "cita inválida — se descarta",
     "Validación de citas [n] en researcher._extract_sources (Fase 6)"),
    ("src/agents/researcher.py", "api_used = search_provider",
     "api_used con proveedor real en researcher (Fase 6)"),
    ("src/agents/researcher_sources.py", "return results, provider",
     "search_web() devuelve (results, provider_name) (Fase 6)"),
    ("src/agents/searcher.py", "self._last_provider = provider",
     "_last_provider seteado en searcher._search_web (Fase 6)"),
    ("src/agents/searcher.py", "if self._last_provider:",
     "_detect_api_used() lee _last_provider (Fase 6)"),
    # ✅ v8.4 (2026-09-04): tras el refactor 50/50 (v0.6.8mm), _handle_chat
    # vive en dispatcher_handlers_extra.py — el wrapper puede estar en
    # cualquiera de las dos mitades según el split vigente.
    (("src/core/dispatcher_handlers.py", "src/core/dispatcher_handlers_extra.py"),
     "<fuente_externa>",
     "Wrapper de tool output en _handle_chat (Fase 6)"),
    # FASE 3 — gate anti-injection
    ("src/agents/validator.py", "_INJECTION_PATTERNS",
     "ValidatorAgent._INJECTION_PATTERNS (Fase 3)"),
    ("src/agents/validator.py", "def match_injection",
     "ValidatorAgent.match_injection() (Fase 3)"),
    # ✅ v0.6.9v: `_screen_input` se movió al mixin guard en el refactor 50/50
    # (dispatcher_process_guard.py). La protección sigue intacta y sigue siendo
    # llamada desde process() → el check busca en la FAMILIA de módulos, y se
    # agrega un check explícito de que sigue CABLEADA desde process().
    (("src/core/dispatcher_process.py", "src/core/dispatcher_process_guard.py"),
     "def _screen_input",
     "DispatcherProcessMixin._screen_input() (Fase 3)"),
    ("src/core/dispatcher_process.py", "self._screen_input(message.content)",
     "Llamada a _screen_input desde process() (Fase 3)"),
    ("src/core/dispatcher_process.py", "input_blocked",
     "Respuesta input_blocked en process() (Fase 3)"),
    ("config/settings.py", "input_screening_enabled",
     "settings.input_screening_enabled (Fase 3)"),
    # FASE 2 — observabilidad
    ("src/interfaces/cli/command_parser.py", "def _cmd_traces",
     "Comando CLI /traces (Fase 2)"),
    ("src/interfaces/web/routes_extended.py", '@router.get("/traces")',
     "Endpoint web GET /api/v1/traces (Fase 2)"),
    # FASE 5 — evals
    ("src/llm/judge.py", "Judge parse failed",
     "Fix _parse_output capturado en judge._call (Fase 5)"),
    # FASE 4 — repo-map (sin archivo nuevo)
    ("src/agents/coder.py", "from axioma_auditor import CodeIndex",
     "Repo-map: CodeIndex importado en coder.py (Fase 4)"),
    ("src/agents/coder.py", "_build_repo_context",
     "Repo-map: _build_repo_context() en coder.py (Fase 4)"),
    ("config/settings.py", "repo_map_enabled",
     "settings.repo_map_enabled (Fase 4)"),
]


class PlanV2ProtectionsDetector(BaseDetector):
    """Verifica la presencia de las protecciones del PLAN v2.0 (Fases 2-6)."""
    name = "plan_v2_protections"
    description = "Protecciones PLAN v2.0: grounding de contenido, anti-injection, /traces, evals, repo-map"

    def _detect(self) -> None:
        src_text: Dict[str, str] = {}
        for rel, text in self.index.files.items():
            key = str(rel).replace("\\", "/")
            src_text[key] = text

        missing = []
        for entry in _PLANV2_CHECKS:
            rels, symbol, label = entry[0], entry[1], entry[2]
            if isinstance(rels, str):
                rels = (rels,)
            if not any(symbol in src_text.get(r, "") for r in rels):
                missing.append((" | ".join(rels), label, symbol))

        if missing:
            self._add(
                finding_id="AUD-PV2-001",
                title=f"{len(missing)} protecciones del PLAN v2.0 ausentes",
                severity=AudSeverity.HIGH, verdict=AudVerdict.NEW,
                file=str(self.index.root), line_start=0,
                code_snippet="; ".join(
                    f"{rel}: {label}" for rel, label, _ in missing[:10]
                ),
                explanation=(
                    "El plan v2.0 (2026-09-01) definió estas protecciones "
                    "(Fases 2-6); su ausencia indica regresión o "
                    "implementación incompleta."
                ),
                recommendation="Restaurar las protecciones listadas.",
                confidence=0.9, tags={'security', 'plan-v2', 'regression'},
            )
        # Verificación de no-regresión de Fase 1 (descartada por diseño)
        _settings_txt = "\n".join(v for k, v in src_text.items() if k.startswith("config/settings"))
        if "hybrid_search_enabled" in _settings_txt:
            self._add(
                finding_id="AUD-PV2-002",
                title="hybrid_search_enabled presente (Fase 1 descartada por diseño)",
                severity=AudSeverity.INFO, verdict=AudVerdict.NEW,
                file="config/settings.py", line_start=0,
                code_snippet="hybrid_search_enabled",
                explanation=(
                    "La Fase 1 (retrieval híbrido) se descartó por falta de "
                    "caller real de search_semantic. Si aparece este flag, "
                    "alguien la reactivó sin consumidor — revisar."
                ),
                recommendation="Eliminar el flag si no hay caller real.",
                confidence=0.6, tags={'plan-v2', 'dead-flag'},
            )


# ============================================================================
# DETECTOR: Referencias a config (TaskType/Paths/settings inexistentes)
# ============================================================================
# ✅ v8.1 (2026-08-27): fusión de tools/verify_all.py (eliminado) — los
# checks de sintaxis/imports ya los cubre syntax_import; este detector
# conserva los checks ÚNICOS de verify_all (TaskType.X / Paths.X /
# settings.X inexistentes) pero con verificación DINÁMICA contra los
# enums/clases reales, no listas estáticas curadas.
# ============================================================================

class ConfigRefsDetector(BaseDetector):
    """Referencias a atributos inexistentes de TaskType, Paths y settings."""
    name = "config_refs"
    description = "TaskType.X / Paths.X / settings.X inexistentes (fusionado de tools/verify_all.py)"

    def _load_symbols(self):
        """Símbolos reales de TaskType/Paths/settings (degradación silenciosa)."""
        task_types: set = set()
        paths_attrs: set = set()
        settings_attrs: set = set()
        try:
            from src.domain.types import TaskType
            # Miembros del enum + classmethods (is_search_related, etc.)
            task_types = {k for k in vars(TaskType) if not k.startswith("_")}
        except Exception:
            pass
        try:
            from config.paths import Paths
            paths_attrs = {a for a in dir(Paths) if not a.startswith("_")}
        except Exception:
            pass
        try:
            from config.settings import settings
            settings_attrs = {a for a in dir(settings) if not a.startswith("_")}
        except Exception:
            pass
        return task_types, paths_attrs, settings_attrs

    def _detect(self) -> None:
        """Detecta `settings.X`/`Paths.X`/`TaskType.X` inexistentes con AST.

        ✅ v0.7.5 (A4): antes recorría las líneas crudas con regex, así que
        marcaba strings, anotaciones `def f(x: "settings.campo")` y comentarios
        al final de línea (5 MEDIUM falsos en un fixture). El AST sólo ve
        accesos REALES: `ast.Attribute` cuyo `value` es el `Name` del módulo.
        Docstrings, strings, comentarios y anotaciones string no son
        `ast.Attribute` → no se marcan. Un `settings.atributo_inexistente`
        usado de verdad sí.
        """
        task_types, paths_attrs, settings_attrs = self._load_symbols()
        if not (task_types or paths_attrs or settings_attrs):
            return  # sin símbolos de referencia → nada que verificar

        # Archivos donde se DEFINEN los símbolos (no marcar su propia def)
        skip_files = {"src/domain/types.py", "config/paths.py",
                      "config/settings.py", "config/settings_computations.py",
                      "axioma_auditor.py"}
        checks = {
            "TaskType": (task_types, "no existe en src/domain/types.py",
                         "src/domain/types.py"),
            "Paths": (paths_attrs, "no existe en config/paths.py",
                      "config/paths.py"),
            "settings": (settings_attrs, "no existe en config/settings.py",
                         "config/settings.py"),
        }

        for rel, tree in self.index.asts.items():
            if str(rel).replace("\\", "/") in skip_files:
                continue
            lines = self.index.files.get(rel, "").splitlines()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)):
                    continue
                entry = checks.get(node.value.id)
                if entry is None:
                    continue
                valid, reason, target_file = entry
                name = node.attr
                # Grupo sin símbolos (import falló): no marcar TODO como typo.
                if not valid or name in valid:
                    continue
                # Falsos positivos históricos de nombres de módulo/atributos.
                if name in ("py", "settings", "env"):
                    continue
                line = (lines[node.lineno - 1].strip()
                        if 0 < node.lineno <= len(lines) else "")
                self._add(
                    finding_id=f"AUD-CFG-{len(self.findings)+1:03d}",
                    title=f"{node.value.id}.{name} inexistente en {rel}",
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=node.lineno,
                    code_snippet=line[:200],
                    explanation=(f"Referencia real (AST) a "
                                 f"{node.value.id}.{name} que {reason}."),
                    recommendation=(
                        f"Corregir la referencia o verificar el nombre real en "
                        f"{target_file}."
                    ),
                    confidence=0.8,
                    tags={"config", "refs", "typo"},
                )


# ============================================================================
# DETECTOR: Singletons instanciados directamente (fusionado de axioma_inspector)
# ============================================================================
# ✅ v8.3 (2026-09-01): portado de axioma_inspector.py (sección 3) — clases
# singleton instanciadas con `Class()` en vez de su accessor get_*().
# ============================================================================

_SINGLETON_CLASSES = {
    "RouterCache", "FeedbackLoop", "CacheL2", "QualityMonitor",
    "EmbeddingOptimizer", "TaskQueue", "ParallelExecutor",
}


class SingletonViolationDetector(BaseDetector):
    """Instanciaciones directas de clases singleton en vez de get_*()."""
    name = "singleton_violation"
    description = "Clases singleton instanciadas directamente (fusionado de axioma_inspector)"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            if 'test' in str(rel).lower():
                continue
            # mapa child→parent para contexto de instanciación
            parent_map: Dict[int, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parent_map[id(child)] = node

            def _enclosing_function(node: ast.AST) -> Optional[str]:
                p = parent_map.get(id(node))
                while p is not None:
                    if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return p.name
                    p = parent_map.get(id(p))
                return None

            def _is_fallback_pattern(node: ast.AST) -> bool:
                p = parent_map.get(id(node))
                return isinstance(p, ast.BoolOp) and isinstance(p.op, ast.Or)

            def _is_multi_instance_context(node: ast.AST) -> bool:
                """True si la instanciación crea VARIAS instancias a propósito.

                ✅ v0.6.9v: evita un falso positivo real. `TaskQueue` está en
                `_SINGLETON_CLASSES`, pero el singleton es la FACTORY
                `get_task_queue()` — no la clase. `DistributedQueue` crea una
                cola POR PARTICIÓN dentro de una dict-comprehension
                (`{name: TaskQueue(...) for name in PARTITION_NAMES}`), que es
                justamente el diseño (aislar tareas lentas de las rápidas).
                Usar la factory ahí colapsaría las particiones en una sola.
                Regla: si la llamada vive en una comprehension o en un literal
                de colección, se están creando N instancias → no es violación.
                """
                p = parent_map.get(id(node))
                while p is not None:
                    if isinstance(p, (ast.DictComp, ast.ListComp, ast.SetComp,
                                      ast.GeneratorExp, ast.Dict, ast.List,
                                      ast.Set, ast.Tuple)):
                        return True
                    p = parent_map.get(id(p))
                return False

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if name not in _SINGLETON_CLASSES:
                    continue
                enclosing = _enclosing_function(node)
                if enclosing and (enclosing.startswith("get_") or enclosing.startswith("reset_")):
                    continue
                if _is_fallback_pattern(node):
                    continue
                if _is_multi_instance_context(node):
                    continue
                self._add(
                    finding_id=f"AUD-SNG-{len(self.findings)+1:03d}",
                    title=f"Singleton instanciado directo: {name}() en {rel}",
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=node.lineno,
                    code_snippet=f"{name}() en línea {node.lineno}",
                    explanation=(
                        f"La clase singleton {name} se instancia directamente "
                        "en vez de usar su accessor get_*(). Puede crear una "
                        "segunda instancia independiente (estado duplicado)."
                    ),
                    recommendation=(
                        f"Usar get_{_to_snake_case(name)}() en lugar de {name}()."
                    ),
                    confidence=0.85,
                    tags={'singleton', 'wiring'},
                )


# ============================================================================
# DETECTOR: asyncio a nivel módulo (fusionado de axioma_inspector)
# ============================================================================
# ✅ v8.3 (2026-09-01): portado de axioma_inspector.py (sección 4) —
# asyncio.get_event_loop() deprecado y primitivas asyncio a nivel módulo.
# ============================================================================

class AsyncModuleLevelDetector(BaseDetector):
    """asyncio.get_event_loop() y primitivas asyncio fuera de coroutine."""
    name = "async_module_level"
    description = "asyncio.get_event_loop() y primitivas asyncio a nivel módulo (fusionado de axioma_inspector)"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            # get_event_loop() deprecado
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "get_event_loop" \
                        and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "asyncio":
                    self._add(
                        finding_id=f"AUD-ASY-{len(self.findings)+1:03d}",
                        title=f"asyncio.get_event_loop() en {rel}:{node.lineno}",
                        severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                        file=str(rel), line_start=node.lineno,
                        code_snippet="asyncio.get_event_loop()",
                        explanation="Deprecado en 3.10 — puede devolver un loop distinto al running.",
                        recommendation="Usar asyncio.get_running_loop() dentro de coroutine o asyncio.run().",
                        confidence=0.95, tags={'asyncio', 'deprecated'},
                    )
            # primitivas asyncio asignadas a nivel módulo (col_offset == 0)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign) or node.col_offset != 0:
                    continue
                val = node.value
                if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                        and isinstance(val.func.value, ast.Name)
                        and val.func.value.id == "asyncio"):
                    continue
                attr = val.func.attr
                if attr in ("Lock", "Event", "Semaphore"):
                    self._add(
                        finding_id=f"AUD-ASY-{len(self.findings)+1:03d}",
                        title=f"asyncio.{attr}() a nivel módulo en {rel}:{node.lineno}",
                        severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                        file=str(rel), line_start=node.lineno,
                        code_snippet=f"asyncio.{attr}() a nivel módulo",
                        explanation=(
                            f"asyncio.{attr}() creado a nivel módulo se ata al "
                            "event loop del primer import — falla si el loop "
                            "cambia (tests, reinicios)."
                        ),
                        recommendation=(
                            f"Instanciar asyncio.{attr}() dentro de la coroutine "
                            "o usar threading.{attr}() si es cross-loop."
                        ),
                        confidence=0.9, tags={'asyncio', 'module-level'},
                    )


# ============================================================================
# DETECTOR: Imports circulares por grafo (fusionado de axioma_inspector)
# ============================================================================
# ✅ v8.3 (2026-09-01): portado de axioma_inspector.py (sección 2) — análisis
# de grafo estático de imports (DFS) para listar ciclos completos. El
# syntax_import solo detecta circulares que fallan al importar.
# ============================================================================

class CircularImportDetector(BaseDetector):
    """Ciclos de import entre módulos src./config./tools. (grafo estático)."""
    name = "circular_import"
    description = "Imports circulares por grafo estático (fusionado de axioma_inspector)"

    def _detect(self) -> None:
        all_paths = {str(rel).replace("\\", "/") for rel in self.index.files}
        pkg_to_init: Dict[str, str] = {}
        for p in all_paths:
            if p.endswith("__init__.py"):
                pkg = p[:-len("__init__.py")].rstrip("/")
                if pkg:
                    pkg_to_init[pkg] = p

        graph: Dict[str, Set[str]] = defaultdict(set)
        for rel, tree in self.index.asts.items():
            r = str(rel).replace("\\", "/")
            src_mod = r.replace("/", ".").replace(".py", "")
            if src_mod.endswith(".__init__"):
                src_mod = src_mod[:-len(".__init__")]
            for imp in _resolve_top_level_imports_for(rel, tree):
                if not imp:
                    continue
                imp_path = imp.replace(".", "/")
                if imp_path + ".py" not in all_paths and imp_path not in pkg_to_init:
                    continue
                if imp.startswith(("src.", "config.", "tools.")):
                    graph[src_mod].add(imp)

        # DFS iterativo (portado tal cual de axioma_inspector)
        cycles: List[List[str]] = []
        state: Dict[str, int] = {n: 0 for n in graph}
        for start_node in graph:
            if state[start_node] == 2:
                continue
            stack = [(start_node, iter(graph.get(start_node, set())))]
            path = [start_node]
            state[start_node] = 1
            while stack:
                node, children = stack[-1]
                try:
                    neighbor = next(children)
                    if state.get(neighbor) == 1:
                        try:
                            idx = path.index(neighbor)
                            cycle = path[idx:] + [neighbor]
                        except ValueError:
                            cycle = path + [neighbor]
                        if cycle not in cycles:
                            cycles.append(cycle)
                    elif state.get(neighbor, 0) == 0:
                        state[neighbor] = 1
                        path.append(neighbor)
                        stack.append((neighbor, iter(graph.get(neighbor, set()))))
                except StopIteration:
                    state[node] = 2
                    path.pop()
                    stack.pop()

        for cycle in cycles[:20]:
            self._add(
                finding_id=f"AUD-CIR-{len(self.findings)+1:03d}",
                title=f"Import circular: {' → '.join(cycle)}",
                severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                file=str(self.index.root), line_start=0,
                code_snippet=' → '.join(cycle),
                explanation=(
                    "Ciclo de imports entre módulos — puede causar partial "
                    "initialization o fallos intermitentes al importar."
                ),
                recommendation="Mover el símbolo compartido a un módulo base o import diferido.",
                confidence=0.8, tags={'imports', 'circular', 'architecture'},
            )


# ============================================================================
# DETECTOR: Duplicados reales por AST (fusionado de axioma_inspector)
# ============================================================================
# ✅ v8.3 (2026-09-01): portado de axioma_inspector.py (sección 6) — código
# copy-paste con fingerprint AST normalizado (nombres de args normalizados,
# docstrings omitidos). Complementa name_collision (que solo lista colisiones).
# ============================================================================

_FALSE_POSITIVE_DUP_NAMES = frozenset({
    "main", "__getattr__", "health_check", "parse_args",
    "print_header", "print_success", "print_info",
    "print_warning", "print_error", "print_summary",
})
_LOG_HELPER_DUP_PATTERN = re.compile(r'^(_log_\w+|_safe_log|_get_caller_info)$')
_TOOL_PATH_DUP_MARKERS = ("tools/", "benchmarks/", "optimize_ollama",
                          "generate_report", "verify_all")


class DuplicateCodeDetector(BaseDetector):
    """Definiciones con código estructuralmente idéntico en varios archivos."""
    name = "duplicate_code"
    description = "Código duplicado real por fingerprint AST (fusionado de axioma_inspector)"

    def _detect(self) -> None:
        def _normalize(node: ast.AST) -> str:
            class _N(ast.NodeTransformer):
                def _strip_doc(self, body):
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        return body[1:]
                    return body

                def visit_FunctionDef(self, node):  # noqa: N802
                    self.generic_visit(node)
                    node.body = self._strip_doc(node.body)
                    for i, arg in enumerate(node.args.args):
                        arg.arg = f"_a{i}"
                    for i, arg in enumerate(node.args.kwonlyargs):
                        arg.arg = f"_k{i}"
                    if node.args.vararg:
                        node.args.vararg.arg = "_va"
                    if node.args.kwarg:
                        node.args.kwarg.arg = "_kw"
                    return node

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_ClassDef(self, node):  # noqa: N802
                    self.generic_visit(node)
                    node.body = self._strip_doc(node.body)
                    return node

            try:
                canonical = ast.dump(_N().visit(ast.parse(ast.unparse(node))), indent=None)
                import hashlib as _hl
                return _hl.md5(canonical.encode(), usedforsecurity=False).hexdigest()
            except Exception:
                return ""

        name_map: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for rel, tree in self.index.asts.items():
            r = str(rel).replace("\\", "/")
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and node.col_offset == 0:
                    fp = _normalize(node)
                    if fp:
                        name_map[node.name].append((r, fp))

        for name, entries in name_map.items():
            seen: Dict[str, str] = {}
            for path, fp in entries:
                if path not in seen:
                    seen[path] = fp
            entries = list(seen.items())
            if len(entries) <= 1:
                continue
            if name in _FALSE_POSITIVE_DUP_NAMES or _LOG_HELPER_DUP_PATTERN.match(name):
                continue
            test_e = [(p, fp) for p, fp in entries if "test" in p.lower()]
            tool_e = [(p, fp) for p, fp in entries
                      if any(m in p for m in _TOOL_PATH_DUP_MARKERS) and "test" not in p.lower()]
            src_e = [(p, fp) for p, fp in entries if (p, fp) not in test_e and (p, fp) not in tool_e]
            if len(src_e) >= 2:
                effective = src_e
            elif len(src_e) == 1 and len(tool_e) >= 1:
                effective = src_e + tool_e
            elif len(test_e) >= 2:
                effective = test_e
            else:
                continue
            if len(set(fp for _, fp in effective)) == 1:
                paths = [p for p, _ in effective]
                self._add(
                    finding_id=f"AUD-DUP-{len(self.findings)+1:03d}",
                    title=f"Código duplicado real: {name}() en {len(paths)} archivos",
                    severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                    file=paths[0], line_start=0,
                    code_snippet=", ".join(paths[:6]),
                    explanation=(
                        f"Definición {name} estructuralmente idéntica en "
                        f"{len(paths)} archivos — copy-paste. Cada copia "
                        "deriva por separado."
                    ),
                    recommendation="Extraer a un módulo compartido y reutilizar.",
                    confidence=0.85, tags={'duplicate', 'copy-paste', 'maintainability'},
                )


# ============================================================================
# DETECTOR: Integración (aislados/residuales) + rendimiento (fusionado de axioma_inspector)
# ============================================================================
# ✅ v8.3 (2026-09-01): portado de axioma_inspector.py (secciones 8 y 9) —
# módulos sin referencias entrantes, residuales de refactor (_old/_legacy) y
# archivos pesados / métodos largos.
# ============================================================================

_REFACTOR_SUFFIXES = ("_old", "_legacy", "_v1", "_v2", "_v3", "_deprecated")
_ISOLATED_EXCLUSIONS = ("tools/", "benchmarks/", "main.py", "hardware_report.py",
                        "network_scanner.py", "axioma_inspector.py",
                        # ✅ v0.6.9v: utilidad PERSONAL del usuario — se corre a
                        # mano para volcar los paquetes instalados (pacman -Q) a
                        # un .md. No es parte del runtime ni debe importarse: es
                        # una herramienta standalone por diseño, igual que
                        # hardware_report.py o network_scanner.py.
                        "lista_paquetes.py",
                        # ✅ v0.6.9v (ISSUE-036): RESERVADO PARA USO FUTURO —
                        # ejecución de modelos EN PARALELO cuando haya GPU.
                        # Hoy no se cablea a propósito: CPU-only + ~14 GB
                        # marginales hacen que paralelizar LLM degrade (duplica
                        # RAM y baja el throughput). NO eliminar: está listo para
                        # el día que haya VRAM para varios modelos residentes.
                        # (Ver encabezado del módulo y docs/ISSUES_HISTORY.md.)
                        "src/agents/parallel_executor.py",
                        "src/agents/parallel_executor_models.py")


def _collect_entrypoints(root: Path, all_paths) -> set:
    """Archivos con `if __name__ == "__main__":` (se ejecutan, no se importan).

    ✅ v0.6.9v: un entrypoint sin importadores NO es un gap de integración.
    """
    out = set()
    for p in all_paths:
        try:
            txt = (root / p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if '__name__ == "__main__"' in txt or "__name__ == '__main__'" in txt:
            out.add(p)
    return out


def _collect_referenced_from_tools(root: Path) -> set:
    """Stems importados desde archivos FUERA del índice (tools/ y raíz).

    ✅ v0.6.9v: el CodeIndex excluye `tools/`; sin esto, un módulo usado por una
    herramienta (p. ej. src/llm/judge.py ← tools/run_evals.py) aparecía aislado.
    """
    stems = set()
    candidates = []
    tools_dir = root / "tools"
    if tools_dir.is_dir():
        candidates.extend(tools_dir.rglob("*.py"))
    candidates.extend(root.glob("*.py"))
    for py in candidates:
        if any(part in {"__pycache__", "venv", "venv_axioma", ".venv", "node_modules"} for part in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                stems.add(node.module.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    stems.add(alias.name.rsplit(".", 1)[-1])
    return stems



def _docstring_lines(tree: ast.Module) -> set:
    """Líneas ocupadas por docstrings (módulo/clase/función y strings sueltos)."""
    doc: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            doc.update(range(start, end + 1))
    return doc


def _effective_lines(lines: List[str], node: ast.AST, doc_lines: set) -> int:
    """Líneas EFECTIVAS de un método: sin blancos, comentarios ni docstring.

    ✅ v0.7.5 (A6): el detector medía `end_lineno - lineno` (span FÍSICO), así
    que un método con 79 líneas de comentarios/docstring se marcaba como
    ">80 líneas". Un método de 100 líneas de lógica sí debe marcarse.
    """
    start = int(getattr(node, "lineno", 0) or 0)
    end = int(getattr(node, "end_lineno", start) or start)
    total = 0
    for i in range(start, end + 1):
        raw = lines[i - 1] if 0 < i <= len(lines) else ""
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or i in doc_lines:
            continue
        total += 1
    return total


class IntegrationDetector(BaseDetector):
    """Módulos aislados/residuales de refactor y archivos/métodos pesados."""
    name = "integration"
    description = "Módulos aislados/residuales + archivos pesados (fusionado de axioma_inspector)"

    def _detect(self) -> None:
        files_by_rel = {str(rel).replace("\\", "/"): rel for rel in self.index.files}
        all_paths = set(files_by_rel)
        # ✅ v0.6.9v: contexto para descartar falsos positivos de "isolated".
        _ENTRYPOINTS = _collect_entrypoints(self.index.root, all_paths)
        _REFERENCED_FROM_TOOLS = _collect_referenced_from_tools(self.index.root)
        pkg_to_init: Dict[str, str] = {}
        for p in all_paths:
            if p.endswith("__init__.py"):
                pkg = p[:-len("__init__.py")].rstrip("/")
                if pkg:
                    pkg_to_init[pkg] = p

        referenced: Set[str] = set()
        for rel, tree in self.index.asts.items():
            for imp in _resolve_imports_for(rel, tree):
                if not imp:
                    continue
                candidate = imp.replace(".", "/") + ".py"
                if candidate in all_paths:
                    referenced.add(candidate)
                    continue
                pkg_path = imp.replace(".", "/")
                if pkg_path in pkg_to_init:
                    referenced.add(pkg_to_init[pkg_path])

        residual: List[str] = []
        for p in sorted(all_paths):
            if p in referenced or p.startswith("tests/") or p.endswith("__init__.py"):
                continue
            stem = p.rsplit("/", 1)[-1][:-3]
            parent_dir = p.rsplit("/", 1)[0] if "/" in p else ""
            for suffix in _REFACTOR_SUFFIXES:
                if not stem.endswith(suffix):
                    continue
                canonical = f"{parent_dir}/{stem[:-len(suffix)]}.py" if parent_dir else f"{stem[:-len(suffix)]}.py"
                if canonical in referenced and canonical in all_paths:
                    residual.append(p)
                    break

        isolated = sorted(
            p for p in all_paths
            if p not in referenced and p not in residual
            and not p.startswith("tests/") and not p.endswith("__init__.py")
            and not any(p.startswith(exc) for exc in _ISOLATED_EXCLUSIONS)
            and not (p.startswith("src/") and p.endswith("_base.py"))
            # ✅ v0.6.9v: dos falsos positivos reales que inflaban esta categoría:
            #   1) ENTRYPOINTS: un módulo con `if __name__ == "__main__":` se
            #      EJECUTA, no se importa (ptt_axioma.py, rafael_widget.py,
            #      Rafael.py) → no es un gap de integración.
            #   2) Referencias desde FUERA del índice: el CodeIndex excluye
            #      `tools/`, así que un módulo usado por tools/ (p. ej.
            #      src/llm/judge.py ← tools/run_evals.py) parecía aislado.
            and p not in _ENTRYPOINTS
            and p.rsplit("/", 1)[-1][:-3] not in _REFERENCED_FROM_TOOLS
        )

        for m in residual[:15]:
            self._add(
                finding_id=f"AUD-INT-{len(self.findings)+1:03d}",
                title=f"Residual de refactor: {m}",
                severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                file=m, line_start=0,
                code_snippet=m,
                explanation="Sufijo _old/_legacy/_vN y nadie lo referencia — código viejo reemplazado.",
                recommendation="Eliminar si el reemplazo está confirmado.",
                confidence=0.75, tags={'refactor', 'dead-code', 'cleanup'},
            )
        for m in isolated[:15]:
            self._add(
                finding_id=f"AUD-INT-{len(self.findings)+1:03d}",
                title=f"Módulo sin referencias entrantes: {m}",
                severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                file=m, line_start=0,
                code_snippet=m,
                explanation="Ningún módulo lo importa — puede estar desconectado o ser nuevo.",
                recommendation="Verificar integración real o eliminarlo.",
                confidence=0.6, tags={'integration', 'isolated'},
            )

        # Rendimiento: archivos pesados y métodos largos
        for rel, src in self.index.files.items():
            r = str(rel).replace("\\", "/")
            lines_count = src.count("\n") + 1
            _lines = src.splitlines()
            if lines_count > 1000:
                self._add(
                    finding_id=f"AUD-PERF-{len(self.findings)+1:03d}",
                    title=f"Archivo pesado: {r} ({lines_count} líneas)",
                    severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                    file=r, line_start=0,
                    code_snippet=f"{lines_count} líneas",
                    explanation="Archivos >1000 líneas son difíciles de mantener y revisar.",
                    recommendation="Considerar dividir en módulos cohesivos.",
                    confidence=0.7, tags={'performance', 'maintainability'},
                )
            try:
                tree = self.index.asts[rel]
            except KeyError:
                continue
            _doc = _docstring_lines(tree)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fisicas = (node.end_lineno or node.lineno) - node.lineno + 1
                    efectivas = _effective_lines(_lines, node, _doc)
                    if efectivas > 80:
                        self._add(
                            finding_id=f"AUD-PERF-{len(self.findings)+1:03d}",
                            title=(f"Método largo: {r}::{node.name}() "
                                   f"({efectivas} efectivas / {fisicas} físicas)"),
                            severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                            file=r, line_start=node.lineno,
                            code_snippet=(f"{node.name}() {efectivas} líneas "
                                          f"efectivas (span {fisicas})"),
                            explanation=("Métodos >80 líneas EFECTIVAS (sin "
                                         "blancos, comentarios ni docstring) "
                                         "concentran demasiada lógica."),
                            recommendation="Dividir en helpers con responsabilidad única.",
                            confidence=0.6, tags={'performance', 'maintainability'},
                            line_end=node.end_lineno or node.lineno,
                        )


# ============================================================================
# DETECTORES: superficie de escritura sin auth (AUD-AUTH-002 / AUD-AUTH-003)
# ============================================================================
# ✅ v0.7.6: medido con TestClient — `POST /v1/upload_file`,
# `POST /v1/preflight/instalar` y `POST /v1/preflight/instalar/<id>/detener`
# aceptan un POST "simple" (multipart/query/path, SIN modelo Pydantic) y NO
# tienen `Depends(...)` de auth. Un navegador puede dispararlos cross-origin
# sin preflight (CSRF). Las dos rutas que sí tienen token
# (`/preflight/claves`, `/preflight/modelos`) no se marcan.

_MUTATING_METHODS = {"post", "put", "patch", "delete"}
# "Simple request" CORS/CSRF: sólo POST con content-type simple (form,
# multipart, text/plain). PUT/PATCH/DELETE disparan preflight → fuera.
_SIMPLE_CSRF_METHODS = {"post"}
_AUTH_HINTS = re.compile(
    r"(token|auth|seguridad|security|permission|permiso|verify|verificar|"
    r"apikey|api_key|clave|exigir|credential|login|session)", re.I)
_NON_BODY_ANNOTATIONS = {"Request", "Response", "BackgroundTasks", "WebSocket"}


def _callable_name(expr: ast.AST) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ""


def _names_in(expr: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _looks_like_auth(expr: ast.AST) -> bool:
    """¿La expresión (Depends/Security/llamada) es un chequeo de auth?"""
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and _callable_name(n.func) in (
                "Depends", "Security"):
            if any(_AUTH_HINTS.search(name) for name in _names_in(n)):
                return True
    return False


def _route_info(func: ast.AST):
    """(método, path, decorador) del primer decorador de ruta mutante."""
    for dec in getattr(func, "decorator_list", []):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                and dec.func.attr in _MUTATING_METHODS:
            ruta = ""
            if dec.args and isinstance(dec.args[0], ast.Constant) \
                    and isinstance(dec.args[0].value, str):
                ruta = dec.args[0].value
            return dec.func.attr.upper(), ruta, dec
    return None


def _has_auth(func: ast.AST, dec: ast.Call) -> bool:
    # 1) dependencies=[Depends(...)] en el decorador.
    for kw in dec.keywords:
        if kw.arg == "dependencies" and _looks_like_auth(kw.value):
            return True
    # 2) parámetro con Depends/Security de auth.
    args = getattr(func, "args", None)
    if args is not None:
        todos = (list(getattr(args, "posonlyargs", [])) + list(args.args)
                 + list(args.kwonlyargs) + [args.vararg, args.kwarg])
        for a in todos:
            default = a.default if hasattr(a, "default") else None
            if default is not None and _looks_like_auth(default):
                return True
    return False


def _pydantic_models(index) -> Set[str]:
    """Nombres de clases Pydantic (BaseModel directo o vía otro modelo)."""
    modelos: Set[str] = set()
    for nombre, defs in index.classes.items():
        for _, cls in defs:
            bases = {_callable_name(b) for b in cls.bases}
            if "BaseModel" in bases or (bases & modelos):
                modelos.add(nombre)
    return modelos


def _param_is_body_model(func: ast.AST, modelos: Set[str]) -> bool:
    args = getattr(func, "args", None)
    if args is None:
        return False
    todos = (list(getattr(args, "posonlyargs", [])) + list(args.args)
             + list(args.kwonlyargs))
    for a in todos:
        if a.arg in ("self", "cls"):
            continue
        ann = getattr(a, "annotation", None)
        if ann is None:
            continue
        if _callable_name(ann) in _NON_BODY_ANNOTATIONS:
            continue
        if _names_in(ann) & modelos:
            return True
    return False


def _mutating_routes(index):
    """[(rel, func, método, path, tiene_auth)] de handlers HTTP mutantes."""
    out = []
    for rel, tree in index.asts.items():
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            info = _route_info(func)
            if not info:
                continue
            metodo, ruta, dec = info
            out.append((rel, func, metodo, ruta, _has_auth(func, dec)))
    return out


class CsrfSimpleRequestDetector(BaseDetector):
    """AUD-AUTH-002 — POST mutante sin auth con body "simple" (CSRF)."""
    name = "csrf_simple_request"
    description = ("POST mutante sin auth y con body simple "
                   "(Query/Path/Form/File, sin modelo Pydantic) → CSRF")

    def _detect(self) -> None:
        # ✅ 2026-10-26: si el `server.py` del ÍNDICE monta el middleware GLOBAL de mismo
        # origen (`_validar_origen`), TODAS las rutas mutantes ya están protegidas contra
        # CSRF: marcarlas ruta por ruta sería un falso positivo. Se mira el índice (no el
        # archivo real) para que los tests puedan controlar el caso.
        for _ruta_idx, _src in self.index.files.items():
            if str(_ruta_idx).replace("\\", "/").endswith("interfaces/web/server.py"):
                if "_validar_origen" in _src:
                    return
        modelos = _pydantic_models(self.index)
        for rel, func, metodo, ruta, auth in _mutating_routes(self.index):
            if metodo.lower() not in _SIMPLE_CSRF_METHODS or auth:
                continue
            if _param_is_body_model(func, modelos):
                continue  # JSON body → el navegador no lo manda cross-origin
            self._add(
                finding_id=f"AUD-AUTH-002-{len(self.findings) + 1:03d}",
                title=(f"Endpoint mutante sin auth con simple request: "
                       f"{metodo} {ruta} ({rel}:{func.lineno})"),
                severity=AudSeverity.MEDIUM,
                verdict=AudVerdict.NEW,
                file=str(rel),
                line_start=int(getattr(func, "lineno", 0) or 0),
                code_snippet=f"{metodo} {ruta} → {func.name}()",
                explanation=(
                    "El handler muta estado (POST) y no exige "
                    "`Depends(...)`/token de auth. Su body es 'simple' "
                    "(Query/Path/Form/File o multipart, sin modelo Pydantic), "
                    "así que un navegador puede dispararlo cross-origin como "
                    "simple request SIN preflight CORS: una página ajena "
                    "ejecuta el handler (CSRF). Medido: un POST con "
                    "`Origin: evil` ejecutó el handler."
                ),
                recommendation=(
                    "Exigir el token de escritura (`depends=[Depends("
                    "exigir_token_de_escritura)]`, como /preflight/claves) o "
                    "validar `Origin`/`Sec-Fetch-Site`; y que el body tenga "
                    "un modelo Pydantic (application/json) cuando aplique."
                ),
                confidence=0.85,
                tags={"security", "csrf", "cwe-352", "auth"},
            )


class MutatingRoutesInventoryDetector(BaseDetector):
    """AUD-AUTH-003 — inventario INFO de rutas mutantes sin auth."""
    name = "mutating_routes_inventory"
    description = "Inventario (INFO) de rutas POST/PUT/PATCH/DELETE sin auth"

    def _detect(self) -> None:
        sin_auth = [(rel, func, metodo, ruta)
                    for rel, func, metodo, ruta, auth in _mutating_routes(self.index)
                    if not auth]
        if not sin_auth:
            return
        lista = "; ".join(
            f"{metodo} {ruta or func.name} ({rel}:{func.lineno})"
            for rel, func, metodo, ruta in sorted(
                sin_auth, key=lambda x: (str(x[0]), x[1].lineno)))
        primero_rel, primero_func, _, _ = sin_auth[0]
        self._add(
            finding_id="AUD-AUTH-003-001",
            title=f"{len(sin_auth)} rutas mutantes sin auth (inventario)",
            severity=AudSeverity.INFO,
            verdict=AudVerdict.NEW,
            file=str(primero_rel),
            line_start=int(getattr(primero_func, "lineno", 0) or 0),
            code_snippet=lista[:500],
            explanation=(
                f"Hay {len(sin_auth)} handlers mutantes "
                "(POST/PUT/PATCH/DELETE) sin `Depends(...)` de auth. No todos "
                "son explotables por CSRF (los que tienen body JSON no son "
                "'simple requests'), pero conviene tener el inventario "
                "explícito y decidir cuáles exigen token."
            ),
            recommendation=(
                "Revisar la lista y exigir token en los que mutan estado "
                "global o del usuario; dejar como excepción justificada los "
                "que sean idempotentes o de sólo lectura."
            ),
            confidence=0.9,
            tags={"security", "csrf", "auth", "inventory"},
        )


