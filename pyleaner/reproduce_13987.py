#!/usr/bin/env python3
"""Reproduce leanprover/lean4#13987 *through the PyLeaner framework*.

`Lean.Json.parse` aborts the whole program with

    INTERNAL PANIC: Nat.pow exponent is too big

when it meets a JSON number whose exponent is large but still below 2^64, e.g.
``3E9999999993``.  The exponent slips past the parser's loose bound
(``if n > USize.size then fail "exp too large"``) because 9999999993 << 2^64,
then gets materialised as ``10 ^ 9999999993`` via ``Nat.pow``, which trips the
runtime guard.  The panic is unrecoverable (cannot be caught with ``Except``),
so it kills the whole server process.

The crucial detail
------------------
The panic fires only when ``3E9999999993`` reaches the server as a **bare JSON
number token** (unquoted).  A **quoted JSON string** ``"3E9999999993"`` is just
characters -- ``Lean.Json.parse`` never runs the number path on it, so it does
NOT crash.  (Likewise the document text sent in didChange is a JSON string and
is elaborated as Lean source, never re-parsed as a JSON number.)  This script
proves both sides with a control and a trigger in the same session.

Why a serialization hook
------------------------
The framework only sends dicts through ``json.dumps``, and no Python value
serialises to the bare token ``3E9999999993`` (int -> digits, no ``E``; float ->
overflows to ``inf``).  So to keep the trigger inside the framework while still
getting the bare token on the wire, we rewrite the single serialization exit
(``LspClient._make_lsp_message``) so that a quoted sentinel in the payload
becomes the bare number.  Every other message is byte-for-byte unchanged.

Usage:
    python reproduce_13987.py
"""

import sys
import os
import queue
import time
import threading

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_PATH)
from pyleaner import LspClient, Task  # noqa: E402


# ── Constants ───────────────────────────────────────────────

# A large-but-below-2^64 exponent, exactly as in upstream #13987.
EVIL_EXPONENT = "3E9999999993"

# Sentinel planted in a payload; the serialization hook rewrites its quoted
# form into the bare evil number on the wire.
SENTINEL = "@@EVIL_EXPONENT@@"


# ── Helpers (mirror test_all_methods.py) ────────────────────

def load(name: str) -> str:
    path = os.path.join(PROJECT_PATH, "examples", name)
    with open(path) as f:
        return f.read()

CONTENT = load("HasParams.lean")
FULL_RANGE = {"start": {"line": 0, "character": 0},
              "end": {"line": 999, "character": 0}}


def start_client(cwd=None):
    cwd = cwd or PROJECT_PATH
    c = LspClient(server_cmd=["lake", "serve"], cwd=cwd)
    c.start()
    c.initialize(f"file://{cwd}")
    c.initialized()
    return c


def stop_client(c):
    try:
        c.shutdown()
        c.exit()
    except Exception:
        pass


def submit(client, task_type, kwargs, timeout=60.0):
    """Submit one task and return (success, content_or_error)."""
    rq = queue.Queue()
    task: Task = {"task_type": task_type, "result_q": rq, "kwargs": kwargs}
    client.worker_pool.submit_task(task)
    resp = rq.get(timeout=timeout)
    if resp.get("success"):
        return True, resp.get("content")
    else:
        return False, resp.get("error")


def server_alive(client) -> bool:
    return client.process is not None and client.process.poll() is None


# ── Serialization hook ──────────────────────────────────────

