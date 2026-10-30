# AXIOMA - DESINSTALAR (para empezar de cero sin dejar basura)
#
# Que hace, en orden:
#   1. Apaga y borra los contenedores de AXIOMA (sin tocar otros contenedores).
#   2. Borra las imagenes locales de AXIOMA (axioma:local y la de ghcr).
#   3. Borra los accesos directos del Escritorio y del menu Inicio.
#   4. Deja TUS DATOS por defecto (data, logs, .env) en una carpeta de respaldo y borra la
#      carpeta del proyecto. Con -Todo borra tambien esos datos y los volumenes (modelos).
#
# Uso:
#   .\instalar\desinstalar_axioma.ps1              # pide confirmacion
#   .\instalar\desinstalar_axioma.ps1 -Si         # sin preguntar
#   .\instalar\desinstalar_axioma.ps1 -Todo       # ademas borra datos y modelos
#   .\instalar\desinstalar_axioma.ps1 -Ensayo     # muestra que haria, sin tocar nada
[CmdletBinding()]
param(
    [switch]$Todo,
    [switch]$Si,
    [switch]$Ensayo
)

$ErrorActionPreference = "Continue"
$Raiz = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Raiz "docker-compose.yml"))) { $Raiz = $PSScriptRoot }
$Raiz = (Resolve-Path $Raiz).Path

function paso { param([string]$Texto) Write-Host "`n> $Texto" -ForegroundColor Cyan }
function ok   { param([string]$Texto) Write-Host "  [OK] $Texto" -ForegroundColor Green }
function aviso{ param([string]$Texto) Write-Host "  [!]  $Texto" -ForegroundColor Yellow }
function mal  { param([string]$Texto) Write-Host "  [X]  $Texto" -ForegroundColor Red }

paso "AXIOMA - desinstalacion"
Write-Host "   carpeta: $Raiz"
if ($Todo) {
    Write-Host "   modo: TODO (contenedores, imagenes, accesos, carpeta, DATOS y MODELOS)"
} else {
    Write-Host "   modo: normal (contenedores, imagenes, accesos y carpeta; TUS DATOS se conservan)"
}

if ($Ensayo) {
    Write-Host "  (ensayo) docker compose ... down --remove-orphans$(if ($Todo) { ' -v' } else { '' })"
    Write-Host "  (ensayo) docker rmi axioma:local y ghcr.io/...:vX"
    Write-Host "  (ensayo) borraria los accesos directos"
    Write-Host "  (ensayo) borraria $Raiz$(if ($Todo) { ' (incluye data, logs y .env)' } else { '' })"
    exit 0
}

if (-not $Si) {
    Write-Host ""
    $respuesta = Read-Host "   Seguir? [s/N]"
    if ($respuesta -notmatch '^(s|si|sí|y)$') {
        Write-Host "   Cancelado."
        exit 3
    }
}

$hayDocker = $false
try { docker info *> $null; $hayDocker = ($LASTEXITCODE -eq 0) } catch { $hayDocker = $false }
if ($hayDocker) {
    paso "Apagando y borrando los contenedores de AXIOMA"
    $compose = Join-Path $Raiz "docker-compose.yml"
    if (Test-Path $compose) {
        if ($Todo) {
            & docker compose -f $compose down --remove-orphans -v
        } else {
            & docker compose -f $compose down --remove-orphans
        }
        if ($LASTEXITCODE -eq 0) { ok "contenedores de AXIOMA borrados$(if ($Todo) { ' (con volumenes)' })" }
        else { aviso "no se pudieron borrar del todo (mira los mensajes de arriba)" }
    } else {
        aviso "no encontre docker-compose.yml en $Raiz"
    }
    paso "Borrando las imagenes de AXIOMA"
    & docker image inspect axioma:local *> $null
    if ($LASTEXITCODE -eq 0) {
        & docker rmi axioma:local *> $null
        if ($LASTEXITCODE -eq 0) { ok "imagen axioma:local borrada" } else { aviso "no pude borrar axioma:local" }
    }
    $ghcr = & docker images --format "{{.Repository}}:{{.Tag}}" | Where-Object { $_ -match '^ghcr\.io/.+/:v[0-9]' }
    foreach ($imagen in $ghcr) {
        & docker rmi $imagen *> $null
        if ($LASTEXITCODE -eq 0) { ok "imagen $imagen borrada" } else { aviso "no pude borrar $imagen" }
    }
} else {
    aviso "Docker no responde: se saltean contenedores e imagenes"
}

paso "Borrando los accesos directos"
$accesos = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "AXIOMA.lnk"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\AXIOMA.lnk")
)
foreach ($acceso in $accesos) {
    if (Test-Path $acceso) {
        Remove-Item $acceso -Force -ErrorAction SilentlyContinue
        ok "borrado $acceso"
    }
}

paso "Borrando la carpeta de AXIOMA"
if ($Todo) {
    Remove-Item -Recurse -Force $Raiz -ErrorAction SilentlyContinue
    ok "borrado $Raiz (incluye data, logs y .env)"
} else {
    $respaldo = Join-Path ([Environment]::GetFolderPath("UserProfile")) ("axioma-datos-" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    New-Item -ItemType Directory -Path $respaldo -Force | Out-Null
    foreach ($cosa in @("data", ".env", "logs")) {
        $origen = Join-Path $Raiz $cosa
        if (Test-Path $origen) {
            Move-Item $origen (Join-Path $respaldo $cosa) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -Recurse -Force $Raiz -ErrorAction SilentlyContinue
    ok "carpeta borrada; tus datos quedaron en $respaldo"
}

paso "Listo"
Write-Host "   Para volver a instalar: baja AXIOMA-Setup.exe del Release y ejecutalo."
