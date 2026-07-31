"""Which memories actually reached the prompt, and why the rest did not.

Claude Code's ``/context`` answers one question O+V could not: not what is
remembered, but what LOADED. ``/memory`` reports a corpus and a routing flag,
and a corpus plus a flag is not evidence — this codebase has repeatedly
shipped a capability that existed, was flagged on, and reached nothing.

The gap is structural, not cosmetic. ``ModuleContextRouter`` ranks topics,
selects a few, renders a section, and returns. Everything it decided —
what was available, what lost on rank, what was cut by budget, what was
dropped as a ghost — exists only inside one stack frame and is gone by the
time anyone can ask. The organism computed the answer and dropped it one
frame short of the eye.

Modelled on the manifest that already exists
---------------------------------------------
``context_manifest.CompactionManifest`` solved this exact problem for the
compaction subsystem: per-item rows carrying a decision and a STRUCTURED
reason, bounded and append-only per op, with listener hooks bridged to SSE.
This is that pattern applied to memory admission, not a second invention —
same row/record/registry shape, same reason-code discipline, same
``context_observability_enabled`` gate.

It is deliberately NOT ``CompactionManifest`` itself. That class records a
``PreservationResult`` of scored dialogue chunks keyed by position in a
sequence; memory admission records documents keyed by content hash, carrying
a corpus provenance and a staleness reading it has no field for. Forcing one
into the other would couple two subsystems through a duck-type neither
satisfies.

Withheld, not just admitted
---------------------------
Rows are written for topics that did NOT make it, and that is most of the
value. "These three loaded" is a fact an operator can already infer from the
prompt; "this one lost to budget by 200 characters" and "this one was
withheld as an untracked ghost" are the facts that explain a bad generation.
An admission record that lists only admissions is a receipt, not a ledger.

Consumers are named
-------------------
Every record carries the CONSUMER that requested it — the main pipeline, or
one of the EXPLORE / REVIEW / PLAN / GENERAL subagents. Claude Code makes a
deliberate choice not to inherit conversation memory into subagents; O+V had
never made that choice either way, which means it was being made by accident
at four different call sites. Naming the consumer on every row turns an
accident into a policy that can be read off the ledger.

Reads authority, holds none
---------------------------
This records what the router decided. It cannot change a decision, cannot
re-rank, and is never consulted by the ranker — the moment a display becomes
an input, a postmortem tool becomes a policy engine. Same reason ``/posture``
keeps override as its only write surface.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.MemoryAdmission")

MEMORY_ADMISSION_SCHEMA_VERSION: str = "memory_admission.1"

__all__ = [
    "MEMORY_ADMISSION_SCHEMA_VERSION",
    "AdmissionDecision",
    "AdmissionReason",
    "AdmissionRecord",
    "AdmissionRow",
    "MemoryConsumer",
    "admission_enabled",
    "get_default_registry",
    "ledger_for",
    "latest_record",
    "record_admission",
    "render_admission_lines",
    "reset_default_registry",
]


def _flag(name: str, default: str = "1") -> bool:
    try:
        return os.environ.get(name, default).strip().lower() not in (
            "0", "false", "no", "off", "")
    except Exception:  # noqa: BLE001
        return True


def _num(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(float(
            os.environ.get(name, "").strip() or default))))
    except Exception:  # noqa: BLE001
        return default


def admission_enabled() -> bool:
    """``JARVIS_MEMORY_ADMISSION_ENABLED`` (default true).

    OFF makes :func:`record_admission` a no-op and every read surface report
    "not recorded" — which is honest, and distinct from "nothing loaded".
    """
    return _flag("JARVIS_MEMORY_ADMISSION_ENABLED", "1")


def _max_ops() -> int:
    """How many ops the registry retains. ``JARVIS_MEMORY_ADMISSION_MAX_OPS``."""
    return _num("JARVIS_MEMORY_ADMISSION_MAX_OPS", 64, 1, 4096)


def _max_rows() -> int:
    """Rows kept per record. ``JARVIS_MEMORY_ADMISSION_MAX_ROWS``.

    Withheld rows are the long tail — a 383-topic corpus produces 383 rows
    per op if nothing bounds it, and a ledger that costs more memory than the
    prompt it describes has inverted its own purpose. Admitted rows are never
    dropped by this cap; see :meth:`AdmissionRecord.of`.
    """
    return _num("JARVIS_MEMORY_ADMISSION_MAX_ROWS", 64, 4, 4096)


class MemoryConsumer(str, enum.Enum):
    """Who asked for the memory — the boundary CC scopes and O+V had not.

    ``UNKNOWN`` exists so a caller that has not been taught to declare itself
    produces a visibly undeclared row rather than silently borrowing the main
    pipeline's identity.
    """

    MAIN = "main"
    EXPLORE = "explore"
    REVIEW = "review"
    PLAN = "plan"
    GENERAL = "general"
    OPERATOR = "operator"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: Any) -> "MemoryConsumer":
        """Best-effort parse. NEVER raises."""
        try:
            if isinstance(value, cls):
                return value
            token = str(value or "").strip().lower()
            for member in cls:
                if member.value == token:
                    return member
        except Exception:  # noqa: BLE001
            pass
        return cls.UNKNOWN


class AdmissionDecision(str, enum.Enum):
    """Whether a topic's text reached the rendered section."""

    ADMITTED = "admitted"
    WITHHELD = "withheld"


