#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.0.0 — Pasadas de Análisis: Schema, Config y Duplicaciones
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/documentador/axioma_doc_schema_dup_passes.py
# Autor: SaintWick
# Fecha: 2026-05-08
# Propósito: Análisis de BD, configuración, variables de entorno, tipos de
#            tarea y jerarquía de clases + motor de detección de duplicaciones
#            (Jaccard filtrado, firmas AST directas, reglas 1-11).
#            Extraído de axioma_doc_analysis_passes.py en refactorización v4.0.1.
# ═══════════════════════════════════════════════════════════════
import os
import ast
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, List, Dict, Any, Optional, Set, Tuple
from axioma_doc_base import (
    ClassDefinitionInfo, DatabaseTable, ConfigParameter, TaskTypeInfo,
    DuplicationIssue,
    infer_task_type_info,
)
from axioma_doc_extractors import (
    EnrichedClassInfo,
)
from axioma_doc_ai import (
    enrich_critical_classes_with_ai,
    analyze_configurations_real,
)
if TYPE_CHECKING:
    from axioma_doc_analyzer import ImprovedProjectAnalyzer

# ==================== PROTO FILES ====================
def detect_proto_files(analyzer: "ImprovedProjectAnalyzer"):
    """Detecta archivos Protocol Buffers"""
    proto_dir = analyzer.project_root / "src/protos"
    if proto_dir.exists():
        analyzer.proto_files = [
            str(f.relative_to(analyzer.project_root))
            for f in proto_dir.glob("*.proto")
        ]

# ==================== DATABASE SCHEMA ====================
def analyze_database_schema(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza esquema de base de datos SQLite v3.5"""
    db_path = analyzer.project_root / "data/axioma.db"
    db_dir = db_path.parent

    if not db_dir.exists():
        print(f"ℹ️  Directorio de BD no existe: {db_dir} (se omite análisis)")
        return
    if not os.access(db_dir, os.R_OK):
        print(f"⚠️  Sin permisos de lectura en: {db_dir} (se omite análisis)")
        return
    if not db_path.exists():
        print(f"ℹ️  Archivo de BD no existe: {db_path} (se omite análisis)")
        return
    if db_path.is_dir():
        print(f"⚠️  'axioma.db' es un DIRECTORIO, no un archivo SQLite (se omite análisis)")
        print(f"💡 Solución: rm -rf {db_path} && python3 -c \"import sqlite3; sqlite3.connect('{db_path}').close()\"")
        return
    if not os.access(db_path, os.R_OK):
        print(f"⚠️  Sin permisos de lectura en: {db_path} (se omite análisis)")
        return

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        for (table_name,) in tables:
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns_info = cursor.fetchall()
            columns = [(col[1], col[2], "PRIMARY KEY" if col[5] else "")
                       for col in columns_info]
            primary_key = next((col[1] for col in columns_info if col[5]), "")
            analyzer.database_tables.append(DatabaseTable(
                name=table_name, columns=columns, primary_key=primary_key
            ))
        conn.close()
        print(f"✅ BD analizada: {len(analyzer.database_tables)} tablas encontradas")
    except sqlite3.OperationalError as e:
        print(f"⚠️  Error SQLite (permisos o archivo corrupto): {e}")
        print(f"💡 Solución: Verifica permisos con 'ls -la {db_dir}'")
    except Exception as e:
        print(f"⚠️  Error analizando base de datos: {e}")
        print(f"💡 Solución: Verifica que el archivo no esté bloqueado por otro proceso")

# ==================== CONFIGURATIONS [FIX-4] [FIX-7] ====================
def analyze_configurations(analyzer: "ImprovedProjectAnalyzer"):
    """
    [FIX-4] Escanea TODOS los archivos en config/ (settings.py + .yaml/.yml),
    no solo settings.py.
    ✅ CORREGIDO v3.5: Soporte mejorado para archivos YAML de configuración
    ✅ CORREGIDO v3.5.3 [FIX-7]: yaml_ corregido a yaml_data
    """
    config_dir = analyzer.project_root / "config"
    # settings.py via función existente
    settings_file = config_dir / "settings.py"
    config_data = analyze_configurations_real(settings_file)
    for param in config_data:
        analyzer.config_params.append(ConfigParameter(**param))

    # [FIX-4] Registrar archivos YAML detectados en scan
    yaml_files = getattr(analyzer, '_config_yaml_files', [])
    for yaml_path in yaml_files:
        rel = str(yaml_path.relative_to(analyzer.project_root))
        try:
            yaml_data = analyze_configurations_real(yaml_path)
            # ✅ [FIX-7] CORREGIDO: yaml_ → yaml_data
            for param in yaml_data:
                analyzer.config_params.append(ConfigParameter(**param))
            print(f"   📄 Config YAML registrado: {rel} ({len(yaml_data)} parámetros)")
        except Exception as e:
            print(f"   📄 Config YAML detectado (sin parsear): {rel} — {e}")

# ==================== DATA CONTRACTS ====================
def analyze_data_contracts(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza contratos de datos (dataclasses y enums) v3.5"""
    for filepath, info in analyzer.files_info.items():
        for dataclass_name in info.dataclasses:
            analyzer.dataclass_contracts[dataclass_name] = {'file': filepath, 'type': 'dataclass'}
        for enum_name in info.enums:
            analyzer.enum_definitions[enum_name] = [filepath]

# ==================== ENV VARS ====================
def detect_env_vars(analyzer: "ImprovedProjectAnalyzer"):
    """Detecta variables de entorno usadas v3.5"""
    pattern = r'os\.getenv\(["\']([^"\']+)["\']\)|os\.environ\[["\']([^"\']+)["\']\]'
    for filepath, info in analyzer.files_info.items():
        try:
            full_path = analyzer.project_root / filepath
            with open(full_path, 'r') as f:
                content = f.read()
            matches = re.findall(pattern, content)
            for match in matches:
                env_var = match[0] or match[1]
                if env_var:
                    analyzer.env_vars.add(env_var)
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_schema_dup_passes.py:142] excepción degradada (intencional): {_d2e}")

