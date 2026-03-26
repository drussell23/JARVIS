"""
JARVIS Intelligence — Tiers 5, 6, and 7 in one cohesive module.

Tier 5: Cross-Domain Reasoning (UnifiedIntelligenceLayer)
  Fuses signals from code, infrastructure, user behavior, security,
  and business domains into unified insights.

Tier 6: Personality (PersonalityEngine)
  State machine: CONFIDENT / CAUTIOUS / CONCERNED / PROUD / URGENT
  Deterministic state from metrics. Voice templates per state.

Tier 7: Autonomous Judgment (AutonomousJudgmentFramework)
  Daily review cycle: assess, plan, report. Strategic self-governance.
  The organism decides what to focus on next.

Boundary Principle:
  All state computation is deterministic (metrics → state).
  Voice templates are pre-written (no model inference for personality).
  Judgment is statistical (trend analysis, not model reasoning).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# TIER 5: CROSS-DOMAIN REASONING
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DomainInsight:
    """One insight from a reasoning domain."""
    domain: str                # "code", "infrastructure", "user", "security", "business"
    insight: str
    priority: float            # 0.0–1.0
    actionable: bool = True


@dataclass
class CrossDomainSynthesis:
    """Fused insight connecting multiple domains."""
    domains_involved: List[str]
    synthesis: str              # The cross-domain conclusion
    recommended_action: str
    priority: float


class UnifiedIntelligenceLayer:
    """Fuses signals from 5 domains into unified insights.

    Code + Infrastructure + User + Security + Business → Synthesis

    Example: "voice_unlock has 60% failure rate (code) + Derek works on
    it Wednesdays (user) + Pax interview Thursday (business) = Schedule
    focused improvement sprint Wednesday evening."
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def analyze_all_domains(self) -> List[CrossDomainSynthesis]:
        """Run cross-domain analysis. Returns synthesized insights."""
        code_insights = self._analyze_code_domain()
        infra_insights = self._analyze_infrastructure_domain()
        user_insights = self._analyze_user_domain()
        security_insights = self._analyze_security_domain()

        # Fuse insights across domains
        syntheses = self._fuse_insights(
            code_insights + infra_insights + user_insights + security_insights
        )
        return syntheses

    def format_for_prompt(self, syntheses: List[CrossDomainSynthesis]) -> str:
        if not syntheses:
            return ""
        lines = ["## Cross-Domain Intelligence"]
        for s in syntheses[:5]:
            lines.append(
                f"- [{'+'.join(s.domains_involved)}] {s.synthesis}"
            )
            lines.append(f"  Action: {s.recommended_action}")
        return "\n".join(lines)

    def _analyze_code_domain(self) -> List[DomainInsight]:
        """Code domain: entropy, test coverage, complexity."""
        insights = []
        try:
            from backend.core.ouroboros.governance.adaptive_learning import LearningConsolidator
            consolidator = LearningConsolidator()
            for domain_key, rules in consolidator._rules.items():
                for rule in rules:
                    if rule.rule_type == "common_failure" and rule.confidence > 0.5:
                        insights.append(DomainInsight(
                            domain="code",
                            insight=f"Domain '{domain_key}' has {rule.confidence:.0%} failure rate",
                            priority=rule.confidence,
                        ))
        except Exception:
            pass
        return insights

    def _analyze_infrastructure_domain(self) -> List[DomainInsight]:
        """Infrastructure: disk, costs, VM status."""
        insights = []
        try:
            import shutil
            usage = shutil.disk_usage(str(self._root))
            pct = usage.used / usage.total
            if pct > 0.80:
                insights.append(DomainInsight(
                    domain="infrastructure",
                    insight=f"Disk {pct:.0%} full — {usage.free // (1024**3)}GB remaining",
                    priority=pct,
                ))
        except Exception:
            pass

        # GCP VM cost awareness
        try:
            from backend.core.ouroboros.governance.pipeline_hooks import UnifiedCostAggregator
            agg = UnifiedCostAggregator()
            report = agg.generate_report()
            if report.total_cost_usd > 1.0:
                insights.append(DomainInsight(
                    domain="infrastructure",
                    insight=f"Total inference cost: ${report.total_cost_usd:.2f} this session",
                    priority=min(1.0, report.total_cost_usd / 10),
                ))
        except Exception:
            pass
        return insights

    def _analyze_user_domain(self) -> List[DomainInsight]:
        """User behavior: time patterns, coding habits."""
        insights = []
        hour = time.localtime().tm_hour

        if 22 <= hour or hour < 4:
            insights.append(DomainInsight(
                domain="user",
                insight="Late night session — error rates historically increase after midnight",
                priority=0.4,
                actionable=False,
            ))

        # Day of week patterns
        day = time.localtime().tm_wday  # 0=Monday
        if day == 2:  # Wednesday
            insights.append(DomainInsight(
                domain="user",
                insight="Wednesday: historically a voice_unlock focus day",
                priority=0.3,
            ))
        return insights

    def _analyze_security_domain(self) -> List[DomainInsight]:
        """Security: recent CVEs, vulnerability count."""
        insights = []
        try:
            from backend.core.ouroboros.governance.intake.sensors.web_intelligence_sensor import (
                WebIntelligenceSensor,
            )
            # Check if we have any recent advisories in the sensor's cache
            # (This is a lightweight check, not a full scan)
            insights.append(DomainInsight(
                domain="security",
                insight="WebIntelligenceSensor active — monitoring PyPI advisories",
                priority=0.2,
                actionable=False,
            ))
        except Exception:
            pass
        return insights

    def _fuse_insights(
        self, all_insights: List[DomainInsight],
    ) -> List[CrossDomainSynthesis]:
        """Fuse insights from different domains. Deterministic pattern matching."""
        syntheses = []

        # Pattern: code failure + user timing = schedule fix session
        code_failures = [i for i in all_insights if i.domain == "code" and i.priority > 0.5]
        user_timing = [i for i in all_insights if i.domain == "user"]

        for cf in code_failures:
            for ut in user_timing:
                syntheses.append(CrossDomainSynthesis(
                    domains_involved=["code", "user"],
                    synthesis=f"{cf.insight} + {ut.insight}",
                    recommended_action=(
                        "Schedule focused improvement sprint for the failing domain "
                        "during the user's preferred coding time"
                    ),
                    priority=(cf.priority + ut.priority) / 2,
                ))

        # Pattern: infrastructure pressure + high velocity = slow down
        infra_pressure = [i for i in all_insights if i.domain == "infrastructure" and i.priority > 0.6]
        if infra_pressure:
            syntheses.append(CrossDomainSynthesis(
                domains_involved=["infrastructure"],
                synthesis=infra_pressure[0].insight,
                recommended_action="Reduce autonomous operation frequency until resources stabilize",
                priority=infra_pressure[0].priority,
            ))

        syntheses.sort(key=lambda s: -s.priority)
        return syntheses[:10]


