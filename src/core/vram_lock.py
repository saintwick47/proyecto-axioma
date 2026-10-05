#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA — VRAM Lock (Candado de VRAM)
# Ruta: src/core/vram_lock.py
# Autor: SaintWick
# Propósito: Prevenir VRAM thrashing bloqueando cargas de modelos
#            cuando una operación pesada (como RAC) está en curso.
# ═══════════════════════════════════════════════════════════════
import threading
import logging

logger = logging.getLogger(__name__)

# Evento de threading: set() = bloqueado, clear() = libre
_vram_lock = threading.Event()
# ✅ ISSUE-101: segundo evento con la polaridad INVERSA (set() = LIBRE).
# `wait_for_vram_unlock()` esperaba sobre `_vram_lock`, que está SET justamente
# mientras el candado está TOMADO, y `Event.wait()` sobre un evento ya seteado
# vuelve **True al instante** (informa "seteado", no "liberado") ⇒ nunca esperaba:
# medido, devolvía True en 0,000 s con timeout=3 s y el candado tomado.
_vram_libre = threading.Event()
_vram_libre.set()  # arranca libre
_vram_lock_reason = ""

def acquire_vram_lock(reason: str = "system_maintenance"):
    """Toma el candado de VRAM. Las operaciones de carga de modelos deben detenerse."""
    global _vram_lock_reason
    _vram_lock_reason = reason
    # Primero marcar como NO libre y después como tomado (así un esperador que
    # consulta `is_vram_locked()` y luego espera no puede leer un estado viejo).
    _vram_libre.clear()
    _vram_lock.set()
    logger.info(f"🔒 VRAM LOCK ACQUIRED: {reason}")

def release_vram_lock():
    """Libera el candado de VRAM y despierta a quien esté esperando."""
    global _vram_lock_reason
    _vram_lock_reason = ""
    _vram_lock.clear()
    _vram_libre.set()  # ✅ ISSUE-101: despierta a wait_for_vram_unlock()
    logger.info("🔓 VRAM LOCK RELEASED — Modelos pueden cargar normalmente.")

def is_vram_locked() -> bool:
    """Verifica si la VRAM está bloqueada."""
    return _vram_lock.is_set()

def get_vram_lock_reason() -> str:
    """Retorna la razón del bloqueo actual."""
    return _vram_lock_reason

def wait_for_vram_unlock(timeout: float = 300.0) -> bool:
    """Espera a que la VRAM se libere (útil para warmup y ModelSwap).

    ✅ ISSUE-101: se espera sobre `_vram_libre` (set() = LIBRE), NO sobre
    `_vram_lock`. Antes se esperaba sobre el evento del candado, que está SET
    mientras está tomado, así que `Event.wait()` volvía True de inmediato y esta
    función **nunca esperaba**: informaba "VRAM libre" con una operación pesada en
    curso. La usan `ModelSwap` (antes de cargar un modelo) y `tools/warmup_models.py`.

    Args:
        timeout: Segundos máximos a esperar (default 5 minutos)

    Returns:
        True si se liberó, False si timeout expiró
    """
    if not _vram_lock.is_set():
        return True
    logger.info(f"⏳ VRAM bloqueada ({_vram_lock_reason}). Esperando liberación...")
    unlocked = _vram_libre.wait(timeout=timeout)
    if unlocked:
        logger.info("✅ VRAM liberada, continuando...")
    else:
        logger.warning(f"⚠️ Timeout esperando liberación de VRAM ({timeout}s)")
    return unlocked
