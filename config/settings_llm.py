#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_llm.py  (REFACTORIZACION POR PARTES, pieza 6b)
# Ruta: config/settings_llm.py
#
# 2026-10-13: dominio MODELOS LLM extraido de `config/settings.py`, sin cambios de comportamiento.
# Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase comun no recolecta
# anotaciones como campos; el mixin de METODOS que ya existia, `settings_computations.py`, es una
# clase comun a proposito).
#
#   Que se movio desde settings.py:
#     - MODELOS LLM                       (12 campos)
#     - CACHES DE RESPUESTA                (5 campos + validador ensure_ollama_host_scheme)
#     - CONCURRENCIA LLM                   (1 campo)
#     - TIMEOUTS PROGRESIVOS              (13 campos + validador enforce_standard_timeout_cap)
#     - VERIFICACION DE MODELOS AL INICIO  (6 campos)
#
#   `Settings(BaseSettings, SettingsLLM, SettingsVision, SettingsComputations)` hereda este mixin,
#   asi la API publica NO cambia: mismos nombres, anotaciones y defaults.
# ===============================================================
import logging
from typing import List, Literal, Union

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from src.utils.degradation import log_degraded

logger = logging.getLogger(__name__)


class SettingsLLM(BaseModel):
    """Dominio de MODELOS LLM: modelos, caches de respuesta, concurrencia, timeouts y verificacion.

    Extraido de `Settings` (pieza 6b). Solo CAMPOS y sus validadores de campo.
    """

    # ═══════════════════════════════════════════════════════════
    # === MODELOS LLM ===
    # ═══════════════════════════════════════════════════════════
    # ✅ FIX: modelos migrados a qwen3:8b (chat), qwen2.5-coder:7b (código), qwen3-vl:4b (visión)
    # desinstalados. qwen3:8b cubre default/chat.
    # ✅ FIX (benchmark_code_results.md, esta sesión): llm_code_model pasa a
    # qwen2.5-coder:7b — venció a qwen3:8b y deepseek-r1:8b en calidad de
    # código (Docstrings/Types/Quality; el ranking "Overall"/Exec-Pass del
    # reporte no es confiable, ver nota más abajo).
    llm_default_model: str = "qwen3:8b"
    llm_code_model: str = "qwen2.5-coder:7b"
    # ✅ FIX: gemma4:e4b desinstalado (nunca funcionó bien, muy pesado) —
    # default actualizado a qwen3-vl:4b, candidato instalado en evaluación.
    llm_vision_model: str = "qwen3-vl:4b"
    llm_vision_fallback_model: str = ""  # Sin fallback — un solo VLM activo a la vez
    llm_embed_model: str = "BAAI/bge-m3"
    # ✅ FIX: deepseek-r1:8b descartado (benchmark_code_results.md). A probar:
    # qwen2.5-coder:7b — distinto de llm_default_model (resuelve la falta de
    # resiliencia para el rol chat), aunque queda igual a llm_code_model (si
    # el circuit breaker abre por una falla de CÓDIGO específicamente, cae en
    # el mismo modelo que falló — sin resolver ese caso puntual). También pasa
    # a ser el modelo que usa ValidatorAgent para juzgar — un especialista de
    # código como juez general es una apuesta a validar con uso real.
    llm_fallback_1: str = "qwen2.5-coder:7b"
    llm_fallback_2: str = ""  # Desactivado — eliminado en fase0_stabilizer
    # ✅ ISSUE-142 (2026-10-04, MEDIDO): **respaldo por NEGATIVA del modelo de código**.
    # El modelo censurado se niega en prosa para pedidos legítimos (medido: 3 de 4
    # pedidos de laboratorio del usuario, en 2-5 s: "Lo siento, pero no puedo
    # ayudarte con eso."). Ante una negativa, el sistema **reintenta una vez** con este
    # modelo (el abliterado, que sí genera esos casos) y sigue con la validación
    # normal. Como sólo entra un modelo a la vez en RAM, el reintento implica descargar
    # el primario y cargar el de respaldo (~6 s medidos, `ISSUE-141`).
    # Si el modelo NO está instalado, no se reintenta y se avisa: se verifica contra
    # Ollama con `list_models()`, no se confía en la configuración.
    llm_code_model_fallback: str = Field(
        default="huihui_ai/qwen2.5-coder-abliterate:7b",
        description="Modelo de respaldo si el de código se NIEGA a generar (vacío = sin respaldo)",
    )
    llm_judge_model: str = ""  # Desactivado — juez externo eliminado en v0.6.2
    # ✅ v0.6.9h (juez = chat, MEDIDO): modelo del VALIDADOR factual.
    # Medición 2026-09-06: qwen3:8b como juez detecta igual (Francia/Roma FAIL,
    # 2+2 PASS) pero tarda 2× (68-99 s vs 22-50 s) y con timeout 90 s falló
    # 2/4 por timeout → DEFAULT se mantiene en llm_fallback_1 (coder).
    # Opt-in para probar chat-juez: VALIDATOR_MODEL=qwen3:8b + subir timeout.
    validator_model: str = Field(
        default="",
        description="Modelo del validador factual (vacío = llm_fallback_1)",
    )
    # ✅ v0.6.9i (clasificador kNN, MEDIDO): kNN bge-m3 antes del LLM.
    # Medición: 14/20 (70%) con conf 0.72-0.90 y errores en tareas código
    # cercanas (cola→autonomous, optimizá→data_analysis) → aceptarlo degrada
    # el ruteo. Default OFF: el LLM sigue primario (calidad intacta).
    classifier_knn_enabled: bool = Field(
        default=False,
        description="kNN embeddings antes del LLM (experimental, default OFF)",
    )
    # ✅ MEDIDO (2026-10-03): clasificación por LLM como PASO de ruteo.
    # Antes de este interruptor la llamada existía siempre y **siempre se pasaba
    # del límite de 15 s** (el modelo gastaba 68,5 s "pensando"): se perdían 15 s
    # por consulta y encima no clasificaba nada (todo caía a palabras clave). Al
    # arreglarla (`think: False`, responde en 3-6 s) y dejarla decidir, el ruteo
    # EMPEORA en preguntas conceptuales: "¿Qué es un índice en una base de datos?"
    # → `data_analysis` ⇒ 203,9 s y 829 tokens de informe (contra 79,2 s del chat).
    # Default OFF = el comportamiento real de hoy (palabras clave) pero sin perder
    # los 15 s; al encenderlo sólo se aceptan categorías de CÓDIGO
    # (`LLM_CLASSIFY_ACEPTADOS` en `src/router/classifier.py`).
    classifier_llm_enabled: bool = Field(
        default=False,
        description="Clasificación por LLM como paso de ruteo (OFF: palabras clave, sin los 15 s perdidos)",
    )
    # ═══════════════════════════════════════════════════════════
    # === CACHÉS DE RESPUESTA (MEDIDO 2026-10-03: contaminaban) ===
    # El usuario veía respuestas "contaminadas" por la caché. Medido:
    #  · La clave de la caché de respuesta en RAM era **sólo el texto de la
    #    pregunta** ⇒ con el historial inyectado desde ISSUE-130, el mismo texto en
    #    OTRA conversación devolvía la respuesta vieja (TTL de `general_chat`: 600 s).
    #  · La caché semántica L3 usaba umbral **0.60**, y medido con bge-m3 dos
    #    preguntas DISTINTAS puntúan 0.603 y 0.635 ("¿Qué es un índice en una base de
    #    datos?" ↔ "¿Cómo funciona una base de datos relacional?") ⇒ servía la
    #    respuesta de una pregunta para la otra. Un parafraseo real puntúa 0.933.
    # Por eso: umbral 0.85 (arriba del falso positivo medido y debajo del parafraseo),
    # la clave incluye historial + modelo, y la L3 sólo se usa en conversaciones SIN
    # historial (una respuesta contextual nunca debe salir de un atajo semántico).
    # ═══════════════════════════════════════════════════════════
    cache_respuestas_enabled: bool = Field(
        default=True,
        description="Caché de respuestas en RAM (clave: pregunta + historial + modelo)",
    )
    cache_semantica_enabled: bool = Field(
        default=True,
        description="Caché semántica L3 (sólo sin historial, con umbral medido)",
    )
    cache_semantica_umbral: float = Field(
        default=0.85, ge=0.5, le=0.99,
        description="Similitud mínima de la caché semántica (0.60 daba falsos positivos medidos)",
    )
    default_temperature: float = Field(0.7, ge=0.0, le=1.0)
    ollama_host: str = "http://127.0.0.1:11434"
    # ✅ 2026-10-22: campo REAL para el keep-alive. Antes no existía: el embed leía con un
    # `getattr(settings, "ollama_keep_alive", "30m")` (siempre "30m") y los otros 4 sitios
    # usaban "30m" para el modelo por defecto y "5m" para el resto. Ahora los 5 leen de acá
    # (los modelos que no son el default pasan de 5m a 30m: aprobado y documentado en el CHANGELOG).
    ollama_keep_alive: str = "30m"

    @field_validator("ollama_host", mode="before")
    @classmethod
    def ensure_ollama_host_scheme(cls, v: Union[str, None]) -> str:
        """
        OLLAMA_HOST es la misma variable de entorno que usa Ollama para su
        propio bind (ej. "127.0.0.1:11434", sin esquema). pydantic-settings
        la hereda tal cual para este campo — sin esquema, httpx rechaza la
        URL. Anteponer http:// si falta.
        """
        if not v:
            return "http://127.0.0.1:11434"
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            return f"http://{v}"
        return v

    # ═══════════════════════════════════════════════════════════
    # === CONCURRENCIA LLM — v0.5.2 ===
    # ═══════════════════════════════════════════════════════════
    ollama_max_concurrent_requests: int = Field(
        default=2, ge=1, le=4,
        description="Máximo de requests LLM concurrentes (limita semáforos del Dispatcher)"
    )

    # ═══════════════════════════════════════════════════════════
    # === TIMEOUTS PROGRESIVOS ===
    # ═══════════════════════════════════════════════════════════
    ollama_timeout_standard: int = Field(45, ge=30, le=90)
    ollama_timeout_first_load: int = Field(65, ge=25, le=120)
    # ✅ v0.6.8t: Chat (agent loop) — qwen3:8b en CPU puede tardar 2-3 min
    # (verificado: document_analysis con 420s tardó 139s; el chat con 60s
    # hardcodeado hacía timeout). Configurable por usuario.
    ollama_timeout_chat: int = Field(180, ge=60, le=600)
    # ✅ v0.1.6b: Aumentado a 150s para margen de seguridad (gemma4 en CPU ~111s en 1920px)
    ollama_timeout_vision: int = Field(150, ge=60, le=300)  # ✅ v0.1.6b: gemma4:e4b en CPU ~111s en resolución original
    ollama_timeout_vision_fallback: int = Field(150, ge=60, le=300)  # ✅ v0.1.6b: Mantenido por compatibilidad
    ollama_timeout_embed: int = Field(35, ge=10, le=60)
    search_timeout: int = Field(30, ge=10, le=120)
    ollama_timeout_optimizer: int = Field(90, ge=60, le=180)
    ollama_timeout_code_level_1: int = Field(180, ge=120, le=240)
    ollama_timeout_code_level_2: int = Field(420, ge=300, le=600)
    ollama_timeout_code_level_3: int = Field(900, ge=600, le=1200)
    ollama_timeout_code_level_4: int = Field(1200, ge=900, le=1800)
    ollama_timeout_code: int = Field(420, ge=180, le=900)

    @field_validator("ollama_timeout_standard", mode="before")
    @classmethod
    def enforce_standard_timeout_cap(cls, v: Union[int, str, None], info: ValidationInfo) -> int:
        """Valida timeout estándar con rango flexible."""
        if v is None:
            return 45
        if isinstance(v, str):
            try:
                v = int(v.strip())
            except (ValueError, AttributeError) as e:
                log_degraded(logger, e, "config/settings.py:enforce_standard_timeout_cap")
                return 45
        return max(30, min(v, 90))

    # ═══════════════════════════════════════════════════════════
    # === VERIFICACIÓN DE MODELOS AL INICIO ===
    # ═══════════════════════════════════════════════════════════
    verify_models_on_startup: bool = Field(default=True)
    model_check_timeout: int = Field(5, ge=1, le=15)
    model_check_retries: int = Field(2, ge=1, le=5)
    model_check_retry_delay: float = Field(2.0, ge=0.5, le=10.0)
    # ✅ FIX: lista actualizada. qwen2.5-coder:7b reemplaza a qwen3:8b en código
    # (benchmark). deepseek-r1:8b descartado como fallback — ver llm_fallback_1.
    required_models_on_startup: List[str] = Field(default_factory=lambda: [
        "qwen3:8b",           # Chat/default (ver llm_default_model) — también fallback provisorio
        "qwen2.5-coder:7b",   # Código (ver llm_code_model)
        "qwen3-vl:4b",        # Visión (ver llm_vision_model)
        "BAAI/bge-m3",
    ])
    startup_behavior_on_missing_model: Literal["warn", "error", "continue"] = "warn"
