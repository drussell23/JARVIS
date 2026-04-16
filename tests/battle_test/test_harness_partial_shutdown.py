"""Tests for BattleTestHarness partial-shutdown insurance.

Covers the atexit fallback that guarantees every session dir ends up
with a ``summary.json`` — even when the clean async path in
``_generate_report`` never completed (SIGTERM during cleanup, exception
in shutdown components, interpreter teardown mid-async).

Why this matters: LastSessionSummary v1.1a lex-max-discovers the prior
session dir at CONTEXT_EXPANSION. Before this fix, a partial-shutdown
session left a dir with only ``debug.log``; LSS correctly skipped it
but the dir itself was noise. These tests lock in the fix so the
fallback keeps firing across future refactors.
"""
from __future__ import annotations

import atexit
import json
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    """Build a harness rooted in tmp_path. Clean up its atexit handler
    after the test so pytest's own shutdown doesn't fire 100 fallbacks
    across a suite run."""
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-unit-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    # atexit.unregister requires the exact callable that was registered.
    # The __init__ path registers harness._atexit_fallback_write.
    atexit.unregister(harness._atexit_fallback_write)


# ---------------------------------------------------------------------------
# (1) atexit handler is registered at __init__
# ---------------------------------------------------------------------------


def test_atexit_fallback_registered_at_init(tmp_harness):
    """``__init__`` installs the fallback via ``atexit.register``.

    Can't observe the atexit registry directly, but ``atexit.unregister``
    returns without error (actually returns None always; the proof is
    that the harness.__init__ path doesn't raise and the method exists).
    """
    assert callable(tmp_harness._atexit_fallback_write)
    # The flag starts False — no summary has been written yet.
    assert tmp_harness._summary_written is False


# ---------------------------------------------------------------------------
# (2) Fallback writes summary.json when _summary_written is False
# ---------------------------------------------------------------------------


def test_atexit_fallback_writes_summary_when_flag_false(tmp_harness):
    """Manually invoking the fallback with flag=False must write a
    partial summary.json with the expected stop_reason."""
    session_dir = tmp_harness._session_dir
    summary_path = session_dir / "summary.json"
    assert not summary_path.exists()

    tmp_harness._atexit_fallback_write()

    assert summary_path.exists(), "fallback did not write summary.json"
    data = json.loads(summary_path.read_text())
    # Must preserve the v1.1a schema so the next session's LSS can
    # parse partial summaries the same way it parses clean ones.
    assert data["schema_version"] == 2
    assert data["session_id"] == tmp_harness._session_id
    # Default _stop_reason ("unknown") → explicit partial-shutdown tag.
    assert data["stop_reason"] == "partial_shutdown:atexit_fallback"
    # Minimal but valid stats block (from empty SessionRecorder).
    assert "stats" in data
    # cost fields present even when empty.
    assert data["cost_total"] == 0.0
    assert data["cost_breakdown"] == {}
    # Flag flipped so a second invocation is a no-op (see test 3).
    assert tmp_harness._summary_written is True


# ---------------------------------------------------------------------------
# (3) Fallback is a no-op when _summary_written is True
# ---------------------------------------------------------------------------


def test_atexit_fallback_no_op_when_flag_already_true(tmp_harness):
    """Clean path flipped the flag — fallback must NOT overwrite the
    (presumably richer) clean summary. Byte-compare to confirm."""
    session_dir = tmp_harness._session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    summary_path = session_dir / "summary.json"
    # Simulate the clean path having already written a richer summary.
    clean_payload = {
        "schema_version": 2,
        "session_id": tmp_harness._session_id,
        "stop_reason": "idle_timeout",
        "stats": {"attempted": 5, "completed": 4, "failed": 1,
                  "cancelled": 0, "queued": 0},
        "cost_total": 0.42,
        "cost_breakdown": {"claude": 0.42},
    }
    summary_path.write_text(json.dumps(clean_payload))
    tmp_harness._summary_written = True  # clean path flipped the flag

    original_bytes = summary_path.read_bytes()
    tmp_harness._atexit_fallback_write()
    # Byte-exact unchanged — fallback must not touch the clean summary.
    assert summary_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# (4) Fallback preserves signal-driven stop_reason with +atexit_fallback suffix
