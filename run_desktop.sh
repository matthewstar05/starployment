#!/usr/bin/env bash
# Start the native Starployment window from a checkout, or open a packaged build.
set -euo pipefail
cd "$(dirname "$0")"

python_is_312() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1
}

checkout_data_dir() {
  local base installed
  if [[ "$(uname -s)" == "Darwin" ]]; then
    base="$HOME/Library/Application Support"
    set -- "$base/Starployment" "$base/JobHub"
  else
    base="${XDG_DATA_HOME:-$HOME/.local/share}"
    set -- "$base/starployment" "$base/jobhub"
  fi
  for installed in "$@"; do
    if [[ -f "$installed/.checkout-import.json" ]]; then
      printf '%s' "$installed"
      return
    fi
  done
  printf '%s' "$PWD/data"
}

if [[ -x "$PWD/.venv/bin/python" ]] && python_is_312 "$PWD/.venv/bin/python"; then
  export JOBHUB_DATA_DIR="${JOBHUB_DATA_DIR:-$(checkout_data_dir)}"
  exec "$PWD/.venv/bin/python" -m jobhub.desktop "$@"
fi
if [[ -x "$PWD/dist/Starployment.app/Contents/MacOS/Starployment" ]]; then
  exec "$PWD/dist/Starployment.app/Contents/MacOS/Starployment" "$@"
fi
if [[ -x "$PWD/dist/Starployment/Starployment" ]]; then
  exec "$PWD/dist/Starployment/Starployment" "$@"
fi
export JOBHUB_DATA_DIR="${JOBHUB_DATA_DIR:-$(checkout_data_dir)}"

for python in python3.12 python3; do
  if command -v "$python" >/dev/null 2>&1 && python_is_312 "$(command -v "$python")"; then
    exec "$(command -v "$python")" -m jobhub.desktop "$@"
  fi
done

echo "Starployment needs Python 3.12. Run 'uv sync --frozen' to create the project environment, then try again." >&2
exit 1
