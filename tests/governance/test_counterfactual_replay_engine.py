"""Priority #3 Slice 2 — Counterfactual Replay engine regression suite.

End-to-end tests over the engine that loads recorded ledgers + summary,
projects original/counterfactual branches, detects downstream
divergence via the Causality DAG, and produces ReplayVerdicts.

Test classes:
  * TestEngineEnabledFlag — sub-flag asymmetric env semantics
  * TestSummaryRoot — env-knob path resolution
  * TestSwapPointSchema — frozen value object
  * TestDivergenceInfoSchema — frozen value object
  * TestInferenceRegistry — dynamic registration / replacement / reset
  * TestDefaultInferences — 5 closed-taxonomy values produce expected
    terminal states
  * TestLoadArtifacts — sync loader degrades on missing files
  * TestProjectOriginal — pure projection from DAG + summary
  * TestLocateSwap — first-by-chronological-order match
  * TestDetectDivergence — DAG BFS over reverse edges
  * TestProjectCounterfactual — registry-driven inference
  * TestRunCounterfactualReplayMatrix — full async surface
  * TestEngineDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Tuple

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
)
from backend.core.ouroboros.governance.verification import (
    counterfactual_replay_engine as engine_mod,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay_engine import (
    DivergenceInfo,
    SwapPoint,
    _LoadedArtifacts,
    _detect_divergence,
    _gate_decision_inference,
    _load_session_artifacts,
    _locate_swap,
    _passthrough_inference,
    _project_counterfactual_branch,
    _project_original_branch,
    get_inference,
    register_inference,
    replay_engine_enabled,
    replay_summary_root,
    reset_registry_for_tests,
    run_counterfactual_replay,
)
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens — same construction pattern as Slice 1's test file.
# Concatenation prevents the literal substring from appearing in the
# source bytes, so the AST validator can scan THIS file's source for the
# forbidden patterns AS substrings without false-positiving on its own
# detection logic. Mirrors test_counterfactual_replay.py:
# ``_FORBIDDEN_CALL_TOKENS = ("e" + "val(", "e" + "xec(")``.
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Test fixtures — synthetic ledger + summary writers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    record_id: str,
    op_id: str,
    phase: str,
    kind: str,
    ordinal: int,
    output: Any,
    parents: Tuple[str, ...] = (),
    wall_ts: float = 1000.0,
    monotonic_ts: float = 10.0,
    session_id: str = "bt-test",
) -> dict:
    """Construct a JSONL-shaped DecisionRecord row."""
    row = {
        "record_id": record_id,
        "session_id": session_id,
        "op_id": op_id,
        "phase": phase,
        "kind": kind,
        "ordinal": ordinal,
        "inputs_hash": f"hash_{record_id}",
        "output_repr": json.dumps(output, sort_keys=True),
        "monotonic_ts": monotonic_ts,
        "wall_ts": wall_ts,
        "schema_version": "decision_record.1",
    }
    if parents:
        row["parent_record_ids"] = list(parents)
    return row


def _write_ledger(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def _write_summary(
    path: Path,
    *,
    session_id: str = "bt-test",
    stop_reason: str = "complete",
    duration_s: float = 100.0,
    attempted: int = 1,
    completed: int = 1,
    failed: int = 0,
    cost_total: float = 0.05,
    convergence_state: str = "complete",
    last_apply_mode: str = "single",
    last_apply_files: int = 1,
    last_verify_passed: int = 10,
    last_verify_total: int = 10,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "stop_reason": stop_reason,
        "duration_s": duration_s,
        "stats": {
            "attempted": attempted, "completed": completed,
            "failed": failed, "cancelled": 0, "queued": 0,
        },
        "cost_total": cost_total,
        "cost_breakdown": {"claude": cost_total},
        "branch_stats": {
            "commits": 1, "files_changed": 1,
            "insertions": 10, "deletions": 2,
        },
        "convergence_state": convergence_state,
        "ops_digest": {
            "last_apply_mode": last_apply_mode,
            "last_apply_files": last_apply_files,
            "last_apply_op_id": "op-1",
            "last_verify_tests_passed": last_verify_passed,
            "last_verify_tests_total": last_verify_total,
            "last_commit_hash": "abc123def456",
        },
    }
    path.write_text(json.dumps(payload))


@pytest.fixture
def fixture_session(tmp_path):
    """Yield (session_id, ledger_path, summary_root, summary_path) +
    writes a 4-record ROUTE → GATE → APPLY → VERIFY chain plus a
    successful summary."""
    sid = "bt-fixture"
    ledger_dir = tmp_path / "ledgers" / sid
    ledger_path = ledger_dir / "decisions.jsonl"
    summary_root = tmp_path / "sessions"
    summary_path = summary_root / sid / "summary.json"

    records = [
        _make_record(
            record_id="r1", op_id="op-1", phase="ROUTE",
            kind="route_assignment", ordinal=0,
            output={"route": "STANDARD"},
            wall_ts=1000.0, session_id=sid,
        ),
        _make_record(
            record_id="r2", op_id="op-1", phase="GATE",
            kind="gate_decision", ordinal=0,
            output={"verdict": "auto_apply"},
            parents=("r1",), wall_ts=1001.0, session_id=sid,
        ),
        _make_record(
            record_id="r3", op_id="op-1", phase="APPLY",
            kind="apply_outcome", ordinal=0,
            output={"applied": True},
            parents=("r2",), wall_ts=1002.0, session_id=sid,
        ),
        _make_record(
            record_id="r4", op_id="op-1", phase="VERIFY",
            kind="test_run", ordinal=0,
            output={"passed": 10, "total": 10},
            parents=("r3",), wall_ts=1003.0, session_id=sid,
        ),
    ]
    _write_ledger(ledger_path, records)
    _write_summary(summary_path, session_id=sid)

    return sid, ledger_path, summary_root, summary_path


@pytest.fixture(autouse=True)
def _reset_engine_state():
    """Clean slate before each test: reset inference registry +
    re-register the 5-value defaults."""
    reset_registry_for_tests()
    register_inference(
        kind=DecisionOverrideKind.GATE_DECISION,
        fn=_gate_decision_inference,
    )
    for k in (
        DecisionOverrideKind.POSTMORTEM_INJECTION,
        DecisionOverrideKind.RECURRENCE_BOOST,
        DecisionOverrideKind.QUORUM_INVOCATION,
        DecisionOverrideKind.COHERENCE_OBSERVER,
    ):
        register_inference(kind=k, fn=_passthrough_inference)
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# TestEngineEnabledFlag — sub-flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestEngineEnabledFlag:

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_ENGINE_ENABLED", raising=False)
        assert replay_engine_enabled() is False

    def test_empty_string_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "")
        assert replay_engine_enabled() is False

    def test_whitespace_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "   ")
        assert replay_engine_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", val)
        assert replay_engine_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off"])
    def test_falsy_variants(self, monkeypatch, val):
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", val)
        assert replay_engine_enabled() is False


# ---------------------------------------------------------------------------
# TestSummaryRoot
# ---------------------------------------------------------------------------


class TestSummaryRoot:

    def test_default_is_ouroboros_sessions(self, monkeypatch):
        monkeypatch.delenv("JARVIS_REPLAY_SUMMARY_ROOT", raising=False)
        assert replay_summary_root() == Path(".ouroboros/sessions")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_SUMMARY_ROOT", "/tmp/custom_root")
        assert replay_summary_root() == Path("/tmp/custom_root")

    def test_empty_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_SUMMARY_ROOT", "   ")
        assert replay_summary_root() == Path(".ouroboros/sessions")


# ---------------------------------------------------------------------------
# TestSwapPointSchema + TestDivergenceInfoSchema
# ---------------------------------------------------------------------------


class TestSwapPointSchema:

    def test_construction(self):
        sp = SwapPoint(
            record_id="r1", phase="GATE", kind="gate_decision",
            ordinal=0, wall_ts=1000.0, output_repr='{"v":"a"}',
        )
        assert sp.record_id == "r1"
        assert sp.phase == "GATE"

    def test_frozen(self):
        sp = SwapPoint(
            record_id="r1", phase="GATE", kind="gate_decision",
            ordinal=0, wall_ts=1000.0,
        )
        with pytest.raises(FrozenInstanceError):
            sp.record_id = "different"  # type: ignore

    def test_hashable(self):
        sp1 = SwapPoint(
            record_id="r1", phase="GATE", kind="gate_decision",
            ordinal=0, wall_ts=1000.0,
        )
        sp2 = SwapPoint(
            record_id="r1", phase="GATE", kind="gate_decision",
            ordinal=0, wall_ts=1000.0,
        )
        s = {sp1, sp2}
        assert len(s) == 1


class TestDivergenceInfoSchema:

    def test_default_no_divergence(self):
        d = DivergenceInfo(diverged=False)
        assert d.diverged is False
        assert d.divergence_phase == ""
        assert d.divergence_reason == ""
        assert d.downstream_record_count == 0

    def test_diverged(self):
        d = DivergenceInfo(
            diverged=True, divergence_phase="APPLY",
            divergence_reason="r2_invalidated",
            downstream_record_count=3,
        )
        assert d.diverged is True
        assert d.divergence_phase == "APPLY"
        assert d.downstream_record_count == 3

    def test_frozen(self):
        d = DivergenceInfo(diverged=False)
        with pytest.raises(FrozenInstanceError):
            d.diverged = True  # type: ignore


# ---------------------------------------------------------------------------
# TestInferenceRegistry — dynamic registration
# ---------------------------------------------------------------------------


class TestInferenceRegistry:

    def test_default_inferences_registered(self):
        for kind in DecisionOverrideKind:
            assert get_inference(kind) is not None

    def test_register_replaces_existing(self):
        def custom(*, payload, original, swap):
            return ("X", True, "y")

        register_inference(
            kind=DecisionOverrideKind.QUORUM_INVOCATION, fn=custom,
        )
        assert get_inference(DecisionOverrideKind.QUORUM_INVOCATION) is custom

    def test_register_idempotent_same_fn(self):
        existing = get_inference(DecisionOverrideKind.GATE_DECISION)
        register_inference(
            kind=DecisionOverrideKind.GATE_DECISION, fn=existing,
        )
        assert get_inference(DecisionOverrideKind.GATE_DECISION) is existing

    def test_register_invalid_kind_silent(self):
        register_inference(kind="GATE_DECISION", fn=lambda **_: ("", False, ""))  # type: ignore
        register_inference(kind=42, fn=lambda **_: ("", False, ""))  # type: ignore
        assert get_inference(DecisionOverrideKind.GATE_DECISION) is _gate_decision_inference

    def test_get_invalid_kind_returns_none(self):
        assert get_inference("not a kind") is None  # type: ignore

    def test_reset_clears_all(self):
        reset_registry_for_tests()
        for kind in DecisionOverrideKind:
            assert get_inference(kind) is None


# ---------------------------------------------------------------------------
# TestDefaultInferences — closed-taxonomy values produce expected terminals
# ---------------------------------------------------------------------------


_BASE_ORIG = BranchSnapshot(
    branch_id="orig", terminal_phase="COMPLETE",
    terminal_success=True, apply_outcome="single",
    verify_passed=10, verify_total=10,
)
_BASE_SWAP = SwapPoint(
    record_id="r1", phase="GATE", kind="gate_decision",
    ordinal=0, wall_ts=1000.0,
)


class TestDefaultInferences:

    @pytest.mark.parametrize("verdict", ["auto_apply", "safe_auto", "notify_apply"])
    def test_gate_promote_inherits_terminal(self, verdict):
        phase, success, apply = _gate_decision_inference(
            payload={"verdict": verdict},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert success is True
        assert apply == "single"

    def test_gate_approval_required_halts_with_gated(self):
        phase, success, apply = _gate_decision_inference(
            payload={"verdict": "approval_required"},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert phase == "GATE"
        assert success is False
        assert apply == "gated"

    def test_gate_blocked_halts_with_none(self):
        phase, success, apply = _gate_decision_inference(
            payload={"verdict": "blocked"},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert success is False
        assert apply == "none"

    def test_gate_unknown_verdict_halts_default(self):
        phase, success, apply = _gate_decision_inference(
            payload={"verdict": "made_up_verdict"},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert success is False

    def test_gate_missing_payload_halts_default(self):
        phase, success, apply = _gate_decision_inference(
            payload={}, original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert success is False

    def test_gate_case_insensitive(self):
        phase, success, apply = _gate_decision_inference(
            payload={"verdict": "APPROVAL_REQUIRED"},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert apply == "gated"

    def test_passthrough_inherits_original(self):
        phase, success, apply = _passthrough_inference(
            payload={"records": ["test_failure_X"]},
            original=_BASE_ORIG, swap=_BASE_SWAP,
        )
        assert phase == "COMPLETE"
        assert success is True
        assert apply == "single"


# ---------------------------------------------------------------------------
# TestLoadArtifacts — sync loader
# ---------------------------------------------------------------------------


class TestLoadArtifacts:

    def test_missing_ledger_returns_empty_dag(self, tmp_path):
        loaded = _load_session_artifacts(
            session_id="missing", ledger_path=tmp_path / "no.jsonl",
            summary_root=tmp_path,
        )
        assert isinstance(loaded, _LoadedArtifacts)
        assert loaded.dag.is_empty
        assert loaded.summary is None

    def test_missing_summary_returns_none(self, tmp_path):
        ledger = tmp_path / "decisions.jsonl"
        ledger.write_text("")
        loaded = _load_session_artifacts(
            session_id="missing", ledger_path=ledger,
            summary_root=tmp_path,
        )
        assert loaded.summary is None

    def test_full_load(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        assert loaded.dag.node_count == 4
        assert loaded.summary is not None
        assert loaded.summary.session_id == sid


# ---------------------------------------------------------------------------
# TestProjectOriginal
# ---------------------------------------------------------------------------


class TestProjectOriginal:

    def test_empty_dag_and_no_summary_returns_none(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        result = _project_original_branch(
            dag=CausalityDAG(), summary=None, target=target,
        )
        assert result is None

    def test_dag_only_projection(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        result = _project_original_branch(
            dag=loaded.dag, summary=None, target=target,
        )
        assert result is not None
        assert result.terminal_phase == "VERIFY"
        assert result.terminal_success is False
        assert result.verify_total == 0

    def test_full_projection(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        result = _project_original_branch(
            dag=loaded.dag, summary=loaded.summary, target=target,
        )
        assert result is not None
        assert result.terminal_phase == "VERIFY"
        assert result.terminal_success is True
        assert result.apply_outcome == "single"
        assert result.verify_passed == 10
        assert result.verify_total == 10
        assert result.cost_usd == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# TestLocateSwap
# ---------------------------------------------------------------------------


class TestLocateSwap:

    def test_empty_dag_returns_none(self):
        t = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        assert _locate_swap(dag=CausalityDAG(), target=t) is None

    def test_no_match_returns_none(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        t = ReplayTarget(
            session_id=sid, swap_at_phase="UNKNOWN_PHASE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        assert _locate_swap(dag=loaded.dag, target=t) is None

    def test_match_returns_first_chronological(self, tmp_path, monkeypatch):
        ledger_path = tmp_path / "ledger.jsonl"
        records = [
            _make_record(
                record_id="r1", op_id="op-1", phase="GATE",
                kind="gate_decision", ordinal=1,
                output={"v": "a"}, wall_ts=2000.0,
                session_id="bt-test",
            ),
            _make_record(
                record_id="r2", op_id="op-2", phase="GATE",
                kind="gate_decision", ordinal=0,
                output={"v": "b"}, wall_ts=1000.0,
                session_id="bt-test",
            ),
        ]
        _write_ledger(ledger_path, records)
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id="bt-test", ledger_path=ledger_path,
            summary_root=tmp_path,
        )
        t = ReplayTarget(
            session_id="bt-test", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        sp = _locate_swap(dag=loaded.dag, target=t)
        assert sp is not None
        assert sp.record_id == "r2"


# ---------------------------------------------------------------------------
# TestDetectDivergence
# ---------------------------------------------------------------------------


class TestDetectDivergence:

    def test_empty_dag_no_divergence(self):
        sp = SwapPoint(
            record_id="r1", phase="X", kind="y", ordinal=0,
            wall_ts=0.0,
        )
        d = _detect_divergence(dag=CausalityDAG(), swap=sp)
        assert d.diverged is False

    def test_leaf_swap_no_downstream(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        sp = SwapPoint(
            record_id="r4", phase="VERIFY", kind="test_run",
            ordinal=0, wall_ts=1003.0,
        )
        d = _detect_divergence(dag=loaded.dag, swap=sp)
        assert d.diverged is False
        assert d.downstream_record_count == 0

    def test_internal_swap_has_downstream(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        sp = SwapPoint(
            record_id="r2", phase="GATE", kind="gate_decision",
            ordinal=0, wall_ts=1001.0,
        )
        d = _detect_divergence(dag=loaded.dag, swap=sp)
        assert d.diverged is True
        assert d.downstream_record_count == 2
        assert d.divergence_phase == "APPLY"

    def test_root_swap_has_max_downstream(self, fixture_session, monkeypatch):
        sid, ledger_path, summary_root, _ = fixture_session
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        loaded = _load_session_artifacts(
            session_id=sid, ledger_path=ledger_path,
            summary_root=summary_root,
        )
        sp = SwapPoint(
            record_id="r1", phase="ROUTE", kind="route_assignment",
            ordinal=0, wall_ts=1000.0,
        )
        d = _detect_divergence(dag=loaded.dag, swap=sp)
        assert d.diverged is True
        assert d.downstream_record_count == 3
        assert d.divergence_phase == "GATE"


# ---------------------------------------------------------------------------
# TestProjectCounterfactual
# ---------------------------------------------------------------------------


class TestProjectCounterfactual:

    def test_no_divergence_inherits_original(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "auto_apply"},
        )
        cf = _project_counterfactual_branch(
            original=_BASE_ORIG, swap=_BASE_SWAP,
            divergence=DivergenceInfo(diverged=False),
            target=target,
        )
        assert cf.branch_id == "counterfactual"
        assert cf.terminal_phase == _BASE_ORIG.terminal_phase
        assert cf.terminal_success == _BASE_ORIG.terminal_success
        assert cf.apply_outcome == _BASE_ORIG.apply_outcome

    def test_diverged_gate_approval_truncates(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "approval_required"},
        )
        cf = _project_counterfactual_branch(
            original=_BASE_ORIG, swap=_BASE_SWAP,
            divergence=DivergenceInfo(
                diverged=True, divergence_phase="APPLY",
                divergence_reason="x", downstream_record_count=2,
            ),
            target=target,
        )
        assert cf.branch_id == "counterfactual"
        assert cf.terminal_phase == "GATE"
        assert cf.terminal_success is False
        assert cf.apply_outcome == "gated"
        assert cf.verify_passed == 0
        assert cf.verify_total == 0
        assert cf.postmortem_records == ()

    def test_inference_raises_uses_safe_default(self):
        def bad_inference(*, payload, original, swap):
            raise RuntimeError("bad")

        register_inference(
            kind=DecisionOverrideKind.QUORUM_INVOCATION,
            fn=bad_inference,
        )
        target = ReplayTarget(
            session_id="x", swap_at_phase="QUORUM",
            swap_decision_kind=DecisionOverrideKind.QUORUM_INVOCATION,
        )
        cf = _project_counterfactual_branch(
            original=_BASE_ORIG, swap=_BASE_SWAP,
            divergence=DivergenceInfo(
                diverged=True, divergence_phase="APPLY",
                downstream_record_count=1,
            ),
            target=target,
        )
        assert cf.terminal_phase == _BASE_SWAP.phase
        assert cf.terminal_success is False
        assert cf.apply_outcome == "none"

    def test_inference_returns_garbage_uses_safe_default(self):
        def garbage_inference(*, payload, original, swap):
            return "not a 3-tuple"

        register_inference(
            kind=DecisionOverrideKind.QUORUM_INVOCATION,
            fn=garbage_inference,  # type: ignore
        )
        target = ReplayTarget(
            session_id="x", swap_at_phase="QUORUM",
            swap_decision_kind=DecisionOverrideKind.QUORUM_INVOCATION,
        )
        cf = _project_counterfactual_branch(
            original=_BASE_ORIG, swap=_BASE_SWAP,
            divergence=DivergenceInfo(
                diverged=True, divergence_phase="APPLY",
                downstream_record_count=1,
            ),
            target=target,
        )
        assert cf.terminal_success is False


# ---------------------------------------------------------------------------
# TestRunCounterfactualReplayMatrix
# ---------------------------------------------------------------------------


class TestRunCounterfactualReplayMatrix:

    @pytest.fixture(autouse=True)
    def _engine_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "true")
        monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
        yield

    def test_master_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "false")
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = asyncio.run(run_counterfactual_replay(target))
        assert v.outcome is ReplayOutcome.DISABLED

    def test_sub_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "false")
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = asyncio.run(run_counterfactual_replay(target))
        assert v.outcome is ReplayOutcome.DISABLED

    def test_enabled_override_false_returns_disabled(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = asyncio.run(run_counterfactual_replay(
            target, enabled_override=False,
        ))
        assert v.outcome is ReplayOutcome.DISABLED

    def test_garbage_target_returns_failed(self):
        v = asyncio.run(run_counterfactual_replay(
            "not a target",  # type: ignore
        ))
        assert v.outcome is ReplayOutcome.FAILED

    def test_no_artifacts_returns_partial(self, tmp_path):
        target = ReplayTarget(
            session_id="missing", swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=tmp_path / "no.jsonl",
            summary_root=tmp_path,
        ))
        assert v.outcome is ReplayOutcome.PARTIAL

    def test_swap_not_found_returns_partial(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="UNKNOWN_PHASE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert v.outcome is ReplayOutcome.PARTIAL
        assert v.original_branch is not None
        assert v.counterfactual_branch is None

    def test_leaf_swap_returns_success_equivalent(self, fixture_session):
        """Swap at LEAF record (no downstream) → counterfactual
        inherits original projection → EQUIVALENT verdict.

        Override the fixture to make GATE the leaf (no APPLY/VERIFY
        children) so the swap target matches a leaf node."""
        sid, ledger_path, summary_root, _ = fixture_session
        records = [
            _make_record(
                record_id="r1", op_id="op-1", phase="ROUTE",
                kind="route_assignment", ordinal=0,
                output={"route": "STANDARD"},
                wall_ts=1000.0, session_id=sid,
            ),
            _make_record(
                record_id="r2", op_id="op-1", phase="GATE",
                kind="gate_decision", ordinal=0,
                output={"verdict": "auto_apply"},
                parents=("r1",), wall_ts=1001.0, session_id=sid,
            ),
        ]
        _write_ledger(ledger_path, records)

        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "auto_apply"},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.verdict is BranchVerdict.EQUIVALENT
        assert v.counterfactual_branch is not None
        assert v.counterfactual_branch.branch_id == "counterfactual"

    def test_diverged_gate_approval_required_diverged_better(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "approval_required"},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.verdict is BranchVerdict.DIVERGED_BETTER
        assert v.counterfactual_branch is not None
        assert v.counterfactual_branch.terminal_phase == "GATE"
        assert v.counterfactual_branch.apply_outcome == "gated"
        assert v.is_prevention_evidence() is True

    def test_diverged_gate_blocked_diverged_better(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "blocked"},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.counterfactual_branch is not None
        assert v.counterfactual_branch.apply_outcome == "none"

    def test_postmortem_passthrough_equivalent(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        records = [
            _make_record(
                record_id="r1", op_id="op-1",
                phase="CONTEXT_EXPANSION",
                kind="postmortem_injection", ordinal=0,
                output={"records": []}, wall_ts=1000.0,
                session_id=sid,
            ),
            _make_record(
                record_id="r2", op_id="op-1", phase="GENERATE",
                kind="candidate", ordinal=0,
                output={"hash": "abc"},
                parents=("r1",), wall_ts=1001.0, session_id=sid,
            ),
            _make_record(
                record_id="r3", op_id="op-1", phase="APPLY",
                kind="apply_outcome", ordinal=0,
                output={"applied": True},
                parents=("r2",), wall_ts=1002.0, session_id=sid,
            ),
        ]
        _write_ledger(ledger_path, records)

        target = ReplayTarget(
            session_id=sid, swap_at_phase="CONTEXT_EXPANSION",
            swap_decision_kind=DecisionOverrideKind.POSTMORTEM_INJECTION,
            swap_decision_payload={"records": ["test_failure_X"]},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.counterfactual_branch is not None
        assert v.counterfactual_branch.terminal_success is True

    def test_detail_includes_monotonic_tightening_stamp(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "auto_apply"},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert "monotonic_tightening=passed" in (v.detail or "")

    def test_detail_includes_dag_node_count(self, fixture_session):
        sid, ledger_path, summary_root, _ = fixture_session
        target = ReplayTarget(
            session_id=sid, swap_at_phase="GATE",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={"verdict": "auto_apply"},
        )
        v = asyncio.run(run_counterfactual_replay(
            target, ledger_path=ledger_path, summary_root=summary_root,
        ))
        assert "dag_nodes=4" in (v.detail or "")


# ---------------------------------------------------------------------------
# TestEngineDefensiveContract — public surface NEVER raises
# ---------------------------------------------------------------------------


class TestEngineDefensiveContract:

    def test_run_with_none_target_returns_failed_no_raise(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_REPLAY_ENGINE_ENABLED", "true")
        v = asyncio.run(run_counterfactual_replay(None))  # type: ignore
        assert v.outcome is ReplayOutcome.FAILED

    def test_locate_swap_with_garbage_target_returns_none(self):
        sp = _locate_swap(dag=CausalityDAG(), target=None)  # type: ignore
        assert sp is None

    def test_detect_divergence_with_garbage_swap_no_raise(self):
        sp = SwapPoint(
            record_id="nonexistent", phase="X", kind="y",
            ordinal=0, wall_ts=0.0,
        )
        d = _detect_divergence(dag=CausalityDAG(), swap=sp)
        assert d.diverged is False

    def test_project_counterfactual_with_garbage_target_safe_default(self):
        target = ReplayTarget(
            session_id="x", swap_at_phase="X",
            swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
            swap_decision_payload={},
        )
        cf = _project_counterfactual_branch(
            original=_BASE_ORIG, swap=_BASE_SWAP,
            divergence=DivergenceInfo(
                diverged=True, divergence_phase="X",
                downstream_record_count=1,
            ),
            target=target,
        )
        assert isinstance(cf, BranchSnapshot)


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants — AST-level pin
# ---------------------------------------------------------------------------


_ENGINE_PATH = Path(engine_mod.__file__)


def _module_source() -> str:
    return _ENGINE_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


# Banned imports — would break the cost-contract invariant
_BANNED_IMPORT_SUBSTRINGS = (
    ".providers",
    "doubleword_provider",
    "urgency_router",
    "candidate_generator",
    "orchestrator",
    "tool_executor",
    "phase_runner",
    "iron_gate",
    "change_engine",
    "auto_action_router",
    "subagent_scheduler",
    "semantic_guardian",
    "semantic_firewall",
    "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name, (
                            f"banned import '{alias.name}' "
                            f"contains '{banned}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module, (
                        f"banned ImportFrom module '{module}' "
                        f"contains '{banned}'"
                    )

    def test_no_eval_family_calls(self):
        """Critical safety pin — engine NEVER executes code.
        Mirrors Slice 1's _FORBIDDEN_CALL_TOKENS scan."""
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"forbidden bare call: {node.func.id}"
                    )
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src, (
                f"forbidden syntactic call: {token!r}"
            )

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_no_mutation_tools(self):
        """AST walk: no Call nodes target filesystem-mutation
        functions. Substring search would false-positive on
        docstring mentions of forbidden patterns; the AST view
        catches actual call sites only."""
        tree = _module_ast()
        forbidden_calls = {
            ("shutil", "rmtree"),
            ("os", "remove"),
            ("os", "unlink"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute,
            ):
                attr = node.func.attr
                if isinstance(node.func.value, ast.Name):
                    module = node.func.value.id
                    assert (module, attr) not in forbidden_calls, (
                        f"forbidden mutation call: {module}.{attr}"
                    )

        # String-tool tokens scanned via AST string-constant
        # extraction (not raw source) to avoid docstring false
        # positives.
        forbidden_tokens = {
            "edit_file", "write_file", "delete_file",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_tokens, (
                        f"forbidden mutation tool call: {node.func.id}"
                    )

    def test_public_api_exported(self):
        for name in engine_mod.__all__:
            assert hasattr(engine_mod, name), (
                f"engine.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_run_counterfactual_replay_is_async(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                if node.name == "run_counterfactual_replay":
                    return
        raise AssertionError(
            "run_counterfactual_replay must be `async def`"
        )

    def test_cost_contract_constant_present(self):
        assert hasattr(engine_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION")
        assert engine_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_existing_modules(self):
        """Positive invariant — proves no duplication of existing
        infrastructure (Slice 1 primitive + Causality DAG +
        last_session_summary + DecisionRecord)."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.counterfactual_replay import" in src
        assert "from backend.core.ouroboros.governance.verification.causality_dag import" in src
        assert "last_session_summary" in src
        assert "DecisionRecord" in src
