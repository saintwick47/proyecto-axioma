# 📋 CHANGELOG — AXIOMA

Todos los cambios notables en este proyecto serán documentados en este archivo.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-1.0.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Sin publicar] — AXIOMA en contenedores (instalable)

### Agregado
- **Instalación en contenedores**: imagen de dos etapas **sin modelos adentro** (~5,6 GB), CPU primero,
  con la suite completa corriendo **dentro** de la imagen (una imagen que no pasa las pruebas no se publica).
- **Lanzador de doble clic**: `instalar/instalar_axioma.sh` (una sola vez) y `instalar/iniciar_axioma.sh`
  (cada vez). Decide solo si usar el **Ollama del equipo** o levantar el que viene con AXIOMA.
- **Windows**: `instalar/instalar_axioma.ps1` y `instalar/iniciar_axioma.ps1` (PowerShell) hacen lo mismo
  que los de Linux y dejan acceso directo en el menú Inicio y en el Escritorio. Ahí no existe
  `network_mode: host`, así que usan el servicio `axioma-puente` (perfil `puente`, puerto 8080 y
  `host.docker.internal` para llegar al Ollama del equipo). La **voz** todavía no está disponible en
  Windows. Los guiones están cubiertos por pruebas de estructura (BOM para PowerShell 5.1, sintaxis
  balanceada, perfiles que existen, sin emoji por la consola CP850), pero **todavía no se probaron en un
  Windows real** (en el equipo del autor no hay ninguno, medido: `command -v pwsh` no devuelve nada).

- **Primer arranque guiado**: la pantalla **"Configurar AXIOMA"** se abre sola la primera vez, explica qué
  falta y lo instala con un clic (con avance real y botón DETENER).
- **Carga guiada de claves** (`python -m src.core.apikeys`): cada persona carga las suyas; el archivo queda
  con permisos sólo para su usuario y **nunca se muestran los valores**.
- **Preflight** (`python -m src.core.preflight`): informa qué le falta al equipo, con el motivo y el remedio.
- **Memoria por niveles** (9 GB mínimo · 16 GB recomendado · 24-32 GB ideal) y **detección de placa de video**.
- **Modo de trabajo en paralelo**: con placa suficiente, el modelo más pesado vive en la placa y el otro en
  memoria (no se descarga al cambiar de tarea).
- **Servicio de voz en el contenedor** (perfil `voz`), con su propio volumen de registros.

### Cambiado
- **Ningún usuario cocido**: la identidad por defecto queda **vacía** y el rol por defecto es `user`. La
  primera vez AXIOMA **pide crear un usuario propio** (antes traía el usuario del autor).
- Los manuales (`README.md`, `MANUAL_USUARIO.md` —nuevo, para quien no sabe programar—, `COMANDOS_AXIOMA.txt`,
  `MANUAL_AXIOMA.md`, `MANUAL_RAFAEL.md`) están actualizados a la instalación con contenedores.

### Corregido
- **El perfil `puente` arrancaba de más** (encontrado al probar el camino de Windows): medido el
  2026-10-06, `docker compose --profile puente up -d` levanta **también** el servicio `axioma` (no tiene
  perfil, arranca siempre). Como ése usa la red del equipo, los dos pelean por el 8080 y `axioma` queda en
  **bucle de reinicios** (verificado: pasó a `Restarting` mientras `axioma-puente` atendía el 8080). Ahora
  el lanzador nombra el servicio —`up -d axioma-puente`, y `ollama` si hace falta— y hay prueba que lo exige.
- Rutas absolutas del autor, `.env` relativo a la carpeta de trabajo, ruta del modelo de voz y del
- El chequeo del entorno ya no dice "listo" cuando falta Ollama o un modelo (explica qué falta y sale con 4).
- El registro del CLI no se cae en los caminos de error; el paquete CLI ya no recursa al importarlo.
- Las pruebas ya no dependen de la configuración de un equipo en particular (fallaban en un entorno limpio).

**Esquema de Versionado:**
- Versión inicial: `0.0.1`
- Cada fix grande: `+0.0.1` (ej: 0.0.1 → 0.0.2)
- Cada archivo nuevo: `+0.0.1` (ej: 0.0.2 → 0.0.3)
- Funcionalidad completa: `0.0.5`
- Features mayores: `0.1.0`, `0.2.0`, etc.
- Release estable: `1.0.0`

---

## [Unreleased] — En Desarrollo

### 🧠 v0.6.9z — P1.3: el loop de aprendizaje consume su salida (ISSUE-069) — 2026-09-14

**Problema (ISSUE-069):** el sistema medía feedback y calculaba ajustes de
`temperature`/`timeout`/`threshold` que **nadie consumía**
(`get_optimized_parameter` no tenía lectores fuera de `feedback_loop*`). Además un
key mismatch en `ParameterOptimizer.get_current_value()` hacía que el ajuste
guardado nunca se leyera.

- ✅ **`ParameterOptimizer.get_current_value()`**: corregido el key mismatch
  (buscaba la clave compuesta `"param:task_type"` donde `adjust_parameter()`
  guarda bajo `task_type`). Nuevo `get_adjusted_value()` para leer solo valores
  **aprendidos** (sin caer al default).
- ✅ **Flag `feedback_temperature_tuning_enabled`** (default `False`, R8).
- ✅ **`LLMConfig.get_model_for_task()`** consume el valor aprendido detrás del
  flag y solo cuando hay ajuste real (no pisa las temperaturas por tarea de
  `models.yaml`).
- ✅ **`dispatcher_process.py`**: el learning loop periódico dispara el cálculo
  async del ajuste.
- 🔧 **Revisión 2026-09-14**: restaurado `metadata["confidence"]` (regresión P1.1
  / ISSUE-056/063) y corregido el override indebido de temperaturas por tarea.
- 🧪 Los 4 archivos pasan `ast.parse`; sin cambio de comportamiento con el flag
  apagado.

### 🚫 v0.6.9y — `logs/` y `docs/` fuera de git (commit y push) — 2026-09-13

ni subir el contenido de `logs/` ni de `docs/`. Son carpetas de runtime
(reportes de test, `commit_log.jsonl`, trazas) o de documentación generada por
herramientas / interna (STRUCTURE_REPORT, AXIOMA_COMPLETE_CONTEXT, RAC_*, planes).

- ✅ **`.gitignore`**: `logs/` y `docs/` completos (antes `docs/` solo ignoraba
  `STRUCTURE_REPORT.*`, `verification_report_*`, `test_report_*`, `*.pdf/…`).
  Cualquier archivo NUEVO en esas carpetas queda fuera del repo.
  docstring decía explícitamente "SE COMMITEA: docs/"). `logs/` ya estaba.
  pushear inspecciona `git diff --name-status <upstream>..HEAD` y avisa qué
  archivos de `logs/`/`docs/` **suben contenido** (A/M/C/R; las eliminaciones no
  cuentan). `--strict` cancela el push en ese caso.
- ⚠️ **Excepción puntual**: para versionar un doc concreto hay que forzarlo a
  mano (`git add -f docs/<archivo>`), nunca desde las herramientas.
  así que sus cambios viven solo en local; lo versionado de esta regla es
  `.gitignore`.
- 📌 Los 11 archivos de `docs/` que ya estaban trackeados siguen en el repo: se
  dejan de commitear sus cambios, pero no se borran (si se quisiera ser
  estricto: `git rm -r --cached docs && git commit …`, lo que los quita del
  remoto y rompe los enlaces del README a `docs/`).
- ✅ Verificado: `docs/FIX_RAC_v0.6.9w.md` y `docs/FIX_VERSIONES_v0.6.9x.md`
  pasaron a estar ignorados (ya no aparecen en `git status`), y
  (con `--strict` los bloquea).

### 🔢 v0.6.9x — FIX VERSIONES: cada artefacto muestra la versión real — 2026-09-13

**Problema (ISSUE-071):** las versiones mostradas no correspondían a la del
proyecto (`0.6.9`, `src/__init__.py`). Los `.md` generados **no se tocaron**: se
corrigieron los archivos que los generan.

| Artefacto / superficie | Mostraba | Ahora | Generador corregido |
|---|---|---|---|
| `docs/AXIOMA_COMPLETE_CONTEXT.md` (título) | `AXIOMA v0.3.6` | `AXIOMA v0.6.9` | `tools/documentador/axioma_doc_md_generator.py` → `axioma_doc_base.AXIOMA_PROJECT_VERSION` |
| Identidad enviada al modelo | `Eres AXIOMA v0.2.0` | `Eres AXIOMA v0.6.9` | `src/prompts/base_prompts.py` |
| `GET /api/v1/status` | `"version": "0.2.0"` | `"version": "0.6.9"` | `src/interfaces/web/routes_extended.py` |
| Banner CLI del documentador | `v4.1.1` / `v3.9.0` / `v3.6` mezclados | `v4.2.1` (`DOCUMENTADOR_VERSION`) | `tools/documentador/*` |

- ✅ **Fuente única por artefacto**: la versión del **proyecto** se lee siempre de
  `src/__init__.py` (`src.AXIOMA_VERSION` / `AXIOMA_PROJECT_VERSION`); la del
  **documentador** vive en `axioma_doc_base.DOCUMENTADOR_VERSION` (4.2.1) y la del
- ✅ Se conservaron como **históricos** los marcadores de cuándo se implementó
  cada cosa (`✅ v0.3.x`, `CORRECCIONES_en_v3.9.0`) y las métricas antiguas pasan
  a decir `(medido en v0.3.6)` en lugar de aparentar ser actuales.
- ✅ Verificado: header generado `# AXIOMA v0.6.9 — Documentación Completa`,
  `STRUCTURE_REPORT.md` regenerado en `/tmp` con la cabecera correcta, y la suite
  completa en verde (976 passed / 17 skipped).
- 📄 Detalle y comandos de regeneración: `docs/FIX_VERSIONES_v0.6.9x.md`
  *(local: `docs/` ya no se versiona desde v0.6.9y)*.

### 🔴 v0.6.9w — FIX RAC: fin de los falsos positivos por truncado del pipeline — 2026-09-13

**Causa raíz (ISSUE-070):** `AnalystAgent.review_system()` leía cada archivo con
`f.read(1500)`. El LLM recibía clases abiertas e imports partidos y reportaba
"clase sin cerrar", "import truncado", "función incompleta" — **14 falsos
positivos** en `src/core/` que la auditoría y los 971 tests refutaban. Se sumaban
`max_tokens=300` (la propia respuesta se cortaba a media frase) y la ausencia de
cualquier verificación estática previa.

- ✅ **Nuevo `src/agents/rac_static.py`** (sin dependencias, solo stdlib):
  - **Lectura completa** del archivo (`leer_fuente`, tope de seguridad 400 KB)
    en lugar del corte de 1500 caracteres.
  - **Pre-validación determinista**: `ast.parse` (y `json.loads` para `.json`).
    Si la sintaxis falla, se reporta el **error real con línea** y ese archivo
    **no se envía al LLM** (antes gastaba VRAM para producir ruido).
  - **Segmentación en fronteras de nodos AST**: ningún bloque empieza a mitad de
    una clase o un import; las clases gigantes se expanden en sub-bloques de
    métodos válidos; los métodos enormes (p. ej. `process()`, 899 líneas) se
    envían como firma marcada `# [RAC] cuerpo omitido` en vez de texto cortado.
  - **Prompt anti-alucinación**: se inyecta el bloque de verificación estática,
    la lista de símbolos definidos y 6 reglas obligatorias (prohibido reportar
    sintaxis/truncamientos/ausencias refutadas; cada hallazgo con
    `archivo:línea` y cita literal; lista de comprobación de 5 puntos antes de
    responder "sin hallazgos").
  - **Filtro post-LLM**: descarta afirmaciones refutadas por la evidencia
    (p. ej. "falta el método `reserve()`" cuando `reserve` está definido) y deja
    la traza en un `<details>` colapsable.
- ✅ **Informe con sección `0. Verificación estática previa`**: módulos con
  `ast.parse` OK, errores de sintaxis reales, bloques enviados al LLM y
  afirmaciones descartadas.
- 🧪 **Validación A/B real** (mismo modelo `qwen2.5-coder:7b`, Ollama local):
  `task_board.py` → el prompt antiguo inventa *"la clase `Task` no se cierra
  correctamente"* (idéntico al RAC parcial); el nuevo devuelve
  `SIN HALLAZGOS RELEVANTES`. Igual en `state_emitter.py` y `memory_guard.py`.
  Contrapartida honesta: con 7B la RAC es ahora **más conservadora**; para más
  sensibilidad hay que subir el modelo (`settings.llm_code_model`), el pipeline
  ya no trunca.
- ✅ **`RAC_MAX_TOKENS` 300 → 600** y presupuesto de tiempo configurable
  (`AXIOMA_RAC_MAX_MINUTES`, default 45) porque el chunking hace más llamadas
  por archivo que el antiguo corte de 1500 caracteres.
- ✅ **Tests**: `tests/test_axioma_26_rac_estatico.py` (22 tests: lectura
  completa, `ast`, chunking que siempre parsea, cobertura de líneas sin huecos,
  filtros y end-to-end con LLM simulado — sin Ollama ni VRAM).
- 📄 Detalle y evidencia: `docs/FIX_RAC_v0.6.9w.md` *(local: `docs/` ya no se
  versiona desde v0.6.9y)*.

### 🟢 v0.6.9v — Plan nuevo (P0-P2): transparencia, feedback real, salud y gate — 2026-09-11

**UI / experiencia de usuario**
- ✅ **Panel de transparencia por respuesta**: colapsable "¿Por qué esta
  respuesta?" con modelo, tarea, agente que atendió (`agent_role`), **confianza
  calibrada**, estado de validación honesto (`aprobada`/`fallida`/`omitida`/
  **`no aplica`**), fuentes (diciendo si están capeadas) y tiempo.
  `confidence`/`confidence_label`/`validation_passed` **se descartaban** en
  `_build_response_metadata` (ISSUE-051).
- ✅ **Feedback explícito enriquecido y CABLEADO**: rating 1-5 + comentario +
  etiquetas. Antes eran 👍/👎 que **solo escribían un trace de log**:
  `FeedbackLoop.record_explicit()` tenía **0 llamadas en todo el repo** — el
  ciclo feedback → QualityMonitor → PromptOptimizer estaba cortado (ISSUE-052).
- ✅ **Confianza calibrada** (`src/utils/confidence.py`): 60% score del validador
  (0-10) + 40% agente, techo 0.45 si la validación falló, bonus por grounding y
  **sin inventar nada** si no hay evidencia. Verificado antes de tocar que
  `Response.confidence` **no decide** nada en el pipeline (ISSUE-056).
- ✅ **Badge de salud** en el header (🟢/🟡/🔴) con motivos en el tooltip y
  **sin polling** (ISSUE-053). Detección colateral: un `ui.timer(0.1)` latía
  siempre con la cola de UI vacía → ahora duerme cuando no hay trabajo.
- ✅ **Gestor de sesiones** (🗂️): listar, buscar (sin acentos), exportar
  (MD/HTML/JSON) y borrar con confirmación; **no deja borrar la sesión en uso**
  (ISSUE-054).
- ✅ **Export universal**: conversación completa (usuario + asistente + fuentes +
  **código generado**) en Markdown, HTML imprimible y JSON; desde el header y
  desde el gestor. Conversor Markdown→HTML **compartido** para no duplicar el
  escapado (ISSUE-055).

**Calidad e infraestructura**
- ✅ **`config/models.yaml` saneado**: 14 → 2 claves raíz, **0 sin lector**
  (ISSUE-041). `task_temperatures` no se "cableó": se movió al mecanismo que ya
  existía (`task_mappings.<tarea>.temperature`), con etiquetas canónicas.
