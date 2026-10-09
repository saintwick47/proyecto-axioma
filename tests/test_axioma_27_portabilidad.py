#!/usr/bin/env python3
import ast
import json
import sys

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Los archivos de EMPAQUETADO (`docker-compose.yml`) no viajan dentro de la imagen: la suite corre
_sin_compose = pytest.mark.skipif(
    not (PROJECT_ROOT / "docker-compose.yml").exists(),
    reason="el empaquetado no viaja dentro de la imagen (se comprueba en el código)")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_la_raiz_del_proyecto_es_la_carpeta_del_archivo():
    """B1: el punto de entrada tiene que usar SU carpeta, no la del autor.

    Medido el 2026-10-04: corriendo una copia en `/tmp/axioma_fase0`, `main.py --check`
    informaba y **escribía** en `/ruta/a/axioma` (la carpeta original).
    """
    import importlib
    main = importlib.import_module("main")
    # Se compara contra la carpeta del ARCHIVO, no contra la raíz del repo: en el equipo del
    assert Path(main.PROJECT_ROOT).resolve() == Path(main.__file__).resolve().parent, \
        main.PROJECT_ROOT
    assert Path(main.PROJECT_ROOT).is_dir()


@pytest.mark.skipif(not (PROJECT_ROOT / "tools" / "documentador").is_dir(),
                    reason="el documentador queda en el repositorio privado: no lo publica "
                           "preparar_publicacion.sh y el público no lo necesita")
def test_el_documentador_usa_la_raiz_del_proyecto():
    """B3: mismo defecto en `tools/documentador/axioma_doc_base.py`.

    El documentador se usa como SCRIPTS de su propia carpeta (importa `axioma_doc_base_utils`
    sin paquete), así que en la prueba se pone su carpeta en `sys.path`, igual que cuando se
    lo ejecuta de verdad.
    """
    carpeta = PROJECT_ROOT / "tools" / "documentador"
    sys.path.insert(0, str(carpeta))
    try:
        from axioma_doc_base import PROJECT_ROOT as raiz_doc
    finally:
        sys.path.remove(str(carpeta))
    assert Path(raiz_doc).resolve() == carpeta.resolve().parents[1], raiz_doc


def test_la_configuracion_del_usuario_se_carga_sin_importar_la_carpeta_de_trabajo():
    """`.env` relativo al CWD ⇒ la configuración del usuario no se cargaba y no avisaba.

    Medido: arrancando desde otra carpeta, `settings.model_config["env_file"]` era `.env`
    (relativo) y se perdía todo lo configurado.
    """
    from config.settings import settings
    env = Path(settings.model_config["env_file"])
    assert env.is_absolute(), f"el `.env` quedó relativo a la carpeta de trabajo: {env}"
    assert env == PROJECT_ROOT / ".env", env


def test_la_ruta_del_modelo_de_voz_no_depende_de_la_carpeta_de_trabajo():
    """B2 (el real): `piper_model` es relativo, así que se resolvía contra el CWD.

    Medido: `PiperVoice.load()` fallaba arrancando desde otro lado y la voz caía al
    respaldo **sin error visible** (la excepción sólo deja un aviso en el registro).
    """
    from config.multimodal_loader import build_tts_config
    ruta = Path(build_tts_config().piper_model)
    assert ruta.is_absolute(), f"la ruta quedó relativa a la carpeta de trabajo: {ruta}"


@pytest.mark.skipif(not (PROJECT_ROOT / "tools" / "axioma_optimizer.sh").exists(),
                    reason="el optimizador del equipo no se publica (es una herramienta del autor)")
def test_el_guion_del_optimizador_usa_la_raiz_del_proyecto(tmp_path):
    """`ISSUE-147`: asumía `$HOME/Escritorio/axioma` (rompe con el proyecto en `/app`).

    Se ejecuta el guion de verdad, pero con `--print-root`: informa la raíz que va a usar y
    sale antes de crear nada (sin efectos sobre el proyecto).
    """
    import subprocess
    casa = tmp_path / "casa_sin_escritorio"     # HOME sin la carpeta del autor
    casa.mkdir()
    salida = subprocess.run(["bash", str(PROJECT_ROOT / "tools" / "axioma_optimizer.sh"),
                             "--print-root"], capture_output=True, text=True, timeout=60,
                            env={"HOME": str(casa), "PATH": "/usr/bin:/bin"})
    assert salida.returncode == 0, salida.stderr
    assert Path(salida.stdout.strip()).resolve() == PROJECT_ROOT, salida.stdout


def test_no_quedan_rutas_del_autor_en_codigo_ejecutable():
    """Guardia de CLASE: B1/B3 eran rutas absolutas cableadas.

    Recorre el árbol de sintaxis y busca la carpeta del autor en constantes de texto que
    NO son documentación (comentarios y docstrings no cuentan: son ~207 apariciones
    cosméticas, la mayoría encabezados `# Ruta:`). En YAML se ignoran las líneas de
    comentario por el mismo motivo.
    """
    malas = []
    for py in [PROJECT_ROOT / "main.py", *sorted((PROJECT_ROOT / "src").rglob("*.py")),
               *sorted((PROJECT_ROOT / "tools").rglob("*.py"))]:
        nodos = list(ast.walk(ast.parse(py.read_text(encoding="utf-8", errors="ignore"))))
        docs = {id(n.value) for n in nodos if isinstance(n, ast.Expr)}
        malas += [f"{py.relative_to(PROJECT_ROOT)}:{n.lineno}" for n in nodos
                  if isinstance(n, ast.Constant) and "/usuario" in str(n.value)
                  and id(n) not in docs]
    assert malas == [], f"rutas del autor en código que se ejecuta: {malas}"

    def con_ruta(texto):
        return any(("/usuario" in ln or "Escritorio" in ln)
                   and not ln.lstrip().startswith("#") for ln in texto.splitlines())

    yamls = [y.name for y in (PROJECT_ROOT / "config").glob("*.y*ml")
             if con_ruta(y.read_text(encoding="utf-8"))]
    assert yamls == [], f"rutas del autor en configuración: {yamls}"
    # `preparar_publicacion.sh` queda exceptuado a propósito: su trabajo es LIMPIAR la carpeta del
    guiones = [g.name for g in sorted((PROJECT_ROOT / "tools").rglob("*.sh"))
               if g.name != "preparar_publicacion.sh"
               and con_ruta(g.read_text(encoding="utf-8", errors="ignore"))]
    assert guiones == [], f"rutas del autor en guiones: {guiones}"


# ═══════════════════════════════════════════════════════════════


