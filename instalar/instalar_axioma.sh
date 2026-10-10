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
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  ok "Creé tu .env a partir de la plantilla (ahí se guardan tus claves, y no se publican)"
fi
paso "Armando AXIOMA (la primera vez tarda unos minutos)"
if $DRY; then
  echo "  (ensayo) cd $RAIZ && docker compose build"
else
  cd "$RAIZ"
  docker compose build
  ok "AXIOMA armado"
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
