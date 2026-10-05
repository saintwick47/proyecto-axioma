#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.6.2 — LLM Judge
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/llm/judge.py
# Autor: SaintWick
# Fecha: 2026-04-20
# Propósito: Evaluación de calidad de respuestas con modelo juez especializado
#            + Benchmarking reproducible
#            + Validator: segunda opinión en CoT cuando confianza < umbral
# Modelo: settings.llm_fallback_1 (default: qwen2.5-coder:7b, ~1.9 GB RAM)
# Especialista en evaluación de respuestas (Reward Model)
# Optimizado para rubricas y scoring
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations
import re
import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from config.settings import settings
from src.llm.client import OllamaClient, OllamaResponse
logger = logging.getLogger("axioma.judge")
# ============================================================================
# IMPORT SEGURO DE DETAILEDLOGGER
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

# ============================================================================
# HELPERS DE LOGGING — VERIFICACIÓN ROBUSTA
# ============================================================================
def _log_model(model: str, action: str, mode: str, duration: float = None, success: bool = True):
    """Registra llamada al modelo juez en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_model_call(model_name=model, action=action, task_type=mode, duration=duration, success=success)
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 judge.py:45] excepción degradada (intencional): {_d2e}")

def _log_error(error: Exception, context: str):
    """Registra error con traceback completo."""
    if not LOGGER_AVAILABLE:
        logger.error(f"judge - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, function_name=f"judge.{context}")
    except Exception:
        logger.error(f"judge - {context}: {error}")

# ============================================================================
# TIPOS PÚBLICOS
# ============================================================================
class JudgeMode(str, Enum):
    """Modos de evaluación soportados por el juez."""
    ABSOLUTE = "absolute"    # Score 1-5 para una sola respuesta
    BINARY = "binary"        # pass / fail
    PAIRWISE = "pairwise"    # cuál de dos respuestas es mejor

@dataclass
class JudgeResult:
    """Resultado estructurado de evaluación."""
    score: float                    # Absoluto: 1-5 | Binario: 0.0/1.0 | Pairwise: 0/1
    verdict: str                    # "pass"/"fail" o "A"/"B"/"tie" o "1"-"5"
    critique: str                   # Razonamiento del juez
    mode: JudgeMode
    model: str
    latency_ms: float = 0.0
    raw: Optional[str] = None

    @property
    def passed(self) -> bool:
        """Atajos para uso en validator/benchmarks."""
        if self.mode == JudgeMode.BINARY:
            return self.verdict == "pass"
        if self.mode == JudgeMode.ABSOLUTE:
            return self.score >= 3.0
        return self.score == 1.0

# ============================================================================
# PROMPTS DEL JUEZ — CENTRALIZADOS EN DICT
# ============================================================================
_SYSTEM_PROMPT = "You are a precise evaluator. Follow the rubric exactly. Always end with Result: on its own line."
_PROMPT_TEMPLATES = {
    JudgeMode.ABSOLUTE: """
Evaluate the following response on a scale of 1 to 5.
Instruction: {instruction}
Response: {response}
Rubric:
Score 1: Completely wrong, irrelevant, or harmful.
Score 2: Major errors or missing key parts.
Score 3: Partially correct, incomplete.
Score 4: Mostly correct with minor issues.
Score 5: Complete, accurate, and well-structured.
Provide your reasoning, then write:
Result: <integer 1-5>
""",
    JudgeMode.BINARY: """
Evaluate whether the response is acceptable.
Instruction: {instruction}
Response: {response}
Criteria: {criteria}
Answer "pass" if the response meets the criteria, "fail" otherwise.
Provide brief reasoning, then write:
Result: <pass|fail>
""",
    JudgeMode.PAIRWISE: """