- ✅ **Regla de precedencia de temperatura escrita y testeada** (ISSUE-048): un
  mapeo sin `temperature:` ya **no fuerza 0.7** — hereda la del modelo.
- ✅ **Validador de `models.yaml`** como test que deriva la verdad del AST del
  loader (claves raíz sin lector, labels no canónicos, modelos inexistentes,
  fallbacks, rangos) (ISSUE-042).
- ✅ **Gate de calidad + CI**: 13 condiciones (SCORE 100, HIGH=0, 4 detectores
  protegidos, `pytest exitstatus` real, `run_id`, frescura del health report).
  Verificado que **bloquea en 9 escenarios** mutados (ISSUE-043).
- ✅ **Observabilidad**: `/metrics/degradation` y `/feedback/stats`; `/stats` dejó
  de ser un stub de ceros (ISSUE-044/045). Bug real encontrado: la señal de
  feedback se serializaba como `"ImplicitSignal.ROUTED"` (nombre) y al recargar
  de la DB la clasificación fallaba en silencio.
- ✅ **Frescura automatizada del health report** (hook de arranque, sin bloquear)
  y **poda de reportes**: el auditor acumulaba 51 reportes (~5 MB) sin limpiar
  (ISSUE-047/058).
- ✅ **Tests fusionados por tema: 30 → 17 archivos**, ninguno por encima de
  **950 líneas**, con la política enforceada por test (R13/ISSUE-057). 768 tests.
- 📋 **Plan de distribución guardado para el futuro**
  (`docs/PLAN_DISTRIBUCION.md`): contenedores + instalación de modelos sin
  incluirlos en la imagen, analizando cómo lo resuelve Odysseus. **No implementar
  todavía.**

**Documentación**
- ✅ `docs/AGENT_GUIDE.md` (guía operativa del agente: estado verificado, comandos,
  reglas duras con dueño y test, trampas conocidas, índice ISSUE→archivo).
- ✅ `docs/ISSUES_REPORT.md` (solo pendientes) + `docs/ISSUES_HISTORY.md`
  (historial completo `ISSUE-001…058`).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9i — Clasificador kNN (bge-m3): implementado y MEDIDO → flag OFF — 2026-09-06

- ✅ **Implementación** (`classifier.py`): kNN por embeddings sobre 182
  ejemplos por tarea (TRAINING_EXAMPLES + chat) cacheados en
  `data/cache/classifier_knn.json` (batch ~9.5 s una vez) + coseno; se insertó
  ANTES del LLM en los keyword-miss. El LLM queda como fallback.
- 🔬 **Medición real (20 consultas de la batería)**:
  - Aciertos: **14/20 (70%)** con conf 0.72-0.90 (tiempo ~130-160 ms/query vs
    ~15 000 ms del LLM).
  - Errores en tareas de código CERCANAS: cola→autonomous_task,
    optimizá→data_analysis, documentá→code_debugging, race→code_correction,
    corregí→code_debugging, noticias→news_query.
- 📌 **Conclusión**: 70% con confidencias altas pero erróneas degradaría el
  ruteo (contra tu criterio de "misma o mejor calidad") → **default OFF**
  (`classifier_knn_enabled=False`); el LLM sigue primario. El kNN queda
  disponible para experimentar (mejorar ejemplos por tarea) y como base de un
  futuro ruteo híbrido. Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9h — Juez = chat: implementado y MEDIDO → se mantiene coder — 2026-09-06

- ✅ **Implementación opt-in**: setting `validator_model` (vacío = llm_fallback_1).
- 🔬 **Medición real (4 casos, 2 jueces, Ollama)**:
  | Caso | coder 7b (actual) | chat qwen3:8b |
  |---|---|---|
  | Explicación correcta (azul) | PASS 10.0 (50 s) | **FAIL por timeout (99 s)** |
  | Alucinación (Francia→Roma) | FAIL 0.0 ✓ (30 s) | FAIL 2.5 ✓ (82 s) |
  | 2+2 | PASS 10.0 (23 s) | PASS 10.0 (68 s) |
  | Función (correcta) | PASS 10.0 (31 s) | **FAIL por timeout (90 s)** |
- 📌 **Conclusión**: el juez de chat detecta igual pero tarda **2×** (68-99 s vs
  22-50 s) y con el timeout de 90 s falló 2/4 → **se mantiene qwen2.5-coder:7b
  como juez por defecto**. El setting queda como opt-in para experimentar
  (VALIDATOR_MODEL=qwen3:8b + subir timeout del validador).
- Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9g — Fix short_term expirada para siempre + traza de validación dinámica — 2026-09-06

- ✅ **Diagnóstico (log 15:21-17:01, batería)**: los fixes 9e/9f funcionaron
  (9/9 prompts rutean al task correcto y completan). Quedaron 2 issues:
  1. **10× "add — short_term ✗"** (cluster 16:24): el TTL de ShortTermMemory
     es por CREACIÓN y nunca se renovaba → tras ~1 h la ventana quedaba
     "expirada para siempre" y todos los adds fallaban (contexto caía a
     long_term).
  2. **8× SANDBOX_EXECUTION FAILED** sin detalle en el evento.
- ✅ **Fix 1** (`short_term.py`): al expirar, la ventana se RENUEVA
  (`_created_at = now` tras limpiar) — los mensajes viejos se descartan y la
  memoria sigue funcionando en sesiones largas. +1 test.
- ✅ **Fix 2** (`code_validator.py`): el fallo de la capa dinámica ahora
  loguea `[CodeValidator] dynamic FAIL: <primer error 300 chars>` — para
  diagnosticar los 8 fallos de tests en la próxima corrida.
- Suites memoria/router ✓ (76). Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9f — Fix batería re-run (log 14:38-15:02): timeout de retry + orden clasificador — 2026-09-06

- ✅ **Diagnóstico (log 14:38-15:02)**: con el ruteo de 9e activo, LRU entró
  bien (code_generation, 185 s, ok) pero:
  1. "Diseñá un sistema de eventos bus pub/sub…" → code_generation, 1er
     intento 168 s con **SyntaxError** ("await" fuera de función) → retry con
     timeout del optimizer de **90 s** → ReadTimeout → "Failed after 2
     attempts" → request fallida (0 chars, 276 s).
  2. "9. Generá una función… carrito…" → se clasificó **code_explanation**
     (keywords 0.80) y el override la mandó a code_review (análisis en vez de
     generar el código).
- ✅ **Fix 1** (`coder.py`): los reintentos (attempt>1) ahora usan
  `ollama_timeout_code` (420 s) en vez del timeout del optimizer (90 s) — en
  CPU una generación tarda 60-170 s; antes TODA retry moría por timeout.
- ✅ **Fix 2** (`classifier_core.py`): el compound code_verb+context ahora se
  evalúa **primero** (antes de code_explanation y quality) — una orden de
  GENERAR código ya no puede perder contra un FP de explanation/quality.
  + variantes voseo en QUALITY_VALIDATION_KEYWORDS (validá/verificá/evaluá).
- Verificado offline: carrito→code_generation, explicación→code_explanation,
  "validá la calidad"→quality_validation. Suites 03/02 ✓ (69). Sin commit (lo
  hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9e — Fix batería de calidad de código (prueba codigo.txt): ruteo — 2026-09-06

- ✅ **Diagnóstico (log 12:54-14:25 + DB)**: los prompts de código POR
  DESCRIPCIÓN (sin sintaxis inline) se ruteaban mal:
  - "Implementá una clase LRUCache…", cola de tareas, cliente HTTP, race
    condition → **general_chat** (~195 s y respuesta de error) porque
    CODE_VERBS no tenía implementá/diseñá/… y faltaba contexto de código.
  - "Diseñá un sistema de eventos bus pub/sub…" → **finance_query** por un
    falso positivo del patrón `cripto` (matcheaba dentro de
    "su**script**ores").
  - "Generá tests pytest… (deba validar saldo…)" → **quality_validation**
    (keyword "validar" ganaba antes del compound code_verb+context) → el
    ValidatorAgent validaba el prompt en vez de generar los tests.
- ✅ **Fixes** (`classifier_core.py` + `classifier_api.py`):
  1. CODE_VERBS: +implementá/implementa/diseñá/diseña/construí/construye.
  2. CODE_CONTEXT: +tests/pytest/race condition/cola de tareas/cliente
     http/httpx/circuit breaker/handlers/pub/sub/topic.
  3. Compound code_verb+context ahora se evalúa ANTES de
     QUALITY_VALIDATION (generar tests ya no lo pisa "validar").
  4. KEYWORD_MAP: +race condition/encontrala → code_debugging,
     +optimizá → code_optimization.
  5. Patrón finance `cripto` → `\bcripto…` (palabra completa) — ya no
     matchea "suscriptores".
- ✅ Verificado: los 9 prompts de la batería rutean al task correcto
  (code_generation .85 directo, code_debugging/code_optimization), eventos
  pub/sub ya NO es finance, bitcoin sigue finance. +2 tests de regresión.
- Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9d — Documentador: entry-points conectados + cableado de quality_monitor y security_listener — 2026-09-05

- ✅ **Mejora 1 — axioma_doc_flow_mapper**: los archivos con guard
  `if __name__ == "__main__":` se tratan como ENTRY-POINTS conectados (mismo
  criterio que __init__.py y subprocess) — antes el reporte marcaba como
  "aislados" todos los CLI tools (`python tools/x.py`, Rafael.py, etc.).
  Resultado: aislados **15 → 3** (quality_monitor, security_listener,
  el detector de estructura queda congelado por decisión).
- ✅ **quality_monitor.py — CABLEADO (no eliminado)**: lógica única (detección
  de degradación, alertas por score 0-10, con tests) — no es reemplazada por
  FeedbackLoop/metrics. Nuevo hook en `dispatcher_process`: tras la validación
  single-pass del chat, el score real se alimenta vía
  `_qm_record()` fire-and-forget (nunca bloquea; fallos solo DEBUG).
- ✅ **security_listener.py — CABLEADO (no eliminado)**: observador PASIVO de
  eventos de seguridad del EventBus (tool denies, circuit open, sandbox
  fail-closed, file ops, mode switch). Los emisores YA existían en runtime
  (tool_executor, sandbox, promotion_gate, dispatcher_process) — faltaba
  arrancar el consumidor: ahora se inicia en `Dispatcher.__init__` (cubre web
  y chat). **Garantía anti-bloqueo verificada**: el módulo no contiene ningún
  raise/deny/block — solo loguea, audita (FileAuditTrail) y cuenta. Nunca
  puede bloquear peticiones legítimas.
- Suites (02/07/08): 83 ✓. Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9c — M1 retención de memoria + M2 telemetría (health report) — 2026-09-05

- ✅ **M1 — Retención** (`memory/gateway.py` + setting `memory_retention_days`,
  default 0 = off): poda en background (executor, no bloquea init) de
  mensajes/sesiones más viejos que N días. La fecha sale del session_id
  (20YYMMDD: saintwick_20260905, nicegui_20260904_…); sesiones sin fecha
  (coder_*, voice-*, tests) NO se tocan. Borra en lotes de 200 de
  `messages` + `sessions`. +2 tests.
- ✅ **M2 — Telemetría de memoria en `log_health_report.py`**: nueva sección
  "🧠 Memoria (ops MEMORY)" — writes/reads por capa con avg ms, embeddings y
  promedio, top de operaciones (init/add/get_recent/search_semantic…).
  Verificado sobre logs reales (5 embeddings ~296 ms, search_semantic
  ~157 ms). Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9b — Fix: crítica sembrada se descartaba en attempt=1 (F + repair Fase 4) — 2026-09-05

- ✅ **Verificación de las levas A-G (v0.6.9a)**: A/B/C/D/E/G correctas;
  F tenía un bug real — `_build_prompt` solo inyectaba la crítica con
  `attempt > 1`, pero el refine (F) y el repair externo de Fase 4 llaman a
  `CoderAgent.execute` con attempt=1 → la crítica se descartaba y la
  regeneración salía **idéntica** a la original (esto explica los retries
  cacheados sin progreso vistos en logs anteriores).
- ✅ **Fix** (`coder.py`): la crítica se aplica SIEMPRE que venga
  (`if previous_critique:`) — los retries internos la pasan en attempt>1
  (sin cambio), y la semilla externa ahora entra en el primer intento.
  `is_retry`/`quality_score` del system prompt también responden a crítica
  presente (modo revisión). Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.9a — Calidad de código: lote bajo riesgo (A·B·C·D) + riesgo medio con flags (E·F·G) — 2026-09-05

- ✅ **A — Scoring real por AST** (`coder_core.analyze_code_quality`):
  `has_type_hints` (funciones/métodos con anotaciones o AnnAssign) y
  `tests_count` (asserts reales por bloque). Pesos recalibrados (suma 1.0):
  syntax .40 · docstrings .20 · **type_hints .05** · **tests .15** (+mitad si
  ≥2 asserts) · estructura .10 · error_handling .10.
- ✅ **B — `reflection` documentado como no-cableado** en coder.yaml (comentario;
  no hay usos en runtime — se conserva como especificación).
- ✅ **C — Post-proceso del contrato**: `_strip_outer_fence()` limpia fences
  ``` alrededor de SALIDA ESPERADA al parsear (antes quedaba ```plaintext en
  expected_output).
- ✅ **D — Telemetría de calidad**: el evento CODE_QUALITY ahora incluye
  has_type_hints/has_tests/tests_count, y `log_health_report.py` (E2) agrega
  la sección "🧪 Calidad de código generado" (score promedio, % type
  hints/tests/docstrings, asserts promedio, máx. intentos).
- ✅ **E — (flag OFF) `code_require_tests_auto`**: exige TESTS cuando la
  generación es código EJECUTABLE (no explicación) aunque
  code_require_tests=False → refuerza la capa dinámica real (pytest sandbox).
- ✅ **F — (flag OFF) `plan_refine_enabled` + `plan_refine_min_score`**:
  refinamiento dirigido en /plan — si el validador final da score < mínimo con
  crítica accionable, UNA segunda pasada coder con esa crítica (via
  `metadata.previous_critique` leído por _handle_code) y revalidación.
  Verificado offline: 8.5 → 2 pases → 9.5.
- ✅ **G — template**: constraint "tests con ≥2 asserts y casos de borde
  (negativos, cero, vacíos, errores) — no solo el caso feliz".
- Todos los cambios con default OFF o aditivos (no alteran el flujo actual).
  Suites core/agents/regresiones ✓ (76). Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.8zz — Fix sandbox: tests generados sin import (NameError) + guard sin-progreso — 2026-09-05

- ✅ **Diagnóstico (log 17:17 + reproducción offline)**: el `SANDBOX_EXECUTION
  FAILED` ×4 en el repair loop del CoderAgent NO era del entorno — el sandbox
  funciona (venv + pytest, verificado corriendo el contrato offline). Causa
  raíz: los **tests generados no importan el módulo** bajo test
  (`def test_sumar(): assert sumar(...)` sin `from suma_numeros import
  sumar`) → `NameError` en pytest → falla dinámica → el retry con la MISMA
  crítica regeneraba el MISMO código (caché) → 4 vueltas idénticas.
