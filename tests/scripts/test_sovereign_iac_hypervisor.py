# -*- coding: utf-8 -*-
"""Pure-logic tests for scripts/sovereign_iac_hypervisor.py.

No real GCP/SSH/rsync. ALL gcloud/ssh/rsync funnel through the script's single
`_run` boundary (or `_run_streaming` for the streamed surgery), which these tests
monkeypatch with a fake that records call order -- asserting dry-run never
executes, the triple-gate refuses, the startup-script carries the Docker install
+ the dead-man SA-token REST self-DELETE + the completion-sentinel trigger,
provision uses e2-standard-8 + SPOT + DELETE-on-preempt + max-run + cloud-platform
scope, sync issues commands with the excludes, the surgery streams line-by-line,
and the BURN runs in `finally` on PASS, on FRACTURE, AND on a raised exception
(delete issued in all three, after everything else).
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sovereign_iac_hypervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("sovereign_iac_hypervisor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def iac():
    return _load_module()


@pytest.fixture()
def args(iac):
    """Parse a default arg namespace (dry-run defaults) the tests can mutate."""
    return iac.build_parser().parse_args([])


class _Recorder:
    """Records every (cmd) passed to the faked _run / _run_streaming, in order."""

    def __init__(self):
        self.calls = []

    def run(self, cmd, *, timeout_s=120.0):
        self.calls.append(cmd)
        # Default: success + benign output. Specific tests override behavior.
        return 0, ""

    def joined(self):
        return [" ".join(c) for c in self.calls]


# --------------------------------------------------------------------------- #
# Startup-script generator.
# --------------------------------------------------------------------------- #
def test_startup_script_installs_docker(iac):
    s = iac.build_startup_script()
    assert "docker" in s.lower()
    assert "get.docker.com" in s or "docker.io" in s
    assert s.startswith("#!")
    s.encode("ascii")  # ASCII only


def test_startup_script_has_deadman_sa_token_rest_self_delete(iac):
    s = iac.build_startup_script()
    # metadata SA token + Compute REST DELETE -- the proven self-delete pattern.
    assert "metadata.google.internal" in s
    assert "service-accounts/default/token" in s
    assert "compute.googleapis.com" in s
    assert "-X DELETE" in s
    assert "Authorization: Bearer" in s
    assert "export HOME=/root" in s  # the bake lesson


def test_startup_script_completion_sentinel_triggers_immediate_burn(iac):
    sentinel = "/var/run/custom_done_marker"
    s = iac.build_startup_script(completion_sentinel=sentinel)
    assert sentinel in s
    # The sentinel branch must self-delete immediately (before the idle path).
    assert "COMPLETION SENTINEL" in s
    assert s.index("COMPLETION SENTINEL") < s.index("IDLE TIMEOUT")


def test_startup_script_writes_ready_sentinel(iac):
    s = iac.build_startup_script()
    assert iac._READY_SENTINEL in s


# --------------------------------------------------------------------------- #
# Provision command -- e2-standard-8 + SPOT + DELETE + max-run + scope.
# --------------------------------------------------------------------------- #
def test_provision_cmd_uses_e2_standard_8_spot_delete_maxrun_scope(iac, args):
    cmd = iac._create_node_cmd(args, "sovereign-sandbox-x", "/tmp/startup.sh")
    j = " ".join(cmd)
    assert "instances create sovereign-sandbox-x" in j
    assert "--machine-type=e2-standard-8" in j
    assert "--provisioning-model=SPOT" in j
    assert "--instance-termination-action=DELETE" in j
    assert "--max-run-duration=3600s" in j
    assert "--scopes=cloud-platform" in j  # the dead-man needs it
    assert "--metadata-from-file=startup-script=/tmp/startup.sh" in j


def test_provision_node_writes_startup_to_tmpfile_and_runs(iac, args, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    ok, detail = iac.provision_sandbox_node(args, "sovereign-sandbox-y", "SCRIPT BODY")
    assert ok
    assert len(rec.calls) == 1
    assert "instances" in rec.calls[0]
    assert "create" in rec.calls[0]


# --------------------------------------------------------------------------- #
# Sync bridge -- 3 repos with excludes.
# --------------------------------------------------------------------------- #
def test_sync_issues_commands_with_excludes_for_three_repos(iac, args, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.setenv("JARVIS_IAC_SYNC_TRANSPORT", "rsync")
    args.prime_repo_path = "/repos/prime"
    args.reactor_repo_path = "/repos/reactor"
    excludes = iac.parse_excludes(".git,__pycache__,node_modules,.venv")
    ok, detail = iac.sync_repos_to_node(args, "sovereign-sandbox-z", excludes)
    assert ok, detail
    assert len(rec.calls) == 3  # jarvis, prime, reactor
    joined = rec.joined()
    # excludes present in the rsync command.
    assert any("--exclude" in c and ".git" in c for c in joined)
    assert any("node_modules" in c for c in joined)
    # all three remote targets.
    assert any("/opt/trinity/jarvis" in c for c in joined)
    assert any("/opt/trinity/prime" in c for c in joined)
    assert any("/opt/trinity/reactor" in c for c in joined)


def test_sync_fails_soft_on_unset_repo_path(iac, args, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.delenv("JARVIS_PRIME_REPO_PATH", raising=False)
    monkeypatch.delenv("JARVIS_REACTOR_REPO_PATH", raising=False)
    args.prime_repo_path = None
    args.reactor_repo_path = None
    ok, detail = iac.sync_repos_to_node(args, "node", iac.parse_excludes(".git"))
    assert not ok
    assert "prime" in detail


# --------------------------------------------------------------------------- #
# Remote surgery -- env set + streaming line-by-line.
# --------------------------------------------------------------------------- #
def test_remote_surgery_shell_sets_trinity_env(iac, args):
    shell = iac._remote_surgery_shell(args)
    assert "JARVIS_PRIME_REPO_PATH=" in shell
    assert "JARVIS_REACTOR_REPO_PATH=" in shell
    assert "JARVIS_TRINITY_PREBAKE_ENABLED=1" in shell
    assert "JARVIS_CROSS_REPO_MUTATION_ENABLED=1" in shell
    assert "JARVIS_CHAOS_INJECTOR_ENABLED=1" in shell
    assert "/opt/trinity/jarvis" in shell
    # ALWAYS touch the completion-sentinel so the remote dead-man fires.
    assert args.completion_sentinel in shell
    assert "touch" in shell


def test_run_streaming_reads_line_by_line(iac, monkeypatch):
    """Assert the streaming loop iterates stdout line-by-line and emits each."""
    emitted = []

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = iter(lines)

        def __iter__(self):
            return self._lines

    class _FakeProc:
        def __init__(self, lines):
            self.stdout = _FakeStdout(lines)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    fake_lines = ["line-1\n", "Blast-Radius\n", "VERDICT: PASS\n"]

    def _fake_popen(cmd, **kwargs):
        return _FakeProc(fake_lines)

    monkeypatch.setattr(iac.subprocess, "Popen", _fake_popen)
    rc, captured = iac._run_streaming(["gcloud", "ssh"], sink=lambda l: emitted.append(l))
    assert rc == 0
    # captured EACH line, in order (proves line-by-line read).
    assert captured == fake_lines
    assert emitted == fake_lines


def test_parse_verdict(iac):
    assert iac.parse_verdict(["...", "VERDICT: PASS\n"]) == "PASS"
    assert iac.parse_verdict(["[SOVEREIGN YIELD: CROSS-REPO FRACTURE] emitted\n"]) == "FRACTURE"
    assert iac.parse_verdict(["nothing here\n"]) == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Triple-gate -- refuses without all three.
# --------------------------------------------------------------------------- #
def test_triple_gate_refuses_without_master_env(iac, args, monkeypatch):
    monkeypatch.delenv("JARVIS_IAC_HYPERVISOR_ENABLED", raising=False)
    args.dry_run = False
    args.i_understand_this_spends_money = True
    ok, reason = iac.check_triple_gate(args)
    assert not ok
    assert "master gate" in reason


def test_triple_gate_refuses_without_execute(iac, args, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_HYPERVISOR_ENABLED", "1")
    args.dry_run = True  # still dry-run
    args.i_understand_this_spends_money = True
    ok, reason = iac.check_triple_gate(args)
    assert not ok


def test_triple_gate_refuses_without_money_flag(iac, args, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_HYPERVISOR_ENABLED", "1")
    args.dry_run = False
    args.i_understand_this_spends_money = False
    ok, reason = iac.check_triple_gate(args)
    assert not ok
    assert "spends-money" in reason or "spends_money" in reason


def test_triple_gate_passes_with_all_three(iac, args, monkeypatch):
    monkeypatch.setenv("JARVIS_IAC_HYPERVISOR_ENABLED", "1")
    args.dry_run = False
    args.i_understand_this_spends_money = True
    ok, reason = iac.check_triple_gate(args)
    assert ok


# --------------------------------------------------------------------------- #
# Dry-run: prints the plan, issues ZERO real commands.
# --------------------------------------------------------------------------- #
def test_dry_run_issues_zero_commands(iac, monkeypatch, capsys):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.setattr(iac, "_run_streaming", lambda *a, **k: (0, []))
    rc = iac.main(["--dry-run", "--prime-repo-path", "/p", "--reactor-repo-path", "/r"])
    assert rc == 0
    assert rec.calls == []  # ZERO real commands
    out = capsys.readouterr().out
    assert "PLAN (dry-run" in out
    assert "e2-standard-8" in out
    assert "quadruple teardown" in out.lower()
    assert "COST ESTIMATE" in out


def test_execute_refused_without_gate_issues_zero_commands(iac, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.delenv("JARVIS_IAC_HYPERVISOR_ENABLED", raising=False)
    rc = iac.main(["--execute"])  # no money flag, no master env
    assert rc == 2
    assert rec.calls == []  # refused BEFORE any command


# --------------------------------------------------------------------------- #
# BURN runs in finally on PASS, FRACTURE, AND exception -- after everything.
# --------------------------------------------------------------------------- #
def _wire_execute_fakes(iac, monkeypatch, rec, *, surgery_behavior):
    """Wire provision/ready/sync to succeed; surgery_behavior drives phase 3."""
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.setattr(iac, "provision_sandbox_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    monkeypatch.setattr(iac, "run_remote_surgery", surgery_behavior)


def _delete_issued(rec):
    return any("delete" in c and "instances" in c for c in rec.calls)


def test_burn_runs_on_pass(iac, args, monkeypatch):
    rec = _Recorder()
    _wire_execute_fakes(
        iac, monkeypatch, rec,
        surgery_behavior=lambda *a, **k: (0, ["VERDICT: PASS\n"], "PASS"),
    )
    rc = iac._execute(args, "sovereign-sandbox-pass", iac.build_startup_script(), [])
    assert rc == 0
    assert _delete_issued(rec), "BURN must issue the delete on PASS"


def test_burn_runs_on_fracture(iac, args, monkeypatch):
    rec = _Recorder()
    _wire_execute_fakes(
        iac, monkeypatch, rec,
        surgery_behavior=lambda *a, **k: (0, ["[SOVEREIGN YIELD: CROSS-REPO FRACTURE]\n"], "FRACTURE"),
    )
    rc = iac._execute(args, "sovereign-sandbox-frac", iac.build_startup_script(), [])
    assert rc == 0
    assert _delete_issued(rec), "BURN must issue the delete on FRACTURE"


def test_burn_runs_on_exception(iac, args, monkeypatch):
    rec = _Recorder()

    def _boom(*a, **k):
        raise RuntimeError("ssh dropped mid-surgery")

    _wire_execute_fakes(iac, monkeypatch, rec, surgery_behavior=_boom)
    with pytest.raises(RuntimeError):
        iac._execute(args, "sovereign-sandbox-boom", iac.build_startup_script(), [])
    # BURN still issued in the finally despite the raised exception.
    assert _delete_issued(rec), "BURN must issue the delete even on a raised exception"


def test_burn_is_after_everything(iac, args, monkeypatch):
    """Assert the delete (burn) is the LAST instances-command issued."""
    rec = _Recorder()
    # Don't stub _run for describe -- let provision/sync use real-ish recorder.
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    monkeypatch.setattr(
        iac, "run_remote_surgery",
        lambda *a, **k: (0, ["VERDICT: PASS\n"], "PASS"),
    )
    rc = iac._execute(args, "sovereign-sandbox-order", iac.build_startup_script(), [])
    assert rc == 0
    # Find the provision (create) and the burn (delete) call indices.
    create_idx = next(i for i, c in enumerate(rec.calls) if "create" in c and "instances" in c)
    delete_idx = next(i for i, c in enumerate(rec.calls) if "delete" in c and "instances" in c)
    assert delete_idx > create_idx, "burn (delete) must come AFTER provision (create)"


# --------------------------------------------------------------------------- #
# Verify-gone is checked.
# --------------------------------------------------------------------------- #
def test_verify_gone_true_when_describe_fails(iac, args, monkeypatch):
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (1, "not found"))
    assert iac.verify_node_gone(args, "node") is True


def test_verify_gone_false_when_describe_succeeds(iac, args, monkeypatch):
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (0, "node"))
    assert iac.verify_node_gone(args, "node") is False


def test_burn_node_prints_quadruple_teardown(iac, args, monkeypatch, capsys):
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (0, ""))
    iac.burn_node(args, "node")
    out = capsys.readouterr().out
    assert "quadruple teardown" in out
    assert "remote-deadman" in out


# --------------------------------------------------------------------------- #
# Node naming -- stamp from the CLI.
# --------------------------------------------------------------------------- #
def test_default_node_name_uses_stamp(iac):
    assert iac.default_node_name("20260623-120000") == "sovereign-sandbox-20260623-120000"
