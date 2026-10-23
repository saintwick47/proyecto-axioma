#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — INSTALAR (una sola vez, pensado para DOBLE CLIC)
# ═══════════════════════════════════════════════════════════════
# Qué hace:
#   1. Comprueba Docker (y lo explica en castellano si falta).
#   2. Arma la imagen de AXIOMA (la primera vez tarda unos minutos).
#   3. Deja un ACCESO DIRECTO en tu menú de aplicaciones ("AXIOMA"), así después
#      arrancás con doble clic y nunca más tocás la terminal.
#   4. Enciende AXIOMA y abre el navegador.
#
# Uso:
#   ./instalar/instalar_axioma.sh             # instalar y arrancar
#   ./instalar/instalar_axioma.sh --sin-acceso  # sin crear el acceso directo
#   ./instalar/instalar_axioma.sh --dry-run   # muestra qué haría, sin tocar nada
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIN_ACCESO=false
DRY=false
for arg in "$@"; do
  case "$arg" in
    --sin-acceso) SIN_ACCESO=true ;;
    --dry-run) DRY=true ;;
    *) echo "Opción no reconocida: $arg"; exit 2 ;;
  esac
done

paso() { printf '\n▶ %s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
aviso(){ printf '  ⚠️  %s\n' "$*"; }
mal()  { printf '  ❌ %s\n' "$*"; }

paso "AXIOMA — instalación (una sola vez)"
echo "   Carpeta del proyecto: $RAIZ"

# ── 1. Docker ───────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  mal "Docker no está disponible."
  echo "     Instalá Docker Desktop desde https://www.docker.com/products/docker-desktop"
  echo "     (es el programa que ejecuta AXIOMA; después volvé a hacer doble clic acá)."
  exit 4
fi
ok "Docker responde ($(docker --version | cut -d, -f1))"

# ── 2. Imagen ───────────────────────────────────────────────────────────────
# ── Los datos del equipo para el audio (UID, GID y grupo `audio`) ───────────
# Medido el 2026-10-07: el compose tenía 996 fijo para el grupo de audio y `/run/user/1000` para el
# socket del servidor de sonido. En Debian/Ubuntu el grupo `audio` es 29 y en Fedora 63; y si tu UID no
# es 1000 el socket no existe. Con esos valores fijos la voz no ve los dispositivos y el motivo no se
# entiende. Se calculan acá y se exportan: el compose los toma con `${AXIOMA_...}`.
# (El instalador calcula los mismos tres valores: son dos guiones sueltos y NO comparten código a
#  propósito, para que cada uno se pueda leer y ejecutar solo.)
AXIOMA_UID="$(id -u)"; export AXIOMA_UID
AXIOMA_GID="$(id -g)"; export AXIOMA_GID
AXIOMA_AUDIO_GID="$(getent group audio 2>/dev/null | cut -d: -f3 || true)"
AXIOMA_AUDIO_GID="${AXIOMA_AUDIO_GID:-996}"; export AXIOMA_AUDIO_GID
ok "Audio del equipo: UID ${AXIOMA_UID} · GID ${AXIOMA_GID} · grupo audio ${AXIOMA_AUDIO_GID}"

# ✅ 2026-10-08: las carpetas que el contenedor escribe tienen que existir ANTES de encenderlo y ser
# TUYAS. Medido: en una instalación nueva, Docker las crea como `root`, y como el contenedor corre con tu
# UID (fase 5) no puede escribir → `PermissionError: /app/data/memory` y AXIOMA no arranca.
mkdir -p data logs cache

# ✅ 2026-10-09: el `.env` tiene que EXISTIR antes de encender: es el archivo donde la interfaz guarda
# las claves del usuario y el compose lo monta. Si no existe, Docker crea una CARPETA con ese nombre y el
# montaje falla (medido en la documentación de Docker, y es un clásico). Se parte de la plantilla.
if [ -d .env ]; then
  # 2026-10-12 (ISSUE-147): Docker crea una CARPETA `.env` cuando falta el archivo, y `[ ! -f .env ]`
  # da verdadero igual para una carpeta: sin este chequeo el instalador no hacía nada (o copiaba la
  # plantilla ADENTRO) y el montaje seguía roto. Vacía la sacamos (la creó Docker); con contenido,
  # avisamos y no borramos nada (R16).
  if [ -z "$(ls -A .env 2>/dev/null)" ]; then
    rmdir .env && cp .env.example .env &&
      ok "Había una CARPETA vacía .env (la crea Docker cuando falta el archivo): la saqué y creé tu .env"
  else
    mal "Hay una CARPETA llamada .env con contenido: el montaje va a fallar."
    echo "     Mirá qué tiene:  ls -la .env"
    echo "     Si no te sirve, borrala:  rm -rf .env"
    exit 4
  fi
elif [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  ok "Creé tu .env a partir de la plantilla (ahí se guardan tus claves, y no se publican)"
fi
paso "Armando AXIOMA (la primera vez tarda unos minutos)"
# ✅ 2026-10-23: primero se intenta BAJAR la imagen publicada (tarda mucho menos que construirla).
# El repositorio de la imagen viene de AXIOMA_REPO (lo estampa el CI en el instalador de un solo
# archivo) y el tag de AXIOMA_VERSION; si no hay versión, o el tag no existe, se prueba `latest`.
# Si tampoco se puede, se cae al build de siempre: nunca se deja al usuario sin instalación.
IMAGEN_GHCR="${AXIOMA_IMAGEN:-}"
if [ -z "$IMAGEN_GHCR" ] && [ -n "${AXIOMA_REPO:-}" ]; then
  IMAGEN_GHCR="ghcr.io/$(printf '%s' "$AXIOMA_REPO" | tr '[:upper:]' '[:lower:]')"
fi
TAGS_IMAGEN=""
if [ -n "${AXIOMA_VERSION:-}" ] && [ "$AXIOMA_VERSION" != "main" ]; then
  TAGS_IMAGEN="$AXIOMA_VERSION"
fi
case " $TAGS_IMAGEN " in
  *" latest "*) ;;
  *) TAGS_IMAGEN="${TAGS_IMAGEN:+$TAGS_IMAGEN }latest" ;;
