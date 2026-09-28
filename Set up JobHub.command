#!/usr/bin/env bash
# Double-click in Finder to edit your JobHub candidate data and question bank.
set -euo pipefail
cd "$(dirname "$0")"
exec "$PWD/run_desktop.command" --setup
