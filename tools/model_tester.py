#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v3.1 — Tester de Modelos LLM
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/tools/model_tester.py
# Autor: SaintWick
# Fecha: 2026-03-06
# Propósito: Test de latencia y capacidad por modelo
# ═══════════════════════════════════════════════════════════════

import argparse
import sys
import time
import json
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime

# Asegurar ruta para imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings


class ModelTester:
    """Tester de rendimiento para modelos LLM."""

    TEST_PROMPTS = {
        "simple": "¿Qué es Python?",
        "code": "Escribe una función Fibonacci en Python",
        "reasoning": "Si todos los A son B y algunos B son C, ¿podemos concluir que algunos A son C?",
        "long": "Explica los conceptos fundamentales de la programación orientada a objetos",
        "judge": "Evalúa si esta respuesta es correcta y explica por qué",
    }

    def __init__(self, output_dir: Path = None):
        """
        Inicializa tester de modelos.

        Args:
            output_dir: Directorio para guardar resultados
        """
        self.output_dir = output_dir or PROJECT_ROOT / "data" / "model_tests"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.results: List[Dict[str, Any]] = []

    def test_model(
        self,
        model: str,
        prompt_type: str = "simple",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """
        Testea un modelo con un prompt específico.

        Args:
            model: Nombre del modelo
            prompt_type: Tipo de prompt (simple, code, reasoning, long)
            timeout: Timeout en segundos

        Returns:
            Dict con resultados del test
        """
        prompt = self.TEST_PROMPTS.get(prompt_type, self.TEST_PROMPTS["simple"])

        result = {
            "model": model,
            "prompt_type": prompt_type,
            "prompt_length": len(prompt),
            "timestamp": datetime.now().isoformat(),
            "success": False,
            "error": None,
            "latency_ms": 0,
            "tokens_per_second": 0,
            "response_length": 0,
        }

        try:
            from src.llm.client import OllamaClient
            client = OllamaClient(model=model, timeout=timeout)

            start = time.time()
            response = client.generate(prompt=prompt, temperature=0.5)
            latency = (time.time() - start) * 1000

            result["success"] = True
            result["latency_ms"] = round(latency, 2)
            result["response_length"] = len(response.content)
            result["tokens_per_second"] = round(
                response.eval_count / (latency / 1000), 2
            ) if latency > 0 and response.eval_count else 0

            client.close()

        except Exception as e:
            result["error"] = str(e)

        return result

    def test_all_models(
        self,
        models: List[str] = None,
        prompt_types: List[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Testea todos los modelos configurados.

        Args:
            models: Lista de modelos a testear
            prompt_types: Lista de tipos de prompt

        Returns:
            Lista de resultados
        """
        if models is None:
            # Incluye modelo juez vicgalle/prometheus-7b-v2.0
            models = [
                settings.llm_default_model if hasattr(settings, 'llm_default_model') else "qwen3:8b",
                settings.llm_code_model if hasattr(settings, 'llm_code_model') else "qwen2.5-coder:7b",
                settings.llm_vision_model if hasattr(settings, 'llm_vision_model') else "qwen3-vl:4b",
                settings.llm_judge_model if hasattr(settings, 'llm_judge_model') else "vicgalle/prometheus-7b-v2.0:latest",
            ]

        if prompt_types is None:
            prompt_types = ["simple", "code", "reasoning"]

        results = []

        for model in models:
            print(f"\n🔹 Testing: {model}")
            for prompt_type in prompt_types:
                result = self.test_model(model, prompt_type)
                results.append(result)

                status = "✅" if result["success"] else "❌"
                latency = f"{result['latency_ms']}ms" if result["success"] else result["error"]
                print(f"  {status} {prompt_type}: {latency}")

        self.results = results
        return results

    def save_results(self, filename: str = None) -> Path:
        """
        Guarda resultados a archivo JSON.

        Args:
            filename: Nombre del archivo (opcional)

        Returns:
            Ruta del archivo guardado
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"model_test_{timestamp}.json"

        output_path = self.output_dir / filename

        report = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": len(self.results),
            "successful": sum(1 for r in self.results if r["success"]),
            "failed": sum(1 for r in self.results if not r["success"]),
            "results": self.results,
            "summary": self._generate_summary(),
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        return output_path

    def _generate_summary(self) -> Dict[str, Any]:
        """Genera resumen estadístico."""
        successful = [r for r in self.results if r["success"]]

        if not successful:
            return {"error": "No successful tests"}

        latencies = [r["latency_ms"] for r in successful]
        tps = [r["tokens_per_second"] for r in successful if r["tokens_per_second"] > 0]

        return {
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
            "min_latency_ms": round(min(latencies), 2),
            "max_latency_ms": round(max(latencies), 2),
            "avg_tokens_per_second": round(sum(tps) / len(tps), 2) if tps else 0,
            "total_models_tested": len(set(r["model"] for r in successful)),
        }

    def print_report(self) -> None:
        """Imprime reporte en consola."""
        print("\n" + "=" * 60)
        print("📊 MODEL TESTER — REPORTE")
        print("=" * 60)

        summary = self._generate_summary()

        print(f"\nTests totales: {len(self.results)}")
        print(f"Exitosos: {sum(1 for r in self.results if r['success'])}")
        print(f"Fallidos: {sum(1 for r in self.results if not r['success'])}")

        if "avg_latency_ms" in summary:
            print(f"\nLatencia promedio: {summary['avg_latency_ms']}ms")
            print(f"Tokens/segundo: {summary['avg_tokens_per_second']}")

        # Reporte por modelo
        print("\nPor modelo:")
        models_report = {}
        for r in self.results:
            model = r["model"]
            if model not in models_report:
                models_report[model] = {"total": 0, "success": 0, "latency_total": 0}
            models_report[model]["total"] += 1
            if r["success"]:
                models_report[model]["success"] += 1
                models_report[model]["latency_total"] += r["latency_ms"]

        for model, stats in models_report.items():
            avg_latency = stats["latency_total"] / max(stats["success"], 1)
            success_rate = stats["success"] / stats["total"] * 100
            print(f"  • {model}: {success_rate:.0f}% éxito, {avg_latency:.0f}ms avg")

        print("\n" + "=" * 60)


def main() -> int:
    """Función principal."""
    parser = argparse.ArgumentParser(description="AXIOMA Model Tester")
    parser.add_argument(
        "--model", "-m",
        action="append",
        help="Modelo a testear (puede usarse múltiples veces)"
    )
    parser.add_argument(
        "--prompt", "-p",
        choices=["simple", "code", "reasoning", "long", "judge"],
        action="append",
        help="Tipo de prompt"
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Testear todos los modelos configurados"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        help="Archivo de salida JSON"
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Solo mostrar resumen"
    )
    parser.add_argument(
        "--include-judge", "-j",
        action="store_true",
        help="Incluir modelo juez (vicgalle/prometheus-7b-v2.0)"
    )

    args = parser.parse_args()

    tester = ModelTester()

    if args.all or (not args.model and not args.prompt):
        results = tester.test_all_models(
            models=args.model,
            prompt_types=args.prompt,
        )
    else:
        models = args.model or [settings.llm_default_model]
        prompts = args.prompt or ["simple"]
        results = []
        for model in models:
            for prompt in prompts:
                result = tester.test_model(model, prompt)
                results.append(result)
                if not args.quiet:
                    status = "✅" if result["success"] else "❌"
                    print(f"{status} {model} ({prompt}): {result.get('latency_ms', 'N/A')}ms")
        tester.results = results

    # Guardar resultados
    output_path = tester.save_results(args.output)
    if not args.quiet:
        print(f"\n💾 Resultados guardados: {output_path}")

    # Imprimir reporte
    tester.print_report()

    # Return code
    failed = sum(1 for r in tester.results if not r["success"])
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
