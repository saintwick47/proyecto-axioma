#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_evaluator.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_evaluator.py
#
# 2026-10-13: dominio **EVALUATOR-OPTIMIZER ... CONTRATO DE CÓDIGO** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================

from pydantic import BaseModel, Field


class SettingsEvaluator(BaseModel):
    """Dominio EVALUATOR-OPTIMIZER ... CONTRATO DE CÓDIGO extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === EVALUATOR-OPTIMIZER (FASE 1) ===
    # ═══════════════════════════════════════════════════════════
    enable_optimizer_loop: bool = Field(default=True)
    max_optimizer_attempts: int = Field(default=3, ge=1, le=3)
    optimizer_global_timeout: int = Field(default=120, ge=60, le=180)
    quality_threshold_default: float = Field(default=6.5, ge=4.0, le=8.0)
    quality_threshold_code: float = Field(default=7.0, ge=5.0, le=9.0)
    quality_threshold_research: float = Field(default=6.5, ge=4.0, le=8.0)
    quality_threshold_quick: float = Field(default=5.0, ge=3.0, le=7.0)
    quality_threshold_validation: float = Field(default=6.5, ge=4.0, le=8.0)
    stop_on_score_degradation: bool = Field(default=True)
    degradation_threshold_percent: float = Field(default=20.0)
    degradation_consecutive_count: int = Field(default=2)
    require_external_validation_for_code: bool = Field(default=True)
    external_validation_timeout: int = Field(default=5)
    enable_optimizer_monitoring: bool = Field(default=True)
    monitoring_window_size: int = Field(default=100)
    alert_on_degradation_percent: float = Field(default=15.0)
    code_quality_level_1: float = Field(default=0.50, ge=0.40, le=0.80)
    code_quality_level_2: float = Field(default=0.55, ge=0.40, le=0.90)
    code_quality_level_3: float = Field(default=0.50, ge=0.30, le=1.0)
    code_quality_level_4: float = Field(default=0.45, ge=0.20, le=1.0)

    # ═══════════════════════════════════════════════════════════
    # === LÍMITES DE CÓDIGO ===
    # ═══════════════════════════════════════════════════════════
    code_max_lines_recommended: int = Field(default=100, ge=50, le=150)
    code_max_lines_absolute: int = Field(default=200, ge=150, le=300)
    chunked_generation_enabled: bool = Field(default=True)
    chunk_max_tokens: int = Field(default=1800, ge=500, le=4096)
    chunk_overlap_lines: int = Field(default=15, ge=5, le=50)
    chunk_max_iterations: int = Field(default=20, ge=3, le=50)
    # ✅ v0.7.5 (ISSUE-074/A3b): TOPE TOTAL de tokens por archivo generado.
    # Antes el único límite era `chunk_max_iterations × chunk_max_tokens` =
    # 20 × 1800 = 36.000 tokens por archivo ⇒ a 7,9 tok/s (coder medido) son
    # **76 min** de generación (con 20 timeouts de 1200 s disponibles): no había
    # tope real. 8192 tokens ≈ 1037 s: cubre el p95 de `code_generation`
    # (578 s) y 2× el máximo observado (879 s) en `data/traces.db`.
    chunk_total_token_budget: int = Field(default=8192, ge=1000, le=32768)
    chunked_lines_threshold: int = Field(default=150, ge=50, le=500)

    # ═══════════════════════════════════════════════════════════
    # === CONTRATO DE CÓDIGO VERIFICABLE (FASE 1) ===
    # ═══════════════════════════════════════════════════════════
    code_contract_enabled: bool = Field(
        default=True,
        description="Exige secciones de contrato (código/dependencias/tests/comando) en la salida de CoderAgent"
    )
    code_require_tests: bool = Field(default=False)
    code_require_dependencies: bool = Field(default=False)
    code_require_commands: bool = Field(default=False)
    code_require_expected_output: bool = Field(default=False)
    code_contract_max_retries: int = Field(default=3, ge=1, le=5)
    code_generation_max_tokens: int = Field(
        default=2048, ge=512, le=4096,
        description="Techo de tokens para generación de código por contrato (Ryzen 5 8500G). "
                     "Reemplaza el 1024 hardcodeado en MAX_TOKENS_BY_TASK['code_generation'] (coder.py)"
    )
    code_validation_timeout: int = Field(
        default=15, ge=5, le=60,
        description="Timeout en segundos para la validación dinámica en sandbox (Fase 3)"
    )
