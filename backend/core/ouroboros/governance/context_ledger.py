"""
ContextLedger — Slice 1 of the Intent-Aware Context Preservation arc.
======================================================================

Structured, append-only preservation layer that survives every
compaction pass. The existing :class:`ContextCompactor` operates on
dialogue entries and may drop them; :class:`ContextLedger` operates on
*structured facts* extracted from those same entries and NEVER drops
them. The two layers are complementary.

What this module IS
-------------------

A parallel ledger of five fact kinds, each capturing the minimum
information needed to reconstruct "what the agent did / learned" after
dialogue chunks have been compacted away:

* :class:`FileReadEntry`  — which files were read, when, by what tool,
  with a hash snapshot for staleness detection.
* :class:`ToolCallEntry`  — tool name, args fingerprint, outcome,
  duration, round index.
* :class:`ErrorEntry`     — error class, scoped location, status
  (open / investigating / resolved), recovery attempts.
* :class:`DecisionEntry`  — structured authorization moments: plan
  approvals, inline-permission allows, orange-PR merges. Immutable.
* :class:`QuestionEntry`  — ``ask_human`` questions + operator answers.
  Open questions are first-class state, not prose buried in history.

What this module IS NOT
-----------------------

* A dialogue store. The existing ``op_context`` / ``op_dialogue`` stays
  the source of truth for turn-level content. This layer is *meta*.
* An LLM interface. Every extraction helper is pure code (§5 Tier 0).
  Model-written summaries live in the separate compactor path.
* A replacement for :class:`ContextCompactor`. Compaction still runs;
  the ledger just makes sure the structured residue doesn't die with
  the compacted dialogue chunks.

Manifesto alignment
-------------------

* §4 Synthetic Soul — the ledger IS the episodic memory of one op.
  After compaction, the agent still remembers which files it touched,
  which errors it saw, which decisions were blessed. That's memory,
  not prose regurgitation.
* §5 Tier 0 — all extraction is deterministic string / regex / typed
  accessor. No model calls. The ledger's write path is microsecond.
* §7 Authority Override — ledger entries are **immutable** by design.
  No ``edit()``, no ``replace()``. Corrections are new entries; the
  old one stays as the audit trail (§8).
* §8 Absolute Observability — every write emits an INFO log line
  keyed by ``[ContextLedger]``; Slice 4 adds SSE + GET endpoints.

Growth discipline
-----------------

The ledger is append-only but NOT unbounded. Per-kind caps (env-tunable)
apply LRU-style eviction — oldest entries of the same kind within the
same op drop off once the cap is exceeded. Pinned entries (Slice 3)
are exempt. A single op that fires 100,000 tool calls will not OOM the
process; the cap trades tail-history for safety.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ContextLedger")


# ---------------------------------------------------------------------------
# Schema version (Slice 5 pins into telemetry)
# ---------------------------------------------------------------------------

CONTEXT_LEDGER_SCHEMA_VERSION: str = "context_ledger.v1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _max_entries_per_kind() -> int:
    """Per-kind LRU cap. Default 512: large enough to cover a ~30-round
    heavy op, small enough to keep memory bounded across long sessions."""
    try:
        return max(16, int(os.environ.get(
            "JARVIS_CONTEXT_LEDGER_MAX_PER_KIND", "512",
        )))
    except (TypeError, ValueError):
        return 512


def _max_ops_retained() -> int:
    """Upper bound on simultaneous op_ids in memory. Each op carries its
    own ledger; the registry evicts oldest once cap is exceeded."""
    try:
        return max(4, int(os.environ.get(
            "JARVIS_CONTEXT_LEDGER_MAX_OPS", "64",
        )))
    except (TypeError, ValueError):
        return 64


# ---------------------------------------------------------------------------
# Entry kinds
# ---------------------------------------------------------------------------


class LedgerEntryKind(str, enum.Enum):
    FILE_READ = "file_read"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    DECISION = "decision"
    QUESTION = "question"


ALL_KINDS: FrozenSet[str] = frozenset(k.value for k in LedgerEntryKind)
"""Public tuple of known entry kinds. Slice 4 uses this to pin the
bridge's event-type allowlist without duplicating the enum."""


