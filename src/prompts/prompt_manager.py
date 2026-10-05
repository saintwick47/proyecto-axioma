#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# prompt_manager.py - Orquestador de 6 Layers de Prompts
# Ruta: src/prompts/prompt_manager.py
# Autor: SaintWick
# Versión: 0.1.0
#
# Orquesta los 6 layers del sistema de prompts de AXIOMA:
# identidad, tarea, herramientas, contexto, formato y reflexión.
# Carga templates YAML por agente y permite reintentos con crítica.
# ═══════════════════════════════════════════════════════════════
"""
Orquestador central del sistema de prompts de AXIOMA.

Compone los 6 layers en un system prompt final coherente,
cargando la configuración desde templates YAML por agente
y mezclándola con contexto dinámico de runtime.

Layers:
    0. base_prompts.py       → Identidad y reglas constitucionales
    1. task_prompts.py       → Clasificación y objetivo de tarea
    2. tool_prompts.py       → Herramientas disponibles y permisos
    3. context_injection.py  → Memoria y estado de sesión
    4. output_formats.py     → Formato de salida esperado
    5. reflection_triggers.py → Auto-corrección y reflexión
"""
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import yaml

from .base_prompts import IdentityLayer, identity_layer
from .task_prompts import TaskLayer
from .tool_prompts import ToolLayer
from .context_injection import ContextLayer
from .output_formats import OutputLayer, OutputStyle
from .reflection_triggers import ReflectionLayer

# ── PromptOptimizer — import seguro (Fase 5) ────────────────────────────────
try:
    from .prompt_optimizer import get_prompt_optimizer, PromptOptimizer
    _OPTIMIZER_AVAILABLE = True
except ImportError:
    get_prompt_optimizer = None  # type: ignore[assignment]
    _OPTIMIZER_AVAILABLE = False

logger = logging.getLogger(__name__)

# Ruta base de templates YAML
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Caché de templates cargados (evita I/O repetido)
_TEMPLATE_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_template(agent_type: str) -> Dict[str, Any]:
    """
    Carga el template YAML de un agente, con caché en memoria.

    Args:
        agent_type: Nombre del agente (ej: 'coder', 'validator')

    Returns:
        Dict con el template cargado, o dict vacío si no existe
    """
    if agent_type in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[agent_type]

    template_path = TEMPLATES_DIR / f"{agent_type}.yaml"
    if not template_path.exists():
        logger.debug(f"Template no encontrado: {template_path} — usando defaults")
        return {}

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _TEMPLATE_CACHE[agent_type] = data
        logger.debug(f"Template cargado: {agent_type}.yaml")
        return data
    except yaml.YAMLError as e:
        logger.error(f"Error parseando {agent_type}.yaml: {e}")
        return {}


def invalidate_cache(agent_type: Optional[str] = None) -> None:
    """
    Invalida el caché de templates (útil después de editar YAMLs).

    Args:
        agent_type: Si se especifica, solo invalida ese agente.
                    Si es None, invalida todo el caché.
    """
    global _TEMPLATE_CACHE
    if agent_type:
        _TEMPLATE_CACHE.pop(agent_type, None)
    else:
        _TEMPLATE_CACHE.clear()
    logger.debug(f"Caché de templates invalidado: {agent_type or 'todos'}")


