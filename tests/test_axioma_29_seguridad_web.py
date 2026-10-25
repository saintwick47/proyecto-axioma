#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# Sección 29: SEGURIDAD DE LA INTERFAZ WEB (2026-10-07)
# ═══════════════════════════════════════════════════════════════
# De dónde salen estas pruebas: el 2026-10-07 se midió que la interfaz escuchaba en `0.0.0.0:8080`
# (visible desde toda la red local) y que la clave con la que se firman las sesiones era una constante
# escrita en el código (`axioma_secret_key_change_in_production`, en `app.py` y `server.py`). Con esas
# dos cosas juntas, cualquiera en la misma red podía abrir AXIOMA y firmarse una sesión propia.
#
# Lo que se prueba acá es el COMPORTAMIENTO (no el texto): que el secreto sea propio del equipo, que se
# conserve entre arranques, que se guarde sólo para el usuario y que el compose no vuelva a exponer la
# interfaz sin querer.
import hashlib
import os
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONSTANTE_VIEJA = "axioma_secret_key_change_in_production"

# Los archivos de empaquetado no viajan dentro de la imagen: estas pruebas se saltean ahí.
_sin_empaquetado = pytest.mark.skipif(
    not (PROJECT_ROOT / "docker-compose.yml").exists(),
    reason="el empaquetado no viaja dentro de la imagen (se comprueba en el código)")


def _modulo():
    from src.interfaces.web.sesion_segura import secreto_de_sesion
    return secreto_de_sesion


def test_el_secreto_de_sesion_es_propio_del_equipo_y_se_conserva(tmp_path):
    """Un secreto por equipo, guardado sólo para tu usuario, igual entre arranques.

    Si cambiara en cada arranque, cada reinicio echaría a todas las sesiones abiertas; si fuera el mismo
    en todos los equipos (como la constante vieja), una cookie forjada en uno serviría en otro.
    """
    archivo = tmp_path / "data" / ".storage_secret"
    secreto = _modulo()(ruta=archivo)
    assert secreto != CONSTANTE_VIEJA, "el secreto no puede ser la constante que estaba en el código"
    assert len(secreto) >= 32, f"secreto demasiado corto: {len(secreto)} caracteres"
    assert re.fullmatch(r"[0-9a-f]+", secreto), "el secreto tiene que ser aleatorio, no un texto elegido"
    assert archivo.exists(), "el secreto tiene que quedar guardado"

    modo = os.stat(archivo).st_mode & 0o777
    assert modo == 0o600, f"el secreto tiene que ser sólo para tu usuario, y quedó en {oct(modo)}"

    # Segundo arranque: MISMO secreto (las sesiones no se caen al reiniciar).
    assert _modulo()(ruta=archivo) == secreto, "el secreto cambió entre dos arranques"

    # Dos equipos distintos (dos archivos distintos) → dos secretos distintos.
    assert _modulo()(ruta=tmp_path / "otro" / ".storage_secret") != secreto


def test_la_variable_de_entorno_manda_y_lo_corto_se_ignora(tmp_path):
    """Quien quiera gestionar el secreto puede hacerlo por entorno; lo que no vale es un secreto corto."""
    archivo = tmp_path / ".storage_secret"
    elegido = "a" * 48
    assert _modulo()(ruta=archivo, entorno={"AXIOMA_STORAGE_SECRET": elegido}) == elegido
    assert not archivo.exists(), "si viene por entorno, no hace falta escribir el archivo"

    # Una variable demasiado corta no se usa (y el archivo se genera igual).
    corto = _modulo()(ruta=archivo, entorno={"AXIOMA_STORAGE_SECRET": "corto"})
    assert corto != "corto" and len(corto) >= 32, corto


def test_un_archivo_incompleto_se_regenera(tmp_path):
    """Medido: un archivo a medio escribir (corte de luz, disco lleno) no puede dejar sesiones sin firma."""
    archivo = tmp_path / ".storage_secret"
    archivo.write_text("a-medio", encoding="utf-8")
    secreto = _modulo()(ruta=archivo)
    assert len(secreto) >= 32
    assert archivo.read_text(encoding="utf-8").strip() == secreto, "el archivo tiene que quedar corregido"


def test_la_constante_vieja_no_volvio_al_codigo():
    """La guarda del defecto: nadie puede reintroducir la constante a mano."""
    culpables = []
    for archivo in [PROJECT_ROOT / "src" / "interfaces" / "web" / "app.py",
                    PROJECT_ROOT / "src" / "interfaces" / "web" / "server.py"]:
        texto = archivo.read_text(encoding="utf-8")
        if re.search(r"storage_secret\s*=\s*['\"]" + CONSTANTE_VIEJA, texto):
            culpables.append(str(archivo.relative_to(PROJECT_ROOT)))
    assert culpables == [], f"volvió el secreto fijo en el código: {culpables}"

    # Y los dos lugares siguen usando la función (no un valor escrito a mano).
    for archivo in [PROJECT_ROOT / "src" / "interfaces" / "web" / "app.py",
                    PROJECT_ROOT / "src" / "interfaces" / "web" / "server.py"]:
        assert "secreto_de_sesion()" in archivo.read_text(encoding="utf-8"), \
            f"{archivo.name} dejó de usar el secreto del equipo"


@pytest.mark.skipif(not (PROJECT_ROOT / "docker-compose.yml").exists(),
                    reason="el empaquetado no viaja dentro de la imagen: se comprueba en el código")
def test_el_compose_no_expone_la_interfaz_a_toda_la_red():
    """Medido el 2026-10-07 (`ss -ltn`): la interfaz escuchaba en `0.0.0.0:8080` sin autenticación.

    Dos cosas tienen que seguir así:
      · el servicio con red del equipo (`axioma`) escucha en la dirección LOCAL por defecto;
      · el servicio de Docker Desktop (`puente`) publica el puerto SÓLO en la dirección local
        (adentro del contenedor tiene que escuchar en `0.0.0.0`, que es lo que Docker exige para poder
        publicar un puerto; hacia afuera, no).
    """
    crudo = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    comp = yaml.safe_load(crudo)
    axioma = comp["services"]["axioma"]

    comando = axioma.get("command") or []
    texto_comando = " ".join(str(p) for p in comando)
    assert "--host" in texto_comando, "el servicio `axioma` tiene que fijar el host del servidor web"
    assert "${AXIOMA_WEB_HOST:-127.0.0.1}" in texto_comando, (
        "por defecto tiene que escuchar en 127.0.0.1 (para exponerlo hay que pedirlo a propósito): "
        f"{texto_comando}")
    assert comando.count("0.0.0.0") == 0, "no puede quedar un `0.0.0.0` fijo en el comando"

    puertos = comp["services"]["axioma-puente"]["ports"]
    assert any(str(p).startswith("127.0.0.1:") for p in puertos), (
        f"el perfil `puente` tiene que publicar el puerto sólo en la dirección local: {puertos}")
    assert "0.0.0.0" not in crudo.split("axioma-puente")[1][:400], \
        "el perfil `puente` no puede publicar en todas las interfaces"


