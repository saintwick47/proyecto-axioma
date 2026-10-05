#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_generator_sections.py
# Autor: SaintWick
"""
axioma_doc_generator_sections.py - Generador de secciones de documentación
Parte del documentador de AXIOMA v3.9.0
✅ Ruta: /ruta/a/axioma/tools/documentador/
✅ MEJORAS v3.2: Soporte para caché, análisis diferencial, validación de consistencia
✅ MEJORAS v3.3: Contratos de datos expandidos, mapa de dependencias, flujos con puntos de fallo
✅ MEJORAS v3.4: Configuración con impacto, ejemplos reales de AXIOMA, investigación prioritaria
✅ MEJORAS v3.5: Actualizado a AXIOMA v0.3.5 (Tests 100% Pass Rate + Calidad 0.93)
✅ MEJORAS v3.6 (FASE 2-3):
   - Integración FlowMapper: badges [✅ CONECTADO]/[⚠️ AISLADO]
   - Nuevas secciones: Contexto/Flujo, Integración Asíncrona, Estado de Conexión
✅ MEJORAS v3.8: Análisis Universal Automático, Zero Maintenance, listas negras
✅ CORREGIDO v3.9.0:
   - BUG F: _format_enriched_class_toon() y generate_key_components_section() ahora
             documentan métodos internos relevantes (_security_audit_defensive,
             _extract_score_from_response, _build_validation_prompt_with_critique, etc.)
             para clases con complejidad >= 3 o con >= 2 métodos privados con docstring.
             Antes solo mostraba métodos públicos y __init__, ocultando la lógica más
             valiosa de cada agente.
   - BUG E: Usa info.integration_badge en lugar de parsear el prefijo de info.purpose.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple
from axioma_doc_base import *
from axioma_doc_analyzer import ImprovedProjectAnalyzer, FunctionSignature, EnrichedClassInfo, ParamInfo

try:
    from axioma_doc_flow_mapper import IntegrationReport, get_integration_badge_for_symbol, map_project_flows
    FLOW_MAPPER_AVAILABLE = True
except ImportError:
    FLOW_MAPPER_AVAILABLE = False

# ── Mixin: segunda mitad de secciones (refactorización v3.9.1) ──
from axioma_doc_generator_sections_ext import DocumentSectionGeneratorExt


# ==================== UMBRAL PARA DOCUMENTAR MÉTODOS INTERNOS ====================
# Clases con complejidad >= este valor muestran también sus métodos privados relevantes
_INTERNAL_METHODS_COMPLEXITY_THRESHOLD = 3
# Mínimo de métodos privados con docstring para activar la sección interna
_INTERNAL_METHODS_MIN_WITH_DOCSTRING = 2


# ==================== GENERADOR DE SECCIONES v3.9.0 ====================

class DocumentSectionGenerator(DocumentSectionGeneratorExt):
    """Generador de secciones individuales de documentación v3.9.0"""

    def __init__(self, analyzer: ImprovedProjectAnalyzer):
        self.analyzer = analyzer

        self._global_target_symbols = set()
        for classes in self.analyzer.enriched_classes.values():
            for ec in classes:
                self._global_target_symbols.add(ec.name)

        if hasattr(self.analyzer, 'module_functions'):
            for sigs in self.analyzer.module_functions.values():
                for sig in sigs:
                    if not sig.name.startswith('_'):
                        self._global_target_symbols.add(sig.name)

    # ==================== UTILIDADES DE FORMATO ====================

    def _format_signature(self, sig: FunctionSignature, indent: str = "") -> str:
        prefix = "async def" if sig.is_async else "def"
        decorators_str = ""
        if sig.decorators:
            decorators_str = "".join(f"{indent}@{d}\n" for d in sig.decorators)
        params_parts = []
        for p in sig.params:
            param_str = p.name
            if p.type_hint:
                param_str += f": {p.type_hint}"
            if p.default:
                param_str += f" = {p.default}"
            params_parts.append(param_str)
        params_str = ", ".join(params_parts)
        return_str = f" -> {sig.return_type}" if sig.return_type else ""
        return f"{decorators_str}{indent}{prefix} {sig.name}({params_str}){return_str}"

    def _format_param_table_toon(self, params: list) -> str:
        if not params:
            return ""
        rows = []
        for p in params:
            type_str = p.type_hint if p.type_hint else "-"
            default_str = p.default if p.default else "-"
            rows.append(f"{p.name}|{type_str}|{default_str}")
        header = "PARAMS|Nombre|Tipo|Default"
        return header + "\n" + "\n".join(rows)

    def _build_dependency_graph(self) -> Dict[str, Set[str]]:
        EXT_DEPS = {
            'chromadb', 'sqlite3', 'fastapi', 'pydantic', 'ollama',
            'torch', 'sklearn', 'httpx', 'aiohttp', 'requests',
        }
        def _group(dotted: str) -> str:
            parts = dotted.split('.')
            if len(parts) >= 2 and parts[0] == 'src':
                return parts[1]
            return parts[0]
        dependency_graph: Dict[str, Set[str]] = {}
        for filepath, info in self.analyzer.files_info.items():
            module_dotted = filepath.replace('/', '.').replace('.py', '')
            group = _group(module_dotted)
            if group not in dependency_graph:
                dependency_graph[group] = set()
            for imp in info.imports:
                if imp.startswith('src.'):
                    dep = _group(imp)
                    if dep != group:
                        dependency_graph[group].add(dep)
                else:
                    base = imp.split('.')[0]
                    if base in EXT_DEPS:
                        dependency_graph[group].add(f"ext:{base}")
        return dependency_graph

    def _identify_critical_paths(self) -> List[Dict[str, Any]]:
        if self.analyzer.critical_flows:
            result = []
            for cf in self.analyzer.critical_flows:
                steps = []
                for s in cf.steps:
                    if isinstance(s, dict):
                        steps.append(s)
                    else:
                        steps.append({
                            'step_number': getattr(s, 'step_number', 0),
                            'component': getattr(s, 'component', ''),
                            'fallback': getattr(s, 'fallback', None),
                            'timeout': getattr(s, 'timeout', None),
                        })
                result.append({
                    'name': cf.name,
                    'description': cf.description,
                    'steps': steps,
                    'failure_points': cf.failure_points,
                })
            return result
        from axioma_doc_base import CRITICAL_FLOWS_DEFINITION
        return [
            {
                'name': fd['name'],
                'description': fd['description'],
                'steps': fd['steps'],
                'failure_points': fd['failure_points'],
            }
            for fd in CRITICAL_FLOWS_DEFINITION
        ]

    def _get_integration_badge(self, symbol: str) -> str:
        """✅ Retorna badge de integración para un símbolo."""
        if not FLOW_MAPPER_AVAILABLE:
            return "[❓ SIN RASTREO]"
        return (
            get_integration_badge_for_symbol(symbol, self.analyzer.integration_report)
            if self.analyzer.integration_report
            else "[❓ PENDIENTE]"
        )

    def _get_file_badge(self, rel_path: str) -> str:
        """
        ✅ BUG E FIX v3.9.0: Lee el badge desde info.integration_badge en lugar
        de parsear el prefijo de info.purpose.
        """
        info = self.analyzer.files_info.get(rel_path)
        if info is None:
            return ""
        # Campo nuevo (v3.9.0)
        if hasattr(info, 'integration_badge'):
            return info.integration_badge
        # Fallback legacy: parsear purpose si aún tiene el prefijo
        for prefix in ("[✅ CONECTADO]", "[⚠️ AISLADO]"):
            if info.purpose.startswith(prefix):
                return prefix
        return ""

    # ==================== BUG F: MÉTODOS INTERNOS RELEVANTES ====================

    def _has_relevant_internal_methods(self, ec: EnrichedClassInfo) -> bool:
        """
        ✅ BUG F FIX v3.9.0: Determina si una clase tiene métodos internos
        que vale la pena documentar.

        Criterio: clase con complejidad >= umbral O con >= N métodos privados
        que tengan docstring (indica intencionalidad, no solo helpers triviales).
        """
        file_info = self.analyzer.files_info.get(ec.file)
        complexity = getattr(file_info, 'complexity', 0) if file_info else 0
        if complexity >= _INTERNAL_METHODS_COMPLEXITY_THRESHOLD:
            return True
        private_with_doc = [
            m for m in ec.methods
            if m.name.startswith('_')
            and m.name != '__init__'
            and not m.name.startswith('__')
            and m.docstring
        ]
        return len(private_with_doc) >= _INTERNAL_METHODS_MIN_WITH_DOCSTRING

    def _format_internal_methods_toon(self, ec: EnrichedClassInfo) -> str:
        """
        ✅ BUG F FIX v3.9.0: Genera bloque TOON de métodos internos relevantes.

        Incluye métodos privados (prefijo _) que tengan docstring, excluyendo
        dunders (__str__, __repr__, etc.) que no aportan valor documental.
        Limita a 10 métodos para no inflar el documento.
        """
        internal = [
            m for m in ec.methods
            if m.name.startswith('_')
            and m.name != '__init__'
            and not m.name.startswith('__')
            and m.docstring
        ]
        if not internal:
            return ""

        output = "Metodos_Internos_Relevantes\n"
        for method in internal[:10]:
            output += self._format_method_detail_toon(method)
        if len(internal) > 10:
            output += f"[NOTA]+{len(internal) - 10} métodos internos adicionales (ver código fuente)\n"
        return output
    # ==================== FORMATO DE CLASES ====================

    def _format_enriched_class_toon(self, ec: EnrichedClassInfo) -> str:
        """
        ✅ BUG F FIX v3.9.0: Formatea una clase enriquecida con todo su detalle.
        Ahora incluye métodos internos relevantes para clases con complejidad >= 3.
        ✅ FIX 1: Campos Pydantic con validaciones Field()
        ✅ FIX 4: Expansión de listas de seguridad
        """
        output = f"[CLASS]{ec.name}"
        if ec.bases:
            output += f"|Hereda:{', '.join(ec.bases)}"
        output += "\n"
        tags = []
        if ec.is_dataclass: tags.append("dataclass")
        if ec.is_enum: tags.append("enum")
        if ec.is_pydantic: tags.append("Pydantic")
        if ec.has_fallback_marker: tags.append("fallback")
        if tags:
            output += f"Tipo|{' | '.join(tags)}\n"
        badge = self._get_integration_badge(ec.name)
        output += f"Estado_Integracion|{badge}\n"
        output += f"Linea|{ec.line_number}\n"
        output += f"Archivo|{ec.file}\n"
        if ec.docstring:
            doc = ec.docstring[:300] + ('...' if len(ec.docstring) > 300 else '')
            output += f"Docstring|{doc}\n"
        if ec.ai_summary:
            output += f"Analisis_IA|{ec.ai_summary}\n"
        if ec.attributes:
            output += "Atributos\n"
            output += "ATTR|Nombre|Tipo\n"
            for attr_name, attr_type in ec.attributes[:15]:
                type_str = attr_type if attr_type else "-"
                output += f"ROW|{attr_name}|{type_str}\n"
            if len(ec.attributes) > 15:
                output += f"ROW|... {len(ec.attributes) - 15} más|-\n"
            output += "END_ATTR\n"

        # ✅ FIX 1: Campos Pydantic con validaciones
        if hasattr(ec, 'pydantic_fields') and ec.pydantic_fields:
            output += "Campos_Pydantic\n"
            output += "PYDANTIC_FIELD|Nombre|Tipo|Default|Constraints|Descripción\n"
            for pf in ec.pydantic_fields:
                constraints_str = ""
                if pf.get('constraints'):
                    constraints_str = ", ".join(f"{k}={v}" for k, v in pf['constraints'].items())
                default_str = str(pf.get('default', '-'))[:50]
                desc_str = pf.get('description', '')[:80]

                # ✅ FIX 4: Marcar listas de seguridad
                security_marker = "🔒 " if pf.get('is_security_list') else ""

                output += f"ROW|{security_marker}{pf['name']}|{pf['type_hint']}|{default_str}|{constraints_str}|{desc_str}\n"

                # ✅ FIX 4: Expandir listas de seguridad
                if pf.get('is_security_list') and pf.get('security_values'):
                    output += f"SECURITY_LIST|{pf['name']}|{len(pf['security_values'])} elementos\n"
                    for val in pf['security_values'][:15]:
                        output += f"SEC_ITEM|{val}\n"
                    if len(pf['security_values']) > 15:
                        output += f"SEC_ITEM|... +{len(pf['security_values']) - 15} más\n"

            output += "END_PYDANTIC\n"

        # Métodos públicos
        public_methods = [
            m for m in ec.methods
            if not m.name.startswith('_') or m.name == '__init__'
        ]
        if public_methods:
            output += "Metodos_Publicos\n"
            for method in public_methods:
                output += self._format_method_detail_toon(method)

        # ✅ BUG F FIX: Métodos internos relevantes
        if self._has_relevant_internal_methods(ec):
            output += self._format_internal_methods_toon(ec)

        output += "[END_CLASS]\n"
        return output

    def _format_method_detail_toon(self, sig: FunctionSignature) -> str:
        output = ""
        method_prefix = "async" if sig.is_async else ""

        # ✅ FIX 3: Detectar Lazy Init
        is_lazy = 'LAZY_INIT' in getattr(sig, 'tags', [])
        lazy_marker = "🔄 LAZY " if is_lazy else ""

        if sig.decorators:
            dec_str = " ".join(f"@{d}" for d in sig.decorators)
            output += f"[METHOD]{sig.name}|{dec_str}|{method_prefix}|{lazy_marker}\n"
        else:
            output += f"[METHOD]{sig.name}||{method_prefix}|{lazy_marker}\n"
        formatted = self._format_signature(sig, indent='')
        output += f"Signature|{formatted.replace(chr(10), ' ')}\n"

        # ✅ FIX 3: Si es Lazy, mostrar info de la clase instanciada
        if is_lazy and sig.ai_summary and 'Lazy init' in sig.ai_summary:
            output += f"Patron|{sig.ai_summary}\n"
        elif sig.docstring:
            short_doc = sig.docstring.split('\n')[0][:150]
            output += f"Docstring|{short_doc}\n"
        if sig.ai_summary and 'Lazy init' not in sig.ai_summary:
            output += f"Analisis_IA|{sig.ai_summary}\n"

        typed_params = [p for p in sig.params if p.type_hint]
        if typed_params and len(sig.params) > 0:
            output += self._format_param_table_toon(sig.params) + "\n"
        if sig.return_type:
            output += f"Returns|{sig.return_type}\n"

        # ✅ FIX 5: Mostrar notas de changelog inline
        if hasattr(sig, 'changelog_notes') and sig.changelog_notes:
            for note in sig.changelog_notes[:3]:
                if isinstance(note, tuple) and len(note) >= 2:
                    output += f"Changelog|{note[0]}: {note[1]}\n"
                else:
                    output += f"Changelog|{note}\n"

        output += "[END_METHOD]\n"
        return output
    # ==================== SECCIONES PRINCIPALES ====================

    def generate_header(self) -> str:
        ai_status = "✅ Con análisis IA (Ollama)" if self.analyzer._use_ollama else "ℹ️ Sin análisis IA"
        model_name = self.analyzer.ollama.model if self.analyzer.ollama else "N/A"
        cache_info = ""
        if hasattr(self.analyzer, 'analysis_stats'):
            stats = self.analyzer.analysis_stats
            if stats.get('total_files', 0) > 0:
                hit_rate = (stats.get('cached_files', 0) / stats['total_files']) * 100
                cache_info = f"\n[CACHE]Archivos_Cacheados|{stats.get('cached_files', 0)}|Hit_Rate|{hit_rate:.1f}%"

        flow_mapper_info = ""
        if self.analyzer.integration_report:
            conn = len(self.analyzer.integration_report.connected_modules)
            iso = len(self.analyzer.integration_report.isolated_modules)
            flow_mapper_info = f"\n[FLOWMAPPER]✅_Cruce_AST_Dinámico_v3.9|Conectados|{conn}|Aislados|{iso}|Simbolos_Rastreados|{len(self._global_target_symbols)}"

        return f"""[HEADER]AXIOMA_Documentation_v{DOCUMENTADOR_VERSION}
