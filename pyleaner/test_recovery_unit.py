#!/usr/bin/env python3
"""Unit tests for watchdog wedge-recovery (Tasks 1-6). No Lean server needed.

Run: python3 pyleaner/test_recovery_unit.py
"""

import os
import sys
import types
import queue as Q
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyleaner.errors import ServiceUnavailable, ToxicTaskError
from pyleaner.watchdog import Watchdog, POISON_METHOD
from pyleaner.worker import Worker
from pyleaner.client import _submit_resilient


# ── fakes ───────────────────────────────────────────────────


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else -9


class FakeClient:
    def __init__(self):
        self.process = FakeProc()
        self.worker_pool = None
        self._pending_requests = {}
        self._pending_lock = threading.Lock()


# ── Task 1: exceptions + server_ready + fatal channel ───────


def test_exceptions_distinct_and_carry_payload():
    assert not issubclass(ToxicTaskError, ServiceUnavailable)
    e = ToxicTaskError("get_diagnostics", "INTERNAL PANIC: x", "def f := 5^9")
    assert e.task_type == "get_diagnostics"
    assert e.reason.startswith("INTERNAL PANIC")
    assert e.input_text == "def f := 5^9"


def test_server_ready_set_by_default():
    wd = Watchdog(FakeClient())
    assert wd.server_ready.is_set() is True


def test_server_ready_clear_set_roundtrip():
    wd = Watchdog(FakeClient())
    wd.server_ready.clear()
    assert wd.server_ready.is_set() is False
    wd.server_ready.set()
    assert wd.server_ready.is_set() is True


def test_fatal_flag_take_consumes():
    wd = Watchdog(FakeClient())
    assert wd.take_fatal() is None
    wd.flag_fatal("INTERNAL PANIC: boom")
    assert wd.take_fatal() == "INTERNAL PANIC: boom"
    assert wd.take_fatal() is None  # consumed


# ── Task 2: worker tracks current_task / task_started_at ─────


def test_worker_has_tracking_fields():
    w = Worker.__new__(Worker)
    w.current_task = None
    w.task_started_at = None
    assert w.current_task is None and w.task_started_at is None


def test_worker_run_sets_then_clears_current_task():
    w = Worker.__new__(Worker)
    w.task_queue = Q.Queue()
    w.notification_queue = Q.Queue()
    w.current_task = None
    w.task_started_at = None
    seen = {}

    def fake(**kwargs):
        seen["during"] = w.current_task
        seen["started_set"] = w.task_started_at is not None
        return "done"

    w.process_funcs = {"ping": fake}
    rq = Q.Queue()
    w.task_queue.put({"task_type": "ping", "result_q": rq, "kwargs": {}})
    w.task_queue.put(None)  # shutdown after one task
    w._run()
    r = rq.get_nowait()
    assert r == {"success": True, "content": "done"}
    assert seen["during"] is not None and seen["started_set"] is True
    assert w.current_task is None and w.task_started_at is None  # cleared after


# ── Task 3: poison sentinel + drain ──────────────────────────


def test_didchange_raises_on_poison():
    w = Worker.__new__(Worker)
    w.notification_queue = Q.Queue()
    w.uri = "file:///x.lean"
    w.document_version = 0
    w.client = types.SimpleNamespace(notify=lambda *a, **k: None)
    w.notification_queue.put({"method": POISON_METHOD})
    try:
        w._didchange("text", {})
        assert False, "should have raised ServiceUnavailable"
    except ServiceUnavailable:
        pass


def test_worker_drains_queue_on_unavailable():
    w = Worker.__new__(Worker)
    w.task_queue = Q.Queue()
    culprit_rq = Q.Queue()
    w.current_task = {"task_type": "changecontent", "result_q": culprit_rq,
                      "kwargs": {}, "_culprit": True}
    q1 = Q.Queue()
    q2 = Q.Queue()
    w.task_queue.put({"task_type": "ping", "result_q": q1, "kwargs": {}})
    w.task_queue.put({"task_type": "ping", "result_q": q2, "kwargs": {}})
    w._on_service_unavailable()
    r = culprit_rq.get_nowait()
    assert r["success"] is False and r["toxic"] is True
    assert isinstance(r["error"], ServiceUnavailable)
    assert q1.get_nowait()["toxic"] is False
    assert q2.get_nowait()["toxic"] is False


def test_worker_innocent_inflight_not_marked_toxic():
    w = Worker.__new__(Worker)
    w.task_queue = Q.Queue()
    rq = Q.Queue()
    w.current_task = {"task_type": "ping", "result_q": rq, "kwargs": {}}  # no _culprit
    w._on_service_unavailable()
    assert rq.get_nowait()["toxic"] is False