def install_evil_hook():
    """Rewrite the quoted SENTINEL into the bare evil number at the wire.

    Replaces LspClient._make_lsp_message (the only place that builds an outgoing
    frame).  For messages without the sentinel the output is identical to the
    original (json.dumps + Content-Length framing); for the one message carrying
    the sentinel, the quoted string becomes a bare JSON number so the server's
    Lean.Json.parse hits #13987.  Content-Length is recomputed from the rewritten
    body so the frame stays well-formed.
    """
    import json

    def _make_lsp_message(payload):
        s = json.dumps(payload)
        if ('"' + SENTINEL + '"') in s:
            s = s.replace('"' + SENTINEL + '"', EVIL_EXPONENT)
        body = s.encode("utf-8")
        return b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body

    # The original is a @staticmethod, so wrap the replacement the same way;
    # then `self._make_lsp_message(payload)` calls it without binding self.
    LspClient._make_lsp_message = staticmethod(_make_lsp_message)


# ── Main ────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("PyLeaner -- lean4#13987 reproduction (through the framework)")
    print("=" * 60)

    client = None
    try:
        print("\n[1] starting client + worker pool ...")
        client = start_client()
        uri = f"file://{PROJECT_PATH}/workers/repro_13987/"
        client.initialize_worker_pool(size=1, init_uri=uri, init_text=CONTENT)
        print(f"    server alive = {server_alive(client)}")

        print("\n[2] baseline: a normal RPC task (ping) ...")
        ok, content = submit(client, "ping", {})
        print(f"    -> success = {ok}, content = {content!r}")
        if not ok:
            print("    baseline failed -- aborting (pool/server not healthy)")
            return 2

        # ── Control: a QUOTED string does NOT trigger the bug ──
        # Lean.Json.parse only computes Nat.pow for a bare number token; a
        # quoted JSON string is just characters, so the server must survive.
        print('\n[3] CONTROL: echo() with a QUOTED string "3E9999999993" ...')
        ok, content = submit(client, "echo", {"message": "3E9999999993"})
        print(f"    -> success = {ok}, content = {content!r}")
        print(f"    -> server still alive = {server_alive(client)} "
              "(expected: True -- a string value does not panic)")

        # ── Trigger: a BARE number DOES trigger the bug ──
        print("\n[4] TRIGGER: echo() with a BARE number 3E9999999993 ...")
        print("    installing serialization hook that emits the bare token")
        install_evil_hook()

        holder = {}

        def fire():
            try:
                holder["res"] = submit(
                    client, "echo", {"message": SENTINEL}, timeout=70.0
                )
            except Exception as e:  # e.g. submit's own queue timeout
                holder["err"] = e

        t = threading.Thread(target=fire, daemon=True)
        t.start()

        # The panic is near-instant once the server reads the corrupted frame.
        print("    watching the server process ...")
        deadline = time.monotonic() + 15
        exit_code = None
        while time.monotonic() < deadline:
            rc = client.process.poll()
            if rc is not None:
                exit_code = rc
                break
            time.sleep(0.2)
        state = "CRASHED" if exit_code is not None else "still alive"
        print(f"    server process exit_code = {exit_code} ({state})")

        print("\n[5] observing the framework's reaction ...")
        t.join(timeout=10)
        if t.is_alive():
            print("    echo submit STILL BLOCKED after 10s.")
            print("    -> no crash detection: the worker parks on the RPC")
            print("       response queue and wakes only at the 60s timeout.")
        else:
            print(f"    echo submit returned: {holder.get('res')} "
                  f"(err={holder.get('err')})")

        print("\n" + "=" * 60)
        if exit_code is not None:
            print(f"REPRODUCED -- server crashed (exit {exit_code}).")
            print("Above you should see: SERVER STDERR: INTERNAL PANIC: "
                  "Nat.pow exponent is too big")
            print("Contrast: the quoted-string control in [3] did NOT crash.")
        else:
            print("NOT REPRODUCED -- server did not crash within 15s.")
        print("=" * 60)
        return 0 if exit_code is not None else 1

    finally:
        # stop_client() calls shutdown() (a blocking request/response) which
        # hangs for ~60s on a dead server, so only attempt graceful shutdown
        # while the process is still alive; otherwise just reap the corpse.
        if client is not None:
            if server_alive(client):
                stop_client(client)
            else:
                try:
                    client.exit()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
