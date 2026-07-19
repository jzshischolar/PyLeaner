# PyLeaner

A Python interface to the Lean 4 kernel — designed for AI–Lean interactive automated theorem proving and related research. PyLeaner provides a production-ready bridge between Python and Lean's internals.

> **PyLeaner** consists of a **Lean 4 LSP extension** (for kernel-level RPC capabilities) and a **Python client** (for communication, worker pool management, and load balancing).

## Highlights

1. **Deep kernel access via native RPC.**  Unlike bridges built around the REPL or LSP, PyLeaner communicates directly with Lean’s kernel through its RPC extension. It does not shell out to external processes or rely on regex-based source parsing, which means it can, in principle, support the full range of kernel functionality as well as user-defined Lean functions.

2. **Native concurrency.**  A worker pool manages multiple Lean environments with automatic load balancing.  Tasks are routed to the least-busy worker; concurrency is transparent to the caller.

3. **Self-healing.**  A built-in watchdog detects crashes, wedges, and fatal errors, then restarts the full server process tree and rebuilds the pool.  `client.submit_resilient()` transparently retries innocent work and raises `ToxicTaskError` for the task that caused the failure so it is never retried blindly.

## Features

- **Declaration extraction** — `def`, `theorem`, `lemma`, `structure`, `inductive`, `class`, `instance`, `abbrev`, `example` with full parameter, type, and body information
- **Syntax tree parsing** — Access Lean's internal syntax tree via `parseDocument` and `debugSyntaxTree`
- **Proof goal inspection** — Query proof state at any source position (`$/lean/plainGoal`)
- **Diagnostics** — Real-time compiler errors and warnings (`textDocument/publishDiagnostics`)

## Architecture

```

  ┌──────────────────────────────────┐    ┌───────────────────────────┐
  │       Python Client (pyleaner)   │    │       Lean 4 side         │
  │                                  │    │                           │
  │  ┌──────────┐                    │    │  ┌─────────────────────┐  │
  │  │ Watchdog ├────────────────────┼────┼──┤ lake serve          │  │
  │  └────┬─────┘  monitor & restart │    │  │  ├─ lean --server   │  │
  │       │                          │    │  │  └─ lean --worker   │  │
  │  ┌────┴─────┐                    │    │  └─────────────────────┘  │
  │  │LspClient │                    │    │                           │
  │  └────┬─────┘                    │    │  ┌─────────────────────┐  │
  │       │              RPC call    │    │  │ LeanLspExtension    │  │
  │  ┌────┴─────┐────────────────────┼────┼──┤ • extractDecls      │  │
  │  │WorkerPool│                    │    │  │ • parseDocument     │  │
  │  │ • router │                    │    │  │ • plainGoal         │  │
  │  │ • workers│                    │    │  │ • diagnostics       │  │
  │  └──────────┘                    │    │  └─────────────────────┘  │
  └──────────────────────────────────┘    └───────────────────────────┘
```

## Installation

### 1. Clone the repository

PyLeaner contains **both** the Lean extension and the Python client in a single repo.

```bash
git clone https://github.com/jzshischolar/PyLeaner
```


### 2. Lean Extension

Add the cloned directory as a Lake dependency in your Lean project:

```lean
-- lakefile.lean
require PyLeaner from "/path/to/PyLeaner"
```


Or with `lakefile.toml`:

```toml
# lakefile.toml
[[require]]
name = "PyLeaner"
path = "/path/to/PyLeaner"
```


Then in your Lean files:

```lean
import LeanLspExtension
```


### 3. Python Client

```bash
pip install /path/to/PyLeaner
```


## Quick Start

Run the bundled demo to see PyLeaner in action:

```bash
pip install /path/to/PyLeaner
python examples/demo.py                   # all three demos
python examples/demo.py --declarations    # extract declarations
python examples/demo.py --diagnostics     # compiler diagnostics
python examples/demo.py --proof-goal      # inspect proof state
```


## Tests

Tests and fault-reproduction scripts live outside the runtime package in
`test/`:

```bash
pytest test
python test/test_all_methods.py
python test/test_recovery_integration.py
python test/test_watchdog_e2e.py
```

The integration and end-to-end scripts require a working Lean toolchain and
start real `lake serve` processes.


Or use the API directly:

```python
from pyleaner import LspClient

# 1. Start server + handshake
client = LspClient(server_cmd=["lake", "serve"], cwd="/path/to/lean/project")
client.connect()

# 2. Load a file
with open("path/to/PyLeaner/examples/HasParams.lean") as f:
    content = f.read()
pool = client.create_pool(text=content, size=1)

# 3. Extract declarations
result = pool.extract_declarations(text=content)
for decl in result["decls"]:
    print(f"{decl['kind']} {decl['name']}")

# 4. Get diagnostics
diags = pool.get_diagnostics(text=content)

# 5. Get proof goal
goals = pool.get_proof_goal(text=content, position={"line": 10, "character": 4})

# 6. Cleanup
client.shutdown()
client.exit()
```


## How It Works

### Task dispatch via WorkerPool

All operations go through the **WorkerPool**. You never call a Worker directly — the pool routes each task to the least-busy worker and returns the result. This keeps the API simple and avoids race conditions on shared documents.

```

you  →  pool.extract_declarations(text=...)
         →  router picks least-busy worker
              →  worker queues the task
                   →  worker runs didChange + RPC call atomically
                        →  result returned to you
```


