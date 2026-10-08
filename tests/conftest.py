#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/conftest.py
# Autor: SaintWick
# AXIOMA — tests/conftest.py
# ✅ v3.1.0 (2026-08-25): Suite integral.
# - Registra markers.
# - Al final de la sesión escribe el resumen en logs/axioma_test_report_*.md
#   (vía tests/axioma_report.py).
# - collect_ignore: los 3 diagnósticos de ENTORNO REAL (Ollama/audio/daemon)
#   movidos desde la raíz NO se recolectan como tests pytest — requieren
#   hardware/modelos reales y ralentizarían/romperían la suite. Se ejecutan
#   a mano (python tests/test_*.py).
# ═══════════════════════════════════════════════════════════════
collect_ignore = [
    "test_audio_diagnostic.py",
    "test_code_contract_e2e.py",
    "test_rafael_bridge.py",
    # ✅ 2026-10-01 (ISSUE-121): `test_netron.py` tiene 164 tests y los 157 activos
    # PASAN (7 saltan, 52 s, offline), pero **netron y actron son herramientas que
    # todavía NO están integradas al proyecto** (decisión del usuario): sus tests no
    # pertenecen a la suite de AXIOMA. Quedan como diagnóstico MANUAL:
    #     venv/bin/python -m pytest tests/test_netron.py -q      # 157 passed, 7 skipped
    # Si algún día se integran al runtime, se quitan de esta lista y entran a la suite.
    "test_netron.py",
]

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

from config.settings import settings

try:
    from .axioma_report import finalize
    _REPORT_AVAILABLE = True
except Exception:  # pragma: no cover
    try:
        from axioma_report import finalize
        _REPORT_AVAILABLE = True
    except Exception:
        _REPORT_AVAILABLE = False


# ── Markers ──────────────────────────────────────────────────────────────────
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: llama Ollama real")
    config.addinivalue_line("markers", "integration: tests con dependencias reales")
    config.addinivalue_line("markers", "unit: tests unitarios con mocks")


# ── Cierre: escribir resumen final del reporte en logs/ ──────────────────────
def pytest_sessionfinish(session, exitstatus):
    if not _REPORT_AVAILABLE:
        return
    try:
        from .axioma_report import totals, report_path
    except Exception:
        from axioma_report import totals, report_path
    try:
        t = totals()
        # ✅ v0.7.4 (ISSUE-092): si hubo checks FALLIDOS, la corrida NO puede salir
        # con exitstatus 0. `check()` solo registra (muchos tests no tienen
        # `assert`), así que un check fallido dejaba el archivo en ❌ para el gate
        # **pero pytest salía 0** ⇒ la CI bloqueaba con "17/18 archivos" y sin
        # rastro en la consola (medido: ~50 tests con `check()` y 0 asserts).
        fallidos = int(t.get("failed", 0))
        if fallidos and not exitstatus:
            session.exitstatus = 1        # ← pytest usa esto como código de salida
            exitstatus = 1
            print(f"\n❌ {fallidos} check(s) FALLIDOS → exitstatus=1 "
                  f"(el gate bloquea por archivo; ver el reporte para el detalle)")
        extra = (
            f"**Ejecución pytest:** exitstatus={exitstatus} — "
            f"tests recolectados={len(session.items)}"
        )
        finalize(extra=extra, pytest_exitstatus=exitstatus)
        print(f"\n📄 Reporte generado: {report_path()}")
    except Exception as e:  # pragma: no cover
        print(f"⚠️  No se pudo finalizar el reporte: {e}")


# ── Fixtures base (suite integral) ───────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def models():
    return {
        "default":    settings.llm_default_model,
        "code":       settings.llm_code_model,
        "vision":     settings.llm_vision_model,
        "embed":      settings.llm_embed_model,
        "judge":      settings.llm_judge_model,
        "fallback_1": settings.llm_fallback_1,
        "fallback_2": "",
    }


