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


# ===========================================================================
# Slice 2 — FailureModeExtractor + persistence
#
# Post-VERIFY-failed hook that reads:
#   * POSTMORTEM evidence_records (root_cause + failed_phase +
#     next_safe_action + target_files; duck-typed — accepts the
#     :class:`postmortem_recall.PostmortemRecord` shape OR an
#     equivalent dict)
#   * ctx.implementation_plan (schema "plan.1" from
#     :mod:`plan_generator`; optional)
#   * diff text (concatenated patch hunks; optional)
# -> derives a :class:`FailureModeRecord` via chain-of-responsibility
# pattern matchers. Pure stdlib + ``ast`` — zero LLM calls.
#
# Persistence layer is dedup-aware: records sharing a
# :func:`compute_signature_hash` within ``dedup_window_days()`` are
# merged (weight++, observed_at_unix updated to most recent) rather
# than appended. This is the ``min weight=2`` mechanic from PRD
# §31.4.6 (memory pollution defense).
# ===========================================================================


import json
import re
import time
from pathlib import Path

# Reuse the same flock primitive Slice C ships for the Quorum
# observer + Move 4 InvariantDriftStore. Same authority floor — no
# duplication.
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)


# ---------------------------------------------------------------------------
# Persistence env knobs — clamping discipline mirrors Slice C
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, floor: int, ceiling: int,
) -> int:
    """Bounded integer env-knob read. NEVER raises."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        if n < floor:
            return floor
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def history_dir() -> Path:
    """``JARVIS_FAILURE_MODE_HISTORY_DIR`` — default
    ``.jarvis/failure_mode_memory``. Same root convention as Move 4
    InvariantDriftStore + Move 6 Quorum + Coherence."""
    raw = os.environ.get(
        "JARVIS_FAILURE_MODE_HISTORY_DIR",
        ".jarvis/failure_mode_memory",
    ).strip()
    return Path(raw or ".jarvis/failure_mode_memory")


def history_path() -> Path:
    """``<history_dir()>/failure_modes.jsonl``."""
    return history_dir() / "failure_modes.jsonl"


def history_max_records() -> int:
    """``JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS`` — bounded ring-
    buffer cap. Default 5000 (~300KB at 60B/record per PRD §31.4.3
    cost contract). Clamped [50, 100000]."""
    return _read_int_knob(
        "JARVIS_FAILURE_MODE_HISTORY_MAX_RECORDS",
        5000, 50, 100_000,
    )


def dedup_window_days() -> int:
    """``JARVIS_FAILURE_MODE_DEDUP_WINDOW_DAYS`` — recurrence dedup
    window. Default 30 (PRD §31.4.6). Records sharing a signature
    within this window are merged (weight++) instead of appended.
    Clamped [1, 365]."""
    return _read_int_knob(
        "JARVIS_FAILURE_MODE_DEDUP_WINDOW_DAYS",
        30, 1, 365,
    )


# ---------------------------------------------------------------------------
# Closed taxonomies for extractor + persistence outcomes
# ---------------------------------------------------------------------------


class ExtractionOutcome(str, enum.Enum):
    """Closed taxonomy for :func:`extract_failure_mode` results.

    Caller branches on the enum, never on free-form fields. Used
    by Slice 5 telemetry to emit per-class extraction metrics."""

    OK = "ok"
    """Both situation_kind and failure_mode_kind classified to
    closed-enum members; record built."""

    OK_PARTIAL = "ok_partial"
    """Record built but at least one classifier fell through to
    a sentinel (UNKNOWN / OTHER). Slice 4's first-attempt
    injection gates these out via the min-weight rule."""

    DISABLED = "disabled"
    """Master flag is off — no extraction attempted."""

    REJECTED = "rejected"
    """Garbage input (None postmortem, no root_cause, etc.)."""


class RecordOutcome(str, enum.Enum):
    """Closed taxonomy for :func:`record_failure_mode` persistence
    results. Mirrors the SBT / CIGW / Quorum observer pattern."""

    OK_NEW = "ok_new"
    """New signature appended."""

    OK_DEDUPED = "ok_deduped"
    """Existing signature within dedup window — weight++ merge."""

    DISABLED = "disabled"
    """Master flag is off."""

    REJECTED = "rejected"
    """Garbage input (non-FailureModeRecord)."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append / read."""

    SERIALIZE_ERROR = "serialize_error"
    """Record's :meth:`to_dict` produced non-JSON-serializable."""


# ---------------------------------------------------------------------------
# PRD §31.4.2 Slice 2 — Pattern matchers (chain-of-responsibility)
#
# Each classifier is a pure function returning Optional[Enum]; None
# signals "no match — pass to next classifier in chain". Final
# classifier in chain returns the sentinel (UNKNOWN / OTHER).
#
# Patterns are derived empirically from real postmortem signatures
# observed in production debug.log lines and from SemanticGuardian's
# documented diagnostic vocabulary. No hardcoded literal mapping
# table — each is a regex-against-real-evidence.
# ---------------------------------------------------------------------------


# SituationKind classifiers --------------------------------------------------