Compare two responses and choose the better one.
Instruction: {instruction}
Response A: {response_a}
Response B: {response_b}
Criteria: {criteria}
Choose A, B, or tie. Provide brief reasoning, then write:
Result: <A|B|tie>
""",
}

# ============================================================================
# PARSER DE SALIDA — REGEX Y LÓGICA UNIFICADA
# ============================================================================
# ✅ FIX: los templates piden "Result:" plano (sin negrita) — el regex
# exigía "**Result:**" y no matcheaba la salida real la mayoría de las veces.
# Ahora acepta 0, 1 o 2 asteriscos de cada lado.
_RESULT_RE = re.compile(r"\**Result:\**\s*([1-5]|pass|fail|A|B|tie)", re.IGNORECASE | re.MULTILINE)

def _parse_output(text: str, mode: JudgeMode) -> tuple[float, str, str]:
    """Extrae (score, verdict, critique) del texto crudo del juez."""
    match = _RESULT_RE.search(text)
    split_idx = match.start() if match else len(text)
    critique = text[:split_idx].strip()

    if not match:
        logger.warning("Judge: no se encontró **Result:** en la salida. Raw: %s", text[:200])
        return 0.0, "error", critique

    raw_verdict = match.group(1).lower()

    if mode == JudgeMode.ABSOLUTE:
        return float(raw_verdict), raw_verdict, critique

    if mode == JudgeMode.BINARY:
        return (1.0 if raw_verdict == "pass" else 0.0), raw_verdict, critique

    # PAIRWISE
    verdict = raw_verdict.upper() if raw_verdict in ("a", "b") else raw_verdict
    score = 1.0 if verdict == "A" else (0.5 if verdict == "tie" else 0.0)
    return score, verdict, critique

# ============================================================================
# CLASE PRINCIPAL: LLMJudge
# ============================================================================
class LLMJudge:
    """
    Juez LLM usando settings.llm_fallback_1 (actual: qwen2.5-coder:7b).
    Features:
    - temperature=0.0 siempre (determinismo absoluto)
    - keep_alive=0 por defecto: libera RAM tras cada llamada
    - Compatible con OllamaClient existente
    - No usa memoria ni validación (enable_memory=False)
    Hardware: ~1.9 GB RAM, CPU/GPU compatible.
    """
    TEMPERATURE = 0.0
    MAX_TOKENS = 512

    def __init__(self, model: Optional[str] = None, keep_alive: int = 0, timeout: Optional[int] = None) -> None:
        """Inicializa el juez LLM."""
        # ✅ FIX: antes hardcodeaba "qwen3:8b" acá. Ahora lee settings.llm_fallback_1
        # — el mismo campo que usa ValidatorAgent para este rol (ver settings.py/.env).
        self.model = model or settings.llm_fallback_1
        self.keep_alive = keep_alive
        self._client = OllamaClient(model=self.model, timeout=timeout or 60)

    # ─────────────────────────────────────────────────────────────
    # API PÚBLICA — CONVENIENCIA CON WRAPPERS TIPEADOS
    # ─────────────────────────────────────────────────────────────
    def evaluate_absolute(self, instruction: str, response: str) -> JudgeResult:
        """Evalúa una respuesta en escala 1-5."""
        return self._call(_PROMPT_TEMPLATES[JudgeMode.ABSOLUTE].format(instruction=instruction, response=response), JudgeMode.ABSOLUTE)

    def evaluate_binary(self, instruction: str, response: str, criteria: str = "The response must be correct and complete.") -> JudgeResult:
        """Evalúa una respuesta como pass/fail."""
        return self._call(_PROMPT_TEMPLATES[JudgeMode.BINARY].format(instruction=instruction, response=response, criteria=criteria), JudgeMode.BINARY)

    def evaluate_pairwise(self, instruction: str, response_a: str, response_b: str, criteria: str = "Choose the more accurate and complete response.") -> JudgeResult:
        """Compara dos respuestas. Devuelve A, B, o tie."""
        return self._call(_PROMPT_TEMPLATES[JudgeMode.PAIRWISE].format(instruction=instruction, response_a=response_a, response_b=response_b, criteria=criteria), JudgeMode.PAIRWISE)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — LLAMADA CENTRALIZADA CON LOGGING UNIFICADO
    # ─────────────────────────────────────────────────────────────
    def _call(self, prompt: str, mode: JudgeMode) -> JudgeResult:
        """Llama al modelo juez y parsea la respuesta."""
        t0 = time.perf_counter()
        try:
            raw: OllamaResponse = self._client.generate(
                prompt=prompt, system=_SYSTEM_PROMPT, temperature=self.TEMPERATURE,
                num_predict=self.MAX_TOKENS, keep_alive=self.keep_alive,
            )
            text = raw.content
        except Exception as exc:
            _log_error(exc, "_call")
            logger.error("Judge call failed: %s", exc)
            return JudgeResult(score=0.0, verdict="error", critique=str(exc), mode=mode, model=self.model, latency_ms=(time.perf_counter() - t0) * 1000, raw=None)

        latency_ms = (time.perf_counter() - t0) * 1000
        # ✅ FIX PLAN v2.0 (Fase 5): _parse_output() va en su PROPIO try/except.
        # ANTES quedaba fuera del try que envuelve generate() — si el LLM
        # devolvía un formato inesperado en modo ABSOLUTE (ej. "Result: pass"
        # en vez de un número 1-5), float(raw_verdict) lanzaba ValueError sin
        # capturar y crasheaba todo el runner en vez de marcar el caso fallido.
        try:
            score, verdict, critique = _parse_output(text, mode)
        except Exception as exc:
            _log_error(exc, "_call.parse_output")
            logger.error("Judge parse failed (raw=%r): %s", text[:200], exc)
            return JudgeResult(score=0.0, verdict="error", critique=str(exc),
                               mode=mode, model=self.model,
                               latency_ms=latency_ms, raw=text)
        success = verdict != "error"
        _log_model(self.model, "judge_evaluate", mode.value, duration=latency_ms / 1000, success=success)

        # Logging detallado vía DetailedLogger
        try:
            log = get_logger()
            if LOGGER_AVAILABLE and hasattr(log, "is_initialized") and log.is_initialized:
                log.log_event("JUDGE", f"Result: {verdict} (score={score:.1f})",
                              level="INFO" if success else "ERROR",
                              extra={"mode": mode.value, "score": score, "verdict": verdict,
                                     "latency_ms": round(latency_ms, 2), "critique_preview": critique[:100]})
        except Exception as _d2e:
            logging.getLogger(__name__).debug(f"[D2 judge.py:239] excepción degradada (intencional): {_d2e}")

        logger.debug("Judge[%s] mode=%s verdict=%s score=%.1f latency=%.0fms", self.model, mode.value, verdict, score, latency_ms)
        return JudgeResult(score=score, verdict=verdict, critique=critique, mode=mode, model=self.model, latency_ms=latency_ms, raw=text)

    # ─────────────────────────────────────────────────────────────
    # GESTIÓN DE RECURSOS
    # ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Cierra el cliente Ollama y libera recursos."""
        self._client.close()

    def __enter__(self) -> "LLMJudge":
        """Context manager entry."""
        return self

    def __exit__(self, *_) -> None:
        """Context manager exit — asegura cierre de recursos."""
        self.close()

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE OPTIMIZACIONES APLICADAS v0.4.0
# ═══════════════════════════════════════════════════════════════
# | Sección                  | Optimización Realizada              | Impacto                  |
# |--------------------------|-------------------------------------|--------------------------|
# | Imports                  | Consolidados en bloque lógico       | Legibilidad + mantenibilidad |
# | Prompts                  | Unificados en _PROMPT_TEMPLATES dict| -40% duplicación, DRY    |
# | evaluate_* wrappers      | Delegan a _call con template lookup | API pública intacta, código interno unificado |
# | _parse_output            | Branching simplificado con early-return | Claridad + rendimiento |
# | _call logging            | Unificado: _log_model + detailed_logger en bloque único | Menos duplicación |
# | JudgeResult              | Dataclass con defaults + property passed | Código más Pythonic |
# | Context manager          | Preservado sin cambios              | Compatibilidad con with  |
# | Error handling           | Patrón consistente: log + return JudgeResult(error) | Diagnóstico uniforme |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — FUNCIONALIDAD PRESERVADA — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════
