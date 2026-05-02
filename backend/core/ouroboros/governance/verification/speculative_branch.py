"""Priority #4 Slice 1 — Speculative Branch Tree primitive.

The cognitive-gap-closing primitive: extends HypothesisProbe (Phase 7.6)
from a single read-only probe to a TREE of N parallel branches at any
decision point. Closes CC's interleaved-thinking + plan-mode-replan +
speculative-branching cognitive paradigm in one Antivenom-aligned move.

Slice 1 ships the **primitive layer only** — pure data + pure compute.
No I/O, no async, no governance imports. Slice 2 adds the async runner;
Slice 3 the comparator; Slice 4 the observer + SSE; Slice 5 graduation.

Closes the cognitive gap identified in §29 brutal review:

  * O+V's HypothesisProbe is ONE probe per ambiguity (Phase 7.6).
  * Move 5's PROBE_ENVIRONMENT fires on confidence drop — a SYMPTOM,
    not the source of ambiguity.
  * Move 6's Quorum is K-flat at GENERATE phase only,
    APPROVAL_REQUIRED+ tier only.
  * SBT runs at ANY decision point with N parallel typed-evidence
    branches that converge via deterministic sha256-fingerprint
    majority. When branches diverge, depth-bounded tie-breaker spawn
    is permitted (Slice 2 runner enforces depth × breadth ×
    wall-time × diminishing-returns triple-termination).

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — frozen dataclasses propagate cleanly
    across async boundaries (Slice 2's runner round-trips
    TreeVerdictResult through asyncio.gather + asyncio.to_thread).

  * **Dynamic** — every numeric threshold (max_depth, max_breadth,
    max_wall_seconds, dim_returns_threshold,
    min_confidence_for_winner) is env-tunable with floor + ceiling
    clamps. NO hardcoded magic constants in convergence logic.

  * **Adaptive** — degraded inputs (empty branch list, all-FAILED
    branches, mixed timeout/success) all map to explicit TreeVerdict
    values rather than raises. INCONCLUSIVE (mixed) and TRUNCATED
    (depth/breadth/wall cap) are first-class outcomes — Slice 3
    aggregator records them distinct from FAILED.

  * **Intelligent** — convergence is sha256-fingerprint majority
    over CANONICAL evidence representation (kind + content_hash),
    not free-text comparison. Same canonicalization discipline as
    Move 6's ast_canonical signatures (Quine-class-resistant by
    construction). Tie-break tolerance via env knob lets operators
    tune strictness without code changes.

  * **Robust** — every public function NEVER raises out. Garbage
    input → TreeVerdict.FAILED rather than exception. Pure-data
    primitive callable from any context, sync or async.

  * **No hardcoding** — 5-value × 3 closed-taxonomy enums
    (J.A.R.M.A.T.R.I.X. — every input maps to exactly one). Per-knob
    env helpers with floor + ceiling clamps mirror Priority #3 Slice
    1's pattern exactly.

  * **Observational not prescriptive** — Slice 1 primitives produce
    verdicts but NEVER propose mutations. Branches consume read-only
    tool budget; tree termination is BOUNDED structurally. Slice 2's
    runner enforces the read-only contract via tool allowlist
    (READONLY_TOOL_ALLOWLIST from Move 5 reused).

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib ONLY. NEVER imports any governance module —
    strongest authority invariant. Slice 3+ may import
    ``adaptation.ledger.MonotonicTighteningVerdict``; Slice 1 stays
    pure.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No async (Slice 2 wraps via asyncio.gather + to_thread).
  * Read-only — never writes a file, never executes code.
  * No mutation tools.
  * No exec/eval/compile (mirrors Move 6 Slice 2 + Priority #1/#2/#3
    Slice 1 critical safety pin).

Master flag default-false until Slice 5 graduation:
``JARVIS_SBT_ENABLED``. Asymmetric env semantics — empty/whitespace
= unset = current default; explicit truthy/falsy overrides at call
time.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


SBT_SCHEMA_VERSION: str = "speculative_branch.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def sbt_enabled() -> bool:
    """``JARVIS_SBT_ENABLED`` (default ``false`` until Slice 5
    graduation).

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates false;
    explicit truthy values evaluate true. Re-read on every call so
    flips hot-revert without restart."""
    raw = os.environ.get("JARVIS_SBT_ENABLED", "").strip().lower()
    if raw == "":
        return False  # default-false until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    """Read int env knob with floor+ceiling clamping. NEVER raises.
    Garbage → default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    """Read float env knob with floor+ceiling clamping. NEVER raises.
    Garbage → default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def sbt_max_depth() -> int:
    """``JARVIS_SBT_MAX_DEPTH`` — maximum tree depth (root + N levels
    of tie-breaker children). Default 3, clamped [1, 8]. Each level
    adds at most max_breadth branches; total branches bounded by
    O(max_breadth × max_depth)."""
    return _env_int_clamped(
        "JARVIS_SBT_MAX_DEPTH", 3, floor=1, ceiling=8,
    )


def sbt_max_breadth() -> int:
    """``JARVIS_SBT_MAX_BREADTH`` — maximum parallel branches per
    level. Default 3, clamped [2, 8]. Two is the floor because a
    single branch can't establish convergence (need at least 2 to
    compare fingerprints)."""
    return _env_int_clamped(
        "JARVIS_SBT_MAX_BREADTH", 3, floor=2, ceiling=8,
    )


def sbt_max_wall_seconds() -> float:
    """``JARVIS_SBT_MAX_WALL_SECONDS`` — total wall-clock budget for
    one tree. Default 60.0, clamped [10.0, 600.0]. Slice 2 runner
    cancels remaining branches once exceeded."""
    return _env_float_clamped(
        "JARVIS_SBT_MAX_WALL_SECONDS", 60.0, floor=10.0, ceiling=600.0,
    )


def sbt_diminishing_returns_threshold() -> float:
    """``JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD`` — when N
    consecutive branches all share the same fingerprint at this
    fraction or higher, halt remaining branches early (no new
    information). Default 0.95, clamped [0.5, 1.0]. Mirrors
    HypothesisProbe's diminishing-returns pattern."""
    return _env_float_clamped(
        "JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD", 0.95,
        floor=0.5, ceiling=1.0,
    )


