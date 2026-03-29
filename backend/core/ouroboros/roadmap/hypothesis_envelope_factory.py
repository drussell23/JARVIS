"""Convert FeatureHypothesis objects into IntentEnvelopes for the governance pipeline.

Called during the REM PATCHING phase after roadmap synthesis produces a batch
of hypotheses that need to flow into the Ouroboros governed pipeline.
"""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)


def hypotheses_to_envelopes(
    hypotheses: List[FeatureHypothesis],
    *,
    snapshot_version: int,
) -> List[IntentEnvelope]:
    """Convert a list of FeatureHypothesis objects into IntentEnvelopes.

    Each hypothesis becomes one envelope with ``source="roadmap"``.
    ``requires_human_ack`` is always ``False`` — roadmap hypotheses flow
    through the governed (autonomous) tier.

    The evidence dict carries all fields required by the downstream pipeline
    to correlate, deduplicate, and bypass the ANALYZING stage
    (``"analysis_complete": True``).

    Parameters
    ----------
    hypotheses:
        Synthesised hypotheses to convert.
    snapshot_version:
        Integer version of the ``RoadmapSnapshot`` that produced these
        hypotheses.  Stored in evidence for traceability.

    Returns
    -------
    List[IntentEnvelope]
        One envelope per hypothesis, in the same order as *hypotheses*.
    """
    envelopes: List[IntentEnvelope] = []
    for h in hypotheses:
        envelope = make_envelope(
            source="roadmap",
            description=f"[{h.gap_type}] {h.description}",
            target_files=(h.suggested_scope,),
            repo=h.suggested_repos[0] if h.suggested_repos else "jarvis",
            confidence=h.confidence,
            urgency=h.urgency,
            evidence={
                "hypothesis_id": h.hypothesis_id,
                "provenance": h.provenance,
                "gap_type": h.gap_type,
                "confidence_rule_id": h.confidence_rule_id,
                "snapshot_version": snapshot_version,
                "analysis_complete": True,
            },
            requires_human_ack=False,
        )
        envelopes.append(envelope)
    return envelopes
