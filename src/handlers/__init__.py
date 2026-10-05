#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# AXIOMA v0.0.8 — Handlers Module
# ═══════════════════════════════════════════════════════════════
# Ruta: /ruta/a/axioma/src/handlers/__init__.py
# Autor: SaintWick
# ═══════════════════════════════════════════════════════════════

"""
Handlers para procesamiento directo de datos sin LLM.

Módulos:
- data_handler: Weather, finance, time, news
"""

from src.handlers.data_handler import DataHandler

__all__ = ["DataHandler"]
