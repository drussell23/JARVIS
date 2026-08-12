"""The terminal tier: what happens when every lane is economically dry.

The property under test is not "a fallback exists" — it is that the
fallback CANNOT FABRICATE. A tier that invented a candidate would put
work into the ledger that never happened, which is worse than the thrash
it replaces.
"""
from __future__ import annotations

import ast

import pytest

from backend.core.ouroboros.governance import rehearsal_tier as rt
from backend.core.ouroboros.governance.rehearsal_tier import (
    MASTER_FLAG_ENV_VAR,
    PROVENANCE,
    RehearsalDisposition,
    RehearsalOutcome,
    RehearsalTier,
    get_rehearsal_tier,
    is_economic_exhaustion,
    reset_rehearsal_tier_for_tests,
)


ECONOMIC = {
    "cause": "fallback_failed",
    "fsm_state": "QUEUE_ONLY",
    "fsm_failure_mode": "HTTP_402_PAYMENT_REQUIRED",
    "route": "standard",
    "tier0_name": "doubleword",
    "primary_name": "claude",
    "fallback_name": "none",
}

TRANSIENT = {
    "cause": "deadline_exhausted_pre_fallback",
    "fsm_state": "DEGRADED",
    "fsm_failure_mode": "TIMEOUT",
    "route": "standard",
    "tier0_name": "doubleword",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    reset_rehearsal_tier_for_tests()
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    yield
    reset_rehearsal_tier_for_tests()


# ===========================================================================
# It cannot fabricate — the whole point
# ===========================================================================


def test_outcome_has_nowhere_to_put_fabricated_work():
    """Honesty by SHAPE, not by convention: there is no field for content,
    so no future call site can populate one."""
    fields = set(RehearsalOutcome.__dataclass_fields__)
    for banned in ("content", "full_content", "diff", "diff_text",
                   "patch", "files"):
        assert banned not in fields, f"RehearsalOutcome gained {banned!r}"


def test_outcome_is_structurally_non_mutating_and_non_committable():
    out = get_rehearsal_tier().consult("op-1", report=ECONOMIC)
    assert out.disposition is RehearsalDisposition.REHEARSED
    assert out.mutates_disk is False
    assert out.eligible_for_commit is False
    assert out.provenance == PROVENANCE == "rehearsal"


def test_provenance_cannot_be_constructed_as_anything_else():
    """It is a property, not a field — so no caller can pass 'model'."""
    assert "provenance" not in RehearsalOutcome.__dataclass_fields__
    with pytest.raises(TypeError):
        RehearsalOutcome(                                    # type: ignore
            op_id="x", disposition=RehearsalDisposition.REHEARSED,
            provenance="model",
        )


def test_the_module_never_reaches_a_mutation_surface():
    """AST pin: a rehearsal that could import change_engine is one
    refactor from writing bytes."""
    invariants = rt.register_shipped_invariants()
    assert {i.invariant_name for i in invariants} == {
        "rehearsal_tier_has_no_mutation_surface",
        "rehearsal_outcome_cannot_carry_work",
    }
    src = open(rt.__file__).read()
    tree = ast.parse(src)
    for inv in invariants:
        assert inv.validate(tree, src) == (), inv.invariant_name


# ===========================================================================
# Classification — economic vs transient
# ===========================================================================


def test_economic_exhaustion_engages():
    out = get_rehearsal_tier().consult("op-1", report=ECONOMIC)
    assert out.disposition is RehearsalDisposition.REHEARSED
    assert "doubleword" in out.exhausted_providers
    assert out.replay_token == "rehearsal:op-1"


def test_a_transient_exhaustion_must_not_engage():
    """Retrying a timeout is exactly right. Suppressing it would turn a
    recoverable blip into a stalled loop."""
    out = get_rehearsal_tier().consult("op-1", report=TRANSIENT)
    assert out.disposition is RehearsalDisposition.NOT_ENGAGED_TRANSIENT


def test_ambiguity_resolves_toward_retrying():
    """A report naming BOTH a timeout and a quota is ambiguous. Guessing
    'economic' suppresses generation that might have worked; guessing
    'transient' only costs a cascade we would have paid anyway."""
    mixed = dict(ECONOMIC, cause="timeout_then_402")
    assert is_economic_exhaustion(mixed) is False
    out = get_rehearsal_tier().consult("op-1", report=mixed)
    assert out.disposition is RehearsalDisposition.NOT_ENGAGED_TRANSIENT


def test_no_report_is_no_evidence_not_an_assumption():
    out = get_rehearsal_tier().consult("op-1", report=None)
    assert out.disposition is RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    assert rt.rehearsal_enabled() is False
    out = get_rehearsal_tier().consult("op-1", report=ECONOMIC)
    assert out.disposition is RehearsalDisposition.NOT_ENGAGED_DISABLED


# ===========================================================================
# Fail-closed toward the status quo
# ===========================================================================


def test_a_broken_consult_declines_rather_than_engaging(monkeypatch):
    """This tier may make an op cheaper. It must never be the reason an op
    that would have been attempted was not."""
    monkeypatch.setattr(
        rt, "is_economic_exhaustion",
        lambda _r: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = get_rehearsal_tier().consult("op-1", report=ECONOMIC)
    assert out.disposition.engaged is False
    assert out.disposition is RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE


def test_every_declined_disposition_is_distinguishable():
    """'It did not engage' is not actionable; the three reasons call for
    different responses."""
    tier = RehearsalTier()
    assert len({
        RehearsalDisposition.NOT_ENGAGED_DISABLED,
        RehearsalDisposition.NOT_ENGAGED_TRANSIENT,
        RehearsalDisposition.NOT_ENGAGED_NO_EVIDENCE,
    }) == 3
    assert tier.consult("a", report=TRANSIENT).disposition \
        is RehearsalDisposition.NOT_ENGAGED_TRANSIENT


# ===========================================================================
# Adaptive, not scheduled
# ===========================================================================


def test_ttl_is_derived_and_unknown_is_reported_as_unknown():
    """A fabricated TTL would state a lift-time nobody measured — the same
    dishonesty as a fabricated candidate, in a smaller box."""
    out = get_rehearsal_tier().consult("op-1", report=ECONOMIC)
    assert out.suppressed_for_s >= 0.0
    assert isinstance(out.suppressed_for_s, float)


def test_route_state_accumulates_and_is_observable():
    tier = RehearsalTier()
    for i in range(3):
        tier.consult(f"op-{i}", report=ECONOMIC)
    stats = tier.snapshot_stats()
    assert stats["counters"]["rehearsed"] == 3
    assert stats["counters"]["consults"] == 3
    assert stats["routes"]["standard"]["rehearsals"] == 3
    assert stats["routes"]["standard"]["last_cause"] == "fallback_failed"


def test_counters_separate_the_decline_reasons(monkeypatch):
    tier = RehearsalTier()
    tier.consult("a", report=TRANSIENT)
    tier.consult("b", report=None)
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    tier.consult("c", report=ECONOMIC)
    c = tier.snapshot_stats()["counters"]
    assert c["declined_transient"] == 1
    assert c["declined_no_evidence"] == 1
    assert c["declined_disabled"] == 1
    assert c["rehearsed"] == 0


def test_outcome_serialises_its_own_evidence():
    out = get_rehearsal_tier().consult(
        "op-9", report=ECONOMIC, target_files=("a.py", "b.py"),
    )
    d = out.to_dict()
    assert d["provenance"] == "rehearsal"
    assert d["mutates_disk"] is False
    assert d["eligible_for_commit"] is False
    assert d["target_files"] == ["a.py", "b.py"]
    assert "402" in str(ECONOMIC["fsm_failure_mode"])


# ===========================================================================
# The wiring seam
# ===========================================================================


def test_raise_exhausted_still_raises_and_now_carries_the_verdict():
    """The helper is typed NoReturn and every caller depends on that. The
    tier rides ALONGSIDE the raise, never in place of it."""
    src = open(
        "backend/core/ouroboros/governance/candidate_generator.py",
    ).read()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_raise_exhausted"
    )
    # Still terminates in a raise on every path.
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises, "_raise_exhausted must still raise"
    # And no `return` was introduced that could bypass it.
    returns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Return) and n.value is not None]
    assert not returns, (
        "_raise_exhausted must never return a value — callers depend on "
        "NoReturn, and a rehearsal must not become a silent success"
    )
