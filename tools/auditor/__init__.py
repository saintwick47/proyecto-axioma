"""
tools.auditor — paquete del Auditor AXIOMA (v0.6.8uu).

5 módulos con prefijo `aud_` y tipos `Aud*` para NO colisionar con nombres
existentes de AXIOMA (promotion_gate.Verdict, security_audit.Severity/Finding).
El CLI se mantiene vía shim raíz: python axioma_auditor.py --full
"""
from tools.auditor.aud_core import (
    AudFinding, AudSeverity, AudVerdict,
    BaseDetector, CodeIndex, DetectorResult,
)
from tools.auditor.aud_main import AxiomaAuditor, DETECTORS, main

# Alias históricos por compatibilidad (no colisionan si se importan desde
# este paquete, pero se exportan igual que antes desde axioma_auditor raíz)
Finding = AudFinding
Severity = AudSeverity
Verdict = AudVerdict

__all__ = [
    "AudFinding", "AudSeverity", "AudVerdict",
    "Finding", "Severity", "Verdict",
    "BaseDetector", "CodeIndex", "DetectorResult",
    "AxiomaAuditor", "DETECTORS", "main",
]
__version__ = "v0.6.8uu (refactor tools/auditor, 5 módulos aud_*)"
