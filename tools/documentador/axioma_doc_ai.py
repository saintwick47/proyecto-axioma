#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
# axioma_doc_ai.py - Análisis con IA para el documentador AXIOMA
# Ruta: /ruta/a/axioma/tools/documentador/axioma_doc_ai.py
# Autor: SaintWick
# Versión: 4.0
#
# Cliente Ollama para enriquecer la documentación con análisis
# técnico de clases, funciones y módulos. Incluye caché de
# análisis, detección automática de modelos, timeout configurable
# y prompts de ingeniería estrictos (sin adjetivos de IA).
# ═══════════════════════════════════════════════════════════════
import ast
import re
import subprocess
import hashlib
import json
from typing import List, Dict, Any, Optional, Set, Tuple
from pathlib import Path
from datetime import datetime

# Importar desde extractors
from axioma_doc_extractors import EnrichedClassInfo, FunctionSignature, SignatureExtractor
from axioma_doc_base import PROJECT_ROOT, CACHE_DB_PATH

# ==================== CONFIGURACIÓN v4.0 ====================
AI_CACHE_FILE = PROJECT_ROOT / "data" / ".ai_analysis_cache.json"

# ✅ 2026-08-25: timeout con piso para hardware CPU. qwen2.5-coder:7b tarda
# ~30s por generación corta; los prompts de análisis (con código fuente)
# pueden requerir 60-180s+. Antes con 60s TODAS las clases daban
# "Timeout en Ollama para análisis (60s)" y la documentación IA salía vacía.
try:
    from config.settings import settings
    _cfg_timeout = getattr(settings, "ollama_timeout", 60)
except Exception:
    _cfg_timeout = 60
AI_ANALYSIS_TIMEOUT = max(_cfg_timeout, 300)  # segundos (piso 5 min por clase)
MAX_AI_ANALYSIS_PER_RUN = 20

