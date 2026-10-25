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


# ════════════════════════════════════════════════════════════════════════
# A2 — DETECTORES DE LAS CLASES DE BUG REALES (seguridad)
# ════════════════════════════════════════════════════════════════════════
# ✅ v0.7.5 (A2): el auditor no veía los 4 bugs que el proyecto YA TUVO:
# contención de paths, `rmtree` fuera del base, `except ImportError` que no
# atrapa `OSError` y conexiones SQLite sin cierre (esta última en A3).
# También se cubren `shell=True`, `os.system`, `eval`/`exec` y `pickle`.
#
# Criterio de severidad:
#   · HIGH  → operación DESTRUCTIVA sobre un path derivado sin contención
#             (`rmtree`/`remove`/`unlink` de un path construido con un
#             parámetro). Es el bug que borró fuera del base.
#   · MEDIUM → path derivado sin contención usado para leer/escribir, imports
#             nativos que sólo atrapan `ImportError`, `pickle.load(s)` y
#             `shell=True` en subprocess.
#   · HIGH  → `os.system`/`eval`/`exec` con entrada NO constante.
# ════════════════════════════════════════════════════════════════════════

_PATH_BASE_HINT = re.compile(
    r"(base|root|dir|folder|home|workspace|tmp|temp|cache|staging|backup|"
    r"data|path)$", re.I)
# Borrado real. `rename`/`replace`/`move` quedan FUERA a propósito: un
# rename atómico en el mismo directorio es legítimo y marcaba FPs (medido en
# `interfaces/cli/backup.py::_crear_tar`, que usa `temporal.replace(destino)`).
_DESTRUCTIVE_CALLS = {"rmtree", "remove", "unlink", "rmdir", "removedirs"}
_WRITE_CALLS = {"write_text", "write_bytes", "touch", "mkdir", "makedirs"}
_READ_CALLS = {"read_text", "read_bytes", "open", "iterdir", "glob", "rglob",
               "listdir", "scandir", "walk", "copy", "copy2", "copyfile",
               "copytree", "tarfile", "text", "read"}
# Atributos que ya son un NOMBRE de archivo (sin separadores de path) o que
# validan contención → no propagan taint.
_SANITIZING_ATTRS = {"name", "stem", "suffix", "parts", "parent"}
_SANITIZING_CALLS = {"hexdigest", "digest", "relative_to", "is_relative_to",
                     "uuid4", "uuid1", "token_hex", "fullmatch", "match",
                     "int", "quote", "slugify"}

# Módulos con parte NATIVA (C/Wheel): un fallo de carga de la librería del
# sistema (PortAudio, libGL, CUDA…) lanza OSError, no ImportError.
_NATIVE_OPTIONAL_MODULES = frozenset({
    # Módulos con extensión C/binding de librería del sistema: si el paquete
    # está instalado pero falta la .so (PortAudio, libGL, CUDA, libctranslate2,
    # liblance), el import lanza OSError. NO se incluyen pure-python ni wheels
    # con fallback (numpy/PIL/pyttsx3), que en la práctica dan ImportError.
    "sounddevice", "cv2", "torch", "lancedb", "lance", "pyaudio",
    "soundfile", "mediapipe", "faster_whisper", "onnxruntime", "pyarrow",
    "webrtcvad", "pvporcupine", "openwakeword",
})


def _attr_name(node: Optional[ast.AST]) -> Optional[str]:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _own_ast_nodes(node: ast.AST):
    """Nodos dentro de `node` SIN bajar a funciones/clases/lambdas anidadas."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _is_path_ctor(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _attr_name(node.func) in ("Path", "PurePath", "PosixPath",
                                    "WindowsPath")


def _looks_like_base(node: ast.AST) -> bool:
    """¿La expresión parece una RAÍZ (base dir) y no el nombre de usuario?"""
    if _is_path_ctor(node):
        return True
    name = _attr_name(node)
    return bool(name and _PATH_BASE_HINT.search(name))


def _is_join_call(node: ast.AST) -> bool:
    """Sólo `os.path.join(...)` (no `str.join`)."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "join"):
        return False
    return isinstance(f.value, ast.Attribute) and f.value.attr == "path"


