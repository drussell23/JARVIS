"""Tests for scripts/ignite_a1_brain.py -- the remote A1 dispatch orchestrator.

All GCP/SSH/subprocess collaborators are injected fakes -- ZERO real gcloud
calls, zero dollars, no network. Proves:

  1. --dry-run returns 0 and touches NEITHER compute-rest NOR the subprocess
     runner NOR provision_fn (zero network/subprocess side effects).
  2. The composed remote command carries the exact Piece B/C config strings.
  3. The SSH argv is built by the REUSED sovereign_iac_hypervisor._ssh_cmd
     (gcloud compute ssh <node> --project=.. --zone=.. --tunnel-through-iap
     --command <remote>).
  4. Teardown targets the resolved node + zone via
     get_compute_rest().delete_instance(node, zone=...).
  5. The provisioning env carries JARVIS_BRAIN_VM_PERSISTENT=true and a
     positive JARVIS_BRAIN_ABSOLUTE_LIFETIME_S while provision_fn runs.
  6. The up-front reaper registry is idempotent and never raises when drained
     twice.
  7. Re-review fix: an explicit --lifetime-s/JARVIS_A1_BRAIN_LIFETIME_S BELOW
     the coordination floor (soak_wall_s + boot_offset_s +
     verdict_pull_margin_s) is CLAMPED UP to the floor (with a loud warning,
     never silently honored, never rejected/crashed) -- proven regression
     for the exact reported failure: `--soak-wall-s 2400 --lifetime-s 1800`
     used to yield lifetime_s=1800 < wall=2400 ($0-killed mid-verdict).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import io
import json
import os
import pathlib
import sys
import tarfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ignite_a1_brain.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ignite_a1_brain", _SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ignite_a1_brain"] = mod
    spec.loader.exec_module(mod)
    return mod


ignite = _load_module()


# ===========================================================================
# Fakes -- no real gcloud / subprocess / network anywhere in this file.
# ===========================================================================


class ExplodingComputeRest:
    """A compute-rest stand-in that FAILS the test if ANY method is invoked.
    Used to prove --dry-run makes zero GCP calls."""

    def __getattr__(self, name: str) -> Any:
        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError(
                "compute-rest.%s() was called -- --dry-run must make ZERO "
                "network calls" % (name,)
            )

        return _boom


class RecordingRunner:
    """Records every SSH/subprocess invocation; used to prove --dry-run never
    shells out, and to script SSH responses in live-path tests."""

    def __init__(self, responses: Optional[List[Tuple[int, str]]] = None) -> None:
        self.calls: List[Tuple[Sequence[str], float]] = []
        self._responses = list(responses or [])

    def __call__(self, argv: Sequence[str], timeout_s: float) -> Tuple[int, str]:
        self.calls.append((list(argv), timeout_s))
        if self._responses:
            return self._responses.pop(0)
        return (0, "")


class FakeComputeRest:
    """A minimal async fake mirroring GCPComputeRest's public surface used by
    the orchestrator: project(), list_instances_by_label(), delete_instance()."""

    def __init__(
        self,
        *,
        project: str = "fake-project",
        instances: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._project = project
        self._instances = instances if instances is not None else []
        self.delete_calls: List[Tuple[str, Optional[str]]] = []
        self.list_calls = 0
        self.project_calls = 0

    async def project(self) -> Optional[str]:
        self.project_calls += 1
        return self._project

    async def list_instances_by_label(self, *, label_key: str, label_value: str) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return list(self._instances)

    async def delete_instance(self, name: Optional[str] = None, *, zone: Optional[str] = None) -> Tuple[bool, str]:
        """Zone-aware fake: only "deletes" (removes from `self._instances`,
        so a subsequent `list_instances_by_label` no longer reports it) when
        `zone` matches the instance's actual zone (or is None) -- mirrors
        real GCE semantics well enough to exercise the zone-mismatch
        re-delete path (MINOR #4) without a real API."""
        self.delete_calls.append((name or "", zone))
        remaining = []
        matched = False
        for inst in self._instances:
            inst_zone = str(inst.get("zone") or "").rsplit("/", 1)[-1]
            if inst.get("name") == name and (zone is None or zone == inst_zone):
                matched = True
                continue
            remaining.append(inst)
        self._instances = remaining
        if matched:
            return (True, "deleted:200")
        return (False, "not_found:404")


class FakeBlackboxTransport:
    """Stands in for IapBlackBoxTransport -- no real scp/ssh."""

    def __init__(self, *, node: str, hypervisor_args: Any) -> None:
        self.node = node
        self.args = hypervisor_args
        self.bundle_calls: List[Dict[str, Any]] = []
        self.pull_calls: List[Dict[str, Any]] = []

    def bundle_on_node(self, *, run_id: str, out_dir: str) -> Optional[Dict[str, str]]:
        self.bundle_calls.append({"run_id": run_id, "out_dir": out_dir})
        return {
            "archive": "%s/black_box_%s.tar.gz" % (out_dir, run_id),
            "sha256": "deadbeef",
            "sha_path": "%s/black_box_%s.tar.gz.sha256" % (out_dir, run_id),
        }

    def pull_archive(self, *, node_archive: str, node_sha_path: str, local_dir: str) -> Optional[str]:
        self.pull_calls.append(
            {"node_archive": node_archive, "node_sha_path": node_sha_path, "local_dir": local_dir}
        )
        return "deadbeef"

    def _scp_pull_cmd(self, node_src: str, local_dir: str) -> List[str]:
        project = getattr(self.args, "project", None)
        zone = getattr(self.args, "zone", None)
        return [
            "gcloud", "compute", "scp", "--recurse",
            "--project=%s" % (project,), "--zone=%s" % (zone,),
            "--tunnel-through-iap", "%s:%s" % (self.node, node_src), local_dir,
        ]

    def teardown(self, *, node: str) -> bool:
        return True


class FakeBlackboxTransportWithArchive:
    """Stands in for IapBlackBoxTransport where `pull_archive()` copies a REAL
    prebuilt tar.gz into `local_dir` -- so `retrieve_verdict()`'s extraction
    path has an actual tarball to open (still zero real scp/ssh; the "pull" is
    a local file copy)."""

    def __init__(
        self, *, node: str, hypervisor_args: Any, archive_src: str,
        sha256: str = "deadbeef", pull_should_succeed: bool = True,
    ) -> None:
        self.node = node
        self.args = hypervisor_args
        self._archive_src = str(archive_src)
        self._sha256 = sha256
        self._pull_should_succeed = pull_should_succeed
        self.bundle_calls: List[Dict[str, Any]] = []
        self.pull_calls: List[Dict[str, Any]] = []

    def bundle_on_node(self, *, run_id: str, out_dir: str) -> Optional[Dict[str, str]]:
        self.bundle_calls.append({"run_id": run_id, "out_dir": out_dir})
        return {
            "archive": "%s/black_box_%s.tar.gz" % (out_dir, run_id),
            "sha256": self._sha256,
            "sha_path": "%s/black_box_%s.tar.gz.sha256" % (out_dir, run_id),
        }

    def pull_archive(self, *, node_archive: str, node_sha_path: str, local_dir: str) -> Optional[str]:
        self.pull_calls.append(
            {"node_archive": node_archive, "node_sha_path": node_sha_path, "local_dir": local_dir}
        )
        if not self._pull_should_succeed:
            return None
        os.makedirs(local_dir, exist_ok=True)
        import shutil  # noqa: PLC0415 -- test-only, avoids a module-level import for one use

        local_archive = os.path.join(local_dir, os.path.basename(node_archive))
        shutil.copyfile(self._archive_src, local_archive)
        return self._sha256

    def _scp_pull_cmd(self, node_src: str, local_dir: str) -> List[str]:
        return ["gcloud", "compute", "scp", node_src, local_dir]

    def teardown(self, *, node: str) -> bool:
        return True


def _make_verdict_tarball(path: pathlib.Path, *, verdict: Dict[str, Any],
                           include_verdict: bool = True) -> pathlib.Path:
    """Build a REAL tiny tar.gz at `path` containing `a1_verdict.json` (unless
    `include_verdict` is False, mirroring a1_black_box.py's fail-soft-absent
    case) -- no real subprocess/network."""
    with tarfile.open(path, "w:gz") as tf:
        if include_verdict:
            data = json.dumps(verdict).encode("utf-8")
            info = tarfile.TarInfo(name="a1_verdict.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        filler = b"MANIFEST placeholder\n"
        info2 = tarfile.TarInfo(name="MANIFEST.txt")
        info2.size = len(filler)
        tf.addfile(info2, io.BytesIO(filler))
    return path


def _real_hypervisor_loader():
    """Loads the REAL sovereign_iac_hypervisor.py -- safe because `_ssh_cmd` /
    `_delete_node_cmd` are PURE argv builders (no subprocess call inside
    them); only `_run` shells out, and we never call the real `_run` in these
    tests (the injected `runner` always intercepts it first)."""
    hyper_path = _REPO_ROOT / "scripts" / "sovereign_iac_hypervisor.py"
    spec = importlib.util.spec_from_file_location("sovereign_iac_hypervisor", hyper_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sovereign_iac_hypervisor", mod)
    spec.loader.exec_module(mod)
    return mod


async def _exploding_provision_fn(*, node_name: str) -> Tuple[bool, str]:
    raise AssertionError("provision_fn was called -- --dry-run must never provision")


def _extract_root_wrapped_payload(remote: str) -> str:
    """Decode the base64 payload out of a `_wrap_as_root`-composed remote
    command string (`printf %s <b64> | base64 -d | sudo bash`) -- used to
    prove the ROOT-WRAPPED command actually carries the intended inner
    script, not just that it LOOKS root-wrapped."""
    prefix, suffix = "printf %s ", " | base64 -d | sudo bash"
    assert remote.startswith(prefix), remote
    assert remote.endswith(suffix), remote
    encoded = remote[len(prefix): -len(suffix)]
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


def _make_ignition(**overrides: Any):
    kwargs: Dict[str, Any] = dict(
        node_name="a1-brain-test",
        project="",
        zone="",
        # Comfortably above the coordination floor implied by the default
        # soak_wall_s (1800) + default boot_offset_s (180) + default
        # verdict_pull_margin_s (180) = 2160 -- so this fixture's default
        # explicit lifetime is honored as-is (not clamped) unless a test
        # deliberately overrides soak_wall_s/lifetime_s to probe the clamp.
        lifetime_s=3600,
        seed=3,
        remote_run_dir="/opt/trinity/jarvis/a1_iso_runs/a1-brain-test",
        local_out_root="/tmp/a1_brain_test_out",
        verbose=False,
        hypervisor_loader=_real_hypervisor_loader,
        blackbox_transport_factory=lambda *, node, hypervisor_args: FakeBlackboxTransport(
            node=node, hypervisor_args=hypervisor_args),
        compute_rest_factory=lambda: FakeComputeRest(),
        provision_fn=None,
        runner=None,
        monitor_poll_s=0.01,
        monitor_max_s=0.05,
    )
    kwargs.update(overrides)
    return ignite.A1BrainIgnition(**kwargs)


# ===========================================================================
# 1. --dry-run: zero network/subprocess side effects, exit 0.
# ===========================================================================


def test_dry_run_returns_zero_and_touches_nothing(capsys):
    exploding_rest = ExplodingComputeRest()
    runner = RecordingRunner()

    ignition = _make_ignition(
        dry_run=True,
        project="demo-project",
        zone="us-central1-a",
        compute_rest_factory=lambda: exploding_rest,
        provision_fn=_exploding_provision_fn,
        runner=runner,
    )

    rc = asyncio.run(ignition.run())

    assert rc == 0
    assert runner.calls == []  # no subprocess/SSH ever executed
    out = capsys.readouterr().out
    assert "PLAN (dry-run, zero side effects)" in out
    assert "DRY RUN: zero network" in out


def test_dry_run_via_main_never_calls_asyncio_run(monkeypatch, capsys):
    """main() must short-circuit --dry-run BEFORE asyncio.run() -- proves no
    coroutine can accidentally touch the network even by mistake."""
    called = {"asyncio_run": False}
    real_run = asyncio.run

    def _spy_run(*a: Any, **kw: Any) -> Any:
        called["asyncio_run"] = True
        return real_run(*a, **kw)

    monkeypatch.setattr(ignite.asyncio, "run", _spy_run)
    rc = ignite.main(["--dry-run", "--node-name", "a1-brain-cli-test"])
    assert rc == 0
    assert called["asyncio_run"] is False
    out = capsys.readouterr().out
    assert "a1-brain-cli-test" in out


# ===========================================================================
# 2. Composed remote command carries the exact Piece B/C config strings.
# ===========================================================================


def test_remote_command_contains_piece_b_and_c_config():
    remote_cmd = ignite._compose_remote_command(
        remote_run_dir="/opt/trinity/jarvis/a1_iso_runs/x", seed=0, verbose=True,
        soak_wall_s=1800)

    assert "JARVIS_CHAOS_TARGET_DIRS=backend/core/ouroboros/a1_ignition_vector" in remote_cmd
    assert "autonomy.*" in remote_cmd
    assert "JARVIS_BRAIN_OUTBOUND_TOPICS=" in remote_cmd
    assert "actuation.*" in remote_cmd
    assert "telemetry.posture.*" in remote_cmd
    assert "isomorphic_a1_local.py" in remote_cmd
    assert "--mode process" in remote_cmd
    assert "OUROBOROS_BATTLE_HEADLESS=1" in remote_cmd
    assert "OUROBOROS_BATTLE_MAX_WALL_SECONDS=1800" in remote_cmd


def test_plan_remote_cmd_matches_composer():
    ignition = _make_ignition(dry_run=True, project="p", zone="z")
    plan = ignition.build_plan()
    expected = ignite._compose_remote_command(
        remote_run_dir=ignition.remote_run_dir, seed=ignition.seed, verbose=ignition.verbose,
        soak_wall_s=ignition.soak_wall_s)
    assert plan.remote_cmd == expected
    assert "autonomy.*" in plan.remote_cmd
    assert "backend/core/ouroboros/a1_ignition_vector" in plan.remote_cmd


# ===========================================================================
# 2b. Coordinated soak-wall <-> node-lifetime budget (review fix, IMPORTANT #2).
# ===========================================================================


def test_remote_command_carries_coordinated_wall_seconds():
    ignition = _make_ignition(dry_run=True, project="p", zone="z", soak_wall_s=900)
    plan = ignition.build_plan()
    assert "OUROBOROS_BATTLE_MAX_WALL_SECONDS=900" in plan.remote_cmd
    assert plan.soak_wall_s == 900


def test_lifetime_is_derived_from_soak_wall_plus_margins():
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z",
        soak_wall_s=1000, lifetime_s=None,
    )
    assert ignition.lifetime_s == (
        1000 + ignition.boot_offset_s + ignition.post_wall_margin_s
        + ignition.verdict_pull_margin_s)
    assert ignition.lifetime_s >= (
        ignition.soak_wall_s + ignition.boot_offset_s
        + ignition.post_wall_margin_s + ignition.verdict_pull_margin_s
    )


def test_bumping_soak_wall_grows_lifetime_no_drift():
    small = _make_ignition(dry_run=True, project="p", zone="z", soak_wall_s=600, lifetime_s=None)
    large = _make_ignition(dry_run=True, project="p", zone="z", soak_wall_s=2400, lifetime_s=None)
    assert large.lifetime_s > small.lifetime_s
    assert large.lifetime_s - small.lifetime_s == 2400 - 600


def test_explicit_lifetime_s_still_overrides_derivation():
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z", soak_wall_s=100, lifetime_s=9999)
    assert ignition.lifetime_s == 9999


def test_monitor_stop_leaves_verdict_pull_headroom_before_lifetime():
    """monitor_max_s (the default monitor-stop deadline) must leave >=
    verdict_pull_margin_s of headroom before lifetime_s -- i.e.
    monitor_max_s + verdict_pull_margin_s <= lifetime_s."""
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z", soak_wall_s=1800, lifetime_s=None,
        monitor_max_s=None,
    )
    assert ignition._monitor_max_s + ignition.verdict_pull_margin_s <= ignition.lifetime_s
    assert ignition.soak_wall_s < ignition._monitor_max_s < ignition.lifetime_s


def test_resolve_lifetime_s_helper_direct():
    """Derived (no-explicit) path: unchanged by the clamp fix."""
    assert ignite._resolve_lifetime_s(
        explicit=None, soak_wall_s=500, boot_offset_s=100, verdict_pull_margin_s=50,
    ) == 650


# ===========================================================================
# 2c. Re-review fix: explicit lifetime_s below the coordination floor is
#     CLAMPED UP (never silently honored, never rejected/crashed) --
#     `soak_wall_s < monitor_stop < lifetime_s` must hold after resolution.
# ===========================================================================


def test_resolve_lifetime_s_below_floor_is_clamped_up_with_warning(capsys):
    """The exact reported failure class, at the pure-function level: an
    explicit lifetime below soak_wall_s + boot_offset_s +
    verdict_pull_margin_s (== 650 here) must be clamped UP to that floor,
    not honored verbatim -- and a loud warning must be emitted (never a
    silent override)."""
    result = ignite._resolve_lifetime_s(
        explicit=42, soak_wall_s=500, boot_offset_s=100, verdict_pull_margin_s=50,
    )
    assert result == 650  # clamped to the floor, NOT the explicit 42
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "--lifetime-s" in out
    assert "650" in out


def test_resolve_lifetime_s_above_floor_is_honored(capsys):
    """An explicit lifetime AT or ABOVE the floor is honored as-is (an
    operator may deliberately extend it) -- and no clamp warning fires."""
    result = ignite._resolve_lifetime_s(
        explicit=9999, soak_wall_s=500, boot_offset_s=100, verdict_pull_margin_s=50,
    )
    assert result == 9999
    out = capsys.readouterr().out
    assert "WARN" not in out

    # Exactly AT the floor is also honored (not clamped past itself).
    at_floor = ignite._resolve_lifetime_s(
        explicit=650, soak_wall_s=500, boot_offset_s=100, verdict_pull_margin_s=50,
    )
    assert at_floor == 650


def test_resolve_lifetime_s_env_var_below_floor_is_clamped(monkeypatch, capsys):
    """The env-var path (JARVIS_A1_BRAIN_LIFETIME_S) gets the same clamp
    treatment as the explicit --lifetime-s CLI path."""
    monkeypatch.setenv("JARVIS_A1_BRAIN_LIFETIME_S", "10")
    result = ignite._resolve_lifetime_s(
        explicit=None, soak_wall_s=500, boot_offset_s=100, verdict_pull_margin_s=50,
    )
    assert result == 650
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "JARVIS_A1_BRAIN_LIFETIME_S" in out


def test_ignition_clamps_reported_regression_case(capsys):
    """End-to-end regression pin for the EXACT reported failure:
    `--soak-wall-s 2400 --lifetime-s 1800` used to yield lifetime_s=1800 <
    wall=2400 -- a $0-kill mid-verdict. It must now clamp lifetime_s up to
    the floor (2400 + boot 180 + post_wall 300 + verdict 180 = 3060) and the
    full ordering invariant (soak_wall_s < monitor_stop < lifetime_s) must hold."""
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z", soak_wall_s=2400, lifetime_s=1800,
        monitor_max_s=None,  # let monitor_max_s DERIVE from lifetime_s (fixture
        # default overrides it to a tiny test value otherwise, which would
        # trivially satisfy any ordering check).
    )
    expected_floor = (2400 + ignition.boot_offset_s
                      + ignition.post_wall_margin_s + ignition.verdict_pull_margin_s)
    assert expected_floor == 3060
    assert ignition.lifetime_s == expected_floor
    assert ignition.lifetime_s >= 2400  # never $0-killed before the soak wall

    out = capsys.readouterr().out
    assert "WARN" in out

    # Full ordering invariant, same shape as
    # test_monitor_stop_leaves_verdict_pull_headroom_before_lifetime.
    assert ignition.soak_wall_s < ignition._monitor_max_s < ignition.lifetime_s
    assert ignition._monitor_max_s + ignition.verdict_pull_margin_s <= ignition.lifetime_s