class PromptManager:
    """
    Orquestador central de los 6 layers del sistema de prompts.

    Uso básico:
        pm = PromptManager("coder")
        system_prompt = pm.build(
            task_type="CODE_GENERATION",
            context=ContextLayer.from_memory_gateway(...),
        )

    Uso con reintento:
        system_prompt = pm.build_for_retry(
            task_type="CODE_GENERATION",
            previous_score=0.45,
            critique="Faltan docstrings en todas las funciones",
        )
    """

    # Mapeo de agente → ToolLayer factory
    _TOOL_FACTORIES = {
        "coder":      ToolLayer.for_coder,
        "validator":  ToolLayer.for_validator,
        "searcher":   ToolLayer.for_searcher,
        "researcher": ToolLayer.for_researcher,
        "analyst":    ToolLayer.for_analyst,
    }

    # Mapeo de agente → OutputLayer factory
    _OUTPUT_FACTORIES = {
        "coder":      OutputLayer.for_code,
        "validator":  OutputLayer.for_validation,
        "searcher":   OutputLayer.for_chat,
        "researcher": OutputLayer.for_research,
        "analyst":    OutputLayer.for_analysis,
    }

    def __init__(self, agent_type: str):
        """
        Inicializa el PromptManager para un agente específico.

        Args:
            agent_type: Tipo de agente ('coder', 'validator', etc.)
        """
        self.agent_type = agent_type
        self.template = _load_template(agent_type)
        self._layer0 = identity_layer
        self._optimizer = get_prompt_optimizer() if _OPTIMIZER_AVAILABLE else None
        logger.debug(f"PromptManager inicializado para: {agent_type}")

    def build(
        self,
        task_type: str = "GENERAL_CHAT",
        context: Optional[ContextLayer] = None,
        tool_layer: Optional[ToolLayer] = None,
        output_layer: Optional[OutputLayer] = None,
        reflection: Optional[ReflectionLayer] = None,
        user_intent: Optional[str] = None,
        complexity_hint: Optional[str] = None,
        include_reflection_precheck: bool = True,
        active_tools: Optional[List[str]] = None,
    ) -> str:
        """
        Construye el system prompt completo con los 6 layers.

        Args:
            task_type: Tipo de tarea clasificada
            context: Layer 3 con contexto de memoria/sesión
            tool_layer: Layer 2 custom (usa default del agente si None)
            output_layer: Layer 4 custom (usa default del agente si None)
            reflection: Layer 5 custom (usa standard si None)
            user_intent: Descripción de la intención del usuario
            complexity_hint: 'simple' | 'medium' | 'complex'
            include_reflection_precheck: Si incluir auto-revisión pre-respuesta
            active_tools: Lista de herramientas realmente disponibles

        Returns:
            System prompt completo como string
        """
        # ✅ FIX: se sacó la llamada a self._optimizer.get_best_version() que
        # había acá — se ejecutaba en cada build() (costo real en cada
        # request) pero el resultado (active_version) nunca se usaba para
        # variar el contenido del prompt, solo se logueaba. record_quality()
        # más abajo sigue intacto — esos datos (avg_score, retry_rate) sí
        # tienen valor propio como métrica, aunque el auto-switch de versión
        # no esté implementado. Si en el futuro se arma soporte real de
        # variantes de prompt por versión, la selección vuelve acá.
        sections = []

        # ── Layer 0: Identidad + reglas constitucionales ──────────────
        layer0_extra_rules = []
        if self.template.get("constitutional", {}).get("rules"):
            layer0_extra_rules = self.template["constitutional"]["rules"]

        l0 = self._layer0
        if layer0_extra_rules:
            l0 = self._layer0.with_rules(*layer0_extra_rules)
        sections.append(l0.build())

        # ── Layer 1: Clasificación de tarea ───────────────────────────
        l1 = TaskLayer.from_task_type(
            task_type=task_type,
            user_intent=user_intent,
            complexity_hint=complexity_hint,
        )
        l1_str = l1.build()
        if l1_str:
            sections.append(l1_str)

        # ── Layer 2: Herramientas ──────────────────────────────────────
        if tool_layer is None:
            factory = self._TOOL_FACTORIES.get(self.agent_type)
            tool_layer = factory() if factory else ToolLayer.minimal()
        if active_tools:
            tool_layer.active_tools = active_tools
        l2_str = tool_layer.build()
        if l2_str:
            sections.append(l2_str)

        # ── Layer 3: Contexto de memoria ──────────────────────────────
        l3 = context or ContextLayer.empty()
        l3_str = l3.build()
        if l3_str:
            sections.append(l3_str)

        # ── Layer 4: Formato de salida ─────────────────────────────────
        if output_layer is None:
            factory = self._OUTPUT_FACTORIES.get(self.agent_type)
            output_layer = factory() if factory else OutputLayer()

        # Inyectar formato desde template YAML si existe
        yaml_format = self.template.get("output_format", {}).get("structure")
        if yaml_format and output_layer.style not in (OutputStyle.JSON,):
            sections.append(f"FORMATO DE SALIDA:\n{yaml_format.strip()}")
        else:
            l4_str = output_layer.build()
            if l4_str:
                sections.append(l4_str)

        # ── Layer 5: Reflexión / auto-corrección ──────────────────────
        if reflection is None and include_reflection_precheck:
            reflection = ReflectionLayer.standard(self.agent_type)
        if reflection:
            l5_str = reflection.build()
            if l5_str:
                sections.append(l5_str)

        # ── Composición final ─────────────────────────────────────────
        separator = "\n" + "─" * 60 + "\n"
        prompt = separator.join(sections)

        logger.debug(
            f"PromptManager.build() → {self.agent_type} | task={task_type} | "
            f"layers={len(sections)} | chars={len(prompt)}"
        )
        return prompt

    def record_quality(
        self,
        task_type: str,
        score: float,
        was_retry: bool = False,
        version_id: Optional[str] = None,
    ) -> None:
        """
        Registra el score de calidad de una ejecución en el optimizer.
        Llamar después de recibir feedback del agente.

        Args:
            task_type:  Tipo de tarea ejecutada
            score:      Score de calidad (0.0-1.0)
            was_retry:  Si fue un reintento
            version_id: Versión usada (None = activa)
        """
        if self._optimizer is None:
            return
        self._optimizer.record_execution(
            agent_type=self.agent_type,
            task_type=task_type,
            score=score,
            was_retry=was_retry,
            version_id=version_id,
        )
        logger.debug(
            f"PromptManager.record_quality: {self.agent_type}/{task_type} "
            f"score={score:.2f} retry={was_retry}"
        )

    def get_optimizer_stats(self) -> dict:
        """Estadísticas del optimizer para este agente."""
        if self._optimizer is None:
            return {"available": False}
        return {
            "available": True,
            **self._optimizer.get_stats(agent_type=self.agent_type),
        }

    def build_for_retry(
        self,
        task_type: str,
        previous_score: float,
        critique: str,
        problem_type: str = "low_quality",
        problem_detail: Optional[str] = None,
        context: Optional[ContextLayer] = None,
        **kwargs: Any,
    ) -> str:
        """
        Construye el system prompt para un reintento con crítica del intento anterior.

        Args:
            task_type: Tipo de tarea
            previous_score: Score del intento anterior (0.0-1.0)
            critique: Descripción de qué falló
            problem_type: Clave del tipo de problema (ver IMPROVEMENT_TEMPLATES)
            problem_detail: Detalle adicional para el template de mejora
            context: Contexto de memoria/sesión
            **kwargs: Argumentos adicionales para build()

        Returns:
            System prompt con capa de reflexión/reintento activada
        """
        reflection = ReflectionLayer.on_retry(
            agent_type=self.agent_type,
            score=previous_score,
            critique=critique,
            problem_type=problem_type,
            detail=problem_detail,
        )
        return self.build(
            task_type=task_type,
            context=context,
            reflection=reflection,
            **kwargs,
        )

    def get_template_value(self, *keys: str, default: Any = None) -> Any:
        """
        Accede a un valor del template YAML con path de keys.

        Args:
            *keys: Path de claves (ej: 'output_format', 'max_tokens_hint')
            default: Valor por defecto si no existe

        Returns:
            Valor del template o default
        """
        val = self.template
        for key in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(key, default)
        return val

    def reload_template(self) -> None:
        """Recarga el template YAML desde disco (invalida caché del agente)."""
        invalidate_cache(self.agent_type)
        self.template = _load_template(self.agent_type)
        logger.info(f"Template recargado para: {self.agent_type}")


# ── Instancias singleton por agente (lazy init) ───────────────────────────────
_managers: Dict[str, PromptManager] = {}


def get_prompt_manager(agent_type: str) -> PromptManager:
    """
    Retorna o crea el PromptManager singleton para un agente.

    Args:
        agent_type: Tipo de agente

    Returns:
        Instancia de PromptManager para ese agente
    """
    if agent_type not in _managers:
        _managers[agent_type] = PromptManager(agent_type)
    return _managers[agent_type]
