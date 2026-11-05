#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# MODELOS POR ROL — elegir, instalar, borrar (2026-11-05)
# Pedido del usuario: el chat no se instala solo; el usuario elige de la lista, puede
# cambiar el modelo de un rol y eliminar el que no le convenció.
# ═══════════════════════════════════════════════════════════════
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _env(tmp_path, texto: str) -> Path:
    archivo = tmp_path / ".env"
    archivo.write_text(texto, encoding="utf-8")
    return archivo


def test_asignar_rol_cambia_solo_su_linea(tmp_path):
    from src.core import model_seleccion
    archivo = _env(tmp_path, "OTRA=COSA\nLLM_DEFAULT_MODEL=viejo:1b\nLLM_CODE_MODEL=coder:7b\n")
    model_seleccion.asignar("chat", "nuevo:9b", archivo)
    texto = archivo.read_text(encoding="utf-8")
    assert "LLM_DEFAULT_MODEL=nuevo:9b" in texto
    assert "OTRA=COSA" in texto and "LLM_CODE_MODEL=coder:7b" in texto
    assert texto.count("LLM_DEFAULT_MODEL=") == 1
    assert (tmp_path / ".env.bak").exists()
    assert model_seleccion.actuales(archivo)["chat"] == "nuevo:9b"


def test_asignar_rol_agrega_la_variable_si_falta(tmp_path):
    from src.core import model_seleccion
    archivo = _env(tmp_path, "OTRA=COSA\n")
    model_seleccion.asignar("codigo", "coder:7b", archivo)
    assert "LLM_CODE_MODEL=coder:7b" in archivo.read_text(encoding="utf-8")


@pytest.mark.parametrize("modelo", ["", "  ", "con\nsalto", "con\rretorno"])
def test_asignar_rechaza_modelos_invalidos(tmp_path, modelo):
    from src.core import model_seleccion
    with pytest.raises(ValueError):
        model_seleccion.asignar("chat", modelo, _env(tmp_path, ""))


def test_asignar_rechaza_rol_desconocido(tmp_path):
    from src.core import model_seleccion
    with pytest.raises(ValueError):
        model_seleccion.asignar("embeddings", "x", _env(tmp_path, ""))


def test_en_uso_devuelve_los_roles_del_modelo(tmp_path):
    from src.core import model_seleccion
    archivo = _env(tmp_path, "LLM_DEFAULT_MODEL=qwen3.5:9b\nLLM_VISION_MODEL=qwen3.5:9b\n")
    assert set(model_seleccion.en_uso("qwen3.5:9b", archivo)) == {"chat", "vision"}
    assert model_seleccion.en_uso("otro:1b", archivo) == []


def test_catalogo_tiene_alternativas_de_chat_para_elegir():
    from config.model_catalog import cargar_catalogo
    modelos = cargar_catalogo().modelos
    for nombre in ("llama3.1:8b", "mistral:7b", "llama3.2:1b"):
        assert nombre in modelos, f"falta la alternativa {nombre}"
        assert "chat" in modelos[nombre].roles
        assert modelos[nombre].nota, f"{nombre} debería aclarar que el tamaño es aproximado"


def test_catalogo_avisa_que_el_9b_es_el_unico_con_vision():
    from config.model_catalog import cargar_catalogo
    modelo = cargar_catalogo().modelos["qwen3.5:9b"]
    assert "visión" in modelo.nota.lower()


def test_la_ui_muestra_la_version_real_y_no_un_literal():
    """2026-11-05 (medido en el navegador): la cabecera decía v0.1.8 con la versión en 0.7.8."""
    import re
    from src import __version__

    base = Path(__file__).resolve().parent.parent / "src" / "interfaces" / "web"
    for nombre in ("app_ui.py", "app.py"):
        texto = (base / nombre).read_text(encoding="utf-8")
        literales = re.findall(r"AXIOMA v[0-9]", texto)
        assert not literales, f"{nombre}: versión hardcodeada {literales}"
        assert "AXIOMA_VERSION" in texto, f"{nombre}: no usa la versión única"
    assert __version__ == "0.7.8" or re.match(r"^\d+\.\d+\.\d+$", __version__)
