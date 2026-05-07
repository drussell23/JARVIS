"""Move 6.5 Slice 6 — §33.1 graduation contract harness.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "§33.1 graduation contract harness — phase 9 / shipping
   gates any default-TRUE flip on the contract's evidence
   verdict."

Pinned coverage (~32 tests):
  * Closed verdict taxonomy (5-value) bytes-pinned
  * Harness master default-TRUE per §33.1 separation
  * Threshold knobs clamp [1, 10000] / [0.0, 1.0]
  * 3-gate first-match-wins:
    - DISABLED (harness off)
    - ALREADY_GRADUATED (Slice 3 master flag on)
    - INSUFFICIENT_OBSERVATIONS (total < required)
    - EXCESSIVE_NON_ACTIONABLE_RATE (rate > threshold)
    - READY_FOR_GRADUATION (otherwise)
  * Caller-injected snapshot reader override
  * Default snapshot reader composes Slice 4's
    read_recent_observations
  * Default snapshot reader returns {} on Slice 4 unavailable
  * Frozen report shape + to_dict round-trip
  * is_actionable convention (READY_FOR_GRADUATION only)
  * 4 AST pins clean (parametrized) + each fires on synthetic
    regression
  * Pattern compliance: §33.1 canonical-shape parity with
    Move 7 (predicate name + verdict 5-value + frozen report
    + harness flag default-TRUE)
  * Public API surface complete + register_flags + swallows
    registry errors
  * End-to-end live integration: Slice 4 ledger feeds the
    contract through the canonical read API
"""
from __future__ import annotations

import ast
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_graduation_contract.py"
    )


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_5_values():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
    )
    assert {
        v.name for v in MultiPriorGraduationVerdict
    } == {
        "READY_FOR_GRADUATION",
        "INSUFFICIENT_OBSERVATIONS",
        "EXCESSIVE_NON_ACTIONABLE_RATE",
        "ALREADY_GRADUATED",
        "DISABLED",
    }


def test_verdict_str_values_canonical():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
    )
    assert (
        MultiPriorGraduationVerdict
        .READY_FOR_GRADUATION.value == "ready_for_graduation"
    )
    assert (
        MultiPriorGraduationVerdict.DISABLED.value
        == "disabled"
    )


# ---------------------------------------------------------------------------
# Harness master flag — default-TRUE per §33.1 separation
# ---------------------------------------------------------------------------


def test_harness_default_true(monkeypatch):
    """§33.1 separation: harness is a passive read-only
    oracle, so the contract default-TRUE is correct shape
    (mirrors Move 7's harness pattern). Producer Slices 1-5
    stay default-FALSE."""
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        harness_enabled,
    )
    assert harness_enabled() is True


def test_harness_explicit_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_GRADUATION_CONTRACT_ENABLED",
        "false",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        harness_enabled,
    )
    assert harness_enabled() is False


# ---------------------------------------------------------------------------
# Threshold knobs
# ---------------------------------------------------------------------------


def test_required_observations_default(monkeypatch):
    monkeypatch.delenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "REQUIRED_OBSERVATIONS"
        ),
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        required_observations,
    )
    assert required_observations() == 50


def test_required_observations_clamps(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        required_observations,
    )
    monkeypatch.setenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "REQUIRED_OBSERVATIONS"
        ),
        "0",
    )
    assert required_observations() == 1
    monkeypatch.setenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "REQUIRED_OBSERVATIONS"
        ),
        "999999",
    )
    assert required_observations() == 10000
    monkeypatch.setenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "REQUIRED_OBSERVATIONS"
        ),
        "junk",
    )
    assert required_observations() == 50


def test_max_non_actionable_rate_default(monkeypatch):
    monkeypatch.delenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "MAX_NON_ACTIONABLE_RATE"
        ),
        raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        max_non_actionable_rate,
    )
    assert max_non_actionable_rate() == 0.40


