"""Phase 8.4 — Master-flag change emitter.

Per `OUROBOROS_VENOM_PRD.md` §3.6.4:

  > Master-flag change SSE event — new `flag_changed` event when
  > any `JARVIS_*` env mutates mid-session.

With 481+ env flags governing autonomic behavior, an operator
toggling one mid-session has cascading effects that are currently
INVISIBLE to observability. This module ships a snapshot-and-diff
detector: at known sample points (operator-invoked or scheduled
sweep), compare current `JARVIS_*` env vars to a prior snapshot;
emit one ``FlagChangeEvent`` per delta.

## Why snapshot-and-diff (not env hooks)

POSIX has no API to subscribe to env changes. Python's `os.environ`
modifications go through `os.putenv` but there's no monitor. The
options:
  1. Subprocess /proc/<pid>/environ scraping — POSIX-only + racy
  2. Inotify on a shadow file — adds OS dependency
  3. **Snapshot-and-diff** at known sample points — portable, explicit

We pick (3). Sample points: at op start (snapshot baseline), at op
end (compare); at SIGUSR1 (operator on-demand); at scheduled
intervals (background tick).

## Default-off

`JARVIS_FLAG_CHANGE_EMITTER_ENABLED` (default false).

## Emit shape

```python
FlagChangeEvent(
    flag_name="JARVIS_HYPOTHESIS_PROBE_ENABLED",
    prev_value="false",
    next_value="true",
    ts_epoch=1714128000.0,
)
```

Production wires the emitter to the existing SSE event broker;
this module just produces the events. Wiring is a follow-up.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on tracked flags (defends against operator setting
# thousands of JARVIS_* env vars).
MAX_TRACKED_FLAGS: int = 1024

# Hard cap on per-value chars stored in the snapshot (defends
# against operator-typo with multi-MB env value).
MAX_VALUE_CHARS: int = 4096

# Prefix that scopes the emitter to JARVIS-relevant flags only.
TRACKED_PREFIX: str = "JARVIS_"

# Sensitive-value masking patterns (PRD §3.6.2 vector #9 — Wave 3
# hygiene 2026-05-05). When a flag name matches any of these
# substrings (case-insensitive), the persisted/projected value is
# replaced with a sha256 prefix + length so SSE consumers + audit
# ledgers never see raw secrets. Bytes-pinned: changing this list
# without updating the regression suite fails the AST authority
# pin in cleanup_invariants.py.
_SENSITIVE_NAME_TOKENS: FrozenSet[str] = frozenset({
    "key", "token", "secret", "password", "passwd",
    "pwd", "credential", "private", "auth", "session_id",
})


def _is_sensitive_flag(flag_name: str) -> bool:
    """True iff the flag's name suggests a credential / secret.
    Case-insensitive substring match against
    :data:`_SENSITIVE_NAME_TOKENS`."""
    if not flag_name:
        return False
    lowered = flag_name.lower()
    for token in _SENSITIVE_NAME_TOKENS:
        if token in lowered:
            return True
    return False


def _mask_value(value: Optional[str]) -> Optional[str]:
    """Replace a sensitive value with ``<MASKED:sha256[:8]:len=N>``.
    None passes through unchanged so add/remove transitions stay
    distinguishable from set-to-empty."""
    if value is None:
        return None
    try:
        import hashlib
        h = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"<MASKED:{h}:len={len(value)}>"
    except Exception:  # noqa: BLE001 — defensive
        return "<MASKED>"


def is_emitter_enabled() -> bool:
    """Master flag — ``JARVIS_FLAG_CHANGE_EMITTER_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED", "",
    ).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class FlagChangeEvent:
    """One env-flag delta. Frozen — append-only audit if persisted."""

    flag_name: str
    prev_value: Optional[str]
    next_value: Optional[str]
    ts_epoch: float

    @property
    def is_added(self) -> bool:
        return self.prev_value is None and self.next_value is not None

    @property
    def is_removed(self) -> bool:
        return self.prev_value is not None and self.next_value is None

    @property
    def is_changed(self) -> bool:
        return (
            self.prev_value is not None
            and self.next_value is not None
            and self.prev_value != self.next_value
        )

    def to_dict(self) -> Dict[str, Any]:
        """Project to dict for SSE / observability ledger
        emission. Values for credential-shaped flag names are
        masked via sha256[:8] + length token (PRD §3.6.2 vector
        #9 — defense-in-depth against accidental secret leak
        through the audit trail)."""
        sensitive = _is_sensitive_flag(self.flag_name)
        prev_v = (
            _mask_value(self.prev_value)
            if sensitive else self.prev_value
        )
        next_v = (
            _mask_value(self.next_value)
            if sensitive else self.next_value
        )
        return {
            "flag_name": self.flag_name,
            "prev_value": prev_v,
            "next_value": next_v,
            "ts_epoch": self.ts_epoch,
            "is_added": self.is_added,
            "is_removed": self.is_removed,
            "is_changed": self.is_changed,
            "value_masked": sensitive,
        }


