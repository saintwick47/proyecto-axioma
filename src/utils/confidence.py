#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — confidence.py: calibración de la confianza mostrada
# ═══════════════════════════════════════════════════════════════
# Ruta: src/utils/confidence.py
#
# POR QUÉ EXISTE: `Response.confidence` la fijaba **cada agente por su cuenta**
# con valores fijos o semi-fijos (el coder devolvía 1.0, el analista 0.8, rutas
# de caché 0.8). En el panel de transparencia (P0) eso se veía como "confianza
# Alta (100%)" incluso cuando el validador había dado un score mediocre — o sea,
# el número no informaba nada en las tareas simples.
#
# ⚠️ REGLA DE ORO: `Response.confidence` NO decide nada en el pipeline
# (verificado: los chequeos de `confidence` en el código son de *clasificador*,
# OCR/STT o detectores internos; nadie ramifica por `response.confidence`). Es un
# dato **de presentación**: se muestra en el panel y viaja al feedback. Por eso
# calibrarla es seguro y por eso la fórmula vive acá, aislada y testeable.
#
# QUÉ HACE: combina lo que el sistema SABE con lo que el agente cree:
#   1. score del validador (0-10)  → evidencia medida
#   2. confianza declarada del agente
#   3. resultado de validación (falló / se omitió / no aplica)
#   4. fuentes/citas presentes (grounding)
# Si NO hay ninguna evidencia de validación, deja la confianza del agente
# **intacta**: no se inventan números.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

# Pesos de la mezcla: la evidencia (validador) pesa más que la autopercepción
# del agente. Elegidos deliberadamente simples y documentados para poder
# revisarlos si alguna vez se mide con datos reales de feedback.
W_VALIDATOR = 0.6
W_AGENT = 0.4

# Techo cuando la validación FALLÓ y se conservó la respuesta original: el
# sistema sabe que no pasó, así que no puede mostrarse como confiable.
FAILED_CAP = 0.45

# Bonus por grounding (evidencia real, no opinión).
SOURCE_BONUS_1 = 0.05
SOURCE_BONUS_3 = 0.05
MIN_CONF = 0.05
MAX_CONF = 0.95

LABELS = ((0.8, "Alto"), (0.5, "Moderado"), (0.0, "Bajo"))


def label_for(value: Optional[float]) -> Optional[str]:
    """Etiqueta válida para `Response.confidence_label` (o None)."""
    if value is None:
        return None
    for umbral, nombre in LABELS:
        if float(value) >= umbral:
            return nombre
    return "Bajo"


def _clamp(value: float) -> float:
    return max(MIN_CONF, min(MAX_CONF, round(float(value), 2)))


def calibrate_confidence(agent_confidence: Optional[float],
                         validator_score: Optional[float] = None,
                         validation_failed: bool = False,
                         source_count: int = 0) -> Optional[float]:
    """Devuelve la confianza calibrada, o la del agente si no hay evidencia.

    Args:
        agent_confidence: lo que declaró el agente (0-1, puede ser None).
        validator_score: score del validador en escala 0-10 (None si no corrió).
        validation_failed: True si la validación falló y se conservó la respuesta.
        source_count: cantidad de fuentes/citas que respaldan la respuesta.

    Returns:
        Confianza final (0.05-0.95) o `agent_confidence` sin tocar si no hay
        ninguna evidencia de validación.
    """
    base = 0.5 if agent_confidence is None else float(agent_confidence)
    base = max(0.0, min(1.0, base))

    have_score = isinstance(validator_score, (int, float))
    if not have_score and not validation_failed:
        # Sin evidencia: se respeta lo que dijo el agente (no se inventa).
        return agent_confidence

    if have_score:
        # score 0-10 → 0-1, acotado por si el validador devolviera algo raro
        norm = max(0.0, min(1.0, float(validator_score) / 10.0))
        value = W_VALIDATOR * norm + W_AGENT * base
    else:
        value = base

    if validation_failed:
        value = min(value, FAILED_CAP)

    if source_count >= 3:
        value += SOURCE_BONUS_1 + SOURCE_BONUS_3
    elif source_count >= 1:
        value += SOURCE_BONUS_1

    if validation_failed:
        value = min(value, FAILED_CAP)

    return _clamp(value)


def apply_calibration(response: Any, agent_confidence: Optional[float] = None,
                      validator_score: Optional[float] = None,
                      validation_failed: bool = False) -> Optional[float]:
    """Aplica la calibración sobre un `Response` (in place) y devuelve el valor.

    Nunca lanza: si el objeto no acepta asignación, se degrada en silencio
    (es un dato de presentación, no puede romper la respuesta).
    """
    try:
        srcs = getattr(response, "sources", None) or []
        meta = getattr(response, "metadata", None) or {}
        if not srcs and isinstance(meta, dict):
            srcs = meta.get("sources") or []
        n_sources = len([s for s in srcs if isinstance(s, str) and s.strip()])

        if agent_confidence is None:
            agent_confidence = getattr(response, "confidence", None)

        calibrated = calibrate_confidence(
            agent_confidence=agent_confidence,
            validator_score=validator_score,
            validation_failed=validation_failed,
            source_count=n_sources,
        )
        if calibrated is None:
            return None
        response.confidence = calibrated
        # La etiqueta se recalcula: si el agente había dejado una vieja
        # ("Alto" con confidence 0.4) quedaría inconsistente con el número.
        try:
            response.confidence_label = label_for(calibrated)
        except Exception as e:  # pragma: no cover — label es opcional
            # R3: `confidence` (el número) ya quedó calibrado; la etiqueta es cosmética.
            log_degraded(logger, e, "confidence.apply_calibration (confidence_label)",
                         contract_level=logging.DEBUG)
        return calibrated
    except Exception:  # pragma: no cover — dato de presentación
        return None


def calibration_summary(values: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """Resumen para medir antes/después (útil en tests y en el informe)."""
    pares = {k: v for k, v in values.items() if isinstance(v, (int, float))}
    if not pares:
        return {"n": 0, "media": None, "min": None, "max": None}
    vals = list(pares.values())
    return {
        "n": len(vals),
        "media": round(sum(vals) / len(vals), 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
        "valores": pares,
    }


__all__ = ["calibrate_confidence", "apply_calibration", "label_for",
           "calibration_summary", "W_VALIDATOR", "W_AGENT", "FAILED_CAP"]
