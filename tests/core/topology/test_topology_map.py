"""Tests for TopologyMap capability DAG and Shannon Entropy."""
import math

import pytest

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


class TestCapabilityNode:
    def test_defaults(self):
        node = CapabilityNode(name="route_ollama", domain="llm_routing", repo_owner="jarvis")
        assert node.active is False
        assert node.coverage_score == 0.0
        assert node.exploration_attempts == 0


class TestTopologyMap:
    def test_register_and_lookup(self):
        topo = TopologyMap()
        node = CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor")
        topo.register(node)
        assert "parse_csv" in topo.nodes
        assert topo.nodes["parse_csv"].domain == "data_io"

    def test_edges_initialized_on_register(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="jarvis"))
        assert "a" in topo.edges
        assert topo.edges["a"] == set()

    def test_all_domains(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="llm", repo_owner="jarvis"))
        topo.register(CapabilityNode(name="b", domain="vision", repo_owner="prime"))
        assert topo.all_domains() == frozenset({"llm", "vision"})

    def test_domain_coverage_empty_domain(self):
        topo = TopologyMap()
        assert topo.domain_coverage("nonexistent") == 1.0

    def test_domain_coverage_all_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=True))
        assert topo.domain_coverage("d") == 1.0

    def test_domain_coverage_half(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        assert topo.domain_coverage("d") == 0.5

    def test_domain_coverage_none_active(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        assert topo.domain_coverage("d") == 0.0

    def test_entropy_fully_known(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        assert topo.entropy_over_domain("d") == 0.0

    def test_entropy_fully_unknown(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=False))
        assert topo.entropy_over_domain("d") == 0.0

    def test_entropy_maximum_at_half(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        assert topo.entropy_over_domain("d") == pytest.approx(1.0)

    def test_entropy_nonexistent_domain(self):
        topo = TopologyMap()
        assert topo.entropy_over_domain("nope") == 0.0

    def test_entropy_intermediate(self):
        topo = TopologyMap()
        topo.register(CapabilityNode(name="a", domain="d", repo_owner="j", active=True))
        topo.register(CapabilityNode(name="b", domain="d", repo_owner="j", active=False))
        topo.register(CapabilityNode(name="c", domain="d", repo_owner="j", active=False))
        p = 1.0 / 3.0
        expected_h = -p * math.log2(p) - (1 - p) * math.log2(1 - p)
        assert topo.entropy_over_domain("d") == pytest.approx(expected_h)

    def test_feasible_cpu_only_rejects_gpu_capability(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is False

    def test_feasible_with_gpu(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        gpu = GPUState(name="L4", vram_total_mb=24576, vram_free_mb=20000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is True

    def test_feasible_low_vram_rejects(self):
        topo = TopologyMap()
        gpu_node = CapabilityNode(name="vision_gpu_ocr", domain="vision", repo_owner="prime")
        gpu = GPUState(name="L4", vram_total_mb=4096, vram_free_mb=2000, driver_version="535.0")
        hw = _make_hardware(gpu=gpu, tier=ComputeTier.CLOUD_GPU)
        assert topo.feasible_for_hardware(gpu_node, hw) is False

    def test_feasible_cpu_capability_always_ok(self):
        topo = TopologyMap()
        cpu_node = CapabilityNode(name="parse_csv", domain="data_io", repo_owner="reactor")
        hw = _make_hardware(gpu=None, tier=ComputeTier.LOCAL_CPU)
        assert topo.feasible_for_hardware(cpu_node, hw) is True
