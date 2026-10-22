#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_cache.py  (REFACTORIZACIÓN POR PARTES)
# Ruta: src/router/classifier_cache.py
#
# ✅ 2026-10-13 (pieza 2, R14 exceptuado el 2026-10-12 / decisión C4 reabierta):
#   Capa de CACHÉ DE RESPUESTAS extraída de `src/router/classifier_core.py`, sin cambios de
#   comportamiento. Mismo patrón que `classifier_embedding.py` y `dispatcher_cache_metrics.py`.
#
#   Qué se movió desde classifier_core.py:
#     - `_generate_cache_key`  → clave sha256 corta del texto
#     - `_get_from_cache`      → devuelve (TaskType, confianza) o None; cuenta hits/misses y
#                                descarta lo vencido según `self._cache_ttl`
#     - `_save_to_cache`       → guarda con marca de tiempo
#     - `clear_cache`          → la vacía y lo informa
#
#   `IntentClassifierCore` hereda ahora `ClassifierCacheMixin`, así la API pública
#   (self._get_from_cache, self.clear_cache, …) NO cambia.
#
#   El estado (`self._cache`, `self._cache_ttl`, `self._stats`) lo sigue inicializando
#   `IntentClassifierCore.__init__`: este mixin sólo lo usa.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Optional, Tuple

from src.domain.types import TaskType

logger = logging.getLogger(__name__)


class ClassifierCacheMixin:
    """Caché de respuestas del clasificador, con vencimiento por tiempo.

    Extraído de IntentClassifierCore (pieza 2): guarda el resultado de clasificar un texto para
    no repetir el trabajo. El estado vive en la instancia que hereda el mixin.
    """

    def _generate_cache_key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _get_from_cache(self, text: str) -> Optional[Tuple[TaskType, float]]:
        cache_key = self._generate_cache_key(text)
        if cache_key not in self._cache:
            self._stats["cache_misses"] += 1
            return None
        task_type, confidence, timestamp = self._cache[cache_key]
        if (datetime.now() - timestamp).total_seconds() >= self._cache_ttl:
            del self._cache[cache_key]
            self._stats["cache_misses"] += 1
            return None
        self._stats["cache_hits"] += 1
        return task_type, confidence

    def _save_to_cache(self, text: str, task_type: TaskType, confidence: float) -> None:
        cache_key = self._generate_cache_key(text)
        self._cache[cache_key] = (task_type, confidence, datetime.now())

    def clear_cache(self) -> None:
        cache_size = len(self._cache)
        self._cache.clear()
        logger.info(f"IntentClassifierCore cache cleared: {cache_size} entries")
