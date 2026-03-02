"""Autonomous Gmail Triage v1 — score, label, and notify on incoming emails.

Public API:
    EmailTriageRunner  — singleton runner, called by agent_runtime
    TriageConfig       — configuration (feature flags, thresholds, env vars)
    score_email        — pure deterministic scoring function
    extract_features   — structured feature extraction with heuristic fallback
"""

from autonomy.email_triage.config import TriageConfig, get_triage_config
from autonomy.email_triage.runner import EmailTriageRunner
from autonomy.email_triage.scoring import score_email
from autonomy.email_triage.extraction import extract_features

__all__ = [
    "EmailTriageRunner",
    "TriageConfig",
    "get_triage_config",
    "score_email",
    "extract_features",
]
