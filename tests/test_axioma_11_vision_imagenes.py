#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# tests/test_axioma_11_vision_imagenes.py
# v0.6.9p — pruebas EXHAUSTIVAS del canal de imágenes (chat → qwen3-vl).
#
# Capas:
#   A) Unidades de vision_service.describe_image() — SIN LLM (agente fake):
#      recorte, instrucciones, errores, VLM vacío, unload seguro.
#   B) Detección de adjunto de imagen en el chat (_is_image_attachment).
#   C) Integración REAL contra Ollama — solo si AXIOMA_VISION_E2E=1 y el
#      modelo llm_vision_model está disponible (salta en CI y en local
#      normal; ver tools/vision_battery.py para la batería diagnóstica).
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# Generadores de imágenes sintéticas reales (PIL) — reutilizados de la
# batería de diagnóstico para que los tests usen los mismos insumos.
try:
    from tools.vision_battery import (
        _png_b64, _solid, _shape, _count_circles, _text_image, _noise,
    )
    _IMG_GEN_OK = True
except Exception:  # pragma: no cover
    _IMG_GEN_OK = False


# ═══════════════════════════════════════════════════════════════
# A) Unidades de describe_image (agente fake, sin LLM)
# ═══════════════════════════════════════════════════════════════


class _FakeVision:
    """Agente VLM simulado: captura el mensaje y devuelve lo configurado."""

    def __init__(self, content: str = "respuesta", success: bool = True,
                 error: str | None = None):
        self.content = content
        self.success = success
        self.error = error
        self.captured = None
        self.model = "fake-vlm"
        self.host = "http://127.0.0.1:1"  # unreachable → unload best-effort

    def execute(self, msg, **kwargs):
        self.captured = (getattr(msg, "content", ""), kwargs)
        return SimpleNamespace(content=self.content, success=self.success,
                               error=self.error)


class TestDescribeImageUnidades:
    """describe_image() sin depender de Ollama (quality-first: lógica pura)."""

    def _call(self, b64="b64", instruction="", agent=None):
        from src.interfaces.web.vision_service import describe_image
        return describe_image(b64, instruction, agent=agent)

    def test_ok_devuelve_texto_recortado(self):
        agent = _FakeVision(content="  \n Hay un gato.  \n")
        assert self._call("b64", agent=agent) == "Hay un gato."

    def test_instruccion_vacia_usa_default(self):
        agent = _FakeVision()
        self._call("b64", "   ", agent=agent)
        msg, kwargs = agent.captured
        assert "Describí" in msg and "detalle" in msg
        assert kwargs.get("image_b64") == "b64"
        assert kwargs.get("context_hint") == "generic"

    def test_instruccion_propia_se_pasa_verbatim(self):
        agent = _FakeVision()
        self._call("b64", "¿Cuántas ventanas tiene?", agent=agent)
        msg, _ = agent.captured
        assert msg == "¿Cuántas ventanas tiene?"

    def test_sin_imagen_raise(self):
        with pytest.raises(RuntimeError, match="imagen"):
            self._call("", agent=_FakeVision())

    def test_vlm_falla_raise_con_error(self):
        agent = _FakeVision(content="", success=False, error="timeout 120s")
        with pytest.raises(RuntimeError, match="timeout"):
            self._call("b64", agent=agent)

    def test_vlm_exito_pero_vacio_raise(self):
        agent = _FakeVision(content="   ", success=True)
        with pytest.raises(RuntimeError, match="vac"):
            self._call("b64", agent=agent)

    def test_unload_no_rompe_con_agente_fake(self):
        # host inalcanzable → el unload best-effort debe silenciarse
        agent = _FakeVision(content="ok")
        assert self._call("b64", agent=agent) == "ok"

    def test_png_real_pasa_por_el_servicio(self):
        # b64 de un PNG real (PIL): el servicio no valida el decode (eso lo
        # hace el VLM), pero confirma que acepta base64 largo sin problemas.
        if not _IMG_GEN_OK:
            pytest.skip("PIL no disponible")
        agent = _FakeVision(content="Rojo")
        out = self._call(_png_b64(_solid((220, 30, 30))), "¿color?",
                         agent=agent)
        assert out == "Rojo"
        b64_len = len(agent.captured[1]["image_b64"])
        assert b64_len > 200  # PNG real de 200x200 pesa en base64


# ═══════════════════════════════════════════════════════════════
# B) Detección de adjunto de imagen en el chat
# ═══════════════════════════════════════════════════════════════


