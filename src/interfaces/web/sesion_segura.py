#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — secreto de sesión de la web
# ═══════════════════════════════════════════════════════════════
# Trazabilidad:
#   Antes: `storage_secret='axioma_secret_key_change_in_production'` escrito a mano en DOS archivos
#   (`src/interfaces/web/app.py` y `src/interfaces/web/server.py`). Cualquiera que leyera el código podía
#   firmar una cookie de sesión válida. Medido el 2026-10-07: además la interfaz escuchaba en
#   `0.0.0.0:8080` sin autenticación, así que el problema no era teórico.
#
#   Ahora: un secreto por equipo, generado la primera vez y guardado SÓLO para tu usuario.
#     1. Si está `AXIOMA_STORAGE_SECRET` en el entorno, manda esa (para quien quiera gestionarla).
#     2. Si no, se lee `data/.storage_secret` (permisos 0600: sólo tu usuario).
#     3. Si no existe (primer arranque), se genera con `secrets.token_hex(32)` y se guarda.
#
#   Por qué un archivo y no una variable: tiene que **sobrevivir al reinicio** (si cambia, se invalidan
#   todas las sesiones) y no se puede regenerar en cada arranque en varios procesos a la vez.
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NOMBRE_ARCHIVO = ".storage_secret"
VARIABLE_ENTORNO = "AXIOMA_STORAGE_SECRET"
LARGO_MINIMO = 32          # menos que esto no es un secreto serio


def _carpeta_de_datos() -> Path:
    """La carpeta de datos del proyecto (`data/`), preguntándole al proyecto y no a una ruta fija."""
    try:
        from config.settings import settings
        return Path(settings.data_dir)
    except Exception as e:  # noqa: BLE001 — si la configuración falla, se usa la carpeta del código
        logger.debug(f"[secreto] no pude leer data_dir de la configuración: {e}")
        return Path(__file__).resolve().parents[3] / "data"


def _guardar(archivo: Path, valor: str) -> None:
    """Escribe el secreto con permisos sólo para el usuario (0600)."""
    archivo.parent.mkdir(parents=True, exist_ok=True)
    archivo.write_text(valor, encoding="utf-8")
    try:
        os.chmod(archivo, 0o600)
    except OSError as e:                        # Windows sin permisos POSIX, sistemas de archivos raros
        logger.warning(f"[secreto] no pude ajustar los permisos de {archivo}: {e}")


def secreto_de_sesion(ruta: Optional[Path] = None, entorno: Optional[dict] = None) -> str:
    """Devuelve el secreto con el que se firman las sesiones de la web.

    Es determinista entre arranques (si no, cada reinicio echaría a todos) y distinto en cada equipo
    (si no, una cookie forjada en una máquina serviría en otra).
    """
    variables = os.environ if entorno is None else entorno
    de_entorno = (variables.get(VARIABLE_ENTORNO) or "").strip()
    if len(de_entorno) >= LARGO_MINIMO:
        return de_entorno
    if de_entorno:
        logger.warning(f"[secreto] {VARIABLE_ENTORNO} es demasiado corta ({len(de_entorno)} caracteres): "
                       "se ignora y se usa el archivo del equipo")

    archivo = Path(ruta) if ruta is not None else _carpeta_de_datos() / NOMBRE_ARCHIVO
    try:
        if archivo.exists():
            existente = archivo.read_text(encoding="utf-8").strip()
            if len(existente) >= LARGO_MINIMO:
                return existente
            logger.warning(f"[secreto] {archivo} está incompleto: se genera uno nuevo "
                           "(las sesiones abiertas se van a cerrar)")
    except OSError as e:
        logger.error(f"[secreto] no pude leer {archivo} ({e}): se genera uno nuevo para esta corrida")

    nuevo = secrets.token_hex(32)
    try:
        _guardar(archivo, nuevo)
        logger.info(f"[secreto] secreto de sesión generado en {archivo} (sólo tu usuario puede leerlo)")
    except OSError as e:
        # Sin poder guardar, el secreto vive sólo en esta corrida: la web funciona, pero las sesiones
        # no sobreviven al reinicio. Se avisa (nunca en silencio).
        logger.error(f"[secreto] no pude guardar {archivo} ({e}): las sesiones no van a sobrevivir "
                     "al reinicio")
    return nuevo
