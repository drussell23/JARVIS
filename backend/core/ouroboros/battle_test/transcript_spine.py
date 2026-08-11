"""One ordered transcript, four vocabularies.

The cockpit addresses its history through four reference namespaces::

    o-N   op blocks        op_block_buffer     (JARVIS_OP_BLOCK_BUFFER_SIZE)
    d-N   diffs            diff_archive        (JARVIS_DIFF_ARCHIVE_SIZE)
    t-N   tool bodies      tool_render_store   (JARVIS_TOOL_RENDER_STORE_SIZE)
    n-N   narrative        narrative_channel   (its own ring)

Four bounded rings, four independent evictions, four different capacities,
unified only at the ``/expand`` dispatcher. Two consequences follow, and
neither is a bug in any single store:

**Dangling references.** ``o-12`` can outlive the ``t-7`` it mentions, because
the op-block ring holds 50 and the diff ring holds 30. Expanding the surviving
block offers a reference into a store that has already forgotten it. The
lookup returns ``None`` — graceful, and still a transcript that points at
something no longer there.

**No ordering between namespaces.** Each ring knows the order of its OWN
entries. Nothing knows whether ``t-7`` happened before or after ``n-4``. There
is no answer to "what happened next", because "next" spans namespaces and no
structure spans namespaces.

This is the spine those four become views of. It does not replace them: they
keep their types, their rendering and their public APIs. What moves here is
ORDER and RETENTION — the two things that have to be shared to be coherent.

Append-only, and why that is the design rather than a ring
-----------------------------------------------------------
A record, once appended, is never mutated and never moved. That single
property answers the concurrency question outright: a reader holding a
sequence range cannot have it invalidated, because nothing rewrites what it
is looking at. New work becomes visible to the NEXT read, never inside the
current one.

So a background agent appending while the operator scrolls is not a race to
be locked against — the two are appends and reads on disjoint sequence
ranges, ordered by ``seq`` rather than by a mutex. There is no lock on the
read path at all, and the write path takes one only to advance the counter.

Retention is derived, not chosen
--------------------------------
The spine's capacity is the SUM of what the four stores were already allowed
to hold, read from their own modules at call time. That is not a new limit:
it is exactly the union of the existing ones, so nothing that fits today is
evicted tomorrow. Change ``JARVIS_DIFF_ARCHIVE_SIZE`` and the spine follows,
with no second knob to keep in sync.

Eviction is then uniform — oldest ``seq`` first, across every kind — which is
what makes a dangling reference structurally impossible rather than handled:
if ``o-12`` is live, everything appended before it is live too.

Slice 1 of 3. This slice is memory-only and adds no persistence: the spine
dies with the daemon exactly as the four rings do today. Durability (append
log + torn-record recovery + atomic compaction) and memory tiering (a
``MemoryPressureGate`` consumer) are separate slices, because a mistake in
either is invisible until a crash and wants its own kill test.
"""
from __future__ import annotations

import itertools
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.TranscriptSpine")

__all__ = [
    "SpineRecord",
    "TranscriptSpine",
    "get_default_spine",
    "reset_default_spine",
    "known_prefixes",
]


def known_prefixes() -> Dict[str, str]:
    """``{prefix: kind}`` read from the STORES, never restated here.

    ``o-`` / ``d-`` / ``t-`` / ``n-`` are already public constants on the four
    modules. Copying them into this file would create a second definition that
    drifts the first time one is renamed — the defect `chat_response_style`
    avoids by reading `LABEL_PREFIX` off the executor rather than restating
    ``"logged-"``.

    A module that cannot be imported is skipped rather than defaulted: a
    missing store means that vocabulary is not in play, and inventing its
    prefix would let the spine claim references nothing can resolve.
    """
    out: Dict[str, str] = {}
    for kind, module in (
        ("op_block", "op_block_buffer"),
        ("diff", "diff_archive"),
        ("tool_body", "tool_render_store"),
        ("narrative", "narrative_channel"),
        # Slice 2: APPLY / VERIFY / commit milestones, arriving through
        # the existing ops-digest fan-out. A fifth vocabulary, so `m-4`
        # resolves and `was_evicted` can tell "aged out" from "never
        # existed" for it too.
        ("milestone", "transcript_milestones"),
    ):
        try:
            mod = __import__(
                f"backend.core.ouroboros.battle_test.{module}",
                fromlist=["REF_PREFIX"],
            )
            prefix = getattr(mod, "REF_PREFIX", "")
            if isinstance(prefix, str) and prefix:
                out[prefix] = kind
        except Exception:  # noqa: BLE001 — an absent store is not an error
            continue
    return out


