#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.1 — Extractores Avanzados
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/documentador/axioma_doc_extractors_advanced.py
# Autor: SaintWick
# Propósito: Segunda mitad de axioma_doc_extractors: hash de archivos,
#            extracción de firmas completa, dependencias de módulo,
#            campos Pydantic, constantes, lazy properties,
#            SignatureValidator y CriticalFlowAnalyzer.
#            Extraído en refactorización v4.1.1.
# ═══════════════════════════════════════════════════════════════
import ast
import re
import hashlib
from dataclasses import dataclass
from typing import List, Dict, Tuple, Any
from pathlib import Path
# Tipos y extractores base — importados desde la primera mitad del módulo
from axioma_doc_extractors import (
    EnrichedClassInfo, DependencyInfo, FlowStepInfo,
    SignatureExtractor,
    extract_inline_tags,
    extract_classes_defined_only, extract_bases, extract_function_signatures,
)

def compute_file_hash(file_path: Path) -> str:
    """
    ✅ NUEVO v3.2: Computa hash SHA-256 de un archivo para caché
    """
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (IOError, OSError):
        return ""

def compute_classes_signature_hash(classes: List[EnrichedClassInfo]) -> str:
    """
    ✅ NUEVO v3.2: Computa hash combinado de signatures de clases para validación
    """
    hasher = hashlib.sha256()
    for cls in sorted(classes, key=lambda c: c.name):
        hasher.update(cls.compute_signature_hash().encode('utf-8'))
    return hasher.hexdigest()

def extract_all_signatures_from_file(file_path: Path) -> Dict[str, Any]:
    """
    ✅ NUEVO v3.2: Extrae todas las signatures de un archivo para validación
    Returns: Dict con classes, functions, hashes
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content, filename=str(file_path))
        rel_path = str(file_path)
        lines = content.splitlines()

        classes = extract_classes_defined_only(tree, content, rel_path)
        enriched = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
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
                        if SignatureExtractor.should_include_method(sig.name, sig.decorators, sig.docstring, sig.tags):
                            methods.append(sig)
                attributes = SignatureExtractor.extract_class_attributes(node)

                version_tags = {}
                raw_tags = extract_inline_tags(content, node.lineno, node.end_lineno or node.lineno + 50)
                for k, v in raw_tags:
                    version_tags[k] = v

                # ✅ FIX 1, 3, 5 extraction for validation as well
                pydantic_fields = extract_pydantic_fields(node) if is_pydantic else []
                lazy_properties = detect_lazy_properties(node)
                changelog_notes = []

                enriched.append(EnrichedClassInfo(
                    name=node.name,
                    file=rel_path,
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
                    pydantic_fields=pydantic_fields,
                    lazy_properties=lazy_properties,
                    changelog_notes=changelog_notes
                ))
        functions = extract_function_signatures(tree, lines)
        return {
            'file': rel_path,
            'classes': [c.to_dict() for c in enriched],
            'functions': [f.to_dict() for f in functions],
            'classes_hash': compute_classes_signature_hash(enriched),
            'file_hash': compute_file_hash(file_path)
        }
    except Exception as e:
        return {
            'file': str(file_path),
            'error': str(e),
            'classes': [],
            'functions': [],
            'classes_hash': '',
            'file_hash': ''
        }

def extract_module_dependencies(file_path: Path, project_root: Path) -> DependencyInfo:
    """
    ✅ NUEVO v3.3: Extrae dependencias de un módulo
    Returns: DependencyInfo con imports internos y externos
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content, filename=str(file_path))
        rel_path = str(file_path.relative_to(project_root))
        module_name = rel_path.replace('/', '.').replace('.py', '')
        imports = SignatureExtractor.extract_module_imports(tree)
        depends_on = []
        for imp in imports['internal']:
            dep_module = imp.split('.')[1] if '.' in imp else imp
            if dep_module not in depends_on and dep_module != module_name.split('.')[0]:
                depends_on.append(dep_module)
        for imp in imports['external']:
            ext_dep = f"ext:{imp}"
            if ext_dep not in depends_on:
                depends_on.append(ext_dep)
        return DependencyInfo(
            module=module_name,
            depends_on=depends_on,
            dependency_type='internal'
        )
    except Exception:
        return DependencyInfo(module=str(file_path), depends_on=[])

