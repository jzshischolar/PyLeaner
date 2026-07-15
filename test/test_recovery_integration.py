#!/usr/bin/env python3
"""Integration tests for watchdog wedge-recovery against a real Lean server.

Scenarios:
  A. wedge via panic (#eval huge Nat.pow) -> submit_resilient raises ToxicTaskError
  B. an innocent task queued behind the wedge transparently retries on the
     revived server (NOT marked toxic)

Uses a 3s watchdog interval (post-start override) so the tests don't wait the
full 20s production cadence.

Run: python3 test/test_recovery_integration.py
"""

import os
import sys
import time
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient, ToxicTaskError  # noqa: E402

CWD = str(PROJECT_ROOT)
WEDGE = "#eval Nat.pow 10 9999999993\n"   # runtime panic -> wedge (process alive)
TRIVIAL = "-- ok\n"


def _fast_watchdog(c):
    """Restart the watchdog with a 3s interval so tests are quick."""
    c.watchdog.stop()
    c.watchdog.interval = 3.0
    c.watchdog.start()


def _new_client():
    c = LspClient(server_cmd=["lake", "serve"], cwd=CWD)
    c.connect(timeout=300)
    c.create_pool(text=TRIVIAL, size=1)
    _fast_watchdog(c)
    return c


def scenario_a_wedge_to_toxic() -> bool:
    print("\n[A] wedge (panic) -> ToxicTaskError ...")
    c = _new_client()
    try:
        t0 = time.monotonic()
        try:
            c.submit_resilient("changecontent",
                               {"text": WEDGE, "content_range": {}}, timeout=120.0)
            print("    FAIL: expected ToxicTaskError, got success")
            return False
        except ToxicTaskError as e:
            dt = time.monotonic() - t0
            print(f"    PASS: ToxicTaskError in {dt:.1f}s "
                  f"(reason: {e.reason[:60]!r})")
            return True
        except Exception as e:
            print(f"    FAIL: expected ToxicTaskError, got {type(e).__name__}: {e}")
            return False
    finally:
        try:
            c.exit()
        except Exception:
            pass


def scenario_b_innocent_retries() -> bool:
    print("\n[B] innocent task transparently retries across a wedge ...")
    c = _new_client()
    try:
        results = {}

        def wedge_thread():
            try:
                c.submit_resilient("changecontent",
                                   {"text": WEDGE, "content_range": {}}, timeout=180.0)
                results["wedge"] = "no-error(?!)"
            except ToxicTaskError:
                results["wedge"] = "toxic"
            except Exception as e:
                results["wedge"] = f"other:{type(e).__name__}"

        def innocent_thread():
            try:
                c.submit_resilient("changecontent",
                                   {"text": TRIVIAL, "content_range": {}}, timeout=180.0)
                results["innocent"] = "success"
            except ToxicTaskError:
                results["innocent"] = "toxic(BAD)"
            except Exception as e:
                results["innocent"] = f"other:{type(e).__name__}"

        tw = threading.Thread(target=wedge_thread, daemon=True)
        ti = threading.Thread(target=innocent_thread, daemon=True)
        tw.start()
        time.sleep(0.5)   # wedge submits first -> in-flight; innocent queues behind
        ti.start()
        tw.join(timeout=200)
        ti.join(timeout=200)
        print(f"    wedge result:    {results.get('wedge')}")
        print(f"    innocent result: {results.get('innocent')}")
        ok = (results.get("wedge") == "toxic"
              and results.get("innocent") == "success")
        print("    PASS" if ok else "    FAIL")
        return ok
    finally:
        try:
            c.exit()
        except Exception:
            pass


def main() -> int:
    a = scenario_a_wedge_to_toxic()
    b = scenario_b_innocent_retries()
    print("\n" + "=" * 50)
    print(f"integration: A={'PASS' if a else 'FAIL'}  B={'PASS' if b else 'FAIL'}")
    return 0 if (a and b) else 1


if __name__ == "__main__":
    sys.exit(main())
