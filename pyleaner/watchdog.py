"""General-purpose liveness + wedge watchdog for the Lean LSP server.

A background daemon that revives the server if its process dies OR gets wedged.
Every ``interval`` seconds it checks three signals, in priority order:

  1. **fatal stderr** -- ``_read_stderr`` flagged a universal fatal prefix
     (``INTERNAL PANIC`` / ``Stack overflow detected`` / OOM). Instant, zero
     false-positive. Caught wedges that print a fatal.
  2. **deadline** -- a worker's current task has run longer than
     ``WEDGE_DEADLINE``. Catches silent divergent wedges (no stderr). Per-worker,
     so the culprit is identified precisely.
  3. **death** -- ``process.poll()`` reports the process exited. Catches hard
     crashes (#13987 JSON panic, OOM, segfault).

On any signal it clears a ``server_ready`` Event, marks the culprit task(s)
``_culprit`` (deadline -> precise; fatal/death -> 通杀 all in-flight), poisons
every worker so its blocked ``_didchange`` aborts, hard-restarts the server+pool,
then sets ``server_ready``. ``LspClient.submit_resilient`` waits on
``server_ready`` and transparently retries innocent tasks; a culprit task is
raised as :class:`pyleaner.errors.ToxicTaskError`.

Restart is watchdog-internal and single-threaded (only ``_run`` calls it).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Optional

from . import debug_log

if TYPE_CHECKING:
    from .client import LspClient


# ── tunables ────────────────────────────────────────────────

# Universal Lean fatal prefixes (NOT specific to Nat.pow). Any line matching
# means the server declared an unrecoverable condition -> wedge.
FATAL_RE = re.compile(
    r"INTERNAL PANIC|Stack overflow detected|out of memory|memory allocation of"
)

# A task running longer than this (seconds) with no result is treated as wedged.
# Must exceed the longest legitimate elaboration in your workload.
WEDGE_DEADLINE = 120.0

# Sentinel "method" injected into a worker's notification_queue to abort its
# blocked _didchange/_didopen read with ServiceUnavailable.
POISON_METHOD = "$/internal/service-unavailable"


# ── process-tree killing (OS-level, reusable) ───────────────


def _collect_descendants(root_pid: int) -> list:
    """All descendant PIDs of ``root_pid`` via repeated ``pgrep -P``.

    Must be called WHILE the root is still alive: once it dies, its children are
    reparented to init and ``pgrep -P <root>`` no longer lists them.
    """
    descendants: list = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        parent = frontier.pop()
        try:
            out = subprocess.run(
                ["pgrep", "-P", str(parent)],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            out = ""
        for line in out.split():
            try:
                child = int(line)
            except ValueError:
                continue
            if child in seen:          # cycle / repeat guard
                continue
            seen.add(child)
            descendants.append(child)
            frontier.append(child)
    return descendants


def _kill_process_tree(proc, known_pids: Optional[set] = None) -> None:
    """SIGKILL ``proc`` AND its descendants.

    ``lake serve`` forks ``lean --server`` as a child, and that child is the
    real CPU hog when an elaboration diverges. Killing only the lake PID reaps
    lake but orphans lean (reparented to init, keeps spinning). So we snapshot
    descendants while the root is alive, SIGKILL them first, then the root.
    SIGKILL because a wedged/divergent process ignores a polite SIGTERM.

    If the root process is already dead (crash) children are reparented to
    init and ``pgrep -P <root>`` can't find them. Pass ``known_pids`` from a
    snapshot taken while the root was still alive as a fallback kill list.
    """
    try:
        root_pid = proc.pid
    except Exception:
        return

    # Primary: collect descendants while root is alive.
    to_kill: set = set()
    try:
        to_kill.update(_collect_descendants(root_pid))
    except Exception:
        pass

    # Fallback: if root is already dead AND primary returned nothing, use the
    # pre-snapshot list (taken before root died) to catch orphaned children.
    if not to_kill and known_pids:
        to_kill.update(known_pids)
        to_kill.discard(root_pid)  # root is already dead, don't bother

    # If STILL nothing and root is dead, do a last-resort scan for orphaned
    # lean processes that match the patterns we know (lean --server / --worker).
    if not to_kill and proc.poll() is not None:
        to_kill = _find_orphaned_lean()

    # Kill children first, then root.
    for child in sorted(to_kill):
        try:
            os.kill(child, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _find_orphaned_lean() -> set:
    """Last-resort scan: find orphaned lean --server / --worker processes.

    Only used when the root process is already dead and we have no PID list.
    Scans /proc for lean processes whose ppid is 1 (reparented to init).
    """
    orphans: set = set()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                pid = int(entry)
            except ValueError:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            # Match ``lean --server`` or ``lean --worker`` (but not lake, not
            # claude, not some random tool named "clean").
            if ("lean" in os.path.basename(cmdline.split("\0")[0]).lower()
                    and ("--server" in cmdline or "--worker" in cmdline)):
                try:
                    with open(f"/proc/{pid}/stat", "r") as f:
                        stat = f.read()
                    ppid = int(stat.split(" ")[3])
                except (OSError, ValueError, IndexError):
                    continue
                if ppid == 1:  # reparented to init -> orphan
                    orphans.add(pid)
    except Exception:
        pass
    return orphans


# ── watchdog ────────────────────────────────────────────────


class Watchdog:
    """Revive the Lean server on crash or wedge, with toxic-task attribution."""

    def __init__(
        self,
        client: "LspClient",
        interval: float = 20.0,
    ):
        """
        Args:
            client: The ``LspClient`` whose server to monitor. Mutated in place
                on respawn (same object all callers hold).
            interval: Seconds between checks.
        """
        self.client = client
        self.interval = interval
        # Pool config captured from create_pool(), used to rebuild on respawn.
        self._armed: bool = False
        self._base_text: str = ""
        self._size: int = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Cleared while a restart is in progress; submit_resilient waits on it.
        self.server_ready = threading.Event()
        self.server_ready.set()
        # Fatal-stderr channel: _read_stderr sets, _run consumes.
        self._fatal_reason: Optional[str] = None
        self._fatal_lock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────

    def arm(self, base_text: str, size: int = 1) -> None:
        """Capture the pool config so a respawn can rebuild it faithfully."""
        with self._lock:
            self._base_text = base_text
            self._size = size
            self._armed = True

    def start(self) -> None:
        """Start the watchdog thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="lean-watchdog"
        )
        self._thread.start()
        debug_log("Lean server watchdog started")

    def stop(self) -> None:
        """Stop the watchdog thread (e.g. during intentional shutdown)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None
        debug_log("Lean server watchdog stopped")

    # ── fatal-stderr channel ────────────────────────────────

    def flag_fatal(self, reason: str) -> None:
        """Called by ``LspClient._read_stderr`` when the server prints a fatal."""
        with self._fatal_lock:
            if self._fatal_reason is None:
                self._fatal_reason = reason

    def take_fatal(self) -> Optional[str]:
        with self._fatal_lock:
            r = self._fatal_reason
            self._fatal_reason = None
            return r

    # ── detection helpers ───────────────────────────────────

    def _is_alive(self) -> bool:
        proc = self.client.process
        return proc is not None and proc.poll() is None

    def _worker_over_deadline(self, w) -> bool:
        ts = getattr(w, "task_started_at", None)
        return ts is not None and (time.monotonic() - ts) > WEDGE_DEADLINE

    def _inflight_tasks(self) -> list:
        """Return [(worker, current_task)] for every worker with an in-flight task."""
        pool = getattr(self.client, "worker_pool", None)
        out = []
        if pool is not None:
            for w in getattr(pool, "workers", []):
                t = getattr(w, "current_task", None)
                if t is not None:
                    out.append((w, t))
        return out

    def _attribute_toxic(self, trigger: str, inflight_tasks: list,
                         over_deadline_workers: list) -> list:
        """Which in-flight tasks to mark toxic.

        ``deadline`` -> precise: only the over-deadline workers' tasks.
        ``fatal`` / ``death`` -> 通杀: all in-flight (can't attribute to a worker).
        """
        if trigger == "deadline":
            bad_ids = set()
            for w in over_deadline_workers:
                t = getattr(w, "current_task", None)
                if t is not None:
                    bad_ids.add(id(t))
            return [t for t in inflight_tasks if id(t) in bad_ids]
        return list(inflight_tasks)

    # ── teardown + restart ──────────────────────────────────

    def _teardown_pool_state(self) -> None:
        """Stop the OLD pool's background threads before a new server exists.

        KeepAliveManager writes ``$/lean/rpc/keepAlive`` to
        ``client.process.stdin`` every 10s; if not stopped before reconnect, its
        stale keep-alive corrupts the new server's ``initialize`` handshake.
        (Poisoning the workers is done separately in ``_restart``; this only
        stops the keep-alive manager and drops pending requests.)
        """
        pool = getattr(self.client, "worker_pool", None)
        if pool is not None:
            kam = getattr(pool, "keep_alive_manager", None)
            if kam is not None:
                try:
                    kam.stop()
                except Exception:
                    pass
            for w in getattr(pool, "workers", []):
                try:
                    w.rpc_session.invalidate()
                except Exception:
                    pass
        try:
            with self.client._pending_lock:
                self.client._pending_requests.clear()
        except Exception:
            pass

    def _restart(self, trigger: str, reason: str) -> None:
        """The SOLE revival path (watchdog-internal, single-threaded).

        Order: clear server_ready -> attribute+mark toxic -> poison all workers
        (wake blocked _didchange) -> teardown keep-alive -> kill tree -> connect
        -> rebuild pool -> set server_ready.
        """
        self.server_ready.clear()
        pairs = self._inflight_tasks()
        inflight_tasks = [t for (_, t) in pairs]
        over = [w for (w, _) in pairs] if trigger == "deadline" else []
        toxic = self._attribute_toxic(trigger, inflight_tasks, over)
        for t in toxic:
            t["_culprit"] = True
        # Poison every worker so its blocked _didchange/_didopen aborts now and
        # fails its task (toxic flag) + drains its queue (innocent).
        for (w, _) in pairs:
            try:
                w.notification_queue.put_nowait({"method": POISON_METHOD})
            except Exception:
                pass
        # Snapshot the full process tree BEFORE killing anything so we can
        # reach orphaned children even if the root dies mid-operation.
        known_pids: set = set()
        try:
            if self.client.process is not None and self.client.process.pid is not None:
                root_pid = self.client.process.pid
                known_pids.add(root_pid)
                known_pids.update(_collect_descendants(root_pid))
        except Exception:
            pass
        print(f"watchdog: {trigger} ({reason}) — reviving; "
              f"{len(toxic)} task(s) marked toxic", flush=True)
        self._teardown_pool_state()
        _kill_process_tree(self.client.process, known_pids=known_pids)
        self.client.connect(timeout=300)
        if self._armed:
            self.client.create_pool(text=self._base_text, size=self._size)
        self.server_ready.set()

    # ── main loop ───────────────────────────────────────────

    def _run(self) -> None:
        # _stop.wait(interval) blocks `interval` then returns False (or True if
        # stop was set), giving the poll cadence and a clean exit.
        while not self._stop.wait(self.interval):
            try:
                fatal = self.take_fatal()
                if fatal is not None:
                    self._restart("fatal", fatal)
                    continue
                pool = getattr(self.client, "worker_pool", None)
                if pool is not None:
                    over = [w for w in getattr(pool, "workers", [])
                            if self._worker_over_deadline(w)]
                    if over:
                        self._restart("deadline",
                                      f"task exceeded {WEDGE_DEADLINE}s")
                        continue
                if not self._is_alive():
                    # Capture exit code for post-mortem diagnosis.
                    code = self.client.process.poll() if self.client.process else None
                    detail = f"process exited (rc={code})" if code is not None else "process exited"
                    self._restart("death", detail)
                    continue
            except Exception as e:
                # A failed restart must not kill the watchdog thread; retry next
                # poll. server_ready stays cleared meanwhile (callers wait).
                print(f"watchdog: restart attempt failed: {e}; "
                      f"will retry next poll", flush=True)
