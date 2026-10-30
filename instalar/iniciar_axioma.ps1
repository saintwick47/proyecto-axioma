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

function Comando-Compose {
    # 2026-10-30 (MEDIDO en Windows): si falta el plugin Compose v2, `docker compose ...` cae al CLI
    # de `docker` y el flag siguiente se interpreta como propio ("unknown shorthand flag: 'd' in -d").
    # Devuelve la lista del Compose que funcione, o $null si no hay ninguno.
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $null }
    try { & docker compose version *> $null } catch { }
    if ($LASTEXITCODE -eq 0) { return @("docker", "compose") }
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        try { & docker-compose version *> $null } catch { return $null }
        if ($LASTEXITCODE -eq 0) { return @("docker-compose") }
    }
    return $null
}

function Invocar-Compose {
    param([string[]]$Compose, [string[]]$Argumentos)
    $previos = @()
    if ($Compose.Count -gt 1) { $previos = $Compose[1..($Compose.Count - 1)] }
    & $Compose[0] @previos @Argumentos
}

function Buscar-DockerDesktopExe {
    # Docker Desktop se instala "para todos" (Program Files) o SOLO para el usuario
    # (%LOCALAPPDATA%\Programs\DockerDesktop): hay que mirar los dos sitios. Como último recurso se
    # deduce de dónde está docker.exe (...\DockerDesktop\resources\bin\docker.exe) o del registro.
    $candidatos = New-Object System.Collections.ArrayList
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base) { [void]$candidatos.Add((Join-Path $base "Docker\Docker\Docker Desktop.exe")) }
    }
    if ($env:LOCALAPPDATA) {
        [void]$candidatos.Add((Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"))
        [void]$candidatos.Add((Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"))
    }
    try {
        $cli = Get-Command docker -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cli -and $cli.Source) {
            $raizDocker = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $cli.Source))
            if ($raizDocker) { [void]$candidatos.Add((Join-Path $raizDocker "Docker Desktop.exe")) }
        }
    } catch { }
    foreach ($clave in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop", "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop")) {
        try {
            $reg = Get-ItemProperty -LiteralPath $clave -ErrorAction Stop
            if ($reg.InstallLocation) { [void]$candidatos.Add((Join-Path $reg.InstallLocation "Docker Desktop.exe")) }
        } catch { }
    }
    foreach ($exe in $candidatos) {
        if ($exe -and (Test-Path -LiteralPath $exe)) { return $exe }
    }
    return ""
}

function Agregar-DockerAlPath {
    # Docker Desktop instalado "para este usuario" deja docker.exe en ...\DockerDesktop\resources\bin;
    # si esta ventana se abrió antes de instalarlo, el PATH todavía no lo incluye.
    if (Get-Command docker -ErrorAction SilentlyContinue) { return }
    $exe = Buscar-DockerDesktopExe
    if (-not $exe) { return }
    $bin = Join-Path (Split-Path -Parent $exe) "resources\bin"
    if (Test-Path -LiteralPath (Join-Path $bin "docker.exe")) { $env:Path = "$bin;$env:Path" }
}

function Asegurar-Docker {
    # Devuelve $true si Docker responde. Si Docker Desktop está instalado pero cerrado (lo normal
    # después de reiniciar Windows), lo abre y espera hasta 3 minutos: así el doble clic "simplemente anda".
    Agregar-DockerAlPath
    if (Probar-Docker) { return $true }
    $exe = Buscar-DockerDesktopExe
    if (-not $exe) { return $false }
    if ($Ensayo) {
        Write-Host "  (ensayo) Docker Desktop está cerrado: lo abriría ($exe) y esperaría hasta 3 minutos a que responda"
        return $false
    }
    aviso "Docker Desktop está cerrado: lo abro yo (puede tardar hasta tres minutos; la primera vez, más)..."
    try { Start-Process -FilePath $exe | Out-Null }
    catch { aviso "No pude abrirlo: $($_.Exception.Message)"; return $false }
    $espera = 0
    while ($espera -lt 180) {
        Start-Sleep -Seconds 3
        $espera += 3
        Agregar-DockerAlPath
        if (Probar-Docker) { ok "Docker Desktop listo (tardó $espera s)"; return $true }
        if (($espera % 15) -eq 0) { Write-Host "     ...sigo esperando a Docker Desktop ($espera s)" }
    }
    return $false
}

