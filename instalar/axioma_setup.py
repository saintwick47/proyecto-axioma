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
    if not shutil.which("docker"):
        mal("Docker no esta instalado.")
        print("     Windows: Docker Desktop desde https://www.docker.com/products/docker-desktop")
        print("              (necesita WSL 2: si te lo pide, 'wsl --install' como administrador y reinicia)")
        print("     Linux:   el de tu distribucion (o 'sudo systemctl start docker' si ya lo tenes)")
        return 4
    if not docker_responde():
        mal("Docker esta instalado pero no responde (lo mas comun: Docker Desktop esta cerrado).")
        print("     Windows: abrilo desde el menu Inicio y espera a que diga 'Engine running'")
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
    try:
        return subprocess.run(orden, cwd=str(destino)).returncode
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
