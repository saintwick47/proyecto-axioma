from __future__ import annotations


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════


class TestBm25:
    def test_documento_con_query_queda_arriba(self):
        from src.utils.bm25 import bm25_rank
        corpus = [
            "receta de milanesa al horno",
            "como compilar rust",
            "milanesas de pollo con papas",
        ]
        idx = bm25_rank("milanesa", corpus)
        assert corpus[idx[0]].find("milanesa") != -1

    def test_score_mayor_con_coincidencia(self):
        from src.utils.bm25 import bm25_scores
        scores = bm25_scores("gato", ["un gato negro", "un perro marrón"])
        assert scores[0] > scores[1]

    def test_sin_coincidencia_cero(self):
        from src.utils.bm25 import bm25_scores
        assert bm25_scores("zzz", ["apple", "banana"]) == [0.0, 0.0]

    def test_corpus_vacio(self):
        from src.utils.bm25 import bm25_scores, bm25_rank
        assert bm25_scores("hola", []) == []
        assert bm25_rank("hola", []) == []

    def test_tokens_con_acentos(self):
        from src.utils.bm25 import bm25_scores
        scores = bm25_scores("café", ["café en la plaza", "cafe oscuro"])
        assert scores[0] >= scores[1]


# ── Fusionado desde test_axioma_16_corpus.py ──
_TERMS = ("milanesa", "python", "receta", "rust")


def _fake_embed(text: str):
    low = text.lower()
    return [1.0 if t in low else 0.0 for t in _TERMS]


class TestChunkText:
    def test_separa_en_trozos(self):
        from src.memory.corpus import _chunk_text
        text = ("párrafo uno.\n" * 300) + "\n\n" + ("párrafo dos.\n" * 300)
        chunks = _chunk_text(text, size=200)
        assert len(chunks) > 2
        assert all(len(c) <= 200 for c in chunks)

    def test_parrafo_largo_se_corta(self):
        from src.memory.corpus import _chunk_text
        chunks = _chunk_text("x" * 1000, size=120)
        assert all(len(c) <= 120 for c in chunks)
        assert chunks


class TestCorpusLibrary:
    def _lib(self, tmp_path):
        from src.memory.corpus import CorpusLibrary
        return CorpusLibrary(db_path=str(tmp_path / "corpus.db"),
                             embed_fn=_fake_embed)

    def test_indexar_y_buscar_con_cita(self, tmp_path):
        lib = self._lib(tmp_path)
        try:
            carpeta = tmp_path / "docs"
            carpeta.mkdir()
            (carpeta / "cocina.txt").write_text(
                "Receta de milanesa al horno.", encoding="utf-8")
            (carpeta / "code.txt").write_text(
                "Python y Rust: ejemplos de código.", encoding="utf-8")
            res = lib.index_folder(str(carpeta))
            assert res["indexed_files"] == 2
            hits = lib.search("milanesa", k=2, hybrid=False)
            assert hits and "cocina.txt" in hits[0]["path"]
            assert "milanesa" in hits[0]["text"]
        finally:
            lib.close()

    def test_dedup_no_reenvuelve(self, tmp_path):
        lib = self._lib(tmp_path)
        try:
            f = tmp_path / "doc.txt"
            f.write_text("contenido único", encoding="utf-8")
            first = lib.index_path(str(f))
            second = lib.index_path(str(f))
            assert first.get("indexed")
            assert second.get("skipped") == str(f)
        finally:
            lib.close()

    def test_borrar(self, tmp_path):
        lib = self._lib(tmp_path)
        try:
            f = tmp_path / "x.txt"
            f.write_text("hola mundo", encoding="utf-8")
            lib.index_path(str(f))
            assert len(lib.list_docs()) == 1
            assert lib.delete_path(str(f)) is True
            assert len(lib.list_docs()) == 0
        finally:
            lib.close()

    def test_consulta_que_no_matchea(self, tmp_path):
        lib = self._lib(tmp_path)
        try:
            f = tmp_path / "a.txt"
            f.write_text("aprobación de acciones", encoding="utf-8")
            lib.index_path(str(f))
            assert lib.search("milanesa", k=2, hybrid=False) == []
        finally:
            lib.close()


class TestCorpusTools:
    """v0.6.9s — tools MCP del corpus (registradas y funcionales)."""

    def test_registradas(self):
        from src.tools_mcp.tool_registry import get_registry
        reg = get_registry()
        assert reg.has("corpus_search") and reg.has("corpus_index")

    def test_index_y_search(self, tmp_path):
        from src.tools_mcp.tool_registry import get_registry
        reg = get_registry()
        d = tmp_path / "docs"
        d.mkdir()
        (d / "cocina.md").write_text(
            "Receta de milanesa con huevo y pan rallado.", encoding="utf-8")
        db = str(tmp_path / "c.db")
        idx = reg.get("corpus_index").run(folder=str(d), db=db)
        assert idx.success and "1 archivos" in idx.output
        res = reg.get("corpus_search").run(query="milanesa", db=db)
        assert res.success
        assert "cocina.md" in res.output
        assert "milanesa" in res.output


