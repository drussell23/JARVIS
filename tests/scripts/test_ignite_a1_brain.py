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
import importlib.util
import pathlib
import sys
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
    assert ignition.lifetime_s == 1000 + ignition.boot_offset_s + ignition.verdict_pull_margin_s
    assert ignition.lifetime_s >= (
        ignition.soak_wall_s + ignition.boot_offset_s + ignition.verdict_pull_margin_s
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
    the floor (2400 + 180 + 180 = 2760) and the full ordering invariant
    (soak_wall_s < monitor_stop < lifetime_s) must hold."""
    ignition = _make_ignition(
        dry_run=True, project="p", zone="z", soak_wall_s=2400, lifetime_s=1800,
        monitor_max_s=None,  # let monitor_max_s DERIVE from lifetime_s (fixture
        # default overrides it to a tiny test value otherwise, which would
        # trivially satisfy any ordering check).
    )
    expected_floor = 2400 + ignition.boot_offset_s + ignition.verdict_pull_margin_s
    assert expected_floor == 2760
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
    # The remote payload is the detached dispatch wrapper, which embeds the
    # real isomorphic_a1_local.py invocation.
    assert "isomorphic_a1_local.py" in argv[-1]


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
# 4. Teardown targets the resolved node + zone.
# ===========================================================================


def test_teardown_deletes_resolved_node_and_zone():
    rest = FakeComputeRest(instances=[{"name": "a1-brain-test", "zone": "projects/p/zones/us-east1-b"}])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="")

    asyncio.run(ignition.resolve_actual_zone())
    assert ignition.zone == "us-east1-b"

    result = asyncio.run(ignition.teardown())

    assert result["deleted"] is True
    assert rest.delete_calls == [("a1-brain-test", "us-east1-b")]


def test_teardown_reissues_delete_on_zone_mismatch():
    """MINOR #4: when the all-zone label-based verify list detects the node
    LEAKED in a DIFFERENT zone than the one the explicit delete targeted
    (zone mismatch), teardown() must RE-ISSUE delete_instance with the
    discovered zone rather than just logging LEAK."""
    rest = FakeComputeRest(
        instances=[{"name": "a1-brain-test", "zone": "projects/p/zones/us-east1-b"}])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="us-central1-a")

    result = asyncio.run(ignition.teardown())

    # First call: the explicit delete, targeting the (wrong) pre-set zone.
    assert rest.delete_calls[0] == ("a1-brain-test", "us-central1-a")
    # Second call: the zone-mismatch re-delete, targeting the zone DISCOVERED
    # from the verify list.
    assert rest.delete_calls[-1] == ("a1-brain-test", "us-east1-b")
    assert len(rest.delete_calls) == 2
    # Re-delete succeeded (FakeComputeRest always returns ok=True) -> no leak.
    assert result["zero_cost"] is True


def test_teardown_no_reissue_when_verify_list_clean():
    """No leaked instance in the verify list -> no re-delete is issued."""
    rest = FakeComputeRest(instances=[])
    ignition = _make_ignition(compute_rest_factory=lambda: rest, zone="us-central1-a")

    result = asyncio.run(ignition.teardown())

    assert len(rest.delete_calls) == 1  # only the explicit delete
    assert result["zero_cost"] is True


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


def test_provision_registers_node_up_front_for_teardown():
    ignite._ACTIVE_IGNITIONS.clear()

    async def _ok_provision_fn(*, node_name: str) -> Tuple[bool, str]:
        # Assert the registration already happened BEFORE the "create" call.
        assert any(e["node"] == node_name for e in ignite._ACTIVE_IGNITIONS)
        return True, "ok"

    ignition = _make_ignition(node_name="a1-brain-upfront", provision_fn=_ok_provision_fn)
    ok, _ = asyncio.run(ignition.provision())
    assert ok is True
    assert any(e["node"] == "a1-brain-upfront" for e in ignite._ACTIVE_IGNITIONS)
    ignite._ACTIVE_IGNITIONS.clear()


# ===========================================================================
# 6. Reaper registry: idempotent + never raises when drained twice.
# ===========================================================================


def test_reap_registry_idempotent_and_never_raises():
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    ignite._register_ignition(node="a1-brain-reap-1", zone="us-central1-a",
                              compute_rest_factory=lambda: rest)
    ignite._register_ignition(node="a1-brain-reap-2", zone=None,
                              compute_rest_factory=lambda: rest)
    assert len(ignite._ACTIVE_IGNITIONS) == 2

    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []
    assert sorted(rest.delete_calls) == [
        ("a1-brain-reap-1", "us-central1-a"),
        ("a1-brain-reap-2", None),
    ]

    # Second drain: registry already empty -- must be a silent no-op, no raise.
    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []
    assert len(rest.delete_calls) == 2  # unchanged -- nothing re-deleted


def test_reap_registry_never_raises_on_deleter_exception():
    ignite._ACTIVE_IGNITIONS.clear()

    class BoomComputeRest:
        async def delete_instance(self, name: Optional[str] = None, *, zone: Optional[str] = None):
            raise RuntimeError("simulated GCP failure")

    ignite._register_ignition(node="a1-brain-boom", zone=None,
                              compute_rest_factory=lambda: BoomComputeRest())

    # Must not raise, despite the deleter blowing up.
    asyncio.run(ignite._reap_ignitions_async())
    assert ignite._ACTIVE_IGNITIONS == []


def test_reap_sync_wrapper_idempotent_and_never_raises():
    ignite._ACTIVE_IGNITIONS.clear()
    rest = FakeComputeRest()
    ignite._register_ignition(node="a1-brain-sync-reap", zone="us-central1-a",
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
    ignition = _make_ignition(dry_run=True, lifetime_s=2400, project="p", zone="z")
    plan = ignition.build_plan()
    assert plan.lifetime_s == 2400
    assert plan.provision_env["JARVIS_BRAIN_VM_PERSISTENT"] == "true"
    assert plan.provision_env["JARVIS_BRAIN_ABSOLUTE_LIFETIME_S"] == "2400"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
