#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# structure_detector.py - Detector de Estructura y Contenido (INTELIGENTE)
# Ruta: tools/structure_detector.py
# Autor: SaintWick
# Versión: 0.3.2 (versión DEL DETECTOR — no confundir con la del proyecto)
#
# Escanea el proyecto y muestra la estructura real del código,
# filtrando ruido (Git internals, node_modules, caches, checkouts
# externos) y ocultando contenido sensible (tokens, credenciales).
# Genera un reporte Markdown con árbol, métricas honestas y estado
# del repositorio Git.
#
# ✅ v0.3.0 (corrección completa):
#   - EXCLUDE_DIRS solo aplica a DIRECTORIOS (antes también excluía
#     archivos como .env, impidiendo mostrarlos redactados).
#   - Los internos de .git (objects/refs/hooks/info/logs) se excluyen
#     SOLO cuando están bajo .git/ — ya no excluyen carpetas del proyecto
#     con esos nombres (p. ej. logs/ del proyecto).
#   - Detecta binarios por extensión Y por bytes nulos (antes leía
#     .wav/.coverage como texto e inflaba líneas/chars).
#   - Métricas honestas: "Líneas de Código" = solo archivos de código;
#     "Funciones" incluye métodos; se añaden métricas de archivos Python,
#     binarios y de texto total.
#   - deepseek-harness/ (checkout externo del harness) excluido.
#   - Reporta la versión declarada del proyecto (src/__init__.py).
# ═══════════════════════════════════════════════════════════════
import os
import sys
import ast
import subprocess
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any, Set
from dataclasses import dataclass, field, asdict

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9w (FIX VERSIONES): `AXIOMA_VERSION` era el nombre de la versión DEL
# DETECTOR (0.3.0) pero el reporte lo imprimía junto a la versión del proyecto
# y el lector las confundía (`STRUCTURE_REPORT.md` parecía declarar v0.3.0/0.3.1
# cuando el proyecto va por 0.6.9). Ahora:
#   - `DETECTOR_VERSION` (fuente única) = versión de ESTA herramienta.
#   - La versión DEL PROYECTO se lee siempre de `src/__init__.py`
#     (`detect_declared_version`) y se etiqueta explícitamente como tal.
DETECTOR_VERSION = "0.3.2"   # ✅ v0.3.2: commits recientes + archivos tocados
# Alias legacy: nada dentro del repo lo importa, pero se mantiene para no romper
# scripts externos. OJO: es la versión del detector, NO la del proyecto.
AXIOMA_VERSION = DETECTOR_VERSION

# ✅ v0.2.3: Archivos específicos a excluir del reporte (seguridad + ruido)
# Estos archivos NO aparecerán en el árbol, tablas ni métricas del MD.
# ✅ v0.3.0: añadidos .coverage (artefacto pytest) y .directory (metadata de escritorio)
# ✅ v0.3.1: EXCLUSIÓN TOTAL de .env* y push.py (no se listan ni redactan) e inforchat/.
EXCLUDE_FILES: Set[str] = {
    # 🔐 CRÍTICO: Archivos con credenciales expuestas — NUNCA listar
    "tokens github.txt",      # Contiene PAT de GitHub — NUNCA mostrar en reportes
    "tokens.txt",             # Variante genérica
    "passwords.txt",          # Archivo de contraseñas
    "credentials.txt",        # Archivo de credenciales
    # ✅ v0.3.1: Variables de entorno reales — exclusión TOTAL (ni listar ni redactar)
    ".env", ".env.local", ".env.development",
    ".env.production", ".env.staging", ".env.test",
    # ✅ v0.3.1: Script de push remoto — exclusión TOTAL por política de seguridad
    "push.py",
    # 📦 RUIDO: Archivos binarios/voluminosos no relevantes para la estructura
    "axioma-v1.0.0.bundle",   # Bundle binario de 48K líneas
    ".coverage",              # Artefacto de pytest-cov (binario SQLite)
    ".directory",             # Metadata de escritorio (KDE/Dolphin)
}

# Directorios a omitir COMPLETAMENTE (no escanear su contenido).
# ✅ v0.3.0: SOLO se aplican a directorios (no a archivos con el mismo nombre).
# Los internos de .git se excluyen por estar bajo un padre ".git" (ver _should_exclude),
# no por nombre: así "logs/" del proyecto NO queda excluido (se muestra resumido).
EXCLUDE_DIRS: Set[str] = {
    # Entornos virtuales y caches de herramientas
    "venv", ".venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "htmlcov", ".tox", ".nox", "node_modules",
    ".idea", ".vscode", ".ruff_cache",
    "dist", "build", ".eggs",
    ".cache", ".parcel-cache", ".next", ".nuxt",
    "coverage", ".nyc_output",
    # ✅ v0.2.5: La carpeta data/ completa (backups, binarios, BD) NO se escanea
    "data",
    # ✅ v0.3.0: Checkout externo del harness (14K+ archivos, no es código de AXIOMA)
    "deepseek-harness",
    # ✅ v0.3.1: Notas/planes internos de chat (contenido privado del desarrollador)
    "inforchat",
}

# Directorios a escanear pero NO contar en progreso (datos, caché, etc.)
DATA_DIRS: Set[str] = {".git", "logs", "docs", "assets", "static", "media"}  # data ya excluido

# Extensiones a excluir del conteo de código
EXCLUDE_EXTENSIONS: Set[str] = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll",
    ".exe", ".bin", ".dat", ".db", ".sqlite",
    ".lock", ".pid", ".log", ".bak", ".swp", ".swo", ".map",
}

# Extensiones de código para análisis AST
CODE_EXTENSIONS: Set[str] = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".scala", ".clj", ".ex", ".exs", ".erl", ".hs",
    ".lua", ".r", ".R", ".jl", ".dart",
}

# Extensiones binarias
BINARY_EXTENSIONS: Set[str] = {
    # Imágenes
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tiff", ".tif",
    # Audio
    ".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a", ".wma",
    # Video
    ".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".wmv",
    # Documentos y archivos comprimidos
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z", ".zst",
    # Ejecutables y librerías
    ".exe", ".bin", ".so", ".dll", ".dylib", ".a", ".lib",
    ".bundle", ".whl", ".egg", ".jar", ".war", ".ear",
    # Fuentes tipográficas
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Datos binarios (ML, datasets)
    ".pkl", ".npy", ".npz", ".parquet", ".arrow", ".feather",
    ".db", ".sqlite", ".sqlite3",
    # Compilados
    ".class", ".o", ".obj",
    # ✅ v0.2.1: Formatos binarios de LanceDB
    ".lance",      # Datos vectoriales de LanceDB
    ".txn",        # Transacciones de LanceDB
    ".manifest",   # Manifests de versiones de LanceDB
}

# Headers conocidos que NO constituyen implementación real en .py
_KNOWN_EMPTY_PY_HEADERS: Set[str] = {
    "",
    "# AXIOMA Package",
    "# AXIOMA Tests",
    "# -*- coding: utf-8 -*-",
}

# Archivos críticos del proyecto
CRITICAL_FILES: List[str] = [
    "README.md", "README.rst", "README.txt", "README",
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE",
    "CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG",
    "CONTRIBUTING.md", "CONTRIBUTING.rst",
    "CODE_OF_CONDUCT.md",
    "AUTHORS", "AUTHORS.md",
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements.in",
    "Pipfile", "Pipfile.lock",
    "poetry.lock",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".gitignore", ".gitattributes", ".editorconfig",
    ".env.example", ".env.template",
]

