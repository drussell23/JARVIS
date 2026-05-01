"""Priority #2 Slice 1 — PostmortemRecall primitive.

Cross-session sibling of ``episodic_memory.EpisodicFailureMemory``:
where EpisodicFailureMemory carries failure context WITHIN a single
operation (Reflexion-style retry feedback), PostmortemRecall carries
failure context ACROSS sessions. The two compose linearly — per-op
memory governs GENERATE retry; cross-session recall governs first-
pass GENERATE composition. Zero overlap, zero replacement.

Closes the recurrence-prevention loop identified in §28.7's brutal
review: Move 4 detects structural drift, Priority #1 detects
behavioral drift (including ``RECURRENCE_DRIFT``). Without
PostmortemRecall, those signals are operator-readable but never
operationally consumed — same ``failure_class`` postmortem can recur
indefinitely across sessions because no mechanism injects the prior-
failure context. Priority #2 Slice 4 will activate Priority #1 Slice
4's currently-dormant ``INJECT_POSTMORTEM_RECALL_HINT`` advisory by
boosting recall budget on detected recurrence drift.

Slice 1 ships the **primitive layer only** — pure data + pure
compute. No I/O, no async, no governance imports. Slice 2 adds the
cross-session index store (parses ``summary.json`` files via the
shared ``last_session_summary._parse_summary``); Slice 3 the
``CONTEXT_EXPANSION`` injector with robust degradation; Slice 4 the
recurrence consumer; Slice 5 graduation.

Direct-solve principles (per the operator directive):

  * **Asynchronous-ready** — frozen dataclasses propagate cleanly
    across async boundaries (Slice 3's CONTEXT_EXPANSION injection
    will round-trip ``RecallVerdict`` through orchestrator hooks).

  * **Dynamic** — every numeric threshold is env-tunable with floor
    + ceiling clamps. NO hardcoded magic constants. Per-component
    knobs (top_k / max_age_days / halflife_days / threshold) are
    independently adjustable.

  * **Adaptive** — degraded inputs (empty index, all-irrelevant
    records, garbage records) all map to explicit ``RecallOutcome``
    values rather than raises. EMPTY_INDEX vs MISS vs DISABLED are
    distinct first-class outcomes — Slice 3 renders different prompt
    sections (or nothing) per outcome.

  * **Intelligent** — relevance is a 4-value closed enum
    (NONE/LOW/MEDIUM/HIGH) with deterministic mapping from (file,
    symbol, failure_class, ast_signature) overlap. Recency-weighted
    ranking via SemanticIndex's halflife-decay formula (literal byte-
    parity pinned by companion test, mirroring Priority #1 Slice 1's
    discipline — keeps the module pure-stdlib).

  * **Robust** — every public function is total. Garbage input,
    schema mismatch, type errors — all collapse to
    ``RecallOutcome.FAILED`` or ``RelevanceLevel.NONE``, never
    raises.

  * **No hardcoding** — 5-value ``RecallOutcome`` and 4-value
    ``RelevanceLevel`` are closed taxonomy enums (J.A.R.M.A.T.R.I.X.
    — every input maps to exactly one). Per-knob env helpers with
    floor+ceiling clamps mirror the Move 5/6/Priority#1 patterns.

  * **Zero duplication** — ``PostmortemRecord`` is the cross-session
    extension of ``episodic_memory.FailureEpisode``: every
    FailureEpisode field is present here with matching type.
    Verified by ``test_failure_episode_field_parity`` in the
    companion suite. No duplicate parser, no duplicate sanitizer
    (Slice 2 reuses ``last_session_summary._parse_summary``; Slice 3
    reuses ``last_session_summary._sanitize_field``).

Authority invariants (AST-pinned by Slice 5 graduation):

  * Imports stdlib ONLY. NEVER imports any governance module —
    not even Move 6 Slice 2's ``ast_canonical`` (Slice 2 may import
    it for fingerprint computation; Slice 1 receives precomputed
    fingerprints as input). Strongest possible authority invariant:
    pure-stdlib means zero attack surface from upstream module
    changes.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * Never imports ``episodic_memory`` (avoid coupling — field-parity
    is verified by AST test, not enforced by runtime import).
  * No async functions in Slice 1 (Slice 3+ may introduce async).
  * Read-only — never writes a file, never executes code.
  * No mutation tools.
  * No exec/eval/compile (mirrors Move 6 Slice 2 + Priority #1
    Slice 1 critical safety pin).

Master flag default-false until Slice 5 graduation:
``JARVIS_POSTMORTEM_RECALL_ENABLED``. Asymmetric env semantics —
empty/whitespace = unset = current default; explicit truthy/falsy
overrides at call time.

Closed taxonomies:

  * ``RecallOutcome`` (5 values):
      ``HIT``         — at least one record met threshold
      ``MISS``        — index non-empty but no records passed
                        relevance threshold
      ``EMPTY_INDEX`` — index has zero records (cold start)
      ``DISABLED``    — master flag off
      ``FAILED``      — defensive sentinel
  * ``RelevanceLevel`` (4 values):
      ``NONE``   — no overlap on any criterion
      ``LOW``    — failure_class match only
      ``MEDIUM`` — failure_class + (file OR symbol) match
      ``HIGH``  — failure_class + file + symbol match, OR ast
                  signature exact match
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


POSTMORTEM_RECALL_SCHEMA_VERSION: str = "postmortem_recall.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


def postmortem_recall_enabled() -> bool:
    """``JARVIS_POSTMORTEM_RECALL_ENABLED`` (default ``true``
    post Slice 5 graduation 2026-05-01).

    Master kill switch for the cross-session PostmortemRecall arc.
    When false, the entire 4-slice pipeline reverts in lockstep:
      * recall_postmortems returns DISABLED
      * record_postmortem / rebuild_index → FAILED
      * render_postmortem_recall_section → ""
      * get_active_recurrence_boosts → empty mapping

    Cost-correctness: graduating default-true is appropriate
    because PostmortemRecall is read-only over existing artifacts
    (.ouroboros/sessions/*/summary.json + Priority #1's
    .jarvis/coherence_advisory.jsonl), runs at CONTEXT_EXPANSION
    (per-op pre-generation, NOT per-LLM-call), and produces ONLY
    advisory output (operator approval still required for any
    actual flag flip downstream). Zero LLM calls. Zero additional
    generation amplification.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit ``0``/``false``/``no``/``off`` evaluates
    false; explicit truthy values evaluate true. Re-read on every
    call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_RECALL_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-01 (Priority #2 Slice 5)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env-knob helpers — every numeric clamped (floor + ceiling)
# ---------------------------------------------------------------------------


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def recall_top_k() -> int:
    """``JARVIS_POSTMORTEM_RECALL_TOP_K`` (default 3, floor 1,
    ceiling 10).

    Maximum number of records returned per ``recall_postmortems``
    call. Slice 4's recurrence consumer can boost this up to
    ``recall_top_k_ceiling()`` for matched failure_class on next
    N ops; the absolute ceiling is the hard structural cap."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_TOP_K",
        3, floor=1, ceiling=10,
    )


def recall_top_k_ceiling() -> int:
    """``JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING`` (default 10,
    floor 3, ceiling 30).

    Absolute hard cap for top_k including any Slice 4 recurrence-
    boost extension. Even a maximally-boosted recall cannot
    exceed this — operator-bounded by construction."""
    return _env_int_clamped(
        "JARVIS_POSTMORTEM_RECALL_TOP_K_CEILING",
        10, floor=3, ceiling=30,
    )


def recall_max_age_days() -> float:
    """``JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS`` (default 30.0,
    floor 1.0, ceiling 365.0).

    Records older than this age (computed at recall time vs
    ``timestamp`` field) are excluded from results. Stale
    postmortems shouldn't bias new ops indefinitely."""
    return _env_float_clamped(
        "JARVIS_POSTMORTEM_RECALL_MAX_AGE_DAYS",
        30.0, floor=1.0, ceiling=365.0,
    )


def recall_halflife_days() -> float:
    """``JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS`` (default 14.0,
    floor 0.5, ceiling 90.0).

    Recency-weight halflife for ranking. Mirrors SemanticIndex's
    14-day default for cross-arc consistency. Formula parity with
    ``semantic_index._recency_weight`` is pinned by companion
    test (literal byte-equivalence) — re-implemented here so this
    module stays pure-stdlib (zero governance imports)."""
    return _env_float_clamped(
        "JARVIS_POSTMORTEM_RECALL_HALFLIFE_DAYS",
        14.0, floor=0.5, ceiling=90.0,
    )


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of recall outcomes (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class RecallOutcome(str, enum.Enum):
    """5-value closed taxonomy. Every ``recall_postmortems``
    call returns exactly one — never None, never implicit
    fall-through.

    ``HIT``         — at least one record met threshold
    ``MISS``        — index non-empty but no records met
                      threshold (e.g., target file/symbol has
                      no prior failures)
    ``EMPTY_INDEX`` — index has zero records (cold start /
                      fresh deployment)
    ``DISABLED``    — master flag off
    ``FAILED``      — defensive sentinel"""

    HIT = "hit"
    MISS = "miss"
    EMPTY_INDEX = "empty_index"
    DISABLED = "disabled"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Closed 4-value taxonomy of relevance levels
# ---------------------------------------------------------------------------


class RelevanceLevel(str, enum.Enum):
    """4-value closed taxonomy.

    ``NONE``    — no overlap (also returned on failure_class
                  hard-filter rejection)
    ``LOW``     — failure_class match only (target had
                  failure_class but no file/symbol overlap)
    ``MEDIUM``  — file OR symbol overlap (failure_class either
                  None or matching)
    ``HIGH``    — file + symbol both overlap, OR exact ast
                  signature match (structural identity)"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RELEVANCE_RANK: Dict[RelevanceLevel, int] = {
    RelevanceLevel.NONE: 0,
    RelevanceLevel.LOW: 1,
    RelevanceLevel.MEDIUM: 2,
    RelevanceLevel.HIGH: 3,
}


_VALID_RELEVANCE_THRESHOLDS: Tuple[RelevanceLevel, ...] = (
    RelevanceLevel.LOW,
    RelevanceLevel.MEDIUM,
    RelevanceLevel.HIGH,
)


def recall_relevance_threshold() -> RelevanceLevel:
    """``JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD`` (default
    ``"medium"``; valid ``"low"|"medium"|"high"``).

    Records below this threshold are excluded from results.
    Default MEDIUM means a record matches a file OR symbol AND
    matches the failure_class filter (if specified). HARDEN
    posture may want LOW (more inclusive); MAINTAIN posture may
    want HIGH (tighter filtering)."""
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_RECALL_RELEVANCE_THRESHOLD", "",
    ).strip().lower()
    if not raw:
        return RelevanceLevel.MEDIUM
    try:
        level = RelevanceLevel(raw)
    except ValueError:
        return RelevanceLevel.MEDIUM
    # Defensive: NONE is not a valid threshold (would match
    # everything including non-overlapping records)
    if level not in _VALID_RELEVANCE_THRESHOLDS:
        return RelevanceLevel.MEDIUM
    return level


# ---------------------------------------------------------------------------
# Frozen dataclasses — propagation-safe across async + lock boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostmortemRecord:
    """Cross-session failure record. Frozen — immutable after
    parsing.

    **Field-parity contract** (verified by
    ``test_failure_episode_field_parity``): every
    ``episodic_memory.FailureEpisode`` field is present here with
    matching type:
      * ``file_path: str``
      * ``attempt: int``
      * ``failure_class: str``
      * ``error_summary: str``
      * ``specific_errors: Tuple[str, ...]``
      * ``line_numbers: Tuple[int, ...]``
      * ``timestamp: float``

    **Semantic divergence**: ``timestamp`` here is wall-clock
    epoch (``time.time()``) for cross-session ordering, vs
    ``FailureEpisode``'s monotonic (within-op ordering only).
    Cross-session lookups by elapsed-days require epoch.

    **Cross-session extensions** (not in FailureEpisode):
      * ``session_id`` — which session produced the postmortem
      * ``op_id`` — which op produced the postmortem
      * ``symbol_name`` — function/class/method name (extracted
        by Slice 2's parser; "" when unavailable)
      * ``ast_signature`` — Move 6 Slice 2 fingerprint of the
        symbol's AST (for structural-identity matching across
        renames; "" when not computed)
      * ``failure_phase`` — pipeline phase (GENERATE / VALIDATE /
        APPLY / etc); "" when unavailable
      * ``failure_reason`` — more detailed than ``error_summary``
        (full sanitized error chain)
      * ``schema_version``"""

    # FailureEpisode field-parity (7 fields)
    file_path: str = ""
    attempt: int = 0
    failure_class: str = ""
    error_summary: str = ""
    specific_errors: Tuple[str, ...] = field(default_factory=tuple)
    line_numbers: Tuple[int, ...] = field(default_factory=tuple)
    timestamp: float = field(default_factory=time.time)

    # Cross-session extensions
    session_id: str = ""
    op_id: str = ""
    symbol_name: str = ""
    ast_signature: str = ""
    failure_phase: str = ""
    failure_reason: str = ""
    schema_version: str = POSTMORTEM_RECALL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "attempt": self.attempt,
            "failure_class": self.failure_class,
            "error_summary": self.error_summary,
            "specific_errors": list(self.specific_errors),
            "line_numbers": list(self.line_numbers),
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "op_id": self.op_id,
            "symbol_name": self.symbol_name,
            "ast_signature": self.ast_signature,
            "failure_phase": self.failure_phase,
            "failure_reason": self.failure_reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["PostmortemRecord"]:
        """Schema-tolerant reconstruction. Returns ``None`` on
        schema mismatch. NEVER raises."""
        try:
            if (
                payload.get("schema_version")
                != POSTMORTEM_RECALL_SCHEMA_VERSION
            ):
                return None
            return cls(
                file_path=str(payload.get("file_path", "")),
                attempt=int(payload.get("attempt", 0)),
                failure_class=str(payload.get("failure_class", "")),
                error_summary=str(payload.get("error_summary", "")),
                specific_errors=tuple(
                    str(s) for s in (
                        payload.get("specific_errors") or []
                    )
                ),
                line_numbers=tuple(
                    int(n) for n in (
                        payload.get("line_numbers") or []
                    )
                ),
                timestamp=float(payload.get("timestamp", 0.0)),
                session_id=str(payload.get("session_id", "")),
                op_id=str(payload.get("op_id", "")),
                symbol_name=str(payload.get("symbol_name", "")),
                ast_signature=str(
                    payload.get("ast_signature", ""),
                ),
                failure_phase=str(
                    payload.get("failure_phase", ""),
                ),
                failure_reason=str(
                    payload.get("failure_reason", ""),
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def age_days(self, *, now_ts: Optional[float] = None) -> float:
        """Age in days vs the given reference timestamp (default
        wall-clock). Used by Slice 3's renderer for human-readable
        output. NEVER raises (returns 0.0 on error)."""
        try:
            ref = (
                float(now_ts) if now_ts is not None
                else time.time()
            )
            age_s = max(0.0, ref - float(self.timestamp))
            return age_s / 86400.0
        except Exception:  # noqa: BLE001 — defensive
            return 0.0


@dataclass(frozen=True)
class RecallTarget:
    """Bounded query: what files/symbols are we looking up
    postmortems for? Frozen for safe propagation across orchestrator
    hooks.

    Empty ``target_files`` and empty ``target_symbols`` mean "no
    file/symbol filter" — relevance falls to LOW (failure_class
    match only) at best.

    ``target_failure_class=None`` means "match any failure_class"
    (no hard filter); a non-None value applies a hard filter
    (mismatched records → NONE).

    ``target_ast_signature`` enables structural-identity matching:
    when set, records with identical ast_signature are HIGH
    relevance even if file/symbol differ (catches renames)."""

    target_files: frozenset = field(default_factory=frozenset)
    target_symbols: frozenset = field(default_factory=frozenset)
    target_failure_class: Optional[str] = None
    target_ast_signature: Optional[str] = None
    max_age_days: float = 30.0
    schema_version: str = POSTMORTEM_RECALL_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_files": sorted(self.target_files),
            "target_symbols": sorted(self.target_symbols),
            "target_failure_class": self.target_failure_class,
            "target_ast_signature": self.target_ast_signature,
            "max_age_days": self.max_age_days,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class RecallVerdict:
    """Result of one ``recall_postmortems`` call. Frozen for safe
    propagation across CONTEXT_EXPANSION hooks."""

    outcome: RecallOutcome
    records: Tuple[PostmortemRecord, ...] = field(
        default_factory=tuple,
    )
    total_index_size: int = 0
    max_relevance: RelevanceLevel = RelevanceLevel.NONE
    detail: str = ""
    schema_version: str = POSTMORTEM_RECALL_SCHEMA_VERSION

    def has_recall(self) -> bool:
        """True iff outcome is HIT — at least one record met
        threshold and is included in ``records``."""
        return self.outcome is RecallOutcome.HIT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "records": [r.to_dict() for r in self.records],
            "total_index_size": self.total_index_size,
            "max_relevance": self.max_relevance.value,
            "detail": self.detail,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Internal: recency-decay weighting (parity with SemanticIndex)
# ---------------------------------------------------------------------------


def _recency_weight(age_s: float, halflife_d: float) -> float:
    """``0.5 ** (age_days / halflife_days)``. Clamped to [0, 1].
    NEVER raises.

    Literal formula parity with ``semantic_index._recency_weight``
    AND ``coherence_auditor._recency_weight`` — pinned by
    companion test ``test_recency_weight_parity``. Re-implemented
    here (same as Priority #1 Slice 1) so this module stays
    pure-stdlib (zero governance imports) — strongest possible
    authority invariant."""
    try:
        if halflife_d <= 0 or age_s < 0:
            return 1.0
        age_days = age_s / 86400.0
        return 0.5 ** (age_days / halflife_d)
    except Exception:  # noqa: BLE001 — defensive
        return 1.0


# ---------------------------------------------------------------------------
# Public: compute_relevance — pure decision per (record, target) pair
# ---------------------------------------------------------------------------


def compute_relevance(
    record: PostmortemRecord,
    target: RecallTarget,
) -> RelevanceLevel:
    """Pure decision: returns the relevance level for a record
    against a target. Closed 4-value taxonomy. NEVER raises.

    Decision tree:

      1. ``target.target_failure_class`` is non-None AND
         ``record.failure_class`` mismatches → ``NONE`` (hard
         filter — operators pre-filter by failure class).
      2. ``target.target_ast_signature`` is non-None AND
         ``record.ast_signature`` matches exactly → ``HIGH``
         (structural identity — catches renames).
      3. ``record.file_path`` ∈ ``target.target_files`` AND
         ``record.symbol_name`` ∈ ``target.target_symbols`` →
         ``HIGH`` (both overlap).
      4. Either file OR symbol overlaps (one only) → ``MEDIUM``.
      5. ``target.target_failure_class`` is non-None AND
         passed step 1 (i.e., failure_class matched) → ``LOW``
         (failure_class match only).
      6. Otherwise → ``NONE``."""
    try:
        if not isinstance(record, PostmortemRecord):
            return RelevanceLevel.NONE
        if not isinstance(target, RecallTarget):
            return RelevanceLevel.NONE

        # Step 1: failure_class hard filter
        if target.target_failure_class is not None:
            if record.failure_class != target.target_failure_class:
                return RelevanceLevel.NONE

        # Step 2: AST signature exact match → HIGH
        if (
            target.target_ast_signature is not None
            and record.ast_signature
            and record.ast_signature == target.target_ast_signature
        ):
            return RelevanceLevel.HIGH

        # Compute file + symbol overlap
        file_match = bool(
            target.target_files
            and record.file_path
            and record.file_path in target.target_files
        )
        symbol_match = bool(
            target.target_symbols
            and record.symbol_name
            and record.symbol_name in target.target_symbols
        )

        # Step 3: both file + symbol → HIGH
        if file_match and symbol_match:
            return RelevanceLevel.HIGH

        # Step 4: one of file or symbol → MEDIUM
        if file_match or symbol_match:
            return RelevanceLevel.MEDIUM

        # Step 5: failure_class match only → LOW
        if target.target_failure_class is not None:
            return RelevanceLevel.LOW

        # Step 6: nothing overlapped → NONE
        return RelevanceLevel.NONE
    except Exception:  # noqa: BLE001 — defensive
        return RelevanceLevel.NONE


# ---------------------------------------------------------------------------
# Public: recall_postmortems — pure ranking + filtering
# ---------------------------------------------------------------------------


def recall_postmortems(
    records: Iterable[PostmortemRecord],
    target: RecallTarget,
    *,
    max_results: Optional[int] = None,
    threshold: Optional[RelevanceLevel] = None,
    halflife_days_override: Optional[float] = None,
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> RecallVerdict:
    """Pure ranking over an iterable of records. Returns a
    ``RecallVerdict``. NEVER raises.

    Decision tree (every input maps to exactly one verdict):

      1. ``enabled_override`` (test fixture) OR
         ``postmortem_recall_enabled()`` is False → ``DISABLED``.
      2. ``records`` is None / empty → ``EMPTY_INDEX``.
      3. Filter by ``target.max_age_days`` (records older than
         cutoff excluded).
      4. ``compute_relevance`` per record; drop those below
         threshold.
      5. After threshold filter: empty → ``MISS``.
      6. Score = ``_RELEVANCE_RANK[level] * _recency_weight``.
      7. Sort descending, take top-K (clamped to ``recall_top_
         k_ceiling()``), return ``HIT``.

    Threshold defaults to ``recall_relevance_threshold()`` env
    knob. ``max_results`` defaults to ``recall_top_k()``."""
    try:
        # Step 1: master flag
        is_enabled = (
            enabled_override if enabled_override is not None
            else postmortem_recall_enabled()
        )
        if not is_enabled:
            return RecallVerdict(
                outcome=RecallOutcome.DISABLED,
                detail=(
                    "JARVIS_POSTMORTEM_RECALL_ENABLED is false "
                    "(or override) — no recall performed"
                ),
            )

        # Step 2: empty input
        if records is None:
            return RecallVerdict(
                outcome=RecallOutcome.EMPTY_INDEX,
                detail="records argument was None",
            )
        record_list: List[PostmortemRecord] = []
        try:
            for r in records:
                if isinstance(r, PostmortemRecord):
                    record_list.append(r)
        except TypeError:
            return RecallVerdict(
                outcome=RecallOutcome.EMPTY_INDEX,
                detail="records argument was not iterable",
            )
        if not record_list:
            return RecallVerdict(
                outcome=RecallOutcome.EMPTY_INDEX,
                detail="no PostmortemRecord instances in input",
            )

        if not isinstance(target, RecallTarget):
            return RecallVerdict(
                outcome=RecallOutcome.FAILED,
                total_index_size=len(record_list),
                detail="target is not a RecallTarget",
            )

        # Resolve params with env defaults + clamps
        eff_top_k = (
            int(max_results) if max_results is not None
            else recall_top_k()
        )
        # Clamp to absolute ceiling regardless of caller intent
        eff_top_k = min(recall_top_k_ceiling(), max(1, eff_top_k))

        eff_threshold = (
            threshold if threshold is not None
            else recall_relevance_threshold()
        )
        threshold_rank = _RELEVANCE_RANK.get(eff_threshold, 2)

        eff_halflife = (
            float(halflife_days_override)
            if halflife_days_override is not None
            else recall_halflife_days()
        )

        ref_ts = (
            float(now_ts) if now_ts is not None else time.time()
        )
        cutoff_ts = ref_ts - (
            float(target.max_age_days) * 86400.0
        )

        # Step 3 + 4: age filter + relevance compute + threshold
        scored: List[Tuple[float, RelevanceLevel, PostmortemRecord]] = []
        for record in record_list:
            try:
                # Age filter
                if record.timestamp < cutoff_ts:
                    continue
                # Relevance
                level = compute_relevance(record, target)
                if _RELEVANCE_RANK[level] < threshold_rank:
                    continue
                # Recency-weighted score
                age_s = max(0.0, ref_ts - float(record.timestamp))
                recency = _recency_weight(age_s, eff_halflife)
                # Score = relevance_rank × recency. HIGH+recent
                # outranks LOW+recent; HIGH+old outranks
                # MEDIUM+recent only when (3 × old_decay) >
                # (2 × 1.0). For halflife=14d, this means HIGH
                # records up to ~9d old still outrank MEDIUM
                # records at age 0.
                score = float(_RELEVANCE_RANK[level]) * recency
                scored.append((score, level, record))
            except Exception:  # noqa: BLE001 — per-record defensive
                continue

        # Step 5: empty after filtering → MISS
        if not scored:
            return RecallVerdict(
                outcome=RecallOutcome.MISS,
                total_index_size=len(record_list),
                detail=(
                    f"no records met threshold "
                    f"{eff_threshold.value} or age cutoff"
                ),
            )

        # Step 6 + 7: sort descending + clamp + return HIT
        scored.sort(key=lambda triple: -triple[0])
        clamped = scored[:eff_top_k]
        max_rel = clamped[0][1]

        return RecallVerdict(
            outcome=RecallOutcome.HIT,
            records=tuple(r for _s, _l, r in clamped),
            total_index_size=len(record_list),
            max_relevance=max_rel,
            detail=(
                f"{len(clamped)} of {len(record_list)} records "
                f"met threshold {eff_threshold.value}; "
                f"max_relevance={max_rel.value}"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[PostmortemRecall] recall_postmortems raised: %s",
            exc,
        )
        return RecallVerdict(
            outcome=RecallOutcome.FAILED,
            detail=f"recall_postmortems raised: {exc!r}",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "POSTMORTEM_RECALL_SCHEMA_VERSION",
    "PostmortemRecord",
    "RecallOutcome",
    "RecallTarget",
    "RecallVerdict",
    "RelevanceLevel",
    "compute_relevance",
    "postmortem_recall_enabled",
    "recall_halflife_days",
    "recall_max_age_days",
    "recall_postmortems",
    "recall_relevance_threshold",
    "recall_top_k",
    "recall_top_k_ceiling",
]