def _store_capacity(module: str) -> int:
    """One store's configured capacity, read from the store. NEVER raises.

    Discovered by CONVENTION rather than by name: all four stores publish an
    env-var NAME ending ``_SIZE_ENV_VAR`` and a private ``_DEFAULT_*_SIZE``
    beside it. Reading that pair means a rename or a re-tuning carries here
    automatically, and there is no second copy of "how big is the diff ring"
    to fall out of step.

    Returns 0 when the module or the convention is absent — the caller treats
    that as "unknown", never as "zero".
    """
    try:
        mod = __import__(
            f"backend.core.ouroboros.battle_test.{module}", fromlist=["*"],
        )
        env_var = next(
            (getattr(mod, n) for n in dir(mod)
             if n.endswith("SIZE_ENV_VAR")
             and isinstance(getattr(mod, n, None), str)),
            "",
        )
        default = next(
            (getattr(mod, n) for n in dir(mod)
             if n.startswith("_DEFAULT_") and n.endswith("SIZE")
             and isinstance(getattr(mod, n, None), int)),
            0,
        )
        if not env_var and not default:
            return 0
        raw = os.environ.get(env_var, "").strip() if env_var else ""
        return max(0, int(raw) if raw else int(default))
    except (TypeError, ValueError):
        return 0
    except Exception:  # noqa: BLE001
        return 0


def _derived_capacity() -> int:
    """The union of what the four stores already hold. NEVER raises.

    Summing their existing capacities means unifying retention cannot evict
    anything that survives today: the spine can retain everything the four
    rings could retain between them.

    Returns 0 when nothing can be read, which callers MUST treat as
    "unbounded" rather than "empty" — refusing to retain because a capacity
    could not be read would silently discard the transcript.
    """
    return sum(
        _store_capacity(m) for m in (
            "op_block_buffer", "diff_archive",
            "tool_render_store", "narrative_channel",
            # A fifth producer that added no capacity would make the
            # spine evict SOONER than the union it promises. Milestones
            # publish their own budget under the same convention, so the
            # invariant "nothing that fits today is evicted tomorrow"
            # survives their arrival.
            "transcript_milestones",
        )
    )


@dataclass(frozen=True)
class SpineRecord:
    """One entry in the transcript. Frozen: append-only means a record is
    never rewritten, and freezing makes that a type error rather than a
    convention a future edit can quietly break."""

    seq: int
    kind: str
    ref: str
    op_id: str = ""
    #: The store's own object. Held by reference, not copied — the stores
    #: remain the owners of their types and their rendering.
    payload: Any = None

    def to_dict(self, *, include_payload: bool = False) -> Dict[str, Any]:
        """Reuses the payload's OWN ``to_dict`` when it has one, so a record
        serialises exactly as its store already serialises. NEVER raises."""
        out: Dict[str, Any] = {
            "seq": self.seq, "kind": self.kind, "ref": self.ref,
        }
        if self.op_id:
            out["op_id"] = self.op_id
        if include_payload and self.payload is not None:
            try:
                fn = getattr(self.payload, "to_dict", None)
                if callable(fn):
                    out["payload"] = fn()
                elif isinstance(
                    self.payload, (dict, list, tuple, str, int, float, bool),
                ):
                    # Already representable. The previous unconditional
                    # ``str()`` turned a dict payload into a PYTHON REPR
                    # — lossy, and unparseable by anything downstream.
                    # Harmless while this was render-only; a real defect
                    # the moment a payload is persisted and read back.
                    out["payload"] = self.payload
                else:
                    out["payload"] = str(self.payload)
            except Exception:  # noqa: BLE001
                out["payload"] = "<unrenderable>"
        return out


