#!/usr/bin/env python3
import logging
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_base_utils.py
# Autor: SaintWick
"""
axioma_doc_base_utils.py - Funciones utilitarias del Documentador AXIOMA v3.6
Extraído de axioma_doc_base.py para reducir longitud y mejorar mantenibilidad.
✅ Ruta: /ruta/a/axioma/tools/documentador/
✅ v3.6: AXIOMA v0.3.6 (Integración FlowMapper + Async + Validación Estricta)
✅ MEJORAS v3.6:
   - infer_task_type_info() completado para 40+ tipos (0 ejemplos vacíos)
   - Actualización de versiones y strings
✅ CORREGIDO v3.6:
   - split('\\n') corregido para evitar SyntaxError
   - Soporte categorías: Multimodal, Voz, Sandbox
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
from typing import Dict, List, Any, Set, Tuple, Optional
from datetime import datetime

# ==================== DETECCIÓN AUTOMÁTICA DE MODELOS OLLAMA ====================
def detect_ollama_models() -> Dict[str, Dict[str, Any]]:
    """Detecta automáticamente los modelos instalados en Ollama.

    ✅ FIX: antes adivinaba 'purpose' por substring del NOMBRE del modelo,
    encadenado if/elif ("code"/"coder" → código, "deepseek" → código,
    "qwen" → código...) — completamente desconectado de config.settings.
    Como "qwen" matcheaba después de "code"/"coder" pero antes de nada más,
    CUALQUIER modelo qwen* sin "code" en el nombre caía en "especializado
    en código" (qwen3:8b usado para chat, qwen3-vl:4b usado para visión,
    ambos mal etiquetados) — y CUALQUIER deepseek* se etiquetaba código sin
    importar su uso real. Ahora compara contra los roles REALES configurados
    en AXIOMA — un modelo se etiqueta por lo que hace en el sistema, no por
    lo que sugiere su nombre.
    """
    models = {}
    role_by_model: Dict[str, List[str]] = {}
    try:
        from config.settings import settings
    except ImportError:
        # ✅ FIX: el import silencioso fallaba en la corrida real — los 3
        # modelos configurados (qwen3:8b/qwen2.5-coder:7b/qwen3-vl:4b) caían
        # al label genérico "sin rol configurado", confirmado en un run real.
        # tools/documentador/ puede no tener el project root en sys.path
        # según cómo se invoque axioma_doc_main.py — lo agrega acá explícito
        # (3 niveles arriba de este archivo: tools/documentador/ → tools/ →
        # raíz del proyecto) antes de reintentar.
        try:
            import sys as _sys
            _project_root = str(Path(__file__).resolve().parent.parent.parent)
            if _project_root not in _sys.path:
                _sys.path.insert(0, _project_root)
            from config.settings import settings
        except Exception as e:
            print(f"⚠️ WARNING: no se pudo importar config.settings ({e}) — "
                  f"Propósito de modelos quedará genérico")
            settings = None
    except Exception as e:
        print(f"⚠️ WARNING: error inesperado importando config.settings ({e})")
        settings = None

    if settings is not None:
        for model_name, label in [
            (settings.llm_default_model, "Chat / modelo por defecto (llm_default_model)"),
            (settings.llm_code_model, "Generación de código (llm_code_model)"),
            (settings.llm_vision_model, "Visión / multimodal (llm_vision_model)"),
            (settings.llm_fallback_1, "Fallback / juez (llm_fallback_1)"),
            (settings.llm_embed_model, "Embeddings para búsqueda semántica (llm_embed_model)"),
        ]:
            if model_name:
                role_by_model.setdefault(model_name, []).append(label)

    try:
        result = subprocess.run(
            ['ollama', 'list'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # ✅ CORREGIDO: splitlines() para evitar errores de sintaxis con \n
            lines = result.stdout.strip().splitlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    name = parts[0]
                    model_id = parts[1] if len(parts) > 1 else "N/A"
                    size = parts[2] if len(parts) > 2 else "N/A"
                    purpose = " + ".join(role_by_model.get(name, [])) or \
                        "Detectado en Ollama, sin rol configurado en AXIOMA"
                    models[name] = {
                        'model_id': model_id,
                        'size': size,
                        'purpose': purpose,
                        'provider': 'Ollama',
                        'files_used': []
                    }
    except FileNotFoundError:
        print("⚠️ WARNING: Ollama no encontrado")
    except subprocess.TimeoutExpired:
        print("⚠️ WARNING: Timeout al ejecutar 'ollama list'")
    except Exception as e:
        print(f"⚠️ WARNING: Error detectando modelos: {e}")
    return models

# ==================== UTILIDADES DE HASH v3.2 ====================
def compute_file_hash(file_path: Path) -> str:
    """Computa hash SHA-256 de un archivo para caché."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (IOError, OSError):
        return ""

