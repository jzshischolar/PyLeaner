"""LSP client for communicating with the Lean 4 language server."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Optional, Dict, TYPE_CHECKING

from . import debug_log

if TYPE_CHECKING:
    from .pool import WorkerPool


class LspClient:
    """Simple JSON-RPC/LSP client with Lean 4 RPC support."""

    def __init__(self, server_cmd: list, cwd: str = ""):
        """Initialize the LSP client with a server command."""
        self.process: Optional[subprocess.Popen] = None
        self.server_cmd = server_cmd
        self.cwd = cwd
        self.message_id = 0
        # Route responses by message id: {msg_id: response_queue}
        self._pending_requests: Dict[int, queue.Queue] = {}
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
        )

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
                        f"Received: {message[:200]}"
                        f"{'...' if len(message) > 200 else ''}"
                    )
                    self._handle_message(message)
            except EOFError:
                debug_log("Server closed stdout")
                break
            except Exception as e:
                debug_log(f"Error reading stdout: {e}")
                break

    def _read_stderr(self) -> None:
        """Read stderr from server (for debugging)."""
        while self.process and self.process.poll() is None:
            try:
                if self.process.stderr is None:
                    break
                line = self.process.stderr.readline()
                if line:
                    line_str = line.decode("utf-8", errors="replace").rstrip()
                    print(f"SERVER STDERR: {line_str}", flush=True)
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
                if msg_id in self._pending_requests:
                    self._pending_requests[msg_id].put(data)
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
        """Get next message ID."""
        self.message_id += 1
        return self.message_id

    # ── LSP protocol ─────────────────────────────────────────

    def request(
        self, method: str, params: Any, msg_id: int, timeout: float = 60.0
    ) -> Any:
        """Send a request and wait for response."""
        response_q: queue.Queue = queue.Queue()
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
            if "error" in response:
                raise Exception(f"Request error: {response['error']}")
            debug_log(f"Got response for {method}")
            return response.get("result")
        except queue.Empty:
            raise TimeoutError(f"Timeout waiting for response to {method}")
        finally:
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
        return self.worker_pool

    def shutdown(self) -> Any:
        """Shutdown LSP connection."""
        debug_log("Shutting down LSP connection")
        return self.request("shutdown", {}, 0)

    def exit(self) -> None:
        """Send exit notification and terminate server."""
        self.notify("exit", {})
        if self.process:
            self.process.terminate()
            time.sleep(0.5)
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
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
