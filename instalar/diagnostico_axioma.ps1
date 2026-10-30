# =====================================================================
# AXIOMA - DIAGNOSTICO DE INSTALACION (Windows)
# =====================================================================
# Que hace:
#   Recolecta, SIN modificar nada del sistema, la informacion necesaria para
#   saber por que falla (o falla en silencio) la instalacion de AXIOMA:
#   Windows, virtualizacion, WSL 2, Docker Desktop, red, puertos, carpeta del
#   proyecto, .env (sin mostrar claves), accesos directos, antivirus, eventos
#   del sistema, y un ensayo de los instaladores con toda su salida.
#   Cada comando externo corre con tiempo limite: si algo se CUELGA, queda
#   registrado en vez de dejar la ventana congelada.
#
# Privacidad:
#   No guarda valores de claves ni tokens (se redactan). Todo queda en una
#   carpeta y un .zip locales; vos decidis a quien se lo pasas.
#
# Uso:
#   DIAGNOSTICO_AXIOMA.bat                  (solo lectura)
#   DIAGNOSTICO_AXIOMA_CON_TRAZA.bat        (ademas INSTALA de verdad con traza)
#   powershell -ExecutionPolicy Bypass -File diagnostico_axioma.ps1 [-Raiz C:\ruta] [-Traza] [-SinRed]
#
# Sin emoji a proposito: la consola de Windows imprime mal los caracteres raros.
# Compatible con Windows PowerShell 5.1 (sin sintaxis exclusiva de PowerShell 7).
# =====================================================================
param(
    [string]$Raiz = "",
    [string]$Salida = "",
    [switch]$Traza,
    [switch]$SinAcceso,
    [switch]$SinRed,
    [switch]$SinPreguntas,
    [int]$TimeoutDocker = 40,
    [int]$TimeoutEnsayo = 90
)

Set-StrictMode -Off
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$script:VersionScript = "1.0"
$script:EsWindows = ($env:OS -eq "Windows_NT")
$script:Inicio = Get-Date
$script:Texto = New-Object System.Text.StringBuilder
$script:Hallazgos = New-Object System.Collections.ArrayList
$script:Hechos = [ordered]@{}
$script:Tareas = New-Object System.Collections.ArrayList
$script:Temp = Join-Path ([IO.Path]::GetTempPath()) ("axioma_diag_" + [guid]::NewGuid().ToString("N"))
[void](New-Item -ItemType Directory -Path $script:Temp -Force)
$script:PF = $env:ProgramFiles; if (-not $script:PF) { $script:PF = "C:\Program Files" }
$script:LAD = $env:LOCALAPPDATA; if (-not $script:LAD) { $script:LAD = [IO.Path]::GetTempPath() }
$script:APPD = $env:APPDATA; if (-not $script:APPD) { $script:APPD = [IO.Path]::GetTempPath() }
$script:UPROF = $env:USERPROFILE; if (-not $script:UPROF) { $script:UPROF = $env:HOME }
if (-not $script:UPROF) { $script:UPROF = [IO.Path]::GetTempPath() }
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------
function Redactar {
    param([string]$t)
    if ([string]::IsNullOrEmpty($t)) { return $t }
    $t = [regex]::Replace($t, '(?i)Bearer\s+[A-Za-z0-9._\-]{10,}', 'Bearer <REDACTADO>')
    $t = [regex]::Replace($t, '(?im)^(\s*[A-Za-z0-9_.\-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.\-]*\s*[=:]\s*)(?!<(?:definida|vacia|REDACTADO))(\S.*)$', '$1<REDACTADO>')
    $t = [regex]::Replace($t, '(?i)(api[_-]?key|token|secret|password|authorization)(["'']?\s*[=:]\s*["'']?)(?!<(?:definida|vacia|REDACTADO))([^\s"'',;&]{6,})', '$1$2<REDACTADO>')
    $t = [regex]::Replace($t, '\b(sk-[A-Za-z0-9_\-]{16,}|tvly-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_\-]{20,}|xox[abp]-[A-Za-z0-9\-]{10,})', '<REDACTADO>')
    return $t
}

function Seccion {
    param([string]$Titulo)
    [void]$script:Texto.AppendLine("")
    [void]$script:Texto.AppendLine(("=" * 78))
    [void]$script:Texto.AppendLine("== " + $Titulo)
    [void]$script:Texto.AppendLine(("=" * 78))
    Write-Host ("  > " + $Titulo) -ForegroundColor Cyan
}

function Linea {
    param([string]$t)
    [void]$script:Texto.AppendLine((Redactar $t))
}

function Hallazgo {
    param([string]$Nivel, [string]$Que, [string]$Como = "")
    [void]$script:Hallazgos.Add([pscustomobject]@{ Nivel = $Nivel; Que = $Que; Como = $Como })
}

function Hecho {
    param([string]$Clave, $Valor)
    $script:Hechos[$Clave] = $Valor
}

function Intentar {
    param([string]$Nombre, [scriptblock]$Bloque)
    try { & $Bloque }
    catch {
        Linea ("[ERROR en '" + $Nombre + "'] " + $_.Exception.Message)
        if ($_.InvocationInfo) { Linea ("    " + ($_.InvocationInfo.PositionMessage -replace "\r?\n", " | ")) }
    }
}

function Leer-Texto {
    param([string]$Ruta)
    if (-not (Test-Path -LiteralPath $Ruta)) { return "" }
    $b = [IO.File]::ReadAllBytes($Ruta)
    if ($b.Length -eq 0) { return "" }
    $n = [Math]::Min($b.Length, 400)
    $nul = 0
    for ($i = 0; $i -lt $n; $i++) { if ($b[$i] -eq 0) { $nul++ } }
    if ($nul -gt ($n / 4)) { $t = [Text.Encoding]::Unicode.GetString($b) } else { $t = [Text.Encoding]::UTF8.GetString($b) }
    $t = [regex]::Replace($t, ([string][char]27) + "\[[0-9;]*[A-Za-z]", "")
    return $t.TrimStart([char]0xFEFF)
}

function Recortar {
    param([string]$t, [int]$Max = 20000)
    if ($null -eq $t) { return "" }
    if ($t.Length -le $Max) { return $t }
    return ($t.Substring(0, $Max) + "`r`n[... recortado: " + ($t.Length - $Max) + " caracteres mas ...]")
}

function Volcar {
    param($r)
    Linea ("$ " + $r.Comando)
    if ($r.Timeout) { Linea ("  >> TIMEOUT: no termino en el tiempo limite (" + $r.Segundos + " s). Posible cuelgue.") }
    elseif ($null -ne $r.Codigo) { Linea ("  (codigo de salida " + $r.Codigo + ", " + $r.Segundos + " s)") }
    else { Linea ("  (sin codigo de salida, " + $r.Segundos + " s)") }
    if ($r.Salida) { Linea (Recortar $r.Salida.TrimEnd()) }
    if ($r.Error)  { Linea ("  [stderr] " + (Recortar $r.Error.TrimEnd() 8000)) }
    Linea ""
}

# Ejecuta un programa externo con tiempo limite, capturando salida y error.
function Ejecutar {
    param(
        [string]$Nombre,
        [string]$Exe,
        [string]$Argumentos = "",
        [int]$Timeout = 60,
        [string]$Dir = "",
        [switch]$Silencioso
    )
    Write-Host ("    - " + $Nombre) -ForegroundColor DarkGray
    $o = Join-Path $script:Temp ([guid]::NewGuid().ToString("N") + ".out")
    $e = Join-Path $script:Temp ([guid]::NewGuid().ToString("N") + ".err")
    $r = [pscustomobject]@{ Nombre = $Nombre; Comando = (($Exe + " " + $Argumentos).Trim()); Codigo = $null; Segundos = 0; Timeout = $false; Salida = ""; Error = ""; NoEncontrado = $false }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $cmd = Get-Command $Exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) {
        $r.NoEncontrado = $true
        $r.Error = "No se encontro el programa '" + $Exe + "' en el PATH."
    } else {
        try {
            $ruta = $cmd.Source
            if (-not $ruta) { $ruta = $cmd.Definition }
            $parametros = @{ FilePath = $ruta; NoNewWindow = $true; PassThru = $true; RedirectStandardOutput = $o; RedirectStandardError = $e }
            if ($Argumentos) { $parametros["ArgumentList"] = $Argumentos }
            if ($Dir -and (Test-Path -LiteralPath $Dir -PathType Container)) { $parametros["WorkingDirectory"] = $Dir }
            $p = Start-Process @parametros
            $null = $p.Handle
            if (-not $p.WaitForExit($Timeout * 1000)) {
                $r.Timeout = $true
                if ($script:EsWindows) { try { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null } catch { } }
                try { $p.Kill() } catch { }
            } else {
                $p.WaitForExit()
                $r.Codigo = $p.ExitCode
            }
        } catch {
            $r.Error = "No se pudo ejecutar: " + $_.Exception.Message
        }
    }
    $sw.Stop()
    $r.Segundos = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $r.Salida = Leer-Texto $o
    $r.Error = (($r.Error + " " + (Leer-Texto $e)).Trim())
    Remove-Item -LiteralPath $o, $e -Force -ErrorAction SilentlyContinue
    [void]$script:Tareas.Add($r)
    if (-not $Silencioso) { Volcar $r }
    return $r
}

function Cola-De-Archivo {
    param([string]$Ruta, [int]$Lineas = 150)
    if (-not (Test-Path -LiteralPath $Ruta -PathType Leaf)) { Linea ("(no existe: " + $Ruta + ")"); return }
    $info = Get-Item -LiteralPath $Ruta
    Linea ("--- " + $Ruta + "  (" + $info.Length + " bytes, modificado " + $info.LastWriteTime + ")")
    try {
        $t = Get-Content -LiteralPath $Ruta -Tail $Lineas -ErrorAction Stop
        Linea (($t -join "`r`n"))
    } catch { Linea ("(no pude leerlo: " + $_.Exception.Message + ")") }
    Linea ""
}

