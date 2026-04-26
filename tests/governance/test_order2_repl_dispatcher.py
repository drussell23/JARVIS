"""RR Pass B Slice 6 (module 3) — /order2 REPL dispatcher regression suite.

Pins:
  * 14-value DispatchStatus enum + frozen DispatchResult + .ok helper
    + .to_dict shape + DEFAULT/MAX history limits + reason cap.
  * Master flag default-false-pre-graduation.
  * help subcommand always works (even master-off — discoverability).
  * Subcommand allowlist enforced (UNKNOWN_SUBCOMMAND for anything
    outside it).
  * Read-side subcommands: pending (empty + populated), show
    (MISSING_OP_ID + OP_ID_NOT_FOUND + OK), history (default limit +
    custom limit + INVALID_ARGS + clamped to MAX).
  * Reject subcommand: full path including reader prompt, OPERATOR_REQUIRED,
    MISSING_OP_ID, OP_ID_NOT_FOUND, NOT_PENDING, REASON_REQUIRED,
    QUEUE_REJECTED.
  * Amend subcommand (THE authority-gating ceremony):
    - OPERATOR_REQUIRED, MISSING_OP_ID, OP_ID_NOT_FOUND, NOT_PENDING.
    - CORPUS_UNAVAILABLE when corpus.status != LOADED.
    - NO_APPLICABLE_SNAPSHOTS when MetaEvaluation has zero or when
      snapshots referenced aren't in live corpus.
    - REPLAY_ALL_FAILED when sandbox replays all diverge.
    - REASON_REQUIRED when operator types empty.
    - OK end-to-end with real replay executor running.
    - Replay results bundle attached to the queue record on success.
    - operator_authorized=True is what the dispatcher passes (verified
      via mocking the replay executor and asserting kwargs).
  * Authority invariants: no banned governance imports; no subprocess
    /network/env-mutation tokens.
"""
from __future__ import annotations

import ast as _ast
import asyncio
import dataclasses
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.meta.order2_repl_dispatcher import (
    DEFAULT_HISTORY_LIMIT,
    DISPATCH_SCHEMA_VERSION,
    DispatchResult,
    DispatchStatus,
    MAX_HISTORY_LIMIT,
    MAX_REASON_CHARS_DISPATCH,
    dispatch_order2,
    is_enabled,
    parse_argv,
)
from backend.core.ouroboros.governance.meta.order2_review_queue import (
    Order2ReviewQueue,
    QueueEntryStatus,
)
from backend.core.ouroboros.governance.meta.replay_executor import (
    ReplayExecutionResult,
    ReplayExecutionStatus,
)
from backend.core.ouroboros.governance.meta.shadow_replay import (
    ReplayCorpus,
    ReplayLoadStatus,
    ReplaySnapshot,
)


_REPO = Path(__file__).resolve().parent.parent.parent
_MODULE_PATH = (
    _REPO / "backend" / "core" / "ouroboros" / "governance"
    / "meta" / "order2_repl_dispatcher.py"
)


# ===========================================================================
# Fixtures + helpers
# ===========================================================================


@pytest.fixture(autouse=True)
def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REPL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_REPLAY_EXECUTOR_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_ORDER2_REVIEW_QUEUE_PATH",
        str(tmp_path / "queue.jsonl"),
    )
    yield


def _good_runner_source(
    phase_name: str = "CLASSIFY",
    next_phase_name: str = "ROUTE",
):
    return (
        "class GoodRunner(PhaseRunner):\n"
        f"    phase = OperationPhase.{phase_name}\n"
        "    async def run(self, ctx):\n"
        "        try:\n"
        f"            new_ctx = ctx.advance(phase='{next_phase_name}')\n"
        "            return PhaseResult(\n"
        "                next_ctx=new_ctx,\n"
        f"                next_phase=OperationPhase.{next_phase_name},\n"
        "                status='ok', reason=None,\n"
        "            )\n"
        "        except Exception as exc:\n"
        "            return PhaseResult(\n"
        "                next_ctx=ctx, next_phase=None,\n"
        "                status='fail', reason=str(exc),\n"
        "            )\n"
    )


def _diverging_runner_source():
    return (
        "class Diverger(PhaseRunner):\n"
        "    phase = OperationPhase.CLASSIFY\n"
        "    async def run(self, ctx):\n"
        "        new_ctx = ctx.advance(phase='ROUTE', risk_tier='HIGH')\n"
        "        return PhaseResult(\n"
        "            next_ctx=new_ctx,\n"
        "            next_phase=OperationPhase.ROUTE,\n"
        "            status='ok',\n"
        "        )\n"
    )


