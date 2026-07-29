from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from robinhood.browser_functions.firefox import Firefox

# TODO: Browser code is currently being refactored, and its tests are
# cluttered across multiple files. Consolidate the browser tests after the
# refactor is complete.

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Firefox process-group cleanup is not implemented on Windows",
)

RESISTANT_PROCESS_CODE = """
import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(os.getpid(), flush=True)
time.sleep(60)
"""


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False

    if sys.platform == "linux":
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            state = stat_path.read_text().rpartition(")")[2].split()[0]
            return state != "Z"
    return True


def _wait_until_process_stops(pid: int, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while _process_is_running(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"Process {pid} is still running")
        time.sleep(0.05)


class TestFirefoxCloseProcess:
    def test_closes_child_when_leader_exited_before_cleanup(self) -> None:
        leader_code = (
            "import subprocess, sys; "
            "subprocess.Popen("
            f"[sys.executable, '-c', {RESISTANT_PROCESS_CODE!r}]"
            ")"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code],
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        if leader.stdout is None:
            raise AssertionError("Leader stdout was not captured")
        child_pid = int(leader.stdout.readline().decode())
        leader.wait(timeout=1)

        try:
            Firefox.__new__(Firefox).close_process(leader, timeout=0.1)

            assert leader.returncode == 0
            _wait_until_process_stops(child_pid)
        finally:
            leader.stdout.close()
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            leader.wait()

    def test_force_kills_and_reaps_resistant_leader(self) -> None:
        leader = subprocess.Popen(
            [sys.executable, "-c", RESISTANT_PROCESS_CODE],
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        if leader.stdout is None:
            raise AssertionError("Leader stdout was not captured")
        leader.stdout.readline()

        try:
            Firefox.__new__(Firefox).close_process(leader, timeout=0.1)

            assert leader.returncode == -signal.SIGKILL
        finally:
            leader.stdout.close()
            try:
                os.killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            leader.wait()
