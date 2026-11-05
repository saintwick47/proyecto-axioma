#!/usr/bin/env python3
"""Selección del modelo de cada ROL, escrita en el `.env`.

Pedido del usuario (2026-11-05): *"el único modelo que debería instalarse sin aviso es el
embeddings; después el usuario elige el de chat, y si no le convence puede eliminarlo y bajar
otro"*. Para eso hace falta que la elección se guarde en un lugar que AXIOMA lea al arrancar:
las variables `LLM_DEFAULT_MODEL` (chat), `LLM_CODE_MODEL` (código) y `LLM_VISION_MODEL`
(visión) del `.env`.

Reglas que cumple:
  · Escribe **sólo** la línea de la variable elegida; el resto del `.env` queda igual.
  · Hace copia de seguridad (`.env.bak`) y deja el archivo con permisos 0600.
  · Rechaza valores con saltos de línea (misma defensa que `apikeys.guardar_valores`).
  · No importa `config.settings`: lee el archivo, así sirve aunque la app ya esté corriendo.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
ENV_POR_DEFECTO = RAIZ / ".env"

# Rol del catálogo → variable del `.env`. El rol `embeddings` NO se elige (es obligatorio).
ROLES: dict[str, str] = {
    "chat": "LLM_DEFAULT_MODEL",
    "codigo": "LLM_CODE_MODEL",
    "vision": "LLM_VISION_MODEL",
}

_ETIQUETAS = {"chat": "chat", "codigo": "código", "vision": "visión"}


def etiqueta(rol: str) -> str:
    """Nombre para mostrar del rol (`codigo` → `código`)."""
    return _ETIQUETAS.get(rol, rol)


def _archivo(ruta: Path | None) -> Path:
    return Path(ruta) if ruta else ENV_POR_DEFECTO


def _leer(archivo: Path) -> dict[str, str]:
    valores: dict[str, str] = {}
    if not archivo.exists():
        return valores
    for linea in archivo.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", linea)
        if m:
            valores[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return valores


def actuales(ruta: Path | None = None) -> dict[str, str]:
    """Modelo configurado hoy para cada rol, leído del `.env` ("" si no está)."""
    valores = _leer(_archivo(ruta))
    return {rol: valores.get(variable, "") for rol, variable in ROLES.items()}


def en_uso(modelo: str, ruta: Path | None = None) -> list:
    """Roles en los que está configurado ese modelo (vacío si en ninguno)."""
    return [rol for rol, actual in actuales(ruta).items() if actual and actual == modelo]


def asignar(rol: str, modelo: str, ruta: Path | None = None) -> Path:
    """Guarda `modelo` como el modelo de ese rol en el `.env` y devuelve la ruta escrita.

    Raises:
        ValueError: rol desconocido o nombre de modelo inválido (vacío o con saltos de línea).
    """
    if rol not in ROLES:
        raise ValueError(f"rol desconocido: {rol!r} (válidos: {', '.join(ROLES)})")
    nombre = (modelo or "").strip()
    if not nombre:
        raise ValueError("el modelo no puede estar vacío")
    if any(c in nombre for c in ("\n", "\r", "\0")):
        raise ValueError("el modelo no puede tener saltos de línea ni caracteres nulos")

    archivo = _archivo(ruta)
    lineas = archivo.read_text(encoding="utf-8", errors="replace").splitlines() if archivo.exists() else []
    if archivo.exists():
        respaldo = archivo.with_name(archivo.name + ".bak")
        respaldo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(respaldo, 0o600)

    variable = ROLES[rol]
    patron = re.compile(rf"^\s*{variable}\s*=")
    reemplazada = False
    for i, linea in enumerate(lineas):
        if patron.match(linea):
            lineas[i] = f"{variable}={nombre}"
            reemplazada = True
            break
    if not reemplazada:
        lineas.append(f"{variable}={nombre}")

    archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(archivo, 0o600)
    return archivo
