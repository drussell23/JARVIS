"""Regression spine for §41.3 #26 Phase 2 Slice 6 — cost_governor
FlagRegistry seed.

Slice 6 closes the FlagRegistry observability gap at
cost_governor.py: pre-Slice 6, the 17 ``JARVIS_OP_COST_*`` env
knobs lived only in CostGovernorConfig default-factories, with
ZERO entries in the canonical FlagRegistry. Operators had to
grep source to discover them. Slice 6 ships ``register_flags``
that seeds all 17 knobs into the registry — including the new
``JARVIS_OP_COST_ROUTE_INFORMATIONAL`` knob added in Slice 2
that paired the closed-5→6 ProviderRoute expansion.

Pins:

* ``register_flags(registry)`` module-level function present
  (auto-discovered by the canonical FlagRegistry boot-time
  walker — mirrors the convention in fast_path_qa.py,
  sensor_governor.py).
* All 17 canonical JARVIS_OP_COST_* knobs registered.
* The Phase 2 D3b knob (JARVIS_OP_COST_ROUTE_INFORMATIONAL) is
  registered with the canonical default (0.3) + Category.SAFETY
  + description referencing §41.3 #26.
* NEVER raises — each spec registers independently; a single
  bad spec doesn't crash the whole boot.
"""
from __future__ import annotations

from typing import Any, List

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    register_flags,
)


class _CaptureRegistry:
    """Minimal duck-typed registry. Mirrors what the canonical
    FlagRegistry exposes via register(spec)."""

    def __init__(self) -> None:
        self.specs: List[Any] = []

    def register(self, spec: Any) -> None:
        self.specs.append(spec)


# ---------------------------------------------------------------------------
# Function presence + return contract
# ---------------------------------------------------------------------------


def test_register_flags_is_callable():
    """Module-level dispatcher present — the boot-time
    FlagRegistry walker auto-discovers this symbol."""
    assert callable(register_flags)


def test_register_flags_returns_count():
    r = _CaptureRegistry()
    count = register_flags(r)
    assert isinstance(count, int)
    assert count == len(r.specs)


def test_register_flags_registers_at_least_17():
    """Slice 6 ships exactly 17 specs (6 routes + 5 complexities
    + 6 cap-derivation knobs). ``>= 17`` floor permits future
    additive expansion without churning this pin."""
    r = _CaptureRegistry()
    count = register_flags(r)
    assert count >= 17


# ---------------------------------------------------------------------------
# Phase 2 D3b — the INFORMATIONAL knob specifically
# ---------------------------------------------------------------------------


def test_informational_knob_registered():
    r = _CaptureRegistry()
    register_flags(r)
    names = {s.name for s in r.specs}
    assert "JARVIS_OP_COST_ROUTE_INFORMATIONAL" in names


def test_informational_knob_default_matches_substrate():
    """The FlagSpec default must match cost_governor's
    CostGovernorConfig default-factory — drift would mean
    /help flags lies about what operators get."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernorConfig,
    )
    cfg = CostGovernorConfig()
    canonical_default = cfg.route_factors["informational"]
    r = _CaptureRegistry()
    register_flags(r)
    spec = next(
        s for s in r.specs
        if s.name == "JARVIS_OP_COST_ROUTE_INFORMATIONAL"
    )
    assert spec.default == pytest.approx(canonical_default)


def test_informational_knob_description_references_phase_2_d3b():
    """Operator-discoverability: the description must point at
    the design-decision lineage so future operators understand
    why the knob exists."""
    r = _CaptureRegistry()
    register_flags(r)
    spec = next(
        s for s in r.specs
        if s.name == "JARVIS_OP_COST_ROUTE_INFORMATIONAL"
    )
    assert "§41.3" in spec.description
    assert "D3b" in spec.description
    assert "INFORMATIONAL" in spec.description


def test_informational_knob_categorized_as_safety():
    """Cost caps gate spend → SAFETY category (mirrors the
    sibling JARVIS_OP_COST_ROUTE_* knobs)."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
    )
    r = _CaptureRegistry()
    register_flags(r)
    spec = next(
        s for s in r.specs
        if s.name == "JARVIS_OP_COST_ROUTE_INFORMATIONAL"
    )
    assert spec.category is Category.SAFETY


# ---------------------------------------------------------------------------
# Full route-factor family — all 6 routes registered
# ---------------------------------------------------------------------------


