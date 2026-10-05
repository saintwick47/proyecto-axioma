#!/usr/bin/env python3
"""
tools/retrieval_eval.py — Evaluación del retrieval (v0.6.9t).

Mide sobre un GOLD SET real qué configuración conviene:
  - solo vectorial            (hybrid=False, rerank=False)
  - +BM25                     (hybrid=True)
  - +reranker                 (hybrid=True, rerank=True)

Formato del gold (JSON):
  { "queries": [ {"query": "...", "relevant": ["path1", "path2"]} ] }

Métricas por config: MRR@5, precision@3, hit@3 y LATENCIA (promedio/p95).
Embeddings reales (bge-m3) — correr en tu máquina.

Uso:
  # 1) Borrador de gold desde una carpeta REAL (lo escribís en gold.json):
  python tools/retrieval_eval.py --corpus CARPETA --draft --gold gold.json
  # 2) Editar gold.json (consulta + "relevant") y evaluar:
  python tools/retrieval_eval.py --corpus CARPETA --gold gold.json
  # Plantilla vacía:
  python tools/retrieval_eval.py --template-out gold.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEMPLATE = {"queries": [
    {"query": "¿qué dice sobre <tema>?", "relevant": ["ruta/al/doc.ext"]},
]}
_CONFIGS = [
    ("vectorial", dict(hybrid=False, rerank=False)),
    ("+BM25", dict(hybrid=True, rerank=False)),
    ("+reranker", dict(hybrid=True, rerank=True)),
]


def _mrr(results: List[Dict[str, Any]], relevant: List[str], k: int) -> float:
    for i, r in enumerate(results[:k]):
        if r["path"] in relevant:
            return 1.0 / (i + 1)
    return 0.0


def _prec(results: List[Dict[str, Any]], relevant: List[str], k: int) -> float:
    top = results[:k]
    return max(0.0, sum(1 for r in top if r["path"] in relevant) / len(top)) if top else 0.0


def _hit(results: List[Dict[str, Any]], relevant: List[str], k: int) -> float:
    return 1.0 if any(r["path"] in relevant for r in results[:k]) else 0.0


def _lat_txt(lats: List[float]) -> str:
    if not lats:
        return "-"
    s = sorted(lats)
    p95 = s[int(len(s) * 0.95) - 1]
    return f"avg {sum(lats)/len(lats)*1000:.0f}ms · p95 {p95*1000:.0f}ms"


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluación de retrieval")
    ap.add_argument("--corpus", help="Carpeta REAL a indexar")
    ap.add_argument("--gold", help="Gold set JSON (se escribe con --draft; requerido para evaluar)")
    ap.add_argument("--db", default="data/corpus_eval.db", help="DB")
    ap.add_argument("--template-out", help="Solo escribe la plantilla del gold")
    ap.add_argument("--draft", action="store_true",
                    help="Generar borrador de gold desde --corpus (en --gold)")
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args()

    if args.template_out:
        Path(args.template_out).write_text(
            json.dumps(_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"plantilla → {args.template_out}")
        return 0

    # Borrador: requiere --corpus; escribe en --gold (o gold.json por defecto)
    if args.draft:
        if not args.corpus:
            print("❌ --draft requiere --corpus (carpeta real)")
            return 1
        out = args.gold or "gold.json"
        from src.memory.corpus import CorpusLibrary
        lib = CorpusLibrary(db_path=args.db)
        try:
            queries = []
            for p in sorted(Path(args.corpus).rglob("*")):
                if p.is_file() and p.suffix.lower() in {
                        ".txt", ".md", ".py", ".json", ".csv"}:
                    queries.append({"query": f"¿qué dice {p.stem}?",
                                    "relevant": [str(p)]})
            Path(out).write_text(
                json.dumps({"queries": queries}, ensure_ascii=False,
                           indent=2), encoding="utf-8")
            print(f"borrador → {out} ({len(queries)} consultas). "
                  f"Editá cada 'query'/'relevant' y corré con --gold.")
        finally:
            lib.close()
        return 0

    if not args.gold:
        print("❌ Falta --gold (o usá --draft/--template-out). "
              "Ej: --corpus CARPETA --gold gold.json")
        return 1

    from src.memory.corpus import CorpusLibrary
    lib = CorpusLibrary(db_path=args.db)
    try:
        if args.corpus:
            print(f"⏳ Indexando {args.corpus} en CPU (solo la 1ª vez; "
                  f"puede tardar unos minutos)...", flush=True)
            res = lib.index_folder(args.corpus)
            print(f"   ✓ indexado: {res.get('indexed_files', 0)} archivos "
                  f"(las siguientes corridas con el mismo --db reusan y "
                  f"son rápidas).", flush=True)
        gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
        queries = gold.get("queries", [])
        if not queries:
            print("gold vacío — usá --template-out o --draft")
            return 1
        print(f"{'config':14} {'MRR@5':>7} {'P@3':>7} {'hit@3':>7} {'latencia':>22}")
        for name, cfg in _CONFIGS:
            print(f"→ {name} ...", end="", flush=True)
            mrr_s = prec_s = hit_s = 0.0
            lats: List[float] = []
            for q in queries:
                t0 = time.perf_counter()
                res = lib.search(q["query"], k=args.topk, **cfg)
                lats.append(time.perf_counter() - t0)
                rel = q.get("relevant", [])
                mrr_s += _mrr(res, rel, 5)
                prec_s += _prec(res, rel, 3)
                hit_s += _hit(res, rel, 3)
                print(".", end="", flush=True)
            print()
            n = len(queries)
            print(f"{name:16} {mrr_s/n:.3f}  {prec_s/n:.3f}  "
                  f"{hit_s/n:.3f}  {_lat_txt(lats):>22}")
    finally:
        lib.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
