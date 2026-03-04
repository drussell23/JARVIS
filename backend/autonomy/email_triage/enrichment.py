"""Triage enrichment — pure merge of triage metadata into raw email dicts.

Called by the command processor to decorate email results with tier/score
information from the most recent triage cycle.  This is a pure function:
no side effects, no network, no exceptions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

_COMPATIBLE_SCHEMA_VERSIONS = {"1.0"}
# Extraction contract versions whose confidence values we trust.
# Unknown/missing contract versions default to heuristic confidence only.
_TRUSTED_EXTRACTION_CONTRACTS = {"1.0", ""}


def enrich_with_triage(
    emails: List[Dict[str, Any]],
    runner: Any,
    staleness_window_s: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], bool, Optional[float]]:
    """Merge triage metadata into raw email dicts.

    Parameters
    ----------
    emails:
        Raw email dicts from GoogleWorkspaceAgent.  Each must have an ``"id"``
        key for matching.
    runner:
        An ``EmailTriageRunner`` instance (or None).  Uses the public
        ``get_triage_snapshot()`` accessor to atomically read triage state.
    staleness_window_s:
        Maximum age (in seconds, monotonic clock) of the last triage report
        before results are considered stale and skipped.  When None, the
        runner's configured staleness window is used.

    Returns
    -------
    (enriched_emails, was_enriched, triage_age_s)
        * ``enriched_emails`` — same length / order as *emails*.  Matched
          emails are shallow copies with added triage keys; unmatched emails
          are passed through as-is.
        * ``was_enriched`` — ``True`` iff at least one email was decorated.
        * ``triage_age_s`` — seconds since the last report (monotonic), or
          ``None`` when enrichment was skipped entirely.

    Invariants
    ----------
    * ``len(output) == len(input)`` — never removes or reorders emails.
    * Original dicts are never mutated.
    * Pure function — no side effects, no network, no exceptions raised.
    """

    # Guard: runner is None
    if runner is None:
        return (emails, False, None)

    # Use public accessor for atomic snapshot
    snapshot_fn = getattr(runner, "get_triage_snapshot", None)
    if snapshot_fn is None:
        return (emails, False, None)

    snapshot = snapshot_fn(staleness_window_s=staleness_window_s)
    if snapshot is None:
        return (emails, False, None)

    # Guard: incompatible schema version
    schema_version = snapshot.get("schema_version")
    if schema_version not in _COMPATIBLE_SCHEMA_VERSIONS:
        return (emails, False, None)

    age = snapshot.get("age_s")
    triaged_emails = snapshot.get("triaged_emails") or {}
    if not triaged_emails:
        return (emails, False, age)

    # Enrich matched emails
    enriched: List[Dict[str, Any]] = []
    any_matched = False

    for email in emails:
        msg_id = email.get("id")
        triaged = triaged_emails.get(msg_id) if msg_id is not None else None

        if triaged is not None:
            # Shallow copy to avoid mutating the original dict
            enriched_email = dict(email)
            enriched_email["triage_tier"] = triaged.scoring.tier
            enriched_email["triage_score"] = triaged.scoring.score
            enriched_email["triage_tier_label"] = triaged.scoring.tier_label
            enriched_email["triage_action"] = triaged.notification_action

            # Extraction confidence + confirmation flag
            # Only trust confidence from known extraction contract versions.
            # Unknown contracts get demoted to heuristic-level confidence.
            _extraction_contract = getattr(
                triaged.features, "extraction_contract_version", ""
            )
            _source = getattr(triaged.features, "extraction_source", "heuristic")
            _raw_confidence = getattr(triaged.features, "extraction_confidence", 0.0)

            if _extraction_contract in _TRUSTED_EXTRACTION_CONTRACTS:
                confidence = _raw_confidence
            else:
                # Untrusted contract — demote confidence to prevent drift
                confidence = min(_raw_confidence, 0.5)
                _source = f"{_source}:untrusted_contract"

            enriched_email["triage_confidence"] = confidence
            enriched_email["triage_extraction_source"] = _source
            enriched_email["triage_extraction_contract"] = _extraction_contract

            # Ambiguous classification: low confidence + not extreme tier
            _CONFIDENCE_THRESHOLD = 0.6
            enriched_email["triage_needs_confirmation"] = (
                confidence < _CONFIDENCE_THRESHOLD
                and triaged.scoring.tier in (2, 3)
            )

            # Policy explanation for transparency
            if triaged.policy_explanation is not None:
                pe = triaged.policy_explanation
                enriched_email["triage_suppressed_by"] = pe.suppressed_by
                enriched_email["triage_dedup_hit"] = pe.dedup_hit

            enriched.append(enriched_email)
            any_matched = True
        else:
            # Pass through as-is (no copy needed)
            enriched.append(email)

    return (enriched, any_matched, age)
