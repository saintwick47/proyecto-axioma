#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/utils/reranker.py — Re-ranking (v0.6.9t)
#
# Cierra el retrieval híbrido. Ofrece:
#  - Proxy determinístico: re-rankea la lista corta combinando el score
#    base + BM25 + cobertura de términos + bonus de frase (sin modelos,
#    sin descargas, ~ms) — usable ya.
#  - Soporte opcional a un modelo local (bge-reranker) vía `model`
#    (callable score(query, doc)), para cuando el usuario lo instale
#    y lo mida en hardware con RAM de sobra.
#
# Uso:  top = rerank(query, candidates, model=None, topk=5)
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\wáéíóúüñ]+", re.UNICODE)


def score_proxy(query: str, candidate: Dict[str, Any]) -> float:
    """Score de re-ranking sin modelo: base + BM25 + cobertura + frase."""
    from src.utils.bm25 import bm25_scores
    text = candidate.get("text", "") or ""
    base = float(candidate.get("score", 0.0) or 0.0)
    bm = bm25_scores(query, [text])
    lex = bm[0] if bm else 0.0
    q_terms = set(_TOKEN_RE.findall(query.lower()))
    t_low = text.lower()
    coverage = (sum(1 for w in q_terms if w in t_low)
                / max(1, len(q_terms)))
    phrase = 1.0 if query.lower().strip() in t_low else 0.0
    return round(0.2 * base + 0.5 * lex + 0.3 * coverage + 0.2 * phrase, 4)


def rerank(
    query: str,
    candidates: List[Dict[str, Any]],
    model: Optional[Callable[[str, str], float]] = None,
    topk: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Re-ordena candidatos por relevancia; devuelve el top-k con `score` nuevo.

    - `model` = callable(doc_text, query) -> float (bge-reranker u otro).
      Si es None se usa `score_proxy` (determinístico, sin modelo).
    - Mutación leve: agrega/actualiza `candidate['rerank_score']`.
    """
    indexed = list(range(len(candidates)))

    def score_of(i: int) -> float:
        c = candidates[i]
        if model is not None:
            try:
                return float(model(c.get("text", ""), query))
            except Exception as exc:
                logger.debug(f"[reranker] model score falló: {exc}")
                return float(c.get("score", 0.0))
        return score_proxy(query, c)

    ranked = sorted(indexed, key=lambda i: (-score_of(i), i))
    topk = topk if topk is not None else len(candidates)
    out: List[Dict[str, Any]] = []
    for i in ranked[:topk]:
        item = dict(candidates[i])
        item["rerank_score"] = score_of(i)
        out.append(item)
    return out
