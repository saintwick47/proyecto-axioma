#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — external_apis_helpers.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/integrations/external_apis_helpers.py
#
# ✅ v0.6.9v (REFACTOR): capa de HELPERS de integración extraída de
#   src/integrations/external_apis.py (el original queda intacto).
#
#   Qué se movió desde external_apis.py (sin cambios de comportamiento):
#     - `_scrub_secrets`            → nunca filtrar API keys a los logs
#     - Helpers de logging `_log_event` / `_log_error` / `_log_api_call`
#       (+ LOGGER_AVAILABLE / get_logger)
#     - Dólar: `_DOLAR_HINT` / `_query_es_dolar` / `_format_dolarapi`
#     - Motores SerpAPI: `_ENGINE_LIST_KEYS` / `_normalize_engine_items`
#
#   `ExternalAPIManager` (la clase pública) permanece en external_apis.py, que
#   re-exporta estos símbolos para no romper la API pública del módulo
#   (consumidores: searcher.py, smart_router_api.py, tool_implementations.py y
#   los tests test_axioma_13 / test_axioma_14).
#
#   Este módulo NO importa nada de external_apis.py → sin ciclo de import.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from config.settings import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# SEGURIDAD — scrub de secretos
# ═══════════════════════════════════════════════════════════════
def _scrub_secrets(text: Any) -> str:
    """Reemplaza cualquier API key configurada dentro de un texto por '***'.

    🔒 Seguridad (v0.6.9q): los errores de httpx incluyen la URL completa —
    en SerpAPI la key viaja en el query string y NO debe quedar en logs.
    """
    s = str(text)
    for attr in ("tavily_api_key", "serper_api_key", "serpapi_api_key",
                 "news_api_key", "google_api_key", "search_engine_id",
                 "brave_api_key"):
        val = getattr(settings, attr, None)
        if val:
            s = s.replace(str(val), "***")
    return s


# ============================================================================
# IMPORT SEGURO DE DETAILEDLOGGER
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False


# ============================================================================
# HELPERS DE LOGGING — VERIFICACIÓN ROBUSTA
# ============================================================================
def _log_event(category: str, message: str, level: str = "INFO", extra: Dict[str, Any] = None):
    """Registra evento genérico de integración en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_event(category=category, message=message, level=level, extra=extra)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 external_apis_helpers.py] excepción degradada (intencional): {_d2e}")


def _log_error(error: Exception, context: str, extra_info: Dict[str, Any] = None):
    """Registra error con traceback completo en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        logger.error(f"external_apis - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, extra_info=extra_info,
                          function_name=f"external_apis.{context}")
    except Exception:
        logger.error(f"external_apis - {context}: {error}")


def _log_api_call(api_name: str, endpoint: str, operation: str,
                  success: bool, duration: float = None, status_code: int = None,
                  response_size: int = None, error: str = None):
    """Registra llamada específica a API externa con métricas completas."""
    if not LOGGER_AVAILABLE:
        return
    extra = {"api": api_name, "endpoint": endpoint, "operation": operation, "success": success}
    if duration is not None: extra["duration_ms"] = round(duration * 1000, 2)
    if status_code is not None: extra["status_code"] = status_code
    if response_size is not None: extra["response_bytes"] = response_size
    if error: extra["error"] = error
    level = "INFO" if success else "WARNING" if status_code == 429 else "ERROR"
    _log_event("API_CALL", f"[{'✓' if success else '✗'}] {api_name}.{operation}", level=level, extra=extra)


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9q: dólar → dolarapi.com (público, SIN API key) — la key de
# Tavily de este equipo es inválida y "¿a cuánto está el dólar?" fallaba.
# ═══════════════════════════════════════════════════════════════
_DOLAR_HINT = re.compile(
    r"\bd[oó]lar(?:es)?\b|\busd\b|\bblue\b|\boficial\b|\bmep\b|\bccl\b|"
    r"\bcontado\b|\beuro\b|cotizaci[oó]n", re.IGNORECASE)


def _query_es_dolar(query: str) -> bool:
    return bool(query and _DOLAR_HINT.search(query))


