"""Triage enrichment — pure merge of triage metadata into raw email dicts.

Called by the command processor to decorate email results with tier/score
information from the most recent triage cycle.  This is a pure function:
no side effects, no network, no exceptions.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

_COMPATIBLE_SCHEMA_VERSIONS = {"1.0"}


def enrich_with_triage(
    emails: List[Dict[str, Any]],
    runner: Any,
    staleness_window_s: float,
) -> Tuple[List[Dict[str, Any]], bool, Optional[float]]:
    """Merge triage metadata into raw email dicts.

    Parameters
    ----------
    emails:
        Raw email dicts from GoogleWorkspaceAgent.  Each must have an ``"id"``
        key for matching.
    runner:
        An ``EmailTriageRunner`` instance (or None).  The function reads
        ``_triaged_emails``, ``_last_report``, ``_last_report_at``, and
        ``_triage_schema_version`` — all read-only.
    staleness_window_s:
        Maximum age (in seconds, monotonic clock) of the last triage report
        before results are considered stale and skipped.

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

    # Guard: no last report
    last_report = getattr(runner, "_last_report", None)
    if last_report is None:
        return (emails, False, None)

    # Guard: incompatible schema version
    schema_version = getattr(runner, "_triage_schema_version", None)
    if schema_version not in _COMPATIBLE_SCHEMA_VERSIONS:
        return (emails, False, None)

    # Guard: stale results
    last_report_at = getattr(runner, "_last_report_at", None)
    if last_report_at is None:
        return (emails, False, None)

    now = time.monotonic()
    age = now - last_report_at
    if age > staleness_window_s:
        return (emails, False, None)

    # Guard: empty triaged emails
    triaged_emails = getattr(runner, "_triaged_emails", None) or {}
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
            enriched.append(enriched_email)
            any_matched = True
        else:
            # Pass through as-is (no copy needed)
            enriched.append(email)

    return (enriched, any_matched, age)
