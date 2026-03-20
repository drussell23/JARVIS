"""backend/core/ouroboros/consciousness/types.py

Frozen dataclasses, protocols, and configuration for the Trinity Consciousness
self-awareness layer.

Design:
    - All state-bearing structs are frozen dataclasses — safe to cache, hash,
      and pass across async boundaries without defensive copying.
    - The only mutable struct is DreamMetrics (accumulated counters).
    - ConsciousnessConfig.from_env() is the single authoritative factory;
      callers must never construct it by hand in production code.
    - Utility functions (compute_job_key, compute_blueprint_id) are pure and
      deterministic — tested under TC12.
"""

from __future__ import annotations

import hashlib
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # Python 3.7 safety shim (not needed on 3.9+ but harmless)
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _sha256_hex(*parts: str) -> str:
    """Return the SHA-256 hex digest of the concatenated parts joined by ':'."""
    payload = ":".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_job_key(
    repo_sha: str,
    policy_hash: str,
    prompt_family: str,
    model_class: str,
) -> str:
    """Deterministic deduplication key for a dream job.

    TC12: same inputs always produce the same 64-char hex string.
    """
    return _sha256_hex(repo_sha, policy_hash, prompt_family, model_class)


def compute_blueprint_id(
    repo_sha: str,
    policy_hash: str,
    prompt_family: str,
    model_class: str,
) -> str:
    """Deterministic ID for an ImprovementBlueprint.

    Identical to compute_job_key — same four axes define uniqueness.
    """
    return _sha256_hex(repo_sha, policy_hash, prompt_family, model_class)


# ---------------------------------------------------------------------------
# Health types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubsystemHealth:
    """Point-in-time health reading for a single named subsystem.

    Args:
        name:         Identifier — "gls" | "ils" | "oracle" | "prime" | "reactor"
        status:       Coarse bucket — "healthy" | "degraded" | "unknown" | "offline"
        score:        Normalised 0.0–1.0 health score (1.0 = fully healthy).
        details:      Raw health dict preserved verbatim for debugging/audit.
        polled_at_utc: ISO-8601 UTC timestamp of when this reading was taken.
    """

    name: str
    status: str
    score: float
    details: Dict[str, Any]
    polled_at_utc: str


@dataclass(frozen=True)
class ResourceHealth:
    """Snapshot of host-level resource utilisation.

    Args:
        cpu_percent:  0–100 CPU utilisation across all cores.
        ram_percent:  0–100 physical RAM utilisation.
        disk_percent: 0–100 disk utilisation (primary volume).
        pressure:     Derived categorical — "NORMAL" | "ELEVATED" | "CRITICAL" | "EMERGENCY"
    """

    cpu_percent: float
    ram_percent: float
    disk_percent: float
    pressure: str


@dataclass(frozen=True)
class BudgetHealth:
    """Snapshot of API spend for the current rolling day.

    All monetary values are in USD.
    """

    daily_spend_usd: float
    iteration_spend_usd: float
    remaining_usd: float


@dataclass(frozen=True)
class TrustHealth:
    """Current autonomy tier and progress toward the next one.

    Args:
        current_tier:        AutonomyTier.value string (e.g. "governed").
        graduation_progress: 0.0–1.0 fraction of criteria met for the next tier.
    """

    current_tier: str
    graduation_progress: float


@dataclass(frozen=True)
class TrinityHealthSnapshot:
    """Complete system health reading at a single point in time.

    This is the atomic unit written to HealthTrend.

    Args:
        timestamp_utc:   ISO-8601 UTC timestamp.
        overall_verdict: "HEALTHY" | "DEGRADED" | "CRITICAL"
        overall_score:   Weighted composite 0.0–1.0 across all subsystems.
        jarvis:          Health of the local JARVIS backend process.
        prime:           Health of the JARVIS-Prime GCP model server.
        reactor:         Health of the Reactor feedback loop.
        resources:       Host CPU/RAM/disk snapshot.
        budget:          Rolling-day API budget snapshot.
        trust:           Current autonomy tier status.
    """

    timestamp_utc: str
    overall_verdict: str
    overall_score: float
    jarvis: SubsystemHealth
    prime: SubsystemHealth
    reactor: SubsystemHealth
    resources: ResourceHealth
    budget: BudgetHealth
    trust: TrustHealth


