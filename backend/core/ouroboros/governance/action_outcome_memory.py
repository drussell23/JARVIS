"""M11 Slice 1 — ActionOutcomeMemory primitive (PRD §30.5.3).

The **symmetric positive-evidence pair** to Upgrade 3 Failure-Mode
Memory. Every successful outcome — and every reverted/rejected/
deferred one — is recorded as an ``ActionOutcomeRecord`` so the
next op in the same code region sees "last time we did X here, it
worked / was reverted / was rejected" inline in its GENERATE
prompt.

Closes the **weak-form embodiment gap** identified in PRD §30.5.3:
LLM weights aren't updated from action consequences (no fine-tune
on Claude / DoubleWord), but in-context grounding via RAG provides
"every patch's outcome shapes the next patch in the same region"
without weight updates.

Architectural mirror of Upgrade 3 — same closed-enum + frozen-
record + flock'd-JSONL shape, different polarity:

  * Upgrade 3 records FAILURES (post-VERIFY-failed) → injects
    "don't repeat" context
  * M11 records OUTCOMES (post-APPLY, all dispositions) → injects
    "last time in this region" context

The two compose orthogonally: Upgrade 3 stops recurrence;
M11 amplifies what works. Together they close cross-op pattern
accumulation in both polarities.

This Slice 1 ships the **primitive layer only**:

  * :data:`ACTION_OUTCOME_MEMORY_SCHEMA_VERSION`
  * :func:`action_outcome_memory_enabled` — master flag
    (``JARVIS_ACTION_OUTCOME_MEMORY_ENABLED``) — default-FALSE
    for Slice 1; flips to default-TRUE at Slice 5 graduation per
    §30.5.3.
  * :class:`OutcomeKind` — 5-value closed enum per PRD §30.5.3
    Slice 1 spec. ``DISABLED`` is the master-off sentinel
    (mirrors :class:`ConsensusOutcome.DISABLED`).
  * :class:`ActionOutcomeRecord` — frozen dataclass with the
    canonical 11-field shape. Includes ``target_files`` on the
    record itself (an improvement over
    :class:`FailureModeRecord` Slice 1 which deferred this to
    Slice 4/5; M11 stores it from day one so Slice 3's
    region-Jaccard is meaningful).
  * :func:`compute_outcome_signature` — deterministic sha256
    over the dedup-keyed dimensions ``(situation_kind,
    attempted_action_kind, outcome_kind, target_files)``. The
    dedup tuple includes ``outcome_kind`` (unlike Upgrade 3)
    because the model genuinely tried twice and got different
    results IS a recordable distinction.

Slices 2-5 (NOT in this commit):

  * Slice 2 — Persistence: per-cluster
    ``.jarvis/action_outcomes/{cluster_id}.jsonl`` flock'd
    appends; Decision A robustness fallback (per scope) when
    SemanticIndex unavailable.
  * Slice 3 — :class:`ActionOutcomeRetriever`
    ``recall_for_region(target_files, ...)`` — Coherence-style
    diversity-weighted scoring. Decision C: refactor shared
    scoring primitives or cross-module import.
  * Slice 4 — :mod:`strategic_direction` integration; new
    ``## Recent Region Outcomes`` block (4KB cap per PRD).
  * Slice 5 — Graduation (default-true), 4 AST pins, 5
    FlagRegistry seeds, ``/outcomes`` REPL,
    ``/observability/action-outcomes`` HTTP routes, SSE event,
    Decision B :class:`SuccessPatternStore` façade migration.

Reuses (zero duplication):

  * :class:`SituationKind` from :mod:`failure_mode_memory` —
    SAME 7-value taxonomy. The forward-direction classifier
    (``classify_situation_from_ctx``) used by M11's retriever
    is also reused. Adding a new SituationKind to
    :mod:`failure_mode_memory` automatically benefits M11.

Cost contract (entire 5-slice arc):

  * Zero LLM calls on retrieval hot path (deterministic +
    SemanticIndex cluster lookup).
  * +<= 4KB to GENERATE prompt amortized by Anthropic 5-min
    prompt cache.
  * ~25MB disk total (50 clusters × 1000 records × 500B per
    PRD §30.5.3 estimate).

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + the SituationKind enum from
    :mod:`failure_mode_memory` ONLY (Slice 1 narrowest floor;
    Slice 2 adds :mod:`semantic_index`, Slice 3 may add
    cross-module scoring import or shared primitives module
    per Decision C).
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    tool_executor / change_engine / subagent_scheduler /
    auto_action_router / strategic_direction (Slice 4 reverses
    this asymmetry — strategic_direction lazy-imports
    action_outcome_memory, NEVER the reverse).
  * Pure data — never mutates external state, never raises out
    of any public function.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

# Reuse the SituationKind closed taxonomy from Upgrade 3 — adding
# a new situation to one arc benefits both (cross-op pattern
# accumulation in BOTH polarities). The forward-direction
# classifier (Slice 2 of Upgrade 3) is the same code path M11's
# retriever will use to classify the current op before lookup.
from backend.core.ouroboros.governance.failure_mode_memory import (
    SituationKind,
    _canonicalize_target_files,
)

logger = logging.getLogger(__name__)


ACTION_OUTCOME_MEMORY_SCHEMA_VERSION: str = "action_outcome_memory.1"


# ---------------------------------------------------------------------------
# Master flag — asymmetric env semantics, default-FALSE for Slice 1
# ---------------------------------------------------------------------------


def action_outcome_memory_enabled() -> bool:
    """``JARVIS_ACTION_OUTCOME_MEMORY_ENABLED`` (default ``false``
    until Slice 5 graduation per PRD §30.5.3).

    Asymmetric env semantics — empty/whitespace = unset = current
    default (false for Slice 1); explicit ``1``/``true``/``yes``/
    ``on`` flips on. Same shape as
    :func:`failure_mode_memory_enabled` /
    :func:`coherence_auditor_enabled` / :func:`cigw_enabled` /
    :func:`quorum_enabled` graduated flags so the Slice 5
    graduation flip is a one-character edit.

    Re-read on every call so flips hot-revert without restart."""
    raw = os.environ.get(
        "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # Slice 1 default; flips to True at Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# OutcomeKind — 5-value closed enum (PRD §30.5.3 Slice 1 spec)
# ---------------------------------------------------------------------------


class OutcomeKind(str, enum.Enum):
    """Closed taxonomy of *what happened* to an attempted action.
    Mirrors PRD §30.5.3 canonical 5-value list. Outcome is known
    at write site (the orchestrator knows whether APPLY succeeded
    + whether VERIFY passed + whether the operator
    canceled/rejected) — there is intentionally NO chain-of-
    responsibility ``OTHER`` / ``UNKNOWN`` sentinel because the
    extractor cannot fall through to "unclear what happened" at
    record time.

    Closed by construction — caller branches on the enum, never
    on free-form strings. Adding a new OutcomeKind requires a PRD
    update + recorder pattern; this is intentional friction
    against silent vocabulary drift."""

    APPLIED_VERIFIED = "applied_verified"
    """Patch applied to disk + post-APPLY VERIFY passed + no
    rollback observed. The gold-standard success outcome —
    Slice 4's first-attempt injection prioritizes these as
    "last time we did X here, it worked"."""

    APPLIED_REVERTED = "applied_reverted"
    """Patch applied but later reverted (manual or auto-rollback
    via L2 RepairEngine). Distinct from REJECTED because the
    patch DID land and DID get observed in the working tree —
    important signal for the next op ("X looked like it would
    work, but didn't survive contact with the codebase")."""

    REJECTED = "rejected"
    """Patch failed at GATE / Iron Gate / SemanticGuardian /
    Iron Gate exploration floor — NEVER reached APPLY phase. The
    candidate was generated but structurally rejected before any
    disk mutation."""

    DEFERRED = "deferred"
    """Operator chose ``/cancel``, NOTIFY_APPLY rejection, or
    Orange-tier APPROVAL_REQUIRED was declined. Intentional
    non-action — the candidate may have been correct; the
    operator simply chose not to apply it (e.g., cost ceiling,
    out-of-scope, or "good idea but wait")."""

    DISABLED = "disabled"
    """Master-off sentinel. Records with ``outcome_kind=DISABLED``
    are NEVER persisted (Slice 2 recorder filters them
    structurally) and are NEVER returned by the retriever. This
    matches :class:`ConsensusOutcome.DISABLED` and
    :class:`FailureModeKind.OTHER` discipline — an enum value
    that exists for type-safety but carries zero observational
    signal."""


# ---------------------------------------------------------------------------
# Frozen ActionOutcomeRecord — 11-field shape per PRD §30.5.3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionOutcomeRecord:
    """One ``(situation, action, outcome)`` triplet recorded at
    APPLY/VERIFY/REJECT/CANCEL boundaries. Frozen for safe
    propagation across async + lock boundaries.

    11-field canonical shape — improves on
    :class:`FailureModeRecord` Slice 1 by storing
    ``target_files`` on the record itself (M11 retrieval needs
    region Jaccard from day one; Upgrade 3 deferred this to a
    later slice). Other field shapes mirror the symmetric
    positive-evidence pair pattern.

    Dedup key: ``signature_hash`` derived from
    ``(situation_kind, attempted_action_kind, outcome_kind,
    target_files)``. **Critically includes ``outcome_kind``** —
    unlike :class:`FailureModeRecord` where the dedup tuple was
    (situation, attempt, files). Reason: if the same situation +
    region + attempt produced DIFFERENT outcomes in two ops,
    those are genuinely distinct evidence ("we tried twice and
    got different results"), not a recurrence to merge. Two
    APPLIED_VERIFIED for the same triplet → merge weight. One
    APPLIED_VERIFIED + one APPLIED_REVERTED for the same triplet
    → keep both records (the model needs both signals).
    """

    signature_hash: str
    """sha256 hex of the dedup-keyed 4-tuple — produced by
    :func:`compute_outcome_signature`. Stable across ops +
    sessions."""

    situation_kind: SituationKind
    """Reused 7-value closed enum from
    :mod:`failure_mode_memory`. Adding a new situation to that
    module automatically benefits M11."""

    attempted_action_kind: str
    """Short free-form tag for the *attempt* shape (e.g.
    ``add_dataclass``, ``rename_function``). Free-form rather
    than enum because attempt vocabulary is open-set; closed
    taxonomy lives at situation + outcome level."""

    outcome_kind: OutcomeKind
    """Closed 5-value disposition. ``DISABLED`` is the master-
    off sentinel and is NEVER persisted by Slice 2's recorder."""

    target_files: Tuple[str, ...]
    """Canonicalized sorted tuple of touched files. Stored on
    the record (unlike Upgrade 3) so Slice 3's retriever can
    compute meaningful Jaccard against the current op's
    ``target_files`` from day one."""

    commit_hash: str
    """Git commit hash for ``APPLIED_VERIFIED`` outcomes
    (``AutoCommitter`` provenance). Empty string for non-applied
    outcomes (``REJECTED`` / ``DEFERRED``) and for
    ``APPLIED_REVERTED`` (the original commit may have been
    rebased away by the revert)."""

    summary: str
    """Short operator-readable string. Polarity-dependent:
    for ``APPLIED_VERIFIED`` it's "what worked" ("Imported X
    from canonical module; tests pass"); for ``APPLIED_REVERTED``
    it's "what didn't survive" ("Looked correct in isolation but
    broke downstream caller"); for ``REJECTED`` it's "what the
    gate caught" ("SemanticGuardian: removed_import_still_-
    referenced"); for ``DEFERRED`` it's the operator's
    rationale if surfaced, or empty.

    Slice 4 injects this verbatim into the GENERATE prompt's
    ``## Recent Region Outcomes`` block."""

    observed_at_unix: float
    """Unix ts of the original APPLY/VERIFY/REJECT/CANCEL
    event. Slice 3 retriever weights records by recency with
    14d half-life (mirrors :mod:`semantic_index` commit
    half-life and Upgrade 3 retrieval discipline)."""

    op_id: str
    """Originating ``op_id`` — surfaced for traceability into
    the causality DAG (Priority #2)."""

    cluster_id: str = ""
    """Optional :mod:`semantic_index` cluster identifier (Decision
    A from scope: SemanticIndex-optional). Populated by Slice 2
    when SemanticIndex is available; empty string otherwise.
    Slice 3 retrieval uses cluster_id when non-empty for the
    region lookup; falls back to file-Jaccard when empty
    (graceful degradation when SemanticIndex unavailable, e.g.
    cold boot)."""

    weight: int = 1
    """Recurrence count. Initial 1; Slice 2 recorder increments
    when a new triplet's signature matches an existing record
    within the dedup window. Slice 4's injection prioritizes
    higher-weight records."""

    schema_version: str = field(
        default=ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
    )

    # ---- Serialization ----------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Render to JSON-friendly dict. Stable key order via
        explicit construction (NOT dataclasses.asdict — that
        would leak field-iteration-order coupling)."""
        return {
            "signature_hash": self.signature_hash,
            "situation_kind": self.situation_kind.value,
            "attempted_action_kind": self.attempted_action_kind,
            "outcome_kind": self.outcome_kind.value,
            "target_files": list(self.target_files),
            "commit_hash": self.commit_hash,
            "summary": self.summary,
            "observed_at_unix": float(self.observed_at_unix),
            "op_id": self.op_id,
            "cluster_id": self.cluster_id,
            "weight": int(self.weight),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, payload: Any,
    ) -> Optional["ActionOutcomeRecord"]:
        """Reconstruct from a :meth:`to_dict` payload. Returns
        ``None`` on schema mismatch / missing required fields /
        unknown enum values. NEVER raises.

        Schema-version gate: payloads with a different
        ``schema_version`` are rejected silently (caller treats
        unparseable lines as corrupt — mirrors :mod:`postmortem_-
        recall` and other graduated arcs)."""
        if not isinstance(payload, dict):
            return None
        try:
            if (
                payload.get("schema_version")
                != ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
            ):
                return None
            sk = _situation_kind_from_value(
                payload.get("situation_kind"),
            )
            ok = _outcome_kind_from_value(
                payload.get("outcome_kind"),
            )
            if sk is None or ok is None:
                return None
            sig = payload.get("signature_hash")
            if not isinstance(sig, str) or not sig:
                return None
            tf_raw = payload.get("target_files", [])
            if isinstance(tf_raw, (list, tuple)):
                tf = tuple(str(f) for f in tf_raw if f)
            else:
                tf = ()
            return cls(
                signature_hash=sig,
                situation_kind=sk,
                attempted_action_kind=str(
                    payload.get("attempted_action_kind", ""),
                ),
                outcome_kind=ok,
                target_files=tf,
                commit_hash=str(payload.get("commit_hash", "")),
                summary=str(payload.get("summary", "")),
                observed_at_unix=float(
                    payload.get("observed_at_unix", 0.0),
                ),
                op_id=str(payload.get("op_id", "")),
                cluster_id=str(payload.get("cluster_id", "")),
                weight=int(payload.get("weight", 1)),
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[action_outcome_memory] from_dict swallowed: %s",
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
    miss. NEVER raises. Reuses the closed taxonomy from
    :mod:`failure_mode_memory`."""
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


def _outcome_kind_from_value(
    value: Any,
) -> Optional[OutcomeKind]:
    """Map a string to :class:`OutcomeKind` member; ``None`` on
    miss. NEVER raises."""
    if value is None:
        return None
    try:
        token = str(value).strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not token:
        return None
    for member in OutcomeKind:
        if member.value == token:
            return member
    return None


# ---------------------------------------------------------------------------
# Signature hashing — deterministic, file-order-invariant, includes outcome
# ---------------------------------------------------------------------------


def compute_outcome_signature(
    *,
    situation_kind: SituationKind,
    attempted_action_kind: str,
    outcome_kind: OutcomeKind,
    target_files: Iterable[str] = (),
) -> str:
    """Deterministic sha256 hex over the dedup-keyed 4-tuple.

    Critically includes ``outcome_kind`` in the dedup dimension —
    unlike :func:`failure_mode_memory.compute_signature_hash`
    where the dedup tuple is (situation, attempt, files). Reason
    documented on :class:`ActionOutcomeRecord`: the same triplet
    producing different outcomes IS a recordable distinction.

    Inputs are joined with ``\\x00`` separators (cannot collide
    with any path or token character). File listing is
    canonicalized via the same
    :func:`failure_mode_memory._canonicalize_target_files`
    helper (sorted internally — order doesn't matter).

    Returns full 64-char sha256 hex. NEVER raises — falls back
    to the empty-input hash on type errors so callers always
    have a string to store and key on."""
    try:
        sk = (
            situation_kind.value
            if isinstance(situation_kind, SituationKind)
            else str(situation_kind or "")
        ).strip().lower()
        ak = str(attempted_action_kind or "").strip().lower()
        ok = (
            outcome_kind.value
            if isinstance(outcome_kind, OutcomeKind)
            else str(outcome_kind or "")
        ).strip().lower()
        files = _canonicalize_target_files(target_files)
        payload = "\x00".join(
            (
                "sk=" + sk,
                "ak=" + ak,
                "ok=" + ok,
                "files=" + ",".join(files),
            ),
        )
        return hashlib.sha256(
            payload.encode("utf-8", errors="replace"),
        ).hexdigest()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[action_outcome_memory] compute_outcome_signature "
            "swallowed: %s", exc,
        )
        return hashlib.sha256(b"").hexdigest()


