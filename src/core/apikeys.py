#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Carga guiada de las claves de API del usuario
# ═══════════════════════════════════════════════════════════════
# ✅ Fase 1 del plan de contenedores. Pedido del usuario (2026-10-04):
#
#   *"las API keys son personales: cada persona que descargue el proyecto contenido en una imagen
#   (axioma) debe proporcionar sus propias API keys y no usar las mías. Agregar una función donde el
#   usuario pueda agregar sus API keys de forma fácil. El archivo `.env` debe configurarse de forma
#   automática con la información proporcionada por el usuario."*
#
# Cómo se usa:
#     python -m src.core.apikeys            # pregunta una por una (se puede saltear con Enter)
#     python -m src.core.apikeys --listar   # qué claves hay y cuáles faltan (NO muestra valores)
#     python -m src.core.apikeys --json     # para la interfaz
#
# Reglas que cumple:
#   · **Nunca imprime los valores** (ni al guardar ni al listar): sólo dice si están o faltan.
#   · Guarda el `.env` con permisos **sólo para el usuario** (0600) y **hace copia de seguridad**.
#   · **No reescribe** el archivo: cambia únicamente las líneas de las claves que tocás (los
#     comentarios y el resto de la configuración quedan intactos).
#   · **R1 (nada de configuración sin lector)**: sólo se ofrecen claves que el proyecto usa de
#     verdad. `BRAVE_API_KEY` y `SECRET_KEY` están en la plantilla pero **no tienen lector** hoy,
#     así que no se piden (ver `docs/PLAN_CONTENEDORES.md`).
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parents[2]
ENV_POR_DEFECTO = RAIZ / ".env"
PLANTILLA = RAIZ / ".env.example"


@dataclass(frozen=True)
class Clave:
    """Una clave que el proyecto LEE de verdad, con qué se pierde si falta."""

    variable: str
    campo: str
    titulo: str
    para: str
    sin_ella: str
    como: str
    companera: str = ""      # otra clave que va de la mano (Google necesita las dos)


# Las 6 claves que el proyecto lee hoy (verificado por sus campos en `config/settings.py` y sus
# lectores en `src/agents/searcher.py`, `src/handlers/data_fetchers.py` y
# `src/integrations/external_apis_helpers.py`).
CLAVES: Tuple[Clave, ...] = (
    Clave(
        variable="TAVILY_API_KEY", campo="tavily_api_key", titulo="Tavily (búsqueda web)",
        para="buscar en la web y traer datos actuales (también las consultas de finanzas)",
        sin_ella=("las búsquedas web quedan sin proveedor y las consultas de finanzas/dólar caen a "
                  "la fuente gratuita o fallan"),
        como="creá una cuenta en https://tavily.com y copiá la clave del panel",
    ),
    Clave(
        variable="SERPER_API_KEY", campo="serper_api_key", titulo="Serper (búsqueda web)",
        para="segundo proveedor de búsqueda (Google vía serper.dev)",
        sin_ella="queda sin este proveedor; si tenés otro, la búsqueda sigue funcionando",
        como="https://serper.dev → panel → API key",
    ),
    Clave(
        variable="SERPAPI_API_KEY", campo="serpapi_api_key", titulo="SerpAPI (búsqueda web)",
        para="tercer proveedor de búsqueda (serpapi.com)",
        sin_ella="queda sin este proveedor (ojo: esta clave viaja en la dirección del pedido, por eso "
                 "el proyecto la tapa en los registros)",
        como="https://serpapi.com/manage-api-key",
    ),
    Clave(
        variable="NEWS_API_KEY", campo="news_api_key", titulo="NewsAPI (noticias)",
        para="consultas de noticias",
        sin_ella="las consultas de noticias fallan con 'NEWS_API_KEY no configurado'",
        como="https://newsapi.org → registro → API key",
    ),
    Clave(
        variable="GOOGLE_API_KEY", campo="google_api_key",
        titulo="Google (búsqueda personalizada)",
        para="búsqueda con Google Custom Search (necesita también el identificador del motor)",
        sin_ella="queda sin este proveedor",
        como="https://console.cloud.google.com → habilitar 'Custom Search API' → credenciales",
        companera="SEARCH_ENGINE_ID",
    ),
    Clave(
        variable="SEARCH_ENGINE_ID", campo="search_engine_id",
        titulo="Google (identificador del motor)",
        para="el 'cx' del motor de búsqueda personalizado que acompaña a GOOGLE_API_KEY",
        sin_ella="Google no funciona aunque tengas la clave (van de la mano)",
        como="https://programmablesearchengine.google.com → crear motor → copiar el 'cx'",
        companera="GOOGLE_API_KEY",
    ),
)

