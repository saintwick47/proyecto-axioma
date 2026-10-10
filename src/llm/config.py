#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v2026 — Configuración de Modelos LLM [D3]
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/llm/config.py
# Autor: SaintWick
# Fecha: 2026-04-20
# Propósito: Configuración específica por modelo y tarea
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO v0.2.0:
# - ModelConfig.copy() → dataclasses.replace() (dataclass no tiene .copy())
# - Código funcional para get_model_for_task()
# - Eliminada referencia a qwen2.5-coder:32b (no instalado)
# - CODE_MODEL centralizado: settings.llm_code_model (qwen2.5-coder:7b)
# ═══════════════════════════════════════════════════════════════
# ✅ ACTUALIZADO v0.1.0-uncensored:
# - Migración completa a tags abliterated/uncensored verificados en Ollama
# ═══════════════════════════════════════════════════════════════
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field, replace
from pathlib import Path
import yaml
import logging
from config.settings import settings
from config.paths import Paths
logger = logging.getLogger(__name__)
# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

def _log_event(category: str, message: str, level: str = "INFO"):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 config.py:43] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str):
    if not LOGGER_AVAILABLE:
        logger.error(f"llm_config - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context,
                          function_name=f"llm_config.{context}")
    except Exception:
        logger.error(f"llm_config - {context}: {error}")

# ═══════════════════════════════════════════════════════════
# ✅ v0.2.0: MODELO DE CÓDIGO ÚNICO
# ═══════════════════════════════════════════════════════════
# ✅ 2026-10-10: qué tareas pertenecen a cada rol del `.env` (decisión del usuario, 2026-10-10).
# Las demás tareas sólo declaran temperatura en `models.yaml` y ya resolvían con el `.env`.
TAREAS_DE_CODIGO: tuple = ("code_generation", "code_review", "code_correction", "code_explanation")
TAREAS_DE_CHAT: tuple = ("general_chat", "complex_reasoning")

CODE_MODEL:    str = settings.llm_code_model
DEFAULT_MODEL: str = settings.llm_default_model
VISION_MODEL:  str = settings.llm_vision_model

@dataclass
class ModelConfig:
    """Configuración específica para un modelo."""
    name: str
    task_types: List[str] = field(default_factory=list)
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout: int = 25
    context_window: int = 8192
    priority: int = 1  # 1 = highest
    fallback: Optional[str] = None
    # ✅ [D3]: None = usar settings.max_reasoning_tokens global
    max_reasoning_tokens: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        """Crea instancia desde dict."""
        return cls(
            name=data.get("name", "unknown"),
            task_types=data.get("task_types", []),
            temperature=data.get("temperature", 0.7),
            max_tokens=data.get("max_tokens", 2048),
            timeout=data.get("timeout", 25),
            context_window=data.get("context_window", 8192),
            priority=data.get("priority", 1),
            fallback=data.get("fallback"),
            max_reasoning_tokens=data.get("max_reasoning_tokens"),
        )

