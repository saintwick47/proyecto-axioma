#!/usr/bin/env python3
"""
tools/log_health_report.py — Auto-reporte de salud desde logs (E2 del plan).

v2 (detector): además del resumen médico del sistema en
logs/axioma_health_<fecha>.md, ahora DIAGNOSTICA:

  - Clasificación de errores por tipo: infra/red, memoria, validación
    dinámica esperada (sandbox), otros — con ejemplos por tipo.
  - Correlación mínima WARNING ↔ ERROR (p. ej. HardwareFit marginal).
  - Reintentos/timeouts de Ollama (líneas "Sending request (attempt N/M)").
  - Ciclos de reparación del pipeline de código (sandbox FAILED → coder
    reinicia = Fase4) — señal positiva, no un error.
  - Hit-rate de caché C1 corregido: incluye FULL + PARTIAL (antes los FULL
    quedaban fuera del denominador y no contaban para nada).
  - Sección de ANOMALÍAS con umbrales y VERDICTO final (✅/⚠️/❌).
  - Comparación con el reporte previo (sidecar JSON) → detecta regresiones.

Secciones del reporte:
  - Requests por task_type (count/min/mediana/p95/max/ok%).
  - Caché/clasificación (C1): FULL/PARTIAL/pre-routing/clasif LLM → hit-rate.
  - Validador: PASS/FAIL %, score promedio, single-pass, skips A1.
  - Tools (B2): tasa de tool-call en el Agent Loop.
  - Modelos: generaciones y duración media por modelo.
  - Reintentos / timeouts.
  - Pipeline de código: validaciones dinámicas negativas vs reparaciones.
  - Calidad de código (CODE_QUALITY) y Memoria (ops MEMORY).
  - Errores clasificados (infra / memoria / esperados / otros).
  - Anomalías y verdicto.
  - Delta vs reporte anterior (si existe).

Uso:
  python tools/log_health_report.py                 # logs/reg_error.txt
  python tools/log_health_report.py --logs a.txt b.txt
  python tools/log_health_report.py -o /tmp/h.md
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════
# Parser de líneas del detailed_logger
# ═══════════════════════════════════════════════════════════════
_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\] "
    r"\[(\w+)\s*\] \[([A-Z_0-9]+)\s*\] (.*?)(?:\s+\|\s+(\{.*\}))?$"
)

_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"

_ATTEMPT_RE = re.compile(r"Sending request \(attempt (\d+)/(\d+)\)")
_SANDBOX_FAIL = "SANDBOX_EXECUTION — FAILED"
_TIMEOUT_RE = re.compile(
    r"timeout|timed\s*out|connection|refused|reset\s*by\s*peer|"
    r"ECONN|unreachable|socket|DNS", re.IGNORECASE)

# ═══════════════════════════════════════════════════════════════
# Umbrales de anomalías (ajustables por CLI con --umbral clave=valor)
# ═══════════════════════════════════════════════════════════════
THRESHOLDS: Dict[str, float] = {
    "validator_fail_pct": 20.0,     # > X% FAIL del validador LLM → ⚠️
    "single_pass_fails": 0,         # >0 respuestas degradadas conservadas → ⚠️
    "infra_errors": 0,              # >0 errores de infra/red → ⚠️
    "mem_errors": 0,                # >0 fallos de memoria → ⚠️
    "other_errors": 0,              # >0 errores sin clasificar → ⚠️
    "quality_min": 0.70,            # score calidad promedio < X → ⚠️
    "error_handling_min": 50.0,     # % error handling < X (con datos) → ⚠️
    "circuit_opens": 0,             # >0 circuit breaker OPEN → ❌
    "ok_min_pct": 100.0,            # ok% global < X → ❌
    "retry_warn_pct": 20.0,         # reintentos > X% de envíos → ⚠️
    "tool_loops_min": 3,            # con ≥X loops y tasa 0% → ℹ️
    "cache_min_hit": 20.0,          # cobertura caché < X% (con datos) → ℹ️
    "repair_warn_pct": 50.0,        # reparaciones < X% de sandbox fails → ℹ️
    "stale_hours_max": 12.0,        # ✅ ISSUE-004: log sin actividad > X h → ℹ️
                                    # (evita confundir "log viejo" con "todo ok")
}

# Patrones conocidos → causa probable (heurística de soporte)
_HINTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"✗\] add — short_term"),
     "fallo de add en short_term: si es sesión >1 h puede ser la ventana TTL "
     "expirada sin renovar (bug v0.6.9g, ya corregido — verificar fecha del "
     "log vs. fix). Si persiste post-fix, falta el motivo en el evento: "
     "enriquecer el emisor con error_type/reason."),
    (re.compile(r"Cannot add: memory expired"),
     "memoria short_term con TTL vencido (ventana no renovada)."),
    (re.compile(r"SANDBOX_EXECUTION — FAILED"),
     "resultado NEGATIVO esperado del validador dinámico (el código no pasó); "
     "dispara el ciclo de reparación Fase4. No es un error de infraestructura."),
    (re.compile(r"HardwareFit.*marginal"),
     "hardware al límite (CPU/RAM marginal): esperar latencias altas."),
]


def _safe_json(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_line(line: str) -> Optional[Dict[str, Any]]:
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    ts, level, category, msg, raw_json = m.groups()
    try:
        dt = datetime.strptime(ts, _TS_FMT)
    except ValueError:
        dt = None
    return {
        "ts": dt, "level": level, "category": category,
        "message": msg.strip(), "extra": _safe_json(raw_json),
    }


# ═══════════════════════════════════════════════════════════════
# Clasificación de errores
# ═══════════════════════════════════════════════════════════════
BUCKETS: Dict[str, str] = {
    "infra": "🌐 Infraestructura / red",
    "memoria": "🧠 Memoria (fallos de op)",
    "sandbox_esperado": "🔁 Validación dinámica esperada (sandbox)",
    "otros": "❔ Otros / sin clasificar",
}


def classify_error(ev: Dict[str, Any]) -> str:
    """Clasifica un evento ERROR en un bucket accionable."""
    msg = ev.get("message", "")
    cat = ev.get("category", "")
    if _SANDBOX_FAIL in msg:
        return "sandbox_esperado"
    if cat == "MEMORY":
        return "memoria"
    if _TIMEOUT_RE.search(msg) or _TIMEOUT_RE.search(str(ev.get("extra", {}))):
        return "infra"
    return "otros"


def hint_for(msg: str) -> Optional[str]:
    for pat, hint in _HINTS:
        if pat.search(msg):
            return hint
    return None


# ═══════════════════════════════════════════════════════════════
# Métricas auxiliares
# ═══════════════════════════════════════════════════════════════
def _pct(n: int, d: int) -> float:
    return (100.0 * n / d) if d else 0.0


def _dur_ms(extra: Dict[str, Any]) -> Optional[float]:
    """duration_ms (ms) o duration (segundos) → ms."""
    if "duration_ms" in extra and isinstance(extra.get("duration_ms"), (int, float)):
        return float(extra["duration_ms"])
    d = extra.get("duration")
    if isinstance(d, (int, float)):
        return float(d) * 1000.0
    return None


def _lat_stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    v = sorted(values)
    n = len(v)
    return {
        "count": n,
        "min": round(v[0], 1),
        "median": round(statistics.median(v), 1),
        "p95": round(v[int(n * 0.95) - 1], 1),
        "max": round(v[-1], 1),
        "avg": round(sum(v) / n, 1),
    }


# ═══════════════════════════════════════════════════════════════
# Colector
# ═══════════════════════════════════════════════════════════════
class HealthCollector:
    def __init__(self) -> None:
        self.turns: List[Dict[str, Any]] = []          # eventos Processing
        self.requests: List[Dict[str, Any]] = []       # Request completed
        self.l3_partial = 0
        self.l3_full = 0
        self.pre_routed = 0
        self.llm_classifications: List[float] = []     # duración ms
        self.validator_results: List[Dict[str, Any]] = []
        self.validation_skips: Dict[str, int] = {}
        self.single_pass_fails = 0
        self.tool_requested_events = 0
        self.tools_executed_total = 0
        self.chat_loops = 0
        self.errors: List[str] = []                    # msg[:120] (compat)
        self.error_events: List[Dict[str, Any]] = []   # eventos ERROR completos
        self.warning_events: List[Dict[str, Any]] = []
        self.circuit_opens = 0
        self.model_events: List[Dict[str, Any]] = []
        self.code_quality: List[Dict[str, Any]] = []
        self.mem_ops: Dict[str, Dict[str, Any]] = {}
        self.mem_embeddings: List[float] = []
        # v2 — reintentos y pipeline
        self.attempts_total = 0          # líneas "Sending request (attempt N/M)"
        self.attempts_retried = 0        # de esas, con N >= 2
        self.sandbox_fails = 0           # SANDBOX_EXECUTION FAILED (esperado)
        self.repairs = 0                 # coder reinicia tras sandbox FAILED
        self._last_sandbox_ts: Optional[datetime] = None
        self.first_ts: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None

    def _touch(self, ts: Optional[datetime]) -> None:
        if ts is None:
            return
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts
        if self.last_ts is None or ts > self.last_ts:
            self.last_ts = ts

    def feed(self, ev: Dict[str, Any]) -> None:
        self._touch(ev.get("ts"))
        msg = ev.get("message", "")
        extra = ev.get("extra", {})
        level = ev.get("level", "")

        if level == "ERROR":
            self.errors.append(msg[:120])
            self.error_events.append(ev)
        elif level == "WARNING":
            self.warning_events.append(ev)

        # Reintentos de Ollama: "Sending request (attempt 1/2)"
        m = _ATTEMPT_RE.search(msg)
        if m:
            self.attempts_total += 1
            if int(m.group(1)) >= 2:
                self.attempts_retried += 1

        # Validación dinámica negativa esperada → ciclo de reparación Fase4
        if _SANDBOX_FAIL in msg:
            self.sandbox_fails += 1
            self._last_sandbox_ts = ev.get("ts")
        elif msg.startswith("[✓] coder —") and "— started" in msg \
                and self._last_sandbox_ts is not None \
                and ev.get("ts") is not None \
                and (ev["ts"] - self._last_sandbox_ts).total_seconds() <= 3.0:
            self.repairs += 1
            self._last_sandbox_ts = None

        if msg.startswith("Processing:"):
            self.turns.append(ev)
        elif msg.startswith("Request completed:"):
            m2 = re.match(r"Request completed:\s*(\w+)", msg)
            ev["task"] = m2.group(1) if m2 else "?"
            self.requests.append(ev)
        elif "L3 Semantic PARTIAL HIT" in msg:
            self.l3_partial += 1
        elif "L3 Hybrid FULL HIT" in msg:
            self.l3_full += 1
        elif msg.startswith("Classification (pre-routing)"):
            self.pre_routed += 1
        elif msg.startswith("Classification:"):
            d = extra.get("classification_duration_ms")
            if isinstance(d, (int, float)):
                self.llm_classifications.append(float(d))
        elif msg.startswith("Validation result:"):
            self.validator_results.append({
                "passed": "PASS" in msg,
                "score": float(extra.get("score", 0) or 0),
                "attempt": int(extra.get("attempt", 1) or 1),
            })
        elif msg.startswith("Chat validation SKIPPED (A1)"):
            r = extra.get("reason", "?")
            self.validation_skips[r] = self.validation_skips.get(r, 0) + 1
        elif "Chat validation SKIPPED — respuesta sin grounding" in msg:
            self.validation_skips["grounding_missing"] = \
                self.validation_skips.get("grounding_missing", 0) + 1
        elif msg.startswith("Chat validation FAILED — keeping original"):
            self.single_pass_fails += 1
        elif msg.startswith("LLM requested tool calls"):
            self.tool_requested_events += 1
        elif msg.startswith("Chat Agent Loop - Iteration 1"):
            self.chat_loops += 1
        elif msg.startswith("Chat completed without tools or final synthesis done"):
            self.tools_executed_total += int(extra.get("tools_executed", 0) or 0)
        elif "Circuit breaker OPEN" in msg:
            self.circuit_opens += 1
        elif ev.get("category") == "MEMORY" and level != "ERROR":
            # Solo ops exitosas en la telemetría (los fallos van a errores)
            op = str(extra.get("operation", "") or "")
            coll = str(extra.get("collection", "") or "")
            if op:
                key = f"{op}/{coll}"
                e = self.mem_ops.setdefault(key, {"count": 0, "dur_ms": []})
                e["count"] += 1
                d_ms = _dur_ms(extra)
                if d_ms is not None:
                    e["dur_ms"].append(d_ms)
            if "get_embedding" in msg or op == "get_embedding":
                d_ms = _dur_ms(extra)
                if d_ms is not None:
                    self.mem_embeddings.append(d_ms)
        elif ev.get("category") == "CODE_QUALITY" and msg.startswith("Quality analysis completed"):
            self.code_quality.append({
                "quality_score": float(extra.get("quality_score", 0) or 0),
                "has_syntax": bool(extra.get("has_syntax")),
                "has_docstrings": bool(extra.get("has_docstrings")),
                "has_type_hints": bool(extra.get("has_type_hints")),
                "has_tests": bool(extra.get("has_tests")),
                "tests_count": int(extra.get("tests_count", 0) or 0),
                "has_error_handling": bool(extra.get("has_error_handling")),
                "attempt": int(extra.get("attempt", 1) or 1),
            })
        elif ev.get("category") == "MODEL" and "— generate" in msg and extra.get("success"):
            self.model_events.append({
                "model": extra.get("model", "?"),
                "duration_ms": float(extra.get("duration_ms", 0) or 0),
                "output_tokens": int(extra.get("output_tokens", 0) or 0),
            })


# ═══════════════════════════════════════════════════════════════
# Resumen de métricas (reutilizado por reporte, anomalías y sidecar)
# ═══════════════════════════════════════════════════════════════
def summarize(c: HealthCollector) -> Dict[str, Any]:
    total = len(c.requests)
    ok = sum(1 for r in c.requests
             if r.get("extra", {}).get("success", False))
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for ev in c.error_events:
        buckets.setdefault(classify_error(ev), []).append(ev)

    denom = c.l3_full + c.l3_partial + c.pre_routed + len(c.llm_classifications)
    hit_rate = _pct(c.l3_full + c.l3_partial, denom) if denom else 0.0

    val_total = len(c.validator_results)
    val_fails = sum(1 for v in c.validator_results if not v["passed"])
    val_fail_pct = _pct(val_fails, val_total) if val_total else 0.0

    q = c.code_quality
    quality_score = (sum(x["quality_score"] for x in q) / len(q)) if q else 0.0
    err_hand_pct = (100.0 * sum(1 for x in q if x["has_error_handling"]) / len(q)
                    if q else 0.0)

    retry_pct = (_pct(c.attempts_retried, c.attempts_total)
                 if c.attempts_total else 0.0)
    return {
        "requests": total,
        "ok_pct": _pct(ok, total),
        "errors_total": len(c.error_events),
        "errors_buckets": {k: len(v) for k, v in buckets.items()},
        "mem_errors": len(buckets.get("memoria", [])),
        "infra_errors": len(buckets.get("infra", [])),
        "sandbox_expected": len(buckets.get("sandbox_esperado", [])),
        "other_errors": len(buckets.get("otros", [])),
        "hit_rate": hit_rate,
        "l3_full": c.l3_full,
        "l3_partial": c.l3_partial,
        "pre_routed": c.pre_routed,
        "llm_classif": len(c.llm_classifications),
        "validator_fails": val_fails,
        "validator_total": val_total,
        "validator_fail_pct": val_fail_pct,
        "single_pass_fails": c.single_pass_fails,
        "circuit_opens": c.circuit_opens,
        "chat_loops": c.chat_loops,
        "tool_requested": c.tool_requested_events,
        "attempts_total": c.attempts_total,
        "attempts_retried": c.attempts_retried,
        "retry_pct": retry_pct,
        "sandbox_fails": c.sandbox_fails,
        "repairs": c.repairs,
        "quality_score": quality_score,
        "error_handling_pct": err_hand_pct,
        "quality_analyses": len(q),
        "warnings": len(c.warning_events),
        "mem_writes": sum(v["count"] for k, v in c.mem_ops.items()
                         if k.startswith("add")),
        "mem_reads": sum(v["count"] for k, v in c.mem_ops.items()
                         if k.startswith(("get_context", "get_recent"))),
        "window_ts": (f"{c.first_ts:%Y-%m-%d %H:%M}"
                      if c.first_ts else "n/a"),
        # ✅ ISSUE-004: staleness — cuántas horas pasaron desde el último evento
        # del log. Un log viejo NO debe leerse como "sin problemas".
        "last_ts_iso": (c.last_ts.isoformat() if c.last_ts else ""),
        "stale_hours": _staleness_hours(c.last_ts),
    }


def _staleness_hours(last_ts: Optional[datetime]) -> Optional[float]:
    """Horas transcurridas desde el último evento del log (None si no hay ts)."""
    if last_ts is None:
        return None
    try:
        ref = last_ts
        now = datetime.now(ref.tzinfo) if ref.tzinfo else datetime.now()
        return max(0.0, (now - ref).total_seconds() / 3600.0)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# Anomalías + verdicto
# ═══════════════════════════════════════════════════════════════
def compute_anomalies(s: Dict[str, Any],
                      th: Dict[str, float]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []

    def flag(icon: str, text: str) -> None:
        out.append((icon, text))

    if s["requests"]:
        if s["ok_pct"] < th["ok_min_pct"]:
            flag("❌", f"ok% global {s['ok_pct']:.0f}% < "
                       f"{th['ok_min_pct']:.0f}%")
        if s["hit_rate"] < th["cache_min_hit"] and s["requests"] >= 5:
            flag("ℹ️", f"cobertura de caché C1 baja: {s['hit_rate']:.0f}% "
                       f"(umbral {th['cache_min_hit']:.0f}%)")
    if s["circuit_opens"] > th["circuit_opens"]:
        flag("❌", f"circuit breaker OPEN: {s['circuit_opens']} (>0)")
    if s["infra_errors"] > th["infra_errors"]:
        flag("⚠️", f"errores de infraestructura/red: {s['infra_errors']}")
    if s["mem_errors"] > th["mem_errors"]:
        flag("⚠️", f"fallos de memoria (op ✗): {s['mem_errors']}")
    if s["other_errors"] > th["other_errors"]:
        flag("⚠️", f"errores sin clasificar: {s['other_errors']}")
    if s["validator_total"] and \
            s["validator_fail_pct"] > th["validator_fail_pct"]:
        flag("⚠️", f"validador FAIL {s['validator_fail_pct']:.0f}% "
                   f"(umbral {th['validator_fail_pct']:.0f}%)")
    if s["single_pass_fails"] > th["single_pass_fails"]:
        flag("⚠️", f"single-pass FAILs conservados: {s['single_pass_fails']}")
    if s["quality_analyses"]:
        if s["quality_score"] < th["quality_min"]:
            flag("⚠️", f"calidad de código promedio {s['quality_score']:.2f} "
                       f"< {th['quality_min']:.2f}")
        if s["error_handling_pct"] < th["error_handling_min"]:
            flag("⚠️", f"error handling {s['error_handling_pct']:.0f}% "
                       f"< {th['error_handling_min']:.0f}%")
    if s["attempts_total"] and s["retry_pct"] > th["retry_warn_pct"]:
        flag("⚠️", f"reintentos de red {s['attempts_retried']}/"
                   f"{s['attempts_total']} ({s['retry_pct']:.0f}% > "
                   f"{th['retry_warn_pct']:.0f}%)")
    if s["sandbox_fails"] and s["repairs"] < s["sandbox_fails"] * \
            th["repair_warn_pct"] / 100.0:
        flag("ℹ️", f"solo {s['repairs']} reparaciones Fase4 detectadas tras "
                   f"{s['sandbox_fails']} validaciones dinámicas negativas")
    if s["chat_loops"] >= th["tool_loops_min"] and s["tool_requested"] == 0:
        flag("ℹ️", f"tasa de tools 0% con {s['chat_loops']} loops de chat")

    # ✅ ISSUE-004: staleness — el log puede ser viejo (daemon no corrido, tests
    # que no levantan el daemon, etc.). Sin este aviso, un log desactualizado
    # se lee como "0 errores → todo bien", que es un falso positivo.
    stale_h = s.get("stale_hours")
    if stale_h is None:
        flag("ℹ️", "log sin timestamps parseables — no se puede evaluar "
                   "qué tan actual es esta ventana")
    elif stale_h > th["stale_hours_max"]:
        flag("ℹ️", f"log DESACTUALIZADO: último evento hace {stale_h:.1f} h "
                   f"(umbral {th['stale_hours_max']:.0f} h) — este reporte "
                   f"describe una ventana vieja, NO el estado actual. "
                   f"Corré el daemon/una sesión real antes de concluir nada.")

    if not out:
        flag("✅", "sin anomalías detectadas")
    return out


def _sev_key(icon: str) -> str:
    # Normaliza emojis con variation selector (⚠️ / ℹ️) a su base
    return icon.rstrip("\ufe0f")


def verdict_of(anoms: List[Tuple[str, str]]) -> str:
    sev = Counter(_sev_key(icon) for icon, _ in anoms)
    if sev["❌"]:
        return "❌ Revisar"
    if sev["⚠"]:
        return "⚠️ Atender avisos"
    return "✅ Sano"


# ═══════════════════════════════════════════════════════════════
# Comparación con reporte previo (sidecar JSON)
# ═══════════════════════════════════════════════════════════════
_METRIC_SPECS: List[Tuple[str, str, bool]] = [
    # (label, clave del sidecar, True = subir es bueno)
    ("Requests", "requests", True),
    ("ok%", "ok_pct", True),
    ("Errores totales", "errors_total", False),
    ("Errores infra/red", "infra_errors", False),
    ("Errores memoria", "mem_errors", False),
    ("Validador FAIL%", "validator_fail_pct", False),
    ("Single-pass FAILs", "single_pass_fails", False),
    ("Hit-rate C1 (%)", "hit_rate", True),
    ("Reintentos (%)", "retry_pct", False),
    ("Sandbox negativos", "sandbox_fails", False),
    ("Reparaciones Fase4", "repairs", True),
    ("Calidad código", "quality_score", True),
    ("Error handling (%)", "error_handling_pct", True),
    ("Circuit opens", "circuit_opens", False),
]


def _fmt_num(x: Any) -> str:
    if isinstance(x, (int, float)):
        return f"{x:.1f}"
    return str(x)


def compare_prev(prev: Dict[str, Any], s: Dict[str, Any]) -> List[str]:
    """Devuelve líneas delta (solo métricas presentes en ambos)."""
    lines: List[str] = []
    for label, key, up_is_good in _METRIC_SPECS:
        if key not in prev or key not in s:
            continue
        a, b = prev[key], s[key]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        delta = b - a
        eps = 1e-9
        if abs(delta) <= eps:
            arrow, icon = "→", "🟢"
        else:
            better = (delta > 0) == up_is_good
            arrow = "⬆" if delta > 0 else "⬇"
            icon = "🟢" if better else "🔴"
        lines.append(
            f"- {label}: {_fmt_num(b)} {arrow} (prev {_fmt_num(a)}) {icon}")
    return lines


# ═══════════════════════════════════════════════════════════════
# Reporte
# ═══════════════════════════════════════════════════════════════
def build_report(collector: HealthCollector, sources: List[str],
                 prev: Optional[Dict[str, Any]] = None,
                 thresholds: Optional[Dict[str, float]] = None) -> str:
    c = collector
    th = dict(THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    s = summarize(c)
    anoms = compute_anomalies(s, th)
    lines: List[str] = []
    ap = lines.append

    ap("# 📊 AXIOMA — Health report desde logs (v2 detector)")
    ap("")
    ap(f"- **Fuentes:** {', '.join(sources)}")
    rango = ""
    if c.first_ts and c.last_ts:
        rango = f" ({c.first_ts:%Y-%m-%d %H:%M} → {c.last_ts:%H:%M})"
    ap(f"- **Ventana:**{rango}")
    # ✅ ISSUE-004: vigencia explícita — cuán reciente es la ventana analizada.
    _stale = s.get("stale_hours")
    if _stale is None:
        ap("- **Vigencia:** ⚠️ desconocida (log sin timestamps parseables)")
    elif _stale > th["stale_hours_max"]:
        ap(f"- **Vigencia:** ⚠️ DESACTUALIZADO — último evento hace "
           f"**{_stale:.1f} h** (> {th['stale_hours_max']:.0f} h)")
    else:
        ap(f"- **Vigencia:** ✅ reciente — último evento hace {_stale:.1f} h")
    ap(f"- **Turns (Processing):** {len(c.turns)}  ·  "
       f"**Requests completadas:** {len(c.requests)}  ·  "
       f"**WARNING:** {s['warnings']}")
    ap(f"- **VERDICTO:** {verdict_of(anoms)}")
    ap("")

    # 0) Anomalías
    ap("## 🩺 Anomalías / diagnóstico")
    ap("")
    for icon, text in anoms:
        ap(f"- {icon} {text}")
    ap("")

    # 1) Latencia por task_type
    ap("## ⏱ Requests por task_type (latencia ms)")
    ap("")
    ap("| task | count | min | mediana | p95 | max | ok% |")
    ap("|---|---|---|---|---|---|---|")
    by_task: Dict[str, List[Dict[str, Any]]] = {}
    for r in c.requests:
        by_task.setdefault(r.get("task", "?"), []).append(r)
    for task in sorted(by_task):
        rs = by_task[task]
        lat = [float(r.get("extra", {}).get("latency_ms", 0) or 0) for r in rs]
        ok = sum(1 for r in rs if r.get("extra", {}).get("success", False))
        st = _lat_stats(lat)
        ap(f"| {task} | {len(rs)} | {st.get('min','-')} | "
           f"{st.get('median','-')} | {st.get('p95','-')} | "
           f"{st.get('max','-')} | {_pct(ok, len(rs)):.0f}% |")
    ap("")

    # 2) Caché / clasificación — hit-rate C1 (corregido: FULL + PARTIAL)
    ap("## 🧠 Caché / clasificación (C1 — hit-rate)")
    ap("")
    ap(f"- L3 **FULL HIT** (respuesta semántica completa): {c.l3_full}")
    ap(f"- L3 **PARTIAL HIT** (fallback a handler): {c.l3_partial}")
    ap(f"- **Pre-routing** determinístico (sin LLM): {c.pre_routed}")
    if c.llm_classifications:
        ap(f"- **Clasificación LLM**: {len(c.llm_classifications)} "
           f"(mediana {statistics.median(c.llm_classifications)/1000:.1f}s)")
    else:
        ap(f"- **Clasificación LLM**: {len(c.llm_classifications)}")
    ap("")
    denom = c.l3_full + c.l3_partial + c.pre_routed + len(c.llm_classifications)
    if denom:
        ap(f"**Hit-rate C1** = (full+partial)/(full+partial+pre-routing+LLM) "
           f"= **{s['hit_rate']:.1f}%**  "
           f"({'🟢' if s['hit_rate'] >= th['cache_min_hit'] else 'ℹ️ baja'})")
    else:
        ap("**Hit-rate C1**: sin datos (no hubo clasificación ni caché)")
    ap("")

    # 3) Validador
    ap("## ✅ Validador")
    ap("")
    passed = sum(1 for v in c.validator_results if v["passed"])
    total = len(c.validator_results)
    scores = [v["score"] for v in c.validator_results]
    if total:
        ap(f"- Intentos de validación LLM: {total} "
           f"(PASS {passed} · FAIL {total - passed} → "
           f"{_pct(total - passed, total):.0f}% FAIL)")
        if scores:
            ap(f"- Score promedio: {statistics.mean(scores):.1f} / 10")
    else:
        ap("- Intentos de validación LLM: 0")
    ap(f"- Single-pass FAILs (respuesta original conservada): "
       f"{c.single_pass_fails}")
    if c.validation_skips:
        sk = " · ".join(f"{k}: {v}" for k, v in sorted(c.validation_skips.items()))
        ap(f"- Skips A1/grounding: {sk}")
    ap("")

    # 4) Tools (B2)
    ap("## 🔧 Tools (B2 — Agent Loop)")
    ap("")
    rate = _pct(c.tool_requested_events, c.chat_loops) if c.chat_loops else 0.0
    ap(f"- Loops de chat con tools disponibles: {c.chat_loops}")
    ap(f"- Turns donde el LLM pidió tool(s): {c.tool_requested_events} "
       f"(tasa {rate:.0f}%)")
    ap(f"- Tools ejecutadas en total: {c.tools_executed_total}")
    ap("")

    # 5) Modelos
    if c.model_events:
        ap("## 🤖 Modelos (generaciones)")
        ap("")
        by_model: Dict[str, List[Dict[str, Any]]] = {}
        for m in c.model_events:
            by_model.setdefault(m["model"], []).append(m)
        ap("| modelo | generaciones | duración media (s) | tokens out |")
        ap("|---|---|---|---|")
        for model in sorted(by_model):
            ms = by_model[model]
            dur = statistics.mean([m["duration_ms"] for m in ms]) / 1000
            tok = sum(m["output_tokens"] for m in ms)
            ap(f"| {model} | {len(ms)} | {dur:.1f} | {tok} |")
        ap("")

    # 5b) Reintentos / timeouts (v2)
    ap("## 🔄 Reintentos / timeouts de Ollama")
    ap("")
    if c.attempts_total:
        ap(f"- Envíos registrados: {c.attempts_total} · reintentados "
           f"(attempt ≥2): {c.attempts_retried} "
           f"({_pct(c.attempts_retried, c.attempts_total):.0f}%)")
    else:
        ap("- Sin líneas 'Sending request' (¿log muy viejo o sin client?)")
    ap(f"- Errores de infra/red (timeout, conexión…): {s['infra_errors']}")
    ap("")

    # 5c) Pipeline de código (v2)
    ap("## 🔁 Pipeline de código (validación dinámica)")
    ap("")
    ap(f"- Validaciones dinámicas **negativas** (sandbox FAILED, esperado): "
       f"{c.sandbox_fails}")
    ap(f"- Ciclos de reparación Fase4 detectados (coder reinicia tras FAIL): "
       f"{c.repairs}")
    if c.sandbox_fails:
        ap(f"- Tasa de reparación: {_pct(c.repairs, c.sandbox_fails):.0f}%")
    ap("")

    # 5d) Calidad de código generado
    if c.code_quality:
        ap("## 🧪 Calidad de código generado (CODE_QUALITY)")
        ap("")
        n = len(c.code_quality)
        avg = sum(q["quality_score"] for q in c.code_quality) / n
        pct = lambda k: 100.0 * sum(1 for q in c.code_quality if q[k]) / n  # noqa: E731
        ap(f"- Análisis: {n} · score promedio: **{avg:.2f}**")
        ap(f"- type hints: {pct('has_type_hints'):.0f}% · tests: "
           f"{pct('has_tests'):.0f}% (promedio "
           f"{sum(q['tests_count'] for q in c.code_quality)/n:.1f} asserts) · "
           f"docstrings: {pct('has_docstrings'):.0f}% · error handling: "
           f"{pct('has_error_handling'):.0f}%")
        max_attempt = max(q["attempt"] for q in c.code_quality)
        ap(f"- Máximo de intentos de generación en un análisis: {max_attempt}")
        ap("")

    # 5e) Memoria
    if c.mem_ops:
        ap("## 🧠 Memoria (ops MEMORY)")
        ap("")
        emb = len(c.mem_embeddings)
        emb_avg = (sum(c.mem_embeddings) / emb) if emb else 0
        ap(f"- Writes: {s['mem_writes']} · Reads: {s['mem_reads']} · "
           f"Embeddings: {emb} (promedio {emb_avg:.0f} ms)")
        top = sorted(c.mem_ops.items(), key=lambda kv: -kv[1]["count"])[:6]
        for k, v in top:
            dur = ""
            if v["dur_ms"]:
                dur = f" · avg {sum(v['dur_ms'])/len(v['dur_ms']):.1f} ms"
            ap(f"  - {k}: {v['count']}{dur}")
        ap("")

    # 6) Errores clasificados (v2)
    ap("## 🚨 Errores clasificados")
    ap("")
    ap(f"- Líneas ERROR: {s['errors_total']}  ·  Circuit breaker OPEN: "
       f"{c.circuit_opens}")
    if not c.error_events:
        ap("- Sin errores 🎉")
    else:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for ev in c.error_events:
            buckets.setdefault(classify_error(ev), []).append(ev)
        for bucket in ("infra", "memoria", "sandbox_esperado", "otros"):
            evs = buckets.get(bucket, [])
            if not evs:
                continue
            ap(f"- **{BUCKETS[bucket]}**: {len(evs)}")
            for ev in evs[:3]:
                ts = f"{ev['ts']:%H:%M:%S}" if ev.get("ts") else "??"
                extra_txt = ""
                ex = ev.get("extra", {})
                det = ex.get("operation") or ex.get("agent") or \
                    ex.get("error") or ex.get("reason")
                if det:
                    extra_txt = f" · {det}"
                ap(f"  - `{ts}` {ev.get('message','')[:100]}{extra_txt}")
            if len(evs) > 3:
                ap(f"  - … y {len(evs) - 3} más")
            hint = hint_for(evs[0].get("message", ""))
            if hint:
                ap(f"  - 💡 {hint}")
        if c.warning_events:
            ap("")
            w_top = Counter(w.get("message", "")[:90]
                            for w in c.warning_events).most_common(3)
            for wmsg, n in w_top:
                ap(f"- WARNING ({n}x): {wmsg}")
    ap("")

    # 7) Delta vs reporte anterior
    if prev:
        ap("## 📈 Delta vs reporte anterior")
        ap("")
        for line in compare_prev(prev, s):
            ap(line)
        ap("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Sidecar JSON (comparación entre corridas)
# ═══════════════════════════════════════════════════════════════
def _load_prev(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None


def _dump_sidecar(path: Path, s: Dict[str, Any]) -> None:
    payload = dict(s)
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
DEFAULT_LOGS: List[str] = ["logs/reg_error.txt"]

# ✅ v0.6.9v (paso 5): con la frescura AUTOMÁTICA (un reporte por arranque de
# app) los archivos se acumularían sin límite. Se conservan los últimos N.
HEALTH_KEEP = 1   # el sidecar _last.json es la base de comparación


def _prune_health_reports(keep: int = HEALTH_KEEP,
                          logs_dir: Optional[Path] = None) -> int:
    """Borra los health reports más viejos. Devuelve cuántos eliminó."""
    try:
        d = Path(logs_dir) if logs_dir else Path("logs")
        reports = sorted(d.glob("axioma_health_*.md"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for old in reports[keep:]:
            old.unlink(missing_ok=True)
            removed += 1
        return removed
    except Exception:
        return 0


def generate_health_report(logs: Optional[List[str]] = None,
                           output: Optional[str] = None,
                           thresholds: Optional[Dict[str, float]] = None,
                           write_sidecar: bool = True) -> Optional[Path]:
    """Genera el health report desde los logs. Devuelve la ruta o None.

    ✅ v0.6.9v (paso 5 del plan): extraído de `main()` para poder AUTOMATIZAR la
    frescura sin pasar por argparse — `main()` usa `parse_args()`, así que
    llamarlo desde la app parsearía los argumentos de `main.py` (bug seguro).
    Lo usan: el arranque de la app (`main.py`) y el quality gate.
    """
    logs = list(logs or DEFAULT_LOGS)
    thresholds = dict(thresholds or {})

    collector = HealthCollector()
    read_any = False
    for name in logs:
        p = Path(name)
        if not p.exists():
            continue
        read_any = True
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            ev = parse_line(raw)
            if ev:
                collector.feed(ev)

    if not read_any:
        return None

    out_path = Path(output or (
        f"logs/axioma_health_{datetime.now():%Y%m%d_%H%M%S}.md"))

    prev: Optional[Dict[str, Any]] = None
    sidecar = out_path.with_name("axioma_health_last.json")
    if write_sidecar:
        prev = _load_prev(sidecar)

    report = build_report(collector, logs, prev=prev, thresholds=thresholds)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    if write_sidecar:
        _dump_sidecar(sidecar, summarize(collector))
    _prune_health_reports(logs_dir=out_path.parent)
    return out_path


async def refresh_health_report_async(logs: Optional[List[str]] = None,
                                     output: Optional[str] = None
                                     ) -> Optional[Path]:
    """Regenera el health report SIN bloquear el event loop (thread aparte).

    ✅ v0.6.9v (paso 5): es lo que llama el arranque de la app (`main.py`) para
    que la frescura sea automática en vez de depender de acordarse de correrlo.
    """
    import asyncio as _aio
    try:
        return await _aio.to_thread(generate_health_report, logs, output)
    except Exception:  # pragma: no cover — nunca debe romper el arranque
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Health report desde logs")
    parser.add_argument("--logs", nargs="+", default=DEFAULT_LOGS,
                        help="Archivos de log (detailed_logger)")
    parser.add_argument("-o", "--output", default=None,
                        help="Salida (default logs/axioma_health_<fecha>.md)")
    parser.add_argument("--umbral", action="append", default=[],
                        metavar="clave=valor",
                        help="Ajustar umbral p. ej. --umbral infra_errors=1")
    parser.add_argument("--no-sidecar", action="store_true",
                        help="No leer/escribir el sidecar de comparación")
    args = parser.parse_args()

    thresholds: Dict[str, float] = {}
    for kv in args.umbral:
        if "=" not in kv:
            print(f"[!] Umbral inválido (clave=valor): {kv}", file=sys.stderr)
            continue
        k, v = kv.split("=", 1)
        if k not in THRESHOLDS:
            print(f"[!] Umbral desconocido: {k} "
                  f"(válidos: {', '.join(sorted(THRESHOLDS))})",
                  file=sys.stderr)
            continue
        try:
            thresholds[k] = float(v)
        except ValueError:
            print(f"[!] Valor no numérico para {k}: {v}", file=sys.stderr)

    for name in args.logs:
        if not Path(name).exists():
            print(f"[!] Log no encontrado: {name}", file=sys.stderr)

    out_path = generate_health_report(
        logs=args.logs, output=args.output, thresholds=thresholds,
        write_sidecar=not args.no_sidecar)
    if out_path is None:
        print("[!] No hay logs que leer.", file=sys.stderr)
        return 1

    print(out_path.read_text(encoding="utf-8"))
    print(f"[+] Health report escrito en: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
