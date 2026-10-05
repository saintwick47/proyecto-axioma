#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — UserProfile: Modelo persistente del usuario
# Ruta: src/memory/user_profile.py
# Autor: SaintWick
# Versión: 1.1.0 (Semana 2 — B) [B3]
# ✅ [B3]: _compute_weight() extraído como método privado (cosmético, 0 riesgo)
# Construido sobre KnowledgeGraph. Sin nueva DB.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .knowledge_graph import KnowledgeGraph, KGNode, KGEdge
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

CATEGORY_PERSON     = "person"
CATEGORY_PROJECT    = "project"
CATEGORY_TOPIC      = "topic"
CATEGORY_PREFERENCE = "preference"
CATEGORY_HABIT      = "habit"

_PROJECT_KEYWORDS = {"proyecto", "app", "sistema", "módulo", "api", "service"}


class UserProfile:
    """
    Modelo del mundo del usuario sobre KnowledgeGraph.
    Aprende de cada interacción. Persiste entre sesiones.
    Categorías: person, project, topic, preference, habit.
    No requiere nueva DB — usa KG existente.
    """

    def __init__(self, kg: KnowledgeGraph, user_id: str = "default"):
        self._kg = kg
        self._user_id = user_id
        self._user_node_id = f"user_{user_id}"
        self._ensure_user_node()
        logger.info(f"[UserProfile] Inicializado para user={user_id}")

    # ── Inicialización ────────────────────────────────────────────────────────

    def _ensure_user_node(self) -> None:
        """Crea nodo raíz del usuario si no existe."""
        if not self._kg.get_node(self._user_node_id):
            self._kg.add_node(KGNode(
                node_id=self._user_node_id,
                label=f"Usuario {self._user_id}",
                category="user",
                properties={"created_at": datetime.now().isoformat()},
            ))

    # ── Aprendizaje ───────────────────────────────────────────────────────────

    def learn_from_interaction(
        self, query: str, response: str, task_type: str,
    ) -> None:
        """
        Extrae entidades del query y persiste en KG.

        ✅ ISSUE-066: el peso se actualiza con una **delta rule** hacia la
        frecuencia relativa de la entidad (ver `_compute_weight`), y se guarda
        `hits` (conteo crudo) en la metadata para las reglas de frecuencia.
        """
        try:
            entities = self._extract_entities(query, task_type)
            for entity in entities:
                node_id = (
                    f"{entity['category']}_"
                    f"{entity['label'].lower().replace(' ', '_')}"
                )
                self._kg.add_node(KGNode(
                    node_id=node_id,
                    label=entity["label"],
                    category=entity["category"],
                    properties={
                        "last_seen": datetime.now().isoformat(),
                        "task_type": task_type,
                    },
                ))
                # ── Peso por frecuencia relativa (delta rule) ──────────────
                existing = self._kg.query_related(
                    self._user_node_id, relation="interacts_with", direction="out",
                )
                current_weight = 0.0
                current_hits = 0
                total_hits = 0
                for e in existing:
                    meta = e.get("metadata") or {}
                    h = meta.get("hits")
                    h = int(h) if isinstance(h, (int, float)) and h > 0 else 1
                    total_hits += h
                    if e["id"] == node_id:
                        current_weight = float(e["weight"] or 0.0)
                        current_hits = h
                # Las aristas viejas no traían `hits`: se estiman como 1 cada una
                # (así el sistema re-aprende la escala sin arrastrar la saturación
                # de la regla Hebbiana anterior).
                hits_nuevo = current_hits + 1
                total_nuevo = total_hits + 1
                nuevo_peso = self._compute_weight(
                    current_weight, hits=hits_nuevo,
                    total_hits=total_nuevo,
                )

                self._kg.add_edge(KGEdge(
                    source_id=self._user_node_id,
                    target_id=node_id,
                    relation="interacts_with",
                    weight=nuevo_peso,
                    metadata={
                        "last_task": task_type,
                        "timestamp": datetime.now().isoformat(),
                        "hits": hits_nuevo,
                    },
                ))
        except Exception as e:
            logger.warning(f"[UserProfile] learn error: {e}")

    # ── Consulta ──────────────────────────────────────────────────────────────

    def get_user_context(self, query: str) -> Dict[str, Any]:
        """
        Retorna contexto relevante del usuario para la query.
        Filtra por palabras de la query; si no hay match, devuelve top frecuentes.
        """
        try:
            related = self._kg.query_related(
                self._user_node_id, relation="interacts_with",
                direction="out", limit=20,
            )
            query_words = {w for w in query.lower().split() if len(w) > 3}
            relevant = [
                r for r in related
                if any(w in r["label"].lower() for w in query_words)
            ]
            if not relevant:
                relevant = sorted(related, key=lambda x: x["weight"], reverse=True)[:5]

            return {
                "known_entities": relevant[:5],
                "top_topics": self.get_frequent_patterns()[:3],
            }
        except Exception as e:
            logger.warning(f"[UserProfile] context error: {e}")
            return {}

    def get_frequent_patterns(self) -> List[Dict]:
        """Entidades más frecuentadas (por peso normalizado). Input para ProactiveEngine.

        ✅ ISSUE-066: el peso ahora es una **frecuencia relativa** (0-1), así que
        se expone también `hits` (conteo crudo) para las reglas que necesitan
        "cuántas veces" y no "qué proporción".
        """
        try:
            related = self._kg.query_related(
                self._user_node_id, relation="interacts_with",
                direction="out", limit=50,
            )
            for r in related:
                meta = r.get("metadata") or {}
                h = meta.get("hits")
                r["hits"] = int(h) if isinstance(h, (int, float)) and h > 0 else 1
            return sorted(related, key=lambda x: x["weight"], reverse=True)[:10]
        except Exception as e:
            log_degraded(logger, e, "src/memory/user_profile.py:get_frequent_patterns")
            return []

    def update_preference(self, key: str, value: Any) -> None:
        """Upsert preferencia explícita del usuario."""
        node_id = f"pref_{key}"
        self._kg.add_node(KGNode(
            node_id=node_id,
            label=key,
            category=CATEGORY_PREFERENCE,
            properties={"value": value, "updated_at": datetime.now().isoformat()},
        ))
        self._kg.add_edge(KGEdge(
            source_id=self._user_node_id,
            target_id=node_id,
            relation="prefers",
            weight=1.0,
        ))

    # ── Helpers internos ─────────────────────────────────────────────────────

    # ── Regla de asociación (ISSUE-066) ──────────────────────────────────────
    # ANTES: `w ← 0.95·w + 1.0` (Hebbiana) → punto fijo **w* = 20**: todas las
    # entidades convergían al mismo valor y no distinguía 30 interacciones de
    # 100. Sin señal correctiva: si el interés caía, el peso seguía subiendo.
    #
    # AHORA (delta rule, estilo "deep optimizer" del paper de Nested Learning):
    #     w ← w + η·(target − w)     con target = hits_i / Σ hits
    # es decir, el peso persigue la **frecuencia relativa actual** de la entidad
    # para ese usuario. Propiedades: acotado en [0,1], **competitivo** (si otra
    # entidad crece, ésta baja) y con error explícito (si el interés decae, el
    # peso decae). `hits` guarda el conteo crudo para reglas de frecuencia.
    _ETA = 0.3   # paso: converge en ~5-7 interacciones, sin oscilar

    def _compute_weight(self, current: float, hits: int = 1,
                        total_hits: int = 1, eta: float = None) -> float:
        """Delta rule: weight = weight + η·(frecuencia_relativa − weight)."""
        try:
            eta = self._ETA if eta is None else float(eta)
            target = float(hits) / float(max(1, total_hits))
            nuevo = float(current) + eta * (target - float(current))
            return round(max(0.0, min(1.0, nuevo)), 4)
        except Exception as e:
            log_degraded(logger, e, "src/memory/user_profile.py:_compute_weight")
            return round(max(0.0, min(1.0, float(current or 0.0))), 4)

    # ── Extracción de entidades ───────────────────────────────────────────────

    def _extract_entities(self, query: str, task_type: str) -> List[Dict[str, str]]:
        """
        Extracción heurística sin NLP externo.
        Palabras capitalizadas → person.
        Keywords de proyecto → project.
        task_type siempre → topic.
        Máx 5 entidades por interacción.
        """
        entities: List[Dict[str, str]] = []
        words = query.split()

        for word in words:
            clean = word.strip(".,?!:;\"'")
            if len(clean) > 3 and clean[0].isupper():
                entities.append({"label": clean, "category": CATEGORY_PERSON})

        q_lower = query.lower()
        for kw in _PROJECT_KEYWORDS:
            if kw in q_lower:
                idx = q_lower.find(kw)
                snippet = query[idx:idx + 30].split()[:2]
                label = " ".join(snippet)
                if label:
                    entities.append({"label": label, "category": CATEGORY_PROJECT})
                break  # un proyecto por interacción

        entities.append({"label": task_type, "category": CATEGORY_TOPIC})
        return entities[:5]