function Explicar-FaltaDocker {
    $exe = Buscar-DockerDesktopExe
    if ($exe) {
        mal "Docker Desktop está instalado pero no llegó a responder."
        Write-Host "     1. Abrilo desde el menú Inicio y esperá a que diga «Engine running»."
        Write-Host "        (Si te pide aceptar los términos o actualizar WSL, hacelo.)"
        Write-Host "     2. Volvé a hacer doble clic acá."
        Write-Host "     Para que no haga falta cada vez: Docker Desktop -> Settings -> General -> «Start Docker Desktop when you sign in»."
    } else {
        mal "Docker Desktop no está disponible (es el programa que ejecuta AXIOMA)."
        Write-Host "     1. Instalalo desde https://www.docker.com/products/docker-desktop"
        Write-Host "     2. Abrilo una vez y esperá a que diga «Engine running»."
        Write-Host "     3. Volvé a hacer doble clic acá."
    }
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
    $comando = "docker compose down"
    if ($Ensayo) { Write-Host "  (ensayo) $comando"; exit 0 }
    if (-not (Probar-Docker)) { Explicar-FaltaDocker; exit 4 }
    $compose = Comando-Compose
    if ($null -eq $compose) {
        mal "Docker Compose no está disponible (Docker lo necesita para apagar AXIOMA)."
        Write-Host "     Probá:  docker compose version"
        exit 4
    }
    Push-Location $Raiz
    try {
        try { Invocar-Compose -Compose $compose -Argumentos @("down") }
        catch { aviso "Docker devolvió un error al apagar: revisá que Docker Desktop esté abierto." }
    } finally { Pop-Location }
    ok "AXIOMA apagado. Tus datos y tus claves quedan guardados en la carpeta del proyecto."
    exit 0
}

# ── 1. ¿Está Docker? ────────────────────────────────────────────────────────
paso "Comprobando Docker"
if (-not (Asegurar-Docker)) { Explicar-FaltaDocker; exit 4 }
# 2026-10-30 (MEDIDO): sin el plugin Compose v2, `docker compose ...` cae al CLI de docker y el
# flag siguiente se toma como propio ("unknown shorthand flag: 'd' in -d"). Se elige el que ande.
$compose = Comando-Compose
if ($null -eq $compose) {
    mal "Docker Compose no está disponible (Docker lo necesita para encender AXIOMA)."
    Write-Host "     Probá:  docker compose version"
    Write-Host "     Docker Desktop lo incluye; si tenés otro 'docker' en el PATH, quitálo o actualizá Docker Desktop."
    exit 4
}
ok ("Compose: " + ($compose -join " "))
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
} elseif (Test-Path -LiteralPath $archivoEnv -PathType Container) {
    # 2026-10-12 (ISSUE-147): Docker crea una CARPETA `.env` cuando falta el archivo, y `Test-Path`
    # da verdadero igual para una carpeta: sin este chequeo el arranque no hacía NADA y el montaje
    # seguía roto. Vacía la sacamos (la creó Docker); con contenido, avisamos y no borramos nada.
    $adentro = @(Get-ChildItem -LiteralPath $archivoEnv -Force -ErrorAction SilentlyContinue)
    if ($adentro.Count -eq 0) {
        try {
            Remove-Item -LiteralPath $archivoEnv -Force
            Copy-Item -LiteralPath $plantillaEnv -Destination $archivoEnv
            ok "Había una CARPETA vacía .env (la crea Docker cuando falta el archivo): la saqué y creé tu .env"
        } catch {
            mal "Hay una CARPETA llamada .env y no pude sacarla ($($_.Exception.Message))."
            exit 4
        }
    } else {
        mal "Hay una CARPETA llamada .env con contenido: el montaje va a fallar."
        Write-Host "     Mirá qué tiene:  Get-ChildItem `"$archivoEnv`""
        Write-Host "     Si no te sirve, borrala:  Remove-Item -Recurse `"$archivoEnv`""
        exit 4
    }
} elseif ((Test-Path -LiteralPath $plantillaEnv) -and -not (Test-Path -LiteralPath $archivoEnv)) {
    Copy-Item -LiteralPath $plantillaEnv -Destination $archivoEnv
    ok "Creé tu .env a partir de la plantilla (ahí se guardan tus claves, y no se publican)"
}

