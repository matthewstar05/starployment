#!/bin/bash
# Double-click in Finder to open the native JobHub desktop app.
set -euo pipefail
cd "$(dirname "$0")"
exec "$PWD/run_desktop.command" "$@"