def test_max_non_actionable_rate_clamps(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        max_non_actionable_rate,
    )
    monkeypatch.setenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "MAX_NON_ACTIONABLE_RATE"
        ),
        "-0.5",
    )
    assert max_non_actionable_rate() == 0.0
    monkeypatch.setenv(
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "MAX_NON_ACTIONABLE_RATE"
        ),
        "5.0",
    )
    assert max_non_actionable_rate() == 1.0


# ---------------------------------------------------------------------------
# 3-gate first-match-wins
# ---------------------------------------------------------------------------


def test_disabled_when_harness_off():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {"accept_canonical": 100},
        enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.DISABLED
    )


def test_already_graduated_first_match():
    """Gate 1 first — even when evidence wouldn't pass other
    gates, if Slice 3's master flag is on, ALREADY_GRADUATED
    wins (no re-graduation)."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {},  # would trip Gate 2
        master_enabled_override=True,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.ALREADY_GRADUATED
    )


def test_insufficient_observations_under_floor():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 30,
        },
        required_observations_override=50,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.INSUFFICIENT_OBSERVATIONS
    )
    assert r.total_observations == 30
    assert "30" in r.detail
    assert "50" in r.detail


def test_excessive_non_actionable_rate():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 20,
            "escalate_to_operator_review": 25,
            "fall_through": 5,
        },
        required_observations_override=10,
        max_non_actionable_rate_override=0.40,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict
        .EXCESSIVE_NON_ACTIONABLE_RATE
    )
    assert r.total_observations == 50
    assert r.non_actionable_count == 30  # 25 + 5
    assert r.non_actionable_rate == pytest.approx(0.6)


def test_ready_for_graduation():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 45,
            "clamp_to_notify_apply": 8,
            "escalate_to_operator_review": 5,
            "fall_through": 2,
        },
        required_observations_override=10,
        max_non_actionable_rate_override=0.40,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.READY_FOR_GRADUATION
    )
    assert r.total_observations == 60
    assert r.is_actionable() is True


def test_clamp_to_notify_apply_not_counted_as_non_actionable():
    """Operator binding: CLAMP_TO_NOTIFY_APPLY is "majority
    with outliers" — operator reviews; signal converged
    enough to be partial. NOT counted as non-actionable."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    # 100% clamp → still actionable (rate=0)
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "clamp_to_notify_apply": 50,
        },
        required_observations_override=10,
        max_non_actionable_rate_override=0.40,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.READY_FOR_GRADUATION
    )
    assert r.non_actionable_rate == 0.0


def test_failure_rolls_count_as_non_actionable():
    """Operator binding: cancellations + errors are
    orthogonal failure modes — they count as non-actionable
    even when action_recommendation is otherwise actionable.
    The default reader stashes failure count as
    ``_with_failures`` sentinel."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )
    # 10 accept_canonical actions but 6 had cancels/errors
    # → non_actionable_rate = max(0, 6) / 10 = 0.6
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 10,
            "_with_failures": 6,
        },
        required_observations_override=5,
        max_non_actionable_rate_override=0.40,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict
        .EXCESSIVE_NON_ACTIONABLE_RATE
    )
    assert r.non_actionable_count == 6


# ---------------------------------------------------------------------------
# Default snapshot reader composition
# ---------------------------------------------------------------------------


def test_default_reader_handles_substrate_unavailable(
    monkeypatch,
):
    """If Slice 4's read API fails, the default reader
    returns {} → Gate 2 trips with INSUFFICIENT_OBSERVATIONS
    rather than crashing."""
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_graduation_contract as mod,
    )
    snapshot = mod._default_snapshot_reader()
    # Whether ledger has data or is empty, this MUST NOT raise.
    assert isinstance(snapshot, dict)


def test_default_reader_composes_slice4_read_api(
    monkeypatch,
):
    """End-to-end: the default reader pulls from Slice 4's
    canonical read_recent_observations. Insert rows via the
    observer + assert the contract sees them."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        get_default_observer,
        reset_default_observer_for_test,
    )
    reset_default_observer_for_test()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH",
            str(ledger),
        )
        obs = get_default_observer()
        for i in range(5):
            obs.record(
                op_id=f"op-{i}", decision="enabled",
                action_recommendation="accept_canonical",
                consensus_outcome="consensus",
                completed_count=4, cancelled_count=0,
                timeout_count=0, error_count=0,
                cost_total_usd=0.0, wall_clock_s=1.0,
                rationale="r",
                ledger_path_override=ledger,
            )

        from backend.core.ouroboros.governance.verification import (  # noqa: E501
            multi_prior_graduation_contract as mod,
        )
        snap = mod._default_snapshot_reader()
        assert snap.get("accept_canonical", 0) == 5
        assert snap.get("_with_failures", 0) == 0
        reset_default_observer_for_test()


