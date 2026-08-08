"""RPC session management for Lean 4 LSP server."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Optional, Dict, Set, TYPE_CHECKING

from . import debug_log
from .errors import ServiceUnavailable
from .watchdog import RPC_RESPONSE_TIMEOUT

if TYPE_CHECKING:
    from .client import LspClient


# ── RPC Error Types ───────────────────────────────────────────


class RpcError(Exception):
    """Base exception for RPC errors."""

    def __init__(self, error: dict):
        self.error = error
        super().__init__(f"RPC error: {error}")


class RpcNeedsReconnectError(RpcError):
    """Session has expired or been destroyed by the server (-32900)."""

    pass


class WorkerRestartedError(RpcError):
    """Lean file worker exited (-32901) or crashed (-32902)."""

    pass


class RpcContentModifiedError(RpcError):
    """File changed while the request was being processed (-32801)."""

    pass


class RpcRequestCancelledError(RpcError):
    """Request was cancelled (-32800)."""

    pass


class RpcTimeoutError(TimeoutError):
    """A Lean RPC response did not arrive within its configured deadline."""

    def __init__(self, method: str, timeout: float):
        self.method = method
        self.timeout = timeout
        super().__init__(
            f"Timeout waiting {timeout:g}s for RPC response to {method}"
        )


def classify_rpc_error(error: dict) -> Exception:
    """Map a JSON-RPC error object to the appropriate exception class."""
    code = error.get("code")
    message = error.get("message", "")

    # Lean-specific: session expired / outdated
    if code == -32900 or "Outdated RPC session" in message:
        return RpcNeedsReconnectError(error)

    # Lean-specific: worker exited / crashed
    if code in (-32901, -32902):
        return WorkerRestartedError(error)

    # LSP-standard: file content modified during request
    if code == -32801:
        return RpcContentModifiedError(error)

    # LSP-standard: request cancelled
    if code == -32800:
        return RpcRequestCancelledError(error)

    return RpcError(error)


# ── Keep-Alive Manager ────────────────────────────────────────


class KeepAliveManager:
    """Manages keep-alive notifications for all RPC sessions."""

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
        """Keep-alive thread main loop.

        Sends ``$/lean/rpc/keepAlive`` notifications to every registered
        session.  Session expiry is *not* detected here — it is discovered
        on the next ``$/lean/rpc/call`` that returns ``RpcNeedsReconnect``.
        """
        while self._running:
            with self._lock:
                sessions = list(self._sessions)

            for session in sessions:
                try:
                    success = session._send_keep_alive()
                    if not success:
                        session.invalidate()
                        self.unregister(session)
                except Exception as e:
                    debug_log(f"Keep-alive failed for {session.uri}: {e}")
                    session.invalidate()
                    self.unregister(session)

            time.sleep(self.KEEP_ALIVE_INTERVAL)


# ── RPC Session ───────────────────────────────────────────────


class RpcSession:
    """Manages a Lean 4 RPC session."""

    KEEP_ALIVE_INTERVAL = 20  # seconds (unused — manager drives interval)

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

    # ── Session lifecycle ──────────────────────────────────────

    def connect(self, timeout: float = 60.0) -> int:
        """Create a new RPC session and return the session ID."""
        debug_log(f"Creating RPC session for: {self.uri}")
        result = self.client.request(
            "$/lean/rpc/connect",
            {"uri": self.uri},
            self.client._next_id(),
            timeout=timeout,
        )
        session_id = result.get("sessionId")
        if session_id is not None:
            if isinstance(session_id, str):
                return int(session_id)
            return session_id
        raise RuntimeError(f"Failed to connect RPC: {result}")

    def ensure_connected(self) -> None:
        """Ensure RPC session is connected (only connects once)."""
        with self._lock:
            if self._connected and self.session_id is not None:
                return

            debug_log(f"Connecting RPC session for {self.uri}")
            self.session_id = self.connect()
            self._connected = True

            if self._keep_alive_manager:
                self._keep_alive_manager.register(self)

    def invalidate(self) -> None:
        """Mark this RPC session as invalid.

        Old RpcRefs belonging to this session must be discarded by
        higher-level code.
        """
        with self._lock:
            old_session_id = self.session_id
            self.session_id = None
            self._connected = False

        debug_log(f"Invalidated RPC session: uri={self.uri}, "
                  f"oldSessionId={old_session_id}")

    def reconnect(self, timeout: float = 60.0) -> int:
        """Force reconnect and return the new session ID."""
        self.invalidate()

        debug_log(f"Reconnecting RPC session for {self.uri}")
        self.session_id = self.connect(timeout=timeout)
        self._connected = True

        if self._keep_alive_manager:
            self._keep_alive_manager.register(self)

        return self.session_id

    # ── Keep-alive ─────────────────────────────────────────────

    def _send_keep_alive(self) -> bool:
        """Send Lean RPC keepAlive notification.

        This is a *notification* (no ``id``) so no response is expected.
        The server silently updates the session expiry time.

        Returns:
            True if the notification was sent successfully.
            False if this session is currently disconnected or writing failed.
        """
        with self._lock:
            if not self._connected or self.session_id is None:
                return False

            session_id = self.session_id

        try:
            message = {
                "jsonrpc": "2.0",
                "method": "$/lean/rpc/keepAlive",
                "params": {
                    "uri": self.uri,
                    "sessionId": str(session_id),
                },
            }
            self.client._send_message(message)
            self.last_activity = time.time()
            debug_log(f"RPC keepAlive sent: uri={self.uri}, "
                      f"sessionId={session_id}")
            return True

        except Exception as e:
            debug_log(f"RPC keepAlive write failed for {self.uri}: {e}")
            self.invalidate()
            return False

    # ── RPC calls ──────────────────────────────────────────────

    def _rpc_call_once(
        self,
        method: str,
        params: dict,
        position: Dict[str, int],
        timeout: float = 60.0,
    ) -> Any:
        """Send one RPC call using the current session_id.

        Does **not** handle reconnect — callers should use :meth:`call`
        for automatic error recovery.
        """
        if self.session_id is None:
            raise RuntimeError("Session ID is not set")

        msg_id = self.client._next_id()
        response_q = queue.Queue()
        with self.client._pending_lock:
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

        try:
            self.client._send_message(message)
            debug_log(f"RPC call sent: id={msg_id}, method={method}, "
                      f"sessionId={self.session_id}")

            response = response_q.get(timeout=timeout)

            if isinstance(response, ServiceUnavailable):
                raise response

            if "error" in response:
                raise classify_rpc_error(response["error"])

            result = response.get("result")

            # Lean RPC result may be a serialized JSON string in some handlers.
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return result

            return result

        except queue.Empty:
            raise RpcTimeoutError(method, timeout)

        finally:
            with self.client._pending_lock:
                self.client._pending_requests.pop(msg_id, None)
            self.last_activity = time.time()

    def call(
        self,
        method: str,
        params: dict,
        position: Optional[Dict[str, int]] = None,
        timeout: float = RPC_RESPONSE_TIMEOUT,
        retry_on_reconnect: bool = True,
    ) -> Any:
        """Make an RPC call with automatic session recovery.

        Handles:
        - expired / outdated RPC session  (``RpcNeedsReconnect``)
        - worker restart / crash
        - optional one-time reconnect retry

        Args:
            method: Lean RPC method name (e.g. ``"LeanLspExtension.ping"``).
            params: Method-specific parameters dict.
            position: LSP position ``{"line": int, "character": int}``.
            timeout: Seconds to wait for each individual response.
            retry_on_reconnect: If *True*, automatically reconnect and
                retry **once** on session-expiry errors.  Set to *False*
                when *params* contains ``RpcRef`` objects from the old
                session (they become invalid after reconnect).
        """
        if position is None:
            raise ValueError("position is required for RPC calls")

        self.ensure_connected()

        try:
            return self._rpc_call_once(method, params, position,
                                       timeout=timeout)

        except RpcNeedsReconnectError as e:
            debug_log(f"RPC session needs reconnect for {self.uri}: "
                      f"{e.error}")
            self.invalidate()

            if not retry_on_reconnect:
                raise

            self.reconnect(timeout=timeout)
            return self._rpc_call_once(method, params, position,
                                       timeout=timeout)

        except WorkerRestartedError as e:
            debug_log(f"Lean file worker restarted/crashed for "
                      f"{self.uri}: {e.error}")
            self.invalidate()

            if not retry_on_reconnect:
                raise

            self.reconnect(timeout=timeout)
            return self._rpc_call_once(method, params, position,
                                       timeout=timeout)

        except RpcContentModifiedError:
            # Not a session-lifecycle error — the file changed mid-request.
            raise

        except RpcRequestCancelledError:
            # Also not a reconnect case.
            raise