def test_la_configuracion_de_la_web_se_lee_del_entorno_no_del_codigo(tmp_path):
    """Lo configurable (host y secreto) entra por entorno/archivo, nunca escrito en el código."""
    ajustes = (PROJECT_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    assert "AXIOMA_STORAGE_SECRET" in ajustes or "storage_secret" not in ajustes, \
        "si el proyecto gestiona el secreto, tiene que poder leerse del entorno"
    # El archivo del secreto no puede quedar versionado (es de cada equipo).
    ignorados = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".storage_secret" in ignorados or "data/" in ignorados, \
        "el secreto del equipo no puede terminar en el repositorio"


def test_la_direccion_publica_de_la_api_no_se_duplica():
    """MEDIDO el 2026-10-08 contra el servidor andando: la API quedaba en `/api/api/v1/chat`.

    El router tenía prefijo `/api/v1` y además se monta bajo `/api`, así que la dirección se duplicaba:
    `/api/v1/chat` daba **404** (la que documenta el README y la que espera cualquiera) y
    `/api/api/v1/chat` daba 200. La auto-comprobación del arranque no lo veía porque comparaba contra
    las rutas del ROUTER, que siempre existen. Ahora la dirección pública es `/api/v1/...`.
    """
    from src.interfaces.web.routes import router
    fuente_servidor = (PROJECT_ROOT / "src" / "interfaces" / "web" / "server.py").read_text(encoding="utf-8")

    # El router no puede volver a llevar el `/api` que ya pone el montaje.
    assert router.prefix == "/v1", (
        f"el prefijo del router tiene que ser `/v1` (el `/api` lo pone el montaje), y es {router.prefix!r}")
    assert "mount('/api'" in fuente_servidor, "el montaje de la API cambió de lugar: revisá esta prueba"

    # La dirección pública es la esperada y no está duplicada.
    publicas = [f"/api{getattr(r, 'path', '')}" for r in router.routes]
    assert "/api/v1/health" in publicas and "/api/v1/chat" in publicas, publicas[:10]
    assert not [p for p in publicas if p.startswith("/api/api/")], \
        f"la dirección pública volvió a duplicarse: {[p for p in publicas if p.startswith('/api/api/')]}"

    # Y la comprobación del arranque mira la dirección PÚBLICA (antes miraba el router y no lo veía).
    assert 'critical_paths = [f"{montaje}/v1/health", f"{montaje}/v1/chat"]' in fuente_servidor, \
        "la comprobación de endpoints críticos tiene que usar la dirección pública"


def test_las_rutas_documentadas_existen_de_verdad():
    """Vigilancia del prefijo de la API (D-28): lo que dice la documentación tiene que existir.

    MEDIDO el 2026-10-08: `COMANDOS_AXIOMA.txt` documentaba `/api/api/v1/...` (la dirección duplicada) y
    el README decía `/api/v1/...` (la rota entonces). Ahora se comprueba que **cada ruta que nombran los
    documentos exista de verdad** y que nadie vuelva a documentar la duplicada.
    """
    import re
    # MEDIDO al escribir esta prueba: las rutas de `routes_extended`, `routes_hwfit` y `routes_preflight`
    # se registran AL IMPORTAR sus módulos (el arranque de la app los importa). Si no se importan, la
    # lista queda incompleta y la prueba marca como «faltantes» rutas que sí existen.
    from src.interfaces.web import routes, routes_extended, routes_hwfit, routes_preflight  # noqa: F401
    from src.interfaces.web.routes import router

    publicas = [f"/api{getattr(r, 'path', '')}" for r in router.routes]
    # CHANGELOG y DECISIONES NARRAN el cambio: ahí la dirección duplicada se nombra como historia (y
    # tiene que poder nombrarse). El resto de la documentación describe cómo se usa HOY.
    historicos = {"CHANGELOG.md", "DECISIONES.md"}
    documentos = [p for p in PROJECT_ROOT.glob("*.md") if p.name not in historicos] \
        + [PROJECT_ROOT / "COMANDOS_AXIOMA.txt"]
    revisadas, faltantes, duplicadas = 0, [], []
    for documento in documentos:
        if not documento.exists():
            continue
        for numero, linea in enumerate(documento.read_text(encoding="utf-8").splitlines(), 1):
            if "/api/api/" in linea:
                duplicadas.append(f"{documento.name}:{numero}")
            for ruta in re.findall(r"/api/v1/[A-Za-z0-9_/]*", linea):
                ruta = ruta.rstrip("/")
                if not ruta or ruta.endswith("/v1"):
                    continue
                revisadas += 1
                # Se compara por prefijo: las rutas con parámetros llevan `{...}` en el router.
                if not any(p == ruta or p.startswith(ruta + "/") or p.startswith(ruta + "{")
                           for p in publicas):
                    faltantes.append(f"{documento.name}:{numero} → {ruta}")
    assert revisadas > 0, "no encontré rutas de la API en la documentación (¿cambió el formato?)"
    assert duplicadas == [], f"volvió a documentarse la dirección DUPLICADA: {duplicadas}"
    assert faltantes == [], (
        "estas rutas están documentadas y no existen (revisá el prefijo de la API): "
        + ", ".join(sorted(set(faltantes))[:5]))


# ═══════════════════════════════════════════════════════════════
# LAS CLAVES DEL USUARIO: una línea por clave y que no se pierdan
# ═══════════════════════════════════════════════════════════════
# MEDIDO el 2026-10-09: (1) un valor con salto de línea podía AGREGAR variables nuevas al `.env`
# (inyección: alcanzaba con mandar "algo\nSANDBOX_MODE=off"); (2) las claves cargadas desde la interfaz
# se escribían en `/app/.env`, que NO es volumen, así que se perdían al recrear el contenedor.

def test_una_clave_no_puede_inyectar_variables_en_el_env(tmp_path):
    """Un valor con salto de línea se rechaza: el `.env` cambia el comportamiento del sistema."""
    from src.core.apikeys import guardar_valores

    archivo = tmp_path / ".env"
    (tmp_path / ".env.example").write_text("TAVILY_API_KEY=\n", encoding="utf-8")

    with pytest.raises(ValueError) as fallo:
        guardar_valores({"TAVILY_API_KEY": "valor\nSANDBOX_MODE=off"}, ruta=archivo)
    assert "una sola línea" in str(fallo.value), str(fallo.value)

    contenido = archivo.read_text(encoding="utf-8") if archivo.exists() else ""
    assert "SANDBOX_MODE" not in contenido, "el salto de línea agregó una variable al .env"
    assert "\\0" not in contenido and "\\r" not in contenido

    # Y un valor normal se guarda igual (el camino bueno no se rompió).
    guardar_valores({"TAVILY_API_KEY": "clave-de-ejemplo-tvl"}, ruta=archivo)
    assert "clave-de-ejemplo-tvl" in archivo.read_text(encoding="utf-8")
    assert (os.stat(archivo).st_mode & 0o777) == 0o600


@_sin_empaquetado
def test_el_env_del_equipo_esta_montado_y_los_guiones_lo_crean():
    """Sin el montaje, las claves de la interfaz viven dentro del contenedor y se pierden al recrearlo."""
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for servicio in ("axioma", "axioma-puente"):
        volumenes = comp["services"][servicio].get("volumes") or []
        assert any(".env" in str(v) and "/app/.env" in str(v) for v in volumenes), \
            f"el servicio `{servicio}` tiene que montar el .env del equipo: {volumenes}"
    for guion in ("instalar/instalar_axioma.sh", "instalar/iniciar_axioma.sh"):
        fuente = (PROJECT_ROOT / guion).read_text(encoding="utf-8")
        assert "cp .env.example .env" in fuente, \
            f"{guion} tiene que crear el .env antes de encender (si no, Docker crea una carpeta)"


def test_el_cors_no_refleja_cualquier_origen():
    """MEDIDO el 2026-10-10: `allow_origins=["*"]` con `allow_credentials=True` hace que Starlette
    **refleje** el origen que pida el navegador (el comodín no se puede usar con credenciales). Con la
    interfaz escuchando en tu equipo, cualquier página abierta podía llamar a la API con tus credenciales.
    Ahora sólo se aceptan orígenes locales.
    """
    from fastapi.testclient import TestClient
    from src.interfaces.web.server import build_api_app

    app = build_api_app()
    assert app is not None, "la app de la API no se construyó (¿faltan endpoints críticos?)"
    cliente = TestClient(app, base_url="http://127.0.0.1")

    # Un origen AJENO no recibe permiso...
    ajeno = cliente.get("/v1/health", headers={"Origin": "http://sitio-ajeno.test"})
    assert "access-control-allow-origin" not in {k.lower() for k in ajeno.headers}, dict(ajeno.headers)

    # ... y el propio SÍ (en cualquiera de sus formas locales y con cualquier puerto).
    for origen in ("http://127.0.0.1:8080", "http://localhost:8099"):
        propio = cliente.get("/v1/health", headers={"Origin": origen})
        assert propio.headers.get("access-control-allow-origin") == origen, \
            (origen, dict(propio.headers))


@_sin_empaquetado
def test_la_imagen_de_ollama_esta_fijada_a_una_version():
    """MEDIDO el 2026-10-10: el compose usaba `ollama/ollama:latest`.

    Con `latest`, dos personas (o la misma, dentro de dos meses) bajan versiones distintas del motor y
    AXIOMA se comporta distinto sin que nadie haya cambiado el proyecto. Se fija a la versión medida en el
    equipo (0.30.0), comprobada contra Docker Hub antes de escribirla.
    """
    import re
    import yaml
    comp = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    imagen = comp["services"]["ollama"]["image"]
    assert not imagen.endswith(":latest"), f"la imagen de Ollama no puede ser `latest`: {imagen}"
    assert re.match(r"^ollama/ollama:\d+\.\d+(\.\d+)?$", imagen), \
        f"la imagen de Ollama tiene que estar fijada a una versión concreta: {imagen}"


def test_los_yaml_no_tienen_claves_duplicadas():
    """MEDIDO el 2026-10-10: `config/models.yaml` tenía DOS entradas `qwen2.5-coder:7b` —una de código y
    otra de respaldo— y en YAML **gana la última**: se perdían silenciosamente `type: code`, los `timeouts`
    level_1..4, los `limits` y los `parameters`. Un `yaml.safe_load` normal no avisa: la anterior
    simplemente desaparece. Esta prueba usa un lector que **detecta duplicados, también anidados**.
    """
    import yaml

    class Estricto(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            vistas = set()
            for clave_nodo, _ in node.value:
                clave = self.construct_object(clave_nodo, deep=deep)
                assert clave not in vistas, (
                    f"clave duplicada {clave!r} en la línea {clave_nodo.start_mark.line + 1} de {archivo.name}"
                    " (en YAML gana la última y la anterior se pierde en silencio)")
                vistas.add(clave)
            return super().construct_mapping(node, deep=deep)

    yamls = sorted((PROJECT_ROOT / "config").glob("*.yaml")) + \
        sorted((PROJECT_ROOT / "config").glob("*.yml"))
    assert yamls, "no encontré configuración para revisar"
    for archivo in yamls:
        yaml.load(archivo.read_text(encoding="utf-8"), Loader=Estricto)


def test_el_catalogo_sigue_los_modelos_que_eligio_el_usuario(monkeypatch):
    """MEDIDO el 2026-10-10: el preflight exigía el nombre del CATÁLOGO aunque el usuario hubiera
    configurado otro modelo en su `.env` — justo lo contrario de la idea del proyecto (escanear el equipo,
    sugerir, y que el usuario ELIJA). Lo que el catálogo no conoce se estima y así lo dice.
    """
    # OJO: `config/__init__.py` re-exporta la INSTANCIA de `settings` (no el módulo).
    from config import settings as ajustes
    from config.model_catalog import cargar_catalogo, modelos_efectivos

    monkeypatch.setattr(ajustes, "llm_default_model", "mi-modelo-elegido:9b", raising=False)
    pares = dict((rol, modelo) for rol, modelo in modelos_efectivos(cargar_catalogo(usar_cache=False)))
    assert "chat" in pares, pares.keys()
    elegido = pares["chat"]
    assert elegido.nombre == "mi-modelo-elegido:9b", elegido.nombre
    assert "ESTIMADO" in elegido.para, elegido.para
    assert elegido.instalar == "ollama pull mi-modelo-elegido:9b", elegido.instalar
    assert elegido.ram_gb > 0, "tiene que estimar la memoria por el nombre para poder avisar"


def test_elegir_modelo_por_rol_valida_contra_ollama(monkeypatch, tmp_path, ollama_falso):
    """El paso de la interfaz: se ELIGE el modelo por rol y se guarda en el `.env`, pero sólo si EXISTE.

    MEDIDO: sin validación, escribir un nombre que no está en Ollama deja a AXIOMA sin poder responder en
    ese rol y el error aparece mucho después (cuando alguien pide algo de ese rol).
    """
    import asyncio
    from config import settings as ajustes
    from src.core import apikeys
    from src.interfaces.web import routes_preflight as rp

    monkeypatch.setattr(ajustes, "ollama_host", ollama_falso.url, raising=False)
    monkeypatch.setattr(apikeys, "ENV_POR_DEFECTO", tmp_path / ".env", raising=False)
    (tmp_path / ".env").write_text("LLM_DEFAULT_MODEL=qwen3:8b\n", encoding="utf-8")

    # 1) Lo que hay en Ollama (el falso anuncia `falso:1b`).
    listado = asyncio.run(rp.preflight_modelos())
    assert listado["instalados"] == ["falso:1b"], listado
    assert listado["ollama_disponible"] is True

    # 1b) Y lo que la pantalla dice que está configurado tiene que ser lo que AXIOMA usa de verdad.
    # MEDIDO el 2026-10-10 con la prueba visual: el nombre del atributo se armaba desde el ROL
    # (`llm_codigo_model`), así que «Programar (código)» salía VACÍO aunque el `.env` tuviera
    # `LLM_CODE_MODEL`. La prueba vieja miraba `instalados` y no `configurados`: por eso no lo cazó.
    for variable, valor in (("llm_default_model", "falso:1b"), ("llm_code_model", "codigo-real:7b"),
                            ("llm_code_model_fallback", "respaldo:3b"), ("llm_vision_model", "vision:4b")):
        monkeypatch.setattr(ajustes, variable, valor, raising=False)
    configurados = asyncio.run(rp.preflight_modelos())["configurados"]
    assert configurados == {"chat": "falso:1b", "codigo": "codigo-real:7b",
                            "codigo_respaldo": "respaldo:3b", "vision": "vision:4b"}, configurados

    # 2) Un modelo que NO existe: no se guarda y se explica.
    malo = asyncio.run(rp.preflight_guardar_modelos({"chat": "no-existe:99b"}))
    assert malo["guardados"] == [] and "no-existe:99b" in str(malo["errores"]), malo
    assert "no-existe:99b" not in (tmp_path / ".env").read_text(encoding="utf-8")

    # 3) Uno que SÍ existe: se guarda en la variable del rol.
    bueno = asyncio.run(rp.preflight_guardar_modelos({"chat": "falso:1b"}))
    assert bueno["guardados"] == ["LLM_DEFAULT_MODEL"], bueno
    assert "LLM_DEFAULT_MODEL=falso:1b" in (tmp_path / ".env").read_text(encoding="utf-8")

    # 4) Y se APLICA ya, no sólo en el archivo: MEDIDO (2026-10-10) — antes quedaba en el `.env` y la
    # pantalla (que lee `settings`) seguía mostrando el modelo viejo hasta reiniciar, mientras el mensaje
    # decía «AXIOMA lo usa para ese rol».
    assert ajustes.llm_code_model == "codigo-real:7b"          # lo dejó puesto el paso 1b
    otro = asyncio.run(rp.preflight_guardar_modelos({"codigo": "falso:1b"}))
    assert otro["guardados"] == ["LLM_CODE_MODEL"], otro
    assert ajustes.llm_code_model == "falso:1b", ajustes.llm_code_model
    assert "se aplica ya" in otro["detalle"].lower(), otro["detalle"]


def test_elegir_modelo_sin_ollama_no_guarda_a_ciegas(monkeypatch, tmp_path):
    """Sin Ollama no se puede validar: se avisa y NO se escribe (mejor que dejar la config a medias)."""
    import asyncio
    from config import settings as ajustes
    from src.core import apikeys
    from src.interfaces.web import routes_preflight as rp

    monkeypatch.setattr(ajustes, "ollama_host", "http://127.0.0.1:1", raising=False)
    monkeypatch.setattr(apikeys, "ENV_POR_DEFECTO", tmp_path / ".env", raising=False)
    (tmp_path / ".env").write_text("LLM_DEFAULT_MODEL=qwen3:8b\n", encoding="utf-8")

    r = asyncio.run(rp.preflight_guardar_modelos({"chat": "cualquiera:7b"}))
    assert r["guardados"] == [] and r["errores"], r
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LLM_DEFAULT_MODEL=qwen3:8b\n"


def test_la_pantalla_permite_elegir_modelo_por_rol():
    """El paso «elegir» de la idea del usuario: la pantalla 🧩 ofrece los modelos de Ollama y guarda el
    elegido por rol, con la validación del backend (no se escribe un modelo que no está instalado).
    """
    fuente = (PROJECT_ROOT / "src" / "interfaces" / "components" / "configurar_axioma.py").read_text(encoding="utf-8")
    assert "def _seccion_modelos" in fuente, "falta la sección para elegir modelos"
    assert "_seccion_modelos()" in fuente.split("def abrir_configurar_axioma")[1], \
        "la sección existe pero no se muestra al abrir la pantalla"
    for pieza in ("preflight_modelos", "preflight_guardar_modelos", "ui.select"):
        assert pieza in fuente, f"la sección no usa {pieza}"
    # Los cuatro roles, y ninguno inventado: son los del catálogo.
    for rol in ("chat", "codigo", "codigo_respaldo", "vision"):
        assert f'("{rol}"' in fuente, f"falta el rol {rol} en la pantalla"
    # Regla R7: la consulta se hace UNA vez (nada de sondas en bucle).
    assert "ui.timer(0.05, cargar, once=True)" in fuente, \
        "la carga de modelos tiene que ser una sola vez (regla R7)"


def test_las_secciones_de_la_pantalla_van_ADENTRO_del_dialogo():
    """MEDIDO (lo reportó el usuario el 2026-10-10): las secciones se llamaban al final de la función,
    o sea FUERA del `with ui.dialog()...`, así que sus campos se dibujaban en la PÁGINA y no dentro de
    la pantalla. Resultado: al cerrarla quedaban a la vista **tapando todo** y cada clic en el ícono de
    configuración agregaba otra copia (nunca se iban). Se comprueba con el árbol sintáctico: estar
    nombrada no alcanza, tiene que estar **dentro** del `with` que crea el diálogo.
    """
    import ast

    fuente = (PROJECT_ROOT / "src" / "interfaces" / "components" / "configurar_axioma.py"
              ).read_text(encoding="utf-8")
    funcion = next((n for n in ast.walk(ast.parse(fuente))
                    if isinstance(n, ast.FunctionDef) and n.name == "abrir_configurar_axioma"), None)
    assert funcion is not None, "no encontré abrir_configurar_axioma"
    dialogos = [n for n in ast.walk(funcion) if isinstance(n, ast.With)
                if any("dialog" in ast.dump(item.context_expr) for item in n.items)]
    assert dialogos, "no encontré el `with ui.dialog()` de la pantalla de configuración"
    adentro = {ast.dump(nodo) for dialogo in dialogos for nodo in ast.walk(dialogo)}
    for seccion in ("_seccion_chequeo_de_modelo", "_seccion_modelos"):
        llamadas = [n for n in ast.walk(funcion)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == seccion]
        assert llamadas, f"la pantalla no llama a {seccion}"
        assert all(ast.dump(llamada) in adentro for llamada in llamadas), \
            f"{seccion} se llama FUERA del diálogo: se dibujaría en la página y taparía la pantalla"

def test_la_api_rechaza_un_host_ajeno_y_acepta_el_local(monkeypatch):
    """MEDIDO (2026-10-10): la API no validaba el `Host`. Una página puede apuntar un dominio propio a
    127.0.0.1 (DNS rebinding) y, como el navegador lo ve como el MISMO origen, el CORS no la frena:
    podía llamar a las rutas que ESCRIBEN el `.env`. Con la dirección local sólo se acepta el `Host` local.
    """
    from fastapi.testclient import TestClient
    from src.interfaces.web.server import build_api_app

    monkeypatch.setenv("AXIOMA_WEB_HOST", "127.0.0.1")
    app = build_api_app()
    assert app is not None, "la app de la API no se construyó"
    local = TestClient(app, base_url="http://127.0.0.1").get("/v1/health")
    assert local.status_code != 400, local.status_code
    ajeno = TestClient(app, base_url="http://sitio-ajeno.test").get("/v1/health")
    assert ajeno.status_code == 400, f"un Host ajeno tendría que rechazarse (salió {ajeno.status_code})"
    assert "no permitido" in ajeno.json()["detail"], ajeno.json()

    # Expuesto a propósito (`AXIOMA_WEB_HOST=0.0.0.0`): NO se restringe (decisión explícita del usuario).
    monkeypatch.setenv("AXIOMA_WEB_HOST", "0.0.0.0")
    expuesto = build_api_app()
    assert TestClient(expuesto, base_url="http://sitio-ajeno.test").get("/v1/health").status_code != 400


# ═══════════════════════════════════════════════════════════════
# TOKEN EN LAS RUTAS QUE ESCRIBEN CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════
# MEDIDO el 2026-10-25 con TestClient: `POST /api/v1/preflight/claves`
# respondía 200 y escribía el `.env` SIN credenciales. El check de `Host` sólo
# protege en loopback, así que con AXIOMA_WEB_HOST=0.0.0.0 un Host ajeno
# pasaba. Diseño (ver `routes_preflight.exigir_token_de_escritura`):
#   · token en AXIOMA_WEB_TOKEN + header X-AXIOMA-Token, `hmac.compare_digest`;
#   · configurado → se exige siempre (también en loopback);
#   · sin configurar → loopback permite (uso local), red rechaza con 403.

def _cliente_config(tmp_path, monkeypatch, *, bind="127.0.0.1", token=None, host="127.0.0.1"):
    """Cliente de la API con un `.env` aislado y el bind/token que pida la prueba."""
    from fastapi.testclient import TestClient
    from config.settings import settings
    from src.core import apikeys
    from src.interfaces.web.server import build_api_app

    env = tmp_path / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("TAVILY_API_KEY=clave-vieja\n", encoding="utf-8")
    monkeypatch.setattr(apikeys, "ENV_POR_DEFECTO", env, raising=False)
    # Aislar también la copia que settings leyó del `.env` real (si tuviera token).
    monkeypatch.setattr(settings, "axioma_web_token", token or "", raising=False)
    # Y el token automático: nunca el `data/.web_token` real del equipo (lo genera el uso
    # normal de AXIOMA); cada prueba mira su propio archivo dentro de tmp_path.
    monkeypatch.setenv("AXIOMA_WEB_TOKEN_FILE", str(tmp_path / ".web_token"))
    monkeypatch.setenv("AXIOMA_WEB_HOST", bind)
    if token is None:
        monkeypatch.delenv("AXIOMA_WEB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AXIOMA_WEB_TOKEN", token)
    app = build_api_app()
    assert app is not None, "la app de la API no se construyó"
    return TestClient(app, base_url=f"http://{host}"), env


def test_escribir_config_desde_la_red_sin_token_se_rechaza_y_no_toca_el_env(tmp_path, monkeypatch):
    """El exploit medido: bind 0.0.0.0, sin token y un Host de la LAN. Ni 200 ni escritura."""
    cliente, env = _cliente_config(tmp_path, monkeypatch, bind="0.0.0.0",
                                   host="192.168.1.50:8000")
    antes = env.read_bytes()
    sha_antes = hashlib.sha256(antes).hexdigest()

    for ruta, cuerpo in (("/v1/preflight/claves", {"TAVILY_API_KEY": "clave-nueva"}),
                         ("/v1/preflight/modelos", {"chat": "modelo:1b"})):
        respuesta = cliente.post(ruta, json=cuerpo)
        assert respuesta.status_code == 403, (ruta, respuesta.status_code, respuesta.text[:160])
        assert "token" in respuesta.json()["detail"].lower(), respuesta.json()

    # El `.env` no cambió ni un byte, y no se creó copia (no hubo escritura).
    assert hashlib.sha256(env.read_bytes()).hexdigest() == sha_antes
    assert env.read_text(encoding="utf-8") == "TAVILY_API_KEY=clave-vieja\n"
    assert not (tmp_path / ".env.bak").exists(), "no puede haber copia de una escritura que no ocurrió"


def test_el_token_valido_escribe_y_el_invalido_no(tmp_path, monkeypatch):
    """Con token configurado: falta/incorrecto → 401 sin tocar el `.env`; correcto → 200 y escribe."""
    token = "token-de-prueba-0123456789abcdef"
    cliente, env = _cliente_config(tmp_path, monkeypatch, bind="0.0.0.0",
                                   token=token, host="192.168.1.50:8000")
    antes = env.read_bytes()
    sha_antes = hashlib.sha256(antes).hexdigest()

    for cabeceras in ({}, {"X-AXIOMA-Token": "otro-token"}):
        respuesta = cliente.post("/v1/preflight/claves",
                                 json={"TAVILY_API_KEY": "clave-nueva"}, headers=cabeceras)
        assert respuesta.status_code == 401, (cabeceras, respuesta.status_code, respuesta.text[:160])
    assert hashlib.sha256(env.read_bytes()).hexdigest() == sha_antes, "un 401 no puede escribir"
    assert not (tmp_path / ".env.bak").exists()

    # Camino bueno: 200, guarda sólo la clave conocida y deja copia con el valor anterior.
    respuesta = cliente.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "clave-nueva",
                                                           "CLAVE_INVENTADA": "no"},
                             headers={"X-AXIOMA-Token": token})
    assert respuesta.status_code == 200, respuesta.text[:160]
    assert respuesta.json()["guardadas"] == ["TAVILY_API_KEY"], respuesta.json()
    assert "CLAVE_INVENTADA" not in env.read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=clave-nueva" in env.read_text(encoding="utf-8")
    assert hashlib.sha256(env.read_bytes()).hexdigest() != sha_antes
    assert "TAVILY_API_KEY=clave-vieja" in (tmp_path / ".env.bak").read_text(encoding="utf-8")


