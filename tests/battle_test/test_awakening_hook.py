"""Cockpit hook: harness builds the conductor with briefing wired to
on_ignition; SOAK builds nothing (spec §3.5)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.harness import build_awakening_for_cockpit
from backend.core.ouroboros.ui.awakening import AwakeningConductor
from rich.console import Console
from backend.core.ouroboros.ui import theme


def test_builder_returns_wired_conductor(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    console = Console(file=open("/dev/null", "w"), force_terminal=True,
                      width=80, color_system="truecolor")
    theme.ensure_theme(console)
    conductor = build_awakening_for_cockpit(
        console, intake_probe=lambda: 2, approvals_probe=lambda: 0)
    assert isinstance(conductor, AwakeningConductor)
    assert conductor._on_ignition is not None      # briefing wired


def test_builder_returns_none_in_soak(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "soak")
    console = Console(file=open("/dev/null", "w"))
    assert build_awakening_for_cockpit(console, intake_probe=None,
                                       approvals_probe=None) is None