class HealthTrend:
    """Bounded rolling window of TrinityHealthSnapshot entries.

    Entries are ordered oldest→newest.  When ``max_entries`` is reached the
    oldest entry is evicted to make room for the new one (ring-buffer
    semantics).

    Args:
        max_entries: Maximum number of snapshots to retain (default 720 ≈ 6h
                     at 30 s poll interval).
    """

    def __init__(self, max_entries: int = 720) -> None:
        self._max_entries = max_entries
        self._entries: Deque[TrinityHealthSnapshot] = deque(maxlen=max_entries)

    def add(self, snapshot: TrinityHealthSnapshot) -> None:
        """Append a snapshot, evicting the oldest if at capacity."""
        self._entries.append(snapshot)

    def get_window(self, minutes: int) -> List[TrinityHealthSnapshot]:
        """Return all snapshots whose timestamp falls within the last *minutes*.

        Snapshots without a parseable ISO-8601 timestamp are excluded.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        result: List[TrinityHealthSnapshot] = []
        for snap in self._entries:
            try:
                ts = datetime.fromisoformat(snap.timestamp_utc)
                # Make tz-aware if naive
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    result.append(snap)
            except (ValueError, TypeError):
                continue
        return result

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Memory types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryInsight:
    """A distilled learning extracted from operational history.

    Args:
        insight_id:    Unique identifier (e.g. UUID or slug).
        category:      "failure_pattern" | "success_pattern" | "file_fragility"
                       | "timing_pattern"
        content:       Human-readable summary of the insight.
        confidence:    0.0–1.0 strength of the insight.
        evidence_count: Number of distinct observations that support this insight.
        last_seen_utc: ISO-8601 UTC of the most recent corroborating event.
        ttl_hours:     Shelf-life in hours from *last_seen_utc* (default 168 = 1 week).
    """

    insight_id: str
    category: str
    content: str
    confidence: float
    evidence_count: int
    last_seen_utc: str
    ttl_hours: float = 168.0

    def is_expired(self, now_utc: str) -> bool:
        """Return True if the TTL has elapsed since *last_seen_utc*.

        Both *now_utc* and *last_seen_utc* are ISO-8601 strings.  If either
        cannot be parsed, the insight is conservatively considered *not*
        expired so we don't accidentally discard data due to clock skew.
        """
        try:
            last = datetime.fromisoformat(self.last_seen_utc)
            now = datetime.fromisoformat(now_utc)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            expiry = last + timedelta(hours=self.ttl_hours)
            return now > expiry
        except (ValueError, TypeError):
            return False

    def decay_confidence(self, days_past_ttl: float) -> float:
        """Return a decayed confidence score, reduced by 10% per day past TTL.

        The returned value is clamped to [0.0, 1.0].  Passing
        ``days_past_ttl=0`` returns the original confidence unchanged.
        """
        decayed = self.confidence * (1.0 - 0.10 * days_past_ttl)
        return max(0.0, min(1.0, decayed))


@dataclass(frozen=True)
class FileReputation:
    """Accumulated reputation of a source file within the pipeline.

    Args:
        file_path:          Repo-relative path to the file.
        change_count:       Total number of pipeline-driven changes to this file.
        success_rate:       Fraction of changes that passed all tests (0.0–1.0).
        avg_blast_radius:   Average number of test files impacted per change.
        common_co_failures: Tuple of paths most frequently failing alongside
                            this file (sorted by co-failure frequency descending).
        fragility_score:    0.0 (solid) → 1.0 (fragile) composite score.
    """

    file_path: str
    change_count: int
    success_rate: float
    avg_blast_radius: int
    common_co_failures: Tuple[str, ...]
    fragility_score: float


@dataclass(frozen=True)
class PatternSummary:
    """Aggregated view of active memory insights.

    Args:
        top_patterns:      Tuple of insights sorted by ``evidence_count`` desc.
        total_insights:    All known insights including archived.
        active_insights:   Insights not yet expired.
        archived_insights: Insights past their TTL but retained for audit.
    """

    top_patterns: Tuple[MemoryInsight, ...]
    total_insights: int
    active_insights: int
    archived_insights: int


# ---------------------------------------------------------------------------
# Dream types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImprovementBlueprint:
    """A pre-computed code-improvement plan produced during idle/dream time.

    Blueprints are keyed on (repo_sha, policy_hash, prompt_family, model_class).
    When any key component changes the blueprint is *stale* and must be
    discarded rather than applied.

    TC13: stale when ``repo_sha`` drifts from current HEAD.
    TC14: stale when ``policy_hash`` drifts from current policy.
    TC15: fresh when both match.
    """

    blueprint_id: str
    title: str
    description: str
    category: str           # "complexity" | "test_coverage" | "security" | "performance" | "debt"
    priority_score: float
    target_files: Tuple[str, ...]
    estimated_effort: str   # "small" | "medium" | "large"
    estimated_cost_usd: float
    repo: str
    repo_sha: str
    computed_at_utc: str
    ttl_hours: float
    model_used: str
    policy_hash: str
    oracle_neighborhood: Dict[str, Any]
    suggested_approach: str
    risk_assessment: str

    def is_stale(self, current_head: str, current_policy_hash: str) -> bool:
        """Return True if this blueprint should be discarded.

        Staleness is detected on either axis independently:
        - HEAD has advanced (repo_sha mismatch) — TC13
        - Policy has changed (policy_hash mismatch) — TC14
        """
        return (
            self.repo_sha != current_head
            or self.policy_hash != current_policy_hash
        )


@dataclass
class DreamMetrics:
    """Mutable runtime counters for the DreamEngine background loop.

    Not frozen — these are accumulated in-place during a process lifetime.
    """

    opportunistic_compute_minutes: float = 0.0
    preemptions_count: int = 0
    blueprints_computed: int = 0
    blueprints_discarded_stale: int = 0
    blueprint_hit_rate: float = 0.0
    jobs_deduplicated: int = 0
    estimated_cost_saved_usd: float = 0.0


# ---------------------------------------------------------------------------
# Prophecy types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PredictedFailure:
    """A single test-file risk prediction from ProphecyEngine.

    Args:
        test_file:   Repo-relative path to the test module predicted to fail.
        probability: 0.0–1.0 estimated probability of failure.
        reason:      Short human-readable cause.
        evidence:    Supporting evidence text (e.g. callers, shared fixtures).
    """

    test_file: str
    probability: float
    reason: str
    evidence: str


@dataclass(frozen=True)
class ProphecyReport:
    """Complete failure-risk report for a proposed changeset.

    Args:
        change_id:          Identifier for the proposed change (op_id or git ref).
        risk_level:         "low" | "medium" | "high" | "critical"
        predicted_failures: Ordered tuple of PredictedFailure (highest probability first).
        confidence:         0.0–1.0 overall confidence in the prediction.
        reasoning:          Narrative summary of the reasoning.
        recommended_tests:  Paths of test files that should be run to validate the change.
    """

    change_id: str
    risk_level: str
    predicted_failures: Tuple[PredictedFailure, ...]
    confidence: float
    reasoning: str
    recommended_tests: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class UserActivityMonitor(Protocol):
    """Structural protocol for objects that can report user idle time.

    ConsciousnessService (and DreamEngine) accept any object implementing
    this interface — including the real VoiceGate and test doubles.
    """

    def last_activity_s(self) -> float:
        """Return seconds elapsed since the last detected user interaction."""
        ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConsciousnessConfig:
    """Immutable runtime configuration for the Trinity Consciousness layer.

    All fields are read from environment variables via ``from_env()`` with
    safe defaults so the system degrades gracefully when vars are absent.

    Environment variables (all prefixed ``JARVIS_CONSCIOUSNESS_``):

    =============================================  ========  =======
    Variable                                       Type      Default
    =============================================  ========  =======
    JARVIS_CONSCIOUSNESS_ENABLED                   bool      true
    JARVIS_CONSCIOUSNESS_HEALTH_POLL_S             float     30.0
    JARVIS_CONSCIOUSNESS_DREAM_ENABLED             bool      true
    JARVIS_CONSCIOUSNESS_DREAM_IDLE_THRESHOLD_S    float     300.0
    JARVIS_CONSCIOUSNESS_DREAM_REENTRY_COOLDOWN_S  float     60.0
    JARVIS_CONSCIOUSNESS_DREAM_MAX_MINUTES_PER_DAY float     120.0
    JARVIS_CONSCIOUSNESS_DREAM_BLUEPRINT_TTL_HOURS float     24.0
    JARVIS_CONSCIOUSNESS_PROPHECY_ENABLED          bool      true
    JARVIS_CONSCIOUSNESS_MEMORY_TTL_HOURS          float     168.0
    JARVIS_CONSCIOUSNESS_BRIEFING_ON_STARTUP       bool      true
    =============================================  ========  =======
    """

    enabled: bool
    health_poll_interval_s: float
    dream_enabled: bool
    dream_idle_threshold_s: float
    dream_reentry_cooldown_s: float
    dream_max_minutes_per_day: float
    dream_blueprint_ttl_hours: float
    prophecy_enabled: bool
    memory_ttl_hours: float
    briefing_on_startup: bool

    @classmethod
    def from_env(cls) -> ConsciousnessConfig:
        """Construct configuration from environment variables.

        All values are read fresh on each call — do not call in hot paths.
        """
        def _bool(key: str, default: str) -> bool:
            return os.getenv(key, default).lower().strip() in ("1", "true", "yes", "on")

        def _float(key: str, default: str) -> float:
            return float(os.getenv(key, default))

        return cls(
            enabled=_bool("JARVIS_CONSCIOUSNESS_ENABLED", "true"),
            health_poll_interval_s=_float("JARVIS_CONSCIOUSNESS_HEALTH_POLL_S", "30.0"),
            dream_enabled=_bool("JARVIS_CONSCIOUSNESS_DREAM_ENABLED", "true"),
            dream_idle_threshold_s=_float("JARVIS_CONSCIOUSNESS_DREAM_IDLE_THRESHOLD_S", "300.0"),
            dream_reentry_cooldown_s=_float("JARVIS_CONSCIOUSNESS_DREAM_REENTRY_COOLDOWN_S", "60.0"),
            dream_max_minutes_per_day=_float("JARVIS_CONSCIOUSNESS_DREAM_MAX_MINUTES_PER_DAY", "120.0"),
            dream_blueprint_ttl_hours=_float("JARVIS_CONSCIOUSNESS_DREAM_BLUEPRINT_TTL_HOURS", "24.0"),
            prophecy_enabled=_bool("JARVIS_CONSCIOUSNESS_PROPHECY_ENABLED", "true"),
            memory_ttl_hours=_float("JARVIS_CONSCIOUSNESS_MEMORY_TTL_HOURS", "168.0"),
            briefing_on_startup=_bool("JARVIS_CONSCIOUSNESS_BRIEFING_ON_STARTUP", "true"),
        )
