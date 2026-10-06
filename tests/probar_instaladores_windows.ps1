# ═══════════════════════════════════════════════════════════════
# AXIOMA — comprobador de los instaladores de Windows
# ═══════════════════════════════════════════════════════════════
# Corre en un Windows DE VERDAD (job `instaladores-windows` del CI, sobre `windows-latest`) y con los
# dos PowerShell que puede tener un usuario: el 5.1 que trae Windows y el 7 si lo instaló.
#
# Por qué existe: en el equipo del autor no hay Windows (`command -v pwsh` no devuelve nada), así que
# los guiones de Windows sólo tenían pruebas de ESTRUCTURA (leer el archivo y comparar texto). Acá se
# ejecutan: si el guion no arranca, o si los acentos salen mal, o si el camino de "falta Docker" no
# explica nada, el CI se pone rojo.
#
# QUÉ **NO** CUBRE (eso necesita una PC con Windows y Docker Desktop, a mano):
#   · bajar y armar la imagen de verdad, y crear los accesos directos del menú Inicio y del Escritorio
#   · llegar al Ollama del equipo por `host.docker.internal` y abrir el navegador
#   · que el usuario vea la pantalla de configuración la primera vez
#
# Uso local (en un Windows, desde la raíz del proyecto):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\probar_instaladores_windows.ps1
#
# Códigos de salida: 0 = todo bien · 1 = alguna comprobación falló

param(
    [switch]$VerDetalle
)

# `Continue` a propósito: un comprobador tiene que SEGUIR y contar todos los problemas, no morir en el
# primero. (Medido el 2026-10-06: con `Stop`, cualquier sorpresa cortaba el guion sin decir dónde.)
$ErrorActionPreference = "Continue"

# Sin emoji y sin dibujos: la consola de Windows (CP850/CP437) los imprime mal.
function Bien { param([string]$Texto) Write-Host "  [OK] $Texto" -ForegroundColor Green }
function Fallo { param([string]$Texto) Write-Host "  [X]  $Texto" -ForegroundColor Red }
function Dato { param([string]$Texto) Write-Host "       $Texto" -ForegroundColor DarkGray }

$Raiz = Split-Path -Parent $PSScriptRoot          # tests\ -> raíz del proyecto
$Guiones = @("instalar\instalar_axioma.ps1", "instalar\iniciar_axioma.ps1")
$Problemas = 0

Write-Host ""
Write-Host "AXIOMA - comprobando los instaladores de Windows ($($PSVersionTable.PSVersion))" -ForegroundColor Cyan
Write-Host "Proyecto: $Raiz"

# ── 1. Los archivos: BOM, sintaxis y acentos ─────────────────────────────────────────────────────
Write-Host "`n> Los archivos"
foreach ($relativo in $Guiones) {
    $ruta = Join-Path $Raiz $relativo
    if (-not (Test-Path $ruta)) {
        Fallo "falta $relativo"
        $Problemas++
        continue
    }
    # BOM: sin BOM, PowerShell 5.1 lo lee como ANSI y los acentos salen mal.
    $bytes = [System.IO.File]::ReadAllBytes($ruta)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        Bien "BOM en $relativo (PowerShell 5.1 lo necesita para los acentos)"
    } else {
        Fallo "SIN BOM: $relativo (los acentos van a salir mal en PowerShell 5.1)"
        $Problemas++
    }
    # Sintaxis: el analizador de PowerShell, sin ejecutar nada.
    $errores = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($ruta, [ref]$null, [ref]$errores)
    if ($null -eq $errores -or @($errores).Count -eq 0) {
        Bien "sintaxis de $relativo"
    } else {
        # OJO: `"$relativo:"` NO es válido — PowerShell lee los dos puntos como si fuera una unidad
        # (`$relativo:`) y el guion NI SIQUIERA ARRANCA: fue el primer error real que marcó el CI
        # de Windows (2026-10-06). Con `${relativo}` queda claro dónde termina el nombre.
        Fallo "sintaxis de ${relativo}: $(@($errores).Count) error(es)"
        Dato (@($errores) | Out-String)
        $Problemas++
    }
    # Acentos legibles de verdad (prueba de que el BOM sirve para algo).
    $texto = Get-Content -Raw -Encoding UTF8 $ruta
    if ($texto -match "instalación" -or $texto -match "Apagando" -or $texto -match "Comprobando") {
        Bien "acentos legibles en $relativo"
    } else {
        Fallo "los acentos de $relativo no se leen bien (¿BOM?)"
        $Problemas++
    }
}