def test_ignition_honors_explicit_lifetime_above_floor():
    """An explicit lifetime comfortably above the floor is honored as-is at
    the A1BrainIgnition level too (operator deliberately extending it)."""
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z", soak_wall_s=100, lifetime_s=9999,
        monitor_max_s=None,  # derive, see comment above
    )
    assert ignition.lifetime_s == 9999
    assert ignition.soak_wall_s < ignition._monitor_max_s < ignition.lifetime_s


# ===========================================================================
# 3. SSH argv shape -- built by the REUSED hyper._ssh_cmd.
# ===========================================================================


def test_ssh_argv_built_by_reused_hypervisor_ssh_cmd():
    ignition = _make_ignition(dry_run=True, project="demo-project", zone="us-central1-a")
    plan = ignition.build_plan()

    argv = plan.ssh_argv
    assert argv[:3] == ["gcloud", "compute", "ssh"]
    assert argv[3] == ignition.node_name
    assert "--project=demo-project" in argv
    assert "--zone=us-central1-a" in argv
    assert "--tunnel-through-iap" in argv
    assert argv[-2] == "--command"
    # Bug 3: the remote payload is now ROOT-WRAPPED (base64 | sudo bash) --
    # decode it to confirm the real isomorphic_a1_local.py invocation (the
    # detached dispatch wrapper) survives the round trip.
    remote = argv[-1]
    assert "sudo bash" in remote
    assert "base64 -d" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert "isomorphic_a1_local.py" in decoded


