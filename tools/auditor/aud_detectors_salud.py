#!/usr/bin/env python3
"""
tools/auditor/detectors_salud.py — detectores de salud (sync-blocking, índices, FIFO, sintaxis, silencios, entorno)

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
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)
from tools.auditor.aud_core import (
    BaseDetector, CodeIndex, DetectorResult, AudFinding, AudSeverity, AudVerdict,
    _resolve_imports_for, _resolve_top_level_imports_for, _to_snake_case,
)


# ============================================================================
# DETECTOR: Sync blocking in async (AST puro)
# ============================================================================

class SyncBlockingInAsyncDetector(BaseDetector):
    """time.sleep/requests.get en métodos async."""
    name = "sync_blocking_in_async"
    description = "Llamadas bloqueantes síncronas en métodos async"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue

                for call in self._find_blocking_calls(node):
                    if self._is_wrapped_in_to_thread(call, node):
                        continue

                    self._add(
                        finding_id=f"AUD-SB-{len(self.findings)+1:03d}",
                        title=f"Bloqueo síncrono en async: {node.name}() — {rel}",
                        severity=AudSeverity.HIGH,
                        verdict=AudVerdict.NEW,
                        file=str(rel),
                        line_start=call.lineno,
                        code_snippet=(
                            f"Llamada bloqueante en línea {call.lineno}: "
                            f"{ast.unparse(call) if hasattr(ast, 'unparse') else ''}"
                        ),
                        explanation=(
                            f"El método async {node.name}() contiene una llamada bloqueante "
                            "que bloquea el event loop. Congela otras corrutinas y el pipeline completo."
                        ),
                        recommendation=(
                            "Reemplazar por: asyncio.sleep() en vez de time.sleep(); "
                            "httpx.AsyncClient en vez de requests; o envolver en "
                            "asyncio.to_thread() si no hay alternativa async."
                        ),
                        confidence=0.95,
                        tags={'async', 'blocking-io', 'event-loop'},
                    )

    def _find_blocking_calls(self, func_node: ast.AsyncFunctionDef) -> List[ast.Call]:
        blocking_calls = []
        for child in ast.walk(func_node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute):
                if func.attr == 'sleep' and isinstance(func.value, ast.Name) and func.value.id == 'time':
                    blocking_calls.append(child)
                elif func.attr in ('get', 'post', 'put', 'delete', 'patch'):
                    if isinstance(func.value, ast.Name) and func.value.id == 'requests':
                        blocking_calls.append(child)
        return blocking_calls

    def _is_wrapped_in_to_thread(self, call: ast.Call, func_node: ast.AsyncFunctionDef) -> bool:
        parent = self._find_parent_node(func_node, call)
        if parent is None:
            return False
        if isinstance(parent, ast.Call):
            f = parent.func
            if isinstance(f, ast.Attribute):
                if f.attr == 'to_thread' and isinstance(f.value, ast.Name) and f.value.id == 'asyncio':
                    return True
            if isinstance(f, ast.Name) and f.id == 'to_thread':
                return True
        return False

    def _find_parent_node(self, root: ast.AST, target: ast.AST) -> Optional[ast.AST]:
        stack = [(root, None)]
        while stack:
            node, parent = stack.pop()
            if node is target:
                return parent
            for child in ast.iter_child_nodes(node):
                stack.append((child, node))
        return None


# ============================================================================
# DETECTOR: Missing compound index
# ============================================================================

class MissingCompoundIndexDetector(BaseDetector):
    """Queries SQLite multi-columna sin índice compuesto."""
    name = "missing_compound_index"
    description = "Falta índice compuesto en queries con múltiples condiciones"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            src = self.index.files[rel]
            if 'CREATE TABLE' not in src.upper():
                continue
            create_tables = re.findall(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(([^;]+)\)',
                src, re.IGNORECASE | re.DOTALL
            )
            create_indexes = re.findall(
                r'CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)\s*\(([^)]+)\)',
                src, re.IGNORECASE
            )
            for table_name, _ in create_tables:
                multi_queries = re.findall(
                    r'WHERE\s+(\w+)\s*=\s*(?:\?|\'[^\']*\'|\"[^\"]*\"|\d+)\s+AND\s+(\w+)\s*=\s*(?:\?|\'[^\']*\'|\"[^\"]*\"|\d+)',
                    src, re.IGNORECASE
                )
                for col1, col2 in multi_queries:
                    has_compound = any(
                        idx_table == table_name
                        and col1 in idx_cols and col2 in idx_cols
                        for _, idx_table, idx_cols in create_indexes
                    )
                    if not has_compound:
                        line_num = self._find_line(src, f'WHERE {col1}')
                        self._add(
                            finding_id=f"AUD-CI-{len(self.findings)+1:03d}",
                            title=f"Sin índice compuesto en {table_name}({col1}, {col2})",
                            severity=AudSeverity.LOW,
                            verdict=AudVerdict.NEW,
                            file=str(rel),
                            line_start=line_num,
                            code_snippet=f"WHERE {col1} = ? AND {col2} = ?",
                            explanation=(
                                f"La tabla {table_name} se consulta con WHERE {col1}=? AND "
                                f"{col2}=?. Solo existen índices simples. SQLite hará full "
                                "scan o usará un índice y filtrará en memoria."
                            ),
                            recommendation=(
                                f"CREATE INDEX idx_{table_name}_{col1}_{col2} "
                                f"ON {table_name}({col1}, {col2});"
                            ),
                            confidence=0.7,
                            tags={'sqlite', 'index', 'performance'},
                        )

    def _find_line(self, src: str, pattern: str) -> int:
        for i, line in enumerate(src.splitlines(), 1):
            if pattern in line:
                return i
        return 0


# ============================================================================
# DETECTOR: FIFO cache sin LRU (MEJORADO - busca uso directo)
# ============================================================================

class FifoCacheDetector(BaseDetector):
    """
    Detecta cachés que usan dict común y evicción con next(iter(...))
    sin reordenar (comportamiento FIFO, no LRU). No depende de cómo se inicializa
    el atributo; busca el patrón de uso en el archivo.
    """
    name = "fifo_cache"
    description = "Cache FIFO sin LRU (next(iter(...)) sin move_to_end)"

    CACHE_ATTRS = ('_cache', '_response_cache', '_decision_cache', '_route_cache',
                   '_result_cache', '_metrics_cache')

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            src = self.index.files[rel]
            # Buscar en el archivo completo patrones de FIFO para cada atributo
            for attr in self.CACHE_ATTRS:
                # Buscar next(iter(self.{attr}))
                next_pattern = rf'next\s*\(\s*iter\s*\(\s*self\.{attr}\s*\)\s*\)'
                if not re.search(next_pattern, src, re.DOTALL):
                    continue
                # Buscar del self.{attr}[...] o self.{attr}.pop(...)
                del_pattern = rf'del\s+self\.{attr}\s*\['
                pop_pattern = rf'self\.{attr}\s*\.pop\s*\('
                if not (re.search(del_pattern, src) or re.search(pop_pattern, src)):
                    continue

                # Encontrar línea donde aparece next(iter(...))
                lines = src.splitlines()
                for idx, line in enumerate(lines, start=1):
                    if re.search(next_pattern, line):
                        # Obtener nombre de clase donde está el atributo
                        class_name = self._find_class_for_line(tree, idx)
                        if class_name:
                            self._add(
                                finding_id=f"AUD-FIFO-{len(self.findings)+1:03d}",
                                title=f"Cache FIFO sin LRU en {class_name} (attr {attr})",
                                severity=AudSeverity.MEDIUM,
                                verdict=AudVerdict.NEW,
                                file=str(rel),
                                line_start=idx,
                                code_snippet=line.strip(),
                                explanation=(
                                    f"La clase {class_name} usa un dict común como caché ({attr}) y evicción "
                                    "con `next(iter(...))` sin reordenar las claves. "
                                    "Esto produce un comportamiento FIFO, no LRU: las entradas "
                                    "frecuentemente accedidas pueden ser eliminadas."
                                ),
                                recommendation=(
                                    "Usar OrderedDict con move_to_end en el hit path y "
                                    "popitem(last=False) cuando se exceda el límite."
                                ),
                                confidence=0.9,
                                tags={'cache', 'lru', 'fifo'},
                            )
                            break

    def _find_class_for_line(self, tree: ast.AST, line: int) -> Optional[str]:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.lineno <= line <= (node.end_lineno or node.lineno):
                    return node.name
        return None


# ============================================================================
# DETECTOR: Sintaxis + imports (auditoría completa de salud base)
# ============================================================================
# ✅ v8.0 (2026-08-25): Unifica tools/audit_axioma.py — verifica que TODOS
# los archivos .py del proyecto parseen y que cada módulo de src/+config/
# importe sin errores (ImportError, dependencias rotas, imports circulares
# reales). Además escanea la raíz y tools/ para errores de sintaxis.
# ============================================================================

_ROOT_PY_DIRS = ('src', 'config', 'tools', 'tests')
_ROOT_PY_EXCLUDE = ('__pycache__', 'venv', 'deepseek-harness', '.git', 'data',
                    'logs', 'cache', 'inforchat', 'node_modules', 'benchmarks',
                    '.optimizer_backups')


class SyntaxImportDetector(BaseDetector):
    """Errores de sintaxis y de import en todo el proyecto."""
    name = "syntax_import"
    description = "Errores de sintaxis y de import (salud base del proyecto)"

    def _project_py_files(self) -> List[Path]:
        files: List[Path] = []
        for py in self.index.root.glob("*.py"):
            if py.name in ('conftest.py', 'axioma_report.py'):
                continue
            files.append(py)
        for d in _ROOT_PY_DIRS:
            base = self.index.root / d
            if not base.exists():
                continue
            for py in sorted(base.rglob("*.py")):
                if any(x in py.parts for x in _ROOT_PY_EXCLUDE):
                    continue
                if py.name in ('conftest.py', 'axioma_report.py', 'suite_utils.py'):
                    continue
                files.append(py)
        return files

    def _detect(self) -> None:
        files = self._project_py_files()

        # ── 1. Sintaxis ──────────────────────────────────────────
        syntax_errors: List[Tuple[Path, int, str]] = []
        for py in files:
            try:
                ast.parse(py.read_text(encoding='utf-8', errors='ignore'),
                          filename=str(py))
            except SyntaxError as e:
                syntax_errors.append((py, e.lineno or 0, e.msg))
            except UnicodeDecodeError as e:
                syntax_errors.append((py, 0, f"UnicodeDecodeError: {e}"))

        for py, lineno, msg in syntax_errors[:50]:
            rel = py.relative_to(self.index.root)
            self._add(
                finding_id=f"AUD-SYN-{len(self.findings)+1:03d}",
                title=f"Error de sintaxis en {rel}",
                severity=AudSeverity.HIGH,
                verdict=AudVerdict.NEW,
                file=str(rel),
                line_start=lineno,
                code_snippet=str(py),
                explanation=f"El archivo no parsea: {msg}",
                recommendation="Corregir la sintaxis para que el módulo pueda importarse.",
                confidence=1.0,
                tags={'syntax', 'build'},
            )
        if len(syntax_errors) > 50:
            self._add(
                finding_id="AUD-SYN-OVERFLOW",
                title=f"{len(syntax_errors)} archivos con errores de sintaxis",
                severity=AudSeverity.HIGH, verdict=AudVerdict.NEW,
                file=str(self.index.root), line_start=0,
                explanation="Se muestran los primeros 50; hay más.",
                recommendation="Corregir todos los archivos que no parsean.",
                confidence=1.0, tags={'syntax', 'build'},
            )

        # ── 2. Imports de módulos src/ y config/ ────────────────
        import sys as _sys
        if str(self.index.root) not in _sys.path:
            _sys.path.insert(0, str(self.index.root))

        import importlib
        import_errors: List[Tuple[str, str]] = []
        for d in ('src', 'config'):
            base = self.index.root / d
            if not base.exists():
                continue
            for py in sorted(base.rglob("*.py")):
                if any(x in py.parts for x in _ROOT_PY_EXCLUDE):
                    continue
                rel = py.relative_to(self.index.root)
                parts = list(rel.with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                mod = ".".join(parts)
                try:
                    importlib.import_module(mod)
                except Exception as e:  # noqa: BLE001
                    import_errors.append((mod, f"{type(e).__name__}: {str(e)[:140]}"))

        for mod, err in import_errors[:50]:
            self._add(
                finding_id=f"AUD-IMP-{len(self.findings)+1:03d}",
                title=f"Módulo no importable: {mod}",
                severity=AudSeverity.CRITICAL,
                verdict=AudVerdict.NEW,
                file=mod.replace(".", "/") + ".py",
                line_start=0,
                code_snippet=err,
                explanation="El módulo falla al importarse — dependencias rotas o import circular real.",
                recommendation="Corregir el import o la dependencia faltante.",
                confidence=1.0,
                tags={'import', 'critical'},
            )
        if len(import_errors) > 50:
            self._add(
                finding_id="AUD-IMP-OVERFLOW",
                title=f"{len(import_errors)} módulos no importables",
                severity=AudSeverity.CRITICAL, verdict=AudVerdict.NEW,
                file=str(self.index.root), line_start=0,
                explanation="Se muestran los primeros 50.",
                recommendation="Corregir todos los imports rotos.",
                confidence=1.0, tags={'import', 'critical'},
            )


# ============================================================================
# DETECTOR: Excepciones silenciosas
# ============================================================================
# ✅ v8.0 (2026-08-25): Busca `except: pass`, `except Exception: pass` y
# handlers que tragan errores sin loguear ni usar la excepción.
# ============================================================================


class SilentExceptionsDetector(BaseDetector):
    """Handlers de excepción que ocultan errores (interrupciones silenciosas).

    ✅ v8.2 (2026-09-29, ISSUE-106) — criterio EXPLÍCITO y cobertura ampliada.
    Se marca un handler cuando su cuerpo **no usa la excepción** y **no registra
    nada**. Qué se considera "no registra": ni `logger.*`, ni los helpers `_log_*`,
    ni **`log_degraded(...)`** (que es el estándar del proyecto: sube a WARNING los
    errores de contrato, ver `src/utils/degradation.py`).

    Tipos VIGILADOS (donde el silencio es sospechoso y da LOW): `Exception`,
    `BaseException`, `OSError`, `ValueError`, `KeyError`, `TypeError`,
    `RuntimeError`, `json.JSONDecodeError`, **`PermissionError`** (nuevo: un
    permiso denegado tragado suele ser pérdida de datos) y los **tuples** que
    contengan cualquiera de ellos (antes un `except (ValueError, TypeError):` se
    clasificaba como `"tuple"` y se descartaba: 15 handlers ciegos medidos).

    Tipos NO vigilados a propósito (silencio normal, no dan LOW): `ImportError`
    con cuerpo silencioso ⇒ INFO (detección de feature), `FileNotFoundError`
    (sondas), `asyncio.CancelledError` (cancelación), timeouts de red/subproceso
    (los manejan los reintentos). Igual se **cuentan** en un INFO agregado para que
    la cobertura del detector sea visible y no parezca ciega.
    """
    name = "silent_exceptions"
    description = "Handlers de excepción vacíos o que tragan errores sin log"

    _LOG_ATTRS = ('log_event', 'log_error', 'logger', 'warning', 'error',
                  'exception', 'info', 'debug', 'critical',
                  '_log', '_log_error', '_log_event', '_log_agent')
    # ✅ ISSUE-106: llamadas "planas" que SÍ registran (el estándar del proyecto).
    _LOG_CALLS = ('log_degraded', 'log_agent', 'log_task')
    # ✅ ISSUE-106: tipos donde el silencio es sospechoso (da LOW).
    _WATCHED = ('Exception', 'BaseException', 'OSError', 'ValueError', 'KeyError',
                'TypeError', 'RuntimeError', 'json.JSONDecodeError', 'PermissionError')

    @staticmethod
    def _exc_name(node: Optional[ast.AST]) -> str:
        if node is None:
            return "bare"
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = SilentExceptionsDetector._exc_name(node.value) \
                if isinstance(node.value, (ast.Name, ast.Attribute)) else ""
            return f"{base}.{node.attr}" if base else node.attr
        if isinstance(node, ast.Tuple):
            return "tuple"
        return "?"

    @staticmethod
    def _iter_body(body: List[ast.stmt]):
        for stmt in body:
            yield stmt
            yield from ast.walk(stmt)

    @staticmethod
    def _enclosing_function(tree: ast.Module, lineno: int) -> Optional[str]:
        """Devuelve el nombre de la función/método que contiene la línea."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                    return node.name
        return None

    @classmethod
    def _nombres_de(cls, node: Optional[ast.AST]) -> List[str]:
        """Nombres de los tipos capturados, aplanando tuples (`except (A, B):`)."""
        if node is None:
            return ["bare"]
        if isinstance(node, ast.Tuple):
            fuera: List[str] = []
            for e in node.elts:
                fuera += cls._nombres_de(e)
            return fuera
        if isinstance(node, (ast.Name, ast.Attribute)):
            return [cls._exc_name(node)]
        return ["?"]

    @classmethod
    def _vigilado(cls, node: Optional[ast.AST]) -> bool:
        """True si el handler captura algún tipo VIGILADO (mirando dentro de tuples)."""
        return any(n in cls._WATCHED for n in cls._nombres_de(node))

    @classmethod
    def _captura_import_error(cls, node: Optional[ast.AST]) -> bool:
        return "ImportError" in cls._nombres_de(node) or \
            "ModuleNotFoundError" in cls._nombres_de(node)

    @classmethod
    def _registra(cls, body: List[ast.stmt]) -> bool:
        """True si el cuerpo REGISTRA algo (logger.*, helpers _log_*, log_degraded…)."""
        for n in cls._iter_body(body):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in cls._LOG_ATTRS:
                return True
            if isinstance(f, ast.Name) and f.id in cls._LOG_CALLS:
                return True
        return False

    @staticmethod
    def _is_logging_helper(name: Optional[str]) -> bool:
        """Función contenedora que es una guarda de logging best-effort."""
        if not name:
            return False
        low = name.lower()
        return ("log" in low or "_safe" in low or low.startswith("_dlog"))

    def _detect(self) -> None:
        # Agregación por archivo para no inundar el reporte
        pass_by_file: Dict[Path, List[int]] = defaultdict(list)
        import_pass_by_file: Dict[Path, List[int]] = defaultdict(list)
        silent_by_file: Dict[Path, List[int]] = defaultdict(list)
        no_vigilados: Dict[str, List[str]] = defaultdict(list)  # ✅ ISSUE-106 (por tipo)
        bare: List[Tuple[Path, int]] = []

        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                body = node.body
                exc = self._exc_name(node.type)

                # bare except: captura KeyboardInterrupt/SystemExit
                if node.type is None:
                    bare.append((rel, node.lineno))
                    continue

                # pass puro
                if not body or all(isinstance(s, ast.Pass) for s in body):
                    fn = self._enclosing_function(tree, node.lineno)
                    if self._is_logging_helper(fn):
                        continue  # guarda best-effort de logging — intencional
                    if self._captura_import_error(node.type):
                        # feature detection legítima (módulo opcional ausente)
                        import_pass_by_file[rel].append(node.lineno)
                    else:
                        pass_by_file[rel].append(node.lineno)
                    continue

                # handler que no usa la excepción y no registra nada
                names = {n.id for n in self._iter_body(body) if isinstance(n, ast.Name)}
                uses_exc = (node.name or "e") in names
                if uses_exc or self._registra(body):
                    continue
                if self._vigilado(node.type):
                    silent_by_file[rel].append(node.lineno)
                else:
                    # ✅ ISSUE-106: tipo NO vigilado (ImportError silencioso,
                    # FileNotFoundError, CancelledError, timeouts…) ⇒ se CUENTA en UN
                    # solo INFO agregado, con desglose por tipo: da cobertura visible
                    # sin inundar el reporte (un finding por archivo serían ~100).
                    for nombre_tipo in self._nombres_de(node.type):
                        no_vigilados[nombre_tipo].append(f"{rel}:{node.lineno}")

        # ── Emitir hallazgos agregados ───────────────────────────
        for rel, lines in pass_by_file.items():
            self._add(
                finding_id=f"AUD-SIL-{len(self.findings)+1:03d}",
                title=f"{len(lines)} handler(s) 'except ...: pass' en {rel}",
                severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                file=str(rel), line_start=lines[0],
                line_end=lines[-1],
                code_snippet=f"líneas: {', '.join(str(l) for l in lines[:6])}"
                             + ("…" if len(lines) > 6 else ""),
                explanation="Errores tragados sin loguear ni actuar. Muchos son "
                            "degradación intencional, pero algunos ocultan fallos reales.",
                recommendation="Loguear la excepción o documentar la degradación "
                                "intencional con un comentario.",
                confidence=0.7, tags={'silent', 'exception'},
            )
        for rel, lines in import_pass_by_file.items():
            self._add(
                finding_id=f"AUD-SIL-{len(self.findings)+1:03d}",
                title=f"{len(lines)} 'except ImportError: pass' en {rel} (feature detection)",
                severity=AudSeverity.INFO, verdict=AudVerdict.NEW,
                file=str(rel), line_start=lines[0],
                line_end=lines[-1],
                code_snippet=f"líneas: {', '.join(str(l) for l in lines[:6])}"
                             + ("…" if len(lines) > 6 else ""),
                explanation="Patrón típico de detección de dependencias opcionales — "
                            "normalmente intencional. Verificar que el flag de "
                            "disponibilidad se setee correctamente.",
                recommendation="Asegurar que el módulo setee su flag X_AVAILABLE "
                                "cuando el import falla.",
                confidence=0.4, tags={'silent', 'feature-detection'},
            )
        for rel, lines in silent_by_file.items():
            self._add(
                finding_id=f"AUD-SIL-{len(self.findings)+1:03d}",
                title=f"{len(lines)} handler(s) silencioso(s) en {rel}",
                severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                file=str(rel), line_start=lines[0],
                line_end=lines[-1],
                code_snippet=f"líneas: {', '.join(str(l) for l in lines[:6])}"
                             + ("…" if len(lines) > 6 else ""),
                explanation="Handlers que no usan la excepción ni loguean — "
                            "posibles errores ocultos o degradación no documentada.",
                recommendation="Loguear o documentar la degradación.",
                confidence=0.5, tags={'silent', 'exception', 'review'},
            )
        # ✅ ISSUE-106: cobertura visible — silenciosos de tipos NO vigilados
        # (ImportError, FileNotFoundError, CancelledError, timeouts…). No dan LOW
        # porque el silencio es normal ahí, pero se cuentan para que el detector no
        # parezca ciego (antes: 51 handlers invisibles en el repo real).
        if no_vigilados:
            total_nv = sum(len(v) for v in no_vigilados.values())
            archivos_nv = {d.split(":")[0] for v in no_vigilados.values() for d in v}
            desglose = " · ".join(f"{t_}: {len(v)}" for t_, v in
                                  sorted(no_vigilados.items(), key=lambda kv: -len(kv[1])))
            self._add(
                finding_id=f"AUD-SIL-{len(self.findings)+1:03d}",
                title=(f"{total_nv} handler(s) silenciosos de tipos NO vigilados "
                       f"en {len(archivos_nv)} archivo(s)"),
                severity=AudSeverity.INFO, verdict=AudVerdict.NEW,
                file="(varios)",
                line_start=0,
                code_snippet=desglose[:300],
                explanation="Handlers silenciosos cuyo tipo NO está en la lista "
                            "vigilada (detección de features opcionales, sondas, "
                            "cancelaciones, timeouts). Silencio esperado: no dan LOW.",
                recommendation="Revisar solo si el path implica pérdida de datos; "
                                "el criterio está documentado en el docstring del detector.",
                confidence=0.3, tags={'silent', 'exception', 'coverage'},
            )

        for rel, lineno in bare:
            self._add(
                finding_id=f"AUD-SIL-{len(self.findings)+1:03d}",
                title=f"except: desnudo en {rel}:{lineno}",
                severity=AudSeverity.HIGH, verdict=AudVerdict.NEW,
                file=str(rel), line_start=lineno,
                code_snippet="except:",
                explanation="Captura incluso KeyboardInterrupt/SystemExit y suele tragar errores.",
                recommendation="Usar `except Exception:` y loguear el error.",
                confidence=0.95, tags={'silent', 'exception', 'best-practice'},
            )


