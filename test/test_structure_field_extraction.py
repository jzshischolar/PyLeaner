"""Integration tests for structured structure/class field extraction."""

from __future__ import annotations

import os

import pytest

from pyleaner import LspClient


PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 999, "character": 0},
}

VALID_SOURCE = """import LeanLspExtension
namespace InfrastructureFixture
universe u v

abbrev Proposition := Prop

structure Fields (α : Type u) where
  (x y : α)
  {hidden : α}
  [inst : BEq α]
  [nonempty : Nonempty α]
  law : True
  proofGoal : Prop
  parenthesizedGoal : (Prop)
  aliasedGoal : Proposition
  sortGoal : Sort 0
  data : List α

class Operations (α : Type v) where
  op : α → α
  op_law : ∀ x, op x = op x

inductive DerivedColor where
  | red
  | blue
  deriving DecidableEq, Repr

inductive DerivedWrapper (α : Type) where
  | wrap (value : α)
  deriving Repr

structure WithCtor where mk ::
  -- The body range must include constructor syntax and comments.
  value : Nat

structure FunctionFields (α : Type) where
  mkBEq : Nat → BEq Nat
  nonemptyFor : Nat → Nonempty α

structure _root_.PrivateCollision where
  value : Nat

namespace Nested
private structure PrivateCollision where
  value : BEq Nat

structure _root_.RootLocated where
  value : BEq Nat
end Nested

class Marker where
  value : Nat

instance markerInstance : Marker :=
  -- This comment belongs to the instance body.
  { value := 1 }

instance markerInstanceWhere : Marker where
  -- This comment belongs to the where-style instance body.
  value := 2

local instance localMarker [inst : Marker] : BEq Nat :=
  inferInstance

scoped instance scopedMarker : Inhabited Marker :=
  ⟨{ value := 3 }⟩

noncomputable def noncomputableValue : Nat :=
  Classical.choice (show Nonempty Nat from inferInstance)

@[simp] def attributedValue : Nat := 4

def use (n : Nat) : Nat :=
  n + 1

def commentedBody : Nat :=
  -- This comment belongs to the declaration body.
  7

def dependsOnUse : Nat :=
  use commentedBody

def predecessor : Nat → Nat
  | 0 => 0
  | n + 1 => n

axiom hiddenAssumption : True
opaque hiddenValue : Nat := 3

end InfrastructureFixture
"""


def _source_slice(source: str, range_: dict) -> str:
    """Slice ASCII/BMP fixture text using an LSP range."""
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
    c = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    c.start()
    c.initialize(f"file://{PROJECT_PATH}")
    c.initialized()
    c.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-structure-fields/",
        init_text=VALID_SOURCE,
    )
    try:
        yield c
    finally:
        try:
            c.shutdown()
            c.exit()
        except Exception:
            pass


def _extraction(client: LspClient, source: str) -> dict:
    result = client.worker_pool.extract_declarations(source, FULL_RANGE)
    assert result["success"] is True
    assert isinstance(result["diagnostics"], list)
    return result


def _declarations(client: LspClient, source: str) -> list[dict]:
    return _extraction(client, source)["decls"]