def _expr_names(node: ast.AST) -> Set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _is_sanitizing_call(node: ast.Call) -> bool:
    name = _attr_name(node.func) or ""
    if name in _SANITIZING_CALLS:
        return True
    return name.startswith(("_safe", "_validate", "sanitize"))


def _dangerous_refs(node: ast.AST, tainted: Set[str]) -> Set[str]:
    """Nombres tainted usados como path, ignorando sanitizadores obvios.

    `f"{path.name}_{ts}.bak"` NO propaga `path` (`.name` es un basename);
    `hashlib.md5(x).hexdigest()` tampoco propaga `x`.
    """
    if isinstance(node, ast.Attribute) and node.attr in _SANITIZING_ATTRS:
        return set()
    if isinstance(node, ast.Call) and _is_sanitizing_call(node):
        return set()
    if isinstance(node, ast.Name):
        return {node.id} & tainted
    out: Set[str] = set()
    for child in ast.iter_child_nodes(node):
        out |= _dangerous_refs(child, tainted)
    return out


def _function_tainted_names(func: ast.AST) -> Set[str]:
    """Parámetros de la función + locales asignados a partir de ellos.

    Propagación simple (hasta punto fijo): `sid = session_id or "default"`
    sigue siendo tainted, y `ws = self._base / sid` también.
    """
    tainted: Set[str] = set()
    args = getattr(func, "args", None)
    if args is not None:
        for a in (list(getattr(args, "posonlyargs", []))
                  + list(args.args) + list(args.kwonlyargs)
                  + [args.vararg, args.kwarg]):
            if a is not None:
                tainted.add(a.arg)
    changed = True
    while changed:
        changed = False
        for node in _own_ast_nodes(func):
            targets: List[ast.AST] = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            if value is None or not (_dangerous_refs(value, tainted)):
                continue
            for t in targets:
                if isinstance(t, ast.Name) and t.id not in tainted:
                    tainted.add(t.id)
                    changed = True
    return tainted


def _has_containment_validation(func: ast.AST) -> bool:
    """¿La función valida contención ANTES de usar el path derivado?

    Acepta los patrones reales del proyecto: `resolve()/realpath()` +
    `startswith(base)`, `is_relative_to(...)`, `fullmatch` de una regex
    (allowlist de session_id) o `re.match("^[A-Za-z0-9_-]+$", ...)`.
    """
    has_resolve = has_startswith = has_relative = has_fullmatch = has_regex = False
    for node in _own_ast_nodes(func):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_name(node.func)
        if name == "is_relative_to":
            has_relative = True
        elif name in ("realpath", "resolve"):
            has_resolve = True
        elif name == "startswith":
            has_startswith = True
        elif name == "fullmatch":
            has_fullmatch = True
        elif name == "match" and node.args:
            literales = [a.value for a in node.args
                         if isinstance(a, ast.Constant)
                         and isinstance(a.value, str)]
            has_regex = any(("^" in s or "$" in s or "A-Za-z" in s)
                            for s in literales)
    return has_relative or has_fullmatch or has_regex or \
        (has_resolve and has_startswith)


def _iter_path_derivations(func: ast.AST):
    """(nodo, operando_tainted) de cada path derivado de un parámetro.

    Detecta `Path(base) / param`, `base / param`, `os.path.join(base, param)`
    y `Path(base, param)`. No adivina concatenaciones por f-string.
    """
    tainted = _function_tainted_names(func)
    if not tainted:
        return []
    out: List[Tuple[ast.AST, ast.AST]] = []
    for node in _own_ast_nodes(func):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _looks_like_base(node.left) and _dangerous_refs(node.right, tainted):
                out.append((node, node.right))
        elif _is_join_call(node):
            if len(node.args) >= 2 and _looks_like_base(node.args[0]):
                for arg in node.args[1:]:
                    if _dangerous_refs(arg, tainted):
                        out.append((node, arg))
                        break
        elif _is_path_ctor(node):
            if len(node.args) >= 2 and _looks_like_base(node.args[0]):
                for arg in node.args[1:]:
                    if _dangerous_refs(arg, tainted):
                        out.append((node, arg))
                        break
    return out