# ── 2. ¿Hay Docker en esta máquina? (el runner puede tenerlo o no) ───────────────────────────────
Write-Host "`n> Docker en esta máquina"
$hayDocker = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try { & docker info *> $null } catch { }
    $hayDocker = ($LASTEXITCODE -eq 0)
}
if ($hayDocker) {
    Bien "Docker responde: se prueban los caminos que lo necesitan"
} else {
    Dato "Docker no responde acá: se prueba el camino de 'falta Docker' (que es el que ve quien"
    Dato "recién empieza, y el que tiene que explicar en castellano qué hacer)"
}

# ── 3. Los guiones, ejecutados ───────────────────────────────────────────────────────────────────
function CorrerGuion {
    param([string]$Relativo, [string[]]$Argumentos)
    $ruta = Join-Path $Raiz $Relativo
    # Igual que el acceso directo: powershell.exe con -ExecutionPolicy Bypass.
    # El try/catch no es adorno: MEDIDO en el CI de Windows (2026-10-06), cuando el guion rechaza un
    # interruptor que no existe, PowerShell escribe en stderr y al juntarlo con `2>&1` eso se vuelve un
    # `NativeCommandError` — un error que, con `$ErrorActionPreference = "Stop"`, MATABA al comprobador
    # sin llegar a decir qué pasó. Acá se atrapa y se sigue: lo que importa es el código de salida.
    $salida = ""
    try {
        $salida = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ruta @Argumentos 2>&1 | Out-String
    } catch {
        $salida = "$salida`n$($_.Exception.Message)"
    }
    $codigo = $LASTEXITCODE
    if ($null -eq $codigo) { $codigo = -1 }
    return @{ Codigo = $codigo; Salida = $salida }
}

Write-Host "`n> Los guiones, en modo ensayo (no toca nada)"
$ensayoInstalar = CorrerGuion "instalar\instalar_axioma.ps1" @("-Ensayo")
$ensayoIniciar = CorrerGuion "instalar\iniciar_axioma.ps1" @("-Ensayo")

foreach ($caso in @(
        @{ Nombre = "instalar_axioma.ps1 -Ensayo"; R = $ensayoInstalar; Esperado = "docker compose build" },
        @{ Nombre = "iniciar_axioma.ps1 -Ensayo"; R = $ensayoIniciar; Esperado = "axioma-puente" })) {
    $r = $caso.R
    if ($hayDocker) {
        if ($r.Codigo -eq 0 -and $r.Salida -match [regex]::Escape($caso.Esperado)) {
            Bien "$($caso.Nombre): sale 0 y muestra el plan ('$($caso.Esperado)')"
        } else {
            Fallo "$($caso.Nombre): se esperaba salida 0 con '$($caso.Esperado)' y salió $($r.Codigo)"
            Dato $r.Salida
            $Problemas++
        }
        if ($r.Salida -match "ensayo") {
            Bien "$($caso.Nombre): avisa que es un ensayo (no toca nada)"
        } else {
            Fallo "$($caso.Nombre): no avisa que es un ensayo"
            $Problemas++
        }
    } else {
        # Sin Docker: el guion TIENE que explicar qué falta y salir con 4 (nunca quedarse mudo ni
        # seguir adelante como si nada).
        if ($r.Codigo -eq 4 -and $r.Salida -match "Docker Desktop no está disponible") {
            Bien "$($caso.Nombre): sin Docker explica qué falta y sale con 4"
        } else {
            Fallo "$($caso.Nombre): sin Docker se esperaba salida 4 con la explicación, salió $($r.Codigo)"
            Dato $r.Salida
            $Problemas++
        }
        if ($r.Salida -match "docker.com/products/docker-desktop") {
            Bien "$($caso.Nombre): dice de dónde bajarlo"
        } else {
            Fallo "$($caso.Nombre): no dice de dónde bajar Docker"
            $Problemas++
        }
    }
}

# ── 4. Un interruptor que no existe: lo tiene que rechazar PowerShell, no ignorarlo ───────────────
Write-Host "`n> Un interruptor que no existe"
$raro = CorrerGuion "instalar\instalar_axioma.ps1" @("-NoExiste")
Dato "salida $($raro.Codigo): $(($raro.Salida -split "`n" | Select-Object -First 2) -join ' / ')"
# Lo que importa: que NO lo acepte en silencio. PowerShell lo rechaza con su propio error (código
# distinto de 0) o nombrándolo; las dos formas sirven, ignorarlo no.
if ($raro.Codigo -ne 0 -or $raro.Salida -match "NoExiste|parameter|parámetro") {
    Bien "no acepta en silencio un interruptor que no existe"
} else {
    Fallo "aceptó en silencio un interruptor que no existe"
    $Problemas++
}

# ── Resumen ──────────────────────────────────────────────────────────────────────────────────────
Write-Host ""
if ($Problemas -eq 0) {
    Write-Host "TODO BIEN: los instaladores de Windows arrancan y explican lo que corresponde." -ForegroundColor Green
    exit 0
}
Write-Host "$Problemas comprobación(es) fallaron." -ForegroundColor Red
exit 1
