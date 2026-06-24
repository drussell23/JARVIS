# -*- coding: utf-8 -*-
"""Checkpoint (resume-don't-restart) + synchronous-streaming tests for
scripts/sovereign_iac_hypervisor.py.

No real GCP/SSH/rsync. All gcloud/ssh/rsync funnel through the script's `_run`
(capture) and `_run_streaming` / `_run_streaming_labeled` (stream) boundaries,
which these tests monkeypatch with fakes that record call order. Asserts:

  * the `.hypervisor_state.json` ledger round-trips (write/read phases),
  * RESUME on a recorded RUNNING node skips completed phases (provision NOT
    re-issued; resumes from the first incomplete phase),
  * a recorded-but-GONE node -> fresh start (provision re-issued),
  * --fresh ignores the ledger,
  * a resumable mid-pipeline failure with --keep-warm-on-failure -> node NOT
    burned + ledger persisted + non-zero exit (NO delete issued),
  * a clean PASS/FRACTURE -> burn runs,
  * `_run_streaming` iterates lines in real-time (incrementally, not all-at-end)
    + tees to the per-run log,
  * the streamed phases carry their `[<label>]` prefixes.
"""
from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Isolate the ledger + autopsy dir inside a tmp cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def args(iac):
    return iac.build_parser().parse_args([])


class _Recorder:
    """Records every cmd through both subprocess boundaries, in order."""

    def __init__(self, *, run_rc=0, run_out="", stream_rc=0, stream_lines=None):
        self.calls = []
        self.stream_labels = []
        self.run_rc = run_rc
        self.run_out = run_out
        self.stream_rc = stream_rc
        self.stream_lines = stream_lines or []

    def run(self, cmd, *, timeout_s=120.0):
        self.calls.append(cmd)
        return self.run_rc, self.run_out

    def stream(self, cmd, *, label="", log_path=None, timeout_s=3600.0):
        self.calls.append(cmd)
        self.stream_labels.append(label)
        return self.stream_rc, list(self.stream_lines)

    def patch(self, monkeypatch, iac):
        monkeypatch.setattr(iac, "_run", self.run)
        monkeypatch.setattr(iac, "_run_streaming_labeled", self.stream)


def _delete_issued(rec):
    return any("delete" in c and "instances" in c for c in rec.calls)


def _create_issued(rec):
    return any("create" in c and "instances" in c for c in rec.calls)


# --------------------------------------------------------------------------- #
# Ledger round-trip.
# --------------------------------------------------------------------------- #
def test_ledger_round_trips_phases(iac, tmp_path):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R1", node_name="node-a", zone="z1", project="p1")
    assert iac.CheckpointLedger.completed_phases(data) == []
    data = led.mark_phase(data, "provisioned")
    data = led.mark_phase(data, "docker_ready", external_ip="1.2.3.4")
    # Re-read from disk -- phases + connection info persisted.
    fresh = iac.CheckpointLedger(str(tmp_path / "state.json")).read()
    assert fresh["node_name"] == "node-a"
    assert fresh["external_ip"] == "1.2.3.4"
    assert iac.CheckpointLedger.phase_complete(fresh, "provisioned")
    assert iac.CheckpointLedger.phase_complete(fresh, "docker_ready")
    assert not iac.CheckpointLedger.phase_complete(fresh, "synced")
    assert iac.CheckpointLedger.first_incomplete(fresh) == "synced"
    # Each phase carries a timestamp.
    assert fresh["phases"]["provisioned"]["ts"]


def test_ledger_corrupt_reads_empty(iac, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert iac.CheckpointLedger(str(p)).read() == {}


def test_ledger_atomic_write_is_valid_json(iac, tmp_path):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    led.init_run(run_id="R", node_name="n", zone="z", project="pr")
    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    json.loads(raw)  # parses


# --------------------------------------------------------------------------- #
# RESUME vs FRESH context resolution.
# --------------------------------------------------------------------------- #
def _seed_ledger(iac, tmp_path, *, node, completed):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name=node, zone="us-central1-a", project="proj")
    for ph in completed:
        data = led.mark_phase(data, ph)
    return led


def test_resume_on_running_node_skips_completed_phases(iac, args, tmp_path, monkeypatch, capsys):
    led = _seed_ledger(iac, tmp_path, node="warm-node",
                       completed=["provisioned", "docker_ready", "synced"])
    # Node is RUNNING.
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (0, "RUNNING"))
    node, data, resuming = iac.resolve_run_context(args, led, "fresh-node", "R")
    assert resuming is True
    assert node == "warm-node"
    out = capsys.readouterr().out
    assert "[IAC RESUME]" in out
    assert "files synced=True" in out
    assert "resuming from phase prebaked" in out