function Probar-Url {
    param([string]$Url, [int]$Timeout = 8, [int]$MaxCuerpo = 1500, [string]$Metodo = "GET")
    $res = [pscustomobject]@{ Url = $Url; Estado = $null; Ms = 0; Cuerpo = ""; Error = "" }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $w = Invoke-WebRequest -Uri $Url -Method $Metodo -UseBasicParsing -TimeoutSec $Timeout -ErrorAction Stop
        $res.Estado = [int]$w.StatusCode
        $res.Cuerpo = [string]$w.Content
    } catch {
        $ex = $_.Exception
        $res.Error = $ex.Message
        if ($ex.Response) {
            try {
                $res.Estado = [int]$ex.Response.StatusCode
                $sr = New-Object IO.StreamReader($ex.Response.GetResponseStream())
                $res.Cuerpo = $sr.ReadToEnd()
                $sr.Close()
            } catch { }
        }
    }
    $sw.Stop()
    $res.Ms = [int]$sw.Elapsed.TotalMilliseconds
    if ($res.Cuerpo.Length -gt $MaxCuerpo) { $res.Cuerpo = $res.Cuerpo.Substring(0, $MaxCuerpo) + "..." }
    return $res
}

function Probar-Puerto {
    param([string]$Servidor, [int]$Puerto = 443, [int]$MsEspera = 5000)
    $r = [pscustomobject]@{ Dns = ""; Tcp = $false; Error = "" }
    try {
        $ips = [Net.Dns]::GetHostAddresses($Servidor)
        $r.Dns = (($ips | ForEach-Object { $_.ToString() }) -join ", ")
    } catch { $r.Error = "DNS: " + $_.Exception.Message; return $r }
    try {
        $c = New-Object Net.Sockets.TcpClient
        $iar = $c.BeginConnect($Servidor, $Puerto, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($MsEspera, $false)) { try { $c.EndConnect($iar); $r.Tcp = $true } catch { $r.Error = "TCP: " + $_.Exception.Message } }
        else { $r.Error = "TCP: tiempo agotado" }
        $c.Close()
    } catch { $r.Error = "TCP: " + $_.Exception.Message }
    return $r
}

function Buscar-Raiz {
    param([string]$Dado)
    $cand = New-Object System.Collections.ArrayList
    if ($Dado) { [void]$cand.Add($Dado) }
    if ($PSScriptRoot) { [void]$cand.Add($PSScriptRoot); [void]$cand.Add((Split-Path -Parent $PSScriptRoot)) }
    [void]$cand.Add((Get-Location).Path)
    if ($env:LOCALAPPDATA) { [void]$cand.Add((Join-Path $env:LOCALAPPDATA "AXIOMA")) }
    if ($env:USERPROFILE) { [void]$cand.Add((Join-Path $env:USERPROFILE ".axioma\app")); [void]$cand.Add((Join-Path $env:USERPROFILE "AXIOMA")) }
    if ($env:HOME -and -not $script:EsWindows) { [void]$cand.Add((Join-Path $env:HOME ".axioma/app")) }
    foreach ($c in $cand) {
        if ($c -and (Test-Path -LiteralPath (Join-Path $c "docker-compose.yml"))) { return (Resolve-Path -LiteralPath $c).Path }
    }
    return ""
}

function Es-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

# ---------------------------------------------------------------------
# 1-3. Contexto, sistema, virtualizacion y WSL
# ---------------------------------------------------------------------
function Recolectar-Contexto {
    Seccion "1. Contexto de la ejecucion"
    Linea ("Script: diagnostico_axioma.ps1 v" + $script:VersionScript)
    Linea ("Fecha local: " + $script:Inicio.ToString("yyyy-MM-dd HH:mm:ss") + " | UTC: " + $script:Inicio.ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss"))
    Linea ("Usuario: " + $env:USERNAME + " | Equipo: " + $env:COMPUTERNAME + " | Dominio: " + $env:USERDOMAIN)
    $admin = Es-Admin
    Linea ("Ejecutando como administrador: " + $admin)
    Hecho "es_admin" $admin
    Linea ("PowerShell: " + $PSVersionTable.PSVersion + " | edicion: " + $PSVersionTable.PSEdition + " | CLR: " + $PSVersionTable.CLRVersion)
    Linea ("Cultura: " + (Get-Culture).Name + " | UI: " + (Get-UICulture).Name)
    Linea ("Opciones: Raiz='" + $Raiz + "' Traza=" + $Traza + " SinRed=" + $SinRed + " TimeoutDocker=" + $TimeoutDocker)
    Intentar "politica" {
        Linea "Politica de ejecucion de scripts (todos los ambitos):"
        Linea ((Get-ExecutionPolicy -List | Format-Table -AutoSize | Out-String).TrimEnd())
        $pol = Get-ExecutionPolicy -List
        foreach ($p in $pol) {
            if (($p.Scope -eq "MachinePolicy" -or $p.Scope -eq "UserPolicy") -and ($p.ExecutionPolicy -eq "Restricted" -or $p.ExecutionPolicy -eq "AllSigned")) {
                Hallazgo "CRITICO" ("Una directiva de grupo (" + $p.Scope + ") fija la politica en " + $p.ExecutionPolicy + ": '-ExecutionPolicy Bypass' NO tiene efecto y los .ps1 no van a correr.") "Hay que pedirle al administrador del equipo que habilite scripts, o instalar en otro equipo."
            }
        }
    }
    if ($script:EsWindows) {
        $r = Ejecutar "codepage de la consola (chcp)" "cmd.exe" "/c chcp" 15
        Hecho "chcp" ($r.Salida.Trim())
    }
}

function Recolectar-Sistema {
    Seccion "2. Sistema operativo y hardware"
    if (-not $script:EsWindows) { Linea "(este equipo no es Windows: se omiten las consultas propias de Windows)"; return }
    Intentar "os" {
        $os = Get-CimInstance Win32_OperatingSystem
        Linea ("Windows: " + $os.Caption + " | version " + $os.Version + " | build " + $os.BuildNumber + " | " + $os.OSArchitecture)
        Linea ("Idioma del SO: " + $os.OSLanguage + " | ultimo arranque: " + $os.LastBootUpTime)
        $build = [int]$os.BuildNumber
        Hecho "windows_build" $build
        Hecho "windows_edicion" $os.Caption
        if ($build -lt 19045) { Hallazgo "AVISO" ("Windows build " + $build + ": Docker Desktop con WSL 2 pide Windows 10 22H2 (build 19045) o Windows 11.") "Actualizar Windows desde Windows Update." }
        if ($os.OSArchitecture -notmatch "64") { Hallazgo "CRITICO" "Windows de 32 bits: Docker Desktop no funciona." "" }
        $libreMB = [math]::Round($os.FreePhysicalMemory / 1024, 0)
        Linea ("Memoria libre ahora: " + $libreMB + " MB")
    }
    Intentar "equipo" {
        $cs = Get-CimInstance Win32_ComputerSystem
        $ramGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
        Linea ("Equipo: " + $cs.Manufacturer + " " + $cs.Model + " | RAM total: " + $ramGB + " GB | hypervisor presente: " + $cs.HypervisorPresent)
        Hecho "ram_gb" $ramGB
        Hecho "hypervisor_presente" $cs.HypervisorPresent
        if ($ramGB -lt 8) { Hallazgo "AVISO" ("RAM total " + $ramGB + " GB: con Docker Desktop (WSL 2) y modelos locales va a quedar muy justo.") "Usar modelos chicos o claves de API en vez de modelos locales." }
        if (($cs.Model -match "Virtual|VMware|KVM|QEMU") -or ($cs.Manufacturer -match "VMware|QEMU|innotek|Xen")) {
            Hecho "es_vm" $true
            Linea "Parece una MAQUINA VIRTUAL: WSL 2 y Docker necesitan virtualizacion anidada activada en el anfitrion."
            Hallazgo "AVISO" "El equipo parece una maquina virtual: sin virtualizacion anidada, Docker Desktop no arranca." "Activar la virtualizacion anidada en el anfitrion (VirtualBox/VMware/Hyper-V/KVM)."
        }
    }
    Intentar "cpu" {
        $cpus = @(Get-CimInstance Win32_Processor)
        foreach ($c in $cpus) {
            Linea ("CPU: " + $c.Name + " | nucleos " + $c.NumberOfCores + " | hilos " + $c.NumberOfLogicalProcessors + " | virtualizacion en firmware: " + $c.VirtualizationFirmwareEnabled + " | SLAT: " + $c.SecondLevelAddressTranslationExtensions)
        }
        $fw = $cpus[0].VirtualizationFirmwareEnabled
        Hecho "virtualizacion_firmware" $fw
        if (($fw -eq $false) -and ($script:Hechos["hypervisor_presente"] -ne $true)) {
            Hallazgo "CRITICO" "La virtualizacion esta DESACTIVADA en la BIOS/UEFI (y Windows no tiene hipervisor activo). WSL 2 y Docker no pueden arrancar." "Entrar a la BIOS/UEFI y activar Intel VT-x / AMD-V (SVM Mode), y reiniciar."
        }
    }
    Intentar "discos" {
        $discos = @(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 3 -or $_.DriveType -eq 4 })
        foreach ($d in $discos) {
            $libre = [math]::Round($d.FreeSpace / 1GB, 1)
            $total = [math]::Round($d.Size / 1GB, 1)
            Linea ("Disco " + $d.DeviceID + " | tipo " + $d.DriveType + " | " + $d.FileSystem + " | libre " + $libre + " GB de " + $total + " GB")
            if ($d.DeviceID -eq $env:SystemDrive) {
                Hecho "disco_sistema_libre_gb" $libre
                if ($libre -lt 10) { Hallazgo "CRITICO" ("Quedan solo " + $libre + " GB libres en " + $d.DeviceID + ": la imagen de AXIOMA y los modelos no entran.") "Liberar espacio (se recomiendan 30 GB o mas)." }
                elseif ($libre -lt 30) { Hallazgo "AVISO" ("Quedan " + $libre + " GB libres en " + $d.DeviceID + ": justo para la imagen y un modelo chico.") "" }
            }
        }
    }
    Intentar "gpu" {
        foreach ($g in @(Get-CimInstance Win32_VideoController)) { Linea ("GPU: " + $g.Name + " | driver " + $g.DriverVersion) }
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { [void](Ejecutar "nvidia-smi" "nvidia-smi" "" 20) } else { Linea "nvidia-smi: no esta instalado (sin GPU NVIDIA o sin driver)" }
    }
    Intentar "reinicio" {
        $pend = @()
        if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") { $pend += "Component Based Servicing" }
        if (Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired") { $pend += "Windows Update" }
        $pfr = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager" -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
        if ($pfr) { $pend += "PendingFileRenameOperations" }
        Linea ("Reinicio pendiente: " + $(if ($pend.Count) { $pend -join ", " } else { "no" }))
        if ($pend.Count) { Hallazgo "AVISO" ("Windows tiene un reinicio pendiente (" + ($pend -join ", ") + "): es causa tipica de que WSL/Docker fallen al instalarse.") "Reiniciar el equipo y volver a intentar." }
    }
    Intentar "rutas largas" {
        $lp = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
        Linea ("Rutas largas habilitadas (LongPathsEnabled): " + $lp)
        Hecho "long_paths" $lp
    }
    Intentar "tiempo" {
        Linea ("Zona horaria: " + (Get-TimeZone).Id)
    }
    Intentar "variables" {
        Linea "Variables de entorno relevantes (los valores de claves se redactan):"
        $vars = Get-ChildItem Env: | Where-Object { $_.Name -match "^(AXIOMA|DOCKER|COMPOSE|OLLAMA|LLM_|WSL|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|ALL_PROXY|TEMP|TMP|USERPROFILE|LOCALAPPDATA|APPDATA|ProgramFiles|PROCESSOR_ARCHITECTURE|SystemDrive)" } | Sort-Object Name
        foreach ($v in $vars) { Linea ("  " + $v.Name + "=" + $v.Value) }
        Linea ("Entradas del PATH con docker/wsl: " + ((($env:Path -split ";") | Where-Object { $_ -match "docker|wsl|ollama" }) -join " ; "))
        Linea ("Largo del PATH: " + $env:Path.Length)
        if ($env:USERNAME -match "[^\x00-\x7F]") { Hallazgo "AVISO" ("El nombre de usuario de Windows tiene caracteres no ASCII ('" + $env:USERNAME + "'): puede romper rutas de Docker/WSL.") "Probar con un usuario de nombre simple." }
    }
    [void](Ejecutar "systeminfo (incluye requisitos de Hyper-V)" "systeminfo.exe" "" 90)
}

function Recolectar-Virtualizacion {
    Seccion "3. Virtualizacion, WSL 2 y servicios"
    if (-not $script:EsWindows) { Linea "(solo Windows)"; return }
    Intentar "wsl" {
        $st = Ejecutar "wsl --status" "wsl.exe" "--status" 30
        [void](Ejecutar "wsl --version" "wsl.exe" "--version" 30)
        $l = Ejecutar "wsl -l -v" "wsl.exe" "-l -v" 30
        Hecho "wsl_status_codigo" $st.Codigo
        if ($st.Timeout -or $l.Timeout) { Hallazgo "CRITICO" "Los comandos de WSL se CUELGAN: el subsistema esta trabado." "Reiniciar el servicio (wsl --shutdown, o reiniciar el equipo)." }
        elseif ($st.NoEncontrado) { Hallazgo "CRITICO" "wsl.exe no existe en este Windows." "Instalar WSL: 'wsl --install' desde una consola de administrador y reiniciar." }
        elseif ($st.Codigo -ne 0) { Hallazgo "AVISO" ("'wsl --status' devolvio el codigo " + $st.Codigo + ": WSL puede no estar instalado o activado.") "Como administrador: 'wsl --install', reiniciar." }
        if ($l.Salida -and ($l.Salida -notmatch "docker-desktop")) { Hallazgo "INFO" "No aparece la distribucion 'docker-desktop' en WSL (normal si Docker Desktop aun no arranco nunca)." "" }
    }
    Intentar "funciones" {
        if (Es-Admin) {
            foreach ($f in @("Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform", "Microsoft-Hyper-V-All", "HypervisorPlatform")) {
                $x = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue
                if ($x) { Linea ("Funcion de Windows " + $f + ": " + $x.State) } else { Linea ("Funcion de Windows " + $f + ": (no disponible en esta edicion)") }
            }
        } else {
            Linea "Funciones de Windows (WSL, VirtualMachinePlatform, Hyper-V): requiere administrador. Volve a ejecutar el diagnostico como administrador para verlas."
            Hallazgo "INFO" "No se pudieron leer las funciones de Windows (falta ejecutar como administrador)." ""
        }
    }
    Intentar "servicios" {
        Linea "Servicios:"
        $sv = Get-Service -Name vmcompute, vmms, LxssManager, WslService, com.docker.service, hns -ErrorAction SilentlyContinue
        if ($sv) { Linea ((($sv | Format-Table Name, Status, StartType -AutoSize) | Out-String).TrimEnd()) } else { Linea "(ninguno de los servicios esperados existe)" }
    }
    Intentar "procesos" {
        Linea "Procesos relevantes:"
        $pr = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "^(docker|com\.docker|Docker Desktop|vmmem|vmwp|wsl|wslhost|wslservice|ollama)" }
        if ($pr) { foreach ($p in $pr) { Linea ("  " + $p.ProcessName + " pid " + $p.Id + " | memoria " + [math]::Round($p.WorkingSet64 / 1MB, 0) + " MB") } } else { Linea "  (ninguno)" }
        $script:Hechos["docker_desktop_corriendo"] = [bool]($pr | Where-Object { $_.ProcessName -match "Docker Desktop" })
    }
    Intentar ".wslconfig" {
        $wc = Join-Path $script:UPROF ".wslconfig"
        if (Test-Path -LiteralPath $wc) { Linea "Contenido de .wslconfig:"; Linea (Get-Content -LiteralPath $wc -Raw) } else { Linea ".wslconfig: no existe (WSL usa sus limites por defecto)" }
    }
}