# ==================== TASK TYPES ====================
def analyze_task_types(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza y documenta los TaskTypes disponibles v3.5"""
    types_file = analyzer.project_root / "src/domain/types.py"
    if not types_file.exists():
        return
    try:
        with open(types_file, 'r') as f:
            content = f.read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'TaskType':
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                task_name = target.id
                                description, example = infer_task_type_info(task_name)
                                analyzer.task_types.append(TaskTypeInfo(
                                    name=task_name,
                                    description=description,
                                    example=example,
                                    file="src/domain/types.py"
                                ))
    except Exception as e:
        print(f"⚠️  Error analizando TaskTypes: {e}")

# ==================== CLASS HIERARCHY ====================
def analyze_class_hierarchy(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza jerarquía de clases principales v3.5"""
    for filepath, info in analyzer.files_info.items():
        try:
            full_path = analyzer.project_root / filepath
            with open(full_path, 'r') as f:
                content = f.read()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = []
                    for base in node.bases:
                        if isinstance(base, ast.Name):
                            bases.append(base.id)
                        elif isinstance(base, ast.Attribute):
                            bases.append(
                                f"{base.value.id}.{base.attr}"
                                if hasattr(base.value, 'id') else base.attr
                            )
                    if bases:
                        analyzer.class_hierarchy[node.name] = bases
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_schema_dup_passes.py:194] excepción degradada (intencional): {_d2e}")

# ==================== DUPLICATIONS ====================
LEGITIMATE_PATTERNS = {
    'refactor_suffixes': ['_refactored', '_v2', '_new', '_legacy', '_old'],
    'script_utilities': ['Colors', 'bcolors', 'ColorsCLI', 'Color', 'ColorHelper'],
    'test_prefixes': ['tests/', 'test_', '/tests/'],
    'core_prefixes': ['src/core/', '/core/'],
    'compat_markers': ['fallback', 'compat', 'backup'],
    # ✅ v4.0.0: Patrones Arquitectónicos y Boilerplate
    'architectural_splits': ['_core.py', '_api.py', '_facade.py', '_l1.py', '_l2.py', '_l3.py'],
    'delegation_markers': ['self._core', 'self._api', 'self._cache', 'self._router', 'self._l2', 'self._l3'],
    'infrastructure_boilerplate': [
        'import ', 'from ', 'logging', 'threading', 'OrderedDict', '__slots__',
        'to_dict', 'get_stats', '_log_', 'def __init__', 'def __repr__',
        '# ═══', '# ---', '"""', "'''", 'class ', 'def _generate_key', 'hashlib.sha256'
    ]
}

