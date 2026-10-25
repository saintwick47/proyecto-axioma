#!/usr/bin/env python3
"""
axioma_auditor.py — shim de compatibilidad (v0.7.5).

La lógica del auditor se refactorizó en el paquete `tools/auditor/`
(5 módulos ≤900 líneas: core, detectors_infra, detectors_salud,
detectors_arquitectura, auditor). Este archivo mantiene la CLI y los
exports históricos intactos:

    python axioma_auditor.py --full [--run-tests] [--detectors X]
    from axioma_auditor import CodeIndex, AxiomaAuditor, ...

Versión del auditor: v0.7.5 (paquete tools/auditor; alineada con src/__init__.py).
"""
import sys

from tools.auditor import (  # noqa: F401 — exports públicos históricos
    AUDITOR_VERSION,
    AxiomaAuditor,
    BaseDetector,
    CodeIndex,
    DETECTORS,
    DetectorResult,
    Finding,
    Severity,
    Verdict,
    main,
)

__all__ = [
    "AxiomaAuditor", "BaseDetector", "CodeIndex", "DETECTORS",
    "DetectorResult", "Finding", "Severity", "Verdict", "main",
    "AUDITOR_VERSION",
]
__version__ = AUDITOR_VERSION


if __name__ == "__main__":
    sys.exit(main())
