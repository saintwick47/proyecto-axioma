#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# INSTALADORES — bajar la imagen publicada antes de construir (2026-10-23)
# ═══════════════════════════════════════════════════════════════
# Los instaladores intentan `docker pull ghcr.io/<AXIOMA_REPO>:<AXIOMA_VERSION>` (y `latest`
# como segundo intento), etiquetan lo bajado como `axioma:local` y sólo caen al build si no
# pueden. Hasta ahora eso se había probado a mano; acá queda en la suite.
#
# Se ejecuta el instalador REAL sobre un árbol temporal con un `docker` falso en el PATH:
# sin red y sin Docker. El falso anota cada llamada y falla los `pull` que se le piden por
# entorno. El árbol temporal lleva una copia del instalador (la raíz se calcula desde la
# ubicación del guion) y un `iniciar_axioma.sh` de mentira, para que la prueba termine después
# del bloque de imagen sin arrancar nada.
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_GHCR = "ghcr.io/saintwick47/proyecto-axioma"

_DOCKER_FALSO = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$AXIOMA_TEST_DOCKER_LOG"
case "$1" in
  info) exit 0 ;;
  --version) echo 'Docker version 99.0.0, build falso'; exit 0 ;;
  pull)
    for tag in ${AXIOMA_TEST_PULL_FALLAN:-}; do
      [ "$tag" = "${2##*:}" ] && exit 1
    done
    exit 0 ;;
  tag|compose) exit 0 ;;
  *) exit 0 ;;
esac
"""


def _preparar(tmp_path, *, fallan=""):
    """Arma el árbol temporal, el `docker` falso y el entorno del instalador."""
    raiz = tmp_path / "axioma"
    (raiz / "instalar").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "instalar" / "instalar_axioma.sh",
                 raiz / "instalar" / "instalar_axioma.sh")
    iniciador = raiz / "instalar" / "iniciar_axioma.sh"
    iniciador.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    iniciador.chmod(0o755)
    (raiz / ".env.example").write_text("OLLAMA_HOST=http://127.0.0.1:11434\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_DOCKER_FALSO, encoding="utf-8")
    docker.chmod(0o755)

    registro = tmp_path / "docker.log"
    registro.write_text("", encoding="utf-8")
    entorno = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "AXIOMA_TEST_DOCKER_LOG": str(registro),
        "AXIOMA_TEST_PULL_FALLAN": fallan,
    }
    return raiz, registro, entorno


def _correr(tmp_path, *, repo="saintwick47/proyecto-axioma", version="v0.7.4", fallan=""):
    """Corre el instalador real (con `--sin-acceso`) y devuelve salida y llamadas a docker."""
    raiz, registro, entorno = _preparar(tmp_path, fallan=fallan)
    if repo is not None:
        entorno["AXIOMA_REPO"] = repo
    if version is not None:
        entorno["AXIOMA_VERSION"] = version
    salida = subprocess.run(
        ["bash", str(raiz / "instalar" / "instalar_axioma.sh"), "--sin-acceso"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=entorno, timeout=60)
    return salida, registro.read_text(encoding="utf-8").splitlines()


def test_si_se_baja_la_version_no_construye(tmp_path):
    """Éxito: baja el tag de `AXIOMA_VERSION`, lo etiqueta y NO construye."""
    salida, llamadas = _correr(tmp_path)

    assert salida.returncode == 0, (salida.returncode, salida.stdout, salida.stderr)
    assert llamadas == [
        "info",
        "--version",
        f"pull {_GHCR}:v0.7.4",
        f"tag {_GHCR}:v0.7.4 axioma:local",
    ], llamadas
    assert "Imagen bajada" in salida.stdout, salida.stdout
    assert "No pude bajarla" not in salida.stdout, salida.stdout


def test_si_falla_la_version_prueba_latest_y_no_construye(tmp_path):
    """La versión no existe: cae a `latest`; si baja, tampoco construye."""
    salida, llamadas = _correr(tmp_path, fallan="v0.7.4")

    assert salida.returncode == 0, (salida.returncode, salida.stdout, salida.stderr)
    assert llamadas == [
        "info",
        "--version",
        f"pull {_GHCR}:v0.7.4",
        f"pull {_GHCR}:latest",
        f"tag {_GHCR}:latest axioma:local",
    ], llamadas
    assert "Imagen bajada" in salida.stdout, salida.stdout
    assert "No pude bajarla" not in salida.stdout, salida.stdout


def test_si_fallan_las_dos_baja_al_build_con_el_aviso(tmp_path):
    """Ningún tag se puede bajar: hace los dos intentos y construye, avisando."""
    salida, llamadas = _correr(tmp_path, fallan="v0.7.4 latest")

    assert salida.returncode == 0, (salida.returncode, salida.stdout, salida.stderr)
    assert llamadas == [
        "info",
        "--version",
        f"pull {_GHCR}:v0.7.4",
        f"pull {_GHCR}:latest",
        "compose build",
    ], llamadas
    assert "No pude bajarla; la construyo" in salida.stdout, salida.stdout
    assert "Imagen bajada" not in salida.stdout, salida.stdout


def test_sin_repo_no_intenta_bajar_y_construye(tmp_path):
    """Sin `AXIOMA_REPO` (clon directo) no hay imagen publicada que buscar: construye como hoy."""
    salida, llamadas = _correr(tmp_path, repo=None, version=None)

    assert salida.returncode == 0, (salida.returncode, salida.stdout, salida.stderr)
    assert llamadas == ["info", "--version", "compose build"], llamadas
    assert not any(c.startswith("pull ") for c in llamadas), llamadas
    assert "Bajando la imagen" not in salida.stdout, salida.stdout
    assert "No pude bajarla" not in salida.stdout, salida.stdout


# ════════════════════════════════════════════════════════════════════════════
# Diagnóstico del instalador (2026-10-28, pedido tras un fallo real en Windows)
# ════════════════════════════════════════════════════════════════════════════

def test_el_diagnostico_se_escribe_y_no_filtra_claves(tmp_path, monkeypatch):
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "instalar"))
    import axioma_setup

    destino = tmp_path / "AXIOMA"
    (destino / "logs").mkdir(parents=True)
    (destino / ".env").write_text("CLAVE_DE_PRUEBA=valor-que-no-debe-salir\n", encoding="utf-8")
    (destino / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    falso = tmp_path / "bin"
    falso.mkdir()
    docker = falso / "docker"
    docker.write_text("#!/bin/sh\necho \"falso $1 $2\"\n", encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{falso}:/usr/bin:/bin")

    ruta = axioma_setup.escribir_diagnostico(destino)
    assert ruta is not None and ruta.is_file(), "tiene que dejar el archivo"
    texto = ruta.read_text(encoding="utf-8")
    assert "== docker ==" in texto and "== contenedores (todos) ==" in texto
    assert "CLAVE_DE_PRUEBA" in texto, "los NOMBRES de las claves sí van (sirven para saber qué falta)"
    assert "valor-que-no-debe-salir" not in texto, "los VALORES de las claves nunca"