def test_structure_fields_include_source_and_environment_metadata(client):
    decls = _declarations(client, VALID_SOURCE)
    structure = next(d for d in decls if d["name"] == "Fields")
    fields = {field["name"]: field for field in structure["fields"]}

    assert list(fields) == [
        "x",
        "y",
        "hidden",
        "inst",
        "nonempty",
        "law",
        "proofGoal",
        "parenthesizedGoal",
        "aliasedGoal",
        "sortGoal",
        "data",
    ]
    assert fields["x"]["typeText"] == "α"
    assert fields["x"]["binderKind"] == "explicit"
    assert fields["x"]["range"] == fields["y"]["range"]
    assert fields["hidden"]["binderKind"] == "implicit"

    assert fields["inst"]["typeText"] == "BEq α"
    assert fields["inst"]["binderKind"] == "instance"
    assert fields["inst"]["projectionName"] == "InfrastructureFixture.Fields.inst"
    assert fields["inst"]["isClass"] is True
    assert fields["inst"]["isProp"] is False
    assert fields["inst"]["className"] == "BEq"
    assert fields["nonempty"]["isClass"] is True
    assert fields["nonempty"]["isProp"] is True
    assert fields["nonempty"]["className"] == "Nonempty"
    assert fields["law"]["isClass"] is False
    assert fields["law"]["isProp"] is True
    assert fields["law"]["isPropType"] is False
    # A proposition-valued data field stores a proposition; it is not itself a
    # proof. ``isPropType`` separately records that its type reduces to Prop.
    assert fields["proofGoal"]["typeText"] == "Prop"
    assert fields["proofGoal"]["isProp"] is False
    assert fields["proofGoal"]["isPropType"] is True
    assert fields["parenthesizedGoal"]["isProp"] is False
    assert fields["parenthesizedGoal"]["isPropType"] is True
    assert fields["aliasedGoal"]["isProp"] is False
    assert fields["aliasedGoal"]["isPropType"] is True
    assert fields["sortGoal"]["isProp"] is False
    assert fields["sortGoal"]["isPropType"] is True
    assert fields["data"]["projectionName"] == "InfrastructureFixture.Fields.data"
    assert fields["data"]["isPropType"] is False

    assert _source_slice(VALID_SOURCE, structure["bodyRange"]) == structure["bodyText"]
    for field in fields.values():
        assert field["name"] in _source_slice(VALID_SOURCE, field["range"])


def test_class_fields_and_non_structure_compatibility(client):
    decls = _declarations(client, VALID_SOURCE)
    cls = next(d for d in decls if d["name"] == "Operations")
    with_ctor = next(d for d in decls if d["name"] == "WithCtor")
    definition = next(d for d in decls if d["name"] == "use")
    dependent_definition = next(d for d in decls if d["name"] == "dependsOnUse")
    equation_definition = next(d for d in decls if d["name"] == "predecessor")
    axiom_declaration = next(d for d in decls if d["name"] == "hiddenAssumption")
    opaque_declaration = next(d for d in decls if d["name"] == "hiddenValue")

    assert cls["kind"] == "class"
    assert cls["levelParams"] == ["v"]
    assert next(d for d in decls if d["name"] == "Fields")["levelParams"] == ["u"]
    assert definition["levelParams"] == []
    assert {
        "InfrastructureFixture.use",
        "InfrastructureFixture.commentedBody",
    }.issubset(set(dependent_definition["valueReferences"]))
    assert "Nat" in dependent_definition["typeReferences"]
    assert [f["name"] for f in cls["fields"]] == ["op", "op_law"]
    assert cls["fields"][1]["isProp"] is True
    assert cls["fields"][1]["projectionName"] == "InfrastructureFixture.Operations.op_law"

    assert [f["name"] for f in with_ctor["fields"]] == ["value"]
    assert _source_slice(VALID_SOURCE, with_ctor["bodyRange"]) == with_ctor["bodyText"]

    assert definition["fields"] is None
    assert _source_slice(VALID_SOURCE, definition["bodyRange"]) == definition["bodyText"]
    assert equation_definition["fields"] is None
    assert _source_slice(
        VALID_SOURCE, equation_definition["bodyRange"]
    ) == equation_definition["bodyText"]
    assert axiom_declaration["kind"] == "axiom"
    assert axiom_declaration["fields"] is None
    assert opaque_declaration["kind"] == "opaque"
    assert opaque_declaration["fields"] is None