[TIMESTAMP]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
[VERSION]{DOCUMENTADOR_VERSION} (AXIOMA_v{AXIOMA_PROJECT_VERSION} — Análisis_Universal + Métodos_Internos + Badges_Reales)
[AI_ANALYSIS]{ai_status}{f" | Modelo: {model_name}" if self.analyzer._use_ollama else ""}
[CACHE_FEATURES]✅_Hash_SHA256|✅_Diferencial|✅_Validacion_Signatures|✅_FlowMapper_v3.9{cache_info}{flow_mapper_info}
[INDEX]
1|Resumen_Ejecutivo
2|Archivos_Principales
3|Análisis_Detallado_Clases
4|Problemas_Críticos
5|Componentes_Clave_Sistema
6|TaskTypes_Disponibles
7|Contexto_y_Flujo_Datos
8|Integración_Asíncrona
9|Flujos_Detallados_Con_Puntos_Fallo
10|Jerarquía_Clases
11|Sistema_Memoria
12|Modelos_IA_Detectados
13|Dependencias_Externas
14|Esquema_Base_Datos
15|Contratos_Datos_Expandidos
16|Mapa_Dependencias_Módulos
17|Parámetros_Configuración_Con_Impacto
18|Ejemplos_Uso_Reales
19|Arquitectura
20|Estadísticas
21|Rendimiento_Cache
22|Estado_Conexión_Módulos
23|Investigación_Prioritaria
[END_HEADER]
"""

    def generate_overview(self) -> str:
        cache_stats = ""
        if hasattr(self.analyzer, 'analysis_stats'):
            stats = self.analyzer.analysis_stats
            cache_stats = f"""