# Archivos de seguridad que NO deben estar en Git
SECURITY_FILES: Set[str] = {
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.staging", ".env.test",
    "secrets.json", "credentials.json", "service-account.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}

# ✅ v0.2.4: Archivos de seguridad cuyo contenido debe OCULTARSE en el reporte
# (se muestra existencia en estructura pero no líneas/valores)
# ✅ v0.3.1: .env* y push.py pasan a EXCLUDE_FILES (exclusión TOTAL, ni siquiera se listan).
REDACTED_CONTENT_FILES: Set[str] = {
    "secrets.json", "credentials.json", "service-account.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".pem", ".key",
    # ✅ v0.2.4: Archivos Python que pueden contener credenciales
    "commit.py",         # Puede contener credenciales
}

# ✅ v0.2.4: Descripciones de propósito para archivos redactados
# Permite mostrar qué hace el archivo sin exponer su contenido sensible
REDACTED_PURPOSE_MAP: Dict[str, str] = {
    "commit.py": "Automatización de commits locales (credenciales ocultas)",
    "secrets.json": "Credenciales y API keys (valores ocultos)",
    "credentials.json": "Credenciales de autenticación (valores ocultos)",
    "id_rsa": "Clave privada SSH (contenido oculto)",
    "id_ed25519": "Clave privada SSH Ed25519 (contenido oculto)",
}

# Directorios de CI/CD
CI_CD_DIRS: Set[str] = {".github", ".gitlab", ".circleci", ".travis"}
CI_CD_FILES: Set[str] = {
    ".gitlab-ci.yml", ".travis.yml", ".travis.yaml",
    "Jenkinsfile", "azure-pipelines.yml", "azure-pipelines.yaml",
    "bitbucket-pipelines.yml", "bitbucket-pipelines.yaml",
    ".buildkite/pipeline.yml",
}

# ═══════════════════════════════════════════════════════════════
# CLASES DE DATOS
# ═══════════════════════════════════════════════════════════════
@dataclass
class FileInfo:
    """Información detallada de un archivo."""
    name: str
    path: str
    exists: bool = True
    has_content: bool = False
    lines: int = 0
    chars: int = 0
    status: str = "⚪ DESCONOCIDO"
    note: str = ""
    hidden: bool = False
    category: str = "Otros"
    is_critical: bool = False
    is_security: bool = False
    is_redacted: bool = False  # ✅ v0.2.2: contenido oculto por seguridad
    is_test: bool = False
    is_cicd: bool = False
    in_data_dir: bool = False
    classes: List[str] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # No serializar contenido sensible aunque esté en memoria
        return d

@dataclass
class DirInfo:
    """Información detallada de un directorio."""
    name: str
    path: str
    hidden: bool = False
    is_git: bool = False
    is_data: bool = False
    is_cicd: bool = False
    is_tests: bool = False
    files: List["FileInfo"] = field(default_factory=list)
    subdirs: List["DirInfo"] = field(default_factory=list)
    total_files: int = 0
    total_dirs: int = 0
    complete: int = 0
    empty: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "hidden": self.hidden,
            "is_git": self.is_git,
            "is_data": self.is_data,
            "is_cicd": self.is_cicd,
            "is_tests": self.is_tests,
            "files": [f.to_dict() for f in self.files],
            "subdirs": [d.to_dict() for d in self.subdirs],
            "total_files": self.total_files,
            "total_dirs": self.total_dirs,
            "complete": self.complete,
            "empty": self.empty,
            "error": self.error,
        }

@dataclass
class GitInfo:
    """Información del repositorio Git."""
    exists: bool = False
    commits: int = 0
    branches: int = 0
    tags: int = 0
    current_branch: str = ""
    status: str = "No inicializado"
    is_clean: bool = True
    dirty_files: List[Tuple[str, str]] = field(default_factory=list)
    remote_url: str = ""
    # ✅ v0.3.2: commits recientes con sus archivos — para que los fixes ya
    # commiteados aparezcan en el reporte aunque el árbol esté limpio.
    recent_commits: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class ProjectStats:
    """Estadísticas globales del proyecto.
    ✅ v0.3.0: total_lines/total_chars = SOLO archivos de código (CODE_EXTENSIONS);
    all_lines/all_chars = todos los textos no binarios; python_files/binary_files añadidos."""
    total_files: int = 0
    total_dirs: int = 0
    code_files: int = 0
    python_files: int = 0
    binary_files: int = 0
    complete_files: int = 0
    empty_files: int = 0
    test_files: int = 0
    critical_files: int = 0
    security_files: int = 0
    redacted_files: int = 0  # ✅ v0.2.2
    cicd_files: int = 0
    total_classes: int = 0
    total_functions: int = 0
    total_lines: int = 0
    total_chars: int = 0
    all_lines: int = 0
    all_chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE UTILIDAD
# ═══════════════════════════════════════════════════════════════
def is_hidden_file(file_path: Path) -> bool:
    return file_path.name.startswith('.')

def is_git_directory(dir_path: Path) -> bool:
    return dir_path.name == '.git'

def is_data_directory(dir_path: Path) -> bool:
    """Verifica si el directorio ES un directorio de datos (por nombre)."""
    return dir_path.name.lower() in DATA_DIRS or dir_path.name == '.git'

def is_cicd_directory(dir_path: Path) -> bool:
    return dir_path.name in CI_CD_DIRS

def is_tests_directory(dir_path: Path) -> bool:
    return dir_path.name.lower() in {"test", "tests", "testing", "spec", "specs", "__tests__"}

def path_is_in_data_dir(file_path: Path, root_path: Path) -> bool:
    try:
        rel = file_path.relative_to(root_path)
        for part in rel.parts[:-1]:
            if part in DATA_DIRS or part == ".git":
                return True
    except ValueError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 structure_detector.py:336] excepción degradada (intencional): {_d2e}")
    return False

