#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_base.py
# Autor: SaintWick
"""
axioma_doc_base.py - Configuración, estructuras de datos y constantes para el documentador de AXIOMA.

Este módulo define la infraestructura base utilizada por el sistema de documentación:
- Rutas y configuración del proyecto (PROJECT_ROOT, OUTPUT_FILE, CACHE_DB_PATH, etc.).
- Estructuras de datos (dataclasses) para archivos, clases, dependencias, flujos críticos, etc.
- Configuraciones para caché, validación, dependencias y flujos críticos.
- Definición de flujos críticos del sistema (CRITICAL_FLOWS_DEFINITION) con sus pasos y puntos de fallo.
- Re-export de utilidades desde axioma_doc_base_utils para mantener compatibilidad.

Es el punto central de configuración y definición de tipos para todo el documentador.
"""
import ast
import re
import sys
import subprocess
import json
import sqlite3
import os
import hashlib
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Any, Set, Tuple, Optional
from datetime import datetime

# ==================== IMPORT COMPLETO DE UTILIDADES (con re-export) ====================
from axioma_doc_base_utils import (
    # Hash
    compute_file_hash,
    compute_signature_hash,
    # Ollama
    detect_ollama_models,
    # Database
    verify_database_access,
    ensure_data_directory,
    # File utilities
    is_file_excluded,  # ✅ v3.6.1: Esta es la única fuente de verdad para exclusiones
    load_json_file,
    save_json_file,
    # Categorization
    categorize_file,
    infer_purpose,
    infer_purpose_enhanced,
    # AST utilities
    extract_module_docstring,
    estimate_cyclomatic_complexity,
    get_imports_from_file,
    # Task types
    infer_task_type_info,
    # File info
    get_file_size,
    get_directory_size,
    format_size,
    get_file_modification_time,
    detect_recent_changes,
    # String utilities
    sanitize_filename,
    truncate_string,
    count_code_lines,
    # System
    get_python_version,
    check_file_exists,
    get_relative_path,
    is_python_file,
    format_datetime,
    is_test_file,
    is_init_file,
)

# ==================== CONFIGURACIÓN ====================
# ✅ ISSUE-147: raíz portable (antes la carpeta del autor; ver main.py y settings.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ==================== VERSIONES — FUENTE ÚNICA ====================
# ✅ v0.6.9w (FIX VERSIONES): antes el documento generado llevaba la versión
# hardcodeada (`# AXIOMA v0.3.6 — Documentación Completa`) mientras el proyecto
# iba por otra. Ahora:
#   - `AXIOMA_PROJECT_VERSION` = versión del PROYECTO, leída de `src/__init__.py`.
#   - `DOCUMENTADOR_VERSION`   = versión de ESTA herramienta (fuente única).
# La versión del documentador estaba dispersa e inconsistente por los módulos
# (v3.6 / v3.9.0 / v4.1 / v4.1.1 / v4.2.1). Los marcadores más recientes del
# propio código (`✅ v4.2.1` en `SUPPORTED_PYTHON_VERSIONS` y en
# `CRITICAL_FLOWS_CONFIG`) son la referencia vigente.
DOCUMENTADOR_VERSION = "4.2.2"


def get_project_version() -> str:
    """Lee la versión real del proyecto desde `src/__init__.py`.

    Returns:
        Versión declarada (p. ej. `0.6.9`) o `"desconocida"` si no se puede leer.
    """
    try:
        import re

        texto = (PROJECT_ROOT / "src" / "__init__.py").read_text(
            encoding="utf-8", errors="ignore"
        )
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', texto)
        if m:
            return m.group(1)
    except Exception:  # pragma: no cover — defensivo: nunca romper la generación
        pass
    return "desconocida"


AXIOMA_PROJECT_VERSION = get_project_version()

# ✅ CORREGIDO: Extensión .md en lugar de .toon
OUTPUT_FILE = PROJECT_ROOT / "docs" / "AXIOMA_COMPLETE_CONTEXT.md"

# Nuevas rutas para caché v3.2/v3.3/v3.4/v3.5
CACHE_DB_PATH = PROJECT_ROOT / "data" / "doc_cache.db"
LAST_DOC_TIMESTAMP_FILE = PROJECT_ROOT / "data" / ".last_doc_timestamp"
DOC_HASHES_FILE = PROJECT_ROOT / "data" / ".doc_hashes.json"

