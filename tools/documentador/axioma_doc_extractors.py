#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_extractors.py
# Autor: SaintWick
"""
axioma_doc_extractors.py - Extracción y análisis de código Python vía AST para AXIOMA.

Este módulo provee herramientas para extraer información estructural de archivos Python
mediante AST: firmas de funciones, clases, atributos, imports, constantes, etc.
Se utiliza en el sistema de documentación de AXIOMA para generar reportes de
arquitectura, dependencias y flujos críticos.

Características:
- Extracción de firmas de funciones con tipos, defaults, decoradores y docstrings.
- Análisis de clases: herencia, dataclasses, enums, modelos Pydantic (con validaciones).
- Detección de constantes de módulo (MAYÚSCULAS) con redacción de secretos.
- Identificación de propiedades lazy (@property + if self._x is None).
- Extracción de imports (absolutos y relativos) y dependencias.
- Extracción de tags inline (# ✅ [TAG]: desc, # Versión:, # FIX:, # BUG:, etc.).
- Validación de consistencia de firmas mediante hash.
- Análisis de flujos críticos y dependencias.
"""
import ast
import re
import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Any, Optional, Set
from pathlib import Path

# ✅ SECURITY PATCH v1.0: Filtro de secrets para constantes
try:
    from sensitive_filter import make_default_filter, SensitiveFileFilter
    _SECURITY_FILTER: Optional[SensitiveFileFilter] = make_default_filter()
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_FILTER = None
    _SECURITY_AVAILABLE = False


def safe_constant_value(name: str, value: str) -> str:
    """
    ✅ SECURITY PATCH v1.0
    Pasa el valor de una constante por el filtro de redacción antes de guardarlo
    en cualquier estructura de la documentación.

    Si el nombre de la constante sugiere sensibilidad (TOKEN, KEY, SECRET, etc.)
    y el valor parece un secret, lo redacta completamente.
    Si el nombre no es sensible pero el valor matchea un patrón conocido
    (GitHub PAT, OpenAI key, etc.), también lo redacta.
    """
    if not _SECURITY_AVAILABLE or _SECURITY_FILTER is None:
        # Sin filtro: aplicar redacción mínima por regex
        if value and "ghp_" in value:
            return "[REDACTED_GITHUB_PAT]"
        if value and "sk-" in value and len(value) > 20:
            return "[REDACTED_API_KEY]"
        return value

    return _SECURITY_FILTER.safe_constant_value(name, value)

# ==================== PATRONES DE EXTRACCIÓN INLINE ====================
# Detecta # ✅ [TAG]: descripción al inicio de líneas o en docstrings
TAG_PATTERN = re.compile(r'#\s*✅\s*\[([A-Z0-9]+)\]\s*:\s*(.+?)(?:\r?\n|$)', re.IGNORECASE)
# Detecta # Versión: x.x.x
VERSION_PATTERN = re.compile(r'#\s*Versión:\s*(.+?)(?:\r?\n|$)', re.IGNORECASE)

# ==================== ESTRUCTURAS DE DATOS v4.1 ====================
@dataclass
class ParamInfo:
    """Información de un parámetro de función v3.3"""
    name: str
    type_hint: str = ""
    default: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Convierte a diccionario para serialización"""
        return {
            'name': self.name,
            'type_hint': self.type_hint,
            'default': self.default,
            'description': self.description
        }

    def signature_str(self) -> str:
        """Genera string de signature para este parámetro"""
        sig = self.name
        if self.type_hint:
            sig += f": {self.type_hint}"
        if self.default:
            sig += f" = {self.default}"
        return sig

@dataclass
class FunctionSignature:
    """Signature completa de una función o método v4.1"""
    name: str
    params: List[ParamInfo]
    return_type: str = ""
    docstring: str = ""
    is_async: bool = False
    decorators: List[str] = field(default_factory=list)
    line_number: int = 0
    ai_summary: str = ""
    tags: List[str] = field(default_factory=list)  # ✅ FASE 1: Etiquetas inline detectadas
    is_private: bool = False                       # ✅ FASE 1: Flag de visibilidad
    changelog_notes: List[Tuple[str, str]] = field(default_factory=list)  # ✅ FIX 5: Notas de changelog

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización"""
        return {
            'name': self.name,
            'params': [p.to_dict() for p in self.params],
            'return_type': self.return_type,
            'docstring': self.docstring,
            'is_async': self.is_async,
            'decorators': self.decorators,
            'line_number': self.line_number,
            'ai_summary': self.ai_summary,
            'tags': self.tags,
            'is_private': self.is_private,
            'changelog_notes': self.changelog_notes
        }

    def signature_str(self) -> str:
        """Genera string completo de signature para validación"""
        params_str = ", ".join(p.signature_str() for p in self.params)
        async_prefix = "async " if self.is_async else ""
        return_str = f" -> {self.return_type}" if self.return_type else ""
        return f"{async_prefix}def {self.name}({params_str}){return_str}"

    def compute_hash(self) -> str:
        """Computa hash SHA-256 de esta signature para validación de consistencia"""
        hasher = hashlib.sha256()
        sig_data = {
            'name': self.name,
            'params': [p.to_dict() for p in self.params],
            'return_type': self.return_type,
            'is_async': self.is_async
        }
        hasher.update(json.dumps(sig_data, sort_keys=True).encode('utf-8'))
        return hasher.hexdigest()

