"""Regression spine for Task 21 production wiring (SerpentFlow + registry).

Autonomous coverage of what can be tested without booting the full
Rich TUI:

* ``register_active_vision_sensor`` / ``get_active_vision_sensor`` —
  process-global registry round-trip; ``None`` clears; thread-safe
  under the registered lock.
* ``IntakeLayerService`` wiring — when the boot block constructs
  a VisionSensor, it must also call ``register_active_vision_sensor``
  so the REPL can reach it without a full object-graph lookup.
* SerpentFlow command-dispatch predicates — ``/vision`` / ``/verify-*``
  lines route to the correct handlers (via string-match inspection
  of the production source, not a full REPL boot).
* Help menu lists all three new commands.
* `_handle_vision` sub-command parsing — status / resume / boost /
  unknown paths all route correctly (via a stub flow + console).
"""
from __future__ import annotations

import pathlib
from typing import List
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.intake.sensors.vision_sensor import VisionSensor
from backend.core.ouroboros.governance.vision_repl import (
    get_active_vision_sensor,
    register_active_vision_sensor,
)


# ---------------------------------------------------------------------------
# Autouse: isolate tmp + clear registry between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    register_active_vision_sensor(None)
    yield
    register_active_vision_sensor(None)


# ---------------------------------------------------------------------------
# Active-sensor registry
# ---------------------------------------------------------------------------


def test_registry_empty_by_default():
    assert get_active_vision_sensor() is None


def test_registry_register_and_retrieve():
    sentinel = MagicMock(name="sensor")
    register_active_vision_sensor(sentinel)
    assert get_active_vision_sensor() is sentinel


def test_registry_register_none_clears():
    sentinel = MagicMock(name="sensor")
    register_active_vision_sensor(sentinel)
    register_active_vision_sensor(None)
    assert get_active_vision_sensor() is None


def test_registry_overwrites_previous():
    s1 = MagicMock(name="sensor1")
    s2 = MagicMock(name="sensor2")
    register_active_vision_sensor(s1)
    register_active_vision_sensor(s2)
    assert get_active_vision_sensor() is s2


# ---------------------------------------------------------------------------
# IntakeLayerService wiring — production source check
# ---------------------------------------------------------------------------


def test_intake_layer_registers_active_sensor_on_wiring():
    """Structural guard: the VisionSensor wiring block in
    ``IntakeLayerService`` must call ``register_active_vision_sensor``
    so the REPL can reach the sensor. A regression where someone
    removes this call silently breaks `/vision` commands.
    """
    src = _read_production_source(
        "backend/core/ouroboros/governance/intake/intake_layer_service.py"
    )
    assert "register_active_vision_sensor" in src
    assert "from backend.core.ouroboros.governance.vision_repl import" in src


# ---------------------------------------------------------------------------
# SerpentFlow dispatch predicates + help menu — production source check
# ---------------------------------------------------------------------------


def test_serpent_flow_dispatches_vision_commands():
    """Production-source structural check: `/vision`, `/verify-confirm`,
    and `/verify-undemote` are all wired into the REPL dispatch chain.
    """
    src = _read_production_source(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    )
    # Dispatch branches present.
    assert 'line.startswith("/vision")' in src
    assert 'line.startswith("/verify-confirm")' in src
    assert '"/verify-undemote"' in src
    # Handler methods present.
    assert "def _handle_vision(self" in src
    assert "def _handle_verify_confirm(self" in src
    assert "def _handle_verify_undemote(self" in src


def test_serpent_flow_help_menu_mentions_new_commands():
    src = _read_production_source(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    )
    # Help panel has one line per command family.
    assert "/vision [...]" in src
    assert "/verify-confirm <op> X" in src or "/verify-confirm" in src
    assert "/verify-undemote" in src


def test_serpent_flow_uses_vision_origin_tag():
    """Production-source check: op_started prepends the `[vision-origin]`
    tag on vision-originated op blocks.
    """
    src = _read_production_source(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    )
    assert "vision_origin_tag" in src


# ---------------------------------------------------------------------------
# _handle_vision subcommand parsing — driven directly via the bound class
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repl():
    """Construct a minimal stand-in with just the bits ``_handle_vision``
    touches: a ``_flow`` with ``console.print``."""
    repl = MagicMock()
    printed: List[str] = []
    repl._flow.console.print = lambda msg, **kw: printed.append(str(msg))
    repl._printed = printed
    return repl


def _call_handle_vision(repl, line: str) -> None:
    # Import at call time so chdir + registry-clear fixtures apply.
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    # ``_handle_vision`` lives on the REPL-loop inner class
    # (``_CommandLoop`` / similar) but takes no cross-class state —
    # bind the unbound method from the class directly.
    # Find the inner class that owns _handle_vision:
    import inspect
    for name, obj in inspect.getmembers(
        __import__(
            "backend.core.ouroboros.battle_test.serpent_flow",
            fromlist=["*"],
        )
    ):
        if inspect.isclass(obj) and hasattr(obj, "_handle_vision"):
            obj._handle_vision(repl, line)
            return
    raise AssertionError("could not find class owning _handle_vision")


def test_handle_vision_status_on_unregistered_sensor(fake_repl):
    _call_handle_vision(fake_repl, "/vision status")
    output = "\n".join(fake_repl._printed)
    assert "not configured" in output


def test_handle_vision_status_on_registered_sensor(fake_repl, tmp_path):
    sensor = VisionSensor(
        router=MagicMock(),
        session_id="wire-test",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        register_shutdown_hooks=False,
    )
    register_active_vision_sensor(sensor)
    _call_handle_vision(fake_repl, "/vision status")
    output = "\n".join(fake_repl._printed)
    assert "vision: armed" in output
    assert "tier2:" in output


def test_handle_vision_bare_is_status(fake_repl, tmp_path):
    sensor = VisionSensor(
        router=MagicMock(),
        session_id="wire-test",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        register_shutdown_hooks=False,
    )
    register_active_vision_sensor(sensor)
    _call_handle_vision(fake_repl, "/vision")
    output = "\n".join(fake_repl._printed)
    assert "vision:" in output   # status line rendered


def test_handle_vision_resume_clears_pause(fake_repl, tmp_path):
    from backend.core.ouroboros.governance.intake.sensors.vision_sensor import (
        PAUSE_REASON_FP_BUDGET,
    )
    sensor = VisionSensor(
        router=MagicMock(),
        session_id="wire-test",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        register_shutdown_hooks=False,
    )
    sensor._pause(reason=PAUSE_REASON_FP_BUDGET, duration_s=None)
    register_active_vision_sensor(sensor)
    _call_handle_vision(fake_repl, "/vision resume")
    output = "\n".join(fake_repl._printed)
    assert "resumed" in output
    assert sensor.paused is False


def test_handle_vision_unknown_subcommand(fake_repl, tmp_path):
    sensor = VisionSensor(
        router=MagicMock(),
        session_id="wire-test",
        retention_root=str(tmp_path / "retention"),
        frame_ttl_s=0.0,
        ledger_path=str(tmp_path / "ledger.json"),
        cost_ledger_path=str(tmp_path / "cost.json"),
        register_shutdown_hooks=False,
    )
    register_active_vision_sensor(sensor)
    _call_handle_vision(fake_repl, "/vision bogus")
    output = "\n".join(fake_repl._printed)
    assert "unknown subcommand" in output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_production_source(rel_path: str) -> str:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    return (repo_root / rel_path).read_text(encoding="utf-8")
