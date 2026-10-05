#!/usr/bin/env python3
"""
tools/auditor/core.py — modelos, CodeIndex y BaseDetector

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

# ============================================================================
# DOMINIO
# ============================================================================

class AudVerdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    DUBIOUS = "DUBIOUS"
    NEW = "NEW"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    FIXED = "FIXED"


class AudSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


@dataclass
class AudFinding:
    finding_id: str
    title: str
    severity: AudSeverity
    verdict: AudVerdict
    detector: str
    file: str
    line_start: int = 0
    line_end: int = 0
    code_snippet: str = ""
    explanation: str = ""
    recommendation: str = ""
    confidence: float = 1.0
    cross_refs: List[str] = field(default_factory=list)
    tags: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['severity'] = self.severity.value
        d['verdict'] = self.verdict.value
        d['tags'] = sorted(self.tags)
        return d


@dataclass
class DetectorResult:
    name: str
    description: str
    findings: List[AudFinding]
    duration_ms: float
    files_scanned: int


def _to_snake_case(name: str) -> str:
    """CamelCase → snake_case (TaskQueue → task_queue, CacheL2 → cache_l2)."""
    s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    s2 = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1)
    return s2.lower()


def _resolve_imports_for(rel: Path, tree: ast.Module) -> Set[str]:
    """Resuelve los imports de un archivo a nombres de módulo ABSOLUTOS.

    ✅ v8.3 (2026-09-01): fusionado de axioma_inspector._resolve_relative_import.
    CodeIndex guarda los ImportFrom con node.module crudo (sin el prefijo del
    paquete para imports relativos); este helper reconstruye el módulo
    absoluto (ej. `from .coder_core import X` en src/agents/coder.py →
    `src.agents.coder_core`). Necesario para integration y circular_import.
    """
    rel_str = str(rel).replace("\\", "/")
    parts = rel_str.replace("/", ".").replace(".py", "").split(".")
    if parts and parts[-1] == "__init__":
        pkg_parts = parts[:-1]
    else:
        pkg_parts = parts[:-1]

    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    out.add(node.module)
            else:
                up = node.level - 1
                base = pkg_parts
                if up > 0:
                    if up >= len(base):
                        continue
                    base = base[:-up]
                if node.module:
                    out.add(".".join(base + node.module.split(".")))
                else:
                    for alias in node.names:
                        out.add(".".join(base + [alias.name]))
    return out


def _resolve_top_level_imports_for(rel: Path, tree: ast.Module) -> Set[str]:
    """Igual que _resolve_imports_for pero SOLO imports a nivel de módulo:
    NO baja a funciones/métodos/clases y saltea ramas `if TYPE_CHECKING:`.

    ✅ v0.6.8tt (CIR-001): el grafo estático marcaba un ciclo falso
    dispatcher → jarvis_engine → dispatcher porque contaba imports LAZY
    (dentro de métodos: dispatcher.py:119) y de TIPO (jarvis_engine.py:26,
    bajo TYPE_CHECKING) como dependencias de import de runtime. En runtime
    ambos módulos importan limpio (verificado con import directo). Un import
    lazy o de tipado NO crea ciclo de inicialización de módulos.
    """
    rel_str = str(rel).replace("\\", "/")
    parts = rel_str.replace("/", ".").replace(".py", "").split(".")
    pkg_parts = parts[:-1] if parts[-1] != "__init__" else parts[:-1]

    out: Set[str] = set()

    def _add_import(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    out.add(node.module)
            else:
                up = node.level - 1
                base = pkg_parts
                if up > 0:
                    if up >= len(base):
                        return
                    base = base[:-up]
                if node.module:
                    out.add(".".join(base + node.module.split(".")))
                else:
                    for alias in node.names:
                        out.add(".".join(base + [alias.name]))

    def _scan(stmts: List[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                _add_import(stmt)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # imports lazy/dentro: no crean ciclo de import
            elif isinstance(stmt, ast.If):
                # `if TYPE_CHECKING:` es solo tipado estático
                if isinstance(stmt.test, ast.Name) and stmt.test.id == "TYPE_CHECKING":
                    continue
                _scan(list(stmt.body) + list(stmt.orelse))
            elif isinstance(stmt, ast.Try):
                _scan(list(stmt.body) + list(stmt.orelse)
                      + [h for h in stmt.handlers]
                      + list(stmt.finalbody))
            elif isinstance(stmt, ast.With):
                for item in stmt.body:
                    if isinstance(item, (ast.Import, ast.ImportFrom)):
                        _add_import(item)
    _scan(list(tree.body))
    return out


# ============================================================================
# ÍNDICE DE CÓDIGO
# ============================================================================

class CodeIndex:
    """Índice del proyecto para lookup rápido cross-file."""

    def __init__(self, root: Path):
        self.root = root
        self.files: Dict[Path, str] = {}
        self.asts: Dict[Path, ast.Module] = {}
        self.classes: Dict[str, List[Tuple[Path, ast.ClassDef]]] = defaultdict(list)
        self.functions: Dict[str, List[Tuple[Path, ast.FunctionDef]]] = defaultdict(list)
        self.imports_per_file: Dict[Path, Set[str]] = {}

    def build(self, exclude_dirs: Optional[Set[str]] = None) -> None:
        exclude = exclude_dirs or {
            '.git', 'data', 'logs', '__pycache__', '.venv', 'venv',
            '.optimizer_backups', 'node_modules', 'tests', 'benchmarks',
            'tools', 'docs', 'reports', 'cache', 'inforchat',
            'deepseek-harness', '.pytest_cache', '.coverage'
        }
        for path in self.root.rglob('*.py'):
            if any(part in exclude for part in path.parts):
                continue
            try:
                src = path.read_text(encoding='utf-8', errors='ignore')
                tree = ast.parse(src, filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = path.relative_to(self.root)
            self.files[rel] = src
            self.asts[rel] = tree
            self._index_tree(rel, tree)

    def _index_tree(self, rel: Path, tree: ast.Module) -> None:
        imports: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if not any(self._is_test_base(b) for b in node.bases):
                    self.classes[node.name].append((rel, node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name].append((rel, node))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
        self.imports_per_file[rel] = imports

    def _is_test_base(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and 'Test' in node.id:
            return True
        if isinstance(node, ast.Attribute) and 'TestCase' in node.attr:
            return True
        return False

    def grep(self, pattern: str, *, regex: bool = False,
             case_sensitive: bool = True) -> List[Tuple[Path, int, str]]:
        results: List[Tuple[Path, int, str]] = []
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = re.compile(pattern, flags) if regex else None
        for rel, src in self.files.items():
            for i, line in enumerate(src.splitlines(), 1):
                if regex:
                    if pat and pat.search(line):
                        results.append((rel, i, line))
                else:
                    if pattern in line:
                        results.append((rel, i, line))
        return results

    def find_callers(self, func_name: str) -> List[Tuple[Path, int]]:
        callers: List[Tuple[Path, int]] = []
        for rel, tree in self.asts.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    if isinstance(f, ast.Name) and f.id == func_name:
                        callers.append((rel, node.lineno))
                    elif isinstance(f, ast.Attribute) and f.attr == func_name:
                        callers.append((rel, node.lineno))
        return callers

    def is_imported_in(self, module_name: str, file_rel: Path) -> bool:
        imports = self.imports_per_file.get(file_rel, set())
        return any(module_name in imp for imp in imports)

    def get_method_source(self, class_name: str,
                          method_name: str) -> Optional[Tuple[Path, str, int]]:
        for path, cls in self.classes.get(class_name, []):
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == method_name:
                    src = self.files[path]
                    lines = src.splitlines()
                    end = node.end_lineno or node.lineno
                    snippet = '\n'.join(lines[node.lineno - 1: end])
                    return (path, snippet, node.lineno)
        return None


# ============================================================================
# DETECTORES BASE
# ============================================================================

class BaseDetector:
    name: str = "base"
    description: str = ""

    def __init__(self, index: CodeIndex):
        self.index = index
        self.findings: List[AudFinding] = []

    def run(self) -> DetectorResult:
        t0 = time.perf_counter()
        self._detect()
        return DetectorResult(
            name=self.name,
            description=self.description,
            findings=self.findings,
            duration_ms=(time.perf_counter() - t0) * 1000,
            files_scanned=len(self.index.files),
        )

    def _detect(self) -> None:
        raise NotImplementedError

    def _add(self, **kwargs) -> None:
        kwargs.setdefault('detector', self.name)
        kwargs.setdefault('confidence', 0.8)
        self.findings.append(AudFinding(**kwargs))


# ============================================================================