@pytest.fixture
def mock_ollama_response():
    r = MagicMock()
    r.content           = "Respuesta de test mock"
    r.model             = settings.llm_default_model
    r.done              = True
    r.eval_count        = 42
    r.prompt_eval_count = 10
    r.total_duration    = 1.5
    r.load_duration     = 0.1
    r.eval_duration     = 1.4
    r.raw               = {}
    return r


# ═══════════════════════════════════════════════════════════════
# OLLAMA FALSO — para poder probar los caminos reales sin servidor (fase 6)
# ═══════════════════════════════════════════════════════════════
# Medido el 2026-10-07/08: en el CI, `OLLAMA_HOST` apunta a un puerto muerto
# (`http://127.0.0.1:1`), así que las pruebas que necesitan el servidor se SALTAN (por ejemplo
# `test_axioma_02_core.py::restore_default()`) y el camino real del cliente nunca se ejercita.
#
# En vez de agregar una dependencia (como `respx`, que propuso el plan), este falso es un servidor HTTP
# de verdad de la biblioteca estándar: contesta lo mismo que Ollama y **el código del proyecto hace
# pedidos HTTP reales** contra él (mejor prueba que reemplazar el cliente por un doble). No necesita
# red externa ni puertos fijos: escucha en un puerto libre de `127.0.0.1` y se apaga al terminar.

class _OllamaFalso:
    """Servidor que imita a Ollama: `/api/tags`, `/api/chat` y `/api/pull`."""

    def __init__(self, fallar: bool = False) -> None:
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.pedidos: list = []          # (método, camino, cuerpo) de lo que le pidieron
        self.fallar = fallar             # si está en True, contesta 500 (para probar el error)
        falso = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):    # sin ruido en la salida de las pruebas
                pass

            def _responder(self, datos, lineas_ndjson=False):
                cuerpo = (b"".join(_json.dumps(l).encode() + b"\n" for l in datos)
                          if lineas_ndjson else _json.dumps(datos).encode())
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson" if lineas_ndjson
                                 else "application/json")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def _leer_cuerpo(self):
                largo = int(self.headers.get("Content-Length") or 0)
                return _json.loads(self.rfile.read(largo) or b"{}")

            def do_GET(self):
                falso.pedidos.append(("GET", self.path, {}))
                if falso.fallar:
                    self.send_error(500, "falso: fallo a propósito")
                    return
                if self.path.startswith("/api/tags"):
                    self._responder({"models": [{"name": "falso:1b", "size": 1}]})
                elif self.path.startswith("/api/version"):
                    self._responder({"version": "0.0.0-falso"})
                else:
                    self.send_error(404)

            def do_POST(self):
                cuerpo = self._leer_cuerpo()
                falso.pedidos.append(("POST", self.path, cuerpo))
                if falso.fallar:
                    self.send_error(500, "falso: fallo a propósito")
                    return
                if self.path.startswith("/api/chat"):
                    self._responder({"model": cuerpo.get("model", ""),
                                     "message": {"role": "assistant",
                                                 "content": "respuesta del Ollama falso"},
                                     "done": True})
                elif self.path.startswith("/api/pull"):
                    # Igual que Ollama: varios eventos y el último con el estado final.
                    self._responder([{"status": "pulling manifest"},
                                     {"status": "downloading", "total": 100, "completed": 50},
                                     {"status": "downloading", "total": 100, "completed": 100},
                                     {"status": "success"}], lineas_ndjson=True)
                else:
                    self.send_error(404)

        self._servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._servidor.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._servidor.server_address[1]}"
        self._hilo = threading.Thread(target=self._servidor.serve_forever, daemon=True)
        self._hilo.start()

    def parar(self) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()


@pytest.fixture
def ollama_falso():
    """Un Ollama de mentira, por HTTP de verdad (ver el comentario de arriba)."""
    falso = _OllamaFalso()
    try:
        yield falso
    finally:
        falso.parar()


@pytest.fixture
def ollama_falso_que_falla():
    """El mismo falso, pero contestando error: para probar los caminos de falla."""
    falso = _OllamaFalso(fallar=True)
    try:
        yield falso
    finally:
        falso.parar()
