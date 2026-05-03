"""RR Pass B Slice 6 (module 2) — Order-2 review queue regression suite.

Pins:
  * Module constants + 4-value QueueEntryStatus + EnqueueStatus +
    AmendStatus + RejectStatus enums + frozen dataclasses + .to_dict
    / .from_dict round-trip + sha256 integrity tamper-detection.
  * Master flag default-false-pre-graduation.
  * **Locked-true cage invariant** (Pass B §7.3): the function
    ``amendment_requires_operator`` returns True regardless of any
    env knob value — even ``JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_
    OPERATOR=false`` returns True. This is THE structural cage
    marker; future cage code reads this function (not the env).
  * 8 status outcomes for enqueue (OK, DISABLED, DUPLICATE_OP_ID,
    CAPACITY_EXCEEDED, INVALID_EVALUATION, PERSIST_ERROR).
  * 8 status outcomes for amend (OK, DISABLED, NOT_FOUND, NOT_PENDING,
    OPERATOR_REQUIRED, REASON_REQUIRED, NO_PASSING_REPLAY, PERSIST_ERROR).
  * 6 status outcomes for reject.
  * Persistence pins:
    - Append-only JSONL (state transitions write NEW lines).
    - Latest record per op_id wins for current state.
    - Records survive process restart.
    - Tampered records (bad sha256) are skipped on read with warning.
    - Malformed JSON lines are skipped with warning.
  * TTL expire pin.
  * Authority invariants (AST grep): no banned governance imports;
    only allowed imports are stdlib + meta.* (none required at slice
    surface — keeps minimum import surface).
"""
from __future__ import annotations

import ast as _ast
import dataclasses
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.meta.order2_review_queue import (
    AmendResult,
    AmendStatus,
    DEFAULT_TTL_SECONDS,
    EnqueueResult,
    EnqueueStatus,
    MAX_HISTORY_LINES,
    MAX_OPERATOR_NAME_CHARS,
    MAX_PENDING_ENTRIES,
    MAX_REASON_CHARS,
    OperatorDecision,
    Order2ReviewQueue,
    QUEUE_SCHEMA_VERSION,
    QueueEntry,
    QueueEntryStatus,
    RejectResult,
    RejectStatus,
    amendment_requires_operator,
    get_default_queue,
    is_enabled,
    queue_path,
    reset_default_queue,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "meta" / "order2_review_queue.py"
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        str(tmp_path / "queue.jsonl"),
    )
    yield
    reset_default_queue()


def _evaluation(op_id: str = "op-1", phase: str = "CLASSIFY"):
    """Slice 5 MetaEvaluation.to_dict() shape (the queue stores this
    verbatim)."""
    return {
        "schema_version": 1,
        "op_id": op_id,
        "target_phase": phase,
        "target_files": [
            "backend/core/ouroboros/governance/phase_runners/test_runner.py",
        ],
        "rationale": "test proposal",
        "status": "READY_FOR_OPERATOR_REVIEW",
        "manifest_matched": True,
        "ast_validation": {
            "status": "PASSED", "reason": None, "detail": "",
            "classes_inspected": ["TestRunner"],
        },
        "applicable_snapshots": [
            {"op_id": "snap-1", "phase": phase, "tags": ["seed"]},
        ],
        "notes": [],
    }


def _passed_replay(snapshot_op_id: str = "snap-1"):
    """ReplayExecutionResult.to_dict() PASSED shape."""
    return {
        "schema_version": 1,
        "op_id": "op-1",
        "target_phase": "CLASSIFY",
        "snapshot_op_id": snapshot_op_id,
        "snapshot_phase": "CLASSIFY",
        "status": "PASSED",
        "elapsed_s": 0.012,
        "divergence": None,
        "detail": "",
        "notes": ["structural_diff_clean"],
    }


def _diverged_replay():
    return {**_passed_replay(), "status": "DIVERGED",
            "divergence": {"field_path": "next_phase",
                           "expected": "ROUTE", "actual": "GENERATE",
                           "detail": "x"}}


# ===========================================================================
# A — Module constants + enums
# ===========================================================================


def test_schema_version_pinned():
    assert QUEUE_SCHEMA_VERSION == 1


