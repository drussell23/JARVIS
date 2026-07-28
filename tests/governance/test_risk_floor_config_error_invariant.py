"""A refusal is not a failure — and fail-closed must not confuse them.

`apply_floor_to_name` wraps `recommended_floor` in a fail-closed handler so
that an *erroring* subsystem can never open the governance floor. That is
correct. But it caught `Exception`, and Invariant I2 signals an illegal
configuration by RAISING — so the one deliberate refusal in the module was
swallowed by the guard meant to enforce it.

The consequence was live, not theoretical::

    JARVIS_VISION_SENSOR_RISK_FLOOR=safe_auto, MIN_RISK_TIER unset
        → ('safe_auto', None)

A vision-originated op reached `safe_auto`, precisely what I2 forbids. The
loudest misconfiguration in the module travelled its quietest path.

These pin the distinction structurally: a `RiskFloorConfigError` is an
assertion about configuration and must reach the operator, while every other
failure still fails closed.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.risk_tier_floor import (
    RiskFloorConfigError,
    apply_floor_to_name,
    apply_floor_to_risk_tier,
    get_active_tier_order,
)

_ENV_VISION = "JARVIS_VISION_SENSOR_RISK_FLOOR"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # MIN_RISK_TIER unset is the DANGEROUS case, not an edge case: it is the
    # default install, and it is what turned the swallowed error into a
    # silent safe_auto rather than a merely-wrong-but-strict floor.
    for var in (_ENV_VISION, "JARVIS_MIN_RISK_TIER", "JARVIS_PARANOIA_MODE",
                "JARVIS_AUTO_APPLY_QUIET_HOURS"):
        monkeypatch.delenv(var, raising=False)


class TestConfigErrorReachesTheOperator:
    def test_name_path_raises_rather_than_silently_passing(self, monkeypatch):
        monkeypatch.setenv(_ENV_VISION, "safe_auto")
        with pytest.raises(RiskFloorConfigError, match="cannot be lower"):
            apply_floor_to_name("safe_auto", signal_source="vision_sensor")

    def test_config_error_is_a_valueerror(self):
        # Callers written against the documented `ValueError` contract must
        # keep working; the subclass adds precision without breaking them.
        assert issubclass(RiskFloorConfigError, ValueError)

    def test_enum_path_never_raises_but_never_passes_safe_auto(
        self, monkeypatch,
    ):
        # Gate sites depend on "NEVER raises". Honouring that by returning
        # the input unchanged would reopen the hole, so the illegal value is
        # discarded and the undercut invariant applied instead.
        monkeypatch.setenv(_ENV_VISION, "safe_auto")
        out = apply_floor_to_risk_tier(
            RiskTier.SAFE_AUTO, signal_source="vision_sensor",
        )
        assert out is not RiskTier.SAFE_AUTO
        assert out is RiskTier.NOTIFY_APPLY

    def test_enum_path_does_not_weaken_an_already_stricter_tier(
        self, monkeypatch,
    ):
        # Falling back to the hard floor must never DEMOTE an op that already
        # sits above it — the lattice only ever joins upward.
        monkeypatch.setenv(_ENV_VISION, "safe_auto")
        out = apply_floor_to_risk_tier(
            RiskTier.BLOCKED, signal_source="vision_sensor",
        )
        assert out is RiskTier.BLOCKED


class TestFailClosedStillHolds:
    def test_unexpected_failure_still_falls_back_to_env_floor(
        self, monkeypatch,
    ):
        # The Slice 163 guarantee is untouched: a genuinely erroring
        # subsystem must not let an op auto-apply below the configured floor.
        import backend.core.ouroboros.governance.risk_tier_floor as mod

        monkeypatch.setenv("JARVIS_MIN_RISK_TIER", "approval_required")

        def boom(*_a, **_k):
            raise RuntimeError("subsystem down")

        monkeypatch.setattr(mod, "recommended_floor", boom)
        effective, applied = apply_floor_to_name("safe_auto")
        assert effective == "approval_required"
        assert applied == "approval_required"

    def test_non_vision_signal_is_unaffected_by_a_bad_vision_env(
        self, monkeypatch,
    ):
        # The refusal must be scoped to the ops the invariant governs; a bad
        # vision floor cannot become a global outage.
        monkeypatch.setenv(_ENV_VISION, "safe_auto")
        effective, applied = apply_floor_to_name(
            "safe_auto", signal_source="test_failure",
        )
        assert effective == "safe_auto"
        assert applied is None


class TestLatticeOrderingIsStrict:
    def test_order_is_strictly_monotonic_and_total(self):
        # The property the ladder actually has to have: safer-is-higher, no
        # ties. A tie would make "strictest wins" ambiguous and let a
        # composition resolve either way depending on dict order.
        order = get_active_tier_order()
        ladder = ["safe_auto", "notify_apply", "approval_required",
                  "critical_elevation", "blocked"]
        ranks = [order[name] for name in ladder]
        assert ranks == sorted(ranks), "ladder is not monotonic"
        assert len(set(ranks)) == len(ranks), "ladder has ties"

    def test_no_tier_outranks_blocked(self):
        order = get_active_tier_order()
        assert order["blocked"] == max(order.values())

    def test_accessor_returns_a_copy(self):
        # The public accessor exists so callers never touch `_ORDER`; handing
        # out the live dict would make that distinction cosmetic.
        first = get_active_tier_order()
        first["safe_auto"] = 99
        assert get_active_tier_order()["safe_auto"] == 0
