"""Slice 1 tests — ContextLedger primitive.

Pins:
* Each of the 5 entry kinds round-trips via its typed writer
* Entries are immutable (§7 / §8) — mutation attempts raise
* Immutable correction via new-entry (record_question_answer / record_error_status)
* Per-kind LRU cap bounds growth on runaway ops
* Registry evicts oldest op past max_ops cap
* Query API: get_by_kind / get_since / files_read / tools_used / open_errors /
  latest_error_status / open_questions / approved_paths_so_far
* Summary projection is bounded + safe for SSE (Slice 4 precondition)
* Listener hooks fire once per append; exceptions in listeners don't
  break the write path
* schema_version stamped on every entry
* Thread-safe: concurrent appends don't lose entries
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.context_ledger import (
    CONTEXT_LEDGER_SCHEMA_VERSION,
    ContextLedger,
    ContextLedgerRegistry,
    DecisionEntry,
    ErrorEntry,
    FileReadEntry,
    LedgerEntryKind,
    QuestionEntry,
    ToolCallEntry,
    get_default_registry,
    ledger_for,
    reset_default_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_default_registry()
    yield
    reset_default_registry()


# ===========================================================================
# Per-kind golden writers
# ===========================================================================


def test_file_read_round_trip():
    L = ContextLedger("op-1")
    e = L.record_file_read(
        file_path="backend/foo.py",
        tool="read_file",
        round_index=2,
        content=b"hello",
    )
    assert isinstance(e, FileReadEntry)
    assert e.file_path == "backend/foo.py"
    assert e.content_hash != ""
    assert e.byte_size == 5
    assert e.schema_version == CONTEXT_LEDGER_SCHEMA_VERSION
    stored = L.get_by_kind(LedgerEntryKind.FILE_READ)
    assert len(stored) == 1
    assert stored[0].entry_id == e.entry_id


def test_tool_call_round_trip_fingerprints_args():
    L = ContextLedger("op-1")
    e1 = L.record_tool_call(
        tool="edit_file",
        arguments={"file_path": "x.py", "patch": "abc"},
        round_index=3,
        call_id="c-1",
        status="success",
        duration_ms=42.5,
        output_bytes=1024,
        result_preview="edit ok",
    )
    e2 = L.record_tool_call(
        tool="edit_file",
        arguments={"file_path": "x.py", "patch": "abc"},
        call_id="c-2",
    )
    assert isinstance(e1, ToolCallEntry)
    # Same args → same fingerprint
    assert e1.args_fingerprint == e2.args_fingerprint
    assert len(L.get_by_kind(LedgerEntryKind.TOOL_CALL)) == 2


def test_tool_call_truncates_long_result_preview():
    L = ContextLedger("op-1")
    long_preview = "x" * 5000
    e = L.record_tool_call(
        tool="search_code", call_id="c-long", result_preview=long_preview,
    )
    assert len(e.result_preview) <= 300
    assert e.result_preview.endswith("...")


def test_error_round_trip_defaults_open():
    L = ContextLedger("op-1")
    e = L.record_error(
        error_class="ImportError",
        message="No module named 'foo'",
        where="backend/x.py:12",
    )
    assert isinstance(e, ErrorEntry)
    assert e.status == "open"
    assert e.recovery_attempts == 0


def test_decision_round_trip_captures_rule_id_and_paths():
    L = ContextLedger("op-1")
    e = L.record_decision(
        decision_type="inline_allow",
        outcome="approved",
        reviewer="repl",
        rule_id="RULE_EDIT_OUT_OF_APPROVED",
        approved_paths=("backend/core/",),
        candidate_hash="h-abc",
        operator_note="trusted for this session",
    )
    assert isinstance(e, DecisionEntry)
    assert e.approved_paths == ("backend/core/",)
    assert e.candidate_hash == "h-abc"


def test_question_round_trip_starts_open():
    L = ContextLedger("op-1")
    e = L.record_question(
        question="should I rename FooService → FooClient?",
        related_paths=("backend/foo.py",),
        related_tools=("edit_file",),
    )
    assert isinstance(e, QuestionEntry)
    assert e.status == "open"
    assert e.answer == ""
    assert e.answered_at_iso == ""


# ===========================================================================
# Immutability (§7) — corrections go via new entries
# ===========================================================================


def test_entry_is_frozen():
    L = ContextLedger("op-1")
    e = L.record_tool_call(tool="read_file", call_id="c-1")
    with pytest.raises(Exception):
        e.status = "changed"  # type: ignore[misc]


def test_question_answer_appends_new_entry(monkeypatch):
    L = ContextLedger("op-1")
    q = L.record_question(question="what next?")
    assert L.open_questions()
    ans = L.record_question_answer(
        original_entry_id=q.entry_id, answer="proceed",
    )
    assert ans.entry_id != q.entry_id
    assert ans.status == "answered"
    assert ans.answer == "proceed"
    # Original untouched
    stored = L.get_by_kind(LedgerEntryKind.QUESTION)
    assert len(stored) == 2
    # open_questions() now returns nothing for this question
    assert L.open_questions() == []


def test_question_answer_raises_on_unknown_id():
    L = ContextLedger("op-1")
    with pytest.raises(KeyError):
        L.record_question_answer(original_entry_id="q-does-not-exist",
                                 answer="whatever")


def test_error_status_appends_new_entry():
    L = ContextLedger("op-1")
    err = L.record_error(
        error_class="Timeout", message="x took too long",
        where="backend/y.py:1",
    )
    updated = L.record_error_status(
        original_entry_id=err.entry_id, new_status="resolved",
    )
    assert updated.entry_id != err.entry_id
    assert updated.status == "resolved"
    assert updated.recovery_attempts == err.recovery_attempts + 1
    # Both entries coexist
    stored = L.get_by_kind(LedgerEntryKind.ERROR)
    assert len(stored) == 2
    # latest_error_status reflects the new status
    assert L.latest_error_status(
        error_class="Timeout", where="backend/y.py:1",
    ) == "resolved"
    # open_errors no longer includes it
    assert L.open_errors() == []


def test_error_status_raises_on_unknown_id():
    L = ContextLedger("op-1")
    with pytest.raises(KeyError):
        L.record_error_status(
            original_entry_id="e-nope", new_status="resolved",
        )


# ===========================================================================
# Per-kind LRU cap
# ===========================================================================


def test_per_kind_cap_evicts_oldest(monkeypatch):
    L = ContextLedger("op-1", max_entries_per_kind=3)
    ids = []
    for i in range(5):
        e = L.record_tool_call(tool="read_file", call_id=f"c-{i}")
        ids.append(e.entry_id)
    stored = L.get_by_kind(LedgerEntryKind.TOOL_CALL)
    assert len(stored) == 3
    # The two oldest are gone; last 3 preserved in order.
    assert [e.entry_id for e in stored] == ids[-3:]


def test_cap_does_not_affect_other_kinds(monkeypatch):
    L = ContextLedger("op-1", max_entries_per_kind=2)
    L.record_tool_call(tool="a", call_id="1")
    L.record_tool_call(tool="a", call_id="2")
    L.record_tool_call(tool="a", call_id="3")  # evicts c-1
    L.record_file_read(file_path="x.py")
    L.record_file_read(file_path="y.py")
    assert len(L.get_by_kind(LedgerEntryKind.TOOL_CALL)) == 2
    assert len(L.get_by_kind(LedgerEntryKind.FILE_READ)) == 2


# ===========================================================================
# Query API
# ===========================================================================


def test_get_since_filters_and_orders():
    L = ContextLedger("op-1")
    L.record_tool_call(tool="a", call_id="1")
    mark = L.get_by_kind(LedgerEntryKind.TOOL_CALL)[0].created_at_ts
    # Force timestamp advance by a small sleep
    import time
    time.sleep(0.001)
    L.record_tool_call(tool="b", call_id="2")
    L.record_file_read(file_path="z.py")
    after = L.get_since(mark)
    # Only the second tool call and the file read
    assert len(after) == 2
    kinds = {e.kind for e in after}
    assert kinds == {"tool_call", "file_read"}


def test_files_read_unique_and_sorted():
    L = ContextLedger("op-1")
    L.record_file_read(file_path="b.py")
    L.record_file_read(file_path="a.py")
    L.record_file_read(file_path="a.py")
    assert L.files_read() == ["a.py", "b.py"]


def test_tools_used_unique_and_sorted():
    L = ContextLedger("op-1")
    L.record_tool_call(tool="edit_file", call_id="1")
    L.record_tool_call(tool="read_file", call_id="2")
    L.record_tool_call(tool="edit_file", call_id="3")
    assert L.tools_used() == ["edit_file", "read_file"]


def test_open_errors_groups_by_class_and_where():
    L = ContextLedger("op-1")
    L.record_error(error_class="X", message="m1", where="f:1")
    L.record_error(error_class="Y", message="m2", where="f:2")
    # Same (X, f:1) — latest wins
    e3 = L.record_error(error_class="X", message="m1-v2", where="f:1")
    L.record_error_status(original_entry_id=e3.entry_id,
                          new_status="resolved")
    open_errs = L.open_errors()
    assert len(open_errs) == 1
    assert open_errs[0].error_class == "Y"


def test_approved_paths_so_far_unions_decisions():
    L = ContextLedger("op-1")
    L.record_decision(
        decision_type="plan_approval", outcome="approved",
        approved_paths=("a/", "b/"),
    )
    L.record_decision(
        decision_type="inline_allow", outcome="approved",
        approved_paths=("b/", "c/"),
    )
    L.record_decision(
        decision_type="plan_approval", outcome="rejected",
        approved_paths=("d/",),
    )
    # 'd/' NOT included (outcome=rejected)
    assert L.approved_paths_so_far() == frozenset({"a/", "b/", "c/"})


# ===========================================================================
# Summary projection
# ===========================================================================


def test_summary_shape_is_sse_safe():
    L = ContextLedger("op-1")
    L.record_file_read(file_path="backend/x.py", content=b"abc")
    L.record_tool_call(tool="edit_file", call_id="c1", status="success")
    L.record_error(error_class="E", message="boom", where="x:1")
    L.record_question(question="why?")
    s = L.summary()
    assert s["schema_version"] == CONTEXT_LEDGER_SCHEMA_VERSION
    assert s["op_id"] == "op-1"
    assert s["counts_by_kind"]["file_read"] == 1
    assert s["counts_by_kind"]["tool_call"] == 1
    assert s["counts_by_kind"]["error"] == 1
    assert s["counts_by_kind"]["question"] == 1
    assert s["open_errors_count"] == 1
    assert s["open_questions_count"] == 1
    assert s["latest_open_error"]["error_class"] == "E"
    assert s["latest_open_question"]["question"] == "why?"


def test_summary_handles_empty_ledger():
    L = ContextLedger("op-1")
    s = L.summary()
    assert s["counts_by_kind"]["file_read"] == 0
    assert s["latest_open_error"] is None
    assert s["latest_open_question"] is None


# ===========================================================================
# Listener hooks (Slice 4 integration pin)
# ===========================================================================


def test_on_change_fires_once_per_append():
    L = ContextLedger("op-1")
    events: List[Dict[str, Any]] = []
    L.on_change(events.append)
    L.record_tool_call(tool="x", call_id="1")
    L.record_error(error_class="E", message="m", where="")
    assert len(events) == 2
    assert events[0]["event_type"] == "ledger_entry_added"
    assert events[1]["event_type"] == "ledger_entry_added"


def test_on_change_returns_unsub():
    L = ContextLedger("op-1")
    events: List[Dict[str, Any]] = []
    unsub = L.on_change(events.append)
    L.record_tool_call(tool="x", call_id="1")
    unsub()
    L.record_tool_call(tool="x", call_id="2")
    assert len(events) == 1


def test_listener_exception_does_not_break_append():
    L = ContextLedger("op-1")

    def _bad(_p: Dict[str, Any]) -> None:
        raise RuntimeError("boom")

    L.on_change(_bad)
    # Must still succeed despite the listener raising
    e = L.record_tool_call(tool="x", call_id="1")
    assert e.entry_id.startswith("t-")
    assert L.get_by_kind(LedgerEntryKind.TOOL_CALL) != []


# ===========================================================================
# Registry
# ===========================================================================


def test_registry_returns_same_ledger_per_op_id():
    reg = ContextLedgerRegistry()
    a1 = reg.get_or_create("op-a")
    a2 = reg.get_or_create("op-a")
    assert a1 is a2


def test_registry_isolates_ops():
    reg = ContextLedgerRegistry()
    a = reg.get_or_create("op-a")
    b = reg.get_or_create("op-b")
    a.record_tool_call(tool="x", call_id="1")
    assert b.get_by_kind(LedgerEntryKind.TOOL_CALL) == []


def test_registry_max_ops_evicts_oldest():
    reg = ContextLedgerRegistry(max_ops=2)
    a = reg.get_or_create("op-a")
    b = reg.get_or_create("op-b")
    c = reg.get_or_create("op-c")  # evicts 'op-a'
    # 'op-a' is evicted; a re-get_or_create returns a FRESH ledger
    assert reg.get("op-a") is None
    a2 = reg.get_or_create("op-a")
    assert a2 is not a


def test_registry_drop_removes_op():
    reg = ContextLedgerRegistry()
    reg.get_or_create("op-a")
    assert reg.drop("op-a") is True
    assert reg.drop("op-a") is False


def test_module_singleton_is_shared():
    a = ledger_for("op-a")
    b = ledger_for("op-a")
    assert a is b


def test_reset_default_registry_works():
    ledger_for("op-a").record_tool_call(tool="x", call_id="1")
    reset_default_registry()
    fresh = ledger_for("op-a")
    assert fresh.get_by_kind(LedgerEntryKind.TOOL_CALL) == []


# ===========================================================================
# Thread safety
# ===========================================================================


def test_concurrent_appends_preserve_all_entries():
    L = ContextLedger("op-hot", max_entries_per_kind=10000)
    N = 200
    threads = []

    def _writer(base: int) -> None:
        for i in range(50):
            L.record_tool_call(tool="t", call_id=f"b{base}-{i}")

    for b in range(4):
        t = threading.Thread(target=_writer, args=(b,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    assert len(L.get_by_kind(LedgerEntryKind.TOOL_CALL)) == N


# ===========================================================================
# Construction invariants
# ===========================================================================


def test_ledger_rejects_empty_op_id():
    with pytest.raises(ValueError):
        ContextLedger("")


def test_registry_rejects_empty_op_id():
    reg = ContextLedgerRegistry()
    with pytest.raises(ValueError):
        reg.get_or_create("")


# ===========================================================================
# Entry-id uniqueness across kinds + ops
# ===========================================================================


def test_entry_ids_prefix_by_kind():
    L = ContextLedger("op-1")
    f = L.record_file_read(file_path="x.py")
    t = L.record_tool_call(tool="read_file", call_id="c")
    e = L.record_error(error_class="E", message="m")
    d = L.record_decision(decision_type="dt", outcome="approved")
    q = L.record_question(question="q?")
    assert f.entry_id.startswith("f-")
    assert t.entry_id.startswith("t-")
    assert e.entry_id.startswith("e-")
    assert d.entry_id.startswith("d-")
    assert q.entry_id.startswith("q-")


# ===========================================================================
# Bounded-projection invariants for Slice 4 SSE
# ===========================================================================


def test_projection_has_no_unbounded_fields():
    L = ContextLedger("op-1")
    L.record_tool_call(
        tool="edit_file", call_id="c",
        result_preview="x" * 10_000,  # should be truncated at write time
    )
    snap = L.summary()
    # Summary only counts; doesn't echo result_preview blobs.
    assert "result_preview" not in snap
    # Individual projection *is* bounded by the write-time truncation.
    entry = L.get_by_kind(LedgerEntryKind.TOOL_CALL)[0]
    assert len(getattr(entry, "result_preview")) <= 300


# ===========================================================================
# Schema version is stable + stamped
# ===========================================================================


def test_schema_version_stamped_on_every_entry():
    L = ContextLedger("op-1")
    entries = [
        L.record_file_read(file_path="x.py"),
        L.record_tool_call(tool="t", call_id="c"),
        L.record_error(error_class="E", message="m"),
        L.record_decision(decision_type="dt", outcome="approved"),
        L.record_question(question="q?"),
    ]
    for e in entries:
        assert e.schema_version == CONTEXT_LEDGER_SCHEMA_VERSION
