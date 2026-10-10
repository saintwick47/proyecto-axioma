#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Verificación en NAVEGADOR REAL de la guía de primer arranque
# ═══════════════════════════════════════════════════════════════
# Comprueba TRES cosas, no sólo una:
#   1. **Primera vez** (sin el marcador): la pantalla "Configurar AXIOMA" se abre SOLA, con el saludo
#      que explica qué hace y que se instala con un clic.
#   2. **Segunda vez** (con el marcador): NO se abre sola (no molesta al usuario que ya la vio).
#   3. **La sección de modelos** (🎯 elegir un modelo por rol): que esté en pantalla, que muestre el
#      modelo que AXIOMA tiene configurado y que coincida con lo que dice la API y con lo instalado
#      en Ollama. Es la parte que el usuario eligió: si la pantalla dijera una cosa y AXIOMA usara
#      otra, acá se ve.
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
        try:
            pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:             # ✅ 2026-10-10: una dirección caída o un puerto no permitido
            navegador.close()              # NO tiene que salir como traceback: es un requisito que falta.
            return False, "", [f"no se pudo abrir {url}: {type(e).__name__}"]
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


def _revisar_modelos(url: str, captura: str):
    """Abre la pantalla y devuelve (campos por rol, errores JS, lo que dice la API).

    Comprueba la sección «🎯 ¿Qué modelo querés usar en cada rol?» con el navegador de verdad y
    contrastando tres cosas que tienen que coincidir: lo que **muestra la pantalla**, lo que declara
    **`GET /preflight/modelos`** y lo que está **instalado en Ollama**. Si la pantalla dijera una cosa y
    AXIOMA usara otra, acá se ve.
    """
    import httpx
    from playwright.sync_api import sync_playwright

    try:
        datos = httpx.get(f"{url.rstrip('/')}/api/v1/preflight/modelos", timeout=30).json()
    except Exception as e:
        return None, [f"no pude consultar los modelos ({type(e).__name__}); ¿está AXIOMA andando?"], {}
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        pagina = navegador.new_page(viewport={"width": 1280, "height": 1400})
        errores: list = []
        pagina.on("pageerror", lambda e: errores.append(str(e)))
        try:
            pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            navegador.close()
            return None, [f"no se pudo abrir {url}: {type(e).__name__}"], datos
        time.sleep(4)                      # que la página se arme (NiceGUI)
        try:
            seccion = pagina.locator("text=¿Qué modelo querés usar en cada rol?")
            seccion.wait_for(timeout=30000)
            seccion.scroll_into_view_if_needed()
        except Exception:
            navegador.close()
            return None, ["la sección de modelos no apareció en «Configurar AXIOMA»"], datos
        campos = pagina.eval_on_selector_all(
            ".q-select",
            """(els) => els.map(el => {
                 const et = el.querySelector('.q-field__label');
                 const inp = el.querySelector('input');
                 const r = el.getBoundingClientRect();
                 return {label: et ? et.textContent.trim() : '', valor: inp ? inp.value : '',
                         visible: r.width > 0 && r.height > 0,
                         dentroDelDialogo: !!el.closest('.q-dialog')};
               })""")
        CAPTURAS.mkdir(parents=True, exist_ok=True)
        pagina.screenshot(path=str(CAPTURAS / captura), full_page=True)
        # ✅ 2026-10-10 (lo reportó el usuario y se midió): al cerrar la pantalla NO tiene que quedar
        # nada a la vista. Los campos se dibujaban en la PÁGINA y se quedaban ahí tapando todo (y cada
        # apertura agregaba otra copia). Acá se cierra y se mide.
        cierre: dict = {"cerro": False, "campos_a_la_vista": -1, "centro": ""}
        try:
            pagina.locator("button:has-text('Cerrar')").first.click(timeout=8000)
            # Espera ACOTADA (hasta 10 s) a que la pantalla se vaya de verdad. Antes esperaba 1,5 s fijos
            # y daba un falso fallo: el botón queda abajo (medido: y=1582 en una ventana de 900) y la
            # animación de Quasar todavía no había terminado. Un tiempo fijo acá es una prueba frágil.
            for _ in range(20):
                if pagina.locator(".q-dialog:visible").count() == 0:
                    break
                time.sleep(0.5)
            cierre["cerro"] = pagina.locator(".q-dialog:visible").count() == 0
            cierre["campos_a_la_vista"] = pagina.locator(".q-select:visible").count()
            cierre["centro"] = pagina.evaluate(
                "() => {const e = document.elementFromPoint(innerWidth / 2, innerHeight / 2);"
                "return e ? (e.tagName + '.' + (e.className || '')).slice(0, 60) : '';}")
        except Exception as e:
            cierre["motivo"] = f"{type(e).__name__}: {str(e)[:80]}"
        navegador.close()
    return campos, errores, datos, cierre


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
    respaldo = marca.read_text(encoding="utf-8") if marca.exists() else None
    if respaldo is not None:
        marca.unlink()
        print("ℹ️  Se quitó el marcador para simular la PRIMERA vez")
    try:
        return _comprobar(args, marca)
    finally:
        # ✅ 2026-10-10: el marcador se repone SIEMPRE, incluso si una comprobación falla a mitad de
        # camino. Antes, un fallo en la comprobación 1 salía ANTES de reponerlo y al usuario le volvía a
        # aparecer la guía de primer arranque como si fuera la primera vez (medido: me pasó al reintentar).
        if respaldo is not None:
            marca.write_text(respaldo, encoding="utf-8")


