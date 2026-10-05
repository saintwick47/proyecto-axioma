#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.1.1 — Pasadas de Análisis: Escaneo, AST y Duplicaciones
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/documentador/axioma_doc_analysis_passes.py
# Autor: SaintWick
# Fecha: 2026-06-24
# Propósito: Contiene métodos de escaneo recursivo, análisis de AST,
#            detección de dependencias, flujos críticos y duplicaciones.
#            Integrado en ImprovedProjectAnalyzer.
# ═══════════════════════════════════════════════════════════════
"""
axioma_doc_analysis_passes.py - Pasadas de análisis del documentador de AXIOMA.

Este módulo agrupa las funciones de análisis que se ejecutan como pasadas
sobre el proyecto: escaneo de archivos, extracción de información AST,
detección de duplicaciones, análisis de dependencias, flujos críticos,
sistemas de memoria, etc. Es utilizado por ImprovedProjectAnalyzer para
construir el modelo completo del proyecto.

Funcionalidades principales:
- Escaneo recursivo de archivos Python y YAML, con exclusión de ruido y
  archivos sensibles (credenciales).
- Análisis de archivos individuales: AST, extracción de clases, funciones,
  imports, constantes, propiedades lazy, campos Pydantic, tags inline.
- Inferencia de propósito de archivos mediante múltiples estrategias
  (comentarios de header, docstrings, mapa de nombres).
- Extracción de clases enriquecidas con métodos, atributos, validaciones.
- Análisis de dependencias internas y externas.
- Construcción de flujos críticos del sistema.
- Detección de duplicaciones con filtrado de falsos positivos (semántico,
  patrones arquitectónicos).
- Análisis de sistemas de memoria, esquemas de base de datos y configuraciones.
"""
import os
import ast
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict
from axioma_doc_base import (
    FileInfo, ClassDefinitionInfo, AIModelInfo, MemorySystemInfo,
    DatabaseTable, ConfigParameter, TaskTypeInfo, CriticalFlow,
    EXCLUSIONES, EXCLUDED_PATTERNS, CRITICAL_FLOWS_DEFINITION,
    categorize_file, extract_module_docstring, detect_recent_changes,
    detect_ollama_models, get_directory_size, get_file_size,
    infer_task_type_info,
    DEPENDENCY_CONFIG, CRITICAL_FLOWS_CONFIG,
    is_file_excluded,  # ✅ [FIX-11] Usar la función centralizada de axioma_doc_base_utils
)
from axioma_doc_extractors import (
    EnrichedClassInfo, FunctionSignature, SignatureExtractor,
    extract_classes_defined_only, extract_bases,
    extract_function_signatures, extract_functions,
    extract_imports, extract_dataclasses, extract_enums,
    # ✅ FIX 1-5: Nuevas funciones de extracción
    extract_pydantic_fields, detect_lazy_properties,
    extract_enhanced_inline_tags, extract_module_constants,
)
from axioma_doc_ai import (
    enrich_critical_classes_with_ai, detect_model_usage,
    analyze_configurations_real,
)
if TYPE_CHECKING:
    from axioma_doc_analyzer import ImprovedProjectAnalyzer, DependencyNode

# ==================== MANIFEST CORE (FASE 1) ====================
def _load_manifest_core(project_root: Path) -> List[str]:
    """
    ✅ FASE 1: Carga manifest_core.txt para forzar inclusión de archivos críticos.
    Retorna lista de rutas relativas normalizadas.
    """
    manifest_path = project_root / "tools" / "documentador" / "manifest_core.txt"
    if manifest_path.exists():
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                return [line.strip().replace('\\', '/') for line in f if line.strip() and not line.startswith('#')]
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_analysis_passes.py:80] excepción degradada (intencional): {_d2e}")
    return []

