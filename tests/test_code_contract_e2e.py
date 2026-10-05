#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tests/test_code_contract_e2e.py
# Autor: SaintWick
# test_code_contract_e2e.py — Test de integración end-to-end
# Ruta sugerida: /ruta/a/axioma/test_code_contract_e2e.py
# Versión: 0.1.0
#
# Prueba el sistema de Contrato de Código Verificable (Fases 0-8 +
# refactor de dispatcher.py) contra el AXIOMA REAL — no un sandbox de
# prueba. Usa tools.detailed_logger (el logger real del proyecto) para
# loggear cada paso en logs/reg_error.txt, y además genera un informe
# propio en Markdown (más legible que el log crudo) al final.
#
# Corre en 3 secciones, de menor a mayor dependencia externa:
#   A. Imports y estructura — rápido, sin Ollama ni red
#   B. Lógica de negocio — usa el sandbox REAL de esta PC, sin Ollama
#   C. Pipeline completo — requiere Ollama corriendo (se salta solo si
#      no detecta el servicio, no hace falta pasarle ningún flag)
#
# Uso:
#   cd /ruta/a/axioma
#   python3 test_code_contract_e2e.py
#   python3 test_code_contract_e2e.py --skip-ollama   # solo A y B
#
# Salida:
#   - logs/reg_error.txt         (log detallado de siempre, vía DetailedLogger)
#   - logs/code_contract_report_<timestamp>.md   (informe de este test)
#   - stdout con resumen en vivo mientras corre
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import sys
import time
import json
import argparse
import traceback
import socket
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, List, Optional

# Asegura que el proyecto esté en sys.path — este script se espera que
# corra desde la raíz de axioma (mismo nivel que src/, config/, tools/).
_PROJECT_ROOT = Path(__file__).resolve().parent
# ✅ 2026-08-25: localizado en tests/ → subir un nivel si falta tools/
if not (_PROJECT_ROOT / "tools").exists():
    _PROJECT_ROOT = _PROJECT_ROOT.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from tools.detailed_logger import get_logger, initialize_logger, close_logger, LoggerConfig
except ImportError as e:
    print(f"❌ No se pudo importar tools.detailed_logger: {e}")
    print("   Corré este script desde la raíz de axioma (donde está la carpeta tools/).")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════
# INFRAESTRUCTURA DEL TEST
# ═══════════════════════════════════════════════════════════════
@dataclass
class TestResult:
    section: str
    name: str
    passed: bool
    duration_ms: float
    detail: str = ""
    error: Optional[str] = None
    error_trace: Optional[str] = None
    skipped: bool = False
    skip_reason: str = ""
    extended: str = ""  # diagnóstico largo (JSON/código) — va en su propia sección del informe, no en la tabla


class TestSuite:
    """
    Corre funciones de test una por una, loggeando cada paso con el
    DetailedLogger real de AXIOMA y acumulando resultados para el
    informe final. Un test que crashea NO tumba el resto — se captura,
    se loggea con logger.log_error() (igual que un error real de
    AXIOMA), y sigue con el siguiente.
    """

    def __init__(self, logger: Any):
        self.logger = logger
        self.results: List[TestResult] = []
        self._current_section = ""

    def section(self, name: str) -> None:
        self._current_section = name
        print(f"\n{'─' * 70}\n{name}\n{'─' * 70}")
        self.logger.log_event("TEST_SECTION", f"═══ {name} ═══")

    def run(self, name: str, fn: Callable[[], Any]) -> None:
        start = time.time()
        self.logger.log_event("TEST", f"INICIO: {name}", extra={"section": self._current_section})
        try:
            result = fn()
            if isinstance(result, tuple):
                detail, extended = result
            else:
                detail, extended = (result or ""), ""
            duration = (time.time() - start) * 1000
            self.results.append(TestResult(
                section=self._current_section, name=name, passed=True,
                duration_ms=duration, detail=detail, extended=extended,
            ))
            self.logger.log_event(
                "TEST", f"PASS: {name}",
                extra={"duration_ms": round(duration, 1), "detail": detail[:200]},
            )
            print(f"  ✅ PASS  {name}  ({duration:.0f}ms)" + (f" — {detail}" if detail else ""))
        except Exception as e:
            duration = (time.time() - start) * 1000
            err = f"{type(e).__name__}: {e}"
            self.results.append(TestResult(
                section=self._current_section, name=name, passed=False,
                duration_ms=duration, error=err, error_trace=traceback.format_exc(),
            ))
            self.logger.log_error(e, context=f"test_code_contract_e2e: {name}")
            print(f"  ❌ FAIL  {name}  ({duration:.0f}ms)  — {err}")

    def skip(self, name: str, reason: str) -> None:
        self.results.append(TestResult(
            section=self._current_section, name=name, passed=True,
            duration_ms=0.0, skipped=True, skip_reason=reason,
        ))
        self.logger.log_event("TEST", f"SKIP: {name}", extra={"reason": reason})
        print(f"  ⏭️  SKIP  {name}  ({reason})")

    @property
    def counts(self) -> dict:
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed and not r.skipped)
        failed = sum(1 for r in self.results if not r.passed)
        skipped = sum(1 for r in self.results if r.skipped)
        return {"total": total, "passed": passed, "failed": failed, "skipped": skipped}


