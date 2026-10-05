#!/usr/bin/env python3
"""
tools/vision_real_probe.py — Prueba del canal de visión con FOTOS REALES.

Descarga fotos reales de Wikimedia Commons (API abierta, sin key) por
categoría — persona/perro/gato/auto/árbol — y pregunta al VLM local qué
hay. La imagen se envía SIN nombre de archivo (sin sesgo de etiqueta).

Uso:
  python tools/vision_real_probe.py --download          # baja 5 fotos y clasifica
  python tools/vision_real_probe.py --local FOLDER      # clasifica fotos locales
  python tools/vision_real_probe.py --out DIR           # carpeta de descarga
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_API = "https://commons.wikimedia.org/w/api.php"
_CATEGORIES = {
    "persona": "person portrait face",
    "perro": "dog",
    "gato": "cat",
    "vehiculo": "car",
    "arbol": "tree",
}
_USER_AGENT = "AXIOMA-vision-probe/0.1 (test local; contacto: usuario)"


def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:  # rate limit → esperar y reintentar
                time.sleep(6 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"descarga falló tras reintentos: {last}")


def search_commons(kw: str, limit: int = 4) -> list:
    """Busca archivos de imagen en Commons y devuelve (title, thumburl_480)."""
    params = {
        "action": "query", "format": "json", "list": "search",
        "srsearch": f"filetype:bitmap {kw}", "srnamespace": "6",
        "srlimit": str(limit),
    }
    url = _API + "?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url).decode("utf-8"))
    titles = [r["title"] for r in data.get("query", {}).get("search", [])]
    if not titles:
        return []
    params2 = {
        "action": "query", "format": "json", "prop": "imageinfo",
        "iiprop": "url|size|mime", "iiurlwidth": "480",
        "titles": "|".join(titles),
    }
    url2 = _API + "?" + urllib.parse.urlencode(params2)
    data2 = json.loads(_get(url2).decode("utf-8"))
    out = []
    for page in data2.get("query", {}).get("pages", {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        mime = ii.get("mime", "")
        thumb = ii.get("thumburl") or ii.get("url")
        if mime.startswith("image/") and thumb and page.get("title"):
            out.append((page["title"], thumb))
    return out


def download_to(cat: str, title: str, thumb: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = _get(thumb, timeout=30.0)
    ext = ".jpg"
    if thumb.lower().endswith((".png",)):
        ext = ".png"
    dest = out_dir / f"{cat}_{len(list(out_dir.glob(f'{cat}_*'))):02d}{ext}"
    dest.write_bytes(data)
    return dest


def classify_local(paths: list, out_md: Path) -> list:
    from src.interfaces.web.vision_service import describe_image
    lines = ["# 📷 Proba de visión con FOTOS REALES",
             "", f"- Fecha: {datetime.now():%Y-%m-%d %H:%M}", ""]
    results = []
    for p in paths:
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        q = ("Describí en 1-2 frases qué muestra esta foto y clasificá el "
             "sujeto principal en: persona, animal, vehículo o vegetación. "
             "Formato: 'Descripción: … | Clasificación: X'.")
        t0 = time.time()
        try:
            out = describe_image(b64, q)
        except Exception as exc:
            out = f"ERROR: {exc}"
        secs = time.time() - t0
        results.append((p.name, out, secs))
        print(f"[{p.name}] ({secs:.0f}s) {out[:200]!r}", flush=True)
    lines.append("| archivo | respuesta del VLM | tiempo |")
    lines.append("|---|---|---|")
    for name, out, secs in results:
        safe = out.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {safe[:200]} | {secs:.0f}s |")
    lines.append("")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")
    # ✅ ISSUE-058: podar sondas viejas (se acumulaban).
    try:
        from tools.detailed_logger import prune_reports
        prune_reports("vision_real_*.md", keep=2, protect=out_md)
    except Exception:
        pass
    print(f"[+] Reporte: {out_md}", file=sys.stderr)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe de visión con fotos reales")
    parser.add_argument("--download", action="store_true",
                        help="Bajar fotos de Commons por categoría")
    parser.add_argument("--local", default=None,
                        help="Carpeta con fotos locales para clasificar")
    parser.add_argument("--out", default="data/vision_probe_real",
                        help="Carpeta de descarga")
    args = parser.parse_args()

    report = Path(f"logs/vision_real_{datetime.now():%Y%m%d_%H%M%S}.md")

    if args.local:
        folder = Path(args.local)
        paths = sorted(p for p in folder.iterdir()
                       if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
        if not paths:
            print("[!] No hay imágenes en la carpeta", file=sys.stderr)
            return 1
        classify_local(paths, report)
        return 0

    if not args.download:
        print("[!] Usá --download o --local FOLDER", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    paths = []
    for cat, kw in _CATEGORIES.items():
        try:
            hits = search_commons(kw, limit=4)
        except Exception as exc:
            print(f"[!] Sin resultados para {cat}: {exc}", file=sys.stderr)
            time.sleep(3)
            continue
        if not hits:
            print(f"[!] Sin resultados para {cat}", file=sys.stderr)
            time.sleep(3)
            continue
        title, thumb = hits[0]
        dest = download_to(cat, title, thumb, out_dir)
        paths.append(dest)
        print(f"[↓] {cat}: {title}", flush=True)
        time.sleep(2)  # cortesía con la API de Commons
    if not paths:
        print("[!] No se pudo descargar nada", file=sys.stderr)
        return 1
    classify_local(paths, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