# ---------------------------------------------------------------------
# 4. Docker
# ---------------------------------------------------------------------
function Recolectar-Docker {
    Seccion "4. Docker Desktop y motor Docker"
    $d = Get-Command docker -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($d) { Linea ("docker: " + $d.Source) } else {
        Linea "docker NO esta en el PATH."
        Hallazgo "CRITICO" "El comando 'docker' no existe en el PATH." "Instalar Docker Desktop y abrir una consola NUEVA (el PATH se actualiza al abrir otra ventana)."
    }
    Hecho "docker_en_path" ([bool]$d)
    $script:DDExe = ""
    Intentar "dd_instalacion" {
        $cands = @((Join-Path $script:PF "Docker\Docker\Docker Desktop.exe"), (Join-Path $script:LAD "Programs\DockerDesktop\Docker Desktop.exe"), (Join-Path $script:LAD "Docker\Docker Desktop.exe"))
        foreach ($x in $cands) {
            if ($x -and (Test-Path -LiteralPath $x)) {
                if (-not $script:DDExe) { $script:DDExe = $x }
                Linea ("Docker Desktop instalado: " + $x + " | version " + (Get-Item -LiteralPath $x).VersionInfo.ProductVersion)
            }
        }
        if (-not $script:DDExe) { Linea "No encontre 'Docker Desktop.exe' en: Program Files\\Docker\\Docker, %LOCALAPPDATA%\\Programs\\DockerDesktop ni %LOCALAPPDATA%\\Docker." }
        Hecho "docker_desktop_exe" $script:DDExe
        if ($script:DDExe -match "AppData") { Linea "(instalacion POR USUARIO: no esta en Program Files; cualquier script que solo busque ahi no lo encuentra)" }
        $reg = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "Docker Desktop*" }
        foreach ($r in $reg) { Linea ("Registro: " + $r.DisplayName + " " + $r.DisplayVersion) }
    }
    if (-not $d) { return }

    [void](Ejecutar "docker --version" "docker" "--version" 20)
    [void](Ejecutar "docker compose version" "docker" "compose version" 30)
    [void](Ejecutar "docker context ls" "docker" "context ls" 30)
    $i = Ejecutar "docker info (resumen)" "docker" 'info --format "{{.OSType}}|{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.OperatingSystem}}|{{.Driver}}|{{.DockerRootDir}}|{{.Name}}"' $TimeoutDocker
    $ok = ((-not $i.Timeout) -and ($i.Codigo -eq 0) -and $i.Salida.Trim())
    Hecho "docker_responde" ([bool]$ok)
    if ($i.Timeout) {
        Hallazgo "CRITICO" ("'docker info' no respondio en " + $TimeoutDocker + " s: Docker Desktop esta a medio arrancar, trabado o WSL esta colgado.") "Cerrar Docker Desktop, 'wsl --shutdown', abrirlo de nuevo y esperar a que diga 'Engine running'."
    } elseif (-not $ok) {
        Hallazgo "CRITICO" ("Docker esta instalado pero NO responde ('docker info' fallo con codigo " + $i.Codigo + "): " + (Recortar ($i.Error + " " + $i.Salida) 300)) "Abrir Docker Desktop y esperar a que diga 'Engine running'. Si no arranca, revisar la seccion de WSL y los registros de Docker Desktop."
    } else {
        $p = $i.Salida.Trim().Split("|")
        Linea ("Motor Docker: OSType=" + $p[0] + " | version " + $p[1] + " | CPUs " + $p[2] + " | memoria " + [math]::Round(([double]$p[3]) / 1GB, 1) + " GB | " + $p[4] + " | driver " + $p[5] + " | raiz " + $p[6])
        Hecho "docker_ostype" $p[0]
        Hecho "docker_memoria_gb" ([math]::Round(([double]$p[3]) / 1GB, 1))
        if ($p[0] -ne "linux") { Hallazgo "CRITICO" ("Docker Desktop esta en modo de contenedores '" + $p[0] + "': AXIOMA necesita contenedores Linux.") "Clic derecho en el icono de Docker -> 'Switch to Linux containers'." }
        $memGB = [double]$p[3] / 1GB
        if ($memGB -lt 6) { Hallazgo "AVISO" ("A Docker le tocan solo " + [math]::Round($memGB, 1) + " GB de RAM: la compilacion de la imagen y el modelo pueden morir por falta de memoria.") "Docker Desktop -> Settings -> Resources (o .wslconfig) para subir el limite." }
        [void](Ejecutar "docker info (completo)" "docker" "info" $TimeoutDocker)
        [void](Ejecutar "docker version" "docker" "version" $TimeoutDocker)
        [void](Ejecutar "docker system df" "docker" "system df" $TimeoutDocker)
        [void](Ejecutar "docker images" "docker" "images" $TimeoutDocker)
        [void](Ejecutar "docker volume ls" "docker" "volume ls" $TimeoutDocker)
        [void](Ejecutar "docker network ls" "docker" "network ls" $TimeoutDocker)
    }
    Intentar "dd_config" {
        foreach ($f in @((Join-Path $script:APPD "Docker\settings-store.json"), (Join-Path $script:APPD "Docker\settings.json"))) {
            if (Test-Path -LiteralPath $f) { Linea ("--- " + $f); Linea (Recortar (Get-Content -LiteralPath $f -Raw) 6000); Linea "" }
        }
    }
    Intentar "dd_autostart" {
        $sf = Join-Path $script:APPD "Docker\settings-store.json"
        if (-not (Test-Path -LiteralPath $sf)) { $sf = Join-Path $script:APPD "Docker\settings.json" }
        if (Test-Path -LiteralPath $sf) {
            $cfg = Get-Content -LiteralPath $sf -Raw | ConvertFrom-Json
            $auto = $cfg.AutoStart
            if ($null -eq $auto) { $auto = $cfg.autoStart }
            Linea ("Docker Desktop arranca solo con Windows (AutoStart): " + $auto)
            Hecho "docker_autostart" $auto
            if ($auto -eq $false) { Hallazgo "AVISO" "Docker Desktop NO arranca solo al iniciar Windows (AutoStart desactivado): despues de cada reinicio, AXIOMA no arranca hasta que se abra Docker Desktop a mano." "Docker Desktop -> Settings -> General -> 'Start Docker Desktop when you sign in', o que el script de AXIOMA lo abra por vos." }
        }
        if (($script:Hechos["docker_responde"] -ne $true) -and $script:DDExe -and ($script:Hechos["docker_desktop_corriendo"] -ne $true)) {
            Hallazgo "CRITICO" ("Docker Desktop esta instalado (" + $script:DDExe + ") pero esta CERRADO: por eso 'docker info' no encuentra el motor.") "Abrir Docker Desktop y esperar a 'Engine running'. Si los instaladores de AXIOMA no lo abren solos, no estan buscando en esta ruta."
        }
    }
    Intentar "dd_logs" {
        foreach ($dir in @((Join-Path $script:LAD "Docker\log\host"), (Join-Path $script:LAD "Docker\log\vm"))) {
            if (Test-Path -LiteralPath $dir) {
                $ult = Get-ChildItem -LiteralPath $dir -Filter *.log -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3
                foreach ($u in $ult) { Cola-De-Archivo $u.FullName 80 }
            }
        }
    }
    Intentar "dd_disco" {
        $base = Join-Path $script:LAD "Docker\wsl"
        if (Test-Path -LiteralPath $base) {
            foreach ($v in (Get-ChildItem -LiteralPath $base -Recurse -Filter *.vhdx -ErrorAction SilentlyContinue)) { Linea ("Disco virtual de Docker: " + $v.FullName + " | " + [math]::Round($v.Length / 1GB, 1) + " GB") }
        }
    }
}