def categorize_file(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    name = file_path.name.lower()

    if ext in {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cpp", ".c", ".h", ".go", ".rs", ".rb", ".php", ".swift", ".kt"}:
        return "Código"
    elif ext in {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env", ".conf", ".properties"}:
        return "Configuración"
    elif ext in {".md", ".rst", ".txt", ".html", ".htm", ".pdf", ".adoc", ".asciidoc"}:
        return "Documentación"
    elif ext in {".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1"}:
        return "Scripts"
    elif ext in {".sql", ".db", ".sqlite", ".sqlite3"}:
        return "Base de Datos"
    elif ext in {".css", ".scss", ".sass", ".less", ".styl"}:
        return "Estilos"
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff"}:
        return "Imágenes"
    elif ext in {".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a"}:
        return "Audio"
    elif ext in {".log", ".out", ".err"}:
        return "Logs"
    elif name in {".gitignore", ".gitattributes", ".gitmodules", ".editorconfig", ".gitkeep"}:
        return "Configuración Git"
    elif name.startswith("."):
        return "Oculto"
    elif ext in {".lock", ".sum"}:
        return "Lock File"
    elif name in {"makefile", "dockerfile", "vagrantfile", "rakefile", "gemfile"}:
        return "Build"
    else:
        return "Otros"

def should_count_as_code(file_path: Path) -> bool:
    if file_path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return False
    for part in file_path.parts:
        if part in {"data", "logs", "docs", ".git", "venv", ".venv", "node_modules"}:
            return False
    return file_path.suffix.lower() in CODE_EXTENSIONS

def _looks_binary(file_path: Path) -> bool:
    """✅ v0.3.0: Detecta binarios por bytes nulos en los primeros 8KB
    (cubre .wav, .db, .coverage, etc. sin depender solo de la extensión)."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False

def is_binary_file(file_path: Path) -> bool:
    if file_path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    return _looks_binary(file_path)

def is_critical_file(file_path: Path) -> bool:
    return file_path.name in CRITICAL_FILES or file_path.name.lower() in {f.lower() for f in CRITICAL_FILES}

def is_security_file(file_path: Path) -> bool:
    name = file_path.name
    ext = file_path.suffix.lower()
    if name in SECURITY_FILES:
        return True
    if ext in {".pem", ".key"}:
        return True
    return False

def is_redacted_file(file_path: Path) -> bool:
    """✅ v0.2.4: Archivos cuyo contenido debe ocultarse en el reporte."""
    name = file_path.name
    ext = file_path.suffix.lower()
    if name in REDACTED_CONTENT_FILES:
        return True
    if ext in {".pem", ".key"}:
        return True
    return False

def is_test_file(file_path: Path) -> bool:
    name = file_path.name.lower()
    return (
        name.startswith("test_") or
        name.endswith("_test.py") or
        name.endswith("_test.js") or
        name.endswith("_test.ts") or
        name.endswith(".test.js") or
        name.endswith(".test.ts") or
        name.endswith(".spec.js") or
        name.endswith(".spec.ts") or
        name in {"conftest.py", "pytest.ini", "jest.config.js", "jest.config.ts"}
    )

def is_cicd_file(file_path: Path) -> bool:
    name = file_path.name
    if name in CI_CD_FILES:
        return True
    for part in file_path.parts:
        if part in CI_CD_DIRS:
            return True
    return False

def detect_project_root(start_path: Optional[Path] = None) -> Path:
    if start_path is None:
        start_path = Path.cwd()
    start_path = start_path.resolve()
    current = start_path
    markers = [
        ".git", "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "Cargo.toml", "go.mod", "Gemfile",
        "Makefile", "Dockerfile", "docker-compose.yml",
        "README.md", "README.rst", "README",
        ".project", ".workspace",
    ]
    while current != current.parent:
        for marker in markers:
            if (current / marker).exists():
                return current
        current = current.parent
    return start_path

def parse_git_status_porcelain(line: str) -> Tuple[str, str]:
    """✅ v0.3.0: Maneja renombrados/copiados ("R  old -> new") quedándose
    con el destino, y rutas entrecomilladas (git status --porcelain -z style)."""
    if len(line) < 4:
        return ("??", line.strip())
    status_code = line[:2]
    rest = line[3:]
    if rest.startswith('"') and rest.endswith('"'):
        filename = rest[1:-1]
        filename = filename.replace('\\"', '"').replace('\\\\', '\\')
    else:
        filename = rest
    if " -> " in filename:
        filename = filename.split(" -> ", 1)[1]
    return (status_code, filename)

# ✅ Parche A: función para redactar tokens en URLs de Git
def redact_git_remote(url: str) -> str:
    """Quita el token de URLs tipo https://user:token@host/repo.git"""
    if not url:
        return url
    # Patrón: https://USUARIO:TOKEN@HOST
    return re.sub(
        r'(https?://[^:]+:)[^@]+(@)',
        r'\1***REDACTED***\2',
        url
    )

def detect_declared_version(root_path: Path) -> str:
    """✅ v0.3.0: Lee la versión declarada del proyecto desde src/__init__.py
    (fallback: primer encabezado de versión en CHANGELOG.md).
    Devuelve "" si no se encuentra. El proyecto no tiene una versión
    canónica única, por eso se reporta la fuente."""
    init = root_path / "src" / "__init__.py"
    if init.exists():
        try:
            text = init.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
            if m:
                return m.group(1)
        except OSError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 structure_detector.py:500] excepción degradada (intencional): {_d2e}")
    changelog = root_path / "CHANGELOG.md"
    if changelog.exists():
        try:
            text = changelog.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'##\s*\[([^\]]+)\]', text)
            if m:
                return m.group(1)
        except OSError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 structure_detector.py:509] excepción degradada (intencional): {_d2e}")
    return ""

# ═══════════════════════════════════════════════════════════════
# ANÁLISIS DE ARCHIVOS
# ═══════════════════════════════════════════════════════════════
def analyze_python_file(file_path: Path) -> Tuple[List[str], List[str], str]:
    classes = []
    functions = []
    description = ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            description = docstring.split("\n")[0][:200]

        class ModuleVisitor(ast.NodeVisitor):
            def __init__(self):
                self.classes = []
                self.functions = []
                self.methods = 0

            def visit_ClassDef(self, node: ast.ClassDef):
                self.classes.append(node.name)
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.FunctionDef):
                # ✅ v0.3.0: cuenta TODAS las funciones, incluidas las
                # definidas dentro de clases (métodos) — refleja la realidad
                self.functions.append(node.name)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                self.functions.append(node.name)
                self.generic_visit(node)

        visitor = ModuleVisitor()
        visitor.visit(tree)
        classes = visitor.classes
        functions = visitor.functions
    except SyntaxError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 structure_detector.py:552] excepción degradada (intencional): {_d2e}")
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 structure_detector.py:554] excepción degradada (intencional): {_d2e}")
    return classes, functions, description