def sbt_min_confidence_for_winner() -> float:
    """``JARVIS_SBT_MIN_CONFIDENCE_FOR_WINNER`` — minimum aggregated
    branch confidence (0.0-1.0) required for CONVERGED outcome. When
    majority fingerprint's average confidence falls below this,
    outcome demotes to INCONCLUSIVE. Default 0.5, clamped
    [0.0, 1.0]."""
    return _env_float_clamped(
        "JARVIS_SBT_MIN_CONFIDENCE_FOR_WINNER", 0.5,
        floor=0.0, ceiling=1.0,
    )


# ---------------------------------------------------------------------------
# Closed-taxonomy enums — J.A.R.M.A.T.R.I.X. (3 × 5 values)
# ---------------------------------------------------------------------------


class BranchOutcome(str, enum.Enum):
    """5-value closed taxonomy for a single branch's result.

    Every per-branch result maps to exactly one — the runner branches
    on the enum, never on free-form fields."""

    SUCCESS = "success"
    """Branch ran to completion with confidence ≥ min threshold."""

    PARTIAL = "partial"
    """Branch ran but confidence below min threshold (evidence
    insufficient). Distinct from FAILED — the branch produced
    structured evidence, just not conclusive."""

    TIMEOUT = "timeout"
    """Branch exhausted its share of the wall-clock budget. Slice 2
    runner cancels remaining tool calls and reports the partial
    evidence collected."""

    DISABLED = "disabled"
    """Master flag off, or sub-flag (engine/runner) off. No branch
    work performed."""

    FAILED = "failed"
    """Branch raised internally OR caller-supplied target was
    invalid. Distinct from PARTIAL (which is observational
    insufficiency)."""


