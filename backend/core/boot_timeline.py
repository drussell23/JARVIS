"""
boot_timeline - how far through startup JARVIS really is.

The loading page's percentage had several writers and none of them measured
anything: literal numbers at each phase (the trace said 45 where the
broadcast said 30), a heartbeat that crept upward with elapsed time, and the
loading server's readiness poller, which jumped to 85% at the backend's first
tier and declared 100% "JARVIS is online!" after a fixed grace -- about a
minute before the kernel finished (AGI OS, neural mesh and the UI still
starting). Phases are also not the whole boot: whole minutes pass between
them.

This model is the one answer. A boot is a TIMELINE: every phase boundary the
supervisor already announces on its event bus (``<phase>:start``,
``<phase>:end``) is a milestone at an offset from process start, and the
offsets are learned from completed boots (EMA, ``JARVIS_BOOT_TIMELINE_ALPHA``).
Progress is the position on the learned timeline:

* at a milestone, its learned fraction;
* between milestones, elapsed time against the learned gap -- but never
  past the next milestone that has not happened (a slower boot holds,
  it does not lie);
* 100% only when the supervisor declares startup complete; until then the
  last percent is withheld.

With no completed boot yet there is no timeline to measure against; the
model then counts milestones reached, approaching but never reaching the
end, and says it is learning.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PERCENT = 100


def _store_path() -> Path:
    explicit = os.environ.get("JARVIS_BOOT_TIMELINE_PATH", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".jarvis" / "boot_timeline.json"


def _alpha() -> float:
    try:
        return min(1.0, max(0.0, float(os.environ.get("JARVIS_BOOT_TIMELINE_ALPHA", "") or 0.5)))
    except ValueError:
        return 0.5


class BootTimeline:
    """One boot measured against the boots before it. Thread-safe."""

    def __init__(self, origin: float, store: Optional[Path] = None) -> None:
        self._origin = origin                      # wall-clock time the boot began
        self._store = store or _store_path()
        self._lock = threading.Lock()
        self._learned: Dict[str, float] = {}       # milestone -> learned offset (s)
        self._learned_total = 0.0                  # learned seconds to completion
        self._boots = 0
        self._seen: Dict[str, float] = {}          # milestone -> offset this boot
        self._open_phases: List[Tuple[str, float]] = []  # (phase, started at)
        self._completed_at: Optional[float] = None
        self._shown = 0.0                          # highest fraction reported
        self._load()

    # -- learning ----------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
            self._learned = {str(k): float(v) for k, v in data.get("milestones", {}).items()}
            self._learned_total = float(data.get("total", 0.0))
            self._boots = int(data.get("boots", 0))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        a = _alpha() if self._boots else 1.0
        total = self._completed_at - self._origin  # type: ignore[operator]
        learned = dict(self._learned)
        for key, offset in self._seen.items():
            learned[key] = a * offset + (1 - a) * learned[key] if key in learned else offset
        payload = {
            "boots": self._boots + 1,
            "total": a * total + (1 - a) * self._learned_total if self._boots else total,
            "milestones": learned,
        }
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError as exc:
            logger.debug("[BootTimeline] could not persist: %s", exc)

    # -- observing ---------------------------------------------------------
    def phase_started(self, phase: str, at: float) -> None:
        with self._lock:
            self._seen.setdefault(f"{phase}:start", max(0.0, at - self._origin))
            self._open_phases.append((phase, at))

    def phase_ended(self, phase: str, at: float) -> None:
        with self._lock:
            self._seen.setdefault(f"{phase}:end", max(0.0, at - self._origin))
            self._open_phases = [(p, t) for p, t in self._open_phases if p != phase]

    def completed(self, at: float) -> None:
        with self._lock:
            if self._completed_at is not None:
                return
            self._completed_at = at
            self._save()

    # -- reading -----------------------------------------------------------
    @property
    def learning(self) -> bool:
        return not self._learned_total

    def fraction(self, now: Optional[float] = None) -> float:
        """0..1; exactly 1 only once startup completed."""
        now = time.time() if now is None else now
        with self._lock:
            if self._completed_at is not None:
                return 1.0
            ceiling = (PERCENT - 1) / PERCENT    # the last percent is completion's
            if self.learning:
                n = len(self._seen)
                return min(ceiling, n / (n + 1)) if n else 0.0
            total = self._learned_total
            # Phases run in parallel, so they do not finish in the order the
            # timeline learned. A phase that is visibly still running caps the
            # position at its learned end: one finishing early elsewhere (the
            # 08:51 boot went 33% -> 57% in two seconds, then sat at 57% for
            # 27) must not carry the bar past it. A phase that never started
            # does not cap -- this boot may skip it.
            running = {phase for phase, _ in self._open_phases}
            cap = min((v for k, v in self._learned.items()
                       if k not in self._seen and k.rpartition(":")[2] == "end"
                       and k.rpartition(":")[0] in running), default=total)
            reached = [(self._learned[k], self._seen[k]) for k in self._seen
                       if k in self._learned and self._learned[k] <= cap]
            # The furthest point reached short of the cap, and when the boot
            # got there (the latest of those arrivals).
            last_learned = max((lv for lv, _ in reached), default=0.0)
            last_actual = max((av for _, av in reached), default=0.0)
            ahead = sorted(v for k, v in self._learned.items()
                           if k not in self._seen and v > last_learned)
            next_learned = min(ahead[0] if ahead else total, cap)
            gap = max(next_learned - last_learned, 0.0)
            elapsed_since = max(0.0, (now - self._origin) - last_actual)
            position = last_learned + min(gap, elapsed_since)
            fraction = min(ceiling, position / total) if total > 0 else 0.0
            # A phase starting late lowers the cap; the bar then holds -- it
            # never goes back on what it already showed.
            self._shown = max(self._shown, fraction)
            return self._shown

    def percent(self, now: Optional[float] = None) -> int:
        return int(self.fraction(now) * PERCENT)

    def current_phase(self) -> Optional[str]:
        with self._lock:
            return self._open_phases[-1][0] if self._open_phases else None

    def phases(self) -> List[Tuple[str, float, Optional[float]]]:
        """This boot's phases in the order they began: (name, started,
        ended or None while running), offsets in seconds from the origin."""
        with self._lock:
            seen = dict(self._seen)
        rows = []
        for key, started in seen.items():
            name, _, edge = key.rpartition(":")
            if edge == "start":
                rows.append((name, started, seen.get(f"{name}:end")))
        return sorted(rows, key=lambda row: row[1])

    def elapsed(self, now: Optional[float] = None) -> float:
        """Seconds since the boot began; frozen at completion."""
        with self._lock:
            end = self._completed_at
        end = end if end is not None else (time.time() if now is None else now)
        return max(0.0, end - self._origin)

    @property
    def complete(self) -> bool:
        return self._completed_at is not None

    def remaining_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Learned seconds left, or None while there is nothing to learn from."""
        now = time.time() if now is None else now
        if self.learning or self._completed_at is not None:
            return None if self.learning else 0.0
        return max(0.0, self._learned_total - (now - self._origin))


__all__ = ["BootTimeline", "PERCENT"]
