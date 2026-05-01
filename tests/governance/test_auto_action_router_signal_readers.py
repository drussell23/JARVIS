"""Move 3 Slice 2 — signal source reader regression suite.

Pins the contract that the three readers:
  * Wrap existing ledger surfaces (no duplicated state-gathering).
  * Return tuples of the input dataclasses Slice 1 defined.
  * NEVER raise — missing ledger / parse failure / module not
    importable returns ``()``.
  * ``gather_context`` composes all three into an
    ``AutoActionContext`` with the caller-supplied current_* fields
    rideing on top.

The confidence-verdict reader is intentionally an empty-tuple stub
in Slice 2; Slice 3 wires the producer side at the
confidence_monitor publish seam. This is encoded as a passing
contract (``return ()``) — not a TODO/skip.

Authority Invariant
-------------------
Tests import only the module under test + stdlib + minimal mocks.
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_router_state():
    """Clear the Slice 3 verdict ring buffer + ledger singleton
    before every test in this module so prior-test state doesn't
    leak into the Slice 2 reader assertions (the verdict reader
    pulls from the in-process buffer, which other tests in this
    suite may populate)."""
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer, reset_default_ledger_for_tests,
        clear_op_context_registry,
    )
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()
    yield
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()


# -----------------------------------------------------------------------
# § A — Public API surface
# -----------------------------------------------------------------------


def test_module_exports_three_readers_and_gather_context():
    from backend.core.ouroboros.governance import auto_action_router as m
    expected = {
        "recent_postmortem_outcomes",
        "recent_confidence_verdicts",
        "recent_adaptation_proposals",
        "gather_context",
    }
    actual = set(getattr(m, "__all__", ()))
    missing = expected - actual
    assert not missing, f"missing exports: {missing}"


# -----------------------------------------------------------------------
# § B — Each reader returns a tuple of the right type
# -----------------------------------------------------------------------


def test_postmortem_reader_returns_tuple():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes, RecentOpOutcome,
    )
    result = recent_postmortem_outcomes(limit=4)
    assert isinstance(result, tuple)
    for item in result:
        assert isinstance(item, RecentOpOutcome)


def test_confidence_verdict_reader_returns_tuple():
    """Slice 2 stub: empty tuple. Slice 3 wires the producer."""
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_confidence_verdicts,
    )
    result = recent_confidence_verdicts()
    assert isinstance(result, tuple)
    assert len(result) == 0  # explicit empty stub for Slice 2


def test_adaptation_proposal_reader_returns_tuple():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_adaptation_proposals, RecentAdaptationProposal,
    )
    result = recent_adaptation_proposals(limit=4)
    assert isinstance(result, tuple)
    for item in result:
        assert isinstance(item, RecentAdaptationProposal)


# -----------------------------------------------------------------------
# § C — Postmortem reader behavioral mapping
# -----------------------------------------------------------------------


def _make_fake_pm(
    op_id: str,
    *,
    must_hold_failed: int = 0,
    should_hold_failed: int = 0,
    ideal_failed: int = 0,
    error_count: int = 0,
    has_blocking_failures: bool = False,
    started_unix: float = 1000.0,
    completed_unix: float = 1010.0,
):
    """Build a duck-typed VerificationPostmortem clone for tests."""
    from types import SimpleNamespace
    return SimpleNamespace(
        op_id=op_id,
        must_hold_failed=must_hold_failed,
        should_hold_failed=should_hold_failed,
        ideal_failed=ideal_failed,
        error_count=error_count,
        has_blocking_failures=has_blocking_failures,
        started_unix=started_unix,
        completed_unix=completed_unix,
    )


def test_postmortem_reader_maps_clean_records_as_success():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )
    fake_pms = [
        _make_fake_pm("op-aaa"),
        _make_fake_pm("op-bbb"),
    ]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        result = recent_postmortem_outcomes(limit=2)
    assert len(result) == 2
    assert all(o.success for o in result)
    assert all(o.failure_phase is None for o in result)
    assert result[0].op_id == "op-aaa"
    assert result[1].op_id == "op-bbb"
    assert result[0].elapsed_s == 10.0  # completed - started


def test_postmortem_reader_maps_must_hold_failed_as_verify_failure():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )
    fake_pms = [
        _make_fake_pm(
            "op-fail",
            must_hold_failed=1,
            has_blocking_failures=True,
        ),
    ]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        result = recent_postmortem_outcomes(limit=1)
    assert len(result) == 1
    assert result[0].success is False
    assert result[0].failure_phase == "VERIFY"


def test_postmortem_reader_maps_error_count_as_terminal_failure():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )
    fake_pms = [
        _make_fake_pm("op-err", error_count=2),
    ]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        result = recent_postmortem_outcomes(limit=1)
    assert result[0].success is False
    assert result[0].failure_phase == "TERMINAL"


def test_postmortem_reader_handles_missing_ledger():
    """Reader NEVER raises when the ledger module/file is absent."""
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        side_effect=RuntimeError("ledger gone"),
    ):
        result = recent_postmortem_outcomes(limit=4)
    assert result == ()


def test_postmortem_reader_skips_malformed_records():
    """A record that raises during projection is silently skipped;
    valid records still flow through."""
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )

    class _Bad:
        @property
        def op_id(self):
            raise RuntimeError("bad attribute access")

    fake_pms = [
        _make_fake_pm("op-good"),
        _Bad(),
        _make_fake_pm("op-also-good"),
    ]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        result = recent_postmortem_outcomes(limit=3)
    op_ids = {o.op_id for o in result}
    assert "op-good" in op_ids
    assert "op-also-good" in op_ids
    assert len(result) == 2  # bad one skipped


def test_postmortem_reader_fields_left_empty_for_slice3():
    """Operator binding: ``op_family``, ``risk_tier``,
    ``failed_category`` are NOT populated by the reader — they're
    populated at the orchestrator hook seam in Slice 3 from
    ``ctx.task_complexity`` etc."""
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_postmortem_outcomes,
    )
    fake_pms = [_make_fake_pm("op-1", must_hold_failed=1)]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        (outcome,) = recent_postmortem_outcomes(limit=1)
    assert outcome.op_family == ""
    assert outcome.risk_tier == ""
    assert outcome.failed_category is None


# -----------------------------------------------------------------------
# § D — Adaptation proposal reader behavioral mapping
# -----------------------------------------------------------------------


def _make_fake_proposal(
    proposal_id: str,
    surface_name: str = "iron_gate_exploration_floors",
    decision_value: str = "pending",
):
    """Build a duck-typed AdaptationProposal clone for tests."""
    from types import SimpleNamespace
    surface = SimpleNamespace(value=surface_name)
    decision = SimpleNamespace(value=decision_value)
    return SimpleNamespace(
        proposal_id=proposal_id,
        surface=surface,
        operator_decision=decision,
    )


def test_adaptation_reader_maps_pending_proposals():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_adaptation_proposals,
    )

    class _FakeLedger:
        def history(self, limit=100):
            return (
                _make_fake_proposal("p-1", "iron_gate_exploration_floors", "pending"),
                _make_fake_proposal("p-2", "semantic_guardian_patterns", "approved"),
                _make_fake_proposal("p-3", "per_order_mutation_budget", "rejected"),
            )

    with patch(
        "backend.core.ouroboros.governance.adaptation.ledger"
        ".get_default_ledger",
        return_value=_FakeLedger(),
    ):
        result = recent_adaptation_proposals(limit=3)
    assert len(result) == 3
    assert result[0].operator_outcome == "pending"
    assert result[1].operator_outcome == "approved"
    assert result[2].operator_outcome == "rejected"
    assert result[1].surface == "semantic_guardian_patterns"


def test_adaptation_reader_normalizes_applied_to_approved():
    """``applied`` (post-approve enforcement state) collapses to
    ``approved`` per the documented 3-value vocabulary."""
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_adaptation_proposals,
    )

    class _FakeLedger:
        def history(self, limit=100):
            return (
                _make_fake_proposal("p-1", "x", "applied"),
            )

    with patch(
        "backend.core.ouroboros.governance.adaptation.ledger"
        ".get_default_ledger",
        return_value=_FakeLedger(),
    ):
        (rec,) = recent_adaptation_proposals(limit=1)
    assert rec.operator_outcome == "approved"


def test_adaptation_reader_handles_ledger_failure():
    from backend.core.ouroboros.governance.auto_action_router import (
        recent_adaptation_proposals,
    )
    with patch(
        "backend.core.ouroboros.governance.adaptation.ledger"
        ".get_default_ledger",
        side_effect=RuntimeError("ledger gone"),
    ):
        result = recent_adaptation_proposals(limit=4)
    assert result == ()


# -----------------------------------------------------------------------
# § E — gather_context composes all three readers
# -----------------------------------------------------------------------


def test_gather_context_assembles_input_dataclass(monkeypatch):
    from backend.core.ouroboros.governance.auto_action_router import (
        gather_context, AutoActionContext,
    )
    fake_pms = [_make_fake_pm("op-x")]
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        return_value=tuple(fake_pms),
    ):
        ctx = gather_context(
            current_op_family="test_failure",
            current_risk_tier="SAFE_AUTO",
            current_route="immediate",
            posture="EXPLORE",
        )
    assert isinstance(ctx, AutoActionContext)
    assert len(ctx.recent_outcomes) == 1
    assert ctx.recent_outcomes[0].op_id == "op-x"
    assert ctx.current_op_family == "test_failure"
    assert ctx.current_risk_tier == "SAFE_AUTO"
    assert ctx.current_route == "immediate"
    assert ctx.posture == "EXPLORE"


def test_gather_context_never_raises_on_reader_failure():
    """All three readers return () on failure; gather_context still
    produces a valid AutoActionContext."""
    from backend.core.ouroboros.governance.auto_action_router import (
        gather_context, AutoActionContext, _verdict_buffer,
    )
    # Clear the Slice 3 ring buffer so prior-test state doesn't
    # bleed into this assertion (the verdict reader pulls from
    # the in-process buffer, which other tests may have populated).
    _verdict_buffer.clear()
    with patch(
        "backend.core.ouroboros.governance.verification.postmortem"
        ".list_recent_postmortems",
        side_effect=RuntimeError("postmortem ledger gone"),
    ), patch(
        "backend.core.ouroboros.governance.adaptation.ledger"
        ".get_default_ledger",
        side_effect=RuntimeError("adaptation ledger gone"),
    ):
        ctx = gather_context()
    assert isinstance(ctx, AutoActionContext)
    assert ctx.recent_outcomes == ()
    assert ctx.recent_verdicts == ()
    assert ctx.recent_proposals == ()


# -----------------------------------------------------------------------
# § F — End-to-end: gather_context → propose_advisory_action
# -----------------------------------------------------------------------


def test_end_to_end_pipeline_master_off_returns_no_action(monkeypatch):
    """Post-graduation: env-unset = default-on, so testing the
    master-off path requires explicit JARVIS_AUTO_ACTION_ROUTER_ENABLED=0."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "0")
    from backend.core.ouroboros.governance.auto_action_router import (
        gather_context, propose_advisory_action, AdvisoryActionType,
    )
    ctx = gather_context()
    result = propose_advisory_action(ctx)
    assert result.action_type is AdvisoryActionType.NO_ACTION
    assert result.reason_code == "master_flag_off"


def test_end_to_end_pipeline_with_real_signal(monkeypatch):
    """Master-on, postmortem reader returns failures for a target
    family → dispatcher proposes DEMOTE_RISK_TIER on SAFE_AUTO."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        gather_context, propose_advisory_action, AdvisoryActionType,
        RecentOpOutcome, AutoActionContext,
    )
    # Slice 2's reader leaves op_family empty, so we synthesize the
    # context directly here to test the e2e contract. (Slice 3 will
    # populate op_family at the hook seam from ctx.)
    outcomes = tuple(
        RecentOpOutcome(
            op_id=f"op-{i}",
            op_family="doc_staleness",
            success=False,
            risk_tier="SAFE_AUTO",
        )
        for i in range(3)
    )
    ctx = AutoActionContext(
        recent_outcomes=outcomes,
        current_op_family="doc_staleness",
        current_risk_tier="SAFE_AUTO",
        current_route="background",
    )
    result = propose_advisory_action(ctx)
    assert result.action_type is AdvisoryActionType.DEMOTE_RISK_TIER


# -----------------------------------------------------------------------
# § G — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_authority():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "providers", "orchestrator", "doubleword_provider",
        "candidate_generator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"