@dataclass
class TranscriptSpine:
    """Append-only, sequence-ordered, with the four namespaces as indices.

    Thread-safe by construction rather than by discipline: the only mutation
    is an append under a short lock, and every read works from an immutable
    snapshot taken in O(1).
    """

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _records: List[SpineRecord] = field(default_factory=list, repr=False)
    _by_ref: Dict[str, SpineRecord] = field(default_factory=dict, repr=False)
    _seq: Any = field(default_factory=lambda: itertools.count(1), repr=False)
    #: Sequence of the oldest record ever evicted, so a caller can tell
    #: "never existed" from "aged out" — different answers to an operator.
    _evicted_through: int = 0
    evicted_total: int = 0
    #: Slice 2 durability tap. Invoked INSIDE the append lock — see
    #: :meth:`attach_sink` for why that is load-bearing rather than lazy.
    _sink: Optional[Any] = field(default=None, repr=False)
    sink_failures: int = 0

    # -- write path ---------------------------------------------------------

    def append(self, kind: str, ref: str, payload: Any = None,
               op_id: str = "") -> Optional[SpineRecord]:
        """Record one transcript event. NEVER raises.

        Returns the record, or ``None`` when the input is unusable — a
        transcript that accepts a blank reference cannot resolve it later, so
        refusing is the honest outcome.
        """
        try:
            k, r = str(kind or "").strip(), str(ref or "").strip()
            if not k or not r:
                return None
            with self._lock:
                rec = SpineRecord(
                    seq=next(self._seq), kind=k, ref=r,
                    op_id=str(op_id or ""), payload=payload,
                )
                self._records.append(rec)
                # Last write wins for a re-used ref: a store that re-admits
                # the same reference means the newer object, and the spine
                # must resolve to what the store would.
                self._by_ref[r] = rec
                self._retain_locked()
                self._emit_to_sink_locked(rec)
                return rec
        except Exception:  # noqa: BLE001 — the transcript must never break a render
            logger.debug("[Spine] append degraded", exc_info=True)
            return None

    def attach_sink(self, sink: Optional[Any]) -> None:
        """Tap every append, for durability. ``None`` detaches.

        The sink is called **inside the append lock**, and that is the
        whole design rather than an oversight to optimise away. ``seq``
        is minted under this lock; if the sink were called outside it,
        two concurrent appends could reach the log in the opposite order
        to their sequence numbers, and ``recover_log`` would correctly
        report ``NON_MONOTONIC_SEQ`` and end the trustworthy prefix
        there — losing the transcript to a race that only ever existed
        because the notification escaped the lock that ordered it.

        The contract that makes this safe: a sink MUST be O(1) and
        non-blocking. :meth:`DurableLogWriter.submit` is a queue put; a
        sink that touched a disk here would serialise every render
        behind it.
        """
        with self._lock:
            self._sink = sink

    def _emit_to_sink_locked(self, rec: "SpineRecord") -> None:
        """Fail-soft by construction: a transcript that cannot be
        PERSISTED must not stop it being RECORDED. Failures are counted
        so "durability is off" never has to be inferred from silence."""
        sink = self._sink
        if sink is None:
            return
        try:
            sink(rec)
        except Exception:  # noqa: BLE001
            self.sink_failures += 1
            logger.debug("[Spine] durability sink degraded", exc_info=True)

    def _retain_locked(self) -> None:
        """Uniform eviction, oldest sequence first. Caller holds the lock.

        ONE policy across every kind is the whole point: if ``o-12`` is live,
        everything appended before it is live too, so a reference held by a
        surviving record cannot point at an evicted one.
        """
        cap = _derived_capacity()
        if cap <= 0:
            return                        # unreadable capacity -> unbounded
        overflow = len(self._records) - cap
        if overflow <= 0:
            return
        for rec in self._records[:overflow]:
            # Only drop the index entry if it still points at THIS record —
            # a re-admitted ref has a newer record that must survive.
            if self._by_ref.get(rec.ref) is rec:
                self._by_ref.pop(rec.ref, None)
            self._evicted_through = max(self._evicted_through, rec.seq)
        del self._records[:overflow]
        self.evicted_total += overflow

    # -- read path (no lock held during iteration) --------------------------

    def _snapshot(self) -> Tuple[SpineRecord, ...]:
        """An immutable view, taken in O(1) under the lock.

        Readers iterate the tuple, not the list. An append during iteration
        extends the list and leaves the tuple untouched, which is why the
        read path needs no lock and cannot observe a partial write.
        """
        with self._lock:
            return tuple(self._records)

    def resolve(self, ref: str) -> Optional[SpineRecord]:
        """Look up one reference. NEVER raises."""
        try:
            with self._lock:
                return self._by_ref.get(str(ref or "").strip())
        except Exception:  # noqa: BLE001
            return None

    def was_evicted(self, ref: str) -> bool:
        """True when a well-formed reference is gone rather than never seen.

        "``d-3`` aged out of a 30-entry transcript" and "``d-3`` never
        existed" are different facts, and an operator who typed a ref they
        read a minute ago deserves the first answer rather than a bare miss.
        """
        try:
            r = str(ref or "").strip()
            if not r or self.resolve(r) is not None:
                return False
            prefix = next(
                (p for p in known_prefixes() if r.startswith(p)), "",
            )
            if not prefix:
                return False              # not a transcript ref at all
            return self._evicted_through > 0
        except Exception:  # noqa: BLE001
            return False

    def page(self, after_seq: int = 0, limit: Optional[int] = None,
             kind: Optional[str] = None) -> List[SpineRecord]:
        """Records after ``after_seq``, newest last. NEVER raises.

        Sequence-keyed rather than offset-keyed, so a page stays stable while
        the spine grows: ``after_seq`` names a position in the transcript, not
        a position in a list that eviction can shift underneath the caller.

        ``limit=None`` means "everything after", which is what a cockpit
        catching up on reattach asks for.
        """
        try:
            snap = self._snapshot()
            out = [
                r for r in snap
                if r.seq > int(after_seq or 0)
                and (kind is None or r.kind == kind)
            ]
            if limit is not None and int(limit) >= 0:
                out = out[: int(limit)]
            return out
        except Exception:  # noqa: BLE001
            return []

    def tail(self, limit: int) -> List[SpineRecord]:
        """The most recent ``limit`` records, oldest first."""
        try:
            snap = self._snapshot()
            n = max(0, int(limit))
            return list(snap[-n:]) if n else []
        except Exception:  # noqa: BLE001
            return []

    def __iter__(self) -> Iterator[SpineRecord]:
        return iter(self._snapshot())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def head_seq(self) -> int:
        """Sequence of the newest record, or 0 when empty. The value a
        reattaching cockpit stores so its next `page()` resumes exactly
        where it stopped."""
        snap = self._snapshot()
        return snap[-1].seq if snap else 0

    def snapshot_stats(self) -> Dict[str, Any]:
        """Observable state — §7. NEVER raises."""
        try:
            snap = self._snapshot()
            kinds: Dict[str, int] = {}
            for r in snap:
                kinds[r.kind] = kinds.get(r.kind, 0) + 1
            return {
                "records": len(snap),
                "capacity": _derived_capacity(),
                "head_seq": snap[-1].seq if snap else 0,
                "evicted_total": self.evicted_total,
                "evicted_through": self._evicted_through,
                "by_kind": kinds,
                "namespaces": known_prefixes(),
            }
        except Exception:  # noqa: BLE001
            return {}


