#!/usr/bin/env python3
import logging
# ═══════════════════════════════════════════════════════════════
# AXIOMA system_check.py — Diagnóstico unificado (v1.0.0, 2026-09-01)
# ═══════════════════════════════════════════════════════════════
# Fusión de: health_checker.py + setup_validator.py + system_diagnostic.py
# Subcomandos:
#   python tools/system_check.py health       → HealthCheck (16+ componentes)
#   python tools/system_check.py setup        → SetupValidator (entorno pre-ejecución)
#   python tools/system_check.py diagnostic   → SystemDiagnostic (SO/hardware/IA/optimizaciones)
#   python tools/system_check.py verify       → delega a verify_axioma.py (visión runtime)
#   python tools/system_check.py all          → health + setup + diagnostic
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.0.0 — Verificador de Salud del Sistema (health_checker.py fusionado)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tools/health_checker.py
# Autor: SaintWick
# Fecha: 2026-06-08
# Propósito: Verifica 16+ componentes del sistema post-migración Fases 1-4
#
# ✅ v0.0.9: Timeout validation (5-120s) y Agents (Dispatcher)
# ✅ v1.0.0: Fase 1 — chromadb → lancedb en dependencies
#            Fase 2 — ValidatorAgent 3B, InterpreterLoop config
#            Fase 3 — VAD dual, TTS cadena, multimodal config
#            Fase 4 — StateEmitter, Rafael Widget
#            Modelos actualizados a matriz actual
# ✅ v1.0.1: Fix LanceDB list_tables() → ListTablesResponse compat
# ✅ v1.0.2: Fix Ollama health check — carga robusta de OLLAMA_HOST,
#            saneamiento de URL y mensajes dinámicos.
# ═══════════════════════════════════════════════════════════════
import sys
import time
import os
from pathlib import Path
from typing import Dict, Any, List, Tuple
from datetime import datetime

# ── Cambiar al directorio raíz del proyecto para que .env se cargue ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(str(_PROJECT_ROOT))  # Asegura que el .env se encuentre

from config.settings import settings
from config.paths import Paths


