# Ruta: /ruta/a/axioma/src/domain/exceptions.py
# Autor: SaintWick
"""Excepciones del dominio de AXIOMA."""

from typing import Optional
import logging

logger = logging.getLogger(__name__)

def _log_exception(exc: "AxiomaError"):
    """Log excepción en DetailedLogger si está disponible."""
    try:
        from tools.detailed_logger import get_logger
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event("DOMAIN_ERROR", f"{exc.code}: {exc.message}",
                         level="ERROR", extra={"code": exc.code, "message": exc.message[:200]})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 exceptions.py:18] excepción degradada (intencional): {_d2e}")


class AxiomaError(Exception):
    """Excepción base para todos los errores de AXIOMA."""

    def __init__(self, message: str, code: Optional[str] = None):
        self.message = message
        self.code = code or "AXIOMA_UNKNOWN"
        super().__init__(self.message)
        _log_exception(self)

    def to_dict(self) -> dict:
        """Serializa la excepción a diccionario."""
        return {"error": self.code, "message": self.message}


class ValidationError(AxiomaError):
    """Error de validación de entrada/salida."""

    def __init__(self, message: str, field: Optional[str] = None):
        self.field = field
        code = "AXIOMA_VALIDATION"
        if field:
            code = f"{code}_{field.upper()}"
        super().__init__(message, code)


class LLMError(AxiomaError):
    """Error en comunicación con LLM."""

    def __init__(
        self,
        message: str,
        model: Optional[str] = None,
        timeout: Optional[bool] = False,
    ):
        self.model = model
        self.timeout = timeout
        code = "AXIOMA_LLM"
        if timeout:
            code = "AXIOMA_LLM_TIMEOUT"
        super().__init__(message, code)


class MemoryError(AxiomaError):
    """Error en operaciones de memoria."""

    def __init__(self, message: str, operation: Optional[str] = None):
        self.operation = operation
        code = "AXIOMA_MEMORY"
        if operation:
            code = f"{code}_{operation.upper()}"
        super().__init__(message, code)


class RouterError(AxiomaError):
    """Error en clasificación/ruteo."""

    def __init__(self, message: str, query: Optional[str] = None):
        self.query = query
        super().__init__(message, "AXIOMA_ROUTER")


class AgentError(AxiomaError):
    """Error en ejecución de agente."""

    def __init__(self, message: str, agent: Optional[str] = None):
        self.agent = agent
        code = "AXIOMA_AGENT"
        if agent:
            code = f"{code}_{agent.upper()}"
        super().__init__(message, code)


class ConfigurationError(AxiomaError):
    """Error de configuración."""

    def __init__(self, message: str, setting: Optional[str] = None):
        self.setting = setting
        code = "AXIOMA_CONFIG"
        if setting:
            code = f"{code}_{setting.upper()}"
        super().__init__(message, code)


class IntegrationError(AxiomaError):
    """Error en integración con API externa."""

    def __init__(self, message: str, api: Optional[str] = None):
        self.api = api
        code = "AXIOMA_INTEGRATION"
        if api:
            code = f"{code}_{api.upper()}"
        super().__init__(message, code)


class SandboxUnavailableError(AxiomaError):
    """
    El sandbox no puede confinar la ejecución — se rechaza ejecutar (fail-closed).

    Se lanza cuando el modo de ejecución STRICT exige confinamiento de
    recursos (CPU/memoria/procesos) pero la plataforma o el entorno no
    permiten aplicarlo (Windows, setrlimit no disponible, o fallo al
    aplicar los límites). Nunca se degrada a ejecución sin límites.
    """

    def __init__(self, message: str, backend: Optional[str] = None):
        self.backend = backend
        code = "AXIOMA_SANDBOX_UNAVAILABLE"
        if backend:
            code = f"{code}_{backend.upper()}"
        super().__init__(message, code)
