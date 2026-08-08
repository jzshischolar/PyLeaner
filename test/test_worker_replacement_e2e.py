#!/usr/bin/env python3
"""End-to-end check for replacement of one logical Lean worker process."""

from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import signal
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient  # noqa: E402
from pyleaner.errors import ToxicTaskError  # noqa: E402
from pyleaner.watchdog import _lean_worker_pids  # noqa: E402


SOURCE = """import LeanLspExtension
namespace WorkerReplacement
def value : Nat := 42
end WorkerReplacement
"""


def main() -> int:
    client = LspClient(server_cmd=["lake", "serve"], cwd=str(PROJECT_ROOT))
    try:
        client.connect(timeout=300)
        client.create_pool(text=SOURCE, size=5)
        root_pid = client.process.pid
        before = _lean_worker_pids(root_pid)
        if set(before) != set(range(1, 6)):
            raise AssertionError(f"could not resolve all worker PIDs: {before}")

        victim_id = 3
        victim_pid = before[victim_id]
        os.kill(victim_pid, signal.SIGKILL)

        def check() -> str:
            try:
                result = client.submit_resilient(
                    "extract_declarations",
                    {"text": SOURCE, "content_range": {}},
                )
            except ToxicTaskError:
                # The task assigned to the externally killed worker is
                # conservatively attributed as toxic; every innocent request
                # must be retried across the pool restart.
                return "toxic"
            assert result.get("success") is True, repr(result)
            assert [item["name"] for item in result["decls"]] == ["value"]
            return "success"

        # Drive every logical worker while the Lean server replaces the victim.
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(check) for _ in range(15)]
            outcomes = [
                future.result()
                for future in concurrent.futures.as_completed(futures)
            ]
        assert outcomes.count("toxic") <= 1
        assert outcomes.count("success") >= 14

        replacement_pid = None
        replacement_root_pid = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current_process = client.process
            if current_process is None or current_process.poll() is not None:
                time.sleep(0.2)
                continue
            current_root_pid = current_process.pid
            current = _lean_worker_pids(current_root_pid).get(victim_id)
            if current is not None and current != victim_pid:
                replacement_pid = current
                replacement_root_pid = current_root_pid
                break
            time.sleep(0.2)
        if replacement_pid is None:
            raise AssertionError(
                f"logical worker {victim_id} was not replaced after PID {victim_pid} died"
            )

        # A fresh post-recovery request must not inherit a stale session or a
        # pending response from the old worker pool.
        assert check() == "success"

        print(
            "logical worker replacement passed:",
            f"worker={victim_id}",
            f"server={root_pid}->{replacement_root_pid}",
            f"pid={victim_pid}->{replacement_pid}",
        )
        return 0
    finally:
        client.exit()


if __name__ == "__main__":
    raise SystemExit(main())
