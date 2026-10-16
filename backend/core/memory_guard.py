"""
memory_guard - stop the kernel before memory takes the desktop with it.

Why
---
On 2026-10-15 the host sat at 88-92 % RAM from 01:58 to 04:31 while JARVIS
ran: the kernel's event loop wedged for two hours, every cleanup freed 0 MB,
and the desktop was effectively gone. Nothing in the kernel can be trusted to
stop the kernel in that state -- a wedged loop runs no watchdog -- so this
guard runs in its OWN process and shares nothing with the kernel but the
lock file it reads (CLAUDE.md, Watchdog Isolation Invariant).

What it watches
---------------
* host RAM, judged by THE host pressure policy
  (``memory_types.host_pressure_policy``: tiers derived from this machine's
  RAM beside the model J-Prime holds on purpose, re-measured every tick, with
  the policy's own hysteresis and dwell);
* host commit, judged by the memory-pressure gate's levels
  (``host_commit_probe`` reads it natively on Windows, via interop in WSL);
* the kernel's process tree (the live holder of ``kernel.lock``, followed
  across restarts), with J-Prime/Reactor serving trees reported separately,
  against THE process-tree budget every Trinity tree answers to
  (``process_memory_budget.resolve_process_memory_thresholds``, the same
  warn/cap the O+V soak's ProcessMemoryWatchdog enforces);
* the kernel tree's growth, fitted by the memtrace line fit
  (``memory_growth_tracer.fit_growth`` / ``growth_verdict``).

Every tick is one JSON line in ``host_platform.log_dir()``
(``memory-guard-<ts>.jsonl``), so a run can be read back afterwards
(``python -m backend.core.memory_guard report <journal>``).

When it acts
------------
Only when a dimension has held at or past its trip level
(``JARVIS_MEMORY_GUARD_TRIP_TIER``, default the policy's CRITICAL tier;
``JARVIS_MEMORY_GUARD_COMMIT_TRIP_LEVEL``, default the gate's CRITICAL) for
the policy's dwell, AND stopping the kernel would bring that dimension back
under the trip tier's EXIT threshold. A breach the kernel is not causing is
journalled with the top consumers and the kernel is left alone: stopping it
would cost JARVIS and save nothing.

It stops the kernel the way a launcher hands one over
(``daemon_handover.hand_over``): a shutdown through the kernel's command
socket, its shutdown streamed while it talks, and its process tree
terminated only after silence. Then it keeps watching for the next kernel.

The kernel arms its own guard right after it takes ``kernel.lock``
(:func:`arm`, ``JARVIS_MEMORY_GUARD_ENABLED``), the way the soak harness arms
its external sentinel: a detached process in its own process group that
exits when that kernel does. Run by hand it follows whichever kernel holds
the lock.

The cadence follows headroom: the host-commit sampler's adaptive interval,
never slower than half the dwell, so a breach is confirmed by at least two
readings before anything happens. Never raises out of a tick.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("jarvis.memory_guard")

_MB = 1024.0 * 1024.0


def _tier_named(name: str, default):
    from backend.core.memory_types import PressureTier
    try:
        return PressureTier[(name or "").strip().upper()]
    except KeyError:
        return default


def trip_tier():
    """The host-RAM tier that trips the guard (a ``PressureTier`` name)."""
    from backend.core.memory_types import PressureTier
    return _tier_named(os.environ.get("JARVIS_MEMORY_GUARD_TRIP_TIER", ""), PressureTier.CRITICAL)


def commit_trip_level():
    """The host-commit level that trips the guard (a gate ``PressureLevel``)."""
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    raw = (os.environ.get("JARVIS_MEMORY_GUARD_COMMIT_TRIP_LEVEL", "") or "").strip().lower()
    try:
        return PressureLevel(raw)
    except ValueError:
        return PressureLevel.CRITICAL


def dwell_s(policy) -> float:
    """How long a breach must hold: the policy's own dwell unless overridden."""
    try:
        raw = float(os.environ.get("JARVIS_MEMORY_GUARD_DWELL_S", "") or policy.min_dwell_seconds)
    except ValueError:
        raw = policy.min_dwell_seconds
    return raw if raw > 0 else policy.min_dwell_seconds


