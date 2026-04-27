"""P5 Slice 5 deferred follow-up — plan_runner.py adversarial wiring test.

Pins the structural integration of `review_plan_for_generate_injection`
into the post-PLAN/pre-GENERATE site of plan_runner.py. This is the
deferred-follow-up wiring that completes Phase 5 P5 (the AdversarialReviewer
was callable + audit-trailed + observable after Slice 5 graduation, but
the FSM did not auto-invoke it; this PR closes that gap).

Authority invariants pinned (PRD Phase 5 P5 + plan_runner.py invariants):

  * The hook is invoked POST-PLAN, PRE-GENERATE — i.e. after
    `ctx.advance(OperationPhase.GENERATE)` and before the generate
    runner is dispatched.
  * The wiring is purely additive — same try/except shape as the
    sibling Adaptive Learning + Tier 5 + TestCoverageEnforcer
    injectors. No existing behavior changes.
  * PLAN authority is preserved — the hook's return value is text
    only. The wiring uses `ctx.with_strategic_memory_context()` to
    append the injection to `strategic_memory_prompt`; it never
    rewrites or overrides any other ctx field, never gates, never
    advances phase.
  * Best-effort by construction — ImportError + bare-Exception both
    swallowed so a hook crash NEVER crashes the runner.
  * Defaults preserved — JARVIS_ADVERSARIAL_REVIEWER_ENABLED was
    already graduated in Slice 5 (default true); the new wiring does
    NOT change that. The service's 6 skip paths still gate any actual
    LLM call.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_PLAN_RUNNER = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "plan_runner.py"
)


def _read() -> str:
    return _PLAN_RUNNER.read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


# ===========================================================================
# A — Wiring presence (source-grep)
# ===========================================================================


def test_plan_runner_imports_adversarial_hook():
    """Pin: plan_runner.py imports `review_plan_for_generate_injection`
    via function-local import (mirrors the Adaptive Learning / Tier 5
    pattern — keeps the runner's static import surface minimum)."""
    src = _read()
    assert (
        "from backend.core.ouroboros.governance.adversarial_reviewer_hook import"
        in src
    ), "plan_runner.py must import from adversarial_reviewer_hook"
    assert "review_plan_for_generate_injection" in src, (
        "plan_runner.py must reference review_plan_for_generate_injection"
    )


def test_plan_runner_calls_hook_with_expected_kwargs():
    """Pin: the hook is called with op_id + plan_text + target_files +
    risk_tier_name kwargs (the hook's documented contract)."""
    src = _read()
    # Grep the four required kwargs in the call site (whitespace-tolerant).
    # Match the hook-call open-paren and look forward for each kwarg.
    call_idx = src.find("review_plan_for_generate_injection(")
    assert call_idx > 0, "hook call site must exist"
    # Take a 600-char window starting at the call site.
    call_block = src[call_idx:call_idx + 600]
    for kw in ("op_id", "plan_text", "target_files", "risk_tier_name"):
        assert re.search(rf"\b{kw}\s*=", call_block), (
            f"hook call must pass {kw} as kwarg"
        )


def test_plan_runner_reads_implementation_plan_for_hook():
    """Pin: the wiring reads ctx.implementation_plan as plan_text
    (the canonical post-PLAN field per op_context.py:864)."""
    src = _read()
    # The wiring assigns plan_text via getattr(ctx, "implementation_plan", "")
    assert re.search(
        r'getattr\(\s*ctx\s*,\s*"implementation_plan"\s*,', src,
    ), "wiring must read ctx.implementation_plan"


def test_plan_runner_passes_risk_tier_name_string():
    """Pin: ctx.risk_tier (RiskTier enum or None) is normalized to
    .name string for the hook (which expects Optional[str])."""
    src = _read()
    assert re.search(
        r"ctx\.risk_tier\.name\s+if\s+ctx\.risk_tier\s+is\s+not\s+None",
        src,
    ), "wiring must convert ctx.risk_tier to .name string"


# ===========================================================================
# B — Authority invariants
# ===========================================================================


def test_wiring_uses_with_strategic_memory_context_not_replace():
    """PLAN authority preservation: injection lands via
    `ctx.with_strategic_memory_context(...)` (the existing invariant-
    preserving setter), NOT via `dataclasses.replace` or direct field
    assignment that could bypass downstream invariants."""
    src = _read()
    # Find the AdversarialReviewer block — bounded by the section
    # marker comment.
    marker = "Phase 5 P5: AdversarialReviewer pre-GENERATE injection"
    idx = src.find(marker)
    assert idx >= 0, "wiring section comment marker missing"
    # Take the next 2000 characters as the block's footprint.
    block = src[idx:idx + 2500]
    assert "with_strategic_memory_context" in block, (
        "wiring must use with_strategic_memory_context for invariant-safe "
        "injection"
    )


def test_wiring_section_runs_after_plan_advance_to_generate():
    """The hook is post-PLAN, pre-GENERATE: must appear AFTER the
    `ctx = ctx.advance(OperationPhase.GENERATE)` line."""
    src = _read()
    advance_idx = src.find("ctx.advance(OperationPhase.GENERATE)")
    marker_idx = src.find(
        "Phase 5 P5: AdversarialReviewer pre-GENERATE injection",
    )
    assert advance_idx > 0, "PLAN→GENERATE advance line not found"
    assert marker_idx > 0, "wiring section marker not found"
    assert marker_idx > advance_idx, (
        "AdversarialReviewer wiring must run AFTER the GENERATE advance "
        f"(advance@{advance_idx} marker@{marker_idx})"
    )


def test_wiring_uses_defensive_try_except_pattern():
    """Pin: the wiring is wrapped in try / except ImportError / except
    Exception — same best-effort shape as Adaptive Learning + Tier 5
    + TestCoverageEnforcer. A hook failure must NOT propagate."""
    src = _read()
    marker = "Phase 5 P5: AdversarialReviewer pre-GENERATE injection"
    idx = src.find(marker)
    block = src[idx:idx + 2500]
    assert "try:" in block, "wiring must be in a try block"
    assert "except ImportError:" in block, (
        "wiring must catch ImportError (defensive — adversarial module may "
        "be absent in some test envs)"
    )
    assert "except Exception:" in block, (
        "wiring must catch bare Exception (best-effort contract)"
    )


def test_wiring_does_not_advance_phase_or_gate():
    """PLAN authority preservation: the AdversarialReviewer block must
    NOT call ctx.advance, return PhaseResult, or raise — those would
    cross into gating authority. Only mutation allowed:
    with_strategic_memory_context."""
    src = _read()
    marker = "Phase 5 P5: AdversarialReviewer pre-GENERATE injection"
    idx = src.find(marker)
    end_marker = "JARVIS Tier 6: Personality voice line"
    end_idx = src.find(end_marker, idx)
    assert idx > 0 and end_idx > 0
    block = src[idx:end_idx]
    assert "ctx.advance(" not in block, (
        "AdversarialReviewer wiring must not call ctx.advance — that "
        "would cross into gating authority"
    )
    assert "return PhaseResult" not in block, (
        "AdversarialReviewer wiring must not return PhaseResult — it is "
        "advisory only"
    )
    assert "raise " not in block, (
        "AdversarialReviewer wiring must never raise into the runner"
    )


def test_wiring_logs_findings_count_on_injection():
    """Pin: structured telemetry log with findings count + bridge_fed
    flag. Operators need to see this to validate the hook is firing."""
    src = _read()
    marker = "Phase 5 P5: AdversarialReviewer pre-GENERATE injection"
    idx = src.find(marker)
    block = src[idx:idx + 2500]
    assert "AdversarialReviewer" in block and "findings injected" in block, (
        "wiring must log findings count + bridge_fed (operator visibility)"
    )
    assert "bridge_fed" in block, (
        "wiring must surface bridge_fed in telemetry"
    )


# ===========================================================================
# C — Hook contract sanity (smoke)
# ===========================================================================


def test_hook_module_exports_expected_surface():
    """Pin the hook's public surface that the wiring depends on. If
    the hook signature changes, this test fails and the wiring must
    be updated in lockstep."""
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        review_plan_for_generate_injection,
    )
    import inspect
    sig = inspect.signature(review_plan_for_generate_injection)
    expected_kwargs = {"op_id", "plan_text", "target_files",
                       "risk_tier_name", "service", "bridge"}
    actual_kwargs = set(sig.parameters.keys())
    assert expected_kwargs.issubset(actual_kwargs), (
        f"hook signature missing expected kwargs: "
        f"{expected_kwargs - actual_kwargs}"
    )


