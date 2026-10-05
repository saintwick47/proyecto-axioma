#!/usr/bin/env python3
"""
tools/vision_battery.py — Batería de diagnóstico del canal de imágenes (v0.6.9p).

Genera imágenes sintéticas (PIL) y consulta al VLM local (qwen3-vl vía
src.interfaces.web.vision_service) para medir QUÉ identifica y DÓNDE está
su límite: colores, formas, conteo, OCR (tamaño de fuente), contraste bajo,
ruido (riesgo de alucinación) y escenas simples.

Salida: reporte markdown en logs/vision_battery_<fecha>.md + resumen en
consola. Requiere Ollama con el modelo llm_vision_model (default qwen3-vl:4b).

Uso:
  python tools/vision_battery.py            # batería completa (~5-9 min en CPU)
  python tools/vision_battery.py --quick    # solo colores + OCR grande + conteo
  python tools/vision_battery.py --out /tmp/vision.md
"""
from __future__ import annotations

import argparse
import base64
import io
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# Bootstrap portable: permite correr `python tools/vision_battery.py`
# desde cualquier cwd (imports de src.*).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

_FONT_SANS = "/usr/share/fonts/TTF/Vera.ttf"
_FONT_MONO = "/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf"
_FALLBACK_FONTS = [
    "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int, mono: bool = False):
    for path in ([_FONT_MONO if mono else _FONT_SANS] + _FALLBACK_FONTS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _solid(rgb, size: int = 200) -> Image.Image:
    return Image.new("RGB", (size, size), rgb)


def _shape(kind: str, size: int = 300) -> Image.Image:
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    if kind == "circle":
        d.ellipse([60, 60, size - 60, size - 60], fill="black")
    elif kind == "square":
        d.rectangle([60, 60, size - 60, size - 60], fill="black")
    elif kind == "triangle":
        d.polygon([(size // 2, 60), (60, size - 60), (size - 60, size - 60)],
                  fill="black")
    return img


def _count_circles(n: int, size: int = 300) -> Image.Image:
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    r = 28
    for i in range(n):
        x = 60 + (i % 3) * (size - 120) // 2
        y = 60 + (i // 3) * (size - 120) // 2
        d.ellipse([x, y, x + 2 * r, y + 2 * r], fill="red")
    return img


def _text_image(text: str, size_px: int = 60, fg: tuple = (0, 0, 0),
                bg: tuple = (255, 255, 255), mono: bool = False) -> Image.Image:
    font = _font(size_px, mono=mono)
    img = Image.new("RGB", (640, 200), bg)
    d = ImageDraw.Draw(img)
    d.text((20, 60), text, fill=fg, font=font)
    return img


def _noise(size: int = 300) -> Image.Image:
    rng = random.Random(42)
    img = Image.new("RGB", (size, size))
    img.putdata([(rng.randint(0, 255), rng.randint(0, 255),
                  rng.randint(0, 255)) for _ in range(size * size)])
    return img


def _scene(size: int = 320) -> Image.Image:
    img = Image.new("RGB", (size, size), (135, 206, 235))  # cielo
    d = ImageDraw.Draw(img)
    d.ellipse([40, 40, 110, 110], fill="yellow")            # sol
    d.rectangle([130, 150, 260, 280], fill=(150, 75, 0))    # casa
    d.polygon([(120, 150), (270, 150), (195, 80)], fill="brown")  # techo
    d.rectangle([180, 210, 230, 280], fill=(90, 60, 20))    # puerta
    return img


def _cases() -> list:
    """(id, imagen, pregunta, capacidad, nota)"""
    return [
        ("color_rojo", _solid((220, 30, 30)),
         "¿De qué color es esta imagen? Respondé con una sola palabra.",
         "color", ""),
        ("color_azul", _solid((30, 60, 220)),
         "¿De qué color es esta imagen? Respondé con una sola palabra.",
         "color", ""),
        ("forma_circulo", _shape("circle"),
         "¿Qué forma geométrica hay? Respondé con una sola palabra.",
         "forma", ""),
        ("forma_cuadrado", _shape("square"),
         "¿Qué forma geométrica hay? Respondé con una sola palabra.",
         "forma", ""),
        ("conteo_3", _count_circles(3),
         "¿Cuántos círculos rojos hay? Respondé solo con el número.",
         "conteo", ""),
        ("conteo_5", _count_circles(5),
         "¿Cuántos círculos rojos hay? Respondé solo con el número.",
         "conteo", "límite de conteo"),
        ("ocr_grande", _text_image("HOLA 123", size_px=60),
         "¿Qué texto dice exactamente? Copialo.",
         "ocr", ""),
        ("ocr_mediano", _text_image("AXIOMA 2026", size_px=24),
         "¿Qué texto dice exactamente? Copialo.",
         "ocr", "texto más chico"),
        ("ocr_chico", _text_image("abc123", size_px=10),
         "¿Qué texto dice exactamente? Copialo.",
         "ocr", "LÍMITE: fuente muy pequeña"),
        ("contraste_bajo", _text_image("PRUEBA", size_px=36,
                                       fg=(200, 200, 200), bg=(255, 255, 255)),
         "¿Qué texto dice exactamente? Copialo.",
         "ocr", "LÍMITE: contraste bajo"),
        ("ruido", _noise(),
         "¿Qué muestra esta imagen? Si es solo ruido aleatorio, decilo.",
         "robustez", "riesgo de alucinación"),
        ("escena", _scene(),
         "Describí brevemente qué hay en la imagen.",
         "escena", ""),
        ("numero_42", _text_image("42", size_px=80),
         "¿Qué número se muestra? Respondé con el número.",
         "numero", ""),
    ]


def _call_vision(b64: str, question: str) -> tuple:
    from src.interfaces.web.vision_service import describe_image
    t0 = time.time()
    try:
        out = describe_image(b64, question)
        return out.strip(), time.time() - t0, None
    except Exception as exc:  # noqa: BLE001 — reportamos cualquier fallo
        return "", time.time() - t0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Batería de visión")
    parser.add_argument("--quick", action="store_true",
                        help="Solo casos rápidos (colores + OCR grande + conteo)")
    parser.add_argument("-o", "--out", default=None,
                        help="Salida (default logs/vision_battery_<fecha>.md)")
    args = parser.parse_args()

    cases = _cases()
    if args.quick:
        wanted = {"color_rojo", "color_azul", "ocr_grande", "conteo_3"}
        cases = [c for c in cases if c[0] in wanted]

    print(f"[battery] {len(cases)} casos — VLM qwen3-vl (la primera llamada "
          f"carga el modelo, ~50 s en CPU)", file=sys.stderr)

    results = []
    for cid, img, question, cap, nota in cases:
        b64 = _png_b64(img)
        answer, seconds, error = _call_vision(b64, question)
        ok = error is None and bool(answer.strip())
        results.append({
            "id": cid, "cap": cap, "nota": nota, "question": question,
            "answer": answer, "seconds": seconds, "error": error, "ok": ok,
        })
        estado = f"✅ {answer[:70]!r}" if ok else f"❌ {error}"
        print(f"[{cid}] ({seconds:.0f}s) {estado}", flush=True)

    out_path = Path(args.out or (
        f"logs/vision_battery_{datetime.now():%Y%m%d_%H%M%S}.md"))
    lines = [
        "# 🔍 Batería de visión — qwen3-vl (v0.6.9p)",
        "",
        f"- Fecha: {datetime.now():%Y-%m-%d %H:%M}",
        f"- Modelo: settings.llm_vision_model (qwen3-vl:4b)",
        f"- Casos: {len(results)} · exitosos: "
        f"{sum(1 for r in results if r['ok'])}/{len(results)}",
        "",
        "| caso | capacidad | nota | respuesta | tiempo |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ans = (r["answer"] or r["error"] or "-").replace("|", "\\|")
        lines.append(f"| {r['id']} | {r['cap']} | {r['nota']} | "
                     f"{ans[:90]} | {r['seconds']:.0f}s |")
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    # ✅ ISSUE-058: podar reportes viejos de la batería (se acumulaban).
    try:
        from tools.detailed_logger import prune_reports
        prune_reports("vision_battery_*.md", keep=2, protect=out_path)
    except Exception:
        pass
    print(f"\n[+] Reporte escrito en: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
