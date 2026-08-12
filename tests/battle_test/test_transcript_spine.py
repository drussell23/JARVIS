"""Four bounded rings had no shared order and no shared retention.

`o-N` / `d-N` / `t-N` / `n-N` each lived in its own ring with its own
capacity (50 / 30 / 50 / 200) and its own eviction. Two consequences, neither
a bug in any single store:

  * **dangling references** — `o-12` outlives the `t-7` it mentions, because
    the rings are different sizes and evict independently;
  * **no cross-namespace order** — nothing knows whether `t-7` happened
    before or after `n-4`, so "what happened next" has no answer.

This pins the spine those four become views of: one append-only sequence,
one retention policy, four vocabularies preserved.
"""
from __future__ import annotations

import threading
from typing import Any, List

import pytest

from backend.core.ouroboros.battle_test import transcript_spine as ts
from backend.core.ouroboros.battle_test.transcript_spine import (
    TranscriptSpine,
    get_default_spine,
    known_prefixes,
    record_event,
    reset_default_spine,
)

_CAPS = (
    "JARVIS_OP_BLOCK_BUFFER_SIZE",
    "JARVIS_DIFF_ARCHIVE_SIZE",
    "JARVIS_TOOL_RENDER_STORE_SIZE",
    "JARVIS_NARRATIVE_BUFFER_SIZE",
    # Slice 2 added milestones as a fifth vocabulary; a producer that
    # did not contribute capacity would make the spine evict sooner
    # than the union it promises.
    "JARVIS_MILESTONE_BUFFER_SIZE",
)


@pytest.fixture(autouse=True)
def _fresh():
    reset_default_spine()
    yield
    reset_default_spine()


def _tiny(monkeypatch: Any, each: int = 3) -> None:
    for v in _CAPS:
        monkeypatch.setenv(v, str(each))


# ---------------------------------------------------------------------------
# 1. the vocabulary is READ from the stores, never restated
# ---------------------------------------------------------------------------

def test_the_prefixes_come_from_the_stores() -> None:
    """A second copy of `o-`/`d-`/`t-`/`n-` would drift the first time one is
    renamed — the defect `chat_response_style` avoids by reading
    `LABEL_PREFIX` off the executor."""
    assert known_prefixes() == {
        "o-": "op_block", "d-": "diff",
        "t-": "tool_body", "n-": "narrative",
        "m-": "milestone",
    }


def test_no_prefix_literal_is_restated_in_the_spine() -> None:
    import inspect

    src = inspect.getsource(ts)
    body = src.split('"""', 2)[-1]          # skip the module docstring
    for literal in ('"o-"', '"d-"', '"t-"', '"n-"'):
        assert literal not in body, f"{literal} hardcoded in the spine"


def test_capacity_is_the_union_of_the_stores_own_caps(monkeypatch: Any) -> None:
    """Not a new limit: exactly what the four rings could hold between them,
    so unifying retention cannot evict anything that survives today."""
    _tiny(monkeypatch, 5)
    assert ts._derived_capacity() == 25
    monkeypatch.setenv("JARVIS_DIFF_ARCHIVE_SIZE", "11")
    assert ts._derived_capacity() == 31, "the spine did not follow the store"


# ---------------------------------------------------------------------------
# 2. THE property — a live record cannot reference an evicted one
# ---------------------------------------------------------------------------

def test_eviction_is_uniform_so_dangling_is_impossible(monkeypatch: Any) -> None:
    """The whole point. Independent rings let `o-12` outlive `t-7`; one
    sequence with one policy means everything before a live record is live."""
    _tiny(monkeypatch, 3)
    s = TranscriptSpine()
    for i in range(6):
        for kind, p in (("op_block", "o"), ("diff", "d"),
                        ("tool_body", "t"), ("narrative", "n")):
            s.append(kind, f"{p}-{i}")

    seqs = [r.seq for r in s.page()]
    assert seqs == list(range(seqs[0], seqs[-1] + 1)), (
        "the surviving window has a hole — a live record could reference an "
        "evicted one"
    )
    assert len(s) == 15


def test_an_aged_out_ref_is_distinguishable_from_a_bogus_one(
    monkeypatch: Any,
) -> None:
    """'d-0 scrolled off' and 'd-0 never existed' are different answers, and
    an operator who typed a ref they read a minute ago deserves the first."""
    _tiny(monkeypatch, 2)
    s = TranscriptSpine()
    for i in range(12):
        s.append("diff", f"d-{i}")
    assert s.resolve("d-0") is None
    assert s.was_evicted("d-0") is True
    assert s.was_evicted("zz-9") is False, "a non-ref must not claim eviction"


def test_unreadable_capacity_means_unbounded_not_empty(monkeypatch: Any) -> None:
    """Refusing to retain because a capacity could not be read would silently
    discard the transcript — the worst possible failure for this surface."""
    monkeypatch.setattr(ts, "_derived_capacity", lambda: 0)
    s = TranscriptSpine()
    for i in range(500):
        s.append("diff", f"d-{i}")
    assert len(s) == 500


# ---------------------------------------------------------------------------
# 3. ordering across namespaces — the question four rings cannot answer
# ---------------------------------------------------------------------------

def test_interleaved_namespaces_share_one_order() -> None:
    s = TranscriptSpine()
    s.append("op_block", "o-1")
    s.append("narrative", "n-1")
    s.append("tool_body", "t-1")
    s.append("diff", "d-1")
    assert [r.ref for r in s.page()] == ["o-1", "n-1", "t-1", "d-1"]
    assert [r.seq for r in s.page()] == [1, 2, 3, 4]


