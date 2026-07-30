#!/usr/bin/env python3
"""Verify that an unexpectedly killed monitor process is recreated online."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient  # noqa: E402


def main() -> int:
    client = LspClient(
        server_cmd=["lake", "serve"],
        cwd=str(PROJECT_ROOT),
    )
    try:
        client.connect(timeout=300)
        client.create_pool(text="-- base environment\n", size=1)
        old_server_pid = client.process.pid
        old_monitor_pid = client.watchdog._process.pid
        os.kill(old_monitor_pid, signal.SIGKILL)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            monitor = client.watchdog._process
            server = client.process
            if (
                monitor is not None
                and monitor.is_alive()
                and monitor.pid != old_monitor_pid
                and server is not None
                and server.poll() is None
                and server.pid != old_server_pid
            ):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("watchdog/server were not rebuilt in time")

        result = client.submit_resilient(
            "changecontent",
            {"text": "example : True := by trivial\n", "content_range": {}},
            timeout=60,
        )
        assert isinstance(result, list)
        print(
            "watchdog process recovery passed:",
            f"monitor={old_monitor_pid}->{client.watchdog._process.pid}",
            f"server={old_server_pid}->{client.process.pid}",
        )
        return 0
    finally:
        client.exit()


if __name__ == "__main__":
    raise SystemExit(main())