# Directorios a excluir del análisis de código fuente
EXCLUSIONES = {
    '.venv', '__pycache__', '.git', 'node_modules', 'dist', 'build',
    'backups', 'logs', '.pytest_cache', 'htmlcov', 'venv', 'env',
    'checkpoints', 'documentador',  # Excluir el propio documentador
    'deepseek-harness',  # Checkout externo del harness (no es código de AXIOMA)
    'inforchat',         # Notas/planes internos del desarrollador (privados)
    'data',              # Datos de runtime: BD, memoria vectorial, backups y
                         # salidas del CoderAgent (data/outputs/*.py NO son fuente)
    # ✅ v4.2.2 (2026-09-28): `tests/` y `docs/` FUERA del análisis.
    #   · tests/: no son arquitectura del producto. El propio documentador se
    #     contradecía: el FlowMapper ya los ignoraba (IGNORED_PATTERNS) y el
    #     escáner de env también (_ENV_SCAN_IGNORE), pero el escaneo principal
    #     los incluía → 26 archivos de test en la doc y `tests/test_netron.py`
    #     como #1 del Top 30 de archivos.
    #   · docs/: documentos generados o escritos a mano (regla R15), no código.
    #     Se escaneaban sus directorios (contando en `total_dirs`) sin aportar
    #     archivos, y la "Estructura de directorios" los listaba como código.
    'tests',
    'docs',
}

# Patrones de archivos a excluir
EXCLUDED_PATTERNS = [
    'diagnose_', 'debug_', 'temp_', 'tmp_', 'axioma_doc_complete',
    'structure_detector',  # Excluir el detector de estructura
    # ✅ v4.2.2 (2026-09-28) — ISSUE-079: informes internos, diagnósticos y datos
    # del sistema del usuario. Mismos criterios que INTERNAL_ONLY_PATTERNS de
    # `tools/commit.py` (que ya no los versiona): no son código del producto y no
    # deben quedar documentados. Antes solo se filtraba `diagnose_`, así que
    # `diagnostico_v2.py` (468 líneas) y `lista_paquetes.py` seguían entrando.
    'diagnostico_',
    'diagnostic_recovery',
    'PC_Informe_',
    'calibration_data',
    'calibration_results',
    'paquetes_completos',
    'lista_paquetes',
    'prueba ',
]

# ==================== ESTRUCTURAS DE DATOS v4.0 ====================

@dataclass
class FileHashEntry:
    """Entrada de hash para caché de archivos - NUEVO v3.2"""
    path: str
    hash: str
    size: int
    modified: float
    doc_updated: bool


@dataclass
class DocGenerationStats:
    """Estadísticas de generación de documentación - NUEVO v3.2"""
    total_files: int = 0
    cached_files: int = 0
    analyzed_files: int = 0
    inconsistencies_found: int = 0
    generation_time_ms: float = 0.0
    cache_hit_rate: float = 0.0


@dataclass
class ProjectStats:
    """Estadísticas generales del proyecto"""
    total_files: int = 0
    python_files: int = 0
    total_lines: int = 0
    total_classes: int = 0
    total_functions: int = 0
    total_imports: int = 0
    total_dirs: int = 0


@dataclass
class FileInfo:
    """Información de un archivo Python"""
    path: str
    category: str
    purpose: str
    lines: int
    complexity: int
    classes: List[str]
    functions: List[str]
    imports: List[str]
    dataclasses: List[str]
    enums: List[str]
    docstring: str
    last_modified: Optional[datetime] = None
    recent_changes: str = ""
    # ✅ FIX 2 v4.1: Constantes de módulo (MAYÚSCULAS con valor literal)
    module_constants: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MemorySystemInfo:
    """Información de un sistema de memoria"""
    type: str
    location: str
    purpose: str
    implementation_file: str
    size_estimate: str
    additional_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AIModelInfo:
    """Información de un modelo IA"""
    name: str
    model_id: str
    size: str
    purpose: str
    provider: str
    files_used: List[str] = field(default_factory=list)