# ==================== FIX 1: INTROSPECCIÓN PYDANTIC v4.1 ====================
def extract_pydantic_fields(node: ast.ClassDef) -> List[Dict[str, Any]]:
    """
    ✅ FIX 1: Extrae campos de Pydantic/BaseSettings con validaciones Field().

    Captura:
    - Nombre y tipo del campo
    - Valores default
    - Constraints: ge (>=), le (<=), gt (>), lt (<)
    - Descripción embebida
    - Valores de listas de seguridad (blocked_commands, etc.)

    Retorna lista de dicts con la información completa de cada campo.
    """
    fields = []
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue

        field_info = {
            'name': item.target.id,
            'type_hint': SignatureExtractor._annotation_to_str(item.annotation) if item.annotation else "Any",
            'default': None,
            'constraints': {},
            'description': '',
            'is_security_list': False,
            'security_values': []
        }

        # Detectar Field() call
        if isinstance(item.value, ast.Call):
            func_name = ""
            if isinstance(item.value.func, ast.Name):
                func_name = item.value.func.id
            elif isinstance(item.value.func, ast.Attribute):
                func_name = item.value.func.attr

            if func_name == 'Field':
                # Extraer argumentos posicionales (default está en el primer posicional)
                if item.value.args:
                    field_info['default'] = SignatureExtractor._expr_to_str(item.value.args[0])

                # Extraer keyword arguments
                for kw in item.value.keywords:
                    if kw.arg == 'default':
                        field_info['default'] = SignatureExtractor._expr_to_str(kw.value)
                    elif kw.arg == 'default_factory':
                        field_info['default'] = f"factory:{SignatureExtractor._expr_to_str(kw.value)}"
                    elif kw.arg in ('ge', 'le', 'gt', 'lt', 'min_length', 'max_length'):
                        field_info['constraints'][kw.arg] = SignatureExtractor._expr_to_str(kw.value)
                    elif kw.arg == 'description':
                        desc = SignatureExtractor._expr_to_str(kw.value).strip("'\"")
                        field_info['description'] = desc[:200]

                # FIX 4: Detectar listas de seguridad en default_factory
                if 'factory:' in str(field_info['default']):
                    name_lower = item.target.id.lower()
                    security_patterns = ['blocked', 'allowed', 'blacklist', 'whitelist', 'forbidden', 'banned']
                    if any(p in name_lower for p in security_patterns):
                        field_info['is_security_list'] = True
                        # Intentar extraer valores de la lambda
                        for kw in item.value.keywords:
                            if kw.arg == 'default_factory' and isinstance(kw.value, ast.Lambda):
                                # Buscar lista en el body de la lambda
                                for stmt in ast.walk(kw.value.body):
                                    if isinstance(stmt, ast.List):
                                        field_info['security_values'] = [
                                            SignatureExtractor._expr_to_str(elt).strip("'\"")
                                            for elt in stmt.elts[:30]  # Limitar a 30 elementos
                                        ]
                                        break

        # Detectar asignación simple (sin Field) - para constantes de clase
        elif item.value is not None:
            val_str = SignatureExtractor._expr_to_str(item.value)
            field_info['default'] = val_str[:200] if len(val_str) > 200 else val_str

            # FIX 4: Detectar listas de seguridad directas
            if isinstance(item.value, ast.List):
                name_lower = item.target.id.lower()
                security_patterns = ['blocked', 'allowed', 'blacklist', 'whitelist', 'forbidden', 'banned']
                if any(p in name_lower for p in security_patterns):
                    field_info['is_security_list'] = True
                    field_info['security_values'] = [
                        SignatureExtractor._expr_to_str(elt).strip("'\"")
                        for elt in item.value.elts[:30]
                    ]

        fields.append(field_info)

    return fields


