#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# src/memory/corpus.py — Biblioteca documental persistente (v0.6.9s)
#
# MVP de CORPUS: indexá una carpeta → fragmentos (chunks) con metadata,
# deduplicación por hash y búsqueda híbrida (vectorial + BM25) que
# devuelve CITAS por fragmento.
#
# - Sin dependencias nuevas (sqlite + bge-m3 ya disponibles).
# - La ingesta de embeddings tarda en CPU (background); la CONSULTA es
#   rápida (~0.3-1 s: 1 embedding + cosine + BM25).
# - CLI:  python -m src.memory.corpus --index CARPETA | --query "..." | --list | --delete RUTA
#
# Integración futura: tool MCP `corpus_search` para el chat.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

_PDFDOCX = {".pdf", ".docx", ".doc"}
_TEXTS = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml",
          ".toml", ".html"}
_CHUNK_SIZE = 1200
_CHUNK_OVERLAP = 200
_DEFAULT_DB = Path("data/corpus.db")


def _simbolo_de_chunk(chunk: str) -> str:
    """Último `class`/`def` NOMBRADO dentro del chunk (heurística de texto).

    No se usa AST a propósito: `_chunk_text` corta por párrafos, no por nodos,
    así que un chunk puede empezar en medio de una función. El último nombre
    encontrado es el más cercano a lo que el chunk está definiendo.
    """
    nombres = re.findall(r"^\s*(?:class|def|async\s+def)\s+([A-Za-z_]\w*)",
                         chunk, re.M)
    return nombres[-1] if nombres else ""


def _prefijo_indexado(path: Path, chunk: str) -> str:
    """Cabecera `ruta :: símbolo` que se antepone al TEXTO INDEXADO (ISSUE-084).

    Antes el chunk se indexaba con **solo su contenido** y la ruta vivía
    únicamente en `metadata` (que ni BM25 ni ningún reranker léxico miran), así
    que una consulta por el nombre del archivo ("¿qué hace task_board?") o por un
    símbolo definido adentro no tenía **nada** que matchear: el token
    discriminante no existía en el texto puntuado.

    Se incluye la ruta completa, el nombre del archivo y el símbolo cuando se
    detecta. El prefijo entra en el TEXTO que se embebe y que puntúa BM25 (es el
    punto del arreglo): el score semántico también mejora porque el nombre del
    archivo queda asociado a su contenido.
    """
    cabecera = f"{path} :: {path.stem}"
    simbolo = _simbolo_de_chunk(chunk)
    if simbolo and simbolo != path.stem:
        cabecera += f" :: {simbolo}"
    return f"{cabecera}\n{chunk}"