# ---------------------------------------------------------------------
# 5-6. Puertos y red
# ---------------------------------------------------------------------
function Recolectar-Puertos {
    Seccion "5. Puertos locales (8080 = AXIOMA, 11434 = Ollama)"
    foreach ($pt in @(8080, 11434)) {
        $dueno = ""
        $enUso = $false
        if ($script:EsWindows) {
            try {
                $c = @(Get-NetTCPConnection -State Listen -LocalPort $pt -ErrorAction SilentlyContinue)
                if ($c.Count -gt 0) {
                    $enUso = $true
                    $pr = Get-Process -Id $c[0].OwningProcess -ErrorAction SilentlyContinue
                    if ($pr) { $dueno = $pr.ProcessName + " (pid " + $pr.Id + ")" } else { $dueno = "pid " + $c[0].OwningProcess }
                }
            } catch { Linea ("(no pude consultar el puerto " + $pt + ": " + $_.Exception.Message + ")") }
        } else {
            try {
                $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $pt)
                $l.Start(); $l.Stop()
            } catch { $enUso = $true }
        }
        Linea ("Puerto " + $pt + ": " + $(if ($enUso) { "EN USO por " + $(if ($dueno) { $dueno } else { "(proceso desconocido)" }) } else { "libre" }))
        Hecho ("puerto_" + $pt + "_dueno") $dueno
        if ($pt -eq 8080 -and $enUso -and ($dueno -notmatch "docker|vmmem|wslrelay|wslhost|AXIOMA|python")) {
            Hallazgo "AVISO" ("El puerto 8080 lo esta usando otro programa: " + $(if ($dueno) { $dueno } else { "(proceso desconocido: ejecutar como administrador para verlo)" }) + ". Si AXIOMA no esta corriendo, no va a poder abrir su pantalla.") "Cerrar ese programa o cambiar el puerto de AXIOMA."
        }
    }
}

function Recolectar-Red {
    Seccion "6. Red, proxy, reloj y servidores necesarios"
    Intentar "proxy" {
        if ($script:EsWindows) {
            [void](Ejecutar "netsh winhttp show proxy" "netsh.exe" "winhttp show proxy" 20)
            $ie = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
            if ($ie) {
                Linea ("Proxy de usuario: ProxyEnable=" + $ie.ProxyEnable + " ProxyServer=" + $ie.ProxyServer + " AutoConfigURL=" + $ie.AutoConfigURL)
                if (($ie.ProxyEnable -eq 1) -or $ie.AutoConfigURL) { Hallazgo "AVISO" "Hay un proxy configurado en Windows: Docker y pip pueden necesitar configurarlo para bajar la imagen y los paquetes." "Configurar el proxy en Docker Desktop -> Settings -> Resources -> Proxies." }
            }
            $dns = Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.ServerAddresses }
            foreach ($x in $dns) { Linea ("DNS " + $x.InterfaceAlias + ": " + ($x.ServerAddresses -join ", ")) }
        }
        if ($env:HTTP_PROXY -or $env:HTTPS_PROXY) { Hallazgo "AVISO" "Hay variables HTTP_PROXY/HTTPS_PROXY definidas." "" }
    }
    Intentar "reloj" {
        $w = Invoke-WebRequest -Uri "https://github.com" -Method Head -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop
        $fecha = [DateTime]::Parse($w.Headers["Date"]).ToUniversalTime()
        $desfase = [math]::Round(([DateTime]::UtcNow - $fecha).TotalSeconds, 0)
        Linea ("Reloj del equipo contra GitHub: desfase " + $desfase + " s")
        Hecho "desfase_reloj_s" $desfase
        if ([math]::Abs($desfase) -gt 300) {
            $resto = [math]::Abs($desfase) % 3600
            $pista = ""
            if (($resto -lt 120) -or ($resto -gt 3480)) { $pista = " El desfase es casi un multiplo exacto de horas: suele ser la zona horaria mal puesta o el reloj de hardware en UTC (doble arranque con Linux)." }
            Hallazgo "AVISO" ("La hora del equipo difiere " + $desfase + " s de la de GitHub (si pasan de unos dias, fallan los certificados HTTPS; con horas no suele pasar)." + $pista) "Verificar la hora real y activar 'Establecer la hora automaticamente' en Windows."
        }
    }
    $criticos = @("github.com", "codeload.github.com", "registry-1.docker.io", "auth.docker.io", "pypi.org", "files.pythonhosted.org")
    $hosts = @("github.com", "codeload.github.com", "ghcr.io", "registry-1.docker.io", "auth.docker.io", "production.cloudflare.docker.com", "pypi.org", "files.pythonhosted.org", "registry.ollama.ai", "huggingface.co")
    Linea ""
    Linea ("{0,-36} {1,-6} {2,-6} {3}" -f "Servidor", "TCP443", "HTTPS", "Detalle")
    foreach ($h in $hosts) {
        Write-Host ("    - red: " + $h) -ForegroundColor DarkGray
        $pp = Probar-Puerto $h 443 5000
        $web = $null
        if ($pp.Tcp) { $web = Probar-Url ("https://" + $h + "/") 10 200 "HEAD" }
        $httpTxt = "-"
        if ($web) { if ($web.Estado) { $httpTxt = [string]$web.Estado } else { $httpTxt = "error" } }
        $detalle = $pp.Dns
        if ($pp.Error) { $detalle = $pp.Error }
        elseif ($web -and -not $web.Estado) { $detalle = $web.Error }
        Linea ("{0,-36} {1,-6} {2,-6} {3}" -f $h, $(if ($pp.Tcp) { "ok" } else { "FALLA" }), $httpTxt, $detalle)
        if ((-not $pp.Tcp) -or ($web -and -not $web.Estado)) {
            if ($criticos -contains $h) { Hallazgo "CRITICO" ("No hay conexion HTTPS con " + $h + " (" + $detalle + ").") "Revisar internet, proxy, firewall o antivirus: sin este servidor no se puede descargar AXIOMA o compilar la imagen." }
            else { Hallazgo "AVISO" ("No hay conexion HTTPS con " + $h + " (" + $detalle + ").") "" }
        }
    }
}