# ============================================================================
# DETECTOR: Entorno y modelos (config vs realidad)
# ============================================================================
# ✅ v8.0 (2026-08-25): Verifica consistencia .env vs .env.example y que no
# reaparezcan referencias a modelos obsoletos en código activo.
# ============================================================================

_OBSOLETE_MODELS = (
    "qwen2.5:3b", "axioma-code:latest", "huihui_ai/qwen2.5-abliterate:7b",
    "huihui_ai/qwen2.5-vl-abliterated:7b", "huihui_ai/qwen2.5-coder-abliterate:14b",
    "moondream:v2", "gemma4:e4b", "aletheia-3b:latest", "llava:7b",
    "mannix/llama3.1-8b-lexi:tools-q4_k_m",
)


class EnvModelsDetector(BaseDetector):
    """Configuración de entorno y modelos vs la realidad instalada."""
    name = "env_models"
    description = "Consistencia .env/.env.example y referencias a modelos obsoletos"

    def _detect(self) -> None:
        root = self.index.root

        # ── 1. .env vs .env.example ──────────────────────────────
        env_p = root / ".env"
        example_p = root / ".env.example"

        def _keys(p: Path) -> Set[str]:
            out: Set[str] = set()
            if not p.exists():
                return out
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                out.add(line.split("=")[0].strip())
            return out

        env_keys = _keys(env_p)
        example_keys = _keys(example_p)
        if env_keys and example_keys:
            missing = example_keys - env_keys
            extra = env_keys - example_keys
            if missing:
                self._add(
                    finding_id="AUD-ENV-001",
                    title=f"{len(missing)} claves de .env.example ausentes en .env",
                    severity=AudSeverity.LOW, verdict=AudVerdict.NEW,
                    file=".env", line_start=0,
                    code_snippet=", ".join(sorted(missing)[:8]),
                    explanation="El entorno no define claves que la configuración de ejemplo documenta.",
                    recommendation="Añadir las claves a .env o eliminarlas de .env.example si son legacy.",
                    confidence=0.7, tags={'env', 'config'},
                )
            if extra:
                self._add(
                    finding_id="AUD-ENV-002",
                    title=f"{len(extra)} claves en .env no documentadas en .env.example",
                    severity=AudSeverity.INFO, verdict=AudVerdict.NEW,
                    file=".env", line_start=0,
                    code_snippet=", ".join(sorted(extra)[:8]),
                    explanation="El .env usa claves que el ejemplo no documenta.",
                    recommendation="Documentarlas en .env.example.",
                    confidence=0.9, tags={'env', 'config'},
                )

        # ── 2. Modelos obsoletos en código ACTIVO (no comentarios/docstrings) ──
        found: List[Tuple[str, int, str]] = []
        for rel, tree in self.index.asts.items():
            if rel.name == 'axioma_auditor.py':
                continue  # no auto-auditar la lista del propio detector

            # ids de los Constants que son docstrings (módulo/clase/función)
            doc_ids: Set[int] = set()
            for n in ast.walk(tree):
                if not isinstance(n, (ast.Module, ast.ClassDef,
                                      ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if n.body and isinstance(n.body[0], ast.Expr) \
                        and isinstance(n.body[0].value, ast.Constant) \
                        and isinstance(n.body[0].value.value, str):
                    doc_ids.add(id(n.body[0].value))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) \
                        or not isinstance(node.value, str):
                    continue
                if id(node) in doc_ids:
                    continue  # docstring — contexto histórico legítimo
                low = node.value.lower()
                for m in _OBSOLETE_MODELS:
                    if m.lower() in low:
                        found.append((str(rel), node.lineno or 0, m))
                        break
        for rel, lineno, model in found[:20]:
            self._add(
                finding_id=f"AUD-MDL-{len(self.findings)+1:03d}",
                title=f"Modelo obsoleto '{model}' en {rel}:{lineno}",
                severity=AudSeverity.MEDIUM, verdict=AudVerdict.NEW,
                file=rel, line_start=lineno,
                code_snippet=model,
                explanation="Referencia activa a un modelo no instalado (404 en runtime).",
                recommendation="Reemplazar por settings.llm_* o un modelo instalado.",
                confidence=0.9, tags={'models', 'ollama', 'runtime'},
            )


