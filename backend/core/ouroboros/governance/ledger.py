"""
Operation Ledger — Append-Only State Log
=========================================

Every state transition for an Ouroboros operation is persisted here **before**
any event is published (outbox pattern).  The ledger is the single source of
truth for operation lifecycle history.

Each operation's entries are stored in a dedicated JSONL file inside the
configured ``storage_dir``::

    <storage_dir>/<sanitised_op_id>.jsonl

Entries are appended atomically (one JSON line per entry) and deduplicated
by ``(op_id, state)`` so that replays and retries are safe.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import aiofiles  # type: ignore[import-untyped]

    _HAS_AIOFILES = True
except ImportError:
    _HAS_AIOFILES = False

import asyncio


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OperationState(Enum):
    """Lifecycle states for an Ouroboros operation.

    The state machine flows roughly as::

        PLANNED -> SANDBOXING -> VALIDATING -> GATING -> APPLYING -> APPLIED
                                                     |-> ROLLED_BACK
                                              |-> BLOCKED
                          |-> FAILED (from any intermediate state)
    """

    PLANNED = "planned"
    SANDBOXING = "sandboxing"
    VALIDATING = "validating"
    GATING = "gating"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    BLOCKED = "blocked"

    # Iteration Mode lifecycle checkpoints
    BUDGET_CHECKPOINT = "budget_checkpoint"
    PRE_APPLY_CHECKSUM = "pre_apply_checksum"
    ITERATION_OUTCOME = "iteration_outcome"

    # Tier 0 (Doubleword async batch) lifecycle
    PENDING_TIER0 = "pending_tier0"
    TIER0_COMPLETE = "tier0_complete"

    # RSI Convergence Framework states (v0.2.0)
    SCORE_COMPUTED = "score_computed"
    CONVERGENCE_CHECKED = "convergence_checked"
    PRE_SCORED = "pre_scored"
    VINDICATION_CHECKED = "vindication_checked"

    # Phase 1 Subagents — per-dispatch record from SubagentOrchestrator.
    # Multiple entries may share one op_id (one per subagent in a parallel
    # fan-out), so LedgerSubagentSink supplies the subagent_id as entry_id
    # to honor the (op_id, state, entry_id) dedup key.
    SUBAGENT_DISPATCH = "subagent_dispatch"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    """A single state-transition record in the operation ledger.

    Parameters
    ----------
    op_id:
        The ``op-<uuidv7>-<origin>`` identifier of the operation.
    state:
        The :class:`OperationState` being recorded.
    data:
        Arbitrary metadata attached to this transition (e.g. error details,
        validation results, gate verdicts).
    timestamp:
        Monotonic clock value (``time.monotonic()``).  Used for ordering
        within a single process lifetime.
    wall_time:
        Wall-clock Unix timestamp (``time.time()``).  Used for cross-process
        and human-readable correlation.
    entry_id:
        Optional per-record disambiguator.  When set, deduplication uses
        ``op_id:state:entry_id`` instead of the default ``op_id:state`` key.
        This allows multiple records with the same ``(op_id, state)`` pair
        (e.g. multiple tool-execution records all using SANDBOXING) to be
        written without colliding.  Callers that do not supply ``entry_id``
        retain the original dedup behaviour.
    """

    op_id: str
    state: OperationState
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)
    wall_time: float = field(default_factory=time.time)
    entry_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Characters allowed in filenames: alphanumeric, hyphen, underscore, dot.
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_op_id(op_id: str) -> str:
    """Convert an op_id to a safe filename component."""
    return _SANITIZE_RE.sub("_", op_id)


def _entry_to_dict(entry: LedgerEntry) -> Dict[str, Any]:
    """Serialise a :class:`LedgerEntry` to a JSON-compatible dict."""
    d: Dict[str, Any] = {
        "op_id": entry.op_id,
        "state": entry.state.value,
        "data": entry.data,
        "timestamp": entry.timestamp,
        "wall_time": entry.wall_time,
    }
    if entry.entry_id is not None:
        d["entry_id"] = entry.entry_id
    return d


def _dict_to_entry(d: Dict[str, Any]) -> LedgerEntry:
    """Deserialise a dict (from JSON) back to a :class:`LedgerEntry`."""
    return LedgerEntry(
        op_id=d["op_id"],
        state=OperationState(d["state"]),
        data=d["data"],
        timestamp=d["timestamp"],
        wall_time=d["wall_time"],
        entry_id=d.get("entry_id"),
    )


# ---------------------------------------------------------------------------
# OperationLedger
# ---------------------------------------------------------------------------


class OperationLedger:
    """Append-only, file-backed operation state log with deduplication.

    Parameters
    ----------
    storage_dir:
        Directory where JSONL files are written.  Created if it does not
        exist.
    """

    def __init__(self, storage_dir: Path) -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._seen: Set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _op_file(self, op_id: str) -> Path:
        """Return the JSONL file path for a given operation ID."""
        return self._storage_dir / f"{_sanitize_op_id(op_id)}.jsonl"

    def _dedup_key(self, entry: LedgerEntry) -> str:
        """Return a deduplication key for an entry.

        When ``entry.entry_id`` is set the key includes it, allowing multiple
        records with the same ``(op_id, state)`` pair to coexist in the ledger
        (e.g. one tool-execution record per tool call, all sharing SANDBOXING).
        Without ``entry_id`` the original ``op_id:state`` behaviour is
        preserved for backward compatibility.
        """
        if entry.entry_id:
            return f"{entry.op_id}:{entry.state.value}:{entry.entry_id}"
        return f"{entry.op_id}:{entry.state.value}"

    # ------------------------------------------------------------------
    # Async file I/O (with fallback)
    # ------------------------------------------------------------------

    async def _append_line(self, path: Path, line: str) -> None:
        """Append a single line to *path* using async I/O when available."""
        if _HAS_AIOFILES:
            async with aiofiles.open(path, mode="a", encoding="utf-8") as f:
                await f.write(line + "\n")
        else:
            # Fallback: synchronous write wrapped in executor
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._sync_append_line, path, line)

    @staticmethod
    def _sync_append_line(path: Path, line: str) -> None:
        """Synchronous fallback for appending a line."""
        with open(path, mode="a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _read_lines(self, path: Path) -> List[str]:
        """Read all lines from *path* using async I/O when available."""
        if not path.exists():
            return []
        if _HAS_AIOFILES:
            async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
                content = await f.read()
            return content.splitlines()
        else:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._sync_read_lines, path)

    @staticmethod
    def _sync_read_lines(path: Path) -> List[str]:
        """Synchronous fallback for reading all lines."""
        with open(path, mode="r", encoding="utf-8") as f:
            return f.read().splitlines()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(self, entry: LedgerEntry) -> bool:
        """Append a ledger entry, returning ``True`` on success.

        Returns ``False`` (without writing) if a duplicate is detected.
        The dedup key is ``(op_id, state)`` when ``entry.entry_id`` is
        ``None``, or ``(op_id, state, entry_id)`` when ``entry_id`` is
        set — allowing multiple entries per ``(op_id, state)`` pair when
        each carries a distinct ``entry_id`` (e.g. tool-exec audit records).

        Parameters
        ----------
        entry:
            The :class:`LedgerEntry` to persist.

        Returns
        -------
        bool
            ``True`` if the entry was written, ``False`` if it was a
            duplicate.
        """
        key = self._dedup_key(entry)
        if key in self._seen:
            return False

        line = json.dumps(_entry_to_dict(entry), sort_keys=True, default=str)
        await self._append_line(self._op_file(entry.op_id), line)
        self._seen.add(key)
        return True

    async def get_history(self, op_id: str) -> List[LedgerEntry]:
        """Return all ledger entries for *op_id*, in append order.

        As a side effect, populates the internal ``_seen`` set so that
        subsequent :meth:`append` calls respect previously persisted
        entries (important when a fresh ``OperationLedger`` is created
        against an existing directory).

        Parameters
        ----------
        op_id:
            The operation identifier to look up.

        Returns
        -------
        List[LedgerEntry]
            Ordered list of all entries for this operation (may be empty).
        """
        path = self._op_file(op_id)
        lines = await self._read_lines(path)
        entries: List[LedgerEntry] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            entry = _dict_to_entry(json.loads(stripped))
            entries.append(entry)
            # Populate dedup set from disk
            self._seen.add(self._dedup_key(entry))
        return entries

    async def get_latest_state(self, op_id: str) -> Optional[OperationState]:
        """Return the most recent :class:`OperationState` for *op_id*.

        Parameters
        ----------
        op_id:
            The operation identifier to query.

        Returns
        -------
        Optional[OperationState]
            The latest state, or ``None`` if no entries exist.
        """
        history = await self.get_history(op_id)
        if not history:
            return None
        return history[-1].state

    def get_latest_state_sync(self, op_id: str) -> Optional[OperationState]:
        """Synchronous version of get_latest_state for use in lock-protected paths.

        Reads the JSONL file directly without async. Safe to call from sync code
        (e.g., approval store decide() in the same flock scope).
        """
        path = self._op_file(op_id)
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        last_state: Optional[OperationState] = None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = _dict_to_entry(json.loads(stripped))
                last_state = entry.state
                self._seen.add(self._dedup_key(entry))
            except Exception:
                continue
        return last_state
