#!/usr/bin/env python3
"""End-to-end test: kill the Lean server, watch PyLeaner's Watchdog revive it.

Verifies the general liveness watchdog:
  1. connect() + create_pool() -> server up, watchdog armed & running.
  2. baseline get_diagnostics -> healthy.
  3. SIGKILL the whole process tree (lake + lean) to simulate a hard crash.
  4. the Watchdog's 20s poll detects the death and auto-restarts (connect +
     create_pool) on the SAME LspClient.
  5. get_diagnostics on the revived server -> responsive again.

Run:
    python test/test_watchdog_e2e.py
"""

import sys
import os
import time
from pathlib import Path

# Test against the source tree (edits are live); the installed copy matches it
# after `pip install`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pyleaner  # noqa: E402
from pyleaner import LspClient  # noqa: E402
from pyleaner.watchdog import _kill_process_tree  # noqa: E402

BASE = "-- base env\n"
CWD = str(PROJECT_ROOT)


def main() -> int:
    print("pyleaner from:", pyleaner.__file__)
    c = LspClient(server_cmd=["lake", "serve"], cwd=CWD)
    try:
        print("\n[1] connect + create_pool ...")
        c.connect(timeout=300)
        c.create_pool(text=BASE, size=1)
        old_pid = c.process.pid
        wd = c.watchdog._thread
        print(f"    server pid={old_pid}; "
              f"watchdog thread alive={wd.is_alive() if wd else False}")

        print("\n[2] baseline get_diagnostics ...")
        diags = c.worker_pool.get_diagnostics(text=BASE, content_range={},
                                              timeout=60.0)
        print(f"    OK -- {len(diags)} diagnostic(s) (healthy)")

        print(f"\n[3] killing the whole server tree (pid={old_pid}) "
              f"to simulate a hard crash ...")
        _kill_process_tree(c.process)
        try:
            print(f"    killed; lake poll={c.process.poll()} "
                  "(non-None == dead)")
        except Exception as e:
            print(f"    kill check note: {e}")

        print("\n[4] waiting for the Watchdog to auto-revive "
              "(polls every 20s) ...")
        revived_pid = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            proc = c.process
            if (proc is not None and proc.pid != old_pid
                    and proc.poll() is None):
                revived_pid = proc.pid
                break
            time.sleep(2)
        if revived_pid is None:
            print("    NOT revived within 90s -- FAIL")
            return 1
        print(f"    new process spawned: pid={revived_pid}")

        # create_pool runs right after connect() inside restart(); give it a
        # moment, then probe until the revived server answers.
        print("\n[5] probing the revived server with get_diagnostics ...")
        time.sleep(12)
        ok = False
        for attempt in range(5):
            try:
                diags = c.worker_pool.get_diagnostics(
                    text=BASE, content_range={}, timeout=30.0)
                print(f"    OK -- {len(diags)} diagnostic(s); "
                      f"revived server is responsive")
                ok = True
                break
            except Exception as e:
                print(f"    attempt {attempt + 1} failed: {e}; "
                      f"retrying in 5s ...")
                time.sleep(5)
        if not ok:
            print("    revived server did not answer in time -- FAIL")
            return 1

        print("\n" + "=" * 60)
        print(f"SUCCESS: Watchdog auto-revived the crashed server "
              f"(pid {old_pid} -> {revived_pid}).")
        print("=" * 60)
        return 0

    finally:
        try:
            c.exit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
