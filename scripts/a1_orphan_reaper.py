#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autonomous out-of-band OrphanReaper -- continuously reaps soak/bake GCP
nodes lacking an active local lease (closes the interrupted-launcher zombie
blindspot).

Lease model: local file .jarvis/iac_leases/<node>.lease with JSON:
  {"node": str, "zone": str, "pid": int, "expires_ts": float}

A node is reaped when:
  * no lease file exists                        (reason=no-lease)
  * lease file exists but expires_ts < now()    (reason=expired)
  * lease file exists but owning pid is dead    (reason=dead-pid)

A node is NEVER reaped when:
  * its local lease is valid (pid alive + not expired), OR
  * it is within the boot-grace window (creation age < boot_grace_s) --
    freshly-created nodes may not have had time to register a lease.

All gcloud calls funnel through injectable lister/deleter so tests can
substitute fakes and pay $0.

Default-ON when the master gate is set (JARVIS_ORPHAN_REAPER_ENABLED=true).
Async-first; asyncio.wait_for everywhere (Python 3.9+; never asyncio.timeout).

Usage:
    # watch loop (120 s interval by default):
    JARVIS_ORPHAN_REAPER_ENABLED=1 python3 scripts/a1_orphan_reaper.py --watch

    # single pass:
    python3 scripts/a1_orphan_reaper.py --once

    # dry-run (print what WOULD be reaped, delete nothing):
    python3 scripts/a1_orphan_reaper.py --once --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import importlib.util as _importlib_util