def compute_signature_hash(classes: List[Dict[str, Any]]) -> str:
    """Computa hash de signatures de clases para validación de consistencia."""
    hasher = hashlib.sha256()
    for cls in sorted(classes, key=lambda x: x.get('name', '')):
        sig_data = {
            'name': cls.get('name'),
            'methods': [
                {
                    'name': m.get('name'),
                    'signature': m.get('signature'),
                    'params': m.get('params', [])
                }
                for m in cls.get('methods', [])
            ]
        }
        hasher.update(json.dumps(sig_data, sort_keys=True).encode('utf-8'))
    return hasher.hexdigest()

# ==================== UTILIDADES ====================
def get_directory_size(path: Path) -> str:
    """Calcula el tamaño total de un directorio"""
    try:
        total = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
        return format_size(total)
    except Exception:
        return "N/A"

def get_file_size(path: Path) -> str:
    """Obtiene el tamaño de un archivo"""
    try:
        return format_size(path.stat().st_size)
    except Exception:
        return "N/A"

def format_size(bytes_size: int) -> str:
    """Formatea bytes a unidades legibles"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

def categorize_file(path: str) -> str:
    """
    Categoriza un archivo según su ruta en AXIOMA.
    ✅ v3.6: Soporte categorías: Multimodal, Voz, Sandbox
    """
    if 'tests/' in path or 'test_' in path:
        return 'Tests'
    elif 'src/multimodal/' in path:
        return 'Multimodal'
    elif 'src/domain/' in path:
        return 'Domain'
    elif 'src/llm/' in path:
        return 'LLM Client'
    elif 'src/memory/' in path:
        return 'Memory System'
    elif 'src/router/' in path:
        return 'Router'
    elif 'src/agents/' in path:
        return 'Agents'
    elif 'src/interfaces/' in path:
        return 'Interfaces'
    elif 'src/utils/' in path:
        return 'Utilities'
    elif 'config/' in path:
        return 'Configuration'
    elif 'tools/' in path:
        return 'Tools'
    elif 'data/' in path:
        return 'Data/Storage'
    else:
        return 'Root/Scripts'

def infer_purpose(path: str, classes: List[str], functions: List[str]) -> str:
    """
    Infiere el propósito de un archivo por su nombre.
    ✅ v3.6: Agregados 'multimodal', 'voice', 'sandbox' al mapa
    """
    filename = Path(path).stem.lower()
    purpose_map = {
        'agent': 'Orquestación principal del agente IA',
        'orchestrator': 'Coordinación de modelos IA',
        'router': 'Enrutamiento inteligente de tareas',
        'memory': 'Gestión de memoria persistente',
        'handler': 'Manejo de modelos específicos',
        'executor': 'Ejecución de acciones',
        'classifier': 'Clasificación de intenciones',
        'evaluator': 'Evaluación de calidad',
        'validator': 'Validación de datos',
        'storage': 'Almacenamiento persistente',
        'gui': 'Interfaz gráfica',
        'types': 'Definiciones de tipos y enums',
        'test': 'Tests unitarios o de integración',
        'test_suite': 'Test Suite Unificado de AXIOMA',
        'benchmark': 'Benchmarks de rendimiento',
        'gateway': 'Punto de entrada unificado',
        'client': 'Cliente de comunicación externa',
        'config': 'Configuración del sistema',
        'settings': 'Configuración global',
        'paths': 'Gestión de rutas absolutas',
        'server': 'Servidor web FastAPI',
        'routes': 'Endpoints de la API',
        'health_checker': 'Verificación de estado del sistema',
        'model_tester': 'Tests de latencia de modelos',
        'ui': 'Interfaz de usuario consolidada (Colors + CLI)',
        '__init__': 'Exports públicos del módulo',
        'ambiguity': 'Detección y manejo de ambigüedad en queries',
        'feedback': 'Sistema de feedback y logging de usuario',
        'clarification': 'Diálogo de clarificación para queries ambiguos',
        'dispatcher': 'Punto de entrada principal del sistema',
        'data_handler': 'Manejo de datos deterministas (clima, finanzas, hora)',
        'utils': 'Utilidades y helpers del sistema',
        'geolocator': 'Geolocalización y coordenadas',
        # v3.6 Nuevos
        'multimodal': 'Pipeline de procesamiento de voz y visión',
        'voice': 'Motor de texto a voz (TTS)',
        'stt': 'Motor de transcripción (STT)',
        'sandbox': 'Entorno de ejecución aislado',
    }
    for key, purpose in purpose_map.items():
        if key in filename:
            return purpose
    return f"Componente ({len(classes)} clases, {len(functions)} funciones)"

def infer_purpose_enhanced(path: str, content: str, docstring: str,
                           classes: List[str], functions: List[str]) -> str:
    """Infiere propósito MEJORADO desde contenido real del archivo"""
    if docstring:
        # ✅ CORREGIDO: splitlines() seguro
        lines = [l.strip() for l in docstring.splitlines() if l.strip()]
        if lines:
            first_line = lines[0]
            if len(first_line) > 15 and not first_line.startswith('==='):
                return first_line
    return infer_purpose(path, classes, functions)

def detect_recent_changes(content: str, last_modified: datetime) -> str:
    """Detecta cambios recientes en el archivo"""
    now = datetime.now()
    days_ago = (now - last_modified).days
    if days_ago <= 7:
        version_match = re.search(r'v\d+\.\d+', content)
        if version_match:
            return f"Modificado hace {days_ago} días - {version_match.group()}"
        return f"Modificado hace {days_ago} días"
    elif days_ago <= 30:
        return f"Modificado hace {days_ago} días"
    return ""

def extract_module_docstring(tree: ast.AST) -> str:
    """Extrae el docstring del módulo"""
    if isinstance(tree, ast.Module) and tree.body:
        first_node = tree.body[0]
        if isinstance(first_node, ast.Expr) and isinstance(first_node.value, ast.Constant):
            if isinstance(first_node.value.value, str):
                return first_node.value.value.strip()[:200]
    return ""

def infer_task_type_info(task_name: str) -> Tuple[str, str]:
    """
    ✅ v3.6: Completado al 100% de los TaskTypes soportados.
    Infiere información sobre un TaskType con ejemplos reales de AXIOMA.
    """
    task_info = {
        # --- Code Tasks ---
        'CODE_GENERATION': ('Generación de nuevo código', '"Genera una función factorial en Python"'),
        'CODE_CORRECTION': ('Corrección de errores en código', '"Corrige este código: prnt(\'Hola\')"'),
        'CODE_EXPLANATION': ('Explicación de código existente', '"Explica qué hace esta función de sorts"'),
        'CODE_REVIEW': ('Revisión de calidad y buenas prácticas', '"Revisa este PR y sugiere mejoras de legibilidad"'),
        'CODE_OPTIMIZATION': ('Mejora de rendimiento y complejidad', '"Optimiza este bucle para grandes volúmenes de datos"'),
        'CODE_TESTING': ('Generación de tests unitarios', '"Crea tests pytest para este módulo de autenticación"'),
        'CODE_DOCUMENTATION': ('Generación de docstrings y docs', '"Documenta esta clase con Google Style Docstrings"'),
        'CODE_MIGRATION': ('Migración entre versiones/librerías', '"Migra este código de Python 2 a 3 o de v1 a v2 de API"'),
        'CODE_DEPLOYMENT': ('Scripts de despliegue y CI/CD', '"Crea un Dockerfile para esta app FastAPI"'),
        'CODE_DEBUGGING': ('Depuración asistida por logs/errores', '"¿Por qué falla este traceback con KeyError?"'),

        # --- Analysis & Data ---
        'DATA_ANALYSIS': ('Análisis exploratorio de datos', '"Analiza este CSV y encuentra correlaciones"'),
        'DOCUMENT_ANALYSIS': ('Análisis de documentos', '"Resume este PDF y extrae los puntos clave"'),
        'SENTIMENT_ANALYSIS': ('Análisis de sentimiento en texto', '"¿Cuál es el tono de estos comentarios de usuarios?"'),
        'TREND_ANALYSIS': ('Detección de tendencias', '"¿Qué patrones se repiten en estas ventas mensuales?"'),
        'RISK_ANALYSIS': ('Evaluación de riesgos', '"Evalúa los riesgos de seguridad de este script"'),

        # --- Search & Research ---
        'WEB_SEARCH': ('Búsqueda de información en la web', '"Busca información sobre el cambio climático"'),
        'NEWS_SEARCH': ('Búsqueda de noticias recientes', '"Últimas noticias sobre tecnología hoy"'),
        'ACADEMIC_SEARCH': ('Búsqueda de papers y estudios', '"Busca papers recientes sobre transformers"'),
        'CODE_SEARCH': ('Búsqueda de snippets o librerías', '"¿Cómo usar requests con proxy en Python?"'),

        # --- Conversational ---
        'GENERAL_CHAT': ('Diálogo general y saludos', '"Hola, ¿cómo estás?"'),
        'TECHNICAL_SUPPORT': ('Soporte técnico asistido', '"¿Cómo configuro un entorno virtual?"'),
        'TUTORING': ('Tutoría y enseñanza paso a paso', '"Enséñame cómo funcionan los decoradores"'),
        'BRAINSTORMING': ('Lluvia de ideas creativa', '"Dame 5 ideas para nombres de proyecto"'),
        'ROLEPLAY': ('Simulación de roles', '"Actúa como un experto en ciberseguridad"'),

        # --- Real-time/Tools ---
        'WEATHER_QUERY': ('Consultas sobre el clima', '"¿Qué clima hace en Buenos Aires?"'),
        'TIME_QUERY': ('Consultas sobre la hora', '"¿Qué hora es en Londres?"'),
        'FINANCE_QUERY': ('Consultas financieras', '"Cotización del dólar blue hoy"'),
        'NEWS_QUERY': ('Consultas sobre noticias', '"¿Qué pasó en el mercado hoy?"'),

        # --- Content ---
        'TRANSLATION': ('Traducción entre idiomas', '"Traduce esto al inglés técnico"'),
        'SUMMARIZATION': ('Resumen de texto', '"Resume este artículo largo"'),
        'FACT_CHECKING': ('Verificación de hechos', '"¿Es cierta esta afirmación histórica?"'),
        'CONTENT_CREATION': ('Creación de contenido creativo', '"Escribe un poema sobre el mar"'),
        'QUALITY_VALIDATION': ('Validación de calidad con CoT', '"Valida si esta respuesta es segura"'),
        'SECURITY_AUDIT': ('Auditoría de seguridad', '"Encuentra vulnerabilidades en este código"'),
        'SCRIPT_WRITING': ('Redacción de scripts', '"Escribe un script para limpiar logs antiguos"'),
        'POETRY_GENERATION': ('Generación de poesía', '"Escribe un haiku sobre código"'),

        # --- System Control (Multimodal/Tools) ---
        'SYSTEM_CONTROL': ('Control del sistema operativo', '"Cierra esa ventana"'),
        'APP_LAUNCH': ('Ejecución de aplicaciones', '"Abre el navegador Firefox"'),
        'VOLUME_CONTROL': ('Ajuste de volumen', '"Sube el volumen al 80%"'),
        'SCREENSHOT': ('Captura de pantalla', '"Toma una captura y dime qué ves"'),
    }

    return task_info.get(task_name, ('Tipo de tarea genérico', f'"Ejecutar tarea de tipo {task_name}"'))

# ==================== FUNCIONES DE UTILIDAD ADICIONALES ====================
def sanitize_filename(filename: str) -> str:
    """Sanitiza un nombre de archivo para uso seguro"""
    return re.sub(r'[^\w\-_.]', '_', filename)

def truncate_string(s: str, max_length: int = 100, suffix: str = "...") -> str:
    """Trunca una cadena a una longitud máxima"""
    if len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix

def count_code_lines(content: str) -> int:
    """Cuenta líneas de código reales (excluye comentarios y vacías)"""
    # ✅ CORREGIDO: splitlines() seguro
    lines = content.splitlines()
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            count += 1
    return count

def get_python_version() -> str:
    """Obtiene la versión de Python actual"""
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

def check_file_exists(filepath: Path) -> bool:
    """Verifica si un archivo existe y es legible"""
    try:
        return filepath.exists() and filepath.is_file() and os.access(filepath, os.R_OK)
    except Exception:
        return False

def get_relative_path(filepath: Path, base: Path) -> str:
    """Obtiene la ruta relativa de un archivo respecto a una base"""
    try:
        return str(filepath.relative_to(base))
    except ValueError:
        return str(filepath)

def is_python_file(filepath: Path) -> bool:
    """Verifica si un archivo es un archivo Python válido"""
    return filepath.suffix == '.py' and not filepath.name.startswith('.')

def get_file_modification_time(filepath: Path) -> Optional[datetime]:
    """Obtiene la fecha de modificación de un archivo"""
    try:
        return datetime.fromtimestamp(filepath.stat().st_mtime)
    except Exception:
        return None

def format_datetime(dt: datetime, format_str: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Formatea un datetime según un formato específico"""
    return dt.strftime(format_str)