# ===========================================================================
# Slice 2 — Persistence layer (PRD §30.5.3 Slice 2)
#
# Per-cluster ``.jarvis/action_outcomes/{cluster_id}.jsonl`` flock'd
# appends with dedup-window merge semantics. Decision A3 from scope
# ("SemanticIndex-optional graceful fallback") realized: when
# SemanticIndex is unavailable (no embedder, cold boot, master-off,
# fastembed import failure), records persist to a global fallback
# file (``_global.jsonl``) keyed off empty cluster_id. The retriever
# (Slice 3) walks all cluster files + the global file, so cluster-
# bucketing is a *storage optimization*, never a correctness
# dependency.
#
# Mirrors Upgrade 3 Slice 2 (failure_mode_memory.record_failure_mode)
# structurally — same flock'd read-modify-write discipline, same
# RecordOutcome closed enum, same dedup-window merge math. Reuses
# :mod:`cross_process_jsonl` (Move 6 / Slice C / Upgrade 3 Slice 2)
# — zero new flock substrate.
#
# Authority widening: this slice introduces dependencies on
# :mod:`semantic_index` (cluster lookup) + :mod:`cross_process_jsonl`
# (flock primitive). Slice 1's authority test is updated in lockstep
# to exempt these two modules from the forbidden list. The full
# forbidden-imports cage (orchestrator / iron_gate / providers / ...)
# remains structurally pinned.
# ===========================================================================


