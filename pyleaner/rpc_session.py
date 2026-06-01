"""RPC session management for Lean 4 LSP server."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Optional, Dict, Set, TYPE_CHECKING

from . import debug_log

if TYPE_CHECKING:
    from .client import LspClient


class KeepAliveManager:
    """Manages keep-alive for all RPC sessions."""

    KEEP_ALIVE_INTERVAL = 10  # seconds

    def __init__(self):
        self._sessions: Set[RpcSession] = set()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def register(self, session: RpcSession) -> None:
        """Register an RPC session for keep-alive."""
        with self._lock:
            self._sessions.add(session)
            debug_log(f"Registered RPC session for keep-alive: {session.uri}, "
                      f"total: {len(self._sessions)}")

    def unregister(self, session: RpcSession) -> None:
        """Unregister an RPC session from keep-alive."""
        with self._lock:
            self._sessions.discard(session)
            debug_log(f"Unregistered RPC session: {session.uri}, "
                      f"total: {len(self._sessions)}")

    def start(self) -> None:
        """Start the keep-alive thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="keep-alive"
        )
        self._thread.start()
        debug_log("Keep-alive manager started")

    def stop(self) -> None:
        """Stop the keep-alive thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        debug_log("Keep-alive manager stopped")

    def _run_loop(self) -> None:
        """Keep-alive thread main loop."""
        while self._running:
            with self._lock:
                sessions = list(self._sessions)

            for session in sessions:
                try:
                    success = session._send_keep_alive()
                    if not success:
                        self.unregister(session)
                except Exception as e:
                    debug_log(f"Keep-alive failed for {session.uri}: {e}")
                    self.unregister(session)

            time.sleep(self.KEEP_ALIVE_INTERVAL)


class RpcSession:
    """Manages a Lean 4 RPC session."""

    KEEP_ALIVE_INTERVAL = 20  # seconds

    def __init__(
        self,
        worker_id: int,
        uri: str,
        client: LspClient,
        keep_alive_manager: Optional[KeepAliveManager] = None,
    ):
        self.worker_id = worker_id
        self.uri = uri
        self.client = client
        self.session_id: Optional[int] = None
        self.last_activity = time.time()
        self._connected = False
        self._lock = threading.Lock()
        self._keep_alive_manager = keep_alive_manager

    def connect(self, timeout: float = 60.0) -> int:
        """Create a new RPC session and return the session ID."""
        debug_log(f"Creating RPC session for: {self.uri}")
        result = self.client.request(
            "$/lean/rpc/connect", {"uri": self.uri}, self.worker_id, timeout=timeout
        )
        session_id = result.get("sessionId")
        if session_id:
            if isinstance(session_id, str):
                return int(session_id)
            return session_id
        raise Exception(f"Failed to connect RPC: {result}")

    def ensure_connected(self):
        """Ensure RPC session is connected (only connects once)."""
        with self._lock:
            if not self._connected:
                debug_log(f"Connecting RPC session for {self.uri}")
                self.session_id = self.connect()
                self._connected = True
                # Register with keep-alive manager after connection
                if self._keep_alive_manager:
                    self._keep_alive_manager.register(self)

    def _send_keep_alive(self) -> bool:
        """Send a keep-alive RPC call to maintain the session.

        Returns:
            True if keep-alive succeeded, False if session is disconnected.
        """
        if not self._connected or self.session_id is None:
            return False
        try:
            msg_id = self.client._next_id()
            response_q = queue.Queue()
            self.client._pending_requests[msg_id] = response_q

            message = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "$/lean/rpc/call",
                "params": {
                    "textDocument": {"uri": self.uri},
                    "position": {"line": 0, "character": 0},
                    "sessionId": str(self.session_id),
                    "method": "LeanLspExtension.ping",
                    "params": {},
                },
            }
            self.client._send_message(message)

            # Wait for response with short timeout
            try:
                response = response_q.get(timeout=5.0)
                if "error" in response:
                    error = response["error"]
                    debug_log(f"Keep-alive error for {self.uri}: {error}")
                    self._connected = False
                    return False
                debug_log(f"Keep-alive OK for {self.uri}")
                self.last_activity = time.time()
                return True
            except queue.Empty:
                debug_log(f"Keep-alive timeout for {self.uri}")
                self._connected = False
                return False
            finally:
                self.client._pending_requests.pop(msg_id, None)
        except Exception as e:
            debug_log(f"Keep-alive error for {self.uri}: {e}")
            self._connected = False
            return False

    def call(
        self,
        method: str,
        params: dict,
        position: Optional[Dict[str, int]] = None,
    ) -> Any:
        """Make an RPC call, handling connection and keep-alive if needed."""
        self.ensure_connected()

        if position is None:
            raise ValueError("position is required for RPC calls")

        # After ensure_connected, session_id should always be set
        if self.session_id is None:
            raise RuntimeError("Session ID not set after connection")

        msg_id = self.client._next_id()
        response_q = queue.Queue()
        self.client._pending_requests[msg_id] = response_q

        message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "$/lean/rpc/call",
            "params": {
                "textDocument": {"uri": self.uri},
                "position": position,
                "sessionId": str(self.session_id),
                "method": method,
                "params": params,
            },
        }
        self.client._send_message(message)
        debug_log(f"RPC call: id={msg_id}, method={method}")

        try:
            response = response_q.get(timeout=60.0)
            if "error" in response:
                error = response["error"]
                raise Exception(f"RPC error: {error}")
            result = response.get("result")
            # Parse JSON result if it's a string
            if isinstance(result, str):
                return json.loads(result)
            return result
        except queue.Empty:
            raise TimeoutError(f"Timeout waiting for RPC response to {method}")
        finally:
            self.client._pending_requests.pop(msg_id, None)
            self.last_activity = time.time()