def _comprobar(args, marca) -> int:
    """Las tres comprobaciones. Devuelve 0 si todo se comporta bien y 4 si algo falla."""

    print("\n▶ 1) Primera vez: la guía tiene que abrirse SOLA")
    abierta, texto, errores = _abrir(args.url, "primer_arranque.png")
    if not abierta:
        print(f"   ❌ no se abrió la pantalla: {errores[0] if errores else 'motivo desconocido'}")
        print("      (¿está AXIOMA encendido? `instalar/iniciar_axioma.sh`)")
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

    print("\n▶ 3) La sección de modelos ofrece lo instalado y muestra el modelo elegido")
    marca.unlink(missing_ok=True)          # que la pantalla se abra sola, como la primera vez
    campos, errores3, datos, cierre = _revisar_modelos(args.url, "modelos_en_configurar.png")
    if campos is None:
        print(f"   ❌ {errores3[0]} (captura: data/capturas/modelos_en_configurar.png)")
        return 4
    # La lista de roles se IMPORTA del componente: si mañana se agrega uno, esta prueba lo pide sola.
    from src.interfaces.components.configurar_axioma import _ROLES_DE_MODELO

    configurados, instalados = datos.get("configurados") or {}, set(datos.get("instalados") or [])
    problemas, vistos = [], []
    for rol, etiqueta in _ROLES_DE_MODELO:
        campo = next((c for c in campos if etiqueta in c["label"]), None)
        if campo is None:
            problemas.append(f"{etiqueta}: no está en pantalla")
            continue
        esperado = (configurados.get(rol) or "").strip()
        if esperado and campo["valor"].strip() != esperado:
            problemas.append(f"{etiqueta}: la pantalla muestra «{campo['valor']}» y AXIOMA usa «{esperado}»")
        elif esperado and esperado not in instalados:
            problemas.append(f"{etiqueta}: «{esperado}» está configurado pero no instalado en Ollama")
        else:
            vistos.append(f"{etiqueta} → {campo['valor'].strip() or '(vacío)'}")
    if problemas:
        for p_ in problemas:
            print(f"   ❌ {p_}")
        print("   (captura: data/capturas/modelos_en_configurar.png)")
        return 4
    # ✅ Dónde se dibujan: tienen que estar DENTRO de la pantalla de configuración.
    dibujados_fuera = [c for c in campos if c.get("visible") and not c.get("dentroDelDialogo")]
    if dibujados_fuera:
        print(f"   ❌ {len(dibujados_fuera)} campo(s) de modelo se dibujan FUERA de la pantalla de "
              "configuración (se quedan a la vista al cerrarla y cada apertura agrega otra copia)")
        print("   (captura: data/capturas/modelos_en_configurar.png)")
        return 4
    print("   ✅ los campos están DENTRO de la pantalla (se van con ella)")
    # ✅ Y que se CIERRE: es lo que reportó el usuario (quedaba tapando todo).
    if cierre.get("cerro") and cierre.get("campos_a_la_vista") == 0:
        print(f"   ✅ al cerrar se va y no queda nada a la vista (centro de la pantalla: {cierre['centro']})")
    else:
        print(f"   ❌ al cerrar quedó algo tapando: diálogo visible={not cierre.get('cerro')} · "
              f"campos a la vista={cierre.get('campos_a_la_vista')} · {cierre.get('motivo', '')}")
        return 4
    print(f"   ✅ los {len(_ROLES_DE_MODELO)} roles están en pantalla, con el modelo que AXIOMA usa:")
    for v in vistos:
        print(f"      · {v}")
    if datos.get("ollama_disponible"):
        print(f"   ✅ Ollama responde: ofrece {len(instalados)} modelo(s) instalado(s)")
    else:
        print("   ⚠️  Ollama no responde: la sección avisa en pantalla, pero no se pudo comprobar lo ofrecido")
    if errores3:
        print(f"   ⚠️  errores de JavaScript: {errores3[:2]}")

    print("\n✅ La guía se comporta como corresponde (capturas en data/capturas/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
