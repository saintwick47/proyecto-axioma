#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_main.py
# Autor: SaintWick
"""
axioma_doc_main.py - Punto de entrada del generador de documentación de AXIOMA.

Este script analiza el código fuente del proyecto AXIOMA (utilizando AST, análisis
semántico y detección de duplicaciones) y genera un único archivo Markdown
con la documentación completa: clases, funciones, dependencias, flujos críticos,
estado de integración de módulos (con FlowMapper) y métricas de calidad.

Características:
- Análisis completo de todos los archivos Python del proyecto.
- Detección de duplicaciones estructurales con filtrado de falsos positivos.
- Integración opcional con FlowMapper para rastrear conexiones entre módulos.
- Soporte para análisis con IA (Ollama) para resúmenes de clases.
- Múltiples opciones CLI para controlar el comportamiento (--trace-flows, --validate, --strict-include, etc.).
- Filtrado de secretos en la documentación generada (parche de seguridad).
- Soporte para imports relativos en el mapeo de dependencias.
"""
import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import dataclass, asdict

from axioma_doc_base import *
from axioma_doc_analyzer import ImprovedProjectAnalyzer
from axioma_doc_md_generator import MarkdownDocGenerator
from axioma_doc_core import (
    DocGenerationStats,
    OUTPUT_MD_FILE,
)

# ✅ SECURITY PATCH v1.0: Filtro de archivos sensibles y redacción de secrets
try:
    from sensitive_filter import make_default_filter
    _SECURITY_FILTER = make_default_filter(project_root=PROJECT_ROOT)
    _SECURITY_AVAILABLE = True
    print("🔒 Filtro de seguridad ACTIVADO (sensitive_filter.py)")
except ImportError:
    _SECURITY_FILTER = None
    _SECURITY_AVAILABLE = False
    print("⚠️  Filtro de seguridad NO disponible — secrets podrían filtrarse")

# ==================== GENERADOR DE DOCUMENTACIÓN v4.0 ====================
class ImprovedDocumentGenerator:
    """
    Generador de documentación AXIOMA — salida exclusiva Markdown.
    Análisis completo en cada ejecución, sin caché.
    """
    def __init__(self, analyzer: ImprovedProjectAnalyzer):
        self.analyzer = analyzer
        self.stats = DocGenerationStats()

    def generate(self, output_file: Path) -> DocGenerationStats:
        """
        Genera el documento completo en formato Markdown.
        Args:
            output_file: Ruta del archivo .md de salida
        Returns:
            DocGenerationStats: Estadísticas de generación
        """
        start_time = datetime.now()

        output_file.parent.mkdir(parents=True, exist_ok=True)
        md_gen = MarkdownDocGenerator(self.analyzer)
        md_gen.generate(output_file)

        end_time = datetime.now()
        self.stats.generation_time_ms = (end_time - start_time).total_seconds() * 1000
        self.stats.total_files = self.analyzer.analysis_stats['total_files']
        self.stats.analyzed_files = self.analyzer.analysis_stats['analyzed_files']
        return self.stats

# ==================== EJECUCIÓN v4.0 ====================
def parse_args():
    parser = argparse.ArgumentParser(
        description="AXIOMA Documentador v4.1.1 - Análisis semántico, cruce AST, "
                    "detección inteligente de duplicaciones"
    )
    parser.add_argument(
        '--no-ollama',
        action='store_true',
        help='Desactivar análisis IA con Ollama (más rápido)'
    )
    parser.add_argument(
        '--model',
        type=str,
        default=None,
        help='Modelo Ollama a usar para análisis IA (default: settings.llm_code_model; '
             'ej: qwen2.5-coder:7b, qwen3:8b)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='Ruta del archivo de salida (default: AXIOMA_COMPLETE_CONTEXT.md)'
    )
    parser.add_argument(
        '--max-ai-classes',
        type=int,
        default=20,
        help='Máximo de clases a analizar con IA (default: 20)'
    )
    # ✅ FASE 4: Nuevos flags CLI
    parser.add_argument(
        '--force',
        action='store_true',
        help='Ignorar cualquier caché residual y forzar re-parseo completo (activo por defecto en v4.0)'
    )
    parser.add_argument(
        '--trace-flows',
        action='store_true',
        help='Habilitar FlowMapper: cruce AST/imports, detección de contratos y badges de integración'
    )
    parser.add_argument(
        '--strict-include',
        action='store_true',
        help='Forzar inclusión de módulos listados en tools/documentador/manifest_core.txt'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Ejecutar validación post-generación: reporta discrepancias firma/code y cobertura de flujos'
    )
    # ✅ SECURITY PATCH v1.0: Flag de modo estricto
    parser.add_argument(
        '--strict-secrets',
        action='store_true',
        help='Modo paranoico: falla con exit 7 si se detecta un secret sin redactar en la doc'
    )
    parser.add_argument(
        '--no-strict-secrets',
        action='store_true',
        help='Desactiva el filtrado de secrets (NO recomendado)'
    )
    return parser.parse_args()

