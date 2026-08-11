"""The causal join, salvaged from PR #35182 as a projection.

These pin the three properties that made closing #35182 the right call
rather than a loss: the join survives, the second store does not, and
the row inherits the spine's single order instead of minting a parallel
one.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import transcript_milestones as tm
from backend.core.ouroboros.battle_test import transcript_timeline as tt
from backend.core.ouroboros.battle_test.transcript_spine import TranscriptSpine
from backend.core.ouroboros.battle_test.transcript_timeline import (
    UNOBSERVED,
    build_timeline,
    timeline_for_op,
)
from backend.core.ouroboros.governance import ops_digest_observer as odo


@pytest.fixture
def wired():
    odo.reset_ops_digest_observer()
    tm.uninstall()
    spine = TranscriptSpine()
    tm.install(spine)
    yield spine, odo.get_ops_digest_observer()
    tm.uninstall()
    odo.reset_ops_digest_observer()


# ===========================================================================
# The join — what #35182 got right
# ===========================================================================


def test_three_callbacks_fold_into_one_row(wired):
    """The gap the PR correctly identified: apply / verify / commit
    arrive separately and none of them alone answers 'what happened to
    that op'."""
    spine, obs = wired
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=3)
    obs.on_verify_completed(op_id="op-1", passed=7, total=7)
    obs.on_commit_succeeded(op_id="op-1", commit_hash="cafebabe")

    rows = build_timeline(spine)
    assert len(rows) == 1
    row = rows[0]
    assert row.op_id == "op-1"
    assert (row.apply_mode, row.apply_files) == ("single", 3)
    assert (row.verify_passed, row.verify_total) == (7, 7)
    assert row.commit_hash == "cafebabe"
    assert row.outcome == "committed"
    assert row.milestone_refs == ("m-1", "m-2", "m-3")


def test_rows_sort_in_the_one_transcript_order(wired):
    """A row's first_seq is a position in the SHARED sequence, so a
    timeline interleaves with diffs and tool bodies instead of forming a
    parallel history. This is what a private r-N namespace could not do."""
    spine, obs = wired
    spine.append("diff", "d-1", op_id="op-b")
    obs.on_apply_succeeded(op_id="op-a", mode="single", files=1)
    spine.append("tool_body", "t-1", op_id="op-b")
    obs.on_apply_succeeded(op_id="op-b", mode="multi", files=2)

    rows = build_timeline(spine)
    assert [r.op_id for r in rows] == ["op-b", "op-a"], (
        "op-b's first record (d-1, seq 1) precedes op-a's apply (seq 2)"
    )
    # And the diff/tool refs are reachable FROM the row, as pointers.
    assert rows[0].related_refs == ("d-1", "t-1")


def test_related_refs_are_pointers_never_bodies(wired):
    """#35182's 'the timeline owns only the edges', adopted verbatim."""
    spine, obs = wired

    class _FatDiff:
        def to_dict(self):
            return {"diff_text": "x" * 10_000}

    spine.append("diff", "d-1", payload=_FatDiff(), op_id="op-1")
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)

    row = timeline_for_op("op-1", spine)
    assert row is not None
    assert row.related_refs == ("d-1",)
    blob = repr(row.to_dict())
    assert "x" * 100 not in blob, "the row copied a body instead of pointing"


def test_latest_write_wins_per_field_not_per_row(wired):
    """A re-applied op must read as a continuation, not a reset: a second
    APPLY may not erase the VERIFY that followed the first."""
    spine, obs = wired
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    obs.on_verify_completed(op_id="op-1", passed=4, total=4)
    obs.on_apply_succeeded(op_id="op-1", mode="multi", files=9)

    row = timeline_for_op("op-1", spine)
    assert row is not None
    assert (row.apply_mode, row.apply_files) == ("multi", 9), "apply updated"
    assert (row.verify_passed, row.verify_total) == (4, 4), "verify survived"


def test_unobserved_verify_is_not_zero(wired):
    """0/0 is a real result. Never reported is a different fact, and a
    row that renders both the same states evidence nobody measured."""
    spine, obs = wired
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    obs.on_verify_completed(op_id="op-2", passed=0, total=0)

    unverified = timeline_for_op("op-1", spine)
    zeroed = timeline_for_op("op-2", spine)
    assert unverified is not None and zeroed is not None
    assert unverified.has_verify is False
    assert unverified.verify_total is UNOBSERVED
    assert "verify" not in unverified.to_dict()
    assert zeroed.has_verify is True
    assert zeroed.to_dict()["verify"]["total"] == 0


def test_scoped_qualifier_reaches_the_row(wired):
    spine, obs = wired
    obs.on_verify_completed(op_id="op-1", passed=9, total=9,
                            scoped_to_applied_op=False)
    row = timeline_for_op("op-1", spine)
    assert row is not None and row.verify_scoped_to_op is False
    assert row.to_dict()["verify"]["scoped_to_applied_op"] is False


def test_outcome_is_derived_so_it_cannot_disagree(wired):
    spine, obs = wired
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    assert timeline_for_op("op-1", spine).outcome == "applied"
    obs.on_verify_completed(op_id="op-1", passed=3, total=5)
    assert timeline_for_op("op-1", spine).outcome == "verify_failed"
    obs.on_verify_completed(op_id="op-1", passed=5, total=5)
    assert timeline_for_op("op-1", spine).outcome == "verified"
    obs.on_commit_succeeded(op_id="op-1", commit_hash="abc")
    assert timeline_for_op("op-1", spine).outcome == "committed"


# ===========================================================================
# What a private store could not do — the reason #35182 was closed
# ===========================================================================