class TestDetectaImagenAdjunta:
    """chat._is_image_attachment: qué entra al canal de visión y qué no."""

    @staticmethod
    def _check(name):
        from src.interfaces.components.chat import _is_image_attachment
        return _is_image_attachment(name)

    @pytest.mark.parametrize("name", [
        "foto.png", "foto.PNG", "imagen.JPG", "img.jpg", "a.webp",
        "scan.gif", "x.bmp", "foto.jpeg", "sin.espacio.png",
    ])
    def test_imagenes_reconocidas(self, name):
        assert self._check(name) is True

    @pytest.mark.parametrize("name", [
        "doc.txt", "doc.pdf", "doc.docx", "data.csv", "code.py",
        "file.md", "sin_extension", "", "img.png.exe", "imagen",
    ])
    def test_no_imagenes_rechazadas(self, name):
        assert self._check(name) is False

    def test_none_seguro(self):
        from src.interfaces.components.chat import _is_image_attachment
        assert _is_image_attachment(None) is False


# ═══════════════════════════════════════════════════════════════
# D) Fase 1 — preprocesado adaptativo (vision_preprocess)
# ═══════════════════════════════════════════════════════════════


class TestVisionPreprocess:
    """build_variants(): qué imagen llega al VLM y en qué orden."""

    def test_imagen_normal_una_variante(self):
        from src.interfaces.web.vision_preprocess import build_variants
        # texto negro sobre blanco: contraste alto, sin upscale
        b64 = _png_b64(_text_image("HOLA", size_px=60))
        variants = build_variants(b64)
        assert len(variants) == 1 and variants[0] == b64

    def test_contraste_bajo_aplica_autocontraste(self):
        from src.interfaces.web.vision_preprocess import build_variants
        b64 = _png_b64(_text_image("PRUEBA", size_px=36,
                                   fg=(200, 200, 200), bg=(255, 255, 255)))
        variants = build_variants(b64)
        assert len(variants) == 2  # [autocontraste, original]
        assert variants[1] == b64

    def test_imagen_chica_upscalea(self):
        import base64 as _b64
        import io as _io
        from PIL import Image as _PIL
        from src.interfaces.web.vision_preprocess import build_variants
        b64 = _png_b64(_shape("circle", size=200))
        variants = build_variants(b64)
        assert len(variants) == 2 and variants[1] == b64
        # la primaria debe ser la imagen AGRANDADA (400px)
        img = _PIL.open(_io.BytesIO(_b64.b64decode(variants[0])))
        assert max(img.size) >= 400

    def test_disabled_una_sola_variante(self):
        from src.interfaces.web.vision_preprocess import build_variants
        b64 = _png_b64(_solid((220, 30, 30)))
        assert len(build_variants(b64, enabled=False)) == 1

    def test_color_solido_no_preprocesa(self):
        # color sólido grande → uniforme (std=0) y no chico → sin variantes
        from src.interfaces.web.vision_preprocess import build_variants
        b64 = _png_b64(_solid((220, 30, 30), size=600))
        assert len(build_variants(b64)) == 1
        # sólido CHICO → entra al upscale (imagen pequeña)
        assert len(build_variants(_png_b64(_solid((220, 30, 30))))) == 2

    def test_base64_invalido_no_rompe(self):
        from src.interfaces.web.vision_preprocess import build_variants
        assert build_variants("no-es-base64") == ["no-es-base64"]
        assert build_variants("") == [""]

    def test_analisis_reporta_contraste(self):
        from src.interfaces.web.vision_preprocess import analyze
        a_bajo = analyze(_png_b64(_text_image("X", size_px=30,
                                              fg=(200, 200, 200),
                                              bg=(255, 255, 255))))
        a_alto = analyze(_png_b64(_text_image("X", size_px=30,
                                              fg=(0, 0, 0),
                                              bg=(255, 255, 255))))
        assert a_bajo["contrast_std"] < a_alto["contrast_std"]


# ═══════════════════════════════════════════════════════════════
# E) Fase 2 — OCR híbrido con Tesseract (vision_ocr)
# ═══════════════════════════════════════════════════════════════
_TSV_OK = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
    "width\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t0\t0\t100\t20\t95.5\tHOLA\n"
    "5\t1\t1\t1\t1\t2\t100\t0\t100\t20\t92\t123\n"
)


