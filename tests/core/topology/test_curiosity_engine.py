"""Tests for CuriosityEngine — Shannon Entropy + UCB1 target selection."""
import math

import pytest

from backend.core.topology.curiosity_engine import (
    CuriosityEngine,
    CuriosityTarget,
    UCB_EXPLORATION_CONSTANT,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap
from backend.core.topology.hardware_env import (
    ComputeTier,
    GPUState,
    HardwareEnvironmentState,
)


def _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU):
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=tier, gpu=gpu,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _populated_topology():
    topo = TopologyMap()
    topo.register(CapabilityNode(name="route_ollama", domain="llm_routing", repo_owner="jarvis", active=True))
    topo.register(CapabilityNode(name="route_claude", domain="llm_routing", repo_owner="jarvis", active=False))
    topo.register(CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor", active=True))
    topo.register(CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor", active=False))
    topo.register(CapabilityNode(name="vision_ocr", domain="vision", repo_owner="prime", active=False))
    return topo


class TestCuriosityTarget:
    def test_frozen(self):
        node = CapabilityNode(name="a", domain="d", repo_owner="j")
        target = CuriosityTarget(
            capability=node, ucb_score=1.5, entropy_score=0.8,
            feasibility_score=1.0, rationale="test",
        )
        with pytest.raises(AttributeError):
            target.ucb_score = 2.0


class TestCuriosityEngine:
    def test_select_target_returns_highest_ucb(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert target is not None
        assert isinstance(target, CuriosityTarget)
        assert target.capability.active is False

    def test_select_target_skips_active_capabilities(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        for node, score in scored:
            assert node.active is False

    def test_select_target_skips_infeasible(self):
        topo = _populated_topology()
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        names = [n.name for n, _ in scored]
        assert "vision_ocr" not in names

    def test_select_target_includes_gpu_cap_with_gpu(self):
        topo = _populated_topology()
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        names = [n.name for n, _ in scored]
        assert "vision_ocr" in names

    def test_select_target_none_when_all_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        assert engine.select_target() is None

    def test_select_target_none_when_empty(self):
        topo = TopologyMap()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        assert engine.select_target() is None

    def test_ucb_exploration_bonus_decreases_with_attempts(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(
            name="a", domain="d", repo_owner="j", active=False, exploration_attempts=1,
        ))
        topo.register(CapabilityNode(
            name="b", domain="d", repo_owner="j", active=False, exploration_attempts=10,
        ))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = dict(engine.score_all())
        assert scored[topo.nodes["a"]] > scored[topo.nodes["b"]]

    def test_rationale_contains_math(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert "Shannon Entropy" in target.rationale
        assert "UCB=" in target.rationale
        assert "coverage=" in target.rationale

    def test_score_all_sorted_descending(self):
        topo = _populated_topology()
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = engine.score_all()
        scores = [s for _, s in scored]
        assert scores == sorted(scores, reverse=True)

    def test_laplace_smoothing_prevents_div_zero(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        target = engine.select_target()
        assert target is not None
        assert math.isfinite(target.ucb_score)

    def test_dependency_feasibility(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="base", domain="d", repo_owner="j", active=False))
        topo.register(CapabilityNode(name="dependent", domain="d", repo_owner="j", active=False))
        topo.edges["dependent"] = {"base"}
        hw = _make_hardware()
        engine = CuriosityEngine(topo, hw)
        scored = dict((n.name, s) for n, s in engine.score_all())
        assert scored["base"] > scored.get("dependent", 0.0)
