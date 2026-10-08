@echo off
setlocal
cd /d "%~dp0"
title AegisTrans

:: Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed.
    echo Please install Python 3.10-3.12 from https://www.python.org/downloads/
    echo Make sure to check 'Add Python to PATH' during installation!
    pause
    exit /b 1
)

:: Create virtual environment if missing
if not exist ".venv" (
    echo [1/3] Setting up environment for the first time...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

:: Install dependencies & assets on first run
if not exist ".venv\installed.flag" (
    echo [2/3] Installing dependencies and downloading AI models...
    pip install -r requirements-app.txt
    python scripts\fetch_assets.py
    python scripts\bundle_9router.py
    echo done > .venv\installed.flag
)

:: Launch the Desktop GUI
echo [3/3] Starting AegisTrans...
start pythonw -m app.gui
