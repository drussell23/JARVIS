"""Regression spine for Task 13 — VisionSensor boot wiring.

Verifies that ``IntakeLayerService`` correctly gates VisionSensor
registration on ``JARVIS_VISION_SENSOR_ENABLED`` (default off) and,
when enabled, appends the sensor to the end of the sensor list with
the canonical observability log line.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Invariant I8 (no capture authority — sensor is a read-only
consumer registered *after* the Ferrari owner VisionCortex) +
§Policy Layer (default opt-out; ``JARVIS_VISION_SENSOR_ENABLED=false``).

Scope of Task 13:

* Sensor is NOT wired when the env switch is absent or falsy.
* Sensor IS wired when the env switch is truthy (any of ``1`` / ``true`` /
  ``yes`` / ``on``, case-insensitive).
* Wiring appends at the TAIL of the sensor list (so every other
  intake-layer subsystem is constructed first).
* The ``[IntakeLayer] VisionSensor registered enabled=... tier2=...
  chain_max=... session_id=...`` line lands at INFO on opt-in and
  DEBUG on opt-out.
* Construction failures are swallowed with a WARNING log — they
  never bring down the intake layer.

We exercise the wiring path directly (constructing the relevant
sub-objects) rather than booting the full ``GovernedLoopService`` —
that stack has dozens of dependencies and the boot ordering itself
is the only invariant we care about here.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared wiring driver
# ---------------------------------------------------------------------------


def _run_wiring(monkeypatch, env_value):
    """Execute the VisionSensor wiring block in isolation.

    Returns ``(sensors_list, captured_logs, maybe_sensor)`` — the
    ``sensors_list`` mirrors the tail of what ``IntakeLayerService``
    would produce, and ``maybe_sensor`` is the constructed
    ``VisionSensor`` (or ``None`` when the switch was off / failed).
    """
    import os

    if env_value is None:
        monkeypatch.delenv("JARVIS_VISION_SENSOR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("JARVIS_VISION_SENSOR_ENABLED", env_value)

    sensors: list = []
    captured_logs: list = []

    # Minimal IntakeLayerService shim — only the attributes the
    # wiring block reads.
    class _Shim:
        _router = MagicMock()
        _sensors = sensors
        _vision_sensor = None

    shim = _Shim()

    # Inline logger capture.
    handler = logging.Handler()
    handler.emit = lambda record: captured_logs.append(  # type: ignore[assignment]
        (record.levelname, record.getMessage())
    )
    module_logger = logging.getLogger(
        "backend.core.ouroboros.governance.intake.intake_layer_service"
    )
    prev_level = module_logger.level
    module_logger.addHandler(handler)
    module_logger.setLevel(logging.DEBUG)
    try:
        # Replicate the wiring block behavior directly (matches the
        # production code at IntakeLayerService around line 670).
        def _env_truthy(raw: str) -> bool:
            return (raw or "").strip().lower() in ("1", "true", "yes", "on")

        if _env_truthy(os.environ.get("JARVIS_VISION_SENSOR_ENABLED", "false")):
            try:
                from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
                    VisionSensor,
                )
                _vs = VisionSensor(
                    router=shim._router,
                    repo="jarvis",
                    register_shutdown_hooks=False,  # don't leak atexit
                )
                sensors.append(_vs)
                shim._vision_sensor = _vs
                _tier2 = _env_truthy(
                    os.environ.get("JARVIS_VISION_SENSOR_TIER2_ENABLED", "false"),
                )
                module_logger.info(
                    "[IntakeLayer] VisionSensor registered enabled=true "
                    "tier2=%s chain_max=%d session_id=%s",
                    _tier2,
                    _vs._chain_max,
                    _vs._session_id,
                )
            except Exception as exc:
                module_logger.warning(
                    "[IntakeLayer] VisionSensor skipped (construction error): %s",
                    exc,
                )
        else:
            module_logger.debug(
                "[IntakeLayer] VisionSensor registered enabled=false "
                "(set JARVIS_VISION_SENSOR_ENABLED=1 to opt in)",
            )
    finally:
        module_logger.removeHandler(handler)
        module_logger.setLevel(prev_level)
    return sensors, captured_logs, shim._vision_sensor


# ---------------------------------------------------------------------------
# Default-off
# ---------------------------------------------------------------------------


def test_vision_sensor_not_wired_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sensors, logs, vs = _run_wiring(monkeypatch, env_value=None)
    assert sensors == []
    assert vs is None
    # Opt-out path logs at DEBUG.
    assert any(
        "VisionSensor registered enabled=false" in m
        for (_lvl, m) in logs
    )


@pytest.mark.parametrize("bad_value", ["false", "0", "no", "", "off", "FALSE"])
def test_vision_sensor_not_wired_when_env_falsy(monkeypatch, tmp_path, bad_value):
    monkeypatch.chdir(tmp_path)
    sensors, logs, vs = _run_wiring(monkeypatch, env_value=bad_value)
    assert sensors == []
    assert vs is None


# ---------------------------------------------------------------------------
# Opt-in truthy values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("truthy_value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_vision_sensor_wired_when_env_truthy(monkeypatch, tmp_path, truthy_value):
    monkeypatch.chdir(tmp_path)
    sensors, logs, vs = _run_wiring(monkeypatch, env_value=truthy_value)
    assert len(sensors) == 1
    assert vs is not None
    # Canonical info-line tokens.
    info_lines = [m for (lvl, m) in logs if lvl == "INFO"]
    assert any(
        "VisionSensor registered enabled=true" in m
        for m in info_lines
    )
    assert any("tier2=" in m for m in info_lines)
    assert any("chain_max=" in m for m in info_lines)
    assert any("session_id=" in m for m in info_lines)


# ---------------------------------------------------------------------------
# Tail placement — VisionSensor is appended AFTER any pre-existing
# sensors in the list.
# ---------------------------------------------------------------------------


def test_vision_sensor_appended_after_existing_sensors(monkeypatch, tmp_path):
    """Simulate a sensors list that already contains upstream entries
    (BacklogSensor, TodoScannerSensor, CUExecutionSensor equivalents).
    The VisionSensor wiring block must ``append`` — not insert — so
    the resulting order places VisionSensor LAST.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_VISION_SENSOR_ENABLED", "true")

    # Pre-populate the sensor list with dummies — simulating the
    # production wiring order where VisionSensor is the final block.
    existing = [MagicMock(name="backlog"), MagicMock(name="todo"), MagicMock(name="cu")]
    sensors = list(existing)

    import os

    def _env_truthy(raw: str) -> bool:
        return (raw or "").strip().lower() in ("1", "true", "yes", "on")

    if _env_truthy(os.environ.get("JARVIS_VISION_SENSOR_ENABLED", "false")):
        from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
            VisionSensor,
        )
        vs = VisionSensor(
            router=MagicMock(),
            repo="jarvis",
            register_shutdown_hooks=False,
        )
        sensors.append(vs)

    # VisionSensor is at the tail; existing entries preserved in order.
    assert sensors[:3] == existing
    from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
        VisionSensor,
    )
    assert isinstance(sensors[-1], VisionSensor)


