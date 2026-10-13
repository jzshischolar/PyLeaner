import json
import queue
import threading
from types import SimpleNamespace

import pytest

from pyleaner import LeanTaskTimeout, LeanTimeoutExhausted
from pyleaner.client import _submit_resilient
from pyleaner.timeout_policy import (
    sample_timeout_scope, run_lean_tool, finish_timeout_feedback, note_lean_submission,
)
from pyleaner.worker import Worker


def timeout():
    note_lean_submission()
    raise LeanTaskTimeout('deadline exceeded')


def test_timeout_streak_survives_resume_and_does_not_reexecute_receipt(tmp_path):
    path = tmp_path / 'sample.json'
    for i in range(3):
        with sample_timeout_scope(path):
            _, feedback = run_lean_tool(timeout, key=f'g{i}:call')
            assert feedback['consecutive_timeouts'] == i + 1
            _, replay = run_lean_tool(lambda: pytest.fail('replayed timeout'), key=f'g{i}:call')
            assert replay == feedback
            if i < 2:
                finish_timeout_feedback(feedback)
            else:
                with pytest.raises(LeanTimeoutExhausted):
                    finish_timeout_feedback(feedback)
    with pytest.raises(LeanTimeoutExhausted):
        with sample_timeout_scope(path):
            pytest.fail('terminal sample resumed')


def test_completed_lean_tool_resets_streak_but_auxiliary_rpc_does_not(tmp_path):
    path = tmp_path / 'sample.json'
    with sample_timeout_scope(path):
        run_lean_tool(timeout, key='one')
        run_lean_tool(lambda: 'no Lean submission')
        assert json.loads(path.read_text())['consecutive'] == 1
        def partial_success_then_timeout():
            note_lean_submission()  # successful preparatory RPC
            timeout()
        _, feedback = run_lean_tool(partial_success_then_timeout, key='two')
        assert feedback['consecutive_timeouts'] == 2
        run_lean_tool(lambda: (note_lean_submission(), 'compiler error'))
        _, feedback = run_lean_tool(timeout, key='three')
        assert feedback['consecutive_timeouts'] == 1


def test_sample_scopes_are_isolated_and_transport_errors_are_not_timeouts(tmp_path):
    with sample_timeout_scope(tmp_path / 'a.json'):
        run_lean_tool(timeout, key='a')
        with sample_timeout_scope(tmp_path / 'b.json'):
            _, feedback = run_lean_tool(timeout, key='b')
            assert feedback['consecutive_timeouts'] == 1
        def broken():
            raise ConnectionError('service down')
        with pytest.raises(ConnectionError):
            run_lean_tool(broken)
        _, feedback = run_lean_tool(timeout, key='a2')
        assert feedback['consecutive_timeouts'] == 2


def test_didchange_deadline_cancels_source_and_preserves_timeout_type():
    sent = []
    worker = object.__new__(Worker)
    worker.client = SimpleNamespace(notify=lambda *args: sent.append(args))
    worker.document_version = 0
    worker.uri = 'file:///test.lean'
    worker.notification_queue = queue.Queue()
    with pytest.raises(LeanTaskTimeout):
        worker._didchange('expensive proof', {}, timeout=0)
    assert len(sent) == 2
    assert sent[1][1]['contentChanges'][0]['text'] == ''
    assert sent[1][1]['textDocument']['version'] > sent[0][1]['textDocument']['version']


def test_client_returns_worker_timeout_without_retrying():
    error = LeanTaskTimeout('120 seconds')
    calls = []
    class Pool:
        def submit_task(self, task):
            calls.append(task)
            task['result_q'].put({'success': False, 'error': error})
    ready = threading.Event()
    ready.set()
    client = SimpleNamespace(worker_pool=Pool(), watchdog=SimpleNamespace(server_ready=ready))
    with pytest.raises(LeanTaskTimeout) as raised:
        _submit_resilient(client, 'get_diagnostics', {'text': 'expensive'})
    assert raised.value is error
    assert len(calls) == 1


def test_cancellation_failure_does_not_hide_observed_timeout():
    calls = []
    def notify(*args):
        calls.append(args)
        if len(calls) == 2:
            raise BrokenPipeError('worker exited during cancellation')
    worker = object.__new__(Worker)
    worker.client = SimpleNamespace(notify=notify)
    worker.document_version = 0
    worker.uri = 'file:///test.lean'
    worker.notification_queue = queue.Queue()
    with pytest.raises(LeanTaskTimeout):
        worker._didchange('expensive proof', {}, timeout=0)
