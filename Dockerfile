# ═══════════════════════════════════════════════════════════════
# AXIOMA — imagen de contenedor (Fase 1 del plan, `docs/PLAN_CONTENEDORES.md` §3.3)
# ═══════════════════════════════════════════════════════════════
# Decisiones que aplica este archivo:
#   · **Sin modelos** dentro de la imagen: los modelos son volúmenes (Ollama del equipo o uno
#     propio del contenedor). La imagen sólo lleva código y dependencias.
#   · **Sólo CPU**: torch se instala del índice CPU de PyTorch, en el mismo orden que el CI
#     (si no, PyPI arrastra la versión con CUDA y la imagen engorda varios GB).
#   · **Python 3.14** (el MISMO que el entorno de desarrollo y el del usuario): hasta el 2026-10-07 la
#     imagen quedaba en 3.12 y el desarrollo corría en 3.14 — dos entornos distintos, que es justamente
#     donde aparecen las sorpresas. Decisión del usuario (2026-10-07): una sola versión, 3.14.
#   · **Dos etapas**: las herramientas de compilación no viajan a la imagen final.
#   · **Sin usuario root** para ejecutar: el proceso corre como `axioma` (uid 1000).
#
# Nota de mantenimiento: el tamaño final hay que MEDIRLO (`docker images`) y anotarlo en el
# plan; si crece mucho, la palanca es separar torch/sentence-transformers en su propia capa.

# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14

# ═══════════════════════════════════════════════════════════════
# ETAPA 1 — dependencias (con herramientas de compilación)
# ═══════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS dependencias

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Librerías del sistema que el proyecto necesita de verdad: audio (PortAudio/sndfile) y
# ffmpeg para multimedia. Las de compilación quedan sólo en esta etapa.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libsndfile1 \
        portaudio19-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Entorno virtual propio: es lo que se copia a la etapa final (nada de arrastrar /usr/local)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt requirements-ci.txt ./

# ORDEN IMPORTANTE (igual que en `.github/workflows/ci.yml`):
#   1. torch/torchaudio del índice CPU   2. el resto de las dependencias
#   3. sentence-transformers al final (su dependencia de torch ya está satisfecha)
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu \
           torch==2.11.0+cpu torchaudio==2.11.0+cpu \
    && pip install -r requirements-ci.txt \
    && pip install sentence-transformers

# ═══════════════════════════════════════════════════════════════
# ETAPA 2 — imagen final (sin herramientas de compilación)
# ═══════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # (2026-10-07) Acá se declaraba `AXIOMA_EN_CONTENEDOR=1` y NINGÚN módulo la leía: lo que el contenedor
# necesita ya se pasa explícito (`--no-browser`, `AXIOMA_RAFAEL_DIRECTO`, `AXIOMA_SIN_AUDIO`, rutas de
# logs). Una idea, una bandera: la que se usa es `AXIOMA_SIN_AUDIO`.
AXIOMA_SIN_AUDIO=1

# Sólo las librerías de ejecución (sin build-essential)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libportaudio2 \
        # ✅ ISSUE-150 (MEDIDO): para el servicio de VOZ hace falta el puente de ALSA hacia el
        # servidor de sonido del equipo (PulseAudio/PipeWire). Sin esto, dentro del contenedor el
        # lazo de escucha fallaba en bucle: primero `No input device matching 'pulse'` (el nombre
        # del equipo no existe acá) y después `Invalid sample rate` al abrir la placa directamente.
        libasound2-plugins \
        libpulse0 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 axioma

COPY --from=dependencias /opt/venv /opt/venv

WORKDIR /app
# El código. `data/`, `logs/`, `docs/`, `venv/`, `.git/` y `deepseek-harness/` quedan fuera por
# `.dockerignore`; los datos entran como volúmenes (ver docker-compose.yml).
COPY --chown=axioma:axioma . .

# Lo que el proyecto ESPERA que exista, medido corriendo la suite dentro de la imagen (3 puntos
# del checklist): sus propias comprobaciones verifican `docs/`, `cache/` y `.env`.
#   · `docs/` y `cache/` van VACÍOS: son documentación generada y caché de ejecución.
#   · `.env` se arma desde `.env.example` (lo mismo que hace el CI): son los valores
#     DOCUMENTADOS por defecto, y las comprobaciones del proyecto verifican que estén. La
#     configuración del USUARIO no viaja en la imagen (`.dockerignore` excluye `.env`): entra por
#     variables de entorno o por el `.env` montado, y esas tienen prioridad sobre este archivo.
# El `chown -R` de /app además evita que el usuario `axioma` no pueda escribir su caché de pruebas.
RUN mkdir -p /app/data /app/logs /app/docs /app/cache \
    && if [ -f /app/.env.example ]; then cp /app/.env.example /app/.env; else : > /app/.env; fi \
    && chown -R axioma:axioma /app

USER axioma
# ✅ 2026-10-08: la imagen se puede correr con el UID del equipo (`user:` en el compose) para que los
# archivos de `data/` y `logs/` queden TUYOS y no de otro usuario. Medido: sin esto, con un UID distinto
# de 1000 el contenedor no podía escribir en ninguna carpeta (`/app/data`, `/app/logs`, `/app/cache`) ni
# en `src/` (Python no podía dejar su `__pycache__` y la configuración no se importaba).
# Se abren SÓLO las carpetas de datos, no el código.
RUN mkdir -p data logs cache && chmod -R 0777 data logs cache
# Python no escribe `__pycache__` en el código: evita ensuciar el árbol y el error de permisos.
ENV PYTHONDONTWRITEBYTECODE=1
# ✅ 2026-10-08: lo que baja HuggingFace (el modelo de embeddings, medido 4,3 GB) va a `cache/`, que en
# el compose es volumen del equipo: se baja UNA vez y sobrevive a recrear el contenedor.
ENV HF_HOME=/app/cache/huggingface
# ✅ 2026-10-08: el código tiene que ser LEGIBLE para cualquier UID. Medido: 1622 archivos del proyecto
# estaban en modo 600 (los escribe así la herramienta del autor) y al copiarse a la imagen quedaban
# ilegibles para un contenedor que corre con otro UID → `import config.settings` explotaba con
# `PermissionError` sobre `src/utils/degradation.py` y AXIOMA no arrancaba. Esto lo garantiza en la
# imagen sin depender de cómo estén los permisos en el equipo donde se compila.
RUN chmod -R a+rX /app

# La interfaz web escucha en 8080 (el puerto de la configuración es 8000; en el contenedor se
# publica el 8080 para no chocar con el del equipo).
EXPOSE 8080

# Salud = el propio preflight: si falta Ollama o el modelo de chat, el contenedor se declara
# "no sano" en vez de aparentar que anda (misma convención de `ISSUE-146`: 0 = puede responder).
HEALTHCHECK --interval=60s --timeout=30s --start-period=40s --retries=3 \
    CMD python -m src.core.preflight --json > /dev/null 2>&1 || exit 1

VOLUME ["/app/data", "/app/logs"]

# `--host 0.0.0.0` es imprescindible dentro del contenedor (si no, sólo escucha en su localhost)
CMD ["python", "main.py", "--web", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
