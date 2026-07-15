#!/usr/bin/env python3
"""Does a huge Nat.pow in Lean content crash the server (path B)?

Hypothesis: an AI agent's "check" step forced Lean to elaborate content
containing a huge power, e.g. Nat.pow 10 9999999993, and the runtime
lean_nat_pow panicked with 'Nat.pow exponent is too big', killing the server.

This is the *Lean-evaluation* path (distinct from the JSON-parse path of
lean4#13987): the panic originates in the C runtime lean_nat_pow, reached
whenever Lean actually evaluates Nat.pow with an astronomically large exponent.

We test three forms, each in its own fresh session (a crash kills the server):
  #check  -- type-checks only, does NOT evaluate the term.
  #eval   -- forces full runtime evaluation -> lean_nat_pow -> should panic.
  #reduce -- forces kernel normalization (may not unfold @extern Nat.pow).

The contrast shows which Lean construct actually triggers the crash.

Run: python test/check_pow2005.py
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

# The exact exponent from lean4#13987 (large but < 2^64).
HUGE_EXP = 9999999993

SAFE_INIT = "-- safe init (no commands)\n"


# ── Helpers (mirror test_all_methods.py) ────────────────────

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


def alive(c) -> bool:
    return c.process is not None and c.process.poll() is None


# ── Per-variant runner ──────────────────────────────────────

def try_content(name: str, content: str,
                wait_crash: float = 20.0) -> bool:
    """Fresh session: load SAFE_INIT, then didChange to `content`, watch.

    Returns True if the server CRASHED on this content.
    """
    print(f"\n--- {name} ---")
    print(f"    Lean: {content!r}")
    client = None
    try:
        client = start_client()
        uri = f"file://{PROJECT_PATH}/workers/powhuge/{name}/"
        client.initialize_worker_pool(
            size=1, init_uri=uri, init_text=SAFE_INIT
        )

        holder = {}

        def fire():
            try:
                rq = queue.Queue()
                task: Task = {"task_type": "changecontent", "result_q": rq,
                              "kwargs": {"text": content, "content_range": {}}}
                client.worker_pool.submit_task(task)
                holder["resp"] = rq.get(timeout=120.0)
            except Exception as e:
                holder["err"] = e

        t = threading.Thread(target=fire, daemon=True)
        t.start()

        # A panic kills the server near-instantly once elaboration runs.
        deadline = time.monotonic() + wait_crash
        exit_code = None
        while time.monotonic() < deadline:
            rc = client.process.poll()
            if rc is not None:
                exit_code = rc
                break
            time.sleep(0.3)

        t.join(timeout=8)
        crashed = exit_code is not None
        if crashed:
            print(f"    => SERVER CRASHED (exit {exit_code})  <-- panic")
        else:
            resp = holder.get("resp")
            if isinstance(resp, dict) and resp.get("success"):
                diags = resp.get("content") or []
                print(f"    => server survived; {len(diags)} diagnostic(s)")
                for d in diags[:3]:
                    msg = str(d.get("message", "")).replace("\n", " ")
                    print(f"         - {msg[:80]}")
            else:
                print(f"    => server survived; result={resp} "
                      f"err={holder.get('err')}")
        return crashed

    finally:
        if client is not None:
            if alive(client):
                stop_client(client)
            else:
                try:
                    client.exit()
                except Exception:
                    pass


# ── Variants ────────────────────────────────────────────────

VARIANTS = [
    ("check_no_eval",
     f"#check Nat.pow 10 {HUGE_EXP}"),
    ("reduce_kernel",
     f"#reduce Nat.pow 10 {HUGE_EXP}"),
    ("eval_runtime",
     f"#eval Nat.pow 10 {HUGE_EXP}"),
]


# ── Main ────────────────────────────────────────────────────

def main() -> int:
    print("=" * 64)
    print(f"Does a huge Nat.pow (exp={HUGE_EXP}) crash the server?")
    print("=" * 64)

    results = {}
    for name, content in VARIANTS:
        results[name] = try_content(name, content)

    print("\n" + "=" * 64)
    print("Summary:")
    for name, _ in VARIANTS:
        rc = results[name]
        tag = "CRASHED (panic)" if rc else "survived"
        print(f"  {name:16s} -> {tag}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