# ---------------------------------------------------------------------------


def test_atexit_fallback_suffixes_existing_stop_reason(tmp_harness):
    """If ``run()`` got far enough to stamp a stop_reason (e.g.
    ``shutdown_signal``) but the subsequent async cleanup didn't
    finish, the fallback must preserve that signal context and tag
    ``+atexit_fallback`` so ops can tell the clean path didn't close.
    """
    tmp_harness._stop_reason = "shutdown_signal"
    tmp_harness._atexit_fallback_write()

    data = json.loads(
        (tmp_harness._session_dir / "summary.json").read_text()
    )
    assert data["stop_reason"] == "shutdown_signal+atexit_fallback"


def test_atexit_fallback_suffixes_budget_exhausted(tmp_harness):
    tmp_harness._stop_reason = "budget_exhausted"
    tmp_harness._atexit_fallback_write()
    data = json.loads(
        (tmp_harness._session_dir / "summary.json").read_text()
    )
    assert data["stop_reason"] == "budget_exhausted+atexit_fallback"


# ---------------------------------------------------------------------------
# (5) Fallback is fully defensive — swallows any exception
# ---------------------------------------------------------------------------


def test_atexit_fallback_never_raises_on_bad_session_recorder(tmp_harness):
    """Replacing the recorder with a broken stub must not propagate
    any exception — atexit handlers CANNOT raise without making the
    interpreter exit dirty."""
    class _BrokenRecorder:
        def save_summary(self, **_kw):
            raise RuntimeError("boom — simulated teardown failure")

    tmp_harness._session_recorder = _BrokenRecorder()
    # Must not raise.
    tmp_harness._atexit_fallback_write()
    # Flag stays False because write failed.
    assert tmp_harness._summary_written is False


def test_atexit_fallback_never_raises_on_bad_cost_tracker(tmp_harness):
    """If CostTracker attributes are bad (e.g. partial boot), fallback
    degrades to zero-cost snapshot rather than raising."""
    class _BrokenTracker:
        @property
        def total_spent(self):
            raise RuntimeError("cost tracker broke")

        @property
        def breakdown(self):
            raise RuntimeError("cost tracker broke")

    tmp_harness._cost_tracker = _BrokenTracker()
    # Must not raise.
    tmp_harness._atexit_fallback_write()
    # But the write itself should still succeed — cost fields default to 0.
    summary_path = tmp_harness._session_dir / "summary.json"
    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["cost_total"] == 0.0
    assert data["cost_breakdown"] == {}


# ---------------------------------------------------------------------------
# (6) Fallback creates the session dir if absent
# ---------------------------------------------------------------------------


def test_atexit_fallback_creates_missing_session_dir(tmp_harness):
    """Session dir may not exist yet (harness crashed before boot
    components materialized it). Fallback must mkdir + write."""
    # Safety: confirm dir does not exist.
    assert not tmp_harness._session_dir.exists()

    tmp_harness._atexit_fallback_write()

    assert tmp_harness._session_dir.is_dir()
    assert (tmp_harness._session_dir / "summary.json").is_file()


# ---------------------------------------------------------------------------
# (7) Partial summary is v1.1a-parseable by LastSessionSummary
# ---------------------------------------------------------------------------


