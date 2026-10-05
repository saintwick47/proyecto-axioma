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
