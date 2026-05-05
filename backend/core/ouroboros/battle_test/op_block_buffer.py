"""OpBlockBuffer — per-op buffered rendering with deferred commit.
==================================================================

Slice 2 of the **Gap #3 closure arc** (collapsible op blocks).

Root problem
------------

Today's render path emits each op's output line-by-line as the op
progresses through phases — typically 8-15 lines per op (header,
phase markers, tool calls, validation, diff, footer). With a swarm
of concurrent ops, the log becomes a fast-scrolling unstructured
stream where retrospective inspection requires scrollback discipline.

Claude Code's UX collapses each completed op into a single
``⏺ Update(file.py)`` summary line that the operator can expand
on-demand. We want the same: per-op output buffered into a private
list, late-committed at op terminal phase as a single collapsed
summary, retrievable in full via ``/expand <op-id>`` (Slice 3).

Substrate scope
---------------

This module ships the **storage primitive only**:

* :class:`OpBlock` frozen dataclass with the buffered lines + state
* Closed 3-value :class:`OpBlockState` (``BUFFERING`` / ``COMMITTED``
  / ``EXPANDED``)
* :class:`OpBlockBuffer` thread-safe FIFO ring with monotonic ``o-N``
  refs (NEVER reused — mirrors ``BoundedBodyStore`` and ``DiffArchive``
  safety contracts from Gap #2 + Gap #4)
* Mutating API: ``start_op``, ``append``, ``commit``, ``mark_expanded``
* Query API: ``lookup``, ``find_by_op_id``, ``list_recent``,
  ``all_refs``

Slice 3 wires the buffer into the existing render hooks
(``op_tool_call`` / ``op_validation`` / ``show_diff`` / etc.) and
adds the ``/expand`` REPL verb.

Architectural reuse — zero duplication
---------------------------------------

* :class:`BoundedBodyStore` (Gap #2 Slice 3) — same monotonic-ref ring
  pattern; we don't subclass to keep type-narrow APIs but the design
  language is intentionally identical
* :class:`DiffArchive` (Gap #4 Slice 1) — terminal-frozen state
  semantics are mirrored here (once COMMITTED, ``append`` is rejected)
* Frozen dataclass + closed enum + ``schema_version`` house style

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no I/O, no Console
* §7 fail-closed — every public method has a documented degradation
  (unknown op_id → no-op or ``None``); NEVER raises
* §8 observable — :class:`BufferSnapshot` projection ready for
  ``GET /observability/op-blocks`` (graduation slice follow-up)

What this module does NOT do
----------------------------

* Render anything — the lines are stored as plain strings (Rich
  markup pre-applied by the caller). Slice 3 wiring layer routes the
  caller's output here instead of straight to ``Console.print``.
* Decide collapse policy — Slice 3 chooses when to ``commit``. This
  module just stores until told.
* Handle the ``/expand`` REPL verb — that's Slice 3.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.OpBlockBuffer")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


OP_BLOCK_BUFFER_SCHEMA_VERSION: str = "op_block_buffer.v1"


BUFFER_SIZE_ENV_VAR: str = "JARVIS_OP_BLOCK_BUFFER_SIZE"


_DEFAULT_BUFFER_SIZE: int = 50
_MIN_BUFFER_SIZE: int = 1
_MAX_BUFFER_SIZE: int = 5_000


REF_PREFIX: str = "o-"


# ===========================================================================
# Closed taxonomy — block lifecycle
# ===========================================================================


class OpBlockState(str, enum.Enum):
    """Closed 3-value lifecycle.

    ``BUFFERING`` is the only mutable state. Once ``COMMITTED``, the
    block is read-only; subsequent ``append`` calls are rejected.
    ``EXPANDED`` is a presentation-state marker (operator clicked
    ``/expand <ref>``) — doesn't change content, just observability.
    """

    BUFFERING = "buffering"
    COMMITTED = "committed"
    EXPANDED = "expanded"

    @classmethod
    def coerce(cls, raw: object) -> "OpBlockState":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.BUFFERING

    @property
    def is_terminal(self) -> bool:
        return self is not OpBlockState.BUFFERING


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class OpBlock:
    """One buffered op's rendering state.

    The ``lines`` tuple is the full, ordered sequence of rendered
    lines emitted during the op's lifecycle. ``summary_line`` is the
    one-liner shown in collapsed form (e.g.
    ``"⏺ Update(foo.py)  ⎿ +12/-3 in 2 files  [42s]"``).

    Frozen + hashable. Mutations replace-in-place via
    :func:`dataclasses.replace` from inside :meth:`OpBlockBuffer.*`
    methods.

    Fields
    ------
    * ``ref`` — opaque ``o-N`` handle.
    * ``op_id`` — orchestrator op id this block belongs to.
    * ``lines`` — full ordered tuple of rendered lines (Rich markup
      pre-applied by caller).
    * ``summary_line`` — collapsed-form one-liner; empty until
      :meth:`OpBlockBuffer.commit` is called.
    * ``state`` — :class:`OpBlockState`.
    * ``started_at`` — ``time.monotonic()`` at :meth:`start_op`.
    * ``committed_at`` — ``time.monotonic()`` at :meth:`commit`;
      ``0.0`` while buffering.
    """

    ref: str
    op_id: str
    lines: Tuple[str, ...] = ()
    summary_line: str = ""
    state: OpBlockState = OpBlockState.BUFFERING
    started_at: float = 0.0
    committed_at: float = 0.0
    schema_version: str = OP_BLOCK_BUFFER_SCHEMA_VERSION

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def duration_s(self) -> float:
        if self.committed_at <= 0.0:
            return 0.0
        return max(0.0, self.committed_at - self.started_at)

    def to_dict(self, *, include_lines: bool = False) -> Dict[str, object]:
        d: Dict[str, object] = {
            "ref": self.ref,
            "op_id": self.op_id,
            "summary_line": self.summary_line,
            "state": self.state.value,
            "started_at": self.started_at,
            "committed_at": self.committed_at,
            "duration_s": self.duration_s,
            "line_count": self.line_count,
            "schema_version": self.schema_version,
        }
        if include_lines:
            d["lines"] = list(self.lines)
        return d


@dataclass(frozen=True)
class BufferSnapshot:
    """Read-only projection of the buffer's state."""

    capacity: int
    size: int
    next_seq: int
    buffering_count: int
    committed_count: int
    expanded_count: int
    schema_version: str = OP_BLOCK_BUFFER_SCHEMA_VERSION

    @property
    def utilization(self) -> float:
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.size / self.capacity)


