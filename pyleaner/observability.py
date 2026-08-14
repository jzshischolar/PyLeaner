"""Optional, domain-neutral execution observability for PyLeaner.

The event API deliberately describes transport and worker lifecycle facts only.
Consumers may correlate these facts with their own domain objects, but PyLeaner
does not know about proof trees, rewards, datasets, or training runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence


EVENT_SCHEMA_VERSION = "pyleaner.execution-event.v1"
ENVIRONMENT_SCHEMA_VERSION = "pyleaner.environment-fingerprint.v1"


_IMPORT_LINE = re.compile(r"^\s*import\s+(.+?)\s*$")


@dataclass(frozen=True)
class LeanExecutionEvent:
    """One immutable task, worker, or recovery lifecycle observation."""

    kind: str
    request_id: Optional[str] = None
    task_id: Optional[str] = None
    task_type: Optional[str] = None
    worker_id: Optional[int] = None
    document_uri: Optional[str] = None
    source_fingerprint: Optional[str] = None
    environment_fingerprint: Optional[str] = None
    outcome: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at_ns: int = field(default_factory=time.time_ns)
    schema_version: str = EVENT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible shallow representation."""
        value = asdict(self)
        value["details"] = dict(self.details)
        return value


EventSink = Callable[[LeanExecutionEvent], None]


@dataclass(frozen=True)
class LeanEnvironmentFingerprint:
    """Static, reproducible identity of one Lean task environment."""

    fingerprint: str
    project_root: str
    server_command: tuple[str, ...]
    lean_toolchain: str | None
    imports: tuple[str, ...]
    file_fingerprints: Mapping[str, str]
    schema_version: str = ENVIRONMENT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_correlation_id() -> str:
    """Create an opaque correlation identifier without global mutable state."""
    return uuid.uuid4().hex


def fingerprint_text(text: str) -> str:
    """Fingerprint exact UTF-8 source text without retaining the text itself."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_value(value: Any) -> str:
    """Fingerprint a JSON-like result deterministically when possible."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        ).encode("utf-8")
    except Exception:
        encoded = repr(value).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def source_fingerprint_from_kwargs(kwargs: Mapping[str, Any]) -> Optional[str]:
    """Return the fingerprint of a task's source text, if it has one."""
    text = kwargs.get("text")
    return fingerprint_text(text) if isinstance(text, str) else None


def _imports(source: str) -> tuple[str, ...]:
    modules: list[str] = []
    for line in source.splitlines():
        match = _IMPORT_LINE.match(line)
        if match is None:
            continue
        import_text = match.group(1).split("--", 1)[0]
        for module in import_text.split():
            if module and module not in modules:
                modules.append(module)
    return tuple(modules)


def _local_module_path(root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (root / relative.with_suffix(".lean"), root / relative / "Main.lean")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _local_import_closure(
    root: Path, initial_imports: Sequence[str]
) -> tuple[tuple[str, ...], dict[str, str]]:
    pending = list(initial_imports)
    seen: set[str] = set()
    local_files: dict[str, str] = {}
    while pending:
        module = pending.pop(0)
        if module in seen:
            continue
        seen.add(module)
        path = _local_module_path(root, module)
        if path is None:
            continue
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        local_files[relative] = fingerprint_text(source)
        pending.extend(child for child in _imports(source) if child not in seen)
    return tuple(sorted(seen)), dict(sorted(local_files.items()))


def fingerprint_lean_environment(
    project_root: str | Path,
    server_command: Sequence[str],
    *,
    source: str = "",
) -> LeanEnvironmentFingerprint:
    """Fingerprint toolchain, Lake graph, local import closure, and command."""
    root = Path(project_root or ".").resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Lean project root does not exist: {root}")
    files: dict[str, str] = {}
    toolchain: str | None = None
    for name in ("lean-toolchain", "lake-manifest.json", "lakefile.lean", "lakefile.toml"):
        path = root / name
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        files[name] = fingerprint_text(content)
        if name == "lean-toolchain":
            toolchain = content.strip() or None
    imports, local_files = _local_import_closure(root, _imports(source))
    files.update(local_files)
    payload = {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "server_command": list(server_command),
        "lean_toolchain": toolchain,
        "imports": list(imports),
        "file_fingerprints": dict(sorted(files.items())),
    }
    digest = fingerprint_value(payload)
    return LeanEnvironmentFingerprint(
        fingerprint=digest,
        project_root=str(root),
        server_command=tuple(str(item) for item in server_command),
        lean_toolchain=toolchain,
        imports=imports,
        file_fingerprints=dict(sorted(files.items())),
    )


def emit_safely(
    sink: Optional[EventSink],
    kind: str,
    **fields: Any,
) -> Optional[LeanExecutionEvent]:
    """Emit an event without allowing observer failures to break Lean work."""
    if sink is None:
        return None
    event = LeanExecutionEvent(kind=kind, **fields)
    try:
        sink(event)
    except Exception:
        # Observability is optional. A failing user callback must never alter
        # task success, retry, or watchdog semantics.
        return event
    return event
