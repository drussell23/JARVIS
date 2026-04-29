"""RR Pass C Slice 6 (CLOSES Pass C) — MetaAdaptationGovernor + /adapt REPL regression suite.

Pins:
  * 12-status DispatchStatus enum + frozen DispatchResult + .ok
    helper + .to_dict shape + module constants.
  * Master flag default-true-post-graduation (Move 1 Pass C cadence
    2026-04-29) + explicit "0" hot-revert.
  * help subcommand always works (even master-off — discoverability).
  * Substrate (ledger) master-off short-circuit: even with REPL on,
    LEDGER_DISABLED returned for read+write subcommands (help still
    works).
  * Subcommand allowlist enforced (UNKNOWN_SUBCOMMAND for anything
    outside it).
  * pending: empty + populated.
  * show: MISSING_PROPOSAL_ID + PROPOSAL_NOT_FOUND + OK.
  * history: default limit + custom limit + invalid + clamp-to-max
    + --surface filter + --surface invalid + --surface missing arg.
  * stats: empty / per-surface aggregation / totals correct.
  * approve: OPERATOR_REQUIRED + MISSING_PROPOSAL_ID + PROPOSAL_NOT_
    FOUND + NOT_PENDING + REASON_REQUIRED + reader-raises +
    OK + LEDGER_REJECTED.
  * reject: same path matrix as approve.
  * Authority invariants (AST grep): no banned governance imports;
    no subprocess+network; substrate+stdlib only.
  * End-to-end: full pipeline (mining → propose → REPL approve →
    proposal landed APPROVED).
"""
from __future__ import annotations

import ast as _ast
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    OperatorDecisionStatus,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.adaptation.meta_governor import (
    DEFAULT_HISTORY_LIMIT,
    DISPATCH_SCHEMA_VERSION,
    DispatchResult,
    DispatchStatus,
    MAX_HISTORY_LIMIT,
    MAX_REASON_CHARS_DISPATCH,
    compute_stats,
    dispatch_adapt,
    is_enabled,
    parse_argv,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "adaptation" / "meta_governor.py"
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPT_REPL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH", str(tmp_path / "ledger.jsonl"),
    )
    yield
    reset_default_ledger()


def _ev(observations=3, summary="evidence"):
    return AdaptationEvidence(
        window_days=7, observation_count=observations,
        source_event_ids=("ev-1",), summary=summary,
    )


def _ledger(tmp_path):
    return AdaptationLedger(tmp_path / "ledger.jsonl")


def _propose(
    ledger, *,
    proposal_id="prop-1",
    surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    proposal_kind="add_pattern",
    current_hash="sha256:current",
    proposed_hash="sha256:proposed",
    observations=3,
):
    # Surface validators from Slices 2-5 may be auto-registered when
    # those modules are imported by other test files in the same run.
    # Build a summary that satisfies all of them: contains both ↑ and
    # ↓ direction indicators, "net +" indicator, "→" arrow, and
    # "insert between" — defense-in-depth so this helper works
    # regardless of which validators are registered.
    summary = (
        "evidence: ↑ raise + ↓ lower; floor 10 → 11; "
        "insert between A and B (net +0.1)."
    )
    # Slice 4a's per-Order budget validator + Slice 3's iron-gate
    # validator + Slice 5's category-weight validator + Slice 2's
    # SemanticGuardian validator — observation_count >= 5 satisfies
    # the strictest threshold (Slice 3+4+5 are 5+; Slice 2 is 3).
    obs = max(observations, 5)
    return ledger.propose(
        proposal_id=proposal_id,
        surface=surface,
        proposal_kind=proposal_kind,
        evidence=AdaptationEvidence(
            window_days=7, observation_count=obs,
            source_event_ids=("ev-1",), summary=summary,
        ),
        current_state_hash=current_hash,
        proposed_state_hash=proposed_hash,
    )


def _stub_reader(response: str = ""):
    return lambda prompt: response


# ===========================================================================
# A — Module constants + enum + frozen result
# ===========================================================================


def test_dispatch_schema_version_pinned():
    assert DISPATCH_SCHEMA_VERSION == 1


