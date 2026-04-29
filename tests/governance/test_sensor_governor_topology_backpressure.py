"""Slice 3c — SensorGovernor topology-aware backpressure.

When TopologySentinel reports any DW endpoint blocked
(``state in {OPEN, TERMINAL_OPEN}``), the governor applies an
additional multiplier to the weighted cap of BACKGROUND and
SPECULATIVE urgency requests. IMMEDIATE / STANDARD / COMPLEX caps
stay untouched — those routes can fall back to Claude and must keep
firing even with DW down.

Pins:
  §1   topology_backpressure_enabled flag — default true; case-tolerant
  §2   topology_backpressure_mult — default 0.2; case-tolerant; clamped
  §3   _default_topology_state_fn returns () when sentinel unavailable
  §4   __init__ accepts topology_state_fn injection (mirrors posture_fn)
  §5   BG urgency cap reduced when state_fn reports blocked
  §6   SPEC urgency cap reduced when state_fn reports blocked
  §7   IMMEDIATE urgency cap UNCHANGED (cascade route)
  §8   STANDARD urgency cap UNCHANGED (cascade route)
  §9   COMPLEX urgency cap UNCHANGED (cascade route)
  §10  No reduction when state_fn returns ()
  §11  Master flag off → backpressure no-op
  §12  BudgetDecision.topology_blocked field reflects whether factor applied
  §13  reason_code = "governor.topology_backpressure" when topology
       caused per-sensor exhaustion
  §14  reason_code unchanged when topology factor wasn't load-bearing
  §15  Composes with emergency brake (multiplicative)
  §16  state_fn that raises is swallowed (governor never breaks on outage)
  §17  to_dict includes topology_blocked
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.sensor_governor import (
    BudgetDecision,
    SensorBudgetSpec,
    SensorGovernor,
    Urgency,
    topology_backpressure_enabled,
    topology_backpressure_mult,
    _default_topology_state_fn,
)


def _spec(name="X", cap=10):
    return SensorBudgetSpec(sensor_name=name, base_cap_per_hour=cap)


# ===========================================================================
# §1-§2 — Master flag + multiplier env contract
# ===========================================================================


def test_backpressure_default_true(monkeypatch) -> None:
    """Master flag default: env unset → True. Note: this module uses
    the strict ``_env_bool`` convention where empty-string is False
    (not the asymmetric verification/determinism convention)."""
    monkeypatch.delenv("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", raising=False)
    assert topology_backpressure_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_backpressure_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", val)
    assert topology_backpressure_enabled() is True


@pytest.mark.parametrize(
    "val", ["", " ", "0", "false", "no", "off", "garbage"],
)
def test_backpressure_falsy(monkeypatch, val) -> None:
    """Strict convention — empty-string AND whitespace AND any
    non-truthy string returns False. Matches sensor_governor module
    style."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", val)
    assert topology_backpressure_enabled() is False


