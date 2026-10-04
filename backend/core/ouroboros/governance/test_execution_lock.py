"""Resource-aware test execution lock: admission + kernel-enforced containment.

Why this exists
---------------
2026-10-04: a full ``pytest tests/governance`` run inside WSL, started beside
soak ``bt-2026-10-04-204411``, grew one python to 14+ GB. The guest has a
fixed ceiling (``.wslconfig`` memory=40GB) and the soak already held ~16 GB,
so the kernel OOM killer fired in ``init.scope`` -- the cgroup every WSL
session lands in. ``init.scope`` carries ``OOMPolicy=stop``: systemd stopped
it, which SIGKILLed the soak, and because the OOM record lives in the shared
VM kernel log it was replayed on every later distro start, killing each new
session ~90 s in until ``wsl --shutdown``.

Two layers, because either alone is insufficient
------------------------------------------------
1. **Admission** (this module's :func:`compute_grant` + :func:`predict_need`).
   Headroom is not "free memory". A live soak is licensed to grow to the cap
   its own ``ProcessMemoryWatchdog`` enforces (``process_memory_budget``), so
   the unused part of that licence is RESERVED, as is the unused part of
   every other governed test run's grant. What remains, minus a safety margin
   for the kernel and page cache, is the grant. A run whose grant is below
   the floor, or below the peak this machine has measured for runs of the
   same scale (the ledger), is refused with :class:`ResourceExhaustion`
   before a single test executes.
2. **Containment** (:func:`adopt_into_scope`). Admission is a prediction; a
   test can still balloon past it. The admitted pytest process moves ITSELF
   into a transient systemd scope with ``MemoryMax`` = the grant and no swap.
   The kernel then cannot let the test tree exceed the grant: an overrun is
   OOM-killed INSIDE that scope (``OOMPolicy=continue`` kills only the
   offender, so pytest survives to report), never in ``init.scope``, and the
   scope's OOM record names a unit that is garbage-collected, so it cannot be
   replayed against anything that lives on.

Exempt by construction: the soak's own VALIDATE runs (already inside the
soak's budget -- detected by a live session heartbeat in their inherited
environment), pytest-xdist workers and nested pytest runs (they inherit the
parent's capped cgroup), and non-Linux hosts.

Leaf discipline: stdlib at import time; repo helpers are imported lazily.
Every probe NEVER raises; the decision functions are pure and take their
inputs explicitly so they are testable without a live soak.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_RESOURCE_EXHAUSTION",
    "Grant",
    "NeedEstimate",
    "ResourceExhaustion",
    "RunRecord",
    "SoakTenant",
    "adopt_into_scope",
    "admission_section",
    "cgroup_headroom_bytes",
    "compute_grant",
    "exemption_reason",
    "find_soak_pids",
    "lock_enabled",
    "own_cgroup",
    "predict_need",
    "read_ledger",
    "record_run",
    "scope_stats",
    "sibling_scope_reserve",
    "soak_tenants",
]

#: sysexits ``EX_TEMPFAIL``: the run is refused NOW, not broken; retry later.
EXIT_RESOURCE_EXHAUSTION = 75

ENV_ENABLED = "JARVIS_TEST_EXEC_LOCK_ENABLED"
ENV_SAFETY_FRACTION = "JARVIS_TEST_EXEC_SAFETY_FRACTION"
ENV_FLOOR_FRACTION = "JARVIS_TEST_EXEC_FLOOR_FRACTION"
ENV_SCALE_BAND = "JARVIS_TEST_EXEC_SCALE_BAND"
ENV_TRUNCATION_MARGIN = "JARVIS_TEST_EXEC_TRUNCATION_MARGIN"
ENV_LEDGER = "JARVIS_TEST_EXEC_LEDGER"
ENV_SCOPE_TIMEOUT = "JARVIS_TEST_EXEC_SCOPE_TIMEOUT_S"

#: Transient scope names this module creates; also how sibling grants are found.
SCOPE_PREFIX = "ov-pytest-"
#: The soak daemon's entry script (``scripts/ouroboros_battle_test.py``). Its
#: supervisor embeds the same argv, so only argv[1] -- the program python
#: actually runs -- identifies a soak.
SOAK_ENTRYPOINT = "ouroboros_battle_test.py"

_PROC = Path("/proc")
_CGROUP = Path("/sys/fs/cgroup")


class ResourceExhaustion(RuntimeError):
    """Raised (and surfaced as exit 75) when a run cannot be admitted safely."""

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _env_fraction(name: str, default: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def lock_enabled() -> bool:
    return os.environ.get(ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def _ledger_path() -> Path:
    raw = os.environ.get(ENV_LEDGER, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".jarvis" / "test_execution_ledger.jsonl"


# ---------------------------------------------------------------------------
# Probes (NEVER raise)
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_int(path: Path) -> Optional[int]:
    raw = (_read_text(path) or "").strip()
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cmdline(pid: int, proc_root: Path) -> List[str]:
    raw = _read_text(proc_root / str(pid) / "cmdline") or ""
    return [part for part in raw.split("\0") if part]


def _environ(pid: int, proc_root: Path) -> Dict[str, str]:
    raw = _read_text(proc_root / str(pid) / "environ") or ""
    env: Dict[str, str] = {}
    for part in raw.split("\0"):
        key, sep, value = part.partition("=")
        if sep:
            env[key] = value
    return env


def find_soak_pids(proc_root: Path = _PROC) -> List[int]:
    """Every live soak daemon on this machine, whatever checkout it runs from.

    Machine-wide on purpose: worktrees share one guest's memory, so a lock
    keyed to a project root (``singleton_lock``) would miss a soak running
    from a sibling checkout -- which is exactly how the 2026-10-04 OOM began.
    """
    pids: List[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return pids
    me = os.getpid()
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        argv = _cmdline(int(entry.name), proc_root)
        if len(argv) >= 2 and os.path.basename(argv[1]) == SOAK_ENTRYPOINT:
            pids.append(int(entry.name))
    return sorted(pids)


@dataclass(frozen=True)
class SoakTenant:
    """A live soak and the memory it is licensed to grow into."""

    pid: int
    cap_bytes: Optional[int]
    tree_bytes: Optional[int]

    @property
    def reserve_bytes(self) -> Optional[int]:
        """Unused licence: cap minus current tree. ``None`` = unbounded."""
        if self.cap_bytes is None:
            return None
        return max(0, self.cap_bytes - (self.tree_bytes or 0))


def soak_tenants(
    *,
    total_bytes: int,
    proc_root: Path = _PROC,
    tree_probe: Optional[Callable[[int], Optional[float]]] = None,
) -> List[SoakTenant]:
    """Size every live soak with ITS OWN cap formula and environment."""
    from backend.core.ouroboros.governance.process_memory_budget import (  # noqa: PLC0415
        resolve_process_memory_thresholds,
    )
    if tree_probe is None:
        from backend.core.ouroboros.governance.process_tree_probe import (  # noqa: PLC0415
            probe_process_tree_memory_mb as tree_probe,
        )
    tenants: List[SoakTenant] = []
    for pid in find_soak_pids(proc_root):
        _warn, cap_mb, _interval = resolve_process_memory_thresholds(
            _environ(pid, proc_root), total_mb=total_bytes / (1024.0 * 1024.0),
        )
        try:
            tree_mb = tree_probe(pid)
        except Exception:  # noqa: BLE001 -- probe contract: never raise
            tree_mb = None
        tenants.append(SoakTenant(
            pid=pid,
            cap_bytes=None if cap_mb is None else int(cap_mb * 1024 * 1024),
            tree_bytes=None if tree_mb is None else int(tree_mb * 1024 * 1024),
        ))
    return tenants


def own_cgroup(proc_root: Path = _PROC) -> Optional[str]:
    """The unified-hierarchy (v2) path of this process, e.g. ``/init.scope``."""
    for line in (_read_text(proc_root / "self" / "cgroup") or "").splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return None


def cgroup_headroom_bytes(
    cgroup_path: Optional[str], cgroup_root: Path = _CGROUP,
) -> Optional[int]:
    """Tightest ``memory.max - memory.current`` on the path to the root.

    ``None`` when no ancestor carries a finite limit (``max`` everywhere),
    which is the WSL default -- the guest's MemAvailable is then the bound.
    """
    if not cgroup_path:
        return None
    tightest: Optional[int] = None
    node = cgroup_root / cgroup_path.strip("/")
    while True:
        limit = _read_int(node / "memory.max")
        if limit is not None:
            used = _read_int(node / "memory.current") or 0
            room = max(0, limit - used)
            tightest = room if tightest is None else min(tightest, room)
        if node == cgroup_root or cgroup_root not in node.parents:
            break
        node = node.parent
    return tightest


def _scope_dirs(cgroup_root: Path) -> List[Path]:
    try:
        return sorted(cgroup_root.glob(f"user.slice/user-*.slice/user@*.service/app.slice/{SCOPE_PREFIX}*.scope"))
    except OSError:
        return []


def sibling_scope_reserve(
    cgroup_root: Path = _CGROUP, *, exclude: Optional[str] = None,
) -> int:
    """Unused grant of every OTHER live governed test run (max - current)."""
    reserve = 0
    for scope in _scope_dirs(cgroup_root):
        if exclude and scope.name == exclude:
            continue
        limit = _read_int(scope / "memory.max")
        if limit is None:
            continue
        reserve += max(0, limit - (_read_int(scope / "memory.current") or 0))
    return reserve


def exemption_reason(
    environ: Mapping[str, str] = os.environ,
    *,
    proc_root: Path = _PROC,
) -> Optional[str]:
    """Why this pytest process must NOT be gated, or ``None`` to gate it."""
    if not sys.platform.startswith("linux"):
        return "non_linux_host"
    if not lock_enabled():
        return "disabled"
    if environ.get("PYTEST_XDIST_WORKER"):
        return "xdist_worker_inherits_parent_scope"
    own = own_cgroup(proc_root) or ""
    if f"/{SCOPE_PREFIX}" in own:
        return "already_inside_governed_scope"
    session_dir = (environ.get("JARVIS_OUROBOROS_SESSION_DIR") or "").strip()
    if session_dir:
        from backend.core.ouroboros.battle_test.terminal_supervisor import (  # noqa: PLC0415
            last_heartbeat, stale_after_s,
        )
        beat = last_heartbeat(Path(session_dir), None)
        if beat > 0 and time.time() - beat < stale_after_s():
            return "soak_tenant_inside_soak_budget"
    return None


# ---------------------------------------------------------------------------
# Decisions (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    granted_bytes: int
    floor_bytes: int
    available_bytes: int
    cgroup_headroom_bytes: Optional[int]
    tenant_reserve_bytes: int
    sibling_reserve_bytes: int
    safety_bytes: int
    tenants: Tuple[SoakTenant, ...] = ()
    refusal: str = ""

    @property
    def admitted(self) -> bool:
        return not self.refusal

    def describe(self) -> str:
        gib = 1024 ** 3
        soaks = ",".join(str(t.pid) for t in self.tenants) or "none"
        return (
            f"grant={self.granted_bytes / gib:.1f}GiB floor={self.floor_bytes / gib:.1f}GiB "
            f"available={self.available_bytes / gib:.1f}GiB "
            f"soak_reserve={self.tenant_reserve_bytes / gib:.1f}GiB "
            f"sibling_reserve={self.sibling_reserve_bytes / gib:.1f}GiB "
            f"safety={self.safety_bytes / gib:.1f}GiB soaks={soaks}"
            + (f" refusal={self.refusal}" if self.refusal else "")
        )


def compute_grant(
    *,
    total_bytes: int,
    available_bytes: int,
    cgroup_headroom: Optional[int],
    tenants: Sequence[SoakTenant],
    sibling_reserve: int,
) -> Grant:
    """Memory a new test run may use without encroaching on anyone's licence."""
    safety = int(total_bytes * _env_fraction(ENV_SAFETY_FRACTION, 0.05))
    floor = int(total_bytes * _env_fraction(ENV_FLOOR_FRACTION, 0.05))
    unbounded = [t.pid for t in tenants if t.reserve_bytes is None]
    tenant_reserve = sum(t.reserve_bytes or 0 for t in tenants)
    base = available_bytes if cgroup_headroom is None else min(available_bytes, cgroup_headroom)
    granted = max(0, base - tenant_reserve - sibling_reserve - safety)
    if available_bytes <= 0 or total_bytes <= 0:
        refusal = "memory_unprobeable"
    elif unbounded:
        # A soak whose cap cannot be derived could take everything; no grant
        # beside it is provably safe.
        refusal = f"soak_cap_unbounded:{','.join(map(str, unbounded))}"
    elif granted < floor:
        refusal = "insufficient_headroom"
    else:
        refusal = ""
    return Grant(
        granted_bytes=granted if not refusal else 0,
        floor_bytes=floor,
        available_bytes=available_bytes,
        cgroup_headroom_bytes=cgroup_headroom,
        tenant_reserve_bytes=tenant_reserve,
        sibling_reserve_bytes=sibling_reserve,
        safety_bytes=safety,
        tenants=tuple(tenants),
        refusal=refusal,
    )


