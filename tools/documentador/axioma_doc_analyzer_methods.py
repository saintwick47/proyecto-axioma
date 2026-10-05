#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_analyzer_methods.py
# Autor: SaintWick
"""
axioma_doc_analyzer_methods.py - Métodos delegados del Analizador AXIOMA v3.6
Extraído de axioma_doc_analyzer.py para reducir longitud y mejorar mantenibilidad.
✅ Ruta: /ruta/a/axioma/tools/documentador/
✅ v3.6: FlowMapper integrado, validación estricta, filtros relajados
✅ CORREGIDO: Sincronización de versiones en prints y docstrings (v3.5 → v3.6)
"""
from pathlib import Path
from typing import Dict, Any, List
import axioma_doc_analysis_passes as passes

# ==================== MÉTODOS DELEGADOS ====================
def print_summary(analyzer):
    """Imprime resumen de análisis."""
    print("✅ Análisis completado v3.6 (FlowMapper):")
    print(f"   - {analyzer.stats.total_dirs} directorios")
    print(f"   - {analyzer.stats.python_files} archivos Python")
    print(f"   - {analyzer.stats.total_classes} clases")
    print(f"   - {analyzer.stats.total_functions} funciones (incl. métodos)")
    print(f"   - {len(analyzer.ai_models)} modelos IA detectados")
    print(f"   - {len(analyzer.module_dependencies)} módulos con dependencias")
    print(f"   - {len(analyzer.critical_flows)} flujos críticos documentados")
    if analyzer._use_ollama:
        enriched_count = sum(
            1 for classes in analyzer.enriched_classes.values()
            for c in classes if c.ai_summary
        )
        print(f"   - {enriched_count} clases enriquecidas con IA")
    print()
    print("📈 Rendimiento (v3.6):")
    print(f"   - Archivos analizados: {analyzer.analysis_stats['analyzed_files']}")

def build_analysis_stats(analyzer) -> Dict[str, Any]:
    """Construye diccionario de estadísticas completas."""
    return {
        **analyzer.analysis_stats,
        'total_dirs': analyzer.stats.total_dirs,
        'python_files': analyzer.stats.python_files,
        'total_lines': analyzer.stats.total_lines,
        'total_classes': analyzer.stats.total_classes,
        'total_functions': analyzer.stats.total_functions,
        'ai_models': len(analyzer.ai_models),
        'memory_systems': len(analyzer.memory_systems),
        'task_types': len(analyzer.task_types),
        'module_dependencies': len(analyzer.module_dependencies),
        'critical_flows': len(analyzer.critical_flows),
        'duplications_total': len(analyzer.duplications),
        'duplications_legitimate': len([d for d in analyzer.duplications if d.is_legitimate]),
        'duplications_problematic': len([d for d in analyzer.duplications if not d.is_legitimate]),
    }

# ==================== DELEGADOS DE COMPATIBILIDAD ====================
# Mantenidos para consumidores externos (axioma_doc_main.py)
def scan_files_recursive(analyzer) -> List[Path]:
    return passes.scan_files_recursive(analyzer)

def analyze_file(analyzer, filepath: Path):
    passes.analyze_file(analyzer, filepath)

def extract_enriched_classes(analyzer, tree, content: str, filepath: str):
    return passes.extract_enriched_classes(analyzer, tree, content, filepath)

def infer_purpose_rich(path: str, content: str, docstring: str,
                       classes: List[str], functions: List[str]) -> str:
    return passes.infer_purpose_rich(path, content, docstring, classes, functions)

def enrich_with_ai(analyzer):
    passes.enrich_with_ai(analyzer)

def analyze_dependencies(analyzer):
    passes.analyze_dependencies(analyzer)

def analyze_module_dependencies(analyzer):
    passes.analyze_module_dependencies(analyzer)

def build_critical_flows(analyzer):
    passes.build_critical_flows(analyzer)

def analyze_memory_system(analyzer):
    passes.analyze_memory_system(analyzer)

def detect_proto_files(analyzer):
    passes.detect_proto_files(analyzer)

def analyze_database_schema(analyzer):
    passes.analyze_database_schema(analyzer)

def analyze_configurations(analyzer):
    passes.analyze_configurations(analyzer)

def detect_duplications(analyzer):
    passes.detect_duplications(analyzer)

def categorize_file_purpose(filepath: str) -> str:
    return passes._categorize_file_purpose(filepath)

def analyze_duplication_legitimacy(class_name: str, definitions: List):
    return passes._analyze_duplication_legitimacy(class_name, definitions)

def analyze_data_contracts(analyzer):
    passes.analyze_data_contracts(analyzer)

def detect_env_vars(analyzer):
    passes.detect_env_vars(analyzer)

def analyze_task_types(analyzer):
    passes.analyze_task_types(analyzer)

def analyze_class_hierarchy(analyzer):
    passes.analyze_class_hierarchy(analyzer)
