"""P5 Slice 3 — AdversarialReviewer GENERATE-injection hook + bridge feed.

Per OUROBOROS_VENOM_PRD.md §9 Phase 5 P5:

  > Activates post-PLAN, pre-GENERATE. Given the plan, the model is
  > prompted as: "You are a senior engineer reviewing this plan for
  > the most likely way it will fail. Find at least 3 failure modes."
  > Output is structured findings injected into GENERATE prompt as
  > "Reviewer raised:" section.

This module is the **wiring layer** that the orchestrator (Slice 5)
will call from its post-PLAN/pre-GENERATE hook. It composes:

  * Slice 2's :class:`AdversarialReviewerService` for the actual
    review pass.
  * Slice 1's :func:`format_findings_for_generate_prompt` for the
    "Reviewer raised:" section.
  * The existing :class:`ConversationBridge` for cross-op recall —
    the review summary is fed in as a postmortem-source turn so a
    future op's CONTEXT_EXPANSION can surface "this plan had N
    high-severity findings."

Slice 3 ships **NO orchestrator edits**. The hook is self-contained
and unit-testable end-to-end with injected fakes. Slice 5 graduation
adds the actual call site in `orchestrator.py`.

Public surface:

  * :func:`review_plan_for_generate_injection(...)` — the orchestrator
    hook. Returns the injection string (`""` when no findings) plus
    the underlying :class:`AdversarialReview` (for telemetry).
  * :func:`inject_into_generate_prompt(base_prompt, findings_section)`
    — pure helper that appends the section with a clean delimiter.
  * :func:`feed_review_to_bridge(review, bridge=None)` — best-effort
    bridge feed; returns True on success.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: ``adversarial_reviewer`` + ``adversarial_reviewer_service``
    (own slice family) + ``conversation_bridge`` (already-built
    primitive).
  * No subprocess / file I/O / env mutation / network. The bridge
    feed delegates I/O to the existing ConversationBridge primitive.
  * Best-effort throughout — every step (review, format, bridge feed,
    prompt injection) is wrapped in ``try / except``; failures NEVER
    propagate to the orchestrator. The reviewer is **advisory only**
    per PRD edge case "PLAN still authoritative."
  * Empty / skipped reviews → empty injection string. The orchestrator
    can append unconditionally; no special-case branching needed.

Default-off behind ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED`` (Slice 1
flag, gated inside the service). Slice 5 graduation flips the
default + wires the orchestrator call site.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialReview,
    format_findings_for_generate_prompt,
)
from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    AdversarialReviewerService,
    get_default_service,
)

logger = logging.getLogger(__name__)


# Delimiter inserted between the base GENERATE prompt and the
# "Reviewer raised:" section. Two blank lines so the model sees a
# clean section boundary; pinned by tests.
_INJECTION_DELIMITER: str = "\n\n"

# Cap on the bridge-feed text body — the review is summarized into a
# single line; this defends against a future change shoving the full
# findings into the bridge (which would defeat the K-cap logic).
_MAX_BRIDGE_TEXT_CHARS: int = 480

# Bridge source label. The existing ConversationBridge whitelist
# (SOURCE_TUI_USER / ASK_HUMAN_Q / ASK_HUMAN_A / POSTMORTEM / VOICE)
# is the only set of accepted sources; "postmortem" is the closest
# match semantically — adversarial findings function as
# pre-GENERATE postmortem on the plan.
_BRIDGE_SOURCE: str = "postmortem"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateInjection:
    """Bundle returned to the orchestrator (Slice 5 wiring).

    ``injection_text`` is appended to the GENERATE base prompt
    verbatim — empty string means "no Reviewer raised: section to
    inject" (review skipped, or all findings filtered out).
    ``review`` is the underlying :class:`AdversarialReview` so the
    orchestrator can log telemetry / surface in REPL.
    ``bridge_fed`` is True when the summary landed in
    ConversationBridge (Slice 4 REPL exposes this); False on bridge
    failure / disabled / no-bridge."""

    injection_text: str
    review: AdversarialReview
    bridge_fed: bool = False


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def review_plan_for_generate_injection(
    *,
    op_id: str,
    plan_text: str,
    target_files: Sequence[str] = (),
    risk_tier_name: Optional[str] = None,
    service: Optional[AdversarialReviewerService] = None,
    bridge=None,
) -> GenerateInjection:
    """Run the reviewer over a plan, render the GENERATE-prompt
    injection, and feed the summary to ConversationBridge.

    Returns a :class:`GenerateInjection` with:
      * ``injection_text`` — string to append to the GENERATE prompt.
        Empty string when:
          - the service skipped (master_off, safe_auto, empty_plan,
            no_provider, provider_error, budget_exhausted), OR
          - the response had no grounded findings after the
            hallucination filter.
      * ``review`` — the underlying :class:`AdversarialReview`.
      * ``bridge_fed`` — True when the bridge accepted a summary turn.

    Best-effort: every internal failure is caught + logged; the
    function NEVER raises. The orchestrator can call this
    unconditionally during the post-PLAN / pre-GENERATE window.

    PLAN authority is preserved by construction: this function
    returns text only; it never gates or mutates the plan or the
    decision to proceed to GENERATE. The orchestrator is free to
    ignore the injection text entirely (e.g., for a future
    operator-controlled "stealth mode")."""
    svc = service or get_default_service()

    # 1. Run the review.
    try:
        review = svc.review_plan(
            op_id=op_id,
            plan_text=plan_text,
            target_files=target_files,
            risk_tier_name=risk_tier_name,
        )
    except Exception as exc:  # noqa: BLE001
        # Service is best-effort by design, but defensive-wrap in
        # case a future change re-introduces a raise path.
        logger.warning(
            "[AdversarialReviewerHook] op=%s service raised "
            "(should be best-effort): %s", op_id, exc,
        )
        # Fall back to an empty skipped review so the rest of the
        # flow still has a well-formed object.
        review = AdversarialReview(
            op_id=op_id, findings=(),
            skip_reason="hook_service_exception",
        )

    # 2. Render the GENERATE-prompt injection.
    injection = ""
    if not review.was_skipped and review.findings:
        try:
            injection = format_findings_for_generate_prompt(review.findings)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialReviewerHook] op=%s format failed: %s",
                op_id, exc,
            )
            injection = ""

    # 3. Feed the bridge.
    bridge_fed = feed_review_to_bridge(review, bridge=bridge)

    return GenerateInjection(
        injection_text=injection,
        review=review,
        bridge_fed=bridge_fed,
    )


# ---------------------------------------------------------------------------
# Pure prompt-injection helper
# ---------------------------------------------------------------------------


def inject_into_generate_prompt(
    base_prompt: str,
    findings_section: str,
) -> str:
    """Append the "Reviewer raised:" section to a GENERATE base
    prompt with a clean two-blank-line delimiter.

    Returns ``base_prompt`` unchanged when ``findings_section`` is
    empty (so the orchestrator can call this unconditionally).
    Defensive: ``None`` inputs coerced to empty string."""
    base = base_prompt or ""
    section = findings_section or ""
    if not section.strip():
        return base
    if not base.strip():
        return section
    return base + _INJECTION_DELIMITER + section


# ---------------------------------------------------------------------------
# ConversationBridge feed
# ---------------------------------------------------------------------------


def feed_review_to_bridge(
    review: AdversarialReview,
    bridge=None,
) -> bool:
    """Feed a one-line summary of the review into ConversationBridge.

    Best-effort: returns True when the bridge accepted the turn,
    False on:
      * empty review (skipped or no findings — nothing useful to feed),
      * bridge import failure (e.g., minimal test env),
      * bridge disabled (the bridge's own master flag is off),
      * bridge.record_turn raised.

    The summary text is bounded by ``_MAX_BRIDGE_TEXT_CHARS`` so it
    can't blow out the bridge's per-turn cap. Pinned by tests."""
    if review is None:
        return False
    if review.was_skipped:
        return False
    if not review.findings:
        return False

    summary = _summarize_review(review)
    if not summary:
        return False

    target = bridge
    if target is None:
        try:
            from backend.core.ouroboros.governance.conversation_bridge import (
                get_default_bridge,
            )
            target = get_default_bridge()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[AdversarialReviewerHook] no bridge available: %s", exc,
            )
            return False

    try:
        target.record_turn(
            role="assistant",
            text=summary,
            source=_BRIDGE_SOURCE,
            op_id=review.op_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[AdversarialReviewerHook] bridge feed failed: %s", exc,
        )
        return False

    # The bridge silently drops turns when its own master flag is
    # false (per its v1.1 contract). We can't tell the difference
    # without a stat-snapshot; for §8 audit purposes treat the
    # call-completed-without-raise as success.
    return True


def _summarize_review(review: AdversarialReview) -> str:
    """Render a single-line summary of a review for bridge feed.

    Format::

        AdversarialReviewer raised 3 findings (high=1, med=1, low=1)
        for op-X: file_a.py, file_b.py
    """
    if not review.findings:
        return ""
    hist = review.severity_histogram()
    files = sorted({f.file_reference for f in review.findings if f.file_reference})
    files_str = ", ".join(files[:5])  # cap at 5 for bridge brevity
    if len(files) > 5:
        files_str += f", +{len(files) - 5} more"
    text = (
        f"AdversarialReviewer raised {len(review.findings)} findings "
        f"(high={hist['HIGH']}, med={hist['MEDIUM']}, low={hist['LOW']}) "
        f"for {review.op_id}"
    )
    if files_str:
        text += f": {files_str}"
    if len(text) > _MAX_BRIDGE_TEXT_CHARS:
        text = text[: _MAX_BRIDGE_TEXT_CHARS - 3] + "..."
    return text


__all__ = [
    "GenerateInjection",
    "feed_review_to_bridge",
    "inject_into_generate_prompt",
    "review_plan_for_generate_injection",
]
