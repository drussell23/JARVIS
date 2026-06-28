# -*- coding: utf-8 -*-
"""Pure-logic tests for scripts/bake_soak_golden_image.py.

No real gcloud. ALL subprocess goes through the script's single `_run` boundary,
which these tests monkeypatch to assert dry-run never executes, the VALIDATION
LOCK aborts (no snapshot) on broken deps, the image is stamped with the
requirements sha label, and execute deletes the bake node on every exit path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "bake_soak_golden_image.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("bake_soak_golden_image", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bake():
    return _load_module()


@pytest.fixture()
def args(bake):
    return bake.build_parser().parse_args([])


class _Recorder:
    """Records every (cmd) passed to the faked _run, in order, with scripted rcs."""

    def __init__(self, responses=None):
        self.calls = []
        # responses: list of (predicate, (rc, out)). First match wins.
        self.responses = responses or []
        self.default = (0, "")

    def __call__(self, cmd, *, timeout_s=120.0):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for pred, resp in self.responses:
            if pred(joined):
                return resp
        return self.default

    def joined(self):
        return [" ".join(c) for c in self.calls]


# --------------------------------------------------------------------------- #
# Startup-script generator.
# --------------------------------------------------------------------------- #
def test_startup_script_installs_deps_and_sentinels(bake):
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    # Installs pip + build tools.
    assert "python3-pip" in script
    # Hard-ensure core deps appear in the install line.
    for core in ("aiohttp", "uuid6", "fastapi", "pydantic"):
        assert core in script
    # ROOT-CAUSE REGRESSION GUARD: HOME exported before anything.
    assert "export HOME=" in script
    # Sentinel written ONLY after the install (mirrors J-Prime).
    assert bake._SENTINEL_PATH in script
    # Filters the ML libs (no native portaudio/torch build on a bare node).
    assert "torch" in script and "pyaudio" in script  # they appear in the filter
    # requirements.txt staged from metadata.
    assert "jarvis-requirements" in script


def test_startup_script_sentinel_after_install(bake):
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    # The sentinel write must come AFTER the hard-ensure install command.
    assert script.index("hard-ensuring core deps") < script.index("hard-ensure install complete")


def test_startup_script_bakes_docker(bake):
    """Golden image must bake the Docker engine so the IaC boot's 'installing
    Docker' + 'docker info' ready-probe are instant (the Confirm-4 fix)."""
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    # Docker package install is in the startup-script.
    assert "docker.io" in script
    assert "docker-compose-plugin" in script
    # Daemon is enabled so it auto-starts on every golden boot.
    assert "systemctl enable docker" in script
    # Fail-closed guard: script verifies docker binary is present.
    assert "command -v docker" in script


def test_startup_script_no_docker_pull_logged(bake):
    """Soak is pure-python -- no docker pull at runtime. The script must log
    this explicitly so operators know no pre-pull step is missing."""
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    assert "no docker pull required" in script


def test_startup_script_sentinel_after_docker(bake):
    """Sentinel must appear AFTER the Docker install + verify block (all-or-nothing)."""
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    docker_install_pos = script.index("docker.io")
    sentinel_pos = script.index(bake._SENTINEL_PATH)
    # sentinel_path appears in the 'rm -f' guard AND the write -- use the LAST
    # occurrence (the actual write) for the ordering check.
    sentinel_write_pos = script.rindex(bake._SENTINEL_PATH)
    assert docker_install_pos < sentinel_write_pos, (
        "sentinel write must come AFTER the Docker install step"
    )


def test_startup_script_fail_closed_docker(bake):
    """If Docker is not found after install, the script must exit 1 and NOT
    write the sentinel (fail-closed: no broken golden image)."""
    deps = bake._load_hard_ensure_deps()
    script = bake.build_startup_script(deps)
    # The fail-closed exit must appear before the sentinel write.
    docker_fail_exit_pos = script.index(
        "docker not found after install -- NOT writing sentinel"
    )
    sentinel_write_pos = script.rindex(bake._SENTINEL_PATH)
    assert docker_fail_exit_pos < sentinel_write_pos, (
        "docker fail-closed exit must appear before the sentinel write"
    )
    # And the exit statement must be present.
    assert "exit 1" in script[docker_fail_exit_pos:sentinel_write_pos]


