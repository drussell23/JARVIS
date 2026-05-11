"""Regression spine for §40 Wave 5 #19 — Proof Carrier Transport."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    proof_carrier_transport as pct,
)
from backend.core.ouroboros.governance.proof_carrier_transport import (
    PROOF_CARRIER_SCHEMA_VERSION,
    EvidenceSource,
    ProofCarrier,
    ProofVerdict,
    _ENV_BLOCK_ON_COHERENCE,
    _ENV_BLOCK_ON_MCP,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_PERSIST,
    _build_verdict,
    _classify_source,
    block_on_coherence_drift,
    block_on_mcp_finding,
    build_proof_carrier,
    format_proof_panel,
    ledger_path,
    master_enabled,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    source_glyph,
    verdict_glyph,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_BLOCK_ON_MCP,
        _ENV_BLOCK_ON_COHERENCE, _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "proof.jsonl"),
    )
    yield


def test_schema():
    assert PROOF_CARRIER_SCHEMA_VERSION == "proof_carrier.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_block_on_mcp_default_true():
    assert block_on_mcp_finding() is True


def test_block_on_coherence_default_false():
    assert block_on_coherence_drift() is False


def test_verdict_taxonomy_closed():
    assert {v.value for v in ProofVerdict} == {
        "clean", "warn", "block", "disabled",
    }


def test_source_taxonomy_closed():
    assert {s.value for s in EvidenceSource} == {
        "mcp_scan", "coherence", "rehearsal", "none",
    }


@pytest.mark.parametrize("v", list(ProofVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(EvidenceSource))
def test_source_glyph(s):
    assert source_glyph(s) != "?"


# Classifier


def test_source_none_no_evidence():
    assert _classify_source(0, "", 0) is EvidenceSource.NONE


def test_source_mcp_priority():
    assert (
        _classify_source(2, "critical", 5)
        is EvidenceSource.MCP_SCAN
    )


def test_source_rehearsal_when_no_mcp():
    assert (
        _classify_source(0, "moderate", 3)
        is EvidenceSource.REHEARSAL
    )


def test_source_coherence_only():
    assert (
        _classify_source(0, "critical", 0)
        is EvidenceSource.COHERENCE
    )


# Verdict


def test_verdict_clean_no_evidence():
    assert _build_verdict(0, "", 0, "") is ProofVerdict.CLEAN


def test_verdict_block_on_mcp():
    assert (
        _build_verdict(2, "", 0, "")
        is ProofVerdict.BLOCK
    )


def test_verdict_warn_on_mcp_when_block_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_BLOCK_ON_MCP, "false")
    assert (
        _build_verdict(2, "", 0, "")
        is ProofVerdict.WARN
    )


def test_verdict_block_on_critical_coherence_when_enabled(monkeypatch):
    monkeypatch.setenv(_ENV_BLOCK_ON_COHERENCE, "true")
    assert (
        _build_verdict(0, "critical", 0, "")
        is ProofVerdict.BLOCK
    )


def test_verdict_warn_on_critical_coherence_default():
    """Default: block_on_coherence=false → critical drift is WARN."""
    assert (
        _build_verdict(0, "critical", 0, "")
        is ProofVerdict.WARN
    )


def test_verdict_block_on_rehearsal_escalate():
    assert (
        _build_verdict(0, "", 1, "escalate")
        is ProofVerdict.BLOCK
    )


def test_verdict_warn_on_moderate_coherence():
    assert (
        _build_verdict(0, "moderate", 0, "")
        is ProofVerdict.WARN
    )


# build_proof_carrier


def test_build_master_off_disabled():
    c = build_proof_carrier("op-1", ["x.py"])
    assert c.master_enabled is False
    assert c.verdict is ProofVerdict.DISABLED


def test_build_clean_no_evidence(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    assert c.verdict is ProofVerdict.CLEAN


def test_build_block_on_mcp_finding(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=3,
        mcp_kinds_override=["aws_key"],
        coherence_drift_override="",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    assert c.verdict is ProofVerdict.BLOCK
    assert c.dominant_source is EvidenceSource.MCP_SCAN


def test_build_warn_on_rehearsal_concern(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="",
        rehearsal_count_override=2,
        rehearsal_verdict_override="concern_raised",
    )
    assert c.verdict is ProofVerdict.WARN
    assert c.dominant_source is EvidenceSource.REHEARSAL


def test_build_diagnostic_includes_components(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=1, mcp_kinds_override=["test"],
        coherence_drift_override="moderate",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    assert "verdict=" in c.diagnostic
    assert "mcp=" in c.diagnostic


def test_build_real_composition(monkeypatch):
    """All Wave 3 substrates master-off → CLEAN."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier("op-1", ["x.py"])
    # Wave 3 substrates default master-off → no findings
    assert c.verdict is ProofVerdict.CLEAN


# Persistence


def test_persist_clean_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    assert not ledger_path().exists()


def test_persist_block_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=3,
        mcp_kinds_override=["aws_key"],
        coherence_drift_override="",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    assert ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    assert "disabled" in format_proof_panel()


def test_format_panel_with_carrier(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    c = build_proof_carrier(
        "op-1", ["x.py"],
        mcp_count_override=0, mcp_kinds_override=[],
        coherence_drift_override="",
        rehearsal_count_override=0,
        rehearsal_verdict_override="",
    )
    out = format_proof_panel(c)
    assert "Proof Carrier" in out


# to_dict


def test_carrier_to_dict():
    c = ProofCarrier(
        op_id="op", candidate_target_files=("x.py",),
        verdict=ProofVerdict.WARN,
        dominant_source=EvidenceSource.MCP_SCAN,
        mcp_finding_count=1, mcp_finding_kinds=("k",),
        coherence_drift_level="", rehearsal_concern_count=0,
        rehearsal_verdict="", boundary_crossed=False,
        diagnostic="x", evaluated_at_unix=1.0,
        elapsed_s=0.0, master_enabled=True,
    )
    d = c.to_dict()
    assert d["verdict"] == "warn"
    assert d["schema_version"] == PROOF_CARRIER_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "proof_carrier_transport.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "source_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


# Flags + SSE


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 4


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_PROOF_CARRIER_BUILT
        == "proof_carrier_built"
    )
    assert "proof_carrier_built" in ios._VALID_EVENT_TYPES