def test_el_catalogo_de_modelos_se_lee_y_es_coherente():
    from config.model_catalog import cargar_catalogo
    catalogo = cargar_catalogo()
    assert catalogo.por_rol, "el catálogo no declara roles"
    for rol, nombre in catalogo.por_rol.items():
        modelo = catalogo.modelo_de_rol(rol)
        assert modelo is not None, f"el rol '{rol}' apunta a '{nombre}' y no está catalogado"
        assert modelo.instalar.strip(), f"'{nombre}' no dice cómo se instala"
        assert modelo.ram_gb > 0 and modelo.disco_gb > 0, f"'{nombre}' sin medidas"
    obligatorios = catalogo.obligatorios()
    assert obligatorios, "el catálogo no marca ningún modelo como obligatorio"
    assert catalogo.modelo_de_rol("no_existe") is None


def test_el_catalogo_coincide_con_los_modelos_configurados():
    """Si alguien cambia el modelo de un rol en la configuración, el catálogo queda viejo.

    Sin esta comprobación el preflight avisaría con los tamaños de OTRO modelo (mentiría
    sobre lo que hay que descargar). Medido: los tres roles coinciden hoy.
    """
    from config.model_catalog import cargar_catalogo
    from config.settings import settings
    catalogo = cargar_catalogo()
    assert catalogo.por_rol["chat"] == settings.llm_default_model
    assert catalogo.por_rol["codigo"] == settings.llm_code_model
    assert catalogo.por_rol["codigo_respaldo"] == settings.llm_code_model_fallback


def test_el_espacio_a_descargar_incluye_el_margen():
    from config.model_catalog import cargar_catalogo
    catalogo = cargar_catalogo()
    faltantes = catalogo.obligatorios()
    esperado = sum(m.disco_gb for m in faltantes) * catalogo.hardware.disco_factor
    assert abs(catalogo.espacio_para_descargar_gb(faltantes) - esperado) < 0.01


def test_un_catalogo_mal_escrito_falla_con_mensaje_claro(tmp_path):
    """Un catálogo inválido NO se degrada en silencio: el preflight mentiría."""
    import pytest
    from config.model_catalog import cargar_catalogo
    malo = tmp_path / "catalogo.yaml"
    cabecera = (
        "version: 1\n"
        "hardware: {ram_minima_gb: 16, ram_margen_gb: 1.5, disco_factor: 1.5}\n"
        "por_rol: {chat: 'qwen3:8b', fantasma: 'no-existe:1b'}\n"
        "modelos:\n"
    )
    # 1) un modelo sin medidas válidas
    malo.write_text(cabecera + "  'qwen3:8b': {roles: [chat], obligatorio: true, tipo: ollama,\n"
                               "    disco_gb: 0, ram_gb: 6.67, para: x, si_falta: y, instalar: z}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="disco_gb"):
        cargar_catalogo(malo, usar_cache=False)
    # 2) un rol que apunta a un modelo que no está en 'modelos'
    malo.write_text(cabecera + "  'qwen3:8b': {roles: [chat], obligatorio: true, tipo: ollama,\n"
                               "    disco_gb: 4.9, ram_gb: 6.67, para: x, si_falta: y, instalar: z}\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="fantasma"):
        cargar_catalogo(malo, usar_cache=False)


# ═══════════════════════════════════════════════════════════════

class _ClienteFalso:
    def __init__(self, vivo=True, modelos=()):
        self.base_url = "http://127.0.0.1:11434"
        self._vivo, self._modelos = vivo, list(modelos)
    def health_check(self):
        return self._vivo
    def list_models(self):
        return [{"name": m} for m in self._modelos]


def _hw(ram_gb=32.0):
    from types import SimpleNamespace
    return SimpleNamespace(ram_gb=ram_gb, gpu_vram_gb=0.0, cpu_threads=6, backend="cpu")


def _revisar(monkeypatch, modelos, libre_gb=500.0, ram_gb=32.0, vivo=True):
    """Corre el preflight con el entorno controlado (disco y audio fijos)."""
    from types import SimpleNamespace
    from src.core import preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=int(libre_gb * 1024 ** 3)))
    monkeypatch.setattr(pf, "_probar_audio",
                        lambda _c: pf.Requisito("audio", "Audio (voz)", pf.OK, "falso"))
    return pf.revisar(cliente=_ClienteFalso(vivo=vivo, modelos=modelos), hw=_hw(ram_gb))


def _todos_los_modelos():
    from config.model_catalog import cargar_catalogo
    return [n for n, m in cargar_catalogo().modelos.items() if m.tipo == "ollama"]


def test_con_todo_presente_el_preflight_dice_que_puede_responder(monkeypatch):
    pre = _revisar(monkeypatch, _todos_los_modelos())
    assert pre.puede_responder(), pre.veredicto()
    assert pre.faltantes() == []
    assert "puede responder" in pre.veredicto()


def test_sin_ollama_no_puede_responder_y_dice_como_instalarlo(monkeypatch):
    pre = _revisar(monkeypatch, [], vivo=False)
    faltan = {r.clave for r in pre.faltantes()}
    assert "ollama" in faltan, faltan
    assert not pre.puede_responder()
    remedio = next(r.remedio for r in pre.faltantes() if r.clave == "ollama")
    assert "ollama serve" in remedio, remedio


def test_si_falta_un_modelo_obligatorio_lo_bloquea_y_dice_el_comando(monkeypatch):
    pre = _revisar(monkeypatch, [])
    faltan = {r.clave for r in pre.faltantes()}
    assert {"modelo_chat", "modelo_codigo"} <= faltan, faltan
    chat = next(r for r in pre.faltantes() if r.clave == "modelo_chat")
    assert "ollama pull qwen3:8b" in chat.remedio, chat.remedio


def test_los_modelos_opcionales_que_faltan_no_bloquean(monkeypatch):
    """El respaldo de código, la visión y la voz son opcionales: avisan, no bloquean."""
    from config.model_catalog import cargar_catalogo
    cat = cargar_catalogo()
    obligatorios = [cat.por_rol[r] for r in ("chat", "codigo")]
    pre = _revisar(monkeypatch, obligatorios)
    assert pre.puede_responder(), pre.veredicto()
    avisos = {r.clave for r in pre.avisos()}
    assert "modelo_codigo_respaldo" in avisos and "modelo_vision" in avisos, avisos
    assert pre.faltantes() == []


def test_sin_espacio_en_disco_lo_dice_antes_de_descargar(monkeypatch):
    pre = _revisar(monkeypatch, [], libre_gb=1.0)
    disco = next((r for r in pre.requisitos if r.clave == "disco"), None)
    assert disco is not None and disco.estado == "falta", disco
    assert disco.datos["necesario_gb"] > disco.datos["libre_gb"]
    assert not pre.puede_responder()