def test_resume_runs_only_incomplete_phases_no_reprovision(iac, args, tmp_path, monkeypatch):
    """Full _execute resume: provision/sync are complete -> NOT re-issued; the
    incomplete phases (prebake/boot/surgery) run; provision NOT re-created."""
    led = _seed_ledger(iac, tmp_path, node="warm-node",
                       completed=["provisioned", "docker_ready", "synced"])
    data = led.read()
    rec = _Recorder(stream_lines=["VERDICT: PASS\n"])
    rec.patch(monkeypatch, iac)
    # poll/sync/prebake/boot succeed; surgery returns a verdict via the recorder.
    prebake_called = {"n": 0}
    boot_called = {"n": 0}
    monkeypatch.setattr(iac, "run_remote_prebake",
                        lambda *a, **k: (prebake_called.update(n=prebake_called["n"] + 1), (True, "ok"))[1])
    monkeypatch.setattr(iac, "run_remote_boot",
                        lambda *a, **k: (boot_called.update(n=boot_called["n"] + 1), (True, "ok"))[1])
    monkeypatch.setattr(iac, "run_remote_surgery",
                        lambda *a, **k: (0, ["VERDICT: PASS\n"], "PASS"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)

    rc = iac._execute(args, "warm-node", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=True)
    assert rc == 0
    # provision (create) was SKIPPED -- not re-issued.
    assert not _create_issued(rec), "provision must NOT be re-issued on resume"
    # the incomplete phases ran.
    assert prebake_called["n"] == 1
    assert boot_called["n"] == 1
    # terminal verdict -> burn ran.
    assert _delete_issued(rec)


def test_gone_node_starts_fresh_reprovisions(iac, args, tmp_path, monkeypatch):
    _seed_ledger(iac, tmp_path, node="dead-node", completed=["provisioned", "synced"])
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    # describe rc != 0 -> node GONE.
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (1, "not found"))
    node, data, resuming = iac.resolve_run_context(args, led, "new-node", "R2")
    assert resuming is False
    assert node == "new-node"
    # ledger re-seeded clean (no completed phases).
    assert iac.CheckpointLedger.completed_phases(data) == []


def test_node_running_but_not_RUNNING_status_is_not_resumable(iac, args, tmp_path, monkeypatch):
    _seed_ledger(iac, tmp_path, node="stopping-node", completed=["provisioned"])
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (0, "TERMINATED"))
    node, data, resuming = iac.resolve_run_context(args, led, "new-node", "R")
    assert resuming is False
    assert node == "new-node"


def test_fresh_flag_ignores_and_clears_ledger(iac, args, tmp_path, monkeypatch):
    _seed_ledger(iac, tmp_path, node="warm-node", completed=["provisioned", "synced"])
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    args.fresh = True
    # alive-check must NOT even be consulted; but stub it to RUNNING to prove it.
    monkeypatch.setattr(iac, "_run", lambda cmd, **k: (0, "RUNNING"))
    node, data, resuming = iac.resolve_run_context(args, led, "brand-new", "R")
    assert resuming is False
    assert node == "brand-new"
    assert iac.CheckpointLedger.completed_phases(data) == []


def test_fresh_execute_reprovisions(iac, args, tmp_path, monkeypatch):
    """With no checkpoint, _execute issues the create (provision)."""
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    rec = _Recorder(stream_lines=["VERDICT: PASS\n"])
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_boot", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_surgery", lambda *a, **k: (0, ["VERDICT: PASS\n"], "PASS"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    rc = iac._execute(args, "n", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    assert rc == 0
    assert _create_issued(rec), "fresh run must provision"


# --------------------------------------------------------------------------- #
# Keep-warm-on-failure: resumable failure -> NO burn, ledger persisted, non-zero.
# --------------------------------------------------------------------------- #
def test_resumable_failure_keeps_node_warm_no_burn(iac, args, tmp_path, monkeypatch, capsys):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="warm", zone="z", project="p")
    rec = _Recorder()
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    # prebake FAILS (the PyPI-timeout class) -- resumable.
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (False, "PyPI timeout pulling wheels"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)

    args.keep_warm_on_failure = True
    args.burn_on_failure = False
    rc = iac._execute(args, "warm", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    assert rc != 0  # non-zero so the operator re-runs --execute to resume
    assert not _delete_issued(rec), "keep-warm must NOT burn the node"
    # checkpoint persisted: provisioned/docker_ready/synced complete; prebaked NOT.
    persisted = iac.CheckpointLedger(str(tmp_path / "state.json")).read()
    assert iac.CheckpointLedger.phase_complete(persisted, "synced")
    assert not iac.CheckpointLedger.phase_complete(persisted, "prebaked")
    assert iac.CheckpointLedger.first_incomplete(persisted) == "prebaked"
    out = capsys.readouterr().out
    assert "[IAC KEEP-WARM]" in out
    # the no-orphan backstop is stated.
    assert "NO-ORPHAN BACKSTOP" in out
    assert "max-run-duration" in out


def test_burn_on_failure_overrides_keep_warm(iac, args, tmp_path, monkeypatch):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="warm", zone="z", project="p")
    rec = _Recorder()
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    args.keep_warm_on_failure = True
    args.burn_on_failure = True  # operator forces burn
    iac._execute(args, "warm", iac.build_startup_script(), [],
                 ledger=led, ledger_data=data, resuming=False)
    assert _delete_issued(rec), "--burn-on-failure must burn even on a resumable failure"


def test_no_keep_warm_flag_burns_on_resumable_failure(iac, args, tmp_path, monkeypatch):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="warm", zone="z", project="p")
    rec = _Recorder()
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    args.keep_warm_on_failure = False  # legacy always-burn
    iac._execute(args, "warm", iac.build_startup_script(), [],
                 ledger=led, ledger_data=data, resuming=False)
    assert _delete_issued(rec)


