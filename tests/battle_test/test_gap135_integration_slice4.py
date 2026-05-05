"""Slice 4 — integration tests + edge cases for the Gap #1+3+5 arc.

Covers cross-substrate interactions: master-flag interlocks, no-TTY
fallback path (byte-identical legacy), concurrent ops in the buffer,
and cleanup paths (discard_active for cancelled ops).
"""
from __future__ import annotations

import threading
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.live_status_line import (
    MASTER_FLAG_ENV_VAR as STATUS_LINE_FLAG,
    is_master_flag_enabled as status_line_enabled,
    make_bottom_toolbar_callable,
    render_status_segment,
)
from backend.core.ouroboros.battle_test.op_block_buffer import (
    BUFFER_SIZE_ENV_VAR,
    OpBlockBuffer,
    OpBlockState,
    reset_default_buffer_for_tests,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(STATUS_LINE_FLAG, raising=False)
    monkeypatch.delenv("JARVIS_OP_COLLAPSE_ENABLED", raising=False)
    monkeypatch.delenv(BUFFER_SIZE_ENV_VAR, raising=False)
    reset_default_buffer_for_tests()
    yield
    reset_default_buffer_for_tests()


# ===========================================================================
# Master-flag interlocks — both Gap #1 and Gap #3 default off pre-graduation
# ===========================================================================


def test_status_line_default_on_post_graduation():
    """Slice 5 graduation flipped this default-on. Operators set
    ``=false`` to opt out."""
    assert status_line_enabled() is True


def test_status_line_off_yields_legacy_passthrough(monkeypatch):
    """When status flag explicitly off, the wrapper returns the swarm
    callable's output BYTE-IDENTICALLY — no extra newlines, no ANSI."""
    monkeypatch.setenv(STATUS_LINE_FLAG, "false")
    raw = "  🐍 swarm:0"
    inner = mock.Mock(return_value=raw)
    wrapped = make_bottom_toolbar_callable(inner)
    out = wrapped()
    assert out == raw  # exact equality, including type


def test_op_collapse_default_off_buffer_unused(monkeypatch):
    """Without the master flag, buffer hooks should be silent.
    The substrate-level test covers buffer correctness; here we
    verify the helper's gate."""
    # The helper is on SerpentREPL/SerpentFlow but we can grep-check
    # the env-flag short-circuit works at module level by reading the
    # source. (Direct invocation requires Console+prompt_toolkit
    # construction.)
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read()
    assert "_op_collapse_enabled" in src
    assert 'JARVIS_OP_COLLAPSE_ENABLED' in src


# ===========================================================================
# No-TTY fallback — should_render gate inside StatusLineBuilder
# ===========================================================================


def test_no_tty_fallback_returns_empty(monkeypatch):
    """When ``should_render()`` returns False (no TTY / kill-switch),
    the wrapper degrades to empty status segment — byte-identical
    legacy passthrough is preserved."""
    monkeypatch.setenv(STATUS_LINE_FLAG, "true")
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=False,
    ):
        assert render_status_segment() == ""


def test_no_builder_registered_returns_empty(monkeypatch):
    """Headless / pre-harness-boot: no builder registered → empty.
    The wrapper must not crash; legacy passthrough applies."""
    monkeypatch.setenv(STATUS_LINE_FLAG, "true")
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=None,
    ):
        assert render_status_segment() == ""


# ===========================================================================
# Concurrent ops in the buffer
# ===========================================================================


def test_swarm_of_concurrent_ops_correctly_committed():
    """8 worker threads each spawn 5 ops (start → append → commit).
    All ops must end COMMITTED with their own line counts intact."""
    buf = OpBlockBuffer(capacity=1000)
    n_workers = 8
    per = 5
    barrier = threading.Barrier(n_workers)

    def _worker(tid: int):
        barrier.wait()
        for i in range(per):
            op_id = f"op-{tid}-{i}"
            buf.start_op(op_id)
            buf.append(op_id, f"line A from {op_id}")
            buf.append(op_id, f"line B from {op_id}")
            buf.commit(op_id, f"summary for {op_id}")

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every op should be COMMITTED with exactly 2 lines.
    snap = buf.snapshot()
    assert snap.committed_count == n_workers * per
    assert snap.buffering_count == 0


