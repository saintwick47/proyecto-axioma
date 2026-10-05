#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# tool_implementations.py - Implementaciones de Tools (External & Data)
# Ruta: /ruta/a/axioma/src/tools_mcp/tool_implementations.py
# Autor: SaintWick
# Versión: 1.5.0
#
# Implementaciones concretas de tools de AXIOMA: adapters para APIs externas
# (weather, finance, news, web_search) y tools de filesystem (lectura, escritura,
# listado, eliminación, movimiento). Esta capa de datos es registrada desde
# tool_registry.py y no conoce el registry.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import ast
import logging
import mimetypes
import operator
import os
import time
from pathlib import Path
from typing import Any, Optional

from .tool_base import (
    BaseTool,
    ToolPermission,
    ToolResult,
    ToolSchema,
    ToolParam,
)

# ✅ v0.6.9v (REFACTOR 50/50): herramientas de filesystem (Fs*) movidas a
# src/tools_mcp/tool_fs_implementations.py. Se re-exportan acá para que
# tool_registry.py (from ...tool_implementations import FsReadTool, …) NO cambie.
from .tool_fs_implementations import (  # noqa: E402,F401
    FsReadTool,
    FsWriteTool,
    FsListTool,
    FsSearchTool,
    FsDeleteTool,
    FsMoveTool,
    # ✅ v0.6.9v (ISSUE): _classify_kind y constantes de texto se movieron con
    # las Fs*, pero `src/core/promotion_gate.py` las importa desde acá.
    # Re-exporta para preservar la API pública de este módulo.
    _classify_kind,
    _TEXT_SUFFIXES,
    _TEXT_FILENAMES,
)

logger = logging.getLogger(__name__)


# ============================================================================
# ADAPTERS — Wrappers de ExternalAPIManager (sin romper nada existente)
# ============================================================================

