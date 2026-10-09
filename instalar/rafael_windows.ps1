# ═══════════════════════════════════════════════════════════════
# AXIOMA — RAFAEL PARA WINDOWS (demonio de voz nativo)
# ═══════════════════════════════════════════════════════════════
# Decisión del usuario (2026-10-08): en Windows la voz NO va dentro del contenedor (Docker Desktop corre
# sobre WSL 2, que no expone el micrófono ni el servidor de sonido). Va un **demonio nativo**: escucha y
# habla con la voz que YA TRAE Windows (`System.Speech`), y le manda el texto al AXIOMA que corre en el
# contenedor por HTTP. Así no hay que instalar Python ni nada extra en Windows.
#
# QUÉ HACE
#   1. Comprueba que AXIOMA esté encendido y que el endpoint de chat conteste.
#   2. Escucha: dictás con el reconocedor de Windows, o escribís el texto (si no hay micrófono o no está
#      el idioma instalado).
#   3. Le manda el texto a AXIOMA por `POST /api/v1/chat` y espera la respuesta.
#   4. Te la LEE en voz alta con la voz de Windows.
#
# Uso:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\instalar\rafael_windows.ps1
#   ... -Ensayo          # comprueba todo (voz, micrófono, AXIOMA) y sale, sin escuchar
#   ... -SoloTexto       # sin micrófono: escribís y te contesta por voz
#   ... -Url http://127.0.0.1:8080   # si AXIOMA corre en otro puerto o en otra máquina
#
# Códigos de salida: 0 = bien · 4 = falta algo (AXIOMA apagado, sin voz instalada)

[CmdletBinding()]
param(
    [string]$Url = "http://127.0.0.1:8080",
    [switch]$Ensayo,
    [switch]$SoloTexto,
    [string]$FraseDeActivacion = ""
)

$NOMBRE_REGISTRO = "rafael_windows"
$ErrorActionPreference = "Continue"     # el guion se explica solo: no muere en el primer tropiezo

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

# ── Las rutas del endpoint ──────────────────────────────────────────────────────────────────────
# MEDIDO el 2026-10-08 contra el AXIOMA andando: el router tiene prefijo `/api/v1` y además se monta
# bajo `/api`, así que la dirección real queda DUPLICADA: `/api/api/v1/chat`. Se prueban las dos, en
# orden, para que esto siga funcionando el día que se arregle el prefijo.
$rutas = @("/api/v1/chat", "/api/api/v1/chat")

