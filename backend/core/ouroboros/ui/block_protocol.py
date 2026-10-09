"""Typed transcript blocks: the cockpit as a component tree, not a log.

The cockpit used to receive its transcript as markup LINES, each final the
moment it arrived. A terminal cannot unprint, so anything still happening (a
tool call in flight, an op mid-flight) had nowhere to live except a separate
status strip, and every tool appeared twice: once when it started and once
when it finished. Claude Code's terminal (built on Ink) treats the transcript
as COMPONENTS: a live component re-renders in place, and a finished one
freezes into static history.

This module is that model's wire contract, shared by the daemon that emits
blocks and the cockpit that renders them:

* A :class:`Block` has an immutable ``id`` (a UUID), a ``seq`` (its creation
  order, assigned by the daemon), a ``rev`` (its revision, monotonic), a
  ``kind``, a ``state`` (``live`` or ``final``) and its composed ``lines``.
* Every frame carries the block's FULL state, never a delta. A frame can
  therefore never be applied out of order into corruption: a stale ``rev``
  is ignored, and ``final`` is terminal.
* Frames are bounded (:func:`frame_limit_bytes`), and :func:`encode_frame`
  shrinks a block to fit rather than producing a frame the reader cannot
  take.

A plain ``markup`` frame remains valid: it is an anonymous final block of one
line. Stdlib only. NEVER raises.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple

#: The protocol version a cockpit declares in its ``caps`` frame. A daemon
#: sends block frames only to a cockpit that declared at least this.
BLOCK_PROTOCOL_VERSION = 1

LIVE = "live"
FINAL = "final"
_STATES = (LIVE, FINAL)

#: Block kinds. A closed set the renderer can rely on; unknown kinds are
#: rendered as their lines and never refused.
TOOL = "tool"
DIFF = "diff"
CLOSURE = "closure"
LINE = "line"
KINDS = frozenset({TOOL, DIFF, CLOSURE, LINE})


def frame_limit_bytes() -> int:
    """``JARVIS_ATTACH_FRAME_MAX_BYTES``: the largest frame either end of
    the attach socket accepts (default 4 MiB). Both ends read it, so the
    reader's limit and the writer's budget are one number."""
    try:
        value = int(os.environ.get("JARVIS_ATTACH_FRAME_MAX_BYTES", "") or 4 * 1024 * 1024)
    except ValueError:
        value = 4 * 1024 * 1024
    return max(64 * 1024, value)


@dataclass(frozen=True)
class Block:
    id: str
    seq: int
    rev: int
    kind: str
    state: str
    op_id: str = ""
    lines: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.state == FINAL

    def to_wire(self) -> Dict[str, Any]:
        return {"id": self.id, "seq": self.seq, "rev": self.rev,
                "kind": self.kind, "state": self.state, "op": self.op_id,
                "lines": list(self.lines), "meta": dict(self.meta)}

    @classmethod
    def from_wire(cls, raw: Any) -> Optional["Block"]:
        """A block from a frame's ``block`` object, or ``None`` when it is
        not one. Validates shape, never content: lines are markup the
        daemon composed and the renderer already treats defensively."""
        try:
            if not isinstance(raw, dict):
                return None
            bid = str(raw.get("id") or "")
            state = str(raw.get("state") or "")
            if not bid or state not in _STATES:
                return None
            lines = raw.get("lines") or ()
            if not isinstance(lines, (list, tuple)):
                return None
            meta = raw.get("meta") or {}
            return cls(
                id=bid, seq=int(raw.get("seq") or 0), rev=int(raw.get("rev") or 0),
                kind=str(raw.get("kind") or LINE), state=state,
                op_id=str(raw.get("op") or ""),
                lines=tuple(str(ln) for ln in lines),
                meta=dict(meta) if isinstance(meta, dict) else {},
            )
        except Exception:  # noqa: BLE001
            return None


def _frame_bytes(block: Block) -> bytes:
    frame = {"type": "block", "block": block.to_wire()}
    return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def fit(block: Block, *, limit: Optional[int] = None) -> Optional[Block]:
    """*block* shrunk to fit the frame budget, whatever form it travels in.

    A block too large keeps its head and its last line (the lines that
    identify it) and states how many it left out, halving the middle until
    it fits. The full content is not lost: tool bodies are parked
    daemon-side behind ``/expand``. ``None`` only when even the smallest
    form cannot fit. The ONE bound for both delivery forms: a block frame,
    and the line frames a cockpit without the protocol receives. NEVER
    raises.
    """
    try:
        budget = int(limit or frame_limit_bytes())
        lines = list(block.lines)
        keep = len(lines)
        while True:
            if keep >= len(lines):
                shown = lines
            else:
                head = lines[: max(1, keep - 1)]
                dropped = len(lines) - len(head) - 1
                shown = head + [f"… +{dropped} more lines elided …"] + lines[-1:]
            candidate = replace(block, lines=tuple(shown))
            if len(_frame_bytes(candidate)) <= budget:
                return candidate
            if keep <= 2:
                return None
            keep = keep // 2
    except Exception:  # noqa: BLE001
        return None


def encode_frame(block: Block, *, limit: Optional[int] = None) -> Optional[bytes]:
    """The newline-terminated frame for *block*, within the frame budget
    (:func:`fit`). ``None`` when it cannot fit. NEVER raises."""
    fitted = fit(block, limit=limit)
    return _frame_bytes(fitted) if fitted is not None else None


class BlockClock:
    """Daemon-side id/seq/rev issuance. ``seq`` orders blocks by creation
    across every op, so a cockpit can place a block correctly whatever order
    its frames arrive in. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0

    def open(self, kind: str, *, op_id: str = "", lines: Tuple[str, ...] = (),
             meta: Optional[Dict[str, Any]] = None, state: str = LIVE) -> Block:
        with self._lock:
            self._seq += 1
            seq = self._seq
        return Block(id=uuid.uuid4().hex, seq=seq, rev=1, kind=kind,
                     state=state if state in _STATES else LIVE, op_id=op_id,
                     lines=tuple(lines), meta=dict(meta or {}))

    @staticmethod
    def revise(block: Block, *, lines: Optional[Tuple[str, ...]] = None,
               meta: Optional[Dict[str, Any]] = None,
               state: Optional[str] = None) -> Block:
        """The next revision. ``final`` is terminal: a final block is
        returned unchanged."""
        if block.final:
            return block
        return replace(
            block, rev=block.rev + 1,
            lines=tuple(lines) if lines is not None else block.lines,
            meta={**block.meta, **(meta or {})},
            state=state if state in _STATES else block.state,
        )


__all__ = [
    "BLOCK_PROTOCOL_VERSION", "Block", "BlockClock", "CLOSURE", "DIFF", "FINAL",
    "KINDS", "LINE", "LIVE", "TOOL", "encode_frame", "fit", "frame_limit_bytes",
]