class TestVisionOCR:
    """ocr_image() con Tesseract simulado (sin binario real en CI)."""

    @staticmethod
    def _fake_run(stdout: bytes = _TSV_OK.encode(), returncode: int = 0):
        proc = SimpleNamespace()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = b""
        return proc

    def test_parse_tsv(self):
        from src.interfaces.web.vision_ocr import _parse_tsv
        text, conf = _parse_tsv(_TSV_OK)
        assert text == "HOLA 123"
        assert 90 <= conf <= 99

    def test_ocr_ok(self, monkeypatch):
        from src.interfaces.web import vision_ocr
        monkeypatch.setattr(vision_ocr.subprocess, "run",
                            lambda *a, **k: self._fake_run())
        b64 = _png_b64(_text_image("HOLA 123", size_px=60))
        res = vision_ocr.ocr_image(b64, ocr_bin="tesseract-fake")
        assert res is not None
        assert res.text == "HOLA 123"
        assert res.confidence > 90

    def test_ocr_base64_invalido_none(self, monkeypatch):
        from src.interfaces.web import vision_ocr
        monkeypatch.setattr(vision_ocr.subprocess, "run",
                            lambda *a, **k: self._fake_run())
        assert vision_ocr.ocr_image("no-es-base64!!",
                                    ocr_bin="tesseract-fake") is None

    def test_ocr_sin_texto_devuelve_none(self, monkeypatch):
        from src.interfaces.web import vision_ocr
        tsv_vacio = _TSV_OK.splitlines()[0] + "\n"
        monkeypatch.setattr(vision_ocr.subprocess, "run",
                            lambda *a, **k: self._fake_run(tsv_vacio.encode()))
        assert vision_ocr.ocr_image("b64", ocr_bin="tesseract-fake") is None

    def test_ocr_timeout_devuelve_none(self, monkeypatch):
        import subprocess
        from src.interfaces.web import vision_ocr
        def _explode(*a, **k):
            raise subprocess.TimeoutExpired("tesseract", 10)
        monkeypatch.setattr(vision_ocr.subprocess, "run", _explode)
        assert vision_ocr.ocr_image("b64", ocr_bin="tesseract-fake") is None

    def test_ocr_sin_binario_none(self, monkeypatch):
        from src.interfaces.web import vision_ocr
        monkeypatch.setattr(vision_ocr.shutil, "which", lambda *a: None)
        assert vision_ocr.ocr_image("b64") is None

    def test_instruccion_pide_lectura(self):
        from src.interfaces.web.vision_service import _instruction_pide_lectura
        assert _instruction_pide_lectura("¿Qué texto dice? Copialo.") is True
        assert _instruction_pide_lectura("transcribí el cartel") is True
        assert _instruction_pide_lectura("Describí qué hay") is False
        assert _instruction_pide_lectura("") is False


# ═══════════════════════════════════════════════════════════════
# C) Integración REAL contra el VLM local (opt-in)
# ═══════════════════════════════════════════════════════════════


def _vision_e2e_disponible() -> bool:
    """True si el VLM local está instalado y Ollama responde."""
    if not _IMG_GEN_OK:
        return False
    try:
        import json
        import urllib.request
        from config.settings import settings
        host = getattr(settings, "ollama_host", "http://127.0.0.1:11434")
        model = getattr(settings, "llm_vision_model", "qwen3-vl:4b")
        base = model.split(":")[0]
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name", "").split(":")[0] == base
                   for m in data.get("models", []))
    except Exception:
        return False


_E2E = os.environ.get("AXIOMA_VISION_E2E") == "1"


@pytest.mark.skipif(not _E2E,
                    reason="seteá AXIOMA_VISION_E2E=1 para correr contra "
                           "Ollama real (lento: carga el VLM ~50 s)")
class TestVisionIntegracionReal:
    """Qué IDENTIFICA el VLM con imágenes reales (colores/formas/conteo/OCR).

    La batería completa con límites está en tools/vision_battery.py
    (python tools/vision_battery.py).
    """

    @pytest.fixture(autouse=True)
    def _disponible(self):
        if not _vision_e2e_disponible():
            pytest.skip("Ollama o modelo llm_vision_model no disponible")

    def _preguntar(self, img, question: str) -> tuple[str, float]:
        import time
        from src.interfaces.web.vision_service import describe_image
        t0 = time.time()
        out = describe_image(_png_b64(img), question)
        return out.strip(), time.time() - t0

    def test_color_rojo(self):
        out, dur = self._preguntar(_solid((220, 30, 30)),
                                   "¿De qué color? Una palabra.")
        assert "rojo" in out.lower(), f"dur={dur:.0f}s out={out!r}"

    def test_color_azul(self):
        out, dur = self._preguntar(_solid((30, 60, 220)),
                                   "¿De qué color? Una palabra.")
        assert "azul" in out.lower(), f"dur={dur:.0f}s out={out!r}"

    def test_forma_circulo(self):
        out, dur = self._preguntar(_shape("circle"),
                                   "¿Qué forma hay? Una palabra.")
        low = out.lower()
        assert any(k in low for k in ("círculo", "circulo", "redondo")), \
            f"dur={dur:.0f}s out={out!r}"

    def test_conteo_3(self):
        out, dur = self._preguntar(_count_circles(3),
                                   "¿Cuántos círculos rojos? Solo el número.")
        assert "3" in out, f"dur={dur:.0f}s out={out!r}"

    def test_ocr_texto_grande(self):
        out, dur = self._preguntar(
            _text_image("HOLA 123", size_px=60),
            "¿Qué texto dice exactamente? Copialo.")
        low = out.lower()
        assert ("hola" in low) or ("123" in out), \
            f"dur={dur:.0f}s out={out!r}"