def _derivation_vars(func: ast.AST, deriv: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in _own_ast_nodes(func):
        if isinstance(node, ast.Assign) and node.value is deriv:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and node.value is deriv \
                and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _mentions(expr: ast.AST, deriv: ast.AST, names: Set[str]) -> bool:
    if expr is deriv:
        return True
    return any(isinstance(n, ast.Name) and n.id in names
               for n in ast.walk(expr))


def _sink_categories(func: ast.AST, deriv: ast.AST,
                     names: Set[str]) -> Set[str]:
    """{'destructive','write'} según los sinks que usan el path derivado."""
    cats: Set[str] = set()
    for node in _own_ast_nodes(func):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_name(node.func)
        if name in _DESTRUCTIVE_CALLS:
            category = "destructive"
        elif name == "open" and len(node.args) >= 2 and \
                isinstance(node.args[1], ast.Constant) and \
                str(node.args[1].value) in ("w", "a", "x", "wb", "ab", "xb"):
            category = "write"
        elif name in _WRITE_CALLS:
            category = "write"
        elif name in _READ_CALLS:
            category = "read"
        else:
            continue
        if _mentions(node.func, deriv, names) or any(
                _mentions(arg, deriv, names) for arg in node.args):
            cats.add(category)
    return cats


class PathContainmentDetector(BaseDetector):
    """`Path(base) / param` / `os.path.join(base, param)` sin contención.

    ✅ A2.1: el bug real (snapshot c4cd41f^) era `self._base / session_id`
    sin validar el formato ni la contención → `session_id="/etc"` leía
    `/etc/passwd` y `cleanup("../evil")` borraba fuera del base. Este detector
    reporta los usos NO destructivos (leer/escribir) como MEDIUM; los
    destructivos los escala `unsafe_path_delete` a HIGH.
    """
    name = "path_containment"
    description = "Path derivado de un parámetro sin validar contención (CWE-22)"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                derivaciones = _iter_path_derivations(func)
                if not derivaciones:
                    continue
                if _has_containment_validation(func):
                    continue
                sospechosas: List[Tuple[ast.AST, ast.AST]] = []
                for deriv, operando in derivaciones:
                    names = _derivation_vars(func, deriv)
                    cats = _sink_categories(func, deriv, names)
                    if "destructive" in cats:
                        continue  # lo reporta unsafe_path_delete (HIGH)
                    # Si el path sólo se devuelve/guarda, no hay uso inseguro
                    # demostrable en ESTA función: no marcar (evita FPs de
                    # utilidades como Paths.resolve / _propose).
                    if not ({"read", "write"} & cats):
                        continue
                    sospechosas.append((deriv, operando))
                if not sospechosas:
                    continue
                linea = min(int(getattr(d, "lineno", 0) or 0)
                            for d, _ in sospechosas)
                operandos = ", ".join(sorted({
                    ast.unparse(o)[:60] for _, o in sospechosas
                    if hasattr(ast, "unparse")}))
                self._add(
                    finding_id=f"AUD-PTH-{len(self.findings) + 1:03d}",
                    title=(f"Path derivado sin contención en "
                           f"{rel}::{func.name}() ({len(sospechosas)})"),
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=linea,
                    code_snippet=f"{func.name}(): {operandos}"[:300],
                    explanation=(
                        f"{func.name}() construye un path con "
                        f"`{operandos}` (parámetro de la función) sin "
                        "`resolve().is_relative_to(...)`, `realpath` + "
                        "`startswith` ni una allowlist por regex. Un valor "
                        "como `../..` o `/etc` sale del directorio base "
                        "(CWE-22 path traversal)."
                    ),
                    recommendation=(
                        "Validar el parámetro ANTES de construir el path: "
                        "regex de allowlist (`^[A-Za-z0-9_-]+$`), y después "
                        "`p = (base / param).resolve(); "
                        "p.is_relative_to(base.resolve())`."
                    ),
                    confidence=0.8,
                    tags={"security", "path-traversal", "cwe-22"},
                )


class UnsafePathDeleteDetector(BaseDetector):
    """`shutil.rmtree`/`os.remove`/`unlink` sobre un path derivado sin contención.

    ✅ A2.2: este es el bug que borró `../evil` fuera del base.
    """
    name = "unsafe_path_delete"
    description = "Borrado/movida sobre un path derivado sin contención (CWE-22)"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                derivaciones = _iter_path_derivations(func)
                if not derivaciones:
                    continue
                if _has_containment_validation(func):
                    continue
                peligrosas = []
                for deriv, operando in derivaciones:
                    names = _derivation_vars(func, deriv)
                    if "destructive" in _sink_categories(func, deriv, names):
                        peligrosas.append((deriv, operando))
                if not peligrosas:
                    continue
                linea = min(int(getattr(d, "lineno", 0) or 0)
                            for d, _ in peligrosas)
                operandos = ", ".join(sorted({
                    ast.unparse(o)[:60] for _, o in peligrosas
                    if hasattr(ast, "unparse")}))
                self._add(
                    finding_id=f"AUD-DEL-{len(self.findings) + 1:03d}",
                    title=(f"Borrado sobre path derivado sin contención en "
                           f"{rel}::{func.name}()"),
                    severity=AudSeverity.HIGH,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=linea,
                    code_snippet=f"{func.name}(): {operandos}"[:300],
                    explanation=(
                        f"{func.name}() pasa un path construido con "
                        f"`{operandos}` a rmtree/remove/unlink/rename sin "
                        "verificar que siga dentro del base. Con "
                        "`session_id='../evil'` el borrado/movida ocurre "
                        "FUERA del directorio de trabajo (CWE-22)."
                    ),
                    recommendation=(
                        "Validar el parámetro (allowlist) y comprobar "
                        "`p.resolve().is_relative_to(base.resolve())` ANTES "
                        "de la operación destructiva."
                    ),
                    confidence=0.85,
                    tags={"security", "path-traversal", "destructive", "cwe-22"},
                )


def _flatten_handler_types(node: Optional[ast.AST]) -> Set[str]:
    if node is None:
        return {"bare"}
    if isinstance(node, ast.Tuple):
        out: Set[str] = set()
        for elt in node.elts:
            out |= _flatten_handler_types(elt)
        return out
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Name):
        return {node.id}
    return {"?"}


