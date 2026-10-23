#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — INSTALADOR DE UN SOLO ARCHIVO (Linux)
# ═══════════════════════════════════════════════════════════════
# ESTE es el archivo que se descarga del Release. Hace TODO, sin clonar el repositorio, sin Python
# y sin saber de programación:
#   1. Comprueba las herramientas del sistema y Docker.
#   2. Descarga AXIOMA y lo deja en una carpeta tuya (`~/.axioma/app`).
#   3. Le pasa el control al instalador del proyecto, que crea las carpetas y tu `.env`, arma la
#      imagen, deja el ACCESO DIRECTO en el menú y lo enciende abriendo el navegador.
#
# Uso:
#   bash axioma-instalar.sh                      # instala y arranca
#   bash axioma-instalar.sh --dry-run            # muestra qué haría, sin tocar nada
#   bash axioma-instalar.sh --solo-descargar     # baja y descomprime, y se detiene (para revisar)
#   AXIOMA_VERSION=v0.7.5 bash axioma-instalar.sh      # fija la versión (por defecto: la última)
#   AXIOMA_DESTINO=~/axioma bash axioma-instalar.sh    # otra carpeta (por defecto: ~/.axioma/app)
#
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo) · 2 = uso incorrecto
set -euo pipefail

# El repositorio NO va escrito acá: el producto que se distribuye no lleva la identidad del autor
# (regla del proyecto, con guardián en `test_axioma_28`). El CI lo fija al armar el Release,
# cambiando `__REPO__` por el repositorio real.
REPO="${AXIOMA_REPO:-__REPO__}"
VERSION="${AXIOMA_VERSION:-main}"
DESTINO="${AXIOMA_DESTINO:-$HOME/.axioma/app}"
DRY=false
SOLO_DESCARGAR=false
for arg in "$@"; do
  case "$arg" in
    --dry-run|--ensayo)  DRY=true ;;
    --solo-descargar)    SOLO_DESCARGAR=true ;;
    -h|--help)           sed -n '3,20p' "$0"; exit 0 ;;
    *) echo "⚠️  No conozco el interruptor '$arg' (probá --help)"; exit 2 ;;
  esac
done

