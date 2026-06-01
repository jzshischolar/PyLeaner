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

Returns an array of `DeclarationInfo` with fields:
- `kind`: Declaration type ("def", "theorem", "lemma", etc.)
- `name`: Declaration name (null for `example`)
- `paramsText`: Raw parameter text (backward compatible)
- `params`: Structured parameter array (`ParamInfo`)
- `typeText`: Return type text
- `bodyText`: Body text
- `fullText`: Complete declaration source text
- `range`: LSP range in document
- `hasError`: Whether the declaration has errors
- `errorMessage`: Error message if applicable

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