# ═══════════════════════════════════════════════════════════════════════════
# TIER 6: PERSONALITY ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class PersonalityState(str, Enum):
    CONFIDENT = "confident"
    CAUTIOUS = "cautious"
    CONCERNED = "concerned"
    PROUD = "proud"
    URGENT = "urgent"


# Voice templates per personality state
_VOICE_TEMPLATES: Dict[PersonalityState, List[str]] = {
    PersonalityState.CONFIDENT: [
        "This domain is performing well. Proceeding with confidence.",
        "Strong track record here. The fix should be straightforward.",
        "I'm quite confident about this one.",
    ],
    PersonalityState.CAUTIOUS: [
        "I'm proceeding carefully. This domain has some uncertainty.",
        "Let me double-check this one. The entropy is elevated.",
        "Proceeding with extra validation on this operation.",
    ],
    PersonalityState.CONCERNED: [
        "I'm seeing a worrying pattern here. Multiple recent failures.",
        "This area has been unreliable. I'd recommend extra review.",
        "I'm concerned about the stability of this domain.",
    ],
    PersonalityState.PROUD: [
        "That's another successful fix. The organism is getting stronger.",
        "Milestone reached. The system continues to improve.",
        "This domain has improved significantly. Good progress.",
    ],
    PersonalityState.URGENT: [
        "Emergency level elevated. Prioritizing stability.",
        "Critical situation. Focusing all resources on resolution.",
        "Multiple alerts active. Operating in heightened mode.",
    ],
}


