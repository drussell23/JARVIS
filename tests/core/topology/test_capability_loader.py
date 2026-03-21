"""Tests for capability_loader — YAML to TopologyMap."""
import math
import tempfile
from pathlib import Path

import pytest
import yaml

from backend.core.topology.capability_loader import load_topology, DEFAULT_CAPABILITIES_PATH
from backend.core.topology.topology_map import TopologyMap


class TestLoadTopology:
    def test_loads_default_yaml(self):
        topo = load_topology()
        assert len(topo.nodes) > 0
        assert len(topo.all_domains()) >= 5

    def test_all_nodes_have_required_fields(self):
        topo = load_topology()
        for name, node in topo.nodes.items():
            assert node.name, f"Node missing name"
            assert node.domain, f"Node {name} missing domain"
            assert node.repo_owner in ("jarvis", "prime", "reactor"), (
                f"Node {name} has invalid repo_owner: {node.repo_owner}"
            )

    def test_has_active_and_inactive_capabilities(self):
        topo = load_topology()
        active = [n for n in topo.nodes.values() if n.active]
        inactive = [n for n in topo.nodes.values() if not n.active]
        assert len(active) > 0, "Need at least one active capability"
        assert len(inactive) > 0, "Need at least one inactive capability for CuriosityEngine"

    def test_dependencies_wired_as_edges(self):
        topo = load_topology()
        # voice_anti_spoofing depends on voice_auth_ecapa
        assert "voice_auth_ecapa" in topo.edges.get("voice_anti_spoofing", set())

    def test_dependency_targets_exist(self):
        topo = load_topology()
        for name, deps in topo.edges.items():
            for dep in deps:
                assert dep in topo.nodes, (
                    f"Node {name} depends on {dep} which doesn't exist"
                )

    def test_entropy_nonzero_for_mixed_domains(self):
        """Domains with both active and inactive capabilities should have nonzero entropy."""
        topo = load_topology()
        nonzero_domains = []
        for domain in topo.all_domains():
            h = topo.entropy_over_domain(domain)
            if h > 0:
                nonzero_domains.append(domain)
        assert len(nonzero_domains) >= 3, (
            f"Expected at least 3 domains with nonzero entropy, got {nonzero_domains}"
        )

    def test_missing_file_returns_empty(self):
        topo = load_topology(Path("/nonexistent/file.yaml"))
        assert len(topo.nodes) == 0

    def test_empty_yaml_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({}, f)
            f.flush()
            topo = load_topology(Path(f.name))
        assert len(topo.nodes) == 0

    def test_custom_yaml(self):
        data = {
            "capabilities": [
                {"name": "test_cap", "domain": "test", "repo_owner": "jarvis", "active": True},
                {"name": "test_cap2", "domain": "test", "repo_owner": "jarvis", "active": False,
                 "dependencies": ["test_cap"]},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            topo = load_topology(Path(f.name))
        assert len(topo.nodes) == 2
        assert "test_cap" in topo.edges.get("test_cap2", set())

    def test_default_yaml_path_exists(self):
        assert DEFAULT_CAPABILITIES_PATH.exists(), (
            f"capabilities.yaml not found at {DEFAULT_CAPABILITIES_PATH}"
        )

    def test_domain_count(self):
        topo = load_topology()
        domains = topo.all_domains()
        expected = {"llm_routing", "voice", "vision", "governance",
                    "infrastructure", "neural_mesh", "data_io", "exploration"}
        assert domains == expected, f"Expected {expected}, got {domains}"
