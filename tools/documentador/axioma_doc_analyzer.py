#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# axioma_doc_analyzer.py - Núcleo del Analizador AXIOMA v4.0.2
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_analyzer.py
# Autor: SaintWick
# Versión: 4.0.2
#
# Orquestador principal del análisis documental. Coordina el escaneo
# de archivos, parsing AST, detección de duplicaciones semánticas,
# enriquecimiento con IA, integración con FlowMapper (AST/imports),
# y extracción de configuraciones, variables de entorno Pydantic
# y contratos de datos. Incluye capas de filtrado de archivos
# sensibles para evitar filtración de credenciales.
# ═══════════════════════════════════════════════════════════════
import ast
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple
from collections import defaultdict
from dataclasses import dataclass, field
from axioma_doc_base import (
    ProjectStats, FileInfo, ClassDefinitionInfo, DuplicationIssue,
    AIModelInfo, MemorySystemInfo, DatabaseTable, ConfigParameter,
    TaskTypeInfo, EXCLUSIONES, EXCLUDED_PATTERNS,
    DependencyInfo, CriticalFlow, CONFIG_IMPACT_MAP,
    DEPENDENCY_CONFIG, CRITICAL_FLOWS_CONFIG, RESEARCH_CONFIG,
    CRITICAL_FLOWS_DEFINITION,
)
from axioma_doc_extractors import (
    ParamInfo, FunctionSignature, EnrichedClassInfo,
    SignatureExtractor,
)
from axioma_doc_ai import OllamaAnalyzer
# ✅ FASE 2: Integración FlowMapper (v3.9.0 con bugs corregidos)
from axioma_doc_flow_mapper import FlowMapper, IntegrationReport, map_project_flows
import axioma_doc_analysis_passes as passes
import axioma_doc_analyzer_methods as methods

# ==================== CONFIGURACIÓN ====================
# ✅ v4.0.2: Auto-detección de PROJECT_ROOT desde ubicación del script
# Antes: Path("/ruta/a/axioma") — filtraba username y estructura
# Ahora: Se resuelve desde tools/documentador/ → raíz del proyecto
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ✅ v4.0.2: Archivos sensibles que NUNCA deben ser analizados por el orquestador
# Defensa en profundidad: complementa el filtro en axioma_doc_analysis_passes.py
SENSITIVE_FILES: Set[str] = {
    "push.py",       # Token de GitHub hardcodeado
    "commit.py",     # Puede contener credenciales de backup
    ".env",          # Variables de entorno reales
    ".env.local",    # Variables de entorno locales
    ".env.production",
    ".env.staging",
    "secrets.json",  # Credenciales JSON
    "credentials.json",
    "tokens.json",
    "tokens github.txt",
}

# Patrones de exclusión para el scanner de env vars
_ENV_SCAN_IGNORE = ['data/outputs/', 'tests/', 'tools/documentador/',
                    'deepseek-harness/', 'inforchat/']

# ==================== ESTRUCTURAS DE DATOS ====================
@dataclass
class FileAnalysisResult:
    """Resultado del análisis de un archivo individual"""
    path: str
    size: int
    modified: float
    classes: List[Dict[str, Any]]
    functions: List[str]
    imports: List[str]

@dataclass
class DependencyNode:
    """Nodo de dependencia para mapa de módulos"""
    module: str
    depends_on: List[str] = field(default_factory=list)
    dependency_type: str = "internal"
    usage_count: int = 0

@dataclass
class FlowStep:
    """Paso de flujo crítico"""
    step_number: int
    component: str
    fallback: Optional[str]
    timeout: Optional[str]

# ==================== HELPER: FILTRO DE ARCHIVOS SENSIBLES ====================
def _is_sensitive_file(filepath: Path) -> bool:
    """
    ✅ v4.0.2: Verifica si un archivo es sensible y debe omitirse del análisis.
    Capa de defensa del orquestador — complementa el filtro en passes.scan_files_recursive().
    """
    filename = filepath.name
    # Coincidencia exacta
    if filename in SENSITIVE_FILES:
        return True
    # Patrones por extensión sensible
    if filepath.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}:
        return True
    # Archivos ocultos que empiezan con .env
    if filename.startswith('.env'):
        return True
    return False

