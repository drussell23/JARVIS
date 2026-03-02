"""Deterministic email scoring engine.

Pure function -- no I/O, no network, no randomness.
Same EmailFeatures + same TriageConfig -> identical ScoringResult every time.
"""

from __future__ import annotations

import hashlib
from typing import Set

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.schemas import EmailFeatures, ScoringResult

# Urgency keywords (normalized to lowercase)
_URGENT_KEYWORDS: Set[str] = {
    "urgent", "critical", "immediate", "asap", "emergency",
    "action_required", "action required", "time-sensitive",
    "deadline", "due today", "overdue", "escalation",
}

_NOISE_KEYWORDS: Set[str] = {
    "unsubscribe", "sale", "discount", "promo", "deal",
    "marketing", "newsletter", "advertisement", "offer",
}

_PROMOTIONAL_LABELS: Set[str] = {
    "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES",
    "CATEGORY_FORUMS", "SPAM",
}


def score_email(features: EmailFeatures, config: TriageConfig) -> ScoringResult:
    """Score an email deterministically.

    Factor weights:
        sender   (30%) -- known contacts, frequency, domain
        content  (35%) -- urgency keywords in subject/snippet, attachment
        urgency  (25%) -- urgency signals from extraction, is_reply
        context  (10%) -- label context (INBOX vs CATEGORY_PROMOTIONS)

    Args:
        features: Extracted email features.
        config: Triage configuration (thresholds, scoring version).

    Returns:
        ScoringResult with score (0-100), tier (1-4), breakdown, idempotency key.
    """
    sender_score = _score_sender(features)
    content_score = _score_content(features)
    urgency_score = _score_urgency(features)
    context_score = _score_context(features)

    raw = (
        sender_score * 0.30
        + content_score * 0.35
        + urgency_score * 0.25
        + context_score * 0.10
    )
    score = max(0, min(100, int(round(raw * 100))))
    tier = config.tier_for_score(score)
    tier_label = config.label_for_tier(tier)

    idempotency_key = hashlib.sha256(
        f"{features.message_id}:{config.scoring_version}".encode()
    ).hexdigest()[:16]

    return ScoringResult(
        score=score,
        tier=tier,
        tier_label=tier_label,
        breakdown={
            "sender": round(sender_score, 4),
            "content": round(content_score, 4),
            "urgency": round(urgency_score, 4),
            "context": round(context_score, 4),
        },
        idempotency_key=idempotency_key,
    )


def _score_sender(features: EmailFeatures) -> float:
    """Score based on sender identity and frequency (0.0 - 1.0)."""
    base = 0.3  # unknown sender baseline

    freq = features.sender_frequency
    if freq == "frequent":
        base = 0.9
    elif freq == "occasional":
        base = 0.5

    if features.sender.startswith("noreply@") or features.sender.startswith("no-reply@"):
        base *= 0.5

    return max(0.0, min(1.0, base))


def _score_content(features: EmailFeatures) -> float:
    """Score based on subject, snippet, keywords, attachment (0.0 - 1.0)."""
    score = 0.2

    kw_lower = {k.lower() for k in features.keywords}
    urgent_hits = kw_lower & _URGENT_KEYWORDS
    noise_hits = kw_lower & _NOISE_KEYWORDS

    score += min(len(urgent_hits) * 0.2, 0.6)
    score -= min(len(noise_hits) * 0.15, 0.3)

    subj_lower = features.subject.lower()
    for word in _URGENT_KEYWORDS:
        if word in subj_lower:
            score += 0.1
            break

    if features.has_attachment:
        score += 0.05

    return max(0.0, min(1.0, score))


def _score_urgency(features: EmailFeatures) -> float:
    """Score based on urgency signals and reply status (0.0 - 1.0)."""
    score = 0.1

    signal_count = len(features.urgency_signals)
    score += min(signal_count * 0.25, 0.7)

    if features.is_reply:
        score += 0.15

    return max(0.0, min(1.0, score))


def _score_context(features: EmailFeatures) -> float:
    """Score based on label context (0.0 - 1.0)."""
    labels = set(features.label_ids)

    if labels & _PROMOTIONAL_LABELS:
        return 0.1

    if "IMPORTANT" in labels:
        return 0.9

    if "INBOX" in labels:
        return 0.6

    return 0.3
