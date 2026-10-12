@echo off
REM ═══════════════════════════════════════════════════════════════
REM AXIOMA - INSTALAR EN WINDOWS (DOBLE CLIC)
REM ═══════════════════════════════════════════════════════════════
REM Por que existe este .bat:
REM   Un .ps1 NO se ejecuta con doble clic: Windows lo abre en el editor, o lo corre
REM   en una consola que se CIERRA sola al terminar (y si falla, el mensaje se pierde).
REM   Este .bat lo llama con -ExecutionPolicy Bypass (para que funcione aunque Windows
REM   tenga la ejecucion de guiones deshabilitada, que es lo normal recien instalado)
REM   y deja la ventana ABIERTA al final para que se pueda leer que paso.
REM
REM Sin acentos ni emoji a proposito: la consola de Windows (CP850/CP437) los imprime mal.
REM Uso: doble clic. Nada mas.

title AXIOMA - Instalacion
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   AXIOMA - instalacion en Windows
echo   Carpeta del proyecto: %CD%
echo ============================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo   [X] No encontre PowerShell en este Windows.
  echo       Es raro: viene con Windows. Avisa y lo vemos.
  echo.
  pause
  exit /b 4
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar_axioma.ps1"
set AXIOMA_RESULTADO=%ERRORLEVEL%

echo.
echo ============================================================
if "%AXIOMA_RESULTADO%"=="0" (
  echo   Listo. Si algo falto, la pantalla de AXIOMA lo explica y lo instala.
) else (
  echo   La instalacion NO termino bien ^(codigo %AXIOMA_RESULTADO%^).
  echo   Arriba esta el paso exacto que fallo. La ventana queda abierta
  echo   para que puedas leerlo o copiarlo.
)
echo ============================================================
echo.
pause
exit /b %AXIOMA_RESULTADO%