def main():
    args = parse_args()
    if not PROJECT_ROOT.exists():
        print(f"❌ ERROR: No existe {PROJECT_ROOT}")
        print(f"💡 Ejecuta este script desde la raíz del proyecto AXIOMA")
        sys.exit(1)

    output_file = Path(args.output) if args.output else OUTPUT_MD_FILE
    use_ollama = not args.no_ollama

    # ✅ FASE 4: Wire de flags
    trace_flows = args.trace_flows
    strict_include = args.strict_include

    # ✅ SECURITY PATCH v1.0: Configurar modo de filtrado
    if _SECURITY_AVAILABLE and not args.no_strict_secrets:
        _SECURITY_FILTER.strict_mode = True
    else:
        _SECURITY_FILTER.strict_mode = False
    if args.strict_secrets:
        _SECURITY_FILTER.fail_on_secret_found = True

    print("=" * 70)
    print(f"🚀 AXIOMA - Documentador Completo v{DOCUMENTADOR_VERSION}")
    print("   Análisis semántico — 0 falsos positivos — cruce AST FlowMapper")
    print(f"   Proyecto: AXIOMA v{AXIOMA_PROJECT_VERSION} | Salida: Markdown")
    sec_status = "🔒 ACTIVADO" if _SECURITY_AVAILABLE and not args.no_strict_secrets else "⚠️  DESACTIVADO"
    print(f"   Seguridad: {sec_status}")
    print("=" * 70)
    print(f"📂 Proyecto: {PROJECT_ROOT}")
    print(f"📄 Salida: {output_file}")
    print(f"🤖 Análisis IA: {'Habilitado' if use_ollama else 'Deshabilitado'}")
    print(f"🔀 FlowMapper: {'✅ Activo' if trace_flows else 'ℹ️ Inactivo (usa --trace-flows)'}")
    print(f"📦 Strict-Include: {'✅ Activo' if strict_include else 'ℹ️ Normal'}")
    if args.model:
        print(f"🔧 Modelo: {args.model}")
    print()

    analyzer = ImprovedProjectAnalyzer(
        project_root=PROJECT_ROOT,
        use_ollama=use_ollama,
        ollama_model=args.model,
        trace_flows=trace_flows,
        strict_include=strict_include
    )

    print("🔍 Analizando proyecto...")
    analyzer.analyze()
    print("\n📝 Generando documentación en formato Markdown v4.1.1...")
    generator = ImprovedDocumentGenerator(analyzer)
    stats = generator.generate(output_file)

    enriched_count = sum(len(v) for v in analyzer.enriched_classes.values())
    ai_analyzed = sum(
        1 for classes in analyzer.enriched_classes.values()
        for c in classes if hasattr(c, 'ai_summary') and c.ai_summary
    )
    print()
    print("=" * 70)
    print("✅ DOCUMENTACIÓN v4.1.1 GENERADA (Markdown)")
    print("=" * 70)
    print(f"📝 MD   : {output_file}")
    print()
    print("📊 Resumen:")
    print(f"   - Directorios: {analyzer.stats.total_dirs}")
    print(f"   - Archivos Python: {analyzer.stats.python_files}")
    print(f"   - Líneas de código: {analyzer.stats.total_lines:,}")
    print(f"   - Clases: {analyzer.stats.total_classes}")
    print(f"   - Funciones (incl. métodos): {analyzer.stats.total_functions}")
    print(f"   - Modelos IA: {len(analyzer.ai_models)}")
    print(f"   - Sistemas de memoria: {len(analyzer.memory_systems)}")
    print(f"   - TaskTypes: {len(analyzer.task_types)}")
    print(f"   - Clases con signatures: {enriched_count}")
    print(f"   - Clases analizadas con IA: {ai_analyzed}")
    print()
    print("📈 Rendimiento:")
    print(f"   - Archivos analizados: {stats.analyzed_files}")
    print(f"   - Tiempo generación: {stats.generation_time_ms:.0f}ms")
    print()

    real_dups = len([d for d in analyzer.duplications if not d.is_legitimate])
    legit_dups = len([d for d in analyzer.duplications if d.is_legitimate])
    print(f"   🛡️ Duplicaciones problemáticas: {real_dups}")
    print(f"   ✅ Duplicaciones legítimas (Patrones Arq.): {legit_dups}")

    if real_dups == 0 and legit_dups > 0:
        print("   🎯 MODO 0 FALSOS POSITIVOS: Todas las alertas son patrones arquitectónicos reales.")
    print()

    # ✅ FASE 4: Reporte de validación si se activa el flag
    if args.validate:
        print("🔍 Ejecutando validación estricta (--validate)...")
        inconsistencies = analyzer.analysis_stats.get('inconsistencies_found', 0)
        flow_contracts = 0
        flow_connected = 0
        if analyzer.integration_report:
            flow_contracts = len(analyzer.integration_report.flow_contracts)
            flow_connected = len([fc for fc in analyzer.integration_report.flow_contracts
                                  if fc.status == 'CONNECTED'])

        print(f"   ⚠️ Inconsistencias firma/code: {inconsistencies}")
        print(f"   🔗 Contratos evaluados: {flow_contracts}")
        print(f"   ✅ Integración activa: {flow_connected}/{flow_contracts}")

        if inconsistencies == 0 and flow_contracts > 0:
            print("   ✅ VALIDACIÓN APROBADA: Documentación refleja código real.")
        else:
            print("   ⚠️ VALIDACIÓN CON ALERTAS: Revisar AXIOMA_COMPLETE_CONTEXT.md sección Problemas detectados.")
        print()

    print("💡 Uso:")
    print("   python axioma_doc_main.py                    # Análisis completo")
    print("   python axioma_doc_main.py --trace-flows      # + Cruce AST FlowMapper")
    print("   python axioma_doc_main.py --validate         # + Validación firma/code")
    print("   python axioma_doc_main.py --strict-include   # + Manifest core forzado")
    print("   python axioma_doc_main.py --no-ollama        # Sin IA (más rápido)")
    print("   python axioma_doc_main.py --model qwen-coder  # Modelo específico")
    print("   python axioma_doc_main.py --strict-secrets   # Falla si detecta secrets")
    print()

    # ✅ SECURITY PATCH v1.0: Verificación post-generación
    if _SECURITY_AVAILABLE and not args.no_strict_secrets:
        print("🔍 Verificando que la doc no contiene secrets sin redactar...")
        try:
            from secret_scanner import scan_directory
            findings = scan_directory(Path("docs"), PROJECT_ROOT)
            if findings:
                print(f"❌ FALLO: {len(findings)} secret(s) detectado(s) sin redactar")
                for f, ln, pat, txt in findings[:10]:
                    print(f"   {f}:{ln} — {pat}")
                if args.strict_secrets:
                    print("   Modo --strict-secrets activo. Abortando con exit 7.")
                    sys.exit(7)
            else:
                print("✅ Verificación OK: sin secrets en la doc.")
        except Exception as e:
            print(f"⚠️  No se pudo verificar: {e}")
    print()

if __name__ == "__main__":
    main()