def _ollama_available(host: str = "localhost", port: int = 11434, timeout: float = 1.5) -> bool:
    """Chequeo rápido de socket — no requiere el módulo requests/httpx."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dump_json(obj: Any, max_chars: int = 4000) -> str:
    """JSON legible y capado — para meter dumps grandes (metadata, contrato,
    reporte de validación) en el informe sin que se vuelva ilegible."""
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        return f"<no serializable: {e}>"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [{len(text) - max_chars} caracteres más, truncado]"
    return text


def _system_diagnostics() -> dict:
    """
    Info de entorno que ayuda a diagnosticar sin tener que preguntar de
    vuelta — versiones, modelos de Ollama disponibles. Todo de solo
    lectura, con timeout corto, nunca falla el test si algo no está.
    """
    info: dict = {"python": sys.version.split()[0]}

    try:
        import nicegui
        info["nicegui"] = getattr(nicegui, "__version__", "desconocida")
    except Exception as e:
        info["nicegui"] = f"no disponible: {e}"

    try:
        import pytest as _pytest
        info["pytest"] = _pytest.__version__
    except Exception as e:
        info["pytest"] = f"no disponible: {e}"

    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5,
        )
        info["ollama_list"] = result.stdout.strip() if result.returncode == 0 else f"error: {result.stderr.strip()}"
    except Exception as e:
        info["ollama_list"] = f"no disponible: {e}"

    return info


# ═══════════════════════════════════════════════════════════════
# SECCIÓN A — IMPORTS Y ESTRUCTURA
# ═══════════════════════════════════════════════════════════════
def test_import_code_contract() -> str:
    from src.agents.code_contract import CodeFile, CodeContract, parse_code_contract, build_contract_critique
    assert callable(parse_code_contract)
    assert callable(build_contract_critique)
    return "CodeFile, CodeContract, parse_code_contract, build_contract_critique OK"


def test_import_code_validator() -> str:
    from src.agents.code_validator import CodeValidator, ValidationReport, LayerResult, build_validation_critique
    assert callable(build_validation_critique)
    return "CodeValidator, ValidationReport, LayerResult, build_validation_critique OK"


def test_settings_contract_fields() -> str:
    from config.settings import settings
    fields = [
        "code_contract_enabled", "code_require_tests", "code_require_dependencies",
        "code_require_commands", "code_require_expected_output",
        "code_contract_max_retries", "code_generation_max_tokens",
        "code_validation_timeout",
    ]
    missing = [f for f in fields if not hasattr(settings, f)]
    assert not missing, f"faltan en settings: {missing}"
    return f"{len(fields)} campos de contrato presentes en settings"


def test_dispatcher_split_mro() -> str:
    """
    Confirma que el refactor de dispatcher.py/dispatcher_process.py
    compone bien contra el código REAL (no contra stubs, como en el
    harness de la sesión de desarrollo) — importa el Dispatcher real
    de este proyecto y chequea el MRO + métodos de los 4 mixins.
    """
    from src.core.dispatcher import Dispatcher
    mro_names = [c.__name__ for c in Dispatcher.__mro__]
    required_mixins = [
        "DispatcherDistributedMixin", "DispatcherHandlersMixin",
        "DispatcherOperationsMixin", "DispatcherProcessMixin",
    ]
    missing_mixins = [m for m in required_mixins if m not in mro_names]
    assert not missing_mixins, f"faltan mixins en el MRO: {missing_mixins}"

    required_methods = ["process", "_dispatch", "_handler_name", "_handle_jarvis", "_handle_code", "_record_metric"]
    missing_methods = [m for m in required_methods if not hasattr(Dispatcher, m)]
    assert not missing_methods, f"faltan métodos: {missing_methods}"
    return f"MRO OK ({' → '.join(mro_names)}), {len(required_methods)} métodos accesibles"


def test_trace_store_schema() -> str:
    """
    Abre (o crea) la traces.db real del proyecto y confirma que las 4
    columnas de Fase 7 existen — corre la migración real (_migrate_schema)
    contra la base real, no una copia de prueba.
    """
    from src.learning.trace_store import get_trace_store
    store = get_trace_store()
    conn = store._pool.acquire(timeout=5.0)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(traces)").fetchall()]
    finally:
        store._pool.release(conn)
    required = ["code_contract_complete", "code_validation_passed", "code_retries", "code_truncated"]
    missing = [c for c in required if c not in cols]
    assert not missing, f"faltan columnas en traces.db: {missing}"
    return f"traces.db con {len(cols)} columnas, las 4 de Fase 7 presentes"


# ═══════════════════════════════════════════════════════════════
# SECCIÓN B — LÓGICA DE NEGOCIO (sandbox real, sin Ollama)
# ═══════════════════════════════════════════════════════════════
def test_contract_parsing_completo() -> str:
    from src.agents.code_contract import parse_code_contract

    class _S:
        code_require_dependencies = False
        code_require_tests = False
        code_require_commands = False
        code_require_expected_output = False

    resp = (
        "CÓDIGO:\nARCHIVO: suma.py\n```python\n"
        "def suma(a: int, b: int) -> int:\n    return a + b\n```\n\n"
        "TESTS:\nARCHIVO: test_suma.py\n```python\n"
        "from suma import suma\ndef test_suma():\n    assert suma(2, 2) == 4\n```\n\n"
        "COMANDO:\npytest test_suma.py -v\n"
    )
    contract = parse_code_contract(resp, _S(), query="suma de dos numeros")
    assert contract.complete, f"debería estar completo, missing={contract.missing}"
    assert contract.files[0].name == "suma.py"
    assert contract.test_files[0].name == "test_suma.py"
    return f"contrato completo, {len(contract.files)} archivo(s), {len(contract.test_files)} test(s)"


def test_static_validation_syntax_error() -> str:
    from src.agents.code_contract import parse_code_contract
    from src.agents.code_validator import CodeValidator
    from src.tools_mcp.sandbox import get_sandbox_executor
    from config.settings import settings

    class _S:
        code_require_dependencies = False
        code_require_tests = False
        code_require_commands = False
        code_require_expected_output = False

    resp = "CÓDIGO:\nARCHIVO: roto.py\n```python\ndef roto(:\n    return 1\n```\n"
    contract = parse_code_contract(resp, _S(), query="roto")
    validator = CodeValidator(sandbox=get_sandbox_executor(), settings=settings)
    report = validator.validate(contract)
    assert not report.passed, "un SyntaxError no debería pasar la capa estática"
    assert report.layers[-1].layer == "static"
    return f"SyntaxError detectado correctamente en capa estática: {report.error_summary[:100]}"


def test_dynamic_validation_sandbox_real() -> str:
    """
    El test más valioso de esta suite — corre pytest DE VERDAD en el
    sandbox real de esta PC (execute_script, modo STRICT). Esto no se
    pudo probar en la sesión de desarrollo porque sandbox_staging.py
    no estaba disponible ahí; acá corre contra el sandbox real completo.
    """
    from src.agents.code_contract import parse_code_contract
    from src.agents.code_validator import CodeValidator
    from src.tools_mcp.sandbox import get_sandbox_executor
    from config.settings import settings

    class _S:
        code_require_dependencies = False
        code_require_tests = False
        code_require_commands = False
        code_require_expected_output = False

    resp = (
        "CÓDIGO:\nARCHIVO: factorial.py\n```python\n"
        "def factorial(n: int) -> int:\n"
        "    return 1 if n == 0 else n * factorial(n - 1)\n```\n\n"
        "TESTS:\nARCHIVO: test_factorial.py\n```python\n"
        "from factorial import factorial\n"
        "def test_factorial():\n    assert factorial(5) == 120\n```\n"
    )
    contract = parse_code_contract(resp, _S(), query="factorial")
    validator = CodeValidator(sandbox=get_sandbox_executor(timeout=settings.code_validation_timeout), settings=settings)
    report = validator.validate(contract)
    assert report.passed, f"debería pasar — layers: {[(l.layer, l.passed, l.errors) for l in report.layers]}"
    dynamic = next((l for l in report.layers if l.layer == "dynamic"), None)
    assert dynamic is not None and dynamic.passed, "la capa dinámica no corrió o no pasó"
    return "pytest real corrió en el sandbox real de esta PC y pasó"


def test_dynamic_validation_test_que_falla() -> str:
    """Mismo mecanismo que el anterior, pero con un bug real a propósito."""
    from src.agents.code_contract import parse_code_contract
    from src.agents.code_validator import CodeValidator
    from src.tools_mcp.sandbox import get_sandbox_executor
    from config.settings import settings

    class _S:
        code_require_dependencies = False
        code_require_tests = False
        code_require_commands = False
        code_require_expected_output = False

    resp = (
        "CÓDIGO:\nARCHIVO: resta.py\n```python\n"
        "def resta(a: int, b: int) -> int:\n    return a + b\n```\n\n"  # bug: debería ser a - b
        "TESTS:\nARCHIVO: test_resta.py\n```python\n"
        "from resta import resta\n"
        "def test_resta():\n    assert resta(5, 3) == 2\n```\n"
    )
    contract = parse_code_contract(resp, _S(), query="resta")
    validator = CodeValidator(sandbox=get_sandbox_executor(timeout=settings.code_validation_timeout), settings=settings)
    report = validator.validate(contract)
    assert not report.passed, "un test que falla de verdad no debería pasar"
    assert "FAILED" in report.error_summary
    return f"fallo real detectado y resumido: {report.error_summary[:100]}"


def test_classifier_weather_false_positives() -> str:
    from src.router.classifier_api import APIDetector

    detector = APIDetector(enable_api_integration=True)
    prosa_tecnica = [
        "el tiempo de ejecución de esta función es muy alto",
        "cuánto tiempo tarda este algoritmo en procesar los datos",
        "necesito optimizar el tiempo de respuesta de esta consulta sql",
    ]
    clima_real = [
        "qué tiempo hace en Buenos Aires",
        "clima en Madrid",
        "va a llover mañana",
    ]
    for q in prosa_tecnica:
        assert detector._is_weather_query(q) is False, f"falso positivo NO corregido: {q!r}"
    for q in clima_real:
        assert detector._is_weather_query(q) is True, f"regresión en clima real: {q!r}"
    return f"{len(prosa_tecnica)} falsos positivos corregidos, {len(clima_real)} casos de clima real sin regresión"


# ✅ 2026-10-01: `_TEST_PROMPT` se usaba en 3 pruebas e2e (líneas 470/479/511) pero
# NUNCA se definió ⇒ esas pruebas morían con `NameError`. Lo detectó `ruff --select
# F821` al extender el linter a `tests/` (antes solo miraba `src` y `tools`).
_TEST_PROMPT = (
    "Escribí una función Python `sumar(a, b)` que devuelva la suma y explicá en una "
    "frase qué hace."
)


def test_geocoding_negative_cache_ttl() -> str:
    """✅ 2026-10-01: la capa de clima+geocoding se movió en el REFACTOR 50/50.

    El test buscaba `_geocoding_cache_negative_ts` en `src.handlers.data_fetchers`,
    pero esa variable vive ahora en `src/handlers/data_fetchers_weather.py` (donde el
    refactor movió la capa de clima). **La feature NO se perdió**: el test estaba
    desactualizado y por eso este archivo quedó fuera de la suite sin que nadie lo
    notara. Se corrige apuntando al módulo real.
    """
    from src.handlers import data_fetchers_weather as dfw
    assert hasattr(dfw, "_geocoding_cache_negative_ts"), (
        "la caché negativa de geocoding debe vivir en data_fetchers_weather")
    return "TTL negativo de geocoding presente en el módulo correcto"


def _summarize_response(response: Any, logger: Any, log_label: str) -> tuple:
    """
    Arma detail (corto, para la tabla) + extended (JSON completo, para
    la sección de diagnóstico) a partir de un Response real — usado por
    los dos tests de Sección C para no duplicar la lógica de resumen.
    También lo manda al log real (reg_error.txt) vía logger.log_event,
    no solo al informe.
    """
    meta = response.metadata or {}
    code_artifact = meta.get("code_artifact")
    validation_report = meta.get("validation_report")

    detail_parts = [f"task_type={response.task_type}", f"model={response.model_used}"]
    if code_artifact:
        detail_parts.append(f"archivos={len(code_artifact.get('files', []))}")
        detail_parts.append(f"contrato_completo={code_artifact.get('complete')}")
    if validation_report:
        detail_parts.append(f"validacion_passed={validation_report.get('passed')}")
    detail_parts.append(f"validation_passed(response)={response.validation_passed}")
    detail = " | ".join(detail_parts)

    extended_obj = {
        "task_type": response.task_type,
        "model_used": response.model_used,
        "latency_ms": response.latency_ms,
        "validation_passed": response.validation_passed,
        "error": response.error,
        "code_artifact": code_artifact,
        "validation_report": validation_report,
        "metadata_keys": sorted(meta.keys()),
        "content_preview": (response.content or "")[:1500],
    }
    extended = _dump_json(extended_obj)

    logger.log_event(log_label, "Response completo", extra={
        "task_type": response.task_type, "model_used": response.model_used,
        "validation_passed": response.validation_passed,
        "has_code_artifact": code_artifact is not None,
        "has_validation_report": validation_report is not None,
    })
    return detail, extended


def test_dispatcher_code_generation_real() -> tuple:
    """
    El test de mayor alcance: instancia el Dispatcher real y le pide
    generar código de verdad, a través de TODO el pipeline — incluido el
    ruteo (clasificación + _should_use_autonomous_mode()). Si el mensaje
    dispara modo autónomo, este test NO ejercita el contrato de código
    (Fases 1-4) — ver test_handle_code_direct_real() para eso. Acá se
    reporta explícitamente cuál de los dos caminos se usó, en vez de
    dejarlo para inferir del task_type de la respuesta.
    """
    import asyncio
    from src.core.dispatcher import Dispatcher
    from src.domain.entities import Message

    logger = get_logger()
    dispatcher = Dispatcher()
    message = Message(content=_TEST_PROMPT)

    autonomous_precheck = "método no encontrado"
    if hasattr(dispatcher, "_should_use_autonomous_mode"):
        try:
            autonomous_precheck = str(dispatcher._should_use_autonomous_mode(message.content))
        except Exception as e:
            autonomous_precheck = f"error al chequear: {type(e).__name__}: {e}"
    logger.log_event("TEST_DIAGNOSTIC", "_should_use_autonomous_mode() pre-check",
                     extra={"prompt": _TEST_PROMPT, "result": autonomous_precheck})

    response = asyncio.run(dispatcher.process(message, session_id="test_e2e_session"))

    assert response is not None, "process() devolvió None"
    assert response.error is None, (
        f"error en la respuesta: {response.error} | "
        f"_should_use_autonomous_mode()={autonomous_precheck}"
    )
    assert response.content, "respuesta sin contenido"

    detail, extended = _summarize_response(response, logger, "TEST_DISPATCHER_PROCESS")
    detail = f"autonomous_precheck={autonomous_precheck} | {detail}"
    if response.task_type == "code_autonomous":
        detail = "⚠️ RUTEADO A MODO AUTÓNOMO (no ejercita Fases 1-4) | " + detail
    return detail, extended


def test_handle_code_direct_real() -> tuple:
    """
    Llama _handle_code() DIRECTO — sin pasar por process()/_dispatch()
    ni por _should_use_autonomous_mode(). Garantiza que se ejercite el
    contrato de código + validación multicapa + repair loop (Fases 1-4)
    sin depender de qué decida la heurística de ruteo. Es el test más
    directo de lo que se construyó en esta sesión.
    """
    import asyncio
    from src.core.dispatcher import Dispatcher
    from src.domain.entities import Message

    logger = get_logger()
    dispatcher = Dispatcher()
    message = Message(content=_TEST_PROMPT)

    response = asyncio.run(dispatcher._handle_code(
        message, "code_generation", "test_e2e_direct_session", None,
    ))

    assert response is not None, "_handle_code() devolvió None"
    assert response.error is None, f"error en la respuesta: {response.error}"
    assert response.content, "respuesta sin contenido"

    detail, extended = _summarize_response(response, logger, "TEST_HANDLE_CODE_DIRECT")
    return detail, extended


# ═══════════════════════════════════════════════════════════════
# INFORME
# ═══════════════════════════════════════════════════════════════
def _write_report(suite: TestSuite, report_path: Path, ollama_skipped: bool, system_diag: dict) -> None:
    counts = suite.counts
    lines: List[str] = []
    lines.append("# Informe — Test de Integración Contrato de Código Verificable")
    lines.append("")
    lines.append(f"**Fecha:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"**Python:** {sys.version.split()[0]}")
    lines.append(f"**Proyecto:** {_PROJECT_ROOT}")
    lines.append("")
    lines.append("## Diagnóstico de sistema")
    lines.append("")
    lines.append(f"- NiceGUI: `{system_diag.get('nicegui')}`")
    lines.append(f"- pytest: `{system_diag.get('pytest')}`")
    lines.append("- `ollama list`:")
    lines.append("```")
    lines.append(str(system_diag.get("ollama_list", "")))
    lines.append("```")
    lines.append("")
    lines.append("## Resumen")
    lines.append("")
    lines.append(f"- ✅ Pasaron: **{counts['passed']}**")
    lines.append(f"- ❌ Fallaron: **{counts['failed']}**")
    lines.append(f"- ⏭️ Saltados: **{counts['skipped']}**")
    lines.append(f"- Total: {counts['total']}")
    if ollama_skipped:
        lines.append("")
        lines.append("> ⚠️ Ollama no detectado en localhost:11434 — Sección C saltada. "
                     "Corré `ollama serve` y volvé a ejecutar este test para cubrirla.")
    lines.append("")

    current_section = None
    for r in suite.results:
        if r.section != current_section:
            current_section = r.section
            lines.append(f"## {current_section}")
            lines.append("")
            lines.append("| Test | Resultado | Duración | Detalle |")
            lines.append("|---|---|---|---|")
        if r.skipped:
            status = "⏭️ SKIP"
            detail = r.skip_reason
        elif r.passed:
            status = "✅ PASS"
            detail = r.detail
        else:
            status = "❌ FAIL"
            detail = r.error or ""
        lines.append(f"| {r.name} | {status} | {r.duration_ms:.0f}ms | {detail} |")

    failed_results = [r for r in suite.results if not r.passed]
    if failed_results:
        lines.append("")
        lines.append("## Detalle de fallos")
        for r in failed_results:
            lines.append("")
            lines.append(f"### {r.name}")
            lines.append(f"**Sección:** {r.section}")
            lines.append("```")
            lines.append(r.error_trace or r.error or "(sin traceback)")
            lines.append("```")

    extended_results = [r for r in suite.results if r.extended]
    if extended_results:
        lines.append("")
        lines.append("## Diagnóstico extendido")
        lines.append("")
        lines.append("Dump completo — metadata, contrato de código, reporte de "
                     "validación y preview de la respuesta. Para no tener que ir a "
                     "buscarlo a reg_error.txt.")
        for r in extended_results:
            lines.append("")
            lines.append(f"### {r.name}")
            lines.append("```json")
            lines.append(r.extended)
            lines.append("```")

    lines.append("")
    lines.append("## Log completo")
    lines.append("")
    lines.append(f"Detalle línea por línea (incluye variables locales redactadas, "
                 f"cadena de causa completa) en: `{LoggerConfig.OUTPUT_FILE}`")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-ollama", action="store_true",
                        help="Salta la Sección C (requiere Ollama corriendo)")
    args = parser.parse_args()

    print("🧪 Test de integración — Contrato de Código Verificable (AXIOMA)")
    print(f"   Proyecto: {_PROJECT_ROOT}")

    if not LoggerConfig.LOGS_DIR.exists():
        print(f"❌ ERROR: {LoggerConfig.LOGS_DIR} no existe — creala con: mkdir -p {LoggerConfig.LOGS_DIR}")
        return 1

    if not initialize_logger(rotate=False):
        print("❌ ERROR: no se pudo inicializar DetailedLogger")
        return 1

    logger = get_logger()
    suite = TestSuite(logger)

    with logger.track_execution("test_code_contract_e2e", "Suite completa de integración"):
        suite.section("A. Imports y estructura")
        suite.run("import code_contract.py", test_import_code_contract)
        suite.run("import code_validator.py", test_import_code_validator)
        suite.run("settings — campos de contrato", test_settings_contract_fields)
        suite.run("dispatcher.py + dispatcher_process.py — MRO real", test_dispatcher_split_mro)
        suite.run("trace_store.py — schema/migración real", test_trace_store_schema)

        suite.section("B. Lógica de negocio (sandbox real, sin Ollama)")
        suite.run("parse_code_contract — contrato completo", test_contract_parsing_completo)
        suite.run("CodeValidator — SyntaxError detectado", test_static_validation_syntax_error)
        suite.run("CodeValidator — pytest real en sandbox real (pasa)", test_dynamic_validation_sandbox_real)
        suite.run("CodeValidator — pytest real en sandbox real (falla)", test_dynamic_validation_test_que_falla)
        suite.run("classifier_api — falsos positivos de clima corregidos", test_classifier_weather_false_positives)
        suite.run("data_fetchers — TTL de cache negativo configurado", test_geocoding_negative_cache_ttl)

        ollama_up = _ollama_available()
        suite.section("C. Pipeline completo (Dispatcher real + Ollama)")
        if args.skip_ollama:
            suite.skip("Dispatcher.process() — generación de código real (ruteo completo)", "--skip-ollama pasado por el usuario")
            suite.skip("_handle_code() directo — Fases 1-4 garantizadas", "--skip-ollama pasado por el usuario")
        elif not ollama_up:
            suite.skip("Dispatcher.process() — generación de código real (ruteo completo)", "Ollama no responde en localhost:11434")
            suite.skip("_handle_code() directo — Fases 1-4 garantizadas", "Ollama no responde en localhost:11434")
        else:
            suite.run("Dispatcher.process() — generación de código real (ruteo completo)", test_dispatcher_code_generation_real)
            suite.run("_handle_code() directo — Fases 1-4 garantizadas", test_handle_code_direct_real)

    system_diag = _system_diagnostics()
    logger.log_event("SYSTEM_DIAGNOSTIC", "Info de entorno", extra=system_diag)

    counts = suite.counts
    print(f"\n{'═' * 70}")
    print(f"RESUMEN: {counts['passed']} pasaron, {counts['failed']} fallaron, {counts['skipped']} saltados")
    print(f"{'═' * 70}")

    report_path = LoggerConfig.LOGS_DIR / f"code_contract_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    _write_report(suite, report_path, ollama_skipped=(args.skip_ollama or not ollama_up), system_diag=system_diag)
    print(f"\n📄 Informe: {report_path}")
    print(f"📋 Log detallado: {LoggerConfig.OUTPUT_FILE}")

    # ✅ 2026-08-29: conservar solo el informe code_contract más reciente
    # (evita acumular reportes obsoletos en logs/).
    try:
        old_reports = sorted(
            LoggerConfig.LOGS_DIR.glob("code_contract_report_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in old_reports[1:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass

    close_logger()
    return 1 if counts["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