def test_por_debajo_del_piso_de_memoria_lo_bloquea(monkeypatch):
    """El piso de 16 GB es decisión del usuario (sin modo "modelo chico")."""
    pre = _revisar(monkeypatch, _todos_los_modelos(), ram_gb=8.0)
    ram = next(r for r in pre.requisitos if r.clave == "ram")
    assert ram.estado == "falta", ram
    assert not pre.puede_responder()


def test_sin_audio_avisa_pero_no_bloquea(monkeypatch):
    from types import SimpleNamespace
    from src.core import preflight as pf
    monkeypatch.setattr(pf.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(free=500 * 1024 ** 3))
    pre = pf.revisar(cliente=_ClienteFalso(vivo=True, modelos=_todos_los_modelos()),
                     hw=_hw())
    audio = next(r for r in pre.requisitos if r.clave == "audio")
    assert audio.estado in ("ok", "aviso"), audio     # nunca bloquea, en cualquier equipo
    if audio.estado == "aviso":
        assert "chat" in audio.remedio


def test_el_informe_y_el_json_dicen_lo_mismo(monkeypatch, capsys):
    from src.core import preflight as pf
    pre = _revisar(monkeypatch, [])
    monkeypatch.setattr(pf, "revisar", lambda *a, **k: pre)
    assert pf.main(["--json"]) == 4, "sin lo obligatorio el código no puede ser 0"
    datos = json.loads(capsys.readouterr().out)
    assert datos["puede_responder"] is False
    assert "modelo_chat" in datos["faltantes"]
    assert datos["veredicto"] == pre.veredicto()
    assert pf.main([]) == 4
    assert "NO puede responder" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════
_sin_empaquetado = pytest.mark.skipif(
    not (PROJECT_ROOT / "Dockerfile").exists(),
    reason="los archivos de empaquetado no viajan dentro de la imagen (se comprueban en el código)")
# No se puede compilar la imagen sin Docker instalado, pero SÍ se puede comprobar que el

