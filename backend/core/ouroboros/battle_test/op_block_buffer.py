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
    # §37 Tier 2 #12 (2026-05-07) — parent/child + fan-out
    # tracking. Backward-compat: every field defaults to a
    # neutral value so existing constructors (start_op + commit)
    # produce blocks byte-identical to pre-slice. Populated only
    # when callers invoke ``OpBlockBuffer.register_parent`` —
    # master-flag-gated.
    parent_op_id: str = ""
    """Empty when this op is a root (no parent). Set to the
    parent op's ``op_id`` when an op was spawned as a child
    (e.g., L3 subagent dispatch, recursive exploration agent,
    Move 6 K-way candidate)."""
    candidate_index: int = 0
    """0-indexed position in a sibling fan-out (e.g., Move 6
    K-way: candidate_index in [0, K)). Zero for root ops or
    single-child spawns."""
    subagent_kind: str = ""
    """One of ``explore`` / ``review`` / ``plan`` / ``general``
    when the op was dispatched as a typed subagent. Empty for
    direct orchestrator ops or recursive-exploration spawns."""
    child_op_ids: Tuple[str, ...] = ()
    """Op IDs spawned by this op (in spawn order). Mirrors the
    parent_op_id reverse-direction relationship — populated on
    the parent at ``register_parent`` time so subtree walks
    don't require a global reverse index."""

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def is_root(self) -> bool:
        """True when this op has no parent (top-level
        orchestrator op or session-bootstrapped op)."""
        return not self.parent_op_id

    @property
    def fan_out_size(self) -> int:
        """Number of direct children spawned by this op. Zero
        for leaf ops + ops that didn't spawn anything."""
        return len(self.child_op_ids)

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
            # §37 Tier 2 #12 — fan-out fields. Always emitted
            # (defaults to neutral values for non-fan-out ops).
            "parent_op_id": self.parent_op_id,
            "candidate_index": self.candidate_index,
            "subagent_kind": self.subagent_kind,
            "child_op_ids": list(self.child_op_ids),
            "is_root": self.is_root,
            "fan_out_size": self.fan_out_size,
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


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def op_dependency_graph_enabled() -> bool:
    """§37 Tier 2 #12 — master switch for op-dependency-graph
    tracking. ``JARVIS_OP_DEPENDENCY_GRAPH_ENABLED`` default-
    FALSE per §33.1: when off, ``OpBlockBuffer.register_parent``
    is a no-op (zero state churn pre-graduation). The frozen
    ``OpBlock`` fields (parent_op_id / candidate_index /
    subagent_kind / child_op_ids) ALWAYS exist (default
    neutral) — only the WRITE surface gates on this flag, so
    backward-compat readers see byte-identical behavior."""
    raw = os.environ.get(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


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

    # ---- §37 Tier 2 #12 — fan-out tracking ----------------------------

    def register_parent(
        self,
        *,
        child_op_id: object,
        parent_op_id: object,
        candidate_index: int = 0,
        subagent_kind: str = "",
    ) -> bool:
        """Stamp a parent→child relationship between two ops.

        Atomically updates BOTH endpoints under the buffer
        lock:
          * The child block's ``parent_op_id`` /
            ``candidate_index`` / ``subagent_kind`` fields.
          * The parent block's ``child_op_ids`` tuple
            (appended in order).

        Master-flag-gated: when
        ``JARVIS_OP_DEPENDENCY_GRAPH_ENABLED`` is off, the
        call is a no-op (zero state churn pre-graduation).
        Returns ``True`` on success, ``False`` when:
          * Master flag is off.
          * Either op_id is missing or unknown.
          * Either op has been evicted (out-of-window — best-
            effort tracking, not authoritative).

        Idempotent: re-registering the same (child, parent)
        pair is a no-op (no duplicate child entries).

        NEVER raises.
        """
        if not op_dependency_graph_enabled():
            return False
        child_safe = _safe_str(child_op_id)
        parent_safe = _safe_str(parent_op_id)
        if not child_safe or not parent_safe:
            return False
        if child_safe == parent_safe:
            # Self-parent is structurally invalid — defensive
            # rejection rather than crash.
            return False
        try:
            cidx = max(0, int(candidate_index))
        except (TypeError, ValueError):
            cidx = 0
        kind_safe = _safe_str(subagent_kind)
        with self._lock:
            child_block = self._find_block_by_op_id(child_safe)
            parent_block = self._find_block_by_op_id(
                parent_safe,
            )
            if child_block is None or parent_block is None:
                return False
            # Update child first.
            updated_child = replace(
                child_block,
                parent_op_id=parent_safe,
                candidate_index=cidx,
                subagent_kind=kind_safe,
            )
            self._items[child_block.ref] = updated_child
            # Append child to parent (idempotent).
            if child_safe not in parent_block.child_op_ids:
                updated_parent = replace(
                    parent_block,
                    child_op_ids=(
                        parent_block.child_op_ids + (child_safe,)
                    ),
                )
                self._items[parent_block.ref] = updated_parent
            return True

    def get_parent_op_id(self, op_id: object) -> str:
        """Return the parent op_id for ``op_id``, or ``""``
        when no parent registered / op unknown."""
        op_safe = _safe_str(op_id)
        if not op_safe:
            return ""
        with self._lock:
            block = self._find_block_by_op_id(op_safe)
            if block is None:
                return ""
            return block.parent_op_id

    def get_child_op_ids(self, op_id: object) -> Tuple[str, ...]:
        """Return the tuple of direct children for ``op_id``.
        Empty tuple when ``op_id`` is unknown or has no
        children."""
        op_safe = _safe_str(op_id)
        if not op_safe:
            return ()
        with self._lock:
            block = self._find_block_by_op_id(op_safe)
            if block is None:
                return ()
            return block.child_op_ids

    def find_root_ops(self) -> Tuple[OpBlock, ...]:
        """Return the tuple of all currently-buffered ops with
        no parent (oldest-first ordering preserved)."""
        with self._lock:
            return tuple(
                block for block in self._items.values()
                if block.is_root
            )

    def walk_subtree(
        self,
        op_id: object,
        *,
        max_depth: int = 16,
    ) -> Tuple[OpBlock, ...]:
        """Breadth-first walk rooted at ``op_id``. Returns the
        ops in BFS order (root first). Stops at ``max_depth``
        levels (default 16; clamped to [1, 64]) to bound the
        traversal regardless of fan-out shape. NEVER raises."""
        op_safe = _safe_str(op_id)
        if not op_safe:
            return ()
        try:
            depth_clamp = max(1, min(64, int(max_depth)))
        except (TypeError, ValueError):
            depth_clamp = 16
        with self._lock:
            root = self._find_block_by_op_id(op_safe)
            if root is None:
                return ()
            visited: set = {op_safe}
            ordered: list = [root]
            frontier: list = [(root, 0)]
            while frontier:
                current, depth = frontier.pop(0)
                if depth >= depth_clamp:
                    continue
                for child_id in current.child_op_ids:
                    if child_id in visited:
                        continue
                    visited.add(child_id)
                    child_block = self._find_block_by_op_id(
                        child_id,
                    )
                    if child_block is None:
                        continue
                    ordered.append(child_block)
                    frontier.append((child_block, depth + 1))
            return tuple(ordered)

    def _find_block_by_op_id(
        self, op_id: str,
    ) -> Optional[OpBlock]:
        """Resolve an op_id to its OpBlock. Searches the active
        index first (O(1)) then walks the buffer for committed
        blocks (O(N), bounded by capacity). Caller MUST hold
        ``self._lock``."""
        ref = self._active_op_ids.get(op_id)
        if ref is not None:
            block = self._items.get(ref)
            if block is not None:
                return block
        for block in reversed(self._items.values()):
            if block.op_id == op_id:
                return block
        return None

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


def register_shipped_invariants() -> list:
    """§37 Tier 2 #12 — auto-discovered AST pins:

      1. ``op_block_fan_out_fields_present`` — OpBlock dataclass
         carries the 4 fan-out fields (parent_op_id /
         candidate_index / subagent_kind / child_op_ids).
      2. ``op_dependency_master_flag_default_false`` — §33.1
         producer flag stays default-FALSE.
    """
    import ast as _ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/battle_test/op_block_buffer.py"
    )

    def _validate_fan_out_fields(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "parent_op_id", "candidate_index",
            "subagent_kind", "child_op_ids",
        }
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "OpBlock"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, _ast.AnnAssign):
                        target_node = stmt.target
                        if isinstance(target_node, _ast.Name):
                            seen.add(target_node.id)
                missing = required - seen
                if missing:
                    violations.append(
                        f"OpBlock missing §37 Tier 2 #12 "
                        f"fan-out fields: {sorted(missing)}"
                    )
                return tuple(violations)
        violations.append("OpBlock dataclass missing")
        return tuple(violations)

    def _validate_master_default_false(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef):
                if node.name == "op_dependency_graph_enabled":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "op_dependency_graph_enabled() helper missing"
            )
            return tuple(violations)
        empty_guard_returns_false = False
        for sub in _ast.walk(target_func):
            if not isinstance(sub, _ast.If):
                continue
            test = sub.test
            compares: list = []
            for st in _ast.walk(test):
                if isinstance(st, _ast.Compare):
                    compares.append(st)
            compares_empty_str = False
            for cmp_node in compares:
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], _ast.Eq,
                ):
                    continue
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, _ast.Constant)
                        and operand.value == ""
                    ):
                        compares_empty_str = True
                        break
                if compares_empty_str:
                    break
            if not compares_empty_str:
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, _ast.Return):
                    if (
                        isinstance(
                            body_stmt.value, _ast.Constant,
                        )
                        and body_stmt.value.value is False
                    ):
                        empty_guard_returns_false = True
                        break
            if empty_guard_returns_false:
                break
        if not empty_guard_returns_false:
            violations.append(
                "op_dependency_graph_enabled() MUST return "
                "False on empty env-var string per §33.1"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "op_block_fan_out_fields_present"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #12 — OpBlock carries the 4 fan-"
                "out fields (parent_op_id / candidate_index "
                "/ subagent_kind / child_op_ids) so /graph "
                "can render dependency canvas."
            ),
            validate=_validate_fan_out_fields,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "op_dependency_master_flag_default_false"
            ),
            target_file=target,
            description=(
                "§37 Tier 2 #12 — §33.1 producer flag stays "
                "default-FALSE; register_parent is a no-op "
                "until operator flips."
            ),
            validate=_validate_master_default_false,
        ),
    ]


__all__ = [
    "BUFFER_SIZE_ENV_VAR",
    "BufferSnapshot",
    "OP_BLOCK_BUFFER_SCHEMA_VERSION",
    "OpBlock",
    "OpBlockBuffer",
    "OpBlockState",
    "REF_PREFIX",
    "get_default_buffer",
    "op_dependency_graph_enabled",
    "register_shipped_invariants",
    "reset_default_buffer_for_tests",
]