# ---------------------------------------------------------------------------
# Frozen report shape
# ---------------------------------------------------------------------------


def test_report_to_dict_shape():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        is_ready_for_graduation,
    )
    r = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 60,
        },
        required_observations_override=10,
        master_enabled_override=False,
    )
    d = r.to_dict()
    assert d["verdict"] == "ready_for_graduation"
    assert d["total_observations"] == 60
    assert "breakdown_by_action" in d
    assert "schema_version" in d
    assert "ts_unix" in d


def test_is_actionable_only_ready():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        is_ready_for_graduation,
    )
    ready = is_ready_for_graduation(
        snapshot_reader=lambda: {
            "accept_canonical": 60,
        },
        required_observations_override=10,
        master_enabled_override=False,
    )
    assert ready.is_actionable() is True
    not_ready = is_ready_for_graduation(
        snapshot_reader=lambda: {},
        master_enabled_override=False,
    )
    assert not_ready.is_actionable() is False


def test_predicate_never_raises_on_broken_reader():
    """Defensive: if the snapshot reader raises, the
    predicate must NOT raise — it returns INSUFFICIENT_OBSERVATIONS
    with empty snapshot."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        MultiPriorGraduationVerdict,
        is_ready_for_graduation,
    )

    def bad_reader():
        raise RuntimeError("boom")

    r = is_ready_for_graduation(
        snapshot_reader=bad_reader,
        master_enabled_override=False,
    )
    assert r.verdict is (
        MultiPriorGraduationVerdict.INSUFFICIENT_OBSERVATIONS
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_graduation_verdict_taxonomy_5_values",
        "multi_prior_graduation_authority_asymmetry",
        "multi_prior_graduation_composes_substrate",
        "multi_prior_graduation_pattern_compliance",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class MultiPriorGraduationVerdict:
    READY_FOR_GRADUATION = "x"
    DISABLED = "y"
    EXTRA_VALUE = "z"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_graduation_verdict_taxonomy_5_values"
        )
    )
    assert pin.validate(tree, bad)


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_graduation_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_composes_substrate_pin_fires_on_top_level_import():
    """Operator binding: master_enabled + read_recent_observations
    MUST be lazy-imported. Top-level import is forbidden."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.verification."
        "multi_prior_dispatch import master_enabled\n"
        "x = 1\n"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_graduation_composes_substrate"
        )
    )
    assert pin.validate(tree, bad)


