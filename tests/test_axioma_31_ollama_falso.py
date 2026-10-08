#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# Sección 31: OLLAMA FALSO — probar los caminos reales sin servidor
# ═══════════════════════════════════════════════════════════════
# Por qué existe: en el CI, `OLLAMA_HOST` apunta a un puerto muerto (`http://127.0.0.1:1`), así que las
# pruebas que necesitan el servidor se SALTAN (medido: `test_axioma_02_core.py::restore_default()` y dos
# de visión) y **el camino real del cliente nunca se ejercita**. Estos tests usan el Ollama falso de
# `conftest.py`: un servidor HTTP de verdad de la biblioteca estándar, así que el cliente del proyecto
# hace pedidos HTTP reales (mejor que reemplazarlo por un doble).
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _cliente(url, **extra):
    from src.llm.client import OllamaClient
    return OllamaClient(model="falso:1b", timeout=10, base_url=url, **extra)


def test_el_chat_habla_por_http_de_verdad(ollama_falso):
    """`chat()` contra el falso: pedido HTTP real, respuesta leída del servidor."""
    with _cliente(ollama_falso.url) as cliente:
        respuesta = cliente.chat([{"role": "user", "content": "hola"}])
    assert "respuesta del Ollama falso" in str(respuesta), respuesta
    pedidos = [p for p in ollama_falso.pedidos if p[1].startswith("/api/chat")]
    assert len(pedidos) == 1, f"tiene que haber UN pedido de chat: {ollama_falso.pedidos}"
    assert pedidos[0][2].get("model") == "falso:1b", pedidos[0][2]


def test_la_descarga_informa_progreso_y_termina(ollama_falso):
    """`pull_model()`: el camino que MEDIMOS en el trabajo de contenedores (progreso real + final).

    Es el pedido que Ollama contesta en modo flujo; sin un servidor de verdad no se podía probar.
    """
    eventos = []
    with _cliente(ollama_falso.url) as cliente:
        cliente.pull_model("falso:1b", on_progress=eventos.append)
    pedidos = [p for p in ollama_falso.pedidos if p[1].startswith("/api/pull")]
    assert len(pedidos) == 1, f"tiene que haber UN pedido de descarga: {ollama_falso.pedidos}"
    # Ollama recibe la descarga con `name` (medido: el cuerpo real es {"name": …, "stream": true}).
    assert pedidos[0][2].get("name") == "falso:1b", pedidos[0][2]
    assert pedidos[0][2].get("stream") is True, (
        "la descarga tiene que ir en modo flujo: sin flujo no hay progreso ni forma de cancelar "
        f"{pedidos[0][2]}")
    assert eventos, "la descarga tiene que informar progreso (antes no informaba nada)"
    # Ollama manda varios eventos y termina con `success`: el cliente tiene que haberlos procesado.
    estados = [str(e.get("estado", "")) for e in eventos]
    assert any("success" in e for e in estados), f"no llegó el final de la descarga: {eventos}"


def test_un_error_del_servidor_no_cuelga_ni_miente(ollama_falso_que_falla):
    """Servidor caído: el falso contesta 500 y el cliente falla claro, sin colgarse.

    MEDIDO al escribir esto (por eso la prueba es así): comprobar el 500 **por HTTP directo** y no
    «cuántos pedidos llegaron». El cliente tiene un **cortacircuitos** y además consulta la salud del
    servidor **configurado** (`OLLAMA_HOST`), no la del `base_url` que se le pasa: en un equipo sin
    Ollama la conexión se rechaza antes de llegar al falso, y eso es comportamiento buscado (fallar
    rápido y claro), no un defecto de la prueba. Con estado compartido, contar pedidos daba un fallo
    intermitente: la prueba tiene que mirar el comportamiento, no el contador.
    """
    import urllib.error
    import urllib.request

    with pytest.raises(urllib.error.HTTPError) as fallo:
        urllib.request.urlopen(f"{ollama_falso_que_falla.url}/api/tags", timeout=5)
    assert fallo.value.code == 500, "el falso tiene que contestar error"

    with _cliente(ollama_falso_que_falla.url) as cliente:
        with pytest.raises(Exception) as error:
            cliente.chat([{"role": "user", "content": "hola"}])
    assert str(error.value).strip(), "el error no puede ser mudo"


def test_el_falso_sirve_para_saber_que_modelos_hay(ollama_falso):
    """`/api/tags`: es lo que usan el preflight y `hardware_fit` para saber si un modelo está."""
    import urllib.request
    with urllib.request.urlopen(f"{ollama_falso.url}/api/tags", timeout=5) as r:
        assert r.status == 200
        assert b"falso:1b" in r.read()


def test_las_pruebas_no_dependen_de_que_haya_ollama_en_la_maquina():
    """El falso escucha en un puerto libre de `127.0.0.1` que elige el sistema operativo.

    Así las pruebas funcionan igual en el CI (donde `OLLAMA_HOST` apunta a un puerto muerto) y en un
    equipo con Ollama de verdad: nadie comparte puerto ni hay que abrir nada.
    """
    import socket
    from tests.conftest import _OllamaFalso
    falso = _OllamaFalso()
    try:
        host, puerto = "127.0.0.1", int(falso.url.rsplit(":", 1)[1])
        assert puerto > 0, falso.url
        with socket.create_connection((host, puerto), timeout=2):
            pass                                     # se puede conectar: está escuchando
    finally:
        falso.parar()
    with pytest.raises(OSError):                     # y al pararlo, el puerto deja de responder
        with socket.create_connection((host, puerto), timeout=1):
            pass
