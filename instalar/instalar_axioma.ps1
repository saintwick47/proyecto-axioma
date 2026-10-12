# ═══════════════════════════════════════════════════════════════
# AXIOMA — INSTALAR EN WINDOWS (una sola vez)
# ═══════════════════════════════════════════════════════════════
# OJO: NO lo ejecutes con doble clic: un .ps1 no se ejecuta así (Windows lo abre en el
#    editor, o lo corre en una consola que se cierra sola y perdés el mensaje).
#    Hacé doble clic en:  instalar\INSTALAR_AXIOMA.bat
#
# Qué hace, paso por paso (y si algo falla, dice EN QUÉ PASO y QUÉ HACER):
#   1. Windows y PowerShell.
#   2. Docker Desktop: si está apagado, lo arranca y espera.
#   3. Carpetas y .env (y recupera el caso de una CARPETA llamada .env, que crea Docker).
#   4. Arma la imagen de AXIOMA (la primera vez tarda unos minutos).
#   5. Accesos directos «AXIOMA» en el menú Inicio y en el Escritorio.
#   6. Enciende AXIOMA y abre el navegador.
#
# Uso desde PowerShell (si preferís la terminal):
#   .\instalar\instalar_axioma.ps1              # instalar y arrancar
#   .\instalar\instalar_axioma.ps1 -SinAcceso   # sin crear los accesos directos
#   .\instalar\instalar_axioma.ps1 -Ensayo      # muestra qué haría, sin tocar nada
#
# Códigos de salida: 0 = bien · 4 = falta algo (Docker, por ejemplo)

[CmdletBinding()]
param(
    [switch]$SinAcceso,
    [switch]$Ensayo
)

$ErrorActionPreference = "Stop"

# Marcadores sin emoji/dibujos: la consola de Windows (CP850/CP437) los imprime mal.
function paso  { param([string]$Texto) Write-Host "`n> $Texto" -ForegroundColor Cyan }
function ok    { param([string]$Texto) Write-Host "  [OK] $Texto" -ForegroundColor Green }
function aviso { param([string]$Texto) Write-Host "  [!]  $Texto" -ForegroundColor Yellow }
function mal   { param([string]$Texto) Write-Host "  [X]  $Texto" -ForegroundColor Red }

# ── Registro de pasos: es lo que permite decir EN QUÉ PASO falló ────────────
$Fallos = New-Object System.Collections.ArrayList

function Anotar-Fallo {
    param([string]$Paso, [string]$Que, [string]$Como)
    [void]$Fallos.Add([pscustomobject]@{ Paso = $Paso; Que = $Que; Como = $Como })
}

function Mostrar-Resumen {
    param([string]$Freno, [int]$Codigo)
    Write-Host ""
    Write-Host "============================================================"
    if ($Fallos.Count -eq 0 -and $Codigo -eq 0) {
        Write-Host "  LISTO: AXIOMA quedó instalado." -ForegroundColor Green
        Write-Host "============================================================"
        return 0
    }
    Write-Host "  NO SE PUDO TERMINAR LA INSTALACIÓN." -ForegroundColor Red
    if ($Freno) { Write-Host "  Frenó en: $Freno" -ForegroundColor Red }
    Write-Host "============================================================"
    for ($i = 0; $i -lt $Fallos.Count; $i++) {
        $f = $Fallos[$i]
        Write-Host ""
        Write-Host "  Falla $($i + 1) de $($Fallos.Count): Paso $($f.Paso): $($f.Que)" -ForegroundColor Yellow
        Write-Host "     Qué hacer: $($f.Como)"
    }
    Write-Host ""
    Write-Host "  Volvé a ejecutar INSTALAR_AXIOMA.bat cuando lo hayas resuelto." -ForegroundColor Cyan
    Write-Host "============================================================"
    return $Codigo
}

$Raiz = Split-Path -Parent $PSScriptRoot

paso "AXIOMA — instalación en Windows (una sola vez)"
Write-Host "   Carpeta del proyecto: $Raiz"

# ── 1. Windows y PowerShell ─────────────────────────────────────────────────
paso "Paso 1 de 6: Windows y PowerShell"
$psVersion = $PSVersionTable.PSVersion.ToString()
if ($PSVersionTable.PSVersion.Major -lt 5) {
    mal "Necesitás PowerShell 5.1 o superior (tenés $psVersion)."
    Anotar-Fallo "1" "PowerShell demasiado viejo ($psVersion)" "Actualizá Windows Update (PowerShell 5.1 viene con Windows 10/11)."
    exit (Mostrar-Resumen -Freno "Paso 1 de 6 (Windows y PowerShell)" -Codigo 4)
}
ok "PowerShell $psVersion · $([Environment]::OSVersion.VersionString)"
if ($Ensayo) { Write-Host "   (ensayo: no voy a tocar nada más que lo que diga abajo)" }

