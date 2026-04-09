"""
Adaptive Learning — Consolidation, positive feedback, and threshold tuning.

Three P2 intelligence gaps closed in one module:

1. LearningConsolidator: Periodically synthesizes domain-level rules from
   outcome history. "We fail at voice code 70% of the time because of
   CoreAudio threading" becomes an actionable context injection.

2. SuccessPatternStore: Records (domain, context, approach) triples from
   successful operations. On future similar tasks, injects "a similar task
   succeeded with this approach" into the generation prompt.

3. ThresholdTuner: Adjusts entropy/staleness/confidence thresholds based
   on observed false positive and miss rates. Self-calibrating organism.

Boundary Principle:
  ALL computation is deterministic — statistical aggregation, pattern
  matching, threshold adjustment via moving averages. No model inference.
  The INTERPRETATION of these signals by the generation prompt is agentic.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CONSOLIDATION_INTERVAL_S = float(
    os.environ.get("JARVIS_LEARNING_CONSOLIDATION_INTERVAL_S", "3600")
)
_MIN_OUTCOMES_FOR_RULE = int(
    os.environ.get("JARVIS_LEARNING_MIN_OUTCOMES_FOR_RULE", "5")
)
_SUCCESS_PATTERN_MAX_AGE_S = float(
    os.environ.get("JARVIS_SUCCESS_PATTERN_MAX_AGE_S", "604800")  # 7 days
)
_THRESHOLD_TUNING_WINDOW = int(
    os.environ.get("JARVIS_THRESHOLD_TUNING_WINDOW", "50")
)
_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_ADAPTIVE_LEARNING_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "learning"),
    )
)


# ---------------------------------------------------------------------------
# 1. Learning Consolidator — domain-level rule synthesis
# ---------------------------------------------------------------------------

@dataclass
class DomainRule:
    """A synthesized rule from historical outcomes.

    Not a hardcoded policy — a statistical observation that the system
    discovered about its own performance. Can be injected into generation
    prompts as context.
    """
    domain_key: str            # e.g., "code_gen::.py"
    rule_type: str             # "always_include", "avoid_pattern", "prefer_provider"
    description: str           # Human-readable rule
    confidence: float          # [0.0, 1.0] — statistical confidence
    sample_size: int           # How many outcomes this is based on
    created_at: float = field(default_factory=time.time)


class LearningConsolidator:
    """Synthesizes domain-level rules from LearningBridge outcome history.

    Runs periodically (default: hourly). Analyzes outcome patterns and
    generates actionable rules like:
    - "For voice_unlock code, always include backend/voice_unlock/core/verify.py in context"
    - "For governance changes, prefer Doubleword over J-Prime (higher success rate)"
    - "For test files, the most common failure is import errors — check imports first"

    All computation is deterministic (statistical aggregation). The rules
    are injected into generation prompts as additional context.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._rules: Dict[str, List[DomainRule]] = {}
        self._last_consolidation: float = 0.0
        self._load_persisted_rules()

    def consolidate(self, outcomes: List[Dict[str, Any]]) -> List[DomainRule]:
        """Analyze outcomes and generate domain-level rules.

        Parameters
        ----------
        outcomes:
            List of outcome dicts with keys: domain_key, success (bool),
            error_pattern (str), provider (str), target_files (list).

        Returns new rules discovered in this consolidation pass.
        """
        if not outcomes:
            return []

        new_rules: List[DomainRule] = []

        # Group by domain
        by_domain: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for outcome in outcomes:
            domain = outcome.get("domain_key", "unknown")
            by_domain[domain].append(outcome)

        for domain, domain_outcomes in by_domain.items():
            if len(domain_outcomes) < _MIN_OUTCOMES_FOR_RULE:
                continue

            # Rule 1: High failure rate domains — identify common error patterns
            failures = [o for o in domain_outcomes if not o.get("success", False)]
            failure_rate = len(failures) / len(domain_outcomes)

            if failure_rate > 0.5 and len(failures) >= 3:
                # Find most common error pattern
                error_counts: Dict[str, int] = defaultdict(int)
                for f in failures:
                    pattern = f.get("error_pattern", "unknown")
                    if pattern:
                        error_counts[pattern] += 1

                if error_counts:
                    top_error, top_count = max(error_counts.items(), key=lambda x: x[1])
                    error_pct = top_count / len(failures)

                    new_rules.append(DomainRule(
                        domain_key=domain,
                        rule_type="common_failure",
                        description=(
                            f"Domain '{domain}' fails {failure_rate:.0%} of the time. "
                            f"Most common error ({error_pct:.0%} of failures): {top_error}. "
                            f"Address this error pattern proactively in your solution."
                        ),
                        confidence=min(1.0, failure_rate * error_pct),
                        sample_size=len(domain_outcomes),
                    ))

            # Rule 2: Provider preference — which provider succeeds more
            provider_success: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))
            for o in domain_outcomes:
                provider = o.get("provider", "unknown")
                s, t = provider_success[provider]
                if o.get("success", False):
                    provider_success[provider] = (s + 1, t + 1)
                else:
                    provider_success[provider] = (s, t + 1)

            if len(provider_success) >= 2:
                best_provider = max(
                    provider_success.items(),
                    key=lambda x: x[1][0] / max(1, x[1][1]),
                )
                best_name, (best_s, best_t) = best_provider
                best_rate = best_s / max(1, best_t)

                if best_rate > 0.7 and best_t >= 3:
                    new_rules.append(DomainRule(
                        domain_key=domain,
                        rule_type="prefer_provider",
                        description=(
                            f"For domain '{domain}', provider '{best_name}' has "
                            f"the highest success rate ({best_rate:.0%} over "
                            f"{best_t} attempts)."
                        ),
                        confidence=best_rate,
                        sample_size=best_t,
                    ))

            # Rule 3: Common target files in successful operations
            success_files: Dict[str, int] = defaultdict(int)
            successes = [o for o in domain_outcomes if o.get("success", False)]
            for o in successes:
                for f in o.get("target_files", []):
                    success_files[f] += 1

            if successes and success_files:
                # Files that appear in >50% of successes
                threshold = len(successes) * 0.5
                common_files = [
                    f for f, count in success_files.items()
                    if count >= threshold
                ]
                if common_files:
                    new_rules.append(DomainRule(
                        domain_key=domain,
                        rule_type="always_include",
                        description=(
                            f"For domain '{domain}', these files appear in most "
                            f"successful operations: {', '.join(common_files[:5])}. "
                            f"Consider including them in context."
                        ),
                        confidence=0.7,
                        sample_size=len(successes),
                    ))

        # Store and persist
        for rule in new_rules:
            self._rules.setdefault(rule.domain_key, []).append(rule)

        if new_rules:
            self._persist_rules()
            logger.info(
                "[LearningConsolidator] Generated %d new rules from %d outcomes",
                len(new_rules), len(outcomes),
            )

        self._last_consolidation = time.time()
        return new_rules

    def get_rules_for_domain(self, domain_key: str) -> List[DomainRule]:
        """Get consolidated rules for a domain. Deterministic lookup."""
        return self._rules.get(domain_key, [])

    def format_rules_for_prompt(self, domain_key: str) -> str:
        """Format domain rules as context for injection into generation prompt."""
        rules = self.get_rules_for_domain(domain_key)
        if not rules:
            return ""

        lines = [f"## Historical Insights for Domain: {domain_key}"]
        for rule in rules:
            lines.append(
                f"- [{rule.rule_type}] (confidence: {rule.confidence:.0%}, "
                f"n={rule.sample_size}): {rule.description}"
            )
        return "\n".join(lines)

    def _persist_rules(self) -> None:
        """Persist rules to disk. Fault-isolated."""
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "consolidated_rules.json"
            data = {}
            for domain, rules in self._rules.items():
                data[domain] = [
                    {
                        "domain_key": r.domain_key,
                        "rule_type": r.rule_type,
                        "description": r.description,
                        "confidence": r.confidence,
                        "sample_size": r.sample_size,
                        "created_at": r.created_at,
                    }
                    for r in rules
                ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("[LearningConsolidator] Persist failed", exc_info=True)

    def _load_persisted_rules(self) -> None:
        """Load persisted rules from disk. Fault-isolated."""
        try:
            path = self._persistence_dir / "consolidated_rules.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for domain, rules_data in data.items():
                self._rules[domain] = [
                    DomainRule(**r) for r in rules_data
                ]
            logger.info(
                "[LearningConsolidator] Loaded %d rules across %d domains",
                sum(len(r) for r in self._rules.values()),
                len(self._rules),
            )
        except Exception:
            logger.debug("[LearningConsolidator] Load failed", exc_info=True)


# ---------------------------------------------------------------------------
# 2. Success Pattern Store — positive feedback loop
# ---------------------------------------------------------------------------

@dataclass
class SuccessPattern:
    """A recorded successful approach for a domain."""
    domain_key: str
    description: str           # What the task was
    approach_summary: str      # What worked (provider, key decisions)
    target_files: Tuple[str, ...]
    provider: str
    timestamp: float = field(default_factory=time.time)


class SuccessPatternStore:
    """Records successful operation patterns for positive reinforcement.

    When a governance operation succeeds, the (domain, context, approach)
    triple is recorded. On future similar operations, the store provides
    "a similar task succeeded with this approach" context.

    This is the POSITIVE counterpart to EpisodicMemory's failure tracking.
    The organism learns from what WORKS, not just what breaks.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._patterns: Dict[str, List[SuccessPattern]] = defaultdict(list)
        self._load()

    def record_success(
        self,
        domain_key: str,
        description: str,
        target_files: Tuple[str, ...],
        provider: str,
        approach_summary: str = "",
    ) -> None:
        """Record a successful operation pattern. Deterministic write."""
        pattern = SuccessPattern(
            domain_key=domain_key,
            description=description,
            approach_summary=approach_summary or f"Succeeded via {provider}",
            target_files=target_files,
            provider=provider,
        )
        self._patterns[domain_key].append(pattern)

        # Prune old patterns (keep last 20 per domain)
        if len(self._patterns[domain_key]) > 20:
            self._patterns[domain_key] = self._patterns[domain_key][-20:]

        self._persist()

    def get_similar_successes(
        self, domain_key: str, target_files: Tuple[str, ...], limit: int = 3
    ) -> List[SuccessPattern]:
        """Find successful patterns for similar operations.

        Matches by domain key and file overlap. Deterministic.
        """
        candidates = self._patterns.get(domain_key, [])
        now = time.time()

        # Filter out stale patterns
        fresh = [
            p for p in candidates
            if (now - p.timestamp) < _SUCCESS_PATTERN_MAX_AGE_S
        ]

        if not fresh:
            return []

        # Score by file overlap
        target_set = set(target_files)
        scored = []
        for p in fresh:
            overlap = len(target_set & set(p.target_files))
            scored.append((overlap, p))

        scored.sort(key=lambda x: (-x[0], -x[1].timestamp))
        return [p for _, p in scored[:limit]]

    def format_for_prompt(
        self, domain_key: str, target_files: Tuple[str, ...]
    ) -> str:
        """Format success patterns as context for generation prompt."""
        patterns = self.get_similar_successes(domain_key, target_files)
        if not patterns:
            return ""

        lines = ["## Successful Past Approaches"]
        for p in patterns:
            lines.append(
                f"- **{p.description[:100]}** — {p.approach_summary} "
                f"(files: {', '.join(p.target_files[:3])})"
            )
        lines.append(
            "\nConsider applying similar approaches. "
            "These patterns have historically succeeded for this domain."
        )
        return "\n".join(lines)

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "success_patterns.json"
            data = {}
            for domain, patterns in self._patterns.items():
                data[domain] = [
                    {
                        "domain_key": p.domain_key,
                        "description": p.description,
                        "approach_summary": p.approach_summary,
                        "target_files": list(p.target_files),
                        "provider": p.provider,
                        "timestamp": p.timestamp,
                    }
                    for p in patterns
                ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("[SuccessPatternStore] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "success_patterns.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for domain, patterns_data in data.items():
                self._patterns[domain] = [
                    SuccessPattern(
                        domain_key=p["domain_key"],
                        description=p["description"],
                        approach_summary=p["approach_summary"],
                        target_files=tuple(p["target_files"]),
                        provider=p["provider"],
                        timestamp=p.get("timestamp", 0),
                    )
                    for p in patterns_data
                ]
        except Exception:
            logger.debug("[SuccessPatternStore] Load failed", exc_info=True)


# ---------------------------------------------------------------------------
# 3. Threshold Tuner — adaptive self-calibration
# ---------------------------------------------------------------------------

@dataclass
class ThresholdRecommendation:
    """A recommended threshold adjustment."""
    parameter: str             # env var name
    current_value: float
    recommended_value: float
    reason: str
    false_positive_rate: float
    miss_rate: float


class ThresholdTuner:
    """Adjusts entropy/staleness/confidence thresholds based on outcomes.

    Analyzes the relationship between threshold-triggered actions and their
    outcomes. If the system triggers too often without producing value
    (high false positive rate), thresholds are raised. If regressions slip
    through (high miss rate), thresholds are lowered.

    All computation is deterministic — exponential moving average over
    a sliding window of observations. No model inference.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        # Observations: (threshold_name, triggered: bool, outcome_was_correct: bool)
        self._observations: Dict[str, List[Tuple[bool, bool]]] = defaultdict(list)
        self._load()

    def record_observation(
        self,
        threshold_name: str,
        triggered: bool,
        outcome_correct: bool,
    ) -> None:
        """Record whether a threshold trigger led to a correct outcome.

        triggered=True, outcome_correct=True  → true positive (good trigger)
        triggered=True, outcome_correct=False → false positive (wasted work)
        triggered=False, outcome_correct=True → true negative (correctly skipped)
        triggered=False, outcome_correct=False → false negative (missed problem)
        """
        obs = self._observations[threshold_name]
        obs.append((triggered, outcome_correct))

        # Keep sliding window
        if len(obs) > _THRESHOLD_TUNING_WINDOW:
            self._observations[threshold_name] = obs[-_THRESHOLD_TUNING_WINDOW:]

        self._persist()

    def compute_recommendations(self) -> List[ThresholdRecommendation]:
        """Analyze observations and recommend threshold adjustments.

        Returns a list of recommendations. Deterministic computation.
        """
        recommendations = []

        _THRESHOLD_MAP = {
            "entropy_systemic": ("JARVIS_ENTROPY_SYSTEMIC_THRESHOLD", 0.7),
            "entropy_acute": ("JARVIS_ENTROPY_ACUTE_THRESHOLD", 0.6),
            "entropy_chronic": ("JARVIS_ENTROPY_CHRONIC_THRESHOLD", 0.5),
            "staleness_minor": ("JARVIS_STALENESS_THRESHOLD_MINOR", 3.0),
            "perf_latency_factor": ("JARVIS_PERF_LATENCY_REGRESSION_FACTOR", 1.5),
            "perf_success_drop": ("JARVIS_PERF_SUCCESS_DROP_THRESHOLD", 0.15),
        }

        for name, (env_var, default) in _THRESHOLD_MAP.items():
            obs = self._observations.get(name, [])
            if len(obs) < 10:
                continue  # Insufficient data

            current = float(os.environ.get(env_var, str(default)))

            # Compute false positive and miss rates
            triggered_correct = sum(1 for t, c in obs if t and c)
            triggered_wrong = sum(1 for t, c in obs if t and not c)
            not_triggered_wrong = sum(1 for t, c in obs if not t and not c)
            total_triggered = triggered_correct + triggered_wrong
            total_not_triggered = len(obs) - total_triggered

            fp_rate = triggered_wrong / max(1, total_triggered)
            miss_rate = not_triggered_wrong / max(1, total_not_triggered)

            # Adjustment logic
            if fp_rate > 0.4 and total_triggered >= 5:
                # Too many false positives — raise threshold
                adjustment = current * 1.1
                recommendations.append(ThresholdRecommendation(
                    parameter=env_var,
                    current_value=current,
                    recommended_value=round(adjustment, 3),
                    reason=f"False positive rate {fp_rate:.0%} exceeds 40%. "
                           f"Raising threshold to reduce noise.",
                    false_positive_rate=fp_rate,
                    miss_rate=miss_rate,
                ))
            elif miss_rate > 0.3 and total_not_triggered >= 5:
                # Too many misses — lower threshold
                adjustment = current * 0.9
                recommendations.append(ThresholdRecommendation(
                    parameter=env_var,
                    current_value=current,
                    recommended_value=round(adjustment, 3),
                    reason=f"Miss rate {miss_rate:.0%} exceeds 30%. "
                           f"Lowering threshold to catch more issues.",
                    false_positive_rate=fp_rate,
                    miss_rate=miss_rate,
                ))

        return recommendations

    def auto_apply(self, min_sample_size: int = 20) -> List[ThresholdRecommendation]:
        """Auto-apply threshold recommendations with sufficient confidence.

        Only applies when the observation window is large enough
        (min_sample_size) to avoid premature tuning. Updates os.environ
        in-process — takes effect on the next pipeline run.

        Deterministic: statistical threshold → env var write. No inference.
        """
        _auto_enabled = os.environ.get(
            "JARVIS_THRESHOLD_AUTO_APPLY", "true"
        ).lower() in ("true", "1", "yes")
        if not _auto_enabled:
            return []

        applied = []
        for rec in self.compute_recommendations():
            # Only auto-apply with sufficient data
            obs = self._observations.get(
                rec.parameter.replace("JARVIS_", "").lower(), []
            )
            if len(obs) < min_sample_size:
                continue

            os.environ[rec.parameter] = str(rec.recommended_value)
            applied.append(rec)
            logger.info(
                "[ThresholdTuner] Auto-applied: %s = %s -> %s (%s)",
                rec.parameter, rec.current_value,
                rec.recommended_value, rec.reason[:60],
            )

        if applied:
            self._persist()
        return applied

    def format_recommendations(self) -> str:
        """Format recommendations for logging/display."""
        recs = self.compute_recommendations()
        if not recs:
            return "No threshold adjustments recommended."

        lines = ["## Threshold Tuning Recommendations"]
        for r in recs:
            lines.append(
                f"- **{r.parameter}**: {r.current_value} -> {r.recommended_value} "
                f"(FP: {r.false_positive_rate:.0%}, Miss: {r.miss_rate:.0%}) — {r.reason}"
            )
        return "\n".join(lines)

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "threshold_observations.json"
            data = {k: v for k, v in self._observations.items()}
            path.write_text(json.dumps(data))
        except Exception:
            logger.debug("[ThresholdTuner] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "threshold_observations.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for name, obs in data.items():
                self._observations[name] = [tuple(o) for o in obs]
        except Exception:
            logger.debug("[ThresholdTuner] Load failed", exc_info=True)


# ---------------------------------------------------------------------------
# 4. Provider Performance Tracker — model-selection learning (P2.3)
# ---------------------------------------------------------------------------

# Minimum observations before a provider recommendation is actionable.
_MIN_OBS_FOR_RECOMMENDATION = int(
    os.environ.get("JARVIS_PROVIDER_MIN_OBS", "3")
)
# Window size: only consider the last N observations per (provider, complexity).
_PROVIDER_WINDOW_SIZE = int(
    os.environ.get("JARVIS_PROVIDER_WINDOW_SIZE", "30")
)


@dataclass
class ProviderRecord:
    """A single observation of provider performance."""
    provider: str
    complexity: str       # "trivial" | "light" | "heavy_code" | "complex" | "moderate"
    success: bool
    generation_s: float   # How long generation took (0 if unknown)
    timestamp: float = field(default_factory=time.time)


class ProviderPerformanceTracker:
    """Tracks which provider succeeds at which operation complexity.

    Lightweight in-memory aggregator with JSON persistence.  Records
    (provider, complexity, success, duration) tuples and answers:
    "Given this complexity, which provider has the best success rate?"

    The CandidateGenerator queries this before Tier 1 routing to decide
    whether to prefer primary or fallback for a given complexity class.

    Boundary Principle (Manifesto §5):
      Deterministic: Statistical aggregation, no model inference.
      Agentic: How the routing decision affects the generation prompt.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        # (provider, complexity) → List[ProviderRecord], bounded by window
        self._records: Dict[Tuple[str, str], List[ProviderRecord]] = defaultdict(list)
        self._dirty: bool = False
        self._load()

    def record(
        self,
        provider: str,
        complexity: str,
        success: bool,
        generation_s: float = 0.0,
    ) -> None:
        """Record an observation. Called from orchestrator _publish_outcome."""
        key = (provider, complexity or "unknown")
        rec = ProviderRecord(
            provider=provider,
            complexity=complexity or "unknown",
            success=success,
            generation_s=generation_s,
        )
        bucket = self._records[key]
        bucket.append(rec)
        # Sliding window — keep only the most recent observations.
        if len(bucket) > _PROVIDER_WINDOW_SIZE:
            self._records[key] = bucket[-_PROVIDER_WINDOW_SIZE:]
        self._dirty = True

    def success_rate(self, provider: str, complexity: str) -> Tuple[float, int]:
        """Return (success_rate, sample_count) for a (provider, complexity) pair.

        Returns (0.0, 0) if no observations exist.
        """
        key = (provider, complexity or "unknown")
        bucket = self._records.get(key, [])
        if not bucket:
            return 0.0, 0
        wins = sum(1 for r in bucket if r.success)
        return wins / len(bucket), len(bucket)

    def recommend_provider(
        self,
        complexity: str,
        candidates: List[str],
    ) -> Optional[str]:
        """Recommend the best provider for a given complexity class.

        Returns the provider name with the highest success rate (among
        *candidates*) if it has enough observations and meaningfully
        outperforms the default ordering.  Returns None if no clear
        recommendation (too few observations or no significant difference).

        Parameters
        ----------
        complexity:
            Operation complexity class ("trivial", "moderate", "complex", etc.)
        candidates:
            Provider names to consider (in default priority order).
        """
        best_name: Optional[str] = None
        best_rate: float = -1.0
        best_count: int = 0

        for name in candidates:
            rate, count = self.success_rate(name, complexity)
            if count >= _MIN_OBS_FOR_RECOMMENDATION and rate > best_rate:
                best_rate = rate
                best_count = count
                best_name = name

        if best_name is None or best_count < _MIN_OBS_FOR_RECOMMENDATION:
            return None  # Not enough data to recommend

        # Only recommend if the best is meaningfully better than default
        # (first candidate in the list). If default is already best, no change.
        if best_name == candidates[0]:
            return None  # Default ordering already optimal

        default_rate, default_count = self.success_rate(candidates[0], complexity)
        if default_count >= _MIN_OBS_FOR_RECOMMENDATION:
            # Require >15% improvement over default to justify reordering
            if best_rate - default_rate < 0.15:
                return None

        logger.info(
            "[ProviderPerformance] Recommending '%s' for complexity=%s "
            "(%.0f%% over %d obs vs default '%.0f%%')",
            best_name, complexity, best_rate * 100, best_count,
            default_rate * 100 if default_count > 0 else 0,
        )
        return best_name

    def format_summary(self) -> str:
        """Format a human-readable summary of provider performance."""
        if not self._records:
            return "No provider performance data yet."

        # Aggregate by provider across all complexities
        by_provider: Dict[str, List[ProviderRecord]] = defaultdict(list)
        for records in self._records.values():
            for r in records:
                by_provider[r.provider].append(r)

        lines = ["## Provider Performance"]
        for provider, records in sorted(by_provider.items()):
            wins = sum(1 for r in records if r.success)
            total = len(records)
            rate = wins / total if total else 0
            avg_s = (
                sum(r.generation_s for r in records if r.generation_s > 0)
                / max(1, sum(1 for r in records if r.generation_s > 0))
            )
            lines.append(
                f"- **{provider}**: {rate:.0%} success ({wins}/{total}), "
                f"avg {avg_s:.1f}s"
            )

            # Per-complexity breakdown
            by_cx: Dict[str, List[ProviderRecord]] = defaultdict(list)
            for r in records:
                by_cx[r.complexity].append(r)
            for cx, cx_recs in sorted(by_cx.items()):
                cx_wins = sum(1 for r in cx_recs if r.success)
                lines.append(f"  - {cx}: {cx_wins}/{len(cx_recs)}")

        return "\n".join(lines)

    def persist(self) -> None:
        """Flush to disk if dirty. Called periodically, not on every record."""
        if not self._dirty:
            return
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "provider_performance.json"
            data: Dict[str, list] = {}
            for (prov, cx), records in self._records.items():
                key = f"{prov}:{cx}"
                data[key] = [
                    {
                        "provider": r.provider,
                        "complexity": r.complexity,
                        "success": r.success,
                        "generation_s": r.generation_s,
                        "timestamp": r.timestamp,
                    }
                    for r in records
                ]
            path.write_text(json.dumps(data, indent=2))
            self._dirty = False
        except Exception:
            logger.debug("[ProviderPerformance] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "provider_performance.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for compound_key, records_data in data.items():
                parts = compound_key.split(":", 1)
                prov = parts[0]
                cx = parts[1] if len(parts) > 1 else "unknown"
                key = (prov, cx)
                self._records[key] = [
                    ProviderRecord(
                        provider=r["provider"],
                        complexity=r["complexity"],
                        success=r["success"],
                        generation_s=r.get("generation_s", 0.0),
                        timestamp=r.get("timestamp", 0.0),
                    )
                    for r in records_data[-_PROVIDER_WINDOW_SIZE:]
                ]
            total = sum(len(v) for v in self._records.values())
            if total:
                logger.info(
                    "[ProviderPerformance] Loaded %d records across %d buckets",
                    total, len(self._records),
                )
        except Exception:
            logger.debug("[ProviderPerformance] Load failed", exc_info=True)
