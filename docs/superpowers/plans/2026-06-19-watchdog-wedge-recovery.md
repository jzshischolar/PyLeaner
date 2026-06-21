# Watchdog Wedge Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The components are tightly coupled (shared `server_ready` event, worker/pool/watchdog state) — build in the listed order and run the test suite after each phase. Commits are **checkpoints only**: do not `git commit` unless the user asks.

**Goal:** Give PyLeaner a single, general server-revival mechanism (the Watchdog) that auto-recovers from both hard crashes and wedges, with transparent retry for innocent tasks and a `ToxicTaskError` for the task that caused the failure — so an ATP loop never infinite-retries a wedging input.

**Architecture:** The Watchdog daemon already polls process liveness every 20s and revives on death. We extend it with two wedge signals — server-declared fatals on stderr (`INTERNAL PANIC` / `Stack overflow` / OOM) and a per-worker task-age deadline. On any trigger it clears a `server_ready` Event, marks the culprit task(s) (`_culprit`), poisons all workers so their blocked `_didchange` aborts, hard-restarts the server+pool, then sets `server_ready`. A new `LspClient.submit_resilient()` waits on `server_ready`, submits to the *current* pool, and on `ServiceUnavailable` either raises `ToxicTaskError` (culprit) or transparently retries (innocent). Attribution: deadline → precise (the over-deadline worker); fatal/death → 通杀 (all in-flight); queued tasks are always innocent.

**Tech Stack:** Python 3.12 stdlib only (threading, queue, subprocess, re). Lean 4.25.0-rc2 via `lake serve`. Tests are runnable scripts (`python3 pyleaner/test_*.py`) — no pytest dependency. Integration tests need a working Lean toolchain.

---

## File Structure

| File | Responsibility | Status |
|------|----------------|--------|
| `pyleaner/errors.py` | `ServiceUnavailable` (internal, retryable) + `ToxicTaskError` (public, non-retryable) exceptions | **Create** |
| `pyleaner/watchdog.py` | `Watchdog`: death poll + fatal-stderr flag + deadline detection; attribution; `server_ready` Event; internal `_restart()` (clear→poison→restart→set); `mark_toxic()` | **Modify** (exists) |
| `pyleaner/worker.py` | Worker tracks `current_task` + `task_started_at`; `_didchange` raises `ServiceUnavailable` on poison sentinel; `_run` fails current_task (toxic flag) + drains task_queue (innocent) + exits on `ServiceUnavailable` | **Modify** |
| `pyleaner/client.py` | `_read_stderr` flags fatals to watchdog; new `submit_resilient()` transparent-retry submit; wire `server_ready` into connect/create_pool/exit (already wired for watchdog start/stop) | **Modify** |
| `pyleaner/__init__.py` | Export `ToxicTaskError`, `ServiceUnavailable` | **Modify** |
| `pyleaner/test_recovery_unit.py` | Unit tests (no server): fatal matching, attribution, `server_ready`, `submit_resilient` retry loop, worker drain | **Create** |
| `pyleaner/test_recovery_integration.py` | Integration tests (real server): crash revival, wedge→ToxicTaskError (panic), innocent→retry, deadline→ToxicTaskError | **Create** |
| `checker_lean_native.py` (app) | `_submit_task` delegates to `submit_resilient`; `except ToxicTaskError` → error prompt to model | **Modify** |

**Constants (in watchdog.py):**
- `FATAL_RE = re.compile(r"INTERNAL PANIC|Stack overflow detected|out of memory|memory allocation of")`
- `WEDGE_DEADLINE = 120.0` (seconds; > longest legit elaboration; tunable)
- `POISON_METHOD = "$/internal/service-unavailable"` (sentinel method name injected into worker `notification_queue`)

---

## Task 1: Exception types + `server_ready` plumbing (foundation)

**Files:** Create `pyleaner/errors.py`; Modify `pyleaner/watchdog.py`, `pyleaner/__init__.py`.

- [ ] **Step 1: Write the failing unit test** (`pyleaner/test_recovery_unit.py`)

