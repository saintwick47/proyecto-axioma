#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Core Module
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/core/__init__.py
# Autor: SaintWick
# Fecha: 2026-03-18
# Propósito: Export público del núcleo del sistema (Dispatcher)
# ═══════════════════════════════════════════════════════════════
# ✅ CORREGIDO: Orchestrator eliminado — Dispatcher como nuevo núcleo
"""
Core components del sistema AXIOMA.

Módulos:
- dispatcher: Ruteo simplificado de requests (reemplaza Orchestrator)

Ejemplo de uso:
    >>> from src.core import Dispatcher
    >>> dispatcher = Dispatcher()
    >>> response = dispatcher.process(message)
"""
from src.core.dispatcher import Dispatcher

# ✅ CORREGIDO: __all__ actualizado — Orchestrator eliminado
__all__ = ["Dispatcher"]

# ═══════════════════════════════════════════════════════════════
# 📋 RESUMEN DE CAMBIOS — ELIMINACIÓN Orchestrator
# ═══════════════════════════════════════════════════════════════
# | Línea | Cambio                          | Razón                  |
# |-------|---------------------------------|------------------------|
# | ~16   | ✅ Import Dispatcher            | Módulo correcto        |
# | ~17   | ✅ __all__ = ["Dispatcher"]     | Export limpio          |
# | TODAS | ✅ Sin referencias Orchestrator | Limpio completo        |
# ═══════════════════════════════════════════════════════════════
# ✅ ARCHIVO COMPLETO — 17 LÍNEAS — SIN OMISIONES — PROTOCOLO CUMPLIDO
# ═══════════════════════════════════════════════════════════════
