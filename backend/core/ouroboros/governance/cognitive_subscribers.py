"""Cognitive subscribers — Slice 101 Phase 3 (Adaptive Intelligence Invariant).

Closes the synthetic learning loop on top of the :mod:`cognitive_bus`:

    GENERATE failure / cage block
        → orchestrator publishes ``ouroboros.lifecycle.post_failure``
        → :func:`belief_revision_on_failure` (async subscriber)
        → ``belief_revision_ledger`` records a FALSIFIED belief about the
          generation pattern (the files that just failed)
        → :func:`recent_avoidance_digest` reads recent DRIFTING/FALSIFIED
          beliefs and is injected, authority-free, into the GENERATE prompt
          (via ``strategic_direction`` additive block)
        → the FSM organically biases AWAY from recently-failing areas.

Every surface is observational: the subscriber only *records*, the digest only
*advises the prompt*. Neither holds FSM authority (Manifesto §1 invariant). Each
substrate gates itself on its own master flag (``JARVIS_BELIEF_REVISION_ENABLED``
§33.1 default-FALSE), composed under the cognitive-bus master. NEVER raises.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional, Sequence

from backend.core.ouroboros.governance.cognitive_bus import (
    CognitiveSubscriber,
    LIFECYCLE_POST_FAILURE,
    lifecycle_pattern,
)

logger = logging.getLogger("ouroboros.cognitive_subscribers")

# The belief domain under which generation/cage failures are recorded. Keeping
# it stable lets the read-side digest scope to generation-failure memory.
_DOMAIN_GENERATION_FAILURE = "generation/failure"

_MAX_DIGEST_FILES = 8


def _event_payload(event: Any) -> Mapping[str, Any]:
    try:
        payload = getattr(event, "payload", None)
        if isinstance(payload, Mapping):
            return payload
    except Exception:  # noqa: BLE001
        pass
    return {}


async def belief_revision_on_failure(event: Any) -> None:
    """Subscriber: on a ``post_failure`` lifecycle event, record the failed
    generation pattern as a FALSIFYING belief so future GENERATE prompts can
    steer away from it. NEVER raises (the bus wrapper double-guards too)."""
    payload = _event_payload(event)
    if payload.get("lifecycle_kind") != LIFECYCLE_POST_FAILURE:
        return
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (
            EvidenceKind,
            master_enabled,
            record_claim,
            record_evidence,
        )
    except Exception as exc:  # noqa: BLE001 — substrate unavailable → inert
        logger.debug("[CognitiveSub] belief import failed: %s", exc)
        return
    if not master_enabled():
        return

    files: Sequence[Any] = payload.get("target_files") or ()
    op_id = str(payload.get("op_id") or "")
    reason = str(payload.get("reason") or payload.get("state") or "failure")[:200]
    sig = ", ".join(sorted({str(f) for f in files if f})[:6]) or "(no target files)"
    # The belief we are falsifying: "generation in <files> succeeds". A failure
    # is direct falsifying evidence against it.
    text = f"generation in [{sig}] succeeds"

    try:
        claim = record_claim(
            text,
            _DOMAIN_GENERATION_FAILURE,
            target_files=list(files),
            confidence=0.5,
        )
        if claim is not None:
            record_evidence(
                claim.claim_id,
                EvidenceKind.FALSIFYING,
                source_op_id=op_id,
                note=reason,
            )
    except Exception as exc:  # noqa: BLE001 — recording is best-effort
        logger.debug("[CognitiveSub] belief record failed: %s", exc)


def recent_avoidance_digest(
    *,
    rows: Optional[Sequence[Mapping[str, Any]]] = None,
    now_unix: Optional[float] = None,
    max_files: int = _MAX_DIGEST_FILES,
) -> str:
    """Read-side of the learning loop. Returns a short, authority-free markdown
    block naming the files that have recently accrued DRIFTING/FALSIFIED beliefs,
    ranked by failure recurrence — to be injected into the GENERATE prompt. Empty
    string when the substrate is off, the ledger is empty, or nothing recurs.
    NEVER raises. ``rows`` is a testing seam (passed through to the evaluator).
    """
    try:
        from backend.core.ouroboros.governance.belief_revision_ledger import (
            BeliefVerdict,
            evaluate_recent_beliefs,
            master_enabled,
        )
    except Exception:  # noqa: BLE001
        return ""
    if not master_enabled():
        return ""
    try:
        reports = evaluate_recent_beliefs(rows=rows, now_unix=now_unix)
    except Exception:  # noqa: BLE001
        return ""

    file_counts: dict[str, int] = {}
    for r in reports:
        verdict = getattr(r, "verdict", None)
        if verdict not in (BeliefVerdict.DRIFTING, BeliefVerdict.FALSIFIED):
            continue
        claim = getattr(r, "claim", None)
        for f in (getattr(claim, "target_files", ()) or ()):
            key = str(f)
            if key:
                file_counts[key] = file_counts.get(key, 0) + 1
    if not file_counts:
        return ""

    ranked = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:max_files]
    lines = [f"- `{f}` (recent failures: {n})" for f, n in ranked]
    return (
        "## Recently-Failing Areas (proceed with extra care)\n\n"
        "Prior autonomous generations in these areas recently failed validation "
        "or hit a cage block. Diagnose the root cause before patching here; do "
        "not blindly retry the same approach.\n\n" + "\n".join(lines)
    )


def build_default_subscribers() -> List[CognitiveSubscriber]:
    """The default cognitive subscriber set registered at GLS boot. Each handler
    self-gates on its own substrate master flag, so this list is safe to register
    whenever the cognitive bus is enabled."""
    return [
        CognitiveSubscriber(
            "belief_revision_failure",
            lifecycle_pattern(),
            belief_revision_on_failure,
        ),
    ]
