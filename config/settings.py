#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# settings.py - Configuración Central con Validaciones
# Ruta: /ruta/a/axioma/config/settings.py
# Autor: SaintWick
# Versión: 0.1.6b
#
# Define la configuración de AXIOMA usando Pydantic Settings.
# Incluye campos para modelos LLM, timeouts, rutas, APIs externas,
# agentes, seguridad, geolocalización, evaluador-optimizador,
# límites de código, MCP, sandbox, embedding, monitoreo, captura
# de pantalla y modo Jarvis. Los métodos computacionales están
# extraídos en settings_computations.py como mixin.
# ═══════════════════════════════════════════════════════════════
from pathlib import Path
from typing import Optional, List, Literal, Dict, Any, Union
import logging
import subprocess
import os
from pydantic import Field, field_validator, model_validator, ValidationInfo
from pydantic import AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict
from src.utils.degradation import log_degraded

# ✅ v0.6.9m: raíz portable (CI/hardware) — antes hardcodeada a
# /ruta/a/axioma → en GitHub Actions intentaba crear
# /usuario (sin permiso) → PermissionError al importar settings.

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[1]

from .settings_llm import SettingsLLM
from .settings_vision import SettingsVision
from .settings_paths import SettingsPaths
from .settings_web import SettingsWeb
from .settings_agents import SettingsAgents
from .settings_security import SettingsSecurity
from .settings_evaluator import SettingsEvaluator
from .settings_tools import SettingsTools
from .settings_memory import SettingsMemory
from .settings_jarvis import SettingsJarvis
from .settings_computations import SettingsComputations


class Settings(BaseSettings, SettingsLLM, SettingsVision, SettingsPaths, SettingsWeb, SettingsAgents, SettingsSecurity, SettingsEvaluator, SettingsTools, SettingsMemory, SettingsJarvis, SettingsComputations):
    """Configuración central con validaciones estrictas para AXIOMA."""

    model_config = SettingsConfigDict(
        # ✅ ISSUE-147 (MEDIDO): `env_file=".env"` es relativo a la carpeta de trabajo, así
        # que arrancando desde otra carpeta (contenedor con otro WORKDIR, servicio sin
        # WorkingDirectory) la configuración del usuario NO se cargaba y no avisaba nada.
        # Ahora el `.env` es el del proyecto, sin importar desde dónde se arranque.
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )










    # ═══════════════════════════════════════════════════════════
    # === PLAN v2.0 (2026-09-01) — integridad de contenido + seguridad ===
    # ═══════════════════════════════════════════════════════════
    # Fase 6: delimita contenido externo (web) antes de inyectarlo al LLM
    # para evitar prompt injection indirecta vía resultados de búsqueda.
    content_grounding_enabled: bool = Field(
        default=True,
        description="Envuelve fuentes externas en <fuente_externa> y valida citas [n] "
                    "contra search_results antes de sintetizar (Fase 6 plan v2.0)"
    )
    # Fase 3: gate anti-injection sobre el input directo del usuario, antes de
    # cualquier cache/classify en dispatcher_process.process().
    input_screening_enabled: bool = Field(
        default=True,
        description="Screen anti-jailbreak del input directo en process() "
                    "(Fase 3 plan v2.0)"
    )
    # Fase 4: repo-map opcional para CoderAgent (CodeIndex de axioma_auditor).
    # OFF por defecto — no cambia el comportamiento actual.
    repo_map_enabled: bool = Field(
        default=False,
        description="Inyecta CONTEXTO DEL REPO en _build_prompt() de CoderAgent "
                    "usando CodeIndex (Fase 4 plan v2.0)"
    )
    repo_map_max_symbols: int = Field(
        default=10, ge=1, le=50,
        description="Máximo de símbolos del repo-map inyectados al prompt del CoderAgent"
    )

    @property
    def jarvis_config(self) -> Dict[str, Any]:
        """
        Puente entre los campos jarvis_* sueltos y el dict que consumen
        JarvisEngine y VoicePipeline. Sin esto ambos ignoran el .env
        y usan defaults hardcodeados.
        """
        return {
            "default_mode": self.jarvis_mode_default,
            "wake_word": self.jarvis_wake_word,
            "response_max_tokens": self.jarvis_response_max_tokens,
            "tts_auto_speak": self.jarvis_tts_auto_speak,
            "voice_confidence_threshold": self.jarvis_voice_confidence_threshold,
            "screen_describe_on_query": self.jarvis_screen_describe_on_query,
            "autonomous_task_max_steps": self.jarvis_autonomous_task_max_steps,
            "autonomous_task_timeout": self.jarvis_autonomous_task_timeout,
            "gui_indicator": self.jarvis_gui_indicator,
        }