def _format_dolarapi(data: Any) -> str:
    lines = ["💵 Cotizaciones (dolarapi):"]
    items = data if isinstance(data, list) else []

    def _fmt(x: Any) -> str:
        if isinstance(x, (int, float)):
            return f"${x:,.2f}".replace(",", ".")
        return str(x)

    for item in items:
        casa = item.get("casa") or item.get("nombre") or "?"
        compra, venta = item.get("compra"), item.get("venta")
        if compra is None and venta is None:
            continue
        lines.append(f"- {casa}: compra {_fmt(compra)} · venta {_fmt(venta)}")
    fechas = {i.get("fechaActualizacion", "") for i in items
              if isinstance(i, dict) and i.get("fechaActualizacion")}
    if fechas:
        lines.append(
            f"(actualizado {sorted(fechas)[-1][:19].replace('T', ' ')} UTC)")
    return "\n".join(lines) if len(lines) > 1 else ""


# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9q: motores SerpAPI genéricos (google_images, youtube, maps,
# shopping, scholar, patents) — normalización → {title, url, content}.
# Cada motor con su propia clave de lista en la respuesta.
# ═══════════════════════════════════════════════════════════════
_ENGINE_LIST_KEYS = {
    "google": ("organic_results",),  # ✅ v0.6.9v: web general
    "google_images": ("images_results",),
    "youtube": ("video_results", "results"),
    "google_maps": ("local_results",),
    "google_shopping": ("shopping_results",),
    "google_scholar": ("organic_results",),
    "google_patents": ("organic_results",),
}


def _normalize_engine_items(engine: str, data: Any,
                            max_results: int) -> List[Dict[str, Any]]:
    """Normaliza la respuesta de un motor SerpAPI a entradas uniformes."""
    payload = data if isinstance(data, dict) else {}
    raw: List[Any] = []
    for key in _ENGINE_LIST_KEYS.get(engine, ()):
        raw = payload.get(key) or []
        if raw:
            break
    out: List[Dict[str, Any]] = []
    for item in raw[:max_results]:
        title = item.get("title") or item.get("name") or ""
        content = ""
        link = ""
        if engine == "google_images":
            src = item.get("source") or ""
            content = f"Imagen ({src})" if src else "Imagen"
            link = item.get("original") or item.get("link") or ""
        elif engine == "youtube":
            ch = item.get("channel") or {}
            chn = ch.get("name") if isinstance(ch, dict) else ""
            parts = [item.get("snippet") or ""]
            if chn:
                parts.append(f"Canal: {chn}")
            content = " · ".join(p for p in parts if p)
            link = item.get("link") or ""
        elif engine == "google":
            # ✅ v0.6.9v: general web search — organic_results con link+snippet.
            link = item.get("link") or item.get("url") or ""
            content = item.get("snippet") or ""
        elif engine == "google_maps":
            parts = []
            if item.get("rating") is not None:
                parts.append(f"⭐ {item['rating']}"
                             + (f" ({item.get('reviews')})"
                                if item.get("reviews") is not None else ""))
            for field in ("address", "phone"):
                if item.get(field):
                    parts.append(str(item[field]))
            content = " · ".join(parts)
            link = item.get("link") or ""
        elif engine == "google_shopping":
            parts = [str(x) for x in (item.get("price"),
                                      item.get("source")) if x]
            content = " · ".join(parts)
            link = item.get("link") or ""
        elif engine == "google_scholar":
            pub = item.get("publication") or {}
            cb = item.get("cited_by") or {}
            parts = [item.get("snippet") or "",
                     pub.get("summary") if isinstance(pub, dict) else ""]
            if isinstance(cb, dict) and cb.get("total") is not None:
                parts.append(f"Citas: {cb['total']}")
            content = " · ".join(p for p in parts if p)
            link = item.get("link") or ""
        elif engine == "google_patents":
            parts = [item.get("snippet") or ""]
            for label, key in (("Patente", "patent_number"),
                               ("Asignatario", "assignee"),
                               ("Fecha", "publication_date")):
                if item.get(key):
                    parts.append(f"{label}: {item[key]}")
            content = " · ".join(parts)
            link = item.get("link") or ""
        if title or link:
            out.append({"title": title, "url": link,
                        "content": content.strip()})
    return out


__all__ = [
    "_scrub_secrets",
    "_log_event",
    "_log_error",
    "_log_api_call",
    "_query_es_dolar",
    "_format_dolarapi",
    "_ENGINE_LIST_KEYS",
    "_normalize_engine_items",
]
