#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — INSTALADOR DE UN SOLO ARCHIVO (fuente de AXIOMA-Setup.exe)
# ═══════════════════════════════════════════════════════════════
# ESTE es el archivo que el CI empaqueta como `AXIOMA-Setup.exe` (PyInstaller --onefile), y es lo
# que se descarga del Release. Hace TODO, sin clonar el repositorio y sin tener Python instalado
# (PyInstaller lo lleva adentro):
#   1. Comprueba las herramientas del sistema y Docker Desktop.
#   2. Descarga AXIOMA y lo deja en una carpeta tuya.
#   3. Le pasa el control al instalador del proyecto, que crea las carpetas y tu `.env`, arma la
#      imagen, deja el ACCESO DIRECTO en el menú Inicio y en el Escritorio, y lo enciende.
#
# Es multiplataforma a propósito (mismo archivo, misma lógica): en Windows llama al instalador
# `.ps1` y en Linux/macOS al `.sh`. Así el CI lo puede probar y la lógica vive en un solo lugar.
#
# Uso:
#   AXIOMA-Setup.exe                     # instala y arranca (doble clic)
#   AXIOMA-Setup.exe --dry-run           # muestra qué haría, sin tocar nada
#   AXIOMA-Setup.exe --solo-descargar    # baja y descomprime, y se detiene (para revisar)
#   AXIOMA-Setup.exe --destino C:\AXIOMA # otra carpeta
#
# Variables de entorno: AXIOMA_REPO · AXIOMA_VERSION (por defecto, la última) · AXIOMA_DESTINO
#
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo) · 2 = uso incorrecto
#
# Sin emoji ni dibujos a propósito: la consola clásica de Windows (CP850/CP437) los imprime mal.
# (Mismo criterio que `instalar_axioma.ps1`.)
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# El repositorio NO va escrito acá: el producto que se distribuye no lleva la identidad del autor
# (regla del proyecto, con guardián en `test_axioma_28`). El CI lo fija al armar el Release,
# cambiando `__REPO__` por el repositorio real.
REPO = os.environ.get("AXIOMA_REPO", "__REPO__")
VERSION_DEFECTO = os.environ.get("AXIOMA_VERSION", "main")
ES_WINDOWS = platform.system() == "Windows"


def destino_por_defecto() -> Path:
    """Windows: %LOCALAPPDATA%\\AXIOMA (lo tuyo, sin permisos de administrador). Otros: ~/.axioma/app."""
    if ES_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AXIOMA"
    return Path.home() / ".axioma" / "app"


def paso(texto: str) -> None:
    print(f"\n> {texto}")


def ok(texto: str) -> None:
    print(f"  [OK] {texto}")


def aviso(texto: str) -> None:
    print(f"  [!]  {texto}")


def mal(texto: str) -> None:
    print(f"  [X]  {texto}")


def docker_responde() -> bool:
    """¿Docker está instalado Y respondiendo? (nunca lanza: se informa, no se cae)"""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except OSError:
        return False


ESPERA_DOCKER_SEGUNDOS = 180


