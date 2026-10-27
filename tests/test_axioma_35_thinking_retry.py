#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# test_axioma_35_thinking_retry.py
# ═══════════════════════════════════════════════════════════════
# "Thinking SÓLO si falla" (2026-10-27). Con `chat_disable_thinking` activo,
# un modelo con razonamiento puede gastar TODO `num_predict` en el "thinking" y
# devolver la respuesta VACÍA. Medido con qwen3.5:9b y 200 tokens: 200 de
# razonamiento y `response=""`. Antes eso llegaba al usuario como respuesta en
# blanco; ahora se reintenta UNA vez con `think: True` y el doble de presupuesto.
#
# Autorizado por el tope R13: `test_axioma_02_core.py` (946), `test_axioma_03`
# (948) y `test_axioma_22` (949) están al límite de 950 líneas.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm import client_base as cb  # noqa: E402


def _cliente(model="qwen3.5:9b"):
    """Cliente real sin red: el HTTP se reemplaza en cada test."""
    cli = cb.OllamaClientBase(model=model, base_url="http://127.0.0.1:9", timeout=5)
    cli.max_retries = 1
    cli.is_first_request = False
    return cli


class _SesionFalsa:
    def __init__(self):
        self.payloads = []

    def close(self):
        pass


def test_respuesta_fallida_detecta_vacio_y_solo_vacio():
    assert cb.OllamaClientBase._respuesta_fallida({"response": ""}) is True
    assert cb.OllamaClientBase._respuesta_fallida({"response": "   \n "}) is True
    assert cb.OllamaClientBase._respuesta_fallida({}) is True
    assert cb.OllamaClientBase._respuesta_fallida({"response": "3"}) is False


def test_generate_reintenta_con_thinking_si_la_respuesta_viene_vacia(monkeypatch):
    cli = _cliente()
    cli.is_first_request = False
    sesion = _SesionFalsa()
    monkeypatch.setattr(cli, "_get_client", lambda timeout=None: sesion)
    monkeypatch.setattr(cli, "_release_client", lambda s: None)
    payloads = []

    def _fake_datos(session, payload, timeout, token):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return {"response": "", "done": True, "eval_count": 200}
        return {"response": "3", "done": True, "eval_count": 40}

    monkeypatch.setattr(cb, "_datos_de_generacion", _fake_datos)

    r = cli.generate("cuantas horas?", max_tokens=100)

    assert r.content == "3"
    assert len(payloads) == 2, "tiene que haber reintentado una vez"
    assert payloads[0].get("think") is False, "el primer intento NO usa thinking"
    assert payloads[1].get("think") is True, "el reintento SÍ usa thinking"
    assert payloads[1]["options"]["num_predict"] > payloads[0]["options"]["num_predict"]


def test_generate_no_reintenta_si_hay_respuesta(monkeypatch):
    cli = _cliente()
    cli.is_first_request = False
    sesion = _SesionFalsa()
    monkeypatch.setattr(cli, "_get_client", lambda timeout=None: sesion)
    monkeypatch.setattr(cli, "_release_client", lambda s: None)
    llamadas = []

    def _fake_datos(session, payload, timeout, token):
        llamadas.append(payload)
        return {"response": "3", "done": True, "eval_count": 10}

    monkeypatch.setattr(cb, "_datos_de_generacion", _fake_datos)

    cli.generate("cuantas horas?", max_tokens=100)
    assert len(llamadas) == 1, "con respuesta válida no se reintenta"


def test_generate_no_reintenta_si_el_modelo_no_razona(monkeypatch):
    cli = _cliente(model="qwen2.5-coder:7b")
    cli.is_first_request = False
    sesion = _SesionFalsa()
    monkeypatch.setattr(cli, "_get_client", lambda timeout=None: sesion)
    monkeypatch.setattr(cli, "_release_client", lambda s: None)
    payloads = []

    def _fake_datos(session, payload, timeout, token):
        payloads.append(dict(payload))
        return {"response": "", "done": True, "eval_count": 5}

    monkeypatch.setattr(cb, "_datos_de_generacion", _fake_datos)

    cli.generate("hola", max_tokens=50)
    assert len(payloads) == 1, "sin flag think en el payload no se reintenta"
    assert "think" not in payloads[0]


@pytest.mark.parametrize("modelo", ["qwen3.5:9b", "qwen3:8b"])
def test_think_flag_apaga_el_razonamiento_en_la_familia_qwen3(modelo):
    """Con `chat_disable_thinking` activo (default), la familia qwen3 va sin thinking."""
    from config.settings import settings
    if not settings.chat_disable_thinking:
        pytest.skip("chat_disable_thinking desactivado en este entorno")
    assert cb.OllamaClientBase._think_flag(modelo) == {"think": False}


@pytest.mark.parametrize("modelo", ["qwen2.5-coder:7b", "llama3:8b"])
def test_think_flag_no_toca_modelos_sin_razonamiento(modelo):
    assert cb.OllamaClientBase._think_flag(modelo) == {}
