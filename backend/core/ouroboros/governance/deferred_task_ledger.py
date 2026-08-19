"""What the organism DID NOT look at, and why — so a blind spot has a name.

THE DEFECT THIS CLOSES
----------------------
When OpportunityMiner's AST analysis timed out, the sensor did::

    if _s11b_result.outcome != _S11B_AO.OK:
        errors += 1
        continue

That is better than it first appears — a timeout is counted as an ERROR, not
silently folded into "no opportunities found". But the file is then DROPPED
and never revisited, and nothing anywhere records WHICH files went unanalysed.
The scan reports "3 errors" and moves on; the next scan makes the same choice
about the same files for the same reason. The organism has a blind spot and no
representation of it.

An error counter says *how many* it missed. It cannot say *what*, so nothing
can ever go back.

WHY A LEDGER AND NOT A RETRY
----------------------------
Retrying inline is the wrong shape twice over. The work was shed BECAUSE the
pool was saturated, so retrying immediately re-enters the condition that
caused the shed -- and a sensor that blocks its scan retrying one file starves
the other twenty-one sensors sharing that pool. Deferral makes the decision
explicit and cheap: record it, finish the scan, revisit when there is room.

WHAT MAKES AN ENTRY HONEST
--------------------------
Each entry carries the REASON as a typed value, not prose, because the
reasons demand different treatments:

  * ``shed_pressure``  -- nothing wrong with the file; the pool was full.
                          Revisit freely, it will probably succeed.
  * ``timeout``        -- the analysis genuinely exceeded its budget. Revisit
                          with a larger budget or not at all.
  * ``pathological``   -- rejected pre-flight by shape (minified, binary,
                          absurd line density). Revisiting UNCHANGED is
                          pointless; only a content change should re-queue it.
  * ``too_large``      -- over the byte cap. Same reasoning as pathological.

Collapsing these into "failed" would make the ledger unactionable, which is
the state we are leaving.

BOUNDED BY CONSTRUCTION
-----------------------
A ledger of blind spots that grows without bound becomes a second blind spot.
Entries are keyed by path so re-shedding the same file updates rather than
appends, and the store is capped with oldest-eviction. Eviction is COUNTED, so
"we forgot that we forgot" is itself visible.

Python 3.9+, stdlib only. Fail-soft everywhere: a ledger that raises would
convert a deferral into the crash it exists to prevent.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.DeferredTaskLedger")

ENABLED_ENV = "JARVIS_DEFERRED_LEDGER_ENABLED"
PATH_ENV = "JARVIS_DEFERRED_LEDGER_PATH"
MAX_ENTRIES_ENV = "JARVIS_DEFERRED_LEDGER_MAX"

_TRUTHY = ("1", "true", "yes", "on")
_DEFAULT_PATH = os.path.join(".jarvis", "deferred_tasks.json")


def ledger_enabled() -> bool:
    """Master gate. Default ON — the ledger only ever ADDS information, and
    with it off a shed task is invisible again. NEVER raises."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def ledger_path() -> str:
    try:
        return os.environ.get(PATH_ENV, "") or _DEFAULT_PATH
    except Exception:  # noqa: BLE001
        return _DEFAULT_PATH


def max_entries() -> int:
    """Cap. Default 500. A blind-spot ledger that grows without bound is
    itself a blind spot."""
    try:
        return max(16, int(os.environ.get(MAX_ENTRIES_ENV, "500")))
    except (TypeError, ValueError):
        return 500


class DeferReason(str, enum.Enum):
    """WHY the work was not done. Typed because each implies a different
    revisit policy — see the module docstring."""

    SHED_PRESSURE = "shed_pressure"   # pool saturated; the file is fine
    TIMEOUT = "timeout"               # genuinely exceeded its budget
    PATHOLOGICAL = "pathological"     # rejected pre-flight by shape
    TOO_LARGE = "too_large"           # over the byte cap
    INTERNAL_ERROR = "internal_error"

    @property
    def worth_retrying_unchanged(self) -> bool:
        """Is a retry of the SAME bytes likely to behave differently?

        Only pressure and (marginally) timeout are transient. Re-running a
        minified file through the same guard yields the same refusal, so
        re-queueing it unchanged burns budget to learn nothing.
        """
        return self in (DeferReason.SHED_PRESSURE, DeferReason.TIMEOUT)


@dataclass(frozen=True)
class DeferredTask:
    """One thing the organism chose not to look at."""

    path: str
    reason: DeferReason
    caller: str = ""
    source_bytes: int = 0
    detail: str = ""
    first_deferred_at: float = 0.0
    last_deferred_at: float = 0.0
    occurrences: int = 1

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["reason"] = self.reason.value
        d["worth_retrying_unchanged"] = self.reason.worth_retrying_unchanged
        return d


