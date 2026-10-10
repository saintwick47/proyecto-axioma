#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# settings.py - Configuración Central con Validaciones
# Ruta: /ruta/a/axioma/config/settings.py
# Autor: SaintWick
# Versión: 0.1.6b
#
# Define la configuración de AXIOMA usando Pydantic Settings.
# Incluye campos para modelos LLM, timeouts, rutas, APIs externas,
# agentes, seguridad, geolocalización, evaluador-optimizador,
# límites de código, MCP, sandbox, embedding, monitoreo, captura
# de pantalla y modo Jarvis. Los métodos computacionales están
# extraídos en settings_computations.py como mixin.
# ═══════════════════════════════════════════════════════════════
from pathlib import Path
from typing import Optional, List, Literal, Dict, Any, Union
import logging
import subprocess
import os
from pydantic import Field, field_validator, model_validator, ValidationInfo
from pydantic import AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict
from src.utils.degradation import log_degraded

# ✅ v0.6.9m: raíz portable (CI/hardware) — antes hardcodeada a
# /ruta/a/axioma → en GitHub Actions intentaba crear
# /usuario (sin permiso) → PermissionError al importar settings.

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[1]

from .settings_computations import SettingsComputations


class Settings(BaseSettings, SettingsComputations):
    """Configuración central con validaciones estrictas para AXIOMA."""

    model_config = SettingsConfigDict(
        # ✅ ISSUE-147 (MEDIDO): `env_file=".env"` es relativo a la carpeta de trabajo, así
        # que arrancando desde otra carpeta (contenedor con otro WORKDIR, servicio sin
        # WorkingDirectory) la configuración del usuario NO se cargaba y no avisaba nada.
        # Ahora el `.env` es el del proyecto, sin importar desde dónde se arranque.
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

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

    # ═══════════════════════════════════════════════════════════
    # === RUTAS (absolutas, resueltas) ===
    # ═══════════════════════════════════════════════════════════
    base_dir: Path = Field(default_factory=lambda: _REPO_ROOT)
    data_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "data")
    logs_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "logs")
    config_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "config")
    cache_dir: Path = Field(default_factory=lambda: _REPO_ROOT / "cache")

    @field_validator("base_dir", "data_dir", "logs_dir", "config_dir", "cache_dir", mode="before")
    @classmethod
    def resolve_absolute_path(cls, v: Union[str, Path, None]) -> Path:
        """Convierte rutas relativas a absolutas."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return _REPO_ROOT
        path = Path(v)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        return path.resolve() if path.exists() else path.absolute()

    @model_validator(mode="after")
    def ensure_directories(self) -> "Settings":
        """Crea directorios críticos si no existen."""
        for dir_path in [self.data_dir, self.logs_dir, self.cache_dir]:
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                    logging.info(f"Directorio creado: {dir_path}")
                except PermissionError:
                    logging.warning(f"Sin permisos para crear: {dir_path}")
        return self

    # ═══════════════════════════════════════════════════════════
    # === MEMORIA VECTORIAL ===
    # ═══════════════════════════════════════════════════════════
    vector_db_path: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "memory" / "lancedb")
    db_path: Path = Field(default_factory=lambda: _REPO_ROOT / "data" / "memory" / "axioma.db")

    @field_validator("vector_db_path", "db_path", mode="before")
    @classmethod
    def resolve_db_paths(cls, v: Union[str, Path, None]) -> Path:
        """Resuelve y crea directorio para DBs."""
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return _REPO_ROOT / "data" / "memory"
        path = Path(v)
        if path.suffix == '.db':
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
        return path.resolve() if path.exists() else path.absolute()

    # ═══════════════════════════════════════════════════════════
    # === SERVIDOR WEB ===
    # ═══════════════════════════════════════════════════════════
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1024, le=65535)
    debug: bool = False
    reload: bool = False

    # ═══════════════════════════════════════════════════════════
    # === APIs EXTERNAS ===
    # ═══════════════════════════════════════════════════════════
    tavily_api_key: Optional[str] = None
    serper_api_key: Optional[str] = None
    serpapi_api_key: Optional[str] = None  # ✅ v0.6.9q: SerpAPI (serpapi.com)
    news_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    search_engine_id: Optional[str] = None

    # ═══════════════════════════════════════════════════════════
    # === AGENTES ===
    # ═══════════════════════════════════════════════════════════
    max_retries: int = Field(2, ge=0, le=5)
    # ✅ v0.7.5 (RAC): knobs de la Revisión Axioma Completa. ANTES se leían con
    # `os.getenv("AXIOMA_RAC_MAX_MINUTES")`, que **nunca funcionó**: `.env` lo lee
    # pydantic (no `load_dotenv()`), así que el valor del archivo se ignoraba y el
    # informe pedía "ajustá AXIOMA_RAC_MAX_MINUTES" sin efecto posible. Ahora son
    # campos de settings (y se aceptan los dos nombres).
    rac_max_minutes: int = Field(default=45, ge=1, le=1440, validation_alias=AliasChoices(
        "AXIOMA_RAC_MAX_MINUTES", "RAC_MAX_MINUTES"))
    rac_timeout: int = Field(default=600, ge=60, le=3600, validation_alias=AliasChoices(
        "AXIOMA_RAC_TIMEOUT", "RAC_TIMEOUT"))
    rac_batch_pause: int = Field(default=45, ge=0, le=600, validation_alias=AliasChoices(
        "AXIOMA_RAC_BATCH_PAUSE", "RAC_BATCH_PAUSE"))
    rac_cooldown: int = Field(default=3, ge=0, le=60, validation_alias=AliasChoices(
        "AXIOMA_RAC_COOLDOWN", "RAC_COOLDOWN"))
    # ✅ 2026-09-29 (ISSUE-115): RAC INCREMENTAL. Medido: el scope `completa` son 213
    # archivos y **577 llamadas** al LLM por ciclo (≈6,8 h de prefill + generación),
    # pero en un ciclo nuevo se re-analizaba TODO aunque no hubiera cambiado nada.
    # Con esto, un archivo cuyo CONTENIDO es idéntico al ya analizado (hash SHA-1, no
    # `mtime`) **no se vuelve a enviar al LLM** y su análisis anterior se reutiliza en
    # el informe (marcado como reutilizado) ⇒ la calidad no baja: lo que se reusa es
    # exactamente el análisis del mismo código, el mismo prompt y el mismo modelo.
    # Poner `false` fuerza un re-análisis completo (se pierde el ahorro).
    rac_solo_cambiados: bool = Field(default=True, validation_alias=AliasChoices(
        "AXIOMA_RAC_SOLO_CAMBIADOS", "RAC_SOLO_CAMBIADOS"))

    # ✅ 2026-10-01 (ISSUE-122): VOZ del TTS configurable. Antes estaba hardcodeada en
    # `TTSConfig(voice="es-ES-AlvaroNeural")` (español de ESPAÑA) ⇒ para un usuario
    # argentino sonaba "de afuera". Con edge-tts las voces neurales rioplatenses
    # (`es-AR-TomasNeural`, `es-AR-ElenaNeural`, `es-UY-ValentinaNeural`) son mucho más
    # naturales para él. Se elige con `AXIOMA_TTS_VOICE` en `.env` sin tocar código.
    tts_voice: str = Field(default="es-AR-TomasNeural", validation_alias=AliasChoices(
        "AXIOMA_TTS_VOICE", "TTS_VOICE"))
    tts_fallback_voice: str = Field(default="es-AR-ElenaNeural", validation_alias=AliasChoices(
        "AXIOMA_TTS_FALLBACK_VOICE", "TTS_FALLBACK_VOICE"))
    # Prosodia: `-8%` habla un poco más pausado (más natural en notificaciones largas).
    tts_rate: str = Field(default="-5%", validation_alias=AliasChoices(
        "AXIOMA_TTS_RATE", "TTS_RATE"))
    tts_pitch: str = Field(default="+0Hz", validation_alias=AliasChoices(
        "AXIOMA_TTS_PITCH", "TTS_PITCH"))
    tts_volume: str = Field(default="+0%", validation_alias=AliasChoices(
        "AXIOMA_TTS_VOLUME", "TTS_VOLUME"))
    # ✅ 2026-10-01: voz OFFLINE de Piper (la que usa RAFAEL, el daemon). Medido:
    # `es_AR-daniela-high` (argentina, calidad high) carga en 0,9 s y sintetiza una
    # frase en 0,91 s en CPU, sin internet. Si está vacío, se usa el del YAML.
    piper_model: str = Field(default="data/piper/es_AR-daniela-high.onnx",
                             validation_alias=AliasChoices("AXIOMA_PIPER_MODEL", "PIPER_MODEL"))
    # ✅ 2026-10-01 (ISSUE-126): sensibilidad de la wake word. La detección es por
    # ENERGÍA (no hay modelo ML instalado: porcupine/openwakeword ausentes), así que
    # con 0.5 cualquier ruido fuerte la dispara. Subirla ⇒ MENOS falsos positivos
    # (cuesta más "despertarlo"); bajarla ⇒ más fácil pero más falsos.
    wake_sensitivity: float = Field(default=0.5, ge=0.1, le=3.0, validation_alias=AliasChoices(
        "AXIOMA_WAKE_SENSITIVITY", "WAKE_SENSITIVITY"))
    # ✅ v0.7.5 (ISSUE-099): apagar el "thinking" en el CHAT. Medido con
    # qwen3:8b (prompt real con fuentes, num_predict=431): sin `think` el modelo
    # gastó ~400 de 431 tokens PENSANDO ⇒ respuesta de 192 chars con
    # `done_reason=length` (y en la corrida del usuario, **respuesta vacía**).
    # Con `think: false`: respuesta completa de 1275 chars, `done_reason=stop` y
    # 54 s contra 87 s (1,6× más rápido). Se puede volver a activar con
    # CHAT_DISABLE_THINKING=false.
    chat_disable_thinking: bool = Field(default=True)
    validation_threshold: float = Field(0.7, ge=0.0, le=1.0)
    route_cache_ttl: int = Field(300, ge=60, le=3600)

    # ═══════════════════════════════════════════════════════════
    # === F3 OPT-IN: PIPELINE MULTI-PASO (/plan) ===
    # PLAN_ADAPTADO.md FASE 3 — NUNCA en el flujo normal por defecto
    # (multiplicaría latencia 2-4× en CPU). El comando explícito
    # "/plan <consulta>" funciona SIEMPRE; este flag activa además el
    # pipeline automático para consultas descomponibles (research/code).
    # ═══════════════════════════════════════════════════════════
    plan_pipeline_enabled: bool = Field(
        default=False,
        description="F3 opt-in: pipeline multi-paso automático para consultas complejas",
    )
    plan_max_steps: int = Field(default=2, ge=1, le=4)

    # ═══════════════════════════════════════════════════════════
    # === CALIDAD DE CÓDIGO (v0.6.9a) — lote 2, todo OFF ===
    # E: exigir TESTS automáticamente cuando la generación es código
    #     ejecutable (no explicación) — refuerza la capa dinámica real.
    # F: refinamiento dirigido por el validador en /plan: si el score final
    #     < plan_refine_min_score y la crítica es accionable, una 2ª pasada
    #     coder con esa crítica (coste +1-2 generaciones CPU).
    # ═══════════════════════════════════════════════════════════
    code_require_tests_auto: bool = Field(
        default=False,
        description="E: exigir tests en generaciones ejecutables (off por defecto)",
    )
    plan_refine_enabled: bool = Field(
        default=False,
        description="F: refinamiento dirigido por validador en /plan (off por defecto)",
    )
    plan_refine_min_score: float = Field(default=9.0, ge=5.0, le=10.0)

    # ═══════════════════════════════════════════════════════════
    # === A3: STREAMING DE TOKENS A LA UI (v0.6.8ss) ===
    # ✅ Fase 1 (2026-10-02, MEDIDO): ahora ON por defecto. Antes estaba OFF
    # "hasta probarlo en vivo"; se midió y la diferencia es de otra categoría:
    # sin flujo NO aparece nada hasta que termina la respuesta (**83-90 s** en
    # tres preguntas reales), con flujo el primer texto llega a los **5-21 s**
    # (prelectura del contexto) y después se completa solo. El tiempo total no
    # cambia (59-135 s según el largo), lo que cambia es la espera percibida.
    # Antes de encenderlo se cerraron los dos huecos del camino con flujo: no
    # respetaba `MAX_TOKENS_CHAT` (generaba de más: 1162 vs 589 caracteres para
    # la misma pregunta) y no reportaba tokens (las trazas quedaban en 0, lo que
    # ISSUE-082 había arreglado). Cualquier error → fallback a respuesta
    # completa (nunca rompe el chat).
    # ═══════════════════════════════════════════════════════════
    stream_chat_tokens: bool = Field(
        default=True,
        description="A3: streaming de tokens del chat a la UI (ON por defecto desde Fase 1, medido)",
    )

    # ✅ Fase 1 (ISSUE-134): brevedad por defecto en el chat. En CPU la espera es
    # proporcional a lo generado (~6-8 tokens/s), así que 1-3 oraciones por
    # defecto y "ampliá si te lo pido" es la palanca de velocidad más barata.
    # Interruptor propio para poder medirla A/B y revertirla sin tocar código.
    chat_respuestas_breves: bool = Field(
        default=True,
        description="Fase 1: pedir respuestas breves en el chat (menos tokens = menos espera)",
    )

    # ═══════════════════════════════════════════════════════════
    # === BENCHMARK ===
    # ═══════════════════════════════════════════════════════════
    benchmark_seed: int = Field(42, description="Seed fija")

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

    # ═══════════════════════════════════════════════════════════
    # === AGENT LOOP — v0.3.0 ===
    # ═══════════════════════════════════════════════════════════
    agent_loop_max_iterations: int = Field(default=10, ge=1, le=50)
    agent_loop_timeout_per_step: int = Field(default=30, ge=10, le=120)
    agent_loop_global_timeout: int = Field(default=180, ge=60, le=600)
    agent_loop_enable_reflection: bool = Field(default=True)
    agent_loop_quality_threshold: float = Field(default=0.80, ge=0.50, le=1.0)
    agent_loop_max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    max_reasoning_tokens: int = Field(
        default=2048, ge=512, le=4096,
        description="Máximo de tokens de razonamiento por iteración del AgentLoop (Ryzen 5 8500G)"
    )
    dynamic_context_threshold: float = Field(
        default=0.95, ge=0.85, le=0.99,
        description="Umbral dinámico de similitud para ContextWindowManager (floor=0.85)"
    )
    barge_in_timeout_ms: int = Field(
        default=50, ge=20, le=200,
        description="Timeout en ms para detección de barge-in en VoicePipeline"
    )

    # ═══════════════════════════════════════════════════════════
    # === MCP + TOOL ARCHITECTURE — v0.4.0 ===
    # ═══════════════════════════════════════════════════════════
    tool_permission_coder: int = Field(default=2, ge=0, le=4)
    tool_permission_researcher: int = Field(default=3, ge=0, le=4)
    tool_permission_analyst: int = Field(default=0, ge=0, le=4)
    tool_permission_validator: int = Field(default=0, ge=0, le=4)
    tool_permission_searcher: int = Field(default=3, ge=0, le=4)
    tool_permission_default: int = Field(default=2, ge=0, le=4)
    mcp_timeout: float = Field(default=30.0, ge=5.0, le=120.0)
    mcp_connect_timeout: float = Field(default=10.0, ge=3.0, le=30.0)
    mcp_max_reconnect_attempts: int = Field(default=3, ge=1, le=10)
    mcp_servers: List[str] = Field(default_factory=list)
    mcp_auto_connect: bool = Field(default=False)
    tool_executor_max_retries: int = Field(default=0, ge=0, le=3)
    tool_executor_retry_delay: float = Field(default=1.0, ge=0.5, le=5.0)

    # ═══════════════════════════════════════════════════════════
    # === SANDBOX EXECUTION — v0.4.1 ===
    # ═══════════════════════════════════════════════════════════
    sandbox_max_cpu_seconds: int = Field(default=60, ge=10, le=300)
    sandbox_max_memory_mb: int = Field(default=256, ge=64, le=2048)
    sandbox_max_output_bytes: int = Field(default=100000, ge=10000, le=1000000)
    sandbox_max_file_size_bytes: int = Field(default=10000000, ge=1000000, le=100000000)
    sandbox_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    sandbox_work_dir: Optional[str] = Field(default=None)
    sandbox_allowed_commands: List[str] = Field(default_factory=list)
    sandbox_blocked_commands: List[str] = Field(
        default_factory=lambda: [
            "rm -rf", "mkfs", "dd if=", ":(){ :|:& };:", "chmod -R 777",
            "> /dev/sda", "shutdown", "reboot", "halt", "poweroff",
            "sudo", "su ", "pkexec", "doas",
            "curl", "wget", "nc ", "netcat", "ssh", "scp", "sftp",
            "python", "python3", "pip", "pip3",
            "node", "npm", "ruby", "gem",
            "docker", "kubectl", "podman", "containerd",
        ],
    )
    sandbox_blocked_env_vars: List[str] = Field(
        default_factory=lambda: [
            "AWS_SECRET", "AWS_ACCESS_KEY", "DATABASE_URL",
            "PRIVATE_KEY", "SECRET_KEY", "API_KEY",
            "PASSWORD", "TOKEN", "CREDENTIAL",
        ],
    )
    sandbox_mode: Literal["strict", "relaxed", "disabled"] = Field(default="strict")
    sandbox_enable_network: bool = Field(default=False)
    sandbox_enable_file_write: bool = Field(default=False)

    # ═══════════════════════════════════════════════════════════
    # === VRAM LOCK — timeout de espera (AUD-FT-001) ===
    # Piso para esperar la liberación del candado de VRAM (RAC) antes
    # de un swap. Config-driven (antes era un literal 300.0 hardcodeado
    # en model_swap.py::_check_vram_lock).
    # ═══════════════════════════════════════════════════════════
    vram_lock_wait_timeout: float = Field(
        default=300.0, ge=60.0, le=1800.0,
        description="Piso (segundos) de espera por el candado de VRAM antes de un swap de modelo",
    )

    # ═══════════════════════════════════════════════════════════
    # === RUNTIME MODE — FASE 1 (plan_harness) ===
    # Modo de ejecución del sistema: acota el toolset disponible por
    # sesión vía RuntimeMode (src/domain/types.py). STANDARD = toolset
    # completo (default). Ver ToolExecutor paso 2.5 y
    # ToolRegistry.list_for_mode().
    # ═══════════════════════════════════════════════════════════
    runtime_mode: Literal["standard", "minimal", "safe_mode", "code_mode"] = Field(
        default="standard",
        description="Modo de ejecución del sistema (standard|minimal|safe_mode|code_mode)",
    )

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

    @field_validator("sandbox_blocked_commands", mode="before")
    @classmethod
    def validate_blocked_commands(cls, v: Union[List[str], str, None]) -> List[str]:
        """Valida que la lista de comandos bloqueados sea una lista de strings."""
        if v is None:
            return []
        if isinstance(v, str):
            return [cmd.strip() for cmd in v.split(",") if cmd.strip()]
        return [str(cmd).strip() for cmd in v if str(cmd).strip()]

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

    # ═══════════════════════════════════════════════════════════
    # === SCREEN CAPTURE / VISION — v0.3.6 ===
    # ═══════════════════════════════════════════════════════════
    enable_screen_capture: bool = Field(
        default=True,
        description="Habilita ScreenCaptureEngine para análisis visual"
    )
    vision_capture_backend: str = Field(
        default="auto",
        description="Backend de captura: auto | grim | scrot | import_magick | pil"
    )
    # ✅ v0.1.6b: RESTAURADO a resolución ORIGINAL (3840×2160 para soportar 4K/HiDPI)
    # El resize a 800px destruía legibilidad de texto en IDEs, múltiples monitores, HiDPI
    # gemma4:e4b probado exitosamente en 1920×1080 (111s, 1004 tokens) — resolución nativa funciona
    vision_capture_max_width: int = Field(
        default=3840, ge=640, le=7680,
        description="Ancho máximo del frame enviado al VisionModel (3840 soporta 4K nativo, resize deshabilitado)"
    )
    vision_capture_max_height: int = Field(
        default=2160, ge=480, le=4320,
        description="Alto máximo del frame enviado al VisionModel (2160 soporta 4K nativo, resize deshabilitado)"
    )
    vision_jpeg_quality: int = Field(
        default=82, ge=50, le=95,
        description="Calidad JPEG de compresión del frame (balance calidad/tokens)"
    )
    vision_context_hint_enabled: bool = Field(
        default=True,
        description="Infiere contexto desde la ventana activa (web/code/document/image)"
    )
    vision_response_cache_ttl: int = Field(
        default=30, ge=10, le=300,
        description="TTL del cache de respuestas del VisionAgent (segundos)"
    )
    # ✅ v0.6.9p (Fase 1): robustez del canal de imágenes del chat
    vision_num_predict: int = Field(
        default=512, ge=64, le=2048,
        description="Tope de tokens de salida por generación VLM (evita "
                    "generaciones desbocadas que colgaban hasta el timeout)"
    )
    vision_preprocess_enabled: bool = Field(
        default=True,
        description="Preprocesado adaptativo antes del VLM: autocontraste si "
                    "contraste bajo, upscale 2x si la imagen es chica"
    )
    # ✅ v0.6.9p (Fase 2): OCR híbrido con Tesseract local
    vision_ocr_enabled: bool = Field(
        default=True,
        description="Usa Tesseract local como segunda opinión cuando la "
                    "instrucción pide leer/transcribir texto"
    )
    vision_ocr_min_conf: float = Field(
        default=60.0, ge=0.0, le=100.0,
        description="Confianza mínima de Tesseract (0-100) para aceptar su "
                    "transcripción sin pasar por el VLM"
    )
    # ✅ 2026-10-10: era `Literal["auto", "qwen3-vl:4b"]` —el nombre de UN modelo— y su descripción
    # prometía «forzar uso explícito», pero MEDIDO: `get_vision_config()` devolvía `llm_vision_model` en
    # las dos ramas, así que no tenía ningún efecto. Ahora es texto libre: «auto» = usá el VLM que
    # configuraste (`llm_vision_model`), que es lo que hace siempre, y cualquier otro nombre se acepta
    # por compatibilidad (la interfaz puede guardarlo al recargar ajustes).
    vision_model_selector: str = Field(
        default="auto",
        description=(
            "Selector de modelo de visión para la interfaz. 'auto' = usá `llm_vision_model`, que es el "
            "VLM que configuraste y lo que se usa siempre. Se aceptan otros nombres por compatibilidad."
        )
    )

    # ═══════════════════════════════════════════════════════════
    # === JARVIS MODE — v0.4.0 ===
    # ═══════════════════════════════════════════════════════════
    jarvis_mode_default: Literal["normal", "jarvis", "voice_only", "silent"] = Field(
        default="normal",
        description=(
            "Modo por defecto al iniciar el sistema. "
            "'normal' = GUI + voz opcional. "
            "'jarvis' = voz full-duplex + respuestas cortas. "
            "'voice_only' = igual que jarvis, sin salida en GUI. "
            "'silent' = procesa sin TTS (automatizaciones)."
        )
    )
    jarvis_wake_word: str = Field(
        default="hey rafael",
        validation_alias=AliasChoices("AXIOMA_JARVIS_WAKE_WORD", "JARVIS_WAKE_WORD"),
        description="Frase de activación del modo Jarvis. ✅ 2026-10-01 (ISSUE-127): "
                    "default 'hey rafael'. Se verifica por STT sobre la frase; "
                    "`_strip_wake_prefix` acepta TAMBIÉN el nombre solo ('rafael …'), "
                    "porque Whisper puede transcribir 'hey' de varias formas.",
    )
    jarvis_response_max_tokens: int = Field(
        default=150, ge=50, le=512,
        description="Máximo de tokens por respuesta en modo Jarvis (respuestas cortas y directas)"
    )
    jarvis_tts_auto_speak: bool = Field(
        default=True,
        description="Si True, toda respuesta en modo Jarvis se vocaliza automáticamente vía TTS"
    )
    jarvis_voice_confidence_threshold: float = Field(
        default=0.5, ge=0.3, le=0.95,
        description="Umbral mínimo de confidence STT para procesar un comando en modo Jarvis. "
                    "✅ plan_rafael (2026-08-31): ÚNICA fuente del umbral (antes yaml=0.5, "
                    "voice_pipeline hardcode 0.35, settings 0.6 — tres valores divergentes). "
                    "STTConfig y VoicePipeline lo leen de acá.",
    )
    jarvis_proactive_enabled: bool = Field(
        default=False,
        description="✅ plan_rafael (2026-08-31): ProactiveEngine OFF por default — "
                    "Rafael SOLO responde cuando el usuario le habla (chat o voz). "
                    "Si se activa, vuelve a hablar solo (clima/noticias en horarios).",
    )
    jarvis_screen_describe_on_query: bool = Field(
        default=False,
        description=(
            "Si True, al recibir cualquier consulta en modo Jarvis se captura "
            "y describe la pantalla como contexto adicional"
        )
    )
    jarvis_autonomous_task_max_steps: int = Field(
        default=5, ge=1, le=20,
        description="Máximo de pasos en una tarea autónoma encadenada (AUTONOMOUS_TASK)"
    )
    jarvis_autonomous_task_timeout: int = Field(
        default=120, ge=30, le=600,
        description="Timeout global en segundos para tareas autónomas encadenadas"
    )
    jarvis_gui_indicator: bool = Field(
        default=True,
        description="Si True, muestra indicador visual del modo Jarvis en la sidebar"
    )

    # ═══════════════════════════════════════════════════════════
    # === PLAN v2.0 (2026-09-01) — integridad de contenido + seguridad ===
    # ═══════════════════════════════════════════════════════════
    # Fase 6: delimita contenido externo (web) antes de inyectarlo al LLM
    # para evitar prompt injection indirecta vía resultados de búsqueda.
    content_grounding_enabled: bool = Field(
        default=True,
        description="Envuelve fuentes externas en <fuente_externa> y valida citas [n] "
                    "contra search_results antes de sintetizar (Fase 6 plan v2.0)"
    )
    # Fase 3: gate anti-injection sobre el input directo del usuario, antes de
    # cualquier cache/classify en dispatcher_process.process().
    input_screening_enabled: bool = Field(
        default=True,
        description="Screen anti-jailbreak del input directo en process() "
                    "(Fase 3 plan v2.0)"
    )
    # Fase 4: repo-map opcional para CoderAgent (CodeIndex de axioma_auditor).
    # OFF por defecto — no cambia el comportamiento actual.
    repo_map_enabled: bool = Field(
        default=False,
        description="Inyecta CONTEXTO DEL REPO en _build_prompt() de CoderAgent "
                    "usando CodeIndex (Fase 4 plan v2.0)"
    )
    repo_map_max_symbols: int = Field(
        default=10, ge=1, le=50,
        description="Máximo de símbolos del repo-map inyectados al prompt del CoderAgent"
    )

    @property
    def jarvis_config(self) -> Dict[str, Any]:
        """
        Puente entre los campos jarvis_* sueltos y el dict que consumen
        JarvisEngine y VoicePipeline. Sin esto ambos ignoran el .env
        y usan defaults hardcodeados.
        """
        return {
            "default_mode": self.jarvis_mode_default,
            "wake_word": self.jarvis_wake_word,
            "response_max_tokens": self.jarvis_response_max_tokens,
            "tts_auto_speak": self.jarvis_tts_auto_speak,
            "voice_confidence_threshold": self.jarvis_voice_confidence_threshold,
            "screen_describe_on_query": self.jarvis_screen_describe_on_query,
            "autonomous_task_max_steps": self.jarvis_autonomous_task_max_steps,
            "autonomous_task_timeout": self.jarvis_autonomous_task_timeout,
            "gui_indicator": self.jarvis_gui_indicator,
        }


# ═══════════════════════════════════════════════════════════
# INSTANCIA GLOBAL
# ═══════════════════════════════════════════════════════════
settings = Settings()


# ═══════════════════════════════════════════════════════════
# FUNCIONES HELPER
# ═══════════════════════════════════════════════════════════
def get_settings() -> Settings:
    """Obtiene la instancia global de configuración."""
    return settings


def reload_settings() -> Settings:
    """Recarga la configuración desde el archivo .env."""
    global settings
    settings = Settings()
    return settings


def get_setting_value(key: str, default: Any = None) -> Any:
    """Obtiene un valor específico de configuración por nombre de atributo."""
    return getattr(settings, key, default)


def validate_setting(key: str, value: Any) -> bool:
    """Valida un valor para un atributo de configuración específico."""
    try:
        current_values = settings.model_dump()
        current_values[key] = value
        Settings(**current_values)
        return True
    except Exception as e:
        log_degraded(logger, e, "config/settings.py:validate_setting")
        return False


# ═══════════════════════════════════════════════════════════
# VALIDACIÓN DE CONFIGURACIÓN AL IMPORTAR
# ═══════════════════════════════════════════════════════════
def _validate_configuration() -> None:
    """Valida la configuración al importar el módulo."""

    # 1. Validar timeouts
    if settings.ollama_timeout_standard < 30:
        logging.warning(
            f"⚠️ TIMEOUT BAJO: ollama_timeout_standard={settings.ollama_timeout_standard}s. "
            "Se recomienda ≥45s para evitar fallos en Validator."
        )

    # 2. Validar cadena de fallback
    if settings.llm_fallback_1 and settings.llm_fallback_2 and settings.llm_fallback_1 == settings.llm_fallback_2:
        logging.warning(
            f"⚠️ FALLBACK DUPLICADO: llm_fallback_1 y llm_fallback_2 apuntan al mismo modelo "
            f"({settings.llm_fallback_1}). El tercer intento repetirá un modelo ya fallido."
        )

    # 3. Validar modelos críticos con parseo robusto
    # ✅ FIX AUDITORÍA (L12): verify_models_on_startup estaba definido pero
    # nunca se chequeaba acá — el subprocess "ollama list" corría siempre al
    # importar config.settings (que casi todo archivo del proyecto importa),
    # sin importar el valor de esa config. model_check_timeout tampoco se
    # usaba — el timeout real estaba hardcodeado en 10. Se gatea el bloque
    # completo y se usa el timeout configurado. Las otras 9 validaciones de
    # esta función (timeouts, MCP, sandbox, etc.) son chequeos puros sin
    # subprocess — se dejan sin gatear, verify_models_on_startup solo
    # controla lo que su nombre promete: la verificación de MODELOS.
    if settings.verify_models_on_startup:
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=settings.model_check_timeout,
            )
            available_models = set()
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if parts and parts[0]:
                    model_name = parts[0].strip()
                    available_models.add(model_name.lower())

            for model in settings.critical_models:
                model_lower = model.lower()
                model_base = model_lower.split(":")[0]
                if not any(
                    model_lower == m
                    or m.startswith(model_base + ":")
                    or m.startswith(model_base + " ")
                    or model_base in m
                    for m in available_models
                ):
                    logging.warning(f"⚠️ MODELO CRÍTICO FALTANTE: {model}")

            # Validar modelo juez
            if settings.llm_judge_model:
                judge_model = settings.llm_judge_model.lower()
                if not any(
                    judge_model == m or judge_model.startswith(m.split(":")[0] + ":")
                    for m in available_models
                ):
                    logging.error(f"❌ ERROR CRÍTICO: MODELO JUEZ NO ENCONTRADO: {settings.llm_judge_model}")
                    logging.error("El Validator Agent usará el modelo por defecto y fallará.")
                    logging.error(f"Solución: Ejecuta 'ollama pull {settings.llm_judge_model}'")
                else:
                    logging.info(f"✅ Modelo Juez disponible: {settings.llm_judge_model}")
            else:
                logging.info("✅ Modelo Juez desactivado (llm_judge_model='') — ValidatorAgent usa llm_fallback_1")

        except FileNotFoundError:
            logging.warning("⚠️ Ollama no encontrado en PATH — saltando validación de modelos")
        except subprocess.TimeoutExpired:
            logging.warning("⚠️ Timeout verificando modelos Ollama — continuando")
        except Exception as e:
            logging.debug(f"Validación de modelos omitida: {e}")
    else:
        logging.debug("Validación de modelos Ollama omitida (verify_models_on_startup=False)")

    # 4. Validar Agent Loop config
    if settings.agent_loop_max_iterations < 1:
        logging.warning(
            f"⚠️ AGENT LOOP: max_iterations={settings.agent_loop_max_iterations}. "
            "Se recomienda ≥10 para tareas complejas."
        )

    # 5. Validar config MCP
    if settings.mcp_auto_connect and not settings.mcp_servers:
        logging.warning(
            "⚠️ MCP: mcp_auto_connect=True pero mcp_servers está vacío. "
            "No se conectará ningún server MCP al startup."
        )
    parsed_servers = settings.parse_mcp_servers()
    if parsed_servers:
        logging.info(
            f"✅ MCP: {len(parsed_servers)} server(s) configurado(s): "
            f"{[s['label'] for s in parsed_servers]}"
        )

    # 6. Validar config Sandbox
    if settings.sandbox_mode == "strict" and settings.sandbox_enable_network:
        logging.warning(
            "⚠️ SANDBOX: sandbox_enable_network=True en modo 'strict'. "
            "El acceso a red será deshabilitado automáticamente."
        )
    if not settings.sandbox_blocked_commands:
        logging.warning(
            "⚠️ SANDBOX: sandbox_blocked_commands está vacío. "
            "Se recomienda mantener al menos los comandos críticos bloqueados."
        )
    logging.info(f"✅ Sandbox config: mode={settings.sandbox_mode}, timeout={settings.sandbox_timeout_seconds}s")

    # 7. Validar config EmbeddingOptimizer
    if settings.embedding_cache_max_size < 500:
        logging.warning(
            f"⚠️ EMBEDDING CACHE: tamaño pequeño ({settings.embedding_cache_max_size}). "
            "Se recomienda ≥1000 para mejor hit rate."
        )
    if settings.query_cache_ttl_seconds < 120:
        logging.warning(
            f"⚠️ QUERY CACHE: TTL muy corto ({settings.query_cache_ttl_seconds}s). "
            "Se recomienda ≥300s para queries frecuentes."
        )
    logging.info(
        f"✅ EmbeddingOptimizer config: "
        f"cache={settings.embedding_cache_max_size}, "
        f"batch={settings.embedding_batch_size}, "
        f"percentile={settings.similarity_percentile}"
    )

    # 8. Validar concurrencia LLM
    if settings.ollama_max_concurrent_requests > 2:
        logging.warning(
            f"⚠️ CONCURRENCIA: ollama_max_concurrent_requests={settings.ollama_max_concurrent_requests}. "
            "Valores >2 pueden saturar la GPU compartida y causar latencias de 60-120s. "
            "Recomendado: 2 para GPU única."
        )
    logging.info(
        f"✅ LLM Concurrency: max_concurrent={settings.ollama_max_concurrent_requests}"
    )

    # 9. Validar Vision models
    vision_model = settings.llm_vision_model
    vision_selector = settings.vision_model_selector
    logging.info(
        f"✅ Vision: principal={vision_model} (timeout={settings.ollama_timeout_vision}s), "
        f"selector={vision_selector} — modelo legacy ELIMINADO por fallo silencioso"
    )

    # 10. Validar Jarvis Mode
    logging.info(
        f"✅ Jarvis Mode: default={settings.jarvis_mode_default}, "
        f"wake_word='{settings.jarvis_wake_word}', "
        f"tts_auto={settings.jarvis_tts_auto_speak}"
    )


# Ejecutar validación al importar (solo en producción, no en testing)
if __name__ != "__main__":
    try:
        _validate_configuration()
    except Exception as _d2e:
        logging.getLogger(__name__).debug(f"[D2 settings.py:891] excepción degradada (intencional): {_d2e}")