def buscar_docker_desktop() -> Path | None:
    """Ruta de «Docker Desktop.exe» (solo Windows).

    Puede estar instalado para todos (Program Files) o solo para el usuario
    (%LOCALAPPDATA%\\Programs\\DockerDesktop). Si docker.exe no está en el PATH de esta ventana pero
    sí en la carpeta de Docker Desktop, lo agrega al PATH de este proceso.
    """
    if not ES_WINDOWS:
        return None
    candidatos: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            candidatos.append(Path(base) / "Docker" / "Docker" / "Docker Desktop.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidatos.append(Path(local) / "Programs" / "DockerDesktop" / "Docker Desktop.exe")
        candidatos.append(Path(local) / "Docker" / "Docker Desktop.exe")
    cli = shutil.which("docker")
    if cli:
        try:
            # ...\DockerDesktop\resources\bin\docker.exe  ->  ...\DockerDesktop
            candidatos.append(Path(cli).resolve().parents[2] / "Docker Desktop.exe")
        except IndexError:
            # La ruta de docker.exe no cuelga de DockerDesktop (pocos niveles): se omite este
            # candidato y se sigue con los demás. Antes era un `pass` mudo, que el auditor marca
            # como fallo silencioso (AUD-SIL-001): en una instalación conviene enterarse.
            print(f"  (aviso) no pude deducir Docker Desktop desde {cli}")
    for exe in candidatos:
        if exe.is_file():
            carpeta_bin = exe.parent / "resources" / "bin"
            if not shutil.which("docker") and (carpeta_bin / "docker.exe").is_file():
                os.environ["PATH"] = str(carpeta_bin) + os.pathsep + os.environ.get("PATH", "")
            return exe
    return None


def asegurar_docker(ensayo: bool) -> bool:
    """True si Docker responde. En Windows, si Docker Desktop está cerrado lo abre y espera."""
    if docker_responde():
        return True
    exe = buscar_docker_desktop()
    if exe is None:
        return False
    if ensayo:
        print(f"     (ensayo) abriria Docker Desktop ({exe}) y esperaria hasta {ESPERA_DOCKER_SEGUNDOS // 60} minutos")
        return False
    aviso(f"Docker Desktop esta cerrado: lo abro yo ({exe}). Puede tardar hasta 3 minutos...")
    try:
        # DETACHED_PROCESS: Docker Desktop queda abierto aunque esta ventana se cierre.
        subprocess.Popen([str(exe)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
    except OSError as error:
        aviso(f"No pude abrirlo: {error}")
        return False
    espera = 0
    while espera < ESPERA_DOCKER_SEGUNDOS:
        time.sleep(3)
        espera += 3
        if docker_responde():
            ok(f"Docker Desktop listo (tardo {espera} s)")
            return True
        if espera % 15 == 0:
            print(f"     ...sigo esperando a Docker Desktop ({espera} s)")
    return False


def descargar(url: str, destino: Path) -> bool:
    """Descarga con progreso. Devuelve False (y explica) si no se pudo."""
    try:
        with urllib.request.urlopen(url, timeout=60) as respuesta:  # noqa: S310 (URL fija del proyecto)
            total = int(respuesta.headers.get("Content-Length") or 0)
            leido = 0
            with open(destino, "wb") as salida:
                while True:
                    bloque = respuesta.read(256 * 1024)
                    if not bloque:
                        break
                    salida.write(bloque)
                    leido += len(bloque)
                    if total:
                        print(f"\r   {leido * 100 // total:3d}%  ({leido // 1048576} MB de "
                              f"{total // 1048576} MB)", end="", flush=True)
            print()
        return True
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        mal(f"No pude descargar AXIOMA: {e}")
        print("     Revisa tu conexion, o fija una version:  AXIOMA_VERSION=v0.7.5")
        return False


def extraer(tarball: Path, destino: Path) -> bool:
    """Descomprime ENCIMA del destino: el código se actualiza y tu `.env`, tus datos y tus registros
    quedan intactos (no vienen en lo descargado, así que no se tocan)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with tarfile.open(tarball) as tf:
                try:
                    tf.extractall(tmp, filter="data")  # Python 3.12+: sin rutas peligrosas
                except TypeError:  # pragma: no cover - Python < 3.12
                    tf.extractall(tmp)
        except (tarfile.TarError, OSError) as e:
            mal(f"Lo descargado no se pudo descomprimir: {e}")
            return False
        raices = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if not raices:
            mal("Lo descargado vino vacío.")
            return False
        origen = raices[0]
        if not (origen / "instalar").is_dir():
            mal("Lo descargado no tiene la forma esperada de AXIOMA (falta instalar/).")
            return False
        destino.mkdir(parents=True, exist_ok=True)
        for elemento in origen.iterdir():
            final = destino / elemento.name
            if elemento.is_dir():
                shutil.copytree(elemento, final, dirs_exist_ok=True)
            else:
                shutil.copy2(elemento, final)
    return True


def elegir_modo_de_acceso(ensayo: bool) -> str:
    """Pregunta —en lenguaje llano— cómo se va a usar AXIOMA y devuelve "1", "2" o "3".

    La elección se le pasa al instalador del proyecto por `AXIOMA_MODO_ACCESO`,
    que la escribe en el `.env` (`AXIOMA_WEB_HOST`). El código de acceso de la web
    ya no se genera a mano: AXIOMA lo genera solo al arrancar expuesto.
    Sin terminal (o en ensayo) sigue con la opción 1, la recomendada.
    """
    modo = (os.environ.get("AXIOMA_MODO_ACCESO") or "").strip()
    if modo in ("1", "2", "3"):
        ok(f"modo de acceso elegido antes: {modo}")
        return modo

    print()
    print("  ¿Cómo vas a usar AXIOMA?")
    print()
    print("  1) Sólo en esta computadora  (recomendado)")
    print("     Nadie más puede entrar. No hay que configurar nada.")
    print()
    print("  2) Desde otros equipos de mi casa/oficina")
    print("     AXIOMA pedirá un código de acceso que se genera solo.")
    print()
    print("  3) Desde internet")
    print("     No recomendado sin ayuda técnica: además del código,")
    print("     conviene un candado extra (HTTPS).")
    print()
    print("  [1]  [2]  [3]        opción por defecto: 1")

    if ensayo or sys.stdin is None or not sys.stdin.isatty():
        print("  (sin una persona para elegir: sigo con la opción 1, la recomendada)")
        return "1"
    while True:
        try:
            respuesta = input("\n  Elegí una opción [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "1"
        if not respuesta:
            return "1"
        if respuesta in ("1", "2", "3"):
            return respuesta
        print("  Escribí 1, 2 o 3 (o Enter para la 1).")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Instalador de AXIOMA de un solo archivo: comprueba Docker, descarga AXIOMA y lo instala.")
    parser.add_argument("--version", default=VERSION_DEFECTO,
                        help="rama o etiqueta a instalar (por defecto: la última)")
    parser.add_argument("--destino", default=os.environ.get("AXIOMA_DESTINO") or str(destino_por_defecto()),
                        help="carpeta donde dejar AXIOMA")
    parser.add_argument("--dry-run", "--ensayo", action="store_true", dest="dry_run",
                        help="muestra qué haría, sin descargar ni tocar nada")
    parser.add_argument("--solo-descargar", action="store_true", dest="solo_descargar",
                        help="descarga y descomprime, y se detiene (para revisar)")
    args = parser.parse_args()

    destino = Path(args.destino).expanduser().resolve()

    # Un repositorio válido siempre es `dueno/nombre`: sin `/`, es la plantilla sin estampar.
    if "/" not in REPO:
        mal("A este archivo le falta el repositorio (es la plantilla, no el instalador del Release).")
        print("     Usa el que se descarga del Release, o fijalo asi:")
        print("       AXIOMA_REPO=dueno/repositorio AXIOMA-Setup.exe")
        return 4

    paso("AXIOMA - instalacion desde un solo archivo")
    print(f"   Version a instalar: {args.version}")
    print(f"   Carpeta destino:    {destino}")
    if args.dry_run:
        aviso("modo ensayo: no voy a descargar ni a tocar nada")

    # ── 1. Docker ───────────────────────────────────────────────────────────
    paso("Paso 1 de 4: Docker (es el programa que ejecuta AXIOMA)")
    desktop = buscar_docker_desktop()          # tambien arregla el PATH si hace falta
    if not shutil.which("docker") and desktop is None:
        mal("Docker no esta instalado.")
        print("     Windows: Docker Desktop desde https://www.docker.com/products/docker-desktop")
        print("              (necesita WSL 2: si te lo pide, 'wsl --install' como administrador y reinicia)")
        print("     Linux:   el de tu distribucion (o 'sudo systemctl start docker' si ya lo tenes)")
        return 4
    if not asegurar_docker(args.dry_run):
        mal("Docker esta instalado pero no llego a responder (lo mas comun: Docker Desktop esta cerrado).")
        print("     Windows: abri Docker Desktop desde el menu Inicio y espera a que diga 'Engine running'")
        print("              (si te pide aceptar los terminos o actualizar WSL, hacelo y volve a ejecutar esto)")
        print("     Linux:   sudo systemctl start docker")
        return 4
    ok(f"Docker responde ({platform.system()})")

    # ── 2. Descarga ─────────────────────────────────────────────────────────
    url = f"https://github.com/{REPO}/archive/{args.version}.tar.gz"
    paso("Paso 2 de 4: descargando AXIOMA")
    print(f"   {url}")
    paso("Paso 3 de 4: dejando AXIOMA en su carpeta")
    if args.dry_run:
        print(f"   (ensayo) quedaria en {destino}, sin tocar tu .env ni tus datos")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / "axioma.tar.gz"
            if not descargar(url, tarball):
                return 4
            ok(f"Descargado ({tarball.stat().st_size // 1048576} MB)")
            if not extraer(tarball, destino):
                return 4
            ok(f"AXIOMA quedo en {destino}")
            if (destino / ".env").exists():
                ok("Tu .env ya existia: lo deje exactamente como estaba")

    if args.solo_descargar:
        paso("Listo (solo descarga)")
        instalador = "instalar\\instalar_axioma.ps1" if ES_WINDOWS else "instalar/instalar_axioma.sh"
        print(f"   Para instalarlo ahora:  {instalador}")
        return 0

    # ── 4. La instalación de verdad (la hace el instalador del proyecto) ────
    paso("Paso 4 de 4: instalando (carpetas, .env, imagen, accesos directos y arranque)")
    modo_acceso = elegir_modo_de_acceso(args.dry_run)
    if args.dry_run:
        instalador = "instalar\\instalar_axioma.ps1" if ES_WINDOWS else "instalar/instalar_axioma.sh"
        print(f"   (ensayo) ejecutaria:  {destino / instalador}")
        ok("Ensayo terminado: no se descargo ni se instalo nada.")
        return 0

    if ES_WINDOWS:
        # -ExecutionPolicy Bypass: funciona aunque Windows tenga la ejecucion de guiones
        # deshabilitada, que es lo normal recien instalado.
        orden = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(destino / "instalar" / "instalar_axioma.ps1")]
    else:
        orden = ["bash", str(destino / "instalar" / "instalar_axioma.sh")]
    print(f"   Ejecutando: {' '.join(orden)}")
    # ✅ 2026-10-23: el instalador del proyecto intenta BAJAR la imagen publicada y, si no puede,
    # construye como siempre. Para eso necesita saber la version (tag) y el repositorio: se los
    # pasamos por entorno, igual que el instalador de un solo archivo de Linux. La imagen vive en
    # ghcr.io/<repositorio en minusculas>; sin estos datos el instalador solo construye.
    entorno = dict(os.environ)
    entorno["AXIOMA_VERSION"] = args.version
    # La elección de la opción 1/2/3 viaja al instalador del proyecto: él escribe
    # AXIOMA_WEB_HOST en el .env y no vuelve a preguntar.
    entorno["AXIOMA_MODO_ACCESO"] = modo_acceso
    if "/" in REPO:
        entorno.setdefault("AXIOMA_REPO", REPO)
        entorno.setdefault("AXIOMA_IMAGEN", "ghcr.io/" + REPO.lower())
    try:
        return subprocess.run(orden, cwd=str(destino), env=entorno).returncode
    except OSError as e:
        mal(f"No pude ejecutar el instalador: {e}")
        return 4


if __name__ == "__main__":
    codigo = main()
    # La ventana no se cierra sin dejar leer el resultado (sólo si hay alguien mirando).
    if ES_WINDOWS and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPresiona Enter para cerrar...")
        except (EOFError, KeyboardInterrupt):
            # Degradación INTENCIONAL (lo pide el auditor `silent_exceptions`): si no hay consola
            # interactiva o el usuario cancela con Ctrl+C, no hay nada que hacer ni que informar —
            # se cierra igual y sin dejar una excepción en la cara de quien lo ejecutó.
            print()
    sys.exit(codigo)
