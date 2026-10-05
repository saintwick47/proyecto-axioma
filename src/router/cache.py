#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.3 — Caché de Router (L1 + L2 SQLite + L3 Semántico)
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/router/cache.py
# Autor: SaintWick
# Fecha: 2026-06-08
# Propósito: Caché de decisiones de ruteo con TTL + LRU
# ✅ v0.1.0 — L3 semántico: LanceDB + BGE-M3 vía singleton local.
# ✅ v0.2.0 — Refactor L3 extraído a cache_l3.py.
# ✅ v0.3.0 — MIGRACIÓN BGE-M3: Actualizado import a _ST_AVAILABLE.
# ✅ v0.4.0 — L3 WRITE SÍNCRONO: BGE-M3 CPU (~100ms).
# ✅ v0.5.0 — L2 SQLITE ACTIVADO: CacheL2 como backing store persistente.
# ✅ v0.5.1 (FASE 3):
#    - TTL L2→L1 preservado (evita Stale Routing).
#    - _cleanup_expired probabilístico (1% en SET, evita O(N) write).
#    - Skip L2 memory scan si L2 SQLite activo (elimina O(N) read).
# ✅ v0.5.2 — OPTIMIZACIÓN O(1): _l2_cache migrado de List a OrderedDict.
#    - get(), set(), delete() y LRU eviction en L2 fallback ahora son O(1).
# ✅ v0.6.3 — MIGRACIÓN ChromaDB → LanceDB:
#    - _CHROMA_AVAILABLE → _LANCE_AVAILABLE.
#    - Todos los checks y mensajes actualizados.
# ═══════════════════════════════════════════════════════════════
import sys
import builtins
import logging
import time
import json
import hashlib
import random
import threading
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import OrderedDict
from config.settings import settings
from config.paths import Paths
from src.domain.exceptions import RouterError
from src.utils.degradation import log_degraded

# ✅ v0.6.3: L3 refactored to cache_l3.py. Import seguro con flag LanceDB.
try:
    from .cache_l3 import SemanticCacheL3, _LANCE_AVAILABLE, _ST_AVAILABLE
except ImportError:
    try:
        from src.router.cache_l3 import SemanticCacheL3, _LANCE_AVAILABLE, _ST_AVAILABLE
    except (ImportError, AttributeError):
        # Fallback: intentar import viejo por si cache_l3.py no fue actualizado aún
        try:
            from .cache_l3 import SemanticCacheL3
            try:
                from .cache_l3 import _CHROMA_AVAILABLE as _LANCE_AVAILABLE
            except ImportError:
                _LANCE_AVAILABLE = False
            try:
                from .cache_l3 import _ST_AVAILABLE
            except ImportError:
                _ST_AVAILABLE = False
        except ImportError:
            try:
                from src.router.cache_l3 import SemanticCacheL3
                try:
                    from src.router.cache_l3 import _CHROMA_AVAILABLE as _LANCE_AVAILABLE
                except ImportError:
                    _LANCE_AVAILABLE = False
                try:
                    from src.router.cache_l3 import _ST_AVAILABLE
                except ImportError:
                    _ST_AVAILABLE = False
            except ImportError:
                _LANCE_AVAILABLE = False
                _ST_AVAILABLE = False
                SemanticCacheL3 = None  # type: ignore[misc,assignment]

# ✅ v0.5.0: L2 SQLite import seguro.
try:
    from .cache_l2 import CacheL2 as CacheL2Class
    _L2_AVAILABLE = True
except ImportError:
    try:
        from src.router.cache_l2 import CacheL2 as CacheL2Class
        _L2_AVAILABLE = True
    except ImportError:
        _L2_AVAILABLE = False

# ============================================================================
# LOGGING — Integración con DetailedLogger
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_router(query: str, task_type: str, model_selected: str,
                confidence: float, cache_hit: bool = False,
                duration: float = None, api_required: bool = False):
    """Helper para logging de decisiones de router."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_router_decision(query, task_type, model_selected,
                                    confidence, cache_hit, duration, api_required)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 cache.py:107] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str, extra: dict = None):
    """Helper para logging de errores."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error, context=context, extra_info=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 cache.py:118] excepción degradada (intencional): {_d2e}")

