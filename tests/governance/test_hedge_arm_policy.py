# tests/governance/test_hedge_arm_policy.py
from __future__ import annotations

from backend.core.ouroboros.governance.dw_transport_hedge import (
    HedgeArmPolicy,
    hedge_policy_resolver_enabled,
    resolve_hedge_arm_policy,
)


def test_write_intent_prefers_fast_even_with_empty_complexity(monkeypatch):
    monkeypatch.delenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_HEDGE_DEFER_STABLE_ENABLED", raising=False)
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is True          # the fail-closed hole is inverted for writes
    assert p.defer_stable is True         # structural double-billing kill
    assert "write_intent" in p.reason


def test_read_only_trivial_keeps_legacy_race(monkeypatch):
    monkeypatch.delenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", raising=False)
    p = resolve_hedge_arm_policy(
        complexity="trivial", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is False
    assert p.defer_stable is False


def test_gate_demanding_complexity_prefers_fast(monkeypatch):
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "true")
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="standard", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is True


def test_resolver_disabled_reverts_to_legacy_s227(monkeypatch):
    monkeypatch.setenv("JARVIS_HEDGE_POLICY_RESOLVER_ENABLED", "false")
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is False         # legacy: "" -> False (byte-identical s227)
    assert p.defer_stable is False
    assert p.reason == "legacy_s227"


def test_defer_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_HEDGE_DEFER_STABLE_ENABLED", "false")
    p = resolve_hedge_arm_policy(
        complexity="moderate", route="background", is_read_only=False,
        target_files=("backend/foo.py",), repo_root=None,
    )
    assert p.prefer_fast is True
    assert p.defer_stable is False        # eager-buffer mode preserved as fallback


def test_never_raises_on_garbage(monkeypatch):
    p = resolve_hedge_arm_policy(
        complexity=None, route=None, is_read_only=None,  # type: ignore[arg-type]
        target_files=None, repo_root=123,  # type: ignore[arg-type]
    )
    assert isinstance(p, HedgeArmPolicy)  # fail-soft, never raises
