"""Slice 12AH — Synthetic GENERATE bypass for wiring-validation fixtures.

# Wedge (bt-2026-05-24-080247)

Slices 12AC + 12AD + 12AE + 12AF are all mechanically validated, but
the wiring-validation smoke fixture STILL cannot reach COMPLETE
because of a fundamental protocol contradiction:

  * SWE-Bench-Pro canonical protocol: ``target_files=()`` (cheat
    detection — test_patch paths must NOT be surfaced; gold_patch
    paths would leak the solution)
  * Smoke fixture has ``gold_patch=""`` (no real task → no real
    target to localise)
  * GENERATE has nothing to point at → produces no candidate
  * VALIDATE has ``n_cands=1`` (stub) but ``micro_fix_skipped_no_target``
  * L2 cancels with 3 insufficient claims
  * Op terminates NOT-COMPLETE

# Fix (Slice 12AH — operator-approved Option 2 over Option 1)

Operator explicitly rejected Option 1 (conditional target_files
plumbing — would compromise the cheat-detection contract). Option 2:
**fixture-aware GENERATE short-circuit**. The structurally correct
answer for any wiring-validation fixture is "no patch needed"
(the existing test trivially passes). Synthesize a ``GenerationResult
(is_noop=True)`` at GENERATE entry, BEFORE any provider call. The
existing noop terminal handler at generate_runner.py:~2163 then
flows the op naturally through APPLY-skip → COMPLETE.

# Test surface

  1. Detector composed: synthetic noop fires ONLY for
     ``is_route_wiring_validation_envelope(ctx) is True``.
  2. Real benchmark envelope (real_benchmark=True) NEVER triggers
     the synthetic noop — provider cascade runs normally.
  3. Empty / malformed envelope (NO_PROMISE) NEVER triggers the
     synthetic noop — legacy path preserved.
  4. AST pin: ``generate_runner.py`` imports
     ``is_route_wiring_validation_envelope`` AND constructs
     ``GenerationResult(is_noop=True, ...)`` AND uses the new
     ``Slice12AH`` log marker.
  5. AST pin: synthetic provider_name is ``slice_12ah_synthetic_noop``
     (greppable for operator forensics).
  6. AST pin: the synthetic-noop pre-set is FOLLOWED by an
     early-break in the retry loop (otherwise the first iteration
     would overwrite it via ``orch._generator.generate(...)``).
  7. Canonical GenerationResult fields preserved (candidates=(),
     generation_duration_s=0.0, is_noop=True).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.envelope_metadata import (
    EVIDENCE_KEY_FIXTURE_PURPOSE,
    EVIDENCE_KEY_GOLD_PATCH_EMPTY,
    EVIDENCE_KEY_REAL_BENCHMARK,
    EVIDENCE_KEY_SWE_BENCH_PRO,
    is_route_wiring_validation_envelope,
)
from backend.core.ouroboros.governance.op_context import GenerationResult


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _make_ctx(evidence: dict | None):
    """Minimal duck-typed ctx for is_route_wiring_validation_envelope."""
    ctx = MagicMock()
    ctx.intake_evidence_json = json.dumps(evidence) if evidence else ""
    return ctx


def _wiring_evidence() -> dict:
    return {
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: True,
        EVIDENCE_KEY_REAL_BENCHMARK: False,
        EVIDENCE_KEY_FIXTURE_PURPOSE: "wiring_validation",
    }


def _real_benchmark_evidence() -> dict:
    return {
        EVIDENCE_KEY_SWE_BENCH_PRO: True,
        EVIDENCE_KEY_GOLD_PATCH_EMPTY: False,
        EVIDENCE_KEY_REAL_BENCHMARK: True,
        EVIDENCE_KEY_FIXTURE_PURPOSE: "",
    }


# ──────────────────────────────────────────────────────────────────────
# Detector composition (operator-canonical 2-signal AND)
# ──────────────────────────────────────────────────────────────────────


class TestDetectorComposition:
    def test_wiring_fixture_detected(self):
        ctx = _make_ctx(_wiring_evidence())
        assert is_route_wiring_validation_envelope(ctx) is True

    def test_real_benchmark_rejected(self):
        ctx = _make_ctx(_real_benchmark_evidence())
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_no_envelope_rejected(self):
        ctx = _make_ctx(None)
        assert is_route_wiring_validation_envelope(ctx) is False

    def test_real_benchmark_with_purpose_wiring_validation_still_rejected(self):
        """Defense-in-depth: even if a real benchmark accidentally
        had purpose=wiring_validation set, real_benchmark=True must
        block the synthetic noop."""
        ev = _real_benchmark_evidence()
        ev[EVIDENCE_KEY_FIXTURE_PURPOSE] = "wiring_validation"
        ev[EVIDENCE_KEY_REAL_BENCHMARK] = True
        ctx = _make_ctx(ev)
        assert is_route_wiring_validation_envelope(ctx) is False


# ──────────────────────────────────────────────────────────────────────
# Canonical GenerationResult(is_noop=True) shape preserved
# ──────────────────────────────────────────────────────────────────────


class TestSyntheticNoopShape:
    def test_synthetic_noop_dataclass_construction(self):
        """The synthetic noop MUST construct via the canonical
        GenerationResult dataclass (no parallel type) with the
        exact field shape the existing noop terminal handler expects."""
        synthetic = GenerationResult(
            candidates=(),
            provider_name="slice_12ah_synthetic_noop",
            generation_duration_s=0.0,
            is_noop=True,
        )
        assert synthetic.candidates == ()
        assert synthetic.provider_name == "slice_12ah_synthetic_noop"
        assert synthetic.generation_duration_s == 0.0
        assert synthetic.is_noop is True
        # Optional fields defaults preserved
        assert synthetic.model_id == ""
        assert synthetic.tool_execution_records == ()

    def test_synthetic_provider_name_is_greppable(self):
        """``slice_12ah_synthetic_noop`` is a greppable marker
        operators use to confirm the bypass fired in forensics."""
        synthetic = GenerationResult(
            candidates=(),
            provider_name="slice_12ah_synthetic_noop",
            generation_duration_s=0.0,
            is_noop=True,
        )
        assert "slice_12ah" in synthetic.provider_name
        assert "synthetic_noop" in synthetic.provider_name


# ──────────────────────────────────────────────────────────────────────
# AST pins — generate_runner.py composition discipline
# ──────────────────────────────────────────────────────────────────────


GEN_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "generate_runner.py"
)


class TestGenerateRunnerASTPins:
    def test_imports_is_route_wiring_validation_envelope(self):
        """generate_runner.py MUST import the operator-canonical
        detector from envelope_metadata. Drift would silently
        re-introduce the wedge."""
        src = GEN_RUNNER_PATH.read_text()
        assert "is_route_wiring_validation_envelope" in src, (
            "generate_runner.py must import "
            "is_route_wiring_validation_envelope from envelope_metadata "
            "(Slice 12AH bypass composition)"
        )

    def test_constructs_generation_result_with_is_noop_true(self):
        """The synthetic noop MUST be constructed via the canonical
        GenerationResult dataclass with is_noop=True. AST walks the
        file for a Call to GenerationResult with is_noop=True kwarg."""
        src = GEN_RUNNER_PATH.read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = (
                fn.id if isinstance(fn, ast.Name)
                else (fn.attr if isinstance(fn, ast.Attribute) else "")
            )
            if fname != "GenerationResult":
                continue
            # Look for is_noop=True kwarg
            for kw in node.keywords:
                if kw.arg == "is_noop":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        # Verify provider_name marker is present in same call
                        for kw2 in node.keywords:
                            if kw2.arg == "provider_name":
                                if (
                                    isinstance(kw2.value, ast.Constant)
                                    and "slice_12ah" in str(kw2.value.value)
                                ):
                                    found = True
                                    break
        assert found, (
            "generate_runner.py must construct "
            "GenerationResult(is_noop=True, provider_name=\"slice_12ah_synthetic_noop\", ...) "
            "for the synthetic bypass (Slice 12AH)"
        )

    def test_synthetic_provider_name_marker_present(self):
        """The greppable provider_name marker must appear so
        operators can confirm the bypass fired in debug.log."""
        src = GEN_RUNNER_PATH.read_text()
        assert '"slice_12ah_synthetic_noop"' in src, (
            "generate_runner.py must reference the literal "
            '"slice_12ah_synthetic_noop" provider_name string'
        )

    def test_slice12ah_log_marker_present(self):
        """The ``[Slice12AH]`` log marker must be greppable for
        operator forensics."""
        src = GEN_RUNNER_PATH.read_text()
        assert "[Slice12AH]" in src, (
            "generate_runner.py must emit a [Slice12AH] log marker "
            "when the bypass fires"
        )

    def test_early_break_for_synthetic_noop_in_retry_loop(self):
        """The retry loop MUST short-circuit on a pre-set
        ``generation is not None and generation.is_noop`` so the
        first iteration doesn't overwrite the synthetic noop via
        ``orch._generator.generate(...)``. AST/substring proof that
        the early-break check appears BEFORE the per-op cost cap
        check (which is the first thing in the loop body)."""
        src = GEN_RUNNER_PATH.read_text()
        # The synthetic-noop pre-set lives in a Slice 12AH block
        # which mentions "synthesizing 2b.1-noop"; the early-break
        # in the loop body is annotated with a "Slice 12AH" comment
        # and the `if generation is not None and generation.is_noop:`
        # predicate appears as the FIRST statement of the loop body.
        assert "synthesizing 2b.1-noop" in src, (
            "generate_runner.py must contain the Slice 12AH "
            "'synthesizing 2b.1-noop' log line"
        )
        # The early-break check must appear AFTER 'for attempt in range'
        # and BEFORE 'Per-op cost cap check' so the synthetic noop
        # short-circuits without paying the cost-cap gate.
        loop_idx = src.find("for attempt in range(1 + orch._config.max_generate_retries):")
        early_break_idx = src.find(
            "Slice 12AH — synthetic-noop pre-set",
        )
        cost_cap_idx = src.find("Per-op cost cap check", loop_idx)
        assert loop_idx > 0, "retry loop entry not found"
        assert early_break_idx > loop_idx, (
            "Slice 12AH early-break must be INSIDE the retry loop"
        )
        assert early_break_idx < cost_cap_idx, (
            "Slice 12AH early-break must precede the cost-cap check "
            "so the synthetic noop bypasses the cost gate"
        )


# ──────────────────────────────────────────────────────────────────────
# Composition pin — Slice 12AH uses the SAME detector as Slice 12AD/12AF
# ──────────────────────────────────────────────────────────────────────


class TestCompositionWithSlice12AD:
    def test_same_detector_powers_route_classification_and_bypass(self):
        """Slice 12AD's UrgencyRouter.classify Priority 0.6 uses
        is_route_wiring_validation_envelope to stamp the route.
        Slice 12AH's bypass uses the SAME detector to trigger the
        synthetic noop. Single source of truth — no parallel
        wiring-validation classification anywhere."""
        # Substring proof in both consumers
        ur_path = (
            Path(__file__).resolve().parents[2]
            / "backend" / "core" / "ouroboros" / "governance" / "urgency_router.py"
        )
        ur_src = ur_path.read_text()
        gr_src = GEN_RUNNER_PATH.read_text()
        assert "is_route_wiring_validation_envelope" in ur_src, (
            "urgency_router.py (Slice 12AD) must use the canonical "
            "detector"
        )
        assert "is_route_wiring_validation_envelope" in gr_src, (
            "generate_runner.py (Slice 12AH) must use the canonical "
            "detector"
        )