# --------------------------------------------------------------------------- #
# Clean PASS / FRACTURE -> burn runs (terminal); ledger cleared.
# --------------------------------------------------------------------------- #
def _wire_clean(iac, monkeypatch, rec, verdict_lines, verdict):
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_boot", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    monkeypatch.setattr(iac, "run_remote_surgery", lambda *a, **k: (0, verdict_lines, verdict))


def test_clean_pass_burns_and_clears_ledger(iac, args, tmp_path, monkeypatch):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    rec = _Recorder()
    _wire_clean(iac, monkeypatch, rec, ["VERDICT: PASS\n"], "PASS")
    rc = iac._execute(args, "n", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    assert rc == 0
    assert _delete_issued(rec), "clean PASS must burn"
    # terminal -> checkpoint cleared.
    assert not (tmp_path / "state.json").exists()


def test_clean_fracture_burns(iac, args, tmp_path, monkeypatch):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    rec = _Recorder()
    _wire_clean(iac, monkeypatch, rec, ["[SOVEREIGN YIELD: CROSS-REPO FRACTURE]\n"], "FRACTURE")
    rc = iac._execute(args, "n", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    assert rc == 0
    assert _delete_issued(rec), "clean FRACTURE must burn"


def test_exception_still_burns(iac, args, tmp_path, monkeypatch):
    """An unrecoverable raised exception ALWAYS burns (cannot prove resumable)."""
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    rec = _Recorder()
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_prebake", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_remote_boot", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("ssh dropped mid-surgery")

    monkeypatch.setattr(iac, "run_remote_surgery", _boom)
    args.keep_warm_on_failure = True  # even with keep-warm, an exception burns
    with pytest.raises(RuntimeError):
        iac._execute(args, "n", iac.build_startup_script(), [],
                     ledger=led, ledger_data=data, resuming=False)
    assert _delete_issued(rec), "a raised exception must still burn the node"


# --------------------------------------------------------------------------- #
# --burn force-cleanup.
# --------------------------------------------------------------------------- #
def test_force_burn_deletes_checkpointed_node(iac, args, tmp_path, monkeypatch):
    led = _seed_ledger(iac, tmp_path, node="leftover", completed=["provisioned"])
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    led2 = iac.CheckpointLedger(str(tmp_path / "state.json"))
    rc = iac._force_burn_checkpointed(args, led2)
    assert rc == 0
    assert _delete_issued(rec)
    assert not (tmp_path / "state.json").exists()  # ledger cleared after burn


def test_force_burn_noop_when_no_checkpoint(iac, args, tmp_path):
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    assert iac._force_burn_checkpointed(args, led) == 0


# --------------------------------------------------------------------------- #
# Synchronous streaming: real-time line-by-line + tee + labels.
# --------------------------------------------------------------------------- #
def test_run_streaming_emits_lines_incrementally_not_all_at_end(iac, monkeypatch):
    """Prove the stream emits EACH line as it is read, not buffered to the end:
    the fake stdout records the emit timeline interleaved with iteration."""
    timeline = []

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = lines
            self._i = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self._i >= len(self._lines):
                raise StopIteration
            line = self._lines[self._i]
            self._i += 1
            timeline.append(("yield", line))
            return line

    class _FakeProc:
        def __init__(self, lines):
            self.stdout = _FakeStdout(lines)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    lines = ["a\n", "b\n", "c\n"]
    monkeypatch.setattr(iac.subprocess, "Popen", lambda cmd, **k: _FakeProc(lines))

    def _sink(line):
        timeline.append(("emit", line))

    rc, captured = iac._run_streaming(["x"], sink=_sink)
    assert rc == 0
    assert captured == lines
    # Each yield is immediately followed by its emit -> incremental, interleaved.
    assert timeline == [
        ("yield", "a\n"), ("emit", "a\n"),
        ("yield", "b\n"), ("emit", "b\n"),
        ("yield", "c\n"), ("emit", "c\n"),
    ]


def test_labeled_sink_prefixes_and_tees_to_log(iac, tmp_path):
    log = tmp_path / "run.log"
    sink = iac._make_labeled_sink("prebake", log)
    sink("Step 3/9 : RUN pip install\n")
    sink("collecting wheels\n")
    contents = log.read_text(encoding="utf-8")
    assert "[prebake] Step 3/9 : RUN pip install" in contents
    assert "[prebake] collecting wheels" in contents


def test_run_streaming_labeled_carries_label_and_tees(iac, tmp_path, monkeypatch, capsys):
    class _FakeProc:
        def __init__(self, lines):
            self.stdout = iter(lines)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(iac.subprocess, "Popen",
                        lambda cmd, **k: _FakeProc(["building layer\n"]))
    log = tmp_path / "tee.log"
    rc, captured = iac._run_streaming_labeled(["docker", "build"], label="prebake", log_path=log)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[prebake] building layer" in out
    assert "[prebake] building layer" in log.read_text(encoding="utf-8")


def test_each_long_phase_streams_with_its_label(iac, args, tmp_path, monkeypatch):
    """provision/sync/prebake/boot/surgery each go through the streaming boundary
    with their phase label."""
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    args.prime_repo_path = "/repos/prime"
    args.reactor_repo_path = "/repos/reactor"
    args.prebake_cmd = "docker build ."  # enable the prebake phase
    args.boot_cmd = "docker compose up -d"  # enable the boot phase
    rec = _Recorder(stream_lines=["VERDICT: PASS\n"])
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    # surgery returns PASS via the stream lines (parse_verdict over stream_lines).
    rc = iac._execute(args, "n", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    # The labels seen across all streamed calls, in order.
    assert "provision" in rec.stream_labels
    assert "sync" in rec.stream_labels
    assert "prebake" in rec.stream_labels
    assert "boot" in rec.stream_labels
    assert "surgery" in rec.stream_labels
    # provision before surgery (order preserved).
    assert rec.stream_labels.index("provision") < rec.stream_labels.index("surgery")


def test_prebake_and_boot_skipped_when_cmd_empty(iac, args, tmp_path, monkeypatch):
    """With empty prebake/boot cmds they fold into surgery -- still checkpointed,
    no separate streamed call issued for them."""
    led = iac.CheckpointLedger(str(tmp_path / "state.json"))
    data = led.init_run(run_id="R", node_name="n", zone="z", project="p")
    args.prebake_cmd = ""
    args.boot_cmd = ""
    rec = _Recorder(stream_lines=["VERDICT: PASS\n"])
    rec.patch(monkeypatch, iac)
    monkeypatch.setattr(iac, "poll_node_ready", lambda *a, **k: (True, ""))
    monkeypatch.setattr(iac, "sync_repos_to_node", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(iac, "run_autopsy", lambda *a, **k: None)
    rc = iac._execute(args, "n", iac.build_startup_script(), [],
                      ledger=led, ledger_data=data, resuming=False)
    assert rc == 0
    assert "prebake" not in rec.stream_labels
    assert "boot" not in rec.stream_labels
    # but the phases are still marked complete (folded) -> ledger cleared on PASS.
    assert not (tmp_path / "state.json").exists()


# --------------------------------------------------------------------------- #
# Dry-run surfaces the checkpoint + streaming plan (spends nothing).
# --------------------------------------------------------------------------- #
def test_dry_run_shows_checkpoint_and_streaming_plan(iac, monkeypatch, capsys):
    rec = _Recorder()
    monkeypatch.setattr(iac, "_run", rec.run)
    monkeypatch.setattr(iac, "_run_streaming_labeled", rec.stream)
    rc = iac.main(["--dry-run", "--prime-repo-path", "/p", "--reactor-repo-path", "/r"])
    assert rc == 0
    assert rec.calls == []  # zero real commands
    out = capsys.readouterr().out
    assert "CHECKPOINT / RESUME PLAN" in out
    assert "STREAMING PLAN" in out
    assert "[provision]" in out
    assert "[prebake]" in out
    assert "NO-ORPHAN BACKSTOP" in out


def test_node_idle_timeout_flows_into_startup_script(iac, monkeypatch, capsys):
    """The resume-aware node idle timeout is baked into the dead-man watchdog."""
    rc = iac.main(["--dry-run", "--node-idle-timeout-s", "3600",
                   "--prime-repo-path", "/p", "--reactor-repo-path", "/r"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "IDLE_TIMEOUT_S=3600" in out
