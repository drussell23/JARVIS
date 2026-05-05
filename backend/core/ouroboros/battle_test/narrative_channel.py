"""NarrativeChannel — the model's voice surface for proactive ops.
==================================================================

Slice 1 of the **Gap #6 closure arc** (proactive narrative).

Root problem
------------

Claude Code's UX feels alive because the assistant *narrates* its work
in continuous prose interleaved with tool calls. O+V is **proactive**
(sensors fire ops without operator input) so this need is even more
acute — the operator has no prior question to anchor context against.

Today the orchestrator captures the model's prose into structured
fields (rationales, plan blocks, postmortem analysis) but only streams
output during the GENERATE phase. PLAN reasoning, inter-tool-round
narrative, L2 repair planning text, and postmortem analysis prose all
get parsed into structured slots and stored — never flowing onto the
operator's screen.

This module supplies the **storage + streaming substrate** for a
"narrative channel": a parallel surface that captures the model's
voice across the whole op lifecycle and exposes it via:

  * Live streaming during emission (Slice 3 wires the renderer)
  * Bounded recovery via ``/expand n-N`` REPL verb (Slice 4)
  * SSE event for IDE consumption (Slice 4)

Substrate scope
---------------

* :class:`NarrativeFrame` frozen dataclass — one buffered narrative
  emission with phase + kind metadata
* Closed 6-value :class:`NarrativeKind` taxonomy
* Closed 3-value :class:`FrameState` lifecycle (BUFFERING / COMMITTED
  / DISCARDED)
* :class:`NarrativeChannel` thread-safe FIFO ring with monotonic
  ``n-N`` refs (NEVER reused — mirrors :class:`BoundedBodyStore` /
  :class:`DiffArchive` / :class:`OpBlockBuffer` safety contracts)
* Streaming API: ``start_frame`` → ``append_token`` → ``commit`` /
  ``discard`` — same shape as token streaming so providers can reuse
  the existing fan-out callback (Slice 2 wiring).

Architectural reuse — zero duplication
---------------------------------------

* Same monotonic-ref + drop-oldest + active-index-pruning pattern from
  :class:`OpBlockBuffer` (Gap #3 Slice 2). The structural differences
  are in the FIELDS (phase + kind metadata) and the POLICY (terminal
  via ``commit`` vs ``discard``, no expand-state).
* :class:`StreamRenderer`'s ``on_token`` lifecycle (Slice 2 wiring
  forwards provider tokens to ``append_token`` so the existing
  16ms-batched Live widget can drive narrative rendering with no
  parallel rendering surface).
* House style: frozen dataclass + closed enum + ``schema_version`` +
  module-owned ``register_flags`` / ``register_shipped_invariants``
  for auto-discovery.

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no I/O on the hot path
* §7 fail-closed — every public method has documented degradation
  (unknown frame ref → ``None``, append on terminal frame → no-op);
  NEVER raises into the streaming hot path
* §8 observable — :class:`ChannelSnapshot` projection for
  observability surfaces
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

logger = logging.getLogger("Ouroboros.NarrativeChannel")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


NARRATIVE_CHANNEL_SCHEMA_VERSION: str = "narrative_channel.v1"


BUFFER_SIZE_ENV_VAR: str = "JARVIS_NARRATIVE_BUFFER_SIZE"


_DEFAULT_BUFFER_SIZE: int = 200
_MIN_BUFFER_SIZE: int = 1
_MAX_BUFFER_SIZE: int = 10_000


REF_PREFIX: str = "n-"


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class NarrativeKind(str, enum.Enum):
    """Closed 6-value vocabulary for what the model is saying.

    Each kind maps to a distinct phase/context where the model emits
    natural-language prose. Closed taxonomy — adding a new kind
    requires a slice (the renderer in Slice 3 dispatches on this
    enum to choose a glyph/style).
    """

    INTENT = "intent"                       # op_started: "I'm going to fix X by..."
    PLAN_PROSE = "plan_prose"               # PLAN phase reasoning
    TOOL_PREAMBLE = "tool_preamble"         # "I'll read X first to understand Y"
    THINKING = "thinking"                   # extended-thinking REASONING_TOKEN content
    L2_REPAIR_PROSE = "l2_repair_prose"     # repair iteration narrative
    POSTMORTEM_PROSE = "postmortem_prose"   # failed-op analysis

    @classmethod
    def coerce(cls, raw: object) -> "NarrativeKind":
        """Lenient parse — anything not recognized becomes
        :data:`THINKING` (the most generic). NEVER raises."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for member in cls:
                if member.value == s:
                    return member
        return cls.THINKING