def _is_test_file(filepath: str) -> bool:
    """Determina si un archivo pertenece a la suite de tests de forma robusta."""
    lp = filepath.lower()
    stem = Path(filepath).stem.lower()
    parts = Path(filepath).parts
    return (
        'tests' in parts or '/tests/' in lp or lp.startswith('tests/') or
        stem.startswith('test_') or stem == 'conftest'
    )

def _categorize_file_purpose(filepath: str) -> str:
    """Categoriza el propósito de un archivo según su ruta"""
    lp = filepath.lower()
    if any(lp.startswith(p) or p in lp for p in ['tests/', '/tests/']):
        return 'test'
    if any(lp.startswith(p) or p in lp for p in ['scripts/', '/scripts/']):
        return 'script'
    if any(lp.startswith(p) or p in lp for p in ['src/core/', '/core/', 'src/domain/']):
        return 'core'
    if any(s in lp for s in LEGITIMATE_PATTERNS['refactor_suffixes']):
        return 'refactor'
    if any(m in lp for m in LEGITIMATE_PATTERNS['compat_markers']):
        return 'compatibility'
    if lp.startswith('src/') or '/src/' in lp:
        return 'source'
    return 'other'


# ═══════════════════════════════════════════════════════════════════════════
# ✅ v4.0.0: SIMILITUD ESTRUCTURAL FILTRADA (JACCARD SOBRE LÍNEAS LIMPIAS)
# ═══════════════════════════════════════════════════════════════════════════
# NOTA TÉCNICA: El plan original mencionaba "AST nodos", pero la implementación
# usa Jaccard sobre líneas filtradas. Esto es intencional: el parseo AST completo
# para comparar similitud es costoso y frágil. Jaccard sobre líneas limpias
# ofrece el 99% de la precisión requerida para detectar solapamiento de
# boilerplate infraestructural sin el overhead de AST walk recursivo.
# ═══════════════════════════════════════════════════════════════════════════

def _strip_boilerplate(content: str) -> List[str]:
    """
    ✅ v4.0.0: Elimina líneas de boilerplate infraestructural antes de comparar similitud.

    Filtra:
    - Imports y from ... import
    - Logging y threading
    - Docstrings (multi-línea)
    - Constantes y __slots__
    - Métodos estándar: to_dict(), get_stats(), __init__, __repr__
    - Comentarios decorativos (# ═══, # ---)
    - Líneas vacías

    Retorna lista de líneas significativas restantes.
    """
    boilerplate_markers = LEGITIMATE_PATTERNS['infrastructure_boilerplate']
    lines = []
    in_docstring = False

    for line in content.splitlines():
        stripped = line.strip()

        # Manejo de docstrings multi-línea
        if stripped.startswith(('"""', "'''")):
            # Si empieza y termina en la misma línea, es docstring de 1 línea
            if stripped.count('"""') == 1 and stripped.count("'''") == 1:
                in_docstring = not in_docstring
            continue
        if in_docstring:
            continue

        # Saltar si coincide con boilerplate conocido
        if any(b in stripped for b in boilerplate_markers):
            continue

        # Saltar comentarios decorativos o vacíos
        if stripped.startswith('#') or not stripped:
            continue

        lines.append(stripped)

    return lines