esac
if $DRY; then
  if [ -n "$IMAGEN_GHCR" ]; then
    echo "  (ensayo) bajaría la imagen de AXIOMA ($IMAGEN_GHCR con tag $TAGS_IMAGEN) y la etiquetaría axioma:local"
    echo "  (ensayo) si no se puede: cd $RAIZ && docker compose build"
  else
    echo "  (ensayo) cd $RAIZ && docker compose build"
  fi
else
  cd "$RAIZ"
  BAJADA=false
  if [ -n "$IMAGEN_GHCR" ]; then
    aviso "Bajando la imagen de AXIOMA (más rápido)..."
    # La imagen es pública: un DOCKER_CONFIG temporal sin credenciales evita que `docker pull`
    # tropiece con el mismo ayudante roto que el build (`docker-credential-desktop`).
    CFG_PULL="${TMPDIR:-/tmp}/axioma-docker-config"
    mkdir -p "$CFG_PULL" && printf '{"auths":{}}\n' > "$CFG_PULL/config.json"
    LOG_PULL="${TMPDIR:-/tmp}/axioma-pull.log"
    for TAG in $TAGS_IMAGEN; do
      if DOCKER_CONFIG="$CFG_PULL" docker pull "$IMAGEN_GHCR:$TAG" 2>&1 | tee "$LOG_PULL"; then
        if DOCKER_CONFIG="$CFG_PULL" docker tag "$IMAGEN_GHCR:$TAG" axioma:local; then
          BAJADA=true
          break
        fi
      fi
    done
    if $BAJADA; then
      ok "Imagen bajada (AXIOMA listo, sin construir)"
    else
      aviso "No pude bajarla; la construyo (tarda unos minutos)."
    fi
  fi
  if ! $BAJADA; then
    # ✅ 2026-10-22: antes se decía «AXIOMA armado» sin mirar el código de salida. Ahora se captura el
    # build y, si falla por el ayudante de credenciales, se reintenta con un DOCKER_CONFIG temporal:
    # AXIOMA sólo usa imágenes públicas y no necesita credenciales. (`set -o pipefail` ya está activo.)
    LOG_BUILD="${TMPDIR:-/tmp}/axioma-build.log"
    if docker compose build 2>&1 | tee "$LOG_BUILD"; then
      ok "AXIOMA armado"
    elif grep -qiE "docker-credential|error getting credentials" "$LOG_BUILD" 2>/dev/null; then
      aviso "Docker no encontró su ayudante de credenciales. AXIOMA usa sólo imágenes públicas: reintento con una configuración temporal."
      CFG_TMP="${TMPDIR:-/tmp}/axioma-docker-config"
      mkdir -p "$CFG_TMP" && printf '{"auths":{}}\n' > "$CFG_TMP/config.json"
      if DOCKER_CONFIG="$CFG_TMP" docker compose build 2>&1 | tee "$LOG_BUILD"; then
        ok "AXIOMA armado (con configuración temporal de Docker)"
      else
        mal "No se pudo armar la imagen. El log quedó en $LOG_BUILD"
        exit 1
      fi
    else
      mal "No se pudo armar la imagen. El log quedó en $LOG_BUILD"
      exit 1
    fi
  fi
fi

# ── 3. Acceso directo en el menú ────────────────────────────────────────────
if ! $SIN_ACCESO; then
  paso "Creando el acceso directo en tu menú de aplicaciones"
  DESTINO="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  ARCHIVO="$DESTINO/axioma.desktop"
  if $DRY; then
    echo "  (ensayo) escribiría $ARCHIVO"
  else
    mkdir -p "$DESTINO"
    # El acceso directo NO abre una terminal: arranca y abre el navegador, nada más.
    cat > "$ARCHIVO" <<ACCESO
[Desktop Entry]
Type=Application
Name=AXIOMA
Comment=Asistente de IA local: arranca AXIOMA y abre la pantalla de configuración
Exec=$RAIZ/instalar/iniciar_axioma.sh
Icon=utilities-terminal
Terminal=false
Categories=Utility;Science;
ACCESO
    chmod +x "$ARCHIVO"
    ok "Acceso creado: $ARCHIVO (aparece como «AXIOMA» en tu menú)"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESTINO" >/dev/null 2>&1 || true
  fi
fi

# ── 4. Arrancar ─────────────────────────────────────────────────────────────
paso "Arrancando AXIOMA"
if $DRY; then
  echo "  (ensayo) $RAIZ/instalar/iniciar_axioma.sh"
  exit 0
fi
"$RAIZ/instalar/iniciar_axioma.sh"
