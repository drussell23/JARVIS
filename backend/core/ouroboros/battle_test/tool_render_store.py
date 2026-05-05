"""BoundedBodyStore — session-scoped ring buffer for tool result bodies.
=========================================================================

Slice 3 of the **Gap #2 closure arc**.

Root problem
------------

Slices 1 + 2 produce a *bounded* render — a header + summary + a small
head/tail-elided body chunk. That's right for log-density purposes,
but it strands the operator: the suppressed body is gone for good. CC
keeps every tool result in scrollable history; we want the equivalent
without re-introducing unbounded log bloat.

The fix is a session-scoped store: when the renderer truncates a body,
the *full* body is parked here under a stable expansion reference
(``t-N``). A future ``/expand <ref>`` REPL verb (deferred to Gap #4
since it shares substrate with diff persistence) can re-render the
full body on demand.

Slice 3 scope
-------------

* :class:`StoredBody` — frozen record of one parked body
* :class:`BoundedBodyStore` — thread-safe FIFO ring; drop-oldest on
  overflow; stable monotonic ``t-N`` refs
* Default singleton accessor (``get_default_store()``) with lazy
  env-driven sizing — :data:`STORE_SIZE_ENV_VAR` (default 50)
* No ``/expand`` verb here. That is a Gap #4 concern; the substrate
  exposes ``lookup(ref)`` so the verb can be a one-line wiring change
  when its slice lands.

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no I/O, no Console
* §7 fail-closed — every public method has a documented fallback;
  invalid refs return ``None``, never raise
* §8 observable — :class:`StoreSnapshot` projection lets the
  observability layer report capacity / utilization

What this module does NOT do
----------------------------

* Persistence — the store is in-memory; bodies vanish on process
  exit. Adding disk-backing is a follow-up arc, not Gap #2 scope.
* Time-based eviction (TTL) — capacity-only eviction via FIFO.
  Session lifetime is the natural TTL.
* Any rendering — the substrate stores raw bytes-equivalent strings
  + metadata; Slice 4's wiring layer is responsible for re-rendering
  via :func:`tool_render_registry.render` on lookup.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ToolRenderStore")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


TOOL_RENDER_STORE_SCHEMA_VERSION: str = "tool_render_store.v1"


STORE_SIZE_ENV_VAR: str = "JARVIS_TOOL_RENDER_STORE_SIZE"


_DEFAULT_STORE_SIZE: int = 50

_MIN_STORE_SIZE: int = 1
_MAX_STORE_SIZE: int = 10_000  # defensive upper bound — keeps RAM bounded


# Reference prefix — exposed publicly so REPL parsers / tests can
# build refs without string-munging this module's literals.
REF_PREFIX: str = "t-"


# ===========================================================================
# Frozen records
# ===========================================================================


@dataclass(frozen=True)
class StoredBody:
    """One parked tool result.

    Fields
    ------
    * ``ref`` — opaque expansion handle (``"t-12"``); the only
      stable identifier callers should use.
    * ``op_id`` / ``round_index`` / ``tool_name`` — original key
      components, kept for observability + later filtered queries.
    * ``body`` — full uncapped tool output. The Slice 4 wiring
      layer feeds this back through :func:`tool_render_registry.render`
      with a generous body budget when ``/expand <ref>`` lands.
    * ``summary`` — 1-line summary computed at insert time (mirrors
      the rendered summary so /expand can show it without recomputing).
    * ``lexer`` — Rich Syntax lexer hint (``"diff"``, ``"python"``,
      ``"bash"``, ``"text"``, or ``None``).
    * ``inserted_at`` — ``time.monotonic()`` timestamp; for telemetry.
    """

    ref: str
    op_id: str
    round_index: int
    tool_name: str
    body: str
    summary: str
    lexer: Optional[str]
    inserted_at: float
    schema_version: str = TOOL_RENDER_STORE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "op_id": self.op_id,
            "round_index": self.round_index,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "lexer": self.lexer,
            "inserted_at": self.inserted_at,
            "body_chars": len(self.body),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class StoreSnapshot:
    """Read-only projection of the store's state for observability."""

    capacity: int
    size: int
    next_seq: int
    schema_version: str = TOOL_RENDER_STORE_SCHEMA_VERSION

    @property
    def utilization(self) -> float:
        """Fraction in [0.0, 1.0] of capacity currently used."""
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.size / self.capacity)


# ===========================================================================
# Helpers
# ===========================================================================


