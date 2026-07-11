"""Slice 4 T2 — budget refusal is a LOCAL config gate, not a REMOTE outage.

Run #14 counted 30 ``SessionBudgetPreflightRefused`` preflight refusals (the
session budget was $0.00) as provider transport failures → 27 dual-arm model
blacklists → a 79-minute ``dw_global_outage`` global quarantine → 12 goals
orphaned to the DLQ, plus unbounded ``[Immortal]`` retry loops holding worker
slots against an unpassable wall.

These tests pin the failure-taxonomy fix:

  * :func:`session_budget_authority.is_budget_refusal` classifies a refusal
    even when wrapped by a transport arm (walks ``__cause__``/``__context__``).
  * :func:`race_triage.is_budget_refusal_pair` short-circuits the dual-arm
    CONFIRMED blacklist path when both observed arms are budget refusals.
  * The ``candidate_generator`` sentinel dispatch fails FAST on a pure
    budget-refusal exhaustion BEFORE it can feed the outage/quarantine
    gradient or wake the (real-$) J-Prime GCE failover.

NOTE (brief-correction): the brief's assumed ``SessionBudgetPreflightRefused``
kwargs (``estimated_usd`` / ``effective_remaining_usd``) do not match the real
constructor at session_budget_authority.py:95-111, which is keyword-only
``provider`` / ``estimated_cost_usd`` / ``session_remaining_usd`` / ``reason``.
These tests use the real constructor.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.session_budget_authority import (
    SessionBudgetPreflightRefused,
    is_budget_refusal,
)


def _refusal() -> SessionBudgetPreflightRefused:
    return SessionBudgetPreflightRefused(
        provider="doubleword",
        estimated_cost_usd=0.10,
        session_remaining_usd=0.0,
    )


# ---------------------------------------------------------------------------
# is_budget_refusal — chain-walking classifier
# ---------------------------------------------------------------------------

def test_direct_refusal_detected():
    assert is_budget_refusal(_refusal()) is True


def test_wrapped_refusal_detected_via_cause_chain():
    inner = _refusal()
    try:
        try:
            raise inner
        except SessionBudgetPreflightRefused as e:
            raise RuntimeError("transport wrapper") from e
    except RuntimeError as outer:
        assert is_budget_refusal(outer) is True


def test_wrapped_refusal_detected_via_implicit_context():
    """A bare ``raise`` inside an ``except`` sets ``__context__`` (not
    ``__cause__``) — the classifier must still see through it."""
    inner = _refusal()
    try:
        try:
            raise inner
        except SessionBudgetPreflightRefused:
            raise RuntimeError("implicit-context wrapper")
    except RuntimeError as outer:
        assert is_budget_refusal(outer) is True


def test_ordinary_exception_not_refusal():
    assert is_budget_refusal(TimeoutError("provider timed out")) is False


def _build_cause_chain(n_wrappers: int) -> BaseException:
    """Build a REAL ``__cause__`` chain: ``outer <- wrapper_n <- ... <-
    wrapper_1 <- refusal``, via genuine ``raise ... from`` semantics (not a
    fabricated/discarded local). ``n_wrappers`` is the ``__cause__``-hop
    distance from the returned outer exception down to the buried
    :class:`SessionBudgetPreflightRefused`.
    """
    cur: BaseException = _refusal()
    for i in range(n_wrappers):
        try:
            raise cur
        except BaseException as e:  # noqa: BLE001 — building a real cause chain
            wrapper = RuntimeError(f"transport wrapper {i}")
            wrapper.__cause__ = e
            cur = wrapper
    return cur


def test_depth_bound_terminates_on_long_chain():
    """The walk is bounded (default ``_depth=8``): it inspects the outer
    exception plus its next 7 ``__cause__`` hops (cause-distance 0..7 from
    the outer, 8 checks total). A refusal buried strictly deeper than that
    (cause-distance >= 8) is unreachable and must classify False — proving
    the bound actually terminates the walk rather than traversing forever.
    """
    outer = _build_cause_chain(9)  # refusal at cause-distance 9 > reachable max (7)
    assert is_budget_refusal(outer) is False


def test_depth_bound_boundary_finds_refusal_within_bound():
    """Complement of the above: a refusal at the maximum *reachable*
    cause-distance (7, the last hop the ``seen < _depth`` loop still visits
    under the default ``_depth=8``) must still classify True. Together with
    ``test_depth_bound_terminates_on_long_chain`` this pins the exact
    boundary the guard enforces."""
    outer = _build_cause_chain(7)  # refusal at cause-distance 7 == reachable max
    assert is_budget_refusal(outer) is True


# ---------------------------------------------------------------------------
# race_triage.is_budget_refusal_pair — the dual-arm short-circuit seam
# ---------------------------------------------------------------------------

def test_race_triage_skips_blacklist_on_budget_refusal():
    """Both arms failing on a LOCAL budget gate must not blacklist the model
    (Run #14: 27 spurious dual-arm blacklists → global quarantine)."""
    from backend.core.ouroboros.governance import race_triage as rt
    assert rt.is_budget_refusal_pair(_refusal(), _refusal()) is True


def test_pair_false_when_an_arm_is_a_real_transport_fault():
    from backend.core.ouroboros.governance import race_triage as rt
    assert rt.is_budget_refusal_pair(
        _refusal(), TimeoutError("transport rupture"),
    ) is False


def test_pair_true_when_other_arm_absent_but_present_arm_is_refusal():
    """A missing (None) arm carries no evidence; the single observed refusal
    still classifies the pair as a budget refusal."""
    from backend.core.ouroboros.governance import race_triage as rt
    assert rt.is_budget_refusal_pair(_refusal(), None) is True


def test_pair_false_when_both_arms_absent():
    from backend.core.ouroboros.governance import race_triage as rt
    assert rt.is_budget_refusal_pair(None, None) is False


def test_dual_failure_verdict_is_not_hard_blockage_for_refusal_pair():
    """The public triage verdict itself must refuse to call a budget-refusal
    pair a hard model blockage — that verdict is what drives the blacklist
    write in record_dual_arm_blacklist."""
    from backend.core.ouroboros.governance import race_triage as rt
    verdict = rt.triage_dual_failure(_refusal(), _refusal())
    assert verdict.hard_blockage is False


# ---------------------------------------------------------------------------
# candidate_generator — source-level precedence pins (idiomatic in this repo,
# cf. test_topology_sentinel_dispatch.py). The budget fail-fast MUST precede
# the outage/quarantine gradient AND the note_budget_exhausted awaken, so a
# pure budget-refusal exhaustion can never reach them (real-$ GCE ignition).
# ---------------------------------------------------------------------------

def _sentinel_src() -> str:
    from backend.core.ouroboros.governance import candidate_generator as cg
    return inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)


def test_sentinel_has_budget_refusal_failfast_terminal_cause():
    assert "budget_exhausted_non_transient" in _sentinel_src()


def test_budget_failfast_precedes_immortal_awaken_and_quarantine():
    src = _sentinel_src()
    # Anchor on call syntax (parens) so prose/comments never match — only the
    # real dispatch sites count.
    ff = src.index("budget_exhausted_non_transient")
    awaken = src.index(".note_budget_exhausted()")
    quarantine = src.index(".is_global_outage(")
    assert ff < awaken, (
        "budget fail-fast must precede the note_budget_exhausted awaken so a "
        "local $0.00 gate never ignites the J-Prime GCE failover"
    )
    assert ff < quarantine, (
        "budget fail-fast must precede the is_global_outage quarantine consult "
        "so a local budget gate never trips a phantom dw_global_outage"
    )


def test_health_gradient_failure_sweep_is_guarded_for_pure_budget_exhaustion():
    """The per-exhaustion failure sweep (record_sweep success=False) feeds the
    is_global_outage deduction; it must be skipped when the exhaustion was a
    pure budget refusal, else the outage gradient is poisoned across ops."""
    src = _sentinel_src()
    assert "_saw_non_refusal_failure" in src
    assert "record_sweep" in src
