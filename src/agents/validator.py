#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v4.2.1 — Agente Validador (ValidatorAgent) [C3]
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/agents/validator.py
# Autor: SaintWick
# Fecha: 2026-04-18
# ✅ [C3]: _security_audit_defensive() CWE Top-25 + hook pre-LLM en execute()
# ✅ v4.2.0: ValidationScore movido a src/domain/entities.py, instanciado en execute()
# ✅ v4.2.1: Pre-compilación de _UNSAFE_PATTERNS a nivel de clase (evita re.compile en runtime)
# Propósito: Validación con Chain of Thought forzado (ForcedCoT) + Score estructurado para Evaluator-Optimizer
# ═══════════════════════════════════════════════════════════════
import logging
import time
import re
from typing import ClassVar, Optional, Dict, Any, List
from config.settings import settings
from src.domain.types import TaskType
from src.domain.entities import Message, ValidationScore
from src.domain.exceptions import AgentError, LLMError, ValidationError
from .base import BaseAgent, AgentResult
from src.utils.degradation import log_degraded
# ============================================================================
# INTEGRACIÓN DETAILEDLOGGER — IMPORT SEGURO
# ============================================================================
try:
    from tools.detailed_logger import get_logger
    LOGGER_AVAILABLE = True
except ImportError:
    LOGGER_AVAILABLE = False

logger = logging.getLogger(__name__)

