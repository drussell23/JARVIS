"""Tests for scripts/chaos_injector.py -- the Failover FSM chaos gauntlet.

TDD against the REAL FailoverLifecycleController with injected fakes. ZERO real
GCE / network / subprocess. The FakeClock + ChaosSchedule are asserted
deterministic; live-mode is asserted to refuse without the triple gate.
"""
from __future__ import annotations

import importlib

import pytest

import scripts.chaos_injector as ci
from backend.core.ouroboros.governance import provider_quarantine as pq


@pytest.fixture(autouse=True)
def _chaos_on(monkeypatch):
    """Most tests want the master gate ON + a fresh gradient singleton."""
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None


# ---------------------------------------------------------------------------
# FakeClock / ChaosSchedule determinism
# ---------------------------------------------------------------------------

def test_fakeclock_advances_deterministically():
    c = ci.FakeClock(start=1000.0)
    assert c() == 1000.0
    c.advance(90.0)
    assert c() == 1090.0
    c.advance(30 * 60.0)
    assert c() == 1090.0 + 1800.0


def test_chaos_schedule_step_changes():
    s = ci.ChaosSchedule(initial_healthy=False)
    s.add(1090.0, True)
    s.add(1200.0, False)
    assert s.is_healthy(1000.0) is False   # before first edge
    assert s.is_healthy(1090.0) is True    # at recovery edge
    assert s.is_healthy(1150.0) is True
    assert s.is_healthy(1200.0) is False   # back down
    # Determinism: same input -> same output.
    assert s.is_healthy(1090.0) is True


def test_chaos_schedule_initial_healthy():
    s = ci.ChaosSchedule(initial_healthy=True)
    assert s.is_healthy(0.0) is True
    s.add(500.0, False)
    assert s.is_healthy(499.0) is True
    assert s.is_healthy(500.0) is False


# ---------------------------------------------------------------------------
# Deadman injection assertion
# ---------------------------------------------------------------------------

def test_assert_deadman_injected_real_script():
    from backend.core.ouroboros.governance.failover_deadman import (
        build_deadman_startup_script,
    )
    script = build_deadman_startup_script(idle_timeout_s=120, boot_grace_s=120)
    ok, detail = ci._assert_deadman_injected(script)
    assert ok, detail
    assert "120" in detail


def test_assert_deadman_injected_rejects_empty():
    ok, _ = ci._assert_deadman_injected("")
    assert ok is False


def test_assert_deadman_injected_rejects_non_deadman():
    ok, _ = ci._assert_deadman_injected("#!/bin/bash\necho hello\n")
    assert ok is False


# ---------------------------------------------------------------------------
# Scenario 1 -- Synthetic Collapse (503 storm) -> SERVING with deadman
# ---------------------------------------------------------------------------

async def test_scenario1_reaches_serving_with_deadman(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    r = await ci._scenario_synthetic_collapse()
    assert r["verdict"] == "PASS", r["detail"]
    assert "SERVING" in r["trajectory"]
    assert "AWAKENING" in r["trajectory"]
    assert "DORMANT" in r["trajectory"]
    # The deadman injection is reported in the detail.
    assert "deadman injected" in r["detail"]


# ---------------------------------------------------------------------------
# Scenario 2 -- Phantom Recovery (THE race) -> DORMANT, bounded, no thrash
# ---------------------------------------------------------------------------

async def test_scenario2_variant_node_ready_before_recovery(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    r = await ci._phantom_variant(node_ready_before_recovery=True)
    assert r["verdict"] == "PASS", r["detail"]
    assert r["trajectory"].endswith("DORMANT")
    assert "AWAKENING" in r["trajectory"]
    assert r["transitions"] <= 6


async def test_scenario2_variant_node_ready_after_recovery(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    r = await ci._phantom_variant(node_ready_before_recovery=False)
    assert r["verdict"] == "PASS", r["detail"]
    assert r["trajectory"].endswith("DORMANT")
    assert "AWAKENING" in r["trajectory"]
    assert r["transitions"] <= 6


async def test_scenario2_both_variants_pass(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    r = await ci._scenario_phantom_recovery()
    assert r["verdict"] == "PASS", r["detail"]
    assert r["variant_a"]["verdict"] == "PASS"
    assert r["variant_b"]["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Scenario 3 -- Assassination (deadman injection proof)
# ---------------------------------------------------------------------------

async def test_scenario3_proves_deadman_injection(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    r = await ci._scenario_assassination()
    assert r["verdict"] == "PASS", r["detail"]
    assert "SERVING" in r["trajectory"]
    assert "deadman injected" in r["detail"]
    # FSM must NOT have issued its own delete while SERVING -- the node-side
    # deadman is the sole orphan backstop.
    assert "no delete while SERVING" in r["detail"]


# ---------------------------------------------------------------------------
# Full gauntlet
# ---------------------------------------------------------------------------

def test_run_gauntlet_all_pass(monkeypatch, capsys):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    results = ci.run_gauntlet()
    assert ci.gauntlet_all_pass(results), results
    assert len(results) == 3
    out = capsys.readouterr().out
    assert "CHAOS GAUNTLET REPORT" in out
    assert "OVERALL: PASS" in out


def test_run_gauntlet_restores_enabled_env(monkeypatch):
    # run_gauntlet flips JARVIS_FAILOVER_LIFECYCLE_ENABLED on for the duration;
    # it must restore the prior value (here: unset).
    monkeypatch.delenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", raising=False)
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    ci.run_gauntlet()
    import os
    assert os.environ.get("JARVIS_FAILOVER_LIFECYCLE_ENABLED") is None


# ---------------------------------------------------------------------------
# Live-mode triple gate -- refuses without all three gates
# ---------------------------------------------------------------------------

def _ns(**kw):
    import argparse
    base = dict(gauntlet=False, live=True, i_understand_this_spends_money=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_live_refuses_without_master_gate(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    rc = ci.run_live_soak(_ns(live=True, i_understand_this_spends_money=True))
    assert rc == 2


def test_live_refuses_without_live_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = ci.run_live_soak(_ns(live=False, i_understand_this_spends_money=True))
    assert rc == 2


def test_live_refuses_without_money_ack(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = ci.run_live_soak(_ns(live=True, i_understand_this_spends_money=False))
    assert rc == 2


def test_live_scaffold_prints_with_triple_gate(monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = ci.run_live_soak(_ns(live=True, i_understand_this_spends_money=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "LIVE SOAK SCAFFOLD" in out
    assert "kill -9" in out
    assert "self-DELETE" in out or "self-delete" in out
    # No real resources provisioned.
    assert "No real resources were provisioned" in out


# ---------------------------------------------------------------------------
# main() master gate
# ---------------------------------------------------------------------------

def test_main_refuses_without_master_gate(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    rc = ci.main(["--gauntlet"])
    assert rc == 2


def test_main_gauntlet_exits_zero_on_all_pass(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    rc = ci.main(["--gauntlet"])
    assert rc == 0


def test_chaos_enabled_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    assert ci.chaos_enabled() is False
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    assert ci.chaos_enabled() is True