[SUBSECTION]Rendimiento_Cache_v3.9
STATS|Metrica|Valor
Archivos_Totales|{stats.get('total_files', 0)}
Archivos_en_Cache|{stats.get('cached_files', 0)}
Archivos_Analizados|{stats.get('analyzed_files', 0)}
Inconsistencias_Detectadas|{stats.get('inconsistencies_found', 0)}
Variables_Entorno_Detectadas|{stats.get('env_vars_total', len(self.analyzer.env_vars))}
"""
            if stats.get('total_files', 0) > 0:
                hit_rate = (stats.get('cached_files', 0) / stats['total_files']) * 100
                cache_stats += f"Cache_Hit_Rate|{hit_rate:.1f}%\n"

        flow_stats = ""
        if self.analyzer.integration_report:
            flow_stats = f"""
[SUBSECTION]Estado_Integración_FlowMapper_v3.9
Modo|100%_Automático_Universal
Simbolos_Dinámicos_Rastreados|{len(self._global_target_symbols)}
Contratos_Evaluados|{len(self.analyzer.integration_report.flow_contracts)}
Módulos_Conectados|{len(self.analyzer.integration_report.connected_modules)}
Módulos_Aislados|{len(self.analyzer.integration_report.isolated_modules)}
"""
            for fc in self.analyzer.integration_report.flow_contracts:
                flow_stats += f"CONTRATO|{fc.name}|{fc.badge}\n"

        return f"""[SECTION]Resumen_Ejecutivo
