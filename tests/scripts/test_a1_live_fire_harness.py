"""Tests for the A1 Live-Fire Chaos Harness orchestrator.

TDD spine. The subprocesses (chaos injector, O+V soak, auditor, IaC hypervisor,
sentinel) are ALL mocked -- this test launches no real soak and provisions no
real node and spends zero dollars. It proves the ORCHESTRATION contract:

  * the sequence runs in order: preflight -> inject -> soak -> audit -> collect;
  * chaos is ALWAYS reverted in finally -- even when the soak raises, the auditor
    throws GraduationFailedException, or a SIGINT/KeyboardInterrupt fires
    (revert is asserted called on EVERY path);
  * the verdict is collected into the run report dir;
  * ``--remote`` without the money-gate REFUSES (no hypervisor call);
  * ``--dry-run-local --stub-soak`` produces an A1_DISPATCH_PROVEN verdict
    end-to-end against the synthetic timeline (proves the wiring);
  * the composed env contains the CADENCE_POLICY-DERIVED flags ON (not hardcoded).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "a1_live_fire_chaos_harness.py"
_spec = importlib.util.spec_from_file_location("a1_live_fire_chaos_harness", _SCRIPT)
assert _spec and _spec.loader
harness = importlib.util.module_from_spec(_spec)
sys.modules["a1_live_fire_chaos_harness"] = harness
_spec.loader.exec_module(harness)


# ===========================================================================
# Fakes for the three orchestrated subprocesses.
# ===========================================================================


class FakeChaos:
    """Records the chaos lifecycle calls in order."""

    def __init__(self, *, candidates=2, inject_red=True):
        self.calls = []
        self._candidates = candidates
        self._inject_red = inject_red

    def status(self):
        self.calls.append("status")
        return {"active": False}

    def list_candidates(self):
        self.calls.append("list_candidates")
        return self._candidates

    def inject(self, seed):
        self.calls.append(("inject", seed))
        return self._inject_red

    def revert(self):
        self.calls.append("revert")
        return True


class FakeSoak:
    """A soak launcher that records launch + returns a debug.log path."""

    def __init__(self, debug_log, *, raises=None):
        self._debug_log = debug_log
        self._raises = raises
        self.calls = []

    def launch(self, env, run_dir):
        self.calls.append(("launch", run_dir))
        if self._raises is not None:
            raise self._raises
        return harness.SoakHandle(debug_log=self._debug_log, session_dir=run_dir, proc=None)

    def stop(self):
        self.calls.append("stop")


class FakeAuditor:
    """An auditor runner that records the audit + returns a verdict (or raises)."""

    def __init__(self, *, proven=True, raises=None):
        self._proven = proven
        self._raises = raises
        self.calls = []

    def watch(self, *, base, log_file, timeout_s, verdict_out):
        self.calls.append(("watch", base, log_file))
        if self._raises is not None:
            raise self._raises
        verdict = {"verdict": "proven" if self._proven else "failed", "proven": self._proven}
        Path(verdict_out).write_text(json.dumps(verdict))
        return verdict


# ===========================================================================
# 1. compose_env: flags derived from CADENCE_POLICY, all ON (not hardcoded)
# ===========================================================================


def test_compose_env_flags_are_derived_and_on(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    env = harness.compose_env()
    flags = harness.derive_cognitive_flags()
    assert len(flags) >= 5, "must derive a non-trivial flag set from CADENCE_POLICY"
    for f in flags:
        assert env.get(f) == "true", f"{f} must be composed ON"
    # The orchestration-required flags are present + ON.
    assert env["JARVIS_ROADMAP_ORCHESTRATOR_ENABLED"] == "1"
    assert env["JARVIS_IDE_STREAM_ENABLED"] == "1"
    assert env["JARVIS_A1_TRACE_ENABLED"] == "1"


def test_compose_env_flags_are_not_a_hardcoded_constant(monkeypatch):
    # An env override changes the derived set -> proves derivation, not a literal.
    monkeypatch.setenv("JARVIS_A1_AUDIT_FLAGS", "FLAG_ALPHA,FLAG_BETA")
    flags = harness.derive_cognitive_flags()
    assert "FLAG_ALPHA" in flags and "FLAG_BETA" in flags
    env = harness.compose_env()
    assert env["FLAG_ALPHA"] == "true" and env["FLAG_BETA"] == "true"


def test_compose_env_sources_linux_overlay(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    env = harness.compose_env()
    # The deploy/ouroboros_linux_prod.env overlay keys must be merged.
    assert env.get("JARVIS_PROVIDER_CLAUDE_DISABLED") == "true"
    assert "OUROBOROS_BATTLE_MAX_WALL_SECONDS" in env


# ===========================================================================
# 2. The orchestration sequence runs in order.
# ===========================================================================


def _make_run(tmp_path, *, chaos, soak, auditor, **kw):
    return harness.HarnessRun(
        run_id="test-run",
        run_root=str(tmp_path / "a1_runs"),
        autopsy_root=str(tmp_path / "a1_autopsy"),
        cost_cap=0.0,
        wall_seconds=120,
        seed=7,
        sse_base="http://127.0.0.1:8099",
        chaos=chaos,
        soak=soak,
        auditor=auditor,
        **kw,
    )


def test_sequence_runs_in_order(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    rc = run.execute()
    # preflight (status + list_candidates) -> inject -> revert (finally)
    assert chaos.calls[0] == "status"
    assert chaos.calls[1] == "list_candidates"
    assert chaos.calls[2][0] == "inject"
    assert "revert" in chaos.calls
    assert soak.calls[0][0] == "launch"
    assert auditor.calls[0][0] == "watch"
    assert rc == 0  # proven


# ===========================================================================
# 3. Chaos is ALWAYS reverted in finally -- EVERY failure path.
# ===========================================================================


def test_revert_on_soak_raises(tmp_path):
    chaos = FakeChaos()
    soak = FakeSoak(str(tmp_path / "debug.log"), raises=RuntimeError("soak boom"))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    rc = run.execute()
    assert "revert" in chaos.calls, "chaos must be reverted even when soak raises"
    assert rc != 0


def test_revert_on_auditor_graduation_failed(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    exc = harness.GraduationFailedException(
        "mid-loop human gate", failure_locus="intervention_lock:ask_human"
    )
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(raises=exc)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    rc = run.execute()
    assert "revert" in chaos.calls
    assert rc != 0


def test_revert_on_keyboard_interrupt(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(raises=KeyboardInterrupt())
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    with pytest.raises(KeyboardInterrupt):
        run.execute()
    # Even on SIGINT/KeyboardInterrupt the repo is restored.
    assert "revert" in chaos.calls


def test_revert_on_preflight_inject_red_failure(tmp_path):
    # If inject does NOT turn the test red, we abort -- but still revert.
    chaos = FakeChaos(inject_red=False)
    soak = FakeSoak(str(tmp_path / "debug.log"))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    rc = run.execute()
    assert "revert" in chaos.calls
    # soak never launched because inject failed RED-confirmation.
    assert soak.calls == []
    assert rc != 0


def test_revert_on_no_candidates(tmp_path):
    chaos = FakeChaos(candidates=0)
    soak = FakeSoak(str(tmp_path / "debug.log"))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    rc = run.execute()
    # Aborts at preflight, never injects -> but revert is still safe to call.
    assert ("inject", 7) not in chaos.calls
    assert "revert" in chaos.calls
    assert rc != 0


# ===========================================================================
# 4. The verdict + artifacts are collected into the run report dir.
# ===========================================================================


def test_verdict_collected_into_run_report(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("synthetic log\n")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    run = _make_run(tmp_path, chaos=chaos, soak=soak, auditor=auditor)
    run.execute()
    report_dir = Path(run.report_dir())
    assert report_dir.is_dir()
    assert (report_dir / "a1_verdict.json").exists()
    # The run report manifest aggregates the verdict.
    manifest = json.loads((report_dir / "run_report.json").read_text())
    assert manifest["run_id"] == "test-run"
    assert manifest["verdict"]["proven"] is True


def test_autopsy_invoked_on_failure(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("failing log\n")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=False)
    autopsy_calls = []
    run = _make_run(
        tmp_path, chaos=chaos, soak=soak, auditor=auditor,
        autopsy_fn=lambda **kw: autopsy_calls.append(kw) or "autopsy_dir",
    )
    rc = run.execute()
    assert rc != 0
    assert autopsy_calls, "autopsy must run on a failed/timed-out verdict"


def test_autopsy_skipped_on_success(tmp_path):
    debug_log = tmp_path / "debug.log"
    debug_log.write_text("")
    chaos = FakeChaos()
    soak = FakeSoak(str(debug_log))
    auditor = FakeAuditor(proven=True)
    autopsy_calls = []
    run = _make_run(
        tmp_path, chaos=chaos, soak=soak, auditor=auditor,
        autopsy_fn=lambda **kw: autopsy_calls.append(kw),
    )
    run.execute()
    assert autopsy_calls == []


# ===========================================================================
# 5. --remote without the money-gate REFUSES (no hypervisor call).
# ===========================================================================


def test_remote_without_money_gate_refuses(monkeypatch, capsys):
    hyper_calls = []
    monkeypatch.setattr(
        harness, "provision_and_run_remote",
        lambda **kw: hyper_calls.append(kw),
    )
    rc = harness.main(["--remote"])
    assert rc != 0, "remote without money-gate must refuse"
    assert hyper_calls == [], "hypervisor must NOT be called without the money-gate"
    out = capsys.readouterr().out
    assert "COST ESTIMATE" in out or "cost estimate" in out.lower()


def test_remote_with_money_gate_calls_hypervisor(monkeypatch):
    hyper_calls = []
    monkeypatch.setattr(
        harness, "provision_and_run_remote",
        lambda **kw: hyper_calls.append(kw) or 0,
    )
    rc = harness.main(
        ["--remote", "--i-understand-this-spends-money", "--cost-cap", "10.0",
         "--max-wall-seconds", "3600"]
    )
    assert hyper_calls, "hypervisor MUST be invoked with the money-gate"
    assert rc == 0


# ===========================================================================
# 5b. provision_and_run_remote wires transport=git + --on-demand passthrough.
# ===========================================================================


def test_provision_remote_sets_transport_git(monkeypatch):
    captured = {}

    def fake_run(argv, env=None, check=False):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.delenv("JARVIS_IAC_SYNC_TRANSPORT", raising=False)
    rc = harness.provision_and_run_remote(cost_cap=10.0, wall_seconds=3600, seed=0)
    assert rc == 0
    assert captured["env"].get("JARVIS_IAC_SYNC_TRANSPORT") == "git", \
        "the A1 remote path must drive the fast-WAN git transport"


def test_provision_remote_on_demand_passthrough_present(monkeypatch):
    captured = {}

    def fake_run(argv, env=None, check=False):
        captured["argv"] = list(argv)

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setenv("JARVIS_IAC_ON_DEMAND", "1")
    harness.provision_and_run_remote(cost_cap=10.0, wall_seconds=3600, seed=0)
    assert "--on-demand" in captured["argv"], \
        "JARVIS_IAC_ON_DEMAND=1 must append --on-demand to the hypervisor argv"


def test_provision_remote_on_demand_absent_by_default(monkeypatch):
    captured = {}

    def fake_run(argv, env=None, check=False):
        captured["argv"] = list(argv)

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.delenv("JARVIS_IAC_ON_DEMAND", raising=False)
    harness.provision_and_run_remote(cost_cap=10.0, wall_seconds=3600, seed=0)
    assert "--on-demand" not in captured["argv"], \
        "without JARVIS_IAC_ON_DEMAND the node stays Spot (no --on-demand)"


def test_provision_remote_respects_operator_tar_pin(monkeypatch):
    captured = {}

    def fake_run(argv, env=None, check=False):
        captured["env"] = dict(env or {})

        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setenv("JARVIS_IAC_SYNC_TRANSPORT", "tar")
    harness.provision_and_run_remote(cost_cap=10.0, wall_seconds=3600, seed=0)
    assert captured["env"].get("JARVIS_IAC_SYNC_TRANSPORT") == "tar", \
        "an explicit operator tar pin must be respected (setdefault, not override)"


# ===========================================================================
# 6. --dry-run-local --stub-soak: A1_DISPATCH_PROVEN end-to-end (real wiring).
# ===========================================================================


def test_stub_soak_fixture_has_full_proven_timeline(tmp_path):
    log_path = tmp_path / "stub_debug.log"
    harness.write_stub_soak_log(str(log_path), goal_id="GOAL-STUB")
    text = log_path.read_text()
    # All 5 A1Trace hops in order for one goal.
    for hop in ("emit", "ingest", "dequeue", "submit", "accept"):
        assert f"[A1Trace] {hop} goal=GOAL-STUB" in text


def test_dry_run_local_stub_soak_yields_proven(tmp_path, monkeypatch):
    # Run the REAL auditor core against the synthetic stub timeline -> PROVEN.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    # Lenient flags so UNVERIFIABLE cognitive flags don't fail the wiring proof.
    rc = harness.main(["--dry-run-local", "--stub-soak", "--lenient"])
    assert rc == 0, "the stub timeline must yield A1_DISPATCH_PROVEN"
    # Verdict file written and proven.
    runs = sorted((tmp_path / "a1_runs").glob("*/a1_verdict.json"))
    assert runs, "a verdict must be collected into the run report"
    verdict = json.loads(runs[-1].read_text())
    assert verdict["proven"] is True


def test_dry_run_local_leaves_tree_reverted(tmp_path, monkeypatch):
    # The real chaos controller is used in dry-run-local; assert revert called.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JARVIS_A1_AUDIT_FLAGS", raising=False)
    revert_marker = {"reverted": False}

    real_run_factory = harness.build_dry_run_local

    def _wrapped(args):
        run = real_run_factory(args)
        orig_revert = run.chaos.revert

        def _spy():
            revert_marker["reverted"] = True
            return orig_revert()

        run.chaos.revert = _spy
        return run

    monkeypatch.setattr(harness, "build_dry_run_local", _wrapped)
    harness.main(["--dry-run-local", "--stub-soak", "--lenient"])
    assert revert_marker["reverted"], "dry-run-local must revert chaos in finally"
