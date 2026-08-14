"""Unit tests for coordinated RPC timeout and watchdog recovery behavior."""

import queue
import threading
import os
import time

import pytest

import pyleaner.watchdog as watchdog_module
from pyleaner.client import _submit_resilient
from pyleaner.errors import ServiceUnavailable, ToxicTaskError
from pyleaner.rpc_session import RpcSession, RpcTimeoutError
from pyleaner.watchdog import (
    RESILIENT_RESPONSE_TIMEOUT,
    RPC_RESPONSE_TIMEOUT,
    WEDGE_DEADLINE,
    WATCHDOG_POLL_INTERVAL,
    Watchdog,
    _watchdog_monitor_main,
)


class FakeClient:
    def __init__(self):
        self._pending_lock = threading.Lock()
        self._pending_requests = {}
        self.worker_pool = None


class FakeProcess:
    pid = os.getpid()

    @staticmethod
    def poll():
        return None


def test_timeout_constants_leave_room_for_watchdog_recovery():
    assert RPC_RESPONSE_TIMEOUT > WEDGE_DEADLINE + WATCHDOG_POLL_INTERVAL
    assert RESILIENT_RESPONSE_TIMEOUT > RPC_RESPONSE_TIMEOUT


def test_rpc_timeout_has_a_distinct_exception_type():
    client = FakeClient()
    client._next_id = lambda: 1
    client._send_message = lambda _message: None
    session = RpcSession(worker_id=1, uri="file:///fake.lean", client=client)
    session.session_id = 1

    with pytest.raises(RpcTimeoutError) as exc_info:
        session._rpc_call_once(
            "LeanLspExtension.extractDeclarations",
            {},
            {"line": 0, "character": 0},
            timeout=0.01,
        )

    assert exc_info.value.timeout == 0.01
    assert client._pending_requests == {}


def test_rpc_session_connect_uses_global_request_id_allocator():
    client = FakeClient()
    allocated = iter((41, 42))
    client._next_id = lambda: next(allocated)
    request_ids = []

    def request(_method, _params, msg_id, timeout):
        request_ids.append((msg_id, timeout))
        return {"sessionId": str(1000 + msg_id)}

    client.request = request
    first = RpcSession(worker_id=1, uri="file:///worker_1.lean", client=client)
    second = RpcSession(worker_id=1, uri="file:///worker_1.lean", client=client)

    assert first.connect(timeout=3.0) == 1041
    assert second.connect(timeout=4.0) == 1042
    assert request_ids == [(41, 3.0), (42, 4.0)]


def test_watchdog_teardown_wakes_pending_rpc_callers():
    client = FakeClient()
    response_q = queue.Queue()
    client._pending_requests[7] = response_q

    Watchdog(client)._teardown_pool_state()

    assert client._pending_requests == {}
    assert isinstance(response_q.get_nowait(), ServiceUnavailable)


def test_submit_resilient_preserves_rpc_timeout_type():
    timeout_error = RpcTimeoutError("LeanLspExtension.extractDeclarations", 160)

    class ImmediatePool:
        def submit_task(self, task):
            task["result_q"].put({"success": False, "error": timeout_error})

    client = FakeClient()
    client.worker_pool = ImmediatePool()
    client.watchdog = type("FakeWatchdog", (), {"server_ready": threading.Event()})()
    client.watchdog.server_ready.set()

    with pytest.raises(RpcTimeoutError) as exc_info:
        _submit_resilient(client, "extract_declarations", {"text": "def x := 1"})

    assert exc_info.value is timeout_error


def test_submit_resilient_merges_request_observation_context():
    captured = {}

    class ImmediatePool:
        def submit_task(self, task):
            captured.update(task)
            task["result_q"].put({"success": True, "content": "ok"})

    client = FakeClient()
    client.worker_pool = ImmediatePool()
    client.watchdog = type("FakeWatchdog", (), {"server_ready": threading.Event()})()
    client.watchdog.server_ready.set()
    client.current_observation_context = lambda: {
        "action_id": "action-1", "node_id": "node-1"}

    result = _submit_resilient(
        client,
        "get_diagnostics",
        {"text": "example : True := by trivial"},
        context={"generation_id": "generation-1"},
    )

    assert result == "ok"
    assert captured["context"] == {
        "action_id": "action-1",
        "node_id": "node-1",
        "generation_id": "generation-1",
    }


