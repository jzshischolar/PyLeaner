from __future__ import annotations

import queue
from pyleaner import LspClient

from pyleaner import (
    LeanExecutionEvent,
    Task,
    fingerprint_lean_environment,
    fingerprint_text,
    fingerprint_value,
)
from pyleaner.observability import emit_safely


def test_text_fingerprint_is_exact_and_stable() -> None:
    assert fingerprint_text("theorem t : True := by trivial") == fingerprint_text(
        "theorem t : True := by trivial"
    )
    assert fingerprint_text("x") != fingerprint_text("x\n")


def test_value_fingerprint_normalizes_mapping_key_order() -> None:
    assert fingerprint_value({"b": 2, "a": 1}) == fingerprint_value(
        {"a": 1, "b": 2}
    )


def test_event_is_json_compatible() -> None:
    event = LeanExecutionEvent(
        kind="task_succeeded",
        request_id="req",
        task_id="task",
        details={"diagnostic_count": 0},
    )
    value = event.as_dict()
    assert value["schema_version"] == "pyleaner.execution-event.v1"
    assert value["details"] == {"diagnostic_count": 0}


def test_sink_failure_never_escapes() -> None:
    def broken(_: LeanExecutionEvent) -> None:
        raise RuntimeError("observer is down")

    assert emit_safely(broken, "task_started") is not None


def test_task_correlation_fields_are_optional() -> None:
    result_q: queue.Queue = queue.Queue()
    task: Task = {"task_type": "ping", "result_q": result_q, "kwargs": {}}
    assert task["task_type"] == "ping"


def test_environment_fingerprint_tracks_toolchain_lake_and_local_imports(
    tmp_path,
) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n")
    (tmp_path / "lake-manifest.json").write_text('{"packages": []}\n')
    (tmp_path / "lakefile.lean").write_text("package Test\n")
    (tmp_path / "Ext").mkdir()
    (tmp_path / "Ext" / "Basic.lean").write_text("def basic : Nat := 1\n")
    (tmp_path / "Ext.lean").write_text("import Ext.Basic\ndef ext : Nat := basic\n")
    source = "import Ext\nexample : Nat := ext\n"

    first = fingerprint_lean_environment(tmp_path, ["lake", "serve"], source=source)
    second = fingerprint_lean_environment(tmp_path, ["lake", "serve"], source=source)
    assert first == second
    assert first.lean_toolchain == "leanprover/lean4:v4.32.2"
    assert first.imports == ("Ext", "Ext.Basic")
    assert set(first.file_fingerprints) == {
        "lean-toolchain", "lake-manifest.json", "lakefile.lean",
        "Ext.lean", "Ext/Basic.lean",
    }

    (tmp_path / "Ext" / "Basic.lean").write_text("def basic : Nat := 2\n")
    changed = fingerprint_lean_environment(tmp_path, ["lake", "serve"], source=source)
    assert changed.fingerprint != first.fingerprint


def test_environment_fingerprint_tracks_server_command(tmp_path) -> None:
    first = fingerprint_lean_environment(tmp_path, ["lake", "serve"])
    second = fingerprint_lean_environment(tmp_path, ["lean", "--server"])
    assert first.fingerprint != second.fingerprint


def test_observation_context_is_nested_and_restored() -> None:
    client = LspClient(["lake", "serve"])
    assert client.current_observation_context() == {}

    with client.observation_context(action_id="action-1", node_id="node-1"):
        assert client.current_observation_context() == {
            "action_id": "action-1", "node_id": "node-1"}
        with client.observation_context(generation_id="generation-1"):
            assert client.current_observation_context() == {
                "action_id": "action-1",
                "node_id": "node-1",
                "generation_id": "generation-1",
            }
        assert client.current_observation_context() == {
            "action_id": "action-1", "node_id": "node-1"}

    assert client.current_observation_context() == {}