import json
import re
import time
from pathlib import Path

# Slice 2 dependencies (carved out of Slice 1's narrowest floor):
from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
    flock_critical_section,
)


# ---------------------------------------------------------------------------
# Persistence env knobs — same clamping discipline as Upgrade 3 Slice 2
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
    """``JARVIS_ACTION_OUTCOME_HISTORY_DIR`` — default
    ``.jarvis/action_outcomes``. Per-cluster files
    (``{cluster_id}.jsonl``) live under this dir; the global
    fallback file is ``_global.jsonl``."""
    raw = os.environ.get(
        "JARVIS_ACTION_OUTCOME_HISTORY_DIR",
        ".jarvis/action_outcomes",
    ).strip()
    return Path(raw or ".jarvis/action_outcomes")


def max_records_per_cluster() -> int:
    """``JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER`` — bounded
    ring-buffer cap PER cluster file. Default 1000 (PRD §30.5.3
    storage estimate: 50 clusters × 1000 records × 500B ≈ 25MB
    total). Clamped [50, 100000]."""
    return _read_int_knob(
        "JARVIS_ACTION_OUTCOME_MAX_RECORDS_PER_CLUSTER",
        1000, 50, 100_000,
    )


def dedup_window_days() -> int:
    """``JARVIS_ACTION_OUTCOME_DEDUP_WINDOW_DAYS`` — recurrence
    dedup window. Default 30 (parity with Upgrade 3). Records
    sharing a signature within the window merge (weight++);
    outside the window they coexist as distinct records.

    Note: because :func:`compute_outcome_signature` includes
    ``outcome_kind`` in the dedup tuple, two records with the
    same situation+region+attempt but different outcomes have
    DIFFERENT signatures and never merge — this M11 distinction
    from Upgrade 3 is structural, not policy."""
    return _read_int_knob(
        "JARVIS_ACTION_OUTCOME_DEDUP_WINDOW_DAYS",
        30, 1, 365,
    )