# ---------------------------------------------------------------------------
# One reading
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    at: str
    mono: float
    host_total_gb: float
    host_used_pct: float
    tier: str
    policy: str
    trip_enter_pct: float
    trip_exit_pct: float
    commit_free_pct: Optional[float]
    commit_used_gb: Optional[float]
    commit_limit_gb: Optional[float]
    commit_level: Optional[str]
    kernel_pid: Optional[int]
    kernel_procs: int = 0
    kernel_rss_mb: float = 0.0
    kernel_private_mb: float = 0.0
    siblings_private_mb: float = 0.0
    tree_warn_mb: Optional[float] = None
    tree_cap_mb: Optional[float] = None
    growth: Optional[Dict[str, Any]] = None
    kind: str = "tick"


def _tree_memory(root_pid: int, exclude: Set[int]) -> Tuple[int, float, float]:
    """(processes, working set MB, private MB) of *root_pid* and its
    descendants, minus *exclude* (serving siblings a kernel may have spawned).
    On Windows ``vms`` is the process's private commit."""
    import psutil
    try:
        root = psutil.Process(root_pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0, 0.0, 0.0
    count, rss, private = 0, 0.0, 0.0
    for proc in procs:
        if proc.pid in exclude:
            continue
        try:
            info = proc.memory_info()
        except psutil.Error:
            continue
        count += 1
        rss += info.rss / _MB
        private += getattr(info, "private", info.vms) / _MB
    return count, rss, private


def _pids_private_mb(pids: Set[int]) -> float:
    import psutil
    total = 0.0
    for pid in pids:
        try:
            info = psutil.Process(pid).memory_info()
            total += getattr(info, "private", info.vms) / _MB
        except psutil.Error:
            continue
    return total


def top_consumers(n: int) -> List[Dict[str, Any]]:
    """The *n* processes holding the most private memory -- read once, at a
    breach, never per tick (a full table walk costs seconds on Windows)."""
    import psutil
    rows = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            info = proc.memory_info()
        except psutil.Error:
            continue
        rows.append({"pid": proc.info["pid"], "name": proc.info["name"],
                     "private_mb": round(getattr(info, "private", info.vms) / _MB),
                     "rss_mb": round(info.rss / _MB)})
    rows.sort(key=lambda r: r["private_mb"], reverse=True)
    return rows[:n]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@dataclass
class _Breach:
    since: float
    reason: str


@dataclass
class GuardState:
    tier: Any = None
    ram_breach: Optional[_Breach] = None
    commit_breach: Optional[_Breach] = None
    growth_reported: bool = False
    unattributed_reported: bool = False
    tree_warned: bool = False
    tree_breach: Optional[_Breach] = None
    samples: Deque[Tuple[float, float]] = field(default_factory=deque)
    kernel_pid: Optional[int] = None


class MemoryGuard:
    """Out-of-process memory guard. Everything it reads or does is injectable
    so the verdicts are testable without a kernel or a full machine."""

    def __init__(
        self,
        journal: Path,
        *,
        pid: Optional[int] = None,
        holder: Optional[Callable[[], Optional[int]]] = None,
        stop_kernel: Optional[Callable[[int], "asyncio.Future"]] = None,
        siblings: Optional[Callable[[], Set[int]]] = None,
    ) -> None:
        from backend.core.daemon_handover import kernel_holder
        self.journal = journal
        self._holder = holder or (kernel_holder if pid is None else self._pinned_holder(pid))
        self._stop_kernel = stop_kernel or self._hand_over
        self._siblings_fn = siblings or self._serving_siblings
        self._siblings: Set[int] = set()
        self._siblings_at = float("-inf")
        self.state = GuardState()
        self.stops = 0
        # The guard is the kernel's child when the kernel armed it (with a
        # venv launcher between them): its own memory is not the kernel's.
        self._own_lineage: Set[int] = {os.getpid()}
        try:
            import psutil
            self._own_lineage.update(p.pid for p in psutil.Process().parents())
        except Exception:  # noqa: BLE001
            pass
        journal.parent.mkdir(parents=True, exist_ok=True)

    # -- collaborators ------------------------------------------------------

    @staticmethod
    def _pinned_holder(pid: int) -> Callable[[], Optional[int]]:
        """The holder for a guard pinned to one process (``--pid``, the kernel
        arming its own guard): that process while it is still the one first
        seen -- PID and start time, as a lock record is judged
        (process_identity.lock_holder_alive) -- then None for good. A pinned
        guard used to report its kernel forever, so ``--once`` never ended
        after the kernel died (2026-10-15)."""
        from backend.core.process_identity import lock_holder_alive, process_started_at

        record = {"pid": pid, "started_at": process_started_at(pid)}

        def holder() -> Optional[int]:
            if record["pid"] is not None and not lock_holder_alive(record):
                record["pid"] = None  # gone; a recycled PID is someone else
            return record["pid"]
        return holder

    @staticmethod
    def _serving_siblings() -> Set[int]:
        from backend.core.ouroboros.governance.trinity_siblings import serving_sibling_pids
        return serving_sibling_pids()

    @staticmethod
    async def _hand_over(pid: int) -> bool:
        from backend.core.daemon_handover import KERNEL_SOCKET_PATH, hand_over
        from backend.core.supervisor_ipc import SOCKET_PATH
        return await hand_over(pid, KERNEL_SOCKET_PATH, SOCKET_PATH,
                               say=lambda line: logger.warning("[MemoryGuard] %s", line))

    def _write(self, record: Dict[str, Any]) -> None:
        try:
            with self.journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            logger.warning("[MemoryGuard] journal write failed: %s", exc)

    def event(self, kind: str, **fields: Any) -> None:
        self._write({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields})

    # -- sampling (blocking: runs in a worker thread) -----------------------

    def sample(self) -> Reading:
        import psutil
        from backend.core.memory_types import host_pressure_policy
        from backend.core.ouroboros.governance import host_commit_probe
        from backend.core.ouroboros.governance.memory_pressure_gate import get_default_gate

        vm = psutil.virtual_memory()
        total_gb = vm.total / (1024.0 ** 3)
        policy = host_pressure_policy(total_gb)
        self._policy = policy
        tier = policy.classify(vm.percent, self.state.tier)
        self.state.tier = tier
        trip = trip_tier()

        commit = host_commit_probe.read_host_commit()
        commit_level = (get_default_gate().level_for_free_pct(commit.free_pct)
                        if commit is not None else None)

        now = time.monotonic()
        if now - self._siblings_at >= policy.cooldown_seconds:
            try:
                self._siblings = set(self._siblings_fn())
            except Exception:  # noqa: BLE001 -- unknown siblings: none excluded
                self._siblings = set()
            self._siblings_at = now

        pid = self._holder()
        reading = Reading(
            at=datetime.now().isoformat(timespec="seconds"), mono=now,
            host_total_gb=round(total_gb, 2), host_used_pct=round(vm.percent, 1),
            tier=tier.name, policy=policy.version,
            trip_enter_pct=round(policy.enter_thresholds[trip], 1),
            trip_exit_pct=round(policy.exit_thresholds[trip], 1),
            commit_free_pct=None if commit is None else round(commit.free_pct, 1),
            commit_used_gb=None if commit is None else round((commit.limit_kb - commit.free_kb) / _MB, 2),
            commit_limit_gb=None if commit is None else round(commit.limit_kb / _MB, 2),
            commit_level=None if commit_level is None else commit_level.value,
            kernel_pid=pid,
            siblings_private_mb=round(_pids_private_mb(self._siblings)),
        )
        from backend.core.ouroboros.governance.process_memory_budget import (
            resolve_process_memory_thresholds,
        )
        warn_mb, cap_mb, _ = resolve_process_memory_thresholds(total_mb=vm.total / _MB)
        reading.tree_warn_mb = round(warn_mb) if cap_mb is not None else None
        reading.tree_cap_mb = None if cap_mb is None else round(cap_mb)
        if pid is not None:
            count, rss, private = _tree_memory(pid, self._siblings | (self._own_lineage - {pid}))
            reading.kernel_procs = count
            reading.kernel_rss_mb = round(rss)
            reading.kernel_private_mb = round(private)
        return reading

    # -- judgement (pure on the reading + state) ----------------------------

    def _track_growth(self, reading: Reading) -> None:
        from backend.core.ouroboros.governance import memory_growth_tracer as mgt
        samples = self.state.samples
        if reading.kernel_pid != self.state.kernel_pid:
            samples.clear()
            self.state.growth_reported = False
        if reading.kernel_pid is None:
            return
        window = mgt.window_s()
        samples.append((reading.mono, float(reading.kernel_private_mb)))
        while samples and reading.mono - samples[0][0] > window:
            samples.popleft()
        fit = mgt.fit_growth(list(samples))
        if fit is None:
            return
        growing, why = mgt.growth_verdict(fit, window=window)
        reading.growth = {"slope_mb_per_h": round(fit.slope_mb_per_h, 1), "r2": round(fit.r2, 3),
                          "span_s": round(fit.span_s), "growing": growing, "why": why}
        if growing and not self.state.growth_reported:
            self.state.growth_reported = True
            logger.warning("[MemoryGuard] kernel %s grows %.0f MB/h (r2 %.2f over %.0f s)",
                           reading.kernel_pid, fit.slope_mb_per_h, fit.r2, fit.span_s)
            self.event("kernel_growth", pid=reading.kernel_pid, **reading.growth)

    def _dwelled(self, slot: str, breached: bool, reason: str, now: float, dwell: float) -> Optional[str]:
        current: Optional[_Breach] = getattr(self.state, slot)
        if not breached:
            setattr(self.state, slot, None)
            return None
        if current is None:
            setattr(self.state, slot, _Breach(now, reason))
            return None
        return reason if now - current.since >= dwell else None

    def judge(self, reading: Reading) -> Optional[Dict[str, Any]]:
        """The breach to act on, or None. Records attribution either way."""
        from backend.core.memory_types import PressureTier
        from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

        policy = self._policy
        dwell = dwell_s(policy)
        trip = trip_tier()
        ram_breached = PressureTier[reading.tier] >= trip
        ram = self._dwelled("ram_breach", ram_breached,
                            f"host RAM {reading.host_used_pct:.1f}% in {reading.tier} "
                            f"(trip {trip.name} at {reading.trip_enter_pct:.1f}%)",
                            reading.mono, dwell)
        rank = [PressureLevel.OK, PressureLevel.WARN, PressureLevel.HIGH, PressureLevel.CRITICAL]
        commit_breached = (reading.commit_level is not None
                           and rank.index(PressureLevel(reading.commit_level)) >= rank.index(commit_trip_level()))
        commit = self._dwelled("commit_breach", commit_breached,
                               f"host commit {reading.commit_free_pct}% free ({reading.commit_level})",
                               reading.mono, dwell)
        tree_over = (reading.tree_cap_mb is not None
                     and reading.kernel_private_mb >= reading.tree_cap_mb)
        tree = self._dwelled("tree_breach", tree_over,
                             f"kernel tree {reading.kernel_private_mb:.0f} MB over its "
                             f"budget cap {reading.tree_cap_mb} MB", reading.mono, dwell)
        reason = tree or ram or commit
        if reason is None or reading.kernel_pid is None:
            return None
        if tree:
            # Over its own budget: the kernel IS the cause, by definition.
            return {"reason": reason, "resolves": True,
                    "kernel_private_mb": reading.kernel_private_mb,
                    "host_pct_without_kernel": None}

        # Would stopping the kernel resolve it? Its private memory, taken off
        # the reading, against the trip tier's EXIT threshold (RAM) / the trip
        # level's free threshold (commit).
        kernel_gb = reading.kernel_private_mb / 1024.0
        relieved_ram = reading.host_used_pct - 100.0 * kernel_gb / max(reading.host_total_gb, 1e-9)
        resolves = True
        if ram:
            resolves = relieved_ram < reading.trip_exit_pct
        if commit and reading.commit_limit_gb:
            from backend.core.ouroboros.governance.memory_pressure_gate import get_default_gate
            free_after = 100.0 - 100.0 * max(0.0, reading.commit_used_gb - kernel_gb) / reading.commit_limit_gb
            resolves = resolves and rank.index(get_default_gate().level_for_free_pct(free_after)) < rank.index(commit_trip_level())
        return {"reason": reason, "resolves": resolves,
                "kernel_private_mb": reading.kernel_private_mb,
                "host_pct_without_kernel": round(relieved_ram, 1)}

    # -- the loop -----------------------------------------------------------

    def interval_s(self, reading: Optional[Reading]) -> float:
        """The commit sampler's adaptive cadence, never slower than half the
        dwell: a breach is seen at least twice before it is acted on."""
        from backend.core.ouroboros.governance import host_commit_probe
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            critical_threshold_pct, warn_threshold_pct,
        )
        sampler = host_commit_probe.HostCommitSampler(warn_pct=warn_threshold_pct,
                                                     critical_pct=critical_threshold_pct)
        base = sampler.interval_for(None if reading is None else reading.commit_free_pct)
        policy = getattr(self, "_policy", None)
        return base if policy is None else min(base, dwell_s(policy) / 2.0)

    async def tick(self) -> Optional[Reading]:
        try:
            reading = await asyncio.to_thread(self.sample)
        except Exception:  # noqa: BLE001 -- a failed read is skipped, never fatal
            logger.debug("[MemoryGuard] sample failed", exc_info=True)
            return None
        if reading.kernel_pid != self.state.kernel_pid:
            if self.state.kernel_pid is not None:
                self.event("kernel_gone", pid=self.state.kernel_pid)
                logger.info("[MemoryGuard] kernel %s is gone", self.state.kernel_pid)
            if reading.kernel_pid is not None:
                self.event("kernel_attached", pid=reading.kernel_pid)
                logger.info("[MemoryGuard] watching kernel %s (%s, trip %s at %.1f%%)",
                            reading.kernel_pid, reading.policy, trip_tier().name,
                            reading.trip_enter_pct)
        self._track_growth(reading)
        self.state.kernel_pid = reading.kernel_pid
        if reading.tree_warn_mb is not None and reading.kernel_pid is not None:
            over = reading.kernel_private_mb >= reading.tree_warn_mb
            if over and not self.state.tree_warned:
                logger.warning("[MemoryGuard] kernel %s tree at %d MB, past its budget warn line "
                               "%d MB (cap %s MB)", reading.kernel_pid, reading.kernel_private_mb,
                               reading.tree_warn_mb, reading.tree_cap_mb)
                self.event("tree_warn", pid=reading.kernel_pid, private_mb=reading.kernel_private_mb,
                           warn_mb=reading.tree_warn_mb, cap_mb=reading.tree_cap_mb)
            self.state.tree_warned = over
        self._write(asdict(reading))

        breach = self.judge(reading)
        if breach is None:
            self.state.unattributed_reported = False
            return reading
        if not breach["resolves"] and self.state.unattributed_reported:
            return reading  # this episode is already journalled
        from backend.core.ouroboros.governance import memory_growth_tracer as mgt
        top = await asyncio.to_thread(top_consumers, mgt.top_n())
        if not breach["resolves"]:
            # Journalled once per breach episode (until the breach clears).
            self.state.unattributed_reported = True
            self.event("breach_not_kernel", pid=reading.kernel_pid, top=top, **breach)
            logger.warning("[MemoryGuard] %s -- not the kernel's doing (it holds %d MB); "
                           "leaving it running. Top: %s", breach["reason"],
                           breach["kernel_private_mb"],
                           ", ".join(f"{t['name']} {t['private_mb']} MB" for t in top[:3]))
            return reading
        logger.error("[MemoryGuard] %s held for %.0fs -- stopping kernel %s (%d MB private)",
                     breach["reason"], dwell_s(self._policy), reading.kernel_pid,
                     breach["kernel_private_mb"])
        self.event("stopping_kernel", pid=reading.kernel_pid, top=top, **breach)
        stopped = False
        try:
            stopped = bool(await self._stop_kernel(reading.kernel_pid))
        except Exception as exc:  # noqa: BLE001
            logger.error("[MemoryGuard] stopping kernel %s failed: %s", reading.kernel_pid, exc)
        self.stops += 1
        self.event("kernel_stopped" if stopped else "kernel_stop_failed", pid=reading.kernel_pid)
        self.state.ram_breach = self.state.commit_breach = self.state.tree_breach = None
        return reading

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        self.event("guard_started", pid=os.getpid(), trip_tier=trip_tier().name,
                   commit_trip_level=commit_trip_level().value)
        seen_kernel = False
        reading: Optional[Reading] = None
        while not stop.is_set():
            reading = await self.tick() or reading
            if reading is not None and reading.kernel_pid is not None:
                seen_kernel = True
            if once and seen_kernel and (reading is None or reading.kernel_pid is None):
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s(reading))
            except asyncio.TimeoutError:
                pass
        self.event("guard_stopped", stops=self.stops)


