@echo off
rem AXIOMA - desinstalar (doble clic). Deja tus datos en una carpeta de respaldo;
rem para borrar tambien los datos y los modelos, ejecuta:
rem   instalar\desinstalar_axioma.ps1 -Todo
title AXIOMA - desinstalar
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0desinstalar_axioma.ps1" %*
echo.
pause
