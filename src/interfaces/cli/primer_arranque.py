#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — aviso de primer arranque: ¿cómo vas a usar AXIOMA?
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/cli/primer_arranque.py
#
# Pedido del usuario: AXIOMA está pensado para gente que no sabe programar,
# así que la primera vez que abre la consola tiene que explicarle —en sus
# palabras— que hay tres formas de usarlo, sin hablar de «bind», «loopback» ni
# «header». Se muestra UNA sola vez (marcador en `data/`), también en modo no
# interactivo (ahí elige la opción 1 y sigue).
#
# La opción 1 (sólo esta computadora) es la recomendada y no necesita configurar
# nada: el token de la web ni se genera en ese modo. Si algún día el usuario
# quiere abrirlo a la red, el arranque de la web le genera el código solo.
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import IO, Optional

from config.paths import Paths

logger = logging.getLogger(__name__)

NOMBRE_MARCADOR = ".primer_arranque"

# Texto base pedido (adaptado a lenguaje llano; sin jerga técnica).
TEXTO_MODOS = """
  ¿Cómo vas a usar AXIOMA?

  1) Sólo en esta computadora  (recomendado)
     Nadie más puede entrar. No hay que configurar nada.

  2) Desde otros equipos de mi casa/oficina
     AXIOMA pedirá un código de acceso que se genera solo.

  3) Desde internet
     No recomendado sin ayuda técnica: además del código,
     conviene un candado extra (HTTPS).

  [1]  [2]  [3]        opción por defecto: 1
"""

TEXTO_OPCION_1 = "  ✅ Listo: AXIOMA queda sólo en esta computadora (lo recomendado)."
TEXTO_OPCION_2 = (
    "  ✅ Para usarlo desde otros equipos:\n"
    "     · En una consola:  python main.py --web --host 0.0.0.0\n"
    "       AXIOMA te va a mostrar el código de acceso al arrancar.\n"
    "     · Con el instalador/Docker: poné AXIOMA_WEB_HOST=0.0.0.0 en el archivo .env\n"
    "       y volvé a abrir AXIOMA. El código se genera y se guarda solo."
)
TEXTO_OPCION_3 = (
    "  ⚠️  Desde internet NO se recomienda sin ayuda técnica.\n"
    "     Si igual querés hacerlo, es como la opción 2 (AXIOMA_WEB_HOST=0.0.0.0) y\n"
    "     además hace falta un candado HTTPS: pedile a alguien que te ayude a ponerlo."
)
TEXTO_SIN_PERSONA = (
    "  ℹ️  No hay una persona para elegir ahora mismo: sigo con la opción 1\n"
    "     (sólo esta computadora), que es la recomendada y no configura nada."
)


def marcador(ruta: Optional[object] = None) -> Path:
    """Ruta del marcador que evita repetir el aviso (por defecto `data/`)."""
    return Path(ruta) if ruta is not None else Paths.DATA / NOMBRE_MARCADOR


def ya_se_mostro(ruta: Optional[object] = None) -> bool:
    """¿El aviso ya se mostró en este equipo?"""
    try:
        return marcador(ruta).exists()
    except OSError as e:
        # Si no se puede preguntar, no molestar de nuevo (degradación intencional).
        logger.debug(f"[primer arranque] no pude comprobar {marcador(ruta)}: {e}")
        return True


def marcar_mostrado(ruta: Optional[object] = None) -> None:
    """Deja constancia de que el aviso ya se mostró (tolerante a fallos)."""
    try:
        archivo = marcador(ruta)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        archivo.write_text("El aviso de «cómo vas a usar AXIOMA» ya se mostró.\n",
                           encoding="utf-8")
    except OSError as e:
        # Sin permiso de escritura no se insiste: el peor caso es verlo otra vez.
        logger.debug(f"[primer arranque] no pude marcar {marcador(ruta)}: {e}")


def _imprimir(texto: str, salida: Optional[IO[str]]) -> None:
    print(texto, end="" if texto.endswith("\n") else "\n", file=salida or None)


def _elegir_opcion(entrada: IO[str], salida: Optional[IO[str]]) -> int:
    """Pide la opción hasta que sea 1, 2, 3 o vacío (Enter = 1)."""
    while True:
        print("  Elegí una opción [1]: ", end="", file=salida or None, flush=True)
        try:
            linea = entrada.readline()
        except (EOFError, KeyboardInterrupt):
            return 1
        if not linea:                     # fin de entrada: default
            return 1
        texto = linea.strip()
        if not texto:
            return 1
        if texto in ("1", "2", "3"):
            return int(texto)
        print("  Escribí 1, 2 o 3 (o Enter para la 1).", file=salida or None)


def _mensaje_de_opcion(opcion: int) -> str:
    return {1: TEXTO_OPCION_1, 2: TEXTO_OPCION_2, 3: TEXTO_OPCION_3}.get(opcion, TEXTO_OPCION_1)


def mostrar_aviso_de_modo_de_acceso(
    ruta_marcador: Optional[object] = None,
    interactivo: Optional[bool] = None,
    entrada: Optional[IO[str]] = None,
    salida: Optional[IO[str]] = None,
) -> int:
    """Muestra el aviso UNA vez y devuelve la opción elegida (1, 2 o 3; 0 si ya se mostró).

    `interactivo=None` lo deduce de la terminal (una consola de doble clic o una
    tubería no tienen persona). Si no hay terminal, muestra el aviso y sigue con
    la opción 1, sin bloquearse esperando una respuesta que nunca va a llegar.
    """
    if ya_se_mostro(ruta_marcador):
        return 0

    _imprimir(TEXTO_MODOS, salida)

    if interactivo is None:
        try:
            interactivo = bool(sys_stdin_es_terminal() and sys_stdout_es_terminal())
        except Exception as e:  # noqa: BLE001 — nunca romper el arranque por esto
            logger.debug(f"[primer arranque] no pude saber si hay terminal: {e}")
            interactivo = False

    if not interactivo:
        _imprimir(TEXTO_SIN_PERSONA, salida)
        opcion = 1
    else:
        opcion = _elegir_opcion(entrada or sys.stdin, salida)

    _imprimir(_mensaje_de_opcion(opcion), salida)
    marcar_mostrado(ruta_marcador)
    return opcion


# ═══════════════════════════════════════════════════════════════
# TERMINAL — aislado para poder simularlo en las pruebas
# ═══════════════════════════════════════════════════════════════
def sys_stdin_es_terminal() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def sys_stdout_es_terminal() -> bool:
    return bool(sys.stdout and sys.stdout.isatty())


__all__ = [
    "NOMBRE_MARCADOR", "TEXTO_MODOS", "marcador", "ya_se_mostro",
    "marcar_mostrado", "mostrar_aviso_de_modo_de_acceso",
]