@dataclass(frozen=True)
class RunRecord:
    ts: float
    items: int
    peak_bytes: int
    granted_bytes: int
    oom_kills: int
    exitstatus: Optional[int] = None

    @property
    def truncated(self) -> bool:
        """The run hit its ceiling: ``peak`` is a LOWER bound on its need."""
        return self.oom_kills > 0


@dataclass(frozen=True)
class NeedEstimate:
    need_bytes: int
    basis: str
    samples: int = 0


def predict_need(
    items: int, records: Iterable[RunRecord], *, total_bytes: int,
) -> NeedEstimate:
    """Peak memory to expect for *items* tests, learned from this machine.

    Compares against runs of the same SCALE (within ``JARVIS_TEST_EXEC_SCALE_BAND``
    of the item count, default 2x either way) and takes their worst peak. A
    run that was OOM-killed at its ceiling only proves its need was HIGHER,
    so its peak is inflated by ``JARVIS_TEST_EXEC_TRUNCATION_MARGIN``. With no
    comparable history the floor applies -- the cgroup ceiling still holds.
    """
    floor = int(total_bytes * _env_fraction(ENV_FLOOR_FRACTION, 0.05))
    band = _env_fraction(ENV_SCALE_BAND, 2.0, lo=1.0, hi=1000.0)
    margin = _env_fraction(ENV_TRUNCATION_MARGIN, 0.25, lo=0.0, hi=10.0)
    similar = [
        r for r in records
        if r.items > 0 and items > 0 and 1.0 / band <= items / r.items <= band
    ]
    if not similar:
        return NeedEstimate(need_bytes=floor, basis="floor_no_comparable_history")
    worst = max(
        int(r.peak_bytes * (1.0 + margin)) if r.truncated else r.peak_bytes
        for r in similar
    )
    return NeedEstimate(
        need_bytes=max(floor, worst),
        basis="ledger_same_scale_worst_peak",
        samples=len(similar),
    )


