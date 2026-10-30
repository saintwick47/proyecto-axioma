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
        print("     Revisa tu conexion, o fija una version:  AXIOMA_VERSION=<etiqueta>")
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
                    # ✅ 2026-10-29 (CVE-2007-4559): sin `filter="data"` hay que validar a mano:
                    # nada absoluto, nada con "..", nada de enlaces (símbolicos o duros).
                    for miembro in tf.getmembers():
                        nombre = miembro.name.replace("\\", "/")
                        if (nombre.startswith("/") or ".." in nombre.split("/")
                                or miembro.issym() or miembro.islnk()):
                            raise tarfile.TarError(
                                f"el paquete trae una entrada peligrosa: {miembro.name!r}")
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


def _correr(args: list, cwd: Path = None, timeout: int = 20) -> str:
    """Ejecuta un comando de diagnóstico y devuelve su salida (nunca lanza)."""
    try:
        r = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, errors="replace")
        texto = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return texto.strip() or "(sin salida)"
    except Exception as e:  # noqa: BLE001 — el diagnóstico no puede fallar por un comando
        return f"(no se pudo ejecutar {' '.join(args)}: {type(e).__name__}: {e})"


def escribir_diagnostico(destino: Path) -> "Path | None":
    """Escribe un archivo con todo lo necesario para diagnosticar un arranque fallido.

    ✅ 2026-10-28: versiones, estado de Docker/Compose, contenedores, puertos, PATH y los logs del
    arranque. NUNCA incluye valores de claves: del `.env` sólo van los NOMBRES de las variables.
    Devuelve la ruta del archivo, o None si no se pudo escribir.
    """
    try:
        marca = time.strftime("%Y%m%d_%H%M%S")
        carpeta = Path(destino) / "logs"
        carpeta.mkdir(parents=True, exist_ok=True)
        comp = Path(destino) / "docker-compose.yml"
        partes = [
            "AXIOMA — diagnostico de instalacion",
            f"fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"version del instalador: {REPO} · plataforma: {platform.platform()}",
            f"python: {sys.version.split()[0]}",
            f"carpeta: {destino}",
            f"docker-compose.yml: {'SI' if comp.is_file() else 'NO'} · .env: {'SI' if (Path(destino)/'.env').is_file() else 'NO'}",
            "",
            "== claves presentes en .env (solo nombres) ==",
        ]
        env = Path(destino) / ".env"
        if env.is_file():
            nombres = [ln.split("=", 1)[0].strip() for ln in env.read_text(encoding="utf-8", errors="replace").splitlines()
                       if "=" in ln and not ln.strip().startswith("#")]
            partes.append(", ".join(nombres) or "(ninguna)")
        else:
            partes.append("(no hay .env)")
        partes += [
            "",
            "== docker ==",
            _correr(["docker", "version", "--format", "cliente {{.Client.Version}} · servidor {{.Server.Version}}"]),
            "",
            "== docker info (recortado) ==",
            _correr(["docker", "info"])[:3000],
            "",
            "== docker compose ==",
            _correr(["docker", "compose", "version"]),
            "",
            "== imagenes (axioma/ollama) ==",
            _correr(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"]),
            "",
            "== contenedores (todos) ==",
            _correr(["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]),
            "",
        ]
        if comp.is_file():
            partes += [
                "== compose: servicios ==",
                _correr(["docker", "compose", "-f", str(comp), "config", "--services"]),
                "",
                "== compose ps -a ==",
                _correr(["docker", "compose", "-f", str(comp), "ps", "-a"]),
                "",
                "== logs axioma-puente (80) ==",
                _correr(["docker", "compose", "-f", str(comp), "logs", "--tail", "80", "axioma-puente"], timeout=30),
                "",
                "== logs ollama (80) ==",
                _correr(["docker", "compose", "-f", str(comp), "logs", "--tail", "80", "ollama"], timeout=30),
                "",
            ]
        partes += [
            "== ayudante de credenciales de Docker ==",
            f"docker-credential-desktop: {shutil.which('docker-credential-desktop') or 'NO ENCONTRADO en PATH'}",
            f"docker-credential-pass:    {shutil.which('docker-credential-pass') or 'no' }",
            "entradas de PATH con docker:",
            "\n".join(p for p in os.environ.get("PATH", "").split(os.pathsep) if "docker" in p.lower()) or "(ninguna)",
            "",
            "== puertos en uso (11434/8080) ==",
            _correr(["netstat", "-ano"]) if platform.system() == "Windows" else _correr(["ss", "-ltn"]),
        ]
        ruta = carpeta / f"diagnostico_{marca}.txt"
        ruta.write_text("\n".join(partes), encoding="utf-8")
        return ruta
    except Exception:  # noqa: BLE001 — el diagnóstico es best-effort, nunca rompe el instalador
        return None


def _config_sin_credenciales() -> "str | None":
    """Si Docker no tiene su ayudante de credenciales, prepara un DOCKER_CONFIG temporal.

    ✅ 2026-10-30 (MEDIDO en Windows, dos veces): Docker Desktop con `credsStore: desktop` y el
    ejecutable `docker-credential-desktop` fuera del PATH hace fallar CUALQUIER comando que
    consulte credenciales (pull/build/up) con:
        error getting credentials - err: exec: "docker-credential-desktop": ... not found in %PATH%
    AXIOMA usa sólo imágenes públicas, así que un config sin `credsStore` para esta corrida evita
    el problema de raíz (los instaladores ya lo hacen por comando; esto lo hace para TODO el run).
    """
    if shutil.which("docker-credential-desktop") or shutil.which("docker-credential-pass"):
        return None
    try:
        cfg = Path(tempfile.mkdtemp(prefix="axioma-dcfg-"))
        (cfg / "config.json").write_text('{"auths":{}}', encoding="utf-8")
        return str(cfg)
    except OSError as e:  # noqa: BLE001 — si no se puede, se sigue con el entorno tal cual
        aviso(f"No pude preparar un DOCKER_CONFIG temporal: {e}")
        return None


def diagnostico_previo(destino: Path) -> tuple:
    """Comprobaciones ANTES de instalar: Docker, espacio, WSL y ayudante de credenciales.

    ✅ 2026-10-30 (idea de Qwen, acotada a lo que ya sabemos medir): devuelve (críticos, avisos).
    Los críticos impiden instalar (sin Docker o sin espacio no hay nada que hacer); los avisos se
    explican y se sigue.
    """
    criticos: list = []
    avisos: list = []
    paso("Comprobaciones previas")

    # Docker (con el Desktop ya arrancado por el paso 1)
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if r.returncode == 0:
            ok("Docker responde")
        else:
            criticos.append("Docker instalado pero no responde")
            mal("Docker instalado pero no responde (¿Docker Desktop cerrado?)")
    except Exception as e:  # noqa: BLE001
        criticos.append(f"Docker no se pudo ejecutar ({type(e).__name__})")
        mal(f"Docker no se pudo ejecutar: {e}")

    # Espacio en disco (imagen ~5,5 GB + modelos ~6,6 GB + margen). El umbral se puede bajar con
    # AXIOMA_ESPACIO_MIN_GB (los tests del contenedor lo ponen en 0: el disco del runner es chico).
    minimo_gb = float(os.environ.get("AXIOMA_ESPACIO_MIN_GB", "15"))
    base = destino if destino.exists() else Path.home()
    try:
        libres = shutil.disk_usage(base).free / (1024 ** 3)
        if libres < minimo_gb:
            criticos.append(f"Sólo {libres:.1f} GB libres (hacen falta ~{minimo_gb:.0f} GB)")
            mal(f"Espacio: {libres:.1f} GB libres (hacen falta ~{minimo_gb:.0f} GB)")
        else:
            ok(f"Espacio: {libres:.1f} GB libres")
    except OSError as e:  # noqa: BLE001
        avisos.append(f"No se pudo medir el disco: {e}")

    # WSL 2 (sólo Windows; Docker Desktop lo necesita)
    if ES_WINDOWS:
        try:
            r = subprocess.run(["wsl", "--status"], capture_output=True, timeout=10)
            if r.returncode == 0:
                ok("WSL 2 disponible")
            else:
                avisos.append("WSL 2 no parece configurado (Docker Desktop lo necesita)")
                aviso("WSL 2 no parece configurado: Docker Desktop lo necesita")
        except FileNotFoundError:
            avisos.append("WSL no está instalado")
            aviso("WSL no está instalado (Docker Desktop lo necesita)")
        except Exception as e:  # noqa: BLE001
            avisos.append(f"No se pudo comprobar WSL: {e}")

    # Ayudante de credenciales (el error que ya nos costó dos instalaciones)
    if shutil.which("docker-credential-desktop") or shutil.which("docker-credential-pass"):
        ok("Ayudante de credenciales de Docker presente")
    else:
        avisos.append("Docker no encuentra su ayudante de credenciales")
        aviso("Docker no encuentra 'docker-credential-desktop': uso un DOCKER_CONFIG temporal")
        print("     (las imágenes de AXIOMA son públicas: no hacen falta credenciales)")
    return criticos, avisos


def desinstalar(destino: Path, todo: bool = False) -> int:
    """Ejecuta el desinstalador del proyecto (lo deja limpio para empezar de cero).

    ✅ 2026-10-28 (pedido del usuario): sin esto, si una instalación queda a medias no hay forma
    sencilla de empezar de cero. Por defecto CONSERVA los datos (data/, logs/, .env); con `todo`
    los borra junto con los modelos descargados.
    """
    paso("AXIOMA - desinstalación")
    print(f"   Carpeta: {destino}")
    if not destino.is_dir():
        aviso(f"No hay una instalación de AXIOMA en {destino}.")
        return 0
    guion = destino / "instalar" / ("desinstalar_axioma.ps1" if ES_WINDOWS else "desinstalar_axioma.sh")
    if not guion.is_file():
        mal(f"No encontré el desinstalador del proyecto ({guion}).")
        print("     Podés borrar la carpeta a mano y quitar los contenedores con:")
        print("       docker compose -f <carpeta>\\docker-compose.yml down --remove-orphans")
        return 4
    if ES_WINDOWS:
        orden = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(guion)]
        if todo:
            orden.append("-Todo")
    else:
        orden = ["bash", str(guion)]
        if todo:
            orden.append("--todo")
    try:
        return subprocess.run(orden, cwd=str(destino)).returncode
    except OSError as e:
        mal(f"No pude ejecutar el desinstalador: {e}")
        return 4


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
    parser.add_argument("--solo-diagnostico", action="store_true", dest="solo_diagnostico",
                        help="sólo comprueba el equipo (Docker, espacio, WSL, credenciales) y sale")
    parser.add_argument("--desinstalar", action="store_true",
                        help="quita contenedores, imágenes, accesos directos y la carpeta de AXIOMA")
    parser.add_argument("--todo", action="store_true",
                        help="con --desinstalar: borra TAMBIÉN tus datos y los modelos descargados")
    args = parser.parse_args()

    destino = Path(args.destino).expanduser().resolve()

    if args.desinstalar:
        return desinstalar(destino, todo=args.todo)

    # ✅ 2026-10-30: modo diagnóstico solo (no instala nada).
    if args.solo_diagnostico:
        paso("AXIOMA - diagnóstico del equipo (no instala)")
        criticos, avisos = diagnostico_previo(destino)
        ruta = escribir_diagnostico(destino) if destino.is_dir() else None
        if ruta:
            print(f"\n   Diagnóstico completo en: {ruta}")
        if criticos:
            mal(f"{len(criticos)} problema(s) crítico(s): " + "; ".join(criticos))
            return 4
        ok("Sin problemas críticos: el equipo puede instalar AXIOMA")
        return 0

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

    # ✅ 2026-10-30: si falta el ayudante de credenciales, un DOCKER_CONFIG temporal para TODO el run
    # (mejor que reintentar comando por comando: cubre pull, build y up).
    _cfg = _config_sin_credenciales()
    if _cfg:
        os.environ["DOCKER_CONFIG"] = _cfg

    # ✅ 2026-10-30: comprobaciones previas (espacio/WSL/credenciales). Los críticos frenan.
    criticos, _avisos = diagnostico_previo(destino)
    if criticos:
        mal("No se puede instalar todavía: " + "; ".join(criticos))
        print("     Corregí eso y volvé a ejecutar el instalador (o AXIOMA-Setup.exe --solo-diagnostico).")
        return 4

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
        codigo = subprocess.run(orden, cwd=str(destino), env=entorno).returncode
    except OSError as e:
        mal(f"No pude ejecutar el instalador: {e}")
        codigo = 4
    if codigo != 0:
        # ✅ 2026-10-28 (pedido del usuario tras una instalación real fallida): junto con el error,
        # dejar un diagnóstico en un archivo para no adivinar. No incluye valores de claves.
        ruta = escribir_diagnostico(destino)
        if ruta:
            aviso(f"Diagnostico guardado en: {ruta}")
            print("     Pasalo tal cual: tiene versiones, contenedores, puertos y los logs del arranque.")
    return codigo


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
