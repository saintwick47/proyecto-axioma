#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# coder.py - Agente de Código (CoderAgent)
# Ruta: /ruta/a/axioma/src/agents/coder.py
# Autor: SaintWick
# Versión: 0.4.3
#
# Genera, corrige y explica código mediante un bucle Evaluator-Optimizer.
# Soporta generación por chunks, validación de calidad, blacklist L2 y
# detección de fallos de seguridad críticos (CWE). La lógica auxiliar
# está delegada a coder_core.py.
# ═══════════════════════════════════════════════════════════════
import logging
import time
from typing import Optional, Dict, Any, List, Tuple

from config.settings import settings
from src.domain.types import TaskType
from src.domain.entities import Message
from src.domain.exceptions import LLMError
from .base import BaseAgent, AgentResult
from src.llm.client_base import ScenarioType
from .coder_prompts import CoderPromptMixin

# ── Módulo auxiliar (mismo directorio) ──────────────────────────
from .coder_core import (
    # Constantes
    QUALITY_THRESHOLDS, QUALITY_WEIGHTS, BLACKLIST_TTL,
    # Dataclass
    GenerationCursor,
    # Logging
    log_agent, log_error, log_event,
    # Extracción
    extract_code, extract_filename, suggest_filename, extract_code_blocks,
    detect_refusal,
    # Complejidad
    is_complex_request,
    # Quality
    analyze_code_quality, get_quality_threshold_for_level,
    should_retry_for_quality, build_quality_critique,
    # Blacklist
    get_code_hash, is_code_blacklisted, blacklist_code, init_blacklist,
    # Chunked
    estimate_needs_chunking, extract_first_code_block, remove_overlap,
    detect_completion, update_open_structures, build_continuation_prompt,
)
# ✅ Fase 2: contrato de entrega de código (Fase 1, code_contract.py)
from .code_contract import parse_code_contract, build_contract_critique

# ============================================================================
# REPO-MAP (PLAN v2.0, Fase 4 — adaptado sin archivo nuevo)
# CodeIndex ya vive en axioma_auditor.py; se importa de ahí con fallback
# silencioso (criterio: no crear archivos nuevos cuando la lógica ya está
# en un archivo existente). El build() se cachea como singleton de módulo.
# ============================================================================
_REPO_INDEX = None
_REPO_INDEX_ERROR = None


def _get_repo_index():
    """Devuelve el singleton de CodeIndex o None (build cacheado)."""
    global _REPO_INDEX, _REPO_INDEX_ERROR
    if _REPO_INDEX is not None or _REPO_INDEX_ERROR is not None:
        return _REPO_INDEX
    try:
        from pathlib import Path as _Path
        from axioma_auditor import CodeIndex as _CodeIndex
        _idx = _CodeIndex(_Path(__file__).resolve().parent.parent.parent)
        _idx.build()
        _REPO_INDEX = _idx
    except Exception as e:  # noqa: BLE001 — fallback silencioso
        _REPO_INDEX_ERROR = e
        logger.debug(f"[CoderAgent] repo-map no disponible: {e}")
        _REPO_INDEX = None
    return _REPO_INDEX


def _build_repo_context(query: str) -> str:
    """Construye el bloque CONTEXTO DEL REPO a partir del query.

    Devuelve "" si el repo-map está deshabilitado o no está disponible.
    """
    if not settings.repo_map_enabled:
        return ""
    idx = _get_repo_index()
    if idx is None:
        return ""
    try:
        # Keywords del query: palabras de 3+ chars que parezcan símbolos
        keywords = [
            w.strip(".,;:()[]{}'\"")
            for w in query.split()
            if len(w.strip(".,;:()[]{}'\"")) >= 3
        ]
        symbols = []
        for kw in keywords[:5]:
            for path, line, content in idx.grep(kw, regex=False)[:settings.repo_map_max_symbols]:
                symbols.append(f"  {path}:{line}: {content.strip()[:100]}")
                if len(symbols) >= settings.repo_map_max_symbols:
                    break
            if len(symbols) >= settings.repo_map_max_symbols:
                break
        if not symbols:
            return ""
        header = "CONTEXTO DEL REPO (referencias a símbolos del proyecto):\n"
        return header + "\n".join(symbols) + "\n"
    except Exception as e:  # noqa: BLE001 — fallback silencioso
        logger.debug(f"[CoderAgent] repo-map build error: {e}")
        return ""

# ============================================================================
# IMPORTS OPCIONALES CON FALLBACK SEGURO
# ============================================================================
try:
    from src.utils.ambiguity_detector import AmbiguityDetector
    AMBIGUITY_DETECTOR_AVAILABLE = True
except ImportError:
    AMBIGUITY_DETECTOR_AVAILABLE = False

try:
    from src.prompts import ContextLayer
    CONTEXT_LAYER_AVAILABLE = True
except ImportError:
    CONTEXT_LAYER_AVAILABLE = False
    ContextLayer = None

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# PATRONES DE EARLY EXIT (Seguridad Crítica)
# ═══════════════════════════════════════════════════════════════
# Si se detectan durante los primeros chunks, se aborta la generación
# para ahorrar tokens y forzar un reintento con crítica inmediata.
CRITICAL_CWE_PATTERNS = [
    ("eval(", "CWE-95: Unsafe eval() usage detected"),
    ("exec(", "CWE-95: Unsafe exec() usage detected"),
    ("os.system(", "CWE-78: OS command injection via os.system detected"),
    ("subprocess.call(", "CWE-78: Potential shell injection via subprocess detected"),
    ("pickle.loads(", "CWE-502: Unsafe deserialization detected"),
]