def test_default_history_limit_pinned():
    assert DEFAULT_HISTORY_LIMIT == 20


def test_max_history_limit_pinned():
    assert MAX_HISTORY_LIMIT == 500


def test_max_reason_chars_pinned():
    assert MAX_REASON_CHARS_DISPATCH == 1024


def test_dispatch_status_twelve_values():
    assert {s.name for s in DispatchStatus} == {
        "OK", "MASTER_OFF", "UNKNOWN_SUBCOMMAND",
        "MISSING_PROPOSAL_ID", "PROPOSAL_NOT_FOUND", "NOT_PENDING",
        "OPERATOR_REQUIRED", "REASON_REQUIRED", "LEDGER_REJECTED",
        "LEDGER_DISABLED", "INVALID_ARGS", "INTERNAL_ERROR",
    }


def test_dispatch_result_is_frozen():
    r = DispatchResult(
        schema_version=1, subcommand="help",
        status=DispatchStatus.OK,
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.subcommand = "x"  # type: ignore[misc]


def test_dispatch_result_ok_helper():
    ok = DispatchResult(schema_version=1, subcommand="help",
                        status=DispatchStatus.OK)
    bad = DispatchResult(schema_version=1, subcommand="help",
                         status=DispatchStatus.INTERNAL_ERROR)
    assert ok.ok is True
    assert bad.ok is False


def test_dispatch_result_to_dict_shape():
    r = DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="pending", status=DispatchStatus.OK,
        output="x",
    )
    d = r.to_dict()
    assert d["schema_version"] == 1
    assert d["subcommand"] == "pending"
    assert d["status"] == "OK"
    assert d["proposal"] is None
    assert d["proposals"] == []


# ===========================================================================
# B — parse_argv
# ===========================================================================


def test_parse_argv_simple():
    assert parse_argv("show prop-1") == ["show", "prop-1"]


def test_parse_argv_empty():
    assert parse_argv("") == []


def test_parse_argv_quoted():
    assert parse_argv('show "prop with spaces"') == [
        "show", "prop with spaces",
    ]


def test_parse_argv_unbalanced_falls_back():
    assert parse_argv('show "prop') == ["show", '"prop']


# ===========================================================================
# C — Master flag + help bypass + LEDGER_DISABLED
# ===========================================================================


def test_master_default_true_post_graduation(monkeypatch):
    """Graduated 2026-04-29 (Move 1 Pass C cadence) — empty/unset env
    returns True. Asymmetric semantics: explicit falsy hot-reverts."""
    monkeypatch.delenv("JARVIS_ADAPT_REPL_ENABLED", raising=False)
    assert is_enabled() is True


