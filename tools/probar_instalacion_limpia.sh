#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — PROBAR LA INSTALACIÓN EN UNA MÁQUINA LIMPIA (sin tocar la tuya)
# ═══════════════════════════════════════════════════════════════
# Qué simula: alguien que descarga el proyecto y lo instala desde cero (sin `venv`, sin `data/`, sin
# `logs/`, sin `.env`, sin `.git`). Comprueba que ESO funciona, no tu instalación.
#
# CÓMO SE ASEGURA DE NO TOCAR TU INSTALACIÓN (medido, no prometido):
#   1. Trabaja en una CARPETA TEMPORAL: nada se copia ni se escribe dentro de tu proyecto.
#   2. La imagen que arma usa OTRO nombre (`axioma-limpia:prueba`), así no pisa `axioma:local`.
#   3. Si enciende (`--encender`), lo hace con OTRO nombre de contenedor y en OTRO PUERTO (8099 por
#      defecto), y apaga y borra lo suyo al terminar.
#   4. Toma una FOTO de tu instalación antes y después (estado del contenedor `axioma`, qué escucha en
#      el 8080 y el HTTP) y compara: si algo cambió, lo dice y sale con error.
#
# Uso:
#   tools/probar_instalacion_limpia.sh                # copia + comprobaciones rápidas (30 s, sin Docker)
#   tools/probar_instalacion_limpia.sh --con-imagen   # además arma la imagen y corre la suite ADENTRO
#   tools/probar_instalacion_limpia.sh --encender     # además la enciende en otro puerto y mide
#   tools/probar_instalacion_limpia.sh --solo-plan    # muestra qué haría, sin tocar nada
#   tools/probar_instalacion_limpia.sh --conservar    # no borra la carpeta temporal al terminar
#
# Códigos de salida: 0 = todo bien · 1 = algo falló · 4 = falta algo (Docker, por ejemplo)
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUERTO_AJENO="${AXIOMA_PUERTO_PRUEBA:-8099}"
PROYECTO_PRUEBA="axioma_limpia"
CONTENEDOR_PRUEBA="axioma-prueba"
IMAGEN_PRUEBA="axioma-limpia:prueba"
CON_IMAGEN=false
ENCENDER=false
SOLO_PLAN=false
CONSERVAR=false
for arg in "$@"; do
  case "$arg" in
    --con-imagen) CON_IMAGEN=true ;;
    --encender)   ENCENDER=true; CON_IMAGEN=true ;;
    --solo-plan)  SOLO_PLAN=true ;;
    --conservar)  CONSERVAR=true ;;
    *) echo "Opción no reconocida: $arg"; exit 2 ;;
  esac
done

paso()  { printf '\n▶ %s\n' "$*"; }
ok()    { printf '  ✅ %s\n' "$*"; }
aviso() { printf '  ⚠️  %s\n' "$*"; }
mal()   { printf '  ❌ %s\n' "$*"; }
dato()  { printf '     %s\n' "$*"; }
FALLOS=0

if $SOLO_PLAN; then
  paso "Esto es lo que haría (no toco nada)"
  dato "1. Copiar el proyecto a un temporal, sin venv/ data/ logs/ .env .git/"
  dato "2. Comprobar que la copia está limpia y que los instaladores explican su plan (modo ensayo)"
  dato "3. Validar la configuración de contenedores de la copia (docker compose config)"
  if $CON_IMAGEN; then dato "4. Armar la imagen $IMAGEN_PRUEBA y correr la suite ADENTRO"; fi
  if $ENCENDER; then
    dato "5. Encender la copia como $CONTENEDOR_PRUEBA en el puerto $PUERTO_AJENO (no el 8080 tuyo)"
    dato "6. Apagarla y borrarla"
  fi
  dato "Siempre: foto de tu instalación antes y después, y comparación"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  mal "Docker no responde: sin Docker no se puede probar la instalación en contenedores."
  exit 4
fi

# ── Foto de TU instalación (para probar que no la toco) ─────────────────────────────────────────
foto() {
  local id estado salud escucha http
  id="$(docker inspect --format '{{.Id}}' axioma 2>/dev/null || echo sin-contenedor)"
  estado="$(docker inspect --format '{{.State.Status}}' axioma 2>/dev/null || echo -)"
  salud="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' axioma 2>/dev/null || echo -)"
  escucha="$(ss -ltn 2>/dev/null | awk '$4 ~ /:8080$/ {print $4}' | head -1)"
  http="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/ 2>/dev/null || echo 000)"
  printf '%s|%s|%s|%s|%s' "$id" "$estado" "$salud" "$escucha" "$http"
}

paso "Tu instalación, ANTES (para comparar después)"
ANTES="$(foto)"
dato "contenedor axioma: ${ANTES}"

