"""Slice 227 — Context-aware hedge governor (gate-aware RT/BATCH race).

ROOT CAUSE (live soak GOAL-001::file-00, layer 3): the proactive transport hedge
races RT (which runs the Venom tool loop → does exploration) against BATCH (a
single completion → NO tool loop, zero exploration). `hedged_race` returns the
FIRST success, so when the batch arm finishes first its un-explored candidate
reaches the Iron Gate → `exploration_insufficient: 0/1` → generation_failed. The
performance layer was silently defeating the security floor.

FIX: ``hedged_race`` gains ``prefer_fast``. When set (the caller derives it from
the SAME Slice-226 predicate ``exploration_gate_demands_tools`` — one source of
truth across capability/security/concurrency planes), a winning BATCH result is
held in a speculative buffer and the race keeps waiting for the RT arm. BATCH is
used ONLY if RT ruptures/fails — so the hedge's entire reason for being (rupture
protection) is preserved; we just stop letting batch *pre-empt* an RT arm that's
actively exploring. ``prefer_fast=False`` (default) is byte-identical legacy
FIRST_COMPLETED.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.dw_transport_hedge import hedged_race


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _arm(value, *, delay=0.0, exc=None):
    async def _go():
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return value
    return _go


# ── legacy (prefer_fast=False) is byte-identical FIRST_COMPLETED ────────────

def test_legacy_batch_wins_when_faster():
    r = _run(hedged_race(
        _arm("RT", delay=0.05), _arm("BATCH", delay=0.0), prefer_fast=False))
    assert r == "BATCH"


def test_legacy_rt_wins_when_faster():
    r = _run(hedged_race(
        _arm("RT", delay=0.0), _arm("BATCH", delay=0.05), prefer_fast=False))
    assert r == "RT"


# ── prefer_fast: batch can't pre-empt an exploring RT arm ───────────────────

def test_prefer_fast_waits_for_rt_even_when_batch_faster():
    """The whole fix: batch finishes first, but RT succeeds → RT wins."""
    r = _run(hedged_race(
        _arm("RT", delay=0.05), _arm("BATCH", delay=0.0), prefer_fast=True))
    assert r == "RT", "batch pre-empted the exploring RT arm"


def test_prefer_fast_rt_faster_still_wins():
    r = _run(hedged_race(
        _arm("RT", delay=0.0), _arm("BATCH", delay=0.05), prefer_fast=True))
    assert r == "RT"


def test_prefer_fast_falls_back_to_batch_on_rt_rupture():
    """Rupture protection PRESERVED: batch buffered, RT ruptures → batch wins."""
    r = _run(hedged_race(
        _arm(None, delay=0.05, exc=RuntimeError("rupture")),
        _arm("BATCH", delay=0.0),
        prefer_fast=True, is_rupture=lambda e: True))
    assert r == "BATCH"


def test_prefer_fast_uses_batch_on_rt_nonrupture_failure():
    """RT fails (non-rupture) after batch buffered → still use the batch result."""
    r = _run(hedged_race(
        _arm(None, delay=0.05, exc=ValueError("bad")),
        _arm("BATCH", delay=0.0),
        prefer_fast=True, is_rupture=lambda e: False))
    assert r == "BATCH"


def test_prefer_fast_both_fail_raises():
    with pytest.raises(BaseException):
        _run(hedged_race(
            _arm(None, delay=0.0, exc=RuntimeError("rt")),
            _arm(None, delay=0.02, exc=RuntimeError("batch")),
            prefer_fast=True, is_rupture=lambda e: True))


def test_prefer_fast_reports_winner_rt():
    seen = {}
    _run(hedged_race(
        _arm("RT", delay=0.02), _arm("BATCH", delay=0.0),
        prefer_fast=True,
        on_outcome=lambda w, r: seen.update(winner=w)))
    assert seen.get("winner") == "rt"


def test_prefer_fast_reports_winner_batch_on_rupture():
    seen = {}
    _run(hedged_race(
        _arm(None, delay=0.03, exc=RuntimeError("rupture")),
        _arm("BATCH", delay=0.0),
        prefer_fast=True, is_rupture=lambda e: True,
        on_outcome=lambda w, r: seen.update(winner=w)))
    assert seen.get("winner") == "batch"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
