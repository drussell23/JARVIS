"""Section 38.11-E (PRD v2.68 to v2.69, 2026-05-08) -
proactive proposal surface regression spine.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11e(monkeypatch):
    for var in (
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED",
        "JARVIS_PROACTIVE_PROPOSAL_PANEL_ENABLED",
        "JARVIS_PROACTIVE_PROPOSAL_PERSISTENCE_ENABLED",
        "JARVIS_PROACTIVE_PROPOSAL_RING_SIZE",
        "JARVIS_PROACTIVE_PROPOSAL_EXPIRY_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.governance import (
        proactive_proposal_surface as p,
    )
    p.reset_ledger_for_tests()
    yield
    p.reset_ledger_for_tests()


# ----------------------------------------------------------- Master flag


def test_master_default_false():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", value,
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        master_enabled,
    )
    assert master_enabled() is True


def test_subflags_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_PANEL_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_PERSISTENCE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        panel_enabled, persistence_enabled,
    )
    assert panel_enabled() is False
    assert persistence_enabled() is False


def test_subflags_default_when_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        panel_enabled, persistence_enabled,
    )
    # panel default-true, persistence default-false
    assert panel_enabled() is True
    assert persistence_enabled() is False


# --------------------------------------------------- Closed taxonomies


def test_proposal_kind_4_values():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind,
    )
    assert {m.name for m in ProposalKind} == {
        "CURIOSITY", "CAPABILITY_GAP",
        "OPPORTUNITY", "ARCHITECTURE",
    }


def test_proposal_decision_4_values():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision,
    )
    assert {m.name for m in ProposalDecision} == {
        "PENDING", "ACCEPTED", "REJECTED", "EXPIRED",
    }


def test_decision_terminal_property():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision,
    )
    assert ProposalDecision.PENDING.is_terminal is False
    assert ProposalDecision.ACCEPTED.is_terminal is True
    assert ProposalDecision.REJECTED.is_terminal is True
    assert ProposalDecision.EXPIRED.is_terminal is True


def test_kind_coerce_lenient():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind,
    )
    assert (
        ProposalKind.coerce("architecture")
        is ProposalKind.ARCHITECTURE
    )
    # Unknown → OPPORTUNITY (default)
    assert ProposalKind.coerce("nonsense") is ProposalKind.OPPORTUNITY


# ------------------------------------------------- Versioned artifact


def test_proactive_proposal_to_dict():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProactiveProposal, PROACTIVE_PROPOSAL_SCHEMA_VERSION,
        ProposalKind, ProposalDecision,
    )
    p = ProactiveProposal(
        proposal_id="abc",
        kind=ProposalKind.CURIOSITY,
        signal_source="src",
        summary="s",
        rationale="r",
        priority_hint=0.7,
        decision=ProposalDecision.PENDING,
    )
    d = p.to_dict()
    assert d["kind"] == "curiosity"
    assert d["decision"] == "pending"
    assert d["schema_version"] == PROACTIVE_PROPOSAL_SCHEMA_VERSION


def test_proactive_proposal_from_dict_round_trip():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProactiveProposal, ProposalDecision, ProposalKind,
    )
    original = ProactiveProposal(
        proposal_id="abc",
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src",
        summary="s",
        rationale="r",
        priority_hint=0.42,
        decision=ProposalDecision.ACCEPTED,
        decided_at_unix=12345.0,
        decision_note="ship it",
    )
    restored = ProactiveProposal.from_dict(original.to_dict())
    assert restored is not None
    assert restored.proposal_id == original.proposal_id
    assert restored.kind is original.kind
    assert restored.decision is original.decision
    assert restored.decision_note == "ship it"
    assert restored.decided_at_unix == 12345.0


def test_from_dict_invalid_returns_none():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProactiveProposal,
    )
    assert ProactiveProposal.from_dict("not a dict") is None
    assert ProactiveProposal.from_dict(None) is None


def test_priority_hint_clamped(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, emit_proposal, get_default_ledger,
    )
    pid = emit_proposal(
        kind=ProposalKind.CURIOSITY,
        signal_source="x",
        summary="x",
        priority_hint=99.0,  # outside [0, 1]
    )
    p = get_default_ledger().get(pid)
    assert p.priority_hint == 1.0
    pid2 = emit_proposal(
        kind=ProposalKind.CURIOSITY,
        signal_source="y",
        summary="y",
        priority_hint=-5.0,
    )
    p2 = get_default_ledger().get(pid2)
    assert p2.priority_hint == 0.0


# ------------------------------------------------------ Producer-bridge


def test_emit_master_off_returns_none():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, emit_proposal,
    )
    pid = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="x",
        summary="x",
    )
    assert pid is None


def test_emit_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, emit_proposal, get_default_ledger,
    )
    pid = emit_proposal(
        kind=ProposalKind.CAPABILITY_GAP,
        signal_source="capability_gap_sensor",
        summary="missing telemetry",
    )
    assert pid is not None
    assert len(pid) == 12  # sha256[:12]
    p = get_default_ledger().get(pid)
    assert p is not None
    assert p.kind is ProposalKind.CAPABILITY_GAP
    assert p.signal_source == "capability_gap_sensor"


def test_emit_idempotent_on_duplicate(monkeypatch):
    """Same (kind, source, summary, emitted_at) → same id."""
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProactiveProposal, ProposalKind, get_default_ledger,
    )
    p = ProactiveProposal(
        proposal_id="dedup1234567",
        kind=ProposalKind.CURIOSITY,
        signal_source="x",
        summary="dup",
    )
    ledger = get_default_ledger()
    a = ledger.record(p)
    b = ledger.record(p)
    assert a is b
    assert len(ledger) == 1


# --------------------------------------------------- Operator decisions


def test_accept_proposal(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision, ProposalKind, accept_proposal,
        emit_proposal, get_default_ledger,
    )
    pid = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src", summary="x",
    )
    assert accept_proposal(pid, note="ship") is True
    p = get_default_ledger().get(pid)
    assert p.decision is ProposalDecision.ACCEPTED
    assert p.decision_note == "ship"


def test_reject_proposal(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision, ProposalKind, emit_proposal,
        get_default_ledger, reject_proposal,
    )
    pid = emit_proposal(
        kind=ProposalKind.CURIOSITY,
        signal_source="src", summary="x",
    )
    assert reject_proposal(pid) is True
    p = get_default_ledger().get(pid)
    assert p.decision is ProposalDecision.REJECTED


def test_decide_unknown_id_returns_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        accept_proposal,
    )
    assert accept_proposal("does-not-exist") is False


def test_decide_terminal_idempotent(monkeypatch):
    """Once ACCEPTED, second accept is no-op (returns True);
    second reject returns False (state mismatch)."""
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, accept_proposal, emit_proposal,
        reject_proposal,
    )
    pid = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src", summary="x",
    )
    assert accept_proposal(pid) is True
    assert accept_proposal(pid) is True   # idempotent
    assert reject_proposal(pid) is False  # state mismatch


# ----------------------------------------------------- Expire sweep


def test_expire_stale_marks_pending_old(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProactiveProposal, ProposalDecision, ProposalKind,
        get_default_ledger,
    )
    ledger = get_default_ledger()
    # Plant an old PENDING proposal directly (bypassing emit
    # to control emitted_at_unix).
    ledger.record(ProactiveProposal(
        proposal_id="oldoldoldold",
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src",
        summary="old",
        emitted_at_unix=time.time() - 100000,  # very old
    ))
    # Plant a fresh one.
    ledger.record(ProactiveProposal(
        proposal_id="newnewnewnew",
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src",
        summary="new",
        emitted_at_unix=time.time(),
    ))
    n = ledger.expire_stale(expiry_s=60)
    assert n == 1
    old = ledger.get("oldoldoldold")
    new = ledger.get("newnewnewnew")
    assert old.decision is ProposalDecision.EXPIRED
    assert new.decision is ProposalDecision.PENDING


# ---------------------------------------------------- Ring eviction


def test_ring_eviction(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_RING_SIZE", "8",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, emit_proposal, get_default_ledger,
        reset_ledger_for_tests,
    )
    reset_ledger_for_tests()
    for i in range(20):
        emit_proposal(
            kind=ProposalKind.OPPORTUNITY,
            signal_source="src",
            summary=f"item-{i}",
        )
        time.sleep(0.001)  # ensure unique emitted_at_unix
    ledger = get_default_ledger()
    assert len(ledger) == 8
    # Oldest dropped — first survivor is item-12
    summaries = [
        p.summary
        for p in ledger.all_proposals(limit=64)
    ]
    assert summaries[0] == "item-12"


# ------------------------------------------------------- Read API


def test_pending_proposals_filter(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, accept_proposal, emit_proposal,
        get_default_ledger,
    )
    pid_a = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src", summary="A",
    )
    pid_b = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src", summary="B",
    )
    accept_proposal(pid_a)
    pending = get_default_ledger().pending_proposals(limit=10)
    pending_ids = {p.proposal_id for p in pending}
    assert pid_b in pending_ids
    assert pid_a not in pending_ids


# ----------------------------------------------------------- Renderer


def test_format_panel_master_off():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        format_proposal_panel,
    )
    assert format_proposal_panel() == ""


def test_format_panel_no_pending(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        format_proposal_panel,
    )
    assert format_proposal_panel() == ""


def test_format_panel_renders_4_kinds(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalKind, emit_proposal, format_proposal_panel,
    )
    for kind in (
        ProposalKind.CURIOSITY,
        ProposalKind.CAPABILITY_GAP,
        ProposalKind.OPPORTUNITY,
        ProposalKind.ARCHITECTURE,
    ):
        emit_proposal(
            kind=kind,
            signal_source="src",
            summary=f"{kind.value} summary",
        )
        time.sleep(0.001)
    out = format_proposal_panel()
    assert "Pending proposals" in out
    # All 4 kind glyphs present
    assert "🔭" in out
    assert "🧩" in out
    assert "💡" in out
    assert "🏛" in out


# ----------------------------------------------------- /proposals REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals help")
    assert r.ok is True
    assert "proactive" in r.text.lower()


def test_repl_panel_master_off_blocks():
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals panel")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_panel_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals panel")
    assert r.ok is True


def test_repl_show_unknown_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals show nonexistent")
    assert r.ok is False
    assert "no proposal" in r.text.lower()


def test_repl_accept_via_command(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision, ProposalKind, emit_proposal,
        get_default_ledger,
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    pid = emit_proposal(
        kind=ProposalKind.OPPORTUNITY,
        signal_source="src", summary="x",
    )
    r = dispatch_proposals_command(
        f"/proposals accept {pid} testing-note",
    )
    assert r.ok is True
    p = get_default_ledger().get(pid)
    assert p.decision is ProposalDecision.ACCEPTED
    assert p.decision_note == "testing-note"


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_expire_returns_count(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals expire")
    assert r.ok is True


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PROACTIVE_PROPOSAL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.proposals_repl import (
        dispatch_proposals_command,
    )
    r = dispatch_proposals_command("/proposals gibberish")
    assert r.ok is False


# ------------------------------------------------------------ AST pins


def _pins():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src():
    return Path(
        "backend/core/ouroboros/governance/"
        "proactive_proposal_surface.py"
    ).read_text()


def test_pins_register_5():
    assert len(_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _pins()
    src = _src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired on canonical "
        f"source: {violations}"
    )


def test_pin_master_default_false_fires():
    pin = next(
        p for p in _pins()
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_kind_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "proposal_kind_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class ProposalKind(str, enum.Enum):\n"
        "    CURIOSITY = 'curiosity'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_decision_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "proposal_decision_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class ProposalDecision(str, enum.Enum):\n"
        "    PENDING = 'pending'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_signal_source_fires_on_missing():
    pin = next(
        p for p in _pins()
        if "composes_canonical_signal_source"
        in p.invariant_name
    )
    bad_src = (
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class ProactiveProposal:\n"
        "    proposal_id: str\n"
        "    summary: str = ''\n"  # missing signal_source
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# ------------------------------------------------------- FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 5  # master + 2 sub-flags + 2 tunables


# ------------------------------------------ Canonical-source smokes


def test_canonical_event_proactive_proposal_emitted_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED,
        _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED
        == "proactive_proposal_emitted"
    )
    assert EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED in _VALID_EVENT_TYPES


def test_canonical_signal_source_importable():
    from backend.core.ouroboros.governance.intent.signals import (
        SignalSource,
    )
    assert SignalSource is not None
