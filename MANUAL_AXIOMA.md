# 🧠 MANUAL DE AXIOMA — Sistema multi-agente local-first

**AXIOMA** es un ecosistema de agentes de IA que corre 100% local sobre
**Ollama** (sin nube): chat, generación de código, clima, dólar, noticias,
búsqueda web, archivos y memoria persistente por usuario.

---

## 0. Instalación rápida (con contenedor) — la vía recomendada

0. **La interfaz queda en tu equipo**: AXIOMA escucha en `127.0.0.1:8080` por defecto (no en la red
   local) y la clave de las sesiones es propia de tu equipo (`data/.storage_secret`, permisos 600).
   Para exponerlo a la red local hay que pedirlo a propósito: `AXIOMA_WEB_HOST=0.0.0.0`. En ese caso
   **no hay que generar ningún token a mano**: AXIOMA genera un código de acceso y lo guarda en
   `data/.web_token` (permisos **0600**), lo muestra en la consola y en un banner de la interfaz.
   Protege las rutas que escriben el `.env` (`POST /api/v1/preflight/claves` y
   `POST /api/v1/preflight/modelos`); se manda en el header **`X-AXIOMA-Token`** (ausente o inválido
   → **401**). Para rotarlo: `python main.py --rotar-token`. `AXIOMA_WEB_TOKEN` (entorno o `.env`)
   sigue siendo la opción avanzada para fijarlo a mano. El resto de la API no tiene autenticación.

   ```bash
   # el código se genera solo al arrancar expuesto; también se puede leer del archivo
   export AXIOMA_WEB_TOKEN="$(cat data/.web_token)"
   curl -X POST http://127.0.0.1:8080/api/v1/preflight/claves \
     -H "X-AXIOMA-Token: $AXIOMA_WEB_TOKEN" -H "Content-Type: application/json" \
     -d '{"TAVILY_API_KEY": "tvly-tu-clave"}'
   ```