class FrameState(str, enum.Enum):
    """Closed 3-value frame lifecycle.

    ``BUFFERING`` is the only mutable state. ``COMMITTED`` means the
    model finished emitting; ``DISCARDED`` means the frame was
    abandoned (provider error / op cancelled / model returned no prose).
    """

    BUFFERING = "buffering"
    COMMITTED = "committed"
    DISCARDED = "discarded"

    @classmethod
    def coerce(cls, raw: object) -> "FrameState":
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
        return self is not FrameState.BUFFERING


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class NarrativeFrame:
    """One narrative emission.

    Frozen + hashable. Mutations replace-in-place via
    :func:`dataclasses.replace` from inside :meth:`NarrativeChannel.*`
    methods.

    Fields
    ------
    * ``ref`` — opaque ``n-N`` handle.
    * ``op_id`` — orchestrator op id (empty string for system-level
      narrative; rare).
    * ``phase`` — orchestrator phase string (e.g. ``"PLAN"``,
      ``"GENERATE"``, ``"L2_REPAIR"``). Stored as string for
      forward-compat with the orchestrator's enum.
    * ``kind`` — :class:`NarrativeKind` member.
    * ``provider`` — model provider id (``"claude"`` / ``"doubleword"``
      / ``"gcp-jprime"``). Empty for synthesized fallback preambles.
    * ``prose`` — accumulated text content (built up by ``append_token``
      while BUFFERING; finalized at ``commit``).
    * ``state`` — :class:`FrameState`.
    * ``started_at`` / ``terminal_at`` — ``time.monotonic()`` timestamps.
    """

    ref: str
    op_id: str
    phase: str
    kind: NarrativeKind
    provider: str
    prose: str
    state: FrameState
    started_at: float
    terminal_at: float
    schema_version: str = NARRATIVE_CHANNEL_SCHEMA_VERSION

    @property
    def char_count(self) -> int:
        return len(self.prose)

    @property
    def duration_s(self) -> float:
        if self.terminal_at <= 0.0:
            return 0.0
        return max(0.0, self.terminal_at - self.started_at)

    def to_dict(self, *, include_prose: bool = False) -> Dict[str, object]:
        d: Dict[str, object] = {
            "ref": self.ref,
            "op_id": self.op_id,
            "phase": self.phase,
            "kind": self.kind.value,
            "provider": self.provider,
            "state": self.state.value,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
            "duration_s": self.duration_s,
            "char_count": self.char_count,
            "schema_version": self.schema_version,
        }
        if include_prose:
            d["prose"] = self.prose
        return d


@dataclass(frozen=True)
class ChannelSnapshot:
    """Read-only projection of the channel's state."""

    capacity: int
    size: int
    next_seq: int
    buffering_count: int
    committed_count: int
    discarded_count: int
    schema_version: str = NARRATIVE_CHANNEL_SCHEMA_VERSION

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
# NarrativeChannel — the load-bearing class
# ===========================================================================


