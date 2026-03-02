"""Data contracts for the email triage system.

All result types are frozen dataclasses for immutability and determinism.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class EmailFeatures:
    """Structured features extracted from a raw email.

    Built from heuristic parsing (always available) + optional J-Prime
    structured extraction (when extraction_confidence > 0).
    """

    message_id: str
    sender: str
    sender_domain: str
    subject: str
    snippet: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    keywords: Tuple[str, ...]
    sender_frequency: str  # "first_time" | "occasional" | "frequent"
    urgency_signals: Tuple[str, ...]  # "deadline", "action_required", etc.
    extraction_confidence: float  # 0.0-1.0


@dataclass(frozen=True)
class ScoringResult:
    """Deterministic scoring output. Same inputs → same result."""

    score: int  # 0-100
    tier: int  # 1-4
    tier_label: str  # "jarvis/tier1_critical", etc.
    breakdown: Dict[str, float]  # per-factor scores
    idempotency_key: str  # sha256(message_id + scoring_version)[:16]


@dataclass
class TriagedEmail:
    """A fully triaged email with features, score, and action decision."""

    features: EmailFeatures
    scoring: ScoringResult
    notification_action: str  # "immediate" | "summary" | "label_only" | "quarantine"
    processed_at: float


@dataclass
class TriageCycleReport:
    """Summary of a single triage cycle."""

    cycle_id: str
    started_at: float
    completed_at: float
    emails_fetched: int
    emails_processed: int
    tier_counts: Dict[int, int]
    notifications_sent: int
    notifications_suppressed: int
    errors: List[str]
    skipped: bool = False
    skip_reason: Optional[str] = None
