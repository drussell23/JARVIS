"""Tests for scripts/a1_graduation_auditor.py — the Real-Time SSE
GraduationAuditor + Absolute Intervention-Lock.

Synthetic event/log streams ONLY — no network, no live soak. We inject events
into the pure auditor core and assert the verdict math, the A1Trace hop
ordering, the honest UNVERIFIABLE state, and the fail-CLOSED intervention-lock.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the standalone script by path (it lives in scripts/, not a package).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "a1_graduation_auditor.py"
_spec = importlib.util.spec_from_file_location("a1_graduation_auditor", _SCRIPT)
assert _spec and _spec.loader
aud = importlib.util.module_from_spec(_spec)
sys.modules["a1_graduation_auditor"] = aud
_spec.loader.exec_module(aud)


# ---------------------------------------------------------------------------
# Helpers — build a fully-passing event/log timeline, then mutate per test.
# ---------------------------------------------------------------------------


def _all_flags():
    """Use a small explicit flag set covering every observable family + one
    deliberately-UNVERIFIABLE flag, so we don't depend on CADENCE_POLICY's
    exact membership for the unit assertions."""
    return [
        "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS",  # semantic_guardian
        "JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS",   # iron_gate
        "JARVIS_RISK_TIER_FLOOR_LOAD_ADAPTED_TIERS",       # risk_tier
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",            # decision_trace
        "JARVIS_TOTALLY_UNOBSERVABLE_FLAG",                # no family -> UNVERIFIABLE
    ]


def _make_auditor(strict=True, flags=None):
    return aud.A1GraduationAuditor(flags=flags or _all_flags(), strict=strict)


def _feed_passing(a, *, include_unverifiable_eval=True, include_pr=True):
    """Drive an auditor through a full happy-path A1 dispatch."""
    # 5 A1Trace hops in order (log source).
    for hop in aud.A1TRACE_HOPS:
        a.ingest_log_line(f"WARNING [A1Trace] {hop} goal=GOAL-001 op=op-abc")
    # FSM CLASSIFY ... APPLY.
    a.ingest_event("fsm_phase_changed", {"phase": "CLASSIFY", "op_id": "op-abc", "risk_tier": "safe_auto"})
    a.ingest_event("fsm_phase_changed", {"phase": "GENERATE", "op_id": "op-abc"})
    a.ingest_event("fsm_phase_changed", {"phase": "APPLY", "op_id": "op-abc", "risk_tier": "notify_apply"})
    # Gate telemetry proving each observable flag-family evaluated (not rejected).
    a.ingest_log_line("[SemanticGuard] op=op-abc findings=0")            # semantic_guardian
    a.ingest_event("tool_exploration_start", {"op_id": "op-abc"})        # iron_gate + scoped
    a.ingest_event("decision_recorded", {"op_id": "op-abc"})            # decision_trace + phase8
    # state=applied terminal.
    a.ingest_event("operation_terminal", {"op_id": "op-abc", "state": "applied", "phase": "APPLY"})
    if include_pr:
        a.ingest_event("review_branch_created", {"op_id": "op-abc"})


# ===========================================================================
# 1. SSE parser — multi-event + id/replay
# ===========================================================================


def test_sse_block_parses_event_id_and_payload():
    block = "id: 42\nevent: fsm_phase_changed\ndata: {\"event_type\": \"fsm_phase_changed\", \"payload\": {\"phase\": \"APPLY\"}}"
    et, eid, payload = aud.parse_sse_block(block)
    assert et == "fsm_phase_changed"
    assert eid == "42"
    assert payload["payload"]["phase"] == "APPLY"


def test_sse_block_heartbeat_and_malformed_never_raise():
    et, eid, payload = aud.parse_sse_block(": heartbeat\n")
    assert payload is None
    et2, _eid2, payload2 = aud.parse_sse_block("event: x\ndata: {not json")
    assert payload2 is None  # malformed JSON -> None, no raise


def test_envelope_to_event_unwraps_inner_payload():
    et, inner = aud.envelope_to_event(
        "fsm_phase_changed",
        {"event_type": "fsm_phase_changed", "payload": {"phase": "GENERATE"}},
    )
    assert et == "fsm_phase_changed"
    assert inner["phase"] == "GENERATE"


# ===========================================================================
# 2. A1Trace 5 hops in order
# ===========================================================================


def test_a1trace_five_hops_in_order_met():
    a = _make_auditor()
    for hop in aud.A1TRACE_HOPS:
        a.ingest_log_line(f"[A1Trace] {hop} goal=G1")
    assert a.trace.all_hops_in_order() is True
    assert a.trace.winning_goal() == "G1"


def test_a1trace_out_of_order_not_met():
    a = _make_auditor()
    # submit before dequeue for the same goal.
    for hop in ("emit", "ingest", "submit", "dequeue", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=G1")
    assert a.trace.all_hops_in_order() is False


def test_a1trace_partial_not_met():
    a = _make_auditor()
    for hop in ("emit", "ingest", "dequeue"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=G1")
    assert a.trace.all_hops_in_order() is False


def test_a1trace_line_parser_returns_none_on_non_trace():
    assert aud.parse_a1trace_line("just a normal log line") is None
    assert aud.parse_a1trace_line("[A1Trace] emit goal=GOAL-7") == ("emit", "GOAL-7")


# ===========================================================================
# 3. The 12-flag audit — PASS / FAIL(rejected) / UNVERIFIABLE(strict vs lenient)
# ===========================================================================


def test_flag_audit_pass_when_all_observable_evaluated_lenient():
    # Lenient: the unobservable flag warns (UNVERIFIABLE) but does not fail.
    a = _make_auditor(strict=False)
    _feed_passing(a)
    passed, locus = a._flag_audit_passed()
    assert passed is True
    assert locus == ""


def test_flag_audit_fails_strict_on_unverifiable():
    a = _make_auditor(strict=True)
    _feed_passing(a)
    passed, locus = a._flag_audit_passed()
    assert passed is False
    assert "unverifiable" in locus
    assert "JARVIS_TOTALLY_UNOBSERVABLE_FLAG" in locus


def test_flag_audit_fails_on_rejection():
    # An iron_gate rejection marker -> REJECTED -> FAIL even in lenient mode.
    a = _make_auditor(strict=False)
    _feed_passing(a)
    a.ingest_log_line("ExplorationInsufficientError op=op-abc")
    passed, locus = a._flag_audit_passed()
    assert passed is False
    assert "rejected" in locus
    iron = a.flags["JARVIS_EXPLORATION_LEDGER_LOAD_ADAPTED_FLOORS"]
    assert iron.verdict() == aud.FlagVerdict.REJECTED


def test_unverifiable_is_real_no_fake_pass():
    a = _make_auditor(strict=True)
    # Only feed a flag with no observable signal.
    st = a.flags["JARVIS_TOTALLY_UNOBSERVABLE_FLAG"]
    assert st.family is None
    assert st.verdict() == aud.FlagVerdict.UNVERIFIABLE


def test_observed_flag_marks_evaluated():
    a = _make_auditor()
    a.ingest_log_line("[SemanticGuard] op=op-1 findings=0")
    sg = a.flags["JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS"]
    assert sg.observed_evaluated is True
    assert sg.verdict() == aud.FlagVerdict.OBSERVED_EVALUATED


# ===========================================================================
# 4. Absolute Intervention-Lock
# ===========================================================================


def test_intervention_lock_throws_on_plan_pending():
    a = _make_auditor()
    with pytest.raises(aud.GraduationFailedException) as ei:
        a.ingest_event("plan_pending", {"op_id": "op-abc"})
    assert ei.value.event_type == "plan_pending"
    assert "intervention_lock" in ei.value.failure_locus


def test_intervention_lock_throws_on_ask_human_log_line():
    a = _make_auditor()
    a.ingest_event("fsm_phase_changed", {"phase": "GENERATE", "op_id": "op-x"})
    with pytest.raises(aud.GraduationFailedException) as ei:
        a.ingest_log_line("[Venom] ask_human: which file should I edit?")
    assert ei.value.fsm_phase == "GENERATE"


def test_intervention_lock_throws_on_clarification_and_approval_required():
    for marker, et in (("CLARIFICATION_REQUEST", ""), ("", "plan_pending")):
        a = _make_auditor()
        with pytest.raises(aud.GraduationFailedException):
            if et:
                a.ingest_event(et, {})
            else:
                a.ingest_log_line(marker)
    a2 = _make_auditor()
    with pytest.raises(aud.GraduationFailedException):
        a2.ingest_log_line("risk_tier=APPROVAL_REQUIRED op=op-1")


def test_intervention_lock_allows_terminal_critical_elevation_merge():
    a = _make_auditor()
    # The terminal Sovereign-Law merge gate is the ONLY permitted human gate.
    a.ingest_event("cross_repo_elevation_pending", {"pr_id": "PR-1", "target_repo": "jarvis-prime"})
    assert a.terminal_merge_reached is True
    assert a.intervention_tripped is False
    # And a CRITICAL_ELEVATION marker likewise permitted.
    a2 = _make_auditor()
    a2.ingest_log_line("[critical_elevation] CRITICAL_ELEVATION merge approval pending PR-2")
    assert a2.terminal_merge_reached is True
    assert a2.intervention_tripped is False


def test_intervention_lock_post_merge_prompt_does_not_trip():
    a = _make_auditor()
    a.ingest_event("cross_repo_elevation_pending", {"pr_id": "PR-1"})  # terminal merge reached
    # A human prompt AFTER the merge gate is out of the autonomy window.
    a.ingest_event("plan_pending", {"op_id": "later"})  # should NOT raise
    assert a.intervention_tripped is False


# ===========================================================================
# 5. Full A1_DISPATCH_PROVEN requires all 5 criteria
# ===========================================================================


def test_full_proven_verdict_lenient():
    a = _make_auditor(strict=False)
    _feed_passing(a)
    v = a.verdict()
    assert v.proven is True
    assert all(v.criteria.values())
    assert v.failure_locus == ""


def test_not_proven_when_pr_signal_missing():
    a = _make_auditor(strict=False)
    _feed_passing(a, include_pr=False)
    v = a.verdict()
    assert v.proven is False
    assert v.criteria["autonomous_pr_observed"] is False
    assert "pr" in v.failure_locus


def test_not_proven_when_fsm_not_applied():
    a = _make_auditor(strict=False)
    # Feed everything EXCEPT the terminal applied + apply phase.
    for hop in aud.A1TRACE_HOPS:
        a.ingest_log_line(f"[A1Trace] {hop} goal=G1")
    a.ingest_event("fsm_phase_changed", {"phase": "CLASSIFY"})
    a.ingest_log_line("[SemanticGuard] findings=0")
    a.ingest_event("tool_exploration_start", {})
    a.ingest_event("decision_recorded", {})
    a.ingest_event("review_branch_created", {})
    v = a.verdict()
    assert v.proven is False
    assert v.criteria["fsm_classify_to_applied"] is False
    assert "fsm" in v.failure_locus


def test_not_proven_when_trace_out_of_order():
    a = _make_auditor(strict=False)
    for hop in ("emit", "ingest", "submit", "dequeue", "accept"):
        a.ingest_log_line(f"[A1Trace] {hop} goal=G1")
    a.ingest_event("fsm_phase_changed", {"phase": "CLASSIFY"})
    a.ingest_event("fsm_phase_changed", {"phase": "APPLY"})
    a.ingest_log_line("[SemanticGuard] findings=0")
    a.ingest_event("tool_exploration_start", {})
    a.ingest_event("decision_recorded", {})
    a.ingest_event("operation_terminal", {"state": "applied"})
    a.ingest_event("review_branch_created", {})
    v = a.verdict()
    assert v.proven is False
    assert v.criteria["a1trace_5_hops_in_order"] is False
    assert "a1trace" in v.failure_locus


def test_strict_unverifiable_blocks_proven():
    a = _make_auditor(strict=True)
    _feed_passing(a)
    v = a.verdict()
    assert v.proven is False  # the unobservable flag fails strict
    assert v.criteria["twelve_flag_audit_passed"] is False


# ===========================================================================
# 6. Verdict JSON round-trips + names the failure locus
# ===========================================================================


def test_verdict_json_round_trips():
    a = _make_auditor(strict=False)
    _feed_passing(a)
    v = a.verdict()
    blob = v.to_json()
    parsed = json.loads(blob)
    assert parsed["verdict"] == "proven"
    assert parsed["proven"] is True
    assert isinstance(parsed["flags"], list)
    assert "a1trace_timeline" in parsed
    assert parsed["criteria"]["fsm_classify_to_applied"] is True


def test_verdict_names_failure_locus_on_failure():
    a = _make_auditor(strict=False)
    _feed_passing(a, include_pr=False)
    v = a.verdict()
    parsed = json.loads(v.to_json())
    assert parsed["proven"] is False
    assert parsed["failure_locus"]  # non-empty, names the locus


def test_intervention_exception_recorded_in_verdict():
    a = _make_auditor()
    try:
        a.ingest_event("plan_pending", {"op_id": "op-abc"})
    except aud.GraduationFailedException:
        pass
    v = a.verdict()
    assert v.proven is False
    assert v.criteria["intervention_lock_clean"] is False
    assert v.graduation_exception is not None
    assert "intervention_lock" in v.failure_locus


# ===========================================================================
# 7. Flag set derives from CADENCE_POLICY (not hardcoded)
# ===========================================================================


def test_load_audit_flags_from_cadence_policy():
    os.environ.pop("JARVIS_A1_AUDIT_FLAGS", None)
    flags = aud.load_audit_flags()
    # Must contain known CADENCE_POLICY entries.
    assert "JARVIS_SEMANTIC_GUARDIAN_LOAD_ADAPTED_PATTERNS" in flags
    assert len(flags) >= 12


def test_load_audit_flags_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_AUDIT_FLAGS", "FLAG_A, FLAG_B ,FLAG_C")
    flags = aud.load_audit_flags()
    assert flags == ["FLAG_A", "FLAG_B", "FLAG_C"]


def test_family_for_flag_binds_known_families():
    assert aud.family_for_flag("JARVIS_SEMANTIC_GUARDIAN_X") == "semantic_guardian"
    assert aud.family_for_flag("JARVIS_RISK_TIER_FLOOR_Y") == "risk_tier"
    assert aud.family_for_flag("JARVIS_NONSENSE_FLAG") is None


# ===========================================================================
# 8. Async sources — fake injection (no network)
# ===========================================================================


def test_log_tail_source_reads_growing_file(tmp_path):
    import asyncio

    log_path = tmp_path / "soak.log"
    log_path.write_text("")
    a = _make_auditor(strict=False)
    seen = []

    def _on_line(line):
        seen.append(line)
        a.ingest_log_line(line)

    async def _driver():
        stop = asyncio.Event()
        task = asyncio.ensure_future(
            aud.log_tail_source(str(log_path), on_line=_on_line, stop=stop, poll_interval=0.01, log=lambda *_: None)
        )
        # Write A1Trace hops after the tail starts.
        await asyncio.sleep(0.05)
        with open(log_path, "a", encoding="utf-8") as fh:
            for hop in aud.A1TRACE_HOPS:
                fh.write(f"[A1Trace] {hop} goal=G9\n")
            fh.flush()
        await asyncio.sleep(0.1)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_driver())
    assert a.trace.all_hops_in_order() is True


def test_run_watch_proven_via_injected_log(tmp_path):
    import asyncio

    log_path = tmp_path / "soak.log"
    log_path.write_text("")
    a = _make_auditor(strict=False)

    async def _driver():
        async def _writer():
            await asyncio.sleep(0.05)
            with open(log_path, "a", encoding="utf-8") as fh:
                for hop in aud.A1TRACE_HOPS:
                    fh.write(f"[A1Trace] {hop} goal=G1\n")
                # FSM + gates + terminal + PR all via log markers the auditor
                # also scans (operation_terminal/state surfaces via SSE in
                # prod; here we drive the pure ingest directly for the FSM
                # part, then let the tail finish the trace).
                fh.flush()
            # Drive the SSE-only criteria directly (no live server in test).
            a.ingest_event("fsm_phase_changed", {"phase": "CLASSIFY"})
            a.ingest_event("fsm_phase_changed", {"phase": "APPLY"})
            a.ingest_log_line("[SemanticGuard] findings=0")
            a.ingest_event("tool_exploration_start", {})
            a.ingest_event("decision_recorded", {})
            a.ingest_event("operation_terminal", {"state": "applied"})
            a.ingest_event("review_branch_created", {})

        writer = asyncio.ensure_future(_writer())
        verdict = await aud.run_watch(
            a, base=None, log_file=str(log_path), timeout_s=3.0, log=lambda *_: None
        )
        await asyncio.gather(writer, return_exceptions=True)
        return verdict

    verdict = asyncio.run(_driver())
    assert verdict.proven is True


def test_run_watch_intervention_trips_and_fails(tmp_path):
    import asyncio

    log_path = tmp_path / "soak.log"
    log_path.write_text("")
    a = _make_auditor(strict=False)

    async def _driver():
        async def _writer():
            await asyncio.sleep(0.05)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("[A1Trace] emit goal=G1\n")
                fh.write("[Venom] ask_human: clarify the target file?\n")
                fh.flush()

        writer = asyncio.ensure_future(_writer())
        verdict = await aud.run_watch(
            a, base=None, log_file=str(log_path), timeout_s=3.0, log=lambda *_: None
        )
        await asyncio.gather(writer, return_exceptions=True)
        return verdict

    verdict = asyncio.run(_driver())
    assert verdict.proven is False
    assert verdict.criteria["intervention_lock_clean"] is False
    assert "intervention_lock" in verdict.failure_locus
