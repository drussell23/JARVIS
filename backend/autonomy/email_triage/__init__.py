"""Autonomous Gmail Triage v1.1 — score, label, notify, and enrich emails.

Public API:
    EmailTriageRunner  — singleton runner, called by agent_runtime
    TriageConfig       — configuration (feature flags, thresholds, env vars)
    score_email        — pure deterministic scoring function
    extract_features   — structured feature extraction with heuristic fallback
    enrich_with_triage — enrich raw emails with triage metadata
    DependencyResolver — dependency resolution with backoff
    DependencyHealth   — per-dependency health tracking
"""

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.extraction import extract_features
from autonomy.email_triage.enrichment import enrich_with_triage
from autonomy.email_triage.dependencies import DependencyResolver, DependencyHealth

__all__ = [
    "EmailTriageRunner",
    "TriageConfig",
    "get_triage_config",
    "score_email",
    "extract_features",
    "enrich_with_triage",
    "DependencyResolver",
    "DependencyHealth",
]
