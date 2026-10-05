#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.2 — Singleton Centralizado de Embeddings (BGE-M3)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/embedding_model_singleton.py
# Autor: SaintWick
# Propósito: UNA SOLA instancia de BAAI/bge-m3 compartida por:
#            - cache_l3.py (Router)
#            - semantic_base.py (Memoria)
#            - semantic.py (Memoria)
#            - classifier_core.py (Clasificador)
#            - client_base.py (Redirección HTTP → local)
# ═══════════════════════════════════════════════════════════════
import logging
import time
import hashlib
import threading
import atexit
import importlib.util
from typing import Optional, List, Callable, TYPE_CHECKING
from config.settings import settings
from src.utils.degradation import log_degraded

if TYPE_CHECKING:   # ✅ Fase 0: sólo para el tipo — en ejecución no importa nada
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Modelo y dimensión únicos para todo el sistema
EMBED_MODEL_NAME = "BAAI/bge-m3"
EMBED_DIMENSION = 1024

# ✅ Fase 0 (2026-10-02, MEDIDO): la sonda de disponibilidad NO importa la
# biblioteca. `import sentence_transformers` arrastra torch + transformers +
# sklearn = **492 MB de Pss medidos** en TODO proceso que toque la memoria
# (consola, UI web y Rafael: los tres lo importaban al arrancar) aunque nunca
# se calcule un solo vector. `find_spec` no importa nada — mismo patrón que
# `src/utils/degradation.py::verificar_interprete` — y el import real vive
# ahora dentro de `load()`, que es el único lugar donde se usa.
_ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None


class EmbeddingModelSingleton:
    """
    Singleton que mantiene UNA SOLA instancia de BAAI/bge-m3.

    Garantiza:
    - Una sola carga en RAM (~2.2GB)
    - Dimensión consistente (1024) en todo el sistema
    - Sin dependencia HTTP/Ollama para embeddings
    - Descarga automática por inactividad (opcional)
    """
    _instance: Optional['EmbeddingModelSingleton'] = None
    _lock = threading.Lock()

    def __init__(self):
        self._model: Optional['SentenceTransformer'] = None
        self._loaded = False
        self._load_lock = threading.Lock()
        self._last_access = 0.0
        self._total_embeddings = 0
        self._total_errors = 0

    @classmethod
    def get_instance(cls) -> 'EmbeddingModelSingleton':
        """Retorna el singleton (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Destruye el singleton y libera RAM."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.unload()
                cls._instance = None

    def load(self) -> bool:
        """Carga el modelo (lazy, thread-safe)."""
        if self._loaded:
            self._last_access = time.time()
            return True

        if not _ST_AVAILABLE:
            logger.error("[EmbedSingleton] sentence-transformers no disponible")
            return False

        with self._load_lock:
            if self._loaded:
                self._last_access = time.time()
                return True

            try:
                logger.info(f"[EmbedSingleton] Cargando {EMBED_MODEL_NAME}...")
                start = time.time()
                # ✅ Fase 0: import diferido (es el ÚNICO uso real de la
                # biblioteca). Acá ya se pagó el costo de cargar el modelo
                # (~2,2 GB), así que los ~492 MB de la librería dejan de ser
                # un peaje fijo de arranque para todos los procesos.
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(EMBED_MODEL_NAME)
                self._loaded = True
                self._last_access = time.time()
                duration = time.time() - start
                logger.info(f"[EmbedSingleton] Cargado en {duration:.1f}s (~2.2GB RAM)")
                return True
            except Exception as e:
                logger.error(f"[EmbedSingleton] Error cargando modelo: {e}")
                return False

    def unload(self) -> None:
        """Descarga el modelo para liberar RAM."""
        with self._load_lock:
            if self._model is not None:
                logger.info("[EmbedSingleton] Descargando modelo (~2.2GB RAM liberada)")
                del self._model
                self._model = None
                self._loaded = False
                import gc
                gc.collect()

    def embed(self, text: str) -> Optional[List[float]]:
        """Genera embedding para un texto (dim 1024)."""
        if not self._loaded:
            if not self.load():
                return None

        self._last_access = time.time()
        try:
            vec = self._model.encode(
                text[:1000],
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            self._total_embeddings += 1
            return vec.tolist()
        except Exception as e:
            self._total_errors += 1
            logger.warning(f"[EmbedSingleton] Embed error: {e}")
            return None

    def embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Genera embeddings para batch de textos (dim 1024 c/u)."""
        if not self._loaded:
            if not self.load():
                return None

        self._last_access = time.time()
        try:
            vectors = self._model.encode(
                [t[:1000] for t in texts],
                normalize_embeddings=True,
                batch_size=32,
                show_progress_bar=False,
            )
            self._total_embeddings += len(texts)
            return [v.tolist() for v in vectors]
        except Exception as e:
            self._total_errors += len(texts)
            logger.warning(f"[EmbedSingleton] Batch embed error: {e}")
            return None

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def dimension(self) -> int:
        return EMBED_DIMENSION

    def get_stats(self) -> dict:
        return {
            "model": EMBED_MODEL_NAME,
            "dimension": EMBED_DIMENSION,
            "loaded": self._loaded,
            "total_embeddings": self._total_embeddings,
            "total_errors": self._total_errors,
            "last_access": self._last_access,
            "st_available": _ST_AVAILABLE,
        }


# ═══════════════════════════════════════════════════════════════
# FUNCIONES DE CONVENIENCIA
# ═══════════════════════════════════════════════════════════════
def get_embedding_model() -> EmbeddingModelSingleton:
    """Retorna el singleton del modelo de embeddings."""
    return EmbeddingModelSingleton.get_instance()


def get_embedding_fn() -> Callable[[str], List[float]]:
    """
    Retorna función compatible con EmbeddingOptimizer.get_embedding().
    Incluye fallback hash si falla el modelo.
    """
    model = get_embedding_model()

    def fn(text: str) -> List[float]:
        result = model.embed(text)
        if result is None:
            # Fallback hash (dim 16, no semántico pero funcional)
            hash_bytes = hashlib.md5(text.encode()).digest()
            return [float(b) / 255.0 for b in hash_bytes]
        return result

    return fn


def get_batch_embedding_fn() -> Callable[[List[str]], List[List[float]]]:
    """Retorna función batch compatible con EmbeddingOptimizer."""
    model = get_embedding_model()

    def fn(texts: List[str]) -> List[List[float]]:
        result = model.embed_batch(texts)
        if result is None:
            return [
                [float(b) / 255.0 for b in hashlib.md5(t.encode()).digest()]
                for t in texts
            ]
        return result

    return fn


def _atexit_unload() -> None:
    """Descarga el modelo de embeddings al salir del proceso.

    ✅ v0.6.9v (ISSUE-002): evita el ruido "cannot schedule new futures after
    interpreter shutdown" del pool de threads durante el cierre del intérprete.
    Sin un shutdown limpio, tasks en background intentaban programar futuros
    cuando el executor ya estaba cerrado → error espurio en los logs.
    """
    try:
        EmbeddingModelSingleton.reset()
    except Exception as _d2e:  # noqa: BLE001 — el atexit nunca debe lanzar
        log_degraded(logger, _d2e, "src/memory/embedding_model_singleton.py:_atexit_unload")


atexit.register(_atexit_unload)