# ═══════════════════════════════════════════════════════════════
# G) v0.6.9q — resize con normalización RGBA→RGB (bug "respondió vacío")
# ═══════════════════════════════════════════════════════════════


class TestResizeImagenRGBA:
    """_resize_image_b64 no debe fallar con imágenes RGBA (canal alfa)."""

    @staticmethod
    def _b64(img):
        import base64 as _b64
        import io as _io
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return _b64.b64encode(buf.getvalue()).decode("ascii")

    def test_rgba_grande_se_normaliza_a_rgb_jpeg(self):
        from src.agents.vision_agent import _resize_image_b64
        import base64 as _b64
        import io as _io
        from PIL import Image as _PIL
        img = _PIL.new("RGBA", (1600, 1000), (220, 30, 30, 255))
        out = _resize_image_b64(self._b64(img), max_dim=800)
        decoded = _PIL.open(_io.BytesIO(_b64.b64decode(out)))
        assert decoded.mode == "RGB"
        assert max(decoded.size) <= 800

    def test_rgba_pequena_se_deja_igual(self):
        from src.agents.vision_agent import _resize_image_b64
        from PIL import Image as _PIL
        img = _PIL.new("RGBA", (400, 300), (0, 128, 0, 128))
        b64 = self._b64(img)
        assert _resize_image_b64(b64, max_dim=800) == b64

    def test_base64_invalido_no_rompe(self):
        from src.agents.vision_agent import _resize_image_b64
        assert _resize_image_b64("no-valido") == "no-valido"


# ═══════════════════════════════════════════════════════════════
# F) FOTOS REALES (assets del repo) — clasificación de categorías
# ═══════════════════════════════════════════════════════════════
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets", "vision")

# (archivo, categoría esperada, palabras aceptadas en la respuesta)
_FOTOS_REALES = [
    ("persona_01.jpg", "persona", ("persona", "hombre", "mujer", "niño")),
    ("perro_01.jpg", "animal", ("perro", "animal")),
    ("gato_01.jpg", "animal", ("gato", "animal")),
    ("vehiculo_01.jpg", "vehiculo", ("vehículo", "vehiculo", "auto",
                                     "automóvil", "coche", "carro")),
    ("arbol_00.jpg", "vegetacion", ("árbol", "arbol", "vegetación",
                                    "vegetacion", "planta")),
]


@pytest.mark.skipif(not _E2E,
                    reason="seteá AXIOMA_VISION_E2E=1 para correr contra "
                           "Ollama real")
class TestVisionIntegracionFotosReales:
    """Clasificación de FOTOS REALES (assets en tests/assets/vision/).

    Verifica la capacidad de distinguir persona/animal/vehículo/vegetación
    en fotografías reales (no pictogramas). La imagen se envía sin nombre
    de archivo (sin sesgo de etiqueta). Atribución en tests/assets/vision/.
    """

    @pytest.fixture(autouse=True)
    def _disponible(self):
        if not _vision_e2e_disponible():
            pytest.skip("Ollama o modelo llm_vision_model no disponible")
        missing = [f for f, _, _ in _FOTOS_REALES
                   if not os.path.exists(os.path.join(_ASSETS_DIR, f))]
        if missing:
            pytest.skip(f"assets de fotos ausentes: {missing}")

    def _clasificar(self, foto: str) -> tuple[str, float]:
        import time
        from src.interfaces.web.vision_service import describe_image
        import base64
        path = os.path.join(_ASSETS_DIR, foto)
        b64 = base64.b64encode(open(path, "rb").read()).decode("ascii")
        q = ("¿El sujeto principal de esta foto es una persona, un animal, "
             "un vehículo o vegetación? Respondé con UNA palabra de esa "
             "lista.")
        t0 = time.time()
        out = describe_image(b64, q)
        return out.strip(), time.time() - t0

    @pytest.mark.parametrize(
        "foto,esperado,aceptadas", _FOTOS_REALES,
        ids=[f[0].replace(".jpg", "") for f in _FOTOS_REALES])
    def test_clasifica_foto_real(self, foto, esperado, aceptadas):
        out, dur = self._clasificar(foto)
        low = out.lower()
        assert any(k in low for k in aceptadas), \
            f"{foto}: esperaba {esperado}, VLM dijo {out!r} (dur={dur:.0f}s)"