- ✅ **Fix 1 — template del coder** (`src/prompts/templates/coder.yaml`): el
  ejemplo de TESTS ahora muestra el `from <módulo> import <función>` como
  primera línea + constraint explícito ("los tests corren en una carpeta
  propia y NO ven los nombres sin ese import").
- ✅ **Fix 2 — guard sin-progreso** (`_handle_code` Fase 4): fingerprint
  sha256 del artefacto (código+tests); si un reintento produce un artefacto
  idéntico al anterior, corta el repair loop (`validation_stalled=True`) —
  espejo del guard no_progress del InterpreterLoop.
- Verificación: template con import ✓, reproducción offline de la falla
  original (NameError) y del caso OK ✓, suites core/agents/regresiones ✓.
- Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.8yy — Fix /plan: el validador reemplazaba el artefacto ("respondía cualquier cosa") — 2026-09-05

- ✅ **Diagnóstico en vivo (log 17:17)**: el pipeline `/plan [coder, validator]`
  corría bien (F3 pipeline start → coder 58 s → validator PASS 10.0 → 121 s)
  pero la respuesta final era **el análisis del validador**
  ("1. ANÁLISIS: El usuario pregunta por la validación de un código…") y no el
  programa. `execute_pipeline` encadena contenido paso a paso: el paso
  `validator` pisaba el artefacto del paso `coder`.
- ✅ **Fix** (`execute_pipeline`): `validator` ahora es una validación FINAL
  que **conserva el artefacto** del paso productor y deja el veredicto en
  `metadata.pipeline_validator` = {passed, score, note}. Solo devuelve su
  contenido si no hay artefacto previo. +1 test de regresión (artefacto
  conservado + veredicto en metadata). La crítica ya no se responde como
  contenido.
- ⚠️ Nota: el paso coder aún muestra 4× `SANDBOX_EXECUTION FAILED` (repair
  loop con regen idéntica cacheada) — falla ambiental del sandbox, separada
  de este fix. Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.8xx — Fix /plan: CODE_AUTONOMOUS no mapeaba roles → caía al fallback atómico — 2026-09-04

- ✅ **Diagnóstico en vivo (log 19:14)**: `/plan generá un programa…` ya llegaba
  al dispatcher (ww) pero **no armaba pipeline**: el gate pasa
  `task_type="code_autonomous"` y `TaskDecomposer` NO tiene entrada para
  CODE_AUTONOMOUS en `_TASK_TO_AGENT` → descompone en 1 subtarea
  ORCHESTRATOR (sin rol de pipeline) → `_plan_pipeline_for` devolvía None →
  fallback atómico → se generaba código normal (106 s, sin validación final
  del pipeline).
- ✅ **Fix** (`_plan_pipeline_for`): reintento heurístico cuando el primer
  mapeo no produce roles — pistas de código (programa/python/función/…) →
  CODE_GENERATION → `[coder, validator]`; pistas de búsqueda/investigación →
  WEB_SEARCH → `[researcher, validator]`; chat puro → None (ruta normal).
  Verificado: /plan código → [coder, validator]; /plan investigá → [researcher,
  validator]; /plan haiku → None. +1 test de regresión.
- Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.8ww — Fix: /plan interceptado por la UI como "comando desconocido" — 2026-09-04

- ✅ **Causa raíz (prueba en vivo)**: `/plan generá un programa…` respondía
  `❌ Comando desconocido: /plan` — el UI web (`chat.py::_dispatch_input`) y el
  CLI interactivo (`cli/main.py`) interceptan TODO lo que empieza con `/` y lo
  mandan al `CommandParser`, que no conoce `/plan` → **nunca llegaba al
  dispatcher** (por eso no aparecía nada en logs).
- ✅ **Fix**: `/plan` (y `/plan <consulta>`) se deja **pasar como mensaje
  normal** en ambos puntos (matching con límite de palabra: `/planear` sigue
  siendo comando desconocido, no se come como `/plan`). El dispatcher lo
  detecta por el prefijo y arma el pipeline F3. `/plan` solo → mensaje de uso.
- Verificado por lógica: `/plan…` → dispatcher; `/modo`, `/rac`, `/help` →
  CommandParser como antes. Sin commit (lo hace el usuario).

## [Unreleased] — En Desarrollo

### 🟢 v0.6.8vv — E2: auto-reporte de salud desde logs — 2026-09-04

- ✅ **`tools/log_health_report.py`** — lee logs/reg_error.txt (detailed_logger)
  y genera `logs/axioma_health_<fecha>.md` con el estado real del sistema:
  - Requests por task_type: count/min/mediana/p95/max/ok%.
  - **Hit-rate L3 (C1)** = L3 partial / (partial + pre-routing + clasif LLM)
    — responde "cuántos misses productivos pegan al caché semántico".
  - Validador: intentos PASS/FAIL %, score promedio, single-pass FAILs,
    skips A1 (short/grounded) y grounding_missing.
  - Tools (B2): tasa de tool-call en el Agent Loop + tools ejecutadas.
  - Modelos (generaciones y duración media) y errores/circuit breakers.
  - +2 tests unitarios del parser (test_axioma_08).
- Uso: `python tools/log_health_report.py [--logs a.txt] [-o salida.md]`.
- Sin commit (lo hace el usuario).


### 🟢 v0.6.8uu — Refactor del auditor: paquete tools/auditor (5 módulos) — 2026-09-04

- ✅ **`axioma_auditor.py` (3112 L) → paquete `tools/auditor/`** con 5 módulos
  ≤900 líneas c/u (mantenibilidad):
  - `aud_core.py` (327L) — Verdict/Severity/Finding/DetectorResult, resolvers de
    imports, `CodeIndex`, `BaseDetector`.
  - `aud_detectors_infra.py` (899L) — LRU, CB cosmético, caches, fire-and-forget,
    dead code, cascadas, timeouts, colas, colisiones de nombres.
  - `aud_detectors_salud.py` (639L) — sync-blocking, índices, FIFO, sintaxis,
    excepciones silenciosas, entorno/modelos.
  - `aud_detectors_arquitectura.py` (884L) — security harness, plan v2,
    config refs, singletons, asyncio módulo, imports circulares, duplicados,
    integración.
  - `aud_main.py` (498L) — registro `DETECTORS`, `AxiomaAuditor`,
    `compute_score`, reporte y CLI `main()`.
- ✅ **Compatibilidad intacta**: `axioma_auditor.py` raíz quedó como shim
  (CLI `python axioma_auditor.py --full [--run-tests] [--detectors X]` y
  `from axioma_auditor import CodeIndex/AxiomaAuditor/...` siguen
  funcionando — coder.py y el check del plan v2 no se tocan).
- ✅ **Nombres sin colisión** (v0.6.8uu, corrección): los tipos del paquete
  se renombraron `Verdict/Severity/Finding` → **`AudVerdict/AudSeverity/
  AudFinding`** porque AXIOMA ya tiene `promotion_gate.Verdict` y
  `security_audit.Severity/Finding`. Módulos con prefijo `aud_`:
  `aud_core`, `aud_detectors_infra`, `aud_detectors_salud`,
  `aud_detectors_arquitectura`, `aud_main`. El shim raíz sigue exportando
  los alias históricos (`axioma_auditor.Severity` etc.) sin colisionar.
- ✅ **Equivalencia verificada** detector por detector: ENV/INT/SNG/SP
  idénticos; PERF −8 y SIL −2 (el auditor ya no se auto-flaggea como
  archivo pesado ni con sus propios silencios). SCORE 96/100, 0 CRITICAL/
  HIGH. Suite pytest completa ✓. Sin commit (lo hace el usuario).

### 🟢 v0.6.8tt — CIR-001 + D2 extendido repo-wide — 2026-09-04

- ✅ **CIR-001 (falso positivo corregido)** — `axioma_auditor.py`: el grafo
  de imports circulares contaba imports LAZY (dentro de métodos,
  dispatcher.py:119) y de TIPADO (`if TYPE_CHECKING:`, jarvis_engine.py:26)
  como dependencias de runtime → marcaba un ciclo falso
  dispatcher → jarvis_engine → dispatcher. Verificado en runtime: ambos
  módulos importan limpio. Nuevo `_resolve_top_level_imports_for()` solo
  considera imports a nivel de módulo (sin funciones/clases ni ramas
  TYPE_CHECKING). Detector circular_import: 1 hallazgo → **0**.
- ✅ **D2 extendido repo-wide** — 294 `except: pass` vacíos transformados a
  `logging.getLogger(__name__).debug(...)` en **103 archivos** (pase 1: 218
  sitios en 81 archivos con `import logging`; pase 2: +76 sitios en 22
  scripts, agregando `import logging` donde faltaba — con corrección del
  orden de `from __future__` en 4 archivos). Sin cambio de lógica; los
  fallos degradados ahora quedan trazables en DEBUG.
  Efecto en el detector `silent_exceptions`: MEDIUM **48 → 10** (el resto
  son excepts NO vacíos con lógica pero sin log — requieren juicio por
  sitio, fuera de alcance automático). Excluidos: herramientas experimentales aisladas,
  tests/, axioma_auditor.py (auto-referencia), venv/dsh.
- Suite completa ✓. Sin commit (lo hace el usuario).

### 🟢 v0.6.8ss — Ronda de optimización recomendada (B1·A1·E1·D2·B2·A3) — 2026-09-04

- ✅ **B1 — Fix de extracción de score del validador** (`agents/validator.py`):
  el LLM emitía `**SCORE:** 10.0` (markdown) y el regex estricto `SCORE:\s*`
  no matcheaba → fallback fijo 5.0 → FAIL fantasma de respuestas buenas
  (log 15:24: critique "**SCORE**: 10.0" con score 5.0). Ahora:
  `SCORE_PATTERN`/`VERDICT_PATTERN`/`CRITIQUE_PATTERN` toleran markdown/
  espacios/saltos; fallback de score SIMÉTRICO al veredicto (APROBADO→8.0,
  RECHAZADO→4.0) en vez del 5.0 duro; fallback de veredicto protegido contra
  negación ("no fue APROBADO"). +6 tests unitarios. NO se bajó el threshold.
- ✅ **A1 — Skip de validación innecesaria** (`dispatcher_process.py`): el
  validador (~43 s CPU) ya no corre para respuestas triviales (≤80 chars:
  ecos, saludos, fórmulas) ni para respuestas con `sources` reales (grounded).
  Marcado `validation_skipped_short/grounded` + evento `Chat validation
  SKIPPED (A1)`. El skip inverso (carpeta sin sources) y el single-pass
  siguen intactos.
- ✅ **E1 — SCORE del auditor sensible a seguridad** (`axioma_auditor.py`):
  `compute_score()` penalizaba por detector (syntax/async/singleton/dup/
  circular) — un HIGH de `plan_v2_protections` NO movía el SCORE (métrica
  ciega a regresiones de seguridad). Ahora cualquier HIGH/CRITICAL de
  detectores no cubiertos resta −10 c/u. Verificado: 0 HIGH → 88 intacto;
  1 HIGH → 90; 2 → 80.
- ✅ **D2 — except:pass silenciosos → logger.debug** en hot paths:
  dispatcher_process (11), chat.py (5), dispatcher_handlers_extra (1):
  los bloques `except: pass` vacíos ahora loguean en debug con el contexto
  del archivo/línea (degradación intencional documentada). Sin cambio de
  lógica.
- ✅ **B2 — Gate por verbo + ejemplos de tool-call** (`dispatcher_handlers_extra
  ._handle_chat`): qwen3:8b casi nunca llamaba tools. Ahora las tools se
  exponen SOLO si la consulta sugiere necesidad (verbos/contextos: buscar,
  noticia, clima, archivo, fs_*, contenido…) → chat puro = 1 generación sin
  schema extra; cuando están activas, el system prompt incluye ejemplos
  concretos cuándo/cómo llamar web_search/news/fs_search+fs_read. Métrica:
  `tools_executed` en el evento y `metadata.chat_tool_calls` en la respuesta.
- ✅ **A3 — Streaming de tokens a la UI (opt-in)** (`llm/client.py` +
  `dispatcher_handlers_extra.py` + `chat.py` + `web/app.py`): nuevo
  `OllamaClient.chat_stream()` (/api/chat stream) + helper
  `_stream_chat_response()` que cede chunks vía asyncio.to_thread sin
  bloquear el loop + wiring en send_message (placeholder de streaming →
  finalize/remove). DETRÁS de `settings.stream_chat_tokens` (default
  **False**): requiere prueba en vivo del pool de sesiones; cualquier error
  → fallback a respuesta completa (nunca rompe el chat). El frontend
  (add_streaming_placeholder/update/finalize) existía sin callers.
- Herramientas experimentales aisladas: sin cambios (fuera de alcance).
- Suites core/router/agents/web/regresiones/plan_v2 ✓. Sin commit (lo hace el usuario).

### 🟢 v0.6.8rr — F3 opt-in: pipeline multi-paso `/plan` (PLAN_ADAPTADO.md) — 2026-09-04

- ✅ **Comando `/plan <consulta>`** (`dispatcher_process.py`, antes del
  clasificador LLM): descompone la tarea (heurístico, gratis, sin LLM) y
  ejecuta `execute_pipeline` con cap `settings.plan_max_steps` (default 2).
  Pipeline determinístico: `[rol_principal, "validator"]` — coder para
  tareas code, researcher para research/búsqueda; **validador SOLO como paso
  final** ("validar el resultado final", nunca por paso intermedio).
- ✅ **Auto-pipeline opt-in**: `settings.plan_pipeline_enabled` (default
  **False**) + consulta ≥40 chars → intenta pipeline tras la clasificación;
  si no mapea roles (chat puro) sigue la ruta normal sin costo extra.
  Nunca en el flujo normal por defecto (F3 multiplicaría latencia 2-4× en CPU).
- ✅ **Seguridad/guard**: los pasos de pipeline (`metadata.pipeline_step`)
  NUNCA derivan a modo autónomo/InterpreterLoop — el pipeline es una cadena
  de generaciones, no ejecución en sandbox. Sin este guard, un `/plan …probá
  X…` habría lanzado el sandbox desde el paso coder.
- ✅ `/plan` sin consulta → mensaje de uso (sin LLM). Consulta atómica o sin
  roles de pipeline → se procesa por la ruta normal (el prefijo se descarta).
- Suites core/router/regresiones/plan_v2 + tests F3 nuevos ✓.
- Sin commit (lo hace el usuario).

### 🟢 v0.6.8qq — Pre-routing determinístico (sin clasificador LLM) — 2026-09-04

- ✅ **Los ~15 s muertos del clasificador LLM** (logs 15:26): "usá fs_search
  para…" pagaba ~15 s de qwen3:8b clasificando (→ web_search) para que el
  override post-clasificación lo corrigiera a file_search. Lo mismo con
  "analizá la carpeta X" y comandos de sistema.
- ✅ **PRE-ROUTING determinístico en `dispatcher_process.py`** (antes de
  `classifier.classify()`): si la consulta matchea `_is_fs_tool_intent`
  (→ file_search), `_is_folder_analysis_query` (→ file_search) o
  `_is_system_command_intent` (→ system_control, 1ª palabra en
  DISPATCHER_ROUTING_VERBS), se **salta la generación LLM del clasificador**
  (conf 0.9, evento `Classification (pre-routing)` con `llm_skipped: True`).
  Intenciones determinísticas no necesitan LLM: resuelven en µs.
  - Efecto medido esperado: consulta fs_search 15.3 s → ~0.3 s; el eco de
    "escribí en una línea…" (general_chat puro) NO se ve afectado porque no
    matchea ningún patrón determinístico (sigue por clasificador LLM).
- Overrides post-clasificación intactos para el resto (code→analysis, fs_*,
  veredicto con propuesta pendiente). Suites core/router/regresiones ✓.
- Sin commit (lo hace el usuario).

### 🟢 v0.6.8pp — Routing fs_* + validador single-pass + fix code_testing (logs 15:03) — 2026-09-04

- ✅ **(C) Intención fs_* / "archivos que hablan de X" → file_search grounded**
  (`dispatcher_handlers.py` + `dispatcher_process.py`): "usá fs_search para
  buscar qué archivos de tools/auditor hablan del SCORE…" caía a web_search /
  general_chat → 43-47 s de síntesis LLM al pedo. Nuevos helpers
  `_is_fs_tool_intent()` / `_extract_content_keyword()` + override de routing
  (`→ file_search (fs_* / content)`) + handler `_handle_file_content_search()`:
  grep local del contenido real (fs_list + fs_read, ≤60 archivos/carpeta, cap
  12 hits) con snippet de la línea que matchea. CERO generación LLM: respuesta
  grounded en <1 s con `sources`. Queda el flujo por nombre si no hay carpeta/
  tema identificable.
- ✅ **Validador SINGLE-PASS** (`dispatcher_process.py`): el validador
  (qwen2.5-coder:7b) suele marcar FAIL respuestas cortas y correctas (scores
  2.0-7.0). ANTES: FAIL → re-generar con fallback (+20-90 s) → re-validar
  (+20-90 s) → a veces FAIL otra vez (caso pico ~391 s). AHORA: una sola
  validación; si falla se conserva la respuesta ORIGINAL marcada
  `validation_single_pass=True` — sin re-generar ni re-validar.
- ✅ **Diagnóstico y fix del caso `code_testing` 234 s (15:03)**:
  "escribí en una línea: \"prueba de historial demo-123\"" (texto plano) →
  1) `KEYWORD_MAP["prueba"]` matcheó dentro del TEXTO CITADO → code_testing
  (conf 0.60); 2) `_should_use_autonomous_mode()` también matcheó "prueba" →
  InterpreterLoop; 3) el modelo generó un parser sobre-diseñado que ni su
  propio ejemplo corre (4 tokens vs `len != 3`) y los intentos 3-5 repitieron
  código byte-idéntico (fix prompt idéntico → caché). Fixes:
  - Clasificador (`classifier_core.py`): el scan de KEYWORD_MAP ignora spans
    entre comillas (contenido citado ≠ orden). "prueba este script" sigue
    siendo code_testing. Se suman "probá"/"probar" (word boundary) para no
    perder el caso "probá el archivo test_x.py" al ignorar el "test" citado,
    sin FP en "comprobá/aprobá".
  - `_should_use_autonomous_mode()` (`dispatcher_handlers_extra.py`): quita el
    texto entre comillas antes de matchear keywords — una palabra citada ya no
    dispara ejecución de código.
  - `interpreter_loop.py`: guard de estancamiento `no_progress` — si un
    intento falla con el MISMO error y el MISMO código que el anterior, corta
    (otra generación produciría el mismo código roto por caché). Aplica a
    errores estáticos y de sandbox.
