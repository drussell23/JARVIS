"""Tests for ArchitecturalProposal output contract."""
import json
import os
import tempfile

import pytest

from backend.core.topology.architectural_proposal import (
    ArchitecturalProposal,
    ShadowTestResult,
)
from backend.core.topology.curiosity_engine import CuriosityTarget
from backend.core.topology.topology_map import CapabilityNode
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_target():
    node = CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor")
    return CuriosityTarget(
        capability=node, ucb_score=1.5, entropy_score=0.8,
        feasibility_score=1.0, rationale="Domain 'data_io' has Shannon Entropy H=0.918",
    )


class TestShadowTestResult:
    def test_frozen(self):
        r = ShadowTestResult(test_name="test_parse", passed=True, duration_ms=50.0, output="ok")
        with pytest.raises(AttributeError):
            r.passed = False


class TestArchitecturalProposal:
    def test_create_with_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "parser.py")
            with open(f1, "w") as fh:
                fh.write("def parse(): pass\n")
            target = _make_target()
            hw = _make_hardware()
            results = [
                ShadowTestResult(test_name="test_parse", passed=True, duration_ms=50.0, output="ok"),
            ]
            proposal = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=results,
                sentinel_elapsed=120.5,
            )
            assert proposal.capability_name == "parse_parquet"
            assert proposal.capability_domain == "data_io"
            assert proposal.repo_owner == "reactor"
            assert proposal.all_tests_passed is True
            assert len(proposal.proposal_id) > 0
            assert len(proposal.content_hash) == 64

    def test_frozen(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[],
            sentinel_elapsed=10.0,
        )
        with pytest.raises(AttributeError):
            proposal.capability_name = "changed"

    def test_to_json_valid(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[],
            sentinel_elapsed=10.0,
        )
        data = json.loads(proposal.to_json())
        assert data["capability_name"] == "parse_parquet"
        assert "proposal_id" in data
        assert "content_hash" in data

    def test_summary_contains_key_info(self):
        target = _make_target()
        hw = _make_hardware()
        results = [
            ShadowTestResult(test_name="t1", passed=True, duration_ms=50.0, output="ok"),
            ShadowTestResult(test_name="t2", passed=False, duration_ms=30.0, output="fail"),
        ]
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=["a.py", "b.py"], shadow_results=results,
            sentinel_elapsed=300.0,
        )
        summary = proposal.summary()
        assert "parse_parquet" in summary
        assert "reactor" in summary
        assert "1/2" in summary
        assert "2 file(s)" in summary
        assert proposal.all_tests_passed is False

    def test_content_hash_deterministic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "a.py")
            with open(f1, "w") as fh:
                fh.write("x = 1\n")
            target = _make_target()
            hw = _make_hardware()
            p1 = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=[], sentinel_elapsed=10.0,
            )
            p2 = ArchitecturalProposal.create(
                target=target, hardware=hw,
                generated_files=[f1], shadow_results=[], sentinel_elapsed=10.0,
            )
            assert p1.content_hash == p2.content_hash

    def test_hardware_context_captured(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[], sentinel_elapsed=10.0,
        )
        assert proposal.hardware_tier == "local_cpu"
        assert proposal.ram_available_mb == 8192
        assert proposal.gpu_vram_free_mb == 0

    def test_curiosity_provenance(self):
        target = _make_target()
        hw = _make_hardware()
        proposal = ArchitecturalProposal.create(
            target=target, hardware=hw,
            generated_files=[], shadow_results=[], sentinel_elapsed=10.0,
        )
        assert proposal.ucb_score == 1.5
        assert proposal.entropy_score == 0.8
        assert "Shannon Entropy" in proposal.curiosity_rationale
