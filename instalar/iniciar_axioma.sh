#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — INICIAR (pensado para DOBLE CLIC: sin terminal)
# ═══════════════════════════════════════════════════════════════
# Qué hace, en orden:
#   1. Comprueba que Docker esté instalado y respondiendo (y si no, lo explica en castellano).
#   2. Decide solo si usar el Ollama del equipo o levantar el de AXIOMA:
#        · si el equipo YA tiene Ollama andando  → usa ese (no levanta otro: no gasta disco ni choca)
#        · si NO tiene                          → levanta el Ollama que viene con AXIOMA
#   3. Enciende AXIOMA, espera a que responda y ABRE EL NAVEGADOR en la pantalla de configuración.
#
# Uso:
#   ./instalar/iniciar_axioma.sh            # enciende y abre el navegador
#   ./instalar/iniciar_axioma.sh --detener  # apaga todo
#   ./instalar/iniciar_axioma.sh --dry-run  # muestra qué haría, sin tocar nada
#
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo)
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUERTO="${AXIOMA_PUERTO:-8080}"
URL="http://127.0.0.1:${PUERTO}/"
DRY=false
DETENER=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    --detener) DETENER=true ;;
    *) echo "Opción no reconocida: $arg"; exit 2 ;;
  esac
done

paso() { printf '\n▶ %s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
aviso(){ printf '  ⚠️  %s\n' "$*"; }
mal()  { printf '  ❌ %s\n' "$*"; }

if $DETENER; then
  paso "Apagando AXIOMA"
  if $DRY; then echo "  (ensayo) docker compose --profile ollama-local down"; exit 0; fi
  cd "$RAIZ"
  docker compose --profile ollama-local down
  ok "AXIOMA apagado. Tus datos y tus claves quedan guardados en la carpeta del proyecto."
  exit 0
fi

# ── 1. ¿Está Docker? ────────────────────────────────────────────────────────
paso "Comprobando Docker"
if ! command -v docker >/dev/null 2>&1; then
  mal "No tenés Docker instalado (es el programa que ejecuta AXIOMA)."
  echo "     Instalalo desde https://www.docker.com/products/docker-desktop y volvé a hacer doble clic acá."
  exit 4
fi
if ! docker info >/dev/null 2>&1; then
  mal "Docker está instalado pero no responde."
  echo "     Abrí Docker Desktop (o revisá el servicio) y volvé a intentar."
  echo "     Si dice 'permiso denegado', agregá tu usuario al grupo docker y volvé a entrar."
  exit 4
fi
ok "Docker responde"

# ── 2. ¿Usamos el Ollama del equipo o el que trae AXIOMA? ───────────────────
paso "Buscando Ollama"
OLLAMA_HOST_EFECTIVO="${AXIOMA_OLLAMA_HOST:-127.0.0.1:11434}"
if curl -s --max-time 3 "http://${OLLAMA_HOST_EFECTIVO}/api/tags" >/dev/null 2>&1; then
  PERFIL=""
  ok "Ya tenés Ollama andando en ${OLLAMA_HOST_EFECTIVO}: uso ese (no hace falta otro)."
else
  PERFIL="--profile ollama-local"
  aviso "No encontré Ollama en ${OLLAMA_HOST_EFECTIVO}."
  ok "Levanto el Ollama que viene con AXIOMA (los modelos quedan guardados en un volumen)."
fi

paso "Encendiendo AXIOMA"
if $DRY; then
  echo "  (ensayo) cd $RAIZ && docker compose ${PERFIL} up -d"
  echo "  (ensayo) abriría $URL cuando responda"
  exit 0
fi
cd "$RAIZ"
# shellcheck disable=SC2086  # PERFIL puede ir vacío a propósito
docker compose ${PERFIL} up -d
ok "Contenedores encendidos"

# ── 3. Esperar y abrir el navegador ─────────────────────────────────────────
paso "Esperando a que AXIOMA responda"
for _ in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 "$URL"; then
    ok "AXIOMA está en línea: $URL"
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 || aviso "No pude abrir el navegador: entrá a mano a $URL"
    else
      aviso "Abrí tu navegador en $URL"
    fi
    paso "Listo"
    echo "  Si en la pantalla dice que falta algo (modelos, claves), AXIOMA mismo lo explica y lo instala."
    echo "  Para apagarlo: doble clic otra vez, o  ./instalar/iniciar_axioma.sh --detener"
    exit 0
  fi
  sleep 2
done
mal "AXIOMA no respondió en un minuto."
echo "     Mirá qué dice con:  docker compose logs --tail 40 axioma"
exit 4