### Atomic content change + information retrieval

Each task registered in `Worker.process_funcs` encapsulates a **content change and the corresponding information retrieval as a single atomic operation**. For example, `get_proof_goal` internally calls `_didchange` to push the latest content to Lean, then immediately calls `$/lean/plainGoal` to fetch goals — all within the same worker thread:

- **`extract_declarations`** — didChange → RPC `LeanLspExtension.extractDeclarations`
- **`get_diagnostics`** — didChange → wait for publishDiagnostics notification
- **`get_proof_goal`** — didChange → LSP `$/lean/plainGoal`

> ⚠️ Never call didChange separately and then query proof goals or declarations in a different task — the document version might have changed in between, and you'll get stale or mismatched results. Always use the pool methods that combine both steps atomically.

### Adding new tasks

New task types must be registered in `Worker.process_funcs` (in `worker.py`) and then exposed as a convenience method on `WorkerPool` (in `pool.py`). The pool method should accept the needed parameters and delegate to `self._submit(task_type, kwargs)`.

```python
# worker.py
self.process_funcs = {
    ...
    "my_new_task": self.my_new_task,
}

def my_new_task(self, ...):
    self._didchange(text, content_range)
    return self._submit_rpc(pos, "LeanLspExtension.myMethod", params)

# pool.py
def my_new_task(self, text, ...):
    """Docstring."""
    return self._submit("my_new_task", {"text": text, ...})
```

## Crash Resilience

A built-in **Watchdog** monitors the Lean server and auto-recovers from
crashes, silent wedges, and fatal errors.  The caller uses
`client.submit_resilient()` — a drop-in replacement that transparently
retries on the revived server and raises `ToxicTaskError` for the task
that caused the failure (so it is never retried blindly).

Key design points:

- **Three detection layers**: fatal stderr (instant), task deadline (120 s), process death (20 s poll)
- **Full process-tree restart**: kills `lake` + `lean --server` + `lean --worker`, then rebuilds the pool.  No orphaned Lean processes.
- **Process isolation**: `lean --server` runs in its own session — terminal signals (SIGINT/SIGHUP) don't propagate to it.
- **Orphan prevention**: six exit paths all converge on `_kill_process_tree` or `/proc` orphan scanning, plus a cron fallback every 4 hours.
- **Worker gating**: uninitialized workers are skipped by the router; `create_pool` raises if all workers fail.

## API Reference

### LspClient

Main client for communicating with the Lean 4 LSP server.

| Method | Description |
|--------|-------------|
| `connect()` | Start server + initialize handshake (replaces start/initialize/initialized) |
| `create_pool(text, uri?, size?)` | Create worker pool + load file content |
| `submit_resilient(task_type, kwargs)` | Submit a task with transparent crash/wedge recovery |
| `start()` | Start the LSP server subprocess (isolated session) |
| `initialize(root_uri)` | Send LSP initialize request |
| `initialized()` | Send initialized notification |
| `shutdown()` | Send shutdown request |
| `exit()` | Kill full process tree + cleanup (no orphans) |

### Exceptions

| Exception | Meaning |
|-----------|---------|
| `ToxicTaskError(task_type, reason, input_text)` | This task caused the server to crash/wedge — **do not retry** |
| `ServiceUnavailable` | Internal retryable signal — the server is restarting; transparently retried by `submit_resilient` |

### WorkerPool

| Method | Description |
|--------|-------------|
| `extract_declarations(text, ...)` | Extract all declarations with structured params |
| `get_diagnostics(text, ...)` | Get Lean compiler diagnostics |
| `get_proof_goal(text, position, ...)` | Get proof goal state at a position |
| `ping()` | RPC round-trip test |
| `echo(message)` | Echo test with parameters |

### DeclarationInfo

```json
{
  "kind": "def",
  "name": "add",
  "paramsText": "(x : Nat) (y : Nat)",
  "params": [
    {"name": "x", "type": "Nat", "binderKind": "explicit"},
    {"name": "y", "type": "Nat", "binderKind": "explicit"}
  ],
  "typeText": "Nat",
  "bodyText": "x + y",
  "fullText": "def add (x : Nat) (y : Nat) : Nat := x + y",
  "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 43}},
  "hasError": false,
  "errorMessage": null
}
```


### ParamInfo

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string \| null` | Parameter name |
| `type` | `string \| null` | Type annotation |
| `binderKind` | `string` | One of: `explicit`, `implicit`, `strictImplicit`, `instance` |

## Documentation

- [Configuration Reference](docs/CONFIGURATION.md) — All configurable parameters
- [Declaration Syntax Trees](docs/DECLARATION_SYNTAX_TREES.md) — How Lean syntax maps to extraction fields

## Examples

See the `examples/` directory for Lean files that demonstrate supported declaration types:

- `Simple.lean` — Basic def/theorem/example
- `HasParams.lean` — Explicit, implicit, and instance parameters
- `BodyTest.lean` — Multi-line bodies, match expressions, complex types
- `ComprehensiveTypes.lean` — All supported declaration types

## Requirements

- **Lean 4**: `v4.25.0-rc2` (specified in `lean-toolchain`)
- **Python**: ≥ 3.12
- **Mathlib**: **Not required.** PyLeaner core only depends on the Lean standard library. Import Mathlib only if your project already uses it

## License

MIT
