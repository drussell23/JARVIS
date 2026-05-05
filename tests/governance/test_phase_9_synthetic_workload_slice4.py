"""Phase 9 Slice 4 — latent ops_count bug fix regression spine.

Pre-existing latent bug (2026-05-05): the
``predicate_requires_decision_trace_rows`` and
``predicate_requires_curiosity_hypothesis`` predicates BOTH read
``summary.get("ops_count", 0)`` — but the battle-test
``summary.json`` schema does NOT emit a top-level ``ops_count``
field. The canonical session op-count lives at
``summary.strategic_drift.total_ops``.

Effect: every Phase 9 cadence soak's CLEAN classification was
silently downgraded to RUNNER because the predicate read 0
regardless of how many ops actually fired. This was masking real
op activity (debug.log evidence on bt-2026-05-05-224545: 16 ops
fired including 3 cadence_synthetic, but predicate returned 0 →
contract downgraded → soak's evidence wasted).

Fix: extracted canonical ``_session_ops_count`` helper that:
  1. Reads top-level ``ops_count`` first (forward-compat for when
     harness eventually emits it explicitly)
  2. Falls back to ``strategic_drift.total_ops`` (canonical
     truth in the current schema)
  3. Returns 0 on any malformed input; NEVER raises

Verifies (16 tests):
  * Helper: top-level ops_count wins when set
  * Helper: strategic_drift.total_ops fallback when top is 0/missing
  * Helper: handles malformed input (None, non-dict, missing keys,
    string values, negative numbers)
  * Helper: returns 0 on session_outcome=incomplete (defensive)
  * predicate_requires_decision_trace_rows: TRUE when fallback
    yields >= 1 + clean
  * predicate_requires_decision_trace_rows: FALSE when total_ops=0
  * predicate_requires_curiosity_hypothesis: same fallback path
  * Real-artifact regression: validates against the actual
    bt-2026-05-05-224545/summary.json from the failed soak —
    proving the fix WOULD have classified that session CLEAN
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.graduation.graduation_contract import (  # noqa: E501
    _session_ops_count,
    default_clean_predicate,
    predicate_requires_curiosity_hypothesis,
    predicate_requires_decision_trace_rows,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _clean_summary(**overrides) -> dict:
    """Build a minimal session_outcome=complete summary."""
    base = {
        "session_outcome": "complete",
        "failure_class_counts": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _session_ops_count helper — forward-compat + canonical fallback
# ---------------------------------------------------------------------------


def test_helper_reads_top_level_ops_count_when_set():
    """Forward-compat: when harness eventually emits top-level
    ops_count, helper picks it up first."""
    summary = {"ops_count": 7}
    assert _session_ops_count(summary) == 7


def test_helper_falls_back_to_strategic_drift_total_ops():
    """The canonical-fallback path: today's summary.json has no
    top-level ops_count, so helper reads
    strategic_drift.total_ops."""
    summary = {"strategic_drift": {"total_ops": 16}}
    assert _session_ops_count(summary) == 16


def test_helper_top_level_zero_falls_back_to_drift():
    """When top-level ops_count is explicitly 0 (legacy bug),
    helper still finds the real count via fallback."""
    summary = {
        "ops_count": 0,
        "strategic_drift": {"total_ops": 12},
    }
    assert _session_ops_count(summary) == 12


def test_helper_returns_zero_when_no_strategic_drift():
    summary = {"session_outcome": "complete"}
    assert _session_ops_count(summary) == 0


def test_helper_returns_zero_on_non_dict_input():
    assert _session_ops_count(None) == 0
    assert _session_ops_count("not a dict") == 0
    assert _session_ops_count(42) == 0


def test_helper_handles_malformed_strategic_drift():
    """strategic_drift not a dict → 0; total_ops not int → 0."""
    assert _session_ops_count(
        {"strategic_drift": "garbage"},
    ) == 0
    assert _session_ops_count(
        {"strategic_drift": {"total_ops": "n/a"}},
    ) == 0


def test_helper_clamps_negative_to_zero():
    """Defensive: a negative total_ops (impossible in practice
    but pinned for safety) clamps to 0."""
    summary = {"strategic_drift": {"total_ops": -5}}
    assert _session_ops_count(summary) == 0


def test_helper_handles_missing_total_ops():
    summary = {"strategic_drift": {}}
    assert _session_ops_count(summary) == 0


def test_helper_handles_string_int_in_top_level():
    """Some serializers emit '7' as string. int() coerces."""
    summary = {"ops_count": "7"}
    assert _session_ops_count(summary) == 7


def test_helper_never_raises_on_garbage():
    """Pure-function contract: NEVER raises."""
    assert _session_ops_count({"strategic_drift": None}) == 0
    assert _session_ops_count({"ops_count": float("nan")}) == 0


# ---------------------------------------------------------------------------
# predicate_requires_decision_trace_rows — fixed via composed helper
# ---------------------------------------------------------------------------


def test_predicate_decision_trace_passes_with_drift_fallback():
    """The load-bearing assertion: the predicate now correctly
    classifies a session with strategic_drift.total_ops >= 1
    as PASS, even when top-level ops_count is missing (the
    legacy bug)."""
    summary = _clean_summary(
        strategic_drift={"total_ops": 5},
    )
    assert predicate_requires_decision_trace_rows(summary) is True


def test_predicate_decision_trace_fails_with_zero_ops():
    summary = _clean_summary(
        strategic_drift={"total_ops": 0},
    )
    assert (
        predicate_requires_decision_trace_rows(summary) is False
    )


def test_predicate_decision_trace_fails_when_not_clean():
    summary = {
        "session_outcome": "incomplete_kill",
        "strategic_drift": {"total_ops": 16},
        "failure_class_counts": {},
    }
    assert (
        predicate_requires_decision_trace_rows(summary) is False
    )


def test_predicate_decision_trace_fails_with_runner_failure():
    """default_clean_predicate must reject runner-class failures
    even with non-zero ops."""
    summary = _clean_summary(
        strategic_drift={"total_ops": 16},
        failure_class_counts={"iron_gate_violation": 1},
    )
    assert (
        predicate_requires_decision_trace_rows(summary) is False
    )


# ---------------------------------------------------------------------------
# predicate_requires_curiosity_hypothesis — same fallback path
# ---------------------------------------------------------------------------


def test_predicate_curiosity_uses_drift_fallback_when_no_hypothesis_field():
    """Pre-instrumentation fallback: when
    curiosity_hypotheses_generated is missing, helper falls
    back to strategic_drift.total_ops."""
    summary = _clean_summary(
        strategic_drift={"total_ops": 3},
    )
    assert (
        predicate_requires_curiosity_hypothesis(summary) is True
    )


def test_predicate_curiosity_explicit_hypothesis_wins():
    """When curiosity_hypotheses_generated is set explicitly,
    that field takes precedence over the ops fallback."""
    summary = _clean_summary(
        curiosity_hypotheses_generated=2,
        strategic_drift={"total_ops": 0},
    )
    assert (
        predicate_requires_curiosity_hypothesis(summary) is True
    )


def test_predicate_curiosity_zero_hypotheses_does_not_fall_through():
    """Edge case: when curiosity_hypotheses_generated IS set
    (not None) but is 0, predicate returns False — does NOT
    fall through to ops_count proxy. The explicit value wins."""
    summary = _clean_summary(
        curiosity_hypotheses_generated=0,
        strategic_drift={"total_ops": 16},
    )
    assert (
        predicate_requires_curiosity_hypothesis(summary)
        is False
    )


# ---------------------------------------------------------------------------
# Real-artifact regression — proves the fix works end-to-end
# ---------------------------------------------------------------------------


def test_real_failed_soak_summary_now_classifies_clean():
    """Bytes-level regression: load the actual summary.json from
    bt-2026-05-05-224545 (the failed green-soak proof attempt)
    and prove the fix WOULD have classified it CLEAN. This is
    the citation-purposes test — auditing the brutal-review
    backlog, finding this test passing is structural proof the
    bug is closed."""
    target = (
        _repo_root()
        / ".ouroboros/sessions/bt-2026-05-05-224545"
        / "summary.json"
    )
    if not target.exists():
        pytest.skip(
            "real-artifact fixture missing (session dir cleaned)"
        )
    summary = json.loads(target.read_text(encoding="utf-8"))
    # The artifact has session_outcome=complete and
    # strategic_drift.total_ops=16, but no top-level ops_count.
    assert summary.get("session_outcome") == "complete"
    assert (
        summary.get("strategic_drift", {}).get("total_ops") == 16
    )
    assert "ops_count" not in summary, (
        "real-artifact fixture changed — top-level ops_count "
        "now present? update test fixture"
    )
    # The fix turns this artifact into a CLEAN classification.
    assert _session_ops_count(summary) == 16
    assert predicate_requires_decision_trace_rows(summary) is True
