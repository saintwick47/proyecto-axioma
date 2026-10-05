#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# knowledge_graph.py - Memoria Relacional Persistente (KnowledgeGraph)
# Ruta: /ruta/a/axioma/src/memory/knowledge_graph.py
# Autor: SaintWick
# Versión: 0.2.0
#
# Extensión de memoria a largo plazo para almacenar entidades, conceptos
# y relaciones en un grafo de conocimiento persistente sobre SQLite.
# ═══════════════════════════════════════════════════════════════
"""
Grafo de conocimiento persistente sobre SQLite.
Compatible con `LongTermMemory` y `MemoryGateway`.
Implementa gestión propia de conexiones para independencia del Pool global.
"""
from __future__ import annotations
import logging
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from config.paths import Paths

logger = logging.getLogger(__name__)

# ✅ FIX AUDITORÍA: pool de conexiones — antes cada método creaba una
# conexión SQLite nueva (con sus 5 PRAGMA) y la dejaba para el garbage
# collector, sin pool ni cierre explícito. Import seguro: si no está
# disponible, cae al comportamiento anterior (conexión directa por llamada).
try:
    from src.utils.db_pool import SQLiteConnectionPool
    _POOL_AVAILABLE = True
except ImportError:
    _POOL_AVAILABLE = False


