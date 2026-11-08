"""Migración de la tabla LanceDB cuando cambia la dimensión del embedding (2026-11-08).

El equipo tenía `axioma_semantic` creada con **1024** dims (modelo viejo, bge-m3). Al pasar a
F2LLM-v2-330M (**896**) el schema quedó en 1024 y **cada alta fallaba**:

    Background Semantic add failed: lance error: Cast error: Cannot cast to
    FixedSizeList(1024): value at index 0 has length 896

Esta migración **re-embebe** las filas existentes (la tabla guarda `text`, la fuente de verdad)
y recrea la tabla con la dimensión correcta. Si una fila no se puede re-embeder, se omite y se
avisa; nunca se pierde el texto silenciosamente sin log.

Uso:
    tabla = asegurar_dimension(db, "axioma_semantic", EMBED_DIMENSION, get_embedding_fn())
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


def dimension_de_la_tabla(tabla: Any) -> int:
    """Dimensión del campo `vector` (FixedSizeList) de una tabla LanceDB."""
    return int(tabla.schema.field("vector").type.list_size)


def _recrear(db: Any, nombre: str, dim: int, filas: list, vectores: list) -> Any:
    """Crea la tabla con la dimensión nueva y reemplaza la vieja.

    Devuelve la tabla resultante con el nombre final (LanceDB OSS no tiene `rename_table`).
    """
    import pyarrow as pa

    vieja = db.open_table(nombre)
    campos = [pa.field(c.name, pa.list_(pa.float32(), dim)) if c.name == "vector" else c
              for c in vieja.schema]
    esquema = pa.schema(campos)
    datos = {c.name: [f.get(c.name) for f in filas] for c in esquema}
    datos["vector"] = vectores
    nueva_nombre = f"{nombre}__migrando"
    try:
        db.drop_table(nueva_nombre)
    except Exception as e:
        log_degraded(logger, e, "migracion_dims._recrear (la temporal no existía)")
    arrow_nueva = pa.Table.from_pydict(datos, schema=esquema)
    # LanceDB OSS NO soporta `rename_table` (probado 2026-11-08): se crea una tabla temporal
    # para validar los datos, se borra la vieja y se vuelve a crear con el nombre final.
    db.create_table(nueva_nombre, data=arrow_nueva)
    db.drop_table(nombre)
    db.create_table(nombre, data=arrow_nueva)
    try:
        db.drop_table(nueva_nombre)
    except Exception as e:
        log_degraded(logger, e, "migracion_dims._recrear (no se borró la temporal)")
    return db.open_table(nombre)


def asegurar_dimension(db: Any, nombre: str, dim: int,
                       embed_fn: Callable[[str], Any] | None = None) -> Any:
    """Abre `nombre`; si su `vector` no mide `dim`, la migra y devuelve la tabla resultante.

    Returns:
        La tabla (migrada o no), o `None` si no existe.
    """
    try:
        tabla = db.open_table(nombre)
    except Exception:
        return None
    try:
        actual = dimension_de_la_tabla(tabla)
    except Exception as e:
        logger.warning(f"[memoria] no pude leer la dimensión de {nombre}: {e}")
        return tabla
    if actual == dim:
        return tabla
    logger.warning(f"[memoria] {nombre} está en {actual} dims y el modelo da {dim}: migrando…")
    if embed_fn is None:
        logger.error(f"[memoria] sin embed_fn no puedo migrar {nombre}; quedará rota")
        return tabla

    filas, vectores, omitidas = [], [], 0
    for fila in tabla.to_arrow().to_pylist():
        texto = (fila.get("text") or "").strip()
        if not texto:
            omitidas += 1
            continue
        try:
            vector = embed_fn(texto)
            if not vector or len(vector) != dim:
                raise ValueError(f"dimensión {len(vector) if vector else 0} ≠ {dim}")
            filas.append(fila)
            vectores.append(list(vector))
        except Exception as e:
            omitidas += 1
            logger.warning(f"[memoria] fila sin re-embeder ({e}); se omite")
    logger.info(f"[memoria] migradas {len(filas)} filas de {nombre} ({omitidas} omitidas)")
    return _recrear(db, nombre, dim, filas, vectores)
