#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_scoring.py  (REFACTORIZACIÓN POR PARTES)
# Ruta: src/router/classifier_scoring.py
#
# ✅ 2026-10-13 (pieza 5, R14 exceptuado el 2026-10-12 / decisión C4 reabierta):
#   Capa de SCORING extraída de `src/router/classifier_core.py`, sin cambios de comportamiento.
#   Mismo patrón que `classifier_embedding.py`.
#
#   Qué se movió desde classifier_core.py:
#     - `_preprocess_text`             → normaliza el texto (acentos, signos)
#     - `calibrate_probability`        → calibra el score crudo
#     - `_adaptive_threshold`          → umbral según el tipo de tarea y su varianza
#     - `_assign_task_type_adaptive`   → asigna tipo con el umbral adaptativo
#     - `_assign_task_type`            → asigna tipo (camino directo)
#
#   `IntentClassifierCore` hereda ahora `ClassifierScoringMixin`, así la API pública
#   (self._preprocess_text, …) NO cambia.
#
#   `TASK_TYPE_NORMALIZATION` se importa del módulo de DATOS `classifier_keywords.py`.
#   Los umbrales (`ADAPTIVE_*`, `MIN_FLOOR_THRESHOLD`) siguen en `classifier_core.py` y se importan
#   DIFERIDO dentro de los métodos que los usan, para evitar el ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
from typing import Optional

from src.domain.types import TaskType
from src.router.classifier_keywords import TASK_TYPE_NORMALIZATION

logger = logging.getLogger(__name__)


class ClassifierScoringMixin:
    """Preprocesado del texto, calibración del score y asignación del tipo de tarea.

    Extraído de IntentClassifierCore (pieza 5).
    """

    @staticmethod
    def _preprocess_text(text: str) -> str:
        """Normaliza el texto para el matching de keywords.

        ✅ ISSUE-064 (bug real): antes era solo `lower().strip()`, así que un
        texto con **espaciado irregular** ("que  hora es", típico de dictado o de
        copiar/pegar) NO matcheaba keywords multi-palabra: `_keyword_match` usa
        `keyword in text`, y `'que hora es' in 'que  hora es'` es **False**.
        Resultado: clasificación degradada a `general_chat` en silencio (y ~15 s
        de LLM para clasificar en CPU). Ahora los espacios internos se colapsan.
        """
        return re.sub(r"\s+", " ", str(text or "")).strip().lower()

    @staticmethod
    def calibrate_probability(raw_score: float) -> float:
        return round(max(0.0, min(1.0, raw_score)), 4)

    # ═══════════════════════════════════════════════════════════════
    # ✅ v4.8.0: THRESHOLD ADAPTATIVO
    # ═══════════════════════════════════════════════════════════════
    def _adaptive_threshold(self, task_type_str: Optional[str]) -> float:
        from src.router.classifier_core import (
            ADAPTIVE_DEFAULT_THRESHOLD, ADAPTIVE_THRESHOLDS, ADAPTIVE_VARIANCE_BOOST,
            ADAPTIVE_VARIANCE_MAX, ADAPTIVE_VARIANCE_THRESHOLD,
        )  # noqa: F401 — diferido, para no crear el ciclo de import
        """
        Calcula el threshold adaptativo para un tipo de tarea.
        Lógica:
        1. Si self._adaptive_enabled=False o self.threshold fue seteado
           explícitamente (≠ 0.7 default), usar self.threshold como override
           global (backward compat con tests/legacy code).
        2. Si no, buscar threshold por tipo en ADAPTIVE_THRESHOLDS.
        3. Si _last_feature_std > 0.4 (clasificador "indeciso" en embeddings),
           sumar 0.10 al threshold para ser más estricto.
        4. Cap superior en 0.95 para evitar umbrales imposibles.
        """
        # ── Backward compat: override global ──
        if not self._adaptive_enabled or self.threshold != 0.7:
            return self.threshold
        # ── Threshold base por tipo ──
        base = ADAPTIVE_THRESHOLDS.get(task_type_str or "", ADAPTIVE_DEFAULT_THRESHOLD)
        # ── Boost por feature variance (clasificador indeciso) ──
        if self._last_feature_std > ADAPTIVE_VARIANCE_THRESHOLD:
            base = min(base + ADAPTIVE_VARIANCE_BOOST, ADAPTIVE_VARIANCE_MAX)
        return round(base, 4)

    def _assign_task_type_adaptive(self, calibrated_prob: float, raw_intent: Optional[str]) -> TaskType:
        from src.router.classifier_core import (
            MIN_FLOOR_THRESHOLD,
        )  # noqa: F401 — diferido, para no crear el ciclo de import
        """
        ✅ v4.8.0: Asigna TaskType con threshold adaptativo por tipo.
        Flujo de decisión:
        1. Si raw_intent es None → GENERAL_CHAT (sin candidato)
        2. Si calibrated_prob < MIN_FLOOR (0.3) → GENERAL_CHAT (confianza muy baja)
        3. Calcular threshold adaptativo para raw_intent
        4. Si calibrated_prob < adaptive → GENERAL_CHAT (confianza insuficiente
           para el tipo específico)
        5. Si pasa el threshold → aceptar raw_intent como TaskType
        Backward compat: _assign_task_type() (static) sigue existiendo
        para callers legacy/tests que no necesitan el threshold adaptativo.
        """
        # ── Sin candidato o confianza muy baja → fallback ──
        if raw_intent is None or calibrated_prob < MIN_FLOOR_THRESHOLD:
            return TaskType.GENERAL_CHAT
        # ── Threshold adaptativo por tipo ──
        adaptive = self._adaptive_threshold(raw_intent)
        if calibrated_prob < adaptive:
            logger.debug(
                f"[AdaptiveThreshold] {raw_intent}: score={calibrated_prob:.3f} "
                f"< adaptive={adaptive:.3f} → fallback GENERAL_CHAT"
            )
            return TaskType.GENERAL_CHAT
        # ── Aceptar clasificación ──
        normalized = TASK_TYPE_NORMALIZATION.get(raw_intent, raw_intent)
        try:
            return TaskType(normalized)
        except ValueError:
            logger.warning(f"Unknown TaskType value: {normalized}, falling back to GENERAL_CHAT")
            return TaskType.GENERAL_CHAT

    # ── Método legacy preservado para backward compat ──
    @staticmethod
    def _assign_task_type(calibrated_prob: float, raw_intent: Optional[str]) -> TaskType:
        from src.router.classifier_core import (
            MIN_FLOOR_THRESHOLD,
        )  # noqa: F401 — diferido, para no crear el ciclo de import
        """
        ⚠️  LEGACY: Threshold fijo 0.3 floor, sin adaptive por tipo.
        Mantenido para callers externos (tests, integrations) que
        esperan este comportamiento. Usar _assign_task_type_adaptive()
        para el nuevo flujo.
        """
        if raw_intent is None or calibrated_prob < MIN_FLOOR_THRESHOLD:
            return TaskType.GENERAL_CHAT
        normalized = TASK_TYPE_NORMALIZATION.get(raw_intent, raw_intent)
        try:
            return TaskType(normalized)
        except ValueError:
            logger.warning(f"Unknown TaskType value: {normalized}, falling back to GENERAL_CHAT")
            return TaskType.GENERAL_CHAT
