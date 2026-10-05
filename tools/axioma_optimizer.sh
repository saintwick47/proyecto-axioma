#!/usr/bin/env bash
# ============================================================================
# AXIOMA Optimizer Pro v1.3 — CachyOS Edition (Correcciones completas)
# - Detección de venv activo (VIRTUAL_ENV) + fallback a .venv/venv/env
# - Detección de scheduler scx compatible CachyOS 7.0+ (3 métodos)
# - Sintaxis scxctl corregida: scxctl start --sched rusty
# - Rollback con switch/start fallback
# - NUEVO: Inyección de variables en ollama.service (systemd del sistema)
# Uso: ./axioma_optimizer.sh [--apply|--rollback|--status|--check-deps|--install-deps|--dry-run]
# ============================================================================
set -euo pipefail
IFS=$'\n\t'

# === CONFIGURACIÓN ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ✅ ISSUE-147: la raíz por defecto es la carpeta PADRE del guion (antes
# `$HOME/Escritorio/axioma`, que asume el nombre en español y rompe en un contenedor
# con el proyecto en /app). Se puede seguir forzando con AXIOMA_ROOT=...
AXIOMA_ROOT="${AXIOMA_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
MANIFEST="$AXIOMA_ROOT/.optimizer_manifest.json"
BACKUP_DIR="$AXIOMA_ROOT/.optimizer_backups/$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$AXIOMA_ROOT/logs/optimizer_$(date +%Y%m%d_%H%M%S).log"
DRY_RUN=true
APPLY=false
ROLLBACK=false
STATUS=false
CHECK_DEPS=false
INSTALL_DEPS=false

# Colores
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; MAGENTA='\033[0;35m'
NC='\033[0m'; BOLD='\033[1m'

# ✅ ISSUE-147 (2026-10-04): `--print-root` informa la raíz que va a usar y sale, SIN
# tocar nada. Lo usa la prueba de portabilidad y sirve para diagnóstico.
for _arg in "$@"; do
    if [[ "$_arg" == "--print-root" ]]; then
        echo "$AXIOMA_ROOT"
        exit 0
    fi
done

# === LOGGING ===
mkdir -p "$(dirname "$LOG_FILE")" "$AXIOMA_ROOT/.optimizer_backups" 2>/dev/null || true
log() { echo -e "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }
info() { log "${BLUE}[INFO]${NC} $*"; }
ok() { log "${GREEN}[OK]${NC} $*"; }
warn() { log "${YELLOW}[WARN]${NC} $*"; }
err() { log "${RED}[ERROR]${NC} $*"; }

# ============================================================================
# HELPER: Detección de venv activo (compatible con VIRTUAL_ENV + .venv + venv)
# ============================================================================
get_venv_python() {
    # Método 1: VIRTUAL_ENV está seteado (venv activado con `source .venv/bin/activate`)
    if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
        echo "$VIRTUAL_ENV/bin/python"
        return 0
    fi

    # Método 2: Buscar .venv, venv, env, .env en AXIOMA_ROOT
    for venv_name in .venv venv env .env; do
        local venv_path="$AXIOMA_ROOT/$venv_name"
        if [[ -d "$venv_path" && -x "$venv_path/bin/python" ]]; then
            echo "$venv_path/bin/python"
            return 0
        fi
    done

    # Método 3: Python actual si sys.prefix != sys.base_prefix (venv activo sin VIRTUAL_ENV)
    local current_python
    current_python="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
    if [[ -n "$current_python" && -x "$current_python" ]]; then
        local is_venv
        is_venv=$("$current_python" -c "import sys; print('yes' if sys.prefix != sys.base_prefix else 'no')" 2>/dev/null || echo "no")
        if [[ "$is_venv" == "yes" ]]; then
            echo "$current_python"
            return 0
        fi
    fi

    return 1
}

get_venv_pip() {
    local python_bin
    python_bin=$(get_venv_python) || return 1
    local pip_path="${python_bin%/*}/pip"
    if [[ -x "$pip_path" ]]; then
        echo "$pip_path"
        return 0
    fi
    # Fallback: python -m pip
    echo "$python_bin -m pip"
    return 0
}

get_venv_path() {
    local python_bin
    python_bin=$(get_venv_python) || return 1
    # Retornar el directorio padre de bin/python
    dirname "$(dirname "$python_bin")"
}

# ============================================================================
# DETECCIÓN DE SCHEDULER ACTIVO (compatible CachyOS 7.0+)
# ============================================================================
get_active_scheduler() {
    # Método 1: /sys/kernel/sched/scx/scx_name (kernels antiguos con sched_ext expuesto)
    if [[ -f /sys/kernel/sched/scx/scx_name ]]; then
        cat /sys/kernel/sched/scx/scx_name 2>/dev/null || echo "none"
        return
    fi

    # Método 2: scxctl list --running (scx-tools 1.1.0+)
    if command -v scxctl >/dev/null 2>&1; then
        local running
        running=$(scxctl list --running 2>/dev/null | head -1 || true)
        if [[ -n "$running" && "$running" != "none" ]]; then
            echo "$running"
            return
        fi
    fi

    # Método 3: ps aux fallback (detectar proceso scx_*)
    local scx_proc
    scx_proc=$(ps aux 2>/dev/null | grep -E '[s]cx_(rusty|bpfland|lavd|rustland|p2dq|cosmos|cake|flash|tickless|beerland|pandemonium)' | awk '{print $11}' | head -1 || true)
    if [[ -n "$scx_proc" ]]; then
        echo "${scx_proc#scx_}"
        return
    fi

    echo "none"
}

# === MANIFIESTO (JSON para rollback exacto) ===
manifest_init() {
    if [[ ! -f "$MANIFEST" ]]; then
        cat > "$MANIFEST" << 'EOF'
{
  "version": "1.3",
  "created": "",
  "backups": [],
  "sysctl_applied": [],
  "env_vars_set": [],
  "systemd_units": [],
  "scheduler": null,
  "installed_packages": []
}
EOF
    fi
}

manifest_add_backup() {
    local original="$1" backup="$2" type="$3"
    python3 - "$MANIFEST" "$original" "$backup" "$type" << 'PY'
import json, sys, datetime
m, orig, bak, typ = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
data = json.load(open(m))
data['backups'].append({'original': orig, 'backup': bak, 'type': typ, 'ts': datetime.datetime.now().isoformat()})
json.dump(data, open(m, 'w'), indent=2)
PY
}

manifest_add_installed_package() {
    local pkg="$1" type="$2"
    python3 - "$MANIFEST" "$pkg" "$type" << 'PY'
import json, sys, datetime
m, pkg, typ = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(m))
if 'installed_packages' not in data:
    data['installed_packages'] = []
data['installed_packages'].append({'package': pkg, 'type': typ, 'ts': datetime.datetime.now().isoformat()})
json.dump(data, open(m, 'w'), indent=2)
PY
}

