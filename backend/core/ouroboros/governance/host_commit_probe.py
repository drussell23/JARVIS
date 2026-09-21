"""Host commit probe — the memory number that actually kills this machine.

## Why ``psutil`` inside WSL is the wrong instrument

Soak bt-2026-09-20-183259 was stopped for HOST memory pressure while the guest
looked idle. Read at the same instant, from the same shell:

    WSL guest   (/proc/meminfo — what psutil reports)   47.9 of 49.3 GB free   97 %
    Windows host commit                                  40.9 of 105.6 GB free  39 %

Those are different machines. Windows charges the whole WSL VM — guest memory
ever touched, page cache included, plus whatever CUDA spills from a full card
into system RAM — against its COMMIT limit, and when commit runs out the
desktop dies (2026-09-04: ``dwm.exe``, Explorer, the NVIDIA container). Nothing
in the guest reports that number. A watchdog on "guest available < 10 %" would
have read 87 % free at the moment the soak was killed: a gate that cannot fire.

So the number is read from the host, through WSL interop:

    Win32_OperatingSystem.FreeVirtualMemory / TotalVirtualMemorySize   (kB)

which is commit available / commit limit.

## Never on the event loop, never on the asking thread

One read is a PowerShell spawn: ~400 ms. ``MemoryPressureGate.pressure()`` is
synchronous and is called from the loop by five consumers, so the read lives on
its own daemon OS THREAD and the gate only ever looks at the last sample. The
cadence is adaptive — the base interval while there is headroom, tightening
linearly to a floor as free commit falls from the WARN threshold to the
CRITICAL one — because commit has been measured moving at ~1 GiB/s during a
model load, and a fixed 15 s poll would sleep through the entire event.

## Fails OPEN, and says nothing when it has nothing to say

Not WSL, no PowerShell, a timeout, a stale sample: all answer "unknown", which
the gate reads as OK. This dimension can only ever make the gate MORE
conservative; the in-process ``ProcessMemoryWatchdog`` remains the hard stop.
NEVER raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_HOST_COMMIT_PROBE_ENABLED"
_ENV_INTERVAL_S = "JARVIS_HOST_COMMIT_INTERVAL_S"
_ENV_MIN_INTERVAL_S = "JARVIS_HOST_COMMIT_MIN_INTERVAL_S"
_ENV_TIMEOUT_S = "JARVIS_HOST_COMMIT_TIMEOUT_S"
_ENV_POWERSHELL = "JARVIS_HOST_POWERSHELL"

_DEFAULT_INTERVAL_S = 15.0
_DEFAULT_MIN_INTERVAL_S = 2.0
_DEFAULT_TIMEOUT_S = 10.0

_QUERY = (
    "$o=Get-CimInstance Win32_OperatingSystem;"
    "'{0} {1} {2} {3}' -f $o.FreeVirtualMemory,$o.TotalVirtualMemorySize,"
    "$o.FreePhysicalMemory,$o.TotalVisibleMemorySize"
)


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
        return value if value >= minimum else default
    except (TypeError, ValueError):
        return default


def probe_enabled() -> bool:
    try:
        return os.environ.get(_ENV_ENABLED, "true").strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return True


@dataclass(frozen=True)
class HostCommit:
    free_kb: int
    limit_kb: int
    phys_free_kb: int
    phys_total_kb: int
    at: float  # time.monotonic() of the read

    @property
    def free_pct(self) -> float:
        return 100.0 * self.free_kb / self.limit_kb if self.limit_kb > 0 else 100.0

    @property
    def free_gib(self) -> float:
        return self.free_kb / (1024.0 * 1024.0)

    def render(self) -> str:
        return (
            f"host commit {self.free_gib:.1f} GiB free of "
            f"{self.limit_kb / (1024.0 * 1024.0):.1f} ({self.free_pct:.0f}%)"
        )


def is_wsl() -> bool:
    try:
        if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
            return True
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


def _powershell() -> Optional[str]:
    explicit = (os.environ.get(_ENV_POWERSHELL, "") or "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("powershell.exe")
    if found:
        return found
    # Service accounts often carry no Windows PATH. The system root is where
    # WSL mounts it; ask the mount table rather than assume a drive letter.
    try:
        for line in Path("/proc/mounts").read_text(errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[2] in ("9p", "drvfs"):
                candidate = Path(fields[1]) / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                if candidate.exists():
                    return str(candidate)
    except OSError:
        pass
    return None


def available() -> bool:
    """Whether the host can be read from here at all."""
    return probe_enabled() and is_wsl() and _powershell() is not None


def parse(raw: str, at: float) -> Optional[HostCommit]:
    try:
        fields = raw.replace("\r", " ").split()
        free, limit, pfree, ptotal = (int(float(x)) for x in fields[:4])
        if limit <= 0 or free < 0 or free > limit:
            return None
        return HostCommit(free, limit, pfree, ptotal, at)
    except (ValueError, IndexError, TypeError):
        return None


def read_host_commit() -> Optional[HostCommit]:
    """ONE synchronous read (~400 ms). Call from a worker thread only."""
    try:
        shell = _powershell()
        if shell is None or not is_wsl():
            return None
        done = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            timeout=_env_float(_ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S, 0.5), check=False,
        )
        if done.returncode != 0:
            return None
        return parse(done.stdout.decode("utf-8", errors="replace"), time.monotonic())
    except (OSError, subprocess.SubprocessError):
        return None
    except Exception:  # noqa: BLE001
        logger.debug("[HostCommit] read degraded", exc_info=True)
        return None


class HostCommitSampler:
    """Daemon OS thread that keeps the last host-commit sample fresh."""

    def __init__(
        self,
        *,
        warn_pct: Callable[[], float],
        critical_pct: Callable[[], float],
        reader: Callable[[], Optional[HostCommit]] = read_host_commit,
    ) -> None:
        self._warn_pct = warn_pct
        self._critical_pct = critical_pct
        self._reader = reader
        self._lock = threading.Lock()
        self._latest: Optional[HostCommit] = None
        self._interval_s = _env_float(_ENV_INTERVAL_S, _DEFAULT_INTERVAL_S, 0.1)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.reads = 0
        self.failures = 0

    # -- cadence -----------------------------------------------------------

    def interval_for(self, free_pct: Optional[float]) -> float:
        """Base interval with headroom; linear down to the floor between the
        WARN and CRITICAL thresholds — the gate's own, so there is ONE
        definition of "getting close"."""
        base = _env_float(_ENV_INTERVAL_S, _DEFAULT_INTERVAL_S, 0.1)
        floor = min(base, _env_float(_ENV_MIN_INTERVAL_S, _DEFAULT_MIN_INTERVAL_S, 0.05))
        if free_pct is None:
            return base
        try:
            warn, critical = float(self._warn_pct()), float(self._critical_pct())
        except Exception:  # noqa: BLE001
            return base
        if warn <= critical:
            return base
        headroom = (free_pct - critical) / (warn - critical)
        headroom = 0.0 if headroom < 0.0 else 1.0 if headroom > 1.0 else headroom
        return floor + (base - floor) * headroom

    @property
    def interval_s(self) -> float:
        with self._lock:
            return self._interval_s

    # -- lifecycle ---------------------------------------------------------

    def sample_once(self) -> Optional[HostCommit]:
        sample = None
        try:
            sample = self._reader()
        except Exception:  # noqa: BLE001
            logger.debug("[HostCommit] reader raised", exc_info=True)
        with self._lock:
            self.reads += 1
            if sample is None:
                self.failures += 1
            else:
                self._latest = sample
            self._interval_s = self.interval_for(None if sample is None else sample.free_pct)
        return sample

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self.interval_s)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="host-commit-sampler", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> Optional[HostCommit]:
        """The last sample, or ``None`` when there is none FRESH enough to act
        on. A value older than three cadences plus one read is a statement
        about the past; acting on it would shed work over a number that may
        have recovered, or — worse — read healthy over one that has not."""
        with self._lock:
            sample, interval = self._latest, self._interval_s
        if sample is None:
            return None
        limit = 3.0 * interval + _env_float(_ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S, 0.5)
        return sample if (time.monotonic() - sample.at) <= limit else None


_sampler: Optional[HostCommitSampler] = None
_sampler_lock = threading.Lock()


def start_sampler(
    *, warn_pct: Callable[[], float], critical_pct: Callable[[], float],
) -> Optional[HostCommitSampler]:
    """Start the process-wide sampler. ``None`` where the host is unreadable —
    EXPLICIT, by the daemon at boot: a library import (or a unit test touching
    the gate) must never start spawning PowerShell on its own."""
    global _sampler
    if not available():
        return None
    with _sampler_lock:
        if _sampler is None:
            _sampler = HostCommitSampler(warn_pct=warn_pct, critical_pct=critical_pct)
        _sampler.start()
        return _sampler


def latest_sample() -> Optional[HostCommit]:
    with _sampler_lock:
        sampler = _sampler
    return None if sampler is None else sampler.latest()


def current_interval_s() -> float:
    with _sampler_lock:
        sampler = _sampler
    if sampler is None:
        return _env_float(_ENV_INTERVAL_S, _DEFAULT_INTERVAL_S, 0.1)
    return sampler.interval_s


def stop_sampler() -> None:
    global _sampler
    with _sampler_lock:
        if _sampler is not None:
            _sampler.stop()
        _sampler = None


__all__ = [
    "HostCommit",
    "HostCommitSampler",
    "available",
    "current_interval_s",
    "is_wsl",
    "latest_sample",
    "parse",
    "probe_enabled",
    "read_host_commit",
    "start_sampler",
    "stop_sampler",
]
