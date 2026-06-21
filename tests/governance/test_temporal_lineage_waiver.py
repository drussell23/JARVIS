"""Sovereign Temporal Lineage Waiver tests (2026-06-21).

A pre-fix contract-metrics-predicate downgrade with NO actual runner failures is
forgiven as legacy infra-latency (DW batch latency, fixed by the Infinite-Horizon
Batch Matrix) — but ONLY before the architectural-fix cutoff. Post-fix downgrades
remain hard runner failures."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.graduation.lineage_waiver import (
    is_legacy_infra_latency_downgrade,
    latency_waiver_cutoff_epoch,
)
from backend.core.ouroboros.governance.adaptation.graduation_ledger import (
    GraduationLedger,
)

_NOTES = "complete_no_runner_failures|contract_metrics_predicate_downgraded"
_FLAG = "JARVIS_COMMAND_BUS_BRIDGE_ENABLED"
_CUTOFF = 1782021888.0  # Infinite-Horizon merge epoch


def test_cutoff_default():
    assert latency_waiver_cutoff_epoch() == _CUTOFF


def test_pre_fix_downgrade_waived():
    assert is_legacy_infra_latency_downgrade(
        outcome="runner", notes=_NOTES, recorded_at_epoch=_CUTOFF - 1000,
    ) is True


def test_post_fix_downgrade_not_waived():
    # The temporal bound: a downgrade AT/AFTER the fix is a hard failure.
    assert is_legacy_infra_latency_downgrade(
        outcome="runner", notes=_NOTES, recorded_at_epoch=_CUTOFF + 1000,
    ) is False
    assert is_legacy_infra_latency_downgrade(
        outcome="runner", notes=_NOTES, recorded_at_epoch=_CUTOFF,
    ) is False


def test_genuine_runner_fault_not_waived():
    assert is_legacy_infra_latency_downgrade(
        outcome="runner", notes="venom_tool_loop_failed",
        recorded_at_epoch=_CUTOFF - 1000,
    ) is False


def test_clean_row_not_waived():
    assert is_legacy_infra_latency_downgrade(
        outcome="clean", notes=_NOTES, recorded_at_epoch=_CUTOFF - 1000,
    ) is False


def test_missing_no_runner_token_not_waived():
    # A metrics downgrade that DID have runner failures is not waived.
    assert is_legacy_infra_latency_downgrade(
        outcome="runner",
        notes="some_runner_failure|contract_metrics_predicate_downgraded",
        recorded_at_epoch=_CUTOFF - 1000,
    ) is False


def test_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_LATENCY_WAIVER_CUTOFF_EPOCH", "1000000")
    assert latency_waiver_cutoff_epoch() == 1000000.0


def test_never_raises_on_bad_epoch():
    assert is_legacy_infra_latency_downgrade(
        outcome="runner", notes=_NOTES, recorded_at_epoch="notanumber",  # type: ignore
    ) is False


def _ledger(rows, monkeypatch):
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    p = Path(tempfile.mktemp())
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return GraduationLedger(path=p)


def test_eligibility_flips_with_waiver(monkeypatch):
    rows = [
        {"flag_name": _FLAG, "session_id": f"s{i}", "outcome": "runner",
         "recorded_at_iso": "x", "recorded_at_epoch": _CUTOFF - 5000 + i,
         "notes": _NOTES, "runner_attributed_kind": "default_conservative"}
        for i in range(5)
    ] + [
        {"flag_name": _FLAG, "session_id": f"c{i}", "outcome": "clean",
         "recorded_at_iso": "x", "recorded_at_epoch": _CUTOFF + 10000 + i,
         "notes": "complete_no_runner_failures"}
        for i in range(3)
    ]
    led = _ledger(rows, monkeypatch)
    prog = led.progress(_FLAG)
    assert prog["clean"] == 3
    assert prog["runner"] == 0
    assert prog["waived_legacy_infra_latency"] == 5
    assert led.is_eligible(_FLAG) is True


def test_post_fix_downgrade_blocks_eligibility(monkeypatch):
    rows = [
        {"flag_name": _FLAG, "session_id": f"c{i}", "outcome": "clean",
         "recorded_at_iso": "x", "recorded_at_epoch": _CUTOFF + 10000 + i,
         "notes": "complete_no_runner_failures"}
        for i in range(3)
    ] + [
        # A POST-fix metrics downgrade — must remain a hard runner failure.
        {"flag_name": _FLAG, "session_id": "post1", "outcome": "runner",
         "recorded_at_iso": "x", "recorded_at_epoch": _CUTOFF + 99999,
         "notes": _NOTES, "runner_attributed_kind": "default_conservative"},
    ]
    led = _ledger(rows, monkeypatch)
    prog = led.progress(_FLAG)
    assert prog["runner"] == 1
    assert led.is_eligible(_FLAG) is False