@_sin_empaquetado
def test_el_contexto_de_compilacion_deja_fuera_lo_pesado_y_lo_ajeno():
    texto = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    patrones = [ln.strip() for ln in texto.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    for excluido in ("deepseek-harness/", "venv/", ".git/", "data/", "logs/",
                     ".optimizer_backups/", ".env", "__pycache__/"):
        assert excluido in patrones, f"el contexto de compilación no excluye {excluido}"
    for necesario in ("src/", "config/", "tests/", "tools/"):
        assert necesario not in patrones, (
            f"no se puede excluir {necesario}: la imagen lo necesita para correr (y la suite)")
    # OJO: los paths excluidos (`deepseek-harness/`, `venv/`, `.optimizer_backups/`) son artefactos
    for necesario in ("src", "config", "tests", "tools", "main.py"):
        assert (PROJECT_ROOT / necesario).exists(), f"falta {necesario} en el árbol"


@_sin_empaquetado
def test_la_imagen_es_de_dos_etapas_sin_cuda_y_sin_root():
    df = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert df.count("\nFROM ") >= 2, "la imagen tiene que ser de dos etapas (compilar / ejecutar)"
    assert "download.pytorch.org/whl/cpu" in df, (
        "torch tiene que venir del índice CPU: de PyPI bajaría la versión con CUDA")
    assert "USER axioma" in df, "el proceso no puede quedar corriendo como root"
    assert "src.core.preflight" in df, "la salud del contenedor tiene que ser el preflight"
    assert "COPY data" not in df and "COPY venv" not in df, (
        "los datos y el entorno virtual no van dentro de la imagen")


@_sin_empaquetado
@_sin_compose
def test_el_compose_usa_el_ollama_del_equipo_por_defecto():
    """Decisión del usuario (§5-A): soportar los dos, con el del equipo por defecto."""
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    axioma = comp["services"]["axioma"]
    assert not axioma.get("profiles"), "AXIOMA tiene que arrancar sin necesidad de perfil"
    assert "OLLAMA_HOST" in axioma["environment"], "tiene que poder apuntar al Ollama del equipo"
    assert any(v.startswith("./data") for v in axioma["volumes"]), "los datos van por volumen"
    assert any(v.startswith("./logs") for v in axioma["volumes"]), "los registros van por volumen"
    propio = comp["services"]["ollama"]
    assert propio.get("profiles"), "el Ollama propio tiene que ser opcional (detrás de un perfil)"
    assert any("ollama_models" in v for v in propio["volumes"]), (
        "los modelos del Ollama propio van a un volumen (si no, se rebajan en cada arranque)")
    assert "ollama_models" in comp.get("volumes", {}), "falta declarar el volumen de modelos"


# ═══════════════════════════════════════════════════════════════
_GUIONES_WINDOWS = ("instalar_axioma.ps1", "iniciar_axioma.ps1")


def _codigo_ps1(texto: str) -> str:
    """El guion sin las líneas de comentario.

    Las comprobaciones de comportamiento se hacen sobre esto y no sobre el archivo entero:
    medido con una mutación —al renombrar `[switch]$Detener` quedaba el «-Detener» del comentario
    de uso y la prueba seguía pasando, o sea que no comprobaba nada—.
    """
    return "\n".join(ln for ln in texto.splitlines() if not ln.lstrip().startswith("#"))


def _ps1_balanceado(texto: str) -> bool:
    """Paréntesis, llaves y corchetes balanceados, ignorando comentarios y textos.

    Se prueba a sí misma en la prueba que la usa (con un texto roto a propósito): un verificador
    que nunca falla no verifica nada.
    """
    pares = {")": "(", "}": "{", "]": "["}
    pila = []
    i = 0
    while i < len(texto):
        c = texto[i]
        if c == "`":                       # escape de PowerShell: se saltea el siguiente carácter
            i += 2
            continue
        if c == "#":                       # comentario hasta el fin de línea
            while i < len(texto) and texto[i] != "\n":
                i += 1
            continue
        if c in "'\"":
            cierre = c
            i += 1
            while i < len(texto):
                if texto[i] == "`" and cierre == '"':   # `" dentro de comillas dobles
                    i += 2
                    continue
                if texto[i] == cierre:
                    if cierre == "'" and i + 1 < len(texto) and texto[i + 1] == "'":
                        i += 2                      # en comillas simples se escapa duplicando
                        continue
                    break
                i += 1
            i += 1
            continue
        if c in "({[":
            pila.append(c)
        elif c in pares:
            if not pila or pila.pop() != pares[c]:
                return False
        i += 1
    return not pila


@_sin_empaquetado
def test_los_guiones_de_windows_estan_en_utf8_con_bom_y_sin_emoji():
    for nombre in _GUIONES_WINDOWS:
        ruta = PROJECT_ROOT / "instalar" / nombre
        assert ruta.exists(), f"falta instalar/{nombre} (el usuario de Windows no tiene launcher)"
        crudo = ruta.read_bytes()
        assert crudo.startswith(b"\xef\xbb\xbf"), (
            f"{nombre} tiene que empezar con BOM: sin BOM, PowerShell 5.1 lo lee como ANSI "
            "y los acentos salen mal")
        texto = crudo.decode("utf-8-sig")
        # Permitido: latin-1 (acentos, «»), tipografía (guiones largos) y los recuadros de los
        permitido = [(0x0000, 0x00FF), (0x2000, 0x206F), (0x2500, 0x257F)]
        raros = sorted({ch for ch in texto
                        if not any(a <= ord(ch) <= b for a, b in permitido)})
        assert raros == [], (
            f"{nombre} usa {raros}: la consola de Windows (CP850/CP437) los imprime como «?»")
        assert "/usuario" not in texto and "saintwick" not in texto.lower(), (
            f"{nombre} nombra al autor")


@_sin_empaquetado
@_sin_compose
def test_los_guiones_de_windows_nombran_perfiles_y_servicios_que_existen():
    """En Windows `network_mode: host` no está disponible: el camino es el perfil «puente»."""
    import re
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    perfiles = {p for s in comp["services"].values() for p in (s.get("profiles") or [])}
    for nombre in _GUIONES_WINDOWS:
        texto = (PROJECT_ROOT / "instalar" / nombre).read_bytes().decode("utf-8-sig")
        usados = set(re.findall(r"--profile\s+([A-Za-z0-9_-]+)", texto))
        assert usados <= perfiles, (
            f"{nombre} usa perfiles que no existen en docker-compose.yml: {usados - perfiles}")
        # Se miran sólo las líneas que se ejecutan: los comentarios SÍ pueden (y deben) explicar
        codigo = _codigo_ps1(texto)
        assert "network_mode" not in codigo, (
            f"{nombre} no debe tocar `network_mode`: eso es de compose, no del guion")
    # El que elige el perfil es el lanzador: el instalador sólo arma la imagen y lo llama.
    lanzador = (PROJECT_ROOT / "instalar" / "iniciar_axioma.ps1").read_bytes().decode("utf-8-sig")
    usados = set(re.findall(r"--profile\s+([A-Za-z0-9_-]+)", lanzador))
    assert "puente" in usados, "el lanzador tiene que usar el perfil «puente» en Windows"
    assert "ollama-local" in usados, "tiene que poder levantar el Ollama propio (perfil ollama-local)"


@_sin_empaquetado
def test_el_lanzador_de_windows_hace_lo_mismo_que_el_de_linux():
    texto = _codigo_ps1((PROJECT_ROOT / "instalar" / "iniciar_axioma.ps1").read_bytes().decode("utf-8-sig"))
    # 1) comprueba Docker y, si falta, lo explica (el de Linux sale con el mismo código 4)
    assert "docker info" in texto and "exit 4" in texto, "no comprueba Docker antes de arrancar"
    assert "docker-desktop" in texto, "si falta Docker tiene que decir de dónde bajarlo"
    # 2) decide solo entre el Ollama del equipo y el que trae AXIOMA (misma decisión que Linux)
    assert "/api/tags" in texto and "11434" in texto, "no busca el Ollama del equipo"
    assert "ollama-local" in texto, "no sabe levantar el Ollama propio si no hay ninguno"
    # 3) espera a que responda y abre el navegador
    assert "Start-Process $Url" in texto, "no abre el navegador al terminar"
    # 3.b) nombra el servicio UNO POR UNO. Medido en Linux el 2026-10-06: `--profile puente` a secas
    assert '"axioma-puente"' in texto and "up -d @servicios" in texto, (
        "tiene que arrancar sólo axioma-puente (y ollama si hace falta), no todo el perfil")
    # 4) apagado y ensayo, igual que el guion de Linux (se busca el interruptor Y su uso: si no,
    for interruptor in ("$Detener", "$Ensayo"):
        assert f"[switch]{interruptor}" in texto, f"falta el interruptor {interruptor}"
        assert f"if ({interruptor})" in texto, f"el interruptor {interruptor} no se usa para nada"
    # 5) los llamados a Docker, protegidos. En PowerShell 7.4+ `$PSNativeCommandUseErrorActionPreference`
    assert "try { & docker info *> $null } catch { return $false }" in texto, \
        "Probar-Docker tiene que proteger el llamado a Docker (PowerShell 7.4+)"
    assert "catch { $encendido = $false }" in texto, \
        "encender tiene que tratar el error de Docker como falla propia, no como excepción suelta"
    # Estos NO pueden aparecer ni en los comentarios: son de Linux.
    completo = (PROJECT_ROOT / "instalar" / "iniciar_axioma.ps1").read_bytes().decode("utf-8-sig")
    for ajeno in ("xdg-open", "/dev/snd", "#!/usr/bin/env"):
        assert ajeno not in completo, f"{ajeno} es de Linux y no va en el guion de Windows"


@_sin_empaquetado
def test_el_instalador_de_windows_deja_acceso_directo_y_sintaxis_balanceada():
    texto = _codigo_ps1((PROJECT_ROOT / "instalar" / "instalar_axioma.ps1").read_bytes().decode("utf-8-sig"))
    assert "docker compose build" in texto, "el instalador tiene que armar la imagen"
    assert "iniciar_axioma.ps1" in texto, "el acceso directo tiene que arrancar AXIOMA"
    assert "WScript.Shell" in texto, "los accesos directos se crean con WScript.Shell"
    assert "Programs" in texto and "Desktop" in texto, "tiene que dejar acceso en menú Inicio y Escritorio"
    # Windows recién instalado trae la ejecución de guiones deshabilitada: el acceso directo
    assert "-ExecutionPolicy Bypass" in texto, "el acceso directo no podría ejecutarse"
    # Los llamados a Docker, protegidos (ver el motivo en la prueba del lanzador).
    assert "try { & docker info *> $null } catch { $hayDocker = $false }" in texto, \
        "el chequeo de Docker tiene que protegerse (PowerShell 7.4+)"
    assert "catch { $armado = $false }" in texto, \
        "armar la imagen tiene que tratar el error de Docker como falla propia"
    for nombre in _GUIONES_WINDOWS:
        crudo = (PROJECT_ROOT / "instalar" / nombre).read_bytes().decode("utf-8-sig")
        assert _ps1_balanceado(crudo), f"{nombre} tiene paréntesis o llaves sin cerrar"
    # El verificador tiene que saber decir que NO (si no, la comprobación de arriba no vale nada):
    assert not _ps1_balanceado("Write-Host 'ok'\nif ($true) {\n"), "el verificador no detecta una llave sin cerrar"
    assert not _ps1_balanceado("Write-Host \"texto\" )\n"), "el verificador no detecta un paréntesis de más"
    assert _ps1_balanceado("Write-Host \"un `\" entre comillas)\"  # ) suelto en un comentario"), (
        "el verificador se confunde con los escapes y los comentarios")


# ═══════════════════════════════════════════════════════════════

# Paquetes que están en requirements.txt y NO en el del CI, cada uno con su motivo escrito en el
_OMITIDOS_A_PROPOSITO = {
    "torch",                  # el CI lo instala aparte, desde el índice CPU
    "torchaudio",             # ídem
    "sentence-transformers",  # ídem (arrastra torch)
    "pyatspi",                # necesita las libs de accesibilidad del escritorio
    "openwakeword",           # opcional: la palabra de activación funciona sin él
}


def _leer_requirements(nombre):
    import re
    texto = (PROJECT_ROOT / nombre).read_text(encoding="utf-8")
    return {m.group(1).lower().replace("_", "-"): m.group(2) for m in
            (re.match(r"^([A-Za-z0-9_.\-]+)==([^\s#]+)", ln.strip()) for ln in texto.splitlines()) if m}


def test_requirements_ci_es_requirements_menos_lo_documentado():
    completo = _leer_requirements("requirements.txt")
    ci = _leer_requirements("requirements-ci.txt")
    assert completo, "requirements.txt no declara nada"
    assert ci, "requirements-ci.txt no declara nada"
    solo_ci = sorted(set(ci) - set(completo))
    assert solo_ci == [], (
        f"estos paquetes están SÓLO en el archivo del CI {solo_ci}: el de CI es un subconjunto y "
        "tiene que declararlos también requirements.txt")
    omitidos = set(completo) - set(ci)
    assert omitidos == _OMITIDOS_A_PROPOSITO, (
        f"la diferencia entre los dos archivos cambió: {sorted(omitidos ^ _OMITIDOS_A_PROPOSITO)}. "
        "Si agregaste una dependencia al completo, agregala también al del CI (o justificá la "
        "omisión acá y en el encabezado de requirements-ci.txt)")
    distintas = {n: (completo[n], ci[n]) for n in set(ci) if completo[n] != ci[n]}
    assert distintas == {}, f"misma dependencia con dos versiones: {distintas}"


# ═══════════════════════════════════════════════════════════════

def _hw_simulado(ram_gb=32.0, placa=False, vram_gb=0.0):
    from types import SimpleNamespace
    return SimpleNamespace(ram_gb=ram_gb, gpu_vram_gb=vram_gb, cpu_threads=8, backend="cpu",
                           is_gpu_available=placa, effective_budget_gb=vram_gb * 0.88 if placa else 1.0,
                           gpu_name="simulada" if placa else None)


def test_recomendar_cubre_todos_los_roles_del_catalogo():
    """Si el catálogo agrega un rol, la recomendación no puede olvidarlo."""
    from config.model_catalog import cargar_catalogo
    from src.core.preflight import recomendar
    catalogo = cargar_catalogo()
    datos = recomendar(_hw_simulado(ram_gb=32.0))
    roles = [f["rol"] for f in datos["recomendaciones"]]
    assert set(roles) == set(catalogo.por_rol), f"faltan roles en la recomendación: {roles}"
    for fila in datos["recomendaciones"]:
        assert fila["modelo"] == catalogo.por_rol[fila["rol"]], fila
        assert fila["veredicto"] and isinstance(fila["entra"], bool) and fila["para"], fila
        # Cómo se instala: los de Ollama con `ollama pull`; el de voz (piper) se baja a data/piper/.
        assert fila["instalar"].strip(), fila
        if catalogo.modelo_de_rol(fila["rol"]).tipo == "ollama":
            assert fila["instalar"].startswith("ollama pull"), fila
    # Los obligatorios van primero: son los que deciden si AXIOMA puede arrancar.
    assert [f["obligatorio"] for f in datos["recomendaciones"]] == sorted(
        [f["obligatorio"] for f in datos["recomendaciones"]], reverse=True)
    assert datos["nivel_de_memoria"] == "ideal", datos["nivel_de_memoria"]


def test_recomendar_respeta_el_minimo_medido_del_proyecto():
    """Medido: por debajo de 9 GB no entra el modelo de conversación (catálogo, decisión D-05).

    La cuenta por nombre de parámetros es gruesa y puede decir «entra justo»: manda el mínimo medido,
    no la estimación. Sin esto, un equipo de 8 GB recibía un «sí, pero justo» que en la práctica no anda.
    """
    from config.model_catalog import cargar_catalogo
    from src.core.preflight import recomendar
    minimo = cargar_catalogo().hardware.ram_para_funcionar_gb
    datos = recomendar(_hw_simulado(ram_gb=minimo - 1))
    assert datos["nivel_de_memoria"] == "no_alcanza", datos["nivel_de_memoria"]
    obligatorios = {f["modelo"] for f in datos["recomendaciones"] if f["obligatorio"]}
    assert set(datos["obligatorios_que_no_entran"]) == obligatorios, (
        "con menos memoria que el mínimo, los modelos obligatorios tienen que quedar marcados")
    for fila in datos["recomendaciones"]:
        if fila["obligatorio"]:
            assert "mínimo medido" in fila["aviso"], fila
            assert fila["entra"] is False, fila
    # Y con el mínimo justo, ya no se marca: la frontera se respeta en los dos sentidos.
    datos_ok = recomendar(_hw_simulado(ram_gb=minimo))
    assert datos_ok["nivel_de_memoria"] != "no_alcanza", datos_ok["nivel_de_memoria"]


def test_el_catalogo_no_deja_texto_crudo_a_la_vista():
    """Medido el 2026-10-06: el texto del respaldo estaba escrito como concatenación de Python
    (`para: ("…" "…")`), que en YAML no existe: la pantalla mostraba los paréntesis y las comillas.
    """
    import yaml
    crudo = (PROJECT_ROOT / "config" / "model_catalog.yaml").read_text(encoding="utf-8")
    for linea in crudo.splitlines():
        limpia = linea.strip()
        if limpia.startswith(("para:", "si_falta:")):
            valor = limpia.split(":", 1)[1].strip()
            assert not valor.startswith(('("', "('")), f"texto crudo en el catálogo: {limpia}"
    datos = yaml.safe_load(crudo)
    for nombre, modelo in datos["modelos"].items():
        for campo in ("para", "si_falta"):
            texto = str(modelo[campo])
            assert '" "' not in texto and not texto.startswith(('("', "('")), (
                f"'{nombre}': {campo} quedó con comillas y paréntesis a la vista: {texto!r}")


def test_la_orden_recomendar_y_la_orden_modelo_contestan_bien(capsys):
    """Las dos formas de preguntar desde la consola, con `--json` (lo que usa un instalador)."""
    import json as _json
    from src.core import preflight

    assert preflight.main(["--recomendar", "--json"]) in (0, 4)
    datos = _json.loads(capsys.readouterr().out)
    assert datos["recomendaciones"] and "nivel_de_memoria" in datos and "nota" in datos

    # Un modelo que NO está en el catálogo y no entra en ninguna PC de este mundo.
    codigo = preflight.main(["--modelo", "llama3:70b", "--json"])
    evaluacion = _json.loads(capsys.readouterr().out)
    assert codigo == 4, "un modelo que no entra tiene que salir con 4"
    assert evaluacion["modelo"] == "llama3:70b"
    assert evaluacion["nivel"] in ("no_fit", "marginal", "too_tight", "desconocido"), evaluacion


@_sin_empaquetado
def test_el_ci_prueba_los_instaladores_en_un_windows_de_verdad():
    """En el equipo del autor no hay Windows, así que la prueba REAL tiene que vivir en el CI.

    El comprobador (`tests/probar_instaladores_windows.ps1`) corre en un runner `windows-latest` con
    PowerShell 5.1 y con PowerShell 7. Acá se vigila que exista, que tenga los mismos cuidados que los
    guiones (BOM: sin él, PowerShell 5.1 lee ANSI y los acentos salen mal) y que el CI lo siga llamando
    con los dos intérpretes.
    """
    comprobador = PROJECT_ROOT / "tests" / "probar_instaladores_windows.ps1"
    assert comprobador.exists(), "falta el comprobador que corre el CI en Windows"
    crudo = comprobador.read_bytes()
    assert crudo.startswith(b"\xef\xbb\xbf"), "el comprobador también necesita BOM"
    texto = crudo.decode("utf-8-sig")
    permitido = [(0x0000, 0x00FF), (0x2000, 0x206F), (0x2500, 0x257F)]
    raros = sorted({ch for ch in texto if not any(a <= ord(ch) <= b for a, b in permitido)})
    assert raros == [], f"el comprobador usa {raros}: la consola de Windows los imprime mal"
    # Tiene que ejecutar los guiones de verdad (no sólo leerlos).
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File' in texto, \
        "el comprobador tiene que ejecutar los guiones como lo hace el acceso directo"
    for esperado in ("-Ensayo", "Docker Desktop no está disponible", "axioma-puente"):
        assert esperado in texto, f"el comprobador no comprueba: {esperado}"

    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "instaladores-windows" in ci and "windows-latest" in ci, \
        "el CI dejó de probar los instaladores de Windows en un Windows real"
    assert "probar_instaladores_windows.ps1" in ci, "el trabajo de Windows no llama al comprobador"
    for interprete in ("shell: powershell", "shell: pwsh"):
        assert interprete in ci, f"el CI no prueba con «{interprete}» (5.1 y 7 son los que hay en la calle)"
    # MEDIDO el 2026-10-06: con `shell: ${{ matrix.shell }}` GitHub NO valida el workflow entero: la
    solo_ejecutable = "\n".join(l for l in ci.splitlines() if not l.lstrip().startswith("#"))
    assert "shell: ${{" not in solo_ejecutable, \
        "una expresión en `shell:` rompe la validación de TODO el workflow"


@_sin_empaquetado
def test_los_guiones_de_powershell_no_usan_variable_dos_puntos():
    """`"$nombre:"` NO es válido en PowerShell: lee los dos puntos como una unidad y el guion **no
    arranca** (error de parseo, no de ejecución).

    Medido el 2026-10-06: lo marcó el trabajo de Windows del CI, con el archivo publicado:
        Variable reference is not valid. ':' was not followed by a valid variable name character.
    Había pasado mi comprobación de llaves balanceadas porque no es un problema de llaves. Esta guarda
    es un aviso temprano (barato, sin Windows); la prueba de verdad sigue siendo el CI, que ejecuta el
    guion con PowerShell de verdad. `${nombre}:` sí es válido, y los ámbitos conocidos (`$env:PATH`,
    `$script:x`, `$global:x`, `$using:x`) también.
    """
    import re
    ambitos = {"env", "script", "global", "local", "private", "using", "variable", "function", "workflow"}
    sospechosos = []
    for guion in (sorted((PROJECT_ROOT / "instalar").glob("*.ps1"))
                  + sorted((PROJECT_ROOT / "tests").glob("*.ps1"))):
        for numero, linea in enumerate(guion.read_text(encoding="utf-8-sig").splitlines(), 1):
            if linea.lstrip().startswith("#"):
                continue                       # los comentarios pueden explicar el error
            for encontrado in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*):", linea):
                if encontrado.group(1).lower() not in ambitos:
                    sospechosos.append(f"{guion.name}:{numero} → {encontrado.group(0)}")
    assert sospechosos == [], (
        "en PowerShell `$var:` es un error de PARSEO (el guion no arranca): usá `${var}:` → "
        + ", ".join(sospechosos))