def test_en_loopback_sin_token_se_permite_pero_con_token_configurado_se_exige(tmp_path, monkeypatch):
    """El default documentado: loopback sin token = uso local de siempre; si hay token, manda el token."""
    cliente, env = _cliente_config(tmp_path, monkeypatch, bind="127.0.0.1")
    permitido = cliente.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "clave-loopback"})
    assert permitido.status_code == 200, permitido.text[:160]
    assert "TAVILY_API_KEY=clave-loopback" in env.read_text(encoding="utf-8")

    # Con token configurado NO se relaja por estar en loopback (una página local
    # también puede llamar a 127.0.0.1 con el Host correcto).
    cliente2, env2 = _cliente_config(tmp_path / "con-token", monkeypatch, bind="localhost",
                                     token="token-local-0123456789")
    sin_token = cliente2.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "x"})
    assert sin_token.status_code == 401, sin_token.text[:160]
    con_token = cliente2.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "x"},
                              headers={"X-AXIOMA-Token": "token-local-0123456789"})
    assert con_token.status_code == 200, con_token.text[:160]
    assert env2.exists()


# ═══════════════════════════════════════════════════════════════
# WORKSPACES: el session_id no puede salirse del directorio base
# ═══════════════════════════════════════════════════════════════
# MEDIDO el 2026-10-25 con sondas reales sobre `build_api_app()`:
#   · GET /v1/workspace/download?session_id=/etc&filename=passwd
#     devolvía 200 con 2182 B de /etc/passwd. La comprobación de contención
#     comparaba el archivo contra un «workspace» calculado con el MISMO
#     session_id del atacante, así que nunca bloqueaba.
#   · GET /v1/workspace/attempts?session_id=/etc también respondía 200.
#   · cleanup('../evil') borraba un archivo externo al base (y '../../..'
#     resolvía al home). Se alcanza con prompt injection vía la tool
#     workspace_manage(action='cleanup').
# El arreglo valida en UN punto (WorkspaceManager._workspace_path) y los
# bordes traducen el rechazo a 400/fail. Estas pruebas usan tmp_path.

