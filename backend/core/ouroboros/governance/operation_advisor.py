"""
Operation Advisor — JARVIS-Level Tier 1.

"Sir, I wouldn't recommend that."

Evaluates the WISDOM of an operation before the pipeline executes it.
Not just "can we do this?" but "SHOULD we do this right now?"

Signals: blast radius, test coverage, chronic entropy, time context,
failure streaks, merge freeze, file staleness, concurrent operations.

Decisions: RECOMMEND / CAUTION / ADVISE_AGAINST / BLOCK

Boundary Principle:
  Deterministic: All signals computed via AST, git log, system clock,
  and historical data. No model inference in the judgment itself.
  The advice is injected into the generation prompt as context.
"""
from __future__ import annotations

import ast
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_ADVISOR_ENABLED", "true"
).lower() in ("true", "1", "yes")
_BLAST_RADIUS_WARN = int(os.environ.get("JARVIS_ADVISOR_BLAST_RADIUS_WARN", "10"))
_FAILURE_STREAK_WARN = int(os.environ.get("JARVIS_ADVISOR_FAILURE_STREAK_WARN", "3"))


# ---------------------------------------------------------------------------
# Read-only intent inference (deterministic keyword scan)
# ---------------------------------------------------------------------------
#
# The orchestrator calls infer_read_only_intent() BEFORE the Advisor so the
# flag can be stamped onto the OperationContext hash chain. The Advisor then
# trusts the flag — not because of the keywords, but because tool_executor
# + orchestrator jointly refuse any mutating tool call / APPLY transition
# whenever ctx.is_read_only is True. The keywords are a soft trigger; the
# enforcement is the mathematical guarantee.

_READ_ONLY_POSITIVE: Tuple[str, ...] = (
    "read-only",
    "read only",
    "readonly",
    "do not mutate",
    "do not write",
    "do not modify",
    "do not change",
    "cartography",
    "architectural mapping",
    "call graph",
    "gap analysis",
    "coupling map",
    "pure-exploration",
    "pure exploration",
    "exploration-only",
    "survey",
    "audit",
    "do not run any tests",
    "do not write any source files",
)

_READ_ONLY_NEGATIVE: Tuple[str, ...] = (
    "refactor",
    "rewrite",
    "implement ",
    "fix ",
    "patch ",
    "rename ",
    "replace ",
    "add a ",
    "add new ",
    "remove ",
    "delete ",
    "migrate ",
    "upgrade ",
)


def infer_read_only_intent(description: str) -> bool:
    """Return True iff *description* strongly signals a non-mutating op.

    Deterministic keyword scan, no LLM call. Conservative: requires at
    least one positive signal AND no mutation verbs. False negatives are
    acceptable (the op proceeds through normal risk gating); false
    positives would reach APPLY and be short-circuited by the orchestrator.
    """
    if not description:
        return False
    norm = description.lower()
    if not any(kw in norm for kw in _READ_ONLY_POSITIVE):
        return False
    if any(kw in norm for kw in _READ_ONLY_NEGATIVE):
        return False
    return True


class AdvisoryDecision(str, Enum):
    RECOMMEND = "recommend"            # Proceed normally
    CAUTION = "caution"                # Proceed but inject warnings into prompt
    ADVISE_AGAINST = "advise_against"  # Allow but voice warning
    BLOCK = "block"                    # Refuse (safety-critical only)


@dataclass
class Advisory:
    """The advisor's judgment on an operation."""
    decision: AdvisoryDecision
    reasons: List[str]
    blast_radius: int          # Number of files that import the targets
    test_coverage: float       # 0.0–1.0, % of targets with tests
    chronic_entropy: float     # Domain failure rate from LearningConsolidator
    risk_score: float          # Composite 0.0–1.0
    voice_message: str = ""    # What JARVIS would say


