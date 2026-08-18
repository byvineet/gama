#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/../web_ui"
if [ ! -d node_modules ]; then
  npm install
fi
echo "Starting Vite — http://127.0.0.1:5173"
echo "Ensure Gama is running (python main.py) so ws://127.0.0.1:8765 is up."
npm run dev
