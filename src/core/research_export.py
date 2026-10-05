#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — F3: exportación de investigación (Markdown / HTML)
# ═══════════════════════════════════════════════════════════════
# Ruta: src/core/research_export.py
# Propósito: convertir las respuestas CON CITAS de una sesión en un
#            documento exportable (.md y .html imprimible a PDF).
# Diseño (F3 del informe):
#   · 0 LLM, 0 dependencias nuevas, 0 red. Solo stdlib.
#   · PDF: NO se agrega dependencia pesada (weasyprint/reportlab) — el HTML
#     trae hoja de impresión (@media print) para "Imprimir → Guardar como PDF"
#     del navegador. Decisión acordada con el usuario.
#   · Funciones PURAS (`collect_entries`, `build_markdown`, `build_html`)
#     para poder testear el formato sin UI ni disco.
#   · `export_research()` escribe en el workspace de la sesión
#     (WorkspaceManager) — el mismo workspace que ya sirve /workspace/download.
#   · El texto exportado pasa por `_scrub_secrets`: si una respuesta contuviera
#     una API key, NO sale al archivo.
# ═══════════════════════════════════════════════════════════════
import html as _html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

try:
    from src.utils.degradation import log_degraded
except ImportError:  # pragma: no cover — fallback defensivo
    def log_degraded(_logger, _exc, _context, **_kw):  # type: ignore[misc]
        pass

try:
    from src.core.html_document import (  # ✅ ISSUE-055: conversor compartido
    CSS as _CSS, esc as _esc, inline_md as _inline_md,
    md_to_html_body as _md_body_to_html, html_document as _html_document,
)
except ImportError:  # pragma: no cover
    _CSS = _esc = _inline_md = _md_body_to_html = _html_document = None

try:
    from src.integrations.external_apis_helpers import _scrub_secrets
except ImportError:  # pragma: no cover — sin el helper, no se filtra nada
    def _scrub_secrets(text: Any) -> str:  # type: ignore[misc]
        return str(text)


EXPORT_FORMATS = ("md", "html")
_EXT = {"md": ".md", "html": ".html"}
_MAX_EXPORT_CHARS = 400_000   # tope defensivo (sesiones enormes)


@dataclass
class ResearchEntry:
    """Una respuesta con sus citas, dentro del documento exportable."""
    question: str = ""
    answer: str = ""
    sources: List[str] = field(default_factory=list)
    timestamp: str = ""
    model: str = ""
    task_type: str = ""

    @property
    def has_citations(self) -> bool:
        return bool(self.sources)


def collect_entries(messages: Iterable[Any]) -> List[ResearchEntry]:
    """Empareja cada respuesta del asistente con la pregunta previa.

    Acepta cualquier objeto con atributos `role`/`content` (MessageData o
    similar): se usa getattr para no acoplarse a la dataclass de la UI.
    """
    entries: List[ResearchEntry] = []
    last_question = ""
    for msg in messages or []:
        role = (getattr(msg, "role", "") or "").lower()
        content = getattr(msg, "content", "") or ""
        if role == "user":
            last_question = content.strip()
            continue
        if role != "assistant":
            continue
        raw_sources = getattr(msg, "sources", None) or []
        sources = [s.strip() for s in raw_sources
                   if isinstance(s, str) and s.strip()]
        entries.append(ResearchEntry(
            question=last_question,
            answer=content.strip(),
            sources=list(dict.fromkeys(sources)),
            timestamp=str(getattr(msg, "timestamp", "") or ""),
            model=str(getattr(msg, "model", "") or getattr(msg, "model_used", "") or ""),
            task_type=str(getattr(msg, "task_type", "") or ""),
        ))
        last_question = ""
    return entries


def export_summary(entries: List[ResearchEntry]) -> Dict[str, int]:
    """Conteos para la UI (cuánto hay para exportar)."""
    answers = [e for e in entries if e.answer]
    with_cites = [e for e in answers if e.has_citations]
    unique_sources = {s for e in answers for s in e.sources}
    return {
        "answers": len(answers),
        "with_citations": len(with_cites),
        "sources": len(unique_sources),
    }


def _unique_sources(entries: List[ResearchEntry]) -> List[str]:
    seen: Dict[str, None] = {}
    for e in entries:
        for s in e.sources:
            seen.setdefault(s, None)
    return list(seen)


def build_markdown(entries: List[ResearchEntry], session_id: str = "",
                   meta: Optional[Dict[str, Any]] = None,
                   generated_at: Optional[str] = None) -> str:
    """Documento Markdown con metadatos, respuestas y citas."""
    meta = meta or {}
    stats = export_summary(entries)
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out: List[str] = [
        "# 🔎 Investigación — AXIOMA",
        "",
        f"- **Generado:** {stamp}",
        f"- **Sesión:** `{session_id or 'sin-sesión'}`",
    ]
    if meta.get("model"):
        out.append(f"- **Modelo:** `{meta['model']}`")
    out += [
        f"- **Respuestas:** {stats['answers']} "
        f"({stats['with_citations']} con citas)",
        f"- **Fuentes únicas:** {stats['sources']}",
        "",
        "---",
        "",
    ]

    if not stats["answers"]:
        out += ["> No hay respuestas en esta sesión todavía.", ""]
        return "\n".join(out)

    for i, e in enumerate(entries, 1):
        if not e.answer:
            continue
        out.append(f"## {i}. {e.question or '(sin pregunta registrada)'}")
        out.append("")
        if e.timestamp or e.task_type or e.model:
            bits = [b for b in (e.timestamp, e.task_type, e.model) if b]
            out.append(f"<sub>{' · '.join(bits)}</sub>")
            out.append("")
        out.append(e.answer)
        out.append("")
        if e.sources:
            out.append(f"### Fuentes ({len(e.sources)})")
            out.append("")
            for s in e.sources:
                out.append(f"- {s}")
            out.append("")
        out.append("---")
        out.append("")

    all_sources = _unique_sources(entries)
    if all_sources:
        out += ["## 📚 Índice de fuentes", ""]
        for i, s in enumerate(all_sources, 1):
            out.append(f"{i}. {s}")
        out.append("")
    return _scrub_secrets("\n".join(out))[:_MAX_EXPORT_CHARS]


