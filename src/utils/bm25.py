#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/utils/bm25.py — BM25 (Okapi) para re-ranking híbrido (v0.6.9r).
#
# Búsqueda HÍBRIDA: combinar la puntuación semántica (embeddings) con la
# coincidencia léxica BM25. Sin modelos nuevos (sin RAM extra, ~ms en
# corpora chicos) — ideal para re-rankear el top-k de la búsqueda semántica.
#
# Uso:
#   from src.utils.bm25 import bm25_scores
#   scores = bm25_scores(query, [texto1, texto2, ...])   # mayor = mejor match
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import math
import re
from typing import List, Sequence

_TOKEN_RE = re.compile(r"[\wáéíóúüñ]+", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t]


def bm25_scores(query: str, corpus: Sequence[str],
                k1: float = 1.5, b: float = 0.75,
                eps: float = 0.25) -> List[float]:
    """Okapi BM25: relevancia léxica de `query` sobre cada doc de `corpus`.

    Idempotente y acotada (sin dependencias nuevas). Devuelve una lista
    con un score por documento (0 = sin coincidencia).
    """
    docs = [_tokens(d) for d in corpus]
    n = max(1, len(docs))
    doc_freq: dict[str, int] = {}
    for toks in docs:
        for word in set(toks):
            doc_freq[word] = doc_freq.get(word, 0) + 1

    avgdl = (sum(len(t) for t in docs) / n) if n else 1.0
    query_terms = set(_tokens(query))
    scores: List[float] = []
    for toks in docs:
        dl = len(toks)
        score = 0.0
        for word in query_terms:
            df = doc_freq.get(word, 0)
            if df == 0:
                continue
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            f = toks.count(word)
            denom = f + k1 * (1.0 - b + b * dl / max(avgdl, 1.0))
            score += idf * (f * (k1 + 1.0)) / (denom if denom else 1.0)
        scores.append(score)
    return scores


def bm25_rank(query: str, corpus: Sequence[str],
              k1: float = 1.5, b: float = 0.75,
              eps: float = 0.25) -> List[int]:
    """Índices de los documentos ordenados por BM25 (mayor primero)."""
    scores = bm25_scores(query, corpus, k1=k1, b=b, eps=eps)
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
