"""Tests for diff_archive (Gap #4 Slice 1)."""
from __future__ import annotations

import threading

import pytest

from backend.core.ouroboros.battle_test.diff_archive import (
    ARCHIVE_SIZE_ENV_VAR,
    ArchivedDiff,
    DIFF_ARCHIVE_SCHEMA_VERSION,
    DiffArchive,
    DiffOutcome,
    REF_PREFIX,
    VerifyOutcome,
    get_default_archive,
    reset_default_archive_for_tests,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ARCHIVE_SIZE_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


def _add_one(
    archive: DiffArchive, *,
    op_id: str = "op-x", risk_tier: str = "notify_apply",
    file_paths=("foo.py",), diff_text: str = "+a\n-b",
    summary: str = "+1/-1",
) -> ArchivedDiff:
    return archive.add(
        op_id=op_id,
        risk_tier=risk_tier,
        file_paths=file_paths,
        diff_text=diff_text,
        summary=summary,
    )


# ===========================================================================
# Schema + constants
# ===========================================================================


def test_schema_version_pinned():
    assert DIFF_ARCHIVE_SCHEMA_VERSION == "diff_archive.v1"


def test_ref_prefix_is_d_dash():
    assert REF_PREFIX == "d-"


# ===========================================================================
# Closed taxonomies
# ===========================================================================


def test_diff_outcome_closed_taxonomy():
    assert {m.value for m in DiffOutcome} == {
        "pending", "applied", "rejected", "superseded", "failed",
    }


def test_verify_outcome_closed_taxonomy():
    assert {m.value for m in VerifyOutcome} == {
        "pending", "passed", "failed", "skipped",
    }


@pytest.mark.parametrize("outcome,terminal", [
    (DiffOutcome.PENDING, False),
    (DiffOutcome.APPLIED, True),
    (DiffOutcome.REJECTED, True),
    (DiffOutcome.SUPERSEDED, True),
    (DiffOutcome.FAILED, True),
])
def test_diff_outcome_is_terminal(outcome: DiffOutcome, terminal: bool):
    assert outcome.is_terminal is terminal


@pytest.mark.parametrize("outcome,terminal", [
    (VerifyOutcome.PENDING, False),
    (VerifyOutcome.PASSED, True),
    (VerifyOutcome.FAILED, True),
    (VerifyOutcome.SKIPPED, True),
])
def test_verify_outcome_is_terminal(
    outcome: VerifyOutcome, terminal: bool,
):
    assert outcome.is_terminal is terminal


@pytest.mark.parametrize("raw,expected", [
    ("APPLIED", DiffOutcome.APPLIED),
    ("rejected", DiffOutcome.REJECTED),
    ("  failed  ", DiffOutcome.FAILED),
    (DiffOutcome.PENDING, DiffOutcome.PENDING),
])
def test_diff_outcome_coerce_recognized(raw, expected):
    assert DiffOutcome.coerce(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "garbage", 42])
def test_diff_outcome_coerce_unknowns_return_pending(raw):
    assert DiffOutcome.coerce(raw) is DiffOutcome.PENDING


# ===========================================================================
# Capacity construction
# ===========================================================================


def test_explicit_capacity_used():
    a = DiffArchive(capacity=7)
    assert a.capacity == 7


def test_default_capacity_when_unset():
    assert DiffArchive().capacity == 30


def test_env_capacity(monkeypatch):
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "12")
    assert DiffArchive().capacity == 12


def test_env_capacity_clamped(monkeypatch):
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "0")
    assert DiffArchive().capacity == 1
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "999999")
    assert DiffArchive().capacity == 1_000


def test_env_capacity_garbage_falls_back(monkeypatch):
    monkeypatch.setenv(ARCHIVE_SIZE_ENV_VAR, "not-int")
    assert DiffArchive().capacity == 30


# ===========================================================================
# add() — basic ref allocation
# ===========================================================================


