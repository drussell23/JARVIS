"""Slice 11.1 regression spine — shared AST slicer module.

Pins:
  §1 Module surface — exports + types unchanged from pre-extraction
  §2 ChunkType enum values pinned
  §3 RelevanceReason enum values pinned
  §4 ASTChunker behavior pinned for representative cases:
     - module header extraction (imports + docstring)
     - top-level function extraction
     - class with methods + skeleton
     - target_names filter
     - call extraction
     - cyclomatic complexity
     - decorator inclusion (constructor knob)
     - graceful syntax-error fallback
  §5 Backward-compat — smart_context re-exports unchanged so every
     `from backend.core.smart_context import ASTChunker, CodeChunk`
     keeps working.
  §6 Authority — ast_slicer.py top-level imports stay stdlib-only;
     no orchestrator/policy/iron_gate imports.

The chunker's behavior MUST be byte-identical to the pre-extraction
implementation. Slice 11.2 will start consuming it from a new code
path; this slice is refactor-only.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import ast_slicer
from backend.core.ouroboros.governance.ast_slicer import (
    ASTChunker,
    ChunkPriority,
    ChunkType,
    CodeChunk,
    RelevanceReason,
    TokenCounterProtocol,
)


# ---------------------------------------------------------------------------
# Stub TokenCounter — we don't need tiktoken for behavior tests; the
# chunker only calls .count(text) so any object satisfying the
# protocol works.
# ---------------------------------------------------------------------------


class _StubTokenCounter:
    """Heuristic counter — ~4 chars per token + newlines."""

    def count(self, text: str) -> int:
        return len(text) // 4 + text.count("\n")


# ---------------------------------------------------------------------------
# §1 — Module surface
# ---------------------------------------------------------------------------


def test_module_exports_pinned() -> None:
    expected = {
        "ASTChunker", "ChunkPriority", "ChunkType", "CodeChunk",
        "RelevanceReason", "TokenCounterProtocol",
    }
    assert set(ast_slicer.__all__) == expected


def test_token_counter_protocol_is_protocol() -> None:
    """TokenCounterProtocol is duck-typed — anything with .count() is
    a valid argument."""
    counter = _StubTokenCounter()
    assert hasattr(counter, "count")
    # The chunker accepts it without isinstance check (Protocol).
    chunker = ASTChunker(counter)
    assert chunker._token_counter is counter  # noqa: SLF001


def test_module_top_level_imports_stdlib_only() -> None:
    """ast_slicer.py must NOT import from orchestrator / iron_gate /
    policy — preserves the no-authority-coupling contract."""
    src = Path(ast_slicer.__file__).read_text(encoding="utf-8")
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for needle in forbidden:
        assert needle not in src, (
            f"ast_slicer must not import {needle!r} — pure extraction"
        )


def test_module_top_level_imports_only_allowed_stdlib() -> None:
    """Top-level imports MUST be stdlib + typing.Protocol only."""
    module = ast.parse(
        Path(ast_slicer.__file__).read_text(encoding="utf-8"),
    )
    allowed = {
        "ast", "asyncio", "logging", "dataclasses", "enum",
        "pathlib", "typing", "__future__",
    }
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed, (
                    f"top-level import {alias.name} not in stdlib allowlist"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in allowed, (
                f"top-level from-import {node.module} not in stdlib allowlist"
            )


# ---------------------------------------------------------------------------
# §2 — ChunkType enum
# ---------------------------------------------------------------------------


def test_chunk_type_values_pinned() -> None:
    assert ChunkType.FUNCTION.value == "function"
    assert ChunkType.METHOD.value == "method"
    assert ChunkType.CLASS.value == "class"
    assert ChunkType.CLASS_SKELETON.value == "class_skeleton"
    assert ChunkType.MODULE_HEADER.value == "module_header"
    assert ChunkType.VARIABLE.value == "variable"
    assert ChunkType.DECORATOR.value == "decorator"


def test_chunk_type_enum_has_no_extras() -> None:
    """Pinning the exact set ensures a future PR doesn't silently add
    a chunk type that breaks downstream switch statements."""
    assert {m.value for m in ChunkType} == {
        "function", "method", "class", "class_skeleton",
        "module_header", "variable", "decorator",
    }


# ---------------------------------------------------------------------------
# §3 — RelevanceReason enum
# ---------------------------------------------------------------------------


def test_relevance_reason_values_pinned() -> None:
    assert RelevanceReason.DIRECT_MATCH.value == "direct_match"
    assert RelevanceReason.DEPENDENCY.value == "dependency"
    assert RelevanceReason.STRUCTURAL.value == "structural"
    assert RelevanceReason.SEMANTIC.value == "semantic"
    assert RelevanceReason.BLAST_RADIUS.value == "blast_radius"


# ---------------------------------------------------------------------------
# §4 — CodeChunk dataclass
# ---------------------------------------------------------------------------


def test_codechunk_equality_by_chunk_id() -> None:
    a = CodeChunk(
        chunk_id="x", chunk_type=ChunkType.FUNCTION, name="foo",
        qualified_name="foo", file_path=Path("/tmp/a.py"),
        start_line=1, end_line=2, source_code="def foo(): pass",
    )
    b = CodeChunk(
        chunk_id="x", chunk_type=ChunkType.METHOD, name="bar",
        qualified_name="C.bar", file_path=Path("/tmp/b.py"),
        start_line=10, end_line=20, source_code="different",
    )
    # Same chunk_id → equal even though every other field differs.
    assert a == b
    assert hash(a) == hash(b)


def test_codechunk_priority_queue_ordering() -> None:
    """``__lt__`` returns True when self has HIGHER relevance — so
    sorting yields highest-relevance first (priority queue)."""
    high = CodeChunk(
        chunk_id="h", chunk_type=ChunkType.FUNCTION, name="h",
        qualified_name="h", file_path=Path("/tmp/h.py"),
        start_line=1, end_line=2, source_code="x",
        relevance_score=0.9,
    )
    low = CodeChunk(
        chunk_id="l", chunk_type=ChunkType.FUNCTION, name="l",
        qualified_name="l", file_path=Path("/tmp/l.py"),
        start_line=1, end_line=2, source_code="x",
        relevance_score=0.1,
    )
    sorted_chunks = sorted([low, high])
    assert sorted_chunks[0].chunk_id == "h"


def test_chunk_priority_namedtuple_shape() -> None:
    chunk = CodeChunk(
        chunk_id="x", chunk_type=ChunkType.FUNCTION, name="x",
        qualified_name="x", file_path=Path("/tmp/x.py"),
        start_line=1, end_line=2, source_code="x",
    )
    cp = ChunkPriority(negative_score=-0.5, chunk_id="x", chunk=chunk)
    assert cp.negative_score == -0.5
    assert cp.chunk_id == "x"
    assert cp.chunk is chunk


# ---------------------------------------------------------------------------
# §5 — ASTChunker behavior
# ---------------------------------------------------------------------------


SAMPLE_SOURCE = textwrap.dedent('''
    """Sample module for chunker tests."""
    import os
    import sys
    from typing import List


    def top_level_function(x: int) -> int:
        """Return x plus one."""
        return x + 1


    async def async_function(name: str) -> None:
        """Doc."""
        if name:
            print(name)
        for i in range(10):
            pass


    class SampleClass:
        """A sample class."""

        def method_one(self, value: int) -> int:
            return value * 2

        @staticmethod
        def static_method() -> str:
            return "hello"

        async def async_method(self) -> None:
            try:
                pass
            except Exception:
                pass
''').lstrip()


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE_SOURCE, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_chunker_extracts_module_header(sample_file: Path) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    headers = [c for c in chunks if c.chunk_type == ChunkType.MODULE_HEADER]
    assert len(headers) == 1
    h = headers[0]
    # Three imports → all captured in the header chunk's imports set.
    assert any("import os" in imp for imp in h.imports)
    assert any("import sys" in imp for imp in h.imports)
    assert any("from typing import List" in imp for imp in h.imports)


@pytest.mark.asyncio
async def test_chunker_extracts_top_level_function(
    sample_file: Path,
) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    fns = [
        c for c in chunks
        if c.chunk_type == ChunkType.FUNCTION
        and c.name == "top_level_function"
    ]
    assert len(fns) == 1
    fn = fns[0]
    assert "def top_level_function(x: int) -> int" in fn.signature
    assert fn.docstring == "Return x plus one."
    assert "x + 1" in fn.source_code


@pytest.mark.asyncio
async def test_chunker_extracts_async_function(
    sample_file: Path,
) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    afns = [c for c in chunks if c.name == "async_function"]
    assert len(afns) == 1
    assert afns[0].signature.startswith("async def async_function")


@pytest.mark.asyncio
async def test_chunker_extracts_class_with_methods(
    sample_file: Path,
) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    cls_chunks = [c for c in chunks if c.name == "SampleClass"]
    method_chunks = [c for c in chunks if c.chunk_type == ChunkType.METHOD]
    assert len(cls_chunks) == 1
    method_names = {c.name for c in method_chunks}
    assert {
        "method_one", "static_method", "async_method",
    }.issubset(method_names)
    # Methods should be qualified with parent class name.
    assert any(
        c.qualified_name == "SampleClass.method_one"
        for c in method_chunks
    )


@pytest.mark.asyncio
async def test_chunker_target_names_filters(sample_file: Path) -> None:
    """When target_names is set, only those entities are extracted."""
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(
        sample_file, target_names={"top_level_function"},
    )
    names = [c.name for c in chunks]
    # top_level_function is included; async_function and SampleClass are not.
    assert "top_level_function" in names
    assert "async_function" not in names
    assert "SampleClass" not in names


@pytest.mark.asyncio
async def test_chunker_target_method_includes_class_skeleton(
    sample_file: Path,
) -> None:
    """Targeting a method pulls its class (as a skeleton)."""
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(
        sample_file, target_names={"method_one"},
    )
    # Class chunk should be present (as skeleton or class).
    assert any(c.name == "SampleClass" for c in chunks)
    # Method should be extracted.
    method_chunks = [
        c for c in chunks
        if c.qualified_name == "SampleClass.method_one"
    ]
    assert len(method_chunks) == 1


@pytest.mark.asyncio
async def test_chunker_extracts_calls(sample_file: Path) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    afn = next(c for c in chunks if c.name == "async_function")
    # async_function calls print() and range()
    assert "print" in afn.calls
    assert "range" in afn.calls


@pytest.mark.asyncio
async def test_chunker_complexity_baseline(sample_file: Path) -> None:
    """top_level_function has no branches → complexity 1.
    async_function has if + for → complexity 3 (1 + 2 branches)."""
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    top = next(c for c in chunks if c.name == "top_level_function")
    afn = next(c for c in chunks if c.name == "async_function")
    assert top.complexity == 1
    assert afn.complexity == 3


@pytest.mark.asyncio
async def test_chunker_token_counts_populated(
    sample_file: Path,
) -> None:
    """Every chunk should have a non-zero token_count after extraction."""
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(sample_file, include_all=True)
    assert chunks
    for c in chunks:
        assert c.token_count > 0, f"{c.qualified_name} has zero tokens"


@pytest.mark.asyncio
async def test_chunker_caches_results(sample_file: Path) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    a = await chunker.extract_chunks(sample_file, include_all=True)
    b = await chunker.extract_chunks(sample_file, include_all=True)
    # Same list instance returned (cache hit).
    assert a is b


@pytest.mark.asyncio
async def test_chunker_handles_syntax_error(tmp_path: Path) -> None:
    """Malformed source returns empty list — does NOT raise."""
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(bad, include_all=True)
    assert chunks == []


@pytest.mark.asyncio
async def test_chunker_handles_missing_file(tmp_path: Path) -> None:
    chunker = ASTChunker(_StubTokenCounter())
    chunks = await chunker.extract_chunks(
        tmp_path / "does_not_exist.py", include_all=True,
    )
    assert chunks == []


@pytest.mark.asyncio
async def test_chunker_decorator_inclusion_toggle(tmp_path: Path) -> None:
    """``include_decorators=False`` constructor knob excludes decorator
    lines from extracted source. (The Slice 11.1 extraction made this
    a knob instead of reading SmartContextConfig directly.)"""
    p = tmp_path / "deco.py"
    p.write_text(textwrap.dedent('''
        @staticmethod
        @custom_decorator
        def decorated() -> int:
            return 1
    ''').lstrip(), encoding="utf-8")
    on = ASTChunker(_StubTokenCounter(), include_decorators=True)
    off = ASTChunker(_StubTokenCounter(), include_decorators=False)
    chunks_on = await on.extract_chunks(p, include_all=True)
    chunks_off = await off.extract_chunks(p, include_all=True)
    fn_on = next(c for c in chunks_on if c.name == "decorated")
    fn_off = next(c for c in chunks_off if c.name == "decorated")
    # With decorators on, the chunk source includes them.
    assert "@staticmethod" in fn_on.source_code
    # With decorators off, the chunk source skips them.
    assert "@staticmethod" not in fn_off.source_code


# ---------------------------------------------------------------------------
# §6 — Backward compat: smart_context re-exports
# ---------------------------------------------------------------------------


def test_smart_context_re_exports_ast_chunker() -> None:
    """Existing callers that import from smart_context must keep
    working post-extraction. Pin the re-export contract."""
    from backend.core import smart_context as sc
    # Identity check — sc.ASTChunker IS ast_slicer.ASTChunker.
    assert sc.ASTChunker is ASTChunker
    assert sc.CodeChunk is CodeChunk
    assert sc.ChunkType is ChunkType
    assert sc.RelevanceReason is RelevanceReason
    assert sc.ChunkPriority is ChunkPriority


def test_smart_context_module_no_longer_defines_ast_chunker() -> None:
    """The old definition is gone — no shadow class lurking in
    smart_context.py."""
    src = Path(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/backend/core/smart_context.py"
    ).read_text(encoding="utf-8")
    # Only the re-export remains; no `class ASTChunker:` definition.
    assert "class ASTChunker:" not in src
    assert "class CodeChunk:" not in src
    assert "class ChunkType(Enum):" not in src


def test_smart_context_token_counter_still_satisfies_protocol() -> None:
    """SmartContext's TokenCounter MUST still be usable as the
    chunker's argument — duck-typed protocol contract."""
    from backend.core.smart_context import TokenCounter
    # Has .count(text) method — Protocol satisfied.
    assert hasattr(TokenCounter, "count")
    # Constructor accepts no required args.
    counter = TokenCounter()
    chunker = ASTChunker(counter)
    assert chunker._token_counter is counter  # noqa: SLF001