@dataclass
class TaskModelMapping:
    """Mapeo de una tarea a un modelo, con temperatura OPCIONAL.

    ═══════════════════════════════════════════════════════════════
    📏 REGLA DE PRECEDENCIA DE TEMPERATURA (v0.6.9v — escrita y testeada)
    ═══════════════════════════════════════════════════════════════
    De más específico a más general; gana el primero que esté definido:

      1. `task_mappings.<tarea>.temperature`   ← por TAREA (la más específica)
      2. `task_mappings.<tarea>` sin modelo, SOLO `temperature`
         (va a `LLMConfig.task_temperatures`)  ← también por TAREA; no puede
         coexistir con la 1 (un mapeo con modelo y una entrada solo-temperatura
         para la misma tarea son configuración ambigua → el validador la rechaza)
      3. `models.<modelo>.temperature`         ← por MODELO (default del modelo)
      4. `0.7`                                 ← default del código

    ⚠️ POR QUÉ ESTÁ ESCRITA: `temperature` era `float = 0.7`, así que un mapeo
    **sin** `temperature:` pisaba en silencio la temperatura declarada en
    `models.<modelo>` (el caso real: declarar `temperature: 0.2` en `qwen3:8b`
    no tenía efecto para `general_chat`/`complex_reasoning`, porque esos mapeos
    existen y forzaban 0.7). Ahora `temperature=None` significa **heredar del
    modelo** y el override por tarea solo se aplica si está declarado.
    Ver `tests/test_axioma_19_mi_hard.py` (regla enforceada).
    """
    task_type: str
    primary_model: str
    fallback_models: List[str] = field(default_factory=list)
    temperature: Optional[float] = None

    @classmethod
    def from_dict(cls, task_type: str, data: Dict[str, Any]) -> "TaskModelMapping":
        """Crea instancia desde dict.

        ✅ v0.6.9v (ISSUE-028): se aceptan AMBAS convenciones de claves.
        `config/models.yaml` escribe `preferred_model` / `fallback_model`, pero
        este loader solo leía `primary` / `fallbacks` → **todos** los
        `preferred_model` se descartaban en silencio y cada tarea mapeada caía al
        default `CODE_MODEL`. Impacto real medido: `complex_reasoning` (que debe
        usar `qwen3:8b`) corría en `qwen2.5-coder:7b`, y `vision_analysis` (que
        debe usar `qwen3-vl:4b`) también en el modelo de código.
        `primary`/`fallbacks` siguen teniendo prioridad si están presentes.

        ✅ v0.6.9v (regla de precedencia): `temperature` ausente queda en `None`
        (= heredar del modelo), NO en un 0.7 hardcodeado que pisaba al modelo.
        """
        primary = (data.get("primary") or data.get("preferred_model")
                   or CODE_MODEL)
        fallbacks = data.get("fallbacks")
        if not fallbacks:
            fb = data.get("fallback_model") or data.get("fallback")
            # "" (sin fallback, p.ej. visión) → lista vacía, no [""]
            fallbacks = [fb] if isinstance(fb, str) and fb.strip() else []
            if isinstance(fb, list):
                fallbacks = [f for f in fb if f]
        temp = data.get("temperature")
        return cls(
            task_type=task_type,
            primary_model=primary,
            fallback_models=fallbacks,
            temperature=float(temp) if isinstance(temp, (int, float)) else None,
        )

