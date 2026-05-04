"""Upgrade 3 Slice 1 — Failure-Mode Memory primitive (PRD §31.4).

Cross-op pattern accumulation substrate. Every postmortem will eventually
extract a ``(situation_signature, failure_mode, mitigation)`` triplet
(Slice 2 extractor). A retriever (Slice 3) surfaces matching prior
failures into the **first-attempt GENERATE prompt** via
``StrategicDirection`` injection (Slice 4) — moving recurrence-recall
from retry-context (existing :mod:`postmortem_recall`) to first-attempt.

Without this loop closure, the system relearns the same lesson 100×
per the §31.4 motivation — exactly the non-deterministic degradation
the §32.8 critical path was designed to solve.

This Slice 1 ships the **primitive layer only**:

  * :data:`FAILURE_MODE_MEMORY_SCHEMA_VERSION`
  * :func:`failure_mode_memory_enabled` — master flag
    (``JARVIS_FAILURE_MODE_MEMORY_ENABLED``) — default-FALSE for
    Slice 1; flips to default-TRUE at Slice 5 graduation per §31.4
    sequencing.
  * :class:`SituationKind` — 7-value closed enum (6 from PRD §31.4.2
    spec + ``UNKNOWN`` sentinel for extractor chain-of-responsibility
    fallback, mirroring :class:`FailureModeKind.OTHER`).
  * :class:`FailureModeKind` — 7-value closed enum (PRD §31.4.2 spec).
    ``OTHER`` is the sentinel the Slice 2 extractor returns when
    pattern-match doesn't yield a more specific value.
  * :class:`FailureModeRecord` — frozen dataclass with the canonical
    8-field shape from PRD §31.4.2:
    ``signature_hash, situation_kind, attempted_action_kind,
    failure_mode_kind, mitigation_summary, observed_at_unix, op_id,
    weight``. ``to_dict()`` / ``from_dict()`` round-trip with
    schema-version gate.
  * :func:`compute_signature_hash` — deterministic sha256 hex over
    ``(situation_kind, attempted_action_kind, target_files)`` with
    file-order invariance (sorted before hashing) so the SAME
    situation in two ops produces the SAME signature regardless of
    listing order. The dedup key Slice 2's extractor uses to
    accumulate ``weight`` across recurrences.

Slices 2-5 (NOT in this commit):

  * Slice 2 — :class:`FailureModeExtractor` (post-VERIFY-failed hook;
    pure stdlib + ast pattern-match; persists to flock'd JSONL).
  * Slice 3 — :class:`FailureModeRetriever` (top-K via
    :func:`semantic_index.cosine_score` + 14d recency half-life).
  * Slice 4 — :mod:`strategic_direction` injection at first-attempt
    GENERATE; min-weight=2 gate against memory pollution.
  * Slice 5 — Graduation (default-true), 4 AST pins, 5 FlagRegistry
    seeds, ``/failures`` REPL, ``/observability/failure-modes``,
    SSE ``failure_mode_recalled_at_generate``.

Cost contract (entire 5-slice arc):

  * Zero LLM calls in extractor (pattern-match) OR retriever (RAG).
  * +<= 3KB to GENERATE prompt amortized by Anthropic 5-min cache.
  * ~500B/record x ~20 failures/session x 30d ~= ~300KB disk.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib ONLY. NEVER imports orchestrator / phase_runners
    / candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router / tool_executor
    / change_engine / subagent_scheduler / auto_action_router /
    strategic_direction (Slice 4 reverses this for the injection
    callsite — the dependency direction is strategic_direction ->
    failure_mode_memory, never the reverse).
  * Pure data — never mutates external state, never raises out of
    any public function.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


FAILURE_MODE_MEMORY_SCHEMA_VERSION: str = "failure_mode_memory.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics, default-FALSE for Slice 1
# ---------------------------------------------------------------------------


def failure_mode_memory_enabled() -> bool:
    """``JARVIS_FAILURE_MODE_MEMORY_ENABLED`` (default ``false``
    until Slice 5 graduation per PRD §31.4).

    Asymmetric env semantics — empty/whitespace = unset = current
    default (false for Slice 1); explicit ``1``/``true``/``yes``/
    ``on`` flips on. Same shape as
    :func:`coherence_auditor_enabled` /
    :func:`cigw_enabled` / :func:`quorum_enabled` graduated flags
    so the Slice 5 graduation flip is a one-character edit.

    Re-read on every call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_FAILURE_MODE_MEMORY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 1 default; flips to True at Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# SituationKind — 7-value closed enum (6 PRD-spec + UNKNOWN sentinel)