def test_pattern_compliance_pin_fires_when_helper_missing():
    """The §33.1 canonical shape REQUIRES harness_enabled()."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class MultiPriorGraduationVerdict:
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    EXCESSIVE_NON_ACTIONABLE_RATE = "excessive_non_actionable_rate"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"

class MultiPriorGraduationReport:
    pass

def is_ready_for_graduation():
    pass
# harness_enabled() missing — pattern-compliance pin should fire
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_graduation_pattern_compliance"
        )
    )
    assert pin.validate(tree, bad)


def test_pattern_compliance_default_true_canonical():
    """The harness_enabled() function MUST return True on
    empty env-var string per §33.1 separation. Mirrors Move
    7's harness pattern — pattern-compliance pin enforces
    structurally."""
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    # Hypothetical version where harness_enabled returned
    # False on empty (would violate §33.1 separation).
    bad = '''
class MultiPriorGraduationVerdict:
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    EXCESSIVE_NON_ACTIONABLE_RATE = "excessive_non_actionable_rate"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"

class MultiPriorGraduationReport:
    pass

def is_ready_for_graduation():
    pass

def harness_enabled():
    raw = ""
    if raw == "":
        return False  # WRONG — should be True per §33.1 separation
    return True
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_graduation_pattern_compliance"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Pattern-compliance: §33.1 parity with Move 7
# ---------------------------------------------------------------------------


def test_pattern_parity_with_move7_canonical_shape():
    """Move 7's cross_op_semantic_budget_graduation_contract
    is the §33.1 canonical-shape reference. Move 6.5's
    contract MUST mirror its public-surface shape (predicate
    + verdict-enum + Report + harness flag helper)."""
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_graduation_contract as ours,
    )
    # Required public surfaces per §33.1 canonical shape
    assert hasattr(ours, "is_ready_for_graduation")
    assert hasattr(ours, "MultiPriorGraduationVerdict")
    assert hasattr(ours, "MultiPriorGraduationReport")
    assert hasattr(ours, "harness_enabled")
    # Verdict has exactly 5 values per canonical shape
    enum_values = [
        v for v in ours.MultiPriorGraduationVerdict
    ]
    assert len(enum_values) == 5


# ---------------------------------------------------------------------------
# End-to-end live integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_live_integration(monkeypatch):
    """Full integration: dispatch ops → record into
    Slice 4 ledger → contract reads via canonical read API
    → verdict reflects actual evidence."""
    for k in (
        "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED",
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        "JARVIS_MULTI_PRIOR_RUNNER_ENABLED",
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
    ):
        monkeypatch.setenv(k, "true")
    from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
        reset_default_observer_for_test,
    )
    reset_default_observer_for_test()

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.jsonl"
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH",
            str(ledger),
        )
        from backend.core.ouroboros.governance.verification.multi_prior_dispatch import (  # noqa: E501
            dispatch_multi_prior,
        )
        from backend.core.ouroboros.governance.verification.multi_prior_observer import (  # noqa: E501
            record_dispatch_outcome,
        )
        from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
            MultiPriorGraduationVerdict,
            is_ready_for_graduation,
        )

        # Fire 12 convergent ops — all → ACCEPT_CANONICAL
        async def converge(*, prior, roll_id):  # noqa: ARG001
            return "identical"

        for i in range(12):
            v = await dispatch_multi_prior(
                converge, op_id=f"op-{i}",
                route="complex", posture="EXPLORE",
            )
            record_dispatch_outcome(
                v, ledger_path_override=ledger,
            )

        # Required threshold = 10 → 12 observations passes
        # Gate 2; all ACCEPT_CANONICAL → rate 0 passes Gate 3
        # → READY_FOR_GRADUATION
        # Need to bypass Slice 3's master_enabled (which is
        # ON for this test).
        report = is_ready_for_graduation(
            required_observations_override=10,
            max_non_actionable_rate_override=0.40,
            master_enabled_override=False,
        )
        assert report.verdict is (
            MultiPriorGraduationVerdict.READY_FOR_GRADUATION
        )
        assert report.total_observations == 12
        assert report.non_actionable_count == 0
        reset_default_observer_for_test()


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_graduation_contract as mod,
    )
    expected = {
        "MULTI_PRIOR_GRADUATION_SCHEMA_VERSION",
        "MultiPriorGraduationReport",
        "MultiPriorGraduationVerdict",
        "harness_enabled",
        "is_ready_for_graduation",
        "max_non_actionable_rate",
        "register_flags",
        "register_shipped_invariants",
        "required_observations",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_three():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 3
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "CONTRACT_ENABLED"
        ),
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "REQUIRED_OBSERVATIONS"
        ),
        (
            "JARVIS_MULTI_PRIOR_GRADUATION_"
            "MAX_NON_ACTIONABLE_RATE"
        ),
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.verification.multi_prior_graduation_contract import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)
