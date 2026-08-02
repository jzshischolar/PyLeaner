#!/usr/bin/env python3
"""Real Lean smoke test for process watchdog and per-worker cgroup guards."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient  # noqa: E402


def main() -> int:
    memory_high = 2 * 1024**3
    memory_max = 3 * 1024**3
    client = LspClient(
        server_cmd=["lake", "serve"],
        cwd=str(PROJECT_ROOT),
        worker_memory_high_bytes=memory_high,
        worker_memory_max_bytes=memory_max,
        watchdog_memory_poll_interval=0.25,
    )
    scope_path = None
    try:
        client.connect(timeout=300)
        client.create_pool(text="-- guarded base environment\n", size=1)
        assert client.watchdog._process is not None
        assert client.watchdog._process.is_alive()
        guard = client.watchdog._worker_scopes[1]
        scope_path = Path(guard["cgroup_path"])
        assert (scope_path / "memory.high").read_text().strip() == "max"
        assert int((scope_path / "memory.max").read_text()) == memory_max
        assert int((scope_path / "memory.swap.max").read_text()) == 0
        assert int(guard["pid"]) in {
            int(value)
            for value in (scope_path / "cgroup.procs").read_text().split()
        }
        result = client.submit_resilient(
            "changecontent",
            {"text": "example : True := by trivial\n", "content_range": {}},
            timeout=60,
        )
        assert isinstance(result, list)
        print(
            "memory guard smoke passed:",
            f"monitor_pid={client.watchdog._process.pid}",
            f"worker_pid={guard['pid']}",
            f"memory_current={int((scope_path / 'memory.current').read_text())}",
            f"cgroup={scope_path}",
        )
        return 0
    finally:
        client.exit()
        if scope_path is not None:
            deadline = time.monotonic() + 5
            while scope_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not scope_path.exists()


if __name__ == "__main__":
    raise SystemExit(main())