class AdmissionReason(str, enum.Enum):
    """Exactly one per row. Callers add human detail via ``note``.

    Split into why-it-got-in and why-it-did-not because those answer
    different operator questions. "Semantic" tells you the ranker earned
    it; "budget_exhausted" tells you to raise a number.
    """

    # admitted
    STRUCTURAL_TARGET = "structural_target"
    STRUCTURAL_RELATED = "structural_related"
    SEMANTIC = "semantic"
    OPERATOR_PINNED = "operator_pinned"

    # withheld
    RANK_BELOW_CUTOFF = "rank_below_cutoff"
    MAX_TOPICS_REACHED = "max_topics_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DUPLICATE_PAYLOAD = "duplicate_payload"
    UNTRACKED_GHOST = "untracked_ghost"
    #: Withheld because a scoping POLICY said so — the REVIEW subagent's
    #: COMPLEMENT scope withholding what the parent already saw. Kept
    #: distinct from RANK_BELOW_CUTOFF because folding them together erases
    #: the only evidence that a boundary decision acted, leaving an operator
    #: to conclude the ranker simply disliked the topic.
    SCOPE_EXCLUDED = "scope_excluded"
    ORPHANED_SUBJECT = "orphaned_subject"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class AdmissionRow:
    """One topic's outcome in one routing pass."""

    source_id: str
    uri: str
    content_hash: str
    decision: AdmissionDecision
    reason: AdmissionReason
    score: float
    chars: int
    drift: str = "unknown"
    #: Score components, echoed for debugging the way ``ManifestRow.breakdown``
    #: echoes the scorer's detail.
    breakdown: Tuple[Tuple[str, float], ...] = ()
    note: str = ""

    @property
    def admitted(self) -> bool:
        return self.decision is AdmissionDecision.ADMITTED


