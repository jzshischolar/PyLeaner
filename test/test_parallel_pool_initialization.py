from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from pyleaner import LspClient
from pyleaner.pool import WorkerPool


class _BarrierWorker:
    def __init__(self, barrier: threading.Barrier, result: bool) -> None:
        self.barrier = barrier
        self.result = result
        self.thread_id: int | None = None

    def initialize_environment(self) -> bool:
        self.thread_id = threading.get_ident()
        self.barrier.wait(timeout=2.0)
        return self.result


def _pool(workers) -> WorkerPool:
    pool = object.__new__(WorkerPool)
    pool.workers = list(workers)
    return pool


def test_worker_environments_initialize_concurrently(capsys) -> None:
    barrier = threading.Barrier(4)
    workers = [_BarrierWorker(barrier, True) for _ in range(4)]

    _pool(workers).initialize_all_workers()

    assert len({worker.thread_id for worker in workers}) == 4
    assert capsys.readouterr().out == ""


def test_parallel_initialization_preserves_partial_and_total_failure(capsys) -> None:
    partial_barrier = threading.Barrier(3)
    partial = [
        _BarrierWorker(partial_barrier, True),
        _BarrierWorker(partial_barrier, False),
        _BarrierWorker(partial_barrier, True),
    ]
    _pool(partial).initialize_all_workers()
    assert "1/3 workers failed" in capsys.readouterr().out

    failed_barrier = threading.Barrier(2)
    failed = [_BarrierWorker(failed_barrier, False) for _ in range(2)]
    with pytest.raises(RuntimeError, match="All 2 workers failed"):
        _pool(failed).initialize_all_workers()


class _OverlapDetectingStream:
    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._active = 0
        self.overlap = False
        self.writes = 0

    def write(self, _payload: bytes) -> None:
        with self._state_lock:
            self._active += 1
            self.overlap = self.overlap or self._active > 1
        time.sleep(0.01)
        with self._state_lock:
            self._active -= 1
            self.writes += 1

    def flush(self) -> None:
        return None


def test_concurrent_lsp_messages_are_framed_serially() -> None:
    client = LspClient(["unused"])
    stream = _OverlapDetectingStream()
    client.process = SimpleNamespace(stdin=stream)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(client.notify, "test/message", {"index": index})
            for index in range(16)
        ]
        for future in futures:
            future.result()

    assert stream.writes == 16
    assert stream.overlap is False
