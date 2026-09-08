"""Per-target exponential cooldown — the brake on a runaway autonomous loop.

An organism that picks its own work will pick the SAME work again the moment it
fails, because the signal that produced the target (an ambient red, an uncovered
module) is still there — the failure did not remove it. Without a memory of
"I already tried this and it did not work", the discovery loop is a spin: the
same file, the same generation, the same refusal, forever, at full model cost.

This is that memory. A target that fails is put on a cooldown that DOUBLES with
each consecutive failure, so the loop degrades gracefully from "try again soon"
to "leave it alone" without anyone deciding a cutoff in advance. A target that
SUCCEEDS is forgiven completely — the backoff measures consecutive failure, not
history, or one bad afternoon would poison a file permanently.

## Nothing here is a hardcoded constant

Every bound is derived from the session's own budget, so a 20-minute cockpit and
a 9000-second soak get proportionate behaviour from the same code:

    base    = pipeline_timeout / _BASE_DIVISOR   (one target-attempt's worth)
    delay   = base * 2**(failures - 1)
    ceiling = wall clock of the session

A cooldown longer than the session it lives in is just "never again", so the
ceiling is the session — which also means the ledger cannot silently retire a
target across runs it was never asked to retire it across.

## Durability

The ledger is JSON on disk under the ouroboros state dir, so a restart does not
hand the loop a clean slate and let it re-attack a target it just abandoned.
Every read and write is fail-soft: a corrupt or unwritable ledger degrades to
"nothing is cooling", which is the pre-existing behaviour, never a crash — a
brake that cannot be read must not stop the organism, only stop braking.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.SentinelCooldown")

__all__ = [
    "CooldownEntry",
    "TargetCooldownLedger",
    "get_default_ledger",
    "reset_default_ledger",
]

#: How many target-attempts fit in one pipeline budget. The first cooldown is
#: "about as long as one attempt takes", which is the only non-arbitrary
#: starting point available — it is measured from the session, not chosen.
_BASE_DIVISOR = 4.0
_ENV_STATE_DIR = "OUROBOROS_STATE_DIR"
_ENV_PIPELINE = "JARVIS_PIPELINE_TIMEOUT_S"
_ENV_WALL = "JARVIS_SENTINEL_WALL_S"


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _base_delay_s() -> float:
    """One target-attempt's worth of time, derived from the pipeline budget."""
    pipeline = _env_float(_ENV_PIPELINE, 0.0)
    if pipeline <= 0:
        # No envelope hydrated (a bare process). Fall back to the approval
        # deadline, which production_envelope derives from the same budget.
        pipeline = _env_float("JARVIS_APPROVAL_DEADLINE_S", 0.0) * 2.0
    if pipeline <= 0:
        # Nothing to derive from at all. One minute is not a policy — it is
        # the smallest interval that cannot itself become a spin.
        return 60.0
    return max(30.0, pipeline / _BASE_DIVISOR)


def _ceiling_s() -> float:
    """A cooldown longer than the session is just 'never again'."""
    wall = _env_float(_ENV_WALL, 0.0)
    if wall <= 0:
        wall = _base_delay_s() * 32.0
    return max(_base_delay_s(), wall)