def _wm_aislado(tmp_path, monkeypatch):
    """Instala el singleton de workspaces sobre un base temporal (nunca el home)."""
    from src.core import workspace_manager as modulo
    base = tmp_path / "workspaces"
    base.mkdir()
    monkeypatch.setattr(modulo, "_wm", modulo.WorkspaceManager(base_path=str(base)))
    return modulo, base


def _cliente_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.interfaces.web.server import build_api_app

    _, base = _wm_aislado(tmp_path, monkeypatch)
    app = build_api_app()
    assert app is not None, "la app de la API no se construyó"
    return TestClient(app, base_url="http://127.0.0.1"), base


def test_el_workspace_rechaza_session_id_que_intenta_salir_del_base(tmp_path):
    """Un session_id es un NOMBRE de directorio: rutas, absolutos, '..' y symlinks se rechazan."""
    from src.core.workspace_manager import WorkspaceManager

    base = tmp_path / "workspaces"
    base.mkdir()
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "secreto.txt").write_text("SECRETO-EXTERNO", encoding="utf-8")
    # El caso exacto del exploit: base/../evil debe sobrevivir a cleanup('../evil').
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "archivo.txt").write_text("NO-SE-BORRA", encoding="utf-8")
    wm = WorkspaceManager(base_path=str(base))

    maliciosos = [
        "/etc",
        str(fuera),
        "../evil",
        "../../..",
        "..",
        ".",
        "a/b",
        "a\\b",
        "con espacio",
        "ñ",
        "x" * 65,
    ]
    for sid in maliciosos:
        with pytest.raises(ValueError, match="session_id inválido"):
            wm.get_workspace(sid)
        with pytest.raises(ValueError, match="session_id inválido"):
            wm.cleanup(sid)
        with pytest.raises(ValueError, match="session_id inválido"):
            wm.workspace_exists(sid)
        with pytest.raises(ValueError, match="session_id inválido"):
            wm.scan_workspace(sid)
        with pytest.raises(ValueError, match="session_id inválido"):
            wm.get_attempt_history(sid)

    # Nada se borró ni se creó fuera del base: el cleanup malicioso no tocó disco.
    assert (evil / "archivo.txt").read_text(encoding="utf-8") == "NO-SE-BORRA"
    assert (fuera / "secreto.txt").read_text(encoding="utf-8") == "SECRETO-EXTERNO"

    # Un symlink DENTRO del base que apunta afuera también se rechaza.
    enlace = base / "enlace"
    enlace.symlink_to(fuera, target_is_directory=True)
    with pytest.raises(ValueError, match="session_id inválido"):
        wm.get_workspace("enlace")
    with pytest.raises(ValueError, match="session_id inválido"):
        wm.cleanup("enlace")
    assert (fuera / "secreto.txt").exists()