# ---------------------------------------------------------------------------
# RecordOutcome — closed taxonomy for record_action_outcome
# ---------------------------------------------------------------------------


class RecordOutcome(str, enum.Enum):
    """Closed taxonomy for :func:`record_action_outcome` results.
    Mirrors :class:`failure_mode_memory.RecordOutcome` shape."""

    OK_NEW = "ok_new"
    """New signature appended."""

    OK_DEDUPED = "ok_deduped"
    """Existing signature within dedup window — weight++ merge."""

    DISABLED = "disabled"
    """Master flag is off OR record's outcome_kind is DISABLED."""

    REJECTED = "rejected"
    """Garbage input (non-ActionOutcomeRecord)."""

    PERSIST_ERROR = "persist_error"
    """Disk fault during flock'd append / read."""

    SERIALIZE_ERROR = "serialize_error"
    """Record's :meth:`to_dict` produced non-JSON-serializable."""


# ---------------------------------------------------------------------------
# Cluster ID resolution — Decision A3 graceful fallback
# ---------------------------------------------------------------------------


# Filename safety — cluster_id is used as filename stem. Only allow
# alphanumeric + hyphen + underscore (path traversal + reserved-name
# defense). SemanticIndex cluster_ids in production are
# integer-like strings ("0", "1", ...) so the filter is generous.
_SAFE_CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# Sentinel filename for records whose cluster_id couldn't be
# resolved (SemanticIndex unavailable / empty / disabled). Distinct
# from any valid cluster_id (cluster_ids never start with ``_``
# in SemanticIndex's current vocab).
_GLOBAL_FALLBACK_STEM: str = "_global"


