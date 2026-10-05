#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# axioma_model_bench_v2.py - Benchmark Mejorado de Modelos de Código
# Ruta: /ruta/a/axioma/tools/axioma_model_bench_v2.py
# Autor: AXIOMA Auto-Generated (basado en axioma_model_bench.py)
# Fecha: 2026-08-12 (migrado a tools/ el 2026-09-01)
# Propósito: Comparar modelos Ollama instalados en tareas de
#            generación de código Python. Mide velocidad,
#            calidad estructural, ejecución correcta y RAM.
#            Genera informe Markdown + JSON.
# ✅ 2026-09-01: migrado a tools/ (estaba en la raíz), CODE_CANDIDATES
#    actualizados a los modelos realmente instalados y OLLAMA_HOST
#    leído de settings.ollama_host (no hardcodeado).
# ✅ 2026-10-01: (1) `think` explícito en /api/generate (--think off|on|both|default;
#    antes no se enviaba y los modelos con razonamiento gastaban num_predict
#    pensando → respuesta vacía → exec 0% artefactual); (2) métricas de prefill
#    (prompt_eval_count/duration), truncado (done_reason=length) y respuesta
#    vacía por caso; (3) RSS real del proceso ollama/llama-server vía psutil
#    (ram_gb de /api/ps queda como referencia); (4) warm-up por modelo para
#    excluir la carga en frío de las métricas; (5) chequeo de CPU ociosa antes
#    de medir (--max-idle-cpu / --force); (6) resolución tolerante de nombres
#    instalados (:latest, hf.co/...); (7) código vacío ya no cuenta como
#    sintaxis válida; (8) candidato MoE Mellum2-12B-A2.5B (MXFP4) agregado.
# ✅ 2026-10-01 (2): fix del wrapper de ejecución en `lru_cache`: json.loads convertía
#    las tuplas en listas, `isinstance(_input[0], tuple)` daba False y se llamaba
#    LRUCache(_input) → 'not JSON serializable' en TODOS los modelos (bug previo
#    que afectaba las comparaciones anteriores). El prompt ahora exige que get()
#    devuelva -1 ante un miss, coherente con expected_outputs.
# ✅ 2026-10-02: --repeat N (N muestras por caso; exec/tok/s se promedian sobre todas
#    las corridas y se reporta el pass-rate por caso), --temperature (default 0.3,
#    igual que antes → comparable) y --seed (reproducible: seed+run en cada
#    repetición). Motivo: con 1 muestra a temp 0.3 los casos que fallan alternan
#    entre corridas y una diferencia de un caso no distingue un modelo de otro.
# ✅ 2026-10-02 (3): qwen3-vl:4b salió de CODE_CANDIDATES (es el modelo de visión,
#    no cumple rol de código; medirlo con tareas de código no tiene sentido).
#    La fórmula de overall_score NO cambió (comparable con corridas previas).
#    Recordatorio: correr con OLLAMA_NUM_PARALLEL=1 y sin otros procesos pesados.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations
import logging

import argparse
import ast
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    import psutil
    _PSUTIL = True
except ImportError:  # psutil opcional: sin él no hay RSS real ni chequeo de CPU
    psutil = None  # type: ignore[assignment]
    _PSUTIL = False


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

# ✅ 2026-09-01: leer host de settings (antes hardcodeado) con fallback.
try:
    from config.settings import settings as _settings
    OLLAMA_HOST = _settings.ollama_host
except Exception:  # noqa: BLE001 — ejecución standalone sin el paquete
    OLLAMA_HOST = "http://127.0.0.1:11434"

GENERATE_TIMEOUT = 180
EXEC_TIMEOUT = 10

# ✅ 2026-10-01: presupuesto de tokens cuando think=on (el razonamiento consume
# num_predict), warm-up mínimo, umbral de CPU ociosa y procesos cuyo RSS se suma.
THINK_BUDGET_TOKENS = 4096
WARMUP_NUM_PREDICT = 8
MAX_IDLE_CPU_PERCENT = 25.0
DEFAULT_TEMPERATURE = 0.3
RUNNER_PROCESS_NAMES: tuple[str, ...] = ("ollama", "llama-server", "llama_server")