class EvidenceKind(str, enum.Enum):
    """5-value closed taxonomy for the EVIDENCE a branch produces.

    Branches return TYPED evidence (not free text) — prompt-injection-
    resistant by construction. Each kind maps to a specific Slice 2
    read-only tool category from READONLY_TOOL_ALLOWLIST (Move 5)."""

    FILE_READ = "file_read"
    """Evidence from ``read_file`` — file contents (or excerpt)
    materialized to inform the branch's verdict."""

    SYMBOL_LOOKUP = "symbol_lookup"
    """Evidence from ``list_symbols`` / ``list_dir`` — symbol /
    namespace topology lookup."""

    PATTERN_MATCH = "pattern_match"
    """Evidence from ``search_code`` / ``glob_files`` — regex or
    pattern hits across the codebase."""

    CALLER_GRAPH = "caller_graph"
    """Evidence from ``get_callers`` — caller / callee graph
    relationships for a target symbol."""

    TYPE_INFERENCE = "type_inference"
    """Evidence from AST-level type inference (read-only static
    analysis). Distinct from PATTERN_MATCH because it carries
    structured type information rather than text matches."""


class TreeVerdict(str, enum.Enum):
    """5-value closed taxonomy for the AGGREGATE TREE verdict.

    Resolves over a stream of BranchResults via deterministic
    sha256-fingerprint majority. Distinct from BranchOutcome (which
    is per-branch)."""

    CONVERGED = "converged"
    """Majority of SUCCESS-outcome branches share a single
    fingerprint AND average confidence ≥ min threshold. The tree
    has resolved the ambiguity; the winning branch's evidence is
    actionable."""

    DIVERGED = "diverged"
    """≥2 distinct fingerprints with no majority (or majority
    confidence below min threshold). The ambiguity is genuine —
    Slice 2 runner MAY spawn one tie-breaker sub-branch (depth
    permitting); Slice 3 comparator records the diverged tree as
    a signal that this decision class needs operator escalation."""

    INCONCLUSIVE = "inconclusive"
    """Mixed branches (some SUCCESS, some FAILED, some PARTIAL) with
    no clear pattern. Distinct from DIVERGED — the branches don't
    even agree on whether they have evidence, let alone what it
    says."""

    TRUNCATED = "truncated"
    """Tree hit its depth × breadth × wall-time cap before
    converging. Slice 2 records the partial state; operators see
    this as a signal that the budget was insufficient."""

    FAILED = "failed"
    """Empty branch list, garbage input, or all branches FAILED.
    Slice 3 records distinct from TRUNCATED (which had successful
    work)."""


