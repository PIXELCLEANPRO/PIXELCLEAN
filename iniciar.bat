@echo off
title Reparador de pixeles quemados
echo ============================================================
echo   Reparador de pixeles quemados - preparando todo...
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python instalado.
    echo.
    echo Por favor instalalo primero desde:
    echo   https://www.python.org/downloads/
    echo IMPORTANTE: al instalar, tildar la casilla "Add python.exe to PATH"
    echo.
    echo Despues de instalarlo, volve a hacer doble clic en este archivo.
    pause
    exit /b
)

echo Verificando Pillow y numpy (librerias necesarias)...
python -m pip install --quiet --disable-pip-version-check pillow numpy

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo No se encontro ffmpeg instalado.
    echo Intentando instalarlo automaticamente con winget...
    winget install --id Gyan.FFmpeg -e --silent
    echo.
    echo Si esto fallo, instalalo manualmente desde https://ffmpeg.org/download.html
    echo y volve a correr este archivo.
    echo Continuando de todas formas...
)

echo.
echo Abriendo el programa...
python "%~dp0reparador_pixeles_v2.py"

if errorlevel 1 (
    echo.
    echo Hubo un error al abrir el programa. Revisa el mensaje de arriba.
    pause
)