def test_backpressure_mult_default_0_2(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_TOPOLOGY_BACKPRESSURE_MULT", raising=False)
    assert topology_backpressure_mult() == 0.2


def test_backpressure_mult_clamped_to_1(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_MULT", "5.0")
    assert topology_backpressure_mult() == 1.0


def test_backpressure_mult_floored_at_zero(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_MULT", "-0.5")
    # min=0.0 floor in _env_float
    assert topology_backpressure_mult() == 0.0


# ===========================================================================
# §3-§4 — Defaults + injection
# ===========================================================================


def test_default_topology_state_fn_returns_tuple() -> None:
    """When sentinel isn't engaged or master-off, returns () so
    backpressure is a no-op."""
    result = _default_topology_state_fn()
    assert isinstance(result, tuple)


def test_governor_accepts_topology_state_fn_injection() -> None:
    """Mirror of posture_fn / signal_bundle_fn injection. Mock state-fn."""
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    assert g._topology_state_fn() == ("dw-1",)  # noqa: SLF001


# ===========================================================================
# §5-§9 — Cap math: throttled urgencies vs cascade urgencies
# ===========================================================================


def test_bg_cap_reduced_when_topology_blocked(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_MULT", "0.2")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("DocStaleness", cap=10))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    # 10 * 1.0 (posture) * 0.5 (BG urgency) * 0.2 (topology) = 1.0 → 1
    assert d.weighted_cap == 1
    assert d.topology_blocked is True


def test_spec_cap_reduced_when_topology_blocked(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("IntentDiscovery", cap=20))
    d = g.request_budget("IntentDiscovery", Urgency.SPECULATIVE)
    # 20 * 1.0 * 0.3 (SPEC urgency) * 0.2 (topology) = 1.2 → 1
    assert d.weighted_cap == 1
    assert d.topology_blocked is True


def test_immediate_cap_unchanged_under_topology_block(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("VoiceCommand", cap=10))
    d = g.request_budget("VoiceCommand", Urgency.IMMEDIATE)
    # 10 * 1.0 * 2.0 (IMMEDIATE) = 20; topology factor NOT applied
    assert d.weighted_cap == 20
    assert d.topology_blocked is False


def test_standard_cap_unchanged_under_topology_block(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("S", cap=10))
    d = g.request_budget("S", Urgency.STANDARD)
    # 10 * 1.0 * 1.0 = 10; topology factor NOT applied
    assert d.weighted_cap == 10
    assert d.topology_blocked is False


def test_complex_cap_unchanged_under_topology_block(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("S", cap=10))
    d = g.request_budget("S", Urgency.COMPLEX)
    # 10 * 1.0 * 0.8 = 8; topology factor NOT applied
    assert d.weighted_cap == 8
    assert d.topology_blocked is False


# ===========================================================================
# §10-§11 — No-op paths
# ===========================================================================


def test_no_reduction_when_state_fn_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: (),
    )
    g.register(_spec("DocStaleness", cap=10))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    # 10 * 0.5 (BG) = 5; no topology factor
    assert d.weighted_cap == 5
    assert d.topology_blocked is False


def test_master_flag_off_disables_backpressure(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TOPOLOGY_BACKPRESSURE_ENABLED", "false")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("DocStaleness", cap=10))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    # Master flag off → topology factor not applied even though state_fn
    # reports blocked. 10 * 0.5 (BG) = 5
    assert d.weighted_cap == 5
    assert d.topology_blocked is False


# ===========================================================================
# §12-§14 — Decision field + reason_code
# ===========================================================================


def test_topology_blocked_field_set_when_factor_applied(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("DocStaleness", cap=10))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    assert d.topology_blocked is True
    d2 = g.request_budget("DocStaleness", Urgency.IMMEDIATE)
    assert d2.topology_blocked is False


def test_reason_code_topology_when_topology_caused_exhaustion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("DocStaleness", cap=10))
    # Burn through the cap (BG cap = 10 * 0.5 * 0.2 = 1)
    g.request_budget("DocStaleness", Urgency.BACKGROUND)
    g.record_emission("DocStaleness", Urgency.BACKGROUND)
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    assert d.allowed is False
    assert d.reason_code == "governor.topology_backpressure"
    assert d.topology_blocked is True


def test_reason_code_normal_when_topology_factor_not_applied(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=lambda: (),  # nothing blocked
    )
    g.register(_spec("S", cap=1))
    g.request_budget("S", Urgency.STANDARD)
    g.record_emission("S", Urgency.STANDARD)
    d = g.request_budget("S", Urgency.STANDARD)
    assert d.allowed is False
    assert d.reason_code == "governor.sensor_cap_exhausted"
    assert d.topology_blocked is False


# ===========================================================================
# §15 — Composability with emergency brake
# ===========================================================================


def test_topology_factor_composes_with_emergency_brake(monkeypatch) -> None:
    """Both factors apply multiplicatively. 100 * 0.5 (BG) * 0.2
    (topology) * 0.2 (brake) = 2.0 → 2."""
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SENSOR_GOVERNOR_EMERGENCY_REDUCTION_PCT", "0.2",
    )
    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: {
            "cost_burn_normalized": 1.0,
            "postmortem_failure_rate": 0.0,
        },
        topology_state_fn=lambda: ("dw-1",),
    )
    g.register(_spec("DocStaleness", cap=100))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    assert d.weighted_cap == 2
    assert d.topology_blocked is True
    assert d.emergency_brake is True


# ===========================================================================
# §16 — Defensive — state_fn raises
# ===========================================================================


def test_topology_state_fn_raises_swallowed(monkeypatch) -> None:
    """A sentinel outage must not break the governor — backpressure
    silently disables when state_fn raises."""
    monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")

    def _raise():
        raise RuntimeError("sentinel outage")

    g = SensorGovernor(
        posture_fn=lambda: None,
        signal_bundle_fn=lambda: None,
        topology_state_fn=_raise,
    )
    g.register(_spec("DocStaleness", cap=10))
    d = g.request_budget("DocStaleness", Urgency.BACKGROUND)
    # No backpressure applied (factor swallowed); BG cap = 10 * 0.5 = 5
    assert d.weighted_cap == 5
    assert d.topology_blocked is False


# ===========================================================================
# §17 — to_dict serialisation
# ===========================================================================


def test_to_dict_includes_topology_blocked() -> None:
    d = BudgetDecision(
        allowed=False, sensor_name="X", urgency=Urgency.BACKGROUND,
        posture=None, weighted_cap=1, current_count=1, remaining=0,
        reason_code="governor.topology_backpressure",
        topology_blocked=True,
    )
    payload = d.to_dict()
    assert payload["topology_blocked"] is True
    assert payload["reason_code"] == "governor.topology_backpressure"
