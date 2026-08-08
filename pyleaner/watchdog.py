"""Process-backed liveness and memory watchdog for the Lean LSP server.

Observation runs in a dedicated process, so a memory-hungry Lean elaboration
cannot starve the watchdog through the Python GIL. Every discoverable Lean
``--worker`` is monitored for unexpected exit. When memory limits are enabled,
workers are additionally adopted by transient systemd user scopes with
``MemoryMax``. The process-level soft RSS limit normally triggers an orderly
online restart; the cgroup hard limit remains effective even when the monitor
itself is not scheduled.

The small event-receiver thread in the owning process performs the actual
``LspClient`` mutation.  Only the worker(s) identified by the monitor are marked
toxic.  Their callers receive :class:`pyleaner.errors.ToxicTaskError`; innocent
in-flight calls transparently retry on the rebuilt pool.
"""

from __future__ import annotations

import os
import ctypes
import multiprocessing
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

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
WATCHDOG_POLL_INTERVAL = 20.0
MEMORY_POLL_INTERVAL = 1.0
# Lean's language server may replace the OS process backing a logical
# ``worker_N.lean`` document during normal cancellation/re-elaboration.  Give
# the replacement a short window to appear before treating the vanished PID as
# a crash.  The logical worker URI, not the transient OS PID, is the identity.
WORKER_REPLACEMENT_GRACE = 3.0

# RPC waits must outlive the watchdog's worst-case wedge detection
# (deadline + one poll), otherwise a local queue timeout escapes before the
# watchdog can attribute and recover the task.  The resilient caller waits
# longer still, so it remains available to receive the poison/retry response.
RPC_RESPONSE_TIMEOUT = WEDGE_DEADLINE + WATCHDOG_POLL_INTERVAL + 20.0
RESILIENT_RESPONSE_TIMEOUT = RPC_RESPONSE_TIMEOUT + 40.0

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


# ── process monitor + cgroup helpers ───────────────────────


def _set_parent_death_signal(sig: int = signal.SIGKILL) -> None:
    """Ask Linux to kill the monitor if its owning Python process disappears."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, sig, 0, 0, 0)  # PR_SET_PDEATHSIG
        if os.getppid() == 1:
            os.kill(os.getpid(), sig)
    except Exception:
        pass


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        state = stat.rsplit(")", 1)[1].strip().split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return False


def _read_rss_anon(pid: int) -> int | None:
    """Return anonymous RSS, the part that reflects Lean heap growth."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text(
                encoding="utf-8").splitlines():
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_oom_kill(path: str) -> int | None:
    try:
        for line in Path(path, "memory.events").read_text(
                encoding="utf-8").splitlines():
            key, value = line.split()
            if key == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        return None
    return None