class CriticalSecurityFlawDetected(Exception):
    """Excepción lanzada cuando se detecta un patrón CWE crítico durante la generación temprana."""
    pass


# ═══════════════════════════════════════════════════════════════
# CLASE: CoderAgent v0.4.2
# ═══════════════════════════════════════════════════════════════
class CoderAgent(CoderPromptMixin, BaseAgent):
    """
    Agente de código con bucle Evaluator-Optimizer. Genera código mediante
    chunking progresivo y lo valida iterativamente contra métricas de calidad
    y Blacklist L2 SQLite, inyectando críticas accionables en reintentos sucesivos.

    ✅ v0.4.2: Añadido Early Exit con excepción específica para ahorro de tokens,
    corrección de mutación de estado y consolidación DRY de errores.
    """
    AGENT_TYPE = "coder"
    # ✅ FIX: antes el timeout de 420s (code) dependía de que el nombre del
    # modelo contuviera "code"/"coder" (client_base.py lo adivinaba). Ahora
    # es explícito acá — sobrevive a cualquier renombre del modelo de código.
    LLM_SCENARIO = ScenarioType.CODE

    DEFAULT_SYSTEM_PROMPT = (
        "Eres un experto desarrollador Python. Genera código limpio y funcional.\n"
        "REGLAS (0 tolerancia):\n"
        "- Docstrings Google/NumPy en TODAS las funciones y clases\n"
        "- Type hints en TODOS los parámetros y retornos\n"
        "- try/except donde corresponda\n"
        "- Código sintácticamente válido\n"
        "FORMATO: responde SOLO con ```python\n...\n``` sin preámbulos.\n"
        "Máximo 2 párrafos de explicación DESPUÉS del código si es necesario.\n"
        "El código será validado automáticamente — docstrings y type hints son obligatorios."
    )

    TEMPERATURE_BY_LEVEL: dict = {1: 0.2, 2: 0.3, 3: 0.3, 4: 0.1}

    # ✅ Fase 2: code_generation lee de settings.code_generation_max_tokens
    # (default 2048, antes 1024 hardcodeado acá) — CoderAgent() se instancia
    # sin args en dispatcher.py:486, así que _get_max_tokens() cae en este
    # dict (self.config.max_tokens queda en el sentinel 2048 del __init__,
    # ver _get_max_tokens más abajo).
    MAX_TOKENS_BY_TASK: dict = {
        "code_explanation":   512,
        "code_review":        512,
        "code_debugging":     768,
        "code_optimization":  768,
        "code_generation":    settings.code_generation_max_tokens,
        "code_testing":      1024,
        "code_documentation": 768,
        "code_migration":    1024,
        "code_deployment":    512,
    }
    MAX_TOKENS_DEFAULT = 1024

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        enable_memory: bool = True,
        enable_validation: bool = True,
        max_retries: int = 3,
        system_prompt: Optional[str] = None,
        language: Optional[str] = None,
        max_tokens: int = 2048,
    ):
        """Inicializa agente de código con configuración personalizada."""
        super().__init__(
            model=model or settings.llm_code_model,
            temperature=temperature or 0.3,
            # ✅ FIX: antes no se pasaba — el parámetro `timeout` del
            # constructor se aceptaba y se descartaba sin usarse.
            timeout=timeout,
            enable_memory=enable_memory,
            enable_validation=enable_validation,
            max_retries=max_retries,
            system_prompt=system_prompt or self.DEFAULT_SYSTEM_PROMPT,
            max_tokens=max_tokens,
        )
        self.language = language
        self._code_stats            = {"lines_generated": 0, "languages_used": {}}
        self._optimizer_stats       = {"total_iterations": 0, "improvements_applied": 0}
        self._consecutive_failures  = 0
        self._failure_threshold     = 2
        self.ambiguity_detector     = None

        if AMBIGUITY_DETECTOR_AVAILABLE:
            try:
                self.ambiguity_detector = AmbiguityDetector()
            except Exception as e:
                logger.warning(f"AmbiguityDetector init failed: {e}")

        # Blacklist L2 delegada a coder_core
        self._validation_blacklist = init_blacklist()

        logger.info(
            f"CoderAgent initialized: model={self.config.model}, "
            f"max_tokens={self.config.max_tokens}, "
            f"blacklist={'✅' if self._validation_blacklist else '❌'}"
        )

    # ═══════════════════════════════════════════════════
    # FAILURE TRACKING Y FALLBACK
    # ═══════════════════════════════════════════════════
    def _reset_failure_count(self) -> None:
        """Resetea contador de fallos consecutivos."""
        self._consecutive_failures = 0
        log_event("FALLBACK", "Failure count reset after success")

    def _increment_failure_count(self, request: str) -> bool:
        """Incrementa contador y determina si se activa fallback."""
        self._consecutive_failures += 1
        log_event("FALLBACK", f"Failure count: {self._consecutive_failures}")
        if self._consecutive_failures >= self._failure_threshold:
            log_event("FALLBACK_TRIGGERED",
                      f"Fallback tras {self._consecutive_failures} fallos",
                      level="WARNING", extra={"request_preview": request[:100]})
            return True
        return False

    def _get_fallback_questions(self, request: str) -> List[str]:
        """Obtiene preguntas de clarificación para requests ambiguos."""
        if AMBIGUITY_DETECTOR_AVAILABLE and self.ambiguity_detector:
            try:
                return self.ambiguity_detector.get_clarification_questions(request)
            except Exception as e:
                logger.warning(f"Failed to get clarification questions: {e}")
        return [
            "¿Podés ser más específico sobre qué necesitás?",
            "¿Hay algún requisito o restricción que deba considerar?",
        ]

    # ═══════════════════════════════════════════════════
    # TEMPERATURE Y TOKENS
    # ═══════════════════════════════════════════════════
    def _get_temperature_for_level(self, level: int) -> float:
        return self.TEMPERATURE_BY_LEVEL.get(level, 0.3)

    def _get_max_tokens(self, task_type: str) -> int:
        if self.config.max_tokens != 2048:
            return self.config.max_tokens
        return self.MAX_TOKENS_BY_TASK.get(task_type, self.MAX_TOKENS_DEFAULT)

    def _detect_critical_cwe_early(self, code: str) -> Optional[str]:
        """Escaneo ligero durante los primeros chunks para abortar si hay fallos críticos."""
        for pattern, reason in CRITICAL_CWE_PATTERNS:
            if pattern in code:
                return reason
        return None

    def _generate_chunked(
        self,
        prompt: str,
        system_prompt: str,
        temperature: float,
        timeout: Optional[int] = None,
    ) -> Tuple[str, int]:
        """Genera código en chunks progresivos hasta completar el archivo."""
        chunk_max_tokens = getattr(settings, 'chunk_max_tokens', 1800)
        chunk_max_iter   = getattr(settings, 'chunk_max_iterations', 20)
        overlap_lines    = getattr(settings, 'chunk_overlap_lines', 15)
        # ✅ v0.7.5 (ISSUE-074/A3b): presupuesto TOTAL por archivo. Sin esto el
        # único tope era `chunk_max_iter × chunk_max_tokens` (20 × 1800 = 36.000
        # tokens ⇒ ~76 min a 7,9 tok/s), muy por encima de los timeouts reales.
        chunk_token_budget = getattr(settings, 'chunk_total_token_budget', 8192)
        tokens_usados = 0
        effective_timeout = timeout or settings.get_timeout_for_code_level(4)

        cursor = GenerationCursor()
        original_prompt = prompt

        logger.info(
            f"🔄 Iniciando chunked generation "
            f"(max_iter={chunk_max_iter}, chunk_tokens={chunk_max_tokens}, "
            f"presupuesto_total={chunk_token_budget} tokens)"
        )

        for i in range(chunk_max_iter):
            current_prompt = (
                original_prompt if i == 0
                else build_continuation_prompt(cursor, original_prompt, settings)
            )
            try:
                chunk_raw = self._generate(
                    prompt=current_prompt, system=system_prompt,
                    max_tokens=chunk_max_tokens, temperature=temperature,
                    timeout=effective_timeout,
                )
            except Exception as e:
                logger.error(f"Chunked generation error en chunk {i}: {e}")
                break

            chunk_code = extract_first_code_block(chunk_raw)
            if not chunk_code.strip():
                logger.warning(f"Chunk {i} vacío — deteniendo generación")
                break

            chunk_clean = (
                remove_overlap(cursor.accumulated_code, chunk_code, overlap_lines)
                if cursor.accumulated_code else chunk_code
            )
            if not chunk_clean.strip():
                logger.warning(f"Chunk {i} completamente solapado — EOF implícito")
                cursor.is_complete = True
                break

            cursor.accumulated_code += ("\n" if cursor.accumulated_code else "") + chunk_clean
            cursor.current_line     += len(chunk_clean.splitlines())
            cursor.chunk_count      += 1
            update_open_structures(cursor, chunk_clean)

            # ✅ v0.7.5 (ISSUE-074/A3b): tokens REALES del chunk (telemetría
            # ISSUE-082) contra el presupuesto total del archivo.
            _resp = getattr(self, "_last_llm_response", None)
            tokens_usados += int(getattr(_resp, "eval_count", 0) or 0)

            log_event("CHUNKED_GENERATION", f"Chunk {i + 1} generado", extra={
                "chunk_index": i,
                "lines_this_chunk": len(chunk_clean.splitlines()),
                "total_lines": cursor.current_line,
                "open_structures": cursor.open_structures,
                "tokens_chunk": int(getattr(_resp, "eval_count", 0) or 0),
                "tokens_acumulados": tokens_usados,
                "presupuesto_total": chunk_token_budget,
            })
            logger.info(
                f"✅ Chunk {i + 1}: +{len(chunk_clean.splitlines())} líneas "
                f"(total: {cursor.current_line})"
            )

            if tokens_usados >= chunk_token_budget:
                logger.warning(
                    f"🛑 Presupuesto de tokens agotado ({tokens_usados}/"
                    f"{chunk_token_budget}) tras {cursor.chunk_count} chunks — "
                    f"se entrega el código acumulado (A3b)"
                )
                log_event("CHUNKED_BUDGET_EXHAUSTED",
                          f"Presupuesto agotado en chunk {i + 1}", level="WARNING",
                          extra={"tokens_usados": tokens_usados,
                                 "presupuesto": chunk_token_budget,
                                 "chunks": cursor.chunk_count})
                break

            # ═══════════════════════════════════════════════════════════════
            # ✅ OPTIMIZACIÓN: EARLY EXIT CWE v2
            # Si se detecta, lanzamos excepción para evitar desperdiciar tokens
            # y permitir que execute() inyecte una crítica específica inmediata.
            # ═══════════════════════════════════════════════════════════════
            if cursor.chunk_count <= 2:
                cwe_flaw = self._detect_critical_cwe_early(cursor.accumulated_code)
                if cwe_flaw:
                    logger.warning(f"🚨 EARLY EXIT: {cwe_flaw} detectado en chunk {cursor.chunk_count}. Abortando para ahorrar tokens.")
                    log_event("EARLY_EXIT_CWE", cwe_flaw, level="WARNING", extra={"chunk": cursor.chunk_count})
                    raise CriticalSecurityFlawDetected(cwe_flaw)

            # ✅ Fase 6: detect_completion() es heurística de texto (fences,
            # balance de estructuras) — si Ollama dice que cortó por
            # max_tokens (done_reason="length", ver base.py H6 y
            # client_base.py), eso pesa más que la heurística: nunca marcar
            # is_complete=True en ese caso, forzar otro chunk. was_truncated
            # solo bloquea el positivo (EOF), nunca fuerza uno falso — si
            # was_truncated es False, detect_completion() decide como antes.
            last_llm = getattr(self, "_last_llm_response", None)
            chunk_was_truncated = bool(last_llm and getattr(last_llm, "was_truncated", False))
            if chunk_was_truncated:
                logger.info(f"Chunk {i + 1} cortado por max_tokens (done_reason=length) — sigue de largo")
            elif detect_completion(chunk_raw) or detect_completion(chunk_code):
                cursor.is_complete = True
                logger.info(f"✅ EOF detectado en chunk {i + 1}")
                break

        if not cursor.is_complete:
            logger.warning(
                f"⚠️ Chunked generation terminó por límite de iteraciones "
                f"({chunk_max_iter}), puede estar incompleto"
            )
        # ✅ Fase 7: not cursor.is_complete ya es la señal correcta de
        # "no se encontró EOF limpio" — cubre tanto el caso de agotar
        # chunk_max_iter como el caso (más común) de que el último chunk
        # haya sido was_truncated=True en el loop de arriba.
        return cursor.accumulated_code, cursor.chunk_count, not cursor.is_complete

    # ═══════════════════════════════════════════════════
    # EXECUTE — MÉTODO PRINCIPAL
    # ═══════════════════════════════════════════════════
    def execute(
        self,
        input_msg: Message,
        task_type: Optional[str] = None,
        previous_critique: Optional[str] = None,
        attempt: int = 1,
        ambiguity_level: str = "none",
        show_warning: bool = False,
        warning_message: Optional[str] = None,
        clarification_answers: Optional[Dict[int, str]] = None,
        search_context: Optional[str] = None,
        session_id: Optional[str] = None,  # ✅ plan_memory (B): memoria por sesión
        user_id: Optional[str] = None,    # ✅ UI de usuarios (2026-08-31): identidad
    ) -> AgentResult:
        """Ejecuta el CoderAgent para generar, depurar o explicar código."""
        start_time = time.time()
        try:
            # ── Detección automática de task_type ───────────────────────────
            if not task_type:
                c = input_msg.content.lower()
                if any(w in c for w in ["error", "bug", "falla", "no funciona"]):
                    task_type = TaskType.CODE_DEBUGGING.value
                elif any(w in c for w in ["explica", "qué hace", "cómo funciona"]):
                    task_type = TaskType.CODE_EXPLANATION.value
                elif any(w in c for w in ["revisa", "review", "mejora"]):
                    task_type = TaskType.CODE_REVIEW.value
                elif any(w in c for w in ["optimiza", "mejora rendimiento"]):
                    task_type = TaskType.CODE_OPTIMIZATION.value
                else:
                    task_type = TaskType.CODE_GENERATION.value

            # ── Incorporar clarificaciones ───────────────────────────────────
            if clarification_answers:
                answers = "\n".join(f"- {v}" for v in clarification_answers.values())
                input_msg.content += f"\nClarificaciones del usuario:\n{answers}"

            # ── Detección de request complejo (solo intento 1) ───────────────
            if attempt == 1 and task_type == TaskType.CODE_GENERATION.value and not clarification_answers:
                is_complex, clarification = is_complex_request(input_msg.content)
                if is_complex:
                    logger.info(f"Complex request: {input_msg.content[:50]}...")
                    return AgentResult(
                        success=True, content=clarification, agent_type=self.AGENT_TYPE,
                        model_used=self.config.model, task_type=task_type,
                        latency_ms=(time.time() - start_time) * 1000, confidence=1.0,
                        metadata={
                            "needs_clarification": True, "reason": "complex_request",
                            "original_query": input_msg.content, "has_code": False,
                        },
                    )

            self._start_execution(task_type=task_type, input_summary=input_msg.content[:200])

            # ── Reescritura por ambigüedad ───────────────────────────────────
            rewritten_query    = None
            rewrite_duration_ms = 0.0
            if AMBIGUITY_DETECTOR_AVAILABLE and self.ambiguity_detector and ambiguity_level in ["high", "medium"]:
                try:
                    t0 = time.time()
                    rewritten_query = self.ambiguity_detector.get_rewrite_template(
                        ambiguity_level, input_msg.content
                    )
                    rewrite_duration_ms = (time.time() - t0) * 1000
                    log_event("AMBIGUITY_REWRITE", f"Query reescrito: {ambiguity_level}", extra={
                        "ambiguity_level": ambiguity_level,
                        "original_preview": input_msg.content[:100],
                        "rewritten_preview": rewritten_query[:100],
                        "rewrite_duration_ms": round(rewrite_duration_ms, 2),
                    })
                except Exception as e:
                    logger.warning(f"Ambiguity rewrite failed: {e}")

            # ── Construcción de prompt ───────────────────────────────────────
            prompt = self._build_prompt(
                input_msg, task_type, previous_critique, attempt,
                ambiguity_level, rewritten_query, search_context=search_context,
                session_id=session_id,  # ✅ plan_memory (B)
                user_id=user_id,  # ✅ UI de usuarios
            )
            estimated_level = min(4, max(1, len(input_msg.content.split()) // 15))
            temperature = self._get_temperature_for_level(estimated_level)
            # ✅ v0.6.9f: en el reintento (attempt>1) se usaba el timeout del
            # optimizer (90 s) — en CPU una generación de código tarda
            # 60-170 s, así que TODA retry moría por ReadTimeout (log 14:47:
            # eventos pub/sub: 1er intento 168 s con SyntaxError → retry 90 s
            # → "Failed after 2 attempts: timed out" → request fallida 0 chars).
            # Ahora los reintentos usan el mismo timeout amplio del código.
            timeout     = settings.ollama_timeout_code if attempt > 1 else None

            history = self._get_context(
                limit=6,
                task_type=task_type,
                query=input_msg.content,
                max_tokens=self._get_max_tokens(task_type),
                session_id=session_id,  # ✅ plan_memory (B)
                user_id=user_id,  # ✅ UI de usuarios
            )
            system_prompt = self._build_coder_system_prompt(
                task_type=task_type, history=history, search_context=search_context,
                # v0.6.9a-F: modo "revisión" también con crítica externa sembrada
                # en attempt=1 (refinamiento /plan / repair Fase 4).
                is_retry=previous_critique is not None,
                quality_score=0.45 if previous_critique is not None else 0.0,
                quality_critique=previous_critique,
            )

            # ── Generación: chunked vs normal ────────────────────────────────
            needs_chunked = (
                task_type == TaskType.CODE_GENERATION.value
                and attempt == 1
                and estimate_needs_chunking(input_msg.content, settings)
            )

            if needs_chunked:
                logger.info("🔄 Chunked generation activado")
                raw_code, chunk_count, chunked_was_truncated = self._generate_chunked(
                    prompt=prompt, system_prompt=system_prompt,
                    temperature=temperature,
                    timeout=settings.get_timeout_for_code_level(4),
                )
                response_content = f"```python\n{raw_code}\n```"
                log_event("CHUNKED_GENERATION", "Generación chunked completada", extra={
                    "chunk_count": chunk_count,
                    "total_lines": len(raw_code.splitlines()),
                })
            else:
                chunk_count = 0
                chunked_was_truncated = False
                response_content = self._generate(
                    prompt=prompt, system=system_prompt,
                    max_tokens=self._get_max_tokens(task_type),
                    timeout=timeout, temperature=temperature,
                )

            # ✅ Fase 6: needs_chunked ya maneja su propio truncamiento
            # chunk a chunk (arriba, _generate_chunked). Acá es la generación
            # de una sola llamada — si esa única llamada se truncó, el
            # response_content completo es código a medio terminar por
            # definición, sin importar qué tan bien parezca el resto.
            truncation_critique = None
            was_truncated       = chunked_was_truncated
            if not needs_chunked:
                last_llm = getattr(self, "_last_llm_response", None)
                if last_llm is not None and getattr(last_llm, "was_truncated", False):
                    was_truncated = True
                    truncation_critique = (
                        "La respuesta anterior se cortó por el límite de tokens antes de "
                        "terminar el código. Generá una versión más corta y completa — "
                        "priorizá terminar el bloque de código sobre las explicaciones."
                    )
                    logger.warning("⚠️ Generación truncada por max_tokens (done_reason=length)")

            # ── Extracción y estadísticas ────────────────────────────────────
            code_blocks  = extract_code(response_content)
            total_lines  = sum(len(b["code"].splitlines()) for b in code_blocks)
            self._code_stats["lines_generated"] += total_lines
            self._optimizer_stats["total_iterations"] += 1
            if previous_critique:
                self._optimizer_stats["improvements_applied"] += 1

            # ── ✅ ISSUE-142: NEGATIVA del modelo (censura) ──────────────────
            # Medido: el modelo censurado contesta "Lo siento, pero no puedo
            # ayudarte con eso." en 2-5 s para pedidos legítimos de laboratorio.
            # Sin esto, esa prosa entraba al análisis de calidad como "código sin
            # sintaxis" ⇒ el agente gastaba un **retry completo (70-90 s)**
            # pidiéndole al mismo modelo que corrija un "error de sintaxis" que en
            # realidad es una negativa (y se negaba otra vez).
            # Acá sólo se DETECTA y se informa: qué hacer con la negativa (avisar,
            # cambiar de modelo, Propuesta 4-B) lo decide el despachador.
            if not code_blocks and task_type == TaskType.CODE_GENERATION.value:
                _refusal = detect_refusal(response_content)
                if _refusal:
                    logger.warning(
                        f"🚫 El modelo se NEGÓ a generar (frase: {_refusal!r}) — "
                        f"sin retry: no hay código que corregir (attempt {attempt})"
                    )
                    log_event("CODER", "code_refusal", extra={
                        "refusal": _refusal, "model": self.config.model,
                        "task_type": task_type, "attempt": attempt,
                        "chars": len(response_content or ""),
                    })
                    return AgentResult(
                        success=False, content="", agent_type=self.AGENT_TYPE,
                        model_used=self.config.model, task_type=task_type,
                        latency_ms=(time.time() - start_time) * 1000,
                        error=f"code_refusal: {_refusal}",
                        metadata={
                            "code_refusal": _refusal,
                            "refusal_raw": (response_content or "")[:400],
                            "attempt": attempt,
                            "has_code": False,
                        },
                    )

            # ── Verificación blacklist (attempt > 1) ─────────────────────────
            if code_blocks and task_type == TaskType.CODE_GENERATION.value and attempt > 1:
                blacklisted = is_code_blacklisted(self._validation_blacklist, code_blocks[0]["code"])
                if blacklisted:
                    logger.warning(f"⚠️ Código blacklisted — forzando variación (attempt {attempt})")
                    blacklist_critique = (
                        f"⚠️ CÓDIGO REPETIDO DETECTADO. Ya fue rechazado por: "
                        f"{blacklisted.get('critique', 'razón desconocida')}. "
                        "Genera una solución COMPLETAMENTE DIFERENTE."
                    )
                    if attempt < self.config.max_retries:
                        return self.execute(
                            input_msg=input_msg, task_type=task_type,
                            previous_critique=blacklist_critique,
                            attempt=attempt + 1, ambiguity_level=ambiguity_level,
                            search_context=search_context,
                            session_id=session_id,  # ✅ v0.6.8u: no perder identidad en retries
                            user_id=user_id,        # ✅ v0.6.8u
                        )

            # ── Quality analysis ─────────────────────────────────────────────
            quality_analysis = None
            quality_critique = None
            should_retry     = False

            if truncation_critique and attempt < self.config.max_retries:
                should_retry = True

            if task_type == TaskType.CODE_GENERATION.value:
                quality_analysis = analyze_code_quality(response_content, settings)
                logger.info(
                    f"Code quality: {quality_analysis['quality_score']:.2f} "
                    f"(level {quality_analysis.get('code_level', 2)})"
                )
                log_event("CODE_QUALITY", "Quality analysis completed", extra={
                    "quality_score":          round(quality_analysis["quality_score"], 4),
                    "code_level":             quality_analysis.get("code_level", 2),
                    "has_syntax":             quality_analysis["has_syntax"],
                    "has_docstrings":         quality_analysis["has_docstrings"],
                    "has_type_hints":         quality_analysis.get("has_type_hints", False),
                    "has_tests":              quality_analysis.get("has_tests", False),
                    "tests_count":            quality_analysis.get("tests_count", 0),
                    "has_error_handling":     quality_analysis["has_error_handling"],
                    "line_count":             quality_analysis["line_count"],
                    "attempt":                attempt,
                    "extracted_code_length":  quality_analysis.get("extracted_code_length", 0),
                })

                # ✅ FIX AUDITORÍA: reportar el score real al PromptOptimizer.
                # Antes se calculaba quality_analysis["quality_score"] pero nunca
                # se llamaba record_quality() — PromptOptimizer.get_best_version()
                # (consultado en PromptManager.build()) nunca tenía historial real
                # para decidir nada, aunque toda la máquina de scoring/downgrade/
                # A-B testing estaba construida y funcionando.
                if self._prompt_manager is not None:
                    try:
                        self._prompt_manager.record_quality(
                            task_type=task_type,
                            score=quality_analysis["quality_score"],
                            was_retry=attempt > 1,
                        )
                    except Exception as e:
                        logger.debug(f"CoderAgent: record_quality falló (no crítico): {e}")

                # ✅ Fase 0.5 (H2): el gate ahora dispara también con SyntaxError
                # puro, aunque el score total llegue a 0.50+ (docstrings+
                # error_handling+tests+estructura solos pueden dar hasta 0.60
                # sin el punto de sintaxis). should_retry_for_quality() ya
                # tenía el check "if not has_syntax: return True" pero antes
                # vivía anidado bajo "quality_score < 0.50" y nunca se
                # evaluaba en ese caso — código con SyntaxError se entregaba
                # como éxito.
                if not quality_analysis["has_syntax"] or quality_analysis["quality_score"] < 0.50:
                    if quality_analysis["quality_score"] < 0.50:
                        logger.warning(f"⚠️ CALIDAD BAJA: {quality_analysis['quality_score']:.2f}")
                    if not quality_analysis["has_syntax"]:
                        logger.error(f"❌ SYNTAX ERROR: {quality_analysis.get('syntax_error', 'Unknown')}")

                    if attempt < self.config.max_retries and should_retry_for_quality(quality_analysis, settings):
                        should_retry     = True
                        quality_critique = build_quality_critique(quality_analysis)
                        logger.info(f"Reintentando generación (attempt {attempt + 1}/{self.config.max_retries})")

            # ── Contrato de código (Fase 2) ───────────────────────────────────
            # Corre aparte de quality_analysis: chequea secciones (código/
            # dependencias/tests/comando/salida esperada), no calidad del
            # código en sí. Se salta en needs_chunked — ese path arma
            # response_content como un único ```python sin secciones por
            # diseño (Fase 0 lo generaba así ya antes del contrato), y con
            # los require_* default=False (Fase 1) no hay retry infinito,
            # pero si a futuro se activa code_require_tests/etc. globalmente,
            # chunked generation nunca podría satisfacerlos — no es el
            # propósito de ese path.
            contract           = None
            contract_critique  = None
            if (
                task_type == TaskType.CODE_GENERATION.value
                and settings.code_contract_enabled
                and not needs_chunked
            ):
                contract = parse_code_contract(response_content, settings, query=input_msg.content)
                # ✅ v0.6.9a (E, flag code_require_tests_auto — OFF default):
                # exigir TESTS cuando la generación es código EJECUTABLE (no
                # explicación/revisión) aunque code_require_tests esté False.
                # Refuerza la capa dinámica real (pytest en sandbox).
                if (
                    getattr(settings, "code_require_tests_auto", False)
                    and not contract.test_files
                    and "tests" not in contract.missing
                ):
                    _q = (input_msg.content or "").lower()
                    _gen = any(v in _q for v in (
                        "generá", "genera", "escribí", "escribe", "crea", "creá",
                        "implementa", "hacé", "programa", "script", "función",
                        "funcion", "clase",
                    ))
                    _expl = any(v in _q for v in (
                        "expli", "resum", "revis", "qué hace", "que hace",
                        "cómo funciona", "como funciona", "para qué sirve",
                    ))
                    if _gen and not _expl:
                        contract.missing.append("tests")
                        contract.complete = False
                        logger.info(
                            "[v0.6.9a-E] auto-requisito: faltan TESTS en "
                            "generación ejecutable → retry"
                        )
                if contract.missing:
                    logger.info(f"Contrato incompleto — faltan secciones: {contract.missing}")
                    if attempt < self.config.max_retries:
                        should_retry      = True
                        contract_critique = build_contract_critique(contract)

            latency_ms = (time.time() - start_time) * 1000
            self._end_execution(success=True, output_summary=response_content[:200])
            self._reset_failure_count()

            metadata = {
                "code_blocks":               len(code_blocks),
                "total_lines":               total_lines,
                "language":                  self.language,
                "max_tokens":                self.config.max_tokens,
                "attempt":                   attempt,
                "previous_critique_applied": previous_critique is not None and attempt > 1,
                "extracted_code":            code_blocks,
                "has_code":                  len(code_blocks) > 0,
                "suggested_filename":        suggest_filename(input_msg.content, code_blocks),
                "extracted_filename":        extract_filename(response_content),
                "ambiguity_level":           ambiguity_level,
                "show_warning":              show_warning,
                "warning_message":           warning_message,
                "rewrite_duration_ms":       rewrite_duration_ms,
                # ✅ Fase 7: para trazar en TraceStore (dispatcher.py lee esto)
                "was_truncated":             was_truncated,
                "consecutive_failures":      self._consecutive_failures,
                "failure_threshold":         self._failure_threshold,
                "response_length":           len(response_content) if response_content else 0,
                "temperature_used":          temperature,
                "prompt_manager_used":       self._prompt_manager is not None,
                "chunked_generation":        needs_chunked,
                "chunk_count":               chunk_count,
                "blacklist_active":          self._validation_blacklist is not None,
            }

            if quality_analysis:
                metadata["quality_analysis"] = quality_analysis
                metadata["quality_critique"]  = quality_critique
                metadata["should_retry"]      = should_retry

            if contract is not None:
                metadata["code_artifact"] = contract.to_dict()

            # ── Reintento con blacklist (solo cuando el motivo es calidad real —
            # un contrato incompleto con código bueno no debe blacklistearse) ──
            if should_retry and quality_critique:
                if code_blocks:
                    blacklist_code(
                        self._validation_blacklist,
                        code_blocks[0]["code"],
                        quality_critique,
                        quality_analysis["quality_score"],
                    )

            if should_retry:
                retry_critique = " ".join(
                    c for c in (truncation_critique, quality_critique, contract_critique) if c
                )
                return self.execute(
                    input_msg=input_msg, task_type=task_type,
                    previous_critique=retry_critique,
                    attempt=attempt + 1, ambiguity_level=ambiguity_level,
                    search_context=search_context,
                    session_id=session_id,  # ✅ v0.6.8u: no perder identidad en retries
                    user_id=user_id,        # ✅ v0.6.8u
                )

            # ✅ Fase 0.5 (H4) + Fase 2: validation_passed se propaga 1:1 a
            # Response vía AgentResult.to_response() pero nunca se pasaba
            # acá — quedaba en False (default) incluso en generación
            # "exitosa". Ahora exige también contrato completo cuando el
            # contrato está activo (contract is None cubre CODE_EXPLANATION/
            # CODE_REVIEW/etc., donde no corre parse_code_contract()).
            validation_passed = bool(
                quality_analysis
                and quality_analysis.get("has_syntax")
                and quality_analysis.get("quality_score", 0.0) >= 0.50
                and (contract is None or contract.complete)
            )

            return AgentResult(
                success=True, content=response_content, agent_type=self.AGENT_TYPE,
                model_used=self.config.model, task_type=task_type,
                latency_ms=latency_ms, confidence=0.85, metadata=metadata,
                validation_passed=validation_passed,
            )

        # ═══════════════════════════════════════════════════════════════
        # ✅ MANEJO EXPLÍCITO DE EARLY EXIT CWE
        # ═══════════════════════════════════════════════════════════════
        except CriticalSecurityFlawDetected as e:
            logger.warning(f"🚨 CRITICAL CWE FLAW DETECTED. Forzando reintento con crítica específica.")
            cwe_critique = (
                f"⚠️ FALLO DE SEGURIDAD CRÍTICO DETECTADO EN TU CÓDIGO: {e.args[0]}\n"
                "REGLA ESTRICTA: NUNCA uses eval(), exec(), os.system(), subprocess.call() sin shell=False, ni pickle.loads().\n"
                "DEBES generar una solución completamente segura y alternativa."
            )
            if attempt < self.config.max_retries:
                return self.execute(
                    input_msg=input_msg, task_type=task_type,
                    previous_critique=cwe_critique,
                    attempt=attempt + 1, ambiguity_level=ambiguity_level,
                    search_context=search_context,
                    session_id=session_id,  # ✅ v0.6.8u: no perder identidad en retries
                    user_id=user_id,        # ✅ v0.6.8u
                )
            else:
                return AgentResult(
                    success=False, content="", agent_type=self.AGENT_TYPE,
                    model_used=self.config.model, task_type=task_type or "unknown",
                    latency_ms=(time.time() - start_time) * 1000,
                    error=f"Critical Security Flaw not resolved after retries: {e.args[0]}",
                    metadata={"attempt": attempt, "cwe_flaw": e.args[0]}
                )
        except Exception as e:
            # ✅ REFACT DRY: Consolidación de manejo de errores
            return self._handle_execution_error(
                e, input_msg, task_type, attempt, previous_critique, ambiguity_level, start_time
            )

    # ═══════════════════════════════════════════════════
    # MANEJO DE ERRORES CONSOLIDADO (DRY)
    # ═══════════════════════════════════════════════════
    def _handle_execution_error(
        self,
        error: Exception,
        input_msg: Message,
        task_type: Optional[str],
        attempt: int,
        previous_critique: Optional[str],
        ambiguity_level: str,
        start_time: float
    ) -> AgentResult:
        """Manejo centralizado de excepciones de execute() para evitar duplicación."""
        log_error(error, "execute_error")
        self._end_execution(success=False, error=str(error))
        fallback_needed = self._increment_failure_count(input_msg.content)

        is_llm_error = isinstance(error, LLMError)
        error_msg = str(error) if is_llm_error else f"Unexpected: {error}"

        return AgentResult(
            success=False, content="", agent_type=self.AGENT_TYPE,
            model_used=self.config.model, task_type=task_type or "unknown",
            latency_ms=(time.time() - start_time) * 1000, error=error_msg,
            metadata={
                "attempt": attempt,
                "previous_critique_applied": previous_critique is not None and attempt > 1,
                "ambiguity_level": ambiguity_level,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self._failure_threshold,
                "fallback_needed": fallback_needed,
                "prompt_manager_used": self._prompt_manager is not None,
                "blacklist_active": self._validation_blacklist is not None,
            },
        )

    # ═══════════════════════════════════════════════════
    # MÉTODOS DE CONVENIENCIA
    # ═══════════════════════════════════════════════════
    def generate_code(self, description: str, language: Optional[str] = None) -> AgentResult:
        """Genera código a partir de una descripción."""
        prompt = f"Genera código para: {description}"
        if language:
            prompt += f"\nLenguaje: {language}"

        old_lang = self.language
        try:
            # ✅ FIX: Protección de estado. Si execute falla, el lenguaje se restaura.
            self.language = language or self.language
            result = self.execute(Message(content=prompt, role="user"))
            return result
        finally:
            self.language = old_lang

    def debug_code(self, code: str, error_message: str = "") -> AgentResult:
        """Analiza y corrige código con error."""
        prompt = f"Analiza y corrige:\n```python\n{code}\n```"
        if error_message:
            prompt += f"\nError: {error_message}"
        return self.execute(
            Message(content=prompt, role="user"),
            task_type=TaskType.CODE_DEBUGGING.value,
        )

    def explain_code(self, code: str, language: Optional[str] = None) -> AgentResult:
        """Explica código de forma clara."""
        return self.execute(
            Message(content=f"Explica:\n```{language or 'code'}\n{code}\n```", role="user"),
            task_type=TaskType.CODE_EXPLANATION.value,
        )

    def get_code_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas combinadas del agente de código."""
        return {
            **self.get_stats(),
            "code_stats":           self._code_stats.copy(),
            "language":             self.language,
            "optimizer_stats":      self._optimizer_stats.copy(),
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold":    self._failure_threshold,
            "prompt_manager_active": self._prompt_manager is not None,
            "blacklist_active":     self._validation_blacklist is not None,
        }