[SUBSECTION]Qué_es_AXIOMA
AXIOMA es un sistema de agente IA multi-modelo que:
1|Orquesta múltiples modelos locales (Ollama) según la tarea
2|Mantiene memoria persistente usando ChromaDB + SQLite + Embeddings
3|Aprende de interacciones con clasificadores ML entrenables
4|Ejecuta código Python de forma segura
5|Integra APIs externas (búsqueda web, datos financieros, etc.)
6|Valida respuestas con Chain of Thought forzado
7|Investiga automáticamente en GitHub/docs ante errores
[SUBSECTION]Arquitectura_Alto_Nivel
Usuario (GUI/CLI)
│
▼
┌──────────────────────┐
│   Dispatcher         │ ← Punto de entrada (reemplaza Orchestrator)
└──────┬───────────────┘
│
├─→ Smart Router ────→ Selección de modelo
├─→ Data Handler ────→ Clima / Finanzas / Hora / Noticias
├─→ Memory Gateway ──→ ChromaDB + SQLite + Cache
├─→ LLM Client ──────→ OllamaHandler
└─→ Agents ──────────→ Coder, Researcher, Analyst, Validator
[SUBSECTION]Estadísticas_Proyecto_v{AXIOMA_PROJECT_VERSION}
STATS|Metrica|Valor
Directorios|{self.analyzer.stats.total_dirs}
Archivos_Python|{self.analyzer.stats.python_files}
Lineas_Codigo|{self.analyzer.stats.total_lines:,}
Clases_Totales|{self.analyzer.stats.total_classes}
Funciones_Totales (incl. métodos)|{self.analyzer.stats.total_functions}
Modelos_IA_Detectados|{len(self.analyzer.ai_models)}
Sistemas_Memoria|{len(self.analyzer.memory_systems)}
TaskTypes_Definidos|{len(self.analyzer.task_types)}
Variables_Entorno|{len(self.analyzer.env_vars)}
Clases_Signatures_Extraidas|{sum(len(v) for v in self.analyzer.enriched_classes.values())}
Clases_Analisis_IA|{sum(1 for classes in self.analyzer.enriched_classes.values() for c in classes if c.ai_summary)}
Tests_Pass_Rate|100% (medido en v0.3.6)
Calidad_Promedio|0.93 (medido en v0.3.6)
{cache_stats}{flow_stats}
[END_SECTION]
"""

    def generate_main_files_section(self) -> str:
        output = """[SECTION]Archivos_Principales