1. **Docker** instalado y en ejecución ([Docker Desktop](https://www.docker.com/products/docker-desktop));
   en **Windows** además **WSL 2**.
2. **Bajar el instalador de la página de _Releases_** (versión **v0.7.5**, lo más simple):
   - **Windows**: `AXIOMA-Setup.exe` — doble clic (necesita Docker Desktop con WSL 2).
   - **Linux y macOS**: `axioma-instalar.sh` — `bash axioma-instalar.sh`.

   Baja la **imagen ya compilada desde GHCR** (GitHub Container Registry) y deja un acceso directo
   «AXIOMA» en el menú Inicio (y en el Escritorio, en Windows). **Alternativa con el código fuente**:
   descargar el proyecto y ejecutar **una sola vez** `instalar/instalar_axioma.sh` (doble clic en Linux);
   en **Windows** es `instalar/instalar_axioma.ps1`:
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\instalar\instalar_axioma.ps1`. Ahí el puerto
   es 8080 fijo (no hay `network_mode: host`) y la **voz del contenedor no está disponible**: existe un
   **demonio nativo** que usa la voz de Windows (`instalar/rafael_windows.ps1`, ver `MANUAL_RAFAEL.md`).
   **El instalador pregunta cómo vas a usar AXIOMA** (1: sólo esta computadora —recomendado—, 2: otros
   equipos de tu casa/oficina, 3: desde internet) y guarda `AXIOMA_WEB_HOST` en el `.env`; en el primer
   arranque del CLI aparece el mismo aviso, una sola vez (marcador `data/.primer_arranque`).
3. De ahí en adelante, **doble clic en el icono «AXIOMA»**: se enciende y se abre en el navegador.
4. La primera vez, AXIOMA **pide crear tu usuario** (no hay ninguno predefinido) y abre la pantalla
   **🧩 Configurar AXIOMA**, que dice qué falta y lo instala con un clic.

Para el uso diario alcanza con eso. Lo que sigue (secciones 1 en adelante) es el detalle **técnico** y la
vía de **desarrollo** (correr AXIOMA sin contenedor, desde el código).

**Memoria (RAM)**: 9 GB mínimo para funcionar · **16 GB** recomendado · 24-32 GB ideal. Con placa de video
suficiente, el modelo más pesado se aloja en la placa y AXIOMA trabaja **en paralelo**.

---

## 1. Requisitos (desarrollo, sin contenedor)

- **Python** 3.10+ (el proyecto usa 3.14).
- **Ollama** corriendo en `127.0.0.1:11434` con los modelos:
  - `qwen3:8b` (chat/default, 4,87 GB) · `qwen2.5-coder:7b` (código, 4,68 GB)
  - `huihui_ai/qwen2.5-coder-abliterate:7b` (respaldo de código, 4,68 GB) · `qwen3-vl:4b`
    (visión, 3,30 GB) · `piper:es_AR-daniela-high` (voz, 0,11 GB)
  - Fuente única de esta lista: `config/model_catalog.yaml` (tamaños **medidos**). Para saber qué
    conviene en tu equipo: `python -m src.core.preflight --recomendar` (y `--modelo NOMBRE` para
    consultar uno que no esté en el catálogo)
  - `BAAI/bge-m3` (embeddings — se carga localmente vía sentence-transformers,
    **no** hace falta en Ollama).
- **Tesseract** (opcional, mejora el OCR de texto en imágenes):
  `sudo apt install tesseract-ocr tesseract-ocr-spa` — si falta, el OCR
  simplemente lo hace el modelo de visión (más lento).
- **PortAudio** (opcional, sólo para voz): si falta la librería del sistema, los módulos de
  `src/multimodal/` **se importan igual** (quedan sin dispositivo de audio) y los tests de voz se
  saltean. La voz no arranca hasta instalarla (`sudo apt install libportaudio2`); el chat sigue igual.
- Verificación rápida: `python main.py --check`.

## 2. Comandos principales (CLI)

```bash
python main.py --check              # Diagnóstico del sistema
python main.py --chat               # Chat interactivo en terminal
python main.py --web                # Interfaz web (NiceGUI) → http://127.0.0.1:8000
python main.py --web --host 0.0.0.0 --port 8080   # Servidor accesible en red (bind explícito)
python main.py --backup             # Respaldo ESTRUCTURAL: sólo el JSON en data/backups/
python -m src.interfaces.cli.main --backup --full
                                    # + .tar.gz con data/memory (LanceDB) + data/knowledge
# --full: retención máximo 3 backups (rota el par {json, tar} junto), aviso si AXIOMA
# está corriendo y escritura atómica del tar. `main.py --backup` sólo crea el JSON.
```

## 3. Interfaz web (`python main.py --web`)

### 3.1 Ventana de usuarios (primera pantalla)
- Aparecen los usuarios existentes como **tarjetas clicables**. Tocás uno y se
  habilita el chat.
- **El primer usuario que creás** es el administrador (`root`); los que se crean después son
  **"➕ Usuario nuevo"** (por ahora solo se completa el **nombre**; correo,
  lugar y país están deshabilitados) → **Crear** → volvés a la lista y elegís
  el usuario nuevo.
- La memoria (conversaciones y búsquedas) queda **aislada por usuario**.

### 3.2 Chat
Escribís y AXIOMA enruta la tarea automáticamente (clasificador de intención):

| Pedido | Ejemplo |
|---|---|
| Pregunta general | "¿Qué es una red neuronal?" |
| Generar código | "Creá una función que ordene una lista en Python" |
| Clima | "¿Qué tiempo hace en Buenos Aires?" |
| Dólar | "¿A cuánto está el dólar hoy?" |
| Noticias | "Buscá noticias de tecnología" |
| Búsqueda web | "Buscá información sobre X" |
| **Archivos** | "buscá el archivo settings" · "leé el archivo README y resumilo" · "citá un bloque del archivo .env" |
| **Cuentas** | "¿cuánto es 15% de 4200?" · "¿(2+3)*4?" (calculadora local) |
| **Imagen adjunta** | subís una foto con 📎 y preguntás "¿qué dice acá?" (visión) |

### 3.3 Panel 📁 Archivos (explorar, leer y adjuntar)
Botón **📁** en la barra superior — diálogo con dos formas de encontrar archivos:
- **🔍 Buscar por nombre** dentro del proyecto → resultados con ruta exacta.
- **📂 Explorá carpetas**: navegás por el sistema (🔼 subir, click en carpetas;
  arranca en la raíz del proyecto; oculta `.git`, `venv`, dotfiles y carpetas pesadas).
- Sobre cualquier archivo (de búsqueda o del explorador):
  **📄 Leer** (contenido) · **📝 Resumen** (LLM, ~1 min en CPU) · **💬 Cita** (bloque,
  instantáneo) · **📎 Adjuntar al chat** (lo deja listo como si lo hubieras subido
  con 📎; escribí tu instrucción y enviá).
- **Workspaces**: el código que AXIOMA ejecuta por sesión vive en una carpeta por `session_id`. Ese
  id sólo admite letras, números, `_` y `-` (1 a 64 caracteres); una ruta absoluta, `..` o un
  separador se rechaza, y el `cleanup` no puede salir del directorio base.

### 3.4 Adjuntar archivos e imágenes (📎 en el chat)
- **Texto / PDF / DOCX**: se adjuntan y su contenido se lee al enviar (PDF y
  DOCX se parsean; tope ~150K caracteres con aviso si se trunca).
- **Imágenes** (PNG/JPG/WebP/GIF/BMP): AXIOMA las analiza con `qwen3-vl` —
  describe la imagen, responde preguntas sobre ella y **lee texto** (OCR):
  si pedís "copiá/transcribí el texto" usa Tesseract local (instantáneo) y si
  no, el modelo. Primera imagen tarda ~45-55 s en CPU (carga el modelo).
  Si el modelo de chat está ocupando la RAM, el canal lo libera solo antes de
  cargar la visión; cada análisis queda registrado en logs (eventos VISION_CHAT).
- **Fuentes/citas**: cuando la respuesta sale de tus archivos (búsqueda
  grounded), aparece el bloque **"📎 Fuentes (N)"** con los archivos usados —
  tanto en la web como en la terminal.

### 3.5 Otros botones
- **🟢/🟡/🔴 (badge de salud)** → estado del sistema de un vistazo (degradaciones,
  errores de la API, feedback negativo). Verde = sano, amarillo = degradado,
  rojo = errores de contrato en rutas críticas. El tooltip explica el motivo.
- **📁 Archivos** → explorar, leer y adjuntar archivos del proyecto.
- **🏥 Estado** → estado de servicios/modelos (dice si falta la API key).
- **🖥️ Mi Hard** → hardware detectado, qué modelos entran y ranking de instalados.
- **📄 Exportar** → investigación (respuestas con citas) y **conversación
  completa** (con el código generado) en Markdown / HTML / JSON.
- **🗂️ Sesiones** → listar, buscar, exportar y borrar sesiones (no deja borrar la
  sesión en uso).
- **⚙️ Configuración** → modelo, temperatura, validación, modo Jarvis.
- **🗑️ Limpiar chat** → borra la conversación visible.
- **🤖 Modo Jarvis** → activa la voz (Rafael) desde la web.
- **🚪 Salir** → apaga el servidor.

### 3.6 Panel «¿Por qué esta respuesta?»
Debajo de cada respuesta hay un colapsable con **modelo, tarea, agente que
atendió, confianza, estado de validación, fuentes y tiempo**: es la información
para decidir si confiar en la respuesta. Ahí mismo se **califica (1 a 5)** con
comentario y etiquetas — esa calificación **entrena al sistema**
(`FeedbackLoop` → `QualityMonitor`), no queda solo en un log.

## 4. Memoria (Capa 2)

- Cada mensaje se guarda por **usuario** y por **sesión** (short-term en RAM,
  long-term en SQLite, semántico en LanceDB).
- **Cross-sesión**: al preguntar, AXIOMA inyecta mensajes relevantes de
  sesiones anteriores del **mismo usuario** (búsqueda semántica con BGE-M3).
- La ventana de la sesión activa siempre se mantiene; el contexto de sesiones
  pasadas se agrega como refuerzo (máx. `CROSS_SESSION_LIMIT`=3 mensajes).
- La identidad del usuario sale de la ventana de selección o de
  `AXIOMA_USER_ID` en `.env`.

## 5. Archivos importantes

| Ruta | Qué es |
|---|---|
| `.env` | Configuración (modelos, claves, umbrales, user id) — **nunca subir** |
| `config/settings.py` | Settings validados (Pydantic) |
| `config/multimodal.yaml` | Config de voz (STT/TTS/VAD) |
| `data/` | Bases de datos, memoria vectorial, backups |
| `logs/` | Logs del sistema (incl. `rafael.log`, reportes de tests) |
| `tools/` | Utilidades (benchmark, diagnóstico, commit) |

## 6. Solución de problemas

| Problema | Solución |
|---|---|
| "Ollama no responde" | `ollama serve` debe estar corriendo; probá `curl http://127.0.0.1:11434/api/tags` |
| Modelo no encontrado | `ollama pull qwen3:8b` (y los otros según la tarea) |
| Poca RAM / OOM | El MemoryGuard evita cargar modelos que no caben; calibrá con `python -m tools.benchmark_calibration` |
| Embeddings lentos | Es CPU: la primera carga de BGE-M3 tarda; después usa cache L1 |
| El servidor web no abre | Verificá que el puerto esté libre; `--port` para cambiarlo |

## 7. Tests y verificación

```bash
venv/bin/python tools/run_all_tests.py --timeout 150   # suite completa (26 archivos, 1191 tests) + reporte
venv/bin/python tools/quality_gate.py                  # gate de calidad (15 condiciones)
python -m pytest tests/            # pytest directo (suite del proyecto)
AXIOMA_VISION_E2E=1 python -m pytest tests/test_axioma_11_vision_imagenes.py -q   # visión real (requiere Ollama, ~7 min)
python tools/vision_battery.py     # batería de visión (13 casos sintéticos)
python tools/log_health_report.py  # health report desde logs (verdicto + anomalías)
python axioma_auditor.py --full    # auditoría + SCORE AXIOMA /100
python axioma_auditor.py --format json -o logs/audit_summary.json  # resumen para el gate
python tests/test_audio_diagnostic.py   # Diagnóstico de voz (Rafael)
python -m tools.benchmark_calibration   # Calibración de hardware/modelos
```

---

*AXIOMA v0.7.5*