def snapshot_flags(
    prefix: str = TRACKED_PREFIX,
) -> Dict[str, str]:
    """Return a frozen snapshot of all env vars matching ``prefix``.

    Bounded: at most MAX_TRACKED_FLAGS keys; per-value MAX_VALUE_CHARS.
    Used as the baseline for diff(prev, next).
    """
    out: Dict[str, str] = {}
    for k, v in os.environ.items():
        if not k.startswith(prefix):
            continue
        if len(out) >= MAX_TRACKED_FLAGS:
            break
        out[k] = v[:MAX_VALUE_CHARS]
    return out


def diff_snapshots(
    prev: Dict[str, str],
    next_: Dict[str, str],
    *,
    ts_epoch: Optional[float] = None,
) -> List[FlagChangeEvent]:
    """Compare two snapshots and return one event per delta.

    Returns empty list when:
      * Master flag off (skip computation)
      * Snapshots are identical

    Determinism: events emitted in alpha-sorted flag_name order.
    """
    if not is_emitter_enabled():
        return []
    ts = ts_epoch if ts_epoch is not None else time.time()
    out: List[FlagChangeEvent] = []
    all_keys = sorted(set(prev.keys()) | set(next_.keys()))
    for k in all_keys:
        prev_v = prev.get(k)
        next_v = next_.get(k)
        if prev_v == next_v:
            continue
        out.append(FlagChangeEvent(
            flag_name=k,
            prev_value=prev_v,
            next_value=next_v,
            ts_epoch=ts,
        ))
    return out


class FlagChangeMonitor:
    """Stateful monitor: holds the latest baseline snapshot;
    ``check()`` compares current env to baseline + updates baseline
    + returns the deltas.

    Used by the orchestrator (or a scheduled tick) to detect drift
    over an op or session.
    """

    def __init__(self, prefix: str = TRACKED_PREFIX) -> None:
        self._prefix = prefix
        self._baseline: Dict[str, str] = {}

    @property
    def baseline_size(self) -> int:
        return len(self._baseline)

    def initialize(self) -> None:
        """Set the baseline from the current env (call once at boot)."""
        self._baseline = snapshot_flags(self._prefix)

    def check(self) -> List[FlagChangeEvent]:
        """Compare current env to baseline, return deltas, advance
        baseline. Master-off → no-op + empty list."""
        if not is_emitter_enabled():
            return []
        current = snapshot_flags(self._prefix)
        deltas = diff_snapshots(self._baseline, current)
        self._baseline = current
        return deltas


_DEFAULT_MONITOR: Optional[FlagChangeMonitor] = None


def get_default_monitor() -> FlagChangeMonitor:
    global _DEFAULT_MONITOR
    if _DEFAULT_MONITOR is None:
        _DEFAULT_MONITOR = FlagChangeMonitor()
    return _DEFAULT_MONITOR


def reset_default_monitor() -> None:
    global _DEFAULT_MONITOR
    _DEFAULT_MONITOR = None


__all__ = [
    "FlagChangeEvent",
    "FlagChangeMonitor",
    "MAX_TRACKED_FLAGS",
    "MAX_VALUE_CHARS",
    "TRACKED_PREFIX",
    "diff_snapshots",
    "get_default_monitor",
    "is_emitter_enabled",
    "reset_default_monitor",
    "snapshot_flags",
]
