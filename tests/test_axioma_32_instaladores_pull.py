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
    import pytest
    raiz = _raiz()
    if not (raiz / "Dockerfile").is_file():
        pytest.skip("el Dockerfile no viaja dentro de la imagen: sólo el código")
    docker = (raiz / "Dockerfile").read_text(encoding="utf-8")
    reqs = (raiz / "requirements.txt").read_text(encoding="utf-8")
    for paquete in ("torch", "sentence-transformers"):
        en_docker = re.search(rf"{paquete}==([0-9][^ \n\\]*)", docker)
        en_reqs = re.search(rf"^{paquete}==([0-9][^\n;]*)", reqs, re.M)
        assert en_docker and en_reqs, f"falta el pin de {paquete}"
        assert en_docker.group(1).strip() == en_reqs.group(1).strip(), (
            f"{paquete}: Dockerfile {en_docker.group(1)} != requirements {en_reqs.group(1)}")


def test_el_compose_monta_la_cache_de_embeddings():
    import pytest
    archivo = _raiz() / "docker-compose.yml"
    if not archivo.is_file():
        pytest.skip("el compose no viaja dentro de la imagen: sólo el código")
    compose = archivo.read_text(encoding="utf-8")
    assert "./cache:/app/cache" in compose, "sin este montaje el modelo se re-descarga en cada arranque"


def test_las_dependencias_solo_linux_llevan_marcador():
    import pytest
    archivo = _raiz() / "requirements.txt"
    if not archivo.is_file():
        pytest.skip("requirements.txt no viaja dentro de la imagen")
    reqs = archivo.read_text(encoding="utf-8")
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


