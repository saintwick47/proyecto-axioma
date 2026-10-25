#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — token de acceso de la web (automático, por equipo)
# ═══════════════════════════════════════════════════════════════
# Ruta: config/acceso_web.py
#
# Por qué existe
#   Las rutas `POST /api/v1/preflight/claves` y `POST /api/v1/preflight/modelos`
#   ESCRIBEN el `.env`. Hasta ahora el token que las protege había que generarlo
#   y pegarlo a mano en `AXIOMA_WEB_TOKEN`: un usuario inexperto no tiene forma de
#   saber que eso existe. Ahora se resuelve solo, en este orden:
#
#     1. `AXIOMA_WEB_TOKEN` del entorno o del `.env` (opción AVANZADA para fijarlo).
#     2. `data/.web_token` (permisos 0600: sólo tu usuario puede leerlo).
#     3. Si no existe, se genera con `secrets.token_urlsafe(32)` y se guarda.
#
#   Mismo patrón que `src/interfaces/web/sesion_segura.py` (secreto de sesión):
#   archivo por equipo, 0600, generación perezosa y tolerante a fallos. **No** se
#   lee ni se genera al importar `config.settings`: la lectura es barata, pero un
#   import no debe crear archivos (la suite lo importa todo el tiempo y la regla
#   es no escribir fuera de `tmp_path`). Se resuelve cuando de verdad hace falta
#   (arranque de la web expuesta, rotación, chequeo, banner de la UI).
#
#   Si el archivo no se puede escribir, `token_de_web()` devuelve "" y el sistema
#   sigue andando SIN token (la web local funciona igual; la API de escritura
#   queda cerrada cuando AXIOMA está expuesto). Nunca se cae por esto.
import logging
import os
import secrets
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

NOMBRE_ARCHIVO = ".web_token"
VARIABLE_ENTORNO = "AXIOMA_WEB_TOKEN"
# Sólo para pruebas y casos avanzados: apuntar el archivo a otra ruta (p. ej. tmp_path).
VARIABLE_ARCHIVO = "AXIOMA_WEB_TOKEN_FILE"

_HOSTS_LOCALES = {"127.0.0.1", "localhost", "::1", ""}


# ═══════════════════════════════════════════════════════════════
# RUTA Y LECTURA
# ═══════════════════════════════════════════════════════════════
def ruta_token_web(ruta: Optional[object] = None) -> Path:
    """Dónde vive el token automático.

    Prioridad: la ruta explícita (pruebas) → `AXIOMA_WEB_TOKEN_FILE` (avanzado) →
    `data_dir` de la configuración → `<proyecto>/data`. La carpeta de datos se
    pregunta al proyecto para que respete `DATA_DIR` (contenedor, instalación).
    """
    if ruta is not None:
        return Path(ruta)
    de_entorno = (os.environ.get(VARIABLE_ARCHIVO) or "").strip()
    if de_entorno:
        return Path(de_entorno).expanduser()
    try:
        from config.settings import settings
        return Path(settings.data_dir) / NOMBRE_ARCHIVO
    except Exception as e:  # noqa: BLE001 — sin configuración se usa la carpeta del código
        logger.debug(f"[token] no pude leer data_dir de la configuración: {e}")
        return Path(__file__).resolve().parents[1] / "data" / NOMBRE_ARCHIVO