# ── 1. La copia limpia ──────────────────────────────────────────────────────────────────────────
paso "Copiando el proyecto a una carpeta temporal (sin tu entorno)"
DESTINO="$(mktemp -d "/tmp/axioma_limpia_XXXXXX")"
dato "destino: $DESTINO"
if ! rsync -a \
      --exclude 'venv/' --exclude 'data/' --exclude 'logs/' --exclude '.git/' \
      --exclude '.env' --exclude '.env.local' --exclude '__pycache__/' --exclude '*.pyc' \
      --exclude 'docs/' --exclude 'cache/' --exclude '.optimizer_backups/' \
      --exclude 'deepseek-harness/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
      --exclude 'data/backups/' \
      "$RAIZ/" "$DESTINO/"; then
  mal "No pude copiar el proyecto"; exit 1
fi
ok "copia hecha"

limpieza_ok=true
for no_debe in venv data logs .env .git; do
  if [ -e "$DESTINO/$no_debe" ]; then mal "la copia NO está limpia: tiene $no_debe"; limpieza_ok=false; fi
done
$limpieza_ok && ok "la copia está limpia (sin venv, data, logs, .env ni .git)"
$limpieza_ok || FALLOS=$((FALLOS+1))

# ── 2. Los instaladores, en ensayo ──────────────────────────────────────────────────────────────
paso "Instaladores de la copia, en modo ensayo (no encienden nada)"
for guion in instalar/instalar_axioma.sh instalar/iniciar_axioma.sh; do
  salida="$(cd "$DESTINO" && bash "$guion" --dry-run 2>&1)"; codigo=$?
  if [ "$codigo" -eq 0 ]; then
    if grep -q "ensayo" <<<"$salida"; then ok "$guion: explica su plan (código 0)"
    else mal "$guion: salió 0 pero no muestra el plan"; FALLOS=$((FALLOS+1)); fi
  elif [ "$codigo" -eq 4 ] && grep -qi "docker" <<<"$salida"; then
    aviso "$guion: sin Docker en el entorno, explica qué falta (código 4) — correcto igual"
  else
    mal "$guion: código inesperado $codigo"; dato "$(tail -3 <<<"$salida")"; FALLOS=$((FALLOS+1))
  fi
done
# El lanzador tiene que haber decidido el Ollama y decir a dónde abriría.
salida_lanzador="$(cd "$DESTINO" && bash instalar/iniciar_axioma.sh --dry-run 2>&1)"
grep -qE "Ya tenés Ollama|Levanto el Ollama" <<<"$salida_lanzador" \
  && ok "el lanzador decide qué Ollama usar" || { mal "el lanzador no decidió el Ollama"; FALLOS=$((FALLOS+1)); }
grep -q "abriría http://127.0.0.1:8080" <<<"$salida_lanzador" \
  && ok "el lanzador dice a dónde abriría el navegador" || { mal "no dice la dirección"; FALLOS=$((FALLOS+1)); }

paso "La configuración de contenedores de la copia"
if (cd "$DESTINO" && docker compose config -q); then ok "docker compose config: válida"
else mal "la configuración de contenedores no valida"; FALLOS=$((FALLOS+1)); fi

# ── 3. La imagen y la suite adentro (opcional: tarda minutos) ───────────────────────────────────
if $CON_IMAGEN; then
  paso "Armando la imagen de la copia ($IMAGEN_PRUEBA: no pisa tu axioma:local)"
  if (cd "$DESTINO" && docker build -t "$IMAGEN_PRUEBA" . >/tmp/axioma_limpia_build.log 2>&1); then
    tamano="$(docker images "$IMAGEN_PRUEBA" --format '{{.Size}}' | head -1)"
    ok "imagen armada · tamaño medido: ${tamano:-desconocido}"
    version="$(docker run --rm "$IMAGEN_PRUEBA" python -V 2>&1 | tail -1)"
    ok "Python adentro: ${version:-desconocido}"
    paso "Suite completa DENTRO de la imagen de la copia"
    if docker run --rm -e OLLAMA_HOST=http://127.0.0.1:1 "$IMAGEN_PRUEBA" \
         python tools/run_all_tests.py --timeout 150 >/tmp/axioma_limpia_suite.log 2>&1; then
      ok "la suite pasa dentro de la copia limpia ($(grep -oE '[0-9]+/[0-9]+ archivos OK' /tmp/axioma_suite_imagen.log 2>/dev/null || grep -oE '[0-9]+/[0-9]+ archivos OK' /tmp/axioma_limpia_suite.log | tail -1))"
    else
      mal "la suite falló dentro de la imagen (registro: /tmp/axioma_limpia_suite.log)"
      dato "$(grep -E 'FAIL' /tmp/axioma_limpia_suite.log | head -3)"
      FALLOS=$((FALLOS+1))
    fi
  else
    mal "no se pudo armar la imagen (registro: /tmp/axioma_limpia_build.log)"
    FALLOS=$((FALLOS+1))
  fi
