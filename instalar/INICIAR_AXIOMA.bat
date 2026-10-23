@echo off
REM ===============================================================
REM AXIOMA - ARRANCAR EN WINDOWS (DOBLE CLIC)
REM ===============================================================
REM Igual que INSTALAR_AXIOMA.bat pero para el uso diario: enciende AXIOMA,
REM espera a que responda y abre el navegador.
REM   -Detener detiene: para eso, abrir PowerShell y correr
REM   .\instalar\iniciar_axioma.ps1 -Detener
REM
REM Sin acentos ni emoji a proposito (consola CP850/CP437).

title AXIOMA
setlocal
cd /d "%~dp0.."

echo ============================================================
echo   AXIOMA - arrancando
echo ============================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo   [X] No encontre PowerShell en este Windows.
  echo.
  pause
  exit /b 4
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_axioma.ps1"
set AXIOMA_RESULTADO=%ERRORLEVEL%

echo.
if "%AXIOMA_RESULTADO%"=="0" (
  echo   AXIOMA esta en linea: http://127.0.0.1:8080/
) else (
  echo   No pude arrancar AXIOMA ^(codigo %AXIOMA_RESULTADO%^). Arriba esta el paso que fallo.
)
echo.
echo   Esta ventana se puede cerrar: AXIOMA sigue andando en segundo plano.
echo.
pause
exit /b %AXIOMA_RESULTADO%