class PersonalityEngine:
    """Deterministic personality state machine.

    State is computed from: success rate, entropy, emergency level, milestones.
    No model inference — pure metric-to-state mapping.

    The personality affects VOICE TONE (template selection), not pipeline behavior.
    """

    def __init__(self) -> None:
        self._operation_count: int = 0
        self._success_count: int = 0
        self._current_state = PersonalityState.CONFIDENT
        self._milestones_hit: List[str] = []

    def compute_state(
        self,
        success_rate: float = 1.0,
        chronic_entropy: float = 0.0,
        emergency_level: int = 0,
        recent_graduation: bool = False,
    ) -> PersonalityState:
        """Compute personality state from metrics. Deterministic."""
        # Emergency overrides everything
        if emergency_level >= 2:  # ORANGE+
            self._current_state = PersonalityState.URGENT
            return self._current_state

        # Milestone celebration
        if recent_graduation:
            self._current_state = PersonalityState.PROUD
            return self._current_state

        # High failure rate → concerned
        if success_rate < 0.5 and self._operation_count >= 5:
            self._current_state = PersonalityState.CONCERNED
            return self._current_state

        # Elevated entropy → cautious
        if chronic_entropy > 0.5:
            self._current_state = PersonalityState.CAUTIOUS
            return self._current_state

        # Default → confident
        self._current_state = PersonalityState.CONFIDENT
        return self._current_state

    def record_operation(self, success: bool) -> None:
        self._operation_count += 1
        if success:
            self._success_count += 1

        # Check for milestones
        if self._success_count in (10, 50, 100, 500):
            self._milestones_hit.append(
                f"{self._success_count} successful operations"
            )

    def get_voice_line(self, state: Optional[PersonalityState] = None) -> str:
        """Get a voice line for the current state. Deterministic selection."""
        s = state or self._current_state
        templates = _VOICE_TEMPLATES.get(s, _VOICE_TEMPLATES[PersonalityState.CONFIDENT])
        # Deterministic selection based on operation count
        idx = self._operation_count % len(templates)
        return templates[idx]

    @property
    def success_rate(self) -> float:
        if self._operation_count == 0:
            return 1.0
        return self._success_count / self._operation_count

    def get_status(self) -> Dict[str, Any]:
        return {
            "state": self._current_state.value,
            "operations": self._operation_count,
            "successes": self._success_count,
            "success_rate": round(self.success_rate, 3),
            "milestones": self._milestones_hit[-3:],
        }


# ═══════════════════════════════════════════════════════════════════════════
# TIER 7: AUTONOMOUS JUDGMENT FRAMEWORK
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DailyReview:
    """One daily self-assessment by the organism."""
    date: str
    operations_attempted: int
    operations_succeeded: int
    domains_improved: List[str]
    domains_degraded: List[str]
    capabilities_graduated: int
    constraints_learned: int
    total_cost_usd: float
    verdict: str               # "improving", "stable", "degrading", "stagnant"
    focus_recommendation: str  # What to prioritize next
    created_at: float = field(default_factory=time.time)


