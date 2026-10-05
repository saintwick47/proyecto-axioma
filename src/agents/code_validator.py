#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.1.0 — Validador de Código Multicapa
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/code_validator.py
# Autor: SaintWick
# Fecha: 2026-08-21
# Propósito: Valida un CodeContract (Fase 1) en 3 capas deterministas
#            — estructural, estática, dinámica — antes de que el
#            código se considere apto para entrega. Fase 3 del plan
#            de contrato de código verificable. La capa LLM
#            (validator.py, ValidatorAgent) es apoyo/no-bloqueante y
#            se orquesta desde dispatcher_handlers.py en Fase 4, no
#            desde acá.
#
# Corrección de diseño (H7, ver plan_corregido_v2.md): la validación
# dinámica NO usa sandbox.execute_python() (ejecuta `python -c code`,
# un único string inline — no soporta un proyecto multi-archivo con
# tests separados). Tampoco usa sandbox.execute_command() con un
# argv tipo [sys.executable, "-m", "pytest", ...] — CONFIRMADO con
# harness real que "python"/"python3"/"pip"/"pip3" están en la
# blocklist default de SandboxConfig y execute_command() la aplica a
# cualquier caller externo (no tiene _skip_command_checks=True).
# Única vía real: escribir los archivos del contrato + un script
# runner a un tempdir real, y correrlo vía sandbox.execute_script()
# — ese método SÍ pasa _skip_command_checks=True internamente para
# sys.executable (confirmado leyendo sandbox.py). El runner invoca
# pytest.main() en el mismo proceso (no un subprocess anidado) para
# no depender de que "pytest" también esté en el PATH del entorno
# sandboxeado, y confía en el auto-discovery de pytest sobre el
# tempdir en vez de parsear/confiar en el test_command textual que
# escribió el LLM (más robusto, no importa qué nombre de archivo o
# variante de comando haya puesto el modelo).
# ═══════════════════════════════════════════════════════════════
import ast
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .code_contract import CodeContract, CodeFile

logger = logging.getLogger(__name__)

_RUNNER_SCRIPT = (
    "import sys\n"
    "try:\n"
    "    import pytest\n"
    "except ImportError:\n"
    "    print('AXIOMA_NO_PYTEST: pytest no está disponible en este entorno')\n"
    "    sys.exit(3)\n"
    "sys.exit(pytest.main(['-v', '--tb=short']))\n"
)
_RUNNER_FILENAME = "_axioma_run_tests.py"


@dataclass
class LayerResult:
    """Resultado de una capa individual de validación."""
    layer: str
    passed: bool
    errors: List[str] = field(default_factory=list)
    output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"layer": self.layer, "passed": self.passed, "errors": list(self.errors), "output": self.output}


@dataclass
class ValidationReport:
    """
    Resultado agregado de `CodeValidator.validate()`.

    `passed` es True solo si TODAS las capas que corrieron pasaron.
    Una capa que no corrió (ej. dinámica sin tests) no cuenta como
    fallo — queda simplemente ausente de `layers`.
    """
    passed: bool
    layers: List[LayerResult] = field(default_factory=list)

    @property
    def error_summary(self) -> str:
        """Texto en español con todos los errores, para crítica de retry (Fase 4)."""
        parts: List[str] = []
        for layer in self.layers:
            if not layer.passed and layer.errors:
                parts.append(f"[{layer.layer}] " + "; ".join(layer.errors))
        return " | ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "layers": [l.to_dict() for l in self.layers]}