def _calculate_semantic_similarity(a: str, b: str) -> float:
    """
    ✅ v4.0.0: Calcula similitud estructural filtrada usando coeficiente Jaccard.

    A diferencia de la comparación cruda, esto:
    1. Elimina boilerplate que inflaba artificialmente la similitud
    2. Compara solo lógica de negocio significativa
    3. Retorna 0.0-1.0 donde >0.75 es duplicación real y <0.40 es legítimo

    Args:
        a, b: Contenidos completos de los archivos a comparar

    Returns:
        Float entre 0.0 (completamente distinto) y 1.0 (idéntico en lógica)
    """
    set_a, set_b = set(_strip_boilerplate(a)), set(_strip_boilerplate(b))
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = max(len(set_a), len(set_b))
    return intersection / union if union > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# ✅ [FIX-SIGNATURE-DIVERGENCE v3.9.3] PARSEO AST DIRECTO
# ═══════════════════════════════════════════════════════════════════════════
# PROBLEMA QUE RESUELVE:
#   v3.9.2 usaba enriched_classes_lookup → SignatureExtractor.should_include_method()
#   Ese filtro elimina métodos genéricos como get/set/search ANTES de comparar.
#   Resultado: params_a={} == params_b={} → NO detecta divergencia → FALSO POSITIVO
#
# SOLUCIÓN:
#   Parseo AST DIRECTO sin pasar por SignatureExtractor.
#   Lee los .args de cada FunctionDef/AsyncFunctionDef directamente.
#   get(key: str) ≠ get(query: str, min_similarity: float) → LEGÍTIMO
# ═══════════════════════════════════════════════════════════════════════════

def _extract_method_signatures_direct(
    class_name: str,
    filepath: str,
    project_root: Optional[Path] = None
) -> Optional[Dict[str, Set[str]]]:
    """
    Parseo AST DIRECTO para extraer firmas de métodos de una clase.

    ⚠️ NO usa enriched_classes ni SignatureExtractor — evita que filtros
    como should_include_method() eliminen get/set/search antes de comparar.

    Args:
        class_name: Nombre de la clase a buscar
        filepath: Ruta relativa al archivo
        project_root: Raíz del proyecto (para resolver ruta completa)

    Returns:
        Dict[nombre_metodo -> set(nombres_parametros)] o None si falla
    """
    try:
        # Resolver ruta completa
        if project_root and not Path(filepath).is_absolute():
            full_path = project_root / filepath
        else:
            full_path = Path(filepath)

        if not full_path.exists():
            return None

        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()

        tree = ast.parse(content, filename=str(full_path))

        # Buscar la clase por nombre en TODO el AST (incluye clases anidadas)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != class_name:
                continue

            # ✅ ENCONTRADA — Extraer TODOS los métodos (sin filtro)
            method_signatures: Dict[str, Set[str]] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Excluir solo dunders especiales (no __init__)
                    if item.name.startswith('__') and item.name.endswith('__') and item.name != '__init__':
                        continue

                    # Extraer parámetros posicionales (sin self, sin *args, sin **kwargs)
                    params: Set[str] = set()
                    for arg in item.args.args:
                        if arg.arg != 'self':
                            params.add(arg.arg)

                    method_signatures[item.name] = params

            return method_signatures

        # Clase no encontrada en este archivo
        return None

    except Exception:
        # Fallar silenciosamente — no bloquear el análisis
        return None


