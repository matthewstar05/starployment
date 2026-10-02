#!/usr/bin/env bash
# Local server and hourly scheduler. Keep this window open while Starployment runs.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v uv >/dev/null || { echo "Starployment needs uv. Install it from https://docs.astral.sh/uv/getting-started/installation/ then reopen this launcher."; exit 1; }
uv sync --frozen --quiet
uv run --no-sync python scripts/ensure_browser.py
exec uv run --no-sync jobhub serve "$@"