def test_ssh_exec_uses_ssh_cmd_and_injected_runner():
    runner = RecordingRunner(responses=[(0, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")])
    ignition = _make_ignition(project="p", zone="z", runner=runner)

    rc, out = ignition._ssh_exec("git -C /opt/trinity/jarvis rev-parse HEAD", timeout_s=5.0)

    assert rc == 0
    assert "deadbeef" in out
    assert len(runner.calls) == 1
    argv, timeout_s = runner.calls[0]
    assert argv[:3] == ["gcloud", "compute", "ssh"]
    assert "--project=p" in argv
    assert "--zone=z" in argv
    assert timeout_s == 5.0


# ===========================================================================
# Bug 2 (live-fire): resolve_actual_zone must pick the RUNNING winner, not a
# being-deleted loser -- and must PREFER the zone provision() parsed straight
# out of the create-time detail string over re-listing at all.
# ===========================================================================


def test_status_rank_orders_running_before_stopping_and_terminated():
    assert ignite._status_rank("RUNNING") < ignite._status_rank("STOPPING")
    assert ignite._status_rank("RUNNING") < ignite._status_rank("TERMINATED")
    assert ignite._status_rank("PROVISIONING") < ignite._status_rank("STOPPING")
    # Unknown/missing status never raises and ranks in the middle (neither
    # preferred over a confirmed RUNNING match nor penalized below a
    # confirmed STOPPING/TERMINATED one).
    assert ignite._status_rank(None) == ignite._status_rank("")
    assert ignite._status_rank("RUNNING") < ignite._status_rank(None)
    assert ignite._status_rank(None) < ignite._status_rank("TERMINATED")


def test_parse_provision_zone_extracts_zone_from_create_instance_detail():
    """gcp_compute_rest.GCPComputeRest._insert_in_zone's winner reports
    "created:zone=<z>:mode=<m>" -- provision_brain forwards that detail
    string verbatim as the (ok, detail) return."""
    assert ignite._parse_provision_zone(
        "created:zone=us-central1-b:mode=spot") == "us-central1-b"
    assert ignite._parse_provision_zone(
        "created:zone=us-east4-a:mode=on_demand") == "us-east4-a"
    assert ignite._parse_provision_zone("ok") is None
    assert ignite._parse_provision_zone("") is None
    assert ignite._parse_provision_zone(None) is None  # fail-soft, never raises


def test_resolve_actual_zone_prefers_provision_reported_zone_over_relisting():
    """Bug 2 (primary source of truth): when provision() already parsed an
    authoritative zone out of the create-time detail string,
    resolve_actual_zone() must use it DIRECTLY -- list_instances_by_label is
    never called at all."""
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest(instances=[
        # A STOPPING same-named loser sitting in a DIFFERENT zone than the
        # real winner -- proves the provision-zone path never even consults
        # this list (a naive list-based resolver could still pick this).
        {"name": "a1-brain-test", "zone": "projects/p/zones/us-central1-a", "status": "STOPPING"},
    ])

    async def _provision_fn(*, node_name: str) -> Tuple[bool, str]:
        return True, "created:zone=us-central1-b:mode=spot"

    ignition = _make_ignition(
        compute_rest_factory=lambda: rest, zone="", provision_fn=_provision_fn)
    asyncio.run(ignition.provision())

    zone = asyncio.run(ignition.resolve_actual_zone())

    assert zone == "us-central1-b"
    assert ignition.zone == "us-central1-b"
    assert rest.list_calls == 0  # never re-listed
    ignite._ACTIVE_IGNITIONS.clear()


def test_resolve_actual_zone_prefers_running_over_stopping_without_provision_zone():
    """Bug 2 (fallback path): without a provision-reported zone, the exact
    live-fire race -- a scatter-gather LOSER (STOPPING, mid-reap) in zone A
    briefly coexists with the RUNNING winner in zone B -- must resolve to
    the RUNNING zone, even though the STOPPING entry is listed FIRST (proves
    rank-based selection, not first-match)."""
    rest = FakeComputeRest(instances=[
        {"name": "a1-brain-test", "zone": "projects/p/zones/us-central1-a", "status": "STOPPING"},
        {"name": "a1-brain-test", "zone": "projects/p/zones/us-central1-b", "status": "RUNNING"},
    ])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="")
    assert ignition._provision_zone is None  # exercises the list-fallback path

    zone = asyncio.run(ignition.resolve_actual_zone())

    assert zone == "us-central1-b"
    assert ignition.zone == "us-central1-b"
    assert rest.list_calls == 1


def test_resolve_actual_zone_handles_missing_status_field_without_raising():
    rest = FakeComputeRest(instances=[
        {"name": "a1-brain-test", "zone": "projects/p/zones/us-central1-a"},  # no status key
    ])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="")

    zone = asyncio.run(ignition.resolve_actual_zone())

    assert zone == "us-central1-a"


# ===========================================================================
# Bug 3 (live-fire): remote commands must run as ROOT. /opt/trinity/jarvis is
# ROOT-owned (golden-image bake; the boot-time git-pull runs as root) -- the
# SSH OS-Login user can neither `git rev-parse` (dubious ownership) nor
# `mkdir`/write under it. Every node-side command routes through
# `_wrap_as_root` (base64 | sudo bash -- one clean layer of quoting).
# ===========================================================================


def test_wrap_as_root_round_trips_the_inner_command():
    inner = "echo hello; mkdir -p /opt/trinity/jarvis/x && echo done"
    wrapped = ignite._wrap_as_root(inner)

    assert wrapped.startswith("printf %s ")
    assert "sudo bash" in wrapped
    assert "base64 -d" in wrapped
    assert _extract_root_wrapped_payload(wrapped) == inner


def test_wrap_as_root_handles_quotes_and_pipes_in_the_inner_command():
    """The whole point of the base64 pattern is avoiding nested-quote hell --
    prove a payload FULL of quotes/pipes/ampersands survives the round trip
    byte-for-byte."""
    inner = (
        "mkdir -p '/opt/trinity/jarvis/a b' && nohup bash -c 'echo \"hi\" | cat' "
        "> /tmp/x.log 2>&1 < /dev/null & disown; echo DISPATCH_STARTED"
    )
    wrapped = ignite._wrap_as_root(inner)
    assert _extract_root_wrapped_payload(wrapped) == inner


def test_compose_verify_head_command_adds_safe_directory_exception():
    inner = ignite._compose_verify_head_command()
    assert "safe.directory" in inner
    assert "/opt/trinity/jarvis" in inner
    assert "git -C /opt/trinity/jarvis rev-parse HEAD" in inner


def test_verify_head_sends_root_wrapped_command():
    runner = RecordingRunner(responses=[(0, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")])
    ignition = _make_ignition(project="p", zone="z", runner=runner)

    sha = asyncio.run(ignition.verify_head())

    assert sha == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert len(runner.calls) == 1
    argv, _timeout_s = runner.calls[0]
    remote = argv[-1]
    assert "sudo bash" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert "safe.directory" in decoded
    assert "git -C /opt/trinity/jarvis rev-parse HEAD" in decoded


def test_dispatch_sends_root_wrapped_command():
    runner = RecordingRunner(responses=[(0, "DISPATCH_STARTED\n")])
    ignition = _make_ignition(project="p", zone="z", runner=runner)

    ok = asyncio.run(ignition.dispatch())

    assert ok is True
    assert len(runner.calls) == 1
    argv, _timeout_s = runner.calls[0]
    remote = argv[-1]
    assert "sudo bash" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert "mkdir -p" in decoded
    assert "isomorphic_a1_local.py" in decoded
    assert "DISPATCH_STARTED" in decoded


def test_monitor_poll_command_is_root_wrapped():
    """The done-file/log-file live under the root-owned run dir -- the poll
    command must ALSO run as root (the SSH user may lack read access)."""
    runner = RecordingRunner(responses=[(0, "__IGNITE_DONE__\nDONE_RC=0\n")])
    ignition = _make_ignition(project="p", zone="z", runner=runner,
                              monitor_poll_s=0.01, monitor_max_s=5.0)

    result = asyncio.run(ignition.monitor())

    assert result["completed"] is True
    assert len(runner.calls) == 1
    argv, _timeout_s = runner.calls[0]
    remote = argv[-1]
    assert "sudo bash" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert "ignite_dispatch.done" in decoded


def test_discover_verdict_path_is_root_wrapped():
    runner = RecordingRunner(responses=[
        (0, "/opt/trinity/jarvis/a1_iso_runs/a1-brain-test/run1/a1_verdict.json\n"),
    ])
    ignition = _make_ignition(project="p", zone="z", runner=runner)

    path = ignition._discover_verdict_path()

    assert path == "/opt/trinity/jarvis/a1_iso_runs/a1-brain-test/run1/a1_verdict.json"
    argv, _timeout_s = runner.calls[0]
    remote = argv[-1]
    assert "sudo bash" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert "find" in decoded
    assert "a1_verdict.json" in decoded


def test_fetch_verdict_json_is_root_wrapped():
    verdict = {"proven": True, "criteria": {}}
    runner = RecordingRunner(responses=[(0, json.dumps(verdict))])
    ignition = _make_ignition(project="p", zone="z", runner=runner)

    result = ignition._fetch_verdict_json("/opt/trinity/jarvis/x/a1_verdict.json")

    assert result == verdict
    argv, _timeout_s = runner.calls[0]
    remote = argv[-1]
    assert "sudo bash" in remote
    decoded = _extract_root_wrapped_payload(remote)
    assert decoded == "cat /opt/trinity/jarvis/x/a1_verdict.json"


def test_dry_run_plan_shows_root_wrapped_commands_and_all_zone_sweep(capsys):
    """--dry-run must show the ROOT-WRAPPED commands (Bug 3) and confirm the
    all-zone teardown sweep (Bug 1) -- both fixes must be legible in the plan
    preview, not just in the live-path code."""
    ignition = _make_ignition(dry_run=True, project="demo-project", zone="us-central1-a")
    plan = ignition.build_plan()
    ignition.print_plan(plan)
    out = capsys.readouterr().out

    assert "root-wrapped" in out.lower()
    assert "sudo bash" in out
    assert "base64 -d" in out
    assert "ALL-ZONE SWEEP" in out
    assert "us-central1-a" in out  # the pre-create zone leads the printed chain

    # The plan dataclass itself carries the root-wrapped forms + zone list.
    assert plan.verify_head_cmd.startswith("printf %s ")
    assert _extract_root_wrapped_payload(plan.verify_head_cmd) == (
        ignite._compose_verify_head_command())
    assert plan.dispatch_wrapper_root_cmd.startswith("printf %s ")
    assert plan.teardown_zones  # non-empty
    assert plan.teardown_zones[0] == "us-central1-a"


def test_dry_run_still_zero_side_effect_with_root_wrapped_commands(capsys):
    """Re-confirms the --dry-run zero-side-effect guarantee holds after the
    Bug 3 root-wrapping rewrite (no SSH/subprocess call is ever made on the
    dry-run path -- only the plan STRING preview changed)."""
    exploding_rest = ExplodingComputeRest()
    runner = RecordingRunner()

    ignition = _make_ignition(
        dry_run=True, project="demo-project", zone="us-central1-a",
        compute_rest_factory=lambda: exploding_rest,
        provision_fn=_exploding_provision_fn,
        runner=runner,
    )

    rc = asyncio.run(ignition.run())

    assert rc == 0
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "DRY RUN: zero network" in out


# ===========================================================================
# 4. Teardown targets the resolved node + zone.
# ===========================================================================


def test_teardown_deletes_resolved_node_and_zone(monkeypatch):
    """Bug 1 (live-fire): teardown's explicit layer-1 delete sweeps EVERY zone
    in the candidate chain -- not just the one zone resolve_actual_zone()
    picked. Constrain the chain to a small deterministic pair via the env
    override so the assertion doesn't depend on the full default matrix."""
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-east1-b,us-central1-a")
    rest = FakeComputeRest(instances=[{"name": "a1-brain-test", "zone": "projects/p/zones/us-east1-b"}])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="")

    asyncio.run(ignition.resolve_actual_zone())
    assert ignition.zone == "us-east1-b"

    result = asyncio.run(ignition.teardown())

    assert result["deleted"] is True
    # Every zone in the (constrained) chain was attempted, in order.
    assert rest.delete_calls == [
        ("a1-brain-test", "us-east1-b"),
        ("a1-brain-test", "us-central1-a"),
    ]
    assert result["zero_cost"] is True


def test_teardown_reissues_delete_on_zone_mismatch(monkeypatch):
    """MINOR #4 (retained under Bug 1): when the all-zone label-based verify
    list detects the node LEAKED in a zone OUTSIDE the swept candidate chain
    (zone mismatch/drift), teardown() must RE-ISSUE delete_instance with the
    discovered zone rather than just logging LEAK -- the $0-verify is the
    final backstop for a zone the chain sweep didn't cover."""
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-central1-a,us-central1-b")
    rest = FakeComputeRest(
        instances=[{"name": "a1-brain-test", "zone": "projects/p/zones/us-east1-b"}])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="us-central1-a")

    result = asyncio.run(ignition.teardown())

    # The chain sweep tried both configured zones -- neither matches the
    # instance's real zone (us-east1-b), so both come back not_found.
    assert rest.delete_calls[0] == ("a1-brain-test", "us-central1-a")
    assert rest.delete_calls[1] == ("a1-brain-test", "us-central1-b")
    # The $0-verify then discovers the real zone and RE-ISSUES the delete.
    assert rest.delete_calls[-1] == ("a1-brain-test", "us-east1-b")
    assert len(rest.delete_calls) == 3
    # Re-delete succeeded (FakeComputeRest always returns ok=True) -> no leak.
    assert result["zero_cost"] is True


def test_teardown_no_reissue_when_verify_list_clean(monkeypatch):
    """No leaked instance in the verify list -> no re-delete is issued (but
    the full constrained chain is still swept)."""
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-central1-a,us-central1-b")
    rest = FakeComputeRest(instances=[])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="us-central1-a")

    result = asyncio.run(ignition.teardown())

    assert len(rest.delete_calls) == 2  # the full (constrained) chain sweep
    assert result["zero_cost"] is True


# ===========================================================================
# Bug 1 (live-fire, MONEY-CRITICAL): teardown + the signal-path reaper must
# both sweep EVERY zone in the candidate chain, unconditionally, idempotent +
# never-raising -- proven directly against `_reap_ignitions_async` /
# `_reap_ignitions` (the sync wrapper the SIGINT/SIGTERM handler drains) and
# against `A1BrainIgnition.teardown()`'s explicit layer-1 sweep.
# ===========================================================================


def test_teardown_sweeps_full_candidate_zone_chain_before_any_match(monkeypatch):
    """Even when NOTHING matches in ANY swept zone, every zone in the chain
    is still attempted (idempotent 404s) -- the sweep never short-circuits
    early just because earlier zones came back not-found."""
    monkeypatch.setenv(
        "JARVIS_GCP_ZONE_FALLBACK", "us-central1-a,us-central1-b,us-central1-c")
    rest = FakeComputeRest(instances=[])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="us-central1-a")

    result = asyncio.run(ignition.teardown())

    assert rest.delete_calls == [
        ("a1-brain-test", "us-central1-a"),
        ("a1-brain-test", "us-central1-b"),
        ("a1-brain-test", "us-central1-c"),
    ]
    assert result["deleted"] is False  # nothing actually matched anywhere
    assert result["zero_cost"] is True  # but the $0-verify still confirms clean


