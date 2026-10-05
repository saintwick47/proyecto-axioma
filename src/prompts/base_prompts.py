#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Layer 0: Core Identity & Constitutional Rules
# Ruta: src/prompts/base_prompts.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════
"""
Layer 0 del sistema de prompts.
Define la identidad central de AXIOMA y las reglas constitucionales
que ningún agente puede violar, independientemente de su especialización.
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


def _version_del_proyecto() -> str:
    """✅ v0.6.9w (FIX VERSIONES): la identidad mostrada al modelo decía
    "AXIOMA v0.2.0" mientras el proyecto iba por 0.6.9. Ahora se lee la versión
    real de `src/__init__.py` (fuente única) en lugar de duplicarla a mano.

    Returns:
        Versión declarada del proyecto, o `"desconocida"` si no se puede leer.
    """
    try:
        import re

        raiz = Path(__file__).resolve().parents[2]      # …/axioma
        texto = (raiz / "src" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', texto)
        if m:
            return m.group(1)
    except Exception as e:  # pragma: no cover — nunca romper la construcción del prompt
        # R3: la versión es informativa; el prompt se construye igual.
        log_degraded(logger, e, "base_prompts._version_del_proyecto")
    return "desconocida"


AXIOMA_VERSION = _version_del_proyecto()

CORE_IDENTITY = (
    f"Eres AXIOMA v{AXIOMA_VERSION}, un asistente de IA local, preciso y eficiente.\n"
    "Operas completamente de forma local sin enviar datos a servidores externos.\n"
    "Tu objetivo es ser útil, honesto y directo."
)

CONSTITUTIONAL_RULES = """
REGLAS CONSTITUCIONALES (no negociables para todos los agentes):
❌ NUNCA uses preámbulos como "Basado en...", "Puedo...", "Según..."
❌ NUNCA muestres procesos internos al usuario final
❌ NUNCA inventes datos, fechas, fuentes o hechos
❌ NUNCA respondas en un idioma distinto al del usuario
✅ Respuesta directa PRIMERO — datos, valores, resultados
✅ Máximo 3 párrafos salvo que se solicite más detalle
✅ Ofrece ayuda adicional al final si corresponde
✅ Sé honesto cuando no puedas responder algo
"""

SAFETY_CONSTRAINTS = """
RESTRICCIONES DE SEGURIDAD ABSOLUTAS:
- No generes código malicioso, destructivo o para eludir autenticación
- No compartas información personal identificable de terceros
- No actúes como si fueras un humano cuando se te pregunte directamente
- No ejecutes acciones irreversibles sin confirmación explícita
"""


@dataclass
class IdentityLayer:
    """
    Layer 0: Identidad y reglas constitucionales de AXIOMA.

    Este layer es el fundamento de todos los system prompts.
    Se inyecta primero, antes que cualquier especialización.
    """
    version: str = AXIOMA_VERSION
    identity: str = CORE_IDENTITY
    constitutional_rules: str = CONSTITUTIONAL_RULES
    safety_constraints: str = SAFETY_CONSTRAINTS
    custom_rules: List[str] = field(default_factory=list)

    def build(self, include_safety: bool = True) -> str:
        """
        Construye el bloque de Layer 0 listo para inyectar.

        Args:
            include_safety: Si incluir restricciones de seguridad

        Returns:
            String del layer 0 formateado
        """
        parts = [self.identity.strip(), self.constitutional_rules.strip()]
        if include_safety:
            parts.append(self.safety_constraints.strip())
        if self.custom_rules:
            extra = "\nREGLAS ADICIONALES:\n" + "\n".join(f"- {r}" for r in self.custom_rules)
            parts.append(extra)
        return "\n\n".join(parts)

    def with_rules(self, *rules: str) -> "IdentityLayer":
        """
        Añade reglas adicionales al layer.

        Args:
            *rules: Reglas adicionales a incluir

        Returns:
            Nueva instancia con las reglas agregadas
        """
        return IdentityLayer(
            version=self.version,
            identity=self.identity,
            constitutional_rules=self.constitutional_rules,
            safety_constraints=self.safety_constraints,
            custom_rules=list(self.custom_rules) + list(rules),
        )


# Instancia global reutilizable
identity_layer = IdentityLayer()
