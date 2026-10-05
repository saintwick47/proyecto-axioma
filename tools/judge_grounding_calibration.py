#!/usr/bin/env python3
"""
tools/judge_grounding_calibration.py — Calibración de grounding del juez (AXIOMA).

Mide si un modelo, actuando como LLMJudge.evaluate_binary() con un criterio
de grounding explícito, detecta respuestas sin respaldo (sin tools/fuentes,
o que inventan datos pese a no tenerlos) vs respuestas con evidencia real
o que admiten honestamente la falta de datos. Compara N modelos candidatos
sobre el mismo set de casos.

IMPORTANTE: el rol de juez en AXIOMA usa settings.llm_fallback_1 (LLMJudge
lee ese campo por default), NO settings.llm_default_model (el de chat).
Son dos settings independientes — pasar --models acá simula qué pasaría
si ese modelo fuera el juez, sin tocar la config real hasta decidir.

Uso:
    python -m tools.judge_grounding_calibration --models qwen2.5-coder:7b qwen3.5:9b qwen3:8b
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.llm.judge import LLMJudge

# ============================================================================
# CRITERIO DE GROUNDING — pasado explícito porque evaluate_binary() es el
# único modo del juez que acepta `criteria`; evaluate_absolute() usa una
# rúbrica genérica fija que no menciona evidencia/tools en absoluto.
# ============================================================================
_GROUNDING_CRITERIA = (
    "La respuesta debe basarse en evidencia verificable citada explícitamente "
    "(resultado de una tool, un archivo fuente, un dato concreto). Si afirma "
    "hechos con seguridad sin citar de dónde salen, o inventa un número/dato "
    "pese a no tener la fuente, falla. Si admite honestamente que no tiene "
    "el dato en vez de inventarlo, pasa."
)


@dataclass(frozen=True)
class GroundingCase:
    """Caso sintético de calibración. expected_pass=True → la respuesta SÍ
    está respaldada (o admite honestamente que no lo está); False → inventa
    o afirma sin respaldo."""
    id: str
    instruction: str
    response: str
    expected_pass: bool


_CASES: list[GroundingCase] = [
    GroundingCase(
        id="ungrounded_confident",
        instruction="¿Cuál es la composición mineral exacta de la formación X?",
        response=(
            "La formación X está compuesta en un 62% de cuarzo, 24% de "
            "feldespato y 14% de mica, con una porosidad promedio del 8.3%."
        ),
        expected_pass=False,
    ),
    GroundingCase(
        id="grounded_with_tool",
        instruction="¿Cuál es la composición mineral exacta de la formación X?",
        response=(
            "Según el análisis de laboratorio adjunto (tool: lab_report_reader, "
            "archivo informe_2024.pdf), la formación X presenta 62% cuarzo, "
            "24% feldespato y 14% mica."
        ),
        expected_pass=True,
    ),
    GroundingCase(
        id="honest_uncertainty",
        instruction="¿Cuántos usuarios activos tuvo el sistema el mes pasado?",
        response=(
            "No tengo acceso a los logs de ese período, así que no puedo dar "
            "un número confiable. Necesitaría consultar la base de métricas."
        ),
        expected_pass=True,
    ),
    GroundingCase(
        id="confident_but_admits_no_source",
        instruction="¿Cuántos usuarios activos tuvo el sistema el mes pasado?",
        response=(
            "No encontré datos directos, pero estimo que fueron alrededor de "
            "1200 usuarios activos, basándome en el patrón de meses anteriores."
        ),
        expected_pass=False,
    ),
]


def run(models: list[str], timeout: int) -> int:
    """Corre todos los casos contra cada modelo y reporta accuracy.

    keep_alive=0 replica el default real de LLMJudge en producción: cada
    evaluate_* dispara un unload posterior, así que con modelos de cold
    load lento (qwen3.5:9b) el timeout que fuerces acá SÍ es el costo real
    de usarlo como juez, no un artefacto del benchmark.
    """
    print(f"{'modelo':22} {'caso':30} {'esperado':>9} {'juez':>6} {'ok':>4}")
    for model in models:
        judge = LLMJudge(model=model, keep_alive=0, timeout=timeout)
        try:
            hits = 0
            for case in _CASES:
                result = judge.evaluate_binary(
                    instruction=case.instruction,
                    response=case.response,
                    criteria=_GROUNDING_CRITERIA,
                )
                got_pass = result.verdict == "pass"
                ok = got_pass == case.expected_pass
                hits += int(ok)
                print(
                    f"{model:22} {case.id:30} "
                    f"{'pass' if case.expected_pass else 'fail':>9} "
                    f"{result.verdict:>6} {'ok' if ok else 'FALLA':>5}"
                )
                if result.verdict == "error":
                    print(f"    -> raw: {result.critique[:200]!r}")
            print(f"  == accuracy {model}: {hits}/{len(_CASES)}\n")
        finally:
            judge.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibración de grounding del juez")
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="Modelos a evaluar en rol de juez (ej. qwen2.5-coder:7b qwen3.5:9b)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout por llamada en segundos (default 60, el mismo de LLMJudge)",
    )
    args = parser.parse_args()
    return run(args.models, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
