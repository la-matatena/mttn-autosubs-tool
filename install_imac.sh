#!/usr/bin/env bash
#
# Installs MTTN AutoSubs Tool on macOS (Intel iMac 2017 and Apple Silicon).
#
# Steps, all idempotent so the script can be re-run safely:
#   1. Installs whisper-cpp, ffmpeg, and ffmpeg-full via Homebrew if present.
#   2. Creates the app data tree under ~/Library/Application Support/AutoSubsES
#      (models/, fonts/, bridge/inbox/, bridge/outbox/, cache/).
#   3. Copies the three Lua scripts into Resolve's Fusion/Scripts/Utility
#      (Workspace > Scripts menu) and the five Text+ .setting templates into
#      Fusion/Templates/Edit/Titles/MTTN AutoSubs (Effects > Titles).
#   4. Downloads the selected Whisper GGML model from Hugging Face, resuming a
#      partial ".part" download when possible.
#   5. Creates an isolated venv with the optional pywebview native window.
#   6. On macOS, generates ~/Applications/MTTN AutoSubs Tool.app.
#
# Usage:
#   ./install_imac.sh [--skip-model] [--model=MODEL_ID]
#
# Flags:
#   --skip-model      Do not download a Whisper model (it can be fetched later
#                     from the app).
#   --model=MODEL_ID  One of turbo-q8 (default), large-v3-q5, turbo-q5,
#                     small-q8, base-q8.
#
# User-facing messages are in Spanish; exits non-zero on bad flags or a failed
# model download.
set -euo pipefail

# Paths: repo checkout, app data, and Resolve's per-user Fusion folders.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DATA_DIR="${HOME}/Library/Application Support/AutoSubsES"
RESOLVE_FUSION_DIR="${HOME}/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion"
RESOLVE_SCRIPTS_DIR="${RESOLVE_FUSION_DIR}/Scripts/Utility"
RESOLVE_TEMPLATES_DIR="${RESOLVE_FUSION_DIR}/Templates/Edit/Titles/MTTN AutoSubs"
MODEL_ID="turbo-q8"
SKIP_MODEL=false

# --- Argument parsing ---
for arg in "$@"; do
  case "${arg}" in
    --skip-model) SKIP_MODEL=true ;;
    --model=*) MODEL_ID="${arg#*=}" ;;
    *)
      echo "Uso: ./install_imac.sh [--skip-model] [--model=turbo-q8|large-v3-q5|turbo-q5|small-q8|base-q8]"
      exit 1
      ;;
  esac
done

# Model IDs must match hardware_imac2017.MODEL_CATALOG. Sizes are the download
# sizes shown to the user, not RAM usage.
case "${MODEL_ID}" in
  turbo-q8) MODEL_FILE="ggml-large-v3-turbo-q8_0.bin" ; MODEL_SIZE="~834 MB" ;;
  large-v3-q5) MODEL_FILE="ggml-large-v3-q5_0.bin" ; MODEL_SIZE="~1080 MB" ;;
  turbo-q5) MODEL_FILE="ggml-large-v3-turbo-q5_0.bin" ; MODEL_SIZE="~547 MB" ;;
  small-q8) MODEL_FILE="ggml-small-q8_0.bin" ; MODEL_SIZE="~252 MB" ;;
  base-q8) MODEL_FILE="ggml-base-q8_0.bin" ; MODEL_SIZE="~82 MB" ;;
  *)
    echo "Modelo no reconocido: ${MODEL_ID}"
    exit 1
    ;;
esac
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_FILE}"

echo "=== MTTN AutoSubs Tool (La Matatena) — Instalador macOS + DaVinci Resolve 20 ==="
echo "Sistema: $(uname -s) $(uname -m)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Aviso: este script está diseñado para macOS. Creando solo estructura local."
fi

# --- Homebrew dependencies ---
# Intel Homebrew lives in /usr/local, Apple Silicon in /opt/homebrew.
BREW_BIN=""
if [[ -x "/usr/local/bin/brew" ]]; then
  BREW_BIN="/usr/local/bin/brew"
