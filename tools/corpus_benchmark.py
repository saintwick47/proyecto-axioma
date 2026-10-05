#!/usr/bin/env python3
"""
tools/corpus_benchmark.py — Benchmark de retrieval híbrido (v0.6.9t).

Compara SOLO VECTORIAL vs +BM25 vs +RERANKER en un corpus sintético
diseñado para mostrar el valor del reranker: cada consulta tiene un
doc BUENO (frase completa) y uno RUIDOSO (término repetido suelto).
BM25 tiende a subir el ruidoso (mayor TF); el reranker lo corrige por
frase/cobertura. Embeddings FAKE (presencia de términos) → corre en CI.

Uso:  python tools/corpus_benchmark.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TERMS = ["milanesa", "python", "rust", "dolar", "sqlite"]


def _fake_embed(text: str):
    words = set(text.lower().split())
    return [1.0 if t in words or t in text.lower() else 0.0 for t in _TERMS]


def _build(tmp: Path, real: bool = False):
    from src.memory.corpus import CorpusLibrary
    # real=False → embeddings FAKE (presencia) para CI sin bge-m3.
    # real=True  → embeddings reales (bge-m3, requiere el modelo cargado).
    embed_fn = None if real else _fake_embed
    lib = CorpusLibrary(db_path=str(tmp / "bench.db"), embed_fn=embed_fn)
    targets: Dict[str, str] = {}
    for t in _TERMS:
        # BUENO: contiene la frase completa
        good = tmp / f"good_{t}.txt"
        good.write_text(f"Receta de {t} al horno con pan rallado y huevo.",
                        encoding="utf-8")
        lib.index_path(str(good))
        targets[t] = str(good)
        # RUIDOSO: término repetido suelto (BM25 lo infla por TF)
        noise = tmp / f"noise_{t}.txt"
        noise.write_text(f"{t} {t} {t} aplicado a otros usos del término.",
                         encoding="utf-8")
        lib.index_path(str(noise))
        # Filler para no dejar el corpus mínimo
        filler = tmp / f"filler_{t}.txt"
        filler.write_text("contenido genérico sin relación con la consulta.",
                          encoding="utf-8")
        lib.index_path(str(filler))
    return lib, targets


def _metric(results: List[Dict], target_path: str) -> Tuple[float, float]:
    rank = next((i + 1 for i, r in enumerate(results)
                 if target_path in r["path"]), None)
    mrr = (1.0 / rank) if rank and rank <= 5 else 0.0
    p3 = 1.0 if rank and rank <= 3 else 0.0
    return mrr, p3


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Benchmark retrieval híbrido")
    parser.add_argument("--real", action="store_true",
                        help="Usar embeddings reales (bge-m3) en vez de fake")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        lib, targets = _build(td, real=args.real)
        try:
            configs = [
                ("solo vectorial", dict(hybrid=False, rerank=False)),
                ("+BM25", dict(hybrid=True, rerank=False)),
                ("+reranker", dict(hybrid=True, rerank=True)),
            ]
            print(f"{'config':20} {'MRR@5':>7} {'P@3':>7}")
            for name, kw in configs:
                mrr_s = p3_s = 0.0
                n = 0
                for t in _TERMS:
                    hits = lib.search(f"receta de {t}", k=5, **kw)
                    m, p = _metric(hits, targets[t])
                    mrr_s += m
                    p3_s += p
                    n += 1
                print(f"{name:22} {mrr_s / n:.3f}  {p3_s / n:.3f}")
        finally:
            lib.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
