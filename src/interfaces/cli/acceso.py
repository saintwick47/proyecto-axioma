#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — avisos y rotación del código de acceso de la web
# ═══════════════════════════════════════════════════════════════
# Ruta: src/interfaces/cli/acceso.py
#
# Pedido del usuario (Parte C): `python main.py --check` (y el arranque) tienen
# que DETECTAR el caso peligroso «expuesto a la red sin código de acceso» y
# explicar qué hacer, en lenguaje llano. Con el token automático (Parte A) casi
# no debería ocurrir, pero queda como red de seguridad para quien forzó
# `AXIOMA_WEB_HOST=0.0.0.0`.
#
# Acá vive también la rotación simple (`python main.py --rotar-token`), para no
# engordar `main.py` (el auditor marca los archivos de más de 1000 líneas).
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def avisar_acceso_expuesto(host: Optional[str] = None) -> None:
    """Avisa —en lenguaje llano— si AXIOMA quedó abierto a la red sin código.

    No genera nada: sólo informa. Si hay código, dice dónde está; si no, explica
    que se genera solo al arrancar y cómo generarlo ya.
    """
    from config.acceso_web import host_expuesto, token_configurado
    try:
        expuesto = host_expuesto(host) or host_expuesto()
    except Exception as e:  # noqa: BLE001 — un chequeo no puede romper --check
        logger.warning(f"[acceso] no pude determinar si está expuesto: {e}")
        return
    if not expuesto:
        return
    token = token_configurado()
    if token:
        print("ℹ️  AXIOMA está configurado para abrirse a otros equipos.")
        print("   El código de acceso está guardado en data/.web_token.")
        print("   Para volver a sólo esta computadora: AXIOMA_WEB_HOST=127.0.0.1 en el .env.")
        return
    print("⚠️  AXIOMA está configurado para abrirse a otros equipos y TODAVÍA no tiene código de acceso.")
    print("   El código se genera solo al arrancar AXIOMA; para generarlo ahora:")
    print("       python main.py --rotar-token")
    print("   Si preferís que nadie más entre, poné en el archivo .env:")
    print("       AXIOMA_WEB_HOST=127.0.0.1")
    logger.warning("[acceso] AXIOMA expuesto sin token configurado")


def cmd_rotar_token() -> int:
    """Genera un código de acceso NUEVO para la API web y lo guarda (0600).

    No hace falta reiniciar AXIOMA (las rutas leen el archivo en cada pedido) y
    el valor anterior deja de servir.
    """
    from config.acceso_web import (
        VARIABLE_ENTORNO, ruta_token_web, rotar_token_web, token_configurado)
    try:
        nuevo = rotar_token_web()
    except OSError as e:
        print(f"❌ [TOKEN] No pude guardar el código nuevo: {e}")
        print("   Revisá los permisos de la carpeta data/ o fijá AXIOMA_WEB_TOKEN en el .env.")
        return 1
    print(f"✅ [TOKEN] Código de acceso nuevo: {nuevo}")
    print(f"   Guardado en: {ruta_token_web()} (sólo tu usuario puede leerlo)")
    print("   Si AXIOMA ya estaba abierto, el código nuevo ya vale: no hace falta reiniciarlo.")
    try:
        if token_configurado() != nuevo:
            print(f"⚠️  Tenés {VARIABLE_ENTORNO} fijado en el entorno o en el .env: ese valor")
            print("   manda sobre el archivo. Borralo para que valga el código nuevo.")
    except Exception as e:  # noqa: BLE001 — informar el aviso no puede fallar
        logger.warning(f"[token] no pude comprobar si hay un token fijado: {e}")
    return 0