import json
import logging
import os
import pathlib
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Repo root.
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Shared lease primitives (iac_lease.py is the single source of truth for the
# lease JSON shape -- neither consumer duplicates it).
# --------------------------------------------------------------------------- #
def _load_iac_lease():
    """Load scripts/iac_lease.py via importlib (fail-soft)."""
    _p = pathlib.Path(__file__).resolve().parent / "iac_lease.py"
    try:
        spec = _importlib_util.spec_from_file_location("iac_lease", str(_p))
        if spec and spec.loader:
            mod = _importlib_util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    except Exception:  # noqa: BLE001
        pass
    return None

_iac_lease = _load_iac_lease()

# --------------------------------------------------------------------------- #
# Node-name prefixes -- ONE canonical definition, never duplicated.
# Derived from:
#   autonomous_omni_launcher._SOAK_NODE_PREFIX  = "sovereign-sandbox-"
#   autonomous_omni_launcher._BAKE_NODE_PREFIX  = "jarvis-soak-bake-"
#   sovereign_iac_hypervisor node_name()        = "sovereign-sandbox-<stamp>"
# Plus the plain soak ("jarvis-soak-") and bake ("jarvis-bake-") variants
# used by independent provisioners.
# --------------------------------------------------------------------------- #
NODE_PREFIXES: Tuple[str, ...] = (
    "sovereign-sandbox-",
    "jarvis-soak-",
    "jarvis-bake-",
    "jarvis-soak-bake-",
)

# --------------------------------------------------------------------------- #
# Env-var driven config -- every tunable reads from env with a sensible default.
# --------------------------------------------------------------------------- #
_DEFAULT_PROJECT: str = os.environ.get("GCP_PROJECT", "jarvis-473803")
_DEFAULT_ZONE: str = os.environ.get("GCP_ZONE", "us-central1-a")
_DEFAULT_INTERVAL_S: int = int(os.environ.get("JARVIS_ORPHAN_REAPER_INTERVAL_S", "120"))
_DEFAULT_BOOT_GRACE_S: int = int(os.environ.get("JARVIS_ORPHAN_REAPER_BOOT_GRACE_S", "300"))
_DEFAULT_LEASE_TTL_S: int = int(os.environ.get("JARVIS_ORPHAN_REAPER_LEASE_TTL_S", "600"))
_GCLOUD_TIMEOUT_S: float = float(os.environ.get("JARVIS_ORPHAN_REAPER_GCLOUD_TIMEOUT_S", "60"))

_DEFAULT_LEASE_DIR: pathlib.Path = (
    _iac_lease.DEFAULT_LEASE_DIR if _iac_lease is not None
    else _REPO_ROOT / ".jarvis" / "iac_leases"
)

# --------------------------------------------------------------------------- #
# Logging -- a module-level logger; caller can configure level/handler.
# --------------------------------------------------------------------------- #
log = logging.getLogger("a1_orphan_reaper")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Default gcloud lister -- asyncio.create_subprocess_exec, never shell=True.
# --------------------------------------------------------------------------- #
async def _default_instance_lister(
    project: str,
    prefixes: Tuple[str, ...],
) -> List[Dict[str, Any]]:
    """List GCP instances whose names start with any of *prefixes*.

    Returns a list of {name, zone, creationTimestamp} dicts.
    Fail-soft: any error (network, auth, gcloud absent) yields [] and logs a
    warning -- the caller will simply find nothing to reap this pass.
    """
    # One regex alternation covers all prefixes in a single API call.
    alt = "|".join(f"^{p}" for p in prefixes)
    filter_str = f"name~({alt})"
    cmd: List[str] = [
        "gcloud", "compute", "instances", "list",
        f"--project={project}",
        f"--filter={filter_str}",
        "--format=json(name,zone,creationTimestamp)",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GCLOUD_TIMEOUT_S
        )
        if proc.returncode != 0:
            log.warning(
                "instance lister rc=%d: %s",
                proc.returncode,
                stderr.decode(errors="replace")[:300],
            )
            return []
        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return []
        instances: List[Dict[str, Any]] = json.loads(raw)
        result: List[Dict[str, Any]] = []
        for inst in instances:
            name: str = inst.get("name", "")
            if not any(name.startswith(p) for p in prefixes):
                continue
            zone_raw: str = inst.get("zone", "")
            zone = zone_raw.rsplit("/", 1)[-1] if "/" in zone_raw else zone_raw
            result.append(
                {
                    "name": name,
                    "zone": zone,
                    "creationTimestamp": inst.get("creationTimestamp", ""),
                }
            )
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("instance lister raised (fail-soft): %r", exc)
        return []


# --------------------------------------------------------------------------- #
# Default gcloud deleter -- asyncio.create_subprocess_exec, never shell=True.
# --------------------------------------------------------------------------- #
async def _default_instance_deleter(project: str, node: str, zone: str) -> None:
    """Delete a single GCP instance and all its disks. Fail-soft: never raises.

    Mirrors the hypervisor's reap idiom:
        gcloud compute instances delete <node> --delete-disks=all --quiet
    """
    cmd: List[str] = [
        "gcloud", "compute", "instances", "delete", node,
        f"--project={project}",
        f"--zone={zone}",
        "--delete-disks=all",
        "--quiet",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GCLOUD_TIMEOUT_S
        )
        if proc.returncode != 0:
            log.warning(
                "delete %s rc=%d: %s",
                node,
                proc.returncode,
                stderr.decode(errors="replace")[:300],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("delete %s raised (fail-soft): %r", node, exc)


# --------------------------------------------------------------------------- #
# Lease helpers (pure functions -- no class dependency).
# --------------------------------------------------------------------------- #
def _lease_path(lease_dir: pathlib.Path, node: str) -> pathlib.Path:
    """Return the lease file path for *node*.  Delegates to iac_lease (single source).

    Falls back to an inline implementation if iac_lease failed to load so the
    reaper remains self-contained.
    """
    if _iac_lease is not None:
        return _iac_lease.lease_path(node, lease_dir)
    safe = "".join(c for c in node if c.isalnum() or c in "-_.")
    return lease_dir / f"{safe}.lease"


def _parse_creation_ts(ts_str: str) -> Optional[float]:
    """Parse a GCP creationTimestamp (RFC 3339) into a Unix epoch float.

    GCP format: "2024-01-15T12:34:56.000-07:00" or ending with 'Z'.
    Returns None on any parse failure (fail-soft).
    """
    if not ts_str:
        return None
    try:
        # Python 3.7+ fromisoformat does not handle the 'Z' suffix -- replace it.
        normalised = ts_str.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(normalised).timestamp()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# OrphanReaper.
# --------------------------------------------------------------------------- #
class OrphanReaper:
    """Async orphan-node reaper with local lease-based liveness checking.

    Instantiate with injectable *instance_lister* / *instance_deleter* to keep
    tests $0 (no real gcloud calls needed).
    """

    def __init__(
        self,
        lease_dir: pathlib.Path = _DEFAULT_LEASE_DIR,
        project: str = _DEFAULT_PROJECT,
        zone: str = _DEFAULT_ZONE,
        interval_s: int = _DEFAULT_INTERVAL_S,
        boot_grace_s: int = _DEFAULT_BOOT_GRACE_S,
        dry_run: bool = False,
        instance_lister: Optional[Callable] = None,
        instance_deleter: Optional[Callable] = None,
    ) -> None:
        self.lease_dir = pathlib.Path(lease_dir)
        self.project = project
        self.zone = zone
        self.interval_s = interval_s
        self.boot_grace_s = boot_grace_s
        self.dry_run = dry_run
        self._lister: Callable = instance_lister or _default_instance_lister
        self._deleter: Callable = instance_deleter or _default_instance_deleter

    # ---------------------------------------------------------------------- #
    # Lease API.
    # ---------------------------------------------------------------------- #
    async def write_lease(
        self,
        node: str,
        zone: str,
        pid: int,
        ttl_s: int = _DEFAULT_LEASE_TTL_S,
    ) -> None:
        """Write (or refresh) the lease file for *node*.

        Atomic write: temp file + os.replace so readers never see a partial
        JSON. Creates the lease directory on first call.
        Fail-soft: a filesystem error is logged and swallowed.
        Delegates to iac_lease (single source of truth for the JSON shape).
        """
        if _iac_lease is not None:
            _iac_lease.write_lease(node, zone, pid, ttl_s, self.lease_dir)
            return
        # Fallback: inline implementation (keeps the reaper self-contained).
        try:
            self.lease_dir.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {
                "node": node,
                "zone": zone,
                "pid": pid,
                "expires_ts": time.time() + ttl_s,
            }
            path = _lease_path(self.lease_dir, node)
            tmp = path.with_suffix(".lease.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("write_lease(%s) failed (fail-soft): %r", node, exc)

    async def is_lease_valid(self, node: str) -> Tuple[bool, str]:
        """Check whether the local lease for *node* is currently valid.

        Returns (valid: bool, reason: str).
          valid=True,  reason='ok'       -- lease file exists, pid alive, not expired
          valid=False, reason='no-lease' -- file absent or unreadable
          valid=False, reason='expired'  -- file present but expires_ts < now()
          valid=False, reason='dead-pid' -- pid does not exist

        Delegates to iac_lease (single source of truth for the JSON shape).
        """
        if _iac_lease is not None:
            return _iac_lease.is_lease_valid(node, self.lease_dir)
        # Fallback: inline implementation (keeps the reaper self-contained).
        path = _lease_path(self.lease_dir, node)
        if not path.exists():
            return False, "no-lease"
        try:
            data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False, "no-lease"
        expires_ts = float(data.get("expires_ts", 0.0))
        if time.time() > expires_ts:
            return False, "expired"
        pid = int(data.get("pid", 0))
        if pid <= 0:
            return False, "dead-pid"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, "dead-pid"
        except PermissionError:
            return True, "ok"
        return True, "ok"

    # ---------------------------------------------------------------------- #
    # Core reap logic.
    # ---------------------------------------------------------------------- #
    async def run_once(self) -> None:
        """Single reap pass: list instances, check leases, reap orphans.

        Per-node errors are swallowed (fail-soft) so a single bad node never
        halts processing of subsequent nodes.
        """
        try:
            instances = await self._lister(self.project, NODE_PREFIXES)
        except Exception as exc:  # noqa: BLE001
            log.warning("instance lister raised (fail-soft, skip pass): %r", exc)
            return

        now = time.time()
        for inst in instances:
            node: str = inst.get("name", "")
            if not node:
                continue
            zone: str = inst.get("zone", "") or self.zone

            # --- Boot-grace check (creation timestamp). -------------------- #
            created = _parse_creation_ts(inst.get("creationTimestamp", ""))
            if created is not None:
                age = now - created
                if age < self.boot_grace_s:
                    log.debug(
                        "boot-grace: skip %s (age=%.0fs < grace=%ds)",
                        node, age, self.boot_grace_s,
                    )
                    continue

            # --- Lease validity check (fail-soft per-node). ---------------- #
            try:
                valid, reason = await self.is_lease_valid(node)
            except Exception as exc:  # noqa: BLE001
                log.warning("is_lease_valid(%s) raised (skip node): %r", node, exc)
                continue

            if valid:
                log.debug("valid lease: skip %s", node)
                continue

            # --- Orphan detected. ----------------------------------------- #
            if self.dry_run:
                log.info(
                    "[OrphanReaper] dry-run: would reap %s (reason=%s)", node, reason
                )
                continue

            log.info("[OrphanReaper] reaped %s (reason=%s)", node, reason)
            try:
                await self._deleter(self.project, node, zone)
            except Exception as exc:  # noqa: BLE001
                log.warning("deleter(%s) raised (fail-soft): %r", node, exc)

    async def reap_loop(self, interval_s: Optional[int] = None) -> None:
        """Continuous reap loop: run_once() every *interval_s* seconds.

        Runs forever (or until cancelled). Suitable for use as a background
        asyncio task:
            asyncio.create_task(reaper.reap_loop())
        """
        effective_interval = interval_s if interval_s is not None else self.interval_s
        while True:
            await self.run_once()
            await asyncio.sleep(effective_interval)


# --------------------------------------------------------------------------- #
# CLI entry point.
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Autonomous out-of-band OrphanReaper -- reaps soak/bake GCP nodes "
            "lacking an active local lease. Default: --once (single pass)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--watch",
        action="store_true",
        help="run continuously every --interval seconds",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="single reap pass then exit (default)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="log what WOULD be reaped; never delete anything",
    )
    p.add_argument(
        "--project",
        default=_DEFAULT_PROJECT,
        help="GCP project (env GCP_PROJECT)",
    )
    p.add_argument(
        "--zone",
        default=_DEFAULT_ZONE,
        help="GCP zone fallback (env GCP_ZONE)",
    )
    p.add_argument(
        "--interval",
        dest="interval_s",
        type=int,
        default=_DEFAULT_INTERVAL_S,
        help="watch-mode poll interval in seconds (env JARVIS_ORPHAN_REAPER_INTERVAL_S)",
    )
    p.add_argument(
        "--boot-grace",
        dest="boot_grace_s",
        type=int,
        default=_DEFAULT_BOOT_GRACE_S,
        help="seconds after creation during which a node is never reaped "
             "(env JARVIS_ORPHAN_REAPER_BOOT_GRACE_S)",
    )
    p.add_argument(
        "--lease-dir",
        dest="lease_dir",
        type=pathlib.Path,
        default=_DEFAULT_LEASE_DIR,
        help="directory containing .lease files",
    )
    return p


def _main(argv: Optional[List[str]] = None) -> int:
    if not _env_truthy("JARVIS_ORPHAN_REAPER_ENABLED", "true"):
        log.info("[OrphanReaper] JARVIS_ORPHAN_REAPER_ENABLED=false -- not running")
        return 0

    args = _build_parser().parse_args(argv)

    reaper = OrphanReaper(
        lease_dir=args.lease_dir,
        project=args.project,
        zone=args.zone,
        interval_s=args.interval_s,
        boot_grace_s=args.boot_grace_s,
        dry_run=args.dry_run,
    )

    if args.watch:
        log.info(
            "[OrphanReaper] watch mode: interval=%ds boot_grace=%ds dry_run=%s",
            args.interval_s, args.boot_grace_s, args.dry_run,
        )
        asyncio.run(reaper.reap_loop())
    else:
        log.info(
            "[OrphanReaper] single pass: boot_grace=%ds dry_run=%s",
            args.boot_grace_s, args.dry_run,
        )
        asyncio.run(reaper.run_once())

    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