# ==================== SCANNER DE ENV VARS PYDANTIC ====================
def _detect_env_vars_pydantic(analyzer: "ImprovedProjectAnalyzer") -> Set[str]:
    """
    ✅ FIX BUG D v3.9.0: Detecta variables de entorno declaradas como fields
    en clases que heredan de BaseSettings (pydantic_settings).

    El scanner original solo buscaba os.environ.get() / os.getenv() en el AST,
    lo que dejaba ciego al documentador frente a toda la configuración de AXIOMA
    que usa Pydantic BaseSettings con env_file='.env'.

    ✅ v4.0.2: Ahora omite archivos en SENSITIVE_FILES para evitar extraer
    constantes de archivos con credenciales hardcodeadas.
    """
    env_vars: Set[str] = set()

    for rel_path, cache_entry in analyzer._ast_cache.items():
        # Excluir ruido
        if any(p in rel_path for p in _ENV_SCAN_IGNORE):
            continue

        # ✅ v4.0.2: Excluir archivos sensibles
        if Path(rel_path).name in SENSITIVE_FILES:
            continue

        tree = cache_entry.get('tree')
        if tree is None:
            continue

        for node in ast.walk(tree):
            # ── Patrón 1: BaseSettings fields ──────────────────────────────
            if isinstance(node, ast.ClassDef):
                # Verificar si hereda de BaseSettings (cualquier alias)
                base_names = set()
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        base_names.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        base_names.add(base.attr)

                is_settings_class = bool(
                    base_names & {'BaseSettings', 'Settings', 'BaseConfig'}
                )

                if not is_settings_class:
                    continue

                # Extraer todos los fields anotados del body de la clase
                for item in node.body:
                    if isinstance(item, ast.AnnAssign):
                        if isinstance(item.target, ast.Name):
                            field_name = item.target.id
                            # Excluir métodos/propiedades disfrazados y campos privados
                            if not field_name.startswith('_'):
                                env_vars.add(field_name.upper())

            # ── Patrón 2: os.environ.get() / os.getenv() clásico ───────────
            elif isinstance(node, ast.Call):
                func = node.func

                # os.environ.get("VAR") o os.environ["VAR"]
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ('get', '__getitem__')
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == 'environ'
                ):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        env_vars.add(str(node.args[0].value).upper())

                # os.getenv("VAR")
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == 'getenv'
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'os'
                ):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        env_vars.add(str(node.args[0].value).upper())

    return env_vars