# ---------------------------------------------------------------------------


class SituationKind(str, enum.Enum):
    """Closed taxonomy of the high-level "what was the op trying to
    do" situation a postmortem belongs to. Mirrors PRD §31.4.2
    canonical 6-value list + ``UNKNOWN`` sentinel for the Slice 2
    extractor's chain-of-responsibility fallback (mirrors
    :attr:`FailureModeKind.OTHER`).

    Closed by construction — extractor branches on the enum, never
    on free-form strings. Adding a new SituationKind requires a
    spec update + extractor pattern; this is intentional friction
    against silent vocabulary drift."""

    MULTI_FILE_REFACTOR = "multi_file_refactor"
    """Operation touches >= 2 source files in a coordinated way
    (the multi-file coordinated generation path)."""

    DB_MIGRATION = "db_migration"
    """Schema or data migration; touches ``migrations/`` or
    matches DDL-shape patterns."""

    ASYNC_RESTRUCTURE = "async_restructure"
    """asyncio refactor — adding/removing ``async``/``await``,
    converting between sync and async APIs, restructuring event
    loops or tasks."""

    NEW_TEST_FRAMEWORK_INTEGRATION = "new_test_framework_integration"
    """Adding a new pytest/unittest framework (fixtures, plugins,
    conftest scaffolding) — distinct from in-place test edits."""

    API_VERSION_BUMP = "api_version_bump"
    """Upgrading a major dependency or API version — semver-major
    or vendored-API breaking change."""

    CROSS_REPO_DRIFT_FIX = "cross_repo_drift_fix"
    """Realigning a contract or signature that drifted between
    sibling repos (CrossRepoDrift sensor lineage)."""

    UNKNOWN = "unknown"
    """Slice 2 extractor sentinel — pattern-match yielded no
    closed-enum match. Records with ``situation_kind=UNKNOWN`` are
    intentionally NOT eligible for first-attempt injection (Slice
    4 gates them out) but remain queryable for retry-context recall
    via the existing :mod:`postmortem_recall` path."""


# ---------------------------------------------------------------------------
# FailureModeKind — 7-value closed enum (PRD §31.4.2 spec)
# ---------------------------------------------------------------------------


class FailureModeKind(str, enum.Enum):
    """Closed taxonomy of *how* the operation failed structurally.
    Mirrors PRD §31.4.2 canonical 7-value list. ``OTHER`` is the
    Slice 2 extractor's chain-of-responsibility fallback (when no
    more specific pattern matches)."""

    MISSING_IMPORT = "missing_import"
    """Patch references a name that has no corresponding import in
    the touched file(s) — caught by SemanticGuardian's
    ``removed_import_still_referenced`` pattern OR by a real
    ``ImportError`` at VERIFY."""

    TYPE_MISMATCH = "type_mismatch"
    """Type-check or runtime ``TypeError`` regression — typing
    Protocol mismatch, return-type drift, dataclass field shape."""

    ASSERT_INVERTED = "assert_inverted"
    """Test assertion was negated/inverted — caught by
    SemanticGuardian's ``test_assertion_inverted`` pattern."""

    CIRCULAR_DEP_INTRODUCED = "circular_dep_introduced"
    """A new import edge created a cycle in the module DAG — caught
    by ``ImportError`` (circular) at VERIFY or by static analysis."""

    BANNED_TOKEN_INTRODUCED = "banned_token_introduced"
    """The patch introduced a token on the ASCII-strict gate +
    Iron Gate banned-token list (e.g. dynamic-execution shells,
    arbitrary-shell invocation, raw-eval primitives). The exact
    banned-token vocabulary lives in :mod:`ascii_strict_gate` /
    :mod:`iron_gate`; this enum value just classifies that the
    failure mode WAS that pattern."""

    TEST_TIMEOUT_REGRESSED = "test_timeout_regressed"
    """A previously-passing test now exceeds the timeout (the
    ``JARVIS_TEST_TIMEOUT_S=30`` floor by default)."""

    OTHER = "other"
    """Sentinel for Slice 2 extractor's chain-of-responsibility
    fallback. Records with ``failure_mode_kind=OTHER`` are
    bookkept for visibility but carry weak signal — Slice 4's
    injection block deprioritizes them via diversity dedup."""