# ==================== SCAN [FIX-3] + MANIFEST + [FIX-10] + [FIX-EXCLUSIONES] + [FIX-SECURITY v4.0.1] ====================
def scan_files_recursive(analyzer: "ImprovedProjectAnalyzer") -> List[Path]:
    """
    [FIX-3] Escanea .py Y .yaml/.yml.
    ✅ FASE 1: Integra manifest_core.txt para sobrescribir exclusiones si se activa strict mode.
    ✅ [FIX-10] v3.6.1: Archivos YAML ahora aplican EXCLUDED_PATTERNS (evita debug_, temp_, etc.)
    ✅ [FIX-EXCLUSIONES] v3.9.1: Excluye directorios basándose en la ruta relativa completa,
       corrigiendo el bug donde 'data/outputs' en EXCLUSIONES no filtraba el subdirectorio 'outputs'.
    🔐 [FIX-SECURITY] v4.0.1: Excluye archivos con credenciales hardcodeadas (push.py, commit.py)
       del análisis del documentador para evitar que constantes sensibles (GITHUB_TOKEN) aparezcan
       en la documentación generada. El filtro se aplica ANTES de cualquier lógica de inclusión.

    Retorna lista de todos los archivos a analizar.
    """
    print("🔍 Escaneando recursivamente todos los directorios...")
    script_dir_name = Path(__file__).parent.name

    # ✅ v4.0.1: Archivos con credenciales que NUNCA deben ser documentados
    # push.py contiene token de GitHub hardcodeado (ghp_...) que no debe filtrarse en la docs
    SENSITIVE_FILES: Set[str] = {
        "push.py",       # Sincronización y push remoto a GitHub (token oculto)
        "commit.py",     # Automatización de commits locales (credenciales ocultas)
    }

    python_files: List[Path] = []
    config_files: List[Path] = []

    # ✅ FASE 1: Cargar manifiesto de inclusión estricta
    strict_paths = set(_load_manifest_core(analyzer.project_root))
    analyzer._strict_include = strict_paths  # Guardar referencia para otros módulos

    for dirpath, dirnames, filenames in os.walk(analyzer.project_root):
        current_dir = Path(dirpath)

        # Calcular ruta relativa del directorio actual para exclusión robusta
        try:
            rel_dir = str(current_dir.relative_to(analyzer.project_root)).replace('\\', '/')
            if rel_dir == '.':
                rel_dir = ""
        except ValueError:
            rel_dir = ""

        # ✅ FIX-EXCLUSIONES: Verificar por nombre Y por ruta relativa
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUSIONES
            and (f"{rel_dir}/{d}" if rel_dir else d) not in EXCLUSIONES  # ← SOLUCIÓN CONFIRMADA
            and not d.startswith('.')
            and d != script_dir_name
        ]

        if current_dir != analyzer.project_root:
            analyzer.stats.total_dirs += 1

        for filename in filenames:
            if filename.startswith('.'):
                continue

            # 🔐 [FIX-SECURITY v4.0.1]: Excluir archivos sensibles ANTES de cualquier otra lógica
            # Esto garantiza que push.py y commit.py NUNCA se analicen, ni siquiera si están
            # en manifest_core.txt o en rutas de strict-include.
            if filename in SENSITIVE_FILES:
                continue

            filepath = current_dir / filename
            rel_path = str(filepath.relative_to(analyzer.project_root)).replace('\\', '/')

            # ✅ FASE 1: Si está en manifest, saltar exclusiones
            is_strict = rel_path in strict_paths or any(fp.endswith(rel_path) for fp in strict_paths)
            if is_strict:
                pass  # Forzar inclusión
            elif filename.endswith('.py'):
                if any(pattern in filename for pattern in EXCLUDED_PATTERNS):
                    continue
                if is_file_excluded(filepath):
                    continue
            elif filename.endswith(('.yaml', '.yml')):
                # ✅ [FIX-10] Aplicar EXCLUDED_PATTERNS a los YAMLs también
                if any(pattern in filename for pattern in EXCLUDED_PATTERNS):
                    continue
                if is_file_excluded(filepath):
                    continue

            if filename.endswith('.py'):
                analyzer.stats.total_files += 1
                analyzer.stats.python_files += 1
                python_files.append(filepath)
            elif filename.endswith(('.yaml', '.yml')):
                config_files.append(filepath)

    # Guardar config files en analyzer para uso posterior
    analyzer._config_yaml_files = config_files
    return python_files
