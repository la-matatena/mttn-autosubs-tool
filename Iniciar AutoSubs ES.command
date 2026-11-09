#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/usr/local/bin:/opt/homebrew/bin:${PATH}"

# Ventana nativa (pywebview) si el instalador preparó su entorno aislado.
VENV_PY="${HOME}/Library/Application Support/AutoSubsES/venv/bin/python"
if [[ -x "${VENV_PY}" ]] && "${VENV_PY}" -c "import webview" >/dev/null 2>&1; then
  exec "${VENV_PY}" -B autosubs_server.py --desktop "$@"
fi

PYTHON_BIN="python3"
if [[ -x "/usr/local/bin/python3" ]]; then
  PYTHON_BIN="/usr/local/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi

exec "${PYTHON_BIN}" -B autosubs_server.py --app-window "$@"
