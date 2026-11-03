"""Native process-liveness checks used when recovering interrupted work."""
from __future__ import annotations

import subprocess
import sys

import pytest

from jobhub.platform_support import process_alive


def test_process_alive_tracks_a_child_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert process_alive(child.pid)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not process_alive(child.pid)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Windows OpenProcess invalid-PID behavior")
def test_process_alive_treats_invalid_windows_pid_as_dead():
    assert not process_alive(2_147_483_647)
