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
