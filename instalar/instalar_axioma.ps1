# ═══════════════════════════════════════════════════════════════
# AXIOMA — INSTALAR EN WINDOWS (una sola vez, pensado para DOBLE CLIC)
# ═══════════════════════════════════════════════════════════════
# Hace lo mismo que instalar/instalar_axioma.sh, pero en Windows:
#   1. Comprueba Docker Desktop (y lo explica en castellano si falta, con el paso de WSL 2).
#   2. Arma la imagen de AXIOMA (la primera vez tarda unos minutos).
#   3. Deja un ACCESO DIRECTO «AXIOMA» en el menú Inicio y en el Escritorio.
#   4. Enciende AXIOMA y abre el navegador.
#
# Uso (PowerShell, dentro de la carpeta del proyecto):
#   .\instalar\instalar_axioma.ps1              # instalar y arrancar
#   .\instalar\instalar_axioma.ps1 -SinAcceso   # sin crear los accesos directos
#   .\instalar\instalar_axioma.ps1 -Ensayo      # muestra qué haría, sin tocar nada
#
# Si Windows dice "la ejecución de scripts está deshabilitada", usá esta forma:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\instalar\instalar_axioma.ps1
#
# Este guion NO se probó todavía en un Windows real (ver MANUAL_USUARIO.md, sección Windows).
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo)

[CmdletBinding()]
param(
    [switch]$SinAcceso,
    [switch]$Ensayo
)

$NOMBRE_REGISTRO = "instalacion_windows"
$ErrorActionPreference = "Stop"

# Marcadores sin emoji/dibujos: la consola de Windows (CP850/CP437) los imprime mal.
function paso  { param([string]$Texto) Write-Host "`n> $Texto" -ForegroundColor Cyan; Registrar "> $Texto" }
function ok    { param([string]$Texto) Write-Host "  [OK] $Texto" -ForegroundColor Green; Registrar "[OK] $Texto" }
function aviso { param([string]$Texto) Write-Host "  [!]  $Texto" -ForegroundColor Yellow; Registrar "[!] $Texto" }
Registrar "AXIOMA — registro de la corrida ($(Get-Date))"
function mal   { param([string]$Texto) Write-Host "  [X]  $Texto" -ForegroundColor Red; Registrar "[X] $Texto" }

# ── Registro en archivo (sin Start-Transcript) ──────────────────────────────────────────────────
# 2026-10-09: se probó con `Start-Transcript` y el CI se puso rojo en PowerShell 5.1 sin poder ver el
# detalle. BUSCADO: `Start-Transcript` falla en hosts que NO soportan transcripción (por ejemplo cuando
# la salida está redirigida/capturada, que es como el CI ejecuta estos guiones).
#   · https://stackoverflow.com/questions/5032075  ("This host does not support transcription")
#   · https://github.com/PowerShell/PowerShell/discussions/25174  (alternativas robustas: escribir uno mismo)
# Así que el registro lo escribe el propio guion: todos los mensajes pasan por paso/ok/aviso/mal, y esas
# funciones además AGREGAN la línea al archivo. No depende del host ni de la consola.
$CARPETA_REGISTRO = Join-Path (Split-Path -Parent $PSScriptRoot) "logs"
try { New-Item -ItemType Directory -Force -Path $CARPETA_REGISTRO | Out-Null } catch { }
$ARCHIVO_REGISTRO = Join-Path $CARPETA_REGISTRO ("{0}_{1}.log" -f $NOMBRE_REGISTRO,
                                                 (Get-Date -Format "yyyyMMdd_HHmmss"))
function Registrar {
    param([string]$Texto)
    try { Add-Content -Path $ARCHIVO_REGISTRO -Value $Texto -Encoding UTF8 } catch { }
}

$Raiz = Split-Path -Parent $PSScriptRoot

paso "AXIOMA — instalación en Windows (una sola vez)"
Write-Host "   Carpeta del proyecto: $Raiz"

# ── 1. Docker ───────────────────────────────────────────────────────────────
# (El try/catch es por PowerShell 7.4+: ahí un comando que falla LANZA excepción, y con
#  `$ErrorActionPreference = "Stop"` el guion se cortaría en vez de explicar qué falta.)
$hayDocker = (Get-Command docker -ErrorAction SilentlyContinue) -ne $null
if ($hayDocker) {
    try { & docker info *> $null } catch { $hayDocker = $false }
    if ($hayDocker) { $hayDocker = ($LASTEXITCODE -eq 0) }
}
if (-not $hayDocker) {
    mal "Docker Desktop no está disponible."
    Write-Host "     Instalalo desde https://www.docker.com/products/docker-desktop"
    Write-Host "     En Windows necesita WSL 2: si te lo pide, corré 'wsl --install' en una consola de administrador y reiniciá."
    Write-Host "     Después volvé a ejecutar este guion."
    exit 4
}
ok "Docker responde ($(& docker --version))"

# ── 2. Imagen ───────────────────────────────────────────────────────────────
paso "Armando AXIOMA (la primera vez tarda unos minutos)"
if ($Ensayo) {
    Write-Host "  (ensayo) cd $Raiz; docker compose build"
} else {
    Push-Location $Raiz
    try {
        $armado = $true
        try { & docker compose build } catch { $armado = $false }
        if ($armado) { $armado = ($LASTEXITCODE -eq 0) }
        if (-not $armado) { mal "No se pudo armar la imagen (mirá los mensajes de arriba)."; exit 4 }
    } finally { Pop-Location }
    ok "AXIOMA armado"
}

# ── 3. Accesos directos (menú Inicio y Escritorio) ──────────────────────────
if (-not $SinAcceso) {
    paso "Creando los accesos directos"
    # El acceso directo NO deja una ventana abierta de más: arranca AXIOMA y abre el navegador.
    # Se usa powershell.exe con -ExecutionPolicy Bypass para que funcione aunque Windows tenga
    # la ejecución de guiones deshabilitada (es lo normal recién instalado).
    $destinos = @(
        [Environment]::GetFolderPath("Programs"),
        [Environment]::GetFolderPath("Desktop")
    )
    foreach ($destino in $destinos) {
        if (-not $destino) { continue }
        $archivo = Join-Path $destino "AXIOMA.lnk"
        if ($Ensayo) { Write-Host "  (ensayo) escribiría $archivo"; continue }
        try {
            $shell = New-Object -ComObject WScript.Shell
            $acceso = $shell.CreateShortcut($archivo)
            $acceso.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
            $acceso.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Raiz\instalar\iniciar_axioma.ps1`""
            $acceso.WorkingDirectory = $Raiz
            $acceso.Description = "AXIOMA: asistente de IA local (arranca y abre la pantalla de configuración)"
            $acceso.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
            $acceso.Save()
            ok "Acceso creado: $archivo"
        } catch {
            aviso "No pude crear el acceso en $destino ($($_.Exception.Message))."
            Write-Host "        Podés arrancar igual con: powershell -NoProfile -ExecutionPolicy Bypass -File `"$Raiz\instalar\iniciar_axioma.ps1`""
        }
    }
}

# ── 4. Arrancar ─────────────────────────────────────────────────────────────
paso "Arrancando AXIOMA"
if ($Ensayo) {
    Write-Host "  (ensayo) powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\iniciar_axioma.ps1`""
    exit 0
}
aviso "En Windows la voz todavía no está disponible (el micrófono del equipo no llega al contenedor): el chat y los modelos funcionan igual."
& "$PSScriptRoot\iniciar_axioma.ps1"
exit $LASTEXITCODE
