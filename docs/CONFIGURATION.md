# Configuration Reference

This document describes all configurable parameters in PyLeaner.

## Server Configuration

### Server Command

The Lean LSP server is started as a subprocess. By default, PyLeaner uses `lake serve`.

```python
from pyleaner import LspClient

client = LspClient(
    server_cmd=["lake", "serve"],  # Default
    cwd="/path/to/lean/project"     # Required: your Lean project root
)
```

You can also use `lean --server` directly:

```python
client = LspClient(
    server_cmd=["lean", "--server"],
    cwd="/path/to/lean/project"
)
```

### Watchdog and Worker Memory Guards

On Linux systems with a systemd user manager and cgroup v2 memory controller,
each Lean worker can be placed in an isolated transient scope:

```python
client = LspClient(
    server_cmd=["lake", "serve"],
    cwd="/path/to/lean/project",
    worker_memory_high_bytes=12 * 1024**3,
    worker_memory_max_bytes=16 * 1024**3,
    watchdog_memory_poll_interval=1.0,
)
```

Both memory limits must be provided together and the soft limit must be below
the hard limit. `worker_memory_high_bytes` is observed by the dedicated
watchdog process using anonymous RSS. `worker_memory_max_bytes` becomes the
scope's kernel-enforced `MemoryMax`; swap is disabled for that scope so an
offending worker cannot evade the ceiling by forcing system-wide swap
thrashing. If the worker is killed, PyLeaner attributes the event to its
current task, rebuilds the server and pool online, and lets innocent tasks
retry. Omitting both values disables cgroup guards while retaining process
liveness and deadline monitoring.

### Lean Version

The required Lean version is specified in the `lean-toolchain` file at the project root. For example:

```
leanprover/lean4:v4.25.0-rc2
```

PyLeaner follows the same version as your project's `lean-toolchain` file.

## Worker Pool

### Pool Size

The worker pool manages multiple LSP server instances for parallel processing.

```python
client.initialize_worker_pool(
    size=3,              # Number of workers (default: 3)
    init_uri=uri,        # Initial file URI
    init_text=content    # Initial file content
)
```

### Task Timeout

When submitting tasks, you can set a timeout for waiting for results:

```python
result = result_queue.get(timeout=30.0)  # Default: 30 seconds
```

## RPC Session Management

### Keep-Alive Interval

PyLeaner automatically sends keep-alive messages to prevent RPC session expiration.

| Parameter | Value | Description |
|-----------|-------|-------------|
| `KEEP_ALIVE_INTERVAL` | 10 seconds | Interval between keep-alive pings |
| Lean server session timeout | 30 seconds | Server-side RPC session expiration |

The keep-alive runs automatically in a background daemon thread managed by `KeepAliveManager`.

## API Configuration

### extract_declarations

Extracts structured declaration information from a Lean file.

```python
result = worker.extract_declarations(
    text=source_code,
    content_range={
        "start": {"line": 0, "character": 0},
        "end": {"line": 999, "character": 0}
    }
)
```

Returns an object with `success`, a `decls` array, and a top-level
`diagnostics` array collected from the same document update. Each
`DeclarationInfo` in `decls` has fields:

- `modifiers`: optional normalized declaration modifiers. In particular,
  `local instance` and `scoped instance` are returned as ordinary
  `kind: "instance"` declarations with `modifiers: ["local"]` or
  `modifiers: ["scoped"]`.
- `kind`: Declaration type ("def", "theorem", "lemma", etc.)
- `name`: Declaration name (null for `example`)
- `paramsText`: Raw parameter text (backward compatible)
- `params`: Structured parameter array (`ParamInfo`)
- `typeText`: Return type text
- `bodyText`: Body text
- `bodyRange`: Exact source range for `bodyText`, when available
- `fields`: Structured source and elaborated metadata for `structure`/`class`
- `environmentDelta`: Every elaborated constant introduced by this source
  command, including the primary declaration, constructors, projections,
  recursors, equation declarations, and `deriving` output
- `generatedDeclarations`: The non-primary subset of `environmentDelta`
- `environmentDeltaComplete`: `true` only when a preceding environment
  snapshot exists and the source command has no attributed error
- `fullText`: Complete declaration source text
- `range`: LSP range in document
- `hasError`: Whether the declaration has errors
- `errorMessage`: Error message if applicable

The declaration-local `hasError`/`errorMessage` fields are best-effort command
attribution and can omit document errors outside or ambiguously spanning the
command range. Use the top-level `diagnostics` array whenever complete Lean
error reporting is required. Existing callers that only read `success` and
`decls` remain compatible.

Each environment-delta item includes its elaborated name, coarse constant kind,
type, universe parameters, type/body references, source declaration and command
range. `isInstance`, `instancePriority`, and `instanceScope` are read from
Lean's instance extension. For registered instances, `instanceTargetText` is
the target after introducing provider parameters and `instanceClassName` is its
Lean-validated head class; `instanceParameters` preserves those introduced
parameters and their binder kinds. Class and instance registration are also
reported in `attributes`. Attribute extensions in Lean may use extension-specific storage,
so `attributes` is not claimed to enumerate every arbitrary user attribute.

### get_proof_goal

Gets the proof goal state at a specific position.

```python
result = worker.get_proof_goal(
    text=source_code,
    content_range=range,
    position={"line": 5, "character": 10}
)
```

Returns:
- `diagnostics`: Array of error diagnostics
- `proof_goal`: Current goal state at the position

### get_diagnostics

Gets diagnostics (errors/warnings) after a content change.

```python
diagnostics = worker.get_diagnostics(
    text=source_code,
    content_range=range
)
```

Returns an array of diagnostics with severity filtering (errors only by default).