@dataclass
class DuplicationIssue:
    """Problema de duplicación de clases"""
    class_name: str
    locations: List[str]
    severity: str
    is_legitimate: bool
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    similarity: Optional[float] = None  # ✅ v4.0.0: Umbral de similitud estructural (0.0-1.0)


@dataclass
class DatabaseTable:
    """Tabla de base de datos"""
    name: str
    columns: List[Tuple[str, str, str]]
    primary_key: str


@dataclass
class ConfigParameter:
    """Parámetro de configuración"""
    name: str
    value: Any
    type_hint: str
    description: str
    file: str


@dataclass
class TaskTypeInfo:
    """Información de un TaskType"""
    name: str
    description: str
    example: str
    file: str


@dataclass
class ClassDefinitionInfo:
    """Información detallada sobre una definición de clase"""
    name: str
    file: str
    line_number: int
    docstring: str
    bases: List[str]
    is_dataclass: bool
    is_enum: bool
    is_pydantic: bool
    has_fallback_marker: bool


@dataclass
class InconsistencyRecord:
    """Registro de inconsistencia docs vs código - NUEVO v3.2"""
    file: str
    type: str
    issues: List[str]
    severity: str
    class_name: Optional[str] = None
    method_name: Optional[str] = None


@dataclass
class DependencyInfo:
    """Información de dependencia entre módulos - NUEVO v3.3"""
    module: str
    depends_on: List[str] = field(default_factory=list)
    dependency_type: str = 'internal'  # ✅ CORREGIDO: Valor default agregado
    usage_count: int = 0


@dataclass
class CriticalFlow:
    """Flujo crítico del sistema - NUEVO v3.3"""
    name: str
    description: str
    steps: List[Dict[str, Any]]
    failure_points: List[str]
    fallback_chain: List[str]


# ==================== CONSTANTES ====================
DEFAULT_ENCODING = 'utf-8'
MAX_FILE_SIZE_MB = 10
MAX_LINES_PER_FILE = 10000
CACHE_TTL_SECONDS = 300
SUPPORTED_PYTHON_VERSIONS = ['3.11', '3.12', '3.13', '3.14']  # ✅ v4.2.1: proyecto usa Python 3.14

# ==================== SECCIONES DE DOCUMENTACIÓN v4.0 ====================
DOCUMENTATION_SECTIONS = [
    'Resumen Ejecutivo',
    'Archivos Principales',
    'Análisis Detallado Clases',
    'Problemas Críticos',
    'Componentes Clave Sistema',
    'TaskTypes Disponibles',
    'Flujos Detallados Con Puntos Fallo',
    'Jerarquía Clases',
    'Sistema Memoria',
    'Modelos IA Detectados',
    'Dependencias Externas',
    'Esquema Base Datos',
    'Contratos Datos Expandidos',
    'Mapa Dependencias Módulos',
    'Parámetros Configuración Con Impacto',
    'Ejemplos Uso Reales',
    'Arquitectura',
    'Estadísticas',
    'Rendimiento Cache',
    'Investigación Prioritaria',
    'Solución Problemas'
]

# ==================== CONFIGURACIÓN v4.0 ====================
# Nuevas constantes v3.2 para caché y validación
CACHE_CONFIG = {
    'enabled': True,
    'db_path': str(CACHE_DB_PATH),
    'hashes_file': str(DOC_HASHES_FILE),
    'timestamp_file': str(LAST_DOC_TIMESTAMP_FILE),
    'hash_algorithm': 'sha256',
    'max_cache_size_mb': 50
}

VALIDATION_CONFIG = {
    'enabled': True,
    'check_signatures': True,
    'check_class_names': True,
    'check_method_signatures': True,
    'severity_levels': ['high', 'medium', 'low']
}

# NUEVO v3.3: Configuración para análisis de dependencias
DEPENDENCY_CONFIG = {
    'enabled': True,
    'track_internal': True,
    'track_external': True,
    'identify_critical': True,
    'max_depth': 5
}