def test_master_off_blocks_subcommands_except_help(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPT_REPL_ENABLED", "0")
    assert is_enabled() is False
    res = dispatch_adapt(["pending"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.MASTER_OFF


def test_master_off_does_not_block_help(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPT_REPL_ENABLED", "0")
    res = dispatch_adapt(["help"])
    assert res.status is DispatchStatus.OK
    assert "/adapt" in res.output


def test_empty_args_returns_help():
    res = dispatch_adapt([])
    assert res.status is DispatchStatus.OK


def test_unknown_subcommand(tmp_path):
    res = dispatch_adapt(["foo"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.UNKNOWN_SUBCOMMAND
    assert "foo" in res.detail


def test_subcommand_normalized_to_lowercase(tmp_path):
    res = dispatch_adapt(["PENDING"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.OK


def test_ledger_master_off_returns_ledger_disabled(monkeypatch, tmp_path):
    """Even with REPL on, if the ledger master is off, all read+write
    subcommands return LEDGER_DISABLED."""
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "0")
    res = dispatch_adapt(["pending"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.LEDGER_DISABLED


def test_ledger_master_off_does_not_block_help(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "0")
    res = dispatch_adapt(["help"])
    assert res.status is DispatchStatus.OK


# ===========================================================================
# D — pending
# ===========================================================================


def test_pending_empty(tmp_path):
    res = dispatch_adapt(["pending"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.OK
    assert "No pending" in res.output


def test_pending_lists_proposals(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    _propose(led, proposal_id="p-2", proposal_kind="raise_floor",
             surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS)
    res = dispatch_adapt(["pending"], ledger=led)
    assert res.status is DispatchStatus.OK
    assert "p-1" in res.output
    assert "p-2" in res.output
    assert len(res.proposals) == 2


# ===========================================================================
# E — show
# ===========================================================================


def test_show_missing_proposal_id(tmp_path):
    res = dispatch_adapt(["show"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.MISSING_PROPOSAL_ID


def test_show_proposal_not_found(tmp_path):
    res = dispatch_adapt(["show", "missing"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.PROPOSAL_NOT_FOUND


def test_show_renders_full_proposal(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(["show", "p-1"], ledger=led)
    assert res.status is DispatchStatus.OK
    assert "p-1" in res.output
    assert res.proposal is not None
    assert "Surface:" in res.output
    assert "Kind:" in res.output
    assert "semantic_guardian.patterns" in res.output


# ===========================================================================
# F — history
# ===========================================================================


def test_history_default_limit(tmp_path):
    led = _ledger(tmp_path)
    for i in range(5):
        _propose(led, proposal_id=f"p-{i}")
    res = dispatch_adapt(["history"], ledger=led)
    assert res.status is DispatchStatus.OK
    assert len(res.proposals) == 5


def test_history_custom_limit(tmp_path):
    led = _ledger(tmp_path)
    for i in range(10):
        _propose(led, proposal_id=f"p-{i}")
    res = dispatch_adapt(["history", "3"], ledger=led)
    assert res.status is DispatchStatus.OK
    assert len(res.proposals) <= 3


def test_history_invalid_limit(tmp_path):
    res = dispatch_adapt(["history", "abc"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.INVALID_ARGS


def test_history_zero_limit(tmp_path):
    res = dispatch_adapt(["history", "0"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.INVALID_ARGS


def test_history_clamped_to_max(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(["history", "9999"], ledger=led)
    assert res.status is DispatchStatus.OK


def test_history_surface_filter(tmp_path):
    led = _ledger(tmp_path)
    _propose(
        led, proposal_id="p-sg",
        surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
    )
    _propose(
        led, proposal_id="p-ig",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
        proposal_kind="raise_floor",
    )
    res = dispatch_adapt(
        ["history", "--surface", "iron_gate.exploration_floors"],
        ledger=led,
    )
    assert res.status is DispatchStatus.OK
    for p in res.proposals:
        assert p.surface is AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS


def test_history_invalid_surface(tmp_path):
    res = dispatch_adapt(
        ["history", "--surface", "fake_surface"],
        ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.INVALID_ARGS


def test_history_surface_missing_arg(tmp_path):
    res = dispatch_adapt(
        ["history", "--surface"], ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.INVALID_ARGS


# ===========================================================================
# G — stats
# ===========================================================================


def test_stats_empty(tmp_path):
    res = dispatch_adapt(["stats"], ledger=_ledger(tmp_path))
    assert res.status is DispatchStatus.OK
    assert res.stats["totals"] == {
        "pending": 0, "approved": 0, "rejected": 0,
    }


def test_stats_per_surface_aggregation(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")  # pending in semantic_guardian
    _propose(
        led, proposal_id="p-2",
        surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
        proposal_kind="raise_floor",
    )
    led.approve("p-1", operator="alice")
    res = dispatch_adapt(["stats"], ledger=led)
    assert res.status is DispatchStatus.OK
    assert res.stats["totals"]["approved"] == 1
    assert res.stats["totals"]["pending"] == 1
    sg_key = AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS.value
    ig_key = AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS.value
    assert res.stats["per_surface"][sg_key]["approved"] == 1
    assert res.stats["per_surface"][ig_key]["pending"] == 1


def test_compute_stats_directly(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    _propose(led, proposal_id="p-2")
    led.reject("p-2", operator="bob")
    s = compute_stats(led)
    assert s["totals"]["pending"] == 1
    assert s["totals"]["rejected"] == 1


# ===========================================================================
# H — approve
# ===========================================================================


def test_approve_operator_required(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="", reader=_stub_reader("ok"), ledger=led,
    )
    assert res.status is DispatchStatus.OPERATOR_REQUIRED


def test_approve_missing_proposal_id(tmp_path):
    res = dispatch_adapt(
        ["approve"],
        operator="alice", reader=_stub_reader("r"),
        ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.MISSING_PROPOSAL_ID


def test_approve_proposal_not_found(tmp_path):
    res = dispatch_adapt(
        ["approve", "missing"],
        operator="alice", reader=_stub_reader("r"),
        ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.PROPOSAL_NOT_FOUND


def test_approve_not_pending(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    led.approve("p-1", operator="alice")  # already terminal
    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="bob", reader=_stub_reader("r"), ledger=led,
    )
    assert res.status is DispatchStatus.NOT_PENDING


def test_approve_empty_reason(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="alice", reader=_stub_reader(""), ledger=led,
    )
    assert res.status is DispatchStatus.REASON_REQUIRED


def test_approve_reader_raises(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")

    def _broken(prompt):
        raise RuntimeError("reader broke")

    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="alice", reader=_broken, ledger=led,
    )
    assert res.status is DispatchStatus.INTERNAL_ERROR
    assert "reader_failed" in res.detail


def test_approve_ok_records_application(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="alice", reader=_stub_reader("approved by review"),
        ledger=led,
    )
    assert res.status is DispatchStatus.OK
    assert res.proposal is not None
    assert res.proposal.operator_decision is OperatorDecisionStatus.APPROVED
    assert res.proposal.operator_decision_by == "alice"
    assert res.proposal.applied_at is not None


# ===========================================================================
# I — reject
# ===========================================================================


def test_reject_operator_required(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["reject", "p-1"],
        operator="", reader=_stub_reader("r"), ledger=led,
    )
    assert res.status is DispatchStatus.OPERATOR_REQUIRED


def test_reject_missing_proposal_id(tmp_path):
    res = dispatch_adapt(
        ["reject"],
        operator="alice", reader=_stub_reader("r"),
        ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.MISSING_PROPOSAL_ID


def test_reject_proposal_not_found(tmp_path):
    res = dispatch_adapt(
        ["reject", "missing"],
        operator="alice", reader=_stub_reader("r"),
        ledger=_ledger(tmp_path),
    )
    assert res.status is DispatchStatus.PROPOSAL_NOT_FOUND


def test_reject_not_pending(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    led.reject("p-1", operator="alice")  # already terminal
    res = dispatch_adapt(
        ["reject", "p-1"],
        operator="bob", reader=_stub_reader("r"), ledger=led,
    )
    assert res.status is DispatchStatus.NOT_PENDING


def test_reject_empty_reason(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["reject", "p-1"],
        operator="alice", reader=_stub_reader(""), ledger=led,
    )
    assert res.status is DispatchStatus.REASON_REQUIRED


def test_reject_ok_records_decision(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    res = dispatch_adapt(
        ["reject", "p-1"],
        operator="bob", reader=_stub_reader("not aligned with gates"),
        ledger=led,
    )
    assert res.status is DispatchStatus.OK
    assert res.proposal is not None
    assert res.proposal.operator_decision is OperatorDecisionStatus.REJECTED
    assert res.proposal.applied_at is None


def test_reason_truncated_at_max(tmp_path):
    led = _ledger(tmp_path)
    _propose(led, proposal_id="p-1")
    long_reason = "x" * (MAX_REASON_CHARS_DISPATCH + 100)
    res = dispatch_adapt(
        ["approve", "p-1"],
        operator="alice", reader=_stub_reader(long_reason), ledger=led,
    )
    assert res.status is DispatchStatus.OK
    # The reason was clipped before reaching the substrate. The
    # substrate's approve() doesn't store the reason currently
    # (Slice 1 contract), but the REPL still echoes it back in
    # output truncated to MAX.
    assert "Approved p-1" in res.output


# ===========================================================================
# J — End-to-end (mining surface → REPL approve)
# ===========================================================================


def test_end_to_end_mine_propose_approve(tmp_path, monkeypatch):
    """Pin: an op-formed adaptation flows from a mining surface
    through the substrate through the REPL → APPLIED state."""
    monkeypatch.setenv("JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1")
    from backend.core.ouroboros.governance.adaptation.semantic_guardian_miner import (
        PostmortemEventLite,
        propose_patterns_from_events,
    )
    led = _ledger(tmp_path)
    events = [
        PostmortemEventLite(
            op_id=f"op-{i}",
            root_cause="critical_pattern_missing",
            failure_class="code",
            code_snippet_excerpt=f"DETECTOR_PATTERN_XYZABC{i}",
            timestamp_unix=time.time(),
        )
        for i in range(3)
    ]
    out = propose_patterns_from_events(events, ledger=led)
    assert len(out) == 1
    proposal_id = out[0].proposal_id
    # Verify pending count includes our proposal
    pending_res = dispatch_adapt(["pending"], ledger=led)
    assert any(p.proposal_id == proposal_id
               for p in pending_res.proposals)
    # Approve it through the REPL
    approve_res = dispatch_adapt(
        ["approve", proposal_id],
        operator="alice",
        reader=_stub_reader("validated end-to-end"),
        ledger=led,
    )
    assert approve_res.status is DispatchStatus.OK
    assert approve_res.proposal is not None
    assert (
        approve_res.proposal.operator_decision
        is OperatorDecisionStatus.APPROVED
    )
    assert approve_res.proposal.applied_at is not None
    # Verify stats reflect the approval
    stats_res = dispatch_adapt(["stats"], ledger=led)
    assert stats_res.stats["totals"]["approved"] >= 1


# ===========================================================================
# K — Authority invariants (AST grep on module source)
# ===========================================================================


def test_module_has_no_banned_governance_imports():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_substrings = (
        "orchestrator",
        "iron_gate",
        "change_engine",
        "candidate_generator",
        "risk_tier_floor",
        "semantic_guardian",
        "semantic_firewall",
        "scoped_tool_backend",
        ".gate.",
        "phase_runners",
        "providers",
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
    assert not found_banned, (
        f"meta_governor.py contains banned imports: {found_banned}"
    )


def test_module_imports_only_substrate_and_stdlib():
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    stdlib_prefixes = (
        "__future__",
        "enum", "logging", "os", "shlex", "dataclasses", "typing",
    )
    allowed_governance = (
        "backend.core.ouroboros.governance.adaptation.ledger",
    )
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            ok = (
                any(mod == p or mod.startswith(p + ".")
                    for p in stdlib_prefixes)
                or mod in allowed_governance
            )
            assert ok, f"unauthorized import {mod!r}"


def test_module_does_not_call_subprocess_or_network():
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "subprocess.",
        "socket.",
        "urllib.",
        "requests.",
        "http.client",
        "os." + "system(",
        "shutil.rmtree(",
    )
    found = [tok for tok in forbidden if tok in src]
    assert not found


def test_module_does_not_call_llm():
    """Cage check (Pass C §4.4): zero LLM in the cage."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    forbidden_tokens = (
        "messages.create(",
        "anthropic.Anthropic(",
        "ClaudeProvider(",
        "from openai",
    )
    found = [tok for tok in forbidden_tokens if tok in src]
    assert not found


def test_module_does_not_import_other_adaptive_surfaces():
    """The meta_governor must NOT import the 4 mining-surface
    modules. Each surface registers its own validator at import
    time; the meta_governor only consumes the AdaptationLedger
    surface. Pin to keep the substrate acyclic."""
    tree = _ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    banned_surface_modules = (
        "semantic_guardian_miner",
        "exploration_floor_tightener",
        "per_order_mutation_budget",
        "risk_tier_extender",
        "category_weight_rebalancer",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for surface_mod in banned_surface_modules:
                assert surface_mod not in mod, (
                    f"meta_governor MUST NOT import surface module "
                    f"{surface_mod!r} (found: {mod})"
                )