@dataclass
class EnrichedClassInfo:
    """Clase con información completa incluyendo métodos detallados v4.1"""
    name: str
    file: str
    line_number: int
    docstring: str
    bases: List[str]
    is_dataclass: bool
    is_enum: bool
    is_pydantic: bool
    has_fallback_marker: bool
    methods: List[FunctionSignature] = field(default_factory=list)
    attributes: List[Tuple[str, str]] = field(default_factory=list)
    ai_summary: str = ""
    version_tags: Dict[str, str] = field(default_factory=dict)
    # ✅ FIX 1: Campos Pydantic con validaciones
    pydantic_fields: List[Dict[str, Any]] = field(default_factory=list)
    # ✅ FIX 3: Propiedades Lazy Init
    lazy_properties: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # ✅ FIX 5: Notas de changelog inline
    changelog_notes: List[Tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización"""
        return {
            'name': self.name,
            'file': self.file,
            'line_number': self.line_number,
            'docstring': self.docstring,
            'bases': self.bases,
            'is_dataclass': self.is_dataclass,
            'is_enum': self.is_enum,
            'is_pydantic': self.is_pydantic,
            'has_fallback_marker': self.has_fallback_marker,
            'methods': [m.to_dict() for m in self.methods],
            'attributes': [{'name': a[0], 'type': a[1]} for a in self.attributes],
            'ai_summary': self.ai_summary,
            'version_tags': self.version_tags,
            'pydantic_fields': self.pydantic_fields,
            'lazy_properties': self.lazy_properties,
            'changelog_notes': self.changelog_notes
        }

    def compute_signature_hash(self) -> str:
        """Computa hash de signatures de todos los métodos para validación"""
        hasher = hashlib.sha256()
        methods_data = []
        for method in sorted(self.methods, key=lambda m: m.name):
            methods_data.append({
                'name': method.name,
                'signature': method.signature_str(),
                'params': [p.to_dict() for p in method.params]
            })
        hasher.update(json.dumps(methods_data, sort_keys=True).encode('utf-8'))
        return hasher.hexdigest()

@dataclass
class DependencyInfo:
    """Información de dependencia entre módulos - NUEVO v3.3"""
    module: str
    depends_on: List[str] = field(default_factory=list)
    dependency_type: str = "internal"
    usage_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización"""
        return {
            'module': self.module,
            'depends_on': self.depends_on,
            'dependency_type': self.dependency_type,
            'usage_count': self.usage_count
        }

@dataclass
class FlowStepInfo:
    """Información de paso de flujo crítico - NUEVO v3.3"""
    step_number: int
    component: str
    fallback: Optional[str] = None
    timeout: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario para serialización"""
        return {
            'step_number': self.step_number,
            'component': self.component,
            'fallback': self.fallback,
            'timeout': self.timeout
        }

# ==================== UTILIDADES DE ETIQUETAS INLINE (FASE 1 + FIX 5) ====================
# ✅ FIX 5: Patrones ampliados para capturar más tipos de comentarios críticos
ENHANCED_TAG_PATTERNS = [
    # Patrones existentes
    (re.compile(r'#\s*✅\s*\[([A-Z0-9]+)\]\s*:\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'tag'),
    (re.compile(r'#\s*Versión:\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'version'),
    # FIX 5: Nuevos patrones
    (re.compile(r'#\s*(?:FIX|CORRECCI[ÓO]N)\s*[:]?\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'fix'),
    (re.compile(r'#\s*BUG\s*[:]?(\w*)\s*[:.]?\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'bug'),
    (re.compile(r'#\s*CORREGIDO\s*[:.]?\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'fixed'),
    (re.compile(r'#\s*⚠️\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'warning'),
    (re.compile(r'#\s*SEGURIDAD\s*[:.]?\s*(.+?)(?:\r?\n|$)', re.IGNORECASE), 'security'),
]

def extract_enhanced_inline_tags(content: str, start_line: int, end_line: int) -> List[Tuple[str, str, str]]:
    """
    ✅ FIX 5: Extrae tags inline ampliados.

    Retorna lista de tuples: (tag_type, tag_key, descripción)
    Tipos: tag, version, fix, bug, fixed, warning, security
    """
    lines = content.splitlines()
    scope = lines[max(0, start_line-3):min(len(lines), end_line+3)]
    tags_found = []

    for line in scope:
        for pattern, tag_type in ENHANCED_TAG_PATTERNS:
            match = pattern.search(line)
            if match:
                if tag_type == 'tag':
                    tags_found.append((tag_type, match.group(1).strip(), match.group(2).strip()))
                elif tag_type == 'bug':
                    bug_id = match.group(1).strip()
                    desc = match.group(2).strip()
                    key = f"BUG_{bug_id}" if bug_id else "BUG"
                    tags_found.append((tag_type, key, desc))
                else:
                    tags_found.append((tag_type, tag_type.upper(), match.group(1).strip()))
                break  # Un tag por línea

    return tags_found

def extract_inline_tags(content: str, start_line: int, end_line: int) -> List[Tuple[str, str]]:
    """
    ✅ FASE 1 + FIX 5: Extrae etiquetas inline.
    Ahora usa patrones ampliados para capturar FIX, BUG, CORREGIDO, etc.
    Retorna lista de tuples (tag, descripción).
    """
    enhanced_tags = extract_enhanced_inline_tags(content, start_line, end_line)

    # Convertir a formato legacy (solo tag + descripción) para compatibilidad
    legacy_tags = []
    for tag_type, tag_key, desc in enhanced_tags:
        if tag_type in ('tag', 'version'):
            legacy_tags.append((tag_key, desc))
        else:
            # FIX 5: Nuevos tags se almacenan con prefijo de tipo
            legacy_tags.append((f"{tag_type.upper()}_{tag_key}", desc))

    return legacy_tags

# ==================== EXTRACTOR DE SIGNATURES ====================
class SignatureExtractor:
    """
    Extrae signatures completas de funciones y métodos desde AST.
    v4.1: Mejorado para validación de consistencia, hash computation, dependencias, filtro relajado y patrones lazy.
    """
    CRITICAL_DECORATORS = {'property', 'classmethod', 'staticmethod', 'abstractmethod', 'override'}

    @staticmethod
    def extract_param_info(arg: ast.arg, defaults: Dict[str, ast.expr] = None) -> ParamInfo:
        """Extrae información completa de un parámetro"""
        type_hint = ""
        if arg.annotation:
            type_hint = SignatureExtractor._annotation_to_str(arg.annotation)
        default = ""
        if defaults and arg.arg in defaults:
            default = SignatureExtractor._expr_to_str(defaults[arg.arg])
        return ParamInfo(
            name=arg.arg,
            type_hint=type_hint,
            default=default
        )

    @staticmethod
    def _annotation_to_str(annotation: ast.expr) -> str:
        """Convierte anotación AST a string legible, manejando genéricos async"""
        try:
            if isinstance(annotation, ast.Name):
                return annotation.id
            elif isinstance(annotation, ast.Constant):
                return repr(annotation.value)
            elif isinstance(annotation, ast.Attribute):
                return f"{annotation.value.id}.{annotation.attr}" if hasattr(annotation.value, 'id') else annotation.attr
            elif isinstance(annotation, ast.Subscript):
                value = SignatureExtractor._annotation_to_str(annotation.value)
                slice_val = SignatureExtractor._annotation_to_str(annotation.slice)
                return f"{value}[{slice_val}]"
            elif isinstance(annotation, ast.Tuple):
                elts = [SignatureExtractor._annotation_to_str(e) for e in annotation.elts]
                return ', '.join(elts)
            elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
                left = SignatureExtractor._annotation_to_str(annotation.left)
                right = SignatureExtractor._annotation_to_str(annotation.right)
                return f"{left} | {right}"
            elif hasattr(ast, 'unparse'):
                return ast.unparse(annotation)
            else:
                return "Any"
        except Exception:
            return "Any"

    @staticmethod
    def _expr_to_str(expr: ast.expr) -> str:
        """Convierte expresión AST a string"""
        try:
            if isinstance(expr, ast.Constant):
                return repr(expr.value)
            elif isinstance(expr, ast.Name):
                return expr.id
            elif isinstance(expr, ast.List):
                return "[]"
            elif isinstance(expr, ast.Dict):
                return "{}"
            elif isinstance(expr, ast.Tuple):
                return "()"
            elif isinstance(expr, ast.Call):
                func_name = ""
                if isinstance(expr.func, ast.Name):
                    func_name = expr.func.id
                elif isinstance(expr.func, ast.Attribute):
                    func_name = expr.func.attr
                return f"{func_name}(...)"
            elif hasattr(ast, 'unparse'):
                return ast.unparse(expr)
            return "..."
        except Exception:
            return "..."

    @staticmethod
    def should_include_method(name: str, decorators: List[str], docstring: str, tags: List[str]) -> bool:
        """
        ✅ FASE 1: Lógica de filtro relajado para métodos privados.
        Incluye _métodos si:
        1. Tienen docstring no vacío
        2. Contienen etiquetas # ✅ [X]:
        3. Están decorados con @property, @classmethod, etc.
        """
        if not name.startswith('_'):
            return True
        if name in ('__init__', '__call__', '__str__', '__repr__', '__enter__', '__exit__'):
            return True
        if any(d in SignatureExtractor.CRITICAL_DECORATORS for d in decorators):
            return True
        if docstring and len(docstring.strip()) > 5:
            return True
        if tags:
            return True
        return False

    @staticmethod
    def extract_function_signature(node: ast.FunctionDef, source_lines: List[str] = None) -> FunctionSignature:
        """Extrae la signature completa de una función/método"""
        all_args = node.args.args
        defaults = node.args.defaults
        defaults_map: Dict[str, ast.expr] = {}
        if defaults:
            offset = len(all_args) - len(defaults)
            for i, default in enumerate(defaults):
                arg = all_args[offset + i]
                defaults_map[arg.arg] = default

        params = []
        for arg in all_args:
            if arg.arg in ('self', 'cls'):
                continue
            params.append(SignatureExtractor.extract_param_info(arg, defaults_map))

        if node.args.vararg:
            params.append(ParamInfo(
                name=f"*{node.args.vararg.arg}",
                type_hint=SignatureExtractor._annotation_to_str(node.args.vararg.annotation)
                if node.args.vararg.annotation else ""
            ))
        if node.args.kwarg:
            params.append(ParamInfo(
                name=f"**{node.args.kwarg.arg}",
                type_hint=SignatureExtractor._annotation_to_str(node.args.kwarg.annotation)
                if node.args.kwarg.annotation else ""
            ))

        return_type = ""
        if node.returns:
            return_type = SignatureExtractor._annotation_to_str(node.returns)

        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(f"{dec.value.id}.{dec.attr}" if hasattr(dec.value, 'id') else dec.attr)
            elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                decorators.append(dec.func.id)

        is_async = isinstance(node, ast.AsyncFunctionDef)
        docstring = ast.get_docstring(node) or ""

        # ✅ FASE 1 + FIX 5: Extracción de tags inline ampliados
        tags = []
        changelog_notes = []
        if source_lines and node.lineno and node.end_lineno:
            content_scope = "\n".join(source_lines[node.lineno-1:node.end_lineno])
            raw_tags = extract_enhanced_inline_tags(content_scope, 0, node.end_lineno - node.lineno)
            for tag_type, tag_key, desc in raw_tags:
                if tag_type in ('tag', 'version'):
                    tags.append(tag_key)
                else:
                    changelog_notes.append((f"{tag_type.upper()}_{tag_key}", desc))

        return FunctionSignature(
            name=node.name,
            params=params,
            return_type=return_type,
            docstring=docstring,
            is_async=is_async,
            decorators=decorators,
            line_number=node.lineno,
            tags=tags,
            is_private=node.name.startswith('_'),
            changelog_notes=changelog_notes
        )

    @staticmethod
    def extract_class_attributes(node: ast.ClassDef) -> List[Tuple[str, str]]:
        """Extrae atributos de clase con sus tipos"""
        attributes = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attr_name = item.target.id
                attr_type = SignatureExtractor._annotation_to_str(item.annotation)
                attributes.append((attr_name, attr_type))
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.AnnAssign):
                        if isinstance(stmt.target, ast.Attribute) and isinstance(stmt.target.value, ast.Name):
                            if stmt.target.value.id == 'self':
                                attr_name = stmt.target.attr
                                attr_type = SignatureExtractor._annotation_to_str(stmt.annotation)
                                attributes.append((attr_name, attr_type))
                    elif isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                                if target.value.id == 'self':
                                    attributes.append((target.attr, ""))
        seen = set()
        result = []
        for attr in attributes:
            if attr[0] not in seen and not attr[0].startswith('_'):
                seen.add(attr[0])
                result.append(attr)
        return result[:20]

    @staticmethod
    def extract_module_imports(tree: ast.AST, current_filepath: str = "") -> Dict[str, List[str]]:
        """
        Extrae imports categorizados (internos vs externos).
        ✅ FIX v4.1.2: Ahora soporta imports relativos (from . import X, from .. import Y)
        Resuelve los puntos (.) mapeándolos a la ruta física del directorio actual.
        """
        imports = {'internal': [], 'external': []}

        def _resolve_relative(level: int, module_name: Optional[str], imported_names: List[str]) -> List[str]:
            """Helper interno para resolver la ruta física de un import relativo."""
            if not current_filepath:
                return [module_name] if module_name else imported_names

            current_dir = Path(current_filepath).parent
            # Subir niveles de directorio según los puntos (from .. = 2 niveles)
            base_dir = current_dir
            for _ in range(level - 1):
                base_dir = base_dir.parent

            if module_name:
                # Ej: from .modulo import X -> base_dir/modulo
                base_path = str(base_dir / module_name.replace('.', '/'))
                return [base_path]
            else:
                # Ej: from . import X, Y -> base_dir/X, base_dir/Y
                return [str(base_dir / name) for name in imported_names]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith('src.') or alias.name.startswith('axioma') or alias.name.startswith('config'):
                        imports['internal'].append(alias.name)
                    else:
                        imports['external'].append(alias.name)

            elif isinstance(node, ast.ImportFrom):
                # ✅ FIX v4.1.2: Capturar imports relativos donde node.module es None
                if node.level > 0:
                    imported_names = [alias.name for alias in node.names]
                    resolved = _resolve_relative(node.level, node.module, imported_names)
                    imports['internal'].extend(resolved)

                elif node.module:
                    if node.module.startswith('src.') or node.module.startswith('axioma') or node.module.startswith('config'):
                        imports['internal'].append(node.module)
                    else:
                        imports['external'].append(node.module)

        # Limpiar duplicados manteniendo el orden
        imports['internal'] = list(set(imports['internal']))
        imports['external'] = list(set(imports['external']))
        return imports

# ==================== FUNCIONES DE EXTRACCIÓN ====================
def extract_classes_defined_only(tree: ast.AST, content: str, filepath: str) -> List[Any]:
    """
    Extrae SOLO clases DEFINIDAS en este archivo (no importadas).
    v3.6: Retorna ClassDefinitionInfo desde axioma_doc_base con soporte para tags
    """
    from axioma_doc_base import ClassDefinitionInfo
    classes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            docstring = ast.get_docstring(node) or ""
            has_fallback = any(marker in docstring.lower() for marker in [
                'fallback', 'backup', 'compatibility', 'compat',
                'si no está disponible', 'if not available'
            ])
            bases = extract_bases(node)
            is_pydantic = any(b in ['BaseModel', 'BaseSettings'] for b in bases)
            is_dataclass = any(
                isinstance(dec, ast.Name) and dec.id == 'dataclass'
                for dec in node.decorator_list
            )
            is_enum = any('Enum' in b for b in bases)

            version_tags = {}
            raw_tags = extract_inline_tags(content, node.lineno, node.end_lineno or node.lineno + 50)
            for tag_key, tag_desc in raw_tags:
                version_tags[tag_key] = tag_desc

            classes.append(ClassDefinitionInfo(
                name=node.name,
                file=filepath,
                line_number=node.lineno,
                docstring=docstring[:200],
                bases=bases,
                is_dataclass=is_dataclass,
                is_enum=is_enum,
                is_pydantic=is_pydantic,
                has_fallback_marker=has_fallback
            ))
    return classes

def extract_bases(node: ast.ClassDef) -> List[str]:
    """Extrae nombres de clases base"""
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(f"{base.value.id}.{base.attr}" if hasattr(base.value, 'id') else base.attr)
    return bases

def extract_function_signatures(tree: ast.AST, source_lines: List[str] = None) -> List[FunctionSignature]:
    """Extrae signatures de funciones de nivel superior del módulo"""
    signatures = []
    if hasattr(tree, 'body'):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = SignatureExtractor.extract_function_signature(node, source_lines)
                if SignatureExtractor.should_include_method(sig.name, sig.decorators, sig.docstring, sig.tags):
                    signatures.append(sig)
    return signatures

def extract_functions(tree: ast.AST, source_lines: List[str] = None) -> List[str]:
    """Extrae funciones del nivel superior"""
    functions = []
    if hasattr(tree, 'body'):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if SignatureExtractor.should_include_method(node.name, [], ast.get_docstring(node) or "", []):
                    functions.append(node.name)
    return functions

def extract_imports(tree: ast.AST, current_filepath: str = "") -> List[str]:
    """
    Extrae imports del archivo.
    ✅ FIX v4.1.2: Incluye resolución de imports relativos (from . import X).
    """
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend([alias.name for alias in node.names])

        elif isinstance(node, ast.ImportFrom):
            # ✅ FIX v4.1.2: Imports relativos
            if node.level > 0:
                if not current_filepath:
                    continue

                current_dir = Path(current_filepath).parent
                base_dir = current_dir
                for _ in range(node.level - 1):
                    base_dir = base_dir.parent

                if node.module:
                    # from .modulo import X
                    imports.append(str(base_dir / node.module.replace('.', '/')))
                else:
                    # from . import X, Y, Z
                    for alias in node.names:
                        imports.append(str(base_dir / alias.name))

            elif node.module:
                imports.append(node.module)

    return list(set(imports))

def extract_dataclasses(tree: ast.AST) -> List[str]:
    """Extrae dataclasses del nivel superior del módulo."""
    dataclasses = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Name) and decorator.id == 'dataclass':
                    dataclasses.append(node.name)
                    break
    return dataclasses

def extract_enums(tree: ast.AST) -> List[str]:
    """Extrae enums del nivel superior del módulo."""
    enums = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and 'Enum' in base.id:
                    enums.append(node.name)
                    break
    return enums

# ==================== RE-EXPORTS (refactorización v4.1.1) ====================
# axioma_doc_extractors_advanced contiene la segunda mitad de este módulo.
# Se re-exporta aquí para preservar la interfaz pública sin cambios.
from axioma_doc_extractors_advanced import (
    compute_file_hash,
    compute_classes_signature_hash,
    extract_all_signatures_from_file,
    extract_module_dependencies,
    extract_pydantic_fields,
    extract_module_constants as _extract_module_constants_raw,
    detect_lazy_properties,
    SignatureValidator,
    CriticalFlowAnalyzer,
    build_dependency_graph,
    identify_critical_dependencies,
)

# ✅ SECURITY PATCH v1.0: Wrapper que aplica redacción a las constantes extraídas
def extract_module_constants(filepath: str, content: Optional[str] = None):
    """
    Versión segura de extract_module_constants.
    Aplica safe_constant_value() a cada valor antes de retornarlo.
    """
    raw = _extract_module_constants_raw(filepath, content)
    if not raw:
        return raw

    # raw es una lista de ModuleConstantInfo
    for c in raw:
        # Redactar el valor
        if hasattr(c, 'value') and c.value:
            c.value = safe_constant_value(c.name, c.value)
        if hasattr(c, 'literal_value') and c.literal_value:
            c.literal_value = safe_constant_value(c.name, c.literal_value)
        # Marcar como security si el nombre lo sugiere
        if hasattr(c, 'name') and c.name:
            name_upper = c.name.upper()
            if any(h in name_upper for h in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
                c.is_security = True

    return raw

if __name__ == "__main__":
    print("Módulo de extracción de código para AXIOMA. Utilice las funciones de alto nivel como extract_all_signatures_from_file()")
    print("Funciones principales: extract_classes_defined_only, extract_function_signatures, extract_imports, extract_module_constants, etc.")
    print("Disponibles: ParamInfo, FunctionSignature, EnrichedClassInfo, DependencyInfo, FlowStepInfo")
