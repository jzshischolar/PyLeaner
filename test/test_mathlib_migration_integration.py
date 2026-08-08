"""Optional integration checks against the matching external Mathlib project."""

from __future__ import annotations

import os

import pytest

from pyleaner import LspClient


MATHLIB_PROJECT = os.environ.get("PYLEANER_MATHLIB_PROJECT")
SOURCE = """import Mathlib
import LeanLspExtension
namespace MathlibMigrationContract

private lemma localLemma {α : Type} (x : α) : x = x := by rfl

end MathlibMigrationContract
"""


@pytest.mark.skipif(
    not MATHLIB_PROJECT,
    reason="set PYLEANER_MATHLIB_PROJECT to run the external Mathlib contract",
)
def test_mathlib_lemma_macro_retains_declaration_kind_and_metadata():
    client = LspClient(server_cmd=["lake", "serve"], cwd=MATHLIB_PROJECT)
    client.connect()
    client.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-mathlib-migration-contract/",
        init_text=SOURCE,
    )
    try:
        result = client.worker_pool.extract_declarations(SOURCE)
        errors = [d for d in result["diagnostics"] if d.get("severity") == 1]
        assert not errors
        assert [(d["name"], d["kind"]) for d in result["decls"]] == [
            ("localLemma", "lemma")
        ]
        declaration = result["decls"][0]
        assert declaration["modifiers"] == ["private"]
        assert declaration["params"] == [
            {"name": "α", "type": "Type", "binderKind": "implicit"},
            {"name": "x", "type": "α", "binderKind": "explicit"},
        ]
        assert declaration["typeText"] == "x = x"
    finally:
        try:
            client.shutdown()
            client.exit()
        except Exception:
            pass