def test_reap_ignitions_sweeps_full_zone_chain_for_registered_node():
    """`_register_ignition` now takes the FULL candidate chain (as
    `provision()` computes it up front) -- draining the registry must issue
    `delete_instance` for the node across EVERY zone in that chain."""
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    chain = ["us-central1-b", "us-central1-a", "us-central1-c", "us-central1-f"]
    ignite._register_ignition(node="a1-brain-chain-test", zones=chain,
                              compute_rest_factory=lambda: rest)

    asyncio.run(ignite._reap_ignitions_async())

    assert ignite._ACTIVE_IGNITIONS == []
    assert rest.delete_calls == [("a1-brain-chain-test", z) for z in chain]


def test_reap_ignitions_never_raises_when_one_zone_explodes():
    """A single zone's delete_instance raising must NOT abort the sweep of
    the remaining zones in the chain -- proves the per-zone try/except."""
    ignite._ACTIVE_IGNITIONS.clear()

    class HalfBoomComputeRest:
        def __init__(self) -> None:
            self.delete_calls: List[Tuple[str, Optional[str]]] = []

        async def delete_instance(self, name: Optional[str] = None, *, zone: Optional[str] = None):
            self.delete_calls.append((name or "", zone))
            if zone == "us-central1-b":
                raise RuntimeError("simulated GCP failure in this zone only")
            return (True, "deleted:404")

    rest = HalfBoomComputeRest()
    chain = ["us-central1-a", "us-central1-b", "us-central1-c"]
    ignite._register_ignition(node="a1-brain-half-boom", zones=chain,
                              compute_rest_factory=lambda: rest)

    asyncio.run(ignite._reap_ignitions_async())

    assert ignite._ACTIVE_IGNITIONS == []
    # ALL three zones were attempted despite the middle one raising.
    assert rest.delete_calls == [
        ("a1-brain-half-boom", "us-central1-a"),
        ("a1-brain-half-boom", "us-central1-b"),
        ("a1-brain-half-boom", "us-central1-c"),
    ]