# ✅ 2026-09-01: modelos realmente instalados (ollama list 2026-09-01).
# deepseek-r1:8b fue descartado por el proyecto (settings.py:51) y no está
# instalado — correrlo causaba "Ningún modelo disponible".
CODE_CANDIDATES: list[str] = [
    "qwen2.5-coder:7b",
    "qwen3:8b",
    "hf.co/JetBrains/Mellum2-12B-A2.5B-Instruct-GGUF-MXFP4_MOE:MXFP4_MOE",
]

CODER_SYSTEM_PROMPT = (
    "Eres un experto desarrollador Python. Genera código limpio y funcional.\n"
    "REGLAS (0 tolerancia):\n"
    "- Docstrings Google/NumPy en TODAS las funciones y clases\n"
    "- Type hints en TODOS los parámetros y retornos\n"
    "- try/except donde corresponda\n"
    "- Código sintácticamente válido\n"
    "FORMATO: respondé SOLO con ```python\n...\n``` sin preámbulos."
)


# ═══════════════════════════════════════════════════════════════
# CASOS DE PRUEBA
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CodeTestCase:
    id: str
    name: str
    level: str
    prompt: str
    max_tokens: int
    test_inputs: list[Any]
    expected_outputs: list[Any]
    func_name: str


CODE_TEST_CASES: list[CodeTestCase] = [
    CodeTestCase(
        id="sum_list",
        name="Suma de lista",
        level="Fácil",
        prompt=(
            "Escribe una función Python llamada suma_lista que reciba una lista de números "
            "y devuelva la suma total. Si la lista está vacía, devuelve 0. "
            "Incluye type hints y docstring. Solo código, sin explicaciones."
        ),
        max_tokens=512,
        test_inputs=[[1, 2, 3, 4, 5], [], [10, -5, 3]],
        expected_outputs=[15, 0, 8],
        func_name="suma_lista",
    ),
    CodeTestCase(
        id="factorial",
        name="Factorial",
        level="Fácil",
        prompt=(
            "Escribe una función Python llamada factorial que calcule el factorial de n. "
            "Maneja n=0 (devuelve 1) y n negativo (devuelve None). "
            "Incluye type hints y docstring. Solo código."
        ),
        max_tokens=512,
        test_inputs=[0, 5, 10, -3],
        expected_outputs=[1, 120, 3628800, None],
        func_name="factorial",
    ),
    CodeTestCase(
        id="fib_memo",
        name="Fibonacci memoizado",
        level="Medio",
        prompt=(
            "Escribe una función Python llamada fibonacci que calcule el n-ésimo número "
            "de Fibonacci usando memoización (cache). Maneja n=0 y n=1. "
            "Incluye type hints y docstring. Solo código."
        ),
        max_tokens=768,
        test_inputs=[0, 1, 10, 20, 30],
        expected_outputs=[0, 1, 55, 6765, 832040],
        func_name="fibonacci",
    ),
    CodeTestCase(
        id="is_palindrome",
        name="Palíndromo",
        level="Medio",
        prompt=(
            "Escribe una función Python llamada es_palindromo que determine si un string "
            "es palíndromo (ignora espacios, mayúsculas y puntuación). "
            "Incluye type hints y docstring. Solo código."
        ),
        max_tokens=768,
        test_inputs=["Anita lava la tina", "hello", "A man a plan a canal Panama", ""],
        expected_outputs=[True, False, True, True],
        func_name="es_palindromo",
    ),
    CodeTestCase(
        id="lru_cache",
        name="LRU Cache",
        level="Difícil",
        prompt=(
            "Escribe una clase Python llamada LRUCache que implemente una caché "
            "Least Recently Used con capacidad fija. Métodos: get(key) y put(key, value). "
            "Si la caché está llena, elimina el item menos usado recientemente. "
            "get(key) devuelve -1 si la clave no existe. "
            "Incluye type hints y docstring. Solo código."
        ),
        max_tokens=1024,
        test_inputs=[
            [("put", 1, 1), ("put", 2, 2), ("get", 1), ("put", 3, 3), ("get", 2)],
        ],
        expected_outputs=[[None, None, 1, None, -1]],
        func_name="LRUCache",
    ),
    CodeTestCase(
        id="expr_parser",
        name="Parser aritmético",
        level="Difícil",
        prompt=(
            "Escribe una función Python llamada evaluar_expresion que evalúe una expresión "
            "aritmética en string respetando precedencia de operadores "
            "(+,-,*,/, paréntesis). No uses eval() ni ast.literal_eval. "
            "Incluye type hints y docstring. Solo código."
        ),
        max_tokens=1024,
        test_inputs=["2 + 3 * 4", "(2 + 3) * 4", "10 / 2 - 3", "5 + ((1 + 2) * 4) - 3"],
        expected_outputs=[14, 20, 2.0, 14],
        func_name="evaluar_expresion",
    ),
]