# ==================== CLIENTE OLLAMA MEJORADO v4.0 ====================
class OllamaAnalyzer:
    """
    Cliente para análisis de código con Ollama v4.0
    Features:
    - Detección automática del mejor modelo disponible
    - Cache de análisis para evitar reprocesamiento
    - Timeout configurable por análisis
    - Fallback gracefully si Ollama no está disponible
    - v4.0: Prompts de ingeniería estrictos para documentación técnica
    """
    DEFAULT_MODEL = "qwen2.5-coder:7b"  # Real instalado; _detect_best_model() prefiere settings.llm_code_model
    TIMEOUT = AI_ANALYSIS_TIMEOUT

    def __init__(self, model: str = None):
        self.model = model or self._detect_best_model()
        self.enabled = self._check_ollama_available()
        self.analysis_cache: Dict[str, str] = {}
        self._load_cache()

    def _load_cache(self):
        """Carga caché de análisis IA previos"""
        if AI_CACHE_FILE.exists():
            try:
                with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.analysis_cache = data.get('analysis_cache', {})
            except (json.JSONDecodeError, IOError):
                self.analysis_cache = {}

    def _save_cache(self):
        """Guarda caché de análisis IA"""
        AI_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'analysis_cache': self.analysis_cache,
            'last_updated': datetime.now().isoformat(),
            'model_used': self.model
        }
        with open(AI_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def _compute_analysis_hash(self, code_snippet: str, analysis_type: str) -> str:
        """Computa hash para caché de análisis"""
        hasher = hashlib.sha256()
        hasher.update(f"{analysis_type}:{code_snippet[:2000]}".encode('utf-8'))
        return hasher.hexdigest()[:16]

    def _detect_best_model(self) -> str:
        """
        Detecta el modelo a usar para análisis IA.

        ✅ FIX: antes mantenía su propia lista de preferencia hardcodeada
        ("aletheia-3b", "qwen-coder", "qwen2.5:7b", "mannix/llama3.1...") —
        ninguno de esos nombres coincide con los modelos reales instalados
        hoy, así que siempre caía en `models[0]` (el primero que devuelve
        `ollama list`, esencialmente arbitrario según orden de instalación).
        Root cause confirmado de las descripciones mal etiquetadas en el
        reporte y de la lentitud/timeouts erráticos cuando ese "primero"
        resultaba ser un modelo pesado o thinking.
        Ahora usa el mismo modelo que el resto de AXIOMA usa para código
        (settings.llm_code_model) — un solo lugar para cambiar el modelo,
        no una lista aparte que se desactualiza cada vez que cambian.
        """
        try:
            from config.settings import settings
            code_model = settings.llm_code_model
        except Exception:
            code_model = None

        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")[1:]
                models = [line.split()[0] for line in lines if line.strip()]

                if code_model and code_model in models:
                    return code_model

                if models:
                    return models[0]
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_ai.py:126] excepción degradada (intencional): {_d2e}")

        return self.DEFAULT_MODEL

    def _check_ollama_available(self) -> bool:
        """Verifica si Ollama está disponible"""
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def analyze_function(self, func_name: str, code_snippet: str, file_path: str) -> str:
        """
        Analiza una función con Ollama y retorna un resumen técnico en español.
        v4.0: Prompt de ingeniería estricto. Sin adjetivos de IA.
        """
        if not self.enabled:
            return ""

        # Verificar caché
        cache_key = self._compute_analysis_hash(code_snippet, f"function:{func_name}")
        if cache_key in self.analysis_cache:
            return self.analysis_cache[cache_key]

        lines = [
            "Eres un arquitecto de software documentando un sistema crítico.",
            "Describe la mecánica de esta función Python en 1-2 oraciones concisas en español.",
            "",
            "REGLAS ESTRICTAS:",
            "1. PROHÍBIDO usar adjetivos vacíos: 'inteligente', 'avanzado', 'mágico', 'dinámico'.",
            "2. OBLIGATORIO usar sustantivos de ingeniería: 'validación', 'caché', 'fallback', 'parser', 'resolución', 'routing'.",
            "3. Enfócate en el MECANISMO: flujo de datos, contratos de entrada/salida, y mecanismos de control (timeouts, reintentos, validaciones).",
            "4. No digas 'Esta función...' - ve directo al grano.",
            "",
            f"Archivo: {file_path}",
            f"Función: {func_name}",
            "",
            "```python",
            code_snippet[:1500],
            "```",
            "",
            "Descripción técnica directa:"
        ]
        prompt = "\n".join(lines)

        result = self._query_ollama(prompt)

        # Guardar en caché si hay resultado
        if result:
            self.analysis_cache[cache_key] = result
            self._save_cache()

        return result

    def analyze_class(self, class_name: str, code_snippet: str, file_path: str) -> str:
        """
        Analiza una clase con Ollama y retorna un resumen técnico en español.
        v4.0: Prompt de ingeniería estricto. Sin adjetivos de IA.
        """
        if not self.enabled:
            return ""

        # Verificar caché
        cache_key = self._compute_analysis_hash(code_snippet, f"class:{class_name}")
        if cache_key in self.analysis_cache:
            return self.analysis_cache[cache_key]

        lines = [
            "Eres un arquitecto de software documentando un sistema crítico.",
            "Describe esta clase Python en máximo 3 oraciones en español.",
            "",
            "REGLAS ESTRICTAS:",
            "1. PROHÍBIDO usar adjetivos de IA: 'inteligente', 'avanzado', 'que aprende', 'autónomo' (salvo code_autonomous).",
            "2. OBLIGATORIO usar terminología arquitectónica: 'Sandbox estricto', 'Circuit Breaker', 'Caché L1/L2', 'Evaluator-Optimizer', 'Fallback en cascada', 'Inyección de dependencias'.",
            "3. Describe el MECANISMO interno, no el propósito abstracto. En lugar de 'Valida respuestas', escribe 'Ejecuta Forced Chain of Thought y auditoría CWE Top-25'.",
            "4. Estructura: (1) Mecanismo principal, (2) Dependencias técnicas clave, (3) Estrategia de resiliencia si aplica.",
            "",
            f"Archivo: {file_path}",
            f"Clase: {class_name}",
            "",
            "```python",
            code_snippet[:2000],
            "```",
            "",
            "Descripción técnica y arquitectónica:"
        ]
        prompt = "\n".join(lines)

        result = self._query_ollama(prompt)

        # Guardar en caché si hay resultado
        if result:
            self.analysis_cache[cache_key] = result
            self._save_cache()

        return result

    def analyze_module(self, module_name: str, docstring: str, classes: List[str], functions: List[str]) -> str:
        """
        Genera un análisis del módulo completo.
        v4.0: Contexto de documentación de ingeniería
        """
        if not self.enabled:
            return ""

        classes_str = ", ".join(classes) if classes else "Ninguna"
        functions_str = ", ".join(functions) if functions else "Ninguna"
        doc_str = docstring or "Sin docstring"

        content_parts = [
            f"Módulo: {module_name}",
            f"Docstring: {doc_str}",
            f"Clases: {classes_str}",
            f"Funciones: {functions_str}"
        ]
        content = "\n".join(content_parts)

        lines = [
            "Eres un arquitecto de software documentando un sistema crítico.",
            "Analiza este módulo Python de AXIOMA en 2-3 oraciones en español.",
            "",
            "REGLAS ESTRICTAS:",
            "1. PROHÍBIDO marketing de IA.",
            "2. Describe el rol arquitectónico: contratos de datos, mecanismos de resiliencia, flujo de datos.",
            "3. Sé técnico y directo al grano.",
            "",
            content,
            "",
            "Análisis técnico directo:"
        ]
        prompt = "\n".join(lines)

        return self._query_ollama(prompt)

    def _query_ollama(self, prompt: str) -> str:
        """Envía query a Ollama y retorna la respuesta"""
        try:
            result = subprocess.run(
                ["ollama", "run", self.model, prompt],
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT
            )
            if result.returncode == 0:
                response = result.stdout.strip()
                lines = [l for l in response.split("\n") if l.strip()]
                return " ".join(lines[:3])
            return ""
        except subprocess.TimeoutExpired:
            print(f"⏱️  Timeout en Ollama para análisis ({self.TIMEOUT}s)")
            return ""
        except FileNotFoundError:
            print("⚠️  Ollama no encontrado - análisis IA deshabilitado")
            return ""
        except Exception as e:
            print(f"⚠️  Error en Ollama: {e}")
            return ""

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del analizador"""
        return {
            'enabled': self.enabled,
            'model': self.model,
            'cache_size': len(self.analysis_cache),
            'timeout': self.TIMEOUT
        }

    def clear_cache(self):
        """Limpia caché de análisis IA"""
        self.analysis_cache = {}
        if AI_CACHE_FILE.exists():
            AI_CACHE_FILE.unlink()


# ==================== FUNCIONES DE ENRIQUECIMIENTO v4.0 ====================
def enrich_critical_classes_with_ai(
    ollama: OllamaAnalyzer,
    files_info: Dict[str, Any],
    enriched_classes: Dict[str, List[EnrichedClassInfo]],
    project_root: Path,
    max_to_analyze: int = MAX_AI_ANALYSIS_PER_RUN
) -> int:
    """
    Analiza con Ollama las clases de archivos críticos.
    v3.3: Prioriza archivos core, llm, memory, router, agents.
    Retorna el número de clases analizadas.
    """
    priority_paths = [
        "src/domain/", "src/llm/", "src/memory/",
        "src/router/", "src/agents/"
    ]
    analyzed = 0

    for filepath, file_info in files_info.items():
        if analyzed >= max_to_analyze:
            break

        is_priority = any(p in filepath for p in priority_paths)
        file_classes = getattr(file_info, 'classes', [])

        if not is_priority or not file_classes:
            continue

        try:
            full_path = project_root / filepath
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            tree = ast.parse(content)

            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue

                if analyzed >= max_to_analyze:
                    break

                lines = content.splitlines()
                start = node.lineno - 1
                end = node.end_lineno if hasattr(node, "end_lineno") else start + 60
                class_code = "\n".join(lines[start:min(end, start + 100)])

                print(f"   🤖 Analizando clase: {node.name} ({filepath})")
                ai_summary = ollama.analyze_class(node.name, class_code, filepath)

                if node.name in enriched_classes:
                    for ec in enriched_classes[node.name]:
                        if ec.file == filepath:
                            ec.ai_summary = ai_summary
                            analyzed += 1

                # Analizar métodos sin docstring
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name.startswith("_") or item.name == "__init__":
                            continue

                        existing_doc = ast.get_docstring(item)
                        if existing_doc:
                            continue

                        func_start = item.lineno - 1
                        func_end = item.end_lineno if hasattr(item, "end_lineno") else func_start + 30
                        func_code = "\n".join(lines[func_start:min(func_end, func_start + 40)])

                        method_summary = ollama.analyze_function(item.name, func_code, filepath)

                        if node.name in enriched_classes:
                            for ec in enriched_classes[node.name]:
                                if ec.file == filepath:
                                    for method in ec.methods:
                                        if method.name == item.name and method_summary:
                                            method.ai_summary = method_summary
        except Exception as e:
            print(f"⚠️  Error en análisis IA de {filepath}: {e}")

    return analyzed


def _analyze_yaml_config(yaml_file: Path) -> List[Dict]:
    """
    [FIX-4] Parsea archivos .yaml/.yml extrayendo pares clave-valor como ConfigParameter dicts.
    No requiere PyYAML: usa regex para maxima compatibilidad.
    """
    config_params = []
    try:
        with open(yaml_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            match = re.match(r"^[\s]*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+)$", line)
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip().split("#")[0].strip()
                if key and value:
                    try:
                        rel = str(yaml_file.relative_to(yaml_file.parent.parent))
                    except ValueError:
                        rel = str(yaml_file)
                    config_params.append({
                        "name": key,
                        "value": value,
                        "type_hint": "str",
                        "description": "",
                        "file": rel,
                    })
    except Exception as e:
        print(f"\u26a0\ufe0f  Error leyendo YAML {yaml_file}: {e}")
    return config_params

def analyze_configurations_real(settings_file: Path) -> List[Dict]:
    """
    Analiza configuraciones mostrando valores reales.
    Entiende os.getenv, Path, bool expressions.
    v3.4: [FIX-4] Soporte .yaml/.yml via _analyze_yaml_config
    """
    config_params = []

    if not settings_file.exists():
        return config_params

    # [FIX-4] Delegar a parser YAML si corresponde
    if settings_file.suffix in (".yaml", ".yml"):
        return _analyze_yaml_config(settings_file)

    try:
        with open(settings_file, "r") as f:
            content = f.read()

        tree = ast.parse(content)
        seen_vars: Set[str] = set()
        SKIP_VARS = {"logger", "env_path", "status", "handler"}

        def _expr_to_value(node: ast.expr) -> Tuple[str, str]:
            if isinstance(node, ast.Constant):
                return repr(node.value), type(node.value).__name__

            if isinstance(node, ast.Call):
                func = node.func

                if isinstance(func, ast.Attribute) and func.attr == "getenv":
                    args = node.args
                    var = args[0].value if args and isinstance(args[0], ast.Constant) else "?"
                    default_val = "None"
                    if len(args) > 1 and isinstance(args[1], ast.Constant):
                        default_val = repr(args[1].value)
                    return f"env:{var} (default={default_val})", "str"

                if isinstance(func, ast.Name) and func.id in ("int", "float", "str", "bool"):
                    inner = "<?>"
                    if node.args:
                        inner_val, _ = _expr_to_value(node.args[0])
                        inner = inner_val
                    return f"{func.id}({inner})", func.id

                if isinstance(func, ast.Name) and func.id == "Path":
                    return "Path(...)", "Path"

                func_name = "fn"
                if isinstance(func, ast.Name):
                    func_name = func.id
                elif isinstance(func, ast.Attribute):
                    func_name = func.attr
                return f"{func_name}(...)", "call"

            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left, _ = _expr_to_value(node.left)
                right, _ = _expr_to_value(node.right)
                return f"{left}/{right}", "Path"

            if isinstance(node, ast.Compare):
                lhs = node.left
                if (isinstance(lhs, ast.Call)
                    and isinstance(lhs.func, ast.Attribute)
                    and lhs.func.attr in ("lower", "upper", "strip")):
                    inner_call = lhs.func.value
                    if (isinstance(inner_call, ast.Call)
                        and isinstance(inner_call.func, ast.Attribute)
                        and inner_call.func.attr == "getenv"
                        and inner_call.args
                        and isinstance(inner_call.args[0], ast.Constant)):
                        env_var = inner_call.args[0].value
                        default = "true"
                        if len(inner_call.args) > 1 and isinstance(inner_call.args[1], ast.Constant):
                            default = inner_call.args[1].value
                        return f"env:{env_var} (default={repr(default)}) → bool", "bool"

                left_str, _ = _expr_to_value(lhs)
                return f"{left_str} == ...", "bool"

            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Call):
                    inner, t = _expr_to_value(node.value)
                    return f"{inner}.{node.attr}()", t
                if isinstance(node.value, ast.Name):
                    return f"{node.value.id}.{node.attr}", "attr"
                return node.attr, "attr"

            if isinstance(node, ast.List):
                n = len(node.elts)
                if n > 0 and isinstance(node.elts[0], ast.Constant):
                    sample = repr(node.elts[0].value)
                    return f"[{sample}, ...] ({n} items)", "list"
                return f"[{n} items]", "list"

            if isinstance(node, ast.Dict):
                return f"{{dict {len(node.keys)} claves}}", "dict"

            if isinstance(node, ast.Name):
                return node.id, "ref"

            return "<complejo>", "unknown"

        def _add_param(var_name: str, value_node: ast.expr, type_hint: str = ""):
            if var_name in seen_vars or var_name.startswith("_") or var_name in SKIP_VARS:
                return
            seen_vars.add(var_name)
            value, inferred_type = _expr_to_value(value_node)
            config_params.append({
                "name": var_name,
                "value": value,
                "type_hint": type_hint or inferred_type,
                "description": "",
                "file": "config/settings.py"
            })

        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        _add_param(target.id, node.value)

            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                type_str = ""
                if node.annotation:
                    type_str = SignatureExtractor._annotation_to_str(node.annotation)
                if node.value:
                    _add_param(node.target.id, node.value, type_str)

            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                _add_param(target.id, item.value)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        type_str = ""
                        if item.annotation:
                            type_str = SignatureExtractor._annotation_to_str(item.annotation)
                        if item.value:
                            _add_param(item.target.id, item.value, type_str)

    except Exception as e:
        print(f"⚠️  Error analizando configuraciones: {e}")

    return config_params


def detect_model_usage(filepath: str, content: str, ai_models: Dict) -> None:
    """
    Detecta uso de modelos IA en el archivo.
    v3.3: Actualizado para detectar qwen-coder (reemplaza deepseek)
    """
    for model_name in ai_models.keys():
        if model_name in content:
            if "files_used" not in ai_models[model_name]:
                ai_models[model_name]["files_used"] = []
            if filepath not in ai_models[model_name]["files_used"]:
                ai_models[model_name]["files_used"].append(filepath)


def get_ai_analysis_stats() -> Dict[str, Any]:
    """
    ✅ NUEVO v3.2: Obtiene estadísticas de análisis IA
    v4.0: Agregado soporte para versión
    """
    stats = {
        'cache_file': str(AI_CACHE_FILE),
        'cache_exists': AI_CACHE_FILE.exists(),
        'cache_size': 0,
        'last_updated': None,
        'version': '4.0'
    }

    if AI_CACHE_FILE.exists():
        try:
            with open(AI_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            stats['cache_size'] = len(data.get('analysis_cache', {}))
            stats['last_updated'] = data.get('last_updated')
        except (json.JSONDecodeError, IOError) as _d2e:
            logging.getLogger(__name__).debug(f"[D2 axioma_doc_ai.py:603] excepción degradada (intencional): {_d2e}")

    return stats


def clear_ai_cache() -> bool:
    """
    ✅ NUEVO v3.2: Limpia caché de análisis IA
    v3.3: Mejorado manejo de errores
    Returns: True si se limpió, False si hubo error
    """
    try:
        if AI_CACHE_FILE.exists():
            AI_CACHE_FILE.unlink()
        return True
    except Exception:
        return False


def analyze_critical_flows_with_ai(
    ollama: OllamaAnalyzer,
    flow_name: str,
    flow_description: str,
    steps: List[Dict[str, Any]]
) -> str:
    """
    ✅ NUEVO v4.0: Analiza flujos críticos con IA enfocado en resiliencia y arquitectura
    """
    if not ollama.enabled:
        return ""

    steps_str = "\n".join([
        f"  {s.get('step_number', i+1)}. {s.get('component', 'Unknown')} (Fallback: {s.get('fallback', 'N/A')}, Timeout: {s.get('timeout', 'N/A')})"
        for i, s in enumerate(steps)
    ])

    lines = [
        "Eres un arquitecto de software analizando un flujo crítico en un sistema distribuido.",
        "Analiza este flujo en máximo 3 oraciones en español.",
        "",
        "REGLAS ESTRICTAS:",
        "1. PROHÍBIDO lenguaje de marketing.",
        "2. Identifica la cadena de resiliencia: cómo se manejan los fallos (circuit breakers, fallbacks, cachés).",
        "3. Identifica riesgos de contención o cuellos de botella (ej. concurrencia, saturación de GPU).",
        "4. Sé técnico y directo.",
        "",
        f"Flujo: {flow_name}",
        f"Descripción: {flow_description}",
        "",
        "Pasos:",
        steps_str,
        "",
        "Análisis de resiliencia y arquitectura:"
    ]
    prompt = "\n".join(lines)

    return ollama._query_ollama(prompt)


def validate_documentation_consistency(
    ollama: OllamaAnalyzer,
    doc_content: str,
    code_snippet: str
) -> Tuple[bool, str]:
    """
    ✅ NUEVO v3.3: Valida consistencia entre documentación y código con IA
    Returns: (es_consistente, explicación)
    """
    if not ollama.enabled:
        return True, "IA no disponible - validación omitida"

    lines = [
        "Compara esta documentación con el código y determina si son consistentes.",
        "Responde SOLO con 'CONSISTENTE' o 'INCONSISTENTE' seguido de una breve explicación técnica.",
        "",
        "Documentación:",
        doc_content[:1000],
        "",
        "Código:",
        code_snippet[:1000],
        "",
        "Veredicto:"
    ]
    prompt = "\n".join(lines)

    response = ollama._query_ollama(prompt)

    is_consistent = "CONSISTENTE" in response.upper() and "INCONSISTENTE" not in response.upper().replace("CONSISTENTE", "")

    return is_consistent, response


# ==================== EJECUCIÓN ====================
if __name__ == "__main__":
    print("Este es un módulo de análisis IA. Ejecute axioma_doc_main.py")
    print(f"Versión: v4.0 con prompts de ingeniería estrictos y caché de análisis")
    print(f"AI_CACHE_FILE: {AI_CACHE_FILE}")
    print(f"MAX_AI_ANALYSIS_PER_RUN: {MAX_AI_ANALYSIS_PER_RUN}")
    print(f"AI_ANALYSIS_TIMEOUT: {AI_ANALYSIS_TIMEOUT}s")
    print()

    # Demo de estadísticas
    stats = get_ai_analysis_stats()
    print(f"📊 Estadísticas de caché IA:")
    print(f"   Archivo caché: {stats['cache_file']}")
    print(f"   Existe: {stats['cache_exists']}")
    print(f"   Tamaño: {stats['cache_size']} análisis cacheados")
    print(f"   Última actualización: {stats['last_updated']}")
    print(f"   Versión: {stats['version']}")
    print()

    # Demo de detección de modelo
    analyzer = OllamaAnalyzer()
    print(f"🤖 Estado de Ollama:")
    print(f"   Habilitado: {analyzer.enabled}")
    print(f"   Modelo: {analyzer.model}")
    print(f"   Timeout: {analyzer.TIMEOUT}s")
