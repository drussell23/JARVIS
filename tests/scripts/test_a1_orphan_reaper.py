from __future__ import annotations

"""Tests for a1_orphan_reaper.OrphanReaper -- $0 (no real gcloud calls).

All 8 required scenarios are covered:
  1. valid-lease node            -> NOT reaped
  2. expired-lease node          -> REAPED, reason=expired
  3. dead-pid node               -> REAPED, reason=dead-pid
  4. no-lease node               -> REAPED, reason=no-lease
  5. boot-grace node             -> NOT reaped (even without a lease)
  6. dry-run                     -> expired + dead-pid nodes NOT deleted
  7. lease write/read/expiry     -> roundtrip + advance-time expiry
  8. fail-soft                   -> deleter raises for one node; others processed

Fake lister/deleter are injected -- zero real gcloud calls, zero dollars.
Uses asyncio.run() for each async test (compatible with pytest without
pytest-asyncio; works under Python 3.9+).
"""

import asyncio
import datetime
import importlib.util
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

# --------------------------------------------------------------------------- #
# Load the module under test via importlib so the test suite works even when
# the scripts/ directory is not on sys.path.
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REAPER_PATH = _REPO_ROOT / "scripts" / "a1_orphan_reaper.py"


def _load_reaper():
    spec = importlib.util.spec_from_file_location("a1_orphan_reaper", str(_REAPER_PATH))
    assert spec and spec.loader, f"could not load spec from {_REAPER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["a1_orphan_reaper"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def R():
    """Module-scoped fixture: load the reaper module once per test session."""
    return _load_reaper()


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _make_instance(
    name: str,
    zone: str = "us-central1-a",
    age_s: float = 1000.0,
) -> Dict[str, Any]:
    """Build a fake GCP instance dict.

    age_s defaults to 1000 s (>> default boot_grace_s=300) so the instance is
    considered past boot-grace and eligible for reaping.
    """
    created = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_s)
    ts = created.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    return {"name": name, "zone": zone, "creationTimestamp": ts}


def _make_reaper(
    R,
    tmp_path: pathlib.Path,
    instances: List[Dict[str, Any]],
    dry_run: bool = False,
    boot_grace_s: int = 300,
) -> Tuple["Any", List[str]]:
    """Construct an OrphanReaper with injected fake lister/deleter.

    Returns (reaper, deleted_list).  deleted_list is mutated by fake_deleter
    so tests can inspect which nodes were actually deleted.
    """
    deleted: List[str] = []

    async def fake_lister(project: str, prefixes: tuple) -> List[Dict[str, Any]]:
        return list(instances)

    async def fake_deleter(project: str, node: str, zone: str) -> None:
        deleted.append(node)

    reaper = R.OrphanReaper(
        lease_dir=tmp_path / "leases",
        project="test-project",
        zone="us-central1-a",
        interval_s=1,
        boot_grace_s=boot_grace_s,
        dry_run=dry_run,
        instance_lister=fake_lister,
        instance_deleter=fake_deleter,
    )
    return reaper, deleted


# --------------------------------------------------------------------------- #
# Scenario 1: valid-lease node -> NOT reaped.
# --------------------------------------------------------------------------- #

def test_valid_lease_not_reaped(R, tmp_path):
    """A node with a valid lease (pid alive, not expired) must never be deleted."""
    node = "sovereign-sandbox-valid"
    instances = [_make_instance(node)]
    reaper, deleted = _make_reaper(R, tmp_path, instances)

    async def run():
        # Write a lease owned by this process, expiring in 1 h.
        await reaper.write_lease(node, "us-central1-a", os.getpid(), ttl_s=3600)
        await reaper.run_once()

    asyncio.run(run())
    assert node not in deleted, "valid-lease node must NOT be reaped"


# --------------------------------------------------------------------------- #
# Scenario 2: expired-lease node -> REAPED, reason=expired.
# --------------------------------------------------------------------------- #

