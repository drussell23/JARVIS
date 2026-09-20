"""Execution provenance: registered -> invoked -> effective.

Why
---

This repo grades capabilities on whether they were built. The audit that
produced this module found the gap that grading cannot see:

* the VALIDATE_RETRY ladder never regenerated, for the system's whole
  history -- 524 byte-identical iterations;
* the micro-fix ran 532 times and repaired nothing, four independent causes
  deep, every one of them shipped green with tests and FSM telemetry;
* 43 ``_ENABLED`` capability switches default off and are set nowhere.

Every one of those was *registered*. Most were *invoked*. None were ever
*effective*, and nothing in the system could tell the difference. The single
most valuable piece of telemetry in the investigation was the arithmetic
``micro_fix_pre 532 = micro_fix_returned 375 + micro_fix_skipped_new_file
157``, and it took a grep across every session log to find.

The three tiers
---------------

``REGISTERED``
    The capability loaded -- imported, mounted, constructed. What boot logs
    already say, and the weakest possible evidence: a dead subsystem
    registers perfectly.

``INVOKED``
    Control entered it. Distinguishes "wired" from "wired and reached",
    which is where ``micro_fix_skipped_new_file`` lived.

``EFFECTIVE``
    It changed an outcome. This is the only tier that answers the question
    the PRD thinks it is answering, and the only one that cannot be
    satisfied by code that merely exists.

The alarm that matters is ``invoked > 0 and effective == 0`` over a window.
That single rule would have caught all four defects above on their first
session.

Effectiveness is never self-asserted. A caller states its criterion --
for a repair, that the AST it produced is structurally different from the
one it was given -- and the criterion is evaluated here. ``ast.dump``
comparison is delegated to ``shadow_harness.OutputComparator``, which
already owns structural equivalence for this tree; a whitespace or comment
change scores 1.0 there and is correctly *not* effective.

Concurrency
-----------

VALIDATE fans candidates out under ``asyncio.gather`` and the sentinel runs
goals concurrently, so every counter here is read-modify-write under
contention -- the same shape as the admission race found in the micro-fix
governor. All mutation happens under a single ``asyncio.Lock``; the
in-process counters cannot interleave.

The durable line is appended with ``O_APPEND`` in one ``write()`` per
record, which Linux does not interleave for writes of this size. That is a
narrower guarantee than a cross-process lock and it is stated rather than
implied: ``adaptation/_file_lock`` exists for that, but its module contract
says only ``adaptation/ledger.py`` may import it, and quietly widening a
declared one-way dependency to save nine lines is how boundaries rot.
Nothing outside this process writes this file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.Reachability")

_DEFAULT_MAX_RECORDS = 50_000
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_KEEP_ROTATIONS = 3


def _env_int(name: str, default: int) -> int:
    """Call-time, never raises."""
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = int(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def rotate_at_bytes() -> int:
    """Size at which the active ledger is rotated aside.

    An append-only file on a multi-day soak grows without bound, and the
    panel that reads it re-reads a tail every poll -- so an unrotated ledger
    degrades the very surface it feeds, then the filesystem.
    """
    return _env_int("JARVIS_REACHABILITY_ROTATE_BYTES", _DEFAULT_MAX_BYTES)


def keep_rotations() -> int:
    """How many rotated generations survive. Older ones are removed, because
    unbounded history is the same defect one directory over."""
    return _env_int("JARVIS_REACHABILITY_KEEP_ROTATIONS", _DEFAULT_KEEP_ROTATIONS)


class Tier(str, Enum):
    """Strength of evidence that a capability is alive."""

    REGISTERED = "registered"
    INVOKED = "invoked"
    EFFECTIVE = "effective"


@dataclass
class CapabilityState:
    """What a capability has proven about itself."""

    capability: str
    registered: int = 0
    invoked: int = 0
    effective: int = 0
    first_seen: float = 0.0
    last_invoked_at: float = 0.0
    last_effective_at: float = 0.0
    last_detail: str = ""

    @property
    def inert(self) -> bool:
        """Reached, and never changed anything."""
        return self.invoked > 0 and self.effective == 0

    @property
    def dormant(self) -> bool:
        """Loaded, and never reached."""
        return self.registered > 0 and self.invoked == 0

    def render(self) -> str:
        return (
            f"{self.capability}: registered={self.registered} "
            f"invoked={self.invoked} effective={self.effective}"
            + (" INERT" if self.inert else "")
            + (" DORMANT" if self.dormant else "")
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "registered": self.registered,
            "invoked": self.invoked,
            "effective": self.effective,
            "inert": self.inert,
            "dormant": self.dormant,
            "last_effective_at": self.last_effective_at,
            "last_detail": self.last_detail,
        }


class Effect:
    """A caller's claim that its capability changed something.

    Deliberately not a boolean the caller sets at will: ``ast_mutation``
    states a criterion and this object evaluates it, so "effective" cannot
    drift into meaning "ran without raising" -- which is exactly the
    conflation that let a repair loop report success 532 times while
    repairing nothing.
    """

    __slots__ = ("_effective", "_detail")

    def __init__(self) -> None:
        self._effective = False
        self._detail = ""

    @property
    def effective(self) -> bool:
        return self._effective

    @property
    def detail(self) -> str:
        return self._detail

    def mark(self, detail: str = "") -> None:
        """Assert effectiveness directly, for outcomes with no AST."""
        self._effective = True
        self._detail = detail

    def ast_mutation(self, before: str, after: str, *, detail: str = "") -> bool:
        """Effective iff *after* is a structurally different program.

        Reformatting is not repair. ``OutputComparator`` in AST mode scores
        identical ``ast.dump`` output 1.0, so whitespace-, comment- and
        quote-style-only edits are correctly refused. A file that stops
        parsing scores 0.0 there, which is a structural change -- and a
        regression the micro-fix governor severs on separately.
        """
        if not after or before == after:
            self._detail = detail or "identical text"
            return False
        try:
            from backend.core.ouroboros.governance.shadow_harness import (  # noqa: PLC0415
                CompareMode, OutputComparator,
            )
            score = OutputComparator().compare(before, after, CompareMode.AST)
        except Exception:  # noqa: BLE001
            logger.debug("[Reachability] AST comparison unavailable", exc_info=True)
            self._detail = detail or "ast comparison unavailable"
            return False
        changed = score < 1.0
        self._effective = changed
        self._detail = detail or f"ast_similarity={score:.3f}"
        return changed


class ReachabilityLedger:
    """Counters plus an append-only record of what actually executed."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        max_records: Optional[int] = None,
    ) -> None:
        self._path = path
        self._max_records = max_records or _DEFAULT_MAX_RECORDS
        self._states: Dict[str, CapabilityState] = {}
        self._written = 0
        self._rotations = 0
        self._lock = asyncio.Lock()

    # -- recording -----------------------------------------------------

    async def record(
        self,
        capability: str,
        tier: Tier,
        *,
        op_id: str = "",
        detail: str = "",
    ) -> CapabilityState:
        """Append one observation. NEVER raises."""
        now = time.time()
        async with self._lock:
            state = self._states.get(capability)
            if state is None:
                state = CapabilityState(capability=capability, first_seen=now)
                self._states[capability] = state
            if tier is Tier.REGISTERED:
                state.registered += 1
            elif tier is Tier.INVOKED:
                state.invoked += 1
                state.last_invoked_at = now
            else:
                state.effective += 1
                state.last_effective_at = now
            if detail:
                state.last_detail = detail
            self._append(capability, tier, op_id, detail, now)
            return state

    async def registered(self, capability: str, *, detail: str = "") -> None:
        await self.record(capability, Tier.REGISTERED, detail=detail)

    async def invoked(
        self, capability: str, *, op_id: str = "", detail: str = "",
    ) -> None:
        await self.record(capability, Tier.INVOKED, op_id=op_id, detail=detail)

    async def effective(
        self, capability: str, *, op_id: str = "", detail: str = "",
    ) -> None:
        await self.record(capability, Tier.EFFECTIVE, op_id=op_id, detail=detail)

    def _append(
        self, capability: str, tier: Tier, op_id: str, detail: str, at: float,
    ) -> None:
        """One O_APPEND write per record. Called under the lock; never raises."""
        if self._path is None or self._written >= self._max_records:
            return
        line = json.dumps({
            "at": round(at, 3), "capability": capability,
            "tier": tier.value, "op_id": op_id, "detail": detail,
        }, sort_keys=True) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._maybe_rotate()
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            self._written += 1
        except OSError:
            logger.debug("[Reachability] append degraded", exc_info=True)

    def _maybe_rotate(self) -> None:
        """Rotate aside when the active file breaches its ceiling.

        Called under the ledger's own lock and BEFORE the append, so no
        concurrent writer can be mid-write during the rename: every caller
        reaches this code through :meth:`record`, which holds the lock for
        the whole read-modify-append. That is what makes the rotation
        lossless rather than merely atomic -- ``os.rename`` is atomic on its
        own, but atomicity alone would still let a writer that had already
        opened the old inode append into a file nobody reads again.

        Generations shift up (``.1`` -> ``.2``) and the oldest is removed;
        unbounded history is the same defect one directory over. NEVER
        raises -- a rotation fault must not cost the write that triggered
        it.
        """
        if self._path is None:
            return
        try:
            ceiling = rotate_at_bytes()
            if not self._path.is_file() or self._path.stat().st_size < ceiling:
                return
            keep = keep_rotations()
            oldest = self._path.with_suffix(self._path.suffix + f".{keep}")
            if oldest.exists():
                oldest.unlink()
            for gen in range(keep - 1, 0, -1):
                src = self._path.with_suffix(self._path.suffix + f".{gen}")
                if src.exists():
                    src.rename(
                        self._path.with_suffix(self._path.suffix + f".{gen + 1}")
                    )
            self._path.rename(self._path.with_suffix(self._path.suffix + ".1"))
            self._rotations += 1
            logger.info(
                "[Reachability] ledger rotated at %d bytes (generation %d, "
                "keeping %d)", ceiling, self._rotations, keep,
            )
        except OSError:
            logger.debug("[Reachability] rotation degraded", exc_info=True)

    # -- reading -------------------------------------------------------

    async def state(self, capability: str) -> CapabilityState:
        async with self._lock:
            return self._states.get(capability) or CapabilityState(
                capability=capability,
            )

    async def snapshot(self) -> Dict[str, CapabilityState]:
        async with self._lock:
            return {k: v for k, v in self._states.items()}

    async def inert(self, *, min_invocations: int = 1) -> List[CapabilityState]:
        """Capabilities reached at least *min_invocations* times that have
        never changed an outcome. This is the alarm, not a log line."""
        async with self._lock:
            return sorted(
                (
                    s for s in self._states.values()
                    if s.invoked >= min_invocations and s.effective == 0
                ),
                key=lambda s: -s.invoked,
            )

    async def dormant(self) -> List[CapabilityState]:
        """Registered and never reached."""
        async with self._lock:
            return sorted(
                (s for s in self._states.values() if s.dormant),
                key=lambda s: s.capability,
            )


