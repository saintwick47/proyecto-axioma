#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# AXIOMA — Verificación de la imagen de contenedor (checklist §2.4)
# ═══════════════════════════════════════════════════════════════
# Fase 1 del plan de contenedores (`docs/PLAN_CONTENEDORES.md` §2.4 y §9.9).
#
# Para qué: los cuatro puntos del checklist que NO se pueden comprobar sin contenedor, en un
# solo comando y con los números medidos (nada de "debería andar"):
#   1. compila la imagen (y falla si la compilación falla)
#   2. MIDE el tamaño de la imagen (riesgo declarado en el plan: "imagen gigante")
#   3. corre la SUITE DENTRO de la imagen (que la compilación falle si el portero bloquea)
#   4. prueba el contenedor SIN AUDIO y con Python 3.12 (uso real, no sólo pruebas)
#
# Uso:
#   tools/verificar_imagen.sh              # hace todo
#   tools/verificar_imagen.sh --solo-plan  # muestra los pasos y sale (no necesita Docker)
#
# Códigos de salida: 0 = todo bien · 4 = falta algo (Docker, o la verificación no pasó)
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGEN="${AXIOMA_IMAGEN:-axioma:local}"
SOLO_PLAN=false
[[ "${1:-}" == "--solo-plan" ]] && SOLO_PLAN=true

paso() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
mal()  { printf '  ❌ %s\n' "$*"; }

PASOS=(
  "docker build -t $IMAGEN .                              # compila (dos etapas, sin modelos)"
  "docker images $IMAGEN --format '{{.Size}}'             # MIDE el tamaño (riesgo del plan)"
  "docker run --rm $IMAGEN python tools/run_all_tests.py  # la SUITE dentro de la imagen"
  "docker run --rm $IMAGEN python -V                      # Python 3.12 en uso real"
  "docker run --rm $IMAGEN python -m src.core.preflight   # sin audio: tiene que degradar solo"
)

if $SOLO_PLAN; then
  echo "Verificación de la imagen '$IMAGEN' (pasos, sin ejecutar):"
  for p in "${PASOS[@]}"; do echo "  · $p"; done
  exit 0
fi

# ── 0. ¿Se puede usar Docker? (si no, se explica en vez de fallar raro) ──
paso "Requisito: Docker"
if ! command -v docker >/dev/null 2>&1; then
  mal "no está instalado: el plan usa Docker Engine + compose (ver §9.4 del plan)"
  exit 4
fi
if ! docker info >/dev/null 2>&1; then
  mal "está instalado pero no responde: revisá el servicio ('systemctl status docker')"
  mal "si dice 'permiso denegado', agregá tu usuario al grupo docker y volvé a entrar"
  exit 4
fi
ok "$(docker --version)"

# ── 1. Compilar ──
paso "Compilando la imagen (sin modelos: son volúmenes)"
inicio=$(date +%s)
docker build -t "$IMAGEN" "$RAIZ"
ok "compilada en $(( $(date +%s) - inicio )) s"

# ── 2. Medir ──
paso "Tamaño de la imagen"
tamano=$(docker images "$IMAGEN" --format '{{.Size}}' | head -1)
ok "tamaño: ${tamano:-desconocido} (medido con 'docker images')"

# ── 3. Suite dentro de la imagen ──
paso "Suite DENTRO de la imagen (si falla, la imagen no sirve)"
if docker run --rm "$IMAGEN" python tools/run_all_tests.py --timeout 150 >/tmp/axioma_suite_imagen.log 2>&1; then
  ok "$(grep -E 'RESUMEN' /tmp/axioma_suite_imagen.log | tail -1)"
else
  mal "la suite falló dentro de la imagen (registro: /tmp/axioma_suite_imagen.log)"
  tail -5 /tmp/axioma_suite_imagen.log
  exit 4
fi

# ── 4. Python y audio ──
paso "Python en uso real dentro del contenedor"
version=$(docker run --rm "$IMAGEN" python -V 2>&1)
ok "$version (el proyecto probó 3.12 y 3.14)"

paso "Sin audio (contenedor sin dispositivos): tiene que degradar solo"
if docker run --rm "$IMAGEN" python -m src.core.preflight >/tmp/axioma_preflight_imagen.log 2>&1; then
  ok "el preflight corre y dice que puede responder"
elif grep -q "no puede responder" /tmp/axioma_preflight_imagen.log; then
  ok "el preflight corre y explica qué falta (Ollama del equipo o modelos)"
else
  mal "el preflight falló dentro del contenedor (registro: /tmp/axioma_preflight_imagen.log)"
  tail -5 /tmp/axioma_preflight_imagen.log
  exit 4
fi
grep -E "Audio \(voz\)|Placa de video|Memoria RAM" /tmp/axioma_preflight_imagen.log || true

paso "Verificación terminada"
ok "tamaño ${tamano:-?} · $version · suite dentro de la imagen ✅"
echo "  Anotá el tamaño en docs/PLAN_CONTENEDORES.md §9.9 (medición del riesgo 'imagen gigante')."
