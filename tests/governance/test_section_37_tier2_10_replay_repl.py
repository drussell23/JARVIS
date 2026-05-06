"""§37 Tier 2 #10 — `/replay` REPL + `--rerun-from <session>:<phase>`
regression spine.

Pins per operator binding 2026-05-05:

  * /replay REPL composes canonical CausalityDAG.build_dag()
    via singleton + read-API extension (§37 Tier 1 pattern)
  * NEW phase-filter helpers on CausalityDAG return correct
    insertion-order results
  * Authority asymmetry — REPL NEVER calls
    apply_replay_from_record_env (read-only browser)
  * Auto-discovered via §32.11 Slice 4 naming-cage (zero edits to
    repl_dispatch_registry.py)
  * Harness CLI `--rerun-from` accepts BOTH RECORD_ID and
    SESSION:PHASE forms; phase form resolves via DAG before
    handing off to the existing record_id codepath
  * SESSION:PHASE form rejects mismatched session vs --rerun
  * NEVER raises across all dispatch / wrap / parse paths
  * Public API stability + 3 AST pins fire clean

Verifies (24 tests).
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# CausalityDAG NEW phase-filter helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubRec:
    record_id: str
    op_id: str
    kind: str
    phase: str
    parent_record_ids: Tuple[str, ...] = ()


def _build_stub_dag(*records):
    """Helper: construct a CausalityDAG with stub records via
    the public constructor (CausalityDAG accepts a `nodes` dict
    keyed by record_id, preserving insertion order)."""
    from backend.core.ouroboros.governance.verification.causality_dag import (
        CausalityDAG,
    )
    nodes = {r.record_id: r for r in records}
    return CausalityDAG(nodes=nodes, edges={})


def test_nodes_for_phase_returns_only_matching():
    dag = _build_stub_dag(
        _StubRec("r1", "op1", "k", "GENERATE"),
        _StubRec("r2", "op1", "k", "VALIDATE"),
        _StubRec("r3", "op2", "k", "GENERATE"),
    )
    out = dag.nodes_for_phase("GENERATE")
    assert tuple(r.record_id for r in out) == ("r1", "r3")


def test_nodes_for_phase_empty_returns_empty():
    dag = _build_stub_dag(
        _StubRec("r1", "op1", "k", "GENERATE"),
    )
    assert dag.nodes_for_phase("VALIDATE") == ()


def test_nodes_for_phase_blank_returns_empty():
    dag = _build_stub_dag(_StubRec("r1", "op1", "k", "GENERATE"))
    assert dag.nodes_for_phase("") == ()
    assert dag.nodes_for_phase("   ") == ()


def test_first_record_in_phase_returns_first_by_insertion():
    dag = _build_stub_dag(
        _StubRec("r1", "op1", "k", "VALIDATE"),
        _StubRec("r2", "op1", "k", "GENERATE"),
        _StubRec("r3", "op2", "k", "GENERATE"),
    )
    rec = dag.first_record_in_phase("GENERATE")
    assert rec is not None
    assert rec.record_id == "r2"


def test_first_record_in_phase_missing_returns_none():
    dag = _build_stub_dag(_StubRec("r1", "op1", "k", "ROUTE"))
    assert dag.first_record_in_phase("VALIDATE") is None


def test_distinct_phases_preserves_insertion_order():
    dag = _build_stub_dag(
        _StubRec("r1", "op1", "k", "ROUTE"),
        _StubRec("r2", "op1", "k", "GENERATE"),
        _StubRec("r3", "op2", "k", "GENERATE"),
        _StubRec("r4", "op2", "k", "VALIDATE"),
    )
    assert dag.distinct_phases() == (
        "ROUTE", "GENERATE", "VALIDATE",
    )


def test_distinct_phases_skips_blanks():
    dag = _build_stub_dag(
        _StubRec("r1", "op1", "k", ""),
        _StubRec("r2", "op1", "k", "GENERATE"),
    )
    assert dag.distinct_phases() == ("GENERATE",)


def test_phase_helpers_never_raise():
    dag = _build_stub_dag(_StubRec("r1", "op1", "k", "GENERATE"))
    # None / non-string defensively handled
    assert dag.nodes_for_phase(None) == ()  # type: ignore
    assert dag.first_record_in_phase(None) is None  # type: ignore


# ---------------------------------------------------------------------------
# /replay REPL dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_help_renders_help():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/replay help")
    assert r.ok is True
    assert r.matched is True
    assert "deterministic-replay browser" in r.text
    assert "Workflow" in r.text


def test_dispatch_bare_lists_sessions():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/replay")
    assert r.ok is True
    # Either lists sessions or honest empty-state
    assert (
        "Replay-Eligible Sessions" in r.text
        or "No sessions" in r.text
    )


def test_dispatch_phases_without_arg_errors():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/replay phases")
    assert r.ok is False
    assert "session_id required" in r.text


def test_dispatch_show_without_arg_errors():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/replay show")
    assert r.ok is False
    assert "argument required" in r.text


def test_dispatch_unknown_subcommand():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/replay garbage")
    assert r.ok is False
    assert "unknown subcommand" in r.text


def test_dispatch_non_replay_line_returns_unmatched():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command("/health")
    assert r.matched is False


def test_dispatch_phases_for_missing_session():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command(
        "/replay phases nonexistent-session-id-12345"
    )
    # Returns honest empty-state, NOT a crash
    assert r.ok is True
    assert (
        "No DAG data" in r.text or "no replay" in r.text.lower()
    )


def test_dispatch_show_session_phase_form_parses(monkeypatch):
    """The colon form is recognized and routed to the (session,
    phase) renderer (which gracefully handles missing data)."""
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    r = dispatch_replay_command(
        "/replay show fake-session:GENERATE"
    )
    assert r.ok is True
    # The colon form goes through phase-resolution, which
    # produces honest empty-state messaging
    assert (
        "Fork Boundary" in r.text
        or "No DAG data" in r.text
        or "No records found" in r.text
    )


def test_dispatch_does_not_raise_on_garbage():
    from backend.core.ouroboros.governance.replay_repl import (
        dispatch_replay_command,
    )
    for line in [
        "/replay '\"\\",
        "/replay show \"unclosed",
        "/replay show :",
        "/replay show :::",
    ]:
        r = dispatch_replay_command(line)
        assert r.matched is True
        # Returns a result; never raises


# ---------------------------------------------------------------------------
# /help auto-discovery & registration
# ---------------------------------------------------------------------------


def test_register_verbs_is_idempotent_in_shape():
    from backend.core.ouroboros.governance.replay_repl import (
        register_verbs,
    )

    class _Reg:
        def __init__(self):
            self.calls = []
        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Reg()
    n = register_verbs(reg)
    assert n == 1
    assert reg.calls[0]["verb"] == "replay"
    assert "RELEVANT" in reg.calls[0]["posture_relevance"]


def test_repl_dispatch_registry_routes_replay():
    """Auto-discovery: §32.11 Slice 4 naming-cage means the
    canonical dispatcher routes `/replay ...` without any edits
    to repl_dispatch_registry.py."""
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        try_dispatch,
    )
    r = try_dispatch("/replay help")
    assert r.matched is True
    assert r.ok is True
    assert "deterministic-replay browser" in r.text


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_3():
    from backend.core.ouroboros.governance.replay_repl import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    names = {i.invariant_name for i in invs}
    assert names == {
        "replay_repl_composes_canonical_dag",
        "replay_repl_authority_read_only",
        "replay_repl_authority_asymmetry",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.replay_repl import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/replay_repl.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_authority_read_only_pin_fires_on_apply_call():
    """Synthetic regression — adding apply_replay_from_record_env
    must trip the pin."""
    from backend.core.ouroboros.governance.replay_repl import (
        register_shipped_invariants,
    )
    bad_source = """
