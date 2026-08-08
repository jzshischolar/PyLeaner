"""Portable RPC/AST contract frozen before the Lean 4.32.2 migration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pyleaner import LspClient


PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = Path(__file__).parent / "fixtures" / "lean_4_25_rpc_contract.json"
FULL_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 999, "character": 0},
}
SOURCE = """import LeanLspExtension
namespace MigrationContract
universe u

class System (α : Type u) where
  (x y : α)
  [eqv : BEq α]
  proof : x = y
  goal : Prop

inductive Shade where
  | light
  | dark
  deriving DecidableEq, Repr

local instance selected [sys : System Nat] : BEq Nat :=
  sys.eqv

def objective [sys : System Nat] : Prop :=
  sys.goal

end MigrationContract
"""


def _source_slice(source: str, range_: dict) -> str:
    lines = source.splitlines(keepends=True)
    start = range_["start"]
    end = range_["end"]
    if start["line"] == end["line"]:
        return lines[start["line"]][start["character"] : end["character"]]
    pieces = [lines[start["line"]][start["character"] :]]
    pieces.extend(lines[start["line"] + 1 : end["line"]])
    pieces.append(lines[end["line"]][: end["character"]])
    return "".join(pieces)


@pytest.fixture(scope="module")
def client():
    value = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    value.connect()
    value.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-version-contract/",
        init_text=SOURCE,
    )
    try:
        yield value
    finally:
        try:
            value.shutdown()
            value.exit()
        except Exception:
            pass


def test_lean_migration_preserves_portable_rpc_contract(client):
    expected = json.loads(FIXTURE.read_text())
    result = client.worker_pool.extract_declarations(SOURCE, FULL_RANGE)
    assert result["success"] is True
    assert not [item for item in result["diagnostics"] if item.get("severity") == 1]
    decls = result["decls"]

    assert [[item["name"], item["kind"]] for item in decls] == expected[
        "declarationOrder"
    ]
    by_name = {item["name"]: item for item in decls}

    system = by_name["System"]
    assert system["levelParams"] == expected["system"]["levelParams"]
    assert system["params"] == expected["system"]["params"]
    assert [
        {
            key: field[key]
            for key in (
                "name",
                "typeText",
                "binderKind",
                "isClass",
                "isProp",
                "isPropType",
            )
        }
        for field in system["fields"]
    ] == expected["system"]["fields"]

    derived = by_name["Shade"]
    generated_instances = [
        item for item in derived["generatedDeclarations"] if item["isInstance"]
    ]
    assert derived["environmentDeltaComplete"] is expected["derived"][
        "environmentDeltaComplete"
    ]
    assert sorted(item["instanceClassName"] for item in generated_instances) == (
        expected["derived"]["generatedInstanceClasses"]
    )

    selected = by_name["selected"]
    primary_instance = next(
        item for item in selected["environmentDelta"] if item["isPrimary"]
    )
    actual_selected = {
        "modifiers": selected["modifiers"],
        "params": selected["params"],
        "typeText": selected["typeText"],
        "bodyText": selected["bodyText"],
        "instanceClassName": primary_instance["instanceClassName"],
        "instanceScope": primary_instance["instanceScope"],
        "instancePriority": primary_instance["instancePriority"],
    }
    assert actual_selected == expected["selected"]

    objective = by_name["objective"]
    assert objective["typeText"] == expected["objective"]["typeText"]
    assert objective["bodyText"] == expected["objective"]["bodyText"]
    assert set(expected["objective"]["typeReferencesContain"]).issubset(
        objective["typeReferences"]
    )
    assert set(expected["objective"]["valueReferencesContain"]).issubset(
        objective["valueReferences"]
    )

    for declaration in decls:
        assert _source_slice(SOURCE, declaration["range"]) == declaration["fullText"]
        if declaration["bodyRange"] is not None:
            assert _source_slice(SOURCE, declaration["bodyRange"]) == declaration[
                "bodyText"
            ]


def test_lean_migration_preserves_error_diagnostics(client):
    broken = """import LeanLspExtension
structure Broken where
  x : Nat
  [inst : BEq
"""
    result = client.worker_pool.extract_declarations(broken, FULL_RANGE)
    declaration = next(item for item in result["decls"] if item["name"] == "Broken")
    errors = [item for item in result["diagnostics"] if item.get("severity") == 1]
    assert errors
    assert all("range" in item and item.get("message") for item in errors)
    assert declaration["hasError"] is True
    assert declaration["environmentDeltaComplete"] is False
    assert [item["name"] for item in declaration["fields"]] == ["x", "inst"]