# === BACKUP SEGURO ===
backup_file() {
    local src="$1" type="${2:-file}"
    if [[ ! -e "$src" ]]; then
        warn "No existe: $src (skip backup)"
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    local rel_path="${src#$AXIOMA_ROOT/}"
    rel_path="${rel_path//\//__}"
    local dst="$BACKUP_DIR/${rel_path}.bak"
    if [[ -d "$src" ]]; then
        cp -r "$src" "$dst"
    else
        cp -a "$src" "$dst"
    fi
    manifest_add_backup "$src" "$dst" "$type"
    ok "Backup: $src → $dst"
}

# ============================================================================
# VERIFICACIÓN E INSTALACIÓN DE DEPENDENCIAS
# ============================================================================
declare -A SYSTEM_DEPS=(
    ["scx-scheds"]="Scheduler sched_ext para baja latencia"
    ["scx-tools"]="Herramientas scxctl para scheduler"
    ["ananicy-cpp"]="Prioridad automática de procesos"
    ["zram-generator"]="Swap comprimido en RAM"
    ["grim"]="Captura de pantalla Wayland nativa"
    ["spectacle"]="Captura de pantalla KDE"
    ["pipewire"]="Servidor de audio"
    ["wireplumber"]="Session manager para PipeWire"
    ["xdotool"]="Detección de ventana activa"
)

declare -A PYTHON_DEPS=(
    ["torch"]="PyTorch para embeddings y tensores"
    ["sentence_transformers"]="BGE-M3 para embeddings"
    ["lancedb"]="Base de datos vectorial"
    ["nicegui"]="Framework UI web"
    ["faster_whisper"]="STT (speech-to-text)"
    ["pydantic"]="Validación de datos"
    ["httpx"]="Cliente HTTP async"
    ["onnxruntime"]="Runtime para modelos ONNX"
    ["webrtcvad"]="Voice Activity Detection"
    ["PIL"]="Procesamiento de imágenes (Pillow)"
    ["ctranslate2"]="Inferencia optimizada"
    ["sounddevice"]="Captura de audio"
    ["numpy"]="Computación numérica"
    ["scipy"]="Cálculos científicos"
    ["sqlite3"]="Base de datos SQLite"
    ["yaml"]="Parseo de YAML (PyYAML)"
    ["orjson"]="JSON de alto rendimiento"
)

check_system_deps() {
    info "Verificando dependencias del sistema (pacman)..."
    local missing=()
    local installed=()

    for pkg in "${!SYSTEM_DEPS[@]}"; do
        local purpose="${SYSTEM_DEPS[$pkg]}"
        if pacman -Qi "$pkg" >/dev/null 2>&1; then
            installed+=("$pkg")
            echo -e "  ${GREEN}✓${NC} $pkg — $purpose"
        else
            missing+=("$pkg")
            echo -e "  ${YELLOW}✗${NC} $pkg — $purpose ${RED}(FALTANTE)${NC}"
        fi
    done

    echo
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "${#missing[@]} paquete(s) del sistema faltante(s): ${missing[*]}"
        return 1
    fi
    ok "Todas las dependencias del sistema están instaladas (${#installed[@]}/${#SYSTEM_DEPS[@]})"
    return 0
}

check_python_deps() {
    info "Verificando dependencias Python en venv..."

    local python_bin
    if ! python_bin=$(get_venv_python); then
        err "No se encontró ningún venv activo ni en $AXIOMA_ROOT"
        err "Activa tu venv primero: source venv/bin/activate"
        err "O crea uno: python -m venv $AXIOMA_ROOT/.venv"
        return 1
    fi

    info "Usando Python: $python_bin"

    if [[ ! -x "$python_bin" ]]; then
        err "Python del venv no es ejecutable: $python_bin"
        return 1
    fi

    local missing=()
    local installed=()

    for pkg in "${!PYTHON_DEPS[@]}"; do
        local purpose="${PYTHON_DEPS[$pkg]}"
        if "$python_bin" -c "import $pkg" 2>/dev/null; then
            installed+=("$pkg")
            echo -e "  ${GREEN}✓${NC} $pkg — $purpose"
        else
            missing+=("$pkg")
            echo -e "  ${YELLOW}✗${NC} $pkg — $purpose ${RED}(FALTANTE)${NC}"
        fi
    done

    echo
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "${#missing[@]} paquete(s) Python faltante(s): ${missing[*]}"
        return 1
    fi
    ok "Todas las dependencias Python están instaladas (${#installed[@]}/${#PYTHON_DEPS[@]})"
    return 0
}