def test_add_returns_archived_diff_with_ref():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    assert isinstance(e, ArchivedDiff)
    assert e.ref == "d-1"
    assert e.apply_outcome is DiffOutcome.PENDING
    assert e.verify_outcome is VerifyOutcome.PENDING
    assert e.terminal_at == 0.0


def test_refs_are_monotonic():
    a = DiffArchive(capacity=10)
    refs = [_add_one(a).ref for _ in range(5)]
    assert refs == ["d-1", "d-2", "d-3", "d-4", "d-5"]


def test_add_normalizes_file_paths_from_string():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier="notify_apply",
        file_paths="single.py", diff_text="",
    )
    assert e.file_paths == ("single.py",)


def test_add_normalizes_file_paths_from_iterable():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier="notify_apply",
        file_paths=["a.py", "b.py", ""], diff_text="",
    )
    # Empty entries dropped.
    assert e.file_paths == ("a.py", "b.py")


def test_add_normalizes_risk_tier_to_lowercase():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier="NOTIFY_APPLY",
        file_paths=("x.py",), diff_text="",
    )
    assert e.risk_tier == "notify_apply"


def test_add_uses_unknown_when_risk_tier_blank():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier=None,
        file_paths=("x.py",), diff_text="",
    )
    assert e.risk_tier == "unknown"


def test_add_handles_pathological_inputs():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id=None, risk_tier=None, file_paths=None,
        diff_text=None, summary=None, review_branch=None,
    )
    assert isinstance(e, ArchivedDiff)
    assert e.op_id == ""
    assert e.file_paths == ()
    assert e.diff_text == ""


# ===========================================================================
# Eviction — drop-oldest
# ===========================================================================


def test_eviction_drops_oldest_first():
    a = DiffArchive(capacity=3)
    refs = [_add_one(a, op_id=f"op-{i}").ref for i in range(5)]
    assert a.lookup(refs[0]) is None  # d-1 evicted
    assert a.lookup(refs[1]) is None  # d-2 evicted
    assert a.lookup(refs[2]) is not None
    assert a.lookup(refs[3]) is not None
    assert a.lookup(refs[4]) is not None
    assert len(a) == 3


def test_eviction_preserves_seq_after_overflow():
    a = DiffArchive(capacity=2)
    for i in range(6):
        _add_one(a, op_id=f"op-{i}")
    snap = a.snapshot()
    assert snap.next_seq == 7
    assert snap.size == 2


def test_clear_drops_all_but_not_seq():
    a = DiffArchive(capacity=10)
    r1 = _add_one(a).ref
    a.clear()
    assert a.lookup(r1) is None
    # Counter must NOT reset — next ref is d-2.
    r2 = _add_one(a).ref
    assert r2 == "d-2"


# ===========================================================================
# Snapshot — counts by outcome
# ===========================================================================


def test_snapshot_counts_pending_only_at_start():
    a = DiffArchive(capacity=10)
    for _ in range(3):
        _add_one(a)
    snap = a.snapshot()
    assert snap.pending_count == 3
    assert snap.applied_count == 0
    assert snap.rejected_count == 0
    assert snap.failed_count == 0


def test_snapshot_counts_after_lifecycle_transitions():
    a = DiffArchive(capacity=10)
    refs = [_add_one(a).ref for _ in range(4)]
    a.mark_applied(refs[0], DiffOutcome.APPLIED)
    a.mark_applied(refs[1], DiffOutcome.REJECTED)
    a.mark_applied(refs[2], DiffOutcome.FAILED)
    # refs[3] stays PENDING
    snap = a.snapshot()
    assert snap.pending_count == 1
    assert snap.applied_count == 1
    assert snap.rejected_count == 1
    assert snap.failed_count == 1


def test_snapshot_utilization_math():
    a = DiffArchive(capacity=4)
    for _ in range(2):
        _add_one(a)
    assert a.snapshot().utilization == 0.5


# ===========================================================================
# mark_applied — lifecycle transitions
# ===========================================================================