@_sin_empaquetado
def test_el_ci_ejecuta_los_instaladores_de_linux():
    """Los de Linux se prueban igual que los de Windows: **ejecutándolos**, no leyéndolos.

    Pedido del usuario (2026-10-06): «no sólo debemos saber si en Windows funciona, sino también en
    Linux». En modo ensayo no tocan nada, pero recorren el camino real: comprobar Docker, decidir entre
    el Ollama del equipo y el propio, armar el plan y decir a qué dirección abriría el navegador.
    """
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "instaladores-linux" in ci and "ubuntu-latest" in ci, \
        "el CI dejó de ejecutar los instaladores de Linux"
    for guion in ("instalar/instalar_axioma.sh --dry-run", "iniciar_axioma.sh --dry-run"):
        assert guion in ci, f"el CI no ejecuta: {guion}"
    for comprobacion in ("docker compose build", "abriría http://127.0.0.1:8080",
                         "Ya tenés Ollama|Levanto el Ollama",
                         "Docker no pudo encender AXIOMA"):   # el camino de error, con un docker falso
        assert comprobacion in ci, f"el CI no comprueba: {comprobacion}"

    # Y el lanzador de Linux tiene que MIRAR el código de Docker, como el de Windows: si el contenedor
    lanzador = (PROJECT_ROOT / "instalar" / "iniciar_axioma.sh").read_text(encoding="utf-8")
    assert "if ! docker compose" in lanzador, "el lanzador de Linux no comprueba si Docker pudo encender"
    assert "Docker no pudo encender AXIOMA" in lanzador, "no explica el fallo en castellano"