fi

# ── 4. Encenderla en otro puerto (opcional) ─────────────────────────────────────────────────────
if $ENCENDER; then
  paso "Encendiendo la COPIA en el puerto $PUERTO_AJENO (el tuyo, el 8080, no se toca)"
  # Se genera un archivo de ajustes SÓLO en la copia: otro nombre de contenedor y otro puerto. La red
  # del equipo se mantiene para que la copia llegue al Ollama del equipo (127.0.0.1:11434).
  cat > "$DESTINO/docker-compose.prueba.yml" <<YAML
services:
  axioma:
    container_name: $CONTENEDOR_PRUEBA
    command: ["python", "main.py", "--web", "--host", "127.0.0.1", "--port", "$PUERTO_AJENO", "--no-browser"]
    ports: []
YAML
  if (cd "$DESTINO" && docker compose -p "$PROYECTO_PRUEBA" -f docker-compose.yml \
        -f docker-compose.prueba.yml up -d >/tmp/axioma_limpia_up.log 2>&1); then
    ok "contenedor $CONTENEDOR_PRUEBA encendido"
    listo=false
    for _ in $(seq 1 30); do
      if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PUERTO_AJENO/" || true)" = "200" ]; then listo=true; break; fi
      sleep 2
    done
    if $listo; then
      ok "la copia responde en http://127.0.0.1:$PUERTO_AJENO (medido)"
      dato "Python adentro: $(docker exec "$CONTENEDOR_PRUEBA" python -V 2>&1 | tail -1)"
      # Que lo que escribe sea del usuario del equipo (el UID real, no el de la imagen).
      docker exec "$CONTENEDOR_PRUEBA" sh -c 'echo prueba > /app/logs/prueba_limpia.txt' 2>/dev/null
      if [ -f "$DESTINO/logs/prueba_limpia.txt" ]; then
        ok "lo que escribe en la copia queda de: $(stat -c '%U' "$DESTINO/logs/prueba_limpia.txt")"
      fi
      # Y tu instalación sigue escuchando en el 8080, sin enterarse.
      if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8080/ || true)" = "200" ]; then
        ok "tu AXIOMA (8080) sigue respondiendo mientras la copia corría"
      else
        mal "tu AXIOMA (8080) dejó de responder durante la prueba"; FALLOS=$((FALLOS+1))
      fi
    else
      mal "la copia no respondió en $PUERTO_AJENO (registro: /tmp/axioma_limpia_up.log)"
      dato "$(tail -3 /tmp/axioma_limpia_up.log)"
      FALLOS=$((FALLOS+1))
    fi
  else
    mal "no se pudo encender la copia"; dato "$(tail -3 /tmp/axioma_limpia_up.log)"; FALLOS=$((FALLOS+1))
  fi
  paso "Apagando y borrando lo de la prueba"
  (cd "$DESTINO" && docker compose -p "$PROYECTO_PRUEBA" -f docker-compose.yml \
     -f docker-compose.prueba.yml down >/dev/null 2>&1) || true
  docker rm -f "$CONTENEDOR_PRUEBA" >/dev/null 2>&1 || true
  ok "apagada"
fi

# ── 5. Que TU instalación esté igual que antes ──────────────────────────────────────────────────
paso "Tu instalación, DESPUÉS (comparación)"
DESPUES="$(foto)"
dato "contenedor axioma: ${DESPUES}"
if [ "$ANTES" = "$DESPUES" ]; then
  ok "tu instalación quedó IGUAL (mismo contenedor, mismo estado y mismo HTTP)"
else
  mal "tu instalación CAMBIÓ durante la prueba"
  dato "antes:   $ANTES"
  dato "después: $DESPUES"
  FALLOS=$((FALLOS+1))
fi

# ── Limpieza ────────────────────────────────────────────────────────────────────────────────────
if $CONSERVAR; then
  aviso "Dejo la carpeta temporal para que la mires: $DESTINO"
else
  rm -rf "$DESTINO"
  ok "carpeta temporal borrada"
fi
if $CON_IMAGEN; then
  docker rmi "$IMAGEN_PRUEBA" >/dev/null 2>&1 && ok "imagen de prueba borrada" || aviso "no pude borrar $IMAGEN_PRUEBA"
fi

printf '\n'
if [ "$FALLOS" -eq 0 ]; then
  echo "TODO BIEN: la copia limpia instala, explica su plan y (si se pidió) enciende y responde."
  exit 0
fi
echo "$FALLOS comprobación(es) fallaron."
exit 1