def is_test_file(filepath: Path) -> bool:
    """Verifica si un archivo es un archivo de pruebas"""
    name = filepath.name.lower()
    return name.startswith('test_') or name.endswith('_test.py') or 'tests' in str(filepath)

def is_init_file(filepath: Path) -> bool:
    """Verifica si es un archivo __init__.py"""
    return filepath.name == '__init__.py'

def get_imports_from_file(filepath: Path) -> List[str]:
    """Extrae todos los imports de un archivo Python"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend([alias.name for alias in node.names])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        return list(set(imports))
    except Exception:
        return []

def estimate_cyclomatic_complexity(tree: ast.AST) -> int:
    """Estima la complejidad ciclomática de un módulo"""
    complexity = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
            complexity += 1
        elif isinstance(node, ast.FunctionDef):
            complexity += 1
    return complexity

def verify_database_access(project_root: Path) -> Tuple[bool, str]:
    """Verifica acceso a la base de datos antes de intentar conectar"""
    db_path = project_root / "data/axioma.db"
    db_dir = db_path.parent
    if not db_dir.exists():
        return (False, f"Directorio {db_dir} no existe - crear con: mkdir -p {db_dir}")
    if not os.access(db_dir, os.R_OK):
        return (False, f"Sin permisos de lectura en {db_dir}")
    if not os.access(db_dir, os.X_OK):
        return (False, f"Sin permisos de ejecución en {db_dir}")
    if not db_path.exists():
        return (False, f"Archivo {db_path} no existe - se omitirá análisis de BD")
    if not os.access(db_path, os.R_OK):
        return (False, f"Sin permisos de lectura en {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.close()
        return (True, "Acceso OK")
    except sqlite3.OperationalError as e:
        return (False, f"Archivo bloqueado o corrupto: {e}")
    except Exception as e:
        return (False, f"Error desconocido: {e}")

def ensure_data_directory(project_root: Path) -> bool:
    """Asegura que el directorio data/ exista"""
    db_dir = project_root / "data"
    try:
        if not db_dir.exists():
            db_dir.mkdir(parents=True, exist_ok=True)
            print(f"ℹ️  Directorio {db_dir} creado")
        return True
    except Exception as e:
        print(f"⚠️  Error creando directorio {db_dir}: {e}")
        return False

def is_file_excluded(file_path: Path) -> bool:
    """
    Verifica si un archivo debe excluirse del análisis de código.
    ✅ VERIFICADO: 'utils' NO está en la lista de exclusiones
    ✅ src/utils/ui.py y src/utils/__init__.py SE ANALIZAN correctamente
    """
    path_str = str(file_path)
    # ✅ v1.0.9: Exclusión por SEGMENTO de ruta (no subcadena — 'data' como
    # subcadena excluiría src/handlers/data_handler.py).
    # ✅ v4.2.2 (2026-09-28): 'tests' y 'docs' también acá — segunda barrera
    # (la primera es EXCLUSIONES en axioma_doc_base.py, que poda los directorios).
    ALWAYS_EXCLUDE = {'__pycache__', 'venv', '.git', 'documentador',
                      'deepseek-harness', 'inforchat', 'data',
                      'tests', 'docs'}
    return any(part in ALWAYS_EXCLUDE for part in Path(path_str).parts)

def load_json_file(filepath: Path) -> Optional[Dict[str, Any]]:
    """Carga archivo JSON de forma segura"""
    try:
        if filepath.exists():
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as _d2e:
        logging.getLogger(__name__).debug(f"[D2 axioma_doc_base_utils.py:491] excepción degradada (intencional): {_d2e}")
    return None

def save_json_file(filepath: Path, data: Dict[str, Any]) -> bool:
    """Guarda archivo JSON de forma segura"""
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return True
    except (IOError, TypeError):
        return False

# ==================== EJECUCIÓN ====================
if __name__ == "__main__":
    print("Este es un módulo de utilidades. Importe desde axioma_doc_base.py")
    from axioma_doc_base import AXIOMA_PROJECT_VERSION, DOCUMENTADOR_VERSION
    print(f"Versión: documentador v{DOCUMENTADOR_VERSION} - AXIOMA v{AXIOMA_PROJECT_VERSION}")
    print(f"Funciones disponibles: {len([f for f in dir() if not f.startswith('_')])}")
    print()
    print("🎯 NOVEDADES v3.6:")
    print("   ✅ infer_task_type_info() -> 0% vacíos")
    print("   ✅ Soporte Categorías: Multimodal, Voz, Sandbox")
    print("   ✅ Strings compatibles con parser actual (splitlines seguro)")