def test_signal_path_sync_reaper_sweeps_full_zone_chain():
    """The SYNC reaper (`_reap_ignitions`) is what the SIGINT/SIGTERM
    `_handler` and `atexit` actually drain -- this is the exact live-fire
    regression: the signal path must ALSO sweep every zone in the chain, not
    just whichever single zone resolve_actual_zone() happened to pick."""
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    chain = ["us-central1-b", "us-central1-a"]
    ignite._register_ignition(node="a1-brain-signal-test", zones=chain,
                              compute_rest_factory=lambda: rest)

    ignite._reap_ignitions()  # the sync path the signal handler calls

    assert ignite._ACTIVE_IGNITIONS == []
    assert rest.delete_calls == [
        ("a1-brain-signal-test", "us-central1-b"),
        ("a1-brain-signal-test", "us-central1-a"),
    ]


def test_teardown_argv_preview_targets_node_and_zone():
    ignition = _make_ignition(dry_run=True, project="demo-project", zone="us-central1-a")
    plan = ignition.build_plan()

    assert plan.teardown_argv[:4] == ["gcloud", "compute", "instances", "delete"]
    assert ignition.node_name in plan.teardown_argv
    assert any("--project=demo-project" in a for a in plan.teardown_argv)
    assert any("--zone=us-central1-a" in a for a in plan.teardown_argv)