class NarrativeChannel:
    """Thread-safe bounded FIFO of :class:`NarrativeFrame` records.

    Active-frame index
    ------------------
    For O(1) ``append_token`` lookup, an index ``self._active_keys ->
    ref`` maps the composite key ``(op_id, phase, kind)`` to the
    current BUFFERING ref. Multiple parallel frames per op are
    supported as long as their (op, phase, kind) tuples differ —
    matches the real-world case where Claude emits THINKING tokens
    interleaved with PLAN_PROSE.

    Eviction
    --------
    Drop-oldest at capacity. If an evicted frame was still BUFFERING,
    the active-key entry is also pruned (preventing concurrent
    ``append_token`` from misrouting to a different op's frame).

    Thread safety
    -------------
    Single :class:`threading.RLock` serializes all operations.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        cap = (
            _read_capacity_from_env()
            if capacity is None
            else max(_MIN_BUFFER_SIZE, min(_MAX_BUFFER_SIZE, int(capacity)))
        )
        self._capacity: int = cap
        self._items: "OrderedDict[str, NarrativeFrame]" = OrderedDict()
        # composite key (op_id, phase, kind) → ref
        self._active_keys: Dict[Tuple[str, str, str], str] = {}
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection -----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> ChannelSnapshot:
        with self._lock:
            buf = comm = disc = 0
            for entry in self._items.values():
                if entry.state is FrameState.BUFFERING:
                    buf += 1
                elif entry.state is FrameState.COMMITTED:
                    comm += 1
                elif entry.state is FrameState.DISCARDED:
                    disc += 1
            return ChannelSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
                buffering_count=buf,
                committed_count=comm,
                discarded_count=disc,
            )

    # ---- mutating API -------------------------------------------------

    def start_frame(
        self,
        *,
        op_id: object,
        phase: object,
        kind: object,
        provider: object = "",
    ) -> Optional[NarrativeFrame]:
        """Begin buffering a new narrative frame. Returns the new
        :class:`NarrativeFrame` (state=BUFFERING) or the existing
        frame if one is already active for ``(op_id, phase, kind)``
        — start_frame is idempotent for in-flight emissions.

        Empty / non-string ``op_id`` / ``phase`` is allowed (system-
        level narrative). NEVER raises.
        """
        op_safe = _safe_str(op_id)
        phase_safe = _safe_str(phase)
        kind_enum = NarrativeKind.coerce(kind)
        provider_safe = _safe_str(provider)
        composite = (op_safe, phase_safe, kind_enum.value)

        with self._lock:
            existing_ref = self._active_keys.get(composite)
            if existing_ref is not None:
                existing = self._items.get(existing_ref)
                if existing is not None and existing.state is FrameState.BUFFERING:
                    return existing
            ref = f"{REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            frame = NarrativeFrame(
                ref=ref,
                op_id=op_safe,
                phase=phase_safe,
                kind=kind_enum,
                provider=provider_safe,
                prose="",
                state=FrameState.BUFFERING,
                started_at=time.monotonic(),
                terminal_at=0.0,
            )
            self._items[ref] = frame
            self._active_keys[composite] = ref
            self._evict_if_needed()
            return frame

    def append_token(
        self,
        *,
        op_id: object,
        phase: object,
        kind: object,
        token: object,
    ) -> bool:
        """Append a token chunk to the active frame for
        ``(op_id, phase, kind)``. Returns ``True`` on success,
        ``False`` if no active frame exists (e.g. terminal already)
        or the inputs are non-string. NEVER raises.

        Concurrent append from multiple producers is safe — the
        composite-key lookup serializes through the RLock and the
        replace-in-place semantics preserve insertion order.
        """
        op_safe = _safe_str(op_id)
        phase_safe = _safe_str(phase)
        kind_enum = NarrativeKind.coerce(kind)
        token_safe = _safe_str(token)
        if not token_safe:
            return False
        composite = (op_safe, phase_safe, kind_enum.value)
        with self._lock:
            ref = self._active_keys.get(composite)
            if ref is None:
                return False
            current = self._items.get(ref)
            if current is None or current.state is not FrameState.BUFFERING:
                return False
            updated = replace(current, prose=current.prose + token_safe)
            self._items[ref] = updated
            return True

    def commit(
        self,
        *,
        op_id: object,
        phase: object,
        kind: object,
    ) -> Optional[NarrativeFrame]:
        """Mark the active frame for ``(op_id, phase, kind)`` as
        COMMITTED. Returns the committed frame, or ``None`` for
        unknown/already-terminal. NEVER raises."""
        op_safe = _safe_str(op_id)
        phase_safe = _safe_str(phase)
        kind_enum = NarrativeKind.coerce(kind)
        composite = (op_safe, phase_safe, kind_enum.value)
        with self._lock:
            ref = self._active_keys.pop(composite, None)
            if ref is None:
                return None
            current = self._items.get(ref)
            if current is None:
                return None
            if current.state is not FrameState.BUFFERING:
                return current
            updated = replace(
                current,
                state=FrameState.COMMITTED,
                terminal_at=time.monotonic(),
            )
            self._items[ref] = updated
            return updated

    def discard(
        self,
        *,
        op_id: object,
        phase: object,
        kind: object,
    ) -> Optional[NarrativeFrame]:
        """Mark the active frame DISCARDED (provider error / cancel /
        empty model output). Returns the discarded frame or ``None``."""
        op_safe = _safe_str(op_id)
        phase_safe = _safe_str(phase)
        kind_enum = NarrativeKind.coerce(kind)
        composite = (op_safe, phase_safe, kind_enum.value)
        with self._lock:
            ref = self._active_keys.pop(composite, None)
            if ref is None:
                return None
            current = self._items.get(ref)
            if current is None:
                return None
            if current.state is not FrameState.BUFFERING:
                return current
            updated = replace(
                current,
                state=FrameState.DISCARDED,
                terminal_at=time.monotonic(),
            )
            self._items[ref] = updated
            return updated

    def emit_complete(
        self,
        *,
        op_id: object,
        phase: object,
        kind: object,
        prose: object,
        provider: object = "",
    ) -> Optional[NarrativeFrame]:
        """One-shot helper: start_frame + single append + commit.

        Used by Slice 2's deterministic preamble synthesis where the
        complete prose is known up front (no streaming). Returns the
        committed frame or ``None`` on error.
        """
        prose_safe = _safe_str(prose)
        if not prose_safe:
            return None
        frame = self.start_frame(
            op_id=op_id, phase=phase, kind=kind, provider=provider,
        )
        if frame is None:
            return None
        self.append_token(op_id=op_id, phase=phase, kind=kind, token=prose_safe)
        return self.commit(op_id=op_id, phase=phase, kind=kind)

    def clear(self) -> None:
        """Drop all frames. Counter is NOT reset."""
        with self._lock:
            self._items.clear()
            self._active_keys.clear()

    # ---- query API ---------------------------------------------------

    def lookup(self, ref: object) -> Optional[NarrativeFrame]:
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def list_recent(self, limit: int = 10) -> Tuple[NarrativeFrame, ...]:
        """Newest → oldest, capped by ``limit``."""
        if not isinstance(limit, int) or limit <= 0:
            return ()
        with self._lock:
            entries = list(self._items.values())
        entries.reverse()
        return tuple(entries[:limit])

    def find_by_op_id(self, op_id: object) -> Tuple[NarrativeFrame, ...]:
        """All frames (oldest → newest) for ``op_id``."""
        if not isinstance(op_id, str) or not op_id:
            return ()
        with self._lock:
            return tuple(
                e for e in self._items.values() if e.op_id == op_id
            )

    def find_by_kind(self, kind: object) -> Tuple[NarrativeFrame, ...]:
        kind_enum = NarrativeKind.coerce(kind)
        with self._lock:
            return tuple(
                e for e in self._items.values() if e.kind is kind_enum
            )

    def all_refs(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._items.keys())

    def active_keys(self) -> Tuple[Tuple[str, str, str], ...]:
        """Currently-buffering composite keys."""
        with self._lock:
            return tuple(self._active_keys.keys())

    # ---- internals ----------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Drop oldest entries until back within capacity. Prune the
        active-key index when the evicted frame was still BUFFERING."""
        while len(self._items) > self._capacity:
            ref, evicted = self._items.popitem(last=False)
            if evicted.state is FrameState.BUFFERING:
                composite = (evicted.op_id, evicted.phase, evicted.kind.value)
                if self._active_keys.get(composite) == ref:
                    self._active_keys.pop(composite, None)


# ===========================================================================
# Module singleton
# ===========================================================================


_default_channel: Optional[NarrativeChannel] = None
_singleton_lock = threading.Lock()


def get_default_channel() -> NarrativeChannel:
    global _default_channel
    with _singleton_lock:
        if _default_channel is None:
            _default_channel = NarrativeChannel()
        return _default_channel


def reset_default_channel_for_tests() -> None:
    global _default_channel
    with _singleton_lock:
        _default_channel = None


__all__ = [
    "BUFFER_SIZE_ENV_VAR",
    "ChannelSnapshot",
    "FrameState",
    "NARRATIVE_CHANNEL_SCHEMA_VERSION",
    "NarrativeChannel",
    "NarrativeFrame",
    "NarrativeKind",
    "REF_PREFIX",
    "get_default_channel",
    "reset_default_channel_for_tests",
]
