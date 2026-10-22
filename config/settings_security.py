#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_security.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_security.py
#
# 2026-10-13: dominio **SEGURIDAD ... FEEDBACK LOOP** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
from typing import Optional

from pydantic import BaseModel, Field


class SettingsSecurity(BaseModel):
    """Dominio SEGURIDAD ... FEEDBACK LOOP extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === SEGURIDAD ===
    # ═══════════════════════════════════════════════════════════
    secret_key: str = ""
    backup_encryption_key: Optional[str] = None
    session_timeout: int = Field(3600, ge=300, le=86400)
    # ✅ AUD-CFG-001 (2026-08-27): setting referenciado en gateway.py:299
    # con fallback 50 — ahora definido explícitamente (antes no existía).
    max_context_messages: int = Field(50, ge=10, le=500)

    # ✅ v0.6.9c (M1): retención de memoria — poda en background de sesiones
    # más viejas que N días (0 = off). Sesiones sin fecha en el id no se tocan.
    memory_retention_days: int = Field(default=0, ge=0, le=365)
    # ✅ v0.6.9r (3a): búsqueda híbrida BM25. MEDIDO (gold real, bge-m3):
    #   vectorial solo MRR 0.690 · +BM25 0.363 · +reranker 0.294
    # → en documentos reales el vectorial gana; BM25/reranker lo degradan.
    # Por eso quedan OPT-IN (OFF por defecto); activar solo por corpus
    # donde el vectorial falle (p. ej. snippets muy cortos).
    hybrid_bm25_enabled: bool = Field(default=False)
    hybrid_bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    # ✅ v0.6.9t: re-ranking del retrieval (cerrar híbrido). Default OFF.
    reranker_enabled: bool = Field(default=False)
    reranker_topk: int = Field(default=20, ge=5, le=200)
    # ✅ v0.6.9r (2): PDF escaneado → OCR (rasterizar páginas + Tesseract).
    pdf_scan_ocr_enabled: bool = Field(default=True)
    pdf_scan_max_pages: int = Field(default=3, ge=1, le=20)

    # ═══════════════════════════════════════════════════════════
    # === GEOLOCALIZACIÓN ===
    # ═══════════════════════════════════════════════════════════
    default_location: str = Field(default="Córdoba, Argentina")
    default_timezone: str = Field(default="America/Argentina/Cordoba")
    enable_ip_geolocation: bool = Field(default=True)
    geolocator_cache_ttl: int = Field(default=3600)

    # ═══════════════════════════════════════════════════════════
    # === FEEDBACK LOOP — SALIDA A PARÁMETROS (plan_fiabilidad P1.3) ===
    # ═══════════════════════════════════════════════════════════
    # ✅ ISSUE-069 (2026-09-14): ParameterOptimizer.get_optimized_parameter()
    # calculaba ajustes de temperature/timeout/threshold que nadie consumía
    # — el loop de aprendizaje medía y no actuaba. Este flag habilita que
    # LLMConfig.get_model_for_task() use el valor aprendido en vez del
    # configurado en models.yaml. Default False (R8: nada se activa sin
    # medir con datos reales primero vía interaction_metrics/P1.1).
    feedback_temperature_tuning_enabled: bool = Field(default=False)