# ---------------------------------------------------------------------
# 7. Proyecto
# ---------------------------------------------------------------------
function Recolectar-Proyecto {
    Seccion "7. Carpeta del proyecto AXIOMA"
    if (-not $Raiz) {
        Linea "No encontre la carpeta de AXIOMA (busque junto al script, en la carpeta actual y en las rutas habituales)."
        Hallazgo "INFO" "No se encontro la carpeta de AXIOMA: solo se hicieron las pruebas del sistema." "Volver a correr con -Raiz C:\ruta\a\AXIOMA para analizarla."
        return
    }
    Linea ("Raiz: " + $Raiz + "  (largo " + $Raiz.Length + ")")
    Intentar "ruta" {
        if ($Raiz -match "\s") { Hallazgo "INFO" "La ruta de AXIOMA tiene espacios. No es un error en si, pero si falla el montaje de carpetas en Docker, probar con una ruta simple (por ejemplo C:\AXIOMA)." "" }
        if ($Raiz -match "[^\x00-\x7F]") { Hallazgo "AVISO" "La ruta de AXIOMA tiene caracteres no ASCII (acentos, enie): Docker/WSL pueden fallar al montar carpetas." "Mover AXIOMA a una ruta simple, por ejemplo C:\AXIOMA." }
        if ($Raiz -match "OneDrive|Dropbox|Google Drive|iCloud") { Hallazgo "AVISO" "AXIOMA esta dentro de una carpeta sincronizada en la nube: los archivos 'solo en la nube' y los bloqueos de sincronizacion rompen montajes y escrituras." "Mover AXIOMA a una carpeta local fuera de la nube." }
        if ($Raiz.StartsWith("\\")) { Hallazgo "AVISO" "AXIOMA esta en una ruta de red (UNC): Docker no monta bien esas rutas." "" }
        if ($Raiz.Length -gt 120 -and $script:Hechos["long_paths"] -ne 1) { Hallazgo "AVISO" ("La ruta es larga (" + $Raiz.Length + ") y las rutas largas de Windows no estan habilitadas.") "" }
        try {
            $di = New-Object System.IO.DriveInfo([IO.Path]::GetPathRoot($Raiz))
            Linea ("Unidad: " + $di.Name + " | formato " + $di.DriveFormat + " | tipo " + $di.DriveType + " | libre " + [math]::Round($di.AvailableFreeSpace / 1GB, 1) + " GB")
            if ($script:EsWindows -and $di.DriveFormat -ne "NTFS") { Hallazgo "AVISO" ("AXIOMA esta en una unidad " + $di.DriveFormat + " (no NTFS): los permisos y montajes de Docker pueden fallar.") "" }
            if ($di.DriveType -eq "Network") { Hallazgo "AVISO" "AXIOMA esta en una unidad de red." "" }
        } catch { Linea ("(no pude leer la unidad: " + $_.Exception.Message + ")") }
    }
    Intentar "contenido" {
        Linea "Contenido de la raiz:"
        foreach ($i in (Get-ChildItem -LiteralPath $Raiz -Force | Sort-Object { -not $_.PSIsContainer }, Name)) {
            Linea ("  " + $(if ($i.PSIsContainer) { "[carpeta] " } else { "[archivo] " }) + $i.Name + $(if (-not $i.PSIsContainer) { "  (" + $i.Length + " bytes)" } else { "" }))
        }
        $v = Join-Path $Raiz "src\__init__.py"
        if (Test-Path -LiteralPath $v) { $m = Select-String -LiteralPath $v -Pattern "__version__" | Select-Object -First 1; if ($m) { Linea ("Version: " + $m.Line.Trim()) } }
        foreach ($rel in @("docker-compose.yml", "Dockerfile", ".env.example", ".dockerignore", "config\models.yaml", "config\model_catalog.yaml", "requirements-ci.txt", "instalar\instalar_axioma.ps1", "instalar\iniciar_axioma.ps1")) {
            if (-not (Test-Path -LiteralPath (Join-Path $Raiz $rel))) { Hallazgo "CRITICO" ("Falta el archivo " + $rel + " en la carpeta de AXIOMA.") "Volver a descargar AXIOMA completo (la descarga pudo quedar incompleta o un antivirus borro archivos)." ; Linea ("FALTA: " + $rel) }
        }
    }
    Intentar "env" {
        $envp = Join-Path $Raiz ".env"
        if (Test-Path -LiteralPath $envp -PathType Container) {
            $n = @(Get-ChildItem -LiteralPath $envp -Force -ErrorAction SilentlyContinue).Count
            Linea (".env: ES UNA CARPETA (con " + $n + " elementos). Docker la creo al montar un archivo que no existia.")
            Hallazgo "CRITICO" ".env es una CARPETA, no un archivo: AXIOMA no puede leer ni guardar claves ni modelos." "Cerrar AXIOMA ('docker compose down'), borrar la carpeta .env y volver a ejecutar el instalador."
        } elseif (Test-Path -LiteralPath $envp -PathType Leaf) {
            $b = [IO.File]::ReadAllBytes($envp)
            $bom = ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
            $crlf = 0; $lf = 0
            for ($k = 0; $k -lt $b.Length; $k++) { if ($b[$k] -eq 10) { if ($k -gt 0 -and $b[$k - 1] -eq 13) { $crlf++ } else { $lf++ } } }
            Linea (".env: archivo de " + $b.Length + " bytes | BOM UTF-8: " + $bom + " | lineas CRLF: " + $crlf + " | lineas LF: " + $lf)
            if ($bom) { Hallazgo "AVISO" "El .env tiene BOM UTF-8: la primera variable puede leerse con un nombre roto." "Guardarlo como UTF-8 sin BOM." }
            $vistos = @{}
            $nombres = New-Object System.Collections.ArrayList
            $texto = [Text.Encoding]::UTF8.GetString($b).TrimStart([char]0xFEFF)
            $nl = 0
            foreach ($ln in ($texto -split "\r?\n")) {
                $nl++
                $t = $ln.Trim()
                if (-not $t -or $t.StartsWith("#")) { continue }
                if ($ln -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
                    $nom = $Matches[1]; $val = $Matches[2].Trim()
                    [void]$nombres.Add($nom)
                    if ($vistos.ContainsKey($nom)) { Linea ("  [!] variable duplicada: " + $nom + " (lineas " + $vistos[$nom] + " y " + $nl + "; gana la ultima)") ; $vistos[$nom] = [string]$vistos[$nom] + "," + $nl } else { $vistos[$nom] = [string]$nl }
                    if ($nom -match "KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL") {
                        Linea ("  " + $nom + " = " + $(if ($val) { "<definida, " + $val.Length + " caracteres>" } else { "<vacia>" }))
                    } else {
                        if ($val.Length -gt 140) { $val = $val.Substring(0, 140) + "..." }
                        Linea ("  " + $nom + " = " + $val)
                    }
                    if ($val.StartsWith('"') -or $val.StartsWith("'")) { Linea ("  [!] " + $nom + ": el valor lleva comillas (puede quedar con las comillas incluidas)") }
                } else {
                    Linea ("  [!] linea " + $nl + " con formato raro (no es VARIABLE=valor): '" + (Recortar $t 60) + "'")
                }
            }
            $ej = Join-Path $Raiz ".env.example"
            if (Test-Path -LiteralPath $ej) {
                $plantilla = @()
                foreach ($ln in (Get-Content -LiteralPath $ej)) { if ($ln -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=") { $plantilla += $Matches[1] } }
                $faltan = @($plantilla | Where-Object { $nombres -notcontains $_ })
                Linea ("Variables de la plantilla ausentes en tu .env: " + $faltan.Count + $(if ($faltan.Count) { " -> " + (($faltan | Select-Object -First 25) -join ", ") } else { "" }))
            }
        } else {
            Linea ".env: NO existe todavia."
            Hallazgo "INFO" "No existe el archivo .env (el instalador lo crea desde .env.example)." ""
        }
    }
    Intentar "carpetas" {
        foreach ($c in @("data", "logs", "cache")) {
            $p = Join-Path $Raiz $c
            Linea ($c + ": " + $(if (Test-Path -LiteralPath $p -PathType Container) { "existe" } elseif (Test-Path -LiteralPath $p) { "EXISTE PERO NO ES CARPETA" } else { "no existe" }))
        }
        if ($script:EsWindows) {
            foreach ($c in @(".env", "data", "logs", "cache")) {
                $p = Join-Path $Raiz $c
                if (Test-Path -LiteralPath $p) { [void](Ejecutar ("permisos de " + $c) "icacls.exe" ('"' + $p + '"') 20) }
            }
        }
    }
    Intentar "scripts" {
        foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $Raiz "instalar") -File -ErrorAction SilentlyContinue)) {
            $b = [IO.File]::ReadAllBytes($f.FullName)
            $bom = ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)
            $no7 = 0; $lfSolo = 0; $crlf = 0
            for ($k = 0; $k -lt $b.Length; $k++) { if ($b[$k] -gt 127) { $no7++ }; if ($b[$k] -eq 10) { if ($k -gt 0 -and $b[$k - 1] -eq 13) { $crlf++ } else { $lfSolo++ } } }
            $zona = $false
            if ($script:EsWindows) { try { $zona = [bool](Get-Item -LiteralPath $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue) } catch { } }
            Linea ("  " + $f.Name + " | " + $b.Length + " bytes | BOM " + $bom + " | bytes no ASCII " + $no7 + " | CRLF " + $crlf + " | solo-LF " + $lfSolo + " | bloqueado por 'Mark of the Web': " + $zona)
            if ($f.Extension -eq ".ps1" -and (-not $bom) -and ($no7 -gt 0)) { Hallazgo "AVISO" ($f.Name + " tiene caracteres no ASCII y NO tiene BOM: Windows PowerShell 5.1 los lee mal.") "" }
            if ($f.Extension -eq ".bat" -and $lfSolo -gt 0) { Hallazgo "AVISO" ($f.Name + " tiene finales de linea solo-LF: cmd.exe puede fallar al interpretarlo.") "Guardarlo con finales de linea CRLF." }
            if ($f.Extension -eq ".bat" -and $no7 -gt 0) { Hallazgo "INFO" ($f.Name + " tiene caracteres no ASCII (cmd.exe los muestra o interpreta segun la pagina de codigos).") "" }
            if ($zona) { Hallazgo "INFO" ($f.Name + " esta marcado como descargado de internet (Mark of the Web): SmartScreen/politicas pueden bloquearlo.") "Clic derecho -> Propiedades -> Desbloquear, o Unblock-File." }
        }
    }
}

# ---------------------------------------------------------------------
# 8. Accesos directos
# ---------------------------------------------------------------------
function Recolectar-Accesos {
    Seccion "8. Accesos directos de AXIOMA"
    if (-not $script:EsWindows) { Linea "(solo Windows)"; return }
    Intentar "accesos" {
        $carpetas = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"), [Environment]::GetFolderPath("Programs"), (Join-Path $script:UPROF "Desktop"))
        if ($env:OneDrive) { $carpetas += (Join-Path $env:OneDrive "Desktop"); $carpetas += (Join-Path $env:OneDrive "Escritorio") }
        $vistos = @{}
        $sh = New-Object -ComObject WScript.Shell
        foreach ($c in ($carpetas | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)) {
            foreach ($f in @(Get-ChildItem -LiteralPath $c -Filter "*AXIOMA*.lnk" -Recurse -ErrorAction SilentlyContinue)) {
                if ($vistos.ContainsKey($f.FullName)) { continue }
                $vistos[$f.FullName] = $true
                $l = $sh.CreateShortcut($f.FullName)
                Linea ("Acceso: " + $f.FullName)
                Linea ("   destino:   " + $l.TargetPath + "  (existe: " + (Test-Path -LiteralPath $l.TargetPath) + ")")
                Linea ("   argumentos:" + $l.Arguments)
                Linea ("   carpeta de trabajo: " + $l.WorkingDirectory + "  (existe: " + $(if ($l.WorkingDirectory) { Test-Path -LiteralPath $l.WorkingDirectory } else { "n/a" }) + ")")
                if (($l.Arguments -match "iniciar_axioma\.ps1") -and ($l.Arguments -notmatch "\.bat")) {
                    Hallazgo "AVISO" ("El acceso directo '" + $f.Name + "' ejecuta el .ps1 directo, sin pausa final: si AXIOMA no arranca, la ventana se cierra sola y no se ve el error.") "Apuntar el acceso directo a INICIAR_AXIOMA.bat (que si hace pausa) o agregar una pausa en caso de error."
                }
                if ($l.TargetPath -and -not (Test-Path -LiteralPath $l.TargetPath)) { Hallazgo "AVISO" ("El acceso directo '" + $f.Name + "' apunta a un destino que no existe: " + $l.TargetPath) "" }
            }
        }
        if ($vistos.Count -eq 0) { Linea "(no hay accesos directos de AXIOMA en el Escritorio ni en el menu Inicio)" }
    }
}

