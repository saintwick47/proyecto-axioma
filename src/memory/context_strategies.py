#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v2026.2 — Context Selection Strategies [C4]
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/context_strategies.py
# Autor: SaintWick
# Fecha: 2026-04-18
# ✅ [C4]: Strategy pattern para selección + compresión de contexto
# Extraído de: context_window.py (refactorización 50/50)
# ✅ v2026.1 (FASE 3):
#    - MessageDeduplicator: Reemplaza mensajes viejos con nuevos en
#      near-duplicates (evita fuga de estado reciente).
#    - ContextCompactor.micro_compact: No destruye System Prompts.
#      Se eliminó system_msgs = system_msgs[-1:].
# ✅ v2026.2 (2026-06-24): Eliminada referencia a modelo obsoleto
#    (aletheia-3b:latest → qwen3:8b) en comentario.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.memory.embedding_model_singleton import get_embedding_model
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURACIÓN COMPARTIDA POR SELECTORES
# ============================================================================

@dataclass
class SelectorConfig:
    """Valores de WindowConfig que los selectores necesitan sin depender del original."""
    dynamic_threshold_min: float = 0.85
    dynamic_threshold_max: float = 0.95
    max_tokens: int = 2048
    recency_anchor: int = 3
    # ✅ plan_memory (Fase 1, 2026-08-29): boost de score cuando el source_type
    # del mensaje matchea el esperado para el task_type actual. Se SUMA al
    # similarity antes del threshold adaptativo — sin filtro duro (la ventana
    # es chica y filtrar duro podría vaciarla).
    source_type_boost: float = 0.08


# ============================================================================
# TOKEN ESTIMATOR — Estimación sin dependencias externas
# ============================================================================

