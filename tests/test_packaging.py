"""Keep the copied browser in the directory the frozen runtime actually reads."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runpy
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("jobhub_build_desktop", ROOT / "packaging" / "build_desktop.py")
assert SPEC is not None and SPEC.loader is not None
build_desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_desktop)
SMOKE_SPEC = importlib.util.spec_from_file_location("jobhub_smoke_bundle", ROOT / "packaging" / "smoke_bundle.py")
assert SMOKE_SPEC is not None and SMOKE_SPEC.loader is not None
smoke_bundle = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(smoke_bundle)


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_browser_copy_target_matches_frozen_runtime_hook(tmp_path: Path, monkeypatch, platform: str):
    runtime_root = build_desktop.frozen_runtime_root(platform, tmp_path)
    if platform == "darwin":
        assert runtime_root == tmp_path / "dist" / "Starployment.app" / "Contents" / "Frameworks"
    else:
        assert runtime_root == tmp_path / "dist" / "Starployment" / "_internal"

    browser_dir = runtime_root / "playwright-browsers"
    browser_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(runtime_root), raising=False)
    # Track the absent state before the runtime hook sets the variable directly.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "test-sentinel")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    runpy.run_path(str(ROOT / "packaging" / "runtime_hook.py"))

    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == browser_dir
    assert runtime_root / "playwright" / "driver" == browser_dir.parent / "playwright" / "driver"


@pytest.mark.parametrize(
    ("header", "platform"),
    [(b"MZ\x90\x00", "win32"), (b"\x7fELF", "linux"), (b"\xcf\xfa\xed\xfe", "darwin")],
)
def test_bundle_smoke_accepts_native_executable_format(tmp_path: Path, header: bytes, platform: str):
    executable = tmp_path / "Starployment"
    executable.write_bytes(header + bytes(32))

    smoke_bundle.validate_executable_platform(executable, platform)


def test_bundle_smoke_rejects_bundle_from_another_operating_system(tmp_path: Path):
    executable = tmp_path / "Starployment"
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(32))

    with pytest.raises(SystemExit, match="expects linux"):
        smoke_bundle.validate_executable_platform(executable, "linux")
