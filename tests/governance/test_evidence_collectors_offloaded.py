"""Evidence gathering must not block the event loop.

`_gather_test_set_hash_stable` globbed `tests/**/*.py` and called
`p.is_file()` on every hit, inline in its coroutine. `glob` stats each
entry and `is_file()` stats it again; on a /mnt/c (DrvFS) tree each stat
is a round trip to the Windows filesystem driver. Soak
`bt-2026-09-02-003459` measured the consequence: the main loop blocked
**46.9 s** here during POSTMORTEM, and repeated stalls tripped the
out-of-process heartbeat watchdog, which SIGKILLed the session and lost
every in-flight trajectory — including four pairable sibling groups.

The fix composes the Slice 12U substrate (`cooperative_fs_io.offload` →
the dedicated `advisor-blast` executor) over `bounded_walker.bounded_glob`
rather than growing a private thread hop. These tests pin the properties
that make that correct, not the mechanism:

  1. the loop keeps being serviced while the walk runs;
  2. a TRUNCATED walk reports INSUFFICIENT_EVIDENCE, never a partial
     file set — this claim compares a set HASH, so a short answer is a
     wrong answer, not a smaller one;
  3. the three per-target outcomes (missing / not-a-regular-file /
     unreadable) survive the move off-loop unchanged;
  4. nothing here can raise, and a missing substrate degrades to the
     inline behaviour rather than losing evidence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.verification import evidence_collectors as ec
from backend.core.ouroboros.governance.verification.evidence_collectors import (
    dispatch_evidence_gather,
    reset_registry_for_tests,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _claim(kind: str):
    return SimpleNamespace(property=SimpleNamespace(kind=kind, name="t"))


def _tree(tmp_path: Path, n: int = 3) -> Path:
    (tmp_path / "tests").mkdir()
    for i in range(n):
        (tmp_path / "tests" / f"test_{i}.py").write_text(f"def t{i}(): pass\n")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. The property the wedge violated
# ---------------------------------------------------------------------------


def test_a_slow_walk_does_not_starve_the_event_loop(
    fresh_registry, tmp_path, monkeypatch,
) -> None:
    """THE regression. A walk that takes wall-clock time must not hold
    the loop: the heartbeat coroutine has to keep getting slots.

    `bounded_glob` is replaced with a synchronously-slow stand-in — the
    shape a DrvFS tree has. Run inline, it would block the loop for its
    whole duration and the ticker would score ~0. Run on the executor,
    the ticker keeps counting.
    """
    from backend.core.ouroboros.governance import bounded_walker as bw

    slow_s = 0.6

    def _slow_glob(root, pattern, **kwargs):  # noqa: ANN001
        import time
        time.sleep(slow_s)                      # the DrvFS stat storm
        return bw.BoundedWalkResult(
            matches=[str(root / "test_0.py")],
            outcome=bw.BoundedWalkOutcome.COMPLETE,
            scanned_count=1,
        )

    monkeypatch.setattr(bw, "bounded_glob", _slow_glob)

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    async def _drive() -> Any:
        beat = asyncio.ensure_future(_heartbeat())
        try:
            ctx = SimpleNamespace(
                test_files_pre=("tests/test_0.py",), target_dir=str(_tree(tmp_path)),
            )
            return await dispatch_evidence_gather(
                _claim("test_set_hash_stable"), ctx,
            )
        finally:
            beat.cancel()

    result = asyncio.run(_drive())
    assert result["test_files_post"], "the walk still has to produce its answer"
    # Inline, the loop is dead for `slow_s` and this is ~0. Offloaded, the
    # 10 ms ticker gets tens of slots. The bar is deliberately far below
    # the theoretical count so a loaded CI box cannot make it flaky.
    assert ticks >= 10, f"event loop was starved during the walk (ticks={ticks})"


def test_the_starvation_test_actually_discriminates(
    fresh_registry, tmp_path, monkeypatch,
) -> None:
    """Prove the test above measures the fix and not something incidental.

    `JARVIS_COOPERATIVE_FS_IO_ENABLED=false` is the substrate's documented
    byte-identical rollback: `offload` runs the callable IN-LINE. Under it
    the very same walk starves the loop completely — measured ticks=0 —
    which is precisely the pre-fix behaviour. A starvation assertion that
    passed in both modes would be worthless, so this pins the difference.

    It also states the operational consequence plainly: turning that flag
    off REINSTATES the wedge that SIGKILLed soak bt-2026-09-02-003459.
    """
    from backend.core.ouroboros.governance import bounded_walker as bw

    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")

    def _slow_glob(root, pattern, **kwargs):  # noqa: ANN001
        import time
        time.sleep(0.4)
        return bw.BoundedWalkResult(
            matches=[], outcome=bw.BoundedWalkOutcome.COMPLETE, scanned_count=0,
        )

    monkeypatch.setattr(bw, "bounded_glob", _slow_glob)
    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    async def _drive() -> Any:
        beat = asyncio.ensure_future(_heartbeat())
        try:
            await asyncio.sleep(0)          # let the ticker reach its await
            before = ticks
            await ec._walk_tests_offloaded(tmp_path)
            return ticks - before
        finally:
            beat.cancel()

    during = asyncio.run(_drive())
    assert during <= 2, (
        "inline fallback should starve the loop; if this now passes, the "
        f"master switch no longer routes in-line (ticks during walk={during})"
    )


# ---------------------------------------------------------------------------
# 2. Truncation is an absence of evidence, not a smaller answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome_name",
    ["TRUNCATED_SCANNED", "TRUNCATED_MATCHES", "TRUNCATED_TIMEOUT"],
)
def test_a_truncated_walk_reports_insufficient_evidence(
    fresh_registry, tmp_path, monkeypatch, outcome_name: str,
) -> None:
    """A budget-truncated post-set would hash differently from the pre-set
    and fail a candidate that changed nothing. Every truncation reason
    must degrade to INSUFFICIENT_EVIDENCE."""
    from backend.core.ouroboros.governance import bounded_walker as bw

    def _truncated(root, pattern, **kwargs):  # noqa: ANN001
        return bw.BoundedWalkResult(
            matches=[str(root / "test_0.py")],          # a PARTIAL set
            outcome=getattr(bw.BoundedWalkOutcome, outcome_name),
            scanned_count=1,
        )

    monkeypatch.setattr(bw, "bounded_glob", _truncated)
    ctx = SimpleNamespace(
        test_files_pre=("tests/test_0.py",), target_dir=str(_tree(tmp_path)),
    )
    assert asyncio.run(
        dispatch_evidence_gather(_claim("test_set_hash_stable"), ctx),
    ) == {}


def test_a_complete_walk_returns_the_whole_set(fresh_registry, tmp_path) -> None:
    ctx = SimpleNamespace(
        test_files_pre=("tests/test_0.py",), target_dir=str(_tree(tmp_path, 3)),
    )
    result = asyncio.run(
        dispatch_evidence_gather(_claim("test_set_hash_stable"), ctx),
    )
    assert len(result["test_files_post"]) == 3
    assert result["test_files_pre"] == ["tests/test_0.py"]


def test_directories_are_traversed_never_matched(fresh_registry, tmp_path) -> None:
    """The discarded `is_file()` guard is now the walker's job: it yields
    regular files only. A directory named `*.py` must not appear."""
    root = _tree(tmp_path, 1)
    (root / "tests" / "nested.py").mkdir()                 # a DIRECTORY
    (root / "tests" / "nested.py" / "test_deep.py").write_text("def d(): pass\n")
    ctx = SimpleNamespace(test_files_pre=("x",), target_dir=str(root))
    post = asyncio.run(
        dispatch_evidence_gather(_claim("test_set_hash_stable"), ctx),
    )["test_files_post"]
    assert not any(p.endswith("nested.py") for p in post)
    assert any(p.endswith("test_deep.py") for p in post), "recursion still works"


def test_a_missing_tests_dir_is_an_empty_set_not_an_error(
    fresh_registry, tmp_path,
) -> None:
    ctx = SimpleNamespace(test_files_pre=("x",), target_dir=str(tmp_path))
    result = asyncio.run(
        dispatch_evidence_gather(_claim("test_set_hash_stable"), ctx),
    )
    assert result["test_files_post"] == []


def test_walk_helper_returns_none_when_the_substrate_faults(
    tmp_path, monkeypatch,
) -> None:
    """`offload` reports failure by RETURNING a sentinel, so the helper
    must inspect the value — a bare try/except would hand the sentinel on
    as if it were a walk result."""
    from backend.core.ouroboros.governance import cooperative_fs_io as cfs

    class _Sentinel(cfs.OffloadError):
        pass

    async def _fake_offload(fn, *a, **kw):  # noqa: ANN001
        return _Sentinel("pool down")

    monkeypatch.setattr(cfs, "offload", _fake_offload)
    assert asyncio.run(ec._walk_tests_offloaded(tmp_path)) is None

    async def _raising_offload(fn, *a, **kw):  # noqa: ANN001
        raise RuntimeError("executor gone")

    monkeypatch.setattr(cfs, "offload", _raising_offload)
    assert asyncio.run(ec._walk_tests_offloaded(tmp_path)) is None


# ---------------------------------------------------------------------------
# 3. Per-target outcomes survive the move off-loop
# ---------------------------------------------------------------------------


def test_the_three_per_target_outcomes_are_unchanged(tmp_path) -> None:
    real = tmp_path / "mod.py"
    real.write_text("x = 1\n", encoding="utf-8")
    a_dir = tmp_path / "adir.py"
    a_dir.mkdir()

    assert ec._stat_and_read(str(real)) == {"path": str(real), "content": "x = 1\n"}
    # Missing -> absence IS evidence (absent .py reads as a parse failure).
    missing = ec._stat_and_read(str(tmp_path / "gone.py"))
    assert missing == {"path": str(tmp_path / "gone.py"), "content": ""}
    # A directory is not a parse failure and not evidence — skipped.
    assert ec._stat_and_read(str(a_dir)) is None
    # Garbage never raises.
    assert ec._stat_and_read("") in (None, {"path": "", "content": ""})


def test_undecodable_bytes_are_replaced_not_dropped(tmp_path) -> None:
    """errors='replace' is load-bearing: a file with one bad byte is still
    the candidate's content, and dropping it would fabricate a gap."""
    p = tmp_path / "bad.py"
    p.write_bytes(b"x = '\xff'\n")
    got = ec._stat_and_read(str(p))
    assert got is not None and got["content"]