def _log_cache(op: str, level: str = "L1", key: str = "",
               items_count: int = None, duration: float = None,
               success: bool = True, hit: bool = False):
    """Helper para logging de operaciones de caché."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event("CACHE", f"[{level}] {op}: {key[:30] if key else ''} "
                          f"{'HIT' if hit else 'MISS'}",
                          extra={"operation": op, "level": level, "hit": hit,
                                 "items_count": items_count, "duration": duration})
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 cache.py:134] excepción degradada (intencional): {_d2e}")

# ═══════════════════════════════════════════════════════════════
# CLASE: CacheEntry
# ═══════════════════════════════════════════════════════════════
class CacheEntry:
    """Entrada individual de caché con metadata."""
    def __init__(self, key: str, value: Any, ttl_seconds: int = 300):
        self.key = key
        self.value = value
        self.created_at = datetime.now(timezone.utc)
        self.expires_at = self.created_at + timedelta(seconds=ttl_seconds)
        self.access_count = 0
        self.last_accessed = self.created_at

    def is_expired(self) -> bool:
        """Verifica si la entrada expiró."""
        return datetime.now(timezone.utc) > self.expires_at

    def access(self) -> None:
        """Registra acceso para LRU."""
        self.access_count += 1
        self.last_accessed = datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para persistencia."""
        return {
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "access_count": self.access_count,
        }

