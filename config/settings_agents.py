#!/usr/bin/env python3
# ===============================================================
# AXIOMA - settings_agents.py  (REFACTORIZACION POR PARTES)
# Ruta: config/settings_agents.py
#
# 2026-10-13: dominio **AGENTES ... BENCHMARK** extraido de `config/settings.py`, sin cambios de
# comportamiento. Son CAMPOS, asi que el mixin hereda de `pydantic.BaseModel` (una clase
# comun no recolecta anotaciones como campos; el mixin de METODOS que ya existia,
# `settings_computations.py`, es una clase comun a proposito).
#
# La indentacion del bloque NO se toca: va al cuerpo de OTRA clase.
# ===============================================================

from pydantic import BaseModel, AliasChoices, Field


class SettingsAgents(BaseModel):
    """Dominio AGENTES ... BENCHMARK extraido de `Settings`."""

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

    # ✅ ContextSensor (Semana 2 D): `dispatcher.py` lo consultaba con
    # `getattr(settings, "enable_context_sensor", True)` pero el campo NO existía
    # ⇒ el literal True ganaba siempre y apagarlo por entorno era imposible.
    # Default True: no cambia nada hasta que alguien ponga
    # ENABLE_CONTEXT_SENSOR=false (evita psutil+wmctrl en cada mensaje).
    enable_context_sensor: bool = Field(default=True)

    # ✅ Searcher F4: `searcher.py:694` lo consultaba con
    # `getattr(settings, "searcher_fetch_page", True)` pero el campo NO existía
    # (aunque CHANGELOG.md lo documentaba) ⇒ el literal True ganaba siempre.
    # Default True: cero cambio. Con false se evita el `httpx.get` (timeout 6 s)
    # del primer resultado y el prompt usa el snippet de 300 chars (no 1200).
    searcher_fetch_page: bool = Field(default=True)
