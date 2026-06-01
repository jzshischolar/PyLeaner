# PyLeaner

A Python interface to the Lean 4 kernel — designed for AI–Lean interactive automated theorem proving and related research. PyLeaner provides a production-ready bridge between Python and Lean's internals.

> **PyLeaner** consists of a **Lean 4 LSP extension** (for kernel-level RPC capabilities) and a **Python client** (for communication, worker pool management, and load balancing).

## Highlights

1. **Lightweight API.** The Python client exposes a clean, minimal interface. File versioning, message management, and RPC session keep-alive are all handled automatically behind the scenes — focus on your research, not the plumbing.

2. **Extensible, deep kernel access.** Unlike existing Python–Lean bridges that are limited to basic LSP operations, PyLeaner supports Lean's native RPC extension mechanism. You can call into Lean's kernel arbitrarily — syntax tree parsing (done), proof goal extraction (done), diagnostic extraction (done), definition equivalence checking (planned), and more.

3. **Transparent concurrency.** A built-in worker pool manages multiple concurrent LSP server instances with load balancing and dynamic routing. Concurrency is fully transparent to the user — no manual scheduling or thread management required.

## Features

- **Structured Declaration Extraction** — Parse `def`, `theorem`, `lemma`, `structure`, `inductive`, `class`, `instance`, `abbrev`, `example` with full parameter, type, and body information
- **Tactic Execution** — Execute tactics and get proof states programmatically
- **Diagnostics** — Get real-time error and warning information from the Lean compiler
- **Worker Pool** — Manage multiple LSP server instances with load balancing for parallel processing
- **Mathlib Compatible** — PyLeaner does **not** depend on Mathlib itself, but fully supports Mathlib projects. Simply `import Mathlib` in your target file and PyLeaner's syntax-tree-based extraction works with any Lean 4 library out of the box

## Architecture

```
┌──────────────────┐     LSP/RPC      ┌──────────────────────────┐
│   Python Client  │ ◄──────────────► │   Lean 4 LSP Extension   │
│   (pyleaner)     │                  │   (LeanLspExtension)     │
│                  │                  │                          │
│  • LspClient     │                  │  • extractDeclarations   │
│  • WorkerPool    │                  │  • parseDocument         │
│  • RPC_session   │                  │  • Diagnostics           │
└──────────────────┘                  └──────────────────────────┘
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

## API Reference

### LspClient

Main client for communicating with the Lean 4 LSP server.

| Method | Description |
|--------|-------------|
| `connect()` | Start server + initialize handshake (replaces start/initialize/initialized) |
| `create_pool(text, uri?, size?)` | Create worker pool + load file content |
| `start()` | Start the LSP server subprocess |
| `initialize(root_uri)` | Send LSP initialize request |
| `initialized()` | Send initialized notification |
| `shutdown()` | Send shutdown request |
| `exit()` | Send exit notification |

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
