#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — classifier_keyword_rules.py  (REFACTORIZACIÓN POR PARTES)
# Ruta: src/router/classifier_keyword_rules.py
#
# ✅ 2026-10-13 (pieza 4, R14 exceptuado el 2026-10-12 / decisión C4 reabierta):
#   Clasificación POR PALABRAS CLAVE extraída de `src/router/classifier_core.py`, sin cambios de
#   comportamiento. Mismo patrón que `classifier_embedding.py`.
#
#   Qué se movió desde classifier_core.py:
#     - `_keyword_match`, `_set_keyword_count`, `_set_keyword_any`  → helpers (eran @staticmethod)
#     - `_is_greeting_or_basic_chat`  → saludo / chat básico
#     - `_classify_by_keywords`       → el motor de reglas (122 líneas: el bloque más grande)
#
#   `IntentClassifierCore` hereda ahora `ClassifierKeywordRulesMixin`, así la API pública
#   (self._classify_by_keywords, …) NO cambia.
#
#   Dos detalles de la extracción, para que no sorprendan:
#     · `_WORD_BOUNDARY_KEYWORDS` se importa del módulo de DATOS `classifier_keywords.py`, que es
#       exactamente de dónde sale el atributo de clase: es el mismo objeto, sin ciclo de import.
#     · `_log_router` vive en classifier_core y se importa DIFERIDO dentro del método que lo usa
#       (igual que documenta `classifier_embedding.py`).
#   El estado y los helpers que usa (`self._preprocess_text`, `self.KEYWORD_MAP`, `self._stats`, …)
#   los aporta `IntentClassifierCore`: este mixin sólo los usa.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from typing import Optional, Set, Tuple

from src.domain.types import TaskType
from src.utils.degradation import log_degraded
from src.router.classifier_keywords import (
    _WORD_BOUNDARY_KEYWORDS, CODE_CONTEXT_KEYWORDS, CODE_EXPLANATION_KEYWORDS, CODE_VERBS,
    GREETING_PATTERNS, LOCATION_PATTERNS, QUALITY_VALIDATION_KEYWORDS, VISION_SCREEN_KEYWORDS,
)

logger = logging.getLogger(__name__)


