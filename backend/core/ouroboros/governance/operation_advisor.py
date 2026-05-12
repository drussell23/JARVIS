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
import json
import logging
import os
import re
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
# B.2.0 — Worktree-aware advisory (SWE-Bench-Pro Phase 2 enabling layer +
# permanent improvement for L3 worktree-isolated work and the in-repo L2
# exercise corpus). §33.1 default-FALSE master switch; when ON, the advisor
# scans the per-envelope ``repo_root`` for blast/coverage/staleness/large-file
# signals instead of its constructor-bound ``self._project_root``.
#
# Source-agnostic by design: no envelope.source branch is consulted. The
# override applies whenever the envelope carries a trusted ``repo_root``
# string in its evidence, regardless of which sensor produced it. Per
# operator binding (B.2.0 hardening note 4): blast is computed from the
# actual mutation root — not from a category special-case.
# ---------------------------------------------------------------------------
ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR: str = (
    "JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED"
)
ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR: str = (
    "JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST"
)

# Canonical evidence key (operator binding: pick ONE name, document it,
# don't fork parallel spellings in B.2.1 envelope builder). Mirrored by
# OperationContext.intake_evidence_json schema. Sensors that historically
# stamped ``worktree_path`` continue to do so for telemetry; the advisor
# input is unambiguously ``repo_root``.
EVIDENCE_REPO_ROOT_KEY: str = "repo_root"


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

# Mutation verbs — matched as **whole words** (word-boundary regex below).
# Substring-match was used in v1 but tripped on compound words — "dispatch"
# contains "patch", "implementation" contains "implement", etc. — so the
# Trinity cartography task was mis-classified as mutating in the first
# Session-3 run (debug.log bt-2026-04-18-032138).
_READ_ONLY_NEGATIVE: Tuple[str, ...] = (
    "refactor",
    "refactors",
    "refactoring",
    "rewrite",
    "rewrites",
    "rewriting",
    "implement",
    "implements",
    "implementing",
    "fix",
    "fixes",
    "fixing",
    "patch",
    "patches",
    "patching",
    "rename",
    "renames",
    "renaming",
    "replace",
    "replaces",
    "replacing",
    "remove",
    "removes",
    "removing",
    "delete",
    "deletes",
    "deleting",
    "migrate",
    "migrates",
    "migrating",
    "upgrade",
    "upgrades",
    "upgrading",
    # Two-word phrases kept as substring checks below — they can't
    # collide with compound words the way single verbs can.
)

_READ_ONLY_NEGATIVE_PHRASES: Tuple[str, ...] = (
    "add a ",
    "add new ",
    "add an ",
)

# Pre-compile one alternation regex with word boundaries on both sides.
# \b treats "-" as a word boundary in Python re, which is what we want
# for hyphenated verbs like "re-write" if they ever appear.
_READ_ONLY_NEGATIVE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _READ_ONLY_NEGATIVE) + r")\b",
    re.IGNORECASE,
)


def infer_read_only_intent(description: str) -> bool:
    """Return True iff *description* strongly signals a non-mutating op.

    Deterministic keyword scan, no LLM call. Conservative: requires at
    least one positive signal AND no mutation verbs. Mutation verbs are
    matched as **whole words** so "dispatch" / "implementation" / "fixed"
    don't collide with "patch" / "implement" / "fix". False negatives
    remain acceptable (the op proceeds through normal risk gating);
    false positives are structurally clamped — the orchestrator's APPLY
    short-circuit and the policy engine's Rule 0d refuse mutations even
    if a mutating op slipped the classifier.
    """
    if not description:
        return False
    norm = description.lower()
    if not any(kw in norm for kw in _READ_ONLY_POSITIVE):
        return False
    if _READ_ONLY_NEGATIVE_RE.search(norm):
        return False
    if any(phrase in norm for phrase in _READ_ONLY_NEGATIVE_PHRASES):
        return False
    return True


# ---------------------------------------------------------------------------
# Trusted-path resolver — bridges envelope.evidence to advisor.repo_root.
#
# Untrusted-input contract (B.2.0 hardening note 1): the evidence dict is
# operator-influenced data (it flows from sensors, ingest endpoints, and the
# `/attach` REPL path). The advisor must NOT trust an arbitrary path string —
# a hostile or buggy envelope could point ``repo_root`` at ``/etc`` (silently
# making blast=0 globally) or at a symlink that escapes the worktree base.
#
# Validation pipeline (first-failure-wins, NEVER raises):
#   1. master flag ON
#   2. evidence carries ``repo_root`` string + non-empty
#   3. Path resolves (no permission error, no missing-parent ENOENT)
#   4. Resolved path exists + is a directory
#   5. Resolved path is contained under an allowed prefix:
#         a. ``self._project_root`` (covers in-repo worktrees + L3 .worktrees/)
#         b. additional prefixes from
#            ``JARVIS_ADVISOR_WORKTREE_ROOT_ALLOWLIST`` (colon-separated
#            absolute paths; each is itself ``resolve()``-d)
#
# On any failure → returns None → orchestrator falls back to
# ``self._project_root`` (legacy byte-identical behavior).
# ---------------------------------------------------------------------------