elif [[ -x "/opt/homebrew/bin/brew" ]]; then
  BREW_BIN="/opt/homebrew/bin/brew"
elif command -v brew >/dev/null 2>&1; then
  BREW_BIN="$(command -v brew)"
fi

BREW_FAILED=()
if [[ -n "${BREW_BIN}" ]]; then
  echo "Homebrew detectado: ${BREW_BIN}"
  # ffmpeg-full (keg-only) adds libass, required to burn subtitles into MP4;
  # the slim ffmpeg formula only covers audio preparation for Whisper.
  # A failed formula (e.g. ffmpeg-full not building on an older macOS) must
  # not abort the install under `set -e`: the Resolve scripts, templates, and
  # model below are still useful, and the formula can be retried by hand.
  for pkg in whisper-cpp ffmpeg ffmpeg-full; do
    if ! "${BREW_BIN}" list "${pkg}" >/dev/null 2>&1; then
      echo "Instalando ${pkg} mediante Homebrew..."
      if ! "${BREW_BIN}" install "${pkg}"; then
        BREW_FAILED+=("${pkg}")
        echo "Aviso: no se pudo instalar ${pkg}. Reinténtalo más tarde con: brew install ${pkg}"
      fi
    else
      echo "✓ ${pkg} ya está instalado."
    fi
  done
else
  echo "Aviso: Homebrew no detectado. Instala Homebrew (https://brew.sh) y ejecuta:"
  echo "  brew install whisper-cpp ffmpeg ffmpeg-full"
fi

# --- App data and Resolve assets ---
echo "Creando directorios de puente, fuentes y modelos en: ${APP_DATA_DIR}"
mkdir -p "${APP_DATA_DIR}/models" "${APP_DATA_DIR}/fonts" "${APP_DATA_DIR}/bridge/inbox" "${APP_DATA_DIR}/bridge/outbox" "${APP_DATA_DIR}/cache"

echo "Instalando scripts Lua en DaVinci Resolve 20: ${RESOLVE_SCRIPTS_DIR}"
mkdir -p "${RESOLVE_SCRIPTS_DIR}"
# The installed names ("1 - MTTN AutoSubs - ...") are what users see in
# Workspace > Scripts and are referenced by the docs and server messages.
cp "${SCRIPT_DIR}/lua/1_AutoSubs_ES_Exportar_Audio.lua" "${RESOLVE_SCRIPTS_DIR}/1 - MTTN AutoSubs - Exportar Audio.lua"
cp "${SCRIPT_DIR}/lua/2_AutoSubs_ES_Insertar_Subtitulos.lua" "${RESOLVE_SCRIPTS_DIR}/2 - MTTN AutoSubs - Insertar Subtitulos.lua"
cp "${SCRIPT_DIR}/lua/3_AutoSubs_ES_Ir_A_Tiempo.lua" "${RESOLVE_SCRIPTS_DIR}/3 - MTTN AutoSubs - Ir a Tiempo.lua"

echo "Instalando 5 plantillas Text+ GPU-Light en: ${RESOLVE_TEMPLATES_DIR}"
mkdir -p "${RESOLVE_TEMPLATES_DIR}"
cp "${SCRIPT_DIR}/templates/"*.setting "${RESOLVE_TEMPLATES_DIR}/"

chmod +x "${SCRIPT_DIR}/install_imac.sh" "${SCRIPT_DIR}/Iniciar AutoSubs ES.command"