def check_file_content(file_path: Path) -> Tuple[bool, int, int, str, str, bool]:
    """
    Analiza el contenido de un archivo.
    ✅ v0.2.4: Retorna 6-tuple:
        (has_content, lines, chars, status, note, is_redacted)
    Para archivos de seguridad (.env, secrets, keys, push.py) retorna is_redacted=True.

    ✅ v0.2.4 CAMBIO: push.py y commit.py ahora muestran cantidad de líneas reales
    y una descripción de propósito, pero su contenido sensible permanece oculto.
    El conteo de líneas se hace SIN leer el contenido completo en memoria.
    """
    if not file_path.exists():
        return False, 0, 0, "❌ NO EXISTE", "Crear archivo", False

    # ✅ v0.3.0: Archivos binarios NO se leen como texto (antes inflaban
    # líneas/chars con basura). Se reporta tamaño real en bytes.
    if is_binary_file(file_path):
        try:
            size = file_path.stat().st_size
            return True, 0, size, "📦 BINARIO", f"{size:,} bytes", False
        except OSError:
            return True, 0, 0, "📦 BINARIO", "Archivo binario", False

    # ✅ v0.2.4: Archivos con contenido REDACTADO por seguridad
    if is_redacted_file(file_path):
        if file_path.is_file():
            try:
                size = file_path.stat().st_size
                if size > 0:
                    # ✅ v0.2.4: Contar líneas sin almacenar contenido sensible en memoria
                    # Para push.py y commit.py, mostrar líneas reales pero ocultar contenido
                    lines = 0
                    if file_path.name in REDACTED_PURPOSE_MAP:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            for _ in f:
                                lines += 1
                        purpose = REDACTED_PURPOSE_MAP.get(
                            file_path.name,
                            "Contenido sensible (archivo con credenciales)"
                        )
                        return True, lines, 0, "🔐 REDACTADO", purpose, True
                    else:
                        return True, 0, 0, "🔐 REDACTADO", "Contenido oculto (archivo sensible)", True
                else:
                    return False, 0, 0, "🔐 REDACTADO VACÍO", "Archivo sensible vacío", True
            except OSError:
                return True, 0, 0, "🔐 REDACTADO", "Contenido oculto (archivo sensible)", True
        return False, 0, 0, "🔐 REDACTADO", "Contenido oculto", True

    try:
        if file_path.is_file():
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            lines = len(content.splitlines())
            chars = len(content)

            if chars == 0:
                return False, 0, 0, "⚪ VACÍO", "Agregar contenido", False

            if file_path.suffix == ".py":
                stripped = content.strip()
                if stripped in _KNOWN_EMPTY_PY_HEADERS:
                    return False, lines, chars, "⚪ SOLO HEADER", "Agregar implementación", False

            if file_path.name in {".env.example", ".env.template"}:
                useful_lines = sum(
                    1 for line in content.splitlines()
                    if line.strip() and not line.strip().startswith('#')
                )
                if useful_lines >= 5:
                    return True, lines, chars, "✅ COMPLETO", f"{useful_lines} variables", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", "Agregar más variables", False

            if file_path.name == ".gitignore":
                useful_rules = sum(
                    1 for line in content.splitlines()
                    if line.strip() and not line.strip().startswith('#')
                )
                if useful_rules >= 10:
                    return True, lines, chars, "✅ COMPLETO", f"{useful_rules} reglas", False
                elif useful_rules >= 3:
                    return True, lines, chars, "✅ BÁSICO", f"{useful_rules} reglas", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", f"Solo {useful_rules} reglas", False

            if file_path.name in {"requirements.txt", "requirements.in", "requirements-dev.txt"}:
                useful_deps = sum(
                    1 for line in content.splitlines()
                    if line.strip() and not line.strip().startswith('#') and not line.strip().startswith('-')
                )
                if useful_deps >= 5:
                    return True, lines, chars, "✅ COMPLETO", f"{useful_deps} dependencias", False
                elif useful_deps >= 1:
                    return True, lines, chars, "✅ BÁSICO", f"{useful_deps} dependencias", False
                else:
                    return False, lines, chars, "⚪ VACÍO", "Agregar dependencias", False

            if file_path.name.lower().startswith("readme"):
                if lines >= 10:
                    return True, lines, chars, "✅ COMPLETO", f"{lines} líneas", False
                elif lines >= 3:
                    return True, lines, chars, "✅ BÁSICO", f"{lines} líneas", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", "Ampliar documentación", False

            if file_path.name.lower().startswith("license") or file_path.name.lower().startswith("licence"):
                if lines >= 10:
                    return True, lines, chars, "✅ COMPLETO", f"{lines} líneas", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", "Usar licencia completa", False

            if file_path.name == "Dockerfile":
                useful_lines = sum(
                    1 for line in content.splitlines()
                    if line.strip() and not line.strip().startswith('#')
                )
                if useful_lines >= 5:
                    return True, lines, chars, "✅ COMPLETO", f"{useful_lines} instrucciones", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", "Completar Dockerfile", False

            if file_path.name in {"docker-compose.yml", "docker-compose.yaml"}:
                if "services:" in content:
                    return True, lines, chars, "✅ COMPLETO", "Servicios definidos", False
                else:
                    return False, lines, chars, "⚪ INCOMPLETO", "Falta services:", False

            if file_path.name in {"pyproject.toml", "setup.py", "setup.cfg"}:
                if lines >= 5:
                    return True, lines, chars, "✅ COMPLETO", f"{lines} líneas", False
                else:
                    return False, lines, chars, "⚪ MÍNIMO", "Completar configuración", False

            return True, lines, chars, "✅ COMPLETO", "-", False

        return False, 0, 0, "📁 DIRECTORIO", "-", False

    except PermissionError:
        return False, 0, 0, "🔒 SIN PERMISO", "Verificar permisos", False
    except Exception as e:
        return False, 0, 0, "❌ ERROR", str(e)[:100], False

