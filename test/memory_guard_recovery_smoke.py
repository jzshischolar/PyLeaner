#!/usr/bin/env python3
"""Fault-injection smoke test for hard cgroup memory recovery."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient, ToxicTaskError  # noqa: E402


def main() -> int:
    # A deliberately slow poll simulates the watchdog not being scheduled in
    # time.  The kernel cgroup limit must contain the worker until observation
    # resumes and online recovery runs.
    client = LspClient(
        server_cmd=["lake", "serve"],
        cwd=str(PROJECT_ROOT),
        worker_memory_high_bytes=95 * 1024**2,
        worker_memory_max_bytes=96 * 1024**2,
        watchdog_memory_poll_interval=20.0,
    )
    try:
        client.connect(timeout=300)
        client.create_pool(text="-- guarded base environment\n", size=1)
        try:
            client.submit_resilient(
                "changecontent",
                {
                    "text": "#eval (List.range 100000000).length\n",
                    "content_range": {},
                },
                timeout=60,
            )
        except ToxicTaskError as exc:
            print("received expected toxic feedback:", exc.reason)
        else:
            raise AssertionError("memory-heavy Lean task was not rejected")

        result = client.submit_resilient(
            "changecontent",
            {"text": "example : True := by trivial\n", "content_range": {}},
            timeout=60,
        )
        assert isinstance(result, list)
        assert client.watchdog._process is not None
        assert client.watchdog._process.is_alive()
        print("post-OOM Lean call passed on the rebuilt worker pool")
        return 0
    finally:
        client.exit()


if __name__ == "__main__":
    raise SystemExit(main())