# --- Whisper model download ---
if [[ "${SKIP_MODEL}" == "false" ]]; then
  TARGET_MODEL="${APP_DATA_DIR}/models/${MODEL_FILE}"
  if [[ -f "${TARGET_MODEL}" ]]; then
    echo "✓ Modelo español recomendado ya instalado: ${TARGET_MODEL}"
  else
    echo "Descargando modelo español recomendado (${MODEL_FILE}, ${MODEL_SIZE})..."
    # Download to ".part" and rename only on success so an interrupted run
    # never leaves a truncated model that the app would treat as installed.
    # "-C -" resumes an earlier partial download.
    CURL_RC=0
    curl -fL -C - "${MODEL_URL}" -o "${TARGET_MODEL}.part" || CURL_RC=$?
    if [[ "${CURL_RC}" -ne 0 ]]; then
      # curl exit 22 (HTTP error, e.g. 416 Range Not Satisfiable) or 33 (range
      # not supported) usually means the ".part" is already complete or the
      # resume failed. Compare sizes; otherwise restart from scratch.
      if [[ -f "${TARGET_MODEL}.part" && ( "${CURL_RC}" -eq 22 || "${CURL_RC}" -eq 33 ) ]]; then
        # Last Content-Length after redirects (-L) is the real file size.
        REMOTE_SIZE="$(curl -fsIL "${MODEL_URL}" | awk 'tolower($1)=="content-length:"{len=$2} END{gsub(/\r/,"",len); print len+0}')"
        LOCAL_SIZE="$(stat -f%z "${TARGET_MODEL}.part" 2>/dev/null || echo 0)"
        if [[ "${REMOTE_SIZE}" -gt 0 && "${LOCAL_SIZE}" -eq "${REMOTE_SIZE}" ]]; then
          echo "✓ Descarga parcial ya completa (${LOCAL_SIZE} bytes)."
        else
          rm -f "${TARGET_MODEL}.part"
          curl -fL "${MODEL_URL}" -o "${TARGET_MODEL}.part"
        fi
      else
        exit "${CURL_RC}"
      fi
    fi
    mv "${TARGET_MODEL}.part" "${TARGET_MODEL}"
    echo "✓ Modelo descargado correctamente."
  fi
else
  echo "Descarga del modelo omitida (--skip-model). Puedes descargarlo desde la app."
fi

# --- Optional native window (pywebview) ---
# Isolated Python for the native window (pywebview); never touches the system
# Python. The server itself stays stdlib-only and works without this venv.
VENV_DIR="${APP_DATA_DIR}/venv"
HOST_PYTHON=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  if [[ -n "${cand}" && -x "${cand}" ]]; then HOST_PYTHON="${cand}"; break; fi
done
if [[ -n "${HOST_PYTHON}" ]]; then
  echo "Preparando ventana nativa (pywebview) en: ${VENV_DIR}"
  # pywebview is optional, so a venv failure (e.g. python3 without the venv
  # module) must not abort the install under `set -e`. A half-created venv is
  # removed so the next run retries from scratch; the launcher then falls back
  # to browser mode because the venv python is missing.
  VENV_READY=true
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    if ! "${HOST_PYTHON}" -m venv "${VENV_DIR}"; then
      VENV_READY=false
      rm -rf "${VENV_DIR}"
      echo "Aviso: no se pudo crear el entorno virtual de Python; la app se abrirá en Chrome/Brave/Safari."
    fi
  fi
  if [[ "${VENV_READY}" == "true" ]]; then
    if "${VENV_DIR}/bin/pip" install --quiet --disable-pip-version-check --upgrade pywebview; then
      echo "✓ pywebview instalado: la app abrirá en su propia ventana."
    else
      echo "Aviso: no se pudo instalar pywebview; la app se abrirá en Chrome/Brave/Safari."
    fi
  fi
else
  echo "Aviso: no se encontró python3; instala Python con: brew install python"
fi

# --- macOS app bundle ---