def test_expired_lease_reaped(R, tmp_path):
    """A node whose lease's expires_ts is in the past must be reaped."""
    node = "sovereign-sandbox-expired"
    instances = [_make_instance(node)]
    reaper, deleted = _make_reaper(R, tmp_path, instances)

    async def run():
        lease_dir = tmp_path / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        path = lease_dir / f"{node}.lease"
        payload = {
            "node": node,
            "zone": "us-central1-a",
            "pid": os.getpid(),
            "expires_ts": time.time() - 1.0,  # already expired
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        await reaper.run_once()

    asyncio.run(run())
    assert node in deleted, "expired-lease node must be reaped"


# --------------------------------------------------------------------------- #
# Scenario 3: dead-pid node -> REAPED, reason=dead-pid.
# --------------------------------------------------------------------------- #

def test_dead_pid_reaped(R, tmp_path):
    """A node whose lease names a non-existent PID must be reaped."""
    node = "jarvis-soak-deadpid"
    instances = [_make_instance(node)]
    reaper, deleted = _make_reaper(R, tmp_path, instances)

    async def run():
        # pid 999999 is virtually never alive on a developer machine.
        await reaper.write_lease(node, "us-central1-a", 999999, ttl_s=3600)
        await reaper.run_once()

    asyncio.run(run())
    assert node in deleted, "dead-pid node must be reaped"


# --------------------------------------------------------------------------- #
# Scenario 4: no-lease node -> REAPED, reason=no-lease.
# --------------------------------------------------------------------------- #

def test_no_lease_reaped(R, tmp_path):
    """A node with no lease file at all must be reaped."""
    node = "jarvis-bake-nolease"
    instances = [_make_instance(node)]
    reaper, deleted = _make_reaper(R, tmp_path, instances)

    asyncio.run(reaper.run_once())
    assert node in deleted, "no-lease node must be reaped"


# --------------------------------------------------------------------------- #
# Scenario 5: boot-grace node -> NOT reaped, even without a lease.
# --------------------------------------------------------------------------- #

def test_boot_grace_not_reaped(R, tmp_path):
    """A freshly-created node (within boot_grace_s) must not be reaped."""
    node = "sovereign-sandbox-fresh"
    # age_s=10 << boot_grace_s=300 -> still in grace window.
    instances = [_make_instance(node, age_s=10.0)]
    reaper, deleted = _make_reaper(R, tmp_path, instances, boot_grace_s=300)

    asyncio.run(reaper.run_once())
    assert node not in deleted, (
        "boot-grace node must NOT be reaped even without a lease"
    )


# --------------------------------------------------------------------------- #
# Scenario 6: dry-run -> nothing deleted even for expired + dead-pid nodes.
# --------------------------------------------------------------------------- #

def test_dry_run_no_delete(R, tmp_path):
    """With dry_run=True the deleter must NEVER be called."""
    expired_node = "sovereign-sandbox-exp"
    deadpid_node = "jarvis-soak-dead"
    instances = [_make_instance(expired_node), _make_instance(deadpid_node)]
    reaper, deleted = _make_reaper(R, tmp_path, instances, dry_run=True)

    async def run():
        lease_dir = tmp_path / "leases"
        lease_dir.mkdir(parents=True, exist_ok=True)

        # Expired lease.
        (lease_dir / f"{expired_node}.lease").write_text(
            json.dumps({
                "node": expired_node,
                "zone": "us-central1-a",
                "pid": os.getpid(),
                "expires_ts": time.time() - 1.0,
            }),
            encoding="utf-8",
        )
        # Dead-pid lease (still unexpired).
        (lease_dir / f"{deadpid_node}.lease").write_text(
            json.dumps({
                "node": deadpid_node,
                "zone": "us-central1-a",
                "pid": 999999,
                "expires_ts": time.time() + 3600,
            }),
            encoding="utf-8",
        )
        await reaper.run_once()

    asyncio.run(run())
    assert deleted == [], "dry-run must never call the deleter"


# --------------------------------------------------------------------------- #
# Scenario 7: lease write/read/expiry roundtrip.
# --------------------------------------------------------------------------- #

def test_lease_write_read_expiry(R, tmp_path):
    """write_lease -> is_lease_valid=True; overwrite expires_ts -> is_lease_valid=False."""
    node = "jarvis-soak-bake-roundtrip"
    reaper, _ = _make_reaper(R, tmp_path, instances=[])

    async def run():
        # Write a fresh lease.
        await reaper.write_lease(node, "us-central1-a", os.getpid(), ttl_s=3600)
        valid, reason = await reaper.is_lease_valid(node)
        assert valid, f"freshly written lease must be valid; got reason={reason!r}"
        assert reason == "ok", f"expected reason 'ok', got {reason!r}"

        # Manually expire the lease by back-dating expires_ts.
        lease_dir = tmp_path / "leases"
        path = lease_dir / f"{node}.lease"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["expires_ts"] = time.time() - 1.0
        path.write_text(json.dumps(data), encoding="utf-8")

        valid2, reason2 = await reaper.is_lease_valid(node)
        assert not valid2, "back-dated lease must be invalid"
        assert reason2 == "expired", f"expected reason 'expired', got {reason2!r}"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Scenario 8: fail-soft -- deleter raises for one node; others still processed.
# --------------------------------------------------------------------------- #

def test_fail_soft_per_node(R, tmp_path):
    """A deleter exception on one node must not prevent other nodes from being reaped."""
    node_bad = "sovereign-sandbox-badfail"
    node_ok  = "sovereign-sandbox-goodnode"
    instances = [_make_instance(node_bad), _make_instance(node_ok)]

    deleted: List[str] = []
    call_count: List[int] = [0]

    async def fake_lister(project: str, prefixes: tuple) -> List[Dict[str, Any]]:
        return list(instances)

    async def fake_deleter(project: str, node: str, zone: str) -> None:
        call_count[0] += 1
        if node == node_bad:
            raise RuntimeError("simulated delete failure for node_bad")
        deleted.append(node)

    reaper = R.OrphanReaper(
        lease_dir=tmp_path / "leases",
        project="test-project",
        zone="us-central1-a",
        boot_grace_s=300,
        dry_run=False,
        instance_lister=fake_lister,
        instance_deleter=fake_deleter,
    )

    asyncio.run(reaper.run_once())

    assert node_ok in deleted, (
        "fail-soft: the good node must still be reaped after bad node raises"
    )
    assert call_count[0] == 2, (
        f"deleter must be called for BOTH nodes; called {call_count[0]} time(s)"
    )


# --------------------------------------------------------------------------- #
# Bonus: NODE_PREFIXES is a tuple of exactly the expected four prefixes.
# --------------------------------------------------------------------------- #

def test_node_prefixes_constant(R):
    """NODE_PREFIXES must contain exactly the four canonical prefixes."""
    expected = {
        "sovereign-sandbox-",
        "jarvis-soak-",
        "jarvis-bake-",
        "jarvis-soak-bake-",
    }
    assert set(R.NODE_PREFIXES) == expected, (
        f"NODE_PREFIXES mismatch: {set(R.NODE_PREFIXES)} != {expected}"
    )
