"""Tests for the Adaptive Hybrid Reverse-Dependency Resolver (Task 5b).

Powers Gate (2) of the Iron Triad: "every test that transitively touches the
modified code". Built strictly TDD -- the cycle-armor tests (4, 5) are the
load-bearing proof that the transitive reverse closure is ITERATIVE, never
recursive.

AST-path tests build a real synthetic repo under ``tmp_path`` (real ``.py``
files, no parser mocks). Oracle-path tests use a light fake -- the real
``TheOracle`` is never imported or constructed here.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import reverse_dep_resolver as rdr
from backend.core.ouroboros.governance.reverse_dep_resolver import (
    ReverseDepGraphError,
    resolve_reverse_dependency_tests,
)


# ---------------------------------------------------------------------------
# Helpers -- synthetic repo construction
# ---------------------------------------------------------------------------

def _write(root, rel_path: str, body: str = "") -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Light Oracle fakes (NO real TheOracle import)
# ---------------------------------------------------------------------------

class _FakeNode:
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path

    def __hash__(self) -> int:
        return hash(self.file_path)

    def __eq__(self, other) -> bool:
        return isinstance(other, _FakeNode) and other.file_path == self.file_path


class _FakeBlast:
    def __init__(self, directly, transitively) -> None:
        self.directly_affected = set(directly)
        self.transitively_affected = set(transitively)


class _FakeOracle:
    """Configurable fake. ``nodes_by_file`` maps rel-path -> list[_FakeNode];
    ``blast`` is returned by every compute_blast_radius call. If
    ``raise_on_lookup`` is set, find_nodes_in_file raises it."""

    def __init__(self, nodes_by_file=None, blast=None, raise_on_lookup=None) -> None:
        self._nodes_by_file = nodes_by_file or {}
        self._blast = blast or _FakeBlast(set(), set())
        self._raise = raise_on_lookup

    def find_nodes_in_file(self, file_path: str):
        if self._raise is not None:
            raise self._raise
        return list(self._nodes_by_file.get(file_path, []))

    def compute_blast_radius(self, node_id, max_depth=10):
        return self._blast


# ---------------------------------------------------------------------------
# 1. Two-hop transitive (the case one-hop would MISS)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transitive_two_hop(tmp_path):
    _write(tmp_path, "a.py", "X = 1\n")
    _write(tmp_path, "b.py", "import a\n")
    _write(tmp_path, "tests/test_c.py", "import b\n")

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path)
    )

    assert "tests/test_c.py" in result


# ---------------------------------------------------------------------------
# 2. Direct one-hop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_one_hop(tmp_path):
    _write(tmp_path, "a.py", "X = 1\n")
    _write(tmp_path, "tests/test_a.py", "import a\n")

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path)
    )

    assert "tests/test_a.py" in result


# ---------------------------------------------------------------------------
# 3. Unrelated test excluded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unrelated_excluded(tmp_path):
    _write(tmp_path, "a.py", "X = 1\n")
    _write(tmp_path, "tests/test_a.py", "import a\n")
    _write(tmp_path, "tests/test_z.py", "import os\n")

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path)
    )

    assert "tests/test_z.py" not in result
    assert "tests/test_a.py" in result


# ---------------------------------------------------------------------------
# 4. CYCLE ARMOR -- a real circular import must terminate, never RecursionError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_armor_no_recursion(tmp_path):
    # a <-> b circular import
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "import a\n")
    _write(tmp_path, "tests/test_a.py", "import a\n")

    # Must RETURN (no hang, no RecursionError) and include the dependent test.
    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path)
    )

    assert "tests/test_a.py" in result


# ---------------------------------------------------------------------------
# 5. DEEP CHAIN -- 2000-deep import chain must not raise RecursionError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deep_chain_no_recursionerror(tmp_path):
    depth = 2000
    _write(tmp_path, "m0.py", "X = 1\n")
    for i in range(1, depth + 1):
        _write(tmp_path, f"m{i}.py", f"import m{i - 1}\n")
    _write(tmp_path, "tests/test_top.py", f"import m{depth}\n")

    result = await resolve_reverse_dependency_tests(
        ["m0.py"], repo_root=str(tmp_path)
    )

    # Proves the closure is iterative: a recursive walk would RecursionError.
    assert "tests/test_top.py" in result


# ---------------------------------------------------------------------------
# 6. Build failure -> fail-closed raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_error_raises(tmp_path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(ReverseDepGraphError):
        await resolve_reverse_dependency_tests(
            ["a.py"], repo_root=str(missing)
        )


# ---------------------------------------------------------------------------
# 7. Empty result is VALID, not an error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_result_is_not_error(tmp_path):
    # A changed file nobody imports and with no matching test_*.py.
    _write(tmp_path, "lonely.py", "X = 1\n")
    _write(tmp_path, "tests/test_unrelated.py", "import os\n")

    result = await resolve_reverse_dependency_tests(
        ["lonely.py"], repo_root=str(tmp_path)
    )

    assert isinstance(result, set)
    assert "tests/test_unrelated.py" not in result


# ---------------------------------------------------------------------------
# 8. Oracle warm path used, AST builder NOT invoked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oracle_warm_path_used(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "X = 1\n")

    node_a = _FakeNode("a.py")
    impacted_node = _FakeNode("tests/test_c.py")
    oracle = _FakeOracle(
        nodes_by_file={"a.py": [node_a]},
        blast=_FakeBlast(directly={impacted_node}, transitively=set()),
    )

    # Spy: the AST forward-graph builder must NOT run on the warm Oracle path.
    called = {"ast": False}
    real_builder = rdr._build_forward_import_graph

    def _spy(*args, **kwargs):
        called["ast"] = True
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(rdr, "_build_forward_import_graph", _spy)

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path), oracle=oracle
    )

    assert called["ast"] is False
    assert "tests/test_c.py" in result


# ---------------------------------------------------------------------------
# 9. Oracle cold (empty nodes) -> AST fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oracle_cold_falls_back_to_ast(tmp_path):
    _write(tmp_path, "a.py", "X = 1\n")
    _write(tmp_path, "b.py", "import a\n")
    _write(tmp_path, "tests/test_c.py", "import b\n")

    # Cold for this file -> find_nodes_in_file returns [].
    oracle = _FakeOracle(nodes_by_file={})

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path), oracle=oracle
    )

    # AST path ran and found the transitive result.
    assert "tests/test_c.py" in result


# ---------------------------------------------------------------------------
# 10. Oracle error -> swallowed -> AST fallback (Oracle never breaks Gate 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oracle_error_falls_back_to_ast(tmp_path):
    _write(tmp_path, "a.py", "X = 1\n")
    _write(tmp_path, "b.py", "import a\n")
    _write(tmp_path, "tests/test_c.py", "import b\n")

    oracle = _FakeOracle(raise_on_lookup=RuntimeError("oracle boom"))

    result = await resolve_reverse_dependency_tests(
        ["a.py"], repo_root=str(tmp_path), oracle=oracle
    )

    assert "tests/test_c.py" in result