@dataclass(frozen=True)
class CooldownEntry:
    """What the ledger remembers about one target."""

    target: str
    consecutive_failures: int
    until_epoch_s: float
    last_reason: str = ""

    def remaining_s(self, *, now: Optional[float] = None) -> float:
        return max(0.0, self.until_epoch_s - (time.time() if now is None else now))

    def is_cooling(self, *, now: Optional[float] = None) -> bool:
        return self.remaining_s(now=now) > 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "consecutive_failures": self.consecutive_failures,
            "until_epoch_s": self.until_epoch_s,
            "last_reason": self.last_reason[:300],
        }

    @staticmethod
    def from_dict(data: Any) -> "Optional[CooldownEntry]":
        if not isinstance(data, dict):
            return None
        try:
            return CooldownEntry(
                target=str(data["target"]),
                consecutive_failures=int(data.get("consecutive_failures", 1)),
                until_epoch_s=float(data.get("until_epoch_s", 0.0)),
                last_reason=str(data.get("last_reason", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _state_dir() -> Path:
    raw = (os.environ.get(_ENV_STATE_DIR, "") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".jarvis" / "ouroboros"


class TargetCooldownLedger:
    """Durable, exponential, per-target backoff. NEVER raises."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else (
            _state_dir() / "sentinel_cooldown.json"
        )
        self._entries: Dict[str, CooldownEntry] = {}
        self._loaded = False

    # -- persistence ------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return          # absent or corrupt -> nothing is cooling
        if not isinstance(raw, dict):
            return
        for item in raw.get("entries", []):
            entry = CooldownEntry.from_dict(item)
            if entry is not None:
                self._entries[entry.target] = entry

    def _save(self) -> None:
        """Atomic replace so a crash mid-write cannot corrupt the ledger."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "sentinel_cooldown/1",
                "entries": [e.to_dict() for e in self._entries.values()],
            }
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".cooldown-", suffix=".json",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001 — a brake that cannot persist still brakes in-process
            logger.debug("[SentinelCooldown] persist degraded", exc_info=True)

    # -- the contract -----------------------------------------------------

    def delay_for(self, failures: int) -> float:
        """The backoff for the *n*-th consecutive failure, bounded."""
        n = max(1, int(failures))
        # 2**(n-1) grows fast; clamp the EXPONENT before the multiply so a
        # pathological failure count cannot produce an overflow instead of a
        # ceiling.
        exponent = min(n - 1, 32)
        return min(_base_delay_s() * (2.0 ** exponent), _ceiling_s())

    def record_failure(self, target: str, *, reason: str = "") -> CooldownEntry:
        """Escalate *target*'s cooldown. Returns the new entry."""
        self._load()
        key = str(target or "").strip()
        if not key:
            return CooldownEntry("", 0, 0.0)
        prior = self._entries.get(key)
        failures = (prior.consecutive_failures + 1) if prior else 1
        delay = self.delay_for(failures)
        entry = CooldownEntry(
            target=key,
            consecutive_failures=failures,
            until_epoch_s=time.time() + delay,
            last_reason=str(reason or "")[:300],
        )
        self._entries[key] = entry
        self._save()
        logger.warning(
            "[SentinelCooldown] %s failure #%d -> cooling %.0fs (%s)",
            key, failures, delay, reason or "no reason given",
        )
        return entry

    def record_success(self, target: str) -> None:
        """Forgive *target* completely.

        Backoff measures CONSECUTIVE failure. Keeping a decayed count after a
        success would let one bad afternoon poison a file for the rest of the
        session, and the organism would learn to avoid exactly the code it had
        just proven it can fix.
        """
        self._load()
        key = str(target or "").strip()
        if key in self._entries:
            del self._entries[key]
            self._save()
            logger.info("[SentinelCooldown] %s succeeded — cooldown cleared", key)

    def is_cooling(self, target: str, *, now: Optional[float] = None) -> bool:
        self._load()
        entry = self._entries.get(str(target or "").strip())
        return bool(entry and entry.is_cooling(now=now))

    def entry_for(self, target: str) -> Optional[CooldownEntry]:
        self._load()
        return self._entries.get(str(target or "").strip())

    def filter_available(self, targets, *, now: Optional[float] = None) -> Tuple[str, ...]:
        """The subset of *targets* not currently cooling, order preserved."""
        self._load()
        out = []
        for t in targets or ():
            key = str(t or "").strip()
            if key and not self.is_cooling(key, now=now):
                out.append(key)
        return tuple(out)

    def cooling_now(self, *, now: Optional[float] = None) -> Tuple[CooldownEntry, ...]:
        """Every entry still cooling — the operator's view of what is parked."""
        self._load()
        return tuple(
            sorted(
                (e for e in self._entries.values() if e.is_cooling(now=now)),
                key=lambda e: e.until_epoch_s,
            )
        )

    def prune_expired(self, *, now: Optional[float] = None) -> int:
        """Drop entries whose cooldown has elapsed. Returns how many went."""
        self._load()
        stale = [k for k, e in self._entries.items() if not e.is_cooling(now=now)]
        for k in stale:
            del self._entries[k]
        if stale:
            self._save()
        return len(stale)


_DEFAULT: Optional[TargetCooldownLedger] = None


def get_default_ledger() -> TargetCooldownLedger:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TargetCooldownLedger()
    return _DEFAULT


def reset_default_ledger() -> None:
    """Test seam — drops the process-wide ledger."""
    global _DEFAULT
    _DEFAULT = None