- Suites router/regresiones verificadas. Sin commit (lo hace el usuario).

### 🟢 v0.6.8oo — Fixes de la prueba en vivo (logs/reg_error.txt) — 2026-09-04

- ✅ **Botón "Estado" pintaba todo "❌ error"** (4× en el log): causa
  `lbl.classes(color, clear=False)` — NiceGUI no acepta `clear=` →
  TypeError → el except pintaba las 4 filas de error. Nuevo helper
  `_apply_status()` que remueve la clase anterior antes de aplicar la nueva;
  un ítem que falle ya no tumba a los demás (modals.py).
- ✅ **F1 funcionó pero explotaba el parseo de la tool**: el modelo llamó
  `fs_search` con `arguments` como DICT (Ollama no lo devuelve como string
  JSON) y `json.loads(dict)` reventaba a los ~106 s matando la respuesta.
  Ahora se acepta dict o str; además cada tool se ejecuta en try/except — una
  tool con error se inyecta como resultado y el LLM decide (no mata el chat).
- ✅ **Fix A (latencia, logs 14:18-14:30)**: el timeout de síntesis del
  SearcherAgent era 45 s < tiempo real en CPU (40-90 s) → ReadTimeout →
  reintento → fallo (~74 s). Ahora se usa `max(config, 180)` s.
- ✅ **Fix B (latencia)**: si el validador reprueba también el RETRY, ya NO se
  encadena una tercera generación (fallback llm_fallback_1 = +18-90 s en CPU).
  Se conserva la respuesta del retry marcada `validation_retry_kept=True`.
  Elimina ~54-73 s del peor caso (322 s → ~250 s aprox.).
- ✅ **Fix D (F5 real)**: id de sesión ESTABLE por usuario+día
  (`<user>_YYYYMMDD` via `_stable_session_id()` en web/app.py) en los 3 puntos
  que obtienen la sesión. ANTES cada reload generaba `nicegui_<timestamp_micro>`
  → la hidratación (LongTermMemory) nunca encontraba el historial del id viejo.
  Ahora la conversación sobrevive recargas dentro del mismo día.
- Suites web/core/cobertura: **70 ✓**; agents/tools: **52 ✓**. Sin commit (lo hace el usuario).

### 🟢 v0.6.8nn — Plan "Claude Local" adaptado: F1+F4+F2+F5 (F3 opt-in) — 2026-09-04

- ✅ **Análisis previo en `PLAN_ADAPTADO.md`**: el plan original (plan claude.txt)
  es viable en AXIOMA pero su F3 (planner en el flujo normal) multiplicaría la
  latencia 2-4× en CPU (~4-6 tok/s medidos). Se adaptó:
  - **F1 — Agent Loop con tools del registry** (`dispatcher_handlers_extra.py`):
    la lista hardcodeada `["web_search","news","weather","finance"]` ahora se
    arma desde el registry con tools SEGURAS: adapters web + lectura de
    archivos `fs_read/fs_list/fs_search` (READ_ONLY ≤ NETWORK del contexto).
    Escritura/terminal quedan fuera del chat por ahora. Prompt actualizado.
  - **F4 — fetch de página** (`agents/searcher.py`): nuevo `_fetch_page_content`
    (httpx + html.parser de stdlib, cap 1600 chars, timeout 6 s) que alimenta el
    primer resultado con texto REAL (el builder da 1200 chars al contenido
    fetcheado vs 300 a los snippets). Flag `settings.searcher_fetch_page`.
  - **F2 — memoria semántica de respuestas** (`memory/gateway.py`): el write-path
    semántico (fire-and-forget) ahora indexa `role in ("user","assistant")` y
    guarda el role real en metadata (antes solo user → el assistant nunca se
    recordaba).
  - **F5 — continuidad UI read-only** (`interfaces/web/states.py`):
    `SessionState.hydrate_from_memory()` carga el historial desde
    LongTermMemory (session_id, orden cronológico, dedupe) cuando la sesión se
    recrea vacía; se invoca en `get_current_session()`. NO escribe (la capa de
    memoria ya persiste cada turno) — evita la doble escritura del plan.
  - **F3 — NO implementada** (planner): queda como futura opt-in (`/plan` o
    setting), documentada en PLAN_ADAPTADO.md.
- Suites: memory/router/tools/web/user-e2e/log **105 ✓**; core/cobertura/plan_v2
  **52 ✓**. Sin commit (lo hace el usuario).

### 🟢 v0.6.8mm — Refactor 50/50 de dispatcher_handlers.py — 2026-09-04

- ✅ **`dispatcher_handlers.py` (1666 líneas) partido al 50%** siguiendo el
  patrón de mixins que ya usa Dispatcher:
  - **Original conservado** (`src/core/dispatcher_handlers.py`, 812 líneas):
    helpers de logging, constantes de prompts, detección de carpeta/archivo,
    `_handle_file_search` + `_handle_folder_analysis`, `_handle_system`,
    `_handle_data`, `_handle_search` y `_llm_generate_with_retry`.
  - **Archivo nuevo** (`src/core/dispatcher_handlers_extra.py`, ~944 líneas):
    `DispatcherHandlersExtraMixin` con `_handle_code`, `_handle_analysis`,
    `_handle_validation`, `_handle_chat` (+ `_format_tools_for_ollama`),
    `_handle_research`, `_handle_screenshot_analysis`,
    `_handle_autonomous_code`, `_should_use_autonomous_mode`,
    `execute_pipeline`, `_check_code_complexity`, `_error_response`.
  - **`dispatcher.py`** ahora compone ambos mixins:
    `class Dispatcher(..., DispatcherHandlersMixin, DispatcherHandlersExtraMixin, ...)`
    → API pública intacta (todos los `self._handle_*` siguen en Dispatcher).
  - Los imports externos (`dispatcher_process.py`, tests) no cambian: los
    helpers/constantes se quedan en el original y el nuevo archivo los
    importa (sin ciclos: el original no importa el nuevo).
- ✅ **Tests actualizados**: `test_axioma_08` inspecciona ambas mitades para
  `ollama_timeout_chat`. Corridas: core/regresiones/cobertura/plan_v2
  **59 ✓**, router/agentes/tools/web **123 ✓**.
- Sin commit (lo hace el usuario).

### 🟢 v0.6.8ll — Modales: fuente única en modals.py (app.py los consume) — 2026-09-03

- ✅ **Refactor de cableado**: los 4 diálogos de la UI (Estado/Configuración/
  Archivos/Salida) vivían DUPLICADOS en `web/app.py` y en clases legacy de
  `components/modals.py` (~500 líneas muertas con refresh roto). Ahora:
  - `modals.py` es la **fuente única** (builders `_build_status_dialog`,
    `_build_settings_dialog`, `_build_file_dialog`, `_build_exit_dialog` +
    factories `create_status_dialog`, `create_settings_dialog`,
    `create_file_dialog`, `create_exit_dialog`).
  - `app.py` **eliminó sus builders locales** (1167 → 843 líneas) y consume
    las factories vía `from ..components.modals import ...`.
  - Las clases legacy (`StatusDialog`/`SettingsDialog`/`ExitDialog`) quedaron
    como **shims de compatibilidad** que delegan en las factories (sin UI
    propia) para no romper la API exportada por `components/__init__.py`.
- ✅ **Eficiencia (modals.py)**:
  - Refresh de Estado **incremental**: filas creadas una sola vez y solo se
    actualiza texto/clase por refresh (antes `clear()`+rebuild total cada vez).
  - Guard anti-reentrada en refresh (una sola llamada HTTP por refresh).
  - Handlers async nativos de NiceGUI; sin `dialog.on('open')` (evento que no
    existe) ni `create_task` suelto sin contexto.
  - Construcción única por página (el dialog se reutiliza en open/close).
- ✅ **Tests**: contrato actualizado en `test_axioma_06` (app importa de
  modals, sin builders locales; modals expone `dialog._refresh`). File: 36 ✓.
- Sin commit (lo hace el usuario).

### 🟢 v0.6.8kk — Telemetría: un solo evento MODEL canónico por generate (tokens reales) — 2026-09-03

- ✅ **Problema (logs/reg_error.txt)**: un mismo `generate` aparecía 2-3 veces en
  [MODEL] con conteos inconsistentes, y el inicio reportaba `input_tokens: 1816`
  cuando en realidad eran **caracteres** (`len(prompt)`); el completion real
  decía `637` tokens.
- ✅ **`OllamaClientBase.generate`**: eliminado el evento MODEL de START con
  `input_size=len(prompt)` (caracteres como tokens). Ahora:
  - empezar/terminar quedan en `OLLAMA_CLIENT` ("Generate started" /
    "Generate completed successfully");
  - el evento [MODEL] es **ÚNICO por generate**, al completar, con **tokens
    reales de Ollama** (`prompt_eval_count` / `eval_count` / `tokens_used`).
- ✅ **`agents/base.py`**: quitado el log AGENT duplicado de `generate`/
  `generate_stream` con `len(prompt)`/`len(content)` como tokens (los errores
  sí se loguean con contexto AGENT).
- ✅ **`llm/client.py`**: los starts de `generate_stream` y `chat` ya no
  etiquetan caracteres como `input_tokens` (el completion de chat mantiene su
  condición DEBUG original con tokens reales).
- ✅ **`agents/searcher.py`**: el evento `synthesize` conserva duración/éxito
  sin inventar conteos (el generate interno ya emite los tokens reales).
- Suite router+agentes+tools: **87 ✓**. Sin commit (lo hace el usuario).

### 🟢 v0.6.8jj — UI web: el botón "Estado" ahora muestra el estado real — 2026-09-03

- ✅ **Bug**: el botón 🏥 "Estado" (esquina superior derecha) abría un diálogo
  VACÍO. Causa raíz: `dialog.on('open', ...)` **nunca dispara** en NiceGUI —
  `ui.dialog` no emite un evento 'open' al llamar `.open()` (su JS solo alterna
  una clase), así que `refresh_status()` jamás corría.
- ✅ **Fix (`src/interfaces/web/app.py`)**: el refresh se dispara ahora desde un
  **handler async nativo** del botón (`_on_estado_click` → `dialog.open()` +
  `await dialog._refresh()`), que sí corre en el contexto del cliente. El
  diálogo expone `dialog._refresh`; botón "Actualizar" con `on_click` async;
  guard anti-reentrada.
- ✅ **Lógica extraída y testeable** (`app_logic.collect_system_status`): puro,
  sin NiceGUI — Ollama (HTTP `/api/tags`) + API keys (tavily/news/serper),
  inyectable con `http_client` mock.
- ✅ **Mismo bug latente corregido** en `components/modals.py` (StatusDialog,
  hoy sin cablear en la UI): se eliminó el `dialog.on('open')` muerto y el
  refresh se dispara desde `open()`.
- ✅ **Tests**: +3 en `tests/test_axioma_06_web_handlers_analisis.py`
  (collect_system_status OK / Ollama caído / contrato del fix sin
  `dialog.on('open', lambda…)` y con `_on_estado_click`). File completo: 36 ✓.
- Sin commit (lo hace el usuario).

### 🟢 v0.6.8ii — Fix: análisis de carpetas con grounding (dejó de divagar) — 2026-09-03

- ✅ **Diagnóstico desde `logs/reg_error.txt`**: "analiza la carpeta auditor y
  dime qué es" / "…quiero saber qué hacen sus archivos" caían a
  `general_chat`/`code_explanation` y el modelo respondía **SIN leer el FS**
  → divagaba (83-95 s por generación, validador sellando con PASS 10/10).
- ✅ **Clasificador (`classifier_core.py`)**: fast-path "analiza/explora/
  revisa/explica + carpeta/directorio" y "qué hacen sus archivos" →
  `file_search` (keywords, sin 15 s de clasificación LLM).
- ✅ **Routing override (`dispatcher_process.py`)**: si el clasificador no dio
  `file_search` pero la consulta pide analizar una carpeta → se fuerza
  `file_search` (red de seguridad).
- ✅ **Nuevo handler `_handle_folder_analysis` (`dispatcher_handlers.py`)**:
  resuelve la carpeta (`_extract_folder_candidates`: "carpeta auditor",
  "auditor dentro de la carpeta tools", rutas `src/core`…), la lista con
  `fs_list`, lee hasta 12 archivos con `fs_read` (presupuesto ~18k chars) y
  resume con el modelo de código **anclado al contenido** (sources reales).
- ✅ **Validador con grounding**: una consulta de carpeta respondida SIN
  sources ya no se aprueba con el validador (se marca `grounding_missing` y
  se omite la auto-retry inútil).
- ✅ **Telemetría (`data_handler.py`)**: `duration_ms` del handler salía ×1000
  (ej. 1991509.0 ms para ~1991 ms reales) — los callers ya pasaban ms; se
  elimina la doble conversión.
- Verificado sin LLM: las 2 consultas del log → `file_search` (fast) y el
  handler devuelve análisis con sources reales de `tools/auditor`. Sin commit
  (lo hace el usuario).

### 🟢 v0.6.8hh — Documentación: README + COMANDOS_AXIOMA + CHANGELOG — 2026-09-03