function Buscar-Endpoint {
    param([string]$Base)
    foreach ($ruta in $rutas) {
        $completa = "$Base$ruta"
        try {
            # Un pedido con mensaje vacío: si la ruta existe contesta 200 o 422 (no 404).
            $r = Invoke-WebRequest -Uri $completa -Method Post -TimeoutSec 8 -UseBasicParsing `
                 -ContentType "application/json" -Body '{"message":"ping"}' -ErrorAction Stop
            if ($r.StatusCode -lt 500) { return $completa }
        } catch {
            $respuesta = $_.Exception.Response
            if ($respuesta -and [int]$respuesta.StatusCode -ne 404) { return $completa }
        }
    }
    return $null
}

function Preguntar {
    param([string]$Endpoint, [string]$Texto)
    $cuerpo = @{ message = $Texto; session_id = "rafael-windows" } | ConvertTo-Json -Compress
    try {
        $r = Invoke-WebRequest -Uri $Endpoint -Method Post -TimeoutSec 300 -UseBasicParsing `
             -ContentType "application/json" -Body $cuerpo -ErrorAction Stop
        $datos = $r.Content | ConvertFrom-Json
        if ($datos.success -eq $false) {
            return @{ ok = $false; texto = "AXIOMA contestó con un error: $($datos.error)" }
        }
        return @{ ok = $true; texto = [string]$datos.content }
    } catch {
        return @{ ok = $false; texto = "No pude hablar con AXIOMA: $($_.Exception.Message)" }
    }
}

# ── 1. La voz de Windows ────────────────────────────────────────────────────────────────────────
paso "Comprobando la voz de Windows"
$voz = $null
try {
    Add-Type -AssemblyName System.Speech -ErrorAction Stop
    $voz = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $voces = $voz.GetInstalledVoices() | Where-Object { $_.Enabled }
    if ($voces.Count -gt 0) {
        $elegida = ($voces | Where-Object { $_.VoiceInfo.Culture.Name -like "es*" } | Select-Object -First 1)
        if (-not $elegida) { $elegida = $voces | Select-Object -First 1 }
        $voz.SelectVoice($elegida.VoiceInfo.Name)
        ok "Va a hablar con: $($elegida.VoiceInfo.Name)"
        if ($elegida.VoiceInfo.Culture.Name -notlike "es*") {
            aviso "Esa voz no es en castellano: para tener una, en Windows -> Configuración -> Hora e idioma -> Voz"
        }
    } else {
        mal "Windows no tiene ninguna voz instalada (Configuración -> Hora e idioma -> Voz)"
        exit 4
    }
} catch {
    mal "No pude usar la voz de Windows (System.Speech): $($_.Exception.Message)"
    exit 4
}

# ── 2. El micrófono (opcional: si no hay, se escribe) ───────────────────────────────────────────
$reconocedor = $null
if (-not $SoloTexto) {
    paso "Comprobando el micrófono"
    try {
        $reconocedor = New-Object System.Speech.Recognition.SpeechRecognitionEngine
        $reconocedor.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
        $reconocedor.SetInputToDefaultAudioDevice()
        ok "Micrófono listo (idioma del reconocedor: $($reconocedor.RecognizerInfo.Culture.Name))"
        if ($reconocedor.RecognizerInfo.Culture.Name -notlike "es*") {
            aviso "El reconocedor no está en castellano: para dictar en español hay que agregar el idioma"
            aviso "en Windows -> Configuración -> Hora e idioma -> Voz. Igual podés escribir el texto."
        }
    } catch {
        aviso "No pude usar el micrófono: $($_.Exception.Message)"
        aviso "Sigo en modo texto: escribís y AXIOMA te contesta por voz."
        $reconocedor = $null
    }
}

# ── 3. AXIOMA ───────────────────────────────────────────────────────────────────────────────────
paso "Buscando AXIOMA en $Url"
$endpoint = Buscar-Endpoint -Base $Url
if (-not $endpoint) {
    mal "AXIOMA no contesta en $Url (¿está encendido? probá: docker compose ps)"
    exit 4
}
ok "AXIOMA contesta en $endpoint"

if ($Ensayo) {
    paso "Ensayo terminado (no escuché nada)"
    Write-Host "  Todo listo: la voz de Windows anda, AXIOMA contesta y el micrófono $(if ($reconocedor) { 'está' } else { 'no está (modo texto)' })."
    exit 0
}

# ── 4. El lazo: escuchar -> preguntar -> hablar ───────────────────────────────────────────────────
paso "Rafael está escuchando"
if ($reconocedor) {
    Write-Host "  Hablá y hacé una pausa para terminar la frase. Para salir: decí «salir» o cortá con Ctrl+C."
} else {
    Write-Host "  Escribí tu pedido y Enter. Para salir: escribí 'salir'."
}
if ($FraseDeActivacion) { Write-Host "  Frase de activación: $FraseDeActivacion" }

while ($true) {
    $texto = $null
    try {
        if ($reconocedor) {
            $resultado = $reconocedor.Recognize([TimeSpan]::FromSeconds(15))
            if ($resultado -and $resultado.Text) { $texto = $resultado.Text }
        } else {
            $texto = Read-Host "Vos"
        }
    } catch {
        aviso "Se cortó la escucha ($($_.Exception.Message)): sigo en modo texto"
        $reconocedor = $null
    }

    if (-not $texto) { continue }
    $texto = $texto.Trim()
    if ($texto -match "^(salir|chau|adiós|adios)$") { break }
    if ($FraseDeActivacion -and $texto -notmatch [regex]::Escape($FraseDeActivacion)) {
        continue                                 # todavía no lo llamaste
    }
    if ($FraseDeActivacion) { $texto = $texto -replace [regex]::Escape($FraseDeActivacion), "" }

    Write-Host "  (pensando...)" -ForegroundColor DarkGray
    $r = Preguntar -Endpoint $endpoint -Texto $texto
    if ($r.ok) {
        Write-Host "  Rafael: $($r.texto)" -ForegroundColor Green
        try { $voz.Speak($r.texto) } catch { aviso "No pude leer la respuesta: $($_.Exception.Message)" }
    } else {
        mal $r.texto
        try { $voz.Speak("No pude obtener la respuesta.") } catch { }
    }
}

paso "Listo"
try { $voz.Speak("Hasta luego.") } catch { }
exit 0