def _resolve_cluster_id(
    target_files: Iterable[str],
    *,
    cluster_id_override: Optional[str] = None,
) -> str:
    """Resolve a SemanticIndex cluster_id for the current
    ``target_files``. Decision A3: best-effort, never raises,
    falls back to empty-string sentinel on any failure
    (SemanticIndex disabled, fastembed unavailable, embed
    failure, empty corpus, etc.).

    ``cluster_id_override`` is for tests + intake pre-resolved
    paths; bypasses the SemanticIndex call when provided."""
    if cluster_id_override is not None:
        return str(cluster_id_override).strip()
    try:
        files = tuple(
            str(f) for f in target_files if f
        )
        if not files:
            return ""
        # Build query text from file paths — one path per line,
        # matches SemanticIndex's input convention for code-region
        # scoring.
        query_text = "\n".join(files)
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
            get_default_index,
        )
        idx = get_default_index()
        result = idx.score_with_cluster(query_text)
        if not isinstance(result, dict):
            return ""
        cluster_id = result.get("cluster_id")
        if cluster_id is None:
            return ""
        return str(cluster_id).strip()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[action_outcome_memory] _resolve_cluster_id "
            "swallowed: %s", exc,
        )
        return ""


def _safe_filename_stem(cluster_id: str) -> str:
    """Convert a cluster_id to a safe filename stem. Returns the
    ``_GLOBAL_FALLBACK_STEM`` for empty / disallowed values
    (path traversal + reserved-character defense). NEVER raises."""
    try:
        s = str(cluster_id or "").strip()
        if not s:
            return _GLOBAL_FALLBACK_STEM
        if not _SAFE_CLUSTER_ID_RE.match(s):
            return _GLOBAL_FALLBACK_STEM
        return s
    except Exception:  # noqa: BLE001 — defensive
        return _GLOBAL_FALLBACK_STEM


