"""Convert exploration findings into IntentEnvelopes for the governance pipeline."""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.finding_ranker import RankedFinding
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)


def findings_to_envelopes(
    findings: List[RankedFinding],
    *,
    epoch_id: int,
) -> List[IntentEnvelope]:
    """Convert ranked findings into IntentEnvelopes.

    Each finding becomes one envelope with source="exploration".
    The epoch_id is stored in evidence for correlation.
    requires_human_ack is always False (GOVERNED tier).
    """
    envelopes: List[IntentEnvelope] = []
    for finding in findings:
        envelope = make_envelope(
            source="exploration",
            description=f"[{finding.category}] {finding.description}",
            target_files=(finding.file_path,),
            repo=finding.repo,
            confidence=finding.confidence,
            urgency=finding.urgency,
            evidence={
                "epoch_id": epoch_id,
                "category": finding.category,
                "blast_radius": finding.blast_radius,
                "score": finding.score,
                "source_check": finding.source_check,
            },
            requires_human_ack=False,
        )
        envelopes.append(envelope)
    return envelopes