def _imports_native_in(stmts: List[ast.stmt]) -> Set[str]:
    roots: Set[str] = set()
    stack = list(stmts)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module:
                roots.add(stmt.module.split(".")[0])
        else:
            stack.extend(ast.iter_child_nodes(stmt))
    return roots


class NativeImportOSErrorDetector(BaseDetector):
    """`except ImportError` alrededor de imports NATIVOS que pueden dar OSError.

    ✅ A2.4: `import sounddevice` falla con OSError si falta PortAudio aunque
    el paquete esté instalado; el mismo patrón aplica a cv2 (libGL), torch
    (CUDA) y lancedb. Capturar sólo ImportError deja el módulo entero sin
    importar (el bug real medido en tts_engine/vad_filter antes del fix).
    """
    name = "native_import_oserror"
    description = "Import nativo (sounddevice/cv2/torch/…) que sólo atrapa ImportError"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try) or not node.handlers:
                    continue
                roots = _imports_native_in(node.body)
                nativos = sorted(roots & _NATIVE_OPTIONAL_MODULES)
                if not nativos:
                    continue
                capturados: Set[str] = set()
                for handler in node.handlers:
                    capturados |= _flatten_handler_types(handler.type)
                if not ({"ImportError", "ModuleNotFoundError"} & capturados):
                    continue
                if {"OSError", "Exception", "BaseException", "bare"} & capturados:
                    continue
                self._add(
                    finding_id=f"AUD-IEO-{len(self.findings) + 1:03d}",
                    title=(f"Import nativo sólo atrapa ImportError: "
                           f"{', '.join(nativos)} en {rel}"),
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=node.lineno,
                    code_snippet=(f"except ImportError → "
                                  f"import {', '.join(nativos)}")[:300],
                    explanation=(
                        f"El try importa {', '.join(nativos)} (módulo con parte "
                        "nativa) y sólo captura ImportError. Si el paquete "
                        "está instalado pero falta la librería del sistema "
                        "(PortAudio, libGL, CUDA…), el import lanza OSError y "
                        "el módulo revienta al importarse, tumbando también "
                        "las features que no dependen de esa librería."
                    ),
                    recommendation=(
                        "Capturar `except (ImportError, OSError) as exc:` y "
                        "degradar con log visible (el patrón ya usado en "
                        "src/multimodal/tts_engine.py y vad_filter.py)."
                    ),
                    confidence=0.85,
                    tags={"imports", "oserror", "feature-detection", "robustness"},
                )