def test_pagination_is_sequence_keyed_not_offset_keyed(monkeypatch: Any) -> None:
    """A page must stay stable while the spine grows and evicts. An offset
    would shift underneath a paging caller; a sequence names a position in
    the transcript itself."""
    _tiny(monkeypatch, 100)
    s = TranscriptSpine()
    for i in range(10):
        s.append("diff", f"d-{i}")
    first = s.page(after_seq=0, limit=3)
    assert [r.ref for r in first] == ["d-0", "d-1", "d-2"]
    for i in range(10, 20):
        s.append("diff", f"d-{i}")
    resumed = s.page(after_seq=first[-1].seq, limit=3)
    assert [r.ref for r in resumed] == ["d-3", "d-4", "d-5"], (
        "growth shifted an already-served page"
    )


def test_a_kind_filter_narrows_without_losing_order() -> None:
    s = TranscriptSpine()
    for i in range(4):
        s.append("op_block", f"o-{i}")
        s.append("diff", f"d-{i}")
    got = s.page(kind="diff")
    assert [r.ref for r in got] == ["d-0", "d-1", "d-2", "d-3"]
    assert got == sorted(got, key=lambda r: r.seq)


def test_head_seq_is_the_resume_token() -> None:
    s = TranscriptSpine()
    assert s.head_seq == 0
    s.append("diff", "d-1")
    assert s.head_seq == 1
    assert s.page(after_seq=s.head_seq) == []


# ---------------------------------------------------------------------------
# 4. concurrent mutation vs. active iteration
# ---------------------------------------------------------------------------

def test_appends_during_iteration_cannot_invalidate_a_reader() -> None:
    """Append-only is the design BECAUSE of this: a reader holds a snapshot
    that nothing rewrites, so a background agent appending mid-scroll is not
    a race to lock against."""
    s = TranscriptSpine()
    for i in range(5):
        s.append("diff", f"d-{i}")
    seen: List[str] = []
    for rec in s:                     # iterating a snapshot
        seen.append(rec.ref)
        s.append("op_block", f"o-{len(seen)}")   # mutate mid-iteration
    assert seen == ["d-0", "d-1", "d-2", "d-3", "d-4"], (
        "the reader observed writes made during its own iteration"
    )


def test_concurrent_writers_produce_a_total_order() -> None:
    """Ordering is answered by `seq`, not by a mutex the readers share."""
    s = TranscriptSpine()

    def writer(tag: str) -> None:
        for i in range(50):
            s.append("diff", f"{tag}-{i}")

    threads = [threading.Thread(target=writer, args=(t,))
               for t in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = [r.seq for r in s.page()]
    assert len(seqs) == len(set(seqs)), "a sequence number was reused"
    assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# 5. resilience — the transcript must never break a render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,ref", [
    ("", "d-1"), ("diff", ""), (None, None), ("diff", "   "),
])
def test_an_unusable_event_is_refused_not_raised(kind: Any, ref: Any) -> None:
    """A transcript that accepts a blank reference cannot resolve it later."""
    s = TranscriptSpine()
    assert s.append(kind, ref) is None
    assert len(s) == 0


def test_a_reused_ref_resolves_to_the_NEWER_record() -> None:
    """A store re-admitting a reference means the newer object, and the spine
    must resolve to what the store would."""
    s = TranscriptSpine()
    s.append("diff", "d-1", op_id="first")
    s.append("diff", "d-1", op_id="second")
    rec = s.resolve("d-1")
    assert rec is not None and rec.op_id == "second"


def test_a_record_cannot_be_mutated_after_append() -> None:
    """Append-only made a type error rather than a convention."""
    s = TranscriptSpine()
    rec = s.append("diff", "d-1")
    assert rec is not None
    with pytest.raises(Exception):
        rec.seq = 99          # type: ignore[misc]


def test_record_event_never_raises_even_with_no_spine(monkeypatch: Any) -> None:
    """Losing an ordering entry degrades the spine; raising would degrade the
    cockpit. The store must always be able to admit."""
    def _boom() -> Any:
        raise RuntimeError("spine unavailable")

    monkeypatch.setattr(ts, "get_default_spine", _boom)
    record_event("diff", "d-1")          # must not raise


# ---------------------------------------------------------------------------
# 6. the hook is LIVE on the real store paths
# ---------------------------------------------------------------------------

def test_the_real_tool_store_feeds_the_spine() -> None:
    """This codebase's recurring defect is a correct module with no caller.
    Driving the REAL public API, not the helper."""
    from backend.core.ouroboros.battle_test.tool_render_store import (
        BoundedBodyStore,
    )

    spine = get_default_spine()
    store = BoundedBodyStore()
    made = [
        store.store(op_id="op-1", round_index=i, tool_name="bash",
                    body=f"line {i}")
        for i in range(4)
    ]
    refs = [m.ref for m in made]
    assert [r.ref for r in spine.page()] == refs
    assert spine.snapshot_stats()["by_kind"] == {"tool_body": 4}


def test_every_store_carries_the_hook() -> None:
    """All four mint their ref identically; all four must record it."""
    import inspect

    from backend.core.ouroboros.battle_test import (
        diff_archive, narrative_channel, op_block_buffer, tool_render_store,
    )

    for mod in (op_block_buffer, diff_archive,
                tool_render_store, narrative_channel):
        assert "record_event" in inspect.getsource(mod), (
            f"{mod.__name__} mints refs without recording order"
        )
