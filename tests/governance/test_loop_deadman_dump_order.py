"""Tombstone dump ordering -- MainThread-first (fix/watch-budget-deadman-dump).

Defect (live session bt-iso-1783137488): when the LoopDeadman fired on a
34.9s wedge, it dumped 76 thread tombstones -- but the MainThread
tombstone was MISSING (cut off by ``os._exit(75)`` before it could be
written). The MainThread stack IS the diagnostic payload -- it names the
loop's exact block location -- so losing it defeats the instrument's
purpose.

Fix: the wedge-dump sequence now serializes + FLUSHES the MainThread
tombstone FIRST, then dumps the remaining threads, flushing again before
``os._exit(75)``. This is proven at two levels:

  1. Unit-level -- ``_dump_tombstones(frames, main_ident, emit, flush)``
     is a pure, injectable helper: MainThread first + tagged, remaining
     threads still all emitted, a raising emit for one thread never
     skips the rest, and ``flush`` fires both before and after the
     remainder.
  2. Source-level -- a static pin proves the real wedge branch
     (``_fire_wedge``) calls the main-first dump + flush sequence
     strictly BEFORE ``os._exit(75)``.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance import loop_deadman as ld

_REPO = Path(__file__).resolve().parents[2]
_GOV = _REPO / "backend/core/ouroboros/governance"


# -- fixtures -------------------------------------------------------------


class _FakeFrame:
    """Stand-in for a real frame object -- ``_dump_tombstones`` never
    inspects the frame itself (that's the injected ``emit``'s job), so a
    sentinel is sufficient and keeps these tests fast + frame-shape
    independent."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


def _make_frames(main_ident: int = 1) -> dict:
    return {
        main_ident: _FakeFrame("main"),
        2001: _FakeFrame("worker-a"),
        2002: _FakeFrame("worker-b"),
        2003: _FakeFrame("worker-c"),
    }


# -- (a) MainThread emitted FIRST and tagged MAIN ------------------------


def test_main_thread_dumped_first_and_tagged():
    frames = _make_frames(main_ident=1)
    calls = []

    def emit(tid, frame, is_main):
        calls.append((tid, is_main))

    ld._dump_tombstones(frames, main_ident=1, emit=emit)

    assert calls[0] == (1, True), (
        f"MainThread (ident=1) must be the first emitted call, got {calls[0]}"
    )
    # every other emitted call must be tagged is_main=False
    assert all(is_main is False for _, is_main in calls[1:])


# -- (b) all other threads are still emitted after MainThread -----------


def test_all_remaining_threads_emitted_after_main():
    frames = _make_frames(main_ident=1)
    calls = []

    def emit(tid, frame, is_main):
        calls.append(tid)

    ld._dump_tombstones(frames, main_ident=1, emit=emit)

    # main + 3 workers == 4 total emissions, no thread dropped.
    assert set(calls) == set(frames.keys())
    assert len(calls) == len(frames)
    assert calls[0] == 1
    assert set(calls[1:]) == {2001, 2002, 2003}


# -- (c) a raising emit for one thread must not skip the rest -----------


def test_raising_emit_does_not_abort_remaining_dumps():
    frames = _make_frames(main_ident=1)
    calls = []

    def emit(tid, frame, is_main):
        calls.append(tid)
        if tid == 2001:
            raise RuntimeError("boom -- simulated per-thread dump failure")

    # must not raise out of _dump_tombstones itself
    ld._dump_tombstones(frames, main_ident=1, emit=emit)

    # all 4 threads were still attempted despite 2001 raising mid-dump.
    assert set(calls) == set(frames.keys())


def test_raising_main_emit_does_not_abort_remaining_dumps():
    """Even if the MOST important dump (MainThread) itself raises, the
    remaining threads must still be attempted -- the dump path must
    NEVER raise or short-circuit."""
    frames = _make_frames(main_ident=1)
    calls = []

    def emit(tid, frame, is_main):
        calls.append(tid)
        if is_main:
            raise RuntimeError("boom -- main dump failed")

    ld._dump_tombstones(frames, main_ident=1, emit=emit)

    assert set(calls) == set(frames.keys())


# -- (d) flush called before AND after the remainder ---------------------


def test_flush_called_before_and_after_remainder():
    frames = _make_frames(main_ident=1)
    order = []

    def emit(tid, frame, is_main):
        order.append(("emit", tid, is_main))

    def flush():
        order.append(("flush",))

    ld._dump_tombstones(frames, main_ident=1, emit=emit, flush=flush)

    flush_indices = [i for i, ev in enumerate(order) if ev == ("flush",)]
    assert len(flush_indices) == 2, f"flush must fire exactly twice, got {order}"

    main_index = order.index(("emit", 1, True))
    remainder_indices = [
        i for i, ev in enumerate(order)
        if ev[0] == "emit" and ev[1] != 1
    ]

    first_flush, second_flush = flush_indices
    # flush #1 sits between the main dump and the remainder.
    assert main_index < first_flush, "flush must come AFTER the main dump"
    assert all(first_flush < i for i in remainder_indices), (
        "flush must come BEFORE the remainder dump"
    )
    # flush #2 sits after every remainder emission.
    assert all(i < second_flush for i in remainder_indices), (
        "second flush must come AFTER the remainder dump"
    )


