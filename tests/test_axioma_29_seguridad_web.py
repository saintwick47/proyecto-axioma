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
import json
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
    cliente = TestClient(app)

    # Un origen AJENO no recibe permiso...
    ajeno = cliente.get("/v1/health", headers={"Origin": "http://sitio-ajeno.test"})
    assert "access-control-allow-origin" not in {k.lower() for k in ajeno.headers}, dict(ajeno.headers)

    # ... y el propio SÍ (en cualquiera de sus formas locales y con cualquier puerto).
    for origen in ("http://127.0.0.1:8080", "http://localhost:8099"):
        propio = cliente.get("/v1/health", headers={"Origin": origen})
        assert propio.headers.get("access-control-allow-origin") == origen, \
            (origen, dict(propio.headers))