# ═══════════════════════════════════════════════════════════════
# CLASE PRINCIPAL: PROJECT DETECTOR
# ═══════════════════════════════════════════════════════════════
class ProjectDetector:
    """Detector de estructura y contenido del proyecto."""

    def __init__(self, root_path: Path, output_dir: Optional[Path] = None):
        self.root_path = root_path.resolve()
        self.output_dir = output_dir or (self.root_path / "docs")
        self.root_dir: Optional[DirInfo] = None
        self.stats = ProjectStats()
        self.git_info = GitInfo()
        self.file_cache: Dict[str, FileInfo] = {}

    def _should_exclude(self, path: Path) -> bool:
        """
        Verifica si una ruta debe ser excluida completamente del escaneo.
        ✅ v0.3.0 (corregido):
            1. Nombre exacto del archivo (EXCLUDE_FILES)
            2. Patrones glob simples en EXCLUDE_FILES (ej: "*.bundle")
            3. EXCLUDE_DIRS SOLO para directorios: si un directorio padre está
               en EXCLUDE_DIRS, o (si `path` es un directorio) su propio nombre
               está en EXCLUDE_DIRS → excluido. Antes se aplicaba también a
               archivos (p. ej. `.env`), impidiendo mostrarlos redactados.
            4. Todo el contenido bajo un directorio `.git` se excluye
               (internos: objects/refs/hooks/info/logs), pero `.git` mismo NO,
               para mostrarlo resumido como directorio de datos.
            5. Sufijos .egg-info
        """
        # 1. Excluir archivos específicos por nombre exacto
        if path.name in EXCLUDE_FILES:
            return True

        # 2. Excluir por patrones glob simples (ej: "*.bundle")
        for pattern in EXCLUDE_FILES:
            if pattern.startswith("*."):
                suffix = pattern[1:]  # "*.bundle" → ".bundle"
                if path.name.endswith(suffix):
                    return True

        parts = path.parts
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False

        # 3. Directorios padre en EXCLUDE_DIRS (aplica a archivos y dirs)
        for part in parts[:-1]:
            if part in EXCLUDE_DIRS:
                return True
            if part.endswith(".egg-info"):
                return True

        # 4. Si es directorio, su propio nombre en EXCLUDE_DIRS
        if is_dir:
            if parts[-1] in EXCLUDE_DIRS:
                return True
            if parts[-1].endswith(".egg-info"):
                return True

        # 5. Internos de .git: todo lo que cuelga de un padre ".git"
        if ".git" in parts[:-1]:
            return True

        return False

    def _scan_file(self, file_path: Path, relative_path: str, dir_info: DirInfo) -> FileInfo:
        # ✅ v0.2.4: check_file_content ahora retorna 6-tuple con is_redacted
        has_content, lines, chars, status, note, is_redacted = check_file_content(file_path)

        classes, functions, description = [], [], ""
        # ✅ v0.3.0: Se analizan TAMBIÉN los archivos redactados (push.py, commit.py):
        # la redacción oculta su contenido en el reporte, no el conteo de símbolos —
        # así las métricas de clases/funciones reflejan todo el código real.
        if file_path.suffix.lower() == ".py" and has_content:
            classes, functions, description = analyze_python_file(file_path)

        in_data_dir = path_is_in_data_dir(file_path, self.root_path)
        is_code = should_count_as_code(file_path)
        is_binary = is_binary_file(file_path)

        # ✅ v0.3.0: Métricas honestas:
        # - Líneas de Código (total_lines) = SOLO archivos de código, no binarios
        # - Líneas de Texto (all_lines) = todos los textos no binarios fuera de data dirs
        if is_binary:
            self.stats.binary_files += 1
        elif not in_data_dir:
            self.stats.all_lines += lines
            self.stats.all_chars += chars
            if is_code:
                self.stats.total_lines += lines
                self.stats.total_chars += chars

        if is_code:
            self.stats.code_files += 1
            self.stats.total_classes += len(classes)
            self.stats.total_functions += len(functions)
            if file_path.suffix.lower() == ".py":
                self.stats.python_files += 1

        if has_content:
            self.stats.complete_files += 1
        else:
            self.stats.empty_files += 1

        if is_test_file(file_path):
            self.stats.test_files += 1
        if is_critical_file(file_path):
            self.stats.critical_files += 1
        if is_security_file(file_path):
            self.stats.security_files += 1
        if is_redacted:
            self.stats.redacted_files += 1
        if is_cicd_file(file_path):
            self.stats.cicd_files += 1

        return FileInfo(
            name=file_path.name,
            path=relative_path,
            exists=True,
            has_content=has_content,
            lines=lines,
            chars=chars,
            status=status,
            note=note,
            hidden=is_hidden_file(file_path),
            category=categorize_file(file_path),
            is_critical=is_critical_file(file_path),
            is_security=is_security_file(file_path),
            is_redacted=is_redacted,
            is_test=is_test_file(file_path),
            is_cicd=is_cicd_file(file_path),
            in_data_dir=in_data_dir,
            classes=classes,
            functions=functions,
            description=description,
        )

    def _scan_directory(self, path: Path, relative_path: str = "", depth: int = 0,
                        parent_is_data: bool = False) -> DirInfo:
        """
        ✅ v0.2.2: Añade parent_is_data para propagar el estado de "directorio de datos"
        a todos los subdirectorios anidados.
        """
        self_is_data = is_data_directory(path) or parent_is_data

        dir_info = DirInfo(
            name=path.name if path != self.root_path else self.root_path.name,
            path=relative_path or ".",
            hidden=is_hidden_file(path),
            is_git=is_git_directory(path),
            is_data=self_is_data,
            is_cicd=is_cicd_directory(path),
            is_tests=is_tests_directory(path),
        )

        if not path.exists():
            dir_info.error = "Directorio no existe"
            return dir_info

        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.name.startswith('.'), x.name.lower()))

            for item in items:
                if self._should_exclude(item):
                    continue

                if item.is_file():
                    file_info = self._scan_file(item, str(item.relative_to(self.root_path)), dir_info)
                    dir_info.files.append(file_info)
                    dir_info.total_files += 1
                    self.file_cache[str(item.relative_to(self.root_path))] = file_info

                    if file_info.has_content:
                        dir_info.complete += 1
                    else:
                        dir_info.empty += 1

                elif item.is_dir():
                    subdir_info = self._scan_directory(
                        item,
                        str(item.relative_to(self.root_path)),
                        depth + 1,
                        parent_is_data=self_is_data
                    )
                    dir_info.subdirs.append(subdir_info)
                    # ✅ v0.3.0: total_dirs recursivo (antes solo contaba los
                    # subdirectorios DIRECTOS de cada nivel, subestimando el total)
                    dir_info.total_dirs += 1 + subdir_info.total_dirs
                    dir_info.total_files += subdir_info.total_files
                    dir_info.complete += subdir_info.complete
                    dir_info.empty += subdir_info.empty

        except PermissionError as e:
            dir_info.error = f"PermissionError: {e}"

        return dir_info

    def _get_git_info(self) -> GitInfo:
        info = GitInfo()
        git_dir = self.root_path / ".git"

        if not git_dir.exists():
            return info

        info.exists = True

        def run_git(*args) -> Tuple[str, int]:
            try:
                result = subprocess.run(
                    ["git"] + list(args),
                    cwd=self.root_path,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                return result.stdout, result.returncode
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return "", 1

        stdout, rc = run_git("rev-list", "--count", "HEAD")
        if rc == 0:
            stripped = stdout.strip()
            if stripped.isdigit():
                info.commits = int(stripped)

        stdout, rc = run_git("branch", "--list")
        if rc == 0:
            info.branches = len([b for b in stdout.splitlines() if b.strip()])

        stdout, rc = run_git("branch", "--show-current")
        if rc == 0 and stdout.strip():
            info.current_branch = stdout.strip()
        else:
            stdout, rc = run_git("rev-parse", "--short", "HEAD")
            if rc == 0 and stdout.strip():
                info.current_branch = f"HEAD@{stdout.strip()}"

        stdout, rc = run_git("tag", "--list")
        if rc == 0:
            info.tags = len([t for t in stdout.splitlines() if t.strip()])

        stdout, rc = run_git("status", "--porcelain")
        if rc == 0:
            dirty_lines = [line for line in stdout.splitlines() if line.strip()]
            info.is_clean = len(dirty_lines) == 0
            info.dirty_files = [
                parse_git_status_porcelain(line) for line in dirty_lines[:50]
            ]
        else:
            info.is_clean = True

        # ✅ v0.3.2: commits recientes + archivos tocados (git log --name-only).
        # `git status` solo muestra cambios SIN commitear; con el árbol limpio
        # el reporte no reflejaba los fixes recientes. Esta sección los trae.
        recent_commits: List[Dict[str, Any]] = []
        stdout, rc = run_git(
            "log", "-5", "--name-only",
            "--pretty=format:AXIOMA_COMMIT|%h|%s",
        )
        if rc == 0 and stdout.strip():
            current: Optional[Dict[str, Any]] = None
            for raw in stdout.splitlines():
                line = raw.rstrip()
                if line.startswith("AXIOMA_COMMIT|"):
                    _parts = line.split("|", 2)
                    current = {
                        "hash": _parts[1],
                        "subject": _parts[2] if len(_parts) > 2 else "",
                        "files": [],
                    }
                    recent_commits.append(current)
                elif current is not None and line:
                    current["files"].append(line)
        info.recent_commits = recent_commits

        stdout, rc = run_git("remote", "get-url", "origin")
        if rc == 0 and stdout.strip():
            info.remote_url = stdout.strip()
        else:
            stdout, rc = run_git("config", "--get", "remote.origin.url")
            if rc == 0 and stdout.strip():
                info.remote_url = stdout.strip()

        if info.is_clean:
            info.status = f"✅ Limpio ({info.current_branch})"
        else:
            info.status = f"⚠️ Sucio ({info.current_branch}) - {len(info.dirty_files)} cambios"

        return info

    def _is_excluded_path(self, rel_path: str) -> bool:
        """✅ v0.3.1: True si una ruta relativa (p. ej. de `git status`) debe
        quedar fuera del reporte por pertenecer a EXCLUDE_DIRS/EXCLUDE_FILES.
        Evita que rutas excluidas del escaneo (deepseek-harness/, .env, push.py,
        inforchat/, ...) reaparezcan vía la sección de cambios sin commit."""
        parts = rel_path.replace("\\", "/").split("/")
        if parts and parts[0] in EXCLUDE_DIRS:
            return True
        if any(p in EXCLUDE_DIRS for p in parts[:-1]):
            return True
        if parts and parts[-1] in EXCLUDE_FILES:
            return True
        return False

    def analyze(self) -> None:
        print(f"🔹 Escaneando proyecto: {self.root_path}")
        self.root_dir = self._scan_directory(self.root_path)
        print("🔹 Verificando repositorio Git...")
        self.git_info = self._get_git_info()
        self.stats.total_files = self.root_dir.total_files
        self.stats.total_dirs = self.root_dir.total_dirs
        print(f"✅ Análisis completado: {self.stats.code_files} archivos de código")

    def _generate_tree_view(self, dir_info: DirInfo, prefix: str = "", is_last: bool = True,
                            max_depth: int = 10, current_depth: int = 0,
                            show_data_dirs: bool = False) -> List[str]:
        lines = []
        if current_depth > max_depth:
            return lines

        connector = "└── " if is_last else "├── "

        if dir_info.is_git:
            icon = "📦"
        elif dir_info.is_data:
            icon = "💾"
        elif dir_info.is_cicd:
            icon = "⚙️"
        elif dir_info.is_tests:
            icon = "🧪"
        elif dir_info.hidden:
            icon = "🔒"
        else:
            icon = "📁"

        # ✅ v0.3.0: Los directorios de datos (.git/, logs/, docs/, assets/...)
        # se muestran RESUMIDOS salvo con --show-data (coherente con la
        # sección detallada y con la nota del encabezado del reporte).
        if dir_info.is_data and not show_data_dirs:
            lines.append(
                f"{prefix}{connector}{icon} **`{dir_info.name}/`** "
                f"({dir_info.total_files} archivos — datos, no escaneado)"
            )
            return lines

        lines.append(f"{prefix}{connector}{icon} **`{dir_info.name}/`** ({dir_info.total_files} archivos)")

        if current_depth < max_depth:
            new_prefix = prefix + ("    " if is_last else "│   ")

            for i, subdir in enumerate(dir_info.subdirs):
                is_last_subdir = (i == len(dir_info.subdirs) - 1) and len(dir_info.files) == 0
                lines.extend(self._generate_tree_view(
                    subdir, new_prefix, is_last_subdir, max_depth,
                    current_depth + 1, show_data_dirs
                ))

            files_to_show = dir_info.files
            for i, file_info in enumerate(files_to_show):
                is_last_file = (i == len(files_to_show) - 1)
                file_connector = "└── " if is_last_file else "├── "

                if file_info.is_redacted:
                    file_icon = "🔐"
                elif file_info.is_security:
                    file_icon = "🔐"
                elif file_info.is_critical:
                    file_icon = "⭐"
                elif file_info.is_test:
                    file_icon = "🧪"
                elif file_info.is_cicd:
                    file_icon = "⚙️"
                elif file_info.hidden:
                    file_icon = "🔒"
                elif is_binary_file(Path(self.root_path / file_info.path)):
                    file_icon = "📦"
                else:
                    file_icon = "📄"

                lines.append(f"{new_prefix}{file_connector}{file_icon} `{file_info.name}`")

        return lines

    def _render_directory(self, dir_info: DirInfo, level: int = 0, show_data_dirs: bool = False) -> List[str]:
        lines = []
        indent = "  " * level

        if dir_info.is_data and not show_data_dirs:
            if dir_info.is_git:
                prefix = "📦"
            elif dir_info.is_cicd:
                prefix = "⚙️"
            elif dir_info.is_tests:
                prefix = "🧪"
            else:
                prefix = "💾"

            lines.append(f"{indent}{prefix} **`{dir_info.name}/`** (datos - no cuenta en progreso)")
            lines.append(f"{indent}  **Archivos:** {dir_info.total_files} | **✅:** {dir_info.complete} | **⚪:** {dir_info.empty}")
            lines.append("")
            return lines

        if dir_info.is_git:
            prefix = "📦"
        elif dir_info.is_cicd:
            prefix = "⚙️"
        elif dir_info.is_tests:
            prefix = "🧪"
        elif dir_info.hidden:
            prefix = "🔒"
        else:
            prefix = "📁"

        lines.append(f"{indent}{prefix} **`{dir_info.name}/`**")
        lines.append(f"{indent}  **Archivos:** {dir_info.total_files} | **✅:** {dir_info.complete} | **⚪:** {dir_info.empty}")

        if dir_info.files:
            lines.append(f"{indent}  | Archivo | Estado | Líneas | Categoría | Notas |")
            lines.append(f"{indent}  |---------|--------|--------|-----------|-------|")

            for file_info in dir_info.files:
                badges = []
                if file_info.is_redacted:
                    badges.append("🔐")
                elif file_info.hidden:
                    badges.append("🔒")
                if file_info.is_critical:
                    badges.append("⭐")
                if file_info.is_security and not file_info.is_redacted:
                    badges.append("🔐")
                if file_info.is_test:
                    badges.append("🧪")
                if file_info.is_cicd:
                    badges.append("⚙️")

                badge_str = " ".join(badges) + " " if badges else ""

                # ✅ v0.2.4: Para archivos redactados como push.py, mostrar líneas reales
                # pero con indicador de que el contenido está oculto
                if file_info.is_redacted and file_info.lines > 0:
                    lines_display = f"{file_info.lines} 🔒"
                elif file_info.is_redacted or file_info.status == "📦 BINARIO":
                    lines_display = "-"
                else:
                    lines_display = str(file_info.lines)

                lines.append(
                    f"{indent}  | `{file_info.name}` {badge_str}| {file_info.status} | "
                    f"{lines_display} | {file_info.category} | {file_info.note} |"
                )

            lines.append("")

        if dir_info.subdirs:
            lines.append("")
            for subdir in dir_info.subdirs:
                lines.extend(self._render_directory(subdir, level + 1, show_data_dirs))

        return lines

    def generate_report_md(self, show_data_dirs: bool = False) -> str:
        r = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def add(text=""):
            r.append(text)

        add(f"# 📘 AXIOMA — ESTRUCTURA REAL COMPLETA DEL PROYECTO")
        add("")
        add(f"**Generado:** {timestamp}")
        add(f"**Proyecto:** `{self.root_path}`")
        declared = detect_declared_version(self.root_path)
        add(f"**Versión declarada del proyecto:** `{declared or 'no encontrada (src/__init__.py)'}` (fuente: `src/__init__.py`)")
        add(f"**Versión del detector:** {DETECTOR_VERSION} (herramienta `tools/structure_detector.py`, **no** es la versión del proyecto)")
        add(f"**Detector:** `tools/structure_detector.py`")
        add("")
        add(f"> ✅ **Detector v{DETECTOR_VERSION}**: reporte corregido — métricas honestas calculadas sobre el escaneo real.")
        add("> Excluye ruido: internos de `.git`, `venv/`, `node_modules/`, caches, `data/` (backups/BD), `deepseek-harness/` (checkout externo) e `inforchat/` (notas privadas).")
        add("> **EXCLUIDOS por seguridad (no se listan):** `.env*`, `push.py`, `tokens*`, `passwords.txt`, `credentials.txt`.")
        add("> `commit.py`, `secrets.*`, `*.key`, `*.pem` aparecen solo con contenido **REDACTADO** (existencia visible, contenido oculto).")
        add("> Directorios de datos (`.git/`, `logs/`, `docs/`, `assets/`) se muestran **resumidos** — usa `--show-data` para expandirlos.")
        add("> `Líneas de Código` = solo archivos de código; `Funciones` **incluye métodos**; binarios detectados por extensión y por bytes nulos.")
        add("> Todos los directorios y archivos de código están listados. `max_depth=10`.")
        add("> 📚 Función de cada archivo: [`docs/REFERENCIA_ARCHIVOS.md`](REFERENCIA_ARCHIVOS.md) · Arquitectura: [`docs/ARQUITECTURA.md`](ARQUITECTURA.md)")
        add("")
        add("---")
        add("")
        add("## 📊 RESUMEN EJECUTIVO")
        add("")
        add("### Métricas Globales")
        add("")
        add("> ℹ️ `Líneas de Código` cuenta solo archivos de código (`CODE_EXTENSIONS`, no binarios); `Líneas de Texto` cuenta todos los archivos de texto no binarios fuera de directorios de datos; clases/funciones se extraen con AST de los archivos Python.")
        add("")
        add("| Métrica | Valor |")
        add("|---------|-------|")
        add(f"| 📁 Total Directorios | {self.stats.total_dirs} |")
        add(f"| 📄 Total Archivos (escaneados) | {self.stats.total_files} |")
        add(f"| 🐍 Archivos de Código | {self.stats.code_files} |")
        add(f"| 🐍 Archivos Python | {self.stats.python_files} |")
        add(f"| 📦 Archivos Binarios | {self.stats.binary_files} |")
        add(f"| ✅ Con Contenido | {self.stats.complete_files} |")
        add(f"| ⚪ Vacíos/Placeholders | {self.stats.empty_files} |")
        add(f"| 🧪 Archivos de Test | {self.stats.test_files} |")
        add(f"| ⭐ Archivos Críticos | {self.stats.critical_files} |")
        add(f"| 🔐 Archivos de Seguridad | {self.stats.security_files} |")
        add(f"| 🔐 Archivos Redactados | {self.stats.redacted_files} |")
        add(f"| ⚙️ Archivos CI/CD | {self.stats.cicd_files} |")
        add(f"| 🐍 Clases Python (incl. anidadas) | {self.stats.total_classes} |")
        add(f"| 🐍 Funciones Python (incl. métodos) | {self.stats.total_functions} |")
        add(f"| 📝 Líneas de Código | {self.stats.total_lines:,} |")
        add(f"| 💾 Caracteres de Código | {self.stats.total_chars:,} |")
        add(f"| 📝 Líneas de Texto (todos) | {self.stats.all_lines:,} |")
        add("")
        add("### Archivos por Categoría")
        add("")
        cats: Dict[str, int] = {}
        for f in self.file_cache.values():
            cats[f.category] = cats.get(f.category, 0) + 1
        add("| Categoría | Archivos |")
        add("|-----------|----------|")
        for cat in sorted(cats, key=lambda c: -cats[c]):
            add(f"| {cat} | {cats[cat]} |")
        add("")
        add("### Estado del Repositorio Git")
        add("")
        # ✅ v0.3.1: Los cambios sin commit se filtran contra las exclusiones
        # (deepseek-harness/, inforchat/, .env, push.py... no deben aparecer)
        visible_dirty = [
            (sc, fn) for sc, fn in self.git_info.dirty_files
            if not self._is_excluded_path(fn)
        ]
        add("| Componente | Valor |")
        add("|------------|-------|")
        if self.git_info.exists:
            add(f"| Repositorio | ✅ Inicializado |")
            add(f"| Branch Actual | `{self.git_info.current_branch}` |")
            add(f"| Branches | {self.git_info.branches} |")
            add(f"| Tags | {self.git_info.tags} |")
            add(f"| Commits | {self.git_info.commits} |")
            add(f"| Estado | {self.git_info.status} |")
            if self.git_info.remote_url:
                # ✅ Parche A: redactar token en la URL remota
                add(f"| Remote | `{redact_git_remote(self.git_info.remote_url)}` |")
            if not self.git_info.is_clean and visible_dirty:
                add(f"| Archivos Sucios (visibles) | {len(visible_dirty)} |")
        else:
            add(f"| Repositorio | ❌ No inicializado |")
        add("")
        add("---")
        add("")
        add("## 🌳 ÁRBOL COMPLETO DEL PROYECTO (max_depth=10)")
        add("")
        add("```")
        if self.root_dir:
            add(f"{self.root_path.name}/")
            for i, subdir in enumerate(self.root_dir.subdirs):
                is_last = (i == len(self.root_dir.subdirs) - 1) and len(self.root_dir.files) == 0
                tree_lines = self._generate_tree_view(
                    subdir, "", is_last, max_depth=10, current_depth=1,
                    show_data_dirs=show_data_dirs
                )
                r.extend(tree_lines)

            for i, file_info in enumerate(self.root_dir.files):
                is_last = (i == len(self.root_dir.files) - 1)
                connector = "└── " if is_last else "├── "

                if file_info.is_redacted:
                    icon = "🔐"
                elif file_info.is_security:
                    icon = "🔐"
                elif file_info.is_critical:
                    icon = "⭐"
                elif file_info.hidden:
                    icon = "🔒"
                elif is_binary_file(Path(self.root_path / file_info.path)):
                    icon = "📦"
                else:
                    icon = "📄"

                add(f"{connector}{icon} {file_info.name}")
        add("```")
        add("")
        add("---")
        add("")
        add("## 📁 ESTRUCTURA DETALLADA POR DIRECTORIO")
        add("")
        if not show_data_dirs:
            add("> **Nota:** `data/` y `deepseek-harness/` están **excluidos** del escaneo; los directorios `logs/`, `docs/`, `.git/`, `assets/` (y sus subdirectorios) se muestran resumidos (usa `--show-data` para verlos completos).")
        add("")
        add("> **Leyenda:** 🔒 Oculto | ⭐ Crítico | 🔐 Seguridad/Redactado | 🧪 Test | ⚙️ CI/CD")
        add("")

        if self.root_dir and self.root_dir.subdirs:
            for dir_info in self.root_dir.subdirs:
                r.extend(self._render_directory(dir_info, level=0, show_data_dirs=show_data_dirs))

        add("")
        add("### 📄 Archivos en Raíz")
        add("")
        add("| Archivo | Estado | Líneas | Categoría | Notas |")
        add("|---------|--------|--------|-----------|-------|")

        if self.root_dir:
            for file_info in self.root_dir.files:
                badges = []
                if file_info.is_redacted:
                    badges.append("🔐")
                elif file_info.hidden:
                    badges.append("🔒")
                if file_info.is_critical:
                    badges.append("⭐")
                if file_info.is_security and not file_info.is_redacted:
                    badges.append("🔐")

                badge_str = " ".join(badges) + " " if badges else ""

                # ✅ v0.2.4: Mostrar líneas reales para archivos redactados
                if file_info.is_redacted and file_info.lines > 0:
                    lines_display = f"{file_info.lines} 🔒"
                elif file_info.is_redacted or file_info.status == "📦 BINARIO":
                    lines_display = "-"
                else:
                    lines_display = str(file_info.lines)

                add(f"| `{file_info.name}` {badge_str}| {file_info.status} | {lines_display} | {file_info.category} | {file_info.note} |")

        add("")

        security_files_list = [f for f in self.file_cache.values() if f.is_security]
        if security_files_list:
            add("---")
            add("")
            add("## 🔐 ARCHIVOS DE SEGURIDAD DETECTADOS")
            add("")
            add("| Archivo | En .gitignore | Estado |")
            add("|---------|---------------|--------|")
            for file_info in security_files_list:
                in_gitignore = False
                if self.git_info.exists:
                    try:
                        result = subprocess.run(
                            ["git", "check-ignore", "-q", file_info.path],
                            cwd=self.root_path,
                            capture_output=True,
                            timeout=5
                        )
                        in_gitignore = (result.returncode == 0)
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 structure_detector.py:1350] excepción degradada (intencional): {_d2e}")

                ignore_status = "✅ Sí" if in_gitignore else "❌ **NO - PELIGRO**"
                add(f"| `{file_info.path}` | {ignore_status} | {file_info.status} |")
            add("")

        if not self.git_info.is_clean and visible_dirty:
            add("---")
            add("")
            add("## ⚠️ ARCHIVOS CON CAMBIOS SIN COMMIT")
            add("")
            add("| Archivo | Estado |")
            add("|---------|--------|")
            for status_code, filename in visible_dirty[:50]:
                status_map = {
                    " M": "📝 Modificado (unstaged)",
                    "M ": "📝 Staged",
                    "MM": "📝 Staged + Modificado",
                    "A ": "➕ Agregado (staged)",
                    " A": "➕ Sin rastrear → agregado",
                    "D ": "🗑️ Eliminado (staged)",
                    " D": "🗑️ Eliminado (unstaged)",
                    "R ": "📦 Renombrado",
                    "C ": "📋 Copiado",
                    "U ": "⚠️ Sin merge",
                    "UU": "⚠️ Conflicto sin resolver",
                    "??": "❓ Sin rastrear",
                    "!!": "🚫 Ignorado",
                }
                display_status = status_map.get(status_code, status_code.strip() or "❓")
                safe_filename = filename.replace("|", "\\|")
                add(f"| `{safe_filename}` | {display_status} |")

            if len(visible_dirty) > 50:
                add(f"| ... | Y {len(visible_dirty) - 50} más |")
            add("")

        # ✅ v0.3.2: últimos cambios por commits — visibles aunque el árbol
        # esté limpio (los fixes ya commiteados aparecen acá).
        visible_commits = [
            (c, [f for f in c["files"] if not self._is_excluded_path(f)])
            for c in self.git_info.recent_commits
        ]
        visible_commits = [(c, f) for c, f in visible_commits if f]

        if visible_commits:
            add("---")
            add("")
            add("## 🕒 ÚLTIMOS CAMBIOS (commits recientes)")
            add("")
            for commit, files in visible_commits:
                add(f"- **`{commit['hash']}`** — {commit['subject']}")
                for f in files[:12]:
                    add(f"  - `{f.replace('|', '\\|')}`")
                if len(files) > 12:
                    add(f"  - … y {len(files) - 12} archivos más")
            add("")

        add("---")
        add("")
        add("## 🚀 PRÓXIMOS PASOS")
        add("")
        add("El detector muestra la realidad actual del proyecto. No hay estructura esperada hardcodeada.")
        add("")
        add("### Recomendaciones")
        add("")
        recommendations = []
        step = 1

        empty_non_redacted = self.stats.empty_files - sum(
            1 for f in self.file_cache.values() if f.is_redacted and not f.has_content
        )
        if empty_non_redacted > 0:
            recommendations.append(f"{step}. 📝 Revisar **{empty_non_redacted}** archivos vacíos/placeholders")
            step += 1

        critical_missing = []
        for cf in ["README.md", "LICENSE", ".gitignore"]:
            if not any(f.name == cf for f in self.file_cache.values()):
                critical_missing.append(cf)

        if critical_missing:
            missing_str = ", ".join(f"`{f}`" for f in critical_missing)
            recommendations.append(f"{step}. ⭐ Crear archivos críticos faltantes: {missing_str}")
            step += 1

        if self.stats.test_files == 0 and self.stats.code_files > 0:
            recommendations.append(f"{step}. 🧪 Agregar tests para el código existente")
            step += 1

        if not self.git_info.exists:
            recommendations.append(f"{step}. 📦 Inicializar repositorio Git: `git init`")
            step += 1
        elif not self.git_info.is_clean:
            recommendations.append(f"{step}. 💾 Commitear cambios pendientes ({len(visible_dirty)} archivos)")
            step += 1

        if self.stats.security_files > 0:
            sec_not_ignored = []
            for f in self.file_cache.values():
                if f.is_security:
                    try:
                        result = subprocess.run(
                            ["git", "check-ignore", "-q", f.path],
                            cwd=self.root_path,
                            capture_output=True,
                            timeout=5
                        )
                        if result.returncode != 0:
                            sec_not_ignored.append(f.name)
                    except Exception as _d2e:
                        logging.getLogger(__name__).debug(f"[D2 structure_detector.py:1461] excepción degradada (intencional): {_d2e}")

            if sec_not_ignored:
                sec_str = ", ".join(f"`{f}`" for f in sec_not_ignored)
                recommendations.append(f"{step}. 🔐 Agregar a .gitignore: {sec_str}")
                step += 1

        if not recommendations:
            recommendations.append("✅ ¡El proyecto está en buen estado! No hay acciones críticas pendientes.")

        for rec in recommendations:
            add(rec)

        add("")
        add("---")
        add("")
        add("## ⚠️ ALERTAS DE SEGURIDAD")
        add("")
        add("1. **API Keys:** Rotar todas las keys antes de usar en producción")
        add("2. **`.env`:** NUNCA versionar en Git (verificar `.gitignore`) — **excluido de este reporte**")
        add("3. **`push.py`:** excluido de este reporte y de Git — verificar `.gitignore` (v0.4.1 eliminó tokens hardcodeados, usa SSH)")
        add("4. **`.gitignore`:** Debe bloquear `.env`, `venv/`, `logs/`, `data/`, `*.pyc`, `push.py`")
        add("5. **Secret Key:** Generar con `python -c 'import secrets; print(secrets.token_hex(32))'`")
        add("6. **Permisos:** Verificar que scripts de activación son ejecutables")
        add("7. **Dependencias:** Mantener `requirements.txt` o `pyproject.toml` actualizado")
        add("")
        add("---")
        add("")
        add(f"*Reporte generado por `tools/structure_detector.py` v{DETECTOR_VERSION} — escaneo real del proyecto*")
        add(f"**Tiempo de generación:** {timestamp}")

        return "\n".join(r)

    def save_report(self, show_data_dirs: bool = False) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.output_dir / "STRUCTURE_REPORT.md"
        report_md = self.generate_report_md(show_data_dirs)

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(report_md)

        print(f"📄 [1/1] Reporte MD guardado: {md_path}")
        return md_path

