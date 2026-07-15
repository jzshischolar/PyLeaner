"""Unit tests for coordinated RPC timeout and watchdog recovery behavior."""

import queue
import threading

import pytest

from pyleaner.client import _submit_resilient
from pyleaner.errors import ServiceUnavailable
from pyleaner.rpc_session import RpcSession, RpcTimeoutError
from pyleaner.watchdog import (
    RESILIENT_RESPONSE_TIMEOUT,
    RPC_RESPONSE_TIMEOUT,
    WEDGE_DEADLINE,
    WATCHDOG_POLL_INTERVAL,
    Watchdog,
)


class FakeClient:
    def __init__(self):
        self._pending_lock = threading.Lock()
        self._pending_requests = {}
        self.worker_pool = None


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
