"""Every sensor class must actually be BUILT somewhere.

The defect this closes: `MemoryHygieneSensor` and `CageHygieneSensor` were
registered in all three source registries (`_VALID_SOURCES`,
`SignalSource`, `_BACKGROUND_SOURCES`), flag-gated, lifecycle-matched to
`DocStalenessSensor`, and covered by 45 passing tests — and never
constructed. Turning their flags on could not have worked.

Nothing structurally required them to exist. A source token is accepted by
three registries, none of which asks whether anything builds the sensor that
emits it, so "registered" and "running" are different facts that read
identically from every surface an operator can see.

Their own tests could not catch it either: a unit test constructs the sensor
itself, so it proves the class works and says nothing about whether
production ever calls it. This asserts the fact the unit tests structurally
cannot.

The same shape as `test_synthesizer_is_reachable_from_a_production_unit` for
the swarm — reachability is a property of the CALLERS, and has to be
asserted from outside the thing being called.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

OUROBOROS = Path(__file__).resolve().parents[2] / "backend/core/ouroboros"
SENSOR_DIR = OUROBOROS / "governance/intake/sensors"


def _sensor_classes() -> dict:
    """``{ClassName: defining_filename}`` for every ``*Sensor`` in the package.

    Discovered by walking the directory rather than from a hand-kept list —
    a new sensor is covered the moment it is written, which is the only way
    a guard like this stays true.
    """
    out = {}
    for path in sorted(SENSOR_DIR.glob("*_sensor.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Sensor"):
                out[node.name] = path.name
    return out


def _construction_sites(name: str, defining_file: str) -> list:
    """Production files that CALL ``name(...)``.

    Excludes the defining module (a class referencing itself proves nothing)
    and anything under a test path (a test building its own subject proves
    the class works, not that production uses it).
    """
    hits = []
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
    for path in OUROBOROS.rglob("*.py"):
        if path.name == defining_file or "test" in path.name:
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(OUROBOROS)))
        except OSError:
            continue
    return hits


def test_every_sensor_class_has_a_production_construction_site():
    """THE guard. A sensor nothing builds is a sensor that cannot fire."""
    sensors = _sensor_classes()
    assert sensors, "no sensor classes discovered — the walk is broken"
    inert = {
        name: defining
        for name, defining in sensors.items()
        if not _construction_sites(name, defining)
    }
    assert not inert, (
        "these sensor classes are never constructed in production, so their "
        "flags cannot do anything:\n  "
        + "\n  ".join(f"{n}  ({f})" for n, f in sorted(inert.items()))
    )


@pytest.mark.parametrize("sensor", ["MemoryHygieneSensor", "CageHygieneSensor"])
def test_the_two_that_shipped_inert_are_registered_by_the_intake_layer(sensor):
    """Pins WHERE, not just THAT.

    A construction site in some unrelated helper would satisfy the guard
    above while leaving the sensor out of the layer that actually starts it.
    `IntakeLayerService` is the canonical registrar — the same place
    `DocStalenessSensor` is built.
    """
    src = (OUROBOROS / "governance/intake/intake_layer_service.py").read_text(
        encoding="utf-8")
    assert re.search(rf"\b{sensor}\s*\(", src), (
        f"{sensor} is not constructed by IntakeLayerService")
    assert "self._sensors.append" in src


@pytest.mark.parametrize("sensor", ["MemoryHygieneSensor", "CageHygieneSensor"])
def test_registration_is_unconditional_so_disabled_is_visible(sensor):
    """Registered-but-off must be DISTINGUISHABLE from absent.

    Constructing a sensor only when its flag is on makes the boot log
    identical in both cases, which is precisely how two sensors were believed
    to be present for a whole session while being absent. VisionSensor's
    pattern — always construct, log the enabled state — is the one that keeps
    the distinction visible.
    """
    src = (OUROBOROS / "governance/intake/intake_layer_service.py").read_text(
        encoding="utf-8")
    block = src.split(f"{sensor}(")[0].rsplit("# ----", 1)[-1]
    assert "if " not in block.replace("if exc", ""), (
        f"{sensor} construction appears to be behind a conditional")
    assert f"{sensor} registered enabled=" in src, (
        f"{sensor} does not log its enabled state at registration")


def test_the_guard_would_have_caught_the_original_defect():
    """A guard nobody has seen fail is a guard nobody knows works."""
    assert _construction_sites("NoSuchSensorClassXyz", "nothing.py") == []