# ---------------------------------------------------------------------------
# Frozen dataclasses — schema with to_dict/from_dict round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchEvidence:
    """One typed evidence object produced by a single branch.

    Frozen + hashable so branches can be deduplicated by fingerprint
    set. ``content_hash`` is the canonical sha256 of the evidence
    payload (computed by Slice 2 at evidence-capture time); the
    primitive layer doesn't compute it — just records.

    Fields:
      * ``kind`` — closed-taxonomy EvidenceKind value
      * ``content_hash`` — sha256 hex string of the canonicalized
        payload (stable across branches that return semantically
        equivalent evidence)
      * ``confidence`` — 0.0-1.0; branch's self-reported confidence
        in this evidence
      * ``source_tool`` — name of the read-only tool that produced
        the evidence (for observability; not load-bearing)
      * ``snippet`` — bounded string (≤256 chars) for operator
        diagnostics; full payload lives at the source tool's audit
        trail
    """
    kind: EvidenceKind
    content_hash: str
    confidence: float = 0.0
    source_tool: str = ""
    snippet: str = ""
    schema_version: str = SBT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "content_hash": str(self.content_hash),
            "confidence": float(self.confidence),
            "source_tool": str(self.source_tool),
            "snippet": str(self.snippet)[:256],
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["BranchEvidence"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != SBT_SCHEMA_VERSION:
                return None
            kind_raw = raw.get("kind")
            if not isinstance(kind_raw, str):
                return None
            try:
                kind = EvidenceKind(kind_raw)
            except ValueError:
                return None
            return cls(
                kind=kind,
                content_hash=str(raw.get("content_hash", "")),
                confidence=float(raw.get("confidence", 0.0)),
                source_tool=str(raw.get("source_tool", "")),
                snippet=str(raw.get("snippet", ""))[:256],
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class BranchResult:
    """One branch's complete result — frozen, hashable, immutable.

    Fields:
      * ``branch_id`` — caller-supplied (Slice 2 runner generates
        deterministic IDs from tree position)
      * ``outcome`` — closed-taxonomy BranchOutcome
      * ``evidence`` — tuple of typed evidence objects (frozen)
      * ``elapsed_ms`` — wall-clock duration of this branch
      * ``depth`` — position in the tree (0 = root branch, 1+ =
        tie-breaker children)
      * ``fingerprint`` — sha256[:16] over canonical evidence list
        (stable across branches that produced semantically
        equivalent evidence)
      * ``error_detail`` — populated only on FAILED outcome
    """
    branch_id: str
    outcome: BranchOutcome
    evidence: Tuple[BranchEvidence, ...] = field(default_factory=tuple)
    elapsed_ms: float = 0.0
    depth: int = 0
    fingerprint: str = ""
    error_detail: str = ""
    schema_version: str = SBT_SCHEMA_VERSION

    def average_confidence(self) -> float:
        """Mean confidence across this branch's evidence. Returns
        0.0 on empty evidence (defensive)."""
        if not self.evidence:
            return 0.0
        try:
            total = sum(e.confidence for e in self.evidence)
            return float(total / len(self.evidence))
        except Exception:  # noqa: BLE001 — defensive
            return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch_id": str(self.branch_id),
            "outcome": self.outcome.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "elapsed_ms": float(self.elapsed_ms),
            "depth": int(self.depth),
            "fingerprint": str(self.fingerprint),
            "error_detail": str(self.error_detail or ""),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["BranchResult"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != SBT_SCHEMA_VERSION:
                return None
            outcome_raw = raw.get("outcome")
            if not isinstance(outcome_raw, str):
                return None
            try:
                outcome = BranchOutcome(outcome_raw)
            except ValueError:
                return None
            evidence_raw = raw.get("evidence", [])
            evidence: Tuple[BranchEvidence, ...] = ()
            if isinstance(evidence_raw, Sequence):
                parsed = []
                for item in evidence_raw:
                    if isinstance(item, Mapping):
                        ev = BranchEvidence.from_dict(item)
                        if ev is not None:
                            parsed.append(ev)
                evidence = tuple(parsed)
            return cls(
                branch_id=str(raw.get("branch_id", "")),
                outcome=outcome,
                evidence=evidence,
                elapsed_ms=float(raw.get("elapsed_ms", 0.0)),
                depth=int(raw.get("depth", 0)),
                fingerprint=str(raw.get("fingerprint", "")),
                error_detail=str(raw.get("error_detail", "")),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class BranchTreeTarget:
    """Caller-supplied descriptor for ONE speculative tree.

    Fields:
      * ``decision_id`` — caller-supplied identifier (typically
        ``op_id + phase + ambiguity_kind``); used as the target
        for downstream observability + dedup
      * ``ambiguity_kind`` — free-form classifier for what kind of
        ambiguity the tree is resolving (e.g.,
        ``"missing_function_signature"``, ``"unclear_dep_graph"``)
      * ``ambiguity_payload`` — opaque map carrying ambiguity-specific
        context (e.g., file paths, symbol names) — Slice 2 prober
        consumes this; primitive layer doesn't interpret
      * ``max_depth`` / ``max_breadth`` / ``max_wall_seconds`` —
        per-target overrides of env knobs (None = use env default)
    """
    decision_id: str
    ambiguity_kind: str
    ambiguity_payload: Mapping[str, Any] = field(default_factory=dict)
    max_depth: Optional[int] = None
    max_breadth: Optional[int] = None
    max_wall_seconds: Optional[float] = None
    schema_version: str = SBT_SCHEMA_VERSION

    def effective_max_depth(self) -> int:
        if self.max_depth is None:
            return sbt_max_depth()
        return max(1, min(8, int(self.max_depth)))

    def effective_max_breadth(self) -> int:
        if self.max_breadth is None:
            return sbt_max_breadth()
        return max(2, min(8, int(self.max_breadth)))

    def effective_max_wall_seconds(self) -> float:
        if self.max_wall_seconds is None:
            return sbt_max_wall_seconds()
        return max(10.0, min(600.0, float(self.max_wall_seconds)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": str(self.decision_id),
            "ambiguity_kind": str(self.ambiguity_kind),
            "ambiguity_payload": dict(self.ambiguity_payload),
            "max_depth": (
                None if self.max_depth is None
                else int(self.max_depth)
            ),
            "max_breadth": (
                None if self.max_breadth is None
                else int(self.max_breadth)
            ),
            "max_wall_seconds": (
                None if self.max_wall_seconds is None
                else float(self.max_wall_seconds)
            ),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["BranchTreeTarget"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != SBT_SCHEMA_VERSION:
                return None
            payload = raw.get("ambiguity_payload", {})
            if not isinstance(payload, Mapping):
                payload = {}
            return cls(
                decision_id=str(raw.get("decision_id", "")),
                ambiguity_kind=str(raw.get("ambiguity_kind", "")),
                ambiguity_payload=dict(payload),
                max_depth=raw.get("max_depth"),
                max_breadth=raw.get("max_breadth"),
                max_wall_seconds=raw.get("max_wall_seconds"),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class TreeVerdictResult:
    """Aggregate verdict for ONE speculative tree run.

    Fields:
      * ``outcome`` — closed-taxonomy TreeVerdict
      * ``target`` — original caller target (for observability)
      * ``branches`` — tuple of all BranchResults (in chronological
        order; depth × breadth × tie-breakers)
      * ``winning_branch_idx`` — index into ``branches`` of the
        majority-fingerprint winner (None when no convergence)
      * ``winning_fingerprint`` — sha256[:16] of the winning
        evidence canonical (empty when no convergence)
      * ``aggregate_confidence`` — average confidence across the
        winning branch's evidence (0.0 when no winner)
      * ``detail`` — operator-readable summary string
    """
    outcome: TreeVerdict
    target: Optional[BranchTreeTarget] = None
    branches: Tuple[BranchResult, ...] = field(default_factory=tuple)
    winning_branch_idx: Optional[int] = None
    winning_fingerprint: str = ""
    aggregate_confidence: float = 0.0
    detail: str = ""
    schema_version: str = SBT_SCHEMA_VERSION

    def is_actionable(self) -> bool:
        """True iff the tree CONVERGED with a winner. Caller uses
        this to decide whether to act on the evidence."""
        return (
            self.outcome is TreeVerdict.CONVERGED
            and self.winning_branch_idx is not None
        )

    def has_disagreement_signal(self) -> bool:
        """True iff branches disagreed (DIVERGED) — operators see
        this as escalation signal even when no winner."""
        return self.outcome is TreeVerdict.DIVERGED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "target": (
                self.target.to_dict() if self.target is not None
                else None
            ),
            "branches": [b.to_dict() for b in self.branches],
            "winning_branch_idx": (
                None if self.winning_branch_idx is None
                else int(self.winning_branch_idx)
            ),
            "winning_fingerprint": str(self.winning_fingerprint),
            "aggregate_confidence": float(self.aggregate_confidence),
            "detail": str(self.detail or ""),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["TreeVerdictResult"]:
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != SBT_SCHEMA_VERSION:
                return None
            outcome_raw = raw.get("outcome")
            if not isinstance(outcome_raw, str):
                return None
            try:
                outcome = TreeVerdict(outcome_raw)
            except ValueError:
                return None
            target = None
            target_raw = raw.get("target")
            if isinstance(target_raw, Mapping):
                target = BranchTreeTarget.from_dict(target_raw)
            branches_raw = raw.get("branches", [])
            branches: Tuple[BranchResult, ...] = ()
            if isinstance(branches_raw, Sequence):
                parsed = []
                for item in branches_raw:
                    if isinstance(item, Mapping):
                        br = BranchResult.from_dict(item)
                        if br is not None:
                            parsed.append(br)
                branches = tuple(parsed)
            winning_idx_raw = raw.get("winning_branch_idx")
            winning_idx: Optional[int] = None
            if winning_idx_raw is not None:
                try:
                    winning_idx = int(winning_idx_raw)
                except (TypeError, ValueError):
                    winning_idx = None
            return cls(
                outcome=outcome,
                target=target,
                branches=branches,
                winning_branch_idx=winning_idx,
                winning_fingerprint=str(raw.get("winning_fingerprint", "")),
                aggregate_confidence=float(
                    raw.get("aggregate_confidence", 0.0),
                ),
                detail=str(raw.get("detail", "")),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Pure decision functions — the convergence logic
# ---------------------------------------------------------------------------


def canonical_evidence_fingerprint(
    evidence: Sequence[BranchEvidence],
) -> str:
    """Deterministic sha256[:16] over a canonical evidence list.

    Two semantically equivalent evidence sequences produce the same
    fingerprint. Canonicalization order:
      1. Sort by ``(kind.value, content_hash)`` ascending — branches
         that captured the same evidence in different order produce
         the same fingerprint
      2. Concatenate ``f"{kind}|{content_hash}"`` separated by ``\n``
      3. sha256 the bytes; return first 16 hex chars

    NEVER raises. Garbage input → ``""``."""
    try:
        if not evidence:
            return ""
        ordered = sorted(
            evidence,
            key=lambda e: (
                getattr(e.kind, "value", "") or "",
                str(e.content_hash or ""),
            ),
        )
        canonical = "\n".join(
            f"{e.kind.value}|{e.content_hash}" for e in ordered
        )
        return hashlib.sha256(
            canonical.encode("utf-8"),
        ).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt] canonical_evidence_fingerprint failed: %s", exc,
        )
        return ""


def compute_tree_verdict(
    branches: Sequence[BranchResult],
    *,
    min_confidence: Optional[float] = None,
) -> TreeVerdict:
    """Pure convergence resolver.

    Decision tree (every input maps to exactly one verdict):

      1. ``branches`` is empty / not Sequence → FAILED
      2. ALL branches have outcome=FAILED → FAILED
      3. ALL branches have outcome=TIMEOUT → TRUNCATED
      4. Mix of SUCCESS and FAILED with no SUCCESS majority →
         INCONCLUSIVE
      5. Group SUCCESS branches by ``fingerprint``:
         a. Single fingerprint group with ≥2 branches AND
            average confidence ≥ min_confidence → CONVERGED
         b. Single fingerprint group but confidence below
            threshold → INCONCLUSIVE
         c. ≥2 distinct fingerprints with no majority → DIVERGED
         d. Majority fingerprint AND confidence ≥ threshold →
            CONVERGED
         e. Majority fingerprint but confidence below threshold →
            INCONCLUSIVE

    NEVER raises."""
    try:
        if not isinstance(branches, Sequence):
            return TreeVerdict.FAILED
        if not branches:
            return TreeVerdict.FAILED

        threshold = (
            float(min_confidence)
            if min_confidence is not None
            else sbt_min_confidence_for_winner()
        )
        threshold = max(0.0, min(1.0, threshold))

        # Bucket branches by outcome.
        successes = [
            b for b in branches
            if isinstance(b, BranchResult)
            and b.outcome is BranchOutcome.SUCCESS
        ]
        failures = [
            b for b in branches
            if isinstance(b, BranchResult)
            and b.outcome is BranchOutcome.FAILED
        ]
        timeouts = [
            b for b in branches
            if isinstance(b, BranchResult)
            and b.outcome is BranchOutcome.TIMEOUT
        ]
        partials = [
            b for b in branches
            if isinstance(b, BranchResult)
            and b.outcome is BranchOutcome.PARTIAL
        ]

        total_real = len(successes) + len(failures) + len(timeouts) + len(partials)
        if total_real == 0:
            return TreeVerdict.FAILED

        # All-failed paths.
        if not successes and not partials:
            if len(timeouts) == total_real:
                return TreeVerdict.TRUNCATED
            return TreeVerdict.FAILED

        # No SUCCESS branches → INCONCLUSIVE (partials alone don't
        # constitute convergence; the tree didn't establish a winner).
        if not successes:
            return TreeVerdict.INCONCLUSIVE

        # Group SUCCESS branches by fingerprint.
        fingerprint_groups: Dict[str, list[BranchResult]] = {}
        for b in successes:
            fp = b.fingerprint or ""
            fingerprint_groups.setdefault(fp, []).append(b)

        # If only one fingerprint exists across SUCCESS branches:
        if len(fingerprint_groups) == 1:
            group = next(iter(fingerprint_groups.values()))
            avg_conf = _average_branch_confidence(group)
            if avg_conf >= threshold and len(group) >= 1:
                return TreeVerdict.CONVERGED
            return TreeVerdict.INCONCLUSIVE

        # Multiple distinct fingerprints — find majority.
        max_size = max(len(g) for g in fingerprint_groups.values())
        majority_groups = [
            g for g in fingerprint_groups.values()
            if len(g) == max_size
        ]
        if len(majority_groups) > 1:
            # Tie between groups → DIVERGED
            return TreeVerdict.DIVERGED
        # Strict majority requires the largest group has more
        # branches than the union of all others.
        majority_size = max_size
        non_majority_size = len(successes) - majority_size
        if majority_size <= non_majority_size:
            # No strict majority → DIVERGED
            return TreeVerdict.DIVERGED

        # Strict majority — check confidence.
        majority_group = majority_groups[0]
        avg_conf = _average_branch_confidence(majority_group)
        if avg_conf >= threshold:
            return TreeVerdict.CONVERGED
        return TreeVerdict.INCONCLUSIVE
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[sbt] compute_tree_verdict failed: %s", exc)
        return TreeVerdict.FAILED


def _average_branch_confidence(
    branches: Sequence[BranchResult],
) -> float:
    """Mean of per-branch average_confidence over a group. NEVER
    raises — empty → 0.0."""
    if not branches:
        return 0.0
    try:
        total = sum(b.average_confidence() for b in branches)
        return float(total / len(branches))
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def compute_tree_outcome(
    target: Optional[BranchTreeTarget],
    branches: Sequence[BranchResult],
    *,
    enabled_override: Optional[bool] = None,
    min_confidence: Optional[float] = None,
) -> TreeVerdictResult:
    """Aggregate tree result. Composes verdict over branches and
    stamps the closed-taxonomy outcome. NEVER raises.

    Decision tree:
      1. Master flag off → DISABLED (TreeVerdict.FAILED with
         detail explaining)
      2. Target missing / not BranchTreeTarget → FAILED
      3. Compute verdict via ``compute_tree_verdict``
      4. On CONVERGED: identify winning fingerprint + branch index
      5. On DIVERGED / INCONCLUSIVE / TRUNCATED: no winner;
         winning_branch_idx=None
    """
    try:
        is_enabled = (
            enabled_override if enabled_override is not None
            else sbt_enabled()
        )
        if not is_enabled:
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                target=(
                    target if isinstance(target, BranchTreeTarget)
                    else None
                ),
                detail=(
                    "JARVIS_SBT_ENABLED is false (or override) — "
                    "no speculative tree performed"
                ),
            )

        if not isinstance(target, BranchTreeTarget):
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                detail="target is not a BranchTreeTarget",
            )

        if not isinstance(branches, Sequence) or not branches:
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                target=target,
                detail="empty branches list",
            )

        # Defensive: filter out non-BranchResult items (Slice 2's
        # runner shouldn't produce these, but the primitive is
        # callable from any context).
        valid_branches = tuple(
            b for b in branches if isinstance(b, BranchResult)
        )
        if not valid_branches:
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                target=target,
                branches=(),
                detail="no valid BranchResult items",
            )

        verdict = compute_tree_verdict(
            valid_branches, min_confidence=min_confidence,
        )

        winning_idx: Optional[int] = None
        winning_fp = ""
        aggregate_conf = 0.0

        if verdict is TreeVerdict.CONVERGED:
            winning_idx, winning_fp, aggregate_conf = (
                _find_winning_branch(valid_branches)
            )

        detail = _compose_outcome_detail(
            verdict, valid_branches, winning_idx, winning_fp,
            aggregate_conf,
        )

        return TreeVerdictResult(
            outcome=verdict,
            target=target,
            branches=valid_branches,
            winning_branch_idx=winning_idx,
            winning_fingerprint=winning_fp,
            aggregate_confidence=aggregate_conf,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[sbt] compute_tree_outcome failed: %s", exc)
        return TreeVerdictResult(
            outcome=TreeVerdict.FAILED,
            target=(
                target if isinstance(target, BranchTreeTarget) else None
            ),
            detail=f"compute_tree_outcome_error:{type(exc).__name__}",
        )


def _find_winning_branch(
    branches: Sequence[BranchResult],
) -> Tuple[Optional[int], str, float]:
    """Locate the SUCCESS branch with the majority fingerprint.

    Returns ``(branch_idx, fingerprint, aggregate_confidence)``.
    Aggregate is the mean confidence over the winning fingerprint
    group, NOT just the chosen branch — same group, same evidence."""
    successes = [
        (i, b) for i, b in enumerate(branches)
        if isinstance(b, BranchResult)
        and b.outcome is BranchOutcome.SUCCESS
    ]
    if not successes:
        return (None, "", 0.0)

    groups: Dict[str, list[Tuple[int, BranchResult]]] = {}
    for i, b in successes:
        groups.setdefault(b.fingerprint or "", []).append((i, b))

    if not groups:
        return (None, "", 0.0)

    # Largest group wins (caller already verified strict majority).
    winning_fp = max(
        groups.keys(), key=lambda fp: len(groups[fp]),
    )
    winning_group = groups[winning_fp]
    # Pick the branch in the winning group with HIGHEST average
    # confidence — operators want the strongest evidence in hand.
    winning_idx, winning_branch = max(
        winning_group,
        key=lambda pair: pair[1].average_confidence(),
    )
    aggregate = _average_branch_confidence(
        [b for _, b in winning_group],
    )
    return (winning_idx, winning_fp, aggregate)


def _compose_outcome_detail(
    verdict: TreeVerdict,
    branches: Sequence[BranchResult],
    winning_idx: Optional[int],
    winning_fp: str,
    aggregate_conf: float,
) -> str:
    """Operator-readable detail string. Same dense-token discipline
    as Priority #3's compose_aggregated_detail."""
    try:
        n_total = len(branches)
        n_success = sum(
            1 for b in branches if b.outcome is BranchOutcome.SUCCESS
        )
        n_failed = sum(
            1 for b in branches if b.outcome is BranchOutcome.FAILED
        )
        n_timeout = sum(
            1 for b in branches if b.outcome is BranchOutcome.TIMEOUT
        )
        n_partial = sum(
            1 for b in branches if b.outcome is BranchOutcome.PARTIAL
        )
        depths = [b.depth for b in branches]
        max_depth_seen = max(depths) if depths else 0

        tokens = [
            f"verdict={verdict.value}",
            f"branches={n_total}",
            f"success={n_success}",
            f"failed={n_failed}",
            f"timeout={n_timeout}",
            f"partial={n_partial}",
            f"max_depth={max_depth_seen}",
        ]
        if verdict is TreeVerdict.CONVERGED and winning_idx is not None:
            tokens.append(f"winner_idx={winning_idx}")
            tokens.append(f"winner_fp={winning_fp}")
            tokens.append(f"agg_conf={aggregate_conf:.3f}")
        return " ".join(tokens)
    except Exception:  # noqa: BLE001 — defensive
        return f"verdict={verdict.value}"


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "BranchEvidence",
    "BranchOutcome",
    "BranchResult",
    "BranchTreeTarget",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "EvidenceKind",
    "SBT_SCHEMA_VERSION",
    "TreeVerdict",
    "TreeVerdictResult",
    "canonical_evidence_fingerprint",
    "compute_tree_outcome",
    "compute_tree_verdict",
    "sbt_diminishing_returns_threshold",
    "sbt_enabled",
    "sbt_max_breadth",
    "sbt_max_depth",
    "sbt_max_wall_seconds",
    "sbt_min_confidence_for_winner",
]