class LLMConfig:
    """
    Gestor de configuración para modelos LLM.
    Carga configuración desde:
    1. config/models.yaml (primario)
    2. Settings defaults (fallback)
    3. Hardcoded defaults (último recurso)

    Example:
    >>> config = LLMConfig()
    >>> model = config.get_model_for_task("code_generation")
    >>> print(model.name)  # qwen2.5-coder:7b
    """
    DEFAULT_MODELS = {
        CODE_MODEL: ModelConfig(
            name=CODE_MODEL,
            task_types=["code_generation", "code_review", "code_explanation", "code_debugging"],
            temperature=0.2,
            timeout=420,
            priority=1,
        ),
        DEFAULT_MODEL: ModelConfig(
            name=DEFAULT_MODEL,
            task_types=["chat", "reasoning", "summarization", "general", "complex_reasoning", "analysis"],
            temperature=0.7,
            timeout=45,
            priority=1,
        ),
        VISION_MODEL: ModelConfig(
            name=VISION_MODEL,
            task_types=["vision", "image_analysis", "ocr"],
            temperature=0.5,
            timeout=90,
            priority=1,
        ),
        "BAAI/bge-m3": ModelConfig(
            name="BAAI/bge-m3",
            task_types=["embeddings", "semantic_search"],
            temperature=0.0,
            timeout=10,
            priority=1,
        ),
    }

    def __init__(self, config_path: Optional[Path] = None):
        """
        Inicializa configuración LLM.
        Args:
            config_path: Ruta a models.yaml (usa default si None)
        """
        # CORREGIDO: Paths.MODELS_CONFIG → Paths.CONFIG / "models.yaml"
        self.config_path = config_path or Paths.CONFIG / "models.yaml"
        self.models: Dict[str, ModelConfig] = {}
        self.task_mappings: Dict[str, TaskModelMapping] = {}
        # ✅ v0.6.9v (P0 informe): temperatura por tarea SIN tocar el modelo.
        # `task_mappings.<task>` acepta 3 formas:
        #   1) con claves de modelo  → mapea modelo (y opcional `temperature`)
        #   2) SOLO `temperature`     → ajusta temperatura, deja el modelo como
        #      lo resolvería por defecto (antes era imposible: `from_dict`
        #      default-eaba `primary_model` a CODE_MODEL y una entrada "solo
        #      temperatura" habría ruteado TODO al modelo de código).
        #   3) vacío                  → se ignora
        self.task_temperatures: Dict[str, float] = {}
        self.fallback_chain: List[str] = []
        self._load_config()
        _log_event("CONFIG", f"LLMConfig initialized: {len(self.models)} models, {len(self.task_mappings)} task mappings")
        logger.info(f"LLMConfig loaded: {len(self.models)} models, {len(self.task_mappings)} task mappings")

    def _load_config(self) -> None:
        """Carga configuración desde YAML."""
        # Start with defaults
        self.models = dict(self.DEFAULT_MODELS)
        # Load from YAML if exists
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                # Parse models section
                if "models" in data:
                    for model_name, model_data in data["models"].items():
                        if isinstance(model_data, dict) and model_data.get("enabled", True):
                            model_data["name"] = model_name
                            self.models[model_name] = ModelConfig.from_dict(model_data)
                # Parse task_mappings section
                if "task_mappings" in data:
                    for task_type, mapping_data in data["task_mappings"].items():
                        if not isinstance(mapping_data, dict):
                            continue
                        has_model_keys = any(
                            k in mapping_data for k in
                            ("primary", "preferred_model", "fallbacks",
                             "fallback_model", "fallback")
                        )
                        if not has_model_keys:
                            # Entrada SOLO temperatura (o vacía → ignorada)
                            temp = mapping_data.get("temperature")
                            if isinstance(temp, (int, float)):
                                self.task_temperatures[task_type] = float(temp)
                            continue
                        self.task_mappings[task_type] = TaskModelMapping.from_dict(
                            task_type, mapping_data
                        )
                # Parse fallback_chain
                if "fallback_chain" in data:
                    self.fallback_chain = data["fallback_chain"]
                logger.info(f"Loaded config from {self.config_path}")
                _log_event("CONFIG", f"LLMConfig loaded from {self.config_path}: {len(self.models)} models")
            except Exception as e:
                logger.warning(f"Failed to load config: {e}, using defaults")
                _log_error(e, "_load_config")
        # Ensure settings models are included
        self._sync_with_settings()

    def _sync_with_settings(self) -> None:
        """Sincroniza con settings.py."""
        # ✅ 2026-10-10 (lo pidió el usuario; verificado con medición): la elección del `.env` MANDA.
        # MEDIDO: `get_model_for_task` mira PRIMERO `task_mappings`, así que los modelos fijos de
        # `models.yaml` ganaban y el modelo elegido en la pantalla no cambiaba NINGUNA decisión en
        # ejecución (sólo el preflight). Acá se reescribe sólo el MODELO de las tareas de cada rol —la
        # temperatura de cada tarea queda intacta— y sólo si el usuario configuró ese rol: con el `.env`
        # por defecto los valores coinciden con `models.yaml`, así que nada cambia para quien no tocó nada.
        # Se leen de `settings` EN EL MOMENTO (no las constantes del módulo, que son una foto del arranque):
        # así vale también si el modelo se elige con AXIOMA andando.
        codigo = str(getattr(settings, "llm_code_model", "") or "").strip()
        chat = str(getattr(settings, "llm_default_model", "") or "").strip()
        respaldo_codigo = str(getattr(settings, "llm_code_model_fallback", "") or "").strip()
        for nombre, tipo, temperatura in ((codigo, "code", 0.3), (chat, "default", 0.7)):
            if nombre and nombre not in self.models:
                self.models[nombre] = ModelConfig(name=nombre, task_types=[tipo],
                                                  temperature=temperatura,
                                                  timeout=settings.ollama_timeout)
        for tarea in TAREAS_DE_CODIGO:
            if codigo and tarea in self.task_mappings:
                self.task_mappings[tarea].primary_model = codigo
        for tarea in TAREAS_DE_CHAT:
            if chat and tarea in self.task_mappings:
                self.task_mappings[tarea].primary_model = chat
        if respaldo_codigo and codigo in self.models:
            # El respaldo de código atiende a las tareas de código (decisión del usuario): va como
            # `fallback` del modelo de código, así `get_fallback_chain()` lo devuelve para `code_*`.
            self.models[codigo].fallback = respaldo_codigo
        # Ensure default model exists
        if settings.llm_default_model not in self.models:
            self.models[settings.llm_default_model] = ModelConfig(
                name=settings.llm_default_model,
                task_types=["default"],
                temperature=0.7,
                timeout=settings.ollama_timeout,
            )
        # Ensure code model exists (CODE_MODEL = settings.llm_code_model)
        if CODE_MODEL not in self.models:
            self.models[CODE_MODEL] = ModelConfig(
                name=CODE_MODEL,
                task_types=["code"],
                temperature=0.3,
                timeout=settings.ollama_timeout_code,
            )
        # Ensure vision model exists
        if settings.llm_vision_model not in self.models:
            self.models[settings.llm_vision_model] = ModelConfig(
                name=settings.llm_vision_model,
                task_types=["vision"],
                temperature=0.5,
                timeout=settings.ollama_timeout_vision,
            )
        # Ensure embed model exists
        if settings.llm_embed_model not in self.models:
            self.models[settings.llm_embed_model] = ModelConfig(
                name=settings.llm_embed_model,
                task_types=["embeddings"],
                temperature=0.0,
                timeout=10,
            )
        # Ensure judge model exists (only if configured — llm_judge_model="" means disabled)
        if settings.llm_judge_model and settings.llm_judge_model not in self.models:
            self.models[settings.llm_judge_model] = ModelConfig(
                name=settings.llm_judge_model,
                task_types=["validation"],
                temperature=0.1,
                timeout=25,
            )
        # ✅ [D3]: health_check CODE_MODEL — solo loguear, no cambiar default
        try:
            import socket
            from urllib.parse import urlparse
            parsed = urlparse(settings.ollama_host)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 11434
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
        except Exception as _hc_err:
            logger.warning(
                f"[D3] CODE_MODEL health_check falló ({CODE_MODEL}): {_hc_err} "
                f"— usar {settings.llm_fallback_1} si hay error de conexión"
            )

    def get_model_for_task(self, task_type: str) -> ModelConfig:
        """
        Obtiene modelo configurado para una tarea.
        ✅ CORREGIDO v0.2.0: Usa dataclasses.replace() en lugar de .copy()
        porque ModelConfig es una dataclass, no un dict.
        ✅ v0.6.9v: aplica `self.task_temperatures` (entradas de
        `task_mappings.<task>` que SOLO declaran temperatura). Así una tarea sin
        mapeo de modelo puede tener su temperatura propia sin arrastrar el
        default CODE_MODEL.
        ✅ P1.3 (plan_fiabilidad, ISSUE-069, 2026-09-14): el ModelConfig final
        de cada rama pasa por _apply_feedback_temperature() antes de
        devolverse — detrás de flag (ver _apply_feedback_temperature).
        Args:
            task_type: Tipo de tarea (de TaskType enum)
        Returns:
            ModelConfig para la tarea
        """
        temp_override = self.task_temperatures.get(task_type)
        # Check task mappings first
        if task_type in self.task_mappings:
            mapping = self.task_mappings[task_type]
            if mapping.primary_model in self.models:
                # ✅ CORREGIDO: Usar replace() para crear copia inmutable
                model = self.models[mapping.primary_model]
                # 📏 Regla de precedencia: la temperatura del MAPEO solo se
                # aplica si está declarada; si es None se respeta la del MODELO
                # (`models.<x>.temperature`). Antes se forzaba 0.7 y pisaba en
                # silencio la temperatura del modelo.
                if mapping.temperature is None:
                    model_config = self._resolve_reasoning_tokens(replace(model))
                else:
                    model_config = self._resolve_reasoning_tokens(
                        replace(model, temperature=mapping.temperature))
                return self._apply_feedback_temperature(task_type, model_config)
        # Find model by task_type
        for model in self.models.values():
            if task_type in model.task_types:
                # ✅ CORREGIDO: Usar replace() para copia sin modificar
                if temp_override is not None:
                    model_config = self._resolve_reasoning_tokens(
                        replace(model, temperature=temp_override))
                else:
                    model_config = self._resolve_reasoning_tokens(replace(model))
                return self._apply_feedback_temperature(task_type, model_config)
        # Return default (CODE_MODEL para código, llm_default_model para resto)
        # ✅ 2026-10-10: el modelo de código se lee de `settings` EN EL MOMENTO (la constante `CODE_MODEL`
        # es una foto del arranque): así el modelo elegido en la pantalla vale también para tareas de
        # código que no tengan mapeo propio.
        default_name = (str(getattr(settings, "llm_code_model", "") or "").strip() or CODE_MODEL
                        if "code" in task_type.lower() else settings.llm_default_model)
        model = self.models.get(default_name, list(self.models.values())[0])
        if temp_override is not None:
            model_config = self._resolve_reasoning_tokens(
                replace(model, temperature=temp_override))
        else:
            model_config = self._resolve_reasoning_tokens(replace(model))
        return self._apply_feedback_temperature(task_type, model_config)

    def _apply_feedback_temperature(self, task_type: str, model: "ModelConfig") -> "ModelConfig":
        """
        ✅ P1.3 (plan_fiabilidad, ISSUE-069, 2026-09-14): punto de enganche
        entre el loop de aprendizaje (ParameterOptimizer, feedback_loop*) y la
        temperatura real que usa cada llamada al LLM. Antes de esto,
        get_optimized_parameter()/get_current_parameter() no tenían NINGÚN
        consumidor real — el sistema medía feedback y ajustaba un valor que
        nadie leía (ver docs/PLAN_FIABILIDAD.md §3.2).

        Detrás de flag (default OFF — R8: nada se activa sin medir). El
        cálculo del ajuste (async, adjust_parameter()) NO se dispara acá —
        se dispara aparte, en el learning loop periódico de
        dispatcher_process.py; esta función solo LEE el último valor
        calculado (síncrono, sin I/O).

        Import diferido a propósito: src/llm/config.py se carga muy temprano
        (ModelRouter.__init__ instancia LLMConfig()) y
        src.core.feedback_loop hace I/O de DB en su __init__
        (_load_metrics_from_db) — evitamos ese costo/ciclo salvo que el flag
        esté prendido y de verdad se use. Mismo patrón que
        RuntimeMode.allowed_permission_level() en src/domain/types.py.
        """
        if not getattr(settings, "feedback_temperature_tuning_enabled", False):
            return model
        try:
            from src.core.feedback_loop import get_feedback_loop_sync
            loop = get_feedback_loop_sync()
            # ✅ FIX (revisión 2026-09-14): get_current_parameter() devuelve el
            # DEFAULT (settings.default_temperature) cuando aún no hay ajuste
            # aprendido, así que `tuned is None` nunca protegía y se pisaban las
            # temperaturas por tarea de models.yaml (0.10–0.30) con 0.7 apenas
            # se activaba el flag. get_adjusted_value() devuelve None salvo que
            # exista un ajuste REAL para esa tarea.
            if not getattr(loop, "enable_optimization", False) or loop.param_optimizer is None:
                return model
            tuned = loop.param_optimizer.get_adjusted_value("temperature", task_type)
        except Exception as _d2e:
            from src.utils.degradation import log_degraded
            log_degraded(logger, _d2e, "LLMConfig._apply_feedback_temperature")
            return model
        if tuned is None:
            return model
        return replace(model, temperature=float(tuned))

    def _resolve_reasoning_tokens(self, model: "ModelConfig") -> "ModelConfig":
        """[D3] Propaga settings.max_reasoning_tokens si el modelo no tiene valor propio."""
        if model.max_reasoning_tokens is None:
            global_default = getattr(settings, "max_reasoning_tokens", 2048)
            return replace(model, max_reasoning_tokens=global_default)
        return model

    def get_temperature_for_task(self, task_type: str) -> float:
        """Obtiene temperatura recomendada para tarea."""
        model = self.get_model_for_task(task_type)
        return model.temperature

    def get_fallback_chain(self, model_name: str) -> List[str]:
        """
        Obtiene cadena de fallback para un modelo.
        Args:
            model_name: Nombre del modelo
        Returns:
            Lista de modelos en orden de fallback
        """
        chain = [model_name]
        if model_name in self.models:
            current = self.models[model_name]
            while current.fallback:
                chain.append(current.fallback)
                if current.fallback in self.models:
                    current = self.models[current.fallback]
                else:
                    break
        # Add global fallback chain
        for fallback in self.fallback_chain:
            if fallback not in chain:
                chain.append(fallback)
        return chain

    def list_models(self) -> List[Dict[str, Any]]:
        """Lista todos los modelos configurados."""
        return [
            {
                "name": m.name,
                "task_types": m.task_types,
                "temperature": m.temperature,
                "timeout": m.timeout,
                "priority": m.priority,
            }
            for m in self.models.values()
        ]

    def get_available_models(self) -> List[str]:
        """Lista nombres de modelos disponibles."""
        return list(self.models.keys())

    def validate_models(self) -> Dict[str, bool]:
        """
        Valida que los modelos configurados existen en Ollama.
        Returns:
            Dict {model_name: exists}
        """
        from .client import OllamaClient
        results = {}
        try:
            client = OllamaClient()
            ollama_models = client.list_models()
            ollama_names = {m["name"] for m in ollama_models}
            for model_name in self.models.keys():
                results[model_name] = model_name in ollama_names
            client.close()
        except Exception as e:
            logger.warning(f"Failed to validate models: {e}")
            for model_name in self.models.keys():
                results[model_name] = False
        return results

    def reload(self) -> None:
        """Recarga configuración desde disco."""
        self.models.clear()
        self.task_mappings.clear()
        self.fallback_chain.clear()
        self._load_config()
        logger.info("LLMConfig reloaded")