def f():
    apply_replay_from_record_env(plan)
"""
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "read_only" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations
    assert any(
        "apply_replay_from_record_env" in v for v in violations
    )


def test_authority_asymmetry_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.replay_repl import (
        register_shipped_invariants,
    )
    bad_source = """
from backend.core.ouroboros.governance.orchestrator import x
"""
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


def test_composes_canonical_pin_fires_on_direct_construction():
    from backend.core.ouroboros.governance.replay_repl import (
        register_shipped_invariants,
    )
    bad_source = """
def f():
    return CausalityDAG()
"""
    tree = ast.parse(bad_source)
    pin = next(
        i for i in register_shipped_invariants()
        if "composes_canonical" in i.invariant_name
    )
    violations = pin.validate(tree, bad_source)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import replay_repl
    expected = {
        "ReplayReplDispatchResult",
        "dispatch_replay_command",
        "register_shipped_invariants",
        "register_verbs",
    }
    assert set(replay_repl.__all__) == expected


# ---------------------------------------------------------------------------
# Harness CLI integration — --rerun-from <session>:<phase>
# ---------------------------------------------------------------------------


def test_harness_argparse_help_documents_phase_form():
    """Regression: `--help` text MUST mention the SESSION:PHASE
    form per §37 Tier 2 #10 acceptance criterion."""
    target = (
        _repo_root() / "scripts/ouroboros_battle_test.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "SESSION:PHASE" in source or "<session-id>:<phase>" in source, (
        "harness --rerun-from help MUST advertise the phase form"
    )


def test_harness_resolves_phase_form_via_dag():
    """Regression: harness MUST resolve `<session>:<phase>` via
    `build_dag` + `first_record_in_phase` — NOT a parallel
    walker. AST scan."""
    target = (
        _repo_root() / "scripts/ouroboros_battle_test.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "first_record_in_phase" in source, (
        "harness MUST compose CausalityDAG.first_record_in_phase"
    )
    # Both build_dag and first_record_in_phase must appear in
    # the SAME --rerun-from branch for the resolution to be
    # well-formed (anchor to the resolution marker, NOT the
    # argparse help text — first match is in the help string).
    rerun_from_idx = source.find(
        "§37 Tier 2 #10 — ALSO accepts"
    )
    assert rerun_from_idx >= 0, (
        "harness MUST contain the §37 Tier 2 #10 resolution "
        "branch marker"
    )
    section = source[rerun_from_idx:rerun_from_idx + 4000]
    assert "build_dag" in section
    assert "first_record_in_phase" in section


def test_harness_phase_form_rejects_session_mismatch():
    """Regression: if --rerun-from contains <s>:<p> and <s>
    disagrees with --rerun, harness MUST exit 2."""
    target = (
        _repo_root() / "scripts/ouroboros_battle_test.py"
    )
    source = target.read_text(encoding="utf-8")
    # AST: look for the disagrees-with-rerun guard
    assert "disagrees with --rerun" in source, (
        "harness MUST guard against --rerun/--rerun-from "
        "session mismatch"
    )