# ═══════════════════════════════════════════════════════════════
# CLIENTE OLLAMA
# ═══════════════════════════════════════════════════════════════

def ollama_generate(
    model: str,
    prompt: str,
    system: str = "",
    temperature: float = 0.3,
    num_predict: int = 1024,
    timeout: int = GENERATE_TIMEOUT,
    think: Optional[bool] = False,
    seed: Optional[int] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
        "keep_alive": "2m",
    }
    if seed is not None:
        payload["options"]["seed"] = seed
    if think is not None:
        payload["think"] = think
    if system:
        payload["system"] = system
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
        # Modelos sin soporte de razonamiento rechazan `think` con 400:
        # reintentar sin el campo para no descartar el modelo por esto.
        if r.status_code == 400 and "think" in payload and "think" in r.text.lower():
            payload.pop("think")
            r = client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
        r.raise_for_status()
        return r.json()


def ollama_unload(model: str) -> None:
    try:
        with httpx.Client(timeout=30) as client:
            client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": 0},
            )
    except httpx.HTTPError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 axioma_model_bench_v2.py:212] excepción degradada (intencional): {_d2e}")


def get_loaded_ram_gb(model: str) -> float:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{OLLAMA_HOST}/api/ps")
            r.raise_for_status()
            for m in r.json().get("models", []):
                if m.get("name") == model or m.get("model") == model:
                    return round(m.get("size", 0) / (1024 ** 3), 2)
    except httpx.HTTPError as _d2e:
        logging.getLogger(__name__).debug(f"[D2 axioma_model_bench_v2.py:224] excepción degradada (intencional): {_d2e}")
    return 0.0


def check_ollama_available() -> bool:
    try:
        with httpx.Client(timeout=5) as client:
            r = client.get(f"{OLLAMA_HOST}/api/tags")
            return r.status_code == 200
    except httpx.HTTPError:
        return False


def list_installed_models() -> list[str]:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def resolve_installed_model(requested: str, installed: list[str]) -> Optional[str]:
    """Devuelve el nombre instalado que corresponde al pedido (exacto, con/sin :latest o por prefijo de tag)."""
    if requested in installed:
        return requested
    base = requested.removesuffix(":latest")
    for name in installed:
        if name == base or name.removesuffix(":latest") == base:
            return name
    for name in installed:
        if name.startswith(base + ":"):
            return name
    return None


def get_runner_rss_gb() -> float:
    """RSS total (GB) de los procesos ollama/llama-server; 0.0 si psutil no está disponible."""
    if not _PSUTIL:
        return 0.0
    total = 0
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not any(name.startswith(n) for n in RUNNER_PROCESS_NAMES):
                continue
            mem = proc.info.get("memory_info")
            if mem is not None:
                total += mem.rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return round(total / (1024 ** 3), 2)


def host_cpu_percent(interval: float = 2.0) -> float:
    """Uso de CPU global (%) durante `interval` s; -1.0 si psutil no está disponible."""
    if not _PSUTIL:
        return -1.0
    return float(psutil.cpu_percent(interval=interval))


# ═══════════════════════════════════════════════════════════════
# EXTRACCIÓN Y CALIDAD DE CÓDIGO
# ═══════════════════════════════════════════════════════════════

_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL)


def extract_code(text: str) -> str:
    text = _THINK_CLOSED_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


@dataclass
class CodeQuality:
    syntax_valid: bool = False
    has_docstrings: bool = False
    has_type_hints: bool = False
    lines: int = 0
    score: float = 0.0