# NUEVO v3.3: Configuración para flujos críticos
CRITICAL_FLOWS_CONFIG = {
    'enabled': True,
    'flows': [
        'CODE_GENERATION',
        'WEB_SEARCH',
        'INTENT_CLASSIFICATION',
        'DATA_HANDLER',  # ✅ v4.2.1: alineado con CRITICAL_FLOWS_DEFINITION (era MEMORY_OPERATION)
    ],
    'document_fallbacks': True,
    'document_timeouts': True
}

# NUEVO v3.3: Configuración para investigación prioritaria
RESEARCH_CONFIG = {
    'enabled': True,
    'priority_sources': [
        'Documentación interna',
        'Código fuente (AST)',
        'GitHub del proyecto',
        'Documentación oficial dependencias',
        'StackOverflow',
        'Búsqueda web general'
    ],
    'auto_search_on_error': True,
    'log_findings': True
}

# NUEVO v3.3: Impacto de parámetros de configuración
CONFIG_IMPACT_MAP = {
    'timeout': 'Afecta tiempos de respuesta y fallbacks',
    'model': 'Cambia modelo IA usado (afecta calidad/velocidad)',
    'cache': 'Afecta rendimiento y uso de memoria',
    'path': 'Requiere validar ruta existente',
    'dir': 'Requiere validar ruta existente',
    'threshold': 'Afecta criterios de validación/clasificación',
    'score': 'Afecta criterios de validación/clasificación',
    'max_': 'Límite de recursos o iteraciones',
    'min_': 'Límite mínimo de recursos o validación',
    'enable': 'Activa/desactiva funcionalidad',
    'use_': 'Activa/desactiva uso de feature',
}