def leer_token_guardado(ruta: Optional[object] = None) -> str:
    """Token del archivo del equipo, o "" si no existe / no se puede leer.

    No genera nada y no falla: la ausencia de archivo es el estado normal antes
    de la primera vez.
    """
    archivo = ruta_token_web(ruta)
    try:
        if not archivo.is_file():
            return ""
        return archivo.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning(f"[token] no pude leer {archivo}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════
# RESOLUCIÓN
# ═══════════════════════════════════════════════════════════════
def token_configurado(ruta: Optional[object] = None,
                      entorno: Optional[Mapping[str, str]] = None) -> str:
    """Token disponible AHORA, sin generar ni escribir nada.

    Orden: entorno (`AXIOMA_WEB_TOKEN`) → `.env` (`settings.axioma_web_token`) →
    `data/.web_token`. Cuando se pasa `entorno` explícito (pruebas) la
    configuración global no se consulta: el llamador pidió aislamiento.
    """
    variables = os.environ if entorno is None else entorno
    de_entorno = (variables.get(VARIABLE_ENTORNO) or "").strip()
    if de_entorno:
        return de_entorno

    if entorno is None:
        try:
            # pydantic-settings NO copia el `.env` a `os.environ`; el campo es la
            # única forma de que un valor puesto sólo en el archivo tenga efecto.
            from config.settings import settings
            de_config = (settings.axioma_web_token or "").strip()
            if de_config:
                return de_config
        except Exception as e:  # noqa: BLE001 — sin settings se sigue con el archivo
            logger.debug(f"[token] no pude leer settings.axioma_web_token: {e}")

    return leer_token_guardado(ruta)


def _guardar_token(archivo: Path, valor: str) -> None:
    """Escribe el token con permisos 0600, de forma atómica.

    Se escribe a un temporal con `os.open(..., 0o600)` en vez de crear el archivo
    final con los permisos del sistema (umask podría dejar 0644 y el token es una
    credencial). `os.replace` evita que dos procesos vean un archivo a medias.
    """
    archivo.parent.mkdir(parents=True, exist_ok=True)
    temporal = archivo.with_name(f".{archivo.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as salida:
            salida.write(valor)
        os.replace(temporal, archivo)
        try:
            os.chmod(archivo, 0o600)
        except OSError as e:  # Windows sin permisos POSIX, sistemas de archivos raros
            logger.warning(f"[token] no pude ajustar los permisos de {archivo}: {e}")
    finally:
        if temporal.exists():
            try:
                temporal.unlink()
            except OSError as e:
                # No es fatal: el archivo final ya está escrito. Se registra para no
                # ocultar un temporal que quede dando vueltas.
                logger.debug(f"[token] no pude borrar el temporal {temporal}: {e}")


def token_de_web(ruta: Optional[object] = None,
                 entorno: Optional[Mapping[str, str]] = None,
                 generar: bool = True) -> str:
    """Token de escritura de la web, generándolo si hace falta.

    * Si ya hay uno (entorno, `.env` o archivo) se devuelve ese.
    * Si no y `generar=True`, se crea con `secrets.token_urlsafe(32)` y se
      persiste en `data/.web_token` (0600).
    * Si no se puede escribir, devuelve "" y sólo lo registra: AXIOMA sigue
      funcionando sin token (la web local no lo necesita; la API de escritura
      queda cerrada cuando está expuesta).

    `generar=False` sirve para PREGUNTAR si hay token sin tener el efecto de
    crearlo (lo usa el chequeo de `python main.py --check`).
    """
    existente = token_configurado(ruta, entorno)
    if existente or not generar:
        return existente

    archivo = ruta_token_web(ruta)
    nuevo = secrets.token_urlsafe(32)
    try:
        _guardar_token(archivo, nuevo)
    except OSError as e:
        logger.error(
            f"[token] no pude guardar {archivo} ({e}): AXIOMA sigue sin código de acceso. "
            f"Revisá los permisos de esa carpeta o fijá {VARIABLE_ENTORNO} a mano.")
        return ""
    logger.info(f"[token] código de acceso generado en {archivo} (sólo tu usuario puede leerlo)")
    return nuevo


def rotar_token_web(ruta: Optional[object] = None) -> str:
    """Genera un token NUEVO y lo persiste (invalida el anterior).

    Propaga `OSError` si no se puede escribir: el comando que lo llama tiene que
    poder decirle al usuario que la rotación no quedó guardada (no sirve fingir
    que se rotó).
    """
    archivo = ruta_token_web(ruta)
    nuevo = secrets.token_urlsafe(32)
    _guardar_token(archivo, nuevo)
    logger.info(f"[token] código de acceso rotado en {archivo}")
    return nuevo


# ═══════════════════════════════════════════════════════════════
# EXPOSICIÓN
# ═══════════════════════════════════════════════════════════════
def host_local(host: Optional[str]) -> bool:
    """¿La dirección es sólo de este equipo? (`127.0.0.1`, `localhost`, `::1`).

    Acepta host con puerto (`127.0.0.1:8080`) y la forma `[::1]:8080`; también
    IPv6 sin corchetes. Una cadena vacía se considera local (es el default
    seguro del proyecto). `::` (todas las direcciones IPv6) NO es local.
    """
    crudo = (host or "").strip().lower()
    if not crudo:
        return True
    if crudo.startswith("["):
        crudo = crudo[1:crudo.index("]")] if "]" in crudo else crudo
    if crudo.count(":") > 1:          # IPv6 literal (no es host:puerto)
        return crudo in ("::1", "0:0:0:0:0:0:0:1")
    return crudo.split(":")[0] in _HOSTS_LOCALES


def host_expuesto(host: Optional[str] = None) -> bool:
    """¿AXIOMA está escuchando fuera de este equipo?

    Sin argumento mira `AXIOMA_WEB_HOST` (la misma variable que usan el compose y
    `WebServer`); si nadie la puso, es local.
    """
    efectivo = host if host is not None else os.environ.get("AXIOMA_WEB_HOST", "")
    return not host_local(efectivo)