# ── 3. ¿Usamos el Ollama del equipo o el que trae AXIOMA? ───────────────────
paso "Buscando Ollama"
# Los servicios se nombran UNO POR UNO a propósito. Medido el 2026-10-06 en Linux: `docker compose
# `up -d` a secas levanta ADEMÁS el servicio `axioma` (no tiene perfil, arranca siempre), y
# como ése usa la red del equipo, los dos pelean por el puerto 8080: `axioma` queda en bucle de
# reinicios y en Windows directamente no puede usar esa red. Por eso: `axioma-puente` y, si hace
# falta, `ollama`.
if (Hay-OllamaEnElEquipo) {
    # 2026-10-30 (MEDIDO en Windows, Docker 29.8.2): `docker compose --profile ...` falla con
    # "unknown flag: --profile". No hace falta: al NOMBRAR el servicio, Compose habilita su perfil.
    $servicios = @("axioma-puente")
    ok "Ya tenés Ollama andando en 127.0.0.1:11434: uso ese (no hace falta otro)."
} else {
    $servicios = @("axioma-puente", "ollama")
    aviso "No encontré Ollama en 127.0.0.1:11434."
    ok "Levanto el Ollama que viene con AXIOMA (los modelos quedan guardados en un volumen)."
}

# ── 4. Encender, esperar y abrir el navegador ───────────────────────────────
paso "Encendiendo AXIOMA"
if ($Ensayo) {
    Write-Host "  (ensayo) cd $Raiz; $($compose -join ' ') up -d $($servicios -join ' ')"
    Write-Host "  (ensayo) abriría $Url cuando responda"
    exit 0
}
Push-Location $Raiz
try {
    # 2026-10-27 (MEDIDO en una instalación real en Windows, paso 6 de 6): Docker Desktop puede
    # tener `credsStore: desktop` en su config y el ayudante `docker-credential-desktop` FUERA del
    # PATH. El instalador ya esquiva eso al bajar la imagen de AXIOMA, pero `compose up` tambien
    # intenta bajar `ollama/ollama` y moria con:
    #   error getting credentials - err: exec: "docker-credential-desktop": executable file not found
    # Estrategia: primer intento normal; si falla por credenciales se reintenta UNA vez con un
    # DOCKER_CONFIG temporal sin credsStore (todas las imagenes del compose son publicas).
    # 2026-10-28 (MEDIDO en un Windows real): con `$ErrorActionPreference = "Stop"`, redirigir el
    # stderr de un comando nativo (`2>&1`) lanza NativeCommandError ANTES de asignar la variable.
    # Resultado: el texto de Docker se PERDIA (el usuario veia "no pudo encender" sin motivo) y el
    # reintento por credenciales nunca disparaba porque el mensaje no llegaba a `$salida`.
    # Se captura con "Continue" y se restaura al terminar.
    $salida = ""
    $codigo = 0
    $encendido = $true
    $eapAntes = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $salida = (Invocar-Compose -Compose $compose -Argumentos (@("up", "-d") + $servicios) 2>&1 | Out-String) } catch { $encendido = $false }
    $codigo = $LASTEXITCODE
    $ErrorActionPreference = $eapAntes
    if ($encendido) { $encendido = ($codigo -eq 0) }
    if (-not $encendido -and ($salida -match "credential|error getting credentials")) {
        aviso "Docker no encontro su ayudante de credenciales: reintento sin el (las imagenes son publicas)."
        $cfgTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("axioma-cfg-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $cfgTmp -Force | Out-Null
        '{"auths":{}}' | Set-Content -Path (Join-Path $cfgTmp "config.json") -Encoding ascii
        $cfgAntes = $env:DOCKER_CONFIG
        $eapAntes2 = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $env:DOCKER_CONFIG = $cfgTmp
            $salida = (& docker compose up -d @servicios 2>&1 | Out-String)
            $codigo = $LASTEXITCODE
            $encendido = ($codigo -eq 0)
        } catch { $encendido = $false } finally {
            $ErrorActionPreference = $eapAntes2
            if ($null -eq $cfgAntes) {
                Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
            } else {
                $env:DOCKER_CONFIG = $cfgAntes
            }
            Remove-Item -Recurse -Force $cfgTmp -ErrorAction SilentlyContinue
        }
    }
    if ($salida) { Write-Host $salida }
    if (-not $encendido) {
        mal "Docker no pudo encender AXIOMA (mira los mensajes de arriba)."
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
