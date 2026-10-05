#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.8 — Multi-Query Ligero (sin LLM) + Fusión RRF
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/multi_query.py
# Autor: SaintWick
# Propósito: Mejora de recall para búsqueda semántica adaptada a
#            hardware CPU-only (6C/12T, 14.7GB RAM, sin GPU).
#
# ⚠️ NO usa LLM para generar variantes (inviable en CPU: +15-60s).
#    Expansión 100% determinística + UN batch de embeddings +
#    fusión Reciprocal Rank Fusion (RRF). Costo real: +0-200ms.
#
# Punto de integración (único hook activo):
#   MemoryGateway.search_semantic() → src/memory/gateway.py
#   SemanticCacheL3.set() (variantes en escritura) → src/router/cache_l3.py
#
# Qué hace:
#   1. expand_query()        — genera 1-4 variantes determinísticas
#   2. batch_embed_variants()— UN solo batch BGE-M3 (embed_batch)
#   3. search_variants()     — 1 búsqueda LanceDB por variante
#   4. rrf_fuse()            — fusión Reciprocal Rank Fusion + dedupe
#   5. stats globales        — get_multi_query_stats()
# ═══════════════════════════════════════════════════════════════
import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================================
# STOPWORDS (ES + EN) — para variante "keywords-only"
# Lista conservadora: solo conectores, sin tocar términos técnicos.
# ============================================================================
_STOPWORDS = frozenset(
    "a al ante bajo cabe con contra de del desde durante e el ella ellas ellos "
    "en entre hacia hasta la las le les lo los me mi mis mucho muchos muy nada "
    "ni nos nosotras nosotros o os para pero poca pocas poco pocos por porque "
    "que quien quienes se sea sean ser si sin sois somos son soy su sus sus "
    "también te ti tiene tienen tus tu un una unas uno unos vosotras vosotros "
    "vuestra vuestras vuestro vuestros y ya yo él ésta éstas éste éstos ése "
    "ésa ésos esa esas ese eso esto estos esta estas este estos"
    " a an and are as at be but by for from had has have he her hers him his "
    "i if in into is it its me my nor of on or our ours she should so some "
    "such than that the their theirs them then there these they this those "
    "to too was we were what when where which while who whom why will with "
    "would you your yours"
    # Interrogativos acentuados — sin peso semántico en retrieval.
    # NOTA: "no"/"si" NO son stopwords (invierten significado en queries).
    " qué cómo dónde cuándo cuál cuáles quién quiénes cuánto cuánta cuántos cuántas "
    .split()
)


class MultiQueryError(Exception):
    """Error específico del módulo multi-query."""


# ============================================================================
# 1) EXPANSIÓN DE QUERY — 100% determinística, sin LLM
# ============================================================================
def expand_query(query: str, max_variants: int = 4, min_keyword_len: int = 3) -> List[str]:
    """
    Genera variantes determinísticas de una query para multi-query retrieval.

    Variantes (siempre incluye la original como primera):
    1. Original (normalizada: whitespace colapsado, strip)
    2. keywords-only — sin stopwords (mantiene términos técnicos)
    3. reordenada — keywords con el término más "significativo" al frente
       (heurística: longitud > 4 chars → mayor peso semántico)
    4. sin puntuación / lowercase — variante robusta a formatos

    Args:
        query: Query original del usuario
        max_variants: Máximo de variantes (incluye la original), 1-5
        min_keyword_len: Longitud mínima para considerar keyword significativa

    Returns:
        Lista de variantes únicas (≥1). Vacía si query es vacía/None.
    """
    if not query or not isinstance(query, str):
        return []
    if max_variants < 1:
        return []

    original = re.sub(r"\s+", " ", query.strip())
    if not original:
        return []

    variants: List[str] = [original]
    lower = original.lower()

    def _add(variant: str) -> None:
        """Agrega variante si no es duplicado (dedupe por clave normalizada:
        lowercase + colapsado + sin puntuación). Dos variantes que difieren
        solo en formato ('¿cómo...?' vs 'cómo...') cuentan como UNA."""
        v = variant.strip()
        if not v or len(v) < len(original) * 0.3:
            return  # variante demasiado degradada vs la original
        key = normalize_key(v)
        if key and all(normalize_key(existing) != key for existing in variants):
            variants.append(v)

    # --- Variante 2: keywords-only -------------------------------------
    words = re.findall(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ0-9]+", lower)
    keywords = [w for w in words if w not in _STOPWORDS and len(w) >= min_keyword_len]
    if len(keywords) >= 2:
        _add(" ".join(keywords))

    # --- Variante 3: reordenada por peso heurístico ---------------------
    # Palabras largas (>4) suelen ser el núcleo semántico; moverlas al
    # frente aumenta el recall para docs que usan ese término exacto.
    if len(keywords) >= 2:
        def _weight(w: str) -> int:
            # 3: largo, 2: medio, 1: corto no-stopword
            if len(w) >= 8:
                return 3
            if len(w) >= 5:
                return 2
            return 1
        ranked = sorted(keywords, key=_weight, reverse=True)
        if len(ranked) >= 2 and ranked[0] != keywords[0]:
            _add(" ".join(ranked))

    # --- Variante 4: lowercase + sin puntuación -------------------------
    cleaned = re.sub(r"[^\w\sáéíóúüñ]", " ", lower)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    _add(cleaned)

    # --- Variante 5 (opcional): primera oración truncada ---------------
    # Para queries largas: solo la cláusula principal.
    if len(variants) < max_variants and len(original) > 60:
        first_clause = re.split(r"[,.;:¿?¡!]", original)[0].strip()
        if len(first_clause) >= 15:
            _add(first_clause)

    return variants[:max_variants]