- ✅ **README.md** actualizado (verificación 2026-09-03):
  - Métricas reales del repositorio y árbol de directorios al día.
  - Fix de markdown: bloque de código "Git/commit" sin fence abierto que
    descuadraba el resto (fences balanceados 6/6) y nota de métricas de
    estructura (08-24).
- ✅ **COMANDOS_AXIOMA.txt** reorganizado por secciones numeradas.

### 🟢 v0.6.8cc — Archivos: análisis real de PDF/DOCX + límite 150K — 2026-09-01

- ✅ **Nuevo `src/utils/file_text.py`** — extracción de TEXTO REAL de archivos:
  - `.pdf` → `pypdf` (texto por página)
  - `.docx`/`.doc` → `python-docx` (párrafos + tablas)
  - resto → texto utf-8 con `errors='ignore'`
  - Degradación silenciosa: si falta la librería o el parseo falla, cae a
    texto crudo — nunca lanza hacia el caller.
- ✅ **`/analyze_file` (routes.py)**: ANTES leía con `open(..., "r")` — un
  PDF binario crasheaba (UnicodeDecodeError) o inyectaba basura al LLM, y
  solo pasaba `content[:5000]`. AHORA usa `extract_file_text()` con límite
  **150.000 chars** — PDFs/DOCX se parsean y el modelo ve el texto real.
- ✅ **Chat GUI (chat.py)**: `MAX_FILE_CHARS` subido de **50K → 150K**; el
  adjunto de PDF/DOCX ahora se extrae con `extract_file_text()` (archivo
  temporal + parseo + cleanup) en vez de decodificar bytes crudos.
- ✅ **Dependencias**: `pypdf==6.16.2` y `python-docx==1.2.0` instaladas y
  agregadas a `requirements.txt` (sección DOCUMENT PROCESSING).
- ✅ **Tests**: `tests/test_axioma_10_plan_v2.py` + 4 tests nuevos
  (TestFileTextExtractor): PDF real parseado, DOCX real parseado, texto
  plano + límite, e integración en routes/chat.
- Suite: **258 passed** (254 + 4 nuevos). Sin commit (lo hace el usuario).

### 🟢 v0.6.8bb — Tools: análisis, fusiones y logger robusto — 2026-09-01

  herramientas redundantes en una con subcomandos:
  - Eliminados: `tools/health_checker.py`, `tools/setup_validator.py`, `tools/system_diagnostic.py`.
  - **Bug corregido en la fusión**: setup_validator verificaba `chromadb`
    (ELIMINADO del proyecto) y usaba `__import__("scikit-learn")` con guion
    (inválido) — ahora verifica `lancedb`/`pyarrow`/`sklearn` correctamente.
    (`tools.health_checker`/`setup_validator`/`system_diagnostic` resuelven a
    `from . import X` en `__getattr__` causaba RecursionError).
