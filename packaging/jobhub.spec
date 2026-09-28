"""PyInstaller spec for the native JobHub desktop application.

Build with ``packaging/build_desktop.py``. The build script downloads Chromium
to ``packaging/playwright-browsers`` first, so the result does not depend on a
developer's Playwright cache.
"""
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata
from tls_client.cffi import file_ext as tls_library_suffix


ROOT = Path(SPECPATH).parent
web_dir = ROOT / "web"
browser_dir = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ROOT / "packaging" / "playwright-browsers"))

datas = [(str(web_dir), "web")]
lucide_dir = ROOT / "packaging" / "assets" / "lucide"
icon_png = ROOT / "packaging" / "assets" / "jobhub-icon-256.png"
icon_file = ROOT / "packaging" / "assets" / (
    "jobhub.icns" if sys.platform == "darwin" else "jobhub.ico" if os.name == "nt" else "jobhub-icon-256.png"
)
datas.append((str(icon_png), "assets"))
datas.append((str(lucide_dir), "assets/lucide"))
datas.extend(copy_metadata("keyring"))

# Playwright's browser bundle is copied into the finished app by
# build_desktop.py. Including the nested Chrome.app here makes PyInstaller try
# to sign its internal frameworks as ordinary binaries on macOS.

hiddenimports = (
    collect_submodules("jobhub")
    + collect_submodules("keyring.backends")
    + ["jobhub.desktop", "jobhub.desktop.__main__"]
)

# JobSpy imports tls_client, whose cffi module loads a platform-specific Go
# library by filename. PyInstaller does not detect that ctypes path, and its
# default library patterns do not match the Linux tls-client-*.so names. Use
# the exact library chosen by tls_client itself so a thin macOS build does not
# try to include a binary for the other CPU architecture.
tls_library_pattern = f"tls-client{tls_library_suffix}"
tls_libraries = collect_dynamic_libs("tls_client", search_patterns=[tls_library_pattern])
if not tls_libraries:
    raise RuntimeError(f"No tls_client native library matched {tls_library_pattern}")

a = Analysis(
    [str(ROOT / "jobhub" / "desktop" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=tls_libraries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "runtime_hook.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    name="JobHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(icon_file),
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JobHub",
)

if sys.platform == "darwin":
    app = BUNDLE(coll, name="JobHub.app", icon=str(icon_file), bundle_identifier="local.jobhub.desktop")
else:
    app = coll
