"""Slice 223 — headless-soak sensor demotion (the 10A pattern, generalized).

Slice 10A precedent: SWE-Bench fixtures masquerading as test_failure routed
IMMEDIATE -> Claude-direct and burned 99.83% of soak spend on Claude. Same
disease in the unattended soak: the TestFailure sensor's storm (3 known
pre-existing failures it can never fix) routed IMMEDIATE, saturating workers
+ burning Claude rescue tokens on noise. §5's reasoning applies verbatim:
urgency routing was designed for the HUMAN-REFLEX case — in a headless soak,
NO HUMAN IS WAITING on a sensor alarm.

Priority 0.75: when JARVIS_SOAK_SENSOR_DEMOTION_ENABLED + the soak is headless
(OUROBOROS_BATTLE_HEADLESS truthy), test_failure-source ops downgrade
IMMEDIATE -> STANDARD (DW primary, Claude per-round rescue preserved — the 10A
choice: capability kept, cost restored). Gated default-FALSE; interactive
(non-headless) behavior byte-identical. Source-discriminated (signal_source),
not priority-label-based — a generated patch can't elevate itself past it by
relabeling priority (the S208 concern).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute, UrgencyRouter,
)


def _ctx(source="test_failure", urgency="critical"):
    return SimpleNamespace(
        signal_source=source, signal_urgency=urgency,
        task_complexity="moderate", target_files=["a.py"], cross_repo=False,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("JARVIS_SOAK_SENSOR_DEMOTION_ENABLED", "OUROBOROS_BATTLE_HEADLESS"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_default_off_is_byte_identical(monkeypatch):
    """Gate off -> test_failure + critical still routes IMMEDIATE (legacy)."""
    route, _ = UrgencyRouter().classify(_ctx())
    assert route is ProviderRoute.IMMEDIATE


def test_headless_soak_demotes_test_failure(monkeypatch):
    monkeypatch.setenv("JARVIS_SOAK_SENSOR_DEMOTION_ENABLED", "1")
    monkeypatch.setenv("OUROBOROS_BATTLE_HEADLESS", "1")
    route, reason = UrgencyRouter().classify(_ctx())
    assert route is ProviderRoute.STANDARD
    assert "headless" in reason and "test_failure" in reason


def test_interactive_session_unaffected(monkeypatch):
    """Gate ON but NOT headless -> human may be waiting -> IMMEDIATE stays."""
    monkeypatch.setenv("JARVIS_SOAK_SENSOR_DEMOTION_ENABLED", "1")
    route, _ = UrgencyRouter().classify(_ctx())
    assert route is ProviderRoute.IMMEDIATE


def test_non_sensor_sources_unaffected(monkeypatch):
    """Voice/human-critical signals keep the reflex lane even in headless."""
    monkeypatch.setenv("JARVIS_SOAK_SENSOR_DEMOTION_ENABLED", "1")
    monkeypatch.setenv("OUROBOROS_BATTLE_HEADLESS", "1")
    route, _ = UrgencyRouter().classify(_ctx(source="voice_human"))
    assert route is ProviderRoute.IMMEDIATE


def test_discriminator_is_source_not_priority_label(monkeypatch):
    """S208 concern: the gate keys on signal_source (stamped by the sensor),
    not any priority label a patch could self-elevate."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "backend" / "core"
           / "ouroboros" / "governance" / "urgency_router.py").read_text(encoding="utf-8")
    assert "JARVIS_SOAK_SENSOR_DEMOTION_ENABLED" in src
    assert "OUROBOROS_BATTLE_HEADLESS" in src