@dataclass(frozen=True)
class AdmissionRecord:
    """One full routing pass — what the corpus offered and what got through."""

    pass_id: str
    op_id: str
    consumer: MemoryConsumer
    recorded_at_iso: str
    recorded_at: float
    rows: Tuple[AdmissionRow, ...]
    corpus_size: int
    corpus_provenance: str
    corpus_excluded: int
    considered: int
    admitted_count: int
    admitted_chars: int
    char_budget: int
    query: str = ""
    target_files: Tuple[str, ...] = ()
    rows_withheld_from_record: int = 0
    schema_version: str = MEMORY_ADMISSION_SCHEMA_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def budget_used_fraction(self) -> float:
        """Share of the char budget the admitted set consumed. NEVER raises."""
        try:
            return (self.admitted_chars / self.char_budget) if self.char_budget else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @classmethod
    def of(
        cls,
        *,
        op_id: str,
        consumer: MemoryConsumer,
        rows: Sequence[AdmissionRow],
        corpus_size: int,
        corpus_provenance: str,
        corpus_excluded: int,
        char_budget: int,
        query: str = "",
        target_files: Sequence[str] = (),
        extra: Optional[Dict[str, Any]] = None,
    ) -> "AdmissionRecord":
        """Build a record, bounding the row list without losing admissions.

        The cap drops WITHHELD rows only, lowest-score first. An admitted row
        is the answer to "what loaded", which is the question this whole
        module exists for; a ledger that elides one to save memory has
        answered the wrong question cheaply.
        """
        admitted = [r for r in rows if r.admitted]
        withheld = sorted((r for r in rows if not r.admitted),
                          key=lambda r: -r.score)
        room = max(0, _max_rows() - len(admitted))
        kept_withheld = withheld[:room]
        dropped = len(withheld) - len(kept_withheld)

        now = time.time()
        iso = datetime.fromtimestamp(now, timezone.utc).replace(
            microsecond=0).isoformat()
        pass_id = f"ma-{abs(hash((op_id, time.time_ns()))) & 0xFFFFFFFF:08x}"

        ordered = tuple(admitted) + tuple(kept_withheld)
        return cls(
            pass_id=pass_id,
            op_id=str(op_id or "unknown"),
            consumer=consumer,
            recorded_at_iso=iso,
            recorded_at=now,
            rows=ordered,
            corpus_size=int(corpus_size),
            corpus_provenance=str(corpus_provenance),
            corpus_excluded=int(corpus_excluded),
            considered=len(rows),
            admitted_count=len(admitted),
            admitted_chars=sum(r.chars for r in admitted),
            char_budget=int(char_budget),
            query=str(query or "")[:200],
            target_files=tuple(str(f) for f in list(target_files)[:16]),
            rows_withheld_from_record=dropped,
            extra=dict(extra or {}),
        )

    def as_payload(self) -> Dict[str, Any]:
        """JSON-safe projection for the observability router / SSE. NEVER raises."""
        try:
            return {
                "schema_version": self.schema_version,
                "pass_id": self.pass_id,
                "op_id": self.op_id,
                "consumer": self.consumer.value,
                "recorded_at_iso": self.recorded_at_iso,
                "corpus": {
                    "size": self.corpus_size,
                    "provenance": self.corpus_provenance,
                    "excluded": self.corpus_excluded,
                },
                "considered": self.considered,
                "admitted": self.admitted_count,
                "admitted_chars": self.admitted_chars,
                "char_budget": self.char_budget,
                "budget_used": round(self.budget_used_fraction, 4),
                "rows_withheld_from_record": self.rows_withheld_from_record,
                "query": self.query,
                "target_files": list(self.target_files),
                "rows": [
                    {
                        "uri": r.uri,
                        "hash": r.content_hash,
                        "decision": r.decision.value,
                        "reason": r.reason.value,
                        "score": round(r.score, 4),
                        "chars": r.chars,
                        "drift": r.drift,
                        "note": r.note,
                    }
                    for r in self.rows
                ],
                "extra": dict(self.extra),
            }
        except Exception:  # noqa: BLE001
            return {"schema_version": self.schema_version, "op_id": self.op_id,
                    "error": "projection failed"}