@_sin_empaquetado
def test_el_paso_de_librerias_de_audio_no_puede_tumbar_la_corrida():
    """Medido el 2026-10-07: un `apt-get` colgado hizo que GitHub **cancelara** el trabajo; el paso
    prometía tolerar fallas, pero sólo toleraba que el comando devolviera error, no que se colgara.
    Y como el portero corre con `if: always()`, falló después diciendo «sin reporte consolidado», que no
    explica nada. Ahora el paso tiene límite de tiempo y `continue-on-error`, y el mensaje del portero
    dice que el reporte puede faltar porque se canceló el paso.
    """
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    bloque = ci.split("Instalar librerías de sistema (audio)", 1)
    assert len(bloque) == 2, "el paso de librerías de audio desapareció del CI"
    encabezado = bloque[1].split("run:", 1)[0]
    assert "timeout-minutes:" in encabezado, "el paso puede volver a colgarse y tumbar la corrida"
    assert "continue-on-error: true" in encabezado, \
        "si el paso se pasa de tiempo, la corrida entera no puede perderse"

    portero = (PROJECT_ROOT / "tools" / "quality_gate.py").read_text(encoding="utf-8")
    assert "o el paso se canceló antes" in portero, \
        "el portero tiene que explicar por qué puede faltar el reporte de la suite"