# ═══════════════════════════════════════════════════════════════
# Markdown → HTML (v0.6.9v / ISSUE-055)
# ═══════════════════════════════════════════════════════════════
# El conversor y el CSS viven en `src/core/html_document.py` (una sola
# implementación del ESCAPADO, compartida con el export de conversaciones).
# Se importan con los nombres históricos de este módulo (arriba) para no romper
# a quien los importaba desde acá.


def build_html(entries: List[ResearchEntry], session_id: str = "",
               meta: Optional[Dict[str, Any]] = None,
               generated_at: Optional[str] = None) -> str:
    """Documento HTML autocontenido (imprimible a PDF desde el navegador)."""
    meta = meta or {}
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats = export_summary(entries)
    # El cuerpo se arma directamente (no se re-parsea el título del .md)
    body_parts: List[str] = []
    for i, e in enumerate([x for x in entries if x.answer], 1):
        body_parts.append(f"<h2>{i}. {_esc(e.question or '(sin pregunta registrada)')}</h2>")
        bits = [b for b in (e.timestamp, e.task_type, e.model) if b]
        if bits:
            body_parts.append(f'<p class="meta">{_esc(" · ".join(bits))}</p>')
        body_parts.append(_md_body_to_html(e.answer))
        if e.sources:
            body_parts.append(f"<h3>Fuentes ({len(e.sources)})</h3><ul>")
            for s in e.sources:
                body_parts.append(f"<li>{_esc(s)}</li>")
            body_parts.append("</ul>")

    all_sources = _unique_sources(entries)
    if all_sources:
        body_parts.append("<h2>📚 Índice de fuentes</h2><ol>")
        for s in all_sources:
            body_parts.append(f"<li>{_esc(s)}</li>")
        body_parts.append("</ol>")

    if not stats["answers"]:
        body_parts.append("<blockquote>No hay respuestas en esta sesión todavía.</blockquote>")

    header_meta = [f"Generado: {stamp}",
                   f"Sesión: {session_id or 'sin-sesión'}"]
    if meta.get("model"):
        header_meta.append(f"Modelo: {meta['model']}")
    header_meta.append(
        f"Respuestas: {stats['answers']} ({stats['with_citations']} con citas)")
    header_meta.append(f"Fuentes únicas: {stats['sources']}")

    doc = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Investigación — AXIOMA {stamp}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>🔎 Investigación — AXIOMA</h1>
<div class="summary"><p class="meta">{_esc(" · ".join(header_meta))}</p></div>
{chr(10).join(body_parts)}
<footer>Generado por AXIOMA · {_esc(stamp)} · para PDF: Imprimir → Guardar como PDF</footer>
</body>
</html>
"""
    return _scrub_secrets(doc)[:_MAX_EXPORT_CHARS]


def suggested_filename(session_id: str = "", fmt: str = "md",
                       when: Optional[str] = None) -> str:
    """Nombre de archivo seguro (sin separadores de path)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"formato no soportado: {fmt!r}")
    stamp = when or datetime.now().strftime("%Y%m%d_%H%M%S")
    sid = re.sub(r"[^A-Za-z0-9_-]", "", (session_id or "")[:8]) or "sesion"
    return f"investigacion_{sid}_{stamp}{_EXT[fmt]}"


def export_research(entries: List[ResearchEntry], session_id: str,
                    fmt: str = "md", meta: Optional[Dict[str, Any]] = None,
                    out_dir: Optional[Any] = None) -> Path:
    """Escribe el documento en disco y devuelve su ruta.

    Args:
        entries: entradas a exportar (de `collect_entries`).
        session_id: sesión (define el workspace destino).
        fmt: 'md' o 'html'.
        meta: metadatos opcionales (model, ...).
        out_dir: directorio destino; por defecto el workspace de la sesión.
    """
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"formato no soportado: {fmt!r}")
    content = (build_markdown(entries, session_id, meta) if fmt == "md"
               else build_html(entries, session_id, meta))

    if out_dir is None:
        from src.core.workspace_manager import get_workspace_manager
        target_dir = Path(get_workspace_manager().get_workspace(session_id))
    else:
        target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    path = target_dir / suggested_filename(session_id, fmt)
    path.write_text(content, encoding="utf-8")
    logger.info(f"[research_export] {fmt} escrito en {path} "
                f"({len(content)} chars)")
    return path


def export_all(entries: List[ResearchEntry], session_id: str,
               meta: Optional[Dict[str, Any]] = None,
               out_dir: Optional[Any] = None) -> Dict[str, Path]:
    """Exporta Markdown y HTML; devuelve {'md': Path, 'html': Path}."""
    result: Dict[str, Path] = {}
    for fmt in EXPORT_FORMATS:
        try:
            result[fmt] = export_research(entries, session_id, fmt, meta, out_dir)
        except Exception as _d2e:  # noqa: BLE001 — un formato no debe tumbar el otro
            log_degraded(logger, _d2e, f"research_export: formato {fmt}")
    return result


if __name__ == "__main__":  # pragma: no cover — utilidad manual
    demo = [ResearchEntry(question="¿Qué es AXIOMA?", answer="Un **agente** local.",
                          sources=["Doc - https://example.com"], timestamp="2026-01-01")]
    print(build_markdown(demo, "demo"))