class WeatherTool(BaseTool):
    """Clima actual para cualquier ubicación vía ExternalAPIManager."""
    name = "weather"
    description = (
        "Obtiene el clima actual, temperatura, humedad y pronóstico "
        "para una ubicación. Usa APIs externas (Tavily/Serper) con "
        "fallback a LLM si no hay API key configurada."
    )
    permission_level = ToolPermission.NETWORK
    tags = ["api", "weather", "external"]

    schema = ToolSchema(
        name="weather",
        description=description,
        params=[
            ToolParam("query", "str", "Consulta de clima, ej: 'clima en Buenos Aires'"),
        ],
        returns="str — descripción del clima con temperatura y condiciones",
        examples=["weather(query='clima en Córdoba')"],
        tags=["api", "weather"],
    )

    def _run(self, query: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            from src.integrations.external_apis import ExternalAPIManager
            import asyncio

            manager = ExternalAPIManager()

            async def _call():
                await manager.initialize()  # ✅ FIX AUDITORÍA: sin esto, _api_status quedaba en False siempre — execute()/search_web() fallaban SIEMPRE sin importar si había API key configurada
                return await manager.execute("weather_query", query)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_call())
            finally:
                loop.close()

            latency = (time.perf_counter() - t0) * 1000
            if result.get("success"):
                return ToolResult.ok(
                    output=result.get("summary") or result.get("answer") or "",  # ✅ FIX AUDITORÍA: execute() nunca devuelve "content" (devuelve "summary"/"answer") — output quedaba vacío siempre
                    tool_name=self.name,
                    latency_ms=latency,
                    data=result.get("data"),
                    source="api",
                    metadata={"timestamp": result.get("timestamp")},
                )
            return ToolResult.fail(
                error=result.get("error", "API sin respuesta"),
                tool_name=self.name,
                latency_ms=latency,
            )
        except ImportError:
            return ToolResult.fail(
                error="ExternalAPIManager no disponible",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def is_available(self) -> bool:
        try:
            from config.settings import settings
            return bool(settings.tavily_api_key or settings.serper_api_key)
        except ImportError:
            return False


class FinanceTool(BaseTool):
    """Cotizaciones y datos financieros vía ExternalAPIManager."""
    name = "finance"
    description = (
        "Obtiene cotizaciones de divisas, acciones y datos financieros. "
        "Incluye dólar blue, MEP, CCL y precios de mercado."
    )
    permission_level = ToolPermission.NETWORK
    tags = ["api", "finance", "external"]

    schema = ToolSchema(
        name="finance",
        description=description,
        params=[
            ToolParam("query", "str", "Consulta financiera, ej: 'cotización dólar blue'"),
        ],
        returns="str — datos financieros actualizados",
        tags=["api", "finance"],
    )

    def _run(self, query: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            from src.integrations.external_apis import ExternalAPIManager
            import asyncio

            manager = ExternalAPIManager()

            async def _call():
                await manager.initialize()  # ✅ FIX AUDITORÍA: sin esto, _api_status quedaba en False siempre — execute()/search_web() fallaban SIEMPRE sin importar si había API key configurada
                return await manager.execute("finance_query", query)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_call())
            finally:
                loop.close()

            latency = (time.perf_counter() - t0) * 1000
            if result.get("success"):
                return ToolResult.ok(
                    output=result.get("summary") or result.get("answer") or "",  # ✅ FIX AUDITORÍA: execute() nunca devuelve "content" (devuelve "summary"/"answer") — output quedaba vacío siempre
                    tool_name=self.name,
                    latency_ms=latency,
                    data=result.get("data"),
                    source="api",
                )
            return ToolResult.fail(
                error=result.get("error", "API sin respuesta"),
                tool_name=self.name,
                latency_ms=latency,
            )
        except ImportError:
            return ToolResult.fail(
                error="ExternalAPIManager no disponible",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def is_available(self) -> bool:
        try:
            from config.settings import settings
            return bool(settings.tavily_api_key or settings.serper_api_key)
        except ImportError:
            return False


class NewsTool(BaseTool):
    """Noticias recientes vía ExternalAPIManager."""
    name = "news"
    description = (
        "Obtiene noticias recientes sobre cualquier tema usando "
        "NewsAPI, Tavily o Serper según disponibilidad."
    )
    permission_level = ToolPermission.NETWORK
    tags = ["api", "news", "external"]

    schema = ToolSchema(
        name="news",
        description=description,
        params=[
            ToolParam("query", "str", "Tema de búsqueda, ej: 'noticias tecnología Argentina'"),
        ],
        returns="str — resumen de noticias recientes con fuentes",
        tags=["api", "news"],
    )

    def _run(self, query: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            from src.integrations.external_apis import ExternalAPIManager
            import asyncio

            manager = ExternalAPIManager()

            async def _call():
                await manager.initialize()  # ✅ FIX AUDITORÍA: sin esto, _api_status quedaba en False siempre — execute()/search_web() fallaban SIEMPRE sin importar si había API key configurada
                return await manager.execute("news_query", query)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_call())
            finally:
                loop.close()

            latency = (time.perf_counter() - t0) * 1000
            if result.get("success"):
                return ToolResult.ok(
                    output=result.get("summary") or result.get("answer") or "",  # ✅ FIX AUDITORÍA: execute() nunca devuelve "content" (devuelve "summary"/"answer") — output quedaba vacío siempre
                    tool_name=self.name,
                    latency_ms=latency,
                    data=result.get("data"),
                    source="api",
                )
            return ToolResult.fail(
                error=result.get("error", "API sin respuesta"),
                tool_name=self.name,
                latency_ms=latency,
            )
        except ImportError:
            return ToolResult.fail(
                error="ExternalAPIManager no disponible",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def is_available(self) -> bool:
        try:
            from config.settings import settings
            # ✅ v0.6.9q: SerpAPI google_news cubre noticias sin NewsAPI
            return bool(settings.news_api_key or settings.tavily_api_key
                        or getattr(settings, "serpapi_api_key", None))
        except ImportError:
            return False


class WebSearchTool(BaseTool):
    """Búsqueda web general vía ExternalAPIManager."""
    name = "web_search"
    description = (
        "Realiza búsquedas web generales usando Tavily, Serper o SerpAPI. "
        "Retorna resultados relevantes con URLs y snippets."
    )
    permission_level = ToolPermission.NETWORK
    tags = ["api", "search", "external"]

    schema = ToolSchema(
        name="web_search",
        description=description,
        params=[
            ToolParam("query", "str", "Consulta de búsqueda"),
            ToolParam("max_results", "int", "Máximo de resultados", required=False, default=5),
        ],
        returns="str — resultados de búsqueda con URLs y resúmenes",
        tags=["api", "search"],
    )

    def _run(self, query: str = "", max_results: int = 5, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            from src.integrations.external_apis import ExternalAPIManager
            import asyncio

            manager = ExternalAPIManager()

            async def _call():
                await manager.initialize()  # ✅ FIX AUDITORÍA: sin esto, _api_status quedaba en False siempre — execute()/search_web() fallaban SIEMPRE sin importar si había API key configurada
                return await manager.execute("web_search", query, max_results=max_results)

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(_call())
            finally:
                loop.close()

            latency = (time.perf_counter() - t0) * 1000
            if result.get("success"):
                return ToolResult.ok(
                    output=result.get("summary") or result.get("answer") or "",  # ✅ FIX AUDITORÍA: execute() nunca devuelve "content" (devuelve "summary"/"answer") — output quedaba vacío siempre
                    tool_name=self.name,
                    latency_ms=latency,
                    data=result.get("data"),
                    source="api",
                )
            return ToolResult.fail(
                error=result.get("error", "Búsqueda sin resultados"),
                tool_name=self.name,
                latency_ms=latency,
            )
        except ImportError:
            return ToolResult.fail(
                error="ExternalAPIManager no disponible",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

    def is_available(self) -> bool:
        try:
            from config.settings import settings
            # ✅ v0.6.9q: SerpAPI (serpapi.com) como proveedor de búsqueda
            return bool(settings.tavily_api_key or settings.serper_api_key
                        or getattr(settings, "serpapi_api_key", None))
        except ImportError:
            return False


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9q: tools SerpAPI por motor — imágenes, YouTube, mapas,
# compras, scholar y patentes. Sirven de RESPALDO si otro proveedor se
# bloquea o limita (todas comparten la key de SerpAPI).
# ═══════════════════════════════════════════════════════════════
def _make_engine_tool(name: str, engine: str, label: str,
                      hint: str, engine_hint: str) -> type:
    """Crea una tool BaseTool para un motor SerpAPI genérico."""
    description = (
        f"Busca {hint} usando el motor SerpAPI '{engine}' ({label}). "
        f"{engine_hint}"
    )

    def _run(self, query: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            import asyncio
            from src.integrations.external_apis import ExternalAPIManager
            manager = ExternalAPIManager()

            async def _call():
                await manager.initialize()
                return await manager.search_engine(engine, query or "",
                                                   max_results=5)

            loop = asyncio.new_event_loop()
            try:
                results = loop.run_until_complete(_call())
            finally:
                loop.close()
            latency = (time.perf_counter() - t0) * 1000
            if not results:
                return ToolResult.fail(
                    error="Sin resultados (¿API limitada? probá otro motor)",
                    tool_name=name, latency_ms=latency)
            lines = []
            for r in results[:5]:
                block = f"**{r['title']}**\n{r['url']}"
                if r.get("content"):
                    block += f"\n{r['content']}"
                lines.append(block)
            return ToolResult.ok(
                output="\n\n".join(lines), tool_name=name,
                latency_ms=latency, source="serpapi")
        except Exception as exc:  # noqa: BLE001 — tool falla con mensaje claro
            return ToolResult.fail(
                error=str(exc), tool_name=name,
                latency_ms=(time.perf_counter() - t0) * 1000)

    schema = ToolSchema(
        name=name,
        description=description,
        params=[
            ToolParam("query", "str", f"Qué buscar ({hint})"),
        ],
        returns="str — resultados con título, URL y detalle",
        tags=["api", "search", "serpapi"],
    )
    return type(name, (BaseTool,), {
        "name": name,
        "description": description,
        "permission_level": ToolPermission.NETWORK,
        "tags": ["api", "search", "serpapi"],
        "schema": schema,
        "_run": _run,
    })


SearchImagesTool = _make_engine_tool(
    "search_images", "google_images", "Google Images",
    "imágenes", "Devuelve títulos, enlaces a la imagen original y fuente.")
SearchYoutubeTool = _make_engine_tool(
    "search_youtube", "youtube", "YouTube",
    "videos", "Devuelve videos con canal, descripción y enlace.")
SearchMapsTool = _make_engine_tool(
    "search_maps", "google_maps", "Google Maps",
    "lugares/negocios", "Devuelve dirección, teléfono y puntuación.")
SearchShoppingTool = _make_engine_tool(
    "search_shopping", "google_shopping", "Google Shopping",
    "productos y precios", "Devuelve producto, precio y tienda.")
SearchScholarTool = _make_engine_tool(
    "search_scholar", "google_scholar", "Google Scholar",
    "artículos académicos", "Devuelve papers con citas y resumen.")
SearchPatentsTool = _make_engine_tool(
    "search_patents", "google_patents", "Google Patents",
    "patentes", "Devuelve patentes con número, asignatario y fecha.")


class CorpusSearchTool(BaseTool):
    """Búsqueda en la biblioteca documental (corpus) con citas (v0.6.9s)."""
    name = "corpus_search"
    description = (
        "Busca en la biblioteca documental local (corpus) y devuelve "
        "fragmentos (chunks) con su archivo, posición y score. Usala cuando "
        "el usuario pregunte sobre documentos/carpetas ya indexados."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["corpus", "search", "local"]

    schema = ToolSchema(
        name="corpus_search",
        description=description,
        params=[
            ToolParam("query", "str", "Consulta (ej: 'milanesa')"),
            ToolParam("k", "int", "Máximo de fragmentos (default 5)",
                      required=False, default=5),
        ],
        returns="str — fragmentos citables con archivo y score",
        tags=["corpus", "search"],
    )

    def _run(self, query: str = "", k: int = 5, db: Optional[str] = None,
             **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        from src.memory.corpus import CorpusLibrary
        lib = CorpusLibrary(db_path=db)
        try:
            results = lib.search(query, k=int(k or 5))
        finally:
            lib.close()
        latency = (time.perf_counter() - t0) * 1000
        if not results:
            return ToolResult.fail(
                error="Sin coincidencias en el corpus (¿indexaste la carpeta?)",
                tool_name=self.name, latency_ms=latency)
        lines = []
        for r in results:
            lines.append(f"**{r['path']}** (chunk {r['chunk']}, "
                         f"score {r['score']})\n{r['text']}")
        return ToolResult.ok(output="\n\n".join(lines), tool_name=self.name,
                             latency_ms=latency, source="corpus")


class CorpusIndexTool(BaseTool):
    """Indexa una carpeta en la biblioteca documental (corpus)."""
    name = "corpus_index"
    description = (
        "Indexa una carpeta local en la biblioteca documental: divide en "
        "fragmentos (chunks) con metadata y deduplicación por hash. "
        "Después se puede consultar con corpus_search."
    )
    permission_level = ToolPermission.LOCAL_WRITE
    tags = ["corpus", "ingesta", "local"]

    schema = ToolSchema(
        name="corpus_index",
        description=description,
        params=[
            ToolParam("folder", "str", "Carpeta a indexar"),
        ],
        returns="str — cantidad de archivos indexados",
        tags=["corpus", "ingesta"],
    )

    def _run(self, folder: str = "", db: Optional[str] = None,
             **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        from src.memory.corpus import CorpusLibrary
        lib = CorpusLibrary(db_path=db)
        try:
            res = lib.index_folder(folder)
            chunks = lib.stats().get("chunks", 0)
        finally:
            lib.close()
        latency = (time.perf_counter() - t0) * 1000
        if res.get("error"):
            return ToolResult.fail(error=res["error"], tool_name=self.name,
                                   latency_ms=latency)
        return ToolResult.ok(
            output=f"Indexados: {res.get('indexed_files', 0)} archivos "
                   f"({chunks} fragmentos)",
            tool_name=self.name, latency_ms=latency, source="corpus")


# ============================================================================
# FILESYSTEM TOOLS
# ============================================================================

# ── Clasificación de tipo de archivo (texto / programa / binario) ───────────
# ✅ NUEVO (agente filesystem): usado por FsReadTool/FsListTool para que el
# agente distinga contenido legible de ejecutables/binarios antes de leerlo o
# de decidir qué hacer con él. Orden de resolución: extensión/nombre conocido
# como texto (gana siempre, incluso si el archivo tiene el bit +x — un script
# con shebang sigue siendo fuente legible) → bit de ejecución (sin extensión
# de texto reconocida = "program", ej. binarios compilados) → "binary" como

# ============================================================================
# CALCULADORA ARITMÉTICA (v0.6.9p) — determinística, sin eval()
# ============================================================================
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_math(expr: str) -> float:
    """Evalúa una expresión aritmética SEGURA (nunca usa eval()).

    Solo admite constantes numéricas y operadores + - * / // % ** con
    paréntesis (y signos unarios). Cualquier otro nodo del AST (nombres,
    llamadas, atributos, import…) se rechaza: no hay ejecución de código.
    """
    expr = expr.strip()
    if not expr:
        raise ValueError("expresión vacía")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(
            f"expresión mal formada ({exc.msg or 'sintaxis inválida'})"
        ) from None

    def _ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = _ev(node.left), _ev(node.right)
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
                raise ValueError("división por cero")
            try:
                return float(_BIN_OPS[type(node.op)](left, right))
            except (OverflowError, ZeroDivisionError) as exc:
                raise ValueError(f"operación inválida: {exc}") from None
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_ev(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +_ev(node.operand)
        raise ValueError(
            "expresión no soportada (solo números y operadores + - * / // % **)")

    result = _ev(tree)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError("resultado no finito")
    return result


class CalculatorTool(BaseTool):
    """Calculadora aritmética determinística (sin red, sin eval())."""
    name = "calculator"
    description = (
        "Calcula expresiones aritméticas con precisión: + - * / // % ** y "
        "paréntesis. Usala cuando el usuario pida una cuenta (sumas, "
        "porcentajes, IVA, promedios, conversiones) en vez de calcular a "
        "mano. Ejemplos de expresión: '2+2*3', '(150*0.21)/12', '10/4', "
        "'2**10'."
    )
    permission_level = ToolPermission.READ_ONLY
    tags = ["util", "math"]

    schema = ToolSchema(
        name="calculator",
        description=description,
        params=[
            ToolParam("expression", "str",
                      "Expresión aritmética segura, ej: '(2+3)*4'"),
        ],
        returns="str — resultado numérico o error claro",
        tags=["util", "math"],
    )

    def _run(self, expression: str = "", **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            value = _eval_math(expression)
        except ValueError as exc:
            return ToolResult.fail(
                error=f"expresión inválida: {exc}",
                tool_name=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        if float(value).is_integer() and abs(value) < 1e15:
            out = str(int(value))
        else:
            out = f"{value:.10f}".rstrip("0").rstrip(".")
        return ToolResult.ok(
            output=out,
            tool_name=self.name,
            latency_ms=(time.perf_counter() - t0) * 1000,
            source="calculator",
        )


# ============================================================================
# EXPORTS — Usados por tool_registry.py para registrar las tools
# ============================================================================

__all__ = [
    # Adapters — APIs externas
    "WeatherTool",
    "FinanceTool",
    "NewsTool",
    "WebSearchTool",
    # Filesystem — Archivos y directorios
    "FsReadTool",
    "FsWriteTool",
    "FsListTool",
    "FsSearchTool",
    "FsDeleteTool",
    "FsMoveTool",
]