class HealthCheck:
    """
    Verificador de salud del sistema AXIOMA v1.0.2.

    Checks realizados:
     1. Python version (>=3.10)
     2. Dependencies críticas (lancedb, no chromadb)
     3. Settings (timeouts 5-120s)
     4. Directorios requeridos
     5. Environment (.env cargado)
     6. Permisos de escritura
     7. Ollama conectado (con URL robusta)
     8. Modelos requeridos (matriz actual)
     9. Memory Gateway (LanceDB)
    10. Intent Classifier
    11. Agents (Dispatcher)
    12. LanceDB storage
    13. ValidatorAgent (3B forzado)
    14. State Emitter (IPC widget)
    15. Multimodal (VAD + TTS)
    16. InterpreterLoop config
    """

    def __init__(self):
        """Inicializa el verificador de salud."""
        self.results: Dict[str, Dict[str, Any]] = {}
        self.start_time = time.time()

    # ── Helper para obtener la URL de Ollama de forma robusta ──
    def _get_ollama_url(self) -> str:
        """Obtiene la URL de Ollama desde settings con saneamiento."""
        # Intentar obtener la URL desde diferentes posibles atributos
        url = getattr(settings, "OLLAMA_HOST", None) or getattr(settings, "ollama_host", None)
        if not url:
            url = "http://127.0.0.1:11434"
            print("⚠️ OLLAMA_HOST no definido en settings, usando fallback: http://127.0.0.1:11434")
        # Asegurar protocolo
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
            print(f"ℹ️ OLLAMA_HOST sin protocolo, se agregó: {url}")
        return url

    def _check_python_version(self) -> Dict[str, Any]:
        """Verifica versión de Python."""
        version = sys.version_info
        required = (3, 10)
        passed = version >= required

        return {
            "name": "Python Version",
            "passed": passed,
            "value": f"{version.major}.{version.minor}.{version.micro}",
            "required": f">={'.'.join(map(str, required))}",
            "message": "OK" if passed else f"Requiere Python {'.'.join(map(str, required))}+",
        }

    def _check_dependencies(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0: Verifica dependencias críticas.
        chromadb ELIMINADO, lancedb + pyarrow AÑADIDOS.
        """
        critical = {
            "pydantic": "pydantic",
            "fastapi": "fastapi",
            "uvicorn": "uvicorn",
            "httpx": "httpx",
            "lancedb": "lancedb",
            "pyarrow": "pyarrow",
            "scikit-learn": "sklearn",
            "numpy": "numpy",
            "PyYAML": "yaml",
        }

        missing = []
        for pkg_name, import_name in critical.items():
            try:
                __import__(import_name)
            except ImportError:
                missing.append(pkg_name)

        # ✅ v1.0.0: Verificar que chromadb NO esté instalado
        chromadb_ghost = False
        try:
            __import__("chromadb")
            chromadb_ghost = True
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:134] excepción degradada (intencional): {_d2e}")

        passed = len(missing) == 0 and not chromadb_ghost

        message = "OK"
        if missing:
            message = f"Faltan: {', '.join(missing)}"
        if chromadb_ghost:
            message += " | ⚠️ chromadb aún instalado (ejecutar: pip uninstall chromadb)"

        return {
            "name": "Dependencies",
            "passed": passed,
            "value": f"{len(critical) - len(missing)}/{len(critical)}",
            "required": "All critical, no chromadb",
            "message": message,
        }

    def _check_settings(self) -> Dict[str, Any]:
        """Verifica configuración de settings con rango 5-120s."""
        try:
            timeout = getattr(settings, 'ollama_timeout_standard',
                            getattr(settings, 'ollama_timeout', 30))
            passed = 5 <= timeout <= 120

            return {
                "name": "Settings",
                "passed": passed,
                "value": f"timeout={timeout}s",
                "required": "5-120s",
                "message": "OK" if passed else f"Timeout {timeout}s fuera de rango",
            }
        except Exception as e:
            return {
                "name": "Settings",
                "passed": False,
                "value": "Error",
                "required": "Valid",
                "message": str(e),
            }

    def _check_directories(self) -> Dict[str, Any]:
        """Verifica directorios críticos incluyendo lancedb."""
        required_dirs = [
            Paths.ROOT,
            Paths.DATA,
            Paths.LOGS,
            Paths.MEMORY,
        ]

        missing = []
        for dir_path in required_dirs:
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                except Exception:
                    missing.append(str(dir_path))

        # ✅ v1.0.0: Verificar que NO exista chroma/ y SÍ exista lancedb/
        chroma_dir = Paths.MEMORY / "chroma"
        lancedb_dir = Paths.MEMORY / "lancedb"
        chroma_ghost = chroma_dir.exists()
        lancedb_exists = lancedb_dir.exists()

        passed = len(missing) == 0 and not chroma_ghost

        message = "OK"
        if missing:
            message = f"Faltan: {missing}"
        if chroma_ghost:
            message += " | ⚠️ data/memory/chroma/ aún existe (eliminar: rm -rf data/memory/chroma/)"
        if not lancedb_exists:
            message += " | ℹ️ data/memory/lancedb/ no existe aún (se crea al primer uso)"

        return {
            "name": "Directories",
            "passed": passed,
            "value": f"{len(required_dirs) - len(missing)}/{len(required_dirs)}",
            "required": "All exist, no chroma/",
            "message": message,
        }

    def _check_ollama(self) -> Dict[str, Any]:
        """Verifica conexión con Ollama usando URL robusta."""
        try:
            from src.llm.client import OllamaClient

            # Obtener URL base saneada
            base_url = self._get_ollama_url()

            # Crear cliente con la URL explícita
            client = OllamaClient(base_url=base_url)
            connected = client.health_check()

            return {
                "name": "Ollama",
                "passed": connected,
                "value": "Connected" if connected else "Disconnected",
                "required": f"Running at {base_url}",
                "message": "OK" if connected else f"Ollama no responde en {base_url}",
            }
        except ImportError:
            return {
                "name": "Ollama",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "LLM Client no disponible",
            }
        except Exception as e:
            return {
                "name": "Ollama",
                "passed": False,
                "value": "Error",
                "required": "Running",
                "message": str(e),
            }

    def _check_models(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0: Verifica modelos instalados — matriz actualizada.
        Antes: aletheia-3b, qwen2.5-coder-abliterate:14b
        Ahora: qwen3:8b (chat), qwen2.5-coder:7b (código), qwen3-vl:4b (visión)
        """
        try:
            from src.llm.client import OllamaClient

            base_url = self._get_ollama_url()
            client = OllamaClient(base_url=base_url)
            models = client.list_models()
            model_names = [m["name"] for m in models]

            # ✅ v1.0.0: Matriz de modelos actual (2026-08-25)
            required = [
                "qwen3:8b",                              # Chat principal
                "qwen2.5-coder:7b",                      # Código / Fallback
            ]
            # Opcional pero importante
            optional = [
                "qwen3-vl:4b",                           # Visión
            ]

            installed_required = [m for m in required if m in model_names]
            installed_optional = [m for m in optional if m in model_names]

            # Al menos 1 modelo requerido
            passed = len(installed_required) >= 1

            all_installed = installed_required + installed_optional
            message = f"Instalados: {', '.join(all_installed)}" if all_installed else "Ningún modelo requerido instalado"

            if not installed_optional:
                message += " | ℹ️ modelo de visión no instalado"

            return {
                "name": "Models",
                "passed": passed,
                "value": f"{len(installed_required)}/{len(required)} required, {len(installed_optional)} optional",
                "required": "At least 1",
                "message": message,
            }
        except Exception as e:
            return {
                "name": "Models",
                "passed": False,
                "value": "Error",
                "required": "At least 1",
                "message": f"No se pudo verificar modelos: {e}",
            }

    def _check_memory(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0: Verifica Memory Gateway (LanceDB backend).
        """
        try:
            from src.memory.gateway import MemoryGateway

            gateway = MemoryGateway(session_id="health_check_test")
            passed = gateway is not None

            # Verificar que Palace usa LanceDB
            uses_lance = False
            if hasattr(gateway, '_semantic') and gateway._semantic is not None:
                sem_type = type(gateway._semantic).__name__
                uses_lance = 'Palace' in sem_type or 'Lance' in sem_type

            message = "OK"
            if uses_lance:
                message = "OK (LanceDB backend)"
            else:
                message = "OK (backend no verificado)"

            return {
                "name": "Memory Gateway",
                "passed": passed,
                "value": "Initialized" if passed else "Failed",
                "required": "Available",
                "message": message,
            }
        except ImportError:
            return {
                "name": "Memory Gateway",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "MemoryGateway no disponible",
            }
        except Exception as e:
            return {
                "name": "Memory Gateway",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_router(self) -> Dict[str, Any]:
        """Verifica router de intenciones."""
        try:
            from src.router.classifier import IntentClassifier

            classifier = IntentClassifier()
            passed = classifier is not None

            return {
                "name": "Intent Classifier",
                "passed": passed,
                "value": "Initialized" if passed else "Failed",
                "required": "Available",
                "message": "OK" if passed else "Classifier no disponible",
            }
        except ImportError:
            return {
                "name": "Intent Classifier",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "IntentClassifier no disponible",
            }
        except Exception as e:
            return {
                "name": "Intent Classifier",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_agents(self) -> Dict[str, Any]:
        """Verifica agentes disponibles usando Dispatcher."""
        try:
            from src.core.dispatcher import Dispatcher

            dispatcher = Dispatcher()

            agents_available = []

            if dispatcher.data_handler:
                agents_available.append("DataHandler")
            if dispatcher.coder:
                agents_available.append("CoderAgent")
            if dispatcher.searcher:
                agents_available.append("SearcherAgent")
            if dispatcher.researcher:
                agents_available.append("ResearcherAgent")

            # ✅ v1.0.0: Verificar ValidatorAgent
            if hasattr(dispatcher, 'validator_agent') and dispatcher.validator_agent:
                agents_available.append("ValidatorAgent(3B)")

            passed = len(agents_available) > 0

            return {
                "name": "Agents",
                "passed": passed,
                "value": f"{len(agents_available)} available",
                "required": ">0",
                "message": f"Agentes: {', '.join(agents_available)}" if passed else "Sin agentes",
            }
        except ImportError as e:
            return {
                "name": "Agents",
                "passed": False,
                "value": "Not installed",
                "required": ">0",
                "message": f"Dispatcher no disponible: {str(e)}",
            }
        except Exception as e:
            return {
                "name": "Agents",
                "passed": False,
                "value": "Error",
                "required": ">0",
                "message": str(e),
            }

    def _check_lancedb(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0 (Fase 1): Verifica LanceDB storage.
        ✅ v1.0.1: Fix list_tables() → ListTablesResponse compat (lancedb 0.33+)
        """
        try:
            import lancedb

            lancedb_dir = Paths.MEMORY / "lancedb"
            db = lancedb.connect(str(lancedb_dir))

            # ✅ v1.0.1: lancedb 0.33+ devuelve ListTablesResponse con .tables
            if hasattr(db, 'list_tables'):
                _resp = db.list_tables()
                if hasattr(_resp, 'tables'):
                    tables = [str(t) for t in _resp.tables]
                else:
                    tables = list(_resp) if not isinstance(_resp, list) else _resp
            else:
                tables = db.table_names()

            return {
                "name": "LanceDB Storage",
                "passed": True,
                "value": f"Connected, {len(tables)} tables",
                "required": "Available",
                "message": f"Tablas: {', '.join(str(t) for t in tables)}" if tables else "Sin tablas aún (se crean al primer uso)",
            }
        except ImportError:
            return {
                "name": "LanceDB Storage",
                "passed": False,
                "value": "lancedb not installed",
                "required": "Available",
                "message": "pip install lancedb pyarrow",
            }
        except Exception as e:
            return {
                "name": "LanceDB Storage",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_validator(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0 (Fase 2): Verifica ValidatorAgent con modelo 3B forzado.
        """
        try:
            from src.agents.validator import ValidatorAgent
            from config.settings import settings

            # Crear instancia — model=None fuerza 3B
            validator = ValidatorAgent(model=None)
            model_used = validator.config.model
            fallback_model = getattr(settings, 'llm_fallback_1', 'qwen3:8b')

            # Verificar que usa el modelo 3B
            uses_3b = fallback_model in model_used

            return {
                "name": "ValidatorAgent",
                "passed": True,
                "value": f"model={model_used}",
                "required": f"3B forced ({fallback_model})",
                "message": "OK — 3B forzado" if uses_3b else f"⚠️ Usa {model_used}, esperaba {fallback_model}",
            }
        except ImportError:
            return {
                "name": "ValidatorAgent",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "ValidatorAgent no disponible",
            }
        except Exception as e:
            return {
                "name": "ValidatorAgent",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_state_emitter(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0 (Fase 4): Verifica State Emitter (IPC widget).
        """
        try:
            from src.core.state_emitter import emit_state, get_current_state

            # Emitir COMPUTING y verificar
            emit_state("COMPUTING")
            current = get_current_state()
            emit_state("IDLE")  # Restaurar

            passed = current == "COMPUTING"

            return {
                "name": "State Emitter",
                "passed": passed,
                "value": f"emit→read: {current}",
                "required": "IPC working",
                "message": "OK — Widget IPC funcional" if passed else "Estado emitido no coincide",
            }
        except ImportError:
            return {
                "name": "State Emitter",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "state_emitter.py no disponible",
            }
        except Exception as e:
            return {
                "name": "State Emitter",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_multimodal(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0 (Fase 3): Verifica pipeline multimodal (VAD + TTS).
        """
        vad_available = False
        vad_backend = "none"
        tts_available = False
        tts_chain: List[str] = []

        # Check VAD
        try:
            import webrtcvad
            vad_available = True
            vad_backend = "webrtcvad"
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:568] excepción degradada (intencional): {_d2e}")

        try:
            import torch
            vad_backend += "+silero(installed)"
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:574] excepción degradada (intencional): {_d2e}")

        # Check TTS chain
        try:
            from piper import PiperVoice
            tts_chain.append("piper")
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:581] excepción degradada (intencional): {_d2e}")

        try:
            import edge_tts
            tts_chain.append("edge-tts")
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:587] excepción degradada (intencional): {_d2e}")

        try:
            import pyttsx3
            tts_chain.append("pyttsx3")
        except ImportError as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:593] excepción degradada (intencional): {_d2e}")

        tts_available = len(tts_chain) > 0

        # Check config YAML
        config_ok = False
        try:
            import yaml
            config_path = Paths.ROOT / "config" / "multimodal.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                config_ok = cfg is not None
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:607] excepción degradada (intencional): {_d2e}")

        passed = vad_available and tts_available
        chain_str = " → ".join(tts_chain) if tts_chain else "none"

        return {
            "name": "Multimodal",
            "passed": passed,
            "value": f"VAD={vad_backend}, TTS={chain_str}",
            "required": "VAD + at least 1 TTS",
            "message": "OK" if passed else f"VAD={'✅' if vad_available else '❌'}, TTS={'✅' if tts_available else '❌'}",
        }

    def _check_interpreter_loop_config(self) -> Dict[str, Any]:
        """
        ✅ v1.0.0 (Fase 2): Verifica configuración del InterpreterLoop.
        """
        try:
            from src.core.interpreter_loop import LoopConfig

            config = LoopConfig()

            timeout_ok = config.global_timeout <= 120.0
            sandbox_ok = config.sandbox_timeout <= 60.0
            attempts_ok = config.max_fix_attempts >= 3

            passed = timeout_ok and sandbox_ok and attempts_ok

            issues = []
            if not timeout_ok:
                issues.append(f"global_timeout={config.global_timeout}s (debe ≤120)")
            if not sandbox_ok:
                issues.append(f"sandbox_timeout={config.sandbox_timeout}s (debe ≤60)")
            if not attempts_ok:
                issues.append(f"max_fix_attempts={config.max_fix_attempts} (debe ≥3)")

            return {
                "name": "InterpreterLoop Config",
                "passed": passed,
                "value": f"timeout={config.global_timeout}s, sandbox={config.sandbox_timeout}s, attempts={config.max_fix_attempts}",
                "required": "timeout≤120, sandbox≤60, attempts≥3",
                "message": "OK" if passed else f"Issues: {'; '.join(issues)}",
            }
        except ImportError:
            return {
                "name": "InterpreterLoop Config",
                "passed": False,
                "value": "Not installed",
                "required": "Available",
                "message": "interpreter_loop.py no disponible",
            }
        except Exception as e:
            return {
                "name": "InterpreterLoop Config",
                "passed": False,
                "value": "Error",
                "required": "Available",
                "message": str(e),
            }

    def _check_env(self) -> Dict[str, Any]:
        """Verifica variables de entorno."""
        env_file = Paths.ROOT / ".env"
        passed = env_file.exists()

        return {
            "name": "Environment",
            "passed": passed,
            "value": "Loaded" if passed else "Missing",
            "required": ".env exists",
            "message": "OK" if passed else "Archivo .env no encontrado",
        }

    def _check_permissions(self) -> Dict[str, Any]:
        """Verifica permisos de escritura."""
        test_file = Paths.DATA / ".write_test"
        passed = False

        try:
            test_file.touch()
            test_file.unlink()
            passed = True
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 system_check.py:690] excepción degradada (intencional): {_d2e}")

        return {
            "name": "Permissions",
            "passed": passed,
            "value": "Write OK" if passed else "Write Failed",
            "required": "Write access",
            "message": "OK" if passed else "Sin permisos de escritura en data/",
        }

    def run_all_checks(self) -> Dict[str, Any]:
        """Ejecuta todos los checks de salud."""
        checks = [
            # Core
            self._check_python_version,
            self._check_dependencies,
            self._check_settings,
            self._check_directories,
            self._check_env,
            self._check_permissions,
            # LLM
            self._check_ollama,
            self._check_models,
            # Memory (Fase 1)
            self._check_memory,
            self._check_lancedb,
            # Router
            self._check_router,
            # Agents (Fase 2)
            self._check_agents,
            self._check_validator,
            self._check_interpreter_loop_config,
            # Multimodal (Fase 3)
            self._check_multimodal,
            # Widget IPC (Fase 4)
            self._check_state_emitter,
        ]

        for check in checks:
            try:
                result = check()
                self.results[result["name"]] = result
            except Exception as e:
                self.results[check.__name__] = {
                    "name": check.__name__,
                    "passed": False,
                    "value": "Error",
                    "required": "N/A",
                    "message": str(e),
                }

        return self.get_summary()

    def get_summary(self) -> Dict[str, Any]:
        """Obtiene resumen de checks."""
        total = len(self.results)
        passed = sum(1 for r in self.results.values() if r["passed"])
        failed = total - passed

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success_rate": round(passed / max(total, 1) * 100, 2),
            "duration_ms": round((time.time() - self.start_time) * 1000, 2),
            "timestamp": datetime.now().isoformat(),
        }

    def print_report(self, verbose: bool = True) -> None:
        """Imprime reporte de salud."""
        summary = self.get_summary()

        print("\n" + "=" * 60)
        print("🏥 AXIOMA — REPORTE DE SALUD")
        print("=" * 60)

        status = "✅ SALUDABLE" if summary["failed"] == 0 else "⚠️ PROBLEMAS DETECTADOS"
        print(f"\nEstado: {status}")
        print(f"Checks: {summary['passed']}/{summary['total']} aprobados ({summary['success_rate']}%)")
        print(f"Duración: {summary['duration_ms']}ms")

        if verbose:
            print("\n" + "-" * 60)
            print("DETALLE POR COMPONENTE")
            print("-" * 60)

            for name, result in self.results.items():
                icon = "✅" if result["passed"] else "❌"
                print(f"\n{icon} {name}")
                print(f"   Valor: {result['value']}")
                print(f"   Requerido: {result['required']}")
                print(f"   Mensaje: {result['message']}")

            print("\n" + "=" * 60)

            # Recomendaciones
            if summary["failed"] > 0:
                print("\n🔧 RECOMENDACIONES:")
                for name, result in self.results.items():
                    if not result["passed"]:
                        print(f"  • {name}: {result['message']}")
                print()

    def run_and_exit(self) -> int:
        """Ejecuta checks y retorna código de salida."""
        self.run_all_checks()
        summary = self.get_summary()
        return 0 if summary["failed"] == 0 else 1


# ============================================================================
# FUNCIÓN PRINCIPAL
# ============================================================================


# ============================================================================
# SETUP VALIDATOR (fusionado de setup_validator.py)
# ============================================================================

#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5 — Validador de Entorno Pre-Ejecución
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tools/setup_validator.py
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Verifica entorno antes de arrancar AXIOMA
# ═══════════════════════════════════════════════════════════════

import sys
import os
from pathlib import Path
from typing import Dict, Any

class SetupValidator:
    """Validador de configuración pre-ejecución."""

    CRITICAL_CHECKS = [
        "python_version",
        "dependencies",
        "env_file",
        "directories",
        "ollama",
    ]

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passed: List[str] = []

    def check_python_version(self) -> bool:
        """Verifica versión de Python."""
        version = sys.version_info
        required = (3, 10)

        if version >= required:
            self.passed.append(f"Python {version.major}.{version.minor}.{version.micro}")
            return True
        else:
            self.errors.append(
                f"Python {required[0]}.{required[1]}+ requerido, encontrado "
                f"{version.major}.{version.minor}"
            )
            return False

    def check_dependencies(self) -> bool:
        """Verifica dependencias críticas.

        ✅ v1.0.0 (fusión): mapeo nombre→módulo (igual que HealthCheck) —
        "scikit-learn" se importa como "sklearn", no "scikit_learn".
        """
        critical = {
            "pydantic": "pydantic",
            "fastapi": "fastapi",
            "httpx": "httpx",
            "yaml": "yaml",
            "lancedb": "lancedb",
            "pyarrow": "pyarrow",
            "scikit-learn": "sklearn",
            "numpy": "numpy",
        }

        missing = []
        for dep, import_name in critical.items():
            try:
                __import__(import_name)
            except ImportError:
                missing.append(dep)

        if missing:
            self.errors.append(f"Dependencias faltantes: {', '.join(missing)}")
            return False
        else:
            self.passed.append(f"Dependencias críticas ({len(critical)})")
            return True

    def check_env_file(self) -> bool:
        """Verifica archivo .env."""
        env_file = _PROJECT_ROOT / ".env"

        if env_file.exists():
            self.passed.append(".env existe")
            return True
        else:
            self.warnings.append(".env no encontrado (usando defaults)")
            return True  # No crítico

    def check_directories(self) -> bool:
        """Verifica directorios críticos."""
        required = [
            _PROJECT_ROOT / "data",
            _PROJECT_ROOT / "logs",
            _PROJECT_ROOT / "config",
        ]

        missing = []
        for dir_path in required:
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    missing.append(f"{dir_path} ({e})")

        if missing:
            self.errors.append(f"Directorios críticos: {', '.join(missing)}")
            return False
        else:
            self.passed.append("Directorios críticos")
            return True

    def check_ollama(self) -> bool:
        """Verifica conexión con Ollama."""
        try:
            from src.llm.client import OllamaClient
            client = OllamaClient()
            connected = client.health_check()
            client.close()

            if connected:
                self.passed.append("Ollama conectado")
                return True
            else:
                self.warnings.append("Ollama no responde (fallback disponible)")
                return True
        except ImportError:
            self.warnings.append("LLM Client no disponible")
            return True
        except Exception as e:
            self.warnings.append(f"Ollama error: {e}")
            return True

    def run_all_checks(self) -> Tuple[bool, Dict[str, Any]]:
        """Ejecuta todos los checks."""
        checks = {
            "python_version": self.check_python_version,
            "dependencies": self.check_dependencies,
            "env_file": self.check_env_file,
            "directories": self.check_directories,
            "ollama": self.check_ollama,
        }

        results = {}
        for name, check_func in checks.items():
            try:
                passed = check_func()
                results[name] = passed
            except Exception as e:
                self.errors.append(f"{name}: {e}")
                results[name] = False

        all_critical_passed = all(
            results.get(check, False) for check in self.CRITICAL_CHECKS
        )

        return all_critical_passed, {
            "passed": len(self.passed),
            "warnings": len(self.warnings),
            "errors": len(self.errors),
            "details": results,
        }

    def print_report(self) -> None:
        """Imprime reporte de validación."""
        print("\n" + "=" * 60)
        print("🔧 SETUP VALIDATOR — AXIOMA")
        print("=" * 60)

        if self.passed:
            print("\n✅ PASSED:")
            for item in self.passed:
                print(f"   • {item}")

        if self.warnings:
            print("\n⚠️ WARNINGS:")
            for item in self.warnings:
                print(f"   • {item}")

        if self.errors:
            print("\n❌ ERRORS:")
            for item in self.errors:
                print(f"   • {item}")

        print("\n" + "=" * 60)

        if self.errors:
            print("❌ VALIDACIÓN FALLIDA — Corregir errores antes de continuar")
        elif self.warnings:
            print("⚠️ VALIDACIÓN EXITOSA — Con advertencias")
        else:
            print("✅ VALIDACIÓN EXITOSA — Todo listo")

        print()




# ============================================================================
# SYSTEM DIAGNOSTIC (fusionado de system_diagnostic.py)
# ============================================================================

#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/system_diagnostic.py
# Autor: SaintWick
"""AXIOMA System Diagnostic v2 - Incluye verificación de optimizaciones aplicadas"""
import os, sys, platform, subprocess, sqlite3, importlib.metadata, json
from pathlib import Path

class SystemDiagnostic:
    def __init__(self):
        self.root = Path.cwd()
        self.lines = []

    def _cmd(self, cmd, timeout=8, sudo=False):
        try:
            if sudo:
                cmd = ['sudo'] + cmd
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return res.stdout.strip() if res.returncode == 0 else None
        except Exception:
            return None

    def _pkg_version(self, name):
        try:
            return importlib.metadata.version(name)
        except Exception:
            return "❌ No instalado"

    def _add(self, text=""):
        self.lines.append(text)

    def collect_system(self):
        self._add("## 🖥️ Sistema Operativo & Python")
        self._add(f"- **OS**: {platform.system()} {platform.release()}")
        self._add(f"- **Kernel**: {platform.release()}")
        self._add(f"- **Python**: {sys.version.split()[0]}")
        self._add(f"- **Entorno**: {'✅ Virtualenv/venv activo' if sys.prefix != sys.base_prefix else '⚠️ Python del sistema'}")
        self._add(f"- **Arquitectura**: {platform.machine()}")
        self._add()

    def collect_hardware(self):
        self._add("## ⚙️ Hardware")
        cpu_info = self._cmd(["lscpu"])
        if cpu_info:
            model = next((l.split(":", 1)[1].strip() for l in cpu_info.splitlines() if "Model name" in l), "N/A")
            cores = next((l.split(":", 1)[1].strip() for l in cpu_info.splitlines() if l.startswith("CPU(s):")), "N/A")
            self._add(f"- **CPU**: {model} ({cores} cores)")
        mem = self._cmd(["free", "-h"])
        if mem:
            total = next((l.split()[1] for l in mem.splitlines() if "Mem:" in l), "N/A")
            self._add(f"- **RAM Total**: {total}")
        self._add()
        self._add("### 🎮 GPUs Detectadas")
        lspci = self._cmd(["lspci", "-vnn"])
        gpus = [l for l in (lspci or "").splitlines() if "VGA" in l or "3D controller" in l]
        for gpu in gpus:
            name = gpu.split(":", 2)[-1].strip() if ":" in gpu else gpu
            self._add(f"- `{name}`")
            if "AMD" in name.upper():
                rocm = self._cmd(["rocm-smi"]) or self._cmd(["rocminfo"])
                self._add(f"  - ROCm: {'✅' if rocm else '❌ No soportado'}")
        self._add()

    def collect_ai_stack(self):
        self._add("## 🤖 Stack IA & Ollama")
        ollama = self._cmd(["ollama", "list"])
        if ollama:
            self._add("### Modelos Ollama")
            self._add("```")
            self._add(ollama)
            self._add("```")
        self._add()
        self._add("### Paquetes Críticos (Python)")
        pkgs = ["torch", "torchaudio", "sentence-transformers", "lancedb",
                "nicegui", "faster-whisper", "pydantic", "httpx",
                "onnxruntime", "webrtcvad", "pillow", "ctranslate2"]
        for p in pkgs:
            self._add(f"- `{p}`: {self._pkg_version(p)}")
        self._add()

    # ====== NUEVO: VERIFICACIÓN DE OPTIMIZACIONES ======
    def collect_optimizations(self):
        self._add("## ⚡ Optimizaciones AXIOMA Optimizer")
        self._add()

        # 1. Variables de entorno (usuario)
        env_file = Path.home() / ".config/environment.d/axioma.conf"
        if env_file.exists():
            self._add(f"### ✅ Variables de entorno (usuario)")
            self._add(f"- **Archivo**: `{env_file}`")
            try:
                content = env_file.read_text()
                for line in content.splitlines():
                    if line.strip() and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        self._add(f"  - `{k}` = `{v}`")
            except Exception as e:
                self._add(f"- ⚠️ Error leyendo: {e}")
        else:
            self._add("### ❌ Variables de entorno (usuario): NO configuradas")
        self._add()

        # 2. Override systemd de Ollama (CRÍTICO)
        ollama_override = Path("/etc/systemd/system/ollama.service.d/optimizer.conf")
        self._add("### Variables en proceso Ollama (systemd)")
        if ollama_override.exists():
            self._add(f"- ✅ **Override systemd**: `{ollama_override}`")
            # Verificar variables realmente cargadas
            ollama_pid = self._cmd(["pgrep", "-x", "ollama"])
            if ollama_pid:
                environ = self._cmd(["cat", f"/proc/{ollama_pid}/environ"], sudo=True)
                if environ:
                    vars_found = [v for v in environ.split('\0')
                                  if v.startswith(('OLLAMA_', 'OMP_', 'MKL_'))]
                    self._add(f"- ✅ **Proceso Ollama (PID {ollama_pid})**: {len(vars_found)} variables activas")
                    for v in sorted(vars_found):
                        self._add(f"  - `{v}`")
                else:
                    self._add("- ⚠️ No se pudo leer entorno del proceso")
            else:
                self._add("- ⚠️ Proceso Ollama no detectado")
        else:
            self._add("- ❌ **Override systemd**: NO configurado")
            self._add("  - ⚠️ Las variables de `environment.d` NO llegan a Ollama")
        self._add()

        # 3. Scheduler scx
        self._add("### Scheduler (sched_ext)")
        sched_name = None
        if Path("/sys/kernel/sched/scx/scx_name").exists():
            sched_name = self._cmd(["cat", "/sys/kernel/sched/scx/scx_name"])
        else:
            # Fallback: detectar proceso scx_*
            ps_out = self._cmd(["ps", "aux"])
            if ps_out:
                for line in ps_out.splitlines():
                    if "scx_" in line and "grep" not in line:
                        parts = line.split()
                        for p in parts:
                            if p.startswith("scx_"):
                                sched_name = p.replace("scx_", "")
                                break
        if sched_name and sched_name != "none":
            self._add(f"- ✅ **Scheduler activo**: `{sched_name}` (óptimo)" if sched_name == "rusty"
                      else f"- ⚠️ **Scheduler activo**: `{sched_name}`")
        else:
            self._add("- ❌ **Scheduler**: CFS default (no optimizado)")
        self._add()

        # 4. Sysctl
        self._add("### Sysctl (baja latencia)")
        sysctl_file = Path("/etc/sysctl.d/99-axioma.conf")
        if sysctl_file.exists():
            self._add(f"- ✅ **Archivo**: `{sysctl_file}`")
            swappiness = self._cmd(["sysctl", "-n", "vm.swappiness"])
            file_max = self._cmd(["sysctl", "-n", "fs.file-max"])
            if swappiness:
                self._add(f"  - `vm.swappiness` = `{swappiness}`")
            if file_max:
                self._add(f"  - `fs.file-max` = `{file_max}`")
        else:
            self._add("- ❌ **Sysctl**: NO configurado")
        self._add()

        # 5. Ananicy rules
        self._add("### Ananicy (prioridades)")
        ananicy_file = Path("/etc/ananicy.d/axioma.rules")
        if ananicy_file.exists():
            self._add(f"- ✅ **Reglas**: `{ananicy_file}`")
        else:
            self._add("- ❌ **Reglas Ananicy**: NO configuradas")
        self._add()

        # 6. ZRAM
        self._add("### ZRAM (swap comprimido)")
        zram = self._cmd(["swapon", "--show"])
        if zram and "zram" in zram:
            self._add(f"- ✅ **ZRAM activo**: `{zram.splitlines()[1] if len(zram.splitlines()) > 1 else zram}`")
        else:
            self._add("- ❌ **ZRAM**: No activo")
        self._add()

        # 7. Config runtime AXIOMA
        self._add("### Config runtime (NiceGUI/Uvicorn)")
        opt_env = self.root / "config/optimizer.env"
        if opt_env.exists():
            self._add(f"- ✅ **Archivo**: `{opt_env}`")
        else:
            self._add("- ❌ **Config runtime**: NO creada")
        self._add()

    def collect_axioma(self):
        self._add("## 🏗️ Estado Proyecto AXIOMA")
        key_files = ["main.py", "config/settings.py", "src/core/dispatcher.py",
                     ".env.example", "data/axioma.db", "config/models.yaml"]
        for f in key_files:
            p = self.root / f
            self._add(f"- `{f}`: {'✅ Existe' if p.exists() else '❌ Falta'}")
        git_status = self._cmd(["git", "status", "--short"])
        if git_status is not None:
            changes = len([l for l in git_status.splitlines() if l.strip()])
            self._add(f"- **Git**: {changes} cambios sin commitear")
        db_path = self.root / "data" / "axioma.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                self._add(f"- **DB (axioma.db)**: ✅ {tables} tablas")
                conn.close()
            except Exception as e:
                self._add(f"- **DB**: ⚠️ Error: {e}")
        self._add()

    def run(self):
        self._add(f"# 🏥 AXIOMA System Diagnostic v2 — {datetime.now():%Y-%m-%d %H:%M}")
        self._add()
        self.collect_system()
        self.collect_hardware()
        self.collect_ai_stack()
        self.collect_optimizations()  # NUEVO
        self.collect_axioma()
        self._add("---")
        self._add("✅ Reporte v2 con verificación completa de optimizaciones.")
        return "\n".join(self.lines)


# ============================================================================
# CLI UNIFICADO — subcomandos
# ============================================================================
def _run_health(json_out: bool = False, quiet: bool = False) -> int:
    checker = HealthCheck()
    checker.run_all_checks()
    if json_out:
        import json
        print(json.dumps({"summary": checker.get_summary(), "checks": checker.results}, indent=2))
    else:
        checker.print_report(verbose=not quiet)
    summary = checker.get_summary()
    return 0 if summary["failed"] == 0 else 1


def _run_setup(quiet: bool = False) -> int:
    validator = SetupValidator(verbose=not quiet)
    success, stats = validator.run_all_checks()
    if not quiet:
        validator.print_report()
    else:
        status = "✅ OK" if success else "❌ FAIL"
        print(f"Setup Validator: {status} ({stats['passed']} passed, {stats['errors']} errors)")
    return 0 if success else 1


def _run_diagnostic() -> int:
    diag = SystemDiagnostic()
    report = diag.run()
    print(report)
    return 0


def _run_verify(extra_args: List[str]) -> int:
    """Delega a verify_axioma.py (verificación integral + visión runtime)."""
    import subprocess as _sp
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "verify_axioma.py")] + extra_args
    proc = _sp.run(cmd)
    return proc.returncode


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="AXIOMA System Check — diagnóstico unificado (health + setup + diagnostic + verify)",
        epilog="Ejemplos:\n"
               "  python tools/system_check.py all\n"
               "  python tools/system_check.py health --json\n"
               "  python tools/system_check.py verify --category core\n"
    )
    parser.add_argument("command", nargs="?", default="all",
                        choices=["health", "setup", "diagnostic", "verify", "all"],
                        help="Subcomando a ejecutar (default: all)")
    parser.add_argument("--json", action="store_true", help="(health) salida JSON")
    parser.add_argument("--quiet", "-q", action="store_true", help="Solo resumen")
    parser.add_argument("--category", help="(verify) filtrar categoría")
    parser.add_argument("--model", help="(verify) filtrar modelo de visión")
    args, unknown = parser.parse_known_args()

    if args.command in ("health", "all"):
        rc = _run_health(json_out=args.json, quiet=args.quiet)
        if args.command == "health":
            return rc
        print()
    if args.command in ("setup", "all"):
        rc = _run_setup(quiet=args.quiet)
        if args.command == "setup":
            return rc
        print()
    if args.command in ("diagnostic", "all"):
        rc = _run_diagnostic()
        if args.command == "diagnostic":
            return rc
        print()
    if args.command == "verify":
        extra = list(unknown)
        if args.category:
            extra += ["--category", args.category]
        if args.model:
            extra += ["--model", args.model]
        return _run_verify(extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
