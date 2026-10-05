#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
# axioma_doc_flow_mapper.py - Mapeador de Flujos Cruzados mediante AST
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_flow_mapper.py
# Autor: SaintWick
# Versión: 3.9.8
#
# Analiza el código fuente para construir un grafo de dependencias entre módulos,
# rastrear el uso de símbolos críticos y determinar el estado de integración
# de cada módulo ([✅ CONECTADO] o [⚠️ AISLADO]). Soporta FastAPI routes,
# imports relativos, subprocess launches y evaluación de contratos de flujo.
# ═══════════════════════════════════════════════════════════════
import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict


# ==================== ESTRUCTURAS DE DATOS ====================

@dataclass
class ModuleEdge:
    """Arista de dependencia entre módulos"""
    source: str
    target: str
    edge_type: str = "import"
    symbols: List[str] = field(default_factory=list)
    call_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source': self.source,
            'target': self.target,
            'edge_type': self.edge_type,
            'symbols': self.symbols,
            'call_count': self.call_count,
        }


@dataclass
class SymbolTrace:
    """Rastreo de uso de un símbolo crítico"""
    symbol_name: str
    defined_in: Optional[str] = None
    used_in: List[str] = field(default_factory=list)
    call_sites: int = 0
    instantiation_count: int = 0
    is_async: bool = False
    internal_uses: int = 0  # ✅ v3.9.3: Usos dentro del mismo archivo (implementación interna)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol_name': self.symbol_name,
            'defined_in': self.defined_in,
            'used_in': self.used_in,
            'call_sites': self.call_sites,
            'instantiation_count': self.instantiation_count,
            'is_async': self.is_async,
            'internal_uses': self.internal_uses,
        }


@dataclass
class FlowContract:
    """Contrato de flujo entre componentes"""
    name: str
    components: List[str]
    status: str = "UNKNOWN"
    badge: str = "[❓ PENDIENTE]"
    references: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'components': self.components,
            'status': self.status,
            'badge': self.badge,
            'references': self.references,
        }


@dataclass
class IntegrationReport:
    """Reporte consolidado de integración del proyecto"""
    module_graph: Dict[str, List[ModuleEdge]] = field(default_factory=dict)
    symbol_traces: Dict[str, SymbolTrace] = field(default_factory=dict)
    flow_contracts: List[FlowContract] = field(default_factory=list)
    isolated_modules: List[str] = field(default_factory=list)
    connected_modules: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'module_graph': {k: [e.to_dict() for e in v] for k, v in self.module_graph.items()},
            'symbol_traces': {k: v.to_dict() for k, v in self.symbol_traces.items()},
            'flow_contracts': [fc.to_dict() for fc in self.flow_contracts],
            'isolated_modules': self.isolated_modules,
            'connected_modules': self.connected_modules,
        }


# ==================== FILTROS DE EXCLUSIÓN (LISTA NEGRA) ====================
# En lugar de una lista blanca que se vuelve obsoleta, usamos una lista negra
# para excluir ruido conocido. Si creas un agente nuevo, se detecta solo.
IGNORED_PATTERNS = [
    # Outputs generados por CoderAgent — código muerto, no arquitectura
    'data/outputs/',
    # Tests — no pertenecen al grafo de dependencias de producción
    'tests/',
    # Scripts raíz que no son parte del sistema core
    'axioma_inspector.py',
    'network_scanner.py',
    # ✅ v3.9.4: Herramientas standalone intencionales — no forman parte del grafo de producción.
    # NOTA: benchmark_suites.py está [✅ CONECTADO] (usa StateManager, ToolExecutor, get_router_cache)
    # y NO debe excluirse. Los patrones siguientes son precisos para no capturarlo.
    'benchmark_latency',       # tools/benchmark_latency.py
    'benchmark_response',      # tools/benchmark_response.py
    'verify_',                 # tools/verify_axioma.py, verify_chromadb.py (verify_all eliminado)
    'system_check',            # tools/system_check.py (fusión health/setup/diagnostic, 2026-09-01)
    'model_tester',            # tools/model_tester.py
    'vllm_client',             # src/llm/vllm_client.py
]

# ✅ v3.9.5: funciones que lanzan un archivo del proyecto como proceso del OS,
# no como import — usado por FlowMapper._track_subprocess_launches().
SUBPROCESS_LAUNCH_FUNCS = {
    'create_subprocess_exec',
    'create_subprocess_shell',
    'Popen',
    'run',
    'call',
    'check_call',
    'check_output',
}

