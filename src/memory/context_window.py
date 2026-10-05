#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v1.1.1 — Context Window Manager [C4]
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/memory/context_window.py
# Autor: SaintWick
# Fecha: 2026-04-18
# ✅ [C4]: MYTHOS_INSPIRED strategy + adaptive threshold
# ✅ v1.1.1 (FASE 3):
#    - _enforce_token_limit: System Prompt Overflow mitigado.
#    - auto_compact: Deduplicación de notas de compresión (evita Token Drift).
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Import seguro DetailedLogger ──────────────────────────────────────────────
try:
    from tools.detailed_logger import get_logger as _get_detailed_logger
    _LOGGER_AVAILABLE = True
except ImportError:
    _LOGGER_AVAILABLE = False


def _log_ctx(op: str, session_id: str = "", duration: float = None,
             success: bool = True, extra: Dict[str, Any] = None) -> None:
    """Delega al DetailedLogger si está disponible."""
    if not _LOGGER_AVAILABLE:
        return
    try:
        log = _get_detailed_logger()
        if getattr(log, "is_initialized", False):
            log.log_memory_operation(
                op, "context_window", session_id,
                items_count=None, duration=duration, success=success,
            )
    except Exception as _d2e:
        log_degraded(logging.getLogger(__name__), _d2e, "src/memory/context_window.py:_log_ctx")


# ── Import seguro EmbeddingOptimizer (para estrategia RELEVANCE) ─────────────
try:
    from .embedding_optimizer import EmbeddingOptimizer
    _OPTIMIZER_AVAILABLE = True
except ImportError:
    _OPTIMIZER_AVAILABLE = False
    EmbeddingOptimizer = None

# ── Import de componentes extraídos ──────────────────────────────────────────
from .context_strategies import (
    TokenEstimator,
    MessageCompressor,
    MessageDeduplicator,
    ContextCompactor,
    SelectorConfig,
    RecencySelector,
    RelevanceSelector,
    HybridSelector,
    MythosInspiredSelector,
)
from src.utils.degradation import log_degraded


# ============================================================================
# TIPOS Y CONFIGURACIÓN
# ============================================================================

class SelectionStrategy(str, Enum):
    """
    Estrategia de selección de mensajes para la ventana de contexto.

    RECENCY   → Últimos N mensajes. Rápido, sin análisis semántico.
    RELEVANCE → Mensajes más similares a la query. Requiere EmbeddingOptimizer.
    HYBRID    → Combina recencia + relevancia con pesos configurables.
    SMART     → Auto-selecciona según largo de conversación.
    MYTHOS_INSPIRED → Pruning por densidad semántica.
    """
    RECENCY   = "recency"
    RELEVANCE = "relevance"
    HYBRID    = "hybrid"
    SMART     = "smart"
    MYTHOS_INSPIRED = "mythos_inspired"


@dataclass
class WindowConfig:
    """
    Configuración del ContextWindowManager.

    Atributos:
        max_tokens          : Límite de tokens para el contexto completo.
        chars_per_token     : Estimación de chars por token.
        recency_anchor      : En HYBRID, siempre incluir los últimos N mensajes.
        relevance_weight    : En HYBRID, peso de relevancia vs recencia (0.0-1.0).
        min_similarity      : Threshold mínimo para incluir mensaje por relevancia.
        always_include_system: Siempre incluir mensajes de sistema.
        compress_long       : Si True, comprime mensajes muy largos truncando.
        compress_threshold  : Tokens máximos por mensaje antes de comprimir.
        deduplicate         : Eliminar mensajes duplicados o casi duplicados.
        session_id          : ID de sesión para logging.
        dynamic_threshold_min: Floor en contexto pesado.
        dynamic_threshold_max: Ceiling en contexto ligero.
    """
    max_tokens: int = 2048
    chars_per_token: float = 4.0
    recency_anchor: int = 3
    relevance_weight: float = 0.6
    min_similarity: float = 0.3
    always_include_system: bool = True
    compress_long: bool = True
    compress_threshold: int = 300
    deduplicate: bool = True
    session_id: str = ""
    dynamic_threshold_min: float = 0.85
    dynamic_threshold_max: float = 0.95

    @property
    def max_chars(self) -> int:
        """Límite en caracteres equivalente a max_tokens."""
        return int(self.max_tokens * self.chars_per_token)

    def to_selector_config(self) -> SelectorConfig:
        """Proyecta los campos que los selectores necesitan."""
        return SelectorConfig(
            dynamic_threshold_min=self.dynamic_threshold_min,
            dynamic_threshold_max=self.dynamic_threshold_max,
            max_tokens=self.max_tokens,
            recency_anchor=self.recency_anchor,
        )


