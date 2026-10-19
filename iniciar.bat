@echo off
setlocal
cd /d "%~dp0"
title FLOW

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno virtual .venv...
  where py >nul 2>&1
  if errorlevel 1 (
    where python >nul 2>&1
    if errorlevel 1 (
      echo No se encontro Python. Instalalo desde https://www.python.org/downloads/
      pause
      exit /b 1
    )
    python -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
  if errorlevel 1 (
    echo No se pudo crear el entorno virtual.
    pause
    exit /b 1
  )
  echo Instalando dependencias...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Fallo la instalacion de dependencias.
    pause
    exit /b 1
  )
)

call :ready
if not errorlevel 1 goto open

echo Levantando servidor...
start "FLOW" /D "%~dp0." "%~dp0.venv\Scripts\python.exe" -m flow.web

echo Esperando a que la pagina responda...
set /a tries=0
:wait
set /a tries+=1
if %tries% GTR 90 (
  echo El servidor no respondio. Mira la ventana FLOW.
  pause
  exit /b 1
)
call :ready
if errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)

:open
start "" "http://127.0.0.1:8787/"
exit /b 0

:ready
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8787/' -UseBasicParsing -TimeoutSec 2; exit $(if ($r.StatusCode -eq 200) { 0 } else { 1 }) } catch { exit 1 }"
exit /b %errorlevel%