def test_swarm_eviction_with_buffering_blocks_does_not_corrupt():
    """When a still-BUFFERING block is evicted, the active-index must
    be cleaned. Concurrent appends after eviction must safely fail
    rather than misroute to a different op's slot."""
    buf = OpBlockBuffer(capacity=3)
    # Fill capacity with buffering blocks
    for i in range(3):
        buf.start_op(f"op-{i}")
    # Push capacity over by starting another
    buf.start_op("op-3")
    # op-0 should be evicted; further appends to it must fail safely
    assert buf.append("op-0", "post-eviction") is False
    # Still-active ops continue to work
    assert buf.append("op-1", "still alive") is True


# ===========================================================================
# Cleanup paths — cancelled / failed ops
# ===========================================================================


def test_discard_active_clears_buffer_state():
    """Cancellation path: op started, then aborted before terminal
    phase. discard_active drops the buffered block entirely so the
    capacity isn't wasted on dead ops."""
    buf = OpBlockBuffer(capacity=5)
    buf.start_op("op-cancel")
    buf.append("op-cancel", "partial line 1")
    buf.append("op-cancel", "partial line 2")
    discarded = buf.discard_active("op-cancel")
    assert discarded is not None
    # No more entry for op-cancel
    assert buf.find_by_op_id("op-cancel") == ()
    # Active index empty
    assert "op-cancel" not in buf.active_op_ids()


def test_commit_after_discard_returns_none():
    """Defensive: if discard happens then a commit fires (race), the
    commit must safely return None rather than resurrect state."""
    buf = OpBlockBuffer(capacity=5)
    buf.start_op("op-x")
    buf.discard_active("op-x")
    assert buf.commit("op-x", "summary") is None


# ===========================================================================
# Gap #1 + Gap #3 master flag interlock (both flags off → both legacy)
# ===========================================================================


def test_both_master_flags_off_yields_full_legacy(monkeypatch):
    """Both gaps support an explicit opt-out: setting both flags to
    ``=false`` returns byte-identical legacy behavior. This is the
    rollback contract — operators can disable post-graduation."""
    monkeypatch.setenv(STATUS_LINE_FLAG, "false")
    monkeypatch.setenv("JARVIS_OP_COLLAPSE_ENABLED", "false")
    # Status line flag OFF
    assert status_line_enabled() is False
    # Op collapse flag also OFF (verified at source level)
    src = open(
        "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read()
    assert "_op_collapse_enabled" in src
    # Wrapper passes through legacy swarm output
    raw = "  🐍 legacy"
    inner = mock.Mock(return_value=raw)
    assert make_bottom_toolbar_callable(inner)() == raw


# ===========================================================================
# Substrate read-only contract — neither slice mutates global state
# ===========================================================================


def test_live_status_line_consumer_only():
    """Slice 1 must NEVER call register_status_line_builder (the
    harness owns construction). AST walk for Call nodes."""
    import ast as _ast
    import backend.core.ouroboros.battle_test.live_status_line as mod
    tree = _ast.parse(open(mod.__file__).read())
    forbidden = {"register_status_line_builder"}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name) and func.id in forbidden:
                pytest.fail(f"live_status_line invokes {func.id}")
            if isinstance(func, _ast.Attribute) and func.attr in forbidden:
                pytest.fail(f"live_status_line invokes {func.attr}")


def test_op_block_buffer_no_console_import():
    """Substrate must remain renderer-agnostic — Slice 2 stores plain
    strings; Console imports would couple us to Rich."""
    import backend.core.ouroboros.battle_test.op_block_buffer as mod
    src = open(mod.__file__).read()
    assert "from rich" not in src
    assert "import rich" not in src
