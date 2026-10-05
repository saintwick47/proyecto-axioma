#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — palace_storage.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/memory/palace_storage.py
#
# ✅ v0.6.9v (REFACTOR): capa de ALMACENAMIENTO semántico (LanceDB) extraída
#   de src/memory/gateway.py. gateway.py (el original) queda intacto como
#   orquestador de capas y delega aquí la persistencia/recall vectorial.
#
#   Qué se movió desde gateway.py (sin cambios de comportamiento):
#     - Bloque de import seguro LanceDB (lancedb / PalaceRecord /
#       _PALACE_LANCE_AVAILABLE / get_embedding_model / EMBED_DIMENSION)
#     - Clase `Palace` (store / recall / clear / close / _ensure_filter_columns)
#     - Constante `MEMPALACE_AVAILABLE`
#
#   API pública preservada en gateway.py: `MemoryGateway`, `TASK_TO_SOURCE`
#   (nada de lo que consume el resto del sistema cambió).
#
#   Import seguro para el Singleton de embeddings — ver AXIOMA_COMPLETE_CONTEXT.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import json
import logging
import importlib.util
from typing import Any, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)
lancedb = None  # type: ignore[assignment]
EMBED_DIMENSION = 1024


def _fallback_embedding_model():  # type: ignore[misc]
    """Fallback si LanceDB/embedding no están disponibles."""
    return None


# ✅ Fase 0 (2026-10-02, MEDIDO): `import lancedb` cuesta **~129 MB de Pss** y
# se hacía al arrancar en TODOS los procesos (consola, UI web, Rafael), aunque
# la memoria semántica no se abriera nunca. La sonda usa `find_spec` (no
# importa nada) y `lancedb` pasa a importarse donde se usa: al conectar la base
# y al construir el schema, ambos dentro de `Palace`. Mismo patrón que
# `src/utils/degradation.py::verificar_interprete`.
_PALACE_LANCE_AVAILABLE = importlib.util.find_spec("lancedb") is not None

if _PALACE_LANCE_AVAILABLE:
    from src.memory.embedding_model_singleton import get_embedding_model
else:
    get_embedding_model = _fallback_embedding_model  # type: ignore[assignment]

_PALACE_RECORD: Any = None


def _palace_record() -> Any:
    """Schema de LanceDB para la memoria semántica, construido a la primera.

    Es lo único que necesitaba `lancedb` en tiempo de import (definir el
    `LanceModel`); se arma cuando de verdad se crea la tabla.
    """
    global _PALACE_RECORD
    if _PALACE_RECORD is None:
        from lancedb.pydantic import LanceModel, Vector

        class _Record(LanceModel):
            """Schema LanceDB para memoria semántica de Palace."""
            vector: Vector(EMBED_DIMENSION)
            text: str
            metadata: str = "{}"
            # ✅ Capa 2 (2026-08-31): columnas top-level para filtrado eficiente
            # por sesión y por USUARIO (retrieval cross-sesión — mismo patrón que
            # MemoryRecord en semantic.py).
            session_id: str = ""
            user_id: str = "default"

        _PALACE_RECORD = _Record
    return _PALACE_RECORD


MEMPALACE_AVAILABLE = _PALACE_LANCE_AVAILABLE


