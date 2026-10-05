#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Módulo Router de Intenciones
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-17
# Propósito: Clasificación de intenciones + ruteo inteligente
# Componentes:
# - IntentClassifier: Clasifica queries por tipo de tarea (Facade)
# - IntentClassifierCore: Núcleo de clasificación (keywords, embeddings)
# - APIDetector: Detección de consultas especiales + APIs
# - SmartRouter: Router principal con caché L1/L2
# Estado: Fase 3 — Implementación completa + refactorización modular
# ═══════════════════════════════════════════════════════════════
from typing import Optional, Any, Dict

__version__ = "0.0.8"
__author__ = "SaintWick"

__all__ = [
    "IntentClassifier",
    "IntentClassifierCore",
    "APIDetector",
    "SmartRouter",
]

# Mapeo nombre_público → submódulo para lazy loading.
# RouterCache no se exporta: es interno, solo smart_router.py lo usa.
_LAZY_CLASSES = {
    "IntentClassifier": ".classifier",
    "IntentClassifierCore": ".classifier_core",
    "APIDetector": ".classifier_api",
    "SmartRouter": ".smart_router",
}


def __getattr__(name: str) -> Any:
    """
    Lazy loading de clases del módulo Router.

    Args:
        name: Nombre de la clase solicitada

    Returns:
        Clase solicitada

    Raises:
        AttributeError: Si el nombre no existe
    """
    if name in _LAZY_CLASSES:
        import importlib
        module = importlib.import_module(_LAZY_CLASSES[name], __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_classifier(
    model_path: Optional[str] = None,
    threshold: float = 0.7,
    use_embeddings: bool = True,
    enable_api_integration: bool = True,
) -> Any:
    """
    Factory function para crear IntentClassifier.

    Args:
        model_path: Ruta a modelo entrenado (opcional)
        threshold: Umbral de confianza mínimo
        use_embeddings: Usar embeddings para clasificación
        enable_api_integration: Habilitar integración con APIs externas

    Returns:
        Instancia de IntentClassifier

    Example:
        >>> classifier = create_classifier(threshold=0.8)
        >>> result = classifier.classify("¿Cómo instalo Python?")
    """
    from .classifier import IntentClassifier
    return IntentClassifier(
        model_path=model_path,
        threshold=threshold,
        use_embeddings=use_embeddings,
        enable_api_integration=enable_api_integration,
    )


def health_check() -> Dict[str, bool]:
    """
    Verifica estado del router instanciando cada componente.

    Returns:
        Dict con estado de cada componente
    """
    results = {
        "classifier": False,
        "classifier_core": False,
        "api_detector": False,
        "router": False,
    }

    try:
        from .classifier import IntentClassifier
        IntentClassifier()
        results["classifier"] = True
    except Exception:
        results["classifier"] = False

    try:
        from .classifier_core import IntentClassifierCore
        IntentClassifierCore()
        results["classifier_core"] = True
    except Exception:
        results["classifier_core"] = False

    try:
        from .classifier_api import APIDetector
        APIDetector()
        results["api_detector"] = True
    except Exception:
        results["api_detector"] = False

    try:
        from .smart_router import SmartRouter
        SmartRouter()
        results["router"] = True
    except Exception:
        results["router"] = False

    return results