_default: Optional[ReachabilityLedger] = None


def default_ledger() -> ReachabilityLedger:
    """Process-wide ledger. Path resolves at first use, never at import."""
    global _default  # noqa: PLW0603
    if _default is None:
        raw = (os.environ.get("JARVIS_REACHABILITY_LEDGER_PATH", "") or "").strip()
        _default = ReachabilityLedger(path=Path(raw) if raw else None)
    return _default


@asynccontextmanager
async def track_reachability(
    capability: str,
    *,
    op_id: str = "",
    ledger: Optional[ReachabilityLedger] = None,
) -> AsyncIterator[Effect]:
    """Record an invocation, and whatever the body proves about its effect.

    The invocation is recorded on entry rather than exit: a capability that
    raised still ran, and a ledger that only counts clean exits under-reports
    exactly the paths most worth seeing. Effectiveness is recorded on exit,
    and only if the body established it.
    """
    book = ledger or default_ledger()
    await book.invoked(capability, op_id=op_id)
    effect = Effect()
    try:
        yield effect
    finally:
        if effect.effective:
            await book.effective(capability, op_id=op_id, detail=effect.detail)


def tracks_reachability(
    capability: str,
    *,
    ledger: Optional[ReachabilityLedger] = None,
) -> Callable:
    """Decorator for async callables whose effect is their truthy return.

    Thin on purpose -- it delegates to :func:`track_reachability` so the
    two entry points cannot drift. Use the context manager wherever
    effectiveness needs a real criterion; a truthy return is the weakest
    one that is still better than nothing.
    """
    def _decorate(fn: Callable) -> Callable:
        @wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            op_id = str(kwargs.get("op_id", "") or "")
            async with track_reachability(
                capability, op_id=op_id, ledger=ledger,
            ) as effect:
                result = await fn(*args, **kwargs)
                if result:
                    effect.mark(f"truthy:{type(result).__name__}")
                return result
        return _wrapped
    return _decorate


__all__ = [
    "CapabilityState",
    "Effect",
    "ReachabilityLedger",
    "Tier",
    "default_ledger",
    "track_reachability",
    "tracks_reachability",
]
