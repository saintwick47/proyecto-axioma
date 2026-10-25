#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Autor: SaintWick — AXIOMA, suite integral
# Sección 30: AUDIO OPCIONAL Y BANDERAS QUE SE LEEN (regla R1)
# ═══════════════════════════════════════════════════════════════
# Medido el 2026-10-07: `AXIOMA_SIN_AUDIO` (declarada en `docker-compose.yml` para los dos servicios sin
# audio) y `AXIOMA_EN_CONTENEDOR` (declarada en el `Dockerfile`) **no las leía ningún módulo**. El
# comentario del compose decía «la voz se desactiva sola» y eso era falso: el usuario veía «no se ven
# dispositivos de audio» y pensaba que era su equipo, cuando en realidad era el contenedor.
#
# Decisión (fase 2 del plan): **una idea, una bandera**. Se implementa `AXIOMA_SIN_AUDIO` (con lector,
# con efecto real: no se abren dispositivos y se explica por qué) y se ELIMINA `AXIOMA_EN_CONTENEDOR`
# del Dockerfile, porque lo que el contenedor necesita ya se pasa explícito (`--no-browser`,
# `AXIOMA_RAFAEL_DIRECTO`, `AXIOMA_SIN_AUDIO`, rutas de logs).
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Los archivos de EMPAQUETADO (`docker-compose.yml`) no viajan dentro de la imagen: la suite corre
# adentro del contenedor y estas pruebas lo leen, así que se saltean ahí. Se comprueban en el CI y en el
# equipo (donde el archivo existe). MEDIDO el 2026-10-08: 7 pruebas fallaban sólo dentro de la imagen
# armada desde una copia limpia por este motivo.
_sin_compose = pytest.mark.skipif(
    not (PROJECT_ROOT / "docker-compose.yml").exists(),
    reason="el empaquetado no viaja dentro de la imagen (se comprueba en el código)")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_la_bandera_se_lee_de_verdad():
    """Lo que la bandera significa, sin ambigüedades (y sin confundir `0` con «no»)."""
    from config.multimodal_loader import audio_deshabilitado
    for si in ("1", "true", "TRUE", "si", "sí", "yes", "on"):
        assert audio_deshabilitado({"AXIOMA_SIN_AUDIO": si}) is True, si
    for no in ("", "0", "false", "no", "off", "quizás"):
        assert audio_deshabilitado({"AXIOMA_SIN_AUDIO": no}) is False, no
    assert audio_deshabilitado({}) is False, "sin la bandera, el audio está disponible"


def test_sin_audio_el_preflight_no_sondea_dispositivos_y_explica_por_que(monkeypatch):
    """El efecto REAL: no se abren dispositivos y el mensaje dice que es a propósito.

    Antes se sondeaba igual, y el mensaje («no se ven dispositivos de audio») hacía pensar en un
    problema del equipo.
    """
    from src.core import preflight

    sondeos = []
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # Sin PortAudio (libportaudio2) sounddevice no se puede importar: sin
        # módulo no hay `query_devices` que espiar. Es el entorno del CI cuando
        # el apt-get de las librerías de audio no llega a instalarlas.
        pytest.skip(f"sin audio (PortAudio): {exc}")
    monkeypatch.setattr(sounddevice, "query_devices", lambda *a, **k: sondeos.append(1) or [], raising=False)
    monkeypatch.setenv("AXIOMA_SIN_AUDIO", "1")

    requisito = preflight._probar_audio(None)
    assert sondeos == [], "con AXIOMA_SIN_AUDIO=1 no se tienen que sondear dispositivos"
    assert "desactivada a propósito" in requisito.detalle, requisito.detalle
    assert requisito.estado == preflight.AVISO, "la voz es opcional: no puede bloquear el arranque"
    assert "AXIOMA_SIN_AUDIO=1" in requisito.detalle, requisito.detalle

    # Y sin la bandera vuelve a sondear (el camino normal no se rompió).
    monkeypatch.delenv("AXIOMA_SIN_AUDIO", raising=False)
    preflight._probar_audio(None)
    assert sondeos, "sin la bandera tiene que sondear los dispositivos"