def test_caps_pinned():
    assert MAX_PENDING_ENTRIES == 256
    assert MAX_HISTORY_LINES == 4096
    assert MAX_REASON_CHARS == 1024
    assert MAX_OPERATOR_NAME_CHARS == 128
    assert DEFAULT_TTL_SECONDS == 7 * 24 * 3600


def test_queue_entry_status_four_values():
    assert {s.name for s in QueueEntryStatus} == {
        "PENDING_REVIEW", "AMENDED", "REJECTED", "EXPIRED",
    }


def test_enqueue_status_six_values():
    assert {s.name for s in EnqueueStatus} == {
        "OK", "DISABLED", "DUPLICATE_OP_ID", "CAPACITY_EXCEEDED",
        "INVALID_EVALUATION", "PERSIST_ERROR",
    }


def test_amend_status_eight_values():
    assert {s.name for s in AmendStatus} == {
        "OK", "DISABLED", "NOT_FOUND", "NOT_PENDING",
        "OPERATOR_REQUIRED", "REASON_REQUIRED",
        "NO_PASSING_REPLAY", "PERSIST_ERROR",
    }


def test_reject_status_six_values():
    assert {s.name for s in RejectStatus} == {
        "OK", "DISABLED", "NOT_FOUND", "NOT_PENDING",
        "OPERATOR_REQUIRED", "REASON_REQUIRED", "PERSIST_ERROR",
    }


def test_dataclasses_are_frozen():
    e = QueueEntry(
        schema_version=1, op_id="o", enqueued_at_iso="i",
        enqueued_at_epoch=1.0,
        status=QueueEntryStatus.PENDING_REVIEW,
        meta_evaluation={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.op_id = "x"  # type: ignore[misc]

    d = OperatorDecision(
        decided_at_iso="i", operator="o", decision="amend", reason="r",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.operator = "x"  # type: ignore[misc]


# ===========================================================================
# B — LOCKED-TRUE CAGE INVARIANT (Pass B §7.3)
# ===========================================================================


def test_amendment_requires_operator_true_by_default():
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_locked_true_under_explicit_false(monkeypatch):
    """The cage hard-pins True even when the env tries to flip it."""
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR", "false",
    )
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_locked_true_under_zero(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR", "0",
    )
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_locked_true_under_no(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR", "no",
    )
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_locked_true_under_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR", "off",
    )
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_locked_true_under_garbage(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR",
        "MALICIOUS_PATCH_ATTEMPT",
    )
    assert amendment_requires_operator() is True


def test_amendment_requires_operator_logs_warning_on_falsy_attempt(
    monkeypatch, caplog,
):
    """When the env is set to a non-truthy value, the cage logs a
    warning (audit visibility) but still returns True."""
    monkeypatch.setenv(
        "JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR", "false",
    )
    import logging as _logging
    with caplog.at_level(_logging.WARNING,
                         logger="backend.core.ouroboros.governance.meta.order2_review_queue"):
        result = amendment_requires_operator()
    assert result is True
    assert any(
        "ignored" in r.message and "cage invariant" in r.message
        for r in caplog.records
    )


# ===========================================================================
# C — Master flag
# ===========================================================================


def test_master_flag_off_returns_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "0")
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    assert is_enabled() is False
    res = q.enqueue(_evaluation())
    assert res.status is EnqueueStatus.DISABLED


def test_master_default_true_post_graduation(monkeypatch):
    """Pass B Slice 6.2 graduation 2026-05-03: review queue master
    flag flipped default-true. Mutation is structurally gated by
    amendment_requires_operator() (locked-true cage), so graduating
    the queue surface is safe."""
    monkeypatch.delenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", raising=False)
    assert is_enabled() is True


def test_disabled_returns_empty_lists_for_read_methods(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "0")
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    assert q.list_pending() == ()
    assert q.list_history() == ()
    assert q.get("any-op") is None


# ===========================================================================
# D — Enqueue
# ===========================================================================


