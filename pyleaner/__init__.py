"""PyLeaner — Python interface to Lean 4.

Provides structured declaration extraction, tactic execution, and proof
interaction via the Lean 4 LSP server with RPC extensions.

Usage::

    from pyleaner import LspClient, Task

    client = LspClient(server_cmd=["lake", "serve"], cwd="/path/to/project")
    client.start()
    client.initialize("file:///path/to/project")
    client.initialized()
"""

from typing import Any, Dict, TypedDict

__version__ = "0.1.0"

import queue  # noqa: E402


# ── Shared types ─────────────────────────────────────────────

class Task(TypedDict):
    """A task to be executed by a worker thread."""
    task_type: str
    result_q: "queue.Queue"  # type: ignore[name-defined]
    kwargs: Dict[str, Any]


# ── Debug utilities ──────────────────────────────────────────

DEBUG = False


def debug_log(msg: str) -> None:
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        print(f"[DEBUG] {msg}", flush=True)


# ── Public API ───────────────────────────────────────────────

from .client import LspClient  # noqa: E402, F401
from .pool import WorkerPool  # noqa: E402, F401
from .rpc_session import (  # noqa: E402, F401
    KeepAliveManager,
    RpcSession,
    RpcError,
    RpcNeedsReconnectError,
    WorkerRestartedError,
    RpcContentModifiedError,
    RpcRequestCancelledError,
)
from .worker import Worker  # noqa: E402, F401
from .watchdog import Watchdog  # noqa: E402, F401
from .errors import ServiceUnavailable, ToxicTaskError  # noqa: E402, F401

__all__ = [
    "__version__",
    "Task",
    "DEBUG",
    "debug_log",
    "LspClient",
    "WorkerPool",
    "Worker",
    "Watchdog",
    "ServiceUnavailable",
    "ToxicTaskError",
    "KeepAliveManager",
    "RpcSession",
    "RpcError",
    "RpcNeedsReconnectError",
    "WorkerRestartedError",
    "RpcContentModifiedError",
    "RpcRequestCancelledError",
]