# Process-wide default, same shape as `set_default_intake_router` and
# `register_stream_renderer`: the daemon owns one, every consumer asks.
_DEFAULT: Optional[TranscriptSpine] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_spine() -> TranscriptSpine:
    """The process transcript. Constructed on first use. NEVER raises."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TranscriptSpine()
        return _DEFAULT


def record_event(kind: str, ref: str, op_id: str = "") -> None:
    """Note one transcript event on the process spine. NEVER raises.

    THE call site for the four stores. Each mints its reference the same way
    (``ref = f"{REF_PREFIX}{self._next_seq}"``) and calls this immediately
    after, so ordering across namespaces is captured at the moment it is
    knowable — a shared helper rather than four copies of the same hook.

    Deliberately payload-free in slice 1. The spine owns ORDER and RETENTION;
    the stores keep their types and their rendering. Passing the object here
    would duplicate ownership before there is any consumer that needs it.

    Fail-soft by construction: a transcript that cannot record an event must
    not prevent the store from admitting it. Losing an ordering entry
    degrades the spine; raising here would degrade the cockpit.
    """
    try:
        get_default_spine().append(kind, ref, op_id=op_id)
    except Exception:  # noqa: BLE001
        pass


def reset_default_spine() -> None:
    """Drop the process transcript — tests only."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None