```python
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pyleaner.errors import ServiceUnavailable, ToxicTaskError
from pyleaner.watchdog import Watchdog

class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
    def poll(self):
        return None if self._alive else -9

class FakeClient:
    def __init__(self):
        self.process = FakeProc(alive=True)
        self.worker_pool = None

def test_server_ready_set_on_init():
    wd = Watchdog(FakeClient())
    assert wd.server_ready.is_set() is False  # not set until started/armed

def test_server_ready_set_after_start():
    wd = Watchdog(FakeClient())
    wd.server_ready.set()   # connect() will do this
    assert wd.server_ready.is_set() is True

def test_exceptions_distinct():
    assert not issubclass(ToxicTaskError, ServiceUnavailable)
    e = ToxicTaskError("get_diagnostics", "INTERNAL PANIC: ...", "def f := 5^9999999993")
    assert e.task_type == "get_diagnostics"
    assert e.reason.startswith("INTERNAL PANIC")
    assert "5^9999999993" in e.input_text

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                print("FAIL", name, e); raise
```

- [ ] **Step 2: Run — verify it fails** (`ImportError: no module 'pyleaner.errors'`).
- [ ] **Step 3: Create `pyleaner/errors.py`**

```python
"""Recovery-related exceptions for PyLeaner."""

class ServiceUnavailable(Exception):
    """Internal: the Lean server is restarting. Retryable (unless the task is
    marked culprit). Raised into the worker when a poison sentinel aborts a
    blocked _didchange; submit_resilient treats it as 'wait for server_ready and
    retry', unless the carrying result is flagged toxic."""

class ToxicTaskError(Exception):
    """Public: this task caused the server to crash or wedge, so it was rejected
    (not retried). The application should drop/regenerate the input — retrying it
    would re-crash the server. Carries task_type, reason, and the full input."""

    def __init__(self, task_type: str, reason: str, input_text: str):
        self.task_type = task_type
        self.reason = reason
        self.input_text = input_text
        super().__init__(f"{task_type} rejected (server {reason})")
```

- [ ] **Step 4: Add `server_ready` to `Watchdog.__init__`** in `watchdog.py`:

```python
import threading
# inside __init__, after self._last_restart = 0.0:
self.server_ready = threading.Event()
self.server_ready.set()   # assume ready until a restart clears it
self._fatal_reason = None         # set by client._read_stderr on a fatal
self._fatal_lock = threading.Lock()
```

Add a fatal-flag method + accessor:

```python
def flag_fatal(self, reason: str) -> None:
    """Called by LspClient._read_stderr when the server prints a fatal line."""
    with self._fatal_lock:
        if self._fatal_reason is None:
            self._fatal_reason = reason

def take_fatal(self):
    with self._fatal_lock:
        r = self._fatal_reason
        self._fatal_reason = None
        return r
```

- [ ] **Step 5: Export from `__init__.py`**: add `from .errors import ServiceUnavailable, ToxicTaskError` and both names to `__all__`.
- [ ] **Step 6: Run unit test → PASS.** `python3 pyleaner/test_recovery_unit.py`

---

## Task 2: Worker tracks `current_task` + `task_started_at`

**Files:** Modify `pyleaner/worker.py`.

- [ ] **Step 1: Write failing unit test** (append to `test_recovery_unit.py`)

```python
from pyleaner.worker import Worker

class FakeKeepAlive: 
    def register(self,*a,**k): pass
class FakeClientForWorker:
    def __init__(self):
        self.process = FakeProc(); self.worker_pool = None
        self._pending_requests = {}; self._pending_lock = threading.Lock()
    def request(self,*a,**k): ...   # not used here
    def notify(self,*a,**k): pass
    def _next_id(self): return 1
    def _send_message(self,*a,**k): pass

def test_worker_tracks_current_task():
    # construct a Worker without starting the real server: just check the fields
    # are set/cleared around task processing using a stubbed process_func.
    w = Worker.__new__(Worker)   # bypass __init__ (no real server)
    w.worker_id = 1
    w.task_queue = __import__("queue").Queue()
    w.notification_queue = __import__("queue").Queue()
    w.current_task = None
    w.task_started_at = None
    assert w.current_task is None and w.task_started_at is None
```