# ==================== ANALYZE FILE + AST CACHE ====================
def analyze_file(analyzer: "ImprovedProjectAnalyzer", filepath: Path):
    """
    Analiza archivo Python individual v3.6
    ✅ FASE 2: Almacena AST raw en analyzer._ast_cache para cruce con FlowMapper
    ✅ FIX 2: Extracción de constantes de módulo (MAYÚSCULAS)
    ✅ FIX v4.1.1: Llamada a extract_imports() actualizada con current_filepath para soporte de imports relativos.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content, filename=str(filepath))
        rel_path = str(filepath.relative_to(analyzer.project_root))

        # ✅ FASE 2: Caché de AST para FlowMapper (evita re-parseo costoso)
        if not hasattr(analyzer, '_ast_cache'):
            analyzer._ast_cache = {}
        analyzer._ast_cache[rel_path] = {
            'tree': tree,
            'content': content,
            'source_lines': content.splitlines()
        }

        category = categorize_file(rel_path)
        lines = len(content.splitlines())
        analyzer.stats.total_lines += lines

        classes = extract_classes_defined_only(tree, content, rel_path)
        enriched = extract_enriched_classes(analyzer, tree, content, rel_path)
        functions_names = extract_functions(tree)
        functions_signatures = extract_function_signatures(tree)

        # ✅ FIX v4.1.1: Pasar rel_path como current_filepath para resolver imports relativos
        imports = extract_imports(tree, current_filepath=rel_path)

        dataclasses_found = extract_dataclasses(tree)
        enums_found = extract_enums(tree)
        docstring = extract_module_docstring(tree)

        # ✅ v1.0.9: Métricas GLOBALES honestas — incluyen clases anidadas y
        # métodos (antes solo contaban definiciones de nivel módulo: ~586
        # clases y ~735 funciones vs ~679 y ~4.491 reales). Las listas por
        # módulo/clase del reporte mantienen su granularidad de nivel superior.
        total_classes_all = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        total_functions_all = sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        analyzer.stats.total_classes += total_classes_all
        analyzer.stats.total_functions += total_functions_all
        analyzer.stats.total_imports += len(imports)

        for cls_info in classes:
            analyzer.class_locations[cls_info.name].append(rel_path)
            analyzer.class_definitions[cls_info.name].append(cls_info)
        for cls_info in enriched:
            analyzer.enriched_classes[cls_info.name].append(cls_info)

        if functions_signatures:
            analyzer.module_functions[rel_path] = functions_signatures

        last_modified = datetime.fromtimestamp(filepath.stat().st_mtime)
        recent_changes = detect_recent_changes(content, last_modified)
        purpose = infer_purpose_rich(
            rel_path, content, docstring,
            [c.name for c in classes], functions_names
        )

        # ✅ FIX 2: Extraer constantes de módulo
        module_constants = extract_module_constants(tree, content)

        analyzer.files_info[rel_path] = FileInfo(
            path=rel_path,
            category=category,
            purpose=purpose,
            lines=lines,
            complexity=len(classes) + len(functions_names),
            classes=[c.name for c in classes],
            functions=functions_names,
            imports=imports,
            dataclasses=dataclasses_found,
            enums=enums_found,
            docstring=docstring,
            last_modified=last_modified,
            recent_changes=recent_changes,
            # ✅ FIX 2: Constantes de módulo
            module_constants=module_constants,
        )
        detect_model_usage(rel_path, content, analyzer.ai_models)
    except Exception as e:
        print(f"⚠️  Error analizando {filepath}: {e}")

# ==================== INFER PURPOSE [FIX-PURPOSE v3.9.4] ====================

# ✅ [FIX-PURPOSE] Regex para capturar comentario explícito de propósito.
# Captura líneas con el patrón: "# Propósito: <descripción>" o "# Proposito: <descripción>"
# ignorando mayúsculas y espacios variables alrededor del separador.
_RE_PURPOSE_COMMENT = re.compile(
    r'#\s*[Pp]rop[oó]sito\s*:\s*(.+)',
    re.IGNORECASE
)

# ✅ [FIX-PURPOSE] Regex para header AXIOMA estándar:
# "# AXIOMA vX.X.X — <descripción>" o "# AXIOMA vX.X — <descripción>"
# El separador puede ser — (em dash), - (hyphen), o : (dos puntos)
_RE_AXIOMA_HEADER = re.compile(
    r'#\s*AXIOMA\s+v[\d.]+\s*[—\-:]\s*(.+)',
    re.IGNORECASE
)

def _extract_purpose_from_header(content: str) -> Optional[str]:
    """
    ✅ [FIX-PURPOSE v3.9.4] Extrae propósito desde los comentarios del header.

    Estrategia de 2 niveles sobre las primeras 40 líneas del archivo:
      Nivel 1: Comentario explícito '# Propósito: <descripción>'
               → Más confiable, presente en ~60% de los archivos AXIOMA
      Nivel 2: Header '# AXIOMA vX.X — <descripción>'
               → Presente en prácticamente todos los archivos restantes

    No depende de docstrings triple-comilla. Resuelve el problema raíz donde
    el parser ignoraba los comentarios # aunque contenían la descripción real.

    Args:
        content: Contenido completo del archivo Python

    Returns:
        Cadena de propósito limpia o None si no se encuentra ningún patrón
    """
    lines = content.splitlines()
    header_lines = lines[:40]  # El header AXIOMA siempre está en las primeras ~20 líneas

    # Nivel 1: '# Propósito: ...' — más específico, tiene prioridad
    for line in header_lines:
        m = _RE_PURPOSE_COMMENT.match(line.strip())
        if m:
            purpose = m.group(1).strip()
            # Descartar líneas que son solo decoración o muy cortas
            if len(purpose) > 15 and not purpose.startswith('═') and not purpose.startswith('-'):
                return purpose[:150]

    # Nivel 2: '# AXIOMA vX.X — <descripción>'
    for line in header_lines:
        m = _RE_AXIOMA_HEADER.match(line.strip())
        if m:
            desc = m.group(1).strip()
            # Limpiar sufijos de versión adicionales: "v0.4.0 BGE-M3 Local" → solo desc
            # Descartar si es solo una versión sin descripción real
            if len(desc) > 10 and not desc.startswith('v') and not desc.startswith('('):
                return desc[:150]

    return None


def infer_purpose_rich(path: str, content: str, docstring: str,
                       classes: List[str], functions: List[str]) -> str:
    """
    ✅ [FIX-PURPOSE v3.9.4] Inferencia de propósito en 4 niveles, de mayor a menor confianza:
      1. Comentario '# Propósito:' o header 'AXIOMA vX — desc' (comentarios del archivo)
      2. Primera línea significativa del docstring triple-comilla
      3. filename_map expandido con todos los módulos de AXIOMA v1.0.0
      4. Fallback estructural (clases/funciones detectadas)

    El nivel 1 es nuevo en v3.9.4 y resuelve el problema principal: el 80%+ de archivos
    AXIOMA tienen su descripción en comentarios #, no en docstrings, y el parser los ignoraba.
    """
    # ── Nivel 1: Header de comentarios AXIOMA ──────────────────────────────
    header_purpose = _extract_purpose_from_header(content)
    if header_purpose:
        return header_purpose

    # ── Nivel 2: Docstring triple-comilla ─────────────────────────────────
    filename = Path(path).stem.lower()
    if docstring:
        lines = [l.strip() for l in docstring.split('\n') if l.strip()]
        for line in lines:
            if (len(line) > 20
                    and not line.startswith('===')
                    and not line.startswith('---')
                    and not line.startswith('═')
                    and not line.startswith('─')
                    # No descartar por nombre de archivo — el filtro original era demasiado agresivo
                    and not line.lower().endswith('.py')
                    and not re.match(r'^v?\d+\.\d+', line)):   # No retornar líneas de versión
                return line[:120]

    # ── Nivel 3: filename_map expandido (AXIOMA v1.0.0) ────────────────────
    # Expandido con todos los módulos identificados en el proyecto
    filename_map = {
        # Core pipeline
        'dispatcher':           'Punto de entrada principal del sistema AXIOMA',
        'dispatcher_handlers':  'Handlers deterministas del dispatcher (tiempo, clima, finanzas)',
        'router':               'Enrutamiento inteligente de tareas al agente correcto',
        'smart_router':         'Router semántico con clasificación ML y caché multicapa',
        'smart_router_core':    'Núcleo de enrutamiento: constantes, regex y helpers puros',
        'agent':                'Orquestación principal del agente IA',
        'orchestrator':         'Coordinación de modelos y agentes IA',
        'validator':            'Validación de respuestas y estructuras del sistema',
        'tool_executor':        'Ejecución segura de herramientas externas con CircuitBreaker',
        'parallel_executor':    'Ejecución paralela de tareas con control de concurrencia',
        'state_manager':        'Gestión de estado de sesión con checkpoints y rollback',
        # Caché
        'cache_l1':             'Caché L1 en memoria RAM (dict, O(1), TTL por entrada)',
        'cache_l2':             'Caché L2 SQLite persistente (queries frecuentes entre sesiones)',
        'cache_l3':             'Caché L3 semántico LanceDB + BAAI/bge-m3 (similitud vectorial)',
        'cache':                'Coordinador del sistema de caché multicapa L1/L2/L3',
        # Memoria
        'memory':               'Gestión de memoria persistente del sistema',
        'gateway':              'Punto de entrada unificado para todas las operaciones de memoria',
        'semantic':             'Memoria semántica con búsqueda por similitud vectorial (LanceDB)',
        'semantic_base':        'Infraestructura base para memoria semántica (LanceDB, pool)',
        'long_term':            'Almacenamiento persistente en SQLite para memoria a largo plazo',
        'short_term':           'Memoria de contexto de sesión en RAM (ventana deslizante)',
        'context_window':       'Gestión dinámica de ventana de contexto para el LLM',
        'embedding_optimizer':  'Optimización de embeddings: caché LRU, batch processing, threshold',
        # Clasificación
        'classifier':           'Clasificación de intenciones con ML y embeddings',
        'classifier_core':      'Núcleo de clasificación: constantes, regex y helpers puros',
        'classifier_api':       'API pública de clasificación expuesta por Facade',
        # Agentes especializados
        'coder_agent':          'Agente especializado en generación y corrección de código',
        'data_handler':         'Handler determinista para datos externos (clima, finanzas, hora)',
        'web_handler':          'Handler para búsquedas y consultas web',
        'creative_handler':     'Handler para generación de contenido creativo',
        # Calidad y monitoreo
        'quality_monitor':      'Monitor de calidad de respuestas con métricas y alertas',
        'feedback_loop':        'Loop de retroalimentación para mejora continua del sistema',
        'circuit_breaker':      'Disyuntor automático para protección ante fallos en cascada',
        # Configuración y dominio
        'settings':             'Configuración global del sistema via Pydantic BaseSettings',
        'paths':                'Gestión centralizada de rutas absolutas del proyecto',
        'types':                'Definiciones de tipos, enums TaskType y contratos del dominio',
        'exceptions':           'Jerarquía de excepciones personalizadas del dominio AXIOMA',
        # LLM / cliente
        'client':               'Cliente de comunicación con Ollama (HTTP + streaming)',
        'ollama':               'Integración con modelos Ollama locales',
        # API / servidor
        'server':               'Servidor web FastAPI — punto de entrada HTTP',
        'routes':               'Endpoints REST de la API AXIOMA',
        'routes_hwfit':         'Endpoints REST para consulta de compatibilidad hardware/modelo',
        'hardware_fit':         'Detección de hardware y compatibilidad de modelos con VRAM/RAM',
        'health_checker':       'Verificación de estado de todos los subsistemas',
        # Ambigüedad / clarificación
        'ambiguity':            'Detección y manejo de ambigüedad en queries del usuario',
        'clarification':        'Diálogo de clarificación para queries ambiguas',
        'feedback':             'Sistema de feedback y logging de interacciones',
        # Tests y herramientas
        'model_tester':         'Tests de latencia y calidad de modelos Ollama',
        'benchmark':            'Benchmarks y métricas de rendimiento del sistema',
        'test_suite':           'Suite de tests unificada de AXIOMA',
        # Utilidades
        'utils':                'Utilidades y helpers transversales del sistema',
        'ui':                   'Interfaz de usuario consolidada (Colors + CLI)',
        '__init__':             'Exports públicos del módulo',
        'detailed_logger':      'Logger estructurado con niveles y rotación de archivos',
        'logger':               'Configuración y helpers de logging del sistema',
    }

    for key, desc in filename_map.items():
        if key in filename:
            return desc

    # ── Nivel 4: Fallback estructural ─────────────────────────────────────
    parts = []
    if classes:
        parts.append(f"Clases: {', '.join(classes[:3])}" + (" ..." if len(classes) > 3 else ""))
    if functions:
        pub_funcs = [f for f in functions if not f.startswith('_')][:3]
        if pub_funcs:
            parts.append(f"Funciones: {', '.join(pub_funcs)}" + (" ..." if len(pub_funcs) >= 3 else ""))

    return ' | '.join(parts) if parts else 'Módulo del sistema'

# ==================== EXTRACT ENRICHED CLASSES [FIX-6] ====================
def extract_enriched_classes(analyzer: "ImprovedProjectAnalyzer",
                             tree: ast.AST, content: str,
                             filepath: str) -> List[EnrichedClassInfo]:
    """
    [FIX-6] Usa ast.walk en lugar de iterar solo tree.body,
    capturando clases anidadas y definidas dentro de funciones.
    ✅ CORREGIDO v3.5: Manejo mejorado de clases con signatures completas
    ✅ FASE 1: Filtros relajados delegados a SignatureExtractor.should_include_method
    ✅ FIX 1: Introspección Pydantic con validaciones Field()
    ✅ FIX 3: Detección de Lazy Properties
    ✅ FIX 5: Tags inline ampliados (FIX, BUG, CORREGIDO, etc.)
    """
    enriched_list = []
    lines = content.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        docstring = ast.get_docstring(node) or ""
        bases = extract_bases(node)
        is_pydantic = any(b in ['BaseModel', 'BaseSettings'] for b in bases)
        is_dataclass = any(
            isinstance(dec, ast.Name) and dec.id == 'dataclass'
            for dec in node.decorator_list
        )
        is_enum = any('Enum' in b for b in bases)
        has_fallback = any(marker in docstring.lower() for marker in [
            'fallback', 'backup', 'compatibility', 'compat'
        ])

        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = SignatureExtractor.extract_function_signature(item, lines)
                # ✅ FASE 1: Aplicar filtro relajado antes de agregar
                if SignatureExtractor.should_include_method(sig.name, sig.decorators, sig.docstring, sig.tags):
                    methods.append(sig)

        attributes = SignatureExtractor.extract_class_attributes(node)

        # ✅ FIX 1: Extraer campos Pydantic con validaciones
        pydantic_fields = []
        if is_pydantic:
            pydantic_fields = extract_pydantic_fields(node)

        # ✅ FIX 3: Detectar Lazy Properties
        lazy_props = detect_lazy_properties(node)

        # Marcar métodos lazy en sus metadatos
        for method in methods:
            if method.name in lazy_props:
                method.tags.append('LAZY_INIT')
                # Agregar info de la clase instanciada como metadata
                if not method.ai_summary:
                    init_class = lazy_props[method.name].get('init_class', '')
                    if init_class and init_class != 'Unknown':
                        method.ai_summary = f"Lazy init → {init_class} (ahorra VRAM/RAM)"

        start_line = node.lineno - 1
        end_line = node.end_lineno if hasattr(node, 'end_lineno') else start_line + 50
        class_code = '\n'.join(lines[start_line:min(end_line, start_line + 80)])

        # ✅ FIX 5: Tags inline ampliados
        version_tags = {}
        changelog_notes = []
        enhanced_tags = extract_enhanced_inline_tags(content, node.lineno, end_line or node.lineno + 50)
        for tag_type, tag_key, desc in enhanced_tags:
            if tag_type in ('tag', 'version'):
                version_tags[tag_key] = desc
            else:
                changelog_notes.append((f"{tag_type.upper()}_{tag_key}", desc))

        enriched_list.append(EnrichedClassInfo(
            name=node.name,
            file=filepath,
            line_number=node.lineno,
            docstring=docstring,
            bases=bases,
            is_dataclass=is_dataclass,
            is_enum=is_enum,
            is_pydantic=is_pydantic,
            has_fallback_marker=has_fallback,
            methods=methods,
            attributes=attributes,
            version_tags=version_tags,
            # ✅ FIX 1 & 3 & 5: Nuevos campos
            pydantic_fields=pydantic_fields,
            lazy_properties=lazy_props,
            changelog_notes=changelog_notes,
        ))
    return enriched_list

# ==================== DEPENDENCIES ====================
def analyze_dependencies(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza dependencias externas del proyecto v3.5"""
    requirements_file = analyzer.project_root / "requirements.txt"
    if requirements_file.exists():
        with open(requirements_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    pkg = line.split('==')[0].split('>=')[0].split('<=')[0]
                    analyzer.dependencies['external'].append(pkg)

def analyze_module_dependencies(analyzer: "ImprovedProjectAnalyzer"):
    """
    Analiza dependencias entre módulos internos.
    Usa segundo segmento para src/* evitando colapso en 'src'.
    ✅ CORREGIDO v3.5: Soporte mejorado para detección de dependencias críticas
    """
    print("🔗 Analizando dependencias entre módulos...")
    from axioma_doc_analyzer import DependencyNode

    EXT_DEPS = {
        'chromadb', 'sqlite3', 'fastapi', 'pydantic', 'ollama',
        'torch', 'sklearn', 'httpx', 'aiohttp', 'requests',
    }

    def _group(module_dotted: str) -> str:
        parts = module_dotted.split('.')
        if len(parts) >= 2 and parts[0] == 'src':
            return parts[1]
        return parts[0]

    for filepath, info in analyzer.files_info.items():
        module_dotted = filepath.replace('/', '.').replace('.py', '')
        group = _group(module_dotted)

        if group not in analyzer.module_dependencies:
            analyzer.module_dependencies[group] = DependencyNode(
                module=group,
                depends_on=[],
                dependency_type='internal'
            )

        for imp in info.imports:
            if imp.startswith('src.'):
                dep_group = _group(imp)
                if dep_group != group and dep_group not in analyzer.module_dependencies[group].depends_on:
                    analyzer.module_dependencies[group].depends_on.append(dep_group)
            else:
                base = imp.split('.')[0]
                if base in EXT_DEPS:
                    ext_dep = f"ext:{base}"
                    if ext_dep not in analyzer.module_dependencies[group].depends_on:
                        analyzer.module_dependencies[group].depends_on.append(ext_dep)

    dep_counts: Dict[str, int] = {}
    for node in analyzer.module_dependencies.values():
        for dep in node.depends_on:
            dep_counts[dep] = dep_counts.get(dep, 0) + 1

    for group_name, node in analyzer.module_dependencies.items():
        node.usage_count = dep_counts.get(group_name, 0)
    print(f"   ✅ {len(analyzer.module_dependencies)} grupos de módulos analizados")

# ==================== CRITICAL FLOWS [FIX-2] ====================
def build_critical_flows(analyzer: "ImprovedProjectAnalyzer"):
    """
    [FIX-2] fallback_chain ahora lee flow_def.get('fallback_chain', [])
    en lugar de duplicar failure_points.
    ✅ CORREGIDO v3.5: Flujos actualizados para AXIOMA v0.0.9
    """
    print("🔄 Construyendo flujos críticos con enriquecimiento AST...")
    for flow_def in CRITICAL_FLOWS_DEFINITION:
        enriched_steps = [dict(s) for s in flow_def['steps']]

        for rel_path, method_name in flow_def.get('ast_targets', []):
            full_path = analyzer.project_root / rel_path
            if not full_path.exists():
                continue
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                       and node.name == method_name:
                        is_async = isinstance(node, ast.AsyncFunctionDef)
                        for step in enriched_steps:
                            if method_name in step.get('component', ''):
                                step['verified'] = True
                                step['is_async'] = is_async
                                step['source_line'] = node.lineno
                                break
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 axioma_doc_analysis_passes.py:636] excepción degradada (intencional): {_d2e}")

        analyzer.critical_flows.append(CriticalFlow(
            name=flow_def['name'],
            description=flow_def['description'],
            steps=enriched_steps,
            failure_points=flow_def['failure_points'],
            fallback_chain=flow_def.get('fallback_chain', []),  # [FIX-2]
        ))
    print(f"   ✅ {len(analyzer.critical_flows)} flujos críticos documentados")