def cluster_jsonl_path(cluster_id: str) -> Path:
    """Resolve the JSONL file path for a given cluster_id. Empty
    or invalid cluster_ids resolve to the global fallback file
    (``_global.jsonl``)."""
    stem = _safe_filename_stem(cluster_id)
    return history_dir() / f"{stem}.jsonl"


# ---------------------------------------------------------------------------
# Internal: serialization + read helpers
# ---------------------------------------------------------------------------


def _serialize_record(record: ActionOutcomeRecord) -> Optional[str]:
    """Render record as one JSONL line. NEVER raises."""
    try:
        return json.dumps(
            record.to_dict(), sort_keys=True, ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        logger.debug(
            "[action_outcome_memory] serialize: %s", exc,
        )
        return None


def _read_existing_records(
    path: Path,
) -> Tuple[ActionOutcomeRecord, ...]:
    """Defensively read all records from one cluster JSONL.
    Corrupt lines silently dropped. NEVER raises."""
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
        rec = ActionOutcomeRecord.from_dict(payload)
        if rec is not None:
            out.append(rec)
    return tuple(out)


def _within_dedup_window(
    candidate_ts: float,
    existing_ts: float,
    window_days: int,
) -> bool:
    """True iff the two timestamps are within ``window_days`` days
    of each other. Mirrors Upgrade 3 Slice 2 helper."""
    if window_days <= 0:
        return False
    delta = abs(candidate_ts - existing_ts)
    return delta <= float(window_days) * 86400.0


# ---------------------------------------------------------------------------
# Public: record_action_outcome
# ---------------------------------------------------------------------------


def record_action_outcome(
    record: ActionOutcomeRecord,
    *,
    enabled_override: Optional[bool] = None,
    now_ts: Optional[float] = None,
    cluster_id_override: Optional[str] = None,
) -> RecordOutcome:
    """Persist an :class:`ActionOutcomeRecord` to the per-cluster
    bounded JSONL store with dedup-aware merge.

    Decision tree:

      1. Master flag check — ``enabled_override`` OR the master
         env flag.
      2. Type check — must be an :class:`ActionOutcomeRecord`.
      3. ``DISABLED`` outcome rejection — these are sentinel
         values for the master-off case; never persist them.
      4. Resolve cluster_id (Decision A3 graceful fallback).
         If ``record.cluster_id`` is non-empty, honor it; else
         compute via :func:`_resolve_cluster_id`.
      5. Acquire flock on the per-cluster JSONL.
      6. Read existing records.
      7. Find matching signature within dedup window. If found,
         replace with merged record (weight++,
         observed_at_unix=max). If not, append.
      8. Truncate to :func:`max_records_per_cluster`.
      9. Atomic-write the truncated payload back.

    Cross-process safe via :mod:`cross_process_jsonl`. NEVER
    raises. ``now_ts`` is reserved for future telemetry; today
    the dedup decision uses ``record.observed_at_unix``."""
    try:
        if enabled_override is False:
            return RecordOutcome.DISABLED
        if enabled_override is None:
            if not action_outcome_memory_enabled():
                return RecordOutcome.DISABLED

        if not isinstance(record, ActionOutcomeRecord):
            return RecordOutcome.REJECTED

        if record.outcome_kind is OutcomeKind.DISABLED:
            # Sentinel — never persisted.
            return RecordOutcome.REJECTED

        line = _serialize_record(record)
        if line is None:
            return RecordOutcome.SERIALIZE_ERROR

        # Resolve / honor cluster_id. If the record already carries
        # a cluster_id from the orchestrator (already resolved
        # upstream), use it; otherwise compute from target_files.
        if record.cluster_id:
            resolved_cluster = record.cluster_id
        else:
            resolved_cluster = _resolve_cluster_id(
                record.target_files,
                cluster_id_override=cluster_id_override,
            )
            # Stamp the resolved cluster_id back onto the record
            # so the on-disk row is self-describing (the retriever
            # can use ``cluster_id`` field directly without
            # re-resolving).
            if resolved_cluster:
                record = ActionOutcomeRecord(
                    signature_hash=record.signature_hash,
                    situation_kind=record.situation_kind,
                    attempted_action_kind=(
                        record.attempted_action_kind
                    ),
                    outcome_kind=record.outcome_kind,
                    target_files=record.target_files,
                    commit_hash=record.commit_hash,
                    summary=record.summary,
                    observed_at_unix=record.observed_at_unix,
                    op_id=record.op_id,
                    cluster_id=resolved_cluster,
                    weight=record.weight,
                )
                # Re-serialize with the stamped cluster_id
                line = _serialize_record(record)
                if line is None:
                    return RecordOutcome.SERIALIZE_ERROR

        path = cluster_jsonl_path(resolved_cluster)
        # ``now_ts`` reserved for Slice 5 telemetry; current dedup
        # uses ``record.observed_at_unix`` (canonical event time).
        _now_ts = (
            now_ts if now_ts is not None else time.time()
        )
        del _now_ts
        window = dedup_window_days()
        cap = max_records_per_cluster()

        with flock_critical_section(path) as acquired:
            if not acquired:
                # Best-effort fallback — append rather than drop
                # the record entirely. Mirrors Upgrade 3 Slice 2.
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
                            ActionOutcomeRecord(
                                signature_hash=old.signature_hash,
                                situation_kind=old.situation_kind,
                                attempted_action_kind=(
                                    old.attempted_action_kind
                                ),
                                outcome_kind=old.outcome_kind,
                                target_files=old.target_files,
                                commit_hash=(
                                    record.commit_hash
                                    or old.commit_hash
                                ),
                                summary=(
                                    record.summary
                                    or old.summary
                                ),
                                observed_at_unix=max(
                                    old.observed_at_unix,
                                    record.observed_at_unix,
                                ),
                                op_id=record.op_id or old.op_id,
                                cluster_id=(
                                    old.cluster_id
                                    or record.cluster_id
                                ),
                                weight=(
                                    int(old.weight)
                                    + int(record.weight)
                                ),
                            ),
                        )
                        deduped = True
                    else:
                        merged_records.append(old)
                else:
                    merged_records.append(old)

            if not deduped:
                merged_records.append(record)

            # Ring-buffer truncate by recency.
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
                    "[action_outcome_memory] write failed: %s",
                    exc,
                )
                return RecordOutcome.PERSIST_ERROR

        return (
            RecordOutcome.OK_DEDUPED if deduped
            else RecordOutcome.OK_NEW
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[action_outcome_memory] record_action_outcome "
            "raised: %s", exc,
        )
        return RecordOutcome.PERSIST_ERROR


