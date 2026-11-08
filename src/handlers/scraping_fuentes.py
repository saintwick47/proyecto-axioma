"""Fuentes de scraping para datos que no tienen API (2026-11-08).

Hay datos que las APIs gratuitas no cubren (p. ej. el riesgo país de países distintos de
Argentina). En vez de meter un framework pesado como Scrapy —que arranca un reactor y suma
latencia y dependencias— acá va una capa **liviana**:

  · `httpx` (ya es dependencia) para el GET, con timeout corto (5 s por defecto);
  · `lxml`/regex para extraer el número: `lxml` ya está instalado;
  · **caché en memoria** (15 min) para que no pegue en cada consulta;
  · las fuentes se declaran en `config/fuentes_scraping.yaml` (sin tocar código);
  · si una fuente no responde o el patrón no matchea, se devuelve `None` y el llamador sigue
    con el aviso honesto (nunca se inventa un número).

Formato de una fuente en el YAML:

    fuentes:
      - nombre: "Brasil — ejemplo"
        pais: "brasil"
        url: "https://sitio.example/riesgo-pais/brasil"
        patron: '(?P<valor>[0-9]{2,5})\\s*(?:puntos|pts|bps)'
        nota: "opcional: de dónde sale el número"
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parents[2]
RUTA_POR_DEFECTO = RAIZ / "config" / "fuentes_scraping.yaml"
TTL_CACHE = 900.0          # 15 min: el riesgo país no cambia al segundo
TIMEOUT_POR_DEFECTO = 5.0  # si la fuente tarda más, se corta: no se suma latencia al chat
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class Fuente:
    nombre: str
    pais: str
    url: str
    patron: str
    nota: str = ""


def cargar_fuentes(ruta: Any = None) -> list[Fuente]:
    """Lee las fuentes del YAML. Si falta o está mal, devuelve [] (nunca rompe el arranque)."""
    archivo = Path(ruta) if ruta else RUTA_POR_DEFECTO
    if not archivo.exists():
        return []
    try:
        import yaml
        datos = yaml.safe_load(archivo.read_text(encoding="utf-8")) or {}
        crudas = datos.get("fuentes") or []
        fuentes = []
        for f in crudas:
            try:
                fuentes.append(Fuente(
                    nombre=str(f["nombre"]), pais=str(f["pais"]).lower(),
                    url=str(f["url"]), patron=str(f["patron"]),
                    nota=str(f.get("nota", "")),
                ))
            except (KeyError, TypeError) as e:
                logger.warning(f"[scraping] fuente inválida en {archivo}: {e}")
        return fuentes
    except Exception as e:
        logger.warning(f"[scraping] no pude leer {archivo}: {e}")
        return []


def _extraer(html: str, patron: str) -> str | None:
    m = re.search(patron, html, re.S)
    if not m:
        return None
    valor = m.groupdict().get("valor") or (m.group(1) if m.groups() else None)
    return str(valor).strip() if valor else None


def buscar(pais: str, timeout: float = TIMEOUT_POR_DEFECTO,
           ruta: Any = None) -> dict[str, Any] | None:
    """Busca el dato de `pais` en las fuentes declaradas; devuelve el primero que matchea.

    Returns:
        `{"pais", "valor", "fuente", "url", "nota"}` o `None` si ninguna fuente lo trae.
    """
    pais = (pais or "").strip().lower()
    if not pais:
        return None
    ahora = time.time()
    cacheado = _CACHE.get(pais)
    if cacheado and ahora - cacheado[0] < TTL_CACHE:
        return cacheado[1]

    for fuente in cargar_fuentes(ruta):
        if fuente.pais != pais and fuente.pais != "*":
            continue
        try:
            respuesta = httpx.get(
                fuente.url, timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AXIOMA/1.0)"},
                follow_redirects=True,
            )
            respuesta.raise_for_status()
            valor = _extraer(respuesta.text, fuente.patron)
        except Exception as e:
            logger.warning(f"[scraping] {fuente.nombre} falló: {e}")
            continue
        if not valor:
            logger.debug(f"[scraping] {fuente.nombre}: el patrón no matcheó")
            continue
        resultado = {"pais": pais, "valor": valor, "fuente": fuente.nombre,
                     "url": fuente.url, "nota": fuente.nota}
        _CACHE[pais] = (ahora, resultado)
        logger.info(f"[scraping] {pais}: {valor} ({fuente.nombre})")
        return resultado
    return None


def limpiar_cache() -> None:
    """Vacía la caché (para pruebas o para forzar un dato nuevo)."""
    _CACHE.clear()
