#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — aud_detectors_regresiones.py
# Ruta: tools/auditor/aud_detectors_regresiones.py
#
# ✅ v0.6.9v: detectores nuevos que cubren las CLASES DE BUG encontradas en la
# sesión de refactorización + observabilidad (documentadas en
# docs/ISSUES_HISTORY.md). Antes de esto, el auditor NO las detectaba:
#
#   • SilentContractSwallowDetector  (ISSUE-018 / ISSUE-019)
#       El auditor tenía `silent_exceptions`, pero ese detector considera
#       `logger.debug(...)` como "logueado" → NO veía el caso real: un
#       `except Exception` que degrada a DEBUG y traga un ERROR DE CONTRATO
#       (firma/tipo equivocado), perdiendo datos o una feature en silencio.
#       Caso real: `_qm_record` pasaba float donde se esperaba QualityScore.
#
#   • FragileSourceGrepTestDetector  (ISSUE-020)
#       Tests que verifican la UBICACIÓN del código (grep sobre el fuente) en
#       vez del comportamiento: se rompen con cada refactor legítimo
#       (50/50) y dan falsos negativos.
#
#   • NondeterministicHardwareTestDetector  (ISSUE-022)
#       Tests que inyectan clases que leen el ENTORNO en vivo (MemoryGuard,
#       HardwareFit) → flaky según la RAM del equipo (aquí: 10 falsos fallos).
#
# Criterio de severidad: MEDIUM/LOW a propósito, para que DETECTEN sin
# distorsionar el SCORE AXIOMA (la fórmula penaliza -10 por cada HIGH de un
# detector no listado). Escalar a HIGH solo si el gate lo requiere.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from tools.auditor.aud_core import (
    AudSeverity,
    AudVerdict,
    BaseDetector,
)

# Rutas donde un fallo silencioso implica pérdida de DATOS/FEATURES.
_HIGH_VALUE_HINTS = (
    "memory", "gateway", "corpus", "dispatcher", "quality",
    "validator", "learning", "trace", "feedback", "session",
)

# Niveles de log que hacen VISIBLE el problema (no silencian).
_VISIBLE_LEVELS = ("warning", "error", "critical", "exception", "log_degraded")


def _own_nodes(handler: ast.ExceptHandler):
    """Nodos del handler SIN descender a handlers anidados.

    ✅ v0.6.9v: `ast.walk(handler)` bajaba a los `except` anidados dentro del
    cuerpo, así que un debug log de un handler INTERNO se le atribuía al
    externo → falso positivo (caso real: `semantic_base._init_lance`, cuyo
    `except` externo no loguea nada y la excepción esperada es "tabla no
    existe → crearla").
    """
    stack = list(handler.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ExceptHandler):
                continue  # no es responsabilidad de ESTE handler
            stack.append(child)


def _handler_calls(handler: ast.ExceptHandler) -> set:
    """Nombres de funciones llamadas por el handler (sin handlers anidados)."""
    calls = set()
    for sub in _own_nodes(handler):
        if isinstance(sub, ast.Call):
            fn = sub.func
            calls.add(fn.attr if isinstance(fn, ast.Attribute)
                      else (fn.id if isinstance(fn, ast.Name) else ""))
    return calls


def _handler_debug_calls(handler: ast.ExceptHandler) -> list:
    """Llamadas `.debug(...)` propias del handler (sin handlers anidados)."""
    return [n for n in _own_nodes(handler)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "debug"]


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in _own_nodes(handler))


_ERROR_WORDS = re.compile(
    r"error|fail|fall|excep|skipp|ignor|omit|invalid|inválid|no se pudo|"
    r"descart|degrad|timeout|exception|traceback",
    re.IGNORECASE,
)


