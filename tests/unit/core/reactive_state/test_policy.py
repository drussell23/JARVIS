"""Tests for the reactive state policy engine.

Covers PolicyResult construction, individual invariant rule functions
(gcp_offload_requires_ip, gcp_offload_requires_port, hollow_requires_offload),
and PolicyEngine evaluation logic including short-circuit behaviour and the
default builder.
"""
from __future__ import annotations

from typing import Dict

import pytest

from backend.core.reactive_state.types import StateEntry
from backend.core.reactive_state.policy import (
    PolicyResult,
    PolicyEngine,
    build_default_policy_engine,
    gcp_offload_requires_ip,
    gcp_offload_requires_port,
    hollow_requires_offload,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _entry(key: str, value: object, version: int = 1) -> StateEntry:
    return StateEntry(
        key=key,
        value=value,
        version=version,
        epoch=1,
        writer="test",
        origin="explicit",
        updated_at_mono=0.0,
        updated_at_unix_ms=0,
    )


def _snapshot(*entries: StateEntry) -> Dict[str, StateEntry]:
    return {e.key: e for e in entries}


# ── TestPolicyResult ──────────────────────────────────────────────────


class TestPolicyResult:
    """PolicyResult.ok() and PolicyResult.rejected() factory methods."""

    def test_ok_is_allowed(self):
        result = PolicyResult.ok()
        assert result.allowed is True
        assert result.reason is None

    def test_rejected_is_not_allowed(self):
        result = PolicyResult.rejected("missing ip")
        assert result.allowed is False
        assert result.reason == "missing ip"


# ── TestGcpOffloadRequiresIp ─────────────────────────────────────────


class TestGcpOffloadRequiresIp:
    """gcp_offload_requires_ip rejects offload activation without a valid IP."""

    def test_allows_when_ip_present(self):
        snapshot = _snapshot(_entry("gcp.node_ip", "10.0.0.1"))
        result = gcp_offload_requires_ip("gcp.offload_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_when_ip_empty_string(self):
        snapshot = _snapshot(_entry("gcp.node_ip", ""))
        result = gcp_offload_requires_ip("gcp.offload_active", True, snapshot)
        assert result.allowed is False
        assert result.reason is not None

    def test_allows_when_offload_false(self):
        result = gcp_offload_requires_ip("gcp.offload_active", False, {})
        assert result.allowed is True

    def test_ignores_unrelated_keys(self):
        result = gcp_offload_requires_ip("audio.active", True, {})
        assert result.allowed is True

    def test_rejects_when_ip_entry_missing(self):
        result = gcp_offload_requires_ip("gcp.offload_active", True, {})
        assert result.allowed is False
        assert result.reason is not None


# ── TestGcpOffloadRequiresPort ───────────────────────────────────────


class TestGcpOffloadRequiresPort:
    """gcp_offload_requires_port rejects offload activation without a port entry."""

    def test_allows_when_port_present(self):
        snapshot = _snapshot(_entry("gcp.node_port", 8080))
        result = gcp_offload_requires_port("gcp.offload_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_when_port_entry_missing(self):
        result = gcp_offload_requires_port("gcp.offload_active", True, {})
        assert result.allowed is False
        assert result.reason is not None


# ── TestHollowRequiresOffload ────────────────────────────────────────


class TestHollowRequiresOffload:
    """hollow_requires_offload rejects hollow client without offload active."""

    def test_allows_when_offload_active(self):
        snapshot = _snapshot(_entry("gcp.offload_active", True))
        result = hollow_requires_offload("hollow.client_active", True, snapshot)
        assert result.allowed is True

    def test_rejects_when_offload_not_active(self):
        snapshot = _snapshot(_entry("gcp.offload_active", False))
        result = hollow_requires_offload("hollow.client_active", True, snapshot)
        assert result.allowed is False
        assert result.reason is not None

    def test_allows_when_hollow_false(self):
        result = hollow_requires_offload("hollow.client_active", False, {})
        assert result.allowed is True


# ── TestPolicyEngine ─────────────────────────────────────────────────


class TestPolicyEngine:
    """PolicyEngine runs rules and short-circuits on first rejection."""

    def test_empty_engine_allows_all(self):
        engine = PolicyEngine()
        result = engine.evaluate("any.key", "any_value", {})
        assert result.allowed is True

    def test_first_failing_rule_short_circuits(self):
        """When the first rule rejects, subsequent rules are not called."""
        call_log: list[str] = []

        def rule_a(key, value, snapshot):
            call_log.append("a")
            return PolicyResult.rejected("a fails")

        def rule_b(key, value, snapshot):
            call_log.append("b")
            return PolicyResult.ok()

        engine = PolicyEngine()
        engine.add_rule(rule_a)
        engine.add_rule(rule_b)

        result = engine.evaluate("x", 1, {})
        assert result.allowed is False
        assert result.reason == "a fails"
        assert call_log == ["a"]

    def test_all_rules_pass(self):
        def always_ok(key, value, snapshot):
            return PolicyResult.ok()

        engine = PolicyEngine()
        engine.add_rule(always_ok)
        engine.add_rule(always_ok)

        result = engine.evaluate("x", 1, {})
        assert result.allowed is True

    def test_build_default_engine_has_at_least_three_rules(self):
        engine = build_default_policy_engine()
        assert len(engine.rules) >= 3