def test_submit_resilient_preserves_precise_toxic_reason():
    class ImmediatePool:
        def submit_task(self, task):
            task["result_q"].put({
                "success": False,
                "error": ServiceUnavailable(),
                "toxic": True,
                "toxic_reason": "worker 2 exceeded its memory limit",
            })

    client = FakeClient()
    client.worker_pool = ImmediatePool()
    client.watchdog = type(
        "FakeWatchdog", (), {"server_ready": threading.Event()})()
    client.watchdog.server_ready.set()

    with pytest.raises(ToxicTaskError) as exc_info:
        _submit_resilient(
            client, "changecontent", {"text": "example : True := by simp"})

    assert exc_info.value.reason == "worker 2 exceeded its memory limit"


def test_watchdog_observer_runs_in_a_separate_process():
    client = FakeClient()
    client.process = FakeProcess()
    watchdog = Watchdog(client, interval=0.05)
    try:
        watchdog.start()
        deadline = time.monotonic() + 5
        while (watchdog._process is not None
               and not watchdog._process.is_alive()
               and time.monotonic() < deadline):
            time.sleep(0.01)
        assert watchdog._process is not None
        assert watchdog._process.is_alive()
        assert watchdog._process.pid != os.getpid()
    finally:
        watchdog.stop()


def _start_fake_monitor(
    command_queue,
    event_queue,
    *,
    replacement_grace=0.02,
    soft_memory_limit=1024,
    hard_memory_limit=2048,
):
    monitor = threading.Thread(
        target=_watchdog_monitor_main,
        args=(
            command_queue,
            event_queue,
            os.getppid(),
            60.0,
            60.0,
            0.005,
            soft_memory_limit,
            hard_memory_limit,
            replacement_grace,
        ),
        daemon=True,
    )
    monitor.start()
    return monitor


def test_logical_worker_pid_replacement_rebinds_without_restart(monkeypatch):
    command_queue = queue.Queue()
    event_queue = queue.Queue()
    stopped_units = []
    created = []

    monkeypatch.setattr(
        "pyleaner.watchdog._set_parent_death_signal", lambda *_args: None)
    monkeypatch.setattr(
        "pyleaner.watchdog._process_alive",
        lambda pid: pid in {9000, 1002})
    monkeypatch.setattr(
        "pyleaner.watchdog._lean_worker_pids",
        lambda _root_pid: {1: 1002})
    monkeypatch.setattr(
        "pyleaner.watchdog._read_oom_kill", lambda _path: 0)
    monkeypatch.setattr(
        "pyleaner.watchdog._read_rss_anon", lambda _pid: 0)

    def create_scope(pid, worker_id, memory_max):
        created.append((pid, worker_id, memory_max))
        return "new.scope", "/fake/new"

    monkeypatch.setattr(
        "pyleaner.watchdog._systemd_scope_for_pid", create_scope)
    monkeypatch.setattr(
        "pyleaner.watchdog._stop_systemd_scope",
        lambda unit: stopped_units.append(unit))

    command_queue.put({"type": "root", "pid": 9000, "generation": 1})
    command_queue.put({
        "type": "guards",
        "generation": 1,
        "guards": {
            1: {
                "pid": 1001,
                "unit": "old.scope",
                "cgroup_path": "/fake/old",
                "oom_kill": 0,
            },
        },
    })
    command_queue.put({
        "type": "task_started",
        "generation": 1,
        "worker_id": 1,
        "started_at": time.monotonic(),
    })
    monitor = _start_fake_monitor(command_queue, event_queue)
    try:
        event = event_queue.get(timeout=1)
        assert event["type"] == "guard_replaced"
        assert event["worker_id"] == 1
        assert event["old_pid"] == 1001
        assert event["guard"]["pid"] == 1002
        assert created == [(1002, 1, 2048)]
        assert stopped_units == ["old.scope"]
        with pytest.raises(queue.Empty):
            event_queue.get(timeout=0.05)
    finally:
        command_queue.put({"type": "stop"})
        monitor.join(timeout=1)
    assert not monitor.is_alive()


