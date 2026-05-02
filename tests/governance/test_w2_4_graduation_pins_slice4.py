"""W2(4) Slice 4 — graduation pin tests.

Pins the post-graduation contract for the curiosity engine. These tests
run on every commit going forward; if any pin breaks, either:

* The change was an unintentional regression — fix the change.
* The contract is intentionally being expanded — update the pin AND
  the hot-revert documentation.

The master-off invariant is non-negotiable per the operator binding:
``JARVIS_CURIOSITY_ENABLED=false`` MUST restore byte-for-byte pre-W2(4)
behavior at every layer (env knobs, budget tracker, Rule 14, SSE bridge,
ledger persistence).

Pin coverage:

A. Master flag default is now **True** (Slice 4 flip).
B. Sub-flag composition under master-on default — strict caps preserved
   (3 questions / $0.05 / EXPLORE+CONSOLIDATE), SSE stays default-off
   (operator opt-in), ledger persists default-on.
C. Hot-revert path: ``JARVIS_CURIOSITY_ENABLED=false`` force-disables
   every sub-flag regardless of their individual env values.
D. Rule 14 widening surface — Allowed at SAFE_AUTO when budget bound
   AND posture allowed AND quota+cost OK; reverts to legacy
   ``tool.denied.ask_human_low_risk`` when master off.
E. Authority invariants — SSE event vocabulary additive only, schema
   ``curiosity.1`` frozen, IDE GET routes read-only, BLOCKED tier
   never widened.
F. Source-grep pins — code shape that must survive drift.
G. Hot-revert documentation pin — every env knob discoverable in source.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_curiosity_contextvar():
    """ContextVar pollution prevention — graduation tests bind budgets to
    the ambient ``curiosity_budget_var``; reset to None before AND after
    each test so this suite doesn't leak state into adjacent suites
    (test_curiosity_engine_slice1's ``test_current_curiosity_budget_default_none``
    is the canonical adjacent victim)."""
    from backend.core.ouroboros.governance.curiosity_engine import (
        curiosity_budget_var,
    )
    curiosity_budget_var.set(None)
    yield
    curiosity_budget_var.set(None)


# ---------------------------------------------------------------------------
# (A) Master flag default — graduation flip
# ---------------------------------------------------------------------------


def test_master_flag_defaults_true_post_slice_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_CURIOSITY_ENABLED defaults to True after Slice 4 graduation.

    If this test fails post-merge, either the flip was reverted (fix) or
    the operator chose to revert the graduation (update the pin and the
    scope-doc graduation appendix)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        curiosity_enabled,
    )
    assert curiosity_enabled() is True


# ---------------------------------------------------------------------------
# (B) Sub-flag composition under master-on default
# ---------------------------------------------------------------------------


def test_sse_subflag_default_off_even_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE bridge requires explicit opt-in via JARVIS_CURIOSITY_SSE_ENABLED.
    Master-on alone does not start publishing curiosity_question_emitted
    events. Mirrors W3(7) cancel SSE sub-flag default."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CURIOSITY_SSE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        curiosity_enabled,
        sse_enabled,
    )
    assert curiosity_enabled() is True   # master on by default
    assert sse_enabled() is False         # but SSE still off


def test_questions_per_session_defaults_3_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default per-session quota stays at 3 (operator binding)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        questions_per_session,
    )
    assert questions_per_session() == 3


def test_cost_cap_defaults_005_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default per-question cost cap stays at $0.05 (operator binding)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CURIOSITY_COST_CAP_USD", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        cost_cap_usd,
    )
    assert cost_cap_usd() == 0.05


def test_posture_allowlist_defaults_explore_consolidate_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default allowlist stays {EXPLORE, CONSOLIDATE} — HARDEN/MAINTAIN
    excluded by design (operator binding)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CURIOSITY_POSTURE_ALLOWLIST", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        posture_allowlist,
    )
    assert posture_allowlist() == frozenset({"EXPLORE", "CONSOLIDATE"})


def test_ledger_persist_defaults_true_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledger persistence defaults true when master is on (operators can
    disable via JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED=false for log-only)."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        ledger_persist_enabled,
    )
    assert ledger_persist_enabled() is True


# ---------------------------------------------------------------------------
# (C) Hot-revert path — master=false force-disables every sub-flag
# ---------------------------------------------------------------------------


def test_hot_revert_master_off_disables_all_subflags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_CURIOSITY_ENABLED=false force-disables every sub-flag
    regardless of their individual env values. The single env-var revert
    is the operator's only-knob hot-revert path (mirrors W3(7) cancel
    master-off composition)."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    # Even if operator tries to enable sub-flags, they must stay off
    monkeypatch.setenv("JARVIS_CURIOSITY_SSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURIOSITY_QUESTIONS_PER_SESSION", "100")
    monkeypatch.setenv("JARVIS_CURIOSITY_COST_CAP_USD", "1.00")
    monkeypatch.setenv("JARVIS_CURIOSITY_POSTURE_ALLOWLIST", "EXPLORE,HARDEN,MAINTAIN")
    from backend.core.ouroboros.governance.curiosity_engine import (
        cost_cap_usd,
        curiosity_enabled,
        ledger_persist_enabled,
        posture_allowlist,
        questions_per_session,
        sse_enabled,
    )
    assert curiosity_enabled() is False
    assert sse_enabled() is False
    assert ledger_persist_enabled() is False
    assert questions_per_session() == 0
    assert cost_cap_usd() == 0.0
    assert posture_allowlist() == frozenset()


def test_hot_revert_master_off_try_charge_returns_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master-off → try_charge always returns Denied(MASTER_OFF), counter
    never increments past 0."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityBudget,
        DenyReason,
    )
    bud = CuriosityBudget(op_id="op-revert", posture_at_arm="EXPLORE")
    result = bud.try_charge(question_text="x", est_cost_usd=0.01)
    assert result.allowed is False
    assert result.deny_reason is DenyReason.MASTER_OFF
    assert bud.questions_used == 0


# ---------------------------------------------------------------------------
# (D) Rule 14 widening surface — end-to-end policy gate
# ---------------------------------------------------------------------------


def test_rule_14_safe_auto_allowed_when_all_gates_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Master on default + budget bound + posture in allowlist + quota OK
    → SAFE_AUTO ask_human is allowed via curiosity widening."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityBudget,
        curiosity_budget_var,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier
    from backend.core.ouroboros.governance.tool_executor import (
        GoverningToolPolicy,
        PolicyContext,
        PolicyDecision,
        ToolCall,
    )

    bud = CuriosityBudget(op_id="op-w24-grad", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = PolicyContext(
        repo="jarvis", repo_root=tmp_path,
        op_id="op-w24-grad", call_id="op-w24-grad:r0:ask_human",
        round_index=0, risk_tier=RiskTier.SAFE_AUTO, is_read_only=False,
    )
    gate = GoverningToolPolicy(repo_roots={"jarvis": tmp_path})
    result = gate.evaluate(
        ToolCall(name="ask_human", arguments={"question": "what?"}),
        ctx,
    )
    # Allowed (curiosity widening fired)
    assert result.decision is PolicyDecision.ALLOW
    # Counter incremented
    assert bud.questions_used == 1


def test_rule_14_safe_auto_legacy_reject_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Hot-revert: master off → SAFE_AUTO ask_human reverts to legacy
    ``tool.denied.ask_human_low_risk`` reject (byte-for-byte pre-W2(4))."""
    monkeypatch.setenv("JARVIS_CURIOSITY_ENABLED", "false")
    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityBudget,
        curiosity_budget_var,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier
    from backend.core.ouroboros.governance.tool_executor import (
        GoverningToolPolicy,
        PolicyContext,
        PolicyDecision,
        ToolCall,
    )

    bud = CuriosityBudget(op_id="op-revert-rule14", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = PolicyContext(
        repo="jarvis", repo_root=tmp_path,
        op_id="op-revert-rule14",
        call_id="op-revert-rule14:r0:ask_human",
        round_index=0, risk_tier=RiskTier.SAFE_AUTO, is_read_only=False,
    )
    gate = GoverningToolPolicy(repo_roots={"jarvis": tmp_path})
    result = gate.evaluate(
        ToolCall(name="ask_human", arguments={"question": "what?"}),
        ctx,
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code == "tool.denied.ask_human_low_risk"
    # Counter unchanged (master-off → MASTER_OFF deny)
    assert bud.questions_used == 0


def test_rule_14_blocked_tier_rejected_even_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """BLOCKED tier ALWAYS rejects ask_human, regardless of master state.
    The 'no gate softening' invariant — even post-graduation, BLOCKED ops
    cannot interact with the human."""
    monkeypatch.delenv("JARVIS_CURIOSITY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityBudget,
        curiosity_budget_var,
    )
    from backend.core.ouroboros.governance.risk_engine import RiskTier
    from backend.core.ouroboros.governance.tool_executor import (
        GoverningToolPolicy,
        PolicyContext,
        PolicyDecision,
        ToolCall,
    )

    bud = CuriosityBudget(op_id="op-blocked", posture_at_arm="EXPLORE")
    curiosity_budget_var.set(bud)

    ctx = PolicyContext(
        repo="jarvis", repo_root=tmp_path,
        op_id="op-blocked", call_id="op-blocked:r0:ask_human",
        round_index=0, risk_tier=RiskTier.BLOCKED, is_read_only=False,
    )
    gate = GoverningToolPolicy(repo_roots={"jarvis": tmp_path})
    result = gate.evaluate(
        ToolCall(name="ask_human", arguments={"question": "what?"}),
        ctx,
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code == "tool.denied.ask_human_blocked_op"


# ---------------------------------------------------------------------------
# (E) Authority invariants — SSE vocab additive, schema frozen
# ---------------------------------------------------------------------------


def test_sse_event_vocabulary_includes_curiosity_question_emitted() -> None:
    """Slice 3 added curiosity_question_emitted to the additive vocab.
    Removing it would break the IDE clients' wire-format API."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_CURIOSITY_QUESTION_EMITTED == "curiosity_question_emitted"
    assert EVENT_TYPE_CURIOSITY_QUESTION_EMITTED in _VALID_EVENT_TYPES


def test_sse_event_vocabulary_count_is_41_post_slice_4() -> None:
    """Property-based additive-only contract: post-Slice-4 floor is 41
    (Slice 3 added curiosity_question_emitted; Slice 4 is graduation
    only). The vocabulary may grow as later arcs add events, but must
    NEVER shrink — a shrink means an event type was REMOVED, which is
    a wire-format contract break."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    _SLICE_4_FLOOR = 41
    assert len(_VALID_EVENT_TYPES) >= _SLICE_4_FLOOR, (
        f"SSE event vocabulary SHRANK below post-Slice-4 floor: "
        f"got {len(_VALID_EVENT_TYPES)}, floor {_SLICE_4_FLOOR}. "
        "An event type was REMOVED — wire-format contract break. "
        "Fix the source (re-add the missing event), don't lower this "
        "floor."
    )


def test_curiosity_record_schema_version_is_curiosity_1() -> None:
    """The CuriosityRecord schema is wire-format API. Schema bumps need
    additive migration semantics (same contract as cancel.1)."""
    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityRecord,
    )
    rec = CuriosityRecord(
        schema_version="curiosity.1",
        question_id="qid",
        op_id="op-schema",
        posture_at_charge="EXPLORE",
        question_text="?",
        est_cost_usd=0.01,
        issued_at_monotonic=0.0,
        issued_at_iso="2026-04-25T03:00:00Z",
        result="allowed",
    )
    assert rec.schema_version == "curiosity.1"


def test_deny_reason_vocabulary_stable() -> None:
    """The 5 deny-reason values are wire-format API for the JSONL ledger.
    Renames break operator dashboards / log-grep + IDE GET filters."""
    from backend.core.ouroboros.governance.curiosity_engine import DenyReason
    expected = {
        "master_off",
        "posture_disallowed",
        "questions_exhausted",
        "cost_exceeded",
        "invalid_question",
    }
    actual = {dr.value for dr in DenyReason}
    assert actual == expected, (
        f"DenyReason vocabulary changed: {actual} != {expected}. "
        "If intentional, update operator-facing audit docs."
    )


# ---------------------------------------------------------------------------
# (F) Source-grep pins — code shape that must survive drift
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_curiosity_engine_master_default_true() -> None:
    """The `curiosity_enabled()` reader literal-defaults to True."""
    src = _read("backend/core/ouroboros/governance/curiosity_engine.py")
    assert '_env_bool("JARVIS_CURIOSITY_ENABLED", True)' in src, (
        "Master flag default must be True post-Slice-4 graduation. "
        "If this pin fails, either the flip was reverted or the env_bool "
        "call was refactored — update both the source and this pin."
    )


def test_pin_rule_14_widening_present_in_tool_executor() -> None:
    """tool_executor Rule 14 imports curiosity_engine for the widening."""
    src = _read("backend/core/ouroboros/governance/tool_executor.py")
    assert "from backend.core.ouroboros.governance.curiosity_engine" in src
    assert "current_curiosity_budget" in src
    assert "tool.denied.ask_human_low_risk" in src
    assert "tool.denied.ask_human_blocked_op" in src


def test_pin_generate_runner_binds_budget_to_contextvar() -> None:
    """generate_runner.py binds CuriosityBudget at GENERATE entry."""
    src = _read("backend/core/ouroboros/governance/phase_runners/generate_runner.py")
    assert "curiosity_engine" in src
    assert "curiosity_budget_var" in src
    assert "CuriosityBudget" in src


def test_pin_curiosity_engine_has_sse_bridge() -> None:
    """curiosity_engine has bridge_curiosity_to_sse + sse_enabled gate."""
    src = _read("backend/core/ouroboros/governance/curiosity_engine.py")
    assert "def bridge_curiosity_to_sse" in src
    assert "def sse_enabled" in src
    # The bridge is invoked from _record_decision
    assert "bridge_curiosity_to_sse(record)" in src


def test_pin_ide_observability_curiosity_routes() -> None:
    """IDE observability has /observability/curiosity routes (Slice 3)."""
    src = _read("backend/core/ouroboros/governance/ide_observability.py")
    assert "/observability/curiosity" in src
    assert "_handle_curiosity_list" in src
    assert "_handle_curiosity_detail" in src
    assert "_read_curiosity_records" in src


def test_pin_master_off_composition_all_subflag_readers() -> None:
    """All sub-flag readers gate on `if not curiosity_enabled():` first.
    This is the structural enforcement of the master-off invariant."""
    src = _read("backend/core/ouroboros/governance/curiosity_engine.py")
    # Each of these 5 sub-flag readers must short-circuit on master-off
    for fn in (
        "def questions_per_session",
        "def cost_cap_usd",
        "def posture_allowlist",
        "def sse_enabled",
        "def ledger_persist_enabled",
    ):
        # Find the function and confirm `if not curiosity_enabled()`
        # appears within ~10 lines after it.
        idx = src.find(fn)
        assert idx >= 0, f"Function {fn!r} not found in curiosity_engine.py"
        window = src[idx: idx + 800]
        assert "if not curiosity_enabled()" in window, (
            f"{fn} missing master-off composition gate; the master-off "
            "invariant requires every sub-flag reader to short-circuit "
            "when curiosity_enabled() is False."
        )


# ---------------------------------------------------------------------------
# (G) Hot-revert documentation — env vars discoverable in source
# ---------------------------------------------------------------------------


def test_full_env_var_revert_matrix_documented() -> None:
    """Every W2(4) env knob documented in the curiosity_engine.py source.
    Hot-revert recipe must be discoverable via grep."""
    src = _read("backend/core/ouroboros/governance/curiosity_engine.py")
    for env_var in (
        "JARVIS_CURIOSITY_ENABLED",
        "JARVIS_CURIOSITY_QUESTIONS_PER_SESSION",
        "JARVIS_CURIOSITY_COST_CAP_USD",
        "JARVIS_CURIOSITY_POSTURE_ALLOWLIST",
        "JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED",
        "JARVIS_CURIOSITY_SSE_ENABLED",
    ):
        assert env_var in src, (
            f"{env_var} not discoverable in curiosity_engine.py — "
            "operators rely on grep for hot-revert recipe lookup."
        )


def test_runbook_documents_w2_4_hot_revert() -> None:
    """The operations runbook documents the W2(4) hot-revert recipe."""
    runbook = Path("docs/operations/curiosity-graduation.md")
    assert runbook.exists(), (
        "docs/operations/curiosity-graduation.md must exist post-Slice-4 "
        "as the operator-facing hot-revert reference."
    )
    txt = runbook.read_text(encoding="utf-8")
    assert "JARVIS_CURIOSITY_ENABLED=false" in txt
    assert "hot-revert" in txt.lower()