class TokenEstimator:
    """
    Estimador de tokens sin dependencias externas.

    Usa heurísticas basadas en caracteres y palabras. No es exacto
    (los tokenizadores reales son más complejos) pero es suficiente
    para tomar decisiones de truncado sin overhead de una llamada al modelo.

    Para qwen3:8b y qwen2.5-coder:7b, 4 chars/token
    es una buena aproximación para texto en español/inglés mezclado.
    """

    def __init__(self, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = chars_per_token

    def estimate(self, text: str) -> int:
        """
        Estima tokens de un texto.

        Heurística: chars / chars_per_token, con ajuste por palabras largas
        y puntuación (que generalmente son tokens separados).
        """
        if not text:
            return 0
        # Base: chars / chars_per_token
        base = len(text) / self.chars_per_token
        # Ajuste leve por palabras — cada palabra tiene overhead de separador
        word_count = len(text.split())
        adjusted = base * 0.85 + word_count * 0.15
        return max(1, int(adjusted))

    def estimate_message(self, message: Dict[str, Any]) -> int:
        """Estima tokens de un mensaje completo {role, content}."""
        role_tokens = self.estimate(message.get("role", ""))
        content_tokens = self.estimate(message.get("content", ""))
        return role_tokens + content_tokens + 4  # overhead de formato

    def estimate_messages(self, messages: List[Dict[str, Any]]) -> int:
        """Estima tokens totales de una lista de mensajes."""
        return sum(self.estimate_message(m) for m in messages)

    def fits_in_window(self, messages: List[Dict[str, Any]], max_tokens: int) -> bool:
        """True si los mensajes caben en la ventana de tokens."""
        return self.estimate_messages(messages) <= max_tokens


# ============================================================================
# MESSAGE COMPRESSOR — Compresión de mensajes individuales
# ============================================================================

class MessageCompressor:
    """
    Comprime mensajes individuales que exceden el threshold de tokens.

    Estrategias de compresión (aplicadas en orden):
    1. Eliminar líneas en blanco múltiples consecutivas
    2. Truncar al threshold manteniendo inicio y fin (más informativo que solo inicio)
    3. Marcar compresión con indicador visible
    """

    def __init__(self, estimator: TokenEstimator, threshold_tokens: int = 300) -> None:
        self.estimator = estimator
        self.threshold_tokens = threshold_tokens
        self._compressed_count = 0

    def compress(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Comprime un mensaje si excede el threshold.
        Retorna el mensaje original si no necesita compresión.
        """
        content = message.get("content", "")
        if not content:
            return message

        tokens = self.estimator.estimate(content)
        if tokens <= self.threshold_tokens:
            return message

        # Paso 1: Limpiar whitespace excesivo
        cleaned = re.sub(r'\n{3,}', '\n\n', content)
        cleaned = re.sub(r' {2,}', ' ', cleaned)

        tokens_cleaned = self.estimator.estimate(cleaned)
        if tokens_cleaned <= self.threshold_tokens:
            return {**message, "content": cleaned}

        # Paso 2: Truncar preservando inicio + fin
        max_chars = int(self.threshold_tokens * self.estimator.chars_per_token)
        head_chars = int(max_chars * 0.7)
        tail_chars = int(max_chars * 0.2)

        head = cleaned[:head_chars].rstrip()
        tail = cleaned[-tail_chars:].lstrip() if tail_chars > 0 else ""

        compressed = (
            f"{head}\n\n[...contenido omitido...]\n\n{tail}"
            if tail else f"{head}\n\n[...truncado]"
        )

        self._compressed_count += 1
        logger.debug(
            f"[ContextWindow] Mensaje comprimido: {tokens} → "
            f"{self.estimator.estimate(compressed)} tokens"
        )

        return {**message, "content": compressed, "_compressed": True}

    @property
    def compressed_count(self) -> int:
        return self._compressed_count


# ============================================================================
# DEDUPLICATOR — Eliminación de mensajes duplicados
# ============================================================================

class MessageDeduplicator:
    """
    Elimina mensajes duplicados o casi duplicados de la lista de contexto.

    Usa hash SHA256 del contenido normalizado para detectar duplicados exactos,
    y ratio de similitud de caracteres para duplicados aproximados.
    No usa embeddings — es una operación rápida y ligera.
    """

    def __init__(self, similarity_threshold: float = 0.95) -> None:
        self.similarity_threshold = similarity_threshold

    def _normalize(self, text: str) -> str:
        """Normaliza texto para comparación."""
        return re.sub(r'\s+', ' ', text.lower().strip())

    def _hash(self, text: str) -> str:
        return hashlib.sha256(self._normalize(text).encode()).hexdigest()[:16]

    def _similarity(self, a: str, b: str) -> float:
        """Ratio de similitud de caracteres entre dos textos (rápido, sin embeddings)."""
        if not a or not b:
            return 0.0
        na, nb = self._normalize(a), self._normalize(b)
        if na == nb:
            return 1.0
        # Jaccard sobre bigrams de palabras
        words_a = set(na.split())
        words_b = set(nb.split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def deduplicate(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Elimina duplicados manteniendo el más reciente de cada par.
        Preserva el orden original de los mensajes supervivientes.

        ✅ FIX FASE 3: Si un mensaje nuevo es casi idéntico a uno viejo,
        reemplaza el viejo con el nuevo para evitar fuga de estado reciente.
        """
        if len(messages) <= 1:
            return messages

        seen_hashes: set = set()
        result: List[Dict[str, Any]] = []

        for msg in messages:
            content = msg.get("content", "")
            h = self._hash(content)

            if h in seen_hashes:
                logger.debug("[ContextWindow] Mensaje duplicado eliminado (hash match)")
                continue

            # Verificar similitud aproximada con mensajes ya aceptados del mismo rol
            is_near_dup = False
            same_role = [m for m in result if m.get("role") == msg.get("role")]
            for existing in same_role[-3:]:  # solo comparar con los 3 más recientes
                sim = self._similarity(content, existing.get("content", ""))
                if sim >= self.similarity_threshold:
                    # ═══════════════════════════════════════════════════════════════
                    # ✅ FIX FASE 3: Reemplazar mensaje viejo con el nuevo.
                    # Antes se descartaba el nuevo, causando fuga de estado reciente.
                    # ═══════════════════════════════════════════════════════════════
                    idx = result.index(existing)
                    result[idx] = msg
                    is_near_dup = True
                    logger.debug(f"[ContextWindow] Near-duplicate replaced with newer (sim={sim:.2f})")
                    break

            if not is_near_dup:
                seen_hashes.add(h)
                result.append(msg)

        return result


# ============================================================================
# HELPERS COMPARTIDOS
# ============================================================================

def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Similitud coseno entre dos vectores."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ============================================================================
# BASE SELECTOR — Interfaz común para estrategias de selección
# ============================================================================

class BaseSelector(ABC):
    """
    Interfaz base para todas las estrategias de selección de contexto.
    Cada selector recibe mensajes y retorna un subconjunto que cabe en max_tokens.
    """

    @abstractmethod
    def select(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        expected_source_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Selecciona un subconjunto de mensajes.

        Args:
            messages  : Lista completa de mensajes candidatos.
            query     : Query actual del usuario (puede ser vacía).
            max_tokens: Presupuesto de tokens disponible.
            expected_source_type: (plan_memory Fase 1) source_type esperado
                para el task_type actual; los selectores que no usan
                relevancia lo ignoran.

        Returns:
            Subconjunto de mensajes seleccionados, en orden cronológico.
        """
        ...


# ============================================================================
# RECENCY SELECTOR — Últimos N mensajes que caben
# ============================================================================

class RecencySelector(BaseSelector):
    """
    Selecciona los mensajes más recientes que caben en max_tokens.
    Mantiene el orden cronológico original.
    Equivale al comportamiento actual de get_context().
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def select(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        expected_source_type: Optional[str] = None,  # plan_memory: ignorado
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        tokens_used = 0

        # Iterar desde el más reciente hacia atrás
        for msg in reversed(messages):
            msg_tokens = self._estimator.estimate_message(msg)
            if tokens_used + msg_tokens > max_tokens:
                break
            result.insert(0, msg)
            tokens_used += msg_tokens

        return result


# ============================================================================
# RELEVANCE SELECTOR — Mensajes más similares a la query
# ============================================================================

class RelevanceSelector(BaseSelector):
    """
    Selecciona mensajes por similitud semántica con la query.
    Requiere EmbeddingOptimizer. Fallback a RecencySelector si no disponible.

    Incluye:
      - _compute_adaptive_threshold: umbral dinámico según carga de contexto
      - _score_messages_by_relevance: scoring semántico vía embeddings
      - _get_model_fn: fallback a hash cuando no hay modelo de embeddings
    """

    def __init__(
        self,
        estimator: TokenEstimator,
        optimizer: Optional[Any],
        config: SelectorConfig,
        fallback: RecencySelector,
    ) -> None:
        self._estimator = estimator
        self._optimizer = optimizer
        self._config = config
        self._fallback = fallback

    def select(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        expected_source_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._optimizer or not query:
            logger.debug("[ContextWindow] RELEVANCE → fallback RECENCY (no optimizer/query)")
            return self._fallback.select(messages, query, max_tokens)

        try:
            scored = self._score_messages_by_relevance(
                messages, query, expected_source_type=expected_source_type
            )
            scored.sort(key=lambda x: x[1], reverse=True)

            threshold = self._compute_adaptive_threshold(messages)
            result: List[Dict[str, Any]] = []
            tokens_used = 0

            for msg, score in scored:
                if score < threshold:
                    continue
                msg_tokens = self._estimator.estimate_message(msg)
                if tokens_used + msg_tokens > max_tokens:
                    continue
                result.append(msg)
                tokens_used += msg_tokens

            # Si no hay mensajes relevantes, fallback a recency
            if not result:
                logger.debug("[ContextWindow] Sin mensajes relevantes → fallback RECENCY")
                return self._fallback.select(messages, query, max_tokens)

            # Restaurar orden cronológico
            original_order = {id(m): i for i, m in enumerate(messages)}
            result.sort(key=lambda m: original_order.get(id(m), 0))

            return result

        except Exception as e:
            logger.warning(f"[ContextWindow] RELEVANCE falló ({e}) → fallback RECENCY")
            return self._fallback.select(messages, query, max_tokens)

    # ── Umbral adaptativo ────────────────────────────────────────────────

    def _compute_adaptive_threshold(self, messages: List[Dict[str, Any]]) -> float:
        """
        [C4] Umbral adaptativo: baja a 0.85 en contexto pesado.
        Gradiente lineal entre dynamic_threshold_max (ligero)
        y dynamic_threshold_min (pesado).
        """
        try:
            mn = self._config.dynamic_threshold_min
            mx = self._config.dynamic_threshold_max
            count = len(messages)
            token_count = self._estimator.estimate_messages(messages)
            heavy_token = self._config.max_tokens * 0.8

            # Calcular factor de carga (0.0 = ligero, 1.0 = pesado)
            load_count = min(1.0, max(0.0, (count - 10) / 20))
            load_token = min(
                1.0,
                max(0.0, (token_count - heavy_token * 0.5) / (heavy_token * 0.5)),
            )
            load = max(load_count, load_token)

            return round(mx - load * (mx - mn), 4)
        except Exception as e:
            log_degraded(logger, e, "src/memory/context_strategies.py:_compute_adaptive_threshold")
            return 0.3  # min_similarity default de WindowConfig

    # ── Scoring semántico ────────────────────────────────────────────────

    def _score_messages_by_relevance(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        expected_source_type: Optional[str] = None,
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Asigna score de relevancia a cada mensaje usando EmbeddingOptimizer."""
        if not self._optimizer:
            return [(m, 0.5) for m in messages]

        try:
            model_fn = self._get_model_fn()

            query_embedding = self._optimizer.get_embedding(
                text=query,
                model_fn=model_fn,
                model_name="BAAI/bge-m3",   # ✅ plan_memory: mismo namespace que
                use_cache=True,             # Palace/add_message → cache compartido
            )

            scored: List[Tuple[Dict[str, Any], float]] = []
            for msg in messages:
                content = msg.get("content", "")
                if not content:
                    scored.append((msg, 0.0))
                    continue

                msg_embedding = self._optimizer.get_embedding(
                    text=content,
                    model_fn=model_fn,
                    model_name="BAAI/bge-m3",
                    use_cache=True,
                )

                score = cosine_similarity(query_embedding, msg_embedding)

                # ✅ plan_memory (Fase 1): boost si el source_type del mensaje
                # matchea el esperado para el task_type actual. Mensajes viejos
                # sin source_type → boost 0 (sin regresión).
                if expected_source_type:
                    msg_source = (msg.get("metadata") or {}).get("source_type")
                    if msg_source == expected_source_type:
                        score += self._config.source_type_boost

                scored.append((msg, score))

            return scored

        except Exception as e:
            log_degraded(logger, e, "src/memory/context_strategies.py:_score_messages_by_relevance")
            return [(m, 0.5) for m in messages]

    def _get_model_fn(self) -> Callable[[str], List[float]]:
        """
        Retorna función de embedding: usa el MODELO REAL (BGE-M3) cuando está
        disponible y cae a hash SOLO si falla o no hay modelo.

        ✅ plan_memory (Fase 0, 2026-08-29): antes
        `hasattr(self._optimizer, '_hash_embedding')` era SIEMPRE True (el
        optimizador define ese método como fallback), así que el scoring
        usaba MD5 en vez de embeddings reales — relevancia degradada a ruido/
        recency. Mismo patrón que gateway.py:59/74.
        """
        def _real_fn(text: str) -> List[float]:
            try:
                model = get_embedding_model()
                if model is not None:
                    emb = model.embed(text)
                    if emb is not None:
                        return emb
            except Exception as _d2e:
                log_degraded(logging.getLogger(__name__), _d2e, "src/memory/context_strategies.py:_real_fn")
            return self._optimizer._hash_embedding(text)

        return _real_fn


# ============================================================================
# HYBRID SELECTOR — Ancla temporal + relleno por relevancia
# ============================================================================

class HybridSelector(BaseSelector):
    """
    Combina recencia + relevancia.

    1. Reserva espacio para los últimos `recency_anchor` mensajes (siempre incluidos)
    2. Con el espacio restante, agrega los más relevantes para la query
    3. Elimina duplicados entre anchor y relevantes
    4. Restaura orden cronológico
    """

    def __init__(
        self,
        estimator: TokenEstimator,
        relevance_selector: RelevanceSelector,
        recency_selector: RecencySelector,
        config: SelectorConfig,
    ) -> None:
        self._estimator = estimator
        self._relevance_selector = relevance_selector
        self._recency_selector = recency_selector
        self._config = config

    def select(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        expected_source_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        anchor_count = min(self._config.recency_anchor, len(messages))
        anchor_msgs = messages[-anchor_count:] if anchor_count > 0 else []
        anchor_tokens = self._estimator.estimate_messages(anchor_msgs)

        remaining_tokens = max_tokens - anchor_tokens

        # Mensajes candidatos para relevancia (excluir los del anchor)
        anchor_ids = {id(m) for m in anchor_msgs}
        candidates = [m for m in messages if id(m) not in anchor_ids]

        # Seleccionar por relevancia con tokens restantes
        if candidates and query:
            relevant = self._relevance_selector.select(
                candidates, query, remaining_tokens,
                expected_source_type=expected_source_type,
            )
        elif candidates:
            relevant = self._recency_selector.select(
                candidates, query, remaining_tokens
            )
        else:
            relevant = []

        # Combinar sin duplicados
        combined_ids = {id(m) for m in relevant}
        final_anchor = [m for m in anchor_msgs if id(m) not in combined_ids]
        combined = relevant + final_anchor

        # Restaurar orden cronológico
        original_order = {id(m): i for i, m in enumerate(messages)}
        combined.sort(key=lambda m: original_order.get(id(m), 0))

        return combined


# ============================================================================
# MYTHOS INSPIRED SELECTOR — Pruning por densidad semántica
# ============================================================================

class MythosInspiredSelector(BaseSelector):
    """
    [C4] Selección por densidad semántica (MYTHOS_INSPIRED).
    Descarta mensajes con ratio unique_words/total_words < 0.2
    (coloquiales/repetitivos).
    Nunca descarta más del 50% del corpus.
    Fallback a HybridSelector si todos tienen densidad < floor.
    """

    DENSITY_FLOOR = 0.2
    MAX_DISCARD_RATIO = 0.5

    def __init__(self, hybrid_selector: HybridSelector) -> None:
        self._hybrid_selector = hybrid_selector

    def select(
        self,
        messages: List[Dict[str, Any]],
        query: str,
        max_tokens: int,
        expected_source_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        try:
            def _density(msg: Dict[str, Any]) -> float:
                words = msg.get("content", "").lower().split()
                if not words:
                    return 1.0
                return len(set(words)) / len(words)

            # Calcular densidades
            densities = [(msg, _density(msg)) for msg in messages]

            # Filtrar baja densidad — respetando floor y límite 50%
            survivors = [m for m, d in densities if d >= self.DENSITY_FLOOR]
            max_discard = int(len(messages) * self.MAX_DISCARD_RATIO)

            if len(messages) - len(survivors) > max_discard:
                # No descartar más del 50% — ordenar por densidad y conservar top
                densities_sorted = sorted(
                    densities, key=lambda x: x[1], reverse=True
                )
                keep_count = len(messages) - max_discard
                survivors = [m for m, _ in densities_sorted[:keep_count]]
                # Restaurar orden original
                original_order = {id(m): i for i, m in enumerate(messages)}
                survivors.sort(key=lambda m: original_order.get(id(m), 0))

            # Fallback si todos descartados
            if not survivors:
                logger.debug(
                    "[ContextWindow] MYTHOS_INSPIRED: todos baja densidad → HYBRID"
                )
                return self._hybrid_selector.select(
                    messages, query, max_tokens,
                    expected_source_type=expected_source_type,
                )

            return self._hybrid_selector.select(
                survivors, query, max_tokens,
                expected_source_type=expected_source_type,
            )

        except Exception as e:
            logger.warning(f"[ContextWindow] _select_mythos error ({e}) → HYBRID")
            return self._hybrid_selector.select(
                messages, query, max_tokens,
                expected_source_type=expected_source_type,
            )


# ============================================================================
# CONTEXT COMPACTOR — MicroCompact + JSONL transcripts (v0.6.0 Fase 3)
# ============================================================================

class ContextCompactor:
    """
    Compresión ligera sin LLM y guardado de transcripts JSONL.

    micro_compact():
      1. Deduplicación (near-duplicates por rol)
      2. Limpieza de whitespace excesivo en contenidos
      Reducción esperada: 5-20% en sesiones normales. O(n).

    save_transcript():
      Guarda mensajes como JSONL para replay/debug.
      Formato: data/transcripts/{session_id}_{timestamp}.jsonl
    """

    def __init__(self, deduplicator: MessageDeduplicator) -> None:
        self._deduplicator = deduplicator
        self._total_micro_compacted = 0

    def micro_compact(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Compresión ligera sin LLM.

        Aplica en orden:
          1. Deduplicación (near-duplicates por rol)
          2. Limpieza de whitespace excesivo en contenidos

        Reducción esperada: 5-20% en sesiones normales.
        No requiere LLM — es O(n) en tiempo.
        """
        if len(messages) <= 1:
            return messages

        before = len(messages)

        # 1. Deduplicar near-duplicates
        working = self._deduplicator.deduplicate(messages)

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 3: No destruir System Prompts.
        # Se eliminó la lógica `system_msgs = system_msgs[-1:]` que causaba
        # la pérdida del System Prompt principal si había múltiples mensajes
        # de sistema (ej. prompt principal + notas de compresión).
        # La deduplicación ya maneja mensajes repetidos de forma segura.
        # ═══════════════════════════════════════════════════════════════

        # 2. Limpiar whitespace excesivo en contenidos
        cleaned: List[Dict[str, Any]] = []
        for msg in working:
            content = msg.get("content", "")
            if content:
                content_clean = re.sub(r'\n{3,}', '\n\n', content)
                content_clean = re.sub(r' {2,}', ' ', content_clean).strip()
                if content_clean != content:
                    msg = {**msg, "content": content_clean}
            cleaned.append(msg)

        # Restaurar orden cronológico original
        cleaned.sort(
            key=lambda m: next(
                (i for i, orig in enumerate(messages)
                 if orig.get("content", "")[:50] == m.get("content", "")[:50]
                 and orig.get("role") == m.get("role")),
                9999
            )
        )

        after = len(cleaned)
        dropped = before - after
        self._total_micro_compacted += dropped

        if dropped > 0:
            logger.debug(
                f"[ContextWindow] micro_compact: {before}→{after} msgs "
                f"({dropped} eliminados)"
            )

        return cleaned

    def save_transcript(
        self,
        messages: List[Dict[str, Any]],
        session_id: str = "",
        transcript_dir: Optional[str] = None,
    ) -> Optional[str]:
        """
        Guarda mensajes como transcript JSONL para replay/debug.
        Formato: data/transcripts/{session_id}_{timestamp}.jsonl
        Retorna la ruta del archivo o None si falló.
        """
        sid = session_id or "unknown"
        ts = int(time.time())
        base_dir = transcript_dir or "data/transcripts"

        try:
            os.makedirs(base_dir, exist_ok=True)
            path = os.path.join(base_dir, f"{sid}_{ts}.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for msg in messages:
                    # Serializar solo campos básicos — evitar objetos no-JSON
                    record = {
                        "role": msg.get("role", ""),
                        "content": msg.get("content", ""),
                        "timestamp": msg.get("timestamp", ts),
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.debug(
                f"[ContextWindow] Transcript guardado: {path} ({len(messages)} msgs)"
            )
            return path
        except Exception as e:
            logger.warning(f"[ContextWindow] No se pudo guardar transcript: {e}")
            return None

    @property
    def total_micro_compacted(self) -> int:
        return self._total_micro_compacted

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN v2026.2 — FASE 3 + CORRECCIÓN
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Header               | ✅ v2026.2 changelog                      | Trazabilidad              |
# | TokenEstimator doc   | ✅ aletheia-3b:latest → qwen3:8b        | Eliminar modelo obsoleto  |
# | MessageDeduplicator  | ✅ Reemplazo viejo→nuevo en near-dup     | Evitar fuga de estado     |
# | ContextCompactor     | ✅ Eliminado system_msgs = system_msgs[-1:]| Evitar pérdida de System  |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