# ==================== FLUJOS CRÍTICOS — FUENTE ÚNICA DE VERDAD v4.2.0 ====================
# Definición base manual. El analyzer los enriquece con datos AST reales del código.
# NO duplicar esta definición en axioma_doc_analyzer.py ni en axioma_doc_generator_sections.py.
# ✅ ACTUALIZADO v4.2.0: Nombres de componentes alineados con código real post-upgrade
#    - IntentClassifier.predict()       → IntentClassifierCore.classify()
#    - Text Preprocessing              → IntentClassifierCore._preprocess_text()
#    - Probability Calibration         → IntentClassifierCore.calibrate_probability()
#    - TaskType Assignment             → IntentClassifierCore._assign_task_type()
#    - ExternalAPIs.fetch()            → ExternalAPIManager.search_web()
#    Esto permite que el FlowMapper resuelva badges correctamente:
#    IntentClassifierCore es importado por classifier.py → [✅ CONECTADO]
#    ExternalAPIManager es importado por data_handler.py → [✅ CONECTADO]
CRITICAL_FLOWS_DEFINITION: List[Dict[str, Any]] = [
    {
        'name': 'CODE_GENERATION',
        'description': 'Flujo completo desde input usuario hasta código validado',
        'entry_point': 'src/core/dispatcher.py',    # Dispatcher reemplazó al Orchestrator
        'steps': [
            {'step': 1, 'component': 'Dispatcher.dispatch()',         'fallback': None,                          'timeout': 'N/A'},
            {'step': 2, 'component': 'SmartRouter.classify()',        'fallback': 'Rule-based classifier',       'timeout': '2s'},
            {'step': 3, 'component': 'SmartRouter.select_model()',    'fallback': 'qwen3:8b (llm_default_model)',  'timeout': '1s'},
            {'step': 4, 'component': 'MemoryGateway.search_context()','fallback': 'Sin contexto (continuar)',    'timeout': '3s'},
            {'step': 5, 'component': 'CoderAgent.generate()',         'fallback': 'qwen2.5-coder:7b (llm_code_model)', 'timeout': '25s'},
            {'step': 6, 'component': 'ValidatorAgent.validate()',     'fallback': 'Score mínimo 0.7',            'timeout': '2s'},
            {'step': 7, 'component': 'MemoryGateway.store()',         'fallback': 'Log error, continuar',        'timeout': '2s'},
        ],
        'failure_points': [
            'Ollama no disponible → Fallback a API externa',
            'Timeout >25s → Reintentar con modelo más ligero',
            'ValidatorAgent score <0.7 → Regenerar con más contexto',
        ],
        # AST enrichment: el analyzer busca estos métodos en el código real
        'ast_targets': [
            ('src/core/dispatcher.py',    'dispatch'),
            ('src/agents/coder.py',       'generate'),
            ('src/agents/validator.py',   'validate'),
        ],
    },
    {
        'name': 'WEB_SEARCH',
        'description': 'Búsqueda en internet con fallback en cascada',
        'entry_point': 'src/core/dispatcher.py',
        'steps': [
            {'step': 1, 'component': 'Dispatcher.dispatch()',              'fallback': None,                       'timeout': 'N/A'},
            {'step': 2, 'component': 'SmartRouter.classify()',             'fallback': 'Rule-based',               'timeout': '2s'},
            {'step': 3, 'component': 'SearcherAgent.search()',             'fallback': 'Tavily→Serper→DuckDuckGo', 'timeout': '10s'},
            {'step': 4, 'component': 'SearcherAgent.synthesize()',         'fallback': 'Resultados crudos',        'timeout': '5s'},
            {'step': 5, 'component': 'MemoryGateway.store_knowledge()',    'fallback': 'Log error',                'timeout': '2s'},
        ],
        'failure_points': [
            'Tavily API down → Serper API',
            'Serper API down → DuckDuckGo',
            'Todas las APIs down → Informar usuario',
        ],
        'ast_targets': [
            ('src/core/dispatcher.py',    'dispatch'),
            ('src/agents/searcher.py',    'search'),
            ('src/agents/searcher.py',    'synthesize'),
        ],
    },
    {
        'name': 'INTENT_CLASSIFICATION',
        'description': 'Clasificación ML de intención con pipeline explícito y trazable',
        'entry_point': 'src/router/classifier_core.py',
        'steps': [
            {'step': 1, 'component': 'IntentClassifierCore._preprocess_text()',     'fallback': None,                'timeout': '<1s'},
            {'step': 2, 'component': 'IntentClassifierCore.classify()',              'fallback': 'Keyword matching',  'timeout': '<1s'},
            {'step': 3, 'component': 'IntentClassifierCore.calibrate_probability()', 'fallback': 'Threshold 0.6',     'timeout': '<1s'},
            {'step': 4, 'component': 'IntentClassifierCore._assign_task_type()',     'fallback': 'GENERAL_CHAT',      'timeout': '<1s'},
        ],
        'failure_points': [
            'Modelo no entrenado → Usar reglas basadas en keywords',
            'Probabilidad <0.6 → TaskType por defecto',
            'Error de preprocessing → Log y continuar',
        ],
        'ast_targets': [
            ('src/router/classifier.py',      'classify'),
            ('src/router/classifier_core.py', 'classify'),
            ('src/router/classifier_core.py', 'calibrate_probability'),
            ('src/router/classifier_core.py', '_preprocess_text'),
            ('src/router/classifier_core.py', '_assign_task_type'),
        ],
    },
    {
        'name': 'DATA_HANDLER',
        'description': 'Dominios deterministas sin LLM (clima, finanzas, hora, noticias)',
        'entry_point': 'src/handlers/data_handler.py',
        'steps': [
            {'step': 1, 'component': 'Dispatcher.dispatch()',                'fallback': None,              'timeout': 'N/A'},
            {'step': 2, 'component': 'DataHandler.handle()',                 'fallback': None,              'timeout': '5s'},
            {'step': 3, 'component': 'ExternalAPIManager.search_web()',       'fallback': 'Cache local',     'timeout': '4s'},
        ],
        'failure_points': [
            'API externa caída → Servir desde cache si existe',
            'Sin cache → Informar usuario',
        ],
        'ast_targets': [
            ('src/handlers/data_handler.py',      'handle'),
            ('src/integrations/external_apis.py', 'search_web'),
        ],
    },
]

if __name__ == "__main__":
    print("Módulo base del documentador de AXIOMA. Proporciona estructuras de datos, configuración y utilidades.")
    print(f"Versión documentador: v{DOCUMENTADOR_VERSION} | Proyecto: v{AXIOMA_PROJECT_VERSION}")
    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"OUTPUT_FILE: {OUTPUT_FILE}")
    print(f"Secciones de documentación: {len(DOCUMENTATION_SECTIONS)}")
    print(f"Flujos críticos definidos: {len(CRITICAL_FLOWS_DEFINITION)}")