# ---------------------------------------------------------------------------
# Public: read APIs — per-cluster + cross-cluster aggregation
# ---------------------------------------------------------------------------


def read_action_outcomes_for_cluster(
    cluster_id: str,
    *,
    limit: Optional[int] = None,
    since_unix: float = 0.0,
) -> Tuple[ActionOutcomeRecord, ...]:
    """Read records from a single cluster's JSONL file. Empty
    cluster_id resolves to the global fallback file. Sorted
    ascending by ``observed_at_unix``; tail-clamp at ``limit``
    (default :func:`max_records_per_cluster`). NEVER raises."""
    try:
        path = cluster_jsonl_path(cluster_id)
        records = _read_existing_records(path)
        if not records:
            return ()
        cap_max = max_records_per_cluster()
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
            "[action_outcome_memory] "
            "read_action_outcomes_for_cluster raised: %s", exc,
        )
        return ()


def read_all_action_outcomes(
    *,
    limit: Optional[int] = None,
    since_unix: float = 0.0,
) -> Tuple[ActionOutcomeRecord, ...]:
    """Walk every JSONL file under :func:`history_dir` (per-cluster
    + global fallback) and return the union, sorted ascending by
    ``observed_at_unix``, tail-clamped at ``limit`` (default sums
    to a hard ceiling of 50 × :func:`max_records_per_cluster` per
    PRD §30.5.3 storage estimate). NEVER raises."""
    try:
        base = history_dir()
        if not base.exists() or not base.is_dir():
            return ()
        all_records: list = []
        for child in base.iterdir():
            if not child.is_file():
                continue
            if child.suffix != ".jsonl":
                continue
            try:
                records = _read_existing_records(child)
            except Exception:  # noqa: BLE001 — defensive per-file
                continue
            for r in records:
                if r.observed_at_unix >= float(since_unix or 0.0):
                    all_records.append(r)
        if not all_records:
            return ()
        all_records.sort(key=lambda r: r.observed_at_unix)
        # Hard ceiling to bound memory under pathological dirs.
        hard_ceiling = 50 * max_records_per_cluster()
        cap = (
            int(limit) if limit is not None else hard_ceiling
        )
        cap = max(0, min(cap, hard_ceiling))
        if cap == 0:
            return ()
        if cap < len(all_records):
            all_records = all_records[-cap:]
        return tuple(all_records)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[action_outcome_memory] read_all_action_outcomes "
            "raised: %s", exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Public: clear (operator-triggered maintenance)