@dataclass
class KGNode:
    """Nodo del grafo de conocimiento (entidad/concepto)."""
    node_id: str
    label: str
    properties: Dict[str, Any] = field(default_factory=dict)
    category: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa nodo para transporte/API."""
        return {
            "node_id": self.node_id,
            "label": self.label,
            "category": self.category,
            "properties": self.properties
        }


@dataclass
class KGEdge:
    """Relación dirigida entre dos nodos del grafo."""
    source_id: str
    target_id: str
    relation: str
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)  # ✅ CORREGIDO

    def to_dict(self) -> Dict[str, Any]:
        """Serializa arista para transporte/API."""
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "weight": self.weight,
            "metadata": self.metadata
        }


class KnowledgeGraph:
    """
    Gestor de grafo de conocimiento persistente (SQLite).
    Permite almacenar entidades (nodos) y relaciones (aristas) con soporte de consultas estructuradas.
    Gestiona sus propias conexiones con configuración optimizada (WAL, etc.).
    """

    _SCHEMA_INITIALIZED = False

    def __init__(self, db_path: Optional[Path | str] = None, pool_size: int = 3):
        # ✅ FIX T17: Normaliza str/None → Path antes de llamar a .parent
        # ✅ FIX: "data/knowledge_graph.db" era relativo al CWD del proceso —
        # mismo bug ya encontrado y corregido esta sesión en feedback_loop.py
        # y feedback_loop_components.py. Paths.DATA está resuelto de forma
        # absoluta contra Paths.ROOT (config/paths.py). Mismo nombre de
        # archivo que antes, solo se ancla de forma absoluta.
        self.db_path = Path(db_path) if db_path else (Paths.DATA / "knowledge_graph.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # ✅ FIX AUDITORÍA: pool en vez de conexión nueva por método
        self._pool: Optional[SQLiteConnectionPool] = None
        if _POOL_AVAILABLE:
            try:
                self._pool = SQLiteConnectionPool(self.db_path, pool_size=pool_size)
                self._pool.initialize()
            except Exception as e:
                logger.warning(f"KnowledgeGraph: pool no disponible, fallback directo: {e}")
                self._pool = None

        self._ensure_schema()
        logger.debug(f"KnowledgeGraph inicializado en {self.db_path}")

    def _create_direct_connection(self) -> sqlite3.Connection:
        """Conexión directa (fallback si el pool no está disponible o timeoutea)."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self):
        """
        ✅ FIX AUDITORÍA: entrega una conexión del pool (o directa si el
        pool no está disponible/timeoutea) y la libera/cierra correctamente
        al salir. `with conn:` sigue dando el commit/rollback automático
        de siempre (comportamiento nativo de sqlite3.Connection, no cierra
        la conexión) — lo nuevo es qué pasa DESPUÉS de ese `with`.
        """
        conn = None
        from_pool = False
        if self._pool is not None:
            conn = self._pool.acquire(timeout=5.0)
            from_pool = conn is not None
        if conn is None:
            conn = self._create_direct_connection()
        try:
            with conn:
                yield conn
        finally:
            if from_pool:
                self._pool.release(conn)
            else:
                conn.close()

    def _ensure_schema(self) -> None:
        """Crea las tablas e índices necesarios si no existen."""
        schema = """
        CREATE TABLE IF NOT EXISTS kg_nodes (
            node_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            properties TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS kg_edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source_id, target_id, relation),
            FOREIGN KEY (source_id) REFERENCES kg_nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES kg_nodes(node_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_category ON kg_nodes(category);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON kg_edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON kg_edges(target_id);
        CREATE INDEX IF NOT EXISTS idx_edges_relation ON kg_edges(relation);
        -- ✅ FIX AUDITORÍA (H9): query_related() filtra por source_id+relation
        -- o target_id+relation A LA VEZ, pero solo había índices simples por
        -- columna separada — SQLite escaneaba por source_id/target_id y
        -- filtraba relation fila por fila. Compuestos = seek directo.
        CREATE INDEX IF NOT EXISTS idx_edges_source_relation ON kg_edges(source_id, relation);
        CREATE INDEX IF NOT EXISTS idx_edges_target_relation ON kg_edges(target_id, relation);
        """
        try:
            with self._connection() as conn:
                conn.executescript(schema)
            if not KnowledgeGraph._SCHEMA_INITIALIZED:
                logger.info("Esquema de KnowledgeGraph validado/creado exitosamente")
                KnowledgeGraph._SCHEMA_INITIALIZED = True
        except sqlite3.Error as e:
            logger.error(f"Error crítico al inicializar esquema de KG: {e}")
            raise RuntimeError(f"KG Schema initialization failed: {e}")

    def add_node(self, node: KGNode) -> bool:
        """Inserta o actualiza (UPSERT) un nodo en el grafo."""
        try:
            props_json = json.dumps(node.properties, ensure_ascii=False)
            with self._connection() as conn:
                conn.execute(
                    """INSERT INTO kg_nodes (node_id, label, category, properties)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(node_id) DO UPDATE SET
                       label=excluded.label,
                       category=excluded.category,
                       properties=excluded.properties""",
                    (node.node_id, node.label, node.category, props_json)
                )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error añadiendo nodo {node.node_id}: {e}")
            return False

    def add_edge(self, edge: KGEdge) -> bool:
        """Inserta o actualiza (UPSERT) una relación entre nodos."""
        try:
            meta_json = json.dumps(edge.metadata, ensure_ascii=False)
            with self._connection() as conn:
                conn.execute(
                    """INSERT INTO kg_edges (source_id, target_id, relation, weight, metadata)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                       weight=excluded.weight,
                       metadata=excluded.metadata""",
                    (edge.source_id, edge.target_id, edge.relation, edge.weight, meta_json)
                )
            return True
        except sqlite3.Error as e:
            logger.error(f"Error añadiendo arista {edge.source_id}->{edge.target_id}: {e}")
            return False

    def get_node(self, node_id: str) -> Optional[KGNode]:
        """Recupera un nodo completo por su ID."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT node_id, label, category, properties FROM kg_nodes WHERE node_id = ?",
                    (node_id,)
                ).fetchone()
            if row:
                return KGNode(
                    node_id=row[0],
                    label=row[1],
                    category=row[2],
                    properties=json.loads(row[3])
                )
            return None
        except sqlite3.Error as e:
            logger.error(f"Error consultando nodo {node_id}: {e}")
            return None

    def query_related(
        self,
        node_id: str,
        relation: Optional[str] = None,
        direction: str = "out",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Consulta nodos conectados a un nodo dado.
        Args:
            node_id: ID del nodo origen/destino
            relation: Filtrar por tipo de relación (opcional)
            direction: "out" (source→target), "in" (target←source), "both"
            limit: Máximo de resultados
        Returns:
            Lista de diccionarios con datos del nodo relacionado y metadatos de la arista
        """
        rel_filter = "AND ke.relation = ?" if relation else ""
        base_params: List[Any] = [node_id]
        if relation:
            base_params.append(relation)

        if direction == "out":
            query = f"""
                SELECT kn.node_id, kn.label, kn.category, kn.properties, ke.relation, ke.weight,
                       ke.metadata
                FROM kg_edges ke
                JOIN kg_nodes kn ON ke.target_id = kn.node_id
                WHERE ke.source_id = ? {rel_filter}
                LIMIT ?
            """
            params = base_params + [limit]
        elif direction == "in":
            query = f"""
                SELECT kn.node_id, kn.label, kn.category, kn.properties, ke.relation, ke.weight,
                       ke.metadata
                FROM kg_edges ke
                JOIN kg_nodes kn ON ke.source_id = kn.node_id
                WHERE ke.target_id = ? {rel_filter}
                LIMIT ?
            """
            params = base_params + [limit]
        else:  # both
            query = f"""
                SELECT kn.node_id, kn.label, kn.category, kn.properties, ke.relation, ke.weight,
                       ke.metadata,
                       CASE WHEN ke.source_id = ? THEN 'out' ELSE 'in' END as direction_flag
                FROM kg_edges ke
                JOIN kg_nodes kn ON (ke.target_id = kn.node_id OR ke.source_id = kn.node_id)
                WHERE (ke.source_id = ? OR ke.target_id = ?) {rel_filter}
                  AND kn.node_id != ?
                LIMIT ?
            """
            params = base_params + [node_id, node_id, node_id, limit]

        try:
            with self._connection() as conn:
                cursor = conn.execute(query, params)
                results = []
                for row in cursor.fetchall():
                    props = json.loads(row[3])
                    # ✅ ISSUE-066: se devuelve también la metadata de la arista
                    # (`hits`, `last_task`, `timestamp`). Sin esto, el conteo de
                    # hits del perfil de usuario se perdía en cada actualización
                    # (todas las aristas viejas se leían como hits=1).
                    try:
                        arista_meta = json.loads(row[6]) if row[6] else {}
                    except (TypeError, ValueError):
                        arista_meta = {}
                    result = {
                        "id": row[0], "label": row[1], "category": row[2],
                        "properties": props, "relation": row[4], "weight": row[5],
                        "metadata": arista_meta if isinstance(arista_meta, dict) else {},
                    }
                    if direction == "both":
                        result["direction"] = row[7]
                    else:
                        result["direction"] = direction
                    results.append(result)
                return results
        except sqlite3.Error as e:
            logger.error(f"Error en consulta KG ({direction}): {e}")
            return []

    def search_nodes(
        self,
        label_pattern: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20
    ) -> List[KGNode]:
        """Búsqueda flexible de nodos por patrón de etiqueta o categoría."""
        conditions, params = [], []
        if label_pattern:
            conditions.append("label LIKE ?")
            params.append(f"%{label_pattern}%")
        if category:
            conditions.append("category = ?")
            params.append(category)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        query = f"SELECT node_id, label, category, properties FROM kg_nodes WHERE {where_clause} LIMIT ?"
        try:
            with self._connection() as conn:
                cursor = conn.execute(query, params)
                return [
                    KGNode(node_id=row[0], label=row[1], category=row[2], properties=json.loads(row[3]))
                    for row in cursor.fetchall()
                ]
        except sqlite3.Error as e:
            logger.error(f"Error en búsqueda de nodos KG: {e}")
            return []

    def delete_node(self, node_id: str) -> bool:
        """Elimina un nodo y todas sus aristas asociadas (cascada)."""
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM kg_edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
                conn.execute("DELETE FROM kg_nodes WHERE node_id = ?", (node_id,))
            return True
        except sqlite3.Error as e:
            logger.error(f"Error eliminando nodo {node_id}: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas operativas del grafo."""
        try:
            with self._connection() as conn:
                nodes = conn.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
                edges = conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
                categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM kg_nodes").fetchall()]
                relations = [r[0] for r in conn.execute("SELECT DISTINCT relation FROM kg_edges").fetchall()]
            return {
                "total_nodes": nodes,
                "total_edges": edges,
                "categories": categories,
                "relation_types": relations,
                "db_path": str(self.db_path)
            }
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo stats KG: {e}")
            return {"error": str(e)}

    def reset(self) -> bool:
        """Limpia todas las tablas del grafo (útil para testing o reinicio de sesión)."""
        try:
            with self._connection() as conn:
                conn.execute("DELETE FROM kg_edges")
                conn.execute("DELETE FROM kg_nodes")
            logger.info("KnowledgeGraph reiniciado (tablas limpiadas)")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error reiniciando KG: {e}")
            return False
