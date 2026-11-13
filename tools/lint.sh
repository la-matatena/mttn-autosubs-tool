#!/usr/bin/env bash
#
# Runs every formatter and linter for MTTN AutoSubs Tool (dev-only tooling).
#
# The product itself stays dependency-free; these tools are only needed by
# contributors and CI. CI (.github/workflows/ci-release.yml, job "lint") runs
# this same script, so a clean local run means a green lint job.
#
# Usage:
#   tools/lint.sh          Check only: exits non-zero on any finding.
#   tools/lint.sh --fix    Apply the formatters (pyink, Prettier, StyLua) and
#                          ESLint autofixes, then run every check.
#   tools/lint.sh --help   Print this help.
#
# Tools (pinned; CI installs the same versions):
#   pyink 26.5.1, pylint 4.1.3, shellcheck-py 0.11.0.1   (pip)
#   prettier 3.10.1, eslint 10.14.0, @johnnymorganz/stylua-bin 2.6.0   (npm)
# Each tool is looked up in the git-ignored local install dirs
# .venv-dev/bin and .venv-dev/node/node_modules/.bin first, then on PATH (CI
# installs onto PATH). print_install_help shows how to create the local dirs.

set -euo pipefail

readonly PIP_TOOLS="pyink==26.5.1 pylint==4.1.3 shellcheck-py==0.11.0.1"
readonly NPM_TOOLS="prettier@3.10.1 eslint@10.14.0 @johnnymorganz/stylua-bin@2.6.0"
readonly NPM_REGISTRY="https://registry.npmjs.org/"

# StyLua formats this whole directory.
readonly LUA_DIR="lua"

#######################################
# Prints the commands that install the pinned tools locally.
# Globals:
#   PIP_TOOLS, NPM_TOOLS, NPM_REGISTRY
# Arguments:
#   None
# Outputs:
#   Writes install instructions to stderr.
#######################################
print_install_help() {
  cat >&2 <<EOF

Install the pinned dev tools (git-ignored, inside the repo):
  python3 -m venv .venv-dev
  .venv-dev/bin/pip install ${PIP_TOOLS}
  npm install --prefix .venv-dev/node --registry ${NPM_REGISTRY} \\
    ${NPM_TOOLS}
Then re-run: tools/lint.sh
EOF
}

#######################################
# Resolves a tool to an executable path (local dev dirs, then PATH).
# Globals:
#   REPO_ROOT
# Arguments:
#   $1: Executable name, e.g. "pyink".
# Outputs:
#   Writes the resolved path to stdout, or nothing if the tool is missing.
#######################################
find_tool() {
  local name="$1"
  local candidate
  for candidate in \
    "${REPO_ROOT}/.venv-dev/bin/${name}" \
    "${REPO_ROOT}/.venv-dev/node/node_modules/.bin/${name}"; do
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  done
  command -v "${name}" || true
}

#######################################
# Runs one named step and records whether it failed.
# Globals:
#   FAILED (appended to on failure)
# Arguments:
#   $1: Human-readable step name.
#   $@: Command and arguments to run.
# Outputs:
#   Writes a step header and the tool output to stdout/stderr.
#######################################
run_step() {
  local label="$1"
  shift
  echo "==> ${label}"
  if ! "$@"; then
    FAILED+=("${label}")
  fi
}

#######################################
# Entry point: parses flags, locates tools, and runs fixers then checks.
# Globals:
#   REPO_ROOT (set here), FAILED, LUA_DIR
# Arguments:
#   $1: Optional --fix or --help.
# Outputs:
#   Writes progress and findings to stdout/stderr.
# Returns:
#   0 when every check passes, 1 on findings, 2 on usage or missing tools.
#######################################
main() {
  local fix=0
  case "${1:-}" in
    "") ;;
    --fix) fix=1 ;;
    -h | --help)
      sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
      return 0
      ;;
    *)
      echo "Unknown option: $1 (use --fix or --help)" >&2
      return 2
      ;;
  esac

  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  readonly REPO_ROOT
  cd "${REPO_ROOT}"

  # First-party sources per language (globs expand from the repo root).
  # HTML, site/, templates/*.setting and Markdown are intentionally not
  # formatted (see .prettierignore). No associative arrays or `readonly`
  # arrays: macOS still ships bash 3.2.
  local py_files=(*.py)
  local prettier_files=(
    ui/app.js
    ui/app_helpers.js
    ui/styles.css
    tests/js/*.mjs
    eslint.config.mjs
    .prettierrc.json
  )
  local eslint_files=(ui/app.js ui/app_helpers.js tests/js eslint.config.mjs)
  local shell_files=(install_imac.sh "Iniciar AutoSubs ES.command" tools/lint.sh)

  local pyink pylint shellcheck prettier eslint stylua
  pyink="$(find_tool pyink)"
  pylint="$(find_tool pylint)"
  shellcheck="$(find_tool shellcheck)"
  prettier="$(find_tool prettier)"
  eslint="$(find_tool eslint)"
  stylua="$(find_tool stylua)"
  local missing=()
  [[ -n "${pyink}" ]] || missing+=(pyink)
  [[ -n "${pylint}" ]] || missing+=(pylint)
  [[ -n "${shellcheck}" ]] || missing+=(shellcheck)
  [[ -n "${prettier}" ]] || missing+=(prettier)
  [[ -n "${eslint}" ]] || missing+=(eslint)
  [[ -n "${stylua}" ]] || missing+=(stylua)
  if ((${#missing[@]} > 0)); then
    echo "Missing lint tools: ${missing[*]}" >&2
    print_install_help
    return 2
  fi

  FAILED=()
  if ((fix)); then
    run_step "pyink (format)" "${pyink}" --quiet "${py_files[@]}"
    run_step "prettier (format)" "${prettier}" --log-level warn --write "${prettier_files[@]}"
    run_step "eslint (autofix)" "${eslint}" --fix "${eslint_files[@]}"
    run_step "stylua (format)" "${stylua}" --verify "${LUA_DIR}"
  fi

  run_step "pyink --check" "${pyink}" --check --quiet "${py_files[@]}"
  run_step "pylint" "${pylint}" --rcfile=.pylintrc "${py_files[@]}"
  run_step "prettier --check" "${prettier}" --log-level warn --check "${prettier_files[@]}"
  run_step "eslint" "${eslint}" --max-warnings=0 "${eslint_files[@]}"
  run_step "shellcheck" "${shellcheck}" "${shell_files[@]}"
  run_step "stylua --check" "${stylua}" --check "${LUA_DIR}"

  if ((${#FAILED[@]} > 0)); then
    echo "Lint failed: ${FAILED[*]}" >&2
    if ((!fix)); then
      echo "Formatting-only failures can be fixed with: tools/lint.sh --fix" >&2
    fi
    return 1
  fi
  echo "All lint checks passed."
}

main "$@"
