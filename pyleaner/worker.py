"""Worker thread with initialized Lean environment and RPC session."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Optional, TYPE_CHECKING

from . import debug_log, Task
from .rpc_session import RpcSession, KeepAliveManager
from .errors import ServiceUnavailable
from .watchdog import POISON_METHOD

if TYPE_CHECKING:
    from .client import LspClient


class Worker:
    """A worker thread with initialized Lean environment and RPC session."""

    def __init__(
        self,
        client: LspClient,
        worker_id: int,
        init_uri: str,
        init_text: str,
        keep_alive_manager: Optional[KeepAliveManager] = None,
        language_id: str = "lean4",
    ):
        """Initialize a worker with Lean environment and RPC session.

        Args:
            client: The LspClient instance
            worker_id: Worker identifier
            init_uri: Initial file URI for environment initialization
            init_text: Initial file content
            keep_alive_manager: KeepAliveManager instance for RPC session keep-alive
            language_id: Language identifier (default: lean4)
        """
        self.client = client
        self.worker_id = worker_id
        self.task_queue = queue.Queue()
        # Queue for receiving notifications related to this worker
        self.notification_queue = queue.Queue()
        # Track document version for this worker
        self.document_version: int = 0
        # Track which URI this worker is responsible for
        self.uri = init_uri
        # In-flight task tracking: for the watchdog's wedge-deadline detection
        # (task_started_at) and toxic attribution (current_task).
        self.current_task: Optional[Task] = None
        self.task_started_at: Optional[float] = None
        # Set to True once _didopen succeeds; the router skips workers that are
        # not ready so a failed initialization doesn't silently wedge tasks.
        self._ready: bool = False

        self.process_funcs: dict[str, Any] = {
            "ping": self.ping,
            "echo": self.echo,
            "debug_document": self.debug_document,
            "parse_document": self.parse_document,
            "test_declaration_kind": self.test_declaration_kind,
            "test_declaration_name": self.test_declaration_name,
            "extract_declarations": self.extract_declarations,
            "search_declarations": self.search_declarations,
            "test_has_params": self.test_has_params,
            "test_params_text": self.test_params_text,
            "test_type_text": self.test_type_text,
            "test_body_text": self.test_body_text,
            "test_body_fields": self.test_body_fields,
            "debug_body_fields": self.debug_body_fields,
            "debug_syntax_tree": self.debug_syntax_tree,
            "debug_binder_structure": self.debug_binder_structure,
            "debug_all_snapshots": self.debug_all_snapshots,
            "debug_snapshot_info": self.debug_snapshot_info,
            "changecontent": self.get_diagnostics,
            "get_diagnostics": self.get_diagnostics,
            "get_proof_goal": self.get_proof_goal,
        }

        # Store init params for lazy initialization (called after worker_pool ready)
        self._init_uri = init_uri
        self._init_text = init_text
        self._init_language_id = language_id
        self._keep_alive_manager = keep_alive_manager

        # Create RPC session for this worker
        self.rpc_session = RpcSession(worker_id, init_uri, client, keep_alive_manager)

        # Start worker thread
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"worker-{worker_id}"
        )
        self.thread.start()

    def initialize_environment(self) -> bool:
        """Call _didopen after worker_pool is ready to receive notifications.

        Returns True if the Lean environment initialized successfully.
        """
        debug_log(f"Worker {self.worker_id}: initializing environment with {self._init_uri}")
        ok = self._didopen(self._init_uri, self._init_text,
                           language_id=self._init_language_id, timeout=300.0)
        if not ok:
            print(f"[ERROR] worker_id:{self.worker_id} start failed.", flush=True)
            self._ready = False
        else:
            self._ready = True
            print(f"worker_id:{self.worker_id} started.")
        return ok

    # ── Thread loop ──────────────────────────────────────────

    def _run(self):
        """Worker thread main loop."""
        while True:
            task: Task = self.task_queue.get()
            if task is None:  # Shutdown signal
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
                # Poisoned by the watchdog during a restart: fail the in-flight
                # task (toxic flag from its _culprit mark) + drain queued tasks
                # as innocent, then exit (the hard restart replaces this pool).
                self._on_service_unavailable()
                break
            except Exception as e:
                if result_q is not None:
                    result_q.put({"success": False, "error": e})
            finally:
                self.current_task = None
                self.task_started_at = None

    def _on_service_unavailable(self) -> None:
        """Poisoned by the watchdog mid-restart.

        Fail the in-flight task (toxic flag from its ``_culprit`` mark) and drain
        the task_queue, failing every queued (innocent) task as non-toxic so its
        caller transparently retries on the new pool.
        """
        cur = self.current_task
        if cur is not None and cur.get("result_q") is not None:
            cur["result_q"].put({
                "success": False,
                "error": ServiceUnavailable(),
                "toxic": bool(cur.get("_culprit", False)),
            })
        while True:
            try:
                t = self.task_queue.get_nowait()
            except queue.Empty:
                break
            if t is None:
                continue
            rq = t.get("result_q")
            if rq is not None:
                rq.put({
                    "success": False,
                    "error": ServiceUnavailable(),
                    "toxic": False,
                })

    # ── Internal communication ───────────────────────────────

    def _wait_result(self, result_q: queue.Queue, method: str, timeout: float) -> Any:
        """Wait for result from the worker."""
        try:
            response = result_q.get(timeout=timeout)
            if response["success"]:
                return response["result"]
            else:
                raise response["error"]
        except queue.Empty:
            raise TimeoutError(f"Timeout waiting for result: {method}")

    def _submit_lsp(self, method: str, params: dict) -> Any:
        """Submit an LSP request to this worker."""
        result = self.client.request(method, params, self.worker_id + 1000, 60.0)
        return result

    def _submit_rpc(self, position: dict, method: str, params: dict) -> Any:
        """Submit an RPC request to this worker."""
        return self.rpc_session.call(method, params, position)

    # ── Document management ──────────────────────────────────

    def _get_next_version(self) -> int:
        """Get next version for a document."""
        self.document_version += 1
        return self.document_version

    def _reset_version(self) -> None:
        """Reset version for a document (for didOpen)."""
        self.document_version = 0

    def _didopen(
        self, uri: str, text: str, language_id: str = "lean4", timeout: float = 300.0
    ) -> bool:
        """Send textDocument/didOpen notification and wait for processing."""
        self._reset_version()

        self.client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 0,
                "text": text,
            }
        })

        # Drain any notifications buffered during WorkerPool construction
        for msg in self.client._drain_init_buffer(uri):
            self.notification_queue.put(msg)

        try:
            while True:
                msg = self.notification_queue.get(timeout=timeout)
                debug_log(f"Worker {self.worker_id}: file processing ...")
                method = msg.get("method")

                if method == "$/lean/fileProgress":
                    params = msg.get("params", {})
                    if (
                        params.get("processing", []) == []
                        and params.get("textDocument", {}).get("version", None) == 0
                    ):
                        debug_log(f"Worker {self.worker_id}: file processing completed")
                        break
            return True
        except Exception as e:
            debug_log(f"Worker {self.worker_id}: error {e}")
            return False

    def _didchange(
        self, text: str, content_range: dict, timeout: float = 120.0
    ) -> list:
        """Send textDocument/didChange notification and wait for processing.

        Returns:
            List of error diagnostics (severity=1 only).
        """
        if content_range == {}:
            content_range = {
                "start": {"line": 0, "character": 0},
                "end": {"line": 999, "character": 0},
            }

        version = self._get_next_version()

        self.client.notify("textDocument/didChange", {
            "textDocument": {
                "uri": self.uri,
                "version": version,
            },
            "contentChanges": [{"range": content_range, "text": text}],
        })

        counter = 0
        diagnostics = None
        valid_diagnostics = []
        while True:
            msg = self.notification_queue.get(timeout=timeout)
            method = msg.get("method")

            # Poisoned by the watchdog during a restart: abort this didChange so
            # the task fails fast (and the worker drains its queue) instead of
            # blocking until the deadline.
            if method == POISON_METHOD:
                raise ServiceUnavailable()

            if (
                method == "textDocument/publishDiagnostics"
                and msg.get("params", {}).get("version", None) == version
            ):
                diagnostics = msg.get("params", {}).get("diagnostics", [])

            if (
                method == "$/lean/fileProgress"
                and msg.get("params", {}).get("processing", []) == []
                and msg.get("params", {}).get("textDocument", {}).get("version", None)
                == version
            ):
                counter += 1

            if counter >= 2 and diagnostics is not None:
                time.sleep(2)
                if not self.notification_queue.empty():
                    continue
                valid_diagnostics = []
                try:
                    for diag in diagnostics:
                        severity = diag.get("severity")
                        if severity is None or severity <= 3:
                            valid_diagnostics.append(diag)
                        else:
                            continue
                    break
                except Exception:
                    print("error!")
                    break

        return valid_diagnostics

    # ── Public API ────────────────────────────────────────────

    def get_proof_goal(self, text, content_range, position) -> Any:
        diagnostics = self._didchange(text, content_range)
        params = {
            "textDocument": {
                "uri": self.uri,
                "version": self.document_version,
            },
            "position": position,
        }
        result = self._submit_lsp("$/lean/plainGoal", params)
        # result is already unwrapped by request() — can be dict or None
        if isinstance(result, dict):
            goals = result.get("goals", None)
        else:
            goals = None  # null = no goals at this position
        return {"diagnostics": diagnostics, "proof_goal": goals}

    def get_diagnostics(self, text, content_range) -> Any:
        return self._didchange(text, content_range)

    def ping(self) -> Any:
        """Send lean/ping RPC call (for debugging)."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.ping", {}
        )

    def echo(self, message: str) -> Any:
        """Send lean/echo RPC call (for debugging)."""
        return self._submit_rpc(
            {"line": 0, "character": 0},
            "LeanLspExtension.echo",
            {"textDocument": {"uri": self.uri}, "message": message},
        )

    def extract_declarations(self, text, content_range) -> Any:
        """Extract declarations after atomically updating this worker's document.

        Structure and class declarations include a ``fields`` array with source
        binder metadata plus optional elaboration metadata. All declarations
        include ``bodyRange`` when Lean exposes an exact body syntax node.  The
        response also carries the diagnostics produced by the same document
        update, so callers do not need a second ``didChange`` merely to verify
        that the extracted snapshot elaborated successfully.
        """
        diagnostics = self._didchange(text, content_range)
        result = self._submit_rpc(
            {"line": 0, "character": 0},
            "LeanLspExtension.extractDeclarations",
            {},
        )
        if isinstance(result, dict):
            # This is deliberately a Python transport extension instead of a
            # Loc-Decomp-specific Lean RPC field.  Existing consumers that only
            # read ``success``/``decls`` remain source-compatible.
            result = dict(result)
            result["diagnostics"] = diagnostics
        return result

    def search_declarations(
        self, text, query, max_results=8, fuzzy=False, content_range=None
    ) -> Any:
        """Search the current elaborated environment for declaration names.

        The document is updated first so local declarations and imports match
        the diagnostics being repaired.  Matching and type pretty-printing are
        performed by Lean; Python adds no domain-specific aliases.
        """
        diagnostics = self._didchange(text, content_range or {})
        result = self._submit_rpc(
            {"line": 0, "character": 0},
            "LeanLspExtension.searchDeclarations",
            {"query": query, "maxResults": max_results, "fuzzy": fuzzy},
        )
        if isinstance(result, dict):
            result = dict(result)
            result["diagnostics"] = diagnostics
        return result

    def debug_document(self) -> Any:
        """Send lean/debugDocument RPC call to read document content."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugDocument", {}
        )

    def parse_document(self) -> Any:
        """Send lean/parseDocument RPC call to parse document structure."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.parseDocument", {}
        )

    def test_declaration_kind(self) -> Any:
        """Send lean/testDeclarationKind RPC call to identify declaration kinds."""
        return self._submit_rpc(
            {"line": 0, "character": 0},
            "LeanLspExtension.testDeclarationKind",
            {},
        )

    def test_declaration_name(self) -> Any:
        """Send lean/testDeclarationName RPC call to extract declaration names."""
        return self._submit_rpc(
            {"line": 0, "character": 0},
            "LeanLspExtension.testDeclarationName",
            {},
        )

    def test_has_params(self) -> Any:
        """Send lean/testHasParams RPC call to check if declarations have params."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.testHasParams", {}
        )

    def test_params_text(self) -> Any:
        """Send lean/testParamsText RPC call to extract parameters text."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.testParamsText", {}
        )

    def test_type_text(self) -> Any:
        """Send lean/testTypeText RPC call to extract return type text."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.testTypeText", {}
        )

    def test_body_text(self) -> Any:
        """Send lean/testBodyText RPC call to extract body text."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.testBodyText", {}
        )

    def test_body_fields(self) -> Any:
        """Send lean/testBodyFields RPC call to test body extraction for structures."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.testBodyFields", {}
        )

    def debug_body_fields(self) -> Any:
        """Send lean/debugBodyFields RPC call to debug body extraction."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugBodyFields", {}
        )

    def debug_snapshot_info(self) -> Any:
        """Send lean/debugSnapshotInfo RPC call to debug snapshot info."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugSnapshotInfo", {}
        )

    def debug_all_snapshots(self) -> Any:
        """Send lean/debugAllSnapshots RPC call to debug all snapshots."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugAllSnapshots", {}
        )

    def debug_syntax_tree(self) -> Any:
        """Send lean/debugSyntaxTree RPC call to debug syntax tree structure."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugSyntaxTree", {}
        )

    def debug_binder_structure(self) -> Any:
        """Send lean/debugBinderStructure RPC call to inspect binder internals."""
        return self._submit_rpc(
            {"line": 0, "character": 0}, "LeanLspExtension.debugBinderStructure", {}
        )