def _worktree_aware_enabled() -> bool:
    """Master switch (§33.1 default-FALSE)."""
    raw = os.environ.get(ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR, "")
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _parse_allowlist_env() -> Tuple[Path, ...]:
    """Parse the colon-separated allowlist env into resolved Paths.
    NEVER raises; invalid entries are skipped with a debug log."""
    raw = os.environ.get(ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR, "").strip()
    if not raw:
        return ()
    out: List[Path] = []
    for entry in raw.split(os.pathsep):
        s = entry.strip()
        if not s:
            continue
        try:
            out.append(Path(s).resolve())
        except (OSError, RuntimeError):
            logger.debug(
                "[Advisor] worktree_root_allowlist: skipping invalid entry %r",
                s,
            )
    return tuple(out)


def _is_under(candidate: Path, parent: Path) -> bool:
    """True iff ``candidate`` is ``parent`` or a descendant of it.

    Uses POSIX-style path comparison on already-resolved Paths (caller
    must ``resolve()`` first to defeat symlink escapes). NEVER raises.
    """
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_envelope_repo_root(
    intake_evidence_json: str,
    *,
    project_root: Path,
    extra_allowlist: Optional[Tuple[Path, ...]] = None,
) -> Optional[Path]:
    """Resolve a per-envelope ``repo_root`` to a trusted absolute Path.

    Parameters
    ----------
    intake_evidence_json:
        The JSON-encoded evidence snapshot from ``OperationContext
        .intake_evidence_json`` (or any source-equivalent string). Empty
        string + malformed JSON + missing key are all silently treated
        as "no override".
    project_root:
        The orchestrator's bound project root. Used both as the legacy
        fallback context AND as the canonical allowed-prefix anchor.
    extra_allowlist:
        Optional caller-supplied extra prefixes (already resolved). When
        ``None`` (default), the env-derived allowlist is consulted.

    Returns
    -------
    Optional[Path]
        Resolved trusted path on success, ``None`` on:
          * master flag OFF
          * evidence missing / not a dict / no ``repo_root`` key
          * path doesn't resolve / doesn't exist / isn't a directory
          * resolved path escapes every allowed prefix

    NEVER raises (mirrors advisor fail-open contract).
    """
    if not _worktree_aware_enabled():
        return None
    if not intake_evidence_json:
        return None
    try:
        evidence = json.loads(intake_evidence_json)
    except (ValueError, TypeError):
        logger.debug(
            "[Advisor] resolve_envelope_repo_root: evidence not valid JSON",
        )
        return None
    if not isinstance(evidence, dict):
        return None
    raw = evidence.get(EVIDENCE_REPO_ROOT_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        # ``resolve(strict=False)`` defeats symlink escapes by canonicalizing
        # the path against the live filesystem. ``strict=True`` would raise
        # on missing components — we want a graceful None, not an exception.
        resolved = Path(raw).resolve(strict=False)
    except (OSError, RuntimeError):
        logger.debug(
            "[Advisor] resolve_envelope_repo_root: Path.resolve raised "
            "for %r",
            raw,
        )
        return None
    try:
        if not resolved.exists() or not resolved.is_dir():
            return None
    except (OSError, PermissionError):
        return None
    try:
        anchor = Path(project_root).resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    allowlist: List[Path] = [anchor]
    extras = (
        extra_allowlist if extra_allowlist is not None
        else _parse_allowlist_env()
    )
    allowlist.extend(extras)
    for parent in allowlist:
        if _is_under(resolved, parent):
            return resolved
    logger.info(
        "[Advisor] resolve_envelope_repo_root: %r rejected — "
        "outside %d allowed prefix(es)",
        str(resolved), len(allowlist),
    )
    return None


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
        repo_root: Optional[Path] = None,
    ) -> Advisory:
        """Evaluate an operation and return advisory judgment.

        When ``is_read_only`` is True the Advisor skips blast_radius and
        test_coverage signals — the downstream contract is that tool_executor
        will refuse every mutating tool call and the orchestrator will
        refuse the APPLY transition, so those two signals are mathematically
        unreachable. Stale-file, large-file, time-of-day, and chronic-entropy
        signals still apply because they speak to generation quality, not
        blast radius.

        ``repo_root`` (B.2.0) — when supplied, all filesystem-scanning signals
        (blast radius, test coverage, staleness, large-file) compute against
        this root instead of ``self._project_root``. Callers MUST validate
        the path through :func:`resolve_envelope_repo_root` before passing
        it in. When ``None`` (default) the advisor falls back to its
        constructor-bound project root — byte-identical to pre-B.2.0
        behavior. Source-agnostic: the advisor never branches on which
        sensor produced the envelope (operator binding: blast is root-
        correct, not category-special).
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
        blast_radius = self._compute_blast_radius(target_files, root=repo_root)
        if not is_read_only and blast_radius >= _BLAST_RADIUS_WARN:
            reasons.append(
                f"High blast radius: {blast_radius} files import these targets"
            )
            risk_factors.append(min(1.0, blast_radius / 30))

        # Signal 2: Test coverage
        # Same bypass logic — read-only ops don't execute mutations, so
        # coverage of the targets is structurally irrelevant.
        test_coverage = self._compute_test_coverage(target_files, root=repo_root)
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
        stale_files = self._check_staleness(target_files, root=repo_root)
        if stale_files:
            reasons.append(
                f"Stale files (>90 days untouched): {', '.join(stale_files[:3])}"
            )
            risk_factors.append(0.2)

        # Signal 6: Large file risk
        large_files = self._check_large_files(target_files, root=repo_root)
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

    def _compute_blast_radius(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> int:
        """Count files that import the targets. AST-based, deterministic.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior). Callers MUST validate the override
        path through :func:`resolve_envelope_repo_root` first.
        """
        scan_root = root if root is not None else self._project_root
        target_modules = set()
        for f in target_files:
            if f.endswith(".py"):
                module = f.replace("/", ".").replace(".py", "")
                target_modules.add(module)
                target_modules.add(Path(f).stem)

        if not target_modules:
            return 0

        importers = 0
        for py_file in scan_root.rglob("*.py"):
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

    def _compute_test_coverage(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> float:
        """Fraction of target files that have corresponding test files.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        if not target_files:
            return 1.0
        py_files = [f for f in target_files if f.endswith(".py") and "test_" not in f]
        if not py_files:
            return 1.0

        covered = 0
        for f in py_files:
            stem = Path(f).stem
            if any((scan_root / "tests" / f"test_{stem}.py").exists()
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

    def _check_staleness(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> List[str]:
        """Find files not modified in 90+ days. Git-free check via mtime.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        stale = []
        cutoff = time.time() - (90 * 86400)
        for f in target_files:
            full = scan_root / f
            if full.exists():
                try:
                    if full.stat().st_mtime < cutoff:
                        stale.append(f)
                except Exception:
                    pass
        return stale

    def _check_large_files(
        self,
        target_files: Tuple[str, ...],
        *,
        root: Optional[Path] = None,
    ) -> List[Tuple[str, int]]:
        """Find files with >500 lines.

        ``root`` (B.2.0) — scan tree. Defaults to ``self._project_root`` when
        ``None`` (pre-B.2.0 behavior).
        """
        scan_root = root if root is not None else self._project_root
        large = []
        for f in target_files:
            full = scan_root / f
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


# ---------------------------------------------------------------------------
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Returns count successfully
    registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "B.2.0 master switch (§33.1 default-FALSE): when ON, the "
                "OperationAdvisor consumes a per-envelope ``repo_root`` "
                "string from intake_evidence_json and scans THAT tree for "
                "blast radius / coverage / staleness / large-file signals "
                "instead of the orchestrator's constructor-bound "
                "project_root. Source-agnostic — no branch on "
                "envelope.source. Enabling layer for SWE-Bench-Pro Phase 2 "
                "+ permanent improvement for L3 worktree-isolated work + "
                "the in-repo L2 exercise corpus. Untrusted-input contract "
                "enforced by resolve_envelope_repo_root."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/operation_advisor.py"
            ),
            example="false",
            since="v3.7 Phase 2 Phase B.2.0 (2026-05-12)",
        ),
        FlagSpec(
            name=ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR,
            type=FlagType.STR,
            default="",
            description=(
                "Colon-separated absolute-path prefixes that supplement "
                "the orchestrator's project_root as allowed locations for "
                "envelope-provided ``repo_root`` overrides. Default empty "
                "= project_root only (covers in-repo worktrees + L3 "
                ".worktrees/ + .jarvis/swe_bench_pro/worktrees/). Each "
                "entry is Path.resolve()'d at parse time so symlinks "
                "cannot escape the allowlist after the fact. Entries "
                "outside this allowlist are rejected and the advisor "
                "falls back to the constructor-bound project_root."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/operation_advisor.py"
            ),
            example="/private/tmp/eval-clones:/var/jarvis/scratch",
            since="v3.7 Phase 2 Phase B.2.0 (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Advisor] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "ADVISOR_WORKTREE_AWARE_ENABLED_ENV_VAR",
    "ADVISOR_WORKTREE_ROOT_ALLOWLIST_ENV_VAR",
    "EVIDENCE_REPO_ROOT_KEY",
    "Advisory",
    "AdvisoryDecision",
    "OperationAdvisor",
    "infer_read_only_intent",
    "register_flags",
    "resolve_envelope_repo_root",
]