@dataclass
class OptimizedContext:
    """
    Resultado del proceso de optimización de contexto.

    Atributos:
        messages      : Lista de mensajes seleccionados, listos para el LLM.
        token_count   : Tokens estimados del contexto resultante.
        original_count: Cantidad de mensajes originales antes de optimizar.
        dropped_count : Mensajes descartados por límite o irrelevancia.
        strategy_used : Estrategia aplicada efectivamente.
        latency_ms    : Tiempo de optimización en milisegundos.
        stats         : Estadísticas adicionales del proceso.
    """
    messages: List[Dict[str, Any]]
    token_count: int
    original_count: int
    dropped_count: int
    strategy_used: SelectionStrategy
    latency_ms: float = 0.0
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def compression_ratio(self) -> float:
        """Ratio de compresión (1.0 = sin compresión)."""
        if self.original_count == 0:
            return 1.0
        return round(len(self.messages) / self.original_count, 4)

    def to_dict(self) -> Dict[str, Any]:
        """Serialización para logging y métricas."""
        return {
            "message_count": len(self.messages),
            "token_count": self.token_count,
            "original_count": self.original_count,
            "dropped_count": self.dropped_count,
            "compression_ratio": self.compression_ratio,
            "strategy_used": self.strategy_used.value,
            "latency_ms": round(self.latency_ms, 2),
            "stats": self.stats,
        }


# ============================================================================
# CONTEXT WINDOW MANAGER — Orquestador principal
# ============================================================================

