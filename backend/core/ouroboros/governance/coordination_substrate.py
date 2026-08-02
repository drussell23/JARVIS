"""What mutual-exclusion guarantee can this path actually provide?

`cross_process_jsonl` is careful about POSIX vs Windows and says nothing about
the filesystem underneath — reasonably, because it was written for one host.
But ``fcntl.flock`` is a HOST-LOCAL primitive. Two machines sharing a checkout
over NFS each take the lock successfully, each believe they hold it, and both
proceed. The failure is silent on both sides: no error, no timeout, just two
processes in a critical section that promised to hold one.

The honest response is not to pretend a distributed lock exists. It is to
answer, per path, WHICH guarantee is available, and to say so when the answer
is "less than you think":

    CROSS_HOST   a distributed backend answered — real mutual exclusion
    HOST_LOCAL   flock on a filesystem we VERIFIED is local — correct here
    UNVERIFIED   flock, and we could not determine the filesystem
    UNSAFE       flock on a filesystem we VERIFIED is shared, with no
                 distributed backend — the guarantee does not hold

`UNVERIFIED` is deliberately distinct from `HOST_LOCAL`, and neither warns.
Most installs are a laptop, and a module that cried "possible split-brain!" on
every ordinary machine would train its operator to ignore the one time it
mattered. Silence and verified-safe are different facts, so they are different
values — the same rule that made `waived()` distinct from an omitted argument
and `UNKNOWN` distinct from `UNSET`.

REUSE
-------
Nothing here implements locking. `cross_process_jsonl` owns the file lock and
`core.distributed_lock_manager` (v3.0 — Redis backend, fencing tokens, lease
keepalive, "works across VMs, GCP instances, Docker") owns the distributed one.
This module only decides WHICH to ask, and reports what it asked.

Escalation is adaptive rather than configured: the distributed manager is used
when the substrate needs it and it is actually reachable. Imposing Redis on a
single laptop would buy nothing and add a dependency that can be down.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import enum
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple

logger = logging.getLogger("Ouroboros.CoordinationSubstrate")

COORDINATION_SCHEMA_VERSION: str = "coordination_substrate.v1"

#: Filesystems where a host-local lock says nothing about another host.
#: Extended (never replaced) by `JARVIS_COORDINATION_SHARED_FSTYPES` so a site
#: with an exotic clustered filesystem does not need a code change.
_SHARED_FSTYPES = frozenset({
    "nfs", "nfs3", "nfs4", "cifs", "smb", "smbfs", "smb2", "afs", "afpfs",
    "glusterfs", "ceph", "cephfs", "lustre", "gpfs", "ocfs2", "gfs2",
    "beegfs", "fuse.sshfs", "fuse.s3fs", "fuse.gcsfuse", "fuse.rclone",
    "davfs", "webdav", "9p", "virtiofs",
})


class Guarantee(str, enum.Enum):
    """What mutual exclusion is actually available for a path."""

    CROSS_HOST = "cross_host"
    HOST_LOCAL = "host_local"
    UNVERIFIED = "unverified"
    UNSAFE = "unsafe"

    @property
    def is_sufficient(self) -> bool:
        """True when the guarantee covers every writer that could exist.

        `UNVERIFIED` counts: it is the ordinary laptop case, where flock is
        correct and we simply could not read the mount table. Treating "did not
        check" as "unsafe" would make the honest signal worthless.
        """
        return self in (Guarantee.CROSS_HOST, Guarantee.HOST_LOCAL,
                        Guarantee.UNVERIFIED)


@dataclass(frozen=True)
class SubstrateReading:
    """What we learned about a path. Frozen — safe to share and to stamp."""

    guarantee: Guarantee
    fstype: str = ""
    mountpoint: str = ""
    detail: str = ""
    schema_version: str = COORDINATION_SCHEMA_VERSION

    def as_evidence(self) -> dict:
        """Splattable into telemetry — the provenance of a coordinated decision."""
        return {
            "coordination": self.guarantee.value,
            "coordination_fstype": self.fstype or "unknown",
            "coordination_verified": self.guarantee is not Guarantee.UNVERIFIED,
        }


def shared_fstypes() -> frozenset:
    """The shared-filesystem set, EXTENDED by env. NEVER raises."""
    try:
        raw = (os.environ.get("JARVIS_COORDINATION_SHARED_FSTYPES", "") or "").strip()
        extra = {p.strip().lower() for p in raw.split(",") if p.strip()}
        return frozenset(_SHARED_FSTYPES | extra)
    except Exception:  # noqa: BLE001
        return _SHARED_FSTYPES


def _mount_for(path: Path) -> Tuple[str, str]:
    """(mountpoint, fstype) for *path* — longest matching mount. NEVER raises.

    Walks up to an existing ancestor first: the journal may not exist yet, and
    a path that does not exist has no mount of its own.
    """
    try:
        target = path.resolve()
        while not target.exists() and target.parent != target:
            target = target.parent
        resolved = str(target)
        import psutil
        best = ("", "")
        for part in psutil.disk_partitions(all=True):
            mp = part.mountpoint
            if resolved == mp or resolved.startswith(
                    mp if mp.endswith(os.sep) else mp + os.sep):
                if len(mp) > len(best[0]):
                    best = (mp, (part.fstype or "").lower())
        return best
    except Exception:  # noqa: BLE001 — psutil missing, /proc unreadable, sandbox
        return ("", "")


def _distributed_reachable() -> bool:
    """Is a real cross-host backend answering right now? NEVER raises.

    Asks the existing manager rather than re-deriving Redis configuration:
    two opinions about whether the backend is up would be one opinion too many.
    """
    try:
        from backend.core.distributed_lock_manager import get_lock_manager
        import asyncio
        loop = asyncio.get_running_loop()  # noqa: F841 — presence check only
        mgr = getattr(get_lock_manager, "_cached_manager", None)
        if mgr is None:
            return False
        return bool(getattr(mgr, "_redis_available", False))
    except Exception:  # noqa: BLE001
        return False


def probe(path: Path) -> SubstrateReading:
    """What guarantee can *path* support? NEVER raises."""
    try:
        mountpoint, fstype = _mount_for(Path(path))
        if not fstype:
            return SubstrateReading(
                Guarantee.UNVERIFIED, mountpoint=mountpoint,
                detail="mount table unreadable")
        if fstype not in shared_fstypes():
            return SubstrateReading(Guarantee.HOST_LOCAL, fstype=fstype,
                                    mountpoint=mountpoint,
                                    detail="local filesystem")
        if _distributed_reachable():
            return SubstrateReading(Guarantee.CROSS_HOST, fstype=fstype,
                                    mountpoint=mountpoint,
                                    detail="shared filesystem, distributed lock")
        return SubstrateReading(
            Guarantee.UNSAFE, fstype=fstype, mountpoint=mountpoint,
            detail="shared filesystem, no distributed backend — a host-local "
                   "lock cannot exclude another host")
    except Exception:  # noqa: BLE001
        return SubstrateReading(Guarantee.UNVERIFIED, detail="probe failed")


#: Probes are cached per resolved path: a mount does not change under a running
#: process often enough to pay `disk_partitions()` on every critical section,
#: and that call shells out to the kernel.
_CACHE: dict = {}


def cached_probe(path: Path) -> SubstrateReading:
    """`probe`, memoised per path. NEVER raises."""
    try:
        key = str(Path(path).resolve())
    except Exception:  # noqa: BLE001
        key = str(path)
    reading = _CACHE.get(key)
    if reading is None:
        reading = probe(Path(path))
        _CACHE[key] = reading
        if reading.guarantee is Guarantee.UNSAFE:
            # ONCE per path, at WARNING. This is the one case worth the noise:
            # the operator has a real split-brain exposure and no error will
            # ever surface it — both hosts succeed.
            logger.warning(
                "[Coordination] %s is on %s — a host-local lock cannot exclude "
                "another host. Decisions coordinated here may DUPLICATE across "
                "machines. Configure the distributed lock backend (Redis) to "
                "close this, or keep one host per checkout.",
                key, reading.fstype,
            )
    return reading


def reset_cache() -> None:
    """Testing seam. NEVER raises."""
    _CACHE.clear()


@asynccontextmanager
async def exclusive(path: Path, key: str, *,
                    timeout_s: Optional[float] = None,
                    ttl_s: Optional[float] = None
                    ) -> AsyncIterator[Tuple[bool, SubstrateReading]]:
    """Hold the strongest available exclusion for *key*, and say which it was.

    Yields ``(acquired, reading)``. The reading travels with the acquisition so
    a caller can stamp the decision's provenance — a duplicate produced under
    `UNSAFE` is then explicable after the fact instead of a mystery.

    NEVER raises: on any failure it yields ``(False, reading)`` and the caller
    takes its own degraded path, exactly as it would on a lock timeout.
    """
    reading = cached_probe(path)
    if reading.guarantee is not Guarantee.CROSS_HOST:
        # Host-local is the right tool and the fast one. The caller's existing
        # flock path stays authoritative; we only report what it is worth.
        yield (False, reading)
        return
    try:
        from backend.core.distributed_lock_manager import get_lock_manager
        mgr = await get_lock_manager()
        async with mgr.acquire(key, timeout=timeout_s, ttl=ttl_s) as acquired:
            yield (bool(acquired), reading)
    except Exception:  # noqa: BLE001
        logger.debug("[Coordination] distributed acquire degraded", exc_info=True)
        yield (False, reading)