- [ ] **Step 2: Run → fail** (`AttributeError: current_task`).
- [ ] **Step 3: Modify `Worker.__init__`** to add `self.current_task = None` and `self.task_started_at = None`; modify `Worker._run` to set them around task execution:

```python
def _run(self):
    import time
    while True:
        task = self.task_queue.get()
        if task is None:
            break
        self.current_task = task
        self.task_started_at = time.monotonic()
        task_type = task.get("task_type")
        result_q = task.get("result_q")
        kwargs = task.get("kwargs", {})
        try:
            result = self.process_funcs[task_type](**kwargs)
            result_q.put({"success": True, "content": result})
        except ServiceUnavailable:
            self._on_service_unavailable()   # implemented in Task 3
            break   # worker exits (hard restart replaces the pool)
        except Exception as e:
            if result_q is not None:
                result_q.put({"success": False, "error": e})
        finally:
            self.current_task = None
            self.task_started_at = None
```

(Import `ServiceUnavailable` from `.errors` at top of worker.py.)

- [ ] **Step 4: Run → PASS.**

---

## Task 3: Poison sentinel → `ServiceUnavailable` + drain queue

**Files:** Modify `pyleaner/worker.py` (`_didchange` + new `_on_service_unavailable`).

- [ ] **Step 1: Write failing unit test**

```python
def test_worker_drains_queue_on_unavailable():
    import queue as Q
    w = Worker.__new__(Worker)
    w.task_queue = Q.Queue(); w.notification_queue = Q.Queue()
    w.current_task = None; w.task_started_at = None
    # in-flight task (the culprit)
    culprit_rq = Q.Queue()
    w.current_task = {"task_type":"changecontent","result_q":culprit_rq,"kwargs":{},"_culprit":True}
    # two queued innocent tasks
    q1 = Q.Queue(); q2 = Q.Queue()
    w.task_queue.put({"task_type":"ping","result_q":q1,"kwargs":{}})
    w.task_queue.put({"task_type":"ping","result_q":q2,"kwargs":{}})
    w._on_service_unavailable()
    # culprit result carries toxic=True
    r = culprit_rq.get_nowait()
    assert r["success"] is False and r.get("toxic") is True
    # queued results carry toxic=False
    assert q1.get_nowait().get("toxic") is False
    assert q2.get_nowait().get("toxic") is False
```

- [ ] **Step 2: Run → fail** (`AttributeError: _on_service_unavailable`).
- [ ] **Step 3: Implement** in `worker.py`:

```python
POISON_METHOD = "$/internal/service-unavailable"

def _on_service_unavailable(self):
    """Called when a poison sentinel aborts us. Fail the in-flight task (toxic
    flag from its _culprit mark) and drain the task_queue, failing every queued
    (innocent) task as non-toxic, so their callers transparently retry."""
    import queue as Q
    cur = self.current_task
    if cur is not None and cur.get("result_q") is not None:
        cur["result_q"].put({"success": False, "error": ServiceUnavailable(),
                             "toxic": bool(cur.get("_culprit", False))})
    while True:
        try:
            t = self.task_queue.get_nowait()
        except Q.Empty:
            break
        if t is None:
            continue
        rq = t.get("result_q")
        if rq is not None:
            rq.put({"success": False, "error": ServiceUnavailable(), "toxic": False})
```

Modify `_didchange`'s read loop: when `msg.get("method") == POISON_METHOD`, raise `ServiceUnavailable`. Insert at the top of the `while True` loop in `_didchange`, right after `msg = self.notification_queue.get(...)`:

```python
        msg = self.notification_queue.get(timeout=timeout)
        method = msg.get("method")
        if method == POISON_METHOD:
            raise ServiceUnavailable()
```