#######################################
# Writes a minimal macOS .app bundle that launches MTTN AutoSubs Tool.
# The generated Contents/MacOS/AutoSubsES launcher hardcodes this checkout's
# path (SCRIPT_DIR) and the app data dir at install time; escaped \${...}
# variables in the heredoc are expanded later, when the app runs. At runtime
# it prefers the pywebview venv (--desktop, server lives with the window);
# otherwise it starts the server in the background on 127.0.0.1:55120 if it
# is not already up and opens it as a Chrome/Brave app window or in the
# default browser.
# Globals:
#   SCRIPT_DIR
#   APP_DATA_DIR
# Arguments:
#   $1: Absolute path of the bundle to create, e.g.
#       "${HOME}/Applications/MTTN AutoSubs Tool.app". Overwrites files in an
#       existing bundle.
# Outputs:
#   Writes Info.plist, the launcher script, and AppIcon.icns (when
#   assets/AppIcon.icns exists) inside the bundle.
# Returns:
#   0 on success; any failing command aborts the script (set -e).
#######################################
create_macos_app_bundle() {
  local bundle_path="$1"
  local contents_dir="${bundle_path}/Contents"
  mkdir -p "${contents_dir}/MacOS" "${contents_dir}/Resources"
  # Unquoted heredoc delimiters: ${...} expands now (install time); \${...}
  # is written literally and expands when the launcher runs.
  cat > "${contents_dir}/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>MTTN AutoSubs Tool</string>
  <key>CFBundleDisplayName</key>
  <string>MTTN AutoSubs Tool</string>
  <key>CFBundleIdentifier</key>
  <string>com.lamatatena.mttn.autosubstool</string>
  <key>CFBundleVersion</key>
  <string>1.2.0</string>
  <key>CFBundleExecutable</key>
  <string>AutoSubsES</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
</dict>
</plist>
PLIST
  cat > "${contents_dir}/MacOS/AutoSubsES" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:\${PATH}"
APP_HOME="${SCRIPT_DIR}"
LOG_FILE="${APP_DATA_DIR}/cache/server.log"
mkdir -p "${APP_DATA_DIR}/cache"
VENV_PY="${APP_DATA_DIR}/venv/bin/python"
# Ventana nativa: el servidor vive mientras la ventana esté abierta.
if [[ -x "\${VENV_PY}" ]] && "\${VENV_PY}" -c "import webview" >/dev/null 2>&1; then
  cd "\${APP_HOME}"
  exec "\${VENV_PY}" -B autosubs_server.py --desktop >> "\${LOG_FILE}" 2>&1
fi
PYTHON_BIN="/usr/local/bin/python3"
if [[ ! -x "\${PYTHON_BIN}" ]]; then
  PYTHON_BIN="\$(command -v python3)"
fi
if ! curl -fsS "http://127.0.0.1:55120/api/status" >/dev/null 2>&1; then
  cd "\${APP_HOME}"
  nohup "\${PYTHON_BIN}" -B autosubs_server.py --no-browser > "\${LOG_FILE}" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fsS "http://127.0.0.1:55120/api/status" >/dev/null 2>&1; then
      break
    fi
    sleep 0.3
  done
fi
if [[ -d "/Applications/Google Chrome.app" ]]; then
  open -na "/Applications/Google Chrome.app" --args "--app=http://127.0.0.1:55120"
elif [[ -d "/Applications/Brave Browser.app" ]]; then
  open -na "/Applications/Brave Browser.app" --args "--app=http://127.0.0.1:55120"
else
  open "http://127.0.0.1:55120"
fi
LAUNCHER
  chmod +x "${contents_dir}/MacOS/AutoSubsES"
  if [[ -f "${SCRIPT_DIR}/assets/AppIcon.icns" ]]; then
    cp "${SCRIPT_DIR}/assets/AppIcon.icns" "${contents_dir}/Resources/AppIcon.icns"
  fi
  # Touch the bundle so Finder/Dock refresh the icon if the app already existed.
  touch "${bundle_path}"
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  mkdir -p "${HOME}/Applications"
  create_macos_app_bundle "${HOME}/Applications/MTTN AutoSubs Tool.app"
  echo "✓ App de escritorio creada en: ${HOME}/Applications/MTTN AutoSubs Tool.app"
fi

echo ""
echo "Instalación completada."
# Only the count is read while the array may be empty: expanding an empty
# "${arr[*]}" trips `set -u` on macOS's bash 3.2.
if [[ ${#BREW_FAILED[@]} -gt 0 ]]; then
  echo "Aviso: faltan paquetes de Homebrew (${BREW_FAILED[*]}). Instálalos con: brew install ${BREW_FAILED[*]}"
fi
echo "1. Abre ~/Applications/MTTN AutoSubs Tool.app (Spotlight/Launchpad) o haz doble clic en 'Iniciar AutoSubs ES.command'"
echo "2. Usa MTTN AutoSubs Tool de forma independiente o con DaVinci Resolve 20: Workspace > Scripts > 1 - MTTN AutoSubs - Exportar Audio"
