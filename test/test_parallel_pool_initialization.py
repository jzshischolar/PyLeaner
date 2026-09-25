from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import queue
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
    pool.overall_task_queue = queue.Queue()
    pool._selection_cursor = 0
    return pool


class _RoutingWorker:
    def __init__(self, worker_id: int, *, busy: bool = False) -> None:
        self.worker_id = worker_id
        self.uri = f"file:///worker_{worker_id}.lean"
        self.current_task = object() if busy else None
        self._ready = True
        self.task_queue: queue.Queue = queue.Queue()


class _RoutingClient:
    def emit_execution_event(self, *_args, **_kwargs) -> None:
        return None

    def task_environment_fingerprint(self, _kwargs) -> str:
        return "test"


def test_router_counts_inflight_work_and_rotates_idle_ties() -> None:
    workers = [_RoutingWorker(index) for index in range(1, 5)]
    pool = _pool(workers)
    pool.client = _RoutingClient()

    thread = threading.Thread(target=pool.router, daemon=True)
    thread.start()
    for index in range(4):
        pool.submit_task({"task_type": "ping", "kwargs": {},
                          "result_q": queue.Queue(), "task_id": str(index)})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(worker.task_queue.qsize() == 1 for worker in workers):
            break
        time.sleep(0.01)
    assert [worker.task_queue.qsize() for worker in workers] == [1, 1, 1, 1]

    # An executing task is part of the load even when its queue is empty.
    workers[0].current_task = object()
    for worker in workers[1:]:
        while not worker.task_queue.empty():
            worker.task_queue.get_nowait()
    while not workers[0].task_queue.empty():
        workers[0].task_queue.get_nowait()
    pool.submit_task({"task_type": "ping", "kwargs": {},
                      "result_q": queue.Queue(), "task_id": "busy"})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and workers[1].task_queue.qsize() == 0:
        time.sleep(0.01)
    assert workers[1].task_queue.qsize() == 1


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