# ---------------------------------------------------------------------------
# Reading a journal back
# ---------------------------------------------------------------------------

def report(journal: Path) -> Dict[str, Any]:
    """Peaks, the kernel's growth fit and every event, per kernel."""
    from backend.core.ouroboros.governance import memory_growth_tracer as mgt
    ticks: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        (ticks if row.get("kind") == "tick" else events).append(row)
    kernels: Dict[str, Any] = {}
    for pid in sorted({t["kernel_pid"] for t in ticks if t.get("kernel_pid")}):
        rows = [t for t in ticks if t.get("kernel_pid") == pid]
        fit = mgt.fit_growth([(t["mono"], float(t["kernel_private_mb"])) for t in rows])
        kernels[str(pid)] = {
            "ticks": len(rows), "first": rows[0]["at"], "last": rows[-1]["at"],
            "private_mb_first": rows[0]["kernel_private_mb"],
            "private_mb_peak": max(t["kernel_private_mb"] for t in rows),
            "private_mb_last": rows[-1]["kernel_private_mb"],
            "host_pct_peak": max(t["host_used_pct"] for t in rows),
            "slope_mb_per_h": None if fit is None else round(fit.slope_mb_per_h, 1),
            "r2": None if fit is None else round(fit.r2, 3),
        }
    return {"journal": str(journal), "ticks": len(ticks),
            "host_pct_peak": max((t["host_used_pct"] for t in ticks), default=None),
            "kernels": kernels, "events": events}


