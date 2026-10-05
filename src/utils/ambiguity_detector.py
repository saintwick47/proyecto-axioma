import logging
# src/utils/ambiguity_detector.py
# Ruta: /ruta/a/axioma/src/utils/ambiguity_detector.py
# Autor: SaintWick
# Versión: 0.0.8 (actualizado 2026-09-09)
# Propósito: Detectar nivel de ambigüedad en requests de usuario
# Fase: 1 de 6 - Detector de Ambigüedad

import os
import yaml
from typing import Tuple, List, Dict, Any
from datetime import datetime
from config.paths import Paths
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# ✅ FIX: "config/ambiguity_patterns.yaml" era relativo al CWD del proceso —
# mismo patrón ya encontrado y corregido esta sesión en feedback_loop.py,
# feedback_loop_components.py y knowledge_graph.py. Bajo systemd (u otro CWD)
# os.path.exists() daba siempre False y esto caía siempre a
# _load_default_patterns() (patrones genéricos), ignorando el YAML real.
_DEFAULT_CONFIG_PATH = str(Paths.CONFIG / "ambiguity_patterns.yaml")

# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
def _log_ambiguity(event: str, level: str = "INFO", extra: dict = None):
    """Log evento de ambigüedad en DetailedLogger."""
    try:
        from tools.detailed_logger import get_logger
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event("AMBIGUITY", event, level=level, extra=extra or {})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 ambiguity_detector.py:32] excepción degradada (intencional): {_d2e}")


class AmbiguityDetector:
    """
    Detector de ambigüedad en requests de usuario.

    Analiza el texto de entrada y clasifica el nivel de ambigüedad
    según patrones configurables en YAML.

    Atributos:
        patterns (Dict): Patrones cargados desde configuración
        thresholds (Dict): Umbrales para cada nivel de ambigüedad
    """

    def __init__(self, config_path: str = _DEFAULT_CONFIG_PATH):
        """
        Inicializa el detector cargando patrones desde YAML.

        Args:
            config_path: Ruta al archivo de configuración YAML
        """
        self.config_path = config_path
        self.patterns = {}
        self.thresholds = {}
        self._load_config()

    def _load_config(self) -> None:
        """
        Carga la configuración desde el archivo YAML.

        Raises:
            FileNotFoundError: Si el archivo de configuración no existe
            yaml.YAMLError: Si el archivo YAML tiene formato inválido
        """
        if not os.path.exists(self.config_path):
            # Fallback a patrones por defecto si no existe config
            self._load_default_patterns()
            return

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            self.patterns = config.get('detector', {}).get('patterns', {})
            self.thresholds = config.get('detector', {}).get('thresholds', {})
            _log_ambiguity("Config loaded from YAML", extra={"config_path": self.config_path})
        except yaml.YAMLError as e:
            print(f"[AmbiguityDetector] Error cargando YAML: {e}")
            _log_ambiguity(f"YAML error — using defaults: {e}", level="WARNING")
            self._load_default_patterns()
        except Exception as e:
            print(f"[AmbiguityDetector] Error inesperado: {e}")
            _log_ambiguity(f"Load error — using defaults: {e}", level="WARNING")
            self._load_default_patterns()

    def _load_default_patterns(self) -> None:
        """Carga patrones por defecto si falla la carga de configuración."""
        self.patterns = {
            "high": ["mejor que", "reemplazar", "más eficiente que", "sistema completo"],
            "medium": ["más rápido", "optimizar", "mejorar", "más seguro"],
            "low": ["función para", "script que", "código para"]
        }
        self.thresholds = {
            "high": 2,
            "medium": 1,
            "fallback_trigger": 2
        }

    def detect_ambiguity(self, user_request: str) -> Tuple[str, int, List[str]]:
        """
        Detecta el nivel de ambigüedad en un request de usuario.

        Args:
            user_request: Texto del request del usuario

        Returns:
            Tuple[str, int, List[str]]:
                - Nivel de ambigüedad ("high", "medium", "low", "none")
                - Score calculado
                - Lista de patrones encontrados
        """
        if not user_request or not isinstance(user_request, str):
            return "none", 0, []

        request_lower = user_request.lower()
        scores = {"high": 0, "medium": 0, "low": 0}
        found_patterns = []

        for level, patterns in self.patterns.items():
            for pattern in patterns:
                if pattern in request_lower:
                    scores[level] += 1
                    found_patterns.append(pattern)

        # Determinar nivel máximo
        max_level = "none"
        max_score = 0

        for level in ["high", "medium", "low"]:
            if scores[level] >= self.thresholds.get(level, 1):
                max_level = level
                max_score = scores[level]
                break

        _log_ambiguity(
            f"Detected: {max_level} (score={max_score})",
            level="INFO" if max_level != "none" else "DEBUG",
            extra={
                "level": max_level,
                "score": max_score,
                "patterns_found": found_patterns[:5],
                "text_preview": user_request[:50],
                "scores": scores,
            }
        )
        return max_level, max_score, found_patterns

    def get_rewrite_template(self, ambiguity_level: str, user_request: str) -> str:
        """
        Obtiene el template de reescritura para el nivel de ambigüedad.

        Args:
            ambiguity_level: Nivel detectado ("high", "medium", "low")
            user_request: Request original del usuario

        Returns:
            str: Query reescrito para pasar al modelo
        """
        # Cargar templates desde config
        templates = {
            "high": "Genera solución PRÁCTICA para '{tema}'. Incluye advertencias de limitaciones vs herramientas enterprise. No asumas requisitos no especificados.",
            "medium": "Genera código que optimice '{tema}' con implementación práctica. Documenta trade-offs y suposiciones tomadas.",
            "low": "Genera código directo con defaults estándar. Sigue mejores prácticas del lenguaje."
        }

        # Intentar cargar desde YAML si existe
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    templates = config.get('rewriter', {}).get('templates', templates)
        except Exception as e:
            # R3: YAML ilegible → se siguen usando los defaults embebidos.
            log_degraded(logger, e, f"{type(self).__name__}.get_rewrite_template (cargar templates desde {self.config_path})")

        template = templates.get(ambiguity_level, templates["low"])

        # Extraer tema (primeras 5 palabras)
        words = user_request.split()[:5]
        tema = " ".join(words) if words else "tu solicitud"

        rewritten = template.format(tema=tema)
        rewritten += f"\n\nPetición original: {user_request}"
        _log_ambiguity(
            f"Query rewritten: {ambiguity_level}",
            extra={
                "ambiguity_level": ambiguity_level,
                "original_preview": user_request[:50],
                "rewritten_preview": rewritten[:80],
            }
        )
        return rewritten

    def get_clarification_questions(self, user_request: str) -> List[str]:
        """
        Obtiene preguntas de clarificación basadas en patrones encontrados.

        Args:
            user_request: Request original del usuario

        Returns:
            List[str]: Lista de preguntas para clarificar (máx 2)
        """
        questions_db = {
            "mejor que": [
                "¿Qué aspecto específico te interesa? (velocidad, precisión, UI, características)",
                "¿Es para uso personal o empresarial?"
            ],
            "más eficiente": [
                "¿Eficiente en qué aspecto? (tiempo de ejecución, memoria, líneas de código)",
                "¿Tienes un benchmark o código actual para comparar?"
            ],
            "optimizar": [
                "¿Qué métrica quieres mejorar? (rendimiento, legibilidad, mantenimiento)",
                "¿Hay restricciones de recursos o tiempo?"
            ]
        }

        # Intentar cargar desde YAML
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    questions_db = config.get('clarification', {}).get('questions', questions_db)
        except Exception as e:
            # R3: YAML ilegible → se siguen usando los defaults embebidos.
            log_degraded(logger, e, f"{type(self).__name__}.get_clarification_questions (cargar preguntas desde {self.config_path})")

        request_lower = user_request.lower()

        for pattern, questions in questions_db.items():
            if pattern in request_lower:
                return questions[:2]

        return ["¿Podés ser más específico sobre qué necesitás?"]


