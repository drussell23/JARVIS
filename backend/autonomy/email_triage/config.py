"""Configuration for the email triage system.

All settings are env-var configurable. Single source of truth.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.email_triage.config")


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass
class TriageConfig:
    """Email triage configuration. All fields have safe defaults."""

    # Feature flags
    enabled: bool = False
    notify_tier1: bool = True
    notify_tier2: bool = True
    quarantine_tier4: bool = False
    extraction_enabled: bool = True
    summaries_enabled: bool = True

    # Scoring
    scoring_version: str = "v1"

    # Tier thresholds
    tier1_min: int = 85
    tier2_min: int = 65
    tier3_min: int = 35

    # Gmail labels
    label_tier1: str = "jarvis/tier1_critical"
    label_tier2: str = "jarvis/tier2_high"
    label_tier3: str = "jarvis/tier3_review"
    label_tier4: str = "jarvis/tier4_noise"

    # Quiet hours (local time)
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8

    # Dedup windows (seconds)
    dedup_tier1_s: int = 900
    dedup_tier2_s: int = 3600

    # Interrupt budget
    max_interrupts_per_hour: int = 3
    max_interrupts_per_day: int = 12

    # Summary
    summary_interval_s: int = 1800

    # Runner
    poll_interval_s: float = 60.0
    max_emails_per_cycle: int = 25
    cycle_timeout_s: float = 30.0

    # C2: Throughput hardening
    extraction_concurrency: int = 3  # parallel extraction tasks
    extraction_per_email_timeout_s: float = 20.0  # per-email deadline
    extraction_fixed_overhead_s: float = 10.0  # fetch + label + notify budget
    adaptive_admission: bool = True  # auto-shrink batch to fit budget
    latency_ema_alpha: float = 0.3  # EMA smoothing for p95 tracking

    # Phase B: Action commit ledger
    ledger_lease_duration_s: float = 60.0  # cycle_timeout + buffer

    # Dependency resolution
    dep_backoff_base_s: float = 5.0
    dep_backoff_max_s: float = 300.0

    # Staleness
    staleness_window_s: float = 120.0

    # Commit policy
    commit_error_threshold: float = 0.5

    # Notification delivery
    notification_budget_s: float = 10.0
    summary_budget_s: float = 5.0
    immediate_flush_threshold: int = 10
    max_summary_items: int = 20

    # State persistence (WS1)
    state_persistence_enabled: bool = True
    state_db_path: str = ""  # default: ~/.jarvis/email_triage_state.db
    outbox_retry_limit: int = 3
    outbox_replay_on_start: bool = True
    snapshot_retention_count: int = 10

    # Adaptive scoring / Reactor-Core (WS5)
    adaptive_scoring_enabled: bool = False
    outcome_collection_enabled: bool = True
    min_outcomes_for_adaptation: int = 50  # high-confidence only
    weight_bounds_pct: float = 20.0  # max ±20% drift from defaults
    shadow_cycles: int = 5  # shadow before activating adapted weights
    shadow_tier_drift_threshold: float = 0.10  # 10% tier disagreement → rollback
    outcome_lookback_cycles: int = 2  # check N prior cycles for outcomes

    @classmethod
    def from_env(cls) -> TriageConfig:
        """Build config from environment variables."""
        return cls(
            enabled=_env_bool("EMAIL_TRIAGE_ENABLED", False),
            notify_tier1=_env_bool("EMAIL_TRIAGE_NOTIFY_TIER1", True),
            notify_tier2=_env_bool("EMAIL_TRIAGE_NOTIFY_TIER2", True),
            quarantine_tier4=_env_bool("EMAIL_TRIAGE_QUARANTINE_TIER4", False),
            extraction_enabled=_env_bool("EMAIL_TRIAGE_EXTRACTION_ENABLED", True),
            summaries_enabled=_env_bool("EMAIL_TRIAGE_SUMMARIES_ENABLED", True),
            quiet_start_hour=_env_int("EMAIL_TRIAGE_QUIET_START", 23),
            quiet_end_hour=_env_int("EMAIL_TRIAGE_QUIET_END", 8),
            dedup_tier1_s=_env_int("EMAIL_TRIAGE_DEDUP_TIER1_S", 900),
            dedup_tier2_s=_env_int("EMAIL_TRIAGE_DEDUP_TIER2_S", 3600),
            max_interrupts_per_hour=_env_int("EMAIL_TRIAGE_MAX_INTERRUPTS_HOUR", 3),
            max_interrupts_per_day=_env_int("EMAIL_TRIAGE_MAX_INTERRUPTS_DAY", 12),
            summary_interval_s=_env_int("EMAIL_TRIAGE_SUMMARY_INTERVAL_S", 1800),
            poll_interval_s=_env_float("EMAIL_TRIAGE_POLL_INTERVAL_S", 60.0),
            max_emails_per_cycle=_env_int("EMAIL_TRIAGE_MAX_PER_CYCLE", 25),
            cycle_timeout_s=_env_float("EMAIL_TRIAGE_CYCLE_TIMEOUT_S", 30.0),
            # C2: Throughput hardening
            extraction_concurrency=_env_int("EMAIL_TRIAGE_EXTRACTION_CONCURRENCY", 3),
            extraction_per_email_timeout_s=_env_float("EMAIL_TRIAGE_EXTRACTION_TIMEOUT_S", 20.0),
            extraction_fixed_overhead_s=_env_float("EMAIL_TRIAGE_FIXED_OVERHEAD_S", 10.0),
            adaptive_admission=_env_bool("EMAIL_TRIAGE_ADAPTIVE_ADMISSION", True),
            latency_ema_alpha=_env_float("EMAIL_TRIAGE_LATENCY_EMA_ALPHA", 0.3),
            # Phase B: Action commit ledger
            ledger_lease_duration_s=_env_float("EMAIL_TRIAGE_LEDGER_LEASE_S", 60.0),
            dep_backoff_base_s=_env_float("EMAIL_TRIAGE_DEP_BACKOFF_BASE_S", 5.0),
            dep_backoff_max_s=_env_float("EMAIL_TRIAGE_DEP_BACKOFF_MAX_S", 300.0),
            staleness_window_s=_env_float("EMAIL_TRIAGE_STALENESS_WINDOW_S", 120.0),
            commit_error_threshold=_env_float("EMAIL_TRIAGE_COMMIT_ERROR_THRESHOLD", 0.5),
            notification_budget_s=_env_float("EMAIL_TRIAGE_NOTIFICATION_BUDGET_S", 10.0),
            summary_budget_s=_env_float("EMAIL_TRIAGE_SUMMARY_BUDGET_S", 5.0),
            immediate_flush_threshold=_env_int("EMAIL_TRIAGE_IMMEDIATE_FLUSH_THRESHOLD", 10),
            max_summary_items=_env_int("EMAIL_TRIAGE_MAX_SUMMARY_ITEMS", 20),
            # State persistence (WS1)
            state_persistence_enabled=_env_bool("EMAIL_TRIAGE_STATE_PERSISTENCE", True),
            state_db_path=os.getenv("EMAIL_TRIAGE_STATE_DB", ""),
            outbox_retry_limit=_env_int("EMAIL_TRIAGE_OUTBOX_RETRY_LIMIT", 3),
            outbox_replay_on_start=_env_bool("EMAIL_TRIAGE_OUTBOX_REPLAY", True),
            snapshot_retention_count=_env_int("EMAIL_TRIAGE_SNAPSHOT_RETENTION", 10),
            # Adaptive scoring / Reactor-Core (WS5)
            adaptive_scoring_enabled=_env_bool("EMAIL_TRIAGE_ADAPTIVE_SCORING", False),
            outcome_collection_enabled=_env_bool("EMAIL_TRIAGE_OUTCOME_COLLECTION", True),
            min_outcomes_for_adaptation=_env_int("EMAIL_TRIAGE_MIN_OUTCOMES", 50),
            weight_bounds_pct=_env_float("EMAIL_TRIAGE_WEIGHT_BOUNDS_PCT", 20.0),
            shadow_cycles=_env_int("EMAIL_TRIAGE_SHADOW_CYCLES", 5),
            shadow_tier_drift_threshold=_env_float("EMAIL_TRIAGE_SHADOW_DRIFT_THRESHOLD", 0.10),
            outcome_lookback_cycles=_env_int("EMAIL_TRIAGE_OUTCOME_LOOKBACK", 2),
        )

    def tier_for_score(self, score: int) -> int:
        """Map score (0-100) to tier (1-4)."""
        if score >= self.tier1_min:
            return 1
        if score >= self.tier2_min:
            return 2
        if score >= self.tier3_min:
            return 3
        return 4

    def label_for_tier(self, tier: int) -> str:
        """Map tier (1-4) to Gmail label name."""
        return {
            1: self.label_tier1,
            2: self.label_tier2,
            3: self.label_tier3,
            4: self.label_tier4,
        }.get(tier, self.label_tier4)


_singleton: Optional[TriageConfig] = None


def get_triage_config() -> TriageConfig:
    """Get the singleton config instance."""
    global _singleton
    if _singleton is None:
        _singleton = TriageConfig.from_env()
    return _singleton


def reset_triage_config() -> None:
    """Reset singleton (for testing)."""
    global _singleton
    _singleton = None
