# ═══════════════════════════════════════════════════════════════
# AXIOMA — INICIAR EN WINDOWS (pensado para DOBLE CLIC: sin terminal)
# ═══════════════════════════════════════════════════════════════
# Hace lo mismo que instalar/iniciar_axioma.sh, pero en Windows:
#   1. Comprueba que Docker Desktop esté instalado y respondiendo (y si no, lo explica).
#   2. Decide solo si usar el Ollama del equipo o levantar el de AXIOMA:
#        · si el equipo YA tiene Ollama andando  -> usa ese (no levanta otro: no gasta disco ni choca)
#        · si NO tiene                           -> levanta el Ollama que viene con AXIOMA
#   3. Enciende AXIOMA, espera a que responda y ABRE EL NAVEGADOR.
#
# DIFERENCIA CON LINUX (importante): en Windows la red del equipo (`network_mode: host`) no está
# disponible, así que se usa el servicio `axioma-puente` (perfil «puente»), que publica el puerto
# 8080 y llega al Ollama del equipo por `host.docker.internal`. Ver docker-compose.yml.
# Consecuencia: en Windows el puerto es 8080 y no se puede cambiar con AXIOMA_PUERTO.
#
# Uso:
#   .\instalar\iniciar_axioma.ps1            # enciende y abre el navegador
#   .\instalar\iniciar_axioma.ps1 -Detener   # apaga todo (los datos y las claves quedan guardados)
#   .\instalar\iniciar_axioma.ps1 -Ensayo    # muestra qué haría, sin tocar nada
#
# Si Windows dice "la ejecución de scripts está deshabilitada", usá esta forma:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\instalar\iniciar_axioma.ps1
#
# Este guion NO se probó todavía en un Windows real (ver MANUAL_USUARIO.md, sección Windows).
# Un interruptor que no exista lo rechaza PowerShell mismo (no hay código de salida propio para eso).
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo)

[CmdletBinding()]
param(
    [switch]$Detener,
    [switch]$Ensayo
)

$ErrorActionPreference = "Stop"

# Marcadores sin emoji/dibujos: la consola de Windows (CP850/CP437) los imprime mal.
function paso  { param([string]$Texto) Write-Host "`n> $Texto" -ForegroundColor Cyan }
function ok    { param([string]$Texto) Write-Host "  [OK] $Texto" -ForegroundColor Green }
function aviso { param([string]$Texto) Write-Host "  [!]  $Texto" -ForegroundColor Yellow }
function mal   { param([string]$Texto) Write-Host "  [X]  $Texto" -ForegroundColor Red }

$Raiz = Split-Path -Parent $PSScriptRoot
$Puerto = 8080                     # el mapeo del servicio `axioma-puente` (Windows no tiene red de equipo)
$Url = "http://127.0.0.1:$Puerto/"

function Probar-Docker {
    # Devuelve $true si Docker está instalado Y respondiendo.
    # El try/catch no es adorno: en PowerShell 7.4+ `$PSNativeCommandUseErrorActionPreference` viene
    # en $true, así que un comando que sale con error LANZA excepción; con `$ErrorActionPreference =
    # "Stop"` el guion se cortaría en vez de explicar qué falta.
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    try { & docker info *> $null } catch { return $false }
    return ($LASTEXITCODE -eq 0)
}

function Explicar-FaltaDocker {
    mal "Docker Desktop no está disponible (es el programa que ejecuta AXIOMA)."
    Write-Host "     1. Instalalo desde https://www.docker.com/products/docker-desktop"
    Write-Host "     2. Abrilo una vez y esperá a que diga «Engine running»."
    Write-Host "     3. Volvé a hacer doble clic acá."
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if ($wsl) {
        Write-Host "     (Si Docker Desktop se queja de WSL 2: en una consola de administrador, 'wsl --update'.)"
    } else {
        Write-Host "     (Docker Desktop en Windows necesita WSL 2: 'wsl --install' y reiniciar.)"
    }
}

