"""Data contracts for the email triage system.

All result types are frozen dataclasses for immutability and determinism.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    extraction_source: str = "heuristic"  # "heuristic" | "jprime_v1" | "jprime_degraded_fallback"
    extraction_contract_version: str = ""  # "" for heuristic, "1.0" for validated J-Prime


@dataclass(frozen=True)
class NotificationDeliveryResult:
    """Outcome of a single notification delivery attempt."""

    message_id: str
    channel: str  # "voice" | "websocket" | "macos" | "bridge" | "summary"
    success: bool
    latency_ms: int
    error: Optional[str] = None


@dataclass(frozen=True)
class ScoringResult:
    """Deterministic scoring output. Same inputs → same result."""

    score: int  # 0-100
    tier: int  # 1-4
    tier_label: str  # "jarvis/tier1_critical", etc.
    breakdown: Dict[str, float]  # per-factor scores
    idempotency_key: str  # sha256(message_id + scoring_version)[:16]
    scoring_explanation: str = ""  # Human-readable: "Tier 1: frequent sender (90%) + ..."


@dataclass(frozen=True)
class PolicyExplanation:
    """Why a notification action was chosen (WS4 explainability)."""

    action: str  # "immediate" | "summary" | "label_only" | "quarantine"
    reasons: Tuple[str, ...]  # ("tier1_critical", "budget_available", "not_duplicate")
    suppressed_by: Optional[str] = None  # "quiet_hours" | "dedup_window" | "budget_exhausted"
    tier: int = 0
    score: int = 0
    quiet_hours_active: bool = False
    budget_remaining_hour: int = 0
    budget_remaining_day: int = 0
    dedup_hit: bool = False


@dataclass
class TriagedEmail:
    """A fully triaged email with features, score, and action decision."""

    features: EmailFeatures
    scoring: ScoringResult
    notification_action: str  # "immediate" | "summary" | "label_only" | "quarantine"
    processed_at: float
    policy_explanation: Optional[PolicyExplanation] = None


@dataclass(frozen=True)
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
    triage_schema_version: str = "1.0"
    policy_version: str = "v1"
    # Commit policy metadata (v1.1.1)
    degraded: bool = False
    degraded_reason: Optional[str] = None
    snapshot_committed: bool = True

    # C2: Stage-level latency histograms
    stage_latencies_ms: Optional[Dict[str, float]] = None  # fetch, extract, score, label, notify
    extraction_p95_ms: float = 0.0
    admitted_count: int = 0  # emails admitted after adaptive gate
    budget_computed_s: float = 0.0  # computed required timeout