[SUBSECTION]Top_30_Archivos_por_Lineas
TABLE|Rank|Ruta_Completa|Lineas|Categoria|Badge|Propósito
"""
        sorted_files = sorted(
            self.analyzer.files_info.items(),
            key=lambda x: x[1].lines,
            reverse=True
        )[:30]
        for idx, (path, info) in enumerate(sorted_files, 1):
            # ✅ BUG E FIX: badge desde campo separado, propósito limpio
            badge = self._get_file_badge(path)
            purpose = info.purpose
            for prefix in ("[✅ CONECTADO] ", "[⚠️ AISLADO] "):
                if purpose.startswith(prefix):
                    purpose = purpose[len(prefix):]
            output += f"ROW|{idx}|{path}|{info.lines}|{info.category}|{badge}|{purpose}\n"
        output += "[SUBSECTION]Detalles_Archivos_Criticos\n"
        for path, info in sorted_files[:5]:
            badge = self._get_file_badge(path)
            output += f"[FILE]{path}|{badge}\n"
            output += f"Categoria|{info.category}\n"
            output += f"Lineas|{info.lines}\n"
            if info.last_modified:
                output += f"Ultima_Modificacion|{info.last_modified.strftime('%Y-%m-%d %H:%M')}\n"
            if info.recent_changes:
                output += f"Cambios_Recientes|{info.recent_changes}\n"
            purpose = info.purpose
            for prefix in ("[✅ CONECTADO] ", "[⚠️ AISLADO] "):
                if purpose.startswith(prefix):
                    purpose = purpose[len(prefix):]
            output += f"Proposito|{purpose}\n"
            output += "Contenido\n"
            output += f"Clases|{len(info.classes)}|{', '.join(info.classes) if info.classes else 'Ninguna'}\n"
            funcs_display = ', '.join(info.functions[:5]) if info.functions else 'Ninguna'
            if len(info.functions) > 5:
                funcs_display += f" + {len(info.functions) - 5} más"
            output += f"Funciones|{len(info.functions)}|{funcs_display}\n"
            output += f"Dataclasses|{', '.join(info.dataclasses) if info.dataclasses else 'Ninguna'}\n"
            output += f"Enums|{', '.join(info.enums) if info.enums else 'Ninguno'}\n"
            # ✅ FIX 2: Constantes de módulo
            if hasattr(info, 'module_constants') and info.module_constants:
                output += f"Constantes_Modulo|{len(info.module_constants)}\n"
                output += "MODULE_CONSTANT|Nombre|Valor|Categoría|Comentario\n"
                for const in info.module_constants[:20]:
                    security_marker = "🔒 " if const.get('is_security') else ""
                    value_display = const.get('literal_value') or const.get('value', '-')
                    if len(str(value_display)) > 80:
                        value_display = str(value_display)[:77] + "..."
                    comment = const.get('comment', '')[:60]
                    output += f"ROW|{security_marker}{const['name']}|{value_display}|{const.get('category', 'general')}|{comment}\n"
                if len(info.module_constants) > 20:
                    output += f"ROW|... +{len(info.module_constants) - 20} más|-|-|-|\n"
            if info.docstring:
                desc = info.docstring[:200] + ('...' if len(info.docstring) > 200 else '')
                output += f"Descripcion|{desc}\n"
            output += "[END_FILE]\n"
        output += "[END_SECTION]\n"
        return output

    def generate_enriched_classes_section(self) -> str:
        output = """[SECTION]Análisis_Detallado_Clases