# ===========================================================================
# 5. Provisioning env carries JARVIS_BRAIN_VM_PERSISTENT=true + a positive
#    JARVIS_BRAIN_ABSOLUTE_LIFETIME_S while provision_fn executes.
# ===========================================================================


def test_provision_env_carries_persistent_and_lifetime(monkeypatch):
    import os

    monkeypatch.delenv("JARVIS_BRAIN_VM_PERSISTENT", raising=False)
    monkeypatch.delenv("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", raising=False)

    captured: Dict[str, str] = {}

    async def _capture_provision_fn(*, node_name: str) -> Tuple[bool, str]:
        captured["persistent"] = os.environ.get("JARVIS_BRAIN_VM_PERSISTENT", "")
        captured["lifetime"] = os.environ.get("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S", "")
        return True, "ok"

    # soak_wall_s=100 keeps floor (100+180+180=460) below the explicit 900
    # so this test's lifetime_s stays un-clamped -- its purpose is proving
    # the env-var wiring, not the clamp (see the dedicated clamp tests).
    ignition = _make_ignition(
        lifetime_s=900, soak_wall_s=100, provision_fn=_capture_provision_fn)
    ok, detail = asyncio.run(ignition.provision())

    assert ok is True
    assert captured["persistent"] == "true"
    assert int(captured["lifetime"]) > 0
    assert captured["lifetime"] == "900"

    # Env is restored afterward (no permanent pollution).
    assert os.environ.get("JARVIS_BRAIN_VM_PERSISTENT") is None
    assert os.environ.get("JARVIS_BRAIN_ABSOLUTE_LIFETIME_S") is None