_LINEA = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _leer_lineas(ruta: Path) -> List[str]:
    if not ruta.exists():
        return []
    return ruta.read_text(encoding="utf-8").splitlines()


def valores_configurados(ruta: Optional[Path] = None) -> Dict[str, bool]:
    """¿Qué claves conocidas están puestas? Devuelve nombre → True/False. **No** devuelve valores."""
    archivo = Path(ruta) if ruta else ENV_POR_DEFECTO
    puesto: Dict[str, bool] = {c.variable: False for c in CLAVES}
    for linea in _leer_lineas(archivo):
        m = _LINEA.match(linea)
        if not m or m.group(1) not in puesto:
            continue
        valor = linea.split("=", 1)[1].strip().strip('"').strip("'")
        puesto[m.group(1)] = bool(valor)
    return puesto


def estado(ruta: Optional[Path] = None) -> List[Dict[str, object]]:
    """Estado de cada clave para mostrar (sin valores): configurada, para qué sirve, qué se pierde."""
    archivo = Path(ruta) if ruta else ENV_POR_DEFECTO
    puesto = valores_configurados(archivo)
    return [
        {
            "variable": c.variable,
            "titulo": c.titulo,
            "configurada": puesto.get(c.variable, False),
            "para": c.para,
            "sin_ella": c.sin_ella,
            "como": c.como,
            "companera": c.companera,
        }
        for c in CLAVES
    ]


def guardar_valores(cambios: Dict[str, str], ruta: Optional[Path] = None,
                    copia: bool = True) -> Path:
    """Guarda las claves indicadas en el `.env`, **sin tocar el resto del archivo**.

    · Si el `.env` no existe, se parte de la plantilla `.env.example` (mismo criterio que el CI).
    · Se hace copia de seguridad (`<archivo>.bak`) antes de escribir.
    · Sólo se reemplazan las líneas de las claves que vienen en `cambios`; lo demás queda igual.
    · El archivo queda con permisos **sólo para el usuario** (0600).
    """
    archivo = Path(ruta) if ruta else ENV_POR_DEFECTO
    lineas = _leer_lineas(archivo)
    if not lineas and PLANTILLA.exists():
        lineas = _leer_lineas(PLANTILLA)

    if archivo.exists() and copia:
        respaldo = archivo.with_name(archivo.name + ".bak")
        respaldo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        try:
            os.chmod(respaldo, 0o600)
        except OSError as e:
            # En sistemas sin permisos POSIX no hay nada que ajustar, pero se registra (R3).
            log_degraded(logger, e, "apikeys.guardar_valores (permisos de la copia)")

    pendientes = dict(cambios)
    salida: List[str] = []
    for linea in lineas:
        m = _LINEA.match(linea)
        if m and m.group(1) in pendientes:
            salida.append(f"{m.group(1)}={pendientes.pop(m.group(1))}")
        else:
            salida.append(linea)

    if pendientes:
        if salida and salida[-1].strip():
            salida.append("")
        salida.append("# ── Claves cargadas por el usuario (python -m src.core.apikeys) ──")
        for nombre, valor in pendientes.items():
            salida.append(f"{nombre}={valor}")

    archivo.parent.mkdir(parents=True, exist_ok=True)
    archivo.write_text("\n".join(salida) + "\n", encoding="utf-8")
    try:
        os.chmod(archivo, 0o600)
    except OSError as e:
        log_degraded(logger, e, "apikeys.guardar_valores (permisos del .env)")
    return archivo


