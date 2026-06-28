#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared synchronous lease primitives for IaC bake + OrphanReaper.

Both bake_soak_golden_image.py (sync baker) and a1_orphan_reaper.py (async
reaper) need the same lease JSON shape.  This module owns that shape --
neither consumer duplicates it.

Lease JSON: {"node": str, "zone": str, "pid": int, "expires_ts": float}

Semantics:
  A crashed baker leaves a lease whose pid is dead -> reaper reaps the orphan.
  A live baker keeps a valid lease               -> reaper spares it.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import Optional, Tuple

# --------------------------------------------------------------------------- #
# Repo root + default lease directory (single canonical location).
# --------------------------------------------------------------------------- #
_REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[1]

# Mirror of a1_orphan_reaper._DEFAULT_LEASE_DIR -- one definition, both import.
DEFAULT_LEASE_DIR: pathlib.Path = _REPO_ROOT / ".jarvis" / "iac_leases"

# --------------------------------------------------------------------------- #
# Module logger.
# --------------------------------------------------------------------------- #
log = logging.getLogger("iac_lease")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #

def lease_path(node: str, lease_dir: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Return the lease file path for *node*.

    Sanitises the node name (GCP instance names: alnum + hyphen + underscore +
    dot) to prevent any path traversal.
    """
    d = pathlib.Path(lease_dir) if lease_dir is not None else DEFAULT_LEASE_DIR
    safe = "".join(c for c in node if c.isalnum() or c in "-_.")
    return d / f"{safe}.lease"


def write_lease(
    node: str,
    zone: str,
    pid: int,
    ttl_s: int,
    lease_dir: Optional[pathlib.Path] = None,
) -> None:
    """Write (or refresh) the lease file for *node*.  Sync, atomic write.

    Atomic write via temp file + os.replace so readers never see partial JSON.
    Creates the lease directory on first call.  Fail-soft: a filesystem error is
    logged and swallowed.
    """
    try:
        d = pathlib.Path(lease_dir) if lease_dir is not None else DEFAULT_LEASE_DIR
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "node": node,
            "zone": zone,
            "pid": pid,
            "expires_ts": time.time() + ttl_s,
        }
        p = lease_path(node, d)
        tmp = p.with_suffix(".lease.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:  # noqa: BLE001
        log.warning("write_lease(%s) failed (fail-soft): %r", node, exc)


def delete_lease(
    node: str,
    lease_dir: Optional[pathlib.Path] = None,
) -> None:
    """Delete the lease file for *node*.  Fail-soft."""
    try:
        p = lease_path(node, lease_dir)
        p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_lease(%s) failed (fail-soft): %r", node, exc)


def is_lease_valid(
    node: str,
    lease_dir: Optional[pathlib.Path] = None,
) -> Tuple[bool, str]:
    """Synchronous lease-validity check.  Returns (valid, reason).

    valid=True,  reason='ok'        lease file present, pid alive, not expired
    valid=False, reason='no-lease'  file absent or unreadable
    valid=False, reason='expired'   expires_ts < now()
    valid=False, reason='dead-pid'  pid does not exist
    """
    p = lease_path(node, lease_dir)
    if not p.exists():
        return False, "no-lease"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
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
        # EPERM: pid exists but owned by different user -> still alive.
        return True, "ok"
    return True, "ok"