# ── 2. Docker Desktop ───────────────────────────────────────────────────────
# (El try/catch es por PowerShell 7.4+: ahí un comando que falla LANZA excepción, y con
#  `$ErrorActionPreference = "Stop"` el guion se cortaría en vez de explicar qué falta.)
function Probar-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    $hayDocker = $true
    try { & docker info *> $null } catch { $hayDocker = $false }
    if (-not $hayDocker) { return $false }
    return ($LASTEXITCODE -eq 0)
}

function Arrancar-DockerDesktop {
    # Si está instalado y apagado, lo arrancamos nosotros: es el error más común.
    $candidatos = @(
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )
    foreach ($exe in $candidatos) {
        if ($exe -and (Test-Path -LiteralPath $exe)) {
            try { Start-Process -FilePath $exe | Out-Null } catch { return $false }
            return $true
        }
    }
    return $false
}

paso "Paso 2 de 6: Docker Desktop (es el programa que ejecuta AXIOMA)"
if (Probar-Docker) {
    ok "Docker responde ($(& docker --version))"
} else {
    $instalado = (Get-Command docker -ErrorAction SilentlyContinue) -ne $null
    if ($instalado -and -not $Ensayo) {
        aviso "Docker está instalado pero no responde (lo más común: Docker Desktop está cerrado)."
        if (Arrancar-DockerDesktop) {
            aviso "Lo estoy arrancando; puede tardar hasta un minuto y medio en estar listo..."
            for ($intento = 1; $intento -le 30; $intento++) {
                Start-Sleep -Seconds 3
                if (Probar-Docker) { break }
            }
        }
    }
    if (Probar-Docker) {
        ok "Docker responde ($(& docker --version))"
    } else {
        if ($instalado) {
            mal "Docker está instalado pero no llegó a responder."
            Write-Host "     Abrilo a mano (menú Inicio -> Docker Desktop) y esperá a que diga «Engine running»."
            Anotar-Fallo "2" "Docker Desktop no responde" "Abrí Docker Desktop, esperá a que diga «Engine running» y volvé a intentar."
        } else {
            mal "Docker Desktop no está instalado."
            Write-Host "     Instalalo desde https://www.docker.com/products/docker-desktop"
            $wsl = Get-Command wsl -ErrorAction SilentlyContinue
            if ($wsl) {
                Write-Host "     (Si se queja de WSL 2: en una consola de administrador, 'wsl --update'.)"
                Anotar-Fallo "2" "Falta Docker Desktop" "Instalalo desde https://www.docker.com/products/docker-desktop (y si pide WSL 2: 'wsl --update' como administrador)."
            } else {
                Write-Host "     En Windows necesita WSL 2: en una consola de administrador, 'wsl --install' y reiniciá."
                Anotar-Fallo "2" "Falta Docker Desktop y WSL 2" "Como administrador: 'wsl --install', reiniciá, y después instalá Docker Desktop desde https://www.docker.com/products/docker-desktop."
            }
        }
        exit (Mostrar-Resumen -Freno "Paso 2 de 6 (Docker Desktop)" -Codigo 4)
    }
}

# ── 3. Carpetas y .env (tienen que existir ANTES de encender) ──────────────
# 2026-10-10: MEDIDO — las carpetas que el contenedor escribe tienen que existir y ser TUYAS: si no,
# Docker las crea como `root` y el contenedor (que corre con tu usuario) no puede escribir, así que
# AXIOMA no arranca. Y el `.env` tiene que existir: el compose lo monta como archivo y, si no está,
# Docker crea una CARPETA con ese nombre y el montaje falla (probado con `docker run -v`).
paso "Paso 3 de 6: carpetas y tu archivo de claves"
$carpetasOk = $true
foreach ($carpeta in @("data", "logs", "cache")) {
    $ruta = Join-Path $Raiz $carpeta
    if ($Ensayo) { Write-Host "  (ensayo) crearía la carpeta $ruta"; continue }
    try {
        if (-not (Test-Path -LiteralPath $ruta)) { New-Item -ItemType Directory -Path $ruta | Out-Null }
    } catch {
        mal "No pude crear la carpeta $ruta ($($_.Exception.Message))."
        $carpetasOk = $false
    }
}
if ($carpetasOk -and -not $Ensayo) { ok "Carpetas listas (data, logs, cache)" }
if (-not $carpetasOk) {
    Anotar-Fallo "3" "No se pudieron crear data/, logs/ o cache/" "Revisá que tengas permiso de escritura en $Raiz (o que el antivirus no lo bloquee)."
    exit (Mostrar-Resumen -Freno "Paso 3 de 6 (carpetas)" -Codigo 4)
}