@_sin_empaquetado
def test_la_imagen_y_el_ci_usan_la_misma_version_de_python():
    """Decisión del usuario (2026-10-07): UNA sola versión, **3.14** — la del desarrollo y la suya.

    Antes la imagen quedaba en 3.12 y el desarrollo corría en 3.14: dos entornos distintos, que es
    justamente donde aparecen las sorpresas («en mi máquina anda»). Y el CI probaba las dos, con
    `continue-on-error` en 3.14: un rojo que no se veía. Se descartó 3.12 porque cuidarlo es trabajo
    perdido.
    """
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG PYTHON_VERSION=3.14" in dockerfile, \
        "la imagen tiene que usar la MISMA versión que el desarrollo (3.14)"
    assert "ARG PYTHON_VERSION=3.12" not in dockerfile, "la imagen volvió a 3.12"

    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python-version: ['3.14']" in ci, "el CI tiene que probar la versión que se usa de verdad"
    assert "'3.12'" not in ci, "3.12 se descartó: el CI no puede volver a probarlo"
    assert "continue-on-error: ${{" not in ci, "sin segunda versión no hay que tolerar fallos"


# ═══════════════════════════════════════════════════════════════

@_sin_compose
def test_el_compose_corre_con_el_usuario_del_equipo():
    """`user:` en los servicios de la app, con los valores que calcula el lanzador."""
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for servicio in ("axioma", "axioma-puente", "voz"):
        usuario = comp["services"][servicio].get("user")
        assert usuario == "${AXIOMA_UID:-1000}:${AXIOMA_GID:-1000}", \
            f"el servicio `{servicio}` tiene que correr con el UID/GID del equipo, y tiene {usuario!r}"
    assert "user" not in (comp["services"]["ollama"] or {}), \
        "el Ollama propio es una imagen ajena: no se le cambia el usuario"


