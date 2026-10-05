#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_embedding.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/router/classifier_embedding.py
#
# ✅ v0.6.9v (REFACTOR): capa de CLASIFICACIÓN POR EMBEDDINGS extraída de
#   src/router/classifier_core.py (el original queda intacto).
#
#   Qué se movió desde classifier_core.py (sin cambios de comportamiento):
#     - `_classify_by_embeddings`   → similitud coseno contra training vectors
#     - `_get_embedding`            → embedding BGE-M3 (singleton) con hash fallback
#     - `_hash_embedding`           → fallback determinístico (md5 → [0,1])
#     - `_cosine_similarity`        → similitud coseno pura
#     - `_record_feature_std`       → varianza de scores (ajuste de threshold v4.8.0)
#
#   `IntentClassifierCore` hereda ahora `ClassifierEmbeddingMixin`, así la API
#   pública (self._classify_by_embeddings, self._get_embedding, …) no cambia.
#
#   Los helpers de logging (_log_router/_log_error) viven en classifier_core y
#   se importan DENTRO de los métodos (deferred) para evitar el ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from src.domain.types import TaskType
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


class ClassifierEmbeddingMixin:
    """Clasificación por embeddings (similitud coseno contra training vectors).

    Extraído de IntentClassifierCore (50/50): este mixin se ocupa de obtener
    embeddings (BGE-M3 con hash fallback), compararlos por coseno y clasificar.
    Además registra la std de scores para el ajuste dinámico del threshold.
    El state compartido (self._training_vectors, self.threshold, self._stats,
    self._last_feature_std) lo define IntentClassifierCore.
    """

    def _get_embedding(self, text: str) -> List[float]:
        start = time.perf_counter()
        try:
            from src.memory.embedding_model_singleton import get_embedding_model
            model = get_embedding_model()
            embedding = model.embed(text)
            if embedding is not None:
                duration = time.perf_counter() - start
                import logging
                logging.getLogger(__name__).debug(
                    f"Embedding via BGE-M3 singleton: {len(embedding)} dims in {duration:.4f}s")
                return embedding
        except Exception as e:
            duration = time.perf_counter() - start
            try:
                from src.router.classifier_core import _log_error, _log_router  # noqa: F401
                _log_error(e, "IntentClassifierCore._get_embedding", extra={"text_len": len(text)})
            except Exception as _log_exc:
                # R3: `_log_error` es deferred (ciclo de import); si falla, ya
                # se avisa abajo con el `warning` del fallback a hash.
                # ⚠️ NO usar `logging.DEBUG` acá: esta función tiene un
                # `import logging` local más abajo → el nombre estaría sin ligar.
                log_degraded(logger, _log_exc, "IntentClassifierCore._get_embedding (_log_error)")
            import logging
            logging.getLogger(__name__).warning(f"BGE-M3 singleton failed, use hash fallback: {e}")
        return self._hash_embedding(text)

    def _hash_embedding(self, text: str) -> List[float]:
        hash_bytes = hashlib.md5(text.encode()).digest()
        return [float(b) / 255.0 for b in hash_bytes]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        dot_product = sum(x * y for x, y in zip(a, b))
        if dot_product == 0:
            return 0.0
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)

    def _classify_by_embeddings(self, text: str) -> Tuple[Optional[str], float]:
        start = time.perf_counter()
        if not self._training_vectors:
            import logging
            logging.getLogger(__name__).debug("No training vectors available for embedding classification")
            return None, 0.0
        try:
            text_embedding = self._get_embedding(text)
            best_match = None
            best_score = 0.0
            all_scores: List[float] = []  # ✅ v4.8.0: tracking para feature variance
            for task_type, examples in self._training_vectors.items():
                for example_vec in examples:
                    similarity = self._cosine_similarity(text_embedding, example_vec)
                    all_scores.append(similarity)  # ✅ v4.8.0
                    if similarity > best_score:
                        best_score = similarity
                        best_match = task_type
            # ✅ v4.8.0: registrar std de features para ajuste dinámico del threshold
            self._record_feature_std(all_scores)
            duration = time.perf_counter() - start
            from src.router.classifier_core import _log_router
            if best_score >= self.threshold and best_match:
                _log_router(text[:50], best_match, "embeddings", best_score, duration=duration)
                return best_match, best_score
            _log_router(text[:50], best_match or "none", "embeddings_miss", best_score, duration=duration)
            return None, best_score
        except Exception as e:
            duration = time.perf_counter() - start
            try:
                from src.router.classifier_core import _log_error
                _log_error(e, "IntentClassifierCore._classify_by_embeddings", extra={"text_len": len(text)})
            except Exception as _log_exc:
                # R3: `_log_error` es deferred (ciclo de import); si falla, ya
                # se avisa abajo con el `warning` del fallback a hash.
                # ⚠️ NO usar `logging.DEBUG` acá (ver `_get_embedding`).
                log_degraded(logger, _log_exc,
                             "IntentClassifierCore._classify_by_embeddings (_log_error)")
            import logging
            logging.getLogger(__name__).warning(f"Embedding classification failed: {e}")
            return None, 0.0

    def _record_feature_std(self, scores: List[float]) -> None:
        """
        ✅ v4.8.0: Registra la std de scores de similitud para el ajuste
        dinámico del threshold. Si los scores tienen alta varianza
        (muchas similitudes similares), el clasificador está "indeciso"
        y debemos ser más estrictos.
        """
        if not scores or len(scores) < 2:
            self._last_feature_std = 0.0
            return
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        self._last_feature_std = round(variance ** 0.5, 4)
