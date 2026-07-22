"""TargetSymbolResolver — the deterministic-first cascade keystone.

Mandated bulletproof (async):
  1. A failing-test STACK TRACE resolves accurately to its enclosing symbol.
  2. A resolved function's LOCAL HELPER is pulled into the Symbol Cluster.
  3. A ``@classmethod`` decorator is FULLY captured in the node boundary anchor.
  4. An ambiguous target FAILS CLOSED (empty → standard route preserved).

Plus: goal-keyword resolution, explicit-name precedence, and end-to-end parity
with the real ``extract_target_chunk`` (the chunk the swarm receives includes
the decorator, proving the anchor is honored downstream).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.chunked_generation import (
    extract_target_chunk,
)
from backend.core.ouroboros.governance.target_symbol_resolver import (
    METHOD_GOAL_KEYWORD,
    METHOD_STACK_TRACE,
    METHOD_UNRESOLVED,
    resolve_target_symbols,
)

# A file with: a module fn that calls a local helper, and a class with a
# @classmethod. Line numbers are load-bearing for the stack-trace test.
_SRC = '''"""Enterprise module."""


def _topological_sort(graph):
    order = []
    _visit(graph, order)
    return order


def _visit(graph, order):
    for node in graph:
        order.append(node)


def unrelated(x):
    return x + 1


class SagaBuilder:
    @classmethod
    def build(cls, spec):
        """Build a saga."""
        return cls._compose(spec)

    @staticmethod
    def _compose(spec):
        return list(spec)
'''


def _lineno(substr: str) -> int:
    for i, line in enumerate(_SRC.splitlines(), start=1):
        if substr in line:
            return i
    raise AssertionError(f"{substr!r} not found")


# ---------------------------------------------------------------------------
# (1) Deterministic stack-trace mapping
# ---------------------------------------------------------------------------


async def test_stack_trace_resolves_to_enclosing_symbol() -> None:
    # A traceback whose deepest in-file frame lands inside _topological_sort.
    line = _lineno("_visit(graph, order)")  # inside _topological_sort's body
    frames = [
        'File "/repo/other.py", line 10, in test_it',
        f'File "/repo/saga.py", line {line}, in _topological_sort',
    ]
    result = resolve_target_symbols(
        source=_SRC, file_path="backend/saga.py",
        traceback_frames=frames, source_loci=("backend/saga.py",),
    )
    assert result.method == METHOD_STACK_TRACE
    assert result.confidence == 1.0
    assert "_topological_sort" in result.primary


async def test_stack_trace_line_maps_deterministically_not_by_name() -> None:
    # Frame func label is wrong ("<lambda>") but the LINE is authoritative.
    line = _lineno("order = []")
    frames = [f'File "/x/saga.py", line {line}, in <lambda>']
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py", traceback_frames=frames,
    )
    assert result.method == METHOD_STACK_TRACE
    assert result.primary == ("_topological_sort",)


# ---------------------------------------------------------------------------
# (2) Call-Graph Expansion — Symbol Clustering
# ---------------------------------------------------------------------------


async def test_local_helper_pulled_into_cluster() -> None:
    line = _lineno("_visit(graph, order)")
    frames = [f'File "/x/saga.py", line {line}, in _topological_sort']
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py", traceback_frames=frames,
    )
    # _topological_sort calls _visit (a local sibling) → clustered.
    assert "_topological_sort" in result.primary
    assert "_visit" in result.cluster
    # The unrelated function is NOT dragged in.
    assert "unrelated" not in result.symbol_names
    # Cluster members are flagged.
    visit = next(s for s in result.symbols if s.name == "_visit")
    assert visit.is_cluster_member is True


async def test_cluster_can_be_disabled() -> None:
    line = _lineno("_visit(graph, order)")
    frames = [f'File "/x/saga.py", line {line}, in _topological_sort']
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py", traceback_frames=frames,
        expand_cluster=False,
    )
    assert result.cluster == ()
    assert result.symbol_names == ("_topological_sort",)


# ---------------------------------------------------------------------------
# (3) Decorator-Aware Anchoring
# ---------------------------------------------------------------------------


async def test_classmethod_decorator_captured_in_anchor() -> None:
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py", goal="fix SagaBuilder.build",
    )
    build = next(s for s in result.symbols if s.name.endswith("build"))
    # The anchor starts at the @classmethod line, NOT the def line.
    deco_line = _lineno("@classmethod")
    def_line = _lineno("def build(cls, spec)")
    assert build.start_line == deco_line
    assert build.start_line < def_line
    assert any("classmethod" in d for d in build.decorators)

    # End-to-end: the chunk the swarm actually receives includes the decorator.
    chunk = extract_target_chunk(_SRC, "saga.py", build.name)
    assert chunk is not None
    assert "@classmethod" in (chunk.source_code or "")


# ---------------------------------------------------------------------------
# (4) Fail-Closed on ambiguity
# ---------------------------------------------------------------------------


async def test_ambiguous_goal_fails_closed() -> None:
    # A goal that names nothing in the file and shares no meaningful tokens.
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py",
        goal="please improve the overall quality and performance somehow",
    )
    assert result.resolved is False
    assert result.method == METHOD_UNRESOLVED
    assert result.symbols == ()


async def test_no_trace_no_goal_fails_closed() -> None:
    result = resolve_target_symbols(source=_SRC, file_path="saga.py")
    assert result.resolved is False
    assert result.method == METHOD_UNRESOLVED


async def test_unparseable_source_fails_closed() -> None:
    result = resolve_target_symbols(
        source="def broken( : \n  pass", file_path="x.py",
        goal="fix broken", traceback_frames=['File "x.py", line 1, in broken'],
    )
    assert result.resolved is False


# ---------------------------------------------------------------------------
# Goal-keyword resolution (deterministic heuristic)
# ---------------------------------------------------------------------------


async def test_explicit_name_in_goal_resolves() -> None:
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py",
        goal="the _topological_sort function returns nodes in the wrong order",
    )
    assert result.method == METHOD_GOAL_KEYWORD
    assert result.confidence == 1.0
    assert "_topological_sort" in result.primary


async def test_goal_token_overlap_resolves() -> None:
    # No exact name; 'topological' + 'sort' tokens overlap _topological_sort.
    result = resolve_target_symbols(
        source=_SRC, file_path="saga.py",
        goal="the topological sort is unstable",
    )
    assert result.method == METHOD_GOAL_KEYWORD
    assert "_topological_sort" in result.primary