# --------------------------------------------------------------------------- #
# requirements sha (the staleness stamp).
# --------------------------------------------------------------------------- #
def test_requirements_sha_stable_and_truncated(bake, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("aiohttp\nuuid6\n")
    sha = bake.requirements_sha(str(req))
    assert len(sha) == 16
    assert sha == bake.requirements_sha(str(req))  # deterministic
    # Changing the content changes the sha.
    req.write_text("aiohttp\nuuid6\nhttpx\n")
    assert bake.requirements_sha(str(req)) != sha


def test_requirements_sha_missing_file_failsoft(bake, tmp_path):
    assert bake.requirements_sha(str(tmp_path / "nope.txt")) == "norequirements"


def test_real_requirements_sha_is_valid_label(bake):
    """The repo's real requirements.txt produces a GCP-label-safe sha value."""
    import re as _re

    sha = bake.requirements_sha(bake._DEFAULT_REQUIREMENTS)
    assert _re.fullmatch(r"[a-z0-9_-]{1,63}", sha), sha


# --------------------------------------------------------------------------- #
# Validation verdict parser (the load-bearing lock).
# --------------------------------------------------------------------------- #
def test_validation_pass_on_ok_marker(bake):
    ok, _ = bake.parse_validation_verdict(0, "some output\nSOAK_BAKE_VALIDATION_OK\n")
    assert ok is True


def test_validation_fail_on_fail_marker(bake):
    ok, reason = bake.parse_validation_verdict(
        0, "SOAK_BAKE_VALIDATION_FAIL deps-import: ModuleNotFoundError uuid6"
    )
    assert ok is False
    assert "uuid6" in reason


def test_validation_fail_on_missing_ok(bake):
    ok, _ = bake.parse_validation_verdict(0, "no markers here")
    assert ok is False


def test_validation_fail_on_transport_error(bake):
    ok, reason = bake.parse_validation_verdict(1, "SOAK_BAKE_VALIDATION_OK")
    assert ok is False
    assert "rc=" in reason


def test_validation_fail_on_empty(bake):
    ok, _ = bake.parse_validation_verdict(0, "   ")
    assert ok is False


def test_validation_remote_runs_three_checks(bake):
    remote = bake.build_validation_remote()
    assert "import aiohttp, uuid6, fastapi, pydantic" in remote  # CHECK 1
    assert "pytest --collect-only" in remote  # CHECK 2
    assert "operation_id" in remote  # CHECK 3 (O+V core)
    assert "SOAK_BAKE_VALIDATION_OK" in remote
    assert "SOAK_BAKE_VALIDATION_FAIL" in remote


# --------------------------------------------------------------------------- #
# Node-create: ON-DEMAND, debian source image, ships requirements as metadata.
# --------------------------------------------------------------------------- #
def test_create_node_cmd_ships_requirements_metadata(bake, args):
    cmd = bake._create_node_cmd(args, "soak-bake-x", "/tmp/startup.sh", "/repo/requirements.txt")
    joined = " ".join(cmd)
    assert "--image-family=debian-12" in joined
    assert "--image-project=debian-cloud" in joined
    # No SPOT for the bake (reliability).
    assert "SPOT" not in joined
    # Ships both the startup-script AND the requirements metadata.
    assert "startup-script=/tmp/startup.sh" in joined
    assert "jarvis-requirements=/repo/requirements.txt" in joined


# --------------------------------------------------------------------------- #
# Dry-run: prints the plan + req-sha label, executes NOTHING.
# --------------------------------------------------------------------------- #
def test_dry_run_executes_nothing(bake, monkeypatch, capsys):
    rec = _Recorder()
    monkeypatch.setattr(bake, "_run", rec)
    rc = bake.main(["--dry-run"])
    assert rc == 0
    assert rec.calls == []  # NOTHING executed
    out = capsys.readouterr().out
    assert "jarvis_req_sha=" in out  # staleness label printed in the plan
    assert "spends nothing" in out


# --------------------------------------------------------------------------- #
# EXECUTE happy path: validate PASS -> snapshot WITH req-sha label -> node deleted.
# --------------------------------------------------------------------------- #
def test_execute_happy_path_snapshots_with_label_and_cleans_up(bake, monkeypatch):
    # readiness sentinel present immediately; validation passes.
    rec = _Recorder(responses=[
        (lambda j: "test -f " in j, (0, "SOAK_READY")),
        (lambda j: "SOAK_BAKE_VALIDATION_OK" in j or "import aiohttp" in j,
         (0, "SOAK_BAKE_VALIDATION_OK")),
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),  # image absent
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-n", "<startup>")
    assert rc == 0
    j = rec.joined()
    # An image was created with the req-sha label.
    create = [c for c in j if "images create" in c]
    assert create, "no image create issued"
    assert "--labels=jarvis_req_sha=" in create[0]
    # Node + disk deleted (no orphaned billing) -- on the success path too.
    assert any("instances delete" in c and "--delete-disks=all" in c for c in j)


# --------------------------------------------------------------------------- #
# THE VALIDATION LOCK: broken deps -> ABORT, NO image, node deleted, non-zero.
# --------------------------------------------------------------------------- #
def test_validation_failure_aborts_no_snapshot_deletes_node(bake, monkeypatch):
    rec = _Recorder(responses=[
        (lambda j: "test -f " in j, (0, "SOAK_READY")),
        # validation FAILS (broken deps).
        (lambda j: "import aiohttp" in j,
         (0, "SOAK_BAKE_VALIDATION_FAIL deps-import: No module named 'uuid6'")),
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-n", "<startup>")
    assert rc == 6  # validation-failure exit code (non-zero)
    j = rec.joined()
    # NO image was ever created (never snapshot a broken image).
    assert not any("images create" in c for c in j)
    # The node was STILL deleted (no orphaned billing on abort).
    assert any("instances delete" in c for c in j)
    # ORDER: validation ran, then delete -- and NO create between them.
    idx_val = next(i for i, c in enumerate(j) if "import aiohttp" in c)
    idx_del = next(i for i, c in enumerate(j) if "instances delete" in c)
    assert idx_del > idx_val
    assert not any("images create" in c for c in j[idx_val:idx_del])


# --------------------------------------------------------------------------- #
# Readiness timeout (early-fail) also aborts + deletes (no snapshot).
# --------------------------------------------------------------------------- #
def test_readiness_early_fail_aborts_and_deletes(bake, monkeypatch):
    rec = _Recorder(responses=[
        (lambda j: "test -f " in j, (0, "SOAK_NOT_READY")),
        # bake log shows an ERROR -> early abort.
        (lambda j: "tail -n 5" in j, (0, "[soak-bake] ERROR: pip install failed")),
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-n", "<startup>")
    assert rc == 5
    j = rec.joined()
    assert not any("images create" in c for c in j)
    assert any("instances delete" in c for c in j)


# --------------------------------------------------------------------------- #
# Provision failure -> abort, node never created so no delete needed, non-zero.
# --------------------------------------------------------------------------- #
def test_provision_failure_aborts(bake, monkeypatch):
    rec = _Recorder(responses=[
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
        (lambda j: "instances create" in j, (1, "QUOTA_EXCEEDED")),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-n", "<startup>")
    assert rc == 4
    # Node never came up -> no delete issued.
    assert not any("instances delete" in c for c in rec.joined())


# --------------------------------------------------------------------------- #
# Idempotency: existing image without --force refuses (exit 3, no provision).
# --------------------------------------------------------------------------- #
def test_existing_image_without_force_refuses(bake, monkeypatch):
    rec = _Recorder(responses=[
        (lambda j: "images describe" in j, (0, "jarvis-soak-golden-x")),  # exists
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-n", "<startup>")
    assert rc == 3
    assert not any("instances create" in c for c in rec.joined())


# --------------------------------------------------------------------------- #
# Reuse: the baker pulls the hard-ensure list from the IaC hypervisor (no dup).
# --------------------------------------------------------------------------- #
def test_hard_ensure_deps_match_iac(bake):
    deps = bake._load_hard_ensure_deps()
    assert "uuid6" in deps and "pytest-asyncio" in deps and "aiohttp" in deps
    # Load the IaC module directly and compare -- they must be the same source.
    import importlib.util as _u

    iac_path = Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
    spec = _u.spec_from_file_location("_iac_cmp", str(iac_path))
    iac = _u.module_from_spec(spec)
    spec.loader.exec_module(iac)
    assert deps == iac.hard_ensure_deps()


# --------------------------------------------------------------------------- #
# Loader helpers for iac_lease and a1_orphan_reaper.
# --------------------------------------------------------------------------- #
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_iac_lease():
    spec = importlib.util.spec_from_file_location(
        "iac_lease", str(_SCRIPTS / "iac_lease.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_reaper():
    spec = importlib.util.spec_from_file_location(
        "a1_orphan_reaper", str(_SCRIPTS / "a1_orphan_reaper.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Fix #1 -- SSH transport-retry wrapper.
# --------------------------------------------------------------------------- #

def test_ssh_transport_retry_succeeds_after_flakes(bake, monkeypatch):
    """Transport flakes (rc=255, then conn-error with no marker) then SUCCESS.

    Assert:
      - 3 calls to _run (2 flakes + 1 success)
      - time.sleep called twice (backoff after each flake)
      - final result contains the OK marker
    """
    call_count: List[int] = [0]

    def fake_run(cmd, *, timeout_s=120.0):
        call_count[0] += 1
        n = call_count[0]
        if n == 1:
            # Transport flake: SSH own exit code.
            return 255, "ssh: connect to host node port 22: Connection timed out"
        if n == 2:
            # Transport flake: connection-error pattern in output, no marker.
            return 0, "Connection reset by peer"
        # Third attempt succeeds.
        return 0, f"output {bake._VALIDATION_OK_MARKER}"

    sleep_calls: List[float] = []
    monkeypatch.setattr(bake, "_run", fake_run)
    monkeypatch.setattr(bake.time, "sleep", lambda s: sleep_calls.append(s))

    rc, out = bake._run_ssh_with_transport_retry(
        ["gcloud", "compute", "ssh", "node", "--command", "validate"],
        timeout_s=300.0,
        max_retries=3,
        backoff_base=1.0,
        backoff_cap=10.0,
    )

    assert call_count[0] == 3, f"expected 3 attempts, got {call_count[0]}"
    assert len(sleep_calls) == 2, (
        f"expected 2 backoff sleeps (after flakes 1 and 2), got {len(sleep_calls)}"
    )
    assert all(s > 0 for s in sleep_calls), "backoff delays must be positive"
    assert bake._VALIDATION_OK_MARKER in out


def test_ssh_transport_retry_no_marker_retries(bake, monkeypatch):
    """No verdict marker in output (remote died mid-stream) -> treated as transport -> retry."""
    call_count: List[int] = [0]

    def fake_run(cmd, *, timeout_s=120.0):
        call_count[0] += 1
        if call_count[0] < 3:
            # No marker, rc=0: remote output truncated (transport flake).
            return 0, "partial output no marker here"
        return 0, f"full output {bake._VALIDATION_OK_MARKER}"

    monkeypatch.setattr(bake, "_run", fake_run)
    monkeypatch.setattr(bake.time, "sleep", lambda s: None)

    rc, out = bake._run_ssh_with_transport_retry(
        ["ssh", "node"],
        max_retries=3,
        backoff_base=0.001,
        backoff_cap=0.01,
    )

    assert call_count[0] == 3
    assert bake._VALIDATION_OK_MARKER in out


def test_ssh_payload_fail_no_retry(bake, monkeypatch):
    """FAIL marker present -> fail-closed immediately, exactly 1 attempt, no sleep."""
    call_count: List[int] = [0]
    sleep_calls: List[float] = []

    def fake_run(cmd, *, timeout_s=120.0):
        call_count[0] += 1
        return 0, f"{bake._VALIDATION_FAIL_MARKER} deps-import: No module named 'uuid6'"

    monkeypatch.setattr(bake, "_run", fake_run)
    monkeypatch.setattr(bake.time, "sleep", lambda s: sleep_calls.append(s))

    rc, out = bake._run_ssh_with_transport_retry(
        ["gcloud", "compute", "ssh", "node", "--command", "validate"],
        timeout_s=300.0,
        max_retries=3,
        backoff_base=1.0,
        backoff_cap=10.0,
    )

    assert call_count[0] == 1, (
        f"payload fail must be fail-closed (1 attempt), got {call_count[0]}"
    )
    assert sleep_calls == [], "no backoff sleep must occur on payload failure"
    assert bake._VALIDATION_FAIL_MARKER in out


def test_ssh_transport_all_retries_exhausted(bake, monkeypatch):
    """All retries exhausted on transport flakes -> return last result (non-zero)."""
    call_count: List[int] = [0]

    def fake_run(cmd, *, timeout_s=120.0):
        call_count[0] += 1
        return 255, "ssh: connect to host: Connection refused"

    monkeypatch.setattr(bake, "_run", fake_run)
    monkeypatch.setattr(bake.time, "sleep", lambda s: None)

    rc, out = bake._run_ssh_with_transport_retry(
        ["ssh", "node"],
        max_retries=3,
        backoff_base=0.001,
        backoff_cap=0.01,
    )

    assert call_count[0] == 3, "all retries must be exhausted"
    assert rc == 255


# --------------------------------------------------------------------------- #
# Fix #2 -- Lease lifecycle.
# --------------------------------------------------------------------------- #

class _FakeIacLease:
    """Captures write_lease / delete_lease calls without touching the filesystem."""

    def __init__(self):
        self.written: List[Dict[str, Any]] = []
        self.deleted: List[str] = []

    def write_lease(self, node, zone, pid, ttl_s, lease_dir=None):
        self.written.append({"node": node, "zone": zone, "pid": pid, "ttl_s": ttl_s})

    def delete_lease(self, node, lease_dir=None):
        self.deleted.append(node)


def test_lease_written_after_node_creation(bake, monkeypatch):
    """Baker writes lease immediately after node creation (before readiness poll)."""
    fake_lease = _FakeIacLease()
    monkeypatch.setattr(bake, "_iac_lease", fake_lease)

    rec = _Recorder(responses=[
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
        (lambda j: "instances create" in j, (0, "created")),
        (lambda j: "test -f " in j, (0, "SOAK_READY")),
        (lambda j: "import aiohttp" in j, (0, bake._VALIDATION_OK_MARKER)),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-lease-test", "<startup>")

    assert rc == 0
    # Lease must have been written at least once (initial write + heartbeats).
    written_nodes = [w["node"] for w in fake_lease.written]
    assert "soak-bake-lease-test" in written_nodes, (
        "lease must be written for the bake node after creation"
    )
    # The lease must carry the baker's own pid.
    for entry in fake_lease.written:
        if entry["node"] == "soak-bake-lease-test":
            assert entry["pid"] == os.getpid(), "lease pid must match baker process"
            break


def test_lease_deleted_on_success(bake, monkeypatch):
    """Lease is deleted in the finally block on the success path."""
    fake_lease = _FakeIacLease()
    monkeypatch.setattr(bake, "_iac_lease", fake_lease)

    rec = _Recorder(responses=[
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
        (lambda j: "instances create" in j, (0, "created")),
        (lambda j: "test -f " in j, (0, "SOAK_READY")),
        (lambda j: "import aiohttp" in j, (0, bake._VALIDATION_OK_MARKER)),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    bake._execute_bake(args, "soak-bake-del-success", "<startup>")

    assert "soak-bake-del-success" in fake_lease.deleted, (
        "lease must be deleted on the success exit path"
    )


def test_lease_deleted_on_validation_abort(bake, monkeypatch):
    """Lease is deleted in the finally block even when validation aborts."""
    fake_lease = _FakeIacLease()
    monkeypatch.setattr(bake, "_iac_lease", fake_lease)

    rec = _Recorder(responses=[
        (lambda j: "images describe" in j, (1, "NOT_FOUND")),
        (lambda j: "instances create" in j, (0, "created")),
        (lambda j: "test -f " in j, (0, "SOAK_READY")),
        (lambda j: "import aiohttp" in j,
         (0, f"{bake._VALIDATION_FAIL_MARKER} broken")),
    ])
    monkeypatch.setattr(bake, "_run", rec)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-del-abort", "<startup>")

    assert rc == 6, "validation abort must return exit code 6"
    assert "soak-bake-del-abort" in fake_lease.deleted, (
        "lease must be deleted on the abort/failure exit path"
    )


def test_lease_heartbeat_called_during_poll(bake, monkeypatch):
    """Heartbeat (lease refresh) is called each readiness-poll iteration."""
    fake_lease = _FakeIacLease()
    monkeypatch.setattr(bake, "_iac_lease", fake_lease)

    # Node not ready on attempt 1, ready on attempt 2.
    call_count: List[int] = [0]

    def fake_run(cmd, *, timeout_s=120.0):
        joined = " ".join(cmd)
        if "images describe" in joined:
            return 1, "NOT_FOUND"
        if "instances create" in joined:
            return 0, "created"
        if "test -f " in joined:
            call_count[0] += 1
            if call_count[0] == 1:
                return 0, "SOAK_NOT_READY"
            return 0, "SOAK_READY"
        if "tail -n 5" in joined:
            return 0, "[soak-bake] still installing"
        if "import aiohttp" in joined:
            return 0, bake._VALIDATION_OK_MARKER
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    monkeypatch.setattr(bake.time, "sleep", lambda s: None)
    args = bake.build_parser().parse_args(["--execute"])
    rc = bake._execute_bake(args, "soak-bake-heartbeat", "<startup>")

    assert rc == 0
    # At least two writes: the initial lease write + at least one heartbeat.
    writes_for_node = [
        w for w in fake_lease.written if w["node"] == "soak-bake-heartbeat"
    ]
    assert len(writes_for_node) >= 2, (
        f"expected >=2 lease writes (initial + at least 1 heartbeat), "
        f"got {len(writes_for_node)}"
    )


# --------------------------------------------------------------------------- #
# Reaper-spares-leased-node integration test.
# --------------------------------------------------------------------------- #

def test_reaper_spares_node_with_valid_baker_lease(tmp_path):
    """A node with a valid baker lease (pid=live, not expired) is NOT reaped.

    Integration test: uses the real iac_lease and a1_orphan_reaper modules
    loaded via importlib (no real gcloud calls -- injected fake lister/deleter).
    """
    iac_lease_mod = _load_iac_lease()
    reaper_mod = _load_reaper()

    node = "jarvis-soak-bake-live-lease-test"
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir(parents=True, exist_ok=True)

    # Write a valid lease owned by the current (live) process.
    iac_lease_mod.write_lease(node, "us-central1-a", os.getpid(), 3600, lease_dir)

    # Verify the lease is seen as valid by the sync helper.
    valid, reason = iac_lease_mod.is_lease_valid(node, lease_dir)
    assert valid, f"freshly written lease must be valid; got reason={reason!r}"

    reaped: List[str] = []

    import datetime as _dt

    def _make_old_instance(name):
        created = _dt.datetime.utcnow() - _dt.timedelta(seconds=1000)
        ts = created.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        return {"name": name, "zone": "us-central1-a", "creationTimestamp": ts}

    async def fake_lister(project, prefixes):
        return [_make_old_instance(node)]

    async def fake_deleter(project, node_name, zone):
        reaped.append(node_name)

    reaper = reaper_mod.OrphanReaper(
        lease_dir=lease_dir,
        project="test-project",
        zone="us-central1-a",
        boot_grace_s=0,  # no grace -- node is old enough
        dry_run=False,
        instance_lister=fake_lister,
        instance_deleter=fake_deleter,
    )
    asyncio.run(reaper.run_once())

    assert node not in reaped, (
        "OrphanReaper must NOT reap a node that has a valid live baker lease"
    )
