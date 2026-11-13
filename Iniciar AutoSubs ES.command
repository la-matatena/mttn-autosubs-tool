#!/usr/bin/env bash
#
# Finder launcher for MTTN AutoSubs Tool ("Start AutoSubs ES").
#
# Double-click in Finder (or run from Terminal) to start the local server from
# this checkout. Prefers the pywebview native window prepared by
# install_imac.sh; otherwise starts the server with --app-window, which opens
# 127.0.0.1:55120 in a chromeless Chrome/Edge window or a regular browser tab.
#
# Usage:
#   "./Iniciar AutoSubs ES.command" [autosubs_server.py flags...]
#
# Flags:
#   All arguments are forwarded unchanged to autosubs_server.py.
set -euo pipefail

# Run from the repo so autosubs_server.py and ui/ resolve relative paths.
cd "$(dirname "$0")"
# Finder starts .command files with a minimal PATH; add both Homebrew prefixes
# (Intel /usr/local, Apple Silicon /opt/homebrew) for whisper-cli and ffmpeg.
export PATH="/usr/local/bin:/opt/homebrew/bin:${PATH}"

# Native window (pywebview) if the installer prepared its isolated venv.
VENV_PY="${HOME}/Library/Application Support/AutoSubsES/venv/bin/python"
if [[ -x "${VENV_PY}" ]] && "${VENV_PY}" -c "import webview" >/dev/null 2>&1; then
  exec "${VENV_PY}" -B autosubs_server.py --desktop "$@"
fi

# Fallback: any python3 (3.10+ stdlib only). -B avoids writing __pycache__.
PYTHON_BIN="python3"
if [[ -x "/usr/local/bin/python3" ]]; then
  PYTHON_BIN="/usr/local/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi

exec "${PYTHON_BIN}" -B autosubs_server.py --app-window "$@"