def guard_enabled() -> bool:
    return (os.environ.get("JARVIS_MEMORY_GUARD_ENABLED", "true") or "").strip().lower()         not in ("0", "false", "no", "off")


_armed: Dict[int, Any] = {}


def arm(kernel_pid: int) -> Optional[Any]:
    """Start the guard for *kernel_pid* as its own detached process (its own
    process group: a Ctrl+C in the kernel's console is not the guard's), with
    ``--once`` so it leaves with that kernel. Idempotent per kernel; the Popen,
    or None when disabled or it could not start. NEVER raises into boot."""
    if not guard_enabled():
        return None
    existing = _armed.get(kernel_pid)
    if existing is not None and existing.poll() is None:
        return existing
    try:
        import subprocess
        from backend.core.daemon_handover import REPO_ROOT
        from backend.core.host_platform import log_dir
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        from backend.core.process_identity import spawn_detached
        out = (Path(log_dir()) / f"memory-guard-{stamp}.log").open("ab")
        proc = spawn_detached(
            [sys.executable, "-m", "backend.core.memory_guard", "watch",
             "--pid", str(kernel_pid), "--once",
             "--journal", str(Path(log_dir()) / f"memory-guard-{stamp}.jsonl")],
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT,
        )
        out.close()  # the child holds its own handle
        _armed[kernel_pid] = proc
        return proc
    except Exception as exc:  # noqa: BLE001 -- the guard is protection, never a boot blocker
        logger.warning("[MemoryGuard] could not arm for kernel %s: %s", kernel_pid, exc)
        return None