class TestReranker:
    """v0.6.9t — re-ranking (proxy) para cerrar el retrieval híbrido."""

    def test_documento_con_frase_queda_arriba(self):
        from src.utils.reranker import rerank, score_proxy
        cands = [
            {"text": "receta de milanesa al horno", "score": 0.5},
            {"text": "milanesa milanesa milanesa suelto", "score": 0.6},
            {"text": "contenido no relacionado", "score": 0.3},
        ]
        out = rerank("receta de milanesa", cands, model=None)
        assert out[0]["text"].find("al horno") != -1
        assert out[0]["rerank_score"] >= out[1]["rerank_score"]

    def test_proxy_orden_estable(self):
        from src.utils.reranker import score_proxy
        a = score_proxy("python", {"text": "python es genial", "score": 0.5})
        b = score_proxy("python", {"text": "sin nada", "score": 0.5})
        assert a > b

    def test_search_con_rerank_activo(self, tmp_path):
        from src.memory.corpus import CorpusLibrary
        lib = CorpusLibrary(db_path=str(tmp_path / "c.db"),
                            embed_fn=_fake_embed)
        try:
            (tmp_path / "a.md").write_text(
                "receta de milanesa al horno", encoding="utf-8")
            lib.index_path(str(tmp_path / "a.md"))
            hits = lib.search("receta de milanesa", k=3, rerank=True)
            assert hits and "milanesa" in hits[0]["text"]
        finally:
            lib.close()


# ── Fusionado desde test_axioma_17_regression_eval.py ──


class TestDelta:
    def _delta(self, prev, cur):
        from tools.regression_eval import _delta
        return _delta(prev, cur)

    def test_mejora_marca_verde(self):
        out = self._delta({"pytest_passed": 100}, {"pytest_passed": 120})
        assert any("⬆" in line and "🟢" in line for line in out)

    def test_regresion_marca_rojo(self):
        out = self._delta({"pytest_passed": 120}, {"pytest_passed": 100})
        assert any("⬇" in line and "🔴" in line for line in out)

    def test_sin_cambio(self):
        out = self._delta({"pytest_passed": 100}, {"pytest_passed": 100})
        assert any("→" in line and "🟢" in line for line in out)

    def test_clave_ausente_ignorada(self):
        assert self._delta({"pytest_passed": 100}, {"auditor_score": 96}) == []


class TestReporteAcumulable:
    def _fresh_report(self, tmp_path, monkeypatch, run_id, label):
        from tests import axioma_report as rep
        monkeypatch.setattr(rep, "LOGS_DIR", tmp_path)
        monkeypatch.setattr(rep, "_report_path", None)
        monkeypatch.setattr(rep, "_section_open", None)
        monkeypatch.setattr(rep, "_totals", {"passed": 0, "failed": 0, "skipped": 0, "errors": 0})
        monkeypatch.setattr(rep, "_IS_SHARED_RUN", True)
        monkeypatch.setattr(rep, "RUN_ID", run_id)
        monkeypatch.setattr(rep, "RUN_LABEL", label)
        return rep

    def test_run_id_no_se_sobrescribe(self, tmp_path, monkeypatch):
        """Dos corridas con el mismo run-id comparten UN archivo (no se pisan)."""
        rep = self._fresh_report(tmp_path, monkeypatch, "BATCHX", "archivo_1")
        rep.check("check de la corrida 1", True, "")
        rep.finalize(extra="exitstatus=0", pytest_exitstatus=0)

        # Segunda corrida: mismo run-id, otra etiqueta
        rep._report_path = None
        rep._totals = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        rep.RUN_LABEL = "archivo_2"
        rep.check("check de la corrida 2", True, "")
        rep.finalize(extra="exitstatus=0", pytest_exitstatus=0)

        files = list(tmp_path.glob("axioma_test_report_*.md"))
        assert len(files) == 1, f"debería haber 1 solo reporte, hay {len(files)}"
        txt = files[0].read_text(encoding="utf-8")
        assert "check de la corrida 1" in txt and "check de la corrida 2" in txt
        assert "archivo_1" in txt and "archivo_2" in txt

    def test_veredicto_detecta_fallo_de_pytest(self, tmp_path, monkeypatch):
        """Un exitstatus != 0 debe marcar ❌ aunque no haya checks fallidos."""
        rep = self._fresh_report(tmp_path, monkeypatch, "BATCHFAIL", "x")
        rep.check("todo bien", True, "")
        rep.finalize(extra="exitstatus=1", pytest_exitstatus=1)
        txt = rep.report_path().read_text(encoding="utf-8")
        assert "HAY FALLOS" in txt
        assert "pytest exitstatus: **1**" in txt

    def test_veredicto_ok_con_exitstatus_cero(self, tmp_path, monkeypatch):
        rep = self._fresh_report(tmp_path, monkeypatch, "BATCHOK", "y")
        rep.check("todo bien", True, "")
        rep.finalize(extra="exitstatus=0", pytest_exitstatus=0)
        txt = rep.report_path().read_text(encoding="utf-8")
        assert "TODO CORRECTO" in txt

    def test_run_id_se_sanitiza(self):
        from tests.axioma_report import _safe_run_id
        assert _safe_run_id("a/b c:d") == "a_b_c_d"
        assert _safe_run_id("") == "run"
        assert len(_safe_run_id("x" * 200)) <= 64


