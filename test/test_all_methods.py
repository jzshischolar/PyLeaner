#!/usr/bin/env python3
"""Comprehensive incremental test for all Worker atomic methods via PyLeaner.

Tests one method at a time with its own fresh LSP session.
Usage:
    python test/test_all_methods.py               # run all tests
    python test/test_all_methods.py 1             # run only test #1 (ping)
    python test/test_all_methods.py 1 3 7         # run tests #1, #3, #7
"""

import sys
import os
import queue

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_PATH)
from pyleaner import LspClient, Task


# ── Helpers ─────────────────────────────────────────────────

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


def run_one(num: int, name: str, task_type: str, kwargs: dict,
            expect_success: bool = True,
            cwd=None) -> bool:
    """Run a single test with its own fresh session."""
    print(f"\n[{num}] {name} ... ", end="", flush=True)

    client = None
    try:
        client = start_client(cwd)
        uri = f"file://{PROJECT_PATH}/workers/test_{num}/"
        # Use Simpler/lean for proof_goal test, HasParams.lean for everything else
        text = kwargs.pop("_text", CONTENT)
        client.initialize_worker_pool(size=1, init_uri=uri, init_text=text)

        success, content = submit(client, task_type, kwargs)
        passed = (success == expect_success)

        if passed:
            preview = ""
            if success and content is not None:
                if isinstance(content, dict):
                    preview = f"keys={sorted(content.keys())}"
                elif isinstance(content, list):
                    preview = f"list[{len(content)} items]"
                elif isinstance(content, str):
                    preview = content[:80].replace("\n", "\\n")
                else:
                    preview = str(type(content).__name__)
            print(f"PASS  {preview}")
        else:
            print("FAIL")
            print(f"  expected_success={expect_success}, got success={success}")
            print(f"  error/content: {content!r}")

        return passed

    except Exception as e:
        passed = not expect_success
        icon = "PASS (expected failure)" if passed else "FAIL"
        print(f"{icon}")
        print(f"  exception: {e}")
        return passed

    finally:
        if client:
            stop_client(client)


# ── Test definitions ────────────────────────────────────────

TESTS = [
    # (name, task_type, kwargs)
    (1,  "ping",                      "ping",                    {}),
    (2,  "echo",                      "echo",                    {"message": "hello"}),
    (3,  "debug_document",            "debug_document",          {}),
    (4,  "parse_document",            "parse_document",          {}),
    (5,  "test_declaration_kind",     "test_declaration_kind",   {}),
    (6,  "test_declaration_name",     "test_declaration_name",   {}),
    (7,  "extract_declarations",      "extract_declarations",    {"text": CONTENT, "content_range": FULL_RANGE}),
    (8,  "test_has_params",           "test_has_params",         {}),
    (9,  "test_params_text",          "test_params_text",        {}),
    (10, "test_type_text",            "test_type_text",          {}),
    (11, "test_body_text",            "test_body_text",          {}),
    (12, "test_body_fields",          "test_body_fields",        {}),
    (13, "debug_body_fields",         "debug_body_fields",       {}),
    (14, "debug_syntax_tree",         "debug_syntax_tree",       {}),
    (15, "debug_binder_structure",    "debug_binder_structure",  {}),
    (16, "debug_all_snapshots",       "debug_all_snapshots",     {}),
    (17, "debug_snapshot_info",       "debug_snapshot_info",     {}),
    (18, "changecontent (diagnostics)","changecontent",           {"text": CONTENT, "content_range": {}}),
    # A position without an active tactic goal is still a successful RPC.  The
    # public API returns ``{"proof_goal": None, "diagnostics": [...]}`` rather
    # than failing the worker task.
    (19, "get_proof_goal",            "get_proof_goal",          {"text": CONTENT, "content_range": FULL_RANGE, "position": {"line": 8, "character": 0}}),
]

TESTS_MAP = {t[0]: t for t in TESTS}


# ── Main ────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args:
        selected = []
        for a in args:
            try:
                idx = int(a)
                if idx in TESTS_MAP:
                    selected.append(TESTS_MAP[idx])
                else:
                    print(f"Unknown test #{idx} (valid: 1-{len(TESTS)})")
                    return 1
            except ValueError:
                print(f"Invalid argument: {a}")
                return 1
    else:
        selected = TESTS

    print("=" * 60)
    print(f"PyLeaner — Incremental Test Suite")
    print(f"Running {len(selected)} test(s) in sequence")
    print("=" * 60)

    passed = 0
    failed = 0
    for t in selected:
        num, name, task_type, kwargs = t[0], t[1], t[2], t[3]
        expect = t[4] if len(t) > 4 else True
        ok = run_one(num, name, task_type, dict(kwargs), expect_success=expect)
        if ok:
            passed += 1
        else:
            failed += 1

    print("\n" + "=" * 60)
    print(f"Result: {passed} passed, {failed} failed (total {len(selected)})")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
