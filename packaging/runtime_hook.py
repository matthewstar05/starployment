"""Runtime paths for a frozen Starployment process."""
import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    bundled_browsers = bundle_root / "playwright-browsers"
    if bundled_browsers.is_dir():
        # Playwright reads this variable when resolving chromium's executable.
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
