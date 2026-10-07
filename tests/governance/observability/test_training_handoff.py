"""Training Lifecycle Handoff: one exclusive cycle, and the card always comes back."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.observability import landing_provenance as lp
from backend.core.ouroboros.governance.observability import training_handoff as th
from backend.core.ouroboros.governance.observability import training_trigger as tt


class FakeJPrime:
    """Records every call; the lease is held between acquire and release."""

    def __init__(self, *, repo_url="https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct",
                 restore_failed=None):
        self.calls = []
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
        raise AssertionError(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3-coder-ov:30b")
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
    monkeypatch.setattr(th, "_smoke_prompts", lambda n: ["task one", "task two"])
    jp = FakeJPrime()
    monkeypatch.setattr(th, "_http", jp)
    return jp, tmp_path


def stages(*, preflight=0, train=0, convert=0, save_adapter=True):
    seen = []

    async def run(cmd, *, timeout_s, cwd=None, env=None):
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


def smoke(results):
    it = iter(results)

    async def fake(model, prompts):
        ok = next(it)
        return ok, [f"0:{'ok' if ok else 'FAIL'}"]
    return fake


def cycle():
    return asyncio.run(th.run_cycle(trigger="test"))


def test_happy_path_commits_the_new_adapter_and_returns_the_card(env, monkeypatch):
    jp, _ = env
    run, seen = stages()
    monkeypatch.setattr(tt, "_run", run)
    monkeypatch.setattr(th, "_smoke", smoke([True]))
    out = cycle()
    assert out["state"] == "COMMITTED" and out["adapter_version"] == "v2", out
    assert out["base_model"] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"   # from the adapter's own header
    assert not jp.held
    paths = [p for _, p in jp.calls]
    assert paths.index("/v1/lease/acquire") < paths.index("/v1/adapters/qwen3-coder-ov:30b/publish") \
        < paths.index("/v1/lease/release")
    train = next(c for c in seen if "run_grpo_training.py" in " ".join(c))
    assert train[train.index("--model") + 1] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"


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
    monkeypatch.setattr(th, "_smoke", smoke([False, True]))
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and "verified" in out["outcome"]
    assert ("POST", "/v1/adapters/qwen3-coder-ov:30b/rollback") in jp.calls


def test_adapter_that_fails_to_load_is_rolled_back_without_smoking_it(env, monkeypatch):
    jp, _ = env
    jp.restore_failed = {"qwen3-coder-ov:30b": "llama-server exited rc=1"}
    run, _ = stages()
    monkeypatch.setattr(tt, "_run", run)
    calls = []

    async def s(model, prompts):
        calls.append(1)
        return True, ["rollback ok"]
    monkeypatch.setattr(th, "_smoke", s)
    out = cycle()
    assert out["state"] == "ROLLED_BACK" and calls == [1]   # only the rollback was smoked


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

    async def run(cmd, *, timeout_s, cwd=None, env=None):
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

    async def run(cmd, *, timeout_s, cwd=None, env=None):
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
    monkeypatch.setattr(th, "_smoke", smoke([True]))
    cycle()
    states = [json.loads(l)["state"] for l in (tmp / "state" / "history.jsonl").read_text().splitlines()]
    assert states[:3] == ["LABELING", "PREFLIGHT", "LEASING"]
    assert states[-1] == "COMMITTED" and th.status()["state"] == "COMMITTED"
    assert "lease_token" not in th.status()
