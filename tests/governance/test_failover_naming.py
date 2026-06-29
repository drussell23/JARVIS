"""Cryptographic Asset Namespacing -- eradicate dual-node GCP naming collisions.

To run CPU + GPU node lifecycles concurrently, every ephemeral VM and firewall
rule MUST carry a cryptographically deterministic, class-distinct namespace
suffix. CPU and GPU names are mathematically guaranteed to differ (different
hash input) AND each is deterministic (reproducible for teardown without storing
the name). All names are valid GCE resource names.
"""
from __future__ import annotations

import re

import pytest

from backend.core.ouroboros.governance.failover_naming import (
    firewall_name,
    node_name,
)

_GCE_NAME = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_NAMESPACE_SALT", raising=False)
    monkeypatch.delenv("JARVIS_FAILOVER_NODE_NAME", raising=False)
    yield


def test_cpu_and_gpu_node_names_differ():
    assert node_name("cpu") != node_name("gpu")


def test_cpu_and_gpu_firewall_names_differ():
    assert firewall_name("cpu") != firewall_name("gpu")


def test_node_and_firewall_names_differ_within_class():
    assert node_name("gpu") != firewall_name("gpu")


def test_names_are_deterministic():
    # Reproducible -> teardown can reconstruct the name without persisting it.
    assert node_name("cpu") == node_name("cpu")
    assert firewall_name("gpu") == firewall_name("gpu")


def test_class_label_is_human_readable_in_name():
    assert "cpu" in node_name("cpu")
    assert "gpu" in node_name("gpu")


def test_names_are_valid_gce_resource_names():
    for nm in (node_name("cpu"), node_name("gpu"),
               firewall_name("cpu"), firewall_name("gpu")):
        assert _GCE_NAME.match(nm), nm
        assert len(nm) <= 63


def test_salt_changes_the_suffix(monkeypatch):
    a = node_name("cpu")
    monkeypatch.setenv("JARVIS_FAILOVER_NAMESPACE_SALT", "different-deployment")
    b = node_name("cpu")
    assert a != b  # a different deployment salt -> a different namespace


def test_suffix_is_a_short_hex_hash():
    nm = node_name("gpu")
    suffix = nm.rsplit("-", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{8}", suffix)  # 8-char sha256 prefix


def test_unknown_class_still_valid_and_distinct():
    assert _GCE_NAME.match(node_name("speculative"))
    assert node_name("speculative") != node_name("cpu")
