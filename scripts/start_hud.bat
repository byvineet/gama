@echo off
title GAMA React HUD
cd /d "%~dp0\..\web_ui"
if not exist node_modules (
  echo npm install...
  call npm install
)
echo Open http://127.0.0.1:5173  — Gama must already be running
start "" http://127.0.0.1:5173
npm run dev
pause