def test_provision_registers_node_up_front_for_teardown(monkeypatch):
    """Bug 1: provision() registers with the FULL candidate zone chain, not a
    single zone -- proven by constraining the chain via env override and
    checking the registered entry's `zones` list matches it exactly."""
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-central1-b,us-central1-a")
    ignite._ACTIVE_IGNITIONS.clear()

    async def _ok_provision_fn(*, node_name: str) -> Tuple[bool, str]:
        # Assert the registration already happened BEFORE the "create" call,
        # and that it carries the FULL chain (not a single zone).
        entry = next((e for e in ignite._ACTIVE_IGNITIONS if e["node"] == node_name), None)
        assert entry is not None
        assert entry["zones"] == ["us-central1-b", "us-central1-a"]
        return True, "created:zone=us-central1-b:mode=spot"

    ignition = _make_ignition(node_name="a1-brain-upfront", provision_fn=_ok_provision_fn)
    ok, _ = asyncio.run(ignition.provision())
    assert ok is True
    entry = next(e for e in ignite._ACTIVE_IGNITIONS if e["node"] == "a1-brain-upfront")
    assert entry["zones"] == ["us-central1-b", "us-central1-a"]
    ignite._ACTIVE_IGNITIONS.clear()


# ===========================================================================
# 6. Reaper registry: idempotent + never raises when drained twice. Bug 1:
#    registration now carries the FULL candidate zone chain (`zones=[...]`),
#    and draining sweeps EVERY zone for each registered node.
# ===========================================================================


def test_reap_registry_idempotent_and_never_raises():
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    ignite._register_ignition(node="a1-brain-reap-1", zones=["us-central1-a", "us-central1-b"],
                              compute_rest_factory=lambda: rest)
    ignite._register_ignition(node="a1-brain-reap-2", zones=[None],
                              compute_rest_factory=lambda: rest)
    assert len(ignite._ACTIVE_IGNITIONS) == 2

    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []
    assert sorted(rest.delete_calls) == sorted([
        ("a1-brain-reap-1", "us-central1-a"),
        ("a1-brain-reap-1", "us-central1-b"),
        ("a1-brain-reap-2", None),
    ])

    # Second drain: registry already empty -- must be a silent no-op, no raise.
    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []
    assert len(rest.delete_calls) == 3  # unchanged -- nothing re-deleted


def test_reap_registry_never_raises_on_deleter_exception():
    ignite._ACTIVE_IGNITIONS.clear()

    class BoomComputeRest:
        async def delete_instance(self, name: Optional[str] = None, *, zone: Optional[str] = None):
            raise RuntimeError("simulated GCP failure")

    ignite._register_ignition(node="a1-brain-boom", zones=[None, "us-central1-a"],
                              compute_rest_factory=lambda: BoomComputeRest())

    # Must not raise, despite the deleter blowing up on EVERY zone.
    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []


def test_reap_sync_wrapper_idempotent_and_never_raises():
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    ignite._register_ignition(node="a1-brain-sync-reap", zones=["us-central1-a"],
                              compute_rest_factory=lambda: rest)

    ignite._reap_ignitions()  # drains via the sync path (no running loop here)
    assert ignite._ACTIVE_IGNITIONS == []
    assert rest.delete_calls == [("a1-brain-sync-reap", "us-central1-a")]

    # Second call: empty registry, must short-circuit cleanly.
    ignite._reap_ignitions()
    assert ignite._ACTIVE_IGNITIONS == []


# ===========================================================================
# Bonus: full plan dataclass sanity + absolute-lifetime is printed.
# ===========================================================================


def test_plan_includes_absolute_lifetime_and_provision_env():
    # Explicit lifetime ABOVE the coordination floor (soak_wall 1800 + boot 180 +
    # post_wall 300 + verdict 180 = 2460) so it is honored as-is, not clamped up.
    ignition = _make_ignition(dry_run=True, lifetime_s=3000, project="p", zone="z")
    plan = ignition.build_plan()
    assert plan.lifetime_s == 3000
    assert plan.provision_env["JARVIS_BRAIN_VM_PERSISTENT"] == "true"
    assert plan.provision_env["JARVIS_BRAIN_ABSOLUTE_LIFETIME_S"] == "3000"


