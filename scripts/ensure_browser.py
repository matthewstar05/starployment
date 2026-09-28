"""Install the Playwright browser only when the pinned runtime needs it."""
from pathlib import Path
import subprocess
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    ready = Path(playwright.chromium.executable_path).is_file()
if not ready:
    print('Setting up the application browser (first launch only)…', flush=True)
    subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=True)
