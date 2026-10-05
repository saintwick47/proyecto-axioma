# 🧠 AXIOMA — Sistema Multi-Agente de IA Local-First

> **Repositorio:** [github.com/saintwick47/proyecto-axioma](https://github.com/saintwick47/proyecto-axioma)
> **Manual técnico** — para quien quiera entender o modificar el proyecto. Si querés *usarlo*, empezá por [`README.md`](README.md) y [`MANUAL_USUARIO.md`](MANUAL_USUARIO.md).
> **Documentación verificada:** 2026-09-08 (lectura directa del código fuente + suites pytest; ver [STRUCTURE_REPORT.md](docs/STRUCTURE_REPORT.md) y [AXIOMA_COMPLETE_CONTEXT.md](docs/AXIOMA_COMPLETE_CONTEXT.md))

**AXIOMA** es un ecosistema de agentes de IA modulares y resilientes, orquestado localmente sobre **Ollama** (filosofía *Local-First*): enrutamiento inteligente de intenciones, memoria persistente multicapa, pipeline de voz/visión, ejecución segura de código (Sandbox + MCP) y gestión estricta de VRAM.

> ⚠️ **Sobre la versión:** el repo **no tiene una versión canónica única**. `src/__init__.py` declara `0.5.0`, `main.py` declara `1.0.8`, `config/models.yaml` declara `v0.6.7` y el README anterior afirmaba `v0.3.6`. Cualquier cita de versión debe indicar la fuente.

| Estado | Git | Licencia | Arquitectura | LLM Core | Commits |
| :---: | :---: | :---: | :---: | :---: | :---: |
| 🟢 En desarrollo activo | 314 commits · `main` · tags `v0.0.8`, `v0.1.1` | MIT | Linux / Python 3.10+ | Ollama (local) | 314 |

---

## 📚 Documentación (fuentes fiables)

| Documento | Contenido |
|---|---|
| [docs/STRUCTURE_REPORT.md](docs/STRUCTURE_REPORT.md) | Listado estructural auto-generado por `tools/structure_detector.py` (v0.3.1, métricas corregidas y verificadas) |
| [docs/AXIOMA_COMPLETE_CONTEXT.md](docs/AXIOMA_COMPLETE_CONTEXT.md) | Generado por `tools/documentador/` (v1.0.9): modelos reales de `ollama list`, sin fugas ni carpetas externas. ⚠️ Las secciones enriquecidas por IA dependen del modelo usado |

---

## 🏗️ Resumen de arquitectura (verificado)

```
Usuario (GUI NiceGUI / CLI / API / voz)
        ▼
Dispatcher ──────────────────────────── src/core/dispatcher.py (+ 4 mixins)
  ├─ IntentClassifier + caché L1/L2/L3  src/router/
  ├─ SmartRouter (facade)               src/router/smart_router.py
  ├─ Handlers por tarea                 src/core/dispatcher_handlers.py
  │    └─ CoderAgent · Searcher · Researcher · Analyst · Validator ·
  │       System · Vision · JarvisEngine · InterpreterLoop · DataHandler
  ├─ MemoryGateway (4 capas)            src/memory/gateway.py
  ├─ OllamaClient (circuit breaker)     src/llm/client.py
  └─ ModelSwap (VRAM arbitrator)        src/core/model_swap.py
```

### Componentes clave

- **Dispatcher** (`src/core/`): orquestador central, compuesto por 4 mixins (`dispatcher_handlers`, `dispatcher_operations`, `dispatcher_process`, `dispatcher_distributed`). Clasifica intención, consulta caché multicapa, enruta al agente adecuado, valida y retroalimenta.
- **Agentes** (`src/agents/`): `CoderAgent` genera código con contrato verificable (`CodeContract` + `CodeValidator` en 3 capas) y ciclo de reparación ante fallos; `SearcherAgent` (Tavily→Serper→DDG) y `ResearcherAgent` (clima/hora/noticias/finanzas); `AnalystAgent` (documentos/datos); `ValidatorAgent` (CoT forzado + auditoría CWE Top-25); `VisionAgent` (VLM); `SystemAgent` (control del SO); subpaquete `loop/` con el ciclo observe-think-act-reflect.
- **Enrutamiento** (`src/router/`): `IntentClassifier` + `SmartRouter` + caché L1 (RAM LRU) / L2 (SQLite) / L3 (semántica LanceDB).
- **Memoria** (`src/memory/`): `MemoryGateway` unifica ShortTerm (RAM), LongTerm (SQLite `data/axioma.db`), Semantic (LanceDB, embeddings `BAAI/bge-m3`) y KnowledgeGraph (SQLite).
- **Multimodal** (`src/multimodal/`): voz full-duplex (wake word → VAD → STT `faster-whisper` → TTS `edge-tts`/`piper`) y visión (captura de pantalla multi-backend + cámara).
- **Ejecución segura** (`src/tools_mcp/`): `Sandbox` (límites Unix, blacklist, redacción de secretos), `PermissionGateway`, `PromotionGate` (veredictos para operaciones destructivas) y protocolo **MCP** (JSON-RPC 2.0).
- **Seguridad por modos**: `RuntimeMode` (standard/minimal/safe_mode/code_mode) filtra el toolset por sesión en `ToolExecutor` y `ToolRegistry.list_for_mode()`; sandbox **fail-closed** (`SandboxUnavailableError` si STRICT no puede confinar); `PermissionMode.DENY` con reglas "NUNCA tocar" para `.git/**`, `.env*`, `config/**` y `data/**`; `src/**` siempre pide confirmación.
- **Trazabilidad**: `SessionEventLog` (bitácora de sesión con replay/`derive_messages`/`fork`) y `FileAuditTrail` (operaciones de archivo con checksum SHA256), integrados en `MemoryGateway.add_message()` y `PromotionGate`.
- **Eventos**: `EventBus` + `SecurityListener` publican y observan denegaciones de tools, circuit-open, operaciones de archivo y fail-closed del sandbox — sin acoplar productores a consumidores.
- **Coordinación multi-modelo sin OOM**: `AgentCoordinator` + `TaskBoard` + `MemoryGuard` orquestan agentes multi-modelo: el guard rechaza cargas que sobrecargarían la RAM (`refused_oom`), el coordinador **drena** al modelo cargado antes de swappear y usa **checkpoint/resume** para dependencias cruzadas (sin perder el hilo). `allow_parallel_models` habilita el futuro paralelo solo si `can_coexist()` confirma memoria.

---

## 🤖 Modelos configurados (fuente: `config/settings.py` + `config/models.yaml`)

| Rol | Modelo |
|---|---|
| Chat general (default) | `qwen3:8b` |
| Código + fallback 1 | `qwen2.5-coder:7b` |
| Visión (VLM) | `qwen3-vl:4b` |
| Embeddings | `BAAI/bge-m3` |

`llm_judge_model` y `llm_fallback_2` están desactivados; el juez externo fue eliminado — la validación de respuestas la hace el `ValidatorAgent` local (single-pass).

**Comandos del chat:**
- `/plan <consulta compleja>` — pipeline multi-paso opt-in: rol principal (coder/researcher) + validador final, cap `settings.plan_max_steps=2`. Auto-opcional con `plan_pipeline_enabled=true` (default OFF). Detalles en `COMANDOS_AXIOMA.txt` §1.1.
- 🖼️ **Adjuntar imágenes al chat**: PNG/JPG/WebP/GIF/BMP → `qwen3-vl:4b` bajo demanda; OCR híbrido con **Tesseract local** al pedir leer/transcribir texto y **preprocesado adaptativo** (autocontraste/upscale). Ver *🖼️ Imágenes en el chat* más abajo.
- 📎 **Fuentes/citas**: respuestas grounded (fs_search/folder-analysis) muestran el bloque *📎 Fuentes* en la Web y la lista en el CLI.
- 🧮 **Calculadora**: tool MCP determinística y segura (AST, sin `eval()`) en el agent loop.
- Consultas de archivos grounded (sin LLM, <1 s): *"usá fs_search para buscar qué archivos de X hablan de Y"* / *"qué archivos mencionan Z"* / *"usá fs_list para listar la carpeta X"* → grep local con `sources`; validador con extracción tolerante a markdown y streaming de tokens opt-in vía `stream_chat_tokens`.

---

## 🖼️ Imágenes en el chat (v0.6.9p — canal de visión)

Adjuntás una imagen con 📎, escribís tu pregunta (o nada → la describe) y responde el VLM local. Canal en `src/interfaces/web/`:

1. **Detección**: extensiones de imagen se guardan en base64 (nunca se mezclan como texto).
2. **OCR híbrido** (`vision_ocr.py`): si la instrucción pide leer/transcribir (`copiá/transcribí/texto/dice…`) y Tesseract local (dependencia de sistema `tesseract-ocr-spa`) da confianza ≥ `vision_ocr_min_conf` → texto **exacto en ~0 s**; si no, cae al VLM.
3. **Preprocesado** (`vision_preprocess.py`): análisis CV (desviación estándar de grises) → contraste bajo = autocontraste; imagen <400 px = upscale 2×; la original queda como fallback del reintento.
4. **VLM** (`vision_agent.describe_generic_image`): prompt **neutro** (sin el sesgo de screenshot del VisionAgent de Jarvis), tope `vision_num_predict` (evita generaciones colgadas) y **unload de RAM** tras usar (best-effort).

**Medido en vivo (qwen3-vl:4b, CPU):** identifica colores, formas geométricas, conteo de objetos (hasta ≥5), números, escenas simples y **no alucina** con ruido aleatorio; OCR de texto grande/limpio exacto. **Límites:** texto muy chico o embebido → inestable en el VLM (mitigado por Tesseract); fotos reales complejas/rostros → acotados por el hardware (modelo 4B); cada imagen cuesta ~45-55 s en CPU (incluye recarga, que desaloja `qwen3:8b` de Ollama).

```bash
python tools/vision_battery.py                      # batería diagnóstica 13 casos → logs/vision_battery_*.md
python tools/vision_battery.py --quick              # subconjunto rápido
AXIOMA_VISION_E2E=1 venv/bin/python -m pytest tests/test_axioma_11_vision_imagenes.py -q   # 47 tests (integración real)
```

Settings: `vision_num_predict` (512) · `vision_preprocess_enabled` · `vision_ocr_enabled` · `vision_ocr_min_conf` (60) · `llm_vision_model`.

---

## 🚀 Comandos (fuente: `COMANDOS_AXIOMA.txt`)

```bash
# Entry point
python main.py --check                # verificación de dependencias/hardware
python main.py --chat "consulta"      # chat por CLI (--chat sin args = interactivo)
python main.py --web                  # servidor web NiceGUI + API en http://127.0.0.1:8000
python main.py --backup               # backup de datos

# Tests
pytest tests/ -v
python tools/run_diagnostic_suite.py            # diagnóstico por fases
python tools/run_diagnostic_suite.py --real     # incluye tests con Ollama
python tools/log_health_report.py           # health report desde logs (E2; v2 detector: clasificación de errores, anomalías/verdicto, reintentos, reparaciones, delta vs previo)

# Seguridad / auditoría
python axioma_auditor.py                        # auditoría completa (26 detectores → logs/)
python axioma_auditor.py --full --run-tests     # + pytest + SCORE AXIOMA /100
python axioma_auditor.py --format json -o logs/audit_summary.json   # resumen máquina (lo lee el gate)
venv/bin/python tools/run_all_tests.py --timeout 150   # suite por archivo + reporte con run_id
venv/bin/python tools/quality_gate.py           # 13 condiciones de calidad (CI/local)
venv/bin/python tools/measure_task_config.py    # modelo + temperatura EFECTIVOS por tarea
python axioma_auditor.py --detectors security_harness   # solo protecciones FASE 1-3
python axioma_auditor.py --detectors plan_v2_protections # solo protecciones PLAN v2.0
# axioma_inspector.py fue FUSIONADO en el auditor (v8.3): singletons, asyncio
# module-level, imports circulares, duplicados, integración, tests y SCORE.
# v0.6.8uu: axioma_auditor.py es SHIM — la lógica vive en tools/auditor/
# (5 módulos aud_*; tipos AudVerdict/AudSeverity/AudFinding sin colisión).

# Evals (requiere Ollama con qwen2.5-coder:7b)
python tools/run_evals.py                       # ejecuta tests/dataset_evals.yaml con LLMJudge
```

## 🛡️ Seguridad y modos de ejecución (implementado)

> ✅ **Finalizado y verificado** — protecciones de seguridad (plan_harness FASE 1-4) e
> integridad de contenido + anti-injection (PLAN v2.0, Fases 2-6): implementadas, con
> **0 hallazgos** en los detectores `security_harness` y `plan_v2_protections` del
> auditor, y cubiertas por `tests/test_axioma_07_seguridad_modos.py` (23 tests) y
> `tests/test_axioma_10_plan_v2.py` (18 tests por Fase). Los documentos de plan
> originales no están en el repo; la verificación automática del auditor es la canónica.

### RuntimeMode — acota el toolset por sesión

| Modo | Descripción | Escribe archivos | Nivel de permiso máximo |
|---|---|---|---|
| `standard` (default) | Toolset completo | ✅ | PRIVILEGED |
| `minimal` | Solo sandbox_exec + fs_read | ❌ | EXECUTE |
| `safe_mode` | Solo lectura | ❌ | READ_ONLY |
| `code_mode` | Generación de código orquesta tools | ✅ | PRIVILEGED |

- CLI: `/mode [standard|minimal|safe_mode|code_mode]` persiste en `settings.runtime_mode` y se propaga a TODAS las tools de los agentes (verificado end-to-end: `safe_mode` bloquea `fs_write` desde un agente).
- **Sandbox fail-closed**: si STRICT no puede aplicar límites de recursos → `SandboxUnavailableError` — nunca degrada a ejecución sin límites.

### Reglas DENY en `config/permissions.json`

| Regla | Efecto |
|---|---|
| `.git/**` deny | NUNCA tocar el repositorio (ni lectura) |
| `.env*` deny | NUNCA tocar secretos |
| `config/**` deny + `fs_read,fs_list` | Config: solo lectura desde chat |
| `data/**` deny + `fs_read,fs_list` | Datos: solo lectura desde chat |
| `src/**` default | Código fuente: SIEMPRE pide confirmación |

### Trazabilidad, eventos y coordinación multi-modelo

- **Auditoría**: `SessionEventLog` (bitácora de sesión con fork/replay) + `FileAuditTrail` (checksum SHA256), integrados en `MemoryGateway.add_message()` y `PromotionGate`.
- **Eventos**: `EventBus` + `SecurityListener` registran denegaciones de tools, circuit-open, operaciones de archivo y fail-closed (los listeners nunca rompen al publicador).
- **Sin OOM**: `MemoryGuard` (rechaza si la RAM post-swap supera `memory_ram_hard_ratio` 0.95) + `AgentCoordinator` (drenado + checkpoint/resume entre modelos) + `TaskBoard` (estado `refused_oom`). `settings.allow_parallel_models` (default `False`).
- CLI: `/session events [id] | fork <id> | invariant [id]`, `/audit [session_id]`, `/mode [modo]`, `/guard [memory|coordinator]`.

```bash
# Git / commit con seguridad
python tools/commit.py --message "feat: ..."    # backup + commit + log
python tools/commit.py --list-backups
python tools/commit.py --restore TIMESTAMP

# Verificación / diagnóstico
python tools/verify_axioma.py
python tools/structure_detector.py              # regenera STRUCTURE_REPORT.md
python tools/system_check.py health           # chequeo de salud (hardware/Ollama/runtime)
```

---

## 🖥️ Interfaz web (qué ve el usuario)

| Botón / zona | Qué hace |
|---|---|
| **🔍 ¿Por qué esta respuesta?** (bajo cada respuesta) | Panel de transparencia: modelo, tarea, agente que atendió, **confianza calibrada**, estado de validación (`aprobada`/`fallida`/`omitida`/`no aplica`), fuentes y tiempo. Dentro, la **calificación 1-5 + comentario + etiquetas**, que sí llega al ciclo de aprendizaje (`FeedbackLoop.record_explicit`) |
| **🟢/🟡/🔴 badge de salud** (header) | Estado del sistema en vivo: degradaciones de contrato en rutas críticas, tasa de error de la API, feedback negativo. Refresco manual (sin polling) |
| **🗂️ Sesiones** | Listar, buscar, **exportar** (MD/HTML/JSON) y borrar sesiones. No permite borrar la sesión en uso |
| **📄 Exportar** | Investigación (respuestas con citas) **y** conversación completa (incluye el código generado) en MD/HTML/JSON |
| **🖥️ Mi Hard** | Hardware detectado + qué modelos entran (`hardware_fit`) y ranking de instalados |
| **🏥 Estado** | Servicios y APIs (con el motivo real: `Sin key (.env)` vs `No responde`) |
| **📁 Archivos / 📎 Imagen** | Explorar, leer y adjuntar archivos; imágenes por canal de visión |

### Endpoints nuevos (además de los históricos)
```
GET /api/v1/metrics/degradation   → degradaciones de contrato vs esperadas (con alertas)
GET /api/v1/feedback/stats        → feedback agregado + señal ROUTED
GET /api/v1/stats                 → requests/latencia/uptime reales + storage + degradación
GET /api/v1/hwfit/{hardware,rank,check,compatible}
```
> ⚠️ La API se monta bajo `/api` y el router ya trae `/api/v1`: la URL real es
> **`/api/api/v1/...`**.

---

## 🛠️ Stack tecnológico (dependencias reales de `requirements.txt`)

- **Web/UI:** FastAPI, Uvicorn, NiceGUI, Starlette
- **IA/LLM:** Ollama (HTTP), transformers, sentence-transformers, vLLM (cliente alternativo)
- **Voz:** faster-whisper, silero-vad, webrtcvad, edge-tts, piper-tts, sounddevice
- **Memoria vectorial:** lancedb, pyarrow, numpy, pandas
- **ML:** scikit-learn, scipy, networkx, sympy
- **Observabilidad:** opentelemetry-*, rich
- **Configuración:** pydantic, pydantic-settings
- **Automatización web:** playwright
- **Otros:** httpx, pytest, coverage, PyYAML, tenacity

---

## 📊 Métricas reales (medición propia, 2026-08-24)

Excluye `venv/`, `.git/`, `deepseek-harness/`, `cache/`, `data/` (backups/salidas) y `logs/`:

| Métrica | Valor |
|---|---|
| Archivos de código documentados | **270** |
| Archivos Python de fuente | **250** (249 reportados; `tools/push.py` excluido por seguridad) |
| LOC Python | **≈ 117.400** |
| Clases (incl. anidadas) | **≈ 678** |
| Funciones (incl. métodos) | **≈ 4.471** |
| Archivos de test | **18** `test_*.py` en `tests/` (la suite del proyecto) |

> ⚠️ Las métricas de los README anteriores (p. ej. "1.067 funciones", "124.473 LOC", "47 TaskTypes", "162 variables de entorno") **no eran fiables** — provenían del documentador automático. Las de este README y las de `docs/STRUCTURE_REPORT.md` (generadas por `tools/structure_detector.py` v0.3.1) están verificadas. El documentador (`tools/documentador/`) reporta su propio alcance (excluye además `tools/documentador/`, `commit.py` y `structure_detector.py`); sus métricas están etiquetadas con esa nota de alcance.
> **2026-09-11:** suite en verde con **17 archivos** `test_axioma_*.py` (**768 tests**,
SCORE AXIOMA **100/100**, HIGH=0) y **gate de calidad** en CI. Los archivos de
test se **fusionan por tema** (no se multiplican) y ninguno supera las 950 líneas
(regla R13 — ver `docs/AGENT_GUIDE.md`). Se sumaron el canal de imágenes (visión +
OCR híbrido, `tests/test_axioma_11_vision_imagenes.py`), calculadora y
fuentes/citas (hoy dentro de `test_axioma_10_plan_v2.py`) y el health report v2 —
regenerar estructura con `python tools/structure_detector.py` para métricas
actualizadas.

---

## 📁 Estructura de directorios

```
axioma/
├── main.py                  # Entry point CLI + servidor web
├── Rafael.py                # Daemon autónomo (systemd)
├── src/                     # Paquete principal (156 archivos .py)
│   ├── core/                # Dispatcher, colas, VRAM, feedback, jarvis
│   ├── agents/              # Agentes especializados (+ loop/)
│   ├── router/              # Clasificador + SmartRouter + caché L1/L2/L3
│   ├── llm/                 # OllamaClient, ModelRouter, vLLM
│   ├── memory/              # Gateway de 4 capas + embeddings + KG
│   ├── multimodal/          # Voz (STT/TTS/VAD/wake) + visión
│   ├── interfaces/          # Web (FastAPI+NiceGUI), componentes, CLI
│   ├── tools_mcp/           # Sandbox, permisos, herramientas, MCP
│   ├── prompts/             # Gestión de prompts + plantillas YAML
│   ├── domain/              # Tipos, entidades, excepciones, contratos
│   ├── handlers/            # Datos deterministas sin LLM
│   ├── integrations/        # APIs externas
│   ├── monitoring/          # Alertas
│   ├── learning/            # Trazas de ejecución
│   └── utils/               # Utilidades
├── config/                  # settings.py, models.yaml, paths.py, ...
├── data/                    # BD SQLite, memoria vectorial, backups, salidas
├── tests/                   # Suite pytest (18 test_*.py: axioma integral + diagnósticos)
├── tools/                   # Operación, verificación, diagnóstico, documentador
├── logs/                    # Logs del sistema
└── docs/                    # Documentación (STRUCTURE_REPORT, AXIOMA_COMPLETE_CONTEXT, documentador)
```

La estructura y función de **cada archivo** está en [docs/STRUCTURE_REPORT.md](docs/STRUCTURE_REPORT.md).
