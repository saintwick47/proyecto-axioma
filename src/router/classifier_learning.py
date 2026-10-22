#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_learning.py  (REFACTORIZACIÓN POR PARTES)
# Ruta: src/router/classifier_learning.py
#
# ✅ 2026-10-13 (pieza 3, R14 exceptuado el 2026-10-12 / decisión C4 reabierta):
#   Capa de ENTRENAMIENTO/APRENDIZAJE extraída de `src/router/classifier_core.py`, sin cambios
#   de comportamiento. Mismo patrón que `classifier_embedding.py`.
#
#   Qué se movió desde classifier_core.py:
#     - `train`              → entrena con ejemplos y persiste los vectores
#     - `add_example`        → agrega un ejemplo en caliente (tope de 200 por clase)
#     - `_save_model`        → persiste el modelo en disco
#     - `get_learning_stats` → cuántos ejemplos por clase hay
#
#   `IntentClassifierCore` hereda ahora `ClassifierLearningMixin`, así la API pública
#   (self.train, self.add_example, self.get_learning_stats, …) NO cambia.
#
#   `_log_error` vive en classifier_core y se importa DENTRO de cada método (deferred) para
#   evitar el ciclo de import, igual que en `classifier_embedding.py`.
#   El estado (`self._training_vectors`, `self.model_path`, `self.threshold`, `self._cache`) lo
#   inicializa `IntentClassifierCore.__init__`: este mixin sólo lo usa, y también usa
#   `self._get_embedding` (mixin de embeddings) y `self.clear_cache` (mixin de caché).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ClassifierLearningMixin:
    """Entrenamiento y persistencia del clasificador.

    Extraído de IntentClassifierCore (pieza 3): arma los vectores de entrenamiento con los
    embeddings, los guarda en disco y reporta el estado del aprendizaje.
    """

    def train(self, examples: Dict[str, List[str]], save_path: Optional[Path] = None) -> Dict[str, Any]:
        start = time.perf_counter()
        logger.info(f"Training core classifier with {len(examples)} task types")
        training_vectors = {}
        total_examples = 0
        for task_type, texts in examples.items():
            training_vectors[task_type] = []
            for text in texts:
                try:
                    vector = self._get_embedding(text)
                    training_vectors[task_type].append(vector)
                    total_examples += 1
                except Exception as e:
                    from src.router.classifier_core import _log_error  # noqa: F401
                    _log_error(e, "IntentClassifierCore.train", extra={"task_type": task_type})
                    logger.warning(f"Failed to embed example: {e}")
        self._training_vectors = training_vectors
        save_path = save_path or self.model_path
        save_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump({
                    "vectors": training_vectors,
                    "trained_at": datetime.now().isoformat(),
                    "total_examples": total_examples
                }, f, indent=2)
            duration = time.perf_counter() - start
            logger.info(f"Core classifier trained: {total_examples} examples → {save_path} in {duration:.4f}s")
            return {
                "task_types": len(training_vectors),
                "total_examples": total_examples,
                "model_path": str(save_path),
                "duration": duration
            }
        except Exception as e:
            duration = time.perf_counter() - start
            from src.router.classifier_core import _log_error  # noqa: F401
            _log_error(e, "IntentClassifierCore.train", extra={"save_path": str(save_path)})
            logger.error(f"Failed to save trained model: {e}")
            raise

    def add_example(self, task_type: str, text: str, persist: bool = True) -> bool:
        if not text or not task_type:
            return False
        try:
            vector = self._get_embedding(text)
            if not vector:
                return False
            if task_type not in self._training_vectors:
                self._training_vectors[task_type] = []
            _MAX_EXAMPLES_PER_CLASS = 200
            if len(self._training_vectors[task_type]) >= _MAX_EXAMPLES_PER_CLASS:
                self._training_vectors[task_type].pop(0)
            self._training_vectors[task_type].append(vector)
            self.clear_cache()
            if persist:
                self._save_model()
            return True
        except Exception as e:
            from src.router.classifier_core import _log_error  # noqa: F401
            _log_error(e, "IntentClassifierCore.add_example",
                       extra={"task_type": task_type, "text_len": len(text)})
            logger.warning(f"add_example failed (non-fatal): {e}")
            return False

    def _save_model(self) -> None:
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            total = sum(len(v) for v in self._training_vectors.values())
            with open(self.model_path, "w", encoding="utf-8") as f:
                json.dump({
                    "vectors": self._training_vectors,
                    "trained_at": datetime.now().isoformat(),
                    "total_examples": total,
                }, f)
            logger.debug(f"Model persisted: {total} examples → {self.model_path}")
        except Exception as e:
            from src.router.classifier_core import _log_error  # noqa: F401
            _log_error(e, "IntentClassifierCore._save_model",
                       extra={"model_path": str(self.model_path)})
            logger.warning(f"_save_model failed (non-fatal): {e}")

    def get_learning_stats(self) -> Dict[str, Any]:
        per_class = {k: len(v) for k, v in self._training_vectors.items()}
        return {
            "total_examples": sum(per_class.values()),
            "classes": len(per_class),
            "examples_per_class": per_class,
            "threshold": self.threshold,
            "cache_size": len(self._cache),
        }