# ==================== FIX 2: CONSTANTES DE MÓDULO v4.1 ====================
def extract_module_constants(tree: ast.AST, content: str) -> List[Dict[str, Any]]:
    """
    ✅ FIX 2: Extrae constantes de módulo (variables en MAYÚSCULAS con valor literal).

    Captura:
    - Variables como MAX_TOKENS_CODE = 8192
    - Strings críticos como CODE_REVIEW_SYSTEM_PROMPT = "..."
    - Comentarios inline asociados

    Filtra:
    - Variables que empiezan con _ (privadas)
    - Valores muy largos (>500 chars)
    - Imports y nombres de módulo
    """
    constants = []
    lines = content.splitlines()

    # Patrones de constantes importantes a capturar
    IMPORTANT_PATTERNS = {
        'TOKENS': 'Límites de tokens por tipo de tarea',
        'TIMEOUT': 'Configuración de timeouts',
        'PROMPT': 'System prompts especializados',
        'PATTERN': 'Patrones regex de detección',
        'THRESHOLD': 'Umbrales de calidad/validación',
        'MAX_': 'Límites máximos',
        'MIN_': 'Límites mínimos',
        'DEFAULT_': 'Valores por defecto',
        'BLOCKED': 'Listas de bloqueo (seguridad)',
        'ALLOWED': 'Listas de permitidos (seguridad)',
    }

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue

        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue

            name = target.id

            # Filtrar: debe ser MAYÚSCULAS, más de 2 chars, no empezar con _
            if not (name.isupper() and len(name) > 2 and not name.startswith('_')):
                continue

            # Filtrar nombres de módulo/magic
            if name in ('__all__', '__version__', '__author__'):
                continue

            value_str = SignatureExtractor._expr_to_str(node.value)

            # Solo capturar si el valor es relativamente corto
            if len(value_str) > 500:
                continue

            # Extraer comentario inline si existe
            comment = ""
            if hasattr(node, 'end_lineno') and node.end_lineno and node.end_lineno <= len(lines):
                line = lines[node.end_lineno - 1]
                if '#' in line:
                    comment = line.split('#', 1)[1].strip()
                    # Limpiar separadores decorativos
                    comment = comment.lstrip('═─━').strip()

            # Determinar categoría
            category = "general"
            for pattern, desc in IMPORTANT_PATTERNS.items():
                if pattern in name:
                    category = desc.lower().replace(' ', '_')
                    break

            # Extraer valor literal para strings importantes
            literal_value = None
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                literal_value = node.value.value[:300]
            elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float, bool)):
                literal_value = str(node.value.value)

            constants.append({
                'name': name,
                'value': value_str,
                'literal_value': literal_value,
                'line': node.lineno,
                'comment': comment,
                'category': category,
                'is_security': any(p in name for p in ['BLOCKED', 'ALLOWED', 'BLACKLIST', 'FORBIDDEN', 'BANNED']),
            })

    return constants


# ==================== FIX 3: DETECCIÓN LAZY PROPERTIES v4.1 ====================
def detect_lazy_properties(node: ast.ClassDef) -> Dict[str, Dict[str, Any]]:
    """
    ✅ FIX 3: Detecta propiedades con patrón Lazy Init.

    Patrón típico en AXIOMA:
        @property
        def coder(self) -> CoderAgent:
            if self._coder is None:
                self._coder = CoderAgent()
            return self._coder

    Retorna dict: {method_name: {'is_lazy': True, 'init_class': 'CoderAgent'}}
    """
    lazy_properties = {}

    for item in node.body:
        if not isinstance(item, ast.FunctionDef):
            continue

        # Verificar si tiene decorador @property
        has_property = any(
            isinstance(d, ast.Name) and d.id == 'property'
            for d in item.decorator_list
        )

        if not has_property:
            continue

        # Buscar patrón: if self._var is None: self._var = ClassName()
        for stmt in ast.walk(item):
            if not isinstance(stmt, ast.If):
                continue

            test = stmt.test
            # Verificar: if self._var is None
            if not isinstance(test, ast.Compare):
                continue

            if not (isinstance(test.left, ast.Attribute) and
                    isinstance(test.left.value, ast.Name) and
                    test.left.value.id == 'self' and
                    test.left.attr.startswith('_')):
                continue

            # Verificar que la comparación es `is None`
            is_none_check = any(
                isinstance(op, ast.Is) and isinstance(comp, ast.Constant) and comp.value is None
                for op, comp in zip(test.ops, test.comparators)
            )

            if not is_none_check:
                continue

            # Buscar la asignación en el body del if: self._var = ClassName()
            init_class = None
            for body_stmt in stmt.body:
                if isinstance(body_stmt, ast.Assign):
                    for target in body_stmt.targets:
                        if (isinstance(target, ast.Attribute) and
                            isinstance(target.value, ast.Name) and
                            target.value.id == 'self' and
                            target.attr == test.left.attr):

                            # Extraer nombre de clase instanciada
                            if isinstance(body_stmt.value, ast.Call):
                                if isinstance(body_stmt.value.func, ast.Name):
                                    init_class = body_stmt.value.func.id
                                elif isinstance(body_stmt.value.func, ast.Attribute):
                                    init_class = body_stmt.value.func.attr
                            elif isinstance(body_stmt.value, ast.Name):
                                init_class = body_stmt.value.id

            lazy_properties[item.name] = {
                'is_lazy': True,
                'init_class': init_class or 'Unknown',
                'backing_field': test.left.attr,
            }
            break

    return lazy_properties