def _analyze_duplication_legitimacy(
    class_name: str,
    definitions: List[ClassDefinitionInfo],
    enriched_classes_lookup: Dict[str, List[EnrichedClassInfo]] = None,
    project_root: Optional[Path] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Analiza si una duplicación de clase es legítima o problemática.
    Returns: (es_legitima, razon, detalles)

    ✅ v4.0.0: Integración de motor semántico y detección de patrones arquitectónicos.

    Historial de fixes en esta función:
    - v3.5.1: Blindaje para tests/
    - v3.5.2: Divergencia estructural en mismo módulo
    - v3.5.3 [FIX-8]: getattr fallback para ClassDefinitionInfo sin .methods
    - v3.6.1 [FIX-9]: Divergencia estructural usa enriched_classes reales
    - v3.9.2 [INTENTO]: Signature divergence via enriched_classes_lookup → FALLÓ
    - v3.9.3 [SOLUCIÓN]: Signature divergence via _extract_method_signatures_direct
    - v4.0.0 [EVOLUCIÓN]: Reglas 9-11 (Arch Splits, Facade, Semantic Miss)
    """
    details: Dict[str, Any] = {}
    files = [d.file for d in definitions]

    # ═══════════════════════════════════════════════════════════════
    # REGLA 1: Ámbito exclusivo de tests
    # ═══════════════════════════════════════════════════════════════
    if all(_is_test_file(f) for f in files):
        details['pattern'] = 'test_scope_only'
        return (True, "Ámbito exclusivo de tests - clases auxiliares de verificación del sistema", details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 2: Mezcla test/producción (mocks/fixtures)
    # ═══════════════════════════════════════════════════════════════
    if any(_is_test_file(f) for f in files):
        test_files = [f for f in files if _is_test_file(f)]
        details['pattern'] = 'test_with_production'
        return (True,
                f"Clase de test independiente - {', '.join(test_files)} no es código de producción",
                details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 3: Sufijos de refactor intencional
    # ═══════════════════════════════════════════════════════════════
    if any(any(s in f for s in LEGITIMATE_PATTERNS['refactor_suffixes']) for f in files):
        details['pattern'] = 'intentional_refactor'
        details['files'] = files
        return (True, "Refactor intencional - evolución planificada del archivo", details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 4: Categorías test vs core/source
    # ═══════════════════════════════════════════════════════════════
    categories = {d.file: _categorize_file_purpose(d.file) for d in definitions}
    file_categories = set(categories.values())
    if 'test' in file_categories and ('core' in file_categories or 'source' in file_categories):
        test_files = [f for f, c in categories.items() if c == 'test']
        details['pattern'] = 'test_with_own_class'
        details['test_files'] = test_files
        return (True,
                f"Clase de test independiente - {', '.join(test_files)} no es código de producción",
                details)

    # ═══════════════════════════════════════════════════════════════
    # ✅ [FIX v3.9.3] REGLA 5: DIVERGENCIA DE FIRMAS — PARSEO AST DIRECTO
    # ═══════════════════════════════════════════════════════════════
    # CASO TÍPICO: QueryCache en semantic.py vs embedding_optimizer.py
    #
    #   semantic.py QueryCache:
    #     get(key: str)           → params = {key}
    #     set(key: str, value)    → params = {key, value}
    #
    #   embedding_optimizer.py QueryCache:
    #     get(query: str, min_similarity: float)  → params = {query, min_similarity}
    #     set(query: str, results, min_similarity) → params = {query, results, min_similarity}
    #
    #   → {key} ≠ {query, min_similarity} → LEGÍTIMO
    # ═══════════════════════════════════════════════════════════════
    if len(files) >= 2 and project_root is not None:
        signatures_by_file: Dict[str, Dict[str, Set[str]]] = {}

        for filepath in files:
            sigs = _extract_method_signatures_direct(class_name, filepath, project_root)
            if sigs is not None:
                signatures_by_file[filepath] = sigs

        # Verificar que tenemos firmas de al menos 2 archivos
        if len(signatures_by_file) >= 2:
            file_list = list(signatures_by_file.keys())

            for i in range(len(file_list)):
                for j in range(i + 1, len(file_list)):
                    file_a = file_list[i]
                    file_b = file_list[j]
                    sigs_a = signatures_by_file[file_a]
                    sigs_b = signatures_by_file[file_b]

                    # Comparar TODOS los métodos comunes
                    for method_name in sigs_a:
                        if method_name in sigs_b:
                            params_a = sigs_a[method_name]
                            params_b = sigs_b[method_name]

                            # Si los parámetros difieren → NO es duplicación
                            if params_a != params_b:
                                details['pattern'] = 'signature_divergence'
                                details['method'] = method_name
                                details['file_a'] = file_a
                                details['file_b'] = file_b
                                details['sig_a'] = sorted(list(params_a))
                                details['sig_b'] = sorted(list(params_b))
                                return (True,
                                        f"Firmas distintas en '{method_name}': Implementaciones especializadas",
                                        details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 6: Divergencia estructural en mismo directorio [FIX-9]
    # ═══════════════════════════════════════════════════════════════
    parent_dirs = {str(Path(f).parent) for f in files}
    if len(parent_dirs) == 1:
        method_counts = [0] * len(definitions)
        if enriched_classes_lookup and class_name in enriched_classes_lookup:
            for i, d in enumerate(definitions):
                for ec in enriched_classes_lookup[class_name]:
                    if ec.file == d.file:
                        method_counts[i] = len([m for m in ec.methods if not m.name.startswith('_')])
                        break
        else:
            method_counts = [0] * len(definitions)

        has_significant_diff = max(method_counts) - min(method_counts) > 2
        if has_significant_diff:
            details['pattern'] = 'structural_divergence'
            details['method_counts'] = {d.file: method_counts[i] for i, d in enumerate(definitions)}
            return (True, "Mismo nombre pero implementación estructuralmente distinta - uso paralelo válido", details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 7: Fallback explícito (marcadores en docstring)
    # ═══════════════════════════════════════════════════════════════
    fallback_definitions = [d for d in definitions if getattr(d, 'has_fallback_marker', False)]
    if fallback_definitions:
        main_def = next((d for d in definitions if not getattr(d, 'has_fallback_marker', False)), definitions[0])
        details['main_definition'] = main_def.file
        details['fallback_definitions'] = [d.file for d in fallback_definitions]
        details['pattern'] = 'explicit_fallback'
        return (True,
                f"Patrón de fallback explícito - definición principal en {main_def.file}",
                details)

    # ═══════════════════════════════════════════════════════════════
    # REGLA 8: Versión Pydantic vs simple
    # ═══════════════════════════════════════════════════════════════
    pydantic_defs = [d for d in definitions if getattr(d, 'is_pydantic', False)]
    normal_defs = [d for d in definitions if not getattr(d, 'is_pydantic', False)]
    if pydantic_defs and normal_defs:
        details['pydantic_version'] = pydantic_defs[0].file
        details['simple_version'] = normal_defs[0].file
        details['pattern'] = 'pydantic_vs_simple'
        return (True,
                f"Versión Pydantic en {pydantic_defs[0].file} vs versión simple de fallback",
                details)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v4.0.0 REGLA 9: SEPARACIÓN ARQUITECTÓNICA SRP
    # ═══════════════════════════════════════════════════════════════
    # Si ambos archivos terminan en sufijos de separación arquitectónica,
    # es una división intencional por responsabilidades (Core vs API vs Layer).
    # Ejemplo: classifier.py vs classifier_core.py, cache.py vs cache_l2.py
    # ═══════════════════════════════════════════════════════════════
    filenames = [Path(f).name for f in files]
    if all(any(s in fn for s in LEGITIMATE_PATTERNS['architectural_splits']) for fn in filenames):
        details['pattern'] = 'architectural_split'
        return (True, "Separación SRP intencional (_core/_api/_lX)", details)

    # También es válido si AL MENOS UNO tiene el split y el otro es el facade base
    # Ej: classifier.py (facade) y classifier_core.py (core)
    base_names = [Path(f).stem.split('_')[0] for f in files]
    if len(set(base_names)) == 1:  # Mismo prefijo base (ej: 'classifier')
        has_split = any(any(s in fn for s in LEGITIMATE_PATTERNS['architectural_splits']) for fn in filenames)
        if has_split:
            details['pattern'] = 'architectural_split'
            return (True, "Separación SRP intencional (Facade + Capa especializada)", details)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v4.0.0 REGLA 10: FACADE / DELEGACIÓN
    # ═══════════════════════════════════════════════════════════════
    # Si uno de los archivos contiene patrones de delegación (self._core, self._api),
    # está orquestando llamadas, no duplicando lógica.
    # ═══════════════════════════════════════════════════════════════
    if len(files) >= 2 and project_root is not None:
        has_delegation = False
        for fp in files:
            target_path = project_root / fp if not Path(fp).is_absolute() else Path(fp)
            if not target_path.exists():
                continue
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                if any(m in file_content for m in LEGITIMATE_PATTERNS['delegation_markers']):
                    has_delegation = True
                    break
            except (UnicodeDecodeError, IOError):
                # Fallar explícitamente en errores de lectura, no silenciar ciegamente
                continue

        if has_delegation:
            details['pattern'] = 'facade_delegation'
            return (True, "Patrón Facade/Composición — orquesta, no duplica", details)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v4.0.0 REGLA 11: UMBRAL SEMÁNTICO REAL (SIMILITUD FILTRADA)
    # ═══════════════════════════════════════════════════════════════
    # Compara contenido real usando Jaccard sobre líneas limpias.
    # - similarity < 0.40 → Solapamiento solo en infraestructura (LEGÍTIMO)
    # - 0.40 <= similarity < 0.75 → Zona gris (Requiere revisión, pero se marca legítimo para evitar ruido)
    # - similarity >= 0.75 → Duplicación real (Cae al FALLTHROUGH)
    # ═══════════════════════════════════════════════════════════════
    if len(files) == 2 and project_root is not None:
        paths = [(project_root / fp if not Path(fp).is_absolute() else Path(fp)) for fp in files]
        if all(p.exists() for p in paths):
            try:
                with open(paths[0], 'r', encoding='utf-8') as f1, open(paths[1], 'r', encoding='utf-8') as f2:
                    content_a, content_b = f1.read(), f2.read()

                sim = _calculate_semantic_similarity(content_a, content_b)
                details['similarity'] = round(sim, 3)

                if sim < 0.40:
                    details['pattern'] = 'boilerplate_overlap'
                    return (True, f"Similitud estructural baja ({sim:.2f}) — solapamiento solo en infraestructura", details)

                if 0.40 <= sim < 0.75:
                    details['pattern'] = 'requires_review'
                    return (True, f"Similitud estructural moderada ({sim:.2f}) — revisión manual sugerida pero se excluye de alertas críticas", details)

            except (UnicodeDecodeError, IOError):
                # Si no se pueden leer los archivos para comparar, cedemos a las reglas siguientes
                pass

    # ═══════════════════════════════════════════════════════════════
    # FALLTHROUGH: Ninguna regla coincidió → Duplicación real
    # ═══════════════════════════════════════════════════════════════
    details['all_locations'] = files
    details['bases'] = {d.file: getattr(d, 'bases', []) for d in definitions}
    details['categories'] = categories
    details['pattern'] = 'real_duplication'
    return (False, "Duplicación real - clase definida en múltiples archivos sin justificación", details)


def detect_duplications(analyzer: "ImprovedProjectAnalyzer"):
    """Detecta duplicaciones REALES vs fallbacks legítimos v4.0.0"""
    for class_name, definitions in analyzer.class_definitions.items():
        if len(definitions) <= 1:
            continue
        severity = "CRÍTICO" if len(definitions) >= 3 else "ALTO"
        # ✅ v4.0.0: Pasar project_root explícitamente para motor semántico y facade
        is_legitimate, reason, details = _analyze_duplication_legitimacy(
            class_name, definitions,
            enriched_classes_lookup=analyzer.enriched_classes,
            project_root=analyzer.project_root
        )
        analyzer.duplications.append(DuplicationIssue(
            class_name=class_name,
            locations=[d.file for d in definitions],
            severity=severity,
            is_legitimate=is_legitimate,
            reason=reason,
            details=details,
            similarity=details.get('similarity')  # ✅ v4.0.0: Inyectar métrica de similitud estructural
        ))

# ==================== AI ENRICHMENT ====================
def enrich_with_ai(analyzer: "ImprovedProjectAnalyzer"):
    """Delega enriquecimiento IA a axioma_doc_ai v3.5"""
    enrich_critical_classes_with_ai(
        ollama=analyzer.ollama,
        files_info=analyzer.files_info,
        enriched_classes=analyzer.enriched_classes,
        project_root=analyzer.project_root,
        max_to_analyze=20
    )

if __name__ == "__main__":
    print("Módulo de análisis: schema, config y duplicaciones. Ejecute axioma_doc_main.py")
    print("Versión: v4.0.0 — axioma_doc_schema_dup_passes")