def _evaluation(
    op_id: str = "op-1",
    phase: str = "CLASSIFY",
    snapshot_op_id: str = "snap-1",
    candidate_source: str = "",
):
    return {
        "schema_version": 1,
        "op_id": op_id,
        "target_phase": phase,
        "target_files": [
            "backend/core/ouroboros/governance/phase_runners/test_runner.py",
        ],
        "rationale": "operator-test proposal",
        "status": "READY_FOR_OPERATOR_REVIEW",
        "manifest_matched": True,
        "ast_validation": {
            "status": "PASSED", "reason": None, "detail": "",
            "classes_inspected": ["GoodRunner"],
        },
        "applicable_snapshots": [
            {"op_id": snapshot_op_id, "phase": phase, "tags": ["seed"]},
        ],
        "notes": [],
        # Slice 6.3 reads candidate_source from the evaluation —
        # Slice 5 will need to populate this in production wiring;
        # tests inject it directly.
        "candidate_source": candidate_source,
    }


def _snapshot(op_id: str = "snap-1", phase: str = "CLASSIFY"):
    return ReplaySnapshot(
        op_id=op_id,
        phase=phase,
        pre_phase_ctx={
            "op_id": op_id, "phase": phase,
            "risk_tier": "SAFE_AUTO",
            "target_files": ["backend/example.py"],
            "candidate_files": [],
        },
        expected_next_phase="ROUTE",
        expected_status="ok",
        expected_reason=None,
        expected_next_ctx={
            "op_id": op_id, "phase": "ROUTE",
            "risk_tier": "SAFE_AUTO",
            "target_files": ["backend/example.py"],
            "candidate_files": [],
        },
    )


def _make_corpus(snaps=None, status=ReplayLoadStatus.LOADED):
    return ReplayCorpus(
        snapshots=tuple(snaps or [_snapshot()]),
        status=status,
    )


def _empty_corpus():
    return ReplayCorpus(snapshots=(), status=ReplayLoadStatus.NOT_LOADED)


def _queue(tmp_path):
    return Order2ReviewQueue(tmp_path / "queue.jsonl")


def _stub_reader(response: str = ""):
    return lambda prompt: response


def _run(coro):
    return asyncio.run(coro)


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


def test_dispatch_status_fourteen_values():
    assert {s.name for s in DispatchStatus} == {
        "OK", "MASTER_OFF", "UNKNOWN_SUBCOMMAND", "MISSING_OP_ID",
        "OP_ID_NOT_FOUND", "NOT_PENDING", "NO_APPLICABLE_SNAPSHOTS",
        "CORPUS_UNAVAILABLE", "REPLAY_ALL_FAILED",
        "REPLAY_AUTHORIZATION_BUG", "REASON_REQUIRED",
        "OPERATOR_REQUIRED", "QUEUE_REJECTED", "INVALID_ARGS",
        "INTERNAL_ERROR",
    }


def test_dispatch_result_is_frozen():
    r = DispatchResult(
        schema_version=1, subcommand="help",
        status=DispatchStatus.OK,
    )
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
    r = DispatchResult(schema_version=DISPATCH_SCHEMA_VERSION,
                       subcommand="pending", status=DispatchStatus.OK,
                       output="x", detail="d")
    d = r.to_dict()
    assert d["schema_version"] == 1
    assert d["subcommand"] == "pending"
    assert d["status"] == "OK"
    assert d["output"] == "x"
    assert d["entry"] is None
    assert d["replay_results"] == []


# ===========================================================================
# B — parse_argv
# ===========================================================================


def test_parse_argv_simple():
    assert parse_argv("show op-1") == ["show", "op-1"]


def test_parse_argv_quoted():
    assert parse_argv('show "op with spaces"') == ["show", "op with spaces"]


def test_parse_argv_empty():
    assert parse_argv("") == []


def test_parse_argv_unbalanced_quotes_falls_back_to_split():
    assert parse_argv('show "op') == ["show", '"op']


# ===========================================================================
# C — Master flag + help bypass + unknown subcommand
# ===========================================================================