paso() { printf '\n▶ %s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
aviso(){ printf '  ⚠️  %s\n' "$*"; }
mal()  { printf '  ❌ %s\n' "$*"; }

# Un repositorio válido siempre es `dueno/nombre`. Si no tiene `/`, este archivo es la plantilla (el
# marcador `__REPO__` lo reemplaza el CI al armar el Release) — o alguien lo pasó mal a mano.
case "$REPO" in
  */*) ;;
  *) mal "A este archivo le falta el repositorio (es la plantilla, no el instalador del Release)."
     echo "     Usá el que se descarga del Release, o fijalo a mano así:"
     echo "       AXIOMA_REPO=dueno/repositorio bash $0"
     exit 4 ;;
esac

paso "AXIOMA — instalación desde un solo archivo"
echo "   Versión a instalar: $VERSION"
echo "   Carpeta destino:    $DESTINO"
if $DRY; then aviso "modo ensayo: no voy a descargar ni a tocar nada"; fi

# ── 1. Herramientas del sistema ─────────────────────────────────────────────
paso "Paso 1 de 5: herramientas del sistema"
DESCARGADOR=""
if command -v curl >/dev/null 2>&1; then
  DESCARGADOR="curl"
elif command -v wget >/dev/null 2>&1; then
  DESCARGADOR="wget"
else
  mal "No tenés ni 'curl' ni 'wget' para descargar AXIOMA."
  echo "     Debian/Ubuntu:  sudo apt install curl"
  echo "     Fedora:         sudo dnf install curl"
  echo "     Arch:           sudo pacman -S curl"
  exit 4
fi
if ! command -v tar >/dev/null 2>&1; then
  mal "Falta 'tar' para descomprimir lo que se descarga."
  exit 4
fi
ok "Voy a usar $DESCARGADOR y tar"

# ── 2. Docker ───────────────────────────────────────────────────────────────
paso "Paso 2 de 5: Docker (es el programa que ejecuta AXIOMA)"
if ! command -v docker >/dev/null 2>&1; then
  mal "Docker no está instalado."
  echo "     Instalalo desde https://www.docker.com/products/docker-desktop"
  echo "     (o el de tu distribución) y volvé a ejecutar este archivo."
  exit 4
fi
if ! docker info >/dev/null 2>&1; then
  mal "Docker está instalado pero no responde."
  echo "     Abrilo, o arrancalo con:  sudo systemctl start docker"
  echo "     Después volvé a ejecutar este archivo."
  exit 4
fi
ok "Docker responde ($(docker --version | cut -d, -f1))"

# ── 3. Descarga ─────────────────────────────────────────────────────────────
URL="https://github.com/$REPO/archive/$VERSION.tar.gz"
paso "Paso 3 de 5: descargando AXIOMA"
echo "   $URL"
TEMPORAL=""
if $DRY; then
  echo "   (ensayo) lo bajaría a un temporal y lo descomprimiría en $DESTINO"
else
  TEMPORAL="$(mktemp -d)"
  trap 'rm -rf "$TEMPORAL"' EXIT
  if [ "$DESCARGADOR" = "curl" ]; then
    curl -fL --retry 3 --progress-bar "$URL" -o "$TEMPORAL/axioma.tar.gz" || {
      mal "No pude descargar AXIOMA."
      echo "     Revisá tu conexión, o fijá una versión:  AXIOMA_VERSION=v0.7.5 bash $0"
      exit 4; }
  else
    wget -q --show-progress -O "$TEMPORAL/axioma.tar.gz" "$URL" || {
      mal "No pude descargar AXIOMA."
      echo "     Revisá tu conexión, o fijá una versión:  AXIOMA_VERSION=v0.7.5 bash $0"
      exit 4; }
  fi
  ok "Descargado ($(du -h "$TEMPORAL/axioma.tar.gz" | cut -f1))"
fi

# ── 4. Descomprimir ─────────────────────────────────────────────────────────
paso "Paso 4 de 5: dejando AXIOMA en su carpeta"
if $DRY; then
  echo "   (ensayo) quedaría en $DESTINO, sin tocar tu .env ni tus datos"
else
  mkdir -p "$TEMPORAL/extraido"
  tar -xzf "$TEMPORAL/axioma.tar.gz" -C "$TEMPORAL/extraido"
  ORIGEN="$(find "$TEMPORAL/extraido" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [ -z "$ORIGEN" ] || [ ! -f "$ORIGEN/instalar/instalar_axioma.sh" ]; then
    mal "Lo que se descargó no tiene la forma esperada de AXIOMA."
    echo "     ¿La versión '$VERSION' existe? Probá:  AXIOMA_VERSION=v0.7.5 bash $0"
    exit 4
  fi
  mkdir -p "$DESTINO"
  # Se copia ENCIMA a propósito: el código se actualiza y tu `.env`, tus datos y tus registros quedan
  # intactos (no vienen en el archivo descargado, así que `cp -a` no los toca).
  cp -a "$ORIGEN/." "$DESTINO/"
  ok "AXIOMA quedó en $DESTINO"
  if [ -f "$DESTINO/.env" ]; then ok "Tu .env ya existía: lo dejé exactamente como estaba"; fi
fi

if $SOLO_DESCARGAR; then
  paso "Listo (solo descarga)"
  echo "   Para instalarlo ahora:   cd \"$DESTINO\" && bash instalar/instalar_axioma.sh"
  exit 0
fi

# ── 5. La instalación de verdad (la hace el instalador del proyecto) ────────
paso "Paso 5 de 5: instalando (carpetas, .env, imagen, acceso directo y arranque)"
if $DRY; then
  echo "   (ensayo) ejecutaría:  $DESTINO/instalar/instalar_axioma.sh"
  echo
  ok "Ensayo terminado: no se descargó ni se instaló nada."
  exit 0
fi
cd "$DESTINO"
# ✅ 2026-10-23: el instalador del proyecto intenta bajar la imagen publicada (más rápido) y, si no
# puede, construye como siempre. Le pasamos la versión y el repositorio para que sepa de dónde
# bajarla; sin esto sólo construiría.
export AXIOMA_VERSION="$VERSION"
export AXIOMA_REPO="$REPO"
bash instalar/instalar_axioma.sh