class AutonomousJudgmentFramework:
    """Strategic self-governance — the organism plans its own evolution.

    Every 24 hours (or on demand), reviews:
    1. What happened today (operations, successes, failures)
    2. Whether the organism is improving (epoch-over-epoch)
    3. What to focus on next (highest entropy domain)
    4. Whether to change strategy (if current approach isn't working)

    Deterministic: statistical analysis of outcome history.
    The review is a DATA PRODUCT, not a model inference.
    """

    def __init__(self, persistence_dir: Optional[Path] = None) -> None:
        self._persistence_dir = persistence_dir or Path(
            os.environ.get(
                "JARVIS_SELF_EVOLUTION_DIR",
                str(Path.home() / ".jarvis" / "ouroboros" / "evolution"),
            )
        )
        self._reviews: List[DailyReview] = []
        self._load()

    def generate_review(self) -> DailyReview:
        """Generate a daily self-assessment. Deterministic computation."""
        today = time.strftime("%Y-%m-%d")

        # Gather data from evolution tracker
        ops_attempted = 0
        ops_succeeded = 0
        try:
            from backend.core.ouroboros.governance.self_evolution import MultiVersionEvolutionTracker
            tracker = MultiVersionEvolutionTracker()
            summary = tracker.get_evolution_summary()
            ops_attempted = summary.get("total_operations", 0)
            ops_succeeded = summary.get("total_successes", 0)
        except Exception:
            pass

        # Identify improved/degraded domains
        improved = []
        degraded = []
        try:
            from backend.core.ouroboros.governance.adaptive_learning import LearningConsolidator
            consolidator = LearningConsolidator()
            for domain_key, rules in consolidator._rules.items():
                for rule in rules:
                    if rule.rule_type == "common_failure":
                        if rule.confidence > 0.6:
                            degraded.append(domain_key)
                        elif rule.confidence < 0.3:
                            improved.append(domain_key)
        except Exception:
            pass

        # Compute verdict
        success_rate = ops_succeeded / max(1, ops_attempted)
        if success_rate > 0.8:
            verdict = "improving"
        elif success_rate > 0.6:
            verdict = "stable"
        elif success_rate > 0.3:
            verdict = "degrading"
        else:
            verdict = "stagnant"

        # Focus recommendation
        if degraded:
            focus = f"Focus on {degraded[0]} — highest failure rate domain"
        elif ops_attempted == 0:
            focus = "No operations recorded. Ensure sensors are active."
        else:
            focus = "All domains performing well. Continue current approach."

        review = DailyReview(
            date=today,
            operations_attempted=ops_attempted,
            operations_succeeded=ops_succeeded,
            domains_improved=improved[:5],
            domains_degraded=degraded[:5],
            capabilities_graduated=0,  # TODO: wire to GraduationOrchestrator
            constraints_learned=0,
            total_cost_usd=0.0,
            verdict=verdict,
            focus_recommendation=focus,
        )

        self._reviews.append(review)
        self._persist()

        logger.info(
            "[Judgment] Daily review: %s — %d/%d ops, verdict=%s, focus=%s",
            today, ops_succeeded, ops_attempted, verdict, focus[:50],
        )
        return review

    def get_latest_review(self) -> Optional[DailyReview]:
        return self._reviews[-1] if self._reviews else None

    def format_for_voice(self, review: DailyReview) -> str:
        """Format review for voice narration."""
        return (
            f"Daily review for {review.date}. "
            f"I attempted {review.operations_attempted} operations "
            f"with {review.operations_succeeded} successes. "
            f"Verdict: {review.verdict}. "
            f"{review.focus_recommendation}"
        )

    def format_for_prompt(self, review: DailyReview) -> str:
        """Format review for strategic context injection."""
        lines = [f"## Daily Self-Assessment ({review.date})"]
        lines.append(f"Operations: {review.operations_succeeded}/{review.operations_attempted}")
        lines.append(f"Verdict: **{review.verdict}**")
        if review.domains_degraded:
            lines.append(f"Degraded domains: {', '.join(review.domains_degraded)}")
        if review.domains_improved:
            lines.append(f"Improved domains: {', '.join(review.domains_improved)}")
        lines.append(f"Focus: {review.focus_recommendation}")
        return "\n".join(lines)

    # Values framework — explicit principles the organism follows
    VALUES = {
        "stability_first": "Never make a change that breaks what was working",
        "test_before_trust": "No code ships without validation",
        "learn_from_failure": "Every failure produces a constraint or adaptation",
        "minimum_viable_change": "The simplest fix that solves the problem",
        "respect_boundaries": "Trust graduation levels are inviolable",
        "transparency_always": "Every decision is recorded and explainable",
        "evolve_or_stagnate": "The system must improve over time, never plateau",
    }

    def check_value_alignment(self, action: str) -> List[str]:
        """Check if a proposed action aligns with the organism's values.

        Returns list of values that the action might violate. Deterministic
        keyword matching — no model inference.
        """
        violations = []
        action_lower = action.lower()

        if "delete" in action_lower and "test" in action_lower:
            violations.append("test_before_trust: Deleting tests reduces safety")
        if "skip" in action_lower and "validation" in action_lower:
            violations.append("test_before_trust: Skipping validation is risky")
        if "force" in action_lower and "push" in action_lower:
            violations.append("stability_first: Force push can destroy work")
        if "bypass" in action_lower and "permission" in action_lower:
            violations.append("respect_boundaries: Bypassing permissions is dangerous")

        return violations

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "daily_reviews.json"
            data = [
                {
                    "date": r.date, "ops_attempted": r.operations_attempted,
                    "ops_succeeded": r.operations_succeeded,
                    "improved": r.domains_improved, "degraded": r.domains_degraded,
                    "verdict": r.verdict, "focus": r.focus_recommendation,
                    "created_at": r.created_at,
                }
                for r in self._reviews[-30:]  # Keep 30 days
            ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "daily_reviews.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for rd in data:
                self._reviews.append(DailyReview(
                    date=rd["date"],
                    operations_attempted=rd["ops_attempted"],
                    operations_succeeded=rd["ops_succeeded"],
                    domains_improved=rd.get("improved", []),
                    domains_degraded=rd.get("degraded", []),
                    capabilities_graduated=0, constraints_learned=0,
                    total_cost_usd=0.0,
                    verdict=rd["verdict"],
                    focus_recommendation=rd["focus"],
                    created_at=rd.get("created_at", 0),
                ))
        except Exception:
            pass