[DESCRIPTION]Clases clave con signatures completas, atributos, análisis IA y métodos internos relevantes (v3.9.0).
"""
        filtered: List[EnrichedClassInfo] = []
        for classes in self.analyzer.enriched_classes.values():
            for ec in classes:
                public_methods = [m for m in ec.methods if not m.name.startswith('_') or m.name == '__init__']
                has_substance = (
                    len(public_methods) >= 1
                    or ec.ai_summary
                    or len(ec.attributes) >= 2
                )
                if has_substance:
                    filtered.append(ec)

        filtered.sort(key=lambda ec: (ec.file, ec.name))

        by_file: Dict[str, List[EnrichedClassInfo]] = {}
        for ec in filtered:
            if ec.file not in by_file:
                by_file[ec.file] = []
            by_file[ec.file].append(ec)

        total_classes = sum(len(v) for v in self.analyzer.enriched_classes.values())
        output += f"Clases_Documentadas|{len(filtered)} de {total_classes} totales (omitidos stubs vacíos)\n"
        for filepath, classes in by_file.items():
            file_info = self.analyzer.files_info.get(filepath)
            category = file_info.category if file_info else "Unknown"
            badge = self._get_file_badge(filepath)
            output += f"[FILE]{filepath}|{category}|{badge}\n"
            for ec in classes:
                output += self._format_enriched_class_toon(ec)
            output += "[END_FILE]\n"
        output += "[END_SECTION]\n"
        return output

    def generate_critical_issues(self) -> str:
        real_dups = [d for d in self.analyzer.duplications if not d.is_legitimate]
        legit_dups = [d for d in self.analyzer.duplications if d.is_legitimate]
        cache_inconsistencies = ""
        if hasattr(self.analyzer, 'analysis_stats'):
            inconsistencies_count = self.analyzer.analysis_stats.get('inconsistencies_found', 0)
            if inconsistencies_count > 0:
                cache_inconsistencies = f"""
