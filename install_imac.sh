#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DATA_DIR="${HOME}/Library/Application Support/AutoSubsES"
RESOLVE_FUSION_DIR="${HOME}/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion"
RESOLVE_SCRIPTS_DIR="${RESOLVE_FUSION_DIR}/Scripts/Utility"
RESOLVE_TEMPLATES_DIR="${RESOLVE_FUSION_DIR}/Templates/Edit/Titles/AutoSubs ES"
MODEL_ID="turbo-q8"
SKIP_MODEL=false

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

echo "=== AutoSubs ES — Instalador App Independiente + DaVinci Resolve 20 (iMac Intel x86_64) ==="
echo "Sistema: $(uname -s) $(uname -m)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Aviso: este script está diseñado para macOS. Creando solo estructura local."
fi

BREW_BIN=""
if [[ -x "/usr/local/bin/brew" ]]; then
  BREW_BIN="/usr/local/bin/brew"
elif [[ -x "/opt/homebrew/bin/brew" ]]; then
  BREW_BIN="/opt/homebrew/bin/brew"
elif command -v brew >/dev/null 2>&1; then
  BREW_BIN="$(command -v brew)"
fi

if [[ -n "${BREW_BIN}" ]]; then
  echo "Homebrew detectado: ${BREW_BIN}"
  for pkg in whisper-cpp ffmpeg; do
    if ! "${BREW_BIN}" list "${pkg}" >/dev/null 2>&1; then
      echo "Instalando ${pkg} mediante Homebrew..."
      "${BREW_BIN}" install "${pkg}"
    else
      echo "✓ ${pkg} ya está instalado."
    fi
  done
else
  echo "Aviso: Homebrew no detectado. Instala Homebrew (https://brew.sh) y ejecuta:"
  echo "  brew install whisper-cpp ffmpeg"
fi

echo "Creando directorios de puente, fuentes y modelos en: ${APP_DATA_DIR}"
mkdir -p "${APP_DATA_DIR}/models" "${APP_DATA_DIR}/fonts" "${APP_DATA_DIR}/bridge/inbox" "${APP_DATA_DIR}/bridge/outbox" "${APP_DATA_DIR}/cache"

echo "Instalando scripts Lua en DaVinci Resolve 20: ${RESOLVE_SCRIPTS_DIR}"
mkdir -p "${RESOLVE_SCRIPTS_DIR}"
cp "${SCRIPT_DIR}/lua/1_AutoSubs_ES_Exportar_Audio.lua" "${RESOLVE_SCRIPTS_DIR}/1 - AutoSubs ES - Exportar Audio.lua"
cp "${SCRIPT_DIR}/lua/2_AutoSubs_ES_Insertar_Subtitulos.lua" "${RESOLVE_SCRIPTS_DIR}/2 - AutoSubs ES - Insertar Subtitulos.lua"
cp "${SCRIPT_DIR}/lua/3_AutoSubs_ES_Ir_A_Tiempo.lua" "${RESOLVE_SCRIPTS_DIR}/3 - AutoSubs ES - Ir a Tiempo.lua"

echo "Instalando 5 plantillas Text+ GPU-Light en: ${RESOLVE_TEMPLATES_DIR}"
mkdir -p "${RESOLVE_TEMPLATES_DIR}"
cp "${SCRIPT_DIR}/templates/"*.setting "${RESOLVE_TEMPLATES_DIR}/"

chmod +x "${SCRIPT_DIR}/install_imac.sh" "${SCRIPT_DIR}/Iniciar AutoSubs ES.command"

if [[ "${SKIP_MODEL}" == "false" ]]; then
  TARGET_MODEL="${APP_DATA_DIR}/models/${MODEL_FILE}"
  if [[ -f "${TARGET_MODEL}" ]]; then
    echo "✓ Modelo español recomendado ya instalado: ${TARGET_MODEL}"
  else
    echo "Descargando modelo español recomendado (${MODEL_FILE}, ${MODEL_SIZE})..."
    curl -fL -C - "${MODEL_URL}" -o "${TARGET_MODEL}.part"
    mv "${TARGET_MODEL}.part" "${TARGET_MODEL}"
    echo "✓ Modelo descargado correctamente."
  fi
else
  echo "Descarga del modelo omitida (--skip-model). Puedes descargarlo desde la app."
fi

create_macos_app_bundle() {
  local bundle_path="$1"
  local contents_dir="${bundle_path}/Contents"
  mkdir -p "${contents_dir}/MacOS" "${contents_dir}/Resources"
  cat > "${contents_dir}/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>AutoSubs ES</string>
  <key>CFBundleDisplayName</key>
  <string>AutoSubs ES</string>
  <key>CFBundleIdentifier</key>
  <string>com.autosubses.app</string>
  <key>CFBundleVersion</key>
  <string>3.0</string>
  <key>CFBundleExecutable</key>
  <string>AutoSubsES</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
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
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  mkdir -p "${HOME}/Applications"
  create_macos_app_bundle "${HOME}/Applications/AutoSubs ES.app"
  echo "✓ App de escritorio creada en: ${HOME}/Applications/AutoSubs ES.app"
fi

echo ""
echo "Instalación completada."
echo "1. Abre ~/Applications/AutoSubs ES.app (Spotlight/Launchpad) o haz doble clic en 'Iniciar AutoSubs ES.command'"
echo "2. Usa AutoSubs ES de forma independiente o con DaVinci Resolve 20: Workspace > Scripts > 1 - AutoSubs ES - Exportar Audio"