# ═══════════════════════════════════════════════════════════════
# CLASE: RouterCache
# ═══════════════════════════════════════════════════════════════
class RouterCache:
    """
    Caché de tres niveles para decisiones de ruteo.
    Niveles:
    - L1 (Exact Match): Cache en memoria para queries exactas (OrderedDict LRU)
    - L2 (SQLite): Cache persistente en disco como backing store (v0.5.0)
    - L3 (Semantic DB): LanceDB + BGE-M3 (extraído a cache_l3.py)

    Flujo de lectura: L1 (RAM) → L2 SQLite (Disco) → L3 (Semántico) → Miss
    Flujo de escritura: L1 + L2 SQLite (async) + L3 (sync)
    """
    def __init__(
        self,
        ttl_seconds: int = 300,
        max_size: int = 1000,
        persist_path: Optional[Path] = None,
        enable_l2: bool = True,
        enable_l3: bool = True,
        # ✅ MEDIDO (2026-10-03): default desde `settings.cache_semantica_umbral`
        # (0.60 servía respuestas de preguntas distintas: 0.603-0.635 medidos).
        l3_similarity_threshold: float = float(
            getattr(settings, "cache_semantica_umbral", 0.85)),
        l3_ttl_seconds: int = 3600,
        l3_max_entries: int = 500,
    ):
        start = time.time()
        _log_cache("INIT", "CACHE", "", 0, 0, True, False)

        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self.persist_path = persist_path or Paths.DATA / "router_cache.json"
        self.enable_l2 = enable_l2

        # L1: Exact match
        self._l1_cache: OrderedDict[str, CacheEntry] = OrderedDict()

        # ✅ v0.5.2: L2 Fallback en memoria (OrderedDict para O(1) lookup/eviction)
        self._l2_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._l2_max_size = max_size // 2

        # ✅ v0.5.0: L2 SQLite como backing store persistente
        self._l2_db: Optional['CacheL2Class'] = None
        if enable_l2 and _L2_AVAILABLE:
            try:
                self._l2_db = CacheL2Class(
                    max_entries=5000,
                    default_ttl=self.ttl_seconds * 2,  # L2 vive más que L1
                )
                logger.info("RouterCache: L2 SQLite backing store activado")
            except Exception as e:
                logger.warning(f"RouterCache: L2 SQLite no disponible: {e}")
                self._l2_db = None
        elif enable_l2 and not _L2_AVAILABLE:
            logger.debug("RouterCache: L2 SQLite solicitado pero módulo no disponible")

        # ✅ v0.6.3: L3 inyectado desde cache_l3.py (BGE-M3 local + LanceDB)
        self._l3: Optional[SemanticCacheL3] = None
        if enable_l3 and _LANCE_AVAILABLE and _ST_AVAILABLE:
            self._l3 = SemanticCacheL3(
                similarity_threshold=l3_similarity_threshold,
                ttl_seconds=l3_ttl_seconds,
                max_entries=l3_max_entries,
            )
        elif enable_l3 and not _LANCE_AVAILABLE:
            logger.warning("[RouterCache] L3 solicitado pero lancedb no disponible")
        elif enable_l3 and not _ST_AVAILABLE:
            logger.warning("[RouterCache] L3 solicitado pero sentence-transformers no disponible")

        # Estadísticas
        self._stats = {
            "l1_hits": 0, "l1_misses": 0,
            "l2_hits": 0, "l2_misses": 0,
            "l3_hits": 0, "l3_misses": 0,
            "evictions": 0, "expirations": 0,
        }

        self._lock = threading.RLock()

        # ✅ v0.5.0: Cargar L1 desde L2 SQLite al inicio (si L2 tiene datos y L1 vacío)
        self._load_persist()
        self._restore_l1_from_l2()

        duration = time.time() - start
        _log_cache("INIT", "CACHE", "initialized",
                   items_count=len(self._l1_cache), duration=duration, success=True)
        logger.info(f"RouterCache initialized: L1={max_size}, L2-DB={'✅' if self._l2_db else '❌'}, L3={'✅' if self._l3 else '❌'}, TTL={ttl_seconds}s")

    def _generate_key(self, query: str) -> str:
        """Genera key hash para query (SHA256, 16 chars)."""
        return hashlib.sha256(query.encode()).hexdigest()[:16]

    def _restore_l1_from_l2(self) -> int:
        """Restaura L1 desde L2 SQLite al inicio (promote de las más recientes)."""
        if not self._l2_db or len(self._l1_cache) > 0:
            return 0

        restored = 0
        try:
            stats = self._l2_db.get_stats()
            if stats.get("size", 0) == 0:
                return 0

            logger.debug(f"RouterCache: L2 SQLite tiene {stats.get('size', 0)} entries, L1 se llenará on-demand")
        except Exception as e:
            logger.debug(f"RouterCache: Error consultando L2 para restore: {e}")

        return restored

    def _load_persist(self) -> None:
        """Carga caché desde disco si existe (solo si NO hay L2 SQLite)."""
        if self._l2_db is not None:
            return

        start = time.time()
        if self.persist_path and self.persist_path.exists():
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_count = 0
                for entry_data in data.get("l1", []):
                    entry = CacheEntry(
                        key=entry_data["key"],
                        value=entry_data["value"],
                        ttl_seconds=self.ttl_seconds,
                    )
                    entry.created_at = datetime.fromisoformat(entry_data["created_at"])
                    entry.expires_at = datetime.fromisoformat(entry_data["expires_at"])
                    entry.access_count = entry_data.get("access_count", 0)
                    if not entry.is_expired():
                        self._l1_cache[entry.key] = entry
                        loaded_count += 1
                duration = time.time() - start
                _log_cache("LOAD_PERSIST", "CACHE", str(self.persist_path),
                           items_count=loaded_count, duration=duration, success=True)
            except Exception as e:
                duration = time.time() - start
                _log_error(e, "RouterCache._load_persist", extra={"persist_path": str(self.persist_path)})
                logger.warning(f"Failed to load cache persistence: {e}")

    def _save_persist(self) -> None:
        """Guarda caché a disco (solo si NO hay L2 SQLite)."""
        if self._l2_db is not None:
            return

        if sys.meta_path is None:
            return

        start = time.time()
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "l1": [entry.to_dict() for entry in self._l1_cache.values()],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            with builtins.open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            duration = time.time() - start
            _log_cache("SAVE_PERSIST", "CACHE", str(self.persist_path),
                       items_count=len(data['l1']), duration=duration, success=True)
        except Exception as e:
            duration = time.time() - start
            _log_error(e, "RouterCache._save_persist", extra={"persist_path": str(self.persist_path)})
            logger.warning(f"Failed to save cache persistence: {e}")

    def _evict_lru(self) -> None:
        """Evict entrada menos usada (LRU)."""
        if len(self._l1_cache) >= self.max_size:
            oldest_key = next(iter(self._l1_cache))
            del self._l1_cache[oldest_key]
            self._stats["evictions"] += 1
            _log_cache("EVICT_LRU", "L1", oldest_key, success=True)

    def _cleanup_expired(self) -> int:
        """Limpia entradas expiradas de L1 y L2."""
        start = time.time()
        expired_keys = [key for key, entry in self._l1_cache.items() if entry.is_expired()]
        for key in expired_keys:
            del self._l1_cache[key]
            self._stats["expirations"] += 1

        # ✅ v0.5.2: O(1) lookup en OrderedDict en vez de O(N) en List
        original_l2_size = len(self._l2_cache)
        expired_l2_keys = [k for k, e in self._l2_cache.items() if e.is_expired()]
        for k in expired_l2_keys:
            del self._l2_cache[k]
        self._stats["expirations"] += len(expired_l2_keys)

        total_expired = len(expired_keys) + len(expired_l2_keys)
        if total_expired > 0:
            duration = time.time() - start
            _log_cache("CLEANUP_EXPIRED", "CACHE", "", items_count=total_expired, duration=duration, success=True)
        return total_expired

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene valor de caché por query.
        Flujo: L1 (RAM) → L2 SQLite (Disco) → L3 (Semántico) → Miss
        """
        start = time.time()
        cache_key = self._generate_key(query)

        with self._lock:
            # L1: Exact match en RAM (O(1))
            if cache_key in self._l1_cache:
                entry = self._l1_cache[cache_key]
                if entry.is_expired():
                    del self._l1_cache[cache_key]
                    self._stats["expirations"] += 1
                    self._stats["l1_misses"] += 1
                    duration = time.time() - start
                    _log_cache("GET", "L1", query[:30], duration=duration, hit=False)
                else:
                    entry.access()
                    self._l1_cache.move_to_end(cache_key)
                    self._stats["l1_hits"] += 1
                    duration = time.time() - start
                    _log_cache("GET", "L1", query[:30], duration=duration, hit=True)
                    return entry.value
            else:
                self._stats["l1_misses"] += 1

            # ═══════════════════════════════════════════════════════════════
            # ✅ v0.5.2: O(1) lookup en L2 memoria si L2 SQLite no existe.
            # ═══════════════════════════════════════════════════════════════
            if self.enable_l2 and self._l2_db is None:
                entry = self._l2_cache.get(cache_key)
                if entry and not entry.is_expired():
                    entry.access()
                    self._l2_cache.move_to_end(cache_key) # LRU update
                    self._stats["l2_hits"] += 1
                    duration = time.time() - start
                    _log_cache("GET", "L2", query[:30], duration=duration, hit=True)
                    return entry.value
                elif entry and entry.is_expired():
                    del self._l2_cache[cache_key]

            if self.enable_l2 and self._l2_db is None:
                self._stats["l2_misses"] += 1

        # ✅ v0.5.1: L2 SQLite (fuera del lock, no bloquea I/O)
        if self._l2_db is not None:
            try:
                l2_result, l2_expires_at = self._l2_db.get(query)
                if l2_result is not None:
                    # Promote to L1 con TTL restante
                    ttl_remaining = self.ttl_seconds
                    if l2_expires_at:
                        try:
                            expires_dt = datetime.fromisoformat(l2_expires_at)
                            now_dt = datetime.now(timezone.utc)
                            delta = (expires_dt - now_dt).total_seconds()
                            ttl_remaining = max(1, int(delta))
                        except Exception as _d2e:
                            logging.getLogger(__name__).debug(f"[D2 cache.py:423] excepción degradada (intencional): {_d2e}")

                    with self._lock:
                        self._stats["l2_hits"] += 1
                        self._evict_lru()
                        self._l1_cache[cache_key] = CacheEntry(
                            key=cache_key, value=l2_result, ttl_seconds=ttl_remaining
                        )
                    duration = time.time() - start
                    _log_cache("GET", "L2-DB", query[:30], duration=duration, hit=True)
                    logger.debug(f"Cache L2-DB HIT (promoted to L1, ttl_rem={ttl_remaining}s): {query[:40]}")
                    return l2_result
            except Exception as e:
                logger.debug(f"Cache L2-DB query error: {e}")

        # ✅ v0.3.0: L3 se ejecuta FUERA del lock principal para no bloquear I/O
        if self._l3 is not None:
            l3_result = self._l3.get(query)
            if l3_result is not None:
                value, similarity = l3_result
                with self._lock:
                    self._stats["l3_hits"] += 1
                duration = time.time() - start
                _log_cache("GET", "L3", query[:30], duration=duration, hit=True)
                logger.debug(f"Cache L3 HIT (sim={similarity:.3f}): {query[:30]}")
                return value
            with self._lock:
                self._stats["l3_misses"] += 1

        duration = time.time() - start
        _log_cache("GET", "CACHE", query[:30], duration=duration, hit=False)
        return None

    def set(self, query: str, value: Dict[str, Any],
            ttl_seconds: Optional[int] = None, level: int = 1) -> bool:
        """Guarda valor en caché (L1 + L2 SQLite + L3)."""
        start = time.time()
        cache_key = self._generate_key(query)
        ttl = ttl_seconds or self.ttl_seconds

        with self._lock:
            # ═══════════════════════════════════════════════════════════════
            # ✅ FIX FASE 3: _cleanup_expired Probabilístico (1%).
            # ═══════════════════════════════════════════════════════════════
            if random.random() < 0.01:
                self._cleanup_expired()

            entry = CacheEntry(key=cache_key, value=value, ttl_seconds=ttl)

            if level == 1:
                self._evict_lru()
                self._l1_cache[cache_key] = entry
                duration = time.time() - start
                _log_cache("SET", "L1", query[:30], duration=duration, success=True)
            else:
                # ✅ v0.5.2: Eviction y set O(1) usando OrderedDict
                if len(self._l2_cache) >= self._l2_max_size:
                    self._l2_cache.popitem(last=False) # LRU eviction O(1)
                self._l2_cache[cache_key] = entry
                duration = time.time() - start
                _log_cache("SET", "L2", query[:30], duration=duration, success=True)

            # ✅ v0.5.0: Persistencia JSON solo si NO hay L2 SQLite
            if self._l2_db is None:
                total_ops = sum(self._stats.values())
                if total_ops % 100 == 0:
                    self._save_persist()

        # ✅ v0.5.0: Sync con L2 SQLite (fire-and-forget, no bloquea)
        if level == 1 and self._l2_db is not None:
            try:
                self._l2_db.set(query, value, ttl=ttl)
            except Exception as e:
                logger.debug(f"Cache L2-DB set error (non-blocking): {e}")

        # ✅ v0.4.0: L3 write síncrono.
        if level == 1 and self._l3 is not None:
            self._l3.set(query, value)
        return True

    def delete(self, query: str) -> bool:
        """Elimina entrada de caché (L1 + L2 SQLite)."""
        start = time.time()
        cache_key = self._generate_key(query)
        deleted = False

        with self._lock:
            if cache_key in self._l1_cache:
                del self._l1_cache[cache_key]
                deleted = True
                _log_cache("DELETE", "L1", query[:30], duration=time.time()-start, success=True)
            else:
                # ✅ v0.5.2: Delete O(1) en L2 fallback
                if cache_key in self._l2_cache:
                    del self._l2_cache[cache_key]
                    deleted = True
                    _log_cache("DELETE", "L2", query[:30], duration=time.time()-start, success=True)

        # ✅ v0.5.0: Eliminar de L2 SQLite también
        if self._l2_db is not None:
            try:
                self._l2_db.delete(query)
            except Exception as _d2e:
                logging.getLogger(__name__).debug(f"[D2 cache.py:526] excepción degradada (intencional): {_d2e}")

        if not deleted:
            duration = time.time() - start
            _log_cache("DELETE", "CACHE", query[:30], duration=duration, success=False)
        return deleted

    def clear(self) -> None:
        """Limpia toda la caché (L1 + L2 memoria + L2 SQLite + L3)."""
        start = time.time()
        with self._lock:
            l1_count = len(self._l1_cache)
            l2_count = len(self._l2_cache)
            self._l1_cache.clear()
            self._l2_cache.clear()
            if self._l2_db is None:
                self._save_persist()

        if self._l2_db is not None:
            try:
                self._l2_db.clear()
                logger.debug("RouterCache: L2 SQLite limpiado")
            except Exception as e:
                logger.debug(f"RouterCache: Error limpiando L2 SQLite: {e}")

        if self._l3 is not None:
            self._l3.clear()

        duration = time.time() - start
        _log_cache("CLEAR", "CACHE", "", items_count=l1_count + l2_count, duration=duration, success=True)
        logger.info(f"RouterCache cleared: L1={l1_count}, L2={l2_count}, L2-DB={'✅' if self._l2_db else '❌'}, L3={'✅' if self._l3 else '❌'}")

    def get_stats(self) -> Dict[str, Any]:
        """Obtiene estadísticas de caché."""
        start = time.time()
        with self._lock:
            total_hits = self._stats["l1_hits"] + self._stats["l2_hits"] + self._stats["l3_hits"]
            total_misses = self._stats["l1_misses"] + self._stats["l2_misses"] + self._stats["l3_misses"]
            total_requests = total_hits + total_misses
            hit_rate = total_hits / total_requests if total_requests > 0 else 0.0
            l1_hit_rate = self._stats["l1_hits"] / max(self._stats["l1_hits"] + self._stats["l1_misses"], 1)
            l2_hit_rate = self._stats["l2_hits"] / max(self._stats["l2_hits"] + self._stats["l2_misses"], 1)
            l3_hit_rate = self._stats["l3_hits"] / max(self._stats["l3_hits"] + self._stats["l3_misses"], 1)

            result = {
                "l1_size": len(self._l1_cache), "l1_max_size": self.max_size,
                "l2_size": len(self._l2_cache), "l2_max_size": self._l2_max_size,
                "l1_hits": self._stats["l1_hits"], "l1_misses": self._stats["l1_misses"],
                "l2_hits": self._stats["l2_hits"], "l2_misses": self._stats["l2_misses"],
                "l3_hits": self._stats["l3_hits"], "l3_misses": self._stats["l3_misses"],
                "total_hits": total_hits, "total_misses": total_misses,
                "hit_rate": round(hit_rate, 4),
                "l1_hit_rate": round(l1_hit_rate, 4), "l2_hit_rate": round(l2_hit_rate, 4),
                "l3_hit_rate": round(l3_hit_rate, 4),
                "evictions": self._stats["evictions"], "expirations": self._stats["expirations"],
                "ttl_seconds": self.ttl_seconds,
                "l3": self._l3.get_stats() if self._l3 is not None else {"available": False},
            }

        if self._l2_db is not None:
            try:
                result["l2_db"] = self._l2_db.get_stats()
            except Exception:
                result["l2_db"] = {"available": False, "error": True}
        else:
            result["l2_db"] = {"available": False}

        duration = time.time() - start
        _log_cache("GET_STATS", "CACHE", "", duration=duration, success=True)
        return result

    def get_all_keys(self) -> List[str]:
        """Retorna todas las keys en caché."""
        with self._lock:
            return list(self._l1_cache.keys()) + list(self._l2_cache.keys())

    def export(self) -> Dict[str, Any]:
        """Exporta toda la caché a diccionario."""
        with self._lock:
            return {
                "l1": {k: v.to_dict() for k, v in self._l1_cache.items()},
                "l2": [e.to_dict() for e in self._l2_cache.values()],
                "stats": self.get_stats(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }

    def save(self, path: Optional[Path] = None) -> Path:
        """Guarda caché a archivo."""
        save_path = path or self.persist_path
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = self.export()
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        logger.info(f"RouterCache saved to {save_path}")
        return save_path

    def load(self, path: Path) -> bool:
        """Carga caché desde archivo."""
        if not path.exists():
            logger.warning(f"Cache file not found: {path}")
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self._l1_cache.clear()
                self._l2_cache.clear()
                loaded_count = 0
                for key, entry_data in data.get("l1", {}).items():
                    entry = CacheEntry(key=entry_data["key"], value=entry_data["value"], ttl_seconds=self.ttl_seconds)
                    if not entry.is_expired():
                        self._l1_cache[key] = entry
                        loaded_count += 1
                logger.info(f"RouterCache loaded from {path}: {loaded_count} entries")
                return True
        except Exception as e:
            _log_error(e, "RouterCache.load", extra={"path": str(path)})
            logger.error(f"Failed to load cache: {e}")
            return False

    def __len__(self) -> int:
        return len(self._l1_cache) + len(self._l2_cache)

    def __repr__(self) -> str:
        try:
            stats = self.get_stats()
            l2_db_status = "✅" if self._l2_db else "❌"
            return (f"RouterCache(L1={len(self._l1_cache)}/{self.max_size}, "
                    f"L2-DB={l2_db_status}, "
                    f"hit_rate={stats['hit_rate']:.2%})")
        except Exception:
            return f"RouterCache(L1={len(self._l1_cache)}, L2={len(self._l2_cache)})"

    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._save_persist()
        return False
    def __del__(self):
        try:
            self._save_persist()
        except Exception as e:
            # R3: destructor — nunca puede propagar (posible shutdown del intérprete).
            log_degraded(logger, e, "RouterCache.__del__ (_save_persist)")

# ═══════════════════════════════════════════════════════════════
# SINGLETON — get_router_cache / reset_router_cache
# ═══════════════════════════════════════════════════════════════
_router_cache_instance: Optional[RouterCache] = None
_router_cache_lock = threading.Lock()

def get_router_cache(
    ttl_seconds: int = 300,
    max_size: int = 1000,
    persist_path: Optional[Path] = None,
    enable_l2: bool = True,
    enable_l3: bool = True,
    # ✅ MEDIDO (2026-10-03): 0.60 daba falsos positivos; el default sale de
    # `settings.cache_semantica_umbral` (0.85).
    l3_similarity_threshold: float = float(
        getattr(settings, "cache_semantica_umbral", 0.85)),
) -> RouterCache:
    """Retorna la instancia singleton de RouterCache."""
    global _router_cache_instance
    if _router_cache_instance is None:
        with _router_cache_lock:
            if _router_cache_instance is None:
                _router_cache_instance = RouterCache(
                    ttl_seconds=ttl_seconds, max_size=max_size,
                    persist_path=persist_path, enable_l2=enable_l2,
                    enable_l3=enable_l3, l3_similarity_threshold=l3_similarity_threshold,
                )
                logger.info(f"RouterCache singleton creado: ttl={ttl_seconds}s, max_size={max_size}, enable_l3={enable_l3}")
    return _router_cache_instance

def reset_router_cache() -> None:
    """Destruye el singleton actual y guarda persistencia."""
    global _router_cache_instance
    with _router_cache_lock:
        if _router_cache_instance is not None:
            try:
                _router_cache_instance._save_persist()
            except Exception as e:
                # R3: el singleton se destruye igual; la persistencia es best-effort.
                log_degraded(logger, e, "reset_router_cache (_save_persist)")
        _router_cache_instance = None
    logger.info("RouterCache singleton destruido")

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — v0.6.3
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Import L3            | ✅ _CHROMA_AVAILABLE → _LANCE_AVAILABLE   | Migración ChromaDB→LanceDB|
# | Warning messages     | ✅ "chromadb" → "lancedb"                 | Consistencia post-migración|
# | Comments             | ✅ ChromaDB → LanceDB                     | Documentación actualizada  |
# | Fallback import      | ✅ Try _LANCE_AVAILABLE, fallback old flag | Robustez durante migración |
# ═══════════════════════════════════════════════════════════════