- ✅ **Eliminados tests obsoletos de tools/** (duplicados por la suite
  `tests/test_axioma_*.py`):
  - `tools/test_handoff_integration.py` — esperaba el handoff Searcher→Coder
    que se DESACTIVÓ en el PLAN v2.0 Fase 6 (quedó roto).
  - `tools/test_autonomous.py`, `tools/test_detect_intent.py`,
    `tools/test_jarvis_mode.py` — 0 tests pytest-collectables, entorno real.
  - `tools/caac_e2e_validation.py` — duplicaba `tests/test_code_contract_e2e.py`.
- ✅ **`tools/detailed_logger.py` v0.2.0** (usado por 59 archivos de src/):
  - Rutas portátiles derivadas de `__file__` (antes hardcodeadas a
    `/ruta/a/axioma`).
  - Singleton reiniciable (`reset()` / `_reset_singleton()`) — antes el
    segundo `initialize()` en tests fallaba silenciosamente.
  - Auto-crea `logs/` (antes exigía `mkdir -p` manual).
  - `log_feedback()` con lock propio — antes dos threads corrompían
    `feedback_log.jsonl`.
  - `track_function()` redacta kwargs sensibles (key/token/secret/password/
    credential/auth) — antes se logueaban crudos.
  - `atexit` registrado en `initialize_logger()` — el footer se escribe aunque
    el proceso muera sin `close_logger()`.
- Suite: **254 passed**. Sin commit (lo hace el usuario).

### 🟢 v0.6.8aa — Commit/push: fix de eliminaciones y renames (v0.4.5) — 2026-09-01

  (`R  old -> new`) solo se tomaba el DESTINO, descartando el ORIGEN. Un
  "create new file" sin el "delete old file" → quedaba `D  archivo` stageado
- ✅ **Nuevo `get_tracked_deleted_files()`**: la eliminación de un archivo YA
  trackeado (ej. `speedtest_history.json`) siempre debe commitearse, aunque
  el archivo esté en `DEFAULT_EXCLUDE_PATTERNS` — si no, el árbol queda
  eternamente sucio (` D archivo`) y el push se cancela. Se respeta SOLO el
  bloqueo de seguridad (`BLOCKED_FILES`/`BLOCKED_PATTERNS`): un archivo
  sensible eliminado no deja rastro de su contenido.
- ✅ **FIX en `stage_files()`**: `git add -u -- <origen>` fallaba con
  "pathspec no concordó" cuando el path ya fue removido del index por un
  `git mv` stageado. Ahora filtra contra `git ls-files` (solo se agregan
  paths que siguen en el index).
- ✅ **Verificado** en repo temporal: `git mv` + delete de trackeado →
  commit con rename completo + delete → árbol limpio.
- Suite: **254 passed**.

### 🟢 v0.6.8z — Documentador: fix de módulos conectados marcados AISLADOS — 2026-09-01

- ✅ **FIX en `tools/documentador/axioma_doc_flow_mapper.py` (`_map_dependencies`,
  v3.9.9)**: los archivos NOISE (herramientas standalone en `tools/`, listadas en
  `IGNORED_PATTERNS`) se descartaban por completo como FUENTE del grafo — por eso un
  archivo no-noise importado SOLO desde una fuente noise quedaba `[⚠️ AISLADO]` pese a
- ✅ **Comportamiento corregido**: los edges de import desde herramientas standalone
  ahora se registran en `module_graph` (prueban conexión real de producción), pero:
  - desde `tests/` **no** se registran (un módulo importado solo por un test no está
    integrado a producción — `quality_monitor.py` y `security_listener.py` siguen
    correctamente aislados);
  - los `symbol_traces` (used_in/call_sites) no se contaminan desde fuentes noise.
  ahora figura en `connected_modules`. Los 12 aislados restantes son CLIs autónomos /
  entry points legítimos (nadie los importa: `Rafael.py`, `ptt_axioma.py`,
  `test_autonomous.py`, `test_detect_intent.py`, `test_handoff_integration.py`).
- Suite: **254 passed** — sin cambios de comportamiento en runtime (solo análisis del documentador).


  (único benchmark fuera de `tools/`).
- ✅ **`CODE_CANDIDATES` actualizado**: `deepseek-r1:8b` fue reemplazado por
  `qwen3-vl:4b` — deepseek-r1:8b está descartado por el proyecto
  (settings.py:51) y no instalado; correr el bench sin `--models` fallaba con
  "Ningún modelo disponible". Ahora los candidatos son los 3 modelos
  realmente instalados (qwen2.5-coder:7b, qwen3:8b, qwen3-vl:4b).
- ✅ **`OLLAMA_HOST` leído de `settings.ollama_host`** (antes hardcodeado
  `http://127.0.0.1:11434`); si el host cambia en `.env`, el bench lo respeta.
  `CODE_TEST_CASES`/helpers ahora buscan en `tools/` primero (raíz como
  retro-compat) y registran el módulo en `sys.modules` ANTES de `exec_module`
  — fix de un bug latente: cargar el módulo por path sin registrarlo crasheaba
  con `AttributeError` en dataclasses `frozen=True`.
- Suite: **254 passed** (sin cambios de comportamiento en runtime).

### 🟢 v0.6.8x — Fusión de axioma_inspector.py en el auditor (v8.3) — 2026-09-01

- ✅ **`axioma_inspector.py` ELIMINADO** — su lógica única se fusionó en
  `axioma_auditor.py` (v8.3, ahora 26 detectores), siguiendo el precedente de
  `tools/verify_all.py` → `ConfigRefsDetector`. También se eliminó
  `axioma_inspector_report.json`.
- ✅ **5 detectores nuevos portados del inspector:**
  - `singleton_violation` → singletons instanciados directamente en vez de `get_*()` (1 hallazgo real: `TaskQueue()` en distributed_queue).
  - `async_module_level` → `asyncio.get_event_loop()` deprecado + primitivas asyncio (`Lock`/`Event`/`Semaphore`) a nivel módulo.
  - `circular_import` → ciclos de import por **grafo estático** (DFS), complementa `syntax_import` que solo detecta circulares que fallan al importar (1 real: `dispatcher → jarvis_engine → dispatcher`).
  - `duplicate_code` → copy-paste real por **fingerprint AST** (nombres de args normalizados, docstrings omitidos); complementa `name_collision` (1 real: `_to_snake_case` duplicado, resuelto al eliminar el inspector).
  - `integration` → módulos aislados/residuales de refactor + archivos >1000 líneas y métodos >80.
- ✅ **Nuevo helper `_resolve_imports_for()`**: resuelve imports relativos a módulos absolutos (CodeIndex guardaba `from .x import ...` sin el prefijo del paquete) — elimina falsos positivos en `integration`/`circular_import` (loop/* y coder_core ya no aparecen como aislados).
- ✅ **`--run-tests`**: ejecuta pytest (`test_axioma_*.py`) y lo incluye en el reporte.
- ✅ **SCORE AXIOMA /100**: sección al final del reporte markdown + resumen en consola (fórmula del inspector: penaliza por categoría, no por hallazgo).
- ✅ **Docs actualizadas**: `README.md`, `COMANDOS_AXIOMA.txt` (26 detectores + `--run-tests`).
- Suite: **254 passed**. Auditor: SCORE 86/100 (0 CRITICAL, 0 HIGH).

### 🟢 v0.6.8w — PLAN v2.0: integridad de contenido externo + anti-injection + /traces — 2026-09-01

Implementación del **PLAN AXIOMA v2.0** (6 fases planificadas → 5 implementadas, 1 descartada con criterio).

- ✅ **FASE 6 — Integridad de contenido externo (gap de seguridad real, prioridad máxima):**
  - `researcher_sources.py`: `search_web()` ahora devuelve `(results, provider_name)` — el caller reporta el proveedor REAL que respondió (antes adivinaba por keys configuradas).
  - `researcher.py`: `_build_search_prompt()` envuelve cada fuente en `<fuente_externa>...</fuente_externa>` + instrucción explícita de NO ejecutar nada dentro del bloque; `_extract_sources()` dejó de parsear el texto generado por el LLM (podía inventar URLs) y ahora valida citas `[n]` contra `search_results[:max_sources]` (cita inválida se descarta); `require_citations` dejó de ser bandera muerta — sin citas válidas → `confidence` proporcional (`len(citas)/len(results)`) + aviso explícito en `content`; `api_used` usa el provider real.
  - `searcher.py`: mismo delimitador en `_build_search_prompt()` y `_build_raw_context()` (el contexto que iría a un handoff de código); política explícita de "no sé" en el prompt; fix de `_detect_api_used()` — ahora lee `self._last_provider` seteado en el momento exacto en que un proveedor devuelve resultados (antes comparaba contadores acumulados de toda la vida del agente); **handoff `SearcherAgent → CoderAgent` desactivado**: ya no devuelve `content=""` cuando `requires_coding` (nadie lo reenrutaba — respuesta vacía real); la síntesis normal corre siempre y `requires_coding` se conserva en metadata.
  - `dispatcher_handlers.py`: en `_handle_chat()` el `tool_result.output` se envuelve en `<fuente_externa>` antes de inyectarlo al historial del LLM (path de chat por defecto — el más expuesto).
  - `settings.py`: nuevo flag `content_grounding_enabled` (default True).
- ✅ **FASE 3 — Gate anti-injection en input directo:**
  - `validator.py`: nuevo `_INJECTION_PATTERNS` a nivel de clase + `match_injection()` (patrones acotados a jailbreak: `ignore previous instructions`, `you are now`, `system prompt`, `disregard`, + equivalentes en español). NO reutiliza `_UNSAFE_PATTERNS`/`_CWE_PATTERNS` (bloquearían pedidos legítimos con subprocess/eval/SQL o research con "armas").
  - `dispatcher_process.py`: `_screen_input()` al inicio REAL de `process()` — antes de `_response_cache`, `_routing_cache` y el hit L3. Si bloquea → `Response` con `input_blocked` sin tocar caches ni LLM.
  - `settings.py`: nuevo flag `input_screening_enabled` (default True).
- ✅ **FASE 2 — Observabilidad `/traces` (CLI + web):**
  - `command_parser.py`: nuevo comando `/traces [N|task <type>|handler <name>|stats]`; docstring corregido "11 comandos" → 17 (se verificó que había 16 reales, no 17 como decía el plan original).
  - `routes_extended.py`: nuevo endpoint `GET /api/v1/traces` (mismo patrón que `/stats`: sin auth, error como dict inline, `_log_web_event`+`_log_error`), delega al mismo `TraceStore` singleton que el CLI.
- ✅ **CORRECCIONES PUNTUALES (bugs reales verificados):**
  - `researcher.py` `_execute_news()`: `extract_location()` recibía `query_text` en lowercase → el regex exige `[A-Z]` inicial → la ubicación NUNCA se detectaba en noticias. Ahora recibe `input_msg.content` sin lowercasear (igual que `_execute_weather`/`_execute_time`).
  - `judge.py`: header del módulo decía `qwen3:8b` (obsoleto) — el modelo real es `qwen2.5-coder:7b` (`.env:53`, `settings.py:58`).
- ✅ **FASE 5 — Evals con LLMJudge (adaptada sin carpeta nueva):**
  - `judge.py`: **BUG VERIFICADO corregido** — `_parse_output()` se llamaba en `_call()` FUERA del try/except: si el LLM devolvía formato inesperado en modo ABSOLUTE (ej. "Result: pass"), `float()` lanzaba ValueError y crasheaba el runner. Ahora tiene su propio try/except → `JudgeResult(verdict="error", ...)`.
  - `tests/dataset_evals.yaml` (nuevo, en `tests/` — sin carpeta `tests/evals/`): 4 casos `{instruction, agent, criteria, expected}`; `response` NO se guarda (se genera en runtime llamando al agente real).
  - `tools/run_evals.py` (nuevo): runner que carga el YAML, ejecuta el agente real y evalúa con `LLMJudge.evaluate_binary()`; exit code ≠0 si hay fallos.
- ✅ **FASE 4 — Repo-map en CoderAgent (adaptada SIN archivo nuevo):**
  - `coder.py`: `_build_prompt()` inyecta `CONTEXTO DEL REPO` si `settings.repo_map_enabled` (default False — no cambia comportamiento). `CodeIndex` se importa desde `axioma_auditor.py` (ya existía) con fallback silencioso y `build()` cacheado como singleton de módulo. No se creó `src/utils/code_index.py` (criterio: no crear archivos nuevos cuando la lógica ya vive en un archivo existente).
- ⛔ **FASE 1 — Retrieval híbrido FTS5+RRF: DESCARTADA** (2026-09-01): se verificó que `search_semantic()` (el patrón a imitar) no tiene ningún caller fuera de `src/memory/` → construir `search_hybrid()` sería esfuerzo muerto (mismo patrón de flags sin uso que AXIOMA ya tuvo). No se agrega `hybrid_search_enabled`.
- ✅ **Tests:** nuevo `tests/test_axioma_10_plan_v2.py` (14 tests) que verifica todas las fases (flags, delimitadores, citas, `_last_provider`, handoff, `_INJECTION_PATTERNS`, `_screen_input`, `/traces` CLI+web, fix judge, dataset, repo-map). 100% estáticos o lógica pura — sin Ollama.
- ✅ **Auditor:** `axioma_auditor.py` v8.2 — nuevo `PlanV2ProtectionsDetector` (verifica presencia de las 19 protecciones del plan v2.0 + anti-regresión de Fase 1). 21 detectores. `CodeIndex` queda en el archivo (importado por coder.py).
- Suite: **254 passed**.

### 🟢 v0.6.8v — Tests que DIAGNOSTICAN los errores del log — 2026-09-01

- ✅ **Nuevo `tests/test_axioma_08_regresiones_log.py`**: tests con `assert`
  de pytest REALES (el helper `check()` de la suite solo registra en el
  reporte y NUNCA falla el test — por eso los bugs pasaron 221 tests).
  Cubren los errores del log y fallan en rojo si reaparecen:
  - `execute` propaga `session_id`/`user_id` a `_build_prompt` (coder).
  - `_execute_general_search` propaga identidad a `_build_search_prompt` (researcher).
  - El chat usa `settings.ollama_timeout_chat` (no `timeout=60`).
  - `add_message` con content no-str no explota y diagnostica con `_log_error`.
  - Fallo total de capas → `success=False` + warning con estado.
- ✅ **BUG REAL detectado por el test nuevo:** el loop interno de retry de
  `CoderAgent.execute` (blacklist, calidad, seguridad CWE) recursaba SIN
  reenviar `session_id`/`user_id` → la identidad se perdía en el 2º/3º
  intento. Corregido en las 3 recursiones.
- ✅ **CAUSA RAÍZ de los `✗ add_message` del log:** los guards
  `if self._short_term:` / `if self._long_term:` usaban la TRUTHINESS del
  objeto, y `ShortTermMemory.__bool__` (`len > 0`) y `LongTermMemory.__len__`
  (`SELECT COUNT(*)`, global) devuelven False/0 cuando están "vacías" →
  las capas se saltaban silenciosamente (el ✗ intermitente del log). Todos
  los guards (21) ahora usan `is not None` en gateway.py y gateway_ops.py.
- ✅ **Tests de coordinación endurecidos** (`test_axioma_08_coordinacion_memoria.py`):
  `test_drenado_espera_modelo_ocupado` esperaba con `sleep(0.05)` fijo que un
  hilo arrancara → flake bajo carga (2 fallos intermitentes). Ahora espera el
  marcador real de arranque + lookup seguro; `test_wait_for_result_cruzado`
  con lookups defensivos. Verificado: 12/12 corridas de suite completas limpias.
- Suite: **232 passed**.

### 🟢 v0.6.8u — Fixes del análisis de logs (session_id, timeout chat, memoria) — 2026-09-01

- ✅ **BUG CRÍTICO: toda generación de código fallaba** con
  `NameError: name 'session_id' is not defined`. `_build_prompt`
  (src/agents/coder.py) y `_build_search_prompt` (src/agents/researcher.py)
  usaban `session_id`/`user_id` sin declararlos ni recibirlos (introducido
  en plan_memory). Ahora los reciben como parámetros y `execute` los
  propaga. Mismo bug en `_execute_general_search` (researcher) — la
  búsqueda web general tampoco propagaba la identidad.
- ✅ **Chat web: timeout subía 60s hardcodeado** → con qwen3:8b en CPU
  (HW-FIT: "marginal — Solo CPU RAM") el chat daba `ReadTimeout` a los 60s
  (verificado: document_analysis con 420s tardó 139s OK). Nuevo setting
  `ollama_timeout_chat` (180s, configurable 60-600) usado en el agent loop.
- ✅ **Memoria: writes `add_message` fallaban silenciosamente** (solo
  `success=False` en el summary, sin causa). Ahora:
  - Los errores de capa (short/long-term) se registran con `_log_error`
    (traceback en `logs/reg_error.txt`, no solo terminal).
  - El summary de fallo incluye estado de capas (role, content_len,
    short/long/semantic/optimizer) para diagnóstico.
  - `_record_exchange` coacciona content a `str` (content no-str rompía
    la validación de long_term con MemoryError silencioso).
- ✅ **Tests de regresión**: `_build_prompt` y `_build_search_prompt` con
  session_id/user_id (antes: NameError). Suite completa: 225 passed.

### 🟢 v0.6.8t — Fix: selección de usuario en la web no abría el chat — 2026-09-01

- ✅ **Bug:** al hacer clic en un usuario (ventana `/`), el chat no se mostraba.
  En NiceGUI 3.11 `app.storage.browser` es una cookie de **solo lectura**
  después de que la página respondió (`ReadOnlyDict`): escribir `user_id`
  desde el clic lanzaba `TypeError` que el `except: pass` tragaba, `/chat`
  no veía usuario y redirigía de vuelta a `/`.
- ✅ **Fix:** migración a **`app.storage.user`** (server-side, identificado por
  la cookie de sesión, modificable en cualquier momento y sobrevive la
  navegación) en `_browser_user_id`/`_set_browser_user_id` (app.py) y en
  `_init_browser_storage`/`save_settings`/`load_settings` (app_logic.py).
  De paso arregla que la **configuración ⚙️ nunca se guardara** (mismo bug).
- ✅ **Test de regresión:** `tests/test_axioma_07_user_selection_e2e.py` usa el
  harness `nicegui.testing.user_simulation` replicando el patrón exacto del
  app. Verificado: con `storage.browser` falla con el TypeError original; con
  `storage.user` pasa. Suite completa: 223 passed.

### 🟢 v0.6.8s — Dashboard eliminado de la web UI — 2026-09-01

- ✅ **Dashboard retirado de la interfaz web** (no era útil y gastaba
  recursos). Eliminado el botón 📊 del header y la ruta `/dashboard`
  (`src/interfaces/web/app.py`).
- ✅ **Eliminados los endpoints** `/api/v1/metrics`,
  `/api/v1/health/detailed` y `/api/v1/metrics/agents` de
  `routes.py` (manteniendo `/api/v1/health` y `/api/v1/chat`).
- ✅ **Borrados los módulos** `src/interfaces/web/dashboard.py`,
  `src/interfaces/web/dashboard_core.py` y
  `src/interfaces/components/dashboard_widgets.py`; sin referencias
  rotas ni huérfanas (registry, health_check, tests y comentarios
  actualizados). `docs/STRUCTURE_REPORT.md` regenerado.
- ✅ `MANUAL_AXIOMA.md` §3.4 actualizado (sin el ítem Dashboard).

### 🟢 v0.6.8r — Manuales + fix del cierre de Rafael — 2026-08-31

- ✅ **`MANUAL_RAFAEL.md`** y **`MANUAL_AXIOMA.md`** en la raíz del proyecto:
  guías de uso completas (arranque/cierre, comandos, wake word, panel de
  archivos, memoria, configuración y troubleshooting).
- ✅ **Fix: Rafael quedaba activo tras cerrar el widget.** El ✕/Alt+F4
  cerraban el widget pero el daemon de voz seguía corriendo (el IPC /cerrar
  no siempre llegaba). Ahora el daemon se apaga SOLO cuando el widget se
  cierra limpio (`_widget_supervisor` → SIGTERM) y `/cerrar` sigue como
  camino directo. Verificado: stop limpio `Result=success`, sin procesos.
- ✅ **Fix: stop colgaba** — los pools/threads no-daemon mantenían el proceso
  vivo tras el shutdown → systemd lo mataba con SIGKILL ("failed: timeout").
  `os._exit(0)` al final del shutdown + `TimeoutStopSec=15` en
  `~/.config/systemd/user/rafael.service`.

### 🟢 v0.6.8q — Limpieza git + panel de archivos en la UI — 2026-08-31

  `ghp_...`). Estaba en `.gitignore` (no se subía), pero ahora tampoco existe
  localmente. `docs/documentador.txt` actualizado (push vía `git push`).
- ✅ **`axioma-v1.0.0.bundle`**: backup portátil del repo (git bundle,
  tag v1.0.0) — NO es necesario (el commit existe en `.git`). Des-trakkeado
  (`git rm --cached`), `*.bundle` agregado a `.gitignore`, y movido a
  `data/backups/` como archivo (se puede borrar sin problema).
- ✅ **`.gitignore` se conserva trackeado** (es el que evita que `.env`,
  `venv/`, `data/`, `logs/` se suban a GitHub — necesario).
- ✅ **`inforchat/` eliminado** (planes memory y rafael cumplidos e
  implementados; sin deuda pendiente de implementación).
- ✅ **Panel 📁 Archivos en la UI NiceGUI**: botón "📁" en el header →
  diálogo con búsqueda por nombre (tool `fs_search`), resultados con
  acciones **Leer** (`fs_read`), **Resumen** (LLM vía dispatcher) y
  **Cita** (bloque). Misma capacidad que Rafael por voz, en `main.py --web`.
- ✅ Verificado: server `/chat` 200, suite 221 PASS.

### 🟢 v0.6.8p — Rafael: leer archivos → resumen o cita — 2026-08-31

- ✅ **"leé el archivo X y resumilo"** → busca el archivo, lo lee (`fs_read`)
  y el LLM genera un **resumen** del contenido.
- ✅ **"citá un bloque del archivo X"** → lee el archivo y devuelve un
  **bloque citado** (sin LLM — rápido).
- ✅ Routing: nuevas keywords FILE_SEARCH ("leé/resumí/citá/mostrame el
  archivo") + `_detect_file_action()` en el handler (summary/quote/location).
- ✅ Fix del extractor de nombre: "citá/bloque/resumilo/y/leé" se limpian y
  "rafael" ya NO se borra de nombres como "Rafael.py".
- ✅ Verificado end-to-end: "citá un bloque del archivo .env.example" →
  bloque citado (21s); "leé el archivo Rafael.py y resumilo" → resumen LLM
  (111s, esperable en CPU). Suite 221 PASS.

### 🟢 v0.6.8o — Rafael: búsqueda de archivos + "✕" en la esquina — 2026-08-31

- ✅ **Rafael busca archivos/carpetas** (voz o chat): "buscá el archivo X",
  "dónde está la carpeta Y" → devuelve la **ruta exacta**. Nuevo
  `TaskType.FILE_SEARCH` (48 total), tool `fs_search` (búsqueda recursiva por
  nombre, default en la carpeta AXIOMA, salta dirs pesados), routing
  `file_search` → `_handle_file_search` (extrae el nombre de la frase y
  formatea los paths), keywords + training examples en el clasificador.
- ✅ **Fix FP de finance**: la keyword 'libra' matcheaba como substring dentro
  de "benchmark_cali**bra**tion" → toda query con "calibración" iba a
  finance_query. Ahora las keywords financieras se matchean con word-boundary.
- ✅ **"✕" en la esquina superior derecha real**: flet 0.85 no posiciona bien
  `right` en Stack → ahora usa `left`/`top` explícitos (36, 4).
- ✅ **Verificado**: suite 221 PASS; end-to-end "buscá el archivo
  "dónde está la carpeta tools" → `tools/`. Widget relanzado sin errores.

### 🟢 v0.6.8ñ — Widget: botón "✕" visible para cerrar + fix del menú — 2026-08-31

- ✅ **Fix del crash del widget**: `ft.alignment.center` no existe en flet 0.85
  (`AttributeError: module 'flet.controls.alignment' has no attribute
  'center'`) → el widget crasheaba AL ARRANCAR (por eso "no se podía cerrar":
  no había interfaz). Corregido con `ft.alignment.Alignment(0, 0)`.
- ✅ **Fix del menú "Cerrar"**: el clic derecho abría el diálogo con
  `page.dialog = dlg`, que NO existe en flet 0.85 → el menú nunca aparecía
  (error silencioso). Ahora usa `page.show_dialog(dlg)`.
- ✅ **Botón "✕" SIEMPRE visible** sobre el orb (arriba a la derecha, rojo):
  la ventana es frameless y no tiene botón nativo de cierre; el clic derecho
  sigue como atajo, pero ahora hay un camino visible. Toca el "✕" → menú
  "¿Cerrar Rafael?" → Cerrar (IPC /cerrar → shutdown limpio del daemon).
- ✅ Verificado: widget arranca sin errores (journal limpio), proceso vivo
  sin reinicios, suite 221 PASS.

### 🟢 v0.6.8n — Rafael ya NO se auto-ejecuta (arranque solo manual) — 2026-08-31

- ✅ **`rafael.service` deshabilitado y detenido** — antes quedó `enabled`
  (arrancaba solo al iniciar sesión, abriendo el widget sin orden). Ahora:
  `disabled` + `inactive`, sin procesos de Rafael. Solo corre cuando el
  usuario lo ordena: `systemctl --user start rafael.service` o
  `python Rafael.py`. Se cierra con botón derecho → Cerrar.
- ✅ `mic-gain.service` sigue activo (solo fija la ganancia del micrófono —
  no arranca Rafael). Nada más lo re-habilita (verificado: sin autostart,

### 🟢 v0.6.8m — Widget Rafael: botón derecho → "Cerrar" — 2026-08-31

- ✅ **Cerrar Rafael con botón derecho**: clic derecho sobre el orb del
  widget → menú "¿Cerrar Rafael?" con **Cerrar** / Cancelar.
- ✅ **Cerrar** envía el comando IPC `/cerrar` al daemon (reutiliza el canal
  del diálogo) y cierra la ventana del widget. El daemon, al recibirlo, se
  envía SIGTERM a sí mismo → el handler de `Rafael.py` cancela main_task →
  `shutdown()` limpio (pipeline de voz + widget + TTS detenidos).
- ✅ Verificado: widget relanzado con el código nuevo (pid activo), suite
  221 PASS, servicio activo.

### 🟢 v0.6.8l — Wake word "rafael" (verificación por STT) — 2026-08-31

- ✅ **Wake word cambiado a "rafael"** (`jarvis_wake_word`, antes "axioma"):
  decís **"rafael dime <comando>"** (o "rafael, ¿qué hora es?", "rafael decime
  el clima"...).
- ✅ **Mecanismo**: la puerta de audio (energía calibrada, mic a 56%)
  detecta "hay habla"; el nombre se **verifica por STT** sobre la frase
  capturada (`voice_pipeline._strip_wake_prefix`): si contiene "rafael" se
  limpia el prefijo (+ "dime/decime/di...") y se despacha el resto; si no,
  se ignora (Rafael no responde a charla ajena). El ack "Dime" se emite solo
  cuando matchea.
- ✅ **openWakeWord desactivado por default** (`_USE_OPENWAKEWORD=False`): su
  modelo preentrenado detectaba "hey jarvis" (frase que ya no usamos) y
  consumía capturas al pedo. Listo para reactivar si se entrena un modelo
  propio para "rafael".
- ✅ Verificado: strip de prefijo con variantes (rafael dime/decime/", "
  directo), rechazo de charla ajena ("hey jarvis...", "hola maría..."),
  suite 221 PASS, `wake_word=rafael` en el log del servicio.

### 🟢 v0.6.8k — Rafael: dólar real + ProactiveEngine OFF — 2026-08-31

- ✅ **Cotización del dólar SIN API key**: `DataFetchers.fetch_finance` ahora
  detecta consultas de dólar y usa **dolarapi.com** (gratis, Argentina) —
  antes sin `TAVILY_API_KEY` fallaba ("TAVILY_API_KEY no configurado") y no
  daba el valor. Devuelve oficial/blue/bolsa/CCL/mayorista/cripto/tarjeta con
  compra/venta. Tavily queda como general (si hay key).
- ✅ **ProactiveEngine OFF por default**: nuevo setting
  `jarvis_proactive_enabled=False` — `rafael.service` ya NO inicia el loop
  proactivo (antes hablaba solo: clima/noticias y preguntaba ubicación sin
  que se le pida nada). Rafael ahora SOLO responde cuando el usuario le
  habla (chat o voz). Se puede reactivar con el setting en True.
- ✅ Verificado: dólar en vivo (7 casas con valores), suite 221 PASS,
  `rafael.service` activo con log "ProactiveEngine DESHABILITADO".

### 🟢 v0.6.8j — plan_rafael implementado (voz Jarvis) — 2026-08-31

- ✅ **FASE 0 — Audio + wake word**: micrófono 98% (saturado) → 56% (9dB),
  persistido con servicio de usuario `mic-gain.service` (sin root); ruido
  basal 88 (antes 27008) → threshold sano. **openWakeWord** (gratis/offline)
  con "hey_jarvis" integrado en `wake_word.py` (el umbral de energía queda
  solo como fallback).
- ✅ **FASE 1 — Pipeline**: UN solo umbral de confianza
  (`jarvis_voice_confidence_threshold=0.5` en settings, leído por
  VoicePipeline y STTConfig — antes 3 valores: yaml 0.5 / pipeline 0.35 /
  settings 0.6); `beam_size` 5→1 + `min_yield_interval` 0.7→1.2s (streaming
  ~3x más barato en CPU); **AGC** `agc_gain=1.5` + clamp [-1,1] en
  `vad_filter`; **Piper offline** activado con modelo es_ES-davefx-medium
  (63MB) — **fix real**: `_piper_synthesize` reescrito para piper-tts 1.4.2
  (la API vieja fallaba con "channels not specified").
- ✅ **FASE 2 — Rafael completo**: `rafael.service` habilitado y CORRIENDO
  (antes nunca corrió); `loginctl enable-linger saintwick=yes`; métricas de
  voz por TURNO en `logs/rafael.log` (antes vacío) + `Rafael.py --stats`;
  `feedparser` + `beautifulsoup4` instalados; timeout de turno 60s→180s +
  aviso TTS "Un momento, estoy mirando la pantalla" para screen_describe.
- ✅ **FASE 3 — Verificación**: suite **221 tests PASS**; loop real
  TTS→STT: "¿Cuál es el clima de hoy en Buenos Aires?" → conf **0.851**
  (≥0.5); `rafael.service` activo con piper + openwakeword + VAD.
- ✅ **FASE 3 — Validación con VOZ REAL** (por SaintWick): diagnóstico
  TODO VERDE — VAD 72% voz, STT exacto conf 0.79, pipeline éxito razón "ok".
  Fixes del diagnóstico: frase a leer + cuenta regresiva antes de grabar;
  la simulación construye `AudioChunk` (antes ndarray → AttributeError);
  `_process_utterance_from_chunk` retorna el `UtteranceResult` (antes None);
  CONFIG muestra los valores reales del loader (antes 0.6 obsoleto).
- ℹ️ `test_rafael_bridge.py`/`test_audio_diagnostic.py` están en
  `collect_ignore` de `tests/conftest.py` (diagnósticos de entorno real);
  su fallo al correrlos sueltos es preexistente, no de esta implementación.

### 🟢 v0.6.8i — UI de elección de usuario (prerequisito plan_rafael) — 2026-08-31

- ✅ **Ventana preliminar al iniciar `python main.py --web`**: la raíz `/`
  ya no es el chat — es la selección de usuario. Se muestran los usuarios
  existentes como tarjetas clicables (con badge de rol: root rojo / user
  azul, y lugar/país si están cargados).
- ✅ **Elección por botón**: tocar un usuario lo selecciona (se guarda en el
  browser storage) y abre la interfaz de chat (`/chat`). Sin usuario elegido,
  `/chat` redirige a `/`.
- ✅ **Usuario nuevo**: botón "➕ Usuario nuevo" → pantalla `/user-new` con
  4 campos (nombre de usuario, correo electrónico, lugar, país). Por ahora
  SOLO "nombre de usuario" está habilitado; el resto deshabilitado. Botón
  "Crear" → valida (no vacío, no duplicado) → vuelve a la lista donde el
  nuevo usuario ya figura para elegirlo. Botón "Cancelar" → vuelve a la lista.
- ✅ **UserRegistry** (`src/memory/user_registry.py`): persistencia en
  `data/users.json` (JSON simple). Seed automático: saintwick (root).
  `create_user` valida nombre no vacío y único; los creados por la UI son
  role="user" (normales, sin privilegios).
- ✅ **Plumbing de identidad**: `SessionState.user_id/user_role`;
  `Dispatcher(user_id=...)` → UserProfile + gateways de memoria (Capa 2) con
  el user de la sesión; agentes (coder/researcher/analyst) reciben user_id y
  sus gateways de memoria lo usan (aislamiento cross-sesión por usuario).
  Header muestra el usuario activo.
- ✅ **Verificado**: server real arranca con las 3 rutas (`/`, `/chat`,
  `/user-new`) → HTTP 200; registry seeded y validado; plumbing
  Dispatcher/agentes con user_id; suite **221 tests**.
- ⚠️ email/lugar/país quedan deshabilitados por ahora (se guardan vacíos) —
  listos para habilitarse cuando se pidan.

### 🟢 v0.6.8h — Capa 2: retrieval cross-sesión con identidad de usuario — 2026-08-31

- ✅ **Identidad persistente**: `AXIOMA_USER_ID` (default `saintwick`) y
  `AXIOMA_USER_ROLE` (default `root`) en `settings.py`/`.env`/`.env.example`.
  saintwick = root (admin); usuarios futuros = `user` (sin privilegios).
  `UserProfile` del Dispatcher recibe el user_id real (antes siempre
  "default").
- ✅ **Etiquetado por usuario**: `MemoryGateway(user_id=...)` — `add_message`
  agrega `user_id` al metadata de las 3 capas (short/long/Palace) en el merge
  único. Write-path del dispatcher y gateways de agentes usan
  `settings.axioma_user_id`.
- ✅ **Schema Palace con filtrado**: `PalaceRecord` + columnas top-level
  `session_id`/`user_id`; `Palace.__init__` migra tablas viejas con
  `add_columns` (sin perder datos); `Palace.recall(user_id=...)` filtra con
  `where`. `search_semantic(user_id=...)` aplica el filtro en ambos paths
  (multi-query y directo).
- ✅ **Inyección cross-sesión en `get_context`**: `_inject_cross_session()`
  agrega hasta `CROSS_SESSION_LIMIT` (3) mensajes relevantes de OTRAS
  sesiones del MISMO usuario, respetando `max_tokens` (TokenEstimator),
  dedup por contenido y `metadata["source"]="cross_session"`. Se movió el
  bloque de estrategias ANTES del early-return de ventana vacía — una sesión
  nueva (sin historial) es justo el caso que cross-sesión resuelve.
- ✅ **Aislamiento por usuario verificado**: mismo user → inyecta; otro
  user → 0 (filtro por columna `user_id`).
- ✅ **Limpieza**: `data/memory/axioma.db` (1429 mensajes + 618 sesiones de
  tests viejos) y residuos de migración (`mempalace/chroma.sqlite3`, tabla
  UUID de mayo) eliminados.
- ✅ **Tests**: 2 nuevos (`test_capa2_*`) — user_id en metadata de capas y
  aislamiento cross-sesión con Palace real en tmp_path. Suite: **219**.
- ⚠️ Roles/privilegios (root vs user): el user_id aísla la memoria; el gating
  de privilegios es tema de comandos/tools, fuera de este alcance.

### 🟢 v0.6.8g — plan_memory implementado (Fase 0 + B mínimo + Fase 1) — 2026-08-29/31

Orden ejecutado según feedback: 0 (fix bug) → B mínimo (write-path) → 1
(source_type + boost) → Capa 2 (cross-sesión) NO (sin identidad persistente).

- ✅ **Fase 0 — bug MD5** (`src/memory/context_strategies.py`):
  `RelevanceSelector._get_model_fn()` usaba `hasattr(optimizer,
  '_hash_embedding')` (siempre True) → embeddings MD5. Ahora usa el modelo
  real (`get_embedding_model`, BGE-M3) con fallback a hash SOLO si falla.
  Cache namespace unificado `"context_window"` → `"BAAI/bge-m3"` (comparte
  L1 con Palace/add_message).
- ✅ **B mínimo — write-path de memoria conectado** (antes desconectado:
  `MemoryGateway.add_message` sin callers de producción):
  - `src/core/dispatcher_process.py::_record_exchange()` — punto ÚNICO de
    escritura (user + assistant con task_type/source_type) en el retorno
    principal de `process()`; `_get_session_gateway()` cachea gateways por
    sesión (acotado 64, thread-safe).
  - `src/agents/base.py::_memory_for(session_id)` — gateways por sesión por
    agente (acotado 64); `_get_context`/`_add_to_memory` aceptan session_id.
    Sin session_id → comportamiento previo (sin regresión).
  - `dispatcher_handlers.py` pasa `session_id` a coder/searcher/researcher;
    coder/researcher/analyst la pasan a `_get_context`.
- ✅ **Fase 1 — source_type + boost**:
  - `gateway.py`: `TASK_TO_SOURCE` (47 TaskType → code/search/api/analysis/
    validation/vision/system/chat); `add_message(source_type="chat")` con
    UN merge de metadata a las 3 capas; `get_context()` pasa
    `expected_source_type` a `strategy.select()`.
  - `context_strategies.py`: `SelectorConfig.source_type_boost=0.08`;
    `BaseSelector.select(expected_source_type=None)`; boost en el scoring
    cuando metadata.source_type == esperado; forwards en Hybrid/Mythos.
- ✅ **Punto 5 — analyst.py**: `_ANALYSIS_TASK_TYPE` mapea analysis_type a
  TaskType real (antes inventaba "analysis_document" → caía a RecencySelector).
  `_get_strategy_for_task` incluye sentiment/trend/risk_analysis → Relevance.
- ✅ **Punto 6 — contracts.py**: anotado, NO tocado (`MemoryGatewayProtocol`
  exportado y chequeado por test_axioma_01, sin uso como contrato).
- ✅ **Tests**: 7 nuevos (`TestPlanMemory`) — modelo real vs MD5, namespace
  BGE-M3, source_type en capas, boost, TASK_TO_SOURCE 47, estrategia
  analysis → relevance, mapeo analyst, write→read por sesión. Suite: 217.
- ✅ **BGE-M3 verificado FUNCIONANDO (2026-08-31)**: el modelo NO vive en
  Ollama (`ollama list`: solo qwen3:8b/qwen2.5-coder:7b/qwen3-vl:4b) sino en
  el cache de HuggingFace (`~/.cache/huggingface/hub/models--BAAI--bge-m3`,
  4.3GB, sentence-transformers 5.4.1) — el singleton lo carga local, sin
  Ollama. `get_embedding_model().embed()` → **1024 dims** (verificado en
  vivo). El fix de Fase 0 es efectivo: `_get_model_fn()` embebe con BGE-M3
  real (similitud 0.924 para textos relacionados vs 0.328 no relacionados).
  Corrección: la nota previa de "hacer `ollama pull BAAI/bge-m3`" era
  incorrecta — no hace falta, ya está instalado y operativo.
- ⚠️ **Capa 2 (cross-sesión) NO implementada**: session_id es efímero
  (`SessionState` genera `nicegui_<timestamp>` por recarga) y no hay
  identidad de usuario persistente (`UserProfile._user_id` = "default").
  La memoria funciona DENTRO de la sesión. Ver `inforchat/plan_memory.txt` v3.

### 🟢 v0.6.8f — RSS de Ollama por relación padre-hijo (robustez) — 2026-08-29

  en vez de matchear el runner por NOMBRE (`llama-server`, que no contiene
  "ollama" y puede renombrarse en el futuro — Ollama ya lo hizo antes:
  `ollama runner` → `llama-server` → `ollama_llama_server`), ahora se
  localiza la RAÍZ (`ollama` en name/cmdline, el daemon `ollama serve` —
  nombre estable) y se suma el RSS de la raíz + TODOS sus descendientes
  (BFS, dedup por pid), sin filtrar a los hijos por nombre.
- ✅ Fallback sin psutil (`_get_ollama_rss_gb_procfs`): mapa pid→ppid desde
  `/proc/<pid>/stat` (campo 4 tras el último `)`, porque comm puede tener
  espacios) + VmRSS, raíces por name/cmdline, BFS sobre el mapa de hijos.
- ✅ Verificado en vivo (2026-08-29): sin modelo 0.08GB → con qwen3:8b
  **6.2GB** (serve 46MB + hijo `llama-server` 6238MB contado por el árbol,
  no por su nombre). Fase 2 end-to-end: `rss_delta=6.1GB valid=True`,
  accuracy 0.984.
- Beneficio: si Ollama renombra el runner, la señal RSS sigue funcionando;
  solo se rompería si el propio daemon deja de llamarse "ollama".

### 🟢 v0.6.8e — Poda automática de logs y backups (ahorro de disco) — 2026-08-29

  (`prune_backups()`, `BACKUP_KEEP_COUNT=5`) — tras cada commit se conservan
  solo los últimos 5 backups `commit_*` y se borran los más antiguos. Limpieza
  inicial: **22 GB → 7,7 MB** (39 backups viejos eliminados).
- ✅ **`logs/axioma_test_report_*.md`**: `tests/axioma_report.py` conserva
  solo el reporte más reciente (poda al crear el nuevo).
- ✅ **`logs/code_contract_report_*.md`**: `tests/test_code_contract_e2e.py`
  conserva solo el informe más reciente.
  por corrida (antes uno por línea, logs incompletos) y poda al final
  conservando solo el más reciente.
  log de la corrida actual (poda al inicio excluyendo `$LOG_FILE`, porque
  `main()` puede hacer exit antes del final).
- ✅ **`logs/reg_error.txt`**: sin cambios — `tools/detailed_logger.py` ya
  rotaba actual → `reg_error_old.txt` (actual + 1 anterior para cotejar), que
  es exactamente la política pedida.
- ℹ️ Se conservan `logs/.gitkeep`, `logs/commit_log.jsonl` y
  `logs/feedback_log.jsonl` (estado, no snapshots por corrida).

### 🟢 v0.6.8d — Actualización de documentación y tests obsoletos — 2026-08-29

- ✅ **Plan `inforchat/plan_memory.txt` revisado y corregido (v2)** — sin
  implementar (pendiente de decisión). Correcciones vs v1 verificadas contra
  archivos reales:
  - `multi_query.py` YA EXISTE (v0.6.8) — el v1 decía "que no tengo".
  - El write-path de memoria (`MemoryGateway.add_message`) tiene **cero
    callers de producción**; `data/axioma.db` no tiene tabla de mensajes →
    `get_context` devuelve [] y los selectores nunca corren en producción
    (el bug MD5 de `RelevanceSelector` está dormante, activo solo en tests).
  - La conversación web usa `SessionContext`/`SessionState`, no
    MemoryGateway. La feature multi-query de v0.6.8 vive en `search_semantic`
    (camino muerto).
- ✅ **Fix test obsoleto** `tests/test_axioma_08_coordinacion_memoria.py`:
  "sin tag usa default" fallaba desde el 27/08 (4.68 → 4.4) por la
  calibración del ratio — ahora verifica `DEFAULT_MODEL_SIZE_GB *
  overhead_ratio`.
- ✅ **Fix 2 tests rotos** `tests/test_axioma_07_seguridad_modos.py`:
  - dict comprehension de reglas usaba la clave literal "mode" (key real:
    "pattern") → los 4 checks "existe regla deny" daban FALTA siempre.
  - "SessionEventLog sin pool" parcheaba `dbp.SQLiteConnectionPool` pero el
    módulo decide por su flag `_POOL_AVAILABLE` (binding propio) → ahora
    parchea el flag real y valida el ImportError.
- ✅ **Fix setup test paralelo** (`test_paralelo_restringido_por_guard`):
  "RAM casi llena" rechazaba también el PRIMER modelo; con used=40% un
  modelo cabe y dos no (intención original). Suite completa: **209 passed,
  0 fails** (reporte `logs/axioma_test_report_20260829_193409.md`).
- ✅ **Docs corregidas (info obsoleta):**
  - `src/memory/__init__.py`: "SemanticMemory: ChromaDB" → LanceDB.
  - `src/domain/types.py` docstring: 46→47 tipos (y JARVIS 3→4).
  - `README.md`: links rotos a `docs/ARQUITECTURA.md` y
    `docs/REFERENCIA_ARCHIVOS.md` (no existen) → reemplazados por
    STRUCTURE_REPORT.md y AXIOMA_COMPLETE_CONTEXT.md.
  - `docs/AXIOMA_COMPLETE_CONTEXT.md`: "46 tipos" → 47 (2 lugares).

### 🟢 v0.6.8c — Fix RSS del benchmark de calibración (memoria real) — 2026-08-29

  RSS daba ~0.01GB (`delta_valid=False` en los 3 modelos) porque medía SOLO el
  primer proceso que matchea "ollama" (el server `ollama serve`, ~0.05GB). El
  footprint real del modelo cargado vive en el runner **`llama-server`** (hijo
  de serve, binario en `/usr/local/lib/ollama/llama-server`), que mapea los
  pesos GGUF (mmap, ~5GB RssFile) + KV cache/buffers (~1.2GB RssAnon).
  Ahora se SUMA el RSS de todos los procesos de Ollama, matcheando name Y
  cmdline (clave sin psutil: el fallback `/proc` solo expone `Name:` =
  "llama-server", que no contiene "ollama").
- ✅ **Verificado en corrida real (Fase 2, 2026-08-29):** `valid=True` con
  deltas reales — qwen3:8b `6.11GB`, qwen2.5-coder:7b `4.9GB`, qwen3-vl:4b
  `4.0GB` (antes 0.00–0.01GB).
- ℹ️ **Hallazgo:** el RSS real es MENOR que el `size` de `/api/ps` (tamaño en
  disco): qwen3:8b 6.11 vs 6.85GB; qwen2.5-coder:7b 4.9 vs 5.53GB.
- ✅ **RECALIBRACIÓN contra RSS real:** la referencia de `calibrate_memory()`
  pasa de `/api/ps size` (disco, sobreestima la RAM real ~5–13%) al delta RSS
  de Ollama (cuando `delta_valid`). Nuevo `MEMORY_GUARD_OVERHEAD_RATIO=1.10`
  (antes 1.17) — media de RSS/(hw_fit×1.12): 1.120 / 1.005 / 1.166. Aplicado
  en `.env`, `.env.example`, `config/settings.py`, `src/core/memory_guard.py`
  y artefactos regenerados (`calibration_results.json/md`,
  `calibration_data.json`) con la corrida completa (fases 1+2+5).
- ✅ **FIX `MemoryGuard.can_load()`** (`src/core/memory_guard.py`): se eliminó
  el término `- current_req` del cálculo `ram_after`. Con el mmap por defecto
  de Ollama los pesos del modelo cargado son page cache y NO están en
  `ram_used_gb()` (`.used` los excluye) — restar current_req restaba un valor
  nunca sumado y subestimaba la RAM post-swap en ~current_req GB (can_load más
  permisivo de lo diseñado). Sin la resta: conservador y correcto con mmap on/off.
- 📋 **DEUDA DOCUMENTADA** (de otra sesión, sin implementar): `inforchat/
  plan_memory.txt` — `RelevanceSelector._get_model_fn()`
  (`src/memory/context_strategies.py`) sigue con `hasattr(optimizer,
  '_hash_embedding')` siempre-True → embeddings MD5 en producción, no BGE-M3.

### 🟢 v0.6.8b — Cierre del plan Contrato de Código Verificable — 2026-08-27

- ✅ **3.1 CONFIRMADO** — `test_code_contract_e2e.py` pasa 13/13 en corrida
  real con Ollama (sin timeout; antes global_timeout a los 223s). El fix de
  `analyze_code_quality()` (compilar por bloque, no concatenado) quedó
  validado en esta máquina.
- ✅ **3.2 RESUELTO** — bug de ruteo al modo autónomo:
  - `dispatcher_handlers.py::_should_use_autonomous_mode()` usaba substring
    matching plano (`kw in q`) → "test correspondiente" matcheaba "corre"
    (dentro de "correspondiente"), "aprueba" matcheaba "prueba", "correo"
    matcheaba "corre". Toda query de generación de código normal se ruteaba
    a InterpreterLoop (63-163s) saltándose Fases 1-4. Ahora usa word
    boundaries (`(?<!\w)kw(?!\w)`).
  - `interpreter_loop.py`: nueva validación estática temprana
    (`_static_syntax_error()`) — un SyntaxError ya no consume un intento
    de sandbox; va directo a fix con el error de sintaxis.
  - `interpreter_loop.py::LoopConfig.global_timeout` 120s → 300s: en CPU una
    generación sola tarda ~110s, el timeout de 120s cortaba el ciclo
    code→execute→fix antes de completar nada.
- ✅ **3.3 VERIFICADO** — NiceGUI 3.11.0 ≥ 2.14.0 (requisito de
  `ui.download()`). `feedparser`/`bs4` no aplican a este plan.

### 🟢 v0.6.8 — Multi-Query Ligero (sin LLM, CPU-only) — 2026-08-27

- ✅ **Nuevo** `src/memory/multi_query.py` — mejora de recall para búsqueda
  semántica adaptada a hardware sin GPU:
  - Expansión de query 100% determinística (keywords-only, reordenada por
    peso heurístico, lowercase, truncado de cláusula) — sin llamadas LLM.
  - UN solo batch BGE-M3 para todas las variantes (`embed_batch`).
  - Fusión **Reciprocal Rank Fusion (RRF)** + dedupe + max-similarity.
  - Stats globales thread-safe (`get_multi_query_stats()`).
  - Costo real: **+0-200ms** por query (vs +15-60s del multi-query clásico).
- ✅ **Integración** en `MemoryGateway.search_semantic()` (gateway.py):
  activo por defecto para queries ≥ `multi_query_min_length` (30 chars),
  con fallback silencioso al path single-query ante cualquier error.
- ✅ **Mejora amortizada L3** (cache_l3.py): `set()` ahora guarda también
  las variantes expandidas del query → futuras lecturas semánticamente
  cercanas a una variante aciertan HIT aunque no se parezcan al original.
- ✅ **Nuevos flags** en `config/settings.py`: `multi_query_enabled`,
  `multi_query_max_variants`, `multi_query_min_length`, `multi_query_rrf_k`,
  `multi_query_store_l3_variants`.

### 🔴 Fase 1: CIMIENTOS (v0.0.1) — ✅ Completado 100%

- ✅ Estructura de directorios (25+ carpetas)
- ✅ Entorno virtual fish-compatible
- ✅ `config/settings.py` con Pydantic + validación timeout ≤25s
- ✅ `config/models.yaml` con enrutamiento por TaskType + fallback chain
- ✅ `config/paths.py` con rutas absolutas resueltas
- ✅ `src/domain/types.py` con 35 TaskTypes operativos
- ✅ `src/domain/entities.py` con Message, Session, Response, AgentState
- ✅ `src/domain/exceptions.py` con jerarquía de errores AXIOMA (7 clases)
- ✅ `src/domain/contracts.py` con Protocols para LLMClient, MemoryGateway, Router
- ✅ `.gitignore` con reglas para venv, .env, logs, data
- ✅ `.env.example` con plantilla documentada (sin keys reales)
- ✅ `.env` con configuración local (NUNCA versionar)
- ✅ `requirements.txt` con 90 dependencias exactas
- ✅ `main.py` con CLI: --check, --chat, --backup
- ✅ `README.md` con documentación completa
- ✅ `CHANGELOG.md` con este archivo

### 🟢 Fase 2: LLM CLIENT + MEMORY (v0.0.2) — ✅ Completado 100%

- ✅ `src/llm/client.py` — OllamaClient con circuit breaker + retry exponencial
- ✅ `src/llm/router.py` — Selección de modelo por TaskType + temperatura
- ✅ `src/llm/config.py` — Configuración específica por modelo
- ✅ `src/memory/gateway.py` — Gateway unificado (short + long + semantic)
- ✅ `src/memory/short_term.py` — Ventana conversacional con deque + TTL
- ✅ `src/memory/long_term.py` — SQLite para interacciones + knowledge vault
- ✅ `src/memory/semantic.py` — ChromaDB para búsqueda vectorial
- ✅ Health check para Ollama con fallback en cascada

### 🟢 Fase 3: ROUTER + AGENTES (v0.0.3) — ✅ Completado 100%

- ✅ `src/router/classifier.py` — IntentClassifier con embeddings + keywords
- ✅ `src/router/smart_router.py` — Lógica de ruteo principal + caché L1/L2
- ✅ `src/router/cache.py` — RouterCache con TTL + LRU
- ✅ `src/agents/base.py` — BaseAgent Protocol
- ✅ `src/agents/orchestrator.py` — Orquestador de agentes
- ✅ `src/agents/coder.py` — Generación/corrección de código
- ✅ `src/agents/researcher.py` — Búsqueda web con fallback chain
- ✅ `src/agents/validator.py` — Validación con ForcedCoT (5 secciones, temp=0.1)
- ✅ `src/agents/analyst.py` — Análisis de documentos/datos

### 🟢 Fase 4: INTERFACES (v0.0.4) — ✅ Completado 100%

- ✅ `src/interfaces/web/server.py` — FastAPI con middleware logging
- ✅ `src/interfaces/web/routes.py` — Endpoints: /health, /chat, /backup
- ✅ `src/interfaces/web/static/index.html` — UI principal
- ✅ `src/interfaces/web/static/styles.css` — Estilos
- ✅ `src/interfaces/web/static/app.js` — Lógica frontend
- ✅ `src/interfaces/cli/main.py` — CLI headless con rich colors

### 🟢 Fase 5: UTILIDADES (v0.0.5) — ✅ Completado 95%

- ✅ `main.py` — Entry point con --check, --chat, --backup
- ✅ `README.md` — Documentación completa
- ✅ `CHANGELOG.md` — Este archivo
- ✅ `tools/health_checker.py` — Verificación de 10+ componentes
- ✅ `tools/setup_validator.py` — Validación de entorno pre-ejecución
- ✅ `benchmarks/reasoning.py` — Benchmarks reproducibles (seed 42)
- ✅ `benchmarks/classification.py` — Precisión del router por TaskType
- ⚪ `setup_structure.py` — Script de configuración (Pendiente)

### 🟡 Fase 6: TESTS + DOCUMENTACIÓN (v0.1.0) — 🟡 En Progreso 70%

- ✅ Suite de tests pytest configurada
- ✅ `tests/conftest.py` — Fixtures globales
- ✅ `tests/test_domain/test_types.py` — 21/21 tests passing
- ✅ `tests/test_llm/test_client.py` — 12/12 tests passing
- ✅ `tests/test_memory/test_gateway.py` — Tests implementados
- ✅ `tests/test_router/test_classifier.py` — Tests implementados
- ✅ `tests/test_agents/test_validator.py` — Tests implementados
- ⚠️ Coverage actual: 70% (54/77 tests passing)
- ⚠️ Faltan fixes en TaskType names (CODE_DEBUG → CODE_DEBUGGING, etc.)

---

## [0.0.1] — 2026-03-06

### Added

- Estructura inicial de proyecto AXIOMA (91 archivos totales)
- Configuración centralizada con Pydantic Settings
- Dominio puro: tipos, entidades, excepciones, contratos
- Sistema de fases con criterios de aceptación verificables
- Entorno virtual fish-compatible en `venv/`
- Modelos LLM soportados: llama3.2:3b, qwen2.5-coder:7b, llava:7b, atla/selene-mini:q4_k_m
- Sistema de memoria en 3 capas (short-term, long-term, semantic)
- Router de intenciones con classifier + cache
- 4 agentes especializados (coder, researcher, analyst, validator)
- UI Web con FastAPI + HTML/CSS/JS
- CLI con soporte rich colors
- Benchmarks de razonamiento y clasificación
- 77 tests automatizados con pytest

### Changed

- Versión inicial: `0.0.1` (según esquema de versionado nuevo)
- `src/domain/types.py` consolidado con 35 TaskTypes operativos
- `config/settings.py` con validación estricta de timeout (≤25s)
- `requirements.txt` actualizado a 90 dependencias exactas
- `numpy` corregido de 2.4.0 (yanked) a 2.3.5 (estable)
- Progreso total: 96.7% (88/91 archivos con contenido)

### Fixed

- `main.py` corrige imports: `SettingsError` → `ConfigurationError`
- `main.py` corrige atributos: `default_model` → `llm_default_model`
- `main.py` corrige atributos: `default_temperature` → eliminado
- `main.py` corrige paths: `Paths.BACKUPS_DIR` → `Paths.DATA_DIR / "backups"`
- `classifier.py` corrige TaskTypes: `CODE_DEBUG` → `CODE_DEBUGGING`
- `classifier.py` corrige TaskTypes: `WEB_RESEARCH` → `WEB_SEARCH`
- `classifier.py` corrige TaskTypes: `VALIDATION` → `QUALITY_VALIDATION`
- `validator.py` corrige TaskTypes: `VALIDATION` → `QUALITY_VALIDATION`
- Rutas absolutas resueltas en `config/paths.py` para portabilidad
- Manejo de errores con jerarquía `AxiomaError` para debugging claro

### Security

- `.gitignore` actualizado para bloquear `.env`, `venv/`, `logs/`, `data/`
- Advertencias explícitas sobre no versionar `.env` con keys reales
- Generación segura de `SECRET_KEY` documentada en README
- APIs externas configuradas como opcionales (tavily, serper, google, news)

### Performance

- Circuit breaker para proteger contra fallos en cascada
- Retry exponencial con backoff para LLM requests
- Cache L1/L2 para decisiones de ruteo (TTL 300s)
- Embeddings con fallback: Ollama → sentence-transformers → hash
- Benchmark de razonamiento: 100% precisión (5/5 preguntas)
- Benchmark de modelos: 3 modelos testeados, 9/9 tests exitosos

---

## Convenciones de Versionado

| Tipo | Versión | Criterio |
|------|---------|----------|
| **PATCH** | `0.0.1` → `0.0.2` | Fix grande o corrección crítica |
| **MINOR** | `0.0.1` → `0.1.0` | Nuevo archivo o feature completo |
| **MAJOR** | `0.x.x` → `1.0.0` | Release estable con API pública |

> **Nota:** AXIOMA está en desarrollo activo. La API pública se estabilizará en v1.0.0.

---

## Enlaces

- [Repositorio](https://github.com/saintwick/axioma)
- [Documentación](docs/)
- [Reporte de Estructura](docs/STRUCTURE_REPORT_v3.md)
- [Comandos Disponibles](COMANDOS_AXIOMA.txt)

---

*Última actualización: 2026-03-06 | AXIOMA v0.0.1*
