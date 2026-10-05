#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — html_document.py: conversor Markdown→HTML y documento
# ═══════════════════════════════════════════════════════════════
# Ruta: src/core/html_document.py
#
# POR QUÉ: la exportación a HTML (F3, ISSUE-040) tenía el conversor y el CSS
# dentro de `research_export.py`. Con ISSUE-055 el **mismo** HTML hace falta para
# las conversaciones completas. Duplicarlo habría duplicado también la parte
# **crítica de seguridad** (escapado), así que se extrae acá: una sola
# implementación del escape, usada por ambos exporters (esquema 50/50:
# `research_export` lo re-exporta y sus consumidores no cambian).
#
# ⚠️ REGLA DE SEGURIDAD (la más importante de este módulo):
#   **escapar SIEMPRE antes de insertar etiquetas.** El contenido viene del LLM,
#   de títulos de fuentes y de archivos de código: no es HTML confiable. El orden
#   correcto es `_inline_md(_esc(texto))`, nunca al revés.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import html as _html
import re
from typing import Iterable, List, Optional

# ── CSS compartido (documento imprimible: Ctrl+P → PDF) ─────────
CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       max-width: 52rem; margin: 0 auto; padding: 2rem 1.25rem;
       line-height: 1.55; color: #1a1a1a; background: #fff; }
h1 { font-size: 1.7rem; margin-bottom: .25rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid #e5e5e5;
     padding-bottom: .3rem; }
h3 { font-size: 1rem; margin-bottom: .4rem; }
p.meta { color: #666; font-size: .78rem; margin: -.4rem 0 .8rem; }
code { background: #f2f2f2; padding: .1rem .3rem; border-radius: 3px;
       font-size: .9em; }
pre { background: #f6f8fa; padding: .8rem; border-radius: 6px;
      overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #d0d0d0; margin: .5rem 0;
             padding-left: .8rem; color: #444; }
ul { padding-left: 1.3rem; }
a { color: #0b62d0; word-break: break-all; }
hr { border: none; border-top: 1px solid #eee; margin: 1.6rem 0; }
.summary { background: #f8f9fb; border: 1px solid #e6e8ee;
           border-radius: 8px; padding: .8rem 1rem; }
footer { margin-top: 2.5rem; color: #888; font-size: .8rem;
         border-top: 1px solid #eee; padding-top: .6rem; }
@media print { body { max-width: none; padding: 0; }
  a { color: #000; text-decoration: none; }
  h2 { page-break-after: avoid; } pre { page-break-inside: avoid; } }
"""


def esc(text: object) -> str:
    """Escapa texto para insertarlo como TEXTO en HTML."""
    return _html.escape(str(text), quote=True)


def inline_md(text: str) -> str:
    """Negritas, itálicas, código inline y URLs sobre texto YA escapado."""
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  r'<a href="\2" rel="noopener noreferrer">\1</a>', text)
    text = re.sub(r"(?<![\"'>=])(https?://[^\s<)\]]+)",
                  r'<a href="\1" rel="noopener noreferrer">\1</a>', text)
    return text


def md_to_html_body(md: str) -> str:
    """Convierte un subconjunto de Markdown a HTML SEGURO.

    Soporta: fences ```, títulos #..###, listas -, citas >, `---`, párrafos,
    líneas `<sub>…</sub>` (metadatos en gris) y el formato inline de
    `inline_md`. Todo lo demás queda como texto plano escapado.
    """
    lines = (md or "").split("\n")
    out: List[str] = []
    in_code = False
    in_list = False
    para: List[str] = []

    def _flush_para() -> None:
        if para:
            # ⚠️ escapar ANTES de aplicar el markdown inline.
            out.append(f"<p>{inline_md(esc(' '.join(para)))}</p>")
            para.clear()

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            _flush_para()
            _close_list()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(esc(line))
            continue
        if not line.strip():
            _flush_para()
            _close_list()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            _flush_para()
            _close_list()
            level = min(len(m.group(1)) + 1, 6)   # el h1 del documento ya existe
            out.append(f"<h{level}>{inline_md(esc(m.group(2)))}</h{level}>")
            continue
        if line.lstrip().startswith(("- ", "* ")):
            _flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline_md(esc(line.lstrip()[2:]))}</li>")
            continue
        if line.lstrip().startswith("> "):
            _flush_para()
            _close_list()
            out.append(f"<blockquote>{inline_md(esc(line.lstrip()[2:]))}</blockquote>")
            continue
        if line.strip() == "---":
            _flush_para()
            _close_list()
            out.append("<hr>")
            continue
        if line.strip().startswith("<sub>"):
            _flush_para()
            _close_list()
            inner = line.strip()[5:-6] if line.strip().endswith("</sub>") \
                else line.strip()[5:]
            out.append(f'<p class="meta">{esc(inner)}</p>')
            continue
        para.append(line.strip())

    _flush_para()
    _close_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def html_document(title: str, body_html: str,
                  meta_lines: Optional[Iterable[str]] = None,
                  footer: str = "") -> str:
    """Documento HTML autocontenido (CSS inline + hoja de impresión)."""
    meta_block = ""
    lineas = [m for m in (meta_lines or []) if m]
    if lineas:
        meta_block = (f'<div class="summary"><p class="meta">'
                      f'{esc(" · ".join(lineas))}</p></div>')
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{esc(title)}</h1>
{meta_block}
{body_html}
<footer>{esc(footer) if footer else "Generado por AXIOMA"}</footer>
</body>
</html>
"""


__all__ = ["CSS", "esc", "inline_md", "md_to_html_body", "html_document"]
