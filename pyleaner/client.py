"""LSP client for communicating with the Lean 4 language server."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Optional, Dict, TYPE_CHECKING

from . import debug_log
from .errors import ServiceUnavailable, ToxicTaskError
from .rpc_session import RpcTimeoutError
from .watchdog import FATAL_RE, RESILIENT_RESPONSE_TIMEOUT
from .observability import EventSink, emit_safely, new_correlation_id
from .observability import fingerprint_lean_environment, fingerprint_text

if TYPE_CHECKING:
    from .pool import WorkerPool


# Global registry of root PIDs started by any LspClient.  _cleanup_lean_processes
# walks these on abnormal exit so even non-orphaned children are reaped.
_CHILD_ROOT_PIDS: set = set()
_CHILD_PIDS_LOCK = threading.Lock()


def _cleanup_lean_processes() -> None:
    """Best-effort reaper for Lean processes on abnormal exit.

    Registered once (atexit + SIGTERM + SIGHUP).  Kills every PID in the global
    registry (using _kill_process_tree logic where possible), then falls back
    to ``_find_orphaned_lean`` for any processes that were reparented to init.
    """
    from .watchdog import _kill_process_tree, _find_orphaned_lean, _collect_descendants

    # Phase 1: kill known root PIDs (processes we started).
    with _CHILD_PIDS_LOCK:
        known = list(_CHILD_ROOT_PIDS)
        _CHILD_ROOT_PIDS.clear()
    for root_pid in known:
        try:
            descendants = _collect_descendants(root_pid)
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            os.kill(root_pid, signal.SIGKILL)
        except Exception:
            pass

    # Phase 2: sweep for any remaining orphans (reparented to init).
    try:
        for pid in _find_orphaned_lean():
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass


def _is_dead_server_error(err: Exception) -> bool:
    """Return True if ``err`` indicates the server process is dead (pipe/conn gone).

    Covers: BrokenPipeError (EPIPE), ConnectionResetError (ECONNRESET), and
    any OSError with errno 32 or 104 that means the other end of the pipe is closed.
    """
    if isinstance(err, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(err, OSError) and getattr(err, "errno", None) in (32, 104):
        return True
    msg = str(err)
    if "Broken pipe" in msg or "Errno 32" in msg:
        return True
    return False


def _submit_resilient(client: "LspClient", task_type: str, kwargs: dict,
                      timeout: float = 120.0, *,
                      request_id: Optional[str] = None,
                      context: Optional[dict[str, Any]] = None):
    """Submit a task with transparent crash/wedge recovery.

    Waits for ``server_ready``, submits to the CURRENT worker pool (re-fetched
    each attempt so a restarted pool is used), and on ``ServiceUnavailable``
    either raises :class:`ToxicTaskError` (this task caused the failure) or
    transparently retries (innocent task). Returns the task content on success.
    """
    # CRITICAL: the client-side timeout must EXCEED the watchdog's wedge
    # deadline + its poll interval.  A wedged task is resolved by the watchdog's
    # poison mechanism, which attributes toxicity (culprit -> the worker puts a
    # ``toxic=True`` response in our result_q).  If the client timed out FIRST,
    # it would leave rq.get() before that toxic response arrived, discard it, and
    # retry blindly — so a toxic task would be re-submitted on every restart and
    # wedge the server again forever (observed as: watchdog revives, worker
    # "started", then total silence for ~WEDGE_DEADLINE, repeat).
    #
    # The watchdog detects a wedge at the first poll where elapsed > deadline,
    # i.e. at most deadline + interval after task start, then poisons within ms.
    # Clamp so we are still in rq.get() when that happens.  The margin (60s) is
    # deliberately generous: this workload is latency-insensitive but
    # robustness-critical, so we give the watchdog ample headroom over its
    # worst-case detection latency (deadline + interval = 140s) to resolve every
    # wedge via poison/attribution rather than the client bailing early.
    effective_timeout = max(timeout, RESILIENT_RESPONSE_TIMEOUT)

    input_text = kwargs.get("text", "") if isinstance(kwargs, dict) else ""
    inherited_context = getattr(client, "current_observation_context", None)
    inherited_context = (
        inherited_context() if callable(inherited_context) else {})
    merged_context = {
        **dict(inherited_context or {}),
        **dict(context or {}),
    }
    correlation_id = request_id or new_correlation_id()
    attempt = 0
    while True:
        attempt += 1
        client.watchdog.server_ready.wait()
        pool = client.worker_pool
        if pool is None:
            raise RuntimeError("Worker pool not initialized")
        rq: queue.Queue = queue.Queue()
        pool.submit_task({
            "task_type": task_type,
            "result_q": rq,
            "kwargs": kwargs,
            "request_id": correlation_id,
            "context": merged_context,
        })
        try:
            resp = rq.get(timeout=effective_timeout)
        except queue.Empty:
            client.emit_execution_event(
                "resilient_wait_expired",
                request_id=correlation_id,
                task_type=task_type,
                outcome="infrastructure_error",
                details={"attempt": attempt},
            )
            # Only reachable if the watchdog FAILED to detect/restart within
            # effective_timeout (e.g. watchdog thread died).  Wait for any
            # in-flight restart to settle, then retry on the (possibly new) pool.
            while client.watchdog.server_ready.is_set():
                time.sleep(0.5)
            continue
        if resp.get("success", False):
            return resp.get("content")
        err = resp.get("error", "unknown error")
        if isinstance(err, ServiceUnavailable):
            if resp.get("toxic"):
                raise ToxicTaskError(
                    task_type,
                    str(resp.get("toxic_reason")
                        or "crashed/wedged the server"),
                    input_text)
            client.emit_execution_event(
                "resilient_retry",
                request_id=correlation_id,
                task_type=task_type,
                outcome="infrastructure_error",
                details={"attempt": attempt, "reason": "service_unavailable"},
            )
            continue  # innocent -> transparent retry on the (now-current) pool
        # Pipe/connection errors mean the server process died (^C, OOM, crash).
        # Wait for the watchdog to restart, then retry — same as ServiceUnavailable
        # for an innocent task.
        if _is_dead_server_error(err):
            client.emit_execution_event(
                "resilient_retry",
                request_id=correlation_id,
                task_type=task_type,
                outcome="infrastructure_error",
                details={"attempt": attempt, "reason": "dead_server"},
            )
            while client.watchdog.server_ready.is_set():
                time.sleep(0.5)
            continue
        if isinstance(err, RpcTimeoutError):
            raise err
        raise RuntimeError(str(err) or repr(err))


class LspClient:
    """Simple JSON-RPC/LSP client with Lean 4 RPC support."""

    def __init__(
        self,
        server_cmd: list,
        cwd: str = "",
        *,
        worker_memory_high_bytes: Optional[int] = None,
        worker_memory_max_bytes: Optional[int] = None,
        watchdog_memory_poll_interval: float = 1.0,
        event_sink: Optional[EventSink] = None,
    ):
        """Initialize the LSP client with a server command."""
        self.process: Optional[subprocess.Popen] = None
        self.server_cmd = server_cmd
        self.cwd = cwd
        self.worker_memory_high_bytes = worker_memory_high_bytes
        self.worker_memory_max_bytes = worker_memory_max_bytes
        self.watchdog_memory_poll_interval = watchdog_memory_poll_interval
        self.event_sink = event_sink
        self._observation_context: ContextVar[dict[str, Any]] = ContextVar(
            f"pyleaner_observation_context_{id(self)}", default={})
        self._environment_fingerprint_cache: dict[str, str | None] = {}
        self._environment_fingerprint_lock = threading.Lock()
        self.message_id = 0
        self._id_lock = threading.Lock()
        # Route responses by message id: {msg_id: response_queue}
        self._pending_requests: Dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        # Notifications are routed by method name
        self.notification_handlers: dict[str, Any] = {}
        # Worker pool for handling LSP and RPC requests
        self.worker_pool: Optional[WorkerPool] = None
        # Buffers for notifications that arrive before worker_pool is assigned
        self._init_notification_buffers: Dict[str, list] = {}
        # Register notification handlers
        self.notification_handlers["$/lean/fileProgress"] = (
            self._handle_file_progress
        )
        self.notification_handlers["textDocument/publishDiagnostics"] = (
            self._handle_publish_diagnostics
        )
        # Liveness watchdog: revives the server if its process dies. Started in
        # connect(), armed in create_pool(), stopped in exit().
        from .watchdog import Watchdog  # deferred to avoid import cycle
        self.watchdog: Watchdog = Watchdog(self)

    def emit_execution_event(self, kind: str, **fields: Any):
        """Emit one optional lifecycle event through the configured sink.

        Sinks may be called concurrently by worker, router, and watchdog
        threads and therefore should enqueue quickly and be thread-safe.
        Sink exceptions are isolated from Lean execution.
        """
        return emit_safely(self.event_sink, kind, **fields)

    def current_observation_context(self) -> dict[str, Any]:
        """Return a detached request-scoped correlation context."""
        return dict(self._observation_context.get())

    @contextmanager
    def observation_context(self, **fields: Any):
        """Attach caller-owned trace fields to nested Lean tasks.

        The context is local to the current Python execution context and is
        propagated into worker task envelopes. PyLeaner stores it as opaque
        metadata and does not interpret any domain-specific keys.
        """
        value = {
            **self.current_observation_context(),
            **{key: item for key, item in fields.items() if item is not None},
        }
        token = self._observation_context.set(value)
        try:
            yield self
        finally:
            self._observation_context.reset(token)

    def task_environment_fingerprint(self, kwargs: dict[str, Any]) -> str | None:
        """Return a cached static environment identity for one source task."""
        source = kwargs.get("text") if isinstance(kwargs, dict) else ""
        source = source if isinstance(source, str) else ""
        cache_key = fingerprint_text(source)
        with self._environment_fingerprint_lock:
            if cache_key in self._environment_fingerprint_cache:
                return self._environment_fingerprint_cache[cache_key]
        try:
            value = fingerprint_lean_environment(
                self.cwd or ".", self.server_cmd, source=source
            ).fingerprint
        except (OSError, ValueError):
            value = None
        with self._environment_fingerprint_lock:
            self._environment_fingerprint_cache[cache_key] = value
        return value

    # ── Process management ──────────────────────────────────

    def start(self) -> None:
        """Start the LSP server process."""
        debug_log(f"Starting LSP server: {' '.join(self.server_cmd)}")
        self.process = subprocess.Popen(
            self.server_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            cwd=self.cwd,
            start_new_session=True,  # detach from terminal process group
        )

        # Track root PID for cleanup on abnormal exit.
        try:
            with _CHILD_PIDS_LOCK:
                _CHILD_ROOT_PIDS.add(self.process.pid)
        except Exception:
            pass

        # Ensure child processes are reaped even on abnormal exit.
        if not hasattr(LspClient, "_cleanup_registered"):
            atexit.register(_cleanup_lean_processes)
            signal.signal(signal.SIGTERM, lambda *_: _cleanup_lean_processes())
            signal.signal(signal.SIGHUP, lambda *_: _cleanup_lean_processes())
            LspClient._cleanup_registered = True  # type: ignore[attr-defined]

        # Start reader threads
        threading.Thread(
            target=self._read_stdout, daemon=True, name="stdout_reader"
        ).start()
        threading.Thread(
            target=self._read_stderr, daemon=True, name="stderr_reader"
        ).start()
        debug_log("LSP server started")

    # ── Stream I/O ───────────────────────────────────────────

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        """Read exactly n bytes from stream."""
        buf = bytearray(n)
        mv = memoryview(buf)
        while n:
            k = stream.readinto(mv[-n:])
            if k == 0:
                raise EOFError("unexpected EOF")
            n -= k
        return bytes(buf)

    def _read_message(self, stream) -> Optional[str]:
        """Read a complete LSP message from stream."""
        # Read header
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            byte = stream.read(1)
            if not byte:
                raise EOFError("unexpected EOF while reading header")
            header += byte
            if len(header) > 8192:
                raise ValueError("header too long")

        # Parse Content-Length
        header_str = header.decode("utf-8", errors="replace")
        if "Content-Length: " not in header_str:
            raise ValueError(
                f"missing Content-Length in header: {header_str[:100]}"
            )

        try:
            length_str = header_str.split("Content-Length: ")[1].split("\r\n")[0]
            content_length = int(length_str)
        except (IndexError, ValueError) as e:
            raise ValueError(f"invalid Content-Length: {e}")

        # Read content
        content = self._read_exact(stream, content_length)
        return content.decode("utf-8", errors="replace")

    def _read_stdout(self) -> None:
        """Read messages from server stdout."""
        while self.process and self.process.poll() is None:
            try:
                if self.process.stdout is None:
                    break
                message = self._read_message(self.process.stdout)
                if message:
                    debug_log(
                        f"Received: "
                        f"{self._sanitize_for_terminal(message[:200])}"
                        f"{'...' if len(message) > 200 else ''}"
                    )
                    self._handle_message(message)
            except EOFError:
                debug_log("Server closed stdout")
                break
            except Exception as e:
                debug_log(f"Error reading stdout: {e}")
                break

    @staticmethod
    def _sanitize_for_terminal(text: str) -> str:
        """Replace terminal control characters (0x00–0x1F except \\t\\n\\r).

        A bare 0x03 (ETX) printed to stdout is interpreted by the terminal
        driver as Ctrl+C, sending SIGINT to the foreground process group.
        This function prevents binary garbage in Lean's output from
        accidentally interrupting the Python process.
        """
        out: list = []
        for ch in text:
            cp = ord(ch)
            if cp < 0x20 and ch not in ("\t", "\n", "\r"):
                out.append(f"\\x{cp:02x}")
            else:
                out.append(ch)
        return "".join(out)

    def _read_stderr(self) -> None:
        """Read stderr from server; flag universal fatals to the watchdog."""
        while self.process and self.process.poll() is None:
            try:
                if self.process.stderr is None:
                    break
                line = self.process.stderr.readline()
                if line:
                    line_str = self._sanitize_for_terminal(
                        line.decode("utf-8", errors="replace").rstrip())
                    print(f"SERVER STDERR: {line_str}", flush=True)
                    if FATAL_RE.search(line_str):
                        self.watchdog.flag_fatal(line_str)
            except Exception:
                break

    # ── Message handling ─────────────────────────────────────

    def _handle_message(self, message: str) -> None:
        """Handle incoming LSP message."""
        try:
            data = json.loads(message)
            if "id" in data and "method" not in data:
                # Response to client request (has id, no method)
                msg_id = data["id"]
                with self._pending_lock:
                    pending_q = self._pending_requests.get(msg_id)
                if pending_q is not None:
                    pending_q.put(data)
                else:
                    debug_log(f"Unexpected response id={msg_id}")
            elif "id" in data and "method" in data:
                # Server-to-client request (has both id and method)
                debug_log(f"Server request: {data['method']} id={data['id']}")
            elif "method" in data:
                # Notification (has method, no id, no result)
                method = data["method"]
                debug_log(f"Handling notification: {method}")
                if method in self.notification_handlers:
                    self.notification_handlers[method](data.get("params", {}))
        except json.JSONDecodeError as e:
            debug_log(f"JSON decode error: {e}")

    @staticmethod
    def _make_lsp_message(payload: dict) -> bytes:
        """Create LSP message with headers."""
        message = json.dumps(payload)
        return f"Content-Length: {len(message)}\r\n\r\n{message}".encode(
            "utf-8"
        )

    def _send_message(self, message: dict) -> None:
        """Send a message to the server."""
        debug_log(
            f"Sending: {message.get('method', message.get('id', 'response'))}"
        )
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Process or stdin is not available")
        self.process.stdin.write(self._make_lsp_message(message))
        self.process.stdin.flush()

    def _next_id(self) -> int:
        """Get next message ID (thread-safe)."""
        with self._id_lock:
            self.message_id += 1
            return self.message_id

    # ── LSP protocol ─────────────────────────────────────────

    def request(
        self, method: str, params: Any, msg_id: int, timeout: float = 60.0
    ) -> Any:
        """Send a request and wait for response."""
        response_q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending_requests[msg_id] = response_q

        message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        self._send_message(message)

        try:
            response = response_q.get(timeout=timeout)
            if isinstance(response, ServiceUnavailable):
                raise response
            if "error" in response:
                raise Exception(f"Request error: {response['error']}")
            debug_log(f"Got response for {method}")
            return response.get("result")
        except queue.Empty:
            raise TimeoutError(f"Timeout waiting for response to {method}")
        finally:
            with self._pending_lock:
                self._pending_requests.pop(msg_id, None)

    def notify(self, method: str, params: Any) -> None:
        """Send a notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send_message(message)

    def initialize(self, root_uri: str, timeout: float = 60.0) -> Any:
        """Initialize LSP connection."""
        debug_log(f"Initializing LSP with rootUri: {root_uri}")
        return self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
                "initializationOptions": {
                    "editDelay": 200,
                    "hasWidgets": True,
                },
            },
            0,
            timeout=timeout,
        )

    def initialized(self) -> None:
        """Send initialized notification."""
        self.notify("initialized", {})

    # ── Convenience methods ──────────────────────────────────

    def connect(self, timeout: float = 300.0) -> None:
        """Start the server, initialize LSP handshake, send initialized.

        Equivalent to calling start() + initialize() + initialized().

        Args:
            timeout: Max wait for initialize response (default 300s).
                     Mathlib projects may need >60s on first load.
        """
        self.start()
        self.initialize(f"file://{self.cwd or '.'}", timeout=timeout)
        self.initialized()
        self.watchdog.start()

    def create_pool(self, text: str, uri: str = "", size: int = 1) -> Optional[WorkerPool]:
        """Create a worker pool and load the given text into all workers.

        Args:
            text: Lean source content to load.
            uri: Base URI for worker files. Defaults to ``cwd/workers/``.
            size: Number of workers (default 1).
        """
        if not uri:
            uri = f"file://{self.cwd or '.'}/workers/"
        self.initialize_worker_pool(size=size, init_uri=uri, init_text=text)
        self.watchdog.arm(text, size)
        self.watchdog.attach_worker_guards()
        return self.worker_pool

    def submit_resilient(
        self,
        task_type: str,
        kwargs: dict,
        timeout: float = 120.0,
        *,
        request_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ):
        """Submit a task with transparent crash/wedge recovery.

        Waits for the server to be ready, submits to the current worker pool, and
        on ``ServiceUnavailable`` either raises :class:`ToxicTaskError` (this task
        caused the failure) or transparently retries (innocent). See
        :func:`pyleaner.client._submit_resilient`.
        """
        return _submit_resilient(
            self,
            task_type,
            kwargs,
            timeout,
            request_id=request_id,
            context=context,
        )

    def shutdown(self) -> Any:
        """Shutdown LSP connection."""
        debug_log("Shutting down LSP connection")
        return self.request("shutdown", {}, 0)

    def exit(self) -> None:
        """Send exit notification and terminate server (full process tree)."""
        self.watchdog.stop()
        self.notify("exit", {})
        if self.process:
            # Remove from global registry before killing (clean shutdown).
            try:
                with _CHILD_PIDS_LOCK:
                    _CHILD_ROOT_PIDS.discard(self.process.pid)
            except Exception:
                pass
            from .watchdog import _kill_process_tree
            _kill_process_tree(self.process)
            debug_log("LSP server terminated")

    # ── Worker pool ──────────────────────────────────────────

    def initialize_worker_pool(
        self, size: int = 1, init_uri: str = "", init_text: str = ""
    ) -> None:
        """Start the worker pool with initialized Lean environments.

        Args:
            size: Number of worker environments to create
            init_uri: Initial file URI for environment initialization
            init_text: Initial file content
        """
        # Deferred import to avoid circular dependency at module level
        from .pool import WorkerPool as _WorkerPool

        # Register URIs BEFORE creating workers so notifications arriving
        # during _didopen are buffered instead of being dropped.
        for i in range(1, size + 1):
            self._register_init_uri(f"{init_uri}worker_{i}.lean")

        self.worker_pool = _WorkerPool(self, size, init_uri, init_text)

        # Now that worker_pool is assigned, notifications will route correctly.
        # Initialize each worker's Lean environment (calls _didopen).
        self.worker_pool.initialize_all_workers()

        # Start router thread
        threading.Thread(
            target=self.worker_pool.router, daemon=True, name="router"
        ).start()

    # ── Notification handlers ────────────────────────────────

    # ── Notification buffer helpers (for worker init race) ───

    def _register_init_uri(self, uri: str) -> None:
        """Register a URI to buffer notifications before worker_pool is ready."""
        self._init_notification_buffers[uri] = []

    def _drain_init_buffer(self, uri: str) -> list:
        """Retrieve and clear buffered notifications for a URI."""
        return self._init_notification_buffers.pop(uri, [])

    def _route_or_buffer(self, uri: str, msg: dict) -> None:
        """Route notification to worker pool, or buffer if not yet ready."""
        if self.worker_pool is not None:
            try:
                worker = self.worker_pool.get_worker_for_uri(uri)
                worker.notification_queue.put(msg)
                return
            except RuntimeError:
                pass
        # Buffer if worker_pool not ready yet
        if uri in self._init_notification_buffers:
            self._init_notification_buffers[uri].append(msg)

    def _handle_file_progress(self, params: dict) -> None:
        """Handle $/lean/fileProgress notification."""
        uri = params.get("textDocument", {}).get("uri", "")
        if uri:
            self._route_or_buffer(uri, {
                "method": "$/lean/fileProgress", "params": params
            })

    def _handle_publish_diagnostics(self, params: dict) -> None:
        """Handle textDocument/publishDiagnostics notification."""
        uri = params.get("uri", "")
        if uri:
            self._route_or_buffer(uri, {
                "method": "textDocument/publishDiagnostics", "params": params
            })