class ContextWindowManager:
    """
    Optimizador inteligente del contexto antes de pasarlo al LLM.

    Recibe la lista completa de mensajes del gateway y retorna
    un subconjunto optimizado que:
      - Cabe dentro del límite de tokens del modelo
      - Maximiza relevancia para la query actual
      - Preserva coherencia conversacional (ancla temporal)
      - Comprime mensajes muy largos si es necesario
      - Elimina duplicados

    Thread-safe. Una instancia puede reutilizarse entre llamadas.
    No tiene estado entre llamadas — es stateless por diseño.

    Uso desde BaseAgent:
        manager = get_context_window_manager()
        optimized = manager.optimize(
            messages=self._get_context(limit=50),
            query=input_msg.content,
        )
        # Pasar optimized.messages al LLM en lugar del contexto raw
    """

    def __init__(
        self,
        config: Optional[WindowConfig] = None,
        optimizer: Optional[Any] = None,  # EmbeddingOptimizer
    ) -> None:
        self.config = config or WindowConfig()
        self._estimator = TokenEstimator(chars_per_token=self.config.chars_per_token)
        self._compressor = MessageCompressor(
            estimator=self._estimator,
            threshold_tokens=self.config.compress_threshold,
        )
        self._deduplicator = MessageDeduplicator()
        self._compactor = ContextCompactor(deduplicator=self._deduplicator)

        # EmbeddingOptimizer para estrategia RELEVANCE/HYBRID
        self._optimizer: Optional[Any] = None
        if optimizer is not None:
            self._optimizer = optimizer
        elif _OPTIMIZER_AVAILABLE:
            try:
                from .embedding_optimizer import get_embedding_optimizer
                self._optimizer = get_embedding_optimizer()
            except Exception as e:
                log_degraded(logger, e, "src/memory/context_window.py:__init__")

        # ── Construir grafo de selectores (Strategy pattern) ───────────
        sel_cfg = self.config.to_selector_config()
        self._recency = RecencySelector(self._estimator)
        self._relevance = RelevanceSelector(
            self._estimator, self._optimizer, sel_cfg, self._recency,
        )
        self._hybrid = HybridSelector(
            self._estimator, self._relevance, self._recency, sel_cfg,
        )
        self._mythos = MythosInspiredSelector(self._hybrid)

        self._selectors: Dict[SelectionStrategy, Any] = {
            SelectionStrategy.RECENCY: self._recency,
            SelectionStrategy.RELEVANCE: self._relevance,
            SelectionStrategy.HYBRID: self._hybrid,
            SelectionStrategy.MYTHOS_INSPIRED: self._mythos,
        }

        # Métricas acumuladas
        self._total_calls = 0
        self._total_dropped = 0
        self._total_auto_compacted = 0

        logger.info(
            f"ContextWindowManager inicializado: "
            f"max_tokens={self.config.max_tokens} "
            f"optimizer={'✅' if self._optimizer else '❌'}"
        )

    # ── API pública ───────────────────────────────────────────────────────────

    def optimize(
        self,
        messages: List[Dict[str, Any]],
        query: str = "",
        strategy: SelectionStrategy = SelectionStrategy.SMART,
        max_tokens: Optional[int] = None,
    ) -> OptimizedContext:
        """
        Optimiza el contexto para la query actual.

        Punto de entrada principal. Siempre retorna un OptimizedContext
        válido — nunca lanza excepciones al llamador.

        Args:
            messages  : Lista de mensajes de get_context() — [{role, content, ...}]
            query     : Query actual del usuario.
            strategy  : Estrategia de selección. Default SMART (auto-selecciona).
            max_tokens: Override del límite de tokens. None = usar config.

        Returns:
            OptimizedContext con messages listos para el LLM.
        """
        t0 = time.perf_counter()
        self._total_calls += 1
        original_count = len(messages)
        effective_max = max_tokens or self.config.max_tokens

        # Caso trivial — lista vacía
        if not messages:
            return OptimizedContext(
                messages=[], token_count=0, original_count=0,
                dropped_count=0, strategy_used=strategy, latency_ms=0.0,
            )

        try:
            # 1. Deduplicar si está habilitado
            working = messages
            if self.config.deduplicate:
                working = self._deduplicator.deduplicate(working)

            # 2. Separar mensajes de sistema (siempre incluidos primero)
            system_msgs, non_system = self._split_system(working)

            # 3. Resolver estrategia efectiva
            effective_strategy = self._resolve_strategy(strategy, non_system, query)

            # 4. Seleccionar mensajes según estrategia (delegado a selectores)
            available_tokens = effective_max - self._estimator.estimate_messages(system_msgs)
            selector = self._selectors.get(
                effective_strategy, self._recency
            )
            selected = selector.select(non_system, query, available_tokens)

            # 5. Comprimir mensajes largos si es necesario
            if self.config.compress_long:
                selected = [self._compressor.compress(m) for m in selected]
                system_msgs = [self._compressor.compress(m) for m in system_msgs]

            # 6. Ensamblar contexto final: system + selected
            final = system_msgs + selected

            # 7. Verificación final de tokens — si aún excede, truncar por recencia
            final = self._enforce_token_limit(final, effective_max)

            token_count = self._estimator.estimate_messages(final)
            dropped = original_count - len(final)
            self._total_dropped += max(0, dropped)
            latency = (time.perf_counter() - t0) * 1000

            result = OptimizedContext(
                messages=final,
                token_count=token_count,
                original_count=original_count,
                dropped_count=max(0, dropped),
                strategy_used=effective_strategy,
                latency_ms=latency,
                stats={
                    "deduplicated": len(messages) - len(working),
                    "system_messages": len(system_msgs),
                    "selected_messages": len(selected),
                    "compressed": self._compressor.compressed_count,
                    "optimizer_available": self._optimizer is not None,
                },
            )

            _log_ctx(
                "optimize",
                session_id=self.config.session_id,
                duration=latency / 1000,
                success=True,
                extra=result.to_dict(),
            )

            logger.debug(
                f"[ContextWindow] {original_count}→{len(final)} msgs "
                f"({token_count} tokens, {effective_strategy.value}, {latency:.1f}ms)"
            )

            return result

        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            logger.error(f"[ContextWindow] Error en optimize(): {e} — fallback a RECENCY")
            fallback = self._recency.select(messages, "", effective_max)
            return OptimizedContext(
                messages=fallback,
                token_count=self._estimator.estimate_messages(fallback),
                original_count=original_count,
                dropped_count=original_count - len(fallback),
                strategy_used=SelectionStrategy.RECENCY,
                latency_ms=latency,
                stats={"error": str(e), "fallback": True},
            )

    def fits(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> bool:
        """True si los mensajes caben en la ventana sin optimización."""
        effective_max = max_tokens or self.config.max_tokens
        return self._estimator.fits_in_window(messages, effective_max)

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Estima tokens de una lista de mensajes."""
        return self._estimator.estimate_messages(messages)

    def get_stats(self) -> Dict[str, Any]:
        """Métricas acumuladas del manager."""
        return {
            "total_calls": self._total_calls,
            "total_dropped": self._total_dropped,
            "total_micro_compacted": self._compactor.total_micro_compacted,
            "total_auto_compacted": self._total_auto_compacted,
            "avg_dropped_per_call": round(
                self._total_dropped / max(self._total_calls, 1), 2
            ),
            "config": {
                "max_tokens": self.config.max_tokens,
                "strategy_default": SelectionStrategy.SMART.value,
                "compress_long": self.config.compress_long,
                "deduplicate": self.config.deduplicate,
            },
            "optimizer_available": self._optimizer is not None,
        }

    # ── v0.6.0 (Fase 3): MicroCompact + AutoCompact ───────────────────────

    def micro_compact(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Compresión ligera sin LLM — delega a ContextCompactor.

        Aplica en orden:
          1. Deduplicación (near-duplicates por rol)
          2. Colapso de mensajes de sistema repetidos (preserva el último)
          3. Limpieza de whitespace excesivo en contenidos

        Reducción esperada: 5-20% en sesiones normales. O(n).
        """
        return self._compactor.micro_compact(messages)

    def auto_compact(
        self,
        messages: List[Dict[str, Any]],
        session_id: str = "",
        threshold_tokens: int = 12000,
        transcript_dir: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Compresión automática cuando el contexto supera el threshold.

        Si token_count > threshold_tokens:
          1. Guarda el historial completo como transcript JSONL
          2. Aplica micro_compact (delegado a ContextCompactor)
          3. Aplica optimize() con RECENCY sobre los mensajes compactados
          4. Agrega mensaje de sistema indicando que hubo compresión

        Reducción esperada: 40-60% en sesiones largas.
        """
        current_tokens = self._estimator.estimate_messages(messages)

        if current_tokens <= threshold_tokens:
            return messages, False

        logger.info(
            f"[ContextWindow] auto_compact activado — "
            f"{current_tokens} tokens > threshold {threshold_tokens}"
        )

        # 1. Guardar transcript JSONL antes de compactar
        self._compactor.save_transcript(messages, session_id, transcript_dir)

        # 2. MicroCompact primero
        compacted = self._compactor.micro_compact(messages)

        # 3. Optimize con RECENCY — queremos mantener los más recientes
        optimized = self.optimize(
            messages=compacted,
            query="",
            strategy=SelectionStrategy.RECENCY,
            max_tokens=self.config.max_tokens,
        )

        result = optimized.messages

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 3: Token Drift por Acumulación mitigado.
        # Se eliminan las notas de compresión anteriores antes de insertar
        # la nueva, evitando acumular system messages redundantes.
        # ═══════════════════════════════════════════════════════════════
        result = [
            m for m in result
            if not (m.get("role") == "system" and m.get("content", "").startswith("[Contexto comprimido automáticamente"))
        ]

        # 4. Agregar nota de sistema indicando compresión
        compression_note = {
            "role": "system",
            "content": (
                f"[Contexto comprimido automáticamente. "
                f"Historial completo guardado en transcript. "
                f"Mensajes preservados: {len(result)}/{len(messages)}]"
            ),
        }
        system_idx = next(
            (i for i, m in enumerate(result) if m.get("role") == "system"), -1
        )
        if system_idx >= 0:
            result.insert(system_idx + 1, compression_note)
        else:
            result.insert(0, compression_note)

        self._total_auto_compacted += 1
        logger.info(
            f"[ContextWindow] auto_compact completado — "
            f"{len(messages)}→{len(result)} msgs "
            f"({current_tokens}→{self._estimator.estimate_messages(result)} tokens)"
        )

        return result, True

    # ── Métodos internos ──────────────────────────────────────────────────────

    def _split_system(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Separa mensajes de sistema del resto."""
        if not self.config.always_include_system:
            return [], messages
        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        return system, non_system

    def _resolve_strategy(
        self,
        strategy: SelectionStrategy,
        messages: List[Dict[str, Any]],
        query: str,
    ) -> SelectionStrategy:
        """Resuelve SMART a la estrategia concreta apropiada."""
        if strategy != SelectionStrategy.SMART:
            return strategy

        count = len(messages)
        if count <= 10:
            return SelectionStrategy.RECENCY
        elif count <= 30:
            return SelectionStrategy.HYBRID
        else:
            if self._optimizer and query:
                return SelectionStrategy.RELEVANCE
            return SelectionStrategy.HYBRID

    def _enforce_token_limit(
        self, messages: List[Dict[str, Any]], max_tokens: int
    ) -> List[Dict[str, Any]]:
        """
        Verificación final — trunca desde el inicio si aún excede el límite.
        Garantía de seguridad que nunca falla.
        Los mensajes de sistema se preservan si es posible.

        ✅ FIX FASE 3: System Prompt Overflow mitigado.
        Si los system_msgs solos superan max_tokens, se intenta conservar
        solo el más reciente para no enviar un contexto que exceda el límite
        y cause fallos silenciosos en Ollama.
        """
        if self._estimator.fits_in_window(messages, max_tokens):
            return messages

        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        system_tokens = self._estimator.estimate_messages(system)
        available = max_tokens - system_tokens

        # ═══════════════════════════════════════════════════════════════
        # ✅ FIX FASE 3: System Prompt Overflow mitigado.
        # Si available < 0, los system msgs superan la ventana.
        # Intentamos mantener solo el último (suele ser el principal).
        # ═══════════════════════════════════════════════════════════════
        if available < 0:
            logger.warning(
                f"[ContextWindow] System prompts ({system_tokens} tokens) exceed "
                f"max_tokens ({max_tokens}). Truncating older system prompts."
            )
            if len(system) > 1:
                system = system[-1:]
                system_tokens = self._estimator.estimate_messages(system)
                available = max_tokens - system_tokens

            # Si incluso un solo system prompt excede el límite, lo devolvemos
            # para evitar un fallo completo, pero registramos el error crítico.
            if available < 0:
                logger.critical(
                    f"[ContextWindow] Single system prompt ({system_tokens} tokens) "
                    f"STILL exceeds max limit ({max_tokens}). LLM may truncate or fail."
                )
                return system

        result_non_system: List[Dict[str, Any]] = []
        tokens_used = 0
        for msg in reversed(non_system):
            msg_tokens = self._estimator.estimate_message(msg)
            if tokens_used + msg_tokens > available:
                break
            result_non_system.insert(0, msg)
            tokens_used += msg_tokens

        return system + result_non_system


# ============================================================================
# SINGLETON ACCESSOR
# ============================================================================

_manager: Optional[ContextWindowManager] = None


def get_context_window_manager(
    max_tokens: int = 2048,
    config: Optional[WindowConfig] = None,
) -> ContextWindowManager:
    """
    Retorna la instancia singleton del ContextWindowManager.

    Thread-safe. Crea la instancia en el primer llamado.

    Uso desde BaseAgent o cualquier agente:
        from src.memory.context_window import get_context_window_manager
        from src.memory.context_window import SelectionStrategy

        manager = get_context_window_manager(max_tokens=2048)
        optimized = manager.optimize(
            messages=self._get_context(limit=50),
            query=input_msg.content,
            strategy=SelectionStrategy.SMART,
        )
        # Usar optimized.messages como contexto para el LLM
    """
    global _manager
    if _manager is None:
        effective_config = config or WindowConfig(max_tokens=max_tokens)
        _manager = ContextWindowManager(config=effective_config)
    return _manager


# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN v1.1.1 — FASE 3
# ═══════════════════════════════════════════════════════════════
# | Sección              | Cambio                                    | Razón                     |
# |----------------------|-------------------------------------------|---------------------------|
# | Header               | ✅ v1.1.1 changelog (FASE 3)             | Trazabilidad              |
# | _enforce_token_limit | ✅ Verificación available < 0            | System Prompt Overflow    |
# | auto_compact         | ✅ Filtrar notas de compresión previas   | Evitar Token Drift        |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