def test_declaration_modifiers_and_local_instance_extraction(client):
    decls = _declarations(client, VALID_SOURCE)
    local_instance = next(d for d in decls if d["name"] == "localMarker")
    scoped_instance = next(d for d in decls if d["name"] == "scopedMarker")
    noncomputable = next(d for d in decls if d["name"] == "noncomputableValue")
    attributed = next(d for d in decls if d["name"] == "attributedValue")

    assert local_instance["kind"] == "instance"
    assert local_instance["modifiers"] == ["local"]
    assert local_instance["params"] == [{
        "name": "inst", "type": "Marker", "binderKind": "instance"
    }]
    assert local_instance["typeText"] == "BEq Nat"
    assert local_instance["bodyText"] == "inferInstance"
    assert _source_slice(
        VALID_SOURCE, local_instance["bodyRange"]
    ) == local_instance["bodyText"]

    assert scoped_instance["kind"] == "instance"
    assert scoped_instance["modifiers"] == ["scoped"]
    assert noncomputable["modifiers"] == ["noncomputable"]
    assert attributed["modifiers"] == ["attribute"]


def test_environment_delta_audits_generated_and_instance_declarations(client):
    decls = _declarations(client, VALID_SOURCE)
    derived = next(d for d in decls if d["name"] == "DerivedColor")
    local_instance = next(d for d in decls if d["name"] == "localMarker")
    global_instance = next(d for d in decls if d["name"] == "markerInstance")

    assert derived["environmentDeltaComplete"] is True
    delta = derived["environmentDelta"]
    generated = derived["generatedDeclarations"]
    assert len([item for item in delta if item["isPrimary"]]) == 1
    assert {item["name"] for item in generated} == {
        item["name"] for item in delta if not item["isPrimary"]
    }
    assert all(item["sourceDeclaration"] == "DerivedColor" for item in delta)
    assert all(item["sourceRange"] == derived["range"] for item in delta)
    assert {"constructor", "recursor"}.issubset({item["kind"] for item in generated})

    deriving_instances = [
        item for item in generated
        if item["isInstance"] and (
            "DecidableEq" in item["typeText"] or "Repr" in item["typeText"]
        )
    ]
    assert len(deriving_instances) == 2
    assert all(item["attributes"] == ["instance"] for item in deriving_instances)
    assert all(item["instancePriority"] == 1000 for item in deriving_instances)
    assert all(item["instanceScope"] == "global" for item in deriving_instances)
    assert {item["instanceClassName"] for item in deriving_instances} == {
        # DecidableEq reduces to a provider whose ultimate target is Decidable.
        "Decidable", "Repr"
    }
    assert all(item["instanceTargetText"] for item in deriving_instances)
    assert all(item["typeReferences"] for item in deriving_instances)
    assert all(item["valueReferences"] for item in deriving_instances)

    local_primary = next(
        item for item in local_instance["environmentDelta"] if item["isPrimary"]
    )
    global_primary = next(
        item for item in global_instance["environmentDelta"] if item["isPrimary"]
    )
    assert local_primary["isInstance"] is True
    assert local_primary["instanceScope"] == "local"
    assert local_primary["instancePriority"] == 1000
    assert local_primary["instanceClassName"] == "BEq"
    assert local_primary["instanceTargetText"] == "BEq Nat"
    assert global_primary["isInstance"] is True
    assert global_primary["instanceScope"] == "global"
    assert global_primary["instancePriority"] == 1000

    wrapper = next(d for d in decls if d["name"] == "DerivedWrapper")
    wrapper_repr = next(
        item for item in wrapper["generatedDeclarations"]
        if item["isInstance"] and item["instanceClassName"] == "Repr"
    )
    assert wrapper_repr["instanceParameters"] == [
        {"name": "_p0", "type": "Type", "binderKind": "implicit"},
        {"name": "_p1", "type": "Repr _p0", "binderKind": "instance"},
    ]
    assert wrapper_repr["instanceTargetText"] == "Repr (DerivedWrapper _p0)"


def test_incomplete_structure_returns_partial_syntax_fields(client):
    source = """import LeanLspExtension
structure Broken where
  x : Nat
  [inst : BEq
"""
    extraction = _extraction(client, source)
    decls = extraction["decls"]
    broken = next(d for d in decls if d["name"] == "Broken")

    assert any(diag.get("severity") == 1 for diag in extraction["diagnostics"])
    assert broken["hasError"] is True
    assert broken["environmentDeltaComplete"] is False
    assert [f["name"] for f in broken["fields"]] == ["x", "inst"]
    assert broken["fields"][0]["typeText"] == "Nat"
    assert broken["fields"][0]["projectionName"] is None
    assert broken["fields"][0]["isClass"] is None
    assert broken["fields"][0]["isProp"] is None
    assert broken["fields"][0]["isPropType"] is None


