"""Lean 4.32 declaration-shape coverage that complements the migration fixture."""

from __future__ import annotations

import os

import pytest

from pyleaner import LspClient


PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 999, "character": 0},
}
SOURCE = """import LeanLspExtension
namespace Lean432DeclarationContract
universe u v

private def allBinders
    {α : Type u} ⦃β : Type v⦄ [inst : Inhabited α] (x : α) : α :=
  x

theorem namedTheorem : True := by trivial
abbrev NamedAbbrev := Nat
example : True := by trivial

def unicodeBody : String :=
  "😀-Lean-π"

end Lean432DeclarationContract
"""


def _lsp_slice(source: str, range_: dict) -> str:
    """Slice source using UTF-16 LSP character offsets."""

    lines = source.splitlines(keepends=True)

    def index_for_utf16(line: str, units: int) -> int:
        consumed = 0
        for index, char in enumerate(line):
            if consumed == units:
                return index
            consumed += len(char.encode("utf-16-le")) // 2
            if consumed > units:
                raise AssertionError("range splits a UTF-16 surrogate pair")
        if consumed == units:
            return len(line)
        raise AssertionError("LSP character offset is outside the line")

    start = range_["start"]
    end = range_["end"]
    start_index = index_for_utf16(lines[start["line"]], start["character"])
    end_index = index_for_utf16(lines[end["line"]], end["character"])
    if start["line"] == end["line"]:
        return lines[start["line"]][start_index:end_index]
    pieces = [lines[start["line"]][start_index:]]
    pieces.extend(lines[start["line"] + 1 : end["line"]])
    pieces.append(lines[end["line"]][:end_index])
    return "".join(pieces)


@pytest.fixture(scope="module")
def declarations():
    client = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    client.connect()
    client.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-lean-432-declaration-contract/",
        init_text=SOURCE,
    )
    try:
        result = client.worker_pool.extract_declarations(SOURCE, FULL_RANGE)
        assert result["success"] is True
        assert not [d for d in result["diagnostics"] if d.get("severity") == 1]
        yield result["decls"]
    finally:
        try:
            client.shutdown()
            client.exit()
        except Exception:
            pass


def test_all_named_declaration_kinds_and_anonymous_example_are_present(declarations):
    observed = [(decl["name"], decl["kind"]) for decl in declarations]
    assert observed == [
        ("allBinders", "def"),
        ("namedTheorem", "theorem"),
        ("NamedAbbrev", "abbrev"),
        (None, "example"),
        ("unicodeBody", "def"),
    ]


def test_all_binder_kinds_and_private_modifier_are_preserved(declarations):
    declaration = next(d for d in declarations if d["name"] == "allBinders")
    assert declaration["modifiers"] == ["private"]
    assert declaration["params"] == [
        {"name": "α", "type": "Type u", "binderKind": "implicit"},
        {"name": "β", "type": "Type v", "binderKind": "strictImplicit"},
        {"name": "inst", "type": "Inhabited α", "binderKind": "instance"},
        {"name": "x", "type": "α", "binderKind": "explicit"},
    ]


def test_unicode_declaration_and_body_ranges_are_exact(declarations):
    declaration = next(d for d in declarations if d["name"] == "unicodeBody")
    assert _lsp_slice(SOURCE, declaration["range"]) == declaration["fullText"]
    assert _lsp_slice(SOURCE, declaration["bodyRange"]) == declaration["bodyText"]
    assert declaration["bodyText"] == '"😀-Lean-π"'