def test_mark_applied_transitions_pending_to_applied():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    updated = a.mark_applied(e.ref, DiffOutcome.APPLIED)
    assert updated is not None
    assert updated.apply_outcome is DiffOutcome.APPLIED
    assert updated.terminal_at > 0.0


def test_mark_applied_records_error_on_failed():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    updated = a.mark_applied(
        e.ref, DiffOutcome.FAILED, error="git commit failed: HEAD detached",
    )
    assert updated is not None
    assert updated.apply_outcome is DiffOutcome.FAILED
    assert "HEAD detached" in updated.apply_error


def test_mark_applied_idempotent_for_terminal_with_same_outcome():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    a.mark_applied(e.ref, DiffOutcome.APPLIED)
    again = a.mark_applied(e.ref, DiffOutcome.APPLIED)
    assert again is not None
    assert again.apply_outcome is DiffOutcome.APPLIED


def test_mark_applied_freezes_after_terminal():
    """Once APPLIED, attempting to flip to REJECTED is silently ignored."""
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    a.mark_applied(e.ref, DiffOutcome.APPLIED)
    second = a.mark_applied(e.ref, DiffOutcome.REJECTED)
    assert second is not None
    assert second.apply_outcome is DiffOutcome.APPLIED  # unchanged


def test_mark_applied_unknown_ref_returns_none():
    a = DiffArchive(capacity=10)
    assert a.mark_applied("d-999", DiffOutcome.APPLIED) is None
    assert a.mark_applied(None, DiffOutcome.APPLIED) is None


def test_mark_applied_lookup_reflects_update():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    a.mark_applied(e.ref, DiffOutcome.REJECTED, error="operator")
    fetched = a.lookup(e.ref)
    assert fetched is not None
    assert fetched.apply_outcome is DiffOutcome.REJECTED
    assert fetched.apply_error == "operator"


# ===========================================================================
# mark_verified — lifecycle transitions
# ===========================================================================


def test_mark_verified_transitions_pending_to_passed():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    updated = a.mark_verified(e.ref, VerifyOutcome.PASSED)
    assert updated is not None
    assert updated.verify_outcome is VerifyOutcome.PASSED


def test_mark_verified_freezes_after_terminal():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    a.mark_verified(e.ref, VerifyOutcome.PASSED)
    second = a.mark_verified(e.ref, VerifyOutcome.FAILED)
    assert second is not None
    assert second.verify_outcome is VerifyOutcome.PASSED  # unchanged


# ===========================================================================
# attach_review_branch — Slice 2 hook
# ===========================================================================


def test_attach_review_branch_stamps_branch_name():
    a = DiffArchive(capacity=10)
    e = _add_one(a)
    assert e.review_branch is None
    updated = a.attach_review_branch(e.ref, "ouroboros/preview/op-x")
    assert updated is not None
    assert updated.review_branch == "ouroboros/preview/op-x"


def test_attach_review_branch_unknown_ref_returns_none():
    a = DiffArchive(capacity=10)
    assert a.attach_review_branch("d-999", "branch") is None


# ===========================================================================
# Query API
# ===========================================================================


def test_list_recent_newest_first():
    a = DiffArchive(capacity=10)
    for i in range(5):
        _add_one(a, op_id=f"op-{i}")
    recent = a.list_recent(limit=3)
    assert tuple(e.ref for e in recent) == ("d-5", "d-4", "d-3")


def test_list_recent_zero_limit_empty():
    a = DiffArchive(capacity=10)
    _add_one(a)
    assert a.list_recent(limit=0) == ()
    assert a.list_recent(limit=-1) == ()


def test_find_by_op_id():
    a = DiffArchive(capacity=10)
    _add_one(a, op_id="op-A")
    _add_one(a, op_id="op-B")
    _add_one(a, op_id="op-A")
    matches = a.find_by_op_id("op-A")
    assert len(matches) == 2
    assert all(e.op_id == "op-A" for e in matches)