def _is_obvious_constant(expr: ast.AST) -> bool:
    """True si la expresión es un literal (o contenedor de literales)."""
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_obvious_constant(e) for e in expr.elts)
    if isinstance(expr, ast.Dict):
        return all(_is_obvious_constant(e) for e in expr.keys
                   if e is not None) and \
            all(_is_obvious_constant(e) for e in expr.values)
    if isinstance(expr, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in expr.values)
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
        return _is_obvious_constant(expr.operand)
    return False


class UnsafeExecDetector(BaseDetector):
    """`os.system`, `eval`/`exec`, `pickle.load(s)` y `subprocess(shell=True)`.

    ✅ A2.5: entrada NO obvia = no es un literal. `shell=True` se marca
    siempre (el riesgo es el modo de ejecución, no el argv).
    """
    name = "unsafe_exec"
    description = "Ejecución/deserialización peligrosa (shell=True, eval, exec, pickle)"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fname = _attr_name(node.func)
                linea = int(getattr(node, "lineno", 0) or 0)
                args = list(node.args)

                shell_kw = [k for k in node.keywords if k.arg == "shell"]
                if shell_kw and isinstance(shell_kw[0].value, ast.Constant) \
                        and shell_kw[0].value.value is True:
                    self._add_unsafe(rel, linea, node,
                                     "subprocess con shell=True",
                                     AudSeverity.MEDIUM, "shell",
                                     "El comando se interpreta por el shell: "
                                     "metacaracteres/quoting inyectables si "
                                     "algún argumento viene de entrada.",
                                     "Usar argv (lista) con shell=False.")
                    continue

                if isinstance(node.func, ast.Attribute) \
                        and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "os" \
                        and fname in ("system", "popen"):
                    if args and not _is_obvious_constant(args[0]):
                        self._add_unsafe(rel, linea, node, f"os.{fname}() con entrada dinámica",
                                         AudSeverity.HIGH, "os-system",
                                         "os.system/os.popen ejecutan una shell; "
                                         "con entrada dinámica es inyección de comandos.",
                                         "Usar subprocess.run(argv, shell=False).")
                    continue

                if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                    if args and not _is_obvious_constant(args[0]):
                        self._add_unsafe(rel, linea, node, f"{node.func.id}() con entrada dinámica",
                                         AudSeverity.HIGH, "eval-exec",
                                         f"{node.func.id}() sobre datos no confiables "
                                         "permite ejecución arbitraria de código.",
                                         "Usar ast.literal_eval o un parser propio.")
                    continue

                if fname in ("load", "loads") and isinstance(node.func, ast.Attribute):
                    base = node.func.value
                    base_name = _attr_name(base)
                    if base_name == "pickle" and args \
                            and not _is_obvious_constant(args[0]):
                        self._add_unsafe(rel, linea, node,
                                         f"pickle.{fname}() sobre datos dinámicos",
                                         AudSeverity.MEDIUM, "pickle",
                                         "Deserializar pickle no confiable permite "
                                         "ejecución de código (CWE-502).",
                                         "Usar json/msgpack o firmar el payload.")

    def _add_unsafe(self, rel, linea, node, titulo, severidad, slug,
                    explicacion, recomendacion) -> None:
        self._add(
            finding_id=f"AUD-EXEC-{len(self.findings) + 1:03d}",
            title=f"{titulo} en {rel}:{linea}",
            severity=severidad,
            verdict=AudVerdict.NEW,
            file=str(rel),
            line_start=linea,
            code_snippet=ast.unparse(node)[:200] if hasattr(ast, "unparse") else "",
            explanation=explicacion,
            recommendation=recomendacion,
            confidence=0.8,
            tags={"security", slug},
        )