def test_targets_are_read_concurrently_and_in_order(
    fresh_registry, tmp_path,
) -> None:
    """Order must match `ctx.target_files` — the evaluator pairs records
    positionally — even though the reads run concurrently."""
    paths: List[str] = []
    for i in range(5):
        p = tmp_path / f"m{i}.py"
        p.write_text(f"v = {i}\n")
        paths.append(str(p))
    ctx = SimpleNamespace(target_files=tuple(paths))
    out = asyncio.run(
        dispatch_evidence_gather(_claim("file_parses_after_change"), ctx),
    )["target_files_post"]
    assert [r["path"] for r in out] == paths


def test_one_unreadable_target_does_not_discard_the_others(
    fresh_registry, tmp_path, monkeypatch,
) -> None:
    good = tmp_path / "good.py"
    good.write_text("ok = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_text("nope\n")

    real = ec._stat_and_read

    def _flaky(path_str: str):
        if path_str.endswith("bad.py"):
            return None
        return real(path_str)

    monkeypatch.setattr(ec, "_stat_and_read", _flaky)
    ctx = SimpleNamespace(target_files=(str(good), str(bad)))
    out = asyncio.run(
        dispatch_evidence_gather(_claim("file_parses_after_change"), ctx),
    )["target_files_post"]
    assert [r["path"] for r in out] == [str(good)]


def test_reads_fall_back_inline_when_the_substrate_is_absent(
    tmp_path, monkeypatch,
) -> None:
    """A bare checkout must still produce evidence, not lose it."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):  # noqa: ANN001
        if name.endswith("cooperative_fs_io"):
            raise ImportError("substrate absent")
        return real_import(name, *a, **kw)

    p = tmp_path / "m.py"
    p.write_text("z = 9\n")
    monkeypatch.setattr(builtins, "__import__", _blocked)
    got = asyncio.run(ec._read_targets_offloaded([str(p)]))
    assert got == [{"path": str(p), "content": "z = 9\n"}]


def test_a_gather_exception_yields_none_per_target_not_a_raise(
    tmp_path, monkeypatch,
) -> None:
    from backend.core.ouroboros.governance import cooperative_fs_io as cfs

    async def _boom(fn, *a, **kw):  # noqa: ANN001
        raise RuntimeError("pool exploded")

    monkeypatch.setattr(cfs, "offload", _boom)
    got = asyncio.run(ec._read_targets_offloaded(["a.py", "b.py"]))
    assert got == [None, None]


# ---------------------------------------------------------------------------
# 4. Never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["test_set_hash_stable", "file_parses_after_change"])
def test_garbage_ctx_never_raises(fresh_registry, kind: str) -> None:
    for ctx in (None, object(), SimpleNamespace(), SimpleNamespace(target_dir=123)):
        assert isinstance(
            asyncio.run(dispatch_evidence_gather(_claim(kind), ctx)), dict,
        )
