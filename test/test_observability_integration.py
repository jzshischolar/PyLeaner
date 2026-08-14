#!/usr/bin/env python3
"""Live Lean smoke test for correlated execution events."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pyleaner import LspClient, fingerprint_text  # noqa: E402


SOURCE = """import LeanLspExtension
namespace ObservabilitySmoke
theorem observed : True := by trivial
end ObservabilitySmoke
"""


def main() -> int:
    events = []
    client = LspClient(
        server_cmd=["lake", "serve"],
        cwd=str(PROJECT_ROOT),
        event_sink=events.append,
    )
    try:
        client.connect(timeout=300)
        client.create_pool(text=SOURCE, size=1)
        result = client.submit_resilient(
            "extract_declarations",
            {"text": SOURCE, "content_range": {}},
            request_id="observability-smoke-request",
            context={"operation_id": "observability-smoke"},
        )
        assert result["success"] is True

        correlated = [
            event
            for event in events
            if event.request_id == "observability-smoke-request"
        ]
        kinds = [event.kind for event in correlated]
        assert kinds == [
            "task_submitted",
            "task_assigned",
            "task_started",
            "task_succeeded",
        ], kinds
        task_ids = {event.task_id for event in correlated}
        assert len(task_ids) == 1
        assert None not in task_ids
        assert {
            event.source_fingerprint for event in correlated
        } == {fingerprint_text(SOURCE)}
        success = correlated[-1]
        assert success.outcome == "success"
        assert success.details["diagnostic_count"] == 0
        assert success.details["result_fingerprint"].startswith("sha256:")
        assert success.environment_fingerprint is not None
        assert success.environment_fingerprint.startswith("sha256:")
        print("observability integration passed:", success.as_dict())
        return 0
    finally:
        client.exit()


if __name__ == "__main__":
    raise SystemExit(main())