def _chunk_text(text: str, size: int = _CHUNK_SIZE) -> List[str]:
    """Divide texto en fragmentos por párrafos, rellenando hasta `size`."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    cur = ""
    for para in paras:
        if len(para) > size:
            if cur:
                chunks.append(cur)
            cur = ""
            step = max(1, size - _CHUNK_OVERLAP)
            for i in range(0, len(para), step):
                piece = para[i:i + size].strip()
                if piece:
                    chunks.append(piece)
            continue
        if len(cur) + len(para) + 1 <= size:
            cur = (cur + "\n\n" + para).strip() if cur else para
        else:
            if cur:
                chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


def _cos(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class CorpusLibrary:
    """Biblioteca documental local (sqlite) con chunking, dedup y citas."""

    def __init__(self, db_path: Optional[str] = None,
                 embed_fn: Optional[Callable[[str], List[float]]] = None):
        self.db = Path(db_path or _DEFAULT_DB)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.embed_fn = embed_fn  # inyectable (tests) — default bge-m3
        self._conn = sqlite3.connect(str(self.db))
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE,
                file_hash TEXT, added_at REAL);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, doc_id INTEGER, idx INTEGER,
                text TEXT, metadata TEXT, embedding TEXT);
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
        """)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Embeddings ──────────────────────────────────────────────
    def _embed(self, text: str) -> Optional[List[float]]:
        if self.embed_fn is not None:
            try:
                return self.embed_fn(text)
            except Exception as exc:
                log_degraded(logger, exc, "src/memory/corpus.py:_embed")
                return None
        try:
            from src.memory.embedding_model_singleton import get_embedding_model
            model = get_embedding_model()
            if model is None:
                return None
            return list(model.embed(text))
        except Exception as exc:
            log_degraded(logger, exc, "src/memory/corpus.py:_embed")
            return None

    # ── Extracción / hash ───────────────────────────────────────
    @staticmethod
    def _file_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _extract(path: Path) -> str:
        if path.suffix.lower() in _PDFDOCX:
            from src.utils.file_text import extract_file_text
            try:
                return extract_file_text(path, max_chars=600000) or ""
            except Exception as exc:
                log_degraded(logger, exc, "src/memory/corpus.py:_extract")
                return ""
        if path.suffix.lower() in _TEXTS:
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                log_degraded(logger, exc, "src/memory/corpus.py:_extract")
                return ""
        return ""

    # ── Ingesta ─────────────────────────────────────────────────
    def _insert_doc(self, path: Path, data: bytes, replace: bool) -> Dict[str, Any]:
        file_hash = self._file_hash(data)
        existing = self._conn.execute(
            "SELECT id, file_hash FROM docs WHERE path=?", (str(path),)
        ).fetchone()
        if existing and existing[1] == file_hash and not replace:
            return {"skipped": str(path)}
        text = self._extract(path)
        if not text.strip():
            return {"empty": str(path)}
        chunks = _chunk_text(text)
        if existing:
            self._delete_doc(existing[0])
        doc_id = self._conn.execute(
            "INSERT INTO docs(path, file_hash, added_at) VALUES(?,?,?)",
            (str(path), file_hash, time.time())).lastrowid
        # ⚠️ ISSUE-083 (medido 2026-09-28): se probó batchear acá y resultó MÁS
        # LENTO en este hardware — 1×1: **492 ms/chunk** vs batch 60: **850**,
        # batch 8: 665, batch 8 ordenado por largo: 600. Con chunks de largo
        # heterogéneo (88-1200 chars, p50 969) el padding del batch cuesta más de
        # lo que ahorra, y en CPU el encode 1×1 ya usa todos los núcleos. Se deja
        # el camino de a uno (ver `ISSUE-083` en ISSUES_HISTORY.md).
        for i, chunk in enumerate(chunks):
            # ✅ v0.7.5 (ISSUE-084): se indexa el texto CON el prefijo
            # `ruta :: símbolo` (ver `_prefijo_indexado`): es lo que hace que las
            # consultas por nombre de archivo/símbolo tengan señal léxica.
            texto_indexado = _prefijo_indexado(path, chunk)
            emb = self._embed(texto_indexado)
            self._conn.execute(
                "INSERT INTO chunks(doc_id, idx, text, metadata, embedding) "
                "VALUES(?,?,?,?,?)",
                (doc_id, i, texto_indexado,
                 json.dumps({"path": str(path), "chunk": i,
                             "type": path.suffix.lower()}),
                 json.dumps(emb) if emb else None))
        self._conn.commit()
        return {"indexed": str(path), "chunks": len(chunks)}

    def index_path(self, path: str, replace: bool = False) -> Dict[str, Any]:
        p = Path(path)
        if not p.is_file():
            return {"error": f"no es archivo: {path}"}
        try:
            return self._insert_doc(p, p.read_bytes(), replace)
        except Exception as exc:  # noqa: BLE001
            log_degraded(logger, exc, "src/memory/corpus.py:index_path")
            return {"error": str(exc)}

    def index_folder(self, folder: str,
                     exclude: tuple = ("venv", ".git", "node_modules",
                                       "__pycache__", "data", ".pytest_cache",
                                       ".optimizer_backups", "deepseek-harness"),
                     ) -> Dict[str, Any]:
        root = Path(folder)
        if not root.is_dir():
            return {"error": f"no es carpeta: {folder}"}
        n = 0
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                if any(part in exclude for part in p.relative_to(root).parts):
                    continue
                if p.suffix.lower() not in (_TEXTS | _PDFDOCX):
                    continue
                result = self.index_path(str(p))
                if result.get("indexed"):
                    n += 1
            except Exception as exc:
                log_degraded(logger, exc, "src/memory/corpus.py:index_folder")
                continue
        return {"indexed_files": n}

    # ── Borrado ─────────────────────────────────────────────────
    def _delete_doc(self, doc_id: int) -> None:
        self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self._conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))

    def delete_path(self, path: str) -> bool:
        row = self._conn.execute(
            "SELECT id FROM docs WHERE path=?", (str(path),)).fetchone()
        if not row:
            return False
        self._delete_doc(row[0])
        self._conn.commit()
        return True

    # ── Búsqueda híbrida ────────────────────────────────────────
    def search(self, query: str, k: int = 5, hybrid: bool = True,
               rerank: Optional[bool] = None) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT c.id, c.doc_id, c.idx, c.text, c.embedding "
            "FROM chunks c").fetchall()
        if not rows:
            return []
        bm: List[float] = []
        if hybrid:
            from src.utils.bm25 import bm25_scores
            bm = bm25_scores(query, [r[3] for r in rows])
        else:
            bm = [0.0] * len(rows)
        qemb = self._embed(query)
        mx = max(bm) if bm else 0.0
        scored: List[tuple] = []
        for i, (_, doc_id, idx, text, emb_json) in enumerate(rows):
            sim = 0.0
            if qemb and emb_json:
                try:
                    sim = _cos(qemb, json.loads(emb_json))
                except Exception as exc:
                    log_degraded(logger, exc, "src/memory/corpus.py:search")
                    sim = 0.0
            lex = (bm[i] / mx) if mx > 0 else 0.0
            score = (0.5 * sim + 0.5 * lex) if hybrid else sim
            scored.append((doc_id, idx, text, score))
        scored.sort(key=lambda x: -x[3])
        cands = [{"doc_id": d, "idx": i, "text": t, "score": s}
                 for d, i, t, s in scored if s > 0]
        if rerank is None:
            try:
                from config.settings import settings as _st
                rerank = bool(getattr(_st, "reranker_enabled", False))
            except Exception as exc:
                log_degraded(logger, exc, "src/memory/corpus.py:search")
                rerank = False
        if rerank and cands:
            from src.utils.reranker import rerank as _rerank
            cands = _rerank(query, cands, model=None,
                            topk=len(cands))
            cands.sort(key=lambda c: -c.get("rerank_score", c.get("score", 0)))
        out: List[Dict[str, Any]] = []
        for c in cands[:k]:
            row = self._conn.execute(
                "SELECT path FROM docs WHERE id=?", (c["doc_id"],)).fetchone()
            path = row[0] if row else "?"
            score = c.get("rerank_score", c.get("score", 0.0))
            out.append({"path": path, "chunk": c["idx"],
                        "text": (c.get("text") or "")[:300],
                        "score": round(float(score), 4)})
        return out

    # ── Info ────────────────────────────────────────────────────
    def list_docs(self) -> List[Dict[str, Any]]:
        return [{"id": r[0], "path": r[1], "added_at": r[2]}
                for r in self._conn.execute(
                    "SELECT id, path, added_at FROM docs ORDER BY path")]

    def stats(self) -> Dict[str, Any]:
        docs = self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        ch = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"docs": docs, "chunks": ch, "db": str(self.db)}


def _main() -> int:
    parser = argparse.ArgumentParser(description="Biblioteca documental (corpus)")
    parser.add_argument("--index", help="Carpeta a indexar")
    parser.add_argument("--query", help="Consulta")
    parser.add_argument("--list", action="store_true", help="Listar docs")
    parser.add_argument("--delete", help="Borrar doc por ruta")
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="Ruta de la DB")
    args = parser.parse_args()

    lib = CorpusLibrary(db_path=args.db)
    try:
        if args.index:
            print(lib.index_folder(args.index))
        elif args.query:
            for r in lib.search(args.query):
                print(f"[{r['score']:.3f}] {r['path']} · chunk {r['chunk']}: "
                      f"{r['text'][:120]}")
        elif args.list:
            for d in lib.list_docs():
                print(f"{d['id']}  {d['path']}")
        elif args.delete:
            print("borrado:", lib.delete_path(args.delete))
        else:
            print(lib.stats())
    finally:
        lib.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