def default_journal() -> Path:
    from backend.core.host_platform import log_dir
    return Path(log_dir()) / f"memory-guard-{datetime.now():%Y%m%d-%H%M%S}.jsonl"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_guard", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd")
    watch = sub.add_parser("watch", help="guard the kernel (default)")
    watch.add_argument("--pid", type=int, help="guard this process tree instead of the kernel.lock holder")
    watch.add_argument("--once", action="store_true", help="exit when the guarded kernel exits")
    watch.add_argument("--journal", type=Path, help="JSONL journal path (default: log dir)")
    rep = sub.add_parser("report", help="summarise a journal")
    rep.add_argument("journal", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    from backend.core.env_bootstrap import load_env_once
    load_env_once()  # the policy's view of J-Prime comes from the same .env the kernel reads

    if args.cmd == "report":
        print(json.dumps(report(args.journal), indent=2, default=str))
        return 0

    journal = getattr(args, "journal", None) or default_journal()
    guard = MemoryGuard(journal, pid=getattr(args, "pid", None))
    logger.info("[MemoryGuard] journal %s", journal)

    async def _run() -> None:
        stop = asyncio.Event()
        try:
            await guard.run(stop, once=getattr(args, "once", False))
        finally:
            stop.set()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        guard.event("guard_stopped", stops=guard.stops, by="interrupt")
    return 0


if __name__ == "__main__":
    # Run as a module from the repo root: python -m backend.core.memory_guard
    sys.exit(main())