function Hay-OllamaEnElEquipo {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

# ── Apagar ──────────────────────────────────────────────────────────────────
if ($Detener) {
    paso "Apagando AXIOMA"
    $comando = "docker compose --profile puente --profile ollama-local down"
    if ($Ensayo) { Write-Host "  (ensayo) $comando"; exit 0 }
    if (-not (Probar-Docker)) { Explicar-FaltaDocker; exit 4 }
    Push-Location $Raiz
    try {
        try { & docker compose --profile puente --profile ollama-local down }
        catch { aviso "Docker devolvió un error al apagar: revisá que Docker Desktop esté abierto." }
    } finally { Pop-Location }
    ok "AXIOMA apagado. Tus datos y tus claves quedan guardados en la carpeta del proyecto."
    exit 0
}

# ── 1. ¿Está Docker? ────────────────────────────────────────────────────────
paso "Comprobando Docker"
if (-not (Probar-Docker)) { Explicar-FaltaDocker; exit 4 }
ok "Docker responde ($(& docker --version))"

if ($env:AXIOMA_PUERTO -and $env:AXIOMA_PUERTO -ne "$Puerto") {
    aviso "Tenías AXIOMA_PUERTO=$($env:AXIOMA_PUERTO), pero en Windows se usa el $Puerto (el que publica el servicio puente)."
}

# ── 2. Carpetas y .env (tienen que existir ANTES de encender) ──────────────
# 2026-10-10: MEDIDO — las carpetas que el contenedor escribe tienen que existir y ser TUYAS: si no,
# Docker las crea como `root` y el contenedor (que corre con tu usuario) no puede escribir, así que
# AXIOMA no arranca. Y el `.env` tiene que existir: el compose lo monta como archivo y, si no está,
# Docker crea una CARPETA con ese nombre y el montaje falla (probado con `docker run -v`).
paso "Preparando las carpetas y tu archivo de claves"
foreach ($carpeta in @("data", "logs", "cache")) {
    $ruta = Join-Path $Raiz $carpeta
    if ($Ensayo) { Write-Host "  (ensayo) crearía la carpeta $ruta"; continue }
    if (-not (Test-Path -LiteralPath $ruta)) { New-Item -ItemType Directory -Path $ruta | Out-Null }
}
$archivoEnv = Join-Path $Raiz ".env"
$plantillaEnv = Join-Path $Raiz ".env.example"
if ($Ensayo) {
    Write-Host "  (ensayo) si no existe, crearía $archivoEnv a partir de la plantilla"
} elseif ((Test-Path -LiteralPath $plantillaEnv) -and -not (Test-Path -LiteralPath $archivoEnv)) {
    Copy-Item -LiteralPath $plantillaEnv -Destination $archivoEnv
    ok "Creé tu .env a partir de la plantilla (ahí se guardan tus claves, y no se publican)"
}

# ── 3. ¿Usamos el Ollama del equipo o el que trae AXIOMA? ───────────────────
paso "Buscando Ollama"
# Los servicios se nombran UNO POR UNO a propósito. Medido el 2026-10-06 en Linux: `docker compose
# --profile puente up -d` levanta ADEMÁS el servicio `axioma` (no tiene perfil, arranca siempre), y
# como ése usa la red del equipo, los dos pelean por el puerto 8080: `axioma` queda en bucle de
# reinicios y en Windows directamente no puede usar esa red. Por eso: `axioma-puente` y, si hace
# falta, `ollama`.
if (Hay-OllamaEnElEquipo) {
    $perfilOllama = @()
    $servicios = @("axioma-puente")
    ok "Ya tenés Ollama andando en 127.0.0.1:11434: uso ese (no hace falta otro)."
} else {
    $perfilOllama = @("--profile", "ollama-local")
    $servicios = @("axioma-puente", "ollama")
    aviso "No encontré Ollama en 127.0.0.1:11434."
    ok "Levanto el Ollama que viene con AXIOMA (los modelos quedan guardados en un volumen)."
}

# ── 4. Encender, esperar y abrir el navegador ───────────────────────────────
paso "Encendiendo AXIOMA"
if ($Ensayo) {
    Write-Host "  (ensayo) cd $Raiz; docker compose --profile puente $($perfilOllama -join ' ') up -d $($servicios -join ' ')"
    Write-Host "  (ensayo) abriría $Url cuando responda"
    exit 0
}
Push-Location $Raiz
try {
    $encendido = $true
    try { & docker compose --profile puente @perfilOllama up -d @servicios } catch { $encendido = $false }
    if ($encendido) { $encendido = ($LASTEXITCODE -eq 0) }
    if (-not $encendido) {
        mal "Docker no pudo encender AXIOMA (mirá los mensajes de arriba)."
        exit 4
    }
} finally { Pop-Location }
ok "Contenedores encendidos"

paso "Esperando a que AXIOMA responda"
for ($intento = 1; $intento -le 30; $intento++) {
    $listo = $false
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
        $listo = ($r.StatusCode -eq 200)
    } catch {
        $listo = $false
    }
    if ($listo) {
        ok "AXIOMA está en línea: $Url"
        try { Start-Process $Url; ok "Abrí el navegador en $Url" }
        catch { aviso "No pude abrir el navegador: entrá a mano a $Url" }
        paso "Listo"
        Write-Host "  Si en la pantalla dice que falta algo (modelos, claves), AXIOMA mismo lo explica y lo instala."
        Write-Host "  Para apagarlo: .\instalar\iniciar_axioma.ps1 -Detener"
        exit 0
    }
    Start-Sleep -Seconds 2
}
mal "AXIOMA no respondió en un minuto."
Write-Host "     Mirá qué dice con:  docker compose logs --tail 40 axioma-puente"
exit 4
