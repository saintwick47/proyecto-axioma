#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# axioma_doc_core.py - Estructuras de datos y constantes para el documentador AXIOMA
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_core.py
# Autor: SaintWick
# Versión: 4.1
#
# Define las estructuras de datos fundamentales utilizadas por el
# documentador: estadísticas de generación, información de archivos
# sensibles, campos Pydantic, propiedades lazy, constantes de módulo
# y notas de changelog. Proporciona constantes de salida y placeholders
# de seguridad para la documentación generada.
# ═══════════════════════════════════════════════════════════════
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from axioma_doc_base import PROJECT_ROOT

# ==================== CONSTANTES DE SALIDA ====================
OUTPUT_MD_FILE = PROJECT_ROOT / "docs" / "AXIOMA_COMPLETE_CONTEXT.md"

# ==================== SECURITY PATCH v1.0 ====================
SENSITIVE_FILES_PLACEHOLDER = "[🔐 ARCHIVO SENSIBLE — CONTENIDO RESTRINGIDO]"
SENSITIVE_CLASS_PLACEHOLDER = "[🔐 Clase en archivo sensible — contenido no documentado]"
SENSITIVE_CONSTANT_PLACEHOLDER = "[REDACTED]"
SENSITIVE_BADGE = "[🔐 SENSIBLE]"

# ==================== ESTADÍSTICAS DE GENERACIÓN ====================
@dataclass
class DocGenerationStats:
    """Estadísticas de una ejecución del documentador v3.5"""
    total_files: int = 0
    analyzed_files: int = 0
    generation_time_ms: float = 0.0
    # ✅ SECURITY PATCH v1.0
    sensitive_files_excluded: int = 0
    secrets_redacted: int = 0

# ==================== SECURITY PATCH v1.0: INFO DE ARCHIVO SENSIBLE ====================
@dataclass
class SensitiveFileInfo:
    """Metadata segura para mostrar en la doc cuando un archivo es sensible."""
    relative_path: str
    is_sensitive: bool
    matched_pattern: str = ""
    reason: str = ""
    placeholder: str = SENSITIVE_FILES_PLACEHOLDER

    def to_dict(self) -> Dict[str, Any]:
        return {
            'relative_path': self.relative_path,
            'is_sensitive': self.is_sensitive,
            'matched_pattern': self.matched_pattern,
            'reason': self.reason,
            'placeholder': self.placeholder,
        }

# ==================== FIX 1: CAMPO PYDANTIC ENRIQUECIDO ====================
@dataclass
class PydanticFieldInfo:
    """✅ FIX 1: Información completa de un campo Pydantic/BaseSettings"""
    name: str
    type_hint: str = ""
    default: Optional[str] = None
    constraints: Dict[str, str] = field(default_factory=dict)  # ge, le, gt, lt, etc.
    description: str = ""
    is_security_list: bool = False
    security_values: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'type_hint': self.type_hint,
            'default': self.default,
            'constraints': self.constraints,
            'description': self.description,
            'is_security_list': self.is_security_list,
            'security_values': self.security_values[:10],  # Limitar serialización
        }

# ==================== FIX 3: LAZY PROPERTY INFO ====================
@dataclass
class LazyPropertyInfo:
    """✅ FIX 3: Información de una propiedad con patrón Lazy Init"""
    method_name: str
    init_class: str = ""
    backing_field: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            'method_name': self.method_name,
            'init_class': self.init_class,
            'backing_field': self.backing_field,
        }

# ==================== FIX 2: CONSTANTE DE MÓDULO ====================
@dataclass
class ModuleConstantInfo:
    """✅ FIX 2: Información de una constante de módulo (MAYÚSCULAS)"""
    name: str
    value: str = ""
    literal_value: Optional[str] = None
    line: int = 0
    comment: str = ""
    category: str = "general"
    is_security: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'value': self.value[:200],
            'literal_value': self.literal_value[:200] if self.literal_value else None,
            'line': self.line,
            'comment': self.comment[:100],
            'category': self.category,
            'is_security': self.is_security,
        }

# ==================== FIX 5: CHANGELOG NOTE ====================
@dataclass
class ChangelogNote:
    """✅ FIX 5: Nota de changelog inline (FIX, BUG, CORREGIDO, etc.)"""
    note_type: str  # fix, bug, fixed, warning, security
    description: str = ""
    line: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'note_type': self.note_type,
            'description': self.description[:200],
            'line': self.line,
        }

if __name__ == "__main__":
    print("Módulo de constantes y estructuras. Ejecute axioma_doc_main.py")
    print(f"OUTPUT_MD_FILE:   {OUTPUT_MD_FILE}")
    print("✅ v4.1: PydanticFieldInfo, LazyPropertyInfo, ModuleConstantInfo, ChangelogNote")
    print("🔒 v1.0: SensitiveFileInfo + placeholders de seguridad")
