#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/run_evals.py — Runner de evals de AXIOMA (PLAN v2.0, Fase 5)

Carga tests/dataset_evals.yaml, ejecuta el agente REAL (CoderAgent /
ResearcherAgent) con cada instrucción y evalúa la respuesta con
LLMJudge.evaluate_binary(). Reporta pass/fail por caso y exit code != 0
si hay fallos.

Uso:
    python tools/run_evals.py                    # dataset por defecto
    python tools/run_evals.py --dataset path     # dataset alternativo
    python tools/run_evals.py --case eval-001    # un caso específico
    python tools/run_evals.py --max-cases 2      # límite de casos

Requisitos:
    - pyyaml
    - Ollama corriendo con settings.llm_fallback_1 (qwen2.5-coder:7b)
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import yaml
except ImportError:
    print("[!] Falta pyyaml: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

DEFAULT_DATASET = PROJECT_ROOT / "tests" / "dataset_evals.yaml"


def _build_agent(agent_name: str):
    """Instancia el agente real según el nombre del caso."""
    from src.domain.entities import Message

    if agent_name == "coder":
        from src.agents.coder import CoderAgent
        agent = CoderAgent()
        msg = None

        def run(instruction: str):
            return agent.execute(Message(content=instruction, role="user"))

        return run

    if agent_name == "researcher":
        from src.agents.researcher import ResearcherAgent
        agent = ResearcherAgent()

        def run(instruction: str):
            return agent.execute(Message(content=instruction, role="user"))

        return run

    raise ValueError(f"Agente desconocido: {agent_name} (soportados: coder, researcher)")


def _evaluate(instruction: str, response: str, criteria: str) -> tuple[bool, str]:
    """Evalúa la respuesta con LLMJudge. Devuelve (passed, verdict)."""
    from src.llm.judge import LLMJudge

    with LLMJudge() as judge:
        result = judge.evaluate_binary(instruction, response, criteria=criteria)
    return result.passed, result.verdict


def run_case(case: dict, verbose: bool = False) -> dict:
    """Ejecuta un caso del dataset. Devuelve dict con resultado."""
    case_id = case.get("id", "???")
    instruction = case["instruction"]
    agent_name = case["agent"]
    criteria = case["criteria"]

    try:
        run = _build_agent(agent_name)
        result = run(instruction)
        response = getattr(result, "content", "") or ""
    except Exception as e:  # noqa: BLE001 — un caso fallido no mata el runner
        return {
            "id": case_id, "agent": agent_name,
            "passed": False, "verdict": "runner_error",
            "critique": str(e)[:300], "response_preview": "",
        }

    if not response.strip():
        return {
            "id": case_id, "agent": agent_name,
            "passed": False, "verdict": "empty_response",
            "critique": "El agente devolvió respuesta vacía.",
            "response_preview": "",
        }

    try:
        passed, verdict = _evaluate(instruction, response, criteria)
    except Exception as e:  # noqa: BLE001 — el juez no debe crashear el runner
        passed, verdict = False, f"judge_error: {type(e).__name__}"
        critique = str(e)[:300]
    else:
        critique = ""

    return {
        "id": case_id, "agent": agent_name,
        "passed": passed, "verdict": verdict,
        "critique": critique,
        "response_preview": response[:200].replace("\n", " "),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Runner de evals de AXIOMA (Fase 5)")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET),
                        help="Ruta al dataset YAML")
    parser.add_argument("--case", help="Ejecutar solo un caso por id")
    parser.add_argument("--max-cases", type=int, default=0,
                        help="Límite de casos a ejecutar (0 = todos)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"[!] Dataset no encontrado: {dataset_path}", file=sys.stderr)
        return 2

    data = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
    cases = data.get("evals", [])
    if args.case:
        cases = [c for c in cases if c.get("id") == args.case]
        if not cases:
            print(f"[!] Caso no encontrado: {args.case}", file=sys.stderr)
            return 2
    if args.max_cases > 0:
        cases = cases[:args.max_cases]

    print(f"[*] Dataset: {dataset_path}")
    print(f"[*] Casos: {len(cases)}")

    results = []
    for case in cases:
        r = run_case(case, verbose=args.verbose)
        results.append(r)
        mark = "✅ PASS" if r["passed"] else "❌ FAIL"
        verdict = r["verdict"]
        critique = f" — {r['critique']}" if r["critique"] else ""
        preview = f"\n    ↳ respuesta: {r['response_preview']}..." if r["response_preview"] else ""
        print(f"{mark} [{r['id']}] ({r['agent']}) verdict={verdict}{critique}{preview}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n[+] Resultado: {passed}/{len(results)} casos pasaron")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
