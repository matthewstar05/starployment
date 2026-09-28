#!/usr/bin/env bash
# Start the native JobHub window from a checkout, or open a packaged build.
set -euo pipefail
cd "$(dirname "$0")"

python_is_312() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1
}

checkout_data_dir() {
  local installed
  if [[ "$(uname -s)" == "Darwin" ]]; then
    installed="$HOME/Library/Application Support/JobHub"
  else
    installed="${XDG_DATA_HOME:-$HOME/.local/share}/jobhub"
  fi
  if [[ -f "$installed/.checkout-import.json" ]]; then
    printf '%s' "$installed"
  else
    printf '%s' "$PWD/data"
  fi
}

if [[ -x "$PWD/.venv/bin/python" ]] && python_is_312 "$PWD/.venv/bin/python"; then
  export JOBHUB_DATA_DIR="${JOBHUB_DATA_DIR:-$(checkout_data_dir)}"
  exec "$PWD/.venv/bin/python" -m jobhub.desktop "$@"
fi
if [[ -x "$PWD/dist/JobHub.app/Contents/MacOS/JobHub" ]]; then
  exec "$PWD/dist/JobHub.app/Contents/MacOS/JobHub" "$@"
fi
if [[ -x "$PWD/dist/JobHub/JobHub" ]]; then
  exec "$PWD/dist/JobHub/JobHub" "$@"
fi
export JOBHUB_DATA_DIR="${JOBHUB_DATA_DIR:-$(checkout_data_dir)}"

for python in python3.12 python3; do
  if command -v "$python" >/dev/null 2>&1 && python_is_312 "$(command -v "$python")"; then
    exec "$(command -v "$python")" -m jobhub.desktop "$@"
  fi
done

echo "JobHub needs Python 3.12. Run 'uv sync --frozen' to create the project environment, then try again." >&2
exit 1
