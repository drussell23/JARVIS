"""Seed-truth regression — FlagSpec.default must match runtime is_enabled().

Surfaced by scripts/wave1_advisory_probe.py: after graduating three
Wave 1 master flags, the FlagSpec(default=...) descriptors in their
seed files were left at False while is_enabled() was flipped to True.
Operators viewing /help flag <NAME> saw default=False — a lie.

This regression pins: for every (flag_name, is_enabled_accessor) pair
we care about, the FlagSpec default under no-env-set must match the
accessor's return value. Prevents silent drift on future graduations.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.direction_inferrer import (
    is_enabled as di_is_enabled,
)
from backend.core.ouroboros.governance.flag_registry import (
    FlagRegistry,
    ensure_seeded,
    is_enabled as fr_is_enabled,
    reset_default_registry,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    ensure_bridged,
    is_enabled as mpg_is_enabled,
    reset_default_gate,
)
from backend.core.ouroboros.governance.sensor_governor import (
    ensure_seeded as sg_ensure_seeded,
    is_enabled as sg_is_enabled,
    reset_default_governor,
)


# (flag_name, is_enabled_accessor) pairs we pin. Add entries here when
# new master flags graduate — this test is the doc-truth tripwire.
_GRADUATED_MASTER_FLAGS = [
    ("JARVIS_DIRECTION_INFERRER_ENABLED", di_is_enabled),
    ("JARVIS_FLAG_REGISTRY_ENABLED", fr_is_enabled),
    ("JARVIS_SENSOR_GOVERNOR_ENABLED", sg_is_enabled),
    ("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", mpg_is_enabled),
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all graduated-flag env vars so is_enabled() reports the
    code-level default, not an operator-set override."""
    for flag_name, _ in _GRADUATED_MASTER_FLAGS:
        monkeypatch.delenv(flag_name, raising=False)
    # Also strip any lower-level flags that might shift defaults
    for k in list(os.environ):
        if (k.startswith("JARVIS_FLAG_REGISTRY")
                or k.startswith("JARVIS_SENSOR_GOVERNOR")
                or k.startswith("JARVIS_MEMORY_PRESSURE")
                or k.startswith("JARVIS_DIRECTION_INFERRER")
                or k.startswith("JARVIS_POSTURE")):
            monkeypatch.delenv(k, raising=False)
    reset_default_registry()
    reset_default_governor()
    reset_default_gate()
    yield
    reset_default_registry()
    reset_default_governor()
    reset_default_gate()


@pytest.mark.parametrize("flag_name,is_enabled_fn", _GRADUATED_MASTER_FLAGS)
def test_seed_default_matches_runtime_is_enabled(flag_name, is_enabled_fn):
    """FlagSpec(default=...) in the seed must equal is_enabled() when no
    env var is set. Drift here misleads /help flag <NAME> consumers."""
    # Ensure every module's FlagRegistry registration path has run.
    registry = ensure_seeded()  # installs flag_registry_seed.py specs
    sg_ensure_seeded()           # registers sensor_governor specs
    ensure_bridged()             # registers memory_pressure_gate specs

    spec = registry.get_spec(flag_name)
    assert spec is not None, (
        f"{flag_name} is not registered in the FlagRegistry. "
        "Expected one of flag_registry_seed.SEED_SPECS / "
        "sensor_governor._own_flag_specs / memory_pressure_gate._own_flag_specs."
    )

    runtime_default = is_enabled_fn()
    assert spec.default == runtime_default, (
        f"Seed-truth violation: {flag_name} has "
        f"FlagSpec(default={spec.default!r}) but is_enabled()={runtime_default!r}. "
        f"Graduation flipped the runtime default — update the seed spec to match."
    )


def test_no_env_set_all_graduated_flags_true():
    """Documentary: with no env vars, all four graduated masters are True."""
    for flag_name, is_enabled_fn in _GRADUATED_MASTER_FLAGS:
        assert is_enabled_fn() is True, f"{flag_name} is_enabled() returned False"


def test_seed_defaults_all_true_post_graduation():
    """Documentary: seed defaults for all four graduated flags are True."""
    registry = ensure_seeded()
    sg_ensure_seeded()
    ensure_bridged()
    for flag_name, _ in _GRADUATED_MASTER_FLAGS:
        spec = registry.get_spec(flag_name)
        assert spec is not None
        assert spec.default is True, (
            f"{flag_name} seed default={spec.default!r}; expected True post-graduation"
        )
