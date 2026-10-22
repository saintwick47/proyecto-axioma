@echo off
setlocal EnableDelayedExpansion
title AXIOMA
color 0B

REM ══════════════════════════════════════════════════════════════
REM  AXIOMA.bat — Unico punto de entrada
REM  Instala si hace falta, arranca siempre. Arranca Docker solo.
REM ══════════════════════════════════════════════════════════════

echo ============================================================
echo   AXIOMA
echo ============================================================
echo.

REM ── Localizar la raiz de AXIOMA ─────────────────────────────
set "AXIOMA_ROOT=%LOCALAPPDATA%\AXIOMA"
if not exist "%AXIOMA_ROOT%\instalar\instalar_axioma.ps1" (
  if exist "%~dp0instalar\instalar_axioma.ps1" (
    set "AXIOMA_ROOT=%~dp0"
    set "AXIOMA_ROOT=!AXIOMA_ROOT:~0,-1!"
  ) else (
    echo [X] No encuentro la instalacion de AXIOMA.
    echo     Esperado en: %AXIOMA_ROOT%
    echo     o junto a este archivo.
    echo.
    pause
    exit /b 4
  )
)
echo [AXIOMA] Raiz: %AXIOMA_ROOT%
echo.

REM ── Verificar PowerShell ────────────────────────────────────
where powershell >nul 2>&1
if errorlevel 1 (
  echo [X] No encuentro PowerShell en este Windows.
  pause
  exit /b 4
)

REM ── Asegurar Docker ─────────────────────────────────────────
call :AsegurarDocker
if errorlevel 1 (
  echo.
  echo [X] Docker no esta disponible.
  echo     Abri Docker Desktop a mano desde el menu Inicio,
  echo     espera a que diga "Engine running" y volve a intentar.
  echo.
  pause
  exit /b 4
)

REM ── Decidir: instalar o solo arrancar ───────────────────────
REM ✅ 2026-10-22: antes bastaba con que existiera `.env`, pero el instalador lo crea en el PASO 3 y
REM el build es el PASO 4: si el build fallaba, el siguiente doble clic entraba por la rama de
REM ARRANCAR y daba otro error distinto, sin reanudar la instalación. Ahora "instalado" = hay .env
REM Y existe la imagen. Si falta la imagen, se REANUDA la instalación (que es idempotente).
set "INSTALADO=0"
if exist "%AXIOMA_ROOT%\.env" (
  docker image inspect axioma:local >nul 2>&1
  if not errorlevel 1 set "INSTALADO=1"
)
if "!INSTALADO!"=="1" (
  echo [AXIOMA] Instalacion detectada. Arrancando...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%AXIOMA_ROOT%\instalar\iniciar_axioma.ps1" %*
) else (
  if exist "%AXIOMA_ROOT%\.env" (
    echo [AXIOMA] Hay .env pero FALTA la imagen (la instalacion quedo a medias). Reanudando...
  ) else (
    echo [AXIOMA] Primera vez. Instalando (puede tardar varios minutos)...
  )
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%AXIOMA_ROOT%\instalar\instalar_axioma.ps1" %*
)

set CODIGO=%ERRORLEVEL%
echo.
echo ============================================================
if "%CODIGO%"=="0" (
  echo   LISTO
) else (
  echo   Termino con codigo %CODIGO%. Mira los mensajes de arriba.
)
echo ============================================================
echo.
pause
exit /b %CODIGO%


REM ══════════════════════════════════════════════════════════════
REM  AsegurarDocker: si Docker no responde, abre Docker Desktop
REM  y espera hasta 5 minutos. Devuelve 0 si responde, 1 si no.
REM ══════════════════════════════════════════════════════════════
:AsegurarDocker
docker info >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  echo [Docker] Ya esta respondiendo.
  exit /b 0
)

echo [Docker] No responde. Buscando "Docker Desktop.exe"...
set "DOCKER_EXE="
set "PF86=%ProgramFiles(x86)%"

if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" (
  set "DOCKER_EXE=%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
)
if not defined DOCKER_EXE if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
  set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
)
if not defined DOCKER_EXE if defined PF86 if exist "%PF86%\Docker\Docker\Docker Desktop.exe" (
  set "DOCKER_EXE=%PF86%\Docker\Docker\Docker Desktop.exe"
)

if not defined DOCKER_EXE (
  echo [Docker] No lo encontre en las rutas tipicas.
  echo          Instalalo desde https://www.docker.com/products/docker-desktop
  echo          o abri Docker Desktop a mano.
  exit /b 1
)

echo [Docker] Encontrado: !DOCKER_EXE!
echo [Docker] Abriendo Docker Desktop...
start "" "!DOCKER_EXE!"

echo [Docker] Esperando hasta 5 minutos a que responda...
set /a SEG=0
:espera_docker
timeout /t 3 /nobreak >nul
set /a SEG+=3
docker info >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  echo [Docker] Listo en !SEG! s.
  exit /b 0
)
if !SEG! GEQ 300 (
  echo [Docker] No respondio en 5 minutos.
  echo          Si dice algo de WSL 2, en una consola de admin:
  echo              wsl --update
  exit /b 1
)
set /a RESTO=!SEG! %% 15
if !RESTO! EQU 0 echo   ...esperando (!SEG! s)
goto :espera_docker
