#!/usr/bin/env python3
# Ruta: /ruta/a/axioma/tools/run_diagnostic_suite.py
# Autor: SaintWick
"""
AXIOMA Diagnostic Suite Runner v2.0
Ejecuta todos los tests en orden de dependencias y genera reporte consolidado.

Uso:
    python tools/run_diagnostic_suite.py                  # Todas las fases (sin tests reales)
    python tools/run_diagnostic_suite.py --phase 3        # Solo Fase 3 (Core)
    python tools/run_diagnostic_suite.py --real           # Incluye tests que requieren Ollama/audio
    python tools/run_diagnostic_suite.py --tools          # Incluye tests en tools/
    python tools/run_diagnostic_suite.py --html           # Genera reporte HTML
    python tools/run_diagnostic_suite.py -o mi_reporte    # Nombre base del output
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
TOOLS_DIR = PROJECT_ROOT / "tools"
OUTPUT_DIR = TESTS_DIR / "diagnostic_output"

# Tests que requieren entorno real (siempre ignorados por defecto)
IGNORED_TESTS = [
    "tests/test_diagnostics_real_detection.py",
    "tests/test_pipeline_core.py",
]

# ═══════════════════════════════════════════════════════════════
# ✅ v2.1.0 (2026-08-25): Fases re-mapeadas a la SUITE INTEGRAL
# (tests/test_axioma_01..06) — reemplaza los tests antiguos eliminados.
# ═══════════════════════════════════════════════════════════════
PHASES = [
    {
        "id": 0,
        "name": "Config & Domain",
        "tests": [
            "tests/test_axioma_01_config_domain.py",
        ],
        "description": "Estructura del proyecto, config/ (settings, paths, YAML) y dominio"
    },
    {
        "id": 1,
        "name": "Core (Dispatcher + Orchestration)",
        "tests": [
            "tests/test_axioma_02_core.py",
        ],
        "description": "Dispatcher, colas, feedback, calidad, self-healing, planner, model_swap, hardware_fit"
    },
    {
        "id": 2,
        "name": "LLM + Router + Memoria",
        "tests": [
            "tests/test_axioma_03_llm_router_memory.py",
        ],
        "description": "OllamaClient, judge, classifier, caches L2/L3, memoria (short/long/semantic/KG)"
    },
    {
        "id": 3,
        "name": "Agentes",
        "tests": [
            "tests/test_axioma_04_agents.py",
        ],
        "description": "Agentes base, coder, analyst, researcher, loop, parallel executor, code contract"
    },
    {
        "id": 4,
        "name": "Tools MCP + Multimodal",
        "tests": [
            "tests/test_axioma_04_agents.py",
        ],
        "description": "Tools base, registro, executor, MCP, sandbox, permisos, pipeline multimodal"
    },
    {
        "id": 5,
        "name": "Web + Handlers + Análisis Global",
        "tests": [
            "tests/test_axioma_06_web_handlers_analisis.py",
        ],
        "description": "Web/CLI, handlers, prompts, utils, learning, monitoring + import de los 156 módulos"
    },
]

# Tests en tools/ (separados, no son pytest estándar)
# ✅ 2026-09-01: los 4 test_*.py de tools/ fueron ELIMINADOS:
#  - test_handoff_integration.py esperaba el handoff Searcher→Coder que se
#    desactivó en el PLAN v2.0 Fase 6 (quedó obsoleto y fallaba).
#  - test_autonomous/test_detect_intent/test_jarvis_mode eran de entorno
#    real y están cubiertos por la suite tests/test_axioma_*.py.
#  - caac_e2e_validation.py duplicaba tests/test_code_contract_e2e.py.
# La suite integral (tests/test_axioma_*.py) es la fuente de verdad.
TOOLS_TESTS = []


def run_test(test_path: str, timeout: int = 300) -> dict:
    """Ejecuta un test y retorna resultado estructurado."""
    result = {
        "test": test_path,
        "status": "unknown",
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "duration_seconds": 0.0,
        "output": "",
        "error_details": [],
    }

    start = time.time()

    try:
        cmd = [
            sys.executable, "-m", "pytest",
            test_path,
            "-v", "--tb=short", "--no-header", "-q",
        ]

        proc = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        result["output"] = proc.stdout + proc.stderr
        result["returncode"] = proc.returncode

        # Parsear resultados de pytest
        import re
        passed = re.search(r"(\d+) passed", result["output"])
        failed = re.search(r"(\d+) failed", result["output"])
        errors = re.search(r"(\d+) error", result["output"])
        skipped = re.search(r"(\d+) skipped", result["output"])
        collected = re.search(r"collected (\d+) item", result["output"])

        if passed: result["passed"] = int(passed.group(1))
        if failed: result["failed"] = int(failed.group(1))
        if errors: result["errors"] = int(errors.group(1))
        if skipped: result["skipped"] = int(skipped.group(1))
        if collected: result["collected"] = int(collected.group(1))

        # Extraer detalles de errores
        if "FAILED" in result["output"] or "ERROR" in result["output"]:
            lines = result["output"].split("\n")
            for i, line in enumerate(lines):
                if "FAILED" in line or "ERROR" in line:
                    start_idx = max(0, i - 3)
                    end_idx = min(len(lines), i + 10)
                    error_context = "\n".join(lines[start_idx:end_idx])
                    result["error_details"].append(error_context)

        if proc.returncode == 0:
            result["status"] = "passed"
        elif proc.returncode == 1:
            result["status"] = "failed"
        elif proc.returncode == 2:
            result["status"] = "interrupted"
        elif proc.returncode == 3:
            result["status"] = "error"
        elif proc.returncode == 4:
            result["status"] = "usage_error"
        elif proc.returncode == 5:
            result["status"] = "no_tests"
        else:
            result["status"] = f"exit_code_{proc.returncode}"

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["output"] = f"Test excedió {timeout}s de ejecución"
    except Exception as e:
        result["status"] = "exception"
        result["output"] = str(e)
        result["error_details"].append(str(e))

    result["duration_seconds"] = time.time() - start
    return result


def run_phase(phase: dict, include_real: bool = False) -> dict:
    """Ejecuta una fase completa de tests."""
    print(f"\n{'='*80}")
    print(f"🧪 FASE {phase['id']}: {phase['name']}")
    print(f"📝 {phase['description']}")
    print(f"{'='*80}\n")

    phase_results = {
        "phase_id": phase["id"],
        "phase_name": phase["name"],
        "description": phase["description"],
        "tests": [],
        "total_tests": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "duration_seconds": 0.0,
        "start_time": datetime.now().isoformat(),
    }

    phase_start = time.time()

    for test_path in phase["tests"]:
        # Verificar si el test debe ser ignorado
        if not include_real and test_path in IGNORED_TESTS:
            print(f"  ⏭️  {test_path} — IGNORADO (requiere entorno real)")
            phase_results["tests"].append({
                "test": test_path,
                "status": "ignored",
                "collected": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "duration_seconds": 0.0,
                "output": "Ignorado: requiere entorno real (Ollama/audio hardware)",
                "error_details": [],
            })
            continue

        print(f"  🔍 Ejecutando: {test_path}")
        test_result = run_test(test_path)
        phase_results["tests"].append(test_result)
        phase_results["total_tests"] += test_result.get("collected", 0)
        phase_results["passed"] += test_result.get("passed", 0)
        phase_results["failed"] += test_result.get("failed", 0)
        phase_results["errors"] += test_result.get("errors", 0)
        phase_results["skipped"] += test_result.get("skipped", 0)

        status_emoji = {
            "passed": "✅",
            "failed": "❌",
            "error": "🔥",
            "timeout": "⏱️",
            "no_tests": "⚪",
            "unknown": "❓",
        }.get(test_result["status"], "❓")

        print(f"    {status_emoji} {test_result['status'].upper()}")
        if test_result["passed"] > 0:
            print(f"    ✅ {test_result['passed']} passed")
        if test_result["failed"] > 0:
            print(f"    ❌ {test_result['failed']} failed")
        if test_result["errors"] > 0:
            print(f"    🔥 {test_result['errors']} errors")

    phase_results["duration_seconds"] = time.time() - phase_start
    phase_results["end_time"] = datetime.now().isoformat()

    return phase_results


def generate_phase_report(phase_results: dict, output_dir: Path):
    """Genera reporte MD de una fase."""
    output_file = output_dir / f"phase_{phase_results['phase_id']}_{phase_results['phase_name'].lower().replace(' ', '_').replace('+', 'plus')}.md"

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# 🧪 Fase {phase_results['phase_id']}: {phase_results['phase_name']}\n\n")
        f.write(f"**Descripción:** {phase_results['description']}\n\n")
        f.write(f"**Inicio:** {phase_results['start_time']}\n")
        f.write(f"**Fin:** {phase_results['end_time']}\n")
        f.write(f"**Duración:** {phase_results['duration_seconds']:.2f}s\n\n")

        f.write(f"## 📊 Resumen\n\n")
        f.write(f"| Métrica | Valor |\n")
        f.write(f"|---|---|\n")
        f.write(f"| Total Tests | {phase_results['total_tests']} |\n")
        f.write(f"| ✅ Pasaron | {phase_results['passed']} |\n")
        f.write(f"| ❌ Fallaron | {phase_results['failed']} |\n")
        f.write(f"| 🔥 Errores | {phase_results['errors']} |\n")
        f.write(f"| ⏭️  Skipped | {phase_results['skipped']} |\n\n")

        f.write(f"## 📝 Detalle por Test\n\n")

        for test in phase_results["tests"]:
            status_emoji = {
                "passed": "✅",
                "failed": "❌",
                "error": "🔥",
                "timeout": "⏱️",
                "no_tests": "⚪",
                "ignored": "⏭️",
                "unknown": "❓",
            }.get(test["status"], "❓")

            f.write(f"### {status_emoji} {test['test']}\n\n")
            f.write(f"- **Status:** {test['status']}\n")
            f.write(f"- **Duración:** {test['duration_seconds']:.2f}s\n")
            f.write(f"- **Collected:** {test['collected']}\n")
            f.write(f"- **Passed:** {test['passed']}\n")
            f.write(f"- **Failed:** {test['failed']}\n")
            f.write(f"- **Errors:** {test['errors']}\n\n")

            if test["error_details"]:
                f.write(f"#### 🔍 Detalles de Errores\n\n")
                for i, error in enumerate(test["error_details"], 1):
                    f.write(f"**Error {i}:**\n```\n")
                    f.write(error[:2000])
                    f.write("\n```\n\n")

            f.write("---\n\n")

    print(f"  📄 Reporte generado: {output_file.name}")


def generate_final_report(all_results: list, output_dir: Path, output_base: str, include_real: bool, include_tools: bool):
    """Genera reporte final consolidado."""
    final_report = PROJECT_ROOT / f"{output_base}.md"
    summary_json = output_dir / "summary.json"

    # Calcular métricas globales
    total_tests = sum(r["total_tests"] for r in all_results)
    total_passed = sum(r["passed"] for r in all_results)
    total_failed = sum(r["failed"] for r in all_results)
    total_errors = sum(r["errors"] for r in all_results)
    total_skipped = sum(r["skipped"] for r in all_results)
    total_duration = sum(r["duration_seconds"] for r in all_results)

    end_time = datetime.now()

    # Guardar summary JSON
    summary_data = {
        "timestamp": end_time.isoformat(),
        "total_phases": len(all_results),
        "total_tests": total_tests,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "total_errors": total_errors,
        "total_skipped": total_skipped,
        "total_duration_seconds": total_duration,
        "include_real": include_real,
        "include_tools": include_tools,
        "phases": all_results,
    }

    with open(summary_json, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2, ensure_ascii=False)

    # Generar reporte MD final
    with open(final_report, "w", encoding="utf-8") as f:
        f.write("# 🏥 AXIOMA Test Diagnostic Report v2.0\n\n")
        f.write(f"**Generado:** {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Duración total:** {total_duration:.2f}s\n")
        f.write(f"**Tests reales incluidos:** {'Sí' if include_real else 'No'}\n")
        f.write(f"**Tests en tools/ incluidos:** {'Sí' if include_tools else 'No'}\n\n")

        f.write("## 📊 Resumen Ejecutivo\n\n")
        f.write("| Métrica | Valor | % |\n")
        f.write("|---|---|---|\n")
        f.write(f"| Total Tests | {total_tests} | 100% |\n")
        f.write(f"| ✅ Pasaron | {total_passed} | {total_passed/max(total_tests,1)*100:.1f}% |\n")
        f.write(f"| ❌ Fallaron | {total_failed} | {total_failed/max(total_tests,1)*100:.1f}% |\n")
        f.write(f"| 🔥 Errores | {total_errors} | {total_errors/max(total_tests,1)*100:.1f}% |\n")
        f.write(f"| ⏭️  Skipped | {total_skipped} | {total_skipped/max(total_tests,1)*100:.1f}% |\n\n")

        f.write("## 📋 Resumen por Fase\n\n")
        f.write("| Fase | Tests | ✅ | ❌ | 🔥 | Duración | Status |\n")
        f.write("|---|---|---|---|---|---|---|\n")

        for r in all_results:
            status = "✅ OK" if r["failed"] == 0 and r["errors"] == 0 else "❌ PROBLEMAS"
            f.write(f"| {r['phase_id']}. {r['phase_name']} | {r['total_tests']} | {r['passed']} | {r['failed']} | {r['errors']} | {r['duration_seconds']:.1f}s | {status} |\n")

        f.write("\n## 🎯 Hallazgos Críticos\n\n")

        # Recopilar errores críticos
        critical_issues = []
        for r in all_results:
            for test in r["tests"]:
                if test["failed"] > 0 or test["errors"] > 0:
                    for error in test["error_details"]:
                        critical_issues.append({
                            "phase": r["phase_name"],
                            "test": test["test"],
                            "error": error[:500],
                        })

        if critical_issues:
            for i, issue in enumerate(critical_issues[:20], 1):
                f.write(f"### {i}. {issue['phase']} → {issue['test']}\n\n")
                f.write(f"```\n{issue['error']}\n```\n\n")
        else:
            f.write("✅ No se encontraron errores críticos.\n\n")

        f.write("## 📁 Reportes por Fase\n\n")
        for r in all_results:
            phase_file = f"phase_{r['phase_id']}_{r['phase_name'].lower().replace(' ', '_').replace('+', 'plus')}.md"
            f.write(f"- [{phase_file}](tests/diagnostic_output/{phase_file})\n")

        f.write("\n---\n")
        f.write("✅ Reporte generado automáticamente por `tools/run_diagnostic_suite.py v2.0`\n")

    print(f"\n📊 JSON: {summary_json}")
    print(f"📄 MD:   {final_report}")


def main():
    parser = argparse.ArgumentParser(description="AXIOMA Diagnostic Suite Runner v2.1")
    parser.add_argument("--phase", type=int, help="Ejecutar solo una fase específica (0-5)")
    parser.add_argument("--real", action="store_true", help="Incluir tests que requieren Ollama/audio real")
    parser.add_argument("--tools", action="store_true", help="Incluir tests en tools/")
    parser.add_argument("--html", action="store_true", help="Generar reporte HTML adicional")
    parser.add_argument("-o", type=str, default="TEST_DIAGNOSTIC_REPORT", help="Nombre base del output")
    args = parser.parse_args()

    # Crear directorio de output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*80}")
    print(f"🧪 AXIOMA Diagnostic Suite Runner v2.0")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 Project Root: {PROJECT_ROOT}")
    print(f"🔬 Tests reales: {'Incluidos' if args.real else 'Excluidos (usar --real para incluir)'}")
    print(f"🛠️  Tests en tools/: {'Incluidos' if args.tools else 'Excluidos (usar --tools para incluir)'}")
    print(f"{'='*80}")

    all_results = []

    # Ejecutar fases
    phases_to_run = PHASES
    if args.phase is not None:
        phases_to_run = [p for p in PHASES if p["id"] == args.phase]
        if not phases_to_run:
            print(f"❌ Fase {args.phase} no existe (rango: 0-{len(PHASES)-1})")
            sys.exit(1)

    for phase in phases_to_run:
        phase_result = run_phase(phase, include_real=args.real)
        all_results.append(phase_result)
        generate_phase_report(phase_result, OUTPUT_DIR)

        # Pausa breve entre fases para liberar recursos
        time.sleep(1)

    # Tests en tools/ (si se solicitan)
    if args.tools:
        print(f"\n{'='*80}")
        print(f"🛠️  Tests en tools/ (especializados)")
        print(f"{'='*80}\n")

        tools_phase = {
            "phase_id": 9,
            "phase_name": "Tools Especializados",
            "description": "Tests en tools/ (autonomous, detect_intent, handoff, jarvis)",
            "tests": TOOLS_TESTS,
        }

        tools_result = run_phase(tools_phase, include_real=True)
        all_results.append(tools_result)
        generate_phase_report(tools_result, OUTPUT_DIR)

    # Generar reporte final
    generate_final_report(all_results, OUTPUT_DIR, args.o, args.real, args.tools)

    # Resumen final
    total_passed = sum(r["passed"] for r in all_results)
    total_failed = sum(r["failed"] for r in all_results)
    total_errors = sum(r["errors"] for r in all_results)

    print(f"\n{'='*80}")
    print(f"📊 RESUMEN FINAL")
    print(f"{'='*80}")
    print(f"  ✅ Pasaron: {total_passed}")
    print(f"  ❌ Fallaron: {total_failed}")
    print(f"  🔥 Errores: {total_errors}")
    print(f"{'='*80}")

    sys.exit(0 if total_failed == 0 and total_errors == 0 else 1)


if __name__ == "__main__":
    main()
