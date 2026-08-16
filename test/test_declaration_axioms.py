"""Integration tests for kernel-certified declaration axiom dependencies."""

from __future__ import annotations

import os

import pytest

from pyleaner import LspClient


PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = """import LeanLspExtension

namespace AxiomFixture
axiom ancestor : True
def helper : True := ancestor
theorem child : True := helper
theorem clean : True := True.intro
end AxiomFixture
"""


@pytest.fixture(scope="module")
def client():
    value = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    value.start()
    value.initialize(f"file://{PROJECT_PATH}")
    value.initialized()
    value.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-declaration-axioms/",
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


def test_declaration_axioms_crosses_opaque_theorem_and_local_helper(client):
    result = client.worker_pool.declaration_axioms(
        SOURCE, "AxiomFixture.child")
    assert result["success"] is True
    assert result["resolvedName"] == "AxiomFixture.child"
    assert "AxiomFixture.ancestor" in result["axioms"]
    assert not [
        item for item in result["diagnostics"]
        if int(item.get("severity", 1)) <= 1
    ]


def test_declaration_axioms_reports_clean_theorem(client):
    result = client.worker_pool.declaration_axioms(
        SOURCE, "AxiomFixture.clean")
    assert result["success"] is True
    assert "AxiomFixture.ancestor" not in result["axioms"]
    assert not [
        item for item in result["diagnostics"]
        if int(item.get("severity", 1)) <= 1
    ]