# ---------------------------------------------------------------------
# 9. Estado de AXIOMA en ejecucion (contenedores y respuestas locales)
# ---------------------------------------------------------------------
function Recolectar-Estado {
    param([string]$Etiqueta = "")
    Seccion ("9. Estado de AXIOMA en ejecucion" + $Etiqueta)
    $dockerOk = ($script:Hechos["docker_responde"] -eq $true)
    if (-not $dockerOk) {
        Linea "Docker no responde: se omiten las consultas de contenedores."
    } else {
        [void](Ejecutar "docker ps -a" "docker" 'ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"' $TimeoutDocker)
        if ($Raiz -and (Test-Path -LiteralPath (Join-Path $Raiz "docker-compose.yml"))) {
            $cfg = Ejecutar "docker compose config (solo valida, no muestra valores)" "docker" "compose config -q" 60 $Raiz
            if (($cfg.Codigo -ne 0) -and (-not $cfg.Timeout)) { Hallazgo "CRITICO" ("'docker compose config' falla: " + (Recortar ($cfg.Error + " " + $cfg.Salida) 300)) "Es el mismo error que va a dar al encender AXIOMA: suele ser un .env que es carpeta, un YAML danado o una variable faltante." }
            [void](Ejecutar "docker compose ps -a" "docker" "compose ps -a" 60 $Raiz)
            [void](Ejecutar "docker compose logs (ultimas 200 lineas)" "docker" "compose logs --no-color --tail 200" 90 $Raiz)
        }
        $nombres = @()
        $lst = Ejecutar "nombres de contenedores" "docker" 'ps -a --format "{{.Names}}"' 30 "" -Silencioso
        if ($lst.Salida) { $nombres = @($lst.Salida -split "\r?\n" | Where-Object { $_.Trim() -match "axioma|ollama" } | ForEach-Object { $_.Trim() }) }
        if ($nombres.Count -eq 0) { Linea "No hay contenedores de AXIOMA creados." }
        foreach ($n in $nombres) {
            Linea ("----- contenedor " + $n + " -----")
            $est = Ejecutar ("estado de " + $n) "docker" ('inspect --format "{{json .State}}" ' + $n) 30
            [void](Ejecutar ("montajes de " + $n) "docker" ('inspect --format "{{json .Mounts}}" ' + $n) 30)
            [void](Ejecutar ("configuracion de " + $n) "docker" ('inspect --format "imagen={{.Config.Image}} | reinicio={{.HostConfig.RestartPolicy.Name}} | red={{.HostConfig.NetworkMode}} | usuario={{.Config.User}}" ' + $n) 30)
            $corriendo = $false
            try {
                $st = $est.Salida | ConvertFrom-Json
                $corriendo = ($st.Status -eq "running")
                if ($st.OOMKilled) { Hallazgo "CRITICO" ("El contenedor " + $n + " fue matado por falta de memoria (OOMKilled).") "Subir la memoria de Docker/WSL o usar modelos mas chicos." }
                if (($st.Status -eq "exited") -and ($st.ExitCode -ne 0)) { Hallazgo "CRITICO" ("El contenedor " + $n + " se detuvo con codigo " + $st.ExitCode + ". " + $st.Error) "Ver los registros del contenedor en este informe." }
                if ($st.Status -eq "restarting") { Hallazgo "CRITICO" ("El contenedor " + $n + " se reinicia en bucle.") "Ver los registros del contenedor en este informe." }
                if ($st.Health -and $st.Health.Status -eq "unhealthy") { Hallazgo "CRITICO" ("El contenedor " + $n + " esta 'unhealthy'.") "Ver los registros y la prueba de salud en este informe." }
            } catch { Linea ("(no pude interpretar el estado de " + $n + ")") }
            [void](Ejecutar ("registros de " + $n + " (ultimas 200 lineas)") "docker" ('logs --tail 200 ' + $n) 60)
            if ($corriendo -and ($n -match "axioma")) {
                [void](Ejecutar ("permisos y red desde adentro de " + $n) "docker" ('exec ' + $n + ' sh -c "id; ls -ld /app/.env /app/data /app/logs /app/cache; df -h /app/data | tail -n 1; getent hosts host.docker.internal"') 60)
                [void](Ejecutar ("version de Python en " + $n) "docker" ('exec ' + $n + ' python --version') 30)
                $ol = Ejecutar ("alcance de Ollama del host desde " + $n) "docker" ('exec ' + $n + ' python -c "import urllib.request as u; print(u.urlopen(''http://host.docker.internal:11434/api/version'', timeout=5).read())"') 40
                if (($ol.Codigo -ne 0) -and (-not $ol.Timeout) -and ($script:Hechos["ollama_local_responde"] -eq $true)) { Hallazgo "AVISO" ("Ollama responde en el host, pero el contenedor " + $n + " no lo alcanza (host.docker.internal:11434).") "Revisar OLLAMA_HOST y que Ollama escuche en 0.0.0.0 (OLLAMA_HOST=0.0.0.0)." }
            }
        }
        [void](Ejecutar "docker stats (una muestra)" "docker" 'stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"' 60)
    }
    Linea ""
    Linea "Respuestas locales (solo lecturas GET):"
    $axiOk = $false
    foreach ($u in @("http://127.0.0.1:8080/", "http://127.0.0.1:8080/api/v1/health", "http://127.0.0.1:8080/api/api/v1/health", "http://127.0.0.1:8080/v1/health")) {
        $r = Probar-Url $u 8 600
        Linea ("  " + $u + " -> " + $(if ($r.Estado) { "HTTP " + $r.Estado } else { "sin respuesta (" + $r.Error + ")" }) + " | " + $r.Ms + " ms")
        if ($r.Cuerpo) { Linea ("     " + (($r.Cuerpo -replace "\s+", " ").Trim())) }
        if ($r.Estado -eq 200) { $axiOk = $true }
    }
    Hecho "axioma_responde" $axiOk
    $ollOk = $false
    foreach ($u in @("http://127.0.0.1:11434/api/version", "http://127.0.0.1:11434/api/tags")) {
        $r = Probar-Url $u 8 3000
        Linea ("  " + $u + " -> " + $(if ($r.Estado) { "HTTP " + $r.Estado } else { "sin respuesta (" + $r.Error + ")" }) + " | " + $r.Ms + " ms")
        if ($r.Estado -eq 200) {
            $ollOk = $true
            if ($u -match "tags") { try { $m = ($r.Cuerpo | ConvertFrom-Json).models; Linea ("     modelos instalados en Ollama: " + (($m | ForEach-Object { $_.name }) -join ", ")) } catch { Linea ("     " + (($r.Cuerpo -replace "\s+", " ").Trim())) } }
            else { Linea ("     " + (($r.Cuerpo -replace "\s+", " ").Trim())) }
        }
    }
    Hecho "ollama_local_responde" $ollOk
    if (($script:Tareas | Where-Object { $_.Comando -match "inspect --format ""\{\{json \.State\}\}""" }) -and (-not $axiOk)) {
        Hallazgo "AVISO" "Hay contenedores de AXIOMA pero la pantalla (http://127.0.0.1:8080) no responde." "Ver los registros del contenedor en este informe."
    }
}

# ---------------------------------------------------------------------
# 10. Seguridad: antivirus, bloqueos y el instalador .exe
# ---------------------------------------------------------------------
function Recolectar-Seguridad {
    Seccion "10. Antivirus, bloqueos y instalador .exe"
    if (-not $script:EsWindows) { Linea "(solo Windows)"; return }
    Intentar "defender" {
        $mp = Get-MpComputerStatus -ErrorAction Stop
        Linea ("Microsoft Defender: antivirus " + $mp.AntivirusEnabled + " | proteccion en tiempo real " + $mp.RealTimeProtectionEnabled + " | firmas " + $mp.AntivirusSignatureVersion)
    }
    Intentar "av" {
        $av = Get-CimInstance -Namespace "root/SecurityCenter2" -ClassName AntiVirusProduct -ErrorAction SilentlyContinue
        foreach ($a in $av) { Linea ("Antivirus registrado: " + $a.displayName) }
    }
    Intentar "detecciones" {
        $det = Get-MpThreatDetection -ErrorAction SilentlyContinue | Sort-Object InitialDetectionTime -Descending | Select-Object -First 15
        if ($det) {
            Linea "Detecciones recientes de Defender:"
            foreach ($d in $det) {
                Linea ("  " + $d.InitialDetectionTime + " | amenaza " + $d.ThreatID + " | " + (($d.Resources) -join "; "))
                if ((($d.Resources) -join ";") -match "AXIOMA|axioma|docker|Docker") { Hallazgo "CRITICO" ("Defender detecto/cuarentena algo de AXIOMA o Docker: " + (($d.Resources) -join "; ")) "Restaurar el archivo desde 'Seguridad de Windows -> Proteccion contra virus y amenazas -> Historial de proteccion' y agregar una exclusion." }
            }
        } else { Linea "Detecciones de Defender: ninguna (o se necesita administrador para verlas)." }
    }
    Intentar "setup_exe" {
        $buscar = @()
        if ($PSScriptRoot) { $buscar += $PSScriptRoot }
        $buscar += [Environment]::GetFolderPath("Desktop")
        $buscar += (Join-Path $script:UPROF "Downloads")
        $buscar += (Get-Location).Path
        $vistos = @{}
        foreach ($c in ($buscar | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)) {
            foreach ($f in @(Get-ChildItem -LiteralPath $c -Filter "AXIOMA-Setup*.exe" -File -ErrorAction SilentlyContinue)) {
                if ($vistos.ContainsKey($f.FullName)) { continue }
                $vistos[$f.FullName] = $true
                $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
                $sig = Get-AuthenticodeSignature -LiteralPath $f.FullName
                $zona = [bool](Get-Item -LiteralPath $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue)
                Linea ("Instalador: " + $f.FullName + " | " + [math]::Round($f.Length / 1MB, 1) + " MB | SHA256 " + $h)
                Linea ("   firma: " + $sig.Status + " | firmante: " + $(if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { "(sin firma)" }) + " | marcado como descargado de internet: " + $zona)
                if ($sig.Status -eq "NotSigned") { Hallazgo "INFO" ($f.Name + " no esta firmado: SmartScreen y algunos antivirus van a mostrar una advertencia ('Windows protegio su PC').") "Es esperable sin certificado de firma de codigo. En SmartScreen: 'Mas informacion' -> 'Ejecutar de todas formas'." }
                $d = Ejecutar ("instalador .exe en modo ensayo (" + $f.Name + ")") "cmd.exe" ('/c ""' + $f.FullName + '" --dry-run < nul"') 120
                if ($d.Timeout) { Hallazgo "CRITICO" ($f.Name + " --dry-run se colgo.") "" }
                elseif ($d.NoEncontrado -or ($null -eq $d.Codigo)) { Hallazgo "CRITICO" ($f.Name + " no se pudo ejecutar: " + $d.Error) "Probablemente lo bloquea el antivirus o SmartScreen." }
            }
        }
        if ($vistos.Count -eq 0) { Linea "(no encontre AXIOMA-Setup*.exe junto al script, en el Escritorio ni en Descargas)" }
    }
}