def normalize_key(text: str) -> str:
    """Clave de dedupe: lowercase, colapsado, sin puntuación."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sáéíóúüñ]", " ", text.lower())).strip()


# ============================================================================
# 2) BATCH DE EMBEDDINGS — UNA sola llamada para todas las variantes
# ============================================================================
def batch_embed_variants(
    variants: List[str],
    embed_batch_fn: Optional[Callable[[List[str]], Optional[List[List[float]]]]] = None,
) -> Dict[str, Optional[List[float]]]:
    """
    Genera embeddings para todas las variantes en UN solo batch.

    Args:
        variants: Lista de variantes
        embed_batch_fn: Función batch (recibe List[str], devuelve List[vec]).
            Si es None, usa el singleton BGE-M3 centralizado.

    Returns:
        Dict {variante: embedding o None si falló}
    """
    if not variants:
        return {}
    if embed_batch_fn is None:
        try:
            from src.memory.embedding_model_singleton import get_embedding_model
            model = get_embedding_model()
            if model is None:
                return {v: None for v in variants}
            embed_batch_fn = model.embed_batch
        except Exception as e:
            logger.warning(f"[MultiQuery] embed_batch_fn unavailable: {e}")
            return {v: None for v in variants}

    try:
        vectors = embed_batch_fn(variants)
    except Exception as e:
        logger.warning(f"[MultiQuery] batch embed failed ({len(variants)} variants): {e}")
        return {v: None for v in variants}

    if vectors is None:
        return {v: None for v in variants}

    result: Dict[str, Optional[List[float]]] = {}
    for i, v in enumerate(variants):
        vec = vectors[i] if i < len(vectors) else None
        if vec is not None:
            result[v] = list(vec) if not isinstance(vec, list) else vec
        else:
            result[v] = None
    return result


# ============================================================================
# 3) FUSIÓN RECIPROCAL RANK FUSION (RRF) + dedupe
# ============================================================================
def rrf_fuse(
    per_variant_results: List[List[Dict]],
    k: int = 60,
    min_similarity: float = 0.0,
) -> List[Dict]:
    """
    Fusiona rankings de varias búsquedas con Reciprocal Rank Fusion.

    score(doc) = Σ_variante 1 / (k + rank)

    El ranking RRF premia docs que aparecen en VARIAS variantes (robustez),
    no solo los que tienen la similitud más alta en UNA.

    Args:
        per_variant_results: Lista de listas de {text, similarity, metadata...}
        k: Constante RRF (típico 60)
        min_similarity: Umbral mínimo para incluir el doc (usa la similitud
            máxima observada entre variantes para ese doc)

    Returns:
        Lista fusionada, ordenada por score RRF desc. Cada item conserva
        su estructura original + "rrf_score" + "similarity" (máx entre
        variantes).
    """
    if not per_variant_results:
        return []

    scores: Dict[str, Dict] = {}
    for rank_list in per_variant_results:
        for rank, item in enumerate(rank_list):
            text = item.get("text", "")
            if not text:
                continue
            key = normalize_key(text)
            if key not in scores:
                entry = dict(item)
                entry["rrf_score"] = 0.0
                entry["similarity"] = float(item.get("similarity", 0.0) or 0.0)
                scores[key] = entry
            else:
                entry = scores[key]
                # Conservar la similitud MÁXIMA observada entre variantes
                sim = float(item.get("similarity", 0.0) or 0.0)
                if sim > entry["similarity"]:
                    entry["similarity"] = sim
            scores[key]["rrf_score"] += 1.0 / (k + rank + 1)

    fused = list(scores.values())
    fused.sort(key=lambda e: e["rrf_score"], reverse=True)

    if min_similarity > 0.0:
        fused = [e for e in fused if e["similarity"] >= min_similarity]

    for e in fused:
        e["rrf_score"] = round(e["rrf_score"], 6)
        e["similarity"] = round(e["similarity"], 4)
    return fused


# ============================================================================
# 4) ORQUESTACIÓN — multi-query search completo
# ============================================================================
def multi_query_search(
    query: str,
    search_fn: Callable[[str, Optional[List[float]]], List[Dict]],
    embed_batch_fn: Optional[Callable[[List[str]], Optional[List[List[float]]]]] = None,
    max_variants: int = 4,
    limit: int = 5,
    min_similarity: float = 0.5,
    rrf_k: int = 60,
) -> List[Dict]:
    """
    Búsqueda multi-query ligera: expande → batch embed → busca → RRF.

    Args:
        query: Query original
        search_fn: Función que ejecuta UNA búsqueda vectorial por variante.
            Firma: (variant_text, variant_embedding_or_None) -> List[Dict].
            Cada dict debe tener al menos "text" y "similarity".
        embed_batch_fn: Función batch (o None → singleton BGE-M3)
        max_variants: Máximo de variantes a generar (incluye original)
        limit: Límite de resultados finales
        min_similarity: Umbral de similitud para filtrar (post-fusión)
        rrf_k: Constante RRF

    Returns:
        Resultados fusionados, limitados a `limit`.
    """
    start = time.perf_counter()
    variants = expand_query(query, max_variants=max_variants)
    if not variants:
        return []

    # UN solo batch para TODAS las variantes
    embeddings = batch_embed_variants(variants, embed_batch_fn)

    per_variant: List[List[Dict]] = []
    for v in variants:
        vec = embeddings.get(v)
        try:
            results = search_fn(v, vec)
        except Exception as e:
            logger.warning(f"[MultiQuery] search failed for variant '{v[:40]}': {e}")
            results = []
        if results:
            per_variant.append(results)

    fused = rrf_fuse(per_variant, k=rrf_k, min_similarity=min_similarity)

    elapsed_ms = (time.perf_counter() - start) * 1000
    _stats.record(
        queries=1,
        variants=len(variants),
        searches=len(per_variant),
        results=len(fused),
        elapsed_ms=elapsed_ms,
    )
    logger.debug(
        f"[MultiQuery] query='{query[:40]}' | variants={len(variants)} | "
        f"searches={len(per_variant)} | fused={len(fused)} | {elapsed_ms:.0f}ms"
    )
    return fused[:limit]


# ============================================================================
# 5) ESTADÍSTICAS GLOBALES (thread-safe)
# ============================================================================
class _MultiQueryStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.queries = 0
            self.variants_total = 0
            self.searches_total = 0
            self.results_total = 0
            self.elapsed_ms_total = 0.0
            self.errors = 0

    def record(self, queries: int = 0, variants: int = 0, searches: int = 0,
               results: int = 0, elapsed_ms: float = 0.0, errors: int = 0) -> None:
        with self._lock:
            self.queries += queries
            self.variants_total += variants
            self.searches_total += searches
            self.results_total += results
            self.elapsed_ms_total += elapsed_ms
            self.errors += errors

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "queries": self.queries,
                "avg_variants_per_query": round(
                    self.variants_total / max(self.queries, 1), 2
                ),
                "searches_total": self.searches_total,
                "results_total": self.results_total,
                "avg_results_per_query": round(
                    self.results_total / max(self.queries, 1), 2
                ),
                "avg_latency_ms": round(
                    self.elapsed_ms_total / max(self.queries, 1), 2
                ),
                "errors": self.errors,
            }


_stats = _MultiQueryStats()


def get_multi_query_stats() -> Dict:
    """Estadísticas acumuladas del módulo multi-query (para health checks)."""
    return _stats.get_stats()


def reset_multi_query_stats() -> None:
    """Reinicia estadísticas (útil para benchmarks)."""
    _stats.reset()


# ============================================================================
# TESTS RÁPIDOS (python src/memory/multi_query.py)
# ============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("TESTS MULTI-QUERY LIGERO - AXIOMA v0.6.8")
    print("=" * 60)

    # --- Test 1: expansión --------------------------------------------
    queries = [
        "cómo puedo optimizar el rendimiento de mi base de datos",
        "función para leer CSV en Python",
        "hola",  # corta → solo original
        "qué es mejor: pandas o polars para análisis de datos grandes",
    ]
    for q in queries:
        vs = expand_query(q)
        print(f"\nQuery: '{q}'")
        print(f"  Variantes ({len(vs)}):")
        for i, v in enumerate(vs):
            print(f"    [{i}] '{v}'")

    # --- Test 2: RRF --------------------------------------------------
    print("\n" + "=" * 60)
    print("TEST RRF FUSION")
    variant_a = [
        {"text": "doc pandas optimización", "similarity": 0.85},
        {"text": "doc sqlite index", "similarity": 0.78},
    ]
    variant_b = [
        {"text": "doc sqlite index", "similarity": 0.82},
        {"text": "doc pandas optimización", "similarity": 0.60},
        {"text": "doc bases de datos", "similarity": 0.71},
    ]
    fused = rrf_fuse([variant_a, variant_b], min_similarity=0.50)
    print(f"Fusionados: {len(fused)}")
    for f in fused:
        print(f"  rrf={f['rrf_score']:.4f} sim={f['similarity']:.2f} :: {f['text']}")

    # --- Test 3: orquestación con mock (sin cargar BGE-M3) ------------
    print("\n" + "=" * 60)
    print("TEST ORQUESTACIÓN CON MOCK EMBEDDER")

    _fake_docs = [
        "pandas es ideal para análisis de datos tabulares en Python",
        "polars es una alternativa más rápida a pandas en Rust",
        "sqlite permite consultas SQL embebidas sin servidor",
        "el índice en bases de datos mejora la velocidad de consulta",
    ]

    def _mock_search(variant: str, vec: Optional[List[float]]) -> List[Dict]:
        """Mock: similitud = overlap de palabras (sin embeddings reales)."""
        v_words = set(re.findall(r"[a-z]+", variant.lower()))
        out = []
        for doc in _fake_docs:
            d_words = set(re.findall(r"[a-z]+", doc.lower()))
            sim = len(v_words & d_words) / max(len(v_words | d_words), 1)
            if sim > 0.15:
                out.append({"text": doc, "similarity": round(sim, 4)})
        out.sort(key=lambda r: r["similarity"], reverse=True)
        return out[:5]

    def _mock_embed_batch(texts: List[str]) -> List[List[float]]:
        # Firma idéntica a BGE-M3.embed_batch pero sin modelo real.
        return [[1.0] * 4 for _ in texts]

    results = multi_query_search(
        query="optimizar consultas de bases de datos",
        search_fn=_mock_search,
        embed_batch_fn=_mock_embed_batch,
        max_variants=4,
        limit=3,
        min_similarity=0.15,
    )
    print(f"Resultados finales: {len(results)}")
    for r in results:
        print(f"  rrf={r['rrf_score']:.4f} sim={r['similarity']:.2f} :: {r['text'][:60]}")

    print("\nStats:", get_multi_query_stats())
    print("=" * 60)
    print("FIN TESTS")