[SUBSECTION]Inconsistencias_Cache_vs_Codigo|{inconsistencies_count}
⚠️_ESTAS_INCONSISTENCIAS_INDICAN_QUE_LA_DOCUMENTACION_NO_COINCIDE_CON_EL_CODIGO
Recomendacion|Ejecutar con --force para regenerar documentación actualizada
"""
        output = "[SECTION]Problemas_Criticos\n"
        if real_dups:
            output += f"[SUBSECTION]Duplicaciones_Problematicas|{len(real_dups)}\n"
            output += "ESTAS_DUPLICACIONES_REQUIEREN_ATENCION\n"
            for dup in real_dups:
                icon = "CRITICO" if dup.severity == "CRÍTICO" else "ALTO"
                output += f"[ISSUE]{dup.class_name}|{icon}\n"
                output += f"Problema|{dup.reason}\n"
                output += f"Ubicaciones|{len(dup.locations)}\n"
                for loc in dup.locations:
                    output += f"  - {loc}\n"
                output += "Recomendacion|Consolidar en un solo archivo y actualizar todos los imports\n"
                output += "[END_ISSUE]\n"
        else:
            output += "[SUBSECTION]Duplicaciones_Problematicas|0\n"
            output += "No se detectaron duplicaciones problemáticas\n"
        if legit_dups:
            output += f"[SUBSECTION]Duplicaciones_Legitimas|{len(legit_dups)}\n"
            output += "Estas NO son problemas - son patrones de fallback correctos\n"
            for dup in legit_dups:
                output += f"[FALLBACK]{dup.class_name}\n"
                output += f"Razon|{dup.reason}\n"
                output += f"Patron|{dup.details.get('pattern', 'N/A')}\n"
                if 'main_definition' in dup.details:
                    output += f"Definicion_Principal|{dup.details['main_definition']}\n"
                if 'fallback_definitions' in dup.details:
                    output += f"Fallbacks|{', '.join([f'{f}' for f in dup.details['fallback_definitions']])}\n"
                output += "[END_FALLBACK]\n"
        if cache_inconsistencies:
            output += cache_inconsistencies
        output += "[END_SECTION]\n"
        return output

    def generate_key_components_section(self) -> str:
        output = """[SECTION]Componentes_Clave_Sistema