# ---------------------------------------------------------------------------
# Ledger + serialization
# ---------------------------------------------------------------------------


def read_ledger(path: Optional[Path] = None) -> List[RunRecord]:
    records: List[RunRecord] = []
    for line in (_read_text(path or _ledger_path()) or "").splitlines():
        try:
            row = json.loads(line)
            records.append(RunRecord(
                ts=float(row["ts"]), items=int(row["items"]),
                peak_bytes=int(row["peak_bytes"]), granted_bytes=int(row["granted_bytes"]),
                oom_kills=int(row.get("oom_kills", 0)), exitstatus=row.get("exitstatus"),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return records


def record_run(record: RunRecord, path: Optional[Path] = None) -> bool:
    from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: PLC0415
        flock_append_line,
    )
    target = path or _ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return bool(flock_append_line(target, json.dumps(asdict(record), sort_keys=True)))
    except Exception:  # noqa: BLE001 -- the ledger is advisory
        logger.debug("[TestExecLock] ledger append failed", exc_info=True)
        return False


def admission_section():
    """Serializes probe -> grant -> adopt across concurrent pytest launches.

    Without it two launches can read the same MemAvailable before either has
    a scope for the other to reserve, and both take the same memory.
    """
    from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: PLC0415
        flock_critical_section,
    )
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return flock_critical_section(path.with_name(path.name + ".admission"))


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def adopt_into_scope(
    memory_max_bytes: int,
    *,
    runner: Callable[..., Any] = subprocess.run,
    proc_root: Path = _PROC,
) -> Optional[str]:
    """Move THIS process into a fresh capped transient scope.

    Asks the user's systemd manager (``StartTransientUnit`` with ``PIDs`` =
    self) for ``MemoryMax`` = *memory_max_bytes*, ``MemorySwapMax`` = 0 (an
    overrun must be killed, not paged into the swap the soak relies on),
    ``OOMPolicy=continue`` (kill the offender, keep pytest alive to report)
    and ``CollectMode=inactive-or-failed`` (a failed scope is removed instead
    of degrading the user manager). Children inherit the cgroup.

    Returns the scope name once ``/proc/self/cgroup`` confirms the move, or
    ``None`` when the manager is unavailable. NEVER raises.
    """
    unit = f"{SCOPE_PREFIX}{os.getpid()}.scope"
    cmd = [
        "busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
        unit, "fail", "5",
        "PIDs", "au", "1", str(os.getpid()),
        "MemoryMax", "t", str(int(memory_max_bytes)),
        "MemorySwapMax", "t", "0",
        "OOMPolicy", "s", "continue",
        "CollectMode", "s", "inactive-or-failed",
        "0",
    ]
    timeout_s = _env_fraction(ENV_SCOPE_TIMEOUT, 10.0, lo=1.0, hi=120.0)
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=timeout_s)
    except Exception:  # noqa: BLE001 -- busctl absent / manager down
        logger.debug("[TestExecLock] StartTransientUnit unavailable", exc_info=True)
        return None
    if getattr(result, "returncode", 1) != 0:
        logger.debug("[TestExecLock] StartTransientUnit refused: %s", getattr(result, "stderr", ""))
        return None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if unit in (own_cgroup(proc_root) or ""):
            return unit
        time.sleep(0.05)
    return None


def scope_stats(cgroup_root: Path = _CGROUP, proc_root: Path = _PROC) -> Tuple[Optional[int], int]:
    """``(memory.peak, oom_kill count)`` of this process's own cgroup."""
    own = own_cgroup(proc_root)
    if not own:
        return (None, 0)
    node = cgroup_root / own.strip("/")
    oom_kills = 0
    for line in (_read_text(node / "memory.events") or "").splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            try:
                oom_kills = int(value)
            except ValueError:
                pass
    return (_read_int(node / "memory.peak"), oom_kills)
