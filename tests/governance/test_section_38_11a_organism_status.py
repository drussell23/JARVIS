"""Section 38.11-A (PRD v2.63 to v2.64, 2026-05-07) -
organism-status indicators regression spine.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11a(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", raising=False,
    )
    for sub in (
        "JARVIS_ORGANISM_STATUS_RISK_LIGHT_ENABLED",
        "JARVIS_ORGANISM_STATUS_TIME_PRESENCE_ENABLED",
        "JARVIS_ORGANISM_STATUS_HEARTBEAT_ENABLED",
    ):
        monkeypatch.delenv(sub, raising=False)
    from backend.core.ouroboros.governance import (
        organism_status as os_,
    )
    os_.reset_heartbeat_for_tests()
    yield
    os_.reset_heartbeat_for_tests()


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.organism_status import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.organism_status import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_ORGANISM_STATUS_ENABLED", value)
    assert master_enabled() is True


# RiskTierLight taxonomy


def test_risk_tier_light_taxonomy_4_values():
    from backend.core.ouroboros.governance.organism_status import (
        RiskTierLight,
    )
    assert {m.name for m in RiskTierLight} == {
        "GREEN", "YELLOW", "ORANGE", "RED",
    }


# compute_risk_light


@pytest.mark.parametrize(
    "floor,gov,expected",
    [
        (None, False, "green"),
        ("", False, "green"),
        ("safe_auto", False, "green"),
        ("notify_apply", False, "yellow"),
        ("approval_required", False, "orange"),
        ("blocked", False, "red"),
        # Governor emergency overrides everything
        ("safe_auto", True, "red"),
        ("notify_apply", True, "red"),
        # Unknown defaults to green (safe)
        ("weird_value", False, "green"),
    ],
)
def test_compute_risk_light(floor, gov, expected):
    from backend.core.ouroboros.governance.organism_status import (
        compute_risk_light,
    )
    rl = compute_risk_light(
        floor_name=floor, governor_emergency=gov,
    )
    assert rl.value == expected


def test_compute_risk_light_defensive_on_bad_inputs():
    from backend.core.ouroboros.governance.organism_status import (
        compute_risk_light,
    )
    # NEVER raises.
    rl = compute_risk_light(floor_name=None, governor_emergency=False)
    assert rl is not None


# format_risk_tier_badge


def test_format_risk_badge_master_off_returns_empty():
    from backend.core.ouroboros.governance.organism_status import (
        format_risk_tier_badge, RiskTierLight,
    )
    assert format_risk_tier_badge(
        plain=True, light=RiskTierLight.GREEN,
    ) == ""


def test_format_risk_badge_plain(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_risk_tier_badge, RiskTierLight,
    )
    assert format_risk_tier_badge(
        plain=True, light=RiskTierLight.GREEN,
    ) == "● GREEN"
    assert format_risk_tier_badge(
        plain=True, light=RiskTierLight.RED,
    ) == "● RED"


def test_format_risk_badge_rich_markup(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_risk_tier_badge, RiskTierLight,
    )
    assert format_risk_tier_badge(
        plain=False, light=RiskTierLight.GREEN,
    ) == "[green]● GREEN[/green]"
    assert format_risk_tier_badge(
        plain=False, light=RiskTierLight.YELLOW,
    ) == "[yellow]● YELLOW[/yellow]"
    assert format_risk_tier_badge(
        plain=False, light=RiskTierLight.ORANGE,
    ) == "[orange3]● ORANGE[/orange3]"
    assert format_risk_tier_badge(
        plain=False, light=RiskTierLight.RED,
    ) == "[red]● RED[/red]"


def test_read_current_risk_light_safe():
    """Compose canonical risk_tier_floor — NEVER raises."""
    from backend.core.ouroboros.governance.organism_status import (
        read_current_risk_light_safe,
    )
    light = read_current_risk_light_safe()
    assert light is not None


# format_time_of_presence


def test_format_time_of_presence_master_off():
    from backend.core.ouroboros.governance.organism_status import (
        format_time_of_presence,
    )
    assert format_time_of_presence(
        session_started_unix=time.time() - 100,
        op_count=5,
        cost_spent_usd=0.05,
        cost_budget_usd=0.50,
        posture_label="EXPLORE",
    ) == ""


def test_format_time_of_presence_full(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_time_of_presence,
    )
    now = time.time()
    out = format_time_of_presence(
        session_started_unix=now - 4 * 3600 - 12 * 60,
        op_count=23,
        cost_spent_usd=0.12,
        cost_budget_usd=0.50,
        posture_label="CONSOLIDATE",
        now_unix=now,
    )
    assert "alive" in out
    assert "4h12m" in out
    assert "23 ops" in out
    assert "$0.12" in out
    assert "$0.50" in out
    assert "CONSOLIDATE" in out


def test_format_time_of_presence_short_session(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_time_of_presence,
    )
    now = time.time()
    out = format_time_of_presence(
        session_started_unix=now - 45,
        op_count=1,
        cost_spent_usd=0.01,
        cost_budget_usd=0.50,
        posture_label="EXPLORE",
        now_unix=now,
    )
    assert "45s" in out
    assert "1 op" in out  # singular


def test_format_time_of_presence_no_optionals(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_time_of_presence,
    )
    now = time.time()
    # Just session uptime + no ops, no cost, no posture.
    out = format_time_of_presence(
        session_started_unix=now - 60,
        op_count=0,
        cost_spent_usd=0.0,
        cost_budget_usd=0.0,
        posture_label="",
        now_unix=now,
    )
    assert "alive" in out
    # No "ops" / "$" / posture.
    assert "ops" not in out
    assert "$" not in out


def test_format_duration_handles_days(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_time_of_presence,
    )
    now = time.time()
    out = format_time_of_presence(
        session_started_unix=now - (2 * 86400 + 3 * 3600),
        op_count=100,
        cost_spent_usd=0.0,
        cost_budget_usd=0.0,
        posture_label="",
        now_unix=now,
    )
    assert "d" in out  # contains day token


# OrganismHeartbeat


def test_heartbeat_master_off_returns_empty():
    from backend.core.ouroboros.governance.organism_status import (
        OrganismHeartbeat,
    )
    hb = OrganismHeartbeat()
    assert hb.pulse() == ""


def test_heartbeat_active_alternates(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        OrganismHeartbeat,
    )
    hb = OrganismHeartbeat()
    f1 = hb.pulse(ops_per_min=5.0)
    f2 = hb.pulse(ops_per_min=5.0)
    f3 = hb.pulse(ops_per_min=5.0)
    # Non-empty, alternating.
    assert f1
    assert f2
    assert f1 != f2 or f2 != f3


def test_heartbeat_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        OrganismHeartbeat,
    )
    hb = OrganismHeartbeat()
    hb.pulse(ops_per_min=3.0)
    hb.pulse(ops_per_min=3.0)
    st = hb.status()
    assert st["tick"] == 2
    assert st["ops_per_min"] == 3.0


def test_heartbeat_reset(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        OrganismHeartbeat,
    )
    hb = OrganismHeartbeat()
    hb.pulse()
    hb.pulse()
    hb.reset()
    assert hb.status()["tick"] == 0


def test_heartbeat_singleton(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        get_default_heartbeat,
    )
    h1 = get_default_heartbeat()
    h2 = get_default_heartbeat()
    assert h1 is h2


# Composite render


def test_composite_master_off_returns_empty():
    from backend.core.ouroboros.governance.organism_status import (
        format_organism_status_line,
    )
    assert format_organism_status_line(
        session_started_unix=time.time() - 100,
        op_count=5,
    ) == ""


def test_composite_master_on_renders_all_3(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_organism_status_line,
        reset_heartbeat_for_tests,
    )
    reset_heartbeat_for_tests()
    now = time.time()
    out = format_organism_status_line(
        session_started_unix=now - 1000,
        op_count=5,
        cost_spent_usd=0.05,
        cost_budget_usd=0.50,
        ops_per_min=2.0,
        posture_label="EXPLORE",
        now_unix=now,
    )
    # Heartbeat glyph + alive + ops + cost + posture + risk badge.
    assert out
    assert "alive" in out
    assert "5 ops" in out
    assert "EXPLORE" in out
    # Risk-tier badge appears as last segment.
    assert (
        "● GREEN" in out or "● YELLOW" in out
        or "● ORANGE" in out or "● RED" in out
    )


# Sub-flag granularity


def test_sub_flag_disables_heartbeat(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_HEARTBEAT_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.organism_status import (
        OrganismHeartbeat,
    )
    hb = OrganismHeartbeat()
    assert hb.pulse() == ""


def test_sub_flag_disables_risk_light(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_RISK_LIGHT_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.organism_status import (
        format_risk_tier_badge, RiskTierLight,
    )
    assert format_risk_tier_badge(
        plain=True, light=RiskTierLight.GREEN,
    ) == ""


# AST pins


def _organism_pins():
    from backend.core.ouroboros.governance.organism_status import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _organism_source():
    return Path(
        "backend/core/ouroboros/governance/organism_status.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _organism_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _organism_pins()
    src = _organism_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires():
    pins = _organism_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires():
    pins = _organism_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_taxonomy_fires_on_missing():
    pins = _organism_pins()
    pin = next(
        p for p in pins
        if "risk_tier_light_taxonomy_4_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class RiskTierLight(str, enum.Enum):\n"
        "    GREEN = 'green'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_risk_tier_floor_fires():
    pins = _organism_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_risk_tier_floor" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_heartbeat_fires():
    pins = _organism_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_heartbeat" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.organism_status import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 3 sub-flags.
    assert n == 4


# Composition


def test_canonical_risk_tier_floor_importable():
    from backend.core.ouroboros.governance.risk_tier_floor import (
        recommended_floor,
    )
    assert callable(recommended_floor)


def test_canonical_polish_bundle_heartbeat_importable():
    from backend.core.ouroboros.governance.polish_bundle import (
        format_heartbeat,
    )
    assert callable(format_heartbeat)
