from __future__ import annotations

import re
from pathlib import Path

DRIVER = Path("scripts/isomorphic_a1_local.py").read_text(encoding="utf-8")


def test_driver_composes_funded_cost_cap():
    """The composed child env must carry a non-zero OUROBOROS_BATTLE_COST_CAP.
    Run #14 booted the harness at budget=$0.00 -> every GENERATE preflight
    refused -> APPLY structurally unreachable."""
    assert "OUROBOROS_BATTLE_COST_CAP" in DRIVER
    assert "JARVIS_ISO_SESSION_BUDGET_USD" in DRIVER  # env-tunable, not hardcoded


def test_driver_has_zero_budget_failfast():
    """Driver must abort (not burn 83 min) if the effective budget resolves
    to 0. Assert the guard exists and references the autopsy failure class."""
    assert "budget_failfast" in DRIVER or "ZERO-BUDGET" in DRIVER


def test_driver_pins_doc_staleness_off():
    assert re.search(r"JARVIS_DOC_STALENESS_ENABLED[\"']?\s*[:=]\s*[\"']false", DRIVER)


def test_driver_asserts_offload_substrate_armed():
    """Evidence-pack #5: either cooperative-fs-io or posture wholesale-offload
    being off silently reintroduces on-loop scans. The driver must pin both."""
    assert re.search(r"JARVIS_COOPERATIVE_FS_IO_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)
    assert re.search(r"JARVIS_POSTURE_WHOLESALE_OFFLOAD_ENABLED[\"']?\s*[:=]\s*[\"']true", DRIVER)