# Contratos de flujo que requieren validación estricta porque cruzan módulos.
FLOW_CONTRACTS_CONFIG = [
    {
        "name": "Contexto→Dispatcher→Clasificador",
        "components": ["SessionContext", "Dispatcher", "IntentClassifier"],
    },
    {
        "name": "Pipeline_Voz_Full_Duplex",
        "components": ["VoicePipeline", "process_stream", "run_full_duplex"],
    },
    {
        "name": "Colaboración_Tareas",
        "components": ["TaskQueue", "Dispatcher", "ValidatorAgent"],
    },
    # ✅ v3.9.4: Contratos HardwareFit — nuevos módulos hardware_fit.py + routes_hwfit.py
    {
        "name": "HardwareFit→ModelSwap",
        # HardwareFitManager.check_model() es consumido por _check_model_fit() en model_swap.py
        "components": ["HardwareFitManager", "_check_model_fit"],
    },
    {
        "name": "HardwareFit→AxiomaServer",
        # HardwareFitManager expuesto vía endpoints REST en routes_hwfit.py
        "components": ["HardwareFitManager", "hwfit_check_model"],
    },
    {
        "name": "HardwareFit→MainStartup",
        # is_current_config_compatible() llamado en main.py durante arranque
        "components": ["HardwareFitManager", "is_current_config_compatible"],
    },
]


# ==================== MAPEADOR DE FLUJOS v3.9.8 ====================

