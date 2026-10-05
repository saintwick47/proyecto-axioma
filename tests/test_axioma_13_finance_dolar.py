from __future__ import annotations


# ════════════════════════════════════════════════════════════════# CONTENIDO FUSIONADO (ver ISSUE-057): este archivo absorbió otros por tema# ════════════════════════════════════════════════════════════════


class TestDolarDolarapi:
    def test_deteccion_query_dolar(self):
        from src.integrations.external_apis import _query_es_dolar
        assert _query_es_dolar("¿a cuánto está el dólar hoy?") is True
        assert _query_es_dolar("cotización del blue") is True
        assert _query_es_dolar("clima en Buenos Aires") is False
        assert _query_es_dolar("") is False

    def test_formato_dolarapi(self):
        from src.integrations.external_apis import _format_dolarapi
        data = [
            {"casa": "oficial", "compra": 900.5, "venta": 940.25,
             "fechaActualizacion": "2026-09-09T12:00:00.000Z"},
            {"casa": "blue", "compra": 1520.0, "venta": 1540.0,
             "fechaActualizacion": "2026-09-09T13:00:00.000Z"},
        ]
        out = _format_dolarapi(data)
        assert "oficial" in out and "blue" in out
        assert "compra" in out and "venta" in out
        assert "2026-09-09" in out

    def test_formato_datos_invalidos(self):
        from src.integrations.external_apis import _format_dolarapi
        assert _format_dolarapi([]) == ""
        assert _format_dolarapi(None) == ""
        assert _format_dolarapi([{"casa": "x"}]) == ""


class TestNewsTopic:
    def test_limpia_muletillas_sin_depender_de_acentos(self):
        from src.handlers.data_fetchers import _news_topic
        assert _news_topic("dame las ultimas  noticias de argentina hoy") == "argentina"
        assert _news_topic("dame las últimas noticias de argentina") == "argentina"

    def test_conserva_el_tema_real(self):
        from src.handlers.data_fetchers import _news_topic
        assert _news_topic("noticias de tecnología") == "tecnología"
        assert _news_topic("noticias sobre economía") == "economía"

    def test_vacio_si_no_queda_tema(self):
        from src.handlers.data_fetchers import _news_topic
        assert _news_topic("dame las últimas noticias") == ""
        assert _news_topic("") == ""

    def test_fetch_news_no_lanza_si_no_hay_articulos(self, monkeypatch):
        """Sin artículos debe DEGRADAR (dict), no lanzar ValueError."""
        import asyncio
        import httpx
        from src.handlers.data_fetchers import DataFetchers
        from config.settings import settings

        monkeypatch.setattr(settings, "news_api_key", "fake", raising=False)

        class _Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"status": "ok", "totalResults": 0, "articles": []}

        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
        r = asyncio.run(DataFetchers().fetch_news("dame las ultimas noticias de argentina"))
        assert isinstance(r, dict), "debe devolver dict, no lanzar"
        assert r.get("success") is False
        assert "No encontré noticias" in r.get("content", "")


# ── Fusionado desde test_axioma_14_serpapi_engines.py ──


def _norm(engine, payload):
    from src.integrations.external_apis import _normalize_engine_items
    return _normalize_engine_items(engine, payload, max_results=5)


