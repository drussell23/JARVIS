"""Training Lifecycle Handoff: one exclusive cycle, and the card always comes back."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability import landing_provenance as lp
from backend.core.ouroboros.governance.observability import training_handoff as th
from backend.core.ouroboros.governance.observability import training_trigger as tt


#: The production liveness reader, before any fixture replaces it.
_REAL_ORGANISM_LIVE = th._organism_live


class FakeJPrime:
    """Records every call; the lease is held between acquire and release."""

    def __init__(self, *, repo_url="https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct",
                 restore_failed=None):
        self.calls = []
        self.rejected = []
        self.held = False
        self.repo_url = repo_url
        self.restore_failed = restore_failed or {}

    async def __call__(self, method, path, *, body=None, data=None, headers=None, timeout=None):
        self.calls.append((method, path))
        if path == "/api/show":
            return {"adapters": [{"general.base_model.0.repo_url": self.repo_url}]}
        if path == "/v1/lease/acquire":
            assert not self.held, "a second lease while one is held"
            self.held = True
            return {"token": "tok", "freed_mib": 21240, "lease": {}}
        if path == "/v1/lease/renew":
            return {}
        if path == "/v1/lease/release":
            assert body["token"] == "tok"
            self.held = False
            return {"state": "serving", "reloaded": ["qwen3-coder-ov:30b"], "failed": self.restore_failed}
        if path.endswith("/publish"):
            import hashlib
            assert headers["X-Adapter-SHA256"] == hashlib.sha256(data).hexdigest()
            return {"model": "qwen3-coder-ov:30b", "active": "v2", "previous": "origin"}
        if path.endswith("/rollback"):
            return {"active": "origin"}
        if path.endswith("/reject"):
            self.rejected.append(body)
            return {"model": "qwen3-coder-ov:30b", "rejected": body["version"], "active": "origin"}
        raise AssertionError(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3-coder-ov:30b")
    # The budgets these tests assert on, pinned: importing the launcher
    # script elsewhere in a run applies the production envelope to os.environ.
    monkeypatch.setenv("JARVIS_GRPO_AUTOTRAIN_TIMEOUT_S", "43200")
    monkeypatch.delenv("JARVIS_TRAINING_TIME_RESERVE_S", raising=False)
    monkeypatch.delenv("TRINITY_GRPO_BASE_MODEL", raising=False)
    monkeypatch.delenv("TRINITY_GRPO_TRAIN_CMD", raising=False)
    monkeypatch.setattr(th, "_organism_live", lambda: None)

    async def labels():
        return lp.LabelReport(ref="main", commits=3, proven=3)
    monkeypatch.setattr(lp, "label_landings", labels)
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["preflight"])
    monkeypatch.setattr(tt, "_reactor_root", lambda: tmp_path / "reactor")
    monkeypatch.setattr(tt, "_reactor_python", lambda: "py")

    async def free():
        return 30000
    monkeypatch.setattr(tt, "_gpu_free_mib", free)
    monkeypatch.setattr(th, "_smoke_rows", lambda n: [{"prompt": "task one", "file_path": "a.py"},
                                                      {"prompt": "task two", "file_path": "b.py"}])

    async def steady(model, rows):
        return len(rows), ["ok"] * len(rows)
    monkeypatch.setattr(th, "_smoke", steady)

    async def met(model=None):
        return {"met": True, "unlearned": 20, "threshold": 15, "newest_unlearned": 1791370000.0}
    monkeypatch.setattr(th, "training_yield", met)
    jp = FakeJPrime()
    monkeypatch.setattr(th, "_http", jp)
    return jp, tmp_path


def stages(*, preflight=0, train=0, convert=0, save_adapter=True):
    seen = []

    async def run(cmd, *, timeout_s, cwd=None, env=None, log_path=None):
        seen.append(cmd)
        if cmd[0] == "preflight":
            return preflight, "{}"
        if "run_grpo_training.py" in " ".join(cmd):
            out = Path(cmd[cmd.index("--output-dir") + 1])
            if save_adapter and train == 0:
                (out / "adapter_model.safetensors").write_bytes(b"peft")
                (out / "train_report.json").write_text(json.dumps({"result": {"global_step": 27}}))
            return train, "trainer output"
        if "adapter_gguf.py" in " ".join(cmd):
            if convert == 0:
                Path(cmd[-1]).write_bytes(b"GGUF-adapter")
            return convert, '{"success": true}'
        raise AssertionError(cmd)
    return run, seen


def smoke(scores):
    """Successive _smoke calls return these accepted-task counts:
    baseline (incumbent), then the candidate, then the restoration."""
    it = iter(scores)
    calls = []

    async def fake(model, rows):
        n = next(it)
        calls.append(n)
        return n, [f"{i}:{'ok' if i < n else 'FAIL'}" for i in range(len(rows))]
    fake.calls = calls
    return fake


def cycle():
    return asyncio.run(th.run_cycle(trigger="test"))


def test_happy_path_commits_the_new_adapter_and_returns_the_card(env, monkeypatch):
    jp, _ = env
    run, seen = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([2, 2]))
    out = cycle()
    assert out["state"] == "COMMITTED" and out["adapter_version"] == "v2", out
    assert "2/2 vs incumbent 2/2" in out["outcome"]
    assert out["base_model"] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"   # from the adapter's own header
    assert not jp.held
    paths = [p for _, p in jp.calls]
    assert paths.index("/v1/lease/acquire") < paths.index("/v1/adapters/qwen3-coder-ov:30b/publish") \
        < paths.index("/v1/lease/release")
    train = next(c for c in seen if "run_grpo_training.py" in " ".join(c))
    assert train[train.index("--model") + 1] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    # the trainer is told how long it may step: timeout less the reserve
    assert float(train[train.index("--time-budget-s") + 1]) == 43200.0 - 2700.0


def test_nothing_to_learn_never_touches_the_card(env, monkeypatch):
    jp, _ = env
    run, _ = stages(preflight=2)
    monkeypatch.setattr(tt, "_run", run)
    out = cycle()
    assert out["state"] == "REFUSED" and out["outcome"] == "corpus_not_trainable"
    assert ("POST", "/v1/lease/acquire") not in jp.calls


def test_trainer_failure_still_gives_the_card_back(env, monkeypatch):
    jp, _ = env
    run, _ = stages(train=1)
    monkeypatch.setattr(tt, "_run", run)
    out = cycle()
    assert out["state"] == "FAILED" and "trainer rc=1" in out["outcome"]
    assert not jp.held and ("POST", "/v1/lease/release") in jp.calls


def test_trainer_refusal_is_a_refusal_not_a_failure(env, monkeypatch):
    jp, _ = env
    run, _ = stages(train=2)
    monkeypatch.setattr(tt, "_run", run)
    assert cycle()["state"] == "REFUSED" and not jp.held


def test_unverified_adapter_is_rolled_back_and_the_rollback_verified(env, monkeypatch):
    jp, _ = env
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([2, 1, 2]))      # regressed 2 -> 1, restored to 2
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and "-- verified" in out["outcome"]
    assert jp.rejected and jp.rejected[0]["version"] == "v2"            # weights deleted at J-Prime
    assert "below the incumbent" in jp.rejected[0]["reason"]


def test_adapter_that_fails_to_load_is_rolled_back_without_smoking_it(env, monkeypatch):
    jp, _ = env
    jp.restore_failed = {"qwen3-coder-ov:30b": "llama-server exited rc=1"}
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    s = smoke([2, 2])
    monkeypatch.setattr(th, "_smoke", s)
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and s.calls == [2, 2]   # baseline + restoration only


def test_busy_card_after_lease_aborts_and_releases(env, monkeypatch):
    jp, _ = env
    run, seen = stages()
    monkeypatch.setattr(tt, "_run", run)

    async def busy():
        return 1200
    monkeypatch.setattr(tt, "_gpu_free_mib", busy)
    out = cycle()
    assert out["state"] == "FAILED" and "not free" in out["outcome"]
    assert not jp.held and not any("run_grpo_training.py" in " ".join(c) for c in seen)


def test_live_organism_refuses_before_anything(env, monkeypatch):
    jp, _ = env
    monkeypatch.setattr(th, "_organism_live", lambda: 4242)
    out = cycle()
    assert out["state"] == "REFUSED" and "4242" in out["outcome"] and jp.calls == []


def test_unknown_base_refuses_rather_than_training_the_wrong_model(env, monkeypatch):
    jp, _ = env
    jp.repo_url = ""
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    out = cycle()
    assert out["state"] == "REFUSED" and "TRINITY_GRPO_BASE_MODEL" in out["outcome"]
    assert ("POST", "/v1/lease/acquire") not in jp.calls


def test_cancellation_mid_training_releases_the_lease(env, monkeypatch):
    jp, _ = env

    async def run(cmd, *, timeout_s, cwd=None, env=None, log_path=None):
        if cmd[0] == "preflight":
            return 0, "{}"
        raise asyncio.CancelledError
    monkeypatch.setattr(tt, "_run", run)
    with pytest.raises(asyncio.CancelledError):
        cycle()
    assert not jp.held


def test_second_cycle_is_refused_while_one_runs(env, monkeypatch):
    jp, _ = env
    gate = asyncio.Event()

    async def run(cmd, *, timeout_s, cwd=None, env=None, log_path=None):
        if cmd[0] == "preflight":
            await gate.wait()
            return 2, "{}"
        raise AssertionError

    monkeypatch.setattr(tt, "_run", run)

    async def both():
        first = asyncio.ensure_future(th.run_cycle(trigger="a"))
        await asyncio.sleep(0.1)
        second = await th.run_cycle(trigger="b")
        gate.set()
        return await first, second
    first, second = asyncio.run(both())
    assert second["state"] == "REFUSED" and "another cycle" in second["outcome"]
    assert first["state"] == "REFUSED" and first["outcome"] == "corpus_not_trainable"


def test_every_transition_is_recorded(env, monkeypatch):
    _, tmp = env
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([1, 1]))
    cycle()
    states = [json.loads(l)["state"] for l in (tmp / "state" / "history.jsonl").read_text().splitlines()]
    assert states[:4] == ["LABELING", "PREFLIGHT", "BASELINE", "LEASING"]
    assert states[-1] == "COMMITTED" and th.status()["state"] == "COMMITTED"
    assert "lease_token" not in th.status()


def test_below_the_batch_threshold_refuses_before_any_gpu_work(env, monkeypatch):
    jp, _ = env
    run, seen = stages()
    monkeypatch.setattr(tt, "_run", run)

    async def thin(model=None):
        return {"met": False, "unlearned": 3, "threshold": 15}
    monkeypatch.setattr(th, "training_yield", thin)
    out = cycle()
    assert out["state"] == "REFUSED" and out["outcome"] == "below_training_batch"
    assert seen == [] and ("POST", "/v1/lease/acquire") not in jp.calls


def test_force_bypasses_only_the_yield(env, monkeypatch):
    jp, _ = env
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([1, 1]))

    async def thin(model=None):
        return {"met": False, "unlearned": 3, "threshold": 15}
    monkeypatch.setattr(th, "training_yield", thin)
    out = asyncio.run(th.run_cycle(trigger="t", force=True))
    assert out["state"] == "COMMITTED" and not jp.held


def test_published_source_carries_the_new_training_cutoff(env, monkeypatch):
    jp, _ = env
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([1, 1]))
    sources = []

    async def spy(method, path, **kw):
        if path.endswith("/publish"):
            sources.append(json.loads(kw["headers"]["X-Adapter-Source"]))
        return await jp(method, path, **kw)
    monkeypatch.setattr(th, "_http", spy)
    cycle()
    assert sources[0]["trained_through"] == 1791370000.0


def test_deploy_of_a_corrupt_adapter_is_rejected_and_the_last_good_verified(env, monkeypatch, tmp_path):
    jp, _ = env
    bad = tmp_path / "corrupt.gguf"
    bad.write_bytes(b"GGUF-corrupted-weights")
    monkeypatch.setattr(th, "_smoke", smoke([2, 0, 2]))
    out = asyncio.run(th.run_cycle(trigger="t", deploy_gguf=bad))
    assert out["state"] == "ROLLED_BACK" and "verified" in out["outcome"]
    assert jp.rejected[0]["version"] == "v2" and not jp.held


def test_adapter_that_cannot_load_is_rejected_as_a_load_failure(env, monkeypatch, tmp_path):
    jp, _ = env
    jp.restore_failed = {"qwen3-coder-ov:30b": "llama-server exited rc=1"}
    bad = tmp_path / "truncated.gguf"
    bad.write_bytes(b"GGUF")
    monkeypatch.setattr(th, "_smoke", smoke([2, 2]))          # baseline + restoration only
    out = asyncio.run(th.run_cycle(trigger="t", deploy_gguf=bad))
    assert out["state"] == "ROLLED_BACK" and "failed to load" in jp.rejected[0]["reason"]


def test_verifier_rejects_what_o_v_would_not_act_on():
    with pytest.raises(Exception):
        th._verify_response('{"schema_version": "2b.1", "candidates": [{"candidate_id": "c1", '
                            '"file_path": "x.py", "full_content": "def f(:\\n", "rationale": "r"}]}',
                            {"file_path": "x.py"})
    with pytest.raises(ValueError, match="unknown tool"):
        th._verify_response('{"schema_version": "2b.2-tool", "tool_call": {"name": "rm_rf_everything", '
                            '"arguments": {}}}', {})
    assert th._verify_response('{"schema_version": "2b.2-tool", "tool_call": {"name": "read_file", '
                               '"arguments": {"path": "a.py"}}}', {}) == "tool_calls:read_file"


def test_a_regression_within_the_operators_tolerance_ships(env, monkeypatch):
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setenv("JARVIS_TRAINING_VERIFY_TOLERANCE", "1")
    monkeypatch.setattr(th, "_smoke", smoke([3, 2]))
    assert cycle()["state"] == "COMMITTED"


def test_a_candidate_that_answers_nothing_never_ships_even_against_a_useless_incumbent(env, monkeypatch):
    jp, _ = env
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([0, 0, 0]))
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and jp.rejected


def test_a_rollback_that_does_not_reproduce_the_baseline_is_flagged(env, monkeypatch):
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([2, 0, 1]))
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and "UNVERIFIED" in out["outcome"]
    assert out["detail"]["rolled_back"]["rollback_verified"] is False


# ---------------------------------------------------------------------------
# The requester: a session asks from its own teardown, so it is still alive
# when the cycle starts. 2026-10-07 handoff-20261007-075127 refused pid
# 76605 -- the organism that had just asked for it.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402


def _child(seconds):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _as_organism(monkeypatch, proc, *, stale_name=False):
    """The lock holder is `proc` while it runs. ``stale_name``: the lock file
    keeps naming it after it exits, as a lock file on disk does. Liveness is
    the product's own definition (a zombie is gone), never Popen.poll(), which
    races a reaper thread."""
    monkeypatch.setattr(th, "_organism_live", lambda: proc.pid if stale_name or th._proc_start_ticks(proc.pid)
                        is not None else None)


def _reap_later(proc, after_s):
    import threading
    threading.Timer(after_s, proc.wait).start()


def test_the_requester_in_teardown_is_waited_out_then_the_cycle_runs(env, monkeypatch):
    jp, tmp = env
    proc = _child(1.0)
    _reap_later(proc, 0)
    _as_organism(monkeypatch, proc)
    th._write_request("session_end:bt-x", proc.pid, release_within_s=30)
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([2, 2]))
    t0 = time.monotonic()
    out = cycle()
    assert out["state"] == "COMMITTED", out
    assert time.monotonic() - t0 < 25          # ended on the exit EVENT, not the deadline
    hist = [json.loads(line) for line in (tmp / "state" / "history.jsonl").read_text().splitlines()]
    assert hist[0]["state"] == "AWAITING_REQUESTER" and hist[0]["pid"] == proc.pid
    assert th._read_request() is None          # consumed by the cycle that served it


def test_a_requester_outliving_its_own_deadline_is_refused(env, monkeypatch):
    proc = _child(60)
    try:
        _as_organism(monkeypatch, proc)
        th._write_request("session_end:bt-x", proc.pid, release_within_s=0.5)
        out = cycle()
        assert out["state"] == "REFUSED" and str(proc.pid) in out["outcome"], out
    finally:
        proc.kill()
        proc.wait()


def test_a_different_organism_is_still_refused_without_waiting(env, monkeypatch):
    other = _child(60)
    try:
        _as_organism(monkeypatch, other)
        th._write_request("session_end:bt-x", 1, release_within_s=30)   # pid 1 asked, not `other`
        t0 = time.monotonic()
        out = cycle()
        assert out["state"] == "REFUSED" and str(other.pid) in out["outcome"], out
        assert time.monotonic() - t0 < 5
    finally:
        other.kill()
        other.wait()


def test_a_reused_pid_is_not_the_requester(env, monkeypatch):
    proc = _child(60)
    try:
        _as_organism(monkeypatch, proc)
        rec = th._write_request("session_end:bt-x", proc.pid, release_within_s=30)
        rec["requester_start"] = rec["requester_start"] - 1      # a different process once had this pid
        (th.state_dir() / th._REQUEST_FILE).write_text(json.dumps(rec))
        out = cycle()
        assert out["state"] == "REFUSED", out
    finally:
        proc.kill()
        proc.wait()


def test_an_organism_booting_during_baseline_keeps_its_card(env, monkeypatch):
    jp, _ = env
    seen = {"n": 0}

    def live():
        seen["n"] += 1
        return None if seen["n"] == 1 else 4242    # absent at start, present at LEASING
    monkeypatch.setattr(th, "_organism_live", live)
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    out = cycle()
    assert out["state"] == "REFUSED" and "before the card was taken" in out["outcome"], out
    assert ("POST", "/v1/lease/acquire") not in jp.calls


def test_await_exit_is_event_driven_and_bounded():
    proc = _child(0.3)
    _reap_later(proc, 0)
    start = th._proc_start_ticks(proc.pid)
    assert start is not None
    assert asyncio.run(th._await_exit(proc.pid, start, 20)) is True
    slow = _child(60)
    try:
        t0 = time.monotonic()
        assert asyncio.run(th._await_exit(slow.pid, th._proc_start_ticks(slow.pid), 0.4)) is False
        assert time.monotonic() - t0 < 5
    finally:
        slow.kill()
        slow.wait()


def test_the_request_names_its_requester(env, monkeypatch):
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_LAUNCH_CMD", f"{sys.executable} -c pass")
    out = th.request_cycle(trigger="session_end:bt-y", requester_pid=th.os.getpid(), release_within_s=120)
    assert out["requested"] is True
    rec = th._read_request()
    assert rec["requester_pid"] == th.os.getpid()
    assert rec["requester_start"] == th._proc_start_ticks(th.os.getpid())
    assert 100 < rec["release_by"] - time.time() <= 120


def test_the_session_end_hook_names_this_process_and_its_budget(monkeypatch):
    seen = {}

    def req(**kw):
        seen.update(kw)
        return {"requested": True}

    async def met(model=None):
        return {"met": True}
    monkeypatch.setenv("JARVIS_GRPO_AUTOTRAIN_ENABLED", "true")
    monkeypatch.setattr(th, "request_cycle", req)
    monkeypatch.setattr(th, "training_yield", met)
    out = asyncio.run(tt.maybe_train_after_soak(stop_reason="wall_clock_cap", session_id="s",
                                                release_within_s=777.0))
    assert out["fired"] is True
    assert seen["requester_pid"] == th.os.getpid() and seen["release_within_s"] == 777.0


def test_an_exited_requester_still_named_by_the_lock_file_is_not_live(env, monkeypatch):
    # The REAL liveness path: a lock file on disk naming the requester, read
    # by singleton_lock.read_lock_holder. Signal 0 succeeds on a zombie, so
    # an exited-but-unreaped requester used to read as a live organism.
    _, tmp = env
    root = tmp / "repo"
    (root / ".jarvis").mkdir(parents=True)
    proc = _child(0.5)                         # never reaped until the end: it becomes a zombie
    (root / ".jarvis" / "intake_router.lock").write_text(json.dumps({"pid": proc.pid, "ts": time.time()}))
    from backend.core.ouroboros.cli import thin_client
    monkeypatch.setattr(thin_client, "repo_root", lambda: root)
    monkeypatch.setattr(th, "_organism_live", _REAL_ORGANISM_LIVE)
    assert th._organism_live() == proc.pid     # running: it IS the incumbent
    th._write_request("session_end:bt-z", proc.pid, release_within_s=30)
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([2, 2]))
    try:
        out = cycle()
        assert out["state"] == "COMMITTED", out
        from backend.core.ouroboros.battle_test.singleton_lock import read_lock_holder
        assert read_lock_holder(root)[2] is False      # the zombie holder reads as dead
    finally:
        proc.wait()


def test_a_zombie_is_not_a_running_process():
    proc = _child(0.2)
    start = th._proc_start_ticks(proc.pid)
    assert start is not None
    deadline = time.monotonic() + 10
    while th._proc_start_ticks(proc.pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)                       # exits; NOT reaped -> zombie
    assert th._proc_start_ticks(proc.pid) is None
    proc.wait()


# ---------------------------------------------------------------------------
# A cycle stopped from outside: ABORTED recorded, card returned
# ---------------------------------------------------------------------------

def test_a_cancelled_cycle_records_aborted_and_returns_the_card(env, monkeypatch):
    jp, tmp = env
    started = asyncio.Event()

    async def run(cmd, *, timeout_s, cwd=None, env=None, log_path=None):
        if cmd[0] == "preflight":
            return 0, "{}"
        started.set()
        await asyncio.sleep(3600)                  # the trainer, mid-step

    monkeypatch.setattr(tt, "_run", run)

    async def scenario():
        task = asyncio.ensure_future(th.run_cycle(trigger="t"))
        await asyncio.wait_for(started.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    rows = [json.loads(line) for line in (tmp / "state" / "history.jsonl").read_text().splitlines()]
    assert rows[-1]["state"] == "ABORTED" and rows[-1]["outcome"] == "aborted in TRAINING"
    assert not jp.held and ("POST", "/v1/lease/release") in jp.calls


def test_sigterm_stops_a_cycle_gracefully_instead_of_killing_it(tmp_path):
    marker = tmp_path / "finally-ran"
    script = tmp_path / "child.py"
    script.write_text(
        "import asyncio, sys\n"
        "from backend.core.ouroboros.governance.observability import training_handoff as th\n"
        "async def cycle():\n"
        "    try:\n"
        "        print('running', flush=True)\n"
        "        await asyncio.sleep(3600)\n"
        "    finally:\n"
        f"        open({str(marker)!r}, 'w').write('released')\n"
        "res = asyncio.run(th._until_signalled(cycle()))\n"
        "sys.exit(0 if res is None else 1)\n")
    repo = str(Path(th.__file__).resolve().parents[5])
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, cwd=repo,
                            env={**__import__("os").environ, "PYTHONPATH": repo})
    try:
        assert proc.stdout.readline().strip() == "running"
        proc.terminate()                           # SIGTERM, as a Task Scheduler stop sends
        assert proc.wait(timeout=30) == 0
        assert marker.read_text() == "released"
    finally:
        if proc.poll() is None:
            proc.kill()
