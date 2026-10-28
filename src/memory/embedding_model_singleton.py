#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.5.2 — Singleton Centralizado de Embeddings (BGE-M3)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/embedding_model_singleton.py
# Autor: SaintWick
# Propósito: UNA SOLA instancia del modelo de embeddings compartida por:
#            - cache_l3.py (Router)
#            - semantic_base.py (Memoria)
#            - semantic.py (Memoria)
#            - classifier_core.py (Clasificador)
#            - client_base.py (Redirección HTTP → local)
# ═══════════════════════════════════════════════════════════════
import logging
import os
import time
import hashlib
import threading
import atexit
import importlib.util
from typing import Optional, List, Callable, TYPE_CHECKING
from src.utils.degradation import log_degraded

if TYPE_CHECKING:   # ✅ Fase 0: sólo para el tipo — en ejecución no importa nada
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Modelo y dimensión únicos para todo el sistema
# ✅ 2026-10-27: F2LLM-v2-330M (Qwen3, 896 dims) reemplaza a BAAI/bge-m3 (1024).
# MEDIDO en consultas en español sobre el propio código: MRR@5 0.83-0.85 (empate con
# bge-m3), RAM pico 1.770 MB vs 3.237 MB (-45 %), índice 3x más rápido, contexto 40k
# vs 8k y 638 MB en disco vs 4,3 GB. Cuantizar bge-m3 en PyTorch NO bajaba la RAM
# (int8 subía a 6,3 GB): por eso se cambia de modelo en vez de cuantizar.
EMBED_MODEL_NAME = "codefuse-ai/F2LLM-v2-330M"
EMBED_DIMENSION = 896

# ── ✅ 2026-10-28 (MEDIDO): descarga del embedder por inactividad ──────────────
# El modelo ocupa **1030 MB de RSS** (medido con `resource.getrusage`) y se carga la primera
# vez que se usa (5-7 s, medido). Sin política se queda en RAM para siempre, aunque AXIOMA sólo
# esté chateando. Se descarga SÓLO si pasaron `AXIOMA_EMBED_IDLE_UNLOAD_S` segundos sin usarlo
# **y** la RAM del equipo supera `AXIOMA_EMBED_UNLOAD_RAM_RATIO` (0.80): así nadie paga los 5-7 s
# de recarga teniendo memoria de sobra. 0 en la primera variable = desactivado.
EMBED_IDLE_UNLOAD_S = int(os.environ.get("AXIOMA_EMBED_IDLE_UNLOAD_S", "900"))
EMBED_UNLOAD_RAM_RATIO = float(os.environ.get("AXIOMA_EMBED_UNLOAD_RAM_RATIO", "0.80"))

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
    Singleton que mantiene UNA SOLA instancia del modelo de embeddings.

    Garantiza:
    - Una sola carga en RAM (~1,8GB)
    - Dimensión consistente (896) en todo el sistema
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
        self._vigilante: Optional[threading.Thread] = None
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
                # Guardia: si algún día se cambia el modelo sin tocar EMBED_DIMENSION,
                # los vectores viejos y los esquemas de LanceDB quedarían inconsistentes.
                _fn_dim = getattr(self._model, "get_embedding_dimension", None) \
                    or self._model.get_sentence_embedding_dimension
                _dim_real = _fn_dim()
                if _dim_real != EMBED_DIMENSION:
                    logger.error(
                        f"[EmbedSingleton] DIMENSIÓN INCONSISTENTE: el modelo {EMBED_MODEL_NAME} "
                        f"da {_dim_real} dims y EMBED_DIMENSION dice {EMBED_DIMENSION}. "
                        f"Reindexá las tablas vectoriales.")
                self._loaded = True
                self._last_access = time.time()
                duration = time.time() - start
                logger.info(f"[EmbedSingleton] Cargado en {duration:.1f}s (~1.8GB RAM)")
                self._iniciar_vigilante()
                return True
            except Exception as e:
                logger.error(f"[EmbedSingleton] Error cargando modelo: {e}")
                return False

    def maybe_unload(self, ahora: Optional[float] = None,
                     ratio_ram: Optional[float] = None) -> bool:
        """Descarga el modelo si estuvo inactivo **y** hay presión de RAM.

        Args:
            ahora: instante de referencia (inyectable en tests; por defecto `time.time()`).
            ratio_ram: uso de RAM 0..1 (inyectable en tests; por defecto psutil).

        Returns:
            True si descargó el modelo.
        """
        if not self._loaded or self._model is None or EMBED_IDLE_UNLOAD_S <= 0:
            return False
        ahora = time.time() if ahora is None else ahora
        inactivo = ahora - self._last_access
        if inactivo < EMBED_IDLE_UNLOAD_S:
            return False
        if ratio_ram is None:
            try:
                import psutil
                ratio_ram = psutil.virtual_memory().percent / 100.0
            except Exception as exc:  # noqa: BLE001 — sin psutil no hay política, se mantiene cargado
                log_degraded(logger, exc, "no se pudo medir la RAM: el embedder sigue cargado")
                return False
        if ratio_ram < EMBED_UNLOAD_RAM_RATIO:
            return False
        logger.info(
            f"[EmbedSingleton] Descarga por inactividad ({inactivo:.0f}s sin uso) y RAM al "
            f"{ratio_ram * 100:.0f}% (umbral {EMBED_UNLOAD_RAM_RATIO * 100:.0f}%)")
        self.unload()
        return True

    def _iniciar_vigilante(self) -> None:
        """Hilo daemon que revisa cada minuto si corresponde descargar (una sola vez)."""
        if EMBED_IDLE_UNLOAD_S <= 0 or self._vigilante is not None:
            return

        def _lazo() -> None:
            while True:
                time.sleep(60)
                try:
                    self.maybe_unload()
                except Exception as exc:  # noqa: BLE001 — el vigilante nunca puede tumbar el proceso
                    log_degraded(logger, exc, "vigilante del embedder: descarga omitida")

        hilo = threading.Thread(target=_lazo, name="embed-idle-unload", daemon=True)
        hilo.start()
        self._vigilante = hilo

    def unload(self) -> None:
        """Descarga el modelo para liberar RAM."""
        with self._load_lock:
            if self._model is not None:
                logger.info("[EmbedSingleton] Descargando modelo (~1.8GB RAM liberada)")
                del self._model
                self._model = None
                self._loaded = False
                import gc
                gc.collect()

    def embed(self, text: str) -> Optional[List[float]]:
        """Genera embedding para un texto (dim 896)."""
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
        """Genera embeddings para batch de textos (dim 896 c/u)."""
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
