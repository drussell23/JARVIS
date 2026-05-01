"""Priority #3 Slice 5b — operator-polish regression suite.

Covers the 3 deliverables shipped post-graduation:
  * /replay REPL dispatcher (replay_repl.py)
  * 4 GET routes mounted on IDEObservabilityRouter
  * Orchestrator hook (replay_orchestrator_hook.py)

Test classes:
  * TestReplyREPLDispatch — closed-taxonomy subcommand matrix
  * TestReplayREPLDefensive — public surface NEVER raises
  * TestHookEnabledFlag — sub-flag asymmetric env semantics
  * TestHookConcurrencyKnob — env clamping
  * TestHookOutcomeSchema + TestHookResultSchemas — frozen dataclasses
  * TestDefaultReplayPolicies — 5 closed-taxonomy targets
  * TestRecordSessionReplay — full bundle matrix (OK / PARTIAL /
    DISABLED / REJECTED / FAILED)
  * TestIDERoutesRegistered — 4 GET routes attached to router
  * TestSlice5bDefensiveContract — public surfaces NEVER raise
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
)
from backend.core.ouroboros.governance.verification.replay_repl import (
    ReplayDispatchResult,
    dispatch_replay_command,
)
from backend.core.ouroboros.governance.verification.replay_orchestrator_hook import (
    HookBundleResult,
    HookOutcome,
    HookTargetResult,
    default_replay_policies,
    record_session_replay,
    replay_hook_concurrency,
    replay_hook_enabled,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
    record_replay_verdict,
    reset_for_tests,
)
from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_verdict(
    branch_verdict: BranchVerdict = BranchVerdict.DIVERGED_BETTER,
    outcome: ReplayOutcome = ReplayOutcome.SUCCESS,
    sid: str = "bt-5b",
) -> ReplayVerdict:
    target = ReplayTarget(
        session_id=sid, swap_at_phase="GATE",
        swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
    )
    orig = BranchSnapshot(
        branch_id="orig", terminal_phase="COMPLETE",
        terminal_success=True, apply_outcome="single",
        verify_passed=10, verify_total=10,
    )
    cf = BranchSnapshot(
        branch_id="cf", terminal_phase="GATE",
        terminal_success=False, apply_outcome="gated",
    )
    return ReplayVerdict(
        outcome=outcome, target=target,
        original_branch=orig, counterfactual_branch=cf,
        verdict=branch_verdict,
    )


def _make_record_dict(
    *,
    record_id: str, op_id: str, phase: str, kind: str,
    ordinal: int, output: dict, parents: tuple = (),
    wall_ts: float = 1000.0, session_id: str,
) -> dict:
    row = {
        "record_id": record_id,
        "session_id": session_id,
        "op_id": op_id, "phase": phase, "kind": kind,
        "ordinal": ordinal, "inputs_hash": f"h_{record_id}",
        "output_repr": json.dumps(output, sort_keys=True),
        "monotonic_ts": 10.0, "wall_ts": wall_ts,
        "schema_version": "decision_record.1",
    }
    if parents:
        row["parent_record_ids"] = list(parents)
    return row


@pytest.fixture
def synthetic_session(tmp_path, monkeypatch):
    sid = "bt-5b-fix"
    ledger_dir = tmp_path / "ledgers" / sid
    ledger_path = ledger_dir / "decisions.jsonl"
    summary_path = tmp_path / "sessions" / sid / "summary.json"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    records = [
        _make_record_dict(
            record_id="r1", op_id="op-1", phase="ROUTE",
            kind="route_assignment", ordinal=0,
            output={"route": "STANDARD"},
            wall_ts=1000.0, session_id=sid,
        ),
        _make_record_dict(
            record_id="r2", op_id="op-1", phase="GATE",
            kind="gate_decision", ordinal=0,
            output={"verdict": "auto_apply"},
            parents=("r1",), wall_ts=1001.0, session_id=sid,
        ),
        _make_record_dict(
            record_id="r3", op_id="op-1", phase="APPLY",
            kind="apply_outcome", ordinal=0,
            output={"applied": True},
            parents=("r2",), wall_ts=1002.0, session_id=sid,
        ),
    ]
    with ledger_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")

    summary_path.write_text(json.dumps({
        "session_id": sid, "stop_reason": "complete",
        "duration_s": 100.0,
        "stats": {"attempted": 1, "completed": 1, "failed": 0,
                  "cancelled": 0, "queued": 0},
        "cost_total": 0.05,
        "cost_breakdown": {"claude": 0.05},
        "branch_stats": {"commits": 1, "files_changed": 1,
                         "insertions": 10, "deletions": 2},
        "convergence_state": "complete",
        "ops_digest": {
            "last_apply_mode": "single", "last_apply_files": 1,
            "last_apply_op_id": "op-1",
            "last_verify_tests_passed": 10,
            "last_verify_tests_total": 10,
            "last_commit_hash": "abc",
        },
    }))

    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "ledgers"),
    )
    monkeypatch.setenv(
        "JARVIS_REPLAY_SUMMARY_ROOT", str(tmp_path / "sessions"),
    )
    return sid, ledger_path, tmp_path / "sessions"


@pytest.fixture(autouse=True)
def _isolated_5b(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_REPLAY_HISTORY_DIR", str(tmp_path / "history"),
    )
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_LOW_N", "1")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_MEDIUM_N", "3")
    monkeypatch.setenv("JARVIS_REPLAY_BASELINE_HIGH_N", "10")
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# TestReplyREPLDispatch
# ---------------------------------------------------------------------------


class TestReplyREPLDispatch:

    def test_no_match_returns_unmatched(self):
        r = dispatch_replay_command("/posture status")
        assert r.matched is False
        assert r.text == ""

    def test_help_subcommand(self):
        r = dispatch_replay_command("/replay help")
        assert r.ok is True
        assert "/replay" in r.text
        assert "subcommand" not in r.text.lower() or "/replay" in r.text

    def test_help_short_form(self):
        r = dispatch_replay_command("/replay ?")
        assert r.ok is True

    def test_master_off_friendly_error(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        r = dispatch_replay_command("/replay status")
        assert r.ok is False
        assert "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED" in r.text

    def test_status_default_when_no_subcommand(self):
        r = dispatch_replay_command("/replay")
        assert r.ok is True
        assert "/replay status" in r.text

    def test_status_empty_store(self):
        r = dispatch_replay_command("/replay status")
        assert r.ok is True
        assert "total recorded: 0" in r.text

    def test_status_with_records(self):
        for _ in range(3):
            record_replay_verdict(_make_verdict())
        r = dispatch_replay_command("/replay status")
        assert r.ok is True
        assert "total recorded: 3" in r.text
        assert "established" in r.text
        assert "100.00%" in r.text

    def test_history_default_limit(self):
        for _ in range(3):
            record_replay_verdict(_make_verdict())
        r = dispatch_replay_command("/replay history")
        assert r.ok is True
        assert "history (last 3)" in r.text

    def test_history_explicit_limit(self):
        for _ in range(5):
            record_replay_verdict(_make_verdict())
        r = dispatch_replay_command("/replay history 2")
        assert r.ok is True
        assert "history (last 2)" in r.text

    def test_history_invalid_limit(self):
        r = dispatch_replay_command("/replay history abc")
        assert r.ok is False
        assert "invalid N" in r.text

    def test_history_limit_clamped(self):
        # Limit >200 → clamped to 200 internally; dispatch still ok
        r = dispatch_replay_command("/replay history 9999")
        assert r.ok is True

    def test_baseline(self):
        for _ in range(3):
            record_replay_verdict(_make_verdict())
        r = dispatch_replay_command("/replay baseline")
        assert r.ok is True
        assert "outcome:" in r.text
        assert "tightening: passed" in r.text

    def test_run_no_args(self):
        r = dispatch_replay_command("/replay run")
        assert r.ok is False
        assert "usage" in r.text.lower()

    def test_run_unknown_kind(self):
        r = dispatch_replay_command("/replay run sid PHASE bogus_kind")
        assert r.ok is False
        assert "unknown swap_kind" in r.text

    def test_run_unknown_subcommand(self):
        r = dispatch_replay_command("/replay made-up")
        assert r.ok is False
        assert "unknown subcommand" in r.text

    def test_parse_error_friendly(self):
        # Unbalanced quote → shlex.split raises
        r = dispatch_replay_command('/replay status "unbalanced')
        assert r.ok is False
        assert "parse error" in r.text


# ---------------------------------------------------------------------------
# TestReplayREPLDefensive
# ---------------------------------------------------------------------------


class TestReplayREPLDefensive:

    def test_empty_line(self):
        r = dispatch_replay_command("")
        assert r.matched is False

    def test_returns_dispatch_result(self):
        r = dispatch_replay_command("/replay status")
        assert isinstance(r, ReplayDispatchResult)

    def test_garbage_after_command(self):
        # Should not raise even with bizarre args
        r = dispatch_replay_command("/replay history -1")
        # -1 → max(1, min(200, -1)) = 1, so this becomes valid
        assert isinstance(r, ReplayDispatchResult)


# ---------------------------------------------------------------------------
# TestHookEnabledFlag
# ---------------------------------------------------------------------------


class TestHookEnabledFlag:

    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_HOOK_ENABLED", raising=False)
        assert replay_hook_enabled() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_ENABLED", "")
        assert replay_hook_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_ENABLED", v)
        assert replay_hook_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_ENABLED", v)
        assert replay_hook_enabled() is False


# ---------------------------------------------------------------------------
# TestHookConcurrencyKnob
# ---------------------------------------------------------------------------


class TestHookConcurrencyKnob:

    def test_default_one(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_HOOK_CONCURRENCY", raising=False)
        assert replay_hook_concurrency() == 1

    def test_floor_one(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_CONCURRENCY", "0")
        assert replay_hook_concurrency() == 1

    def test_ceiling_eight(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_CONCURRENCY", "999")
        assert replay_hook_concurrency() == 8

    def test_garbage_returns_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_CONCURRENCY", "junk")
        assert replay_hook_concurrency() == 1


# ---------------------------------------------------------------------------
# TestHookResultSchemas
# ---------------------------------------------------------------------------


class TestHookOutcomeSchema:

    def test_5_value_taxonomy(self):
        assert {x.value for x in HookOutcome} == {
            "ok", "partial", "disabled", "rejected", "failed",
        }


class TestHookResultSchemas:

    def test_target_result_to_dict(self):
        verdict = _make_verdict()
        target = verdict.target
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            RecordOutcome,
        )
        tr = HookTargetResult(
            target=target, verdict=verdict,
            record_outcome=RecordOutcome.OK,
        )
        d = tr.to_dict()
        assert "target" in d
        assert "verdict" in d
        assert d["record_outcome"] == "ok"

    def test_target_result_is_actionable(self):
        verdict = _make_verdict()
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            RecordOutcome,
        )
        tr_ok = HookTargetResult(
            target=verdict.target, verdict=verdict,
            record_outcome=RecordOutcome.OK,
        )
        assert tr_ok.is_actionable() is True

        tr_failed = HookTargetResult(
            target=verdict.target, verdict=None,
            record_outcome=None,
            error_detail="engine_raise",
        )
        assert tr_failed.is_actionable() is False

        # Verdict outcome != SUCCESS → not actionable
        partial_v = ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL,
            target=verdict.target,
            verdict=BranchVerdict.FAILED,
        )
        tr_partial = HookTargetResult(
            target=verdict.target, verdict=partial_v,
            record_outcome=RecordOutcome.OK,
        )
        assert tr_partial.is_actionable() is False

    def test_bundle_result_counts(self):
        verdict = _make_verdict()
        from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
            RecordOutcome,
        )
        actionable = HookTargetResult(
            target=verdict.target, verdict=verdict,
            record_outcome=RecordOutcome.OK,
        )
        non = HookTargetResult(
            target=verdict.target, verdict=None,
            record_outcome=None, error_detail="x",
        )
        bundle = HookBundleResult(
            outcome=HookOutcome.PARTIAL,
            session_id="x",
            target_results=(actionable, non),
        )
        assert bundle.actionable_count == 1
        assert bundle.prevention_evidence_count == 1


# ---------------------------------------------------------------------------
# TestDefaultReplayPolicies
# ---------------------------------------------------------------------------


class TestDefaultReplayPolicies:

    def test_one_per_decision_override_kind(self):
        policies = default_replay_policies("bt-test")
        kinds = {p.swap_decision_kind for p in policies}
        assert kinds == set(DecisionOverrideKind)

    def test_empty_session_id_returns_empty(self):
        assert default_replay_policies("") == ()
        assert default_replay_policies("   ") == ()

    def test_session_id_propagated(self):
        policies = default_replay_policies("session-X")
        for p in policies:
            assert p.session_id == "session-X"


# ---------------------------------------------------------------------------
# TestRecordSessionReplay
# ---------------------------------------------------------------------------


class TestRecordSessionReplay:

    def test_empty_session_id_rejected(self):
        bundle = asyncio.run(record_session_replay(""))
        assert bundle.outcome is HookOutcome.REJECTED

    def test_master_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        bundle = asyncio.run(record_session_replay("any-session"))
        assert bundle.outcome is HookOutcome.DISABLED

    def test_hook_sub_off_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_HOOK_ENABLED", "false")
        bundle = asyncio.run(record_session_replay("any-session"))
        assert bundle.outcome is HookOutcome.DISABLED

    def test_enabled_override_false(self):
        bundle = asyncio.run(record_session_replay(
            "any-session", enabled_override=False,
        ))
        assert bundle.outcome is HookOutcome.DISABLED

    def test_empty_policies_rejected(self):
        bundle = asyncio.run(record_session_replay(
            "any-session", policy_overrides=[],
        ))
        assert bundle.outcome is HookOutcome.REJECTED

    def test_full_bundle_synthetic_session(self, synthetic_session):
        sid, _, _ = synthetic_session
        bundle = asyncio.run(record_session_replay(sid))
        # Synthetic session has GATE; other targets miss → PARTIAL
        assert bundle.outcome in (HookOutcome.OK, HookOutcome.PARTIAL)
        assert bundle.actionable_count >= 1
        assert bundle.prevention_evidence_count >= 1

    def test_baseline_refreshed(self, synthetic_session):
        sid, _, _ = synthetic_session
        bundle = asyncio.run(record_session_replay(sid))
        assert bundle.baseline_report is not None

    def test_to_dict_full_shape(self, synthetic_session):
        sid, _, _ = synthetic_session
        bundle = asyncio.run(record_session_replay(sid))
        d = bundle.to_dict()
        assert "outcome" in d
        assert "session_id" in d
        assert "target_results" in d
        assert "baseline_report" in d
        assert "actionable_count" in d
        assert "prevention_evidence_count" in d
        assert d["schema_version"] == "replay_orchestrator_hook.1"

    def test_custom_policy_list(self, synthetic_session):
        sid, _, _ = synthetic_session
        # Just one target — GATE which matches the synthetic ledger
        custom = [
            ReplayTarget(
                session_id=sid, swap_at_phase="GATE",
                swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
                swap_decision_payload={"verdict": "approval_required"},
            ),
        ]
        bundle = asyncio.run(record_session_replay(
            sid, policy_overrides=custom,
        ))
        assert bundle.outcome is HookOutcome.OK
        assert len(bundle.target_results) == 1
        assert bundle.actionable_count == 1


# ---------------------------------------------------------------------------
# TestIDERoutesRegistered
# ---------------------------------------------------------------------------


class TestIDERoutesRegistered:

    def test_handlers_attached_to_router(self):
        r = IDEObservabilityRouter()
        for name in (
            "_handle_replay_health",
            "_handle_replay_baseline",
            "_handle_replay_verdicts",
            "_handle_replay_history",
        ):
            assert hasattr(r, name)

    def test_register_routes_includes_replay(self):
        """register_routes wires the 4 replay paths."""
        registered_paths = []

        class FakeApp:
            def __init__(self):
                self.router = self

            def add_get(self, path, handler):
                registered_paths.append(path)

        IDEObservabilityRouter().register_routes(FakeApp())
        for required in (
            "/observability/replay/health",
            "/observability/replay/baseline",
            "/observability/replay/verdicts",
            "/observability/replay/history",
        ):
            assert required in registered_paths


# ---------------------------------------------------------------------------
# TestSlice5bDefensiveContract
# ---------------------------------------------------------------------------


class TestSlice5bDefensiveContract:

    def test_dispatcher_never_raises(self):
        # Pile of garbage inputs
        for inp in (
            "/replay status",
            "/replay history bogus",
            "/replay run",
            '/replay run "incomplete',
            "/replay made-up sub command",
        ):
            r = dispatch_replay_command(inp)
            assert isinstance(r, ReplayDispatchResult)

    def test_record_session_replay_with_garbage_targets(self):
        # Non-target objects in the policy_overrides — they get
        # filtered out, leaving an empty bundle → REJECTED
        bundle = asyncio.run(record_session_replay(
            "session-x",
            policy_overrides=["not a target", 42, None],  # type: ignore
        ))
        assert bundle.outcome is HookOutcome.REJECTED