# ═══════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    import argparse

    default_root = detect_project_root()

    parser = argparse.ArgumentParser(
        description=(
            f"Detector de Estructura AXIOMA — herramienta v{DETECTOR_VERSION} "
            "(la versión del proyecto se lee de src/__init__.py)"
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=default_root,
        help=f"Ruta raíz del proyecto (default: auto-detectado)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directorio de salida (default: docs/)"
    )
    parser.add_argument(
        "--show-data",
        action="store_true",
        help="Mostrar contenido completo de directorios de datos (data/, .git/, logs/, docs/)"
    )

    args = parser.parse_args()
    output_dir = args.output or (args.root / "docs")

    print("=" * 70)
    print(f"🔹 [INICIO] Detector de Estructura AXIOMA — herramienta v{DETECTOR_VERSION} | proyecto v{detect_declared_version(Path(args.root)) or '?'}")
    print(f"📍 Proyecto: {args.root}")
    print(f"📄 Salida: {output_dir}")
    print(f"🌳 max_depth: 10 (estructura completa)")
    print(f"🔇 Ruido filtrado: internos .git, venv, node_modules, caches")
    print(f"🚫 Excluido: data/, deepseek-harness/, inforchat/, .env*, push.py, tokens, *.bundle")
    print(f"🔐 Redactado: commit.py, secrets.*, keys (contenido oculto en reporte)")
    print(f"👁️  Mostrar datos: {'SÍ (completo)' if args.show_data else 'NO (resumido)'}")
    print("=" * 70)

    try:
        detector = ProjectDetector(args.root, output_dir)
        detector.analyze()

        show_data_dirs = args.show_data
        md_path = detector.save_report(show_data_dirs=show_data_dirs)

        print("")
        print("=" * 70)
        print("RESUMEN RÁPIDO:")
        print("=" * 70)
        print(f"| 📄 Total Archivos      | {detector.stats.total_files} |")
        print(f"| 🐍 Archivos de Código  | {detector.stats.code_files} |")
        print(f"| 🐍 Archivos Python     | {detector.stats.python_files} |")
        print(f"| 📦 Binarios            | {detector.stats.binary_files} |")
        print(f"| ✅ Con Contenido       | {detector.stats.complete_files} |")
        print(f"| ⚪ Vacíos              | {detector.stats.empty_files} |")
        print(f"| 🧪 Tests               | {detector.stats.test_files} |")
        print(f"| ⭐ Críticos            | {detector.stats.critical_files} |")
        print(f"| 🔐 Seguridad           | {detector.stats.security_files} |")
        print(f"| 🔐 Redactados          | {detector.stats.redacted_files} |")
        print(f"| ⚙️ CI/CD               | {detector.stats.cicd_files} |")
        print(f"| 🐍 Clases Python       | {detector.stats.total_classes} |")
        print(f"| 🐍 Funciones Python    | {detector.stats.total_functions} |")
        print(f"| 📝 Líneas de Código    | {detector.stats.total_lines:,} |")
        print(f"| 📦 Git Commits         | {detector.git_info.commits} |")
        print(f"| 🌿 Git Branches        | {detector.git_info.branches} |")
        print("=" * 70)
        print("")
        print("✅ 1 ARCHIVO GENERADO:")
        print(f"  1. {md_path}")
        print("")
        print("📋 Comandos útiles:")
        print(f"  cat {md_path} | head -200")
        print("")
        print("💡 Para ver TODO el contenido (incluyendo datos binarios):")
        print(f"  python tools/structure_detector.py --show-data")
        print("=" * 70)

        return 0

    except Exception as e:
        print(f"\n❌ [ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
