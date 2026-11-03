"""Exercise a frozen Starployment bundle without reading or writing candidate data."""
from __future__ import annotations

import os
import argparse
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zipfile
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def executable_path(platform: str = sys.platform, root: Path = ROOT) -> Path:
    if platform == "darwin":
        return root / "dist" / "Starployment.app" / "Contents" / "MacOS" / "Starployment"
    if platform == "win32":
        return root / "dist" / "Starployment" / "Starployment.exe"
    return root / "dist" / "Starployment" / "Starployment"


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


def check_bundle(executable: Path, screenshots: Path | None = None) -> int:
    if not executable.is_file():
        raise SystemExit(f"No native bundle found at {executable}")
    validate_executable_platform(executable)

    with tempfile.TemporaryDirectory(prefix="starployment-bundle-smoke-") as temporary:
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
        setup_screenshot = temporary_dir / "setup.png"
        subprocess.run([str(executable), "--setup", "--smoke-test", str(setup_screenshot)],
                       check=True, env=env, timeout=90)
        setup_image = setup_screenshot.read_bytes()
        if not setup_image.startswith(b"\x89PNG\r\n\x1a\n") or len(setup_image) < 24:
            raise RuntimeError("The first-run setup wizard did not render a valid PNG")
        setup_width, setup_height = struct.unpack(">II", setup_image[16:24])
        if setup_width < 100 or setup_height < 100:
            raise RuntimeError("The first-run setup wizard rendered at an invalid size")
        connections_screenshot = setup_screenshot.with_name("setup-connections.png")
        connections_image = connections_screenshot.read_bytes()
        if not connections_image.startswith(b"\x89PNG\r\n\x1a\n") or len(connections_image) < 24:
            raise RuntimeError("The first-run Connections page did not render a valid PNG")
        if screenshots is not None:
            import shutil
            screenshots.mkdir(parents=True, exist_ok=True)
            shutil.copy2(screenshot, screenshots / "window.png")
            shutil.copy2(setup_screenshot, screenshots / "setup.png")
            shutil.copy2(connections_screenshot, screenshots / "setup-connections.png")

    print(f"Native dependencies, browser, {width}x{height} window and {setup_width}x{setup_height} setup checks passed on {sys.platform}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="extract and test the actual distributable archive")
    parser.add_argument("--screenshots", type=Path, help="retain fictional first-run screenshots for visual review")
    args = parser.parse_args(argv)
    if args.archive is None:
        return check_bundle(executable_path(), args.screenshots)
    # Relocation into a path with spaces and non-ASCII characters catches
    # dependencies that accidentally point back at the build checkout/cache.
    with tempfile.TemporaryDirectory(prefix="Starployment new user α ") as temporary:
        extracted = Path(temporary)
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(args.archive.resolve()), str(extracted)], check=True)
        elif zipfile.is_zipfile(args.archive):
            with zipfile.ZipFile(args.archive) as archive:
                archive.extractall(extracted)
        else:
            with tarfile.open(args.archive) as archive:
                archive.extractall(extracted, filter="data")
        relative = executable_path().relative_to(ROOT / "dist")
        return check_bundle(extracted / relative, args.screenshots)


if __name__ == "__main__":
    raise SystemExit(main())
