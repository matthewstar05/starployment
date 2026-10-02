"""Private data path and POSIX permission safeguards."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from jobhub import config, db


@pytest.mark.parametrize("platform,variable,invalid,suffix", [
    ("win32", "LOCALAPPDATA", "", Path("AppData/Local/Starployment")),
    ("win32", "LOCALAPPDATA", "relative-data", Path("AppData/Local/Starployment")),
    ("linux", "XDG_DATA_HOME", "", Path(".local/share/starployment")),
    ("linux", "XDG_DATA_HOME", "relative-data", Path(".local/share/starployment")),
])
def test_invalid_platform_data_home_uses_home_default(monkeypatch, tmp_path, platform, variable, invalid, suffix):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv(variable, invalid)
    monkeypatch.delenv("JOBHUB_DATA_DIR", raising=False)
    monkeypatch.delenv("STARPLOYMENT_DATA_DIR", raising=False)
    assert config._data_dir() == tmp_path / suffix


@pytest.mark.parametrize("platform, base, legacy", [
    ("darwin", Path("Library/Application Support"), "JobHub"),
    ("linux", Path(".local/share"), "jobhub"),
])
def test_existing_jobhub_data_is_used_in_place_after_the_rename(monkeypatch, tmp_path, platform, base, legacy):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("JOBHUB_DATA_DIR", raising=False)
    monkeypatch.delenv("STARPLOYMENT_DATA_DIR", raising=False)
    previous = tmp_path / base / legacy
    previous.mkdir(parents=True)
    (previous / "jobhub.db").write_bytes(b"")
    assert config._data_dir() == previous
    # Once a Starployment folder exists, it wins; nothing is moved or copied.
    current = tmp_path / base / ("Starployment" if platform == "darwin" else "starployment")
    current.mkdir()
    assert config._data_dir() == current
    assert (previous / "jobhub.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_secure_data_tree_repairs_existing_directories_and_files(tmp_path):
    root = tmp_path / "JobHub"
    browser = root / "browser_profile" / "Default"
    browser.mkdir(parents=True)
    private_file = browser / "Cookies"
    private_file.write_text("fixture")
    os.chmod(root, 0o755)
    os.chmod(browser, 0o755)
    os.chmod(private_file, 0o644)

    config.secure_data_tree(root)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(browser.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_file.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_unsafe_roots_and_unrecognized_populated_directories_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    with pytest.raises(ValueError):
        config.secure_data_tree(Path("/"))
    with pytest.raises(ValueError):
        config.secure_data_tree(tmp_path / "home")
    unrelated_target = tmp_path / "external"
    unrelated_target.mkdir()
    external_file = unrelated_target / "unrelated.txt"
    external_file.write_text("fixture")
    os.chmod(unrelated_target, 0o755)
    os.chmod(external_file, 0o644)
    data_link = tmp_path / "JobHub"
    data_link.symlink_to(unrelated_target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        config.secure_data_tree(data_link)
    assert stat.S_IMODE(unrelated_target.stat().st_mode) == 0o755
    assert stat.S_IMODE(external_file.stat().st_mode) == 0o644

    unrelated = tmp_path / "shared"
    unrelated.mkdir()
    payload = unrelated / "keep.txt"
    payload.write_text("fixture")
    os.chmod(unrelated, 0o755)
    os.chmod(payload, 0o644)
    with pytest.raises(ValueError):
        config.secure_data_tree(unrelated)
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o755
    assert stat.S_IMODE(payload.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_secure_data_tree_fails_closed_when_chmod_fails(tmp_path, monkeypatch):
    root = tmp_path / "JobHub"
    (root / "resumes").mkdir(parents=True)

    def denied_chmod(self, mode):
        raise PermissionError("fixture permission failure")

    monkeypatch.setattr(Path, "chmod", denied_chmod)
    with pytest.raises(PermissionError, match="fixture permission failure"):
        config.secure_data_tree(root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_sqlite_database_and_wal_are_owner_only(tmp_path, monkeypatch):
    db.close()
    database = tmp_path / "private.db"
    monkeypatch.setattr(config, "DB_PATH", database)
    try:
        with db.conn() as connection:
            connection.execute("CREATE TABLE permissions_probe (value TEXT)")
            connection.execute("INSERT INTO permissions_probe VALUES ('fixture')")
            wal = database.with_name(database.name + "-wal")
            assert wal.exists()
            assert stat.S_IMODE(database.stat().st_mode) == 0o600
            assert stat.S_IMODE(wal.stat().st_mode) == 0o600
    finally:
        db.close()
