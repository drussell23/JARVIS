"""AdmissionGate Slice 2 — _call_fallback integration regression.

Pins the wiring of the AdmissionGate (Slice 1) +
WaitTimeEstimator (Slice 2) into
``CandidateGenerator._call_fallback``. Strictly verifies:

  * Master flag default-FALSE preserves pre-Slice-2 behavior
    (the gate is constructed but doesn't shed; estimator
    updates still record observations for telemetry).
  * AST authority pin: ``_call_fallback`` body MUST contain the
    documented admission-gate symbols. Slice 3 graduation
    promotes this to a ``shipped_code_invariants`` regression
    pin so a future refactor cannot silently remove the wiring.
  * The CandidateGenerator instance has a ``_wait_estimator``
    attribute after construction.
  * The integration imports are correctly named (the AST pin
    catches typos / renames).

End-to-end behavior tests (actual sem.acquire + observed wait +
EWMA propagation) require an aiosrc CandidateGenerator with
real provider plumbing — those are deferred to a follow-up
integration suite that boots the full provider stack. This
slice's regression spine pins the STRUCTURAL wiring, not the
runtime semantics under live load.
"""
from __future__ import annotations

import ast
import inspect

import pytest


# ---------------------------------------------------------------------------
# §A — Construction wires the estimator
# ---------------------------------------------------------------------------


class TestConstructionWiring:
    def test_candidate_generator_has_wait_estimator_attr(self):
        # Smoke: imports the substrate + verifies the
        # CandidateGenerator class exposes the attribute name we
        # added in Slice 2's __init__ wire-up. Doesn't construct
        # an instance (would require provider plumbing).
        from backend.core.ouroboros.governance.candidate_generator import (  # noqa: E501
            CandidateGenerator,
        )
        # The attribute is set in __init__ — verify the source
        # contains the assignment AST pattern.
        src = inspect.getsource(CandidateGenerator)
        assert "self._wait_estimator" in src, (
            "Slice 2 wire-up missing: CandidateGenerator should "
            "set self._wait_estimator in __init__"
        )

    def test_wait_estimator_imported_from_admission_estimator(self):
        from backend.core.ouroboros.governance import (
            candidate_generator,
        )
        src = inspect.getsource(candidate_generator)
        # Pin the import name + path. Slice 3's
        # shipped_code_invariant promotes this to a structural
        # AST validator.
        assert (
            "admission_estimator" in src
        ), "candidate_generator must import admission_estimator"
        assert (
            "WaitTimeEstimator" in src
        ), (
            "candidate_generator must import the "
            "WaitTimeEstimator class"
        )


# ---------------------------------------------------------------------------
# §B — _call_fallback contains admission-gate dispatch
# ---------------------------------------------------------------------------


class TestCallFallbackWiring:
    @staticmethod
    def _call_fallback_source() -> str:
        from backend.core.ouroboros.governance.candidate_generator import (  # noqa: E501
            CandidateGenerator,
        )
        # Find the _call_fallback method's source. Multiple
        # methods may have similar names; we want the one
        # attached to CandidateGenerator.
        method = getattr(CandidateGenerator, "_call_fallback")
        return inspect.getsource(method)

    def test_call_fallback_imports_admission_gate(self):
        # The integration imports the admission_gate module
        # locally inside _call_fallback (lazy import keeps the
        # caller-side dependency one-way). Verify the import
        # statements are present.
        src = self._call_fallback_source()
        assert "admission_gate" in src, (
            "_call_fallback must import admission_gate"
        )
        assert "AdmissionContext" in src, (
            "_call_fallback must import AdmissionContext"
        )
        assert "compute_admission_decision" in src, (
            "_call_fallback must import "
            "compute_admission_decision"
        )

    def test_call_fallback_references_admission_decision_check(self):
        src = self._call_fallback_source()
        # The shed branch dispatches via _raise_exhausted with
        # cause="pre_admission_shed" — the new structural cause
        # distinguishing pre-admission shedding from
        # fallback_failed.
        assert "pre_admission_shed" in src, (
            "_call_fallback must raise EXHAUSTION with "
            "cause='pre_admission_shed' on shed decisions"
        )
        # The admission decision is_shed() helper drives the
        # branch.
        assert ".is_shed(" in src, (
            "_call_fallback must check .is_shed() on the "
            "admission record"
        )

    def test_call_fallback_updates_wait_estimator(self):
        src = self._call_fallback_source()
        # Post-acquire estimator update — feeds the EWMA so
        # subsequent ops' projections reflect actual queue
        # pressure.
        assert "update_observed" in src, (
            "_call_fallback must call _wait_estimator."
            "update_observed after sem.acquire"
        )

    def test_call_fallback_admission_gate_is_fail_open(self):
        # The integration MUST be wrapped in try/except so a gate
        # bug cannot itself starve a legitimate op. Verify the
        # source contains a defensive "Admission gate degraded —
        # proceeding to acquire" log line, which is the marker
        # of the fail-open path.
        src = self._call_fallback_source()
        assert (
            "Admission gate" in src
            and "degraded" in src
        ), (
            "_call_fallback admission integration must be "
            "fail-open (defensive try/except wrapping the "
            "decision call)"
        )


# ---------------------------------------------------------------------------
# §C — Master flag default preserves pre-Slice-2 behavior
# ---------------------------------------------------------------------------


class TestMasterFlagPreservesBehavior:
    def test_admission_gate_default_on_post_graduation(
        self, monkeypatch,
    ):
        # Graduated 2026-05-02 (Slice 3): default-True.
        monkeypatch.delenv(
            "JARVIS_ADMISSION_GATE_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.admission_gate import (  # noqa: E501
            admission_gate_enabled,
        )
        assert admission_gate_enabled() is True

    def test_disabled_gate_decision_proceeds(self):
        # When master flag off, every admission check yields
        # DISABLED → decision.proceeds() == True →
        # _call_fallback skips the shed branch and proceeds
        # to sem.acquire normally.
        from backend.core.ouroboros.governance.admission_gate import (  # noqa: E501
            AdmissionContext,
            AdmissionDecision,
            compute_admission_decision,
        )
        ctx = AdmissionContext(
            route="immediate",
            remaining_s=10.0,  # would normally shed
            queue_depth=20,    # would normally shed
            projected_wait_s=200.0,  # would normally shed
            op_id="op-test",
        )
        rec = compute_admission_decision(ctx, enabled=False)
        assert rec.decision is AdmissionDecision.DISABLED
        assert rec.proceeds() is True
        assert rec.is_shed() is False


# ---------------------------------------------------------------------------
# §D — Cross-module contract: the new exhaustion cause is recognized
# ---------------------------------------------------------------------------


class TestCausePropagation:
    def test_pre_admission_shed_is_distinct_cause_string(self):
        # The new cause MUST be the literal string the AST pin
        # in Slice 2 search for. Pin the literal so any rename
        # in candidate_generator surfaces as a test failure.
        expected = "pre_admission_shed"
        assert expected != "fallback_failed", (
            "pre_admission_shed MUST be distinct from "
            "fallback_failed for observability to distinguish "
            "structural sheds from API timeouts"
        )
        # And it must appear in candidate_generator's source
        # (verified by §B above; this test pins the canonical
        # string).
        from backend.core.ouroboros.governance import (
            candidate_generator,
        )
        src = inspect.getsource(candidate_generator)
        assert expected in src
