#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_memory.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_memory.py
#
# 2026-10-13: dominio **COORDINACIÓN Y MEMORIA ... MONITOREO Y ALERTAS** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================
from typing import List, Union

from pydantic import BaseModel, Field, field_validator


class SettingsMemory(BaseModel):
    """Dominio COORDINACIÓN Y MEMORIA ... MONITOREO Y ALERTAS extraido de `Settings`."""

    # ═══════════════════════════════════════════════════════════
    # === COORDINACIÓN Y MEMORIA — FASE 4 (plan_harness) ===
    # MemoryGuard (src/core/memory_guard.py): evita OOM al cargar
    # modelos. allow_parallel_models habilita el modo paralelo futuro
    # (varios modelos residentes) SOLO si MemoryGuard.can_coexist()
    # confirma que el hardware lo aguanta — hoy default False (un
    # modelo por vez vía ModelSwap).
    # ═══════════════════════════════════════════════════════════
    allow_parallel_models: bool = Field(
        default=False,
        description="FASE 4: permitir varios modelos residentes si la memoria lo aguanta (futuro con más VRAM)",
    )
    memory_ram_warn_ratio: float = Field(
        default=0.85, ge=0.5, le=0.99,
        description="FASE 4: fracción de RAM que dispara advertencia antes de cargar un modelo",
    )
    memory_ram_hard_ratio: float = Field(
        default=0.95, ge=0.6, le=1.0,
        description="FASE 4: fracción de RAM que RECHAZA la carga de un modelo (fail-safe OOM)",
    )
    memory_headroom_gb: float = Field(
        default=1.5, ge=0.0, le=16.0,
        description="FASE 4: margen de seguridad de RAM (GB) sobre el requerimiento del modelo",
    )
    # ✅ CALIBRACIÓN (2026-08-27, re-calibrada 2026-08-29): factor de overhead
    # del footprint real del modelo cargado (KV cache + buffers de Ollama)
    # sobre required_gb de hardware_fit (size * 1.12). El benchmark
    # tools/benchmark_calibration.py mide el RSS real de Ollama (serve +
    # runner llama-server) y lo guarda en calibration_data.json
    # (memory_guard_tag.global_overhead_ratio). Re-calibrado en este hardware
    # contra RSS real: 1.10 (el 1.17 previo se calibró contra /api/ps size en
    # disco, que sobreestima la RAM real ~5-13%). Ajustar acá para que
    # MemoryGuard no subestime la RAM real (riesgo de OOM).
    memory_guard_overhead_ratio: float = Field(
        default=1.0, ge=1.0, le=3.0,
        description="FASE 4: factor de footprint real sobre required_gb (1.0 = KV incluido, calibrar con el benchmark)",
    )

    # ═══════════════════════════════════════════════════════════
    # === CAPA 2 — IDENTIDAD DE USUARIO (plan_memory, 2026-08-31) ===
    # ═══════════════════════════════════════════════════════════
    # El user_id aísla la memoria por usuario: cada usuario solo recupera SUS
    # mensajes (retrieval cross-sesión filtrado por user_id). El usuario del equipo es el
    # usuario root (admin); los usuarios futuros serán "user" (sin privilegios).
    # ✅ ISSUE-151 (2026-10-05, pedido del usuario): **sin usuario cocido**. Antes el valor por
    # defecto era el usuario del autor, así que quien instalaba AXIOMA entraba con ESA identidad (y
    # como root). Ahora arranca VACÍO: la primera vez AXIOMA pide **crear un usuario propio** y
    # recién después se puede usar. El rol por defecto también es `user` (no administrador).
    axioma_user_id: str = Field(
        default="",
        description=("Capa 2: identidad del usuario (aísla la memoria por usuario). Vacío = todavía "
                     "no hay usuario: AXIOMA pide crearlo antes de usarlo"),
    )
    axioma_user_role: str = Field(
        default="user",
        description="Capa 2: rol del usuario — root (admin) | user (normal, sin privilegios)",
    )
    cross_session_limit: int = Field(
        default=3, ge=0, le=20,
        description="Capa 2: máx. mensajes de OTRAS sesiones del mismo usuario a inyectar en el contexto",
    )
    cross_session_min_similarity: float = Field(
        default=0.75, ge=0.0, le=1.0,
        description=("Capa 2: umbral de similitud para el retrieval cross-sesión. "
                     "Medido (ISSUE-143): con 0,5 entraba material de temas ajenos; el "
                     "pedido del keylogger puntuaba 0,582 para un pedido de pcap"),
    )
    # ✅ ISSUE-143 (MEDIDO): el agente de CÓDIGO no recibe contexto de otras sesiones.
    # Interruptor para volver atrás sin tocar código (por defecto OFF = no inyecta).
    cross_session_en_codigo: bool = Field(
        default=False,
        description="Capa 2: inyectar contexto de OTRAS sesiones también en tareas de código",
    )


    # ═══════════════════════════════════════════════════════════
    # === EMBEDDING OPTIMIZER — v0.5.0 ===
    # ═══════════════════════════════════════════════════════════
    embedding_cache_max_size: int = Field(default=1000, ge=100, le=10000)
    embedding_cache_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    query_cache_max_size: int = Field(default=500, ge=100, le=5000)
    query_cache_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    embedding_batch_size: int = Field(default=32, ge=8, le=128)
    embedding_max_workers: int = Field(default=4, ge=1, le=16)
    similarity_percentile: int = Field(default=75, ge=50, le=95)
    similarity_min_threshold: float = Field(default=0.5, ge=0.3, le=0.8)

    # ═══════════════════════════════════════════════════════════
    # === MULTI-QUERY LIGERO — v0.6.8 ===
    # Mejora de recall sin LLM (ver src/memory/multi_query.py).
    # Expansión determinística + UN batch BGE-M3 + fusión RRF.
    # Adaptado a CPU-only: costo extra +0-200ms por query.
    # ═══════════════════════════════════════════════════════════
    multi_query_enabled: bool = Field(
        default=True,
        description="Habilita multi-query ligero en MemoryGateway.search_semantic()"
    )
    multi_query_max_variants: int = Field(
        default=4, ge=1, le=6,
        description="Máximo de variantes de query (incluye la original)"
    )
    multi_query_min_length: int = Field(
        default=30, ge=10, le=200,
        description="Longitud mínima de query para activar multi-query "
                    "(frases cortas → embeddings poco discriminativos, "
                    "mismo criterio que cache_l3.MIN_QUERY_LENGTH_FOR_SEMANTIC_MATCH)"
    )
    multi_query_rrf_k: int = Field(
        default=60, ge=10, le=200,
        description="Constante k de Reciprocal Rank Fusion"
    )
    multi_query_store_l3_variants: bool = Field(
        default=True,
        description="Guarda las variantes expandidas en cache_l3.set() "
                    "(costo amortizado en escritura, amplía recall futuro)"
    )

    # ═══════════════════════════════════════════════════════════
    # === MONITOREO Y ALERTAS — v0.8.0 ===
    # ═══════════════════════════════════════════════════════════
    enable_alert_manager: bool = Field(default=True)
    alert_check_interval_seconds: int = Field(default=30, ge=10, le=300)
    alert_log_level: str = Field(default="WARNING")
    alert_error_rate_warning: float = Field(default=0.15, ge=0.05, le=0.50)
    alert_error_rate_critical: float = Field(default=0.30, ge=0.10, le=0.80)
    alert_p95_warning_ms: float = Field(default=8000.0, ge=1000.0, le=30000.0)
    alert_p95_critical_ms: float = Field(default=15000.0, ge=5000.0, le=60000.0)