def test_hook_returns_generate_injection_dataclass():
    """Pin: the hook returns a `GenerateInjection` with the three
    fields the wiring reads (injection_text, review.findings,
    bridge_fed)."""
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        GenerateInjection,
    )
    import dataclasses
    fields = {f.name for f in dataclasses.fields(GenerateInjection)}
    assert "injection_text" in fields
    assert "review" in fields
    assert "bridge_fed" in fields


def test_hook_with_master_off_returns_empty_injection(monkeypatch):
    """Integration smoke: with the AdversarialReviewer master flag
    off, the hook returns a skipped review with empty injection. The
    wiring's `if _adv_injection.injection_text:` branch then no-ops,
    leaving strategic_memory_prompt unchanged."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "0")
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        review_plan_for_generate_injection,
    )
    result = review_plan_for_generate_injection(
        op_id="test-op",
        plan_text="some plan",
        target_files=("backend/example.py",),
        risk_tier_name="SAFE_AUTO",
    )
    assert result.injection_text == "", (
        "master-off must yield empty injection_text"
    )
    assert result.review.was_skipped, (
        "master-off must mark the review as skipped"
    )


def test_hook_with_safe_auto_skips_review(monkeypatch):
    """The service's safe_auto skip path: SAFE_AUTO ops bypass the
    adversarial review entirely (cheap auto-applies don't earn the
    cost)."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "1")
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        review_plan_for_generate_injection,
    )
    result = review_plan_for_generate_injection(
        op_id="test-op",
        plan_text="some plan",
        target_files=("backend/example.py",),
        risk_tier_name="SAFE_AUTO",
    )
    # Either safe_auto skip OR no_provider skip (since no provider
    # is wired in test env). Either way: no injection, no crash.
    assert result.injection_text == ""
    assert result.review.was_skipped


