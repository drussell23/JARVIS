"""Unit A — Sovereign Telemetry Unification: pure parse extraction.

Proves the harvester's pure parse (``Metrics`` + ``parse_metrics``) was
lifted verbatim into an importable backend module with ZERO duplication:

  * ``backend.core.ouroboros.governance.graduation.telemetry_parse`` owns
    the pure parse (stdlib-only, never-raises).
  * ``scripts/telemetry_harvester.py`` re-imports from there → identity,
    not a copy (no drift).

These tests are written BEFORE the module exists (TDD red).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_harvester_module():
    """Load scripts/telemetry_harvester.py as a proper sys.modules entry
    (required so its dataclass forward-refs resolve)."""
    path = _REPO_ROOT / "scripts" / "telemetry_harvester.py"
    spec = importlib.util.spec_from_file_location(
        "telemetry_harvester_under_test", path,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- full self-heal trajectory log fixture ---------------------------------
_HEAL_LOG = """
2026-06-15T00:00:01 phase=CLASSIFY op=1
2026-06-15T00:00:02 phase=GENERATE op=1
[LiveFire] candidate FAILED live-fire boot: ImportError
2026-06-15T00:00:03 routed back failure_class=build op=1
2026-06-15T00:00:04 GENERATE_RETRY op=1 attempt=2
2026-06-15T00:00:09 op=1 state=applied phase=COMPLETE
"""

_CLEAN_SUMMARY = {
    "session_outcome": "complete",
    "stop_reason": "wall_clock_cap",
    "cost_total": 0.1234,
    "duration_s": 321.0,
    "session_id": "bt-2026-06-15-000000",
}


def test_telemetry_parse_module_exists_and_exports():
    from backend.core.ouroboros.governance.graduation import (
        telemetry_parse,
    )
    assert hasattr(telemetry_parse, "Metrics")
    assert hasattr(telemetry_parse, "parse_metrics")


def test_parse_metrics_extracts_full_self_heal_trajectory():
    from backend.core.ouroboros.governance.graduation.telemetry_parse import (
        parse_metrics,
    )
    m = parse_metrics(_HEAL_LOG, _CLEAN_SUMMARY)
    assert m.booted is True
    assert m.livefire_fired == ["ImportError"]
    assert m.routed_build is True
    assert m.retried is True
    assert m.recovered is True
    assert m.oom is False
    assert m.gate_inert is False
    assert m.session_outcome == "complete"
    assert m.cost_total == pytest.approx(0.1234)
    assert m.duration_s == pytest.approx(321.0)


def test_parse_metrics_detects_oom_anomaly():
    from backend.core.ouroboros.governance.graduation.telemetry_parse import (
        parse_metrics,
    )
    log = _HEAL_LOG + "\nprocess_memory_cap exceeded MemoryError\n"
    m = parse_metrics(log, _CLEAN_SUMMARY)
    assert m.oom is True


def test_parse_metrics_never_raises_on_garbage():
    from backend.core.ouroboros.governance.graduation.telemetry_parse import (
        parse_metrics,
    )
    # Non-dict summary, empty log — must return a Metrics, not raise.
    m = parse_metrics("", None)
    assert m.livefire_fired == []
    assert m.recovered is False
    assert m.session_outcome == ""


def test_harvester_reuses_extracted_parse_by_identity():
    """The script must import the SAME objects, not copy them — proves
    zero duplication / no drift."""
    from backend.core.ouroboros.governance.graduation import (
        telemetry_parse,
    )
    harvester = _load_harvester_module()
    assert harvester.parse_metrics is telemetry_parse.parse_metrics
    assert harvester.Metrics is telemetry_parse.Metrics


def test_harvester_certify_still_present_after_extraction():
    """certify/render_report stay in the script (graduation-classification
    is a different concern than deployment self-heal certification)."""
    harvester = _load_harvester_module()
    assert hasattr(harvester, "certify")
    assert hasattr(harvester, "render_report")
    # And certify still operates on the extracted Metrics correctly.
    m = harvester.parse_metrics(_HEAL_LOG, _CLEAN_SUMMARY)
    cert = harvester.certify(m)
    assert cert.verdict == harvester.FIELD_CERTIFIED