class CodeValidator:
    """
    Corre las 3 capas deterministas de validación sobre un CodeContract,
    en orden, deteniéndose en la primera capa que falla (no tiene
    sentido correr tests dinámicos sobre código que ni siquiera
    compila).

    Uso:
        validator = CodeValidator(sandbox=get_sandbox_executor(), settings=settings)
        report = validator.validate(contract)
        if not report.passed:
            critique = report.error_summary
    """

    def __init__(self, sandbox: Any, settings: Any) -> None:
        self.sandbox = sandbox
        self.settings = settings

    def validate(self, contract: CodeContract) -> ValidationReport:
        layers: List[LayerResult] = []

        structural = self._validate_structural(contract)
        layers.append(structural)
        if not structural.passed:
            return ValidationReport(passed=False, layers=layers)

        static = self._validate_static(contract)
        layers.append(static)
        if not static.passed:
            return ValidationReport(passed=False, layers=layers)

        dynamic = self._validate_dynamic(contract)
        if dynamic is not None:
            layers.append(dynamic)
            if not dynamic.passed:
                return ValidationReport(passed=False, layers=layers)

        return ValidationReport(passed=True, layers=layers)

    def _validate_structural(self, contract: CodeContract) -> LayerResult:
        """Capa 1: el contrato tiene las secciones que exigen los settings (Fase 1)."""
        if contract.missing:
            return LayerResult(
                layer="structural", passed=False,
                errors=[f"Faltan secciones del contrato: {', '.join(contract.missing)}"],
            )
        return LayerResult(layer="structural", passed=True)

    def _validate_static(self, contract: CodeContract) -> LayerResult:
        """
        Capa 2: cada archivo (código + tests) compila por separado vía
        ast.parse(). Por archivo, no concatenado — a diferencia de
        analyze_code_quality() (coder_core.py, que compila un blob
        único), acá un contrato multi-archivo no debe dar falso
        negativo/positivo por mezclar dos módulos en un solo string.
        """
        errors: List[str] = []
        for f in (*contract.files, *contract.test_files):
            try:
                ast.parse(f.content, filename=f.name)
            except SyntaxError as e:
                errors.append(f"{f.name}: {e.msg} (línea {e.lineno})")
        return LayerResult(layer="static", passed=not errors, errors=errors)

    def _validate_dynamic(self, contract: CodeContract) -> Optional[LayerResult]:
        """
        Capa 3: corre los tests reales en sandbox. Devuelve None (no
        "falló", sino "no corrió") si no hay test_files — no tiene
        sentido exigir esta capa cuando code_require_tests=False y el
        LLM no escribió tests.
        """
        if not contract.test_files:
            return None

        timeout = getattr(self.settings, "code_validation_timeout", 15)

        try:
            with tempfile.TemporaryDirectory(prefix="axioma_validate_") as tmp:
                tmp_path = Path(tmp)
                for f in (*contract.files, *contract.test_files):
                    (tmp_path / f.name).write_text(f.content, encoding="utf-8")
                (tmp_path / _RUNNER_FILENAME).write_text(_RUNNER_SCRIPT, encoding="utf-8")

                result = self.sandbox.execute_script(
                    tmp_path / _RUNNER_FILENAME,
                    cwd=tmp_path,
                    timeout=timeout,
                )
        except OSError as e:
            logger.error(f"CodeValidator: fallo de filesystem preparando validación dinámica: {e}")
            return LayerResult(layer="dynamic", passed=False, errors=[f"Error preparando sandbox: {e}"])

        if not result.success:
            error_text = self._extract_pytest_failures(result.stdout) or \
                result.stderr.strip() or result.stdout.strip() or (result.error or "sin salida")
            # v0.6.9g: traza del fallo dinámico para diagnóstico (logs 15:21-17:01:
            # 8× SANDBOX FAILED sin detalle en los eventos de sandbox).
            logger.warning(f"[CodeValidator] dynamic FAIL: {error_text[:300]}")
            return LayerResult(layer="dynamic", passed=False, errors=[error_text], output=result.stdout)

        return LayerResult(layer="dynamic", passed=True, output=result.stdout)

    @staticmethod
    def _extract_pytest_failures(stdout: str) -> str:
        """
        Reduce el output completo de pytest a las líneas `FAILED ...` de
        su "short test summary info" — un resumen de una línea por test
        fallido (ej. "FAILED test_x.py::test_y - assert 8 == 2"), en vez
        del transcript completo de la sesión. Si pytest no llegó a
        imprimir ese resumen (crash antes de terminar, error de colección),
        cae a las últimas líneas del output.
        """
        failed_lines = [line for line in stdout.splitlines() if line.startswith("FAILED ")]
        if failed_lines:
            return " | ".join(failed_lines)
        tail = stdout.strip().splitlines()[-15:]
        return "\n".join(tail)


def build_validation_critique(report: ValidationReport) -> str:
    """
    Construye una crítica en español para el retry externo (Fase 4), a
    partir de las capas fallidas de un ValidationReport. Se pasa como
    `previous_critique` a CoderAgent.execute() desde dispatcher_handlers.py
    — distinto de build_contract_critique() (code_contract.py), que cubre
    secciones faltantes; esto cubre fallos reales de compilación/tests que
    CoderAgent no puede detectar por sí solo.
    """
    if report.passed:
        return ""
    return (
        f"La validación real (no la heurística) encontró estos problemas: "
        f"{report.error_summary}. Corregí el código para que compile y "
        f"pase los tests, manteniendo el resto del contrato."
    )
