#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — measure_task_config.py (v0.6.9v)
# ═══════════════════════════════════════════════════════════════
# Ruta: tools/measure_task_config.py
# Propósito: imprimir la config EFECTIVA por tarea (modelo + temperatura), que
#            es la única forma de comprobar que tocar `models.yaml` tuvo efecto.
#
# POR QUÉ EXISTE: en esta sesión el bug de `task_temperatures` (sin lector) y el
# default 0.7 que pisaba al modelo (ISSUE-041/048) **parecían** funcionar. La
# medición real mostró que TODAS las tareas corrían a 0.7. Regla del proyecto:
# medir antes/después (`docs/AGENT_GUIDE.md`, R8).
#
# Uso:
#   venv/bin/python tools/measure_task_config.py            # tabla por tarea
#   venv/bin/python tools/measure_task_config.py --json     # para comparar/diff
#   venv/bin/python tools/measure_task_config.py > before.txt   # baseline
#   ...cambio... ; venv/bin/python tools/measure_task_config.py > after.txt
#   diff before.txt after.txt
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Tareas representativas: las mapeadas + las que solo declaran temperatura +
# una sin nada declarado (para ver el default del código).
TASKS = [
    "code_generation", "code_review", "code_correction", "code_explanation",
    "general_chat", "complex_reasoning",
    "web_search", "data_analysis", "quality_validation",
    "weather_query", "time_query", "finance_query", "news_query",
    "summarization",
]


def collect() -> dict:
    from src.llm.config import LLMConfig
    cfg = LLMConfig()
    out = {}
    for task in TASKS:
        model = cfg.get_model_for_task(task)
        out[task] = {
            "model": model.name,
            "temperature": model.temperature,
            "source": _source(cfg, task),
        }
    return out


def _source(cfg, task: str) -> str:
    """De dónde sale la temperatura (regla de precedencia, ver AGENT_GUIDE §4 R2)."""
    if task in getattr(cfg, "task_temperatures", {}):
        return "task_mappings.<tarea> (solo temperature)"
    mapping = cfg.task_mappings.get(task)
    if mapping is not None and mapping.temperature is not None:
        return "task_mappings.<tarea>.temperature"
    if mapping is not None:
        return "models.<modelo>.temperature (hereda del modelo)"
    return "default del código (0.7)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Config efectiva por tarea")
    ap.add_argument("--json", action="store_true", help="Salida JSON")
    args = ap.parse_args()

    data = collect()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    print(f"{'TAREA':22s} {'MODELO':20s} {'TEMP':>5s}  ORIGEN")
    for task, info in data.items():
        print(f"{task:22s} {info['model']:20s} {info['temperature']:>5}  "
              f"{info['source']}")
    temps = {i["temperature"] for i in data.values()}
    print(f"\n{len(data)} tareas · temperaturas distintas: {len(temps)} "
          f"({sorted(temps)})")
    if len(temps) == 1:
        print("⚠️  TODAS iguales: revisar si la config por tarea se está leyendo "
              "(fue el síntoma de ISSUE-041).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