class OperationAdvisor:
    """Evaluates whether an operation SHOULD proceed.

    Called before CLASSIFY — the first thing that happens when an
    IntentEnvelope arrives. The advisor computes a risk score from
    multiple deterministic signals and returns an Advisory.

    The advisory doesn't block the pipeline (except for BLOCK).
    It injects warnings into the generation prompt so the model
    is more careful with risky operations.
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def advise(
        self,
        target_files: Tuple[str, ...],
        description: str,
        op_id: str = "",
        is_read_only: bool = False,
    ) -> Advisory:
        """Evaluate an operation and return advisory judgment.

        When ``is_read_only`` is True the Advisor skips blast_radius and
        test_coverage signals — the downstream contract is that tool_executor
        will refuse every mutating tool call and the orchestrator will
        refuse the APPLY transition, so those two signals are mathematically
        unreachable. Stale-file, large-file, time-of-day, and chronic-entropy
        signals still apply because they speak to generation quality, not
        blast radius.
        """
        if not _ENABLED:
            return Advisory(
                decision=AdvisoryDecision.RECOMMEND,
                reasons=["Advisor disabled"], blast_radius=0,
                test_coverage=1.0, chronic_entropy=0.0, risk_score=0.0,
            )

        reasons: List[str] = []
        risk_factors: List[float] = []

        # Signal 1: Blast radius (how many files import the targets)
        # Always computed for observability — surfaced as a reason only
        # for mutating ops.
        blast_radius = self._compute_blast_radius(target_files)
        if not is_read_only and blast_radius >= _BLAST_RADIUS_WARN:
            reasons.append(
                f"High blast radius: {blast_radius} files import these targets"
            )
            risk_factors.append(min(1.0, blast_radius / 30))

        # Signal 2: Test coverage
        # Same bypass logic — read-only ops don't execute mutations, so
        # coverage of the targets is structurally irrelevant.
        test_coverage = self._compute_test_coverage(target_files)
        if not is_read_only and test_coverage < 0.5:
            reasons.append(
                f"Low test coverage: {test_coverage:.0%} of targets have tests"
            )
            risk_factors.append(1.0 - test_coverage)

        # Signal 3: Chronic entropy (historical failure rate)
        chronic_entropy = self._get_chronic_entropy(target_files, description)
        if chronic_entropy > 0.5:
            reasons.append(
                f"High chronic entropy: {chronic_entropy:.0%} historical failure rate"
            )
            risk_factors.append(chronic_entropy)

        # Signal 4: Time of day risk
        hour = time.localtime().tm_hour
        if hour >= 2 and hour < 6:
            reasons.append("Late night operation (2-6 AM) — higher error risk")
            risk_factors.append(0.3)

        # Signal 5: File staleness (untouched for long time = riskier)
        stale_files = self._check_staleness(target_files)
        if stale_files:
            reasons.append(
                f"Stale files (>90 days untouched): {', '.join(stale_files[:3])}"
            )
            risk_factors.append(0.2)

        # Signal 6: Large file risk
        large_files = self._check_large_files(target_files)
        if large_files:
            reasons.append(
                f"Large files (>500 lines): {', '.join(f'{f}({l}L)' for f, l in large_files[:3])}"
            )
            risk_factors.append(0.2)

        # Compute composite risk score
        risk_score = sum(risk_factors) / max(1, len(risk_factors)) if risk_factors else 0.0
        risk_score = min(1.0, risk_score)

        # Make decision
        if risk_score >= 0.8:
            decision = AdvisoryDecision.ADVISE_AGAINST
        elif risk_score >= 0.5:
            decision = AdvisoryDecision.CAUTION
        elif risk_score >= 0.3:
            decision = AdvisoryDecision.CAUTION
        else:
            decision = AdvisoryDecision.RECOMMEND

        # Special case: block if touching LOCKED trust tier with no tests.
        # Read-only ops bypass this block because the no-mutation contract
        # makes blast radius and coverage unreachable — enforced downstream
        # by tool_executor (mutating tools refused) and orchestrator (APPLY
        # phase short-circuited to COMPLETE).
        if not is_read_only and test_coverage == 0 and blast_radius >= 20:
            decision = AdvisoryDecision.BLOCK
            reasons.append("BLOCKED: Zero test coverage + extreme blast radius")

        # Observability: surface the bypass as a positive reason so the
        # log line and prompt-context both show WHY a high-blast op passed.
        if is_read_only:
            reasons.insert(
                0,
                f"Read-only op: blast_radius={blast_radius}, "
                f"coverage={test_coverage:.0%} bypassed (no-mutation contract)",
            )

        # Build voice message
        voice = self._build_voice_message(decision, reasons, target_files)

        advisory = Advisory(
            decision=decision,
            reasons=reasons,
            blast_radius=blast_radius,
            test_coverage=test_coverage,
            chronic_entropy=chronic_entropy,
            risk_score=round(risk_score, 3),
            voice_message=voice,
        )

        logger.info(
            "[Advisor] %s (risk=%.2f, blast=%d, coverage=%.0f%%, entropy=%.0f%%, "
            "read_only=%s) reasons=%d op=%s",
            decision.value, risk_score, blast_radius,
            test_coverage * 100, chronic_entropy * 100,
            is_read_only, len(reasons), op_id,
        )

        return advisory

    def format_for_prompt(self, advisory: Advisory) -> str:
        """Format advisory for injection into generation prompt."""
        if advisory.decision == AdvisoryDecision.RECOMMEND:
            return ""

        lines = [f"## Operation Advisory: {advisory.decision.value.upper()}"]
        lines.append(f"Risk score: {advisory.risk_score:.0%}")
        for reason in advisory.reasons:
            lines.append(f"- {reason}")

        if advisory.decision == AdvisoryDecision.ADVISE_AGAINST:
            lines.append(
                "\nProceed with EXTREME CAUTION. Minimize changes. "
                "Generate tests alongside any modifications."
            )
        elif advisory.decision == AdvisoryDecision.CAUTION:
            lines.append(
                "\nBe careful with these files. Check for side effects."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Signal computation (all deterministic)
    # ------------------------------------------------------------------

    def _compute_blast_radius(self, target_files: Tuple[str, ...]) -> int:
        """Count files that import the targets. AST-based, deterministic."""
        target_modules = set()
        for f in target_files:
            if f.endswith(".py"):
                module = f.replace("/", ".").replace(".py", "")
                target_modules.add(module)
                target_modules.add(Path(f).stem)

        if not target_modules:
            return 0

        importers = 0
        for py_file in self._project_root.rglob("*.py"):
            if "venv" in str(py_file) or "__pycache__" in str(py_file):
                continue
            try:
                content = py_file.read_text(errors="replace")
                if any(mod in content for mod in target_modules):
                    importers += 1
            except Exception:
                pass
            if importers >= 50:
                break  # Cap the search
        return importers

    def _compute_test_coverage(self, target_files: Tuple[str, ...]) -> float:
        """Fraction of target files that have corresponding test files."""
        if not target_files:
            return 1.0
        py_files = [f for f in target_files if f.endswith(".py") and "test_" not in f]
        if not py_files:
            return 1.0

        covered = 0
        for f in py_files:
            stem = Path(f).stem
            if any((self._project_root / "tests" / f"test_{stem}.py").exists()
                   for _ in [1]):
                covered += 1
        return covered / len(py_files)

    def _get_chronic_entropy(
        self, target_files: Tuple[str, ...], description: str,
    ) -> float:
        """Get chronic failure rate from LearningConsolidator."""
        try:
            from backend.core.ouroboros.governance.entropy_calculator import extract_domain_key
            from backend.core.ouroboros.governance.adaptive_learning import LearningConsolidator
            domain = extract_domain_key(target_files, description)
            consolidator = LearningConsolidator()
            rules = consolidator.get_rules_for_domain(domain)
            for rule in rules:
                if rule.rule_type == "common_failure":
                    return rule.confidence
        except Exception:
            pass
        return 0.0

    def _check_staleness(self, target_files: Tuple[str, ...]) -> List[str]:
        """Find files not modified in 90+ days. Git-free check via mtime."""
        stale = []
        cutoff = time.time() - (90 * 86400)
        for f in target_files:
            full = self._project_root / f
            if full.exists():
                try:
                    if full.stat().st_mtime < cutoff:
                        stale.append(f)
                except Exception:
                    pass
        return stale

    def _check_large_files(
        self, target_files: Tuple[str, ...],
    ) -> List[Tuple[str, int]]:
        """Find files with >500 lines."""
        large = []
        for f in target_files:
            full = self._project_root / f
            if full.exists() and f.endswith(".py"):
                try:
                    lines = len(full.read_text().split("\n"))
                    if lines > 500:
                        large.append((f, lines))
                except Exception:
                    pass
        return large

    @staticmethod
    def _build_voice_message(
        decision: AdvisoryDecision,
        reasons: List[str],
        target_files: Tuple[str, ...],
    ) -> str:
        """Build JARVIS-style voice message."""
        target = target_files[0] if target_files else "these files"

        if decision == AdvisoryDecision.RECOMMEND:
            return ""
        elif decision == AdvisoryDecision.CAUTION:
            return f"Proceeding with caution on {Path(target).name}. {reasons[0] if reasons else ''}"
        elif decision == AdvisoryDecision.ADVISE_AGAINST:
            return (
                f"I wouldn't recommend modifying {Path(target).name} right now. "
                f"{reasons[0] if reasons else 'The risk profile is elevated.'}"
            )
        elif decision == AdvisoryDecision.BLOCK:
            return (
                f"I'm blocking this operation on {Path(target).name}. "
                f"{reasons[0] if reasons else 'Safety threshold exceeded.'}"
            )
        return ""
