import logging
#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/__init__.py
# AXIOMA v0.5 — Módulo de Memoria
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Sistema de memoria unificado (short + long + semantic)
#
# Componentes:
#   - MemoryGateway: Gateway unificado para todas las capas
#   - ShortTermMemory: Ventana conversacional (deque + TTL)
#   - LongTermMemory: SQLite para interacciones persistentes
#   - SemanticMemory: LanceDB para búsqueda vectorial (migrado de ChromaDB en v0.6.3)
#   - ContextWindowManager: Gestión de ventana de contexto (Fase 2)
#   - EmbeddingOptimizer: Optimización de embeddings (Fase 2)
#
# Estado: Fase 3 — Exports actualizados
# ═══════════════════════════════════════════════════════════════

from typing import Optional, Any, List, Dict
from datetime import datetime
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

__version__ = "0.3.0"
__author__ = "SaintWick"
__all__ = [
    "MemoryGateway",
    "ShortTermMemory",
    "LongTermMemory",
    "SemanticMemory",
    "ContextWindowManager",
    "EmbeddingOptimizer",
    "get_context_window_manager",
]


def __getattr__(name: str) -> Any:
    """Lazy loading de componentes del módulo Memory."""
    if name == "MemoryGateway":
        from .gateway import MemoryGateway
        return MemoryGateway
    elif name == "ShortTermMemory":
        from .short_term import ShortTermMemory
        return ShortTermMemory
    elif name == "LongTermMemory":
        from .long_term import LongTermMemory
        return LongTermMemory
    elif name == "SemanticMemory":
        from .semantic import SemanticMemory
        return SemanticMemory
    elif name == "ContextWindowManager":
        from .context_window import ContextWindowManager
        return ContextWindowManager
    elif name == "EmbeddingOptimizer":
        from .embedding_optimizer import EmbeddingOptimizer
        return EmbeddingOptimizer
    elif name == "get_context_window_manager":
        from .context_window import get_context_window_manager
        return get_context_window_manager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_gateway(
    session_id: Optional[str] = None,
    enable_short: bool = True,
    enable_long: bool = True,
    enable_semantic: bool = True,
) -> Any:
    """
    Factory function para crear MemoryGateway configurado.

    Args:
        session_id: ID de sesión única
        enable_short: Habilitar memoria a corto plazo
        enable_long: Habilitar memoria a largo plazo
        enable_semantic: Habilitar memoria semántica

    Returns:
        Instancia de MemoryGateway

    Example:
        >>> gateway = create_gateway(session_id="abc123")
        >>> gateway.add_message("user", "Hola")
    """
    from .gateway import MemoryGateway
    return MemoryGateway(
        session_id=session_id,
        enable_short=enable_short,
        enable_long=enable_long,
        enable_semantic=enable_semantic,
    )


def health_check() -> Dict[str, bool]:
    """
    Verifica estado de todos los componentes de memoria.

    Returns:
        Dict con estado de cada componente

    Example:
        >>> status = health_check()
        >>> print(status["short_term"])
        True
    """
    results = {
        "short_term": True,
        "long_term": False,
        "semantic": False,
        "gateway": False,
    }

    try:
        from .short_term import ShortTermMemory
        stm = ShortTermMemory()
        results["short_term"] = True
    except Exception as e:
        log_degraded(logger, e, "src/memory/__init__.py:health_check")
        results["short_term"] = False

    try:
        from .long_term import LongTermMemory
        ltm = LongTermMemory()
        results["long_term"] = True
    except Exception as e:
        log_degraded(logger, e, "src/memory/__init__.py:health_check")
        results["long_term"] = False

    try:
        from .semantic import SemanticMemory
        sm = SemanticMemory()
        results["semantic"] = True
    except Exception as e:
        log_degraded(logger, e, "src/memory/__init__.py:health_check")
        results["semantic"] = False

    try:
        from .gateway import MemoryGateway
        gw = MemoryGateway()
        results["gateway"] = True
    except Exception as e:
        log_degraded(logger, e, "src/memory/__init__.py:health_check")
        results["gateway"] = False

    return results