def test_los_session_id_legitimos_siguen_funcionando(tmp_path):
    """Las formas que ya usa el repo entran en la regex y no se rompió ninguna."""
    from src.core.workspace_manager import WorkspaceManager

    base = tmp_path / "workspaces"
    base.mkdir()
    wm = WorkspaceManager(base_path=str(base))

    legitimos = [
        "default",
        "s_ws",
        "550e8400-e29b-41d4-a716-446655440000",      # UUID con '-'
        "nicegui_20261025_123456_123456",             # id de la web
        "saintwick_20261025",                         # usuario + día
        "A" * 64,                                     # límite de longitud
    ]
    for sid in legitimos:
        ws = Path(wm.get_workspace(sid))
        assert ws.is_relative_to(base), (sid, ws)
        assert ws.is_dir()
        assert wm.workspace_exists(sid) is True

    # Y el cleanup propio borra SÓLO ese workspace.
    assert wm.cleanup("s_ws") is True
    assert not (base / "s_ws").exists()
    assert (base / "default").is_dir()


def test_la_api_no_sirve_archivos_externos_por_session_id(tmp_path, monkeypatch):
    """El exploit medido: session_id=/etc ya no devuelve 200 ni el contenido."""
    cliente, base = _cliente_api(tmp_path, monkeypatch)

    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    (ajeno / "secreto.txt").write_text("SECRETO-EXTERNO", encoding="utf-8")

    for sid in ("/etc", "../evil", "../../..", str(ajeno), "a/b"):
        descarga = cliente.get("/v1/workspace/download",
                               params={"session_id": sid, "filename": "passwd"})
        assert descarga.status_code == 400, (sid, descarga.status_code, descarga.text[:120])
        assert b"root:x:" not in descarga.content, "se sirvió /etc/passwd"
        intentos = cliente.get("/v1/workspace/attempts", params={"session_id": sid})
        assert intentos.status_code == 400, (sid, intentos.status_code, intentos.text[:120])

    # El archivo externo no se puede leer por la puerta del session_id...
    externo = cliente.get("/v1/workspace/download",
                          params={"session_id": str(ajeno), "filename": "secreto.txt"})
    assert externo.status_code == 400
    assert b"SECRETO-EXTERNO" not in externo.content

    # ...ni se creó nada fuera del base (antes el GET con un id absoluto creaba directorios).
    assert not (tmp_path / "evil").exists()
    assert not (base.parent / "evil").exists()
    assert [p.name for p in base.iterdir()] == []

    # Control: con un workspace legítimo, el filename malicioso SÍ se bloquea (403).
    traversal = cliente.get("/v1/workspace/download",
                            params={"session_id": "s_ws", "filename": "../secreto.txt"})
    assert traversal.status_code == 403, traversal.status_code