def test_all_six_route_factor_knobs_registered():
    """Closed-5→6 expansion at the FlagRegistry layer too.
    Every canonical ProviderRoute value has a matching
    JARVIS_OP_COST_ROUTE_* FlagSpec."""
    r = _CaptureRegistry()
    register_flags(r)
    names = {s.name for s in r.specs}
    expected = {
        "JARVIS_OP_COST_ROUTE_IMMEDIATE",
        "JARVIS_OP_COST_ROUTE_STANDARD",
        "JARVIS_OP_COST_ROUTE_COMPLEX",
        "JARVIS_OP_COST_ROUTE_BACKGROUND",
        "JARVIS_OP_COST_ROUTE_SPECULATIVE",
        "JARVIS_OP_COST_ROUTE_INFORMATIONAL",
    }
    assert expected.issubset(names)


def test_all_route_knob_defaults_match_substrate():
    """Defaults match CostGovernorConfig defaults across all 6
    routes — no drift between the two sources of truth."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernorConfig,
    )
    cfg = CostGovernorConfig()
    r = _CaptureRegistry()
    register_flags(r)
    by_name = {s.name: s for s in r.specs}
    for route, factor in cfg.route_factors.items():
        env_name = (
            f"JARVIS_OP_COST_ROUTE_{route.upper()}"
        )
        assert env_name in by_name, (
            f"route {route!r} has no FlagSpec"
        )
        assert by_name[env_name].default == pytest.approx(factor)


# ---------------------------------------------------------------------------
# Complexity-factor family
# ---------------------------------------------------------------------------


def test_complexity_factor_knobs_registered():
    r = _CaptureRegistry()
    register_flags(r)
    names = {s.name for s in r.specs}
    for c in ("TRIVIAL", "SIMPLE", "LIGHT", "HEAVY", "ARCH"):
        assert f"JARVIS_OP_COST_COMPLEXITY_{c}" in names


# ---------------------------------------------------------------------------
# Cap-derivation knobs
# ---------------------------------------------------------------------------


def test_baseline_and_cap_knobs_registered():
    r = _CaptureRegistry()
    register_flags(r)
    names = {s.name for s in r.specs}
    for n in (
        "JARVIS_OP_BASELINE_COST_USD",
        "JARVIS_OP_RETRY_HEADROOM",
        "JARVIS_OP_COST_MIN_CAP_USD",
        "JARVIS_OP_COST_MAX_CAP_USD",
        "JARVIS_OP_COST_READONLY_FACTOR",
        "JARVIS_OP_COST_PARALLEL_STREAM_FACTOR",
    ):
        assert n in names


# ---------------------------------------------------------------------------
# Defensive — register_flags NEVER raises
# ---------------------------------------------------------------------------


def test_register_flags_swallows_individual_register_failures():
    """A registry that throws on one spec must NOT cause
    register_flags to abandon the rest — each spec registers
    independently."""
    class _PartialFailRegistry:
        def __init__(self) -> None:
            self.attempts = 0

        def register(self, spec: Any) -> None:
            self.attempts += 1
            if "INFORMATIONAL" in spec.name:
                raise RuntimeError("simulated failure")

    r = _PartialFailRegistry()
    count = register_flags(r)
    # Attempted all 17 specs even though one raised.
    assert r.attempts >= 17
    # Count reflects successful registrations only — drops 1.
    assert count == r.attempts - 1


def test_register_flags_returns_zero_when_flag_registry_missing(
    monkeypatch,
):
    """If flag_registry import fails (boot order, partial
    install), register_flags returns 0 — never raises."""
    import sys
    saved = sys.modules.get(
        "backend.core.ouroboros.governance.flag_registry"
    )
    sys.modules[
        "backend.core.ouroboros.governance.flag_registry"
    ] = None  # type: ignore[assignment]
    try:
        r = _CaptureRegistry()
        count = register_flags(r)
        assert count == 0
    finally:
        if saved is not None:
            sys.modules[
                "backend.core.ouroboros.governance.flag_registry"
            ] = saved
        else:
            sys.modules.pop(
                "backend.core.ouroboros.governance.flag_registry",
                None,
            )


# ---------------------------------------------------------------------------
# Source-file provenance
# ---------------------------------------------------------------------------


def test_source_file_set_correctly():
    """Every spec must point at cost_governor.py — operators
    using /help flag <name> see where the knob is defined."""
    r = _CaptureRegistry()
    register_flags(r)
    for spec in r.specs:
        assert spec.source_file.endswith("cost_governor.py")


def test_example_strings_present():
    """Each spec has an example for operator copy-paste."""
    r = _CaptureRegistry()
    register_flags(r)
    for spec in r.specs:
        assert spec.example, f"{spec.name} missing example"
        assert spec.name in spec.example
