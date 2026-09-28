"""Exercise a frozen JobHub bundle without reading or writing candidate data."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def executable_path(platform: str = sys.platform, root: Path = ROOT) -> Path:
    if platform == "darwin":
        return root / "dist" / "JobHub.app" / "Contents" / "MacOS" / "JobHub"
    if platform == "win32":
        return root / "dist" / "JobHub" / "JobHub.exe"
    return root / "dist" / "JobHub" / "JobHub"


def executable_format(path: Path) -> str:
    """Identify the native executable container without trying to run it."""
    header = path.read_bytes()[:4]
    if header.startswith(b"MZ"):
        return "win32"
    if header == b"\x7fELF":
        return "linux"
    if header in {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    }:
        return "darwin"
    return "unknown"


def validate_executable_platform(path: Path, platform: str = sys.platform) -> None:
    expected = "win32" if platform == "win32" else "darwin" if platform == "darwin" else "linux"
    actual = executable_format(path)
    if actual != expected:
        raise SystemExit(
            f"Bundle executable at {path} is {actual}, but this smoke test expects {expected}. "
            "Build the bundle on the target operating system before testing it."
        )


def main() -> int:
    executable = executable_path()
    if not executable.is_file():
        raise SystemExit(f"No native bundle found at {executable}")
    validate_executable_platform(executable)

    with tempfile.TemporaryDirectory(prefix="jobhub-bundle-smoke-") as temporary:
        temporary_dir = Path(temporary)
        env = os.environ.copy()
        env["JOBHUB_DATA_DIR"] = str(temporary_dir / "candidate-data")
        env["QT_QPA_PLATFORM"] = "offscreen"
        env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        env.pop("PLAYWRIGHT_NODEJS_PATH", None)

        subprocess.run([str(executable), "--dependency-check"], check=True, env=env, timeout=90)
        subprocess.run([str(executable), "--browser-check"], check=True, env=env, timeout=90)
        screenshot = temporary_dir / "window.png"
        subprocess.run([str(executable), "--smoke-test", str(screenshot)], check=True, env=env, timeout=90)
        image = screenshot.read_bytes()
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 24:
            raise RuntimeError("The native window did not produce a valid PNG screenshot")
        width, height = struct.unpack(">II", image[16:24])
        if width < 100 or height < 100:
            raise RuntimeError(f"The native window rendered at an invalid size: {width}x{height}")

    print(f"Native discovery dependency, browser, and {width}x{height} Qt window checks passed on {sys.platform}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