def configurar(ruta: Optional[Path] = None, preguntar: Callable[[str], str] = input,
               pedir_oculto: Callable[[str], str] = getpass.getpass,
               avisar: Callable[[str], None] = print) -> Dict[str, str]:
    """Pregunta por cada clave una por una y guarda las que el usuario cargue.

    Se puede **saltear** con Enter (no se toca lo que ya estaba) y **borrar** escribiendo `-`.
    La entrada va oculta (no queda en la pantalla ni en el historial).
    """
    archivo = Path(ruta) if ruta else ENV_POR_DEFECTO
    puesto = valores_configurados(archivo)
    cambios: Dict[str, str] = {}
    avisar("")
    avisar("AXIOMA — tus claves de API (son TUYAS: no viajan en la imagen del proyecto)")
    avisar("=" * 66)
    avisar("Enter = dejar como está · '-' = borrarla · el valor no se muestra en pantalla")
    for c in CLAVES:
        ya = "ya configurada" if puesto.get(c.variable) else "falta"
        avisar("")
        avisar(f"▸ {c.titulo}  [{c.variable} · {ya}]")
        avisar(f"   sirve para: {c.para}")
        avisar(f"   si falta:   {c.sin_ella}")
        avisar(f"   dónde:      {c.como}")
        try:
            valor = pedir_oculto(f"   pegá {c.variable} (Enter = saltear): ")
        except (EOFError, KeyboardInterrupt):
            avisar("   (cancelado: no se cambió nada)")
            break
        if not valor:
            continue
        if valor.strip() == "-":
            cambios[c.variable] = ""
            avisar("   → se borra")
        elif " " in valor.strip():
            avisar("   ⚠️  tiene espacios: revisá que hayas copiado sólo la clave (no se guardó)")
        else:
            cambios[c.variable] = valor.strip()
            avisar("   → se guarda (oculta)")

    # Las claves que van de la mano se avisan si queda una sola puesta.
    for c in CLAVES:
        if not c.companera:
            continue
        tiene = bool(cambios.get(c.variable, "")) or puesto.get(c.variable, False)
        otra = bool(cambios.get(c.companera, "")) or puesto.get(c.companera, False)
        if tiene != otra:
            avisar(f"   ⚠️  {c.variable} y {c.companera} van juntas: falta una de las dos")

    if not cambios:
        avisar("")
        avisar("No se cambió ninguna clave.")
        return {}
    destino = guardar_valores(cambios, archivo)
    avisar("")
    avisar(f"✅ Guardado en {destino} (permisos sólo para tu usuario; copia previa en "
           f"{destino.name}.bak)")
    avisar("   Las claves no se muestran nunca: para ver el estado, `--listar`.")
    avisar("   Si AXIOMA está en un contenedor, reinicialo para que tome el archivo nuevo.")
    return cambios


def _listar(ruta: Optional[Path], avisar: Callable[[str], None] = print) -> int:
    datos = estado(ruta)
    faltan = [d["variable"] for d in datos if not d["configurada"]]
    avisar("")
    avisar("AXIOMA — claves de API (los valores NO se muestran nunca)")
    avisar("=" * 66)
    for d in datos:
        marca = "✅ configurada" if d["configurada"] else "—  falta"
        avisar(f"{marca}  {d['variable']:20s} {d['titulo']}")
    avisar("")
    if faltan:
        avisar(f"Faltan {len(faltan)}: {', '.join(faltan)}")
        avisar("Cargalas con:  python -m src.core.apikeys")
    else:
        avisar("Están todas las que el proyecto usa.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Línea de órdenes: `--listar` (estado), `--json` (para la interfaz) o carga guiada."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--listar" in args:
        return _listar(None)
    if "--json" in args:
        print(json.dumps({"claves": estado(None)}, ensure_ascii=False, indent=2))
        return 0
    configurar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