def test_la_descarga_y_los_attempts_del_workspace_propio_siguen_andando(tmp_path, monkeypatch):
    """El camino bueno no se rompió: descargar un archivo propio y ver el historial."""
    from src.core.workspace_manager import get_workspace_manager

    cliente, _ = _cliente_api(tmp_path, monkeypatch)
    ws = Path(get_workspace_manager().get_workspace("s_ws"))
    (ws / "salida.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (ws / "attempt_1.py").write_text("print('uno')\n", encoding="utf-8")
    (ws / "script.py").write_text("print('final')\n", encoding="utf-8")

    descarga = cliente.get("/v1/workspace/download",
                           params={"session_id": "s_ws", "filename": "salida.csv"})
    assert descarga.status_code == 200, descarga.text[:120]
    assert descarga.content == b"a,b\n1,2\n"

    intentos = cliente.get("/v1/workspace/attempts", params={"session_id": "s_ws"})
    assert intentos.status_code == 200, intentos.text[:120]
    cuerpo = intentos.json()
    assert cuerpo["session_id"] == "s_ws" and cuerpo["total"] == 2, cuerpo
    assert [a["attempt"] for a in cuerpo["attempts"]] == [1, "final"]

    # El cleanup propio sigue borrando el workspace propio.
    assert get_workspace_manager().cleanup("s_ws") is True
    assert not ws.exists()


def test_la_tool_workspace_rechaza_el_cleanup_malicioso(tmp_path, monkeypatch):
    """Prompt injection por workspace_manage(action='cleanup', session_id='../evil'): no borra nada."""
    from src.tools_mcp.tools.workspace_tool import WorkspaceManageTool

    modulo, base = _wm_aislado(tmp_path, monkeypatch)
    wm = modulo.get_workspace_manager()
    externo = tmp_path / "externo.txt"
    externo.write_text("NO-SE-BORRA", encoding="utf-8")
    tool = WorkspaceManageTool()

    for sid in ("../evil", "../../..", "/etc", str(tmp_path)):
        resultado = tool._run(action="cleanup", session_id=sid)
        assert resultado.success is False, (sid, resultado)
        assert "session_id inválido" in (resultado.error or ""), (sid, resultado.error)
        inspeccion = tool._run(action="inspect", session_id=sid)
        assert inspeccion.success is False, (sid, inspeccion)
        assert "session_id inválido" in (inspeccion.error or ""), (sid, inspeccion.error)

    assert externo.read_text(encoding="utf-8") == "NO-SE-BORRA"
    assert not (tmp_path / "evil").exists()

    # El cleanup legítimo de la tool sigue funcionando.
    ws = Path(wm.get_workspace("propio"))
    (ws / "output.txt").write_text("ok", encoding="utf-8")
    resultado = tool._run(action="cleanup", session_id="propio")
    assert resultado.success is True, resultado
    assert not ws.exists()
    assert base.is_dir()


# ═══════════════════════════════════════════════════════════════
# TOKEN AUTOMÁTICO (2026-10-25)
# ═══════════════════════════════════════════════════════════════
# Antes había que generar el token a mano y pegarlo en `AXIOMA_WEB_TOKEN`: un
# usuario inexperto no tenía forma de saberlo. Ahora `config/acceso_web.py` resuelve
# entorno/.env → `data/.web_token` (0600) → lo genera. Estas pruebas usan `tmp_path`
# SIEMPRE: el archivo real del equipo no se toca ni se lee.

def test_sin_token_se_genera_solo_y_se_guarda_con_0600(tmp_path):
    from config.acceso_web import token_de_web

    archivo = tmp_path / "data" / ".web_token"
    assert not archivo.exists(), "la prueba tiene que empezar sin token"

    token = token_de_web(ruta=archivo, entorno={})
    assert token and len(token) >= 40, token
    assert archivo.read_text(encoding="utf-8").strip() == token, "el token no quedó guardado"
    if os.name == "posix":
        assert (archivo.stat().st_mode & 0o777) == 0o600, oct(archivo.stat().st_mode)

    # Es estable: el mismo archivo devuelve el mismo código (si no, cada request pediría otro).
    assert token_de_web(ruta=archivo, entorno={}) == token


def test_el_entorno_manda_y_no_pisa_el_archivo(tmp_path):
    from config.acceso_web import token_configurado, token_de_web

    archivo = tmp_path / ".web_token"
    archivo.write_text("guardado-en-el-archivo-0123456789\n", encoding="utf-8")
    del_entorno = "fijado-a-mano-para-la-prueba-0123456789"

    assert token_configurado(ruta=archivo, entorno={"AXIOMA_WEB_TOKEN": del_entorno}) == del_entorno
    assert token_de_web(ruta=archivo, entorno={"AXIOMA_WEB_TOKEN": del_entorno}) == del_entorno
    assert archivo.read_text(encoding="utf-8").strip() == "guardado-en-el-archivo-0123456789"


def test_si_no_se_puede_escribir_sigue_sin_token_y_no_falla(tmp_path):
    """Requisito: lectura/generación tolerante a fallos; AXIOMA no se cae por esto."""
    from config.acceso_web import token_de_web

    no_es_carpeta = tmp_path / "soy_un_archivo"
    no_es_carpeta.write_text("x", encoding="utf-8")
    assert token_de_web(ruta=no_es_carpeta / ".web_token", entorno={}) == ""


def test_rotar_token_genera_otro_y_lo_persiste(tmp_path):
    from config.acceso_web import rotar_token_web, token_de_web

    archivo = tmp_path / ".web_token"
    viejo = token_de_web(ruta=archivo, entorno={})
    nuevo = rotar_token_web(ruta=archivo)

    assert nuevo and nuevo != viejo, "rotar tiene que cambiar el código"
    assert archivo.read_text(encoding="utf-8").strip() == nuevo
    if os.name == "posix":
        assert (archivo.stat().st_mode & 0o777) == 0o600, oct(archivo.stat().st_mode)


def test_el_token_del_archivo_autoriza_las_rutas_de_escritura(tmp_path, monkeypatch):
    """El código generado se usa de verdad: sin header → 401, con header → 200."""
    from config.acceso_web import token_de_web

    token = token_de_web(ruta=tmp_path / ".web_token", entorno={})
    cliente, env = _cliente_config(tmp_path, monkeypatch, bind="0.0.0.0",
                                   host="192.168.1.50:8000")
    sin = cliente.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "sin-token"})
    assert sin.status_code == 401, sin.text[:160]
    assert "sin-token" not in env.read_text(encoding="utf-8")

    con = cliente.post("/v1/preflight/claves", json={"TAVILY_API_KEY": "con-token"},
                       headers={"X-AXIOMA-Token": token})
    assert con.status_code == 200, con.text[:160]
    assert "TAVILY_API_KEY=con-token" in env.read_text(encoding="utf-8")