def detect_ambiguity(user_request: str, config_path: str = _DEFAULT_CONFIG_PATH) -> Tuple[str, int, List[str]]:
    """
    Función convenience para detectar ambigüedad sin instanciar la clase.

    Args:
        user_request: Texto del request del usuario
        config_path: Ruta al archivo de configuración YAML

    Returns:
        Tuple[str, int, List[str]]: Nivel, score y patrones encontrados
    """
    detector = AmbiguityDetector(config_path)
    return detector.detect_ambiguity(user_request)


def rewrite_query(user_request: str, ambiguity_level: str, config_path: str = _DEFAULT_CONFIG_PATH) -> str:
    """
    Función convenience para reescribir query sin instanciar la clase.

    Args:
        user_request: Request original del usuario
        ambiguity_level: Nivel de ambigüedad detectado
        config_path: Ruta al archivo de configuración YAML

    Returns:
        str: Query reescrito para el modelo
    """
    detector = AmbiguityDetector(config_path)
    return detector.get_rewrite_template(ambiguity_level, user_request)


def get_clarification_questions(user_request: str, config_path: str = _DEFAULT_CONFIG_PATH) -> List[str]:
    """
    Función convenience para obtener preguntas de clarificación.

    Args:
        user_request: Request original del usuario
        config_path: Ruta al archivo de configuración YAML

    Returns:
        List[str]: Lista de preguntas para clarificar
    """
    detector = AmbiguityDetector(config_path)
    return detector.get_clarification_questions(user_request)


# ============================================================================
# TESTS RÁPIDOS (Ejecutar con: python src/utils/ambiguity_detector.py)
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TESTS DE DETECTOR DE AMBIGÜEDAD - AXIOMA v0.0.8")
    print("=" * 60)

    test_cases = [
        # High Ambiguity
        ("dame un programa mejor que nmap", "high"),
        ("quiero algo más eficiente que pandas", "high"),
        ("reemplaza esta librería enterprise", "high"),

        # Medium Ambiguity
        ("optimiza este código", "medium"),
        ("hazlo más rápido", "medium"),
        ("mejora la seguridad", "medium"),

        # Low Ambiguity
        ("función para leer CSV en Python", "low"),
        ("script que liste archivos .txt", "low"),
        ("código para conectar a MySQL", "low"),

        # None
        ("hola", "none"),
        ("", "none"),
    ]

    detector = AmbiguityDetector()
    passed = 0
    failed = 0

    for request, expected_level in test_cases:
        level, score, patterns = detector.detect_ambiguity(request)
        status = "✅" if level == expected_level else "❌"

        if level == expected_level:
            passed += 1
        else:
            failed += 1

        print(f"{status} Request: '{request}'")
        print(f"   Esperado: {expected_level} | Obtenido: {level} | Score: {score}")
        if patterns:
            print(f"   Patrones: {patterns}")
        print()

    print("=" * 60)
    print(f"RESULTADOS: {passed}/{len(test_cases)} tests pasados")
    print("=" * 60)
