#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Módulo de Agentes Especializados
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-18
# Propósito: Exports públicos + factory para agentes especializados
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: Orchestrator eliminado completamente — usa src.core.dispatcher

from typing import Optional, Any, Dict, List

__version__ = "0.0.8"
__author__ = "SaintWick"

# ✅ CORREGIDO: Exports actualizados — Orchestrator eliminado
__all__ = [
    "BaseAgent",
    # "Orchestrator",  # ✅ ELIMINADO: Reemplazado por src.core.dispatcher
    "CoderAgent",
    "ResearcherAgent",
    "SearcherAgent",
    "AnalystAgent",
    "ValidatorAgent",
]


def __getattr__(name: str) -> Any:
    """
    Lazy loading de componentes del módulo Agents.

    Args:
        name: Nombre del componente a importar

    Returns:
        Clase o función solicitada

    Raises:
        AttributeError: Si el nombre no existe en el módulo
    """
    if name == "BaseAgent":
        from .base import BaseAgent
        return BaseAgent

    # ✅ ELIMINADO: Orchestrator — usar src.core.dispatcher.Dispatcher en su lugar
    # elif name == "Orchestrator":
    #     from .orchestrator import Orchestrator
    #     return Orchestrator

    elif name == "CoderAgent":
        from .coder import CoderAgent
        return CoderAgent

    elif name == "ResearcherAgent":
        from .researcher import ResearcherAgent
        return ResearcherAgent

    elif name == "SearcherAgent":
        from .searcher import SearcherAgent
        return SearcherAgent

    elif name == "AnalystAgent":
        from .analyst import AnalystAgent
        return AnalystAgent

    elif name == "ValidatorAgent":
        from .validator import ValidatorAgent
        return ValidatorAgent

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_agent(agent_type: str, **kwargs) -> Any:
    """
    Factory function para crear agentes por tipo.

    Args:
        agent_type: Tipo de agente (coder, researcher, searcher, analyst, validator)
        **kwargs: Argumentos específicos del agente

    Returns:
        Instancia del agente solicitado

    Raises:
        ValueError: Si el tipo de agente no es reconocido

    Example:
        >>> agent = create_agent("coder", model="qwen2.5-coder:7b")
        >>> response = agent.execute("Crea una función en Python")
    """
    # ✅ ISSUE-098: los agentes se resuelven por el lazy `__getattr__` del paquete
    # (PEP 562). Antes esta función usaba los nombres PELADOS, que no existen en el
    # módulo ⇒ `NameError` al llamarla (nadie la llamaba: bug latente).
    import sys as _sys
    _mod = _sys.modules[__name__]
    agents = {
        "coder": _mod.CoderAgent,
        "researcher": _mod.ResearcherAgent,
        "searcher": _mod.SearcherAgent,
        "analyst": _mod.AnalystAgent,
        "validator": _mod.ValidatorAgent,
    }

    if agent_type not in agents:
        available = list(agents.keys())
        raise ValueError(f"Unknown agent type: {agent_type}. Available: {available}")

    return agents[agent_type](**kwargs)


def list_available_agents() -> List[Dict[str, Any]]:
    """
    Lista todos los agentes disponibles con sus capacidades.

    Returns:
        Lista de dicts con información de cada agente
    """
    return [
        {"type": "coder", "name": "CoderAgent", "description": "Generación y corrección de código"},
        {"type": "researcher", "name": "ResearcherAgent", "description": "Búsqueda web y síntesis (completo)"},
        {"type": "searcher", "name": "SearcherAgent", "description": "Búsqueda web limpia (1 llamada LLM)"},
        {"type": "analyst", "name": "AnalystAgent", "description": "Análisis de documentos y datos"},
        {"type": "validator", "name": "ValidatorAgent", "description": "Validación con Chain of Thought"},
        # {"type": "orchestrator", "name": "Orchestrator", "description": "Orquestador de agentes (legacy)"},  # ✅ ELIMINADO
    ]


def health_check() -> Dict[str, bool]:
    """
    Verifica estado de inicialización de todos los agentes.

    Returns:
        Dict con estado de cada componente (True = OK, False = Error)
    """
    results = {
        # "orchestrator": False,  # ✅ ELIMINADO: Reemplazado por dispatcher
        "coder": False,
        "researcher": False,
        "searcher": False,
        "analyst": False,
        "validator": False,
    }

    try:
        from .coder import CoderAgent
        CoderAgent()
        results["coder"] = True
    except Exception:
        results["coder"] = False

    try:
        from .researcher import ResearcherAgent
        ResearcherAgent()
        results["researcher"] = True
    except Exception:
        results["researcher"] = False

    try:
        from .searcher import SearcherAgent
        SearcherAgent()
        results["searcher"] = True
    except Exception:
        results["searcher"] = False

    try:
        from .analyst import AnalystAgent
        AnalystAgent()
        results["analyst"] = True
    except Exception:
        results["analyst"] = False

    try:
        from .validator import ValidatorAgent
        ValidatorAgent()
        results["validator"] = True
    except Exception:
        results["validator"] = False

    return results


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — ELIMINACIÓN Orchestrator
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                                  | Razón                        |
# |-------|-----------------------------------------|--------------------------------|
# | ~16   | ✅ "Orchestrator" comentado en __all__  | Módulo eliminado             |
# | ~42-44| ✅ __getattr__: caso Orchestrator eliminado | Previene ImportError    |
# | ~72   | ✅ list_available_agents: Orchestrator comentado | Documentación actualizada |
# | ~84   | ✅ health_check: orchestrator eliminado | Check limpio                |
# | TODAS | ✅ Sin otros cambios                    | Funcionalidad preservada     |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — 185 LÍNEAS — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