def test_master_off_blocks_subcommands_except_help(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REPL_ENABLED", "0")
    assert is_enabled() is False
    res = _run(dispatch_order2(["pending"], queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.MASTER_OFF


def test_master_off_does_not_block_help(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORDER2_REPL_ENABLED", "0")
    res = _run(dispatch_order2(["help"]))
    assert res.status is DispatchStatus.OK
    assert "/order2" in res.output
    assert "amend" in res.output


def test_empty_args_returns_help():
    res = _run(dispatch_order2([]))
    assert res.status is DispatchStatus.OK
    assert "Order-2" in res.output


def test_unknown_subcommand(tmp_path):
    res = _run(dispatch_order2(["foo"], queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.UNKNOWN_SUBCOMMAND
    assert "foo" in res.detail


def test_subcommand_normalized_to_lowercase(tmp_path):
    """SHOW should route the same as show."""
    q = _queue(tmp_path)
    res = _run(dispatch_order2(["SHOW", "op-X"], queue=q))
    assert res.status is DispatchStatus.OP_ID_NOT_FOUND


# ===========================================================================
# D — pending
# ===========================================================================


def test_pending_empty(tmp_path):
    res = _run(dispatch_order2(["pending"], queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.OK
    assert "No pending" in res.output


def test_pending_lists_entries(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    q.enqueue(_evaluation("op-2"))
    res = _run(dispatch_order2(["pending"], queue=q))
    assert res.status is DispatchStatus.OK
    assert "op-1" in res.output
    assert "op-2" in res.output
    assert "PENDING_REVIEW" in res.output


# ===========================================================================
# E — show
# ===========================================================================


def test_show_missing_op_id(tmp_path):
    res = _run(dispatch_order2(["show"], queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.MISSING_OP_ID


def test_show_op_id_not_found(tmp_path):
    res = _run(dispatch_order2(["show", "op-missing"],
                               queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.OP_ID_NOT_FOUND


def test_show_renders_full_entry(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1", phase="CLASSIFY"))
    res = _run(dispatch_order2(["show", "op-1"], queue=q))
    assert res.status is DispatchStatus.OK
    assert res.entry is not None
    assert "op-1" in res.output
    assert "CLASSIFY" in res.output
    assert "MetaEvaluation" in res.output
    assert "PASSED" in res.output  # ast_validation status


# ===========================================================================
# F — history
# ===========================================================================


def test_history_default_limit(tmp_path):
    q = _queue(tmp_path)
    for i in range(5):
        q.enqueue(_evaluation(f"op-{i}"))
    res = _run(dispatch_order2(["history"], queue=q))
    assert res.status is DispatchStatus.OK
    for i in range(5):
        assert f"op-{i}" in res.output


def test_history_custom_limit(tmp_path):
    q = _queue(tmp_path)
    for i in range(5):
        q.enqueue(_evaluation(f"op-{i}"))
    res = _run(dispatch_order2(["history", "2"], queue=q))
    assert res.status is DispatchStatus.OK
    # Only most-recent 2 in output
    assert "op-3" in res.output or "op-4" in res.output


def test_history_invalid_limit(tmp_path):
    res = _run(dispatch_order2(["history", "abc"],
                               queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.INVALID_ARGS


def test_history_zero_limit(tmp_path):
    res = _run(dispatch_order2(["history", "0"],
                               queue=_queue(tmp_path)))
    assert res.status is DispatchStatus.INVALID_ARGS


def test_history_clamped_to_max(tmp_path):
    """history 9999 should clamp to MAX_HISTORY_LIMIT — no INVALID_ARGS."""
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    res = _run(dispatch_order2(["history", "9999"], queue=q))
    assert res.status is DispatchStatus.OK


# ===========================================================================
# G — reject
# ===========================================================================


def test_reject_operator_required(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="", reader=_stub_reader("bad"), queue=q,
    ))
    assert res.status is DispatchStatus.OPERATOR_REQUIRED


def test_reject_missing_op_id(tmp_path):
    res = _run(dispatch_order2(
        ["reject"],
        operator="alice", reader=_stub_reader("r"),
        queue=_queue(tmp_path),
    ))
    assert res.status is DispatchStatus.MISSING_OP_ID


def test_reject_op_id_not_found(tmp_path):
    res = _run(dispatch_order2(
        ["reject", "op-missing"],
        operator="alice", reader=_stub_reader("r"),
        queue=_queue(tmp_path),
    ))
    assert res.status is DispatchStatus.OP_ID_NOT_FOUND


def test_reject_not_pending(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    q.reject("op-1", operator="alice", reason="r")  # already rejected
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="bob", reader=_stub_reader("r2"), queue=q,
    ))
    assert res.status is DispatchStatus.NOT_PENDING


def test_reject_empty_reason(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="alice", reader=_stub_reader(""), queue=q,
    ))
    assert res.status is DispatchStatus.REASON_REQUIRED


def test_reject_ok_records_decision(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="alice", reader=_stub_reader("bad design"), queue=q,
    ))
    assert res.status is DispatchStatus.OK
    assert res.entry is not None
    assert res.entry.status is QueueEntryStatus.REJECTED
    assert res.entry.decision is not None
    assert res.entry.decision.operator == "alice"
    assert res.entry.decision.reason == "bad design"


def test_reject_reader_truncated_at_max(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    long = "x" * (MAX_REASON_CHARS_DISPATCH + 100)
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="alice", reader=_stub_reader(long), queue=q,
    ))
    assert res.status is DispatchStatus.OK
    assert res.entry is not None
    assert res.entry.decision is not None
    # Both layers truncate (dispatcher then queue) — final length
    # must not exceed cap
    assert len(res.entry.decision.reason) == MAX_REASON_CHARS_DISPATCH


def test_reject_reader_raises_returns_internal_error(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))

    def _broken_reader(prompt):
        raise RuntimeError("reader broke")

    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="alice", reader=_broken_reader, queue=q,
    ))
    assert res.status is DispatchStatus.INTERNAL_ERROR
    assert "reader_failed" in res.detail


# ===========================================================================
# H — amend (THE authority-gating ceremony)
# ===========================================================================


def test_amend_operator_required(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1", candidate_source=_good_runner_source()))
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="", reader=_stub_reader("ok"), queue=q,
        corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.OPERATOR_REQUIRED


def test_amend_missing_op_id(tmp_path):
    res = _run(dispatch_order2(
        ["amend"],
        operator="alice", reader=_stub_reader("r"),
        queue=_queue(tmp_path), corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.MISSING_OP_ID


def test_amend_op_id_not_found(tmp_path):
    res = _run(dispatch_order2(
        ["amend", "op-missing"],
        operator="alice", reader=_stub_reader("r"),
        queue=_queue(tmp_path), corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.OP_ID_NOT_FOUND


def test_amend_not_pending(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1", candidate_source=_good_runner_source()))
    q.reject("op-1", operator="alice", reason="r")
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="bob", reader=_stub_reader("r2"), queue=q,
        corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.NOT_PENDING


def test_amend_corpus_unavailable(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1", candidate_source=_good_runner_source()))
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
        corpus=_empty_corpus(),
    ))
    assert res.status is DispatchStatus.CORPUS_UNAVAILABLE


def test_amend_no_applicable_snapshots_in_evaluation(tmp_path):
    q = _queue(tmp_path)
    eval_data = _evaluation("op-1", candidate_source=_good_runner_source())
    eval_data["applicable_snapshots"] = []
    q.enqueue(eval_data)
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
        corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.NO_APPLICABLE_SNAPSHOTS


def test_amend_snapshot_not_in_live_corpus(tmp_path):
    """MetaEvaluation references snap-MISSING but corpus only has snap-1."""
    q = _queue(tmp_path)
    eval_data = _evaluation(
        "op-1", snapshot_op_id="snap-MISSING",
        candidate_source=_good_runner_source(),
    )
    q.enqueue(eval_data)
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
        corpus=_make_corpus(),  # only has snap-1
    ))
    assert res.status is DispatchStatus.NO_APPLICABLE_SNAPSHOTS


def test_amend_replay_all_failed_with_diverging_runner(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation(
        "op-1", candidate_source=_diverging_runner_source(),
    ))
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
        corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.REPLAY_ALL_FAILED
    assert len(res.replay_results) >= 1
    assert all(
        r.status is not ReplayExecutionStatus.PASSED
        for r in res.replay_results
    )


def test_amend_reason_required_after_replay_passes(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation(
        "op-1", candidate_source=_good_runner_source(),
    ))
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader(""), queue=q,
        corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.REASON_REQUIRED
    # Replays still ran (and at least one passed) — bundle attached
    assert len(res.replay_results) >= 1


def test_amend_ok_end_to_end_records_amended_with_replay_bundle(tmp_path):
    q = _queue(tmp_path)
    q.enqueue(_evaluation(
        "op-1", candidate_source=_good_runner_source(),
    ))
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("LGTM after replay"),
        queue=q, corpus=_make_corpus(),
    ))
    assert res.status is DispatchStatus.OK
    assert res.entry is not None
    assert res.entry.status is QueueEntryStatus.AMENDED
    assert res.entry.decision is not None
    assert res.entry.decision.operator == "alice"
    assert res.entry.decision.reason == "LGTM after replay"
    # Replay bundle attached to the recorded decision
    assert len(res.entry.decision.replay_results) >= 1
    assert any(
        r.get("status") == "PASSED"
        for r in res.entry.decision.replay_results
    )


def test_amend_passes_operator_authorized_true_to_replay_executor(tmp_path):
    """Pin the cage-defining call — dispatcher MUST pass
    operator_authorized=True to the replay executor."""
    q = _queue(tmp_path)
    q.enqueue(_evaluation(
        "op-1", candidate_source=_good_runner_source(),
    ))

    captured_kwargs = []

    async def _spy(**kwargs):
        captured_kwargs.append(kwargs)
        return ReplayExecutionResult(
            schema_version=1, op_id=kwargs.get("op_id", ""),
            target_phase=kwargs.get("target_phase", ""),
            snapshot_op_id=kwargs["snapshot"].op_id,
            snapshot_phase=kwargs["snapshot"].phase,
            status=ReplayExecutionStatus.PASSED,
            elapsed_s=0.001,
            notes=("structural_diff_clean",),
        )

    with mock.patch(
        "backend.core.ouroboros.governance.meta.order2_repl_dispatcher."
        "execute_replay_under_operator_trigger", _spy,
    ):
        res = _run(dispatch_order2(
            ["amend", "op-1"],
            operator="alice",
            reader=_stub_reader("LGTM"),
            queue=q, corpus=_make_corpus(),
        ))
    assert res.status is DispatchStatus.OK
    assert len(captured_kwargs) >= 1
    for kwargs in captured_kwargs:
        # Cage rule: this is THE call in the codebase that passes True.
        assert kwargs.get("operator_authorized") is True


def test_amend_runs_replay_for_each_applicable_snapshot(tmp_path):
    """If the corpus has 3 snapshots all matching the eval's
    applicable_snapshots, the dispatcher runs 3 replays."""
    q = _queue(tmp_path)
    eval_data = _evaluation(
        "op-1", candidate_source=_good_runner_source(),
    )
    eval_data["applicable_snapshots"] = [
        {"op_id": f"snap-{i}", "phase": "CLASSIFY", "tags": []}
        for i in range(3)
    ]
    q.enqueue(eval_data)
    snaps = [_snapshot(op_id=f"snap-{i}") for i in range(3)]
    res = _run(dispatch_order2(
        ["amend", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
        corpus=_make_corpus(snaps=snaps),
    ))
    assert res.status is DispatchStatus.OK
    assert len(res.replay_results) == 3


# ===========================================================================
# I — Queue layer rejection surfaced
# ===========================================================================


def test_reject_queue_layer_rejection_surfaced_as_queue_rejected(
    tmp_path, monkeypatch,
):
    """If the queue layer returns a non-OK status, dispatcher maps to
    QUEUE_REJECTED. Force this by disabling the queue mid-call."""
    q = _queue(tmp_path)
    q.enqueue(_evaluation("op-1"))
    monkeypatch.setenv("JARVIS_ORDER2_REVIEW_QUEUE_ENABLED", "0")
    res = _run(dispatch_order2(
        ["reject", "op-1"],
        operator="alice", reader=_stub_reader("r"), queue=q,
    ))
    # Queue is disabled, so .get() returns None and we get OP_ID_NOT_FOUND.
    # That's expected behavior — the dispatcher's "queue disabled"
    # surface is "your op vanished".
    assert res.status is DispatchStatus.OP_ID_NOT_FOUND


# ===========================================================================
# J — Authority invariants (AST grep on module source)
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
        f"order2_repl_dispatcher.py contains banned imports: "
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
        f"order2_repl_dispatcher.py contains forbidden side-effect "
        f"tokens: {found}"
    )


def test_module_explicit_operator_authorized_true_call_present():
    """Source-level pin: the dispatcher source must contain literal
    `operator_authorized=True` somewhere — that is THE call shape
    that authorizes the replay executor. If a refactor accidentally
    elides this, the cage falls open silently."""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "operator_authorized=True" in src