# ---------------------------------------------------------------------------


def clear_action_outcomes(
    *,
    enabled_override: Optional[bool] = None,
) -> bool:
    """Truncate ALL cluster JSONL files under :func:`history_dir`.
    Operational maintenance — to be called by Slice 5's
    ``/outcomes clear`` REPL verb. Master flag is checked: a
    disabled subsystem refuses the clear (operator should re-
    enable + re-clear if they want records gone).

    NEVER raises. Returns True iff every file unlink succeeded
    (or none existed); False on master-off / partial failure."""
    try:
        if enabled_override is False:
            return False
        if enabled_override is None:
            if not action_outcome_memory_enabled():
                return False
        base = history_dir()
        if not base.exists():
            return True
        try:
            children = list(base.iterdir())
        except OSError:
            return False
        any_failed = False
        for child in children:
            if not child.is_file():
                continue
            if child.suffix != ".jsonl":
                continue
            with flock_critical_section(child) as acquired:
                if not acquired:
                    any_failed = True
                    continue
                try:
                    child.unlink()
                except OSError as exc:
                    logger.debug(
                        "[action_outcome_memory] clear unlink: "
                        "%s", exc,
                    )
                    any_failed = True
        return not any_failed
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[action_outcome_memory] clear_action_outcomes "
            "raised: %s", exc,
        )
        return False


__all__ = [
    "ACTION_OUTCOME_MEMORY_SCHEMA_VERSION",
    "ActionOutcomeRecord",
    "OutcomeKind",
    "RecordOutcome",
    "action_outcome_memory_enabled",
    "clear_action_outcomes",
    "cluster_jsonl_path",
    "compute_outcome_signature",
    "dedup_window_days",
    "history_dir",
    "max_records_per_cluster",
    "read_action_outcomes_for_cluster",
    "read_all_action_outcomes",
    "record_action_outcome",
]