# ---------------------------------------------------------------------------
# Shared entry header — every kind carries these
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EntryCommon:
    """Shared structural header. Frozen — entries are immutable by design."""

    entry_id: str
    op_id: str
    kind: str
    created_at_ts: float
    created_at_iso: str
    schema_version: str = CONTEXT_LEDGER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Typed entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileReadEntry(_EntryCommon):
    """A file was read during the op.

    ``content_hash`` is computed at read time and lets Slice 2's scorer
    detect whether a file the operator mentioned was in the same state
    the agent read it in — a proxy for staleness that existing
    compaction throws away.
    """

    file_path: str = ""
    tool: str = ""
    round_index: int = 0
    byte_size: int = 0
    content_hash: str = ""


@dataclass(frozen=True)
class ToolCallEntry(_EntryCommon):
    """A Venom tool invocation and its outcome.

    Sized to capture "what happened" without storing the full output:
    ``args_fingerprint`` is SHA256[:12] of the arguments; ``result_preview``
    is bounded to a short tail for debug legibility.
    """

    tool: str = ""
    args_fingerprint: str = ""
    round_index: int = 0
    call_id: str = ""
    status: str = ""   # "success" / "timeout" / "policy_denied" / "exec_error" / "cancelled"
    duration_ms: float = 0.0
    result_preview: str = ""
    output_bytes: int = 0


@dataclass(frozen=True)
class ErrorEntry(_EntryCommon):
    """An error observed during the op.

    ``status`` tracks the human / system judgement on whether this
    error is still live. A resolved error is NOT removed from the
    ledger — it stays as audit trail; status just moves from 'open'
    to 'resolved'. Correction by *new entry*, not mutation (§8).
    """

    error_class: str = ""
    message: str = ""
    where: str = ""    # file:line if known, else empty
    status: str = "open"  # "open" / "investigating" / "resolved"
    recovery_attempts: int = 0
    linked_tool_call_id: str = ""


@dataclass(frozen=True)
class DecisionEntry(_EntryCommon):
    """A structured authorization moment.

    Captures plan approvals, inline-permission allows, orange-PR merges,
    task-board closes, and any other §1 authorization event. Includes
    the reviewer identity and the structured facts that were reviewed
    (paths, hash, rule_id) — never raw model text.
    """

    decision_type: str = ""    # "plan_approval" / "inline_allow" / "orange_merge" / ...
    outcome: str = ""          # "approved" / "rejected" / "expired" / "paused"
    reviewer: str = ""
    rule_id: str = ""
    approved_paths: Tuple[str, ...] = ()
    candidate_hash: str = ""
    operator_note: str = ""


@dataclass(frozen=True)
class QuestionEntry(_EntryCommon):
    """An ``ask_human`` question (or §1 clarification) and its answer.

    Open questions are first-class state. Slice 2's scorer boosts the
    preservation score of any dialogue chunk that references an open
    question's file / tool / topic — "we were debugging X" survives
    compaction even after 30 rounds.
    """

    question: str = ""
    answer: str = ""              # "" until answered
    asked_by: str = "model"       # "model" or "orchestrator"
    answered_at_iso: str = ""
    related_paths: Tuple[str, ...] = ()
    related_tools: Tuple[str, ...] = ()
    status: str = "open"          # "open" / "answered" / "withdrawn"


# Union alias (the storage keeps heterogeneous entries)
LedgerEntry = _EntryCommon  # every concrete type extends this


# ---------------------------------------------------------------------------
# Id / hash helpers
# ---------------------------------------------------------------------------


def _utc_iso_now() -> Tuple[float, str]:
    ts = time.time()
    return ts, datetime.fromtimestamp(ts, tz=timezone.utc) \
        .replace(microsecond=0).isoformat()


def _make_entry_id(kind: str, op_id: str, extra: str = "") -> str:
    """Deterministic-ish short id: ``<kind-letter>-<short-hash>``.

    Collisions are vanishingly unlikely within one op's ledger (bounded
    size), and the op_id scopes the id anyway — ledgers from different
    ops never share a namespace.
    """
    seed = f"{op_id}\0{kind}\0{extra}\0{time.time_ns()}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:10]
    prefix = {
        "file_read": "f",
        "tool_call": "t",
        "error": "e",
        "decision": "d",
        "question": "q",
    }.get(kind, "x")
    return f"{prefix}-{digest}"


