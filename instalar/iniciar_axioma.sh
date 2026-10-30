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

# 2026-10-30 (MEDIDO en Windows): si falta el plugin Compose v2, `docker compose ...` cae al CLI de
# `docker` y el flag siguiente se interpreta como propio ("unknown shorthand flag: 'd' in -d").
# Se usa el Compose que funcione; si no hay ninguno, se explica en vez de fallar raro.
detectar_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    COMPOSE=()
  fi
}
COMPOSE=(docker compose)

paso() { printf '\n▶ %s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
aviso(){ printf '  ⚠️  %s\n' "$*"; }
mal()  { printf '  ❌ %s\n' "$*"; }

if $DETENER; then
  detectar_compose
  paso "Apagando AXIOMA"
  if $DRY; then echo "  (ensayo) ${COMPOSE[*]} down"; exit 0; fi
  cd "$RAIZ"
  "${COMPOSE[@]}" down
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
detectar_compose
if [ ${#COMPOSE[@]} -eq 0 ]; then
  mal "Docker Compose no está disponible (Docker lo necesita para armar y encender AXIOMA)."
  echo "     Probá:  docker compose version"
  echo "     Docker Desktop lo incluye; si tenés otro 'docker' en el PATH, quitálo o actualizá Docker Desktop."
  exit 4
fi
ok "Compose: ${COMPOSE[*]}"

# ── 2. ¿Usamos el Ollama del equipo o el que trae AXIOMA? ───────────────────
paso "Buscando Ollama"
OLLAMA_HOST_EFECTIVO="${AXIOMA_OLLAMA_HOST:-127.0.0.1:11434}"
if curl -s --max-time 3 "http://${OLLAMA_HOST_EFECTIVO}/api/tags" >/dev/null 2>&1; then
  SERVICIOS="axioma"
  ok "Ya tenés Ollama andando en ${OLLAMA_HOST_EFECTIVO}: uso ese (no hace falta otro)."
else
  SERVICIOS="axioma ollama"
  aviso "No encontré Ollama en ${OLLAMA_HOST_EFECTIVO}."
  ok "Levanto el Ollama que viene con AXIOMA (los modelos quedan guardados en un volumen)."
fi

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
  # da verdadero igual para una carpeta: sin este chequeo el arranque no hacía nada (o copiaba la
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
paso "Encendiendo AXIOMA"
if $DRY; then
  echo "  (ensayo) cd $RAIZ && ${COMPOSE[*]} up -d ${SERVICIOS}"
  echo "  (ensayo) abriría $URL cuando responda"
  exit 0
fi
cd "$RAIZ"
# El código de salida se MIRA (como en el guion de Windows): sin esto, `set -e` cortaba el guion con el
# error crudo de Docker y la persona no sabía que hacer. Medido el 2026-10-06: pasa cuando ya hay otro
# AXIOMA andando (el nombre del contenedor está en uso) o cuando el motor de Docker no está sano.
# ✅ 2026-10-27: mismo caso que en Windows (medido en una instalación real): si el ayudante de
# credenciales de Docker está roto, `compose up` muere al bajar `ollama/ollama` con
# "error getting credentials". El instalador ya lo esquiva al bajar la imagen de AXIOMA; acá se
# reintenta UNA vez con un DOCKER_CONFIG temporal sin credsStore (imágenes públicas).
# 2026-10-27: mismo caso que en Windows (medido en una instalación real): si el ayudante de
# credenciales de Docker está roto, `compose up` muere al bajar `ollama/ollama` con
# "error getting credentials". El instalador ya lo esquiva al bajar la imagen de AXIOMA; acá se
# reintenta UNA vez con un DOCKER_CONFIG temporal sin credsStore (las imágenes son públicas).
log_up=$(mktemp)
up_ok=0
if ! "${COMPOSE[@]}" up -d ${SERVICIOS} >"$log_up" 2>&1; then
  if grep -qiE "credential|error getting credentials" "$log_up"; then
    aviso "Docker no encontró su ayudante de credenciales: reintento sin él (las imágenes son públicas)."
    cfg_tmp=$(mktemp -d)
    printf '{"auths":{}}' > "$cfg_tmp/config.json"
    if DOCKER_CONFIG="$cfg_tmp" "${COMPOSE[@]}" up -d ${SERVICIOS} >>"$log_up" 2>&1; then
      up_ok=1
    fi
    rm -rf "$cfg_tmp"
  fi
else
  up_ok=1
fi
if grep -qiE "credential" "$log_up" 2>/dev/null; then
  grep -i "credential" "$log_up" | head -1
fi
cat "$log_up"
rm -f "$log_up"
if [ "$up_ok" -ne 1 ]; then
  mal "Docker no pudo encender AXIOMA (el motivo está en las líneas de arriba)."
  if grep -qiE "credential" "$log_up" 2>/dev/null; then
    echo "     El ayudante de credenciales de Docker esta roto: quitá la clave credsStore de"
    echo "     ~/.docker/config.json (o instalá docker-credential-pass) y volvé a intentar."
  fi
  echo "     Lo más común: ya hay un AXIOMA andando (mirá con:  docker ps) o Docker no está sano."
  echo "     Para ver el detalle:  docker compose logs --tail 40 axioma"
  exit 4
fi
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