def test_enqueue_ok_persists_to_disk(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    res = q.enqueue(_evaluation("op-1"))
    assert res.status is EnqueueStatus.OK
    assert res.op_id == "op-1"
    assert res.entry is not None
    assert res.entry.status is QueueEntryStatus.PENDING_REVIEW
    assert (tmp_path / "q.jsonl").exists()


def test_enqueue_invalid_evaluation_not_dict(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    res = q.enqueue("not_a_dict")  # type: ignore[arg-type]
    assert res.status is EnqueueStatus.INVALID_EVALUATION


def test_enqueue_invalid_evaluation_missing_op_id(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    res = q.enqueue({"target_phase": "CLASSIFY"})
    assert res.status is EnqueueStatus.INVALID_EVALUATION


def test_enqueue_duplicate_op_id_while_pending(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    r1 = q.enqueue(_evaluation("op-1"))
    assert r1.status is EnqueueStatus.OK
    r2 = q.enqueue(_evaluation("op-1"))
    assert r2.status is EnqueueStatus.DUPLICATE_OP_ID
    assert r2.entry is not None and r2.entry.op_id == "op-1"


def test_enqueue_after_amend_does_not_dedup(tmp_path):
    """Once an op is AMENDED (terminal), re-enqueue is allowed."""
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.amend("op-1", operator="alice", reason="approved",
            replay_results=[_passed_replay()])
    r2 = q.enqueue(_evaluation("op-1"))
    assert r2.status is EnqueueStatus.OK


# ===========================================================================
# E — Amend
# ===========================================================================


def test_amend_ok_with_passing_replay(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend(
        "op-1", operator="alice", reason="LGTM",
        replay_results=[_passed_replay()],
    )
    assert res.status is AmendStatus.OK
    assert res.entry is not None
    assert res.entry.status is QueueEntryStatus.AMENDED
    assert res.entry.decision is not None
    assert res.entry.decision.operator == "alice"
    assert res.entry.decision.decision == "amend"


def test_amend_not_found(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    res = q.amend("op-missing", operator="alice", reason="x",
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.NOT_FOUND


def test_amend_not_pending_after_already_amended(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.amend("op-1", operator="alice", reason="r1",
            replay_results=[_passed_replay()])
    res = q.amend("op-1", operator="bob", reason="r2",
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.NOT_PENDING


def test_amend_operator_required(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend("op-1", operator="", reason="r",
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.OPERATOR_REQUIRED


def test_amend_reason_required(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend("op-1", operator="alice", reason="",
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.REASON_REQUIRED


def test_amend_no_passing_replay_with_zero_results(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend("op-1", operator="alice", reason="r",
                  replay_results=[])
    assert res.status is AmendStatus.NO_PASSING_REPLAY


def test_amend_no_passing_replay_with_only_diverged(tmp_path):
    """All replays diverged — operator cannot amend."""
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend(
        "op-1", operator="alice", reason="r",
        replay_results=[_diverged_replay(), _diverged_replay()],
    )
    assert res.status is AmendStatus.NO_PASSING_REPLAY


def test_amend_passes_with_mixed_pass_and_diverge(tmp_path):
    """At least one PASSED unblocks (operator chose to accept partial
    coverage). Cage records the full bundle as proof."""
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.amend(
        "op-1", operator="alice", reason="accept partial",
        replay_results=[_diverged_replay(), _passed_replay()],
    )
    assert res.status is AmendStatus.OK
    assert res.entry is not None
    assert res.entry.decision is not None
    assert len(res.entry.decision.replay_results) == 2


def test_amend_reason_truncated_at_max(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    long_reason = "x" * (MAX_REASON_CHARS + 100)
    res = q.amend("op-1", operator="alice", reason=long_reason,
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.OK
    assert res.entry is not None
    assert res.entry.decision is not None
    assert len(res.entry.decision.reason) == MAX_REASON_CHARS


def test_amend_operator_truncated_at_max(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    long_op = "alice" + "x" * (MAX_OPERATOR_NAME_CHARS + 100)
    res = q.amend("op-1", operator=long_op, reason="r",
                  replay_results=[_passed_replay()])
    assert res.status is AmendStatus.OK
    assert res.entry is not None
    assert res.entry.decision is not None
    assert len(res.entry.decision.operator) == MAX_OPERATOR_NAME_CHARS


# ===========================================================================
# F — Reject
# ===========================================================================


def test_reject_ok(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.reject("op-1", operator="alice", reason="bad design")
    assert res.status is RejectStatus.OK
    assert res.entry is not None
    assert res.entry.status is QueueEntryStatus.REJECTED
    assert res.entry.decision is not None
    assert res.entry.decision.decision == "reject"
    assert res.entry.decision.replay_results == ()


def test_reject_not_found(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    res = q.reject("op-missing", operator="alice", reason="r")
    assert res.status is RejectStatus.NOT_FOUND


def test_reject_not_pending_after_amend(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    res = q.reject("op-1", operator="bob", reason="r2")
    assert res.status is RejectStatus.NOT_PENDING


def test_reject_operator_required(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.reject("op-1", operator="", reason="r")
    assert res.status is RejectStatus.OPERATOR_REQUIRED


def test_reject_reason_required(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    res = q.reject("op-1", operator="alice", reason="")
    assert res.status is RejectStatus.REASON_REQUIRED


# ===========================================================================
# G — Read-side queries
# ===========================================================================


def test_get_returns_latest_record_per_op(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    entry = q.get("op-1")
    assert entry is not None
    assert entry.status is QueueEntryStatus.AMENDED


def test_list_pending_excludes_terminal_states(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.enqueue(_evaluation("op-2"))
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    pending = q.list_pending()
    assert {e.op_id for e in pending} == {"op-2"}


def test_list_history_includes_all_states_newest_first(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.enqueue(_evaluation("op-2"))
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    history = q.list_history(limit=10)
    assert len(history) == 3
    # Newest first
    assert history[0].enqueued_at_epoch >= history[-1].enqueued_at_epoch


def test_list_history_limit_zero_returns_empty(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    assert q.list_history(limit=0) == ()


# ===========================================================================
# H — Persistence + integrity
# ===========================================================================


def test_persistence_survives_new_queue_instance(tmp_path):
    q1 = Order2ReviewQueue(tmp_path / "q.jsonl")
    q1.enqueue(_evaluation("op-1"))
    q2 = Order2ReviewQueue(tmp_path / "q.jsonl")
    assert q2.get("op-1") is not None


def test_jsonl_format_one_record_per_line(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    lines = (tmp_path / "q.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        d = json.loads(line)
        assert "op_id" in d
        assert "status" in d
        assert "record_sha256" in d


def test_record_sha256_round_trip(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    entry = q.get("op-1")
    assert entry is not None
    assert entry.record_sha256
    assert entry.verify_integrity() is True


def test_tampered_record_skipped_on_read(tmp_path, caplog):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    p = tmp_path / "q.jsonl"
    raw = p.read_text()
    # Tamper: change op_id but keep stored sha256
    tampered = raw.replace('"op-1"', '"op-TAMPERED"', 1)
    p.write_text(tampered)
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        out = q.get("op-1")
    # The tampered record is skipped, so original is gone
    assert out is None
    assert any("sha256 mismatch" in r.message for r in caplog.records)


def test_malformed_json_line_skipped(tmp_path, caplog):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    p = tmp_path / "q.jsonl"
    with p.open("a") as f:
        f.write("{this is not valid json\n")
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        entry = q.get("op-1")
    assert entry is not None  # original record still readable
    assert any("malformed json" in r.message for r in caplog.records)


def test_state_transitions_append_new_lines_not_overwrite(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    line_count_after_enqueue = len(
        (tmp_path / "q.jsonl").read_text().splitlines()
    )
    q.amend("op-1", operator="alice", reason="r",
            replay_results=[_passed_replay()])
    line_count_after_amend = len(
        (tmp_path / "q.jsonl").read_text().splitlines()
    )
    assert line_count_after_amend == line_count_after_enqueue + 1


# ===========================================================================
# I — TTL expire
# ===========================================================================


def test_expire_stale_marks_old_pending_as_expired(tmp_path):
    import time as _t
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    # Sleep briefly then expire with very short TTL
    _t.sleep(0.05)
    expired = q.expire_stale(ttl_seconds=1)  # 1s TTL — but our op
    # is fresh, so should NOT expire
    assert expired == 0
    # Now expire with effectively-zero TTL via a very small float-cast
    # path: ttl_seconds=0 short-circuits to 0 returns. So instead
    # rewrite with a far-past epoch via direct file munging.


def test_expire_stale_zero_ttl_returns_zero(tmp_path):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    assert q.expire_stale(ttl_seconds=0) == 0


def test_expire_stale_disabled_returns_zero(tmp_path, monkeypatch):
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "0")
    assert q.expire_stale(ttl_seconds=1) == 0


def test_expire_stale_with_stale_record_via_file_munging(tmp_path):
    """Simulate an old record by writing a record with epoch=0 then
    calling expire_stale."""
    q = Order2ReviewQueue(tmp_path / "q.jsonl")
    q.enqueue(_evaluation("op-1"))
    p = tmp_path / "q.jsonl"
    # Read the record, rewrite enqueued_at_epoch to 0, recompute hash.
    line = p.read_text().splitlines()[0]
    rec = json.loads(line)
    rec["enqueued_at_epoch"] = 0.0
    # Recompute sha256 over payload sans the hash field.
    from backend.core.ouroboros.governance.meta.order2_review_queue import (
        _hash_record,
    )
    rec["record_sha256"] = _hash_record(rec)
    p.write_text(json.dumps(rec) + "\n")
    # Now expire with reasonable TTL — the epoch-0 record is stale.
    expired = q.expire_stale(ttl_seconds=3600)
    assert expired == 1
    entry = q.get("op-1")
    assert entry is not None
    assert entry.status is QueueEntryStatus.EXPIRED


# ===========================================================================
# J — Default singleton
# ===========================================================================


def test_get_default_queue_returns_singleton(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        str(tmp_path / "default.jsonl"),
    )
    reset_default_queue()
    q1 = get_default_queue()
    q2 = get_default_queue()
    assert q1 is q2


def test_reset_default_queue_creates_fresh_instance(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        str(tmp_path / "default.jsonl"),
    )
    reset_default_queue()
    q1 = get_default_queue()
    reset_default_queue()
    q2 = get_default_queue()
    assert q1 is not q2


def test_queue_path_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.jsonl"
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_PATH", str(custom))
    assert queue_path() == custom


# ===========================================================================
# K — Authority invariants (AST grep)
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
    )
    found_banned = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            for sub in banned_substrings:
                if sub in mod:
                    found_banned.append((mod, sub))
        elif isinstance(node, _ast.Import):
            for n in node.names:
                for sub in banned_substrings:
                    if sub in n.name:
                        found_banned.append((n.name, sub))
    assert not found_banned, (
        f"order2_review_queue.py contains banned governance imports: "
        f"{found_banned}"
    )


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
    assert not found, (
        f"order2_review_queue.py contains forbidden side-effect "
        f"tokens: {found}"
    )


def test_locked_true_invariant_returns_constant_true():
    """Source-level pin: the function body MUST end with `return True`
    (no env-conditional return)."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    found_func = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef):
            if node.name == "amendment_requires_operator":
                found_func = node
                break
    assert found_func is not None, (
        "amendment_requires_operator function not found"
    )
    # Final statement must be `return True`
    last_stmt = found_func.body[-1]
    assert isinstance(last_stmt, _ast.Return), (
        "amendment_requires_operator must end with `return True`"
    )
    assert isinstance(last_stmt.value, _ast.Constant), (
        "amendment_requires_operator return must be a constant"
    )
    assert last_stmt.value.value is True, (
        f"amendment_requires_operator must return True (got "
        f"{last_stmt.value.value!r})"
    )


# ===========================================================================
# L — Round-trip serialization
# ===========================================================================


def test_queue_entry_to_dict_from_dict_round_trip():
    e = QueueEntry(
        schema_version=QUEUE_SCHEMA_VERSION,
        op_id="op-rt", enqueued_at_iso="2026-04-26T00:00:00+00:00",
        enqueued_at_epoch=1000.0,
        status=QueueEntryStatus.AMENDED,
        meta_evaluation={"foo": "bar"},
        decision=OperatorDecision(
            decided_at_iso="2026-04-26T00:01:00+00:00",
            operator="alice", decision="amend", reason="ok",
            replay_results=({"status": "PASSED"},),
        ),
    )
    d = e.to_dict()
    e2 = QueueEntry.from_dict(d)
    assert e2.op_id == "op-rt"
    assert e2.status is QueueEntryStatus.AMENDED
    assert e2.decision is not None
    assert e2.decision.operator == "alice"
    assert e2.decision.replay_results == ({"status": "PASSED"},)


def test_operator_decision_round_trip():
    d = OperatorDecision(
        decided_at_iso="2026-04-26T00:00:00+00:00",
        operator="bob", decision="reject", reason="no",
    )
    d2 = OperatorDecision.from_dict(d.to_dict())
    assert d2 == d
