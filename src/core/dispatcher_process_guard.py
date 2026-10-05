#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────────
# AXIOMA — dispatcher_process_guard.py  (REFACTORIZACIÓN 50/50)
# Ruta: src/core/dispatcher_process_guard.py
#
# ✅ v0.6.9v (REFACTOR): capa de GUARD + WRITE-PATH de memoria extraída de
#   src/core/dispatcher_process.py (el original queda intacto).
#
#   Qué se movió desde dispatcher_process.py (sin cambios de comportamiento):
#     - `_screen_input`               → gate anti-injection (PLAN v2.0 Fase 3)
#     - `_qm_record`                  → alimenta QualityMonitor (fire-and-forget)
#     - `_get_session_gateway`        → gateway de memoria cacheado por sesión
#     - `_record_exchange`            → write-path único de memoria conversacional
#     - `_historial_para_prompt`      → read-path del chat (ISSUE-130: sin esto
#                                        cada consulta se respondía aislada)
#
#   `Dispatcher` hereda ahora `DispatcherProcessGuardMixin`, así la API pública
#   (process llama a estos métodos vía self.) no cambia.
#
#   Los métodos acceden a atributos definidos en dispatcher.py (self._memory_gateways,
#   self._memory_gateways_lock, self._memory_gateways_max, self._user_id) vía self.,
#   sin importar dispatcher.py — evita import circular, igual que el mixin original.
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from src.domain.entities import Message, Response
from src.utils.degradation import log_degraded
from config.settings import settings

logger = logging.getLogger(__name__)


# ── Historial que se reinyecta al modelo en el chat (ISSUE-130) ──────────────
# 4 turnos = 8 mensajes: alcanza para que "¿y eso por qué?" tenga referente y
# queda acotado para no inflar la prelectura del contexto en CPU. El recorte por
# caracteres evita que un turno largo (una respuesta de código, por ejemplo) se
# coma la ventana de los turnos siguientes.
HISTORIAL_TURNOS_MAX = 4
HISTORIAL_CHARS_POR_MENSAJE = 400


# Techo cuando la consulta pedía un DATO verificable y la respuesta no trae
# ninguna fuente: el validador puede aprobar el texto, pero el sistema no tiene
# respaldo → mostrarlo como "Alto" sería engañoso (ISSUE-063).
NO_SOURCES_FACTUAL_CAP = 0.6


def calibrate_response_confidence(response: Any,
                                 validator_score: Optional[float] = None,
                                 validation_failed: bool = False,
                                 task_type: str = "",
                                 expects_grounding: bool = False) -> Optional[float]:
    """Calibra la confianza que se MUESTRA en el panel de transparencia.

    ✅ ISSUE-061: extraída del `process()` (era lógica inline en un método de
    ~1000 líneas). Motivo concreto: ahí vivía un `UnboundLocalError` real —
    `_val_score` se declaraba dentro de la rama del validador y se usaba después
    en TODOS los caminos, así que cuando la validación se omite (respuesta corta,
    grounding, tarea sin validación) la calibración explotaba. Al ser una función
    con contrato explícito, eso ahora se testea.

    Reglas (ver `src/utils/confidence.py`): sin evidencia de validación la
    confianza del agente queda **intacta**; `Response.confidence` NO decide nada
    en el pipeline (es dato de presentación).

    Nunca lanza: si algo falla, se degrada con `log_degraded` y devuelve None.
    """
    try:
        from src.utils.confidence import apply_calibration

        score = validator_score
        if score is None:
            report = (getattr(response, "metadata", None) or {}).get(
                "validation_report")
            if isinstance(report, dict):
                rscore = report.get("score")
                if isinstance(rscore, (int, float)):
                    score = float(rscore)

        before = getattr(response, "confidence", None)
        after = apply_calibration(response, validator_score=score,
                                  validation_failed=validation_failed)
        # ✅ ISSUE-063: consulta de DATO sin ninguna fuente → no se muestra como
        # muy confiable (el panel igual aclara "sin citas").
        if expects_grounding and after is not None and not validation_failed:
            sources = getattr(response, "sources", None) or []
            if not [s for s in sources if isinstance(s, str) and s.strip()]:
                capped = min(after, NO_SOURCES_FACTUAL_CAP)
                if capped != after:
                    response.confidence = capped
                    try:
                        from src.utils.confidence import label_for
                        response.confidence_label = label_for(capped)
                    except Exception as e:
                        # R3: la etiqueta es presentación; el tope de confianza ya se aplicó.
                        log_degraded(logger, e, "Dispatcher.calibrate_response_confidence (confidence_label)",
                                     contract_level=logging.DEBUG)
                    after = capped
        if after is not None and after != before:
            logger.debug(
                f"[confianza] {task_type or 'tarea'}: {before} → {after} "
                f"(val_score={score}, failed={validation_failed})")
        return after
    except Exception as exc:  # noqa: BLE001 — dato de presentación
        log_degraded(logger, exc, "dispatcher_process_guard.calibrate_response_confidence")
        return None