def test_partial_summary_is_lss_parseable(tmp_harness):
    """The whole point of the fix: partial summaries must be readable
    by LastSessionSummary at the next session's CONTEXT_EXPANSION.
    Without this, the partial dir is still noise even after the fix."""
    # Stamp a recognizable stop_reason so we can confirm LSS picks it up.
    tmp_harness._stop_reason = "shutdown_signal"
    tmp_harness._atexit_fallback_write()

    # Drive LastSessionSummary against the tmp repo and assert it
    # produces a parseable record + non-empty render.
    import os
    from backend.core.ouroboros.governance import last_session_summary as lss

    lss.reset_default_summary()
    lss.set_active_session_id(None)
    os.environ["JARVIS_LAST_SESSION_SUMMARY_ENABLED"] = "true"
    try:
        summary = lss.LastSessionSummary(tmp_harness._config.repo_path)
        records = summary.load()
        assert len(records) == 1
        rec = records[0]
        assert rec.session_id == tmp_harness._session_id
        # Rendered output must include stop_reason context from the
        # partial summary (proves LSS didn't silently drop it).
        prompt = summary.format_for_prompt() or ""
        assert tmp_harness._session_id in prompt
        # Zero-op note path fires because stats.attempted == 0.
        assert "zero attempted ops" in prompt
    finally:
        os.environ.pop("JARVIS_LAST_SESSION_SUMMARY_ENABLED", None)
        lss.reset_default_summary()


# ---------------------------------------------------------------------------
# (8) Flag semantics — clean path flag-flip prevents double-write
# ---------------------------------------------------------------------------


def test_summary_written_flag_starts_false(tmp_harness):
    assert tmp_harness._summary_written is False


def test_summary_written_flag_set_by_fallback_success(tmp_harness):
    tmp_harness._atexit_fallback_write()
    assert tmp_harness._summary_written is True


def test_second_fallback_invocation_is_noop(tmp_harness):
    """Calling the fallback twice must not overwrite its own output."""
    tmp_harness._atexit_fallback_write()
    path = tmp_harness._session_dir / "summary.json"
    first_bytes = path.read_bytes()
    # A second call returns early because flag is now True.
    tmp_harness._atexit_fallback_write()
    assert path.read_bytes() == first_bytes


# ---------------------------------------------------------------------------
# (9) Signal-driven sync write — SIGTERM insurance
# ---------------------------------------------------------------------------


def test_signal_handler_writes_partial_summary_synchronously(tmp_harness):
    """``_handle_shutdown_signal`` must land a summary.json on disk
    before setting the shutdown event. This is the insurance path for
    the SIGTERM-followed-quickly-by-SIGKILL case where atexit cannot
    fire (Python's atexit runs only on normal interpreter exit).
    """
    import asyncio
    # Create the shutdown_event in a loop so _handle_shutdown_signal
    # can set it (the method short-circuits gracefully when event is None).
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        tmp_harness._shutdown_event = asyncio.Event()
        assert not (tmp_harness._session_dir / "summary.json").exists()

        tmp_harness._handle_shutdown_signal()

        # Summary landed.
        summary_path = tmp_harness._session_dir / "summary.json"
        assert summary_path.exists()
        data = json.loads(summary_path.read_text())
        assert data["stop_reason"] == "partial_shutdown:atexit_fallback"
        # Shutdown event set so the async finally path can fire next.
        assert tmp_harness._shutdown_event.is_set()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_signal_handler_is_idempotent_across_siginit_sigterm(tmp_harness):
    """Real-world: SIGTERM arrives, handler fires, clean path runs
    save_summary (flips flag), then a stray SIGINT or another SIGTERM
    arrives during cleanup. The second signal must NOT overwrite the
    clean summary."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        tmp_harness._shutdown_event = asyncio.Event()
        # Simulate clean path: set flag as if save_summary completed.
        tmp_harness._summary_written = True
        summary_path = tmp_harness._session_dir / "summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text('{"clean": "summary"}')

        # Second signal arrives.
        tmp_harness._handle_shutdown_signal()

        # Clean summary preserved.
        assert summary_path.read_text() == '{"clean": "summary"}'
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_signal_handler_tolerates_missing_shutdown_event(tmp_harness):
    """If the signal fires before ``run()`` creates the Event (edge
    case: kill during boot), handler must still write the partial
    summary and not crash on ``None.set()``."""
    # _shutdown_event is still None (harness was created but run() not called).
    assert tmp_harness._shutdown_event is None
    # Must not raise.
    tmp_harness._handle_shutdown_signal()
    assert (tmp_harness._session_dir / "summary.json").is_file()
