"""What the organism is actually doing while you wait for it.

THE DEFECT THIS REPLACES
------------------------
The cold-boot wait printed a fresh line every five seconds::

    ⎿ organism waking · 0s
    ⎿ organism waking · 5s
    ⎿ organism waking · 10s
    ⎿ organism waking · 15s
    ⎿ organism waking · 21s
    ⎿ organism waking · 27s

Six lines that say one thing: time is passing. They do not say what stage the
boot reached, whether it is progressing or wedged, or how much longer it will
take — and the repetition itself reads as a stall, which is the opposite of
the reassurance a wait indicator exists to give.

TWO SOURCES OF TRUTH, IN PRIORITY ORDER
---------------------------------------
1. **EVIDENCE — stages actually reached.** The daemon already writes its boot
   to `.jarvis/logs/ov-daemon.log`; the client already knows that path (the
   failure message points at it). Watching it costs one tail per poll and
   yields REAL progress: preflight passed, socket bound, session opened,
   sensors armed. This is measurement, not animation.

2. **ESTIMATE — how long this usually takes.** Boot durations are recorded to
   a small ledger and the median of past boots gives an expected duration.
   Exactly the pattern `session_economics.derived_cost_cap` uses for money:
   the operator's own history, not a constant somebody guessed.

Evidence outranks estimate. When both exist the bar interpolates smoothly
between confirmed stages using elapsed time, so it moves continuously without
ever claiming a stage that has not happened.

THREE HONESTY RULES
-------------------
* **Never regress.** A percentage that goes backwards destroys the one thing a
  progress bar is for. Monotonic by construction.
* **Never reach 100% before the socket is live.** The last few percent are
  reserved for the event that actually matters. A bar that sits at 100% while
  nothing happens is worse than no bar.
* **Degrade to elapsed-only rather than lie.** No history and no matched
  markers means no percentage — just the stage and the clock. A log format
  change therefore costs the bar, never its correctness.

Python 3.9+, stdlib only. Never raises: a progress indicator that can break a
boot is not worth having.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.BootProgress")

ENABLED_ENV = "JARVIS_OV_BOOT_PROGRESS_ENABLED"
HISTORY_ENV = "JARVIS_OV_BOOT_HISTORY_PATH"
MAX_SAMPLES_ENV = "JARVIS_OV_BOOT_HISTORY_MAX"
CEILING_ENV = "JARVIS_OV_BOOT_PROGRESS_CEILING"

_TRUTHY = ("1", "true", "yes", "on")
_DEFAULT_HISTORY = os.path.join(".jarvis", "boot_durations.json")


def boot_progress_enabled() -> bool:
    """Master gate. Default ON. OFF restores the plain elapsed breadcrumb."""
    try:
        return os.environ.get(ENABLED_ENV, "1").strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return True


def history_path() -> str:
    try:
        return os.environ.get(HISTORY_ENV, "") or _DEFAULT_HISTORY
    except Exception:  # noqa: BLE001
        return _DEFAULT_HISTORY


def max_samples() -> int:
    """Boot durations retained. Default 40 — enough for a stable median,
    short enough that a machine that got faster is reflected within days."""
    try:
        return max(3, int(os.environ.get(MAX_SAMPLES_ENV, "40")))
    except (TypeError, ValueError):
        return 40


def progress_ceiling() -> float:
    """The highest fraction shown before the socket is confirmed live.

    Default 0.97. The remaining 3% belongs to the event that actually
    matters; a bar that sits at 100% while nothing happens has told the
    operator the boot finished when it has not."""
    try:
        v = float(os.environ.get(CEILING_ENV, "0.97"))
        return min(0.99, max(0.50, v))
    except (TypeError, ValueError):
        return 0.97


@dataclass(frozen=True)
class BootStage:
    """One observable milestone, and the log marker that proves it."""

    key: str
    label: str
    marker: str          # substring that appears in the daemon log
    weight: float = 1.0  # relative share of the boot this stage represents


#: The stages, in order. Weights are ROUGH SHARES, not durations: the ETA
#: comes from measured history, so these only decide how the bar distributes
#: itself between confirmed milestones.
#:
#: Markers are substrings of log lines the daemon already writes. That is a
#: coupling to log text, which is why an unmatched table degrades to
#: elapsed-only rather than to a wrong number — see `Progress.fraction`.
DEFAULT_STAGES: Tuple[BootStage, ...] = (
    BootStage("preflight", "preflight", "[AegisPreflight]", 1.0),
    BootStage("credentials", "credentials", "[CredentialBootstrap]", 0.5),
    BootStage("aegis", "aegis serving", "[AegisDaemon] serving on", 1.5),
    BootStage("ready", "aegis ready", "daemon ready at", 0.5),
    BootStage("session", "session open", "session=bt-", 1.0),
    BootStage("status", "cockpit wired", "StatusLineBuilder registered", 1.5),
    BootStage("sensors", "sensors arming", "IntakeLayer", 2.0),
)


def observed_boot_durations(path: Optional[str] = None) -> List[float]:
    """Durations of past successful boots, seconds. NEVER raises."""
    try:
        p = path or history_path()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        out = [float(x) for x in (data.get("durations") or [])
               if isinstance(x, (int, float)) and 0.0 < float(x) < 3600.0]
        return out
    except Exception:  # noqa: BLE001
        return []


def record_boot_duration(seconds: float, path: Optional[str] = None) -> None:
    """Append one SUCCESSFUL boot duration. NEVER raises.

    Only successes are recorded. A failed or abandoned boot has no duration —
    folding one in would teach the estimator that boots take as long as the
    operator's patience, which is the number it exists to replace.
    """
    try:
        if not (0.0 < float(seconds) < 3600.0):
            return
        p = path or history_path()
        rows = observed_boot_durations(p)
        rows.append(float(seconds))
        rows = rows[-max_samples():]
        d = os.path.dirname(os.path.abspath(p))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "1.0", "durations": rows}, fh)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def expected_boot_s(path: Optional[str] = None) -> Optional[float]:
    """Median of observed boots, or None when nothing has been measured.

    None rather than a default: an invented expectation produces a confident
    percentage with nothing behind it, and the operator cannot tell the
    difference. No history means no bar.
    """
    rows = sorted(observed_boot_durations(path))
    if len(rows) < 3:
        return None
    return rows[len(rows) // 2]


@dataclass
class Progress:
    """Mutable boot state. One instance per wait."""

    stages: Tuple[BootStage, ...] = DEFAULT_STAGES
    log_path: str = ""
    expected_s: Optional[float] = None
    #: Log size when this wait began. Everything before it belongs to a
    #: PREVIOUS boot and must not count toward this one.
    log_origin: int = 0
    _reached: int = 0
    _high_water: float = 0.0
    _last_stage_at: float = 0.0
    _seen: set = field(default_factory=set)
    #: Elapsed-at-arrival for each stage reached IN THIS BOOT. The basis for
    #: projecting the rest when no cross-boot history exists yet.
    _stage_times: List[float] = field(default_factory=list)

    def observe_log(self, text: str, *, now: float) -> None:
        """Fold in whatever the daemon has written so far. NEVER raises."""
        try:
            for i, st in enumerate(self.stages):
                if st.key in self._seen:
                    continue
                if st.marker and st.marker in text:
                    self._seen.add(st.key)
                    if i + 1 > self._reached:
                        self._reached = i + 1
                        self._last_stage_at = now
                        self._stage_times.append(float(now))
        except Exception:  # noqa: BLE001
            pass

    def _projected_total_s(self, elapsed: float) -> Optional[float]:
        """Expected total boot time, projected from THIS boot's own cadence.

        The first boots on a machine have no history, so `expected_s` is None
        and the bar could only move when a marker landed — it sat frozen at
        one percentage between stages, which is what the operator sees as a
        hang. But a boot in progress is already evidence about itself: if four
        stages arrived over twelve seconds, the remaining three will plausibly
        take about nine more.

        This is measurement, not a constant — it adapts to a slow disk, a cold
        page cache or a loaded machine, and it needs no prior run. Requires
        TWO arrivals so there is an actual interval to average; one timestamp
        is a point, not a rate.
        """
        try:
            if self._reached <= 0 or not self._stage_times:
                return None
            # Rate measured FROM THE START OF THE WAIT, not between arrivals.
            #
            # The interval form needed two distinct timestamps and returned
            # nothing when several markers landed in the same poll — which is
            # the common case on a fast boot, since the client reads the log
            # tail every quarter second and a burst of stages can complete
            # between two reads. Anchoring on t=0 makes a single arrival
            # sufficient: two stages reached by second four is two seconds a
            # stage, and that is a rate, not a point.
            last = float(self._stage_times[-1])
            if last <= 0:
                return None
            per_stage = last / float(self._reached)
            if per_stage <= 0:
                return None
            projected = per_stage * float(len(self.stages))
            # Never project a finish already in the past: a boot running long
            # has disproved its own estimate, and an ETA of "0s left" that
            # keeps not arriving is worse than no ETA.
            return max(elapsed, projected)
        except Exception:  # noqa: BLE001
            return None

    @property
    def stage_label(self) -> str:
        if self._reached <= 0:
            return "igniting"
        return self.stages[min(self._reached, len(self.stages)) - 1].label

    def fraction(self, elapsed: float) -> Optional[float]:
        """Best honest estimate of completion, or None. NEVER decreases.

        Returns None when there is neither evidence nor history — the honest
        rendering of "I don't know how far along this is" is no number at all.
        """
        try:
            total_w = sum(s.weight for s in self.stages) or 1.0
            evidence = None
            if self._reached > 0:
                done = sum(s.weight for s in self.stages[:self._reached])
                evidence = done / total_w
            # Cross-boot history is the better estimator when it exists (more
            # samples, and it already knows how this machine behaves). Within
            # -boot projection is the fallback that makes a FIRST boot move.
            horizon = self.expected_s
            if not horizon or horizon <= 0:
                horizon = self._projected_total_s(elapsed)
            estimate = None
            if horizon and horizon > 0:
                estimate = elapsed / horizon

            if evidence is None and estimate is None:
                return None
            if evidence is None:
                value = estimate
            elif estimate is None:
                value = evidence
            else:
                # Evidence sets the floor; the estimate may only interpolate
                # ABOVE it, and never past the next unconfirmed stage. That is
                # what keeps the bar moving between milestones without ever
                # claiming one that has not happened.
                nxt = (sum(s.weight for s in self.stages[:self._reached + 1])
                       / total_w) if self._reached < len(self.stages) else 1.0
                value = max(evidence, min(estimate, nxt))

            value = min(progress_ceiling(), max(0.0, float(value)))
            # MONOTONIC. A bar that goes backwards destroys the only thing it
            # is for, and both inputs can legitimately fall (history reloads,
            # a marker arrives late).
            self._high_water = max(self._high_water, value)
            return self._high_water
        except Exception:  # noqa: BLE001
            return None

    def render(self, elapsed: float, *, width: int = 18) -> str:
        """One line. Bar + stage + clock + ETA when known. NEVER raises."""
        try:
            frac = self.fraction(elapsed)
            parts = []
            if frac is not None:
                filled = int(round(frac * width))
                parts.append("[" + "█" * filled + "·" * (width - filled) + "]")
                parts.append(f"{int(frac * 100):3d}%")
            parts.append(self.stage_label)
            parts.append(f"{int(elapsed)}s")
            horizon = self.expected_s or self._projected_total_s(elapsed)
            if frac is not None and horizon and frac > 0.02:
                remaining = max(0.0, horizon - elapsed)
                if remaining >= 1.0:
                    parts.append(f"~{int(remaining)}s left")
            return "  ".join(parts)
        except Exception:  # noqa: BLE001
            return f"waking · {int(elapsed)}s"


def log_size(path: str) -> int:
    """Current size of the daemon log, or 0. NEVER raises."""
    try:
        return os.path.getsize(path) if path and os.path.exists(path) else 0
    except Exception:  # noqa: BLE001
        return 0


def read_log_tail(path: str, *, since: int = 0, max_bytes: int = 65536) -> str:
    """Log bytes written AFTER *since*, capped at *max_bytes*. NEVER raises.

    `since` is load-bearing, not an optimisation. The daemon log is APPEND-ONLY
    ACROSS RUNS, so its tail already contains every boot marker from every
    previous boot. Reading it unanchored made a fresh wait match all seven
    stages on its first poll and render 97% instantly — a bar that is
    complete before the work starts, which is worse than no bar at all.
    Anchoring at the size observed when the wait began means only THIS boot's
    output can advance it.

    Still bounded: the log reaches tens of megabytes, and reading it whole on
    every poll would make the progress indicator the slowest thing in the boot.
    """
    try:
        if not path or not os.path.exists(path):
            return ""
        size = os.path.getsize(path)
        start = max(0, int(since))
        if size <= start:
            return ""                      # nothing new since the wait began
        start = max(start, size - max_bytes)
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def make_progress(log_path: str = "") -> Progress:
    """A Progress primed with this machine's history AND anchored to now.

    The anchor is taken at construction, which is the moment the wait starts —
    any later and markers from the boot's own first milliseconds would be
    skipped; any earlier and the previous boot's tail would be counted.
    """
    return Progress(log_path=log_path, expected_s=expected_boot_s(),
                    log_origin=log_size(log_path))