def test_incomplete_deriving_reports_diagnostics_and_incomplete_delta(client):
    source = """import LeanLspExtension
inductive BrokenDerived where
  | value
  deriving
"""
    extraction = _extraction(client, source)
    broken = next(d for d in extraction["decls"] if d["name"] == "BrokenDerived")
    assert any(diag.get("severity") == 1 for diag in extraction["diagnostics"])
    assert broken["hasError"] is True
    assert broken["environmentDeltaComplete"] is False
    assert isinstance(broken["generatedDeclarations"], list)


def test_ranges_are_recomputed_after_document_change(client):
    shifted_source = "\n\n" + VALID_SOURCE
    decls = _declarations(client, shifted_source)
    structure = next(d for d in decls if d["name"] == "Fields")
    expected_structure_line = shifted_source.splitlines().index(
        "structure Fields (α : Type u) where"
    )

    assert structure["range"]["start"]["line"] == expected_structure_line
    assert structure["fields"][0]["range"]["start"]["line"] == (
        expected_structure_line + 1
    )
    assert _source_slice(shifted_source, structure["bodyRange"]) == structure["bodyText"]


def test_function_valued_fields_are_not_overopened(client):
    decls = _declarations(client, VALID_SOURCE)
    structure = next(d for d in decls if d["name"] == "FunctionFields")
    fields = {field["name"]: field for field in structure["fields"]}

    # The residual field type is `Nat → BEq Nat`, not its `BEq Nat` codomain.
    assert fields["mkBEq"]["isClass"] is False
    assert fields["mkBEq"]["isProp"] is False
    assert fields["mkBEq"]["isPropType"] is False
    assert fields["mkBEq"]["className"] is None
    # A Pi whose codomain is Prop is proof-valued, but is not itself a class
    # application even when the codomain (`Nonempty α`) is a class.
    assert fields["nonemptyFor"]["isClass"] is False
    assert fields["nonemptyFor"]["isProp"] is True
    assert fields["nonemptyFor"]["isPropType"] is False
    assert fields["nonemptyFor"]["className"] is None


def test_structure_resolution_handles_private_and_root_names(client):
    decls = _declarations(client, VALID_SOURCE)
    public = next(d for d in decls if d["name"] == "_root_.PrivateCollision")
    private = next(d for d in decls if d["name"] == "PrivateCollision")

    assert public["fields"][0]["projectionName"] == "PrivateCollision.value"
    assert private["fields"][0]["projectionName"] != public["fields"][0]["projectionName"]
    assert "_private" in private["fields"][0]["projectionName"]
    assert private["fields"][0]["isClass"] is True
    assert private["fields"][0]["className"] == "BEq"

    root = next(d for d in decls if d["name"] == "_root_.RootLocated")
    assert root["fields"][0]["projectionName"] == "RootLocated.value"
    assert root["fields"][0]["isClass"] is True


def test_comment_prefixed_and_simple_instance_body_ranges_are_exact(client):
    decls = _declarations(client, VALID_SOURCE)
    definition = next(d for d in decls if d["name"] == "commentedBody")
    instance = next(d for d in decls if d["name"] == "markerInstance")
    where_instance = next(d for d in decls if d["name"] == "markerInstanceWhere")

    assert definition["bodyText"].startswith("-- This comment belongs")
    assert _source_slice(VALID_SOURCE, definition["bodyRange"]) == definition["bodyText"]
    assert instance["bodyText"].startswith("-- This comment belongs")
    assert _source_slice(VALID_SOURCE, instance["bodyRange"]) == instance["bodyText"]
    assert where_instance["bodyText"].startswith("where")
    assert (
        _source_slice(VALID_SOURCE, where_instance["bodyRange"])
        == where_instance["bodyText"]
    )
