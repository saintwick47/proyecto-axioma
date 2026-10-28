#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — DESINSTALAR (para empezar de cero sin dejar basura)
# ═══════════════════════════════════════════════════════════════
# Qué hace, en orden:
#   1. Apaga y borra los contenedores de AXIOMA (sin tocar otros contenedores).
#   2. Borra las imágenes locales de AXIOMA (`axioma:local` y la de ghcr).
#   3. Borra los accesos directos del menú y del escritorio.
#   4. Deja TUS DATOS por defecto (data/, logs/, .env) y borra la carpeta del proyecto.
#      Con --todo borra también esos datos y los volúmenes (modelos descargados).
#
# Uso:
#   ./instalar/desinstalar_axioma.sh            # pide confirmación
#   ./instalar/desinstalar_axioma.sh --si       # sin preguntar
#   ./instalar/desinstalar_axioma.sh --todo     # además borra datos y modelos
#   ./instalar/desinstalar_axioma.sh --dry-run  # muestra qué haría, sin tocar nada
#
# Códigos: 0 = bien · 2 = opción inválida · 3 = cancelado
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TODO=false
SI=false
DRY=false
for arg in "$@"; do
  case "$arg" in
    --todo) TODO=true ;;
    --si|-y|--yes) SI=true ;;
    --dry-run) DRY=true ;;
    *) echo "Opción no reconocida: $arg"; exit 2 ;;
  esac
done

paso() { printf '\n> %s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
aviso(){ printf '  [!]  %s\n' "$*"; }
mal()  { printf '  [X]  %s\n' "$*"; }

paso "AXIOMA - desinstalación"
printf '   carpeta: %s\n' "$RAIZ"
if $TODO; then
  printf '   modo: TODO (contenedores, imágenes, accesos, carpeta, DATOS y MODELOS)\n'
else
  printf '   modo: normal (contenedores, imágenes, accesos y carpeta; TUS DATOS se conservan)\n'
fi

if $DRY; then
  printf '  (ensayo) docker compose ... down --remove-orphans%s\n' "$($TODO && echo ' -v' || true)"
  printf '  (ensayo) docker rmi axioma:local y ghcr.io/...:vX\n'
  printf '  (ensayo) borraría accesos directos\n'
  printf '  (ensayo) borraría %s%s\n' "$RAIZ" "$($TODO && echo ' (incluye data/, logs/ y .env)' || true)"
  exit 0
fi

if ! $SI; then
  printf '\n   ¿Seguir? [s/N]: '
  read -r respuesta || respuesta=""
  case "$respuesta" in
    s|S|si|SI|sí|SÍ) ;;
    *) echo "   Cancelado."; exit 3 ;;
  esac
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  paso "Apagando y borrando los contenedores de AXIOMA"
  if [ -f "$RAIZ/docker-compose.yml" ]; then
    if $TODO; then
      docker compose --profile puente --profile ollama-local down --remove-orphans -v \
        && ok "contenedores y volúmenes de AXIOMA borrados" || aviso "no se pudieron borrar del todo"
    else
      docker compose --profile puente --profile ollama-local down --remove-orphans \
        && ok "contenedores de AXIOMA borrados (volúmenes conservados)" || aviso "no se pudieron borrar del todo"
    fi
  else
    aviso "no encontré docker-compose.yml en $RAIZ"
  fi
  paso "Borrando las imágenes de AXIOMA"
  for imagen in axioma:local; do
    if docker image inspect "$imagen" >/dev/null 2>&1; then
      docker rmi "$imagen" >/dev/null 2>&1 && ok "imagen $imagen borrada" || aviso "no pude borrar $imagen"
    fi
  done
  ghcr=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^ghcr\.io/.+/:v[0-9]' || true)
  for imagen in $ghcr; do
    docker rmi "$imagen" >/dev/null 2>&1 && ok "imagen $imagen borrada" || aviso "no pude borrar $imagen"
  done
else
  aviso "Docker no responde: se saltean contenedores e imágenes"
fi

paso "Borrando los accesos directos"
for acceso in "$HOME/.local/share/applications/axioma.desktop" \
              "$HOME/Escritorio/AXIOMA.desktop" "$HOME/Desktop/AXIOMA.desktop"; do
  if [ -e "$acceso" ]; then rm -f "$acceso" && ok "borrado $acceso"; fi
done

paso "Borrando la carpeta de AXIOMA"
if $TODO; then
  rm -rf "$RAIZ" && ok "borrado $RAIZ (incluye data/, logs/ y .env)"
else
  # Conservar data/ y .env: se mueven a un lado para no perderlos.
  respaldo="$HOME/axioma-datos-$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$respaldo"
  for cosa in data .env; do
    if [ -e "$RAIZ/$cosa" ]; then mv "$RAIZ/$cosa" "$respaldo/" 2>/dev/null || cp -a "$RAIZ/$cosa" "$respaldo/" 2>/dev/null || true; fi
  done
  rm -rf "$RAIZ"
  ok "carpeta borrada; tus datos quedaron en $respaldo"
fi

paso "Listo"
printf '   Para volver a instalar: bajá AXIOMA-Setup.exe del Release (o el .sh) y ejecutalo.\n'