class Palace:
    """
    Adaptador local para LanceDB que emula la API de MemPalace.
    v0.6.3: Migración desde ChromaDB a LanceDB. API store/recall/clear/close idéntica.
    Métrica cosine: similarity = 1.0 - _distance.
    Pre-computed embeddings inyectados desde EmbeddingOptimizer (evita doble cómputo).
    """

    def __init__(self, path: str, wing_name: str, room_name: str, hall_name: str) -> None:
        self.path = path
        self.wing_name = wing_name
        self.room_name = room_name
        self.hall_name = hall_name
        self._closed = False
        if not _PALACE_LANCE_AVAILABLE:
            raise ImportError("LanceDB no disponible. Ejecuta: pip install lancedb pyarrow")
        # ✅ Fase 0: import diferido — sólo se paga al abrir la memoria semántica.
        import lancedb as _lancedb
        self.db = _lancedb.connect(path)
        # ✅ 2026-08-25: race-safe (inits concurrentes → "Table already exists")
        try:
            self.collection = self.db.open_table("axioma_semantic")
            # ✅ Capa 2 (2026-08-31): migrar tablas creadas antes de las
            # columnas session_id/user_id (add_columns, lancedb ≥ 0.33).
            self._ensure_filter_columns()
        except Exception:
            try:
                self.collection = self.db.create_table("axioma_semantic", schema=_palace_record())
            except Exception as e:
                log_degraded(logger, e, "src/memory/palace_storage.py:__init__")
                self.collection = self.db.open_table("axioma_semantic")

    def _ensure_filter_columns(self) -> None:
        """Agrega session_id/user_id a tablas viejas (sin perder datos)."""
        try:
            import pyarrow as pa
            missing = [
                c for c in ("session_id", "user_id")
                if c not in self.collection.schema.names
            ]
            if missing:
                self.collection.add_columns([pa.field(c, pa.string()) for c in missing])
                logger.info(f"Palace: columnas de filtrado agregadas: {missing}")
        except Exception as e:
            logger.warning(f"Palace: no se pudieron agregar columnas de filtrado: {e}")

    def store(self, text: str, metadata: Dict[str, Any], embedding: Optional[Any] = None) -> None:
        # ✅ ISSUE-133: el guardado semántico va en SEGUNDO PLANO
        # (`MemoryGateway._submit`), así que un hilo tardío puede llegar acá
        # después de `close()`. Antes eso terminaba en
        # `AttributeError: 'NoneType' object has no attribute 'add'` y un ERROR
        # en los registros AL CERRAR (pasaba igual sin los imports diferidos).
        # Un portal cerrado no es un fallo: es trabajo que ya no corresponde.
        if self._closed or self.collection is None:
            logger.debug("Palace.store omitido: el portal ya está cerrado")
            return
        final_embedding = embedding
        if final_embedding is None:
            model = get_embedding_model()
            if model is None:
                logger.warning(
                    "Palace.store SKIPPED: embedding model unavailable "
                    "('sentence-transformers' no disponible o modelo no cargado)")
                return
            final_embedding = model.embed(text)
            if final_embedding is None:
                # ✅ v0.6.9v (ISSUE-003): traza del motivo — antes solo decía
                # "no embedding", sin distinguir error del modelo de ausencia.
                logger.warning(
                    f"Palace.store SKIPPED: model.embed devolvió None para "
                    f"'{text[:30]}' (modelo = {getattr(model, '_loaded', False) and 'cargado' or 'no cargado'})"
                )
                return
        vec: List[float] = (
            list(final_embedding)
            if not isinstance(final_embedding, list)
            else final_embedding
        )
        record = {
            "vector": vec,
            "text": text,
            "metadata": json.dumps(metadata) if isinstance(metadata, dict) else str(metadata),
            # ✅ Capa 2: columnas top-level para filtrado por sesión/usuario.
            "session_id": str((metadata or {}).get("session_id", "")) if isinstance(metadata, dict) else "",
            "user_id": str((metadata or {}).get("user_id", "default")) if isinstance(metadata, dict) else "default",
        }
        self.collection.add([record])

    def recall(
        self,
        query: str = None,
        query_embedding: Optional[Any] = None,
        limit: int = 5,
        min_score: float = 0.0,
        user_id: Optional[str] = None,  # ✅ Capa 2: filtra por usuario
    ) -> List[Dict[str, Any]]:
        # ✅ ISSUE-133: mismo motivo que en `store()` — un portal cerrado no
        # puede buscar; devolver vacío es el contrato, no un error.
        if self._closed or self.collection is None:
            logger.debug("Palace.recall omitido: el portal ya está cerrado")
            return []
        vec: Optional[List[float]] = None
        if query_embedding is not None:
            vec = (
                list(query_embedding)
                if not isinstance(query_embedding, list)
                else query_embedding
            )
        elif query:
            model = get_embedding_model()
            if model is None:
                return []
            emb = model.embed(query)
            if emb is None:
                # ✅ v0.6.9v (ISSUE-003): traza del motivo del recall fallido.
                logger.warning(
                    f"Palace.recall SKIPPED: model.embed devolvió None para "
                    f"'{query[:30]}' (modelo = {getattr(model, '_loaded', False) and 'cargado' or 'no cargado'})")
                return []
            vec = list(emb) if not isinstance(emb, list) else emb
        if vec is None:
            return []
        search = (
            self.collection.search(vec)
            .distance_type("cosine")
            .limit(limit)
        )
        if user_id:
            safe = str(user_id).replace("'", "''")
            search = search.where(f"user_id = '{safe}'")
        results = search.to_list()
        items: List[Dict[str, Any]] = []
        for r in results:
            distance = float(r.get("_distance", 1.0))
            similarity = max(0.0, min(1.0, 1.0 - distance))
            if similarity >= min_score:
                meta_raw = r.get("metadata", "{}")
                items.append({
                    "text": r.get("text", ""),
                    "similarity": similarity,
                    "metadata": json.loads(meta_raw) if meta_raw else {},
                })
        return items

    def clear(self) -> None:
        try:
            self.db.drop_table("axioma_semantic")
        except Exception as e:
            logger.warning(f"Palace.clear drop error: {e}")
        try:
            self.collection = self.db.create_table("axioma_semantic", schema=_palace_record())
        except Exception as e:
            log_degraded(logger, e, "src/memory/palace_storage.py:clear")
            # ✅ 2026-08-25: otro proceso pudo recrearla entre drop y create
            self.collection = self.db.open_table("axioma_semantic")

    def close(self) -> None:
        self.collection = None
        self.db = None
        self._closed = True