class TestSerpapiNormalizacion:
    def test_google_images(self):
        out = _norm("google_images", {"images_results": [
            {"title": "Gato siamés", "original": "http://img/x.jpg",
             "source": "pixabay"},
        ]})
        assert out[0]["title"] == "Gato siamés"
        assert out[0]["url"] == "http://img/x.jpg"
        assert "pixabay" in out[0]["content"]

    def test_google(self):
        # ✅ v0.6.9v: motor 'google' (web general) — organic_results con
        # link+snippet. REGRESIÓN: antes faltaba esta rama en
        # _normalize_engine_items, y los resultados llegaban con url/content
        # VACÍOS → _extract_sources devolvía [] y el LLM decía
        # "no tengo información suficiente". Guarda: url y content no vacíos.
        out = _norm("google", {"organic_results": [
            {"title": "Noticias Tech", "link": "https://ejemplo.com/",
             "snippet": "Últimas noticias de tecnología"},
        ]})
        assert out[0]["title"] == "Noticias Tech"
        assert out[0]["url"] == "https://ejemplo.com/"
        assert "tecnología" in out[0]["content"]

    def test_youtube(self):
        out = _norm("youtube", {"video_results": [
            {"title": "Tutorial", "link": "http://yt/1",
             "snippet": "Aprende", "channel": {"name": "CanalX"}},
        ]})
        assert out[0]["title"] == "Tutorial"
        assert "CanalX" in out[0]["content"]
        assert "Aprende" in out[0]["content"]

    def test_google_maps(self):
        out = _norm("google_maps", {"local_results": [
            {"title": "Café X", "address": "Av. Siempre 123",
             "rating": 4.5, "reviews": 120, "phone": "+54 11 5555"},
        ]})
        assert out[0]["title"] == "Café X"
        assert "⭐ 4.5" in out[0]["content"]
        assert "Av. Siempre" in out[0]["content"]

    def test_google_shopping(self):
        out = _norm("google_shopping", {"shopping_results": [
            {"title": "Teclado", "link": "http://shop/1",
             "price": "$45.000", "source": "Tienda X"},
        ]})
        assert "45.000" in out[0]["content"]
        assert "Tienda X" in out[0]["content"]

    def test_google_scholar(self):
        out = _norm("google_scholar", {"organic_results": [
            {"title": "Paper", "link": "http://s/1",
             "snippet": "resumen", "publication": {"summary": "2020"},
             "cited_by": {"total": 42}},
        ]})
        assert "Citas: 42" in out[0]["content"]

    def test_google_patents(self):
        out = _norm("google_patents", {"organic_results": [
            {"title": "Patente", "link": "http://p/1",
             "patent_number": "US123", "assignee": "ACME",
             "publication_date": "2025-01-01"},
        ]})
        assert "US123" in out[0]["content"]
        assert "ACME" in out[0]["content"]

    def test_payload_vacio(self):
        assert _norm("google_images", {}) == []
        assert _norm("youtube", {"organic_results": []}) == []
        assert _norm("google_maps", None) == []


class TestSearcherWebSearchRegression:
    def test_google_normalize_tiene_url_y_content(self):
        """Los resultados del motor google (web general) deben traer url+snippet."""
        out = _norm("google", {"organic_results": [
            {"title": "Noticias Tech", "link": "https://ejemplo.com/",
             "snippet": "Últimas noticias"},
        ]})
        assert out[0]["url"] == "https://ejemplo.com/"
        assert out[0]["content"] == "Últimas noticias"

    def test_extract_sources_no_vacio(self):
        """Con url presente, el searcher debe extraer fuentes (no [])."""
        from src.agents.searcher import SearcherAgent
        a = SearcherAgent()
        sources = a._extract_sources([
            {"title": "T1", "url": "https://a.com/"},
            {"title": "T2", "url": "https://b.com/"},
            {"title": "T3", "url": ""},  # sin url → se descarta
        ])
        assert len(sources) == 2
        assert "https://a.com/" in sources[0]

    def test_extract_sources_filtra_sin_url(self):
        """Si todos los resultados llegan sin url (bug viejo), las fuentes = []."""
        from src.agents.searcher import SearcherAgent
        a = SearcherAgent()
        sources = a._extract_sources([
            {"title": "T1", "url": ""},
            {"title": "T2", "url": ""},
        ])
        assert sources == []


class TestScrubSecrets:
    """Las API keys nunca deben quedar en logs (URLs con api_key=...)."""

    def test_reemplaza_key_en_url(self, monkeypatch):
        from config.settings import settings
        from src.integrations.external_apis import _scrub_secrets
        monkeypatch.setattr(settings, "serpapi_api_key", "SECRETA-123")
        out = _scrub_secrets(
            "GET https://serpapi.com/search.json?engine=youtube"
            "&api_key=SECRETA-123&q=x falló: 400")
        assert "SECRETA-123" not in out
        assert "***" in out

    def test_texto_sin_key_intacto(self, monkeypatch):
        from config.settings import settings
        from src.integrations.external_apis import _scrub_secrets
        monkeypatch.setattr(settings, "serpapi_api_key", "SECRETA-123")
        assert _scrub_secrets("error normal sin claves") == \
            "error normal sin claves"
