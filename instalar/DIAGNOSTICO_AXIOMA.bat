@echo off
setlocal EnableDelayedExpansion
title AXIOMA - Diagnostico
color 0E

REM ══════════════════════════════════════════════════════════════
REM  DIAGNOSTICO_AXIOMA.bat — Diagnostica y ofrece arrancar AXIOMA
REM ══════════════════════════════════════════════════════════════

echo ============================================================
echo   AXIOMA - Diagnostico
echo ============================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo [X] No encuentro PowerShell en este Windows.
  pause
  exit /b 4
)

REM ── Asegurar Docker (para que el diagnostico sea util) ──────
call :AsegurarDocker
if errorlevel 1 (
  echo.
  echo [!] Docker no responde. El diagnostico lo va a marcar como critico.
  echo.
)

REM ── Ejecutar diagnostico ────────────────────────────────────
echo Ejecutando diagnostico...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnostico_axioma.ps1" %*
set RESULTADO=%ERRORLEVEL%

echo.
echo ============================================================
if "%RESULTADO%"=="0" (
  echo   Diagnostico OK. El .zip quedo en tu Escritorio.
) else (
  echo   Diagnostico termino con codigo %RESULTADO%.
)
echo ============================================================
echo.

REM ── Ofrecer arrancar AXIOMA ─────────────────────────────────
set "AXIOMA_BAT=%~dp0AXIOMA.bat"
if not exist "%AXIOMA_BAT%" set "AXIOMA_BAT=%LOCALAPPDATA%\AXIOMA\AXIOMA.bat"

if exist "%AXIOMA_BAT%" (
  set /p "RESPUESTA=Queres arrancar AXIOMA ahora? (S/N): "
  if /i "!RESPUESTA!"=="S" (
    echo.
    call "%AXIOMA_BAT%"
    exit /b %ERRORLEVEL%
  )
)

pause
exit /b %RESULTADO%


REM ══════════════════════════════════════════════════════════════
REM  AsegurarDocker (mismo bloque que AXIOMA.bat)
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
  echo [Docker] No lo encontre. Abri Docker Desktop a mano.
  exit /b 1
)

echo [Docker] Abriendo: !DOCKER_EXE!
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
  exit /b 1
)
set /a RESTO=!SEG! %% 15
if !RESTO! EQU 0 echo   ...esperando (!SEG! s)
goto :espera_docker
