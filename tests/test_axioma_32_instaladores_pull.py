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
import sys
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


# ════════════════════════════════════════════════════════════════════════════
# Desinstalador (2026-10-28, pedido del usuario: poder empezar de cero)
# ════════════════════════════════════════════════════════════════════════════

def _preparar_instalacion(tmp_path):
    """Copia mínima del proyecto con el desinstalador y un docker falso que registra llamadas."""
    import shutil
    from pathlib import Path as _Path
    raiz = _Path(__file__).resolve().parent.parent
    destino = tmp_path / "AXIOMA"
    (destino / "instalar").mkdir(parents=True)
    shutil.copy(raiz / "instalar" / "desinstalar_axioma.sh", destino / "instalar" / "desinstalar_axioma.sh")
    (destino / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (destino / "data").mkdir()
    (destino / "data" / "axioma.db").write_text("datos del usuario", encoding="utf-8")
    (destino / ".env").write_text("CLAVE=valor\n", encoding="utf-8")
    return destino


def test_el_desinstalador_tiene_ensayo_y_conserva_datos_por_defecto(tmp_path):
    import subprocess
    from pathlib import Path as _Path
    raiz = _Path(__file__).resolve().parent.parent
    guion = raiz / "instalar" / "desinstalar_axioma.sh"
    r = subprocess.run(["bash", str(guion), "--dry-run"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "ensayo" in r.stdout and "TUS DATOS se conservan" in r.stdout


def test_el_desinstalador_normal_respalda_datos_y_borra_la_carpeta(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path as _Path
    destino = _preparar_instalacion(tmp_path)
    falso = tmp_path / "bin"
    falso.mkdir()
    registro = tmp_path / "docker.log"
    docker = falso / "docker"
    docker.write_text(
        "#!/bin/sh\necho \"$@\" >> \"$REGISTRO\"\n"
        "if [ \"$1\" = \"info\" ]; then exit 1; fi\n"
        "exit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{falso}:/usr/bin:/bin")
    monkeypatch.setenv("REGISTRO", str(registro))
    monkeypatch.setenv("HOME", str(tmp_path))
    r = subprocess.run(["bash", str(destino / "instalar" / "desinstalar_axioma.sh"), "--si"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not destino.exists(), "la carpeta tiene que desaparecer"
    respaldos = list(tmp_path.glob("axioma-datos-*"))
    assert respaldos, "los datos tienen que quedar respaldados"
    assert (respaldos[0] / "data" / "axioma.db").is_file(), "el .db del usuario se conserva"
    assert (respaldos[0] / ".env").is_file(), "el .env se conserva"


def test_el_exe_avisa_si_no_hay_instalacion_que_desinstalar(tmp_path):
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "instalar"))
    import axioma_setup
    assert axioma_setup.desinstalar(tmp_path / "no-existe", todo=False) == 0


# ════════════════════════════════════════════════════════════════════════════
# Coherencia Dockerfile/requirements/compose y extracción segura (2026-10-29)
# ════════════════════════════════════════════════════════════════════════════

def _raiz():
    from pathlib import Path as _Path
    return _Path(__file__).resolve().parent.parent


def test_el_dockerfile_usa_las_mismas_versiones_que_requirements():
    import re
    raiz = _raiz()
    docker = (raiz / "Dockerfile").read_text(encoding="utf-8")
    reqs = (raiz / "requirements.txt").read_text(encoding="utf-8")
    for paquete in ("torch", "sentence-transformers"):
        en_docker = re.search(rf"{paquete}==([0-9][^ \n\\]*)", docker)
        en_reqs = re.search(rf"^{paquete}==([0-9][^\n;]*)", reqs, re.M)
        assert en_docker and en_reqs, f"falta el pin de {paquete}"
        assert en_docker.group(1).strip() == en_reqs.group(1).strip(), (
            f"{paquete}: Dockerfile {en_docker.group(1)} != requirements {en_reqs.group(1)}")


def test_el_compose_monta_la_cache_de_embeddings():
    compose = (_raiz() / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./cache:/app/cache" in compose, "sin este montaje el modelo se re-descarga en cada arranque"


def test_las_dependencias_solo_linux_llevan_marcador():
    reqs = (_raiz() / "requirements.txt").read_text(encoding="utf-8")
    for paquete in ("evdev", "uvloop", "pyatspi"):
        linea = next((ln for ln in reqs.splitlines() if ln.startswith(f"{paquete}==")), None)
        assert linea, f"falta {paquete}"
        assert 'sys_platform == "linux"' in linea, f"{paquete} se rompe en Windows sin el marcador"


def test_la_extraccion_rechaza_rutas_peligrosas(tmp_path):
    import io
    import sys
    import tarfile
    sys.path.insert(0, str(_raiz() / "instalar"))
    import axioma_setup

    malo = tmp_path / "malo.tar.gz"
    with tarfile.open(malo, "w:gz") as tf:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    destino = tmp_path / "AXIOMA"
    assert axioma_setup.extraer(malo, destino) is False, "tenía que rechazarse"
    assert not (tmp_path.parent / "evil.txt").exists()


# ════════════════════════════════════════════════════════════════════════════
# Diagnóstico previo del .exe (2026-10-30, pedido tras el error de credenciales)
# ════════════════════════════════════════════════════════════════════════════

def _ejecutable(tmp_path, nombre, contenido):
    from pathlib import Path as _Path
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / nombre
    exe.write_text(contenido, encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir


def test_solo_diagnostico_sale_0_si_docker_responde(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--solo-diagnostico", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Sin problemas críticos" in r.stdout


def test_solo_diagnostico_sale_4_si_docker_no_responde(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--solo-diagnostico", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "crítico" in r.stdout.lower()


def test_sin_ayudante_de_credenciales_usa_un_config_temporal(tmp_path, monkeypatch):
    import importlib.util
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    spec = importlib.util.spec_from_file_location("setup_diag", _raiz() / "instalar" / "axioma_setup.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    ruta = modulo._config_sin_credenciales()
    assert ruta is not None
    from pathlib import Path as _Path
    assert (_Path(ruta) / "config.json").is_file()
    assert "credsStore" not in (_Path(ruta) / "config.json").read_text(encoding="utf-8")
    # Y si el ayudante existe, no toca nada.
    _ejecutable(tmp_path, "docker-credential-desktop", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert modulo._config_sin_credenciales() is None


def test_el_exe_diagnostica_antes_de_instalar(tmp_path, monkeypatch):
    """Pedido del usuario: el diagnóstico tiene que ir PRIMERO, no después de 'Paso 1 de 4'."""
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("AXIOMA_REPO", "dueno/repo")
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--dry-run", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.index("Diagnóstico previo") < r.stdout.index("Paso 1 de 4"), (
        "el diagnóstico tiene que aparecer antes de empezar los pasos de instalación")


def test_sin_compose_el_diagnostico_frena(tmp_path, monkeypatch):
    """Causa confirmada (2026-10-30): sin el plugin Compose v2, `docker compose ...` cae al CLI."""
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nif [ \"$1\" = \"compose\" ]; then exit 1; fi\nexit 0\n")
    _ejecutable(tmp_path, "docker-compose", "#!/bin/sh\nexit 1\n")  # tapa el del sistema
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--solo-diagnostico", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "Compose" in r.stdout


def test_sin_compose_pero_con_docker_compose_v1_sigue(tmp_path, monkeypatch):
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nif [ \"$1\" = \"compose\" ]; then exit 1; fi\nexit 0\n")
    _ejecutable(tmp_path, "docker-compose", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--solo-diagnostico", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker-compose v1" in r.stdout