def test_eviction_narrows_a_row_and_the_row_says_so(monkeypatch):
    """The spine's retention is the ONLY retention. A row whose earlier
    milestones aged out must not present as complete."""
    for var in ("JARVIS_OP_BLOCK_BUFFER_SIZE", "JARVIS_DIFF_ARCHIVE_SIZE",
                "JARVIS_TOOL_RENDER_STORE_SIZE",
                "JARVIS_NARRATIVE_BUFFER_SIZE",
                "JARVIS_MILESTONE_BUFFER_SIZE"):
        monkeypatch.setenv(var, "1")          # capacity 5 total

    odo.reset_ops_digest_observer()
    tm.uninstall()
    spine = TranscriptSpine()
    tm.install(spine)
    try:
        obs = odo.get_ops_digest_observer()
        obs.on_apply_succeeded(op_id="op-old", mode="single", files=1)
        for i in range(8):                    # push the old one out
            spine.append("diff", f"d-{i}", op_id="op-new")
        rows = {r.op_id: r for r in build_timeline(spine)}
        assert "op-old" not in rows, "evicted records cannot be projected"
        # first_seq is computed from SURVIVING records, so a predicate like
        # `first_seq <= evicted_through` can never fire. What is knowable
        # is weaker and honest: once anything is evicted, no row can prove
        # it kept everything.
        assert rows["op-new"].partial is True, (
            "a row that cannot prove wholeness must not claim it"
        )
    finally:
        tm.uninstall()
        odo.reset_ops_digest_observer()


def test_projection_holds_no_state_between_calls(wired):
    """Two builds from the same spine are equal, and neither mutates it —
    a projection that cached would be a store wearing a different name."""
    spine, obs = wired
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    before = len(list(spine))
    first = build_timeline(spine)
    second = build_timeline(spine)
    assert first == second
    assert len(list(spine)) == before, "building the timeline mutated the spine"


def test_the_module_writes_nothing_to_disk(tmp_path, monkeypatch):
    """#35182 persisted to .jarvis/operation_timeline.jsonl. The salvage
    must inherit the spine's durability rather than add its own."""
    monkeypatch.chdir(tmp_path)
    odo.reset_ops_digest_observer()
    tm.uninstall()
    spine = TranscriptSpine()
    tm.install(spine)
    try:
        odo.get_ops_digest_observer().on_apply_succeeded(
            op_id="op-1", mode="single", files=1,
        )
        build_timeline(spine)
        assert list(tmp_path.rglob("*.jsonl")) == []
        assert not (tmp_path / ".jarvis").exists()
    finally:
        tm.uninstall()
        odo.reset_ops_digest_observer()


def test_authority_pins_are_registered_and_pass():
    """The AST discipline #35182 established, kept."""
    invariants = tt.register_shipped_invariants()
    assert {i.invariant_name for i in invariants} == {
        "timeline_has_zero_loop_authority",
        "timeline_owns_no_store",
    }
    import ast
    src = open(tt.__file__).read()
    tree = ast.parse(src)
    for inv in invariants:
        assert inv.validate(tree, src) == (), inv.invariant_name


# ===========================================================================
# Resilience
# ===========================================================================


def test_a_spine_that_cannot_be_read_yields_no_rows_not_guesses():
    class _Hostile:
        _evicted_through = 0

        def __iter__(self):
            raise RuntimeError("spine on fire")

    assert build_timeline(_Hostile()) == ()


def test_malformed_milestone_payloads_are_skipped_not_fatal(wired):
    spine, _obs = wired
    spine.append("milestone", "m-99", payload="not a dict", op_id="op-1")
    spine.append("milestone", "m-100", payload={"event": "apply",
                                                "files": "not an int"},
                 op_id="op-1")
    row = timeline_for_op("op-1", spine)
    assert row is not None
    assert row.has_apply is True
    assert row.apply_files == 0, "an unparseable count degrades to 0, not a crash"


def test_causality_catches_a_missing_apply_without_consulting_retention(wired):
    """VERIFY cannot precede the APPLY it describes, so a row carrying one
    without the other is missing a milestone as a matter of causality —
    knowable even when nothing has been evicted."""
    spine, obs = wired
    obs.on_verify_completed(op_id="op-1", passed=2, total=2)
    row = timeline_for_op("op-1", spine)
    assert row is not None
    assert row.partial is False, "nothing was evicted"
    assert row.provably_incomplete is True, "but the apply is causally absent"

    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    healed = timeline_for_op("op-1", spine)
    assert healed is not None and healed.provably_incomplete is False


def test_records_without_an_op_id_are_ignored(wired):
    """narrative_channel does not carry op_id; those records belong to the
    transcript but to no operation, and inventing one would be a lie."""
    spine, obs = wired
    spine.append("narrative", "n-1")
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    rows = build_timeline(spine)
    assert [r.op_id for r in rows] == ["op-1"]


def test_limit_returns_the_most_recent_rows(wired):
    spine, obs = wired
    for i in range(10):
        obs.on_apply_succeeded(op_id=f"op-{i}", mode="single", files=1)
    rows = build_timeline(spine, limit=3)
    assert [r.op_id for r in rows] == ["op-7", "op-8", "op-9"]


def test_max_rows_is_env_tunable(wired, monkeypatch):
    spine, obs = wired
    monkeypatch.setenv(tt.MAX_ROWS_ENV_VAR, "2")
    for i in range(5):
        obs.on_apply_succeeded(op_id=f"op-{i}", mode="single", files=1)
    assert len(build_timeline(spine)) == 2
    monkeypatch.setenv(tt.MAX_ROWS_ENV_VAR, "garbage")
    assert len(build_timeline(spine)) == 5