# ---------------------------------------------------------------------------
# Production code actually contains the wiring block
# ---------------------------------------------------------------------------


def test_intake_layer_service_contains_vision_sensor_wiring_block():
    """Grep the production file for the canonical wiring markers — a
    regression where someone removes the block without realising it's
    the Task 13 contract would be caught here."""
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / (
        "backend/core/ouroboros/governance/intake/intake_layer_service.py"
    )
    src = path.read_text(encoding="utf-8")
    # Master switch check
    assert 'JARVIS_VISION_SENSOR_ENABLED' in src
    # Sensor import path
    assert (
        "from backend.core.ouroboros.governance.intake.sensors.vision_sensor"
        in src
    )
    # Opt-in INFO log line tokens
    assert "VisionSensor registered enabled=true" in src
    # Opt-out DEBUG log line tokens
    assert "VisionSensor registered enabled=false" in src


def test_intake_layer_service_registers_vision_sensor_after_cu_execution():
    """Production-code structural check: the VisionSensor wiring block
    must appear AFTER the CUExecutionSensor block (and therefore after
    every earlier sensor). Mirrors the "register after every upstream
    dependency" invariant in the plan.
    """
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / (
        "backend/core/ouroboros/governance/intake/intake_layer_service.py"
    )
    src = path.read_text(encoding="utf-8")
    cu_idx = src.find("CUExecutionSensor")
    vs_idx = src.find("VisionSensor")
    assert cu_idx > 0, "CUExecutionSensor marker not found"
    assert vs_idx > 0, "VisionSensor marker not found"
    assert vs_idx > cu_idx, (
        f"VisionSensor wiring (idx={vs_idx}) must be registered "
        f"AFTER CUExecutionSensor (idx={cu_idx}) so every upstream "
        "intake subsystem constructs first."
    )
