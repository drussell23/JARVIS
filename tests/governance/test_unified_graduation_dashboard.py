"""Regression spine for the Unified Graduation Dashboard
(PRD §35, 2026-05-07).

Covers:

  * 5 AST pins via register_shipped_invariants
  * Verdict normalization (contract + ledger)
  * Aggregator zero-arg call NEVER raises
  * REPL dispatch matched/unmatched + master-flag gating
  * Audit ledger append (master-flag-gated)
  * §33.5 versioned-artifact projection
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Master-flag isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_master_flag(monkeypatch):
    """Ensure each test starts with master flag clear."""
    monkeypatch.delenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED",
        raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH",
        raising=False,
    )
    yield


# ---------------------------------------------------------------------------
# Master flag default-FALSE
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        is_dashboard_enabled,
    )
    assert is_dashboard_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE", "True"],
)
def test_master_flag_truthy_values(monkeypatch, value):
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        is_dashboard_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", value,
    )
    assert is_dashboard_enabled() is True


@pytest.mark.parametrize(
    "value", ["0", "false", "no", "off", "", "garbage"],
)
def test_master_flag_falsy_values(monkeypatch, value):
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        is_dashboard_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", value,
    )
    assert is_dashboard_enabled() is False


# ---------------------------------------------------------------------------
# Verdict normalization — contract verdict strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ready_for_graduation", "ready"),
        ("ready_for_purge", "ready"),
        ("already_graduated", "ready"),
        ("insufficient_op_samples", "evidence_gathering"),
        ("insufficient_emissions", "evidence_gathering"),
        ("insufficient_observations", "evidence_gathering"),
        ("insufficient_evaluations", "evidence_gathering"),
        ("insufficient_fires", "evidence_gathering"),
        ("insufficient_transitions", "evidence_gathering"),
        ("insufficient_sessions", "evidence_gathering"),
        ("producer_inactive", "evidence_insufficient"),
        ("missing_queue_evidence", "evidence_insufficient"),
        ("missing_recovery_evidence", "evidence_insufficient"),
        ("excessive_drift_detected", "evidence_failed"),
        ("excessive_throttles", "evidence_failed"),
        ("excessive_false_positives", "evidence_failed"),
        ("excessive_failures", "evidence_failed"),
        ("excessive_denies", "evidence_failed"),
        ("excessive_non_actionable_rate", "evidence_failed"),
        ("excessive_disabled_samples", "evidence_failed"),
        ("disabled", "disabled"),
    ],
)
def test_normalize_contract_verdict_known(raw, expected):
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_contract_verdict,
    )
    verdict, _ = _normalize_contract_verdict(raw)
    assert verdict.value == expected


def test_normalize_contract_verdict_unknown_routes_insufficient():
    """Unknown verdict strings route to EVIDENCE_INSUFFICIENT
    with diagnostic — never silently absorbed."""
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_contract_verdict,
    )
    verdict, diag = _normalize_contract_verdict("totally_made_up")
    assert verdict.value == "evidence_insufficient"
    assert "totally_made_up" in diag


def test_normalize_contract_verdict_empty_string():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_contract_verdict,
    )
    verdict, _ = _normalize_contract_verdict("")
    assert verdict.value == "evidence_insufficient"


# ---------------------------------------------------------------------------
# Verdict normalization — ledger progress
# ---------------------------------------------------------------------------


def test_normalize_ledger_state_master_off():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_ledger_state,
    )
    verdict, diag = _normalize_ledger_state(
        {"clean": 5, "runner": 0, "required": 3},
        is_eligible=True,
        ledger_master_on=False,
    )
    assert verdict.value == "disabled"
    assert "ledger_master_off" in diag


def test_normalize_ledger_state_eligible_ready():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_ledger_state,
    )
    verdict, diag = _normalize_ledger_state(
        {"clean": 3, "runner": 0, "required": 3},
        is_eligible=True,
        ledger_master_on=True,
    )
    assert verdict.value == "ready"
    assert "clean=3/3" in diag


def test_normalize_ledger_state_runner_failure():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_ledger_state,
    )
    verdict, diag = _normalize_ledger_state(
        {"clean": 1, "runner": 1, "required": 3},
        is_eligible=False,
        ledger_master_on=True,
    )
    assert verdict.value == "evidence_failed"
    assert "runner=1" in diag


def test_normalize_ledger_state_gathering():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        _normalize_ledger_state,
    )
    verdict, diag = _normalize_ledger_state(
        {"clean": 1, "runner": 0, "required": 3},
        is_eligible=False,
        ledger_master_on=True,
    )
    assert verdict.value == "evidence_gathering"
    assert "clean=1/3" in diag


# ---------------------------------------------------------------------------
# Aggregator behavior
# ---------------------------------------------------------------------------


def test_aggregator_never_raises():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    assert snap is not None
    assert isinstance(snap.rows, tuple)
    assert snap.elapsed_s >= 0.0


def test_aggregator_includes_8_contracts():
    """8 §33.1 contract adapters MUST run."""
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    contract_rows = [r for r in snap.rows if r.source == "contract"]
    # Exactly 8 §33.1 contract adapters.
    assert len(contract_rows) == 8


def test_aggregator_includes_ledger_flags(monkeypatch):
    """Ledger composition MUST yield rows for every
    CADENCE_POLICY entry."""
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
    )
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    snap = aggregate_dashboard()
    ledger_rows = [r for r in snap.rows if r.source == "ledger"]
    # Every CADENCE_POLICY flag yields exactly one row.
    assert len(ledger_rows) == len(CADENCE_POLICY)


def test_aggregator_summary_counts_match_rows():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    summary = snap.summary()
    # Sum of summary counts MUST equal len(rows).
    assert sum(summary.values()) == len(snap.rows)


def test_aggregator_ready_failed_filters_consistent():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
        UnifiedGraduationVerdict,
    )
    snap = aggregate_dashboard()
    ready = snap.ready_rows()
    failed = snap.failed_rows()
    for r in ready:
        assert r.verdict == UnifiedGraduationVerdict.READY
    for r in failed:
        assert r.verdict == UnifiedGraduationVerdict.EVIDENCE_FAILED


# ---------------------------------------------------------------------------
# §33.5 versioned-artifact projection
# ---------------------------------------------------------------------------


def test_dashboard_row_to_dict_has_schema_version():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        DashboardRow,
        UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION,
    )
    row = DashboardRow(name="x", source="contract")
    d = row.to_dict()
    assert d["schema_version"] == (
        UNIFIED_GRADUATION_DASHBOARD_SCHEMA_VERSION
    )
    assert d["name"] == "x"
    assert d["source"] == "contract"


def test_dashboard_snapshot_to_dict_has_summary():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    d = snap.to_dict()
    assert "schema_version" in d
    assert "rows" in d
    assert "summary" in d
    assert isinstance(d["rows"], list)


# ---------------------------------------------------------------------------
# REPL dispatch
# ---------------------------------------------------------------------------


def test_repl_unmatched_returns_matched_false():
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/something_else")
    assert r.matched is False


def test_repl_help_works_master_off():
    """`/graduation help` MUST work even with master flag off
    (discoverability)."""
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation help")
    assert r.ok is True
    assert "Unified Graduation Dashboard" in r.text


def test_repl_status_blocks_master_off():
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation status")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_status_works_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation status")
    assert r.ok is True
    assert "total gates" in r.text


def test_repl_no_subcommand_aliases_to_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation")
    assert r.ok is True
    assert "total gates" in r.text


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation gibberish")
    assert r.ok is False
    assert "unknown subcommand" in r.text.lower()


def test_repl_contract_requires_name(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation contract")
    assert r.ok is False
    assert "name required" in r.text.lower()


def test_repl_contract_lookup_substring_match(monkeypatch):
    """Contract lookup matches substring case-insensitively."""
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    # `phase10_purge` is one of the 8 contract names.
    r = dispatch_graduation_command(
        "/graduation contract phase10_purge",
    )
    assert r.ok is True
    assert "phase10_purge" in r.text


def test_repl_contract_unknown_returns_not_found(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command(
        "/graduation contract zzz_does_not_exist",
    )
    assert r.ok is False
    assert "not found" in r.text.lower()


def test_repl_details_with_limit(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation details 3")
    assert r.ok is True
    # Header line + 3 rows minimum.
    assert r.text.count("\n") >= 3


def test_repl_parse_error_handled():
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation 'unclosed")
    assert r.ok is False
    assert "parse error" in r.text.lower()


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------


def test_audit_record_master_off_returns_false(tmp_path):
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
        append_audit_record,
    )
    snap = aggregate_dashboard()
    # Master flag off → no write.
    assert append_audit_record(snap) is False


def test_audit_record_master_on_writes_jsonl(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    ledger_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH",
        str(ledger_path),
    )
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        aggregate_dashboard,
        append_audit_record,
    )
    snap = aggregate_dashboard()
    ok = append_audit_record(snap)
    assert ok is True
    assert ledger_path.exists()
    line = ledger_path.read_text().strip()
    record = json.loads(line)
    assert "schema_version" in record
    assert "summary" in record
    assert isinstance(record["summary"], dict)


# ---------------------------------------------------------------------------
# 5 AST pins fire on the actual source
# ---------------------------------------------------------------------------


def _module_source():
    return Path(
        "backend/core/ouroboros/governance/"
        "unified_graduation_dashboard.py"
    ).read_text()


def _all_pins():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def test_pins_register_exactly_5():
    pins = _all_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("pin_idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(pin_idx):
    pins = _all_pins()
    src = _module_source()
    tree = ast.parse(src)
    violations = pins[pin_idx].validate(tree, src)
    assert not violations, (
        f"{pins[pin_idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    """Synthetic regression: replacing the master-flag body with
    `return True` MUST fire the pin."""
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def is_dashboard_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any(
        "default-FALSE" in v or "return True" in v
        for v in violations
    )


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("orchestrator" in v for v in violations)


def test_pin_composes_contracts_fires_on_missing_adapter():
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_contracts" in p.invariant_name
    )
    # A module with zero adapters must fire (count != 8).
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_contracts_fires_on_adapter_without_contract_import():
    """Adapter that doesn't lazy-import a `*_graduation_contract`
    module fires the pin."""
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_contracts" in p.invariant_name
    )
    # 8 adapters, but none import a graduation_contract module.
    bad_src = "\n".join(
        f"def _adapter_x{i}():\n    return None\n"
        for i in range(8)
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    # At least one adapter MUST be flagged.
    assert any("MUST lazy-import" in v for v in violations)


def test_pin_verdict_taxonomy_fires_on_missing_value():
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_5_values" in p.invariant_name
    )
    # Missing READY value.
    bad_src = (
        "import enum\n"
        "class UnifiedGraduationVerdict(str, enum.Enum):\n"
        "    EVIDENCE_GATHERING = 'evidence_gathering'\n"
        "    EVIDENCE_INSUFFICIENT = 'evidence_insufficient'\n"
        "    EVIDENCE_FAILED = 'evidence_failed'\n"
        "    DISABLED = 'disabled'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("missing" in v.lower() for v in violations)


def test_pin_verdict_taxonomy_fires_on_extra_value():
    pins = _all_pins()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_5_values" in p.invariant_name
    )
    # Extra MAYBE_READY value.
    bad_src = (
        "import enum\n"
        "class UnifiedGraduationVerdict(str, enum.Enum):\n"
        "    READY = 'ready'\n"
        "    EVIDENCE_GATHERING = 'evidence_gathering'\n"
        "    EVIDENCE_INSUFFICIENT = 'evidence_insufficient'\n"
        "    EVIDENCE_FAILED = 'evidence_failed'\n"
        "    DISABLED = 'disabled'\n"
        "    MAYBE_READY = 'maybe_ready'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("extras" in v.lower() for v in violations)


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    assert n == 2  # master flag + ledger path
    names = {c["name"] for c in reg.calls}
    assert (
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED" in names
    )
    assert (
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_LEDGER_PATH" in names
    )


def test_register_flags_none_registry():
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
        register_flags,
    )
    assert register_flags(None) == 0


# ---------------------------------------------------------------------------
# Integration smoke — REPL composes substrate end-to-end
# ---------------------------------------------------------------------------


def test_repl_ready_renders_real_data(monkeypatch):
    """End-to-end: REPL `/graduation ready` composes the
    substrate aggregator and produces operator-readable output."""
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation ready")
    assert r.ok is True
    # Either lists READY gates or says none are READY — both valid.
    assert (
        "ready" in r.text.lower()
        or "no gates currently" in r.text.lower()
    )


def test_repl_failed_renders_real_data(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.graduation_repl import (
        dispatch_graduation_command,
    )
    r = dispatch_graduation_command("/graduation failed")
    assert r.ok is True
