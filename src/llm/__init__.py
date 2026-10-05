# AXIOMA Package
# Ruta: /ruta/a/axioma/src/llm/__init__.py
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.2.1 — Módulo LLM (Large Language Models)
# ═══════════════════════════════════════════════════════════════
#
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Cliente Ollama + Router de Modelos + Configuración
#
# Componentes:
#   - OllamaClient: Cliente asíncrono con circuit breaker + retry
#   - ModelRouter: Selección de modelo por TaskType + temperatura
#   - LLMConfig: Configuración específica por modelo
#
# Estado: Fase 2 — Pendiente de implementación completa
# ✅ v0.2.1 (2026-06-24): Eliminadas referencias a modelos obsoletos
#   (aletheia-3b:latest → qwen3:8b) en ejemplos y comentarios.
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any

__version__ = "0.2.1"
__author__ = "SaintWick"
__all__ = ["OllamaClient", "ModelRouter", "LLMConfig"]

# Imports diferidos para evitar circular dependencies
def __getattr__(name: str) -> Any:
    """Lazy loading de componentes del módulo LLM."""
    if name == "OllamaClient":
        from .client import OllamaClient
        return OllamaClient
    elif name == "ModelRouter":
        from .router import ModelRouter
        return ModelRouter
    elif name == "LLMConfig":
        from .config import LLMConfig
        return LLMConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_client(model: Optional[str] = None) -> Any:
    """
    Factory function para obtener cliente LLM configurado.

    Args:
        model: Nombre del modelo (usa default si None)

    Returns:
        Instancia de OllamaClient configurada

    Example:
        >>> client = get_client("qwen3:8b")
        >>> response = client.generate("Hola")
    """
    from config.settings import settings
    from .client import OllamaClient

    model_name = model or settings.llm_default_model
    return OllamaClient(model=model_name)


def health_check() -> dict:
    """
    Verifica estado de todos los modelos configurados.

    Returns:
        Dict con estado de cada modelo

    Example:
        >>> status = health_check()
        >>> print(status["qwen3:8b"])
        {"status": "ok", "latency_ms": 45.2}
    """
    from config.settings import settings
    from .client import OllamaClient

    models = [
        settings.llm_default_model,
        settings.llm_code_model,
        settings.llm_vision_model,
        settings.llm_embed_model,
    ]

    results = {}
    for model in models:
        try:
            client = OllamaClient(model=model)
            status = client.health_check()
            results[model] = {"status": "ok" if status else "error", "latency_ms": None}
        except Exception as e:
            results[model] = {"status": "error", "error": str(e)}

    return results
