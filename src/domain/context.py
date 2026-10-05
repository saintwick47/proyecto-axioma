#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — SessionContext: Contrato de Contexto Conversacional
# Ruta: src/domain/context.py
# Autor: SaintWick
# Versión: 1.1.0 (Modo Jarvis)
# Cambios v1.1.0:
#   + jarvis_mode: str — estado de modo activo del sistema
#   + update_mode(): helper para cambio de modo con validación
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, timezone

VALID_JARVIS_MODES = frozenset({"normal", "jarvis", "voice_only", "silent"})


@dataclass
class SessionContext:
    """
    Estado conversacional de una sesión para clasificación contextual.

    Campos:
        session_id       : Identificador de sesión (requerido)
        last_intent      : Último TaskType clasificado (ej: "weather_query")
        recent_intents   : Historial de los últimos 5 intents (más reciente último)
        turns            : Número de turnos en la sesión actual
        is_voice_active  : True si VoicePipeline está hablando o escuchando
        jarvis_mode      : Modo activo del sistema ("normal" | "jarvis" | "voice_only" | "silent")
        last_updated     : Timestamp de última actualización (UTC)

    Uso en Dispatcher:
        ctx = SessionContext(session_id="abc123")
        classification = classifier.classify_with_context(text, ctx)
        ctx.update(task_type="weather_query")

    Uso en VoicePipeline:
        ctx = self._get_or_create_context(session_id)
        response = await dispatcher.process(msg, session_id=sid, context=ctx)
        ctx.update(task_type=response.task_type)

    Modos Jarvis:
        "normal"     — comportamiento estándar AXIOMA (GUI + voz opcional)
        "jarvis"     — voz full-duplex, respuestas cortas, sin GUI obligatoria
        "voice_only" — igual que jarvis pero sin salida en GUI
        "silent"     — procesa sin TTS (útil para automatizaciones)
    """

    session_id: str
    last_intent: Optional[str] = None
    recent_intents: List[str] = field(default_factory=list)
    turns: int = 0
    is_voice_active: bool = False
    jarvis_mode: str = "normal"
    last_updated: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ── Tokens de continuación que indican referencia a turno anterior ────────
    CONTINUATION_TOKENS: frozenset = field(
        default_factory=lambda: frozenset({
            # Español
            "sí", "si", "no", "ok", "dale", "bueno", "bien",
            "eso", "ese", "esa", "esos", "esas",
            "lo anterior", "la anterior", "el anterior",
            "mejor", "peor", "igual", "lo mismo",
            "más", "menos", "también", "además",
            "¿y?", "y?", "¿cuál?", "cuál", "cual",
            "continúa", "continua", "sigue", "seguí",
            "repite", "repetí", "otra vez",
            # Inglés (voz puede transcribir en inglés)
            "yes", "no", "ok", "sure", "continue",
            "same", "again", "more", "that one",
        }),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validaciones post-construcción."""
        if not self.session_id or not self.session_id.strip():
            raise ValueError("session_id no puede estar vacío")
        if self.jarvis_mode not in VALID_JARVIS_MODES:
            raise ValueError(
                f"jarvis_mode inválido: '{self.jarvis_mode}'. "
                f"Valores válidos: {sorted(VALID_JARVIS_MODES)}"
            )
        if len(self.recent_intents) > 5:
            self.recent_intents = self.recent_intents[-5:]

    # ── Actualización ─────────────────────────────────────────────────────────

    def update(self, task_type: str) -> "SessionContext":
        """
        Registra un nuevo intent clasificado.
        Actualiza last_intent, recent_intents (máx 5) y turns.
        Retorna self para encadenamiento.
        """
        self.last_intent = task_type
        self.recent_intents.append(task_type)
        if len(self.recent_intents) > 5:
            self.recent_intents = self.recent_intents[-5:]
        self.turns += 1
        self.last_updated = datetime.now(timezone.utc)
        return self

    def update_mode(self, mode: str) -> "SessionContext":
        """
        Cambia el modo activo del sistema con validación.
        Retorna self para encadenamiento.

        Raises:
            ValueError si el modo no es válido.
        """
        if mode not in VALID_JARVIS_MODES:
            raise ValueError(
                f"Modo inválido: '{mode}'. "
                f"Valores válidos: {sorted(VALID_JARVIS_MODES)}"
            )
        self.jarvis_mode = mode
        self.last_updated = datetime.now(timezone.utc)
        return self

    # ── Helpers de clasificación ──────────────────────────────────────────────

    def is_continuation(self, text: str) -> bool:
        """
        True si el texto parece una continuación del turno anterior.
        Criterios:
          1. Texto de 4 palabras o menos, O
          2. Contiene algún token de continuación conocido
        Requiere que exista last_intent (si no, siempre False).
        """
        if not self.last_intent:
            return False
        words = text.strip().lower().split()
        if len(words) <= 4:
            return True
        text_lower = text.strip().lower()
        return any(token in text_lower for token in self.CONTINUATION_TOKENS)

    def dominant_intent(self) -> Optional[str]:
        """
        Retorna el intent más frecuente en recent_intents.
        Si hay empate, retorna el más reciente.
        None si no hay historial.
        """
        if not self.recent_intents:
            return None
        counts: dict = {}
        for intent in self.recent_intents:
            counts[intent] = counts.get(intent, 0) + 1
        return max(counts, key=lambda k: (counts[k], self.recent_intents[::-1].index(k) * -1))

    def is_jarvis_active(self) -> bool:
        """True si el modo actual es diferente de 'normal'."""
        return self.jarvis_mode != "normal"

    def to_dict(self) -> dict:
        """Serialización para logging/métricas."""
        return {
            "session_id": self.session_id,
            "last_intent": self.last_intent,
            "recent_intents": self.recent_intents,
            "turns": self.turns,
            "is_voice_active": self.is_voice_active,
            "jarvis_mode": self.jarvis_mode,
            "last_updated": self.last_updated.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"SessionContext(sid={self.session_id[:8]}..., "
            f"last={self.last_intent}, turns={self.turns}, "
            f"mode={self.jarvis_mode})"
        )