install_system_deps() {
    info "Instalando dependencias del sistema..."
    local to_install=()

    for pkg in "${!SYSTEM_DEPS[@]}"; do
        if ! pacman -Qi "$pkg" >/dev/null 2>&1; then
            to_install+=("$pkg")
        fi
    done

    if [[ ${#to_install[@]} -eq 0 ]]; then
        ok "No hay paquetes del sistema para instalar"
        return 0
    fi

    info "Paquetes a instalar: ${to_install[*]}"

    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: sudo pacman -S --needed --noconfirm ${to_install[*]}"
        return 0
    fi

    info "Sincronizando base de datos de paquetes..."
    sudo pacman -Sy --noconfirm >/dev/null 2>&1 || true

    if sudo pacman -S --needed --noconfirm "${to_install[@]}"; then
        ok "✓ ${#to_install[@]} paquete(s) del sistema instalados correctamente"
        for pkg in "${to_install[@]}"; do
            manifest_add_installed_package "$pkg" "system"
        done

        if command -v ananicy-cpp >/dev/null 2>&1; then
            sudo systemctl enable --now ananicy-cpp.service 2>/dev/null || true
            ok "ananicy-cpp habilitado"
        fi

        return 0
    else
        err "Error instalando paquetes del sistema. Revisa el log: $LOG_FILE"
        return 1
    fi
}

install_python_deps() {
    info "Instalando dependencias Python en venv..."

    local python_bin
    if ! python_bin=$(get_venv_python); then
        err "No se encontró ningún venv activo ni en $AXIOMA_ROOT"
        err "Activa tu venv primero: source venv/bin/activate"
        return 1
    fi

    local pip_bin
    pip_bin=$(get_venv_pip) || {
        err "No se pudo encontrar pip en el venv"
        return 1
    }

    info "Usando Python: $python_bin"
    info "Usando pip: $pip_bin"

    # Mapeo de nombre importable → nombre pip
    declare -A PIP_NAMES=(
        ["PIL"]="Pillow"
        ["yaml"]="PyYAML"
        ["sentence_transformers"]="sentence-transformers"
        ["faster_whisper"]="faster-whisper"
    )

    local to_install=()
    for pkg in "${!PYTHON_DEPS[@]}"; do
        if ! "$python_bin" -c "import $pkg" 2>/dev/null; then
            local pip_name="${PIP_NAMES[$pkg]:-$pkg}"
            to_install+=("$pip_name")
        fi
    done

    if [[ ${#to_install[@]} -eq 0 ]]; then
        ok "No hay paquetes Python para instalar"
        return 0
    fi

    info "Paquetes a instalar: ${to_install[*]}"

    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: $pip_bin install ${to_install[*]}"
        return 0
    fi

    # Actualizar pip primero
    $pip_bin install --upgrade pip setuptools wheel >/dev/null 2>&1 || true

    if $pip_bin install "${to_install[@]}"; then
        ok "✓ ${#to_install[@]} paquete(s) Python instalados correctamente"
        for pkg in "${to_install[@]}"; do
            manifest_add_installed_package "$pkg" "python"
        done
        return 0
    else
        err "Error instalando paquetes Python. Revisa el log: $LOG_FILE"
        return 1
    fi
}

check_and_install_deps() {
    echo
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}🔍 VERIFICACIÓN DE DEPENDENCIAS${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo

    local sys_ok=true
    local py_ok=true

    if ! check_system_deps; then
        sys_ok=false
    fi

    echo -e "${CYAN}─────────────────────────────────────────────────────${NC}"
    echo

    if ! check_python_deps; then
        py_ok=false
    fi

    echo -e "${CYAN}─────────────────────────────────────────────────────${NC}"
    echo

    if [[ "$sys_ok" == true && "$py_ok" == true ]]; then
        ok "✅ Todas las dependencias están satisfechas — Sistema listo para optimizar"
        return 0
    fi

    if [[ "$INSTALL_DEPS" == false && "$APPLY" == false ]]; then
        warn "Hay dependencias faltantes. Usa --install-deps para instalarlas."
        return 1
    fi

    if [[ "$DRY_RUN" == true && "$APPLY" == false ]]; then
        info "DRY-RUN: Se instalarían las dependencias faltantes"
        return 0
    fi

    echo
    read -rp "$(echo -e "${YELLOW}¿Instalar dependencias faltantes ahora? [Y/n]:${NC} ")" install_choice
    install_choice="${install_choice:-Y}"

    if [[ "${install_choice,,}" =~ ^(y|yes|s|si|sí)$ ]]; then
        echo
        if [[ "$sys_ok" == false ]]; then
            install_system_deps || return 1
            echo
        fi

        if [[ "$py_ok" == false ]]; then
            install_python_deps || return 1
            echo
        fi

        ok "✅ Dependencias instaladas correctamente"
        return 0
    else
        warn "Instalación cancelada. No se pueden aplicar optimizaciones."
        return 1
    fi
}

# ============================================================================
# DETECCIÓN Y OPTIMIZACIONES
# ============================================================================
detect_environment() {
    info "Detectando entorno..."
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}Sistema:${NC} $(uname -rs)"
    echo -e "${BOLD}Kernel:${NC} $(uname -r)"

    local python_info="N/A"
    if command -v python3 >/dev/null 2>&1; then
        python_info="$(python3 --version 2>/dev/null || echo 'N/A')"
    fi
    echo -e "${BOLD}Python:${NC} $python_info"
    echo -e "${BOLD}CPU cores:${NC} $(nproc)"
    echo -e "${BOLD}RAM:${NC} $(free -h | awk '/Mem:/ {print $2}')"
    echo -e "${BOLD}Display:${NC} ${WAYLAND_DISPLAY:-X11}"
    echo -e "${BOLD}Ollama:${NC} $(command -v ollama >/dev/null && ollama --version 2>/dev/null | head -1 || echo 'no instalado')"
    echo -e "${BOLD}Scheduler activo:${NC} $(get_active_scheduler)"

    # Mostrar venv detectado
    local venv_python
    if venv_python=$(get_venv_python 2>/dev/null); then
        local venv_path
        venv_path=$(get_venv_path 2>/dev/null || echo "desconocido")
        echo -e "${BOLD}Venv activo:${NC} ${GREEN}$venv_path${NC}"
    else
        echo -e "${BOLD}Venv activo:${NC} ${RED}No detectado${NC}"
    fi

    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# ============================================================================
# NUEVO v1.3: INYECCIÓN DE VARIABLES EN SERVICIO SYSTEMD DEL SISTEMA (OLLAMA)
# ============================================================================
optimize_ollama_systemd() {
    local override_dir="/etc/systemd/system/ollama.service.d"
    local override_file="$override_dir/optimizer.conf"

    # Verificar que ollama.service existe como servicio del sistema
    if ! systemctl list-unit-files 2>/dev/null | grep -q "^ollama.service"; then
        warn "ollama.service no encontrado como servicio systemd del sistema (skip)"
        return 0
    fi

    info "Inyectando variables en servicio systemd del sistema (ollama.service)..."

    # Backup del override si ya existe
    if [[ -f "$override_file" ]]; then
        sudo cp "$override_file" "$BACKUP_DIR/ollama_optimizer.conf.bak" 2>/dev/null || true
        manifest_add_backup "$override_file" "$BACKUP_DIR/ollama_optimizer.conf.bak" "systemd_override"
    else
        manifest_add_backup "$override_file" "__nonexistent__" "systemd_override_new"
    fi

    local cores_axioma=$(( $(nproc) * 2 / 3 ))
    local tmp_conf=$(mktemp)
    cat > "$tmp_conf" << EOF
# AXIOMA Optimizer v1.3 - Override para ollama.service (servicio systemd del sistema)
# Generado: $(date -Iseconds)
# Revierte: sudo rm -rf $override_dir && sudo systemctl daemon-reload && sudo systemctl restart ollama
[Service]
# === Threading optimizado (CPU-only AMD RX 460) ===
Environment="OMP_NUM_THREADS=$cores_axioma"
Environment="MKL_NUM_THREADS=$cores_axioma"
Environment="OPENBLAS_NUM_THREADS=$cores_axioma"
Environment="NUMEXPR_NUM_THREADS=$(( cores_axioma / 2 ))"
# === Ollama tuning para 14GB RAM + CPU-only ===
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KEEP_ALIVE=10m"
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NUM_THREADS=$cores_axioma"
EOF

    sudo mkdir -p "$override_dir"
    sudo cp "$tmp_conf" "$override_file"
    sudo chmod 644 "$override_file"
    rm -f "$tmp_conf"

    # Recargar systemd y reiniciar Ollama
    sudo systemctl daemon-reload
    sudo systemctl restart ollama 2>/dev/null || true

    ok "Override systemd creado: $override_file"
    ok "Ollama reiniciado con las variables optimizadas"
    info "  → OLLAMA_MAX_LOADED_MODELS=1 (evita OOM con 14GB RAM)"
    info "  → OLLAMA_FLASH_ATTENTION=1 (mejor uso de CPU)"
    info "  → OLLAMA_NUM_THREADS=$cores_axioma (threads para inferencia)"
}

optimize_env_vars() {
    local target_file="$HOME/.config/environment.d/axioma.conf"
    mkdir -p "$(dirname "$target_file")"

    if [[ -f "$target_file" ]]; then
        backup_file "$target_file" "env"
    else
        manifest_add_backup "$target_file" "__nonexistent__" "env_new"
    fi

    local cores_axioma=$(( $(nproc) * 2 / 3 ))
    local cores_ollama=$(( $(nproc) - cores_axioma ))

    cat > "$target_file" << EOF
# AXIOMA Optimizer - Variables de entorno (servicios de usuario)
# Generado: $(date -Iseconds)
# Revierte: rm $target_file && systemctl --user daemon-reload

# === PyTorch / NumPy / SciPy (CPU-only, sin ROCm) ===
OMP_NUM_THREADS=$cores_axioma
MKL_NUM_THREADS=$cores_axioma
OPENBLAS_NUM_THREADS=$cores_axioma
NUMEXPR_NUM_THREADS=$(( cores_axioma / 2 ))
VECLIB_MAXIMUM_THREADS=$cores_axioma

# === Ollama (CPU-only AMD RX 460) ===
OLLAMA_NUM_PARALLEL=2
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KEEP_ALIVE=10m
OLLAMA_HOST=127.0.0.1:11434

# === Python 3.14 compatibility ===
PYTHONFAULTHANDLER=1
PYTHONUNBUFFERED=1

# === LanceDB / SQLite performance ===
LANCE_WRITE_BATCH_SIZE=64
SQLITE_CACHE_SIZE=-8000
EOF

    ok "Variables de entorno (usuario) configuradas en $target_file"
    info "  → OMP/MKL: $cores_axioma threads (AXIOMA Python)"
    info "  → Ollama: $cores_ollama threads (reservados)"
}

optimize_sysctl() {
    local target_file="/etc/sysctl.d/99-axioma.conf"

    if [[ $EUID -ne 0 ]]; then
        warn "Sysctl requiere sudo. Se aplicará con sudo."
    fi

    if [[ -f "$target_file" ]]; then
        sudo cp "$target_file" "$BACKUP_DIR/99-axioma.conf.bak" 2>/dev/null || true
        manifest_add_backup "$target_file" "$BACKUP_DIR/99-axioma.conf.bak" "sysctl"
    else
        manifest_add_backup "$target_file" "__nonexistent__" "sysctl_new"
    fi

    local tmp_conf=$(mktemp)
    cat > "$tmp_conf" << 'EOF'
# AXIOMA Optimizer - Sysctl para baja latencia
# Revierte: sudo rm /etc/sysctl.d/99-axioma.conf && sudo sysctl --system

# === Virtual memory (mejora para LanceDB/SQLite) ===
vm.swappiness=10
vm.vfs_cache_pressure=50
vm.dirty_ratio=15
vm.dirty_background_ratio=5

# === Network (APIs externas: Tavily, Serper, NewsAPI) ===
net.core.netdev_max_backlog=16384
net.core.somaxconn=8192
net.ipv4.tcp_fastopen=3
net.ipv4.tcp_max_syn_backlog=8192

# === File descriptors (multiples conexiones Ollama/HTTP) ===
fs.file-max=524288
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=1024

# === Scheduler (baja latencia para IA interactiva) ===
kernel.sched_autogroup_enabled=1
kernel.sched_min_granularity_ns=10000000
kernel.sched_wakeup_granularity_ns=15000000
EOF

    sudo cp "$tmp_conf" "$target_file"
    sudo sysctl --system >/dev/null 2>&1
    rm -f "$tmp_conf"

    ok "Sysctl aplicado: $target_file"
}

optimize_scheduler() {
    if ! command -v scxctl >/dev/null 2>&1; then
        warn "scxctl no disponible. Skip scheduler optimization."
        return 0
    fi

    # ✅ v1.3: Usar get_active_scheduler() compatible con CachyOS 7.0+
    local current
    current=$(get_active_scheduler)

    python3 - "$MANIFEST" "$current" << 'PY'
import json, sys
m, cur = sys.argv[1], sys.argv[2]
data = json.load(open(m))
data['scheduler'] = {'previous': cur}
json.dump(data, open(m, 'w'), indent=2)
PY

    if [[ "$current" != "rusty" ]]; then
        # ✅ v1.3: Intentar switch primero (ya hay scheduler activo), luego start
        # Sintaxis correcta scx-tools 1.1.0+: scxctl start --sched <nombre>
        if sudo scxctl switch --sched rusty >/dev/null 2>&1; then
            ok "Scheduler cambiado: $current → rusty (vía switch)"
        elif sudo scxctl start --sched rusty >/dev/null 2>&1; then
            ok "Scheduler cambiado: $current → rusty (vía start)"
        else
            warn "No se pudo cambiar scheduler a rusty"
            info "Intenta manualmente: sudo scxctl switch --sched rusty"
            info "O si no hay scheduler activo: sudo scxctl start --sched rusty"
        fi
    else
        info "Scheduler ya está en rusty (óptimo)"
    fi
}

optimize_ananicy() {
    if ! command -v ananicy-cpp >/dev/null 2>&1; then
        warn "ananicy-cpp no disponible. Skip."
        return 0
    fi

    local rules_dir="/etc/ananicy.d"
    local target_file="$rules_dir/axioma.rules"

    if [[ -f "$target_file" ]]; then
        sudo cp "$target_file" "$BACKUP_DIR/axioma.rules.bak" 2>/dev/null || true
        manifest_add_backup "$target_file" "$BACKUP_DIR/axioma.rules.bak" "ananicy"
    else
        manifest_add_backup "$target_file" "__nonexistent__" "ananicy_new"
    fi

    local tmp_rules=$(mktemp)
    cat > "$tmp_rules" << 'EOF'
# AXIOMA - Prioridades para procesos IA
{"name":"ollama","type":"Game","nice":-5,"ionice":"best-effort","ioclass":2}
{"name":"python","cgroups":"axioma","nice":-3}
{"name":"python3","cgroups":"axioma","nice":-3}
EOF

    sudo mkdir -p "$rules_dir"
    sudo cp "$tmp_rules" "$target_file"
    sudo systemctl restart ananicy-cpp 2>/dev/null || true
    rm -f "$tmp_rules"

    ok "Reglas Ananicy aplicadas para ollama/python"
}

optimize_nicegui() {
    local target_file="$AXIOMA_ROOT/config/optimizer.env"

    if [[ -f "$target_file" ]]; then
        backup_file "$target_file" "env_local"
    else
        manifest_add_backup "$target_file" "__nonexistent__" "env_local_new"
    fi

    cat > "$target_file" << 'EOF'
# AXIOMA Runtime Optimizations
# Cargar en main.py: load_dotenv('config/optimizer.env')

# NiceGUI / Uvicorn
UVICORN_WORKERS=1
UVICORN_LIMIT_CONCURRENCY=100
UVICORN_LIMIT_MAX_REQUESTS=1000

# Embedding batch (optimizado para CPU 12 cores)
EMBEDDING_BATCH_SIZE=16
EMBEDDING_MAX_WORKERS=4

# Cache L1/L2 agresivo
ROUTING_CACHE_SIZE=1000
OLLAMA_CACHE_SIZE=500

# Timeouts ajustados (sin GPU, CPU-only)
OLLAMA_TIMEOUT_STANDARD=60
OLLAMA_TIMEOUT_FIRST_LOAD=120
OLLAMA_TIMEOUT_CODE=180
EOF

    ok "Config runtime creada: $target_file"
}

optimize_zram() {
    if ! command -v zramctl >/dev/null 2>&1; then
        warn "zram no disponible. Skip."
        return 0
    fi

    local unit_file="/etc/systemd/system/axioma-zram.service"
    if [[ -f "$unit_file" ]]; then
        sudo cp "$unit_file" "$BACKUP_DIR/axioma-zram.service.bak" 2>/dev/null || true
        manifest_add_backup "$unit_file" "$BACKUP_DIR/axioma-zram.service.bak" "systemd"
    fi

    if [[ -z "$(swapon --show 2>/dev/null | grep zram)" ]]; then
        local tmp_unit=$(mktemp)
        cat > "$tmp_unit" << 'EOF'
[Unit]
Description=AXIOMA ZRAM swap
After=systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'modprobe zram; echo zstd > /sys/block/zram0/comp_algorithm; echo 4G > /sys/block/zram0/disksize; mkswap /dev/zram0; swapon -p 100 /dev/zram0'
ExecStop=/bin/bash -c 'swapoff /dev/zram0; echo 1 > /sys/block/zram0/reset'

[Install]
WantedBy=multi-user.target
EOF
        sudo cp "$tmp_unit" "$unit_file"
        sudo systemctl daemon-reload
        sudo systemctl enable --now axioma-zram.service 2>/dev/null || true
        rm -f "$tmp_unit"
        ok "ZRAM 4GB (zstd) activado como swap de alta prioridad"
    else
        info "ZRAM ya activo: $(swapon --show | grep zram)"
    fi
}

# ============================================================================
# ROLLBACK
# ============================================================================
do_rollback() {
    if [[ ! -f "$MANIFEST" ]]; then
        err "No hay manifiesto. Nada que revertir."
        return 1
    fi

    info "${BOLD}Iniciando rollback completo...${NC}"

    python3 - "$MANIFEST" << 'PY'
import json, sys, shutil, subprocess, os
from pathlib import Path

m = sys.argv[1]
data = json.load(open(m))

print(f"\n🔄 Revirtiendo {len(data['backups'])} archivos...\n")

for entry in reversed(data['backups']):
    orig = entry['original']
    bak = entry['backup']
    typ = entry['type']

    if bak == '__nonexistent__':
        if os.path.exists(orig):
            try:
                if orig.startswith('/etc/') or orig.startswith('/sys/'):
                    subprocess.run(['sudo', 'rm', '-rf', orig], check=True)
                else:
                    if os.path.isdir(orig):
                        shutil.rmtree(orig)
                    else:
                        os.remove(orig)
                print(f"  ✓ Eliminado (era nuevo): {orig}")
            except Exception as e:
                print(f"  ✗ Error eliminando {orig}: {e}")
    else:
        if not os.path.exists(bak):
            print(f"  ✗ Backup no encontrado: {bak}")
            continue
        try:
            if orig.startswith('/etc/') or orig.startswith('/sys/'):
                subprocess.run(['sudo', 'cp', '-a', bak, orig], check=True)
            else:
                if os.path.isdir(bak):
                    if os.path.exists(orig):
                        shutil.rmtree(orig)
                    shutil.copytree(bak, orig)
                else:
                    shutil.copy2(bak, orig)
            print(f"  ✓ Restaurado: {orig}")
        except Exception as e:
            print(f"  ✗ Error restaurando {orig}: {e}")

# ✅ v1.3: Restaurar scheduler con switch/start y sintaxis correcta
if data.get('scheduler') and data['scheduler']['previous']:
    prev = data['scheduler']['previous']
    if prev == 'none':
        subprocess.run(['sudo', 'scxctl', 'stop'], capture_output=True)
        print(f"  ✓ Scheduler detenido")
    else:
        # Intentar switch primero, luego start (sintaxis scx-tools 1.1.0+)
        result = subprocess.run(['sudo', 'scxctl', 'switch', '--sched', prev], capture_output=True)
        if result.returncode != 0:
            subprocess.run(['sudo', 'scxctl', 'start', '--sched', prev], capture_output=True)
        print(f"  ✓ Scheduler restaurado: {prev}")

subprocess.run(['sudo', 'sysctl', '--system'], capture_output=True)
subprocess.run(['sudo', 'systemctl', 'daemon-reload'], capture_output=True)

if 'installed_packages' in data and data['installed_packages']:
    print(f"\n⚠️  Nota: {len(data['installed_packages'])} paquetes fueron instalados:")
    for pkg in data['installed_packages']:
        print(f"  - {pkg['package']} ({pkg['type']})")
    print("  Estos NO se desinstalan automáticamente. Para removerlos:")
    print("  Sistema: sudo pacman -R <paquete>")
    print("  Python: .venv/bin/pip uninstall <paquete>")

print("\n✅ Rollback completado. Reinicia la sesión para aplicar variables de entorno.")
PY

    mv "$MANIFEST" "$MANIFEST.rolled_back_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    ok "Rollback finalizado"
}

# ============================================================================
# STATUS
# ============================================================================
show_status() {
    echo -e "\n${BOLD}📊 Estado actual de optimizaciones:${NC}\n"

    local env_file="$HOME/.config/environment.d/axioma.conf"
    if [[ -f "$env_file" ]]; then
        echo -e "${GREEN}✓${NC} Variables de entorno (usuario): ${CYAN}$env_file${NC}"
    else
        echo -e "${YELLOW}○${NC} Variables de entorno (usuario): no configuradas"
    fi

    # ✅ v1.3: Mostrar estado del override systemd de Ollama
    local ollama_override="/etc/systemd/system/ollama.service.d/optimizer.conf"
    if [[ -f "$ollama_override" ]]; then
        echo -e "${GREEN}✓${NC} Override systemd Ollama: ${CYAN}$ollama_override${NC}"
        # Verificar variables realmente cargadas en el proceso Ollama
        local ollama_pid
        ollama_pid=$(pgrep -x ollama 2>/dev/null | head -1 || true)
        if [[ -n "$ollama_pid" ]]; then
            local loaded_vars
            # ✅ FIX v1.3.1 (2026-08-27): grep -c con 0 matches sale con
            # código 1 → el `|| echo "0"` duplicaba el "0" ("0\n0") y la
            # comparación aritmética fallaba con "error de sintaxis".
            loaded_vars=$(sudo cat /proc/$ollama_pid/environ 2>/dev/null | tr '\0' '\n' | grep -cE "^(OMP|MKL|OLLAMA_MAX|OLLAMA_FLASH|OLLAMA_NUM_PARALLEL)" || true)
            loaded_vars=${loaded_vars:-0}
            if [[ "$loaded_vars" -ge 4 ]]; then
                echo -e "${GREEN}  └─${NC} Variables aplicadas al proceso Ollama (PID $ollama_pid): ${GREEN}$loaded_vars variables activas${NC}"
            else
                echo -e "${YELLOW}  └─${NC} Variables en override pero NO aplicadas — ejecuta: ${CYAN}sudo systemctl restart ollama${NC}"
            fi
        else
            echo -e "${YELLOW}  └─${NC} Proceso Ollama no detectado"
        fi
    else
        echo -e "${YELLOW}○${NC} Override systemd Ollama: no configurado"
    fi

    if [[ -f "/etc/sysctl.d/99-axioma.conf" ]]; then
        echo -e "${GREEN}✓${NC} Sysctl: ${CYAN}/etc/sysctl.d/99-axioma.conf${NC}"
    else
        echo -e "${YELLOW}○${NC} Sysctl: no configurado"
    fi

    # ✅ v1.3: Detección compatible con CachyOS 7.0+
    local sched
    sched=$(get_active_scheduler)
    if [[ "$sched" == "rusty" ]]; then
        echo -e "${GREEN}✓${NC} Scheduler: ${CYAN}$sched${NC} (óptimo)"
    elif [[ "$sched" == "none" ]]; then
        echo -e "${YELLOW}○${NC} Scheduler: none (CFS default)"
    else
        echo -e "${YELLOW}○${NC} Scheduler: $sched"
    fi

    if [[ -f "/etc/ananicy.d/axioma.rules" ]]; then
        echo -e "${GREEN}✓${NC} Ananicy rules: ${CYAN}/etc/ananicy.d/axioma.rules${NC}"
    else
        echo -e "${YELLOW}○${NC} Ananicy rules: no configuradas"
    fi

    if swapon --show 2>/dev/null | grep -q zram; then
        echo -e "${GREEN}✓${NC} ZRAM activo: ${CYAN}$(swapon --show | grep zram | awk '{print $3}')${NC}"
    else
        echo -e "${YELLOW}○${NC} ZRAM: no activo"
    fi

    if [[ -f "$AXIOMA_ROOT/config/optimizer.env" ]]; then
        echo -e "${GREEN}✓${NC} Config runtime: ${CYAN}config/optimizer.env${NC}"
    else
        echo -e "${YELLOW}○${NC} Config runtime: no creada"
    fi

    # Mostrar venv detectado
    local venv_python
    if venv_python=$(get_venv_python 2>/dev/null); then
        local venv_path
        venv_path=$(get_venv_path 2>/dev/null || echo "$VIRTUAL_ENV")
        echo -e "${GREEN}✓${NC} Venv detectado: ${CYAN}$venv_path${NC}"
    else
        echo -e "${YELLOW}○${NC} Venv: no detectado (activa con source venv/bin/activate)"
    fi

    if [[ -f "$MANIFEST" ]]; then
        local count
        count=$(python3 -c "import json; print(len(json.load(open('$MANIFEST'))['backups']))")
        echo -e "${GREEN}✓${NC} Manifiesto: ${CYAN}$count backups registrados${NC}"

        local pkg_count
        pkg_count=$(python3 -c "import json; d=json.load(open('$MANIFEST')); print(len(d.get('installed_packages', [])))")
        if [[ "$pkg_count" -gt 0 ]]; then
            echo -e "${GREEN}✓${NC} Paquetes instalados por optimizer: ${CYAN}$pkg_count${NC}"
        fi
    else
        echo -e "${YELLOW}○${NC} Manifiesto: sin optimizaciones aplicadas"
    fi
    echo
}

# ============================================================================
# APPLY
# ============================================================================
apply_all() {
    echo -e "\n${BOLD}🚀 Aplicando optimizaciones...${NC}\n"

    if ! check_and_install_deps; then
        err "No se pudieron satisfacer las dependencias. Abortando."
        return 1
    fi

    echo

    manifest_init
    python3 -c "import json; d=json.load(open('$MANIFEST')); d['created']='$(date -Iseconds)'; json.dump(d, open('$MANIFEST','w'), indent=2)"

    mkdir -p "$BACKUP_DIR"
    info "Backups en: $BACKUP_DIR"

    optimize_env_vars
    optimize_ollama_systemd   # ✅ v1.3: NUEVO - inyecta variables en Ollama
    optimize_sysctl
    optimize_scheduler
    optimize_ananicy
    optimize_nicegui
    optimize_zram

    echo -e "\n${GREEN}${BOLD}✅ Optimizaciones aplicadas correctamente${NC}"
    echo -e "\n${YELLOW}⚠  Pasos finales:${NC}"
    echo "   1. Reinicia sesión para aplicar variables de usuario"
    echo "   2. Ollama ya fue reiniciado automáticamente con las variables optimizadas"
    echo "   3. Verifica con: ${CYAN}sudo cat /proc/\$(pgrep ollama)/environ | tr '\\0' '\\n' | grep OLLAMA${NC}"
    echo "   4. Ejecuta AXIOMA: ${CYAN}cd $AXIOMA_ROOT && python main.py${NC}"
    echo -e "\n${BLUE}ℹ  Para revertir: ${CYAN}$0 --rollback${NC}"
    echo -e "${BLUE}ℹ  Ver estado: ${CYAN}$0 --status${NC}\n"
}

# ============================================================================
# MENÚ INTERACTIVO
# ============================================================================
interactive_menu() {
    clear
    echo -e "${CYAN}${BOLD}"
    echo "  ╔═══════════════════════════════════════════════════════════╗"
    echo "  ║         AXIOMA OPTIMIZER PRO v1.3 — CachyOS Edition      ║"
    echo "  ║   Optimización reversible para RX 460 + Ollama + IA      ║"
    echo "  ╚═══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    detect_environment
    echo -e "\n${BOLD}Selecciona una opción:${NC}\n"
    echo -e "  ${GREEN}1)${NC} 🚀 ${BOLD}Optimizar sistema${NC}      (verifica deps + instala + aplica)"
    echo -e "  ${YELLOW}2)${NC} ↩️  ${BOLD}Restaurar (rollback)${NC}   (revierte todos los cambios)"
    echo -e "  ${BLUE}3)${NC} 📊 ${BOLD}Ver estado actual${NC}      (muestra qué está aplicado)"
    echo -e "  ${CYAN}4)${NC} 🔍 ${BOLD}Verificar dependencias${NC} (solo verifica, no instala)"
    echo -e "  ${MAGENTA}5)${NC} 📦 ${BOLD}Instalar dependencias${NC}  (verifica e instala faltantes)"
    echo -e "  ${RED}0)${NC} ❌ Salir"
    echo
    read -rp "$(echo -e ${BOLD}Opción [0-5]:${NC} )" choice

    case "$choice" in
        1) APPLY=true; DRY_RUN=false; INSTALL_DEPS=true ;;
        2) ROLLBACK=true ;;
        3) STATUS=true ;;
        4) CHECK_DEPS=true; DRY_RUN=true; INSTALL_DEPS=false ;;
        5) CHECK_DEPS=true; DRY_RUN=false; INSTALL_DEPS=true ;;
        0) exit 0 ;;
        *) err "Opción inválida"; sleep 2; interactive_menu; return ;;
    esac
}