def test_flush_is_optional_and_never_required():
    """When no flush callable is supplied (e.g. legacy callers), the dump
    must still complete without raising."""
    frames = _make_frames(main_ident=1)
    calls = []
    ld._dump_tombstones(frames, main_ident=1, emit=lambda t, f, m: calls.append(t))
    assert set(calls) == set(frames.keys())


def test_raising_flush_does_not_abort_dump():
    frames = _make_frames(main_ident=1)
    calls = []

    def emit(tid, frame, is_main):
        calls.append(tid)

    def bad_flush():
        raise OSError("flush failed -- simulated disk-full")

    ld._dump_tombstones(frames, main_ident=1, emit=emit, flush=bad_flush)
    assert set(calls) == set(frames.keys())


# -- missing MainThread frame (defensive edge case) ----------------------


def test_missing_main_frame_does_not_prevent_remainder_dump():
    """If ``main_ident`` isn't present in ``frames`` (should not happen in
    practice -- sys._current_frames() always includes MainThread -- but
    the dump path must be defensive), the remaining threads are still
    dumped and nothing raises."""
    frames = {2001: _FakeFrame("worker-a"), 2002: _FakeFrame("worker-b")}
    calls = []

    def emit(tid, frame, is_main):
        calls.append((tid, is_main))

    ld._dump_tombstones(frames, main_ident=1, emit=emit)
    assert set(calls) == {(2001, False), (2002, False)}


# -- real _emit_tombstone + _flush_all_logging smoke (not fakes) ---------


def test_emit_tombstone_tags_main_vs_worker(caplog):
    import sys as _sys

    caplog.set_level("CRITICAL", logger="Ouroboros.LoopDeadman")
    frame = _sys._getframe()
    ld._emit_tombstone(999, frame, True)
    ld._emit_tombstone(1000, frame, False)

    records = [r.message for r in caplog.records]
    assert any("[LoopDeadman.TOMBSTONE.MAIN]" in m and "999" in m for m in records)
    assert any(
        "[LoopDeadman.TOMBSTONE]" in m
        and "[LoopDeadman.TOMBSTONE.MAIN]" not in m
        and "1000" in m
        for m in records
    )


def test_flush_all_logging_never_raises(monkeypatch):
    """``_flush_all_logging`` must be best-effort: even if a handler's
    flush() raises, the call must not propagate."""

    class _BadHandler:
        def flush(self):
            raise OSError("disk full")

    root = ld.logging.getLogger()
    root.addHandler(_BadHandler())
    try:
        ld._flush_all_logging()  # must not raise
    finally:
        root.removeHandler(root.handlers[-1])


# -- source-level pin: main-first dump + flush precede os._exit(75) -----


def test_fire_wedge_dumps_main_first_and_flushes_before_exit():
    src = (_GOV / "loop_deadman.py").read_text(encoding="utf-8")

    fire_start = src.index("def _fire_wedge")
    next_def = src.find("\n    def ", fire_start + 1)
    body = src[fire_start:next_def] if next_def != -1 else src[fire_start:]

    dump_call_idx = body.index("_dump_tombstones(")
    # The real (unindented-in-string, executable) call is the LAST
    # occurrence -- earlier occurrences are docstring/comment mentions
    # describing the behavior.
    exit_idx = body.rindex("os._exit(75)")
    assert dump_call_idx < exit_idx, (
        "_fire_wedge must call _dump_tombstones (main-first dump) BEFORE "
        "os._exit(75)"
    )

    # The dump call must be wired to pass threading.main_thread().ident so
    # MainThread is identified structurally, not by convention.
    assert "threading.main_thread().ident" in body, (
        "the real wedge branch must resolve MainThread via "
        "threading.main_thread().ident"
    )

    # The flush helper must be wired into the dump call (passed as the
    # injectable ``flush=`` callback) so the MainThread tombstone --
    # and the remainder -- are flushed before os._exit(75) can cut off
    # buffered I/O. It's passed by reference (not called directly) so
    # _dump_tombstones controls the before/after-remainder timing.
    flush_idx = body.index("_flush_all_logging")
    assert dump_call_idx <= flush_idx < exit_idx, (
        "_flush_all_logging must be wired into the dump call, before "
        "os._exit(75)"
    )


def test_module_defines_dump_helpers():
    assert hasattr(ld, "_dump_tombstones")
    assert hasattr(ld, "_emit_tombstone")
    assert hasattr(ld, "_flush_all_logging")
    assert callable(ld._dump_tombstones)
    assert callable(ld._emit_tombstone)
    assert callable(ld._flush_all_logging)