(Do the same in `_didopen`'s loop.)

- [ ] **Step 4: Run → PASS.**

---

## Task 4: Attribution + poisoned hard-restart (death path)

**Files:** Modify `pyleaner/watchdog.py` (`_restart` internals + attribution helper).

- [ ] **Step 1: Write failing unit test** (attribution, no server)

```python
def test_attribution_death_kills_all_inflight():
    wd = Watchdog(FakeClient())
    inflight = [{"task_type":"a"},{"task_type":"b"}]
    toxic = wd._attribute_toxic(trigger="death", inflight=inflight, over_deadline_workers=[])
    assert toxic == inflight  # 通杀

def test_attribution_deadline_precise():
    wd = Watchdog(FakeClient())
    w1_task = {"task_type":"a"}; w2_task = {"task_type":"b"}
    # only worker index 1 is over deadline
    workers = [type("W",(),{"current_task":w1_task})(),
               type("W",(),{"current_task":w2_task})()]
    toxic = wd._attribute_toxic(trigger="deadline", inflight=[w1_task, w2_task],
                                over_deadline_workers=[workers[1]])
    assert toxic == [w2_task]   # precise: only the over-deadline worker's task

def test_attribution_fatal_kills_all_inflight():
    wd = Watchdog(FakeClient())
    inflight = [{"task_type":"a"},{"task_type":"b"}]
    toxic = wd._attribute_toxic(trigger="fatal", inflight=inflight, over_deadline_workers=[])
    assert toxic == inflight
```

- [ ] **Step 2: Run → fail** (`AttributeError: _attribute_toxic`).
- [ ] **Step 3: Implement** in `watchdog.py`:

```python
def _inflight_tasks(self):
    pool = getattr(self.client, "worker_pool", None)
    out = []
    if pool is not None:
        for w in getattr(pool, "workers", []):
            t = getattr(w, "current_task", None)
            if t is not None:
                out.append((w, t))
    return out

def _attribute_toxic(self, trigger, inflight, over_deadline_workers):
    """Return the subset of `inflight` tasks to mark toxic.
    deadline -> precise (the over-deadline workers' tasks).
    fatal/death -> 通杀 (all in-flight)."""
    if trigger == "deadline":
        s = set(id(w) for w in over_deadline_workers)
        # inflight is a list of plain task dicts here (test); in _restart we pass
        # (worker, task) pairs — see _mark_and_poison for the real call site.
        return [t for (w, t) in inflight if w in over_deadline_workers] \
            if inflight and isinstance(inflight[0], tuple) else inflight
    return inflight  # fatal / death -> 通杀
```

(Refine so the real call site passes `(worker, task)` tuples and the test passes plain dicts — keep the test's expectation: for `deadline` with `over_deadline_workers=[workers[1]]` and `inflight=[w1_task,w2_task]` (plain dicts), return `[w2_task]`. Adjust signature so the test is unambiguous: `_attribute_toxic(trigger, inflight_tasks, over_deadline_workers)` where `inflight_tasks` is a list of task dicts and `over_deadline_workers` is a list of worker objects whose `current_task` is in `inflight_tasks`. Implementation matches by identity.)

Final clean signature used by both test and `_restart`:

```python
def _attribute_toxic(self, trigger, inflight_tasks, over_deadline_workers):
    if trigger == "deadline":
        bad = set(id(t) for t in
                  (getattr(w, "current_task", None) for w in over_deadline_workers))
        return [t for t in inflight_tasks if id(t) in bad]
    return list(inflight_tasks)   # fatal / death -> 通杀
```

Then make `_restart(trigger)` clear `server_ready`, mark toxic, poison all workers, restart, set `server_ready`:

```python
def _restart(self, trigger="death", reason=""):
    self.server_ready.clear()
    pairs = self._inflight_tasks()                 # [(worker, task)]
    inflight_tasks = [t for (_, t) in pairs]
    over = [w for (w, _) in pairs] if trigger != "deadline" else \
           [w for (w, _) in pairs if self._worker_over_deadline(w)]
    toxic = self._attribute_toxic(trigger, inflight_tasks, over)
    for t in toxic:
        t["_culprit"] = True
    # poison every worker so blocked _didchange/_didopen abort now
    for (w, _) in pairs:
        try: w.notification_queue.put_nowait({"method": POISON_METHOD})
        except Exception: pass
    print(f"watchdog: {trigger} ({reason}) — reviving; {len(toxic)} task(s) marked toxic", flush=True)
    _kill_process_tree(self.client.process)
    self.client.connect(timeout=300)               # reconnects + restarts watchdog thread idempotently
    if self._armed:
        self.client.create_pool(text=self._base_text, size=self._size)
    self.server_ready.set()

def _worker_over_deadline(self, w):
    import time
    ts = getattr(w, "task_started_at", None)
    return ts is not None and (time.monotonic() - ts) > WEDGE_DEADLINE
```

Rename the existing public `restart()` → call `_restart("death", "process exited")` internally; keep `restart()` as a thin wrapper is NOT wanted (decision 4: watchdog-only). So **delete the public `restart()`**; the `_run` loop calls `_restart(...)` directly. (App no longer calls restart.)

- [ ] **Step 4: Run attribution tests → PASS.**

---

## Task 5: Watchdog loop detects death + fatal + deadline; calls `_restart`

**Files:** Modify `pyleaner/watchdog.py` (`_run`).

- [ ] **Step 1: Write failing unit test** (loop logic via a stepped fake)

```python
def test_run_triggers_death_restart():
    c = FakeClient()
    wd = Watchdog(c, interval=0.05)
    calls = []
    wd._restart = lambda trigger="death", reason="": calls.append((trigger, reason))
    wd.start()
    c.process._alive = False          # server dies
    time.sleep(0.3)
    wd.stop()
    assert any(t == "death" for (t, _) in calls)

def test_run_triggers_fatal_restart():
    c = FakeClient(); wd = Watchdog(c, interval=0.05)
    calls = []; wd._restart = lambda trigger="death", reason="": calls.append((trigger, reason))
    wd.start(); wd.flag_fatal("INTERNAL PANIC: x"); time.sleep(0.3); wd.stop()
    assert any(t == "fatal" for (t, _) in calls)
```

- [ ] **Step 2: Run → fail** (only death was detected before).
- [ ] **Step 3: Rewrite `_run`**

```python
def _run(self):
    while not self._stop.wait(self.interval):
        # 1) fatal stderr (instant, 通杀)
        fatal = self.take_fatal()
        if fatal is not None:
            self._restart(trigger="fatal", reason=fatal); continue
        # 2) wedge by deadline (per-worker, precise)
        pool = getattr(self.client, "worker_pool", None)
        if pool is not None:
            over = [w for w in getattr(pool, "workers", []) if self._worker_over_deadline(w)]
            if over:
                self._restart(trigger="deadline", reason=f"task exceeded {WEDGE_DEADLINE}s"); continue
        # 3) hard death (poll)
        if not self._is_alive():
            self._restart(trigger="death", reason="process exited"); continue
```

- [ ] **Step 4: Run → PASS** (both tests).
- [ ] **Step 5: Wire `client._read_stderr`** to call `self.watchdog.flag_fatal(line)` when `FATAL_RE` matches (import `FATAL_RE` from `.watchdog`). Keep the existing `print(f"SERVER STDERR: {line}")`.

---

## Task 6: `LspClient.submit_resilient()` — transparent retry / toxic raise

**Files:** Modify `pyleaner/client.py`.

- [ ] **Step 1: Write failing unit test** (mocked pool + event)

```python
def test_submit_resilient_retries_innocent_then_succeeds():
    import queue as Q
    c = FakeClient()
    wd = Watchdog(c); wd.server_ready.set()
    c.watchdog = wd
    class P:
        def __init__(self): self.calls = 0
        def submit_task(self, task):
            self.calls += 1
            if self.calls == 1:
                task["result_q"].put({"success": False, "error": ServiceUnavailable(), "toxic": False})
            else:
                task["result_q"].put({"success": True, "content": "OK"})
    c.worker_pool = P()
    out = c.submit_resilient("ping", {})   # LspClient method
    assert out == "OK" and c.worker_pool.calls == 2

def test_submit_resilient_toxic_raises():
    c = FakeClient(); wd = Watchdog(c); wd.server_ready.set(); c.watchdog = wd
    class P:
        def submit_task(self, task):
            task["result_q"].put({"success": False, "error": ServiceUnavailable(), "toxic": True})
    c.worker_pool = P()
    try:
        c.submit_resilient("changecontent", {"text":"def f := 5^9999999993"})
        assert False, "should have raised"
    except ToxicTaskError as e:
        assert e.task_type == "changecontent"
        assert "5^9999999993" in e.input_text
```

(Note: `submit_resilient` is added to `LspClient`; the test uses a `FakeClient` — so either add the method to `LspClient` and monkeypatch `FakeClient` to use the real method, or write `submit_resilient` as a module function `submit_resilient(client, task_type, kwargs, timeout)` and have `LspClient.submit_resilient` delegate. **Decision: implement as a module function in client.py and a thin `LspClient` method wrapper**, so the test can call it on a fake.)

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** in `client.py`:

```python
def _submit_resilient(client, task_type, kwargs, timeout=120.0):
    """Transparent recovery: wait for server_ready, submit to the CURRENT pool
    (re-fetched each attempt so a restarted pool is used), and on
    ServiceUnavailable either raise ToxicTaskError (culprit) or retry."""
    import queue as Q
    from .errors import ServiceUnavailable, ToxicTaskError
    input_text = kwargs.get("text", "") if isinstance(kwargs, dict) else ""
    while True:
        client.watchdog.server_ready.wait()
        pool = client.worker_pool
        if pool is None:
            raise RuntimeError("Worker pool not initialized")
        rq = Q.Queue()
        pool.submit_task({"task_type": task_type, "result_q": rq, "kwargs": kwargs})
        try:
            resp = rq.get(timeout=timeout)
        except Q.Empty:
            # genuine slow task OR orphaned on an abandoned worker — let the
            # watchdog's deadline/restart path resolve it; surface as retryable.
            raise ServiceUnavailable()
        if resp.get("success", False):
            return resp.get("content")
        err = resp.get("error", "unknown error")
        if isinstance(err, ServiceUnavailable):
            if resp.get("toxic"):
                raise ToxicTaskError(task_type, "crashed/wedged the server", input_text)
            continue   # innocent — transparent retry on the (now-current) pool
        raise RuntimeError(str(err) or repr(err))
```

Add to `LspClient`:

```python
def submit_resilient(self, task_type, kwargs, timeout=120.0):
    from . import _submit_resilient   # or direct call
    return _submit_resilient(self, task_type, kwargs, timeout)
```

- [ ] **Step 4: Run → PASS** (both).

---

## Task 7: Integration tests — crash revival + wedge→toxic + innocent retry

**Files:** Create `pyleaner/test_recovery_integration.py`. (Requires Lean toolchain; slow.)

- [ ] **Step 1: Write the integration script** with these scenarios:

  1. **crash revival** (port of existing `test_watchdog_e2e`): kill the tree → watchdog revives → `submit_resilient` returns after the restart window.
  2. **wedge → ToxicTaskError (panic)**: submit `#eval Nat.pow 10 9999999993` via `submit_resilient("changecontent", {text, content_range})`; expect `ToxicTaskError` once the fatal-stderr path fires (seconds, not 120s).
  3. **innocent → transparent retry**: from a second thread submit a trivial `changecontent`; simultaneously wedge the server from the main thread (scenario 2's content); assert the innocent call eventually returns success (not ToxicTaskError) after the revival.
  4. **deadline → ToxicTaskError**: submit a content that elaborates forever without a fatal (e.g. an infinite recursion under default `maxRecDepth` is caught gracefully — instead use a divergent `decide`-free compute); if no clean divergent input is available, **skip this scenario with a printed note** (do not fake it). Set `WEDGE_DEADLINE` low via a module constant override for the test only.

  Each scenario prints `PASS`/`FAIL` with the observed timing.

- [ ] **Step 2: Run scenarios 1–3** (`python3 pyleaner/test_recovery_integration.py`). Scenario 4 is best-effort.
- [ ] **Step 3: Fix failures** by returning to the relevant Task; re-run until 1–3 PASS.

(Concrete code for the integration script is written during execution against the real `submit_resilient`/`Watchdog` APIs finalized in Tasks 1–6 — the unit tests pin those APIs, so the integration script is straightforward.)

---

## Task 8: App integration — `checker_lean_native.py` uses `submit_resilient`

**Files:** Modify `checker_lean_native.py`.

- [ ] **Step 1: Rewrite `_submit_task`** to delegate:

```python
from pyleaner import ToxicTaskError   # add to imports

def _submit_task(task_type, lspclient, kwargs, timeout=120.0):
    _err = _abuse_error(kwargs.get("text"))
    if _err:
        raise RuntimeError(_err)
    return lspclient.submit_resilient(task_type, kwargs, timeout=timeout)
```

- [ ] **Step 2: At each layer-1 entry that returns an error prompt to the model** (`parse_into_declarations_and_check`, `check_direct_proof`, `check_lean_syntax`, `lean_tool_call`), add `except ToxicTaskError as e:` returning a clear model-facing prompt, e.g.:

```python
except ToxicTaskError as e:
    return {'status': 'error', 'error_prompt':
        "Error: this Lean code crashed/wedged the verifier and was rejected "
        f"({e.reason}). It likely contains a huge power, an unbounded tactic, "
        "or a guard-disabling option. Rewrite it. Rejected input:\n" + e.input_text}
```

(Follow the existing return shape each function already uses — `dict` for the parse/check functions, the `lean_tool_call` dict shape, etc.)

- [ ] **Step 3: `python3 -m py_compile checker_lean_native.py` → OK.**
- [ ] **Step 4: Smoke test**: import `checker_lean_native` and confirm `_submit_task` calls `submit_resilient` (unit-level: monkeypatch `lspclient.submit_resilient` and assert it's called).

---

## Task 9: Full suite + regression

- [ ] **Step 1:** `python3 pyleaner/test_recovery_unit.py` → all PASS.
- [ ] **Step 2:** `python3 pyleaner/test_watchdog_e2e.py` → still PASS (crash revival, unchanged contract).
- [ ] **Step 3:** `python3 pyleaner/test_recovery_integration.py` → scenarios 1–3 PASS.
- [ ] **Step 4:** Re-run `python3 pyleaner/check_pow2005.py` and `reproduce_13987.py` to confirm no regression in the earlier findings.
- [ ] **Step 5:** Reinstall into the venv (`/home/lcw/myenv/bin/pip install -e /home/lcw/PyLeaner`) and re-run the integration suite against the installed copy.

---

## Self-Review

**Spec coverage:**
- Wedge detection (fatal stderr + deadline) → Tasks 4–5 ✓
- Transparent recovery (server_ready + retry) → Tasks 1, 6 ✓
- Hard restart, watchdog-only → Task 4 (delete public `restart`) ✓
- ToxicTaskError for culprit, full input payload → Tasks 1, 3, 6 ✓
- Attribution: deadline precise / fatal·death 通杀 / queued innocent → Task 4 ✓
- Drain queue for innocent → Task 3 ✓
- App notified → Task 8 ✓
- Multi-worker: attribution handles lists of workers (Task 4 `_inflight_tasks` iterates `pool.workers`) ✓; whole-process restart collateral accepted ✓

**Gaps to watch during execution:**
- The `_didopen` path also blocks on `notification_queue` and must honor the poison sentinel (Task 3 step 3 notes this).
- `connect()` inside `_restart` calls `watchdog.start()` — must remain idempotent (it is: `start()` checks `_thread.is_alive()`). Verify in Task 4 integration.
- `WEDGE_DEADLINE` (120s) vs per-call `timeout` (120s) in `submit_resilient`: if the deadline fires first, the worker is poisoned and the caller gets `ServiceUnavailable` (fast) rather than waiting the full 120s `rq.get`. Confirm in Task 7 scenario 2 (panic path is instant via fatal, so this mainly matters for the deadline scenario).

**Type/signature consistency:** `POISON_METHOD`, `WEDGE_DEADLINE`, `FATAL_RE` defined once in `watchdog.py`; `_attribute_toxic(trigger, inflight_tasks, over_deadline_workers)` used identically in test and `_restart`; `submit_resilient(self, task_type, kwargs, timeout=120.0)` matches app call sites.

---

## Execution Handoff

Inline execution is recommended here (the components are tightly coupled around shared concurrency state; a single context that holds the `server_ready`/poison/attribution invariants is safer than parallel subagents). Proceed task-by-task with the test suite as the gate after each phase.