class AdmissionLedger:
    """Per-op append-only admission records. Thread-safe.

    One op can route memory more than once — CONTEXT_EXPANSION injects, and a
    GENERATE retry after an Iron Gate rejection routes again against a
    changed query. Keeping both is what makes "the retry saw different
    memory" a readable fact instead of a guess.
    """

    def __init__(self, op_id: str, *, max_records: int = 8) -> None:
        self._op_id = str(op_id or "unknown")
        self._cap = max(1, int(max_records))
        self._lock = threading.Lock()
        self._records: List[AdmissionRecord] = []
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    @property
    def op_id(self) -> str:
        return self._op_id

    def append(self, record: AdmissionRecord) -> AdmissionRecord:
        """Store *record*, evicting the oldest past the cap. NEVER raises."""
        listeners: List[Callable[[Dict[str, Any]], None]]
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._cap:
                del self._records[: len(self._records) - self._cap]
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record.as_payload())
            except Exception:  # noqa: BLE001
                logger.debug("[MemoryAdmission] listener raised", exc_info=True)
        return record

    def records(self) -> Tuple[AdmissionRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def latest(self) -> Optional[AdmissionRecord]:
        with self._lock:
            return self._records[-1] if self._records else None

    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(fn)


class AdmissionRegistry:
    """Bounded op_id -> :class:`AdmissionLedger`, insertion-ordered eviction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ledgers: Dict[str, AdmissionLedger] = {}
        #: Wall-clock of the newest record per op, so `latest_record()` can
        #: answer "the last op to route memory" without scanning payloads.
        self._recency: Dict[str, float] = {}

    def ledger_for(self, op_id: str) -> AdmissionLedger:
        key = str(op_id or "unknown")
        with self._lock:
            ledger = self._ledgers.get(key)
            if ledger is None:
                ledger = AdmissionLedger(key)
                self._ledgers[key] = ledger
                cap = _max_ops()
                while len(self._ledgers) > cap:
                    oldest = next(iter(self._ledgers))
                    self._ledgers.pop(oldest, None)
                    self._recency.pop(oldest, None)
            return ledger

    def note_recency(self, op_id: str, when: float) -> None:
        with self._lock:
            self._recency[str(op_id or "unknown")] = float(when)

    def most_recent(self) -> Optional[AdmissionRecord]:
        """The newest record across every op. NEVER raises.

        The cockpit's ``/context`` has no op_id to hand in — the operator is
        asking about whatever just happened. Tracking recency explicitly
        beats trusting dict order, which insertion-orders by FIRST touch and
        would name a long-running op that routed memory once at boot.
        """
        with self._lock:
            if not self._recency:
                return None
            op_id = max(self._recency.items(), key=lambda kv: kv[1])[0]
            ledger = self._ledgers.get(op_id)
        return ledger.latest() if ledger is not None else None


_default_registry: Optional[AdmissionRegistry] = None
_registry_lock = threading.Lock()


def get_default_registry() -> AdmissionRegistry:
    """The process-wide registry, created on first use. NEVER raises."""
    global _default_registry  # noqa: PLW0603
    with _registry_lock:
        if _default_registry is None:
            _default_registry = AdmissionRegistry()
        return _default_registry


def reset_default_registry() -> None:
    """Drop the registry. Test-only."""
    global _default_registry  # noqa: PLW0603
    with _registry_lock:
        _default_registry = None


def ledger_for(op_id: str) -> AdmissionLedger:
    return get_default_registry().ledger_for(op_id)


def latest_record() -> Optional[AdmissionRecord]:
    """The newest record anywhere, or None. NEVER raises."""
    try:
        return get_default_registry().most_recent()
    except Exception:  # noqa: BLE001
        return None


def record_admission(record: AdmissionRecord) -> Optional[AdmissionRecord]:
    """File *record*. Returns None when the surface is off. NEVER raises.

    The single write entry point, so the gate is checked once and the router
    never has to know whether the ledger exists.
    """
    if not admission_enabled():
        return None
    try:
        registry = get_default_registry()
        registry.ledger_for(record.op_id).append(record)
        registry.note_recency(record.op_id, record.recorded_at)
        logger.info(
            "[MemoryAdmission] op=%s consumer=%s corpus=%d/%s considered=%d "
            "admitted=%d chars=%d/%d",
            record.op_id, record.consumer.value, record.corpus_size,
            record.corpus_provenance, record.considered,
            record.admitted_count, record.admitted_chars, record.char_budget,
        )
        return record
    except Exception:  # noqa: BLE001
        logger.debug("[MemoryAdmission] record failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Read surface — composition only, no console
# ---------------------------------------------------------------------------


def render_admission_lines(
    record: Optional[AdmissionRecord] = None,
    *,
    verbose: bool = False,
) -> List[str]:
    """Markup lines for ``/memory context``. NEVER raises.

    Returns lines rather than printing so the daemon terminal and the attach
    cockpit render the same text through their own sinks — the reason
    ``/memory`` is mirrored at all, and the reason ~76 print-directly
    handlers are not.
    """
    if not admission_enabled():
        return ["  [dim]admission ledger disabled "
                "(JARVIS_MEMORY_ADMISSION_ENABLED=0)[/dim]"]
    try:
        rec = record if record is not None else latest_record()
        if rec is None:
            return [
                "  [bold]memory · context[/bold]",
                "    [dim]no routing pass recorded yet[/dim]",
                "    [dim]memory routes at CONTEXT_EXPANSION — this fills "
                "on the next op[/dim]",
            ]

        out: List[str] = [
            "  [bold]memory · context[/bold]  "
            f"[dim]{rec.op_id} · {rec.consumer.value} · "
            f"{rec.recorded_at_iso}[/dim]",
        ]

        degraded = rec.corpus_provenance != "git_tracked"
        corpus = (f"    corpus {rec.corpus_size} [{rec.corpus_provenance}]"
                  f"{' ⚠' if degraded else ''}")
        if rec.corpus_excluded:
            corpus += f" · {rec.corpus_excluded} untracked excluded"
        out.append(corpus)

        pct = int(round(rec.budget_used_fraction * 100))
        out.append(
            f"    admitted {rec.admitted_count}/{rec.considered} considered · "
            f"{rec.admitted_chars}/{rec.char_budget} chars ({pct}%)")

        admitted = [r for r in rec.rows if r.admitted]
        if admitted:
            out.append("  [dim]loaded[/dim]")
            for row in admitted:
                drift = "" if row.drift in ("fresh", "unbound") else \
                    f" [dim]{row.drift}[/dim]"
                out.append(f"    ✓ {row.uri} [dim]{row.reason.value} "
                           f"{row.score:.2f} {row.chars}c[/dim]{drift}")
        else:
            out.append("    [dim](nothing loaded — the prompt carried no "
                       "architecture memory)[/dim]")

        withheld = [r for r in rec.rows if not r.admitted]
        if withheld:
            # The tail is the diagnosis. Grouped by reason so "budget cut
            # nine of them" reads at a glance instead of as nine rows.
            by_reason: Dict[str, int] = {}
            for row in withheld:
                by_reason[row.reason.value] = by_reason.get(row.reason.value, 0) + 1
            out.append("  [dim]withheld[/dim]")
            if verbose:
                for row in withheld:
                    out.append(f"    · {row.uri} [dim]{row.reason.value} "
                               f"{row.score:.2f}[/dim]")
            else:
                out.append("    " + " · ".join(
                    f"{r} {n}" for r, n in
                    sorted(by_reason.items(), key=lambda kv: -kv[1])))
            if rec.rows_withheld_from_record:
                out.append(f"    [dim]… {rec.rows_withheld_from_record} further "
                           f"row(s) not retained[/dim]")

        if rec.query:
            out.append(f"  [dim]query[/dim] {rec.query}")
        if not verbose:
            out.append("  [dim]/memory context -v lists every withheld "
                       "topic[/dim]")
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MemoryAdmission] render degraded", exc_info=True)
        return [f"  [dim]admission surface degraded: "
                f"{type(exc).__name__}: {exc}[/dim]"]