# ===========================================================================
# 7. Mandate 3 -- retrieve_verdict() is 100% via IapBlackBoxTransport.
#
#    AUTHORITATIVE: a checksum-verified Black-Box pull whose local tarball
#    contains a1_verdict.json -> parsed straight from the archive, no SSH
#    find/cat. FAIL-SOFT fallback: only when the Black-Box channel produces
#    no verdict at all (pull/checksum failure) does a direct SSH find+cat
#    kick in, clearly demoted via verdict_source=="ssh_fallback". The env
#    flag JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED can disable that
#    fallback entirely, keeping IapBlackBoxTransport the ONLY verdict source.
# ===========================================================================


def test_retrieve_verdict_authoritative_from_blackbox_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED", raising=False)
    archive_src = _make_verdict_tarball(
        tmp_path / "src_black_box.tar.gz",
        verdict={"proven": True, "criteria": {"cycle_completed": True}},
    )
    exploding_runner = RecordingRunner()  # any call here fails the test below.

    ignition = _make_ignition(
        local_out_root=str(tmp_path / "brain_out"),
        blackbox_transport_factory=lambda *, node, hypervisor_args: FakeBlackboxTransportWithArchive(
            node=node, hypervisor_args=hypervisor_args, archive_src=str(archive_src),
            sha256="cafebabe",
        ),
        runner=exploding_runner,
    )

    result = asyncio.run(ignition.retrieve_verdict())

    assert result["verdict_source"] == "blackbox"
    assert result["verdict"] == {"proven": True, "criteria": {"cycle_completed": True}}
    assert result["blackbox"]["pulled"] is True
    assert result["blackbox"]["sha256"] == "cafebabe"
    # The AUTHORITATIVE path never shells out via SSH at all (no find/cat).
    assert exploding_runner.calls == []


def test_retrieve_verdict_falls_back_to_ssh_when_blackbox_pull_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED", raising=False)
    verdict_remote_path = (
        "/opt/trinity/jarvis/a1_iso_runs/a1-brain-test/iso-a1-20260624-010101/a1_verdict.json"
    )
    fallback_verdict = {"proven": False, "criteria": {"cycle_completed": False}}
    runner = RecordingRunner(responses=[
        (0, verdict_remote_path + "\n"),          # _discover_verdict_path (find)
        (0, json.dumps(fallback_verdict)),         # _fetch_verdict_json (cat)
    ])

    ignition = _make_ignition(
        local_out_root=str(tmp_path / "brain_out"),
        blackbox_transport_factory=lambda *, node, hypervisor_args: FakeBlackboxTransportWithArchive(
            node=node, hypervisor_args=hypervisor_args, archive_src="/nonexistent.tar.gz",
            pull_should_succeed=False,  # simulates a checksum/pull failure
        ),
        runner=runner,
    )

    result = asyncio.run(ignition.retrieve_verdict())

    assert result["verdict_source"] == "ssh_fallback"
    assert result["verdict"] == fallback_verdict
    assert result["blackbox"]["pulled"] is False
    # Exactly the two fallback SSH calls (find, then cat) -- nothing more.
    assert len(runner.calls) == 2


def test_retrieve_verdict_ssh_fallback_disabled_by_env(tmp_path, monkeypatch):
    """When the operator disables the SSH fallback, a Black-Box pull failure
    must leave the verdict UNAVAILABLE rather than silently reaching for a
    non-authoritative source."""
    monkeypatch.setenv("JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED", "false")
    runner = RecordingRunner()

    ignition = _make_ignition(
        local_out_root=str(tmp_path / "brain_out"),
        blackbox_transport_factory=lambda *, node, hypervisor_args: FakeBlackboxTransportWithArchive(
            node=node, hypervisor_args=hypervisor_args, archive_src="/nonexistent.tar.gz",
            pull_should_succeed=False,
        ),
        runner=runner,
    )

    result = asyncio.run(ignition.retrieve_verdict())

    assert result["verdict"] is None
    assert result["verdict_source"] == "none"
    # The SSH VERDICT FALLBACK was never entered -- no a1_verdict.json find/cat
    # over SSH (mandate 3: authoritative retrieval is IapBlackBoxTransport-only,
    # and the fallback is disabled). A diagnostic ignite_dispatch.log tail on the
    # UNAVAILABLE branch is NOT verdict retrieval and is permitted.
    assert not any(
        "a1_verdict.json" in " ".join(str(a) for a in call[0])
        for call in runner.calls), (
        "the SSH verdict fallback must not run when disabled: %r" % (runner.calls,))


def test_retrieve_verdict_bundle_without_verdict_member_falls_back(tmp_path, monkeypatch):
    """A checksum-verified archive that simply doesn't CONTAIN a1_verdict.json
    (the node-side bundler's fail-soft-absent case) must be treated the same
    as a pull failure -- fall through to the SSH fallback, not crash."""
    monkeypatch.delenv("JARVIS_A1_BRAIN_VERDICT_SSH_FALLBACK_ENABLED", raising=False)
    archive_src = _make_verdict_tarball(
        tmp_path / "src_black_box_no_verdict.tar.gz",
        verdict={}, include_verdict=False,
    )
    verdict_remote_path = (
        "/opt/trinity/jarvis/a1_iso_runs/a1-brain-test/iso-a1-20260624-010101/a1_verdict.json"
    )
    fallback_verdict = {"proven": True, "criteria": {}}
    runner = RecordingRunner(responses=[
        (0, verdict_remote_path + "\n"),
        (0, json.dumps(fallback_verdict)),
    ])

    ignition = _make_ignition(
        local_out_root=str(tmp_path / "brain_out"),
        blackbox_transport_factory=lambda *, node, hypervisor_args: FakeBlackboxTransportWithArchive(
            node=node, hypervisor_args=hypervisor_args, archive_src=str(archive_src),
        ),
        runner=runner,
    )

    result = asyncio.run(ignition.retrieve_verdict())

    assert result["blackbox"]["pulled"] is True  # the archive itself pulled fine
    assert result["verdict_source"] == "ssh_fallback"
    assert result["verdict"] == fallback_verdict


def test_dry_run_still_zero_side_effect_with_new_verdict_path(capsys):
    """Re-confirms the --dry-run zero-side-effect guarantee holds after the
    mandate-3 retrieve_verdict() rewrite (retrieve_verdict is never reached
    on the dry-run path)."""
    exploding_rest = ExplodingComputeRest()
    runner = RecordingRunner()

    ignition = _make_ignition(
        dry_run=True, project="demo-project", zone="us-central1-a",
        compute_rest_factory=lambda: exploding_rest,
        provision_fn=_exploding_provision_fn,
        runner=runner,
    )

    rc = asyncio.run(ignition.run())

    assert rc == 0
    assert runner.calls == []
    out = capsys.readouterr().out
    assert "DRY RUN: zero network" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