class FlowMapper:
    """
    Construye grafo de dependencias AST, rastrea símbolos y determina
    estado de integración entre módulos del proyecto AXIOMA de forma DINÁMICA.

    Criterios de conexión (por orden de fuerza):
      FUERTE  — instanciación directa (X = Cls()) o llamada (cls.method())
               desde un archivo distinto al de definición
      MEDIO   — importado y usado en type hints / argumentos desde otro archivo
      INTERNO — detalle de implementación interna (ej. Config, Adapter, Exception)
               usado dentro de su propio archivo, el cual está conectado al sistema
      AISLADO — ninguna referencia externa al archivo de definición
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.module_graph: Dict[str, List[ModuleEdge]] = defaultdict(list)
        self.symbol_traces: Dict[str, SymbolTrace] = {}
        self.file_trees: Dict[str, ast.AST] = {}
        self.file_sources: Dict[str, str] = {}
        # ✅ v3.9.5: archivos lanzados vía subprocess (ej. rafael_widget.py desde
        # rafael_daemon.py con asyncio.create_subprocess_exec) — no son "import",
        # nunca van a tener un ast.Import/ImportFrom que los referencie, así que
        # necesitan su propio mecanismo de detección. Ver _track_subprocess_launches().
        self.subprocess_launched_files: Set[str] = set()
        # ✅ v3.9.8: Cache de archivos que son targets en module_graph
        self._imported_files: Set[str] = set()

    # ─── carga ────────────────────────────────────────────────────

    def load_project_files(self, file_paths: List[Path]) -> None:
        """Carga y parsea AST de todos los archivos objetivo."""
        for fp in file_paths:
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    source = f.read()
                rel_path = str(fp.relative_to(self.project_root))
                self.file_trees[rel_path] = ast.parse(source, filename=rel_path)
                self.file_sources[rel_path] = source
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue

    # ─── helpers AST ──────────────────────────────────────────────

    def _resolve_module_path(self, module_dotted: str) -> Optional[str]:
        """Resuelve import dotted a ruta relativa del proyecto."""
        candidate = module_dotted.replace('.', '/') + '.py'
        for rel_path in self.file_trees.keys():
            if rel_path.endswith(candidate) or candidate.endswith(rel_path):
                return rel_path
        return None

    def _resolve_relative_import(
        self,
        current_rel_path: str,
        level: int,
        module: Optional[str],
        name: str,
    ) -> Optional[str]:
        """
        ✅ v3.9.6 FIX: Resuelve imports relativos (from . import X) a ruta física.

        En Python, `from . import modulo` produce node.level=1 y node.module=None.
        Antes, la condición `and node.module:` en _extract_imports descartaba estos
        imports por completo, marcando como AISLADOS módulos perfectamente conectados
        (ej. routes_hwfit.py importado desde server.py).

        Semántica de `level`:
          level=1 → mismo directorio que el archivo actual
          level=2 → directorio padre
          level=3 → abuelo, etc.
        """
        parts = Path(current_rel_path).parts
        # Directorio que contiene al archivo actual (sin el archivo)
        dir_parts = list(parts[:-1])
        # level=1 = "este paquete" = no subir nada extra
        # level=2 = subir 1, level=3 = subir 2, etc.
        up = level - 1
        if up > 0:
            if up >= len(dir_parts):
                return None  # Sube más allá de la raíz del proyecto
            dir_parts = dir_parts[:-up]

        base_dir = '/'.join(dir_parts) if dir_parts else ''

        # Construir lista de rutas candidatas
        candidates: List[str] = []
        if module:
            # from .modulo import X  →  target es `modulo` (X es símbolo dentro)
            # from .paquete.sub import X  →  target es paquete/sub
            mod_path = module.replace('.', '/')
            candidates.append(f"{base_dir}/{mod_path}.py" if base_dir else f"{mod_path}.py")
            candidates.append(f"{base_dir}/{mod_path}/__init__.py" if base_dir else f"{mod_path}/__init__.py")
            # Caso: from .modulo import submodulo  (submodulo es a su vez un archivo)
            candidates.append(f"{base_dir}/{mod_path}/{name}.py" if base_dir else f"{mod_path}/{name}.py")
        else:
            # from . import X  →  target es X (archivo o paquete en base_dir)
            candidates.append(f"{base_dir}/{name}.py" if base_dir else f"{name}.py")
            candidates.append(f"{base_dir}/{name}/__init__.py" if base_dir else f"{name}/__init__.py")

        # 1) Match exacto contra file_trees
        for candidate in candidates:
            normalized = candidate.lstrip('/')
            if normalized in self.file_trees:
                return normalized

        # 2) Match por sufijo (defensivo, igual que _resolve_module_path)
        for candidate in candidates:
            normalized = candidate.lstrip('/')
            for rel in self.file_trees:
                if rel.endswith(normalized) or normalized.endswith(rel):
                    return rel

        return None

    def _find_trailing_py_constant(self, node: ast.AST) -> Optional[str]:
        """
        ✅ v3.9.5: Busca una constante string terminada en ".py" dentro de un
        subárbol AST — usado para resolver expresiones tipo
        `Paths.SRC / "interfaces" / "rafael_widget.py"` (cadena de BinOp/Path)
        a un nombre de archivo. Solo hay una constante ".py" posible en estas
        expresiones, así que no importa el orden de ast.walk().
        """
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.endswith('.py'):
                return sub.value
        return None

    def _track_subprocess_launches(self) -> None:
        """
        ✅ v3.9.5: Detecta archivos del proyecto lanzados como subprocess del OS
        (ej. rafael_widget.py vía asyncio.create_subprocess_exec desde
        rafael_daemon.py). Estos NUNCA generan un ast.Import/ImportFrom —
        _extract_imports() no tiene forma de verlos — así que sin esto quedan
        [⚠️ AISLADO] pese a correr en producción como proceso separado.

        Heurística en dos pasadas por archivo:
          1. Indexa asignaciones `_VAR = Paths.X / "y" / "archivo.py"` como
             {nombre_variable: "archivo.py"}.
          2. Busca llamadas a SUBPROCESS_LAUNCH_FUNCS; por cada argumento,
             resuelve un nombre de archivo .py ya sea directo (constante) o
             indirecto (Name/​str(Name) contra el índice del paso 1), y lo
             matchea por sufijo contra file_trees vía _resolve_module_path().

        No requiere ser exhaustivo — cualquier match es señal suficiente de
        que el archivo target no está aislado, que es lo único que se usa.
        """
        for rel_path, tree in self.file_trees.items():
            if self._is_noise(rel_path):
                continue

            # Paso 1: variable -> "archivo.py"
            path_vars: Dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value_node = node.value
                    if value_node is None:
                        continue
                    py_name = self._find_trailing_py_constant(value_node)
                    if not py_name:
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            path_vars[t.id] = py_name
                        elif isinstance(t, ast.Attribute):
                            path_vars[t.attr] = py_name

            # Paso 2: llamadas a funciones de lanzamiento de subprocess
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if self._get_call_name(node) not in SUBPROCESS_LAUNCH_FUNCS:
                    continue

                for arg in node.args:
                    py_name = self._find_trailing_py_constant(arg)
                    if not py_name and isinstance(arg, ast.Call) and arg.args:
                        # patrón str(_WIDGET_SCRIPT) — resolver el Name interno
                        inner = arg.args[0]
                        if isinstance(inner, ast.Name):
                            py_name = path_vars.get(inner.id)
                    if not py_name and isinstance(arg, ast.Name):
                        py_name = path_vars.get(arg.id)

                    if not py_name:
                        continue

                    target = self._resolve_module_path(py_name[:-3])
                    if target and target != rel_path:
                        self.subprocess_launched_files.add(target)
                        self.module_graph[rel_path].append(ModuleEdge(
                            source=rel_path,
                            target=target,
                            edge_type="subprocess",
                            symbols=[py_name],
                        ))

    def _extract_imports(self, rel_path: str, tree: ast.AST) -> List[Tuple[str, str, str]]:
        """
        Extrae imports como (módulo_resuelto, alias_local, nombre_real).

        ✅ v3.9.5: lookup por nombre_real (no alias) — fix bug "as X".
        ✅ v3.9.6 FIX: Soporte para imports relativos (from . import X).
           Antes, la condición `and node.module:` descartaba los imports relativos
           porque node.module es None cuando no hay nombre tras `from`. Esto hacía
           que módulos como routes_hwfit.py (importados en server.py con
           `from . import routes_hwfit`) quedaran [⚠️ AISLADO] pese a estar
           perfectamente conectados al sistema.
        """
        imports: List[Tuple[str, str, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = self._resolve_module_path(alias.name)
                    if resolved:
                        local_alias = alias.asname or alias.name
                        imports.append((resolved, local_alias, alias.name))
            elif isinstance(node, ast.ImportFrom):
                # ✅ v3.9.6 FIX: Imports relativos (node.level > 0)
                if node.level and node.level > 0:
                    for alias in node.names:
                        resolved = self._resolve_relative_import(
                            rel_path, node.level, node.module, alias.name
                        )
                        if resolved:
                            local_alias = alias.asname or alias.name
                            imports.append((resolved, local_alias, alias.name))
                    continue

                # Imports absolutos (lógica original, sin el `and node.module:` bug)
                if not node.module:
                    continue
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    resolved = (
                        self._resolve_module_path(full)
                        or self._resolve_module_path(node.module)
                    )
                    if resolved:
                        local_alias = alias.asname or alias.name
                        imports.append((resolved, local_alias, alias.name))
        return imports

    def _extract_cross_calls(self, rel_path: str, tree: ast.AST) -> Dict[str, int]:
        """Cuenta llamadas a funciones/métodos de otros módulos."""
        calls: Dict[str, int] = defaultdict(int)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls[node.func.id] += 1
                elif isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        key = f"{node.func.value.id}.{node.func.attr}"
                        calls[key] += 1
                    else:
                        calls[node.func.attr] += 1
        return dict(calls)

    def _get_call_name(self, call_node: ast.Call) -> Optional[str]:
        """Helper para extraer el nombre de una llamada AST."""
        if isinstance(call_node.func, ast.Name):
            return call_node.func.id
        elif isinstance(call_node.func, ast.Attribute):
            return call_node.func.attr
        return None

    # ─── filtros ──────────────────────────────────────────────────

    def _is_noise(self, rel_path: str) -> bool:
        """True si el archivo debe excluirse del análisis de producción."""
        return any(pattern in rel_path for pattern in IGNORED_PATTERNS)

    def _is_valid_for_analysis(self, rel_path: str, symbol_name: str) -> bool:
        """Filtra archivos de ruido y enums internos de Pydantic."""
        if self._is_noise(rel_path):
            return False
        internal_enums = {'ModelField', 'PrivateAttr', 'ValidationError'}
        if symbol_name in internal_enums:
            return False
        return True

    # ─── rastreo de definiciones ──────────────────────────────────

    def _track_symbol_definitions_dynamic(
        self, target_symbols: Optional[Set[str]] = None
    ) -> None:
        """
        Indexa TODAS las clases/funciones relevantes dinámicamente.
        Si se provee target_symbols, restringe a esa lista.
        Después de registrar definiciones, ejecuta rastreo de herencia.
        """
        for rel_path, tree in self.file_trees.items():
            for node in ast.walk(tree):
                node_name: Optional[str] = None
                is_async = False

                if isinstance(node, ast.ClassDef):
                    node_name = node.name
                elif isinstance(node, ast.AsyncFunctionDef):
                    node_name = node.name
                    is_async = True
                elif isinstance(node, ast.FunctionDef):
                    node_name = node.name

                if not node_name:
                    continue
                if target_symbols and node_name not in target_symbols:
                    continue
                if not self._is_valid_for_analysis(rel_path, node_name):
                    continue

                if node_name not in self.symbol_traces:
                    self.symbol_traces[node_name] = SymbolTrace(
                        symbol_name=node_name,
                        defined_in=rel_path,
                        is_async=is_async,
                    )
                elif self.symbol_traces[node_name].defined_in is None:
                    self.symbol_traces[node_name].defined_in = rel_path
                    self.symbol_traces[node_name].is_async = is_async

        # Rastreo de herencia DESPUÉS de registrar todas las definiciones
        self._track_inheritance()

    def _track_inheritance(self) -> None:
        """
        Marca un símbolo como 'usado' si otras clases de PRODUCCIÓN heredan de él.

        ✅ FIX BUG C v3.9.0: Solo procesa archivos que NO están en IGNORED_PATTERNS.
        ✅ FIX FASE 4 v3.9.2: Ya no suma a instantiation_count. La herencia es
           una conexión estructural (usada en used_in), pero no es una instanciación
           en runtime. Sumarla generaba falsos [✅ CONECTADO] en clases base abstractas.
        ✅ FIX FASE 5 v3.9.3: La herencia interna suma a internal_uses.
        """
        for rel_path, tree in self.file_trees.items():
            # ✅ BUG C FIX: ignorar tests y outputs en herencia
            if self._is_noise(rel_path):
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    base_name: Optional[str] = None
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr

                    if base_name and base_name in self.symbol_traces:
                        trace = self.symbol_traces[base_name]
                        if rel_path != trace.defined_in:
                            if rel_path not in trace.used_in:
                                trace.used_in.append(rel_path)
                        else:
                            # ✅ v3.9.3: Herencia interna (ej. clase hija en el mismo archivo)
                            trace.internal_uses += 1

    # ─── construcción del grafo ───────────────────────────────────

    def _map_dependencies(self) -> None:
        """
        Construye grafo de dependencias y rastrea uso de símbolos.
        Ignora imports de archivos de ruido para no contaminar los traces.

        ✅ FIX v3.9.9 (2026-09-01): los archivos NOISE (herramientas
        standalone de tools/) SÍ contribuyen edges al module_graph — antes
        se descartaban por completo, así que un archivo no-noise importado
        SOLO desde una fuente noise (ej. tools/benchmark_suites.py importado
        por tools/benchmark_response.py, que está en IGNORED_PATTERNS) quedaba
        [⚠️ AISLADO] pese a estar conectado. Ahora: los edges se registran
        desde herramientas standalone (prueban conexión real de producción),
        pero NO desde tests/ (un módulo importado solo por un test no está
        integrado a producción) y los symbol_traces (used_in, call_sites)
        solo se contaminan desde fuentes no-noise.
        """
        for rel_path, tree in self.file_trees.items():
            is_noise = self._is_noise(rel_path)
            is_test_file = rel_path.startswith('tests/') or '/tests/' in rel_path
            imports = self._extract_imports(rel_path, tree)
            cross_calls = self._extract_cross_calls(rel_path, tree)

            for target, local_alias, real_name in imports:
                if target != rel_path:
                    # ✅ v3.9.9: registrar el edge desde herramientas
                    # standalone (tools/ noise) — prueba conexión real.
                    # Desde tests/ NO se registra: no es integración de
                    # producción, solo verificación.
                    if is_noise and is_test_file:
                        continue
                    edge = ModuleEdge(
                        source=rel_path,
                        target=target,
                        edge_type="import",
                        symbols=[local_alias],
                    )
                    self.module_graph[rel_path].append(edge)
                    # ✅ v3.9.9: No contaminar symbol_traces desde fuentes
                    # noise (tools/ standalone no prueban uso de producción),
                    # pero el EDGE ya quedó registrado en module_graph.
                    if is_noise:
                        continue
                    # ✅ v3.9.5: lookup por real_name, no por local_alias — ver
                    # docstring de _extract_imports (bug de alias con "as").
                    if real_name in self.symbol_traces:
                        trace = self.symbol_traces[real_name]
                        # ✅ FIX FASE 4 v3.9.2: Solo marcar como usado si es un uso externo real
                        if rel_path != trace.defined_in and rel_path not in trace.used_in:
                            trace.used_in.append(rel_path)

            # ✅ v3.9.9: cross_calls desde noise no contaminan symbol_traces.
            if is_noise:
                continue

            for called_name, count in cross_calls.items():
                parts = called_name.split('.')
                base = parts[0]
                attr = parts[-1]
                # ✅ v3.9.5 (BUG E, causa raíz real): "self" no es un símbolo —
                # self.metodo() generaba key="self.metodo" y base="self", que
                # nunca matchea nada en symbol_traces. Sin esto, NINGÚN método
                # llamado como self.X() (el patrón más común en código OOP)
                # sumaba a internal_uses/call_sites, sin importar el fix de
                # _evaluate_contracts() de más abajo — _check_model_fit()
                # llamado como self._check_model_fit() nunca se detectaba.
                lookup_name = attr if base == 'self' else base
                if lookup_name in self.symbol_traces:
                    trace = self.symbol_traces[lookup_name]
                    # ✅ FIX FASE 4 v3.9.2: Las llamadas internas no inflan call_sites
                    if rel_path != trace.defined_in:
                        if rel_path not in trace.used_in:
                            trace.used_in.append(rel_path)
                        trace.call_sites += count
                    else:
                        # ✅ v3.9.3: Llamadas internas suman a internal_uses
                        trace.internal_uses += count

    def _detect_instantiations(self) -> None:
        """
        Detecta instanciación directa (X = Y()), composición interna
        (self._core = IntentClassifierCore()), returns de factory y
        lanzamiento de excepciones (raise Y()).
        Solo procesa archivos de producción.

        ✅ v3.9.3: Añadido soporte para ast.Raise. Los usos internos suman a internal_uses.
        """
        for rel_path, tree in self.file_trees.items():
            if self._is_noise(rel_path):
                continue

            for node in ast.walk(tree):
                target_call: Optional[ast.Call] = None

                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    target_call = node.value
                elif isinstance(node, ast.Return) and node.value and isinstance(node.value, ast.Call):
                    target_call = node.value
                # ✅ v3.9.3: Detección de lanzamiento de excepciones
                elif isinstance(node, ast.Raise) and node.exc and isinstance(node.exc, ast.Call):
                    target_call = node.exc

                if target_call is None:
                    continue

                func_name = self._get_call_name(target_call)
                if func_name and func_name in self.symbol_traces:
                    trace = self.symbol_traces[func_name]
                    # ✅ FIX FASE 4 v3.9.2: Las instanciaciones internas no inflan el contador
                    if rel_path != trace.defined_in:
                        trace.instantiation_count += 1
                        if rel_path not in trace.used_in:
                            trace.used_in.append(rel_path)
                    else:
                        # ✅ v3.9.3: Instanciación interna (detalles de implementación)
                        trace.internal_uses += 1

    # ─── evaluación de contratos ──────────────────────────────────

    def _evaluate_contracts(self) -> List[FlowContract]:
        """
        Evalúa contratos de flujo conocidos y asigna badges.

        ✅ v3.9.5: connected ahora también acepta internal_uses > 0. Antes solo
        contaba instantiation_count/call_sites (uso EXTERNO). Los contratos en
        FLOW_CONTRACTS_CONFIG a veces incluyen métodos privados a propósito
        (ej. "_check_model_fit" en HardwareFit→ModelSwap) que por diseño SOLO
        se llaman desde dentro de su propio archivo — nunca iban a tener
        call_sites>0, así que el contrato quedaba [⚠️ PARCIAL] para siempre
        aunque la integración real funcionara. Mismo criterio "INTERNO" que ya
        usa get_integration_badge_for_symbol(), aplicado también acá.

        ✅ v3.9.8: FastAPI endpoints (registrados vía decoradores) no tienen
        call_sites, pero si su archivo host está importado en otro módulo del
        sistema (presente en _imported_files), el contrato se considera
        [✅ CONECTADO]. Fix específico para HardwareFit→AxiomaServer.
        """
        results = []
        for cfg in FLOW_CONTRACTS_CONFIG:
            components_found = [
                c for c in cfg["components"] if c in self.symbol_traces
            ]
            connected = [
                c for c in components_found
                if (
                    self.symbol_traces[c].instantiation_count > 0
                    or self.symbol_traces[c].call_sites > 0
                    or self.symbol_traces[c].internal_uses > 0
                    # ✅ v3.9.8: Fix FastAPI contracts
                    or (self.symbol_traces[c].defined_in in self._imported_files)
                )
            ]

            total = len(components_found)
            active = len(connected)

            if total == 0:
                status, badge = "UNKNOWN", "[❌ NO DETECTADO]"
            elif active == total:
                status, badge = "CONNECTED", "[✅ CONECTADO]"
            elif active > 0:
                status, badge = "PARTIAL", "[⚠️ PARCIAL]"
            else:
                status, badge = "ISOLATED", "[⚠️ AISLADO]"

            results.append(FlowContract(
                name=cfg["name"],
                components=cfg["components"],
                status=status,
                badge=badge,
                references=[self.symbol_traces[c].to_dict() for c in components_found],
            ))
        return results

    # ─── reporte final ────────────────────────────────────────────

    def _compute_connected_isolated(
        self, contracts: List[FlowContract]
    ) -> Tuple[List[str], List[str]]:
        """
        ✅ FIX BUG B v3.9.0: Calcula módulos conectados y aislados directamente
           desde symbol_traces, NO solo desde los contratos hardcodeados.
        ✅ FIX BUG G v3.9.1: Archivos __init__.py no se marcan como aislados.
        ✅ FIX v3.9.3: Un archivo está conectado si tiene símbolos con uso externo
           O si tiene símbolos con uso interno que dependen de un archivo conectado.
        ✅ FIX v3.9.7: Un archivo está conectado si es el target de una arista en
           module_graph (es decir, si alguien lo importa), aunque sus símbolos no
           tengan call_sites (típico en routers FastAPI registrados con include_router).
        """
        connected_files: Set[str] = set()
        isolated_files: Set[str] = set()

        # ✅ FIX v3.9.7: Conjunto de archivos que son targets en module_graph
        imported_files: Set[str] = set()
        for edges in self.module_graph.values():
            for edge in edges:
                imported_files.add(edge.target)

        # ✅ v0.6.9d (mejora 1): ENTRY-POINTS — archivos con guard
        # `if __name__ == "__main__":` se ejecutan directo (python tools/x.py,
        # Rafael.py) y por diseño nunca tienen import que los respalde — mismo
        # criterio que __init__.py y los lanzados por subprocess. Sin esto el
        # reporte marcaba como "aislados" todos los CLI tools (ruido).
        entrypoint_files: Set[str] = set()
        for rel, source in self.file_sources.items():
            if re.search(
                r'if\s+__name__\s*==\s*["\']__main__["\']\s*:',
                source,
            ):
                entrypoint_files.add(rel)

        for sym, trace in self.symbol_traces.items():
            if trace.defined_in is None:
                continue

            # ✅ BUG G FIX v3.9.1: __init__.py son manifiestos de exportación.
            if trace.defined_in.endswith('__init__.py'):
                connected_files.add(trace.defined_in)
                continue

            # ✅ v3.9.5: archivo lanzado como subprocess del OS (ver
            # _track_subprocess_launches) — conectado por diseño, nunca va a
            # tener un ast.Import que lo respalde.
            if trace.defined_in in self.subprocess_launched_files:
                connected_files.add(trace.defined_in)
                continue

            # ✅ v0.6.9d: entry-point con guard __main__ → conectado por diseño.
            if trace.defined_in in entrypoint_files:
                connected_files.add(trace.defined_in)
                continue

            # ✅ FIX v3.9.7: Si el archivo es importado por otro, está conectado.
            if trace.defined_in in imported_files:
                connected_files.add(trace.defined_in)
                continue

            # Usos reales fuera del archivo de definición
            external_uses = [
                u for u in trace.used_in if u != trace.defined_in
            ]
            has_external_use = (
                len(external_uses) > 0
                or trace.instantiation_count > 0
                or trace.call_sites > 0
            )

            if has_external_use:
                connected_files.add(trace.defined_in)
            else:
                isolated_files.add(trace.defined_in)

        # Un archivo con al menos UN símbolo conectado no es aislado
        truly_isolated = isolated_files - connected_files

        return sorted(connected_files), sorted(truly_isolated)

    def build_report(self, target_symbols: Optional[Set[str]] = None) -> IntegrationReport:
        """Ejecuta el pipeline completo y retorna el reporte de integración."""
        self._track_symbol_definitions_dynamic(target_symbols)
        self._map_dependencies()
        self._detect_instantiations()
        self._track_subprocess_launches()  # ✅ v3.9.5

        # ✅ v3.9.8: Pre-calcular imported_files para que _evaluate_contracts
        # pueda verificar si el archivo del símbolo está importado (fix endpoints FastAPI).
        self._imported_files: Set[str] = set()
        for edges in self.module_graph.values():
            for edge in edges:
                self._imported_files.add(edge.target)

        contracts = self._evaluate_contracts()
        connected_modules, isolated_modules = self._compute_connected_isolated(contracts)

        return IntegrationReport(
            module_graph=dict(self.module_graph),
            symbol_traces=self.symbol_traces,
            flow_contracts=contracts,
            isolated_modules=isolated_modules,
            connected_modules=connected_modules,
        )


# ==================== UTILIDADES DE INTEGRACIÓN ====================

def map_project_flows(
    project_root: Path,
    target_files: List[Path],
    target_symbols: Optional[Set[str]] = None,
) -> IntegrationReport:
    """Función de entrada principal para el FlowMapper v3.9.8."""
    mapper = FlowMapper(project_root)
    mapper.load_project_files(target_files)
    return mapper.build_report(target_symbols)


def get_integration_badge_for_symbol(symbol: str, report: IntegrationReport, mapper: Optional["FlowMapper"] = None) -> str:
    """
    Retorna badge de integración para un símbolo dado.

    ✅ FIX BUG A v3.9.0: Eliminada la "herencia de conexión por archivo".
    ✅ FIX v3.9.3: Componentes internos de módulos conectados ya no son aislados.
    ✅ v3.9.5: Symbols en archivos lanzados por subprocess (ver mapper.subprocess_launched_files)
       ya no son aislados — mismo criterio que __init__.py. Parámetro `mapper` opcional
       por compatibilidad hacia atrás (llamadas existentes sin este dato siguen andando,
       solo pierden este único criterio adicional).
    ✅ v3.9.7: Si el archivo anfitrión está en connected_modules, el símbolo se considera
       conectado automáticamente (fix para routers FastAPI y patrones similares).

    Criterios actuales (ordenados por fuerza):
      1. FUERTE     — instanciación > 0 O call_sites > 0 → [✅ CONECTADO]
      2. MEDIO      — al menos un import externo al archivo de definición → [✅ CONECTADO]
      3. MANIFIESTO — definido en __init__.py → [✅ CONECTADO] (lazy loading)
      4. SUBPROCESS — archivo lanzado como proceso del OS → [✅ CONECTADO]
      5. INTERNO    — tiene internal_uses > 0 y su archivo está en connected_modules → [✅ CONECTADO]
      6. GRAFO      — su archivo anfitrión está en connected_modules → [✅ CONECTADO]
      7. NINGUNO    — sin referencias externas ni internas → [⚠️ AISLADO]
    """
    if symbol not in report.symbol_traces:
        return "[⚠️ AISLADO]"

    trace = report.symbol_traces[symbol]

    # ✅ v3.9.1: __init__.py son manifiestos de exportación, siempre conectados
    if trace.defined_in and trace.defined_in.endswith('__init__.py'):
        return "[✅ CONECTADO]"

    # ✅ v3.9.5: archivo lanzado como subprocess del OS
    if mapper is not None and trace.defined_in in mapper.subprocess_launched_files:
        return "[✅ CONECTADO]"

    # 1. Conexión fuerte: instanciada o llamada desde otro módulo
    if trace.instantiation_count > 0 or trace.call_sites > 0:
        return "[✅ CONECTADO]"

    # 2. Conexión por import: importada en al menos un módulo externo
    external_uses = [u for u in trace.used_in if u != trace.defined_in]
    if external_uses:
        return "[✅ CONECTADO]"

    # 3. Conexión por implementación interna: Si el símbolo es un detalle de
    #    implementación (ej. Palace, AgentConfig, CriticalSecurityFlawDetected)
    #    y su archivo anfitrión está conectado al sistema, entonces el símbolo
    #    también está conectado (es parte de la maquinaria de ese módulo).
    if trace.internal_uses > 0 and trace.defined_in in report.connected_modules:
        return "[✅ CONECTADO]"

    # ✅ v3.9.7: Si el archivo anfitrión está conectado al sistema, el símbolo
    # también lo está, aunque no tenga call_sites ni internal_uses (típico en
    # routers FastAPI donde las funciones son registradas vía include_router).
    if trace.defined_in in report.connected_modules:
        return "[✅ CONECTADO]"

    # 4. Sin referencias externas ni internas verificadas
    return "[⚠️ AISLADO]"


# ==================== EJECUCIÓN ====================
if __name__ == "__main__":
    print("Módulo de mapeo de flujos cruzados. Use map_project_flows() para analizar.")
    print("Versión: v3.9.8 — FASE 9.1: FastAPI Contracts support")
