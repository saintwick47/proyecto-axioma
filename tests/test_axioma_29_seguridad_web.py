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