# ---------------------------------------------------------------------------
# Frozen FailureModeRecord — schema with to_dict / from_dict round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureModeRecord:
    """One ``(situation, attempt, failure_mode, mitigation)`` triplet
    extracted post-VERIFY-failed by the Slice 2 extractor. Frozen for
    safe propagation across async + lock boundaries.

    8-field shape from PRD §31.4.2. The ``signature_hash`` is the
    dedup key — multiple postmortems matching the same signature
    increment ``weight`` rather than appending duplicate records
    (Slice 2 extractor enforces this).

    ``weight`` semantics: count of distinct postmortems the extractor
    has merged into this record within the 30-day decay window. Slice
    4's first-attempt injection requires ``weight >= 2`` (PRD §31.4.2
    Slice 4 gate against memory pollution from one-off failures).
    """

    signature_hash: str
    """sha256 hex of ``(situation_kind, attempted_action_kind,
    canonicalized target_files)`` — produced by
    :func:`compute_signature_hash`. Stable across ops + sessions."""

    situation_kind: SituationKind
    """High-level "what was the op trying to do" classification."""

    attempted_action_kind: str
    """Short free-form tag for the *attempt* shape (e.g.
    ``add_dataclass``, ``rename_function``, ``inline_constant``).
    Free-form rather than enum because attempt vocabulary is
    open-set; closed taxonomy lives at situation + failure-mode
    level (the dimensions that matter for retrieval)."""

    failure_mode_kind: FailureModeKind
    """Structural failure classification."""

    mitigation_summary: str
    """Short operator-readable string describing what to try
    *instead* — Slice 4 injects this verbatim into the GENERATE
    prompt's ``## Prior Failure Modes for This Situation`` block."""

    observed_at_unix: float
    """Unix ts of the original POSTMORTEM emission. Slice 3
    retriever weights records by recency with 14d half-life
    (mirrors :mod:`semantic_index` commit half-life)."""

    op_id: str
    """Originating ``op_id`` — surfaced for traceability into the
    causality DAG (Priority #2)."""

    weight: int = 1
    """Recurrence count. Initial 1; Slice 2 extractor increments
    when a new postmortem's signature matches an existing record
    within the 30d window. Slice 4 gates first-attempt injection
    on ``weight >= 2``."""

    schema_version: str = field(
        default=FAILURE_MODE_MEMORY_SCHEMA_VERSION,
    )

    # ---- Serialization -------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Render to JSON-friendly dict. Stable key order via
        explicit construction (NOT dataclasses.asdict — that would
        leak field-iteration-order coupling)."""
        return {
            "signature_hash": self.signature_hash,
            "situation_kind": self.situation_kind.value,
            "attempted_action_kind": self.attempted_action_kind,
            "failure_mode_kind": self.failure_mode_kind.value,
            "mitigation_summary": self.mitigation_summary,
            "observed_at_unix": float(self.observed_at_unix),
            "op_id": self.op_id,
            "weight": int(self.weight),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Any,
    ) -> Optional["FailureModeRecord"]:
        """Reconstruct from a :meth:`to_dict` payload. Returns
        ``None`` on schema mismatch / missing required fields /
        unknown enum values. NEVER raises.

        Schema-version gate: payloads with a different
        ``schema_version`` are rejected silently (caller treats
        unparseable lines as corrupt, mirrors :mod:`postmortem_recall`
        and other graduated arcs)."""
        if not isinstance(payload, dict):
            return None
        try:
            if (
                payload.get("schema_version")
                != FAILURE_MODE_MEMORY_SCHEMA_VERSION
            ):
                return None
            sk = _situation_kind_from_value(
                payload.get("situation_kind"),
            )
            fk = _failure_mode_kind_from_value(
                payload.get("failure_mode_kind"),
            )
            if sk is None or fk is None:
                return None
            sig = payload.get("signature_hash")
            if not isinstance(sig, str) or not sig:
                return None
            return cls(
                signature_hash=sig,
                situation_kind=sk,
                attempted_action_kind=str(
                    payload.get("attempted_action_kind", ""),
                ),
                failure_mode_kind=fk,
                mitigation_summary=str(
                    payload.get("mitigation_summary", ""),
                ),
                observed_at_unix=float(
                    payload.get("observed_at_unix", 0.0),
                ),
                op_id=str(payload.get("op_id", "")),
                weight=int(payload.get("weight", 1)),
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[failure_mode_memory] from_dict swallowed: %s",
                exc,
            )
            return None


# ---------------------------------------------------------------------------
# Enum lookup helpers — defensive value->member mapping
# ---------------------------------------------------------------------------


def _situation_kind_from_value(
    value: Any,
) -> Optional[SituationKind]:
    """Map a string to :class:`SituationKind` member; ``None`` on
    miss. NEVER raises."""
    if value is None:
        return None
    try:
        token = str(value).strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not token:
        return None
    for member in SituationKind:
        if member.value == token:
            return member
    return None


def _failure_mode_kind_from_value(
    value: Any,
) -> Optional[FailureModeKind]:
    """Map a string to :class:`FailureModeKind` member; ``None`` on
    miss. NEVER raises."""
    if value is None:
        return None
    try:
        token = str(value).strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not token:
        return None
    for member in FailureModeKind:
        if member.value == token:
            return member
    return None


# ---------------------------------------------------------------------------
# Signature hashing — deterministic, file-order-invariant, stdlib only
# ---------------------------------------------------------------------------


def _canonicalize_target_files(
    target_files: Iterable[str],
) -> Tuple[str, ...]:
    """Normalize a target-files iterable into a sorted tuple of
    cleaned strings. File-order invariance is load-bearing: two
    ops touching the same files in different listing order MUST
    produce the same signature. NEVER raises."""
    cleaned: list = []
    try:
        for raw in target_files:
            if raw is None:
                continue
            try:
                s = str(raw).strip()
            except Exception:  # noqa: BLE001 — defensive
                continue
            if not s:
                continue
            cleaned.append(s)
    except TypeError:
        # Caller passed something non-iterable.
        return tuple()
    cleaned.sort()
    return tuple(cleaned)


def compute_signature_hash(
    *,
    situation_kind: SituationKind,
    attempted_action_kind: str,
    target_files: Iterable[str] = (),
) -> str:
    """Deterministic sha256 hex over the dedup-keyed dimensions of a
    failure record.

    Inputs are joined with ``\\x00`` separators (cannot collide with
    any path or token character). File listing is canonicalized via
    :func:`_canonicalize_target_files` so order doesn't matter.

    Returns full 64-char sha256 hex. NEVER raises — falls back to
    the empty-input hash (``e3b0c44...``) on type errors so callers
    always get a string they can store and key on."""
    try:
        sk = (
            situation_kind.value
            if isinstance(situation_kind, SituationKind)
            else str(situation_kind or "")
        ).strip().lower()
        ak = str(attempted_action_kind or "").strip().lower()
        files = _canonicalize_target_files(target_files)
        payload = "\x00".join(
            ("sk=" + sk, "ak=" + ak, "files=" + ",".join(files)),
        )
        return hashlib.sha256(
            payload.encode("utf-8", errors="replace"),
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[failure_mode_memory] compute_signature_hash "
            "swallowed: %s", exc,
        )
        # The well-known sha256 of the empty string — stable,
        # never-raising fallback.
        return hashlib.sha256(b"").hexdigest()


__all__ = [
    "FAILURE_MODE_MEMORY_SCHEMA_VERSION",
    "FailureModeKind",
    "FailureModeRecord",
    "SituationKind",
    "compute_signature_hash",
    "failure_mode_memory_enabled",
]
