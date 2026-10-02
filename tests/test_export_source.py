"""Portable source releases must never pick up candidate files nearby."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "jobhub_export_source", Path(__file__).resolve().parents[1] / "packaging" / "export_source.py"
)
assert SPEC is not None and SPEC.loader is not None
export_source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export_source)


def test_source_archive_uses_allowlist_and_excludes_candidate_data(tmp_path: Path):
    def write(relative: str, content: str = "safe") -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    write("README.md")
    write("jobhub/__init__.py")
    write("jobhub/app.py")
    write("packaging/build_desktop.py")
    write("data/jobhub.db", "candidate")
    write(".env", "secret")
    write("tmp/application-screenshot.png", "candidate")
    write("packaging/playwright-browsers/chromium/profile", "session")
    write("jobhub/__pycache__/app.pyc", "cache")
    write("docs/notes.pdf", "personal")
    external = tmp_path / "private-resume.pdf"
    external.write_text("resume")
    (tmp_path / "jobhub" / "shortcut.py").symlink_to(external)

    archive_path, count, digest = export_source.build_archive(tmp_path)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert names == {
            "Starployment/README.md", "Starployment/jobhub/__init__.py", "Starployment/jobhub/app.py",
            "Starployment/packaging/build_desktop.py",
        }
        assert archive.read("Starployment/jobhub/app.py") == b"safe"
    assert count == 4
    assert len(digest) == 64