# ===========================================================================
# Helpers
# ===========================================================================


def _read_capacity_from_env() -> int:
    raw = os.environ.get(BUFFER_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_BUFFER_SIZE
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BUFFER_SIZE
    if parsed < _MIN_BUFFER_SIZE:
        return _MIN_BUFFER_SIZE
    if parsed > _MAX_BUFFER_SIZE:
        return _MAX_BUFFER_SIZE
    return parsed


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


# ===========================================================================
# OpBlockBuffer
# ===========================================================================


class OpBlockBuffer:
    """Thread-safe bounded FIFO of :class:`OpBlock` records.

    Active vs. parked blocks
    ------------------------
    The buffer holds BOTH currently-buffering blocks AND
    already-committed history. A parallel index ``self._active_op_ids
    -> ref`` lets ``append(op_id, line)`` do an O(1) lookup without
    walking the ring. When eviction drops a still-buffering block
    (rare — operator generated > capacity ops without any committing),
    the active-index entry is also pruned.

    Thread safety
    -------------
    Single :class:`threading.RLock` serializes all operations
    (consistent with :class:`BoundedBodyStore` and :class:`DiffArchive`).
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        cap = (
            _read_capacity_from_env()
            if capacity is None
            else max(_MIN_BUFFER_SIZE, min(_MAX_BUFFER_SIZE, int(capacity)))
        )
        self._capacity: int = cap
        self._items: "OrderedDict[str, OpBlock]" = OrderedDict()
        self._active_op_ids: Dict[str, str] = {}  # op_id → ref
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection -----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> BufferSnapshot:
        with self._lock:
            buffering = committed = expanded = 0
            for entry in self._items.values():
                if entry.state is OpBlockState.BUFFERING:
                    buffering += 1
                elif entry.state is OpBlockState.COMMITTED:
                    committed += 1
                elif entry.state is OpBlockState.EXPANDED:
                    expanded += 1
            return BufferSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
                buffering_count=buffering,
                committed_count=committed,
                expanded_count=expanded,
            )

    # ---- mutating API -------------------------------------------------

    def start_op(self, op_id: object) -> Optional[OpBlock]:
        """Begin buffering for ``op_id``. Returns the newly-created
        :class:`OpBlock` (state=BUFFERING).

        If the op was already started (and not yet committed), returns
        the existing block — start_op is idempotent for already-active
        ops.

        NEVER raises. Empty / non-string op_id returns ``None``.
        """
        op_id_safe = _safe_str(op_id)
        if not op_id_safe:
            return None
        with self._lock:
            existing_ref = self._active_op_ids.get(op_id_safe)
            if existing_ref is not None:
                existing = self._items.get(existing_ref)
                if existing is not None and existing.state is OpBlockState.BUFFERING:
                    return existing
            ref = f"{REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            block = OpBlock(
                ref=ref, op_id=op_id_safe,
                lines=(),
                state=OpBlockState.BUFFERING,
                started_at=time.monotonic(),
            )
            self._items[ref] = block
            self._active_op_ids[op_id_safe] = ref
            self._evict_if_needed()
            return block

    def append(self, op_id: object, line: object) -> bool:
        """Append a rendered line to the active block for ``op_id``.

        Returns ``True`` on success, ``False`` when:
          * ``op_id`` is not active (never started or already committed)
          * Inputs are non-string

        NEVER raises.
        """
        op_id_safe = _safe_str(op_id)
        line_safe = _safe_str(line)
        if not op_id_safe:
            return False
        with self._lock:
            ref = self._active_op_ids.get(op_id_safe)
            if ref is None:
                return False
            current = self._items.get(ref)
            if current is None or current.state is not OpBlockState.BUFFERING:
                return False
            updated = replace(
                current,
                lines=current.lines + (line_safe,),
            )
            self._items[ref] = updated
            return True

    def commit(
        self, op_id: object, summary_line: object,
    ) -> Optional[OpBlock]:
        """Late-commit the active block for ``op_id`` with the
        operator-visible collapsed ``summary_line``.

        Once committed, the block transitions to ``COMMITTED`` and
        further ``append`` calls are rejected. The active-index
        entry for ``op_id`` is removed (a future ``start_op`` for
        the same ``op_id`` issues a fresh ``o-N``).

        NEVER raises. Returns the committed block, or ``None`` for
        unknown op_id.
        """
        op_id_safe = _safe_str(op_id)
        summary_safe = _safe_str(summary_line)
        if not op_id_safe:
            return None
        with self._lock:
            ref = self._active_op_ids.pop(op_id_safe, None)
            if ref is None:
                return None
            current = self._items.get(ref)
            if current is None:
                return None
            if current.state is not OpBlockState.BUFFERING:
                # Already committed (concurrent commit?) — return the
                # last-known state so the caller can act idempotently.
                return current
            updated = replace(
                current,
                summary_line=summary_safe,
                state=OpBlockState.COMMITTED,
                committed_at=time.monotonic(),
            )
            self._items[ref] = updated
            return updated

    def mark_expanded(self, ref: object) -> Optional[OpBlock]:
        """Stamp ``EXPANDED`` state on a committed block.

        For observability only — content is unchanged. Operators
        running ``/expand <ref>`` route through here so the
        observability surface knows the block was looked at.
        """
        if not isinstance(ref, str):
            return None
        with self._lock:
            current = self._items.get(ref)
            if current is None:
                return None
            if current.state is not OpBlockState.COMMITTED:
                # Don't downgrade BUFFERING → EXPANDED (would lose
                # provisional state) or re-EXPAND (no-op).
                return current
            updated = replace(current, state=OpBlockState.EXPANDED)
            self._items[ref] = updated
            return updated

    def discard_active(self, op_id: object) -> Optional[OpBlock]:
        """Drop a still-buffering block (op cancelled / failed before
        terminal phase). Returns the discarded block or ``None``.

        Unlike :meth:`commit`, no summary_line is recorded; the
        block is removed from the buffer entirely.
        """
        op_id_safe = _safe_str(op_id)
        if not op_id_safe:
            return None
        with self._lock:
            ref = self._active_op_ids.pop(op_id_safe, None)
            if ref is None:
                return None
            return self._items.pop(ref, None)

    def clear(self) -> None:
        """Drop all blocks. Counter is NOT reset (matches the
        monotonic-ref safety contract)."""
        with self._lock:
            self._items.clear()
            self._active_op_ids.clear()

    # ---- query API ---------------------------------------------------

    def lookup(self, ref: object) -> Optional[OpBlock]:
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def find_by_op_id(self, op_id: object) -> Tuple[OpBlock, ...]:
        """All blocks (oldest → newest) for ``op_id``. May contain
        multiple entries if an op was started twice (e.g. retried
        after cancel)."""
        if not isinstance(op_id, str) or not op_id:
            return ()
        with self._lock:
            return tuple(
                e for e in self._items.values() if e.op_id == op_id
            )

    def list_recent(self, limit: int = 10) -> Tuple[OpBlock, ...]:
        """Newest → oldest, capped by ``limit``."""
        if not isinstance(limit, int) or limit <= 0:
            return ()
        with self._lock:
            entries = list(self._items.values())
        entries.reverse()
        return tuple(entries[:limit])

    def all_refs(self) -> Tuple[str, ...]:
        """All resident refs, oldest → newest."""
        with self._lock:
            return tuple(self._items.keys())

    def active_op_ids(self) -> Tuple[str, ...]:
        """Currently-buffering op ids."""
        with self._lock:
            return tuple(self._active_op_ids.keys())

    # ---- internals -----------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Drop oldest entries until back within capacity. If an
        evicted entry was still BUFFERING (active), prune the
        active-index entry too."""
        while len(self._items) > self._capacity:
            ref, evicted = self._items.popitem(last=False)
            if (
                evicted.state is OpBlockState.BUFFERING
                and self._active_op_ids.get(evicted.op_id) == ref
            ):
                self._active_op_ids.pop(evicted.op_id, None)


# ===========================================================================
# Module singleton
# ===========================================================================


_default_buffer: Optional[OpBlockBuffer] = None
_singleton_lock = threading.Lock()


def get_default_buffer() -> OpBlockBuffer:
    global _default_buffer
    with _singleton_lock:
        if _default_buffer is None:
            _default_buffer = OpBlockBuffer()
        return _default_buffer


def reset_default_buffer_for_tests() -> None:
    global _default_buffer
    with _singleton_lock:
        _default_buffer = None


__all__ = [
    "BUFFER_SIZE_ENV_VAR",
    "BufferSnapshot",
    "OP_BLOCK_BUFFER_SCHEMA_VERSION",
    "OpBlock",
    "OpBlockBuffer",
    "OpBlockState",
    "REF_PREFIX",
    "get_default_buffer",
    "reset_default_buffer_for_tests",
]
