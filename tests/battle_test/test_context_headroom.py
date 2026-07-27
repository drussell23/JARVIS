"""Context headroom, from the number the tool loop already computes.

**Per-op token count was NOT missing.** `attach_heartbeat` has rendered
`↓ 15.9k tokens` from `ThinkingProgressObserver` since #70082, and it is
live-proven. An earlier gap analysis in this session listed it as absent
without checking — this suite pins that it exists so nobody rebuilds it.

What WAS missing is context headroom. The tool loop compares
`len(current_prompt)` against `_COMPACT_THRESHOLD_CHARS` every round and only
ever logged the result, so an operator experienced compaction as the model
suddenly forgetting earlier rounds, with nothing on screen to explain it.

Publishing that same number — rather than computing a second one — keeps ONE
definition of "how full is the context", so the status line and the compactor
can never disagree.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.core.ouroboros.battle_test.attach_heartbeat import (
    format_heartbeat_line,
)
from backend.core.ouroboros.governance.tool_executor import (
    compaction_threshold_fraction,
    context_utilisation,
    note_context_utilisation,
)

_REPO = Path(__file__).resolve().parents[2]


def _line(pct: float, **over: Any) -> str:
    payload: Dict[str, Any] = {
        "active": True, "verb": "Synthesizing", "elapsed_s": 249.0,
        "tokens_total": 15900, "provider_label": "DW-397B",
        "context_pct": pct,
    }
    payload.update(over)
    return format_heartbeat_line(payload, now_mono=0.0, arrival_mono=0.0)


# --------------------------------------------------------------------------
# 1. what already existed — pinned so it is not rebuilt
# --------------------------------------------------------------------------

def test_the_token_counter_already_exists() -> None:
    """NOT a gap. Pinned because an earlier analysis called it missing."""
    assert "15.9k tokens" in _line(0.0)


def test_elapsed_and_provider_already_render() -> None:
    out = _line(0.0)
    assert "4m 9s" in out and "DW-397B" in out


# --------------------------------------------------------------------------
# 2. the gauge publishes, it does not recompute
# --------------------------------------------------------------------------

def test_utilisation_is_recorded_and_read_back() -> None:
    note_context_utilisation("op-ctx-1", 1000)
    assert context_utilisation("op-ctx-1") > 0.0


def test_an_unknown_op_reads_zero_not_a_guess() -> None:
    """Zero means "no reading", and the renderer treats it as absent — a
    confident 0% on a full op is worse than saying nothing."""
    assert context_utilisation("op-never-seen") == 0.0
    assert "ctx" not in _line(0.0)


def test_utilisation_is_clamped() -> None:
    note_context_utilisation("op-huge", 10 ** 12)
    assert 0.0 <= context_utilisation("op-huge") <= 1.0


def test_the_gauge_is_bounded() -> None:
    """A gauge, not a ledger."""
    from backend.core.ouroboros.governance.tool_executor import _CONTEXT_GAUGE

    for i in range(500):
        note_context_utilisation(f"op-bound-{i}", 500)
    assert len(_CONTEXT_GAUGE) <= 32


def test_the_newest_reading_wins() -> None:
    note_context_utilisation("op-seq", 100)
    first = context_utilisation("op-seq")
    note_context_utilisation("op-seq", 100000)
    assert context_utilisation("op-seq") > first


@pytest.mark.parametrize("junk", ["", None])
def test_noting_junk_never_raises(junk: Any) -> None:
    note_context_utilisation(junk, 100)
    assert context_utilisation(junk) == 0.0


def test_the_threshold_comes_from_the_compactors_own_config() -> None:
    """One definition of "how full is too full" — the status line cannot
    disagree with the thing that actually compacts."""
    floor = compaction_threshold_fraction()
    assert 0.0 < floor <= 1.0


def test_the_loop_publishes_every_round_not_only_at_the_watermark() -> None:
    """The watermark fires once; an operator watching a long op needs the
    number continuously, and it is already in hand."""
    import ast

    src = (_REPO / "backend/core/ouroboros/governance/tool_executor.py").read_text()
    assert "note_context_utilisation(op_id, len(current_prompt))" in src
    # And it is published OUTSIDE the once-only watermark guard.
    idx = src.index("note_context_utilisation(op_id, len(current_prompt))")
    assert "_soft_overflow_warned" not in src[idx - 200:idx]


# --------------------------------------------------------------------------
# 3. shown only when it matters
# --------------------------------------------------------------------------

def test_an_early_op_shows_nothing() -> None:
    """Every op starts near empty; a number sitting there teaches the
    operator to ignore it."""
    assert "ctx" not in _line(0.30)


def test_approaching_the_threshold_shows_the_figure() -> None:
    assert "ctx 62%" in _line(0.62)


def test_past_the_threshold_explains_what_is_happening() -> None:
    """Otherwise compaction is experienced as the model forgetting."""
    out = _line(0.81)
    assert "ctx 81%" in out and "compacting" in out


def test_the_boundary_is_derived_not_hardcoded_in_the_renderer() -> None:
    """If the compaction threshold moves, the display moves with it."""
    floor = compaction_threshold_fraction()
    assert "compacting" in _line(min(0.99, floor + 0.01))
    assert "compacting" not in _line(max(0.0, floor - 0.05))


def test_an_inactive_heartbeat_renders_nothing() -> None:
    assert format_heartbeat_line({"context_pct": 0.9}) == ""


def test_the_pulse_survives_a_missing_context_field() -> None:
    """Older daemons send no such key — the frame must still render."""
    payload = {"active": True, "verb": "Working", "elapsed_s": 1.0,
               "tokens_total": 10}
    assert format_heartbeat_line(payload, now_mono=0.0, arrival_mono=0.0)


@pytest.mark.parametrize("junk", ["nonsense", None, {}, -1])
def test_a_hostile_context_value_never_raises(junk: Any) -> None:
    assert isinstance(_line(0.0, context_pct=junk), str)
