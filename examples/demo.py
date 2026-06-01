#!/usr/bin/env python3
"""PyLeaner usage demo.

This script demonstrates the three core PyLeaner features:
  1. extract_declarations — parse Lean source into structured declarations
  2. get_diagnostics       — retrieve Lean compiler errors/warnings
  3. get_proof_goal        — inspect proof goals at a cursor position

Prerequisites:
  pip install -e /path/to/PyLeaner
  lake build                # inside your Lean project

Usage:
  python examples/demo.py                          # run all demos
  python examples/demo.py --declarations           # only declaration extraction
  python examples/demo.py --diagnostics            # only diagnostics
  python examples/demo.py --proof-goal             # only proof goal
"""

import os
import sys
import json
import argparse

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_PATH)

from pyleaner import LspClient

content_range = {
        "start": {"line": 2, "character": 0},
        "end": {"line": 999, "character": 0},
    }

# ── Setup helpers ──────────────────────────────────────────

def make_client():
    """Create and connect a Lean LSP client (one call).

    Note: `lake serve` may take up to 3 min on first startup with Mathlib.
    Adjust the timeout as needed.
    """
    c = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    c.connect(timeout=300)  # generous timeout for Mathlib startup
    return c


def stop(c):
    """Shutdown the client gracefully."""
    try:
        c.shutdown()
        c.exit()
    except Exception:
        pass


# ── Demo 1: Extract Declarations ───────────────────────────

def demo_declarations():
    """Parse a Lean file into structured declaration information."""
    print("=" * 60)
    print("Demo 1: extract_declarations")
    print("=" * 60)

    test_file = os.path.join(PROJECT_PATH, "examples", "HasParams.lean")
    with open(test_file) as f:
        content = f.read()

    c = make_client()



    try:
        pool = c.create_pool(text=content, size=1)
        if pool is None:
            raise RuntimeError
        # One line — no manual queue or Task dict
        result = pool.extract_declarations(text=content,content_range=content_range)

        decls = result.get("decls", [])
        print(f"\nFound {len(decls)} declarations:\n")

        for decl in decls:
            kind = decl["kind"]
            name = decl.get("name") or "(anonymous)"
            params = decl.get("paramsText")
            rtype = decl.get("typeText")

            sig_parts = []
            if params:
                sig_parts.append(params)
            if rtype:
                sig_parts.append(f": {rtype}")
            sig = " ".join(sig_parts)

            line = f"  {kind:10s} {name}"
            if sig:
                line += f"  {sig}"
            print(line)

            # Show structured params
            struct_params = decl.get("params")
            if struct_params:
                for p in struct_params:
                    pname = p.get("name") or "?"
                    ptype = p.get("type") or "?"
                    pkind = p.get("binderKind", "?")
                    print(f"            └ [{pkind}] {pname} : {ptype}")

        # Show one full DeclarationInfo as JSON
        if decls:
            print(f"\nFull JSON for first declaration:\n")
            print(json.dumps(decls[0], indent=2, ensure_ascii=False))

    finally:
        stop(c)


# ── Demo 2: Diagnostics ────────────────────────────────────

def demo_diagnostics():
    """Get Lean compiler diagnostics for a file."""
    print("=" * 60)
    print("Demo 2: get_diagnostics")
    print("=" * 60)

    content = """import LeanLspExtension


-- This has an error: adding a String to a Nat
def bad (x : Nat) : Nat := x + "hello"
"""

    c = make_client()

    try:
        c.create_pool(text=content, size=1)
        pool = c.worker_pool
        if pool is None:
            raise RuntimeError
        # One line
        diags = pool.get_diagnostics(text=content,content_range=content_range)

        if diags:
            print(f"\n{len(diags)} diagnostic(s) found:\n")
            for d in diags:
                msg = d.get("message", "")
                line = d.get("range", {}).get("start", {}).get("line", "?")
                print(f"  Line {line}: {msg}")
        else:
            print("\nNo diagnostics. (File may be correct.)")

    finally:
        stop(c)


# ── Demo 3: Proof Goal ─────────────────────────────────────

def demo_proof_goal():
    """Inspect the proof goal state at a specific position."""
    print("=" * 60)
    print("Demo 3: get_proof_goal")
    print("=" * 60)

    content = """

theorem demo_proof (a b : Nat) (h : a = b) : a + 1 = b + 1 := by
  sorry
"""

    c = make_client()

    try:
        c.create_pool(text=content, size=1)
        pool = c.worker_pool
        if pool is None:
            raise RuntimeError
        # Position on the `:= by` line (before any tactic runs)
        result = pool.get_proof_goal(
            text=content,
            content_range=content_range,
            position={"line": 5, "character": 0},
        )

        print(f"\nDiagnostics: {len(result.get('diagnostics', []))} items")
        goals = result.get("proof_goal")
        if goals:
            print(f"\nProof goals at position:\n")
            for g in goals:
                print(f"  {g}")
        else:
            print("\nNo proof goals at this position.")

    finally:
        stop(c)


# ── Main ────────────────────────────────────────────────────

DEMOS = {
    "declarations": demo_declarations,
    "diagnostics":  demo_diagnostics,
    "proof_goal":   demo_proof_goal,
}


def main():
    p = argparse.ArgumentParser(
        description="PyLeaner usage demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--declarations", action="store_true",
                   help="Run declaration extraction demo")
    p.add_argument("--diagnostics",  action="store_true",
                   help="Run diagnostics demo")
    p.add_argument("--proof-goal",   action="store_true",
                   help="Run proof goal demo")

    args = p.parse_args()
    selected = [k for k in ["declarations", "diagnostics", "proof_goal"]
                if getattr(args, k)]
    if not selected:
        selected = list(DEMOS.keys())

    for name in selected:
        try:
            DEMOS[name]()
            print()
        except Exception as e:
            print(f"\n  Demo '{name}' failed: {e}\n")

    print("Done.")


if __name__ == "__main__":
    main()