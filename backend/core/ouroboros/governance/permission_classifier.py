"""
Permission Auto-Approve Classifier
====================================

ML-adjacent permission classifier that learns which governance operations
are safe to auto-approve based on a set of weighted rules and historical
outcome data.

Rules are evaluated as a **weighted vote** — each matching rule contributes
its weight toward ``AUTO_APPROVE``, ``REQUIRE_REVIEW``, or ``AUTO_DENY``.
The decision with the highest total weight wins (with a bias toward
caution: ``AUTO_DENY`` threshold is 0.5, ``AUTO_APPROVE`` is 0.7, and
anything in between requires human review).

Outcomes are logged to ``~/.jarvis/ouroboros/permission_history.jsonl``
and used by ``learn_from_history()`` to adjust rule weights based on
real-world success rates.

Environment variables
---------------------
``JARVIS_PERMISSION_HISTORY_PATH``
    Path to the JSONL history file (default ``~/.jarvis/ouroboros/permission_history.jsonl``).
``JARVIS_PERMISSION_AUTO_APPROVE_THRESHOLD``
    Weight threshold for auto-approve (default ``0.7``).
``JARVIS_PERMISSION_AUTO_DENY_THRESHOLD``
    Weight threshold for auto-deny (default ``0.5``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.PermissionClassifier")

# ---------------------------------------------------------------------------
# Environment defaults
# ---------------------------------------------------------------------------

_DEFAULT_HISTORY_PATH = Path(
    os.environ.get(
        "JARVIS_PERMISSION_HISTORY_PATH",
        str(Path.home() / ".jarvis" / "ouroboros" / "permission_history.jsonl"),
    )
)
_AUTO_APPROVE_THRESHOLD: float = float(
    os.environ.get("JARVIS_PERMISSION_AUTO_APPROVE_THRESHOLD", "0.7")
)
_AUTO_DENY_THRESHOLD: float = float(
    os.environ.get("JARVIS_PERMISSION_AUTO_DENY_THRESHOLD", "0.5")
)

# ---------------------------------------------------------------------------
# Critical file patterns — changes to these require human review at minimum
# ---------------------------------------------------------------------------

_CRITICAL_FILE_NAMES: frozenset[str] = frozenset({
    "unified_supervisor.py",
    "governed_loop_service.py",
    "prime_router.py",
    "distributed_lock_manager.py",
})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PermissionDecision(Enum):
    """Possible classifier outcomes."""

    AUTO_APPROVE = auto()
    REQUIRE_REVIEW = auto()
    AUTO_DENY = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionSignal:
    """Feature vector extracted from an operation for classification."""

    tool_name: str
    target_files: Tuple[str, ...]
    operation_type: str
    risk_tier: str
    blast_radius: int
    test_coverage: float
    time_of_day_hour: int
    recent_failures: int
    file_staleness_days: float
    author_trust_score: float


@dataclass
class PermissionRule:
    """A single weighted classification rule.

    ``match_fn`` is a callable that receives a ``PermissionSignal`` and
    returns ``True`` if this rule fires.  ``decision`` is the vote cast
    when the rule matches, weighted by ``weight``.
    """

    name: str
    condition: str  # human-readable description
    decision: PermissionDecision
    weight: float
    match_fn: Callable[[PermissionSignal], bool]


# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------


def _all_files_under(signal: PermissionSignal, prefix: str) -> bool:
    """Check whether every target file starts with *prefix*."""
    return bool(signal.target_files) and all(
        f.startswith(prefix) or f"/{prefix}" in f for f in signal.target_files
    )


def _any_file_matches_critical(signal: PermissionSignal) -> bool:
    """Check whether any target file is a known critical file."""
    for f in signal.target_files:
        basename = f.rsplit("/", 1)[-1] if "/" in f else f
        if basename in _CRITICAL_FILE_NAMES:
            return True
    return False


_DEFAULT_RULES: List[PermissionRule] = [
    PermissionRule(
        name="test_only_changes",
        condition="All target files are under tests/",
        decision=PermissionDecision.AUTO_APPROVE,
        weight=0.9,
        match_fn=lambda s: _all_files_under(s, "tests/"),
    ),
    PermissionRule(
        name="doc_only_changes",
        condition="All target files are under docs/",
        decision=PermissionDecision.AUTO_APPROVE,
        weight=0.9,
        match_fn=lambda s: _all_files_under(s, "docs/"),
    ),
    PermissionRule(
        name="low_risk_low_blast",
        condition="Risk tier is LOW and blast radius <= 2",
        decision=PermissionDecision.AUTO_APPROVE,
        weight=0.7,
        match_fn=lambda s: s.risk_tier == "LOW" and s.blast_radius <= 2,
    ),
    PermissionRule(
        name="high_coverage",
        condition="Test coverage >= 0.8 and risk tier is not CRITICAL",
        decision=PermissionDecision.AUTO_APPROVE,
        weight=0.6,
        match_fn=lambda s: s.test_coverage >= 0.8 and s.risk_tier != "CRITICAL",
    ),
    PermissionRule(
        name="night_time_caution",
        condition="Time of day is between 0:00 and 6:00",
        decision=PermissionDecision.REQUIRE_REVIEW,
        weight=0.4,
        match_fn=lambda s: 0 <= s.time_of_day_hour <= 6,
    ),
    PermissionRule(
        name="critical_files",
        condition="Target includes unified_supervisor.py or governed_loop_service.py",
        decision=PermissionDecision.AUTO_DENY,
        weight=0.95,
        match_fn=lambda s: _any_file_matches_critical(s),
    ),
    PermissionRule(
        name="recent_failures",
        condition="3 or more failures in last 24h",
        decision=PermissionDecision.REQUIRE_REVIEW,
        weight=0.8,
        match_fn=lambda s: s.recent_failures >= 3,
    ),
    PermissionRule(
        name="stale_files",
        condition="Target files not modified in > 90 days",
        decision=PermissionDecision.REQUIRE_REVIEW,
        weight=0.5,
        match_fn=lambda s: s.file_staleness_days > 90,
    ),
    PermissionRule(
        name="untrusted_author",
        condition="Author trust score below 0.3",
        decision=PermissionDecision.AUTO_DENY,
        weight=0.7,
        match_fn=lambda s: s.author_trust_score < 0.3,
    ),
    PermissionRule(
        name="high_blast_radius",
        condition="Blast radius >= 10 files",
        decision=PermissionDecision.REQUIRE_REVIEW,
        weight=0.8,
        match_fn=lambda s: s.blast_radius >= 10,
    ),
]


# ---------------------------------------------------------------------------
# PermissionClassifier
# ---------------------------------------------------------------------------


class PermissionClassifier:
    """Weighted-vote permission classifier with outcome logging and learning.

    Usage::

        classifier = get_permission_classifier()
        signal = PermissionSignal(
            tool_name="file_edit",
            target_files=("tests/test_login.py",),
            operation_type="MODIFY",
            risk_tier="LOW",
            blast_radius=1,
            test_coverage=0.95,
            time_of_day_hour=14,
            recent_failures=0,
            file_staleness_days=3.0,
            author_trust_score=0.9,
        )
        decision, reason, confidence = classifier.classify(signal)
    """

    def __init__(
        self,
        rules: Optional[List[PermissionRule]] = None,
        history_path: Path = _DEFAULT_HISTORY_PATH,
    ) -> None:
        self._rules: List[PermissionRule] = list(rules or _DEFAULT_RULES)
        self._history_path = history_path
        self._lock = threading.RLock()

        # Counters for stats.
        self._total_decisions: int = 0
        self._decision_counts: Dict[PermissionDecision, int] = {
            d: 0 for d in PermissionDecision
        }

        # Ensure history directory exists.
        self._history_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "PermissionClassifier initialised (%d rules, history=%s)",
            len(self._rules),
            self._history_path,
        )

    # -- public API ---------------------------------------------------------

    def classify(
        self, signal: PermissionSignal
    ) -> Tuple[PermissionDecision, str, float]:
        """Classify an operation signal.

        Returns ``(decision, reason, confidence)`` where *confidence* is
        0.0-1.0 indicating how strongly the rules agreed.
        """
        with self._lock:
            # Accumulate votes per decision.
            weights: Dict[PermissionDecision, float] = {
                d: 0.0 for d in PermissionDecision
            }
            matched_rules: Dict[PermissionDecision, List[str]] = {
                d: [] for d in PermissionDecision
            }

            for rule in self._rules:
                try:
                    if rule.match_fn(signal):
                        weights[rule.decision] += rule.weight
                        matched_rules[rule.decision].append(rule.name)
                except Exception as exc:
                    logger.warning(
                        "rule %r raised during evaluation: %s", rule.name, exc
                    )

            # Determine outcome by threshold hierarchy.
            total_weight = sum(weights.values()) or 1.0

            # AUTO_DENY wins if its weight exceeds the deny threshold.
            if weights[PermissionDecision.AUTO_DENY] >= _AUTO_DENY_THRESHOLD:
                decision = PermissionDecision.AUTO_DENY
                confidence = weights[PermissionDecision.AUTO_DENY] / total_weight
                reasons = matched_rules[PermissionDecision.AUTO_DENY]
                reason = f"AUTO_DENY triggered by: {', '.join(reasons)}"

            # AUTO_APPROVE needs to clear a higher threshold.
            elif weights[PermissionDecision.AUTO_APPROVE] >= _AUTO_APPROVE_THRESHOLD:
                decision = PermissionDecision.AUTO_APPROVE
                confidence = weights[PermissionDecision.AUTO_APPROVE] / total_weight
                reasons = matched_rules[PermissionDecision.AUTO_APPROVE]
                reason = f"AUTO_APPROVE supported by: {', '.join(reasons)}"

            # Anything else falls to human review.
            else:
                decision = PermissionDecision.REQUIRE_REVIEW
                confidence = weights[PermissionDecision.REQUIRE_REVIEW] / total_weight
                # Build a composite reason explaining the ambiguity.
                parts = []
                for d in PermissionDecision:
                    if matched_rules[d]:
                        parts.append(
                            f"{d.name}({', '.join(matched_rules[d])})"
                        )
                reason = "No clear consensus — " + "; ".join(parts) if parts else "No rules matched"

            self._total_decisions += 1
            self._decision_counts[decision] += 1

            logger.info(
                "classify: %s (confidence=%.2f) — %s",
                decision.name,
                confidence,
                reason,
            )
            return decision, reason, confidence

    def record_outcome(
        self,
        signal: PermissionSignal,
        decision: PermissionDecision,
        actual_outcome: str,
    ) -> None:
        """Log an outcome to the history file for future learning."""
        with self._lock:
            record = {
                "timestamp": time.time(),
                "signal": {
                    "tool_name": signal.tool_name,
                    "target_files": list(signal.target_files),
                    "operation_type": signal.operation_type,
                    "risk_tier": signal.risk_tier,
                    "blast_radius": signal.blast_radius,
                    "test_coverage": signal.test_coverage,
                    "time_of_day_hour": signal.time_of_day_hour,
                    "recent_failures": signal.recent_failures,
                    "file_staleness_days": signal.file_staleness_days,
                    "author_trust_score": signal.author_trust_score,
                },
                "decision": decision.name,
                "actual_outcome": actual_outcome,
            }
            try:
                with open(self._history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, default=str) + "\n")
                logger.debug("outcome recorded: %s -> %s", decision.name, actual_outcome)
            except OSError as exc:
                logger.warning("failed to write outcome: %s", exc)

    def learn_from_history(self) -> int:
        """Adjust rule weights based on historical outcomes.

        Reads the history file and computes per-rule success rates.
        Rules whose matched decisions led to successful outcomes get
        their weights boosted; those leading to failures get dampened.

        Returns the number of rules whose weights were adjusted.
        """
        with self._lock:
            if not self._history_path.exists():
                logger.info("learn: no history file found")
                return 0

            # Load history records.
            records: List[Dict[str, Any]] = []
            try:
                with open(self._history_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except OSError as exc:
                logger.warning("learn: failed to read history: %s", exc)
                return 0

            if not records:
                return 0

            # For each rule, count how many times its decision led to
            # "success" vs "failure" outcomes.
            rule_stats: Dict[str, Dict[str, int]] = {}
            for rec in records:
                sig_data = rec.get("signal", {})
                outcome = rec.get("actual_outcome", "")
                is_success = outcome.lower() in ("success", "applied", "passed", "ok")

                # Reconstruct signal to evaluate rules.
                try:
                    signal = PermissionSignal(
                        tool_name=sig_data.get("tool_name", ""),
                        target_files=tuple(sig_data.get("target_files", [])),
                        operation_type=sig_data.get("operation_type", ""),
                        risk_tier=sig_data.get("risk_tier", ""),
                        blast_radius=sig_data.get("blast_radius", 0),
                        test_coverage=sig_data.get("test_coverage", 0.0),
                        time_of_day_hour=sig_data.get("time_of_day_hour", 12),
                        recent_failures=sig_data.get("recent_failures", 0),
                        file_staleness_days=sig_data.get("file_staleness_days", 0.0),
                        author_trust_score=sig_data.get("author_trust_score", 0.5),
                    )
                except (TypeError, KeyError):
                    continue

                for rule in self._rules:
                    try:
                        if rule.match_fn(signal):
                            if rule.name not in rule_stats:
                                rule_stats[rule.name] = {"success": 0, "failure": 0}
                            if is_success:
                                rule_stats[rule.name]["success"] += 1
                            else:
                                rule_stats[rule.name]["failure"] += 1
                    except Exception:
                        continue

            # Adjust weights.
            adjusted = 0
            for rule in self._rules:
                stats = rule_stats.get(rule.name)
                if stats is None:
                    continue
                total = stats["success"] + stats["failure"]
                if total < 5:
                    # Not enough data to learn from.
                    continue
                success_rate = stats["success"] / total
                old_weight = rule.weight

                # Nudge weight toward success rate, but clamp to [0.1, 0.99].
                # Learning rate is deliberately conservative.
                learning_rate = 0.1
                new_weight = rule.weight + learning_rate * (success_rate - rule.weight)
                rule.weight = max(0.1, min(0.99, new_weight))

                if abs(rule.weight - old_weight) > 0.001:
                    adjusted += 1
                    logger.info(
                        "learn: rule %r weight %.3f -> %.3f (success_rate=%.2f, n=%d)",
                        rule.name,
                        old_weight,
                        rule.weight,
                        success_rate,
                        total,
                    )

            logger.info(
                "learn: processed %d records, adjusted %d rules",
                len(records),
                adjusted,
            )
            return adjusted

    def stats(self) -> Dict[str, Any]:
        """Return classification statistics."""
        with self._lock:
            total = self._total_decisions or 1
            return {
                "total_decisions": self._total_decisions,
                "auto_approve_rate": self._decision_counts[PermissionDecision.AUTO_APPROVE] / total,
                "deny_rate": self._decision_counts[PermissionDecision.AUTO_DENY] / total,
                "review_rate": self._decision_counts[PermissionDecision.REQUIRE_REVIEW] / total,
                "rules": len(self._rules),
                "rule_weights": {r.name: r.weight for r in self._rules},
            }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[PermissionClassifier] = None
_singleton_lock = threading.Lock()


def get_permission_classifier() -> PermissionClassifier:
    """Return the process-wide ``PermissionClassifier`` singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PermissionClassifier()
        return _singleton