def test_logical_worker_pid_replacement_is_observed_without_cgroup(monkeypatch):
    command_queue = queue.Queue()
    event_queue = queue.Queue()

    monkeypatch.setattr(
        watchdog_module, "_set_parent_death_signal", lambda *_args: None)
    monkeypatch.setattr(
        watchdog_module, "_process_alive", lambda pid: pid in {9000, 1002})
    monkeypatch.setattr(
        watchdog_module, "_lean_worker_pids", lambda _root_pid: {1: 1002})
    monkeypatch.setattr(
        watchdog_module,
        "_systemd_scope_for_pid",
        lambda *_args: pytest.fail("cgroup setup must remain disabled"),
    )

    command_queue.put({"type": "root", "pid": 9000, "generation": 1})
    command_queue.put({
        "type": "guards",
        "generation": 1,
        "guards": {
            1: {
                "pid": 1001,
                "unit": "",
                "cgroup_path": "",
                "oom_kill": 0,
            },
        },
    })
    monitor = _start_fake_monitor(
        command_queue,
        event_queue,
        soft_memory_limit=None,
        hard_memory_limit=None,
    )
    try:
        event = event_queue.get(timeout=5)
        assert event["type"] == "guard_replaced"
        assert event["worker_id"] == 1
        assert event["old_pid"] == 1001
        assert event["guard"] == {
            "pid": 1002,
            "unit": "",
            "cgroup_path": "",
            "oom_kill": 0,
        }
    finally:
        command_queue.put({"type": "stop"})
        monitor.join(timeout=1)
    assert not monitor.is_alive()


def test_attach_worker_guards_tracks_pids_without_memory_limits(monkeypatch):
    client = FakeClient()
    client.process = FakeProcess()
    watchdog = Watchdog(client)
    watchdog._size = 2
    sent = []
    monkeypatch.setattr(
        watchdog_module,
        "_lean_worker_pids",
        lambda _root_pid: {1: 1001, 2: 1002},
    )
    monkeypatch.setattr(watchdog, "_send", lambda message: sent.append(message))
    watchdog.attach_worker_guards(timeout=1.0)

    assert watchdog._worker_scopes == {
        1: {"pid": 1001, "unit": "", "cgroup_path": "", "oom_kill": 0},
        2: {"pid": 1002, "unit": "", "cgroup_path": "", "oom_kill": 0},
    }
    assert sent == [{
        "type": "guards",
        "generation": 0,
        "guards": watchdog._worker_scopes,
    }]


def test_dead_worker_without_replacement_still_triggers_recovery(monkeypatch):
    command_queue = queue.Queue()
    event_queue = queue.Queue()

    monkeypatch.setattr(
        "pyleaner.watchdog._set_parent_death_signal", lambda *_args: None)
    monkeypatch.setattr(
        "pyleaner.watchdog._process_alive", lambda pid: pid == 9000)
    monkeypatch.setattr(
        "pyleaner.watchdog._lean_worker_pids", lambda _root_pid: {})
    monkeypatch.setattr(
        "pyleaner.watchdog._read_oom_kill", lambda _path: 0)
    monkeypatch.setattr(
        "pyleaner.watchdog._read_rss_anon", lambda _pid: 0)
    monkeypatch.setattr(
        "pyleaner.watchdog._systemd_scope_result",
        lambda _unit: "success")

    command_queue.put({"type": "root", "pid": 9000, "generation": 1})
    command_queue.put({
        "type": "guards",
        "generation": 1,
        "guards": {
            1: {
                "pid": 1001,
                "unit": "old.scope",
                "cgroup_path": "/fake/old",
                "oom_kill": 0,
            },
        },
    })
    command_queue.put({
        "type": "task_started",
        "generation": 1,
        "worker_id": 1,
        "started_at": time.monotonic(),
    })
    monitor = _start_fake_monitor(
        command_queue, event_queue, replacement_grace=0.02)
    try:
        event = event_queue.get(timeout=1)
        assert event["type"] == "trigger"
        assert event["trigger"] == "worker_death"
        assert event["worker_ids"] == [1]
        assert "without a replacement" in event["reason"]
    finally:
        command_queue.put({"type": "stop"})
        monitor.join(timeout=1)
    assert not monitor.is_alive()


def test_replacement_guard_becomes_parent_cleanup_owner(monkeypatch):
    stopped_units = []
    monkeypatch.setattr(
        "pyleaner.watchdog._stop_systemd_scope",
        lambda unit: stopped_units.append(unit))
    client = FakeClient()
    watchdog = Watchdog(client)
    watchdog._generation = 4
    watchdog._worker_scopes = {
        1: {
            "pid": 1001,
            "unit": "old.scope",
            "cgroup_path": "/fake/old",
            "oom_kill": 0,
        },
    }

    watchdog._accept_replacement_guard({
        "type": "guard_replaced",
        "generation": 4,
        "worker_id": 1,
        "old_pid": 1001,
        "guard": {
            "pid": 1002,
            "unit": "new.scope",
            "cgroup_path": "/fake/new",
            "oom_kill": 0,
        },
    })

    assert watchdog._worker_scopes[1]["pid"] == 1002
    assert stopped_units == []
    watchdog._stop_worker_scopes()
    assert stopped_units == ["new.scope"]