# ═══════════════════════════════════════════════════════════
# INSTANCIA GLOBAL
# ═══════════════════════════════════════════════════════════
settings = Settings()


# ═══════════════════════════════════════════════════════════
# FUNCIONES HELPER
# ═══════════════════════════════════════════════════════════
def get_settings() -> Settings:
    """Obtiene la instancia global de configuración."""
    return settings


def reload_settings() -> Settings:
    """Recarga la configuración desde el archivo .env."""
    global settings
    settings = Settings()
    return settings


def get_setting_value(key: str, default: Any = None) -> Any:
    """Obtiene un valor específico de configuración por nombre de atributo."""
    return getattr(settings, key, default)


def validate_setting(key: str, value: Any) -> bool:
    """Valida un valor para un atributo de configuración específico."""
    try:
        current_values = settings.model_dump()
        current_values[key] = value
        Settings(**current_values)
        return True
    except Exception as e:
        log_degraded(logger, e, "config/settings.py:validate_setting")
        return False


# ═══════════════════════════════════════════════════════════
# VALIDACIÓN DE CONFIGURACIÓN AL IMPORTAR
# ═══════════════════════════════════════════════════════════
def _validate_configuration() -> None:
    """Valida la configuración al importar el módulo."""

    # 1. Validar timeouts
    if settings.ollama_timeout_standard < 30:
        logging.warning(
            f"⚠️ TIMEOUT BAJO: ollama_timeout_standard={settings.ollama_timeout_standard}s. "
            "Se recomienda ≥45s para evitar fallos en Validator."
        )

    # 2. Validar cadena de fallback
    if settings.llm_fallback_1 and settings.llm_fallback_2 and settings.llm_fallback_1 == settings.llm_fallback_2:
        logging.warning(
            f"⚠️ FALLBACK DUPLICADO: llm_fallback_1 y llm_fallback_2 apuntan al mismo modelo "
            f"({settings.llm_fallback_1}). El tercer intento repetirá un modelo ya fallido."
        )

    # 3. Validar modelos críticos con parseo robusto
    # ✅ FIX AUDITORÍA (L12): verify_models_on_startup estaba definido pero
    # nunca se chequeaba acá — el subprocess "ollama list" corría siempre al
    # importar config.settings (que casi todo archivo del proyecto importa),
    # sin importar el valor de esa config. model_check_timeout tampoco se
    # usaba — el timeout real estaba hardcodeado en 10. Se gatea el bloque
    # completo y se usa el timeout configurado. Las otras 9 validaciones de
    # esta función (timeouts, MCP, sandbox, etc.) son chequeos puros sin
    # subprocess — se dejan sin gatear, verify_models_on_startup solo
    # controla lo que su nombre promete: la verificación de MODELOS.
    if settings.verify_models_on_startup:
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=settings.model_check_timeout,
            )
            available_models = set()
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if parts and parts[0]:
                    model_name = parts[0].strip()
                    available_models.add(model_name.lower())

            for model in settings.critical_models:
                model_lower = model.lower()
                model_base = model_lower.split(":")[0]
                if not any(
                    model_lower == m
                    or m.startswith(model_base + ":")
                    or m.startswith(model_base + " ")
                    or model_base in m
                    for m in available_models
                ):
                    logging.warning(f"⚠️ MODELO CRÍTICO FALTANTE: {model}")

            # Validar modelo juez
            if settings.llm_judge_model:
                judge_model = settings.llm_judge_model.lower()
                if not any(
                    judge_model == m or judge_model.startswith(m.split(":")[0] + ":")
                    for m in available_models
                ):
                    logging.error(f"❌ ERROR CRÍTICO: MODELO JUEZ NO ENCONTRADO: {settings.llm_judge_model}")
                    logging.error("El Validator Agent usará el modelo por defecto y fallará.")
                    logging.error(f"Solución: Ejecuta 'ollama pull {settings.llm_judge_model}'")
                else:
                    logging.info(f"✅ Modelo Juez disponible: {settings.llm_judge_model}")
            else:
                logging.info("✅ Modelo Juez desactivado (llm_judge_model='') — ValidatorAgent usa llm_fallback_1")

        except FileNotFoundError:
            logging.warning("⚠️ Ollama no encontrado en PATH — saltando validación de modelos")
        except subprocess.TimeoutExpired:
            logging.warning("⚠️ Timeout verificando modelos Ollama — continuando")
        except Exception as e:
            logging.debug(f"Validación de modelos omitida: {e}")
    else:
        logging.debug("Validación de modelos Ollama omitida (verify_models_on_startup=False)")

    # 4. Validar Agent Loop config
    if settings.agent_loop_max_iterations < 1:
        logging.warning(
            f"⚠️ AGENT LOOP: max_iterations={settings.agent_loop_max_iterations}. "
            "Se recomienda ≥10 para tareas complejas."
        )

    # 5. Validar config MCP
    if settings.mcp_auto_connect and not settings.mcp_servers:
        logging.warning(
            "⚠️ MCP: mcp_auto_connect=True pero mcp_servers está vacío. "
            "No se conectará ningún server MCP al startup."
        )
    parsed_servers = settings.parse_mcp_servers()
    if parsed_servers:
        logging.info(
            f"✅ MCP: {len(parsed_servers)} server(s) configurado(s): "
            f"{[s['label'] for s in parsed_servers]}"
        )

    # 6. Validar config Sandbox
    if settings.sandbox_mode == "strict" and settings.sandbox_enable_network:
        logging.warning(
            "⚠️ SANDBOX: sandbox_enable_network=True en modo 'strict'. "
            "El acceso a red será deshabilitado automáticamente."
        )
    if not settings.sandbox_blocked_commands:
        logging.warning(
            "⚠️ SANDBOX: sandbox_blocked_commands está vacío. "
            "Se recomienda mantener al menos los comandos críticos bloqueados."
        )
    logging.info(f"✅ Sandbox config: mode={settings.sandbox_mode}, timeout={settings.sandbox_timeout_seconds}s")

    # 7. Validar config EmbeddingOptimizer
    if settings.embedding_cache_max_size < 500:
        logging.warning(
            f"⚠️ EMBEDDING CACHE: tamaño pequeño ({settings.embedding_cache_max_size}). "
            "Se recomienda ≥1000 para mejor hit rate."
        )
    if settings.query_cache_ttl_seconds < 120:
        logging.warning(
            f"⚠️ QUERY CACHE: TTL muy corto ({settings.query_cache_ttl_seconds}s). "
            "Se recomienda ≥300s para queries frecuentes."
        )
    logging.info(
        f"✅ EmbeddingOptimizer config: "
        f"cache={settings.embedding_cache_max_size}, "
        f"batch={settings.embedding_batch_size}, "
        f"percentile={settings.similarity_percentile}"
    )

    # 8. Validar concurrencia LLM
    if settings.ollama_max_concurrent_requests > 2:
        logging.warning(
            f"⚠️ CONCURRENCIA: ollama_max_concurrent_requests={settings.ollama_max_concurrent_requests}. "
            "Valores >2 pueden saturar la GPU compartida y causar latencias de 60-120s. "
            "Recomendado: 2 para GPU única."
        )
    logging.info(
        f"✅ LLM Concurrency: max_concurrent={settings.ollama_max_concurrent_requests}"
    )

    # 9. Validar Vision models
    vision_model = settings.llm_vision_model
    vision_selector = settings.vision_model_selector
    logging.info(
        f"✅ Vision: principal={vision_model} (timeout={settings.ollama_timeout_vision}s), "
        f"selector={vision_selector} — modelo legacy ELIMINADO por fallo silencioso"
    )

    # 10. Validar Jarvis Mode
    logging.info(
        f"✅ Jarvis Mode: default={settings.jarvis_mode_default}, "
        f"wake_word='{settings.jarvis_wake_word}', "
        f"tts_auto={settings.jarvis_tts_auto_speak}"
    )


# Ejecutar validación al importar (solo en producción, no en testing)
if __name__ != "__main__":
    try:
        _validate_configuration()
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 settings.py:891] excepción degradada (intencional): {_d2e}")
