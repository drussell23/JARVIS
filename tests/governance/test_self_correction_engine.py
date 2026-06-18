"""Tests for the Self-Correction & DPO Alignment Engine.

Covers:
- repair_trajectory_emitter: DPO-pair extraction from converged L2 repairs (provider-labeled),
  gating, fire-and-forget streaming.
- preflight_critic: predicted-failure probability, threshold short-circuit, anti-collapse sampling,
  inert-when-no-model, gating.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.repair_trajectory_emitter import (
    RepairTrajectoryEmitter,
    build_dpo_trajectory,
    emitter_enabled,
)
from backend.core.ouroboros.governance.preflight_critic import (
    PreflightCritic,
    critic_enabled,
)


# --------------------------------------------------------------------------- fixtures
def _ctx(rejected: str = "def f():\n    return 0\n"):
    gen = SimpleNamespace(candidates=[{"file_path": "m.py", "full_content": rejected}],
                          model_id="doubleword-397b", provider_name="doubleword")
    return SimpleNamespace(op_id="op1", generation=gen)


def _result(terminal="L2_CONVERGED", chosen="def f():\n    return 1\n", provider="doubleword"):
    rec = SimpleNamespace(provider_name=provider, failure_class="test")
    return SimpleNamespace(
        terminal=terminal,
        candidate={"file_path": "m.py", "full_content": chosen} if chosen else None,
        stop_reason=None, summary={"provider_name": provider}, iterations=(rec,),
    )


# --------------------------------------------------------------------------- emitter: build
class TestBuildTrajectory:
    def test_converged_pair(self) -> None:
        ev = build_dpo_trajectory(_ctx(), _result())
        assert ev is not None
        assert ev["event_type"] == "correction" and ev["task_type"] == "l2_repair"
        assert ev["provider"] == "doubleword"               # DW-stability labeling
        assert ev["original_response"].endswith("return 0\n")  # rejected
        assert ev["corrected_response"].endswith("return 1\n")  # chosen
        assert "test" in ev["metadata"]["divergence_kinds"]

    def test_not_converged_returns_none(self) -> None:
        assert build_dpo_trajectory(_ctx(), _result(terminal="L2_STOPPED")) is None

    def test_identical_states_returns_none(self) -> None:
        same = "def f():\n    return 0\n"
        assert build_dpo_trajectory(_ctx(rejected=same), _result(chosen=same)) is None

    def test_missing_chosen_returns_none(self) -> None:
        assert build_dpo_trajectory(_ctx(), _result(chosen="")) is None


# --------------------------------------------------------------------------- emitter: gate + send
class TestEmitter:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED", raising=False)
        assert emitter_enabled() is False

    @pytest.mark.asyncio
    async def test_send_uses_client(self) -> None:
        sent: List[Any] = []

        class _Client:
            async def initialize(self): return True
            async def stream_experience(self, ev): sent.append(ev); return True
            async def close(self): return None

        em = RepairTrajectoryEmitter(client=_Client())
        ev = build_dpo_trajectory(_ctx(), _result())
        ok = await em._send(ev)
        assert ok is True and len(sent) == 1 and sent[0]["provider"] == "doubleword"

    @pytest.mark.asyncio
    async def test_send_failsoft(self) -> None:
        class _Boom:
            async def initialize(self): return True
            async def stream_experience(self, ev): raise RuntimeError("reactor down")
            async def close(self): return None
        em = RepairTrajectoryEmitter(client=_Boom())
        assert await em._send({"x": 1}) is False  # no raise

    @pytest.mark.asyncio
    async def test_emit_disabled_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_REPAIR_TRAJECTORY_EMIT_ENABLED", raising=False)
        em = RepairTrajectoryEmitter(client=object())
        assert em.emit(_ctx(), _result()) is False


# --------------------------------------------------------------------------- critic
class TestPreflightCritic:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", raising=False)
        assert critic_enabled() is False

    @pytest.mark.asyncio
    async def test_no_model_is_inert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", "true")
        c = PreflightCritic(infer=None)  # no served critic model
        # _resolve_reactor_infer → None (no critic_infer attr) → inert
        v = await c.evaluate("def f(): pass")
        assert v.failure_probability is None and v.short_circuit is False

    @pytest.mark.asyncio
    async def test_below_threshold_no_shortcircuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", "true")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD", "0.85")
        c = PreflightCritic(infer=lambda src, ctx="": 0.2)
        v = await c.evaluate("x")
        assert v.short_circuit is False and v.failure_probability == 0.2

    @pytest.mark.asyncio
    async def test_above_threshold_shortcircuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", "true")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD", "0.85")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE", "0.0")  # never sample → always gate
        c = PreflightCritic(infer=lambda src, ctx="": 0.95)
        v = await c.evaluate("x")
        assert v.short_circuit is True
        assert "PRE-FLIGHT CRITIC CONSTRAINT" in v.constraint_clause
        assert "2b.1-diff" in v.constraint_clause  # DW structural-failure guidance

    @pytest.mark.asyncio
    async def test_anti_collapse_sampling_lets_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", "true")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_FAIL_THRESHOLD", "0.5")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_SAMPLE_RATE", "1.0")  # always sample → never gate
        c = PreflightCritic(infer=lambda src, ctx="": 0.99, sampler=lambda: 0.0)
        v = await c.evaluate("x")
        assert v.short_circuit is False and "anti_collapse" in v.reason

    @pytest.mark.asyncio
    async def test_inference_failsoft(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_ENABLED", "true")
        def _boom(src, ctx=""):
            raise RuntimeError("model error")
        c = PreflightCritic(infer=_boom)
        v = await c.evaluate("x")
        assert v.failure_probability is None and v.short_circuit is False


# --------------------------------------------------------------------------- M1-native online critic
def _critic(tmp_path):
    from backend.core.ouroboros.governance.preflight_critic import OnlineTopologicalCritic
    return OnlineTopologicalCritic(path=str(tmp_path / "critic.npz"))


class TestOnlineTopologicalCritic:
    def test_featurize_produces_vector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        from backend.core.ouroboros.governance.preflight_critic import featurize
        v = featurize("def f(): return 1", file_path="m.py", graph=None)
        assert v.shape[0] > 4  # code vector ⊕ 4 topology ⊕ bias

    def test_cold_predict_is_none(self, tmp_path) -> None:
        c = _critic(tmp_path)
        assert c.predict_failure("def f(): pass") is None  # no learned signal

    def test_online_learning_separates(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        c = _critic(tmp_path)
        for i in range(120):
            c.learn_pair(f"def b{i}(:\n SYNTAX_ERROR_TOKEN\n", f"def g{i}():\n return {i}\n",
                         file_path="m.py", graph=None)
        assert c.samples() == 240 and c.is_warm() is True
        assert c.windowed_accuracy() >= 0.8
        pf = c.predict_failure("def x(:\n SYNTAX_ERROR_TOKEN\n")
        pp = c.predict_failure("def y():\n return 1\n")
        assert pf is not None and pp is not None and pf > pp  # broken scored riskier than clean

    def test_graduation_gate_requires_samples(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_GRAD_SAMPLES", "50")
        monkeypatch.setenv("JARVIS_PREFLIGHT_CRITIC_GRAD_ACCURACY", "0.7")
        c = _critic(tmp_path)
        assert c.is_graduation_ready() is False  # cold
        for i in range(40):
            c.learn_pair(f"def b{i}(:\n ERR\n", f"def g{i}():\n return {i}\n")
        assert c.is_graduation_ready() is True   # 80 samples ≥ 50, acc high

    def test_persistence_round_trip(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        path = str(tmp_path / "p.npz")
        from backend.core.ouroboros.governance.preflight_critic import OnlineTopologicalCritic
        c1 = OnlineTopologicalCritic(path=path)
        for i in range(20):
            c1.learn_pair(f"def b{i}(:\n ERR\n", f"def g{i}():\n return {i}\n")
        n = c1.samples()
        c2 = OnlineTopologicalCritic(path=path)  # reload from disk
        assert c2.samples() == n and c2.predict_failure("def z(): return 1") is not None

    def test_topology_failsoft_no_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        from backend.core.ouroboros.governance.preflight_critic import _topology_features
        feats = _topology_features("m.py", None)
        assert feats.shape[0] == 4 and float(feats.sum()) == 0.0

    @pytest.mark.asyncio
    async def test_async_learn_and_predict(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_SEMANTIC_EMBEDDER", "stdlib")
        c = _critic(tmp_path)
        for i in range(30):
            await c.alearn_pair(f"def b{i}(:\n ERR\n", f"def g{i}():\n return {i}\n")
        assert await c.apredict_failure("def y(): return 1") is not None