# ==================== MEMORY SYSTEM ====================
def analyze_memory_system(analyzer: "ImprovedProjectAnalyzer"):
    """Analiza sistema de memoria v3.5 — actualizado v3.9.4 (ChromaDB → LanceDB)"""
    # ✅ v3.9.4: La memoria semántica migró de ChromaDB a LanceDB.
    # data/memory/ puede existir con datos legacy; data/semantic_cache_l3_lance/ es el activo.
    lance_memory_path = analyzer.project_root / "data/memory"
    if lance_memory_path.exists():
        size = get_directory_size(lance_memory_path)
        analyzer.memory_systems.append(MemorySystemInfo(
            type="LanceDB (Vector Store)",
            location="data/memory/",
            purpose="Búsqueda semántica de interacciones pasadas (gateway + long-term)",
            implementation_file="src/memory/semantic.py",
            size_estimate=size,
            additional_info={
                "type": "Vector database",
                "embedding_model": "BAAI/bge-m3",
                "capabilities": ["Semantic search", "Context retrieval"],
                "backend": "LanceDB (migrado desde ChromaDB)",
            }
        ))

    # ✅ v3.9.4: Caché semántico L3 — directorio LanceDB dedicado
    lance_l3_path = analyzer.project_root / "data/semantic_cache_l3_lance"
    if lance_l3_path.exists():
        size = get_directory_size(lance_l3_path)
        analyzer.memory_systems.append(MemorySystemInfo(
            type="LanceDB L3 Semantic Cache",
            location="data/semantic_cache_l3_lance/",
            purpose="Caché semántico L3 — similitud vectorial entre queries (BAAI/bge-m3)",
            implementation_file="src/router/cache_l3.py",
            size_estimate=size,
            additional_info={
                "type": "Vector database (L3 cache)",
                "embedding_model": "BAAI/bge-m3",
                "capabilities": ["Semantic deduplication", "Query similarity cache"],
                "backend": "LanceDB",
            }
        ))

    db_path = analyzer.project_root / "data/axioma.db"
    if db_path.exists() and db_path.is_file():
        size = get_file_size(db_path)
        analyzer.memory_systems.append(MemorySystemInfo(
            type="SQLite Database",
            location="data/axioma.db",
            purpose="Almacenamiento estructurado de interacciones",
            implementation_file="src/memory/long_term.py",
            size_estimate=size,
            additional_info={
                "tables": len(analyzer.database_tables),
                "type": "Relational database"
            }
        ))

# ==================== RE-EXPORTS (refactorización v4.0.1) ====================
# axioma_doc_schema_dup_passes contiene la segunda mitad de este módulo.
# Se re-exporta aquí para preservar la interfaz pública sin cambios.
from axioma_doc_schema_dup_passes import (
    detect_proto_files,
    analyze_database_schema,
    analyze_configurations,
    analyze_data_contracts,
    detect_env_vars,
    analyze_task_types,
    analyze_class_hierarchy,
    LEGITIMATE_PATTERNS,
    _is_test_file,
    _categorize_file_purpose,
    _strip_boilerplate,
    _calculate_semantic_similarity,
    _extract_method_signatures_direct,
    _analyze_duplication_legitimacy,
    detect_duplications,
    enrich_with_ai,
)

if __name__ == "__main__":
    print("Módulo de pasadas de análisis para el documentador de AXIOMA.")
    print("Provee funciones de escaneo, extracción AST, detección de duplicaciones y análisis de dependencias.")
    print("Versión: v4.1.1")
