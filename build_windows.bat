@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Build AegisTrans Portable (Windows)

echo =======================================================
echo   AegisTrans - Build Portable Windows Distribution
echo =======================================================

:: Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10-3.12 from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Virtual environment
if not exist ".venv" (
    echo [*] Creating virtual environment .venv...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo [*] Installing dependencies...
python -m pip install -r requirements-app.txt

echo [*] Fetching AI layout models and fonts...
python scripts\fetch_assets.py

echo [*] Staging 9Router binary into app/bin...
python scripts\bundle_9router.py

echo [*] Packaging standalone application with PyInstaller...
python -m PyInstaller --noconfirm --clean app.spec

if not exist "dist\AegisTrans\AegisTrans.exe" (
    echo [ERROR] PyInstaller failed to produce dist\AegisTrans\AegisTrans.exe
    pause
    exit /b 1
)

echo [*] Compressing into dist\AegisTrans-Windows-Portable.zip...
powershell -Command "if (Test-Path 'dist\AegisTrans-Windows-Portable.zip') { Remove-Item 'dist\AegisTrans-Windows-Portable.zip' }; Compress-Archive -Path 'dist\AegisTrans\*' -DestinationPath 'dist\AegisTrans-Windows-Portable.zip' -Force"

echo =======================================================
echo [SUCCESS] Build complete!
echo Portable distribution archive:
echo   dist\AegisTrans-Windows-Portable.zip
echo =======================================================
pause
