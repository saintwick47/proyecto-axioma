#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — degradation.py
# Ruta: src/utils/degradation.py
#
# ✅ v0.6.9v (ISSUE-019): helper de logging para EXCEPCIONES DEGRADADAS.
#
#   Contexto del problema: AXIOMA usa a propósito el patrón "degrada
#   silenciosamente para no romper el request" (correcto para resiliencia).
#   Pero cuando el fallo es un ERROR DE CONTRATO — el consumidor llama con una
#   firma/tipo equivocado (TypeError/AttributeError/NameError/…) — loguearlo a
#   DEBUG esconde un BUG DE INTEGRACIÓN: la feature deja de funcionar en
#   silencio. Caso real: `_qm_record` pasaba un `float` donde se esperaba un
#   `QualityScore` → los scores del validador NUNCA llegaban al QualityMonitor
#   y el fallo era invisible en producción (ver ISSUE-018).
#
#   Uso:
#       from src.utils.degradation import log_degraded
#       try:
#           ...
#       except Exception as exc:
#           log_degraded(logger, exc, "MemoryGateway._record_exchange")
#
#   - Errores de CONTRATO  → WARNING (visibles; indican bug de integración)
#   - Resto (red, timeout, datos) → DEBUG (degradación esperada, no ruido)
#
#   NO reemplaza masivamente los ~327 bloques `except → logger.debug` del
#   proyecto: migrar caso por caso, priorizando paths donde el fallo silencioso
#   implica pérdida de datos/features. Ver tools/audit_silent_excepts.py.
# ═══════════════════════════════════════════════════════════════
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Tuple, Type

# ═══════════════════════════════════════════════════════════════
# ✅ v0.6.9v (P1 del informe): MÉTRICAS de degradación
# ═══════════════════════════════════════════════════════════════
# Problema: `log_degraded` hacía VISIBLE un error de contrato en los logs, pero
# nadie podía CONTARLOS: no había forma de responder "¿cuántos
# [degradado/contrato] hubo, en qué módulo y con qué excepción?". Este registro
# en memoria (un dict + un lock: coste despreciable, sin RAM relevante) es la
# fuente de `GET /api/v1/metrics/degradation`.
#
# Rutas CRÍTICAS: si un error de contrato aparece acá, el sistema perdió una
# feature en silencio (caso real: ISSUE-018 — los scores del validador nunca
# llegaban al QualityMonitor). `degradation_stats()` las expone como alerta.
CRITICAL_ROUTES: Tuple[str, ...] = (
    "dispatcher",
    "memory",
    "validator",
    "feedback",
    "quality",
)

_STATS_LOCK = threading.Lock()
_STATS_MAX_KEYS = 200   # tope defensivo: nunca crecer sin límite
_STATS: Dict[str, Any] = {}



# ── Chequeo de intérprete (ISSUE-125) ─────────────────────────────────────────
def verificar_interprete(nombre_script: str) -> None:
    """Falla con un mensaje ACCIONABLE si falta una dependencia central (ISSUE-125).

    Corre **antes** de importar el resto del proyecto: si se ejecuta con el Python del
    SISTEMA en vez del venv, el error nativo es un `ModuleNotFoundError` en medio de una
    decena de imports, que parece un bug del código. Acá se explica qué hacer.

    Vive acá (y no duplicado en cada entry point) porque el auditor marca MEDIUM el
    código duplicado (`AUD-DUP-001`), y este módulo —a diferencia de `config.paths`—
    se puede importar **sin** las dependencias instaladas.

    ⚠️ Usa `importlib.util.find_spec` en vez de `try/except ModuleNotFoundError`: un
    `except` silencioso de un error de CONTRATO es exactamente lo que el detector
    `silent_contract_swallow` marca como MEDIUM.
    """
    import importlib.util
    import sys
    from pathlib import Path

    if importlib.util.find_spec("pydantic_settings") is not None:
        return

    venv_py = Path(__file__).resolve().parents[2] / "venv" / "bin" / "python"
    print("\n❌ Faltan las dependencias de AXIOMA: 'pydantic_settings' no está instalado.",
          file=sys.stderr)
    print(f"   Estás ejecutando con: {sys.executable}", file=sys.stderr)
    if venv_py.exists():
        print("\n   El proyecto usa su propio venv. Probá alguna de estas formas:",
              file=sys.stderr)
        args = " ".join(sys.argv[1:])
        print(f"       {venv_py} {nombre_script} {args}".rstrip(), file=sys.stderr)
        print(f"       source venv/bin/activate && python {nombre_script}", file=sys.stderr)
    else:
        print("\n   Instalá las dependencias:  pip install -r requirements.txt",
              file=sys.stderr)
    sys.exit(1)


def _reset_stats_locked() -> None:
    _STATS.clear()
    _STATS.update({
        "contract": 0, "soft": 0,
        "by_context": {}, "by_exception": {}, "by_route": {},
        "by_route_contract": {}, "by_context_contract": {},
        "criticos": 0, "first_seen": None, "last_seen": None,
        "last_contract_context": None,
    })


_reset_stats_locked()

# Errores que indican un problema de CONTRATO entre módulos (no de entorno):
# si aparece uno de estos dentro de un `except` de degradación, casi siempre
# es un bug de integración (firma/tipo/atributo) y debe ser visible.
CONTRACT_ERRORS: Tuple[Type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    ImportError,
    NotImplementedError,
)


