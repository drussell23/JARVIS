"""Proactive AST-Chunked Generation — big-file map-reduce.

Proves the "break it down + stitch back" primitive on the exact Saga scenario:
slice ONE function out of a big file (so DW gets a tiny payload), fix only that
chunk, stitch it back — with every other line preserved byte-for-byte and the
file still parsing.
"""

from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.governance.chunked_generation import (
    build_focused_prompt,
    extract_target_chunk,
    is_big_file,
    should_chunk,
    stitch_replacement,
)

# A realistic "big file": a class with the buggy _topological_sort plus a lot
# of unrelated padding so the whole thing is well over the chunk threshold.
_PADDING = "\n".join(
    f"    def _filler_{i}(self):\n"
    f"        '''unrelated method {i}'''\n"
    f"        return {i}\n"
    for i in range(200)
)

_BIG_FILE = f'''"""Saga apply strategy — a big module."""
import collections


class SagaApplyStrategy:
    """Big class."""

{_PADDING}

    def _topological_sort(self, repo_scope, edges):
        """Kahn's algorithm. Stable: alphabetical within same depth."""
        graph = collections.defaultdict(list)
        in_degree = {{r: 0 for r in repo_scope}}
        for dependent, dependency in edges:
            graph[dependency].append(dependent)
            in_degree[dependent] = in_degree.get(dependent, 0) + 1
        queue = collections.deque(sorted(r for r in repo_scope if in_degree.get(r, 0) == 0))
        result = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return result

    def _after(self):
        """A method AFTER the target — must be preserved."""
        return "sentinel-after"
'''

# The heapq fix DW would return for ONLY the function (dedented, as a model
# often emits — the stitcher must re-indent it to method nesting).
_HEAPQ_FIX = '''def _topological_sort(self, repo_scope, edges):
    """Kahn's algorithm. Stable: alphabetical within same depth."""
    import heapq
    graph = collections.defaultdict(list)
    in_degree = {r: 0 for r in repo_scope}
    for dependent, dependency in edges:
        graph[dependency].append(dependent)
        in_degree[dependent] = in_degree.get(dependent, 0) + 1
    heap = sorted(r for r in repo_scope if in_degree.get(r, 0) == 0)
    heapq.heapify(heap)
    result = []
    while heap:
        node = heapq.heappop(heap)
        result.append(node)
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                heapq.heappush(heap, neighbor)
    return result'''


def test_big_file_gate_and_symbol_routing() -> None:
    assert is_big_file(_BIG_FILE) is True          # well over threshold
    assert is_big_file("def f(): pass") is False
    assert should_chunk(_BIG_FILE, "_topological_sort") is True
    assert should_chunk(_BIG_FILE, None) is False  # no symbol → whole-file


def test_extract_only_the_target_function() -> None:
    chunk = extract_target_chunk(_BIG_FILE, "saga_apply_strategy.py", "_topological_sort")
    assert chunk is not None
    assert chunk.name == "_topological_sort"
    # The extracted payload is TINY vs the whole file (the whole point).
    assert "deque" in chunk.source_code
    assert "_filler_" not in chunk.source_code
    assert chunk.end_line > chunk.start_line

    # A focused prompt carries only the function, not the 600-line file.
    prompt = build_focused_prompt(chunk, "use a heapq for lexicographic order")
    assert "_topological_sort" in prompt
    assert "_filler_" not in prompt


def test_slice_fix_and_stitch_back_preserves_everything() -> None:
    """The map-reduce end-to-end: extract → (DW returns heapq fix) → stitch."""
    chunk = extract_target_chunk(_BIG_FILE, "saga_apply_strategy.py", "_topological_sort")
    assert chunk is not None

    stitched = stitch_replacement(_BIG_FILE, chunk, _HEAPQ_FIX)
    assert stitched is not None

    # 1. The whole file still parses.
    tree = ast.parse(stitched)
    assert tree is not None

    # 2. ONLY the target function changed — heapq in, deque out (in that fn).
    assert "heapq" in stitched
    # The fix removed the deque usage from the target function.
    assert "collections.deque" not in stitched

    # 3. Everything else is preserved byte-for-byte.
    assert "sentinel-after" in stitched          # the method after the target
    assert stitched.count("_filler_") == _BIG_FILE.count("_filler_")  # all padding
    assert stitched.startswith('"""Saga apply strategy')

    # 4. The re-indent landed the function at method nesting (4 spaces).
    assert "\n    def _topological_sort(self, repo_scope, edges):" in stitched

    # 5. The fixed function is actually correct (execute the stitched module).
    ns = {}
    exec(compile(stitched, "stitched", "exec"), ns)  # noqa: S102 — test-only
    inst = ns["SagaApplyStrategy"].__new__(ns["SagaApplyStrategy"])
    order = ns["SagaApplyStrategy"]._topological_sort(
        inst, ("A", "B", "W", "X"), (("W", "B"), ("X", "A")),
    )
    assert order == ["A", "B", "W", "X"]         # lexicographic-within-depth


def test_stitch_out_of_range_is_none() -> None:
    class _Bad:
        start_line = 9999
        end_line = 10000
    assert stitch_replacement(_BIG_FILE, _Bad(), "x") is None


def test_chunking_disabled_routes_whole_file(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_DW_BIG_FILE_CHUNKING_ENABLED", "false")
    assert should_chunk(_BIG_FILE, "_topological_sort") is False