class DispatcherProcessGuardMixin:
    """Guardias del pipeline y escritura de memoria conversacional.

    Extraído de dispatcher_process.py (50/50): este mixin se ocupa del
    gate anti-injection, telemetría de QualityMonitor y persistencia de
    los intercambios en memoria. El process() original queda delegando a
    estos métodos sin cambiar su contrato.
    """

    async def _qm_record(self, task_type: str, agent_role: str, score: float) -> None:
        """✅ v0.6.9d: alimenta QualityMonitor con un score (fire-and-forget).

        Nunca lanza hacia el caller: cualquier fallo del monitor (lazy init,
        DB, etc.) solo loguea en DEBUG. No bloquea peticiones.

        ✅ v0.6.9v (ISSUE-018): FIX — `QualityMonitor.record_score()` espera un
        objeto `QualityScore`, no un float. Antes se le pasaba el float directo
        → `AttributeError: 'float' object has no attribute 'overall'`, silenciado
        por el `except` de abajo. Resultado: los scores del validador del
        dispatcher NUNCA llegaban al monitor (métrica perdida). Ahora se
        envuelve el float en `QualityScore(overall=score)`.
        """
        try:
            from src.core.quality_monitor import get_quality_monitor, QualityScore
            monitor = await get_quality_monitor()
            await monitor.record_score(
                task_type=task_type,
                agent_role=agent_role,
                score=QualityScore(overall=float(score)),
            )
        except Exception as e:
            # ✅ ISSUE-019: si es error de contrato (firma/tipo) se loguea a
            # WARNING — antes quedaba en DEBUG y ocultó el bug de ISSUE-018.
            from src.utils.degradation import log_degraded
            log_degraded(logger, e, "Dispatcher._qm_record",
                         extra={"task_type": task_type, "agent_role": agent_role})

    def _get_session_gateway(self, session_id: str) -> Optional[Any]:
        """Gateway de memoria cacheado por sesión (acotado, thread-safe)."""
        if not session_id:
            return None
        with self._memory_gateways_lock:
            gw = self._memory_gateways.get(session_id)
            if gw is None:
                if len(self._memory_gateways) >= self._memory_gateways_max:
                    # Evictar la sesión más vieja (dict mantiene inserción).
                    self._memory_gateways.pop(next(iter(self._memory_gateways)))
                from src.memory.gateway import MemoryGateway
                # ✅ Capa 2 (2026-08-31): el gateway conoce al usuario → el
                # write etiqueta user_id y get_context puede recuperar de
                # otras sesiones del mismo usuario. La identidad viene del
                # Dispatcher de la sesión (UI de usuarios) o del setting.
                gw = MemoryGateway(
                    session_id=session_id,
                    user_id=getattr(self, "_user_id", None)
                    or getattr(settings, "axioma_user_id", ""),
                )
                self._memory_gateways[session_id] = gw
            return gw

    def _record_exchange(
        self,
        message: Message,
        response: Response,
        session_id: Optional[str],
        task_type: str,
    ) -> None:
        """
        ✅ plan_memory (B, 2026-08-29): punto ÚNICO de escritura del write-path
        de memoria conversacional. Antes MemoryGateway.add_message no tenía
        callers de producción (memoria desconectada: data/axioma.db sin tabla
        de mensajes). Acá se persisten user + assistant de cada intercambio
        REAL (path principal, no cache hits — esos ya se registraron al
        generarse) en la sesión; los agentes leen esa misma sesión vía
        _get_context(session_id=...).

        Limitación conocida (plan_memory, punto 4): la sesión es efímera
        (SessionState genera nicegui_<timestamp> por recarga de browser); la
        memoria persiste DENTRO de la sesión. Retrieval cross-sesión (Capa 2)
        requiere identidad persistente de usuario, que hoy no existe.
        """
        if not session_id or response.error is not None or not message.content:
            return
        try:
            gw = self._get_session_gateway(session_id)
            if gw is None:
                return
            from src.memory.gateway import TASK_TO_SOURCE
            source_type = TASK_TO_SOURCE.get(task_type, "chat")
            meta = {"task_type": task_type}
            # ✅ v0.6.8t: coacción defensiva a str — content no-str rompía la
            # validación de long_term (MemoryError silencioso → ✗ add_message).
            user_text = message.content if isinstance(message.content, str) else str(message.content)
            gw.add_message(
                "user", user_text, metadata=meta, source_type=source_type,
            )
            if response.content:
                asst_text = response.content if isinstance(response.content, str) else str(response.content)
                gw.add_message(
                    "assistant", asst_text, metadata=meta,
                    source_type=source_type,
                )
        except Exception as e:
            # Degradación: la memoria nunca debe romper el request.
            # ✅ ISSUE-019: un error de contrato acá = pérdida silenciosa de
            # memoria conversacional → se loguea a WARNING, no a DEBUG.
            from src.utils.degradation import log_degraded
            log_degraded(logger, e, "MemoryGateway._record_exchange",
                         extra={"session_id": session_id, "task_type": task_type})

    def _historial_para_prompt(
        self,
        session_id: Optional[str],
        texto_actual: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        ✅ ISSUE-130: turnos previos de ESTA sesión, listos para el prompt.

        El write-path (`_record_exchange`) ya guardaba cada intercambio en la
        memoria de la sesión, pero NADIE lo leía en el chat: `_handle_chat`
        armaba `messages` con la instrucción de sistema y el mensaje nuevo, así
        que cada consulta se respondía aislada ("y eso por qué?" no tenía
        referente). Esto cierra el círculo. Efecto lateral buscado: la sesión de
        voz de Rafael es fija por instancia (`voice-<hex>`), así que la charla
        hablada también deja de ser un ida-y-vuelta sin memoria.

        Ruta BARATA a propósito: `get_context` sin `task_type` ni `query` cae en
        la rama de cercanía, sin búsqueda semántica ni modelo de vectores, así
        que no agrega cargas permanentes (R7). Reutiliza el portal de memoria ya
        cacheado por sesión (mismo objeto que usa el write-path), no crea uno
        nuevo. Nunca lanza: sin memoria el chat responde igual, en modo aislado.
        """
        if not session_id:
            return []
        try:
            gw = self._get_session_gateway(session_id)
            if gw is None:
                return []
            crudos = gw.get_context(
                limit=HISTORIAL_TURNOS_MAX * 2, include_system=False,
            )
        except Exception as e:
            log_degraded(logger, e, "Dispatcher._historial_para_prompt",
                         extra={"session_id": session_id})
            return []

        historial: List[Dict[str, str]] = []
        for m in crudos or []:
            if not isinstance(m, dict):
                continue
            rol = m.get("role")
            contenido = m.get("content")
            if rol not in ("user", "assistant") or not contenido:
                continue
            texto = (contenido if isinstance(contenido, str) else str(contenido)).strip()
            if not texto:
                continue
            historial.append({
                "role": rol,
                "content": texto[:HISTORIAL_CHARS_POR_MENSAJE],
            })

        # El write-path corre DESPUÉS del handler, así que el mensaje nuevo no
        # debería estar guardado todavía. Si por otra vía (web, reintento del
        # mismo texto) ya estuviera, se descarta el último: mandarlo dos veces
        # empuja al modelo a contestar "ya me lo dijiste".
        actual = (texto_actual or "").strip()[:HISTORIAL_CHARS_POR_MENSAJE]
        if actual and historial and historial[-1]["role"] == "user" \
                and historial[-1]["content"] == actual:
            historial.pop()
        return historial

    def _firma_contexto(self, session_id: Optional[str],
                        texto_actual: Optional[str] = None) -> str:
        """Huella del historial que viaja al modelo (`""` si la sesión es nueva).

        ✅ MEDIDO (2026-10-03): las cachés de respuesta se indexaban **sólo por el
        texto de la pregunta**, así que el mismo texto en OTRA conversación recibía
        la respuesta vieja — con el historial inyectado desde ISSUE-130 eso es
        contaminación visible (el usuario la reportó). Esta huella entra en la clave
        de la caché de respuesta y, además, la caché semántica L3 sólo se consulta
        cuando la huella está vacía: una respuesta contextual nunca debe salir de un
        atajo semántico.
        """
        try:
            historial = self._historial_para_prompt(session_id, texto_actual)
        except Exception as e:
            log_degraded(logger, e, "Dispatcher._firma_contexto")
            return "sin-firma"      # ante la duda, se trata como contexto NO vacío
        if not historial:
            return ""
        crudo = "\x00".join(f"{m.get('role')}:{m.get('content')}" for m in historial)
        return hashlib.sha256(crudo.encode("utf-8", "ignore")).hexdigest()[:16]

    def _screen_input(self, text: Optional[str]) -> Optional[str]:
        """Gate anti-injection sobre el input directo (PLAN v2.0, Fase 3).

        Detecta intentos de jailbreak/prompt-injection reutilizando los
        patrones acotados de ValidatorAgent._INJECTION_PATTERNS. Devuelve el
        motivo del bloqueo (str) o None si el input pasa.

        Nota: no depende de ValidatorAgent instanciado (sin LLM, sin costo) —
        solo de los patrones pre-compilados a nivel de clase.
        """
        if not text:
            return None
        try:
            from src.agents.validator import ValidatorAgent
            match = ValidatorAgent.match_injection(text)
            if match:
                return f"jailbreak_pattern: {match.re.pattern[:60]}"
        except Exception as e:  # noqa: BLE001 — el gate nunca debe romper el pipeline
            # ✅ ISSUE-019: si el gate falla por contrato (p. ej. ValidatorAgent
            # cambió de firma) el filtro anti-injection queda DESACTIVADO en
            # silencio → eso debe verse a WARNING, no en DEBUG.
            from src.utils.degradation import log_degraded
            log_degraded(logger, e, "Dispatcher._screen_input")
        return None