class TestPrefijoIndexado:
    """ISSUE-084: el texto indexado lleva `ruta :: símbolo` al inicio.

    Antes se indexaba solo el contenido del chunk y la ruta vivía únicamente en
    `metadata`, que ni BM25 ni el reranker léxico miran ⇒ una consulta por el
    nombre del archivo no tenía nada que matchear (la evaluación que dejó BM25
    desactivado se hizo con un gold de identificadores sobre ese índice: medía
    una fusión léxica sin señal, no reranking).
    """

    def test_forma_del_prefijo(self):
        from pathlib import Path
        from src.memory.corpus import _prefijo_indexado
        con_clase = _prefijo_indexado(Path("src/core/memory_guard.py"),
                                      "class MemoryGuard:\n    pass")
        sin_simbolo = _prefijo_indexado(Path("src/core/__init__.py"), "solo texto")
        assert con_clase.startswith(
            "src/core/memory_guard.py :: memory_guard :: MemoryGuard"), con_clase
        assert sin_simbolo.startswith("src/core/__init__.py :: __init__"), sin_simbolo

    def test_lo_indexado_guarda_el_prefijo(self, tmp_path):
        """Se indexa de verdad (embedder falso) y se lee el texto guardado."""
        from src.memory.corpus import CorpusLibrary
        archivo = tmp_path / "mystery_module.py"
        archivo.write_text("def unrelated_helper(value):\n    return value\n",
                           encoding="utf-8")
        lib = CorpusLibrary(db_path=str(tmp_path / "c.db"), embed_fn=lambda t: [1.0, 0.0])
        try:
            res = lib.index_path(str(archivo))
            filas = lib._conn.execute("SELECT text FROM chunks").fetchall()
        finally:
            lib.close()
        guardado = filas[0][0] if filas else ""
        assert "chunks" in res, res
        assert guardado.startswith(f"{archivo} :: mystery_module"), guardado[:120]

    def test_la_busqueda_encuentra_por_nombre_de_archivo(self, tmp_path):
        """Sin el prefijo esto daba 0 señal léxica: el nombre no estaba en el texto.

        Embedder falso CONSTANTE ⇒ todos los chunks empatan en similitud y el
        ranking lo decide BM25, que es justo lo que se quiere aislar.
        """
        from src.memory.corpus import CorpusLibrary
        (tmp_path / "gateway_fantasma.py").write_text(
            "def nada_que_ver(a):\n    return a * 2\n", encoding="utf-8")
        (tmp_path / "otro.py").write_text(
            "def nada_que_ver(b):\n    return b * 3\n", encoding="utf-8")
        lib = CorpusLibrary(db_path=str(tmp_path / "c.db"), embed_fn=lambda t: [1.0, 0.0])
        try:
            # ⚠️ El señuelo va PRIMERO a propósito: con embeddings constantes todos
            # los chunks empatan en similitud, así que sin señal léxica el orden
            # queda por inserción y el señuelo ganaría. Solo el prefijo con el
            # nombre del archivo le da a BM25 algo que matchear.
            lib.index_path(str(tmp_path / "otro.py"))
            lib.index_path(str(tmp_path / "gateway_fantasma.py"))
            hits = lib.search("gateway_fantasma", k=2, hybrid=True, rerank=False)
        finally:
            lib.close()
        primero = hits[0]["path"] if hits else ""
        # Mensaje explícito: sin el prefijo el nombre del archivo no está en el
        # texto puntuado y este assert es el que lo demuestra.
        assert primero.endswith("gateway_fantasma.py"), f"primer hit: {primero}"