# ==================== ANALIZADOR PRINCIPAL v4.0.2 ====================
class ImprovedProjectAnalyzer:
    """
    Analizador de proyectos AXIOMA v4.0.2
    Sin caché persistente — análisis completo en cada ejecución.
    Features: mapa de dependencias, flujos críticos, investigación prioritaria,
    cruce AST, detección de env vars Pydantic BaseSettings,
    detección semántica de duplicaciones con filtrado de boilerplate.

    ✅ v4.0.2: Defensa en profundidad contra filtración de credenciales.
    """

    LEGITIMATE_PATTERNS = passes.LEGITIMATE_PATTERNS

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        use_ollama: bool = True,
        ollama_model: str = None,
        trace_flows: bool = True,
        strict_include: bool = False,
    ):
        self.project_root = project_root
        self.stats = ProjectStats()
        self.files_info: Dict[str, FileInfo] = {}
        self.class_locations: Dict[str, List[str]] = defaultdict(list)
        self.class_definitions: Dict[str, List[ClassDefinitionInfo]] = defaultdict(list)
        self.enriched_classes: Dict[str, List[EnrichedClassInfo]] = defaultdict(list)
        self.module_functions: Dict[str, List[FunctionSignature]] = defaultdict(list)
        self.duplications: List[DuplicationIssue] = []
        self.ai_models: Dict[str, AIModelInfo] = {}
        self.memory_systems: List[MemorySystemInfo] = []
        self.proto_files: List[str] = []
        self.dependencies: Dict[str, List[str]] = {'external': [], 'internal': []}
        self.database_tables: List[DatabaseTable] = []
        self.dataclass_contracts: Dict[str, Dict] = {}
        self.enum_definitions: Dict[str, List[str]] = defaultdict(list)
        self.config_params: List[ConfigParameter] = []
        self.env_vars: Set[str] = set()
        self.task_types: List[TaskTypeInfo] = []
        self.class_hierarchy: Dict[str, List[str]] = {}
        self.module_dependencies: Dict[str, DependencyNode] = {}
        self.critical_flows: List[CriticalFlow] = []
        self._config_yaml_files: List[Path] = []

        # FASE 2: Atributos para cruce AST e integración
        self._ast_cache: Dict[str, Dict[str, Any]] = {}
        self._python_files: List[Path] = []
        self._trace_flows = trace_flows
        self._strict_include = strict_include
        self.integration_report: Optional[IntegrationReport] = None

        # Ollama
        self.ollama = OllamaAnalyzer(model=ollama_model) if use_ollama else None
        self.signature_extractor = SignatureExtractor()
        self._use_ollama = use_ollama and (self.ollama.enabled if self.ollama else False)

        self.analysis_stats = {
            'total_files': 0,
            'cached_files': 0,
            'analyzed_files': 0,
            'inconsistencies_found': 0,
        }

    # ==================== ANÁLISIS PRINCIPAL ====================
    def analyze(self):
        """Ejecuta análisis completo del proyecto v4.0.2 (sin caché persistente)"""
        print("📊 Analizando proyecto AXIOMA v4.0.2...")

        if self._use_ollama:
            print(f"🤖 Análisis IA habilitado con modelo: {self.ollama.model}")
        else:
            print("ℹ️  Análisis IA deshabilitado (Ollama no disponible)")

        # 1. Escaneo y parsing
        self._python_files = passes.scan_files_recursive(self)

        # ✅ v4.0.2: Segundo filtro de seguridad en el orquestador
        # Elimina archivos sensibles que puedan haber pasado el filtro de passes
        original_count = len(self._python_files)
        self._python_files = [
            f for f in self._python_files if not _is_sensitive_file(f)
        ]
        filtered_count = original_count - len(self._python_files)
        if filtered_count > 0:
            print(f"🔐 {filtered_count} archivo(s) sensible(s) filtrado(s) por el orquestador")

        self.analysis_stats['total_files'] = len(self._python_files)
        self.analysis_stats['analyzed_files'] = len(self._python_files)
        print(f"📊 Archivos a analizar: {len(self._python_files)}")
        print()

        # 2. Análisis archivo por archivo (llena _ast_cache)
        for filepath in self._python_files:
            passes.analyze_file(self, filepath)

        # 3. Pasadas complementarias estáticas
        passes.analyze_dependencies(self)
        passes.analyze_module_dependencies(self)
        passes.detect_duplications(self)  # ✅ v4.0.0: Motor semántico integrado
        passes.analyze_memory_system(self)
        passes.detect_proto_files(self)
        passes.analyze_database_schema(self)
        passes.analyze_data_contracts(self)
        passes.analyze_configurations(self)

        # ✅ BUG D FIX: detect_env_vars original primero (os.environ clásico),
        # luego el scanner Pydantic complementa con BaseSettings fields.
        passes.detect_env_vars(self)
        pydantic_vars = _detect_env_vars_pydantic(self)
        self.env_vars.update(pydantic_vars)
        if pydantic_vars:
            print(f"   ✅ Variables Pydantic BaseSettings detectadas: {len(pydantic_vars)}")

        passes.analyze_task_types(self)
        passes.analyze_class_hierarchy(self)
        passes.build_critical_flows(self)

        # ✅ FASE 2: Ejecutar FlowMapper si está habilitado
        if self._trace_flows and self._ast_cache:
            print("🔀 Mapeando flujos cruzados AST (v4.0.2)...")
            try:
                self.integration_report = map_project_flows(
                    self.project_root, self._python_files
                )
                self._inject_integration_badges()
                print(f"   ✅ Contratos evaluados: {len(self.integration_report.flow_contracts)}")
                print(f"   ✅ Módulos conectados: {len(self.integration_report.connected_modules)}")
                print(f"   ✅ Módulos aislados detectados: {len(self.integration_report.isolated_modules)}")
            except Exception as e:
                print(f"   ⚠️ Error en FlowMapper: {e}")
                self.integration_report = None

        # 4. Detección de modelos y enriquecimiento IA
        print("🔍 Detectando modelos Ollama...")
        from axioma_doc_base import detect_ollama_models
        detected = detect_ollama_models()
        for name, info in detected.items():
            self.ai_models[name] = AIModelInfo(name=name, **info)

        if self._use_ollama:
            print("🤖 Enriqueciendo clases críticas con análisis IA...")
            passes.enrich_with_ai(self)

        methods.print_summary(self)

    # ==================== BADGES DE INTEGRACIÓN ====================
    def _inject_integration_badges(self) -> None:
        """
        ✅ FIX BUG E v3.9.0 + FIX v4.0.1: Inyecta badges de integración en un campo dedicado
        (integration_badge) en lugar de contaminar info.purpose con el badge como
        prefijo de string.

        El problema anterior: info.purpose = f"[✅ CONECTADO] {info.purpose}"
        acumulaba el badge en cada regeneración del doc, y el generador no podía
        separar el badge del texto real del propósito.

        Ahora: info.integration_badge = "[✅ CONECTADO]" (campo separado).
        Si FileInfo no tiene ese campo (versión antigua de axioma_doc_base),
        se hace un setattr defensivo para compatibilidad.

        ✅ FIX v4.0.1: Además del archivo, inyecta el badge en cada diccionario de
        clase (cls_dict['integration_badge']). Esto corrige los falsos [⚠️ AISLADO]
        en clases internas (como Palace en gateway.py) ya que el FlowMapper v3.9.3
        ahora las reconoce como conectadas gracias a la regla de 'internal_uses'
        y la pertenencia a un host conectado.
        """
        if not self.integration_report:
            return

        from axioma_doc_flow_mapper import get_integration_badge_for_symbol

        # Enriquecer flujos críticos con estado del contrato
        for flow in self.critical_flows:
            components = [
                s.get('component', '').split('.')[0]
                for s in flow.steps
                if isinstance(s, dict)
            ]
            badges = [
                get_integration_badge_for_symbol(comp, self.integration_report)
                for comp in components if comp
            ]
            flow_status = (
                "[✅ CONECTADO]" if all(b == "[✅ CONECTADO]" for b in badges)
                else "[⚠️ PARCIAL]" if any(b == "[✅ CONECTADO]" for b in badges)
                else "[⚠️ AISLADO]"
            ) if badges else "[❓ PENDIENTE]"
            flow.integration_status = flow_status

        # ✅ BUG E FIX: badge en campo separado, NO en purpose
        connected_set = set(self.integration_report.connected_modules)
        isolated_set = set(self.integration_report.isolated_modules)

        for rel_path, info in self.files_info.items():
            # 1. Badge a nivel de ARCHIVO
            if rel_path in connected_set:
                file_badge = "[✅ CONECTADO]"
            elif rel_path in isolated_set:
                file_badge = "[⚠️ AISLADO]"
            else:
                file_badge = ""

            # Setattr defensivo: funciona aunque FileInfo no tenga el campo aún
            if not hasattr(info, 'integration_badge'):
                object.__setattr__(info, 'integration_badge', file_badge)
            else:
                info.integration_badge = file_badge

            # 2. ✅ v4.0.1 FIX: Badge a nivel de CLASE
            if hasattr(info, 'classes') and isinstance(info.classes, list):
                for cls_dict in info.classes:
                    if isinstance(cls_dict, dict):
                        class_name = cls_dict.get('name')
                        if class_name:
                            cls_dict['integration_badge'] = get_integration_badge_for_symbol(
                                class_name, self.integration_report
                            )

            # Limpiar cualquier badge previo que haya quedado incrustado en purpose
            for prefix in ("[✅ CONECTADO] ", "[⚠️ AISLADO] "):
                if hasattr(info, 'purpose') and isinstance(info.purpose, str) and info.purpose.startswith(prefix):
                    info.purpose = info.purpose[len(prefix):]

    # ==================== ESTADÍSTICAS ====================
    def get_analysis_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas completas del análisis v4.0.2"""
        stats = methods.build_analysis_stats(self)
        if self.integration_report:
            stats['flow_mapper_contracts'] = len(self.integration_report.flow_contracts)
            stats['flow_mapper_connected'] = len(self.integration_report.connected_modules)
            stats['flow_mapper_isolated'] = len(self.integration_report.isolated_modules)
        stats['env_vars_total'] = len(self.env_vars)
        return stats

# ==================== EJECUCIÓN ====================
if __name__ == "__main__":
    print("Este es un módulo de análisis. Ejecute axioma_doc_main.py")
    print("Versión: v4.0.2")
    print(f"  [NEW] Motor de Detección Semántica de Duplicaciones (Jaccard + Boilerplate Filter)")
    print(f"  [NEW] Reconocimiento de Patrones Arquitectónicos (Facade, Core, API, L1-L3)")
    print(f"  BUG D: Scanner Pydantic BaseSettings para variables de entorno")
    print(f"  BUG E: Badge de integración en campo separado (no contamina purpose)")
    print(f"  FIX 1 (v4.0.1): Inyección de badges a nivel de clase para evitar falsos AISLADOS")
    print(f"  🔐 FIX (v4.0.2): Doble filtro de archivos sensibles (passes + orquestador)")
    print(f"  🔐 FIX (v4.0.2): PROJECT_ROOT auto-detectado (sin username hardcodeado)")
    print(f"PROJECT_ROOT:          {PROJECT_ROOT}")
    print(f"DEPENDENCY_CONFIG:     {'Habilitado' if DEPENDENCY_CONFIG['enabled'] else 'Deshabilitado'}")
    print(f"CRITICAL_FLOWS_CONFIG: {'Habilitado' if CRITICAL_FLOWS_CONFIG['enabled'] else 'Deshabilitado'}")
    print(f"RESEARCH_CONFIG:       {'Habilitado' if RESEARCH_CONFIG['enabled'] else 'Deshabilitado'}")
    print(f"TRACE_FLOWS (v4.0.2):  True")