def _systemd_scope_result(unit: str) -> str:
    """Return the terminal result of a transient scope, if still available."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "Result", "--value"],
            capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _monitor_event(event_queue, generation: int, trigger: str, reason: str,
                   worker_ids: list[int] | None = None) -> None:
    event_queue.put({
        "type": "trigger",
        "generation": generation,
        "trigger": trigger,
        "reason": reason,
        "worker_ids": list(worker_ids or []),
    })


def _watchdog_monitor_main(command_queue, event_queue, parent_pid: int,
                           deadline: float, poll_interval: float,
                           memory_poll_interval: float,
                           soft_memory_limit: int | None,
                           hard_memory_limit: int | None,
                           replacement_grace: float =
                           WORKER_REPLACEMENT_GRACE) -> None:
    """Observe Lean from a separate process and report restart triggers."""
    _set_parent_death_signal()
    root_pid: int | None = None
    generation = 0
    paused = True
    tasks: dict[int, float] = {}
    guards: dict[int, dict[str, Any]] = {}
    missing_since: dict[int, float] = {}
    last_deadline_poll = 0.0

    while True:
        while True:
            try:
                command = command_queue.get_nowait()
            except queue.Empty:
                break
            kind = command.get("type")
            if kind == "stop":
                return
            if kind == "pause":
                paused = True
                tasks.clear()
                guards.clear()
                missing_since.clear()
                continue
            if kind == "root":
                root_pid = int(command["pid"])
                generation = int(command["generation"])
                paused = False
                tasks.clear()
                guards.clear()
                missing_since.clear()
                continue
            if kind == "guards":
                if int(command["generation"]) == generation:
                    guards = {
                        int(worker_id): dict(value)
                        for worker_id, value in command["guards"].items()}
                    missing_since.clear()
                continue
            if kind == "task_started":
                if int(command["generation"]) == generation:
                    tasks[int(command["worker_id"])] = float(
                        command["started_at"])
                continue
            if kind == "task_finished":
                if int(command["generation"]) == generation:
                    tasks.pop(int(command["worker_id"]), None)
                continue
            if kind == "fatal":
                if not paused and int(command["generation"]) == generation:
                    _monitor_event(
                        event_queue, generation, "fatal",
                        str(command["reason"]), list(tasks))
                    paused = True

        if os.getppid() != parent_pid:
            return
        if paused:
            time.sleep(min(0.2, memory_poll_interval))
            continue

        now = time.monotonic()
        if not _process_alive(root_pid):
            _monitor_event(
                event_queue, generation, "death",
                f"Lean server process exited (pid={root_pid})", list(tasks))
            paused = True
            continue

        if now - last_deadline_poll >= poll_interval:
            overdue = [
                worker_id for worker_id, started_at in tasks.items()
                if now - started_at > deadline]
            if overdue:
                _monitor_event(
                    event_queue, generation, "deadline",
                    f"task exceeded {deadline}s", overdue)
                paused = True
                continue
            last_deadline_poll = now

        for worker_id, guard in list(guards.items()):
            pid = int(guard["pid"])
            cgroup_path = str(guard["cgroup_path"])
            baseline_oom = int(guard.get("oom_kill", 0))
            current_oom = _read_oom_kill(cgroup_path) if cgroup_path else None
            if current_oom is not None and current_oom > baseline_oom:
                _monitor_event(
                    event_queue, generation, "memory",
                    "Lean worker hit its cgroup hard memory limit "
                    f"(worker={worker_id}, pid={pid})",
                    [worker_id])
                paused = True
                break

            rss_anon = _read_rss_anon(pid)
            if (soft_memory_limit is not None
                    and rss_anon is not None
                    and rss_anon > soft_memory_limit
                    and worker_id in tasks):
                _monitor_event(
                    event_queue, generation, "memory",
                    "Lean worker exceeded its anonymous-RSS soft limit "
                    f"(worker={worker_id}, pid={pid}, "
                    f"rss_anon={rss_anon}, limit={soft_memory_limit})",
                    [worker_id])
                paused = True
                break

            if _process_alive(pid):
                missing_since.pop(worker_id, None)
                continue

            old_unit = str(guard.get("unit", ""))
            scope_result = _systemd_scope_result(old_unit) if old_unit else ""
            if scope_result == "oom-kill":
                _monitor_event(
                    event_queue, generation, "memory",
                    "Lean worker was killed by its cgroup hard memory limit "
                    f"(worker={worker_id}, pid={pid})",
                    [worker_id])
                paused = True
                break

            # A Lean file worker is not PID-stable.  During ordinary document
            # cancellation/re-elaboration the server exits the old
            # ``--worker .../worker_N.lean`` process and immediately starts a
            # replacement for the same URI.  Rebind the hard guard to that new
            # PID instead of poisoning a healthy task and restarting the pool.
            replacement_pid = _lean_worker_pids(
                int(root_pid or 0)).get(worker_id)
            if (replacement_pid is not None
                    and replacement_pid != pid
                    and _process_alive(replacement_pid)):
                try:
                    if hard_memory_limit is None:
                        replacement_guard = {
                            "pid": replacement_pid,
                            "unit": "",
                            "cgroup_path": "",
                            "oom_kill": 0,
                        }
                    else:
                        unit, replacement_cgroup = _systemd_scope_for_pid(
                            replacement_pid, worker_id, hard_memory_limit)
                        replacement_guard = {
                            "pid": replacement_pid,
                            "unit": unit,
                            "cgroup_path": replacement_cgroup,
                            "oom_kill":
                                _read_oom_kill(replacement_cgroup) or 0,
                        }
                    # The replacement can itself be superseded while systemd
                    # adopts it.  Never publish a guard for an already-dead PID.
                    if not _process_alive(replacement_pid):
                        replacement_unit = str(replacement_guard.get("unit", ""))
                        if replacement_unit:
                            _stop_systemd_scope(replacement_unit)
                        missing_since.setdefault(worker_id, now)
                        continue
                except Exception:
                    # Retrying here is safer than declaring the proof toxic:
                    # the Lean server is alive and already demonstrated that it
                    # can replace this logical worker.
                    missing_since.setdefault(worker_id, now)
                    continue

                old_guard = guard
                guards[worker_id] = replacement_guard
                missing_since.pop(worker_id, None)
                old_unit = str(old_guard.get("unit", ""))
                if old_unit:
                    _stop_systemd_scope(old_unit)
                event_queue.put({
                    "type": "guard_replaced",
                    "generation": generation,
                    "worker_id": worker_id,
                    "old_pid": pid,
                    "guard": replacement_guard,
                })
                continue

            first_missing = missing_since.setdefault(worker_id, now)
            if (worker_id not in tasks
                    or now - first_missing < replacement_grace):
                continue

            _monitor_event(
                event_queue, generation, "worker_death",
                "Guarded Lean worker exited without a replacement while "
                "processing a task "
                f"(worker={worker_id}, pid={pid}, "
                f"scope_result={scope_result or 'unknown'})",
                [worker_id])
            paused = True
            break

        time.sleep(memory_poll_interval)


def _systemd_scope_for_pid(pid: int, worker_id: int,
                           memory_max: int) -> tuple[str, str]:
    """Adopt one existing Lean worker into a transient user scope."""
    unit = f"pyleaner-w{worker_id}-p{pid}-{uuid.uuid4().hex[:8]}.scope"
    properties: list[tuple[str, str, list[str]]] = [
        ("Description", "s", [
            f"PyLeaner guarded worker {worker_id} (pid {pid})"]),
        ("PIDs", "au", ["1", str(pid)]),
        ("MemoryMax", "t", [str(memory_max)]),
        # Do not let a runaway elaboration evade MemoryMax by filling swap and
        # stalling the entire machine before the monitor is scheduled.
        ("MemorySwapMax", "t", ["0"]),
    ]
    command = [
        "busctl", "--user", "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        unit, "fail", str(len(properties)),
    ]
    for name, signature, values in properties:
        command.extend([name, signature, *values])
    command.append("0")
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot create memory guard {unit}: "
            f"{result.stderr.strip() or result.stdout.strip()}")

    result = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "-p", "ControlGroup", "--value"],
        capture_output=True, text=True, timeout=10)
    control_group = result.stdout.strip()
    if result.returncode != 0 or not control_group:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True, timeout=10)
        raise RuntimeError(f"cannot resolve cgroup for {unit}")
    return unit, f"/sys/fs/cgroup{control_group}"


def _stop_systemd_scope(unit: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit],
            capture_output=True, timeout=10)
    except Exception:
        pass


def _lean_worker_pids(root_pid: int) -> dict[int, int]:
    """Map ``worker_N.lean`` URI suffixes to Lean OS worker PIDs."""
    result: dict[int, int] = {}
    for pid in _collect_descendants(root_pid):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                b"\0", b" ").decode("utf-8", errors="replace")
        except OSError:
            continue
        if "--worker" not in cmdline:
            continue
        match = re.search(r"worker_(\d+)\.lean", cmdline)
        if match:
            result[int(match.group(1))] = pid
    return result


# ── watchdog ────────────────────────────────────────────────


class Watchdog:
    """Process-backed observer plus online recovery coordinator."""

    def __init__(
        self,
        client: "LspClient",
        interval: float = WATCHDOG_POLL_INTERVAL,
    ):
        """
        Args:
            client: The ``LspClient`` whose server to monitor. Mutated in place
                on respawn (same object all callers hold).
            interval: Seconds between checks.
        """
        self.client = client
        self.interval = interval
        self.memory_poll_interval = float(getattr(
            client, "watchdog_memory_poll_interval", MEMORY_POLL_INTERVAL))
        self.worker_memory_high_bytes: int | None = getattr(
            client, "worker_memory_high_bytes", None)
        self.worker_memory_max_bytes: int | None = getattr(
            client, "worker_memory_max_bytes", None)
        if self.memory_poll_interval <= 0:
            raise ValueError("watchdog memory poll interval must be positive")
        if ((self.worker_memory_high_bytes is None)
                != (self.worker_memory_max_bytes is None)):
            raise ValueError(
                "worker memory high/max limits must both be set or both be None")
        if (self.worker_memory_high_bytes is not None
                and self.worker_memory_high_bytes <= 0):
            raise ValueError("worker memory limits must be positive")
        if (self.worker_memory_high_bytes is not None
                and self.worker_memory_high_bytes >= self.worker_memory_max_bytes):
            raise ValueError("worker memory high limit must be below max limit")

        # Pool config captured from create_pool(), used to rebuild on respawn.
        self._armed: bool = False
        self._base_text: str = ""
        self._size: int = 1
        self._lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._stop = threading.Event()
        self._generation = 0
        self._process: Optional[multiprocessing.Process] = None
        self._command_queue = None
        self._event_queue = None
        # The receiver only bridges monitor events back into the owning process;
        # all observation and limit detection occurs in ``_process``.
        self._thread: Optional[threading.Thread] = None
        self._worker_scopes: dict[int, dict[str, Any]] = {}
        self._scope_lock = threading.Lock()
        # Cleared while a restart is in progress; submit_resilient waits on it.
        self.server_ready = threading.Event()
        self.server_ready.set()

    # ── lifecycle ───────────────────────────────────────────

    def arm(self, base_text: str, size: int = 1) -> None:
        """Capture the pool config so a respawn can rebuild it faithfully."""
        with self._lock:
            self._base_text = base_text
            self._size = size
            self._armed = True

    def start(self) -> None:
        """Start the monitor process and its event receiver (idempotent)."""
        if self._process is None or not self._process.is_alive():
            context = multiprocessing.get_context("spawn")
            self._command_queue = context.Queue()
            self._event_queue = context.Queue()
            self._stop.clear()
            self._process = context.Process(
                target=_watchdog_monitor_main,
                args=(
                    self._command_queue,
                    self._event_queue,
                    os.getpid(),
                    WEDGE_DEADLINE,
                    self.interval,
                    self.memory_poll_interval,
                    self.worker_memory_high_bytes,
                    self.worker_memory_max_bytes,
                    WORKER_REPLACEMENT_GRACE,
                ),
                name="lean-watchdog-monitor",
                daemon=True,
            )
            self._process.start()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._receive_events,
                daemon=True,
                name="lean-watchdog-events",
            )
            self._thread.start()
        proc = self.client.process
        if proc is not None and proc.poll() is None:
            self._set_root_process(proc.pid)
        debug_log("Lean server watchdog process started")

    def stop(self) -> None:
        """Stop the observer, receiver, and transient worker scopes."""
        self._stop.set()
        if self._command_queue is not None:
            try:
                self._command_queue.put({"type": "stop"})
            except Exception:
                pass
        if self._process is not None:
            self._process.join(timeout=max(5.0, self.memory_poll_interval + 2.0))
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=5.0)
            self._process = None
        if self._event_queue is not None:
            try:
                self._event_queue.put({"type": "stop"})
            except Exception:
                pass
        if self._thread is not None:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=5.0)
            self._thread = None
        # The monitor creates replacement scopes itself so the new worker is
        # protected immediately. If shutdown raced with the receiver thread,
        # adopt any queued replacement records before stopping every scope.
        if self._event_queue is not None:
            while True:
                try:
                    pending = self._event_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                if pending.get("type") == "guard_replaced":
                    self._accept_replacement_guard(pending)
        self._stop_worker_scopes()
        for q in (self._command_queue, self._event_queue):
            if q is not None:
                try:
                    q.close()
                except Exception:
                    pass
        self._command_queue = None
        self._event_queue = None
        debug_log("Lean server watchdog process stopped")

    def _send(self, message: dict[str, Any]) -> None:
        q = self._command_queue
        if q is not None and self._process is not None:
            try:
                q.put(message)
            except Exception:
                pass

    def _set_root_process(self, pid: int) -> None:
        self._generation += 1
        self._send({
            "type": "root",
            "pid": int(pid),
            "generation": self._generation,
        })

    def _pause_monitor(self) -> None:
        self._send({
            "type": "pause",
            "generation": self._generation,
        })

    def task_started(self, worker_id: int, started_at: float) -> int:
        generation = self._generation
        self._send({
            "type": "task_started",
            "generation": generation,
            "worker_id": int(worker_id),
            "started_at": float(started_at),
        })
        return generation

    def task_finished(self, worker_id: int, generation: int) -> None:
        self._send({
            "type": "task_finished",
            "generation": int(generation),
            "worker_id": int(worker_id),
        })

    # ── fatal-stderr channel ────────────────────────────────

    def flag_fatal(self, reason: str) -> None:
        """Called by ``LspClient._read_stderr`` when the server prints a fatal."""
        self._send({
            "type": "fatal",
            "generation": self._generation,
            "reason": str(reason),
        })

    # ── detection helpers ───────────────────────────────────

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

    def _receive_events(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                process = self._process
                if process is not None and not process.is_alive():
                    self._recover_with_retry(
                        "watchdog_death",
                        "Lean watchdog monitor process exited unexpectedly",
                        set(),
                    )
                continue
            except Exception:
                return
            if event.get("type") == "stop":
                return
            if event.get("type") == "guard_replaced":
                self._accept_replacement_guard(event)
                continue
            if event.get("type") != "trigger":
                continue
            if int(event.get("generation", -1)) != self._generation:
                continue
            self._recover_with_retry(
                str(event["trigger"]),
                str(event["reason"]),
                {
                    int(value)
                    for value in event.get("worker_ids", [])
                },
            )

    def _recover_with_retry(self, trigger: str, reason: str,
                            culprit_worker_ids: set[int]) -> None:
        """Keep rebuilding after transient recovery failures.

        A failed reconnect must not leave ``server_ready`` cleared forever.
        This loop runs only in the event receiver; the separate observer and
        kernel cgroup remain independent of it.
        """
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            try:
                self._restart(
                    trigger,
                    reason,
                    culprit_worker_ids=culprit_worker_ids,
                )
                return
            except Exception as exc:
                delay = min(30.0, float(2 ** min(attempt - 1, 5)))
                print(
                    "watchdog: restart attempt "
                    f"{attempt} failed: {exc}; retrying in {delay:.0f}s",
                    flush=True,
                )
                self._stop.wait(delay)

    # ── cgroup guards ───────────────────────────────────────

    def _accept_replacement_guard(self, event: dict[str, Any]) -> None:
        """Transfer ownership of a monitor-created replacement guard.

        The monitor creates the scope immediately to minimize the interval in
        which a freshly spawned Lean worker is outside ``MemoryMax``.  The
        owning process records it here so later restart/shutdown cleanup stops
        the current scope rather than the obsolete one.
        """
        replacement = dict(event.get("guard") or {})
        unit = str(replacement.get("unit", ""))
        if int(event.get("generation", -1)) != self._generation:
            if unit:
                _stop_systemd_scope(unit)
            return
        worker_id = int(event["worker_id"])
        old_pid = int(event["old_pid"])
        accepted = False
        with self._scope_lock:
            current = self._worker_scopes.get(worker_id)
            if current is not None and int(current.get("pid", -1)) == old_pid:
                self._worker_scopes[worker_id] = replacement
                accepted = True
        if accepted:
            print(
                "watchdog: logical Lean worker "
                f"{worker_id} replaced PID {old_pid} -> "
                f"{replacement.get('pid')}; memory guard rebound",
                flush=True,
            )
        elif unit:
            # A newer generation/replacement won the race; do not leak the
            # stale transient scope.
            _stop_systemd_scope(unit)

    @property
    def memory_guards_enabled(self) -> bool:
        return self.worker_memory_max_bytes is not None

    def attach_worker_guards(self, timeout: float = 15.0) -> None:
        """Observe every worker and optionally put it in a hard-limited scope."""
        proc = self.client.process
        if proc is None or proc.poll() is not None:
            raise RuntimeError("cannot observe workers of a dead Lean server")

        deadline = time.monotonic() + timeout
        worker_pids: dict[int, int] = {}
        while time.monotonic() < deadline:
            worker_pids = _lean_worker_pids(proc.pid)
            if all(worker_id in worker_pids
                   for worker_id in range(1, self._size + 1)):
                break
            time.sleep(0.2)
        missing = sorted(
            set(range(1, self._size + 1)) - set(worker_pids))
        if missing:
            if self.memory_guards_enabled:
                raise RuntimeError(
                    f"cannot locate Lean OS worker(s) for cgroup guard: {missing}")
            print(
                "[WARNING] cannot locate Lean OS worker(s) for liveness "
                f"observation: {missing}",
                flush=True,
            )
            return

        self._stop_worker_scopes()
        created: dict[int, dict[str, Any]] = {}
        try:
            for worker_id in range(1, self._size + 1):
                pid = worker_pids[worker_id]
                if self.memory_guards_enabled:
                    unit, cgroup_path = _systemd_scope_for_pid(
                        pid,
                        worker_id,
                        int(self.worker_memory_max_bytes),
                    )
                else:
                    unit, cgroup_path = "", ""
                created[worker_id] = {
                    "pid": pid,
                    "unit": unit,
                    "cgroup_path": cgroup_path,
                    "oom_kill":
                        (_read_oom_kill(cgroup_path) or 0) if cgroup_path else 0,
                }
        except Exception:
            for value in created.values():
                _stop_systemd_scope(str(value["unit"]))
            raise
        with self._scope_lock:
            self._worker_scopes = created
        self._send({
            "type": "guards",
            "generation": self._generation,
            "guards": created,
        })

    def _stop_worker_scopes(self) -> None:
        with self._scope_lock:
            scopes, self._worker_scopes = self._worker_scopes, {}
        for value in scopes.values():
            unit = str(value["unit"])
            if unit:
                _stop_systemd_scope(unit)

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
        # Wake every LSP/RPC caller before dropping the old response routing.
        # Merely clearing this mapping leaves callers blocked until their local
        # timeout, which can beat submit_resilient's recovery protocol.
        pending = []
        try:
            with self.client._pending_lock:
                pending = list(self.client._pending_requests.values())
                self.client._pending_requests.clear()
        except Exception:
            pending = []
        from .errors import ServiceUnavailable
        for response_q in pending:
            try:
                response_q.put_nowait(ServiceUnavailable())
            except Exception:
                pass

    def _restart(self, trigger: str, reason: str,
                 culprit_worker_ids: set[int] | None = None) -> None:
        """Rebuild the server and return toxicity only to attributed tasks.

        Order: clear server_ready -> attribute+mark toxic -> poison all workers
        (wake blocked _didchange) -> teardown keep-alive -> kill tree -> connect
        -> rebuild pool -> set server_ready.
        """
        if not self._restart_lock.acquire(blocking=False):
            return
        self.server_ready.clear()
        self._pause_monitor()
        try:
            pairs = self._inflight_tasks()
            # Fatal/root-death events cannot be attributed more narrowly.
            precise = trigger in {
                "deadline", "memory", "worker_death", "watchdog_death"}
            culprit_ids = set(culprit_worker_ids or ())
            if not precise:
                culprit_ids = {int(w.worker_id) for w, _ in pairs}
            toxic = []
            for worker, task in pairs:
                if int(worker.worker_id) in culprit_ids:
                    task["_culprit"] = True
                    task["_culprit_reason"] = reason
                    toxic.append(task)

            # Poison every worker so blocked calls wake now.  Only the task(s)
            # above carry ``_culprit=True``; the rest transparently retry.
            for (worker, _) in pairs:
                try:
                    worker.notification_queue.put_nowait({
                        "method": POISON_METHOD})
                except Exception:
                    pass

            known_pids: set = set()
            try:
                if (self.client.process is not None
                        and self.client.process.pid is not None):
                    root_pid = self.client.process.pid
                    known_pids.add(root_pid)
                    known_pids.update(_collect_descendants(root_pid))
            except Exception:
                pass
            print(
                f"watchdog: {trigger} ({reason}) — reviving; "
                f"{len(toxic)} task(s) marked toxic",
                flush=True,
            )
            self._teardown_pool_state()
            _kill_process_tree(self.client.process, known_pids=known_pids)
            self._stop_worker_scopes()
            self.client.connect(timeout=300)
            if self._armed:
                self.client.create_pool(text=self._base_text, size=self._size)
            self.server_ready.set()
        finally:
            self._restart_lock.release()
