#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v3.9.0 — Generador de Secciones (Extensión)
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/documentador/axioma_doc_generator_sections_ext.py
# Autor: SaintWick
# Propósito: Mixin con la segunda mitad de métodos generate_* de
#            DocumentSectionGenerator (generate_task_types_section →
#            generate_full_document). Extraído en refactorización v3.9.1.
#            Requiere herencia desde DocumentSectionGenerator para acceder
#            a self.analyzer y al resto de métodos generate_*.
# ═══════════════════════════════════════════════════════════════
from datetime import datetime
from typing import List, Dict, Any
from axioma_doc_base import *

try:
    from axioma_doc_flow_mapper import IntegrationReport, get_integration_badge_for_symbol, map_project_flows
    FLOW_MAPPER_AVAILABLE = True
except ImportError:
    FLOW_MAPPER_AVAILABLE = False


class DocumentSectionGeneratorExt:
    """Mixin: segunda mitad de secciones de DocumentSectionGenerator v3.9.0.
    No instanciar directamente — usar a través de DocumentSectionGenerator.
    """

    def generate_task_types_section(self) -> str:
        output = """[SECTION]TaskTypes_Disponibles
[DESCRIPTION]Los TaskTypes son categorías de tareas que AXIOMA puede manejar.
Se usan para:
1|Clasificar la intención del usuario (IntentClassifier)
2|Seleccionar el modelo óptimo (SmartRouter)
3|Ejecutar acciones específicas (Agents)
"""
        if not self.analyzer.task_types:
            output += "No se encontraron TaskTypes definidos\n"
        else:
            output += "TABLE|TaskType|Descripcion|Ejemplo_Uso\n"
            for task_type in self.analyzer.task_types:
                output += f"ROW|{task_type.name}|{task_type.description}|{task_type.example}\n"
            output += """
[SUBSECTION]Como_se_Usan
CODE|python
from src.domain.types import TaskType
from src.router.smart_router import SmartRouter
router = SmartRouter()
task_type = router.classify("Crea una función que sume números")
model = router.select_model(task_type)
END_CODE
"""
        output += "[END_SECTION]\n"
        return output

    def generate_context_flows_section(self) -> str:
        output = """[SECTION]Contexto_y_Flujo_Datos
[DESCRIPTION]Rastreo AST dinámico (v3.9.0) de cómo viajan los contratos de datos entre módulos críticos.
"""
        if not FLOW_MAPPER_AVAILABLE or not self.analyzer.integration_report:
            output += "RASTREO_NO_DISPONIBLE|Ejecute con --trace-flows para habilitar FlowMapper\n"
            output += "[END_SECTION]\n"
            return output

        report = self.analyzer.integration_report
        output += "[SUBSECTION]Rastreo_SessionContext_y_Datos\n"
        output += "TABLE|Símbolo|Definido_En|Usado_En|Instanciaciones|Llamadas\n"
        for name, trace in report.symbol_traces.items():
            if trace.instantiation_count > 0 or 'Context' in name or 'Queue' in name:
                used_in_str = (
                    ", ".join(trace.used_in[:3]) + ("..." if len(trace.used_in) > 3 else "")
                    if trace.used_in else "Aislado"
                )
                output += f"ROW|{name}|{trace.defined_in or 'N/A'}|{used_in_str}|{trace.instantiation_count}|{trace.call_sites}\n"
        output += "[END_SUBSECTION]\n"

        output += "[SUBSECTION]Contratos_Activos\n"
        for fc in report.flow_contracts:
            output += f"[CONTRACT]{fc.name}|{fc.badge}\n"
            output += f"Componentes|{', '.join(fc.components)}\n"
            if fc.references:
                output += "Rutas_Cruzadas\n"
                for ref in fc.references:
                    output += f"  → {ref['symbol_name']} en {ref['defined_in']}\n"
            output += "[END_CONTRACT]\n"
        output += "[END_SECTION]\n"
        return output

    def generate_async_integration_section(self) -> str:
        output = """[SECTION]Integración_Asíncrona
[DESCRIPTION]Mapeo de métodos async def, Awaitable y asyncio.Task con su manejo de errores.
"""
        async_methods = []
        for filepath, sigs in self.analyzer.module_functions.items():
            for sig in sigs:
                if sig.is_async:
                    async_methods.append((sig.name, filepath, sig.return_type, sig.docstring))
        for ecs in self.analyzer.enriched_classes.values():
            for ec in ecs:
                for m in ec.methods:
                    if m.is_async:
                        async_methods.append((f"{ec.name}.{m.name}", ec.file, m.return_type, m.docstring))

        if not async_methods:
            output += "NO_ASYNC_METHODS|No se detectaron firmas asíncronas\n"
        else:
            output += "TABLE|Método|Archivo|Tipo_Retorno|Docstring_Resumen\n"
            for name, file, ret, doc in async_methods[:30]:
                doc_summary = doc.split('\n')[0][:80].replace('|', ' ') if doc else "Sin docstring"
                output += f"ROW|{name}|{file}|{ret}|{doc_summary}\n"
            if len(async_methods) > 30:
                output += f"ROW|... +{len(async_methods)-30} más|-|-|\n"

        output += """
[SUBSECTION]Gestión_Event_Loop
NOTA|AXIOMA ejecuta run_full_duplex y process_stream bajo asyncio.run() o bucles personalizados.
ERROR_HANDLING|Usar try/except con asyncio.CancelledError y TimeoutError en puntos de entrada críticos.
RECOMENDACION|Evitar bloqueo síncrono (time.sleep, requests.get) en métodos marcados como async.
"""
        output += "[END_SECTION]\n"
        return output

    def generate_detailed_flows_section(self) -> str:
        critical_paths = self._identify_critical_paths()
        output = """[SECTION]Flujos_Detallados_Con_Puntos_Fallo
[DESCRIPTION]Flujos críticos con puntos de fallo, fallbacks, timeouts y estado de integración real.
"""
        for path in critical_paths:
            first_comp = path['steps'][0]['component'].split('(')[0].split('.')[0] if path['steps'] else ""
            flow_badge = self._get_integration_badge(first_comp)
            output += f"[SUBSECTION]Flujo_{path['name']}|{flow_badge}\n"
            output += f"Descripcion|{path['description']}\n"
            output += "STEPS|Orden|Componente|Estado|Fallback|Timeout\n"
            for step in path['steps']:
                fallback_str = step.get('fallback') or "Ninguno"
                timeout_str = step.get('timeout') or "N/A"
                comp = step.get('component', '')
                step_badge = self._get_integration_badge(comp.split('(')[0].split('.')[0])
                output += f"ROW|{step.get('step_number', step.get('step', 0))}|{comp}|{step_badge}|{fallback_str}|{timeout_str}\n"
            output += "PUNTOS_DE_FALLO\n"
            for failure in path['failure_points']:
                output += f"⚠️|{failure}\n"
            output += "[END_SUBSECTION]\n"
        output += """[SUBSECTION]Flujo_Generacion_Codigo
FLOW|Usuario: "Crea una función que calcule factorial"
STEP|Dispatcher.dispatch(msg)
STEP|SmartRouter.route()|Detecta: TaskType.CODE_GENERATION
STEP|SmartRouter.route()|Selecciona: qwen2.5-coder:7b (llm_code_model)
STEP|MemoryGateway.get_context()|Recupera código Python previo
STEP|CoderAgent.execute()|Genera código con contexto
STEP|ValidatorAgent.execute()|Score: 0.87 (coherencia + sintaxis)
STEP|ValidatorAgent._security_audit_defensive()|CWE Top-25 pre-LLM
STEP|MemoryGateway.add_message()|Guarda interacción + código
END|Respuesta al Usuario
[SUBSECTION]Flujo_Busqueda_Web
FLOW|Usuario: "Busca información sobre AXIOMA"
STEP|Dispatcher.dispatch(msg)|Detecta: TaskType.WEB_SEARCH
STEP|SmartRouter.route()|qwen3:8b (llm_default_model, ligero para resumir)
STEP|SearcherAgent.execute()
BRANCH|Tavily API
BRANCH|Serper API (fallback)
BRANCH|DuckDuckGo (fallback)
STEP|SearcherAgent sintetiza con LLM
STEP|MemoryGateway.add_message()
END|Respuesta con fuentes citadas
"""
        output += "[END_SECTION]\n"
        return output

    def generate_class_hierarchy_section(self) -> str:
        output = """[SECTION]Jerarquía_Clases
[SUBSECTION]Clases_Principales_Herencias
"""
        if not self.analyzer.class_hierarchy:
            output += "No se detectó jerarquía de clases\n"
        else:
            important_classes = [
                'OllamaClient', 'MemoryGateway', 'IntentClassifier',
                'SmartRouter', 'Dispatcher', 'Validator'
            ]
            for class_name in important_classes:
                if class_name in self.analyzer.class_hierarchy:
                    bases = self.analyzer.class_hierarchy[class_name]
                    output += f"[CLASS]{class_name}\n"
                    output += f"Hereda|{', '.join(bases)}\n"
            output += "[SUBSECTION]Arbol_Herencias_Completo\n"
            output += "TABLE|Clase|Hereda_De\n"
            for cls, bases in sorted(self.analyzer.class_hierarchy.items()):
                if bases:
                    output += f"ROW|{cls}|{', '.join(bases)}\n"
            output += "[END_SECTION]\n"
        return output

    def generate_memory_system_section(self) -> str:
        output = """[SECTION]Sistema_Memoria
[DESCRIPTION]AXIOMA usa un sistema de memoria persistente multi-capa:
"""
        for mem_sys in self.analyzer.memory_systems:
            output += f"[MEMORY]{mem_sys.type}\n"
            output += f"Ubicacion|{mem_sys.location}\n"
            output += f"Proposito|{mem_sys.purpose}\n"
            output += f"Implementacion|{mem_sys.implementation_file}\n"
            output += f"Tamano_Estimado|{mem_sys.size_estimate}\n"
            if mem_sys.additional_info:
                output += "Informacion_Adicional\n"
                for key, value in mem_sys.additional_info.items():
                    if isinstance(value, list):
                        output += f"  {key}|{', '.join(str(v) for v in value)}\n"
                    else:
                        output += f"  {key}|{value}\n"
            output += "[END_MEMORY]\n"
        output += "[END_SECTION]\n"
        return output

    def generate_ai_models_section(self) -> str:
        lines = ["[SECTION]Modelos_IA_Detectados"]
        if not self.analyzer.ai_models:
            lines.append("No_se_detectaron_modelos_Ollama")
        else:
            lines.append("TABLE|Modelo|Tamaño|Propósito|Proveedor|Usado_en")
            for name, info in self.analyzer.ai_models.items():
                files_count = f"{len(info.files_used)} archivo(s)" if info.files_used else "-"
                lines.append(f"ROW|{name}|{info.size}|{info.purpose}|{info.provider}|{files_count}")
            lines.append("[SUBSECTION]Detalle_de_uso_por_archivo")
            for name, info in self.analyzer.ai_models.items():
                if info.files_used:
                    lines.append(f"[MODEL]{name}")
                    for f in info.files_used[:5]:
                        lines.append(f"  - {f}")
                    if len(info.files_used) > 5:
                        lines.append(f"  - ... {len(info.files_used) - 5} más")
                    lines.append("[END_MODEL]")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def generate_external_dependencies(self) -> str:
        lines = [
            "[SECTION]Dependencias_Externas",
            "[SUBSECTION]Librerías_Python_requirements",
        ]
        if not self.analyzer.dependencies['external']:
            lines.append("No_se_encontraron_dependencias")
        else:
            lines.append("TABLE|Librería|Categoría_inferida")
            category_map = {
                'chromadb': 'Vector DB', 'sqlite': 'Base de datos',
                'fastapi': 'Web Framework', 'pydantic': 'Validación de datos',
                'torch': 'ML/Deep Learning', 'sklearn': 'Machine Learning',
                'scikit': 'Machine Learning', 'numpy': 'Cómputo numérico',
                'pandas': 'Análisis de datos', 'requests': 'HTTP Client',
                'aiohttp': 'HTTP Async', 'sentence': 'NLP/Embeddings',
                'transformers': 'NLP/LLM', 'ollama': 'LLM Local',
                'protobuf': 'Serialización', 'grpc': 'RPC',
                'pytest': 'Testing', 'joblib': 'Serialización ML',
            }
            for dep in sorted(self.analyzer.dependencies['external'])[:30]:
                dep_lower = dep.lower()
                category = next((v for k, v in category_map.items() if k in dep_lower), "General")
                lines.append(f"ROW|{dep}|{category}")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def generate_database_schema(self) -> str:
        lines = ["[SECTION]Esquema_Base_Datos"]
        if not self.analyzer.database_tables:
            lines.append("No_se_detectaron_tablas")
        else:
            lines.append(f"Total_tablas|{len(self.analyzer.database_tables)}")
            for table in self.analyzer.database_tables:
                lines.append(f"[TABLA]{table.name}")
                lines.append("COLUMNAS|Nombre|Tipo|Constraints")
                for col_name, col_type, constraints in table.columns:
                    cons = constraints or '-'
                    lines.append(f"ROW|{col_name}|{col_type}|{cons}")
                lines.append(f"Primary_Key|{table.primary_key}")
                lines.append("[END_TABLA]")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def generate_data_contracts(self) -> str:
        lines = [
            "[SECTION]Contratos_Datos_Expandidos",
            "[SUBSECTION]Dataclasses_Definidos",
        ]
        if not self.analyzer.dataclass_contracts:
            lines.append("No_se_encontraron_dataclasses")
        else:
            lines.append("TABLE|Nombre|Archivo|Tipo|Campos_Principales")
            for name, info in sorted(self.analyzer.dataclass_contracts.items()):
                filepath = info.get('file', 'N/A')
                lines.append(f"ROW|{name}|{filepath}|dataclass|N/A")
            lines.append("[SUBSECTION]Enums_Definidos")
            if not self.analyzer.enum_definitions:
                lines.append("No_se_encontraron_enums")
            else:
                lines.append("TABLE|Nombre|Archivo|Valores")
                for name, files in sorted(self.analyzer.enum_definitions.items()):
                    lines.append(f"ROW|{name}|{files[0]}|Ver código fuente")
            lines.append("[SUBSECTION]Pydantic_Models")
            lines.append("NOTA|Los modelos Pydantic se documentan en Análisis_Detallado_Clases con tag 'Pydantic'")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def generate_dependency_map_section(self) -> str:
        dependency_graph = self._build_dependency_graph()
        lines = [
            "[SECTION]Mapa_Dependencias_Módulos",
            "[DESCRIPTION]Qué módulos importan a qué otros módulos. Útil para entender acoplamiento.",
            "",
            "[SUBSECTION]Dependencias_Por_Módulo",
        ]
        MODULE_TYPE = {
            'core': 'Core', 'router': 'Enrutamiento', 'memory': 'Memoria',
            'llm': 'LLM Client', 'agents': 'Agentes', 'domain': 'Dominio',
            'handlers': 'Handlers', 'interfaces': 'Interfaces',
            'integrations': 'Integraciones', 'config': 'Configuración',
            'utils': 'Utilidades',
        }
        lines.append("TABLE|Módulo|Dependencias_Directas|Tipo")
        for module, deps in sorted(dependency_graph.items()):
            internal = [d for d in sorted(deps) if not d.startswith('ext:')]
            external = [d.replace('ext:', '') for d in sorted(deps) if d.startswith('ext:')]
            parts = []
            if internal:
                parts.append(', '.join(internal[:8]))
                if len(internal) > 8:
                    parts[-1] += f" + {len(internal)-8} más"
            if external:
                parts.append(f"[ext: {', '.join(external[:4])}]")
            deps_str = ' | '.join(parts) if parts else '-'
            mod_type = MODULE_TYPE.get(module, 'General')
            lines.append(f"ROW|{module}|{deps_str}|{mod_type}")
        lines.append("")
        lines.append("[SUBSECTION]Dependencias_Críticas")
        dep_count: Dict[str, int] = {}
        for deps in dependency_graph.values():
            for dep in deps:
                dep_count[dep] = dep_count.get(dep, 0) + 1
        critical_deps = sorted(dep_count.items(), key=lambda x: x[1], reverse=True)[:10]
        for dep, count in critical_deps:
            lines.append(f"⚡|{dep}|Dependido por {count} módulos")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def _infer_impact(self, name: str) -> str:
        n = name.lower()
        if 'timeout' in n: return "Afecta tiempos de respuesta y fallbacks"
        if 'model' in n: return "Cambia modelo IA usado (afecta calidad/velocidad)"
        if 'cache' in n: return "Afecta rendimiento y uso de memoria"
        if 'path' in n or 'dir' in n: return "Requiere validar ruta existente"
        if 'threshold' in n or 'score' in n: return "Afecta criterios de validación/clasificación"
        if 'pattern' in n or 'regex' in n: return "Modifica detección de patrones"
        if 'level' in n: return "Afecta sensibilidad de detección"
        return "Requiere reinicio"

    def generate_configuration_parameters(self) -> str:
        lines = ["[SECTION]Parámetros_Configuración_Con_Impacto"]
        py_params = [p for p in self.analyzer.config_params
                     if not str(getattr(p, 'file', '') or '').endswith(('.yaml', '.yml'))]
        yaml_params = [p for p in self.analyzer.config_params
                       if str(getattr(p, 'file', '') or '').endswith(('.yaml', '.yml'))]
        lines.append("[SUBSECTION]config_settings_py")
        if not py_params:
            lines.append("No_se_encontraron_parámetros_de_configuración")
        else:
            lines.append("TABLE|Parámetro|Valor|Tipo|Impacto_Si_Se_Cambia")
            for param in py_params[:50]:
                value_str = str(param.value)[:50]
                if len(str(param.value)) > 50:
                    value_str += "..."
                lines.append(f"ROW|{param.name}|{value_str}|{param.type_hint}|{self._infer_impact(param.name)}")
        lines.append("[SUBSECTION]Variables_Entorno_Detectadas")
        lines.append(f"Total|{len(self.analyzer.env_vars)}")
        if self.analyzer.env_vars:
            # Agrupar por categoría
            api_vars = sorted(v for v in self.analyzer.env_vars if any(k in v for k in ('API', 'KEY', 'TOKEN')))
            llm_vars = sorted(v for v in self.analyzer.env_vars if any(k in v for k in ('LLM', 'OLLAMA', 'MODEL')))
            db_vars = sorted(v for v in self.analyzer.env_vars if any(k in v for k in ('DB', 'PATH', 'DIR', 'CHROMA', 'SQLITE')))
            sec_vars = sorted(v for v in self.analyzer.env_vars if any(k in v for k in ('SECRET', 'ENCRYPT', 'SESSION')))
            other_vars = sorted(
                v for v in self.analyzer.env_vars
                if v not in api_vars and v not in llm_vars and v not in db_vars and v not in sec_vars
            )
            for group_name, group_vars in [
                ('APIs_Externas', api_vars),
                ('Modelos_LLM', llm_vars),
                ('Base_de_Datos_y_Rutas', db_vars),
                ('Seguridad', sec_vars),
                ('General', other_vars),
            ]:
                if group_vars:
                    lines.append(f"[GRUPO]{group_name}")
                    for v in group_vars:
                        lines.append(f"  {v}")
        lines.append("[SUBSECTION]config_yaml_files")
        if not yaml_params:
            lines.append("No_se_encontraron_archivos_YAML_de_configuración")
        else:
            from collections import defaultdict
            by_file: dict = defaultdict(list)
            for p in yaml_params:
                by_file[getattr(p, 'file', 'unknown')].append(p)
            for source_file, params in sorted(by_file.items()):
                lines.append(f"[YAML_FILE]{source_file}")
                lines.append("TABLE|Clave|Valor|Tipo|Impacto_Si_Se_Cambia")
                for param in params[:80]:
                    value_str = str(param.value)[:50]
                    if len(str(param.value)) > 50:
                        value_str += "..."
                    lines.append(f"ROW|{param.name}|{value_str}|{param.type_hint}|{self._infer_impact(param.name)}")
        lines.append("[END_SECTION]")
        return "\n".join(lines)

    def generate_usage_examples(self) -> str:
        lines = [
            "[SECTION]Ejemplos_Uso_Reales",
            "",
            "[SUBSECTION]Inicializar_el_Agente",
            "CODE|python",
            "from src.agents.orchestrator import Orchestrator",
            "",
            "agent = Orchestrator()",
            "response = agent.process('Busca información sobre AXIOMA')",
            "print(response.content)",
            "END_CODE",
            "",
            "[SUBSECTION]Acceder_a_Memoria",
            "CODE|python",
            "from src.memory.gateway import MemoryGateway",
            "",
            "memory = MemoryGateway()",
            "relevant_memories = memory.search('¿qué hablamos sobre Python?', limit=5)",
            "for mem in relevant_memories:",
            "    print(f'{mem.timestamp}: {mem.content}')",
            "END_CODE",
            "",
            "[SUBSECTION]Usar_TaskTypes_Específicos",
            "CODE|python",
            "from src.domain.types import TaskType",
            "from src.router.smart_router import SmartRouter",
            "",
            "router = SmartRouter()",
            "task_type = router.classify('Crea una función que sume números')",
            "print(f'TaskType: {task_type}')  # CODE_GENERATION",
            "END_CODE",
            "",
            "[SUBSECTION]Ejecutar_Documentador_v3.9",
            "CODE|bash",
            "python tools/documentador/axioma_doc_main.py --trace-flows",
            "python tools/documentador/axioma_doc_main.py --force",
            "python tools/documentador/axioma_doc_main.py --validate",
            "python tools/documentador/axioma_doc_main.py --no-ollama",
            "END_CODE",
        ]
        return "\n".join(lines)

    def generate_architecture(self) -> str:
        src_path = self.analyzer.project_root / "src"
        structure_lines = []
        if src_path.exists():
            structure_lines.append("axioma/")
            structure_lines.append("├── src/")
            folders = [
                ('domain', 'Tipos, entidades, excepciones, contratos'),
                ('llm', 'Cliente Ollama con circuit breaker'),
                ('memory', 'Gateway unificado + 3 capas'),
                ('router', 'Clasificador + SmartRouter + Caché'),
                ('agents', 'Orchestrator + 4 agentes especializados'),
                ('interfaces', 'Web FastAPI + CLI'),
            ]
            existing_folders = []
            for folder, desc in folders:
                if (src_path / folder).exists():
                    existing_folders.append((folder, desc))
            for i, (folder, desc) in enumerate(existing_folders):
                prefix = "└──" if i == len(existing_folders) - 1 else "├──"
                structure_lines.append(f"│   {prefix} {folder}/       # {desc}")
            structure_lines.append("├── config/             # Configuraciones")
            structure_lines.append("├── data/               # Datos persistentes")
            structure_lines.append("├── tools/              # Utilidades y Documentador v3.9")
            structure_lines.append("├── tests/              # Tests unitarios")
            structure_lines.append("└── logs/               # Logs del sistema")
        else:
            structure_lines.append("⚠️  No se pudo verificar la estructura de directorios")
        structure_block = "\n".join(structure_lines)
        lines = [
            "[SECTION]Arquitectura",
            "[SUBSECTION]Estructura_Directorios_VERIFICADA",
            "CODE",
            structure_block,
            "END_CODE",
            "",
            "[SUBSECTION]Componentes_Principales",
            "TABLE|Componente|Responsabilidad",
            "ROW|Dispatcher|Orquestador principal",
            "ROW|SmartRouter|Selección de modelo óptimo",
            "ROW|MemoryGateway|Gestión de memoria persistente",
            "ROW|OllamaClient|Comunicación con Ollama",
            "ROW|Validator|Validación con ForcedCoT + CWE Top-25",
            "ROW|IntentClassifier|Clasificación ML de intenciones",
            "[END_SECTION]",
        ]
        return "\n".join(lines)

    def generate_stats(self) -> str:
        real_dups = len([d for d in self.analyzer.duplications if not d.is_legitimate])
        legit_dups = len([d for d in self.analyzer.duplications if d.is_legitimate])
        enriched_count = sum(len(v) for v in self.analyzer.enriched_classes.values())
        ai_analyzed = sum(
            1 for classes in self.analyzer.enriched_classes.values()
            for c in classes if c.ai_summary
        )
        cache_stats_section = ""
        if hasattr(self.analyzer, 'analysis_stats'):
            stats = self.analyzer.analysis_stats
            cache_stats_section = f"""
[SUBSECTION]Rendimiento_Cache_v3.9
Archivos_Totales|{stats.get('total_files', 0)}
Archivos_en_Cache|{stats.get('cached_files', 0)}
Archivos_Analizados|{stats.get('analyzed_files', 0)}
Inconsistencias_Detectadas|{stats.get('inconsistencies_found', 0)}
"""
            if stats.get('total_files', 0) > 0:
                hit_rate = (stats.get('cached_files', 0) / stats['total_files']) * 100
                cache_stats_section += f"Cache_Hit_Rate|{hit_rate:.1f}%\n"

        flow_mapper_stats = ""
        if self.analyzer.integration_report:
            flow_mapper_stats = f"""
[SUBSECTION]FlowMapper_Cruce_AST_v3.9
Contratos|{len(self.analyzer.integration_report.flow_contracts)}
Conectados|{len(self.analyzer.integration_report.connected_modules)}
Aislados|{len(self.analyzer.integration_report.isolated_modules)}
"""
        lines = [
            "[SECTION]Estadísticas",
            "[SUBSECTION]Métricas_del_Proyecto",
            f"Directorios|{self.analyzer.stats.total_dirs}",
            f"Archivos_Python|{self.analyzer.stats.python_files}",
            f"Líneas_de_código|{self.analyzer.stats.total_lines:,}",
            f"Clases|{self.analyzer.stats.total_classes}",
            f"Funciones (incl. métodos)|{self.analyzer.stats.total_functions}",
            f"Modelos_IA|{len(self.analyzer.ai_models)}",
            f"Sistemas_de_memoria|{len(self.analyzer.memory_systems)}",
            f"Tablas_de_BD|{len(self.analyzer.database_tables)}",
            f"Dataclasses|{len(self.analyzer.dataclass_contracts)}",
            f"Enums|{len(self.analyzer.enum_definitions)}",
            f"TaskTypes|{len(self.analyzer.task_types)}",
            f"Variables_de_entorno|{len(self.analyzer.env_vars)}",
            f"Duplicaciones_problemáticas|{real_dups}",
            f"Duplicaciones_legítimas_fallbacks|{legit_dups}",
            f"Clases_con_signatures_extraídas|{enriched_count}",
            f"Clases_con_análisis_IA|{ai_analyzed}",
            cache_stats_section,
            flow_mapper_stats,
            "[END_SECTION]",
        ]
        return "\n".join(lines)

    def generate_troubleshooting(self) -> str:
        lines = [
            "[SECTION]Solución_Problemas",
            "",
            "[SUBSECTION]Error_Duplicaciones_de_clases",
            "1|Identificar la ubicación correcta (generalmente src/domain/types.py)",
            "2|Eliminar definiciones duplicadas",
            "3|Actualizar imports en todos los archivos afectados",
            "4|Ejecutar tests para verificar",
            "Nota|Los fallbacks legítimos (con comentario Fallback en docstring) NO necesitan corrección",
            "",
            "[SUBSECTION]Error_Ollama_no_disponible",
            "1|Verificar que Ollama esté instalado: ollama list",
            "2|Verificar que haya al menos un modelo disponible",
            "3|Ejecutar con análisis IA explícito: python axioma_doc_main.py",
            "4|Especificar modelo: python axioma_doc_main.py --model qwen2.5-coder:7b",
            "",
            "[SUBSECTION]Badge_AISLADO_en_componente_esperado_como_CONECTADO",
            "1|Verificar que el símbolo sea instanciado (X = Clase()) fuera de su archivo",
            "2|Verificar que sea importado y usado, no solo type-hinted",
            "3|Si es un DTO/dataclass sin instanciación externa, es correcto que aparezca como AISLADO",
            "4|Revisar si el archivo está en IGNORED_PATTERNS del FlowMapper",
            "",
            "[SUBSECTION]Variables_Entorno_Faltantes",
            "1|El scanner v3.9 detecta fields de BaseSettings automáticamente",
            "2|Si falta una variable, verificar que la clase herede de BaseSettings",
            "3|Fields con prefijo _ no se detectan (son privados por convención)",
            "",
            "[END_SECTION]",
        ]
        return "\n".join(lines)

    def generate_research_priority_section(self) -> str:
        lines = [
            "[SECTION]Investigación_Prioritaria",
            "[DESCRIPTION]Jerarquía de fuentes ante errores o dudas:",
            "",
            "[SUBSECTION]Jerarquía_De_Fuentes",
            "1|Documentación interna (esta documentación)",
            "2|Código fuente del proyecto (AST analysis + FlowMapper v3.9)",
            "3|GitHub del proyecto (issues, commits, discussions)",
            "4|Documentación oficial de dependencias (FastAPI, ChromaDB, Ollama)",
            "5|StackOverflow y foros especializados",
            "6|Búsqueda web general",
            "",
            "[SUBSECTION]Reglas_De_Investigación",
            "✅|Nunca asumir sin contexto completo",
            "✅|Priorizar investigación sobre conjeturas",
            "✅|Buscar automáticamente en GitHub ante errores",
            "✅|Validar información en múltiples fuentes",
            "",
            "[SUBSECTION]Patrones_De_Error_Comunes",
            "TABLE|Error|Causa_Probable|Fuente_Investigación",
            "ROW|Ollama timeout|Modelo muy grande o RAM insuficiente|Ollama GitHub issues",
            "ROW|ChromaDB connection|Ruta incorrecta o permisos|ChromaDB docs",
            "ROW|Classifier low accuracy|Datos de entrenamiento insuficientes|ML best practices",
            "ROW|API fallback triggered|Rate limiting o API down|API status pages",
            "ROW|Memory search slow|Índices no optimizados|ChromaDB performance guide",
            "ROW|FlowMapper [⚠️ AISLADO]|Sin instanciación externa o import no utilizado|Verificar imports cruzados",
            "ROW|Variables_Entorno_Faltantes|Campo no hereda de BaseSettings|Revisar settings.py",
            "",
            "[END_SECTION]",
        ]
        return "\n".join(lines)

    def generate_integration_status_section(self) -> str:
        output = """[SECTION]Estado_Conexión_Módulos
[DESCRIPTION]Resumen automático de integración real de módulos (v3.9.0 — badges corregidos).
"""
        if not FLOW_MAPPER_AVAILABLE or not self.analyzer.integration_report:
            output += "RASTREO_NO_DISPONIBLE|Ejecute el documentador con `--trace-flows`\n"
            output += "[END_SECTION]\n"
            return output

        report = self.analyzer.integration_report
        output += "[SUBSECTION]Resumen_Rápido\n"
        output += f"Total_Módulos_Rastreados|{len(report.module_graph)}\n"
        output += f"Simbolos_Dinámicos_Evaluados|{len(self._global_target_symbols)}\n"
        output += f"Contratos_Activos|{len([fc for fc in report.flow_contracts if fc.status == 'CONNECTED'])}\n"
        output += f"Contratos_Parciales|{len([fc for fc in report.flow_contracts if fc.status == 'PARTIAL'])}\n"
        output += f"Contratos_Aislados|{len([fc for fc in report.flow_contracts if fc.status == 'ISOLATED'])}\n"
        output += "\n"

        output += "[SUBSECTION]Módulos_Conectados\n"
        for mod in report.connected_modules:
            output += f"✅|{mod}\n"

        output += "\n[SUBSECTION]Módulos_Aislados_o_Parciales\n"
        for mod in report.isolated_modules:
            output += f"⚠️|{mod}\n"

        if not report.isolated_modules:
            output += "Ninguno detectado.\n"

        output += "\n[SUBSECTION]Recomendaciones_Automáticas\n"
        for fc in report.flow_contracts:
            if fc.status in ('ISOLATED', 'PARTIAL'):
                missing = [
                    c for c in fc.components
                    if c not in [r.get('symbol_name') for r in fc.references]
                ]
                output += f"⚡|Revisar integración de: {fc.name} ({', '.join(missing) if missing else 'Referencias cruzadas débiles'})\n"

        output += "[END_SECTION]\n"
        return output

    def generate_footer(self) -> str:
        real_dups = len([d for d in self.analyzer.duplications if not d.is_legitimate])
        legit_dups = len([d for d in self.analyzer.duplications if d.is_legitimate])
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ai_info = f"Modelo_IA|{self.analyzer.ollama.model}" if self.analyzer._use_ollama else "Sin_análisis_IA"
        cache_info = ""
        if hasattr(self.analyzer, 'analysis_stats'):
            stats = self.analyzer.analysis_stats
            cache_info = f"""
[SUBSECTION]Cache_v3.9_Info
Archivos_Cacheados|{stats.get('cached_files', 0)}
Archivos_Analizados|{stats.get('analyzed_files', 0)}
Inconsistencias|{stats.get('inconsistencies_found', 0)}
"""
            if stats.get('total_files', 0) > 0:
                hit_rate = (stats.get('cached_files', 0) / stats['total_files']) * 100
                cache_info += f"Hit_Rate|{hit_rate:.1f}%\n"

        flow_footer = ""
        if self.analyzer.integration_report:
            flow_footer = f"""
[SUBSECTION]FlowMapper_v3.9_Info
Simbolos_Dinámicos|{len(self._global_target_symbols)}
Contratos|{len(self.analyzer.integration_report.flow_contracts)}
Conectados|{len(self.analyzer.integration_report.connected_modules)}
Aislados|{len(self.analyzer.integration_report.isolated_modules)}
"""
        lines = [
            "[SECTION]Notas_Finales",
            "[SUBSECTION]Sobre_este_Documento",
            f"Generado_automáticamente_por|axioma_doc_main.py v{DOCUMENTADOR_VERSION}",
            "CORRECCIONES_en_v3.9.0",   # histórico: los BUG A-G se corrigieron en 3.9.0
            "✅|BUG_A — Eliminada herencia de badge por vecindad de archivo (falsos positivos)",
            "✅|BUG_B — isolated_modules calculado desde symbol_traces, no solo contratos",
            "✅|BUG_C — _track_inheritance() respeta IGNORED_PATTERNS (tests no contaminan)",
            "✅|BUG_D — Scanner Pydantic BaseSettings detecta 35+ variables de entorno reales",
            "✅|BUG_E — Badge en campo separado integration_badge, purpose limpio",
            "✅|BUG_F — Métodos internos relevantes documentados (CWE, early-exit, etc.)",
            cache_info,
            flow_footer,
            f"Generado|{now_str}",
            f"Proyecto|AXIOMA",
            f"Ubicación|{self.analyzer.project_root}",
            f"Archivos_Python|{self.analyzer.stats.python_files}",
            f"Líneas_de_código|{self.analyzer.stats.total_lines:,}",
            f"Variables_Entorno|{len(self.analyzer.env_vars)}",
            f"Modelos_detectados|{len(self.analyzer.ai_models)}",
            f"{ai_info}",
            f"Duplicaciones_problemáticas|{real_dups}",
            f"Duplicaciones_legítimas|{legit_dups}",
            "[END_SECTION]",
        ]
        return "\n".join(lines)

    def generate_full_document(self) -> str:
        sections = [
            self.generate_header(),
            self.generate_overview(),
            self.generate_main_files_section(),
            self.generate_enriched_classes_section(),
            self.generate_critical_issues(),
            self.generate_key_components_section(),
            self.generate_task_types_section(),
            self.generate_context_flows_section(),
            self.generate_async_integration_section(),
            self.generate_detailed_flows_section(),
            self.generate_class_hierarchy_section(),
            self.generate_memory_system_section(),
            self.generate_ai_models_section(),
            self.generate_external_dependencies(),
            self.generate_database_schema(),
            self.generate_data_contracts(),
            self.generate_dependency_map_section(),
            self.generate_configuration_parameters(),
            self.generate_usage_examples(),
            self.generate_architecture(),
            self.generate_stats(),
            self.generate_troubleshooting(),
            self.generate_research_priority_section(),
            self.generate_integration_status_section(),
            self.generate_footer(),
        ]
        return "".join(sections)

if __name__ == "__main__":
    print("Módulo de extensión del generador. Ejecute axioma_doc_main.py")
    print("Versión: v3.9.1 — axioma_doc_generator_sections_ext")