def _hash_content(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


def _fingerprint(value: Any) -> str:
    """Stable short fingerprint for arbitrary JSON-serialisable args.

    Never raises — falls back to repr() for non-JSON objects.
    """
    try:
        import json as _json
        serialised = _json.dumps(value, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        serialised = repr(value)
    return hashlib.sha256(serialised.encode()).hexdigest()[:12]


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: max(1, n - 3)] + "..."


# ---------------------------------------------------------------------------
# Per-op ledger
# ---------------------------------------------------------------------------


class ContextLedger:
    """Append-only structured fact store, scoped to one ``op_id``.

    Thread-safe. Per-kind LRU caps prevent unbounded growth on runaway
    ops. Emits structured listener events (Slice 4 bridges to SSE).
    """

    def __init__(
        self,
        op_id: str,
        *,
        max_entries_per_kind: Optional[int] = None,
    ) -> None:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        self._op_id = op_id
        self._cap = max_entries_per_kind or _max_entries_per_kind()
        self._lock = threading.Lock()
        # Per-kind storage; each list is insertion-ordered.
        self._entries: Dict[str, List[_EntryCommon]] = {
            k.value: [] for k in LedgerEntryKind
        }
        # Listener hooks (Slice 4 subscribes).
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # --- core write path -------------------------------------------------

    def _append(self, entry: _EntryCommon) -> _EntryCommon:
        kind = entry.kind
        with self._lock:
            bucket = self._entries.setdefault(kind, [])
            bucket.append(entry)
            # Enforce per-kind LRU cap.
            if len(bucket) > self._cap:
                dropped = bucket.pop(0)
                logger.debug(
                    "[ContextLedger] LRU evict op=%s kind=%s id=%s",
                    self._op_id, kind, dropped.entry_id,
                )
        self._fire("ledger_entry_added", entry)
        logger.info(
            "[ContextLedger] append op=%s kind=%s id=%s",
            self._op_id, kind, entry.entry_id,
        )
        return entry

    # --- typed writers ---------------------------------------------------

    def record_file_read(
        self,
        *,
        file_path: str,
        tool: str = "read_file",
        round_index: int = 0,
        content: Optional[bytes] = None,
        byte_size: Optional[int] = None,
    ) -> FileReadEntry:
        ts, iso = _utc_iso_now()
        body_hash = _hash_content(content) if content is not None else ""
        body_size = (
            byte_size if byte_size is not None
            else (len(content) if content is not None else 0)
        )
        entry = FileReadEntry(
            entry_id=_make_entry_id("file_read", self._op_id, file_path),
            op_id=self._op_id,
            kind=LedgerEntryKind.FILE_READ.value,
            created_at_ts=ts,
            created_at_iso=iso,
            file_path=file_path,
            tool=tool,
            round_index=round_index,
            byte_size=body_size,
            content_hash=body_hash,
        )
        self._append(entry)
        return entry

    def record_tool_call(
        self,
        *,
        tool: str,
        arguments: Any = None,
        round_index: int = 0,
        call_id: str = "",
        status: str = "",
        duration_ms: float = 0.0,
        output_bytes: int = 0,
        result_preview: str = "",
    ) -> ToolCallEntry:
        ts, iso = _utc_iso_now()
        entry = ToolCallEntry(
            entry_id=_make_entry_id("tool_call", self._op_id, call_id),
            op_id=self._op_id,
            kind=LedgerEntryKind.TOOL_CALL.value,
            created_at_ts=ts,
            created_at_iso=iso,
            tool=tool,
            args_fingerprint=_fingerprint(arguments),
            round_index=round_index,
            call_id=call_id,
            status=status,
            duration_ms=duration_ms,
            output_bytes=output_bytes,
            result_preview=_truncate(result_preview, 300),
        )
        self._append(entry)
        return entry

    def record_error(
        self,
        *,
        error_class: str,
        message: str,
        where: str = "",
        linked_tool_call_id: str = "",
        status: str = "open",
    ) -> ErrorEntry:
        ts, iso = _utc_iso_now()
        entry = ErrorEntry(
            entry_id=_make_entry_id(
                "error", self._op_id, f"{error_class}:{where}",
            ),
            op_id=self._op_id,
            kind=LedgerEntryKind.ERROR.value,
            created_at_ts=ts,
            created_at_iso=iso,
            error_class=error_class,
            message=_truncate(message, 500),
            where=where,
            status=status,
            recovery_attempts=0,
            linked_tool_call_id=linked_tool_call_id,
        )
        self._append(entry)
        return entry

    def record_decision(
        self,
        *,
        decision_type: str,
        outcome: str,
        reviewer: str = "",
        rule_id: str = "",
        approved_paths: Tuple[str, ...] = (),
        candidate_hash: str = "",
        operator_note: str = "",
    ) -> DecisionEntry:
        ts, iso = _utc_iso_now()
        entry = DecisionEntry(
            entry_id=_make_entry_id(
                "decision", self._op_id, f"{decision_type}:{outcome}",
            ),
            op_id=self._op_id,
            kind=LedgerEntryKind.DECISION.value,
            created_at_ts=ts,
            created_at_iso=iso,
            decision_type=decision_type,
            outcome=outcome,
            reviewer=reviewer,
            rule_id=rule_id,
            approved_paths=tuple(approved_paths),
            candidate_hash=candidate_hash,
            operator_note=_truncate(operator_note, 500),
        )
        self._append(entry)
        return entry

    def record_question(
        self,
        *,
        question: str,
        asked_by: str = "model",
        related_paths: Tuple[str, ...] = (),
        related_tools: Tuple[str, ...] = (),
    ) -> QuestionEntry:
        ts, iso = _utc_iso_now()
        entry = QuestionEntry(
            entry_id=_make_entry_id("question", self._op_id, question[:40]),
            op_id=self._op_id,
            kind=LedgerEntryKind.QUESTION.value,
            created_at_ts=ts,
            created_at_iso=iso,
            question=_truncate(question, 500),
            answer="",
            asked_by=asked_by,
            answered_at_iso="",
            related_paths=tuple(related_paths),
            related_tools=tuple(related_tools),
            status="open",
        )
        self._append(entry)
        return entry

    # --- immutable-correction writers ------------------------------------

    def record_question_answer(
        self,
        *,
        original_entry_id: str,
        answer: str,
    ) -> QuestionEntry:
        """Record an answer to an open question as a NEW entry.

        The original entry stays intact (§8 immutability). The new
        entry carries the answer + closes the referenced question's
        logical status via :meth:`query` which reports the latest
        status per question-id.
        """
        ts, iso = _utc_iso_now()
        with self._lock:
            original: Optional[QuestionEntry] = None
            for e in self._entries.get(LedgerEntryKind.QUESTION.value, []):
                if e.entry_id == original_entry_id \
                        and isinstance(e, QuestionEntry):
                    original = e
                    break
        if original is None:
            raise KeyError(f"no question entry with id {original_entry_id}")
        entry = QuestionEntry(
            entry_id=_make_entry_id(
                "question", self._op_id, original_entry_id + ":ans",
            ),
            op_id=self._op_id,
            kind=LedgerEntryKind.QUESTION.value,
            created_at_ts=ts,
            created_at_iso=iso,
            question=original.question,
            answer=_truncate(answer, 500),
            asked_by=original.asked_by,
            answered_at_iso=iso,
            related_paths=original.related_paths,
            related_tools=original.related_tools,
            status="answered",
        )
        self._append(entry)
        return entry

    def record_error_status(
        self,
        *,
        original_entry_id: str,
        new_status: str,
        recovery_attempts: Optional[int] = None,
    ) -> ErrorEntry:
        """Record an error's updated status as a NEW entry.

        Same §8 immutability contract as :meth:`record_question_answer`.
        Callers query :meth:`latest_error_status` for the current view.
        """
        ts, iso = _utc_iso_now()
        with self._lock:
            original: Optional[ErrorEntry] = None
            for e in self._entries.get(LedgerEntryKind.ERROR.value, []):
                if e.entry_id == original_entry_id \
                        and isinstance(e, ErrorEntry):
                    original = e
                    break
        if original is None:
            raise KeyError(f"no error entry with id {original_entry_id}")
        entry = ErrorEntry(
            entry_id=_make_entry_id(
                "error", self._op_id,
                f"{original_entry_id}:{new_status}",
            ),
            op_id=self._op_id,
            kind=LedgerEntryKind.ERROR.value,
            created_at_ts=ts,
            created_at_iso=iso,
            error_class=original.error_class,
            message=original.message,
            where=original.where,
            status=new_status,
            recovery_attempts=(
                recovery_attempts if recovery_attempts is not None
                else original.recovery_attempts + 1
            ),
            linked_tool_call_id=original.linked_tool_call_id,
        )
        self._append(entry)
        return entry

    # --- query API -------------------------------------------------------

    @property
    def op_id(self) -> str:
        return self._op_id

    def get_by_kind(self, kind: LedgerEntryKind) -> List[_EntryCommon]:
        with self._lock:
            return list(self._entries.get(kind.value, []))

    def get_since(self, since_ts: float) -> List[_EntryCommon]:
        """All entries (across kinds) strictly after *since_ts*, sorted."""
        with self._lock:
            out: List[_EntryCommon] = []
            for bucket in self._entries.values():
                out.extend(e for e in bucket if e.created_at_ts > since_ts)
            out.sort(key=lambda e: e.created_at_ts)
            return out

    def files_read(self) -> List[str]:
        with self._lock:
            return sorted({
                e.file_path  # type: ignore[attr-defined]
                for e in self._entries.get(
                    LedgerEntryKind.FILE_READ.value, [],
                )
                if getattr(e, "file_path", "")
            })

    def tools_used(self) -> List[str]:
        with self._lock:
            return sorted({
                e.tool  # type: ignore[attr-defined]
                for e in self._entries.get(
                    LedgerEntryKind.TOOL_CALL.value, [],
                )
                if getattr(e, "tool", "")
            })

    def open_errors(self) -> List[ErrorEntry]:
        """Returns the latest ErrorEntry per error_id whose status is not 'resolved'."""
        with self._lock:
            errors = [
                e for e in self._entries.get(
                    LedgerEntryKind.ERROR.value, [],
                )
                if isinstance(e, ErrorEntry)
            ]
        # Group by (error_class, where) — latest entry wins.
        grouped: Dict[Tuple[str, str], ErrorEntry] = {}
        for e in errors:
            grouped[(e.error_class, e.where)] = e
        return [
            e for e in grouped.values()
            if e.status != "resolved"
        ]

    def latest_error_status(
        self, *, error_class: str, where: str = "",
    ) -> Optional[str]:
        with self._lock:
            errors = [
                e for e in self._entries.get(
                    LedgerEntryKind.ERROR.value, [],
                )
                if isinstance(e, ErrorEntry)
                and e.error_class == error_class
                and e.where == where
            ]
        if not errors:
            return None
        errors.sort(key=lambda e: e.created_at_ts, reverse=True)
        return errors[0].status

    def open_questions(self) -> List[QuestionEntry]:
        """Questions with no subsequent 'answered' entry covering them."""
        with self._lock:
            questions = [
                e for e in self._entries.get(
                    LedgerEntryKind.QUESTION.value, [],
                )
                if isinstance(e, QuestionEntry)
            ]
        # A question is 'open' if its most recent status record is 'open'.
        # We match by the original question text (truncated).
        grouped: Dict[str, QuestionEntry] = {}
        for q in questions:
            grouped.setdefault(q.question, q)
            if q.created_at_ts > grouped[q.question].created_at_ts:
                grouped[q.question] = q
        return [q for q in grouped.values() if q.status == "open"]

    def approved_paths_so_far(self) -> FrozenSet[str]:
        with self._lock:
            decisions = [
                e for e in self._entries.get(
                    LedgerEntryKind.DECISION.value, [],
                )
                if isinstance(e, DecisionEntry) and e.outcome == "approved"
            ]
        out: set = set()
        for d in decisions:
            out.update(d.approved_paths)
        return frozenset(out)

    # --- summary projection (Slice 4 SSE / GET will reuse) --------------

    def summary(self) -> Dict[str, Any]:
        """Bounded, printable snapshot of the ledger.

        Safe to log / emit / project into SSE frames. Pure counting;
        no entry-body text leaks beyond the bounded ``latest_error`` /
        ``latest_question`` fields (already truncated at write time).
        """
        with self._lock:
            counts = {k: len(v) for k, v in self._entries.items()}
        latest_err = None
        open_errs = self.open_errors()
        if open_errs:
            open_errs.sort(key=lambda e: e.created_at_ts, reverse=True)
            le = open_errs[0]
            latest_err = {
                "error_class": le.error_class,
                "where": le.where,
                "message": le.message,
                "status": le.status,
            }
        latest_q = None
        open_qs = self.open_questions()
        if open_qs:
            open_qs.sort(key=lambda e: e.created_at_ts, reverse=True)
            lq = open_qs[0]
            latest_q = {
                "question": lq.question,
                "asked_by": lq.asked_by,
                "related_paths": list(lq.related_paths),
            }
        return {
            "schema_version": CONTEXT_LEDGER_SCHEMA_VERSION,
            "op_id": self._op_id,
            "counts_by_kind": counts,
            "files_read_count": len(self.files_read()),
            "tools_used_count": len(self.tools_used()),
            "open_errors_count": len(self.open_errors()),
            "open_questions_count": len(self.open_questions()),
            "approved_paths_count": len(self.approved_paths_so_far()),
            "latest_open_error": latest_err,
            "latest_open_question": latest_q,
        }

    # --- listener hooks (Slice 4 bridges to SSE) -------------------------

    def on_change(
        self, listener: Callable[[Dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe to ledger-change events. Returns an unsubscribe callback."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _fire(self, event_type: str, entry: _EntryCommon) -> None:
        payload = {
            "event_type": event_type,
            "entry_id": entry.entry_id,
            "op_id": self._op_id,
            "projection": self._project_entry(entry),
        }
        for l in list(self._listeners):
            try:
                l(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[ContextLedger] listener exception %s: %s",
                    event_type, exc,
                )

    @staticmethod
    def _project_entry(entry: _EntryCommon) -> Dict[str, Any]:
        """Flatten an entry to a sanitized JSON-serialisable dict.

        Slice 4 SSE payloads echo this directly. Every field is already
        bounded in size at write time so this is safe for network.
        """
        d = asdict(entry)
        # Tuples → lists for JSON.
        for k, v in list(d.items()):
            if isinstance(v, tuple):
                d[k] = list(v)
        return d


# ---------------------------------------------------------------------------
# Registry (per-op singletons)
# ---------------------------------------------------------------------------


class ContextLedgerRegistry:
    """Per-op ledger lookup with bounded retention.

    Registering more than ``max_ops`` ledgers evicts the oldest (not
    just the least-recently-written — eviction is by *registration*
    order, the same LRU-ish pattern :class:`PermissionClassifier`
    uses).
    """

    def __init__(self, *, max_ops: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._ledgers: Dict[str, ContextLedger] = {}
        self._max_ops = max_ops or _max_ops_retained()

    def get_or_create(self, op_id: str) -> ContextLedger:
        if not op_id:
            raise ValueError("op_id must be non-empty")
        with self._lock:
            ledger = self._ledgers.get(op_id)
            if ledger is not None:
                return ledger
            # Evict oldest if over cap.
            if len(self._ledgers) >= self._max_ops:
                oldest = next(iter(self._ledgers))
                evicted = self._ledgers.pop(oldest)
                logger.debug(
                    "[ContextLedger] registry evict op=%s entries=%s",
                    oldest,
                    {k: len(v) for k, v in evicted._entries.items()},
                )
            fresh = ContextLedger(op_id)
            self._ledgers[op_id] = fresh
        return fresh

    def get(self, op_id: str) -> Optional[ContextLedger]:
        with self._lock:
            return self._ledgers.get(op_id)

    def drop(self, op_id: str) -> bool:
        with self._lock:
            return self._ledgers.pop(op_id, None) is not None

    def active_op_ids(self) -> List[str]:
        with self._lock:
            return list(self._ledgers.keys())

    def reset(self) -> None:
        """Test helper."""
        with self._lock:
            self._ledgers.clear()


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_registry: Optional[ContextLedgerRegistry] = None
_registry_lock = threading.Lock()


def get_default_registry() -> ContextLedgerRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = ContextLedgerRegistry()
        return _default_registry


def reset_default_registry() -> None:
    global _default_registry
    with _registry_lock:
        if _default_registry is not None:
            _default_registry.reset()
        _default_registry = None


def ledger_for(op_id: str) -> ContextLedger:
    """Convenience — op-scoped ledger via the module singleton."""
    return get_default_registry().get_or_create(op_id)


__all__ = [
    "CONTEXT_LEDGER_SCHEMA_VERSION",
    "ContextLedger",
    "ContextLedgerRegistry",
    "DecisionEntry",
    "ErrorEntry",
    "FileReadEntry",
    "LedgerEntryKind",
    "QuestionEntry",
    "ToolCallEntry",
    "get_default_registry",
    "ledger_for",
    "reset_default_registry",
]