def test_dry_run_no_falla_si_docker_desktop_esta_cerrado(tmp_path, monkeypatch):
    """MEDIDO 2026-10-31: en --dry-run no se abre Docker Desktop; un Desktop cerrado daba exit 4."""
    import subprocess
    bin_dir = _ejecutable(tmp_path, "docker", "#!/bin/sh\nexit 1\n")  # Docker no responde
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("AXIOMA_REPO", "dueno/repo")
    monkeypatch.setenv("AXIOMA_ESPACIO_MIN_GB", "0")
    r = subprocess.run([sys.executable, str(_raiz() / "instalar" / "axioma_setup.py"),
                        "--dry-run", "--destino", str(tmp_path / "AX")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "en ensayo no lo abro" in r.stdout


# ════════════════════════════════════════════════════════════════════════════
# El config temporal NO puede dejar a Docker sin plugins (2026-10-31, causa real)
# ════════════════════════════════════════════════════════════════════════════

def test_el_config_temporal_conserva_los_plugins_en_sh(tmp_path):
    import subprocess
    texto = (_raiz() / "instalar" / "iniciar_axioma.sh").read_text(encoding="utf-8")
    inicio = texto.index("carpetas_plugins_docker()")
    fin = texto.index("paso() {")
    home = tmp_path / "home"
    (home / ".docker" / "cli-plugins").mkdir(parents=True)
    cfg = tmp_path / "cfg"
    guion = texto[inicio:fin] + f'\nmkdir -p "{cfg}" && escribir_config_sin_credenciales "{cfg}"\n'
    r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=30,
                       env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    generado = (cfg / "config.json").read_text(encoding="utf-8")
    assert str(home / ".docker" / "cli-plugins") in generado, "sin los plugins, compose desaparece"
    assert '"auths":{}' in generado.replace(" ", "")


def test_los_guiones_de_windows_conservan_los_plugins():
    for nombre in ("iniciar_axioma.ps1", "instalar_axioma.ps1"):
        texto = (_raiz() / "instalar" / nombre).read_text(encoding="utf-8-sig")
        assert "cliPluginsExtraDirs" in texto, f"{nombre} deja a Docker sin plugins en el reintento"


def test_el_exe_conserva_los_plugins_en_el_config_temporal(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(_raiz() / "instalar"))
    import axioma_setup
    home = tmp_path / "home"
    (home / ".docker" / "cli-plugins").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    carpetas = axioma_setup._carpetas_plugins_docker()
    assert str(home / ".docker" / "cli-plugins") in carpetas
    ruta = axioma_setup._config_sin_credenciales()
    assert ruta is not None
    from pathlib import Path as _Path
    generado = (_Path(ruta) / "config.json").read_text(encoding="utf-8")
    assert "cliPluginsExtraDirs" in generado and str(home / ".docker" / "cli-plugins") in generado


# ════════════════════════════════════════════════════════════════════════════
# Pantalla 🧠 Modelos: elegir del catálogo e instalar con progreso (2026-10-31)
# ════════════════════════════════════════════════════════════════════════════

def test_el_catalogo_ofrece_variantes_para_elegir():
    import sys
    sys.path.insert(0, str(_raiz()))
    from config.model_catalog import cargar_catalogo
    cat = cargar_catalogo(usar_cache=False)
    ollama = [m for m in cat.modelos.values() if m.tipo == "ollama"]
    assert len(ollama) >= 6, f"el catálogo tiene que ofrecer opciones, hay {len(ollama)}"
    for m in ollama:
        assert m.roles and m.para and m.disco_gb > 0 and m.ram_gb > 0, m.nombre
    nombres = {m.nombre for m in ollama}
    for esperado in ("qwen3.5:9b", "qwen2.5-coder:7b", "qwen2.5-coder:3b", "llama3.2:3b"):
        assert esperado in nombres, f"falta {esperado} en el catálogo"


def test_la_pantalla_de_modelos_lista_y_avisa_el_estado():
    import sys
    sys.path.insert(0, str(_raiz()))
    from src.interfaces.components import catalogo_modelos as cm
    modelos = cm._ollama_del_catalogo()
    assert modelos and all(m.tipo == "ollama" for m in modelos)
    assert cm._estado(modelos[0], {modelos[0].nombre}) == "✅ instalado"
    assert cm._estado(modelos[0], set()).startswith("⬇️")


def test_el_boton_modelos_esta_dentro_de_configurar_axioma():
    texto = (_raiz() / "src" / "interfaces" / "components" / "configurar_axioma.py").read_text(encoding="utf-8")
    assert "abrir_catalogo_modelos" in texto and "🧠 Modelos" in texto


def test_la_pantalla_de_modelos_instala_con_progreso():
    texto = (_raiz() / "src" / "interfaces" / "components" / "catalogo_modelos.py").read_text(encoding="utf-8")
    for pieza in ("pull_model", "linear_progress", "asyncio.to_thread"):
        assert pieza in texto, f"la descarga sin {pieza} no muestra avance"


def test_salir_cierra_la_ventana_y_el_lanzador_abre_en_modo_app():
    modales = (_raiz() / "src" / "interfaces" / "components" / "modals.py").read_text(encoding="utf-8")
    lanzador = (_raiz() / "instalar" / "iniciar_axioma.ps1").read_text(encoding="utf-8-sig")
    assert "window.close()" in modales, "Salir tiene que intentar cerrar la ventana"
    assert "--app=$Url" in lanzador, "el lanzador tiene que abrir en modo app para poder cerrarla"


def test_el_motor_de_ollama_arranca_ajustado_para_poca_ram():
    """Verificado 2026-11-03 con ollama/ollama:0.30.0: acepta las variables y flash_attn=enabled."""
    import pytest
    archivo = _raiz() / "docker-compose.yml"
    if not archivo.is_file():
        pytest.skip("el compose no viaja dentro de la imagen: sólo el código")
    compose = archivo.read_text(encoding="utf-8")
    for clave in ("OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE",
                  "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_NUM_PARALLEL"):
        assert clave in compose, f"falta {clave} en el servicio ollama"


def test_la_pantalla_de_modelos_dice_cuanto_hay_que_descargar():
    import sys
    sys.path.insert(0, str(_raiz()))
    from src.interfaces.components import catalogo_modelos as cm
    modelos = cm._ollama_del_catalogo()
    assert cm._total_a_descargar(modelos, set()) > 0
    assert cm._total_a_descargar(modelos, {m.nombre for m in modelos}) == 0


# ════════════════════════════════════════════════════════════════════════════
# Ícono propio y acceso para desinstalar (2026-11-03, pedido del usuario)
# ════════════════════════════════════════════════════════════════════════════

def test_el_icono_existe_y_es_un_ico_valido():
    ruta = _raiz() / "instalar" / "axioma.ico"
    assert ruta.is_file(), "falta instalar/axioma.ico"
    datos = ruta.read_bytes()
    assert datos[:4] == b"\x00\x00\x01\x00", "no tiene cabecera ICO"
    cuantos = int.from_bytes(datos[4:6], "little")
    assert cuantos >= 4, f"el .ico tiene que traer varios tamaños (trae {cuantos})"


def test_el_instalador_crea_el_acceso_de_desinstalar_con_icono():
    texto = (_raiz() / "instalar" / "instalar_axioma.ps1").read_text(encoding="utf-8-sig")
    assert "Desinstalar AXIOMA.lnk" in texto, "no crea el acceso para desinstalar"
    assert "axioma.ico" in texto, "los accesos no usan el ícono propio"
    assert "IconLocation" in texto


def test_el_desinstalador_borra_los_dos_accesos():
    texto = (_raiz() / "instalar" / "desinstalar_axioma.ps1").read_text(encoding="utf-8-sig")
    assert "AXIOMA.lnk" in texto and "Desinstalar AXIOMA.lnk" in texto
    assert (_raiz() / "instalar" / "DESINSTALAR_AXIOMA.bat").is_file()