def _log_agent(event: str, task_type: str = "", duration: float = None,
               success: bool = True, error: str = None):
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_agent_execution(
                agent_name="validator", task_type=task_type, state=event,
                duration=duration, success=success, error=error
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/agents/validator.py:_log_agent")

def _log_event(event: str, level: str = "INFO", extra: dict = None):
    """Log evento de validator en DetailedLogger."""
    if not LOGGER_AVAILABLE:
        return
    try:
        log = get_logger()
        if hasattr(log, "is_initialized") and log.is_initialized:
            log.log_event("VALIDATOR", event, level=level, extra=extra or {})
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/agents/validator.py:_log_event")

def _log_error(error: Exception, context: str, extra_info: dict = None):
    if not LOGGER_AVAILABLE:
        logger.error(f"validator - {context}: {error}")
        return
    try:
        log = get_logger()
        if hasattr(log, 'is_initialized') and log.is_initialized:
            log.log_error(error=error, context=context, extra_info=extra_info,
                          function_name=f"validator.{context}")
    except Exception:
        logger.error(f"validator - {context}: {error}")


# ═══════════════════════════════════════════════════════════════
# ✅ v4.2.0: ValidationScore ELIMINADO de este archivo
#    Ahora se importa desde src/domain/entities.py (capa de dominio).
#    La clase es instanciada en execute() como contrato estructurado
#    para el ciclo Evaluator-Optimizer.
#    Pasa de [⚠️ AISLADO] a [✅ CONECTADO].
# ═══════════════════════════════════════════════════════════════


class ValidatorAgent(BaseAgent):
    """
    Agente de validación con Forced Chain of Thought (5 secciones).
    Implementa auditoría defensiva pre-LLM (CWE Top-25) y early-exit determinista.
    Genera scores estructurados (0.0-10.0) y críticas accionables inyectables
    en el bucle Evaluator-Optimizer del CoderAgent.

    ✅ v4.2.0: ValidationScore importado desde src/domain/entities.py.
    Instanciado en execute() como metadata["validation_score"],
    garantizando trazabilidad AST del contrato de validación.
    """
    AGENT_TYPE = "validator"

    # ✅ CORREGIDO: System prompt con instrucción de concisión + formato para score
    DEFAULT_SYSTEM_PROMPT = """Eres un validador crítico y estricto de respuestas de IA.
Tu trabajo es verificar meticulosamente que las respuestas sean:
1. Precisas y factualmente correctas (fechas, datos, números, nombres)
2. Completas (responden toda la pregunta)
3. Seguras (sin contenido dañino)
4. Bien estructuradas y coherentes
5. Sin alucinaciones, contradicciones o datos inventados

IMPORTANTE: Si detectas alucinaciones, contradicciones, fechas erróneas o datos inventados,
marca la respuesta con VEREDICTO: RECHAZADO de forma inmediata. Sé CONCISO y directo.
Máximo 3 párrafos. Evita explicaciones innecesariamente largas.

Usa Chain of Thought (CoT) obligatorio en 5 secciones:
1. Análisis de la pregunta
2. Verificación de hechos y fechas
3. Evaluación de completitud
4. Chequeo de seguridad y coherencia
5. Conclusión final

FORMATO DE SALIDA OBLIGATORIO:
Al final de tu respuesta, incluye:
SCORE: X.X (donde X.X es un número entre 0.0 y 10.0)
CRITIQUE: [Feedback específico y accionable en 1-2 líneas]
VEREDICTO: APROBADO o RECHAZADO"""

    COT_SECTIONS = [
        "análisis", "verificación", "evaluación", "chequeo", "conclusión"
    ]

    # ✅ NUEVO: Patrones para extraer score y critique de la respuesta
    # ✅ v0.6.8ss (B1): tolerancia a markdown/espacios — el LLM suele emitir
    # "**SCORE:** 10.0" / "**VEREDICTO:** APROBADO"; el regex estricto
    # "SCORE:\s*" fallaba → fallback 5.0 → FAIL fantasma de respuestas buenas
    # (log 15:24: critique "**SCORE**: 10.0" con score numérico 5.0).
    SCORE_PATTERN = re.compile(r'SCORE[^\d\n]{0,10}?(\d+(?:\.\d+)?)', re.IGNORECASE)
    CRITIQUE_PATTERN = re.compile(
        r'CRITIQUE[^\n]{0,10}?[:：][*_ ]*\s*(.+?)(?:\n(?=\*{0,2}(?:VEREDICTO|CRITIQUE)\b)|VEREDICTO|$)',
        re.IGNORECASE | re.DOTALL,
    )
    VERDICT_PATTERN = re.compile(r'VEREDICTO[^\n]{0,10}?(APROBADO|RECHAZADO)', re.IGNORECASE)

    # ✅ [C3]: CWE Top-25 patterns — compilados UNA vez a nivel de clase
    _CWE_PATTERNS: "ClassVar[List[re.Pattern]]"  # type: ignore[assignment]
    _CWE_PATTERNS = [
        re.compile(r'(?:subprocess|os\.system|eval|exec)\s*\(', re.IGNORECASE),      # CWE-78 OS injection
        re.compile(r'(?:SELECT|INSERT|DROP|DELETE).*(?:--|;|\')', re.IGNORECASE),     # CWE-89 SQL injection
        re.compile(r'\.\.[\\/]|%2e%2e', re.IGNORECASE),                             # CWE-22 path traversal
        re.compile(r'<script|javascript:|onerror\s*=', re.IGNORECASE),                 # CWE-79 XSS
        re.compile(r'(?:pickle\.loads|yaml\.load\b|marshal\.loads)', re.IGNORECASE), # CWE-502 deserialization
    ]

    # ✅ v4.2.1: Patrones inseguros pre-compilados a nivel de clase (evita re.compile en runtime)
    _UNSAFE_PATTERNS: "ClassVar[List[re.Pattern]]"  # type: ignore[assignment]
    _UNSAFE_PATTERNS = [
        re.compile(r"odio\s+\w+", re.IGNORECASE),
        re.compile(r"violencia", re.IGNORECASE),
        re.compile(r"daño\s+a\s+\w+", re.IGNORECASE),
        re.compile(r"ilegal", re.IGNORECASE),
        re.compile(r"peligroso", re.IGNORECASE),
        re.compile(r"armas", re.IGNORECASE),
    ]

    # ✅ PLAN v2.0 (Fase 3): Patrones de jailbreak/prompt-injection para el
    # gate anti-injection de input directo (_screen_input en dispatcher_process).
    # Deliberadamente NO reutiliza _UNSAFE_PATTERNS/_CWE_PATTERNS: esos
    # bloquearían pedidos legítimos con subprocess/eval/SQL o research sobre
    # "armas"/"violencia". Estos patrones se acotan SOLO a intentos de
    # reescribir el prompt del sistema / escalar privilegios del modelo.
    _INJECTION_PATTERNS: "ClassVar[List[re.Pattern]]"  # type: ignore[assignment]
    _INJECTION_PATTERNS = [
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
        re.compile(r"you\s+are\s+now", re.IGNORECASE),
        re.compile(r"system\s+prompt", re.IGNORECASE),
        re.compile(r"disregard\s+(the\s+)?(above|system)", re.IGNORECASE),
        re.compile(r"olvid[aá]\s+(todas\s+)?(las\s+)?instrucciones", re.IGNORECASE),
        re.compile(r"ahora\s+eres", re.IGNORECASE),
        re.compile(r"ignora\s+(las\s+)?(instrucciones|reglas)", re.IGNORECASE),
        re.compile(r"prompt\s+del\s+sistema", re.IGNORECASE),
        re.compile(r"desobedece\s+(las\s+)?(instrucciones|reglas)", re.IGNORECASE),
    ]

    @classmethod
    def match_injection(cls, text: str) -> Optional[re.Match]:
        """Devuelve el primer match de jailbreak en el texto, o None.

        ✅ PLAN v2.0 (Fase 3): usado por el gate _screen_input del dispatcher.
        """
        for pattern in cls._INJECTION_PATTERNS:
            m = pattern.search(text)
            if m:
                return m
        return None

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
        enable_memory: bool = False,
        enable_validation: bool = False,
        max_retries: int = 2,
        system_prompt: Optional[str] = None,
        require_cot: bool = True,
        min_cot_sections: int = 5,
        # ✅ CORREGIDO: Agregar parámetro max_tokens
        max_tokens: int = 512,
    ):
        """
        Inicializa agente validador.
        Args:
            model: Modelo LLM a usar
            temperature: Temperatura (default 0.1 para precisión)
            timeout: Timeout en segundos
            enable_memory: Habilitar memoria
            enable_validation: Habilitar validación
            max_retries: Máximo de reintentos
            system_prompt: System prompt personalizado
            require_cot: Requerir Chain of Thought
            min_cot_sections: Mínimo de secciones CoT
            max_tokens: Máximo de tokens para respuestas (default 512)
        """
        super().__init__(
            model=model or (getattr(settings, 'validator_model', None) or settings.llm_fallback_1 or settings.llm_default_model),  # v0.6.9h
            temperature=temperature,
            # ✅ FIX: sin resolver acá — None deja que BaseAgent/OllamaClientBase
            # calculen el timeout por scenario (STANDARD por default en Validator).
            timeout=timeout,
            enable_memory=enable_memory,
            enable_validation=enable_validation,
            max_retries=max_retries,
            system_prompt=system_prompt or self.DEFAULT_SYSTEM_PROMPT,
            # ✅ CORREGIDO: Pasar max_tokens a la configuración base
            max_tokens=max_tokens,
        )
        self.require_cot = require_cot
        self.min_cot_sections = min_cot_sections
        # ✅ NUEVO: Contador de validaciones para estadísticas
        self._validation_stats = {"total": 0, "approved": 0, "avg_score": 0.0}
        logger.info(f"ValidatorAgent initialized: model={self.config.model}, temp={temperature}, max_tokens={self.config.max_tokens}")
        _log_event(f"ValidatorAgent initialized: model={self.config.model}", extra={
            "model": self.config.model, "temperature": temperature,
            "require_cot": require_cot, "max_tokens": max_tokens
        })

    # ═══════════════════════════════════════════════════════════
    # ✅ MÉTODOS PARA EXTRAER SCORE Y CRITIQUE
    # ═══════════════════════════════════════════════════════════
    def _extract_score_from_response(self, content: str) -> float:
        """
        Extrae score numérico de la respuesta del LLM.
        Args:
            content: Contenido de la respuesta
        Returns:
            Score entre 0.0 y 10.0 (default 5.0 si no se encuentra)
        """
        match = self.SCORE_PATTERN.search(content)
        if match:
            try:
                score = float(match.group(1))
                return max(0.0, min(10.0, score))  # Clamp
            except ValueError as _d2e:
                logging.getLogger(__name__).debug(f"[D2 validator.py:254] excepción degradada (intencional): {_d2e}")
        # ✅ v0.6.8ss (B1): fallback simétrico al veredicto en vez del 5.0 duro.
        # El regex tolerante ya cubre "**SCORE:** 10.0"; si AÚN no hay número,
        # usar el veredicto como proxy conservador: APROBADO → 8.0 (sobre el
        # threshold típico 6.5-7.0), RECHAZADO → 4.0. Antes, un 5.0 fijo
        # tumbaba respuestas correctas cuyo score el LLM escribió en formato
        # markdown (log 15:24).
        return 8.0 if self._extract_verdict_from_response(content) else 4.0

    def _extract_critique_from_response(self, content: str) -> str:
        """
        Extrae critique accionable de la respuesta del LLM.
        Args:
            content: Contenido de la respuesta
        Returns:
            Critique limitado a 500 chars
        """
        match = self.CRITIQUE_PATTERN.search(content)
        if match:
            critique = match.group(1).strip()
            # ✅ Limitar a 500 chars para evitar context drift
            return critique[:500]
        # ✅ Fallback: extraer primera línea de conclusión
        lines = content.split('\n')
        for line in lines[-5:]:  # Últimas 5 líneas
            if line.strip() and len(line.strip()) > 10:
                return line.strip()[:500]
        return ""

    def _extract_verdict_from_response(self, content: str) -> bool:
        """
        Extrae veredicto (APROBADO/RECHAZADO) de la respuesta.
        Args:
            content: Contenido de la respuesta
        Returns:
            True si APROBADO, False si RECHAZADO
        """
        match = self.VERDICT_PATTERN.search(content)
        if match:
            return match.group(1).upper() == "APROBADO"
        # ✅ v0.6.8ss (B1): fallback por substring protegido contra negación —
        # "NO APROBADO" / "no fue aprobado" no deben contar como aprobación.
        upper = content.upper()
        if re.search(r"\bNO\b[^.\n]{0,24}APROBADO", upper):
            return False
        if "RECHAZADO" in upper and "APROBADO" not in upper:
            return False
        return "APROBADO" in upper

    def _check_cot_sections(self, content: str) -> Dict[str, Any]:
        """
        Verifica que el contenido tenga las secciones CoT requeridas.
        Args:
            content: Contenido a verificar
        Returns:
            Dict con resultado de verificación
        """
        content_lower = content.lower()
        found_sections = []
        for section in self.COT_SECTIONS:
            if section in content_lower:
                found_sections.append(section)
        return {
            "has_cot": len(found_sections) >= self.min_cot_sections,
            "sections_found": found_sections,
            "sections_count": len(found_sections),
            "required": self.min_cot_sections,
        }

    def _check_safety(self, content: str) -> Dict[str, Any]:
        """
        Verifica contenido seguro usando patrones pre-compilados.
        Args:
            content: Contenido a verificar
        Returns:
            Dict con resultado de seguridad
        """
        # ✅ v4.2.1: Uso de patrones pre-compilados desde la clase
        flags = []
        for pattern in self._UNSAFE_PATTERNS:
            if pattern.search(content):
                # Extraer el string regex del patrón compilado para el flag
                flags.append(pattern.pattern)
        return {
            "is_safe": len(flags) == 0,
            "flags": flags,
        }

    def _check_completeness(self, question: str, answer: str) -> Dict[str, Any]:
        """
        Verifica que la respuesta sea completa.
        Args:
            question: Pregunta original
            answer: Respuesta a verificar
        Returns:
            Dict con resultado de completitud
        """
        question_words = set(question.lower().split())
        answer_words = set(answer.lower().split())
        overlap = len(question_words & answer_words)
        coverage = overlap / max(len(question_words), 1)
        return {
            "is_complete": coverage > 0.3 or len(answer) > 100,
            "coverage": round(coverage, 2),
            "answer_length": len(answer),
        }

    def _build_validation_prompt(self, question: str, answer: str) -> str:
        """
        Construye prompt de validación.
        Args:
            question: Pregunta original
            answer: Respuesta a validar
        Returns:
            Prompt completo para validación
        """
        return f"""Valida la siguiente respuesta usando Chain of Thought.

Pregunta: {question[:500]}
Respuesta a validar:
{answer[:2000]}

---
Responde con estas 5 secciones obligatorias:
1. ANÁLISIS: ¿Qué pregunta el usuario?
2. VERIFICACIÓN: ¿Los hechos son correctos?
3. EVALUACIÓN: ¿La respuesta es completa?
4. CHEQUEO: ¿Hay contenido inseguro?
5. CONCLUSIÓN: ¿Aprobar o rechazar?

FORMATO DE SALIDA OBLIGATORIO:
SCORE: X.X (número entre 0.0 y 10.0)
CRITIQUE: [Feedback específico y accionable]
VEREDICTO: APROBADO o RECHAZADO

Sé CONCISO. Máximo 3 párrafos."""

    def _security_audit_defensive(self, content: str) -> Dict[str, Any]:
        """
        [C3] Auditoría de seguridad CWE Top-25.
        Detecta patrones peligrosos ANTES de enviar al LLM.
        Returns: {"blocked": True, "cwe": [...], "vectors": [...]} o {"blocked": False}
        """
        try:
            cwe_labels = [
                "CWE-78 (OS injection)",
                "CWE-89 (SQL injection)",
                "CWE-22 (path traversal)",
                "CWE-79 (XSS)",
                "CWE-502 (deserialization)",
            ]
            detected_cwe = []
            detected_vectors = []
            for pattern, label in zip(self._CWE_PATTERNS, cwe_labels):
                match = pattern.search(content)
                if match:
                    detected_cwe.append(label)
                    detected_vectors.append(match.group(0)[:50])
            if detected_cwe:
                return {"blocked": True, "cwe": detected_cwe, "vectors": detected_vectors}
            return {"blocked": False}
        except Exception as e:
            logger.warning(f"[Validator] _security_audit_defensive error: {e}")
            return {"blocked": False}

    def execute(self, input_msg: Message, content_to_validate: Optional[str] = None,
                previous_critique: Optional[str] = None, attempt: int = 1,
                task_type: Optional[str] = None) -> AgentResult:
        """
        Ejecuta validación.
        Args:
            input_msg: Mensaje de entrada
            content_to_validate: Contenido a validar (opcional)
            previous_critique: Critique de iteración anterior (para optimizer loop)
            attempt: Número de intento (para optimizer loop)
            task_type: Tipo de tarea (inyectado por Dispatcher)
        Returns:
            AgentResult con resultado de validación + ValidationScore en metadata
        """
        start_time = time.time()
        try:
            self._start_execution(
                task_type=task_type or TaskType.QUALITY_VALIDATION.value,
                input_summary=input_msg.content[:200],
            )

            # Si no hay contenido específico, validar el mensaje mismo
            content = content_to_validate or input_msg.content

            # ✅ NUEVO: Incluir previous_critique en prompt si es iteración > 1
            if previous_critique and attempt > 1:
                prompt = self._build_validation_prompt_with_critique(
                    input_msg.content, content, previous_critique
                )
            else:
                prompt = self._build_validation_prompt(input_msg.content, content)

            # [C3] Auditoría de seguridad pre-LLM
            audit = self._security_audit_defensive(content)
            if audit["blocked"]:
                latency_ms = (time.time() - start_time) * 1000
                logger.warning(f"[Validator] Contenido bloqueado por CWE: {audit['cwe']}")
                self._end_execution(success=False, error="CWE bloqueado")

                # ✅ v4.2.0: Instanciar ValidationScore del dominio
                vs_cwe = ValidationScore(
                    score=0.0,
                    critique=f"Contenido bloqueado: {', '.join(audit['cwe'])}",
                    passed=False,
                    details={"security_audit": audit, "early_exit": "cwe"},
                )

                return AgentResult(
                    success=False,
                    content="",
                    agent_type=self.AGENT_TYPE,
                    model_used=self.config.model,
                    task_type=TaskType.QUALITY_VALIDATION.value,
                    latency_ms=latency_ms,
                    error="Contenido bloqueado: CWE detectado",
                    validation_passed=False,
                    metadata={
                        "score": vs_cwe.score,
                        "critique": vs_cwe.critique,
                        "passed": vs_cwe.passed,
                        "attempt": attempt,
                        "validation_score": vs_cwe.to_dict(),
                        "security_audit": audit,
                        "early_exit": "cwe",
                    },
                )

            # ✅ EARLY-EXIT pre-LLM: checks locales deterministas
            _safety_pre = self._check_safety(content)
            _complete_pre = self._check_completeness(input_msg.content, content)
            _pre_fail_reason = None
            if not _safety_pre["is_safe"]:
                _pre_fail_reason = f"Contenido inseguro: {_safety_pre['flags']}"
            elif not _complete_pre["is_complete"] and _complete_pre["answer_length"] < 20:
                _pre_fail_reason = f"Respuesta demasiado corta ({_complete_pre['answer_length']} chars)"

            if _pre_fail_reason:
                latency_ms = (time.time() - start_time) * 1000
                logger.info(f"[Validator] Early-exit pre-LLM: {_pre_fail_reason}")
                self._end_execution(success=False, error=_pre_fail_reason)

                # ✅ v4.2.0: Instanciar ValidationScore del dominio
                vs_pre = ValidationScore(
                    score=0.0,
                    critique=_pre_fail_reason,
                    passed=False,
                    details={
                        "safety_check": _safety_pre,
                        "completeness_check": _complete_pre,
                        "early_exit": "pre_llm",
                    },
                )

                return AgentResult(
                    success=False,
                    content="",
                    agent_type=self.AGENT_TYPE,
                    model_used=self.config.model,
                    task_type=TaskType.QUALITY_VALIDATION.value,
                    latency_ms=latency_ms,
                    error=_pre_fail_reason,
                    validation_passed=False,
                    metadata={
                        "score": vs_pre.score,
                        "critique": vs_pre.critique,
                        "passed": vs_pre.passed,
                        "attempt": attempt,
                        "validation_score": vs_pre.to_dict(),
                        "safety_check": _safety_pre,
                        "completeness_check": _complete_pre,
                        "early_exit": "pre_llm",
                    },
                )

            # ✅ CORREGIDO: Pasar max_tokens explícitamente para asegurar límite
            response_content = self._generate(
                prompt=prompt,
                max_tokens=self.config.max_tokens,
            )

            # ✅ NUEVO: Extraer score y critique estructurados
            score = self._extract_score_from_response(response_content)
            critique = self._extract_critique_from_response(response_content)
            verdict_passed = self._extract_verdict_from_response(response_content)

            # Verificar CoT
            cot_check = self._check_cot_sections(response_content) if self.require_cot else {"has_cot": True}

            # ✅ Reutilizar checks pre-LLM ya calculados (evita doble cómputo)
            safety_check = _safety_pre
            completeness_check = _complete_pre

            # ✅ NUEVO: Determinar aprobación basada en score + veredicto + checks
            threshold = settings.get_quality_threshold("validation") if hasattr(settings, 'get_quality_threshold') else 7.0
            is_approved = (
                cot_check.get("has_cot", True) and
                safety_check.get("is_safe", True) and
                completeness_check.get("is_complete", True) and
                verdict_passed and
                score >= threshold
            )

            # ✅ NUEVO: Actualizar estadísticas
            self._validation_stats["total"] += 1
            if is_approved:
                self._validation_stats["approved"] += 1
            # Rolling average
            n = self._validation_stats["total"]
            self._validation_stats["avg_score"] = (
                (self._validation_stats["avg_score"] * (n - 1) + score) / n
            )

            latency_ms = (time.time() - start_time) * 1000
            self._end_execution(
                success=is_approved,
                output_summary=f"Approved: {is_approved}, Score: {score:.1f}",
            )

            # ✅ v4.2.0: Instanciar ValidationScore del dominio
            vs = ValidationScore(
                score=score,
                critique=critique,
                passed=is_approved,
                details={
                    "cot_check": cot_check,
                    "safety_check": safety_check,
                    "completeness_check": completeness_check,
                    "approved": is_approved,
                    "max_tokens": self.config.max_tokens,
                    "threshold": threshold,
                },
            )

            # ✅ NUEVO: Logging para monitoreo del optimizer
            logger.info(f"Validation attempt {attempt}: score={score:.1f}, passed={is_approved}, critique_len={len(critique)}")
            _log_event(f"Validation result: {'PASS' if is_approved else 'FAIL'} (score={score:.1f})",
                      level="INFO" if is_approved else "WARNING", extra={
                "score": score, "passed": is_approved, "attempt": attempt,
                "threshold": threshold, "critique_preview": critique[:100],
                "cot_ok": cot_check.get("has_cot", True),
                "safe": safety_check.get("is_safe", True),
                "complete": completeness_check.get("is_complete", True),
            })

            return AgentResult(
                success=is_approved,
                content=response_content,
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type=TaskType.QUALITY_VALIDATION.value,
                latency_ms=latency_ms,
                confidence=0.9 if is_approved else 0.3,
                validation_passed=is_approved,
                metadata={
                    # ✅ v4.2.0: ValidationScore estructurado del dominio
                    "validation_score": vs.to_dict(),
                    # Campos planos para backward compatibility
                    "score": vs.score,
                    "critique": vs.critique,
                    "passed": vs.passed,
                    "attempt": attempt,
                    # ✅ EXISTENTE: Checks detallados
                    "cot_check": cot_check,
                    "safety_check": safety_check,
                    "completeness_check": completeness_check,
                    "approved": is_approved,
                    "max_tokens": self.config.max_tokens,
                    "threshold": threshold,
                },
            )
        except LLMError as e:
            _log_error(e, "execute_llm")
            self._end_execution(success=False, error=str(e))

            # ✅ v4.2.0: Instanciar ValidationScore del dominio para error LLM
            vs_llm = ValidationScore(
                score=0.0,
                critique=f"Error: {str(e)}",
                passed=False,
                details={"error_type": "llm_error"},
            )

            return AgentResult(
                success=False,
                content="",
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type=TaskType.QUALITY_VALIDATION.value,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e),
                metadata={
                    "validation_score": vs_llm.to_dict(),
                    "score": vs_llm.score,
                    "critique": vs_llm.critique,
                    "passed": vs_llm.passed,
                    "attempt": attempt,
                },
            )
        except Exception as e:
            _log_error(e, "execute_unexpected")
            self._end_execution(success=False, error=str(e))

            # ✅ v4.2.0: Instanciar ValidationScore del dominio para error inesperado
            vs_err = ValidationScore(
                score=0.0,
                critique=f"Error: {str(e)}",
                passed=False,
                details={"error_type": "unexpected"},
            )

            return AgentResult(
                success=False,
                content="",
                agent_type=self.AGENT_TYPE,
                model_used=self.config.model,
                task_type=TaskType.QUALITY_VALIDATION.value,
                latency_ms=(time.time() - start_time) * 1000,
                error=f"Unexpected: {str(e)}",
                metadata={
                    "validation_score": vs_err.to_dict(),
                    "score": vs_err.score,
                    "critique": vs_err.critique,
                    "passed": vs_err.passed,
                    "attempt": attempt,
                },
            )

    # ═══════════════════════════════════════════════════════════
    # ✅ MÉTODO PARA VALIDACIÓN CON CRITIQUE PREVIO
    # ═══════════════════════════════════════════════════════════
    def _build_validation_prompt_with_critique(self, question: str, answer: str,
                                                previous_critique: str) -> str:
        """
        Construye prompt de validación incluyendo critique de iteración anterior.
        Args:
            question: Pregunta original
            answer: Respuesta a validar
            previous_critique: Critique de iteración anterior
        Returns:
            Prompt completo para validación
        """
        base_prompt = self._build_validation_prompt(question, answer)
        return f"""{base_prompt}

---
MEJORAS REQUERIDAS (intento anterior):
{previous_critique[:500]}

Verifica si las mejoras anteriores fueron aplicadas. Si no, inclúyelas en tu critique."""

    def validate_response(self, question: str, answer: str) -> Dict[str, Any]:
        """
        Valida una pregunta-respuesta específica.
        Args:
            question: Pregunta original
            answer: Respuesta a validar
        Returns:
            Dict con resultado de validación
        """
        result = self.execute(
            Message(content=question, role="user"),
            content_to_validate=answer,
        )
        return {
            "valid": result.success,
            "confidence": result.confidence,
            "metadata": result.metadata,
            "error": result.error,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """
        Retorna capacidades del validador.
        Returns:
            Dict con capacidades
        """
        return {
            **super().get_capabilities(),
            "require_cot": self.require_cot,
            "min_cot_sections": self.min_cot_sections,
            "cot_sections": self.COT_SECTIONS,
            "max_tokens": self.config.max_tokens,
            # ✅ NUEVO: Capacidades para optimizer
            "supports_score": True,
            "supports_critique": True,
            "score_range": (0.0, 10.0),
        }

    # ✅ NUEVO: Método para obtener estadísticas de validación
    def get_validation_stats(self) -> Dict[str, Any]:
        """
        Retorna estadísticas de validación para monitoreo.
        Returns:
            Dict con estadísticas
        """
        return {
            **self.get_stats(),
            "validation_stats": self._validation_stats.copy(),
        }


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS v4.2.1
# ═══════════════════════════════════════════════════════════════
# | Sección                  | Cambio Realizado                           | Impacto AST                |
# |--------------------------|--------------------------------------------|----------------------------|
# | ValidationScore local    | ❌ ELIMINADO: Definición local removida    | Ya no es entidad aislada   |
# | Import entities.py       | ✅ +ValidationScore en import desde dominio| [⚠️ AISLADO] → [✅ CONECTADO] |
# | execute() CWE exit       | ✅ Instancia ValidationScore               | Contrato estructurado      |
# | execute() pre-LLM exit   | ✅ Instancia ValidationScore               | Contrato estructurado      |
# | execute() main path      | ✅ Instancia ValidationScore               | Contrato estructurado      |
# | execute() LLMError       | ✅ Instancia ValidationScore               | Contrato estructurado      |
# | execute() Exception      | ✅ Instancia ValidationScore               | Contrato estructurado      |
# | metadata["validation_   | ✅ NUEVO: to_dict() del score estructurado | Evaluator-Optimizer puede  |
# |  score"]                 |                                            | consumir contrato de dominio|
# | metadata planos          | ✅ PRESERVADOS: score/critique/passed      | Backward compatibility     |
# | _UNSAFE_PATTERNS         | ✅ Compilados a nivel de clase             | Evita re.compile runtime   |
# | _check_safety()          | ✅ Uso de patrones pre-compilados          | O(1) lookup, baja CPU      |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — FUNCIONALIDAD PRESERVADA — SIN OMISIONES
# ═══════════════════════════════════════════════════════════════
