"""Integration tests for generic elaborated-environment declaration search."""

from __future__ import annotations

import os

import pytest

from pyleaner import LspClient


PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = """import LeanLspExtension

namespace SearchFixture
def LocalDeclarationForSearch (n : Nat) : Nat := n
end SearchFixture
"""


@pytest.fixture(scope="module")
def client():
    value = LspClient(server_cmd=["lake", "serve"], cwd=PROJECT_PATH)
    value.start()
    value.initialize(f"file://{PROJECT_PATH}")
    value.initialized()
    value.initialize_worker_pool(
        size=1,
        init_uri="file:///tmp/pyleaner-declaration-search/",
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


def test_search_repairs_segment_initial_casing_and_returns_real_signatures(client):
    result = client.worker_pool.search_declarations(
        SOURCE, "searchFixture.LocalDeclarationForSearch", max_results=5
    )
    assert result["success"] is True
    assert result["query"] == "searchFixture.LocalDeclarationForSearch"
    assert isinstance(result["diagnostics"], list)
    assert result["candidates"]
    first = result["candidates"][0]
    assert first["name"] == "SearchFixture.LocalDeclarationForSearch"
    assert first["kind"] == "def"
    assert first["score"] == 0
    assert "Nat" in first["typeText"]


def test_search_is_bounded_and_does_not_require_a_known_name(client):
    result = client.worker_pool.search_declarations(
        SOURCE, "LocalDeclarationForSearcg", max_results=1, fuzzy=True
    )
    assert len(result["candidates"]) <= 1
    assert result["candidates"][0]["name"] == (
        "SearchFixture.LocalDeclarationForSearch"
    )