[DESCRIPTION]Archivos críticos con métodos públicos e internos relevantes (v3.9.0).
"""
        key_components = {
            'src/router/smart_router.py': 'SmartRouter - Ruteo Inteligente',
            'src/router/classifier.py': 'IntentClassifier - Clasificación de Intención',
            'src/memory/gateway.py': 'MemoryGateway - Gestión de Memoria Unificada',
            'src/llm/client.py': 'OllamaClient - Cliente LLM con Circuit Breaker',
            'src/core/dispatcher.py': 'Dispatcher - Orquestador Principal',
            'src/agents/validator.py': 'Validator - Validación con ForcedCoT',
            'src/agents/coder.py': 'Coder - Generación de Código',
            'src/agents/researcher.py': 'Researcher - Búsqueda Web',
            'config/settings.py': 'Configuración Global',
            'src/domain/types.py': 'TaskTypes - Tipos de Tarea',
            'src/memory/semantic.py': 'Memoria Semántica - ChromaDB',
            'src/router/cache.py': 'RouterCache - Caché 2 Niveles',
        }
        for filepath, component_name in key_components.items():
            if filepath in self.analyzer.files_info:
                info = self.analyzer.files_info[filepath]
                comp_symbol = component_name.split(' ')[0].replace('-', '_')
                badge = self._get_integration_badge(comp_symbol)
                file_badge = self._get_file_badge(filepath)
                output += f"[COMPONENT]{component_name}|{badge}\n"
                output += f"Archivo|{filepath}\n"
                output += f"Badge_Archivo|{file_badge}\n"
                output += f"Categoria|{info.category}|Lineas|{info.lines}|Complejidad|{info.complexity}\n"
                purpose = info.purpose
                for prefix in ("[✅ CONECTADO] ", "[⚠️ AISLADO] "):
                    if purpose.startswith(prefix):
                        purpose = purpose[len(prefix):]
                output += f"Proposito|{purpose}\n"
                if info.docstring:
                    desc = info.docstring[:300] + ('...' if len(info.docstring) > 300 else '')
                    output += f"Descripcion|{desc}\n"
                if info.classes:
                    output += f"Clases|{len(info.classes)}\n"
                    for class_name in info.classes:
                        if class_name in self.analyzer.enriched_classes:
                            enriched_list = [
                                ec for ec in self.analyzer.enriched_classes[class_name]
                                if ec.file == filepath
                            ]
                            for ec in enriched_list:
                                output += f"  [CLASS]{class_name}"
                                if ec.bases:
                                    output += f"|Hereda:{', '.join(ec.bases)}"
                                output += "\n"
                                if ec.ai_summary:
                                    output += f"  Analisis_IA|{ec.ai_summary}\n"
                                elif ec.docstring:
                                    short = ec.docstring.split('\n')[0][:120]
                                    output += f"  Docstring|{short}\n"

                                # Métodos públicos
                                if ec.methods:
                                    public_methods = [
                                        m for m in ec.methods
                                        if not m.name.startswith('_') or m.name == '__init__'
                                    ]
                                    if public_methods:
                                        output += "  Metodos_Publicos\n"
                                        for m in public_methods[:8]:
                                            sig = self._format_signature(m)
                                            output += f"    {sig}\n"
                                        if len(public_methods) > 8:
                                            output += f"    # ... {len(public_methods) - 8} métodos más\n"

                                    # ✅ BUG F FIX: métodos internos relevantes
                                    if self._has_relevant_internal_methods(ec):
                                        internal = [
                                            m for m in ec.methods
                                            if m.name.startswith('_')
                                            and m.name != '__init__'
                                            and not m.name.startswith('__')
                                            and m.docstring
                                        ]
                                        if internal:
                                            output += "  Metodos_Internos_Relevantes\n"
                                            for m in internal[:8]:
                                                sig = self._format_signature(m)
                                                doc = m.docstring.split('\n')[0][:80] if m.docstring else ""
                                                output += f"    {sig}\n"
                                                if doc:
                                                    output += f"      # {doc}\n"
                                            if len(internal) > 8:
                                                output += f"    # ... {len(internal) - 8} métodos internos más\n"

                                    output += "  [END_CLASS]\n"
                if info.functions:
                    funcs_display = ', '.join(f'{f}' for f in info.functions[:8])
                    if len(info.functions) > 8:
                        funcs_display += f" + {len(info.functions) - 8} más"
                    output += f"Funciones_Modulo|{len(info.functions)}|{funcs_display}\n"
                if filepath in self.analyzer.module_functions:
                    sigs = self.analyzer.module_functions[filepath]
                    public_sigs = [s for s in sigs if not s.name.startswith('_')][:5]
                    if public_sigs:
                        output += "Signatures_Funciones\n"
                        for sig in public_sigs:
                            output += f"{self._format_signature(sig)}\n"
            else:
                badge = self._get_integration_badge(component_name.split(' ')[0].replace('-', '_'))
                output += f"[COMPONENT]{component_name}|{badge}\n"
                output += f"Archivo|{filepath}\n"
                output += "Estado|Archivo no encontrado o no analizado\n"
            output += "[END_COMPONENT]\n"
        output += "[END_SECTION]\n"
        return output
# ==================== EJECUCIÓN ====================
if __name__ == "__main__":
    print("Este es un módulo generador de secciones. Ejecute axioma_doc_main.py")
    print("Versión: v3.9.0 — BUG F corregido + integración badges reales")