@_sin_empaquetado
def test_la_imagen_deja_escribir_y_leer_con_cualquier_uid():
    """La imagen tiene que servir para el UID de cualquier equipo (medido con `--user 1001`)."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "chmod -R 0777 data logs cache" in dockerfile, \
        "las carpetas donde AXIOMA escribe tienen que ser escribibles por el UID del equipo"
    assert "chmod -R a+rX /app" in dockerfile, \
        "el código tiene que ser legible con cualquier UID (medido: 1622 archivos en 600 rompían el import)"
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile, \
        "Python no tiene que intentar escribir `__pycache__` en el código"


def test_el_codigo_que_va_a_la_imagen_es_legible_para_cualquiera():
    """Guarda temprana del defecto medido: un archivo en modo 600 rompe el contenedor con otro UID.

    La herramienta del autor escribe archivos con permisos sólo para su usuario; si eso entra a la
    imagen, el contenedor que corre con el UID del equipo no puede ni importar la configuración.
    """
    import os
    ilegibles = []
    for carpeta in ("src", "config", "tools", "tests", "instalar"):
        for archivo in sorted((PROJECT_ROOT / carpeta).rglob("*")):
            if archivo.is_file() and not archivo.is_symlink():
                if os.stat(archivo).st_mode & 0o044 != 0o044:
                    ilegibles.append(str(archivo.relative_to(PROJECT_ROOT)))
    assert ilegibles == [], (
        "estos archivos sólo los puede leer su dueño y romperían el contenedor con otro UID: "
        + ", ".join(ilegibles[:5]))


@_sin_empaquetado
def test_el_demonio_de_voz_de_windows_es_nativo_y_habla_con_el_contenedor():
    """Decisión del usuario (2026-10-08): en Windows la voz va en un **demonio nativo** que le manda el
    texto al AXIOMA del contenedor.

    Por qué así (medido el 2026-10-08): Docker Desktop en Windows corre sobre WSL 2, que **no** expone el
    micrófono ni el servidor de sonido (el servicio `voz` del compose no puede funcionar ahí). La
    alternativa a este guion sería pedirle al usuario que instale Python y sus dependencias en Windows,
    justo lo que el proyecto evita. Usa la voz que **ya trae Windows** (`System.Speech`).

    Y la ruta del endpoint: MEDIDA contra el AXIOMA andando, es `/api/api/v1/chat` porque el router tiene
    prefijo `/api/v1` y además se monta bajo `/api` (queda duplicado). El guion prueba las dos rutas, así
    sigue funcionando el día que se arregle el prefijo.
    """
    guion = PROJECT_ROOT / "instalar" / "rafael_windows.ps1"
    assert guion.exists(), "falta el demonio de voz para Windows"
    crudo = guion.read_bytes()
    assert crudo.startswith(b"\xef\xbb\xbf"), "los .ps1 necesitan BOM (PowerShell 5.1 y los acentos)"
    texto = crudo.decode("utf-8-sig")

    # Nativo: la voz de Windows, sin instalar nada.
    assert "System.Speech" in texto, "tiene que usar la voz nativa de Windows"
    for prohibido in ("import sounddevice", "pip install", "python "):
        assert prohibido not in texto, f"no puede depender de {prohibido!r}: tiene que ser nativo"

    # Las dos rutas del endpoint (la real medida y la que corresponde si se arregla el prefijo).
    assert '"/api/v1/chat", "/api/api/v1/chat"' in texto, \
        "tiene que probar las dos rutas del endpoint de chat"
    # Y el modo ensayo, para poder comprobarlo sin micrófono.
    assert "-Ensayo" in texto, "tiene que poder comprobarse en seco"

    # El CI de Windows lo ejecuta de verdad.
    comprobador = (PROJECT_ROOT / "tests" / "probar_instaladores_windows.ps1").read_bytes().decode("utf-8-sig")
    assert "rafael_windows.ps1" in comprobador, \
        "el comprobador que corre el CI en Windows tiene que validar también este guion"
    assert "System.Speech" in comprobador, "el comprobador tiene que verificar que use la voz nativa"


@_sin_empaquetado
def test_el_guion_de_maquina_limpia_no_puede_tocar_la_instalacion_del_usuario():
    """Pedido del usuario (2026-10-08): probar la instalación en una copia limpia **sin tocar** la suya.

    Lo que se exige (cada cosa es una forma de pisar la instalación que ya funciona):
      · trabajar en una carpeta TEMPORAL y borrarla al final;
      · usar OTRO nombre de imagen, OTRO nombre de contenedor y OTRO puerto (el 8080 es el del usuario);
      · tomar una FOTO de la instalación del usuario antes y después y COMPARARLA (y fallar si cambió).
    """
    guion = PROJECT_ROOT / "tools" / "probar_instalacion_limpia.sh"
    assert guion.exists(), "falta el guion de prueba en máquina limpia"
    fuente = guion.read_text(encoding="utf-8")

    assert "mktemp -d" in fuente and "rm -rf \"$DESTINO\"" in fuente, \
        "tiene que trabajar en una carpeta temporal y borrarla"
    assert "axioma-limpia:prueba" in fuente, "tiene que usar OTRO nombre de imagen (no pisar axioma:local)"
    assert "axioma-prueba" in fuente, "tiene que usar OTRO nombre de contenedor"
    assert 'PUERTO_AJENO="${AXIOMA_PUERTO_PRUEBA:-8099}"' in fuente, \
        "tiene que encender en OTRO puerto (el 8080 es el del usuario)"
    assert "ANTES=" in fuente and "DESPUES=" in fuente and '[ "$ANTES" = "$DESPUES" ]' in fuente, \
        "tiene que comparar la instalación del usuario antes y después"
    assert "'--exclude'" in fuente or "--exclude" in fuente, "tiene que copiar sin el entorno del usuario"
    for excluido in ("venv/", "data/", "logs/", ".env", ".git/"):
        assert excluido in fuente, f"la copia tiene que excluir {excluido}"
    assert "--solo-plan" in fuente, "tiene que poder mostrarse el plan sin tocar nada"


@_sin_empaquetado
@_sin_compose
def test_el_modelo_de_la_memoria_no_se_vuelve_a_bajar():
    """MEDIDO el 2026-10-08: `BAAI/bge-m3` ocupa **4,3 GB** y su caché quedaba DENTRO del contenedor, así
    que se volvía a bajar en cada recreación (y el primer arranque tardaba minutos: la copia limpia no
    respondía en un minuto). Ahora va a `cache/`, que es volumen del equipo.
    """
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    axioma = comp["services"]["axioma"]
    assert any("cache" in v and "/app/cache" in v for v in axioma["volumes"]), \
        "falta el volumen de `cache/`: el modelo de la memoria se bajaría de nuevo cada vez"
    assert axioma["environment"].get("HF_HOME") == "/app/cache/huggingface", \
        "la caché de HuggingFace tiene que apuntar a `cache/` (que es volumen)"
    assert "HF_HOME" in (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8"), \
        "la imagen también tiene que saberlo, por si se corre sin compose"


def test_los_guiones_crean_las_carpetas_antes_de_encender():
    """MEDIDO el 2026-10-08 con el guion de máquina limpia: en una instalación NUEVA, `data/` y `logs/` no
    existen; Docker las crea como `root` al montarlas, y como el contenedor corre con el UID del equipo
    (fase 5) no puede escribir → `PermissionError: /app/data/memory` y AXIOMA no arranca.
    Los dos guiones tienen que crearlas ANTES de encender, así quedan del usuario.
    """
    for guion in ("instalar/instalar_axioma.sh", "instalar/iniciar_axioma.sh"):
        fuente = (PROJECT_ROOT / guion).read_text(encoding="utf-8")
        assert "mkdir -p data logs cache" in fuente, \
            f"{guion} no crea data/logs/cache antes de encender (en una máquina nueva no arrancaría)"


# ⚠️ Candado de diseño: las dos máquinas de estado NO son duplicados (ver DECISIONES D-33).
def test_las_dos_maquinas_de_estado_son_distintas_y_no_se_unifican():
    """MEDIDO el 2026-10-09: hay dos `TaskStatus` con el mismo nombre viejo y **no** son la misma cosa.

    - `BoardTaskStatus` (tablero de coordinación): `WAITING_MODEL`, `WAITING_RESULT`, `REFUSED_OOM`…
    - `QueueTaskStatus` (cola de trabajo): `COMPLETED`, `CANCELLED`, `TIMEOUT`…

    Se renombraron a propósito para que nadie (persona o modelo) vuelva a proponer «unificarlas»: unificar
    forzaría la unión de los estados y cada consumidor tendría que defenderse de estados imposibles.
    Esta prueba es el candado: si alguien las iguala, falla.
    """
    from src.core.task_board import BoardTaskStatus
    from src.core.task_queue import QueueTaskStatus

    assert BoardTaskStatus is not QueueTaskStatus, "son dos clases distintas, no la misma"

    def estados(clase):
        """Los nombres de estado, sea una clase simple (tablero) o un Enum (cola).

        MEDIDO al escribir esta prueba: el tablero es una clase con atributos de texto y la cola es un
        `Enum`, así que no se pueden recorrer igual. Asumirlo fue mi primer error acá.
        """
        if hasattr(clase, "__members__"):
            return set(clase.__members__)
        return {k for k in vars(clase) if k.isupper()}

    del_tablero = estados(BoardTaskStatus)
    de_la_cola = estados(QueueTaskStatus)
    assert del_tablero, "el tablero tiene que declarar estados"
    assert de_la_cola, "la cola tiene que declarar estados"
    assert del_tablero - de_la_cola, (
        "el tablero tiene estados que la cola no tiene (WAITING_MODEL/WAITING_RESULT/REFUSED_OOM): "
        f"{sorted(del_tablero)}")
    assert de_la_cola - del_tablero, (
        f"la cola tiene estados que el tablero no tiene: {sorted(de_la_cola)}")