$archivoEnv = Join-Path $Raiz ".env"
$plantillaEnv = Join-Path $Raiz ".env.example"
if ($Ensayo) {
    Write-Host "  (ensayo) si no existe, crearía $archivoEnv a partir de la plantilla"
} elseif (Test-Path -LiteralPath $archivoEnv -PathType Container) {
    # 2026-10-12 (ISSUE-147): si el archivo no existía, Docker crea una CARPETA con ese nombre y el
    # montaje queda roto. Test-Path da verdadero igual para una carpeta, así que sin este chequeo el
    # instalador no hacía NADA y el usuario no se enteraba. Si está vacía la sacamos (no se pierde
    # nada: la creó Docker); si tiene algo adentro, AVISAMOS y no borramos nada (regla del proyecto).
    $adentro = @(Get-ChildItem -LiteralPath $archivoEnv -Force -ErrorAction SilentlyContinue)
    if ($adentro.Count -eq 0) {
        try {
            Remove-Item -LiteralPath $archivoEnv -Force
            aviso "Había una CARPETA vacía llamada .env (la crea Docker cuando falta el archivo): la saqué."
        } catch {
            mal "Hay una CARPETA llamada .env y no pude sacarla ($($_.Exception.Message))."
            Anotar-Fallo "3" "Existe una CARPETA .env y no se pudo borrar" "Cerrala: borrá a mano la carpeta $archivoEnv y volvé a ejecutar el instalador."
            exit (Mostrar-Resumen -Freno "Paso 3 de 6 (.env es una carpeta)" -Codigo 4)
        }
    } else {
        mal "Hay una CARPETA llamada .env con archivos adentro: el montaje va a fallar."
        Write-Host "     No borro nada sin tu permiso. Mirá qué tiene:  Get-ChildItem `"$archivoEnv`""
        Write-Host "     Si no te sirve, borrala y volvé a ejecutar:   Remove-Item -Recurse `"$archivoEnv`""
        Anotar-Fallo "3" "Existe una CARPETA .env con contenido" "Revisá qué hay en $archivoEnv; si no lo necesitás, borrala y volvé a ejecutar el instalador."
        exit (Mostrar-Resumen -Freno "Paso 3 de 6 (.env es una carpeta con contenido)" -Codigo 4)
    }
}

if (-not $Ensayo) {
    if (-not (Test-Path -LiteralPath $plantillaEnv)) {
        mal "No encuentro la plantilla .env.example en $Raiz"
        Anotar-Fallo "3" "Falta .env.example" "¿Descargaste el proyecto completo? La plantilla tiene que estar al lado de este instalador."
        exit (Mostrar-Resumen -Freno "Paso 3 de 6 (.env.example)" -Codigo 4)
    }
    if (-not (Test-Path -LiteralPath $archivoEnv)) {
        Copy-Item -LiteralPath $plantillaEnv -Destination $archivoEnv
        ok "Creé tu .env a partir de la plantilla (ahí se guardan tus claves, y no se publican)"
    } else {
        ok "Tu .env ya existía (no lo toco: ahí están tus claves)"
    }
}

# ── 4. Imagen ───────────────────────────────────────────────────────────────
paso "Paso 4 de 6: armando AXIOMA (la primera vez tarda unos minutos)"
if ($Ensayo) {
    Write-Host "  (ensayo) cd $Raiz; docker compose build"
} else {
    Push-Location $Raiz
    try {
        $armado = $true
        try { & docker compose build } catch { $armado = $false }
        if ($armado) { $armado = ($LASTEXITCODE -eq 0) }
        if (-not $armado) {
            mal "No se pudo armar la imagen (mirá los mensajes de arriba)."
            Anotar-Fallo "4" "Falló 'docker compose build'" "Copiá el error de arriba. Causas típicas: se cortó internet, o a Docker Desktop le queda poca memoria (Settings -> Resources)."
            exit (Mostrar-Resumen -Freno "Paso 4 de 6 (armar la imagen)" -Codigo 4)
        }
    } finally { Pop-Location }
    ok "AXIOMA armado"
}

# ── 5. Accesos directos (menú Inicio y Escritorio) ──────────────────────────
if (-not $SinAcceso) {
    paso "Paso 5 de 6: creando los accesos directos"
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
            Write-Host "        Podés arrancar igual con: instalar\INICIAR_AXIOMA.bat"
        }
    }
}

# ── 6. Arrancar ─────────────────────────────────────────────────────────────
paso "Paso 6 de 6: arrancando AXIOMA"
if ($Ensayo) {
    Write-Host "  (ensayo) powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\iniciar_axioma.ps1`""
    exit (Mostrar-Resumen -Codigo 0)
}
aviso "En Windows la voz todavía no está disponible (el micrófono del equipo no llega al contenedor): el chat y los modelos funcionan igual."
& "$PSScriptRoot\iniciar_axioma.ps1"
$codigoArranque = $LASTEXITCODE
if ($codigoArranque -ne 0) {
    mal "AXIOMA quedó armado, pero no arrancó (código $codigoArranque)."
    Anotar-Fallo "6" "AXIOMA se instaló pero no arrancó" "Mirá el detalle de arriba; el comando útil es: docker compose logs --tail 40 axioma-puente"
    exit (Mostrar-Resumen -Freno "Paso 6 de 6 (arrancar)" -Codigo 4)
}
exit (Mostrar-Resumen -Codigo 0)
