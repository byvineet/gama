@echo off
title GAMA Backend (web-only)
cd /d "%~dp0\.."
set GAMA_WEB_UI_ONLY=1
echo Gama headless + web bridge on http://127.0.0.1:8765
echo Then run scripts\start_hud.bat and open http://127.0.0.1:5173
python main.py
pause