# ---------------------------------------------------------------------
# 11. Eventos de Windows
# ---------------------------------------------------------------------
function Recolectar-Eventos {
    Seccion "11. Eventos de Windows de los ultimos 3 dias (Docker, WSL, Hyper-V, AXIOMA)"
    if (-not $script:EsWindows) { Linea "(solo Windows)"; return }
    $desde = (Get-Date).AddDays(-3)
    Intentar "eventos_app" {
        $ev = @(Get-WinEvent -FilterHashtable @{ LogName = "Application"; Level = 1, 2, 3; StartTime = $desde } -MaxEvents 500 -ErrorAction SilentlyContinue | Where-Object { ($_.ProviderName -match "Docker|WSL|Lxss|Hyper-V|AXIOMA|python") -or ($_.Message -match "docker|axioma|wsl") } | Select-Object -First 40)
        Linea ("Registro Application: " + $ev.Count + " eventos relevantes")
        foreach ($e in $ev) { Linea ("  " + $e.TimeCreated + " | " + $e.LevelDisplayName + " | " + $e.ProviderName + " | " + (Recortar (($e.Message -replace "\s+", " ")) 400)) }
    }
    Intentar "eventos_sistema" {
        $ev = @(Get-WinEvent -FilterHashtable @{ LogName = "System"; Level = 1, 2, 3; StartTime = $desde } -MaxEvents 500 -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match "Hyper-V|Lxss|WSL|vmcompute|hns|Docker" } | Select-Object -First 40)
        Linea ("Registro System: " + $ev.Count + " eventos relevantes")
        foreach ($e in $ev) { Linea ("  " + $e.TimeCreated + " | " + $e.LevelDisplayName + " | " + $e.ProviderName + " | " + (Recortar (($e.Message -replace "\s+", " ")) 400)) }
    }
    Intentar "eventos_hyperv" {
        $ev = @(Get-WinEvent -LogName "Microsoft-Windows-Hyper-V-Compute-Admin" -MaxEvents 30 -ErrorAction SilentlyContinue | Where-Object { $_.TimeCreated -gt $desde -and $_.LevelDisplayName -match "Error|Critical|Warning" } | Select-Object -First 20)
        Linea ("Hyper-V Compute (Admin): " + $ev.Count + " eventos de error/aviso")
        foreach ($e in $ev) { Linea ("  " + $e.TimeCreated + " | " + $e.LevelDisplayName + " | " + (Recortar (($e.Message -replace "\s+", " ")) 400)) }
    }
}

# ---------------------------------------------------------------------
# 12. Ensayo de los instaladores (muestra errores que se tragan en silencio)
# ---------------------------------------------------------------------
function Obtener-PowerShellExe {
    $c = Get-Command powershell.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    $c = Get-Command pwsh -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    return ""
}

function Ensayar-Instaladores {
    Seccion "12. Ensayo de los instaladores (modo -Ensayo: no instalan nada)"
    if (-not $Raiz) { Linea "(sin carpeta de AXIOMA: no hay instaladores para ensayar)"; return }
    $ps = Obtener-PowerShellExe
    if (-not $ps) { Linea "No encontre PowerShell para ensayar los .ps1."; return }
    foreach ($nombre in @("instalar_axioma.ps1", "iniciar_axioma.ps1")) {
        $f = Join-Path $Raiz ("instalar\" + $nombre)
        if (-not (Test-Path -LiteralPath $f)) { Linea ("(no existe " + $f + ")"); continue }
        $r = Ejecutar ("ensayo de " + $nombre) $ps ('-NoProfile -ExecutionPolicy Bypass -File "' + $f + '" -Ensayo') $TimeoutEnsayo $Raiz
        if ($r.Timeout) { Hallazgo "CRITICO" ($nombre + " en modo -Ensayo se COLGO: hay un paso que espera algo que nunca llega.") "Ver la salida del ensayo en este informe para saber en que paso quedo." }
        elseif ($null -eq $r.Codigo) { Hallazgo "CRITICO" ($nombre + " no se pudo ejecutar: " + $r.Error) "Politica de ejecucion o antivirus." }
        elseif ($r.Codigo -ne 0) { Hallazgo "AVISO" ($nombre + " en modo -Ensayo termino con codigo " + $r.Codigo + "." + $(if ($r.Error) { " stderr: " + (Recortar $r.Error 200) } else { "" })) "Ver la salida del ensayo en este informe." }
        if ($r.Error) { Hallazgo "AVISO" ($nombre + " escribio en el canal de errores durante el ensayo (" + (Recortar $r.Error 200) + ").") "Un error escrito a stderr que no corta el script es justo el tipo de fallo silencioso." }
    }
}

# ---------------------------------------------------------------------
# T. Traza de la instalacion REAL (solo con -Traza)
# ---------------------------------------------------------------------
function Mostrar-Nuevo {
    param([string]$Ruta, [hashtable]$Pos)
    if (-not (Test-Path -LiteralPath $Ruta)) { return }
    try {
        $fs = New-Object IO.FileStream($Ruta, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        if ($fs.Length -gt $Pos[$Ruta]) {
            [void]$fs.Seek($Pos[$Ruta], [IO.SeekOrigin]::Begin)
            $n = [int]($fs.Length - $Pos[$Ruta])
            $buf = New-Object byte[] $n
            $leidos = $fs.Read($buf, 0, $n)
            $Pos[$Ruta] = $Pos[$Ruta] + $leidos
            $txt = [Text.Encoding]::UTF8.GetString($buf, 0, $leidos)
            $txt = [regex]::Replace($txt, ([string][char]27) + "\[[0-9;]*[A-Za-z]", "")
            Write-Host -NoNewline $txt
        }
        $fs.Close()
    } catch { }
}

function Ejecutar-Traza {
    param([string]$Carpeta)
    Seccion "T. TRAZA DE LA INSTALACION REAL (instala de verdad, linea por linea)"
    if (-not $Raiz) { Linea "No hay carpeta de AXIOMA: no se puede trazar la instalacion."; return }
    $inst = Join-Path $Raiz "instalar\instalar_axioma.ps1"
    if (-not (Test-Path -LiteralPath $inst)) { Linea ("No existe " + $inst); return }
    $ps = Obtener-PowerShellExe
    if (-not $ps) { Linea "No encontre PowerShell."; return }
    Linea "Estado ANTES de instalar:"
    if ($script:Hechos["docker_responde"] -eq $true) {
        [void](Ejecutar "docker ps -a (antes)" "docker" "ps -a" 40)
        [void](Ejecutar "docker images (antes)" "docker" "images" 40)
    }
    $transcripcion = Join-Path $Carpeta "traza_instalacion.txt"
    $envoltorio = Join-Path $Carpeta "traza_envoltorio.ps1"
    $extra = ""
    if ($SinAcceso) { $extra = " -SinAcceso" }
    $codigoEnv = @'
$ErrorActionPreference = 'Continue'
Start-Transcript -Path '__TRANSCRIPCION__' -Force | Out-Null
Write-Host ('TRAZA INICIO ' + (Get-Date -Format o))
Set-PSDebug -Trace 1
try { & '__INSTALADOR__'__EXTRA__ } catch { Write-Host ('EXCEPCION: ' + ($_ | Out-String)) }
$codigoSalida = $LASTEXITCODE
Set-PSDebug -Off
Write-Host ('TRAZA FIN codigo=' + $codigoSalida)
Write-Host '--- Errores acumulados en $Error (todos, durante la instalacion) ---'
foreach ($e in $Error) { $t = ($e | Out-String); Write-Host $t; [Console]::Error.Write($t) }
Stop-Transcript | Out-Null
exit $codigoSalida
'@
    $codigoEnv = $codigoEnv.Replace("__TRANSCRIPCION__", $transcripcion).Replace("__INSTALADOR__", $inst).Replace("__EXTRA__", $extra)
    [IO.File]::WriteAllText($envoltorio, $codigoEnv, (New-Object Text.UTF8Encoding($true)))
    Linea ("Se ejecuta: " + $ps + " -File traza_envoltorio.ps1  (instalar_axioma.ps1" + $extra + ")")
    Write-Host ""
    Write-Host "  Instalando de verdad con traza. Puede tardar varios minutos (compila la imagen)." -ForegroundColor Yellow
    Write-Host ""
    $consolaTraza = Join-Path $Carpeta "traza_consola.txt"
    $erroresTraza = Join-Path $Carpeta "traza_errores.txt"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $codigo = $null
    $colgado = $false
    try {
        $p = Start-Process -FilePath $ps -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $envoltorio + '"') -NoNewWindow -PassThru -WorkingDirectory $Raiz -RedirectStandardOutput $consolaTraza -RedirectStandardError $erroresTraza
        $null = $p.Handle
        $pos = @{}
        $pos[$consolaTraza] = [int64]0
        $pos[$erroresTraza] = [int64]0
        $limite = (Get-Date).AddSeconds(2700)
        while (-not $p.HasExited) {
            Start-Sleep -Milliseconds 700
            Mostrar-Nuevo $consolaTraza $pos
            Mostrar-Nuevo $erroresTraza $pos
            if ((Get-Date) -gt $limite) {
                $colgado = $true
                if ($script:EsWindows) { try { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null } catch { } }
                try { $p.Kill() } catch { }
                break
            }
        }
        if (-not $colgado) { $p.WaitForExit(); $codigo = $p.ExitCode }
        Start-Sleep -Milliseconds 300
        Mostrar-Nuevo $consolaTraza $pos
        Mostrar-Nuevo $erroresTraza $pos
    } catch { Linea ("No se pudo ejecutar la traza: " + $_.Exception.Message) }
    $sw.Stop()
    Linea ("Traza terminada en " + [math]::Round($sw.Elapsed.TotalSeconds, 0) + " s | codigo de salida: " + $codigo + " | colgado (45 min): " + $colgado)
    Hecho "traza_codigo" $codigo
    if ($colgado) { Hallazgo "CRITICO" "La instalacion real no termino en 45 minutos (se corto)." "Ver traza_instalacion.txt: la ultima linea muestra donde quedo." }
    elseif ($codigo -ne 0) { Hallazgo "CRITICO" ("La instalacion real termino con codigo " + $codigo + ".") "Ver traza_instalacion.txt (ultimas lineas y la seccion de errores acumulados)." }
    $textoConsola = Leer-Texto $consolaTraza
    $textoErrores = Leer-Texto $erroresTraza
    $lineasConsola = @($textoConsola -split "\r?\n")
    Linea ("Lineas de consola/traza capturadas: " + $lineasConsola.Count + " (completas en traza_consola.txt; la transcripcion de PowerShell esta en traza_instalacion.txt)")
    Linea "Ultimas 120 lineas de la traza:"
    Linea (($lineasConsola | Select-Object -Last 120) -join "`r`n")
    if ($textoErrores.Trim()) {
        Linea "Salida de ERRORES (stderr) del proceso de instalacion:"
        Linea (Recortar $textoErrores 8000)
        Hallazgo "AVISO" "La instalacion escribio en el canal de errores (stderr); revisar traza_errores.txt." "Un error escrito a stderr que no corta el script es un fallo silencioso tipico."
    }
    if (-not (Test-Path -LiteralPath $consolaTraza)) { Hallazgo "AVISO" "La traza no genero archivo de consola: el proceso pudo haber muerto antes de escribir." "" }
    Linea "Estado DESPUES de instalar:"
    if ($script:Hechos["docker_responde"] -eq $true -or (Get-Command docker -ErrorAction SilentlyContinue)) {
        [void](Ejecutar "docker ps -a (despues)" "docker" "ps -a" 40)
        [void](Ejecutar "docker images (despues)" "docker" "images" 40)
    }
}

# ---------------------------------------------------------------------
# Informe final
# ---------------------------------------------------------------------
function Elegir-Carpeta-Salida {
    $nombre = "diagnostico_axioma_" + $script:Inicio.ToString("yyyyMMdd_HHmmss")
    $bases = @()
    if ($Salida) { $bases += $Salida }
    try { $bases += [Environment]::GetFolderPath("Desktop") } catch { }
    if ($PSScriptRoot) { $bases += $PSScriptRoot }
    $bases += [IO.Path]::GetTempPath()
    foreach ($b in $bases) {
        if (-not $b) { continue }
        try {
            $dir = Join-Path $b $nombre
            [void](New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop)
            return $dir
        } catch { }
    }
    return ""
}

function Escribir-Informe {
    param([string]$Carpeta)
    $crit = @($script:Hallazgos | Where-Object { $_.Nivel -eq "CRITICO" })
    $avis = @($script:Hallazgos | Where-Object { $_.Nivel -eq "AVISO" })
    $info = @($script:Hallazgos | Where-Object { $_.Nivel -eq "INFO" })
    $cab = New-Object System.Text.StringBuilder
    [void]$cab.AppendLine("INFORME DE DIAGNOSTICO DE AXIOMA (v" + $script:VersionScript + ")")
    [void]$cab.AppendLine("Generado: " + (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + " | duracion: " + [math]::Round(((Get-Date) - $script:Inicio).TotalSeconds, 0) + " s")
    [void]$cab.AppendLine("Resultado: " + $crit.Count + " critico(s), " + $avis.Count + " aviso(s), " + $info.Count + " informativo(s)")
    [void]$cab.AppendLine("Las claves y tokens NO se guardan: se muestran como <REDACTADO> o solo como 'definida'.")
    [void]$cab.AppendLine("")
    [void]$cab.AppendLine("HALLAZGOS (de lo mas grave a lo menos)")
    [void]$cab.AppendLine(("-" * 78))
    $orden = @($crit) + @($avis) + @($info)
    if ($orden.Count -eq 0) { [void]$cab.AppendLine("(sin hallazgos automaticos: revisar igual las secciones de abajo)") }
    $n = 0
    foreach ($h in $orden) {
        $n++
        [void]$cab.AppendLine(("{0,2}. [{1}] {2}" -f $n, $h.Nivel, $h.Que))
        if ($h.Como) { [void]$cab.AppendLine("       -> " + $h.Como) }
    }
    [void]$cab.AppendLine("")
    [void]$cab.AppendLine("COMANDOS EJECUTADOS")
    [void]$cab.AppendLine(("-" * 78))
    foreach ($t in $script:Tareas) {
        $est = "codigo " + $t.Codigo
        if ($t.Timeout) { $est = "TIMEOUT" } elseif ($t.NoEncontrado) { $est = "NO ENCONTRADO" } elseif ($null -eq $t.Codigo) { $est = "sin codigo" }
        [void]$cab.AppendLine(("{0,-58} {1,-14} {2,6} s" -f (Recortar $t.Nombre 58), $est, $t.Segundos))
    }
    $contenido = (Redactar $cab.ToString()) + "`r`n" + $script:Texto.ToString()
    $utf8 = New-Object Text.UTF8Encoding($true)
    $utf8SinBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $Carpeta "informe.txt"), $contenido, $utf8)
    try {
        $json = [ordered]@{ version = $script:VersionScript; hechos = $script:Hechos; hallazgos = $script:Hallazgos; comandos = ($script:Tareas | Select-Object Nombre, Comando, Codigo, Segundos, Timeout, NoEncontrado) }
        [IO.File]::WriteAllText((Join-Path $Carpeta "hechos.json"), (Redactar ($json | ConvertTo-Json -Depth 5)), $utf8SinBom)
    } catch { }
    return @{ Criticos = $crit; Avisos = $avis; Infos = $info }
}

