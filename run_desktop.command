#!/usr/bin/env bash
# Finder-friendly macOS launcher. Keep this window open for diagnostics.
set -euo pipefail
cd "$(dirname "$0")"
exec "$PWD/run_desktop.sh" "$@"
