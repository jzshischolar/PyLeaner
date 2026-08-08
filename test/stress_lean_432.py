"""Long-running concurrent migration stress harness.

This file is intentionally not named ``test_*.py``: run it explicitly for
release qualification so the normal unit suite stays fast.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import statistics
import subprocess
import time

from pyleaner import LspClient
from pyleaner.watchdog import _collect_descendants


DEFAULT_PROJECT_PATH = Path(__file__).resolve().parents[1]
FULL_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 999, "character": 0},
}
SOURCE = """import LeanLspExtension
namespace Lean432Stress
structure Payload where
  value : Nat
  label : String

def transform (payload : Payload) : Nat := payload.value + 1
end Lean432Stress
"""


def _rss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    return 0


def _tree_rss_bytes(root_pid: int) -> int:
    return sum(_rss_bytes(pid) for pid in [root_pid, *_collect_descendants(root_pid)])


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
    return ordered[index]


def run(
    *,
    project_path: Path,
    operations: int,
    workers: int,
    duration_seconds: float,
) -> dict:
    client = LspClient(server_cmd=["lake", "serve"], cwd=str(project_path))
    client.connect()
    client.initialize_worker_pool(
        size=workers,
        init_uri="file:///tmp/pyleaner-lean-432-stress/",
        init_text=SOURCE,
    )
    started = time.monotonic()
    latencies: dict[str, list[float]] = {
        "diagnostics": [],
        "extraction": [],
        "search": [],
    }
    samples = [_tree_rss_bytes(client.process.pid)]

    def perform(index: int) -> tuple[str, float]:
        kind = ("diagnostics", "extraction", "search")[index % 3]
        before = time.monotonic()
        if kind == "diagnostics":
            diagnostics = client.worker_pool.get_diagnostics(SOURCE, FULL_RANGE)
            assert not [item for item in diagnostics if item.get("severity") == 1]
        elif kind == "extraction":
            result = client.worker_pool.extract_declarations(SOURCE, FULL_RANGE)
            assert result["success"] is True
            assert [item["name"] for item in result["decls"]] == [
                "Payload", "transform"
            ]
        else:
            result = client.worker_pool.search_declarations(
                SOURCE,
                "Lean432Stress.transform",
                max_results=3,
                fuzzy=False,
                content_range=FULL_RANGE,
            )
            assert result["success"] is True
            assert result["candidates"][0]["name"] == "Lean432Stress.transform"
        return kind, time.monotonic() - before

    completed = 0
    try:
        batch_size = max(workers * 4, 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            while completed < operations or (
                duration_seconds > 0 and time.monotonic() - started < duration_seconds
            ):
                remaining = max(operations - completed, 0)
                count = batch_size if duration_seconds > 0 else min(batch_size, remaining)
                futures = [executor.submit(perform, completed + i) for i in range(count)]
                for future in concurrent.futures.as_completed(futures):
                    kind, latency = future.result()
                    latencies[kind].append(latency)
                completed += count
                samples.append(_tree_rss_bytes(client.process.pid))
    finally:
        try:
            client.shutdown()
            client.exit()
        except Exception:
            pass

    elapsed = time.monotonic() - started
    return {
        "leanVersion": subprocess.run(
            ["lake", "env", "lean", "--version"],
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "workers": workers,
        "operations": completed,
        "elapsedSeconds": elapsed,
        "operationsPerSecond": completed / elapsed,
        "latencySeconds": {
            kind: {
                "count": len(values),
                "median": statistics.median(values) if values else 0.0,
                "p95": _percentile(values, 0.95),
            }
            for kind, values in latencies.items()
        },
        "rssBytes": {
            "initial": samples[0],
            "maximum": max(samples),
            "final": samples[-1],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT_PATH)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run(
        project_path=args.project.resolve(),
        operations=args.operations,
        workers=args.workers,
        duration_seconds=args.duration_seconds,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