def test_la_interfaz_no_ofrece_voz_cuando_esta_desactivada(monkeypatch):
    """El botón de voz no puede llevar a un fallo: si no hay audio, se sabe de antemano."""
    from src.interfaces.web.voice_runtime import voz_posible
    monkeypatch.setenv("AXIOMA_SIN_AUDIO", "1")
    assert voz_posible() is False
    monkeypatch.setenv("AXIOMA_SIN_AUDIO", "0")
    assert voz_posible() is True


def test_rafael_avisa_y_no_arranca_sin_audio():
    """El daemon de voz: con la bandera puesta tiene que decirlo, no intentar y fallar raro."""
    import Rafael
    assert Rafael._sin_audio({"AXIOMA_SIN_AUDIO": "1"}) is True
    assert Rafael._sin_audio({}) is False
    fuente = (PROJECT_ROOT / "Rafael.py").read_text(encoding="utf-8")
    assert "no hay audio utilizable en este entorno" in fuente, \
        "el daemon tiene que avisar en castellano antes de rendirse"


@_sin_compose
def test_no_quedan_banderas_del_contenedor_sin_lector():
    """Regla R1 del proyecto: ninguna configuración sin quien la lea.

    `AXIOMA_EN_CONTENEDOR` se eliminó (nadie la leía y lo que el contenedor necesita ya se pasa
    explícito); `AXIOMA_SIN_AUDIO` se quedó porque ahora sí tiene lector y efecto.
    """
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    codigo = "\n".join((PROJECT_ROOT / ruta).read_text(encoding="utf-8")
                       for ruta in ("config/multimodal_loader.py", "src/core/preflight.py",
                                    "src/interfaces/web/voice_runtime.py", "Rafael.py"))

    assert "AXIOMA_EN_CONTENEDOR" not in dockerfile.replace(
        "# (2026-10-07) Acá se declaraba `AXIOMA_EN_CONTENEDOR=1`", ""), \
        "el Dockerfile volvió a declarar la bandera que nadie lee"
    assert "AXIOMA_SIN_AUDIO=1" in dockerfile, "el contenedor tiene que declarar que no hay audio"
    assert "AXIOMA_SIN_AUDIO" in compose, "los servicios sin audio tienen que declararlo"
    assert "AXIOMA_SIN_AUDIO" in codigo, "la bandera que se declara tiene que tener lector (R1)"
    assert "audio_deshabilitado" in codigo, "el lector único de la bandera desapareció"

    # Y el comentario del compose tiene que describir lo que AHORA pasa de verdad.
    assert "desactivada A PROPÓSITO" in compose, \
        "el comentario tiene que decir que la voz se desactiva a propósito (y que hay código que lo hace)"


def test_el_widget_de_escritorio_no_se_lanza_sin_pantalla(monkeypatch):
    """MEDIDO (2026-10-12): sin pantalla, el widget de Rafael (Flet desktop) **no falla: se CUELGA**
    (`exit 124` con un timeout de 45 s), así que el `await ...wait()` del supervisor quedaba esperando
    para siempre y los reintentos con abandono nunca llegaban a correr. Es una **ventana nativa**
    (`ft.app`, arrastre en X11/Wayland): sin pantalla no se lanza, y el widget corre en el EQUIPO
    (`docker-compose.yml:140`, `MANUAL_RAFAEL.md:130`).
    """
    import asyncio
    from src.core import rafael_daemon

    lanzamientos = []

    async def falso_lanzar(*args, **kwargs):
        lanzamientos.append(args)
        raise AssertionError("sin pantalla no tiene que lanzar el widget")

    # Sin `__init__` (no hace falta: la guarda sale antes de usar nada más) y sin esperas reales.
    daemon = rafael_daemon.RafaelDaemon.__new__(rafael_daemon.RafaelDaemon)
    daemon._shutdown_event = asyncio.Event()
    monkeypatch.setattr(rafael_daemon, "_WIDGET_RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", falso_lanzar)

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    asyncio.run(daemon._widget_supervisor())
    assert lanzamientos == [], "sin pantalla no se lanza (el runner de Flet queda colgado)"

    # Y con pantalla SÍ lo intenta: el camino normal no se rompió.
    monkeypatch.setenv("DISPLAY", ":0")
    daemon._shutdown_event = asyncio.Event()
    asyncio.run(daemon._widget_supervisor())
    assert lanzamientos, "con pantalla tiene que intentar lanzarlo"
