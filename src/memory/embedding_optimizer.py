#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# embedding_optimizer.py - Optimizador de Embeddings para Memoria Semántica
# Ruta: /ruta/a/axioma/src/memory/embedding_optimizer.py
# Autor: SaintWick
# Versión: 0.5.2
#
# Proporciona caché LRU para embeddings, caché de resultados de búsqueda,
# generación por lotes, y optimización dinámica de umbrales de similitud.
# Reduce latencia y consumo de recursos al evitar recomputar embeddings
# para textos repetidos y al agrupar peticiones en batches.
# ═══════════════════════════════════════════════════════════════
import logging
import time
import hashlib
import threading
from typing import Optional, Dict, Any, List, Callable, Tuple
from datetime import datetime, timezone
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass, field
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import MemoryError
from src.utils.degradation import log_degraded

# ============================================================================
# LOGGING — Integración con DetailedLogger (OPTIMIZADO)
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)


def _log_memory(op: str, collection: str = "", session_id: str = "",
                items_count: int = None, duration: float = None,
                success: bool = True, db_path: str = ""):
    """Helper para logging de operaciones de memoria."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_memory_operation(op, collection, session_id, items_count, duration, success, db_path)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/embedding_optimizer.py:_log_memory")


def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if getattr(log, "is_initialized", False):
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/embedding_optimizer.py:_log_error")


# ============================================================================
# ✅ OPTIMIZACIÓN: Cache LRU para Embeddings (CORREGIDO)
# ============================================================================
@dataclass
class EmbeddingCacheEntry:
    """Entrada individual en el cache de embeddings."""
    text_hash: str
    embedding: List[float]
    created_at: datetime
    accessed_at: datetime
    access_count: int = 1
    model_name: str = "default"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "text_hash": self.text_hash,
            "embedding_dim": len(self.embedding),
            "created_at": self.created_at.isoformat(),
            "accessed_at": self.accessed_at.isoformat(),
            "access_count": self.access_count,
            "model_name": self.model_name,
        }


class EmbeddingCache:
    """
    Cache LRU para embeddings generados.

    Features:
    - Thread-safe con threading.Lock
    - Hash SHA256 para keys únicas
    - LRU eviction cuando se excede max_size
    - Estadísticas de hit/miss/eviction
    - TTL opcional para entradas

    ✅ CORREGIDO v0.5.1: LRU eviction funciona correctamente
    ✅ OPTIMIZADO v0.5.1: __slots__ para reducir memoria
    """

    __slots__ = ['max_size', 'ttl_seconds', '_cache', '_lock', '_hits', '_misses', '_evictions']

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        """
        Inicializa cache de embeddings.

        Args:
            max_size: Máximo de entradas en cache (default: 1000)
            ttl_seconds: Tiempo de vida de entradas en segundos (default: 3600)
        """
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, EmbeddingCacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def _generate_key(self, text: str, model_name: str = "default") -> str:
        """Genera key única para texto + modelo."""
        key_string = f"{text}::{model_name}"
        return hashlib.sha256(key_string.encode()).hexdigest()[:16]

    def _is_expired(self, entry: EmbeddingCacheEntry) -> bool:
        """Verifica si una entrada expiró por TTL."""
        if self.ttl_seconds <= 0:
            return False
        age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
        return age > self.ttl_seconds

    def _evict_if_needed(self):
        """Evict entradas si se excede max_size."""
        while len(self._cache) >= self.max_size:
            self._cache.popitem(last=False)
            self._evictions += 1

    def get(self, text: str, model_name: str = "default") -> Optional[List[float]]:
        """
        Obtiene embedding del cache.

        Args:
            text: Texto para buscar embedding
            model_name: Nombre del modelo

        Returns:
            Lista de floats del embedding o None si no existe/expiró
        """
        cache_key = self._generate_key(text, model_name)
        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                if self._is_expired(entry):
                    del self._cache[cache_key]
                    self._misses += 1
                    return None
                entry.accessed_at = datetime.now(timezone.utc)
                entry.access_count += 1
                self._cache.move_to_end(cache_key)
                self._hits += 1
                return entry.embedding
            self._misses += 1
            return None

    def set(self, text: str, embedding: List[float], model_name: str = "default") -> None:
        """
        Guarda embedding en cache.

        ✅ CORREGIDO v0.5.1: Eviction ANTES de agregar para LRU correcto

        Args:
            text: Texto asociado al embedding
            embedding: Vector de embedding a guardar
            model_name: Nombre del modelo usado
        """
        cache_key = self._generate_key(text, model_name)
        now = datetime.now(timezone.utc)
        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                entry.embedding = embedding
                entry.accessed_at = now
                entry.access_count += 1
                self._cache.move_to_end(cache_key)
            else:
                # ✅ CORREGIDO: Eviction ANTES de agregar nueva entrada
                while len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)
                    self._evictions += 1

                entry = EmbeddingCacheEntry(
                    text_hash=cache_key,
                    embedding=embedding,
                    created_at=now,
                    accessed_at=now,
                    access_count=1,
                    model_name=model_name,
                )
                self._cache[cache_key] = entry

    def get_or_compute(
        self,
        text: str,
        compute_fn: Callable[[str], List[float]],
        model_name: str = "default",
    ) -> List[float]:
        """
        Obtiene embedding del cache o lo computa si no existe.

        ✅ CORREGIDO v0.5.1: Pasa text a compute_fn(text)

        Args:
            text: Texto a embedir
            compute_fn: Función que computa el embedding (recibe text)
            model_name: Nombre del modelo

        Returns:
            Vector de embedding
        """
        cached = self.get(text, model_name)
        if cached is not None:
            return cached

        # ✅ CORREGIDO: Pasar text a compute_fn
        embedding = compute_fn(text)
        # ✅ FIX AUDITORÍA: no cachear None — compute_fn puede devolver None
        # (ej. modelo de embeddings no disponible) sin lanzar excepción.
        # Cachearlo pisaría una entrada previa válida si compute_fn falla de
        # forma intermitente para el mismo texto.
        if embedding is not None:
            self.set(text, embedding, model_name)
        return embedding

    def clear(self) -> None:
        """Limpia completamente el cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def cleanup_expired(self) -> int:
        """Limpia entradas expiradas por TTL."""
        removed = 0
        with self._lock:
            expired_keys = [
                key for key, entry in self._cache.items()
                if self._is_expired(entry)
            ]
            for key in expired_keys:
                del self._cache[key]
                removed += 1
        return removed

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del cache."""
        total = self._hits + self._misses
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": round(self._hits / max(total, 1), 4),
                "ttl_seconds": self.ttl_seconds,
            }


# ============================================================================
# ✅ OPTIMIZACIÓN: Query Cache para Búsquedas Semánticas (CORREGIDO)
# ============================================================================
@dataclass
class QueryCacheEntry:
    """Entrada para cache de resultados de búsqueda."""
    query_hash: str
    results: List[Dict[str, Any]]
    created_at: datetime
    accessed_at: datetime
    access_count: int = 1
    min_similarity: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario."""
        return {
            "query_hash": self.query_hash,
            "results_count": len(self.results),
            "created_at": self.created_at.isoformat(),
            "accessed_at": self.accessed_at.isoformat(),
            "access_count": self.access_count,
            "min_similarity": self.min_similarity,
        }


