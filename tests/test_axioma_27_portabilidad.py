#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral (archivo 19/24)
# Sección 19: CONTENEDORES — Fase 0 (portabilidad) y Fase 1 (catálogo de modelos) del
# plan `docs/PLAN_CONTENEDORES.md`
# ═══════════════════════════════════════════════════════════════
# Estos chequeos existen porque el proyecto sólo se había usado en la carpeta del autor:
# lo que sigue se rompía en otra PC o dentro de un contenedor y **no se notaba acá**
# (las rutas existían). Ver `docs/ISSUE-147` en `docs/ISSUES_HISTORY.md`.
#
# Terminología del plan: B1 = raíz del proyecto, B2 = modelo de voz, B3 = documentador,
# B5 = documentación de uso, B6 = supuestos del equipo anfitrión.
import ast
import json
import sys

import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
    # autor las dos coinciden y con la comparación débil la prueba no cazaba la mutación.
    assert Path(main.PROJECT_ROOT).resolve() == Path(main.__file__).resolve().parent, \
        main.PROJECT_ROOT
    assert Path(main.PROJECT_ROOT).is_dir()


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
    # autor en la copia que se publica, así que necesita nombrarla (es el único que puede).
    guiones = [g.name for g in sorted((PROJECT_ROOT / "tools").rglob("*.sh"))
               if g.name != "preparar_publicacion.sh"
               and con_ruta(g.read_text(encoding="utf-8", errors="ignore"))]
    assert guiones == [], f"rutas del autor en guiones: {guiones}"


# ═══════════════════════════════════════════════════════════════
# FASE 1 — catálogo de modelos por rol (qué hay que instalar)
# ═══════════════════════════════════════════════════════════════
# Es la lista que alimenta al preflight: sin ella el sistema no puede decir qué falta
# antes de arrancar. La prueba cubre que se lea, que sea coherente con lo configurado y
# que un catálogo mal escrito FALLE (en vez de que el preflight mienta).


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
# FASE 1 — preflight: ¿qué le falta a este equipo para funcionar?
# ═══════════════════════════════════════════════════════════════
# Pedido del usuario: que el sistema pueda decir qué hay que instalar ANTES de arrancar.
# La lección de `ISSUE-146` se prueba acá como propiedad: si falta algo obligatorio, el
# veredicto NO puede decir que puede responder.

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
# FASE 1 — empaquetado: contexto de compilación, imagen y compose
# ═══════════════════════════════════════════════════════════════
# Miran el ÁRBOL DE CÓDIGO (`.dockerignore`, `Dockerfile`, compose), que NO entra en la imagen:
# dentro del contenedor se saltean con el motivo dicho.
_sin_empaquetado = pytest.mark.skipif(
    not (PROJECT_ROOT / "Dockerfile").exists(),
    reason="los archivos de empaquetado no viajan dentro de la imagen (se comprueban en el código)")
# No se puede compilar la imagen sin Docker instalado, pero SÍ se puede comprobar que el
# empaquetado no arrastre lo que no debe (medido: `deepseek-harness` 1,9 GB y `venv` 11 GB
# entrarían al contexto; `.optimizer_backups` tiene archivos de `root` que ni se pueden copiar).

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
    # LOCALES de la máquina donde se desarrolla: en un clon limpio (el CI) no existen, y exigir que
    # estén hacía fallar el CI siempre. Lo que sí tiene que existir es lo que la imagen NECESITA.
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