# ==================== VALIDADOR DE CONSISTENCIA ====================
class SignatureValidator:
    """
    ✅ NUEVO v3.2: Valida consistencia entre signatures documentadas y código real
    v3.6: Actualizado para manejar tags y filtros relajados
    """
    def __init__(self):
        self.inconsistencies: List[Dict[str, Any]] = []

    def validate_class_signature(self,
                                 cached_class: Dict[str, Any],
                                 current_class: EnrichedClassInfo) -> Tuple[bool, List[str]]:
        """
        Valida signature de una clase específica.
        Returns: (es_consistente, lista_de_problemas)
        """
        issues = []
        if cached_class.get('name') != current_class.name:
            issues.append(f"Nombre de clase diferente: {cached_class.get('name')} vs {current_class.name}")
        cached_methods = {m['name']: m for m in cached_class.get('methods', [])}
        current_methods = {m.name: m for m in current_class.methods}
        for method_name in cached_methods:
            if method_name not in current_methods:
                issues.append(f"Método eliminado: {method_name}")
        for method_name in current_methods:
            if method_name not in cached_methods:
                issues.append(f"Método agregado: {method_name}")
        for method_name in cached_methods:
            if method_name in current_methods:
                cached_sig = cached_methods[method_name]
                current_sig = current_methods[method_name]
                if cached_sig.get('signature', '') != current_sig.signature_str():
                    issues.append(f"Signature cambiada en método {method_name}")
                cached_params = {p['name']: p for p in cached_sig.get('params', [])}
                current_params = {p.name: p for p in current_sig.params}
                for param_name in cached_params:
                    if param_name not in current_params:
                        issues.append(f"  - Parámetro eliminado: {param_name}")
                for param_name in current_params:
                    if param_name not in cached_params:
                        issues.append(f"  - Parámetro agregado: {param_name}")
        return len(issues) == 0, issues

    def validate_file(self,
                      cached_data: Dict[str, Any],
                      current_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Valida consistencia completa de un archivo.
        Returns: (es_consistente, lista_de_problemas)
        """
        issues = []
        if cached_data.get('file_hash') != current_data.get('file_hash'):
            issues.append("Archivo modificado")
        if cached_data.get('classes_hash') != current_data.get('classes_hash'):
            issues.append("Signatures de clases cambiadas")
        cached_classes = {c['name']: c for c in cached_data.get('classes', [])}
        current_classes = {c['name']: c for c in current_data.get('classes', [])}
        for class_name in cached_classes:
            if class_name in current_classes:
                is_consistent, class_issues = self.validate_class_signature(
                    cached_classes[class_name],
                    EnrichedClassInfo(**current_classes[class_name])
                )
                if not is_consistent:
                    issues.extend([f"[{class_name}] {issue}" for issue in class_issues])
        return len(issues) == 0, issues

    def add_inconsistency(self, file: str, issue_type: str, details: List[str]):
        """Registra una inconsistencia encontrada"""
        self.inconsistencies.append({
            'file': file,
            'type': issue_type,
            'details': details,
            'severity': 'high' if 'eliminado' in str(details).lower() else 'medium'
        })

    def get_report(self) -> str:
        """Genera reporte de inconsistencias"""
        if not self.inconsistencies:
            return "✅ No se encontraron inconsistencias"
        report = ["⚠️ INCONSISTENCIAS DETECTADAS", "=" * 50]
        for inc in self.inconsistencies:
            report.append(f"\n📄 {inc['file']} [{inc['severity'].upper()}]")
            report.append(f"   Tipo: {inc['type']}")
            for detail in inc['details']:
                report.append(f"   - {detail}")
        return "\n".join(report)

    def clear(self):
        """Limpia inconsistencias acumuladas"""
        self.inconsistencies = []

# ==================== ANALIZADOR DE FLUJOS CRÍTICOS ====================
class CriticalFlowAnalyzer:
    """
    ✅ NUEVO v3.3: Analiza y documenta flujos críticos del sistema
    """
    @staticmethod
    def extract_flow_from_code(file_path: Path, function_name: str) -> List[FlowStepInfo]:
        """
        Extrae pasos de flujo desde una función específica
        Returns: Lista de FlowStepInfo
        """
        steps = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = ast.parse(content)
            step_num = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == function_name:
                    for stmt in ast.walk(node):
                        if isinstance(stmt, ast.Call):
                            step_num += 1
                            component = ""
                            if isinstance(stmt.func, ast.Attribute):
                                component = stmt.func.attr
                            elif isinstance(stmt.func, ast.Name):
                                component = stmt.func.id
                            if component:
                                steps.append(FlowStepInfo(
                                    step_number=step_num,
                                    component=component
                                ))
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_extractors_advanced.py:551] excepción degradada (intencional): {_d2e}")
        return steps

    @staticmethod
    def identify_fallback_points(file_path: Path) -> List[str]:
        """
        Identifica puntos de fallback en el código
        Returns: Lista de descripciones de fallback
        """
        fallbacks = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            patterns = [
                (r'except.*:', 'Exception handler'),
                (r'if.*is None', 'Null check fallback'),
                (r'else:', 'Alternative path'),
                (r'try:', 'Try block with potential fallback'),
                (r'default=', 'Default value fallback'),
            ]
            for pattern, desc in patterns:
                if re.search(pattern, content):
                    fallbacks.append(desc)
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_extractors_advanced.py:575] excepción degradada (intencional): {_d2e}")
        return fallbacks

# ==================== UTILIDADES DE DEPENDENCIAS v3.3 ====================
def build_dependency_graph(project_root: Path, python_files: List[Path]) -> Dict[str, DependencyInfo]:
    """
    ✅ NUEVO v3.3: Construye grafo completo de dependencias del proyecto
    Returns: Dict[módulo, DependencyInfo]
    """
    dependency_graph: Dict[str, DependencyInfo] = {}
    for file_path in python_files:
        try:
            dep_info = extract_module_dependencies(file_path, project_root)
            dependency_graph[dep_info.module] = dep_info
        except Exception:
            continue
    dep_counts: Dict[str, int] = {}
    for dep_info in dependency_graph.values():
        for dep in dep_info.depends_on:
            dep_counts[dep] = dep_counts.get(dep, 0) + 1
    for module_name, dep_info in dependency_graph.items():
        dep_info.usage_count = dep_counts.get(module_name, 0)
    return dependency_graph

def identify_critical_dependencies(dependency_graph: Dict[str, DependencyInfo]) -> List[str]:
    """
    ✅ NUEVO v3.3: Identifica dependencias críticas (usadas por muchos módulos)
    Returns: Lista de módulos críticos
    """
    critical = []
    threshold = len(dependency_graph) * 0.3
    for module_name, dep_info in dependency_graph.items():
        if dep_info.usage_count >= threshold:
            critical.append(module_name)
    return critical

# ==================== EJECUCIÓN ====================

if __name__ == "__main__":
    print("Módulo de extractores avanzados. Ejecute axioma_doc_main.py")
    print("Versión: v4.1.1 — axioma_doc_extractors_advanced")
