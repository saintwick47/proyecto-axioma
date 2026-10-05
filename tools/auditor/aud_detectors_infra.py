#!/usr/bin/env python3
"""
tools/auditor/aud_detectors_infra.py — detectores de infraestructura/robustez (LRU, CB, caches, timeouts, colas, colisiones)

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


# DETECTOR: LRU sin move_to_end
# ============================================================================

class LruWithoutMoveToEndDetector(BaseDetector):
    """OrderedDict con popitem(last=False) pero sin move_to_end en hit → FIFO puro."""
    name = "lru_without_move_to_end"
    description = "OrderedDict usado como LRU sin move_to_end en hit"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not (isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == 'OrderedDict'):
                    continue
                cls_name = self._find_enclosing_class(rel, node)
                if not cls_name:
                    continue
                cls_src = self._class_source(cls_name)
                has_popitem_lru = 'popitem(last=False)' in cls_src
                get_info = (self.index.get_method_source(cls_name, 'get')
                            or self.index.get_method_source(cls_name, '_get'))
                has_move = bool(get_info and 'move_to_end' in get_info[1])
                if has_popitem_lru and not has_move:
                    self._add(
                        finding_id=f"AUD-LRU-{len(self.findings)+1:03d}",
                        title=f"LRU sin move_to_end en {cls_name}",
                        severity=AudSeverity.MEDIUM,
                        verdict=AudVerdict.NEW,
                        file=str(rel),
                        line_start=node.lineno,
                        code_snippet=f"OrderedDict asignado en línea {node.lineno}",
                        explanation=(
                            f"La clase {cls_name} usa OrderedDict con popitem(last=False) "
                            "(evicción LRU) pero el método get/_get nunca llama move_to_end. "
                            "Comportamiento FIFO puro, no LRU: entradas frecuentemente accedidas "
                            "pueden ser eviccionadas igual que las no usadas."
                        ),
                        recommendation=(
                            "Copiar el patrón que ya existe en cache.py / "
                            "dispatcher_cache_metrics.py / client_base.py: "
                            "self._cache.move_to_end(key) en el hit path antes de retornar."
                        ),
                        confidence=0.9,
                        tags={'lru', 'cache', 'fifo-bug'},
                    )

    def _find_enclosing_class(self, rel: Path, target: ast.Assign) -> Optional[str]:
        tree = self.index.asts[rel]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in ast.walk(node):
                    if child is target:
                        return node.name
        return None

    def _class_source(self, class_name: str) -> str:
        for path, cls in self.index.classes.get(class_name, []):
            return self.index.files[path]
        return ""


# ============================================================================
# DETECTOR: CIRCUIT_BREAK cosmético
# ============================================================================

class CosmeticCircuitBreakDetector(BaseDetector):
    """Writes a cache keys que ningún archivo lee → 'corte' puramente cosmético."""
    name = "cosmetic_circuit_break"
    description = "CIRCUIT_BREAK que escribe keys sin lectores"

    KEY_PREFIXES = ['__fallback_', '__cb_', '__circuit_']

    def _detect(self) -> None:
        for prefix in self.KEY_PREFIXES:
            refs = self.index.grep(prefix, regex=False)
            writers: List[Tuple[Path, int, str]] = []
            readers: List[Tuple[Path, int, str]] = []
            for path, line, content in refs:
                stripped = content.strip()
                if stripped.startswith('#'):
                    continue
                if re.search(rf'{re.escape(prefix)}\w*\s*\]?\s*=\s*[^=]', stripped) \
                        or re.search(rf'\.set\([^)]*{re.escape(prefix)}', stripped):
                    writers.append((path, line, content))
                elif re.search(rf'\.get\s*\([^)]*{re.escape(prefix)}', stripped) \
                        or re.search(rf'if\s+[^:]*{re.escape(prefix)}\w*\s*\]', stripped) \
                        or re.search(rf'{re.escape(prefix)}\w*\s*\]\s*(?:!=|==|in\b)', stripped):
                    readers.append((path, line, content))
            if writers and not readers:
                for w_path, w_line, w_content in writers[:3]:
                    self._add(
                        finding_id=f"AUD-CB-{len(self.findings)+1:03d}",
                        title=f"CIRCUIT_BREAK cosmético: writes sin readers en {w_path}",
                        severity=AudSeverity.HIGH,
                        verdict=AudVerdict.NEW,
                        file=str(w_path),
                        line_start=w_line,
                        code_snippet=w_content.strip(),
                        explanation=(
                            f"Se escriben entradas con prefijo '{prefix}' (señal de circuit "
                            "break/fallback del self-healing) pero NINGÚN archivo del proyecto "
                            "las lee. El 'corte de circuito' es cosmético: no afecta el flujo. "
                            "Además, las entradas no se limpian cuando el handler se recupera, "
                            "causando growth ilimitado del cache."
                        ),
                        recommendation=(
                            "Opción A: Implementar el check en el dispatcher (leer la key antes "
                            "de delegar al handler y saltar al fallback). Opción B: Eliminar la "
                            "escritura y reemplazar por CircuitBreakerRegistry de tools_mcp."
                        ),
                        confidence=0.85,
                        tags={'circuit-breaker', 'dead-write', 'self-healing'},
                    )


# ============================================================================
# DETECTOR: Cache sin límite
# ============================================================================

class UnboundedCacheDetector(BaseDetector):
    """Dict plano usado como cache sin max_size ni eviction."""
    name = "unbounded_cache"
    description = "Caches dict sin max_size ni eviction"

    CACHE_ATTRS = ('_cache', '_decision_cache', '_route_cache',
                   '_result_cache', '_metrics_cache', '_response_cache')

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                cls = node
                cache_attrs: List[Tuple[str, int]] = []
                init_has_max_size = False
                for child in ast.walk(cls):
                    if isinstance(child, ast.Assign):
                        for tgt in child.targets:
                            if isinstance(tgt, ast.Attribute) \
                                    and tgt.attr in self.CACHE_ATTRS \
                                    and isinstance(child.value, ast.Dict):
                                cache_attrs.append((tgt.attr, child.lineno))
                    if isinstance(child, ast.Name) and child.id == 'max_size':
                        init_has_max_size = True
                for attr_name, line in cache_attrs:
                    get_src = (self._method_src(rel, cls, 'get')
                               or self._method_src(rel, cls, '_get')
                               or self._method_src(rel, cls, 'route'))
                    has_eviction = bool(get_src and (
                        'popitem' in get_src or 'del self.' in get_src
                        or 'max_size' in get_src
                        or 'next(iter(' in get_src
                    ))
                    if has_eviction or init_has_max_size:
                        continue
                    if get_src and 'ttl' in get_src.lower():
                        continue
                    self._add(
                        finding_id=f"AUD-UNC-{len(self.findings)+1:03d}",
                        title=f"Cache sin límite: {cls.name}.{attr_name}",
                        severity=AudSeverity.MEDIUM,
                        verdict=AudVerdict.NEW,
                        file=str(rel),
                        line_start=line,
                        code_snippet=f"self.{attr_name} = {{}}  # en {cls.name}",
                        explanation=(
                            f"El atributo {cls.name}.{attr_name} se inicializa como dict vacío "
                            "y no se encontró mecanismo de max_size ni eviction. En procesos de "
                            "vida larga (systemd) con keys variables, este cache crece sin límite. "
                            "El TTL por sí solo no evita el growth si las keys son únicas."
                        ),
                        recommendation=(
                            "Usar OrderedDict con popitem(last=False) cuando se exceda max_size, "
                            "o functools.lru_cache, o el patrón de RoutingCache ya existente."
                        ),
                        confidence=0.7,
                        tags={'cache', 'memory-leak', 'unbounded'},
                    )

    def _method_src(self, rel: Path, cls: ast.ClassDef, method: str) -> Optional[str]:
        for child in cls.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == method:
                lines = self.index.files[rel].splitlines()
                end = child.end_lineno or child.lineno
                return '\n'.join(lines[child.lineno - 1: end])
        return None


# ============================================================================
# DETECTOR: Fire-and-forget threads
# ============================================================================

class FireAndForgetThreadDetector(BaseDetector):
    """Threads sin pool y sin join() en hot paths."""
    name = "fire_and_forget_threads"
    description = "Threads lanzados sin control en hot paths"

    def _detect(self) -> None:
        hot_paths = {'dispatcher', 'router', 'cache', 'feedback', 'core'}
        for rel, tree in self.index.asts.items():
            if not any(h in str(rel) for h in hot_paths):
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_thread = False
                if isinstance(func, ast.Attribute) and func.attr == 'Thread':
                    is_thread = True
                elif isinstance(func, ast.Name) and func.id == 'Thread':
                    is_thread = True
                if not is_thread:
                    continue

                if self._has_join_call(tree, node):
                    continue

                src = self.index.files[rel]
                if 'ThreadPoolExecutor' in src or 'ThreadPool' in src:
                    continue

                self._add(
                    finding_id=f"AUD-FF-{len(self.findings)+1:03d}",
                    title=f"Thread fire-and-forget en hot path: {rel}",
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=node.lineno,
                    code_snippet=f"Thread en línea {node.lineno}",
                    explanation=(
                        "Se lanza un Thread sin pool y sin join() en hot path. "
                        "Bajo carga puede spawnear cientos de hilos."
                    ),
                    recommendation=(
                        "Usar ThreadPoolExecutor o asyncio.create_task. Si es necesario "
                        "un hilo, añadir join() para controlar su finalización."
                    ),
                    confidence=0.85,
                    tags={'threading', 'fire-and-forget', 'resource-leak'},
                )

    def _has_join_call(self, tree: ast.AST, thread_call: ast.Call) -> bool:
        parent = self._find_parent_function(tree, thread_call)
        if parent is None:
            return False
        for node in ast.walk(parent):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'join':
                    return True
        return False

    def _find_parent_function(self, tree: ast.AST, node: ast.AST) -> Optional[ast.AST]:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(n):
                    if child is node:
                        return n
        return None


# ============================================================================
# DETECTOR: Dead code riesgoso (CORREGIDO)
# ============================================================================

class RiskyDeadCodeDetector(BaseDetector):
    """Métodos públicos con time.sleep síncrono sin callers externos."""
    name = "risky_dead_code"
    description = "Métodos con time.sleep síncrono sin callers"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith('_'):
                    continue
                method_src = self._node_src(rel, node)
                if 'time.sleep' not in method_src:
                    continue
                first_line = method_src.split('\n')[0]
                if 'async def' in first_line:
                    continue
                callers = self.index.find_callers(node.name)
                # ✅ FIX v8.1 (2026-08-26, AUD-DC falsos positivos): antes solo
                # contaban callers EXTERNOS al archivo — un método llamado por
                # otro método del MISMO archivo (delegación, ej. wake_word.
                # listen_once → listen_for_wake, o benchmark_model → main())
                # se marcaba como dead code erróneamente. Ahora cuentan los
                # callers intra-archivo, EXCEPTO las auto-referencias (una
                # llamada dentro del propio cuerpo del método = recursión).
                real_callers: List[Tuple[Path, int]] = []
                for _p, _l in callers:
                    if _p == rel and node.lineno <= _l <= (node.end_lineno or node.lineno):
                        continue  # auto-referencia — no prueba uso
                    real_callers.append((_p, _l))
                if not real_callers:
                    # ✅ FIX: Buscar versión async en la misma clase
                    cls_node = self._find_enclosing_class(rel, node)
                    has_async_alternative = False
                    async_alternative_name = f"{node.name}_async"

                    if cls_node:
                        for child in cls_node.body:
                            if isinstance(child, ast.AsyncFunctionDef) and child.name == async_alternative_name:
                                has_async_alternative = True
                                break

                    # Ajustar severidad y recomendación según contexto
                    if has_async_alternative:
                        recommendation = (
                            f"Existe una versión async segura ({async_alternative_name}). "
                            f"Eliminar {node.name}() o renombrar a _sync para advertir el peligro."
                        )
                        confidence = 0.95 # Alta confianza
                    else:
                        recommendation = (
                            "Opción A: Eliminar si confirmas que no se usa. Opción B: Marcar "
                            "como async o documentar que requiere to_thread. Opción C: Renombrar "
                            "a _sync para advertir el peligro."
                        )
                        confidence = 0.7

                    self._add(
                        finding_id=f"AUD-DC-{len(self.findings)+1:03d}",
                        title=f"Dead code riesgoso: {node.name}() en {rel}",
                        severity=AudSeverity.MEDIUM,
                        verdict=AudVerdict.NEW,
                        file=str(rel),
                        line_start=node.lineno,
                        code_snippet=(method_src[:300] + '...'
                                      if len(method_src) > 300 else method_src),
                        explanation=(
                            f"El método público {node.name}() contiene time.sleep() síncrono "
                            "pero no tiene callers externos. Si algún día se invoca sin wrapper "
                            "async (asyncio.to_thread), bloqueará el event loop completo."
                        ),
                        recommendation=recommendation,
                        confidence=confidence,
                        tags={'dead-code', 'blocking-io', 'event-loop'},
                    )

    def _node_src(self, rel: Path, node: ast.FunctionDef) -> str:
        lines = self.index.files[rel].splitlines()
        end = node.end_lineno or node.lineno
        return '\n'.join(lines[node.lineno - 1: end])

    # ✅ FIX: Helper para encontrar la clase que envuelve al método
    def _find_enclosing_class(self, rel: Path, target: ast.FunctionDef) -> Optional[ast.ClassDef]:
        tree = self.index.asts[rel]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if child is target:
                        return node
        return None


# ============================================================================
# DETECTOR: Cascadas sin CB por proveedor (CORREGIDO)
# ============================================================================

class CascadeWithoutProviderCBDetector(BaseDetector):
    """Cascadas de proveedores (múltiples APIs externas) sin CB."""
    name = "cascade_without_provider_cb"
    description = "Cascadas multi-proveedor sin CB por proveedor"

    PROVIDER_KEYWORDS = ('tavily', 'serper', 'duckduckgo', 'openai',
                         'anthropic', 'gemini', 'mistral', 'perplexity')

    # ✅ FIX: Patrones que indican que un CB ya está en uso en la clase
    CB_USAGE_PATTERNS = ('_ProviderCircuit', 'circuit_breaker', 'CircuitBreaker',
                         'is_open(', 'record_failure(', 'record_success(',
                         '_circuits', '_cb_state')

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                # ✅ FIX: Obtener el código de la CLASE completa para buscar mitigaciones
                class_src = self._class_source(rel, node)

                for child in node.body:
                    if not isinstance(child, ast.FunctionDef):
                        continue
                    method_src = self._node_src(rel, child)

                    if self._is_strategy_sequence(method_src):
                        continue

                    providers_found = set()
                    for kw in self.PROVIDER_KEYWORDS:
                        if kw in method_src.lower():
                            providers_found.add(kw)
                    if len(providers_found) < 2:
                        continue

                    try_count = sum(1 for n in ast.walk(child) if isinstance(n, ast.Try))
                    if try_count < 2:
                        continue

                    # ✅ FIX: Buscar la existencia de CB en la CLASE, no solo en el método
                    has_cb = any(pattern in class_src for pattern in self.CB_USAGE_PATTERNS)

                    if not has_cb:
                        self._add(
                            finding_id=f"AUD-CAS-{len(self.findings)+1:03d}",
                            title=(f"Cascada sin CB por proveedor: "
                                   f"{node.name}.{child.name}()"),
                            severity=AudSeverity.MEDIUM,
                            verdict=AudVerdict.NEW,
                            file=str(rel),
                            line_start=child.lineno,
                            code_snippet=(method_src[:400] + '...' if len(method_src) > 400 else method_src),
                            explanation=(
                                f"El método {child.name}() implementa cascada multi-proveedor "
                                f"({', '.join(sorted(providers_found))}) sin memoria de fallos "
                                "recientes por proveedor. Si un proveedor cae, cada request "
                                "esperará su timeout completo antes de caer al siguiente, "
                                "multiplicando latencia bajo falla sostenida."
                            ),
                            recommendation=(
                                "Adoptar el patrón _ProviderCircuit que ya existe en "
                                "searcher.py y researcher_sources.py: un circuit breaker liviano "
                                "por proveedor con cooldown configurable."
                            ),
                            confidence=0.85,
                            tags={'circuit-breaker', 'cascade', 'latency'},
                        )

    def _is_strategy_sequence(self, src: str) -> bool:
        strategy_indicators = ['_classify_by_', '_is_', '_detect_', 'if self._']
        return any(ind in src for ind in strategy_indicators)

    def _node_src(self, rel: Path, node: ast.FunctionDef) -> str:
        lines = self.index.files[rel].splitlines()
        end = node.end_lineno or node.lineno
        return '\n'.join(lines[node.lineno - 1: end])

    # ✅ FIX: Helper para obtener el código fuente de toda la clase
    def _class_source(self, rel: Path, node: ast.ClassDef) -> str:
        lines = self.index.files[rel].splitlines()
        end = node.end_lineno or node.lineno
        return '\n'.join(lines[node.lineno - 1: end])


# ============================================================================
# DETECTOR: Timeout fijo (CORREGIDO + PARCHE FINAL)
# ============================================================================

class FixedTimeoutDetector(BaseDetector):
    """Timeouts hardcodeados sin adaptación a hardware."""
    name = "fixed_timeout"
    description = "Timeouts hardcodeados sin adaptación a hardware"

    # ✅ FIX: Patrones para detectar si ya existe un método dinámico en la clase
    DYNAMIC_TIMEOUT_PATTERNS = ['compute_timeout', 'calculate_timeout',
                                'adaptive_timeout', 'get_dynamic_timeout',
                                'HardwareFitManager']

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            # ✅ FIX: Evitar que el auditor se audite a sí mismo
            if rel.name == 'axioma_auditor.py':
                continue

            if not self.index.is_imported_in('hardware_fit', rel):
                continue

            src = self.index.files[rel]
            for m in re.finditer(
                r'(\w*timeout\w*)\s*=\s*([^;]+)',
                src, re.IGNORECASE
            ):
                line_num = src[:m.start()].count('\n') + 1
                var_name = m.group(1)
                expr = m.group(2).strip()

                if 'get_hardware' in expr or 'hardware_fit' in expr:
                    continue

                # ✅ FIX (2026-08-26, AUD-FT-001/002): un timeout config-driven
                # NO es un timeout fijo. Se salta si la expresión referencia
                # settings, self._* (config de instancia), getattr(...) u
                # os.environ — el valor puede ajustarse sin tocar código.
                if re.search(
                    r'\b(settings|self\.|getattr\s*\(|os\.environ)\b',
                    expr,
                ):
                    continue

                # ✅ FIX: Ignorar asignaciones booleanas o de strings (ej. has_local_timeouts = 'timeout=' in ...)
                if expr.lower().startswith(('true', 'false', 'in ', 'not ')):
                    continue

                method = self._enclosing_method(rel, line_num)
                if not method:
                    continue
                method_lower = method.lower()
                if any(ex in method_lower for ex in ('unload', 'evict', 'clear', 'cleanup')):
                    continue

                cls_node = self._enclosing_class(rel, line_num)
                has_dynamic_alternative = False
                recommendation = ""

                if cls_node:
                    class_src = self._class_source(rel, cls_node)
                    has_dynamic_alternative = any(pat in class_src for pat in self.DYNAMIC_TIMEOUT_PATTERNS)

                if has_dynamic_alternative:
                    recommendation = (
                        f"La clase ya tiene un método de timeout dinámico. Reutilizar ese método "
                        f"en lugar de hardcodear {var_name} = {expr}."
                    )
                    confidence = 0.9
                else:
                    recommendation = (
                        "Calcular timeout dinámico a partir de HardwareProfile (gpu_vram_gb, "
                        "ram_gb, cpu_threads)."
                    )
                    confidence = 0.7

                self._add(
                    finding_id=f"AUD-FT-{len(self.findings)+1:03d}",
                    title=f"Timeout fijo en {rel}::{method} (variable {var_name})",
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=line_num,
                    code_snippet=f"{var_name} = {expr}",
                    explanation=(
                        f"El método {method}() define un timeout ({var_name}) con un valor fijo "
                        "sin consultar hardware_fit. En hardware modesto puede ser insuficiente; "
                        "en potente, excesivamente conservador."
                    ),
                    recommendation=recommendation,
                    confidence=confidence,
                    tags={'timeout', 'hardware-fit', 'config'},
                )

    def _enclosing_method(self, rel: Path, line: int) -> Optional[str]:
        tree = self.index.asts[rel]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno <= line <= (node.end_lineno or node.lineno):
                    return node.name
        return None

    # ✅ FIX: Helper para encontrar la clase que envuelve la línea
    def _enclosing_class(self, rel: Path, line: int) -> Optional[ast.ClassDef]:
        tree = self.index.asts[rel]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.lineno <= line <= (node.end_lineno or node.lineno):
                    return node
        return None

    # ✅ FIX: Helper para obtener el código fuente de la clase
    def _class_source(self, rel: Path, node: ast.ClassDef) -> str:
        lines = self.index.files[rel].splitlines()
        end = node.end_lineno or node.lineno
        return '\n'.join(lines[node.lineno - 1: end])


# ============================================================================
# DETECTOR: Invalidación muerta (con filtro para servidores web)
# ============================================================================

class DeadInvalidationDetector(BaseDetector):
    """Parámetros de invalidación que nadie usa con True."""
    name = "dead_invalidation"
    description = "Parámetros de invalidación que nunca se activan"

    PARAMS = ['force_refresh', 'refresh', 'invalidate_cache',
              'no_cache', 'skip_cache', 'force_reload', 'reload']

    def _detect(self) -> None:
        for param in self.PARAMS:
            for rel, tree in self.index.asts.items():
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if param not in [a.arg for a in node.args.args]:
                        continue
                    # Excluir funciones de servidor web
                    if node.name in ('start_server', 'run_server', 'serve'):
                        continue
                    callers_with_true = 0
                    callers_total = 0
                    for caller_path, caller_line in self.index.find_callers(node.name):
                        ctx = self._context(caller_path, caller_line, 0, 3)
                        if f'{param}=True' in ctx or f'{param} = True' in ctx:
                            callers_with_true += 1
                        callers_total += 1
                    if callers_total > 0 and callers_with_true == 0:
                        self._add(
                            finding_id=f"AUD-DI-{len(self.findings)+1:03d}",
                            title=f"Invalidación muerta: {node.name}({param}=...)",
                            severity=AudSeverity.LOW,
                            verdict=AudVerdict.NEW,
                            file=str(rel),
                            line_start=node.lineno,
                            code_snippet=f"def {node.name}(..., {param}: bool = False, ...):",
                            explanation=(
                                f"El parámetro {param} de {node.name}() permite invalidar "
                                f"cache/forzar refresh, pero ninguno de los {callers_total} "
                                "callers del proyecto lo pasa en True. La invalidación está "
                                "implementada pero es inalcanzable — el cache nunca se refresca "
                                "ante cambios externos."
                            ),
                            recommendation=(
                                "Opción A: Invocar con True desde triggers externos (cambio de "
                                "hardware, modo, etc.). Opción B: Eliminar el parámetro si no "
                                "se va a usar."
                            ),
                            confidence=0.75,
                            tags={'dead-code', 'cache', 'invalidation'},
                        )

    def _context(self, path: Path, line: int, before: int, after: int) -> str:
        lines = self.index.files[path].splitlines()
        start = max(0, line - 1 - before)
        end = min(len(lines), line + after)
        return '\n'.join(lines[start:end])


# ============================================================================
# DETECTOR: Static priority (sin aging)
# ============================================================================

class StaticPriorityDetector(BaseDetector):
    """Priority queues sin aging → starvation de LOW."""
    name = "static_priority"
    description = "Priority queues sin aging"

    # ✅ v0.6.9v: keywords de aging. Se incluyen las variantes REALES en uso en
    # el proyecto ('aged_task' → `_pop_aged_task`, el mecanismo de TaskQueue) y
    # se evitan substrings peligrosos ('aged' suelto matchearía 'managed').
    _AGING_KEYWORDS = ('aging', 'aged_task', '_pop_aged', 'age_boost',
                       'priority_boost', 'priority_bump', '_adjust_priority')

    def _detect(self) -> None:
        for cls_name, locations in self.index.classes.items():
            if 'Queue' not in cls_name and 'Scheduler' not in cls_name:
                continue
            for rel, cls_node in locations:
                # ✅ v0.6.9v: se evalúa sobre IDENTIFICADORES de código (AST), no
                # sobre el texto crudo. Antes alcanzaba con que un docstring o
                # comentario dijera "aging" para que el detector quedara CIEGO
                # (falso negativo real, detectado con un caso sintético).
                file_ids = self._code_identifiers(self.index.asts.get(rel))
                has_priority = 'priority' in file_ids
                has_aging = any(kw in file_ids for kw in self._AGING_KEYWORDS)
                if not has_priority or has_aging:
                    continue
                pop_name = None
                pop = None
                pop_node = None
                for cand in ('pop', '_pop', 'dequeue', '_dequeue', '_pop_aged_task'):
                    node = self._method_node(cls_node, cand)
                    got = self._method(cls_node, rel, cand)
                    if got:
                        pop, pop_name, pop_node = got, cand, node
                        break
                if not pop:
                    continue
                method_src, method_line = pop
                method_ids = self._code_identifiers(pop_node)
                if any(kw in method_ids for kw in self._AGING_KEYWORDS):
                    continue
                # ✅ v0.6.9v: si el método SOLO delega en otra cola del mismo
                # método (p. ej. DistributedQueue.dequeue → queue.dequeue()), la
                # clase NO es dueña de la lógica de prioridad: el aging le
                # corresponde a la cola delegada (TaskQueue ya lo tiene —
                # _pop_aged_task). Marcarla era un FALSO POSITIVO.
                if self._is_pure_delegator(method_src, pop_name or 'dequeue'):
                    continue
                self._add(
                    finding_id=f"AUD-SP-{len(self.findings)+1:03d}",
                    title=f"Sin aging de prioridad en {cls_name}",
                    severity=AudSeverity.MEDIUM,
                    verdict=AudVerdict.NEW,
                    file=str(rel),
                    line_start=method_line,
                    code_snippet=(method_src[:400] + '...'
                                  if len(method_src) > 400 else method_src),
                    explanation=(
                        f"La cola {cls_name} usa prioridades estáticas asignadas al encolar. "
                        "Sin aging (incremento de prioridad por tiempo de espera), tareas LOW "
                        "pueden sufrir starvation indefinida bajo carga sostenida de HIGH/NORMAL."
                    ),
                    recommendation=(
                        "Implementar aging: priority_effective = priority_base + "
                        "age_seconds / 60. Pop por priority_effective, no por priority_base."
                    ),
                    confidence=0.8,
                    tags={'priority-queue', 'starvation', 'fairness'},
                )

    def _method(self, cls: ast.ClassDef, rel: Path,
                name: str) -> Optional[Tuple[str, int]]:
        for child in cls.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == name:
                lines = self.index.files[rel].splitlines()
                end = child.end_lineno or child.lineno
                return ('\n'.join(lines[child.lineno - 1: end]), child.lineno)
        return None

    @staticmethod
    def _method_node(cls: ast.ClassDef,
                     name: str) -> Optional[ast.AST]:
        """Nodo AST del método `name` de la clase (para análisis preciso)."""
        for child in cls.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == name:
                return child
        return None

    @staticmethod
    def _code_identifiers(node: Optional[ast.AST]) -> str:
        """Identificadores reales del código (minúsculas), sin comentarios.

        ✅ v0.6.9v: permite buscar palabras clave ('aging', 'priority', …) sin
        que un docstring o comentario que las mencione altere el resultado.
        """
        if node is None:
            return ""
        names: List[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.append(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.append(sub.attr)
            elif isinstance(sub, ast.arg):
                names.append(sub.arg)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(sub.name)
            elif isinstance(sub, ast.ClassDef):
                names.append(sub.name)
        return " ".join(names).lower()

    @staticmethod
    def _is_pure_delegator(method_src: str, method_name: str) -> bool:
        """True si el método solo reenvía a OTRA cola el mismo método.

        ✅ v0.6.9v: evita el falso positivo sobre wrappers que no son dueños de
        la lógica de prioridad. Ejemplo real: `DistributedQueue.dequeue()` hace
        `return await queue.dequeue(timeout=timeout)` — el aging vive en la
        `TaskQueue` delegada (`_pop_aged_task`), no acá.
        """
        if not method_src:
            return False
        # Llamada a `.dequeue(` / `._pop(` / `.pop(` sobre otra instancia.
        for pattern in (f".{method_name}(", ".dequeue(", "._dequeue(",
                        "._pop(", ".pop("):
            if pattern not in method_src:
                continue
            # No debe manejar su propia estructura de prioridad.
            owns_structure = any(kw in method_src for kw in
                                 ("heappush", "heappop", "PriorityQueue(",
                                  ".put_nowait(", "_queue.put("))
            if not owns_structure:
                return True
        return False


# ============================================================================
# DETECTOR: Conexión nueva por método
# ============================================================================

class ConnectionPerMethodDetector(BaseDetector):
    """Clases SQLite con conexión nueva en >50% de métodos sin pool."""
    name = "connection_per_method"
    description = "Conexiones SQLite nuevas en muchos métodos"

    def _detect(self) -> None:
        for rel, tree in self.index.asts.items():
            src = self.index.files[rel]
            if 'sqlite3' not in src:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods_with_conn = 0
                methods_total = 0
                first_line = 0
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods_total += 1
                        msrc = self._method_src(rel, node, child.name)
                        if 'sqlite3.connect' in msrc and 'with sqlite3.connect' not in msrc:
                            methods_with_conn += 1
                            if first_line == 0:
                                first_line = child.lineno
                has_pool = ('SQLiteConnectionPool' in src
                            or 'ConnectionPool' in src or '_pool' in src)
                if methods_total > 1 and (methods_with_conn / methods_total) > 0.5 and not has_pool:
                    self._add(
                        finding_id=f"AUD-CP-{len(self.findings)+1:03d}",
                        title=f"Conexión nueva por método en {node.name}",
                        severity=AudSeverity.MEDIUM,
                        verdict=AudVerdict.NEW,
                        file=str(rel),
                        line_start=first_line,
                        code_snippet=(f"{methods_with_conn}/{methods_total} métodos "
                                      f"crean sqlite3.connect() sin 'with'"),
                        explanation=(
                            f"La clase {node.name} crea conexión SQLite en {methods_with_conn} de "
                            f"{methods_total} métodos sin usar 'with' (lo que cierra automáticamente). "
                            "Esto puede dejar conexiones abiertas o causar overhead."
                        ),
                        recommendation=(
                            "Adoptar SQLiteConnectionPool, o usar 'with sqlite3.connect(...) as conn'."
                        ),
                        confidence=0.8,
                        tags={'sqlite', 'connection-pool', 'performance'},
                    )

    def _method_src(self, rel: Path, cls: ast.ClassDef, method: str) -> str:
        for child in cls.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method:
                lines = self.index.files[rel].splitlines()
                end = child.end_lineno or child.lineno
                return '\n'.join(lines[child.lineno - 1: end])
        return ""


# ============================================================================
# DETECTOR: Missing global timeout (sin cambios)
# ============================================================================

class MissingGlobalTimeoutDetector(BaseDetector):
    """Loops async sin asyncio.wait_for envolvente."""
    name = "missing_global_timeout"
    description = "Loops async sin asyncio.wait_for envolvente"

    LOOP_METHODS = ['run_forever', 'run_full_duplex', '_listen_loop',
                    '_process_utterance_stream', 'process_stream',
                    'run_once', '_run_daemon', '_widget_supervisor',
                    '_dialog_poll_loop']

    def _detect(self) -> None:
        for method_name in self.LOOP_METHODS:
            for rel, tree in self.index.asts.items():
                for node in ast.walk(tree):
                    if not isinstance(node, ast.AsyncFunctionDef) \
                            or node.name != method_name:
                        continue
                    msrc = self._node_src(rel, node)
                    has_wait_for = ('asyncio.wait_for' in msrc
                                    or 'asyncio.timeout' in msrc)
                    has_local_timeouts = ('timeout=' in msrc
                                          or 'asyncio.wait_for' in msrc)
                    if not has_wait_for and has_local_timeouts:
                        self._add(
                            finding_id=f"AUD-GT-{len(self.findings)+1:03d}",
                            title=f"Sin timeout global en {method_name}() — {rel}",
                            severity=AudSeverity.HIGH,
                            verdict=AudVerdict.NEW,
                            file=str(rel),
                            line_start=node.lineno,
                            code_snippet=(msrc[:400] + '...'
                                          if len(msrc) > 400 else msrc),
                            explanation=(
                                f"El método async {method_name}() tiene timeouts locales (en "
                                "sub-llamadas) pero ningún asyncio.wait_for envolvente que "
                                "acote el turno completo. Si una operación intermedia cuelga "
                                "sin lanzar excepción, todo el loop se congela sin recuperación."
                            ),
                            recommendation=(
                                f"Envolver el cuerpo principal de {method_name}() en "
                                "asyncio.wait_for(self._do_work(), timeout=GLOBAL_TIMEOUT) "
                                "con timeout apropiado al escenario."
                            ),
                            confidence=0.75,
                            tags={'timeout', 'async', 'hang', 'recovery'},
                        )

    def _node_src(self, rel: Path, node: ast.AsyncFunctionDef) -> str:
        lines = self.index.files[rel].splitlines()
        end = node.end_lineno or node.lineno
        return '\n'.join(lines[node.lineno - 1: end])


# ============================================================================
# DETECTOR: Name collision
# ============================================================================

class NameCollisionDetector(BaseDetector):
    """Clases con mismo nombre en distintos módulos con firmas distintas."""
    name = "name_collision"
    description = "Colisión de nombres de clases entre módulos"

    def _detect(self) -> None:
        for cls_name, locations in self.index.classes.items():
            if len(locations) < 2:
                continue
            unique_paths = set(p for p, _ in locations if p.name != '__init__.py')
            if len(unique_paths) < 2:
                continue
            signatures = []
            for path, cls_node in locations:
                init = next((c for c in cls_node.body
                             if isinstance(c, ast.FunctionDef)
                             and c.name == '__init__'), None)
                if init:
                    args = tuple(a.arg for a in init.args.args)
                    signatures.append((path, args))
            if len(signatures) < 2:
                continue
            unique_sigs = set(s for _, s in signatures)
            if len(unique_sigs) > 1:
                files_str = ', '.join(str(p) for p, _ in signatures)
                self._add(
                    finding_id=f"AUD-NC-{len(self.findings)+1:03d}",
                    title=f"Colisión de nombre: {cls_name} en {len(signatures)} archivos",
                    severity=AudSeverity.LOW,
                    verdict=AudVerdict.NEW,
                    file=str(signatures[0][0]),
                    line_start=0,
                    code_snippet=f"class {cls_name} definido en: {files_str}",
                    explanation=(
                        f"La clase {cls_name} está definida en {len(signatures)} archivos "
                        "distintos con firmas de __init__ diferentes. No es bug de wiring "
                        "(cada consumidor importa la correcta), pero genera confusión al "
                        "leer el código y es fuente potencial de bugs al refactorizar."
                    ),
                    recommendation=(
                        "Renombrar para desambiguar (ej: RouterCacheL1 vs RouterCacheL2 vs "
                        "RouterCacheL3) o consolidar en una sola clase con parámetros."
                    ),
                    confidence=0.85,
                    tags={'naming', 'maintainability', 'tech-debt'},
                )