# ============================================================================
# MAIN
# ============================================================================
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --apply) APPLY=true; DRY_RUN=false; INSTALL_DEPS=true; shift ;;
            --rollback) ROLLBACK=true; shift ;;
            --status) STATUS=true; shift ;;
            --check-deps) CHECK_DEPS=true; DRY_RUN=true; INSTALL_DEPS=false; shift ;;
            --install-deps) CHECK_DEPS=true; DRY_RUN=false; INSTALL_DEPS=true; shift ;;
            --dry-run) DRY_RUN=true; APPLY=false; shift ;;
            -h|--help)
                echo "Uso: $0 [--apply|--rollback|--status|--check-deps|--install-deps|--dry-run]"
                echo
                echo "  Sin args:        Menú interactivo"
                echo "  --apply:         Verifica deps + instala + aplica optimizaciones"
                echo "  --rollback:      Revierte todos los cambios desde backup"
                echo "  --status:        Muestra qué está aplicado"
                echo "  --check-deps:    Solo verifica dependencias (no instala)"
                echo "  --install-deps:  Verifica e instala dependencias faltantes"
                echo "  --dry-run:       Simula sin aplicar cambios"
                exit 0 ;;
            *) err "Opción desconocida: $1"; exit 1 ;;
        esac
    done

    if [[ "$APPLY" == false && "$ROLLBACK" == false && "$STATUS" == false && "$CHECK_DEPS" == false ]]; then
        interactive_menu
    fi

    mkdir -p "$(dirname "$LOG_FILE")"

    if [[ "$STATUS" == true ]]; then
        show_status
        exit 0
    fi

    if [[ "$ROLLBACK" == true ]]; then
        do_rollback
        exit 0
    fi

    if [[ "$CHECK_DEPS" == true ]]; then
        check_and_install_deps
        exit $?
    fi

    if [[ "$APPLY" == true ]]; then
        apply_all
        exit 0
    fi

    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: mostraría optimizaciones sin aplicar"
        show_status
        exit 0
    fi
}

# ✅ 2026-08-29: conservar solo el log de ESTA corrida del optimizer
# (cada corrida genera logs/optimizer_<ts>.log). Se poda al inicio excluyendo
# $LOG_FILE porque main() puede hacer exit antes del final del script.
ls -1t "$AXIOMA_ROOT"/logs/optimizer_*.log 2>/dev/null | while read -r f; do
    [ "$f" != "$LOG_FILE" ] && rm -f "$f"
done

main "$@"