def test_hard_oom_wins_over_available_replacement(monkeypatch):
    command_queue = queue.Queue()
    event_queue = queue.Queue()
    created = []

    monkeypatch.setattr(
        "pyleaner.watchdog._set_parent_death_signal", lambda *_args: None)
    monkeypatch.setattr(
        "pyleaner.watchdog._process_alive",
        lambda pid: pid in {9000, 1002})
    monkeypatch.setattr(
        "pyleaner.watchdog._lean_worker_pids",
        lambda _root_pid: {1: 1002})
    monkeypatch.setattr(
        "pyleaner.watchdog._read_oom_kill",
        lambda path: 1 if path == "/fake/old" else 0)
    monkeypatch.setattr(
        "pyleaner.watchdog._read_rss_anon", lambda _pid: 0)
    monkeypatch.setattr(
        "pyleaner.watchdog._systemd_scope_for_pid",
        lambda *args: created.append(args))

    command_queue.put({"type": "root", "pid": 9000, "generation": 1})
    command_queue.put({
        "type": "guards",
        "generation": 1,
        "guards": {
            1: {
                "pid": 1001,
                "unit": "old.scope",
                "cgroup_path": "/fake/old",
                "oom_kill": 0,
            },
        },
    })
    command_queue.put({
        "type": "task_started",
        "generation": 1,
        "worker_id": 1,
        "started_at": time.monotonic(),
    })
    monitor = _start_fake_monitor(command_queue, event_queue)
    try:
        event = event_queue.get(timeout=1)
        assert event["type"] == "trigger"
        assert event["trigger"] == "memory"
        assert event["worker_ids"] == [1]
        assert created == []
    finally:
        command_queue.put({"type": "stop"})
        monitor.join(timeout=1)
    assert not monitor.is_alive()


def test_precise_memory_event_marks_only_its_worker_toxic(monkeypatch):
    class FakeWorker:
        def __init__(self, worker_id):
            self.worker_id = worker_id
            self.current_task = {"result_q": queue.Queue()}
            self.notification_queue = queue.Queue()
            self.rpc_session = type(
                "FakeRpcSession", (), {"invalidate": lambda self: None})()

    class FakePool:
        def __init__(self):
            self.workers = [FakeWorker(1), FakeWorker(2)]
            self.keep_alive_manager = None

    client = FakeClient()
    client.process = None
    client.worker_pool = FakePool()
    client.connect = lambda timeout=300: None
    client.create_pool = lambda text, size: None
    watchdog = Watchdog(client)
    client.watchdog = watchdog
    monkeypatch.setattr(
        "pyleaner.watchdog._kill_process_tree",
        lambda _process, known_pids=None: None)

    watchdog._restart(
        "memory",
        "Lean worker hit its cgroup hard memory limit (worker=2)",
        culprit_worker_ids={2},
    )

    first = client.worker_pool.workers[0].current_task
    second = client.worker_pool.workers[1].current_task
    assert first.get("_culprit") is None
    assert second["_culprit"] is True
    assert "memory limit" in second["_culprit_reason"]


def test_watchdog_process_failure_retries_tasks_without_toxicity(monkeypatch):
    class FakeWorker:
        worker_id = 1
        current_task = {"result_q": queue.Queue()}
        notification_queue = queue.Queue()
        rpc_session = type(
            "FakeRpcSession", (), {"invalidate": lambda self: None})()

    client = FakeClient()
    client.process = None
    client.worker_pool = type(
        "FakePool", (),
        {"workers": [FakeWorker()], "keep_alive_manager": None})()
    client.connect = lambda timeout=300: None
    client.create_pool = lambda text, size: None
    watchdog = Watchdog(client)
    client.watchdog = watchdog
    monkeypatch.setattr(
        "pyleaner.watchdog._kill_process_tree",
        lambda _process, known_pids=None: None)

    watchdog._restart(
        "watchdog_death",
        "monitor exited",
        culprit_worker_ids=set(),
    )

    assert client.worker_pool.workers[0].current_task.get("_culprit") is None


def test_transient_restart_failures_are_retried(monkeypatch):
    client = FakeClient()
    watchdog = Watchdog(client)
    attempts = []

    def flaky_restart(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("temporary restart failure")

    monkeypatch.setattr(watchdog, "_restart", flaky_restart)
    monkeypatch.setattr(watchdog._stop, "wait", lambda _delay: False)

    watchdog._recover_with_retry("death", "server exited", set())

    assert len(attempts) == 3