def _logs_error_at_debug(handler: ast.ExceptHandler) -> bool:
    """True si el handler REPORTA un error por debug (no un mensaje informativo).

    ✅ v0.6.9v: evita otro falso positivo. Un handler de FALLBACK puede loguear
    a debug mensajes de ÉXITO ("tabla creada con …"), y esos no son un error
    tragado (caso real: `semantic_base._init_lance`, donde la excepción es
    flujo esperado: "la tabla no existe todavía → crearla").

    Señal de error: la llamada referencia la variable de excepción, o su
    mensaje contiene vocabulario de error.
    """
    for d in _handler_debug_calls(handler):
        if handler.name and any(
            isinstance(n, ast.Name) and n.id == handler.name for n in ast.walk(d)
        ):
            return True
        for arg in d.args:
            texto = ""
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                texto = arg.value
            elif isinstance(arg, ast.JoinedStr):
                texto = " ".join(
                    v.value for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            if texto and _ERROR_WORDS.search(texto):
                return True
    return False


class SilentContractSwallowDetector(BaseDetector):
    """`except` amplio que degrada a DEBUG en rutas de alto valor.

    Un error de CONTRATO (TypeError/AttributeError/NameError/ImportError) ahí
    significa que una feature dejó de funcionar en silencio. La corrección es
    `src.utils.degradation.log_degraded`, que sube a WARNING solo esa clase.
    """
    name = "silent_contract_swallow"
    description = ("except amplio que loguea solo a DEBUG en rutas de alto valor "
                   "(puede tragar errores de contrato → pérdida silenciosa)")

    def _detect(self) -> None:
        # Se reporta AGREGADO POR ARCHIVO y solo en rutas de alto valor: ahí el
        # fallo silencioso implica pérdida de datos/features. Las degradaciones
        # a DEBUG en rutas no críticas son diseño deliberado (no se reportan,
        # para no inundar el reporte: ~330 bloques en total).
        for rel, tree in self.index.asts.items():
            rel_s = str(rel)
            if not any(h in rel_s.lower() for h in _HIGH_VALUE_HINTS):
                continue
            lines: List[int] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if node.type is not None and not (
                    isinstance(node.type, ast.Name)
                    and node.type.id in ("Exception", "BaseException")
                ):
                    continue  # excepciones específicas: manejo deliberado
                # ✅ Solo lo que hace ESTE handler (sin handlers anidados).
                calls = _handler_calls(node)
                if _handler_reraises(node):
                    continue
                if calls & set(_VISIBLE_LEVELS):
                    continue  # ya es visible o ya usa log_degraded
                if "debug" not in calls:
                    continue  # sin log → lo cubre silent_exceptions
                if not _logs_error_at_debug(node):
                    continue  # debug informativo (fallback OK), no error tragado
                lines.append(node.lineno)
            if not lines:
                continue
            self._add(
                finding_id=f"AUD-SCS-{len(self.findings) + 1:03d}",
                title=f"{len(lines)} `except` amplio(s) degradado(s) solo a DEBUG en {rel_s}",
                severity=AudSeverity.MEDIUM,
                verdict=AudVerdict.NEW,
                file=rel_s,
                line_start=lines[0],
                line_end=lines[-1],
                code_snippet="except Exception: ... logger.debug(...)",
                explanation=(
                    f"{len(lines)} handler(s) en una ruta de alto valor tragan la "
                    "excepción logueando solo a DEBUG. Si el fallo es un error de "
                    "CONTRATO (firma/tipo equivocado) la feature deja de funcionar "
                    "sin ser visible en producción (caso real: ISSUE-018 — los "
                    "scores del validador nunca llegaban al QualityMonitor)."
                ),
                recommendation=(
                    "Usar `from src.utils.degradation import log_degraded` + "
                    "`log_degraded(logger, exc, \"<contexto>\")`: sube a WARNING "
                    "los errores de contrato y deja el resto en DEBUG. "
                    "Auditoría detallada: `python tools/audit_silent_excepts.py`."
                ),
                confidence=0.7,
                tags={"issue-019", "observabilidad"},
            )


class FragileSourceGrepTestDetector(BaseDetector):
    """Tests que verifican la ubicación del código (grep sobre fuente).

    Se rompen con cada refactor 50/50 legítimo → falso negativo. Preferir tests
    de comportamiento; si no se puede, buscar en la "familia" de módulos.
    """
    name = "fragile_source_grep_tests"
    description = ("tests que assertean texto dentro del fuente de un archivo "
                   "(frágiles ante refactors)")

    _SRC_HELPERS = ("_src(", "_src_family(", "read_text(")

    def _detect(self) -> None:
        tests_dir = self.index.root / "tests"
        if not tests_dir.is_dir():
            return
        for py in sorted(tests_dir.rglob("*.py")):
            if "__pycache__" in str(py):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            hits = self._source_assert_lines(tree)
            if not hits:
                continue
            rel = str(py.relative_to(self.index.root))
            self._add(
                finding_id=f"AUD-FSG-{len(self.findings) + 1:03d}",
                title=f"{len(hits)} assert(s) sobre el FUENTE en {rel}",
                severity=AudSeverity.LOW,
                verdict=AudVerdict.NEW,
                file=rel,
                line_start=hits[0],
                line_end=hits[-1],
                code_snippet='check("...", "texto" in src, ...)',
                explanation=(
                    "El test verifica que cierto texto exista en el fuente de un "
                    "archivo. Cuando un refactor 50/50 mueve el método a un mixin "
                    "nuevo, el test falla aunque el comportamiento sea correcto "
                    "(ISSUE-020: _screen_input → dispatcher_process_guard.py, "
                    "_build_prompt → coder_prompts.py)."
                ),
                recommendation=(
                    "Preferir un test de comportamiento. Si el grep es "
                    "imprescindible, usar un helper que busque en la familia de "
                    "módulos (p. ej. `_src_family('dispatcher_process.py', "
                    "'dispatcher_process_guard.py')`)."
                ),
                confidence=0.6,
                tags={"issue-020", "tests"},
            )

    @staticmethod
    def _is_python_source_var(node: ast.Assign) -> bool:
        """True si la variable contiene FUENTE PYTHON (no `.env` ni un informe).

        ✅ v0.6.9v: antes cualquier `read_text()` contaba como "grep de fuente",
        lo que daba FALSOS POSITIVOS con tests que validan contenido legítimo de
        otros archivos: `.env` (claves configuradas) o el reporte Markdown
        generado. Esos tests NO son frágiles ante refactors de código. Solo cuenta:
          - helpers de fuente explícitos (`_src(...)`, `_src_family(...)`), o
          - `read_text(...)` sobre una ruta literal terminada en `.py`, o
          - una variable cuyo nombre delata fuente (`src`, `*_src`, `code`).
        """
        call = node.value
        fname = ast.unparse(call.func)
        if "_src" in fname and "read_text" not in fname:
            return True
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.endswith(".py"):
                return True
        for tg in node.targets:
            if isinstance(tg, ast.Name):
                n = tg.id.lower()
                if n.endswith("_src") or n == "src" or n.endswith("_code") or n == "code":
                    return True
        return False

    def _source_assert_lines(self, tree: ast.Module) -> List[int]:
        """Líneas de comparaciones `"..." in <var_de_fuente_PYTHON>`."""
        src_vars: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                text = ast.unparse(call.func) if hasattr(ast, "unparse") else ""
                if not any(h.rstrip("(") in text for h in self._SRC_HELPERS):
                    continue
                if not self._is_python_source_var(node):
                    continue  # .env, informe MD, etc. → no es fragilidad de fuente
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        src_vars.add(t.id)
        if not src_vars:
            return []
        lines: List[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(comp, ast.Name) \
                        and comp.id in src_vars:
                    lines.append(node.lineno)
        return sorted(set(lines))


class NondeterministicHardwareTestDetector(BaseDetector):
    """Tests que inyectan clases que leen el ENTORNO en vivo (flaky).

    `MemoryGuard()` / `HardwareFit()` leen la RAM real del equipo: los tests de
    coordinación fallan o pasan según la memoria libre del momento. Inyectar un
    fake determinista; la política de RAM tiene sus propios tests.
    """
    name = "nondeterministic_hw_tests"
    description = ("tests que instancian MemoryGuard/HardwareFit reales "
                   "(dependen de la RAM del equipo → flaky)")

    _ENV_CLASSES = ("MemoryGuard", "HardwareFit")
    _FAKE_HINTS = ("Fake", "Stub", "Mock", "fake", "stub", "mock")

    def _detect(self) -> None:
        tests_dir = self.index.root / "tests"
        if not tests_dir.is_dir():
            return
        for py in sorted(tests_dir.rglob("*.py")):
            if "__pycache__" in str(py):
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(text)
            except SyntaxError:
                continue
            lines: List[int] = []
            for node in ast.walk(tree):
                # Solo cuenta si el guard se INYECTA en otro componente
                # (p. ej. `AgentCoordinator(guard=MemoryGuard())`). Los tests de
                # POLÍTICA de RAM usan el guard real a propósito y no se marcan.
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg not in ("guard", "hw", "hardware", "fit", "memory_guard"):
                        continue
                    v = kw.value
                    if not isinstance(v, ast.Call):
                        continue
                    fname = v.func.id if isinstance(v.func, ast.Name) else (
                        v.func.attr if isinstance(v.func, ast.Attribute) else "")
                    if fname not in self._ENV_CLASSES:
                        continue
                    # ✅ El NOMBRE de la clase alcanza para decidir: si se inyecta
                    # `MemoryGuard(...)` es el real; un doble se llama FakeGuard/
                    # StubX. (Antes se miraban las líneas vecinas buscando "Fake"
                    # y un `FakeSwap` cercano producía un FALSO NEGATIVO —
                    # detectado por el test sintético de este detector.)
                    lines.append(v.lineno)
            if not lines:
                continue
            rel = str(py.relative_to(self.index.root))
            self._add(
                finding_id=f"AUD-NDH-{len(self.findings) + 1:03d}",
                title=f"{len(lines)} uso(s) reales de clase dependiente del entorno en {rel}",
                severity=AudSeverity.LOW,
                verdict=AudVerdict.NEW,
                file=rel,
                line_start=lines[0],
                line_end=lines[-1],
                code_snippet="MemoryGuard() / HardwareFit() real en test",
                explanation=(
                    "La clase lee el estado del sistema en vivo (RAM/CPU). El test "
                    "pasa o falla según el equipo y el momento → flaky. Verificado "
                    "2026-09-10: 10 checks de coordinación en rojo solo por RAM "
                    "ajustada (ISSUE-022), sin ningún bug de código."
                ),
                recommendation=(
                    "Inyectar un doble determinista (p. ej. `FakeGuard` con "
                    "required_gb/can_load/can_coexist). La política real de RAM "
                    "se testea aparte con el guard real."
                ),
                confidence=0.6,
                tags={"issue-022", "tests", "flaky"},
            )


def _enclosing_function_name(tree: ast.Module, lineno: int) -> Optional[str]:
    """Nombre de la función/método que contiene `lineno` (helper de apoyo)."""
    best: Optional[Tuple[int, str]] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                span = end - node.lineno
                if best is None or span < best[0]:
                    best = (span, node.name)
    return best[1] if best else None


__all__ = [
    "ConfigTaskKeysDetector",
    "SilentContractSwallowDetector",
    "FragileSourceGrepTestDetector",
    "NondeterministicHardwareTestDetector",
]


class ConfigTaskKeysDetector(BaseDetector):
    """Claves de tarea en `config/models.yaml` que el código NO lee / no matchean.

    ✅ v0.6.9v (ISSUE-028): cubre una clase de bug real y silenciosa descubierta
    en esta sesión — el YAML escribía `preferred_model` / `fallback_model` pero
    el loader leía `primary` / `fallbacks`, así que **todos** los mapeos caían al
    default `CODE_MODEL` (`complex_reasoning` corría en el modelo de código).
    Y la clave `chat_general` no era un valor de `TaskType` (el canónico es
    `general_chat`), así que esa entrada nunca matcheaba.

    El detector deriva la verdad DEL CÓDIGO (no de una lista curada):
      - claves aceptadas por `TaskModelMapping.from_dict` (los `data.get("X")`);
      - valores canónicos de `TaskType` + etiquetas de tarea usadas en `src/`.
    """
    name = "config_task_keys"
    description = ("claves de task en models.yaml que el loader no lee o que no "
                   "matchean ningún task_type real (config silenciosamente inerte)")

    def _accepted_loader_keys(self) -> Set[str]:
        """Claves que el loader realmente lee (parseadas de su fuente)."""
        keys: Set[str] = set()
        for rel, tree in self.index.asts.items():
            if not str(rel).endswith("llm/config.py"):
                continue
            src = self.index.files[rel]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) \
                        and node.name == "from_dict":
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                                and sub.func.attr == "get" and sub.args:
                            a0 = sub.args[0]
                            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                                keys.add(a0.value)
        return keys

    def _known_task_labels(self) -> Set[str]:
        """TaskType canónicos + etiquetas de tarea usadas como literal en src/."""
        labels: Set[str] = set()
        try:
            import sys as _sys
            if str(self.index.root) not in _sys.path:
                _sys.path.insert(0, str(self.index.root))
            from src.domain.types import TaskType
            labels |= {t.value for t in TaskType}
        except Exception:
            pass
        for rel, src in self.index.files.items():
            if not str(rel).startswith("src/"):
                continue
            for m in re.finditer(r'"([a-z][a-z0-9_]{3,40})"', src):
                labels.add(m.group(1))
        return labels

    def _detect(self) -> None:
        cfg = self.index.root / "config" / "models.yaml"
        if not cfg.exists():
            return
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        mappings = data.get("task_mappings")
        if not isinstance(mappings, dict):
            return

        accepted = self._accepted_loader_keys()
        known_tasks = self._known_task_labels()

        # 1) claves escritas que el loader NO lee → config inerte.
        # Se AGRUPA por clave (una entrada por clave, no por tarea) para no
        # inflar el conteo: la causa raíz es la misma.
        inert_by_key: dict = {}
        for task, entry in mappings.items():
            if not isinstance(entry, dict):
                continue
            for k in entry:
                if accepted and k not in accepted:
                    inert_by_key.setdefault(k, []).append(task)
        for key, tasks in sorted(inert_by_key.items()):
            self._add(
                finding_id=f"AUD-CTK-{len(self.findings) + 1:03d}",
                title=f"models.yaml: clave inerte '{key}' en {len(tasks)} task(s)",
                severity=AudSeverity.LOW,
                verdict=AudVerdict.NEW,
                file="config/models.yaml",
                line_start=0,
                code_snippet=f"{key}: en {', '.join(tasks)}",
                explanation=(
                    f"'{key}' está escrita en el YAML pero NINGÚN consumidor la "
                    "lee (el loader `TaskModelMapping.from_dict` solo acepta "
                    f"{', '.join(sorted(accepted))}, y no hay otro lector de "
                    "`task_mappings.<task>." + key + "`). Editar ese valor NO "
                    "tiene efecto (misma clase de bug que ISSUE-028, donde "
                    "`preferred_model` se ignoraba y todo caía al CODE_MODEL)."
                ),
                recommendation=(
                    f"Wirear '{key}' a un consumidor real o quitarla del YAML "
                    "para no sugerir una configuración que no aplica."
                ),
                confidence=0.8,
                tags={"config", "issue-028", "dead-config"},
            )

        for task, entry in mappings.items():
            if not isinstance(entry, dict):
                continue
            # 2) nombre de tarea que no matchea ningún task_type real
            if task not in known_tasks:
                self._add(
                    finding_id=f"AUD-CTK-{len(self.findings) + 1:03d}",
                    title=f"models.yaml: task '{task}' no matchea ningún task_type",
                    severity=AudSeverity.LOW,
                    verdict=AudVerdict.NEW,
                    file="config/models.yaml",
                    line_start=0,
                    code_snippet=f"task_mappings.{task}",
                    explanation=(
                        f"'{task}' no es un valor de TaskType ni una etiqueta de "
                        "tarea usada en src/ → esta entrada nunca se aplica "
                        "(bug ISSUE-028: `chat_general` vs el canónico "
                        "`general_chat`)."
                    ),
                    recommendation=(
                        "Usar el valor canónico de TaskType (p. ej. "
                        "`general_chat`) o el nombre que realmente produce el "
                        "router."
                    ),
                    confidence=0.8,
                    tags={"config", "issue-028", "dead-config"},
                )