def test_host_expuesto_reconoce_las_direcciones_locales():
    from config.acceso_web import host_expuesto, host_local

    for local in ("127.0.0.1", "localhost", "::1", "[::1]:8080", "127.0.0.1:8000", "", None):
        assert host_local(local) is True, local
        assert host_expuesto(local) is False, local
    for abierta in ("0.0.0.0", "192.168.1.50", "[::]:8080", "::", "fe80::1", "mi-pc.local"):
        assert host_expuesto(abierta) is True, abierta


def test_el_chequeo_avisa_si_esta_expuesto_sin_token(tmp_path, monkeypatch, capsys):
    """Parte C: expuesto a la red sin código ⇒ warning visible con qué hacer."""
    from config.settings import settings
    monkeypatch.setenv("AXIOMA_WEB_TOKEN_FILE", str(tmp_path / ".web_token"))
    monkeypatch.setenv("AXIOMA_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("AXIOMA_WEB_TOKEN", raising=False)
    monkeypatch.setattr(settings, "axioma_web_token", "", raising=False)

    from src.interfaces.cli.acceso import avisar_acceso_expuesto
    avisar_acceso_expuesto("0.0.0.0")
    salida = capsys.readouterr().out
    assert "TODAVÍA no tiene código de acceso" in salida, salida
    assert "--rotar-token" in salida, salida
    assert "AXIOMA_WEB_HOST=127.0.0.1" in salida, salida

    # Con el código ya guardado, el aviso cambia a informativo (no alarma).
    from config.acceso_web import token_de_web
    token_de_web(ruta=tmp_path / ".web_token", entorno={})
    avisar_acceso_expuesto("0.0.0.0")
    salida2 = capsys.readouterr().out
    assert "código de acceso está guardado" in salida2, salida2


def test_el_aviso_de_primer_arranque_se_muestra_una_vez_y_sin_tty(tmp_path):
    import io
    from src.interfaces.cli.primer_arranque import mostrar_aviso_de_modo_de_acceso

    marca = tmp_path / ".primer_arranque"
    salida = io.StringIO()
    opcion = mostrar_aviso_de_modo_de_acceso(ruta_marcador=marca, interactivo=False, salida=salida)
    texto = salida.getvalue()

    assert opcion == 1, "sin terminal tiene que seguir con la opción 1"
    assert "¿Cómo vas a usar AXIOMA?" in texto
    assert "Sólo en esta computadora" in texto and "opción por defecto: 1" in texto
    assert "opción 1" in texto
    for jerga in ("bind", "loopback", "header"):
        assert jerga not in texto.lower(), f"jerga técnica en el aviso: {jerga}"
    assert marca.exists(), "tiene que quedar el marcador para no repetirlo"

    salida2 = io.StringIO()
    assert mostrar_aviso_de_modo_de_acceso(ruta_marcador=marca, interactivo=False,
                                           salida=salida2) == 0
    assert salida2.getvalue() == "", "la segunda vez no tiene que molestar"


def test_el_aviso_de_primer_arranque_respeta_la_opcion_elegida(tmp_path):
    import io
    from src.interfaces.cli.primer_arranque import mostrar_aviso_de_modo_de_acceso

    salida = io.StringIO()
    opcion = mostrar_aviso_de_modo_de_acceso(ruta_marcador=tmp_path / ".primer_arranque",
                                             interactivo=True, entrada=io.StringIO("2\n"),
                                             salida=salida)
    assert opcion == 2
    assert "AXIOMA_WEB_HOST=0.0.0.0" in salida.getvalue(), salida.getvalue()
