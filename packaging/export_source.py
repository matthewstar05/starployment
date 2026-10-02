"""Create a portable, candidate-free Starployment source archive.

The allowlist is intentional: a checkout may contain resumes, mailbox secrets,
browser state, application screenshots, and other local files outside the app.
Only source, generic documentation, tests, and build assets can enter the zip.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("jobhub", "web", "scripts", "packaging", "tests", "docs", ".github")
ROOT_FILES = (
    "README.md", "AGENTS.md", "CLAUDE.md", "pyproject.toml", "uv.lock", ".python-version", ".gitignore", ".env.example",
    "run_desktop.sh", "run_desktop.bat", "run_desktop.command", "run_setup.sh", "run_setup.bat",
    "Launch Starployment.command", "Set up Starployment.command", "run.sh",
)
SKIP_PARTS = {"__pycache__", "playwright-browsers", ".pytest_cache", ".ruff_cache"}
SKIP_NAMES = {".DS_Store", ".env"}
SKIP_SUFFIXES = {".pyc", ".db", ".sqlite", ".sqlite3", ".pdf"}


def source_files(root: Path) -> list[Path]:
    """Use explicit source roots, never a whole-checkout archive."""
    candidates = [root / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        directory = root / name
        if directory.is_dir():
            candidates.extend(directory.rglob("*"))
    files = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if (any(part in SKIP_PARTS for part in relative.parts)
                or path.name in SKIP_NAMES or path.suffix.lower() in SKIP_SUFFIXES):
            continue
        files.append(relative)
    return sorted(set(files))


def build_archive(root: Path = ROOT, output: Path | None = None) -> tuple[Path, int, str]:
    output = output or root / "dist" / "Starployment-source.zip"
    files = source_files(root)
    if not files or Path("jobhub/__init__.py") not in files or Path("README.md") not in files:
        raise RuntimeError("The source checkout is incomplete; refusing to create a partial archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for relative in files:
            archive.write(root / relative, arcname=(Path("Starployment") / relative).as_posix())
    with zipfile.ZipFile(output) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise RuntimeError(f"Source archive failed its integrity check: {bad_file}")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return output, len(files), digest


def main() -> int:
    output, count, digest = build_archive()
    print(f"Created {output} with {count} candidate-free source files")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