def is_contract_error(exc: BaseException) -> bool:
    """True si la excepción parece un error de contrato (bug de integración)."""
    return isinstance(exc, CONTRACT_ERRORS)


def log_degraded(logger: logging.Logger, exc: BaseException, context: str,
                 extra: dict = None, contract_level: int = logging.WARNING,
                 soft_level: int = logging.DEBUG) -> bool:
    """Loguea una excepción degradada eligiendo el nivel según su naturaleza.

    Args:
        logger: logger del módulo que degrada.
        exc: la excepción capturada.
        context: contexto legible (ej. "MemoryGateway._record_exchange").
        extra: dict opcional con datos de diagnóstico.
        contract_level: nivel para errores de contrato (default WARNING).
        soft_level: nivel para el resto (default DEBUG).

    Returns:
        True si se clasificó como error de contrato (útil para métricas/tests).
    """
    contract = is_contract_error(exc)
    level = contract_level if contract else soft_level
    tag = "degradado/contrato" if contract else "degradado"
    detail = f"[{tag}] {context}: {type(exc).__name__}: {exc}"
    if extra:
        detail += f" | {extra}"
    try:
        logger.log(level, detail)
    except Exception:  # pragma: no cover — el logging nunca debe romper
        pass
    record_degradation(context, exc, contract)
    return contract


# ═══════════════════════════════════════════════════════════════
# MÉTRICAS (v0.6.9v, P1 del informe)
# ═══════════════════════════════════════════════════════════════
def _route_of(context: str) -> str:
    """Ruta crítica a la que pertenece un contexto (o 'otras')."""
    low = (context or "").lower()
    for route in CRITICAL_ROUTES:
        if route in low:
            return route
    return "otras"


def _bump(bucket: str, key: str) -> None:
    """Incrementa un contador interno, con tope de claves (evita crecer sin fin)."""
    d = _STATS.get(bucket)
    if not isinstance(d, dict):
        return
    # Se reserva 1 slot para "<otros>": si no, el dict podía llegar a MAX+1
    # claves (las MAX reales + el propio bucket de desborde).
    if key not in d and len(d) >= _STATS_MAX_KEYS - 1:
        key = "<otros>"
    d[key] = d.get(key, 0) + 1


def record_degradation(context: str, exc: BaseException, contract: bool) -> None:
    """Registra una degradación en las métricas en memoria (thread-safe)."""
    try:
        with _STATS_LOCK:
            _STATS["contract" if contract else "soft"] += 1
            _bump("by_context", context or "<sin-contexto>")
            _bump("by_exception", type(exc).__name__)
            route = _route_of(context)
            _bump("by_route", route)
            if contract:
                # Solo los errores de CONTRATO en ruta crítica son alerta: una
                # degradación blanda (timeout/red) en esa misma ruta es esperada.
                _bump("by_route_contract", route)
                _bump("by_context_contract", context or "<sin-contexto>")
                if route in CRITICAL_ROUTES:
                    _STATS["criticos"] += 1
                    _STATS["last_contract_context"] = context
            now = time.time()
            if _STATS["first_seen"] is None:
                _STATS["first_seen"] = now
            _STATS["last_seen"] = now
    except Exception as _d2e:  # pragma: no cover — las métricas nunca rompen
        logging.getLogger(__name__).debug(
            f"[D2 degradation.record_degradation] {_d2e}")


def degradation_stats() -> Dict[str, Any]:
    """Snapshot JSON-serializable de las métricas de degradación."""
    with _STATS_LOCK:
        snapshot: Dict[str, Any] = {
            "contract": _STATS["contract"],
            "soft": _STATS["soft"],
            "total": _STATS["contract"] + _STATS["soft"],
            "criticos_en_rutas_clave": _STATS["criticos"],
            "by_context": dict(_STATS["by_context"]),
            "by_exception": dict(_STATS["by_exception"]),
            "by_route": dict(_STATS["by_route"]),
            "by_route_contract": dict(_STATS["by_route_contract"]),
            "by_context_contract": dict(_STATS["by_context_contract"]),
            "last_contract_context": _STATS["last_contract_context"],
            "first_seen": _STATS["first_seen"],
            "last_seen": _STATS["last_seen"],
            "rutas_criticas": list(CRITICAL_ROUTES),
        }
    # Alerta: hubo errores de CONTRATO en una ruta crítica → se perdió una
    # feature en silencio y hay que mirarlo (no es ruido de red/timeout).
    alertas = []
    for ctx, n in sorted(snapshot["by_context_contract"].items(),
                         key=lambda kv: -kv[1]):
        ruta = _route_of(ctx)
        if ruta in CRITICAL_ROUTES:
            alertas.append(
                f"{n} error(es) de CONTRATO en ruta crítica '{ruta}': {ctx}")
    snapshot["alertas"] = alertas
    snapshot["hay_alerta"] = bool(alertas)
    return snapshot


def reset_degradation_stats() -> None:
    """Reinicia las métricas (tests y reinicios de sesión)."""
    with _STATS_LOCK:
        _reset_stats_locked()


__all__ = [
    "CONTRACT_ERRORS", "CRITICAL_ROUTES", "is_contract_error", "log_degraded",
    "record_degradation", "degradation_stats", "reset_degradation_stats",
]
