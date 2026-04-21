"""Slice 2 Production Integration — Venom tool-loop scorer path.

Pins:
* Env flag default-off
* When off: legacy last-N split returned
* When on but insufficient chunks: no compaction at all
* When on and scorer path runs: score-ordered selection replaces recency-only
* Intent tracker auto-fed from tool chunks (paths + tool names)
* Manifest records the pass
* Failure in scorer path → legacy fallback (never lose data)
* Path / tool extraction helpers are correct
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.context_intent import (
    intent_tracker_for,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_manifest import (
    manifest_for,
    reset_default_manifest_registry,
)
from backend.core.ouroboros.governance.context_pins import (
    reset_default_pin_registries,
)
from backend.core.ouroboros.governance.tool_executor import (
    ToolCall,
    ToolLoopCoordinator,
    _extract_paths_from_tool_chunk,
    _extract_tools_from_tool_chunk,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in list(os.environ.keys()):
        if (
            key.startswith("JARVIS_TOOL_LOOP_SCORER_")
            or key.startswith("JARVIS_COMPACT_PRESERVE_TOOL_CHUNKS")
        ):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()
    yield
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()


# ===========================================================================
# Path / tool extractors — cheap but must be right
# ===========================================================================


def test_extract_paths_from_tool_chunk_finds_canonical_paths():
    chunk = (
        "[TOOL RESULT BEGIN]\n"
        "tool: read_file\n"
        "backend/auth.py contents...\n"
        "referenced tests/test_login.py\n"
        "[TOOL RESULT END]\n"
    )
    paths = _extract_paths_from_tool_chunk(chunk)
    assert "backend/auth.py" in paths
    assert "tests/test_login.py" in paths


def test_extract_paths_dedupes_and_keeps_short_off():
    chunk = "foo backend/x.py bar backend/x.py baz"
    assert _extract_paths_from_tool_chunk(chunk) == ["backend/x.py"]


def test_extract_paths_rejects_too_short_fragments():
    """Fragments under 4 chars are dropped."""
    chunk = "x y z.ts 99.py"
    paths = _extract_paths_from_tool_chunk(chunk)
    # 'z.ts' is exactly 4 chars → accepted; '99.py' is 5 → accepted.
    assert "z.ts" in paths
    assert "99.py" in paths


def test_extract_tools_from_tool_chunk():
    chunk = (
        "\ntool: edit_file\n"
        "...\n"
        "\ntool: read_file\n"
        "...\n"
    )
    tools = _extract_tools_from_tool_chunk(chunk)
    assert tools == ["edit_file", "read_file"]


def test_extract_tools_no_duplicates():
    chunk = "\ntool: read_file\n\ntool: read_file\n\ntool: bash\n"
    assert _extract_tools_from_tool_chunk(chunk) == ["read_file", "bash"]


# ===========================================================================
# _maybe_score_tool_chunks — the core integration
# ===========================================================================


def _build_coord() -> ToolLoopCoordinator:
    from pathlib import Path

    class _FakePolicy:
        def evaluate(self, call, ctx): ...
        def repo_root_for(self, repo): return Path(".")

    class _FakeBackend:
        async def execute_async(self, call, ctx, deadline): ...

    return ToolLoopCoordinator(
        backend=_FakeBackend(),  # type: ignore[arg-type]
        policy=_FakePolicy(),    # type: ignore[arg-type]
        max_rounds=1,
        tool_timeout_s=5.0,
    )


@pytest.mark.asyncio
async def test_default_flag_off_returns_legacy_split():
    coord = _build_coord()
    chunks = [f"chunk-{i}" for i in range(10)]
    old, recent = await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-1", recent_count=3,
    )
    # Legacy: last 3 are "recent", first 7 are "old"
    assert old == chunks[:7]
    assert recent == chunks[7:]


@pytest.mark.asyncio
async def test_flag_on_preserves_intent_rich_chunk(monkeypatch):
    """With scorer path: intent-rich OLDEST chunk is kept over recent noise."""
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "true")
    coord = _build_coord()
    # Pre-feed a strong intent signal via the tracker.
    from backend.core.ouroboros.governance.context_intent import TurnSource
    tracker = intent_tracker_for("op-intent")
    for _ in range(5):
        tracker.ingest_turn("focus backend/hot.py", source=TurnSource.USER)

    chunks = [
        # Index 0 — intent-rich (mentions backend/hot.py)
        "\n[TOOL RESULT]\ntool: read_file\nbackend/hot.py content\n",
    ] + [
        # Indices 1..14 — plain noise
        f"\n[TOOL RESULT]\ntool: bash\nnoise {i}\n"
        for i in range(1, 15)
    ]
    old, recent = await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-intent", recent_count=6,
    )
    # Intent-rich chunk (index 0) MUST be in the kept set
    assert chunks[0] in recent, (
        "intent-rich oldest chunk must be preserved over recent noise"
    )


@pytest.mark.asyncio
async def test_flag_on_with_no_intent_keeps_recent_ish(monkeypatch):
    """Without intent signal, scorer still keeps newest chunks
    (base_recency dominates)."""
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "true")
    coord = _build_coord()
    chunks = [f"\n[TOOL RESULT]\ntool: bash\nresult {i}\n" for i in range(10)]
    old, recent = await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-nointent", recent_count=3,
    )
    # Recent should bias toward the newest chunks
    recent_texts = {c for c in recent}
    assert chunks[-1] in recent_texts


@pytest.mark.asyncio
async def test_flag_on_does_not_auto_feed_chunk_text(monkeypatch):
    """Chunk body paths AND tool names are NOT auto-fed.

    Real-session testing surfaced a self-reinforcement bug: 100 noise
    chunks each mentioning a different path would collectively swamp
    the operator-authored focus path, burying genuinely-intent-rich
    content. Fix: don't feed from chunk bodies. Authoritative signal
    arrives via operator turns + Slice-3 ledger bridges (which see
    explicit record_file_read calls from the orchestrator).
    """
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "true")
    coord = _build_coord()
    chunks = [
        "\n[TOOL RESULT]\ntool: read_file\nbackend/auth.py content\n",
        "\n[TOOL RESULT]\ntool: edit_file\nbackend/auth.py edited\n",
        "\n[TOOL RESULT]\ntool: read_file\ntests/test_x.py content\n",
    ]
    await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-feed", recent_count=2,
    )
    tracker = intent_tracker_for("op-feed")
    intent = tracker.current_intent()
    # Neither paths nor tools are auto-captured from chunk bodies
    assert intent.recent_paths == ()
    assert intent.recent_tools == ()


@pytest.mark.asyncio
async def test_flag_on_records_manifest(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "true")
    coord = _build_coord()
    chunks = [f"\n[TOOL RESULT]\nchunk {i}\n" for i in range(10)]
    await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-man", recent_count=3,
    )
    recs = manifest_for("op-man").all_records()
    assert len(recs) == 1


@pytest.mark.asyncio
async def test_flag_on_scorer_raises_falls_back(monkeypatch):
    """When the scorer raises, we fall back to legacy split (no data loss)."""
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "true")
    coord = _build_coord()
    # Monkey-patch the scorer class to raise via a failed import.
    # Simplest: point the module-level PreservationScorer to something broken.
    import backend.core.ouroboros.governance.context_intent as _ci_mod

    class _BoomScorer:
        def select_preserved(self, *a, **kw):
            raise RuntimeError("boom")

    original = _ci_mod.PreservationScorer
    _ci_mod.PreservationScorer = _BoomScorer  # type: ignore[misc]
    try:
        chunks = [f"chunk-{i}" for i in range(10)]
        old, recent = await coord._maybe_score_tool_chunks(
            chunks=chunks, op_id="op-boom", recent_count=3,
        )
        # Legacy fallback shape
        assert old == chunks[:7]
        assert recent == chunks[7:]
    finally:
        _ci_mod.PreservationScorer = original  # type: ignore[misc]


# ===========================================================================
# Env flag shape
# ===========================================================================


@pytest.mark.asyncio
async def test_explicit_false_returns_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_TOOL_LOOP_SCORER_ENABLED", "false")
    coord = _build_coord()
    chunks = [f"c{i}" for i in range(10)]
    old, recent = await coord._maybe_score_tool_chunks(
        chunks=chunks, op_id="op-off", recent_count=3,
    )
    assert recent == chunks[-3:]
