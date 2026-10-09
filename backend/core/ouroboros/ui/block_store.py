"""The cockpit's component tree: live blocks re-render, final blocks freeze.

The client half of :mod:`ui.block_protocol`. It holds the transcript's LIVE
blocks (a tool call in flight) and hands each block, the moment it is final,
to the static history exactly once.

Resilience is structural rather than defensive:

* Frames carry full state, so applying them is idempotent. A frame whose
  ``rev`` is not newer than what is held is ignored, which makes duplicates,
  retransmits and out-of-order delivery harmless.
* ``final`` is terminal. A block frozen once can never be revived by a late
  live frame: frozen ids are remembered (bounded) for exactly that purpose.
* Live blocks are bounded (``JARVIS_COCKPIT_LIVE_BLOCKS_MAX``) and expire
  (``JARVIS_COCKPIT_LIVE_BLOCK_TTL_S``). A daemon that dies mid-tool cannot
  leave a spinner on screen forever: :meth:`BlockStore.abandon` freezes what
  was live, marked interrupted.
* Callbacks run OUTSIDE the lock, so a renderer that re-enters the store, or
  is slow, cannot deadlock or stall frame intake.

Stdlib only, renderer-agnostic. NEVER raises.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Callable, List, Optional, Tuple

from backend.core.ouroboros.ui.block_protocol import Block, FINAL

FreezeSink = Callable[[Block], None]
LiveSink = Callable[[Tuple[Block, ...]], None]


def _env_number(name: str, default: float, lo: float) -> float:
    try:
        return max(lo, float(os.environ.get(name, "") or default))
    except ValueError:
        return default


def live_blocks_max() -> int:
    return int(_env_number("JARVIS_COCKPIT_LIVE_BLOCKS_MAX", 64, 1))


def live_block_ttl_s() -> float:
    return _env_number("JARVIS_COCKPIT_LIVE_BLOCK_TTL_S", 900.0, 1.0)


def _frozen_memory() -> int:
    return int(_env_number("JARVIS_COCKPIT_FROZEN_IDS_MAX", 4096, 64))


class BlockStore:
    """Live blocks by id, ordered by the daemon's ``seq``."""

    def __init__(self, *, on_freeze: Optional[FreezeSink] = None,
                 on_live: Optional[LiveSink] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._live: "OrderedDict[str, Tuple[Block, float]]" = OrderedDict()
        self._frozen: "OrderedDict[str, None]" = OrderedDict()
        self._on_freeze = on_freeze
        self._on_live = on_live
        self._clock = clock

    # -- intake ---------------------------------------------------------

    def apply(self, block: Optional[Block]) -> bool:
        """Apply one full-state frame. Returns whether it changed anything."""
        if block is None:
            return False
        frozen: List[Block] = []
        changed = False
        try:
            with self._lock:
                if block.id in self._frozen:
                    return False
                held = self._live.get(block.id)
                if held is not None and block.rev <= held[0].rev:
                    return False
                if block.final:
                    self._live.pop(block.id, None)
                    self._remember_frozen(block.id)
                    frozen.append(block)
                else:
                    self._live[block.id] = (block, self._clock())
                    frozen.extend(self._evict_locked())
                changed = True
            self._emit(frozen, live_changed=True)
            return changed
        except Exception:  # noqa: BLE001
            return False

    def expire(self) -> int:
        """Freeze live blocks no frame has refreshed within the TTL, marked
        interrupted. Called from the renderer's tick. Returns how many."""
        try:
            horizon = self._clock() - live_block_ttl_s()
            with self._lock:
                stale = [bid for bid, (_, at) in self._live.items() if at < horizon]
                frozen = [self._interrupt_locked(bid) for bid in stale]
            self._emit([b for b in frozen if b], live_changed=bool(frozen))
            return len(frozen)
        except Exception:  # noqa: BLE001
            return 0

    def abandon(self) -> int:
        """The stream ended (daemon gone, detach). Whatever was live is not
        live any more: freeze it, marked interrupted, rather than leave it
        drawn as if still running."""
        try:
            with self._lock:
                frozen = [self._interrupt_locked(bid) for bid in list(self._live)]
            self._emit([b for b in frozen if b], live_changed=bool(frozen))
            return len(frozen)
        except Exception:  # noqa: BLE001
            return 0

    # -- reading --------------------------------------------------------

    def live(self) -> Tuple[Block, ...]:
        """Live blocks in creation order (``seq``), not arrival order."""
        try:
            with self._lock:
                blocks = [b for b, _ in self._live.values()]
            return tuple(sorted(blocks, key=lambda b: (b.seq, b.id)))
        except Exception:  # noqa: BLE001
            return ()

    # -- internals (lock held) -------------------------------------------

    def _remember_frozen(self, bid: str) -> None:
        self._frozen[bid] = None
        self._frozen.move_to_end(bid)
        while len(self._frozen) > _frozen_memory():
            self._frozen.popitem(last=False)

    def _interrupt_locked(self, bid: str) -> Optional[Block]:
        held = self._live.pop(bid, None)
        if held is None:
            return None
        self._remember_frozen(bid)
        block = held[0]
        return replace(block, rev=block.rev + 1, state=FINAL,
                       meta={**block.meta, "interrupted": True})

    def _evict_locked(self) -> List[Block]:
        out: List[Block] = []
        while len(self._live) > live_blocks_max():
            oldest = min(self._live.items(), key=lambda kv: kv[1][0].seq)[0]
            frozen = self._interrupt_locked(oldest)
            if frozen is not None:
                out.append(frozen)
        return out

    def _emit(self, frozen: List[Block], *, live_changed: bool) -> None:
        for block in frozen:
            if self._on_freeze is not None:
                try:
                    self._on_freeze(block)
                except Exception:  # noqa: BLE001 — a sink fault never wedges intake
                    pass
        if live_changed and self._on_live is not None:
            try:
                self._on_live(self.live())
            except Exception:  # noqa: BLE001
                pass


def block_lines(block: Block) -> Tuple[str, ...]:
    """What a block draws: its lines, plus an interrupted note when it was
    frozen without the daemon finishing it."""
    try:
        if block.meta.get("interrupted"):
            from backend.core.ouroboros.ui import deck_grammar
            return tuple(block.lines) + (deck_grammar.detail("interrupted", tone="warn"),)
        return tuple(block.lines)
    except Exception:  # noqa: BLE001
        return tuple(block.lines)


__all__ = ["BlockStore", "block_lines", "live_block_ttl_s", "live_blocks_max"]