class ClassifierKeywordRulesMixin:
    """Clasificación por reglas de palabras clave.

    Extraído de IntentClassifierCore (pieza 4). El motor de reglas mira el texto ya preprocesado y
    devuelve (intención, confianza) o None si ninguna regla aplica.
    """

    @staticmethod
    def _keyword_match(keyword: str, text: str) -> bool:
        if ' ' in keyword:
            return keyword in text
        if keyword in _WORD_BOUNDARY_KEYWORDS:
            return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))
        return keyword in text

    @staticmethod
    def _set_keyword_count(keyword_set: Set[str], text: str) -> int:
        return sum(1 for kw in keyword_set if ClassifierKeywordRulesMixin._keyword_match(kw, text))

    @staticmethod
    def _set_keyword_any(keyword_set: Set[str], text: str) -> bool:
        return any(ClassifierKeywordRulesMixin._keyword_match(kw, text) for kw in keyword_set)

    def _is_greeting_or_basic_chat(self, processed_text: str) -> bool:
        # ✅ FIX: el facade (classifier.py PASO "greeting_fast_path") llama
        # a este método con el texto crudo, no con _preprocess_text() aplicado.
        # Normalizamos acá para no depender de que el caller lo haga bien.
        text_lower = self._preprocess_text(processed_text)
        if len(text_lower) < 20:
            if any(g in text_lower for g in ["hola", "hello", "hi", "buenos", "buenas"]):
                return True
        for pattern in GREETING_PATTERNS:
            if re.match(pattern, text_lower, re.I):
                return True
        if re.search(r'qué puedes hacer|que puedes hacer|en qué me ayudas|en que me ayudas|para qué sirves|puedes ayudarme', text_lower):
            return not self._set_keyword_any(CODE_CONTEXT_KEYWORDS, text_lower)
        if re.search(r'(puedes|puedo|me puedes|ayuda|ayudame|ayudarme|necesito|quiero).*código', text_lower):
            has_specific_verb = any(v in text_lower for v in [
                "genera", "crea", "escribe", "implementa", "def ", "class ",
                "función", "clase", "script", "programa"
            ])
            if not has_specific_verb:
                return True
        return False

    def _classify_by_keywords(self, processed_text: str) -> Tuple[Optional[str], float]:
        from src.router.classifier_core import _has_leading_negation, _log_router  # noqa: F401
        start = time.perf_counter()
        # ✅ FIX: el facade (classifier.py PASO 3 "keywords_fallback") llama
        # a este método con el texto crudo — nunca pasa por _preprocess_text().
        # LOCATION_PATTERNS no tiene re.IGNORECASE, así que "¿Dónde estamos?"
        # (con mayúscula) no matcheaba nunca y cursaba silenciosamente a
        # keywords_miss. Confirmado con re.search directo: falla en crudo,
        # matchea en minúsculas. Normalizamos acá, en el límite de la función,
        # para no depender de que cada caller futuro lo haga bien.
        processed_text = self._preprocess_text(processed_text)
        if self._is_greeting_or_basic_chat(processed_text):
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.GENERAL_CHAT.value, "keywords_greeting", 0.9, duration=duration)
            return TaskType.GENERAL_CHAT.value, 0.9
        # ✅ v0.6.9f: el compound code_verb+context va PRIMERO (antes de
        # explanation y quality) — una orden de GENERAR código ("Generá una
        # función que calcule el precio…", "Generá tests… que deba validar…")
        # no debe perder contra un FP de explanation/quality. "Explicá qué
        # hace…" no tiene verbo de CODE_VERBS → sigue por explanation.
        code_verb_matches = self._set_keyword_count(CODE_VERBS, processed_text)
        code_context_matches = self._set_keyword_count(CODE_CONTEXT_KEYWORDS, processed_text)
        if code_verb_matches > 0 and code_context_matches >= 1:
            # ✅ v4.7.0: guard de negación — "no generes código" no es CODE_GENERATION.
            if not _has_leading_negation(processed_text):
                duration = time.perf_counter() - start
                _log_router(processed_text[:50], TaskType.CODE_GENERATION.value, "keywords", 0.85, duration=duration)
                return TaskType.CODE_GENERATION.value, 0.85
        code_explanation_matches = self._set_keyword_count(CODE_EXPLANATION_KEYWORDS, processed_text)
        has_code_context = self._set_keyword_any(CODE_CONTEXT_KEYWORDS, processed_text)
        if code_explanation_matches > 0 and has_code_context:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.CODE_EXPLANATION.value, "keywords", 0.80, duration=duration)
            return TaskType.CODE_EXPLANATION.value, 0.80
        quality_validation_matches = self._set_keyword_count(QUALITY_VALIDATION_KEYWORDS, processed_text)
        if quality_validation_matches > 0:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.QUALITY_VALIDATION.value, "keywords", 0.80, duration=duration)
            return TaskType.QUALITY_VALIDATION.value, 0.80
        has_analiza = any(kw in processed_text for kw in ["analiza", "analizá", "analizar", "análisis"])
        has_datos = "datos" in processed_text or "data" in processed_text
        if has_analiza and has_datos:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.DATA_ANALYSIS.value, "keywords", 0.70, duration=duration)
            return TaskType.DATA_ANALYSIS.value, 0.70
        if self._set_keyword_any(VISION_SCREEN_KEYWORDS, processed_text):
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.SCREENSHOT_ANALYSIS.value, "keywords_vision", 0.90, duration=duration)
            return TaskType.SCREENSHOT_ANALYSIS.value, 0.90
        for pattern in LOCATION_PATTERNS:
            if re.search(pattern, processed_text):
                duration = time.perf_counter() - start
                _log_router(processed_text[:50], TaskType.LOCATION_QUERY.value, "keywords_location", 0.90, duration=duration)
                return TaskType.LOCATION_QUERY.value, 0.90
        # ✅ v0.7.5 (ISSUE-073): consulta de DATO verificable → WEB_SEARCH.
        # Caso real (log 12-09): "¿Dónde se ubica la falla geológica en el valle
        # de Calamuchita…?" → keywords_miss → general_chat → 0 tools, 0 fuentes,
        # 208 s. Se reusa `expects_factual_grounding()` (una sola fuente de verdad
        # con el chat) en vez de duplicar la lista de marcadores, y se exige que
        # NO sea una consulta de código/proyecto (esas no necesitan web).
        # ⚠️ `has_code_context` NO alcanza como guarda: usa substring matching y
        # "int" matchea dentro de "**int**endente" (medido: la consulta real
        # "…candidatos para intendente en villa general belgrano" quedaba marcada
        # como contexto de código). Se exige señal REAL de código: verbo de código
        # + contexto, o sintaxis explícita.
        _sintaxis_codigo = bool(re.search(
            r"(```|\bdef\s+\w|\bclass\s+\w|\bimport\s+\w|\breturn\b)",
            processed_text))
        _es_pedido_de_codigo = (
            (code_verb_matches > 0 and code_context_matches >= 1) or _sintaxis_codigo)
        if not _es_pedido_de_codigo:
            try:
                from src.core.dispatcher_handlers import expects_factual_grounding
                if expects_factual_grounding(processed_text):
                    duration = time.perf_counter() - start
                    _log_router(processed_text[:50], TaskType.WEB_SEARCH.value,
                                "keywords_factual", 0.6, duration=duration)
                    return TaskType.WEB_SEARCH.value, 0.6
            except Exception as e:
                # AUD-SIL: si el hook no se puede importar, se sigue el ruteo
                # normal (no se inventa una ruta) pero queda registrado.
                log_degraded(logger, e, "src/router/classifier_core.py:factual_grounding")
        # ✅ v0.6.8pp (logs/reg_error.txt 15:03): "escribí en una línea:
        # \"prueba de historial netron-123\"" → code_testing porque "prueba"
        # aparecía en el TEXTO CITADO (contenido a escribir, no una orden de
        # testear). Los spans entre comillas suelen ser contenido del usuario
        # (strings, nombres de archivo, citas) — no los usamos para decidir
        # intención por keyword. Las órdenes fuera de comillas siguen matcheando.
        _text_no_quotes = re.sub(
            r"[\"'“”‘’]([^\"'“”‘’]{0,120})[\"'“”‘’]", " ", processed_text
        )
        matches = [
            self.KEYWORD_MAP[kw]
            for kw in self.KEYWORD_MAP
            if ClassifierKeywordRulesMixin._keyword_match(kw, _text_no_quotes)
        ]
        if matches:
            most_common = Counter(matches).most_common(1)[0][0]
            duration = time.perf_counter() - start
            # ✅ v4.7.0: guard de negación — bloquea tareas de código si hay negación.
            _CODE_TASK_TYPES = {
                TaskType.CODE_GENERATION.value, TaskType.CODE_DEBUGGING.value,
                TaskType.CODE_EXPLANATION.value, TaskType.CODE_OPTIMIZATION.value,
                TaskType.CODE_CORRECTION.value, TaskType.CODE_REVIEW.value,
            }
            if most_common in _CODE_TASK_TYPES and _has_leading_negation(processed_text):
                pass  # cae a embeddings → PASO 4 LLM
            else:
                _log_router(processed_text[:50], most_common, "keywords", 0.6, duration=duration)
                return most_common, 0.6
        if "noticias" in processed_text or "news" in processed_text:
            duration = time.perf_counter() - start
            _log_router(processed_text[:50], TaskType.NEWS_SEARCH.value, "keywords", 0.6, duration=duration)
            return TaskType.NEWS_SEARCH.value, 0.6
        if not self._set_keyword_any(CODE_CONTEXT_KEYWORDS, processed_text):
            for kw in self.FINANCE_KEYWORDS:
                if ClassifierKeywordRulesMixin._keyword_match(kw, processed_text):
                    duration = time.perf_counter() - start
                    _log_router(processed_text[:50], TaskType.FINANCE_QUERY.value, "keywords", 0.6, duration=duration)
                    return TaskType.FINANCE_QUERY.value, 0.6
        duration = time.perf_counter() - start
        _log_router(processed_text[:50], "none", "keywords_miss", 0.0, duration=duration)
        return None, 0.0
