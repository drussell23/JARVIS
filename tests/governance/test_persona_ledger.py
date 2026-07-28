"""Who was right, who agrees, and how much of it we can stand behind.

Both numbers are DERIVED from `GENERATE → REVIEW → VERIFY`, never
self-reported. Asking an LLM to rate its own interaction costs a model call,
returns a sycophantic number, and has no ground truth — invented data wearing
a percentage sign. VERIFY is a test suite; it does not have opinions.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.persona_ledger import (
    AGAINST, FOR, INSUFFICIENT, MEASURED, PersonaLedger, echo_threshold,
    rotation_threshold, sample_floor,
)


def _agree(ledger: PersonaLedger, a: str, b: str, n: int,
           right_every: int = 4) -> None:
    """Two personas who always take the same side, right 1-in-n times."""
    for i in range(n):
        ledger.note_position(f"op-{a}{b}-{i}", a, FOR)
        ledger.note_position(f"op-{a}{b}-{i}", b, FOR)
        ledger.settle(f"op-{a}{b}-{i}", verified=(i % right_every == 0))


# --------------------------------------------------------------------------
# the mandate's two assertions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_concordance_trips_the_echo_chamber_flag() -> None:
    ledger = PersonaLedger()
    _agree(ledger, "@yes-man", "@me-too", sample_floor() + 4)

    risks = ledger.echo_chamber_risk()
    assert risks, "unchallenged consensus went unnoticed"
    a, b, reading = risks[0]
    assert {a, b} == {"@yes-man", "@me-too"}
    assert reading.rate >= echo_threshold()
    assert reading.provenance == MEASURED


@pytest.mark.asyncio
async def test_low_calibration_proposes_rotation_without_blocking() -> None:
    """A CANDIDATE, never a decision — and derivation must not await
    anything, so it cannot stall the loop it is called from."""
    ledger = PersonaLedger()
    _agree(ledger, "@wrong-often", "@also-wrong", sample_floor() + 4,
           right_every=10)

    async def _derive():
        return ledger.rotation_candidates()

    candidates = await asyncio.wait_for(_derive(), timeout=1.0)
    assert candidates
    who, reading = candidates[0]
    assert reading.rate <= rotation_threshold()
    assert who in ("@wrong-often", "@also-wrong")


# --------------------------------------------------------------------------
# rotation keys on being RIGHT, never on being agreeable
# --------------------------------------------------------------------------

def test_a_disagreeable_persona_who_is_RIGHT_is_never_a_candidate() -> None:
    """THE invariant. Rotating on chemistry selects for agreement and
    converges the room on an echo chamber — the persona who disagrees
    constantly and is right most of the time is the most valuable one here,
    and a concordance-based cull removes her first."""
    ledger = PersonaLedger()
    for i in range(sample_floor() + 4):
        ledger.note_position(f"op-{i}", "@cassandra", AGAINST)
        ledger.note_position(f"op-{i}", "@builder", FOR)
        ledger.settle(f"op-{i}", verified=False)      # cassandra was right

    assert ledger.calibration("@cassandra").rate == 1.0
    assert "@cassandra" not in [w for w, _r in ledger.rotation_candidates()]
    assert "@builder" in [w for w, _r in ledger.rotation_candidates()]


def test_agreeing_wrongly_is_recorded_as_concordance() -> None:
    """Two voices agreeing and both being wrong is exactly the pattern the
    defence exists to notice — it must not be filtered out as a non-event."""
    ledger = PersonaLedger()
    _agree(ledger, "@a", "@b", sample_floor() + 2, right_every=99)
    assert ledger.concordance("@a", "@b").rate == 1.0
    assert ledger.calibration("@a").rate < 0.2


# --------------------------------------------------------------------------
# provenance — a number is not evidence
# --------------------------------------------------------------------------

def test_a_thin_sample_is_INSUFFICIENT_and_inert() -> None:
    """Two calls at 100% is not a track record. Acting on it would fire a
    persona for being new, or trip the defence on a coincidence — the same
    discipline `advisor_locality` applies to an unmeasurable blast radius."""
    ledger = PersonaLedger()
    ledger.note_position("op-1", "@new", FOR)
    ledger.settle("op-1", verified=True)

    reading = ledger.calibration("@new")
    assert reading.provenance == INSUFFICIENT
    assert reading.actionable is False
    assert reading.pct == "—"
    assert ledger.rotation_candidates() == []
    assert ledger.echo_chamber_risk() == []


def test_an_insufficient_pair_never_trips_the_defence() -> None:
    ledger = PersonaLedger()
    _agree(ledger, "@a", "@b", 2)
    assert ledger.concordance("@a", "@b").rate == 1.0
    assert ledger.echo_chamber_risk() == []


def test_an_unknown_persona_reads_as_insufficient() -> None:
    assert PersonaLedger().calibration("@nobody").provenance == INSUFFICIENT


# --------------------------------------------------------------------------
# recording discipline
# --------------------------------------------------------------------------

def test_a_position_with_no_outcome_is_not_a_data_point() -> None:
    """An opinion nobody checked is not evidence."""
    ledger = PersonaLedger()
    ledger.note_position("op-1", "@a", FOR)
    assert ledger.calibration("@a").samples == 0


def test_an_abandoned_op_never_invents_an_outcome() -> None:
    ledger = PersonaLedger()
    ledger.note_position("op-1", "@a", FOR)
    ledger.abandon("op-1")
    ledger.settle("op-1", verified=True)
    assert ledger.calibration("@a").samples == 0


def test_the_window_measures_RECENT_behaviour() -> None:
    """A persona wrong all last month and right all this week should read as
    improving, not as permanently discredited."""
    ledger = PersonaLedger(window=10)
    for i in range(10):
        ledger.note_position(f"w{i}", "@x", FOR)
        ledger.settle(f"w{i}", verified=False)
    for i in range(10):
        ledger.note_position(f"r{i}", "@x", FOR)
        ledger.settle(f"r{i}", verified=True)
    assert ledger.calibration("@x").rate == 1.0


# --------------------------------------------------------------------------
# what the operator sees
# --------------------------------------------------------------------------

def test_standing_renders_a_track_record() -> None:
    ledger = PersonaLedger()
    for i in range(9):
        ledger.note_position(f"op-{i}", "@cassandra", FOR)
        ledger.settle(f"op-{i}", verified=(i < 7))
    assert ledger.standing("@cassandra") == "@cassandra · 7/9 landed"


def test_an_early_record_says_so() -> None:
    ledger = PersonaLedger()
    ledger.note_position("op-1", "@new", FOR)
    ledger.settle("op-1", verified=True)
    assert "early" in ledger.standing("@new")


def test_high_agreement_is_chipped_as_a_WARNING() -> None:
    """Named "unchallenged", not celebrated — consensus that is never
    contested is how a review board stops catching anything."""
    ledger = PersonaLedger()
    _agree(ledger, "@a", "@b", sample_floor() + 4)
    assert "unchallenged" in ledger.chip("@a", "@b")


def test_an_uninteresting_pair_renders_nothing() -> None:
    """A chip reading "tension 12%" on every pair is chrome, and chrome is
    not read."""
    # ~80% agreement: too concordant to be tense, not concordant enough to
    # be unchallenged. Alternating sides would be 50% tension, which IS
    # interesting — the quiet middle is what must render nothing.
    ledger = PersonaLedger()
    for i in range(sample_floor() + 12):
        ledger.note_position(f"op-{i}", "@a", FOR)
        ledger.note_position(f"op-{i}", "@b", AGAINST if i % 5 == 0 else FOR)
        ledger.settle(f"op-{i}", verified=True)
    concordance = ledger.concordance("@a", "@b").rate
    assert 0.65 < concordance < echo_threshold(), "test built the wrong pair"
    assert ledger.chip("@a", "@b") == ""


@pytest.mark.parametrize("junk", [None, "", 42])
def test_junk_never_raises(junk) -> None:
    ledger = PersonaLedger()
    ledger.note_position(junk, junk, junk)
    ledger.settle(junk, verified=True)
    assert isinstance(ledger.calibration(junk).rate, float)
    assert isinstance(ledger.chip(junk, junk), str)


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PERSONA_LEDGER_ENABLED", "0")
    ledger = PersonaLedger()
    ledger.note_position("op-1", "@a", FOR)
    ledger.settle("op-1", verified=True)
    assert ledger.calibration("@a").samples == 0