class QueryResultCache:
    """
    Cache para resultados de búsqueda semántica.

    Evita repetir búsquedas idénticas en la memoria vectorial.
    Útil para queries frecuentes en sesiones de chat.

    ✅ CORREGIDO v0.5.1: LRU eviction funciona correctamente
    ✅ OPTIMIZADO v0.5.1: __slots__ para reducir memoria
    """

    __slots__ = ['max_size', 'ttl_seconds', '_cache', '_lock', '_hits', '_misses']

    def __init__(self, max_size: int = 500, ttl_seconds: int = 300):
        """
        Inicializa cache de queries.

        Args:
            max_size: Máximo de entradas (default: 500)
            ttl_seconds: TTL en segundos (default: 300 = 5 min)
        """
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, QueryCacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _generate_key(self, query: str, min_similarity: float) -> str:
        """Genera key única para query + threshold."""
        key_string = f"{query}::{min_similarity}"
        return hashlib.sha256(key_string.encode()).hexdigest()[:16]

    def _is_expired(self, entry: QueryCacheEntry) -> bool:
        """Verifica si una entrada expiró."""
        if self.ttl_seconds <= 0:
            return False
        age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
        return age > self.ttl_seconds

    def get(self, query: str, min_similarity: float = 0.5) -> Optional[List[Dict[str, Any]]]:
        """Obtiene resultados del cache."""
        cache_key = self._generate_key(query, min_similarity)
        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                if self._is_expired(entry):
                    del self._cache[cache_key]
                    self._misses += 1
                    return None
                entry.accessed_at = datetime.now(timezone.utc)
                entry.access_count += 1
                self._cache.move_to_end(cache_key)
                self._hits += 1
                return entry.results
            self._misses += 1
            return None

    def set(self, query: str, results: List[Dict[str, Any]], min_similarity: float = 0.5) -> None:
        """
        Guarda resultados en cache.

        ✅ CORREGIDO v0.5.1: Eviction ANTES de agregar para LRU correcto

        Args:
            query: Query de búsqueda
            results: Resultados a cachear
            min_similarity: Mínima similitud usada
        """
        cache_key = self._generate_key(query, min_similarity)
        now = datetime.now(timezone.utc)
        with self._lock:
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                entry.results = results
                entry.accessed_at = now
                entry.access_count += 1
                self._cache.move_to_end(cache_key)
            else:
                # ✅ CORREGIDO: Eviction ANTES de agregar nueva entrada
                while len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)

                entry = QueryCacheEntry(
                    query_hash=cache_key,
                    results=results,
                    created_at=now,
                    accessed_at=now,
                    access_count=1,
                    min_similarity=min_similarity,
                )
                self._cache[cache_key] = entry

    def get_or_search(
        self,
        query: str,
        search_fn: Callable[[], List[Dict[str, Any]]],
        min_similarity: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Obtiene del cache o ejecuta búsqueda."""
        cached = self.get(query, min_similarity)
        if cached is not None:
            return cached
        results = search_fn()
        self.set(query, results, min_similarity)
        return results

    def clear(self) -> None:
        """Limpia el cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "ttl_seconds": self.ttl_seconds,
        }