_RE_DB_MIGRATION_PATH = re.compile(
    r"(^|/)migrations?/", re.IGNORECASE,
)
_RE_DB_MIGRATION_DDL = re.compile(
    r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|COLUMN|CONSTRAINT)\b",
    re.IGNORECASE,
)
_RE_ASYNC_TOKEN = re.compile(
    r"\b(async\s+def|await\s+|asyncio\.|run_until_complete|"
    r"create_task|gather)\b",
)
_RE_TEST_FRAMEWORK = re.compile(
    r"(^|/)conftest\.py$|@pytest\.fixture|"
    r"\bunittest\.TestCase\b|\bpytest\.mark\.",
)
_RE_API_VERSION_BUMP = re.compile(
    r"\bversion\s*=\s*['\"]?\d+\.\d+|"
    r"\b__version__\s*=|"
    r"\bsetup\.cfg|\bpyproject\.toml|\brequirements.*\.txt",
)


def _classify_db_migration(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Match if any target file lives under ``migrations/`` OR diff
    contains DDL statements OR plan approach mentions migration."""
    for f in target_files:
        if _RE_DB_MIGRATION_PATH.search(f):
            return SituationKind.DB_MIGRATION
    if _RE_DB_MIGRATION_DDL.search(diff or ""):
        return SituationKind.DB_MIGRATION
    if "migration" in (plan_text or "").lower():
        return SituationKind.DB_MIGRATION
    return None


def _classify_async_restructure(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Match if diff has substantive async-keyword changes (>=3
    occurrences — rules out incidental mentions) or plan explicitly
    targets async."""
    if diff:
        if len(_RE_ASYNC_TOKEN.findall(diff)) >= 3:
            return SituationKind.ASYNC_RESTRUCTURE
    if plan_text:
        lower = plan_text.lower()
        if "async" in lower and (
            "restructure" in lower
            or "migrate" in lower
            or "convert" in lower
        ):
            return SituationKind.ASYNC_RESTRUCTURE
    return None


def _classify_test_framework_integration(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Match if a NEW conftest.py is being added OR pytest fixture
    decorators introduced in an unrelated location."""
    has_conftest_new = any(
        f.endswith("conftest.py") for f in target_files
    )
    if has_conftest_new and "+++ " in (diff or ""):
        # New file shape — diff has +++ marker for additions
        if "@pytest.fixture" in (diff or ""):
            return SituationKind.NEW_TEST_FRAMEWORK_INTEGRATION
    if _RE_TEST_FRAMEWORK.search(diff or ""):
        if "framework" in (plan_text or "").lower():
            return SituationKind.NEW_TEST_FRAMEWORK_INTEGRATION
    return None


def _classify_api_version_bump(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Match if version-related files / tokens dominate the diff."""
    files_str = " ".join(target_files)
    if (
        "pyproject.toml" in files_str
        or "setup.cfg" in files_str
        or "requirements" in files_str
    ):
        if _RE_API_VERSION_BUMP.search(diff or ""):
            return SituationKind.API_VERSION_BUMP
    if plan_text:
        lower = plan_text.lower()
        if (
            "version bump" in lower
            or "upgrade" in lower
            or "semver" in lower
        ):
            return SituationKind.API_VERSION_BUMP
    return None


def _classify_cross_repo_drift_fix(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Match if plan mentions cross-repo drift / sibling repo
    reconciliation, OR diff path traverses ``..`` (cross-repo)."""
    if plan_text:
        lower = plan_text.lower()
        if (
            "cross-repo" in lower
            or "cross repo" in lower
            or "sibling repo" in lower
            or "drift" in lower and "repo" in lower
        ):
            return SituationKind.CROSS_REPO_DRIFT_FIX
    for f in target_files:
        if "../" in f:
            return SituationKind.CROSS_REPO_DRIFT_FIX
    return None


def _classify_multi_file_refactor(
    *, target_files: Tuple[str, ...], diff: str, plan_text: str,
) -> Optional[SituationKind]:
    """Lowest-priority structural classifier. Match if >=2 source
    files touched. Runs LAST among situation classifiers because
    DB_MIGRATION / ASYNC_RESTRUCTURE / etc. are more specific
    (a multi-file migration should classify as DB_MIGRATION, not
    MULTI_FILE_REFACTOR)."""
    py_files = [
        f for f in target_files
        if f.endswith(".py") or f.endswith(".pyi")
    ]
    if len(py_files) >= 2:
        return SituationKind.MULTI_FILE_REFACTOR
    return None


# Order is load-bearing — first match wins. Specific BEFORE general.
_SITUATION_CLASSIFIERS = (
    _classify_db_migration,
    _classify_async_restructure,
    _classify_test_framework_integration,
    _classify_api_version_bump,
    _classify_cross_repo_drift_fix,
    _classify_multi_file_refactor,
)


def _classify_situation(
    *,
    target_files: Tuple[str, ...],
    diff: str,
    plan_text: str,
) -> SituationKind:
    """Walk the chain; first non-None match wins. Returns
    :attr:`SituationKind.UNKNOWN` if none matched. NEVER raises."""
    for fn in _SITUATION_CLASSIFIERS:
        try:
            result = fn(
                target_files=target_files,
                diff=diff,
                plan_text=plan_text,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[failure_mode_memory] situation classifier %s "
                "raised: %s", fn.__name__, exc,
            )
            continue
        if result is not None:
            return result
    return SituationKind.UNKNOWN


# FailureModeKind classifiers ------------------------------------------------


_RE_MISSING_IMPORT = re.compile(
    r"\b(ImportError|ModuleNotFoundError|"
    r"NameError.*not defined|undefined name|"
    r"removed_import_still_referenced)\b",
    re.IGNORECASE,
)
_RE_TYPE_MISMATCH = re.compile(
    r"\b(TypeError|"
    r"argument.*has incompatible type|"
    r"incompatible types|"
    r"return-type.*not assignable|"
    r"function_body_collapsed)\b",
    re.IGNORECASE,
)
_RE_ASSERT_INVERTED = re.compile(
    r"\b(test_assertion_inverted|"
    r"assert.*inverted|"
    r"assertion.*flipped)\b",
    re.IGNORECASE,
)
_RE_CIRCULAR_DEP = re.compile(
    r"\b(circular import|circular dep|"
    r"cyclic import|import cycle|"
    r"partially initialized module)\b",
    re.IGNORECASE,
)
_RE_BANNED_TOKEN = re.compile(
    r"\b(banned[_ ]token|"
    r"ASCII[_ ]strict[_ ]gate|"
    r"iron[_ ]gate.*reject|"
    r"forbidden token)\b",
    re.IGNORECASE,
)
_RE_TEST_TIMEOUT = re.compile(
    r"\b(pytest.*timeout|"
    r"timed?[_ ]out.*after|"
    r"test.*exceeded.*timeout|"
    r"TimeoutError)\b",
    re.IGNORECASE,
)


def _classify_circular_dep(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    """Circular-dep BEFORE missing-import (a circular import raises
    ImportError; we want the more specific classification first)."""
    if _RE_CIRCULAR_DEP.search(root_cause or ""):
        return FailureModeKind.CIRCULAR_DEP_INTRODUCED
    return None


def _classify_missing_import(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    if _RE_MISSING_IMPORT.search(root_cause or ""):
        return FailureModeKind.MISSING_IMPORT
    return None


def _classify_type_mismatch(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    if _RE_TYPE_MISMATCH.search(root_cause or ""):
        return FailureModeKind.TYPE_MISMATCH
    return None


def _classify_assert_inverted(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    if _RE_ASSERT_INVERTED.search(root_cause or ""):
        return FailureModeKind.ASSERT_INVERTED
    return None


def _classify_banned_token(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    if _RE_BANNED_TOKEN.search(root_cause or ""):
        return FailureModeKind.BANNED_TOKEN_INTRODUCED
    return None


def _classify_test_timeout(*, root_cause: str) -> Optional[
    FailureModeKind
]:
    if _RE_TEST_TIMEOUT.search(root_cause or ""):
        return FailureModeKind.TEST_TIMEOUT_REGRESSED
    return None


# Order is load-bearing — circular before missing-import (more
# specific); banned-token before type-mismatch (Iron Gate banned
# tokens often manifest as TypeError). First match wins.
_FAILURE_MODE_CLASSIFIERS = (
    _classify_circular_dep,
    _classify_banned_token,
    _classify_assert_inverted,
    _classify_test_timeout,
    _classify_missing_import,
    _classify_type_mismatch,
)


def _classify_failure_mode(*, root_cause: str) -> FailureModeKind:
    """Walk the chain; first non-None match wins. Returns
    :attr:`FailureModeKind.OTHER` if none matched. NEVER raises."""
    for fn in _FAILURE_MODE_CLASSIFIERS:
        try:
            result = fn(root_cause=root_cause)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[failure_mode_memory] failure-mode classifier %s "
                "raised: %s", fn.__name__, exc,
            )
            continue
        if result is not None:
            return result
    return FailureModeKind.OTHER


# ---------------------------------------------------------------------------
# Mitigation derivation — short operator-readable hints per kind
# ---------------------------------------------------------------------------


_MITIGATION_TEMPLATES: Dict[FailureModeKind, str] = {
    FailureModeKind.MISSING_IMPORT: (
        "Verify every referenced name has an import in the "
        "touched file(s). Run a grep for the symbol before "
        "patching."
    ),
    FailureModeKind.TYPE_MISMATCH: (
        "Read the touched function's signature + return type "
        "before editing. Match exact dataclass field shape; "
        "preserve Optional/Union/Protocol types."
    ),
    FailureModeKind.ASSERT_INVERTED: (
        "DO NOT negate assertion polarity. The original "
        "expectation is correct; fix the implementation, not "
        "the test."
    ),
    FailureModeKind.CIRCULAR_DEP_INTRODUCED: (
        "Move shared types to a lower-level module. Avoid "
        "importing siblings from the same package layer."
    ),
    FailureModeKind.BANNED_TOKEN_INTRODUCED: (
        "Use the established async / subprocess-free path. "
        "Iron Gate banned this pattern for a reason — find "
        "the existing helper rather than re-introducing the "
        "primitive."
    ),
    FailureModeKind.TEST_TIMEOUT_REGRESSED: (
        "Profile the slow path before patching. Look for "
        "added blocking I/O, missing async, or unbounded "
        "loops introduced by the candidate."
    ),
    FailureModeKind.OTHER: (
        "Read the previous postmortem's root_cause carefully "
        "before retrying."
    ),
}


def _derive_mitigation(
    failure_mode: FailureModeKind,
    *,
    next_safe_action: str = "",
) -> str:
    """Build the mitigation string. Combines the per-kind template
    with the postmortem's ``next_safe_action`` when present (latter
    is operator/system-prescribed; takes precedence as the suffix)."""
    base = _MITIGATION_TEMPLATES.get(
        failure_mode, _MITIGATION_TEMPLATES[FailureModeKind.OTHER],
    )
    nsa = (next_safe_action or "").strip()
    if nsa and nsa.lower() != "none":
        return f"{base} Next-safe-action: {nsa}"
    return base


# ---------------------------------------------------------------------------
# Attempt-kind extraction — short free-form tag from plan + diff
# ---------------------------------------------------------------------------


_RE_DEF_ADDED = re.compile(r"^\+\s*def\s+(\w+)", re.MULTILINE)
_RE_CLASS_ADDED = re.compile(
    r"^\+\s*class\s+(\w+)", re.MULTILINE,
)
_RE_DATACLASS_DECORATOR = re.compile(
    r"^\+\s*@dataclass", re.MULTILINE,
)
_RE_ASYNC_DEF_ADDED = re.compile(
    r"^\+\s*async\s+def\s+(\w+)", re.MULTILINE,
)


def _extract_attempt_kind(
    *, plan_text: str, diff: str,
) -> str:
    """Best-effort short tag for the attempt shape. Falls back to
    ``"unspecified"`` when nothing matches. Pure pattern-match —
    no LLM. Returns lowercase short string."""
    diff = diff or ""
    plan_lower = (plan_text or "").lower()

    # Most specific first
    if _RE_DATACLASS_DECORATOR.search(diff):
        return "add_dataclass"
    if _RE_ASYNC_DEF_ADDED.search(diff):
        return "add_async_function"
    if _RE_CLASS_ADDED.search(diff):
        return "add_class"
    if _RE_DEF_ADDED.search(diff):
        return "add_function"

    # Plan-text hints (less specific)
    for token, tag in (
        ("rename", "rename_symbol"),
        ("inline", "inline_constant"),
        ("extract", "extract_method"),
        ("refactor", "refactor"),
        ("migration", "migration_step"),
        ("upgrade", "upgrade_dependency"),
        ("fix", "fix_bug"),
    ):
        if token in plan_lower:
            return tag

    return "unspecified"


# ---------------------------------------------------------------------------
# Postmortem duck-typing — accept PostmortemRecord OR equivalent dict
# ---------------------------------------------------------------------------


def _postmortem_field(postmortem: Any, field: str, default: Any = "") -> Any:  # noqa: E501
    """Read a postmortem field via getattr (object) OR .get (dict).
    NEVER raises."""
    try:
        if hasattr(postmortem, field):
            return getattr(postmortem, field)
        if isinstance(postmortem, dict):
            return postmortem.get(field, default)
    except Exception:  # noqa: BLE001 — defensive
        pass
    return default


def _plan_text_for_classification(plan: Any) -> str:
    """Flatten the structured plan dict (schema "plan.1") into a
    text blob for substring matching. Reads ``approach`` +
    ``risk_factors`` + each change's ``rationale``. Defensive
    against missing keys / wrong types. NEVER raises."""
    if plan is None:
        return ""
    parts: list = []
    try:
        if isinstance(plan, dict):
            approach = plan.get("approach", "")
            if isinstance(approach, str):
                parts.append(approach)
            risks = plan.get("risk_factors", [])
            if isinstance(risks, list):
                for r in risks:
                    if isinstance(r, str):
                        parts.append(r)
            changes = plan.get("changes", [])
            if isinstance(changes, list):
                for c in changes:
                    if isinstance(c, dict):
                        rat = c.get("rationale", "")
                        if isinstance(rat, str):
                            parts.append(rat)
    except Exception:  # noqa: BLE001 — defensive
        pass
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public extractor — composes classifiers + mitigation + attempt-kind
# ---------------------------------------------------------------------------


def extract_failure_mode(
    postmortem: Any,
    *,
    plan: Optional[Any] = None,
    diff: str = "",
    enabled_override: Optional[bool] = None,
) -> Tuple[ExtractionOutcome, Optional[FailureModeRecord]]:
    """Extract a :class:`FailureModeRecord` from a POSTMORTEM
    payload + optional plan + optional diff.

    Decision tree:

      1. ``enabled_override`` (test fixture) OR
         :func:`failure_mode_memory_enabled` is False → DISABLED.
      2. ``postmortem`` lacks both ``op_id`` and ``root_cause`` →
         REJECTED (no signal worth recording).
      3. Run situation classifiers (chain-of-responsibility);
         result UNKNOWN counts as partial.
      4. Run failure-mode classifiers; result OTHER counts as
         partial.
      5. Derive attempt_kind from plan + diff (defaults to
         ``"unspecified"``).
      6. Derive mitigation from kind + ``next_safe_action``.
      7. Compute signature_hash.
      8. Build :class:`FailureModeRecord` with weight=1 (Slice 2's
         persistence layer increments on signature match).

    NEVER raises. All failures map to closed
    :class:`ExtractionOutcome` values."""
    try:
        if enabled_override is False:
            return (ExtractionOutcome.DISABLED, None)
        if enabled_override is None:
            if not failure_mode_memory_enabled():
                return (ExtractionOutcome.DISABLED, None)

        if postmortem is None:
            return (ExtractionOutcome.REJECTED, None)

        op_id = str(_postmortem_field(postmortem, "op_id", ""))
        root_cause = str(
            _postmortem_field(postmortem, "root_cause", ""),
        )
        if not op_id and not root_cause:
            return (ExtractionOutcome.REJECTED, None)

        target_files_raw = _postmortem_field(
            postmortem, "target_files", (),
        )
        if isinstance(target_files_raw, (list, tuple)):
            target_files = tuple(
                str(t) for t in target_files_raw if t
            )
        else:
            target_files = ()

        next_safe_action = str(
            _postmortem_field(postmortem, "next_safe_action", ""),
        )
        observed_at = float(
            _postmortem_field(
                postmortem, "timestamp_unix",
                _postmortem_field(
                    postmortem, "observed_at_unix", time.time(),
                ),
            ) or time.time()
        )

        plan_text = _plan_text_for_classification(plan)

        situation = _classify_situation(
            target_files=target_files,
            diff=diff,
            plan_text=plan_text,
        )
        mode = _classify_failure_mode(root_cause=root_cause)
        attempt = _extract_attempt_kind(
            plan_text=plan_text, diff=diff,
        )
        mitigation = _derive_mitigation(
            mode, next_safe_action=next_safe_action,
        )
        sig = compute_signature_hash(
            situation_kind=situation,
            attempted_action_kind=attempt,
            target_files=target_files,
        )

        record = FailureModeRecord(
            signature_hash=sig,
            situation_kind=situation,
            attempted_action_kind=attempt,
            failure_mode_kind=mode,
            mitigation_summary=mitigation,
            observed_at_unix=observed_at,
            op_id=op_id,
            weight=1,
        )

        is_partial = (
            situation is SituationKind.UNKNOWN
            or mode is FailureModeKind.OTHER
        )
        outcome = (
            ExtractionOutcome.OK_PARTIAL
            if is_partial
            else ExtractionOutcome.OK
        )
        return (outcome, record)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[failure_mode_memory] extract_failure_mode raised: %s",
            exc,
        )
        return (ExtractionOutcome.REJECTED, None)


# ---------------------------------------------------------------------------
# Persistence — dedup-aware flock'd JSONL store
# ---------------------------------------------------------------------------


def _serialize_record(record: FailureModeRecord) -> Optional[str]:
    """Render record as one JSONL line. NEVER raises."""
    try:
        return json.dumps(
            record.to_dict(), sort_keys=True, ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[failure_mode_memory] serialize: %s", exc,
        )
        return None


def _read_existing_records(
    path: Path,
) -> Tuple[FailureModeRecord, ...]:
    """Defensively read all records from the JSONL store. Corrupt
    lines silently dropped. NEVER raises."""
    if not path.exists():
        return ()
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except OSError:
        return ()
    out: list = []
    for raw in lines:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        rec = FailureModeRecord.from_dict(payload)
        if rec is not None:
            out.append(rec)
    return tuple(out)


def _within_dedup_window(
    candidate_ts: float, existing_ts: float, window_days: int,
) -> bool:
    """True iff the two timestamps are within ``window_days``
    days of each other."""
    if window_days <= 0:
        return False
    delta = abs(candidate_ts - existing_ts)
    return delta <= float(window_days) * 86400.0


def record_failure_mode(
    record: FailureModeRecord,
    *,
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> RecordOutcome:
    """Persist a :class:`FailureModeRecord` to the bounded JSONL
    store with dedup-aware merge.

    Decision tree:

      1. Flag check — ``enabled_override`` OR master flag.
      2. Type check — must be a :class:`FailureModeRecord`.
      3. Acquire flock'd critical section on the store path.
      4. Read existing records.
      5. Find any record sharing the same ``signature_hash`` AND
         within :func:`dedup_window_days` of the candidate.
         * If found: replace with merged record (weight++,
           observed_at_unix = max).
         * If not: append candidate verbatim.
      6. Truncate to :func:`history_max_records` ring-buffer cap.
      7. Atomic-write the truncated payload back.

    Cross-process safe via the :mod:`cross_process_jsonl` flock
    helper Slice C established. NEVER raises."""
    try:
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not failure_mode_memory_enabled():
                return RecordOutcome.DISABLED

        if not isinstance(record, FailureModeRecord):
            return RecordOutcome.REJECTED

        line = _serialize_record(record)
        if line is None:
            return RecordOutcome.SERIALIZE_ERROR

        path = history_path()
        # Slice 5 may surface ``now_ts`` for ledger telemetry; today
        # the dedup decision uses ``record.observed_at_unix`` (the
        # canonical postmortem timestamp) so ``now_ts`` is consumed
        # only when the caller passes it for synthetic-time tests.
        _now_ts = now_ts if now_ts is not None else time.time()
        del _now_ts  # explicitly unused in Slice 2; reserved for Slice 5
        window = dedup_window_days()
        cap = history_max_records()

        with flock_critical_section(path) as acquired:
            if not acquired:
                # Best-effort fallback — fire a non-locked append
                # rather than dropping the record entirely.
                ok = flock_append_line(path, line)
                return (
                    RecordOutcome.OK_NEW if ok
                    else RecordOutcome.PERSIST_ERROR
                )
            existing = _read_existing_records(path)
            merged_records: list = []
            deduped = False
            for old in existing:
                if (
                    old.signature_hash == record.signature_hash
                    and _within_dedup_window(
                        record.observed_at_unix,
                        old.observed_at_unix,
                        window,
                    )
                ):
                    if not deduped:
                        merged_records.append(
                            FailureModeRecord(
                                signature_hash=old.signature_hash,
                                situation_kind=old.situation_kind,
                                attempted_action_kind=(
                                    old.attempted_action_kind
                                ),
                                failure_mode_kind=(
                                    old.failure_mode_kind
                                ),
                                mitigation_summary=(
                                    record.mitigation_summary
                                    or old.mitigation_summary
                                ),
                                observed_at_unix=max(
                                    old.observed_at_unix,
                                    record.observed_at_unix,
                                ),
                                op_id=record.op_id or old.op_id,
                                weight=(
                                    int(old.weight)
                                    + int(record.weight)
                                ),
                            )
                        )
                        deduped = True
                    else:
                        # Already merged once; preserve any other
                        # records sharing the signature outside the
                        # window unchanged. (If multiple in-window
                        # records exist, treat first as canonical.)
                        merged_records.append(old)
                else:
                    merged_records.append(old)

            if not deduped:
                merged_records.append(record)

            # Ring-buffer truncate. Keep most-recent by
            # observed_at_unix.
            merged_records.sort(
                key=lambda r: r.observed_at_unix,
            )
            if len(merged_records) > cap:
                merged_records = merged_records[-cap:]

            try:
                payload = "\n".join(
                    _serialize_record(r) or ""
                    for r in merged_records
                )
                if payload:
                    payload = payload + "\n"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
            except OSError as exc:
                logger.debug(
                    "[failure_mode_memory] write failed: %s", exc,
                )
                return RecordOutcome.PERSIST_ERROR

        return (
            RecordOutcome.OK_DEDUPED if deduped
            else RecordOutcome.OK_NEW
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[failure_mode_memory] record_failure_mode raised: %s",
            exc,
        )
        return RecordOutcome.PERSIST_ERROR


def read_failure_mode_history(
    *,
    limit: Optional[int] = None,
    since_unix: float = 0.0,
) -> Tuple[FailureModeRecord, ...]:
    """Read records from the store, sorted ascending by
    ``observed_at_unix``, optionally filtered to those at or after
    ``since_unix``, with a tail-clamp at ``limit`` (default
    :func:`history_max_records`). NEVER raises."""
    try:
        path = history_path()
        records = _read_existing_records(path)
        if not records:
            return ()
        cap_max = history_max_records()
        cap = (
            int(limit) if limit is not None else cap_max
        )
        cap = max(0, min(cap, cap_max))
        if cap == 0:
            return ()
        filtered = [
            r for r in records
            if r.observed_at_unix >= float(since_unix or 0.0)
        ]
        filtered.sort(key=lambda r: r.observed_at_unix)
        if cap < len(filtered):
            filtered = filtered[-cap:]
        return tuple(filtered)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[failure_mode_memory] read_failure_mode_history "
            "raised: %s", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Convenience composer — extract + record in one call
# ---------------------------------------------------------------------------


def record_postmortem(
    postmortem: Any,
    *,
    plan: Optional[Any] = None,
    diff: str = "",
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> Tuple[ExtractionOutcome, RecordOutcome]:
    """Compose :func:`extract_failure_mode` + :func:`record_failure_mode`
    in one call. Caller passes the POSTMORTEM evidence + optional
    plan + diff; returns both outcomes for visibility.

    NEVER raises."""
    extraction, record = extract_failure_mode(
        postmortem,
        plan=plan,
        diff=diff,
        enabled_override=enabled_override,
    )
    if record is None:
        return (extraction, RecordOutcome.REJECTED)
    persist = record_failure_mode(
        record,
        enabled_override=enabled_override,
        now_ts=now_ts,
    )
    return (extraction, persist)


# ===========================================================================
# Slice 3 — FailureModeRetriever (RAG layer)
#
# PRD §31.4.2 Slice 3 spec:
#   "Given (SituationKind, target_files) from ctx, returns top-K
#    matching prior failures via SemanticIndex.cosine_score +
#    recency weighting (14d half-life — same as SemanticIndex's
#    commit half-life). Diversity dedup per Coherence Auditor
#    pattern."
#
# Design decision: ``SemanticIndex.cosine_score`` (the literal
# method name in the PRD) does not exist on the actual
# :class:`SemanticIndex` — that class exposes ``score(text)``
# which returns cosine to a single project-direction centroid,
# not pairwise text-to-text. The PRD's intent is "use semantic
# similarity"; in our **closed-enum** domain (SituationKind is
# 7-value closed; target_files is a finite set), the appropriate
# semantic primitive is **deterministic set similarity** plus the
# **same 14-day half-life formula** SemanticIndex uses for commit
# recency (literal parity — :func:`_recency_weight` mirrors the
# ``coherence_auditor._recency_weight`` formula which itself
# mirrors ``semantic_index._recency_weight``).
#
# Net result: zero embedder dependency, fully deterministic,
# reproducible across environments — and the 14d half-life
# parity makes Slice 4's prompt injection compose cleanly with
# the existing "Recent Development Momentum" digest.
#
# Algorithm:
#   1. Hard-filter pool by exact ``situation_kind`` match
#      (closed enum — silent ambiguity defeats the point).
#   2. Filter ``weight >= min_weight`` (PRD §31.4.6 memory
#      pollution defense).
#   3. UNKNOWN situations are NEVER retrieved (Slice 1 enum
#      docstring contract).
#   4. Score each candidate with combined =
#      recency * jaccard * weight_score, where:
#        * recency = 0.5 ** (age_days / halflife_days)
#        * jaccard = |files ∩| / |files ∪| (1.0 when both empty)
#        * weight_score = min(1.0, log1p(weight) / log1p(N_FLOOR))
#          — bounded, non-linear; weight=2 saturates near floor.
#   5. Sort descending by combined.
#   6. Diversity dedup — preserve at most one match per
#      ``attempted_action_kind`` (Coherence Auditor pattern).
#   7. Tail-clamp at top_k.
# ===========================================================================


import math


# ---------------------------------------------------------------------------
# Retriever env knobs
# ---------------------------------------------------------------------------


def _read_float_knob(
    name: str, default: float, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        if v < floor:
            return floor
        if v > ceiling:
            return ceiling
        return v
    except (TypeError, ValueError):
        return default


def failure_mode_top_k() -> int:
    """``JARVIS_FAILURE_MODE_TOP_K`` — default 3 (PRD §31.4.6
    diversity guidance). Clamped [1, 10]."""
    return _read_int_knob(
        "JARVIS_FAILURE_MODE_TOP_K", 3, 1, 10,
    )


def failure_mode_min_weight() -> int:
    """``JARVIS_FAILURE_MODE_MIN_WEIGHT`` — default 2 (PRD §31.4.6
    memory pollution defense: signature must recur >= 2x in
    ``dedup_window_days``). Clamped [1, 100]."""
    return _read_int_knob(
        "JARVIS_FAILURE_MODE_MIN_WEIGHT", 2, 1, 100,
    )


def failure_mode_recency_halflife_days() -> float:
    """``JARVIS_FAILURE_MODE_RECENCY_HALFLIFE_DAYS`` — default
    14.0 (PRD §31.4.6: "same as SemanticIndex's commit half-life").
    Clamped [1.0, 365.0]."""
    return _read_float_knob(
        "JARVIS_FAILURE_MODE_RECENCY_HALFLIFE_DAYS",
        14.0, 1.0, 365.0,
    )


# ---------------------------------------------------------------------------
# Match dataclass — frozen, exposes per-component scores so the
# operator can see WHY a match was returned (not just THAT it was)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureModeMatch:
    """One scored match returned by :func:`retrieve_failure_modes`.
    Frozen for safe propagation. The component scores are surfaced
    so Slice 4's injection block can render explainable context
    ("matched via 0.8 file overlap + 14d recency") and Slice 5's
    ``/failures`` REPL can show ranking provenance."""

    record: FailureModeRecord
    recency_score: float
    """``0.5 ** (age_days / halflife_days)`` — 1.0 at observation,
    0.5 at one half-life, 0.25 at two half-lives."""

    jaccard_score: float
    """``|files ∩| / |files ∪|`` — 1.0 when both file sets are
    empty (degenerate match by enum-only); 0.0 when disjoint."""

    weight_score: float
    """Bounded log-scale weight. weight=1 (below min_weight floor)
    is filtered before this is computed; weight=2 → ~0.4;
    weight=10 → 1.0 (saturated)."""

    combined_score: float
    """``recency_score × jaccard_score × weight_score`` — the
    canonical ranking key. All three multiplicands are bounded
    [0, 1]; combined is too."""

    schema_version: str = field(
        default=FAILURE_MODE_MEMORY_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "recency_score": float(self.recency_score),
            "jaccard_score": float(self.jaccard_score),
            "weight_score": float(self.weight_score),
            "combined_score": float(self.combined_score),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Pure-function scoring primitives
# ---------------------------------------------------------------------------


def _recency_weight(
    age_seconds: float, halflife_days: float,
) -> float:
    """``0.5 ** (age_days / halflife_days)``. Clamped to [0, 1].

    Literal parity with :func:`coherence_auditor._recency_weight`
    and :func:`semantic_index._recency_weight` — pinned by tests
    so any future divergence trips immediately. NEVER raises."""
    try:
        if halflife_days <= 0 or age_seconds < 0:
            return 1.0
        age_days = age_seconds / 86400.0
        return float(0.5 ** (age_days / halflife_days))
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _jaccard_similarity(
    a: Iterable[str], b: Iterable[str],
) -> float:
    """``|a ∩ b| / |a ∪ b|`` — 1.0 when both sets are empty
    (degenerate exact-match), 0.0 when union is otherwise empty.
    Defensive against non-iterables. NEVER raises."""
    try:
        sa = set(str(x) for x in a if x)
        sb = set(str(x) for x in b if x)
    except (TypeError, ValueError):
        return 0.0
    if not sa and not sb:
        # Both empty — treat as full match (situation alone matched).
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    inter = sa & sb
    return float(len(inter)) / float(len(union))


# Reference floor for log-scale weight saturation — picked so
# weight=2 (the PRD min) yields ~0.4, weight=10 saturates near 1.0.
# Pinned in tests to lock the curve shape.
_WEIGHT_SATURATION_REFERENCE: int = 10


def _weight_score(weight: int) -> float:
    """Bounded, non-linear weight scoring. ``log1p(weight) /
    log1p(N)`` capped at 1.0. Linear weight would let one
    50-recurrence outlier dominate the top-K; log1p compresses
    the tail so multiple medium-weight matches can still surface.
    NEVER raises."""
    try:
        w = max(0, int(weight))
        ref = max(1, _WEIGHT_SATURATION_REFERENCE)
        denom = math.log1p(ref)
        if denom <= 0:
            return 0.0
        return min(1.0, math.log1p(w) / denom)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


# ---------------------------------------------------------------------------
# Diversity dedup — Coherence Auditor pattern
# ---------------------------------------------------------------------------


def _diversity_dedup(
    matches: Iterable["FailureModeMatch"],
    *,
    top_k: int,
) -> Tuple["FailureModeMatch", ...]:
    """Walk matches in score-descending order; preserve at most
    one per ``attempted_action_kind`` until top_k filled. If pool
    exhausted before top_k filled, fall through and accept
    same-attempt-kind matches in score order. NEVER raises."""
    if top_k < 1:
        return tuple()
    primary: list = []
    seen_kinds: set = set()
    overflow: list = []
    for m in matches:
        kind = (m.record.attempted_action_kind or "").strip().lower()
        if kind not in seen_kinds:
            primary.append(m)
            seen_kinds.add(kind)
            if len(primary) >= top_k:
                break
        else:
            overflow.append(m)
    if len(primary) >= top_k:
        return tuple(primary[:top_k])
    # Fill remaining slots from overflow (already score-sorted).
    remaining = top_k - len(primary)
    return tuple(primary + overflow[:remaining])


# ---------------------------------------------------------------------------
# Public retriever
# ---------------------------------------------------------------------------


def retrieve_failure_modes(
    *,
    situation_kind: SituationKind,
    target_files: Iterable[str] = (),
    top_k: Optional[int] = None,
    min_weight: Optional[int] = None,
    halflife_days: Optional[float] = None,
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
) -> Tuple[FailureModeMatch, ...]:
    """Return the top-K prior :class:`FailureModeRecord` instances
    matching the current op's ``(situation_kind, target_files)``.

    Decision tree:
      1. Master-flag gate (``enabled_override`` OR
         :func:`failure_mode_memory_enabled`) — empty tuple when off.
      2. ``situation_kind is UNKNOWN`` → empty (Slice 1 contract).
      3. Read history; filter to records with the same
         ``situation_kind`` AND ``weight >= min_weight``.
      4. Score each survivor with
         ``recency * jaccard * weight_score``.
      5. Sort descending by ``combined_score``.
      6. Diversity dedup by ``attempted_action_kind``.
      7. Tail-clamp at ``top_k``.

    Returns frozen :class:`FailureModeMatch` tuple (possibly empty).
    NEVER raises."""
    try:
        if enabled_override is False:
            return tuple()
        if enabled_override is None:
            if not failure_mode_memory_enabled():
                return tuple()

        if situation_kind is SituationKind.UNKNOWN:
            return tuple()
        if not isinstance(situation_kind, SituationKind):
            # Defensive: caller passed garbage.
            return tuple()

        eff_top_k = (
            int(top_k) if top_k is not None else failure_mode_top_k()
        )
        if eff_top_k < 1:
            return tuple()
        eff_min_weight = (
            int(min_weight) if min_weight is not None
            else failure_mode_min_weight()
        )
        eff_halflife = (
            float(halflife_days)
            if halflife_days is not None
            else failure_mode_recency_halflife_days()
        )
        if eff_halflife <= 0.0:
            eff_halflife = (
                failure_mode_recency_halflife_days()
            )

        now = float(now_ts if now_ts is not None else time.time())
        candidate_files = tuple(
            str(f) for f in target_files if f
        )

        history = read_failure_mode_history()
        if not history:
            return tuple()

        matches: list = []
        for record in history:
            if record.situation_kind is not situation_kind:
                continue
            if int(record.weight) < eff_min_weight:
                continue
            age_s = max(0.0, now - float(record.observed_at_unix))
            recency = _recency_weight(age_s, eff_halflife)
            # Reconstruct the record's target_files set from its
            # signature isn't possible (signature is one-way) — but
            # the dedup-aware persistence preserves the FIRST
            # record's target-file membership in the signature, and
            # records sharing a signature have identical files by
            # construction. We approximate the record's files via
            # the candidate set when situation_kind matches AND
            # weight has accumulated (multiple ops touching the
            # same files form the dedup cluster).
            #
            # For Slice 3 we do NOT have per-record target_files
            # stored (the dataclass field is signature_hash only).
            # Jaccard reduces to a binary "same situation" signal:
            # 1.0 when candidate_files non-empty (we're in a real
            # op), else degenerate 1.0.
            #
            # Slice 4/5 may extend FailureModeRecord with a
            # target_files field for richer Jaccard; today the
            # bound below is the conservative fallback. The full
            # combined score still ranks correctly via recency *
            # weight; the constant Jaccard cancels out.
            jaccard = _jaccard_similarity(
                candidate_files, candidate_files,
            )
            weight_s = _weight_score(record.weight)
            combined = recency * jaccard * weight_s
            if combined <= 0.0:
                continue
            matches.append(
                FailureModeMatch(
                    record=record,
                    recency_score=recency,
                    jaccard_score=jaccard,
                    weight_score=weight_s,
                    combined_score=combined,
                ),
            )

        if not matches:
            return tuple()

        matches.sort(
            key=lambda m: m.combined_score, reverse=True,
        )
        return _diversity_dedup(matches, top_k=eff_top_k)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[failure_mode_memory] retrieve_failure_modes "
            "raised: %s", exc,
        )
        return tuple()


__all__ = [
    "FAILURE_MODE_MEMORY_SCHEMA_VERSION",
    "ExtractionOutcome",
    "FailureModeKind",
    "FailureModeMatch",
    "FailureModeRecord",
    "RecordOutcome",
    "SituationKind",
    "compute_signature_hash",
    "dedup_window_days",
    "extract_failure_mode",
    "failure_mode_memory_enabled",
    "failure_mode_min_weight",
    "failure_mode_recency_halflife_days",
    "failure_mode_top_k",
    "history_dir",
    "history_max_records",
    "history_path",
    "read_failure_mode_history",
    "record_failure_mode",
    "record_postmortem",
    "retrieve_failure_modes",
]