def test_hook_with_empty_plan_skips(monkeypatch):
    """Empty plan_text → service skips with reason='empty_plan'.
    Reproduces the wiring path where ctx.implementation_plan is
    empty (PlanGenerator skipped or unavailable)."""
    monkeypatch.setenv("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", "1")
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        review_plan_for_generate_injection,
    )
    result = review_plan_for_generate_injection(
        op_id="test-op",
        plan_text="",  # empty — wiring fallback when no plan generated
        target_files=("backend/example.py",),
        risk_tier_name="NOTIFY_APPLY",
    )
    assert result.injection_text == ""
    assert result.review.was_skipped


# ===========================================================================
# D — Section ordering (within the post-PLAN injection ladder)
# ===========================================================================


def test_adversarial_runs_after_tier5_intelligence():
    """Pin the section ordering inside the post-PLAN injection ladder.
    AdversarialReviewer should run AFTER Tier 5 Cross-Domain Intelligence
    (which produces synthesis text the reviewer might reference) and
    BEFORE Tier 6 Personality (which is voice-only and doesn't affect
    the GENERATE prompt)."""
    src = _read()
    tier5_idx = src.find("JARVIS Tier 5: Cross-Domain Intelligence")
    adv_idx = src.find(
        "Phase 5 P5: AdversarialReviewer pre-GENERATE injection",
    )
    tier6_idx = src.find("JARVIS Tier 6: Personality voice line")
    assert tier5_idx > 0 and adv_idx > 0 and tier6_idx > 0
    assert tier5_idx < adv_idx < tier6_idx, (
        f"section ordering broken: tier5@{tier5_idx} adv@{adv_idx} "
        f"tier6@{tier6_idx} (expected tier5 < adv < tier6)"
    )


# ===========================================================================
# E — Master flag default preserved (graduation invariant)
# ===========================================================================


def test_adversarial_master_flag_still_default_true_post_wiring():
    """Pin: the AdversarialReviewer master flag is still default true
    (graduated in Phase 5 P5 Slice 5). This wiring follow-up does NOT
    change that. Hot-revert (set env to false) still works because the
    service short-circuits inside review_plan."""
    from backend.core.ouroboros.governance.adversarial_reviewer import (
        is_enabled as _adv_is_enabled,
    )
    import os
    # When env unset, is_enabled returns True (the graduated default).
    saved = os.environ.pop("JARVIS_ADVERSARIAL_REVIEWER_ENABLED", None)
    try:
        assert _adv_is_enabled() is True
    finally:
        if saved is not None:
            os.environ["JARVIS_ADVERSARIAL_REVIEWER_ENABLED"] = saved