def _fn_has_full_type_hints(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = [a for a in fn.args.args if a.arg not in ("self", "cls")]
    all_args_annotated = all(a.annotation is not None for a in args)
    return all_args_annotated and fn.returns is not None


def check_code_quality(code: str) -> CodeQuality:
    q = CodeQuality(lines=len(code.strip().splitlines()) if code.strip() else 0)
    if not code.strip():
        return q
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return q
    q.syntax_valid = True
    definitions = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if definitions:
        q.has_docstrings = all(ast.get_docstring(n) is not None for n in definitions)
    functions = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if functions:
        q.has_type_hints = all(_fn_has_full_type_hints(f) for f in functions)
    checks = sum([q.syntax_valid, q.has_docstrings, q.has_type_hints])
    q.score = round(100.0 * checks / 3, 1)
    return q


# ═══════════════════════════════════════════════════════════════
# EJECUCIÓN DE CÓDIGO GENERADO
# ═══════════════════════════════════════════════════════════════

def execute_generated_code(code: str, case: CodeTestCase) -> tuple[bool, list[Any], str]:
    all_pass = True
    outputs = []
    errors = []

    for inp, expected in zip(case.test_inputs, case.expected_outputs):
        # ✅ FIX: antes envolvía todo el wrapper en d*3 ('"""' construido con
        # chr(34) para no chocar visualmente con el f-string) — pero el
        # código generado SIEMPRE trae sus propios docstrings """ (el prompt
        # los exige). El primer """ del docstring del código generado
        # cerraba el string externo antes de tiempo, y el resto del
        # contenido (texto de docstring, código) quedaba como statements
        # sueltos fuera de cualquier string → root cause exacto del
        # IndentationError en la primera línea de cada docstring, 0% exec
        # pass en los 3 modelos por igual (bug del wrapper, no de los
        # modelos). El wrapper debe ser código ejecutable plano, no un
        # string — sacar el envoltorio d*3 por completo.
        wrapper = f"""
{code}

import json, sys
_input = json.loads({json.dumps(json.dumps(inp))})
try:
    if (isinstance(_input, list) and len(_input) > 0 and isinstance(_input[0], (list, tuple))
            and len(_input[0]) > 0 and _input[0][0] in ('put', 'get')):
        cache = LRUCache(2)
        results = []
        for op in _input:
            if op[0] == 'put':
                cache.put(op[1], op[2])
                results.append(None)
            elif op[0] == 'get':
                results.append(cache.get(op[1]))
        print(json.dumps({{'result': results}}))
    else:
        result = {case.func_name}(_input)
        print(json.dumps({{'result': result}}))
except Exception as e:
    print(json.dumps({{'error': str(e)}}))
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tf:
            tf.write(wrapper)
            tmp = tf.name
        try:
            proc = subprocess.run(
                [sys.executable, tmp],
                capture_output=True,
                text=True,
                timeout=EXEC_TIMEOUT,
            )
            if proc.returncode != 0:
                all_pass = False
                errors.append(proc.stderr.strip() or proc.stdout.strip())
                outputs.append(None)
                continue
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
                if "error" in data:
                    all_pass = False
                    errors.append(data["error"])
                    outputs.append(None)
                else:
                    actual = data.get('result')
                    outputs.append(actual)
                    if actual != expected:
                        all_pass = False
                        errors.append(f"Expected {expected}, got {actual}")
            except Exception:
                all_pass = False
                errors.append(f"Unparseable: {proc.stdout.strip()}")
                outputs.append(None)
        except subprocess.TimeoutExpired:
            all_pass = False
            errors.append("TIMEOUT")
            outputs.append(None)
        except Exception as e:
            all_pass = False
            errors.append(str(e))
            outputs.append(None)
        finally:
            Path(tmp).unlink(missing_ok=True)

    return all_pass, outputs, "; ".join(errors) if errors else ""


# ═══════════════════════════════════════════════════════════════
# RESULTADOS Y BENCHMARK
# ═══════════════════════════════════════════════════════════════

@dataclass
class CaseResult:
    case_id: str = ''
    case_name: str = ''
    level: str = ''
    ok: bool = False
    content: str = ''
    code: str = ''
    syntax_valid: bool = False
    has_docstrings: bool = False
    has_type_hints: bool = False
    quality_score: float = 0.0
    exec_pass: bool = False
    exec_errors: str = ''
    tokens_per_second: float = 0.0
    total_seconds: float = 0.0
    load_seconds: float = 0.0
    ram_gb: float = 0.0
    ram_rss_gb: float = 0.0
    think_mode: str = ''
    run: int = 1
    truncated: bool = False
    empty_response: bool = False
    thinking_chars: int = 0
    prompt_tokens: int = 0
    prefill_tokens_per_second: float = 0.0
    eval_tokens: int = 0
    error: str = ''


@dataclass
class ModelSummary:
    model: str = ''
    cases: list[CaseResult] = field(default_factory=list)
    total_quality: float = 0.0
    avg_quality: float = 0.0
    exec_pass_rate: float = 0.0
    syntax_rate: float = 0.0
    avg_tokens_per_sec: float = 0.0
    avg_latency_sec: float = 0.0
    max_ram_gb: float = 0.0
    max_rss_gb: float = 0.0
    think_mode: str = ''
    repeat: int = 1
    temperature: float = DEFAULT_TEMPERATURE
    seed: Optional[int] = None
    case_pass_rates: dict[str, float] = field(default_factory=dict)
    avg_prefill_tokens_per_sec: float = 0.0
    truncated_count: int = 0
    empty_count: int = 0
    overall_score: float = 0.0


def think_label(think: Optional[bool]) -> str:
    if think is None:
        return "default"
    return "on" if think else "off"


def _tokens_per_second(data: dict[str, Any]) -> float:
    eval_count = data.get('eval_count', 0)
    eval_dur = data.get('eval_duration', 0) / 1e9
    return round(eval_count / eval_dur, 1) if eval_dur > 0 else 0.0


def _prefill_tokens_per_second(data: dict[str, Any]) -> float:
    prompt_count = data.get('prompt_eval_count', 0)
    prompt_dur = data.get('prompt_eval_duration', 0) / 1e9
    return round(prompt_count / prompt_dur, 1) if prompt_dur > 0 else 0.0


def run_case(
    model: str,
    case: CodeTestCase,
    think: Optional[bool] = False,
    think_budget: int = THINK_BUDGET_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: Optional[int] = None,
    run: int = 1,
) -> CaseResult:
    res = CaseResult(case_id=case.id, case_name=case.name, level=case.level,
                     think_mode=think_label(think), run=run)
    num_predict = max(case.max_tokens, think_budget) if think else case.max_tokens
    timeout = max(GENERATE_TIMEOUT, num_predict // 3 + 60)
    try:
        data = ollama_generate(
            model, case.prompt, system=CODER_SYSTEM_PROMPT,
            temperature=temperature, num_predict=num_predict, timeout=timeout, think=think,
            seed=seed,
        )
    except httpx.HTTPError as e:
        res.error = str(e)
        return res

    res.ok = True
    res.content = data.get('response', '')
    res.thinking_chars = len(data.get('thinking') or '')
    res.truncated = data.get('done_reason') == 'length'
    res.tokens_per_second = _tokens_per_second(data)
    res.prefill_tokens_per_second = _prefill_tokens_per_second(data)
    res.prompt_tokens = int(data.get('prompt_eval_count', 0))
    res.eval_tokens = int(data.get('eval_count', 0))
    res.total_seconds = round(data.get('total_duration', 0) / 1e9, 2)
    res.load_seconds = round(data.get('load_duration', 0) / 1e9, 2)
    res.ram_gb = get_loaded_ram_gb(model)
    res.ram_rss_gb = get_runner_rss_gb()

    code = extract_code(res.content)
    res.code = code
    res.empty_response = not code.strip()
    quality = check_code_quality(code)
    res.syntax_valid = quality.syntax_valid
    res.has_docstrings = quality.has_docstrings
    res.has_type_hints = quality.has_type_hints
    res.quality_score = quality.score

    if quality.syntax_valid:
        exec_ok, _, exec_err = execute_generated_code(code, case)
        res.exec_pass = exec_ok
        res.exec_errors = exec_err
    elif res.empty_response:
        res.exec_errors = "EmptyResponse (truncado)" if res.truncated else "EmptyResponse"
    else:
        res.exec_errors = "SyntaxError"

    return res


def benchmark_model(
    model: str,
    cases: list[CodeTestCase],
    think: Optional[bool] = False,
    think_budget: int = THINK_BUDGET_TOKENS,
    warmup: bool = True,
    repeat: int = 1,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: Optional[int] = None,
) -> ModelSummary:
    sep = "=" * 60
    mode = think_label(think)
    print(f"\n{sep}\n{model}  [think={mode}  repeat={repeat}  temp={temperature}  seed={seed}]\n{sep}")
    summary = ModelSummary(model=model, think_mode=mode, repeat=repeat,
                           temperature=temperature, seed=seed)
    if warmup:
        print("  [warmup] cargando modelo ...", end=" ", flush=True)
        try:
            ollama_generate(model, "ok", num_predict=WARMUP_NUM_PREDICT,
                            timeout=GENERATE_TIMEOUT * 2, think=think)
            print("ok")
        except httpx.HTTPError as e:
            print(f"falló: {e}")
    for case in cases:
        for run in range(1, repeat + 1):
            run_seed = None if seed is None else seed + run - 1
            print(f"  [{case.id}] r{run}/{repeat} {case.name} ({case.level}) ...", end=" ", flush=True)
            res = run_case(model, case, think=think, think_budget=think_budget,
                           temperature=temperature, seed=run_seed, run=run)
            summary.cases.append(res)
            status = '✅' if res.exec_pass else '⚠️' if res.syntax_valid else '❌'
            flags = (" TRUNC" if res.truncated else "") + (" EMPTY" if res.empty_response else "")
            print(f"{status} qual={res.quality_score:.0f} exec={res.exec_pass} " +
                  f"tg={res.tokens_per_second}tok/s pp={res.prefill_tokens_per_second}tok/s " +
                  f"{res.total_seconds}s RSS={res.ram_rss_gb}GB ps={res.ram_gb}GB{flags}")
            time.sleep(1)
    ollama_unload(model)

    oks = [c for c in summary.cases if c.ok]
    if oks:
        summary.total_quality = sum(c.quality_score for c in oks)
        summary.avg_quality = summary.total_quality / len(oks)
        summary.exec_pass_rate = sum(1 for c in oks if c.exec_pass) / len(oks) * 100
        summary.syntax_rate = sum(1 for c in oks if c.syntax_valid) / len(oks) * 100
        summary.avg_tokens_per_sec = statistics.mean(c.tokens_per_second for c in oks)
        summary.avg_prefill_tokens_per_sec = statistics.mean(c.prefill_tokens_per_second for c in oks)
        summary.avg_latency_sec = statistics.mean(c.total_seconds for c in oks)
        summary.max_ram_gb = max(c.ram_gb for c in oks)
        summary.max_rss_gb = max(c.ram_rss_gb for c in oks)
        summary.truncated_count = sum(1 for c in oks if c.truncated)
        summary.empty_count = sum(1 for c in oks if c.empty_response)
        for case in cases:
            runs = [c for c in oks if c.case_id == case.id]
            if runs:
                summary.case_pass_rates[case.id] = sum(1 for c in runs if c.exec_pass) / len(runs) * 100
        summary.overall_score = round(
            summary.exec_pass_rate * 0.4 +
            summary.avg_quality * 0.4 +
            min(summary.avg_tokens_per_sec, 50) * 0.4,
            1
        )
    return summary


# ═══════════════════════════════════════════════════════════════
# REPORTE MARKDOWN
# ═══════════════════════════════════════════════════════════════

def _label(s: ModelSummary) -> str:
    return f"{s.model} [think={s.think_mode}]" if s.think_mode else s.model


def generate_markdown(summaries: list[ModelSummary]) -> str:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    model_names = [_label(s) for s in summaries]
    models_joined = ", ".join(model_names)
    lines = [
        "# 🧪 Benchmark Comparativo de Modelos de Código — AXIOMA",
        "",
        f"**Fecha:** `{now}`  ",
        f"**Host:** `{OLLAMA_HOST}`  ",
        f"**Modelos:** {models_joined}  ",
        f"**Casos:** {len(CODE_TEST_CASES)}  ",
        "",
        "---",
        "",
        "## 📊 Resumen Global",
        "",
        "| Modelo | Overall | Exec Pass | Avg Quality | Syntax | TG tok/s | PP tok/s | Latencia | RSS GB | ps GB | Trunc | Vacío |",
        "|--------|---------|-----------|-------------|--------|----------|----------|----------|--------|-------|-------|-------|",
    ]
    sorted_sums = sorted(summaries, key=lambda x: x.overall_score, reverse=True)
    for s in sorted_sums:
        lines.append(
            f"| `{_label(s)}` | **{s.overall_score:.1f}** | {s.exec_pass_rate:.0f}% | "
            f"{s.avg_quality:.1f} | {s.syntax_rate:.0f}% | {s.avg_tokens_per_sec:.1f} | "
            f"{s.avg_prefill_tokens_per_sec:.1f} | {s.avg_latency_sec:.1f}s | "
            f"{s.max_rss_gb:.1f} | {s.max_ram_gb:.1f} | {s.truncated_count} | {s.empty_count} |"
        )
    lines += ['', '---', '', '## 🔍 Detalle por Modelo', '']
    for s in sorted_sums:
        lines += [
            f"### {_label(s)}",
            "",
            f"- **Overall Score:** {s.overall_score:.1f}",
            f"- **Exec Pass Rate:** {s.exec_pass_rate:.0f}%",
            f"- **Avg Quality:** {s.avg_quality:.1f}/100",
            f"- **Syntax Rate:** {s.syntax_rate:.0f}%",
            f"- **Avg Tokens/s (generación):** {s.avg_tokens_per_sec:.1f}",
            f"- **Avg Tokens/s (prefill):** {s.avg_prefill_tokens_per_sec:.1f}",
            f"- **Avg Latency:** {s.avg_latency_sec:.1f}s",
            f"- **Max RSS (ollama):** {s.max_rss_gb:.1f} GB",
            f"- **Max RAM (/api/ps):** {s.max_ram_gb:.1f} GB",
            f"- **Truncados / Vacíos:** {s.truncated_count} / {s.empty_count}",
            f"- **Repeticiones:** {s.repeat} · **Temp:** {s.temperature} · **Seed:** {s.seed}",
            "- **Pass por caso:** " + ", ".join(f"{k} {v:.0f}%" for k, v in s.case_pass_rates.items()),
            "",
            "| Caso | Run | Nivel | Syntax | Docstr | Types | Quality | Exec | TG | PP | Lat | RSS | Trunc | Think chars |",
            "|------|-----|-------|--------|--------|-------|---------|------|----|----|-----|-----|-------|-------------|",
        ]
        for c in s.cases:
            syn = '✅' if c.syntax_valid else '❌'
            doc = '✅' if c.has_docstrings else '❌'
            typ = '✅' if c.has_type_hints else '❌'
            exe = '✅' if c.exec_pass else '❌'
            trunc = '⚠️' if c.truncated else '-'
            lines.append(
                f"| {c.case_name} | {c.run} | {c.level} | {syn} | {doc} | {typ} | "
                f"{c.quality_score:.0f} | {exe} | {c.tokens_per_second:.1f} | "
                f"{c.prefill_tokens_per_second:.1f} | {c.total_seconds:.1f}s | "
                f"{c.ram_rss_gb:.1f} | {trunc} | {c.thinking_chars} |"
            )
        lines += ['', '**Código generado:**', '']
        for c in s.cases:
            if c.run != 1 and c.exec_pass:
                continue
            lines += [
                f"#### {c.case_name} (run {c.run})",
                "",
                "```python",
                c.code or "# (sin código)",
                "```",
                "",
            ]
            if c.exec_errors:
                lines += [
                    f"**Error:** `{c.exec_errors[:200]}`",
                    "",
                ]
        lines += ['---', '']
    lines += ['', '## 🏆 Ranking Final', '']
    for i, s in enumerate(sorted_sums, 1):
        medal = '🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i}.'
        lines.append(f"{medal} **`{_label(s)}`** — Overall: {s.overall_score:.1f}")
    lines += ['', '---', '', '*Generado por AXIOMA Benchmark Engine v2*']
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

THINK_MODES: dict[str, list[Optional[bool]]] = {
    "off": [False],
    "on": [True],
    "both": [False, True],
    "default": [None],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark de modelos de código para AXIOMA")
    parser.add_argument("--models", nargs="+", default=CODE_CANDIDATES)
    parser.add_argument("--output-json", type=Path, default=Path("benchmark_code_results.json"))
    parser.add_argument("--output-md", type=Path, default=Path("benchmark_code_results.md"))
    parser.add_argument("--quick", action="store_true", help="Solo primeros 3 casos")
    parser.add_argument("--think", choices=sorted(THINK_MODES), default="off",
                        help="Razonamiento: off (think:false), on (think:true), both, default (no enviar el campo)")
    parser.add_argument("--think-budget", type=int, default=THINK_BUDGET_TOKENS,
                        help="num_predict mínimo cuando think=on")
    parser.add_argument("--no-warmup", action="store_true", help="No hacer warm-up de carga por modelo")
    parser.add_argument("--max-idle-cpu", type=float, default=MAX_IDLE_CPU_PERCENT,
                        help="CPU global máxima (%%) tolerada antes de medir")
    parser.add_argument("--force", action="store_true", help="Medir aunque la CPU no esté ociosa")
    parser.add_argument("--repeat", type=int, default=1,
                        help="Muestras por caso (exec/tok/s se promedian sobre todas)")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                        help="Temperatura de muestreo (0 = determinista)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed base; la repetición k usa seed+k-1")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat debe ser >= 1")
    if args.temperature == 0 and args.repeat > 1:
        print("AVISO: con --temperature 0 las repeticiones son redundantes", file=sys.stderr)

    if not check_ollama_available():
        print("ERROR: Ollama no responde en " + OLLAMA_HOST, file=sys.stderr)
        sys.exit(1)

    if not _PSUTIL:
        print("AVISO: psutil no instalado — sin RSS real ni chequeo de CPU ociosa", file=sys.stderr)
    else:
        idle = host_cpu_percent()
        print(f"CPU global antes de medir: {idle:.1f}%")
        if idle > args.max_idle_cpu and not args.force:
            print(f"ERROR: CPU ocupada ({idle:.1f}% > {args.max_idle_cpu:.1f}%): cerrá procesos "
                  "pesados (o usá --force); las mediciones serían inválidas", file=sys.stderr)
            sys.exit(2)

    installed = list_installed_models()
    print("Modelos instalados:", installed)
    available: list[str] = []
    for requested in args.models:
        resolved = resolve_installed_model(requested, installed)
        if resolved is not None and resolved not in available:
            available.append(resolved)
    if not available:
        print("ERROR: Ningún modelo disponible", file=sys.stderr)
        sys.exit(1)
    print("Modelos a evaluar:", available, "\n")

    cases = CODE_TEST_CASES[:3] if args.quick else CODE_TEST_CASES
    modes = THINK_MODES[args.think]

    summaries: list[ModelSummary] = []
    for model in available:
        for mode in modes:
            summaries.append(benchmark_model(
                model, cases, think=mode,
                think_budget=args.think_budget, warmup=not args.no_warmup,
                repeat=args.repeat, temperature=args.temperature, seed=args.seed,
            ))
            time.sleep(2)

    json_data = {
        "meta": {
            "date": datetime.now().isoformat(),
            "host": OLLAMA_HOST,
            "models": available,
            "think": args.think,
            "think_budget": args.think_budget,
            "warmup": not args.no_warmup,
            "repeat": args.repeat,
            "temperature": args.temperature,
            "seed": args.seed,
        },
        "summaries": [asdict(s) for s in summaries],
    }
    args.output_json.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding='utf-8')
    print("\n📄 JSON guardado:", args.output_json)

    md = generate_markdown(summaries)
    args.output_md.write_text(md, encoding='utf-8')
    print("📄 Markdown guardado:", args.output_md)

    print("\n" + "=" * 100)
    print("RESUMEN RÁPIDO")
    print("=" * 100)
    hdr = "{:<56} {:>8} {:>7} {:>8} {:>8} {:>8} {:>7} {:>6}".format(
        "Modelo", "Overall", "Exec%", "Quality", "TG t/s", "PP t/s", "RSS GB", "Trunc")
    print(hdr)
    for s in sorted(summaries, key=lambda x: x.overall_score, reverse=True):
        row = "{:<56} {:>8.1f} {:>7.0f} {:>8.1f} {:>8.1f} {:>8.1f} {:>7.1f} {:>6d}".format(
            _label(s)[:56], s.overall_score, s.exec_pass_rate, s.avg_quality,
            s.avg_tokens_per_sec, s.avg_prefill_tokens_per_sec, s.max_rss_gb, s.truncated_count)
        print(row)


if __name__ == "__main__":
    main()
