#!/usr/bin/env python3
import logging
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════
# verify_axioma.py - Verificador Integral del Sistema AXIOMA
# Ruta: /ruta/a/axioma/tools/verify_axioma.py
# Autor: SaintWick
# Versión: 3.2.1
#
# Realiza verificación estática y diagnóstico runtime del sistema
# AXIOMA. Detecta errores silenciosos en el pipeline de visión,
# valida payloads a Ollama, verifica configuraciones, backends de
# captura de pantalla y disponibilidad de modelos de visión.
# ═══════════════════════════════════════════════════════════════
import sys
import re
import os
import json
import time
import base64
import argparse
import tempfile
import subprocess
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from dataclasses import dataclass, field, asdict

# =============================================================================
# RUTAS DEL PROYECTO
# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR      = PROJECT_ROOT / "src"
CONFIG_DIR   = PROJECT_ROOT / "config"
TOOLS_DIR    = PROJECT_ROOT / "tools"
ENV_FILE     = PROJECT_ROOT / ".env"
MODELS_FILE  = CONFIG_DIR / "models.yaml"
LOGS_DIR     = PROJECT_ROOT / "logs" / "diagnostics"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# COLORES ANSI
# =============================================================================
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

C_GREEN  = lambda t: _c("32", t)
C_RED    = lambda t: _c("31", t)
C_YELLOW = lambda t: _c("33", t)
C_CYAN   = lambda t: _c("36", t)
C_BOLD   = lambda t: _c("1", t)
C_DIM    = lambda t: _c("2", t)
C_HEADER = lambda t: _c("95", t)


# =============================================================================
# HELPER: Filtrar comentarios — v3.1.2 MEJORADO
# =============================================================================
def _strip_comments(content: str, lang: str = "python") -> str:
    """
    Elimina líneas de comentarios de un archivo.
    Retorna solo el código ejecutable.

    v3.1.2: Mejorado para YAML — filtra líneas de texto descriptivo
    que no son claves YAML válidas (no contienen ':').
    """
    lines = content.split('\n')
    code_lines = []
    for line in lines:
        stripped = line.strip()

        if not stripped:
            code_lines.append(line)
            continue

        if stripped.startswith('#'):
            continue

        if lang == "yaml":
            is_yaml_valid = (
                ':' in stripped or
                stripped.startswith('-') or
                stripped.startswith('|') or
                stripped.startswith('>') or
                stripped.startswith('%')
            )
            if not is_yaml_valid:
                continue

        code_lines.append(line)

    return '\n'.join(code_lines)


def _check_yaml_for_moondream(yaml_content: str) -> bool:
    """
    v3.1.4: Verifica si 'moondream' aparece en CLAVES o VALORES funcionales del YAML.
    Ignora comentarios, texto descriptivo, y valores de claves de metadatos
    (description, notes, comment) que solo documentan la eliminación del modelo.
    """
    try:
        import yaml
        parsed = yaml.safe_load(yaml_content)
    except Exception:
        code_only = _strip_comments(yaml_content, lang="yaml")
        return "moondream" in code_only.lower()

    if parsed is None:
        return False

    _METADATA_KEYS = {"description", "comment", "notes", "info", "detail"}

    def _search_dict(obj: Any, parent_key: str = "") -> bool:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str) and "moondream" in key.lower():
                    return True
                if _search_dict(value, parent_key=str(key)):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if _search_dict(item, parent_key):
                    return True
        elif isinstance(obj, str) and "moondream" in obj.lower():
            if parent_key.lower() not in _METADATA_KEYS:
                return True
        return False

    return _search_dict(parsed)


# =============================================================================
# ESTRUCTURAS DE DATOS PARA RUNTIME
# =============================================================================
@dataclass
class ResolutionTest:
    resolution_label: str
    actual_width: int
    actual_height: int
    base64_length: int
    has_data_uri_prefix: bool
    payload_valid: bool
    response_status: str
    response_content: str
    response_tokens: int
    duration_ms: float
    error_detail: str = ""
    model_specific_fixes_applied: List[str] = field(default_factory=list)

@dataclass
class ModelRuntimeDiagnostic:
    model_name: str
    model_available: bool
    environment_checks: List[Dict[str, Any]] = field(default_factory=list)
    resolution_tests: List[ResolutionTest] = field(default_factory=list)
    summary: str = ""
    critical_issue: str = ""

@dataclass
class RuntimeReport:
    timestamp: str
    ollama_version: str
    ollama_host: str
    project_root: str
    python_version: str
    screen_resolution_detected: str
    models_tested: List[ModelRuntimeDiagnostic] = field(default_factory=list)
    global_recommendations: List[str] = field(default_factory=list)
    overall_status: str = ""


# =============================================================================
# CONFIGURACIÓN DE MODELOS DE VISION
# =============================================================================
VISION_MODEL_CONFIG = {
    "gemma4:e4b": {
        "max_dimension": 3840,
        "supports_system": True,
        "requires_num_gpu_zero": True,
        "timeout": 150,
        "description": "Modelo vision actual (único VLM instalado). Requiere --ubatch-size 2048. Workaround num_gpu:0.",
    },
}
_VISION_MODEL_PRIMARY = "gemma4:e4b"

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")


