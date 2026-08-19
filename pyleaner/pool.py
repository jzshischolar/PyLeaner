"""Worker pool with load balancing for parallel Lean LSP processing."""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TYPE_CHECKING

from . import debug_log, Task
from .rpc_session import KeepAliveManager
from .worker import Worker
from .observability import new_correlation_id, source_fingerprint_from_kwargs

if TYPE_CHECKING:
    from .client import LspClient


def _environment_fingerprint(client: LspClient, kwargs: dict) -> str | None:
    resolver = getattr(client, "task_environment_fingerprint", None)
    return resolver(kwargs) if callable(resolver) else None


class WorkerPool:
    """Pool of worker threads with initialized Lean environments."""

    def __init__(
        self,
        client: LspClient,
        size: int = 3,
        init_uri: str = "",
        init_text: str = "",
    ):
        """Initialize the worker pool.

        Args:
            client: The LspClient instance
            size: Number of worker environments
            init_uri: Initial file URI for environment initialization
            init_text: Initial file content
        """
        self.client = client
        self.overall_task_queue: queue.Queue[Task] = queue.Queue()
        self.keep_alive_manager = KeepAliveManager()
        self.keep_alive_manager.start()

        def create_worker(i: int) -> Worker:
            return Worker(
                client,
                i,
                init_uri + f"worker_{i}.lean",
                init_text,
                self.keep_alive_manager,
            )

        # Parallel worker initialization
        with ThreadPoolExecutor(max_workers=size) as executor:
            futures = [executor.submit(create_worker, i + 1) for i in range(size)]
            self.workers: list[Worker] = [f.result() for f in as_completed(futures)]

        self.workers.sort(key=lambda w: w.worker_id)
        debug_log(f"WorkerPool initialized with {size} workers")

    # ── Infrastructure ──────────────────────────────────────

    def initialize_all_workers(self) -> None:
        """Initialize every Lean document concurrently.

        Must be called AFTER LspClient.worker_pool is assigned so that
        notification routing works during _didopen.

        Raises RuntimeError if *all* workers fail to start.
        """
        with ThreadPoolExecutor(
            max_workers=len(self.workers),
            thread_name_prefix="pyleaner-environment-init",
        ) as executor:
            futures = [
                executor.submit(worker.initialize_environment)
                for worker in self.workers
            ]
            failed = sum(
                not future.result()
                for future in as_completed(futures)
            )
        if failed == len(self.workers):
            raise RuntimeError(
                f"All {failed} workers failed to initialize their Lean environment."
            )
        if failed:
            print(f"[WARNING] {failed}/{len(self.workers)} workers failed to start; "
                  f"continuing with {len(self.workers) - failed} healthy.", flush=True)

    def router(self):
        """Route tasks to the least-busy *initialized* worker."""
        while True:
            task = self.overall_task_queue.get()
            ready = [w for w in self.workers if w._ready]
            if not ready:
                # No healthy workers left — fail the task so the caller
                # can retry or escalate instead of wedging silently.
                rq = task.get("result_q")
                if rq is not None:
                    rq.put({
                        "success": False,
                        "error": RuntimeError("No healthy workers available"),
                    })
                self.client.emit_execution_event(
                    "task_rejected",
                    request_id=task.get("request_id"),
                    task_id=task.get("task_id"),
                    task_type=task.get("task_type"),
                    source_fingerprint=source_fingerprint_from_kwargs(
                        task.get("kwargs", {})),
                    environment_fingerprint=_environment_fingerprint(
                        self.client, task.get("kwargs", {})),
                    outcome="infrastructure_error",
                    details={"reason": "no_healthy_workers"},
                )
                continue
            worker = min(ready, key=lambda w: w.task_queue.qsize())
            self.client.emit_execution_event(
                "task_assigned",
                request_id=task.get("request_id"),
                task_id=task.get("task_id"),
                task_type=task.get("task_type"),
                worker_id=worker.worker_id,
                document_uri=worker.uri,
                source_fingerprint=source_fingerprint_from_kwargs(
                    task.get("kwargs", {})),
                environment_fingerprint=_environment_fingerprint(
                    self.client, task.get("kwargs", {})),
                details={"context": dict(task.get("context", {}))},
            )
            worker.task_queue.put(task)

    def submit_task(self, task: Task):
        """Submit a raw task dict. Prefer the convenience methods below."""
        task.setdefault("request_id", new_correlation_id())
        task.setdefault("task_id", new_correlation_id())
        task.setdefault("context", {})
        self.client.emit_execution_event(
            "task_submitted",
            request_id=task.get("request_id"),
            task_id=task.get("task_id"),
            task_type=task.get("task_type"),
            source_fingerprint=source_fingerprint_from_kwargs(
                task.get("kwargs", {})),
            environment_fingerprint=_environment_fingerprint(
                self.client, task.get("kwargs", {})),
            details={"context": dict(task.get("context", {}))},
        )
        self.overall_task_queue.put(task)

    def get_worker_for_uri(self, uri: str) -> Worker:
        """Get the worker that handles the given URI."""
        for worker in self.workers:
            if uri == worker.uri:
                return worker
        raise RuntimeError(f"No worker found for URI: {uri}")

    # ── Internal ────────────────────────────────────────────

    def _submit(self, task_type: str, kwargs: dict,
                timeout: float = 60.0) -> Any:
        """Submit a task and return the result content (or raise on error)."""
        rq: queue.Queue = queue.Queue()
        self.submit_task({
            "task_type": task_type, "result_q": rq, "kwargs": kwargs,
        })
        resp = rq.get(timeout=timeout)
        if resp["success"]:
            return resp["content"]
        raise RuntimeError(resp.get("error", "unknown error"))

    # ── Public API ──────────────────────────────────────────

    def ping(self, timeout: float = 60.0) -> Any:
        """Ping the Lean server (RPC round-trip test)."""
        return self._submit("ping", {}, timeout=timeout)

    def echo(self, message: str, timeout: float = 60.0) -> Any:
        """Send an echo RPC call with parameters."""
        return self._submit("echo", {"message": message}, timeout=timeout)

    def extract_declarations(
        self, text: str, content_range: dict | None = None,
        timeout: float = 60.0
    ) -> Any:
        """Extract all declarations from a Lean source file.

        Args:
            text: Full source text of the Lean file.
            content_range: LSP range to replace, or None for full file.
            timeout: Max wait time in seconds.

        Returns:
            A dict with
            ``{"success": bool, "decls": [...], "diagnostics": [...]}``.
            Diagnostics come from the same document update and elaboration as
            the declarations; the additional key is backward compatible.
            Each decl has ``kind``, ``name``, ``params``, ``paramsText``,
            ``typeText``, ``bodyText``, ``bodyRange``, ``fields``,
            ``fullText``, ``range``, ``hasError``, ``errorMessage``.
            ``fields`` is non-null only for structures/classes. Field source
            data (name, type text, binder kind, range) remains available for
            partially elaborated declarations; projection/class/Prop metadata
            is null when Lean could not create or classify the projection.
        """
        if content_range is None:
            content_range = {
                "start": {"line": 0, "character": 0},
                "end": {"line": 999, "character": 0},
            }
        return self._submit("extract_declarations", {
            "text": text, "content_range": content_range,
        }, timeout=timeout)

    def search_declarations(
        self, text: str, query: str, max_results: int = 8, fuzzy: bool = False,
        content_range: dict | None = None, timeout: float = 60.0
    ) -> Any:
        """Return bounded declaration-name matches from Lean's environment."""
        if max_results < 1:
            raise ValueError("max_results must be at least one")
        return self._submit("search_declarations", {
            "text": text,
            "query": query,
            "max_results": max_results,
            "fuzzy": fuzzy,
            "content_range": content_range or {},
        }, timeout=timeout)

    def declaration_axioms(
        self, text: str, declaration_name: str,
        content_range: dict | None = None, timeout: float = 60.0,
    ) -> Any:
        """Return the transitive kernel axiom dependencies of a declaration."""
        if not declaration_name.strip():
            raise ValueError("declaration_name must not be empty")
        return self._submit("declaration_axioms", {
            "text": text,
            "declaration_name": declaration_name,
            "content_range": content_range or {},
        }, timeout=timeout)

    def get_diagnostics(
        self, text: str, content_range: dict | None = None,
        timeout: float = 60.0
    ) -> list:
        """Get Lean compiler diagnostics (errors/warnings) for a file.

        Returns:
            A list of diagnostic objects with ``message``, ``range``,
            ``severity``, etc.
        """
        if content_range is None:
            content_range = {}
        return self._submit("changecontent", {
            "text": text, "content_range": content_range,
        }, timeout=timeout)

    def get_proof_goal(
        self, text: str, position: dict,
        content_range: dict | None = None,
        timeout: float = 60.0
    ) -> dict:
        """Get the proof goal state at a position.

        Args:
            text: Full source text of the Lean file.
            position: LSP position ``{"line": int, "character": int}``.
            content_range: LSP range to replace, or None for full file.
            timeout: Max wait time in seconds.

        Returns:
            ``{"diagnostics": [...], "proof_goal": [...] | None}``.
        """
        if content_range is None:
            content_range = {
                "start": {"line": 0, "character": 0},
                "end": {"line": 999, "character": 0},
            }
        return self._submit("get_proof_goal", {
            "text": text, "content_range": content_range, "position": position,
        }, timeout=timeout)

    # ── Debug / dev methods ─────────────────────────────────

    def debug_document(self, timeout: float = 60.0) -> Any:
        """Read document content from the Lean side."""
        return self._submit("debug_document", {}, timeout=timeout)

    def parse_document(self, timeout: float = 60.0) -> Any:
        """Get command-level parse tree of the document."""
        return self._submit("parse_document", {}, timeout=timeout)

    def test_declaration_kind(self, timeout: float = 60.0) -> Any:
        """Identify declaration kinds in the document."""
        return self._submit("test_declaration_kind", {}, timeout=timeout)

    def test_declaration_name(self, timeout: float = 60.0) -> Any:
        """Extract declaration names."""
        return self._submit("test_declaration_name", {}, timeout=timeout)

    def test_has_params(self, timeout: float = 60.0) -> Any:
        """Check which declarations have parameters."""
        return self._submit("test_has_params", {}, timeout=timeout)

    def test_params_text(self, timeout: float = 60.0) -> Any:
        """Extract parameter text for all declarations."""
        return self._submit("test_params_text", {}, timeout=timeout)

    def test_type_text(self, timeout: float = 60.0) -> Any:
        """Extract return type text for all declarations."""
        return self._submit("test_type_text", {}, timeout=timeout)

    def test_body_text(self, timeout: float = 60.0) -> Any:
        """Extract body text for all declarations."""
        return self._submit("test_body_text", {}, timeout=timeout)

    def test_body_fields(self, timeout: float = 60.0) -> Any:
        """Test body extraction for structures/inductives."""
        return self._submit("test_body_fields", {}, timeout=timeout)

    def debug_body_fields(self, timeout: float = 60.0) -> Any:
        """Debug body extraction for structures."""
        return self._submit("debug_body_fields", {}, timeout=timeout)

    def debug_syntax_tree(self, timeout: float = 60.0) -> Any:
        """Dump the syntax tree for debugging."""
        return self._submit("debug_syntax_tree", {}, timeout=timeout)

    def debug_binder_structure(self, timeout: float = 60.0) -> Any:
        """Inspect binder node structure for debugging."""
        return self._submit("debug_binder_structure", {}, timeout=timeout)

    def debug_all_snapshots(self, timeout: float = 60.0) -> Any:
        """Dump all command snapshots for debugging."""
        return self._submit("debug_all_snapshots", {}, timeout=timeout)

    def debug_snapshot_info(self, timeout: float = 60.0) -> Any:
        """Show snapshot error information."""
        return self._submit("debug_snapshot_info", {}, timeout=timeout)
