#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Verificación en NAVEGADOR REAL de la guía de primer arranque
# ═══════════════════════════════════════════════════════════════
# Comprueba las DOS ramas del comportamiento, no sólo una:
#   1. **Primera vez** (sin el marcador): la pantalla "Configurar AXIOMA" se abre SOLA, con el saludo
#      que explica qué hace y que se instala con un clic.
#   2. **Segunda vez** (con el marcador): NO se abre sola (no molesta al usuario que ya la vio).
#
# Uso:
#   venv/bin/python tools/verificar_primer_arranque.py                 # contra 127.0.0.1:8080
#   venv/bin/python tools/verificar_primer_arranque.py --url http://…  # otra dirección
#
# Requisitos: AXIOMA andando (`instalar/iniciar_axioma.sh`) y playwright con Chromium.
# Deja capturas en `data/capturas/` para poder mirarlas.
# Códigos de salida: 0 = se comporta bien · 4 = algo falla (o falta un requisito)
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
# La carpeta del proyecto tiene que estar en el camino de importación: al correr un guion de `tools/`,
# Python agrega `tools/` y no la raíz (misma precaución que en los demás guiones del proyecto).
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
CAPTURAS = RAIZ / "data" / "capturas"


def _marcador() -> Path:
    from config.paths import Paths
    return Paths.DATA / ".primer_arranque_listo"


def _abrir(url: str, captura: str):
    """Abre la aplicación y devuelve (¿se abrió la guía?, texto de la página, errores JS)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        pagina = navegador.new_page(viewport={"width": 1280, "height": 900})
        errores: list = []
        pagina.on("pageerror", lambda e: errores.append(str(e)))
        pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)                      # que la página se arme (NiceGUI)
        abierta = False
        try:
            pagina.wait_for_selector("text=Configurar AXIOMA", timeout=30000)
            abierta = True
        except Exception:
            abierta = False
        texto = pagina.inner_text("body")
        CAPTURAS.mkdir(parents=True, exist_ok=True)
        pagina.screenshot(path=str(CAPTURAS / captura), full_page=True)
        navegador.close()
        return abierta, texto, errores


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica la guía de primer arranque en un navegador real")
    ap.add_argument("--url", default="http://127.0.0.1:8080/")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("❌ Falta playwright (está en requirements.txt del proyecto)")
        return 4

    marca = _marcador()
    respaldo = None
    if marca.exists():
        respaldo = marca.read_text(encoding="utf-8")
        marca.unlink()
        print("ℹ️  Se quitó el marcador para simular la PRIMERA vez")

    print("\n▶ 1) Primera vez: la guía tiene que abrirse SOLA")
    abierta, texto, errores = _abrir(args.url, "primer_arranque.png")
    if not abierta:
        print("   ❌ no se abrió sola (captura: data/capturas/primer_arranque.png)")
        return 4
    print("   ✅ se abrió sola")
    if "primera vez" in texto.lower() and "un clic" in texto.lower():
        print("   ✅ con el saludo de primera vez y la explicación de que se instala con un clic")
    else:
        print("   ⚠️  abrió, pero no encontré el saludo esperado")
    if errores:
        print(f"   ⚠️  errores de JavaScript: {errores[:2]}")

    print("\n▶ 2) Segunda vez: NO tiene que abrirse sola (no molestar)")
    if not marca.exists():
        print("   ⚠️  la propia página no dejó el marcador: la guía se repetiría siempre")
        return 4
    abierta2, _, _ = _abrir(args.url, "segundo_arranque.png")
    if abierta2:
        print("   ❌ se abrió otra vez (captura: data/capturas/segundo_arranque.png)")
        return 4
    print("   ✅ no se abrió: el usuario que ya la vio no la ve de nuevo")

    if respaldo is not None:
        marca.write_text(respaldo, encoding="utf-8")
    print("\n✅ La guía se comporta como corresponde (capturas en data/capturas/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