# =============================================================================
# CLASE PRINCIPAL: AxiomaVerifier
# =============================================================================
class AxiomaVerifier:
    """Verificador integral del sistema AXIOMA (estatico + runtime)."""

    CATEGORIES = [
        ("core_agents",    "Core Agents"),
        ("dispatcher",     "Dispatcher Flow"),
        ("screen_capture", "Screen Capture"),
        ("settings",       "Settings & Config"),
        ("vision_system",  "Vision System"),
        ("web_ui",         "Web UI"),
        ("logging",        "Logging"),
        ("task_types",     "Task Types"),
        ("runtime",        "Runtime & Connectivity"),
        ("vision_runtime", "Vision Runtime"),
    ]

    def __init__(
        self,
        verbose: bool = False,
        category_filter: Optional[str] = None,
        model_filter: Optional[str] = None,
    ):
        self.verbose = verbose
        self.category_filter = category_filter
        self.model_filter = model_filter
        self.results: Dict[str, List[Dict]] = {cat: [] for cat, _ in self.CATEGORIES}
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
        self.runtime_report: Optional[RuntimeReport] = None

    def add(
        self,
        category: str,
        test: str,
        passed: bool,
        details: str = "",
        warn_only: bool = False,
    ) -> None:
        status = "PASS" if passed else ("WARN" if warn_only else "FAIL")
        self.results[category].append({
            "test": test,
            "status": status,
            "details": details,
        })
        self.counts[status] += 1

    def _read(self, path: Path) -> Optional[str]:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _extract_version(
        self, content: str, min_version: str = "0.0.0"
    ) -> Tuple[Optional[str], bool]:
        m = re.search(r"[Vv]ersi[o\u00f3]n:\s*([\d.]+)", content)
        if not m:
            m = re.search(r"#\s*(?:\u2705\s*)?v([\d]+\.[\d]+\.[\d]+)", content)
        if not m:
            return None, False
        v = m.group(1)
        try:
            ok = tuple(map(int, v.split("."))) >= tuple(map(int, min_version.split(".")))
        except Exception:
            ok = False
        return v, ok

    def _run_shell(self, cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except Exception as e:
            return -1, "", str(e)

    def _check_command_available(self, cmd: str) -> bool:
        try:
            r = subprocess.run(["which", cmd], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    # =============================================================================
    # 1. CORE AGENTS
    # =============================================================================
    def check_core_agents(self) -> None:
        cat = "core_agents"

        path = SRC_DIR / "agents" / "vision_agent.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "VisionAgent: archivo existe", False, f"No encontrado: {path}")
        else:
            self.add(cat, "VisionAgent: archivo existe", True, str(path))

            bad  = re.search(r"from\s+src\.domain\.entities\s+import[^\n]*AgentResult", content)
            good = re.search(r"from\s+src\.agents\.base\s+import[^\n]*AgentResult", content)
            if bad:
                self.add(cat, "VisionAgent: AgentResult import", False,
                         "Importa desde src.domain.entities (INCORRECTO)")
            elif good:
                self.add(cat, "VisionAgent: AgentResult import", True,
                         "from src.agents.base import AgentResult")
            else:
                self.add(cat, "VisionAgent: AgentResult import", False,
                         "AgentResult no importado desde src.agents.base")

            v, ok = self._extract_version(content, "1.6.0")
            if v:
                self.add(cat, "VisionAgent: version >= 1.6.0", ok, f"Version: {v}")
            else:
                self.add(cat, "VisionAgent: version >= 1.6.0", False, "No se pudo extraer")

            has_num_gpu = "num_gpu" in content and "_MODELS_REQUIRING_CPU_WORKAROUND" in content
            self.add(cat, "VisionAgent: fix num_gpu:0 (gemma4)", has_num_gpu,
                     "Workaround CPU para GGML_ASSERT" if has_num_gpu
                     else "Falta workaround num_gpu:0 para gemma4")

            has_resize = "_resize_image_b64" in content and "vision_capture_max_width" in content
            self.add(cat, "VisionAgent: resize settings-based", has_resize,
                     "Usa settings.vision_capture_max_width" if has_resize
                     else "Falta resize basado en settings")

            code_only = _strip_comments(content, lang="python")
            has_moondream = "moondream" in code_only.lower()
            self.add(cat, "VisionAgent: sin moondream (legacy)", not has_moondream,
                     "Limpio de modelos obsoletos" if not has_moondream
                     else "ADVERTENCIA: Aún contiene referencias a moondream en código")

        path = SRC_DIR / "agents" / "system_agent.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "SystemAgent: archivo existe", False, f"No encontrado: {path}")
        else:
            self.add(cat, "SystemAgent: archivo existe", True, str(path))

            ret_match = re.search(r"def\s+_build_command\s*\([^)]*\)\s*->\s*([^\n:]+)", content)
            if ret_match:
                ret_type = ret_match.group(1).strip()
                ok = ret_type == "str"
                self.add(cat, "SystemAgent: _build_command() -> str", ok, f"Retorno: {ret_type}")
            else:
                self.add(cat, "SystemAgent: _build_command() -> str", False,
                         "No se encontro tipo de retorno")

            exec_ok = bool(re.search(r"execute_tool_sync\s*\([^)]*command\s*=", content, re.DOTALL))
            self.add(cat, "SystemAgent: execute_tool_sync usa command=", exec_ok,
                     "command= (respeta schema terminal_exec)" if exec_ok
                     else "Debe usar command=, no args=")

            v, ok = self._extract_version(content, "1.2.1")
            if v:
                self.add(cat, "SystemAgent: version >= 1.2.1", ok, f"Version: {v}")

    # =============================================================================
    # 2. DISPATCHER FLOW
    # =============================================================================
    def check_dispatcher(self) -> None:
        cat = "dispatcher"
        path = SRC_DIR / "core" / "dispatcher_handlers.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "dispatcher_handlers.py: existe", False, f"No encontrado: {path}")
            return

        self.add(cat, "dispatcher_handlers.py: existe", True, str(path))

        has_handler = "_handle_screenshot_analysis" in content
        self.add(cat, "Dispatcher: _handle_screenshot_analysis", has_handler,
                 "Handler presente" if has_handler else "Handler no encontrado")

        has_capture = "ScreenCaptureEngine" in content
        self.add(cat, "Dispatcher: ScreenCaptureEngine", has_capture,
                 "Intenta ScreenCaptureEngine primero" if has_capture
                 else "No intenta ScreenCaptureEngine antes del fallback")

        has_fallback = "SystemAgent" in content or "system_agent" in content.lower()
        self.add(cat, "Dispatcher: fallback SystemAgent", has_fallback,
                 "Fallback presente" if has_fallback else "Falta fallback a SystemAgent")

        has_tool = "terminal_exec" in content or "take_screenshot" in content
        self.add(cat, "Dispatcher: fallback usa tool captura", has_tool,
                 "Usa terminal_exec/take_screenshot" if has_tool
                 else "Fallback no usa tool de captura")

        has_b64 = "image_b64" in content
        self.add(cat, "Dispatcher: extrae image_b64", has_b64,
                 "Extrae image_b64 del CaptureFrame" if has_b64
                 else "No extrae image_b64")

        passes_b64 = bool(re.search(
            r"(?:vision|VisionAgent)[^\n]*image_b64|image_b64[^\n]*(?:vision|VisionAgent)",
            content,
            re.IGNORECASE,
        ))
        self.add(cat, "Dispatcher: pasa image_b64 a VisionAgent", passes_b64,
                 "Pase explicito detectado" if passes_b64
                 else "Puede estar implicito en kwargs",
                 warn_only=not passes_b64)

    # =============================================================================
    # 3. SCREEN CAPTURE — v3.1.6
    # =============================================================================
    def check_screen_capture(self) -> None:
        cat = "screen_capture"
        path = SRC_DIR / "multimodal" / "screen_capture_engine.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "ScreenCaptureEngine: existe", False, f"No encontrado: {path}")
            return

        self.add(cat, "ScreenCaptureEngine: existe", True, str(path))

        # v3.1.6: SpectacleBackend añadido como backend KDE Wayland
        backends = ["GrimBackend", "SpectacleBackend", "ScrotBackend", "ImportBackend", "PILBackend"]
        found = [b for b in backends if b in content]
        self.add(cat, "ScreenCaptureEngine: backends", len(found) >= 2,
                 f"{len(found)} backends: {', '.join(found)}" if found
                 else "No se encontraron backends")

        has_settings   = "from config.settings import settings" in content
        has_hardcoded  = "VISION_MAX_WIDTH  = 1280" in content or "VISION_MAX_HEIGHT = 720" in content
        settings_ok    = has_settings and not has_hardcoded
        self.add(cat, "ScreenCaptureEngine: settings integration (v1.0.1)", settings_ok,
                 "Usa settings.vision_capture_max_width/height" if settings_ok
                 else ("Aun tiene constantes hardcodeadas 1280/720" if has_hardcoded
                       else "No importa settings"))

        # v3.1.6: grim — verifica instalación Y compatibilidad real con el compositor
        grim_installed = self._check_command_available("grim")
        if grim_installed:
            tmp = Path(tempfile.mktemp(suffix=".png"))
            rc, _, _ = self._run_shell(["grim", str(tmp)], timeout=4)
            tmp.unlink(missing_ok=True)
            grim_works = rc == 0
            grim_detail = (
                "grim funcional (wlroots)" if grim_works
                else "grim instalado pero incompatible con compositor (no wlroots)"
            )
        else:
            grim_works = False
            grim_detail = "grim no disponible"
        self.add(cat, "Backend grim (wlroots Wayland)", grim_works,
                 grim_detail, warn_only=True)

        # v3.1.6: spectacle — backend nativo KDE Plasma Wayland
        spectacle_available = self._check_command_available("spectacle")
        self.add(cat, "Backend spectacle (KDE Wayland)", spectacle_available,
                 "spectacle disponible" if spectacle_available else "spectacle no disponible",
                 warn_only=not spectacle_available)

        scrot_available = self._check_command_available("scrot")
        self.add(cat, "Backend scrot disponible (X11)", scrot_available,
                 "scrot instalado" if scrot_available else "scrot no disponible",
                 warn_only=not scrot_available)

        try:
            import PIL  # noqa
            self.add(cat, "PIL/Pillow disponible", True, "Instalado")
        except ImportError:
            self.add(cat, "PIL/Pillow disponible", False, "Necesario para resize y compresion")

    # =============================================================================
    # 4. SETTINGS & CONFIG — v3.1.2 FIXES
    # =============================================================================
    def check_settings(self) -> None:
        cat = "settings"

        path = CONFIG_DIR / "settings.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "settings.py: existe", False, f"No encontrado: {path}")
        else:
            self.add(cat, "settings.py: existe", True, str(path))

            code_only = _strip_comments(content, lang="python")

            # v3.1.4: FIX — Regex maneja type hints Pydantic: name: type = Field(val, ...)
            timeout_match = re.search(
                r"ollama_timeout_vision(?!_fallback)\s*(?::\s*\w+)?\s*=\s*(?:Field\(\s*)?(\d+)",
                code_only
            )
            timeout_value = int(timeout_match.group(1)) if timeout_match else 0
            timeout_ok = timeout_value >= 120

            # v3.2.0 FIX: vision_fallback — verificar la línea específica, no el archivo completo
            # El check anterior ('""' in code_only) era falso positivo: "" aparece en todo el archivo.
            vision_fallback_ok = False
            fallback_line_match = re.search(
                r"llm_vision_fallback_model\s*(?::\s*\w+)?\s*=\s*(?:Field\(\s*)?(['\"])(.*?)\1",
                code_only,
            )
            if fallback_line_match:
                fallback_value = fallback_line_match.group(2).strip()
                vision_fallback_ok = fallback_value == ""
            else:
                vision_fallback_ok = "llm_vision_fallback_model" not in code_only

            checks = [
                ("settings: No moondream:v2 (legacy)", "moondream:v2" not in code_only),
                (f"settings: {_VISION_MODEL_PRIMARY} configurado", _VISION_MODEL_PRIMARY in code_only),
                ("settings: vision_fallback vacio", vision_fallback_ok),
                ("settings: timeout vision >= 120s", timeout_ok,
                 f"Timeout={timeout_value}s" if timeout_match else "No encontrado"),
                ("settings: vision_capture_max_width", "vision_capture_max_width" in code_only),
                ("settings: vision_capture_max_height", "vision_capture_max_height" in code_only),
            ]
            for item in checks:
                if len(item) == 2:
                    name, ok = item
                    details = "Configurado" if ok else "Falta o incorrecto"
                else:
                    name, ok, details = item
                self.add(cat, name, ok, details)

            v, ok = self._extract_version(content, "0.1.6")
            if v:
                self.add(cat, "settings.py: version >= 0.1.6", ok, f"v{v}")

        if MODELS_FILE.exists():
            self.add(cat, "models.yaml: existe", True, str(MODELS_FILE))
            try:
                yaml_content = MODELS_FILE.read_text(encoding="utf-8")

                # v3.1.2: FIX CRÍTICO — Usar PyYAML parse
                has_moondream = _check_yaml_for_moondream(yaml_content)
                self.add(cat, "models.yaml: sin moondream:v2", not has_moondream,
                         "Modelo legacy eliminado" if not has_moondream
                         else "ADVERTENCIA: moondream:v2 presente en claves/valores YAML")

                has_broken_headers = bool(re.search(r"^[\u2550\u2500\u2501]+$", yaml_content, re.MULTILINE))
                has_key_typos = bool(re.search(r"^[a-zA-Z_]+\s+[a-zA-Z_]+:", yaml_content, re.MULTILINE))

                yaml_parseable = False
                yaml_error = None
                try:
                    import yaml
                    parsed = yaml.safe_load(yaml_content)
                    yaml_parseable = parsed is not None
                except ImportError:
                    yaml_parseable = True
                    yaml_error = "PyYAML no instalado"
                except Exception as e:
                    yaml_error = str(e)[:100]

                yaml_ok = yaml_parseable and not has_broken_headers and not has_key_typos
                details_parts = []
                if has_broken_headers:
                    details_parts.append("headers sin #")
                if has_key_typos:
                    details_parts.append("keys con espacios")
                if yaml_error:
                    details_parts.append(f"error: {yaml_error}")

                self.add(cat, "models.yaml: formato valido", yaml_ok,
                         "YAML bien formado" if yaml_ok
                         else f"Errores: {', '.join(details_parts)}")

                self.add(cat, "models.yaml: parseable con PyYAML", yaml_parseable,
                         "PyYAML lo parsea correctamente" if yaml_parseable
                         else f"{yaml_error}")

            except Exception as e:
                self.add(cat, "models.yaml: formato valido", False, f"Error leyendo: {e}")
        else:
            self.add(cat, "models.yaml: existe", False, f"No encontrado: {MODELS_FILE}")

    # =============================================================================
    # 5. VISION SYSTEM (ESTATICO)
    # =============================================================================
    def check_vision_system(self) -> None:
        cat = "vision_system"

        path = SRC_DIR / "llm" / "client.py"
        content = self._read(path)
        if content is None:
            self.add(cat, "llm/client.py: existe", False, f"No encontrado: {path}")
            return

        self.add(cat, "llm/client.py: existe", True, str(path))

        has_chat = "def chat(" in content or "async def chat(" in content
        self.add(cat, "OllamaClient: metodo chat()", has_chat,
                 "chat() presente" if has_chat else "chat() no encontrado")

        # v3.2.0 FIX: El check anterior usaba "vision" in content.lower() que matcheaba
        # "ollama_timeout_vision" importado desde settings — falso positivo garantizado.
        # Ahora busca patrones reales de manejo de imágenes multimodal en el cliente.
        _VISION_PATTERNS = (
            r'["\']images["\']\s*:',          # campo images en payload
            r'image_b64|image_base64',         # variable de imagen
            r'_OllamaVisionClient',            # cliente vision interno
            r'multimodal|image/jpeg|image/png', # mime types
        )
        has_vision_in_client = any(
            re.search(pat, content, re.IGNORECASE)
            for pat in _VISION_PATTERNS
        )
        self.add(cat, "OllamaClient: solo texto (correcto)", not has_vision_in_client,
                 "Correctamente separado — VisionAgent usa _OllamaVisionClient"
                 if not has_vision_in_client
                 else "Contiene lógica de visión (debería estar en VisionAgent)",
                 warn_only=has_vision_in_client)

        vision_path = SRC_DIR / "agents" / "vision_agent.py"
        vision_content = self._read(vision_path)
        if vision_content:
            has_internal_client = "_OllamaVisionClient" in vision_content
            self.add(cat, "VisionAgent: cliente vision propio", has_internal_client,
                     "Usa _OllamaVisionClient" if has_internal_client
                     else "Depende de cliente externo")

            has_resize_b64 = "_resize_image_b64" in vision_content
            has_pil_resize = "Image.resize" in vision_content or "Image.LANCZOS" in vision_content
            self.add(cat, "VisionAgent: resize PIL implementado", has_resize_b64 and has_pil_resize,
                     "Resize con PIL detectado" if (has_resize_b64 and has_pil_resize)
                     else "Falta resize PIL")

    # =============================================================================
    # 6. WEB UI
    # =============================================================================
    def check_web_ui(self) -> None:
        cat = "web_ui"

        app_logic_path = SRC_DIR / "interfaces" / "web" / "app_logic.py"
        app_path = SRC_DIR / "interfaces" / "web" / "app.py"

        app_logic_content = self._read(app_logic_path)
        app_content = self._read(app_path)

        combined_content = ""
        if app_logic_content:
            combined_content += app_logic_content + "\n"
        if app_content:
            combined_content += app_content + "\n"

        if not combined_content.strip():
            self.add(cat, "app_logic.py: existe", False,
                     f"No encontrados: {app_logic_path} ni {app_path}")
            return

        has_app_logic = app_logic_content is not None
        has_app = app_content is not None

        if has_app_logic:
            self.add(cat, "app_logic.py: existe", True, str(app_logic_path))
        else:
            self.add(cat, "app.py: existe", has_app,
                     str(app_path) if has_app else f"No encontrado: {app_path}",
                     warn_only=not has_app)

        has_ui = "ui.run(" in combined_content or "nicegui" in combined_content.lower()
        self.add(cat, "WebUI: nicegui ui.run()", has_ui,
                 "nicegui detectado" if has_ui else "No detecta nicegui")

        has_chat = any(pattern in combined_content.lower() for pattern in [
            "chatcomponent", "send_message", "add_message", "textarea", "chat_component",
        ])
        self.add(cat, "WebUI: chat input", has_chat,
                 "Input de chat detectado (ChatComponent/send_message)" if has_chat
                 else "No detecta input de chat")

        dispatcher_path = SRC_DIR / "core" / "dispatcher_handlers.py"
        dispatcher_content = self._read(dispatcher_path)
        all_content = combined_content
        if dispatcher_content:
            all_content += "\n" + dispatcher_content

        has_vision_trigger = any(pattern in all_content.lower() for pattern in [
            "screen_describe", "screenshot_analysis", "_handle_screenshot",
            "describe.*pantalla", "describe.*screen", "vision_analysis",
        ])
        self.add(cat, "WebUI: trigger screen_describe", has_vision_trigger,
                 "Trigger detectado (screen_describe/screenshot_analysis)" if has_vision_trigger
                 else "No detecta trigger de vision")

    # =============================================================================
    # 7. LOGGING
    # =============================================================================
    def check_logging(self) -> None:
        cat = "logging"
        log_dir = PROJECT_ROOT / "logs"
        self.add(cat, "Directorio logs/ existe", log_dir.exists(),
                 "logs/ presente" if log_dir.exists() else "logs/ no existe")
        reg_error = log_dir / "reg_error.txt"
        self.add(cat, "reg_error.txt existe", reg_error.exists(),
                 "Presente" if reg_error.exists() else "No existe (se creara automaticamente)",
                 warn_only=not reg_error.exists())

    # =============================================================================
    # 8. TASK TYPES
    # =============================================================================
    def check_task_types(self) -> None:
        cat = "task_types"

        possible_paths = [
            SRC_DIR / "core" / "classifier_core.py",
            SRC_DIR / "classifier_core.py",
            SRC_DIR / "core" / "classifier.py",
            PROJECT_ROOT / "classifier_core.py",
        ]

        content = None
        found_path = None
        for p in possible_paths:
            c = self._read(p)
            if c is not None:
                content = c
                found_path = p
                break

        if content is None:
            dispatcher_path = SRC_DIR / "core" / "dispatcher_handlers.py"
            dispatcher_content = self._read(dispatcher_path)
            if dispatcher_content and "_handle_screenshot" in dispatcher_content:
                content = dispatcher_content
                found_path = dispatcher_path

        if content is None:
            self.add(cat, "classifier_core.py: existe", False,
                     "No encontrado en rutas esperadas",
                     warn_only=True)
            return

        self.add(cat, "classifier_core.py: existe", True, str(found_path))

        has_screen = "screen_describe" in content or "screenshot" in content.lower()
        self.add(cat, "Classifier: screen_describe task", has_screen,
                 "Task type detectado" if has_screen else "No detecta screen_describe",
                 warn_only=not has_screen)

        has_dispatcher = "dispatcher" in content.lower()
        self.add(cat, "Classifier: integra dispatcher", has_dispatcher,
                 "Dispatcher detectado" if has_dispatcher else "No integra dispatcher",
                 warn_only=True)  # v3.2.0: warn_only — classifier_core puede no mencionar dispatcher

    # =============================================================================
    # 9. RUNTIME & CONNECTIVITY (ESTATICO)
    # =============================================================================
    def check_runtime(self) -> None:
        cat = "runtime"

        try:
            import urllib.request
            req = urllib.request.Request(
                f"{OLLAMA_HOST}/api/tags",
                method="GET",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ollama_ok = True
                ollama_detail = f"Respondiendo en {OLLAMA_HOST}"
        except Exception as e:
            ollama_ok = False
            ollama_detail = f"No responde: {e}"

        self.add(cat, "Ollama server running", ollama_ok,
                 f"{ollama_detail}" if ollama_ok else f"{ollama_detail}",
                 warn_only=not ollama_ok)

        if ollama_ok:
            rc, out, _ = self._run_shell(["ollama", "list"])
            models = []
            if rc == 0:
                for line in out.strip().split("\n")[1:]:
                    parts = line.split()
                    if parts:
                        models.append(parts[0])

            # v3.2.0: verificar modelo de vision actual
            model = _VISION_MODEL_PRIMARY
            available = any(model in m for m in models)
            self.add(cat, f"Modelo {model} descargado", available,
                     "Disponible" if available else f"No descargado. Ejecuta: ollama pull {model}")

        deps = [
            ("Pillow", "PIL"),
            ("numpy", "numpy"),
            ("requests", "requests"),
            ("yaml", "yaml"),
            ("mss", "mss"),
        ]
        for name, import_name in deps:
            try:
                __import__(import_name)
                self.add(cat, f"Python dep: {name}", True, "Instalado")
            except ImportError:
                warn = name in ["mss", "yaml"]
                self.add(cat, f"Python dep: {name}", False,
                         f"{'No critico' if warn else 'CRITICO'}: No instalado. pip install {name}",
                         warn_only=warn)

    # =============================================================================
    # 10. VISION RUNTIME — v3.1.3 SIMPLIFICADO (Solo resolución original)
    # =============================================================================
    def check_vision_runtime(self) -> None:
        """Ejecuta prueba real de vision contra Ollama — SOLO resolución original."""
        cat = "vision_runtime"

        print("\n[RUNTIME] Capturando pantalla para pruebas...")
        original_img = self._capture_screen_native()
        if original_img is None:
            self.add(cat, "Runtime: captura de pantalla", False,
                     "No se pudo capturar pantalla. Verifica mss/Pillow/gnome-screenshot.")
            return

        w, h = original_img.size
        self.add(cat, "Runtime: captura de pantalla", True,
                 f"{w}x{h} (Resolución detectada)")

        # ✅ v3.1.3: SOLO resolución original (eliminados buckets 800, 512, 256)
        resolutions_to_test = ["original"]

        models_to_test = list(VISION_MODEL_CONFIG.keys())
        if self.model_filter:
            if self.model_filter in VISION_MODEL_CONFIG:
                models_to_test = [self.model_filter]
            else:
                self.add(cat, f"Runtime: modelo {self.model_filter}", False,
                         f"Modelo no soportado. Opciones: {list(VISION_MODEL_CONFIG.keys())}")
                return

        model_diagnostics = []
        for model_name in models_to_test:
            diag = self._diagnose_model_runtime(model_name, original_img, resolutions_to_test)
            model_diagnostics.append(diag)

        report = RuntimeReport(
            timestamp=datetime.now().isoformat(),
            ollama_version=self._get_ollama_version(),
            ollama_host=OLLAMA_HOST,
            project_root=str(PROJECT_ROOT),
            python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            screen_resolution_detected=f"{w}x{h}",
            models_tested=model_diagnostics,
            global_recommendations=[
                "Si gemma4:e4b crashea: reiniciar Ollama con 'OLLAMA_MAX_LOADED_MODELS=1 ollama serve --ubatch-size 2048'",
                "Verificar que base64 no tenga prefijo 'data:image/png;base64,' -- debe ser string puro",
                "Considerar modelo alternativo batiai/gemma4-e4b:q4 (5GB, mas estable, 2x mas rapido)",
                "Si timeout ocurre en resolución original: aumentar OLLAMA_TIMEOUT_VISION en .env (recomendado 150s)"
            ],
            overall_status="HEALTHY" if all(d.critical_issue == "" for d in model_diagnostics) else "CRITICAL"
        )
        self.runtime_report = report

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = LOGS_DIR / f"vision_runtime_{timestamp}.json"
        with open(json_path, 'w', encoding='utf-8') as f_json:
            json.dump(asdict(report), f_json, indent=2, ensure_ascii=False, default=str)
        self.add(cat, "Runtime: reporte JSON guardado", True, f"{json_path}")

        for diag in model_diagnostics:
            model_ok = diag.critical_issue == ""
            self.add(cat, f"Runtime: {diag.model_name} -- estado general", model_ok,
                     diag.summary if model_ok else f"{diag.critical_issue}")

            for test in diag.resolution_tests:
                status_emoji = {
                    "success": "OK", "empty": "VACIO",
                    "timeout": "TIMEOUT", "crash": "CRASH", "error": "ERROR"
                }.get(test.response_status, "?")
                notes = ""
                if test.has_data_uri_prefix:
                    notes += "[DATA_URI_PREFIX] "
                if not test.payload_valid:
                    notes += "[PAYLOAD_INVALID] "
                if test.error_detail:
                    notes += test.error_detail[:60]
                test_ok = test.response_status == "success"
                self.add(cat,
                         f"Runtime: {diag.model_name} @ {test.resolution_label} -> {status_emoji}",
                         test_ok,
                         f"{test.actual_width}x{test.actual_height} | "
                         f"tokens:{test.response_tokens} | {test.duration_ms/1000:.1f}s | {notes}",
                         warn_only=(test.response_status in ["empty", "timeout"])
                )

    @staticmethod
    def _is_black_image(img: Any, threshold: float = 5.0) -> bool:
        """Retorna True si la imagen es negra o casi negra (mean pixel < threshold)."""
        try:
            import numpy as np
            return float(np.array(img).mean()) < threshold
        except Exception:
            return False

    def _capture_screen_native(self) -> Optional[Any]:
        """
        Captura pantalla sin depender de modulos de AXIOMA.
        Orden: mss -> ImageGrab -> spectacle -> gnome-screenshot.
        Valida que cada captura no sea imagen negra antes de retornar
        (mss falla silenciosamente en Wayland devolviendo negro puro).
        """
        # Backend 1: mss (mss.MSS evita DeprecationWarning de mss.mss)
        try:
            import mss as _mss
            from PIL import Image
            _MSS = getattr(_mss, "MSS", None) or getattr(_mss, "mss", None)
            with _MSS() as sct:
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)
                img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                if self._is_black_image(img):
                    if self.verbose:
                        print("    [WARN] mss: imagen negra (Wayland sin permisos), usando fallback")
                else:
                    return img
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 verify_axioma.py:906] excepción degradada (intencional): {_d2e}")
        except Exception as e:
            if self.verbose:
                print(f"    [WARN] mss fallo: {e}")

        # Backend 2: PIL ImageGrab
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            if not self._is_black_image(img):
                return img
            if self.verbose:
                print("    [WARN] ImageGrab: imagen negra, usando fallback")
        except Exception as e:
            if self.verbose:
                print(f"    [WARN] ImageGrab fallo: {e}")

        # Backend 3: spectacle (KDE/Wayland nativo)
        try:
            rc, _, _ = self._run_shell(["which", "spectacle"], timeout=2)
            if rc == 0:
                tmp_path = tempfile.mktemp(suffix=".png")
                rc, _, _ = self._run_shell(
                    ["spectacle", "--background", "--nonotify", "--fullscreen",
                     "--output", tmp_path],
                    timeout=10
                )
                if rc == 0 and Path(tmp_path).exists():
                    from PIL import Image
                    img = Image.open(tmp_path).copy()
                    os.remove(tmp_path)
                    if not self._is_black_image(img):
                        return img
                    if self.verbose:
                        print("    [WARN] spectacle: imagen negra")
        except Exception as e:
            if self.verbose:
                print(f"    [WARN] spectacle fallo: {e}")

        # Backend 4: gnome-screenshot
        try:
            rc, _, _ = self._run_shell(["which", "gnome-screenshot"], timeout=2)
            if rc == 0:
                tmp_path = tempfile.mktemp(suffix=".png")
                rc, _, _ = self._run_shell(["gnome-screenshot", "-f", tmp_path], timeout=10)
                if rc == 0 and Path(tmp_path).exists():
                    from PIL import Image
                    img = Image.open(tmp_path).copy()
                    os.remove(tmp_path)
                    if not self._is_black_image(img):
                        return img
                    if self.verbose:
                        print("    [WARN] gnome-screenshot: imagen negra")
        except Exception as e:
            if self.verbose:
                print(f"    [WARN] gnome-screenshot fallo: {e}")

        return None

    def _resize_image(self, img: Any, max_dimension: int) -> Any:
        from PIL import Image
        if not isinstance(img, Image.Image):
            raise TypeError(f"Se esperaba PIL.Image, se recibio {type(img)}")
        width, height = img.size
        if width <= max_dimension and height <= max_dimension:
            return img.copy()
        ratio = min(max_dimension / width, max_dimension / height)
        new_w = int(width * ratio)
        new_h = int(height * ratio)
        return img.resize((new_w, new_h), Image.LANCZOS)

    def _image_to_base64(self, img: Any, fmt: str = "PNG") -> str:
        import io
        from PIL import Image
        buffer = io.BytesIO()
        img.save(buffer, format=fmt)
        return base64.b64encode(buffer.getvalue()).decode('utf-8')

    def _build_payload(self, model_name: str, image_b64: str) -> Dict[str, Any]:
        config = VISION_MODEL_CONFIG.get(model_name, {})
        messages = []
        if config.get("supports_system", True):
            messages.append({
                "role": "system",
                "content": "You are a helpful vision assistant. Describe images accurately."
            })
        messages.append({
            "role": "user",
            "content": "Describe this image in Spanish.",
            "images": [image_b64]
        })
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False
        }
        options = {}
        if config.get("requires_num_gpu_zero", False):
            options["num_gpu"] = 0
        if options:
            payload["options"] = options
        return payload

    def _validate_payload(self, payload: Dict[str, Any], model_name: str) -> List[str]:
        errors = []
        if "messages" not in payload:
            errors.append("Falta campo 'messages'")
            return errors
        user_msgs = [m for m in payload["messages"] if m.get("role") == "user"]
        if not user_msgs:
            errors.append("No hay mensaje de usuario")
        else:
            last = user_msgs[-1]
            if "images" not in last:
                errors.append("Mensaje user sin campo 'images'")
            elif not last["images"]:
                errors.append("Campo 'images' vacio")
            else:
                for idx, img in enumerate(last["images"]):
                    if img.startswith("data:image"):
                        errors.append(f"Imagen {idx} tiene prefijo data URI")
        return errors

    def _send_to_ollama(self, payload: Dict[str, Any], timeout: int) -> Tuple[str, Dict[str, Any]]:
        import urllib.request
        import urllib.error
        url = f"{OLLAMA_HOST}/api/chat"
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response = json.loads(resp.read().decode('utf-8'))
                duration = (time.time() - start) * 1000
                msg = response.get("message", {})
                content = msg.get("content", "")
                done_reason = response.get("done_reason", "")
                eval_count = response.get("eval_count", 0)
                if not content or content.strip() == "":
                    return "empty", {
                        "duration_ms": duration, "eval_count": eval_count,
                        "done_reason": done_reason, "error": "Respuesta vacia"
                    }
                return "success", {
                    "duration_ms": duration, "eval_count": eval_count,
                    "done_reason": done_reason, "content_preview": content[:200]
                }
        except urllib.error.HTTPError as e:
            duration = (time.time() - start) * 1000
            body = e.read().decode('utf-8') if e.fp else "No body"
            if "GGML_ASSERT" in body or e.code == 502:
                return "crash", {"duration_ms": duration, "error": f"GGML_ASSERT: {body[:500]}"}
            return "error", {"duration_ms": duration, "error": f"HTTP {e.code}: {body[:500]}"}
        except urllib.error.URLError as e:
            duration = (time.time() - start) * 1000
            return "error", {"duration_ms": duration, "error": f"URL Error: {str(e.reason)}"}
        except TimeoutError:
            duration = (time.time() - start) * 1000
            return "timeout", {"duration_ms": duration, "error": f"Timeout despues de {timeout}s"}
        except Exception as e:
            duration = (time.time() - start) * 1000
            return "error", {"duration_ms": duration, "error": f"Excepcion: {str(e)}"}

    def _check_model_env(self, model_name: str) -> List[Dict[str, Any]]:
        checks = []
        try:
            import urllib.request
            req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                checks.append({"name": "Ollama server", "status": "OK", "detail": f"Respondiendo en {OLLAMA_HOST}"})
        except Exception as e:
            checks.append({"name": "Ollama server", "status": "FAIL", "detail": f"No responde: {e}"})
            return checks

        rc, out, _ = self._run_shell(["ollama", "list"])
        models = []
        if rc == 0:
            for line in out.strip().split("\n")[1:]:
                parts = line.split()
                if parts:
                    models.append(parts[0])
        available = any(model_name in m for m in models)
        checks.append({
            "name": f"Modelo {model_name}",
            "status": "OK" if available else "FAIL",
            "detail": "Descargado" if available else f"No descargado. Ejecuta: ollama pull {model_name}"
        })

        if model_name.startswith("gemma4"):
            checks.append({
                "name": "Gemma4 ubatch-size",
                "status": "WARN",
                "detail": "Requiere Ollama server con --ubatch-size 2048",
                "recommendation": "OLLAMA_MAX_LOADED_MODELS=1 ollama serve --ubatch-size 2048"
            })
        return checks

    def _diagnose_model_runtime(self, model_name: str, original_img: Any, test_resolutions: List[Any]) -> ModelRuntimeDiagnostic:
        print(f"\n[RUNTIME] Diagnosticando: {model_name}")
        config = VISION_MODEL_CONFIG.get(model_name, {
            "max_dimension": 3840, "supports_system": True,
            "requires_num_gpu_zero": True, "timeout": 150
        })

        env_checks = self._check_model_env(model_name)
        for check in env_checks:
            status_str = f"[{check['status']}] {check['name']}: {check['detail']}"
            print(f"    {status_str}")

        if any(c["status"] == "FAIL" for c in env_checks):
            return ModelRuntimeDiagnostic(
                model_name=model_name, model_available=False,
                environment_checks=env_checks,
                summary="Entorno no apto para pruebas",
                critical_issue="Verificaciones de entorno fallidas"
            )

        resolution_tests = []
        for resolution in test_resolutions:
            print(f"\n[RES] Probando: {resolution}")
            try:
                # ✅ v3.1.3: Solo resolución original (sin resize)
                test_img = original_img.copy()
                actual_w, actual_h = test_img.size
                print(f"      Tamaño: {actual_w}x{actual_h}")

                b64_str = self._image_to_base64(test_img)
                has_prefix = b64_str.startswith("data:image")
                print(f"      Base64: {len(b64_str):,} chars | data_uri: {has_prefix}")

                payload = self._build_payload(model_name, b64_str)
                payload_errors = self._validate_payload(payload, model_name)
                if payload_errors:
                    for err in payload_errors:
                        print(f"      [ERR] Payload: {err}")

                timeout_val = config["timeout"]
                print(f"      Enviando (timeout: {timeout_val}s)...")
                status, meta = self._send_to_ollama(payload, config["timeout"])
                fixes = []
                if config.get("requires_num_gpu_zero", False):
                    fixes.append("Añadido num_gpu=0")

                test = ResolutionTest(
                    resolution_label=str(resolution),
                    actual_width=actual_w,
                    actual_height=actual_h,
                    base64_length=len(b64_str),
                    has_data_uri_prefix=has_prefix,
                    payload_valid=len(payload_errors) == 0,
                    response_status=status,
                    response_content=meta.get("content_preview", ""),
                    response_tokens=meta.get("eval_count", 0),
                    duration_ms=meta.get("duration_ms", 0),
                    error_detail=meta.get("error", ""),
                    model_specific_fixes_applied=fixes
                )

                if status == "success":
                    preview = meta.get("content_preview", "")
                    dur = meta["duration_ms"]
                    tok = meta["eval_count"]
                    print(f"      [OK] EXITO: {preview[:80]}...")
                    print(f"      [OK] {dur:.0f}ms | tokens:{tok}")
                elif status == "empty":
                    ec = meta.get("eval_count", 0)
                    dr = meta.get("done_reason", "N/A")
                    print(f"      [WARN] VACIO (silencioso) | tokens:{ec} | {dr}")
                elif status == "crash":
                    err = meta.get("error", "")[:100]
                    print(f"      [FAIL] CRASH: {err}")
                elif status == "timeout":
                    print(f"      [WARN] TIMEOUT")
                else:
                    err = meta.get("error", "")[:100]
                    print(f"      [FAIL] ERROR: {err}")

                resolution_tests.append(test)
            except Exception as e:
                print(f"      [FAIL] EXCEPCION: {str(e)}")
                resolution_tests.append(ResolutionTest(
                    resolution_label=str(resolution), actual_width=0, actual_height=0,
                    base64_length=0, has_data_uri_prefix=False, payload_valid=False,
                    response_status="error", response_content="", response_tokens=0,
                    duration_ms=0, error_detail=f"Excepcion: {str(e)}\n{traceback.format_exc()}"
                ))

        success = [t for t in resolution_tests if t.response_status == "success"]
        empty = [t for t in resolution_tests if t.response_status == "empty"]
        crash = [t for t in resolution_tests if t.response_status == "crash"]

        if success:
            best = min(success, key=lambda x: x.duration_ms)
            summary = f"Funciona correctamente en {best.actual_width}x{best.actual_height} ({best.duration_ms:.0f}ms)"
            critical_issue = ""
        elif crash:
            summary = "CRASH detectado"
            critical_issue = crash[0].error_detail
        elif empty:
            summary = "Respuestas vacias detectadas"
            critical_issue = "Respuesta vacia"
        else:
            summary = "Fallo general"
            critical_issue = "Error desconocido"

        return ModelRuntimeDiagnostic(
            model_name=model_name,
            model_available=True,
            environment_checks=env_checks,
            resolution_tests=resolution_tests,
            summary=summary,
            critical_issue=critical_issue
        )

    def _get_ollama_version(self) -> str:
        rc, out, _ = self._run_shell(["ollama", "--version"], timeout=5)
        if rc == 0:
            return out.strip()
        return "unknown"

    def print_summary(self) -> None:
        print("\n" + "=" * 70)
        print("  RESUMEN EJECUTIVO")
        print("=" * 70)

        for cat_key, cat_name in self.CATEGORIES:
            if self.category_filter and self.category_filter != cat_key:
                continue
            cat_results = self.results.get(cat_key, [])
            if not cat_results:
                continue

            cat_pass = sum(1 for r in cat_results if r["status"] == "PASS")
            cat_warn = sum(1 for r in cat_results if r["status"] == "WARN")
            cat_fail = sum(1 for r in cat_results if r["status"] == "FAIL")
            status_label = C_GREEN("PASS") if cat_fail == 0 else C_RED("FAIL")
            print(f"\n[{cat_name}] {status_label}: {cat_pass} WARN:{cat_warn} FAIL:{cat_fail}")

            for res in cat_results:
                icon = "✅" if res["status"] == "PASS" else ("⚠️ " if res["status"] == "WARN" else "❌")
                status_color = C_GREEN if res["status"] == "PASS" else (C_YELLOW if res["status"] == "WARN" else C_RED)
                details = f" | {res['details']}" if res['details'] else ""
                print(f"    {icon} {status_color(res['test'])}{details}")

        print("\n" + "-" * 70)
        total = self.counts["PASS"] + self.counts["WARN"] + self.counts["FAIL"]
        print(f"  TOTAL: PASS={self.counts['PASS']} WARN={self.counts['WARN']} FAIL={self.counts['FAIL']} / {total}")
        print("=" * 70)

        if self.runtime_report:
            print(f"\n[INFO] Reporte runtime JSON: {LOGS_DIR}/vision_runtime_*.json")
            if self.runtime_report.overall_status == "CRITICAL":
                print(f"  [ALERT] Estado Vision Runtime: CRITICAL - Revisar recomendaciones.")
            else:
                print(f"  [OK] Estado Vision Runtime: HEALTHY")


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AXIOMA Verifier v3.2.0")
    parser.add_argument("--category", help="Filtrar por categoria",
                        choices=[c for c, _ in AxiomaVerifier.CATEGORIES])
    parser.add_argument("--model", help="Filtrar modelo de vision (runtime)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Modo verbose")
    args = parser.parse_args()

    verifier = AxiomaVerifier(
        verbose=args.verbose,
        category_filter=args.category,
        model_filter=args.model,
    )

    verifier.check_core_agents()
    verifier.check_dispatcher()
    verifier.check_screen_capture()
    verifier.check_settings()
    verifier.check_vision_system()
    verifier.check_web_ui()
    verifier.check_logging()
    verifier.check_task_types()
    verifier.check_runtime()
    verifier.check_vision_runtime()

    verifier.print_summary()
