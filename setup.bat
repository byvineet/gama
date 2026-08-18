@echo off
REM ============================================================
REM  Gama - One-Click Setup Script for Windows
REM  By Vineet Machchal
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ======================================================
echo   Gama - Windows AI Assistant
echo   Setup Script  (c) Vineet Machchal
echo ======================================================
echo.

REM --- Check Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/5] Python version:
python --version
echo.

REM --- Create venv ---
if not exist ".venv" (
    echo [2/5] Creating virtual environment...
    python -m venv .venv
) else (
    echo [2/5] Virtual environment already exists.
)
echo.

call ".venv\Scripts\activate.bat"

REM --- Upgrade pip ---
echo [3/5] Upgrading pip...
python -m pip install --upgrade pip setuptools wheel
echo.

REM --- Install dependencies ---
echo [4/5] Installing dependencies (this takes 5-15 minutes)...
pip install -r requirements.txt
echo.

REM --- Copy config if missing ---
if not exist "config\api_keys.json" (
    echo [5/5] Creating config from template...
    copy "config\api_keys.example.json" "config\api_keys.json" >nul
    echo.
    echo ======================================================
    echo   IMPORTANT: Add your Gemini API key
    echo ======================================================
    echo   1. Get a free key at: https://aistudio.google.com/app/apikey
    echo   2. Open:  %cd%\config\api_keys.json
    echo   3. Replace YOUR_GEMINI_API_KEY_HERE with your real key
    echo ======================================================
    notepad "config\api_keys.json"
) else (
    echo [5/5] Config already exists.
)
echo.

REM --- Wake word: default (Vosk) config + one-time model download ---
if not exist "config\wake_word.json" (
    copy "config\wake_word.example.json" "config\wake_word.json" >nul
)
if not exist "models\vosk-model-small-en-us-0.15" (
    echo [6/6] Downloading local wake word model ^(~40MB, one-time^)...
    python scripts\download_vosk_model.py
) else (
    echo [6/6] Wake word model already present.
)
echo.

echo ======================================================
echo   Setup Complete!
echo ======================================================
echo.
echo To run Gama:  run.bat
echo To test the wake word alone:  python -m wake_word.listener
echo   ^(say "wake up gama" — see wake_word/README.md to customize^)
echo.
pause
endlocal