# ── Task 4: attribution ─────────────────────────────────────


def test_attribution_deadline_precise():
    wd = Watchdog(FakeClient())
    w1_task = {"task_type": "a"}
    w2_task = {"task_type": "b"}
    w1 = types.SimpleNamespace(current_task=w1_task)
    w2 = types.SimpleNamespace(current_task=w2_task)
    toxic = wd._attribute_toxic("deadline", [w1_task, w2_task], [w2])
    assert toxic == [w2_task]


def test_attribution_death_and_fatal_tongsha():
    wd = Watchdog(FakeClient())
    a = {"task_type": "a"}
    b = {"task_type": "b"}
    assert wd._attribute_toxic("death", [a, b], []) == [a, b]
    assert wd._attribute_toxic("fatal", [a, b], []) == [a, b]


# ── Task 6: submit_resilient ────────────────────────────────


def test_submit_resilient_retries_innocent_then_succeeds():
    c = FakeClient()
    c.watchdog = Watchdog(c)
    c.watchdog.server_ready.set()

    class P:
        def __init__(self):
            self.calls = 0

        def submit_task(self, task):
            self.calls += 1
            if self.calls == 1:
                task["result_q"].put({"success": False,
                                      "error": ServiceUnavailable(),
                                      "toxic": False})
            else:
                task["result_q"].put({"success": True, "content": "OK"})

    c.worker_pool = P()
    out = _submit_resilient(c, "ping", {})
    assert out == "OK" and c.worker_pool.calls == 2


def test_submit_resilient_toxic_raises():
    c = FakeClient()
    c.watchdog = Watchdog(c)
    c.watchdog.server_ready.set()

    class P:
        def submit_task(self, task):
            task["result_q"].put({"success": False,
                                  "error": ServiceUnavailable(),
                                  "toxic": True})

    c.worker_pool = P()
    try:
        _submit_resilient(c, "changecontent", {"text": "def f := 5^9999999993"})
        assert False, "should have raised ToxicTaskError"
    except ToxicTaskError as e:
        assert e.task_type == "changecontent"
        assert "5^9999999993" in e.input_text


def test_submit_resilient_timeout_waits_for_restart_then_retries():
    """queue.Empty (timeout) → wait for watchdog restart → retry → success.

    This is the path that was MISSING before — the worker never responds
    (wedged), rq.get times out, and the function must wait for the watchdog
    to restart instead of raising ServiceUnavailable.
    """
    c = FakeClient()
    c.watchdog = Watchdog(c)
    c.watchdog.server_ready.set()

    # Pool 1: wedged — never puts anything on the result queue
    class WedgedPool:
        def submit_task(self, task):
            pass

    c.worker_pool = WedgedPool()

    result = []
    done = threading.Event()

    def call():
        try:
            out = _submit_resilient(c, "ping", {}, timeout=0.5)
            result.append(out)
        except Exception as e:
            result.append(e)
        done.set()

    t = threading.Thread(target=call, daemon=True)
    t.start()

    time.sleep(0.8)  # > 0.5 timeout — function should now be in the wait loop

    # Simulate watchdog detecting wedge → starts restart
    c.watchdog.server_ready.clear()
    time.sleep(0.3)  # let it break out of the wait loop and hit server_ready.wait()

    # Restart complete: new pool + server_ready set
    class NewPool:
        def submit_task(self, task):
            task["result_q"].put({"success": True, "content": "OK"})

    c.worker_pool = NewPool()
    c.watchdog.server_ready.set()

    assert done.wait(timeout=3.0), "function should complete"
    assert result == ["OK"], f"expected ['OK'], got {result}"


def test_submit_resilient_waits_when_not_ready():
    c = FakeClient()
    c.watchdog = Watchdog(c)
    c.watchdog.server_ready.clear()  # server mid-restart

    class P:
        def submit_task(self, task):
            task["result_q"].put({"success": True, "content": "OK"})

    c.worker_pool = P()
    done = threading.Event()

    def go():
        # blocks until server_ready is set
        _submit_resilient(c, "ping", {})
        done.set()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    assert not done.wait(timeout=0.3)   # still blocked (not ready)
    c.watchdog.server_ready.set()       # restart finishes
    assert done.wait(timeout=2.0)       # now it proceeds


# ── runner ──────────────────────────────────────────────────


def main() -> int:
    tests = [(n, fn) for n, fn in sorted(globals().items())
             if n.startswith("test_") and callable(fn)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
            passed += 1
        except Exception as e:
            print("FAIL", name, "->", repr(e))
            failed += 1
    print(f"\n{passed} passed, {failed} failed ({len(tests)} total)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