def _read_capacity_from_env() -> int:
    """Parse :data:`STORE_SIZE_ENV_VAR` with bounds + fallback."""
    raw = os.environ.get(STORE_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_STORE_SIZE
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.debug(
            "[ToolRenderStore] %s=%r is not an int; using default %d",
            STORE_SIZE_ENV_VAR, raw, _DEFAULT_STORE_SIZE,
        )
        return _DEFAULT_STORE_SIZE
    if parsed < _MIN_STORE_SIZE:
        logger.debug(
            "[ToolRenderStore] %s=%d below MIN; clamping to %d",
            STORE_SIZE_ENV_VAR, parsed, _MIN_STORE_SIZE,
        )
        return _MIN_STORE_SIZE
    if parsed > _MAX_STORE_SIZE:
        logger.debug(
            "[ToolRenderStore] %s=%d above MAX; clamping to %d",
            STORE_SIZE_ENV_VAR, parsed, _MAX_STORE_SIZE,
        )
        return _MAX_STORE_SIZE
    return parsed


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _safe_int(raw: object, default: int = 0) -> int:
    if isinstance(raw, bool):  # bool is a subclass of int — exclude
        return default
    if isinstance(raw, int):
        return raw
    return default


# ===========================================================================
# BoundedBodyStore — the ring
# ===========================================================================


class BoundedBodyStore:
    """Thread-safe bounded FIFO of tool result bodies.

    Eviction policy
    ---------------
    Drop-oldest on overflow. The newest body always wins capacity
    against the oldest; refs to evicted bodies become permanently
    invalid and resolve to ``None`` via :meth:`lookup`.

    Reference allocation
    --------------------
    Refs are issued from a monotonic counter (``t-1``, ``t-2``, …).
    The counter never resets within a store instance — even when
    eviction shrinks ``size`` back to zero. This guarantees that
    a ref printed in the operator's terminal at time ``t1`` always
    refers to either:
      * the same body (if it's still in the ring), OR
      * nothing (``lookup`` returns ``None``)
    — but NEVER a different body than the one that was originally
    referenced. That property is load-bearing for the eventual
    ``/expand <ref>`` verb's safety contract.

    Thread safety
    -------------
    A single :class:`threading.RLock` serializes all mutating + reading
    operations. Reentrant so an observer reading via :meth:`snapshot`
    inside a listener doesn't self-deadlock.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        if capacity is None:
            cap = _read_capacity_from_env()
        else:
            cap = max(_MIN_STORE_SIZE, min(_MAX_STORE_SIZE, _safe_int(capacity, _DEFAULT_STORE_SIZE)))
        self._capacity: int = cap
        self._items: "OrderedDict[str, StoredBody]" = OrderedDict()
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection -------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> StoreSnapshot:
        """Cheap read-only projection. NEVER raises."""
        with self._lock:
            return StoreSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
            )

    # ---- mutating API --------------------------------------------------

    def store(
        self,
        *,
        op_id: object,
        round_index: object,
        tool_name: object,
        body: object,
        summary: object = "",
        lexer: object = None,
    ) -> StoredBody:
        """Park a body and return the :class:`StoredBody` (with stable
        ``ref``). NEVER raises — non-string / non-int inputs are
        coerced via the safe helpers.

        The body is always stored, regardless of length. Capacity
        eviction kicks in *after* insertion so the most recent body
        always wins.
        """
        op_id_safe = _safe_str(op_id)
        round_safe = _safe_int(round_index, 0)
        tool_safe = _safe_str(tool_name)
        body_safe = _safe_str(body)
        summary_safe = _safe_str(summary)
        lexer_safe = _safe_str(lexer) or None

        with self._lock:
            ref = f"{REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            stored = StoredBody(
                ref=ref,
                op_id=op_id_safe,
                round_index=round_safe,
                tool_name=tool_safe,
                body=body_safe,
                summary=summary_safe,
                lexer=lexer_safe,
                inserted_at=time.monotonic(),
            )
            self._items[ref] = stored
            # Evict oldest entries until back within capacity.
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return stored

    def clear(self) -> None:
        """Drop all parked bodies (e.g. on session end). The ref
        counter is NOT reset — see class docstring on monotonic
        ref allocation."""
        with self._lock:
            self._items.clear()

    # ---- lookup --------------------------------------------------------

    def lookup(self, ref: object) -> Optional[StoredBody]:
        """Resolve a ref to its :class:`StoredBody`, or ``None`` if
        absent / evicted / malformed. NEVER raises."""
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def all_refs(self) -> Tuple[str, ...]:
        """All currently-resident refs, oldest → newest. Useful for
        debug REPL listings."""
        with self._lock:
            return tuple(self._items.keys())


# ===========================================================================
# Module singleton — reset_for_tests for clean isolation
# ===========================================================================


_default_store: Optional[BoundedBodyStore] = None
_singleton_lock = threading.Lock()


def get_default_store() -> BoundedBodyStore:
    """Return the process-wide default store (constructed lazily).

    Capacity is read from :data:`STORE_SIZE_ENV_VAR` at first
    construction. Use :func:`reset_default_store_for_tests` to drop
    the singleton between tests."""
    global _default_store
    with _singleton_lock:
        if _default_store is None:
            _default_store = BoundedBodyStore()
        return _default_store


def reset_default_store_for_tests() -> None:
    """Test isolation hook — drops the singleton; next call to
    :func:`get_default_store` re-reads the env."""
    global _default_store
    with _singleton_lock:
        _default_store = None


__all__ = [
    "REF_PREFIX",
    "STORE_SIZE_ENV_VAR",
    "TOOL_RENDER_STORE_SCHEMA_VERSION",
    "BoundedBodyStore",
    "StoreSnapshot",
    "StoredBody",
    "get_default_store",
    "reset_default_store_for_tests",
]
