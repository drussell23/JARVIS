"""Acting on standing — injecting a contrarian, and ASKING to rotate.

`persona_ledger` derives and decides nothing. This is the half that acts, and
the split matters: deriving is arithmetic over recorded outcomes, while
injecting a reviewer or replacing a persona changes how the organism reasons.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.persona_governor import (
    PersonaGovernor, contrarian_cooldown_s, min_room_size,
)
from backend.core.ouroboros.governance.persona_ledger import (
    FOR, PersonaLedger, sample_floor,
)

_ROOM = ["@yes-man", "@me-too", "@cassandra", "@the-pit"]


def _echo_room() -> PersonaLedger:
    """Two who always agree and are usually wrong; one skeptic who lands."""
    ledger = PersonaLedger()
    for i in range(sample_floor() + 4):
        ledger.note_position(f"op-{i}", "@yes-man", FOR)
        ledger.note_position(f"op-{i}", "@me-too", FOR)
        ledger.settle(f"op-{i}", verified=(i % 5 == 0))
    for i in range(sample_floor() + 4):
        ledger.note_position(f"s-{i}", "@cassandra", FOR)
        ledger.settle(f"s-{i}", verified=True)
    return ledger


def _gov(ledger=None, clock=None):
    return PersonaGovernor(ledger=ledger or _echo_room(), clock=clock)


# --------------------------------------------------------------------------
# contrarian injection
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unchallenged_consensus_injects_a_contrarian() -> None:
    directive = _gov().contrarian_for(_ROOM)
    assert directive is not None
    assert "agreed on 100%" in directive.because
    assert "ATTACK" in directive.directive


def test_the_contrarian_comes_from_OUTSIDE_the_consensus() -> None:
    """Asking a member of the agreeing pair to argue against itself is not a
    second opinion; it is the same opinion wearing a costume."""
    directive = _gov().contrarian_for(_ROOM)
    assert directive.persona not in ("@yes-man", "@me-too")


def test_the_best_CALIBRATED_outsider_is_chosen() -> None:
    """The defence is only as useful as the judgement of whoever it hands the
    argument to."""
    assert _gov().contrarian_for(_ROOM).persona == "@cassandra"


def test_with_no_outsider_it_reports_nothing_rather_than_pretending() -> None:
    assert _gov().contrarian_for(["@yes-man", "@me-too"]) is None


def test_the_directive_is_FIXED_not_generated() -> None:
    """Paying a model to invent disagreement produces theatre, and theatre is
    what an echo chamber already has."""
    import inspect

    from backend.core.ouroboros.governance import persona_governor

    src = inspect.getsource(persona_governor)
    assert "_CONTRARIAN_DIRECTIVE" in src
    for banned in ("await ", "acompletion", "generate("):
        assert banned not in inspect.getsource(
            persona_governor.PersonaGovernor.contrarian_for,
        )


def test_the_directive_forbids_a_manufactured_objection() -> None:
    """"I could not find one" must be an allowed answer, or the contrarian
    learns to invent — and an invented objection teaches the operator to
    ignore the one that matters."""
    assert "cannot find one" in _gov().contrarian_for(_ROOM).directive


# --------------------------------------------------------------------------
# the cooldown
# --------------------------------------------------------------------------

def test_a_cooldown_damps_repeat_injections() -> None:
    """A contrarian review costs a model call; an undamped defence turns a
    measurement into a bill."""
    clock = {"t": 1000.0}
    gov = _gov(clock=lambda: clock["t"])
    assert gov.contrarian_for(_ROOM) is not None
    assert gov.contrarian_for(_ROOM) is None
    clock["t"] += contrarian_cooldown_s() + 1
    assert gov.contrarian_for(_ROOM) is not None


def test_the_FIRST_injection_is_never_blocked_by_process_uptime() -> None:
    """`time.monotonic()` is uptime, so an epoch-zero `_last_injection` would
    suppress every injection for the first cooldown window — silently
    disabling the defence exactly when a session starts."""
    gov = _gov(clock=lambda: 5.0)
    assert gov.contrarian_for(_ROOM) is not None


# --------------------------------------------------------------------------
# rotation is REQUESTED, never taken
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_low_calibration_requests_a_gate_without_blocking() -> None:
    async def _derive():
        return _gov().rotation_requests(_ROOM)

    requests = await asyncio.wait_for(_derive(), timeout=1.0)
    assert requests
    payload = requests[0].gate_payload()
    assert payload["risk_tier"] == "approval_required"
    assert payload["kind"] == "persona_rotation"


def test_the_gate_payload_carries_EVIDENCE_not_a_verdict() -> None:
    """An operator asked to approve a rotation needs the record that prompted
    it, or they are approving the system's opinion of itself."""
    payload = _gov().rotation_requests(_ROOM)[0].gate_payload()
    assert "landed" in payload["text"]
    assert "survived VERIFY" in payload["reason"]


def test_a_disagreeable_persona_who_is_RIGHT_is_never_proposed() -> None:
    proposed = [r.persona for r in _gov().rotation_requests(_ROOM)]
    assert "@cassandra" not in proposed


def test_it_never_proposes_emptying_the_room() -> None:
    """A defence that can remove its own last skeptic has removed the thing
    being defended."""
    small = ["@yes-man", "@me-too", "@cassandra"]
    assert len(small) - len(_gov().rotation_requests(small)) >= min_room_size()


def test_a_persona_not_in_the_room_is_not_proposed() -> None:
    assert _gov().rotation_requests(["@cassandra", "@the-pit", "@x"]) == []


# --------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------

def test_an_empty_ledger_acts_on_nothing() -> None:
    gov = PersonaGovernor(ledger=PersonaLedger())
    assert gov.contrarian_for(_ROOM) is None
    assert gov.rotation_requests(_ROOM) == []


@pytest.mark.parametrize("roster", [[], [""], None])
def test_a_degenerate_roster_never_raises(roster) -> None:
    gov = _gov()
    assert gov.contrarian_for(roster or []) is None
    assert gov.rotation_requests(roster or []) == []


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_PERSONA_GOVERNOR_ENABLED", "0")
    gov = _gov()
    assert gov.contrarian_for(_ROOM) is None
    assert gov.rotation_requests(_ROOM) == []