# =====================================================================
# Programa principal
# =====================================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  AXIOMA - diagnostico de instalacion (v$($script:VersionScript))" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
if ($Traza) {
    Write-Host "  MODO TRAZA: ademas del diagnostico, va a INSTALAR AXIOMA DE VERDAD" -ForegroundColor Yellow
    Write-Host "  (compila la imagen de Docker, puede tardar y ocupar varios GB)." -ForegroundColor Yellow
    if (-not $SinPreguntas) {
        $resp = ""
        try { $resp = Read-Host "  Escribi SI para continuar, o cualquier otra cosa para cancelar" } catch { $resp = "NO" }
        if ($resp.Trim().ToUpper() -ne "SI") { Write-Host "  Cancelado. No se hizo nada." ; exit 0 }
    }
} else {
    Write-Host "  Solo LEE el sistema: no instala ni cambia nada." -ForegroundColor Gray
}
Write-Host "  Puede tardar unos minutos. No cierres esta ventana." -ForegroundColor Gray
Write-Host ""

$Raiz = Buscar-Raiz $Raiz
Hecho "raiz" $Raiz
$carpetaSalida = Elegir-Carpeta-Salida
if (-not $carpetaSalida) { Write-Host "  [X] No pude crear ninguna carpeta para guardar el informe." -ForegroundColor Red; exit 2 }
Write-Host ("  Carpeta de resultados: " + $carpetaSalida) -ForegroundColor Gray
Write-Host ("  Carpeta de AXIOMA:     " + $(if ($Raiz) { $Raiz } else { "(no encontrada)" })) -ForegroundColor Gray
Write-Host ""

Intentar "contexto"       { Recolectar-Contexto }
Intentar "sistema"        { Recolectar-Sistema }
Intentar "virtualizacion" { Recolectar-Virtualizacion }
Intentar "docker"         { Recolectar-Docker }
Intentar "puertos"        { Recolectar-Puertos }
if ($SinRed) { Seccion "6. Red"; Linea "(omitido por -SinRed)" } else { Intentar "red" { Recolectar-Red } }
Intentar "proyecto"       { Recolectar-Proyecto }
Intentar "accesos"        { Recolectar-Accesos }
Intentar "estado"         { Recolectar-Estado }
Intentar "seguridad"      { Recolectar-Seguridad }
Intentar "eventos"        { Recolectar-Eventos }
Intentar "ensayo"         { Ensayar-Instaladores }
if ($Traza) {
    Intentar "traza"      { Ejecutar-Traza $carpetaSalida }
    Intentar "accesos2"   { Recolectar-Accesos }
    Intentar "estado2"    { Recolectar-Estado " (despues de la traza)" }
}

$res = $null
try { $res = Escribir-Informe $carpetaSalida } catch { Write-Host ("  [X] No pude escribir el informe: " + $_.Exception.Message) -ForegroundColor Red }

$zip = $carpetaSalida + ".zip"
try { Compress-Archive -Path (Join-Path $carpetaSalida "*") -DestinationPath $zip -Force -ErrorAction Stop }
catch { Write-Host ("  [!] No pude crear el .zip (" + $_.Exception.Message + "). Comprimi la carpeta a mano.") -ForegroundColor Yellow; $zip = "" }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
if ($res) {
    Write-Host ("  Resultado: " + $res.Criticos.Count + " critico(s), " + $res.Avisos.Count + " aviso(s), " + $res.Infos.Count + " informativo(s)") -ForegroundColor $(if ($res.Criticos.Count) { "Red" } elseif ($res.Avisos.Count) { "Yellow" } else { "Green" })
    $mostrar = @($res.Criticos) + @($res.Avisos)
    foreach ($h in ($mostrar | Select-Object -First 8)) {
        Write-Host ("   [" + $h.Nivel + "] " + (Recortar $h.Que 110)) -ForegroundColor $(if ($h.Nivel -eq "CRITICO") { "Red" } else { "Yellow" })
    }
}
Write-Host ""
if ($zip) { Write-Host ("  Archivo para enviar: " + $zip) -ForegroundColor Cyan } else { Write-Host ("  Carpeta para enviar: " + $carpetaSalida) -ForegroundColor Cyan }
Write-Host "  (Las claves no se guardan. Revisalo si queres antes de enviarlo.)" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Green
if ($script:EsWindows -and (-not $SinPreguntas)) { try { if ($zip) { Start-Process explorer.exe ('/select,"' + $zip + '"') } else { Start-Process explorer.exe ('"' + $carpetaSalida + '"') } } catch { } }
try { Remove-Item -LiteralPath $script:Temp -Recurse -Force -ErrorAction SilentlyContinue } catch { }
exit 0