# ============================================================================
# ✅ OPTIMIZACIÓN: Batch Embedding Generator (OPTIMIZADO)
# ============================================================================
class BatchEmbeddingGenerator:
    """
    Generador de embeddings en batch para múltiples textos.

    Optimiza la generación de embeddings procesando múltiples
    textos en una sola llamada al modelo (cuando es soportado).

    ✅ OPTIMIZADO v0.5.1: Mejor error handling y métricas
    """

    __slots__ = ['batch_size', 'max_workers', '_total_generated', '_total_batches', '_errors']

    def __init__(self, batch_size: int = 32, max_workers: int = 4):
        """
        Inicializa generador en batch.

        Args:
            batch_size: Tamaño de batch (default: 32)
            max_workers: Máximo workers para parallelismo (default: 4)
        """
        self.batch_size = batch_size
        self.max_workers = max_workers
        self._total_generated = 0
        self._total_batches = 0
        self._errors = 0

    def generate(
        self,
        texts: List[str],
        model_fn: Callable[[List[str]], List[List[float]]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Optional[List[float]]]:
        """
        Genera embeddings para múltiples textos.

        Args:
            texts: Lista de textos a embedir
            model_fn: Función que genera embeddings (recibe lista de textos)
            progress_callback: Callback opcional para progreso (current, total)

        Returns:
            Lista de embeddings (None para items fallidos)
        """
        start = time.perf_counter()
        total = len(texts)
        embeddings: List[Optional[List[float]]] = [None] * total

        for i in range(0, total, self.batch_size):
            batch_texts = texts[i:i + self.batch_size]
            batch_start = time.perf_counter()

            try:
                batch_embeddings = model_fn(batch_texts)
                for j, emb in enumerate(batch_embeddings):
                    embeddings[i + j] = emb
                self._total_batches += 1
                self._total_generated += len(batch_embeddings)
            except Exception as e:
                logger.error(f"Batch embedding failed for batch {i//self.batch_size}: {e}")
                self._errors += len(batch_texts)
                for j, text in enumerate(batch_texts):
                    try:
                        embeddings[i + j] = model_fn([text])[0]
                        self._total_generated += 1
                    except Exception as e:
                        log_degraded(logger, e, "src/memory/embedding_optimizer.py:generate")
                        embeddings[i + j] = None
                        self._errors += 1

            if progress_callback:
                progress_callback(i + len(batch_texts), total)

            batch_duration = time.perf_counter() - batch_start
            logger.debug(
                f"Batch {i//self.batch_size + 1}: {len(batch_texts)} texts "
                f"in {batch_duration:.4f}s ({len(batch_texts)/max(batch_duration, 0.001):.1f} texts/s)"
            )

        total_duration = time.perf_counter() - start
        throughput = total / max(total_duration, 0.001)
        logger.info(
            f"BatchEmbeddingGenerator: {total} texts, {self._total_generated} success, "
            f"{self._errors} errors, {throughput:.1f} texts/s"
        )
        return embeddings

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del generador."""
        return {
            "batch_size": self.batch_size,
            "max_workers": self.max_workers,
            "total_generated": self._total_generated,
            "total_batches": self._total_batches,
            "errors": self._errors,
            "success_rate": round(self._total_generated / max(self._total_generated + self._errors, 1), 4),
        }

    def reset_stats(self) -> None:
        """Reinicia estadísticas."""
        self._total_generated = 0
        self._total_batches = 0
        self._errors = 0


# ============================================================================
# ✅ OPTIMIZACIÓN: Similarity Threshold Optimizer (OPTIMIZADO)
# ============================================================================
class SimilarityThresholdOptimizer:
    """
    Optimizador dinámico de threshold de similitud.

    Ajusta automáticamente el threshold mínimo de similitud
    basado en el historial de búsquedas y feedback implícito.

    ✅ OPTIMIZADO v0.5.1: Mejor cálculo de percentiles
    """

    __slots__ = ['percentile', 'min_threshold', 'max_threshold', '_current_threshold', '_history', '_score_distributions']

    def __init__(self, percentile: int = 75, min_threshold: float = 0.3, max_threshold: float = 0.9):
        """
        Inicializa optimizador.

        Args:
            percentile: Percentil para cutoff (default: 75)
            min_threshold: Threshold mínimo (default: 0.3)
            max_threshold: Threshold máximo (default: 0.9)
        """
        self.percentile = percentile
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self._current_threshold = 0.5
        self._history: List[float] = []
        self._score_distributions: List[List[float]] = []

    def optimize(self, scores: List[float]) -> float:
        """Calcula threshold óptimo basado en distribución de scores."""
        if not scores:
            return self._current_threshold

        self._score_distributions.append(scores)
        sorted_scores = sorted(scores)
        index = int(len(sorted_scores) * self.percentile / 100)
        optimal = sorted_scores[min(index, len(sorted_scores) - 1)]
        optimal = max(self.min_threshold, min(self.max_threshold, optimal))
        self._history.append(optimal)
        self._current_threshold = optimal
        return optimal

    def get_current_threshold(self) -> float:
        """Retorna threshold actual."""
        return self._current_threshold

    def set_threshold(self, threshold: float) -> None:
        """Setea threshold manualmente."""
        self._current_threshold = max(self.min_threshold, min(self.max_threshold, threshold))
        self._history.append(self._current_threshold)

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas del optimizador."""
        avg_threshold = sum(self._history) / max(len(self._history), 1)
        return {
            "current_threshold": self._current_threshold,
            "average_threshold": round(avg_threshold, 4),
            "percentile": self.percentile,
            "min_threshold": self.min_threshold,
            "max_threshold": self.max_threshold,
            "adjustments_count": len(self._history),
            "distributions_analyzed": len(self._score_distributions),
        }

    def reset(self) -> None:
        """Reinicia el optimizador."""
        self._current_threshold = 0.5
        self._history.clear()
        self._score_distributions.clear()


# ============================================================================
# CLASE PRINCIPAL: EmbeddingOptimizer (OPTIMIZADO + CORREGIDO)
# ============================================================================
class EmbeddingOptimizer:
    """
    Optimizador central para operaciones de embedding.

    ✅ CORREGIDO v0.5.1: Manejo de errores en get_embedding()
    ✅ OPTIMIZADO v0.5.1: __slots__ para reducir memoria
    """

    __slots__ = ['embedding_cache', 'query_cache', 'batch_generator', 'threshold_optimizer', '_total_operations', '_total_time_saved']

    def __init__(
        self,
        embedding_cache_size: int = 1000,
        embedding_cache_ttl: int = 3600,
        query_cache_size: int = 500,
        query_cache_ttl: int = 300,
        batch_size: int = 32,
        similarity_percentile: int = 75,
    ):
        """Inicializa optimizador."""
        start = time.perf_counter()
        self.embedding_cache = EmbeddingCache(
            max_size=embedding_cache_size,
            ttl_seconds=embedding_cache_ttl,
        )
        self.query_cache = QueryResultCache(
            max_size=query_cache_size,
            ttl_seconds=query_cache_ttl,
        )
        self.batch_generator = BatchEmbeddingGenerator(batch_size=batch_size)
        self.threshold_optimizer = SimilarityThresholdOptimizer(percentile=similarity_percentile)
        self._total_operations = 0
        self._total_time_saved = 0.0
        duration = time.perf_counter() - start
        logger.info(f"EmbeddingOptimizer initialized in {duration:.4f}s")
        _log_memory("optimizer_init", "embedding", duration=duration, success=True)

    def _hash_embedding(self, text: str) -> List[float]:
        """
        ✅ NUEVO: Fallback embedding hash cuando falla la generación real.

        Args:
            text: Texto a convertir

        Returns:
            Vector de 16 floats (hash MD5 normalizado)
        """
        hash_bytes = hashlib.md5(text.encode()).digest()
        return [float(b) / 255.0 for b in hash_bytes]

    def get_embedding(
        self,
        text: str,
        model_fn: Callable[[str], List[float]],
        model_name: str = "default",
        use_cache: bool = True,
    ) -> List[float]:
        """
        Obtiene embedding con cache.

        ✅ CORREGIDO v0.5.1: Try/except para manejar errores de compute_fn

        Args:
            text: Texto a embedir
            model_fn: Función que genera embedding (recibe text)
            model_name: Nombre del modelo
            use_cache: Si usar cache

        Returns:
            Vector de embedding (o fallback hash si falla)
        """
        start = time.perf_counter()
        if use_cache:
            cached = self.embedding_cache.get(text, model_name)
            if cached is not None:
                duration = time.perf_counter() - start
                self._total_time_saved += duration
                self._total_operations += 1
                return cached

        # ✅ CORREGIDO: Try/except para manejar errores de compute_fn
        try:
            embedding = self.embedding_cache.get_or_compute(text, model_fn, model_name)
        except Exception as e:
            logger.warning(f"Embedding generation failed: {e}, using hash fallback")
            embedding = self._hash_embedding(text)

        duration = time.perf_counter() - start
        self._total_operations += 1
        _log_memory("get_embedding", "embedding", items_count=1, duration=duration, success=True)
        return embedding

    def get_embeddings_batch(
        self,
        texts: List[str],
        model_fn: Callable[[List[str]], List[List[float]]],
        model_name: str = "default",
        use_cache: bool = True,
    ) -> List[List[float]]:
        """Obtiene embeddings en batch con cache."""
        start = time.perf_counter()
        hits: Dict[int, List[float]] = {}
        misses: List[Tuple[int, str]] = []

        if use_cache:
            for i, text in enumerate(texts):
                cached = self.embedding_cache.get(text, model_name)
                if cached is not None:
                    hits[i] = cached
                else:
                    misses.append((i, text))

        if misses:
            miss_texts = [text for _, text in misses]
            batch_embeddings = self.batch_generator.generate(miss_texts, model_fn)
            for idx, (orig_idx, text) in enumerate(misses):
                emb = batch_embeddings[idx]
                if emb is not None:
                    self.embedding_cache.set(text, emb, model_name)
                    hits[orig_idx] = emb

        results = [hits[i] for i in range(len(texts))]
        duration = time.perf_counter() - start
        self._total_operations += len(texts)
        _log_memory("get_embeddings_batch", "embedding", items_count=len(texts), duration=duration, success=True)
        return results

    def search(
        self,
        query: str,
        search_fn: Callable[[], List[Dict[str, Any]]],
        min_similarity: float = 0.5,
        use_cache: bool = True,
        optimize_threshold: bool = False,
    ) -> List[Dict[str, Any]]:
        """Búsqueda semántica con cache y threshold optimization."""
        start = time.perf_counter()
        if optimize_threshold:
            min_similarity = self.threshold_optimizer.get_current_threshold()

        def wrapped_search():
            results = search_fn()
            if optimize_threshold and results:
                scores = [r.get("similarity", 0.5) for r in results]
                self.threshold_optimizer.optimize(scores)
            return results

        if use_cache:
            results = self.query_cache.get_or_search(query, wrapped_search, min_similarity)
        else:
            results = wrapped_search()

        duration = time.perf_counter() - start
        self._total_operations += 1
        _log_memory("search", "semantic", items_count=len(results), duration=duration, success=True)
        return results

    def cleanup(self) -> Dict[str, int]:
        """Limpia entradas expiradas de todos los caches."""
        embedding_expired = self.embedding_cache.cleanup_expired()
        return {"embedding_expired": embedding_expired}

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas completas del optimizador."""
        return {
            "total_operations": self._total_operations,
            "total_time_saved_seconds": round(self._total_time_saved, 4),
            "embedding_cache": self.embedding_cache.get_stats(),
            "query_cache": self.query_cache.get_stats(),
            "batch_generator": self.batch_generator.get_stats(),
            "threshold_optimizer": self.threshold_optimizer.get_stats(),
        }

    def reset(self) -> None:
        """Reinicia todos los componentes."""
        self.embedding_cache.clear()
        self.query_cache.clear()
        self.batch_generator.reset_stats()
        self.threshold_optimizer.reset()
        self._total_operations = 0
        self._total_time_saved = 0.0
        logger.info("EmbeddingOptimizer reset")
        _log_memory("optimizer_reset", "embedding", success=True)


# ============================================================================
# SINGLETON GLOBAL (OPTIMIZADO)
# ============================================================================
_optimizer_instance: Optional[EmbeddingOptimizer] = None
_optimizer_lock = threading.Lock()


def get_embedding_optimizer(
    embedding_cache_size: int = 1000,
    embedding_cache_ttl: int = 3600,
    query_cache_size: int = 500,
    query_cache_ttl: int = 300,
    batch_size: int = 32,
    similarity_percentile: int = 75,
) -> EmbeddingOptimizer:
    """
    Obtiene instancia singleton del EmbeddingOptimizer.

    Args:
        embedding_cache_size: Max entradas cache embeddings
        embedding_cache_ttl: TTL cache embeddings
        query_cache_size: Max entradas cache queries
        query_cache_ttl: TTL cache queries
        batch_size: Tamaño de batch
        similarity_percentile: Percentil para threshold

    Returns:
        Instancia de EmbeddingOptimizer
    """
    global _optimizer_instance
    if _optimizer_instance is None:
        with _optimizer_lock:
            if _optimizer_instance is None:
                _optimizer_instance = EmbeddingOptimizer(
                    embedding_cache_size=embedding_cache_size,
                    embedding_cache_ttl=embedding_cache_ttl,
                    query_cache_size=query_cache_size,
                    query_cache_ttl=query_cache_ttl,
                    batch_size=batch_size,
                    similarity_percentile=similarity_percentile,
                )
    return _optimizer_instance


def reset_embedding_optimizer() -> None:
    """Reinicia el singleton del optimizador."""
    global _optimizer_instance
    if _optimizer_instance:
        _optimizer_instance.reset()
        _optimizer_instance = None