def test_find_by_op_id_unknown_empty():
    a = DiffArchive(capacity=10)
    assert a.find_by_op_id("nope") == ()
    assert a.find_by_op_id("") == ()
    assert a.find_by_op_id(None) == ()


def test_find_by_file():
    a = DiffArchive(capacity=10)
    a.add(
        op_id="o", risk_tier="x",
        file_paths=("a.py", "b.py"), diff_text="",
    )
    a.add(
        op_id="o", risk_tier="x",
        file_paths=("c.py",), diff_text="",
    )
    matches = a.find_by_file("a.py")
    assert len(matches) == 1
    assert "a.py" in matches[0].file_paths


def test_find_by_outcome_filters_by_apply():
    a = DiffArchive(capacity=10)
    refs = [_add_one(a).ref for _ in range(3)]
    a.mark_applied(refs[0], DiffOutcome.APPLIED)
    a.mark_applied(refs[1], DiffOutcome.REJECTED)
    applied = a.find_by_outcome(apply=DiffOutcome.APPLIED)
    assert len(applied) == 1
    rejected = a.find_by_outcome(apply=DiffOutcome.REJECTED)
    assert len(rejected) == 1
    pending = a.find_by_outcome(apply=DiffOutcome.PENDING)
    assert len(pending) == 1


def test_find_by_outcome_combined_apply_and_verify():
    a = DiffArchive(capacity=10)
    refs = [_add_one(a).ref for _ in range(3)]
    a.mark_applied(refs[0], DiffOutcome.APPLIED)
    a.mark_verified(refs[0], VerifyOutcome.PASSED)
    a.mark_applied(refs[1], DiffOutcome.APPLIED)
    a.mark_verified(refs[1], VerifyOutcome.FAILED)
    a.mark_applied(refs[2], DiffOutcome.REJECTED)
    applied_passed = a.find_by_outcome(
        apply=DiffOutcome.APPLIED, verify=VerifyOutcome.PASSED,
    )
    assert len(applied_passed) == 1
    assert applied_passed[0].ref == refs[0]


# ===========================================================================
# Defensive coercion — non-string lookups, etc.
# ===========================================================================


def test_lookup_non_string_returns_none():
    a = DiffArchive(capacity=10)
    assert a.lookup(None) is None
    assert a.lookup(42) is None


def test_to_dict_omits_diff_text_by_default():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier="notify_apply",
        file_paths=("x.py",), diff_text="huge\n" * 100,
    )
    d = e.to_dict()
    assert "diff_text" not in d
    assert d["diff_chars"] > 0


def test_to_dict_includes_diff_text_when_requested():
    a = DiffArchive(capacity=10)
    e = a.add(
        op_id="op", risk_tier="notify_apply",
        file_paths=("x.py",), diff_text="line1\nline2",
    )
    d = e.to_dict(include_diff_text=True)
    assert d["diff_text"] == "line1\nline2"


# ===========================================================================
# Thread safety
# ===========================================================================


def test_concurrent_adds_produce_unique_refs():
    a = DiffArchive(capacity=10_000)
    n_threads = 8
    inserts_per = 30
    refs: list = []
    refs_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _worker(tid: int):
        barrier.wait()
        local: list = []
        for i in range(inserts_per):
            e = a.add(
                op_id=f"op-{tid}", risk_tier="notify_apply",
                file_paths=(f"f{i}.py",), diff_text=f"#{i}",
            )
            local.append(e.ref)
        with refs_lock:
            refs.extend(local)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(refs) == n_threads * inserts_per
    assert len(set(refs)) == len(refs)
    for r in refs:
        assert a.lookup(r) is not None


# ===========================================================================
# Singleton + reset
# ===========================================================================


def test_singleton_stable():
    a = get_default_archive()
    b = get_default_archive()
    assert a is b


def test_reset_drops_singleton():
    a = get_default_archive()
    reset_default_archive_for_tests()
    b = get_default_archive()
    assert a is not b