class DeferredTaskLedger:
    """Bounded, path-keyed store of deferred work. Thread-safe, fail-soft."""

    __slots__ = ("_lock", "_entries", "_evicted", "_path")

    def __init__(self, path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, DeferredTask] = {}
        self._evicted = 0
        self._path = path or ledger_path()

    def defer(self, *, path: str, reason: DeferReason, caller: str = "",
              source_bytes: int = 0, detail: str = "",
              now: Optional[float] = None) -> None:
        """Record that *path* was not analysed. NEVER raises.

        Keyed BY PATH: re-shedding the same file updates its entry and bumps
        `occurrences` rather than appending. A file the pool sheds on every
        scan is one persistent blind spot, not fifty events, and counting it
        fifty times would drown the ledger in its own worst case.
        """
        if not ledger_enabled():
            return
        try:
            _now = time.time() if now is None else float(now)
            key = str(path or "")
            if not key:
                return
            with self._lock:
                prev = self._entries.get(key)
                if prev is not None:
                    self._entries[key] = DeferredTask(
                        path=key, reason=reason, caller=caller or prev.caller,
                        source_bytes=source_bytes or prev.source_bytes,
                        detail=detail or prev.detail,
                        first_deferred_at=prev.first_deferred_at or _now,
                        last_deferred_at=_now,
                        occurrences=prev.occurrences + 1,
                    )
                else:
                    self._entries[key] = DeferredTask(
                        path=key, reason=reason, caller=caller,
                        source_bytes=source_bytes, detail=detail,
                        first_deferred_at=_now, last_deferred_at=_now,
                    )
                # Oldest-first eviction, and the count is KEPT: a ledger that
                # silently forgot entries would be a blind spot about blind
                # spots.
                cap = max_entries()
                if len(self._entries) > cap:
                    victims = sorted(self._entries.values(),
                                     key=lambda e: e.last_deferred_at
                                     )[:len(self._entries) - cap]
                    for v in victims:
                        self._entries.pop(v.path, None)
                        self._evicted += 1
        except Exception:  # noqa: BLE001
            logger.debug("[DeferredLedger] defer degraded", exc_info=True)

    def resolve(self, path: str) -> None:
        """Drop *path* — it was successfully analysed. NEVER raises."""
        try:
            with self._lock:
                self._entries.pop(str(path or ""), None)
        except Exception:  # noqa: BLE001
            pass

    def retryable(self, limit: int = 50) -> List[DeferredTask]:
        """Deferrals a later scan should revisit, oldest blind spot first.

        Filters on `worth_retrying_unchanged`, so a minified file rejected by
        shape is remembered but never re-attempted until its bytes change.
        """
        try:
            with self._lock:
                items = [e for e in self._entries.values()
                         if e.reason.worth_retrying_unchanged]
            return sorted(items, key=lambda e: e.first_deferred_at)[:max(0, limit)]
        except Exception:  # noqa: BLE001
            return []

    def snapshot(self) -> Dict[str, Any]:
        """Observability surface. NEVER raises."""
        try:
            with self._lock:
                by_reason: Dict[str, int] = {}
                for e in self._entries.values():
                    by_reason[e.reason.value] = by_reason.get(e.reason.value, 0) + 1
                return {
                    "enabled": ledger_enabled(),
                    "total": len(self._entries),
                    "evicted": self._evicted,
                    "by_reason": by_reason,
                    "retryable": sum(
                        1 for e in self._entries.values()
                        if e.reason.worth_retrying_unchanged),
                }
        except Exception:  # noqa: BLE001
            return {"enabled": False, "error": "snapshot degraded"}

    def flush(self) -> bool:
        """Persist. Best-effort; a failed write must not break a scan."""
        try:
            if not ledger_enabled():
                return False
            with self._lock:
                payload = {
                    "schema_version": "1.0",
                    "written_at": time.time(),
                    "evicted": self._evicted,
                    "entries": [e.to_dict() for e in self._entries.values()],
                }
            d = os.path.dirname(self._path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self._path)
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[DeferredLedger] flush degraded", exc_info=True)
            return False

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._evicted = 0


_SINGLETON: Optional[DeferredTaskLedger] = None
_LOCK = threading.Lock()


def get_default_ledger() -> DeferredTaskLedger:
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = DeferredTaskLedger()
        return _SINGLETON


def reset_for_tests() -> None:
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
